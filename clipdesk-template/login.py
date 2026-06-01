"""Abre Chrome com porta de debug, você loga, script extrai cookies via DevTools Protocol.

Sem admin, sem descriptografia. Usa um perfil dedicado em ./chrome_profile.

Uso:
    python login.py <dominio1> [<dominio2> ...]

Exemplo:
    python login.py exemplo.com.br outro.com

Fluxo:
  1. Se o Chrome já estiver aberto na porta 9222, pula para (4).
  2. Abre Chrome com perfil dedicado + porta 9222.
  3. Você loga nos sites nas abas que abrirem.
  4. Volta aqui, aperta ENTER. Os cookies são extraídos.

Na próxima vez só precisa rodar de novo: o perfil persiste, então você
continua logado — basta apertar ENTER pra reexportar os cookies.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websocket  # websocket-client

BASE_DIR = Path(__file__).parent
PROFILE_DIR = BASE_DIR / "chrome_profile"
COOKIES_DIR = BASE_DIR / "cookies"
PORT = 9222


def find_chrome() -> str | None:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
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
        sys.exit("ERRO: Chrome.exe não encontrado. Você tem o Chrome instalado?")
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


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        print("Uso: python login.py <dominio1> [<dominio2> ...]")
        print("Ex.: python login.py exemplo.com.br outro.com")
        return 2

    targets = [(d, f"https://{d}/") for d in argv]

    print("=" * 70)
    print(" Extrator de cookies via Chrome DevTools Protocol")
    print("=" * 70)

    if debugger_up():
        print(f"\nChrome já está rodando com debug port {PORT}. Usando essa instância.")
    else:
        print(f"\nAbrindo Chrome com perfil dedicado em: {PROFILE_DIR}")
        print(f"(URLs: {', '.join(u for _, u in targets)})\n")
        launch_chrome([u for _, u in targets])
        print("Aguardando Chrome subir...")
        if not wait_debugger():
            sys.exit("ERRO: Chrome não abriu a porta de debug. Tem outra instância do Chrome aberta com esse perfil?")

    print()
    print(">>> Agora no Chrome que abriu (janela nova com perfil dedicado):")
    print(f">>> Faça login em cada um dos sites: {', '.join(d for d, _ in targets)}")
    print(">>> Quando terminar, volte aqui.")
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
        status = "OK" if n > 0 else "NENHUM cookie (você logou neste site?)"
        print(f"  [{domain}] {status} - {n} cookie(s)")
        total += n

    if total == 0:
        print("\nNada foi salvo. Confira que você está logado nos sites.")
        return 1

    print(f"\nOK - {total} cookie(s) salvos em {COOKIES_DIR}")
    print("Pode fechar o Chrome. Da próxima vez é só rodar de novo (até expirar).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
