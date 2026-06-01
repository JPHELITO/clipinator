"""Registro de fontes: Platts, Fastmarkets, Valor Econômico, Mining.com, El Financiero."""
from __future__ import annotations

from urllib.parse import quote_plus

# ---------------------------------------------------------------------------
# RSS feeds. Valor tem news-sitemap em tempo real; Mining.com via categoria.
# ---------------------------------------------------------------------------
RSS_FEEDS: dict[str, list[str]] = {
    # Valor Econômico: capturado via scraper direto do sitemap (valor_scraper.py)
    # com Playwright para extração do corpo — removido do pipeline RSS padrão
    # para evitar duplicação e porque fetch_html falha em conteúdo do Globo.
    "www.mining.com": [
        # Commodities com keywords relevantes para S&M
        "https://www.mining.com/commodity/iron-ore/feed/",
        "https://www.mining.com/commodity/copper/feed/",
        "https://www.mining.com/commodity/nickel/feed/",
        "https://www.mining.com/commodity/gold/feed/",
        # Categorias com cobertura de aço, siderurgia e mineração geral
        "https://www.mining.com/category/steel/feed/",
        "https://www.mining.com/category/mining/feed/",
    ],
    # Fastmarkets: capturado via scraper direto do dashboard PP News
    # (fastmarkets_scraper.py) — RSS público desabilitado pois inclui
    # conteúdo de categorias diversas, fora do escopo do PP News dashboard.
}

# Domínios sem RSS em português — query Google News com hl=pt-BR.
NO_RSS_DOMAINS: list[str] = []

# Domínios em inglês — query Google News com hl=en-US.
# Platts migrado para scraper direto via API (platts_scraper.py) — removido do GNews.
ENGLISH_NO_RSS_DOMAINS: list[str] = []

# Sitemaps WordPress padrão — não usamos no momento.
STANDARD_SITEMAPS: dict[str, list[str]] = {}

# Sites que expõem artigos via páginas de categoria (sem RSS confiável).
# Chave = domínio; valor = lista de URLs a raspar.
HOMEPAGE_SCRAPERS: dict[str, list[str]] = {
    "www.mining.com": [
        "https://www.mining.com/commodity/iron-ore/",
        "https://www.mining.com/commodity/copper/",
    ],
    "www.elfinanciero.com.mx": [
        "https://www.elfinanciero.com.mx/",
    ],
}

# Domínios com paywall duro — aceitos sem snippet (título RSS já valida
# relevância). O leitor in-dash usa cookies para exibir o conteúdo completo.
PAYWALL_DOMAINS: frozenset[str] = frozenset([
    "www.spglobal.com",
    "core.spglobal.com",
    "www.fastmarkets.com",
    "dashboard.fastmarkets.com",
    "valor.globo.com",
])

# URLs de sitemap Google News (urlset + news:news).
SITEMAP_URL_MARKERS: tuple[str, ...] = (
    "/sitemap/",
    "sitemap-news",
    "news-sitemap",
    "sitemap_news",
    "news.xml",
)


def is_sitemap_url(url: str) -> bool:
    u = url.lower()
    return any(m in u for m in SITEMAP_URL_MARKERS)


def all_rss_feeds() -> list[tuple[str, str]]:
    """Lista (dominio, url_feed) para todo feed registrado."""
    out: list[tuple[str, str]] = []
    for domain, feeds in RSS_FEEDS.items():
        for feed_url in feeds:
            out.append((domain, feed_url))
    return out


def all_standard_sitemaps() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for domain, urls in STANDARD_SITEMAPS.items():
        for url in urls:
            out.append((domain, url))
    return out


def all_homepage_scrapers() -> list[tuple[str, str]]:
    """Lista (dominio, url) das páginas a raspar por links de artigos."""
    out: list[tuple[str, str]] = []
    for domain, urls in HOMEPAGE_SCRAPERS.items():
        for url in urls:
            out.append((domain, url))
    return out


def google_news_queries(keywords: list[str], hours: int) -> list[str]:
    """URLs de RSS do Google News, uma por keyword (PT-BR)."""
    if hours <= 48:
        when = f"{hours}h"
    else:
        days = max(1, hours // 24)
        when = f"{days}d"
    out: list[str] = []
    for kw in keywords:
        q = quote_plus(f'"{kw}" when:{when}')
        out.append(
            f"https://news.google.com/rss/search?q={q}&hl=pt-BR&gl=BR&ceid=BR:pt"
        )
    return out


def google_news_site_queries(domains: list[str], keywords: list[str], hours: int) -> list[str]:
    """Uma query Google News por domínio sem RSS, OR das keywords (PT-BR)."""
    if hours <= 48:
        when = f"{hours}h"
    else:
        days = max(1, hours // 24)
        when = f"{days}d"
    kw_or = " OR ".join(f'"{k}"' for k in keywords)
    out: list[str] = []
    for domain in domains:
        q = quote_plus(f"site:{domain} ({kw_or}) when:{when}")
        out.append(
            f"https://news.google.com/rss/search?q={q}&hl=pt-BR&gl=BR&ceid=BR:pt"
        )
    return out


def google_news_site_queries_en(domains: list[str], keywords: list[str], hours: int) -> list[str]:
    """Igual a google_news_site_queries mas com hl=en-US para sites em inglês."""
    if hours <= 48:
        when = f"{hours}h"
    else:
        days = max(1, hours // 24)
        when = f"{days}d"
    kw_or = " OR ".join(f'"{k}"' for k in keywords)
    out: list[str] = []
    for domain in domains:
        q = quote_plus(f"site:{domain} ({kw_or}) when:{when}")
        out.append(
            f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
        )
    return out
