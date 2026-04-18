from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from duckduckgo_search import DDGS

from app.config import Settings
from app.schemas import SearchResultItem


class SearchService:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def discover(self, queries: list[str]) -> list[SearchResultItem]:
        return await asyncio.to_thread(self._discover_sync, queries)

    def _discover_sync(self, queries: list[str]) -> list[SearchResultItem]:
        seen_urls: set[str] = set()
        results: list[SearchResultItem] = []
        rank = 1

        with DDGS() as ddgs:
            for query in queries:
                for item in ddgs.text(query, max_results=self.settings.search_result_limit):
                    url = item.get("href")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    domain = urlparse(url).netloc or None
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

        if not results:
            raise RuntimeError("No searchable sources were discovered for this task.")

        return results

