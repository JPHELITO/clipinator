"""Scraper para World Cement (www.worldcement.com/news/).

Fonte de cobertura global de cimento — sem paywall, sem Playwright necessário.
Método: requests + BeautifulSoup (HTML scraping puro).

Fase 1 — Listing
    GET /news/ (e páginas subsequentes até _MAX_PAGES)
    → parseia article.article: title, URL, data (time[datetime]), excerpt
    → filtra artigos dentro da janela de _MAX_AGE_HOURS
    → aplica keyword matching se configurado (padrão: aceita tudo)

Fase 2 — Corpo
    GET {article_url} para cada candidato (paralelo, _BODY_WORKERS threads)
    → extrai article.article-detail + limpa tabs/ads
    → converte em HTML seguro via article_to_safe_html()

Estrutura HTML do site
──────────────────────
Listing (/news/):
  article.article (cobertura de article-top e article-list)
    h2.article-title > a  → href relativo + texto do título
    header > small > time[datetime]  → "2026-05-20 08:27:00Z" (UTC)
    div.col-xs-5 > p / div.col-xs-9 > p  → excerpt

Artigo individual:
  article.article-detail
    header > h1  → título real
    header > small > time  → data (texto, sem datetime attr)
    div.lead > p  → lead paragraph
    > div > p  → parágrafos do corpo
    .tab-content / .tab-pane  → seção de tags (REMOVER)
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

log = logging.getLogger(__name__)

_BASE_URL       = "https://www.worldcement.com"
_NEWS_URL       = "https://www.worldcement.com/news/"
_DOMAIN         = "www.worldcement.com"
_SOURCE_NAME    = "World Cement"
_MAX_AGE_HOURS  = 72      # janela de tempo; alinhado com _RETENTION_HOURS em app.py
_MAX_PAGES      = 3       # máximo de páginas da listagem a varrer
_BODY_WORKERS   = 6       # threads paralelas para fetch do corpo
_REQ_TIMEOUT    = 15      # timeout por request (segundos)
_BODY_TIMEOUT   = 12      # timeout para fetch de corpo (segundos)
_PAGE_DELAY     = 0.4     # pausa entre páginas de listagem (segundos) — politeness

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session():
    """requests.Session com headers configurados."""
    import requests
    s = requests.Session()
    s.headers.update(_HEADERS)
    return s


def _parse_dt(dt_str: str) -> datetime | None:
    """Parseia o atributo datetime="2026-05-20 08:27:00Z" → datetime UTC."""
    if not dt_str:
        return None
    dt_str = dt_str.strip()
    try:
        # fromisoformat aceita "2026-05-20 08:27:00+00:00"
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        # Fallback: strptime sem fuso
        return datetime.strptime(
            dt_str.rstrip("Z"), "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Fase 1 — listagem
# ---------------------------------------------------------------------------

def _fetch_listing(session, url: str) -> list[dict]:
    """Busca uma página de listagem e retorna candidatos brutos.

    Cada item: {"url": str, "title": str, "published_at": datetime|None, "excerpt": str}
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log.error("worldcement: BeautifulSoup não disponível")
        return []

    try:
        r = session.get(url, timeout=_REQ_TIMEOUT)
        r.raise_for_status()
    except Exception as exc:
        log.warning("worldcement: erro ao buscar listagem %s: %s", url, exc)
        return []

    soup = BeautifulSoup(r.text, "lxml")
    items: list[dict] = []

    for art in soup.select("article.article"):
        try:
            # Título + URL
            a = art.select_one("h2.article-title > a")
            if not a:
                continue
            title = a.get_text(strip=True)
            href  = (a.get("href") or "").strip()
            if not title or not href:
                continue
            art_url = urljoin(_BASE_URL, href)

            # Data (atributo datetime na tag <time> dentro do <header>)
            time_el     = art.select_one("time[datetime]")
            published_at = _parse_dt(time_el.get("datetime", "")) if time_el else None

            # Excerpt: parágrafo logo após o header no card
            p_el   = art.select_one("div.col-xs-5 > p, div.col-xs-9 > p")
            excerpt = p_el.get_text(strip=True) if p_el else ""

            items.append({
                "url":          art_url,
                "title":        title,
                "published_at": published_at,
                "excerpt":      excerpt,
            })
        except Exception as exc:
            log.debug("worldcement: erro ao parsear card: %s", exc)

    return items


# ---------------------------------------------------------------------------
# Fase 2 — corpo do artigo
# ---------------------------------------------------------------------------

def _fetch_body(session, url: str) -> str:
    """Busca e extrai o corpo de um artigo. Retorna HTML seguro."""
    try:
        from bs4 import BeautifulSoup
        from .html_utils import article_to_safe_html
    except ImportError as exc:
        log.error("worldcement: dependência ausente: %s", exc)
        return ""

    try:
        r = session.get(url, timeout=_BODY_TIMEOUT)
        r.raise_for_status()
    except Exception as exc:
        log.debug("worldcement: erro ao buscar corpo %s: %s", url, exc)
        return ""

    soup = BeautifulSoup(r.text, "lxml")
    container = soup.select_one("article.article-detail")
    if not container:
        log.debug("worldcement: article.article-detail não encontrado em %s", url)
        return ""

    # Remove elementos indesejados antes de extrair texto
    for el in container.select(
        # Seção de tags ("Related Tags"), botões de share e navegação interna
        ".tab-pane, .tab-content, .tags-container, "
        ".row.row-btn, .btn-default, "
        # Header: título e metadados já capturados separadamente (evita duplicação)
        "article > header, "
        # Scripts/estilos
        "script, style, form"
    ):
        el.decompose()

    return article_to_safe_html(str(container))


# ---------------------------------------------------------------------------
# Coletor principal
# ---------------------------------------------------------------------------

def collect_worldcement_articles():
    """Coleta artigos recentes do World Cement.

    Retorna list[store.Article] prontos para upsert no pipeline.
    """
    from .store import Article, get_all_source_keywords
    from .filter import matches_keywords

    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=_MAX_AGE_HOURS)

    # Configuração de keywords por fonte (permite customização via UI)
    source_kw_map = get_all_source_keywords()
    source_kws    = source_kw_map.get(_DOMAIN)
    # mode=all (source_kws == ["*"] ou não configurado) → aceita tudo
    accept_all    = source_kws is None or source_kws == ["*"]
    kw_list       = source_kws if not accept_all else []

    session = _make_session()

    # ── Fase 1: coletar candidatos das páginas de listagem ────────────────────
    candidates: list[dict] = []
    reached_cutoff = False

    for page in range(1, _MAX_PAGES + 1):
        page_url = _NEWS_URL if page == 1 else f"{_NEWS_URL}?page={page}"
        raw_items = _fetch_listing(session, page_url)

        if not raw_items:
            log.debug("worldcement: página %d vazia ou erro — parando paginação", page)
            break

        new_this_page = 0
        for item in raw_items:
            pub = item["published_at"]

            # Filtra pela janela de tempo (artigos sem data passam para ser conservadores)
            if pub is not None and pub < cutoff:
                reached_cutoff = True
                continue

            # Keyword matching (só aplicado se mode=specific)
            if not accept_all:
                text = f"{item['title']} {item['excerpt']}"
                if not matches_keywords(text, kw_list):
                    continue

            candidates.append(item)
            new_this_page += 1

        log.debug(
            "worldcement: página %d → %d artigos (%d candidatos no total)",
            page, len(raw_items), len(candidates),
        )

        # Se chegamos ao limite de tempo, não faz sentido ir à próxima página
        if reached_cutoff:
            break

        # Pausa educada entre requests
        if page < _MAX_PAGES:
            time.sleep(_PAGE_DELAY)

    if not candidates:
        log.info("worldcement: nenhum candidato na janela de %dh", _MAX_AGE_HOURS)
        return []

    log.info(
        "worldcement: %d candidatos na janela de %dh — iniciando fetch de corpos",
        len(candidates), _MAX_AGE_HOURS,
    )

    # ── Fase 2: fetch de corpos em paralelo ──────────────────────────────────
    def _worker(item: dict) -> tuple[dict, str]:
        body = _fetch_body(session, item["url"])
        return item, body

    articles: list[Article] = []

    with ThreadPoolExecutor(max_workers=_BODY_WORKERS, thread_name_prefix="wc_body") as ex:
        fut_map = {ex.submit(_worker, it): it for it in candidates}
        for fut in as_completed(fut_map):
            try:
                item, body = fut.result()
            except Exception as exc:
                log.debug("worldcement: body future falhou: %s", exc)
                continue

            pub = item["published_at"] or now
            # Clamp para o futuro (timestamp agendado em alguns CMS)
            if pub > now:
                pub = now

            # matched_keywords
            if accept_all:
                matched: list[str] = ["#source"]
            else:
                text    = f"{item['title']} {item['excerpt']}"
                matched = matches_keywords(text, kw_list) or ["#source"]

            articles.append(Article(
                url              = item["url"],
                domain           = _DOMAIN,
                source_name      = _SOURCE_NAME,
                title            = item["title"],
                snippet          = item["excerpt"],
                published_at     = pub,
                found_at         = now,
                matched_keywords = matched,
                body             = body,
            ))

    log.info("worldcement: %d artigos coletados", len(articles))
    return articles
