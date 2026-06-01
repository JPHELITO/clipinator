# Integração News Hunter → SectorData

Este diretório contém tudo o que você precisa para exibir o News Hunter (que roda localmente no seu PC) dentro do dashboard online SectorData (Vercel).

Arquivos:
- [`migration.sql`](migration.sql) — cria a tabela `news_articles` no Supabase.
- [`page.tsx`](page.tsx) — página Next.js com auto-refresh de 30s.
- [`home-module-card.snippet.tsx`](home-module-card.snippet.tsx) — trecho para a home do dashboard.

---

## Arquitetura resumida

```
Seu PC (Clipinator)  ──push service_role──►  Supabase (news_articles)
                                                    │
                                                    │  anon key + RLS
                                                    ▼
                                           Vercel (SectorData)
                                           /news-hunter (polling 30s)
```

- Scanner só roda quando seu PC estiver ligado. Vercel mostra sempre o que está no Supabase.
- Escrita bloqueada por RLS — só o scanner (service_role) escreve. Usuários do dashboard só leem.

---

## Passo 1 — Criar a tabela no Supabase

### 1.1 Pegue as credenciais

Acesse seu projeto no [Supabase Dashboard](https://supabase.com/dashboard) → **Project Settings** → **API**. Anote:

- **Project URL** (algo como `https://abcdefgh.supabase.co`)
- **service_role secret** (começa com `eyJhbGc...`, é a chave longa do campo "service_role")

> ⚠️ A **service_role** bypassa RLS. Use-a só no `.env` do seu PC. **Nunca** exponha no frontend, commit ou Vercel.

### 1.2 Rode a migration

Opção A — SQL Editor do Supabase (mais rápido):

1. No dashboard do Supabase, abra **SQL Editor** → **New query**.
2. Copie o conteúdo de [`migration.sql`](migration.sql) e cole.
3. Clique em **Run**.
4. Verifique que a tabela aparece em **Table Editor** → `news_articles`.

Opção B — via repo SectorData (se você usa `supabase/migrations/`):

1. Copie `migration.sql` para `supabase/migrations/YYYYMMDDHHMMSS_news_hunter.sql` (use um timestamp posterior aos existentes).
2. Faça push. O GitHub Action `supabase-deploy.yml` aplica automaticamente.

---

## Passo 2 — Configurar o scanner local (Clipinator)

No diretório `clipinator/`:

### 2.1 Instale as dependências novas

```
pip install -r requirements.txt
```

Adicionamos `supabase` e `python-dotenv`.

### 2.2 Crie o `.env`

```
copy .env.example .env
```

Abra `.env` e preencha:
```
SUPABASE_URL=https://SEU-PROJETO.supabase.co
SUPABASE_SERVICE_KEY=eyJhbGciOi... (service_role, NÃO a anon key)
```

### 2.3 Teste

```
uvicorn newshunter.app:app --port 8000
```

- No log inicial deve aparecer `Supabase habilitado (target: https://...)`.
- Rode uma busca (dashboard local ou espere o auto-refresh).
- Abra o Supabase **Table Editor** → `news_articles`: devem aparecer rows.

Se aparecer `Supabase desabilitado (SUPABASE_URL/SUPABASE_SERVICE_KEY ausentes)` no log, o `.env` não foi lido — confira que o arquivo está no diretório raiz do clipinator (`clipinator/.env`) e as variáveis estão com o nome exato.

> **Importante:** o scanner continua funcionando mesmo sem Supabase. Se o `.env` estiver vazio, o dashboard local segue 100% funcional — só o espelhamento online fica off.

---

## Passo 3 — Adicionar a página no SectorData

No repo **IBBAOG/SectorData**:

### 3.1 Crie a rota

Copie [`page.tsx`](page.tsx) para:
```
src/app/(dashboard)/news-hunter/page.tsx
```

**Ajuste o import do Supabase** (linha ~14):
```typescript
import { createClient } from "@/lib/supabase";
```
Troque pelo helper que o SectorData já usa (veja `src/lib/supabase.ts` ou similar). O padrão do projeto já tem isso configurado com a anon key.

### 3.2 Adicione o link na home

Edite a home (provavelmente `src/app/(dashboard)/home/page.tsx`) e adicione um card para o novo módulo. Use [`home-module-card.snippet.tsx`](home-module-card.snippet.tsx) como referência — adapte ao componente `<ModuleCard>` ou padrão Bootstrap que o SectorData já usa.

### 3.3 (Se aplicável) Registre em `module_visibility`

Se o SectorData gateia módulos via a tabela `module_visibility` (verifique `src/lib/rpc.ts` ou o `CLAUDE.md` do SectorData), insira:
```sql
insert into module_visibility (module_key, visible_to_all) values ('news_hunter', true);
```

### 3.4 Deploy

`git push` para `main` → Vercel auto-deploya.

---

## Passo 4 — Validação end-to-end

1. Abra `https://SEU-PROJETO.vercel.app/news-hunter` (ou domínio customizado).
2. Se o scanner local estiver rodando, as últimas notícias devem aparecer.
3. Abra o DevTools (F12) → **Network** → filtre por `rest/v1`: requisições a cada 30s.
4. Force um scan local (botão "Buscar período maior") e confirme que artigos novos aparecem no Vercel em até 30s.
5. Desligue o scanner local por 5 min — o Vercel continua mostrando cache (não quebra).

---

## Limites dos providers — status

### Supabase Free (5 GB egress/mês, 500 MB DB)

| Origem                     | Volume estimado        | No budget? |
|----------------------------|------------------------|------------|
| Scanner → Supabase (push)  | ~30 artigos/dia × 1 KB | ✅ ~1 MB/mês |
| Frontend → Supabase (poll) | Incremental + dedup    | ✅ ~100 MB/mês por usuário ativo |
| Tabela `news_articles`     | ~1 KB/row × 30/dia     | ✅ <5 MB/ano |

Com até **~20 usuários concorrentes com aba aberta 8h/dia**, cabe confortavelmente no free tier.

### Vercel Hobby (100 GB bandwidth/mês)

A página Next.js é estática (só JS/CSS no primeiro load). O polling vai **direto** para Supabase, não passa pela Vercel. Consumo desprezível.

### Se o uso crescer

- **>30 usuários concorrentes:** troque polling por [Supabase Realtime](https://supabase.com/docs/guides/realtime) (até 200 conexões concorrentes no free).
- **DB > 400 MB:** habilite retenção automática via `pg_cron` (disponível em Pro+) — já deixei comentado em `migration.sql`.
- **Paywalls quebrando no scanner:** você vai precisar revisitar `python login.py` para re-exportar cookies, como faz hoje.

---

## Operacional: deixar o scanner rodando o dia todo

Como o scanner precisa estar ativo para alimentar o Supabase, recomendo criar uma tarefa no **Agendador de Tarefas** do Windows:

1. `Win + R` → `taskschd.msc`.
2. **Criar Tarefa** → Nome: `Clipinator News Hunter`.
3. **Gatilhos:** "Ao fazer logon".
4. **Ações:** Iniciar programa:
   - Programa: caminho completo do `python.exe` do seu ambiente (ou `cmd.exe`)
   - Argumentos: `/c uvicorn newshunter.app:app --port 8000`
   - Iniciar em: `C:\Users\eduar\Documents\clipinator`
5. Marque "Executar em segundo plano".

Alternativa menos invasiva: criar um atalho `.bat` com `uvicorn newshunter.app:app --port 8000` e colocar na pasta **Inicializar** (`shell:startup`).

---

## Troubleshooting

- **"relation news_articles does not exist"** — a migration não rodou. Volte ao Passo 1.2.
- **Vercel carrega mas cards ficam vazios** — RLS bloqueando. Conferir policy `authenticated read news_articles` em `Authentication → Policies`.
- **Scanner não está mandando rows** — confira o log (`Supabase habilitado ...`). Se está habilitado mas nada aparece, pode ser que não esteja achando artigos novos (apenas artigos *novos* são enviados; updates de snippet/título não re-pusham para economizar banda).
- **Erro `SUPABASE_SERVICE_KEY ausente` sem razão** — o `.env` deve ficar em `clipinator/.env` (raiz), não dentro de `newshunter/`.
