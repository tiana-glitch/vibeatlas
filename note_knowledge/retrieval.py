"""Public web retrieval for keyword-driven note creation.

The default provider only sends the keywords to public search endpoints. It
keeps source URLs in the result and never treats a network response as an
uncited fact. A provider can be injected for enterprise search or tests.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from html import unescape
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, unquote
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from .models import ResearchResult, SearchSource


class SearchProvider(Protocol):
    def search(self, query: str, limit: int = 5) -> Sequence[Mapping[str, Any]]: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_keywords(value: Any) -> List[str]:
    """Normalize a comma/newline separated string or a JSON list of keywords."""

    if isinstance(value, str):
        parts = re.split(r"[,，、;；|\n\t]+", value)
    elif isinstance(value, (list, tuple, set)):
        parts = [str(item) for item in value]
    elif value is None:
        parts = []
    else:
        raise ValueError("keywords must be a string or list of strings")
    cleaned: List[str] = []
    for part in parts:
        item = re.sub(r"\s+", " ", str(part)).strip()
        if item and item not in cleaned:
            cleaned.append(item[:80])
    return cleaned[:8]


def _strip_html(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", unescape(value)).strip()


def _valid_url(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("https://", "http://"))


class PublicSearchProvider:
    """Best-effort public search using Bing RSS with free public fallbacks."""

    def __init__(self, timeout: float = 8.0, user_agent: str = "NoteFlow/1.0") -> None:
        self.timeout = timeout
        self.user_agent = user_agent

    def _get_json(self, url: str) -> Mapping[str, Any]:
        request = Request(url, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
        with urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            return {}
        return payload

    def _get_xml(self, url: str) -> ElementTree.Element:
        request = Request(url, headers={"User-Agent": self.user_agent, "Accept": "application/rss+xml, application/xml"})
        with urlopen(request, timeout=self.timeout) as response:
            return ElementTree.fromstring(response.read())

    def _bing_rss(self, query: str, limit: int) -> List[Mapping[str, Any]]:
        """Read Bing's public RSS result format with a standard XML parser."""
        root = self._get_xml("https://www.bing.com/search?format=rss&count=%d&q=%s" % (limit, quote_plus(query)))
        found: List[Mapping[str, Any]] = []
        for item in root.findall("./channel/item"):
            title = _strip_html(item.findtext("title", ""))
            url = unescape(str(item.findtext("link", "") or "").strip())
            snippet = _strip_html(item.findtext("description", ""))
            if title and _valid_url(url) and snippet:
                found.append({"title": title, "url": url, "snippet": snippet, "source": "Bing RSS"})
            if len(found) >= limit:
                break
        return found

    def _duckduckgo(self, query: str, limit: int) -> List[Mapping[str, Any]]:
        payload = self._get_json("https://api.duckduckgo.com/?q=%s&format=json&no_html=1&skip_disambig=1" % quote_plus(query))
        found: List[Mapping[str, Any]] = []
        abstract = payload.get("AbstractText")
        abstract_url = payload.get("AbstractURL")
        heading = payload.get("Heading") or query
        if abstract and _valid_url(abstract_url):
            found.append({"title": heading, "url": abstract_url, "snippet": abstract, "source": "DuckDuckGo"})

        def walk(topics: Iterable[Any]) -> None:
            for item in topics:
                if len(found) >= limit:
                    return
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get("Topics"), list):
                    walk(item["Topics"])
                    continue
                text = item.get("Text")
                first_url = item.get("FirstURL")
                if text and _valid_url(first_url):
                    found.append({"title": str(text).split(" - ", 1)[0][:140], "url": first_url, "snippet": text, "source": "DuckDuckGo"})

        walk(payload.get("RelatedTopics", []))
        return found[:limit]

    def _wikipedia(self, query: str, limit: int) -> List[Mapping[str, Any]]:
        found: List[Mapping[str, Any]] = []
        for language in ("zh", "en"):
            url = "https://%s.wikipedia.org/w/rest.php/v1/search/page?q=%s&limit=%d" % (language, quote_plus(query), limit)
            payload = self._get_json(url)
            for item in payload.get("pages", []) if isinstance(payload.get("pages"), list) else []:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title", "")).strip()
                key = item.get("key") or title.replace(" ", "_")
                if not title or not key:
                    continue
                snippet = _strip_html(str(item.get("excerpt", "")))
                found.append({"title": title, "url": "https://%s.wikipedia.org/wiki/%s" % (language, quote_plus(str(key))), "snippet": snippet or ("Wikipedia 条目：" + title), "source": "Wikipedia"})
            if found:
                break
        return found[:limit]

    def search(self, query: str, limit: int = 5) -> Sequence[Mapping[str, Any]]:
        errors: List[str] = []
        for method in (self._bing_rss, self._duckduckgo, self._wikipedia):
            try:
                result = method(query, limit)
                if result:
                    return result
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, ElementTree.ParseError) as exc:
                errors.append(str(exc))
        return []


class ResearchAgent:
    """Search keywords, normalize sources, and create a cited text context."""

    def __init__(self, provider: Optional[SearchProvider] = None, limit: int = 5) -> None:
        self.provider = provider or PublicSearchProvider()
        self.limit = max(1, min(int(limit), 8))

    def run(self, keywords: Any, user_note: str = "", seed_text: str = "") -> ResearchResult:
        normalized = normalize_keywords(keywords)
        if not normalized:
            raise ValueError("请至少输入一个检索关键词")
        query = " ".join(normalized)
        retrieved_at = _now()
        try:
            candidates = self.provider.search(query, limit=self.limit)
        except Exception as exc:
            candidates = []
            warning = "公开检索失败：%s" % exc
        else:
            warning = "无"
        sources: List[SearchSource] = []
        seen_urls = set()
        for candidate in candidates or []:
            if not isinstance(candidate, Mapping):
                continue
            url = str(candidate.get("url", candidate.get("URL", ""))).strip()
            title = re.sub(r"\s+", " ", str(candidate.get("title", candidate.get("标题", ""))).strip())
            snippet = re.sub(r"\s+", " ", str(candidate.get("snippet", candidate.get("摘要", ""))).strip())
            if not _valid_url(url) or not title or not snippet or url in seen_urls:
                continue
            seen_urls.add(url)
            sources.append(SearchSource(title=title[:160], url=url, snippet=snippet[:500], source=str(candidate.get("source", "web")), retrieved_at=retrieved_at))
            if len(sources) >= self.limit:
                break
        lines = ["检索主题：%s" % query]
        if user_note.strip():
            lines.append("用户关注：%s" % user_note.strip())
        if sources:
            lines.append("以下内容来自公开网页，仅作为整理线索；关键事实需打开来源核实：")
            for index, source in enumerate(sources, 1):
                lines.extend(["[%d] %s" % (index, source.title), source.snippet, "来源：%s" % source.url])
        else:
            lines.append("未抓取到可用的公开资料，请检查网络或改用更具体的关键词。")
            if warning == "无":
                warning = "没有可引用的公开来源"
        if seed_text.strip():
            lines.extend(["用户补充内容（未作为公开来源）：", seed_text.strip()])
        return ResearchResult(keywords=normalized, query=query, sources=sources, research_text="\n".join(lines), status="completed" if sources else "empty", warning=warning)


__all__ = ["PublicSearchProvider", "ResearchAgent", "SearchProvider", "normalize_keywords"]
