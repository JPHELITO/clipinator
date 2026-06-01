"""FastAPI app: dashboard de busca de notícias S&M / P&P / Cement."""
from __future__ import annotations

import html as _html
import logging
import re as _re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# Carrega .env o mais cedo possível
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .clipping import OUT_DIR as CLIPPING_OUT_DIR, domain_supported, generate_clipping
from .store import get_article_body, save_article_body
from .config import KNOWN_SOURCES, WINDOW_PRESETS
from .pipeline import run_headlines_scan, run_search
from .store import (
    add_keyword,
    add_source_keyword,
    cleanup_old_articles,
    clear_session_alert,
    get_all_source_keywords,
    get_all_source_modes,
    get_config,
    get_session_alerts,
    init_db,
    last_run,
    list_articles,
    remove_keyword,
    remove_source_keyword,
    seed_source_keywords,
    set_config,
    set_source_mode,
)

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "newshunter_templates"
STATIC_DIR = BASE_DIR / "newshunter_static"

# Clipinator root no path para acessar fetch_html com suporte a cookies
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

log = logging.getLogger("newshunter")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="Clipinator News Hunter")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Retenção: alinhada com a janela máxima dos scrapers (72h).
# 48h era insuficiente — apagava artigos que o Estadão/Valor scrapers
# ainda buscam (janela de 72h), fazendo-os desaparecer logo após a busca.
_RETENTION_HOURS = 72
# Janela fixa das páginas Headlines e Clipping.
HEADLINES_WINDOW_HOURS = 48
CLIPPING_WINDOW_HOURS = 24


@app.on_event("startup")
def _startup() -> None:
    init_db()
    seed_source_keywords()  # semeia filtros por fonte na primeira execução
    removed = cleanup_old_articles(_RETENTION_HOURS)
    if removed:
        log.info("Startup cleanup: %d artigos antigos removidos (>%dh)", removed, _RETENTION_HOURS)


def _humanize_age(dt: datetime | None) -> str:
    if dt is None:
        return "sem data"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    secs = int(delta.total_seconds())
    if secs < 0:
        return "agora"
    if secs < 60:
        return "agora"
    if secs < 3600:
        return f"há {secs // 60} min"
    if secs < 86400:
        return f"há {secs // 3600} h"
    return f"há {secs // 86400} d"


def _build_context(request: Request, window_override: int | None = None) -> dict:
    cfg = get_config()
    window_hours = window_override or int(cfg["window_hours"])
    articles = list_articles(window_hours=window_hours, limit=500)
    last = last_run()
    return {
        "request": request,
        "articles": articles,
        "keywords": cfg["keywords"],
        "window_hours": window_hours,
        "default_window_hours": int(cfg["window_hours"]),
        "window_presets": WINDOW_PRESETS,
        "last_run": last,
        "last_run_age": _humanize_age(last["started_at"]) if last else None,
        "humanize": _humanize_age,
    }


def _render(request: Request, name: str, ctx: dict) -> HTMLResponse:
    ctx = {k: v for k, v in ctx.items() if k != "request"}
    return templates.TemplateResponse(request, name, ctx)


@app.get("/", response_class=HTMLResponse)
def home(request: Request, window: int | None = None) -> HTMLResponse:
    ctx = _build_context(request, window_override=window)
    return _render(request, "dashboard.html", ctx)


@app.get("/results", response_class=HTMLResponse)
def results_fragment(request: Request, window: int | None = None) -> HTMLResponse:
    ctx = _build_context(request, window_override=window)
    return _render(request, "_results.html", ctx)


@app.get("/scan", response_class=HTMLResponse)
def scan_fragment(request: Request, window: int | None = None) -> HTMLResponse:
    run_headlines_scan()
    ctx = _build_context(request, window_override=window)
    return _render(request, "_results.html", ctx)


@app.post("/search", response_class=HTMLResponse)
def trigger_search(request: Request, window: int | None = Form(default=None)) -> HTMLResponse:
    stats = run_search()
    cleanup_old_articles(_RETENTION_HOURS)
    log.info("Busca concluída: %s", stats)
    ctx = _build_context(request, window_override=window)
    ctx["search_stats"] = stats
    return _render(request, "_results.html", ctx)


@app.post("/config/window", response_class=HTMLResponse)
def update_window(request: Request, window_hours: int = Form(...)) -> HTMLResponse:
    window_hours = max(1, min(720, window_hours))
    set_config("window_hours", window_hours)
    ctx = _build_context(request)
    return _render(request, "_results.html", ctx)


@app.post("/keywords/add", response_class=HTMLResponse)
def kw_add(request: Request, keyword: str = Form(...)) -> HTMLResponse:
    add_keyword(keyword)
    ctx = _build_context(request)
    return _render(request, "_keywords.html", ctx)


@app.post("/keywords/remove", response_class=HTMLResponse)
def kw_remove(request: Request, keyword: str = Form(...)) -> HTMLResponse:
    remove_keyword(keyword)
    ctx = _build_context(request)
    return _render(request, "_keywords.html", ctx)


# ---------------------------------------------------------------------------
# Alertas de sessão expirada
# ---------------------------------------------------------------------------

_SOURCE_LABELS = {
    "valor": "Valor Econômico",
    "platts": "Platts (S&P Global)",
    "fastmarkets": "Fastmarkets",
}

def _render_publications_panel(pubs: list, success: str = "") -> str:
    """Painel de publicações recentes no clipping (estado normal ou após captura)."""
    n = len(pubs)
    status = f'<span class="pub-count">{n} publicação(ões) no banco</span>'
    if success:
        status += f' <span class="pub-ok">✓ {_html.escape(success)}</span>'

    rows = ""
    for i, p in enumerate(pubs, 1):
        title = _html.escape(p.get("title", "")[:80])
        rows += f'<li class="pub-item">{i}. {title}</li>'
    list_html = f'<ol class="pub-list">{rows}</ol>' if rows else '<p class="muted">Nenhuma publicação salva ainda.</p>'

    return (
        f'<div id="pub-panel">'
        f'<div class="pub-status">{status}</div>'
        f'{list_html}'
        f'<div class="pub-actions">'
        f'<button class="btn-primary" '
        f'hx-post="/api/publications/refresh/start" '
        f'hx-target="#pub-panel" hx-swap="outerHTML" hx-indicator="#pub-spin">'
        f'Update Reports</button>'
        f'<span id="pub-spin" class="htmx-indicator">'
        f'<span class="spin-ring spin-ring-dark"></span></span>'
        f'</div>'
        f'</div>'
    )


def _render_publications_waiting(msg: str) -> str:
    """Estado 'aguardando login' — mostra botão Capturar."""
    return (
        f'<div id="pub-panel">'
        f'<div class="pub-status pub-waiting">'
        f'{_html.escape(msg)}'
        f'</div>'
        f'<p class="pub-instruction">Faça login no portal Itaú BBA Smart no Chrome que abriu.<br>'
        f'Quando os relatórios estiverem visíveis na tela, clique em <strong>Capturar</strong>.</p>'
        f'<div class="pub-actions">'
        f'<button class="btn-primary" '
        f'hx-post="/api/publications/refresh/capture" '
        f'hx-target="#pub-panel" hx-swap="outerHTML" hx-indicator="#pub-spin">'
        f'Capturar</button>'
        f'<span id="pub-spin" class="htmx-indicator">'
        f'<span class="spin-ring spin-ring-dark"></span></span>'
        f'</div>'
        f'</div>'
    )


def _render_publications_error(msg: str) -> str:
    """Estado de erro — mostra mensagem e botão para tentar novamente."""
    return (
        f'<div id="pub-panel">'
        f'<div class="pub-status pub-error">❌ {_html.escape(msg)}</div>'
        f'<div class="pub-actions">'
        f'<button class="btn-secondary" '
        f'hx-post="/api/publications/refresh/start" '
        f'hx-target="#pub-panel" hx-swap="outerHTML" hx-indicator="#pub-spin">'
        f'Tentar novamente</button>'
        f'<span id="pub-spin" class="htmx-indicator">'
        f'<span class="spin-ring spin-ring-dark"></span></span>'
        f'</div>'
        f'</div>'
    )


def _render_alert(source: str, msg: str, state: str = "idle") -> str:
    """Renderiza um banner de alerta com botões de re-login inline."""
    label = _SOURCE_LABELS.get(source, source)
    aid = f"session-alert-{source}"

    # Botão principal varia por estado e fonte
    if state == "waiting":
        # Browser aberto — aguarda confirmação do usuário
        btn_action = (
            f'<button class="session-alert-btn session-alert-btn--confirm" '
            f'hx-post="/api/renew-login/{source}/confirm" '
            f'hx-target="#{aid}" hx-swap="outerHTML" hx-indicator="#{aid}-spin">'
            f'✓ Confirmar login</button>'
            f'<span id="{aid}-spin" class="htmx-indicator session-alert-spin">⏳</span>'
        )
        instruction = " → Faça login no browser que abriu e clique <strong>Confirmar</strong>."
    elif state == "error":
        instruction = f" {msg}"
        btn_action = (
            f'<button class="session-alert-btn" '
            f'hx-post="/api/renew-login/{source}/start" '
            f'hx-target="#{aid}" hx-swap="outerHTML">🔄 Tentar novamente</button>'
        )
    else:
        # idle — botão inicial de renovar
        instruction = ""
        use_chrome = source == "valor"
        btn_label = "🔑 Renovar Login"
        btn_action = (
            f'<button class="session-alert-btn" '
            f'hx-post="/api/renew-login/{source}/start" '
            f'hx-target="#{aid}" hx-swap="outerHTML" hx-indicator="#{aid}-spin">'
            f'{btn_label}</button>'
            f'<span id="{aid}-spin" class="htmx-indicator session-alert-spin">⏳</span>'
        )

    dismiss = (
        f'<button class="session-alert-dismiss" '
        f'hx-post="/api/session-alerts/dismiss/{source}" '
        f'hx-target="#{aid}" hx-swap="outerHTML">✕</button>'
    )

    base_msg = f"<strong>{_html.escape(label)}:</strong> sessão expirada.{instruction}"
    return (
        f'<div class="session-alert" id="{aid}">'
        f'<span class="session-alert-icon">⚠️</span>'
        f'<span class="session-alert-text">{base_msg}</span>'
        f'<div class="session-alert-actions">{btn_action}{dismiss}</div>'
        f'</div>'
    )


@app.get("/api/session-alerts", response_class=HTMLResponse)
def api_session_alerts() -> HTMLResponse:
    """Retorna banners HTML com alertas de sessão expirada (HTMX polling)."""
    alerts = get_session_alerts()
    if not alerts:
        return HTMLResponse("")
    return HTMLResponse("\n".join(_render_alert(a["source"], a.get("message", "")) for a in alerts))


@app.post("/api/session-alerts/dismiss/{source}", response_class=HTMLResponse)
def api_dismiss_alert(source: str) -> HTMLResponse:
    """Descarta (oculta) um alerta de sessão."""
    clear_session_alert(source)
    return HTMLResponse("")


# ---------------------------------------------------------------------------
# Re-login in-dashboard
# ---------------------------------------------------------------------------

@app.post("/api/renew-login/{source}/start", response_class=HTMLResponse)
def api_renew_login_start(source: str) -> HTMLResponse:
    """Abre browser para re-login sem sair da dashboard."""
    from .login_manager import launch_chrome_for_valor, launch_playwright_for

    if source == "valor":
        ok, msg = launch_chrome_for_valor()
    elif source in ("platts", "fastmarkets"):
        ok, msg = launch_playwright_for(source)
    else:
        return HTMLResponse(_render_alert(source, f"Fonte não suportada: {source}", "error"))

    if ok:
        return HTMLResponse(_render_alert(source, msg, "waiting"))
    return HTMLResponse(_render_alert(source, msg, "error"))


@app.get("/api/publications/status", response_class=HTMLResponse)
def api_publications_status() -> HTMLResponse:
    """Retorna painel de status das publicações recentes (para o clipping)."""
    from .store import get_recent_publications
    try:
        pubs = get_recent_publications(10)
    except Exception:
        pubs = []
    return HTMLResponse(_render_publications_panel(pubs))


@app.post("/api/publications/refresh/start", response_class=HTMLResponse)
def api_publications_refresh_start() -> HTMLResponse:
    """Abre Chrome real no portal Itaú BBA para captura de publicações."""
    from .login_manager import launch_chrome_for_itaubba
    ok, msg = launch_chrome_for_itaubba()
    if ok:
        return HTMLResponse(_render_publications_waiting(msg))
    return HTMLResponse(_render_publications_error(msg))


@app.post("/api/publications/refresh/capture", response_class=HTMLResponse)
def api_publications_refresh_capture() -> HTMLResponse:
    """Captura cookies + relatórios do Chrome aberto e salva no banco."""
    from .login_manager import confirm_itaubba_login
    from .store import get_recent_publications
    ok, msg = confirm_itaubba_login()
    try:
        pubs = get_recent_publications(10)
    except Exception:
        pubs = []
    if ok:
        return HTMLResponse(_render_publications_panel(pubs, success=msg))
    return HTMLResponse(_render_publications_error(msg))


@app.post("/api/renew-login/{source}/confirm", response_class=HTMLResponse)
def api_renew_login_confirm(source: str) -> HTMLResponse:
    """Captura sessão do browser aberto e salva o state file."""
    from .login_manager import confirm_valor_login, confirm_playwright_login

    if source == "valor":
        ok, msg = confirm_valor_login()
    elif source in ("platts", "fastmarkets"):
        ok, msg = confirm_playwright_login(source)
    else:
        return HTMLResponse(_render_alert(source, f"Fonte não suportada: {source}", "error"))

    if ok:
        # Sucesso — remove o banner completamente
        return HTMLResponse("")
    return HTMLResponse(_render_alert(source, msg, "error"))


# ---------------------------------------------------------------------------
# Leitor de artigos — busca o conteúdo usando cookies se disponível
# ---------------------------------------------------------------------------

@app.get("/api/article", response_class=HTMLResponse)
def api_article(request: Request, url: str) -> HTMLResponse:
    """Retorna o corpo do artigo em HTML.

    Fluxo:
    1. Corpo em DB ≥ 300 chars → retorna imediatamente (sem scraping)
       Exceção: _FORCE_PLAYWRIGHT_DOMAINS bypassa cache e força Playwright ao vivo.
    2. Playwright ao vivo (SPAs autenticadas) → salva em DB para cache
       Domínios force: sobrescreve DB com corpo limpo (cleanClone JS).
    3. fetch_html + BeautifulSoup (páginas regulares) → salva em DB para cache
    """
    from .playwright_reader import fetch_article, needs_playwright

    _domain = urlparse(url).netloc.lower()
    source = _domain.replace("www.", "")

    # Domínios cujo leitor Playwright aplica limpeza extra (cleanClone JS) que
    # pode produzir corpo mais curto que o sujo armazenado no DB. Para estes,
    # sempre re-fetcha ao vivo e força sobrescrita no DB — garante que o usuário
    # vê conteúdo limpo (sem ads, LE.IA, "Leia também", etc.) mesmo para artigos
    # cuja versão suja já estava em cache.
    _FORCE_PLAYWRIGHT_DOMAINS: frozenset[str] = frozenset([
        "www.estadao.com.br",
    ])
    _force_playwright = _domain in _FORCE_PLAYWRIGHT_DOMAINS

    def _is_html(s: str) -> bool:
        return bool(_re.search(r'</?(?:p|img|ul|ol|li|h[2-6]|strong|em|blockquote)\b', s))

    def _render(body: str, title: str = "") -> HTMLResponse:
        """Renderiza corpo HTML seguro dentro do leitor."""
        t_html = f'<h2 class="reader-title">{_html.escape(title)}</h2>' if title else ""
        if _is_html(body):
            return HTMLResponse(
                f'<div class="reader-article">'
                f'<p class="reader-source">{_html.escape(source)}</p>'
                f'{t_html}'
                f'<div class="reader-body reader-body--html">{body}</div>'
                f'</div>'
            )
        # Texto plano legado: split em parágrafos
        blocks = _re.split(r'\n{2,}', body)
        if len(blocks) <= 1:
            blocks = body.splitlines()
        paras = [b.strip() for b in blocks if len(b.strip()) > 20]
        p_html = "\n".join(f"<p>{_html.escape(p)}</p>" for p in paras) if paras else body
        return HTMLResponse(
            f'<div class="reader-article">'
            f'<p class="reader-source">{_html.escape(source)}</p>'
            f'{t_html}'
            f'<div class="reader-body">{p_html}</div>'
            f'</div>'
        )

    # ── 1. Corpo em cache (DB) ────────────────────────────────────────────────
    # Retorna direto se tiver ≥ 300 chars — garante conteúdo real, não só teaser.
    # Exceção: _force_playwright bypassa o cache para domínios cujo leitor ao
    # vivo aplica limpeza extra (cleanClone JS). Nesses casos, o Playwright
    # sempre roda e o resultado é salvo com force=True no passo 4.
    stored_body = get_article_body(url)
    if stored_body and len(stored_body) >= 300 and not _force_playwright:
        return _render(stored_body)

    # ── 2. Playwright ao vivo (SPAs autenticadas) ─────────────────────────────
    body_html = ""
    title = ""
    is_playwright_domain = needs_playwright(url)
    if is_playwright_domain:
        try:
            title, body_html = fetch_article(url)
        except Exception as e:
            log.warning("playwright_reader falhou em %s: %s", url, e)

        # Se Playwright não trouxe conteúdo (sessão expirada / paywall),
        # usa o que tiver no DB mesmo que seja curto — melhor do que
        # cair no BeautifulSoup sem autenticação e renderizar o paywall HTML.
        if not body_html:
            if stored_body:
                return _render(stored_body)
            return HTMLResponse(
                '<p class="reader-error">Conteúdo não disponível. '
                'A sessão pode ter expirado — execute <code>python login.py</code> '
                'para renovar o acesso.</p>'
            )

    # ── 3. fetch_html + BeautifulSoup (páginas regulares) ─────────────────────
    if not body_html:
        page_html = ""
        try:
            import clipinator as _clip  # type: ignore
            page_html = _clip.fetch_html(url, timeout=20)
        except Exception:
            try:
                import requests as _req
                r = _req.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"},
                    timeout=15,
                )
                page_html = r.text
            except Exception as e:
                return HTMLResponse(
                    f'<p class="reader-error">Não foi possível carregar o artigo.<br>'
                    f'<small>{_html.escape(str(e))}</small></p>'
                )

        if page_html:
            from bs4 import BeautifulSoup
            from .html_utils import article_to_safe_html, extract_article_container
            soup = BeautifulSoup(page_html, "lxml")
            if not title:
                og = soup.find("meta", property="og:title")
                if og and og.get("content"):
                    title = str(og["content"]).strip()
                if not title:
                    t = soup.find("title")
                    if t:
                        title = t.get_text(" ", strip=True)
            for tag in soup.find_all(["nav", "header", "footer", "aside", "script",
                                       "style", "iframe", "form", "noscript"]):
                tag.decompose()
            for tag in soup.find_all(class_=_re.compile(
                r"(^|\b)(ad|ads|banner|popup|cookie|subscribe|paywall|menu|sidebar"
                r"|share|social|comment|promo|newsletter)(\b|$)", _re.I
            )):
                tag.decompose()
            container = extract_article_container(soup, url)
            if container:
                # Usa article_to_safe_html para preservar estrutura completa:
                # headings (h2/h3), listas (ul/ol/li), imagens e parágrafos.
                # Produz o mesmo HTML seguro que os scrapers Fastmarkets/Platts.
                body_html = article_to_safe_html(str(container))
            if not body_html:
                # Fallback: extração de parágrafos em texto plano
                paras = [
                    p.get_text(" ", strip=True)
                    for p in (container or soup).find_all("p")
                    if len(p.get_text(" ", strip=True)) > 40
                ][:40]
                if paras:
                    body_html = "\n".join(f"<p>{_html.escape(p)}</p>" for p in paras)

    # ── 4. Salva no DB para evitar re-scraping na próxima abertura ───────────
    # force=True para domínios com limpeza Playwright (cleanClone JS): sobrescreve
    # o corpo sujo anterior mesmo que o novo seja menor (foi limpo de ads/junk).
    if body_html and len(body_html) >= 300:
        try:
            save_article_body(url, body_html, force=_force_playwright)
        except Exception:
            pass

    # ── 5. Retorna resultado ──────────────────────────────────────────────────
    if body_html:
        return _render(body_html, title)

    return HTMLResponse(
        '<p class="muted">Não foi possível extrair o conteúdo. '
        'O artigo pode estar atrás de um paywall ou exigir JavaScript. '
        'Use "Abrir original" para ler no site.</p>'
    )


# ---------------------------------------------------------------------------
# Headlines
# ---------------------------------------------------------------------------

def _build_headlines_context(request: Request, scan_stats: dict | None = None) -> dict:
    articles = list_articles(window_hours=HEADLINES_WINDOW_HOURS, limit=500)
    last = last_run()
    cfg = get_config()
    return {
        "request": request,
        "articles": articles,
        "keywords": cfg["keywords"],
        "window_hours": HEADLINES_WINDOW_HOURS,
        "last_run": last,
        "last_run_age": _humanize_age(last["started_at"]) if last else None,
        "humanize": _humanize_age,
        "scan_stats": scan_stats,
        "now_iso": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/headlines", response_class=HTMLResponse)
def headlines(request: Request) -> HTMLResponse:
    ctx = _build_headlines_context(request)
    return _render(request, "headlines.html", ctx)


@app.get("/headlines/scan", response_class=HTMLResponse)
def headlines_scan(request: Request) -> HTMLResponse:
    stats = run_headlines_scan()
    ctx = _build_headlines_context(request, scan_stats=stats)
    return _render(request, "_headlines_list.html", ctx)


@app.get("/headlines/list", response_class=HTMLResponse)
def headlines_list(request: Request) -> HTMLResponse:
    ctx = _build_headlines_context(request)
    return _render(request, "_headlines_list.html", ctx)


# ---------------------------------------------------------------------------
# Clipping
# ---------------------------------------------------------------------------

def _build_clipping_context(request: Request, scan_stats: dict | None = None) -> dict:
    articles = list_articles(window_hours=CLIPPING_WINDOW_HOURS, limit=500)
    enriched = [(a, domain_supported(a.url)) for a in articles]
    last = last_run()
    return {
        "request": request,
        "articles": enriched,
        "window_hours": CLIPPING_WINDOW_HOURS,
        "last_run": last,
        "last_run_age": _humanize_age(last["started_at"]) if last else None,
        "humanize": _humanize_age,
        "scan_stats": scan_stats,
        "now_iso": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/clipping", response_class=HTMLResponse)
def clipping(request: Request) -> HTMLResponse:
    ctx = _build_clipping_context(request)
    return _render(request, "clipping.html", ctx)


@app.get("/clipping/scan", response_class=HTMLResponse)
def clipping_scan(request: Request) -> HTMLResponse:
    stats = run_headlines_scan()
    ctx = _build_clipping_context(request, scan_stats=stats)
    return _render(request, "_clipping_list.html", ctx)


@app.get("/clipping/list", response_class=HTMLResponse)
def clipping_list(request: Request) -> HTMLResponse:
    ctx = _build_clipping_context(request)
    return _render(request, "_clipping_list.html", ctx)


@app.post("/clipping/generate", response_class=HTMLResponse)
def clipping_generate(
    request: Request,
    urls: list[str] = Form(...),
    takes: list[str] = Form(default=[]),
    sectors: list[str] = Form(default=[]),
) -> HTMLResponse:
    clean = [(u.strip(), takes[i] if i < len(takes) else "=", sectors[i] if i < len(sectors) else "")
             for i, u in enumerate(urls) if u and u.strip()]
    clean_urls   = [u for u, _, _ in clean]
    clean_takes  = [t for _, t, _ in clean]
    clean_sectors = [s for _, _, s in clean]
    d = date.today()
    out_path, errors = generate_clipping(clean_urls, clean_takes, clean_sectors, d)
    ctx = {
        "request": request,
        "n_ok": len(clean_urls) - len(errors),
        "n_fail": len(errors),
        "n_total": len(clean_urls),
        "errors": errors,
        "download_name": out_path.name if out_path else None,
        "date_str": d.isoformat(),
    }
    return _render(request, "_clipping_result.html", ctx)


@app.get("/clipping/download/{filename}")
def clipping_download(filename: str) -> FileResponse:
    target = (CLIPPING_OUT_DIR / filename).resolve()
    out_root = CLIPPING_OUT_DIR.resolve()
    if out_root != target.parent or not target.exists():
        raise HTTPException(status_code=404, detail="arquivo não encontrado")
    if filename.endswith(".docx"):
        media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif filename.endswith(".eml"):
        media_type = "message/rfc822"
    else:
        media_type = "application/octet-stream"
    return FileResponse(path=str(target), media_type=media_type, filename=filename)


# ---------------------------------------------------------------------------
# Fontes — gerenciamento de filtros por fonte
# ---------------------------------------------------------------------------

def _build_sources_context(request: Request) -> dict:
    mode_map = get_all_source_modes()
    kw_map = get_all_source_keywords()
    sources = []
    for s in KNOWN_SOURCES:
        domain = s["domain"]
        mode = mode_map.get(domain, "global")
        # Para modo specific: lista de keywords (sem o '*' que era legado)
        raw_kws = kw_map.get(domain, [])
        keywords = sorted(kw for kw in raw_kws if kw != "*") if mode == "specific" else []
        sources.append({
            **s,
            "mode": mode,
            "keywords": keywords,
            "domain_id": domain.replace(".", "_").replace("-", "_"),
        })
    return {"request": request, "sources": sources}


@app.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request) -> HTMLResponse:
    ctx = _build_sources_context(request)
    return _render(request, "sources.html", ctx)


@app.post("/sources/mode", response_class=HTMLResponse)
def source_set_mode(
    request: Request,
    domain: str = Form(...),
    mode: str = Form(...),
) -> HTMLResponse:
    if mode in ("all", "global", "specific"):
        set_source_mode(domain, mode)
    ctx = _build_sources_context(request)
    # Retorna apenas o item da fonte alterada
    source = next((s for s in ctx["sources"] if s["domain"] == domain), None)
    if source is None:
        return HTMLResponse("")
    return templates.TemplateResponse(request, "_source_item.html", {"request": request, "s": source})


@app.post("/sources/keywords/add", response_class=HTMLResponse)
def source_kw_add(
    request: Request,
    domain: str = Form(...),
    keyword: str = Form(...),
) -> HTMLResponse:
    add_source_keyword(domain, keyword.strip())
    ctx = _build_sources_context(request)
    source = next((s for s in ctx["sources"] if s["domain"] == domain), None)
    if source is None:
        return HTMLResponse("")
    return templates.TemplateResponse(request, "_source_item.html", {"request": request, "s": source})


@app.post("/sources/keywords/remove", response_class=HTMLResponse)
def source_kw_remove(
    request: Request,
    domain: str = Form(...),
    keyword: str = Form(...),
) -> HTMLResponse:
    remove_source_keyword(domain, keyword)
    ctx = _build_sources_context(request)
    source = next((s for s in ctx["sources"] if s["domain"] == domain), None)
    if source is None:
        return HTMLResponse("")
    return templates.TemplateResponse(request, "_source_item.html", {"request": request, "s": source})


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/favicon.ico")
def favicon() -> RedirectResponse:
    return RedirectResponse(url="/static/favicon.svg", status_code=307)
