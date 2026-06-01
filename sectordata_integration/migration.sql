-- ===========================================================================
-- News Hunter — tabela + policies para integrar o Clipinator ao SectorData.
--
-- Como aplicar:
--   Opcao A (recomendada): colar esse SQL no Supabase SQL Editor e rodar.
--   Opcao B: salvar como `supabase/migrations/<timestamp>_news_hunter.sql`
--            no repo SectorData — o workflow de CI (supabase-deploy.yml) roda
--            automaticamente no push para main.
--
-- Impacto em custos (Supabase free = 500 MB DB / 5 GB egress/mes):
--   - Tabela cresce ~30 artigos/dia em regime (~1 KB/row): < 1 MB/ano.
--   - Egress dominado pelo polling do frontend — para 30s, o page.tsx usa
--     fetch incremental (so rows com found_at > ultimo poll), mantendo
--     chamadas vazias em ~500B cada.
-- ===========================================================================

create table if not exists public.news_articles (
  url              text primary key,
  domain           text not null,
  source_name      text not null,
  title            text not null,
  snippet          text not null default '',
  published_at     timestamptz not null,
  found_at         timestamptz not null default now(),
  matched_keywords text[] not null default '{}'::text[],
  created_at       timestamptz not null default now()
);

-- Indices para os dois padroes de query do frontend:
--   1. Listagem ordenada por publicacao ("ultimas N notas das ultimas 24h")
--   2. Polling incremental ("rows novas desde T")
create index if not exists news_articles_published_at_idx
  on public.news_articles (published_at desc);

create index if not exists news_articles_found_at_idx
  on public.news_articles (found_at desc);

-- ===========================================================================
-- Row-Level Security
--
-- - Leitura: usuarios autenticados no SectorData (ja logaram no dashboard).
-- - Escrita: APENAS service_role (o scanner local tem essa chave no .env;
--            service_role bypassa RLS automaticamente — nao precisa policy).
-- Ninguem com anon key ou authenticated consegue INSERT/UPDATE/DELETE.
-- ===========================================================================

alter table public.news_articles enable row level security;

-- Policy idempotente: drop-and-recreate para reaplicar a migration com
-- seguranca se voce rodar esse script duas vezes.
drop policy if exists "authenticated read news_articles" on public.news_articles;
create policy "authenticated read news_articles"
  on public.news_articles
  for select
  to authenticated
  using (true);

-- (Opcional) Retencao automatica — apaga rows com published_at > 180 dias.
-- Descomente se tiver pg_cron habilitado (disponivel no tier Pro+).
-- create extension if not exists pg_cron;
-- select cron.schedule(
--   'news_articles_retention',
--   '0 3 * * *',
--   $$ delete from public.news_articles where published_at < now() - interval '180 days' $$
-- );

-- Verificacao rapida:
--   select count(*) from public.news_articles;                          -- deve ser 0
--   select * from public.news_articles order by published_at desc lim 5; -- depois do 1o push
