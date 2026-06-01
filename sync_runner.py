"""
Sync runner para GitHub Actions.

Roda o pipeline do clipinator e sincroniza artigos novos para o Supabase.

Uso:
    python sync_runner.py            # modo completo (RSS + Playwright)
    python sync_runner.py --fast     # modo rápido (RSS + requests, sem Playwright)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Carrega .env se existir (local dev)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("sync_runner")

# ── Diretório de cookies ───────────────────────────────────────────────────────
# Localmente: ../news_generator/cookies/ (igual ao padrão dos scrapers)
# Em CI (GitHub Actions): mesmo caminho relativo ao workspace
COOKIES_DIR = Path(__file__).resolve().parent.parent / "news_generator" / "cookies"


def _write_secret(env_var: str, filename: str) -> None:
    """Escreve conteúdo de variável de ambiente como arquivo de cookie/credencial.
    No-op silencioso se a variável não estiver definida."""
    content = os.environ.get(env_var, "").strip()
    if not content:
        return
    COOKIES_DIR.mkdir(parents=True, exist_ok=True)
    dest = COOKIES_DIR / filename
    dest.write_text(content, encoding="utf-8")
    log.info("Secret escrito: %s → %s", env_var, dest)


def setup_secrets() -> None:
    """Restaura arquivos de sessão e credenciais a partir de secrets do CI."""
    # Sessões Playwright (JSON completo do estado do browser)
    _write_secret("PLATTS_STATE_JSON",       "platts_state.json")
    _write_secret("FASTMARKETS_STATE_JSON",  "fastmarkets_state.json")
    _write_secret("VALOR_STATE_JSON",        "valor_state.json")
    _write_secret("ESTADAO_STATE_JSON",      "estadao_state.json")
    # Credenciais de auto-login (JSON: {"email": "...", "password": "..."})
    _write_secret("FASTMARKETS_CREDENTIALS", "fastmarkets_credentials.json")
    _write_secret("VALOR_CREDENTIALS",       "valor_credentials.json")
    _write_secret("ESTADAO_CREDENTIALS",     "estadao_credentials.json")


def main() -> None:
    parser = argparse.ArgumentParser(description="Clipinator sync runner")
    parser.add_argument(
        "--fast", action="store_true",
        help="Modo rápido: apenas RSS + requests (sem Playwright/Chromium)",
    )
    args = parser.parse_args()

    log.info("=== sync_runner start (fast=%s) ===", args.fast)
    setup_secrets()

    from newshunter.store import init_db, cleanup_old_articles
    from newshunter.pipeline import run_search

    init_db()
    cleanup_old_articles(72)

    stats = run_search(
        fast_mode=args.fast,
        skip_playwright=args.fast,
        hours_override=48,
    )
    log.info("=== sync_runner done: %s ===", stats)


if __name__ == "__main__":
    main()
