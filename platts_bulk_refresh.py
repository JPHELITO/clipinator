"""Script de renovação em lote dos artigos Platts — captura imagens."""
import sys, logging, sqlite3, time as _time, json
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("platts_bulk")

# ── Pega artigos sem imagem, por prioridade ──────────────────────────────────
cutoff = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
conn = sqlite3.connect("data/newshunter.db")
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute("""
    SELECT url, title, LENGTH(body) as blen,
        CASE
            WHEN url LIKE '%insightsType=Analysis%' THEN 1
            WHEN url LIKE '%insightsType=Feature%'  THEN 2
            WHEN title LIKE '%INFOGRAPHIC%'          THEN 3
            WHEN url LIKE '%insightsType=News%'      THEN 4
            WHEN LENGTH(body) > 500                  THEN 5
            ELSE 6
        END as priority
    FROM articles
    WHERE domain='core.spglobal.com'
    AND published_at > ?
    ORDER BY priority, published_at DESC
    LIMIT 60
""", (cutoff,))
to_process = [dict(r) for r in cur.fetchall()]
conn.close()
log.info("%d artigos para processar", len(to_process))

state_file = Path("C:/Users/João Paulo Helito/news_generator/cookies/platts_state.json")

JS_SCROLL = """(function() {
    var el = document.querySelector('.newsSection-body');
    if (!el) return;
    window.scrollTo(0, el.scrollHeight);
    window.scrollTo(0, 0);
})()"""

from playwright.sync_api import sync_playwright
from newshunter.html_utils import PLATTS_DOM_WALK_JS, platts_dom_items_to_html
from newshunter.store import save_article_body

n_imgs_total = 0
n_updated = 0

with sync_playwright() as p:
    try:
        browser = p.chromium.launch(
            headless=True, channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )
    except Exception:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )

    ctx = browser.new_context(
        storage_state=str(state_file),
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
    page = ctx.new_page()

    # Warmup idêntico ao scraper real
    log.info("Warmup: carregando homepage...")
    page.goto("https://core.spglobal.com/", wait_until="domcontentloaded", timeout=40_000)
    page.wait_for_timeout(6_000)

    if "login" in page.url.lower():
        log.error("Sessao expirada! Rode python login.py novamente.")
        browser.close()
        sys.exit(1)

    log.info("Warmup: allInsights (18s)...")
    page.evaluate("window.location.hash = '#platts/allInsights'")
    page.wait_for_timeout(18_000)
    log.info("SPA pronto. Processando %d artigos...", len(to_process))

    budget_start = _time.time()
    BUDGET = 600  # 10 minutos

    prev_fp = "__empty__"  # fingerprint do artigo anterior (anti-contaminação)

    for i, art in enumerate(to_process):
        elapsed = _time.time() - budget_start
        if elapsed > BUDGET:
            log.warning("Budget de %ds esgotado após %d artigos", BUDGET, i)
            break

        art_url = art["url"]
        title   = art["title"][:55]
        log.info("[%d/%d] %s", i + 1, len(to_process), title)

        try:
            # ── Fingerprint do artigo ANTERIOR ───────────────────────────────
            # Captura os primeiros 150 chars do corpo ANTES de navegar.
            # Usado para detectar conteúdo stale (Angular SPA reusa componente
            # e exibe artigo anterior enquanto o novo carrega).
            try:
                prev_fp = page.evaluate(
                    "(function() {"
                    "  var el = document.querySelector('.newsSection-body');"
                    "  return el ? (el.innerText || '').trim().slice(0, 150) : '__empty__';"
                    "})"
                ) or "__empty__"
            except Exception:
                prev_fp = "__empty__"

            page.goto(art_url, wait_until="domcontentloaded", timeout=20_000)

            selector_found = False
            try:
                page.wait_for_selector(".newsSection-body", timeout=14_000)
                selector_found = True
            except Exception:
                page.wait_for_timeout(2_000)

            if not selector_found:
                log.debug("  selector nao encontrado, pulando")
                continue

            # ── Aguarda conteúdo NOVO (diferente do fingerprint anterior) ───
            content_ready = False
            if selector_found:
                _fp_js = json.dumps(prev_fp)
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
                    if prev_fp == "__empty__":
                        content_ready = True  # 1º artigo: qualquer conteúdo serve
                    else:
                        # Timeout: verifica se mudou após espera extra
                        page.wait_for_timeout(3_000)
                        try:
                            _curr = page.evaluate(
                                "(function() {"
                                "  var el = document.querySelector('.newsSection-body');"
                                "  return el ? (el.innerText || '').trim().slice(0, 150) : '';"
                                "})"
                            )
                            content_ready = bool(_curr and len(_curr) > 50 and _curr != prev_fp)
                        except Exception:
                            content_ready = False
                        if not content_ready:
                            log.warning("  conteudo nao mudou (stale) — pulando")
                            continue

            try:
                page.evaluate(JS_SCROLL)
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

            # DOM walker: items em ordem (texto + slots de imagem)
            # Imagens capturadas via page.screenshot(clip=bb) — sem hover artifacts
            try:
                walk_data = json.loads(page.evaluate(PLATTS_DOM_WALK_JS))
            except Exception:
                walk_data = {"items": [], "hl": ""}

            # ── Validação de conteúdo (anti-contaminação) ────────────────────
            # Se o início do corpo capturado coincide com o fingerprint anterior,
            # é conteúdo stale do artigo anterior — descarta.
            text_items = [it for it in walk_data.get("items", []) if it.get("t") != "img"]
            bdy_text   = " ".join(it.get("v", "") for it in text_items)
            if (prev_fp and prev_fp != "__empty__" and bdy_text and
                    bdy_text[:150].strip() == prev_fp.strip()):
                log.warning("  corpo = artigo anterior (stale) — pulando")
                continue

            bdy_len = sum(len(it.get("v", "")) for it in text_items)
            if bdy_len < 80:
                log.debug("  sem corpo do DOM")
                continue

            clean_html = platts_dom_items_to_html(walk_data, page)
            if not clean_html or len(clean_html) < 80:
                continue

            # Conta imagens capturadas para o log
            n_imgs_art = clean_html.count("reader-img")
            if n_imgs_art:
                n_imgs_total += n_imgs_art
                log.info("  -> %d imagem(ns) capturada(s)", n_imgs_art)

            save_article_body(art_url, clean_html, force=True)
            n_updated += 1

        except Exception as e:
            log.warning("  ERRO em %s: %s", title, e)

    browser.close()

log.info(
    "=== CONCLUÍDO: %d artigos atualizados, %d imagens capturadas ===",
    n_updated, n_imgs_total,
)
