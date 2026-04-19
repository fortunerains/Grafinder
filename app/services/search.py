from __future__ import annotations

import asyncio
import base64
import re
from urllib.parse import parse_qs, unquote, urlparse
import xml.etree.ElementTree as ET

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
        "smartapps.baidu.com",
        "f7c3we.smartapps.baidu.com",
    }
    _DIRECT_SOURCE_DOMAIN_BOOSTS = {
        "mysteel.com": 18,
        "eastmoney.com": 16,
        "data.eastmoney.com": 20,
        "quote.eastmoney.com": 18,
        "qianzhan.com": 14,
        "smm.cn": 14,
        "100ppi.com": 14,
        "miit.gov.cn": 12,
        "gov.cn": 10,
        "benchmarkminerals.com": 14,
        "trendforce.com": 14,
        "fastmarkets.com": 12,
        "tradingeconomics.com": 12,
    }
    _DEPRIORITIZED_DOMAIN_BOOSTS = {
        "baijiahao.baidu.com": -18,
        "jiemian.com": -8,
        "ce.cn": -6,
        "chinadaily.com.cn": -6,
        "sohu.com": -8,
        "163.com": -8,
        "qq.com": -8,
        "toutiao.com": -10,
        "news.sina.com.cn": -8,
        "finance.sina.com.cn": -5,
        "book118.com": -16,
        "guba.sina.com.cn": -14,
        "10jqka.com.cn": -10,
        "hexun.com": -8,
    }
    _DIRECT_DATA_MARKERS = (
        "价格",
        "行情",
        "数据",
        "报告",
        "研究",
        "统计",
        "趋势",
        "走势",
        "图谱",
        "历史价格",
        "周报",
        "月报",
        "季报",
        "指数",
        "市场价格",
        "price",
        "prices",
        "data",
        "report",
        "analysis",
        "trend",
        "historical",
        "quote",
        "chart",
    )
    _STRUCTURED_PAGE_MARKERS = (
        "list",
        "quote",
        "price",
        "prices",
        "report",
        "analyst",
        "detail",
        "index",
        "chart",
        "data",
        "行情",
        "价格",
        "走势",
        "报告",
        "数据",
        "图谱",
        "统计",
    )
    _NEWS_MARKERS = (
        "新闻",
        "观察",
        "播报",
        "快讯",
        "记者",
        "一文",
        "爆火",
        "为什么",
        "news",
        "breaking",
        "interview",
    )

    def __init__(self, settings: Settings):
        self.settings = settings

    async def discover(
        self,
        queries: list[str],
        *,
        keyword: str | None = None,
        intent: str | None = None,
        source_hint: str | None = None,
    ) -> list[SearchResultItem]:
        return await asyncio.to_thread(self._discover_sync, queries, keyword, intent, source_hint)

    def _discover_sync(
        self,
        queries: list[str],
        keyword: str | None = None,
        intent: str | None = None,
        source_hint: str | None = None,
    ) -> list[SearchResultItem]:
        seen_urls: set[str] = set()
        results: list[SearchResultItem] = []
        rank = 1
        ddgs_cap = max(self.settings.search_result_limit + 2, 10)
        ddgs_count = 0

        ddgs = DDGS(timeout=self.settings.network_timeout_seconds, proxy=self.settings.preferred_proxy)
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
                    ddgs_count += 1
                    if ddgs_count >= ddgs_cap:
                        break
                if ddgs_count >= ddgs_cap:
                    break

        is_chinese_query = any(not self._looks_english(query) for query in queries)

        if is_chinese_query:
            sogou_results = self._discover_sogou_html(
                queries=queries,
                seen_urls=seen_urls,
                starting_rank=rank,
            )
            results.extend(sogou_results)
            rank = len(results) + 1

            baidu_results = self._discover_baidu_html(
                queries=queries,
                seen_urls=seen_urls,
                starting_rank=rank,
            )
            results.extend(baidu_results)
            rank = len(results) + 1

        bing_results = self._discover_bing_html(
            queries=queries,
            seen_urls=seen_urls,
            starting_rank=rank,
        )
        results.extend(bing_results)
        rank = len(results) + 1

        if not is_chinese_query:
            baidu_results = self._discover_baidu_html(
                queries=queries,
                seen_urls=seen_urls,
                starting_rank=rank,
            )
            results.extend(baidu_results)
            rank = len(results) + 1

        filtered_results = self._filter_relevant_results(results, queries)
        rescue_context = " ".join(part for part in [keyword or "", intent or "", source_hint or "", *queries] if part).strip()
        if self._needs_direct_data_rescue(filtered_results, rescue_context):
            rescue_queries = self._direct_data_rescue_queries(keyword or rescue_context)
            rescue_rank = len(results) + 1
            rescue_results = self._discover_sogou_html(
                queries=rescue_queries,
                seen_urls=seen_urls,
                starting_rank=rescue_rank,
            )
            rescue_rank = len(results) + len(rescue_results) + 1
            rescue_results.extend(
                self._discover_baidu_html(
                    queries=rescue_queries,
                    seen_urls=seen_urls,
                    starting_rank=rescue_rank,
                )
            )
            rescue_rank = len(results) + len(rescue_results) + 1
            rescue_results.extend(
                self._discover_bing_html(
                    queries=rescue_queries,
                    seen_urls=seen_urls,
                    starting_rank=rescue_rank,
                )
            )
            filtered_results = self._filter_relevant_results([*results, *rescue_results], [*queries, *rescue_queries])
        if len(filtered_results) < self.settings.search_result_limit:
            rss_results = self._discover_google_news_rss(
                queries=queries,
                seen_urls=seen_urls,
                starting_rank=rank,
            )
            filtered_results = self._filter_relevant_results([*results, *rss_results], queries)

        if not filtered_results:
            proxy_hint = ""
            if not self.settings.preferred_proxy:
                proxy_hint = " Configure HTTP_PROXY/HTTPS_PROXY if your network requires an outbound proxy."
            raise RuntimeError(f"No searchable sources were discovered for this task.{proxy_hint}")

        reranked = self._rerank_results(filtered_results, queries, source_hint)
        return [
            item.model_copy(update={"rank": index})
            for index, item in enumerate(reranked[: self.settings.search_result_limit], start=1)
        ]

    def select_for_crawl(
        self,
        sources: list[SearchResultItem],
        keyword: str,
        intent: str,
        source_hint: str | None,
        max_documents: int,
    ) -> list[SearchResultItem]:
        if len(sources) <= max_documents:
            return sources

        query_text = f"{keyword} {intent}"
        ranked = sorted(
            sources,
            key=lambda item: (self._result_score(item, query_text, crawl_mode=True, source_hint=source_hint), -item.rank),
            reverse=True,
        )
        selected = ranked[:max_documents]
        selected_urls = {item.url for item in selected}
        ordered = [item for item in sources if item.url in selected_urls]
        return ordered[:max_documents]

    def _discover_bing_html(
        self,
        queries: list[str],
        seen_urls: set[str],
        starting_rank: int,
    ) -> list[SearchResultItem]:
        client_kwargs: dict[str, object] = {
            "timeout": self.settings.network_timeout_seconds,
            "follow_redirects": True,
            "trust_env": False,
            "headers": {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"},
        }
        if self.settings.preferred_proxy:
            client_kwargs["proxy"] = self.settings.preferred_proxy

        results: list[SearchResultItem] = []
        rank = starting_rank
        with httpx.Client(**client_kwargs) as client:
            for query in queries:
                response = client.get("https://www.bing.com/search", params={"q": query, "setlang": "en-US"})
                response.raise_for_status()
                soup = BeautifulSoup(response.text, "html.parser")

                for node in soup.select("li.b_algo"):
                    link = node.select_one("h2 a")
                    if link is None:
                        continue

                    href = (link.get("href") or "").strip()
                    title = link.get_text(" ", strip=True)
                    if not href or not title:
                        continue

                    resolved_url = self._resolve_bing_result_url(client, href) or href
                    if resolved_url in seen_urls:
                        continue
                    domain = urlparse(resolved_url).netloc or None
                    if self._is_blocked_domain(domain):
                        continue

                    snippet_node = node.select_one(".b_caption p")
                    snippet = snippet_node.get_text(" ", strip=True) if snippet_node else None
                    seen_urls.add(resolved_url)
                    results.append(
                        SearchResultItem(
                            url=resolved_url,
                            title=title,
                            snippet=snippet,
                            domain=domain,
                            rank=rank,
                        )
                    )
                    rank += 1
                    if len(results) >= self._candidate_cap():
                        return results
        return results

    def _discover_baidu_html(
        self,
        queries: list[str],
        seen_urls: set[str],
        starting_rank: int,
    ) -> list[SearchResultItem]:
        client_kwargs: dict[str, object] = {
            "timeout": self.settings.network_timeout_seconds,
            "follow_redirects": True,
            "trust_env": False,
            "headers": {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"},
        }
        if self.settings.preferred_proxy:
            client_kwargs["proxy"] = self.settings.preferred_proxy

        results: list[SearchResultItem] = []
        rank = starting_rank
        with httpx.Client(**client_kwargs) as client:
            for query in queries:
                response = client.get("http://www.baidu.com/s", params={"wd": query})
                response.raise_for_status()
                soup = BeautifulSoup(response.text, "html.parser")
                candidates = soup.select("div.result, div.result-op, div.c-container")

                for node in candidates:
                    link = node.select_one("h3 a")
                    if link is None:
                        continue

                    href = (link.get("href") or "").strip()
                    title = link.get_text(" ", strip=True)
                    if not href or not title:
                        continue

                    resolved_url = self._resolve_baidu_result_url(client, href) or href
                    if resolved_url in seen_urls:
                        continue
                    domain = urlparse(resolved_url).netloc or None
                    if self._is_blocked_domain(domain):
                        continue

                    snippet = self._extract_baidu_snippet(node, title)
                    seen_urls.add(resolved_url)
                    results.append(
                        SearchResultItem(
                            url=resolved_url,
                            title=title,
                            snippet=snippet,
                            domain=domain,
                            rank=rank,
                        )
                    )
                    rank += 1
                    if len(results) >= self._candidate_cap():
                        return results

        return results

    def _discover_sogou_html(
        self,
        queries: list[str],
        seen_urls: set[str],
        starting_rank: int,
    ) -> list[SearchResultItem]:
        client_kwargs: dict[str, object] = {
            "timeout": self.settings.network_timeout_seconds,
            "follow_redirects": True,
            "trust_env": False,
            "headers": {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"},
        }
        if self.settings.preferred_proxy:
            client_kwargs["proxy"] = self.settings.preferred_proxy

        results: list[SearchResultItem] = []
        rank = starting_rank
        with httpx.Client(**client_kwargs) as client:
            for query in queries:
                response = client.get("https://www.sogou.com/web", params={"query": query})
                response.raise_for_status()
                soup = BeautifulSoup(response.text, "html.parser")

                for node in soup.select("div.vrwrap"):
                    link = node.select_one("h3 a")
                    if link is None:
                        continue

                    href = (link.get("href") or "").strip()
                    title = link.get_text(" ", strip=True)
                    if not href or not title:
                        continue

                    resolved_url = self._resolve_sogou_result_url(client, href) or href
                    if not resolved_url.startswith("http") or resolved_url in seen_urls:
                        continue
                    domain = urlparse(resolved_url).netloc or None
                    if self._is_blocked_domain(domain):
                        continue

                    snippet = self._extract_sogou_snippet(node, title)
                    seen_urls.add(resolved_url)
                    results.append(
                        SearchResultItem(
                            url=resolved_url,
                            title=title,
                            snippet=snippet,
                            domain=domain,
                            rank=rank,
                        )
                    )
                    rank += 1
                    if len(results) >= self._candidate_cap():
                        return results
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
                    if len(results) >= self._candidate_cap():
                        return results
        return results

    def _rerank_results(self, results: list[SearchResultItem], queries: list[str], source_hint: str | None = None) -> list[SearchResultItem]:
        query_text = " ".join(queries)
        return sorted(
            results,
            key=lambda item: (self._result_score(item, query_text, crawl_mode=False, source_hint=source_hint), -item.rank),
            reverse=True,
        )

    def _filter_relevant_results(self, results: list[SearchResultItem], queries: list[str]) -> list[SearchResultItem]:
        query_text = " ".join(queries)
        return [item for item in results if self._looks_relevant(item, query_text)]

    def _needs_direct_data_rescue(self, results: list[SearchResultItem], query_text: str) -> bool:
        if not self._looks_price_or_data_request(query_text.lower()):
            return False
        if not results:
            return True
        non_rss_count = sum(1 for item in results if not self._is_google_news_result(item))
        return non_rss_count < max(2, self.settings.search_result_limit // 3)

    def _direct_data_rescue_queries(self, keyword: str) -> list[str]:
        seed = self._entity_search_seed(keyword)
        queries = [
            f"{seed} 价格表 行情",
            f"{seed} 历史价格 走势图",
            f"site:mysteel.com {seed} 价格",
            f"site:eastmoney.com {seed} 价格",
        ]
        if "锂" in seed or "电池" in seed:
            queries.extend(
                [
                    "碳酸锂 价格 月度 数据",
                    "磷酸铁锂 电芯 价格 月度 数据",
                ]
            )
        seen: set[str] = set()
        deduped: list[str] = []
        for query in queries:
            normalized = query.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)
        return deduped[:6]

    def _result_score(self, item: SearchResultItem, query_text: str, *, crawl_mode: bool, source_hint: str | None = None) -> int:
        text = " ".join(
            part
            for part in [item.title, item.snippet or "", item.url, item.domain or ""]
            if part
        ).lower()
        domain = (item.domain or "").lower()
        score = max(1, 120 - item.rank)

        for suffix, boost in self._DIRECT_SOURCE_DOMAIN_BOOSTS.items():
            if self._domain_matches(domain, suffix):
                score += boost
        for suffix, boost in self._DEPRIORITIZED_DOMAIN_BOOSTS.items():
            if self._domain_matches(domain, suffix):
                score += boost

        if self._contains_any(text, self._DIRECT_DATA_MARKERS):
            score += 10
        if self._contains_any(text, self._STRUCTURED_PAGE_MARKERS):
            score += 8
        if self._contains_any(text, self._NEWS_MARKERS):
            score -= 9 if crawl_mode else 6

        lowered_query = query_text.lower()
        if self._looks_price_or_data_request(lowered_query):
            if self._contains_any(text, ("价格", "行情", "走势", "图谱", "price", "prices", "quote", "historical", "trend")):
                score += 10
            if self._contains_any(text, ("元/吨", "万元", "元/wh", "%", "吨", "price chart", "历史价格", "月报", "周报")):
                score += 6

        focus_terms = self._query_focus_terms(query_text)
        matched_terms = sum(1 for term in focus_terms if term in text)
        score += min(matched_terms, 6) * 2

        if source_hint:
            hinted_domains = self._preferred_domains_from_hint(source_hint)
            for hinted_domain in hinted_domains:
                if self._domain_matches(domain, hinted_domain):
                    score += 26
            if any(marker in source_hint for marker in ("不要新闻", "不看新闻", "不要媒体")) and self._contains_any(text, self._NEWS_MARKERS):
                score -= 16

        if crawl_mode and domain.endswith("baijiahao.baidu.com"):
            score -= 10
        return score

    def _looks_relevant(self, item: SearchResultItem, query_text: str) -> bool:
        text = " ".join(part for part in [item.title, item.snippet or "", item.url, item.domain or ""] if part).lower()
        if self._contains_any(text, self._DIRECT_DATA_MARKERS):
            return True

        focus_terms = self._query_focus_terms(query_text)
        matched_terms = sum(1 for term in focus_terms if term in text)
        if matched_terms >= 2:
            return True
        if matched_terms >= 1 and self._looks_price_or_data_request(query_text.lower()):
            return True
        return False

    @staticmethod
    def _domain_matches(domain: str, suffix: str) -> bool:
        return domain == suffix or domain.endswith(f".{suffix}")

    @staticmethod
    def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
        return any(marker in text for marker in markers)

    @staticmethod
    def _looks_price_or_data_request(query_text: str) -> bool:
        return any(marker in query_text for marker in ("价", "价格", "走势", "趋势", "数据", "price", "trend", "data"))

    @staticmethod
    def _query_focus_terms(query_text: str) -> list[str]:
        terms: list[str] = []
        seen: set[str] = set()
        for part in re.split(r"[\s,，;；、/:：()（）\[\]【】|_-]+", query_text.lower()):
            normalized = part.strip()
            if len(normalized) < 2 or normalized in seen:
                continue
            seen.add(normalized)
            terms.append(normalized)
        return terms[:12]

    @staticmethod
    def _entity_search_seed(keyword: str) -> str:
        seed = keyword
        for marker in ["国内价格", "国际价格", "价格走势", "历史价格", "实时价格", "价格", "行情", "走势", "趋势", "数据", "分析", "报告", "国内", "中国", "新闻"]:
            seed = seed.replace(marker, " ")
        normalized = re.sub(r"\s+", " ", seed).strip()
        return normalized or keyword.strip()

    @staticmethod
    def _is_google_news_result(item: SearchResultItem) -> bool:
        return "news.google.com/rss/articles/" in item.url

    @staticmethod
    def _preferred_domains_from_hint(source_hint: str) -> list[str]:
        domains = re.findall(r"(?:https?://)?([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", source_hint)
        aliases = {
            "mysteel": "mysteel.com",
            "钢联": "mysteel.com",
            "东方财富": "eastmoney.com",
            "eastmoney": "eastmoney.com",
            "工信部": "miit.gov.cn",
            "miit": "miit.gov.cn",
            "上海有色": "smm.cn",
            "smm": "smm.cn",
            "前瞻": "qianzhan.com",
            "qianzhan": "qianzhan.com",
        }
        lowered = source_hint.lower()
        for marker, domain in aliases.items():
            if marker.lower() in lowered:
                domains.append(domain)

        seen: set[str] = set()
        items: list[str] = []
        for domain in domains:
            normalized = domain.lower().strip(".")
            if normalized in seen:
                continue
            seen.add(normalized)
            items.append(normalized)
        return items[:8]

    def _candidate_cap(self) -> int:
        return max(self.settings.search_result_limit * 3, 16)

    @classmethod
    def _is_blocked_domain(cls, domain: str | None) -> bool:
        if not domain:
            return False
        normalized = domain.lower()
        return normalized in cls._BLOCKED_DOMAINS or normalized.endswith(".smartapps.baidu.com")

    @staticmethod
    def _extract_baidu_snippet(node: BeautifulSoup, title: str) -> str | None:
        preferred_selectors = [
            ".c-span-last",
            ".content-right_8Zs40",
            ".c-color-text",
            ".c-font-normal",
            ".c-gap-top-small",
            ".c-abstract",
            ".content-bottom_3B8i6",
        ]
        for selector in preferred_selectors:
            text = node.select_one(selector)
            if text:
                snippet = text.get_text(" ", strip=True)
                if snippet and snippet != title:
                    return snippet

        snippet = node.get_text(" ", strip=True)
        if snippet == title:
            return None
        return snippet[:500] if snippet else None

    @staticmethod
    def _resolve_baidu_result_url(client: httpx.Client, href: str) -> str | None:
        try:
            response = client.get(href, follow_redirects=False)
        except httpx.HTTPError:
            return None

        location = response.headers.get("location")
        if location and location.startswith("http"):
            return location
        if str(response.url).startswith("http"):
            return str(response.url)
        return None

    @staticmethod
    def _resolve_bing_result_url(client: httpx.Client, href: str) -> str | None:
        parsed = urlparse(href)
        if "bing.com" not in parsed.netloc:
            return href

        encoded = parse_qs(parsed.query).get("u")
        if encoded:
            candidate = SearchService._decode_bing_tracking_url(encoded[0])
            if candidate:
                return candidate

        try:
            response = client.get(href, follow_redirects=False)
        except httpx.HTTPError:
            return None

        location = response.headers.get("location")
        if location and location.startswith("http"):
            return location
        if str(response.url).startswith("http") and "bing.com" not in urlparse(str(response.url)).netloc:
            return str(response.url)
        return None

    @staticmethod
    def _resolve_sogou_result_url(client: httpx.Client, href: str) -> str | None:
        target = href
        if href.startswith("/"):
            target = f"https://www.sogou.com{href}"
        if target.startswith("http") and "sogou.com/link?" not in target:
            return target

        try:
            response = client.get(target, follow_redirects=True)
        except httpx.HTTPError:
            return None

        final_url = str(response.url)
        if final_url.startswith("http") and "sogou.com/link?" not in final_url:
            return final_url

        script_match = re.search(r'window\.location\.replace\("([^"]+)"\)', response.text)
        if script_match:
            return script_match.group(1)

        meta_match = re.search(r"URL='([^']+)'", response.text, flags=re.IGNORECASE)
        if meta_match:
            return meta_match.group(1)
        return None

    @staticmethod
    def _decode_bing_tracking_url(value: str) -> str | None:
        raw = unquote(value)
        if raw.startswith("a1"):
            raw = raw[2:]
        padding = "=" * (-len(raw) % 4)
        try:
            decoded = base64.b64decode(raw + padding).decode("utf-8", errors="ignore")
        except (ValueError, OSError):
            return None
        if decoded.startswith("http"):
            return decoded
        return None

    @staticmethod
    def _extract_sogou_snippet(node: BeautifulSoup, title: str) -> str | None:
        for selector in [".text-layout", ".str-text-info", ".star-wiki", ".extra-info", "p"]:
            text = node.select_one(selector)
            if text:
                snippet = text.get_text(" ", strip=True)
                if snippet and snippet != title:
                    return snippet[:500]

        snippet = node.get_text(" ", strip=True)
        if snippet == title:
            return None
        return snippet[:500] if snippet else None

    @staticmethod
    def _looks_english(value: str) -> bool:
        letters = [char for char in value if char.isalpha()]
        if not letters:
            return False
        ascii_letters = sum(1 for char in letters if char.isascii())
        return ascii_letters / len(letters) >= 0.8
