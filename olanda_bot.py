import os
import sys
# Fortam flush-ul logurilor pentru Render
sys.stdout.reconfigure(line_buffering=True)
import time
import hashlib
import json
import sqlite3
import requests
import feedparser
import re
import threading
from datetime import datetime
from typing import Optional, Dict, Any
from http.server import HTTPServer, BaseHTTPRequestHandler
from deep_translator import GoogleTranslator
from telegram import Update, ParseMode
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

# ==========================================
# CONFIGURAȚII (Asistent Șoferi NL - LIVE RADAR v4.1 Fallback)
# ==========================================
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CANAL_DESTINATIE = os.getenv("TELEGRAM_CHANNEL_ID")
PORT = int(os.getenv("PORT", 10000))

RSS_FEEDS = [
    "https://www.ttm.nl/feed/"
]

BLACKLIST_FILE = "processed_links_olanda.txt"
DB_PATH = "memorie_stiri_olanda.db"
BLACKLIST_SET = set()
SEMNATURA = "@real_live_by_luci"
VERIFY_INTERVAL = 60 

# ==========================================
# DUMMY WEB SERVER
# ==========================================
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Olanda Bot: Online si Monitorizeaza Traficul LIVE (Fallback Activ).")

def run_server():
    server = HTTPServer(('0.0.0.0', PORT), SimpleHandler)
    print(f"🌐 Dummy Server pornit pe portul {PORT}")
    server.serve_forever()

# ==========================================
# GESTIONARE MEMORIE
# ==========================================
def load_blacklist():
    global BLACKLIST_SET
    if os.path.exists(BLACKLIST_FILE):
        try:
            with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
                BLACKLIST_SET = set(line.strip() for line in f if line.strip())
        except: pass

def is_blacklisted(h: str) -> bool: return h in BLACKLIST_SET

def add_to_blacklist(h: str):
    if h not in BLACKLIST_SET:
        BLACKLIST_SET.add(h)
        try:
            with open(BLACKLIST_FILE, "a", encoding="utf-8") as f: f.write(h + "\n")
        except: pass

def hash_text(text: str) -> str: return hashlib.md5(text.encode("utf-8")).hexdigest()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS stiri_recente (id INTEGER PRIMARY KEY AUTOINCREMENT, text_rezumat TEXT, data_postare TEXT)''')
    conn.commit(); conn.close()

def preia_stiri_vechi(limita=15):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try: return [row[0] for row in c.execute('SELECT text_rezumat FROM stiri_recente ORDER BY id DESC LIMIT ?', (limita,)).fetchall()]
    except: return []
    finally: conn.close()

def salveaza_stire_in_memorie(text_rezumat):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute('INSERT INTO stiri_recente (text_rezumat, data_postare) VALUES (?, ?)', (text_rezumat, datetime.now().isoformat()))
        c.execute('DELETE FROM stiri_recente WHERE id NOT IN (SELECT id FROM stiri_recente ORDER BY id DESC LIMIT 50)')
        conn.commit()
    except: pass
    finally: conn.close()

# ==========================================
# UTILITARE AI
# ==========================================
def clean_json_response(text: str) -> str:
    return re.sub(r"```$", "", re.sub(r"^```json\s*", "", text).strip()).strip()

def proceseaza_stire_ai(titlu: str, descriere: str, texte_vechi: list, sursa_tip: str = "RSS") -> Optional[Dict[str, Any]]:
    if not DEEPSEEK_KEY: return None
    context_vechi = "\n".join([f"- {t}" for t in texte_vechi]) if texte_vechi else "Nicio stire recenta."
    
    if sursa_tip == "TRAFIC_LIVE":
        prompt = f"""Ești asistent pentru șoferi profesioniști în Olanda. Ai o alertă LIVE.
Tradu și formatează clar în română. 
DEDUPLICARE: Daca e același eveniment din ȘTIRI VECHI, pune "duplicat": true.
Categorii: #Trafic_Drumuri.
Emoji: 🚧, ⛔, 🚗, ⚠️.

ȘTIRI VECHI:
{context_vechi}

DATE TRAFIC LIVE:
{titlu}
{descriere}

Răspunde STRICT JSON:
{{"categorie": "#Trafic_Drumuri", "emoji": "⛔", "text_ro": "Rezumat alertă...", "duplicat": false}}"""
    else:
        prompt = f"""Ești asistent pentru șoferi profesioniști în Olanda. Analizează ȘTIREA NOUĂ. 
1. Dacă NU are legătură cu Olanda/transport, pune "IGNORE".
2. DEDUPLICARE: Compară cu ȘTIRILE VECHI. Dacă e duplicat semantic, pune "duplicat": true.
3. Dacă e relevantă, tradu/rezumă în română.
Categorii: #Legislatie_Taxe, #Sindicate_CAO, #Economie_Logistica, IGNORE.

ȘTIRI VECHI:
{context_vechi}

ȘTIRE NOUĂ:
Titlu: {titlu.replace('"', "'")[:200]}
Descriere: {(descriere or "Fără descriere").replace('"', "'")[:600]}

Răspunde STRICT JSON:
{{"categorie": "#Economie_Logistica", "emoji": "💶", "text_ro": "Rezumat...", "duplicat": false}}"""

    headers = {"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"}
    try:
        # TIMEOUT STRICT: 10 SECUNDE. Daca pica, intram in Fallback Mode.
        resp = requests.post("https://api.deepseek.com/v1/chat/completions",
            json={"model": "deepseek-chat", "messages": [{"role": "user", "content": prompt}], "temperature": 0.1, "response_format": {"type": "json_object"}},
            headers=headers, timeout=10)
        resp.raise_for_status()
        return json.loads(clean_json_response(resp.json()["choices"][0]["message"]["content"]))
    except Exception as e: 
        print(f"⚠️ Eroare/Timeout AI ({sursa_tip}): {e}")
        return None

# ==========================================
# ==========================================
# TELEGRAM
# ==========================================
def trimite_telegram_cu_audio(text_html: str, text_audio: str) -> bool:
    if not TELEGRAM_TOKEN or not CANAL_DESTINATIE: return False
    
    # Trimitem intai mesajul text
    url_text = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url_text, json={"chat_id": CANAL_DESTINATIE, "text": text_html[:3997], "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=15)
        if r.status_code != 200:
            print(f"❌ Eroare Text Telegram ({r.status_code}): {r.text}")
            return False
            
        # Generare si trimitere Audio
        from gtts import gTTS
        import tempfile
        
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_audio:
            tts = gTTS(text=text_audio[:2000], lang='ro')
            tts.save(tmp_audio.name)
            
            url_audio = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendAudio"
            with open(tmp_audio.name, 'rb') as audio_file:
                files = {'audio': audio_file}
                data = {'chat_id': CANAL_DESTINATIE, 'caption': "🎧 Versiunea Audio"}
                r_audio = requests.post(url_audio, data=data, files=files, timeout=20)
                
        # Curatenie fisier temporar
        try: os.remove(tmp_audio.name)
        except: pass
        
        return True
    except Exception as e:
        print(f"❌ Exceptie trimitere Telegram: {e}")
        return False

def trimite_telegram(text_final: str) -> bool:
    if not TELEGRAM_TOKEN or not CANAL_DESTINATIE: return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CANAL_DESTINATIE, "text": text_final[:3997], "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=15)
        if r.status_code != 200:
            print(f"❌ Eroare API Telegram ({r.status_code}): {r.text}")
            return False
        return True
    except Exception as e:
        print(f"❌ Exceptie trimitere Telegram: {e}")
        return False
# ==========================================
# RADAR TRAFIC LIVE (API RIJKSWATERSTAAT) - TOATE INFORMATIILE
# ==========================================
def preia_trafic_live():
    alerte = []
    url = "https://api.rwsverkeersinfo.nl/api/traffic/"
    try:
        data = requests.get(url, timeout=15).json()
        for obs in data.get('obstructions', []):
            alerte.append(obs) # Preluam TOT (radare, accidente, inchideri)
    except Exception as e:
        print(f"⚠️ Eroare API Rijkswaterstaat: {e}")
    return alerte

# ==========================================
# SCRAPER FNV NIEUWS
# ==========================================
def preia_stiri_fnv():
    stiri = []
    url = "https://www.fnv.nl/over-de-fnv/nieuws"
    try:
        from bs4 import BeautifulSoup
        resp = requests.get(url, timeout=15)
        soup = BeautifulSoup(resp.text, 'html.parser')
        # Găsește containerele cu știri
        items = soup.find_all('a', class_=re.compile(r'nieuwsoverzicht__item'))
        for item in items[:5]: # luăm ultimele 5 știri ca să nu spamăm la început
            link = item.get('href', '')
            if not link.startswith('http'):
                link = "https://www.fnv.nl" + link
            
            titlu_tag = item.find('h3', class_='nieuwsoverzicht__item-title')
            titlu = titlu_tag.text.strip() if titlu_tag else ""
            
            desc_tag = item.find('div', class_='nieuwsoverzicht__item-content')
            descriere = desc_tag.text.strip() if desc_tag else "Noutate sindicală FNV."
            
            if titlu and link:
                stiri.append({"title": titlu, "link": link, "description": descriere})
    except Exception as e:
        print(f"⚠️ Eroare scraper FNV: {e}")
    return stiri

# ==========================================
# WORKER LOOP
# ==========================================
def worker_loop():
    print(f"🚀 Pornire Radar Live & Știri NL v4.1 (FALLBACK ACTIV): {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if not all([DEEPSEEK_KEY, TELEGRAM_TOKEN, CANAL_DESTINATIE]): 
        print("❌ EROARE CRITICĂ: Configurație incompletă!")
        print(f"DEEPSEEK_API_KEY prezent: {'DA' if DEEPSEEK_KEY else 'NU'}")
        print(f"TELEGRAM_BOT_TOKEN prezent: {'DA' if TELEGRAM_TOKEN else 'NU'}")
        print(f"TELEGRAM_CHANNEL_ID prezent: {'DA' if CANAL_DESTINATIE else 'NU'}")
        return

    init_db()
    load_blacklist()
    
    while True:
        try:
            print(f"🔄 Verificare Radar Trafic la {datetime.now().strftime('%H:%M:%S')}...")
            stiri_vechi_db = preia_stiri_vechi(15) 
        
            # 1. SCANARE TRAFIC LIVE (CU GOOGLE TRANSLATOR, TOT CONTINUTUL)
            alerte_live = preia_trafic_live()
            for obs in alerte_live:
                obs_id_raw = obs.get('obstructionId', '')
                t_titlu_nl = obs.get('title', 'Alerte')
                t_desc_nl = obs.get('description', '')
                
                # Protectie NULL ptr cifre
                delay = float(obs.get('delay') or 0.0)
                length = (obs.get('length') or 0.0) / 1000.0
                
                # Hashem delay-ul (rotunjit) ca sa primesti UPDATE-uri in timp real la trafic
                # Ex: Daca o coada trece de la 5 la 10 minute, ID-ul se schimba putin si primesti noul mesaj.
                obs_hash = f"RWS_{obs_id_raw}_{int(delay//10)}_{int(length//3)}" 
                if is_blacklisted(obs_hash): continue
            
                # Hashtag pentru CAUTARE TELEGRAM (ex: #A2, #N3)
                road_raw = str(obs.get('roadNumber', '')).strip()
                road_hashtag = f"#{road_raw.replace(' ', '')}" if road_raw else "#TraseuNecunoscut"
                
                direction = obs.get('directionText', '-')
                locatie = obs.get('locationText', '-')
                
                timp_info = ""
                if not obs.get('isCurrent', True): timp_info = f"\n📅 <b>Planificat:</b> {obs.get('timeStart', '')} - {obs.get('timeEnd', '')}"

                # Bypass DeepSeek -> Google Translate Direct
                try:
                    t_titlu_ro = GoogleTranslator(source='nl', target='ro').translate(t_titlu_nl)
                    t_desc_ro = GoogleTranslator(source='nl', target='ro').translate(t_desc_nl)
                except Exception as e:
                    print(f"⚠️ Eroare Google Translate: {e}")
                    t_titlu_ro = t_titlu_nl
                    t_desc_ro = t_desc_nl

                # Determinare Emoji Inteligent dupa textul Raw olandez
                raw_text_lower = f"{t_titlu_nl} {t_desc_nl}".lower()
                emoji = "⚠️"
                categorie = "#Alerte_Trafic"
                
                if any(kw in raw_text_lower for kw in ['ongeval', 'ongeluk', 'botsing']): 
                    emoji = "💥"
                    categorie = "#Accident"
                elif any(kw in raw_text_lower for kw in ['flitser', 'camera', 'snelheid', 'controle']): 
                    emoji = "📸"
                    categorie = "#Radar_Camera"
                elif any(kw in raw_text_lower for kw in ['werkzaamheden', 'onderhoud', 'afgesloten', 'dicht', 'afsluiting', 'dicht']): 
                    emoji = "🚧"
                    categorie = "#Lucrari_Drumuri"
                elif any(kw in raw_text_lower for kw in ['langzaam', 'stilstaand', 'file', 'verkeer', 'vertraging']): 
                    emoji = "🚗"
                    categorie = "#Aglomeratie"
                elif any(kw in raw_text_lower for kw in ['brug', 'open']): 
                    emoji = "🌉"
                    categorie = "#Pod_Deschis"

                # Constructia Mesajului
                postare = f"{emoji} <b>{categorie} {road_hashtag}</b>\n\n"
                postare += f"🚨 <b>{t_titlu_ro.upper()}</b>\n"
                if locatie and locatie != '-': postare += f"📍 <b>Locație:</b> {locatie}\n"
                if direction and direction != '-': postare += f"🧭 <b>Direcție:</b> {direction}\n"
                
                if delay > 0: postare += f"⏳ <b>Întârziere:</b> {int(delay)} minute\n"
                if length > 0: postare += f"📏 <b>Lungime coadă:</b> {length:.1f} km\n"
                
                postare += f"\n📝 <b>Detalii:</b> {t_desc_ro}"
                if timp_info: postare += f"\n{timp_info}"
                
                postare += f"\n\n📍 <i>Rijkswaterstaat LIVE Radar</i>\n<i>{SEMNATURA}</i>"

                if trimite_telegram(postare):
                    add_to_blacklist(obs_hash)
                    salveaza_stire_in_memorie(f"{categorie} {road_hashtag}: {t_titlu_ro}")
                    print(f"   ✅ [TRAFIC LIVE GT] Alerta trimisa pe {road_raw}")
                    time.sleep(2)
                
                time.sleep(1)
        
            # 2. SCANARE STIRI RSS (Astea nu au fallback, daca pica AI-ul, pur si simplu asteapta ciclul urmator)
            for url in RSS_FEEDS:
                try: feed = feedparser.parse(url)
                except: continue

                if not hasattr(feed, "entries"): continue

                for entry in feed.entries[:3]: 
                    titlu = getattr(entry, "title", None)
                    link = getattr(entry, "link", None)
                    if not link or not titlu: continue

                    h = hash_text(link)
                    if is_blacklisted(h): continue 

                    descriere = getattr(entry, "description", "") or getattr(entry, "summary", "")
                    res = proceseaza_stire_ai(titlu, descriere, stiri_vechi_db, sursa_tip="RSS")

                    if res:
                        if res.get("categorie") == "IGNORE" or res.get("duplicat", False):
                            add_to_blacklist(h)
                        else:
                            text_rezumat = res.get('text_ro', 'Fara text')
                            postare = f"{res.get('emoji', '📌')} <b>{res.get('categorie')}</b>\n\n{text_rezumat}\n\n🔗 <a href='{link}'>Sursa Originală</a>\n\n<i>{SEMNATURA}</i>"
                            
                            # Generam audio pentru Stiri RSS
                            text_audio = f"Știre nouă despre {res.get('categorie').replace('#', '')}. {text_rezumat}"
                        
                            if trimite_telegram_cu_audio(postare, text_audio):
                                add_to_blacklist(h)
                                salveaza_stire_in_memorie(text_rezumat) 
                                stiri_vechi_db.insert(0, text_rezumat)  
                                print(f"   ✅ [STIRE AUDIO] Postat: {titlu[:30]}...")
                                time.sleep(2)
                    time.sleep(1)
        
            # 3. SCANARE FNV SCRAPER
            stiri_fnv = preia_stiri_fnv()
            for stire in stiri_fnv:
                titlu = stire.get("title")
                link = stire.get("link")
                if not link or not titlu: continue

                h = hash_text(link)
                if is_blacklisted(h): continue 

                descriere = stire.get("description", "Noutate FNV.")
                res = proceseaza_stire_ai(titlu, descriere, stiri_vechi_db, sursa_tip="RSS")

                if res:
                    if res.get("categorie") == "IGNORE" or res.get("duplicat", False):
                        add_to_blacklist(h)
                    else:
                        text_rezumat = res.get('text_ro', 'Fara text')
                        postare = f"{res.get('emoji', '📌')} <b>{res.get('categorie')}</b>\n\n{text_rezumat}\n\n🔗 <a href='{link}'>Sursa FNV</a>\n\n<i>{SEMNATURA}</i>"
                    
                        # Generam audio pentru Stiri FNV
                        text_audio = f"Noutate sindicală FNV din categoria {res.get('categorie').replace('#', '')}. {text_rezumat}"
                        
                        if trimite_telegram_cu_audio(postare, text_audio):
                            add_to_blacklist(h)
                            salveaza_stire_in_memorie(text_rezumat) 
                            stiri_vechi_db.insert(0, text_rezumat)  
                            print(f"   ✅ [STIRE AUDIO FNV] Postat: {titlu[:30]}...")
                            time.sleep(2)
                time.sleep(1)
        
            time.sleep(VERIFY_INTERVAL)

        except Exception as _e:
            print(f"❌ EROARE CRITICĂ ÎN WORKER: {_e}")
            import traceback
            traceback.print_exc()
            time.sleep(10)

# ==========================================
# BOT INTERACTIV TELEGRAM (COMENZI ON-DEMAND)
# ==========================================
def comanda_drum(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text("⚠️ Folosire corectă: /drum A2\nIntrodu numărul drumului pentru a verifica traficul pe el.")
        return

    road_requested = str(context.args[0]).upper().replace(" ", "")
    update.message.reply_text(f"🔍 Caut informații live pentru drumul {road_requested}...")

    alerte_live = preia_trafic_live()
    alerte_gasite = []

    for obs in alerte_live:
        road_raw = str(obs.get('roadNumber', '')).strip().upper().replace(" ", "")
        if road_requested == road_raw:
            t_titlu_nl = obs.get('title', 'Alerte')
            delay = float(obs.get('delay') or 0.0)
            length = (obs.get('length') or 0.0) / 1000.0
            
            try: t_titlu_ro = GoogleTranslator(source='nl', target='ro').translate(t_titlu_nl)
            except: t_titlu_ro = t_titlu_nl
            
            detalii = f"🚨 <b>{t_titlu_ro}</b>"
            if delay > 0: detalii += f" | ⏳ Întârziere: {int(delay)} min"
            if length > 0: detalii += f" | 📏 Coadă: {length:.1f} km"
            alerte_gasite.append(detalii)

    if not alerte_gasite:
        update.message.reply_text(f"✅ Drum liber! Nu există alerte, accidente sau radare raportate de Rijkswaterstaat pe drumul {road_requested} în acest moment.")
    else:
        rezultat = f"🚧 <b>Situația LIVE pe drumul {road_requested}:</b>\n\n" + "\n\n".join(alerte_gasite)
        update.message.reply_text(rezultat, parse_mode=ParseMode.HTML)

def run_telegram_bot():
    if not TELEGRAM_TOKEN:
        print("❌ Nu pot porni modulul interactiv: TELEGRAM_BOT_TOKEN lipsește!")
        return
    try:
        updater = Updater(TELEGRAM_TOKEN, use_context=True)
        dp = updater.dispatcher
        dp.add_handler(CommandHandler("drum", comanda_drum))
        print("🤖 Modulul Interactiv (Comenzi Telegram) a pornit!")
        updater.start_polling()
        # Nu apelam updater.idle() pentru ca ruleaza intr-un thread separat fata de serverul web.
    except Exception as e:
        print(f"⚠️ Eroare la pornirea modulului interactiv Telegram: {e}")

if __name__ == "__main__":
    # 1. Thread pentru Worker Loop (Scraping Automat)
    worker_thread = threading.Thread(target=worker_loop, daemon=True)
    worker_thread.start()
    
    # 2. Thread pentru Botul Interactiv (Raspunsuri On-Demand)
    telegram_thread = threading.Thread(target=run_telegram_bot, daemon=True)
    telegram_thread.start()
    
    # 3. Server Web (Main thread, pt Render)
    run_server()
