import os
import time
import logging
import random
import re
import json
import sqlite3
from datetime import datetime, timedelta
import telebot
from telebot import types
from threading import Lock
from urllib.parse import urlparse, unquote
from env_loader import TELEGRAM_TOKEN, CHAT_IDS, ADMIN_IDS, DB_FILE, URLS_FILE

lock = Lock()  # Pentru siguranța thread-urilor
# Cache pentru data publicării
PUBLICATION_DATE_CACHE = {}  # ad_id -> {date_str, minutes_ago, last_check_time}
MAX_CACHE_SIZE = 1000
CACHE_EXPIRY_HOURS = 6

# SETĂRI SCANARE - Optimizate pentru GPU Flipping (Viteză maximă)
QUICK_CHECK_INTERVAL = 10     # 10 secunde între verificări
MIN_INTERVAL = 15             # Interval minim între request-uri
MAX_INTERVAL = 30             
MAX_AD_AGE_MINUTES = 20       # Un GPU bun dispare în 20 min. Nu ne interesează ce e mai vechi.
VERY_FRESH_AD_MINUTES = 3     # Notificare prioritară pentru anunțuri sub 3 minute
SKIP_FIRST_N_ADS = 2          # Ignoră primele 2 (Promovate/Ad-uri)
MAX_CARDS_TO_CHECK = 12       # Verificăm doar prima parte a paginii (cele mai noi)
SCROLL_COUNT = 2              # 2 scroll-uri sunt destule pentru OLX.ro desktop
MAX_PARALLEL_URLS = 4         # Putem verifica mai multe căutări deodată
PAGE_LOAD_TIMEOUT = 25        
DETAILED_LOGGING = True       
CONSECUTIVE_OLD_COUNT = 2     # Dacă găsim 2 vechi, tăiem scanarea (economisim timp)
EARLY_EXIT_ON_OLD = True      

def check_ad_sent(link):
    """Verifică în DB dacă anunțul a fost deja trimis pe Telegram."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT sent_to_telegram FROM ads WHERE link = ?", (link,))
        result = cursor.fetchone()
        conn.close()
        return bool(result[0]) if result and result[0] is not None else False
    except Exception as e:
        logging.error(f"Eroare check_ad_sent: {e}")
        return False

def mark_ad_as_sent(link):
    """Marchează anunțul ca trimis în baza de date."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE ads SET sent_to_telegram = 1 WHERE link = ?", (link,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logging.error(f"Eroare mark_ad_as_sent: {e}")
        return False
    
def process_unsent_ads():
    """Trimite anunțurile care sunt în DB dar nu au ajuns pe Telegram (restanțe)."""
    unsent_ads = get_unsent_ads() # Funcția asta am definit-o în calupul anterior
    if not unsent_ads:
        return 0
    
    sent_count = 0
    for ad in unsent_ads:
        # Verificăm dacă mai este "fresh" (să nu trimitem ceva de acum 3 zile)
        minutes_ago = get_cached_ad_age(ad.get('ad_id'), ad.get('publication_date', ''))
        if minutes_ago <= MAX_AD_AGE_MINUTES * 1.5:
            if send_to_telegram(ad):
                sent_count += 1
        else:
            mark_ad_as_sent(ad['link']) # Îl marcăm ca trimis ca să nu mai încerce
            
    return sent_count

def extract_date_from_preview(element):
    """Extrage string-ul de dată din elementul HTML al cardului OLX."""
    # Selectori standard pentru data de pe OLX.ro
    selectors = [
        'css:p[data-testid="location-date"]',
        'css:.css-vbz67q'
    ]
    for selector in selectors:
        try:
            date_element = element.ele(selector)
            if date_element and date_element.text:
                text = date_element.text
                # De obicei textul e de forma "Bucuresti - Azi la 12:00". Luăm ce e după cratimă.
                if " - " in text:
                    return text.split(" - ")[1].strip()
                return text.strip()
        except:
            continue
    return None

def add_ad_to_db(link, title, site="OLX.ro", ad_id=None, date_published=None):
    """Adaugă un anunț nou în baza de date. Returnează True dacă a fost adăugat."""
    now = datetime.now()
    expiry = now + timedelta(days=7) # Anunțul expiră în DB după 7 zile

    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        # Verificăm dacă există deja
        cursor.execute("SELECT sent_to_telegram FROM ads WHERE link = ?", (link,))
        result = cursor.fetchone()
        
        if result is not None:
            # Dacă există, doar îi prelungim viața în DB
            cursor.execute("UPDATE ads SET expiry_date = ? WHERE link = ?", (expiry.isoformat(), link))
            conn.commit()
            conn.close()
            return False, bool(result[0])
        
        # Inserăm anunț nou
        cursor.execute(
            '''
            INSERT INTO ads 
            (link, title, ad_id, site, date_found, date_published, expiry_date, sent_to_telegram)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            ''',
            (link, title, ad_id, site, now.isoformat(), date_published, expiry.isoformat())
        )
        conn.commit()
        conn.close()
        return True, False 

    except Exception as e:
        logging.error(f"❌ Eroare DB la adăugare: {e}")
        return False, False

def get_ad_stats():
    """Generează statisticile pentru comanda /dbstats."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        stats = {}
        
        cursor.execute("SELECT COUNT(*) FROM ads")
        stats['total_ads'] = cursor.fetchone()[0]
        
        cursor.execute("SELECT site, COUNT(*) FROM ads GROUP BY site")
        stats['by_site'] = {site: count for site, count in cursor.fetchall()}
        
        yesterday = (datetime.now() - timedelta(days=1)).isoformat()
        cursor.execute("SELECT COUNT(*) FROM ads WHERE date_found > ?", (yesterday,))
        stats['last_24h'] = cursor.fetchone()[0]
        
        cursor.execute("SELECT value FROM settings WHERE key = 'last_cleanup'")
        res = cursor.fetchone()
        stats['last_cleanup'] = res[0] if res else "Niciodată"
        
        cursor.execute("SELECT title, date_found FROM ads ORDER BY date_found DESC LIMIT 3")
        stats['recent_ads'] = [{'title': r[0], 'date': r[1]} for r in cursor.fetchall()]
        
        cursor.execute("SELECT COUNT(*) FROM ads WHERE sent_to_telegram = 0")
        stats['unsent_ads'] = cursor.fetchone()[0]
        
        conn.close()
        return stats
    except Exception as e:
        logging.error(f"Eroare statistici: {e}")
        return {'total_ads': 0, 'last_cleanup': 'Eroare'}

def quick_check_url(url, options=None):
    """Funcția de worker pentru thread-uri: deschide browser, scanează, închide browser."""
    driver = None
    try:
        if options is None:
            options = create_browser_options()
        
        # Creăm o instanță nouă de browser pentru fiecare thread
        driver = ChromiumPage(addr_or_opts=options)
        
        # Pornim scanarea efectivă a paginii
        fresh_ads_found = quick_check_ads(url, driver)
        
        return fresh_ads_found
    except Exception as e:
        logging.error(f"Eroare critică în thread-ul pentru {url}: {e}")
        return False
    finally:
        if driver:
            try:
                driver.quit() # Foarte important să închidem procesele Chrome
            except: pass

# Inițializare Bot
bot = telebot.TeleBot(TELEGRAM_TOKEN)

def setup_logging():
    logging.basicConfig(
        level=logging.INFO, 
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler("bot.log", encoding='utf-8'),
            logging.StreamHandler()
        ]
    )

# Luni în Română pentru parsare
ROMANIAN_MONTHS = {
    'ianuarie': 1, 'februarie': 2, 'martie': 3, 'aprilie': 4, 'mai': 5, 'iunie': 6,
    'iulie': 7, 'august': 8, 'septembrie': 9, 'octombrie': 10, 'noiembrie': 11, 'decembrie': 12
}

def is_admin(user_id):
    """Verifică dacă ID-ul de Telegram este în lista de admini."""
    return str(user_id) in ADMIN_IDS

def extract_title_from_url(url):
    """Extrage titlul plăcii din URL (util pentru preview rapid)."""
    try:
        path = urlparse(url).path.strip('/').split('/')
        for part in path:
            if 'ID' in part and '-' in part:
                return unquote(part.split('-ID')[0].replace('-', ' ')).strip()
        if path:
            return unquote(path[-1].split('ID')[0].replace('-', ' ')).strip()
    except:
        pass
    return "Componentă PC"

def extract_ad_id_from_url(url):
    """Extrage ID-ul unic al anunțului pentru baza de date."""
    match = re.search(r'ID([a-zA-Z0-9]+)', url)
    return match.group(1) if match else None

def load_urls():
    """Încarcă link-urile de căutare salvate (ex: căutare rtx 3080 defect)."""
    if os.path.exists(URLS_FILE):
        try:
            with open(URLS_FILE, 'r') as f:
                return json.load(f).get('urls', [])
        except:
            pass
    return []

def save_urls(urls):
    try:
        os.makedirs(os.path.dirname(os.path.abspath(URLS_FILE)), exist_ok=True)
        with open(URLS_FILE, 'w') as f:
            json.dump({'urls': urls}, f, indent=2)
        return True
    except:
        return False
    
def init_database():
    """Inițializează baza de date pentru a evita notificările duble."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Tabel unic pentru anunțuri OLX
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS ads (
        link TEXT PRIMARY KEY,
        title TEXT,
        ad_id TEXT,
        date_found TIMESTAMP,
        date_published TEXT,
        expiry_date TIMESTAMP,
        sent_to_telegram BOOLEAN DEFAULT 0
    )
    ''')
    
    cursor.execute('CREATE TABLE IF NOT EXISTS activity_log (id INTEGER PRIMARY KEY, action TEXT, url TEXT, timestamp TIMESTAMP)')
    cursor.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP)')
    
    # Index pentru viteză la căutare link existent
    cursor.execute('CREATE INDEX IF NOT EXISTS ads_link_idx ON ads(link)')
    
    conn.commit()
    conn.close()
    logging.info("Baza de date pregătită strict pentru OLX România.")

def check_ad_exists(link):
    """Verifică dacă anunțul există deja în baza de date."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM ads WHERE link = ?", (link,))
    exists = cursor.fetchone() is not None
    conn.close()
    return exists

def get_unsent_ads():
    """Recuperează anunțurile salvate în DB care nu au fost încă trimise pe Telegram."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT link, title, ad_id, date_published 
            FROM ads WHERE sent_to_telegram = 0
            '''
        )
        results = cursor.fetchall()
        conn.close()
        
        return [{'link': r[0], 'title': r[1], 'ad_id': r[2], 'publication_date': r[3]} for r in results]
    except Exception as e:
        logging.error(f"Eroare recuperează anunțuri netrimise: {e}")
        return []

def cleanup_old_ads():
    """Șterge anunțurile mai vechi de 7 zile pentru a păstra baza de date rapidă."""
    now = datetime.now()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("SELECT value FROM settings WHERE key = 'last_cleanup'")
    last_cleanup = datetime.fromisoformat(cursor.fetchone()[0])
    
    if now - last_cleanup < timedelta(days=7):
        conn.close()
        return False
    
    cursor.execute("DELETE FROM ads WHERE expiry_date < ?", (now.isoformat(),))
    deleted_count = cursor.rowcount
    cursor.execute("UPDATE settings SET value = ?, updated_at = ? WHERE key = 'last_cleanup'", (now.isoformat(), now.isoformat()))
    cursor.execute("VACUUM") # Compactează DB după ștergere
    
    conn.commit()
    conn.close()
    logging.info(f"Cleanup finalizat. Șterse: {deleted_count} anunțuri vechi.")
    return True

def parse_romanian_date(date_str):
    """Transformă 'Azi la 14:00' sau '12 februarie' în minute scurse de la postare."""
    if not date_str: return float('inf')
        
    now = datetime.now()
    date_str = date_str.lower().strip()
    
    try:
        # Format: "Azi la 10:30"
        if "azi la" in date_str:
            time_part = date_str.split("azi la")[1].strip()
            h, m = map(int, time_part.split(':'))
            pub_date = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if pub_date > now: pub_date -= timedelta(days=1)
                
        # Format: "Ieri la 22:15"
        elif "ieri la" in date_str:
            time_part = date_str.split("ieri la")[1].strip()
            h, m = map(int, time_part.split(':'))
            pub_date = (now - timedelta(days=1)).replace(hour=h, minute=m, second=0, microsecond=0)
            
        # Format: "14 februarie"
        elif any(month in date_str for month in ROMANIAN_MONTHS.keys()):
            match = re.search(r'(\d+)\s+([a-z]+)(?:\s+(\d{4}))?', date_str)
            if match:
                day = int(match.group(1))
                month = ROMANIAN_MONTHS.get(match.group(2), now.month)
                year = int(match.group(3)) if match.group(3) else now.year
                pub_date = datetime(year, month, day)
                if pub_date > now and not match.group(3): pub_date = pub_date.replace(year=now.year - 1)
            else: return float('inf')
        else: return float('inf')
        
        return (now - pub_date).total_seconds() / 60
    except: return float('inf')

def get_cached_ad_age(ad_id, date_str):
    """Verifică vârsta anunțului folosind cache-ul local pentru a evita calcule repetitive."""
    now = time.time()
    if ad_id in PUBLICATION_DATE_CACHE:
        entry = PUBLICATION_DATE_CACHE[ad_id]
        if (now - entry['last_check_time']) / 60 < 20: # Cache valid 20 min
            return entry['minutes_ago'] + (now - entry['last_check_time']) / 60
            
    minutes_ago = parse_romanian_date(date_str)
    PUBLICATION_DATE_CACHE[ad_id] = {'minutes_ago': minutes_ago, 'last_check_time': now}
    
    # Menține cache-ul sub limita setată (1000)
    if len(PUBLICATION_DATE_CACHE) > MAX_CACHE_SIZE:
        PUBLICATION_DATE_CACHE.pop(next(iter(PUBLICATION_DATE_CACHE)))
    return minutes_ago

# --- CONFIGURARE BROWSER ---
from DrissionPage import ChromiumPage, ChromiumOptions

# Selectorii actualizați pentru OLX.ro Desktop
AD_CARD_SELECTORS = ['css:div[data-cy="l-card"]', 'css:div[data-testid="l-card"]']
LINK_SELECTORS = ['css:a[href*="/oferta/"]'] # Eliminat Otomoto
IMAGE_SELECTORS = ['css:img[src]']
DATE_SELECTORS = ['css:p[data-testid="location-date"]']

def create_browser_options():
    """Configurează Chrome pentru a fi rapid și greu de detectat."""
    options = ChromiumOptions()
    options.set_user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")
    options.no_imgs = True  # CRITIC: Nu încarcă imagini = Viteză x2
    options.headless = True # Rulează în fundal
    options.set_argument("--disable-blink-features=AutomationControlled")
    options.set_argument("--no-sandbox")
    options.set_argument("--disable-gpu")
    return options

def wait_for_page_load(driver, timeout=PAGE_LOAD_TIMEOUT):
    """Așteaptă până când pagina este stabilă și gata de citit."""
    start = time.time()
    prev_html = ""
    while time.time() - start < timeout:
        try:
            curr_html = driver.html
            if curr_html == prev_html and driver.run_js("return document.readyState") == "complete":
                return True
            prev_html = curr_html
            time.sleep(0.5)
        except: time.sleep(1)
    return False

def wait_for_ads(driver, min_cards=6, timeout=15):
    """Așteaptă încărcarea anunțurilor pe OLX.ro."""
    selectors = [
        'css:div[data-cy="l-card"]',
        'css:div[data-testid="l-card"]',
        'css:.css-l9drzq'
    ]
    
    start = time.time()
    while time.time() - start < timeout:
        for selector in selectors:
            try:
                cards = driver.eles(selector)
                if cards and len(cards) >= min_cards:
                    logging.info(f"✅ Găsit {len(cards)} carduri cu selectorul: {selector}")
                    return True
            except: pass
        time.sleep(0.5)
    
    logging.warning("⚠️ Nu s-au încărcat suficiente anunțuri în timpul alocat.")
    return False

def get_ad_cards(driver):
    """Extrage toate elementele de tip anunț de pe pagină."""
    for selector in AD_CARD_SELECTORS:
        try:
            cards = driver.eles(selector)
            if cards: return cards
        except: pass
    return []

def is_promoted_card(card):
    """Verifică dacă anunțul este promovat (Sponsorizat/TOP)."""
    try:
        # 1. Verificare prin atribut stabil
        if card.ele('css:div[data-testid="adCard-featured"]'):
            return True
        
        # 2. Verificare prin badge-ul vizibil (clasa ta actuală)
        if card.ele('css:div.css-p9u9v3'):
            return True

        # 3. Verificare prin text (Fallback de siguranță)
        # Caută orice label care conține cuvântul "Sponsorizat"
        labels = card.eles('tag:span')
        for label in labels:
            if "sponsorizat" in label.text.lower():
                return True

        return False
    except:
        return False
    
def extract_preview_data(card, card_index=None):
    """Extrage datele esențiale din preview (titlu, preț, link, imagine)."""
    try:
        detailed_log = DETAILED_LOGGING and (card_index is not None and card_index < 10)
        
        # Extragere Link
        link_element = card.ele('css:a[href*="/oferta/"]')
        if not link_element: return None

        link = link_element.attr('href')
        if not link.startswith("http"):
            link = "https://www.olx.pl" + link if "olx.pl" in link else "https://www.olx.ro" + link
        
        ad_id = extract_ad_id_from_url(link)
        title = extract_title_from_url(link)
        date_str = extract_date_from_preview(card)
        
        # Extragere Imagine
        img_element = card.ele('css:img')
        image_url = img_element.attr('src') if img_element else None

        result = {
            'link': link,
            'title': title,
            'ad_id': ad_id,
            'publication_date': date_str,
            'image': image_url,
            'site': "OLX.ro"
        }
        
        if date_str:
            result['minutes_ago'] = get_cached_ad_age(ad_id, date_str)
        
        return result
    except Exception as e:
        if detailed_log: logging.warning(f"Eroare card #{card_index}: {e}")
        return None

def try_send_from_preview(card, card_index=None):
    """Logica de 'SNIPER': Verifică, filtrează și trimite anunțul."""
    try:
        preview_data = extract_preview_data(card, card_index)
        if not preview_data: return False, False

        title = preview_data['title'].lower()
        link = preview_data['link']
        minutes_ago = preview_data.get('minutes_ago')

        # --- FILTRARE PENTRU FLIPPING (Logica ta) ---
        # Definirea cuvintelor cheie care indică un GPU cu defect
        keywords = ['defect', 'piese', 'nefunctional', 'cod 43', 'nu afiseaza', 'artefacte', 'donator']
        
        # Verificăm dacă titlul conține cuvintele cheie
        is_match = any(word in title for word in keywords)
        
        if not is_match:
            # Opțional: Poți lăsa botul să trimită și chilipiruri dacă au preț mic,
            # dar pentru început filtrăm doar defectele solicitate de tine.
            return False, False

        # Verificare Vechime
        if minutes_ago is None or minutes_ago > MAX_AD_AGE_MINUTES:
            logging.info(f"⏰ Anunț prea vechi ({minutes_ago:.1f} min): {preview_data['title']}")
            return False, True

        # Verificare Bază de Date (Să nu trimitem de două ori)
        if check_ad_exists(link):
            if check_ad_sent(link): return False, False
            logging.info(f"🔄 Anunț existent în DB, dar netrimis: {link}")
        else:
            add_ad_to_db(
                link, 
                preview_data['title'], 
                "OLX.ro", 
                preview_data['ad_id'], 
                preview_data['publication_date']
            )

        # Trimitere pe Telegram
        sent = send_to_telegram(preview_data)
        return sent, False

    except Exception as e:
        logging.warning(f"Eroare procesare card: {e}")
        return False, True
    
def send_telegram_message_with_retry(chat_id, message, parse_mode=None, photo=None, max_retries=5, retry_delay=3):
    """Trimite mesajul către Telegram cu logică de reîncercare (exponential backoff)."""
    for attempt in range(max_retries):
        try:
            if photo:
                return bot.send_photo(chat_id, photo, caption=message, parse_mode=parse_mode)
            else:
                return bot.send_message(chat_id, message, parse_mode=parse_mode)
        except Exception as e:
            is_telegram_error = "telegram" in str(e).lower() or "connection" in str(e).lower()
            if not is_telegram_error or attempt == max_retries - 1:
                logging.error(f"❌ Eșec final trimitere Telegram: {e}")
                raise
            
            wait_time = retry_delay * (attempt + 1)
            logging.info(f"⚠️ Eroare Telegram. Reîncercare în {wait_time}s...")
            time.sleep(wait_time)

def send_to_telegram(ad):
    """Pregătește și trimite notificarea în mod asincron pentru a nu bloca scanerul."""
    try:
        logging.info(f"📤 Livrare notificare: {ad['title']}")

        with lock:
            mark_success = mark_ad_as_sent(ad['link'])
            if not mark_success:
                logging.warning(f"⚠️ DB Error: Nu am putut marca anunțul ca trimis.")

        def send_telegram_async():
            try:
                ad_id = ad.get('ad_id')
                date_str = ad.get('publication_date', '')
                minutes_ago = ad.get('minutes_ago') or get_cached_ad_age(ad_id, date_str)

                # Flag pentru anunțuri sub 5 minute (Ultra-proaspete)
                is_very_fresh = isinstance(minutes_ago, (int, float)) and minutes_ago <= VERY_FRESH_AD_MINUTES

                header = "🔥 *ANUNȚ NOU (ULTRA-FRESH)*" if is_very_fresh else "📌 *OPORTUNITATE DETECTATĂ*"
                
                caption = (
                    f"{header}\n\n"
                    f"📦 *Titlu:* {ad['title']}\n"
                    f"⏱️ *Publicat acum:* {minutes_ago:.1f} min\n"
                    f"📆 *Data OLX:* {ad.get('publication_date', 'Necunoscută')}\n\n"
                    f"🔗 [VEZI ANUNȚUL PE OLX]({ad['link']})"
                )

                for chat_id in CHAT_IDS:
                    try:
                        if ad.get('image'):
                            send_telegram_message_with_retry(chat_id, caption, parse_mode="Markdown", photo=ad['image'])
                        else:
                            send_telegram_message_with_retry(chat_id, caption, parse_mode="Markdown")
                        logging.info(f"✅ Notificare trimisă cu succes către {chat_id}")
                    except Exception as err:
                        logging.error(f"❌ Eroare trimitere chat {chat_id}: {err}")

            except Exception as e:
                logging.error(f"🔥 Eroare în thread-ul de Telegram: {e}")

        import threading
        # Folosim threading ca să nu stăm după API-ul Telegram (latență minimă)
        telegram_thread = threading.Thread(target=send_telegram_async, daemon=True)
        telegram_thread.start()
        return True

    except Exception as e:
        logging.error(f"🔥 Eroare generală send_to_telegram: {e}")
        return False

def quick_check_ads(url, driver):
    """Bucla principală de verificare pentru un singur URL de căutare."""
    logging.info(f"🔍 Scanare URL: {url}")
    sent_count = 0
    consecutive_old_count = 0

    try:
        start_time = time.time()
        driver.get(url)

        if not wait_for_page_load(driver): return False
        if not wait_for_ads(driver): return False

        # --- GESTIONARE COOKIES OLX.RO ---
        try:
            # Încercăm să închidem bannerul de cookies automat
            driver.run_js("localStorage.setItem('olx-consent', 'true');")
            accept_btn = driver.eles('css:button[data-role="accept-consent"]')
            if accept_btn:
                accept_btn[0].click()
                time.sleep(0.5)
        except: pass

        # Scroll pentru a încărca elementele lazy-load (imagini/link-uri)
        for i in range(SCROLL_COUNT):
            driver.run_js(f"window.scrollTo(0, {(i+1) * 800});")
            time.sleep(0.5)

        all_cards = get_ad_cards(driver)
        if not all_cards: return False

        # Procesăm doar primele X carduri (cele mai noi)
        cards_to_process = all_cards[SKIP_FIRST_N_ADS : MAX_CARDS_TO_CHECK + SKIP_FIRST_N_ADS]

        for idx, card in enumerate(cards_to_process):
            # Sărim peste cele promovate dacă nu sunt ultra-fresh (pierdere de timp)
            if is_promoted_card(card): continue

            sent, is_old = try_send_from_preview(card, card_index=idx)

            if sent:
                sent_count += 1
                consecutive_old_count = 0
            elif is_old:
                consecutive_old_count += 1
            
            # Strategie de ieșire: dacă ultimele 2-3 sunt vechi, toată pagina e veche
            if EARLY_EXIT_ON_OLD and consecutive_old_count >= CONSECUTIVE_OLD_COUNT:
                logging.info(f"⏹️ Scanare oprită: am ajuns la anunțuri vechi.")
                break

        logging.info(f"🏁 Finalizat: {sent_count} notificări noi trimise în {time.time() - start_time:.1f}s")
        return sent_count > 0

    except Exception as e:
        logging.error(f"❌ Eroare la scanarea URL-ului: {e}")
        return False

def quick_check_all_urls():
    """Verifică toate căutările tale (ex: RTX 3070, RX 6800, etc.) în paralel."""
    urls = load_urls()
    if not urls:
        logging.info("ℹ️ Nu ai adăugat niciun URL de monitorizat. Folosește /addurl.")
        return False

    found_fresh = False
    process_unsent_ads() # Încercăm să trimitem restanțele din DB

    import concurrent.futures
    # Multi-threading pentru a verifica 3-4 căutări simultan
    max_workers = min(MAX_PARALLEL_URLS, len(urls))
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(quick_check_url, url): url for url in urls}
        for future in concurrent.futures.as_completed(futures):
            if future.result(): found_fresh = True

    return found_fresh

def show_admin_menu(chat_id):
    """Afișează meniul de administrare cu butoane inline."""
    markup = types.InlineKeyboardMarkup(row_width=2)
    
    list_btn = types.InlineKeyboardButton("📋 Listă URL-uri", callback_data="listurl")
    add_btn = types.InlineKeyboardButton("➕ Adaugă Căutare", callback_data="addurl")
    del_btn = types.InlineKeyboardButton("🗑️ Șterge URL", callback_data="delurl")
    stats_btn = types.InlineKeyboardButton("📊 Statistici DB", callback_data="dbstats")
    
    markup.add(list_btn, add_btn, del_btn, stats_btn)
    bot.send_message(chat_id, "⚙️ Gestiune Monitorizare OLX.ro:", reply_markup=markup)

def add_url_from_reply(message):
    """Procesează URL-ul primit prin funcția Reply."""
    if not message.text:
        bot.reply_to(message, "❌ Te rog trimite un link valid.")
        return
    process_new_url(message, message.text.strip())

def process_new_url(message, url):
    """Validează și salvează un nou link de căutare OLX."""
    if not (url.startswith('http://') or url.startswith('https://')):
        bot.reply_to(message, "❌ Format invalid. Link-ul trebuie să înceapă cu http:// sau https://")
        return
    
    if 'olx.ro' not in url:
        bot.reply_to(message, "⚠️ Doar link-urile de pe OLX.ro sunt suportate în această versiune.")
        return
    
    urls = load_urls()
    if url in urls:
        bot.reply_to(message, "ℹ️ Acest URL este deja monitorizat.")
        return
    
    urls.append(url)
    if save_urls(urls):
        bot.reply_to(message, "✅ Căutare adăugată cu succes! Botul va începe scanarea.")
        show_admin_menu(message.chat.id)
    else:
        bot.reply_to(message, "❌ Eroare la salvarea fișierului de configurare.")

# --- HANDLERE COMENZI BOT ---

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    """Mesaj de bun venit și instrucțiuni."""
    if is_admin(message.from_user.id):
        welcome_text = (
            "👋 Salut! Sunt botul tău de monitorizare OLX.ro.\n\n"
            "Comenzi disponibile:\n"
            "/addurl - Adaugă o căutare nouă\n"
            "/listurl - Vezi ce cauți acum\n"
            "/delurl - Șterge o căutare\n"
            "/dbstats - Statistici bază de date\n"
            "/cleanup - Curățare manuală DB\n"
            "/menu - Deschide meniul rapid"
        )
        bot.reply_to(message, welcome_text)
        show_admin_menu(message.chat.id)
    else:
        bot.reply_to(message, "⛔ Acces interzis. Doar adminii pot folosi acest bot.")

@bot.message_handler(commands=['menu'])
def menu_command(message):
    if is_admin(message.from_user.id):
        show_admin_menu(message.chat.id)

@bot.message_handler(commands=['addurl'])
def add_url(message):
    if not is_admin(message.from_user.id): return
    
    parts = message.text.split(' ', 1)
    if len(parts) < 2:
        markup = types.ForceReply(selective=True)
        bot.reply_to(message, "Trimite link-ul de căutare OLX.ro:", reply_markup=markup)
        return
    process_new_url(message, parts[1].strip())

@bot.message_handler(commands=['listurl'])
def list_urls(message):
    if not is_admin(message.from_user.id): return
    urls = load_urls()
    if not urls:
        bot.reply_to(message, "ℹ️ Nu monitorizezi niciun link momentan.")
        return
    
    response = "📋 URL-uri monitorizate active:\n\n"
    for i, url in enumerate(urls):
        response += f"{i+1}. {url}\n"
    bot.reply_to(message, response)

@bot.message_handler(commands=['delurl'])
def delete_url(message):
    if not is_admin(message.from_user.id): return
    urls = load_urls()
    if not urls:
        bot.reply_to(message, "ℹ️ Nu există URL-uri de șters.")
        return
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    for i, url in enumerate(urls):
        display_name = url[:45] + "..." if len(url) > 45 else url
        btn = types.InlineKeyboardButton(f"🗑️ {i+1}. {display_name}", callback_data=f"del_{i}")
        markup.add(btn)
    bot.reply_to(message, "Selectează URL-ul pe care vrei să îl elimini:", reply_markup=markup)

@bot.message_handler(commands=['dbstats'])
def db_stats_command(message):
    if not is_admin(message.from_user.id): return
    stats = get_ad_stats()
    response = (
        "📊 Statistici Sistem:\n\n"
        f"Total anunțuri în istoric: {stats['total_ads']}\n"
        f"Anunțuri noi (24h): {stats.get('last_24h', 0)}\n"
        f"În curs de trimitere: {stats.get('unsent_ads', 0)}\n"
        f"Ultima curățenie: {stats['last_cleanup']}\n"
    )
    bot.reply_to(message, response)

@bot.message_handler(commands=['cleanup'])
def cleanup_command(message):
    if not is_admin(message.from_user.id): return
    if cleanup_old_ads():
        bot.reply_to(message, "✅ Baza de date a fost optimizată și curățată.")
    else:
        bot.reply_to(message, "ℹ️ DB este deja curată.")

# --- CALLBACKS PENTRU BUTOANE ---

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Acces refuzat.")
        return
        
    if call.data == "listurl":
        bot.answer_callback_query(call.id)
        list_urls(call.message)
    elif call.data == "addurl":
        bot.answer_callback_query(call.id)
        markup = types.ForceReply(selective=True)
        msg = bot.send_message(call.message.chat.id, "Lipește link-ul OLX.ro aici:", reply_markup=markup)
        bot.register_for_reply(msg, add_url_from_reply)
    elif call.data == "delurl":
        bot.answer_callback_query(call.id)
        delete_url(call.message)
    elif call.data == "dbstats":
        bot.answer_callback_query(call.id)
        db_stats_command(call.message)
    elif call.data.startswith("del_"):
        try:
            index = int(call.data.split("_")[1])
            urls = load_urls()
            if 0 <= index < len(urls):
                removed = urls.pop(index)
                if save_urls(urls):
                    bot.answer_callback_query(call.id, "Șters!")
                    bot.send_message(call.message.chat.id, f"🗑️ Am eliminat: {removed}")
        except:
            bot.answer_callback_query(call.id, "Eroare la ștergere.")

def bot_polling_thread():
    """Funcție de polling pentru Telegram, rulează separat de scaner."""
    while True:
        try:
            bot.polling(none_stop=True, interval=2)
        except Exception as e:
            logging.error(f"Eroare polling bot: {e}")
            time.sleep(5)

# --- FUNCTIA PRINCIPALA ---

def main():
    try:
        os.makedirs('./profiles', exist_ok=True)
        setup_logging()
        init_database()
        cleanup_old_ads()
        
        # Lansăm botul într-un thread separat pentru a răspunde la comenzi în timp ce scanăm
        import threading
        bot_thread = threading.Thread(target=bot_polling_thread, daemon=True)
        bot_thread.start()
        
        urls = load_urls()
        for chat_id in CHAT_IDS:
            try:
                msg = "🚀 Monitor pornire! Caut plăci video pe OLX.ro..." if urls else "🤖 Bot activ! Adaugă un URL pentru a începe scanarea."
                bot.send_message(chat_id, msg)
            except: pass
        
        cycle_counter = 1
        while True:
            try:
                start_time = time.time()
                logging.info(f"--- Început Ciclu #{cycle_counter} ---")
                
                # Verificăm toate link-urile
                fresh_found = quick_check_all_urls()
                
                elapsed = time.time() - start_time
                
                # Timp de așteptare adaptiv (mai rapid dacă găsim ceva nou)
                wait_time = max(5, QUICK_CHECK_INTERVAL - elapsed) if fresh_found else max(10, MIN_INTERVAL - elapsed)
                
                logging.info(f"Ciclu finalizat în {elapsed:.1f}s. Pauză {wait_time:.1f}s.")
                time.sleep(wait_time)
                cycle_counter += 1
                
            except Exception as e:
                logging.error(f"Eroare în bucla principală: {e}")
                time.sleep(15)
                
    except Exception as e:
        logging.critical(f"Eroare CRITICĂ la pornire: {e}")

if __name__ == "__main__":
    main()