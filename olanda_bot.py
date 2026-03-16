import os
import time
import hashlib
import json
import requests
import feedparser
import re
from datetime import datetime
from typing import Optional, Dict, Any

# ==========================================
# CONFIGURAȚII (Asistent Șoferi NL)
# ==========================================
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CANAL_DESTINATIE = os.getenv("TELEGRAM_CHANNEL_ID")

# Noi surse dedicate: Trafic oficial, Logistică, Economie NL
RSS_FEEDS = [
    "https://www.rijkswaterstaat.nl/rss",             # Alerte Rijkswaterstaat (drumuri/infrastructură)
    "https://nos.nl/export/rss/economie.xml",         # NOS Economie (Taxe, Legi, Belasting)
    "https://www.ttm.nl/feed/",                       # Totaal Transactie Management (Transport NL)
    "https://www.nu.nl/rss/Economie"                  # NU.nl Economie/Muncă
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
        try:
            with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
                BLACKLIST_SET = set(line.strip() for line in f if line.strip())
        except Exception as e:
            print(f"⚠️ Eroare încărcare blacklist: {e}")
            BLACKLIST_SET = set()

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
# UTILITARE AI
# ==========================================
def clean_json_response(text: str) -> str:
    cleaned = re.sub(r"^```json\s*", "", text).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    return cleaned

def proceseaza_cu_ai(titlu: str, descriere: str) -> Optional[Dict[str, Any]]:
    if not DEEPSEEK_KEY: return None

    titlu_curat = titlu.replace('"', "'")[:200]
    desc_curat = (descriere or "Fără descriere").replace('"', "'")[:600]

    # Prompt specializat pentru soferi in NL
    prompt = f"""Ești un asistent inteligent pentru un șofer profesionist de camion (contract CAO) în Olanda.
Analizează știrea. Dacă știrea NU are legătură cu Olanda (sau cu impact direct asupra transportului spre/dinspre Olanda), setează categoria la "IGNORE".
Dacă are legătură, tradu și rezumă știrea în română, evidențiind impactul pentru șoferi (accidente, drumuri închise, legi noi, taxe, reguli sindicale).
Categorii permise STRICT: #Trafic_Drumuri, #Legislatie_Taxe, #Sindicate_CAO, #Economie_Logistica, sau IGNORE.
Alege un emoji relevant (ex: 🚛, ⚖️, 👷, 💶).

Titlu: {titlu_curat}
Descriere: {desc_curat}

Răspunde STRICT JSON:
{{"categorie": "#Trafic_Drumuri", "emoji": "🚛", "text_ro": "Rezumat clar și util în max 2 propoziții."}}"""

    headers = {"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"}

    try:
        resp = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "response_format": {"type": "json_object"}
            },
            headers=headers, timeout=60
        )
        resp.raise_for_status()
        data = resp.json()
        continut_brut = data["choices"][0]["message"]["content"]
        rezultat = json.loads(clean_json_response(continut_brut))
        
        if rezultat.get("categorie") == "IGNORE":
            return None # Ignoram stirile irelevante geografic
            
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
    payload = {
        "chat_id": CANAL_DESTINATIE,
        "text": text_final[:3997],
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        return resp.status_code == 200
    except: return False

# ==========================================
# MAIN
# ==========================================
def main():
    print(f"🚀 Pornire Asistent NL: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if not all([DEEPSEEK_KEY, TELEGRAM_TOKEN, CANAL_DESTINATIE]):
        print("❌ Configurație incompletă!")
        return

    load_blacklist()
    
    for url in RSS_FEEDS:
        print(f"\n📡 Scanăm: {url}")
        try: feed = feedparser.parse(url)
        except: continue

        if not hasattr(feed, "entries"): continue

        for entry in feed.entries[:10]:
            titlu = getattr(entry, "title", None)
            link = getattr(entry, "link", None)
            if not link or not titlu: continue

            h = hash_text(link)
            if is_blacklisted(h): continue

            descriere = getattr(entry, "description", "") or getattr(entry, "summary", "")
            res = proceseaza_cu_ai(titlu, descriere)

            if res and res.get("categorie") != "IGNORE":
                postare = f"{res.get('emoji', '📌')} <b>{res.get('categorie')}</b>\n\n{res.get('text_ro')}\n\n🔗 <a href='{link}'>Sursa Originală</a>\n\n<i>{SEMNATURA}</i>"
                if trimite_telegram(postare):
                    add_to_blacklist(h)
                    print(f"   ✅ Postat: {titlu[:30]}")
                    time.sleep(2)
            else:
                add_to_blacklist(h) # Punem in blacklist si ce am ignorat ca sa nu intrebam AI-ul de 100 de ori
                print("   ❌ Ignorat (Filtru AI)")
            time.sleep(1)

if __name__ == "__main__":
    main()
