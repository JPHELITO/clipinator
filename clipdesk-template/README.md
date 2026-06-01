# ClipDesk

Gera um `.eml` (pronto para abrir no Outlook e enviar) a partir de uma lista de URLs de notícias em um Excel.

> **Setup inicial:** veja [`START_HERE.md`](START_HERE.md). Este README assume que o setup já foi feito.

## Estrutura

```
clipdesk/
├── clipdesk.py    # pipeline (NÃO EDITE)
├── extractors.py    # engine de parsing (NÃO EDITE)
├── config.py        # branding (nome do produto, cor, assinatura)
├── sources.py       # fontes suportadas (domínio → extractor)
├── login.py         # exporta cookies da sessão logada no Chrome
├── requirements.txt
├── cookies/         # cookies por domínio (Netscape format)
├── chrome_profile/  # perfil Chrome dedicado (persistente)
└── out/             # .eml gerados
```

## Uso diário

1. Abra `links.xlsx`. Cole as URLs do dia na coluna **url** (1 por linha, na ordem que devem aparecer no e-mail).
2. (Opcional) Na coluna **corpo**, cole o texto manual se um site falhar (paywall sem cookie, layout quebrado etc.).
3. Rode:
   ```
   python clipdesk.py links.xlsx
   ```
4. Abra `out/clipping_YYYY-MM-DD.eml` no Outlook, confira e clique **Enviar**.

### Opções

- `--data 2026-04-23` — força uma data específica no cabeçalho (default: hoje).
- `--out pasta` — muda a pasta de saída (default: `./out`).

## Paywall

O pipeline tenta nesta ordem:

1. **Cookies de sessão logada** (`cookies/<dominio>.txt`) — recomendado.
2. **curl_cffi** (TLS fingerprint de Chrome real) — bypassa Cloudflare, ativado pelos domínios em `IMPERSONATE_DOMAINS` (em `sources.py`).
3. **Wayback Machine** — versão arquivada, se o original vier truncado.
4. **Coluna `corpo` manual** no Excel — último recurso.

### Exportando cookies

```
python login.py <dominio1> <dominio2>
```

Ex.:
```
python login.py exemplo.com outro.com.br
```

Abre o Chrome num perfil dedicado (`./chrome_profile`). Você loga nos sites, volta ao terminal e aperta ENTER. Os cookies ficam em `cookies/*.txt`. O perfil persiste, então da próxima vez já está logado — é só rodar de novo e apertar ENTER.

### Testando rapidamente se o cookie funcionou

```
python -c "from clipdesk import scrape; r=scrape('SUA_URL_AQUI'); print(len(r.paragrafos), 'paragrafos'); print(r.paragrafos[0][:200])"
```

Se aparecerem parágrafos com texto real da matéria (não teaser), o login está valendo.

## Adicionando uma fonte nova depois do setup inicial

Reabra um Claude Code na pasta e diga: *"adicione uma fonte nova ao ClipDesk: <URL de exemplo>. Siga o sub-roteiro 2.3–2.7 do BUILD_GUIDE.md."*

Ou, se for fazer manualmente, edite `sources.py`:

1. Adicione `"dominio.com": "Nome Legível"` em `SOURCE_NAMES`.
2. Adicione `"dominio.com": ex_auto` em `EXTRACTORS` (genérico serve pra maioria).
3. Teste:
   ```
   python -c "from clipdesk import scrape; r=scrape('URL_DE_TESTE'); print(len(r.paragrafos), r.titulo)"
   ```
4. Se ex_auto não pegar o corpo, crie um extractor dedicado:
   ```python
   ex_novosite = _make_extractor(["div.article-body", "article"])
   ```
   E aponte: `"dominio.com": ex_novosite`.

## Ajustando uma fonte que mudou de layout

Se uma fonte parou de extrair direito (ex.: `Corpo vazio em ...`), o site provavelmente mudou o HTML. Edite o extractor desse site em `sources.py`:

1. Inspecione a página no navegador (F12), ache o container do artigo.
2. Atualize a lista de seletores CSS do `ex_<site>` correspondente apontando pro novo container.
3. Teste com o comando de cima.

## Troubleshooting

- **"Corpo vazio em ..."** → seletor do site mudou. Ajuste o `ex_<site>` em `sources.py`.
- **"Conteúdo parece estar atrás de paywall"** → cookies expiraram ou nunca foram exportados. Rode `python login.py <dominio>` de novo.
- **"Domínio não cadastrado"** → adicione o site em `SOURCE_NAMES` + `EXTRACTORS` (ver seção acima).
- **403 Forbidden** → adicione o domínio em `IMPERSONATE_DOMAINS` (em `sources.py`).
- **Cookie não está sendo aplicado** → confira que `cookies/<dominio>.txt` existe e que o login funcionou no Chrome antes de apertar ENTER em `login.py`.
