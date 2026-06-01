"""ClipDesk — gera .eml a partir de um Excel com URLs de notícias.

NÃO EDITE ESTE ARQUIVO. Para personalizar:
  - Marca / branding: edite `config.py`
  - Adicionar/remover fontes: edite `sources.py` (siga BUILD_GUIDE.md)

Uso:
    python clipdesk.py links.xlsx
    python clipdesk.py links.xlsx --data 2026-04-22
    python clipdesk.py links.xlsx --out pasta_saida

Paywall: rode `python login.py <dominio>` uma vez para exportar cookies da
sua sessão logada no Chrome. O scraper carrega cookies/<dominio>.txt
automaticamente.
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
from urllib.parse import urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

from extractors import clean_paragraphs, looks_paywalled
from sources import EXTRACTORS, IMPERSONATE_DOMAINS, SOURCE_NAMES
import config

BASE_DIR = Path(__file__).parent
COOKIES_DIR = BASE_DIR / "cookies"


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


def _site_suffix_patterns() -> list[re.Pattern]:
    names = set(SOURCE_NAMES.values())
    return [
        re.compile(r"\s*[\|–\-]\s*" + re.escape(name) + r"\s*$", re.IGNORECASE)
        for name in names
    ]


def clean_title(titulo: str) -> str:
    t = re.sub(r"\s+", " ", titulo).strip()
    patterns = _site_suffix_patterns()
    changed = True
    while changed:
        changed = False
        for pat in patterns:
            new = pat.sub("", t).strip()
            if new != t and new:
                t = new
                changed = True
    return t


def _extract(html: str, domain: str) -> tuple[str, list[str]]:
    soup = BeautifulSoup(html, "lxml")
    extractor = EXTRACTORS[domain]
    titulo, paragrafos = extractor(soup)
    return clean_title(titulo), clean_paragraphs(paragrafos)


def scrape(url: str, manual_body: str | None = None) -> NewsItem:
    domain = get_domain(url)
    fonte = SOURCE_NAMES.get(domain)
    if fonte is None or domain not in EXTRACTORS:
        raise ValueError(
            f"Domínio não cadastrado: {domain} (url: {url}). "
            f"Adicione em sources.py — veja BUILD_GUIDE.md."
        )

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
        raise ValueError(f"Título não encontrado em {url}")
    if not paragrafos:
        raise ValueError(f"Corpo vazio em {url}")
    if manual_body is None and looks_paywalled(paragrafos):
        raise ValueError(
            f"Conteúdo parece estar atrás de paywall em {url} "
            f"(rode login.py para o domínio, ou preencha a coluna 'corpo' do Excel)"
        )

    return NewsItem(url=url, domain=domain, fonte=fonte, titulo=titulo, paragrafos=paragrafos)


# =============================================================================
# Email builder (HTML + .eml)
# =============================================================================

MONTH_EN = {
    1: "January", 2: "February", 3: "March", 4: "April", 5: "May", 6: "June",
    7: "July", 8: "August", 9: "September", 10: "October", 11: "November", 12: "December",
}
MONTH_PT = {
    1: "janeiro", 2: "fevereiro", 3: "março", 4: "abril", 5: "maio", 6: "junho",
    7: "julho", 8: "agosto", 9: "setembro", 10: "outubro", 11: "novembro", 12: "dezembro",
}

STYLE_BLOCK = (
    "<style>"
    'p.MsoNormal,li.MsoNormal,div.MsoNormal{margin:0;font-size:11.0pt;font-family:"Calibri",sans-serif;}'
    "a:link{color:#0563C1;text-decoration:underline;}"
    "a:visited{color:#954F72;text-decoration:underline;}"
    "</style>"
)
BLANK = '<p class="MsoNormal">&nbsp;</p>'


def _esc(s: str) -> str:
    return escape(s, quote=False)


def format_date_header(d: date) -> str:
    if config.DATE_LANGUAGE == "pt":
        return f"{d.day:02d} de {MONTH_PT[d.month]} de {d.year}"
    return f"{d.day:02d} {MONTH_EN[d.month]} {d.year}"


def build_html(items: list[NewsItem], d: date) -> str:
    date_text = format_date_header(d)
    color = config.HEADER_COLOR

    bullets_html = "".join(
        f'<li class="MsoNormal"><b>{_esc(item.titulo)} ({_esc(item.fonte)})</b></li>'
        for item in items
    )
    index_block = f'<ul type="disc">{bullets_html}</ul>'

    sections: list[str] = []
    for item in items:
        title_html = (
            '<p class="MsoNormal">'
            f'<b><span style="font-size:14.0pt">{_esc(item.titulo)} ({_esc(item.fonte)})</span></b>'
            '</p>'
        )
        body_parts = [f'<p class="MsoNormal">{_esc(par)}</p>' for par in item.paragrafos]
        body_html = BLANK.join(body_parts)
        source_html = (
            '<p class="MsoNormal">'
            '<span style="color:black">Fonte:</span> '
            f'<a href="{_esc(item.url)}">{_esc(item.url)}</a>'
            "</p>"
        )
        sections.append(title_html + BLANK + body_html + BLANK + source_html + BLANK)

    header_text = config.HEADER_TEMPLATE.format(product_name=config.PRODUCT_NAME, date=date_text)
    header_html = (
        '<p class="MsoNormal" align="center" style="text-align:center">'
        f'<b><span style="font-size:18.0pt;color:{_esc(color)}">'
        f"{_esc(header_text)}"
        "</span></b></p>"
    )
    subheader_html = (
        '<p class="MsoNormal">'
        f'<b><span style="font-size:14.0pt;color:{_esc(color)}">{_esc(config.HEADLINES_TITLE)}</span></b>'
        '</p>'
    )

    signature = config.SIGNATURE_BLOCK_HTML
    signature_part = f"{signature}{BLANK}" if signature else ""

    return (
        '<html><head><meta charset="utf-8">'
        f"{STYLE_BLOCK}"
        "</head>"
        '<body lang="EN-US" link="#0563C1" vlink="#954F72" style="word-wrap:break-word">'
        '<div class="WordSection1">'
        f"{header_html}{BLANK}{subheader_html}{index_block}{BLANK}{signature_part}"
        f"{''.join(sections)}"
        "</div></body></html>"
    )


def build_plain_text(items: list[NewsItem], d: date) -> str:
    header = config.HEADER_TEMPLATE.format(
        product_name=config.PRODUCT_NAME, date=format_date_header(d)
    )
    lines = [header, "", config.HEADLINES_TITLE]
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
    date_text = format_date_header(d)
    subject = config.SUBJECT_TEMPLATE.format(product_name=config.PRODUCT_NAME, date=date_text)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = ""
    msg["To"] = ""
    msg.set_content(build_plain_text(items, d), charset="utf-8")
    msg.add_alternative(build_html(items, d), subtype="html")

    out_dir.mkdir(parents=True, exist_ok=True)
    filename = config.OUTPUT_FILENAME_TEMPLATE.format(date=d.strftime("%Y-%m-%d"))
    out_path = out_dir / filename
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
    parser = argparse.ArgumentParser(description="Gera .eml a partir de Excel com URLs.")
    parser.add_argument("xlsx", type=Path, help="Caminho do Excel com coluna 'url'")
    parser.add_argument("--data", type=str, default=None, help="Data do cabeçalho (YYYY-MM-DD). Default: hoje.")
    parser.add_argument("--out", type=Path, default=Path("out"), help="Pasta de saída (default: ./out)")
    args = parser.parse_args()

    if not args.xlsx.exists():
        print(f"ERRO: arquivo não encontrado: {args.xlsx}", file=sys.stderr)
        return 2

    d = datetime.strptime(args.data, "%Y-%m-%d").date() if args.data else date.today()

    rows = read_input(args.xlsx)
    if not rows:
        print("ERRO: nenhuma URL válida encontrada no Excel.", file=sys.stderr)
        return 2

    print(f"Lidas {len(rows)} URL(s). Extraindo notícias...")

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
        print("\nNenhuma notícia extraída com sucesso. Abortando.", file=sys.stderr)
        for u, msg in erros:
            print(f"  {u} -> {msg}", file=sys.stderr)
        return 1

    out_path = build_eml(items, d, args.out)
    print(f"\n{len(items)} notícia(s) incluídas. {len(erros)} falha(s).")
    print(f"Arquivo gerado: {out_path}")
    if erros:
        print("\nURLs com falha (não entraram no e-mail):")
        for u, msg in erros:
            print(f"  {u} -> {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
