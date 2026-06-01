"""Renova cookies/sessão para sites paywalled do clipinator.

Modo padrão (Playwright — recomendado para Fastmarkets e Platts):
    python login.py

  Abre um browser Chromium VISÍVEL para cada site que precisa de Playwright.
  Você faz login normalmente. O script salva automaticamente:
    - news_generator/cookies/fastmarkets_state.json
    - news_generator/cookies/platts_state.json
    - news_generator/cookies/estadao_state.json   (via Chrome real — anti-Akamai)
    - news_generator/cookies/valor_state.json     (via Chrome real)
  incluindo cookies + localStorage (token OIDC) — essenciais para os scrapers.

Modo legado (Chrome DevTools — para Valor Econômico):
    python login.py --legacy

  Abre Chrome com perfil dedicado, você loga, cookies Netscape extraídos via CDP.

Fluxo Playwright (padrão):
  1. Abre janela do browser para cada site listado em PLAYWRIGHT_TARGETS.
  2. Você faz login no site normalmente.
  3. Quando o login for detectado (ou você apertar ENTER), o estado é salvo.
  4. Fecha o browser.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).parent
PROFILE_DIR = BASE_DIR / "chrome_profile"
COOKIES_DIR = BASE_DIR / "cookies"
PORT       = 9222   # porta padrão (Valor)
PORT_ITAU  = 9223   # porta separada para Itaú BBA (evita conflito com Valor)

# ─── Configuração dos sites que usam Playwright state ────────────────────────
# (domain, url_inicial, caminho do state file, seletor que indica login OK)
_NEWS_GEN_COOKIES = BASE_DIR.parent / "news_generator" / "cookies"

PLAYWRIGHT_TARGETS = [
    {
        "domain": "dashboard.fastmarkets.com",
        "url": "https://dashboard.fastmarkets.com/",
        "state_file": _NEWS_GEN_COOKIES / "fastmarkets_state.json",
        # Seletor que indica que o login foi bem-sucedido
        "logged_in_selector": ".dashboard-container, [class*='dashboard'], [class*='widget'], nav",
        "login_indicator": "auth.fastmarkets.com",
        "label": "Fastmarkets PP Dashboard",
        "offer_credentials": True,
        "credentials_file": "fastmarkets_credentials.json",
        "credentials_label": "Fastmarkets",
    },
    {
        "domain": "core.spglobal.com",
        "url": "https://core.spglobal.com/",
        "state_file": _NEWS_GEN_COOKIES / "platts_state.json",
        "logged_in_selector": "[class*='platts'], [class*='insights'], #root, .app-container",
        "login_indicator": "login",
        "label": "Platts / S&P Global Core",
    },
]

# Valor e Estadão usam Chrome real (CDP) — Playwright Chromium bloqueado pelo Akamai/Globo
VALOR_STATE_FILE   = _NEWS_GEN_COOKIES / "valor_state.json"
ESTADAO_STATE_FILE = _NEWS_GEN_COOKIES / "estadao_state.json"

# ─── Targets legado (CDP / Netscape cookies) ──────────────────────────────────
DEFAULT_TARGETS: list[tuple[str, str]] = []


# ═══════════════════════════════════════════════════════════════════════════════
# MODO PLAYWRIGHT — salva state completo (cookies + localStorage)
# ═══════════════════════════════════════════════════════════════════════════════

def renew_playwright_state(target: dict) -> bool:
    """Abre browser visível para o usuário logar, salva Playwright storage state.

    Captura cookies + localStorage (OIDC token) — necessários para os scrapers.
    Retorna True se bem-sucedido.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERRO: playwright não encontrado. Execute: pip install playwright")
        return False

    domain = target["domain"]
    url = target["url"]
    state_file: Path = target["state_file"]
    label = target.get("label", domain)
    logged_in_sel = target.get("logged_in_selector", "body")
    login_indicator = target.get("login_indicator", "login")
    login_indicator_alt = target.get("login_indicator_alt", "")
    locale = target.get("locale", "en-US")

    state_file.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*65}")
    print(f"  Site: {label}")
    print(f"  URL:  {url}")
    print(f"{'='*65}")
    print("  Abrindo browser... faça login quando a janela abrir.")
    print("  O script aguarda você estar na página principal (após login).")
    print("  Se necessário, aperte ENTER aqui depois de logar.")
    print()

    with sync_playwright() as p:
        # Lança browser VISÍVEL (headless=False) para o usuário interagir
        try:
            browser = p.chromium.launch(
                headless=False,
                channel="chrome",
                args=["--start-maximized"],
            )
        except Exception:
            browser = p.chromium.launch(
                headless=False,
                args=["--start-maximized"],
            )

        # Se já existe um state file válido, carrega-o (pode acelerar login)
        ctx_kwargs: dict = {
            "viewport": None,  # Usa tamanho real da janela
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "locale": locale,
        }
        if state_file.exists():
            try:
                ctx_kwargs["storage_state"] = str(state_file)
                print("  (Carregando sessão anterior...)")
            except Exception:
                pass

        ctx = browser.new_context(**ctx_kwargs)
        page = ctx.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=40_000)
        except Exception as e:
            print(f"  Aviso: erro ao carregar {url}: {e}")

        # Aguarda o usuário logar — verifica periodicamente se saiu da página de login
        print(f"  Aguardando login em {domain}...")
        print("  (Quando terminar, aperte ENTER nesta janela de terminal)")

        import threading

        enter_pressed = threading.Event()

        def _wait_enter():
            try:
                input()
            except Exception:
                pass
            enter_pressed.set()

        t = threading.Thread(target=_wait_enter, daemon=True)
        t.start()

        # Verifica a cada 2s se o login foi concluído OU se o usuário apertou ENTER.
        # Se auto_detect=False, pula a detecção automática e aguarda apenas o ENTER.
        auto_detect = target.get("auto_detect", True)
        max_wait = 300  # 5 minutos máximo
        waited = 0
        logged_in = False
        while waited < max_wait and not enter_pressed.is_set():
            time.sleep(2)
            waited += 2
            if not auto_detect:
                continue  # só espera ENTER — não tenta detectar automaticamente
            try:
                current_url = page.url.lower()
                # Não está mais na página de login = logado
                on_login = (
                    login_indicator in current_url
                    or (login_indicator_alt and login_indicator_alt in current_url)
                )
                if not on_login:
                    try:
                        # Verifica se há elementos de dashboard
                        el = page.query_selector(logged_in_sel)
                        if el:
                            logged_in = True
                            print(f"\n  Login detectado! Salvando sessão...")
                            break
                    except Exception:
                        pass
            except Exception:
                pass  # Página ainda carregando

        if not logged_in and not enter_pressed.is_set():
            print(f"\n  Timeout de {max_wait}s. Salvando estado atual...")

        # Aguarda um momento para garantir que localStorage foi populado
        try:
            page.wait_for_timeout(2_000)
        except Exception:
            pass

        # Salva o estado completo (cookies + localStorage)
        try:
            state = ctx.storage_state()
            state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

            # Verifica se tem cookies e localStorage
            n_cookies = len(state.get("cookies", []))
            n_origins = len(state.get("origins", []))
            has_oidc = any(
                "oidc.user:" in item.get("name", "")
                for origin in state.get("origins", [])
                for item in origin.get("localStorage", [])
            )
            print(f"  Salvo: {state_file}")
            print(f"  Cookies: {n_cookies} | LocalStorage origins: {n_origins} | OIDC token: {'sim' if has_oidc else 'nao'}")
            if not has_oidc:
                print(f"  AVISO: token OIDC não encontrado em localStorage.")
                print(f"  Certifique-se de que fez login completo em {url}")
            success = n_cookies > 0
        except Exception as e:
            print(f"  ERRO ao salvar state: {e}")
            success = False

        # Oferece salvar credenciais para auto-login (sites com offer_credentials=True)
        if success and target.get("offer_credentials"):
            creds_file = target.get("credentials_file", "")
            creds_label = target.get("credentials_label", domain)
            if creds_file:
                _offer_save_credentials(
                    state_file.parent,
                    creds_filename=creds_file,
                    site_label=creds_label,
                )

        try:
            ctx.close()
            browser.close()
        except Exception:
            pass

    return success


def _offer_save_credentials(
    cookies_dir: Path,
    creds_filename: str = "fastmarkets_credentials.json",
    site_label: str = "Fastmarkets",
) -> None:
    """Pergunta se quer salvar credenciais para auto-login de um site."""
    creds_file = cookies_dir / creds_filename
    print()
    print("  ─────────────────────────────────────────────────────────────")
    print(f"  Auto-renovação de sessão: {site_label}")
    print("  ─────────────────────────────────────────────────────────────")
    if creds_file.exists():
        print(f"  Credenciais já salvas em: {creds_file.name}")
        resp = input("  Deseja atualizar email/senha? (s/N): ").strip().lower()
        if resp != "s":
            print("  Mantendo credenciais existentes.")
            return

    print()
    print("  Salvar email e senha permite que o scraper refare login")
    print(f"  automaticamente quando a sessão do {site_label} expirar.")
    print("  As credenciais ficam salvas localmente em arquivo JSON.")
    print()
    resp = input("  Deseja salvar credenciais para auto-login? (s/N): ").strip().lower()
    if resp != "s":
        print("  Pulando. Você precisará rodar 'python login.py' manualmente")
        print("  quando a sessão expirar.")
        return

    print()
    email = input(f"  Email / login do {site_label}: ").strip()
    if not email:
        print("  Email vazio — cancelado.")
        return

    import getpass
    password = getpass.getpass(f"  Senha do {site_label}: ")
    if not password:
        print("  Senha vazia — cancelado.")
        return

    try:
        creds = {"email": email, "password": password}
        creds_file.write_text(json.dumps(creds, indent=2), encoding="utf-8")
        print(f"  Credenciais salvas em: {creds_file}")
        print("  O scraper irá renovar a sessão automaticamente!")
    except Exception as e:
        print(f"  ERRO ao salvar credenciais: {e}")


_ITAUBBA_STATE_FILE = _NEWS_GEN_COOKIES / "itaubba_state.json"
_ITAUBBA_DOMAINS    = ["itau.com.br", "www.itau.com.br"]


def renew_itaubba_via_chrome() -> bool:
    """Login do portal Itaú BBA Smart usando Chrome real (anti-bot bypass).

    O portal usa SSO corporativo que bloqueia o Chromium do Playwright.
    Usa Chrome real + CDP na porta separada PORT_ITAU (9223) para não
    conflitar com o Chrome do Valor (porta 9222).
    """
    _URL = "https://www.itau.com.br/itaubba-pt/portal/equity/analyst/004506788"

    def _up() -> bool:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT_ITAU}/json/version", timeout=1)
            return True
        except Exception:
            return False

    def _wait(timeout: int = 20) -> bool:
        for _ in range(timeout * 2):
            if _up():
                return True
            time.sleep(0.5)
        return False

    def _get_cookies() -> list[dict]:
        import websocket
        tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT_ITAU}/json").read())
        page_tabs = [t for t in tabs if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        if not page_tabs:
            version = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{PORT_ITAU}/json/version").read())
            ws_url = version.get("webSocketDebuggerUrl")
        else:
            ws_url = page_tabs[0]["webSocketDebuggerUrl"]
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

    chrome = find_chrome()
    if not chrome:
        print("  ERRO: Chrome.exe não encontrado.")
        print("  Instale o Google Chrome e tente novamente.")
        return False

    state_file = _ITAUBBA_STATE_FILE
    state_file.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*65}")
    print("  Site: Itaú BBA Smart Portal")
    print(f"  URL:  {_URL}")
    print(f"{'='*65}")
    print("  Abrindo Chrome real para evitar bloqueio do SSO corporativo...")
    print()

    if _up():
        print("  Chrome (porta 9223) já está aberto — reusando instância.")
    else:
        profile_dir = BASE_DIR / "chrome_profile_itaubba"
        profile_dir.mkdir(exist_ok=True)
        args = [
            chrome,
            f"--remote-debugging-port={PORT_ITAU}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-allow-origins=*",
            _URL,
        ]
        DETACHED = 0x00000008 if os.name == "nt" else 0
        subprocess.Popen(args, creationflags=DETACHED, close_fds=True)

        print("  Aguardando Chrome abrir...")
        if not _wait(timeout=25):
            print("  ERRO: Chrome não abriu. Verifique se o Chrome está instalado.")
            return False

        print("  Chrome aberto!")

    print()
    print("  >>> Uma janela do Chrome foi aberta com o portal Itaú BBA Smart.")
    print("  >>> Faça login normalmente (usuário e senha corporativos).")
    print("  >>> Quando os relatórios do analista estiverem visíveis na tela,")
    print("  >>> volte aqui e aperte ENTER.")
    print()
    input("  [ENTER após logar] --> ")

    # ── CDP helpers ──────────────────────────────────────────────────────────
    import websocket as _ws_mod

    def _cdp_connect():
        tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT_ITAU}/json").read())
        # Prefere a aba com URL do portal Itaú
        page_tabs = [t for t in tabs if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        itau_tab  = next((t for t in page_tabs if "itau.com.br" in t.get("url", "")), None)
        ws_url = (itau_tab or page_tabs[0] if page_tabs else None)
        if ws_url is None:
            version = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{PORT_ITAU}/json/version").read())
            ws_url = version.get("webSocketDebuggerUrl")
        else:
            ws_url = ws_url["webSocketDebuggerUrl"]
        return _ws_mod.create_connection(ws_url, timeout=15)

    def _cdp_cmd(ws, method, params=None, msg_id=1):
        ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})

    # ── Captura cookies ───────────────────────────────────────────────────────
    try:
        ws = _cdp_connect()
        try:
            result   = _cdp_cmd(ws, "Storage.getCookies", msg_id=1)
            cookies  = result.get("cookies", [])
        finally:
            ws.close()
    except Exception as e:
        print(f"  ERRO ao extrair cookies: {e}")
        return False

    itaubba_cookies = [
        c for c in cookies
        if any(domain_matches(c.get("domain", ""), d) for d in _ITAUBBA_DOMAINS)
    ]

    if not itaubba_cookies:
        print("  ERRO: Nenhum cookie do Itaú encontrado. Você fez login?")
        return False

    pw_cookies = []
    for c in itaubba_cookies:
        pw_cookies.append({
            "name":     c.get("name", ""),
            "value":    c.get("value", ""),
            "domain":   c.get("domain", ""),
            "path":     c.get("path", "/"),
            "expires":  c.get("expires", -1),
            "httpOnly": c.get("httpOnly", False),
            "secure":   c.get("secure", False),
            "sameSite": c.get("sameSite", "Lax") or "Lax",
        })

    state = {"cookies": pw_cookies, "origins": []}
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"  Salvo: {state_file} ({len(pw_cookies)} cookies)")

    # ── Captura lista de relatórios direto da aba aberta (Chrome real) ────────
    # Executa o mesmo JS do scraper mas no Chrome real — sem risco de bloqueio.
    print("  Extraindo lista de relatórios da página atual...")
    _DOM_JS = r"""
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

    reports_cache = []
    try:
        ws = _cdp_connect()
        try:
            r = _cdp_cmd(ws, "Runtime.evaluate",
                         {"expression": _DOM_JS, "returnByValue": True}, msg_id=2)
            raw = r.get("result", {}).get("value", "[]")
            reports_cache = json.loads(raw) if isinstance(raw, str) else []
        finally:
            ws.close()
    except Exception as e:
        print(f"  Aviso: não foi possível extrair relatórios da página: {e}")

    # Salva cache de relatórios para uso pelo scraper (sem precisar do Playwright)
    cache_file = state_file.parent / "itaubba_reports_cache.json"
    cache_data = {
        "captured_at": __import__("datetime").datetime.now().isoformat(),
        "reports": reports_cache[:20],   # guarda até 20
    }
    cache_file.write_text(json.dumps(cache_data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Cache de relatórios: {cache_file} ({len(reports_cache)} relatório(s) encontrado(s))")

    if not reports_cache:
        print()
        print("  ATENÇÃO: nenhum relatório encontrado na página.")
        print("  Certifique-se de que a página do analista está aberta e carregada")
        print("  antes de apertar ENTER na próxima vez.")
        return True

    # Salva direto no banco — sem precisar rodar save_publications.py separadamente
    try:
        import sys, os
        sys.path.insert(0, str(BASE_DIR))
        from newshunter.itaubba_scraper import collect_itaubba_publications
        from newshunter.store import save_publications
        pubs = collect_itaubba_publications()
        if pubs:
            save_publications([
                {"title": p.title, "pdf_url": p.pdf_url, "report_url": p.report_url}
                for p in pubs
            ])
            print(f"  Banco atualizado: {len(pubs)} publicação(ões) salva(s).")
        else:
            print("  Aviso: nenhuma publicação salva no banco (cache vazio).")
    except Exception as e:
        print(f"  Aviso: não foi possível salvar no banco automaticamente: {e}")
        print(f"  Execute 'python save_publications.py' manualmente se necessário.")

    return True


def renew_estadao_via_chrome() -> bool:
    """Login do Estadão usando Chrome real (anti-bot bypass para Akamai).

    Abre Chrome com debugging port, usuário loga em acesso.estadao.com.br,
    cookies são extraídos via CDP e salvos como Playwright state.
    """
    PORT_EST = 9224   # porta separada para evitar conflito com Valor (9222) e Itaú (9223)

    def _up_est() -> bool:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT_EST}/json/version", timeout=1)
            return True
        except Exception:
            return False

    def _wait_est(timeout: int = 25) -> bool:
        for _ in range(timeout * 2):
            if _up_est():
                return True
            time.sleep(0.5)
        return False

    chrome = find_chrome()
    if not chrome:
        print("  ERRO: Chrome.exe não encontrado. Pulando Estadão.")
        return False

    state_file = ESTADAO_STATE_FILE
    state_file.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*65}")
    print("  Site: Estadão (acesso.estadao.com.br)")
    print("  URL:  https://www.estadao.com.br/")
    print(f"{'='*65}")
    print("  Abrindo Chrome real para evitar bloqueio do Akamai CDN...")
    print()

    if _up_est():
        print("  Chrome (porta 9224) já está aberto — reusando instância.")
    else:
        profile_dir = BASE_DIR / "chrome_profile_estadao"
        profile_dir.mkdir(exist_ok=True)
        args = [
            chrome,
            f"--remote-debugging-port={PORT_EST}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-allow-origins=*",
            "https://www.estadao.com.br/",
        ]
        DETACHED = 0x00000008 if os.name == "nt" else 0
        subprocess.Popen(args, creationflags=DETACHED, close_fds=True)

        print("  Aguardando Chrome abrir...")
        if not _wait_est():
            print("  ERRO: Chrome não abriu. Verifique se o Chrome está instalado.")
            return False
        print("  Chrome aberto!")

    print()
    print("  >>> O Chrome abriu o site do Estadão.")
    print("  >>> Clique em 'Entrar' e faça login com suas credenciais.")
    print("  >>> Quando estiver na página principal (logado), volte aqui")
    print("  >>> e aperte ENTER.")
    print()
    input("  [ENTER após logar] --> ")

    # Extrai cookies via CDP
    import websocket as _ws_mod

    def _cdp_connect_est():
        tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT_EST}/json").read())
        page_tabs = [t for t in tabs if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        best = next((t for t in page_tabs if "estadao.com.br" in t.get("url", "")), None)
        ws_url = (best or (page_tabs[0] if page_tabs else None))
        if ws_url is None:
            version = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{PORT_EST}/json/version").read())
            ws_url = version.get("webSocketDebuggerUrl")
        else:
            ws_url = ws_url["webSocketDebuggerUrl"]
        return _ws_mod.create_connection(ws_url, timeout=15)

    try:
        ws = _cdp_connect_est()
        try:
            ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == 1:
                    if "error" in msg:
                        raise RuntimeError(msg["error"])
                    all_cookies = msg["result"]["cookies"]
                    break
        finally:
            ws.close()
    except Exception as e:
        print(f"  ERRO ao extrair cookies: {e}")
        return False

    estadao_cookies = [
        c for c in all_cookies
        if domain_matches(c.get("domain", ""), "estadao.com.br")
        or domain_matches(c.get("domain", ""), "acesso.estadao.com.br")
    ]

    if not estadao_cookies:
        print("  ERRO: Nenhum cookie do Estadão encontrado. Você fez login?")
        return False

    pw_cookies = []
    for c in estadao_cookies:
        pw_cookies.append({
            "name":     c.get("name", ""),
            "value":    c.get("value", ""),
            "domain":   c.get("domain", ""),
            "path":     c.get("path", "/"),
            "expires":  c.get("expires", -1),
            "httpOnly": c.get("httpOnly", False),
            "secure":   c.get("secure", False),
            "sameSite": c.get("sameSite", "Lax") or "Lax",
        })

    state = {"cookies": pw_cookies, "origins": []}
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"  Salvo: {state_file}")
    print(f"  Cookies: {len(pw_cookies)} (Estadão/acesso)")
    return True


def renew_valor_via_chrome() -> bool:
    """Login do Valor Econômico usando Chrome real (anti-bot bypass).

    Abre Chrome com debugging port, usuário loga normalmente,
    cookies são extraídos via CDP e salvos como Playwright state.
    """
    chrome = find_chrome()
    if not chrome:
        print("  ERRO: Chrome.exe não encontrado. Pulando Valor.")
        return False

    state_file = VALOR_STATE_FILE
    state_file.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*65}")
    print("  Site: Valor Econômico (Globo ID)")
    print("  URL:  https://valor.globo.com/")
    print(f"{'='*65}")
    print("  Abrindo Chrome real (não Chromium) para evitar bloqueio...")
    print()

    # Mata instâncias Chrome com debug port aberta (evita conflito)
    if debugger_up():
        print("  Chrome já está rodando com debug port. Usando essa instância.")
    else:
        profile_dir = BASE_DIR / "chrome_profile_valor"
        profile_dir.mkdir(exist_ok=True)
        args = [
            chrome,
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-allow-origins=*",
            "https://valor.globo.com/",
        ]
        DETACHED = 0x00000008 if os.name == "nt" else 0
        subprocess.Popen(args, creationflags=DETACHED, close_fds=True)

        print("  Aguardando Chrome abrir...")
        if not wait_debugger(timeout=20):
            print("  ERRO: Chrome não abriu a porta de debug.")
            return False

    print()
    print("  >>> Faça login no Valor Econômico no Chrome que abriu.")
    print("  >>> Quando estiver na página principal (logado), aperte ENTER aqui.")
    print()
    input("  [ENTER após logar] --> ")

    # Extrai cookies via CDP
    try:
        cookies = cdp_get_all_cookies()
    except Exception as e:
        print(f"  ERRO ao extrair cookies: {e}")
        return False

    valor_cookies = [
        c for c in cookies
        if domain_matches(c.get("domain", ""), "globo.com")
        or domain_matches(c.get("domain", ""), "valor.globo.com")
    ]

    if not valor_cookies:
        print("  ERRO: Nenhum cookie do Valor/Globo encontrado. Você fez login?")
        return False

    # Converte para formato Playwright storage_state
    pw_cookies = []
    for c in valor_cookies:
        domain = c.get("domain", "")
        pw_cookies.append({
            "name": c.get("name", ""),
            "value": c.get("value", ""),
            "domain": domain,
            "path": c.get("path", "/"),
            "expires": c.get("expires", -1),
            "httpOnly": c.get("httpOnly", False),
            "secure": c.get("secure", False),
            "sameSite": c.get("sameSite", "Lax") or "Lax",
        })

    state = {"cookies": pw_cookies, "origins": []}
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"  Salvo: {state_file}")
    print(f"  Cookies: {len(pw_cookies)} (Globo/Valor)")
    return True


def run_playwright_login() -> None:
    """Fluxo principal para renovar sessões via Playwright."""
    print("=" * 65)
    print("  Renovação de sessão — Fastmarkets, Platts, Estadão, Valor e Itaú BBA")
    print("=" * 65)
    print()
    print("Este script abrirá um browser para cada site.")
    print("Faça login normalmente em cada um. O estado será salvo automaticamente.")
    print()

    results = {}

    # Fastmarkets e Platts via Playwright Chromium
    for target in PLAYWRIGHT_TARGETS:
        ok = renew_playwright_state(target)
        results[target["domain"]] = ok
        print()

    # Estadão, Valor e Itaú BBA via Chrome real (Playwright Chromium bloqueado pelo Akamai/SSO)
    ok_estadao = renew_estadao_via_chrome()
    results["www.estadao.com.br"] = ok_estadao
    print()

    ok_valor = renew_valor_via_chrome()
    results["valor.globo.com"] = ok_valor
    print()

    ok_itaubba = renew_itaubba_via_chrome()
    results["www.itau.com.br"] = ok_itaubba
    print()

    print("=" * 65)
    print("  Resumo:")
    for domain, ok in results.items():
        status = "OK" if ok else "FALHOU"
        print(f"  [{status}] {domain}")
    print()
    if all(results.values()):
        print("  Sessões renovadas com sucesso!")
        print("  Os scrapers usarão os novos cookies nas próximas coletas.")
    else:
        print("  Alguns sites falharam — verifique se fez login completo.")
    print("=" * 65)


# ═══════════════════════════════════════════════════════════════════════════════
# MODO LEGADO — Chrome DevTools Protocol (Netscape cookies)
# ═══════════════════════════════════════════════════════════════════════════════

def find_chrome() -> str | None:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def debugger_up() -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1)
        return True
    except Exception:
        return False


def launch_chrome(urls: list[str]) -> None:
    chrome = find_chrome()
    if not chrome:
        sys.exit("ERRO: Chrome.exe nao encontrado. Voce tem o Chrome instalado?")
    PROFILE_DIR.mkdir(exist_ok=True)
    args = [
        chrome,
        f"--remote-debugging-port={PORT}",
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--remote-allow-origins=*",
        *urls,
    ]
    DETACHED = 0x00000008 if os.name == "nt" else 0
    subprocess.Popen(args, creationflags=DETACHED, close_fds=True)


def wait_debugger(timeout: int = 25) -> bool:
    for _ in range(timeout * 2):
        if debugger_up():
            return True
        time.sleep(0.5)
    return False


def cdp_get_all_cookies() -> list[dict]:
    import websocket  # websocket-client
    tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json").read())
    page_tabs = [t for t in tabs if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not page_tabs:
        version = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version").read())
        ws_url = version.get("webSocketDebuggerUrl")
        if not ws_url:
            sys.exit("ERRO: nenhuma aba aberta e sem browser-level WS")
    else:
        ws_url = page_tabs[0]["webSocketDebuggerUrl"]

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


def domain_matches(cookie_domain: str, target: str) -> bool:
    cd = cookie_domain.lstrip(".").lower()
    t = target.lower()
    return cd == t or cd.endswith("." + t) or t.endswith("." + cd)


def save_netscape(cookies: list[dict], target: str) -> int:
    relevant = [c for c in cookies if domain_matches(c.get("domain", ""), target)]
    if not relevant:
        return 0
    COOKIES_DIR.mkdir(exist_ok=True)
    out = COOKIES_DIR / f"{target}.txt"
    lines = ["# Netscape HTTP Cookie File",
             "# Gerado por login.py"]
    for c in relevant:
        cdomain = c["domain"] if c["domain"].startswith(".") else "." + c["domain"]
        incl_sub = "TRUE"
        path = c.get("path") or "/"
        secure = "TRUE" if c.get("secure") else "FALSE"
        expires = int(c.get("expires") or 0)
        if expires <= 0:
            expires = int(time.time()) + 60 * 60 * 24 * 30
        name = c.get("name", "")
        value = c.get("value") or ""
        lines.append(f"{cdomain}\t{incl_sub}\t{path}\t{secure}\t{expires}\t{name}\t{value}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(relevant)


def run_legacy_login() -> None:
    targets = DEFAULT_TARGETS
    print("=" * 70)
    print(" Extrator de cookies via Chrome DevTools Protocol (modo legado)")
    print("=" * 70)

    if debugger_up():
        print(f"\nChrome ja esta rodando com debug port {PORT}. Usando essa instancia.")
    else:
        print(f"\nAbrindo Chrome com perfil dedicado em: {PROFILE_DIR}")
        launch_chrome([u for _, u in targets])
        print("Aguardando Chrome subir...")
        if not wait_debugger():
            sys.exit("ERRO: Chrome nao abriu a porta de debug.")

    print()
    print(f">>> Faca login em: {', '.join(d for d, _ in targets)}")
    print()
    input("Aperte ENTER quando tiver logado em todos ---> ")

    print("\nExtraindo cookies via CDP...")
    try:
        all_cookies = cdp_get_all_cookies()
    except Exception as e:
        sys.exit(f"ERRO ao buscar cookies: {e}")

    print(f"Total de cookies no browser: {len(all_cookies)}")
    total = 0
    for domain, _ in targets:
        n = save_netscape(all_cookies, domain)
        status = "OK" if n > 0 else "NENHUM cookie"
        print(f"  [{domain}] {status} - {n} cookie(s)")
        total += n

    if total == 0:
        print("\nNada foi salvo. Confira que voce esta logado nos sites.")
    else:
        print(f"\nOK - {total} cookie(s) salvos em {COOKIES_DIR}")


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def run_single_target(domain_key: str) -> None:
    """Renova sessão de um único site pelo domínio (ex: 'itaubba', 'fastmarkets', 'platts')."""
    key = domain_key.lower()

    if key == "itaubba":
        ok = renew_itaubba_via_chrome()
        print()
        print("=" * 65)
        print(f"  {'OK — sessão salva!' if ok else 'FALHOU — tente novamente.'}")
        print("=" * 65)
        return

    if key == "estadao":
        ok = renew_estadao_via_chrome()
        print()
        print("=" * 65)
        print(f"  {'OK — sessão salva!' if ok else 'FALHOU — tente novamente.'}")
        print("=" * 65)
        return

    if key == "valor":
        ok = renew_valor_via_chrome()
        print("OK" if ok else "FALHOU")
        return

    alias = {
        "fastmarkets": "dashboard.fastmarkets.com",
        "platts":      "core.spglobal.com",
    }
    domain = alias.get(key, key)
    target = next((t for t in PLAYWRIGHT_TARGETS if t["domain"] == domain), None)
    if target is None:
        print(f"ERRO: site '{domain_key}' não encontrado. Opções: itaubba, fastmarkets, platts, valor")
        return

    print("=" * 65)
    print(f"  Renovando apenas: {target['label']}")
    print("=" * 65)
    ok = renew_playwright_state(target)
    print()
    print("=" * 65)
    print(f"  {'OK — sessão salva!' if ok else 'FALHOU — tente novamente.'}")
    print("=" * 65)


def main() -> int:
    args = sys.argv[1:]

    if "--legacy" in args:
        run_legacy_login()
        return 0

    # Flags de site único: --itaubba, --fastmarkets, --platts, --valor, --estadao
    single_flags = {"--itaubba", "--fastmarkets", "--platts", "--valor", "--estadao"}
    matched = [a for a in args if a in single_flags]
    if matched:
        run_single_target(matched[0].lstrip("-"))
        return 0

    run_playwright_login()
    return 0


if __name__ == "__main__":
    sys.exit(main())
