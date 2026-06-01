"""Testa o scraper do Itaú BBA Smart diretamente, com logs visíveis."""
import logging
logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")

from newshunter.itaubba_scraper import collect_itaubba_publications

print("\n=== Iniciando coleta ===\n")
pubs = collect_itaubba_publications()

print(f"\n=== Resultado: {len(pubs)} publicação(ões) ===")
for i, p in enumerate(pubs, 1):
    print(f"\n[{i}] {p.title}")
    print(f"     PDF: {p.pdf_url}")
    print(f"     Rel: {p.report_url}")
