from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS

from app.config import Settings
from app.schemas import SearchResultItem


class SearchService:
    _BLOCKED_DOMAINS = {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "play.google.com",
        "apps.apple.com",
        "music.apple.com",
        "itunes.apple.com",
        "tiktok.com",
        "www.tiktok.com",
        "instagram.com",
        "www.instagram.com",
        "facebook.com",
        "www.facebook.com",
        "x.com",
        "www.x.com",
    }

    def __init__(self, settings: Settings):
        self.settings = settings

    async def discover(self, queries: list[str]) -> list[SearchResultItem]:
        return await asyncio.to_thread(self._discover_sync, queries)

    def _discover_sync(self, queries: list[str]) -> list[SearchResultItem]:
        seen_urls: set[str] = set()
        results: list[SearchResultItem] = []
        rank = 1

        ddgs = DDGS(timeout=self.settings.network_timeout_seconds)
        with ddgs:
            for query in queries:
                for item in ddgs.text(query, max_results=self.settings.search_result_limit, backend="auto"):
                    url = item.get("href")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    domain = urlparse(url).netloc or None
                    if self._is_blocked_domain(domain):
                        continue
                    results.append(
                        SearchResultItem(
                            url=url,
                            title=item.get("title") or url,
                            snippet=item.get("body"),
                            domain=domain,
                            rank=rank,
                        )
                    )
                    rank += 1
                    if len(results) >= self.settings.search_result_limit:
                        return results

        if len(results) < self.settings.search_result_limit:
            rss_results = self._discover_google_news_rss(
                queries=queries,
                seen_urls=seen_urls,
                starting_rank=rank,
            )
            for item in rss_results:
                results.append(item)
                if len(results) >= self.settings.search_result_limit:
                    return results

        if not results:
            proxy_hint = ""
            if not self.settings.preferred_proxy:
                proxy_hint = " Configure HTTP_PROXY/HTTPS_PROXY if your network requires an outbound proxy."
            raise RuntimeError(f"No searchable sources were discovered for this task.{proxy_hint}")

        return results

    def _discover_google_news_rss(
        self,
        queries: list[str],
        seen_urls: set[str],
        starting_rank: int,
    ) -> list[SearchResultItem]:
        client_kwargs: dict[str, object] = {
            "timeout": self.settings.network_timeout_seconds,
            "follow_redirects": True,
            "trust_env": False,
            "headers": {"User-Agent": "Grafinder/0.1"},
        }
        if self.settings.preferred_proxy:
            client_kwargs["proxy"] = self.settings.preferred_proxy

        results: list[SearchResultItem] = []
        rank = starting_rank
        with httpx.Client(**client_kwargs) as client:
            for query in queries:
                response = client.get(
                    "https://news.google.com/rss/search",
                    params={
                        "q": query,
                        "hl": "zh-CN",
                        "gl": "CN",
                        "ceid": "CN:zh-Hans",
                    },
                )
                response.raise_for_status()
                root = ET.fromstring(response.text)
                for item in root.findall("./channel/item"):
                    url = item.findtext("link")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)

                    raw_title = (item.findtext("title") or url).strip()
                    source_node = item.find("source")
                    source_name = source_node.text.strip() if source_node is not None and source_node.text else None
                    source_url = source_node.attrib.get("url") if source_node is not None else None
                    domain = urlparse(source_url).netloc or "news.google.com"

                    if source_name and raw_title.endswith(f" - {source_name}"):
                        title = raw_title[: -(len(source_name) + 3)].strip()
                    else:
                        title = raw_title

                    description_html = item.findtext("description") or ""
                    description_text = BeautifulSoup(description_html, "html.parser").get_text(" ", strip=True)
                    if source_name and description_text.endswith(source_name):
                        description_text = description_text[: -len(source_name)].strip()

                    snippet_parts = []
                    if description_text:
                        snippet_parts.append(f"Summary: {description_text}")
                    if source_name:
                        snippet_parts.append(f"Source: {source_name}")
                    published = item.findtext("pubDate")
                    if published:
                        snippet_parts.append(f"Published: {published}")
                    snippet = "\n".join(snippet_parts) or None

                    results.append(
                        SearchResultItem(
                            url=url,
                            title=title or url,
                            snippet=snippet,
                            domain=domain,
                            rank=rank,
                        )
                    )
                    rank += 1
                    if len(results) >= self.settings.search_result_limit:
                        return results
        return results

    @classmethod
    def _is_blocked_domain(cls, domain: str | None) -> bool:
        if not domain:
            return False
        normalized = domain.lower()
        return normalized in cls._BLOCKED_DOMAINS
