"""LiveLib.ru metadata provider (book community)."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urljoin

from cps import logger
from cps.services.Metadata import MetaRecord, Metadata

from cps.metadata_provider.base import BaseMetadataProvider

log = logger.create()


class LiveLib(BaseMetadataProvider, Metadata):
    __name__ = "LiveLib"
    __id__ = "livelib"

    DESCRIPTION = "LiveLib"
    META_URL = "https://www.livelib.ru"
    SEARCH_URL = "https://www.livelib.ru/find/{query}"
    DEFAULT_LIMIT = 7

    def search(
        self,
        query: str,
        generic_cover: str = "",
        locale: Any = "ru",
    ) -> Optional[List[MetaRecord]]:
        if not self.active:
            return []

        query = (query or "").strip()
        if not query:
            return []

        log.info("Searching LiveLib for: %s", query)

        url = self.SEARCH_URL.format(query=quote(query, safe=""))
        response = self._get(url)
        if response is None:
            return []

        tree = self._parse_html(response.text)

        # Collect book links, preferring the most specific /book/<id> pages.
        book_urls: List[str] = []
        seen: set[str] = set()
        for a in tree.xpath('//a[contains(@href, "/book/")]/@href'):
            absolute = urljoin(self.META_URL, a)
            key = self._canonical_url(absolute)
            if key and key not in seen:
                seen.add(key)
                book_urls.append(absolute)
            if len(book_urls) >= self.MAX_PARSE:
                break

        results: List[MetaRecord] = []
        for book_url in book_urls:
            record = self._fetch_book(book_url, generic_cover, locale)
            if record:
                results.append(record)
                if len(results) >= self.DEFAULT_LIMIT:
                    break

        log.info("LiveLib search found %d results", len(results))
        return results

    def _fetch_book(
        self,
        url: str,
        generic_cover: str,
        locale: Any,
    ) -> Optional[MetaRecord]:
        response = self._get(url)
        if response is None:
            return None

        tree = self._parse_html(response.text)

        data: Dict[str, Any] = {}

        # JSON-LD (schema.org Book) is the richest source when present.
        for script in tree.xpath('//script[@type="application/ld+json"]/text()'):
            try:
                payload = json.loads(script)
            except Exception:
                continue
            for obj in self._iter_dicts(payload):
                if isinstance(obj, dict) and (
                    "isbn" in obj or "author" in obj or obj.get("@type") == "Book"
                ):
                    data.update(obj)
                    break

        def meta(*names: str) -> str:
            for name in names:
                values = tree.xpath(
                    '//meta[@property=$n or @name=$n]/@content', n=name
                )
                if values and values[0].strip():
                    return values[0].strip()
            return ""

        title = (
            self._text_value(data.get("name"))
            or meta("og:title")
            or self._text_first(tree.xpath("//h1//text()"))
        )
        title = self._clean_title(title)
        if not title:
            return None

        authors = self._authors_from(data)
        if not authors:
            authors = self._authors_from_text(meta("author"))

        cover = (
            self._absolute_url(data.get("image"))
            or meta("og:image")
            or generic_cover
        )
        description = self._strip_html(
            data.get("description") or meta("og:description")
        )
        publisher = self._text_value(data.get("publisher"))
        isbn = self._extract_isbn(
            self._text_value(data.get("isbn"))
        )

        record = self._make_record(
            title,
            authors,
            url,
            pid=self._extract_book_id(url),
        )
        record.cover = cover
        record.description = description
        record.publisher = publisher or ""
        record.publishedDate = self._year_or_date(data.get("datePublished"))
        record.languages = self._lang_name(data.get("inLanguage") or "ru", locale)
        if isbn:
            record.identifiers["isbn"] = isbn
        record.tags = [self._text_value(t) for t in self._names_from_value(data.get("genre"))]

        return record

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _iter_dicts(obj: Any):
        if isinstance(obj, dict):
            yield obj
            for value in obj.values():
                yield from LiveLib._iter_dicts(value)
        elif isinstance(obj, list):
            for value in obj:
                yield from LiveLib._iter_dicts(value)

    @staticmethod
    def _authors_from(data: Dict[str, Any]) -> List[str]:
        authors: List[str] = []
        value = data.get("author")
        if isinstance(value, dict):
            name = value.get("name")
            if name:
                authors.append(str(name))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and item.get("name"):
                    authors.append(str(item["name"]))
                elif isinstance(item, str):
                    authors.append(item)
        elif isinstance(value, str):
            authors.append(value)
        return [a for a in authors if a]

    @staticmethod
    def _authors_from_text(value: str) -> List[str]:
        if not value:
            return []
        parts = [p.strip() for p in value.replace(";", ",").split(",")]
        return [p for p in parts if p]

    @staticmethod
    def _extract_book_id(url: str) -> str:
        import re

        match = re.search(r"/book/(\d+)", url)
        return match.group(1) if match else ""
