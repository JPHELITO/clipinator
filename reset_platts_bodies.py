"""Zera os corpos dos artigos da Platts para que sejam re-extraídos corretamente."""
import sqlite3

db_path = "data/newshunter.db"
conn = sqlite3.connect(db_path)
cur = conn.execute("UPDATE articles SET body = NULL WHERE domain = 'core.spglobal.com'")
conn.commit()
conn.close()
print(f"Feito. {cur.rowcount} artigo(s) da Platts tiveram o corpo zerado.")
