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
# CONFIGURAȚII
# ==========================================
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CANAL_DESTINATIE = os.getenv("TELEGRAM_CHANNEL_ID")

RSS_FEEDS = [
    "https://www.nu.nl/rss/Algemeen",
    "https://nos.nl/export/rss/nederland.xml",
    "https://www.anwb.nl/feeds/verkeersinformatie",
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
            print(f"📚 Blacklist încărcat: {len(BLACKLIST_SET)} intrări")
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
        except Exception as e:
            print(f"⚠️ Eroare scriere blacklist: {e}")

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
    if not DEEPSEEK_KEY:
        print("❌ Lipsă cheie API DeepSeek!")
        return None

    titlu_curat = titlu.replace('"', "'")[:200]
    desc_curat = (descriere or "Fără descriere").replace('"', "'")[:500]

    prompt = f"""Ești un editor de știri OSINT. Traduce și rezumă știrea în română (stil Reuters).
Alege o categorie: #Transport, #Vreme, #Politica, #Economie, #Social sau #Diverse.
Alege un emoji relevant.

Titlu: {titlu_curat}
Descriere: {desc_curat}

Răspunde STRICT JSON cu formatul exact:
{{"categorie": "#Diverse", "emoji": "📰", "text_ro": "Titlu Tradus - Rezumat maxim 2 propoziții"}}"""

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_KEY}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 300,
                "response_format": {"type": "json_object"},
            },
            headers=headers,
            timeout=60,
        )

        resp.raise_for_status()
        data = resp.json()

        if "choices" not in data or not data["choices"]:
            print("⚠️ Răspuns AI invalid (fără choices)")
            return None

        continut_brut = data["choices"][0]["message"]["content"]
        continut_curat = clean_json_response(continut_brut)
        rezultat = json.loads(continut_curat)

        if not all(k in rezultat for k in ["categorie", "emoji", "text_ro"]):
            print(f"⚠️ JSON incomplet: {rezultat.keys()}")
            return None

        return rezultat

    except json.JSONDecodeError as e:
        print(f"⚠️ Eroare parsare JSON: {e}")
        return None
    except Exception as e:
        print(f"⚠️ Eroare neașteptată AI: {e}")
        return None

# ==========================================
# TELEGRAM
# ==========================================
def trimite_telegram(text_final: str) -> bool:
    if not TELEGRAM_TOKEN or not CANAL_DESTINATIE:
        print("❌ Lipsă configurare Telegram!")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    if len(text_final) > 4000:
        text_final = text_final[:3997] + "..."

    payload = {
        "chat_id": CANAL_DESTINATIE,
        "text": text_final,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            return True
        else:
            print(f"⚠️ Eroare Telegram: {resp.status_code} - {resp.text[:200]}")
            return False
    except Exception as e:
        print(f"⚠️ Eroare trimitere Telegram: {e}")
        return False

# ==========================================
# MAIN
# ==========================================
def main():
    print(f"🚀 Pornire: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if not all([DEEPSEEK_KEY, TELEGRAM_TOKEN, CANAL_DESTINATIE]):
        print("❌ Configurație incompletă! Verifică secretele pe GitHub.")
        return

    load_blacklist()
    total_procesate = 0
    total_postate = 0

    for idx, url in enumerate(RSS_FEEDS):
        print(f"\n📡 [{idx+1}/{len(RSS_FEEDS)}] Scanăm: {url}")
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"⚠️ Eroare parsare feed {url}: {e}")
            continue

        if not hasattr(feed, "entries") or not feed.entries:
            continue

        for entry in feed.entries[:15]:
            titlu = getattr(entry, "title", None)
            link = getattr(entry, "link", None)
            if not link or not titlu:
                continue

            h = hash_text(link)
            if is_blacklisted(h):
                continue

            print(f"   🔎 Știre nouă: {titlu[:60]}...")
            total_procesate += 1

            descriere = getattr(entry, "description", "") or getattr(entry, "summary", "") or "Fără descriere"
            res = proceseaza_cu_ai(titlu, descriere)

            if not res:
                print("   ❌ Skipped (eroare AI)")
                time.sleep(1)
                continue

            text_ro = res.get("text_ro", "Eroare traducere")
            categorie = res.get("categorie", "#Diverse")
            emoji = res.get("emoji", "📰")

            postare = (
                f"{emoji} <b>{categorie}</b>\n\n"
                f"{text_ro}\n\n"
                f"🔗 <a href='{link}'>Sursa Originală</a>\n\n"
                f"<i>{SEMNATURA}</i>"
            )

            if trimite_telegram(postare):
                add_to_blacklist(h)
                total_postate += 1
                print("   ✅ Postat!")
                time.sleep(2)
            else:
                print("   ❌ Eroare trimitere Telegram")

            time.sleep(1)

        if idx < len(RSS_FEEDS) - 1:
            time.sleep(2)

    print(f"\n🏁 Finalizat: {total_postate}/{total_procesate} știri postate")

if __name__ == "__main__":
    main()

def get_bot_status_message():
    return "Bot activ si functional. Sistem Olanda online"
