"""Base metadata provider for CWA custom metadata sources.

Provides the shared plumbing used by the site-specific providers in this folder:

- requests.Session with retry / backoff / rate limiting
- the Calibre-Web provider interface (config/setup/name/id/search)
- HTML/JSON helpers (lxml, unescape, text cleanup)
- ISBN / date / language / URL helpers
- a MetaRecord bridge

To add a new site, subclass :class:`BaseMetadataProvider`, override the class
attributes (``__name__``, ``__id__``, ``DESCRIPTION``, ``META_URL``,
``SEARCH_URL``) and implement ``search()`` (return a list of ``MetaRecord``).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from html import unescape
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote, urljoin, urlparse

import requests
from lxml import html as lxml_html
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from cps import logger
from cps.services.Metadata import MetaRecord, MetaSourceInfo, Metadata

try:
    from cps.isoLanguages import get_lang3, get_language_name
except Exception:  # pragma: no cover - import differences between CWA versions
    get_lang3 = None
    get_language_name = None


log = logger.create()


class BaseMetadataProvider:
    """Shared base for the custom providers in this folder.

    Intentionally NOT a ``Metadata`` subclass, so Calibre-Web does not
    auto-register it as a metadata source. Concrete providers inherit from both
    this class and ``Metadata`` (multiple inheritance).
    """

    __name__ = "Base"
    __id__ = "base"

    DESCRIPTION = "Base"
    META_URL = ""
    SEARCH_URL = ""

    DEFAULT_LIMIT = 7
    MAX_PARSE = 20
    TIMEOUT = 20
    MIN_INTERVAL = 1.0

    USER_AGENT = (
        "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) "
        "Gecko/20100101 Firefox/140.0"
    )

    def __init__(self) -> None:
        self.active = True
        self._last_call = 0.0
        self._rate_lock = threading.Lock()

        self.session = requests.Session()
        retry = Retry(
            total=2,
            connect=2,
            read=2,
            backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(
            max_retries=retry,
            pool_connections=4,
            pool_maxsize=4,
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self.session.headers.update({
            "User-Agent": self.USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "application/json;q=0.8,*/*;q=0.7"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
            "Cache-Control": "no-cache",
        })

    # ------------------------------------------------------------------
    # Calibre-Web provider interface
    # ------------------------------------------------------------------

    def config(self, settings: Dict) -> None:
        self.active = settings.get(self.__id__, "True") == "True"
        log.info("%s provider configured, active: %s", self.__name__, self.active)

    @staticmethod
    def setup() -> None:
        log.info("Provider initialized")

    def name(self) -> str:
        return self.__name__

    def id(self) -> str:
        return self.__id__

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _rate_limit(self) -> None:
        with self._rate_lock:
            now = time.monotonic()
            delay = self.MIN_INTERVAL - (now - self._last_call)
            if delay > 0:
                time.sleep(delay)
            self._last_call = time.monotonic()

    def _get(self, url: str, **kwargs: Any) -> Optional[requests.Response]:
        self._rate_limit()
        kwargs.setdefault("timeout", self.TIMEOUT)
        kwargs.setdefault("allow_redirects", True)
        response = self.session.get(url, **kwargs)
        if response.status_code != 200:
            log.debug("%s HTTP %s for %s", self.__name__, response.status_code, response.url)
            return None
        return response

    def _get_json(self, url: str, **kwargs: Any) -> Optional[Any]:
        response = self._get(url, **kwargs)
        if response is None:
            return None
        try:
            return response.json()
        except Exception as exc:
            log.debug("%s response is not JSON: %s", self.__name__, exc)
            return None

    # ------------------------------------------------------------------
    # HTML helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_html(source: str):
        return lxml_html.fromstring(source)

    @staticmethod
    def _text_first(values: Iterable[str]) -> str:
        for value in values:
            value = str(value).strip()
            if value:
                return value
        return ""

    @staticmethod
    def _strip_html(text: str) -> str:
        text = unescape(text or "")
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _parse_book_page(self, tree, url: str = "") -> Dict[str, Any]:
        """Extract common book fields from an HTML book page.

        Tries JSON-LD (schema.org Book/Product) first, then OpenGraph meta tags,
        then the first <h1> as a title fallback.
        """
        data: Dict[str, Any] = {}
        for script in tree.xpath('//script[@type="application/ld+json"]/text()'):
            try:
                payload = json.loads(script)
            except Exception:
                continue
            for obj in self._iter_dicts(payload):
                if isinstance(obj, dict) and (
                    obj.get("@type") in ("Book", "Product", "CreativeWork")
                    or "isbn" in obj
                    or "author" in obj
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

        return {
            "title": title,
            "authors": self._names_from_value(data.get("author")),
            "cover": self._absolute_url(data.get("image")) or meta("og:image"),
            "description": self._strip_html(data.get("description") or meta("og:description")),
            "publisher": self._text_value(data.get("publisher")),
            "isbn": self._extract_isbn(self._text_value(data.get("isbn"))),
            "date": self._year_or_date(data.get("datePublished")),
            "lang": self._text_value(data.get("inLanguage")),
        }

    # ------------------------------------------------------------------
    # Field helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _locale_to_string(locale: Any) -> str:
        if isinstance(locale, str):
            return locale
        try:
            if hasattr(locale, "language"):
                return locale.language
            return str(locale)
        except (AttributeError, TypeError):
            return "ru"

    @staticmethod
    def _text_value(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, list):
            return ", ".join(
                BaseMetadataProvider._text_value(v)
                for v in value
                if BaseMetadataProvider._text_value(v)
            ).strip()
        if isinstance(value, dict):
            for key in ("text", "value", "name", "title", "label", "displayValue"):
                if value.get(key) not in (None, "", [], {}):
                    return BaseMetadataProvider._text_value(value[key])
        return ""

    @staticmethod
    def _iter_dicts(obj: Any):
        """Recursively yield every dict found inside a JSON-like structure."""
        if isinstance(obj, dict):
            yield obj
            for value in obj.values():
                yield from BaseMetadataProvider._iter_dicts(value)
        elif isinstance(obj, list):
            for value in obj:
                yield from BaseMetadataProvider._iter_dicts(value)

    @staticmethod
    def _first_value(obj: Dict[str, Any], keys: Iterable[str]) -> Any:
        """Return the first non-empty value among the given (case-insensitive) keys."""
        lower = {str(k).lower(): v for k, v in obj.items()}
        for key in keys:
            value = lower.get(str(key).lower())
            if value not in (None, "", [], {}):
                return value
        return None

    @staticmethod
    def _names_from_value(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, list):
            result: List[str] = []
            for v in value:
                result.extend(BaseMetadataProvider._names_from_value(v))
            return result
        if isinstance(value, dict):
            for key in ("name", "fullName", "full_name", "text", "value", "label"):
                if value.get(key) not in (None, "", [], {}):
                    return BaseMetadataProvider._names_from_value(value[key])
            return []
        text = BaseMetadataProvider._text_value(value)
        return [text] if text else []

    @staticmethod
    def _normalize(value: Any) -> str:
        return " ".join(str(value).lower().strip().split())

    @staticmethod
    def _clean_title(title: str) -> str:
        title = re.sub(r"\s+", " ", str(title or "")).strip()
        title = re.sub(
            r"\s*[\(\[][^\)\]]*(?:pdf|epub|fb2|mobi|аудиокнига|электронная книга|txt)"
            r"[^\)\]]*[\)\]]\s*$",
            "",
            title,
            flags=re.IGNORECASE,
        )
        return title.strip(" -–—")

    @staticmethod
    def _clean_isbn(value: Any) -> str:
        value = str(value or "").strip().upper()
        digits = re.sub(r"[^0-9X]", "", value)
        if len(digits) in (10, 13):
            return digits
        return ""

    @staticmethod
    def _extract_isbn(value: str) -> str:
        match = re.search(
            r"\b(?:97[89][-\s]?)?\d(?:[-\s]?\d){9,16}X?\b",
            str(value or "").upper(),
        )
        return BaseMetadataProvider._clean_isbn(match.group(0)) if match else ""

    @staticmethod
    def _format_isbn(isbn: str) -> str:
        isbn = re.sub(r"[^0-9X]", "", str(isbn or "").upper())
        if len(isbn) == 13:
            return f"{isbn[:3]}-{isbn[3]}-{isbn[4:7]}-{isbn[7:12]}-{isbn[12]}"
        if len(isbn) == 10:
            return f"{isbn[0]}-{isbn[1:4]}-{isbn[4:9]}-{isbn[9]}"
        return isbn

    @staticmethod
    def _year_or_date(value: Any) -> str:
        text = str(value or "")
        match = re.search(r"\b(19\d{2}|20\d{2}|21\d{2})\b", text)
        if match:
            return match.group(1)
        match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
        return match.group(1) if match else ""

    @staticmethod
    def _canonical_url(url: str) -> str:
        if not url:
            return ""
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")

    def _absolute_url(self, value: Any) -> str:
        value = self._text_value(value)
        if not value:
            return ""
        if value.startswith("//"):
            return "https:" + value
        if value.startswith("/"):
            return urljoin(self.META_URL, value)
        return value

    def _lang_name(self, language: Any, locale: Any) -> List[str]:
        lang = self._text_value(language)
        if not lang:
            return []
        if get_lang3 and get_language_name:
            try:
                return [get_language_name(self._locale_to_string(locale), get_lang3(lang))]
            except Exception:
                pass
        return [lang]

    # ------------------------------------------------------------------
    # MetaRecord bridge
    # ------------------------------------------------------------------

    def _make_record(
        self,
        title: str,
        authors: List[str],
        url: str,
        pid: str = "",
    ) -> MetaRecord:
        return MetaRecord(
            id=str(pid or self._canonical_url(url) or title),
            title=title,
            authors=authors,
            url=url or self.META_URL,
            source=MetaSourceInfo(
                id=self.__id__,
                description=self.DESCRIPTION,
                link=self.META_URL,
            ),
        )

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass
