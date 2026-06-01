"""Scraper Playwright para artigos do Fastmarkets PP News dashboard.

Fluxo em dois estágios:

Fase 1 — Listagem (intercepta POST /search/v3/query):
  • Carrega dashboard.fastmarkets.com/w/.../pp-news com fastmarkets_state.json
  • Intercepta a resposta automática do POST /search/v3/query (dispara ao carregar)
  • Coleta IDs, títulos, datas, snippet e HTML de summary de cada artigo

Fase 2 — Corpo completo (navega artigo-a-artigo):
  • Abre cada URL /a/{id} individualmente
  • Captura window.location.href (URL canônica com slug)
  • Lê innerHTML do container do artigo via DOM walk estruturado
  • Processa com article_to_safe_html() (parágrafos + imagens)

Auto-login:
  • Se a sessão expirar em qualquer ponto, o scraper reloga automaticamente
    usando as credenciais em fastmarkets_credentials.json e retoma a coleta.
  • Para configurar: python login.py  (salva credenciais ao final do login manual)
  • Ou crie manualmente: news_generator/cookies/fastmarkets_credentials.json
    {"email": "seu@email.com", "password": "sua_senha"}

URL canônica de cada artigo:
  https://dashboard.fastmarkets.com/a/{RA-ID}/{slug}
  Exemplo: https://dashboard.fastmarkets.com/a/RA250235/middle-east-conflict-daily-summary-may-14
"""
from __future__ import annotations

import html as _html
import json
import logging
import re
import threading
import time as _time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_STATE_FILE = (
    Path(__file__).resolve().parent.parent.parent
    / "news_generator"
    / "cookies"
    / "fastmarkets_state.json"
)

# Credenciais para auto-login (arquivo criado pelo login.py ou manualmente)
_CREDENTIALS_FILE = _STATE_FILE.parent / "fastmarkets_credentials.json"

_DASHBOARD_URL = "https://dashboard.fastmarkets.com/w/rUks4Ah2y8TjDB8L8RtS9L/pp-news"
_LOGIN_URL     = "https://auth.fastmarkets.com/"

# Janela de busca — alinhada com a retenção de 72h do projeto
_MAX_AGE_HOURS = 72

# Comprimento mínimo (chars) de corpo HTML para considerar um artigo "completo".
# Artigos com menos que isso no DB são re-fetchados na Fase 2.
# Artigos genuinamente curtos (PIX index, notas breves) também passam pela Fase 2,
# mas o resultado será igualmente curto — isso é esperado e correto.
_GOOD_BODY_LEN = 600

# Budget de tempo (segundos) para a Fase 2 completa.
# Estimativa: ~12s/artigo (goto + networkidle + content check).
# 25 artigos × 12s = 300s → budget de 250s processa ~20 artigos.
_PHASE2_BUDGET = 250

# Máximo de tentativas de auto-login por sessão de scraping
_MAX_LOGIN_ATTEMPTS = 2


def _html_to_text(h: str) -> str:
    text = re.sub(r"<[^>]+>", " ", h)
    text = _html.unescape(text)
    text = text.replace("�", "")   # remove replacement chars de encoding errado
    return re.sub(r"\s+", " ", text).strip()


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


def _base_article_url(article_id: str) -> str:
    return f"https://dashboard.fastmarkets.com/a/{article_id}"


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
    """True se a página atual é a tela de autenticação."""
    url = page.url.lower()
    if "auth.fastmarkets.com" in url or "/login" in url or "/signin" in url:
        return True
    try:
        title = page.title().lower()
        if "sign in" in title or "log in" in title or "login" in title:
            return True
    except Exception:
        pass
    return False


def _do_auto_login(page, ctx) -> bool:
    """Executa o login automático no Fastmarkets usando credenciais salvas.

    Página deve estar em auth.fastmarkets.com (já redirecionada).
    Após sucesso, salva o novo state e retorna True.
    Retorna False se credenciais não disponíveis ou login falhar.
    """
    creds = _load_credentials()
    if not creds:
        log.warning(
            "fastmarkets_scraper: auto-login indisponível — sem credenciais. "
            "Execute 'python login.py' para configurar."
        )
        return False

    email    = creds["email"]
    password = creds["password"]
    log.info("fastmarkets_scraper: tentando auto-login para %s...", email)

    try:
        # Garante que estamos na página de login
        if not _is_login_page(page):
            page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=20_000)
            page.wait_for_timeout(2_000)

        # Aguarda o formulário aparecer
        page.wait_for_selector("input[name='username'], input[id='userEmail']", timeout=10_000)

        # Preenche email
        page.fill("input[name='username']", email)

        # Preenche senha
        page.wait_for_selector("input[name='password'], input[id='password']", timeout=5_000)
        page.fill("input[name='password']", password)

        # Marca "remember me" para maximizar duração da sessão
        try:
            rm = page.query_selector("input[name='rememberMe']")
            if rm and not rm.is_checked():
                rm.check()
        except Exception:
            pass

        # Submete o formulário
        page.click("button[id='login-button'], button[type='submit']")

        # Aguarda redirect de volta para o dashboard (até 25s)
        try:
            page.wait_for_url("https://dashboard.fastmarkets.com/**", timeout=25_000)
        except Exception:
            # Verifica se ainda está no login (credencial errada?)
            page.wait_for_timeout(3_000)
            if _is_login_page(page):
                log.warning(
                    "fastmarkets_scraper: auto-login falhou — credenciais incorretas ou CAPTCHA. "
                    "Execute 'python login.py' para atualizar."
                )
                return False

        # Aguarda a aplicação inicializar
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            page.wait_for_timeout(5_000)

        if _is_login_page(page):
            log.warning("fastmarkets_scraper: auto-login falhou — redirecionado para login novamente.")
            return False

        # Salva o novo state (cookies + localStorage com novo token OIDC)
        new_state = ctx.storage_state()
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(new_state), encoding="utf-8")
        log.info(
            "fastmarkets_scraper: auto-login OK — novo state salvo (%d cookies).",
            len(new_state.get("cookies", [])),
        )
        return True

    except Exception as e:
        log.warning("fastmarkets_scraper: auto-login erro: %s", e)
        return False


def _check_state_file() -> bool:
    """Verifica se o state file existe. Apenas avisa sobre token expirado — não aborta."""
    if not _STATE_FILE.exists():
        # Sem state file: tenta criar sessão do zero via auto-login
        creds = _load_credentials()
        if creds:
            log.info(
                "fastmarkets_scraper: state file não encontrado — "
                "usará auto-login para criar nova sessão."
            )
            return True  # Deixa o _scrape_worker tentar auto-login
        log.warning(
            "fastmarkets_scraper: state file não encontrado e sem credenciais. "
            "Execute 'python login.py'."
        )
        return False

    try:
        state_data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        for origin in state_data.get("origins", []):
            for item in origin.get("localStorage", []):
                if "oidc.user:" in item.get("name", ""):
                    oidc = json.loads(item["value"])
                    exp = oidc.get("expires_at", 0)
                    if exp:
                        age_h = (datetime.now(timezone.utc).timestamp() - exp) / 3600
                        if age_h > 0:
                            creds_ok = _CREDENTIALS_FILE.exists()
                            log.info(
                                "fastmarkets_scraper: token expirou há %.1fh. "
                                "%s",
                                age_h,
                                "Auto-login configurado — tentará renovar." if creds_ok
                                else "Sem credenciais para auto-login.",
                            )
                        else:
                            log.info(
                                "fastmarkets_scraper: token válido por mais %.1fh.", -age_h
                            )
    except Exception:
        pass

    return True


def _scrape_worker() -> list[dict]:
    """Executa em thread separada com suporte a auto-login.

    Fase 1: intercepta POST /search/v3/query na página do dashboard.
    Fase 2: navega artigo-a-artigo para ler o corpo completo + URL canônica.
    """
    from playwright.sync_api import sync_playwright

    if not _check_state_file():
        return []

    articles_meta: list[dict] = []
    seen_ids: set[str] = set()
    api_fields_logged = False
    login_attempts = 0

    # ─── Handler de respostas (Fase 1) ───────────────────────────────────────
    def on_response(response):
        nonlocal api_fields_logged
        url = response.url
        if "search/v3/query" not in url or response.request.method != "POST":
            return
        try:
            # Usa response.text() para que o Playwright respeite o charset do
            # Content-Type (e.g. windows-1252).  Fallback para decode UTF-8 se
            # text() não estiver disponível.
            try:
                raw_json = response.text()
            except Exception:
                raw_json = response.body().decode("utf-8", errors="replace")
            # Remove caracteres de substituição (U+FFFD) que podem aparecer
            # quando a API retorna encoding Windows-1252 de forma incorreta.
            raw_json = raw_json.replace("�", "")
            data = json.loads(raw_json)
            result_list = data if isinstance(data, list) else [data]
            for result_wrap in result_list:
                for item in result_wrap.get("result", {}).get("values", []):
                    if item.get("type") != "newsArticle":
                        continue
                    art = item["newsArticle"]
                    article_id = art.get("id", "")
                    if not article_id or article_id in seen_ids:
                        continue
                    seen_ids.add(article_id)

                    if not api_fields_logged:
                        log.debug(
                            "fastmarkets_scraper: campos API: %s", sorted(art.keys())
                        )
                        api_fields_logged = True

                    body_html_api = (
                        art.get("bodyHtml") or art.get("body") or
                        art.get("content") or art.get("fullText") or ""
                    )
                    summary_html = art.get("summary") or ""

                    snippet = (
                        _html_to_text(summary_html)[:360]
                        or _html_to_text(body_html_api)[:360]
                    )

                    from .html_utils import article_to_safe_html
                    if body_html_api and len(body_html_api) > 200:
                        fallback_body = article_to_safe_html(body_html_api)
                    elif summary_html and len(summary_html) > 100:
                        fallback_body = article_to_safe_html(summary_html)
                    else:
                        fallback_body = f"<p>{_html.escape(snippet)}</p>" if snippet else ""

                    articles_meta.append({
                        "id": article_id,
                        "title": art.get("title") or "",
                        "body": fallback_body,
                        "snippet": snippet,
                        "published_at": art.get("publishedDate"),
                        "url": _base_article_url(article_id),
                        "body_from_dom": False,
                    })

        except Exception as e:
            log.debug("fastmarkets_scraper: parse error em %s: %s", url, e)

    # ─── Browser setup ───────────────────────────────────────────────────────
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

            # Carrega state existente (se disponível)
            ctx_kwargs: dict = {
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "viewport": {"width": 1440, "height": 900},
                "locale": "en-US",
            }
            if _STATE_FILE.exists():
                ctx_kwargs["storage_state"] = str(_STATE_FILE)

            ctx = browser.new_context(**ctx_kwargs)
            ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            ctx.on("response", on_response)
            page = ctx.new_page()

            try:
                # ─── Fase 1: Dashboard ────────────────────────────────────────
                log.info("fastmarkets_scraper: [Fase 1] carregando dashboard PP News...")

                while login_attempts <= _MAX_LOGIN_ATTEMPTS:
                    page.goto(_DASHBOARD_URL, wait_until="domcontentloaded", timeout=45_000)

                    if _is_login_page(page):
                        login_attempts += 1
                        if login_attempts > _MAX_LOGIN_ATTEMPTS:
                            log.warning(
                                "fastmarkets_scraper: sessão expirada após %d tentativas. "
                                "Execute 'python login.py' para renovar.",
                                _MAX_LOGIN_ATTEMPTS,
                            )
                            page.close()
                            browser.close()
                            return []
                        log.info(
                            "fastmarkets_scraper: sessão expirada — auto-login "
                            "(tentativa %d/%d)...",
                            login_attempts, _MAX_LOGIN_ATTEMPTS,
                        )
                        if not _do_auto_login(page, ctx):
                            page.close()
                            browser.close()
                            return []
                        # Recarrega o listener de responses no novo contexto
                        # (ctx ainda é o mesmo — apenas a sessão foi renovada)
                        continue  # tenta carregar o dashboard novamente

                    # Dashboard carregou — aguarda a chamada /search/v3/query
                    try:
                        page.wait_for_load_state("networkidle", timeout=30_000)
                    except Exception:
                        page.wait_for_timeout(15_000)

                    # Verifica login após carregamento completo
                    if _is_login_page(page):
                        continue  # loop vai tentar auto-login

                    break  # dashboard carregado com sucesso

                log.info(
                    "fastmarkets_scraper: [Fase 1] %d artigos interceptados",
                    len(articles_meta),
                )

                # Filtro por janela de tempo
                now_utc = datetime.now(timezone.utc)
                candidates = []
                for a in articles_meta:
                    pub = _parse_date(a.get("published_at"))
                    if pub is None or (now_utc - pub).total_seconds() / 3600 <= _MAX_AGE_HOURS:
                        candidates.append(a)

                log.info(
                    "fastmarkets_scraper: [Fase 1] %d artigos dentro de %dh",
                    len(candidates), _MAX_AGE_HOURS,
                )
                articles_meta.clear()
                articles_meta.extend(candidates)

                if not articles_meta:
                    log.info("fastmarkets_scraper: nenhum artigo novo — encerrando")
                    page.close()
                    browser.close()
                    return []

                # ─── Fase 2: Corpo completo por artigo ──────────────────────
                #
                # Estratégia:
                #   1. Consulta o DB para saber quais artigos já têm corpo bom
                #   2. Separa: "precisa fetch" (body < _GOOD_BODY_LEN) vs. "já completo"
                #   3. Processa apenas os que precisam, ordenados do mais curto ao
                #      mais longo — garante que novos artigos e re-fetches urgentes
                #      sejam tratados primeiro
                #   4. Cada artigo: goto → wait_for_selector → networkidle →
                #      wait_for_function → evaluate → retry único se vazio
                #
                # Seletores JavaScript reutilizados em duas funções:
                _JS_SELECTORS = """[
                    '.content-container',
                    '.article-container',
                    '[class*="article-body"]',
                    '[class*="ArticleBody"]',
                    '[class*="articleBody"]',
                    '[class*="article-content"]',
                    '[class*="ArticleContent"]',
                    '[class*="post-content"]',
                    '[class*="entry-content"]',
                    'article',
                ]"""

                # Verifica se o conteúdo já foi renderizado pelo React SPA (> 200 chars)
                _JS_CONTENT_READY = f"""() => {{
                    for (const sel of {_JS_SELECTORS}) {{
                        const el = document.querySelector(sel);
                        if (el && (el.innerText || '').trim().length > 200) return true;
                    }}
                    return false;
                }}"""

                # Extrai innerHTML do primeiro container com conteúdo (> 50 chars)
                _JS_EXTRACT = f"""() => {{
                    for (const sel of {_JS_SELECTORS}) {{
                        const el = document.querySelector(sel);
                        if (el && (el.innerText || '').trim().length > 50)
                            return el.innerHTML.trim();
                    }}
                    return '';
                }}"""

                # Selector CSS para wait_for_selector (subset dos JS selectors)
                _CSS_BODY_SEL = (
                    ".content-container, .article-container, "
                    "[class*='article-body'], [class*='ArticleBody'], "
                    "[class*='articleBody'], [class*='article-content'], article"
                )

                # Consulta DB para classificar artigos
                _body_lens_db: dict[str, int] = {}
                try:
                    import sqlite3 as _sqlite3
                    from .store import DB_PATH as _DB_PATH
                    with _sqlite3.connect(str(_DB_PATH)) as _dbc:
                        _body_lens_db = {
                            row[0]: row[1]
                            for row in _dbc.execute(
                                "SELECT url, LENGTH(COALESCE(body,'')) "
                                "FROM articles WHERE domain='dashboard.fastmarkets.com'"
                            )
                        }
                except Exception as _dbe:
                    log.debug("fastmarkets_scraper: [Fase 2] erro ao consultar DB: %s", _dbe)

                def _db_body_len(m: dict) -> int:
                    uid = m.get("id", "")
                    for url, blen in _body_lens_db.items():
                        if uid in url:
                            return blen
                    return 0  # ausente no DB → prioridade máxima

                # Separa artigos que precisam de fetch dos que já estão completos
                needs_fetch = sorted(
                    [m for m in articles_meta if _db_body_len(m) < _GOOD_BODY_LEN],
                    key=_db_body_len,  # mais curto/ausente primeiro
                )
                has_good = [m for m in articles_meta if _db_body_len(m) >= _GOOD_BODY_LEN]

                log.info(
                    "fastmarkets_scraper: [Fase 2] %d precisam de fetch, "
                    "%d já completos (>=%d chars) — pulando re-fetch",
                    len(needs_fetch), len(has_good), _GOOD_BODY_LEN,
                )

                n_ok = 0
                n_short = 0
                phase2_start = _time.time()

                for i, meta in enumerate(needs_fetch):
                    elapsed = _time.time() - phase2_start
                    budget_left = _PHASE2_BUDGET - elapsed
                    if budget_left <= 5:
                        log.info(
                            "fastmarkets_scraper: [Fase 2] budget esgotado após "
                            "%d/%d artigos (%.0fs decorridos)",
                            i, len(needs_fetch), elapsed,
                        )
                        break

                    art_id = meta.get("id", f"#{i + 1}")
                    nav_url = _base_article_url(meta["id"])
                    try:
                        # Captura fingerprint do conteúdo ATUAL antes de navegar.
                        # Protege contra React SPA reutilizar o DOM do artigo anterior:
                        # o wait seguinte só passa quando o conteúdo for DIFERENTE deste.
                        try:
                            prev_fp = page.evaluate(
                                f"""() => {{
                                    for (const sel of {_JS_SELECTORS}) {{
                                        const el = document.querySelector(sel);
                                        if (el) {{
                                            const t = (el.innerText || '').trim();
                                            if (t.length > 50) return t.slice(0, 150);
                                        }}
                                    }}
                                    return '__empty__';
                                }}"""
                            )
                        except Exception:
                            prev_fp = "__empty__"

                        page.goto(nav_url, wait_until="domcontentloaded", timeout=40_000)

                        # Detecta expiração de sessão
                        if _is_login_page(page):
                            login_attempts += 1
                            if login_attempts > _MAX_LOGIN_ATTEMPTS:
                                log.warning(
                                    "fastmarkets_scraper: [Fase 2] sessão expirou, "
                                    "limite de re-logins atingido. Salvando %d/%d.",
                                    n_ok, len(needs_fetch),
                                )
                                break
                            log.info(
                                "fastmarkets_scraper: [Fase 2] sessão expirou no artigo "
                                "%s — auto-login (%d/%d)...",
                                art_id, login_attempts, _MAX_LOGIN_ATTEMPTS,
                            )
                            if not _do_auto_login(page, ctx):
                                break
                            page.goto(nav_url, wait_until="domcontentloaded", timeout=40_000)
                            if _is_login_page(page):
                                log.warning(
                                    "fastmarkets_scraper: [Fase 2] re-login falhou, "
                                    "pulando %s.", art_id,
                                )
                                continue

                        # Aguarda container do artigo no DOM
                        try:
                            page.wait_for_selector(_CSS_BODY_SEL, timeout=15_000)
                        except Exception:
                            page.wait_for_timeout(3_000)

                        # Aguarda networkidle — garante que o React SPA terminou
                        # de buscar o corpo completo via API
                        try:
                            page.wait_for_load_state("networkidle", timeout=12_000)
                        except Exception:
                            page.wait_for_timeout(2_000)

                        # Aguarda conteúdo NOVO: diferente do fingerprint anterior E > 200 chars.
                        # Dupla proteção contra React SPA que reutiliza o mesmo container DOM
                        # — sem isso, `length > 200` passa imediatamente com conteúdo do artigo anterior.
                        _fp_js = json.dumps(prev_fp)
                        try:
                            page.wait_for_function(
                                f"""() => {{
                                    for (const sel of {_JS_SELECTORS}) {{
                                        const el = document.querySelector(sel);
                                        if (el) {{
                                            const t = (el.innerText || '').trim();
                                            if (t.length > 200 && t.slice(0, 150) !== {_fp_js})
                                                return true;
                                        }}
                                    }}
                                    return false;
                                }}""",
                                timeout=8_000,
                            )
                        except Exception:
                            pass

                        # Captura URL canônica com slug
                        canonical_url: str = page.evaluate("() => window.location.href")
                        if canonical_url and "dashboard.fastmarkets.com/a/" in canonical_url:
                            meta["url"] = canonical_url.split("?")[0].rstrip("/")

                        # Valida que o ID do artigo esperado está na URL atual.
                        # Se não estiver, a navegação ainda está no artigo anterior
                        # (React router atrasado) — descarta para evitar contaminação.
                        if (
                            art_id
                            and not art_id.startswith("#")
                            and canonical_url
                            and art_id.lower() not in canonical_url.lower()
                        ):
                            log.warning(
                                "fastmarkets_scraper: [Fase 2] URL MISMATCH — "
                                "ID %s não encontrado em %s — descartando corpo.",
                                art_id, canonical_url[-80:],
                            )
                            n_short += 1
                            continue

                        # Extrai innerHTML do container de conteúdo
                        raw_inner: str = page.evaluate(_JS_EXTRACT)

                        # Retry único se a captura ficou vazia (React pode ter demorado)
                        if not raw_inner or len(raw_inner) < 50:
                            log.debug(
                                "fastmarkets_scraper: [Fase 2] %s captura vazia — retry",
                                art_id,
                            )
                            page.wait_for_timeout(3_000)
                            raw_inner = page.evaluate(_JS_EXTRACT)

                        # Sanitiza e armazena
                        if raw_inner and len(raw_inner) >= 50:
                            from .html_utils import article_to_safe_html
                            clean_html = article_to_safe_html(raw_inner)
                            if clean_html and len(clean_html) >= 30:
                                meta["body"] = clean_html
                                meta["body_from_dom"] = True
                                n_ok += 1
                                log.info(
                                    "fastmarkets_scraper: [Fase 2] [%d/%d] %s — "
                                    "%d chars ✓",
                                    i + 1, len(needs_fetch), art_id, len(clean_html),
                                )
                            else:
                                n_short += 1
                                log.info(
                                    "fastmarkets_scraper: [Fase 2] [%d/%d] %s — "
                                    "HTML vazio após sanitização",
                                    i + 1, len(needs_fetch), art_id,
                                )
                        else:
                            n_short += 1
                            log.info(
                                "fastmarkets_scraper: [Fase 2] [%d/%d] %s — "
                                "DOM não encontrado (artigo genuinamente curto?)",
                                i + 1, len(needs_fetch), art_id,
                            )

                    except Exception as e:
                        log.warning(
                            "fastmarkets_scraper: [Fase 2] [%d/%d] %s — erro: %s",
                            i + 1, len(needs_fetch), art_id, e,
                        )

                log.info(
                    "fastmarkets_scraper: [Fase 2] concluída — "
                    "%d com corpo completo, %d DOM não encontrado, "
                    "%d já estavam completos, %.0fs decorridos",
                    n_ok, n_short, len(has_good), _time.time() - phase2_start,
                )

            except Exception as e:
                log.warning("fastmarkets_scraper: erro de navegação: %s", e)
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
        log.warning("fastmarkets_scraper: erro geral do Playwright: %s", e)
        return []

    return articles_meta


def collect_fastmarkets_articles() -> list:
    """Coleta artigos do Fastmarkets PP News. Retorna lista de Article."""
    from .store import Article

    result: list[dict] = []
    err: list[Exception | None] = [None]

    def _run():
        try:
            result.extend(_scrape_worker())
        except Exception as e:
            err[0] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=360)  # Fase1 ~50s + login ~20s + Fase2 ~250s + overhead

    if err[0]:
        log.warning("fastmarkets_scraper: falhou com %s", err[0])

    if not result:
        creds_ok = _CREDENTIALS_FILE.exists()
        if not creds_ok:
            log.warning(
                "fastmarkets_scraper: 0 artigos. Sem credenciais para auto-login. "
                "Execute 'python login.py' para configurar."
            )

    now = datetime.now(timezone.utc)
    articles: list[Article] = []
    for r in result:
        if not r.get("title") and not r.get("body"):
            continue
        published = _parse_date(r.get("published_at"))
        articles.append(Article(
            url=r["url"],
            domain="dashboard.fastmarkets.com",
            source_name="Fastmarkets",
            title=r["title"],
            snippet=r["snippet"],
            published_at=published or now,
            found_at=now,
            matched_keywords=["#fastmarkets"],
            body=r["body"],
        ))

    log.info("fastmarkets_scraper: %d artigos prontos para persistência", len(articles))
    return articles
