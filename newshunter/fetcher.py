"""Coleta paralela de RSS/Atom feeds + sitemaps Google News."""
from __future__ import annotations

import calendar
import logging
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import feedparser
import requests
from dateutil import parser as date_parser

from .config import ENGLISH_SITE_KEYWORDS
from .sources import (
    ENGLISH_NO_RSS_DOMAINS,
    NO_RSS_DOMAINS,
    all_homepage_scrapers,
    all_rss_feeds,
    all_standard_sitemaps,
    google_news_queries,
    google_news_site_queries,
    google_news_site_queries_en,
    is_sitemap_url,
)
from .store import normalize_url

log = logging.getLogger(__name__)

# Timeouts curtos. FEED_TIMEOUT limita cada requisicao individual;
# COLLECT_DEADLINE e o teto GLOBAL - depois disso, feeds ainda em voo sao
# abandonados para esta busca e ficam para a proxima.
FEED_TIMEOUT = 6
STANDARD_SITEMAP_TIMEOUT = 10  # sitemaps WordPress podem ser lentos
COLLECT_DEADLINE = 25.0  # aumentado para dar tempo às queries Google News do GNews

# Segmentos de path que indicam paginas de listagem/categoria, nao artigos.
_NON_ARTICLE_SEGMENTS = frozenset({
    "category", "tag", "tags", "colunista", "dados", "autor", "author",
    "page", "search", "busca", "feed", "amp", "print", "rss",
})

# User-Agent padrao (feedparser usa urllib; passamos via agent=)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class RawItem:
    url: str
    title: str
    summary: str
    published_at: datetime | None
    source_domain: str  # dominio da noticia real (nao do feed)
    feed_domain: str    # dominio do feed que trouxe esse item


def _parse_entry_date(entry) -> datetime | None:
    # feedparser's *_parsed fields are time.struct_time in UTC. calendar.timegm
    # converts UTC struct -> UTC timestamp; time.mktime would treat the struct
    # as LOCAL time, shifting dates by the machine's tz offset.
    for key in ("published_parsed", "updated_parsed"):
        val = getattr(entry, key, None) or (entry.get(key) if isinstance(entry, dict) else None)
        if val:
            try:
                ts = calendar.timegm(val)
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                continue
    return None


def _unwrap_redirect(url: str) -> str:
    """Folha usa redir.folha.com.br/redir/.../*<destino> nos feeds; extrai o destino."""
    try:
        p = urlparse(url)
    except ValueError:
        return url
    host = p.netloc.lower()
    if host == "redir.folha.com.br":
        # Formato: .../*https://www1.folha.uol.com.br/...
        if "*" in url:
            after = url.split("*", 1)[1]
            if after.startswith(("http://", "https://")):
                return after
        # Fallback: ?url=<destino>
        qs = parse_qs(p.query)
        if "url" in qs and qs["url"]:
            return qs["url"][0]
    return url


def _entry_to_item(entry, feed_domain: str) -> RawItem | None:
    link = (entry.get("link") or "").strip()
    if not link:
        return None
    link = _unwrap_redirect(link)
    link = normalize_url(link)
    title = (entry.get("title") or "").strip()
    summary = (entry.get("summary") or entry.get("description") or "").strip()
    published = _parse_entry_date(entry)
    src_domain = urlparse(link).netloc.lower()

    # Google News: o dominio real vem em entry.source.href (tag <source url="...">)
    if src_domain.endswith("news.google.com"):
        source = entry.get("source") or {}
        source_href = source.get("href") if isinstance(source, dict) else getattr(source, "href", None)
        if source_href:
            src_domain = urlparse(source_href).netloc.lower() or src_domain
        # O titulo do Google News vem como "Titulo - Fonte"; limpa o sufixo
        if " - " in title:
            base, _, tail = title.rpartition(" - ")
            if base and len(tail) < 60:
                title = base.strip()

    if not src_domain:
        src_domain = feed_domain
    return RawItem(
        url=link,
        title=title,
        summary=summary,
        published_at=published,
        source_domain=src_domain,
        feed_domain=feed_domain,
    )


_NS_SITEMAP = "http://www.sitemaps.org/schemas/sitemap/0.9"
_NS_NEWS = "http://www.google.com/schemas/sitemap-news/0.9"


def _fetch_sitemap(feed_url: str, feed_domain: str) -> tuple[list[RawItem], str | None]:
    """Parseia sitemap Google News (urlset com news:news). Sem summary."""
    try:
        r = requests.get(
            feed_url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/xml, text/xml, */*",
            },
            timeout=FEED_TIMEOUT,
        )
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:  # noqa: BLE001
        return [], f"{feed_domain}: {e!s}"

    items: list[RawItem] = []
    for url_el in root.findall(f"{{{_NS_SITEMAP}}}url"):
        loc_el = url_el.find(f"{{{_NS_SITEMAP}}}loc")
        if loc_el is None or not (loc_el.text or "").strip():
            continue
        link = normalize_url(loc_el.text.strip())

        news_el = url_el.find(f"{{{_NS_NEWS}}}news")
        title = ""
        published: datetime | None = None
        if news_el is not None:
            title_el = news_el.find(f"{{{_NS_NEWS}}}title")
            if title_el is not None and title_el.text:
                title = title_el.text.strip()
            date_el = news_el.find(f"{{{_NS_NEWS}}}publication_date")
            if date_el is not None and date_el.text:
                try:
                    published = date_parser.parse(date_el.text.strip())
                    if published.tzinfo is None:
                        published = published.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    published = None

        src_domain = urlparse(link).netloc.lower() or feed_domain
        items.append(
            RawItem(
                url=link,
                title=title,
                summary="",
                published_at=published,
                source_domain=src_domain,
                feed_domain=feed_domain,
            )
        )
    return items, None


def _scrape_homepage(page_url: str, feed_domain: str) -> tuple[list[RawItem], str | None]:
    """Scrapa homepage com curl_cffi e extrai links de artigos.

    Usado para sites que bloqueiam RSS mas permitem acesso via browser
    (Brasil Energia, Agencia Petrobras, etc.). Retorna itens sem titulo/summary;
    enrich_item busca cada pagina individualmente.
    """
    try:
        from clipinator import fetch_html  # type: ignore
        html = fetch_html(page_url, timeout=8)
    except Exception as e:  # noqa: BLE001
        return [], f"{feed_domain}: {e!s}"

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
    except Exception as e:  # noqa: BLE001
        return [], f"{feed_domain}: parse {e!s}"

    base = page_url.rstrip("/")
    # Coleta todos os hrefs e guarda os que parecem artigos (path com 2+ segmentos)
    seen: set[str] = set()
    items: list[RawItem] = []
    for a in soup.find_all("a", href=True):
        href: str = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        # Resolve URL relativa
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            parsed_base = urlparse(base)
            href = f"{parsed_base.scheme}://{parsed_base.netloc}{href}"
        elif not href.startswith("http"):
            continue
        # Descarta URLs de outros dominios ou de categorias/tags
        parsed = urlparse(href)
        if parsed.netloc.lower() not in (feed_domain, f"www.{feed_domain}", feed_domain.lstrip("www.")):
            continue
        path = parsed.path.rstrip("/")
        segments = [s for s in path.split("/") if s]
        if len(segments) < 1:
            continue
        # Descarta URLs de listagem/categorias: segmentos intermediarios conhecidos
        if any(seg in _NON_ARTICLE_SEGMENTS for seg in segments[:-1]):
            continue
        slug = segments[-1]
        # Slugs de artigo têm 2+ hifens; urls de navegação têm poucos
        if slug.count("-") < 2:
            continue
        link = normalize_url(href)
        if link in seen:
            continue
        seen.add(link)
        items.append(RawItem(
            url=link,
            title="",       # preenchido pelo enrich_item
            summary="",     # preenchido pelo enrich_item
            published_at=None,  # preenchido pelo enrich_item
            source_domain=parsed.netloc.lower(),
            feed_domain=feed_domain,
        ))
    return items, None


def _fetch_standard_sitemap(feed_url: str, feed_domain: str) -> tuple[list[RawItem], str | None]:
    """Parseia sitemap WordPress padrao (urlset sem news:news).

    Retorna itens com titulo/summary VAZIOS — serao preenchidos pelo enrich.
    Filtra por <lastmod> para nao enriquecer milhares de posts antigos.

    Se feed_url aponta para um sitemapindex (ex.: /wp-sitemap.xml), descobre
    automaticamente a ultima pagina de posts e usa essa.
    """
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(hours=96)
    _HDR = {"User-Agent": USER_AGENT, "Accept": "application/xml, text/xml, */*"}
    try:
        r = requests.get(feed_url, headers=_HDR, timeout=STANDARD_SITEMAP_TIMEOUT)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:  # noqa: BLE001
        return [], f"{feed_domain}: {e!s}"

    # Detecta sitemapindex: descobre automaticamente a ultima pagina de posts
    _NS_IDX = f"{{{_NS_SITEMAP}}}sitemapindex"
    if root.tag == _NS_IDX or root.tag == "sitemapindex":
        post_urls: list[str] = []
        for s in root.findall(f"{{{_NS_SITEMAP}}}sitemap"):
            loc_el = s.find(f"{{{_NS_SITEMAP}}}loc")
            if loc_el is not None and "posts-post-" in (loc_el.text or ""):
                post_urls.append(loc_el.text.strip())
        if not post_urls:
            return [], None
        # Ultima pagina tem os posts mais recentes
        try:
            r2 = requests.get(post_urls[-1], headers=_HDR, timeout=STANDARD_SITEMAP_TIMEOUT)
            r2.raise_for_status()
            root = ET.fromstring(r2.content)
        except Exception as e:  # noqa: BLE001
            return [], f"{feed_domain}: ultima pagina: {e!s}"

    # Coleta em duas passagens para poder filtrar duplicatas WordPress:
    # quando um editor republica um post, o WordPress resolve o conflito de
    # slug appendando "-2" (-3, ...). Resultado: o sitemap lista /slug/ e
    # /slug-2/ como posts distintos, com conteudo identico. Sem titulo no
    # sitemap (e comum IstoE ter fetch_html falhando no enrich), o stage 4
    # cai no fallback de slug e gera titulos "... no pr" e "... no pr 2".
    url_elements = list(root.findall(f"{{{_NS_SITEMAP}}}url"))

    # Passagem 1: coletar todas as paths para dedupe.
    all_paths: set[str] = set()
    for url_el in url_elements:
        loc_el = url_el.find(f"{{{_NS_SITEMAP}}}loc")
        if loc_el is not None and (loc_el.text or "").strip():
            try:
                all_paths.add(urlparse(loc_el.text.strip()).path.rstrip("/"))
            except ValueError:
                continue

    items: list[RawItem] = []
    for url_el in url_elements:
        loc_el = url_el.find(f"{{{_NS_SITEMAP}}}loc")
        if loc_el is None or not (loc_el.text or "").strip():
            continue
        link = normalize_url(loc_el.text.strip())

        # Dedupe WordPress: /slug-N/ e duplicata de /slug/ se ambos existem.
        # Conservador: so age se slug base tem 3+ hifens (padrao de artigo).
        path = urlparse(link).path.rstrip("/")
        m = _WP_DUP_SUFFIX_RE.search(path)
        if m:
            base_path = path[: m.start()]
            base_seg = base_path.rsplit("/", 1)[-1] if "/" in base_path else base_path
            if base_seg.count("-") >= 3 and base_path in all_paths:
                continue  # pula a variante duplicada

        lastmod_el = url_el.find(f"{{{_NS_SITEMAP}}}lastmod")
        published: datetime | None = None
        if lastmod_el is not None and lastmod_el.text:
            try:
                published = date_parser.parse(lastmod_el.text.strip())
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass

        # Descarta posts antigos para nao sobrecarregar o enriquecimento
        if published is None or published < cutoff:
            continue

        src_domain = urlparse(link).netloc.lower() or feed_domain
        items.append(RawItem(
            url=link,
            title="",       # preenchido pelo enrich_item
            summary="",     # preenchido pelo enrich_item
            published_at=published,
            source_domain=src_domain,
            feed_domain=feed_domain,
        ))
    return items, None


# "-N" no fim do path, onde N tem 1-2 digitos. Ancora na barra para evitar
# capturar substring no meio (ex.: "/em-10-anos-algo" nao casa).
_WP_DUP_SUFFIX_RE = re.compile(r"-\d{1,2}$")


def _fetch_one(feed_url: str, feed_domain: str) -> tuple[list[RawItem], str | None]:
    """Baixa e parseia um feed. Retorna (items, erro_ou_None)."""
    if is_sitemap_url(feed_url):
        return _fetch_sitemap(feed_url, feed_domain)

    # Baixa via requests (com timeout real) e passa o bytes ao feedparser.
    # feedparser.parse(url) usa urllib sem timeout - causa travas de 10-30s
    # em feeds lentos, estourando o orcamento global.
    # Google News e rate-limitado quando muitas queries disparam em paralelo.
    # Uma tentativa de retry com 1.5s de espera resolve a maioria dos casos.
    import time as _time
    is_gnews = "news.google.com" in feed_url
    for attempt in range(2 if is_gnews else 1):
        if attempt:
            _time.sleep(1.5)
        try:
            r = requests.get(
                feed_url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5",
                },
                timeout=FEED_TIMEOUT,
            )
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt == 0 and is_gnews:
                continue
            return [], f"{feed_domain}: {last_err!s}"
    else:
        return [], f"{feed_domain}: {last_err!s}"
    if r.status_code >= 400:
        return [], f"{feed_domain}: HTTP {r.status_code}"

    try:
        parsed = feedparser.parse(r.content)
    except Exception as e:  # noqa: BLE001
        return [], f"{feed_domain}: {e!s}"

    bozo = getattr(parsed, "bozo", 0)
    entries = parsed.get("entries") or []
    if not entries:
        if bozo:
            exc = parsed.get("bozo_exception")
            return [], f"{feed_domain}: {exc}"
        return [], None

    items: list[RawItem] = []
    for e in entries:
        it = _entry_to_item(e, feed_domain)
        if it:
            items.append(it)
    # Erros cosmeticos (bozo=1 mas entries vieram) nao sao reportados.
    return items, None


def iter_collect(
    keywords: list[str],
    hours: int,
    *,
    max_workers: int = 48,
    include_google_news: bool = True,
    deadline: float | None = None,
):
    """Versao streaming de collect: yield (dom, items, err) a medida que feeds
    terminam. Permite overlap com enriquecimento.
    """
    tasks: list[tuple[str, str]] = list(all_rss_feeds())
    # Sitemaps WordPress padrao (istoedinheiro, visaoagro, etc.)
    std_tasks: list[tuple[str, str]] = list(all_standard_sitemaps())
    if include_google_news:
        if NO_RSS_DOMAINS:
            for url in google_news_site_queries(NO_RSS_DOMAINS, keywords, hours):
                tasks.append(("news.google.com", url))
        if ENGLISH_NO_RSS_DOMAINS:
            # Usa lista curta de termos em inglês para não ultrapassar limite de URL do Google News.
            for url in google_news_site_queries_en(ENGLISH_NO_RSS_DOMAINS, ENGLISH_SITE_KEYWORDS, hours):
                tasks.append(("news.google.com", url))

    import time as _time
    t_start = _time.time()
    dl = deadline if deadline is not None else COLLECT_DEADLINE

    # Agrega todos os tipos de fonte: RSS/sitemaps, sitemaps padrao, homepage scrapers
    # Cada tipo tem seu fetcher mas todos compartilham o mesmo deadline global.
    home_tasks: list[tuple[str, str]] = list(all_homepage_scrapers())

    def _make_fetcher(is_std: bool, is_home: bool):
        if is_home:
            return _scrape_homepage
        if is_std:
            return _fetch_standard_sitemap
        return _fetch_one

    all_tasks = (
        [(dom, url, False, False) for dom, url in tasks] +
        [(dom, url, True, False) for dom, url in std_tasks] +
        [(dom, url, False, True) for dom, url in home_tasks]
    )

    ex = ThreadPoolExecutor(max_workers=max_workers)
    futs = {
        ex.submit(_make_fetcher(is_std, is_home), url, dom): (dom, url)
        for dom, url, is_std, is_home in all_tasks
    }
    try:
        for fut in as_completed(futs.keys(), timeout=dl):
            dom, _url = futs[fut]
            try:
                got, err = fut.result()
            except Exception as e:  # noqa: BLE001
                yield dom, [], f"{dom}: {e!s}"
                continue
            yield dom, got, err
            if _time.time() - t_start >= dl:
                break
    except (FuturesTimeout, TimeoutError):
        pass
    finally:
        for fut in futs:
            if not fut.done():
                fut.cancel()
        ex.shutdown(wait=False, cancel_futures=True)


def collect(
    keywords: list[str],
    hours: int,
    *,
    max_workers: int = 48,
    include_google_news: bool = True,
) -> tuple[list[RawItem], list[str]]:
    """Busca todos os feeds registrados + Google News. Retorna (itens, erros).

    A filtragem por keyword e data nao e feita aqui - cabe ao chamador.
    Mas ja passamos keywords para construir queries do Google News.

    As queries GERAIS do Google News (por keyword em qualquer site) sao
    pesadas: trazem centenas de URLs com wrapper que nao tem snippet sem
    decode. Por padrao usamos apenas queries 'site:' para dominios sem
    RSS/sitemap proprio. Isso mantem o orcamento <10s.
    """
    tasks: list[tuple[str, str]] = list(all_rss_feeds())

    if include_google_news:
        if NO_RSS_DOMAINS:
            for url in google_news_site_queries(NO_RSS_DOMAINS, keywords, hours):
                tasks.append(("news.google.com", url))
        if ENGLISH_NO_RSS_DOMAINS:
            for url in google_news_site_queries_en(ENGLISH_NO_RSS_DOMAINS, ENGLISH_SITE_KEYWORDS, hours):
                tasks.append(("news.google.com", url))

    items: list[RawItem] = []
    errors: list[str] = []

    # Deadline global duro (~6s): o que nao chegou a tempo fica para a proxima
    # busca. Isso mantem a latencia de /search bounded sem depender da boa
    # vontade de cada feed.
    ex = ThreadPoolExecutor(max_workers=max_workers)
    futs = {ex.submit(_fetch_one, url, dom): (dom, url) for dom, url in tasks}
    try:
        done, not_done = wait(futs.keys(), timeout=COLLECT_DEADLINE)
        for fut in done:
            dom, url = futs[fut]
            try:
                got, err = fut.result()
            except Exception as e:  # noqa: BLE001
                errors.append(f"{dom}: {e!s}")
                continue
            if err:
                errors.append(err)
            items.extend(got)
        for fut in not_done:
            dom, _ = futs[fut]
            fut.cancel()
            errors.append(f"{dom}: excedeu deadline de {COLLECT_DEADLINE}s")
    finally:
        # Nao bloqueia no shutdown - futures pendentes continuam rodando
        # mas nao bloqueiam o retorno de collect().
        ex.shutdown(wait=False, cancel_futures=True)

    # Dedupe por URL normalizada (mantem o primeiro com data nao-nula quando possivel)
    by_url: dict[str, RawItem] = {}
    for it in items:
        cur = by_url.get(it.url)
        if cur is None:
            by_url[it.url] = it
        elif cur.published_at is None and it.published_at is not None:
            by_url[it.url] = it

    return list(by_url.values()), errors
