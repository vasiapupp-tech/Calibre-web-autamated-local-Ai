"""Litmarket.ru metadata provider (self-publishing platform, HTML search)."""

from __future__ import annotations

from typing import Any, List, Optional
from urllib.parse import quote_plus, urljoin

from cps import logger
from cps.services.Metadata import MetaRecord, Metadata

from cps.metadata_provider.base import BaseMetadataProvider

log = logger.create()


class Litmarket(BaseMetadataProvider, Metadata):
    __name__ = "Литмаркет"
    __id__ = "litmarket"

    DESCRIPTION = "Литмаркет"
    META_URL = "https://litmarket.ru/"
    SEARCH_URL = "https://litmarket.ru/search"
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

        log.info("Searching Литмаркет for: %s", query)

        url = self.SEARCH_URL
        response = self._get(url, params={"text": query})
        if response is None:
            return []

        tree = self._parse_html(response.text)

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

        log.info("Литмаркет search found %d results", len(results))
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
        data = self._parse_book_page(tree, url)

        title = self._clean_title(data["title"])
        if not title:
            return None

        record = self._make_record(title, data["authors"], url)
        record.cover = data["cover"] or generic_cover
        record.description = data["description"]
        record.publisher = data["publisher"]
        record.publishedDate = data["date"]
        record.languages = self._lang_name(data["lang"] or "ru", locale)
        if data["isbn"]:
            record.identifiers["isbn"] = data["isbn"]

        return record
