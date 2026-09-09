"""Google Books metadata provider (public volumes API, no key required)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from cps import logger
from cps.services.Metadata import MetaRecord, Metadata

from cps.metadata_provider.base import BaseMetadataProvider

log = logger.create()


class GoogleBooks(BaseMetadataProvider, Metadata):
    __name__ = "Google Books"
    __id__ = "google_books"

    DESCRIPTION = "Google Books"
    META_URL = "https://books.google.ru/"
    SEARCH_URL = "https://www.googleapis.com/books/v1/volumes"
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

        log.info("Searching Google Books for: %s", query)

        params = {
            "q": query,
            "country": "RU",
            "maxResults": str(self.MAX_PARSE),
        }
        data = self._get_json(self.SEARCH_URL, params=params)
        if not data:
            return []

        items = data.get("items") or []
        results: List[MetaRecord] = []
        for item in items:
            record = self._process_item(item, generic_cover, locale)
            if record:
                results.append(record)
                if len(results) >= self.DEFAULT_LIMIT:
                    break

        log.info("Google Books search found %d results", len(results))
        return results

    def _process_item(
        self,
        item: Dict[str, Any],
        generic_cover: str,
        locale: Any,
    ) -> Optional[MetaRecord]:
        try:
            volume_info = item.get("volumeInfo") or {}
            title = (volume_info.get("title") or "").strip()
            if not title:
                return None

            authors = [a for a in (volume_info.get("authors") or []) if a]
            url = (
                volume_info.get("canonicalVolumeLink")
                or volume_info.get("infoLink")
                or self.META_URL
            )

            record = self._make_record(title, authors, url, item.get("id") or "")

            # Cover (Google returns small thumbnails; prefer the largest available).
            images = volume_info.get("imageLinks") or {}
            cover = (
                images.get("extraLarge")
                or images.get("large")
                or images.get("medium")
                or images.get("small")
                or images.get("thumbnail")
                or ""
            )
            if cover:
                cover = cover.replace("http://", "https://")
            record.cover = cover or generic_cover

            record.description = self._strip_html(volume_info.get("description") or "")
            record.publisher = volume_info.get("publisher") or ""
            record.publishedDate = volume_info.get("publishedDate") or ""
            record.languages = self._lang_name(volume_info.get("language") or "ru", locale)
            record.tags = list(volume_info.get("categories") or [])

            try:
                rating = float(volume_info.get("averageRating") or 0)
                record.rating = max(0, min(5, int(round(rating))))
            except (TypeError, ValueError):
                record.rating = 0

            identifiers: Dict[str, str] = {}
            for ident in volume_info.get("industryIdentifiers") or []:
                if not isinstance(ident, dict):
                    continue
                ident_type = ident.get("type") or ""
                value = ident.get("identifier") or ""
                if ident_type in ("ISBN_13", "ISBN_10") and value:
                    identifiers["isbn"] = value
                    break
            if identifiers:
                record.identifiers = identifiers

            return record
        except Exception:
            log.exception("Error processing Google Books item")
            return None
