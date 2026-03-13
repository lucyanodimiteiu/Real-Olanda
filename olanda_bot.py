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
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN') # Presupunem că folosești Token pentru Bot API aici
CANAL_DESTINATIE = os.getenv('OLANDA_CHANNEL_ID') 

RSS_FEEDS = [
    'https://www.nu.nl/rss/Algemeen',
    'https://nos.nl/export/rss/nederland.xml',
    # Adaugă aici restul feed-urilor tale
]

BLACKLIST_FILE = "processed_links_olanda.txt"
BLACKLIST_SET = set()
SEMNATURA = "@Real_Olanda"

# ==========================================
# MEMORIE ȘI DEDUPLICARE (Upgrade Real-Live)
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
# CURĂȚARE JSON (Recomandarea Grigore)
# ==========================================
def clean_json_response(raw_text):
    # Elimină ```json ... ``` dacă DeepSeek le adaugă
    clean_text = re.sub(r'```json\s*|```', '', raw_text).strip()
    return clean_text

# ==========================================
# AI - TRADUCERE ȘI ANALIZĂ
# ==========================================
async def proceseaza_cu_ai(titlu, descriere):
    if not DEEPSEEK_KEY: return None
    
    prompt = f"""Ești un traducător profesionist. Traduce și rezumă știrea în română.
Titlu: {titlu}
Descriere: {descriere}
Răspunde strict JSON: {{"text_ro": "Titlu Tradus - Rezumat pe scurt"}}"""

    headers = {"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"}
    try:
        resp = requests.post("https://api.deepseek.com/v1/chat/completions", json={
            "model": "deepseek-chat", 
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "response_format": {"type": "json_object"}
        }, headers=headers, timeout=30)
        
        content = clean_json_response(resp.json()['choices'][0]['message']['content'])
        return json.loads(content)
    except Exception as e:
        print(f"⚠️ Eroare AI: {e}")
        return None

# ==========================================
# MAIN LOOP
# ==========================================
async def main():
    load_blacklist()
    stiri_noi = []

    for url in RSS_FEEDS:
        feed = feedparser.parse(url)
        for entry in feed.entries[:10]:
            titlu = getattr(entry, 'title', '')
            link = getattr(entry, 'link', '')
            # Cream un hash stabil din Link (RSS-ul are link-uri unice de obicei)
            h = hash_text(link)

            if not is_blacklisted(h):
                print(f"🔎 Știre nouă găsită: {titlu}")
                res = await proceseaza_cu_ai(titlu, entry.get('description', ''))
                if res:
                    # Aici trimiți pe Telegram (presupunem funcția de trimitere)
                    # await trimite_telegram(res['text_ro'], link)
                    print(f"📤 Postat: {titlu}")
                    add_to_blacklist(h)

if __name__ == "__main__":
    asyncio.run(main())
