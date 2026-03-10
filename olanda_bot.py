import os
import sqlite3
import hashlib
import json
import requests
import feedparser
import asyncio
from datetime import datetime

# --- CONFIGURARE ---
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY")

DB_PATH = "stiri_olanda.db"

RSS_FEEDS = [
    "https://feeds.nos.nl/nosnieuwsalgemeen",
    "https://feeds.nos.nl/noseconomie",
    "https://www.nu.nl/rss/Algemeen",
    "https://www.nu.nl/rss/Economie",
    "https://www.rijksoverheid.nl/actueel/nieuws/rss", # Decizii guvernamentale
    "https://www.transport-online.nl/site/rss/", # Transporturi auto, soferi, CAO
    "https://www.ttm.nl/feed/", # Transport, legislatie
    "https://www.anwb.nl/feeds/verkeer/fileberichten" # ANWB trafic live
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

def genereaza_imagine(titlu_stire):
    try:
        # Prompt simplu si sigur pentru generare
        prompt_encoded = requests.utils.quote(f"news illustration for: {titlu_stire[:100]}, professional photography, realistic")
        resp = requests.get(f"https://image.pollinations.ai/prompt/{prompt_encoded}?width=800&height=450&nologo=true", timeout=20)
        if resp.status_code == 200:
            return resp.content
    except Exception as e:
        print(f"Eroare generare imagine: {e}")
    return None

async def genereaza_audio(text_de_citit, filepath):
    try:
        import edge_tts
        # Vocea 'ro-RO-EmilNeural' (voce de baiat, clara)
        communicate = edge_tts.Communicate(text_de_citit, "ro-RO-EmilNeural")
        await communicate.save(filepath)
        return True
    except Exception as e:
        print(f"Eroare generare audio: {e}")
        return False

def trimite_pe_telegram(text, image_bytes=None, audio_path=None):
    if not BOT_TOKEN or not CHANNEL_ID:
        print("Lipsesc variabilele de Telegram.")
        return
    
    if image_bytes:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        data = {
            "chat_id": CHANNEL_ID,
            "caption": text,
            "parse_mode": "HTML"
        }
        files = {
            "photo": ("image.jpg", image_bytes, "image/jpeg")
        }
        resp = requests.post(url, data=data, files=files)
        
        # Daca avem audio, il trimitem ca un Reply (sau Voice Message separat) la aceeasi stire
        if audio_path and os.path.exists(audio_path) and resp.status_code == 200:
            msg_id = resp.json().get('result', {}).get('message_id')
            url_audio = f"https://api.telegram.org/bot{BOT_TOKEN}/sendVoice"
            with open(audio_path, 'rb') as f:
                requests.post(url_audio, data={"chat_id": CHANNEL_ID, "reply_to_message_id": msg_id}, files={"voice": f})

    else:
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
Ești un asistent AI pentru un șofer profesionist de TIR în Olanda (contract D6, CAO).
Analizează următorul lot de știri din presa olandeză, guvern și sectorul de transporturi.
REGULI:
1. ELIMINĂ DOAR știrile despre mondenități, cancan, showbiz, bârfe sau sport minor.
2. PĂSTREAZĂ ABSOLUT TOATE CELELALTE ȘTIRI valabile din listă (nu există limită de număr).
3. TRADUCE și RESCRIE fiecare știre selectată în limba română (stil clar, informativ, maxim 3 paragrafe).
4. EVIDENȚIAZĂ/PUNE ACCENT (dacă e cazul) pe: drumuri închise, accidente grave, taxe, decizii economice/guvernamentale, noutăți pentru expați, legislație CAO și forță de muncă în transporturi.

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

async def main_async():
    init_db()
    stiri_noi = []
    
    # 1. Scraping RSS
    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            # Pentru ANWB luăm mai multe, pentru restul 15
            limit = 50 if "anwb.nl" in feed_url else 15
            for entry in feed.entries[:limit]:
                link_hash = hash_text(entry.link)
                if not stire_existenta(link_hash):
                    stiri_noi.append({
                        "hash": link_hash,
                        "title": entry.title,
                        "description": getattr(entry, 'description', ''),
                        "link": entry.link,
                        "source": feed_url
                    })
        except Exception as e:
            print(f"Eroare la parsarea feed-ului {feed_url}: {e}")

    print(f"S-au găsit {len(stiri_noi)} știri noi neprocesate.")
    if not stiri_noi:
        return

    # Impartim in calupuri de max 20 stiri pentru a nu depasi limitele de tokeni per request la DeepSeek
    chunk_size = 20
    for i in range(0, len(stiri_noi), chunk_size):
        chunk = stiri_noi[i:i + chunk_size]
        print(f"Procesăm chunk de la {i} la {i+len(chunk)}")
        
        rezultate = evalueaza_si_traduce_batch(chunk)
        
        # 3. Postare și Salvare
        for rez in rezultate:
            idx = rez.get('idx_original')
            if idx is not None and 0 <= idx < len(chunk):
                stire_bruta = chunk[idx]
                titlu_ro = rez.get('titlu_ro', '')
                rezumat_ro = rez.get('rezumat_ro', '')
                
                # Format mesaj Telegram
                mesaj = f"🇳🇱 <b>{titlu_ro}</b>\n\n{rezumat_ro}\n\n<a href='{stire_bruta['link']}'>Sursa originală</a>"
                
                # Generam o imagine relevanta inainte de trimitere
                image_bytes = genereaza_imagine(titlu_ro)
                
                # Generare Audio cu TTS
                audio_path = f"audio_{stire_bruta['hash']}.mp3"
                text_pt_audio = f"{titlu_ro}. {rezumat_ro}"
                await genereaza_audio(text_pt_audio, audio_path)
                
                trimite_pe_telegram(mesaj, image_bytes, audio_path)
                
                # Cleanup audio
                if os.path.exists(audio_path):
                    os.remove(audio_path)
                
                salveaza_stire(stire_bruta['hash'], stire_bruta['title'], stire_bruta['source'])

        # Salvăm și restul știrilor din chunk ca "procesate" ca să nu le mai trimitem la AI tura viitoare
        for stire in chunk:
            if not stire_existenta(stire['hash']):
                salveaza_stire(stire['hash'], stire['title'], stire['source'])

if __name__ == "__main__":
    asyncio.run(main_async())
