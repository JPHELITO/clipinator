"""Scraper Playwright para artigos da Platts via API content-bff.

Fluxo em dois estágios:

Fase 1 — Listagem (intercepta content-bff/v1/search):
  • Carrega #platts/allInsights (News, Flash, Rationale, Headline Analysis)
  • Carrega #platts/insightsResult?contentType=Market%20Commentary
  • Coleta IDs, títulos, datas e snippet de cada artigo relevante

Fase 2 — Corpo completo (navega artigo-a-artigo):
  • Abre cada URL individual: #platts/insightsArticle?articleID=X&insightsType=Y
  • Lê .newsSection-highlights + .newsSection-body via innerText (preserva parágrafos)
  • Substitui o corpo vindo da API pelo corpo real da página do artigo

URL correta de cada artigo:
  https://core.spglobal.com/#platts/insightsArticle?articleID={ID}&insightsType={ContentType}
"""
from __future__ import annotations

import html
import json
import logging
import re
import threading
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger(__name__)

_STATE_FILE = (
    Path(__file__).resolve().parent.parent.parent
    / "news_generator"
    / "cookies"
    / "platts_state.json"
)

# Tipos de conteúdo que queremos buscar nas listagens
_WANTED_CONTENT_TYPES = {
    "News", "Top News", "Flash", "Market Commentary",
    "Blog", "Rationale", "Headline Analysis",
}

# Filtros de relevância (Fase 1)
_RELEVANT_SECTORS = {
    "Steel", "Ferrous Metals Plus", "Coal", "Iron Ore", "Metals",
    "Copper", "Base Metals", "Pulp & Paper", "Forest Products",
    "Cement", "Mining", "Energy", "Agriculture",
}

_RELEVANT_COMMODITIES_LOWER = {
    "steel", "iron ore", "hot-rolled", "hrc", "coking coal", "coke",
    "pellet", "slab", "billet", "scrap", "copper", "aluminum", "aluminium",
    "pulp", "paper", "bhkp", "bekp", "kraft", "cement", "clinker",
    "nickel", "zinc", "lead", "cobalt", "lithium", "manganese",
}

# Máximo de artigos com corpo completo buscado na Fase 2.
# wait_for_selector 14s × 6 artigos ≈ 84s + overhead → cabe no budget.
_MAX_BODY_FETCH = 6

# Budget de tempo (segundos) reservado para a Fase 2 completa.
_PHASE2_BUDGET = 100


def _is_relevant(item: dict) -> bool:
    """True se o artigo é relevante para Steel & Mining / P&P / Cement."""
    sectors = {s.lower() for s in item.get("Sector", [])}
    if sectors & {s.lower() for s in _RELEVANT_SECTORS}:
        return True
    commodities = " ".join(item.get("Commodity", [])).lower()
    if any(k in commodities for k in _RELEVANT_COMMODITIES_LOWER):
        return True
    headline = (item.get("Headline") or item.get("Name") or "").lower()
    if any(k in headline for k in _RELEVANT_COMMODITIES_LOWER):
        return True
    return False


def _parse_date(s: str | None) -> datetime | None:
    if not s or s.startswith("0001"):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _html_to_text(h: str) -> str:
    """Remove tags HTML e retorna texto limpo em uma linha (para snippets)."""
    text = re.sub(r"<[^>]+>", " ", h)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _article_url(article_id: str, content_type: str = "News") -> str:
    """URL canônica de um artigo Platts Insights."""
    # ContentType como "Market Commentary" → "Market%20Commentary" no hash
    insights_type = quote(content_type, safe="")
    return (
        f"https://core.spglobal.com/"
        f"#platts/insightsArticle?articleID={article_id}&insightsType={insights_type}"
    )


def _scrape_worker() -> list[dict]:
    """Executa em thread separada.

    Fase 1: intercepta content-bff/v1/search nas páginas de listagem.
    Fase 2: navega artigo-a-artigo para ler o corpo completo via DOM.
    """
    from playwright.sync_api import sync_playwright

    if not _STATE_FILE.exists():
        log.warning("platts_scraper: state file não encontrado em %s", _STATE_FILE)
        try:
            from .store import set_session_alert
            set_session_alert(
                "platts",
                "Sessão da Platts (S&P Global) não encontrada. "
                "Execute <code>python login.py</code> para fazer login.",
            )
        except Exception:
            pass
        return []

    articles_meta: list[dict] = []
    seen_ids: set[str] = set()

    # ─── Handler de respostas (Fase 1) ───────────────────────────────────────
    def on_response(response):
        url = response.url
        if "content-bff/v1/search" not in url or "image" in url or "event" in url:
            return
        try:
            from .html_utils import article_to_safe_html, innertext_to_html, _split_api_body
            data = json.loads(response.body().decode("utf-8"))
            for item in data.get("Items", []):
                article_id = item.get("Id", "")
                if not article_id or article_id in seen_ids:
                    continue
                if not _is_relevant(item):
                    continue
                seen_ids.add(article_id)
                content_type = item.get("ContentType", "News")
                headline = item.get("Headline") or item.get("Name") or ""
                # Summary = highlights HTML (<ul><li>…</li></ul>) — mesmo
                # conteúdo que aparece como bullets no site Platts.
                # Content = preview truncado de 203 chars do BodyText (não HTML).
                summary_html   = item.get("Summary") or ""
                content_preview = item.get("Content") or ""
                raw_body_html  = item.get("Body") or ""
                raw_body_text  = item.get("BodyText") or ""

                # ── Highlights (bullets) ──────────────────────────────────────
                # Summary é HTML tipo <p><ul><li>…</li></ul></p>
                # article_to_safe_html extrai o <ul><li> limpo.
                highlights_html = article_to_safe_html(summary_html) if summary_html else ""

                # ── Parágrafos do corpo ───────────────────────────────────────
                # Prefere Body (HTML com <p> corretos) sobre BodyText (texto
                # plano com sentenças coladas que precisam de _split_api_body).
                if raw_body_html:
                    body_paragraphs = article_to_safe_html(raw_body_html)
                else:
                    body_paragraphs = ""
                if not body_paragraphs and raw_body_text:
                    body_paragraphs = innertext_to_html(_split_api_body(raw_body_text))

                # ── Corpo combinado ───────────────────────────────────────────
                parts = [p for p in [highlights_html, body_paragraphs] if p]
                fallback_body = "\n".join(parts)

                # ── Snippet ───────────────────────────────────────────────────
                snippet = (
                    _html_to_text(summary_html)[:360]
                    or _html_to_text(raw_body_html)[:360]
                    or content_preview[:360]
                )
                articles_meta.append({
                    "id": article_id,
                    "title": headline,
                    "body": fallback_body,   # substituído na Fase 2
                    "snippet": snippet,
                    "published_at": item.get("UpdatedDate") or item.get("RtpTimestamp"),
                    "content_type": content_type,
                    "url": _article_url(article_id, content_type),
                    "body_from_dom": False,
                })
        except Exception as e:
            log.debug("platts_scraper: parse error em %s: %s", url, e)

    # ─── Browser setup ───────────────────────────────────────────────────────
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

        ctx = browser.new_context(
            storage_state=str(_STATE_FILE),
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="en-US",
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        ctx.on("response", on_response)
        page = ctx.new_page()

        try:
            # ─── Fase 1: Listagens ────────────────────────────────────────────
            log.info("platts_scraper: [Fase 1] carregando core.spglobal.com...")
            page.goto(
                "https://core.spglobal.com/",
                wait_until="domcontentloaded",
                timeout=40_000,
            )
            page.wait_for_timeout(6_000)

            # Detecta redirecionamento para login (sessão expirada)
            _cur_url = page.url.lower()
            if "login" in _cur_url or "signin" in _cur_url or "auth" in _cur_url:
                log.warning("platts_scraper: sessão expirada — redirecionado para %s", page.url)
                try:
                    from .store import set_session_alert
                    set_session_alert(
                        "platts",
                        "Sessão da Platts (S&P Global) expirada — nenhum artigo coletado. "
                        "Execute <code>python login.py</code> para renovar.",
                    )
                except Exception:
                    pass
                return []
            else:
                # Sessão OK — limpa alerta anterior se existir
                try:
                    from .store import clear_session_alert
                    clear_session_alert("platts")
                except Exception:
                    pass

            # allInsights — News, Flash, Rationale, etc.
            page.evaluate("window.location.hash = '#platts/allInsights'")
            page.wait_for_timeout(18_000)

            # Market Commentary — aba separada
            try:
                page.evaluate(
                    "window.location.hash = "
                    "'#platts/insightsResult?contentType=Market%20Commentary'"
                )
                page.wait_for_timeout(12_000)
            except Exception:
                pass

            log.info(
                "platts_scraper: [Fase 1] %d artigos coletados da listagem",
                len(articles_meta),
            )

            # ─── Fase 2: Corpo completo por artigo ───────────────────────────
            to_fetch = articles_meta[:_MAX_BODY_FETCH]
            n_ok = 0
            phase2_start = _time.time()

            for i, meta in enumerate(to_fetch):
                if _time.time() - phase2_start > _PHASE2_BUDGET:
                    log.info(
                        "platts_scraper: [Fase 2] budget esgotado após %d artigos",
                        i,
                    )
                    break

                art_url    = meta["url"]
                article_id = meta["id"]
                try:
                    # ── Fingerprint do artigo ANTERIOR ────────────────────────
                    # Captura os primeiros 150 chars do corpo atual ANTES de
                    # navegar. A condição de espera abaixo aguarda até o conteúdo
                    # ser DIFERENTE desse fingerprint, eliminando a race condition
                    # clássica de Angular SPA (componente reutilizado entre rotas).
                    try:
                        prev_fp: str = page.evaluate(
                            "(function() {"
                            "  var el = document.querySelector('.newsSection-body');"
                            "  return el ? (el.innerText || '').trim().slice(0, 150) : '__empty__';"
                            "})"
                        )
                    except Exception:
                        prev_fp = "__empty__"

                    page.goto(art_url, wait_until="domcontentloaded", timeout=20_000)

                    # Angular hash routing: aguarda o seletor aparecer no DOM.
                    selector_found = False
                    try:
                        page.wait_for_selector(".newsSection-body", timeout=14_000)
                        selector_found = True
                    except Exception:
                        pass

                    content_ready = False
                    if selector_found:
                        # Espera o conteúdo ser NOVO (diferente do fingerprint)
                        # e ter comprimento suficiente. Isso garante que o Angular
                        # terminou de renderizar o artigo correto.
                        import json as _json
                        _fp_js = _json.dumps(prev_fp)
                        try:
                            page.wait_for_function(
                                f"(function() {{"
                                f"  var el = document.querySelector('.newsSection-body');"
                                f"  if (!el || !el.innerText) return false;"
                                f"  var txt = el.innerText.trim();"
                                f"  if (txt.length < 100) return false;"
                                f"  return txt.slice(0, 150) !== {_fp_js};"
                                f"}})",
                                timeout=12_000,
                            )
                            content_ready = True
                        except Exception:
                            # Timeout: conteúdo não mudou dentro do prazo.
                            # Se prev_fp era "__empty__" (1º artigo), aceita qualquer
                            # conteúdo presente. Caso contrário, é possível que o
                            # Angular ainda esteja mostrando o artigo anterior —
                            # aguarda mais um pouco e verifica a situação.
                            if prev_fp == "__empty__":
                                content_ready = True  # 1º artigo: sem artigo anterior
                            else:
                                page.wait_for_timeout(3_000)
                                # Última chance: verifica se o conteúdo mudou agora
                                try:
                                    _curr_fp = page.evaluate(
                                        "(function() {"
                                        "  var el = document.querySelector('.newsSection-body');"
                                        "  return el ? (el.innerText || '').trim().slice(0, 150) : '';"
                                        "})"
                                    )
                                    content_ready = bool(
                                        _curr_fp and
                                        len(_curr_fp) > 50 and
                                        _curr_fp != prev_fp
                                    )
                                except Exception:
                                    content_ready = False
                                if not content_ready:
                                    log.warning(
                                        "platts_scraper: [Fase 2] conteúdo não mudou após timeout "
                                        "em %s — pulando (evita contaminação de artigo anterior)",
                                        art_url[-60:],
                                    )
                                    continue  # pula este artigo

                    log.debug(
                        "platts_scraper: [Fase 2] artigo %d selector_found=%s url=%s",
                        i + 1, selector_found, art_url[-60:],
                    )

                    # Scroll para ativar lazy-loading de imagens antes de capturar
                    try:
                        page.evaluate("""(function() {
                            var el = document.querySelector('.newsSection-body');
                            if (!el) return;
                            var h = el.scrollHeight;
                            window.scrollTo(0, Math.floor(h / 3));
                            window.scrollTo(0, Math.floor(h * 2 / 3));
                            window.scrollTo(0, h);
                            window.scrollTo(0, 0);
                        })()""")
                        page.wait_for_timeout(1_200)
                    except Exception:
                        pass

                    # Aguarda imagens lazy-load renderizarem (naturalWidth > 0)
                    try:
                        page.wait_for_function(
                            "(function() {"
                            "  var imgs = document.querySelectorAll('.newsSection-body img');"
                            "  if (!imgs.length) return true;"
                            "  for (var i=0; i<imgs.length; i++) {"
                            "    if (imgs[i].naturalWidth > 0) return true;"
                            "  }"
                            "  return false;"
                            "})",
                            timeout=5_000,
                        )
                    except Exception:
                        pass

                    # DOM walker: retorna items em ordem (texto + slots de imagem)
                    # Imagens inseridas na posição correta, screenshot via clip=bb
                    # (sem hover — evita overlay "click to zoom" do Angular)
                    from .html_utils import PLATTS_DOM_WALK_JS, platts_dom_items_to_html
                    import json as _json2
                    try:
                        _walk_data = _json2.loads(page.evaluate(PLATTS_DOM_WALK_JS))
                        dom_url    = _walk_data.get("url", "")
                        bdy_text   = " ".join(
                            it["v"] for it in _walk_data.get("items", [])
                            if it.get("t") not in ("img",) and it.get("v")
                        )  # só para validação de URL / comprimento
                    except Exception:
                        _walk_data = {"items": [], "hl": ""}
                        dom_url    = ""
                        bdy_text   = ""

                    # ── Validação de URL ──────────────────────────────────────
                    # Verifica que o articleID no DOM corresponde ao artigo
                    # que navegamos. Se não bater, o Angular ainda está mostrando
                    # o artigo anterior → descarta para evitar contaminação.
                    if article_id and dom_url and article_id.lower() not in dom_url.lower():
                        log.warning(
                            "platts_scraper: [Fase 2] URL MISMATCH — esperado articleID=%s "
                            "mas dom_url=%s — descartando corpo (fallback API)",
                            article_id, dom_url[-80:],
                        )
                        _walk_data = {"items": [], "hl": ""}
                        bdy_text   = ""

                    # ── Validação de conteúdo (anti-contaminação) ─────────────
                    # Garante que o corpo capturado NÃO é o conteúdo do artigo
                    # anterior (Angular SPA pode reutilizar o componente e exibir
                    # conteúdo antigo enquanto carrega o novo).
                    # Se o fingerprint inicial não era vazio e o início do corpo
                    # capturado bate com o fingerprint anterior, é conteúdo stale.
                    if (prev_fp and prev_fp != "__empty__" and bdy_text and
                            bdy_text[:150].strip() == prev_fp.strip()):
                        log.warning(
                            "platts_scraper: [Fase 2] CONTEÚDO STALE em %s "
                            "(corpo = artigo anterior) — descartando",
                            art_url[-60:],
                        )
                        _walk_data = {"items": [], "hl": ""}
                        bdy_text   = ""

                    log.debug(
                        "platts_scraper: [Fase 2] items=%d bdy_sample=%s dom_url=%s",
                        len(_walk_data.get("items", [])), bdy_text[:40], dom_url[-60:],
                    )

                    text_items = [it for it in _walk_data.get("items", []) if it.get("t") != "img"]
                    if text_items and len(bdy_text) > 80:
                        # Constrói HTML com imagens posicionadas corretamente no fluxo do artigo
                        clean_html = platts_dom_items_to_html(_walk_data, page)
                        if clean_html and len(clean_html) > 80:
                            meta["body"] = clean_html
                            meta["body_from_dom"] = True
                            n_ok += 1
                        else:
                            log.debug(
                                "platts_scraper: [Fase 2] DOM walk vazio em %s (fallback API)",
                                art_url,
                            )
                    else:
                        log.debug(
                            "platts_scraper: [Fase 2] sem DOM em %s (fallback API)",
                            art_url,
                        )

                except Exception as e:
                    log.debug("platts_scraper: [Fase 2] erro em %s: %s", art_url, e)

            log.info(
                "platts_scraper: [Fase 2] %d/%d artigos com corpo do DOM",
                n_ok, len(to_fetch),
            )

        except Exception as e:
            log.warning("platts_scraper: erro de navegação: %s", e)
        finally:
            page.close()
            browser.close()

    return articles_meta


def collect_platts_articles() -> list:
    """Coleta artigos da Platts. Retorna lista de Article."""
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
    t.join(timeout=160)  # Fase1 ~40s + Fase2 ~80s + overhead

    if err[0]:
        log.warning("platts_scraper: falhou com %s", err[0])

    now = datetime.now(timezone.utc)
    articles: list[Article] = []
    for r in result:
        published = _parse_date(r.get("published_at"))
        articles.append(Article(
            url=r["url"],
            domain="core.spglobal.com",
            source_name="Platts",
            title=r["title"],
            snippet=r["snippet"],
            published_at=published or now,
            found_at=now,
            matched_keywords=["#platts"],
            body=r["body"],
        ))

    # Artigos com corpo do DOM walker (body_from_dom=True) são force-saved
    # para sobrescrever qualquer corpo antigo/contaminado no DB.
    # O upsert_articles() usa "keep longer body" o que impediria corpos corretos
    # (mas curtos, ex: Rationale = 500 chars) de substituir contaminações longas.
    dom_saved = 0
    from .store import save_article_body
    for r in result:
        if r.get("body_from_dom") and r.get("body"):
            save_article_body(r["url"], r["body"], force=True)
            dom_saved += 1

    log.info(
        "platts_scraper: %d artigos prontos para persistência (%d com corpo DOM force-saved)",
        len(articles), dom_saved,
    )
    return articles
