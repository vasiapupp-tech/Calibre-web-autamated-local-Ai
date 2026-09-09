"""LibCat.ru metadata provider (pirate library, HTML search)."""

from __future__ import annotations

from typing import Any, List, Optional
from urllib.parse import urljoin

from cps import logger
from cps.services.Metadata import MetaRecord, Metadata

from cps.metadata_provider.base import BaseMetadataProvider

log = logger.create()


class LibCat(BaseMetadataProvider, Metadata):
    __name__ = "LibCat"
    __id__ = "libcat"

    DESCRIPTION = "LibCat"
    META_URL = "https://libcat.ru/"
    SEARCH_URL = "https://libcat.ru/index.php"
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

        log.info("Searching LibCat for: %s", query)

        response = self._get(
            self.SEARCH_URL,
            params={"do": "search", "subaction": "search", "story": query},
        )
        if response is None:
            return []

        tree = self._parse_html(response.text)

        book_urls: List[str] = []
        seen: set[str] = set()
        for a in tree.xpath('//a[contains(@href, "/book/") or contains(@href, "/knigi/")]/@href'):
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

        log.info("LibCat search found %d results", len(results))
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

        authors = data["authors"]
        if not authors:
            for a in tree.xpath('//a[contains(@href, "/author/")]//text()'):
                name = str(a).strip()
                if name and len(name) < 80:
                    authors = [name]
                    break

        record = self._make_record(title, authors, url)
        record.cover = data["cover"] or generic_cover
        record.description = data["description"]
        record.publisher = data["publisher"]
        record.publishedDate = data["date"]
        record.languages = self._lang_name(data["lang"] or "ru", locale)
        if data["isbn"]:
            record.identifiers["isbn"] = data["isbn"]

        return record
