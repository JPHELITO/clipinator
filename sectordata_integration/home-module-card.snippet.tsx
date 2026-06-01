// =============================================================================
// Card do modulo News Hunter — cole no arquivo da home do SectorData
// (provavelmente `src/app/(dashboard)/home/page.tsx`), dentro do grid de
// modulos ja existente, seguindo o padrao dos outros (sales-volumes,
// navios-diesel, market-share, etc).
//
// Ajuste as classes Bootstrap/Tailwind para casar com o estilo do SectorData
// caso ele use um wrapper de "ModuleCard" ou componente proprio.
// =============================================================================

import Link from "next/link";

// Variante 1 — se o SectorData ja tem <ModuleCard>, use esse padrao:
//
//   <ModuleCard
//     href="/news-hunter"
//     title="News Hunter"
//     description="Varredura de noticias O&G em ~60 fontes (RSS + Google News) a cada 30s"
//     icon="📰"
//   />

// Variante 2 — se o padrao e um <Link> com Bootstrap card:
<Link href="/news-hunter" className="col-md-6 col-lg-4 text-decoration-none">
  <div className="card h-100 shadow-sm">
    <div className="card-body">
      <div className="d-flex align-items-center mb-3">
        <span className="fs-3 me-2">📰</span>
        <h5 className="card-title mb-0">News Hunter</h5>
      </div>
      <p className="card-text text-muted">
        Manchetes de petróleo &amp; gás de ~60 fontes (Valor, Brasil Energia,
        Petrobras, Reuters, Bloomberg…) com auto-refresh de 30s.
      </p>
    </div>
  </div>
</Link>
