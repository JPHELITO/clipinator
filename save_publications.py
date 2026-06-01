"""Salva as publicações do cache do Itaú BBA no banco de dados."""
from newshunter.itaubba_scraper import collect_itaubba_publications
from newshunter.store import save_publications

pubs = collect_itaubba_publications()
if pubs:
    save_publications([
        {"title": p.title, "pdf_url": p.pdf_url, "report_url": p.report_url}
        for p in pubs
    ])
    print(f"OK — {len(pubs)} publicações salvas no banco.")
else:
    print("Nenhuma publicação encontrada. Verifique o cache.")
