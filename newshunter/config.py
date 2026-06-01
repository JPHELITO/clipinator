"""Defaults de keywords e janela de tempo — Setor S&M, P&P e Cement."""
from __future__ import annotations

DEFAULT_KEYWORDS: list[str] = [
    # ── Steel & Mining — empresas ──────────────────────────────────────────
    # "Vale" sozinho gera FP em PT (vale = "is worth/voucher") → usar VALE3 e variantes
    "VALE3",
    "Vale S.A.", "Vale SA",
    "Vale mining",          # inglês para Google News
    "Gerdau",
    "GGBR",                 # ticker Gerdau
    "CSN",
    "Usiminas",
    "Ternium",
    "ArcelorMittal",
    "Metalúrgica Gerdau", "Metalurgica Gerdau",
    "Companhia Siderúrgica Nacional",
    # Grandes produtores globais de minério de ferro (frequentes no Mining.com)
    "BHP",
    "Rio Tinto",
    "Fortescue",
    "Anglo American",
    "Nippon Steel",
    "POSCO",
    "Tata Steel",
    # ── Steel & Mining — commodities ───────────────────────────────────────
    "minério de ferro", "minerio de ferro",
    "iron ore",
    "aço inox", "aco inox",         # mais específico que "aço" sozinho
    "aço plano", "aco plano",
    "indústria do aço", "industria do aco",
    "mercado de aço", "mercado de aco",
    "preço do aço", "preco do aco",
    "steel",
    "HRC",
    "slab",
    "billet",
    "carvão siderúrgico", "carvao siderurgico",
    "coking coal",
    "pellet",
    "pelota",
    "siderurgia",
    "steelmaking",
    "mineração", "mineracao",
    # ── Pulp & Paper — empresas ────────────────────────────────────────────
    "Suzano",
    "Klabin",
    "Empresas Copec",
    "CMPC",
    "Veracel",
    "Bracell",
    "Eldorado Celulose",     # evita FP com "Eldorado" genérico
    # ── Pulp & Paper — commodities ─────────────────────────────────────────
    "celulose",
    "pulp and paper",       # mais específico que "pulp" sozinho
    "market pulp",
    "papel e celulose",     # contexto setorial
    "eucalipto",
    "BHKP",
    "BEKP",
    # ── Cement — empresas ──────────────────────────────────────────────────
    "Votorantim Cimentos",
    "Intercement",
    "CSN Cimentos",
    "Itambé",
    "Nassau Cimentos",      # evita FP com "Nassau" genérico
    "Lafarge Holcim", "LafargeHolcim",
    # ── Cement — commodity ─────────────────────────────────────────────────
    "indústria de cimento", "industria de cimento",
    "mercado de cimento",
    "preço do cimento", "preco do cimento",
    "cement",
    "clinker",
]

# Keywords específicos para queries Google News em inglês (Platts, Fastmarkets).
# Mantidos curtos para não ultrapassar o limite de URL do Google News (~2000 chars).
ENGLISH_SITE_KEYWORDS: list[str] = [
    # Commodities
    "steel", "iron ore", "HRC", "slab", "billet", "coking coal", "pellet",
    "steelmaking", "mining", "copper",
    "pulp", "BHKP", "BEKP", "paper",
    "cement", "clinker",
    # Empresas globais (inglês)
    "ArcelorMittal", "BHP", "Rio Tinto", "Anglo American", "Fortescue",
    "Nippon Steel", "POSCO", "Tata Steel", "Ternium",
    "Vale", "Gerdau", "CSN", "Usiminas",
    "Suzano", "Klabin", "CMPC", "LafargeHolcim",
]

DEFAULT_WINDOW_HOURS: int = 72

# IDs de analistas do portal Itaú BBA Smart para coleta de publicações.
# Adicione IDs de outros analistas da equipe conforme necessário.
ITAUBBA_ANALYST_IDS: list[str] = [
    "004506788",   # Daniel Sasson
]

WINDOW_PRESETS: list[int] = [1, 3, 6, 12, 24, 48, 72, 168]

# Fontes conhecidas — usadas na UI de gerenciamento de filtros por fonte.
# type: API (scraper Playwright), RSS (feed RSS), Sitemap, Scraper (homepage)
# NOTA: www.fastmarkets.com (RSS público) foi removido — conteúdo capturado
#       via scraper direto do dashboard PP News (dashboard.fastmarkets.com).
KNOWN_SOURCES: list[dict] = [
    {"domain": "core.spglobal.com",         "name": "Platts",             "type": "API"},
    {"domain": "dashboard.fastmarkets.com",  "name": "Fastmarkets P&P",    "type": "API"},
    {"domain": "valor.globo.com",            "name": "Valor Econômico",    "type": "Sitemap"},
    {"domain": "www.estadao.com.br",         "name": "Estadão",            "type": "Sitemap"},
    {"domain": "www.mining.com",             "name": "Mining.com",         "type": "RSS"},
    {"domain": "www.elfinanciero.com.mx",    "name": "El Financiero",      "type": "Scraper"},
    {"domain": "www.worldcement.com",        "name": "World Cement",       "type": "Scraper"},
]

# Filtros por fonte — defaults para seeding inicial do DB.
# Após o primeiro boot, o DB é a fonte de verdade (INSERT OR IGNORE).
# Platts e Fastmarkets usam "*" pois já são filtrados no scraper/API.
# Mining.com e El Financiero têm keywords específicas do setor.
# Valor Econômico: keywords configuradas pelo usuário via UI (aba Fontes).
SOURCE_KEYWORDS: dict[str, list[str]] = {
    # Platts — scraper direto, já filtrado por setor na API; aceita tudo
    "core.spglobal.com": ["*"],
    "www.spglobal.com":  ["*"],
    # Fastmarkets PP News — dashboard já é focado em P&P; aceita tudo
    "dashboard.fastmarkets.com": ["*"],
    # Mining.com — foco em mineração e metais
    "www.mining.com": [
        "Iron Ore", "Steel", "Copper", "Mining", "BHP", "Rio Tinto",
        "Vale", "Fortescue", "Anglo American", "Coking Coal", "Pellet",
        "Nickel", "Zinc", "Aluminum", "Cobalt", "Lithium",
        "Simandou", "Critical Minerals",
    ],
    # Valor Econômico — keywords setoriais; usuário gerencia via UI
    "valor.globo.com": [
        "VALE3", "Vale S.A.", "Vale mining",
        "Gerdau", "GGBR", "CSN", "Usiminas",
        "BHP", "Rio Tinto", "Fortescue", "Anglo American",
        "Companhia Siderúrgica Nacional",
        "Minério de ferro", "Siderurgia", "Siderúrgica",
        "Mineração", "Ferro", "Aço", "Steel",
        "Suzano", "Klabin", "Celulose", "Papel e celulose",
        "Votorantim", "Cimento",
        "Ouro", "Cobre", "Terras raras", "Simandou",
        "SUZB", "KLBN", "USIM", "VALE",
    ],
    # Estadão — mesmas keywords setoriais do Valor; usuário gerencia via UI
    "www.estadao.com.br": [
        "VALE3", "Vale S.A.", "Vale mining",
        "Gerdau", "GGBR", "CSN", "Usiminas",
        "BHP", "Rio Tinto", "Fortescue", "Anglo American",
        "Companhia Siderúrgica Nacional",
        "Minério de ferro", "Siderurgia", "Siderúrgica",
        "Mineração", "Ferro", "Aço", "Steel",
        "Suzano", "Klabin", "Celulose", "Papel e celulose",
        "Votorantim", "Cimento",
        "Ouro", "Cobre", "Terras raras", "Simandou",
        "SUZB", "KLBN", "USIM", "VALE",
    ],
    # El Financiero — homepage scraper; keywords de cimento/México
    "www.elfinanciero.com.mx": ["*"],
    # World Cement — site focado em cimento global; aceita tudo
    "www.worldcement.com": ["*"],
}
