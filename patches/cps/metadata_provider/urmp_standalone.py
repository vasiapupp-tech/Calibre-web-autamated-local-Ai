"""URMP - Universal Russian Metadata Provider (Standalone for Calibre-Web)."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import quote

import requests
from lxml import etree
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from cps.services.Metadata import MetaRecord, MetaSourceInfo, Metadata

    HAS_CPS = True
except ImportError:
    HAS_CPS = False

log = logging.getLogger(__name__)


# ==================== Models ====================


@dataclass
class BookMetadata:
    """Provider-agnostic book record."""

    title: str
    authors: list[str] = field(default_factory=list)
    source_id: str = ""
    source_url: str = ""
    cover_url: str = ""
    description: str = ""
    publisher: str = ""
    series: str = ""
    series_index: float | None = None
    isbn: str = ""
    language: str = ""
    pubdate: str = ""
    rating: float = 0.0
    tags: list[str] = field(default_factory=list)
    identifiers: dict[str, str] = field(default_factory=dict)
    relevance: float = 0.0

    @property
    def display_title(self) -> str:
        return self.title.strip() or "Unknown Title"

    @property
    def display_authors(self) -> list[str]:
        return self.authors or ["Unknown"]


@dataclass
class SearchQuery:
    """Search query for book metadata."""

    title: str = ""
    authors: list[str] = field(default_factory=list)
    isbn: str = ""
    max_results: int = 10

    @property
    def keyword(self) -> str:
        parts = [self.title.strip()]
        parts.extend(a.strip() for a in self.authors if a.strip())
        return " ".join(parts)


# ==================== Client ====================


class HttpClient:
    """Shared HTTP client with retry, backoff, and rate-limiting."""

    def __init__(
        self,
        timeout: int = 15,
        max_retries: int = 3,
        backoff_factor: float = 1.0,
        user_agent: str | None = None,
    ):
        self._session = requests.Session()
        self._timeout = timeout

        retry = Retry(
            total=max_retries,
            backoff_factor=backoff_factor,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD"],
        )
        adapter = HTTPAdapter(
            max_retries=retry, pool_connections=10, pool_maxsize=10
        )
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

        self._session.headers.update(
            {
                "User-Agent": user_agent or "URMP/1.0",
                "Accept-Encoding": "gzip, deflate",
                "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8",
            }
        )

    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> requests.Response:
        resp = self._session.get(
            url,
            params=params,
            headers=headers,
            timeout=timeout or self._timeout,
        )
        resp.raise_for_status()
        return resp

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self._timeout)
        resp = self._session.post(url, **kwargs)
        resp.raise_for_status()
        return resp

    @property
    def session(self) -> requests.Session:
        return self._session

    def close(self) -> None:
        self._session.close()


class RateLimiter:
    """Per-provider rate limiter: ensures minimum interval between calls."""

    def __init__(self, min_interval: float = 1.0):
        self._min_interval = min_interval
        self._last_call: float = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last_call = time.monotonic()


# ==================== Cache ====================


class CacheStore:
    """Thread-safe SQLite cache with TTL expiration."""

    def __init__(self, db_path: str = "urmp_cache.db", ttl: int = 30 * 24 * 3600):
        self._db_path = db_path
        self._ttl = ttl
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(self._db_path, timeout=5)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA busy_timeout=3000")
        return self._local.conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_cache_created ON cache(created_at);
            """
        )
        conn.commit()

    def get(self, key: str) -> str | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT value, created_at FROM cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        value, created_at = row
        if time.time() - created_at > self._ttl:
            conn.execute("DELETE FROM cache WHERE key = ?", (key,))
            conn.commit()
            return None
        return value

    def set(self, key: str, value: str) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO cache (key, value, created_at) VALUES (?, ?, ?)",
            (key, value, time.time()),
        )
        conn.commit()

    def cleanup(self) -> int:
        """Remove expired entries. Returns count removed."""
        conn = self._get_conn()
        cutoff = time.time() - self._ttl
        cursor = conn.execute("DELETE FROM cache WHERE created_at < ?", (cutoff,))
        conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        if hasattr(self._local, "conn"):
            self._local.conn.close()


# ==================== Scoring ====================


def _normalize(s: str) -> str:
    return " ".join(s.lower().strip().split())


def calculate_relevance(
    book_title: str,
    book_authors: list[str],
    query_title: str,
    query_authors: list[str],
    title_weight: float = 0.7,
    author_weight: float = 0.3,
) -> float:
    title_sim = SequenceMatcher(
        None, _normalize(book_title), _normalize(query_title)
    ).ratio()
    author_sim = 0.0
    if query_authors and book_authors:
        author_sim = SequenceMatcher(
            None,
            _normalize(" ".join(book_authors)),
            _normalize(" ".join(query_authors)),
        ).ratio()
    return title_weight * title_sim + author_weight * author_sim


# ==================== Providers ====================


class BaseProvider(ABC):
    """Abstract base for all book metadata providers."""

    name: str = ""
    provider_id: str = ""
    base_url: str = ""

    def __init__(self, http: HttpClient, cache: CacheStore | None = None):
        self._http = http
        self._cache = cache

    @abstractmethod
    def search(self, query: SearchQuery) -> list[BookMetadata]:
        """Search for books matching query. Return list of results."""
        ...

    @abstractmethod
    def get_book_detail(self, book_id: str) -> BookMetadata | None:
        """Fetch full metadata for a single book by provider-specific ID."""
        ...

    def close(self) -> None:
        """Cleanup resources."""
        pass


_registry: dict[str, type[BaseProvider]] = {}


def register_provider(cls: type[BaseProvider]) -> type[BaseProvider]:
    """Decorator to register a provider class."""
    _registry[cls.provider_id] = cls
    return cls


def create_all_providers(
    http: HttpClient, cache: CacheStore | None = None
) -> list[BaseProvider]:
    return [cls(http=http, cache=cache) for cls in _registry.values()]


# ==================== Labirint Provider ====================


@register_provider
class LabirintProvider(BaseProvider):
    """Labirint.ru metadata provider."""

    name = "Labirint Books"
    provider_id = "labirint"
    base_url = "https://www.labirint.ru"

    SEARCH_URL = "https://www.labirint.ru/search/{query}/?stype=0"
    BOOK_URL = "https://www.labirint.ru/books/{id}/"

    def __init__(self, http: HttpClient, cache: CacheStore | None = None):
        super().__init__(http, cache)
        self._rate_limiter = RateLimiter(min_interval=1.5)

    def search(self, query: SearchQuery) -> list[BookMetadata]:
        self._rate_limiter.wait()
        url = self.SEARCH_URL.format(query=quote(query.keyword))
        resp = self._http.get(url, headers={"Referer": self.base_url})
        return self._parse_search(resp.text, query)

    def get_book_detail(self, book_id: str) -> BookMetadata | None:
        cache_key = f"labirint:detail:{book_id}"
        if self._cache:
            cached = self._cache.get(cache_key)
            if cached:
                data = json.loads(cached)
                return self._from_dict(data)

        self._rate_limiter.wait()
        url = self.BOOK_URL.format(id=book_id)
        resp = self._http.get(url, headers={"Referer": self.base_url})
        book = self._parse_detail(resp.text, book_id)

        if book and self._cache:
            self._cache.set(cache_key, json.dumps(self._to_dict(book)))

        return book

    def _parse_search(self, html: str, query: SearchQuery) -> list[BookMetadata]:
        tree = etree.HTML(html)
        cards = tree.xpath(
            '//div[contains(@class, "product-card") and @data-product-id]'
        )[:20]
        results = []
        for card in cards:
            book = self._parse_card(card)
            if book:
                results.append(book)
        return results

    def _parse_card(self, card) -> BookMetadata | None:
        try:
            title_el = card.xpath('.//a[contains(@class, "product-card__name")]')
            title = title_el[0].text.strip() if title_el else ""
            book_id = card.get("data-product-id", "")
            href = card.xpath('.//a[contains(@class, "product-card__name")]/@href')
            url = self.base_url + href[0] if href else ""

            data_src = card.xpath(
                './/a[contains(@class, "product-card__img")]//img/@data-src'
            )
            src = card.xpath(
                './/a[contains(@class, "product-card__img")]//img/@src'
            )
            cover = (data_src[0] if data_src else src[0] if src else "").strip()
            if cover and "labirint.ru" in cover and "/363-0" in cover:
                cover = cover.replace("/363-0", "/800-0")

            author_els = card.xpath(
                './/div[contains(@class, "product-card__author")]//a/@title'
            )
            authors = [a.strip() for a in author_els if a.strip()]

            pub_el = card.xpath(
                './/div[contains(@class, "product-card__info")]'
                '//a[contains(@class, "product-card__info-item") '
                'and not(contains(@class, "product-card__info-series"))]'
            )
            publisher = pub_el[0].text.strip() if pub_el else ""

            series_el = card.xpath(
                './/a[contains(@class, "product-card__info-item") '
                'and contains(@class, "product-card__info-series")]'
            )
            series = series_el[0].text.strip() if series_el else ""

            rating_el = card.xpath(
                './/div[contains(@class, "product-card__rating-container")]//span'
            )
            rating = 0.0
            if rating_el and rating_el[0].text.strip().replace(".", "").isdigit():
                rating = min(float(rating_el[0].text.strip()) / 2, 5.0)

            return BookMetadata(
                title=title,
                authors=authors,
                source_id=self.provider_id,
                source_url=url,
                cover_url=cover,
                publisher=publisher,
                series=series,
                rating=rating,
                identifiers={self.provider_id: book_id},
            )
        except Exception as e:
            log.error("Error parsing labirint card: %s", e)
            return None

    def _parse_detail(self, html: str, book_id: str) -> BookMetadata | None:
        try:
            tree = etree.HTML(html)

            title_el = tree.xpath('.//h1[@itemprop="name"]//text() | .//h1//text()')
            title = title_el[0].strip() if title_el else ""

            author_els = tree.xpath(
                './/div[@id="characteristics"]'
                '//div[contains(.//div, "Автор")]//a/text()'
            )
            authors = [a.strip() for a in author_els if a.strip()]

            isbn_el = tree.xpath('.//meta[@itemprop="isbn"]/@content')
            isbn = ""
            if isbn_el:
                cleaned = re.sub(r"[^0-9X]", "", isbn_el[0])
                if len(cleaned) in (10, 13):
                    isbn = isbn_el[0].strip()

            desc_els = tree.xpath(
                './/div[@id="annotation"]//*[self::p or self::div]'
                '[not(contains(@class, "tab-content"))]'
                '//text()'
            )
            description = " ".join(d.strip() for d in desc_els if d.strip())
            description = re.sub(
                r"^\s*(Аннотация|Полистать|Содержание)\s*",
                "",
                description,
                flags=re.IGNORECASE,
            )

            lang_el = tree.xpath('.//*[contains(text(), "Язык:")]//text()')
            language = ""
            if lang_el:
                language = re.sub(
                    r"Язык:\s*", "", " ".join(lang_el), flags=re.IGNORECASE
                ).strip()

            pub_el = tree.xpath(
                './/div[@id="characteristics"]'
                '//div[contains(.//div, "Издательство")]//a/text()'
            )
            publisher = pub_el[0].strip() if pub_el else ""

            date_el = tree.xpath(
                './/div[@id="characteristics"]'
                '//div[contains(.//div, "Издательство")]'
                '//span[contains(text(), ",")]/following-sibling::span[1]/text()'
            )
            pubdate = ""
            if date_el and re.match(r"^\d{4}$", date_el[0]):
                pubdate = date_el[0].strip()

            genre_el = tree.xpath(
                './/div[@itemscope and @itemtype="http://schema.org/BreadcrumbList"]'
                '//span[@itemprop="name"]/text()'
            )
            tags = [genre_el[-1].strip()] if genre_el else []

            return BookMetadata(
                title=title,
                authors=authors,
                source_id=self.provider_id,
                source_url=self.BOOK_URL.format(id=book_id),
                description=description,
                publisher=publisher,
                isbn=isbn,
                language=language,
                pubdate=pubdate,
                tags=tags,
                identifiers={self.provider_id: book_id},
            )
        except Exception as e:
            log.error("Error parsing labirint detail: %s", e)
            return None

    def _to_dict(self, book: BookMetadata) -> dict:
        return {
            "title": book.title,
            "authors": book.authors,
            "source_id": book.source_id,
            "source_url": book.source_url,
            "cover_url": book.cover_url,
            "description": book.description,
            "publisher": book.publisher,
            "series": book.series,
            "series_index": book.series_index,
            "isbn": book.isbn,
            "language": book.language,
            "pubdate": book.pubdate,
            "rating": book.rating,
            "tags": book.tags,
            "identifiers": book.identifiers,
        }

    def _from_dict(self, data: dict) -> BookMetadata:
        return BookMetadata(
            title=data.get("title", ""),
            authors=data.get("authors", []),
            source_id=data.get("source_id", ""),
            source_url=data.get("source_url", ""),
            cover_url=data.get("cover_url", ""),
            description=data.get("description", ""),
            publisher=data.get("publisher", ""),
            series=data.get("series", ""),
            series_index=data.get("series_index"),
            isbn=data.get("isbn", ""),
            language=data.get("language", ""),
            pubdate=data.get("pubdate", ""),
            rating=data.get("rating", 0.0),
            tags=data.get("tags", []),
            identifiers=data.get("identifiers", {}),
        )


# ==================== LitRes Provider ====================


@register_provider
class LitresProvider(BaseProvider):
    """LitRes API provider."""

    name = "LitRes"
    provider_id = "litres"
    base_url = "https://www.litres.ru"

    API_URL = "https://api.litres.ru/foundation/api/search"
    API_ARTS_URL = "https://api.litres.ru/foundation/api/arts/{}"

    def __init__(self, http: HttpClient, cache: CacheStore | None = None):
        super().__init__(http, cache)
        self._rate_limiter = RateLimiter(min_interval=1.0)

    def search(self, query: SearchQuery) -> list[BookMetadata]:
        self._rate_limiter.wait()

        params = {
            "q": query.keyword.strip(),
            "limit": query.max_results,
            "show_unavailable": "true",
            "types": ["text_book", "audiobook", "podcast"],
        }

        headers = {
            "ui-language-code": "ru",
            "Accept": "application/json",
        }

        try:
            resp = self._http.get(self.API_URL, params=params, headers=headers)
            data = resp.json()
            items = self._extract_items(data)

            results = []
            for item in items:
                book = self._process_item(item)
                if book:
                    results.append(book)
                    if len(results) >= query.max_results:
                        break

            return results
        except Exception as e:
            log.error("LitRes search failed: %s", e)
            return []

    def get_book_detail(self, book_id: str) -> BookMetadata | None:
        cache_key = f"litres:detail:{book_id}"
        if self._cache:
            cached = self._cache.get(cache_key)
            if cached:
                data = json.loads(cached)
                return self._book_from_dict(data)

        self._rate_limiter.wait()

        try:
            resp = self._http.get(
                self.API_ARTS_URL.format(book_id),
                headers={"Accept": "application/json"},
            )
            data = resp.json()
            payload = data.get("payload", {}).get("data", {})

            if payload:
                book = self._process_item(payload)
                if book and self._cache:
                    self._cache.set(cache_key, json.dumps(self._to_dict(book)))
                return book
        except Exception as e:
            log.error("LitRes detail failed: %s", e)

        return None

    def _extract_items(self, data: dict) -> list[dict]:
        items = []
        payload_data = data.get("payload", {}).get("data")
        if isinstance(payload_data, list):
            for el in payload_data:
                inst = el.get("instance") if isinstance(el, dict) else None
                if inst and isinstance(inst, dict):
                    items.append(inst)
        return items

    def _process_item(self, item: dict) -> BookMetadata | None:
        try:
            item_id = item.get("id") or item.get("uuid")
            if not item_id:
                return None

            title = item.get("title") or item.get("name") or ""
            title = re.sub(
                r"\s*\([^)]*(?:pdf|epub)[^)]*\)", "", title, flags=re.IGNORECASE
            )

            if not title:
                return None

            authors = []
            for p in item.get("persons") or []:
                role = (p.get("role") or "").lower()
                name = p.get("full_name") or p.get("fullName") or p.get("name")
                if name and role in ("author", "автор", ""):
                    authors.append(name)

            cover = item.get("cover_url") or item.get("image") or ""
            isbn = item.get("isbn") or item.get("bookIsbn") or ""
            if isbn:
                isbn = str(isbn).replace("-", "")

            description = item.get("html_annotation") or item.get("annotation") or ""
            tags = []
            for tag in item.get("tags") or []:
                if isinstance(tag, dict) and tag.get("name"):
                    name = str(tag["name"]).strip()
                    if name and name not in tags:
                        tags.append(name)

            return BookMetadata(
                title=title,
                authors=authors,
                source_id=self.provider_id,
                source_url=f"{self.base_url}/book/{item_id}/",
                cover_url=cover,
                description=description,
                tags=tags,
                isbn=isbn,
                identifiers={self.provider_id: str(item_id)},
            )
        except Exception as e:
            log.error("Error processing LitRes item: %s", e)
            return None

    def _to_dict(self, book: BookMetadata) -> dict:
        return {
            "title": book.title,
            "authors": book.authors,
            "source_id": book.source_id,
            "source_url": book.source_url,
            "cover_url": book.cover_url,
            "description": book.description,
            "tags": book.tags,
            "isbn": book.isbn,
            "identifiers": book.identifiers,
        }

    def _book_from_dict(self, data: dict) -> BookMetadata:
        return BookMetadata(
            title=data.get("title", ""),
            authors=data.get("authors", []),
            source_id=data.get("source_id", ""),
            source_url=data.get("source_url", ""),
            cover_url=data.get("cover_url", ""),
            description=data.get("description", ""),
            tags=data.get("tags", []),
            isbn=data.get("isbn", ""),
            identifiers=data.get("identifiers", {}),
        )


# ==================== Bridge ====================


def bridge_to_cw(book: BookMetadata, generic_cover: str = "") -> MetaRecord | None:
    """Convert BookMetadata to cps.services.Metadata.MetaRecord."""
    if not book.title:
        return None

    meta = MetaRecord(
        id=book.identifiers.get(book.source_id, ""),
        title=book.display_title,
        authors=book.display_authors,
        url=book.source_url,
        source=MetaSourceInfo(
            id=book.source_id,
            description=book.title,
            link=book.source_url,
        ),
    )
    meta.cover = book.cover_url or generic_cover
    meta.description = book.description
    meta.publisher = book.publisher
    meta.publishedDate = book.pubdate
    meta.rating = int(book.rating) if book.rating else 0
    meta.series = book.series
    meta.series_index = book.series_index
    meta.identifiers = book.identifiers
    meta.tags = book.tags
    return meta


# ==================== Main Provider ====================


if HAS_CPS:

    class URMPProvider(Metadata):
        __name__ = "URMP"
        __id__ = "urmp"
        DESCRIPTION = "Universal Russian Metadata Provider"
        META_URL = "https://github.com/urmp/urmp"

        def __init__(self):
            super().__init__()
            self._http = HttpClient(timeout=15, user_agent="URMP-CW/1.0")
            self._cache = CacheStore(db_path="urmp_cache.db")
            self._providers = create_all_providers(self._http, self._cache)
            self._provider_map = {p.provider_id: p for p in self._providers}

        def config(self, settings: dict) -> None:
            self.active = settings.get(self.__id__, "True") == "True"

        @staticmethod
        def setup() -> None:
            log.info("URMP provider initialized")

        def name(self) -> str:
            return self.__name__

        def id(self) -> str:
            return self.__id__

        def search(
            self,
            query: str,
            generic_cover: str = "",
            locale: Any = "ru",
        ) -> list[Any] | None:
            if not self.active:
                return []

            search_query = SearchQuery(title=query)
            results: list[Any] = []

            for provider in self._providers:
                try:
                    books = provider.search(search_query)
                    for book in books:
                        book.relevance = calculate_relevance(
                            book.title,
                            book.authors,
                            search_query.title,
                            [],
                        )
                        mr = bridge_to_cw(book, generic_cover)
                        if mr:
                            results.append(mr)
                except Exception as e:
                    log.error("Provider %s failed: %s", provider.provider_id, e)

            return results[:7]

        def close(self) -> None:
            self._http.close()
            self._cache.close()
