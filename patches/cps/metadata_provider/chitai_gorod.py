"""Читай-город metadata provider (public storefront API)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

from cps import logger
from cps.services.Metadata import MetaRecord, Metadata

from cps.metadata_provider.base import BaseMetadataProvider

log = logger.create()


class ChitaiGorod(BaseMetadataProvider, Metadata):
    __name__ = "Читай-город"
    __id__ = "chitai_gorod"

    DESCRIPTION = "Читай-город"
    META_URL = "https://www.chitai-gorod.ru/"
    SEARCH_URL = "https://api.chitai-gorod.ru/api/v1/search/product"
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

        log.info("Searching Читай-город for: %s", query)

        params = {"phrase": query, "perPage": str(self.MAX_PARSE)}
        data = self._get_json(self.SEARCH_URL, params=params)
        if not data:
            return []

        items = self._find_book_items(data)
        results: List[MetaRecord] = []
        for item in items:
            record = self._process_item(item, generic_cover, locale)
            if record:
                results.append(record)
                if len(results) >= self.DEFAULT_LIMIT:
                    break

        log.info("Читай-город search found %d results", len(results))
        return results

    def _find_book_items(self, data: Any) -> List[Dict[str, Any]]:
        found: List[Dict[str, Any]] = []
        seen: set[int] = set()
        for obj in self._iter_dicts(data):
            oid = id(obj)
            if oid in seen:
                continue
            seen.add(oid)
            if self._looks_like_book(obj):
                found.append(obj)
            if len(found) >= self.MAX_PARSE:
                break
        return found

    @staticmethod
    def _looks_like_book(obj: Dict[str, Any]) -> bool:
        keys = {str(k).lower() for k in obj.keys()}
        has_name = bool(keys.intersection({"name", "title", "productname"}))
        has_id = bool(keys.intersection({"id", "productid", "slug", "url"}))
        return has_name and has_id

    def _process_item(
        self,
        item: Dict[str, Any],
        generic_cover: str,
        locale: Any,
    ) -> Optional[MetaRecord]:
        try:
            title = self._clean_title(
                self._text_value(self._first_value(item, ("name", "title", "productName")))
            )
            if not title:
                return None

            authors: List[str] = []
            author_value = self._first_value(item, ("authors", "author", "авторы"))
            for name in self._names_from_value(author_value):
                if name and name not in authors:
                    authors.append(name)

            url = self._absolute_url(
                self._first_value(item, ("url", "slug", "productUrl"))
            )
            if not url:
                pid = self._text_value(self._first_value(item, ("id", "productId")))
                url = f"{self.META_URL}product/{pid}" if pid else self.META_URL

            record = self._make_record(
                title,
                authors,
                url,
                pid=self._text_value(self._first_value(item, ("id", "productId"))),
            )

            record.cover = (
                self._absolute_url(self._first_value(item, ("image", "cover", "imageUrl", "picture")))
                or generic_cover
            )
            record.description = self._strip_html(
                self._text_value(self._first_value(item, ("description", "annotation", "annotationText")))
            )
            record.publisher = self._text_value(self._first_value(item, ("publisher", "издательство")))
            record.publishedDate = self._year_or_date(
                self._first_value(item, ("year", "publicationYear", "datePublished"))
            )
            record.languages = self._lang_name("ru", locale)

            isbn = self._extract_isbn(
                self._text_value(self._first_value(item, ("isbn", "isbn13", "isbn10")))
            )
            if isbn:
                record.identifiers["isbn"] = isbn

            record.tags = [
                self._text_value(t)
                for t in self._names_from_value(self._first_value(item, ("genres", "genre", "categories", "tags")))
            ][:30]

            return record
        except Exception:
            log.exception("Error processing Читай-город item")
            return None
