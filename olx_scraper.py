import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
import sqlite3
import os
import asyncio
import logging
import random
import threading
import re
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta, timezone
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, constants
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# --- KONFIGURACJA ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_TOKEN', '5571257868:AAF5ZYyKWj0W4YnFJoOsLxU_sji_8iUXMhg')

OLX_URLS = [
    'https://www.olx.pl/oddam-za-darmo/wroclaw/',
    'https://www.olx.pl/oddam-za-darmo/wroclaw/?search%5Bdist%5D=10&search%5Bfilter_float_price:from%5D=free'
]

DB_FILE = 'olx_bot.db'
BLACKLIST_FILE = 'blacklist.txt'
CHECK_INTERVAL = 300
TIMEZONE_OFFSET = 1 

SESSION_ID = str(uuid.uuid4())[:8]

# Logowanie
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
# Wyciszamy gadatliwe biblioteki
for lib in ["telegram", "urllib3", "requests"]:
    logging.getLogger(lib).setLevel(logging.WARNING)

logger = logging.getLogger(f"Bot-{SESSION_ID}")

# --- USER AGENTS & HEADERS (Kluczowe dla ominięcia 403) ---
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0'
]

def get_headers():
    return {
        'User-Agent': random.choice(USER_AGENTS),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Referer': 'https://www.google.com/'
    }

# --- BAZA DANYCH & PLIKI ---

def ensure_files():
    if not os.path.exists(BLACKLIST_FILE):
        default_bl = [
            'szukam', 'poszukuję', 'przyjmę', 'kupię', 'potrzebuję',
            'kot ', 'kocur', 'kicia', 'pies', 'szczeniak', 'chomik',
            'rybki', 'akwarium', 'zwierz', 'glonojad'
        ]
        with open(BLACKLIST_FILE, 'w', encoding='utf-8') as f:
            f.write('\n'.join(default_bl))

def load_blacklist():
    ensure_files()
    try:
        with open(BLACKLIST_FILE, 'r', encoding='utf-8') as f:
            return [line.strip().lower() for line in f if line.strip()]
    except:
        return []

def init_db():
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute('''CREATE TABLE IF NOT EXISTS offers (id TEXT PRIMARY KEY, title TEXT, created_at TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS stats (date TEXT PRIMARY KEY, count INTEGER)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS subs (chat_id INTEGER PRIMARY KEY)''')
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"DB Init Error: {e}")

# --- UTILS CZASOWE ---

def get_pl_time():
    utc = datetime.now(timezone.utc)
    return utc + timedelta(hours=TIMEZONE_OFFSET)

def get_pl_time_str():
    return get_pl_time().strftime('%H:%M')

# --- LOGIKA BAZODANOWA ---

def is_seen(oid):
    with sqlite3.connect(DB_FILE) as conn:
        res = conn.execute("SELECT 1 FROM offers WHERE id = ?", (oid,)).fetchone()
    return res is not None

def save_offer(oid, title):
    now = get_pl_time().isoformat()
    today = get_pl_time().strftime('%Y-%m-%d')
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("INSERT OR IGNORE INTO offers (id, title, created_at) VALUES (?, ?, ?)", (oid, title, now))
            conn.execute("INSERT OR IGNORE INTO stats (date, count) VALUES (?, 0)", (today,))
            conn.execute("UPDATE stats SET count = count + 1 WHERE date = ?", (today,))
            conn.execute("DELETE FROM offers WHERE id NOT IN (SELECT id FROM offers ORDER BY created_at DESC LIMIT 2000)")
            conn.commit()
    except Exception as e:
        logger.error(f"DB Save Error: {e}")

def get_subs():
    with sqlite3.connect(DB_FILE) as conn:
        res = conn.execute("SELECT chat_id FROM subs").fetchall()
    return {r[0] for r in res}

def manage_sub(chat_id, action='add'):
    with sqlite3.connect(DB_FILE) as conn:
        if action == 'add':
            conn.execute("INSERT OR IGNORE INTO subs (chat_id) VALUES (?)", (chat_id,))
        else:
            conn.execute("DELETE FROM subs WHERE chat_id = ?", (chat_id,))
        conn.commit()

def get_stats_data():
    today = get_pl_time().strftime('%Y-%m-%d')
    with sqlite3.connect(DB_FILE) as conn:
        today_cnt = conn.execute("SELECT count FROM stats WHERE date = ?", (today,)).fetchone()
        total_cnt = conn.execute("SELECT SUM(count) FROM stats").fetchone()
        subs_cnt = conn.execute("SELECT COUNT(*) FROM subs").fetchone()
    
    return {
        'today': today_cnt[0] if today_cnt else 0,
        'total': total_cnt[0] if total_cnt and total_cnt[0] else 0,
        'subs': subs_cnt[0] if subs_cnt else 0
    }

# --- FILTRY ---

def is_valid_offer(title):
    title_lower = title.lower()
    blacklist = load_blacklist()
    
    for phrase in blacklist:
        if phrase in title_lower:
            if any(x in phrase for x in ['szu', 'prz', 'kup', 'potrzeb']):
                return False
            break 
    else:
        return True

    safe_words = [
        'dla', 'smycz', 'obroża', 'klatka', 'transporter', 'kuweta', 'karma',
        'żwirek', 'jedzenie', 'miska', 'ubranko', 'zabawka', 'drapak',
        'legowisko', 'buda', 'budka', 'akwarium', 'filtr', 'grzałka', 'ozdoba',
        'rośliny', 'akcesoria', 'szelki', 'książka', 'figurka', 'maskotka',
        'pluszak', 'obraz', 'puzzle', 'gra '
    ]

    for safe in safe_words:
        if safe in title_lower:
            return True

    return False

# --- PARSING ---

def clean_text(text):
    if not text: return ""
    return re.sub(r'\s+', ' ', text).strip()

def validate_image_url(url):
    if not url: return None
    url = url.strip()
    if url.startswith('//'): url = 'https:' + url
    if not url.startswith('http'): return None
    return url

def extract_offer_data(a_tag):
    card = a_tag
    for _ in range(6):
        if not card.parent: break
        card = card.parent
        if card.name == 'div' and (card.get('data-testid') == 'l-card' or card.get('data-cy') == 'l-card'):
            break
            
    img_src = None
    img = card.find('img')
    if img:
        src_set = img.get('srcset') or img.get('data-srcset')
        if src_set:
            img_src = src_set.split(',')[-1].strip().split(' ')[0]
        if not img_src:
            img_src = img.get('src') or img.get('data-src')
    
    img_src = validate_image_url(img_src)

    raw_info = ""
    date_p = card.find('p', attrs={'data-testid': 'location-date'})
    if date_p:
        raw_info = date_p.get_text(strip=True)
    
    if ' - ' in raw_info:
        parts = raw_info.split(' - ')
        location, time_str = parts[0].strip(), parts[1].strip()
    else:
        location, time_str = raw_info, ""

    return {'image': img_src, 'location': location, 'time': time_str}

# --- SYNCHRONICZNY SCRAPING (W WĄTKU) ---

def fetch_offers_sync(pages=1):
    """Funkcja synchroniczna używająca requests, uruchamiana w wątku."""
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    session.mount('https://', HTTPAdapter(max_retries=retries))
    
    offers = []
    seen_in_batch = set()

    for base_url in OLX_URLS:
        for page in range(1, pages + 1):
            separator = '&' if '?' in base_url else '?'
            url = f"{base_url}{separator}page={page}" if page > 1 else base_url
            
            try:
                # Używamy bogatych nagłówków
                resp = session.get(url, headers=get_headers(), timeout=10)
                
                if resp.status_code == 403:
                    logger.warning(f"OLX 403 (Access Denied) dla {url} - Może być wymagana zmiana IP lub UserAgent")
                    continue
                if resp.status_code != 200:
                    logger.warning(f"OLX zwrócił {resp.status_code} dla {url}")
                    continue

                soup = BeautifulSoup(resp.content, 'html.parser')
                links = soup.find_all('a', href=re.compile(r'/d/.*'))

                for a in links:
                    href = a['href']
                    if any(x in href for x in ['otodom', 'fixly', 'promowane']): continue

                    full_link = f"https://www.olx.pl{href}" if not href.startswith('http') else href
                    
                    try:
                        oid = full_link.split('-ID')[-1].split('.')[0]
                    except:
                        oid = full_link[-10:]

                    if oid in seen_in_batch: continue
                    seen_in_batch.add(oid)

                    title_tag = a.find(['h6', 'h4'])
                    title = clean_text(title_tag.text) if title_tag else clean_text(a.text)
                    
                    if len(title) < 3 or "zł" in title or title == "Za darmo":
                            if a.find('img') and a.find('img').get('alt'):
                                title = a.find('img').get('alt')
                            else:
                                continue

                    details = extract_offer_data(a)
                    
                    offers.append({
                        'id': oid,
                        'title': title,
                        'link': full_link,
                        **details
                    })
            except Exception as e:
                logger.error(f"Błąd pobierania {url}: {e}")
                
    return offers[::-1]

# --- ASYNC WRAPPER ---

async def fetch_olx_offers(pages=1):
    # Uruchomienie starego dobrego requests w osobnym wątku, żeby nie blokował bota
    return await asyncio.to_thread(fetch_offers_sync, pages=pages)

# --- TELEGRAM SENDING ---

async def send_offer(bot, chat_id, offer, prefix="🔥 <b>Nowa okazja!</b>"):
    time_display = offer['time'] if offer['time'] else "Świeże"
    caption = (
        f"{prefix}\n\n"
        f"📦 <b>{offer['title']}</b>\n"
        f"📍 <b>{offer['location']}</b>\n"
        f"🕒 {time_display}\n"
        f"➖➖➖➖➖➖➖\n"
        f"<i>Znaleziono: {get_pl_time_str()}</i>"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Zobacz na OLX", url=offer['link'])]])

    try:
        if offer['image']:
            try:
                await bot.send_photo(chat_id, offer['image'], caption=caption, parse_mode='HTML', reply_markup=kb)
                return
            except Exception:
                pass 

        await bot.send_message(chat_id, caption, parse_mode='HTML', reply_markup=kb, disable_web_page_preview=False)

    except Exception as e:
        logger.error(f"Nie udało się wysłać do {chat_id}: {e}")
        if "Forbidden" in str(e):
            manage_sub(chat_id, 'remove')

# --- LOGIKA BOTA ---

async def check_cycle(bot, manual_chat_id=None, pages=1):
    offers = await fetch_olx_offers(pages=pages)
    
    if manual_chat_id:
        count = 0
        for o in offers:
            if not is_valid_offer(o['title']): continue
            if is_seen(o['id']): continue
            await send_offer(bot, manual_chat_id, o, prefix="🔎 <b>Podgląd (Test):</b>")
            count += 1
            await asyncio.sleep(0.3)
        return count
    else:
        subs = get_subs()
        if not subs: return 0
        
        count = 0
        for o in offers:
            if is_seen(o['id']): continue
            if is_valid_offer(o['title']):
                for cid in subs:
                    await send_offer(bot, cid, o)
                count += 1
            save_offer(o['id'], o['title'])
            await asyncio.sleep(0.5)
        
        if count > 0:
            logger.info(f"Znaleziono i wysłano {count} nowych ofert.")
        return count

async def job_loop(ctx: ContextTypes.DEFAULT_TYPE):
    await check_cycle(ctx.bot)

# --- KOMENDY ---

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    manage_sub(update.effective_chat.id, 'add')
    await update.message.reply_text(f"👋 <b>Bot aktywny!</b>\nSesja: <code>{SESSION_ID}</code>", parse_mode='HTML')

async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ctx.bot.send_chat_action(update.effective_chat.id, constants.ChatAction.TYPING)
    c = await check_cycle(ctx.bot, manual_chat_id=update.effective_chat.id)
    msg = f"✅ Znaleziono {c} nowych." if c > 0 else "💤 Brak nowości."
    await update.message.reply_text(msg)

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_stats_data()
    await update.message.reply_text(
        f"📊 <b>Statystyki (Sesja {SESSION_ID}):</b>\n"
        f"Dziś znaleziono: {s['today']}\n"
        f"Łącznie w bazie: {s['total']}\n"
        f"Subskrybentów: {s['subs']}",
        parse_mode='HTML'
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚙️ <b>Menu:</b>\n/sprawdz - wymuś sprawdzenie\n/stats - statystyki\n/stop - wyłącz powiadomienia",
        parse_mode='HTML'
    )

# --- HEALTHCHECK ---
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(f"Bot OK. Session: {SESSION_ID}".encode())
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

if __name__ == '__main__':
    init_db()
    ensure_files()

    port = int(os.environ.get("PORT", 8080))
    threading.Thread(
        target=lambda: HTTPServer(('0.0.0.0', port), Health).serve_forever(),
        daemon=True
    ).start()

    logger.info(f"--- START BOTA (Sesja: {SESSION_ID}) ---")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", lambda u, c: manage_sub(u.effective_chat.id, 'remove')))
    app.add_handler(CommandHandler("pomoc", cmd_help))
    app.add_handler(CommandHandler("sprawdz", cmd_check))
    app.add_handler(CommandHandler("stats", cmd_stats))

    if app.job_queue:
        app.job_queue.run_repeating(job_loop, interval=CHECK_INTERVAL, first=10)
    
    app.run_polling(drop_pending_updates=True)
