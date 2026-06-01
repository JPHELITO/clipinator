"""SQLite para cache de artigos, config e historico de runs."""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse, unquote, quote

from .config import DEFAULT_KEYWORDS, DEFAULT_WINDOW_HOURS, SOURCE_KEYWORDS

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "newshunter.db"


TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src",
    "__twitter_impression",
}


_FM_ARTICLE_RE = re.compile(r"^(/a/RA\d+)(/.*)?$", re.IGNORECASE)


def normalize_url(url: str) -> str:
    """Remove fragment, tracking params e 'www.' do host para dedupe estável.

    Exceções:
    - Platts SPA (core.spglobal.com): fragment preservado (é a rota real).
    - Fastmarkets (dashboard.fastmarkets.com): slug após o RA-ID descartado.
      /a/RA250305/german-printer-... → /a/RA250305 (ID é o identificador único).
    """
    try:
        p = urlparse(url)
    except ValueError:
        return url
    netloc = p.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]

    # Platts SPA: fragment contém a rota real do artigo — preservar.
    keep_fragment = netloc in ("core.spglobal.com",) and p.fragment.startswith("platts/")
    if keep_fragment:
        # Normaliza encoding: %20, espaços literais e + são todos equivalentes
        # nos params do fragment. Decodifica tudo e re-encoda de forma canônica
        # (somente caracteres obrigatórios), evitando mismatch de chave no DB.
        fragment = quote(unquote(p.fragment), safe="/:?=&#@!$'()*+,;")
    else:
        fragment = ""

    path = p.path.rstrip("/") or p.path

    # Fastmarkets: /a/RA250305/slug-decorativo → /a/RA250305
    if netloc == "dashboard.fastmarkets.com":
        m = _FM_ARTICLE_RE.match(path)
        if m:
            path = m.group(1)

    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
    return urlunparse((p.scheme, netloc, path, p.params, urlencode(query), fragment))


@dataclass
class Article:
    url: str
    domain: str
    source_name: str
    title: str
    snippet: str
    published_at: datetime | None
    found_at: datetime
    matched_keywords: list[str]
    body: str = ""  # corpo completo do artigo (Platts API, etc.)

    @property
    def published_iso(self) -> str | None:
        return self.published_at.isoformat() if self.published_at else None


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL;")
    c.execute("PRAGMA synchronous=NORMAL;")
    c.execute("PRAGMA foreign_keys=ON;")
    try:
        yield c
    finally:
        c.close()


def init_db() -> None:
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS articles (
                url TEXT PRIMARY KEY,
                domain TEXT NOT NULL,
                source_name TEXT NOT NULL,
                title TEXT NOT NULL,
                snippet TEXT NOT NULL,
                published_at TEXT,
                found_at TEXT NOT NULL,
                matched_keywords TEXT NOT NULL,
                body TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at);
            CREATE INDEX IF NOT EXISTS idx_articles_domain ON articles(domain);

            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                n_found INTEGER DEFAULT 0,
                errors TEXT DEFAULT '[]'
            );
            """
        )
        # Tabelas de filtros por fonte
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS source_keywords (
                domain TEXT NOT NULL,
                keyword TEXT NOT NULL,
                PRIMARY KEY (domain, keyword)
            );
            CREATE TABLE IF NOT EXISTS source_modes (
                domain TEXT PRIMARY KEY,
                mode TEXT NOT NULL DEFAULT 'global'
            );
            """
        )
        # Tabela de publicações de research (Itaú BBA Smart portal).
        # Sempre contém as 10 mais recentes — substituída a cada scrape.
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS publications (
                rank        INTEGER PRIMARY KEY,   -- 1 = mais recente
                title       TEXT NOT NULL,
                pdf_url     TEXT NOT NULL,
                report_url  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
            """
        )

        # Migração: adiciona coluna body se não existir (DBs antigos)
        try:
            c.execute("ALTER TABLE articles ADD COLUMN body TEXT DEFAULT ''")
        except Exception:
            pass  # coluna já existe

        # Migração: domínios em mode='specific' sem nenhuma keyword definida pelo
        # usuário são promovidos para mode='all' se SOURCE_KEYWORDS os define como ["*"].
        # Isso corrige sementes antigas que ficaram em 'specific' com lista vazia.
        try:
            all_domains_rows = c.execute(
                "SELECT domain FROM source_modes WHERE mode='specific'"
            ).fetchall()
            for row in all_domains_rows:
                domain = row["domain"]
                kw_count = c.execute(
                    "SELECT COUNT(*) FROM source_keywords WHERE domain=?", (domain,)
                ).fetchone()[0]
                if kw_count == 0 and SOURCE_KEYWORDS.get(domain) == ["*"]:
                    c.execute(
                        "UPDATE source_modes SET mode='all' WHERE domain=?", (domain,)
                    )
        except Exception:
            pass  # migração opcional

        # Semeia config default no primeiro run
        c.execute("SELECT COUNT(*) AS n FROM config")
        if c.execute("SELECT COUNT(*) AS n FROM config").fetchone()["n"] == 0:
            c.execute(
                "INSERT INTO config(key, value) VALUES (?, ?)",
                ("keywords", json.dumps(DEFAULT_KEYWORDS, ensure_ascii=False)),
            )
            c.execute(
                "INSERT INTO config(key, value) VALUES (?, ?)",
                ("window_hours", json.dumps(DEFAULT_WINDOW_HOURS)),
            )


def get_config() -> dict:
    with _conn() as c:
        rows = c.execute("SELECT key, value FROM config").fetchall()
    out: dict = {r["key"]: json.loads(r["value"]) for r in rows}
    out.setdefault("keywords", list(DEFAULT_KEYWORDS))
    out.setdefault("window_hours", DEFAULT_WINDOW_HOURS)
    return out


def set_config(key: str, value) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO config(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )


# ---------------------------------------------------------------------------
# Alertas de sessão (login expirado)
# ---------------------------------------------------------------------------

_ALERT_PREFIX = "session_alert_"


def set_session_alert(source: str, message: str) -> None:
    """Registra alerta de sessão expirada para uma fonte (ex: 'valor', 'platts')."""
    set_config(f"{_ALERT_PREFIX}{source}", {
        "message": message,
        "ts": datetime.now(timezone.utc).isoformat(),
    })


def clear_session_alert(source: str) -> None:
    """Remove alerta de sessão (após login renovado)."""
    with _conn() as c:
        c.execute("DELETE FROM config WHERE key = ?", (f"{_ALERT_PREFIX}{source}",))


def get_session_alerts() -> list[dict]:
    """Retorna lista de alertas ativos: [{source, message, ts}, ...]."""
    with _conn() as c:
        rows = c.execute(
            "SELECT key, value FROM config WHERE key LIKE ?",
            (f"{_ALERT_PREFIX}%",),
        ).fetchall()
    alerts = []
    for r in rows:
        source = r["key"][len(_ALERT_PREFIX):]
        try:
            data = json.loads(r["value"])
            alerts.append({"source": source, **data})
        except Exception:
            pass
    return alerts


def add_keyword(kw: str) -> list[str]:
    kw = kw.strip()
    if not kw:
        return get_config()["keywords"]
    cfg = get_config()
    kws = cfg["keywords"]
    if kw not in kws:
        kws.append(kw)
        set_config("keywords", kws)
    return kws


def remove_keyword(kw: str) -> list[str]:
    cfg = get_config()
    kws = [k for k in cfg["keywords"] if k != kw]
    set_config("keywords", kws)
    return kws


def upsert_article(a: Article) -> bool:
    """Insere ou atualiza. Retorna True se era novo."""
    with _conn() as c:
        return _upsert_article_conn(c, a)


def _upsert_article_conn(c: sqlite3.Connection, a: Article) -> bool:
    # Normaliza sempre antes de tocar o banco — garante que URLs com slug
    # (e.g. /a/RA250303/titulo-do-artigo) e sem slug (/a/RA250303) apontem
    # para a mesma linha, evitando duplicatas no Fastmarkets e em outros
    # scrapers que atualizam a URL canônica na Fase 2.
    url = normalize_url(a.url)
    existed = c.execute(
        "SELECT 1 FROM articles WHERE url = ?", (url,)
    ).fetchone() is not None
    c.execute(
        "INSERT INTO articles(url, domain, source_name, title, snippet, "
        "published_at, found_at, matched_keywords, body) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(url) DO UPDATE SET "
        "title=excluded.title, snippet=excluded.snippet, "
        "published_at=COALESCE(excluded.published_at, articles.published_at), "
        "matched_keywords=excluded.matched_keywords, "
        "body=CASE WHEN LENGTH(COALESCE(excluded.body,'')) > LENGTH(COALESCE(articles.body,'')) "
        "     THEN excluded.body ELSE articles.body END",
        (
            url, a.domain, a.source_name, a.title, a.snippet,
            a.published_iso, a.found_at.isoformat(),
            json.dumps(a.matched_keywords, ensure_ascii=False),
            a.body,
        ),
    )
    return not existed


# Pattern de duplicacao WordPress: editor republica post, CMS resolve conflito
# de slug appendando -N. Resultado: /slug/ e /slug-2/ no sitemap, conteudo
# identico. O fetcher dedupla ao ler STANDARD_SITEMAPS; esta checagem aqui e
# segunda camada (qualquer coletor futuro que traga esse padrao fica coberto).
_WP_DUP_SUFFIX_RE = re.compile(r"-\d{1,2}$")


def _wp_base_url(url: str) -> str | None:
    """Retorna a URL base se `url` parece ser variante /slug-N/ de um slug-artigo.

    Guarda: slug base precisa ter 3+ hifens (padrao de titulo-longo), para nao
    confundir URLs tipo /xiaomi-13 com duplicata de /xiaomi.
    """
    m = _WP_DUP_SUFFIX_RE.search(url)
    if not m:
        return None
    base_url = url[: m.start()]
    try:
        base_seg = urlparse(base_url).path.rstrip("/").rsplit("/", 1)[-1]
    except ValueError:
        return None
    if base_seg.count("-") < 3:
        return None
    return base_url


def _filter_wp_duplicates(c: sqlite3.Connection, articles: list[Article]) -> list[Article]:
    """Remove do batch articles cuja url e /slug-N/ se a url base /slug/ ja
    esta sendo persistida no mesmo batch ou ja existe no DB."""
    batch_urls = {a.url for a in articles}
    filtered: list[Article] = []
    for a in articles:
        base = _wp_base_url(a.url)
        if base is not None:
            if base in batch_urls:
                continue
            hit = c.execute("SELECT 1 FROM articles WHERE url = ?", (base,)).fetchone()
            if hit is not None:
                continue
        filtered.append(a)
    return filtered


def upsert_articles(articles: list[Article]) -> int:
    """Insere ou atualiza em lote. Retorna numero de novos.

    Uma unica conexao + transacao explicita para amortizar o custo de
    abertura do SQLite (~10-80ms por conexao em WAL).

    Apos commit local, replica os artigos NOVOS (first-time insert) para o
    Supabase se configurado. Updates de snippet/titulo em rows ja existentes
    NAO sao re-enviados — economia de bandwidth em scans de 30s.
    """
    if not articles:
        return 0
    new_articles: list[Article] = []
    with _conn() as c:
        c.execute("BEGIN")
        try:
            articles = _filter_wp_duplicates(c, articles)
            for a in articles:
                if _upsert_article_conn(c, a):
                    new_articles.append(a)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    if new_articles:
        # Import lazy: se o pacote supabase nao estiver instalado, o modulo
        # carrega mas get_sink() vira no-op. Scanner local nao quebra.
        try:
            from . import supabase_sync
            supabase_sync.push_new(new_articles)
        except Exception:  # noqa: BLE001
            # Blindagem adicional — qualquer falha aqui e silenciosa.
            pass

    return len(new_articles)


def get_cached_snippets(urls: list[str]) -> dict[str, tuple[str, datetime | None, str, str]]:
    """Lookup em lote de snippets ja extraidos.

    Retorna url_normalizada -> (snippet, published_at, domain, title). Usado como
    cache: URLs ja enriquecidas em runs anteriores nao precisam de novo
    fetch_html, que e o maior custo do pipeline.
    """
    if not urls:
        return {}
    out: dict[str, tuple[str, datetime | None, str, str]] = {}
    with _conn() as c:
        # Chunking por 500 para evitar estouro de limite SQL.
        for i in range(0, len(urls), 500):
            chunk = urls[i : i + 500]
            placeholders = ",".join("?" * len(chunk))
            rows = c.execute(
                f"SELECT url, snippet, published_at, domain, title FROM articles WHERE url IN ({placeholders})",
                chunk,
            ).fetchall()
            for r in rows:
                pub = r["published_at"]
                out[r["url"]] = (
                    r["snippet"],
                    datetime.fromisoformat(pub) if pub else None,
                    r["domain"],
                    r["title"] or "",
                )
    return out


def list_articles(window_hours: int, limit: int = 500) -> list[Article]:
    """Retorna artigos cuja published_at esta dentro da janela.

    Artigos sem data (published_at NULL) sao incluidos se found_at esta na janela
    (raro mas pode acontecer com Google News quando o parser falha).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM articles "
            "WHERE (published_at IS NOT NULL AND published_at >= ?) "
            "   OR (published_at IS NULL AND found_at >= ?) "
            "ORDER BY COALESCE(published_at, found_at) DESC LIMIT ?",
            (cutoff, cutoff, limit),
        ).fetchall()
    return [_row_to_article(r) for r in rows]


def cleanup_old_articles(max_age_hours: int = 48) -> int:
    """Remove artigos mais antigos que max_age_hours. Retorna quantidade removida."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    with _conn() as c:
        c.execute(
            "DELETE FROM articles WHERE found_at < ?",
            (cutoff,),
        )
        deleted = c.execute("SELECT changes()").fetchone()[0]
    return deleted


def _row_to_article(r: sqlite3.Row) -> Article:
    pub = r["published_at"]
    keys = r.keys()
    return Article(
        url=r["url"],
        domain=r["domain"],
        source_name=r["source_name"],
        title=r["title"],
        snippet=r["snippet"],
        published_at=datetime.fromisoformat(pub) if pub else None,
        found_at=datetime.fromisoformat(r["found_at"]),
        matched_keywords=json.loads(r["matched_keywords"]),
        body=r["body"] if "body" in keys else "",
    )


# ---------------------------------------------------------------------------
# Filtros por fonte (source_keywords)
# ---------------------------------------------------------------------------

def seed_source_keywords(defaults: dict[str, list[str]] | None = None) -> None:
    """Semeia source_keywords e source_modes com os defaults do config.py.

    Usa INSERT OR IGNORE em todos os registros: domínios já configurados
    pelo usuário não são sobrescritos. Domínios novos adicionados ao
    SOURCE_KEYWORDS são inseridos automaticamente no próximo reinício.
    """
    if defaults is None:
        defaults = SOURCE_KEYWORDS
    with _conn() as c:
        c.execute("BEGIN")
        try:
            for domain, kws in defaults.items():
                mode = "all" if kws == ["*"] else "specific"
                c.execute(
                    "INSERT OR IGNORE INTO source_modes(domain, mode) VALUES (?, ?)",
                    (domain, mode),
                )
                for kw in kws:
                    if kw != "*":
                        c.execute(
                            "INSERT OR IGNORE INTO source_keywords(domain, keyword) VALUES (?, ?)",
                            (domain, kw),
                        )
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def get_all_source_keywords() -> dict[str, list[str]]:
    """Retorna dict domain → lista de keywords.
    Domínios em modo 'all' retornam ['*'].
    Domínios em modo 'global' ou 'specific' sem keywords não aparecem no dict
    (o pipeline trata ausência como "usar keywords globais" para global,
    e lista vazia como "sem filtro adicional" para specific).
    Esta função é usada pelo pipeline — ver também get_all_source_modes().
    """
    with _conn() as c:
        mode_rows = c.execute("SELECT domain, mode FROM source_modes").fetchall()
        kw_rows = c.execute(
            "SELECT domain, keyword FROM source_keywords ORDER BY domain, keyword"
        ).fetchall()

    kw_map: dict[str, list[str]] = {}
    for r in kw_rows:
        kw_map.setdefault(r["domain"], []).append(r["keyword"])

    result: dict[str, list[str]] = {}
    for r in mode_rows:
        domain, mode = r["domain"], r["mode"]
        if mode == "all":
            result[domain] = ["*"]
        elif mode == "specific":
            result[domain] = kw_map.get(domain, [])
        # mode='global': não adiciona ao result → pipeline usa keywords globais
    return result


def get_all_source_modes() -> dict[str, str]:
    """Retorna dict domain → mode ('all' | 'specific' | 'global')."""
    with _conn() as c:
        rows = c.execute("SELECT domain, mode FROM source_modes").fetchall()
    return {r["domain"]: r["mode"] for r in rows}


def set_source_mode(domain: str, mode: str) -> None:
    """Persiste o modo de filtragem de uma fonte.

    mode='all'      → aceita todos os artigos da fonte
    mode='global'   → usa a lista global de keywords
    mode='specific' → usa as keywords específicas da fonte (lista pode ser vazia)
    """
    if mode not in ("all", "global", "specific"):
        return
    with _conn() as c:
        c.execute(
            "INSERT INTO source_modes(domain, mode) VALUES (?, ?) "
            "ON CONFLICT(domain) DO UPDATE SET mode=excluded.mode",
            (domain, mode),
        )


def add_source_keyword(domain: str, keyword: str) -> list[str]:
    """Adiciona uma keyword à lista da fonte. Muda o modo para 'specific' automaticamente."""
    keyword = keyword.strip()
    if not keyword:
        return get_source_keywords(domain)
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO source_keywords(domain, keyword) VALUES (?, ?)",
            (domain, keyword),
        )
        # Garante que o modo seja 'specific' ao adicionar keywords
        c.execute(
            "INSERT INTO source_modes(domain, mode) VALUES (?, 'specific') "
            "ON CONFLICT(domain) DO UPDATE SET mode='specific'",
            (domain,),
        )
    return get_source_keywords(domain)


def remove_source_keyword(domain: str, keyword: str) -> list[str]:
    """Remove uma keyword da lista da fonte."""
    with _conn() as c:
        c.execute(
            "DELETE FROM source_keywords WHERE domain = ? AND keyword = ?",
            (domain, keyword),
        )
    return get_source_keywords(domain)


def get_source_keywords(domain: str) -> list[str]:
    """Retorna keywords de uma fonte específica (lista vazia = modo global)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT keyword FROM source_keywords WHERE domain = ? ORDER BY keyword",
            (domain,),
        ).fetchall()
    return [r["keyword"] for r in rows]


def get_article(url: str) -> "Article | None":
    """Retorna artigo completo por URL (normalizada)."""
    norm = normalize_url(url)
    with _conn() as c:
        row = c.execute("SELECT * FROM articles WHERE url = ?", (norm,)).fetchone()
    return _row_to_article(row) if row else None


def get_article_body(url: str) -> str:
    """Retorna corpo completo armazenado para uma URL (Platts, etc.)."""
    norm = normalize_url(url)
    with _conn() as c:
        row = c.execute("SELECT body FROM articles WHERE url = ?", (norm,)).fetchone()
    return row["body"] if row and row["body"] else ""


def save_article_body(url: str, body: str, *, force: bool = False) -> None:
    """Persiste o corpo scrapeado para cache.

    Por padrão, só grava se o novo corpo for mais longo que o existente —
    evita sobrescrever um bom Phase-2 body com resultado menor do leitor ao vivo.

    force=True: sobrescreve sempre, independente do tamanho. Usado para scrapers
    que limpam o corpo (ex.: Estadão cleanClone JS), onde o resultado correto
    pode ser mais curto que o corpo sujo armazenado anteriormente.
    """
    if not body or len(body) < 100:
        return
    norm = normalize_url(url)
    with _conn() as c:
        if force:
            c.execute(
                "UPDATE articles SET body = ? WHERE url = ?",
                (body, norm),
            )
        else:
            c.execute(
                "UPDATE articles SET body = ? "
                "WHERE url = ? AND length(COALESCE(body, '')) < ?",
                (body, norm, len(body)),
            )


def save_publications(pubs: list[dict]) -> None:
    """Substitui as publicações armazenadas pelas novas (rank 1 = mais recente).

    Cada item de ``pubs`` deve ter:
        title (str), pdf_url (str), report_url (str)

    A tabela é limpa e reescrita a cada chamada — mantém sempre as mais recentes.
    """
    if not pubs:
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.execute("DELETE FROM publications")
        c.executemany(
            """
            INSERT INTO publications (rank, title, pdf_url, report_url, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (i + 1, p["title"], p["pdf_url"], p["report_url"], now_iso)
                for i, p in enumerate(pubs)
            ],
        )


def get_recent_publications(limit: int = 10) -> list[dict]:
    """Retorna as publicações mais recentes armazenadas, ordenadas por rank."""
    with _conn() as c:
        rows = c.execute(
            "SELECT rank, title, pdf_url, report_url, updated_at "
            "FROM publications ORDER BY rank LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def start_run() -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO runs(started_at) VALUES (?)",
            (datetime.now(timezone.utc).isoformat(),),
        )
        return cur.lastrowid or 0


def finish_run(run_id: int, n_found: int, errors: list[str]) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE runs SET finished_at=?, n_found=?, errors=? WHERE id=?",
            (
                datetime.now(timezone.utc).isoformat(),
                n_found,
                json.dumps(errors, ensure_ascii=False),
                run_id,
            ),
        )


def last_run() -> dict | None:
    with _conn() as c:
        r = c.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not r:
        return None
    return {
        "id": r["id"],
        "started_at": datetime.fromisoformat(r["started_at"]),
        "finished_at": datetime.fromisoformat(r["finished_at"]) if r["finished_at"] else None,
        "n_found": r["n_found"],
        "errors": json.loads(r["errors"] or "[]"),
    }
