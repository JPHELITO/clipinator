"""Scraper Playwright para artigos do Valor Econômico.

Fluxo em duas etapas:

Etapa 1 — Sitemap (urllib, sem Playwright):
  • Busca https://valor.globo.com/sitemap/valor/news.xml
  • Extrai (url, title, pub_date) de todos os artigos
  • Filtra pela janela de 72h e usa keyword matching para limitar candidatos

Etapa 2 — Corpo (Playwright, artigo-a-artigo):
  • Navega para cada URL com sessão autenticada (valor_state.json)
  • Extrai .content-text (lide) + .wall (corpo completo) para artigos free
  • Detecta paywall: ausência da classe "no-paywall" indica artigo pago
  • Em artigos pagos, tenta auto-login e recarrega

Auto-login:
  • Se a sessão expirar, reloga via login.globo.com/login/438
    usando credenciais em valor_credentials.json
  • Para configurar: python login.py (salva credenciais ao final do login manual)
  • Ou crie manualmente: news_generator/cookies/valor_credentials.json
    {"email": "seu@email.com", "password": "sua_senha"}

Seletores confirmados (debug_valor2.py / debug_valor3.py):
  • .content-text   — parágrafo de abertura / lide
  • .wall           — corpo do artigo (visível quando no-paywall presente)
  • [class*="no-paywall"]  — presente = artigo gratuito
  • [class*="paywall__wall"] — presente = muro de paywall ativo
"""
from __future__ import annotations

import html as _html
import json
import logging
import re
import threading
import time as _time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_STATE_FILE = (
    Path(__file__).resolve().parent.parent.parent
    / "news_generator"
    / "cookies"
    / "valor_state.json"
)

# Credenciais para auto-login (arquivo criado pelo login.py ou manualmente)
_CREDENTIALS_FILE = _STATE_FILE.parent / "valor_credentials.json"

_SITEMAP_URL = "https://valor.globo.com/sitemap/valor/news.xml"

# Janela de busca — alinhada com a retenção de 72h do projeto
_MAX_AGE_HOURS = 72

# Máximo de artigos com corpo completo buscado no Playwright
_MAX_BODY_FETCH = 12

# Budget de tempo (segundos) para a fase Playwright completa
_PHASE2_BUDGET = 150

# Máximo de tentativas de auto-login por sessão de scraping
_MAX_LOGIN_ATTEMPTS = 2

# Namespaces do sitemap Valor
_NS = {
    "n":    "http://www.sitemaps.org/schemas/sitemap/0.9",
    "news": "http://www.google.com/schemas/sitemap-news/0.9",
}


def _html_to_text(h: str) -> str:
    text = re.sub(r"<[^>]+>", " ", h)
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


# Marcadores de paywall — texto presente quando o usuário não está autenticado.
# Usado para descartar corpo que é na verdade a tela de assinatura.
_PAYWALL_MARKERS = (
    "precisa ser um assinante",
    "assine agora",
    "conteúdo exclusivo para assinantes",
    "para continuar lendo",
    "seja assinante",
    "acesso exclusivo",
)


def _is_paywall_content(text: str) -> bool:
    """True se o texto parece ser a página de paywall (não o artigo real)."""
    t = text.lower()
    return any(marker in t for marker in _PAYWALL_MARKERS)


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


# ─── Sitemap ──────────────────────────────────────────────────────────────────

def _fetch_sitemap() -> list[dict]:
    """Busca sitemap e retorna lista de {url, title, pub_date} filtrada por 72h."""
    try:
        req = urllib.request.Request(
            _SITEMAP_URL,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        xml_bytes = urllib.request.urlopen(req, timeout=20).read()
        root = ET.fromstring(xml_bytes)
    except Exception as e:
        log.warning("valor_scraper: erro ao buscar sitemap: %s", e)
        return []

    now = datetime.now(timezone.utc)
    results: list[dict] = []
    for url_el in root.findall("n:url", _NS):
        loc   = url_el.findtext("n:loc", namespaces=_NS) or ""
        title = url_el.findtext("news:news/news:title", namespaces=_NS) or ""
        pub   = url_el.findtext("news:news/news:publication_date", namespaces=_NS) or ""

        if not loc.endswith(".ghtml"):
            continue

        pub_dt = _parse_date(pub)
        if pub_dt is not None:
            age_h = (now - pub_dt).total_seconds() / 3600
            if age_h > _MAX_AGE_HOURS:
                continue

        results.append({"url": loc, "title": title, "pub": pub_dt})

    log.info("valor_scraper: sitemap → %d artigos nas últimas %dh", len(results), _MAX_AGE_HOURS)
    return results


# ─── Auto-login ───────────────────────────────────────────────────────────────

def _load_credentials() -> dict | None:
    """Carrega email/password do arquivo de credenciais, se existir."""
    if not _CREDENTIALS_FILE.exists():
        return None
    try:
        creds = json.loads(_CREDENTIALS_FILE.read_text(encoding="utf-8"))
        if creds.get("email") and creds.get("password"):
            return creds
    except Exception:
        pass
    return None


def _is_login_page(page) -> bool:
    """True se a página atual é a tela de autenticação do Globo."""
    url = page.url.lower()
    return (
        "id.globo.com" in url
        or "login.globo.com" in url
        or "/login" in url
        or "contas.globo.com" in url
    )


def _do_auto_login(page, ctx) -> bool:
    """Executa login automático no Valor Econômico via Globo ID.

    Tenta login.globo.com/login/438 (serviço Valor) com credenciais salvas.
    Após sucesso, salva o novo state e retorna True.
    Retorna False se credenciais não disponíveis ou login falhar.
    """
    creds = _load_credentials()
    if not creds:
        log.warning(
            "valor_scraper: auto-login indisponível — sem credenciais. "
            "Execute 'python login.py' para configurar."
        )
        return False

    email    = creds["email"]
    password = creds["password"]
    log.info("valor_scraper: tentando auto-login para %s...", email)

    try:
        # Navega para o formulário de login do Globo (serviço Valor = 438)
        login_url = "https://login.globo.com/login/438"
        page.goto(login_url, wait_until="domcontentloaded", timeout=25_000)
        page.wait_for_timeout(3_000)  # aguarda JS renderizar o form

        # Seletores possíveis do formulário Globo ID
        email_sel   = "input[id='login'], input[name='login'], input[type='email'], input[id='email']"
        pass_sel    = "input[id='password'], input[name='password'], input[type='password']"
        submit_sel  = "button#loginButton, button[type='submit'], input[type='submit']"

        # Verifica se o form renderizou
        try:
            page.wait_for_selector(email_sel, timeout=8_000)
        except Exception:
            log.warning("valor_scraper: form de login não encontrado em %s", login_url)
            return False

        # Preenche email / login
        page.fill(email_sel, email)
        page.wait_for_timeout(500)

        # Preenche senha
        try:
            page.wait_for_selector(pass_sel, timeout=5_000)
        except Exception:
            pass
        page.fill(pass_sel, password)
        page.wait_for_timeout(500)

        # Submete o formulário
        page.click(submit_sel)

        # Aguarda redirect de volta para Valor (até 25s)
        try:
            page.wait_for_url("https://valor.globo.com/**", timeout=25_000)
        except Exception:
            page.wait_for_timeout(4_000)
            if _is_login_page(page):
                log.warning(
                    "valor_scraper: auto-login falhou — credenciais incorretas ou "
                    "formulário não submetido. Execute 'python login.py' para renovar."
                )
                return False

        # Aguarda inicialização
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            page.wait_for_timeout(3_000)

        if _is_login_page(page):
            log.warning("valor_scraper: auto-login falhou — ainda na página de login.")
            return False

        # Salva o novo state
        new_state = ctx.storage_state()
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(new_state), encoding="utf-8")
        log.info(
            "valor_scraper: auto-login OK — novo state salvo (%d cookies).",
            len(new_state.get("cookies", [])),
        )
        return True

    except Exception as e:
        log.warning("valor_scraper: auto-login erro: %s", e)
        return False


# ─── Extração de corpo ────────────────────────────────────────────────────────

def _extract_body(page) -> tuple[str, bool]:
    """Extrai corpo do artigo da página atual.

    Retorna (html_corpo, is_paywalled).
    html_corpo é HTML seguro; string vazia se falhar.
    is_paywalled=True quando paywall está ativo (não há no-paywall).
    """
    from .html_utils import article_to_safe_html

    try:
        dom_info: str = page.evaluate("""() => {
            const noPaywall  = !!document.querySelector('[class*="no-paywall"]');
            const hasPaywall = !!document.querySelector('[class*="paywall__wall"]');

            function getHtml(sel) {
                const el = document.querySelector(sel);
                return el ? el.innerHTML.trim() : '';
            }
            return JSON.stringify({
                no_paywall:   noPaywall,
                has_paywall:  hasPaywall,
                content_text: getHtml('.content-text'),
                wall:         getHtml('.wall'),
            });
        }""")
        data = json.loads(dom_info)
    except Exception as e:
        log.debug("valor_scraper: erro ao avaliar DOM: %s", e)
        return "", False

    no_paywall  = data.get("no_paywall", False)
    has_paywall = data.get("has_paywall", False)

    if has_paywall and not no_paywall:
        return "", True  # paywall ativo

    parts: list[str] = []

    ct = data.get("content_text", "")
    if ct and len(ct) > 80:
        clean = article_to_safe_html(ct)
        if clean:
            parts.append(clean)

    wall = data.get("wall", "")
    if wall and len(wall) > 80:
        clean = article_to_safe_html(wall)
        if clean:
            parts.append(clean)

    combined = "\n".join(parts)
    return combined, False


# ─── Worker Playwright ────────────────────────────────────────────────────────

def _scrape_worker(sitemap_items: list[dict]) -> list[dict]:
    """Navega os artigos via Playwright e extrai corpos.

    sitemap_items: lista de {url, title, pub} já filtrada por data/keywords.
    Retorna lista de dicts com os campos Article prontos.
    """
    from playwright.sync_api import sync_playwright

    results: list[dict] = []
    login_attempts = 0

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                    channel="chrome",
                )
            except Exception:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )

            ctx_kwargs: dict = {
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "viewport": {"width": 1440, "height": 900},
                "locale": "pt-BR",
            }
            if _STATE_FILE.exists():
                ctx_kwargs["storage_state"] = str(_STATE_FILE)

            ctx = browser.new_context(**ctx_kwargs)
            ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = ctx.new_page()

            try:
                to_fetch = sitemap_items[:_MAX_BODY_FETCH]
                phase_start = _time.time()
                n_ok = 0

                for i, meta in enumerate(to_fetch):
                    if _time.time() - phase_start > _PHASE2_BUDGET:
                        log.info("valor_scraper: budget esgotado após %d artigos", i)
                        break

                    url = meta["url"]
                    title = meta.get("title", "")
                    pub_dt = meta.get("pub")

                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=25_000)

                        # Detecta redirect para login
                        if _is_login_page(page):
                            login_attempts += 1
                            if login_attempts > _MAX_LOGIN_ATTEMPTS:
                                log.warning(
                                    "valor_scraper: sessão expirada após %d tentativas.",
                                    _MAX_LOGIN_ATTEMPTS,
                                )
                                break
                            log.info(
                                "valor_scraper: sessão expirada — auto-login "
                                "(tentativa %d/%d)...",
                                login_attempts, _MAX_LOGIN_ATTEMPTS,
                            )
                            if not _do_auto_login(page, ctx):
                                break
                            # Retenta o artigo atual
                            page.goto(url, wait_until="domcontentloaded", timeout=25_000)
                            if _is_login_page(page):
                                log.warning(
                                    "valor_scraper: re-login não resolveu. "
                                    "Pulando artigo %d.", i + 1
                                )
                                continue

                        # Aguarda carregamento do conteúdo
                        try:
                            page.wait_for_selector(
                                ".content-text, .wall, article",
                                timeout=10_000,
                            )
                        except Exception:
                            page.wait_for_timeout(2_000)

                        body_html, is_paywalled = _extract_body(page)

                        if is_paywalled and login_attempts < _MAX_LOGIN_ATTEMPTS:
                            # Tenta login para desbloquear
                            login_attempts += 1
                            log.info(
                                "valor_scraper: artigo paywalled — tentando login "
                                "(tentativa %d/%d)...",
                                login_attempts, _MAX_LOGIN_ATTEMPTS,
                            )
                            if _do_auto_login(page, ctx):
                                page.goto(url, wait_until="domcontentloaded", timeout=25_000)
                                try:
                                    page.wait_for_selector(
                                        ".content-text, .wall, article",
                                        timeout=10_000,
                                    )
                                except Exception:
                                    page.wait_for_timeout(2_000)
                                body_html, is_paywalled = _extract_body(page)

                        # Fallback genérico só roda se NÃO estiver paywalled.
                        # Se paywalled, qualquer extração de <p> capturaria a tela
                        # "Você precisa ser assinante" — que não é o artigo.
                        if not body_html and not is_paywalled:
                            try:
                                paras = page.evaluate("""() => {
                                    const els = document.querySelectorAll('article p, .content-text p, .wall p');
                                    return Array.from(els)
                                        .map(e => (e.innerText || '').trim())
                                        .filter(t => t.length > 40)
                                        .slice(0, 20);
                                }""")
                                if paras:
                                    import html as _html_mod
                                    body_html = "\n".join(
                                        f"<p>{_html_mod.escape(p)}</p>"
                                        for p in paras
                                    )
                            except Exception:
                                pass

                        # Segunda camada: descarta corpo que é texto de paywall
                        if body_html and _is_paywall_content(body_html):
                            log.debug(
                                "valor_scraper: corpo descartado (paywall text): %s",
                                url[-60:],
                            )
                            body_html = ""

                        if body_html and len(body_html) > 80:
                            n_ok += 1
                            log.debug(
                                "valor_scraper: artigo %d OK (%d chars): %s",
                                i + 1, len(body_html), url[-60:],
                            )
                        else:
                            log.debug(
                                "valor_scraper: artigo %d sem corpo: %s",
                                i + 1, url[-60:],
                            )

                        # Snippet para exibição
                        snippet = _html_to_text(body_html)[:360] if body_html else ""

                        results.append({
                            "url": url,
                            "title": title,
                            "body": body_html,
                            "snippet": snippet,
                            "published_at": pub_dt,
                        })

                    except Exception as e:
                        log.debug("valor_scraper: erro em %s: %s", url, e)

                log.info(
                    "valor_scraper: %d/%d artigos com corpo extraído",
                    n_ok, len(to_fetch),
                )

            except Exception as e:
                log.warning("valor_scraper: erro de navegação: %s", e)
            finally:
                try:
                    page.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass

    except Exception as e:
        log.warning("valor_scraper: erro geral do Playwright: %s", e)

    return results


# ─── Entry point público ──────────────────────────────────────────────────────

def collect_valor_articles() -> list:
    """Coleta artigos do Valor Econômico com filtro de keywords. Retorna lista de Article.

    Fluxo:
      1. Busca sitemap (urllib, rápido).
      2. Filtra pelos keywords configurados para valor.globo.com (aba Fontes).
         Fallback para keywords globais se não houver config específica.
      3. Passa apenas os candidatos para o Playwright (extração de corpo).
      4. Persiste os artigos com os keywords que de fato casaram.
    """
    from .filter import matches_keywords
    from .store import Article, get_all_source_keywords, get_config

    # Etapa 1: sitemap (sem Playwright, rápido)
    sitemap_items = _fetch_sitemap()
    if not sitemap_items:
        log.info("valor_scraper: nenhum artigo no sitemap — abortando")
        return []

    # Etapa 2: keyword filtering no título
    source_kw_map = get_all_source_keywords()
    keywords: list[str] = (
        source_kw_map.get("valor.globo.com")
        or get_config()["keywords"]
    )

    # mode=all: aceita tudo (keyword = ["*"])
    use_all = keywords == ["*"]

    candidates: list[dict] = []  # {"url", "title", "pub", "matched"}
    for item in sitemap_items:
        title = item.get("title", "")
        if use_all:
            matched: list[str] = ["#valor"]
        else:
            matched = matches_keywords(title, keywords) or []
        if matched:
            candidates.append({**item, "matched": matched})

    log.info(
        "valor_scraper: %d/%d artigos passaram no filtro de keywords",
        len(candidates), len(sitemap_items),
    )

    if not candidates:
        log.info("valor_scraper: nenhum artigo casou com os keywords configurados")
        return []

    # Etapa 3: corpo via Playwright para os candidatos filtrados
    result: list[dict] = []
    err: list[Exception | None] = [None]

    def _run():
        try:
            result.extend(_scrape_worker(candidates))
        except Exception as e:
            err[0] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    # sitemap ~5s + goto por artigo ~5s * 12 + overhead login ~25s = ~190s
    t.join(timeout=220)

    if err[0]:
        log.warning("valor_scraper: falhou com %s", err[0])

    now = datetime.now(timezone.utc)

    # Indexa resultados do Playwright por URL para lookup rápido
    pw_results: dict[str, dict] = {r["url"]: r for r in result}

    articles: list[Article] = []
    for cand in candidates:
        url    = cand["url"]
        title  = cand.get("title", "")
        pub_dt = cand.get("pub")
        matched = cand.get("matched", ["#valor"])

        # Corpo: obtido pelo Playwright (se disponível) ou vazio
        pw = pw_results.get(url, {})
        body    = pw.get("body", "")
        snippet = pw.get("snippet", "")

        # Snippet mínimo: usa título se Playwright não produziu nada
        if not snippet and title:
            snippet = title[:360]

        if not title and not body:
            continue

        articles.append(Article(
            url=url,
            domain="valor.globo.com",
            source_name="Valor Econômico",
            title=title,
            snippet=snippet,
            published_at=pub_dt or now,
            found_at=now,
            matched_keywords=matched,
            body=body,
        ))

    log.info("valor_scraper: %d artigos prontos para persistência", len(articles))
    return articles
