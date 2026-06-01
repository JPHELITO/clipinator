# BUILD_GUIDE — roteiro para o Claude Code

> **Para o usuário humano:** este arquivo é dirigido ao Claude Code. Abra um Claude Code nesta pasta e diga: **"leia BUILD_GUIDE.md e me guie pelo setup"**. O Claude vai conduzir o resto.

> **Para o Claude Code:** você está fazendo o setup do **ClipDesk** — uma ferramenta que gera um e-mail (.eml) com clipping de notícias a partir de URLs. Este guia é seu roteiro. Siga **na ordem**, fase por fase, e **respeite os checkpoints**: nenhuma fase avança sem o teste explícito passando. Pular checkpoints quebra o produto em produção.

---

## O que existe no projeto

```
clipdesk-template/
├── extractors.py     # engine de parsing — NÃO EDITE
├── clipdesk.py     # orquestração e CLI — NÃO EDITE
├── config.py         # branding (você edita na FASE 1)
├── sources.py        # fontes (você edita na FASE 2)
├── login.py          # export de cookies (FASE 2.4 se houver paywall)
├── requirements.txt
├── cookies/          # populado por login.py (vazio agora)
└── out/              # .eml saem aqui
```

Você só edita `config.py` e `sources.py`. Não invente novos arquivos. Não mexa em `extractors.py` ou `clipdesk.py`.

---

## FASE 0 — Pré-flight

**0.1.** Pergunte ao usuário em qual sistema ele está (Windows / macOS / Linux) — só pra calibrar comandos. Se já souber pelo ambiente, pule.

**0.2.** Confirme Python 3.10+:
```
python --version
```
Se < 3.10, peça ao usuário pra instalar/atualizar antes de continuar. Não tente prosseguir.

**0.3.** Instale dependências:
```
pip install -r requirements.txt
```

**0.4.** Teste import do engine — isso valida que tudo instalou ok:
```
python -c "from clipdesk import scrape, build_eml; print('engine ok')"
```
Deve imprimir `engine ok`. Se der erro de import, resolva antes de seguir.

**Checkpoint 0:** `engine ok` impresso. Só então avance.

---

## FASE 1 — Branding (config.py)

Faça **uma pergunta de cada vez** ao usuário. Não despeje um formulário gigante.

**1.1. Nome do produto.** Pergunte: *"Como você quer chamar o seu clipping? (ex.: 'Energy Daily', 'Tech News Brief', 'Resumo do Mercado')"*. Edite `PRODUCT_NAME` em `config.py`.

**1.2. Idioma da data.** Pergunte: *"Quer a data do header em português ('27 de abril de 2026') ou inglês ('27 April 2026')?"*. Edite `DATE_LANGUAGE` (`"pt"` ou `"en"`).

**1.3. Título da seção de bullets.** Default `"Main Headlines"`. Pergunte: *"Quer mudar o título 'Main Headlines' (que aparece logo abaixo do header)? Ex.: 'Principais Manchetes', 'Top Stories'."*. Se sim, edite `HEADLINES_TITLE`.

**1.4. Cor de destaque.** Default `#FF5000` (laranja). Pergunte: *"Quer manter o laranja `#FF5000` no header, ou trocar por outra cor (hex)?"*. Se trocar, edite `HEADER_COLOR`.

**1.5. Bloco de assinatura/equipe.** Pergunte: *"Quer um bloco de assinatura (nome/email/equipe) abaixo do índice, antes das matérias? Posso montar pra você. Ou prefere deixar sem?"*.
- Se SIM: peça os campos (ex.: nome do remetente + email; ou lista de pessoas da equipe). Monte um HTML simples no padrão Outlook (use `<p class="MsoNormal">` e `<a href="mailto:...">`). Atribua a `SIGNATURE_BLOCK_HTML` em `config.py`. Ex.:
  ```python
  SIGNATURE_BLOCK_HTML = (
      '<p class="MsoNormal">'
      '<b><span style="color:#FF5000">Equipe Energy Daily</span></b>'
      '</p>'
      '<p class="MsoNormal">'
      '<b>João Silva /</b>&nbsp;'
      '<a href="mailto:joao@exemplo.com">joao@exemplo.com</a>'
      '</p>'
  )
  ```
- Se NÃO: deixe `SIGNATURE_BLOCK_HTML = ""`.

**1.6.** Mostre o `config.py` final ao usuário e peça confirmação: *"Está assim. Pode seguir para fontes?"*

**Checkpoint 1:** usuário confirmou config.py. Só então avance.

---

## FASE 2 — Adicionar fontes (sources.py)

Esta é a fase crítica. Cada fonte é adicionada **com teste real obrigatório** antes de registrar.

### 2.1. Levante a lista de fontes

Pergunte: *"Quais sites/portais de notícias você quer incluir? Me dá uma lista — pode ser nome dos sites + um link de exemplo de uma matéria recente de cada um (não a homepage, uma matéria de verdade)."*

Espere a lista. Se o usuário só passar nomes sem URLs de exemplo, **peça URLs de exemplo** — você precisa delas pra testar. Não invente URLs.

### 2.2. Loop por fonte

**Para cada site da lista, execute o sub-roteiro 2.3–2.7. Não pule. Não processe vários em paralelo. Faça um, valide, vá pro próximo.**

### 2.3. Identifique o domínio

Da URL de exemplo, extraia o domínio (netloc, sem `https://`). Considere se o site usa subdomínios (ex.: `g1.globo.com` vs `oglobo.globo.com` são fontes diferentes; `www.exemplo.com` e `exemplo.com` são o MESMO site mas precisam de duas entradas).

Pergunte ao usuário: *"O nome legível dessa fonte (que vai aparecer no e-mail) é '<chute baseado no site>'. Ok ou quer mudar?"*

### 2.4. Tente com `ex_auto` primeiro

Adicione em `sources.py` (sem committar mentalmente ainda — você vai validar):

```python
SOURCE_NAMES = {
    ...,
    "exemplo.com": "Nome Legível",
    "www.exemplo.com": "Nome Legível",
}
EXTRACTORS = {
    ...,
    "exemplo.com": ex_auto,
    "www.exemplo.com": ex_auto,
}
```

Rode o teste:
```
python -c "from clipdesk import scrape; r=scrape('URL_DE_EXEMPLO_AQUI'); print('paragrafos:', len(r.paragrafos)); print('chars:', sum(len(p) for p in r.paragrafos)); print('titulo:', r.titulo); print('---'); print(r.paragrafos[0][:300] if r.paragrafos else '(vazio)'); print('---'); print(r.paragrafos[-1][:300] if r.paragrafos else '')"
```

**Critério de aprovação automática:**
- `paragrafos >= 3`, E
- `chars >= 600`, E
- O primeiro parágrafo é texto real da matéria (não "Você atingiu o limite", não "Faça login", não menu/breadcrumb).

Se passar nos 3 → **vá pra 2.7** (fonte aprovada com `ex_auto`).

Se NÃO passar → 2.5.

### 2.5. Diagnóstico do que falhou

Rode:
```
python -c "from clipdesk import fetch_html, get_domain; from bs4 import BeautifulSoup; html=fetch_html('URL_DE_EXEMPLO_AQUI'); soup=BeautifulSoup(html,'lxml'); art=soup.find('article'); print('article tag:', 'sim' if art else 'nao'); cands=[(t.name, t.get('class') or [], t.get('id') or '', len(t.find_all('p'))) for t in soup.find_all(['div','section','main']) if len(t.find_all('p')) >= 3]; cands.sort(key=lambda x: -x[3]); [print(c) for c in cands[:10]]"
```

Isso lista as 10 tags `<div>/<section>/<main>` com mais `<p>` filhos — quase certo que o container do artigo está aí. Olhe os top 3: `(tag, classes, id, qtd_p)`.

**Casos:**

- **Top resultado tem classe ou id evidente do corpo do artigo** (`article-body`, `entry-content`, `noticia__corpo`, `mc-article-body`, etc.) → vá pra 2.6.

- **Nenhum candidato com >= 3 `<p>`** → o site provavelmente:
  - (a) renderiza o corpo via JavaScript (SPA) → este projeto não suporta. Avise o usuário: *"Esse site renderiza o corpo via JS. O ClipDesk não consegue raspar — vou marcar como não-suportado. Quando ele cair no Excel, vai precisar usar a coluna 'corpo' manual."*. **Mantenha registrado em `sources.py` mesmo assim com `ex_auto`** — assim a coluna `corpo` manual funciona. Vá pra 2.7.
  - (b) está bloqueando seu fetch (403, Cloudflare) → tente `IMPERSONATE_DOMAINS`: edite `sources.py`, adicione o domínio em `IMPERSONATE_DOMAINS = {"exemplo.com", "www.exemplo.com"}`. Re-rode 2.4. Se ainda falhar, é provavelmente paywall → vá pra 2.4.5 (cookies).
  - (c) tem paywall sem JS → vá pra 2.4.5.

- **HTML veio mas paragrafos < 3 mesmo com `<article>` presente** → 2.6.

### 2.4.5. Cookies de sessão (paywall)

Pergunte: *"Esse site tem paywall e você é assinante? Se sim, posso te guiar a exportar os cookies."*

Se SIM:
1. Peça pro usuário rodar (em outro terminal):
   ```
   python login.py exemplo.com www.exemplo.com
   ```
2. Ele vai fazer login no Chrome que abrir e apertar ENTER.
3. Quando ele confirmar que terminou, re-rode 2.4. Agora deve passar.
4. Se ainda falhar mesmo logado, provavelmente é problema de seletor → 2.6.

Se NÃO (sem assinatura): registre com `ex_auto` mesmo assim, avise que essa fonte só vai funcionar via coluna `corpo` manual no Excel, e vá pra 2.7.

### 2.6. Extractor customizado

Com base no diagnóstico de 2.5, escreva um extractor dedicado em `sources.py`:

```python
ex_meusite = _make_extractor([
    "div.article-body",       # primeiro candidato (mais específico)
    "div.entry-content",      # backup 1
    "article",                # backup final (sempre por último)
])
```

E aponte o domínio pra ele:
```python
EXTRACTORS = {
    ...,
    "exemplo.com": ex_meusite,
    "www.exemplo.com": ex_meusite,
}
```

Re-rode 2.4. Se passar nos 3 critérios → 2.7.

Se ainda falhar depois de 2 tentativas com seletores diferentes → admita ao usuário: *"Não consegui um seletor confiável pra esse site em 2 tentativas. Deixei registrado com ex_auto — quando essa fonte aparecer, ela pode falhar e você vai precisar colar o texto na coluna 'corpo' do Excel."*. Vá pra 2.7.

### 2.7. Confirme com o usuário

Mostre o trecho que você adicionou em `sources.py` e diga: *"Fonte X registrada e testada (Y parágrafos extraídos). Próxima?"*. Volte pra 2.3 com o próximo site.

### 2.8. Fim do loop

Quando todas as fontes passaram pelo loop, diga ao usuário quantas foram registradas com sucesso e quantas ficaram marcadas como "manual only".

**Checkpoint 2:** todas as fontes da lista foram processadas (cada uma com teste passando OU explicitamente registrada como manual-only). Só então avance.

---

## FASE 3 — Smoke test

**3.1. Crie o links.xlsx de teste.** Peça ao usuário **2 ou 3 URLs reais** (de fontes que você acabou de registrar — preferencialmente as que passaram em 2.4 sem custom extractor). Rode:

```
python -c "import openpyxl; wb=openpyxl.Workbook(); ws=wb.active; ws.append(['url','corpo']); ws.append(['URL_1', '']); ws.append(['URL_2', '']); ws.append(['URL_3', '']); wb.save('links.xlsx'); print('ok')"
```

Substitua `URL_1`, `URL_2`, `URL_3` pelas URLs reais.

**3.2. Rode o pipeline completo:**
```
python clipdesk.py links.xlsx
```

Espere ver `OK: <fonte> - <título>` para cada URL e no final `Arquivo gerado: out/clipping_YYYY-MM-DD.eml`.

**3.3. Peça ao usuário pra abrir o .eml** (no Outlook, ou só clicar duplo no arquivo).

**3.4. Pergunte explicitamente:**
- *"O cabeçalho está com o nome do produto e a cor certos?"*
- *"O índice de Headlines apareceu com bullets?"*
- *"O bloco de assinatura saiu como esperado?"* (se tiver configurado)
- *"O corpo de cada matéria está com texto real (não teaser, não menu)?"*
- *"O 'Fonte:' embaixo de cada matéria tem o link clicável?"*

Se qualquer resposta for "não" → debug específico da parte que falhou (não regrida do zero):
- Branding errado → `config.py`.
- Corpo errado de uma fonte específica → revisita 2.5/2.6 daquela fonte.

**Checkpoint 3:** usuário confirmou que o .eml saiu como esperado. Só então avance.

---

## FASE 4 — Wrap-up

**4.1.** Diga ao usuário que o setup acabou e mostre os comandos do dia-a-dia (já estão em `README.md`):
- Edita `links.xlsx` com as URLs do dia.
- Roda `python clipdesk.py links.xlsx`.
- Abre o `.eml` no Outlook e envia.

**4.2.** Liste fontes que ficaram "manual only" (se houver) — pra ele saber que precisa preencher a coluna `corpo` quando essas caírem no Excel.

**4.3.** Lembre que **cookies de paywall expiram** (semanas, às vezes dias). Se um dia uma fonte com paywall falhar, basta `python login.py <dominio>` de novo — o perfil do Chrome persiste.

**4.4. Apague este BUILD_GUIDE.md?** Pergunte se ele quer manter o arquivo (caso queira adicionar fontes depois) ou deletar. Default: manter.

---

## Apêndice A — Princípios

- **Não invente URLs.** Sempre peça ao usuário URLs reais pra testar.
- **Não pule checkpoints.** É tentador ver "parece ok" e seguir — não siga.
- **Adicionou uma fonte que passou no teste mas pareceu marginal (ex.: paragrafos == 3, chars == 600)?** Avise o usuário: *"Passou no limite mínimo, pode ser frágil. Quer testar com mais 1 URL desse site antes de eu seguir?"*
- **Erros silenciosos são piores que erros barulhentos.** Se algo deu errado e você não sabe o quê, **diga ao usuário** e peça orientação. Não chute.
- **Não edite `extractors.py` nem `clipdesk.py`.** Se parece que precisa, é porque você está resolvendo o problema errado — pare e pergunte.

## Apêndice B — Sintomas → causa provável

| Sintoma no teste 2.4 | Causa provável | O que fazer |
|---|---|---|
| `paragrafos == 0` | Seletor errado OU article container ausente | 2.5 → 2.6 |
| `paragrafos >= 3` mas chars < 600 | Pegou só lead/teaser; corpo está atrás de paywall | 2.4.5 (cookies) |
| Texto começa com "Você atingiu", "Faça login", "Continue lendo" | Paywall ativo | 2.4.5 |
| Erro `403 Forbidden` no fetch | TLS fingerprint barrado por Cloudflare | adicionar a `IMPERSONATE_DOMAINS` |
| Erro `Domínio não cadastrado` | Esqueceu de adicionar a entrada em `SOURCE_NAMES` ou `EXTRACTORS` | 2.4 — verifique que ambos os dicts têm a chave |
| Título vem com sufixo do site (`Notícia X — Exemplo News`) | normal, é stripped depois | nada a fazer |
| Parágrafos repetidos / lixo de "Leia também", "Compartilhe" | normal, são removidos pelo `clean_paragraphs` | nada a fazer |
| Build do .eml em 3.2 falhou em uma URL | seletor frágil dessa fonte específica | revisita 2.5/2.6 só dessa fonte |
