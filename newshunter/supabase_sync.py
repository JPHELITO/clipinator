"""Push de artigos novos para uma tabela `news_articles` no Supabase.

Integracao opcional: sem SUPABASE_URL + SUPABASE_SERVICE_KEY no ambiente, o
scanner segue funcionando 100% em modo local (SQLite). Falhas de rede/auth
tambem sao silenciosas — nunca derrubam o pipeline local.

Bandwidth budget (Supabase free = 5 GB/mes):
- So pushamos artigos NOVOS (flag `is_new` devolvido pelo upsert SQLite).
- Atualizacoes de titulo/snippet em artigos ja persistidos nao re-pushamos
  (raras apos o primeiro enrich — economia relevante em polls de 30s).
- Batch upsert: uma requisicao por scan, independente do tamanho da lista.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import Article

log = logging.getLogger(__name__)

# Cap de seguranca: nunca pusha mais que isso de uma vez. Defensivo contra
# primeiro-scan-da-vida (DB vazio) que pode produzir 200+ artigos novos.
MAX_BATCH = 100

_lock = threading.Lock()
_sink: "_SupabaseSink | None" = None
_tried_init = False


class _SupabaseSink:
    """Cliente Supabase lazy. Silencioso quando nao configurado."""

    def __init__(self) -> None:
        self.client = None
        self.table = "news_articles"
        url = os.environ.get("SUPABASE_URL", "").strip()
        key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
        if not url or not key:
            log.info("Supabase desabilitado (SUPABASE_URL/SUPABASE_SERVICE_KEY ausentes)")
            return
        try:
            from supabase import create_client  # type: ignore[import-untyped]
            self.client = create_client(url, key)
            log.info("Supabase habilitado (target: %s)", url)
        except ImportError:
            log.warning("Pacote 'supabase' nao instalado — pip install supabase")
        except Exception as e:  # noqa: BLE001
            log.warning("Supabase init falhou: %s", e)

    def push(self, articles: list["Article"]) -> int:
        """Faz upsert em lote. Retorna numero de rows enviadas (0 em no-op/erro)."""
        if self.client is None or not articles:
            return 0
        if len(articles) > MAX_BATCH:
            log.warning(
                "Cap de batch atingido: %d articles > MAX_BATCH=%d. Enviando primeiros %d.",
                len(articles), MAX_BATCH, MAX_BATCH,
            )
            articles = articles[:MAX_BATCH]
        rows = [_article_to_row(a) for a in articles]
        try:
            self.client.table(self.table).upsert(rows, on_conflict="url").execute()
            return len(rows)
        except Exception as e:  # noqa: BLE001
            # Nunca deixe isso vazar para o pipeline. Vercel offline, chave
            # revogada, tabela nao criada ainda — tudo cai aqui.
            log.warning("Supabase push falhou (%d rows): %s", len(rows), e)
            return 0


def _article_to_row(a: "Article") -> dict:
    """Serializa Article para o schema da tabela `news_articles`."""
    return {
        "url": a.url,
        "domain": a.domain,
        "source_name": a.source_name,
        "title": a.title,
        "snippet": a.snippet,
        "published_at": a.published_at.isoformat() if a.published_at else None,
        "found_at": a.found_at.isoformat(),
        "matched_keywords": list(a.matched_keywords),
    }


def get_sink() -> _SupabaseSink:
    """Singleton: inicializa o cliente na primeira chamada."""
    global _sink, _tried_init
    if _sink is not None:
        return _sink
    with _lock:
        if _sink is None and not _tried_init:
            _tried_init = True
            _sink = _SupabaseSink()
    return _sink  # type: ignore[return-value]


def push_new(articles: list["Article"]) -> int:
    """Helper de alto nivel: pega o sink e envia. Usado por store.upsert_articles.

    Envelopado em try/except — nunca aborta o pipeline local.
    """
    if not articles:
        return 0
    try:
        return get_sink().push(articles)
    except Exception as e:  # noqa: BLE001
        log.warning("push_new falhou: %s", e)
        return 0
