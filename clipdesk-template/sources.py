"""Registro de fontes — domínio → nome legível e extractor.

NÃO edite à mão sem seguir BUILD_GUIDE.md. O guide valida cada fonte com um
teste real (scrape de uma URL → ≥3 parágrafos com texto real) antes de
adicionar aqui. Entradas adicionadas sem teste tendem a quebrar em produção.

Para domínios que servem o mesmo site (ex.: "exemplo.com" e "www.exemplo.com"),
registre os dois apontando para o mesmo extractor.
"""
from __future__ import annotations

from extractors import Extractor, _make_extractor, ex_auto

# ---------------------------------------------------------------------------
# Extractors customizados
# ---------------------------------------------------------------------------
# Crie um extractor dedicado quando ex_auto não pegar o corpo do artigo.
# Passe uma lista de seletores CSS do container do artigo, em ordem de
# prioridade — o primeiro que casar vence. Sempre termine com "article" como
# fallback.
#
# Exemplo:
# ex_meusite = _make_extractor([
#     "div.article-body",
#     "div.entry-content",
#     "article",
# ])


# ---------------------------------------------------------------------------
# Mapas de fontes
# ---------------------------------------------------------------------------
# SOURCE_NAMES: domínio (sem protocolo) → nome legível que aparece no e-mail.
# EXTRACTORS:   domínio → função extractor (ex_auto ou um ex_<site> custom).
#
# Os dois precisam ter exatamente as mesmas chaves.

SOURCE_NAMES: dict[str, str] = {
    # "exemplo.com": "Exemplo News",
    # "www.exemplo.com": "Exemplo News",
}

EXTRACTORS: dict[str, Extractor] = {
    # "exemplo.com": ex_auto,
    # "www.exemplo.com": ex_auto,
}


# ---------------------------------------------------------------------------
# Domínios que precisam de TLS-fingerprint de Chrome real
# ---------------------------------------------------------------------------
# Adicione aqui qualquer site cujo fetch normal devolva 403 / Cloudflare
# challenge. O scraper vai usar curl_cffi com impersonate=chrome124.
# Cookies (em cookies/<dominio>.txt) continuam sendo enviados.

IMPERSONATE_DOMAINS: set[str] = set()
