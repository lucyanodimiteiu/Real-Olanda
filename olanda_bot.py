import os
import sqlite3
import hashlib
import json
import requests
import feedparser
from datetime import datetime

# --- CONFIGURARE ---
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY")

DB_PATH = "stiri_olanda.db"

RSS_FEEDS = [
    "https://feeds.nos.nl/nosnieuwsalgemeen",
    "https://www.nu.nl/rss/Algemeen"
]

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS stiri (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hash_link TEXT UNIQUE,
                    titlu TEXT,
                    sursa TEXT,
                    data_postare TEXT
                 )''')
    conn.commit()
    conn.close()

def hash_text(text):
    return hashlib.md5(text.encode('utf-8')).hexdigest()

def stire_existenta(hash_link):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id FROM stiri WHERE hash_link = ?', (hash_link,))
    result = c.fetchone()
    conn.close()
    return result is not None

def salveaza_stire(hash_link, titlu, sursa):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO stiri (hash_link, titlu, sursa, data_postare) VALUES (?, ?, ?, ?)', 
              (hash_link, titlu, sursa, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def trimite_pe_telegram(text):
    if not BOT_TOKEN or not CHANNEL_ID:
        print("Lipsesc variabilele de Telegram.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    resp = requests.post(url, json=payload)
    if resp.status_code != 200:
        print(f"Eroare Telegram: {resp.text}")

def evalueaza_si_traduce_batch(stiri_noi):
    if not DEEPSEEK_KEY or not stiri_noi:
        return []
    
    # Preparăm textul pentru prompt
    batch_text = ""
    for idx, stire in enumerate(stiri_noi):
        batch_text += f"[{idx}] {stire['title']} - {stire['description']}\\n"

    prompt = f"""
Ești un editor de știri senior (AI OSINT). Analizează următorul lot de știri brute din Olanda.
REGULI:
1. ELIMINĂ complet știrile despre mondenități (cancan, showbiz, sport minor, bârfe).
2. Păstrează DOAR știrile majore (politică, economie, societate, incidente grave).
3. Alege MAXIMUM 10 cele mai importante știri din acest lot.
4. TRADUCE și RESCRIE fiecare știre selectată în limba română (stil Reuters/jurnalistic, maxim 3 paragrafe, fără opinii personale).

Răspunde STRICT în format JSON (array de obiecte), fără alte comentarii de formatare:
[
  {{"idx_original": 0, "titlu_ro": "Titlu Tradus", "rezumat_ro": "Rezumat clar și concis."}},
  ...
]

ȘTIRI BRUTE:
{batch_text}
"""
    try:
        url = "https://api.deepseek.com/chat/completions"
        headers = {"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"}
        payload = {
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3
        }
        resp = requests.post(url, json=payload, headers=headers)
        if resp.status_code == 200:
            content = resp.json()['choices'][0]['message']['content'].strip()
            # Clean potential markdown markdown block
            if content.startswith("```json"): content = content[7:]
            if content.startswith("```"): content = content[3:]
            if content.endswith("```"): content = content[:-3]
            
            return json.loads(content)
        else:
            print(f"Eroare DeepSeek: {resp.text}")
            return []
    except Exception as e:
        print(f"Eroare parsare JSON AI: {e}")
        return []

def main():
    init_db()
    stiri_noi = []
    
    # 1. Scraping RSS
    for feed_url in RSS_FEEDS:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries[:15]: # Luăm ultimele 15 din fiecare sursă
            link_hash = hash_text(entry.link)
            if not stire_existenta(link_hash):
                stiri_noi.append({
                    "hash": link_hash,
                    "title": entry.title,
                    "description": getattr(entry, 'description', ''),
                    "link": entry.link,
                    "source": feed_url
                })

    print(f"S-au găsit {len(stiri_noi)} știri noi neprocesate.")
    if not stiri_noi:
        return

    # 2. Procesare prin AI (Filtrare + Traducere)
    rezultate = evalueaza_si_traduce_batch(stiri_noi)
    
    # 3. Postare și Salvare
    for rez in rezultate:
        idx = rez.get('idx_original')
        if idx is not None and 0 <= idx < len(stiri_noi):
            stire_bruta = stiri_noi[idx]
            titlu_ro = rez.get('titlu_ro', '')
            rezumat_ro = rez.get('rezumat_ro', '')
            
            # Format mesaj Telegram
            mesaj = f"🇳🇱 <b>{titlu_ro}</b>\n\n{rezumat_ro}\n\n<a href='{stire_bruta['link']}'>Sursa originală</a>"
            
            trimite_pe_telegram(mesaj)
            salveaza_stire(stire_bruta['hash'], stire_bruta['title'], stire_bruta['source'])

    # Salvăm și restul știrilor ca "procesate" ca să nu le mai trimitem la AI tura viitoare
    for stire in stiri_noi:
        if not stire_existenta(stire['hash']):
            salveaza_stire(stire['hash'], stire['title'], stire['source'])

if __name__ == "__main__":
    main()
