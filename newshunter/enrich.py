"""Extracao de snippet (2-3 linhas) para cada noticia.

Ordem:
1. Se o RSS ja traz summary decente (>= 150 chars), limpa HTML e usa.
2. Senao (ou se vazio), baixa a pagina com fetch_html e tenta:
   2a. Usar o extractor do clipinator se o dominio estiver cadastrado.
   2b. Usar meta tags og:description / meta[name=description] como fallback.
3. Como ultimo recurso: retorna string vazia.

Tambem preenche published_at quando o RSS nao trouxe, lendo meta
article:published_time ou JSON-LD datePublished.
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from html import unescape

from urllib.parse import urlparse

from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from .fetcher import RawItem
from .filter import strip_wp_footer

try:
    from googlenewsdecoder import gnewsdecoder  # type: ignore
except Exception:  # noqa: BLE001
    gnewsdecoder = None  # type: ignore

# Pool dedicado para gnewsdecoder. A concorrencia e controlada externamente
# pelo pipeline (fase de pre-resolucao separada), entao 8 workers e suficiente.
_gnews_ex = ThreadPoolExecutor(max_workers=8, thread_name_prefix="gnews")
_GNEWS_TIMEOUT = 8.0  # timeout por chamada (gnewsdecoder leva ~1.5s quando sem rate-limit)

log = logging.getLogger(__name__)

# Imports lazy/tolerantes ao clipinator para nao quebrar tests unitarios
try:
    from clipinator import (  # type: ignore
        EXTRACTORS,
        SOURCE_NAMES,
        _extract,
        clean_paragraphs,
        fetch_html,
    )
except Exception:  # noqa: BLE001
    EXTRACTORS = {}  # type: ignore
    SOURCE_NAMES = {}  # type: ignore
    _extract = None  # type: ignore
    clean_paragraphs = None  # type: ignore
    fetch_html = None  # type: ignore


SNIPPET_MAX_CHARS = 360
SNIPPET_MIN_RSS_CHARS = 150

# Boilerplate que o Google News coloca como summary quando agrega de varias fontes.
# Descartamos esse texto e tentamos enriquecer via fetch_html.
_GOOGLE_NEWS_BOILERPLATE = re.compile(
    r"cobertura\s+jornal[ií]stica\s+abrangente\s+e\s+atualizada",
    re.IGNORECASE,
)


def _strip_html(s: str) -> str:
    if not s:
        return ""
    soup = BeautifulSoup(unescape(s), "lxml")
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _truncate(s: str, max_chars: int = SNIPPET_MAX_CHARS) -> str:
    if len(s) <= max_chars:
        return s
    cut = s[:max_chars]
    last_space = cut.rfind(" ")
    if last_space > max_chars * 0.6:
        cut = cut[:last_space]
    return cut.rstrip(" ,;:.-") + "..."


def source_name_for(domain: str) -> str:
    if domain in SOURCE_NAMES:
        return SOURCE_NAMES[domain]
    stripped = domain.lstrip("www.")
    if stripped in SOURCE_NAMES:
        return SOURCE_NAMES[stripped]
    return domain


def _extract_title_from_html(soup: BeautifulSoup) -> str:
    """Extrai og:title ou <title> da pagina."""
    tag = soup.find("meta", attrs={"property": "og:title"})
    if tag and tag.get("content"):
        return tag["content"].strip()
    tag = soup.find("title")
    if tag and tag.string:
        return tag.string.strip()
    return ""


def _extract_from_meta(soup: BeautifulSoup) -> tuple[str, datetime | None]:
    """Pega og:description / meta description + article:published_time / JSON-LD."""
    desc = ""
    tag = soup.find("meta", attrs={"property": "og:description"}) or soup.find(
        "meta", attrs={"name": "description"}
    )
    if tag and tag.get("content"):
        desc = tag["content"].strip()

    pub: datetime | None = None
    for attrs in (
        {"property": "article:published_time"},
        {"name": "article:published_time"},
        {"name": "date"},
        {"itemprop": "datePublished"},
    ):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            try:
                pub = date_parser.parse(tag["content"])
                break
            except (ValueError, TypeError):
                continue

    if pub is None:
        for s in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                data = json.loads(s.string or "{}")
            except (ValueError, TypeError):
                continue
            for obj in data if isinstance(data, list) else [data]:
                if not isinstance(obj, dict):
                    continue
                raw = obj.get("datePublished")
                if raw:
                    try:
                        pub = date_parser.parse(raw)
                        break
                    except (ValueError, TypeError):
                        continue
            if pub:
                break

    # Fallback: <time datetime="..."> — usado por Brasil Energia e outros
    if pub is None:
        for t in soup.find_all("time", attrs={"datetime": True}):
            try:
                pub = date_parser.parse(t["datetime"])
                break
            except (ValueError, TypeError):
                continue

    if pub and pub.tzinfo is None:
        pub = pub.replace(tzinfo=timezone.utc)
    return desc, pub


def _snippet_from_rss(summary_html: str) -> str:
    text = _strip_html(summary_html)
    text = strip_wp_footer(text)
    if len(text) < SNIPPET_MIN_RSS_CHARS:
        return ""
    if _GOOGLE_NEWS_BOILERPLATE.search(text):
        return ""
    return _truncate(text)


def _clean_snippet_candidate(text: str) -> str:
    """Descarta texto se for boilerplate do Google News."""
    if not text:
        return ""
    if _GOOGLE_NEWS_BOILERPLATE.search(text):
        return ""
    return _truncate(text)


def _resolve_google_news_url(url: str) -> tuple[str, str]:
    """Decodifica wrapper news.google.com para URL real. Retorna (url, domain).

    Usa gnewsdecoder (API Google). Sujeito a rate-limit temporário (HTTP 429)
    quando o IP faz muitas chamadas em sequência. Nesse caso devolve o input
    original e o pipeline descarta o item no stage 4.
    A concorrencia é controlada pelo pipeline (RESOLVE_WORKERS=6).
    """
    if gnewsdecoder is None or not url.startswith("https://news.google.com/"):
        return url, urlparse(url).netloc.lower()

    # Normaliza /rss/articles/ → /articles/ (formato esperado pelo decoder)
    article_url = url.replace("news.google.com/rss/articles/", "news.google.com/articles/")
    article_url = article_url.split("?")[0]

    try:
        fut = _gnews_ex.submit(gnewsdecoder, article_url, interval=0)
        res = fut.result(timeout=_GNEWS_TIMEOUT)
    except FutureTimeoutError:
        log.debug("gnewsdecoder timeout em %s", url)
        return url, urlparse(url).netloc.lower()
    except Exception as e:  # noqa: BLE001
        log.debug("gnewsdecoder falhou em %s: %s", url, e)
        return url, urlparse(url).netloc.lower()

    if isinstance(res, dict) and res.get("status") and res.get("decoded_url"):
        real = res["decoded_url"]
        return real, urlparse(real).netloc.lower()

    if isinstance(res, dict) and "429" in str(res.get("message", "")):
        log.warning("gnewsdecoder rate-limited (429) — artigos Platts/SPG serão perdidos neste ciclo")

    return url, urlparse(url).netloc.lower()


def enrich_item(item: RawItem, *, resolve_google_news: bool = False, need_snippet: bool = True) -> tuple[str, datetime | None, str, str, str]:
    """Retorna (snippet, published_at, url_resolvida, dominio_resolvido, titulo).

    titulo: titulo real extraido da pagina quando item.title estava vazio
    (sitemaps WordPress padrao, alguns feeds Google News). Vazio se item
    ja tinha titulo.

    need_snippet=False (modo headlines): retorna sem rodar fetch_html quando
    ja temos title+published no item. O snippet pode sair vazio — o stage 4
    do pipeline decide o que fazer. Itens sem title ou published caem no
    fetch normalmente (mesma logica de sempre).
    """
    published = item.published_at
    extracted_title = ""

    if resolve_google_news and item.url.startswith("https://news.google.com/"):
        resolved_url, resolved_domain = _resolve_google_news_url(item.url)
    else:
        resolved_url, resolved_domain = item.url, item.source_domain

    snippet = _snippet_from_rss(item.summary)

    if not need_snippet and published and item.title:
        return snippet, published, resolved_url, resolved_domain, extracted_title

    if snippet and published and item.title:
        return snippet, published, resolved_url, resolved_domain, extracted_title

    if fetch_html is None or resolved_url.startswith("https://news.google.com/"):
        return snippet, published, resolved_url, resolved_domain, extracted_title

    try:
        html = fetch_html(resolved_url, timeout=6)
    except Exception as e:  # noqa: BLE001
        log.debug("fetch_html falhou em %s: %s", resolved_url, e)
        return snippet, published, resolved_url, resolved_domain, extracted_title

    soup = BeautifulSoup(html, "lxml")

    # Extrai titulo da pagina se o feed nao trouxe
    if not item.title:
        extracted_title = _extract_title_from_html(soup)

    # Preenche data faltante
    if published is None:
        _, meta_pub = _extract_from_meta(soup)
        if meta_pub:
            published = meta_pub

    if snippet:
        return snippet, published, resolved_url, resolved_domain, extracted_title

    # Tenta extractor do clipinator baseado no dominio resolvido
    if _extract is not None and clean_paragraphs is not None and resolved_domain in EXTRACTORS:
        try:
            _, paragrafos = _extract(html, resolved_domain)
            joined = " ".join(paragrafos[:3]).strip()
            cleaned = _clean_snippet_candidate(joined)
            if cleaned:
                return cleaned, published, resolved_url, resolved_domain, extracted_title
        except Exception as e:  # noqa: BLE001
            log.debug("extractor falhou em %s: %s", resolved_url, e)

    # Fallback: meta description
    desc, _ = _extract_from_meta(soup)
    cleaned = _clean_snippet_candidate(desc)
    if cleaned:
        return cleaned, published, resolved_url, resolved_domain, extracted_title

    # Ultimo recurso: primeiros <p> da pagina
    ps = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    ps = [p for p in ps if len(p) > 40][:3]
    joined = " ".join(ps).strip()
    return _clean_snippet_candidate(joined), published, resolved_url, resolved_domain, extracted_title
