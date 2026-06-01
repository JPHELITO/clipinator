"""Scraper de publicações de research do portal Itaú BBA Smart.

Fluxo:
  1. Lê lista de relatórios do cache capturado em 'python login.py --itaubba'
     (itaubba_reports_cache.json — extraído do Chrome real, sem risco de bloqueio)
  2. Para cada relatório, usa requests + cookies para buscar a URL do PDF
     na página /report/{uuid} (sem Playwright — menos detectável que browser)
  3. Fallback: usa URL da página do relatório se PDF não encontrado

Autenticação:
  • State file:  news_generator/cookies/itaubba_state.json   (cookies)
  • Cache file:  news_generator/cookies/itaubba_reports_cache.json  (lista de relatórios)
  • Login manual: python login.py --itaubba
    → abre Chrome real → usuário loga → salva cookies + captura lista de relatórios
"""
from __future__ import annotations

import json
import logging
import re
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger(__name__)

# ── Caminhos ─────────────────────────────────────────────────────────────────

_COOKIES_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "news_generator"
    / "cookies"
)
_STATE_FILE  = _COOKIES_DIR / "itaubba_state.json"
_CACHE_FILE  = _COOKIES_DIR / "itaubba_reports_cache.json"

# ── URLs base ────────────────────────────────────────────────────────────────

_PORTAL      = "https://www.itau.com.br/itaubba-pt/portal/equity"
_REPORT_URL  = f"{_PORTAL}/report/{{uuid}}"
_VIEWER_RE   = re.compile(r"https?://[^\s\"'<>]+/viewer/equity/[^\s\"'<>]+", re.IGNORECASE)
_UUID_RE     = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)

# Remove apenas o tipo de relatório que antecede "Itaú BBA on ...".
# Exemplos:
#   "Market Color Itaú BBA on CSN: Focus on Deleveraging..."   → "Itaú BBA on CSN: Focus on Deleveraging..."
#   "Flash news Itaú BBA on Natural Resources: Commodities..." → "Itaú BBA on Natural Resources: Commodities..."
#   "Company update Itaú BBA on Vale: Updating Model..."       → "Itaú BBA on Vale: Updating Model..."
_ITAUBBA_PREFIX_RE = re.compile(
    r"^.+?(?=Ita[uú]\s*BBA)", re.IGNORECASE
)

# ── Parâmetros ───────────────────────────────────────────────────────────────

_N_PUBLICATIONS = 10
_PHASE2_BUDGET  = 90     # segundos máximos para buscar URLs de PDF
_REQUEST_TIMEOUT = 15    # timeout por requisição HTTP

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Referer": "https://www.itau.com.br/itaubba-pt/portal/equity/",
}


# ── Modelo de dados ──────────────────────────────────────────────────────────

class Publication(NamedTuple):
    title:      str
    report_url: str
    pdf_url:    str
    found_at:   datetime


# ── Helpers ──────────────────────────────────────────────────────────────────

def _load_cookies() -> dict | None:
    """Carrega cookies do state file. Retorna None se inválido."""
    if not _STATE_FILE.exists():
        log.warning(
            "itaubba_scraper: %s não encontrado. "
            "Execute 'python login.py --itaubba' para fazer login.",
            _STATE_FILE.name,
        )
        return None
    try:
        state = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        cookies = state.get("cookies", [])
        if not cookies:
            log.warning("itaubba_scraper: state file sem cookies — refaça o login.")
            return None
        # Converte para dict simples {name: value}
        return {c["name"]: c["value"] for c in cookies if c.get("name") and c.get("value")}
    except Exception as e:
        log.warning("itaubba_scraper: erro ao ler state file: %s", e)
        return None


def _load_cache() -> list[dict]:
    """Carrega lista de relatórios do cache capturado no login."""
    if not _CACHE_FILE.exists():
        log.warning(
            "itaubba_scraper: %s não encontrado. "
            "Execute 'python login.py --itaubba' para capturar a lista de relatórios.",
            _CACHE_FILE.name,
        )
        return []
    try:
        data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        reports = data.get("reports", [])
        captured_at = data.get("captured_at", "?")
        log.info(
            "itaubba_scraper: cache carregado — %d relatório(s) (capturado em %s)",
            len(reports), captured_at[:19],
        )
        return reports
    except Exception as e:
        log.warning("itaubba_scraper: erro ao ler cache: %s", e)
        return []


def _make_session(cookies: dict):
    """Cria requests.Session com cookies e headers do portal."""
    import requests
    s = requests.Session()
    s.headers.update(_HEADERS)
    s.cookies.update(cookies)
    return s


def _clean_title(title: str) -> str:
    """Remove o prefixo de tipo de relatório do título.

    Padrão Itaú BBA: "{Tipo} Itaú BBA on {Empresa}: {Título real}"
    Retorna apenas o "Título real" após o primeiro ': '.
    """
    if not title:
        return title
    cleaned = _ITAUBBA_PREFIX_RE.sub("", title).strip()
    return cleaned if cleaned else title


def _find_pdf_url(html: str, report_url: str) -> str:
    """Extrai URL do viewer/equity do HTML da página do relatório."""
    # 1. href direto
    m = _VIEWER_RE.search(html)
    if m:
        return m.group(0).rstrip("\"'")

    # 2. Padrão parcial (sem domínio)
    partial = re.search(r'["\']([^"\']*viewer/equity/[^"\']+)["\']', html, re.IGNORECASE)
    if partial:
        path = partial.group(1)
        if path.startswith("http"):
            return path
        return "https://www.itau.com.br" + (path if path.startswith("/") else "/" + path)

    return ""


# ── Função principal ─────────────────────────────────────────────────────────

def collect_itaubba_publications() -> list[Publication]:
    """Coleta as 10 publicações mais recentes do portal Itaú BBA Smart.

    Usa o cache capturado em 'python login.py --itaubba' para a lista de
    relatórios, e requests+cookies para buscar as URLs dos PDFs.
    Retorna lista de Publication (pode ser vazia em caso de erro).
    Nunca levanta exceção — falhas são logadas como warnings.
    """
    cookies = _load_cookies()
    if cookies is None:
        return []

    all_reports = _load_cache()
    if not all_reports:
        return []

    # Deduplica e pega os primeiros N
    seen: set[str] = set()
    top_reports: list[dict] = []
    for r in all_reports:
        uuid = r.get("uuid", "").strip()
        if not uuid or uuid in seen:
            continue
        seen.add(uuid)
        top_reports.append(r)
        if len(top_reports) >= _N_PUBLICATIONS:
            break

    log.info(
        "itaubba_scraper: %d relatório(s) no cache — buscando URLs de PDF via requests",
        len(top_reports),
    )

    publications: list[Publication] = []
    now = datetime.now(tz=timezone.utc)

    try:
        import requests as _req
        session = _make_session(cookies)
        phase2_start = _time.time()

        for i, report in enumerate(top_reports):
            elapsed = _time.time() - phase2_start
            if elapsed > _PHASE2_BUDGET:
                log.warning(
                    "itaubba_scraper: budget esgotado (%.0fs) — %d/%d processados",
                    elapsed, i, len(top_reports),
                )
                break

            uuid       = report.get("uuid", "").strip()
            title      = _clean_title(report.get("title", "").strip())
            report_url = report.get("href") or _REPORT_URL.format(uuid=uuid)

            if not (uuid and title):
                continue

            # Usa URL do viewer capturada durante o login (chrome CDP), se disponível.
            # Formato: https://www.itau.com.br/itaubba-pt/portal/viewer/equity/{uuid}/{filename}
            pdf_url = report.get("pdf_url", "").strip()

            if not pdf_url:
                # Fallback: tenta via requests (geralmente recebe 403 nas páginas do portal)
                try:
                    resp = session.get(report_url, timeout=_REQUEST_TIMEOUT, allow_redirects=True)
                    if resp.status_code == 200:
                        pdf_url = _find_pdf_url(resp.text, report_url)
                        if pdf_url:
                            log.debug("itaubba_scraper: [%d/%d] ✓ PDF via requests: %s", i+1, len(top_reports), pdf_url[:80])
                        else:
                            log.debug("itaubba_scraper: [%d/%d] PDF não encontrado em %s", i+1, len(top_reports), report_url[:60])
                    else:
                        log.debug("itaubba_scraper: [%d/%d] HTTP %d em %s", i+1, len(top_reports), resp.status_code, report_url[:60])
                except Exception as e:
                    log.debug("itaubba_scraper: [%d/%d] erro em '%s': %s", i+1, len(top_reports), title[:40], e)
            else:
                log.debug("itaubba_scraper: [%d/%d] ✓ PDF do cache: %s", i+1, len(top_reports), pdf_url[:80])

            publications.append(Publication(
                title=title,
                report_url=report_url,
                pdf_url=pdf_url or report_url,   # fallback: página do relatório
                found_at=now,
            ))

        elapsed_total = _time.time() - phase2_start
        log.info(
            "itaubba_scraper: %d publicação(ões) coletada(s) em %.0fs",
            len(publications), elapsed_total,
        )

    except ImportError:
        log.warning("itaubba_scraper: requests não instalado — pip install requests")
    except Exception:
        log.exception("itaubba_scraper: erro inesperado na coleta")

    return publications
