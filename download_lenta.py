import argparse
import asyncio
import csv
import logging
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from multiprocessing import cpu_count
from typing import List, Optional

import aiohttp
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s @ %(message)s",
    datefmt="%d-%m-%Y %H:%M:%S",
)
logger = logging.getLogger(name="LentaParser")


class LentaParser:

    # lxml is much faster but error prone
    default_parser = "html.parser"

    def __init__(
        self,
        *,
        max_workers: int,
        outfile_name: str,
        from_date: str,
        to_date: Optional[str] = None,
        with_date: bool = False,
        with_related_articles: bool = False,
    ):
        self._endpoint = "https://lenta.ru/news"

        self._sess = None
        self._connector = None

        self._executor = ProcessPoolExecutor(max_workers=max_workers)

        self._outfile_name = outfile_name
        self._outfile = None
        self._csv_writer = None
        self.timeouts = aiohttp.ClientTimeout(total=60, connect=60)

        self._n_downloaded = 0
        self._from_date = datetime.strptime(from_date, "%d.%m.%Y")
        self._to_date = datetime.strptime(to_date, "%d.%m.%Y") if to_date else datetime.today()

        self._with_date = with_date
        self._with_related_articles = with_related_articles

    @property
    def dates_countdown(self):
        date_start, date_end = self._from_date, self._to_date

        while date_start <= date_end:
            yield date_start.strftime("%Y/%m/%d")
            date_start += timedelta(days=1)

    @property
    def _csv_fields(self):
        base_fields = ["url", "title", "text", "topic", "tags"]

        if self._with_date:
            base_fields.append("date")

        if self._with_related_articles:
            base_fields.append("related_articles")

        return base_fields

    @property
    def writer(self):
        if self._csv_writer is None:
            self._outfile = open(self._outfile_name, "w", 1)
            self._csv_writer = csv.DictWriter(
                self._outfile, fieldnames=self._csv_fields
            )
            self._csv_writer.writeheader()

        return self._csv_writer

    @property
    def session(self):
        if self._sess is None or self._sess.closed:

            self._connector = aiohttp.TCPConnector(
                use_dns_cache=True, ttl_dns_cache=60 * 60, limit=1024
            )
            self._sess = aiohttp.ClientSession(
                connector=self._connector, timeout=self.timeouts
            )

        return self._sess

    async def fetch(self, url: str):
        response = await self.session.get(url, allow_redirects=False)
        response.raise_for_status()
        return await response.text(encoding="utf-8")

    @staticmethod
    def _parse_related_articles(soup: BeautifulSoup) -> List[str]:
        article_links = [l.get("href") for l in soup.find_all("a")]
        return list(filter(lambda l: l.startswith("https://lenta.ru"), article_links))

    @staticmethod
    def parse_article_html(html: str, with_related: bool = False):
        parsed_article = {}

        doc_tree = BeautifulSoup(html, LentaParser.default_parser)
        tags = doc_tree.find("a", "item dark active")
        parsed_article["tags"] = tags.get_text() if tags else None

        body = doc_tree.find("div", attrs={"itemprop": "articleBody"})

        if not body:
            raise RuntimeError(f"Article body is not found")

        parsed_article["text"] = " ".join([p.get_text() for p in body.find_all("p")])

        topic = doc_tree.find("a", "b-header-inner__block")
        parsed_article["topic"] = topic.get_text() if topic else None

        title = doc_tree.find("h1", attrs={"itemprop": "headline"})
        parsed_article["title"] = title.get_text() if title else None

        if with_related:
            parsed_article["related_articles"] = LentaParser._parse_related_articles(
                body
            )

        return parsed_article

    @staticmethod
    def _extract_urls_from_html(html: str):
        doc_tree = BeautifulSoup(html, LentaParser.default_parser)
        news_list = doc_tree.find_all("div", "item news b-tabloid__topic_news")
        return tuple(f"https://lenta.ru{news.find('a')['href']}" for news in news_list)

    async def _fetch_all_news_on_page(self, html: str, date: datetime):
        # Get news URLs from raw html
        loop = asyncio.get_running_loop()
        news_urls = await loop.run_in_executor(
            self._executor, self._extract_urls_from_html, html
        )

        # Fetching news
        tasks = tuple(asyncio.create_task(self.fetch(url)) for url in news_urls)

        fetched_raw_news = dict()

        for i, task in enumerate(tasks):
            try:
                fetch_res = await task
            except aiohttp.ClientResponseError as exc:
                logger.error(f"Cannot fetch {exc.request_info.url}: {exc}")
            except asyncio.TimeoutError:
                logger.exception("Cannot fetch. Timeout")
            else:
                fetched_raw_news[news_urls[i]] = fetch_res

        for url, html in fetched_raw_news.items():
            fetched_raw_news[url] = loop.run_in_executor(
                self._executor,
                self.parse_article_html,
                html,
                self._with_related_articles,
            )

        parsed_news = []

        for url, task in fetched_raw_news.items():
            try:
                parse_res = await task
            except Exception:
                logger.exception(f"Cannot parse {url}")
            else:
                parse_res["url"] = url
                if self._with_date:
                    parse_res["date"] = date
                parsed_news.append(parse_res)

        if parsed_news:
            self.writer.writerows(parsed_news)
            self._n_downloaded += len(parsed_news)

        return len(parsed_news)

    async def shutdown(self):
        if self._sess is not None:
            await self._sess.close()

        await asyncio.sleep(0.5)

        if self._outfile is not None:
            self._outfile.close()

        self._executor.shutdown(wait=True)

        logger.info(f"{self._n_downloaded} news saved at {self._outfile_name}")

    async def _producer(self):
        for date in self.dates_countdown:
            news_page_url = f"{self._endpoint}/{date}"

            try:
                html = await asyncio.create_task(self.fetch(news_page_url))
            except aiohttp.ClientResponseError:
                logger.exception(f"Cannot fetch {news_page_url}")
            except aiohttp.ClientConnectionError:
                logger.exception(f"Cannot fetch {news_page_url}")
            else:
                n_proccessed_news = await self._fetch_all_news_on_page(html, date)

                if n_proccessed_news == 0:
                    logger.info(f"News not found at {news_page_url}.")

                logger.info(
                    f"{news_page_url} processed ({n_proccessed_news} news). "
                    f"{self._n_downloaded} news saved totally."
                )

    async def run(self):
        try:
            await self._producer()
        finally:
            await self.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Downloads news from Lenta.Ru")

    parser.add_argument(
        "--outfile", default="lenta-ru-news.csv", help="name of result file"
    )

    parser.add_argument(
        "--cpu-workers", default=cpu_count(), type=int, help="number of workers"
    )

    parser.add_argument(
        "--from-date",
        default="30.08.1999",
        type=str,
        help="download news from this date. Example: 30.08.1999",
    )

    parser.add_argument(
        "--to-date",
        default="30.08.1999",
        type=str,
        help="download news until this date. Example: 30.08.1999",
    )

    parser.add_argument(
        "--with-date",
        default=False,
        action="store_true",
        help="extract dates from news",
    )

    parser.add_argument(
        "--with-related-articles",
        default=False,
        action="store_true",
        help="extract related articles from news",
    )

    args = parser.parse_args()

    parser = LentaParser(
        max_workers=args.cpu_workers,
        outfile_name=args.outfile,
        from_date=args.from_date,
        to_date=args.to_date,
        with_date=args.with_date,
        with_related_articles=args.with_related_articles,
    )

    try:
        asyncio.run(parser.run())
    except KeyboardInterrupt:
        asyncio.run(parser.shutdown())
        logger.info("KeyboardInterrupt, exiting...")


if __name__ == "__main__":
    main()
