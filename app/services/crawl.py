from __future__ import annotations

import inspect
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from app.config import Settings
from app.schemas import CrawledDocument, SearchResultItem


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = date_parser.parse(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class CrawlService:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def crawl(self, sources: list[SearchResultItem]) -> list[CrawledDocument]:
        try:
            return await self._crawl_with_crawl4ai(sources)
        except Exception:
            return await self._crawl_with_httpx(sources)

    async def _crawl_with_crawl4ai(self, sources: list[SearchResultItem]) -> list[CrawledDocument]:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

        browser_channel = self._detect_browser_channel()
        browser_config = BrowserConfig(
            headless=True,
            verbose=False,
            channel=browser_channel,
            chrome_channel=browser_channel,
        )
        crawler_kwargs: dict[str, Any] = {}
        crawler_init = inspect.signature(AsyncWebCrawler)
        if "browser_config" in crawler_init.parameters:
            crawler_kwargs["browser_config"] = browser_config
        elif "config" in crawler_init.parameters:
            crawler_kwargs["config"] = browser_config

        run_config = CrawlerRunConfig(cache_mode=getattr(CacheMode, "BYPASS", None))
        arun_signature = inspect.signature(AsyncWebCrawler.arun)
        run_config_key = "config" if "config" in arun_signature.parameters else "crawler_config"

        documents: list[CrawledDocument] = []
        async with AsyncWebCrawler(**crawler_kwargs) as crawler:
            for source in sources:
                kwargs: dict[str, Any] = {"url": source.url}
                if run_config_key in arun_signature.parameters:
                    kwargs[run_config_key] = run_config
                result = await crawler.arun(**kwargs)
                if not getattr(result, "success", True):
                    continue

                markdown = self._pick_markdown(result)
                if not markdown:
                    continue

                metadata = getattr(result, "metadata", {}) or {}
                documents.append(
                    CrawledDocument(
                        url=source.url,
                        title=getattr(result, "title", None) or source.title,
                        source_name=metadata.get("site_name") or source.domain,
                        published_at=_parse_datetime(metadata.get("published")) or _parse_datetime(metadata.get("date")),
                        markdown=markdown[: self.settings.crawl_max_markdown_chars],
                        raw_metadata=metadata,
                    )
                )

        if not documents:
            raise RuntimeError("Crawl4AI did not produce any usable pages.")

        return documents

    async def _crawl_with_httpx(self, sources: list[SearchResultItem]) -> list[CrawledDocument]:
        documents: list[CrawledDocument] = []
        timeout = httpx.Timeout(self.settings.crawl_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            for source in sources:
                response = await client.get(source.url, headers={"User-Agent": "Grafinder/0.1"})
                response.raise_for_status()
                soup = BeautifulSoup(response.text, "html.parser")
                for tag in soup(["script", "style", "noscript"]):
                    tag.decompose()
                markdown = soup.get_text(separator="\n", strip=True)
                published = None
                for selector in [
                    ('meta[property="article:published_time"]', "content"),
                    ('meta[name="publishdate"]', "content"),
                    ('time', "datetime"),
                ]:
                    node = soup.select_one(selector[0])
                    if node:
                        published = _parse_datetime(node.get(selector[1]))
                        if published:
                            break

                documents.append(
                    CrawledDocument(
                        url=source.url,
                        title=soup.title.get_text(strip=True) if soup.title else source.title,
                        source_name=source.domain,
                        published_at=published,
                        markdown=markdown[: self.settings.crawl_max_markdown_chars],
                        raw_metadata={},
                    )
                )

        return documents

    @staticmethod
    def _pick_markdown(result: Any) -> str:
        markdown_v2 = getattr(result, "markdown_v2", None)
        if markdown_v2 is not None:
            for attr in ("raw_markdown", "markdown"):
                value = getattr(markdown_v2, attr, None)
                if value:
                    return value

        for attr in ("markdown", "fit_markdown", "cleaned_html"):
            value = getattr(result, attr, None)
            if isinstance(value, str) and value.strip():
                return value

        return ""

    @staticmethod
    def _detect_browser_channel() -> str:
        if Path("/Applications/Google Chrome.app").exists():
            return "chrome"
        if shutil.which("google-chrome") or shutil.which("google-chrome-stable"):
            return "chrome"
        return "chromium"
