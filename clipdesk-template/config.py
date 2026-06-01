"""Configuração de marca / produto.

Edite este arquivo para personalizar o e-mail gerado.
Tudo aqui é cosmético — não afeta scraping nem fontes.
"""

# Nome do produto. Aparece no cabeçalho e no assunto do e-mail.
PRODUCT_NAME = "My News Clipping"

# Cor de destaque do header e do título "Headlines" (hex com #).
HEADER_COLOR = "#FF5000"

# Título da seção de bullets logo abaixo do header.
HEADLINES_TITLE = "Main Headlines"

# Idioma do mês na data do header: "en" (April) ou "pt" (Abril).
DATE_LANGUAGE = "en"

# Template do assunto do e-mail. Placeholders disponíveis: {product_name}, {date}.
SUBJECT_TEMPLATE = "*** {product_name} – {date} ***"

# Template do título central no corpo do e-mail.
HEADER_TEMPLATE = "*** {product_name} – {date} ***"

# Template do nome do arquivo .eml gerado. {date} é YYYY-MM-DD.
OUTPUT_FILENAME_TEMPLATE = "clipping_{date}.eml"

# Bloco HTML que aparece logo abaixo do índice de Headlines (antes das matérias).
# Tipicamente: nome da equipe / contatos. Deixe "" para omitir totalmente.
# Use a mesma classe MsoNormal para preservar o look-and-feel do Outlook.
SIGNATURE_BLOCK_HTML = ""
