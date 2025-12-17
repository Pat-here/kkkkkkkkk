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
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta, timezone
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, constants
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.error import TelegramError, Forbidden

# --- KONFIGURACJA ---
# Token pobierany ze zmiennych środowiskowych (bezpieczniej na Renderze)
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_TOKEN', '5571257868:AAF16UB9_niARzdsScPqwOxNHTDu_T-rSO0')

# Lista monitorowanych adresów URL
OLX_URLS = [
    'https://www.olx.pl/oddam-za-darmo/wroclaw/',
    'https://www.olx.pl/oddam-za-darmo/wroclaw/?search%5Bdist%5D=10&search%5Bfilter_float_price:from%5D=free'
]

# Pliki
DB_FILE = 'olx_bot.db'
BLACKLIST_FILE = 'blacklist.txt'
CHECK_INTERVAL = 300
TIMEZONE_OFFSET = 1  # Czas Polski zimowy (UTC+1). Zmień na 2 latem.

# Logowanie
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
for lib in ["httpx", "telegram", "urllib3"]:
    logging.getLogger(lib).setLevel(logging.WARNING)

logger = logging.getLogger("OLX_Bot")


# --- UTILS ---

def get_pl_time():
    """Zwraca aktualny czas w Polsce."""
    utc = datetime.now(timezone.utc)
    return utc + timedelta(hours=TIMEZONE_OFFSET)


def get_pl_time_str():
    return get_pl_time().strftime('%H:%M')


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


# --- FILTRY ---

def is_valid_offer(title):
    title_lower = title.lower()
    blacklist = load_blacklist()

    found_banned = False
    for phrase in blacklist:
        if phrase in title_lower:
            # Twardy ban dla żebraków
            if any(x in phrase for x in ['szu', 'prz', 'kup', 'potrzeb']):
                return False
            found_banned = True
            break

    if not found_banned:
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


# --- BAZA DANYCH ---

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


def is_seen(oid):
    conn = sqlite3.connect(DB_FILE)
    res = conn.execute("SELECT 1 FROM offers WHERE id = ?", (oid,)).fetchone()
    conn.close()
    return res is not None


def save_offer(oid, title):
    conn = sqlite3.connect(DB_FILE)
    now = get_pl_time().isoformat()
    today = get_pl_time().strftime('%Y-%m-%d')
    try:
        conn.execute("INSERT OR IGNORE INTO offers (id, title, created_at) VALUES (?, ?, ?)", (oid, title, now))
        conn.execute("INSERT OR IGNORE INTO stats (date, count) VALUES (?, 0)", (today,))
        conn.execute("UPDATE stats SET count = count + 1 WHERE date = ?", (today,))
        conn.execute("DELETE FROM offers WHERE id NOT IN (SELECT id FROM offers ORDER BY created_at DESC LIMIT 2000)")
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def get_subs():
    conn = sqlite3.connect(DB_FILE)
    res = conn.execute("SELECT chat_id FROM subs").fetchall()
    conn.close()
    return {r[0] for r in res}


def manage_sub(chat_id, action='add'):
    conn = sqlite3.connect(DB_FILE)
    if action == 'add':
        conn.execute("INSERT OR IGNORE INTO subs (chat_id) VALUES (?)", (chat_id,))
    else:
        conn.execute("DELETE FROM subs WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


def get_todays_count():
    conn = sqlite3.connect(DB_FILE)
    today = get_pl_time().strftime('%Y-%m-%d')
    res = conn.execute("SELECT count FROM stats WHERE date = ?", (today,)).fetchone()
    conn.close()
    return res[0] if res else 0


def get_total_count():
    conn = sqlite3.connect(DB_FILE)
    res = conn.execute("SELECT SUM(count) FROM stats").fetchone()
    conn.close()
    return res[0] if res and res[0] else 0


# --- SILNIK SKANUJĄCY ---

def get_session():
    s = requests.Session()
    retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
    s.mount('https://', HTTPAdapter(max_retries=retries))
    return s


def clean_text(text):
    if not text: return ""
    return re.sub(r'\s+', ' ', text).strip()


def parse_olx_date(raw_text):
    if not raw_text: return "Brak danych", ""
    if ' - ' in raw_text:
        parts = raw_text.split(' - ')
        return parts[0].strip(), parts[1].strip()
    return raw_text, ""


def validate_image_url(url):
    """Naprawia i waliduje URL zdjęcia."""
    if not url: return None
    url = url.strip()
    if url.startswith('//'):
        url = 'https:' + url
    if not url.startswith('http'):
        return None
    return url


def extract_offer_data(a_tag):
    # 1. Znajdź kontener (kartę)
    card = None
    curr = a_tag
    for _ in range(6):
        if not curr.parent: break
        curr = curr.parent
        if curr.name == 'div':
            if curr.find('img') or curr.find('p', attrs={'data-testid': 'location-date'}):
                card = curr
                if curr.get('data-testid') == 'l-card' or curr.get('data-cy') == 'l-card':
                    break
    if not card: card = a_tag.parent

    # 2. Obrazek (z walidacją)
    img_src = None
    img = card.find('img')
    if img:
        src_set = img.get('srcset') or img.get('data-srcset')
        if src_set:
            # Rozdzielamy srcset i bierzemy ostatni element (największy)
            candidates = src_set.split(',')
            best_candidate = candidates[-1].strip().split(' ')[0]
            img_src = validate_image_url(best_candidate)

        if not img_src:
            raw_src = img.get('src') or img.get('data-src')
            img_src = validate_image_url(raw_src)

    # 3. Data
    raw_date_loc = ""
    date_p = card.find('p', attrs={'data-testid': 'location-date'})
    if date_p:
        raw_date_loc = date_p.get_text(strip=True)
    else:
        for p in card.find_all('p'):
            txt = p.get_text(strip=True)
            if ('Dzisiaj' in txt or 'Wczoraj' in txt) and ' - ' in txt:
                raw_date_loc = txt
                break

    location, time_str = parse_olx_date(raw_date_loc)

    return {
        'image': img_src,
        'location': location,
        'time': time_str
    }


async def fetch_olx_offers(pages=1):
    session = get_session()
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }

    offers = []
    seen_in_batch = set()

    # Iteracja po wszystkich zdefiniowanych URL-ach
    for base_url in OLX_URLS:
        for page in range(1, pages + 1):
            # Konstrukcja URL zależna od tego czy base_url ma już parametry
            separator = '&' if '?' in base_url else '?'
            url = f"{base_url}{separator}page={page}" if page > 1 else base_url

            try:
                resp = session.get(url, headers=headers, timeout=10)
                if resp.status_code != 200: continue

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

                    if len(title) < 3 or "zł" in title or "Za darmo" == title:
                        img = a.find('img')
                        if img and img.get('alt'):
                            title = img.get('alt')
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
                logger.error(f"Err fetching {url}: {e}")
                continue

    return offers[::-1]


# --- TELEGRAM ---

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

    kb = [[InlineKeyboardButton("🔗 Zobacz na OLX", url=offer['link'])]]

    try:
        # PRÓBA WYSŁANIA ZDJĘCIA (3 PODEJŚCIA)
        if offer['image']:
            for attempt in range(3):
                try:
                    await bot.send_photo(
                        chat_id,
                        offer['image'],
                        caption=caption,
                        parse_mode='HTML',
                        reply_markup=InlineKeyboardMarkup(kb)
                    )
                    return  # Sukces!
                except Exception as e:
                    logger.warning(f"Foto {offer['id']} próba {attempt + 1}/3 nieudana: {e}")
                    if attempt < 2:
                        await asyncio.sleep(1)  # Odczekaj chwilę przed retry

            logger.warning(f"Foto {offer['id']} - wszystkie próby nieudane. Wysyłam tekst.")

        # FALLBACK TEKSTOWY
        await bot.send_message(chat_id, f"📷 <i>(Zdjęcie niedostępne)</i>\n{caption}", parse_mode='HTML',
                               reply_markup=InlineKeyboardMarkup(kb), disable_web_page_preview=False)

    except Forbidden:
        manage_sub(chat_id, 'remove')
    except Exception as e:
        logger.error(f"Critical Send Err: {e}")


# --- LOGIKA ---

async def check_cycle(bot, manual_chat_id=None, pages=1):
    offers = await fetch_olx_offers(pages=pages)

    if manual_chat_id:
        # MANUAL: Tylko podgląd, bez zapisu do bazy
        count = 0
        for o in offers:
            if not is_valid_offer(o['title']): continue
            if is_seen(o['id']): continue
            await send_offer(bot, manual_chat_id, o, prefix="🔎 <b>Podgląd (Jeszcze nie wysłane):</b>")
            count += 1
            await asyncio.sleep(0.5)
        return count
    else:
        # AUTOMAT: Do wszystkich + zapis
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
            await asyncio.sleep(1.0)
        return count


# --- KOMENDY ---

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    manage_sub(update.effective_chat.id, 'add')
    await update.message.reply_text("👋 <b>Bot aktywny.</b>", parse_mode='HTML')


async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ctx.bot.send_chat_action(update.effective_chat.id, constants.ChatAction.TYPING)
    c = await check_cycle(ctx.bot, manual_chat_id=update.effective_chat.id, pages=1)
    msg = f"✅ Znaleziono {c} nowych." if c > 0 else "💤 Brak nowości na 1. stronie."
    await update.message.reply_text(msg)


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ctx.bot.send_chat_action(update.effective_chat.id, constants.ChatAction.TYPING)
    await update.message.reply_text("📋 Sprawdzam historię (3 strony)...")
    offers = await fetch_olx_offers(pages=3)
    valid = [o for o in offers if is_valid_offer(o['title'])]
    if not valid:
        await update.message.reply_text("Pusto.")
        return
    for o in valid[-3:]:
        await send_offer(ctx.bot, update.effective_chat.id, o, prefix="📂 <b>Z historii:</b>")
        await asyncio.sleep(0.5)


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    today = get_todays_count()
    total = get_total_count()
    subs = len(get_subs())
    await update.message.reply_text(
        f"📊 <b>Statystyki:</b>\n"
        f"Dzisiaj: {today}\n"
        f"Łącznie (cała historia): {total}\n"
        f"Użytkowników: {subs}",
        parse_mode='HTML'
    )


async def cmd_skipjob(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Ukryta komenda do wymuszenia natychmiastowego cyklu automatycznego."""
    await update.message.reply_text("⏩ <b>Wymuszam Joba (Dla wszystkich)...</b>", parse_mode='HTML')
    c = await check_cycle(ctx.bot, manual_chat_id=None, pages=1)  # pages=1 jak w automacie
    await update.message.reply_text(f"✅ Job zakończony. Rozesłano {c} nowych ofert.")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = "⚙️ <b>Menu:</b>\n/sprawdz - podgląd nowych\n/lista - historia\n/stats\n/stop"
    await update.message.reply_text(txt, parse_mode='HTML')


# --- START ---

async def job_loop(ctx: ContextTypes.DEFAULT_TYPE):
    await check_cycle(ctx.bot)


class Health(BaseHTTPRequestHandler):
    def do_GET(s):
        s.send_response(200);
        s.wfile.write(b"OK")


if __name__ == '__main__':
    init_db()
    ensure_files()
    threading.Thread(target=lambda: HTTPServer(('0.0.0.0', int(os.environ.get("PORT", 8080))), Health).serve_forever(),
                     daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", lambda u, c: manage_sub(u.effective_chat.id, 'remove')))
    app.add_handler(CommandHandler("pomoc", cmd_help))
    app.add_handler(CommandHandler("sprawdz", cmd_check))
    app.add_handler(CommandHandler("lista", cmd_list))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("skipjob", cmd_skipjob))  # Ukryta komenda

    app.job_queue.run_repeating(job_loop, interval=CHECK_INTERVAL, first=10)
    print("Bot uruchomiony.")
    app.run_polling()