"""Scraper Playwright para artigos do Estadão (economia.estadao.com.br).

Fluxo em duas etapas:

Etapa 1 — Sitemap (urllib, sem Playwright):
  • Tenta os candidatos de sitemap em ordem até encontrar um válido:
      https://www.estadao.com.br/sitemap-news.xml
      https://economia.estadao.com.br/sitemap-news.xml
      https://www.estadao.com.br/news-sitemap.xml
  • Extrai (url, title, pub_date) de todos os artigos
  • Filtra pela janela de 72h e usa keyword matching para limitar candidatos

Etapa 2 — Corpo (Playwright, artigo-a-artigo):
  • Usa wait_until="domcontentloaded" para capturar o HTML *antes* que o
    Zephr (paywall client-side) tenha tempo de bloquear o conteúdo
  • O Estadão usa Zephr by Zuora: o corpo do artigo está FISICAMENTE no DOM
    mesmo para assinantes — apenas um overlay JS o oculta. Não esperar
    "networkidle" significa capturar antes desse overlay ser aplicado.
  • Fallback: se domcontentloaded falhar, tenta com sessão autenticada
  • Extrai lide (.article-summary) + corpo (.article-body ou variantes)

Login (Estadão ≠ Globo):
  • Estadão usa sistema próprio em acesso.estadao.com.br (Zephr Identity)
  • NÃO compartilha login com Globo/Valor
  • State file:  news_generator/cookies/estadao_state.json
  • Credentials: news_generator/cookies/estadao_credentials.json
  • Login manual: python login.py → seleciona "Estadão"

Seletores confirmados (pesquisa + padrões Zephr BR):
  • [class*="article-body"]  — corpo principal do artigo
  • [class*="article-summary"] — lide/resumo de abertura
  • [class*="article__header-summary"] — lide alternativo
  • .article-body, .article__content — variantes de classe
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

_COOKIES_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "news_generator"
    / "cookies"
)
_STATE_FILE       = _COOKIES_DIR / "estadao_state.json"
_CREDENTIALS_FILE = _COOKIES_DIR / "estadao_credentials.json"

# Sitemaps do Estadão (formato Arc Publishing — confirmado via robots.txt).
# O site usa sitemaps por dia: /arc/outboundfeeds/sitemap/latest/ (hoje)
# e /arc/outboundfeeds/sitemap/YYYY-MM-DD/ (dias anteriores).
# NÃO usa Google News Sitemap namespace — apenas <loc> e <lastmod>.
_SITEMAP_BASE = "https://www.estadao.com.br/arc/outboundfeeds/sitemap/{day}/?outputType=xml"

# Seções irrelevantes para o setor S&M / P&P / Cimento — filtradas por path.
_SKIP_SECTIONS = frozenset([
    "esportes", "cultura", "paladar", "viagem", "saude", "ciencia",
    "educacao", "moda", "estilo", "tecnologia", "entretenimento",
    "horoscopo", "enigmas", "galeria", "imagens", "fotos",
])

# Domínio canônico para persistência no DB (normaliza subdomínios)
_DOMAIN = "www.estadao.com.br"

# Janela de busca — alinhada com a retenção de 72h do projeto
_MAX_AGE_HOURS = 72

# Máximo de artigos com corpo buscado no Playwright
_MAX_BODY_FETCH = 12

# Budget de tempo (segundos) para a fase Playwright completa
_PHASE2_BUDGET = 150

# Máximo de tentativas de auto-login por sessão
_MAX_LOGIN_ATTEMPTS = 2

# Marcadores de paywall — se o corpo extraído contiver estes textos,
# descartar e tentar outra abordagem.
_PAYWALL_MARKERS = (
    "precisa ser um assinante",
    "assine agora",
    "conteúdo exclusivo para assinantes",
    "para continuar lendo",
    "seja assinante",
    "acesso exclusivo",
    "assine o estadão",
    "leia sem limites",
    "assine o estadao",
    "acesso ilimitado",
)


def _html_to_text(h: str) -> str:
    text = re.sub(r"<[^>]+>", " ", h)
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _is_paywall_content(text: str) -> bool:
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


def _is_estadao_url(url: str) -> bool:
    """True se a URL pertence ao domínio Estadão."""
    return "estadao.com.br" in url


def _slug_to_title(url: str) -> str:
    """Extrai um título legível do slug da URL do Estadão.

    Ex: '.../economia/vale-lucro-2-bilhoes-trimestre/' → 'vale lucro 2 bilhoes trimestre'
    Usado para keyword matching quando o sitemap não fornece título.
    """
    from urllib.parse import urlparse as _up
    path = _up(url).path.rstrip("/")
    slug = path.split("/")[-1] if path else ""
    # Remove sufixos de tracking comuns (nprec, npres, etc.)
    slug = re.sub(r"-np[a-z]{2,4}$", "", slug)
    return slug.replace("-", " ").strip()


def _section_from_url(url: str) -> str:
    """Retorna a primeira seção do path (ex. 'economia', 'esportes')."""
    from urllib.parse import urlparse as _up
    parts = _up(url).path.strip("/").split("/")
    return parts[0].lower() if parts else ""


# ─── Sitemap ──────────────────────────────────────────────────────────────────

def _fetch_one_sitemap(day: str, headers: dict) -> list[dict]:
    """Baixa e parseia um sitemap Arc do Estadão para o dia informado.

    `day` pode ser 'latest' ou 'YYYY-MM-DD'.
    Retorna lista de {url, slug_title, lastmod}.
    """
    url = _SITEMAP_BASE.format(day=day)
    try:
        req = urllib.request.Request(url, headers=headers)
        xml_bytes = urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        log.debug("estadao_scraper: sitemap %s indisponível: %s", url, e)
        return []

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        log.warning("estadao_scraper: XML inválido em %s: %s", url, e)
        return []

    NS_STD = "http://www.sitemaps.org/schemas/sitemap/0.9"
    results: list[dict] = []
    for url_el in root.findall(f"{{{NS_STD}}}url"):
        loc     = (url_el.findtext(f"{{{NS_STD}}}loc") or "").strip()
        lastmod = (url_el.findtext(f"{{{NS_STD}}}lastmod") or "").strip()

        if not loc or not _is_estadao_url(loc):
            continue

        # Filtra seções irrelevantes por path
        section = _section_from_url(loc)
        if section in _SKIP_SECTIONS:
            continue

        results.append({
            "url":        loc,
            "slug_title": _slug_to_title(loc),
            "lastmod":    lastmod,
        })

    log.debug("estadao_scraper: sitemap %s → %d URLs relevantes", day, len(results))
    return results


def _fetch_sitemap() -> list[dict]:
    """Coleta artigos dos sitemaps Arc do Estadão das últimas _MAX_AGE_HOURS.

    Estratégia:
    - Baixa 'latest' (artigos de hoje) + sitemaps dos dias anteriores
      suficientes para cobrir a janela de _MAX_AGE_HOURS horas.
    - Usa <lastmod> como data de publicação (o sitemap Arc não usa Google
      News Namespace — não há news:publication_date nem news:title).
    - slug_title extraído da URL serve de proxy para keyword matching.
    """
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    now = datetime.now(timezone.utc)

    # Quantos dias anteriores precisamos além do 'latest'?
    extra_days = max(1, (_MAX_AGE_HOURS // 24))

    days_to_fetch = ["latest"] + [
        (now - __import__("datetime").timedelta(days=d)).strftime("%Y-%m-%d")
        for d in range(1, extra_days + 1)
    ]

    seen_urls: set[str] = set()
    results: list[dict] = []

    for day in days_to_fetch:
        for entry in _fetch_one_sitemap(day, headers):
            url = entry["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)

            pub_dt = _parse_date(entry["lastmod"])
            if pub_dt is not None:
                age_h = (now - pub_dt).total_seconds() / 3600
                if age_h > _MAX_AGE_HOURS:
                    continue

            results.append({
                "url":        url,
                "title":      "",            # preenchido pelo Playwright
                "slug_title": entry["slug_title"],
                "pub":        pub_dt,
            })

    log.info(
        "estadao_scraper: sitemap → %d artigos nas últimas %dh",
        len(results), _MAX_AGE_HOURS,
    )
    return results


# ─── Auto-login ───────────────────────────────────────────────────────────────

def _load_credentials() -> dict | None:
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
    """True se a página atual é a tela de autenticação do Estadão."""
    url = page.url.lower()
    return (
        "acesso.estadao.com.br" in url
        or "login.estadao.com.br" in url
        or "/login" in url
        or "/signin" in url
    )


def _set_session_expired_alert() -> None:
    """Registra alerta de sessão expirada na dashboard."""
    try:
        from .store import set_session_alert
        set_session_alert(
            "estadao",
            "Sessão do Estadão expirada — artigos aparecem com paywall. "
            "Execute <code>python login.py --estadao</code> para renovar "
            "(abre Chrome real; login automático headless é bloqueado pelo Akamai).",
        )
    except Exception:
        pass


def _do_auto_login(page, ctx) -> bool:  # noqa: ARG001
    """Auto-login headless do Estadão não é possível.

    O Akamai CDN bloqueia bots no acesso.estadao.com.br com 'Access Denied'.
    Usa Chrome real via 'python login.py --estadao' para renovar a sessão.
    Registra alerta na dashboard e retorna False.
    """
    log.warning(
        "estadao_scraper: sessão expirada. Auto-login headless bloqueado pelo Akamai. "
        "Execute 'python login.py --estadao' para renovar a sessão."
    )
    _set_session_expired_alert()
    return False


# ─── Extração de corpo ────────────────────────────────────────────────────────

# JS de extração: tenta múltiplos seletores conhecidos do Estadão.
# Executado após domcontentloaded (antes do overlay Zephr ter tempo de rodar).
def _estadao_clean_js() -> str:
    """Retorna o JS de extração + limpeza de conteúdo do Estadão.

    Estratégia: clona o elemento news-body, remove os elementos indesejados
    do clone (ads, LE.IA widget, 'Leia também', lixo do CMS) e extrai
    innerHTML do clone limpo — sem alterar o DOM original.
    """
    return r"""() => {
    // ── helpers ──────────────────────────────────────────────────────────────
    function removeAll(root, sel) {
        root.querySelectorAll(sel).forEach(function(el) { el.remove(); });
    }

    function cleanClone(el) {
        var clone = el.cloneNode(true);

        // 1. Propagandas e containers de ads
        removeAll(clone, '.ads-container');
        removeAll(clone, '[class*="AdStyled"]');
        removeAll(clone, '[class*="ad-slot"]');
        removeAll(clone, '.ads-placeholder-label');
        removeAll(clone, '.ads-placeholder-wrapper');

        // 2. Widgets e lixo do CMS (Arc/Fusion)
        removeAll(clone, '[data-fusion-lazy-id]');
        removeAll(clone, '[data-fusion-collection]');
        removeAll(clone, '[class*="powerup-pdf"]');
        removeAll(clone, '[class*="container-preview"]');
        removeAll(clone, '[class*="FigureImageWrapper"]');   // imagens (mantém figcaption se quiser)
        removeAll(clone, 'button');
        removeAll(clone, 'svg');

        // 3. "Leia também" / "Veja também" — h3/h4 + lista seguinte
        clone.querySelectorAll('h3, h4').forEach(function(h) {
            var txt = (h.innerText || h.textContent || '').toLowerCase().trim();
            if (txt === 'leia também' || txt === 'leia tambem' ||
                txt === 'veja também' || txt === 'veja tambem' ||
                txt === 'leia mais'   || txt.startsWith('leia também') ||
                txt.startsWith('veja também')) {
                var next = h.nextElementSibling;
                if (next && (next.tagName === 'UL' || next.tagName === 'OL')) {
                    next.remove();
                }
                h.remove();
            }
        });

        // 4. Widget LE.IA — remove parágrafos com texto de loading/identificação da IA
        clone.querySelectorAll('p, div').forEach(function(el) {
            var txt = (el.innerText || el.textContent || '').toLowerCase().trim();
            if (txt.includes('le.ia') || txt === 'gerando resumo' ||
                txt.includes('ia do estadão') || txt.includes('ia do estadao') ||
                txt.includes('confira o resumo que')) {
                el.remove();
            }
        });

        // 5. Parágrafos vazios ou só com espaço
        clone.querySelectorAll('p').forEach(function(p) {
            if (!(p.innerText || '').trim()) p.remove();
        });

        return clone.innerHTML.trim();
    }

    // ── extração ─────────────────────────────────────────────────────────────
    var BODY_SELS = [
        '[class*="news-body"]',
        '[class*="article-body"]',
        '[data-testid="article-body"]',
        '[class*="article__content"]',
        '.article-body', '.article__content',
        '[class*="content-article"]', '.nota-content'
    ];
    var LEDE_SELS = [
        '[class*="article__header-summary"]',
        '[class*="article-summary"]',
        '.article-summary', 'h2.subtitle'
    ];

    function firstClean(sels) {
        for (var i = 0; i < sels.length; i++) {
            var el = document.querySelector(sels[i]);
            if (el) {
                var html = cleanClone(el);
                if (html.length > 100) return html;
            }
        }
        return '';
    }

    var body = firstClean(BODY_SELS);
    var lede = firstClean(LEDE_SELS);

    // Paywall: verifica apenas no corpo, não na página inteira
    var bodyLow = body.toLowerCase();
    var isPaywalled = (!body || body.length < 200) &&
        (bodyLow.includes('leia sem limites') || bodyLow.includes('continue lendo com'));

    return JSON.stringify({ lede: lede, body: body, is_paywalled: isPaywalled });
}"""


_EXTRACT_JS = _estadao_clean_js()


def _extract_body(page) -> tuple[str, bool]:
    """Extrai lide + corpo do artigo da página atual.

    Retorna (html_corpo, is_paywalled).
    Zephr é client-side — o corpo está no DOM mesmo sem auth,
    basta capturar antes do overlay ser aplicado.
    """
    from .html_utils import article_to_safe_html

    try:
        dom_info: str = page.evaluate(_EXTRACT_JS)
        data = json.loads(dom_info)
    except Exception as e:
        log.debug("estadao_scraper: erro ao avaliar DOM: %s", e)
        return "", False

    is_paywalled = data.get("is_paywalled", False)

    parts: list[str] = []

    lede = data.get("lede", "")
    if lede and len(lede) > 40:
        clean = article_to_safe_html(lede)
        if clean:
            parts.append(clean)

    body = data.get("body", "")
    if body and len(body) > 80:
        clean = article_to_safe_html(body)
        if clean:
            parts.append(clean)

    combined = "\n".join(parts)

    # Descarta se parecer tela de paywall
    if combined and _is_paywall_content(combined):
        return "", True

    return combined, is_paywalled and not combined


# ─── Worker Playwright ────────────────────────────────────────────────────────

def _scrape_worker(sitemap_items: list[dict]) -> list[dict]:
    """Navega artigos via Playwright e extrai corpos.

    Usa domcontentloaded para capturar o HTML antes que o Zephr
    (paywall client-side do Estadão) tenha tempo de aplicar o overlay.
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
                        log.info("estadao_scraper: budget esgotado após %d artigos", i)
                        break

                    url    = meta["url"]
                    title  = meta.get("title", "")
                    pub_dt = meta.get("pub")

                    try:
                        # domcontentloaded: captura antes do Zephr bloquear
                        page.goto(url, wait_until="domcontentloaded", timeout=25_000)

                        if _is_login_page(page):
                            # Sessão expirada — auto-login headless bloqueado pelo Akamai.
                            # Registra alerta na dashboard e para de tentar.
                            if not login_attempts:
                                _do_auto_login(page, ctx)  # registra alerta + log
                            login_attempts += 1
                            log.warning(
                                "estadao_scraper: sessão expirada — execute "
                                "'python login.py --estadao' para renovar."
                            )
                            break

                        # Aguarda seletor de artigo aparecer (não networkidle — evita Zephr)
                        try:
                            page.wait_for_selector(
                                "[class*='news-body'], [class*='article-body'], [class*='article__content'], article",
                                timeout=8_000,
                            )
                        except Exception:
                            page.wait_for_timeout(1_500)

                        body_html, is_paywalled = _extract_body(page)

                        if is_paywalled:
                            # Paywall real (sessão expirada ou artigo premium)
                            if not login_attempts:
                                _set_session_expired_alert()
                            login_attempts += 1
                            log.debug("estadao_scraper: artigo paywalled: %s", url[-60:])

                        # Fallback genérico: coleta <p> se body vazio mas sem paywall
                        if not body_html and not is_paywalled:
                            try:
                                paras = page.evaluate("""() => {
                                    const els = document.querySelectorAll(
                                        '[class*="news-body"] p, [class*="article-body"] p, article p'
                                    );
                                    return Array.from(els)
                                        .map(e => (e.innerText || '').trim())
                                        .filter(t => t.length > 40)
                                        .slice(0, 25);
                                }""")
                                if paras:
                                    import html as _html_mod
                                    body_html = "\n".join(
                                        f"<p>{_html_mod.escape(p)}</p>" for p in paras
                                    )
                            except Exception:
                                pass

                        # Descarte final de texto de paywall
                        if body_html and _is_paywall_content(body_html):
                            log.debug(
                                "estadao_scraper: corpo descartado (paywall text): %s",
                                url[-60:],
                            )
                            body_html = ""

                        if body_html and len(body_html) > 80:
                            n_ok += 1
                            log.debug(
                                "estadao_scraper: artigo %d OK (%d chars): %s",
                                i + 1, len(body_html), url[-60:],
                            )
                        else:
                            log.debug(
                                "estadao_scraper: artigo %d sem corpo: %s",
                                i + 1, url[-60:],
                            )

                        snippet = _html_to_text(body_html)[:360] if body_html else ""

                        # Captura título real da página (h1 ou <title>)
                        page_title = ""
                        for sel in ["h1", "[class*='article__header-title']", "[class*='article-title']"]:
                            try:
                                el = page.query_selector(sel)
                                if el:
                                    t = el.inner_text().strip()
                                    if t and len(t) > 10:
                                        page_title = t
                                        break
                            except Exception:
                                pass
                        if not page_title:
                            pt = page.title().strip()
                            # Remove sufixo " | Estadão" comum no <title>
                            page_title = re.sub(r"\s*[|\-–]\s*[Ee]stad[aã]o.*$", "", pt).strip()

                        results.append({
                            "url":          url,
                            "title":        page_title or title,
                            "body":         body_html,
                            "snippet":      snippet,
                            "published_at": pub_dt,
                        })

                    except Exception as e:
                        log.debug("estadao_scraper: erro em %s: %s", url, e)

                log.info(
                    "estadao_scraper: %d/%d artigos com corpo extraído",
                    n_ok, len(to_fetch),
                )

            except Exception as e:
                log.warning("estadao_scraper: erro de navegação: %s", e)
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
        log.warning("estadao_scraper: erro geral do Playwright: %s", e)

    return results


# ─── Entry point público ──────────────────────────────────────────────────────

def collect_estadao_articles() -> list:
    """Coleta artigos do Estadão com filtro de keywords.

    Fluxo:
      1. Sitemap (urllib, sem Playwright): extrai artigos das últimas 72h
      2. Keyword filtering no título: usa keywords configuradas para
         www.estadao.com.br (aba Fontes) ou fallback para keywords globais
      3. Playwright (domcontentloaded): extrai corpo antes do Zephr bloquear
      4. Retorna lista de Article prontos para persistência
    """
    from .filter import matches_keywords
    from .store import Article, get_all_source_keywords, get_config

    # Etapa 1: sitemap
    sitemap_items = _fetch_sitemap()
    if not sitemap_items:
        log.info("estadao_scraper: nenhum artigo no sitemap — abortando")
        return []

    # Etapa 2: keyword filtering
    source_kw_map = get_all_source_keywords()
    keywords: list[str] = (
        source_kw_map.get(_DOMAIN)
        or get_config()["keywords"]
    )

    use_all = keywords == ["*"]

    candidates: list[dict] = []
    for item in sitemap_items:
        # Usa slug_title (derivado da URL) como proxy para keyword matching,
        # pois o sitemap Arc do Estadão não inclui títulos.
        slug_title = item.get("slug_title", "")
        if use_all:
            matched: list[str] = ["#estadao"]
        else:
            matched = matches_keywords(slug_title, keywords) or []
        if matched:
            candidates.append({**item, "matched": matched})

    log.info(
        "estadao_scraper: %d/%d artigos passaram no filtro de keywords",
        len(candidates), len(sitemap_items),
    )

    if not candidates:
        log.info("estadao_scraper: nenhum artigo casou com os keywords configurados")
        return []

    # Etapa 3: corpo via Playwright
    result: list[dict] = []
    err: list[Exception | None] = [None]

    def _run():
        try:
            result.extend(_scrape_worker(candidates))
        except Exception as e:
            err[0] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    # sitemap ~5s + goto por artigo ~5s * 12 + overhead = ~180s
    t.join(timeout=210)

    if err[0]:
        log.warning("estadao_scraper: falhou com %s", err[0])

    now = datetime.now(timezone.utc)
    pw_results: dict[str, dict] = {r["url"]: r for r in result}

    articles: list[Article] = []
    for cand in candidates:
        url        = cand["url"]
        slug_title = cand.get("slug_title", "")
        pub_dt     = cand.get("pub")
        matched    = cand.get("matched", ["#estadao"])

        pw      = pw_results.get(url, {})
        body    = pw.get("body", "")
        snippet = pw.get("snippet", "")
        # Título real vem do Playwright; slug_title como último fallback
        title   = pw.get("title", "") or slug_title

        if not title:
            continue

        if not snippet:
            snippet = (_html_to_text(body)[:360] if body else title[:360])

        articles.append(Article(
            url=url,
            domain=_DOMAIN,
            source_name="Estadão",
            title=title,
            snippet=snippet,
            published_at=pub_dt or now,
            found_at=now,
            matched_keywords=matched,
            body=body,
        ))

    log.info("estadao_scraper: %d artigos prontos para persistência", len(articles))
    return articles
