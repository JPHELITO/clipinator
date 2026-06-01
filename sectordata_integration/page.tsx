"use client";

/**
 * News Hunter — modulo do SectorData que espelha o dashboard local do
 * Clipinator. Puxa artigos do Supabase (tabela `news_articles`), atualiza a
 * cada 30s via polling incremental e suporta filtro client-side + janela
 * de tempo selecionavel.
 *
 * Onde colocar: `src/app/(dashboard)/news-hunter/page.tsx`
 *
 * Custo de bandwidth (Supabase free = 5 GB/mes):
 *   - 1o fetch: 50 rows x ~1 KB = ~50 KB
 *   - Polls subsequentes: so rows novas desde o ultimo poll (ordem de 0-5
 *     rows na maioria das vezes, ~500 B em scans vazios).
 *   - Ate ~20 usuarios concorrentes cabem confortavelmente no free tier.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
// NOTA: ajuste esse import para o helper ja existente no SectorData (ex.:
// `@/lib/supabase` ou `@/lib/supabaseClient`). Esse arquivo assume que ha
// um `createClient` autenticado exportado no projeto.
import { createClient } from "@/lib/supabase";

type NewsArticle = {
  url: string;
  domain: string;
  source_name: string;
  title: string;
  snippet: string;
  published_at: string; // ISO timestamptz
  found_at: string;     // ISO timestamptz
  matched_keywords: string[];
};

const WINDOW_PRESETS = [1, 3, 6, 12, 24, 48, 72, 168] as const;
const POLL_INTERVAL_MS = 30_000;
const PAGE_LIMIT = 500;

function labelForWindow(h: number): string {
  if (h < 24) return `${h}h`;
  if (h === 24) return "24h (1d)";
  if (h === 168) return "7d";
  return `${h}h (${Math.floor(h / 24)}d)`;
}

function humanizeAge(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "sem data";
  const secs = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (secs < 60) return "agora";
  if (secs < 3600) return `há ${Math.floor(secs / 60)} min`;
  if (secs < 86400) return `há ${Math.floor(secs / 3600)} h`;
  return `há ${Math.floor(secs / 86400)} d`;
}

function stripAccents(s: string): string {
  return s.normalize("NFD").replace(/[\u0300-\u036f]/g, "");
}

export default function NewsHunterPage() {
  const [articles, setArticles] = useState<NewsArticle[]>([]);
  const [windowHours, setWindowHours] = useState<number>(24);
  const [filter, setFilter] = useState<string>("");
  const [loading, setLoading] = useState<boolean>(true);
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Ref ao inves de state para nao re-disparar o polling a cada fetch.
  const lastFoundAtRef = useRef<string | null>(null);
  const supabase = useMemo(() => createClient(), []);

  // Merge dedup-by-url: itens atualizados substituem, novos ficam no topo.
  const mergeArticles = useCallback(
    (prev: NewsArticle[], incoming: NewsArticle[]): NewsArticle[] => {
      if (incoming.length === 0) return prev;
      const byUrl = new Map(prev.map((a) => [a.url, a]));
      for (const a of incoming) byUrl.set(a.url, a);
      return Array.from(byUrl.values()).sort(
        (a, b) =>
          new Date(b.published_at).getTime() -
          new Date(a.published_at).getTime(),
      );
    },
    [],
  );

  // Fetch completo: re-inicializa estado. Chamado no mount e quando o
  // usuario muda a janela.
  const fetchInitial = useCallback(
    async (hours: number) => {
      setLoading(true);
      setError(null);
      const cutoff = new Date(Date.now() - hours * 3600 * 1000).toISOString();
      const { data, error: err } = await supabase
        .from("news_articles")
        .select("*")
        .gte("published_at", cutoff)
        .order("published_at", { ascending: false })
        .limit(PAGE_LIMIT);
      if (err) {
        setError(err.message);
        setLoading(false);
        return;
      }
      const rows = (data as NewsArticle[]) ?? [];
      setArticles(rows);
      setLastUpdate(new Date());
      // Guarda o maior found_at para o proximo poll incremental.
      lastFoundAtRef.current =
        rows.length > 0
          ? rows.reduce(
              (max, r) => (r.found_at > max ? r.found_at : max),
              rows[0].found_at,
            )
          : new Date().toISOString();
      setLoading(false);
    },
    [supabase],
  );

  // Fetch incremental: so rows com found_at > ultimo poll. Payload minimo.
  const fetchIncremental = useCallback(async () => {
    if (!lastFoundAtRef.current) return;
    const { data, error: err } = await supabase
      .from("news_articles")
      .select("*")
      .gt("found_at", lastFoundAtRef.current)
      .order("found_at", { ascending: false })
      .limit(PAGE_LIMIT);
    if (err) {
      setError(err.message);
      return;
    }
    const rows = (data as NewsArticle[]) ?? [];
    setLastUpdate(new Date());
    setError(null);
    if (rows.length === 0) return;
    lastFoundAtRef.current = rows.reduce(
      (max, r) => (r.found_at > max ? r.found_at : max),
      lastFoundAtRef.current!,
    );
    setArticles((prev) => {
      const merged = mergeArticles(prev, rows);
      // Poda rows que sairam da janela atual (evita crescimento indefinido).
      const cutoff = Date.now() - windowHours * 3600 * 1000;
      return merged.filter(
        (a) => new Date(a.published_at).getTime() >= cutoff,
      );
    });
  }, [supabase, mergeArticles, windowHours]);

  // Setup inicial + polling.
  useEffect(() => {
    void fetchInitial(windowHours);
  }, [fetchInitial, windowHours]);

  useEffect(() => {
    const id = setInterval(() => {
      void fetchIncremental();
    }, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [fetchIncremental]);

  // Filtro client-side (titulo/fonte/snippet, accent-insensitive).
  const filtered = useMemo(() => {
    const cutoff = Date.now() - windowHours * 3600 * 1000;
    const inWindow = articles.filter(
      (a) => new Date(a.published_at).getTime() >= cutoff,
    );
    const raw = filter.trim();
    if (!raw) return inWindow;
    const terms = stripAccents(raw.toLowerCase())
      .split(/\s+/)
      .filter(Boolean);
    return inWindow.filter((a) => {
      const hay = stripAccents(
        `${a.title} ${a.source_name} ${a.snippet} ${a.matched_keywords.join(" ")}`.toLowerCase(),
      );
      return terms.every((t) => hay.includes(t));
    });
  }, [articles, filter, windowHours]);

  return (
    <div className="container-fluid py-4">
      <div className="d-flex flex-wrap align-items-center gap-3 mb-4">
        <h1 className="h3 mb-0">News Hunter</h1>
        <span className="text-muted small">
          {loading
            ? "⏳ carregando…"
            : lastUpdate
              ? `atualizado ${humanizeAge(lastUpdate.toISOString())}`
              : ""}
        </span>
        <span className="text-muted small ms-auto">auto-refresh: 30s</span>
      </div>

      <div className="row g-3 mb-4">
        <div className="col-md-6">
          <input
            type="search"
            className="form-control"
            placeholder="🔎 Filtrar (título, fonte, snippet)…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            aria-label="Filtrar notícias"
          />
        </div>
        <div className="col-md-3">
          <select
            className="form-select"
            value={windowHours}
            onChange={(e) => setWindowHours(Number(e.target.value))}
            aria-label="Janela de tempo"
          >
            {WINDOW_PRESETS.map((h) => (
              <option key={h} value={h}>
                Janela: {labelForWindow(h)}
              </option>
            ))}
          </select>
        </div>
        <div className="col-md-3 text-muted small d-flex align-items-center">
          {filtered.length} notícia{filtered.length === 1 ? "" : "s"}
          {filter && ` (de ${articles.length})`}
        </div>
      </div>

      {error && (
        <div className="alert alert-warning" role="alert">
          Erro ao carregar: {error}
        </div>
      )}

      {!loading && filtered.length === 0 && (
        <div className="alert alert-light text-center">
          {articles.length === 0
            ? "Nenhuma notícia ainda. Verifique se o scanner local está rodando."
            : "Nenhuma notícia corresponde ao filtro."}
        </div>
      )}

      <div className="row g-3">
        {filtered.map((a) => (
          <div key={a.url} className="col-md-6 col-lg-4">
            <div className="card h-100 shadow-sm">
              <div className="card-body">
                <div className="d-flex justify-content-between align-items-start mb-2">
                  <span className="badge bg-secondary">{a.source_name}</span>
                  <small className="text-muted">
                    {humanizeAge(a.published_at)}
                  </small>
                </div>
                <h5 className="card-title">
                  <a
                    href={a.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-decoration-none"
                  >
                    {a.title}
                  </a>
                </h5>
                {a.snippet && (
                  <p className="card-text small text-muted">{a.snippet}</p>
                )}
                <div className="mt-2">
                  {a.matched_keywords.map((kw) => (
                    <span
                      key={kw}
                      className="badge bg-primary-subtle text-primary-emphasis me-1 mb-1"
                    >
                      {kw}
                    </span>
                  ))}
                </div>
              </div>
              <div className="card-footer bg-transparent">
                <a
                  href={a.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="small"
                >
                  🔗 abrir matéria
                </a>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
