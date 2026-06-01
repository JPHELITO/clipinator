import os
from pathlib import Path

# Carrega variáveis do .env antes de iniciar o servidor
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=False)
except ImportError:
    pass

import uvicorn

if __name__ == "__main__":
    uvicorn.run("newshunter.app:app", host="0.0.0.0", port=8765, reload=True)
