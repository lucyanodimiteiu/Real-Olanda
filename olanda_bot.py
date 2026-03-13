import os
import asyncio
import hashlib
import sqlite3
import json
import requests
import feedparser
import re
import pytz
from datetime import datetime

# ==========================================
# CONFIGURAȚII
# ==========================================
DEEPSEEK_KEY = os.getenv('DEEPSEEK_API_KEY')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CANAL_DESTINATIE = os.getenv('OLANDA_CHANNEL_ID') 

RSS_FEEDS = [
    'https://www.nu.nl/rss/Algemeen',
    'https://nos.nl/export/rss/nederland.xml',
    'https://www.anwb.nl/feeds/verkeersinformatie'
]

BLACKLIST_FILE = "processed_links_olanda.txt"
BLACKLIST_SET = set()
SEMNATURA = "@real_live_by_luci"

# ==========================================
# MEMORIE ȘI DEDUPLICARE
# ==========================================
def load_blacklist():
    global BLACKLIST_SET
    if os.path.exists(BLACKLIST_FILE):
        with open(BLACKLIST_FILE, 'r', encoding='utf-8') as f:
            BLACKLIST_SET = set(line.strip() for line in f if line.strip())

def is_blacklisted(h):
    return h in BLACKLIST_SET

def add_to_blacklist(h):
    if h not in BLACKLIST_SET:
        BLACKLIST_SET.add(h)
        try:
            with open(BLACKLIST_FILE, 'a', encoding='utf-8') as f:
                f.write(h + '\n')
        except Exception as e:
            print(f"⚠️ Eroare scriere blacklist: {e}")

def hash_text(text):
    return hashlib.md5(text.encode('utf-8')).hexdigest()

# ==========================================
# CURĂȚARE JSON (Protecție Grigore-Approved)
# ==========================================
def clean_json_response(raw_text):
    clean_text = re.sub(r'```json\s*|```', '', raw_text).strip()
    return clean_text

# ==========================================
# AI - TRADUCERE ȘI CATEGORISIRE (Upgrade Estetic)
# ==========================================
async def proceseaza_cu_ai(titlu, descriere):
    if not DEEPSEEK_KEY: 
        print("❌ EROARE: Lipsă DEEPSEEK_API_KEY în Secrets!")
        return None
    
    prompt = f"""
Ești un editor de știri OSINT. Traduce și rezumă știrea în română (stil Reuters).
Alege o categorie: #Transport, #Vreme, #Politica, #Economie, #Social sau #Diverse.
Alege un emoji relevant.

Titlu: {titlu}
Descriere: {descriere}

Răspunde STRICT JSON: 
{{"categorie": "#Diverse", "emoji": "📰", "text_ro": "Titlu Tradus - Rezumat..."}}
"""

    headers = {"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"}
    try:
        resp = requests.post("https://api.deepseek.com/v1/chat/completions", json={
            "model": "deepseek-chat", 
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2
        }, headers=headers, timeout=30)
        
        # --- LINIA DE KONTROL ---
        raw_content = resp.json()['choices'][0]['message']['content']
        print(f"🤖 AI Response raw: {raw_content[:150]}...") 
        # ------------------------

        content = clean_json_response(raw_content)
        return json.loads(content)
    except Exception as e:
        print(f"⚠️ Eroare la comunicarea cu AI: {e}")
        return None

# ==========================================
# TELEGRAM SEND
# ==========================================
async def trimite_telegram(text_final):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CANAL_DESTINATIE,
        "text": text_final,
        "parse_mode": "HTML"
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        print(f"❌ Eroare Telegram: {e}")
        return False

# ==========================================
# MAIN LOOP
# ==========================================
async def main():
    load_blacklist()
    
    for url in RSS_FEEDS:
        print(f"📡 Scanăm feed-ul: {url}")
        feed = feedparser.parse(url)
        for entry in feed.entries[:10]:
            titlu = getattr(entry, 'title', '')
            link = getattr(entry, 'link', '')
            h = hash_text(link)

            if not is_blacklisted(h):
                print(f"🔎 Procesăm știrea: {titlu}")
                res = await proceseaza_cu_ai(titlu, entry.get('description', ''))
                
                if res:
                    # Construim postarea cu noul format
                    postare_finala = (
                        f"{res['emoji']} <b>{res['categorie']}</b>\n\n"
                        f"{res['text_ro']}\n\n"
                        f"🔗 <a href='{link}'>Sursa Originală</a>\n\n"
                        f"{SEMNATURA}"
                    )
                    
                    if await trimite_telegram(postare_finala):
                        add_to_blacklist(h)
                        print(f"✅ Postat cu succes!")
                        await asyncio.sleep(2) # Pauză între postări
            else:
                print(f"⏭️ Sărim peste duplicat: {titlu[:30]}...")

if __name__ == "__main__":
    asyncio.run(main())
