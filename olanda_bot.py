import os
import asyncio
import hashlib
import json
import requests
import feedparser
import re
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
# GESTIONARE MEMORIE
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
# UTILITARE AI
# ==========================================
def clean_json_response(text_de_curatat):
    # Elimină blocurile Markdown (ex:
    return re.sub(r'
json\s*|```', '', text_de_curatat).strip()

async def proceseaza_cu_ai(titlu, descriere):
    if not DEEPSEEK_KEY: 
        print("❌ Lipsă cheie API DeepSeek!")
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
            "temperature": 0.2,
            "response_format": {"type": "json_object"} # FORȚĂM JSON-UL SĂ FIE VALID
        }, headers=headers, timeout=60) # TIMEOUT MĂRIT LA 60S
        
        # Verificăm dacă serverul DeepSeek a dat o eroare generală (ex. 502 Bad Gateway)
        resp.raise_for_status()
        
        # Extragem conținutul brut
        continut_brut = resp.json()['choices'][0]['message']['content']
        print(f"🤖 AI Response raw: {str(continut_brut)[:150]}...") 
        
        # Curățăm și parsăm JSON-ul
        continut_curat = clean_json_response(continut_brut)
        return json.loads(continut_curat)
    except Exception as e:
        print(f"⚠️ Eroare AI: {e}")
        return None

# ==========================================
# TELEGRAM
# ==========================================
async def trimite_telegram(text_final):
    if not TELEGRAM_TOKEN or not CANAL_DESTINATIE: return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CANAL_DESTINATIE, "text": text_final, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=15)
        return resp.status_code == 200
    except Exception:
        return False

# ==========================================
# MAIN
# ==========================================
async def main():
    print(f"🚀 Pornire: {datetime.now().strftime('%H:%M:%S')}")
    load_blacklist()
 
    for url in RSS_FEEDS:
        print(f"📡 Scanăm: {url}")
        feed = feedparser.parse(url)
        if not hasattr(feed, 'entries'): continue

        for entry in feed.entries[:15]:
            if not hasattr(entry, 'get') or isinstance(entry, str): continue
 
            titlu = getattr(entry, 'title', '')
            link = getattr(entry, 'link', '')
            if not link: continue

            h = hash_text(link)
            if not is_blacklisted(h):
                print(f"🔎 Știre nouă: {titlu[:50]}...")
                res = await proceseaza_cu_ai(titlu, getattr(entry, 'description', ''))
 
                if res:
                    postare = (
                        f"{res.get('emoji', '📰')} <b>{res.get('categorie', '#Diverse')}</b>\n\n"
                        f"{res.get('text_ro', '...')}\n\n"
                        f"🔗 <a href='{link}'>Sursa Originală</a>\n\n"
                        f"{SEMNATURA}"
                    )
                    if await trimite_telegram(postare):
                        add_to_blacklist(h)
                        print("✅ Postat!")
                        await asyncio.sleep(2)
            else:
                print("⏭️ Sărit (duplicat)")

    print("🏁 Finalizat.")

if __name__ == "__main__":
    asyncio.run(main())
