"""Extração de corpo de artigo em HTML seguro: parágrafos + imagens.

Entrada: HTML bruto de artigo (pode conter scripts, classes Angular, etc.)
Saída:   HTML seguro para inserção direta no leitor — apenas:
           <p>, <strong>, <em>, <br>
           <h3>, <h4>
           <ul>, <ol>, <li>
           <blockquote>
           <img src="https://..." alt="..." class="reader-img">

Todos os textos são html-escaped. Atributos extras (class, id, style, data-*)
são removidos. Imagens com src não-https são descartadas.
"""
from __future__ import annotations

import html as _he
import re


# ---------------------------------------------------------------------------
# Regex para strip de "Related articles" em HTML
# ---------------------------------------------------------------------------
_RELATED_HTML_RE = re.compile(
    r'(?:<(?:p|h[2-6])[^>]*>[^<]*'
    r'(?:Related\s+(?:articles?|news|stories?|content|coverage)|'
    r'More\s+on\s+this\s+topic|More\s+stories?|Also\s+read|See\s+also|'
    r'RELATED\s+(?:NEWS|ARTICLES?))'
    r'[^<]*</(?:p|h[2-6])>)[\s\S]*',
    re.IGNORECASE,
)


def _strip_related_html(s: str) -> str:
    return _RELATED_HTML_RE.sub("", s).rstrip()


# ---------------------------------------------------------------------------
# Função principal
# ---------------------------------------------------------------------------

def innertext_to_html(text: str) -> str:
    """Converte innerText bruto (retornado pelo browser via Playwright) em HTML seguro.

    ``innerText`` já tem os espaços corretos entre palavras (sem merging Angular).

    Estratégia de separação de parágrafos:
    - Se o texto contém linhas duplas (``\\n\\n``): cada bloco vira um ``<p>``,
      e linhas simples DENTRO de um bloco são mescladas com espaço.
    - Se não há ``\\n\\n`` (caso típico do Angular Platts, onde cada parágrafo
      aparece em uma linha separada): cada linha não-vazia vira um ``<p>`` próprio.
    """
    if not text or not text.strip():
        return ""
    # Normaliza quebras de linha: 3+ → 2
    text = re.sub(r"\n{3,}", "\n\n", text.strip())

    parts: list[str] = []

    if "\n\n" in text:
        # Modo padrão: parágrafos separados por linha dupla
        for block in text.split("\n\n"):
            block = block.strip()
            if not block or len(block) < 5:
                continue
            lines = [l.strip() for l in block.splitlines() if l.strip()]
            combined = " ".join(lines)
            if len(combined) > 5:
                parts.append(f"<p>{_he.escape(combined)}</p>")
    else:
        # Modo linha-a-linha: Angular SPAs como Platts não inserem \n\n
        # entre parágrafos — cada linha é um parágrafo separado.
        for line in text.splitlines():
            line = line.strip()
            if len(line) > 5:
                parts.append(f"<p>{_he.escape(line)}</p>")

    result = "\n".join(parts)
    return _strip_related_html(result)


_SENTENCE_SPLIT_RE = re.compile(
    # Quebra de parágrafo: char minúsculo/dígito/fechamento → ponto → inicial maiúscula
    # seguida de pelo menos 2 letras (evita siglas tipo "S.A.", "U.S.").
    # Cobre o bug da API Platts onde frases são coladas sem espaço: "offers.Platts"
    r'(?<=[a-z0-9"\'»)\]%])\.\s*(?=[A-Z][a-z][a-z])',
)


def _split_api_body(text: str) -> str:
    """Insere \\n entre sentenças coladas de corpo de artigo da API Platts.

    O campo BodyText da API retorna parágrafos sem separador — sentenças são
    unidas diretamente: "...mill offers.Platts assessed...". Esta função insere
    \\n nos limites detectados para que ``innertext_to_html`` possa criar
    múltiplos ``<p>`` em vez de um bloco único.
    """
    if not text or "\n" in text:
        # Já tem newlines (Phase 2 DOM innerText) — não modifica.
        return text
    # Insere \n nos limites de sentença detectados.
    return _SENTENCE_SPLIT_RE.sub(".\n", text)


# JS que caminha o DOM de .newsSection-body[0] e retorna items em ordem:
#   {"t": "p"|"h2"|"h3"|"h4", "v": "texto"}   — parágrafo/heading
#   {"t": "img", "idx": N}                     — imagem (N = índice global no body)
# Usado por platts_scraper, playwright_reader e platts_bulk_refresh.
PLATTS_DOM_WALK_JS: str = r"""(function() {
    var body = document.querySelectorAll('.newsSection-body')[0];
    if (!body) return JSON.stringify({items: [], hl: '', url: ''});

    var hlEl = document.querySelector('.newsSection-highlights');
    var hl   = hlEl ? (hlEl.innerText || '').trim() : '';

    /* Angular envolve todo o conteúdo em um DIV filho de .newsSection-body */
    var container = body;
    if (body.children.length >= 1 && body.children[0].tagName === 'DIV') {
        container = body.children[0];
    }

    var items  = [];
    var imgIdx = 0;

    for (var i = 0; i < container.children.length; i++) {
        var el   = container.children[i];
        var tag  = el.tagName;
        var imgs = (tag === 'IMG') ? [el] : Array.prototype.slice.call(el.querySelectorAll('img'));

        if (imgs.length > 0) {
            /* Um slot de imagem por <img> encontrado */
            for (var j = 0; j < imgs.length; j++) {
                items.push({t: 'img', idx: imgIdx++});
            }
            continue;
        }

        var text = (el.innerText || '').trim();
        if (!text || text.length < 2) continue;

        var itemType = /^H[1-6]$/.test(tag) ? tag.toLowerCase() : 'p';
        items.push({t: itemType, v: text});
    }

    return JSON.stringify({items: items, hl: hl, url: window.location.href});
})()"""


def platts_dom_items_to_html(data: dict, page, *, strip_related: bool = True) -> str:  # noqa: ANN001
    """Constrói HTML Platts a partir dos itens retornados por PLATTS_DOM_WALK_JS.

    Para cada item de imagem, faz screenshot via ``page.screenshot(clip=bb)``
    (sem hover — evita overlay de "click to zoom") e insere o PNG na posição
    correta do artigo.  Para textos/headings, escapa e envolve na tag certa.

    Parâmetros
    ----------
    data : dict
        JSON decodificado retornado pelo PLATTS_DOM_WALK_JS.
    page : playwright.sync_api.Page
        Página Playwright já posicionada no artigo.
    strip_related : bool
        Se True (padrão), remove seções "related articles" detectadas por heurística.
    """
    import base64 as _b64

    items   = data.get("items", [])
    hl_text = data.get("hl", "")

    parts: list[str] = []

    # ── Esconde overlays/banners antes de qualquer screenshot ────────────────
    # Cookie banners, GDPR overlays, modais e qualquer elemento de posição
    # fixa/sticky que apareça na frente dos charts durante a captura.
    _HIDE_OVERLAYS_JS = """(function() {
        var selectors = [
            '[class*="cookie"]', '[id*="cookie"]',
            '[class*="consent"]', '[id*="consent"]',
            '[class*="gdpr"]',   '[id*="gdpr"]',
            '[class*="privacy"]','[id*="privacy"]',
            '[class*="overlay"]','[class*="modal"]',
            '[class*="popup"]',  '[class*="banner"]',
            '[class*="notice"]', '[id*="notice"]',
            '[class*="toast"]',
        ];
        selectors.forEach(function(sel) {
            document.querySelectorAll(sel).forEach(function(el) {
                el.style.setProperty('display', 'none', 'important');
                el.style.setProperty('visibility', 'hidden', 'important');
                el.style.setProperty('opacity', '0', 'important');
            });
        });
        /* Remove também elementos fixed/sticky que cobrem a viewport */
        document.querySelectorAll('*').forEach(function(el) {
            var s = window.getComputedStyle(el);
            if ((s.position === 'fixed' || s.position === 'sticky') &&
                !el.closest('.newsSection-body')) {
                el.style.setProperty('display', 'none', 'important');
            }
        });
    })()"""
    try:
        page.evaluate(_HIDE_OVERLAYS_JS)
    except Exception:
        pass

    # ── Highlights → lista de bullets ────────────────────────────────────────
    if hl_text and hl_text.strip():
        hl_lines = [l.strip() for l in hl_text.splitlines() if len(l.strip()) > 5]
        if hl_lines:
            li_items = "".join(f"<li>{_he.escape(l)}</li>" for l in hl_lines)
            parts.append(f"<ul>{li_items}</ul>")

    img_locs = page.locator(".newsSection-body").first.locator("img")

    for item in items:
        t = item.get("t", "p")

        if t == "img":
            idx = item.get("idx", 0)
            try:
                loc = img_locs.nth(idx)
                # Rola o elemento para a viewport antes de capturar
                try:
                    loc.scroll_into_view_if_needed(timeout=3_000)
                except Exception:
                    pass
                # Re-aplica hide de overlays após scroll (alguns reaparecem)
                try:
                    page.evaluate(_HIDE_OVERLAYS_JS)
                except Exception:
                    pass
                # Usa page.screenshot(clip=bb) em vez de element.screenshot()
                # para não acionar hover/focus que mostram overlays Angular
                bb = loc.bounding_box()
                if bb and bb["width"] > 10 and bb["height"] > 10:
                    png = page.screenshot(clip=bb)
                    if png and len(png) > 500:
                        b64 = _b64.b64encode(png).decode()
                        parts.append(
                            f'<img src="data:image/png;base64,{b64}" alt="" class="reader-img">'
                        )
            except Exception:
                pass

        elif t in ("h1", "h2", "h3", "h4", "h5", "h6"):
            v = _he.escape((item.get("v") or "").strip())
            if v:
                parts.append(f"<{t}>{v}</{t}>")

        else:  # "p" ou qualquer outro bloco de texto
            v = _he.escape((item.get("v") or "").strip())
            if v and len(v) > 2:
                parts.append(f"<p>{v}</p>")

    result = "\n".join(parts)
    return _strip_related_html(result) if strip_related else result


def platts_innertext_to_html(hl_text: str, bdy_text: str) -> str:
    """Converte os dois blocos de innerText da Platts em HTML seguro.

    Reproduz a formatação do site original:
    - highlights (hl_text): cada linha → ``<li>`` em uma ``<ul>``
    - body (bdy_text):      cada linha → ``<p>`` separado

    O Angular coloca uma linha por item/parágrafo no ``innerText`` —
    nunca usar ``join('\\n\\n')`` antes de chamar esta função, pois isso
    colapsaria todos os parágrafos num único bloco.
    """
    parts: list[str] = []

    # ── Highlights → lista de bullets ────────────────────────────────────────
    if hl_text and hl_text.strip():
        hl_lines = [l.strip() for l in hl_text.splitlines() if len(l.strip()) > 5]
        if hl_lines:
            li_items = "".join(f"<li>{_he.escape(l)}</li>" for l in hl_lines)
            parts.append(f"<ul>{li_items}</ul>")

    # ── Body → um <p> por parágrafo ───────────────────────────────────────────
    if bdy_text and bdy_text.strip():
        for line in bdy_text.splitlines():
            line = line.strip()
            if len(line) > 5:
                parts.append(f"<p>{_he.escape(line)}</p>")

    result = "\n".join(parts)
    return _strip_related_html(result)


def dom_items_to_html(items_json: str) -> str:
    """Converte JSON retornado pela função JS de DOM walk em HTML seguro.

    O JSON é uma lista de objetos com chave ``t`` (tipo):
      {"t": "p",         "v": "texto do parágrafo"}
      {"t": "h3",        "v": "título"}
      {"t": "h4",        "v": "subtítulo"}
      {"t": "ul"/"ol",   "lis": ["item1", "item2"]}
      {"t": "blockquote","v": "citação"}
      {"t": "img",       "src": "https://...", "alt": "..."}

    Usado pelo Platts Phase 2 e pelo playwright_reader para evitar
    parsing de innerHTML Angular (que causa merging de palavras em spans).
    """
    import json as _json

    if not items_json or items_json in ("[]", "null", ""):
        return ""
    try:
        items = _json.loads(items_json)
    except Exception:
        return ""

    out: list[str] = []
    for item in items:
        t = item.get("t", "")
        if t == "p":
            v = (item.get("v") or "").strip()
            if v:
                out.append(f"<p>{_he.escape(v)}</p>")
        elif t == "h3":
            v = (item.get("v") or "").strip()
            if v:
                out.append(f"<h3>{_he.escape(v)}</h3>")
        elif t == "h4":
            v = (item.get("v") or "").strip()
            if v:
                out.append(f"<h4>{_he.escape(v)}</h4>")
        elif t == "blockquote":
            v = (item.get("v") or "").strip()
            if v:
                out.append(f"<blockquote><p>{_he.escape(v)}</p></blockquote>")
        elif t in ("ul", "ol"):
            lis = item.get("lis") or []
            if lis:
                tag = t
                li_html = "".join(
                    f"<li>{_he.escape(li)}</li>" for li in lis if li.strip()
                )
                if li_html:
                    out.append(f"<{tag}>{li_html}</{tag}>")
        elif t == "img":
            src = (item.get("src") or "").strip()
            alt = (item.get("alt") or "").strip()
            if src.startswith("https://"):
                out.append(f'<img src="{src}" alt="{_he.escape(alt)}" class="reader-img">')

    result = "\n".join(out)
    return _strip_related_html(result)


def extract_article_container(soup, url: str = ""):
    """Retorna o elemento BeautifulSoup com o corpo do artigo, com lógica por domínio.

    Para sites conhecidos usa seletores específicos que evitam capturar seções de
    "More News", related articles e outros ruídos que aparecem dentro do container
    genérico <article> ou <main>.

    Para sites desconhecidos: fallback para article > role=main > main.

    Parâmetros
    ----------
    soup : BeautifulSoup
        Página já parseada (pode estar parcialmente limpa de scripts/navs).
    url : str
        URL completa do artigo (usada para detectar o domínio).

    Retorna
    -------
    Tag ou None
    """
    from urllib.parse import urlparse
    domain = urlparse(url).netloc.lower() if url else ""

    if domain in ("www.mining.com", "mining.com"):
        # Estrutura mining.com:
        #   article > div.post-inner-content > div.content  (texto real)
        #                                    > div.more-news   (seção "More News" — NÃO incluir)
        #                                    > div.share       (barra social)
        #                                    > section.comment-section
        # Estratégia dupla:
        #   1. Seletor específico div.content (irmão do more-news, não contém ele)
        #   2. Corte por sentinel: remove tudo a partir do primeiro elemento
        #      cujo texto começa com "More News" (salvaguarda para variações)
        container = soup.select_one("div.post-inner-content .content")
        if container:
            # Passa 1 — remove ruídos por seletor CSS
            for tag in container.select(
                "div.more-news, section.comment-section, div.share,"
                " .ad-block, .ad-slot, figure.wp-block-embed,"
                " div.d-flex.justify-content-center"
            ):
                tag.decompose()

            # Passa 2 — corte por sentinel "More News" (qualquer elemento de bloco
            # cujo texto comece com essa expressão sinaliza início da seção indesejada)
            _MORE_NEWS_RE = re.compile(
                r"^\s*more\s+news\b", re.IGNORECASE
            )
            sentinel_found = False
            for child in list(container.children):
                if sentinel_found:
                    child.extract()
                    continue
                text = getattr(child, "get_text", lambda **_: "")(" ", strip=True)
                if _MORE_NEWS_RE.match(text):
                    sentinel_found = True
                    child.extract()

        return container

    if domain in ("www.worldcement.com", "worldcement.com"):
        # Estrutura worldcement.com:
        #   article.article-detail
        #     header > h1  → título (já capturado separadamente)
        #     div.lead > p → lead paragraph
        #     > div > p    → parágrafos do corpo
        #     .tab-content / .tab-pane → seção de tags (REMOVER)
        container = soup.select_one("article.article-detail")
        if container:
            for el in container.select(
                ".tab-pane, .tab-content, .tags-container,"
                " .row.row-btn, .btn-default,"
                " article > header,"
                " script, style, form"
            ):
                el.decompose()
        return container

    # Fallback genérico
    return (
        soup.find("article")
        or soup.find(attrs={"role": "main"})
        or soup.find("main")
    )


def article_to_safe_html(raw_html: str) -> str:
    """Converte HTML bruto de artigo em HTML seguro preservando paragráfos e imagens.

    Tenta usar BeautifulSoup; se não estiver disponível cai no fallback de regex.
    """
    if not raw_html or not raw_html.strip():
        return ""
    try:
        return _bs4_extract(raw_html)
    except Exception:
        return _fallback_extract(raw_html)


def _img_tag(node) -> str | None:
    """Retorna <img class='reader-img'> seguro, ou None se src inválido."""
    src = (node.get("src") or "").strip()
    alt = _he.escape((node.get("alt") or "").strip())
    if src.startswith("//"):
        src = "https:" + src
    if not src.startswith("https://"):
        return None
    return f'<img src="{src}" alt="{alt}" class="reader-img">'


def _inline_html(node, NavigableString) -> str:
    """Converte o conteúdo inline de um elemento para HTML seguro.

    Usa strip=False em get_text para preservar espaços iniciais/finais de
    elementos como <span> prices</span> — comum em Angular SPAs onde cada
    palavra fica em um <span> separado.  Sem isso, "Steel prices rose" vira
    "Steelpricesrose".  Os espaços múltiplos são normalizados no join final.
    """
    parts: list[str] = []
    for ch in node.children:
        if isinstance(ch, NavigableString):
            # Sempre inclui o NavigableString (mesmo se só espaço):
            # espaços entre elementos são separadores de palavra.
            t = str(ch)
            if t:
                parts.append(_he.escape(t))
        elif ch.name in ("strong", "b"):
            t = ch.get_text(" ", strip=False)
            if t.strip():
                parts.append(f"<strong>{_he.escape(t.strip())}</strong>")
        elif ch.name in ("em", "i"):
            t = ch.get_text(" ", strip=False)
            if t.strip():
                parts.append(f"<em>{_he.escape(t.strip())}</em>")
        elif ch.name == "br":
            parts.append("<br>")
        elif ch.name == "img":
            it = _img_tag(ch)
            if it:
                parts.append(it)
        elif ch.name == "a":
            # Preserva texto do link, descarta href
            t = ch.get_text(" ", strip=False)
            if t.strip():
                parts.append(_he.escape(t))
        else:
            # Qualquer outro inline (span, div, etc.): strip=False para não
            # perder o espaço inicial de <span> prices</span>.
            t = ch.get_text(" ", strip=False)
            if t.strip():
                parts.append(_he.escape(t))
    # Junta tudo e normaliza espaços múltiplos (tabs/newlines → espaço único)
    raw = "".join(parts)
    raw = re.sub(r"[ \t]+", " ", raw)
    raw = re.sub(r"\n+", " ", raw)
    return raw.strip()


def _bs4_extract(raw_html: str) -> str:
    from bs4 import BeautifulSoup, NavigableString  # type: ignore

    soup = BeautifulSoup(raw_html, "lxml")

    # Remove ruído
    for tag in soup.find_all([
        "script", "style", "noscript", "iframe", "form",
        "button", "nav", "header", "footer", "aside", "svg",
        "figure > figcaption",  # captions sem imagem pai = fragmento inútil
    ]):
        tag.decompose()
    for tag in soup.find_all(class_=re.compile(
        r"\b(related|share|social|ads?|cookie|promo|sponsored"
        r"|newsletter|subscribe|toolbar|caption-credit)\b",
        re.I,
    )):
        tag.decompose()

    out: list[str] = []

    def walk(node) -> None:  # noqa: ANN001
        if isinstance(node, NavigableString):
            t = str(node).strip()
            if len(t) > 12:
                out.append(f"<p>{_he.escape(t)}</p>")
            return

        name = (getattr(node, "name", None) or "").lower()

        if name == "img":
            it = _img_tag(node)
            if it:
                out.append(it)

        elif name == "figure":
            # Figura: imagem + legenda opcional
            img_el = node.find("img")
            cap_el = node.find("figcaption")
            if img_el:
                it = _img_tag(img_el)
                if it:
                    out.append(it)
                    if cap_el:
                        cap = cap_el.get_text(" ", strip=True)
                        if cap:
                            out.append(
                                f'<p class="reader-caption"><em>{_he.escape(cap)}</em></p>'
                            )

        elif name == "p":
            inner = _inline_html(node, NavigableString)
            if inner.strip():
                out.append(f"<p>{inner}</p>")
            # Imagens soltas dentro de <p> já capturadas por _inline_html

        elif name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            t = node.get_text(" ", strip=True)
            if t:
                level = "h3" if name in ("h1", "h2") else "h4"
                out.append(f"<{level}>{_he.escape(t)}</{level}>")

        elif name in ("ul", "ol"):
            items = [
                f"<li>{_he.escape(li.get_text(' ', strip=True))}</li>"
                for li in node.find_all("li")
                if li.get_text(" ", strip=True)
            ]
            if items:
                tag = "ul" if name == "ul" else "ol"
                out.append(f"<{tag}>{''.join(items)}</{tag}>")

        elif name == "blockquote":
            t = node.get_text(" ", strip=True)
            if t:
                out.append(f"<blockquote><p>{_he.escape(t)}</p></blockquote>")

        elif name in (
            "script", "style", "noscript", "iframe", "form",
            "button", "nav", "header", "footer", "aside", "svg",
        ):
            pass  # ignorar

        else:
            # div, section, article, span, table, td, figure…: desce nos filhos
            for child in node.children:
                walk(child)

    root = soup.body or soup
    for child in root.children:
        walk(child)

    result = "\n".join(out)
    return _strip_related_html(result)


def _fallback_extract(raw_html: str) -> str:
    """Fallback sem BeautifulSoup: converte tags de bloco em \\n\\n, preserva <img>."""
    # Extrai imagens antes de remover tags
    imgs = re.findall(r'<img[^>]+src=["\']?(https://[^"\'>\s]+)["\']?[^>]*>', raw_html, re.I)
    alts = re.findall(r'<img[^>]+alt=["\']([^"\']*)["\']', raw_html, re.I)

    h = re.sub(
        r'</?(p|div|h[1-6]|li|blockquote)(\s[^>]*)?>',
        '\n', raw_html, flags=re.I,
    )
    h = re.sub(r'<br\s*/?>', '\n', h, flags=re.I)
    h = re.sub(r'<[^>]+>', '', h)
    h = _he.unescape(h)
    lines = [line.strip() for line in h.splitlines() if line.strip()]
    paragraphs = [f"<p>{_he.escape(l)}</p>" for l in lines if len(l) > 10]

    img_tags = []
    for i, src in enumerate(imgs):
        alt = alts[i] if i < len(alts) else ""
        img_tags.append(f'<img src="{src}" alt="{_he.escape(alt)}" class="reader-img">')

    return "\n".join(paragraphs + img_tags)
