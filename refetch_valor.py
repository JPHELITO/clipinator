"""Refetch retroativo de artigos do Valor Econômico com paywall no banco.

Uso:
    python refetch_valor.py [--dry-run] [--limit N] [--clear-all]

  --dry-run   Lista os artigos afetados sem re-fetchar
  --limit N   Processa só os N primeiros (default: todos)
  --clear-all Reprocessa TODOS os artigos do Valor, mesmo sem paywall detectado
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

# Garante encoding UTF-8 no terminal Windows
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Adiciona o diretório do projeto ao path
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── Marcadores de paywall (mesmos do valor_scraper.py) ────────────────────────
_PAYWALL_MARKERS = (
    "precisa ser um assinante",
    "assine agora",
    "conteúdo exclusivo para assinantes",
    "para continuar lendo",
    "seja assinante",
    "acesso exclusivo",
    "para ler essa matéria",
)


def _is_paywall(body: str) -> bool:
    t = body.lower()
    return any(m in t for m in _PAYWALL_MARKERS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Só lista, não re-faz")
    parser.add_argument("--limit", type=int, default=0, help="Limite de artigos a processar")
    parser.add_argument("--clear-all", action="store_true", help="Reprocessa todos os artigos do Valor")
    args = parser.parse_args()

    from newshunter.store import DB_PATH, normalize_url

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row

    # Busca artigos do Valor ordenados pelo mais recente
    rows = conn.execute(
        "SELECT url, title, body FROM articles "
        "WHERE domain = 'valor.globo.com' "
        "ORDER BY COALESCE(published_at, found_at) DESC"
    ).fetchall()

    if args.clear_all:
        targets = rows
        print(f"[refetch] Modo --clear-all: {len(targets)} artigo(s) do Valor encontrado(s)")
    else:
        targets = [r for r in rows if _is_paywall(r["body"] or "")]
        print(f"[refetch] {len(rows)} artigo(s) do Valor no banco. "
              f"{len(targets)} com conteúdo de paywall.")

    if args.limit:
        targets = targets[: args.limit]
        print(f"[refetch] Limitado a {args.limit} artigo(s).")

    if not targets:
        print("[refetch] Nenhum artigo para processar. Tudo certo!")
        return

    if args.dry_run:
        print("\n[DRY-RUN] Artigos que seriam re-fatcheados:")
        for r in targets:
            body_preview = (r["body"] or "")[:120].replace("\n", " ")
            print(f"  • {r['title'][:60]}")
            print(f"    URL:  {r['url']}")
            print(f"    Body: {body_preview!r}")
        return

    # ── Importa o leitor Playwright ────────────────────────────────────────────
    from newshunter.playwright_reader import fetch_article

    ok = 0
    fail = 0
    skip = 0

    for i, row in enumerate(targets, 1):
        url = row["url"]
        title = row["title"]
        old_body = row["body"] or ""
        print(f"\n[{i}/{len(targets)}] {title[:70]}")
        print(f"  URL: {url}")

        try:
            fetched_title, new_body = fetch_article(url)
        except Exception as e:
            print(f"  ✗ Erro no fetch: {e}")
            fail += 1
            continue

        if not new_body or len(new_body) < 80:
            print(f"  ○ Sem conteúdo retornado (sessão expirada?)")
            skip += 1
            continue

        if _is_paywall(new_body):
            print(f"  ○ Ainda retornou paywall — sessão ainda expirada")
            skip += 1
            continue

        # Força atualização (ignora a regra "keep longer" para poder sobrescrever paywall)
        norm = normalize_url(url)
        conn.execute(
            "UPDATE articles SET body = ? WHERE url = ?",
            (new_body, norm),
        )
        conn.commit()
        print(f"  ✓ Atualizado ({len(old_body)} → {len(new_body)} chars)")
        ok += 1

        # Pequena pausa entre requests para não sobrecarregar
        if i < len(targets):
            time.sleep(2)

    conn.close()

    print(f"\n{'─'*60}")
    print(f"Resultado: {ok} atualizados | {skip} sem conteúdo | {fail} com erro")
    if skip > 0:
        print("  → Artigos 'sem conteúdo' provavelmente precisam de login renovado.")
        print("     Execute: python login.py")


if __name__ == "__main__":
    main()
