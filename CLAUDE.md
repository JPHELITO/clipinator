# CLAUDE.md — Clipinator: contexto do projeto

> Leia este arquivo inteiro antes de qualquer tarefa. Ele substitui a memória da sessão anterior.

---

## O que é este projeto

**Clipinator** — dashboard de clipping diário para equity research de S&M (Steel & Mining), P&P (Pulp & Paper) e Cimento, usado pelo analista do Itaú BBA.

- Pasta: `C:\Users\João Paulo Helito\clipinator\`
- Pacote principal: `newshunter/`
- Templates Jinja2: `newshunter_templates/`
- Banco de dados: `data/newshunter.db` (SQLite)
- Cookies/sessões: `C:\Users\João Paulo Helito\news_generator\cookies\`
- Iniciar: `python -m newshunter` → `http://localhost:8765`

---

## Arquitetura geral

```
pipeline.py  ← orquestra tudo
  ├── fetcher.py           ← RSS + sitemaps + homepage scrapers
  ├── filter.py            ← matches_keywords() com normalização Unicode
  ├── enrich.py            ← fetch_html para snippet + título
  ├── platts_scraper.py    ← Playwright API scraper (S&P Global)
  ├── fastmarkets_scraper.py ← Playwright scraper (dashboard PP News)
  ├── valor_scraper.py     ← Playwright sitemap scraper (Valor Econômico)
  ├── estadao_scraper.py   ← Playwright sitemap scraper (Estadão)
  ├── worldcement_scraper.py ← requests+BS4 scraper (World Cement — sem Playwright)
  ├── itaubba_scraper.py   ← Playwright scraper (publicações Itaú BBA Smart)
  └── store.py             ← SQLite upsert com "keep longer body" + tabela publications

app.py  ← FastAPI + Jinja2
  ├── GET  /            ← dashboard principal
  ├── GET  /headlines   ← feed rápido
  ├── GET  /sources     ← gerenciamento de fontes e keywords
  ├── GET  /clipping    ← seleção de artigos para email
  ├── POST /search      ← dispara pipeline.run_search()
  └── GET  /api/article ← leitor in-dash via playwright_reader.py

playwright_reader.py ← leitor in-dash para artigos paywalled
login.py             ← renova sessões Playwright (Fastmarkets, Platts, Valor, Itaú BBA)
```

---

## Fontes ativas (7)

| Fonte | Domínio | Tipo | Modo filtro | Arquivo |
|---|---|---|---|---|
| Platts | `core.spglobal.com` | API scraper | Aceitar tudo | `platts_scraper.py` |
| Fastmarkets P&P | `dashboard.fastmarkets.com` | API scraper | Aceitar tudo | `fastmarkets_scraper.py` |
| Valor Econômico | `valor.globo.com` | Sitemap+Playwright | 44 keywords específicas | `valor_scraper.py` |
| Estadão | `www.estadao.com.br` | Sitemap+Playwright | 34 keywords (mesmas do Valor) | `estadao_scraper.py` |
| Mining.com | `www.mining.com` | RSS (6 feeds) + scraper | 33 keywords específicas | RSS pipeline |
| El Financiero | `www.elfinanciero.com.mx` | Homepage scraper | 5 keywords (CEMEX, GCC…) | RSS pipeline |
| World Cement | `www.worldcement.com` | Listing scraper (requests+BS4) | Aceitar tudo | `worldcement_scraper.py` |

**Fontes removidas (não reativar):**
- `www.fastmarkets.com` RSS — conteúdo fora do escopo, substituído pelo scraper do dashboard
- GNews para Platts/Fastmarkets — substituídos pelos scrapers diretos

---

## Scrapers autenticados — detalhes críticos

### Platts (`platts_scraper.py`)
- **Fase 1**: intercepta POST `content-bff/v1/search` nas páginas de listagem (`#platts/allInsights` + `#platts/insightsResult?contentType=Market%20Commentary`) — extrai `Summary` (highlights HTML) + `Body`/`BodyText` (corpo) da resposta JSON da API
- **Fase 2**: navega artigo-a-artigo (máx `_MAX_BODY_FETCH=6`) via DOM walker (`PLATTS_DOM_WALK_JS`) que sempre usa `.newsSection-body[0]` (NUNCA longest-body heuristic)
- **Proteção anti-contaminação (Angular SPA race condition)**: o Angular hash routing atualiza `window.location.hash` IMEDIATAMENTE, mas o componente Angular pode demorar segundos para renderizar o novo conteúdo. Sem proteção, o DOM walker captura o artigo ANTERIOR.
  - **Fingerprint approach**: captura primeiros 150 chars do corpo ANTES de navegar; aguarda o conteúdo MUDAR depois da navegação (`wait_for_function`)
  - **Skip on timeout**: se o conteúdo não mudar em 12s (+ 3s extra), pula o artigo — melhor pular do que contaminar
  - **Stale content check**: compara início do corpo capturado com fingerprint anterior — se igual, descarta
  - **URL validation**: verifica que o `articleID` da URL target está em `window.location.href` — proteção secundária
- **Force-save DOM bodies**: após Fase 2, chama `save_article_body(url, body, force=True)` para cada artigo com `body_from_dom=True` — sobrescreve corpos antigos/contaminados independente do tamanho
- **`PLATTS_DOM_WALK_JS`** e **`platts_dom_items_to_html()`** em `html_utils.py`: DOM walker que retorna itens em ordem (texto + slots de imagem) e captura screenshots via `page.screenshot(clip=bounding_box)` (sem hover artifacts)
- State file: `news_generator/cookies/platts_state.json`
- **`platts_bulk_refresh.py`**: script standalone que visita todos os artigos Platts das últimas 72h e re-processa o corpo via DOM walker com `force=True`. Tem o mesmo sistema de fingerprint/skip que o scraper. Rodar quando há contaminação suspeita.
- **URL normalization**: `normalize_url()` em `store.py` decodifica e re-encoda o fragment do hash de forma canônica (`Market%20Commentary` ≡ `Market Commentary`) — evita mismatch de chave no DB

### Fastmarkets (`fastmarkets_scraper.py`)
- **Fase 1**: intercepta POST `/search/v3/query` no dashboard PP News
- **Fase 2**: navega artigos individuais via `/a/{RA-ID}` — fluxo polido:
  - Consulta DB para classificar artigos: `body < _GOOD_BODY_LEN (600 chars)` → precisa fetch; `>= 600` → já completo, pula
  - Ordena por body length ascending (mais curto/ausente primeiro)
  - Por artigo: `goto(domcontentloaded)` → `wait_for_selector` → `wait_for_load_state(networkidle)` → `wait_for_function(content > 200 chars)` → `evaluate(innerHTML)` → retry único se vazio
  - Budget: `_PHASE2_BUDGET = 250s`; thread timeout: 360s
  - Seletores JS centralizados em `_JS_SELECTORS`, reutilizados em `_JS_CONTENT_READY` e `_JS_EXTRACT`
- Auto-login: `_do_auto_login()` usa `auth.fastmarkets.com`, seletores `input[name='username']` + `input[id='login-button']`
- State file: `news_generator/cookies/fastmarkets_state.json`
- Credenciais: `news_generator/cookies/fastmarkets_credentials.json` → `{"email": "...", "password": "..."}`
- `_check_state_file()` só avisa sobre token expirado, não aborta (token OIDC em localStorage)
- **NÃO MEXER** na Fase 2 sem entender a lógica de priorização e o fluxo de waits

### Fastmarkets — Fase 2: detalhes críticos de implementação

- **`_GOOD_BODY_LEN = 600`**: artigos com corpo ≥ 600 chars no DB são pulados — conteúdo já completo
- **Priorização DB-aware**: consulta DB antes de iniciar Fase 2 → classifica artigos:
  - `needs_fetch` (body < 600): ordenados por tamanho crescente (mais curto/ausente = prioridade máxima)
  - `has_good` (body ≥ 600): pulados completamente
- **`_JS_SELECTORS`**: lista centralizada de seletores CSS, reutilizada em `_JS_CONTENT_READY` e `_JS_EXTRACT`
  - Seletores específicos (`.content-container`, `.article-container`, `[class*="article-body"]`, etc.) — sem `main` (muito amplo)
- **Fluxo por artigo** (com proteção anti-contaminação):
  1. **Fingerprint pré-navegação**: captura os primeiros 150 chars do container atual antes do `goto()`
  2. `goto(domcontentloaded)` → `wait_for_selector` → `wait_for_load_state(networkidle, 12s)`
  3. **`wait_for_function` fingerprint-aware**: aguarda conteúdo > 200 chars AND diferente do fingerprint anterior — protege contra React reutilizar DOM do artigo anterior
  4. **URL ID validation**: verifica que o `article_id` (ex. "RA250235") está na `canonical_url` — se não estiver, descarta corpo
  5. `evaluate(innerHTML)` → retry único se vazio (3s wait)
- **Budget**: `_PHASE2_BUDGET = 250s`; verifica `budget_left <= 5` antes de cada artigo; thread timeout: 360s
- **`networkidle`**: fundamental para React SPAs — garante que chamadas API do conteúdo terminaram antes de capturar

### Estadão (`estadao_scraper.py`)
- **Etapa 1**: urllib busca sitemap (4 candidatos em ordem: news sitemap → sitemap-index → category)
- **Etapa 2**: keyword filtering no título antes do Playwright
- **Etapa 3**: Playwright com `wait_until="domcontentloaded"` — captura HTML antes do Zephr JS rodar
- **Zephr bypass**: paywall do Estadão é 100% client-side. `domcontentloaded` captura conteúdo antes do overlay ser aplicado. Funciona sem login na maioria dos artigos.
- Extrai lide (`[class*='article__header-summary']`) + corpo (`[class*='article-body']`, etc.)
- Domain normalization: todos os subdomínios (economia.estadao.com.br, etc.) → `www.estadao.com.br`
- Auto-login: `acesso.estadao.com.br/login/` (diferente do Globo ID)
- State file: `news_generator/cookies/estadao_state.json`
- Credenciais: `news_generator/cookies/estadao_credentials.json`
- `_SKIP_AUTO_FETCH`: adicionado em `clipping.py` — não auto-fetchar artigos Estadão
- Paywall detection: marcadores específicos ("assine o estadão", "leia sem limites", "continue lendo com")

### World Cement (`worldcement_scraper.py`)
- **Método**: `requests + BeautifulSoup` — sem Playwright, sem autenticação (site livre)
- **Fase 1**: GET `/news/` (até 3 páginas via `?page=N`) → parseia `article.article`
  - Título + URL: `h2.article-title > a`
  - Data: `time[datetime]` → ISO `"2026-05-20 08:27:00Z"` → datetime UTC
  - Excerpt: `div.col-xs-5 > p` ou `div.col-xs-9 > p`
  - Filtro: janela de 72h; para na primeira página que não tem artigos novos
- **Fase 2**: ThreadPoolExecutor (6 workers) → GET individual de cada artigo
  - Container: `article.article-detail`
  - Remove: `.tab-pane, .tab-content, .tags-container, .row.row-btn, article > header, script, style, form`
  - Converte via `article_to_safe_html()`
- **Sem login**: site completamente livre — nenhum estado de sessão necessário
- **`_SKIP_AUTO_FETCH`**: adicionado em `clipping.py` — corpo já rico, não substituir com fetch genérico
- **`extract_article_container()`**: worldcement tem case dedicado em `html_utils.py`

### Valor Econômico (`valor_scraper.py`)
- **Etapa 1**: urllib busca `valor.globo.com/sitemap/valor/news.xml` (XML com ~350 artigos/72h)
- **Etapa 2**: keyword filtering no título antes do Playwright
- **Etapa 3**: Playwright extrai `.content-text` (lide) + `.wall` (corpo) — artigos livres têm classe `no-paywall`
- Auto-login: `login.globo.com/login/438` (serviço Valor = 438)
- State file: `news_generator/cookies/valor_state.json`
- Credenciais: `news_generator/cookies/valor_credentials.json`
- Paywall detection: `[class*="no-paywall"]` presente = livre; `[class*="paywall__wall"]` = paywalled

---

## Mining.com — comportamento e limitações

- **6 feeds RSS**: iron-ore, copper, nickel, gold (commodity), + steel e mining (categoria)
- **Corpo no pipeline**: `enrich_item` retorna cedo quando o snippet RSS é suficiente (> 150 chars) — **não faz fetch da página** → corpo fica vazio no DB após o pipeline
- **Corpo no leitor in-dash**: ao abrir artigo no leitor, `app.py` usa `article_to_safe_html(str(container))` (via `article` ou `role=main`) → salva com `save_article_body()` → fica disponível para próximo clipping
- **Auto-fetch no clipping** (`clipping.py`): antes de gerar o Word, faz fetch paralelo (4 threads) de artigos sem corpo que **não** estão em `_SKIP_AUTO_FETCH` — resolve o problema de artigos mining.com vazios no Word

### `_SKIP_AUTO_FETCH` (clipping.py)

Domínios com scrapers dedicados que **NÃO** devem ser auto-fetchados pelo clipping:
```python
_SKIP_AUTO_FETCH = frozenset(["core.spglobal.com", "dashboard.fastmarkets.com", "valor.globo.com", "www.estadao.com.br", "www.worldcement.com"])
```

### Clipping bilíngue — Valor, Estadão, El Financiero

Artigos dessas três fontes aparecem **duas vezes** no Word gerado:
1. **Original** (PT ou ES) — título com sufixo `(Original)`, corpo na língua nativa
2. **Free Translation** (EN) — título com sufixo `(Free Translation)`, corpo traduzido

**TOC (Sector Headlines):** um único bullet com `Título original \ Título traduzido [Fonte] (take)` — backslash como separador, hyperlink apenas no título original.

**Corpo:** original → tradução em sequência, sem page break entre eles.

**Tradução:** `_translate_to_english(title, body_html, source_lang)` em `clipping.py`:
- Usa `anthropic` SDK → modelo `claude-haiku-4-5` (rápido e barato)
- Prompt instrui: preservar HTML tags, proper nouns, números
- Corpo limitado a 12 000 chars para evitar tokens excessivos
- Tradução paralela (3 threads) ao gerar o clipping
- Em caso de falha: item fica monolíngue (sem quebrar o fluxo)

```python
_BILINGUAL_DOMAINS = frozenset(["valor.globo.com", "www.estadao.com.br", "www.elfinanciero.com.mx"])
_DOMAIN_LANG = {"valor.globo.com": "Portuguese", "www.estadao.com.br": "Portuguese", "www.elfinanciero.com.mx": "Spanish"}
```

`ClippingItem` tem campos opcionais `translated_title: str` e `translated_body: str` (default `""`).

### `_fetch_body_regular(url)` (clipping.py)

Para sites regulares (sem paywall/SPA): usa `requests` + `BeautifulSoup` → chama `extract_article_container(soup, url)` → passa por `article_to_safe_html()` → retorna HTML limpo. Resultado salvo em DB via `save_article_body()`.

### `extract_article_container(soup, url)` (html_utils.py)

Extração de container com lógica **por domínio**. Usado tanto pelo `_fetch_body_regular` do clipping quanto pelo leitor in-dash do `app.py`.

| Domínio | Seletor | Por quê |
|---|---|---|
| `www.mining.com` | `div.post-inner-content .content` | Evita capturar `div.more-news` e `section#more-news-section` que ficam como irmãos do `.content` mas dentro do mesmo `post-inner-content` |
| outros | `article` → `[role=main]` → `main` | Fallback genérico |

Para mining.com, após selecionar `.content`, remove inline ads (`div.d-flex.justify-content-center`), iframes embeds (`figure.wp-block-embed`) e qualquer residual de `.more-news` ou `.ad-slot` que possam existir dentro do container.

---

## Leitor in-dash (`playwright_reader.py`)

Abre artigo paywalled diretamente no modal da dashboard.

```python
PLAYWRIGHT_DOMAINS = {"core.spglobal.com", "dashboard.fastmarkets.com", "valor.globo.com", "www.estadao.com.br"}

_SITE_CONFIG = {
    "core.spglobal.com":        {"use_platts_flow": True, ...},    # Angular SPA: carrega home primeiro
    "dashboard.fastmarkets.com": {"body": [".content-container", ...]},
    "valor.globo.com":           {"use_valor_flow": True, ...},    # extrai .content-text + .wall
    "www.estadao.com.br":        {"use_estadao_flow": True, ...},  # domcontentloaded → bypass Zephr
}
```

Cada `use_*_flow` tem um branch dedicado em `_fetch_worker()`:
- `use_platts_flow`: carrega home → hash routing → `PLATTS_DOM_WALK_JS` (DOM walker via `html_utils.py`) → `platts_dom_items_to_html()` → inclui screenshots de imagens inline. Tem validação de articleID para evitar servir artigo errado por race condition Angular.
- `use_valor_flow`: domcontentloaded → DOM JSON (no_paywall/has_paywall/content_text/wall) → `article_to_safe_html()`
- `use_estadao_flow`: domcontentloaded (Zephr bypass) → multi-seletor JS (lede + body) → `article_to_safe_html()`
- genérico: networkidle → innerHTML seletores de `body` → `article_to_safe_html()`

---

## Fluxo de filtragem por fonte

```
pipeline._keep_candidate(item):
  1. Janela temporal (72h)
  2. Per-source keywords (DB) — prioridade máxima
     - mode=all   → aceita tudo (["*"])
     - mode=specific → filtra pela lista
     - mode=global → usa keywords globais
  3. Homepage scrapers sem config → aceita tudo (#topic)
  4. Keyword match no título + summary

valor_scraper.collect_valor_articles():
  → get_all_source_keywords()["valor.globo.com"] (44 kws)
  → matches_keywords(title, keywords) ANTES do Playwright
  → passa apenas candidatos ao _scrape_worker()
```

**Scrapers Platts/Fastmarkets/Valor** → `to_persist.extend()` direto → **bypassam `_keep_candidate()`**

---

## `login.py` — renovação de sessões

```
python login.py
```
Abre browser visível para os 3 sites na ordem:
1. **Fastmarkets** (`dashboard.fastmarkets.com`) → oferta de salvar credenciais
2. **Platts** (`core.spglobal.com`)
3. **Valor Econômico** (`valor.globo.com`) → oferta de salvar credenciais

Salva `*_state.json` (cookies + localStorage com token OIDC).

> **Estadão**: login manual via `login.py` ainda não implementado. O scraper tem `_do_auto_login()` interno mas não está exposto no fluxo interativo. Para renovar sessão Estadão manualmente, salve um `estadao_state.json` usando Playwright diretamente ou via script ad-hoc.

---

## `store.py` — lógica crítica

- `upsert_articles()`: body usa `CASE WHEN LENGTH(novo) > LENGTH(existente)` → **keep longer body**
- `source_modes`: `all` / `specific` / `global`
- `get_all_source_keywords()`: retorna `["*"]` para mode=all, lista para specific, ausente para global
- Cleanup automático 72h: `cleanup_old_articles()` chamado no startup e após cada busca completa

---

## `filter.py` — `matches_keywords()`

- Normaliza com `unicodedata.NFKD` → strip acentos + lowercase
- Regex `\b(?:keyword1|keyword2|...)\b` com `re.IGNORECASE`
- Cache LRU por tupla de keywords
- **Consequência**: `"Vale"` e `"VALE"` são a mesma keyword. Duplicatas foram removidas do DB.

---

## `html_utils.py` — funções principais

| Função | Uso |
|---|---|
| `article_to_safe_html(html)` | HTML bruto → `<p>`, `<ul>`, `<h3>`, `<img>` seguros |
| `innertext_to_html(text)` | innerText do browser → `<p>` por bloco |
| `_split_api_body(text)` | Separa sentenças concatenadas da API Platts |
| `PLATTS_DOM_WALK_JS` | JS que caminha `.newsSection-body[0]` e retorna items `{t, v/idx}` em ordem |
| `platts_dom_items_to_html(data, page)` | Constrói HTML Platts: texto→`<p>`, imagens→screenshot base64 inline |

---

## `config.py` — defaults

- `DEFAULT_KEYWORDS`: lista global (~53 keywords S&M + P&P + Cimento)
- `KNOWN_SOURCES`: 5 fontes exibidas na aba Fontes da UI
- `SOURCE_KEYWORDS`: defaults para seeding do DB (INSERT OR IGNORE — não sobrescreve)
- `DEFAULT_WINDOW_HOURS`: 48h (sobrescrito via UI)

---

## Estado atual do banco de dados

Fonte | Modo | Keywords
--- | --- | ---
`core.spglobal.com` | all | 27 armazenadas (não usadas — mode=all)
`dashboard.fastmarkets.com` | all | 8 armazenadas (não usadas — mode=all)
`valor.globo.com` | specific | 44 keywords (AUGO, AURA, Aço, BHP, CSN, Celulose…)
`www.mining.com` | specific | 33 keywords (Iron Ore, Steel, Copper, BHP, VALE…)
`www.elfinanciero.com.mx` | specific | 5 keywords (CEMEX, Cement, Cemento, GCC, Grupo México)
`www.worldcement.com` | all | * (aceita tudo — site focado em cimento)

---

## Arquivos de cookies/sessão necessários

```
C:\Users\João Paulo Helito\news_generator\cookies\
  ├── fastmarkets_state.json        ← Playwright state Fastmarkets
  ├── fastmarkets_credentials.json  ← {"email":"...","password":"..."}
  ├── platts_state.json             ← Playwright state Platts
  ├── valor_state.json              ← Playwright state Valor (opcional)
  ├── valor_credentials.json        ← {"email":"...","password":"..."}
  ├── estadao_state.json            ← Playwright state Estadão (opcional — Zephr bypass sem login)
  └── estadao_credentials.json      ← {"email":"...","password":"..."}
```

> **Nota Estadão**: o `estadao_state.json` é opcional. O scraper funciona sem login para a maioria dos artigos graças ao bypass Zephr via `domcontentloaded`. Login melhora cobertura de artigos premium.

---

## Regras invioláveis

1. **Platts — NUNCA usar longest-body heuristic**: o corpo Platts SEMPRE vem de `.newsSection-body[0]` (primeiro elemento). Qualquer código que escolha o "maior" elemento contamina artigos com conteúdo de outros. Usar `PLATTS_DOM_WALK_JS` que enforça `[0]` explicitamente.
2. **Platts — fingerprint obrigatório**: ao navegar artigo-a-artigo (Fase 2 e bulk_refresh), SEMPRE usar fingerprint + skip-on-timeout. Angular SPA atualiza o hash imediatamente mas o DOM demora — sem proteção, o corpo capturado é do artigo anterior.
3. **`save_article_body(force=True)`** para DOM bodies Platts: os corpos gerados pelo DOM walker devem ser force-saved ANTES de `upsert_articles()` para garantir que a versão correta (possivelmente mais curta) prevaleça sobre APIs bodies contaminados anteriores.
4. **NÃO mexer na Fase 2 do `fastmarkets_scraper.py`** sem entender a lógica de priorização DB-aware e o fluxo de waits (networkidle é crítico para React SPAs)
3. Ao modificar scrapers autenticados, manter o padrão `_do_auto_login()` + `_check_state_file()`
4. `upsert_articles()` já tem "keep longer body" — não duplicar essa lógica
5. `matches_keywords()` normaliza acentos — nunca adicionar a mesma keyword em maiúsculo e minúsculo
6. Scrapers (Platts, Fastmarkets, Valor) bypassam `_keep_candidate()` via `to_persist.extend()` — filtragem acontece dentro do scraper
7. `_SKIP_AUTO_FETCH` em `clipping.py` deve sempre conter Platts, Fastmarkets e Valor — evita sobrescrever corpo rico com fetch genérico

---

## `itaubba_scraper.py` — publicações de research

Coleta as 10 publicações mais recentes do portal Itaú BBA Smart.

- **State file**: `news_generator/cookies/itaubba_state.json`
- **Login**: `python login.py` → seleciona "Itaú BBA Smart Portal"
- **Sem auto-login**: portal usa SSO corporativo complexo — login manual necessário periodicamente
- **Fase 1**: navega `/analyst/{id}`, intercepta API JSON + fallback DOM walk para links `/report/{uuid}`
- **Fase 2**: navega cada `/report/{uuid}`, procura link `/viewer/equity/` (direto, iframe, data-*, onclick)
- **Fallback**: se PDF não encontrado, usa URL da página do relatório
- **IDs de analistas**: `config.py → ITAUBBA_ANALYST_IDS` (default: `["004506788"]` = Daniel Sasson)
- **Tabela DB**: `publications (rank, title, pdf_url, report_url, updated_at)` — substituída inteira a cada scrape
- **Integração pipeline**: roda em thread paralela; timeout 300s; resultado salvo via `store.save_publications()`
- **Clipping Word**: `Recent Publications` usa `store.get_recent_publications(10)` → formato:
  `SECTOR – Título do relatório – Click here for full report` (link externo clicável no PDF exportado)

## Pendências / próximos passos possíveis

- [ ] Fazer login no portal Itaú BBA Smart: `python login.py` → aguardar tela "Itaú BBA Smart Portal" → logar → salvar `itaubba_state.json`
- [ ] Configurar credenciais do Valor: `python login.py` → fazer login → salvar email/senha
- [ ] Verificar se Fastmarkets auto-login está funcionando (ou se precisa rodar `python login.py`)
- [ ] Avaliar keywords genéricas do Valor (`China`, `Papel`, `Ouro`) que geram falsos positivos
- [ ] Testar clipping completo com publicações preenchidas (após primeiro login Itaú BBA)
- [ ] Adicionar IDs de outros analistas da equipe em `config.py → ITAUBBA_ANALYST_IDS`

---

## Como iniciar uma nova sessão

1. Abra o Claude Code nesta pasta
2. Este arquivo é lido automaticamente — o assistente tem contexto completo
3. Use `/clear` se quiser limpar a memória sem perder o CLAUDE.md
4. Para atualizar este arquivo após mudanças: peça "atualize o CLAUDE.md"
