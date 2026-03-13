import os
import asyncio
import hashlib
import json
import requests
import feedparser
import re
import pytz
from datetime import datetime

# ==========================================
# CONFIGURAȚII (Preluat din GitHub Secrets)
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
# GESTIONARE MEMORIE (Deduplicare)
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
 print(f"⚠️ Eroare scriere blacklist local: {e}")

def hash_text(text):
 return hashlib.md5(text.encode('utf-8')).hexdigest()

# ==========================================
# UTILITARE AI ȘI CLEANUP
# ==========================================
def clean_json_response(raw_text):
 # Elimină blocurile Markdown
 clean_text = re.sub(r'
json\s*|```', '', raw_text).strip()
 return clean_text

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
 
 raw_content = resp.json()['choices'][0]['message']['content']
 print(f"🤖 AI Response: {raw_content[:150]}...") 
 
 content = clean_json_response(raw_content)
 return json.loads(content)
 except Exception as e:
 print(f"⚠️ Eroare AI: {e}")
 return None

# ==========================================
# TELEGRAM COMUNICAȚIE
# ==========================================
async def trimite_telegram(text_final):
 if not TELEGRAM_TOKEN or not CANAL_DESTINATIE:
 print("❌ EROARE: Lipsesc credențialele Telegram!")
 return False
 
 url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
 payload = {
 "chat_id": CANAL_DESTINATIE,
 "text": text_final,
 "parse_mode": "HTML",
 "disable_web_page_preview": False
 }
 try:
 resp = requests.post(url, json=payload, timeout=15)
 return resp.status_code == 200
 except Exception as e:
 print(f"❌ Eroare la trimiterea mesajului: {e}")
 return False

# ==========================================
# NUCLEUL SISTEMULUI (Main cu fix pentru 'str' object)
# ==========================================
async def main():
 print(f"🚀 Pornire robot Real-Olanda: {datetime.now().strftime('%H:%M:%S')}")
 load_blacklist()
 
 for url in RSS_FEEDS:
 print(f"📡 Scanăm feed-ul: {url}")
 feed = feedparser.parse(url)
 
 if not hasattr(feed, 'entries'):
 print(f"⚠️ Feed-ul {url} este inaccesibil sau invalid.")
 continue

 for entry in feed.entries[:15]:
 # FIX CRITIC: Ignorăm obiectele care nu sunt dicționare (erorile tale anterioare)
 if not hasattr(entry, 'get') or isinstance(entry, str):
 continue

 titlu = getattr(entry, 'title', '')
 link = getattr(entry, 'link', '')
 
 if not link:
 continue

 h = hash_text(link)

 if not is_blacklisted(h):
 print(f"🔎 Știre nouă: {titlu[:50]}...")
 
 descriere = getattr(entry, 'description', '')
 res = await proceseaza_cu_ai(titlu, descriere)
 
 if res:
 # Formatare Premium cu Emoji și Hashtag
 postare_finala = (
 f"{res.get('emoji', '📰')} <b>{res.get('categorie', '#Diverse')}</b>\n\n"
 f"{res.get('text_ro', 'Fără traducere')}\n\n"
 f"🔗 <a href='{link}'>Sursa Originală</a>\n\n"
 f"{SEMNATURA}"
 )
 
 if await trimite_telegram(postare_finala):
 add_to_blacklist(h)
 print(f"✅ Postat și salvat în memorie!")
 await asyncio.sleep(2) # Pauză anti-spam
 else:
 # Doar un indicator vizual scurt pentru duplicate
 print(f"⏭️ Sărit (duplicat)")

 print(f"🏁 Rulare finalizată la {datetime.now().strftime('%H:%M:%S')}")

if __name__ == "__main__":
 asyncio.run(main())
