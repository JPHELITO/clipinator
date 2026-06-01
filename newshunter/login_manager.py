"""Gerenciador de re-login in-dashboard.

Expõe funções usadas pelo app.py para abrir browsers e capturar sessões
sem precisar do terminal:

  launch_chrome_for_valor()  → abre Chrome real com CDP na porta 9222
  confirm_valor_login()      → extrai cookies via CDP e salva valor_state.json

  launch_playwright_for(source) → abre Playwright visível (Platts, Fastmarkets)
  confirm_playwright_login(source) → salva storage_state do contexto aberto
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

_BASE = Path(__file__).resolve().parent.parent
_COOKIES_DIR = _BASE.parent / "news_generator" / "cookies"
_CDP_PORT      = 9222
_CDP_PORT_ITAU = 9223   # porta separada para Itaú BBA (evita conflito com Valor)

_STATE_FILES = {
    "valor":        _COOKIES_DIR / "valor_state.json",
    "platts":       _COOKIES_DIR / "platts_state.json",
    "fastmarkets":  _COOKIES_DIR / "fastmarkets_state.json",
}

_URLS = {
    "valor":       "https://valor.globo.com/",
    "platts":      "https://core.spglobal.com/",
    "fastmarkets": "https://dashboard.fastmarkets.com/",
}

# Guarda contexto Playwright aberto entre start e confirm (por source)
_pw_open: dict[str, object] = {}


# ── Chrome real (Valor) ───────────────────────────────────────────────────────

def _find_chrome() -> str | None:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe"),
    ]
    return next((c for c in candidates if os.path.exists(c)), None)


def _cdp_up() -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{_CDP_PORT}/json/version", timeout=1)
        return True
    except Exception:
        return False


def _wait_cdp(timeout: int = 20) -> bool:
    for _ in range(timeout * 2):
        if _cdp_up():
            return True
        time.sleep(0.5)
    return False


def _cdp_cookies() -> list[dict]:
    import websocket  # websocket-client
    tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{_CDP_PORT}/json").read())
    page_tabs = [t for t in tabs if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if page_tabs:
        ws_url = page_tabs[0]["webSocketDebuggerUrl"]
    else:
        version = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{_CDP_PORT}/json/version").read())
        ws_url = version["webSocketDebuggerUrl"]
    ws = websocket.create_connection(ws_url, timeout=10)
    try:
        ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg["result"]["cookies"]
    finally:
        ws.close()


def launch_chrome_for_valor() -> tuple[bool, str]:
    """Abre Chrome com CDP para login do Valor. Retorna (ok, mensagem)."""
    chrome = _find_chrome()
    if not chrome:
        return False, "Chrome não encontrado. Instale o Google Chrome."

    if _cdp_up():
        return True, "Chrome já está aberto com porta de debug."

    profile_dir = _BASE / "chrome_profile_valor"
    profile_dir.mkdir(exist_ok=True)
    args = [
        chrome,
        f"--remote-debugging-port={_CDP_PORT}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--remote-allow-origins=*",
        _URLS["valor"],
    ]
    DETACHED = 0x00000008 if os.name == "nt" else 0
    subprocess.Popen(args, creationflags=DETACHED, close_fds=True)

    if not _wait_cdp(timeout=20):
        return False, "Chrome não abriu a porta de debug. Tente novamente."

    return True, "Chrome aberto."


def confirm_valor_login() -> tuple[bool, str]:
    """Extrai cookies do Chrome via CDP e salva valor_state.json."""
    if not _cdp_up():
        return False, "Chrome não está aberto. Clique em 'Abrir Chrome' primeiro."

    try:
        cookies = _cdp_cookies()
    except Exception as e:
        return False, f"Erro ao extrair cookies: {e}"

    valor_cookies = [
        c for c in cookies
        if any(d in c.get("domain", "") for d in ("globo.com", "valor.globo.com"))
    ]
    if not valor_cookies:
        return False, "Nenhum cookie do Globo/Valor encontrado. Você fez login?"

    pw_cookies = []
    for c in valor_cookies:
        pw_cookies.append({
            "name":     c.get("name", ""),
            "value":    c.get("value", ""),
            "domain":   c.get("domain", ""),
            "path":     c.get("path", "/"),
            "expires":  c.get("expires", -1),
            "httpOnly": c.get("httpOnly", False),
            "secure":   c.get("secure", False),
            "sameSite": c.get("sameSite") or "Lax",
        })

    state_file = _STATE_FILES["valor"]
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps({"cookies": pw_cookies, "origins": []}, indent=2),
        encoding="utf-8",
    )
    log.info("login_manager: valor_state.json salvo (%d cookies)", len(pw_cookies))

    # Limpa alerta de sessão
    try:
        from .store import clear_session_alert
        clear_session_alert("valor")
    except Exception:
        pass

    return True, f"Login salvo com sucesso ({len(pw_cookies)} cookies)."


# ── Chrome real (Itaú BBA Smart) ─────────────────────────────────────────────

_ITAUBBA_URL        = "https://www.itau.com.br/itaubba-pt/portal/equity/analyst/004506788"
_ITAUBBA_STATE_FILE = _COOKIES_DIR / "itaubba_state.json"
_ITAUBBA_CACHE_FILE = _COOKIES_DIR / "itaubba_reports_cache.json"
_ITAUBBA_DOMAINS    = ["itau.com.br", "www.itau.com.br"]

_DOM_EXTRACT_JS = r"""
(() => {
    const UUID = /\/report\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i;
    const seen  = new Set();
    const items = [];
    for (const a of document.querySelectorAll('a[href]')) {
        const href = a.getAttribute('href') || '';
        const m    = UUID.exec(href);
        if (!m) continue;
        const uuid = m[1];
        if (seen.has(uuid)) continue;
        seen.add(uuid);
        let title = (a.innerText || a.textContent || '').trim();
        if (!title || title.length < 5) {
            let el = a.parentElement;
            for (let i = 0; i < 5 && el; i++) {
                const t = (el.innerText || '').trim().split('\n')[0].trim();
                if (t && t.length > 5) { title = t; break; }
                el = el.parentElement;
            }
        }
        if (title && a.href) {
            items.push({ uuid, title: title.slice(0, 250), href: a.href });
        }
    }
    return JSON.stringify(items);
})()
"""

# JS executado em cada página de relatório para encontrar o link do PDF viewer.
# Procura: links com /viewer/equity/, data-attrs, e atributos onclick.
_VIEWER_EXTRACT_JS = r"""
(() => {
    const VIEWER = /\/viewer\/equity\//i;
    // 1. Links diretos
    for (const a of document.querySelectorAll('a[href]')) {
        if (VIEWER.test(a.href)) return a.href;
    }
    // 2. Data attributes (frameworks que constroem URLs dinamicamente)
    for (const el of document.querySelectorAll('[data-href],[data-src],[data-url],[data-link]')) {
        for (const attr of ['data-href','data-src','data-url','data-link']) {
            const v = el.getAttribute(attr);
            if (v && VIEWER.test(v)) {
                return v.startsWith('http') ? v
                     : 'https://www.itau.com.br' + (v.startsWith('/') ? v : '/' + v);
            }
        }
    }
    // 3. onclick handlers
    for (const el of document.querySelectorAll('[onclick]')) {
        const v = el.getAttribute('onclick') || '';
        const m = v.match(/https?:\/\/[^\s"'<>]+\/viewer\/equity\/[^\s"'<>]+/i);
        if (m) return m[0].replace(/['"\\)]/g, '');
    }
    return '';
})()
"""


def _itaubba_cdp_up() -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{_CDP_PORT_ITAU}/json/version", timeout=1)
        return True
    except Exception:
        return False


def _wait_itaubba_cdp(timeout: int = 25) -> bool:
    for _ in range(timeout * 2):
        if _itaubba_cdp_up():
            return True
        time.sleep(0.5)
    return False


def launch_chrome_for_itaubba() -> tuple[bool, str]:
    """Abre Chrome real na porta 9223 apontando para o portal Itaú BBA Smart."""
    chrome = _find_chrome()
    if not chrome:
        return False, "Chrome não encontrado. Instale o Google Chrome."

    if _itaubba_cdp_up():
        # Chrome já aberto — abre nova aba via API
        try:
            import urllib.parse
            encoded = urllib.parse.quote(_ITAUBBA_URL, safe="")
            urllib.request.urlopen(
                f"http://127.0.0.1:{_CDP_PORT_ITAU}/json/new?{encoded}", timeout=5
            )
        except Exception:
            pass
        return True, "Chrome já aberto — nova aba com o portal Itaú BBA foi aberta."

    profile_dir = _BASE / "chrome_profile_itaubba"
    profile_dir.mkdir(exist_ok=True)
    args = [
        chrome,
        f"--remote-debugging-port={_CDP_PORT_ITAU}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--remote-allow-origins=*",
        _ITAUBBA_URL,
    ]
    DETACHED = 0x00000008 if os.name == "nt" else 0
    subprocess.Popen(args, creationflags=DETACHED, close_fds=True)

    if not _wait_itaubba_cdp(timeout=25):
        return False, "Chrome não abriu. Verifique se o Google Chrome está instalado."

    return True, "Chrome aberto com o portal Itaú BBA Smart."


def confirm_itaubba_login() -> tuple[bool, str]:
    """Captura cookies + lista de relatórios do Chrome aberto. Salva no banco."""
    if not _itaubba_cdp_up():
        return False, "Chrome não está aberto. Clique em 'Abrir Chrome' primeiro."

    import websocket as _ws_mod

    def _connect():
        tabs = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{_CDP_PORT_ITAU}/json").read())
        page_tabs = [t for t in tabs
                     if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        itau_tab  = next((t for t in page_tabs if "itau.com.br" in t.get("url", "")), None)
        ws_url    = (itau_tab or (page_tabs[0] if page_tabs else None))
        if ws_url is None:
            version = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{_CDP_PORT_ITAU}/json/version").read())
            ws_url = version.get("webSocketDebuggerUrl")
        else:
            ws_url = ws_url["webSocketDebuggerUrl"]
        return _ws_mod.create_connection(ws_url, timeout=15)

    def _cmd(ws, method, params=None, msg_id=1):
        ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})

    # Captura cookies
    try:
        ws = _connect()
        try:
            result  = _cmd(ws, "Storage.getCookies", msg_id=1)
            cookies = result.get("cookies", [])
        finally:
            ws.close()
    except Exception as e:
        return False, f"Erro ao extrair cookies: {e}"

    itaubba_cookies = [
        c for c in cookies
        if any(d in c.get("domain", "") for d in _ITAUBBA_DOMAINS)
    ]
    if not itaubba_cookies:
        return False, "Nenhum cookie do Itaú encontrado. Você fez login no portal?"

    pw_cookies = [
        {
            "name":     c.get("name", ""),
            "value":    c.get("value", ""),
            "domain":   c.get("domain", ""),
            "path":     c.get("path", "/"),
            "expires":  c.get("expires", -1),
            "httpOnly": c.get("httpOnly", False),
            "secure":   c.get("secure", False),
            "sameSite": c.get("sameSite") or "Lax",
        }
        for c in itaubba_cookies
    ]
    _ITAUBBA_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _ITAUBBA_STATE_FILE.write_text(
        json.dumps({"cookies": pw_cookies, "origins": []}, indent=2), encoding="utf-8"
    )

    # Captura lista de relatórios via JS na aba aberta
    reports = []
    try:
        ws = _connect()
        try:
            r   = _cmd(ws, "Runtime.evaluate",
                       {"expression": _DOM_EXTRACT_JS, "returnByValue": True}, msg_id=2)
            raw = r.get("result", {}).get("value", "[]")
            reports = json.loads(raw) if isinstance(raw, str) else []
        finally:
            ws.close()
    except Exception as e:
        log.warning("login_manager: erro ao extrair relatórios: %s", e)

    # ─── Fase 2: captura URLs do viewer PDF abrindo cada página de relatório ─────
    # Abre cada relatório em nova aba do Chrome, extrai o link do viewer
    # (/portal/viewer/equity/{uuid}/{filename}) e fecha a aba.
    # Necessário porque requests recebe 403 nas páginas do portal — só funciona
    # com a sessão Chrome autenticada.
    if reports:
        n_pdf = 0
        log.info(
            "login_manager: buscando URLs de PDF para %d relatório(s)...",
            len(reports[:20]),
        )
        import urllib.parse as _urlparse
        for idx, report in enumerate(reports[:20]):
            href = report.get("href", "")
            if not href:
                continue
            target_id = None
            try:
                # Abre nova aba com a página do relatório
                encoded = _urlparse.quote(href, safe="")
                new_tab_raw = urllib.request.urlopen(
                    f"http://127.0.0.1:{_CDP_PORT_ITAU}/json/new?{encoded}",
                    timeout=8,
                ).read()
                target_id = json.loads(new_tab_raw).get("id")
                if not target_id:
                    continue

                # Tenta extrair o viewer URL por até 10s (2s × 5 tentativas)
                viewer_url = ""
                ws_tab = _ws_mod.create_connection(
                    f"ws://127.0.0.1:{_CDP_PORT_ITAU}/devtools/page/{target_id}",
                    timeout=15,
                )
                try:
                    for _attempt in range(5):
                        time.sleep(2)
                        try:
                            r = _cmd(ws_tab, "Runtime.evaluate",
                                     {"expression": _VIEWER_EXTRACT_JS, "returnByValue": True},
                                     msg_id=_attempt + 1)
                            viewer_url = (r.get("result") or {}).get("value", "")
                            if viewer_url:
                                break
                        except Exception:
                            pass
                finally:
                    ws_tab.close()

                if viewer_url:
                    report["pdf_url"] = viewer_url
                    n_pdf += 1
                    log.info(
                        "login_manager: [%d/%d] ✓ PDF: %s",
                        idx + 1, len(reports), viewer_url[-80:],
                    )
                else:
                    log.debug(
                        "login_manager: [%d/%d] PDF não encontrado em %s",
                        idx + 1, len(reports), href[-60:],
                    )

            except Exception as e:
                log.debug("login_manager: [%d/%d] erro ao capturar PDF: %s", idx + 1, e)
            finally:
                # Fecha a aba aberta
                if target_id:
                    try:
                        urllib.request.urlopen(
                            f"http://127.0.0.1:{_CDP_PORT_ITAU}/json/close/{target_id}",
                            timeout=3,
                        )
                    except Exception:
                        pass

        log.info("login_manager: %d/%d URLs de PDF capturadas", n_pdf, len(reports[:20]))

    import datetime as _dt
    cache_data = {
        "captured_at": _dt.datetime.now().isoformat(),
        "reports": reports[:20],
    }
    _ITAUBBA_CACHE_FILE.write_text(
        json.dumps(cache_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    if not reports:
        return False, (
            f"Cookies salvos ({len(pw_cookies)}), mas nenhum relatório encontrado na página. "
            "Certifique-se de que a página do analista está aberta e carregada."
        )

    # Salva publicações no banco de dados
    n_saved = 0
    try:
        from .itaubba_scraper import collect_itaubba_publications
        from .store import save_publications
        pubs = collect_itaubba_publications()
        if pubs:
            save_publications([
                {"title": p.title, "pdf_url": p.pdf_url, "report_url": p.report_url}
                for p in pubs
            ])
            n_saved = len(pubs)
    except Exception as e:
        log.warning("login_manager: erro ao salvar publicações no banco: %s", e)

    msg = f"{len(reports)} relatório(s) capturado(s)"
    if n_saved:
        msg += f", {n_saved} salvo(s) no banco."
    else:
        msg += ". Execute uma busca para atualizar o banco."
    return True, msg


# ── Playwright visível (Platts, Fastmarkets) ──────────────────────────────────

def launch_playwright_for(source: str) -> tuple[bool, str]:
    """Abre browser Playwright visível para login. Retorna (ok, mensagem)."""
    if source not in _STATE_FILES:
        return False, f"Fonte desconhecida: {source}"

    if source in _pw_open:
        return True, "Browser já está aberto."

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, "Playwright não instalado."

    try:
        pw = sync_playwright().start()
        try:
            browser = pw.chromium.launch(
                headless=False,
                channel="chrome",
                args=["--start-maximized"],
            )
        except Exception:
            browser = pw.chromium.launch(headless=False, args=["--start-maximized"])

        ctx_kwargs: dict = {
            "viewport": None,
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        }
        state_file = _STATE_FILES[source]
        if state_file.exists():
            ctx_kwargs["storage_state"] = str(state_file)

        ctx = browser.new_context(**ctx_kwargs)
        page = ctx.new_page()
        page.goto(_URLS[source], wait_until="domcontentloaded", timeout=40_000)

        _pw_open[source] = {"pw": pw, "browser": browser, "ctx": ctx}
        return True, "Browser aberto."
    except Exception as e:
        return False, f"Erro ao abrir browser: {e}"


def confirm_playwright_login(source: str) -> tuple[bool, str]:
    """Salva storage state do browser Playwright aberto e fecha-o."""
    if source not in _pw_open:
        return False, "Nenhum browser aberto. Clique em 'Abrir Browser' primeiro."

    entry = _pw_open.pop(source)
    try:
        ctx = entry["ctx"]
        browser = entry["browser"]
        pw = entry["pw"]

        state = ctx.storage_state()
        state_file = _STATE_FILES[source]
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

        n_cookies = len(state.get("cookies", []))
        log.info("login_manager: %s_state.json salvo (%d cookies)", source, n_cookies)

        try:
            ctx.close()
            browser.close()
            pw.stop()
        except Exception:
            pass

        if n_cookies == 0:
            return False, "Sessão salva mas sem cookies. Você fez login?"

        # Limpa alerta de sessão
        try:
            from .store import clear_session_alert
            clear_session_alert(source)
        except Exception:
            pass

        return True, f"Login salvo com sucesso ({n_cookies} cookies)."
    except Exception as e:
        return False, f"Erro ao salvar sessão: {e}"
