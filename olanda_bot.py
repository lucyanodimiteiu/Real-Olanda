import os
import time
import hashlib
import json
import sqlite3
import requests
import feedparser
import re
from datetime import datetime
from typing import Optional, Dict, Any

# ==========================================
# CONFIGURAȚII (Asistent Șoferi NL - CLOUD WORKER)
# ==========================================
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CANAL_DESTINATIE = os.getenv("TELEGRAM_CHANNEL_ID")

RSS_FEEDS = [
    "https://www.rijkswaterstaat.nl/rss",
    "https://nos.nl/export/rss/economie.xml",
    "https://www.ttm.nl/feed/",
    "https://www.nu.nl/rss/Economie"
]

BLACKLIST_FILE = "processed_links_olanda.txt"
DB_PATH = "memorie_stiri_olanda.db"
BLACKLIST_SET = set()
SEMNATURA = "@real_live_by_luci"
VERIFY_INTERVAL = 60 # Secunde intre verificari

# ==========================================
# GESTIONARE MEMORIE URL (Nivel 1)
# ==========================================
def load_blacklist():
    global BLACKLIST_SET
    if os.path.exists(BLACKLIST_FILE):
        try:
            with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
                BLACKLIST_SET = set(line.strip() for line in f if line.strip())
        except: pass

def is_blacklisted(h: str) -> bool:
    return h in BLACKLIST_SET

def add_to_blacklist(h: str):
    if h not in BLACKLIST_SET:
        BLACKLIST_SET.add(h)
        try:
            with open(BLACKLIST_FILE, "a", encoding="utf-8") as f:
                f.write(h + "\n")
        except: pass

def hash_text(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()

# ==========================================
# BAZĂ DE DATE SQLite (Memorie Semantică Nivel 2)
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS stiri_recente (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text_rezumat TEXT,
                    data_postare TEXT)''')
    conn.commit()
    conn.close()

def preia_stiri_vechi(limita=15):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        rezultate = c.execute('SELECT text_rezumat FROM stiri_recente ORDER BY id DESC LIMIT ?', (limita,)).fetchall()
        return [row[0] for row in rezultate]
    except:
        return []
    finally:
        conn.close()

def salveaza_stire_in_memorie(text_rezumat):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute('INSERT INTO stiri_recente (text_rezumat, data_postare) VALUES (?, ?)', 
                  (text_rezumat, datetime.now().isoformat()))
        conn.commit()
        c.execute('DELETE FROM stiri_recente WHERE id NOT IN (SELECT id FROM stiri_recente ORDER BY id DESC LIMIT 50)')
        conn.commit()
    except Exception as e:
        print(f"⚠️ Eroare DB: {e}")
    finally:
        conn.close()

# ==========================================
# UTILITARE AI
# ==========================================
def clean_json_response(text: str) -> str:
    cleaned = re.sub(r"^```json\s*", "", text).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    return cleaned

def proceseaza_cu_ai(titlu: str, descriere: str, texte_vechi: list) -> Optional[Dict[str, Any]]:
    if not DEEPSEEK_KEY: return None

    titlu_curat = titlu.replace('"', "'")[:200]
    desc_curat = (descriere or "Fără descriere").replace('"', "'")[:600]
    context_vechi = "\n".join([f"- {t}" for t in texte_vechi]) if texte_vechi else "Nicio stire recenta."

    prompt = f"""Ești un asistent inteligent pentru un șofer profesionist de camion în Olanda.
Analizează ȘTIREA NOUĂ. 
1. Dacă NU are legătură cu Olanda (sau transportul aferent), setează categoria la "IGNORE".
2. DEDUPLICARE SEMANTICĂ: Compară ȘTIREA NOUĂ cu ȘTIRILE VECHI. Dacă este exact același subiect major (ex: aceeași grevă a unui sindicat, același accident raportat de altă sursă), setează "duplicat": true.
3. Dacă e relevantă și nouă, tradu și rezumă în română evidențiind impactul (drumuri, taxe, sindicate).
Categorii permise: #Trafic_Drumuri, #Legislatie_Taxe, #Sindicate_CAO, #Economie_Logistica, IGNORE.
Alege un emoji (🚛, ⚖️, 👷, 💶).

ȘTIRI VECHI:
{context_vechi}

ȘTIRE NOUĂ:
Titlu: {titlu_curat}
Descriere: {desc_curat}

Răspunde STRICT JSON:
{{"categorie": "#Trafic_Drumuri", "emoji": "🚛", "text_ro": "Rezumat clar max 2 prop.", "duplicat": false}}"""

    headers = {"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"}

    try:
        resp = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1, 
                "response_format": {"type": "json_object"}
            },
            headers=headers, timeout=60
        )
        resp.raise_for_status()
        data = resp.json()
        rezultat = json.loads(clean_json_response(data["choices"][0]["message"]["content"]))
        return rezultat
    except Exception as e:
        print(f"⚠️ Eroare AI: {e}")
        return None

# ==========================================
# TELEGRAM
# ==========================================
def trimite_telegram(text_final: str) -> bool:
    if not TELEGRAM_TOKEN or not CANAL_DESTINATIE: return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": CANAL_DESTINATIE, "text": text_final[:3997], "parse_mode": "HTML", "disable_web_page_preview": False}, timeout=15)
        return resp.status_code == 200
    except: return False

# ==========================================
# MAIN LOOP (WORKER MODE)
# ==========================================
def main():
    print(f"🚀 Pornire Asistent NL WORKER v3.0: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if not all([DEEPSEEK_KEY, TELEGRAM_TOKEN, CANAL_DESTINATIE]): 
        print("❌ Configurație incompletă! Setati DEEPSEEK_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID.")
        return

    init_db()
    load_blacklist()
    
    while True:
        print(f"🔄 Verificare fluxuri la {datetime.now().strftime('%H:%M:%S')}...")
        stiri_vechi_db = preia_stiri_vechi(15) 
        
        for url in RSS_FEEDS:
            try: feed = feedparser.parse(url)
            except: continue

            if not hasattr(feed, "entries"): continue

            for entry in feed.entries[:5]: # Verificam doar ultimele 5 stiri pentru a fi rapizi
                titlu = getattr(entry, "title", None)
                link = getattr(entry, "link", None)
                if not link or not titlu: continue

                h = hash_text(link)
                if is_blacklisted(h): continue 

                descriere = getattr(entry, "description", "") or getattr(entry, "summary", "")
                res = proceseaza_cu_ai(titlu, descriere, stiri_vechi_db)

                if res:
                    if res.get("categorie") == "IGNORE":
                        add_to_blacklist(h)
                    elif res.get("duplicat", False):
                        add_to_blacklist(h) 
                    else:
                        text_rezumat = res.get('text_ro', 'Fara text')
                        postare = f"{res.get('emoji', '📌')} <b>{res.get('categorie')}</b>\n\n{text_rezumat}\n\n🔗 <a href='{link}'>Sursa Originală</a>\n\n<i>{SEMNATURA}</i>"
                        
                        if trimite_telegram(postare):
                            add_to_blacklist(h)
                            salveaza_stire_in_memorie(text_rezumat) 
                            stiri_vechi_db.insert(0, text_rezumat)  
                            print(f"   ✅ Postat: {titlu[:30]}...")
                            time.sleep(2)
                time.sleep(1)
        
        print(f"💤 Asteptare {VERIFY_INTERVAL} secunde pana la urmatoarea verificare...")
        time.sleep(VERIFY_INTERVAL)

if __name__ == "__main__":
    main()
