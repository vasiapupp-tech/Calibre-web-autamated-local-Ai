# -*- coding: utf-8 -*-
"""
Proglib metadata provider for Calibre-Web Automated.

Source site: https://proglib.io/

Important:
Proglib is an IT media site, not a bibliographic database. The provider searches
Proglib materials and extracts book-like sections from articles. Fields such as
ISBN/publisher/year are filled only when explicitly present in the article text.

Designed for the cps.services.Metadata provider interface used by
Calibre-Web / Calibre-Web Automated.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urljoin, urlparse

import requests
from lxml import html

from cps import logger
from cps.isoLanguages import get_lang3, get_language_name
from cps.services.Metadata import MetaRecord, MetaSourceInfo, Metadata

log = logger.create()


class Proglib(Metadata):
    __name__ = "Proglib"
    __id__ = "proglib"
    DESCRIPTION = "Proglib — Библиотека программиста"
    META_URL = "https://proglib.io"
    TIMEOUT = 20
    DEFAULT_LIMIT = 7

    # Proglib has changed its frontend more than once. We try several public
    # search URL shapes and accept only links that point back to proglib.io.
    SEARCH_URLS = (
        "https://proglib.io/search/?q={query}",
        "https://proglib.io/search?query={query}",
        "https://proglib.io/?search={query}",
    )

    ARTICLE_PREFIXES = (
        "/p/",
        "/w/",
    )

    BOOK_HINTS = (
        "книга",
        "книги",
        "учебник",
        "справочник",
        "руководство",
        "python",
        "java",
        "javascript",
        "c++",
        "linux",
        "sql",
        "алгоритм",
        "программирован",
        "разработ",
    )

    def __init__(self):
        super().__init__()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/140.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.7,en;q=0.5",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Referer": "https://proglib.io/",
            }
        )

    def config(self, settings: Dict) -> None:
        self.active = settings.get(self.__id__, "True") == "True"
        log.info("Proglib provider configured, active: %s", self.active)

    @staticmethod
    def setup() -> None:
        log.info("Proglib provider initialized")

    def name(self) -> str:
        return self.__name__

    def id(self) -> str:
        return self.__id__

    def search(
        self,
        query: str,
        generic_cover: str = "",
        locale: Any = "ru",
    ) -> Optional[List[MetaRecord]]:
        if not self.active:
            return []

        query = self._clean_query(query)
        if not query:
            return []

        log.info("Searching Proglib for: %s", query)

        try:
            article_urls = self._search_article_urls(query)

            # Direct fallback for common Proglib book roundups.
            # This is intentionally small and only activates if the site search
            # yields nothing. The URLs are stable article URLs on proglib.io.
            if not article_urls:
                article_urls = self._fallback_articles(query)

            log.info("Proglib candidate articles: %d", len(article_urls))

            candidates: List[Dict[str, Any]] = []
            seen = set()

            for article_url in article_urls[:12]:
                try:
                    for book in self._extract_books_from_article(article_url, query):
                        key = self._norm(
                            book.get("title", "") + " " + " ".join(book.get("authors", []))
                        )
                        if not key or key in seen:
                            continue
                        seen.add(key)
                        candidates.append(book)
                except Exception as exc:
                    log.warning("Proglib article parse failed %s: %s", article_url, exc)

            candidates.sort(key=lambda x: x.get("_score", 0.0), reverse=True)

            results: List[MetaRecord] = []
            locale_str = self._locale_to_string(locale)
            for item in candidates[: self.DEFAULT_LIMIT]:
                record = self._to_meta_record(item, generic_cover, locale_str)
                if record:
                    results.append(record)

            log.info("Proglib search found %d results", len(results))
            return results

        except requests.exceptions.Timeout:
            log.error("Proglib request timed out")
        except requests.exceptions.RequestException as exc:
            log.error("Proglib request failed: %s", exc)
        except Exception as exc:
            log.exception("Unexpected Proglib search error: %s", exc)

        return []

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _search_article_urls(self, query: str) -> List[str]:
        encoded = quote(query)
        found: List[Tuple[float, str]] = []
        seen = set()

        for template in self.SEARCH_URLS:
            url = template.format(query=encoded)
            try:
                resp = self.session.get(url, timeout=self.TIMEOUT)
                log.debug(
                    "Proglib search URL=%s status=%s content-type=%s size=%s",
                    resp.url,
                    resp.status_code,
                    resp.headers.get("content-type"),
                    len(resp.content),
                )
                if resp.status_code != 200:
                    continue

                tree = html.fromstring(resp.content)
                for a in tree.xpath("//a[@href]"):
                    href = (a.get("href") or "").strip()
                    full = urljoin(self.META_URL, href)
                    if not self._is_article_url(full):
                        continue

                    text = self._node_text(a)
                    parent_text = self._node_text(a.getparent()) if a.getparent() is not None else ""
                    score = self._relevance(text + " " + parent_text, query)

                    if full not in seen:
                        seen.add(full)
                        found.append((score, full))

            except Exception as exc:
                log.debug("Proglib search endpoint failed %s: %s", url, exc)

        found.sort(key=lambda x: x[0], reverse=True)
        return [url for _, url in found[:12]]

    def _fallback_articles(self, query: str) -> List[str]:
        """
        Proglib is article-oriented. These are broad book roundups that are
        useful as a last-resort index if the site's search frontend changes.
        """
        q = self._norm(query)
        urls = []

        if "python" in q or "питон" in q or "лутц" in q:
            urls.extend(
                [
                    "https://proglib.io/p/top-15-knig-po-python-ot-novichka-do-professionala-2020-04-07",
                    "https://proglib.io/p/samouchitel-po-python-dlya-nachinayushchih-chast-2-vse-chto-nuzhno-dlya-izucheniya-python-s-nulya-knigi-sayty-kanaly-i-kursy-2022-09-29",
                ]
            )

        if any(x in q for x in ("программист", "программирован", "чист", "алгоритм")):
            urls.append(
                "https://proglib.io/p/25-luchshih-knig-dlya-programmistov-2020-05-05"
            )

        if "java" in q:
            urls.append("https://proglib.io/p/java-books-rus")

        if "sql" in q or "баз" in q:
            urls.append("https://proglib.io/p/sql-digest")

        if "go" in q or "golang" in q:
            urls.append("https://proglib.io/p/30-golang-books")

        # Remove duplicates, preserve order.
        return list(dict.fromkeys(urls))

    def _is_article_url(self, url: str) -> bool:
        try:
            p = urlparse(url)
            if p.netloc not in ("proglib.io", "www.proglib.io"):
                return False
            return any(p.path.startswith(prefix) for prefix in self.ARTICLE_PREFIXES)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Article parser
    # ------------------------------------------------------------------

    def _extract_books_from_article(
        self, article_url: str, query: str
    ) -> List[Dict[str, Any]]:
        resp = self.session.get(article_url, timeout=self.TIMEOUT)
        if resp.status_code != 200:
            return []

        tree = html.fromstring(resp.content)
        article_title = self._first_text(tree, "//h1")
        article_tags = self._extract_article_tags(tree)
        results: List[Dict[str, Any]] = []

        # Proglib book roundups use H2/H3 headings for individual books.
        headings = tree.xpath("//h2 | //h3")

        for heading in headings:
            heading_text = self._clean_text(self._node_text(heading))
            if not self._looks_like_book_heading(heading_text, query):
                continue

            body_nodes = []
            node = heading.getnext()
            while node is not None:
                tag = (getattr(node, "tag", "") or "").lower()
                if tag in ("h1", "h2", "h3"):
                    break
                body_nodes.append(node)
                node = node.getnext()

            description_parts: List[str] = []
            cover = ""
            external_links: List[str] = []

            for node in body_nodes:
                if not cover:
                    imgs = node.xpath(".//img")
                    for img in imgs:
                        candidate = (
                            img.get("src")
                            or img.get("data-src")
                            or img.get("data-original")
                            or ""
                        ).strip()
                        if candidate:
                            cover = urljoin(article_url, candidate)
                            break

                for href in node.xpath(".//a/@href"):
                    if href.startswith("http") and "proglib.io" not in href:
                        external_links.append(href)

                text = self._clean_text(self._node_text(node))
                if text and not self._is_noise(text):
                    description_parts.append(text)

            description = "\n\n".join(description_parts)
            # Avoid swallowing huge parts of long articles.
            if len(description) > 6000:
                description = description[:6000].rsplit(" ", 1)[0] + "…"

            authors, title = self._split_author_title(heading_text)
            all_text = heading_text + "\n" + description

            isbn = self._extract_isbn(all_text)
            publisher = self._extract_label(
                all_text, ("издательство", "издатель")
            )
            published = self._extract_year(all_text)
            language = "ru"

            score = self._relevance(
                " ".join([title, *authors, description[:700]]), query
            )

            # Keep loosely relevant entries only when article itself is highly relevant.
            article_score = self._relevance(article_title, query)
            score = max(score, 0.35 * article_score)

            if score < 0.18:
                continue

            source_id = hashlib.sha1(
                (article_url + "#" + heading_text).encode("utf-8")
            ).hexdigest()[:20]

            identifiers = {"proglib": source_id}
            if isbn:
                identifiers["isbn"] = re.sub(r"[^0-9Xx]", "", isbn)

            results.append(
                {
                    "id": source_id,
                    "title": title or heading_text,
                    "authors": authors,
                    "url": article_url,
                    "cover": cover,
                    "description": description,
                    "publisher": publisher,
                    "published": published,
                    "language": language,
                    "identifiers": identifiers,
                    "tags": article_tags,
                    "_score": score,
                    "_external_links": external_links,
                }
            )

        return results

    # ------------------------------------------------------------------
    # Conversion
    # ------------------------------------------------------------------

    def _to_meta_record(
        self, item: Dict[str, Any], generic_cover: str, locale: str
    ) -> Optional[MetaRecord]:
        title = (item.get("title") or "").strip()
        if not title:
            return None

        record = MetaRecord(
            id=str(item.get("id", "")),
            title=title,
            authors=item.get("authors") or [],
            url=item.get("url") or self.META_URL,
            source=MetaSourceInfo(
                id=self.__id__,
                description=self.DESCRIPTION,
                link=self.META_URL,
            ),
        )

        record.cover = item.get("cover") or generic_cover
        record.description = item.get("description") or ""
        record.publisher = item.get("publisher") or ""
        record.publishedDate = item.get("published") or ""
        record.rating = 0
        record.identifiers = item.get("identifiers") or {}
        record.tags = item.get("tags") or []

        try:
            lang = item.get("language") or "ru"
            record.languages = [get_language_name(locale, get_lang3(lang))]
        except Exception:
            record.languages = ["ru"]

        return record

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _locale_to_string(locale: Any) -> str:
        if isinstance(locale, str):
            return locale
        try:
            if hasattr(locale, "language"):
                return locale.language
            return str(locale)
        except Exception:
            return "ru"

    @staticmethod
    def _node_text(node) -> str:
        if node is None:
            return ""
        try:
            return " ".join(t.strip() for t in node.itertext() if t and t.strip())
        except Exception:
            return ""

    @classmethod
    def _first_text(cls, tree, xpath: str) -> str:
        nodes = tree.xpath(xpath)
        return cls._clean_text(cls._node_text(nodes[0])) if nodes else ""

    @classmethod
    def _extract_article_tags(cls, tree) -> List[str]:
        """Extract topic tags from a Proglib article page."""
        tags: List[str] = []
        for a in tree.xpath("//a[@href]"):
            href = (a.get("href") or "").lower()
            text = cls._clean_text(cls._node_text(a))
            if not text or len(text) > 40:
                continue
            if any(part in href for part in ("/tag/", "/tags/", "/topic/", "/category/", "tag=")):
                if text not in tags:
                    tags.append(text)
        return tags[:8]

    @staticmethod
    def _clean_text(value: str) -> str:
        value = html_lib.unescape(value or "")
        value = re.sub(r"\s+", " ", value)
        return value.strip()

    @staticmethod
    def _norm(value: str) -> str:
        value = (value or "").lower().replace("ё", "е")
        value = re.sub(r"[«»“”„\"'`]", " ", value)
        value = re.sub(r"[^0-9a-zа-я+#.]+", " ", value, flags=re.IGNORECASE)
        return " ".join(value.split())

    @classmethod
    def _clean_query(cls, query: str) -> str:
        q = cls._clean_text(query)
        q = re.sub(r"\([^)]*(?:pdfdrive|pdf|epub|fb2|mobi)[^)]*\)", " ", q, flags=re.I)
        q = re.sub(r"\[[^\]]+\]", " ", q)
        q = q.replace("|", " ")
        q = re.sub(r"\s+", " ", q).strip()
        return q

    @classmethod
    def _relevance(cls, text: str, query: str) -> float:
        a = cls._norm(text)
        b = cls._norm(query)
        if not a or not b:
            return 0.0

        seq = SequenceMatcher(None, a[:500], b[:500]).ratio()
        q_words = [w for w in b.split() if len(w) >= 3]
        if not q_words:
            return seq

        hits = sum(1 for w in q_words if w in a)
        token_score = hits / len(q_words)

        # Exact/near substring should be rewarded strongly.
        substring_bonus = 0.20 if b in a or a in b else 0.0
        return min(1.0, 0.45 * seq + 0.55 * token_score + substring_bonus)

    @classmethod
    def _looks_like_book_heading(cls, text: str, query: str) -> bool:
        if not text or len(text) < 3 or len(text) > 240:
            return False

        low = cls._norm(text)

        noise = (
            "книги для начинающих",
            "книги для среднего",
            "книги для продвинут",
            "достоинства",
            "недостатки",
            "итоги",
            "заключение",
            "содержание",
        )
        if any(low == x or low.startswith(x + " ") for x in noise):
            return False

        # Common Proglib book heading patterns:
        # "Марк Лутц. Изучаем Python"
        # "Эл Свейгарт, «Автоматизация ...»"
        # "Чистый код"
        punctuation_bookish = bool(re.search(r"[.,:—\-«»]", text))
        query_hit = cls._relevance(text, query) >= 0.22
        hint = any(h in low for h in cls.BOOK_HINTS)

        return query_hit or punctuation_bookish or hint

    @classmethod
    def _split_author_title(cls, heading: str) -> Tuple[List[str], str]:
        h = cls._clean_text(heading)

        # "Автор. Название"
        m = re.match(
            r"^([А-ЯA-ZЁ][^.!?]{2,70}?)\.\s+(.{3,})$",
            h,
        )
        if m:
            author = m.group(1).strip(" ,—-")
            title = m.group(2).strip(" «»\"")
            if cls._looks_like_person(author):
                return [author], title

        # "Автор, «Название»"
        m = re.match(r"^([^,]{3,70}),\s*[«\"](.+?)[»\"]$", h)
        if m:
            author = m.group(1).strip()
            title = m.group(2).strip()
            if cls._looks_like_person(author):
                return [author], title

        # "Название — Автор"
        m = re.match(r"^(.{3,150}?)\s+[—-]\s+([^—-]{3,70})$", h)
        if m and cls._looks_like_person(m.group(2)):
            return [m.group(2).strip()], m.group(1).strip(" «»\"")

        return [], h.strip(" «»\"")

    @staticmethod
    def _looks_like_person(value: str) -> bool:
        words = [w for w in re.split(r"\s+", value.strip()) if w]
        if not 1 <= len(words) <= 5:
            return False
        return all(
            re.match(r"^[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё'.-]*$", w)
            for w in words
        )

    @staticmethod
    def _extract_isbn(text: str) -> str:
        patterns = (
            r"\bISBN(?:-1[03])?\s*[:№]?\s*([0-9Xx][0-9Xx\-\s]{8,20}[0-9Xx])",
            r"\b(97[89][0-9\-\s]{10,20}[0-9])\b",
        )
        for pat in patterns:
            m = re.search(pat, text or "", flags=re.I)
            if not m:
                continue
            raw = m.group(1)
            cleaned = re.sub(r"[^0-9Xx]", "", raw)
            if len(cleaned) in (10, 13):
                return raw.strip()
        return ""

    @staticmethod
    def _extract_year(text: str) -> str:
        # Prefer explicit bibliographic wording.
        m = re.search(
            r"(?:год(?:\s+издания)?|издан[оие]|опубликован[оа]?)\s*[:—-]?\s*((?:19|20)\d{2})",
            text or "",
            flags=re.I,
        )
        if m:
            return m.group(1)
        return ""

    @staticmethod
    def _extract_label(text: str, labels: Tuple[str, ...]) -> str:
        for label in labels:
            m = re.search(
                rf"\b{re.escape(label)}\s*[:—-]\s*([^.;\n]{{2,100}})",
                text or "",
                flags=re.I,
            )
            if m:
                return m.group(1).strip()
        return ""

    @staticmethod
    def _is_noise(text: str) -> bool:
        low = text.lower()
        noise = (
            "больше полезных материалов",
            "подписывайтесь",
            "телеграм-канал",
            "реклама",
            "объявление",
            "читайте также",
        )
        return any(x in low for x in noise)
