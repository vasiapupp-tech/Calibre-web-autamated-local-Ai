"""Litnet.com metadata provider (self-publishing platform, public API)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

from cps import logger
from cps.services.Metadata import MetaRecord, Metadata

from cps.metadata_provider.base import BaseMetadataProvider

log = logger.create()


class Litnet(BaseMetadataProvider, Metadata):
    __name__ = "Litnet"
    __id__ = "litnet"

    DESCRIPTION = "Litnet"
    META_URL = "https://litnet.com/"
    SEARCH_URL = "https://litnet.com/api/books/search"
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

        log.info("Searching Litnet for: %s", query)

        params = {"q": query, "page": 1, "perPage": self.MAX_PARSE}
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

        log.info("Litnet search found %d results", len(results))
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
        has_title = bool(keys.intersection({"title", "name"}))
        has_id = bool(keys.intersection({"id", "bookid", "book_id"}))
        return has_title and has_id

    def _process_item(
        self,
        item: Dict[str, Any],
        generic_cover: str,
        locale: Any,
    ) -> Optional[MetaRecord]:
        try:
            title = self._clean_title(
                self._text_value(self._first_value(item, ("title", "name")))
            )
            if not title:
                return None

            authors: List[str] = []
            author_value = self._first_value(item, ("author", "authors"))
            if isinstance(author_value, dict):
                author_name = (
                    author_value.get("name")
                    or author_value.get("full_name")
                    or author_value.get("fullName")
                    or " ".join(
                        filter(None, [
                            author_value.get("first_name") or author_value.get("firstName"),
                            author_value.get("last_name") or author_value.get("lastName"),
                        ])
                    )
                )
                if author_name:
                    authors.append(str(author_name))
            else:
                authors = [
                    a for a in self._names_from_value(author_value) if a
                ]

            pid = self._text_value(self._first_value(item, ("id", "bookId", "book_id")))
            url = self._absolute_url(
                self._first_value(item, ("url", "slug"))
            )
            if not url and pid:
                url = f"{self.META_URL}ru/book/b{pid}"
            if not url:
                url = self.META_URL

            record = self._make_record(title, authors, url, pid)

            record.cover = (
                self._absolute_url(self._first_value(item, ("cover", "image", "coverUrl", "cover_url")))
                or generic_cover
            )
            record.description = self._strip_html(
                self._text_value(self._first_value(item, ("annotation", "description")))
            )
            record.languages = self._lang_name("ru", locale)
            record.tags = [
                self._text_value(t)
                for t in self._names_from_value(self._first_value(item, ("genres", "genre", "tags")))
            ][:30]

            return record
        except Exception:
            log.exception("Error processing Litnet item")
            return None
