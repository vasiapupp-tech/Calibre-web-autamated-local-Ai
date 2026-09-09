"""MyBook.ru metadata provider (subscription service)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

from cps import logger
from cps.services.Metadata import MetaRecord, Metadata

from cps.metadata_provider.base import BaseMetadataProvider

log = logger.create()


class MyBook(BaseMetadataProvider, Metadata):
    __name__ = "MyBook"
    __id__ = "mybook"

    DESCRIPTION = "MyBook"
    META_URL = "https://mybook.ru/"
    SEARCH_URL = "https://mybook.ru/api/book-search/"
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

        log.info("Searching MyBook for: %s", query)

        params = {"q": query, "limit": self.MAX_PARSE}
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

        log.info("MyBook search found %d results", len(results))
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
        has_title = bool(keys.intersection({"name", "title", "bookname"}))
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
                self._text_value(self._first_value(item, ("name", "title", "bookName")))
            )
            if not title:
                return None

            authors: List[str] = []
            author_value = self._first_value(item, ("authors", "author", "writers"))
            if isinstance(author_value, list):
                for a in author_value:
                    if isinstance(a, dict):
                        name = (
                            a.get("name")
                            or a.get("full_name")
                            or " ".join(filter(None, [
                                a.get("first_name"), a.get("last_name"),
                            ]))
                        )
                        if name:
                            authors.append(str(name))
                    elif isinstance(a, str):
                        authors.append(a)
            else:
                authors = [a for a in self._names_from_value(author_value) if a]

            pid = self._text_value(self._first_value(item, ("id", "bookId", "book_id")))
            url = self._absolute_url(self._first_value(item, ("url", "slug")))
            if not url and pid:
                url = f"{self.META_URL}book/{pid}"
            if not url:
                url = self.META_URL

            record = self._make_record(title, authors, url, pid)

            record.cover = (
                self._absolute_url(self._first_value(item, ("cover", "image", "coverUrl", "picture")))
                or generic_cover
            )
            record.description = self._strip_html(
                self._text_value(self._first_value(item, ("description", "annotation")))
            )
            record.publisher = self._text_value(self._first_value(item, ("publisher",)))
            record.languages = self._lang_name("ru", locale)
            record.tags = [
                self._text_value(t)
                for t in self._names_from_value(self._first_value(item, ("genres", "genre", "tags")))
            ][:30]

            return record
        except Exception:
            log.exception("Error processing MyBook item")
            return None
