# Clipinator — IBBA Oil & Gas News

Gera um `.eml` (pronto para abrir no Outlook e enviar) a partir de uma lista de URLs de notícias em um Excel.

## Estrutura

```
clipinator/
├── clipinator.py       # pipeline completo (fontes, extractors, scraper, .eml, CLI)
├── login.py            # exporta cookies da sessão logada no Chrome (via DevTools Protocol)
├── links.xlsx          # entrada: coluna `url` (1 por linha) e `corpo` opcional
├── requirements.txt
├── cookies/            # cookies exportados por domínio (Netscape format)
├── chrome_profile/     # perfil Chrome dedicado (persistente — fica logado)
└── out/                # .eml gerados
```

## Instalação (uma vez só)

Requer Python 3.10+.

```
pip install -r requirements.txt
```

## Uso diário

1. Abra `links.xlsx`, cole as URLs do dia na coluna **url** (1 por linha, na ordem em que devem aparecer no e-mail).
2. (Opcional) Na coluna **corpo**, cole o texto manual se um site falhar (paywall sem cookie, layout quebrado etc.).
3. Rode:

```
python clipinator.py links.xlsx
```

4. Abra `out/ibba_oil_gas_news_YYYY-MM-DD.eml` no Outlook, confira e clique **Enviar**.

### Opções

- `--data 2026-04-23` — força uma data específica no cabeçalho (default: hoje).
- `--out pasta` — muda a pasta de saída (default: `./out`).

## Paywall (Valor, Brasil Energia e similares)

O pipeline tenta nesta ordem:

1. **Cookies de sessão logada** (`cookies/<dominio>.txt`) — recomendado.
2. **curl_cffi** (TLS fingerprint de Chrome real) — bypassa Cloudflare.
3. **Wayback Machine** — versão arquivada, se o original vier truncado.
4. **Coluna `corpo` manual** no Excel — último recurso.

### Exportando cookies (1 vez a cada algumas semanas)

```
python login.py
```

Abre o Chrome num perfil dedicado (`./chrome_profile`). Você loga em Valor e Brasil Energia, volta ao terminal e aperta ENTER. Os cookies ficam em `cookies/*.txt`. O perfil persiste, então da próxima vez já está logado — é só rodar de novo e apertar ENTER.

Para exportar cookies de outros domínios:
```
python login.py www.outrosite.com outromais.com.br
```

### Testando rapidamente se o cookie funcionou

```
python -c "from clipinator import scrape; r=scrape('SUA_URL_AQUI'); print(len(r.paragrafos), 'paragrafos'); print(r.paragrafos[0][:200])"
```

Se aparecerem parágrafos com texto real da matéria (não teaser), o login está valendo.

## Fontes suportadas

~50 domínios brasileiros e internacionais:

**Imprensa geral:** Valor Econômico, Estadão, Folha de S. Paulo, O Globo, G1, R7, Metrópoles, Veja, IstoÉ Dinheiro, CNN Brasil, CNN, Correio Braziliense, UOL, Terra, Opera Mundi, Brasil 247, Conjur.

**Economia / Mercado:** InfoMoney, Bloomberg Línea, Brazil Journal, InvestNews, NeoFeed, Exame, Money Times, TradingView, Investing.com, CNBC, Reuters, The Edge Singapore.

**Energia / Oil & Gas:** Brasil Energia, eixos, Agência Petrobras, Click Petróleo e Gás, Pipeline (Valor), MegaWhat, INEEP, Argus Media.

**Setor público / infra:** Agência iNFRA, Senado Federal, Observatório Firjan, Poder360, Diário do Poder, O Bastidor, Cláudio Dantas, Vero Notícias, TC Online.

**Outros:** Times Brasil, Visão Agro, Agribiz, Globo Rural, CBN, Monitor Mercantil, Estradão, Visno Invest.

URLs de domínios não cadastrados geram erro claro no console (não entram no e-mail).

## Adicionando um site novo

Edite `clipinator.py`:

1. Adicione `"dominio.com": "Nome Legível"` em `SOURCE_NAMES`.
2. Adicione `"dominio.com": ex_auto` em `EXTRACTORS` (genérico serve pra maioria). Para seletor customizado, crie `ex_novosite = _make_extractor([...])`.

## Ajustando um site que mudou de layout

Cada site tem um extractor em `clipinator.py` (`ex_globo`, `ex_folha`, `ex_estadao`, etc.). Se um portal mudou o HTML, edite a lista de seletores CSS desse extractor apontando para o container principal do artigo (ex.: `div.news-body`, `div.article-content`).

## Dashboard de busca (News Hunter)

Além do pipeline `.eml`, o projeto inclui um **dashboard web** (`newshunter/`) que varre as fontes cadastradas atrás de notícias recentes de petróleo/gás.

### Subir o servidor

```
uvicorn newshunter.app:app --reload --port 8000
```

Abra http://localhost:8000.

### Como funciona

- Varre ~60 feeds RSS e sitemaps Google News dos sites cadastrados + Google News `site:dominio` para sites sem RSS + homepage scrapers para sites que bloqueiam RSS (Agência Petrobras, Brasil Energia).
- Filtra por palavras-chave (default: Petrobras, petróleo, Vibra, Brava, Ultrapar, Ipiranga, PetroReconcavo, oil, gasolina, gás, diesel, combustível, OceanPact, Cosan, Raízen, Braskem, Compass, PRIO — editável na UI).
- Filtra por data de **publicação** (default 24h; ajustável pelo seletor de janela no topo: 1h/3h/6h/12h/24h/48h/72h/7d). Sem data real (RSS `pubDate`, sitemap `news:publication_date` ou `<meta article:published_time>` / JSON-LD `datePublished` do próprio artigo), o item é descartado — evita que links "fixados" em seções apareçam como "agora".
- Datas de `published_parsed` do feedparser são tratadas como UTC (`calendar.timegm`, não `time.mktime`). `published_at` que chega > `found_at + 5min` é considerado agendamento e é clampado para `found_at`.
- Enriquece cada match com snippet de 2-3 linhas (usa o RSS summary quando ≥ 150 chars; senão baixa o artigo e reusa os extractors de `clipinator.py`, que já lidam com paywalls Valor/Brasil Energia via `cookies/`). URLs já vistas em buscas anteriores reusam o snippet do banco — sem novo fetch.
- Pipeline em streaming: o enriquecimento começa assim que os primeiros feeds retornam, em paralelo com o restante da coleta.
- Persiste em `data/newshunter.db` (SQLite) — histórico, dedupe por URL (normalizada: `www.` removido, tracking params/fragment descartados), config.

### Controles do dashboard

- **🔍 Buscar agora** — dispara uma busca completa. Leva ~7–10s.
- **Barra de busca** — filtra as notícias já carregadas em tempo real (client-side), sem nova requisição ao servidor.
- **Janela** (dropdown) — altera a janela de publicação; a configuração persistente é gravada ao trocar e se aplica nas próximas buscas.
- **Palavras-chave** — chips clicáveis para remover; campo de texto para adicionar.

### Adicionando um feed RSS novo

Edite `newshunter/sources.py` e acrescente uma entrada em `RSS_FEEDS` com o domínio como chave e a URL do feed. Sites sem RSS conhecido vão para `NO_RSS_DOMAINS` (cobertos pelo Google News com `site:dominio`).

## Dashboard online (SectorData via Supabase) — opcional

O scanner pode espelhar cada artigo novo em uma tabela Supabase para que o dashboard SectorData (Vercel) exiba as mesmas notícias com auto-refresh de 30s.

Setup completo em [`sectordata_integration/SETUP.md`](sectordata_integration/SETUP.md). Resumo:

1. Rode [`sectordata_integration/migration.sql`](sectordata_integration/migration.sql) no SQL Editor do Supabase (cria `news_articles` + RLS).
2. Copie `.env.example` para `.env` e preencha `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` (service_role — local only).
3. `pip install -r requirements.txt` (adiciona `supabase` + `python-dotenv`).
4. Rode o scanner normalmente — novos artigos agora vão também para o Supabase.
5. Copie [`sectordata_integration/page.tsx`](sectordata_integration/page.tsx) para `src/app/(dashboard)/news-hunter/page.tsx` no repo SectorData.

Sem `.env` configurado, o Clipinator local funciona 100% como antes — a integração é opt-in.

## Troubleshooting

- **"Corpo vazio em ..."**: o seletor do site mudou. Inspecione a página (F12), ache o container do artigo e adicione o seletor no `ex_<site>` correspondente.
- **"Conteudo parece estar por tras de paywall"**: cookies expiraram ou nunca foram exportados. Rode `python login.py` de novo.
- **"Dominio nao cadastrado"**: adicione o site em `SOURCE_NAMES` + `EXTRACTORS` (ver seção acima).
- **Cookie não está sendo aplicado**: confira que `cookies/<dominio>.txt` existe e que o login funcionou no Chrome antes de apertar ENTER em `login.py`.
- **Artigo de homepage scraper não aparece no dashboard**: o pipeline exige data real. Se a página do artigo não tem `article:published_time`, `<time datetime=…>` nem JSON-LD `datePublished`, o item é silenciosamente descartado. Para incluí-lo, adicione o seletor de data no extractor do site ou garanta que o HTML exponha a data em `<meta>` / JSON-LD.
- **Títulos aparecendo como `projeto-cine-petrobras-chegar%C3%A1...`**: significa que o `fetch_html` do artigo estourou o deadline de enrich. O fallback decodifica o slug (`unquote` + capitaliza), mas o ideal é que a página seja fetcheada dentro do `ENRICH_DEADLINE` — se o domínio é recorrentemente lento, aumente o deadline em `newshunter/pipeline.py`.
