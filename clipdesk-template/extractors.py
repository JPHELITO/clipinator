"""Engine de extração — helpers de parsing HTML.

NÃO EDITE ESTE ARQUIVO. Adicione fontes em `sources.py`.
"""
from __future__ import annotations

import re
from typing import Callable

from bs4 import BeautifulSoup, Tag

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
    """Cria um extractor para um site novo passando uma lista de seletores CSS
    do container do artigo, em ordem de prioridade. O primeiro que casar vence."""
    return lambda soup: _generic(soup, selectors)


# Fallback genérico amplo. Funciona para boa parte dos CMSs (WordPress, Drupal,
# layouts custom comuns). Use como ponto de partida — só crie um extractor
# dedicado se este falhar para o site específico.
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
    r"^\s*[⟶→➔➜►▸‣»]\s*",
    r"^\s*[©®]?\s*\d{4}\s+bloomberg\b",
    r"^\s*todos\s+os\s+direitos\s+reservados",
    r"^\s*[\wÀ-ÿ][\wÀ-ÿ\s&'-]{1,40}\s*\|\s*[\wÀ-ÿ][\wÀ-ÿ\s&'-]{1,40}\s*$",
]
NOISE_REGEX = re.compile("|".join(_NOISE_PATTERNS), re.IGNORECASE)


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


def clean_paragraphs(paragraphs: list[str]) -> list[str]:
    out: list[str] = []
    for p in paragraphs:
        p = re.sub(r"\s+", " ", p).strip()
        p = re.sub(r"\s+([.,;:!?])", r"\1", p)
        if not p:
            continue
        if NOISE_REGEX.search(p):
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
