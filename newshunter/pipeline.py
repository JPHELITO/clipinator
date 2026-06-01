"""Orquestracao: coletar -> filtrar -> enriquecer -> persistir."""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

from .enrich import _resolve_google_news_url, enrich_item, source_name_for
from .fetcher import RawItem, iter_collect

from .filter import matches_keywords, strip_related, within_window
from .sources import HOMEPAGE_SCRAPERS, PAYWALL_DOMAINS
from .store import (
    Article,
    finish_run,
    get_all_source_keywords,
    get_cached_snippets,
    get_config,
    normalize_url,
    start_run,
    upsert_articles,
)

log = logging.getLogger(__name__)

ENRICH_WORKERS = 24
# Workers de resolucao separados: gnewsdecoder sofre rate-limit acima de
# ~6 chamadas paralelas ao Google; com 6 workers cada chamada leva ~1.5s.
RESOLVE_WORKERS = 6
# Deadline de enrich sobre URLs ja resolvidas (fetch_html = 6s/item max).
ENRICH_DEADLINE = 35.0
# Deadline adicional para aguardar resolucoes pendentes apos collect.
# Platts/Fastmarkets geram ~100 itens; com NO_RSS_DOMAINS reduzido total cai para ~200.
# 200 / 6 workers * 1.75s ≈ 58s → 60s garante cobertura quase total.
RESOLVE_EXTRA = 60.0

# _ENRICH_CAP define o maximo de itens enriquecidos por dominio.
# IMPORTANTE: o cap e aplicado DUAS VEZES para itens Google News
# (antes e depois da resolucao de URL). Para garantir que os itens
# resolvidos consigam passar, usamos cap = 2x o valor logico desejado.


def _keep_candidate(
    item: RawItem,
    keywords: list[str],
    hours: int,
    source_kw_map: dict[str, list[str]] | None = None,
) -> list[str] | None:
    """Filtragem barata pre-enriquecimento.

    Retorna a lista de keywords que casaram, ou None se deve descartar.
    Itens sem titulo E sem summary (sitemaps WordPress padrao) recebem
    ["#pending"] — keyword check real ocorre no stage 4 apos enriquecimento.
    """
    # Janela primeiro: filtra a maioria dos itens sem pagar custo de regex.
    # Excecao: itens Google News (URL news.google.com/articles/...) ja foram
    # filtrados pela query 'when:Xh' do GNews. Alguns dominios (ex.: Platts/S&P)
    # carregam no <pubDate> a data ORIGINAL do artigo (anos atras); confiar no
    # filtro GNews e mais correto do que rejeitar pelo pubDate do RSS.
    if item.published_at is not None and not within_window(item.published_at, hours):
        if not item.url.startswith("https://news.google.com/"):
            return None
    # Per-source keywords: verificados PRIMEIRO — têm prioridade sobre qualquer
    # outro comportamento, incluindo o bypass de homepage scrapers.
    # Isso permite que o usuário configure mode=specific para El Financiero
    # (homepage scraper) e os keywords sejam de fato respeitados.
    skm = source_kw_map or {}
    source_kws = skm.get(item.source_domain) or skm.get(item.feed_domain)
    if source_kws is not None:
        if source_kws == ["*"]:
            return ["#source"]  # mode=all — aceita tudo desta fonte
        # mode=specific — usa keywords da fonte; cai no matching abaixo
        keywords = source_kws
    elif item.feed_domain in HOMEPAGE_SCRAPERS:
        # Sem config específica: homepage scrapers são topically focused,
        # aceita tudo. Só chega aqui se source_kws é None (nenhuma config).
        return ["#topic"]
    # Items sem titulo nem summary:
    if not item.title and not item.summary:
        # Pre-filtra via URL: slug (sitemaps) ou path completo (homepage scrapers).
        # Reduz drasticamente o numero de fetches de enriquecimento.
        from urllib.parse import urlparse as _up
        path = _up(item.url).path
        if item.published_at is None:
            # Homepage scrapers: usa path inteiro (section + slug) como hint
            path_text = path.replace("-", " ").replace("/", " ")
            path_match = matches_keywords(path_text, keywords)
            return path_match if path_match else None
        # Sitemaps WordPress padrao: usa apenas o slug (ultimo segmento)
        slug = path.rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
        slug_match = matches_keywords(slug, keywords)
        return slug_match if slug_match else None
    # Title-first: titulos sao curtos. Se casou, retorna imediatamente.
    matched = matches_keywords(item.title, keywords)
    if matched:
        return matched
    clean_summary = strip_related(item.summary)
    if not clean_summary:
        return None
    return matches_keywords(clean_summary, keywords) or None


def run_search(
    *,
    include_google_news: bool = True,
    fast_mode: bool = False,
    hours_override: int | None = None,
    skip_playwright: bool = False,
) -> dict:
    """Executa uma busca completa. Retorna estatisticas.

    Pipeline em 3 fases paralelas:
    1. Collect streamed: feeds retornam assim que disponivel.
    2. Pre-resolucao GNews (paralela ao collect): URLs news.google.com sao
       decodificadas com resolve_ex (RESOLVE_WORKERS=6) para nao causar
       rate-limit no Google. Itens nao-GNews vao direto para o enrich.
    3. Enrich: fetch_html nas URLs reais (48 workers, 12s deadline).

    fast_mode=True (usado pela pagina /headlines): passa need_snippet=False
    ao enrich_item, que retorna sem fetch_html quando title+published ja
    vieram do RSS. Em itens "bem formados" do feed, isso elimina a fase 3
    inteira; o que sobra e o proprio collect + resolucao GNews. Os cards
    da pagina de headlines so mostram titulo, fonte e link — snippet vazio
    nao prejudica a UI daquela rota.

    hours_override: quando nao-None, ignora cfg["window_hours"]. Usado pelo
    auto-refresh (sempre 24h leve) — mantem a janela maior do dropdown
    apenas como filtro de exibicao, sem sobrecarregar scans periodicos.
    """
    cfg = get_config()
    keywords: list[str] = cfg["keywords"]
    hours: int = (
        hours_override if hours_override is not None else int(cfg["window_hours"])
    )
    # Carrega filtros por fonte do banco (uma vez por run)
    source_kw_map = get_all_source_keywords()

    run_id = start_run()
    errors: list[str] = []
    n_new = 0
    n_upserted = 0

    try:
        t0 = time.time()

        # Platts + Fastmarkets + Valor + Estadão: scrapers Playwright autenticados.
        # skip_playwright=True pula todos (modo rápido para CI/GitHub Actions).
        platts_future = None
        platts_ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="platts")
        if not skip_playwright:
            try:
                from .platts_scraper import collect_platts_articles
                platts_future = platts_ex.submit(collect_platts_articles)
            except Exception as e:
                log.warning("platts_scraper nao disponivel: %s", e)
                platts_ex.shutdown(wait=False)

        fm_future = None
        fm_ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fastmarkets")
        if not skip_playwright:
            try:
                from .fastmarkets_scraper import collect_fastmarkets_articles
                fm_future = fm_ex.submit(collect_fastmarkets_articles)
            except Exception as e:
                log.warning("fastmarkets_scraper nao disponivel: %s", e)
                fm_ex.shutdown(wait=False)

        valor_future = None
        valor_ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="valor")
        if not skip_playwright:
            try:
                from .valor_scraper import collect_valor_articles
                valor_future = valor_ex.submit(collect_valor_articles)
            except Exception as e:
                log.warning("valor_scraper nao disponivel: %s", e)
                valor_ex.shutdown(wait=False)

        estadao_future = None
        estadao_ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="estadao")
        if not skip_playwright:
            try:
                from .estadao_scraper import collect_estadao_articles
                estadao_future = estadao_ex.submit(collect_estadao_articles)
            except Exception as e:
                log.warning("estadao_scraper nao disponivel: %s", e)
                estadao_ex.shutdown(wait=False)

        itaubba_future = None
        itaubba_ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="itaubba")
        if not skip_playwright:
            try:
                from .itaubba_scraper import collect_itaubba_publications
                itaubba_future = itaubba_ex.submit(collect_itaubba_publications)
            except Exception as e:
                log.warning("itaubba_scraper nao disponivel: %s", e)
                itaubba_ex.shutdown(wait=False)

        worldcement_future = None
        worldcement_ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="worldcement")
        try:
            from .worldcement_scraper import collect_worldcement_articles
            worldcement_future = worldcement_ex.submit(collect_worldcement_articles)
        except Exception as e:
            log.warning("worldcement_scraper nao disponivel: %s", e)
            worldcement_ex.shutdown(wait=False)
        # (item, matched, snippet, published, resolved_url, resolved_domain, extracted_title)
        enriched: list[tuple[RawItem, list[str], str, datetime | None, str, str, str]] = []
        seen_urls: set[str] = set()
        pending: dict = {}          # future -> (item, matched)  — enrich_ex
        pending_resolve: dict = {}  # future -> (item, matched)  — resolve_ex

        enrich_ex = ThreadPoolExecutor(max_workers=ENRICH_WORKERS)
        resolve_ex = ThreadPoolExecutor(max_workers=RESOLVE_WORKERS)
        n_raw = 0
        n_cand = 0
        n_cache = 0
        n_resolved_ok = 0
        # Limita enriquecimentos por dominio (fase de enrich, pos-resolucao).
        # Homepage scrapers (published_at=None) usam cap menor: servidores lentos
        # podem travar threads por 15s+ em fetchs sem retorno rapido.
        _enrich_count: dict[str, int] = {}
        # Budget separado para itens Google News enviados ao resolve_ex.
        # NAO usa _enrich_count antes da resolucao: o dominio real so e conhecido
        # apos gnewsdecoder decodificar a URL — incrementar antes bloquearia todos
        # os itens resolvidos do mesmo dominio (ex.: spglobal.com).
        _resolve_budget: dict[str, int] = {}
        _ENRICH_CAP = 20           # max artigos enriquecidos por dominio
        _ENRICH_CAP_HOMEPAGE = 60  # homepage scrapers (sites topicos, aceita muito)
        _RESOLVE_BUDGET_CAP = 80   # max itens GNews enviados ao resolve_ex por dominio de feed
        try:
            for dom, items, err in iter_collect(
                keywords, hours, include_google_news=include_google_news
            ):
                if err:
                    errors.append(err)
                if not items:
                    continue
                # Filtra items novos e aplica keyword match
                batch_keys: list[str] = []
                batch: list[tuple[RawItem, list[str], str]] = []
                for it in items:
                    n_raw += 1
                    key = normalize_url(it.url)
                    if key in seen_urls:
                        continue
                    seen_urls.add(key)
                    matched = _keep_candidate(it, keywords, hours, source_kw_map)
                    if matched is None:
                        continue
                    n_cand += 1
                    batch_keys.append(key)
                    batch.append((it, matched, key))

                if not batch_keys:
                    continue
                # Lookup cache para o batch
                cached = get_cached_snippets(batch_keys)
                for it, matched, key in batch:
                    hit = cached.get(key)
                    if hit is not None and hit[0]:
                        n_cache += 1
                        sn, pub, domres, cached_title = hit
                        enriched.append((it, matched, sn, pub or it.published_at, key, domres, cached_title))
                    else:
                        if it.url.startswith("https://news.google.com/"):
                            # Fase 2a: resolucao separada (max 6 paralelas → sem rate-limit).
                            # Usa _resolve_budget (por feed_domain) para limitar fila;
                            # _enrich_count so e incrementado apos resolucao, quando o
                            # dominio real e conhecido.
                            _r_cnt = _resolve_budget.get(it.source_domain, 0)
                            if _r_cnt >= _RESOLVE_BUDGET_CAP:
                                continue
                            _resolve_budget[it.source_domain] = _r_cnt + 1
                            fut = resolve_ex.submit(_resolve_google_news_url, it.url)
                            pending_resolve[fut] = (it, matched)
                        else:
                            # Fase 2b: enrich direto (URL real, sem gnewsdecoder)
                            _cap = _ENRICH_CAP_HOMEPAGE if it.published_at is None else _ENRICH_CAP
                            _cnt = _enrich_count.get(it.source_domain, 0)
                            if _cnt >= _cap:
                                continue
                            _enrich_count[it.source_domain] = _cnt + 1
                            fut = enrich_ex.submit(
                                enrich_item, it,
                                resolve_google_news=False,
                                need_snippet=not fast_mode,
                            )
                            pending[fut] = (it, matched)

            # --- Fase 2a: aguarda resolucoes restantes ---
            done_resolve, nd_resolve = wait(
                pending_resolve.keys(), timeout=RESOLVE_EXTRA
            )
            for fut in done_resolve:
                it, matched = pending_resolve[fut]
                try:
                    resolved_url, resolved_domain = fut.result()
                except Exception as e:  # noqa: BLE001
                    errors.append(f"resolve {it.url}: {e!s}")
                    resolved_url, resolved_domain = it.url, it.source_domain
                if resolved_url.startswith("https://news.google.com/"):
                    continue  # nao resolvido — descarta
                _cnt = _enrich_count.get(resolved_domain, 0)
                if _cnt >= _ENRICH_CAP:
                    continue
                _enrich_count[resolved_domain] = _cnt + 1
                n_resolved_ok += 1
                resolved_it = RawItem(
                    url=resolved_url,
                    title=it.title,
                    summary=it.summary,
                    published_at=it.published_at,
                    source_domain=resolved_domain,
                    feed_domain=it.feed_domain,
                )
                fut2 = enrich_ex.submit(
                    enrich_item, resolved_it,
                    resolve_google_news=False,
                    need_snippet=not fast_mode,
                )
                pending[fut2] = (resolved_it, matched)
            for fut in nd_resolve:
                fut.cancel()

            log.info(
                "Candidatos: %d (brutos %d) | cache_hit=%d resolve=%d(ok=%d) enrich=%d",
                n_cand, n_raw, n_cache, len(pending_resolve), n_resolved_ok, len(pending),
            )

            # --- Fase 3: aguarda enrich ---
            done, not_done = wait(pending.keys(), timeout=ENRICH_DEADLINE)
            for fut in done:
                it, matched = pending[fut]
                try:
                    snippet, published, resolved_url, resolved_domain, ext_title = fut.result()
                except Exception as e:  # noqa: BLE001
                    errors.append(f"enrich {it.url}: {e!s}")
                    snippet = ""
                    published = it.published_at
                    resolved_url = it.url
                    resolved_domain = it.source_domain
                    ext_title = ""
                enriched.append((it, matched, snippet, published, resolved_url, resolved_domain, ext_title))
            # Items que nao terminaram a tempo sao descartados. Antes salvavamos
            # homepage scrapers com published=now, mas isso fazia artigos VELHOS
            # (fixados em secoes) aparecerem como "agora".
            for fut in not_done:
                fut.cancel()
        finally:
            resolve_ex.shutdown(wait=False, cancel_futures=True)
            enrich_ex.shutdown(wait=False, cancel_futures=True)

        # Stage 4: janela final + validacao de titulo/snippet.
        # Homepage scrapers apontam para paginas topicas — nao exigem snippet
        # nem re-validacao de keyword (a pagina em si garante relevancia).
        now = datetime.now(timezone.utc)
        to_persist: list[Article] = []
        for it, matched, snippet, published, resolved_url, resolved_domain, ext_title in enriched:
            is_topic = it.feed_domain in HOMEPAGE_SCRAPERS
            is_paywall = resolved_domain in PAYWALL_DOMAINS
            # Data real obrigatoria. Homepage scrapers (agencia.petrobras etc)
            # coletam links "fixados" que podem ser de meses atras — sem data
            # extraida do <meta>, nao da para confiar na recencia.
            # Excecao: dominios com paywall (Platts, Fastmarkets) publicam artigos
            # com datas originais antigas (ex.: ratings S&P de 2020 re-indexados
            # hoje). GNews when:Xh ja garante recencia; usamos found_at como proxy.
            if published is None:
                if is_paywall:
                    published = now
                else:
                    continue
            elif not within_window(published, hours):
                if is_paywall:
                    published = now  # data original do artigo; exibir como "agora"
                else:
                    continue
            # Wrapper Google News nao resolvido = link quebrado, descarta.
            if resolved_url.startswith("https://news.google.com/"):
                continue
            # Para fontes NAO topicas, snippet e obrigatorio (garante relevancia
            # via re-validacao de keyword). Para homepage scrapers e dominios com
            # paywall duro, aceita sem snippet — titulo do RSS ja valida relevancia.
            # Em fast_mode (headlines), aceita sem snippet: a relevancia ja foi
            # validada no _keep_candidate via titulo ou summary do RSS.
            if not snippet and not is_topic and not is_paywall and not fast_mode:
                continue

            # Titulo real: item original > extraido pela pagina > slug da URL
            if it.title:
                display_title = it.title
            elif ext_title:
                display_title = ext_title
            else:
                # Fallback: slug da URL. urldecode percent-escapes (%C3%A1 -> á)
                # e capitaliza como sentence case — senao fica "projeto cine petrobras...".
                slug = resolved_url.rstrip("/").rsplit("/", 1)[-1]
                raw = unquote(slug).replace("-", " ").strip()
                display_title = raw[:1].upper() + raw[1:] if raw else resolved_url

            # Clampa published_at no futuro: alguns sites (ex.: agencia.petrobras)
            # publicam metadata com timestamp agendado. Evita "há -51 min".
            if published and published > now + timedelta(minutes=5):
                published = now

            final_hay = f"{display_title} \n {snippet}"
            final_match = matches_keywords(final_hay, keywords)
            if is_topic:
                # Site ja e topico — se nao casou keyword especifica, marca como #topic.
                if not final_match:
                    final_match = ["#topic"]
            elif not final_match:
                # Sem snippet (fast_mode ou paywall) items so casam via titulo;
                # se _keep_candidate aprovou pelo summary RSS, mantem keywords originais.
                if (fast_mode or is_paywall) and matched:
                    final_match = matched
                else:
                    # Re-validacao estrita para fontes genericas.
                    continue
            to_persist.append(Article(
                url=normalize_url(resolved_url),
                domain=resolved_domain,
                source_name=source_name_for(resolved_domain),
                title=display_title,
                snippet=snippet,
                published_at=published,
                found_at=now,
                matched_keywords=final_match,
            ))

        # Coleta resultados dos scrapers autenticados.
        # Platts (Fase 1 ~40s + Fase 2 ~100s com networkidle): até 200s.
        # Fastmarkets (Fase 1 + Fase 2 por artigo): até 200s.
        elapsed = time.time() - t0
        platts_wait = max(5.0, 200.0 - elapsed)

        if platts_future is not None:
            try:
                platts_articles = platts_future.result(timeout=platts_wait)
                to_persist.extend(platts_articles)
                log.info("platts_scraper: %d artigos adicionados ao lote", len(platts_articles))
            except Exception as e:
                log.warning("platts_scraper future falhou: %s", e)
            finally:
                platts_ex.shutdown(wait=False)

        if fm_future is not None:
            elapsed2 = time.time() - t0
            fm_wait = max(5.0, 200.0 - elapsed2)
            try:
                fm_articles = fm_future.result(timeout=fm_wait)
                to_persist.extend(fm_articles)
                log.info("fastmarkets_scraper: %d artigos adicionados ao lote", len(fm_articles))
            except Exception as e:
                log.warning("fastmarkets_scraper future falhou: %s", e)
            finally:
                fm_ex.shutdown(wait=False)

        if valor_future is not None:
            elapsed3 = time.time() - t0
            # Valor scraper: sitemap ~5s + Playwright por artigo + auto-login
            valor_wait = max(5.0, 230.0 - elapsed3)
            try:
                valor_articles = valor_future.result(timeout=valor_wait)
                to_persist.extend(valor_articles)
                log.info("valor_scraper: %d artigos adicionados ao lote", len(valor_articles))
            except Exception as e:
                log.warning("valor_scraper future falhou: %s", e)
            finally:
                valor_ex.shutdown(wait=False)

        if estadao_future is not None:
            elapsed_est = time.time() - t0
            # Estadão: sitemap + Playwright (domcontentloaded, mais rápido que networkidle)
            estadao_wait = max(5.0, 230.0 - elapsed_est)
            try:
                estadao_articles = estadao_future.result(timeout=estadao_wait)
                to_persist.extend(estadao_articles)
                log.info("estadao_scraper: %d artigos adicionados ao lote", len(estadao_articles))
            except Exception as e:
                log.warning("estadao_scraper future falhou: %s", e)
            finally:
                estadao_ex.shutdown(wait=False)

        if worldcement_future is not None:
            elapsed_wc = time.time() - t0
            # World Cement: requests+BeautifulSoup puro — sem Playwright, mais rápido
            wc_wait = max(5.0, 180.0 - elapsed_wc)
            try:
                wc_articles = worldcement_future.result(timeout=wc_wait)
                to_persist.extend(wc_articles)
                log.info("worldcement_scraper: %d artigos adicionados ao lote", len(wc_articles))
            except Exception as e:
                log.warning("worldcement_scraper future falhou: %s", e)
            finally:
                worldcement_ex.shutdown(wait=False)

        n_new = upsert_articles(to_persist)

        # Itaú BBA Publications: salva sem bloquear o pipeline principal.
        # Budget generoso (300s) — corre em paralelo desde o início do run.
        if itaubba_future is not None:
            elapsed4 = time.time() - t0
            itaubba_wait = max(5.0, 300.0 - elapsed4)
            try:
                pubs = itaubba_future.result(timeout=itaubba_wait)
                if pubs:
                    from .store import save_publications
                    save_publications([
                        {"title": p.title, "pdf_url": p.pdf_url, "report_url": p.report_url}
                        for p in pubs
                    ])
                    log.info("itaubba_scraper: %d publicações salvas", len(pubs))
            except Exception as e:
                log.warning("itaubba_scraper future falhou: %s", e)
            finally:
                itaubba_ex.shutdown(wait=False)
        n_upserted = len(to_persist)

    except Exception as e:  # noqa: BLE001
        log.exception("Falha na busca")
        errors.append(f"fatal: {e!s}")
    finally:
        finish_run(run_id, n_upserted, errors[:200])  # cap de erros no historico

    return {
        "run_id": run_id,
        "n_new": n_new,
        "n_total": n_upserted,
        "errors": errors,
        "window_hours": hours,
        "keywords_count": len(keywords),
    }


# Lock global: o poll das paginas dispara um scan a cada ~30s; com abas
# multiplas, ou quando um scan anterior ainda nao terminou, o segundo skipa
# em vez de concorrer pelo mesmo pool de threads (gnewsdecoder faz rate-limit).
_headlines_scan_lock = threading.Lock()
_last_headlines_scan_ts: float = 0.0
_HEADLINES_MIN_INTERVAL = 20.0

# Janela fixa do auto-refresh (Dashboard, Headlines, Clipping). O dropdown
# de janela do Dashboard apenas FILTRA a exibicao; scans periodicos nunca
# varrem alem de 24h — isso fica reservado para o botao "Buscar agora".
AUTO_SCAN_HOURS = 24


def run_headlines_scan() -> dict:
    """Scan leve para o auto-refresh das paginas (Dashboard/Headlines/Clipping).

    - fast_mode=True: enrich_item skipa fetch_html quando ja tem title+published.
    - hours_override=24: scans automaticos sempre usam 24h, independente da
      janela salva em config (que pode ser 48h/72h/7d via dropdown do Dashboard).
    - Lock global: scans concorrentes (abas multiplas ou poll sobreposto) sao
      descartados.
    - Rate-limit de 20s: mesmo com lock livre, se o ultimo scan terminou ha
      menos de 20s, skipa. Evita hammering por abas abertas de mais.

    Resultado: {'skipped': True/False, 'n_new': int, ...}
    """
    global _last_headlines_scan_ts
    if not _headlines_scan_lock.acquire(blocking=False):
        return {"skipped": True, "reason": "locked", "n_new": 0, "n_total": 0, "errors": []}
    try:
        now = time.time()
        if now - _last_headlines_scan_ts < _HEADLINES_MIN_INTERVAL:
            return {"skipped": True, "reason": "cooldown", "n_new": 0, "n_total": 0, "errors": []}
        result = run_search(fast_mode=True, hours_override=AUTO_SCAN_HOURS)
        _last_headlines_scan_ts = time.time()
        result["skipped"] = False
        return result
    finally:
        _headlines_scan_lock.release()
