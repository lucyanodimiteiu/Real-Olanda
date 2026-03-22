import os
import sys
sys.stdout.reconfigure(line_buffering=True)
import time
import hashlib
import json
import sqlite3
import requests
import feedparser
import re
import threading
import asyncio
import gzip
import io
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional, Dict, Any, List
from http.server import HTTPServer, BaseHTTPRequestHandler
from deep_translator import GoogleTranslator

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ==========================================
# CONFIGURAȚII
# ==========================================
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CANAL_DESTINATIE = os.getenv("TELEGRAM_CHANNEL_ID")
PORT = int(os.getenv("PORT", 10000))

RSS_FEEDS = ["https://www.ttm.nl/feed/"]

BLACKLIST_FILE = "processed_links_olanda.txt"
DB_PATH = "memorie_stiri_olanda.db"
BLACKLIST_SET = set()
SEMNATURA = "@real_live_by_luci"
VERIFY_INTERVAL = 60

NS = {"d2": "http://datex2.eu/schema/2/2_0"}

# Locatii cunoscute bazate pe coordonate AlertC (specificLocation code -> nume)
# Vom folosi un reverse lookup simplu bazat pe coordonate GPS
LOCATIE_CACHE = {}

# ==========================================
# DUMMY WEB SERVER
# ==========================================
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Olanda Bot v7.0: Online si Monitorizeaza Traficul LIVE.")
    def log_message(self, format, *args):
        pass

def run_server():
    server = HTTPServer(('0.0.0.0', PORT), SimpleHandler)
    print(f"🌐 Dummy Server pornit pe portul {PORT}")
    server.serve_forever()

# ==========================================
# GESTIONARE MEMORIE / BLACKLIST
# ==========================================
def load_blacklist():
    global BLACKLIST_SET
    if os.path.exists(BLACKLIST_FILE):
        try:
            with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
                BLACKLIST_SET = set(line.strip() for line in f if line.strip())
        except:
            pass

def is_blacklisted(h: str) -> bool:
    return h in BLACKLIST_SET

def add_to_blacklist(h: str):
    if h not in BLACKLIST_SET:
        BLACKLIST_SET.add(h)
        try:
            with open(BLACKLIST_FILE, "a", encoding="utf-8") as f:
                f.write(h + "\n")
        except:
            pass

def hash_text(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS stiri_recente
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  text_rezumat TEXT,
                  data_postare TEXT)''')
    conn.commit()
    conn.close()

def preia_stiri_vechi(limita=15):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        return [row[0] for row in c.execute(
            'SELECT text_rezumat FROM stiri_recente ORDER BY id DESC LIMIT ?',
            (limita,)
        ).fetchall()]
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
        c.execute('DELETE FROM stiri_recente WHERE id NOT IN '
                  '(SELECT id FROM stiri_recente ORDER BY id DESC LIMIT 50)')
        conn.commit()
    except:
        pass
    finally:
        conn.close()

# ==========================================
# REVERSE GEOCODING - obtine numele locatiei din coordonate
# ==========================================
def get_locatie_din_coordonate(lat: str, lon: str) -> str:
    if not lat or not lon:
        return ""
    cache_key = f"{lat:.4f},{lon:.4f}" if isinstance(lat, float) else f"{lat},{lon}"
    if cache_key in LOCATIE_CACHE:
        return LOCATIE_CACHE[cache_key]
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json&zoom=12&accept-language=ro"
        resp = requests.get(url, headers={"User-Agent": "OlandaBot/7.0"}, timeout=5)
        data = resp.json()
        addr = data.get("address", {})
        # Construim locatia din componente relevante
        parts = []
        road = addr.get("road", "") or addr.get("motorway", "")
        city = addr.get("city", "") or addr.get("town", "") or addr.get("village", "") or addr.get("municipality", "")
        if road:
            parts.append(road)
        if city:
            parts.append(city)
        locatie = ", ".join(parts) if parts else addr.get("display_name", "")[:60]
        LOCATIE_CACHE[cache_key] = locatie
        return locatie
    except:
        return ""

# ==========================================
# UTILITARE AI (DeepSeek pentru stiri RSS/FNV)
# ==========================================
def clean_json_response(text: str) -> str:
    return re.sub(r"```$", "", re.sub(r"^```json\s*", "", text).strip()).strip()

def proceseaza_stire_ai(titlu: str, descriere: str, texte_vechi: list,
                         sursa_tip: str = "RSS") -> Optional[Dict[str, Any]]:
    if not DEEPSEEK_KEY:
        return None
    context_vechi = "\n".join([f"- {t}" for t in texte_vechi]) if texte_vechi else "Nicio stire recenta."
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
        resp = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            json={"model": "deepseek-chat", "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.1, "response_format": {"type": "json_object"}},
            headers=headers, timeout=10)
        resp.raise_for_status()
        return json.loads(clean_json_response(resp.json()["choices"][0]["message"]["content"]))
    except Exception as e:
        print(f"⚠️ Eroare/Timeout AI ({sursa_tip}): {e}")
        return None

# ==========================================
# TELEGRAM - TRIMITERE MESAJE
# ==========================================
def trimite_telegram(text_final: str, chat_id: str = None) -> bool:
    if not TELEGRAM_TOKEN:
        return False
    destinatie = chat_id or CANAL_DESTINATIE
    if not destinatie:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": destinatie,
            "text": text_final[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=15)
        if r.status_code != 200:
            print(f"❌ Eroare API Telegram ({r.status_code}): {r.text[:200]}")
            return False
        return True
    except Exception as e:
        print(f"❌ Exceptie trimitere Telegram: {e}")
        return False

def trimite_telegram_cu_audio(text_html: str, text_audio: str) -> bool:
    if not trimite_telegram(text_html):
        return False
    try:
        from gtts import gTTS
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_audio:
            tts = gTTS(text=text_audio[:2000], lang='ro')
            tts.save(tmp_audio.name)
            url_audio = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendAudio"
            with open(tmp_audio.name, 'rb') as audio_file:
                requests.post(url_audio, data={'chat_id': CANAL_DESTINATIE, 'caption': "🎧 Versiunea Audio"},
                              files={'audio': audio_file}, timeout=20)
        try:
            os.remove(tmp_audio.name)
        except:
            pass
    except Exception as e:
        print(f"⚠️ Eroare audio: {e}")
    return True

# ==========================================
# API DATEX II - RIJKSWATERSTAAT
# ==========================================
def preia_trafic_live() -> List[Dict]:
    alerte = []
    url = "https://opendata.ndw.nu/actuele_statusberichten.xml.gz"
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        with gzip.GzipFile(fileobj=io.BytesIO(resp.content)) as gz:
            xml_data = gz.read()
        root = ET.fromstring(xml_data)

        for situation in root.findall(".//d2:situation", NS):
            sit_id = situation.get("id", "")

            for record in situation.findall(".//d2:situationRecord", NS):
                rec_type = record.get("{http://www.w3.org/2001/XMLSchema-instance}type", "")
                rec_id = record.get("id", "")
                creation_time = record.findtext("d2:situationRecordCreationTime", default="", namespaces=NS)
                cauza_nl = record.findtext(".//d2:causeDescription/d2:values/d2:value", default="", namespaces=NS)
                cauza_type = record.findtext(".//d2:causeType", default="other", namespaces=NS)

                delay_val = record.findtext(".//d2:delayTimeValue", default="0", namespaces=NS)
                try:
                    delay_minutes = float(delay_val) / 60.0
                except:
                    delay_minutes = 0.0

                ql = record.findtext("d2:queueLength", default="0", namespaces=NS)
                try:
                    queue_length_km = int(ql) / 1000.0
                except:
                    queue_length_km = 0.0

                abnormal_type = record.findtext("d2:abnormalTrafficType", default="", namespaces=NS)
                management_type = record.findtext("d2:roadOrCarriagewayOrLaneManagementType", default="", namespaces=NS)
                comment_nl = record.findtext(".//d2:generalPublicComment/d2:comment/d2:values/d2:value", default="", namespaces=NS)
                lat = record.findtext(".//d2:locationForDisplay/d2:latitude", default="", namespaces=NS)
                lon = record.findtext(".//d2:locationForDisplay/d2:longitude", default="", namespaces=NS)

                direction_coded = record.findtext(".//d2:alertCDirectionCoded", default="", namespaces=NS)
                direction_text = "pozitiv" if direction_coded == "positive" else "negativ" if direction_coded == "negative" else ""

                carriageway = record.findtext(".//d2:carriageway", default="", namespaces=NS)
                lane = record.findtext(".//d2:lane", default="", namespaces=NS)
                time_start = record.findtext(".//d2:overallStartTime", default="", namespaces=NS)
                time_end = record.findtext(".//d2:overallEndTime", default="", namespaces=NS)

                # Extrage numarul drumului din situationId si recordId
                # Format tipic: NLRWS_A2_... sau NL_A12_... sau RWS02_N11_...
                road_number = ""
                for sursa in [sit_id, rec_id, comment_nl or ""]:
                    # Cauta pattern de drum: A2, A12, N11, N14, B1 etc.
                    match = re.search(r'(?<![A-Z])([AENB]\d{1,3})(?![0-9A-Z])', sursa, re.IGNORECASE)
                    if match:
                        road_number = match.group(1).upper()
                        break

                # Obtine locatia din coordonate GPS (cu cache)
                locatie_gps = ""
                if lat and lon:
                    locatie_gps = get_locatie_din_coordonate(lat, lon)

                alerte.append({
                    "situationId": sit_id,
                    "recordId": rec_id,
                    "recordType": rec_type,
                    "cauza_nl": cauza_nl,
                    "cauza_type": cauza_type,
                    "delay_minutes": delay_minutes,
                    "queue_length_km": queue_length_km,
                    "abnormal_type": abnormal_type,
                    "management_type": management_type,
                    "comment_nl": comment_nl,
                    "latitude": lat,
                    "longitude": lon,
                    "direction": direction_text,
                    "carriageway": carriageway,
                    "lane": lane,
                    "time_start": time_start,
                    "time_end": time_end,
                    "creation_time": creation_time,
                    "road_number": road_number,
                    "locatie_gps": locatie_gps,
                })

        print(f"   📡 DATEX II: {len(alerte)} înregistrări preluate")

    except Exception as e:
        print(f"⚠️ Eroare API DATEX II: {e}")

    return alerte

# ==========================================
# DETERMINA EMOJI SI CATEGORIE
# ==========================================
def determina_emoji_si_categorie(alerta: Dict) -> tuple:
    rec_type = alerta.get("recordType", "").lower()
    cauza_nl = alerta.get("cauza_nl", "").lower()
    cauza_type = alerta.get("cauza_type", "").lower()
    abnormal = alerta.get("abnormal_type", "").lower()
    mgmt = alerta.get("management_type", "").lower()
    comment = alerta.get("comment_nl", "").lower()
    combined = f"{rec_type} {cauza_nl} {cauza_type} {abnormal} {mgmt} {comment}"

    if cauza_type == "accident" or any(kw in combined for kw in ["ongeval", "ongeluk", "botsing", "accident"]):
        return "💥", "#Accident"
    elif any(kw in combined for kw in ["flitser", "camera", "snelheid", "snelheidscontrole"]):
        return "📸", "#Radar_Camera"
    elif any(kw in combined for kw in ["grenscontrole", "grens"]):
        return "🛂", "#Control_Granita"
    elif any(kw in combined for kw in ["werkzaamheden", "onderhoud", "spoedreparatie", "wegwerkzaamheden"]):
        return "🚧", "#Lucrari_Drumuri"
    elif any(kw in combined for kw in ["wegdek", "slechte toestand"]):
        return "⚠️", "#Drum_Deteriorat"
    elif any(kw in combined for kw in ["carriagewayClosures", "laneClosures", "afgesloten"]):
        return "⛔", "#Banda_Inchisa"
    elif any(kw in combined for kw in ["stilstaand", "stationarytraffic"]):
        return "🛑", "#Trafic_Stationar"
    elif any(kw in combined for kw in ["langzaam", "slowtraffic", "queuingtraffic", "file"]):
        return "🚗", "#Trafic_Lent"
    elif any(kw in combined for kw in ["brug", "open"]):
        return "🌉", "#Pod_Deschis"
    elif "emergencyvehicle" in combined or "weginspecteur" in comment:
        return "🚨", "#Vehicul_Urgenta"
    else:
        return "⚠️", "#Alerta_Trafic"

# ==========================================
# CONSTRUIESTE MESAJ
# ==========================================
def construieste_mesaj_alerta(alerta: Dict, road_tag: str = "") -> str:
    emoji, categorie = determina_emoji_si_categorie(alerta)

    cauza_nl = alerta.get("cauza_nl", "")
    comment_nl = alerta.get("comment_nl", "")
    road_number = alerta.get("road_number", "")
    locatie_gps = alerta.get("locatie_gps", "")
    direction = alerta.get("direction", "")
    carriageway = alerta.get("carriageway", "")

    # Traducere cauza
    try:
        cauza_ro = GoogleTranslator(source='nl', target='ro').translate(cauza_nl) if cauza_nl else ""
    except:
        cauza_ro = cauza_nl

    # Traducere comentariu
    try:
        comment_ro = GoogleTranslator(source='nl', target='ro').translate(comment_nl) if comment_nl else ""
    except:
        comment_ro = comment_nl

    # Traducere carosabil
    cw_map = {
        "mainCarriageway": "carosabil principal",
        "parallelCarriageway": "banda paralela",
        "entrySlipRoad": "banda de intrare",
        "exitSlipRoad": "banda de iesire",
        "slipRoads": "rampa",
        "rightLane": "banda dreapta",
        "leftLane": "banda stanga",
    }
    cw_ro = cw_map.get(carriageway, "")

    lane = alerta.get("lane", "")
    lane_map = {"rightLane": "banda dreapta", "leftLane": "banda stanga", "middleLane": "banda mijloc"}
    lane_ro = lane_map.get(lane, "")

    # Hashtag
    hashtag = road_tag if road_tag else (f"#{road_number}" if road_number else "")

    # Antet
    postare = f"⛔ <b>#Trafic_Drumuri {hashtag}</b>\n"
    postare += f"🚨 <b>ALERTĂ TRAFIC LIVE</b> 🚨\n\n"

    # Tip incident
    tip_ro = cauza_ro.capitalize() if cauza_ro else categorie.replace('#', '').replace('_', ' ')
    postare += f"{emoji} <b>{tip_ro}</b>"
    if road_number and not road_tag:
        postare += f" pe autostrada <b>{road_number}</b>"
    postare += ".\n"

    # Locatie
    locatie_parts = []
    if locatie_gps:
        locatie_parts.append(locatie_gps)
    if cw_ro:
        locatie_parts.append(cw_ro)
    if lane_ro:
        locatie_parts.append(lane_ro)
    if locatie_parts:
        postare += f"📍 <b>Locație:</b> {', '.join(locatie_parts)}\n"

    # Directie
    if direction:
        dir_ro = "spre " + direction if direction not in ["pozitiv", "negativ"] else f"direcția {direction}"
        postare += f"🧭 <b>Direcție:</b> {dir_ro}\n"

    # Date cantitative
    queue = alerta.get("queue_length_km", 0)
    delay = alerta.get("delay_minutes", 0)
    if queue > 0:
        postare += f"📏 <b>Lungime coloană:</b> {queue:.1f} km\n"
    if delay > 0:
        postare += f"⏱️ <b>Întârziere estimată:</b> + {delay:.0f} minute\n"

    # Detalii
    if comment_ro:
        postare += f"ℹ️ <b>Detalii:</b> {comment_ro}\n"

    # Timp sfarsit
    time_end = alerta.get("time_end", "")
    if time_end:
        try:
            dt = datetime.fromisoformat(str(time_end).replace("Z", "+00:00"))
            postare += f"🕐 <b>Până la:</b> {dt.strftime('%d/%m/%Y %H:%M')}\n"
        except:
            pass

    postare += f"\n<i>{SEMNATURA}</i>"
    return postare

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
        items = soup.find_all('a', class_=re.compile(r'nieuwsoverzicht__item'))
        for item in items[:5]:
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
    print(f"🚀 Pornire Olanda Bot v7.0 (DATEX II + GPS Locatie): {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if not all([TELEGRAM_TOKEN, CANAL_DESTINATIE]):
        print("❌ EROARE CRITICĂ: Configurație incompletă!")
        return

    init_db()
    load_blacklist()

    while True:
        try:
            print(f"🔄 Verificare DATEX II la {datetime.now().strftime('%H:%M:%S')}...")
            stiri_vechi_db = preia_stiri_vechi(15)

            # 1. TRAFIC LIVE
            alerte_live = preia_trafic_live()
            for obs in alerte_live:
                rec_id = obs.get("recordId", "")
                delay = obs.get("delay_minutes", 0)
                queue = obs.get("queue_length_km", 0)
                cauza_type = obs.get("cauza_type", "")
                is_accident = cauza_type == "accident"

                # FILTRU: trimite doar daca are delay SAU coada SAU e accident/inchidere
                emoji, categorie = determina_emoji_si_categorie(obs)
                categorii_importante = ["#Accident", "#Banda_Inchisa", "#Control_Granita", "#Drum_Deteriorat"]
                are_impact = delay > 0 or queue > 0 or categorie in categorii_importante

                if not are_impact:
                    continue

                obs_hash = f"D2_{rec_id}_{int(delay // 5)}_{int(queue * 2)}"
                if is_blacklisted(obs_hash):
                    continue

                mesaj = construieste_mesaj_alerta(obs)
                if trimite_telegram(mesaj):
                    add_to_blacklist(obs_hash)
                    road = obs.get("road_number", "?")
                    salveaza_stire_in_memorie(f"{categorie} {road}: {obs.get('cauza_nl', '')} | delay={delay:.0f}min queue={queue:.1f}km")
                    print(f"   ✅ [{road}] {rec_id[:30]} | {categorie} | delay={delay:.0f}min | queue={queue:.1f}km")
                    time.sleep(2)
                time.sleep(0.3)

            # 2. STIRI RSS (TTM)
            for rss_url in RSS_FEEDS:
                try:
                    feed = feedparser.parse(rss_url)
                except:
                    continue
                if not hasattr(feed, "entries"):
                    continue
                for entry in feed.entries[:3]:
                    titlu = getattr(entry, "title", None)
                    link = getattr(entry, "link", None)
                    if not link or not titlu:
                        continue
                    h = hash_text(link)
                    if is_blacklisted(h):
                        continue
                    descriere = getattr(entry, "description", "") or getattr(entry, "summary", "")
                    res = proceseaza_stire_ai(titlu, descriere, stiri_vechi_db, sursa_tip="RSS")
                    if res:
                        if res.get("categorie") == "IGNORE" or res.get("duplicat", False):
                            add_to_blacklist(h)
                        else:
                            text_rezumat = res.get('text_ro', 'Fara text')
                            postare = (f"{res.get('emoji', '📌')} <b>{res.get('categorie')}</b>\n\n"
                                       f"{text_rezumat}\n\n"
                                       f"🔗 <a href='{link}'>Sursa Originală</a>\n\n"
                                       f"<i>{SEMNATURA}</i>")
                            text_audio = f"Știre nouă despre {res.get('categorie','').replace('#', '')}. {text_rezumat}"
                            if trimite_telegram_cu_audio(postare, text_audio):
                                add_to_blacklist(h)
                                salveaza_stire_in_memorie(text_rezumat)
                                print(f"   ✅ [TTM] {titlu[:40]}...")
                                time.sleep(2)
                    time.sleep(1)

            # 3. FNV
            stiri_fnv = preia_stiri_fnv()
            for stire in stiri_fnv:
                titlu = stire.get("title")
                link = stire.get("link")
                if not link or not titlu:
                    continue
                h = hash_text(link)
                if is_blacklisted(h):
                    continue
                descriere = stire.get("description", "Noutate FNV.")
                res = proceseaza_stire_ai(titlu, descriere, stiri_vechi_db, sursa_tip="RSS")
                if res:
                    if res.get("categorie") == "IGNORE" or res.get("duplicat", False):
                        add_to_blacklist(h)
                    else:
                        text_rezumat = res.get('text_ro', 'Fara text')
                        postare = (f"{res.get('emoji', '📌')} <b>{res.get('categorie')}</b>\n\n"
                                   f"{text_rezumat}\n\n"
                                   f"🔗 <a href='{link}'>Sursa FNV</a>\n\n"
                                   f"<i>{SEMNATURA}</i>")
                        text_audio = f"Noutate sindicală FNV. {text_rezumat}"
                        if trimite_telegram_cu_audio(postare, text_audio):
                            add_to_blacklist(h)
                            salveaza_stire_in_memorie(text_rezumat)
                            print(f"   ✅ [FNV] {titlu[:40]}...")
                            time.sleep(2)
                time.sleep(1)

            time.sleep(VERIFY_INTERVAL)

        except Exception as _e:
            print(f"❌ EROARE CRITICĂ ÎN WORKER: {_e}")
            import traceback
            traceback.print_exc()
            time.sleep(10)

# ==========================================
# COMENZI TELEGRAM (async - PTB v20)
# ==========================================
async def cmd_drum(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    args = context.args

    if not args:
        trimite_telegram(
            "❓ <b>Utilizare:</b> <code>/drum A2</code> sau <code>/drum N11</code>\n"
            "Specificați codul drumului pentru informații live.",
            chat_id=chat_id
        )
        return

    road_query = args[0].upper().strip()
    trimite_telegram(f"🔍 Caut informații live pentru <b>{road_query}</b>...", chat_id=chat_id)

    try:
        alerte = preia_trafic_live()
    except Exception as e:
        trimite_telegram(f"❌ Eroare: {e}", chat_id=chat_id)
        return

    alerte_drum = [a for a in alerte if a.get("road_number", "").upper() == road_query]

    if not alerte_drum:
        trimite_telegram(
            f"✅ <b>{road_query}</b>: Nu sunt incidente raportate acum.\n"
            f"<i>DATEX II LIVE • {datetime.now().strftime('%H:%M')}</i>\n"
            f"<i>{SEMNATURA}</i>",
            chat_id=chat_id
        )
        return

    trimite_telegram(
        f"🚦 <b>{road_query}</b> — {len(alerte_drum)} incident(e) găsite:\n"
        f"<i>DATEX II • {datetime.now().strftime('%H:%M')}</i>",
        chat_id=chat_id
    )
    time.sleep(1)
    for a in alerte_drum[:10]:
        mesaj = construieste_mesaj_alerta(a, road_tag=f"#{road_query}")
        trimite_telegram(mesaj, chat_id=chat_id)
        time.sleep(1)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    trimite_telegram(
        f"🤖 <b>Olanda Bot v7.0 Status</b>\n\n"
        f"✅ Bot activ\n"
        f"📡 Sursa: Rijkswaterstaat DATEX II\n"
        f"🕐 Interval: {VERIFY_INTERVAL}s\n"
        f"📅 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n\n"
        f"<i>Comenzi:</i>\n"
        f"• <code>/drum A2</code>\n"
        f"• <code>/status</code>\n\n"
        f"<i>{SEMNATURA}</i>",
        chat_id=chat_id
    )

# ==========================================
# BOT TELEGRAM ASYNC (PTB v20) - FIXED
# ==========================================
def run_telegram_bot():
    if not TELEGRAM_TOKEN:
        print("❌ TELEGRAM_TOKEN lipseste.")
        return

    async def main():
        app = Application.builder().token(TELEGRAM_TOKEN).build()
        app.add_handler(CommandHandler("drum", cmd_drum))
        app.add_handler(CommandHandler("status", cmd_status))
        print("🤖 Bot Telegram v20 pornit")
        async with app:
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            while True:
                await asyncio.sleep(60)

    asyncio.run(main())

# ==========================================
# MAIN
# ==========================================
if __name__ == "__main__":
    worker_thread = threading.Thread(target=worker_loop, daemon=True)
    worker_thread.start()

    telegram_thread = threading.Thread(target=run_telegram_bot, daemon=True)
    telegram_thread.start()

    run_server()
