"""Clipinator - IBBA Oil & Gas News.

Le um Excel com URLs de noticias, extrai titulo + corpo de cada uma
e gera um .eml pronto para abrir no Outlook e enviar.

Uso:
    python clipinator.py links.xlsx
    python clipinator.py links.xlsx --data 2026-04-22
    python clipinator.py links.xlsx --out pasta_saida

Paywall (Valor, Brasil Energia): rode login.py uma vez para exportar cookies
da sua sessao logada no Chrome. O scraper carrega cookies/<dominio>.txt automaticamente.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from email.message import EmailMessage
from html import escape
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup, Tag
from curl_cffi import requests as cffi_requests

BASE_DIR = Path(__file__).parent
COOKIES_DIR = BASE_DIR / "cookies"

# =============================================================================
# Fontes (dominio -> nome legivel)
# =============================================================================

SOURCE_NAMES: dict[str, str] = {
    "valor.globo.com": "Valor Econômico",
    "www.estadao.com.br": "Estadão",
    "estadao.com.br": "Estadão",
    "www1.folha.uol.com.br": "Folha de S. Paulo",
    "folha.uol.com.br": "Folha de S. Paulo",
    "brasilenergia.com.br": "Brasil Energia",
    "www.brasilenergia.com.br": "Brasil Energia",
    "www.metropoles.com": "Metrópoles",
    "metropoles.com": "Metrópoles",
    "www.poder360.com.br": "Poder360",
    "poder360.com.br": "Poder360",
    "www.infomoney.com.br": "InfoMoney",
    "infomoney.com.br": "InfoMoney",
    "www.bloomberglinea.com.br": "Bloomberg Línea",
    "bloomberglinea.com.br": "Bloomberg Línea",
    "noticias.r7.com": "R7",
    "g1.globo.com": "G1",
    "oglobo.globo.com": "O Globo",
    "agencia.petrobras.com.br": "Agência Petrobras",
    "agenciainfra.com": "Agência iNFRA",
    "www.agenciainfra.com": "Agência iNFRA",
    "braziljournal.com": "Brazil Journal",
    "www.braziljournal.com": "Brazil Journal",
    "eixos.com.br": "eixos",
    "www.eixos.com.br": "eixos",
    "monitormercantil.com.br": "Monitor Mercantil",
    "www.monitormercantil.com.br": "Monitor Mercantil",
    "timesbrasil.com.br": "Times Brasil",
    "www.timesbrasil.com.br": "Times Brasil",
    "visaoagro.com.br": "Visão Agro",
    "www.visaoagro.com.br": "Visão Agro",
    "www.theagribiz.com": "Agribiz",
    "theagribiz.com": "Agribiz",
    # Folha / Estadao / Globo - subdominios
    "aovivo.folha.uol.com.br": "Folha de S. Paulo",
    "estradao.estadao.com.br": "Estradão",
    "pipelinevalor.globo.com": "Pipeline (Valor)",
    "globorural.globo.com": "Globo Rural",
    "cbn.globo.com": "CBN",
    # Novos
    "www.cnnbrasil.com.br": "CNN Brasil",
    "cnnbrasil.com.br": "CNN Brasil",
    "veja.abril.com.br": "Veja",
    "investnews.com.br": "InvestNews",
    "www.investnews.com.br": "InvestNews",
    "neofeed.com.br": "NeoFeed",
    "www.neofeed.com.br": "NeoFeed",
    "www.cnbc.com": "CNBC",
    "exame.com": "Exame",
    "www.exame.com": "Exame",
    "istoedinheiro.com.br": "IstoÉ Dinheiro",
    "www.istoedinheiro.com.br": "IstoÉ Dinheiro",
    "www.brasil247.com": "Brasil 247",
    "brasil247.com": "Brasil 247",
    "observatorio.firjan.com.br": "Observatório Firjan",
    "megawhat.uol.com.br": "MegaWhat",
    "www.reuters.com": "Reuters",
    "reuters.com": "Reuters",
    "br.investing.com": "Investing.com",
    "www.correiobraziliense.com.br": "Correio Braziliense",
    "correiobraziliense.com.br": "Correio Braziliense",
    "veronoticias.com": "Vero Notícias",
    "www.veronoticias.com": "Vero Notícias",
    "diariodopoder.com.br": "Diário do Poder",
    "www.diariodopoder.com.br": "Diário do Poder",
    "www.conjur.com.br": "Conjur",
    "conjur.com.br": "Conjur",
    "www.argusmedia.com": "Argus Media",
    "argusmedia.com": "Argus Media",
    "operamundi.uol.com.br": "Opera Mundi",
    "claudiodantas.com.br": "Cláudio Dantas",
    "www.claudiodantas.com.br": "Cláudio Dantas",
    "br.tradingview.com": "TradingView",
    "www.theedgesingapore.com": "The Edge Singapore",
    "www12.senado.leg.br": "Senado Federal",
    "edition.cnn.com": "CNN",
    "www.cnn.com": "CNN",
    "clickpetroleoegas.com.br": "Click Petróleo e Gás",
    "www.clickpetroleoegas.com.br": "Click Petróleo e Gás",
    "ineep.org.br": "INEEP",
    "www.ineep.org.br": "INEEP",
    "tconline.com.br": "TC Online",
    "www.tconline.com.br": "TC Online",
    "obastidor.com.br": "O Bastidor",
    "www.obastidor.com.br": "O Bastidor",
    "noticias.uol.com.br": "UOL",
    "www.terra.com.br": "Terra",
    "terra.com.br": "Terra",
    "www.moneytimes.com.br": "Money Times",
    "moneytimes.com.br": "Money Times",
    "visnoinvest.com.br": "Visno Invest",
    "www.visnoinvest.com.br": "Visno Invest",
    # Newshunter sources
    "www.fastmarkets.com": "Fastmarkets",
    "fastmarkets.com": "Fastmarkets",
    "dashboard.fastmarkets.com": "Fastmarkets",
    "www.spglobal.com": "Platts",
    "core.spglobal.com": "Platts",
    "spglobal.com": "Platts",
    "www.mining.com": "Mining.com",
    "mining.com": "Mining.com",
    "www.elfinanciero.com.mx": "El Financiero",
    "elfinanciero.com.mx": "El Financiero",
}


# =============================================================================
# Extractors por dominio
# =============================================================================

Extractor = Callable[[BeautifulSoup], tuple[str, list[str]]]

NOISE_CLASS_SUBSTRINGS = (
    "advertisement", "publicidade", "newsletter", "related", "relacionad",
    "leia-tambem", "leia-mais", "recomend", "share-", "social-share",
    "tags-list", "author-box", "byline", "sponsor", "subscribe",
    "breadcrumb", "comments", "content-ads", "tag-manager-publicidade",
    "read-more", "mc-read-more", "recommend-theme",
    "box-seja-assinante", "seja-assinante", "assine-", "paywall-wrap",
    "subscription", "premium-content-wall",
)


def _strip_noise(container: Tag) -> None:
    for tag in container.find_all(["figure", "figcaption", "aside", "script", "style", "iframe", "form", "nav"]):
        tag.decompose()
    for el in list(container.find_all(True)):
        if el.attrs is None or el.parent is None:
            continue
        classes = el.get("class") or []
        idv = el.get("id") or ""
        combined = " ".join(list(classes) + [idv]).lower()
        if any(sub in combined for sub in NOISE_CLASS_SUBSTRINGS):
            el.decompose()


def _title_from_meta(soup: BeautifulSoup) -> str:
    for sel in [
        ("meta", {"property": "og:title"}),
        ("meta", {"name": "twitter:title"}),
        ("meta", {"itemprop": "headline"}),
    ]:
        tag = soup.find(*sel)
        if tag and tag.get("content"):
            return tag["content"].strip()
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(" ", strip=True)
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return ""


def _paragraphs_from(container: Tag | None) -> list[str]:
    if container is None:
        return []
    _strip_noise(container)
    paragraphs: list[str] = []
    for p in container.find_all("p"):
        if p.find_all(recursive=False) and all(child.name == "a" for child in p.find_all(recursive=False)):
            text_only = p.get_text(" ", strip=True)
            link_text = " ".join(a.get_text(" ", strip=True) for a in p.find_all("a"))
            if text_only == link_text:
                continue
        txt = p.get_text(" ", strip=True)
        if txt:
            paragraphs.append(txt)
    return paragraphs


def _first_matching(soup: BeautifulSoup, selectors: list[str]) -> Tag | None:
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            return el
    return None


def _generic(soup: BeautifulSoup, selectors: list[str]) -> tuple[str, list[str]]:
    title = _title_from_meta(soup)
    container = _first_matching(soup, selectors) or soup.find("article")
    return title, _paragraphs_from(container)


def _make_extractor(selectors: list[str]) -> Extractor:
    return lambda soup: _generic(soup, selectors)


ex_globo = _make_extractor([
    "div.mc-article-body", "article .mc-article-body",
    "article .content-text__container", "article",
])
ex_folha = _make_extractor([
    "div.c-news__body", "article.c-news", "div.c-main-content", "article",
])
ex_estadao = _make_extractor([
    "div.content-wrapper.news-body", "div.news-body",
    "div.template-reportagem",
    "div.n--noticia__content", "div.noticia__conteudo",
    "section.n--noticia__content", "article",
])
ex_brasilenergia = _make_extractor([
    "div.editorial_", "div.descricao-noticia",
    "div.single-content", "div.entry-content", "article",
])
ex_metropoles = _make_extractor([
    "div.m-news__body", "div.texto-materia",
    "article .noticia-conteudo", "article",
])
ex_poder360 = _make_extractor(["div.entry-content", "article .post-content", "article"])
ex_infomoney = _make_extractor([
    "div.article__content", "div.single__content", "div.im-article", "article",
])
ex_bloomberglinea = _make_extractor(["div.article-content", "div.article-body", "article"])
ex_r7 = _make_extractor(["div.b-article__body", "div.article-content", "article"])
ex_agencia_petrobras = _make_extractor(["div.entry-content", "article .post-content", "article"])
ex_agenciainfra = _make_extractor(["div.entry-content", "article .post-content", "article"])
ex_braziljournal = _make_extractor([
    "div.post-content-text", "section.post-content", "div.entry-content", "article",
])
ex_eixos = _make_extractor([
    "div.entry-content", "div.post-content", "article .tdb-block-inner", "article",
])
ex_monitormercantil = _make_extractor(["div.td-post-content", "div.entry-content", "article"])
ex_timesbrasil = _make_extractor(["div.article-content", "div.entry-content", "article"])
ex_visaoagro = _make_extractor(["div.entry-content", "div.post-content", "article"])

# Fallback generico amplo para sites ainda sem seletor dedicado.
ex_auto = _make_extractor([
    'div[itemprop="articleBody"]',
    "div.article-content", "div.article-body", "div.article__content",
    "div.article__body", "div.post-content", "div.post__content",
    "div.post-body", "div.entry-content", "div.entry__content",
    "div.single-content", "div.single__content", "div.content-text",
    "div.news-text", "div.news-content", "div.news__body",
    "div.materia-conteudo", "div.conteudo-materia", "div.texto-materia",
    "div.texto", "div.content", "div.body", "div.main-content",
    "section.article-body", "section.content", "main article",
    "article .content", "article",
])


EXTRACTORS: dict[str, Extractor] = {
    "valor.globo.com": ex_globo,
    "oglobo.globo.com": ex_globo,
    "g1.globo.com": ex_globo,
    "pipelinevalor.globo.com": ex_globo,
    "globorural.globo.com": ex_globo,
    "cbn.globo.com": ex_globo,
    "www1.folha.uol.com.br": ex_folha,
    "folha.uol.com.br": ex_folha,
    "aovivo.folha.uol.com.br": ex_folha,
    "www.estadao.com.br": ex_estadao,
    "estadao.com.br": ex_estadao,
    "estradao.estadao.com.br": ex_estadao,
    "brasilenergia.com.br": ex_brasilenergia,
    "www.brasilenergia.com.br": ex_brasilenergia,
    "www.metropoles.com": ex_metropoles,
    "metropoles.com": ex_metropoles,
    "www.poder360.com.br": ex_poder360,
    "poder360.com.br": ex_poder360,
    "www.infomoney.com.br": ex_infomoney,
    "infomoney.com.br": ex_infomoney,
    "www.bloomberglinea.com.br": ex_bloomberglinea,
    "bloomberglinea.com.br": ex_bloomberglinea,
    "noticias.r7.com": ex_r7,
    "agencia.petrobras.com.br": ex_agencia_petrobras,
    "agenciainfra.com": ex_agenciainfra,
    "www.agenciainfra.com": ex_agenciainfra,
    "braziljournal.com": ex_braziljournal,
    "www.braziljournal.com": ex_braziljournal,
    "eixos.com.br": ex_eixos,
    "www.eixos.com.br": ex_eixos,
    "monitormercantil.com.br": ex_monitormercantil,
    "www.monitormercantil.com.br": ex_monitormercantil,
    "timesbrasil.com.br": ex_timesbrasil,
    "www.timesbrasil.com.br": ex_timesbrasil,
    "visaoagro.com.br": ex_visaoagro,
    "www.visaoagro.com.br": ex_visaoagro,
    # Generico amplo (ex_auto)
    "theagribiz.com": ex_auto,
    "www.theagribiz.com": ex_auto,
    "www.cnnbrasil.com.br": ex_auto,
    "cnnbrasil.com.br": ex_auto,
    "veja.abril.com.br": ex_auto,
    "investnews.com.br": ex_auto,
    "www.investnews.com.br": ex_auto,
    "neofeed.com.br": ex_auto,
    "www.neofeed.com.br": ex_auto,
    "www.cnbc.com": ex_auto,
    "exame.com": ex_auto,
    "www.exame.com": ex_auto,
    "istoedinheiro.com.br": ex_auto,
    "www.istoedinheiro.com.br": ex_auto,
    "www.brasil247.com": ex_auto,
    "brasil247.com": ex_auto,
    "observatorio.firjan.com.br": ex_auto,
    "megawhat.uol.com.br": ex_auto,
    "www.reuters.com": ex_auto,
    "reuters.com": ex_auto,
    "br.investing.com": ex_auto,
    "www.correiobraziliense.com.br": ex_auto,
    "correiobraziliense.com.br": ex_auto,
    "veronoticias.com": ex_auto,
    "www.veronoticias.com": ex_auto,
    "diariodopoder.com.br": ex_auto,
    "www.diariodopoder.com.br": ex_auto,
    "www.conjur.com.br": ex_auto,
    "conjur.com.br": ex_auto,
    "www.argusmedia.com": ex_auto,
    "argusmedia.com": ex_auto,
    "operamundi.uol.com.br": ex_auto,
    "claudiodantas.com.br": ex_auto,
    "www.claudiodantas.com.br": ex_auto,
    "br.tradingview.com": ex_auto,
    "www.theedgesingapore.com": ex_auto,
    "www12.senado.leg.br": ex_auto,
    "edition.cnn.com": ex_auto,
    "www.cnn.com": ex_auto,
    "clickpetroleoegas.com.br": ex_auto,
    "www.clickpetroleoegas.com.br": ex_auto,
    "ineep.org.br": ex_auto,
    "www.ineep.org.br": ex_auto,
    "tconline.com.br": ex_auto,
    "www.tconline.com.br": ex_auto,
    "obastidor.com.br": ex_auto,
    "www.obastidor.com.br": ex_auto,
    "noticias.uol.com.br": ex_auto,
    "www.terra.com.br": ex_auto,
    "terra.com.br": ex_auto,
    "www.moneytimes.com.br": ex_auto,
    "moneytimes.com.br": ex_auto,
    "visnoinvest.com.br": ex_auto,
    "www.visnoinvest.com.br": ex_auto,
}


# =============================================================================
# Scraper
# =============================================================================

IMPERSONATE_DOMAINS = {
    "brasilenergia.com.br", "www.brasilenergia.com.br",
    # S&P Global / Platts — SPA com autenticação; cffi envia todos os cookies do jar
    "core.spglobal.com", "www.spglobal.com",
    # Fastmarkets — idem
    "www.fastmarkets.com", "dashboard.fastmarkets.com",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Referer": "https://www.google.com/",
}

PAYWALL_MARKERS = (
    "ja e assinante", "já é assinante",
    "faca seu login", "faça seu login",
    "continue lendo",
    "nosso conteudo e exclusivo", "nosso conteúdo é exclusivo",
    "conteudo exclusivo para assinantes", "conteúdo exclusivo para assinantes",
    "assine ja", "assine já", "assine agora",
    "voce atingiu", "você atingiu o limite",
    "matéria exclusiva", "materia exclusiva",
    "tres materias por mes", "três matérias por mês",
    "apoie o jornalismo", "acesse sem limites",
    "acompanhe os mercados com nossas ferramentas",
    "conteudo premium", "conteúdo premium",
)


@dataclass
class NewsItem:
    url: str
    domain: str
    fonte: str
    titulo: str
    paragrafos: list[str]


def get_domain(url: str) -> str:
    return urlparse(url).netloc.lower()


def _find_cookie_file(domain: str) -> Path | None:
    if not COOKIES_DIR.exists():
        return None
    candidates = [domain]
    parts = domain.split(".")
    for i in range(1, len(parts) - 1):
        candidates.append(".".join(parts[i:]))
    for cand in candidates:
        p = COOKIES_DIR / f"{cand}.txt"
        if p.exists():
            return p
    return None


def _load_cookies(domain: str) -> http.cookiejar.CookieJar | None:
    path = _find_cookie_file(domain)
    if path is None:
        return None
    jar = http.cookiejar.MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except http.cookiejar.LoadError:
        content = path.read_text(encoding="utf-8")
        if not content.startswith("# Netscape HTTP Cookie File"):
            tmp = path.with_suffix(".fixed.txt")
            tmp.write_text("# Netscape HTTP Cookie File\n" + content, encoding="utf-8", newline="\n")
            jar = http.cookiejar.MozillaCookieJar(str(tmp))
            jar.load(ignore_discard=True, ignore_expires=True)
        else:
            raise
    return jar


def _cookies_to_dict(jar: http.cookiejar.CookieJar | None) -> dict:
    if jar is None:
        return {}
    return {c.name: c.value for c in jar}


def fetch_html(url: str, timeout: int = 25) -> str:
    domain = get_domain(url)
    jar = _load_cookies(domain)
    if domain in IMPERSONATE_DOMAINS:
        resp = cffi_requests.get(
            url, headers=DEFAULT_HEADERS, timeout=timeout, impersonate="chrome124",
            cookies=_cookies_to_dict(jar),
        )
        resp.raise_for_status()
        return resp.text
    resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout, cookies=jar)
    if resp.status_code == 403:
        resp = cffi_requests.get(
            url, headers=DEFAULT_HEADERS, timeout=timeout, impersonate="chrome124",
            cookies=_cookies_to_dict(jar),
        )
    resp.raise_for_status()
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def fetch_from_wayback(url: str, timeout: int = 25) -> str | None:
    try:
        lookup = requests.get(
            "https://archive.org/wayback/available",
            params={"url": url},
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        )
        lookup.raise_for_status()
        data = lookup.json()
        snap = (data.get("archived_snapshots") or {}).get("closest") or {}
        if not snap.get("available") or not snap.get("url"):
            return None
        snap_url = snap["url"]
        if "id_" not in snap_url:
            snap_url = snap_url + "id_"
        resp = requests.get(snap_url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        resp.raise_for_status()
        if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text
    except Exception:
        return None


_SITE_SUFFIX_PATTERNS = [
    re.compile(r"\s*[\|\u2013\-]\s*" + re.escape(name) + r"\s*$", re.IGNORECASE)
    for name in set(SOURCE_NAMES.values()) | {
        "Valor", "Valor Economico", "Folha", "Estadao", "O Estado de S.Paulo",
        "Brasil Energia - Petróleo e Gás", "oglobo", "Agencia Petrobras",
        "Agencia iNFRA", "Metropoles", "Poder 360", "Visao Agro", "Bloomberg Linea",
    }
]


def clean_title(titulo: str) -> str:
    t = re.sub(r"\s+", " ", titulo).strip()
    changed = True
    while changed:
        changed = False
        for pat in _SITE_SUFFIX_PATTERNS:
            new = pat.sub("", t).strip()
            if new != t and new:
                t = new
                changed = True
    return t


_NOISE_PATTERNS = [
    r"^\s*leia\s+(tamb[eé]m|mais|tudo\s+sobre)\b",
    r"^\s*leia\s+a\s+(reportagem|mat[eé]ria)\s+completa\b",
    r"^\s*continua\s+(ap[oó]s|depois)\s+(a|da)\s+publicidade",
    r"^\s*assine\b",
    r"^\s*assinar\b",
    r"^\s*publicidade\s*$",
    r"^\s*propaganda\s*$",
    r"^\s*anuncio\s*$",
    r"^\s*newsletter\b",
    r"^\s*siga\s+o\s+",
    r"^\s*siga\s+a\s+",
    r"^\s*assista\b",
    r"^\s*foto:\s",
    r"^\s*imagem:\s",
    r"^\s*cr[eé]dito:\s",
    r"^\s*compartilhe\b",
    r"^\s*veja\s+(tamb[eé]m|mais)\b",
    r"^\s*saiba\s+mais\b",
    r"^\s*por\s+[A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wÀ-ÿ\.\-]+(\s+[A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wÀ-ÿ\.\-]+){0,3}\s*$",
    r"j[aá]\s+[eé]\s+assinante\b",
    r"fa[cç]a\s+seu\s+login\b",
    r"continue\s+lendo\b",
    r"nosso\s+conte[uú]do\s+[eé]\s+exclusivo",
    r"conte[uú]do\s+exclusivo\s+para\s+assinantes",
    r"voc[eê]\s+atingiu\s+o\s+limite",
    r"tr[eê]s\s+mat[eé]rias\s+por\s+m[eê]s",
    r"apoie\s+o\s+jornalismo",
    r"acesse\s+sem\s+limites",
    r"acompanhe\s+os\s+mercados\s+com\s+nossas\s+ferramentas",
    r"tenha\s+acesso\s+a\s+informa[cç][aã]o\s+relevante",
    r"voc[eê]\s+pode\s+ler\s+nosso\s+conte[uú]do\s+exclusivo",
    r"cadastro\s+gratuito",
    r"assine\s+as?\s+newsletters?\b",
    r"receba\s+as?\s+not[ií]cias\s+do\s+dia",
    r"em\s+primeira\s+m[aã]o\s+no\s+e-?mail",
    r"^\s*[\u27f6\u2192\u2794\u279c\u25ba\u25b8\u2023\u00bb]\s*",  # linha comecando com seta
    r"^\s*[\u00a9\u00ae]?\s*\d{4}\s+bloomberg\b",  # copyright "©2026 Bloomberg L.P."
    r"^\s*todos\s+os\s+direitos\s+reservados",
    # breadcrumb tipo "PetróleoHoje | Exploração e Produção" (curto, com |, sem pontuacao final)
    r"^\s*[\wÀ-ÿ][\wÀ-ÿ\s&'-]{1,40}\s*\|\s*[\wÀ-ÿ][\wÀ-ÿ\s&'-]{1,40}\s*$",
]
_NOISE_REGEX = re.compile("|".join(_NOISE_PATTERNS), re.IGNORECASE)


def clean_paragraphs(paragraphs: list[str]) -> list[str]:
    out: list[str] = []
    for p in paragraphs:
        p = re.sub(r"\s+", " ", p).strip()
        p = re.sub(r"\s+([.,;:!?])", r"\1", p)
        if not p:
            continue
        if _NOISE_REGEX.search(p):
            continue
        out.append(p)
    dedup: list[str] = []
    for p in out:
        if not dedup or dedup[-1] != p:
            dedup.append(p)
    return dedup


def looks_paywalled(paragrafos: list[str]) -> bool:
    if len(paragrafos) < 3:
        return True
    joined = " ".join(paragrafos).lower()
    if len(joined) < 400:
        return True
    return any(marker in joined for marker in PAYWALL_MARKERS)


def _extract(html: str, domain: str) -> tuple[str, list[str]]:
    soup = BeautifulSoup(html, "lxml")
    extractor = EXTRACTORS[domain]
    titulo, paragrafos = extractor(soup)
    return clean_title(titulo), clean_paragraphs(paragrafos)


def scrape(url: str, manual_body: str | None = None) -> NewsItem:
    domain = get_domain(url)
    fonte = SOURCE_NAMES.get(domain)
    if fonte is None or domain not in EXTRACTORS:
        raise ValueError(f"Dominio nao cadastrado: {domain} (url: {url})")

    html = fetch_html(url)
    titulo, paragrafos = _extract(html, domain)

    if manual_body:
        chunks = re.split(r"\n\s*\n+", manual_body.strip())
        if len(chunks) == 1:
            chunks = [c for c in manual_body.strip().split("\n") if c.strip()]
        paragrafos = clean_paragraphs([c.strip() for c in chunks if c.strip()])
    else:
        if looks_paywalled(paragrafos):
            wb_html = fetch_from_wayback(url)
            if wb_html:
                _, wb_paragrafos = _extract(wb_html, domain)
                if not looks_paywalled(wb_paragrafos) and len(wb_paragrafos) > len(paragrafos):
                    paragrafos = wb_paragrafos

    if not titulo:
        raise ValueError(f"Titulo nao encontrado em {url}")
    if not paragrafos:
        raise ValueError(f"Corpo vazio em {url}")
    if manual_body is None and looks_paywalled(paragrafos):
        raise ValueError(
            f"Conteudo parece estar por tras de paywall em {url} "
            f"(preencha a coluna 'corpo' no Excel com o texto da noticia)"
        )

    return NewsItem(url=url, domain=domain, fonte=fonte, titulo=titulo, paragrafos=paragrafos)


# =============================================================================
# Email builder (HTML + .eml)
# =============================================================================

MONTH_EN = {
    1: "January", 2: "February", 3: "March", 4: "April", 5: "May", 6: "June",
    7: "July", 8: "August", 9: "September", 10: "October", 11: "November", 12: "December",
}

STYLE_BLOCK = (
    "<style>"
    'p.MsoNormal,li.MsoNormal,div.MsoNormal{margin:0;font-size:11.0pt;font-family:"Calibri",sans-serif;}'
    "a:link{color:#0563C1;text-decoration:underline;}"
    "a:visited{color:#954F72;text-decoration:underline;}"
    "</style>"
)
# Estilos inline em cada <p> garantem margin:0 mesmo quando o <style> do <head>
# é descartado pelo cliente de email ao responder/encaminhar a mensagem.
_P_BASE = "margin:0;font-size:11.0pt;font-family:'Calibri',sans-serif"
_P     = f'<p class="MsoNormal" style="{_P_BASE}">'
_P_CTR = f'<p class="MsoNormal" style="{_P_BASE};text-align:center">'
_P_JUS = f'<p class="MsoNormal" style="{_P_BASE};text-align:justify">'

BLANK = f'{_P}&nbsp;</p>'
TEAM_BLOCK = (
    f'{_P}<b><span style="color:#FF5000">Ita&uacute; BBA Oil &amp; Gas Team</span></b></p>'
    f'{_P_JUS}<b><span style="font-size:10.0pt;color:#000512">Monique Greco Natal /</span></b>&nbsp;'
    '<a href="mailto:monique.greco@itaubba.com"><span style="font-size:10.0pt">monique.greco@itaubba.com</span></a>'
    '</p>'
    f'{_P_JUS}<b><span style="font-size:10.0pt;color:#000512">Eric de Mello /</span></b>&nbsp;'
    '<a href="mailto:eric.mello@itaubba.com"><span style="font-size:10.0pt">eric.mello@itaubba.com</span></a>'
    '</p>'
    f'{_P_JUS}<b><span style="font-size:10.0pt;color:#000512">Eduardo Mendes /</span></b>&nbsp;'
    '<a href="mailto:eduardo.mendes@itaubba.com"><span style="font-size:10.0pt">eduardo.mendes@itaubba.com</span></a>'
    '</p>'
)


def _esc(s: str) -> str:
    return escape(s, quote=False)


def format_date_header(d: date) -> str:
    return f"{d.day:02d} {MONTH_EN[d.month]} {d.year}"


def build_html(items: list[NewsItem], d: date) -> str:
    date_text = format_date_header(d)

    bullets_html = "".join(
        f'<li class="MsoNormal"><b>{_esc(item.titulo)} ({_esc(item.fonte)})</b></li>'
        for item in items
    )
    index_block = f'<ul type="disc">{bullets_html}</ul>'

    sections: list[str] = []
    for item in items:
        title_html = (
            f'{_P}<b><span style="font-size:14.0pt">{_esc(item.titulo)} ({_esc(item.fonte)})</span></b></p>'
        )
        body_parts = [f'{_P}{_esc(par)}</p>' for par in item.paragrafos]
        body_html = BLANK.join(body_parts)
        source_html = (
            f'{_P}<span style="color:black">Fonte:</span> '
            f'<a href="{_esc(item.url)}">{_esc(item.url)}</a>'
            "</p>"
        )
        sections.append(title_html + BLANK + body_html + BLANK + source_html + BLANK)

    header_html = (
        f'<p class="MsoNormal" align="center" style="{_P_BASE};text-align:center">'
        '<b><span style="font-size:18.0pt;color:#FF5000">'
        f"*** IBBA Oil &amp; Gas News \u2013 {_esc(date_text)} ***"
        "</span></b></p>"
    )
    subheader_html = (
        f'{_P}<b><span style="font-size:14.0pt;color:#FF5000">Main Headlines</span></b></p>'
    )

    return (
        '<html><head><meta charset="utf-8">'
        f"{STYLE_BLOCK}"
        "</head>"
        '<body lang="EN-US" link="#0563C1" vlink="#954F72" style="word-wrap:break-word">'
        '<div class="WordSection1">'
        f"{header_html}{BLANK}{subheader_html}{index_block}{BLANK}{TEAM_BLOCK}{BLANK}"
        f"{''.join(sections)}"
        "</div></body></html>"
    )


def build_plain_text(items: list[NewsItem], d: date) -> str:
    lines = [f"*** IBBA Oil & Gas News - {format_date_header(d)} ***", "", "Main Headlines"]
    for it in items:
        lines.append(f"  - {it.titulo} ({it.fonte})")
    lines.append("")
    for it in items:
        lines.append(f"{it.titulo} ({it.fonte})")
        lines.append("")
        for p in it.paragrafos:
            lines.append(p)
            lines.append("")
        lines.append(f"Fonte: {it.url}")
        lines.append("")
    return "\n".join(lines)


def build_eml(items: list[NewsItem], d: date, out_dir: Path) -> Path:
    msg = EmailMessage()
    msg["Subject"] = f"*** IBBA Oil & Gas News \u2013 {format_date_header(d)} ***"
    msg["From"] = ""
    msg["To"] = ""
    msg.set_content(build_plain_text(items, d), charset="utf-8")
    msg.add_alternative(build_html(items, d), subtype="html")

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"ibba_oil_gas_news_{d.strftime('%Y-%m-%d')}.eml"
    out_path.write_bytes(bytes(msg))
    return out_path


# =============================================================================
# CLI
# =============================================================================

def read_input(xlsx_path: Path) -> list[tuple[str, str | None]]:
    df = pd.read_excel(xlsx_path)
    df.columns = [str(c).strip().lower() for c in df.columns]
    url_col = "url" if "url" in df.columns else df.columns[0]
    body_col = next((c for c in ("corpo", "body", "texto") if c in df.columns), None)

    rows: list[tuple[str, str | None]] = []
    for _, row in df.iterrows():
        u = row[url_col]
        if pd.isna(u):
            continue
        s = str(u).strip()
        if not s.startswith("http"):
            continue
        body = None
        if body_col is not None:
            b = row[body_col]
            if not pd.isna(b) and str(b).strip():
                body = str(b)
        rows.append((s, body))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Gera .eml IBBA Oil & Gas News a partir de Excel com URLs.")
    parser.add_argument("xlsx", type=Path, help="Caminho do Excel com coluna 'url'")
    parser.add_argument("--data", type=str, default=None, help="Data do cabecalho (YYYY-MM-DD). Default: hoje.")
    parser.add_argument("--out", type=Path, default=Path("out"), help="Pasta de saida (default: ./out)")
    args = parser.parse_args()

    if not args.xlsx.exists():
        print(f"ERRO: arquivo nao encontrado: {args.xlsx}", file=sys.stderr)
        return 2

    d = datetime.strptime(args.data, "%Y-%m-%d").date() if args.data else date.today()

    rows = read_input(args.xlsx)
    if not rows:
        print("ERRO: nenhuma URL valida encontrada no Excel.", file=sys.stderr)
        return 2

    print(f"Lidas {len(rows)} URL(s). Extraindo noticias...")

    items: list[NewsItem] = []
    erros: list[tuple[str, str]] = []
    for i, (url, body) in enumerate(rows, 1):
        try:
            suffix = "  [corpo manual]" if body else ""
            print(f"  [{i}/{len(rows)}] {url}{suffix}")
            item = scrape(url, manual_body=body)
            items.append(item)
            print(f"      OK: {item.fonte} - {item.titulo[:80]}")
        except Exception as e:
            erros.append((url, str(e)))
            print(f"      FALHOU: {e}")

    if not items:
        print("\nNenhuma noticia extraida com sucesso. Abortando.", file=sys.stderr)
        for u, msg in erros:
            print(f"  {u} -> {msg}", file=sys.stderr)
        return 1

    out_path = build_eml(items, d, args.out)
    print(f"\n{len(items)} noticia(s) incluidas. {len(erros)} falha(s).")
    print(f"Arquivo gerado: {out_path}")
    if erros:
        print("\nURLs com falha (nao entraram no e-mail):")
        for u, msg in erros:
            print(f"  {u} -> {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
