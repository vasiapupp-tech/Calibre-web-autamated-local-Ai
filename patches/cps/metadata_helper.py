# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2025 Calibre-Web contributors
# Copyright (C) 2024-2025 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

import base64
import json
import os
import re
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

from cps import config, logger, db, helper
from cps.isoLanguages import get_lang3
from cps.search_metadata import cl as metadata_providers
from cps.services.Metadata import MetaRecord, MetaSourceInfo
import sys
sys.path.insert(1, '/app/calibre-web-automated/scripts/')
from cwa_db import CWA_DB

log = logger.create()

def fetch_and_apply_metadata(book_id: int, user_enabled: bool = False) -> bool:
    """
    Fetch metadata for a newly ingested book and apply it if settings allow.
    
    Args:
        book_id: The ID of the book to fetch metadata for
        user_enabled: Deprecated parameter - metadata fetching is now admin-controlled only
        
    Returns:
        bool: True if metadata was successfully fetched and applied, False otherwise
    """
    try:
        if not db.CalibreDB.session_factory:
            log.error("CalibreDB not initialized; skipping metadata fetch")
            return False

        # Check global settings (admin-controlled only)
        cwa_db = CWA_DB()
        cwa_settings = cwa_db.get_cwa_settings()
        
        if not cwa_settings.get('auto_metadata_fetch_enabled', False):
            log.debug("Auto metadata fetch disabled by administrator")
            return False
            
        # Get the book
        calibre_db_instance = db.CalibreDB(expire_on_commit=False, init=True)
        book = calibre_db_instance.get_book(book_id)
        if not book:
            log.error(f"Book with ID {book_id} not found")
            return False
            
        # Try local AI metadata extraction first (if enabled)
        ai_applied = False
        if cwa_settings.get('auto_metadata_ai_enabled', False):
            ai_metadata = _fetch_metadata_from_ai(book, calibre_db_instance)
            if ai_metadata is not None:
                if _apply_metadata_to_book(book, ai_metadata, calibre_db_instance):
                    log.info("Successfully applied AI metadata for book: %s", book.title)
                    ai_applied = True

        # If AI already set the metadata and the book has an annotation, we're done.
        if ai_applied and _book_has_description(book):
            calibre_db_instance.session.close()
            return True
            
        # Create search query from book title and author
        search_query = book.title
        if book.authors:
            author_names = [author.name for author in book.authors]
            search_query += " " + " ".join(author_names)
            
        log.info(f"Fetching metadata for: {search_query}")
        
        # Get provider hierarchy
        try:
            provider_hierarchy = json.loads(cwa_settings.get('metadata_provider_hierarchy', '["google","douban","dnb","ibdb","comicvine"]'))
        except (json.JSONDecodeError, TypeError):
            provider_hierarchy = ["google", "douban", "dnb", "ibdb", "comicvine"]

        # Global provider enablement map
        enabled_map = _parse_metadata_providers_enabled(
            cwa_settings.get('metadata_providers_enabled', '{}')
        )
            
        # Try each provider in order
        metadata_found = False
        for provider_id in provider_hierarchy:
            # Check if explicitly disabled (default is enabled if not specified)
            is_enabled = enabled_map.get(provider_id, True)
            if not is_enabled:
                log.debug(f"Provider {provider_id} is globally disabled")
                continue
            try:
                # Find the provider
                provider = None
                for p in metadata_providers:
                    if p.__id__ == provider_id:
                        provider = p
                        break
                        
                if not provider or not provider.active:
                    continue
                    
                log.debug(f"Trying metadata provider: {provider.__name__}")
                
                # Search for metadata
                results = provider.search(search_query, "", "en")
                if not results or len(results) == 0:
                    continue
                    
                # Use the first result
                metadata = results[0]
                
                # Apply metadata to book
                if ai_applied:
                    # AI already set title/authors; only take the annotation (description).
                    if _apply_description_only(book, metadata.description, calibre_db_instance):
                        log.info(f"Applied description from {provider.__name__} for book: {book.title}")
                        metadata_found = True
                        break
                else:
                    if _apply_metadata_to_book(book, metadata, calibre_db_instance):
                        log.info(f"Successfully applied metadata from {provider.__name__} for book: {book.title}")
                        metadata_found = True
                        break
                    
            except Exception as e:
                log.warning(f"Error fetching metadata from provider {provider_id}: {e}")
                continue
                
        calibre_db_instance.session.close()
        return ai_applied or metadata_found
        
    except Exception as e:
        log.error(f"Error in fetch_and_apply_metadata: {e}", exc_info=True)
        return False


_LANG_MAP = {
    "ru": "ru", "rus": "ru", "russian": "ru", "русский": "ru",
    "en": "en", "eng": "en", "english": "en", "английский": "en",
    "de": "de", "deu": "de", "ger": "de", "german": "de", "немецкий": "de",
    "fr": "fr", "fra": "fr", "fre": "fr", "french": "fr", "французский": "fr",
    "es": "es", "spa": "es", "spanish": "es", "испанский": "es",
    "uk": "uk", "ukr": "uk", "ukrainian": "uk", "украинский": "uk",
    "it": "it", "ita": "it", "italian": "it", "итальянский": "it",
    "pl": "pl", "pol": "pl", "polish": "pl", "польский": "pl",
}


def _normalize_language(lang):
    """Map a language name or code to an ISO639-1 code (e.g. 'Russian' -> 'ru')."""
    if not lang:
        return ""
    key = str(lang).strip().lower()
    if key in _LANG_MAP:
        return _LANG_MAP[key]
    return str(lang).strip()


def _render_first_pages(book, count: int = 3):
    """Render the first ``count`` pages of the book's PDF format to PNGs via Ghostscript.

    Returns a list of PNG paths (possibly shorter than ``count`` if the PDF has fewer
    pages or rendering fails). Returns an empty list if there is no PDF.
    """
    import subprocess
    import tempfile
    book_dir = os.path.join(config.get_book_path(), book.path)
    if not os.path.isdir(book_dir):
        return []
    pdf_file = None
    for name in sorted(os.listdir(book_dir)):
        if name.lower().endswith('.pdf'):
            pdf_file = os.path.join(book_dir, name)
            break
    if not pdf_file:
        return []
    out_paths = []
    for page in range(1, count + 1):
        out_png = os.path.join(tempfile.gettempdir(), '_cwa_page_%s_%d.png' % (book.id, page))
        try:
            result = subprocess.run(
                ['gs', '-dNOPAUSE', '-dBATCH', '-sDEVICE=png16m', '-r150',
                 '-dFirstPage=%d' % page, '-dLastPage=%d' % page,
                 '-sOutputFile=' + out_png, pdf_file],
                capture_output=True, text=True, timeout=120,
            )
        except Exception as e:
            log.debug("Failed to render page %d for book %s: %s", page, book.id, e)
            break
        if result.returncode != 0 or not os.path.isfile(out_png) or os.path.getsize(out_png) == 0:
            break
        out_paths.append(out_png)
    return out_paths


def _local(tag):
    """Return the local (namespace-stripped) name of an XML tag."""
    return tag.rsplit('}', 1)[-1] if '}' in tag else tag


def _find_all(root, localname):
    return [e for e in root.iter() if _local(e.tag) == localname]


def _extract_epub_cover_and_meta(epub_path):
    """Extract the cover image and metadata from an EPUB file.

    Returns ``(cover_bytes_or_None, meta_dict)``. Parses the OPF package document
    (no external dependency): title, authors, description, subjects, language,
    publisher and the cover image referenced by the manifest.
    """
    meta = {'title': '', 'authors': [], 'description': '', 'tags': [], 'language': '', 'publisher': ''}
    cover_bytes = None
    try:
        with zipfile.ZipFile(epub_path) as zf:
            opf_path = None
            try:
                container = ET.fromstring(zf.read('META-INF/container.xml'))
                for e in container.iter():
                    if _local(e.tag) == 'rootfile':
                        opf_path = e.get('full-path')
                        if opf_path:
                            break
            except Exception:
                opf_path = None
            if not opf_path:
                for name in zf.namelist():
                    if name.lower().endswith('.opf'):
                        opf_path = name
                        break
            if not opf_path:
                return cover_bytes, meta

            root = ET.fromstring(zf.read(opf_path))
            metadata = next((e for e in root.iter() if _local(e.tag) == 'metadata'), None)
            if metadata is None:
                return cover_bytes, meta

            def dc_field(name):
                for e in _find_all(metadata, name):
                    if e.text and e.text.strip():
                        return e.text.strip()
                return ''

            meta['title'] = dc_field('title')
            meta['authors'] = [e.text.strip() for e in _find_all(metadata, 'creator') if e.text and e.text.strip()]
            meta['description'] = dc_field('description')
            meta['tags'] = [e.text.strip() for e in _find_all(metadata, 'subject') if e.text and e.text.strip()]
            meta['language'] = dc_field('language')
            meta['publisher'] = dc_field('publisher')

            # Find the cover image href from the manifest.
            cover_id = None
            for e in _find_all(metadata, 'meta'):
                if e.get('name') == 'cover':
                    cover_id = e.get('content')
                    break
            manifest = next((e for e in root.iter() if _local(e.tag) == 'manifest'), None)
            if manifest is not None:
                cover_href = None
                for item in _find_all(manifest, 'item'):
                    if item.get('id') == cover_id or item.get('properties') == 'cover-image':
                        cover_href = item.get('href')
                        break
                if cover_href:
                    import posixpath
                    base = posixpath.dirname(opf_path)
                    full = posixpath.normpath(posixpath.join(base, cover_href)) if base else cover_href
                    try:
                        cover_bytes = zf.read(full)
                    except Exception:
                        cover_bytes = None
        return cover_bytes, meta
    except Exception as e:
        log.debug("Failed to parse EPUB metadata %s: %s", epub_path, e)
        return None, {}


def _extract_fb2_cover_and_meta(fb2_path):
    """Extract the cover image and metadata from a FictionBook2 (.fb2) file.

    Returns ``(cover_bytes_or_None, meta_dict)``. Parses ``<title-info>`` (title,
    authors, annotation, genres, language, publisher) and the coverpage image.
    """
    meta = {'title': '', 'authors': [], 'description': '', 'tags': [], 'language': '', 'publisher': ''}
    cover_bytes = None
    try:
        root = ET.parse(fb2_path).getroot()
        title_info = next((e for e in root.iter() if _local(e.tag) == 'title-info'), None)
        if title_info is not None:
            for e in _find_all(title_info, 'book-title'):
                if e.text and e.text.strip():
                    meta['title'] = e.text.strip()
                    break
            authors = []
            for author in _find_all(title_info, 'author'):
                parts = []
                for tag in ('first-name', 'middle-name', 'last-name'):
                    for e in _find_all(author, tag):
                        if e.text and e.text.strip():
                            parts.append(e.text.strip())
                            break
                name = ' '.join(parts).strip()
                if name:
                    authors.append(name)
            meta['authors'] = authors
            for ann in _find_all(title_info, 'annotation'):
                text = ''.join(ann.itertext()).strip()
                if text:
                    meta['description'] = text
                    break
            meta['tags'] = [g.text.strip() for g in _find_all(title_info, 'genre') if g.text and g.text.strip()]
            for lang in _find_all(title_info, 'lang'):
                if lang.text and lang.text.strip():
                    meta['language'] = lang.text.strip()
                    break
            for pub in _find_all(title_info, 'publisher'):
                if pub.text and pub.text.strip():
                    meta['publisher'] = pub.text.strip()
                    break
        # Cover image from <coverpage><image .../></coverpage> -> <binary id=...>
        coverpage = next((e for e in root.iter() if _local(e.tag) == 'coverpage'), None)
        if coverpage is not None:
            for img in _find_all(coverpage, 'image'):
                href = img.get('href') or img.get('{http://www.w3.org/1999/xlink}href')
                if not href:
                    continue
                href = href.lstrip('#')
                for binary in _find_all(root, 'binary'):
                    if binary.get('id') == href and binary.text:
                        try:
                            cover_bytes = base64.b64decode(binary.text)
                        except Exception:
                            cover_bytes = None
                        break
                if cover_bytes:
                    break
        return cover_bytes, meta
    except Exception as e:
        log.debug("Failed to parse FB2 metadata %s: %s", fb2_path, e)
        return None, {}


def _save_cover_temp(book_id, data):
    """Save raw cover image bytes to a temporary JPEG and return its path."""
    import io
    import tempfile
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert('RGB')
        path = os.path.join(tempfile.gettempdir(), '_cwa_cover_%s.jpg' % book_id)
        img.save(path, 'JPEG', quality=85)
        return path
    except Exception as e:
        log.debug("Failed to decode cover image for book %s: %s", book_id, e)
        return None


def _get_book_source(book, book_dir):
    """Determine what to send to the AI for a book.

    Returns ``(image_paths, embedded_meta, is_rendered)``:
    - ``image_paths``: image files for the AI (PDF pages, or a single extracted
      epub/fb2 cover, or cover.jpg).
    - ``embedded_meta``: dict with structured metadata extracted from epub/fb2
      (title, authors, description/annotation, tags, language, publisher).
    - ``is_rendered``: True if image_paths are rendered PDF pages (so cover_page
      is a 1..N index), False for a single cover image.
    """
    embedded_meta = {}
    if not os.path.isdir(book_dir):
        return [], embedded_meta, False
    files = sorted(os.listdir(book_dir))

    pdf = next((f for f in files if f.lower().endswith('.pdf')), None)
    if pdf:
        paths = _render_first_pages(book, count=3)
        if paths:
            return paths, embedded_meta, True

    epub = next((f for f in files if f.lower().endswith('.epub')), None)
    if epub:
        cover_bytes, meta = _extract_epub_cover_and_meta(os.path.join(book_dir, epub))
        if meta:
            embedded_meta = meta
        if cover_bytes:
            tmp = _save_cover_temp(book.id, cover_bytes)
            if tmp:
                return [tmp], embedded_meta, False

    fb2 = next((f for f in files if f.lower().endswith('.fb2')), None)
    if fb2:
        cover_bytes, meta = _extract_fb2_cover_and_meta(os.path.join(book_dir, fb2))
        if meta:
            embedded_meta = meta
        if cover_bytes:
            tmp = _save_cover_temp(book.id, cover_bytes)
            if tmp:
                return [tmp], embedded_meta, False

    cover = os.path.join(book_dir, 'cover.jpg')
    if os.path.isfile(cover):
        return [cover], embedded_meta, False

    return [], embedded_meta, False


def _fetch_metadata_from_ai(book, calibre_db_instance):
    """Extract book metadata using a local AI server.

    Sources an image (rendered PDF pages, an extracted epub/fb2 cover, or the
    existing cover.jpg), asks the AI for metadata, and merges it with structured
    metadata parsed from epub/fb2 (title, authors, annotation/description, etc.).
    """
    try:
        cwa_db = CWA_DB()
        cwa_settings = cwa_db.get_cwa_settings()
        ai_url = (cwa_settings.get('ai_metadata_url') or '').strip()
        if not ai_url:
            return None

        book_dir = os.path.join(config.get_book_path(), book.path)

        page_paths, embedded_meta, is_rendered = _get_book_source(book, book_dir)
        if not page_paths:
            log.debug("No image source for AI extraction: %s", book.path)
            return None

        # Build one image part per page/cover.
        image_parts = []
        for path in page_paths:
            with open(path, 'rb') as f:
                b64 = base64.b64encode(f.read()).decode()
            mime = 'image/png' if path.lower().endswith('.png') else 'image/jpeg'
            image_parts.append(
                {"type": "image_url", "image_url": {"url": "data:" + mime + ";base64," + b64}}
            )

        if is_rendered:
            intro = ("Это первые страницы книги (по порядку, начиная с первой). "
                     "Определи, какая из них является обложкой книги (если есть).")
        else:
            intro = "Это обложка книги."
        prompt = (intro + ' Верни строгий JSON: '
                  '{"cover_page": <номер страницы 1..%d или null>, "title": "...", "authors": ["..."], '
                  '"language": "ru", "tags": ["..."], "publisher": "..."}. '
                  '"authors" — полные имена авторов (имя и фамилия полностью). "language" — код '
                  'ISO639-1 из двух букв (ru, en, de, ...). (если поля нет — null или пустой массив).'
                  ) % len(page_paths)
        payload = {
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}] + image_parts}],
            "temperature": 0.0,
            "max_tokens": 1500,
            "response_format": {"type": "json_object"},
        }
        endpoint = ai_url.rstrip('/') + "/v1/chat/completions"
        req = urllib.request.Request(
            endpoint, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read().decode("utf-8"))
        content = resp["choices"][0]["message"]["content"]
        meta = json.loads(content)

        ai_title = (meta.get("title") or "").strip()
        ai_authors = [a.strip() for a in (meta.get("authors") or []) if a and str(a).strip()]
        ai_tags = [t.strip() for t in (meta.get("tags") or []) if t and str(t).strip()]
        ai_publisher = (meta.get("publisher") or "").strip()
        ai_lang = _normalize_language(meta.get("language") or "")
        cover_page = meta.get("cover_page")

        # Structured epub/fb2 metadata is authoritative; the AI fills the gaps.
        title = (embedded_meta.get('title') or '').strip() or ai_title
        authors = embedded_meta.get('authors') or ai_authors
        tags = (embedded_meta.get('tags') or []) or ai_tags
        publisher = (embedded_meta.get('publisher') or '').strip() or ai_publisher
        description = (embedded_meta.get('description') or '').strip()
        lang = _normalize_language(embedded_meta.get('language') or '') or ai_lang

        cover_path = os.path.join(book_dir, 'cover.jpg')

        # Save the cover: PDF -> identified page; epub/fb2 -> the extracted image.
        if is_rendered:
            cover_index = None
            if cover_page is not None:
                try:
                    idx = int(cover_page) - 1
                    if 0 <= idx < len(page_paths):
                        cover_index = idx
                except (TypeError, ValueError):
                    cover_index = None
            if cover_index is not None:
                try:
                    from PIL import Image
                    Image.open(page_paths[cover_index]).convert('RGB').save(cover_path, 'JPEG', quality=85)
                    book.has_cover = 1
                    log.info("AI-detected cover saved for book %s (page %d)", book.id, cover_index + 1)
                except Exception as e:
                    log.warning("Failed to save AI-detected cover for book %s: %s", book.id, e)
        else:
            # Single image that is a freshly-extracted cover (not the existing cover.jpg).
            if page_paths[0] != cover_path:
                try:
                    from PIL import Image
                    Image.open(page_paths[0]).convert('RGB').save(cover_path, 'JPEG', quality=85)
                    book.has_cover = 1
                    log.info("Cover extracted and saved for book %s", book.id)
                except Exception as e:
                    log.warning("Failed to save extracted cover for book %s: %s", book.id, e)

        # Clean up temporary images (skip the real cover.jpg).
        for path in page_paths:
            if path == cover_path:
                continue
            try:
                os.remove(path)
            except OSError:
                pass

        if not title and not authors:
            return None

        record = MetaRecord(
            id="local_ai",
            title=title or book.title,
            authors=authors,
            url="",
            source=MetaSourceInfo(id="local_ai", description="Local AI", link=""),
        )
        record.tags = tags
        record.publisher = publisher or None
        if description:
            record.description = description
        if lang:
            try:
                record.languages = [get_lang3(lang)]
            except Exception:
                record.languages = []
        return record
    except Exception as e:
        log.warning("AI metadata extraction failed for book %s: %s",
                    getattr(book, 'id', 'unknown'), e)
        return None


def _normalize_description(description: str) -> str:
    """Ensure the description is stored as HTML (Calibre comments are HTML).

    Plain-text annotations are wrapped in <p> tags and line breaks are turned
    into paragraph breaks so they render correctly in Calibre-Web.
    """
    text = (description or "").strip()
    if not text:
        return ""
    if re.search(r"<[a-z][^>]*>", text, re.IGNORECASE):
        return text
    paras = [p.strip() for p in text.splitlines() if p.strip()]
    return "<p>" + "</p><p>".join(paras) + "</p>"


def _book_has_description(book) -> bool:
    """Return True if the book already has a non-empty comment (annotation)."""
    if book.comments:
        text = book.comments[0].text
        return bool(text and text.strip())
    return False


def _apply_description_only(book, description, calibre_db_instance) -> bool:
    """Apply only the description/annotation to a book (keeps title/authors intact)."""
    description = (description or "").strip()
    if not description:
        return False
    normalized = _normalize_description(description)
    if book.comments:
        book.comments[0].text = normalized
    else:
        comment = db.Comments(normalized, book.id)
        calibre_db_instance.session.add(comment)
    return True


def _apply_metadata_to_book(book, metadata, calibre_db_instance) -> bool:
    """
    Apply fetched metadata to a book record.
    
    Args:
        book: The book database record
        metadata: The metadata record from provider
        calibre_db_instance: Database instance
        
    Returns:
        bool: True if metadata was successfully applied
    """
    try:
        # Get CWA settings to check smart application preference and field selections
        cwa_db = CWA_DB()
        cwa_settings = cwa_db.get_cwa_settings()
        use_smart_application = cwa_settings.get('auto_metadata_smart_application', False)
        
        updated = False
        
        # Update title - only if enabled in settings
        if (cwa_settings.get('auto_metadata_update_title', True) and 
            metadata.title and metadata.title.strip()):
            if use_smart_application:
                if len(metadata.title.strip()) > len(book.title.strip()):
                    book.title = metadata.title.strip()
                    updated = True
            else:
                book.title = metadata.title.strip()
                updated = True
            
        # Update authors - only if enabled in settings
        if (cwa_settings.get('auto_metadata_update_authors', True) and 
            metadata.authors and len(metadata.authors) > 0):
            # Clear existing authors
            book.authors.clear()
            for author_name in metadata.authors:
                if author_name and author_name.strip():
                    author = calibre_db_instance.get_author_by_name(author_name.strip())
                    if not author:
                        author = db.Authors(author_name.strip(), author_name.strip())
                        calibre_db_instance.session.add(author)
                    book.authors.append(author)
            # Keep author_sort in sync with the (new) authors so Calibre-Web can
            # display them in the right order (avoids stale author_sort).
            book.author_sort = ' & '.join(a.sort for a in book.authors if a.sort)
            updated = True
            
        # Update description - only if enabled in settings
        if (cwa_settings.get('auto_metadata_update_description', True) and 
            metadata.description and metadata.description.strip()):
            normalized_description = _normalize_description(metadata.description)
            current_description = book.comments[0].text if book.comments else ""
            if use_smart_application:
                if len(normalized_description) > len(current_description):
                    if book.comments:
                        book.comments[0].text = normalized_description
                    else:
                        comment = db.Comments(normalized_description, book.id)
                        calibre_db_instance.session.add(comment)
                    updated = True
            else:
                if book.comments:
                    book.comments[0].text = normalized_description
                else:
                    comment = db.Comments(normalized_description, book.id)
                    calibre_db_instance.session.add(comment)
                updated = True
            
        # Update publisher - only if enabled in settings
        if (cwa_settings.get('auto_metadata_update_publisher', True) and 
            metadata.publisher and metadata.publisher.strip()):
            if use_smart_application:
                if not book.publishers or len(book.publishers) == 0:
                    publisher = calibre_db_instance.get_publisher_by_name(metadata.publisher.strip())
                    if not publisher:
                        publisher = db.Publishers(metadata.publisher.strip(), metadata.publisher.strip())
                        calibre_db_instance.session.add(publisher)
                    book.publishers = [publisher]
                    updated = True
            else:
                # Clear existing publishers and add new one
                book.publishers.clear()
                publisher = calibre_db_instance.get_publisher_by_name(metadata.publisher.strip())
                if not publisher:
                    publisher = db.Publishers(metadata.publisher.strip(), metadata.publisher.strip())
                    calibre_db_instance.session.add(publisher)
                book.publishers = [publisher]
                updated = True
                
        # Update tags if available and enabled in settings
        if (cwa_settings.get('auto_metadata_update_tags', True) and 
            hasattr(metadata, 'tags') and metadata.tags):
            book.tags.clear()
            for tag_name in metadata.tags:
                if tag_name and tag_name.strip():
                    tag = calibre_db_instance.get_tag_by_name(tag_name.strip())
                    if not tag:
                        tag = db.Tags(name=tag_name.strip())
                        calibre_db_instance.session.add(tag)
                    if tag not in book.tags:
                        book.tags.append(tag)
            updated = True
            
        # Update language if available (ISO639-3 codes, e.g. 'rus', 'eng')
        if hasattr(metadata, 'languages') and metadata.languages:
            book.languages.clear()
            for lang_code in metadata.languages:
                if not lang_code:
                    continue
                language = calibre_db_instance.session.query(db.Languages).filter(
                    db.Languages.lang_code == lang_code).first()
                if not language:
                    language = db.Languages(lang_code)
                    calibre_db_instance.session.add(language)
                if language not in book.languages:
                    book.languages.append(language)
            updated = True
            
        # Update series if available and enabled in settings
        if (cwa_settings.get('auto_metadata_update_series', True) and 
            hasattr(metadata, 'series') and metadata.series and metadata.series.strip()):
            series = calibre_db_instance.get_series_by_name(metadata.series.strip())
            if not series:
                series = db.Series(metadata.series.strip(), metadata.series.strip())
                calibre_db_instance.session.add(series)
            book.series.clear()
            book.series.append(series)
            
            # Set series index if available
            if hasattr(metadata, 'series_index') and metadata.series_index:
                try:
                    # Convert to float first to validate, then store as string (DB column is String)
                    float_value = float(metadata.series_index)
                    book.series_index = str(float_value)
                except (ValueError, TypeError):
                    book.series_index = '1.0'
            updated = True
            
        # Update published date if available and enabled in settings
        if (cwa_settings.get('auto_metadata_update_published_date', True) and 
            hasattr(metadata, 'publishedDate') and metadata.publishedDate):
            try:
                from datetime import datetime
                if isinstance(metadata.publishedDate, str):
                    # Try to parse various date formats
                    for fmt in ['%Y-%m-%d', '%Y-%m', '%Y']:
                        try:
                            book.pubdate = datetime.strptime(metadata.publishedDate, fmt).date()
                            updated = True
                            break
                        except ValueError:
                            continue
                elif hasattr(metadata.publishedDate, 'date'):
                    book.pubdate = metadata.publishedDate.date()
                    updated = True
            except Exception as e:
                log.warning(f"Error parsing published date: {e}")
                
        # Update rating if available and enabled in settings
        if (cwa_settings.get('auto_metadata_update_rating', True) and 
            hasattr(metadata, 'rating') and metadata.rating):
            try:
                rating_value = float(metadata.rating)
                if 0 <= rating_value <= 10:  # Calibre uses 0-10 scale
                    if book.ratings:
                        book.ratings[0].rating = int(rating_value * 2)  # Convert to Calibre's 0-10 scale
                    else:
                        rating = db.Ratings(rating=int(rating_value * 2))
                        calibre_db_instance.session.add(rating)
                        book.ratings = [rating]
                    updated = True
            except (ValueError, TypeError):
                pass
                
        # Update identifiers if available and enabled in settings
        if (cwa_settings.get('auto_metadata_update_identifiers', True) and 
            hasattr(metadata, 'identifiers') and metadata.identifiers):
            for identifier_type, identifier_value in metadata.identifiers.items():
                if identifier_type and identifier_value:
                    # Check if identifier already exists
                    existing = False
                    for identifier in book.identifiers:
                        if identifier.type == identifier_type:
                            identifier.val = identifier_value
                            existing = True
                            break
                    if not existing:
                        new_identifier = db.Identifiers(identifier_value, identifier_type, book.id)
                        calibre_db_instance.session.add(new_identifier)
                        book.identifiers.append(new_identifier)
                    updated = True
        
        # Handle cover image - only if enabled in settings
        if (cwa_settings.get('auto_metadata_update_cover', True) and 
            hasattr(metadata, 'cover') and metadata.cover):
            if not use_smart_application:
                cover_url = (metadata.cover or "").strip()
                if cover_url and not cover_url.endswith('/static/generic_cover.svg'):
                    try:
                        result, error = helper.save_cover_from_url(cover_url, book.path)
                        if result:
                            book.has_cover = 1
                            updated = True
                        else:
                            log.warning("Failed to save cover for book %s: %s",
                                        getattr(book, 'id', 'unknown'), error)
                    except Exception as e:
                        log.warning("Error downloading cover for book %s: %s",
                                    getattr(book, 'id', 'unknown'), e)
        
        if updated:
            calibre_db_instance.session.commit()
            
        return updated
        
    except Exception as e:
        log.error(f"Error applying metadata to book {getattr(book, 'id', 'unknown')}: {e}")
        calibre_db_instance.session.rollback()
        return False


def _parse_metadata_providers_enabled(raw_value):
    """Lightweight parser for metadata_providers_enabled without importing cwa_functions."""
    try:
        if raw_value is None:
            return {}
        if isinstance(raw_value, bytes):
            raw_value = raw_value.decode('utf-8', errors='ignore')
        if isinstance(raw_value, str):
            s = raw_value.strip()
            if not s:
                return {}
            if s.startswith("'") and s.endswith("'"):
                s = s[1:-1]
            if not s:
                return {}
            data = json.loads(s)
            return data if isinstance(data, dict) else {}
        if isinstance(raw_value, dict):
            return raw_value
        return {}
    except (json.JSONDecodeError, ValueError, TypeError, AttributeError):
        return {}
