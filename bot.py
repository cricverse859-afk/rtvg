# Pehle ye install karein (Kaggle cell me):
# pip install pyrogram tgcrypto cloudscraper beautifulsoup4
# pip install opencv-python-headless

import os
import re
import logging
import json
import time
import asyncio
import shutil
import tempfile
import zipfile
from urllib.parse import urlparse
from pyrogram import Client, filters, idle
from pyrogram.errors import FloodWait
try:
    from pyrogram import enums
    PM_OFF = enums.ParseMode.DISABLED      # ZIP status me markdown parse na ho (naam me _ * [ ] aa sakte hain)
except Exception:
    PM_OFF = None
try:
    from pyrogram import StopTransmission
except Exception:                          # alag Pyrogram fork ke liye
    class StopTransmission(Exception):
        pass
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message, BotCommand
import cloudscraper
from bs4 import BeautifulSoup
import cv2

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')

# --- CONFIGURATION ---
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

app = Client(
    "anime_downloader_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

user_episodes_data = {}
monitored_tasks = {}
bot_stats = {"downloads_completed": 0, "start_time": time.time()}
ITEMS_PER_PAGE = 15 

# --- ZIP SETTINGS (server1 se shift hui ZIP feature ki settings) ---
WORK_DIR = os.getenv("WORK_DIR", "/tmp/rarebot_work")           # ZIP ki temp files yahin banengi
ZIP_PART_MB = int(os.getenv("ZIP_PART_MB", "1900"))           # Telegram bot upload limit ~2000MB; isse chhota rakho
ZIP_MAX_PENDING = 1                                           # upload ke intezaar me kitne ready part rakhne hain (disk bachane ke liye)
VIDEO_EXTS = {"mp4", "mkv", "avi", "webm", "mov", "m4v", "ts", "flv"}
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
zip_jobs = {}        # chat_id -> {"cancel": bool, ...}
zip_tasks = set()    # background tasks ka reference (garbage collect na ho)
_zip_sem = None

@app.on_message(filters.command("start"))
async def send_welcome(client, message: Message):
    welcome_text = (
        "👋 **Welcome to Advanced Anime Downloader Bot!**\n\n"
        "Commands:\n"
        "• **Normal Link:** Sirf link bhejein, episodes ki button list mil jayegi.\n"
        "• `/single <url_ya_ep_no>` - Kisi single episode ke liye.\n"
        "• `/batch <url> <start>-<end>` - Range ke mutabiq download ke liye.\n"
        "• `/allep <url>` - Uss page ke saare episodes ek sath download karne ke liye.\n"
        "• `/monitor <url>` - Har 3 minute mein naye episodes ke liye auto-check.\n"
        "• `/monitored` - Active monitored URLs list (with interactive Remove buttons).\n"
        "• `/remove <url_ya_number>` - Manual remove.\n"
        "• `/stopmonitor <url>` - Background monitoring rokne ke liye.\n"
        "• `/zip <url> [start-end] [720p]` - Episodes ko quality-wise ZIP banakar yahin upload karta hai.\n"
        "• `/cancelzip` - Chalta hua ZIP job rokne ke liye.\n"
        "• `/stats` - Bot ki performance dekhne ke liye."
    )
    await message.reply_text(welcome_text)

@app.on_message(filters.command("stats"))
async def bot_stats_cmd(client, message: Message):
    uptime_seconds = int(time.time() - bot_stats["start_time"])
    hours = uptime_seconds // 3600
    minutes = (uptime_seconds % 3600) // 60
    
    stats_text = (
        "📊 **Bot Performance Monitor**\n"
        "══════════════════\n"
        f"🟢 Status: `Active & Running`\n"
        f"⏱️ Uptime: `{hours}h {minutes}m`\n"
        f"📥 Total Downloads: `{bot_stats['downloads_completed']}`\n"
        f"👁️ Active Monitors: `{len(monitored_tasks)}`\n"
        "══════════════════"
    )
    await message.reply_text(stats_text)

async def fetch_episodes_from_url(url):
    try:
        scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True})
        response = scraper.get(url, timeout=30)
        soup = BeautifulSoup(response.content, 'html.parser')
        
        episodes_list = []
        current_ep_name = "Unknown Episode"
        current_server = "Default"
        
        for p_tag in soup.find_all('p'):
            text = p_tag.get_text(strip=True)
            if text.lower().startswith('episode'):
                current_ep_name = text
                current_server = "Default"
            
            if 'Crunchyroll' in text:
                current_server = "Crunchyroll"
            elif 'AnimeTimes' in text:
                current_server = "AnimeTimes"
            elif 'Tamil' in text:
                current_server = "Tamil"
            elif 'Telugu' in text:
                current_server = "Telugu"
                
            for a_tag in p_tag.find_all('a'):
                link_text = a_tag.get_text(strip=True)
                if 'WatchMult' in link_text:
                    link_href = a_tag.get('href')
                    if link_href and 'http' in link_href:
                        display_name = f"{current_ep_name} [{current_server}]"
                        if not any(ep['link'] == link_href for ep in episodes_list):
                            episodes_list.append({'name': display_name, 'link': link_href})
                        break
        return episodes_list
    except Exception as e:
        print(f"Scraping Error: {e}")
        return []

@app.on_message(filters.command("single"))
async def single_download_command(client, message: Message):
    chat_id = message.chat.id
    args = message.text.split(maxsplit=1)
    
    if len(args) < 2:
        await message.reply_text("⚠️ Sahi tareeqa use karein: `/single <url_ya_ep_number>`")
        return
        
    target_input = args[1].strip()
    
    if target_input.startswith("http"):
        msg = await message.reply_text("🔍 URL se episode fetch kiya ja raha hai...")
        eps = await fetch_episodes_from_url(target_input)
        if not eps:
            await msg.edit_text("❌ Is URL par koi episodes nahi mile.")
            return
        target_ep = eps[0]
        await msg.edit_text(f"🚀 **{target_ep['name']}** download shuru ho raha hai...")
        asyncio.create_task(process_and_send_telegram(client, chat_id, target_ep['link'], target_ep['name']))
    else:
        if chat_id not in user_episodes_data or not user_episodes_data[chat_id]:
            await message.reply_text("❌ Pehle koi main page link bhejein ya URL ke sath command dein.")
            return
            
        if not target_input.isdigit():
            await message.reply_text("⚠️ Episode number sirf digit hona chahiye.")
            return
            
        ep_num = int(target_input)
        episodes = user_episodes_data[chat_id]
        target_ep = None
        
        for ep in episodes:
            if f"episode {ep_num}" in ep['name'].lower() or f"ep {ep_num}" in ep['name'].lower():
                target_ep = ep
                break
                
        if not target_ep and 0 < ep_num <= len(episodes):
            target_ep = episodes[ep_num - 1]
            
        if not target_ep:
            await message.reply_text(f"❌ Episode {ep_num} list mein nahi mila.")
            return
            
        await message.reply_text(f"🚀 **{target_ep['name']}** download shuru ho raha hai...")
        asyncio.create_task(process_and_send_telegram(client, chat_id, target_ep['link'], target_ep['name']))

@app.on_message(filters.command("batch"))
async def batch_download_command(client, message: Message):
    chat_id = message.chat.id
    args = message.text.split()
    
    if len(args) < 3:
        await message.reply_text("⚠️ Sahi format use karein: `/batch <url> <start>-<end>`")
        return
        
    url = args[1]
    range_str = args[2]
    
    if "-" not in range_str:
        await message.reply_text("⚠️ Range ka format galat hai. Istemaal karein: `1-6`")
        return
        
    try:
        start_ep, end_ep = map(int, range_str.split("-"))
    except ValueError:
        await message.reply_text("⚠️ Range mein sirf numbers hone chahiye.")
        return
        
    msg = await message.reply_text("🔍 Batch episodes fetch kiye ja rahe hain...")
    episodes = await fetch_episodes_from_url(url)
    
    if not episodes:
        await msg.edit_text("❌ URL se episodes extract nahi ho paye.")
        return
        
    selected_eps = [ep for idx, ep in enumerate(episodes, start=1) if start_ep <= idx <= end_ep]
    
    if not selected_eps:
        await msg.edit_text("❌ Di gayi range mein koi episodes nahi mile.")
        return
        
    await msg.edit_text(f"📦 **Batch Download Started!**\nRange: {start_ep} se {end_ep}")
    
    for idx, ep in enumerate(selected_eps, start=1):
        try:
            await msg.edit_text(f"📦 **Batch Progress ({idx}/{len(selected_eps)})**\nProcessing: `{ep['name']}`")
            await process_and_send_telegram(client, chat_id, ep['link'], ep['name'], is_batch=True)
            await asyncio.sleep(3)
        except Exception as e:
            print(f"Batch Error: {e}")
            
    await msg.edit_text("✅ **Batch Download Completed Successfully!** 🎉")

@app.on_message(filters.command("allep"))
async def all_episodes_command(client, message: Message):
    chat_id = message.chat.id
    args = message.text.split(maxsplit=1)
    
    if len(args) < 2:
        await message.reply_text("⚠️ Sahi format use karein: `/allep <url>`")
        return
        
    url = args[1].strip()
    msg = await message.reply_text("🔍 Saare episodes fetch kiye ja rahe hain...")
    
    episodes = await fetch_episodes_from_url(url)
    if not episodes:
        await msg.edit_text("❌ Koi episodes nahi mile.")
        return
        
    await msg.edit_text(f"📦 **All Episodes Download Started!** Total: {len(episodes)} episodes.")
    
    for idx, ep in enumerate(episodes, start=1):
        try:
            await msg.edit_text(f"📦 **All-EP Progress ({idx}/{len(episodes)})**\nProcessing: `{ep['name']}`")
            await process_and_send_telegram(client, chat_id, ep['link'], ep['name'], is_batch=True)
            await asyncio.sleep(3)
        except Exception as e:
            print(f"All-EP Error: {e}")
            
    await msg.edit_text("✅ **Saare episodes successfully download ho gaye!** 🎉")

@app.on_message(filters.command("monitor"))
async def monitor_command(client, message: Message):
    chat_id = message.chat.id
    args = message.text.split(maxsplit=1)
    
    if len(args) < 2:
        await message.reply_text("⚠️ Sahi format use karein: `/monitor <url>`")
        return
        
    url = args[1].strip()
    if url in monitored_tasks:
        await message.reply_text("⚠️ Yeh URL pehle se monitor ho raha hai.")
        return
        
    msg = await message.reply_text("🔍 Monitoring set ki ja rahi hai, purane episodes scan ho rahe hain...")
    
    initial_eps = await fetch_episodes_from_url(url)
    if not initial_eps:
        await msg.edit_text("❌ URL se episodes load nahi ho paye, isliye monitoring shuru nahi ho saki.")
        return

    task = asyncio.create_task(background_monitor_task(client, chat_id, url, initial_eps))
    monitored_tasks[url] = task
    
    await msg.edit_text(
        f"👁️ **Monitoring Activated Successfully!**\n"
        f"• Total current episodes found: `{len(initial_eps)}` (Inhe ignore kar diya gaya hai)\n"
        f"• Bot ab har 3 minute mein check karega aur **sirf naya episode aane par** hi download karega."
    )

@app.on_message(filters.command("monitored"))
async def monitored_list_command(client, message: Message):
    if not monitored_tasks:
        await message.reply_text("📭 Filhal koi bhi URL monitor nahi ho raha hai.")
        return
        
    keyboard = []
    text = "📋 **Currently Monitored URLs:**\n\n"
    
    for idx, url in enumerate(list(monitored_tasks.keys()), start=1):
        text += f"{idx}. `{url}`\n"
        keyboard.append([InlineKeyboardButton(f"❌ Remove #{idx}", callback_data=f"rem_idx_{idx-1}")])
        
    markup = InlineKeyboardMarkup(keyboard)
    await message.reply_text(text, reply_markup=markup)

@app.on_message(filters.command("remove"))
async def remove_monitored_command(client, message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply_text("⚠️ Sahi format use karein: `/remove <number>` ya `/remove <url>`")
        return
        
    target = args[1].strip()
    urls_list = list(monitored_tasks.keys())
    
    removed_url = None
    if target.isdigit():
        idx = int(target) - 1
        if 0 <= idx < len(urls_list):
            removed_url = urls_list[idx]
    else:
        if target in monitored_tasks:
            removed_url = target
            
    if removed_url:
        monitored_tasks[removed_url].cancel()
        del monitored_tasks[removed_url]
        await message.reply_text(f"✅ Success! Monitoring hata di gayi hai:\n`{removed_url}`")
    else:
        await message.reply_text("❌ Diya gaya number ya URL monitoring list mein nahi mila.")

@app.on_message(filters.command("stopmonitor"))
async def stop_monitor_command(client, message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply_text("⚠️ Sahi format use karein: `/stopmonitor <url>`")
        return
    url = args[1].strip()
    if url in monitored_tasks:
        monitored_tasks[url].cancel()
        del monitored_tasks[url]
        await message.reply_text("🛑 Monitoring roki ja chuki hai.")
    else:
        await message.reply_text("❌ Yeh URL monitoring list mein nahi mila.")


class BypassError(Exception):
    """Link bypass ke kisi step me problem (message seedha user ko dikhane layak)."""


def make_scraper():
    return cloudscraper.create_scraper(browser={
        'browser': 'chrome',
        'platform': 'windows',
        'desktop': True
    })


def extract_urls_from_json(data):
    urls = []
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, str) and v.startswith("http"):
                urls.append({"name": str(k), "url": v})
            else:
                urls.extend(extract_urls_from_json(v))
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                name = item.get("name") or item.get("label") or "download_file"
                url = item.get("url") or item.get("link") or item.get("file")
                if url and isinstance(url, str) and url.startswith("http"):
                    urls.append({"name": name, "url": url})
                else:
                    urls.extend(extract_urls_from_json(item))
            elif isinstance(item, str) and item.startswith("http"):
                urls.append({"name": "download_file", "url": item})
    return urls


def resolve_download_links(scraper, watch_url, ep_name):
    """Zipper -> Ziptron -> Argon bypass. (parsed_links, argon_url) deta hai, fail par BypassError."""
    headers = {"User-Agent": USER_AGENT}

    resp = scraper.get(watch_url, timeout=30)
    sub_soup = BeautifulSoup(resp.content, 'html.parser')
    zipper_link = None

    for a_tag in sub_soup.find_all('a', class_='btn-dl'):
        href = a_tag.get('href')
        if href and ('animetoonhindi.com/zipper/' in href or 'codedew.com/zipper/' in href or '/zipper/' in href):
            zipper_link = href
            break

    if not zipper_link:
        raise BypassError(f"⚠️ {ep_name}: Zipper link nahi mila.")

    parsed_zipper = urlparse(zipper_link)
    base_origin = f"{parsed_zipper.scheme}://{parsed_zipper.netloc}" if parsed_zipper.scheme and parsed_zipper.netloc else "https://animetoonhindi.com"

    res1 = scraper.get(zipper_link, headers=headers, timeout=30)
    soup1 = BeautifulSoup(res1.text, 'html.parser')
    btn1 = soup1.find('a', id='goBtn')
    if not btn1 or not btn1.get('data-href'):
        raise BypassError(f"❌ {ep_name}: Step 1 link nahi mila.")

    step2_path = btn1.get('data-href')
    step2_url = base_origin + step2_path if step2_path.startswith('/') else step2_path

    res2 = scraper.get(step2_url, headers=headers, timeout=30)
    soup2 = BeautifulSoup(res2.text, 'html.parser')
    btn2 = soup2.find('a', id='goBtn')
    if not btn2 or not btn2.get('data-href'):
        raise BypassError(f"❌ {ep_name}: Ziptron link nahi mila.")

    ziptron_url = btn2.get('data-href')

    res3 = scraper.get(ziptron_url, headers=headers, timeout=30)
    soup3 = BeautifulSoup(res3.text, 'html.parser')
    form = soup3.find('form', id='landing')
    if not form:
        raise BypassError(f"❌ {ep_name}: Form tag nahi mila.")

    action_url = form.get('action')
    input_tag = form.find('input', {'name': 'rtiwatch'})
    token_val = input_tag.get('value') if input_tag else ""

    post_headers = {
        "Referer": ziptron_url,
        "Origin": base_origin,
        "User-Agent": headers["User-Agent"],
        "X-Requested-With": "XMLHttpRequest"
    }

    res4 = scraper.post(action_url, data={"rtiwatch": token_val}, headers=post_headers, timeout=30)
    argon_match = re.search(r'https://argon\.razorshell\.space/downlead/[^\s"\'<]+', res4.text)
    if not argon_match:
        raise BypassError(f"❌ {ep_name}: Argon link generate nahi hua.")

    argon_url = argon_match.group(0)

    res_argon = scraper.get(argon_url, headers=headers, timeout=30)
    html_text = res_argon.text
    start_str = "window.juicyData ="
    parsed_links = []

    if start_str in html_text:
        try:
            json_part = html_text.split(start_str, 1)[1]
            json_str = json_part.split("</script>", 1)[0].strip().rstrip(";")
            juicy_data = json.loads(json_str)

            data_obj = juicy_data.get("data", {})
            links_route = data_obj.get("routes", {}).get("links")
            token = data_obj.get("token", "")

            if links_route:
                api_url = "https://argon.razorshell.space" + links_route
                api_headers = {
                    "Referer": argon_url,
                    "Origin": "https://argon.razorshell.space",
                    "User-Agent": headers["User-Agent"],
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/javascript, */*; q=0.01"
                }
                payload = {"_token": token} if token else {}
                api_res = scraper.post(api_url, data=payload, headers=api_headers, timeout=30)

                try:
                    links_data = api_res.json()
                    parsed_links = extract_urls_from_json(links_data)
                except Exception:
                    pass
        except Exception:
            pass

    if not parsed_links:
        raise BypassError(f"⚠️ {ep_name}: Koi download links nahi mile.")

    return parsed_links, argon_url


# =====================================================================
# 📦 ZIP BUILDER  (server1 se yahan shift kiya gaya)
# ---------------------------------------------------------------------
# /zip <url> [start-end] [480p|720p|1080p]
#   * Har episode ke links ek baar bypass hote hain, phir har quality ki file download hoti hai
#   * Har quality ka alag ZIP banta hai (STORED = compression nahi, video pehle se compressed hota hai)
#   * ZIP_PART_MB se bada hone par naya part; part N upload hota hai tab part N+1 ban raha hota hai
#   * Upload isi chat me hota hai. Caption me "720p EP 1-12" hota hai -> ZIP ko server1 bot me
#     forward karo, wo quality + range padh kar Firebase me save kar dega (server1 me sirf save ka logic hai)
# =====================================================================

class ZipCancelled(Exception):
    pass


# ── formatting helpers (fastzip.py se) ────────────────

def human_size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return "0 B"


def fmt_dur(sec) -> str:
    if sec is None or sec != sec or sec < 0:
        return "calculating…"
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def fmt_speed(bps: float) -> str:
    return f"{bps / 1048576:.1f} MB/s" if bps else "—"


def make_bar(pct: float, width: int = 14) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(width * pct / 100)
    return "█" * filled + "░" * (width - filled)


class _Speed:
    """Smoothed (EMA) speed meter. 8 sec se zyada data na aaye to speed 0 dikhegi."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.ema, self.t, self.b, self.last_feed = 0.0, None, 0, 0.0

    def feed(self, cum_bytes: int):
        now = time.time()
        if self.t is None:
            self.t, self.b, self.last_feed = now, cum_bytes, now
            return
        dt = now - self.t
        if dt >= 1.0:
            inst = (cum_bytes - self.b) / dt
            self.ema = inst if self.ema == 0 else 0.3 * inst + 0.7 * self.ema
            self.t, self.b, self.last_feed = now, cum_bytes, now

    def value(self) -> float:
        if self.last_feed and time.time() - self.last_feed > 8:
            return 0.0
        return self.ema


# ── progress dashboard ────────────────────────────────

class ZipProgress:
    """Download/zip (worker thread) aur upload (async) ek saath chalte hain, isliye dono ki alag state."""

    def __init__(self, slug: str, n_eps: int, only_q=None):
        self.slug, self.n_eps, self.only_q = slug, n_eps, only_q
        self.started = time.time()
        self.ep_i, self.eps_done, self.ep_name = 0, 0, ""
        self.stage = "starting"
        self.file_label, self.file_done, self.file_total = "", 0, 0
        self.dl_cum = 0
        self.dl_speed = _Speed()
        self.parts_built = 0
        self.parts_uploaded = 0
        self.up_name, self.up_done, self.up_total = "", 0, 0
        self.up_speed = _Speed()
        self.last_activity = time.time()
        self.note = ""
        self.failures: list = []

    def touch(self, note: str = ""):
        self.last_activity = time.time()
        self.note = note

    def set_note(self, note: str):
        self.note = note            # retry / wait message (activity time nahi badhata)

    # worker side
    def begin_episode(self, i: int, name: str):
        self.ep_i, self.ep_name, self.stage = i, name, "resolving"
        self.file_label, self.file_done, self.file_total = "", 0, 0
        self.touch()

    def end_episode(self):
        self.eps_done = self.ep_i
        self.touch()

    def start_file(self, label: str, total: int):
        self.stage, self.file_label = "downloading", label
        self.file_done, self.file_total = 0, total
        self.dl_speed.reset()
        self.touch()

    def add_dl(self, n: int):
        self.file_done += n
        self.dl_cum += n
        self.dl_speed.feed(self.dl_cum)
        self.touch()

    # upload side
    def start_upload(self, name: str, total: int):
        self.up_name, self.up_done, self.up_total = name, 0, max(total, 1)
        self.up_speed.reset()
        self.touch()

    def set_upload(self, cur: int, total: int):
        self.up_done = cur
        self.up_total = total or self.up_total
        self.up_speed.feed(cur)
        self.touch()

    def finish_upload(self):
        self.parts_uploaded += 1
        self.up_name = ""

    def eta(self):
        if self.eps_done <= 0:
            return None
        per_ep = (time.time() - self.started) / self.eps_done
        return per_ep * max(self.n_eps - self.eps_done, 0)

    def live_line(self) -> str:
        idle = time.time() - self.last_activity
        clock = time.strftime("%H:%M:%S")
        if idle > 12:
            why = self.note or "Slow hai, retry chal raha hai"
            return f"🟡 {why} ({int(idle)}s se naya data nahi) • {clock}"
        return f"🟢 Live • {clock}"

    def render(self) -> str:
        overall = self.eps_done * 100 / max(self.n_eps, 1)
        q_txt = f" • {self.only_q}" if self.only_q else " • all qualities"
        lines = [
            f"📦 ZIP BUILDER — {self.slug}{q_txt}",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"📊 Overall   {make_bar(overall)}  {overall:.0f}%",
            f"⏱ Elapsed {fmt_dur(time.time() - self.started)}   •   ⏳ ETA {fmt_dur(self.eta())}",
            f"🎬 Episode {self.ep_i}/{self.n_eps} ({self.eps_done} done)",
            f"🧩 Parts: {self.parts_built} built • {self.parts_uploaded} uploaded",
            "",
        ]
        if self.ep_name:
            lines.append(f"📄 {self.ep_name}")
        if self.stage == "resolving":
            lines.append("🔗 Links bypass ho rahe hain...")
        elif self.stage in ("downloading", "zipping") and self.file_label:
            if self.stage == "downloading":
                pct = (self.file_done * 100 / self.file_total) if self.file_total else 0
                lines += [
                    f"⬇️ Download — {self.file_label}",
                    f"{make_bar(pct)}  {pct:.0f}%" if self.file_total else "…",
                    f"💾 {human_size(self.file_done)} / {human_size(self.file_total) if self.file_total else '?'}   ⚡ {fmt_speed(self.dl_speed.value())}",
                ]
            else:
                lines.append(f"🗜 ZIP me likh raha hu — {self.file_label}")
        if self.up_name:
            pct = self.up_done * 100 / self.up_total
            lines += [
                "",
                f"⬆️ Upload — {self.up_name}",
                f"{make_bar(pct)}  {pct:.0f}%",
                f"💾 {human_size(self.up_done)} / {human_size(self.up_total)}   ⚡ {fmt_speed(self.up_speed.value())}",
            ]
        if self.failures:
            lines += ["", f"⚠️ Problem: {len(self.failures)} (last: {self.failures[-1][:80]})"]
        lines += ["", self.live_line(), "━━━━━━━━━━━━━━━━━━━━━━", "🛑 Cancel: /cancelzip"]
        return "\n".join(lines)


# ── naming helpers (server1 ke zip_arcname / plan se) ─

_QUALITY_RE = re.compile(r'(?<!\d)(2160|1440|1080|720|480|360|240)\s*[pP](?![a-zA-Z0-9])')


def quality_key(name: str) -> str:
    """Link ke naam se quality label: '720p' / '1080P' / '720' -> '720p'. Na mile to naam saaf karke."""
    name = name or ""
    m = _QUALITY_RE.search(name)
    if m:
        return f"{m.group(1)}p"
    m = re.fullmatch(r'\s*(2160|1440|1080|720|480|360|240)\s*', name)
    if m:
        return f"{m.group(1)}p"
    return re.sub(r'[^A-Za-z0-9]+', '', name)[:12] or "video"


def parse_ep_name(ep_name: str):
    """'Episode 05 - Title [Crunchyroll]' -> (5, 'Title', 'Crunchyroll')"""
    m = re.search(r'Episode\s*0*([0-9]+)', ep_name or "", re.IGNORECASE)
    num = int(m.group(1)) if m else None
    sm = re.search(r'\[([^\]]+)\]\s*$', ep_name or "")
    server = sm.group(1).strip() if sm else "Default"
    return num, server


def zip_arcname(slug: str, ep_num, ep_idx: int, quality: str, server: str, ext: str, used: set) -> str:
    tail = f"E{ep_num:02d}" if ep_num is not None else f"ep{ep_idx}"
    srv = "" if server in ("", "Default") else f"-{server}"
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", f"{slug}-{tail}-{quality}{srv}")
    final, i = f"{name}.{ext}", 2
    while final in used:
        final = f"{name}_{i}.{ext}"
        i += 1
    used.add(final)
    return final


def fmt_ep_range(a: int, b: int) -> str:
    # server1 ka extract_ep_range 'a-b' maangta hai, isliye single episode bhi 'EP 5-5' likhte hain
    return f"EP {a}-{b}"


def slug_from_url(url: str) -> str:
    parts = [p for p in urlparse(url).path.split("/") if p]
    raw = parts[-1] if parts else "anime"
    return re.sub(r'[^a-z0-9]+', '-', raw.lower()).strip('-') or "anime"


# ── blocking worker (alag thread me, bot responsive rehta hai) ──

def download_file(scraper, url: str, down_headers: dict, dest: str, job: dict, prog: ZipProgress,
                  label: str, attempts: int = 3) -> int:
    last = None
    for attempt in range(1, attempts + 1):
        try:
            with scraper.get(url, headers=down_headers, stream=True, timeout=60) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                total = int(r.headers.get('content-length', 0) or 0)
                prog.start_file(label, total)
                done = 0
                with open(dest, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=262144):
                        if job["cancel"]:
                            raise ZipCancelled()
                        if chunk:
                            f.write(chunk)
                            done += len(chunk)
                            prog.add_dl(len(chunk))
                if total and done < total:
                    raise RuntimeError(f"adhura download ({done}/{total} bytes)")
                if done == 0:
                    raise RuntimeError("khaali file aayi")
                return done
        except ZipCancelled:
            raise
        except Exception as e:
            last = e
            prog.set_note(f"Download retry {attempt}/{attempts}: {type(e).__name__}")
            if os.path.exists(dest):
                try:
                    os.remove(dest)
                except OSError:
                    pass
            if attempt < attempts:
                time.sleep(2 * attempt)
    raise RuntimeError(f"{type(last).__name__}: {last}")


def zip_worker(job: dict, prog: ZipProgress, loop, upload_q: "asyncio.Queue", slug: str,
               episodes: list, only_q, tmp_dir: str):
    limit = ZIP_PART_MB * 1024 * 1024
    scraper = make_scraper()
    open_parts: dict = {}          # quality -> part
    counters: dict = {}            # quality -> part number
    dl_path = os.path.join(tmp_dir, "download.tmp")

    def check():
        if job["cancel"]:
            raise ZipCancelled()

    def pending() -> int:
        return job["queued_parts"] - job["done_parts"]

    def hand_over(part: dict):
        job["queued_parts"] += 1
        prog.parts_built += 1
        loop.call_soon_threadsafe(upload_q.put_nowait, part)
        while pending() > ZIP_MAX_PENDING:          # disk bachane ke liye: bahut saare ready part jama na hon
            check()
            prog.set_note("Upload ka intezaar (disk bachane ke liye)")
            time.sleep(1)

    def new_part(q: str) -> dict:
        n = counters[q] = counters.get(q, 0) + 1
        path = os.path.join(tmp_dir, f"building-{q}-{n}.zip")
        zf = zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED, allowZip64=True)
        return {"q": q, "n": n, "path": path, "zf": zf, "size": 0, "eps": [], "used": set(), "count": 0}

    def close_part(q: str):
        p = open_parts.pop(q)
        p["zf"].close()
        if p["count"] == 0:
            os.remove(p["path"])
            return
        nums = p["eps"]
        rng = (min(nums), max(nums)) if nums and all(n is not None for n in nums) else None
        r_tag = f"-EP{rng[0]}-{rng[1]}" if rng else f"-part{p['n']}"
        name = f"{slug}{r_tag}-ZIP-{q}.zip"
        final, i = os.path.join(tmp_dir, name), 2
        while os.path.exists(final):
            name = f"{slug}{r_tag}-ZIP-{q}_{i}.zip"
            final = os.path.join(tmp_dir, name)
            i += 1
        os.replace(p["path"], final)
        caption = f"{q} {fmt_ep_range(*rng)} • {slug}" if rng else f"{q} • {slug} • part {p['n']}"
        hand_over({"path": final, "name": name, "quality": q, "range": rng, "count": p["count"],
                   "size": os.path.getsize(final), "caption": caption})

    def add_file(q: str, ep_num, ep_idx: int, server: str, ext: str, src_path: str, size: int, label: str):
        if size > limit:
            prog.failures.append(f"{label}: {human_size(size)} ZIP part limit ({ZIP_PART_MB} MB) se bada, skip")
            return
        p = open_parts.get(q)
        if p and p["count"] and p["size"] + size > limit:
            close_part(q)
            p = None
        if p is None:
            p = open_parts[q] = new_part(q)
        arc = zip_arcname(slug, ep_num, ep_idx, q, server, ext, p["used"])
        prog.stage, prog.file_label = "zipping", label
        zi = zipfile.ZipInfo(arc, date_time=time.localtime()[:6])
        zi.compress_type = zipfile.ZIP_STORED
        zi.external_attr = 0o644 << 16
        with open(src_path, 'rb') as src, p["zf"].open(zi, "w", force_zip64=True) as dst:
            while True:
                check()
                buf = src.read(4 * 1024 * 1024)
                if not buf:
                    break
                dst.write(buf)
        p["size"] += size
        p["count"] += 1
        p["eps"].append(ep_num)

    def ensure_disk():
        need = 2560 * 1024 * 1024
        while shutil.disk_usage(tmp_dir).free < need:
            if pending() > 0:
                check()
                prog.set_note("Disk kam hai, upload khatam hone ka intezaar")
                time.sleep(2)
                continue
            raise RuntimeError("Disk space kam hai (ZIP part limit chhoti karo: ZIP_PART_MB)")

    try:
        for i, ep in enumerate(episodes, 1):
            check()
            prog.begin_episode(i, ep["name"])
            links, argon_url, last_err = None, None, ""
            for attempt in (1, 2):
                try:
                    links, argon_url = resolve_download_links(scraper, ep["link"], ep["name"])
                    break
                except BypassError as e:
                    last_err = str(e)
                    break
                except Exception as e:
                    last_err = f"{type(e).__name__}: {e}"
                    check()
                    time.sleep(3)
            if not links:
                prog.failures.append(f"{ep['name']}: {last_err or 'links nahi mile'}")
                prog.end_episode()
                continue

            ep_num, server = parse_ep_name(ep["name"])
            down_headers = {"Referer": argon_url, "User-Agent": USER_AGENT}
            for item in links:
                check()
                q = quality_key(item["name"])
                if only_q and q != only_q:
                    continue
                label = f"{ep['name']} • {q}"
                ensure_disk()
                try:
                    size = download_file(scraper, item["url"], down_headers, dl_path, job, prog, label)
                except ZipCancelled:
                    raise
                except Exception as e:
                    prog.failures.append(f"{label}: download fail ({e})")
                    continue
                try:
                    ext = os.path.splitext(urlparse(item["url"]).path)[1].lstrip(".").lower()
                    if ext not in VIDEO_EXTS:
                        ext = "mp4"
                    add_file(q, ep_num, i, server, ext, dl_path, size, label)
                finally:
                    if os.path.exists(dl_path):
                        os.remove(dl_path)
            prog.end_episode()

        for q in list(open_parts):
            check()
            close_part(q)
    except BaseException:
        for p in open_parts.values():
            try:
                p["zf"].close()
            except Exception:
                pass
        open_parts.clear()
        raise
    finally:
        loop.call_soon_threadsafe(upload_q.put_nowait, None)      # uploader ko khatam ka signal


# ── async side: upload + status + job runner ──────────

async def upload_zip_part(client, chat_id: int, job: dict, prog: ZipProgress, part: dict):
    prog.start_upload(part["name"], part["size"])

    def progress(current, total):     # sync + halka: upload ko kabhi block nahi karta
        if job["cancel"]:
            raise StopTransmission
        prog.set_upload(current, total)

    last = None
    for attempt in range(3):
        try:
            sent = await client.send_document(
                chat_id=chat_id,
                document=part["path"],
                file_name=part["name"],
                caption=part["caption"],
                progress=progress,
            )
            if sent is None:
                raise ZipCancelled()
            return
        except (ZipCancelled, asyncio.CancelledError):
            raise
        except FloodWait as e:
            last = e
            await asyncio.sleep(e.value + 1)
        except Exception as e:
            last = e
            if job["cancel"]:
                raise ZipCancelled()
            print(f"ZIP upload attempt {attempt + 1}/3 fail: {e!r}")
            prog.start_upload(part["name"], part["size"])
            prog.set_note(f"Upload retry {attempt + 1}/3: {type(e).__name__}")
            await asyncio.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Upload fail ho gaya: {last!r}")


async def zip_uploader(client, chat_id: int, job: dict, prog: ZipProgress, upload_q: "asyncio.Queue", results: list):
    while True:
        part = await upload_q.get()
        if part is None:
            return
        try:
            if not job["cancel"]:
                await upload_zip_part(client, chat_id, job, prog, part)
                prog.finish_upload()
                results.append(part)
        except ZipCancelled:
            pass
        except Exception as e:
            print(f"ZIP uploader error: {e!r}")
            job["error"] = f"Upload me error: {e}"
            job["cancel"] = True          # worker bhi turant ruk jaye
        finally:
            job["done_parts"] += 1
            try:
                os.remove(part["path"])
            except OSError:
                pass


async def zip_status_loop(msg, prog: ZipProgress, stop_evt: "asyncio.Event", interval: float = 4.0):
    """Job se independent heartbeat: har ~4s status message refresh (flood par ruk kar)."""
    last = ""
    while not stop_evt.is_set():
        try:
            await asyncio.wait_for(stop_evt.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        if stop_evt.is_set():
            break
        text = prog.render()
        if text == last:
            continue
        try:
            await msg.edit_text(text, parse_mode=PM_OFF)
            last = text
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
        except Exception as e:
            if "MESSAGE_NOT_MODIFIED" in str(e):
                last = text
            else:
                print(f"ZIP status edit error: {e!r}")


def _get_zip_sem() -> "asyncio.Semaphore":
    global _zip_sem
    if _zip_sem is None:
        _zip_sem = asyncio.Semaphore(1)       # ek waqt me ek hi heavy ZIP job (disk/bandwidth bachane ke liye)
    return _zip_sem


async def run_zip_job(client, chat_id: int, url: str, episodes: list, slug: str, only_q):
    job = {"cancel": False, "error": None, "queued_parts": 0, "done_parts": 0}
    zip_jobs[chat_id] = job
    sem = _get_zip_sem()
    status = await client.send_message(
        chat_id,
        "⏳ Pehle wala ZIP job chal raha hai, tumhara line me hai..." if sem.locked() else "📦 ZIP job shuru ho raha hai...",
    )
    prog = ZipProgress(slug, len(episodes), only_q)
    tmp_dir, hb_task, up_task = None, None, None
    stop_evt = asyncio.Event()
    results: list = []
    outcome, err_text = "ok", ""
    try:
        async with sem:
            if job["cancel"]:
                raise ZipCancelled()
            os.makedirs(WORK_DIR, exist_ok=True)
            tmp_dir = tempfile.mkdtemp(prefix="zipjob_", dir=WORK_DIR)
            loop = asyncio.get_running_loop()
            upload_q: asyncio.Queue = asyncio.Queue()
            hb_task = asyncio.create_task(zip_status_loop(status, prog, stop_evt))
            up_task = asyncio.create_task(zip_uploader(client, chat_id, job, prog, upload_q, results))
            worker_exc = None
            try:
                await asyncio.to_thread(zip_worker, job, prog, loop, upload_q, slug, episodes, only_q, tmp_dir)
            except BaseException as e:      # worker ka error: uploader ko khatam hone do, phir report
                worker_exc = e
            await up_task
            up_task = None
            if isinstance(worker_exc, asyncio.CancelledError):
                raise worker_exc
            if worker_exc is not None and not isinstance(worker_exc, ZipCancelled):
                outcome, err_text = "error", f"{type(worker_exc).__name__}: {worker_exc}"
            elif job["error"]:
                outcome, err_text = "error", job["error"]
            elif worker_exc is not None:
                outcome = "cancelled"
    except ZipCancelled:
        outcome = "cancelled"
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except Exception as e:
        print(f"run_zip_job error: {e!r}")
        outcome, err_text = "error", f"{type(e).__name__}: {e}"
    finally:
        job["cancel"] = job["cancel"] or outcome != "ok"
        stop_evt.set()
        if hb_task:
            try:
                await asyncio.wait_for(hb_task, timeout=25)
            except BaseException:
                hb_task.cancel()
        if up_task is not None and not up_task.done():
            up_task.cancel()
            await asyncio.gather(up_task, return_exceptions=True)
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        zip_jobs.pop(chat_id, None)

    # ── final report ──
    results.sort(key=lambda r: (-(int(re.match(r"\d+", r["quality"]).group(0)) if re.match(r"\d+", r["quality"]) else 0),
                                r["range"][0] if r["range"] else 0))
    total_bytes = sum(r["size"] for r in results)
    if outcome == "cancelled":
        text = "🛑 ZIP cancel ho gaya." + (f"\n({len(results)} part upload ho chuke the, wo chat me hain.)" if results else "")
    elif outcome == "error":
        text = f"❌ ZIP banate waqt error aaya:\n{err_text}"
    elif not results:
        text = "❌ Koi ZIP nahi bana (matching file nahi mili)."
        if prog.failures:
            text += "\n\n⚠️ Problems:\n" + "\n".join(f"• {f}" for f in prog.failures[:10])
    else:
        lines = [
            f"🎉 ZIP Complete! — {slug}",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"🧩 Parts: {len(results)}   •   🎬 Episodes: {len(episodes)}",
            f"💾 Total size: {human_size(total_bytes)}",
            f"⏱ Total time: {fmt_dur(time.time() - prog.started)}",
            "",
        ]
        for r in results:
            rtxt = fmt_ep_range(*r["range"]) if r["range"] else "range nahi"
            lines.append(f"• {r['quality']} • {rtxt} • {r['count']} video • {human_size(r['size'])}")
        if prog.failures:
            lines += ["", f"⚠️ {len(prog.failures)} problem:"] + [f"• {f}" for f in prog.failures[:10]]
        lines += [
            "",
            "📤 Ab ye ZIP file(s) server1 bot ko forward karo (pehle wahan /setup <slug> <season>).",
            "Caption me quality + EP range already hai, server1 usi se Firebase me save karega.",
        ]
        text = "\n".join(lines)
    try:
        await status.edit_text(text[:4000], parse_mode=PM_OFF)
    except Exception:
        try:
            await client.send_message(chat_id, text[:4000], parse_mode=PM_OFF)
        except Exception as e:
            print(f"ZIP final message error: {e!r}")


ZIP_USAGE = (
    "⚠️ Sahi format: `/zip <url> [start-end] [480p|720p|1080p]`\n"
    "• `/zip <url>` — page ke saare episodes, har quality ka alag ZIP\n"
    "• `/zip <url> 1-12` — sirf 1 se 12 (list ke number, `/batch` jaisa)\n"
    "• `/zip <url> 1-12 720p` — sirf 720p ka ZIP"
)


@app.on_message(filters.command("zip"))
async def zip_command(client, message: Message):
    chat_id = message.chat.id
    args = message.text.split()
    if len(args) < 2 or not args[1].startswith("http"):
        await message.reply_text(ZIP_USAGE)
        return

    url, start_ep, end_ep, only_q = args[1], None, None, None
    for a in args[2:]:
        if re.fullmatch(r'\d+-\d+', a):
            start_ep, end_ep = sorted(map(int, a.split("-")))
        elif re.fullmatch(r'(2160|1440|1080|720|480|360|240)[pP]?', a):
            only_q = re.match(r'\d+', a).group(0) + "p"
        else:
            await message.reply_text(ZIP_USAGE)
            return

    if chat_id in zip_jobs:
        await message.reply_text("⚠️ Is chat ka ZIP job pehle se chal raha / line me hai. Rokne ke liye /cancelzip")
        return

    msg = await message.reply_text("🔍 Episodes fetch kiye ja rahe hain...")
    episodes = await fetch_episodes_from_url(url)
    if not episodes:
        await msg.edit_text("❌ URL se episodes nahi mile.")
        return
    if start_ep is not None:
        episodes = [ep for idx, ep in enumerate(episodes, start=1) if start_ep <= idx <= end_ep]
        if not episodes:
            await msg.edit_text("❌ Di gayi range mein koi episodes nahi mile.")
            return

    slug = slug_from_url(url)
    await msg.edit_text(
        f"📦 ZIP job: {len(episodes)} episode(s) • {only_q or 'saari qualities'}\n"
        f"Part limit: {ZIP_PART_MB} MB • Cancel: /cancelzip"
    )
    task = asyncio.create_task(run_zip_job(client, chat_id, url, episodes, slug, only_q))
    zip_tasks.add(task)
    task.add_done_callback(zip_tasks.discard)


@app.on_message(filters.command("cancelzip"))
async def cancel_zip_command(client, message: Message):
    job = zip_jobs.get(message.chat.id)
    if not job:
        await message.reply_text("ℹ️ Koi ZIP job active nahi hai.")
        return
    job["cancel"] = True
    await message.reply_text("🛑 ZIP cancel ho raha hai... (kuch second me ruk jayega)")


async def background_monitor_task(client, chat_id, url, initial_eps):
    known_links = {ep['link'] for ep in initial_eps}
        
    while True:
        try:
            await asyncio.sleep(180)  # Har 3 minute baad check
            current_eps = await fetch_episodes_from_url(url)
            
            if not current_eps:
                continue
            
            new_episodes = [ep for ep in current_eps if ep['link'] not in known_links]
            
            for ep in new_episodes:
                known_links.add(ep['link'])
                await client.send_message(chat_id, f"🚨 **Naya Episode Mil Gaya!**\nAuto-downloading: `{ep['name']}`")
                await process_and_send_telegram(client, chat_id, ep['link'], ep['name'], is_batch=True)
                
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Monitor Loop Error: {e}")
            await asyncio.sleep(60)

@app.on_message(filters.text & filters.regex(r"http"))
async def handle_main_page(client, message: Message):
    chat_id = message.chat.id
    url = message.text
    
    status_msg = await message.reply_text("🔍 Main page scan kar raha hu...")

    try:
        episodes_list = await fetch_episodes_from_url(url)
        
        if not episodes_list:
            await status_msg.edit_text("❌ Episodes nahi mil paye.")
            return
            
        user_episodes_data[chat_id] = episodes_list
        
        success_text = (
            f"✅ **Episodes Loaded Successfully!** (Total: {len(episodes_list)})\n\n"
            f"• Buttons se select karein,\n"
            f"• Ya `/single <number>` use karein,\n"
            f"• Ya `/allep <url>` ya `/batch` use karein."
        )
        
        await status_msg.delete()
        await send_episodes_page(client, chat_id, page=0, custom_text=success_text)

    except Exception as e:
        print(f"Error: {e}")
        await status_msg.edit_text("❌ Kuch technical error aa gaya.")

async def send_episodes_page(client, chat_id, page=0, message_id=None, custom_text=None):
    episodes = user_episodes_data.get(chat_id, [])
    total_episodes = len(episodes)
    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    current_eps = episodes[start_idx:end_idx]
    
    buttons = []
    for i, ep in enumerate(current_eps):
        actual_index = start_idx + i
        short_label = ep['name'].replace("Episode ", "Ep ")
        buttons.append(InlineKeyboardButton(short_label[:30], callback_data=f"get_ep_{actual_index}"))
    
    keyboard = [buttons[i:i + 1] for i in range(0, len(buttons))]
    
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅ Prev", callback_data=f"page_{page - 1}"))
    if end_idx < total_episodes:
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{page + 1}"))
    if nav_buttons:
        keyboard.append(nav_buttons)

    markup = InlineKeyboardMarkup(keyboard)
    text_msg = custom_text or f"📺 **Episodes List** (Total: {total_episodes})\nPage: {page + 1}"
    
    if message_id:
        await client.edit_message_text(chat_id, message_id, text_msg, reply_markup=markup)
    else:
        await client.send_message(chat_id, text_msg, reply_markup=markup)

@app.on_callback_query()
async def handle_query(client, callback_query: CallbackQuery):
    chat_id = callback_query.message.chat.id
    data = callback_query.data
    
    if data.startswith("page_"):
        page_num = int(data.split("_")[1])
        await send_episodes_page(client, chat_id, page=page_num, message_id=callback_query.message.id)
        await callback_query.answer()
        
    elif data.startswith("get_ep_"):
        ep_index = int(data.split("_")[2])
        episodes = user_episodes_data.get(chat_id, [])
        
        if ep_index < len(episodes):
            ep_info = episodes[ep_index]
            ep_name = ep_info['name']
            watch_url = ep_info['link']
            
            await callback_query.answer(f"{ep_name} process hona shuru ho gaya hai...")
            asyncio.create_task(process_and_send_telegram(client, chat_id, watch_url, ep_name))
        else:
            await callback_query.answer("Session expire ho gaya.", show_alert=True)
            
    elif data.startswith("rem_idx_"):
        idx = int(data.split("_")[2])
        urls_list = list(monitored_tasks.keys())
        
        if 0 <= idx < len(urls_list):
            target_url = urls_list[idx]
            monitored_tasks[target_url].cancel()
            del monitored_tasks[target_url]
            
            await callback_query.answer("✅ Monitored URL successfully remove kar diya gaya!", show_alert=True)
            
            if monitored_tasks:
                keyboard = []
                text = "📋 **Currently Monitored URLs:**\n\n"
                for new_idx, url in enumerate(list(monitored_tasks.keys()), start=1):
                    text += f"{new_idx}. `{url}`\n"
                    keyboard.append([InlineKeyboardButton(f"❌ Remove #{new_idx}", callback_data=f"rem_idx_{new_idx-1}")])
                await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                await callback_query.message.edit_text("📭 Abhi koi bhi URL monitor nahi ho raha hai.")
        else:
            await callback_query.answer("⚠️ Yeh URL pehle hi remove ho chuka hai.", show_alert=True)

async def process_and_send_telegram(client, chat_id, watch_url, ep_name, is_batch=False):
    msg = await client.send_message(chat_id, f"⏳ **{ep_name}**\n⚡ Step: Zipper link bypass ho raha hai...")
    
    try:
        scraper = make_scraper()
        headers = {"User-Agent": USER_AGENT}

        try:
            parsed_links, argon_url = resolve_download_links(scraper, watch_url, ep_name)
        except BypassError as be:
            await msg.edit_text(str(be))
            return

        down_headers = {"Referer": argon_url, "User-Agent": headers["User-Agent"]}

        total_qualities = len(parsed_links)
        for idx, item in enumerate(parsed_links, start=1):
            link_name = item['name']
            link_url = item['url']
            
            safe_name = "".join(c for c in link_name if c.isalnum() or c in (' ', '.', '_')).rstrip()
            if not safe_name.endswith(('.mp4', '.mkv', '.zip', '.pdf')):
                safe_name += f"_quality_{idx}.mp4"
            
            temp_file_path = os.path.join(WORK_DIR, safe_name)

            with scraper.get(link_url, headers=down_headers, stream=True, timeout=60) as r:
                if r.status_code != 200:
                    continue
                total_size = int(r.headers.get('content-length', 0))
                downloaded = 0
                last_update_time = 0
                
                with open(temp_file_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total_size > 0:
                                percent = (downloaded / total_size) * 100
                                current_time = time.time()
                                if current_time - last_update_time > 2:
                                    filled = int(percent / 10)
                                    bar = "█" * filled + "░" * (10 - filled)
                                    prog_text = (
                                        f"⏳ **{ep_name}**\n"
                                        f"⚡ Downloading `{link_name}` ({idx}/{total_qualities})\n\n"
                                        f"[{bar}] **{percent:.1f}%**\n"
                                        f"📦 {downloaded / (1024*1024):.2f} MB / {total_size / (1024*1024):.2f} MB"
                                    )
                                    try:
                                        await msg.edit_text(prog_text)
                                    except Exception:
                                        pass
                                    last_update_time = current_time

            await msg.edit_text(f"⏳ **{ep_name}**\n⚡ Processing metadata & thumbnail...")
            
            duration = 0
            width = 1280
            height = 720
            thumb_path = os.path.join(WORK_DIR, f"thumb_{idx}.jpg")
            has_thumb = False

            try:
                cam = cv2.VideoCapture(temp_file_path)
                if cam.isOpened():
                    fps = cam.get(cv2.CAP_PROP_FPS)
                    frame_count = cam.get(cv2.CAP_PROP_FRAME_COUNT)
                    if fps > 0:
                        duration = int(frame_count / fps)
                    width = int(cam.get(cv2.CAP_PROP_FRAME_WIDTH))
                    height = int(cam.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    
                    cam.set(cv2.CAP_PROP_POS_FRAMES, 1)
                    ret, frame = cam.read()
                    if ret:
                        cv2.imwrite(thumb_path, frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                        has_thumb = True
                    cam.release()
            except Exception as cv_err:
                print(f"OpenCV Error: {cv_err}")

            ep_num_match = re.search(r'Episode\s*0*([0-9]+)', ep_name, re.IGNORECASE)
            ep_code = f"E{ep_num_match.group(1)}" if ep_num_match else "E31"
            clean_title = re.sub(r'Episode\s*0*[0-9]+[\s\-:]*', '', ep_name, flags=re.IGNORECASE).split('[')[0].strip()

            custom_caption = (
                f"📟 Episode - {ep_code}\n"
                f"🏷️ Title - {clean_title}\n"
                f"🎧 Language - Hindi\n"
                f"📀 Quality - ({link_name})\n"
                f"══════════════════"
            )

            await msg.edit_text(f"⏳ **{ep_name}**\n⚡ Uploading to Telegram...")

            try:
                await client.send_video(
                    chat_id,
                    video=temp_file_path,
                    caption=custom_caption,
                    duration=duration,
                    width=width,
                    height=height,
                    thumb=thumb_path if has_thumb and os.path.exists(thumb_path) else None,
                    supports_streaming=True
                )
                bot_stats["downloads_completed"] += 1
            except Exception as up_err:
                print(f"Video send error: {up_err}")
                try:
                    await client.send_document(chat_id, document=temp_file_path, caption=custom_caption)
                    bot_stats["downloads_completed"] += 1
                except Exception as doc_err:
                    print(f"Document send error: {doc_err}")

            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
            if has_thumb and os.path.exists(thumb_path):
                os.remove(thumb_path)

        if not is_batch:
            await msg.delete()

    except Exception as e:
        print(f"Process Error: {e}")
        try:
            await msg.edit_text(f"❌ Process ke dauran error aa gaya.")
        except Exception:
            pass

# --- DEBUG: har aane wale message ko log me dikhata hai (baad me hata sakte ho) ---
@app.on_message(group=-1)
async def _debug_log(client, message):
    uid = message.from_user.id if message.from_user else "?"
    print(f"📩 Incoming from {uid}: {message.text}", flush=True)

# --- KAGGLE COMPATIBLE RUNNER ---
async def main():
    await app.start()
    me = await app.get_me()
    print(f"✅ Logged in as @{me.username} (id {me.id})", flush=True)
    commands = [
        BotCommand("start", "Bot start karein"),
        BotCommand("single", "Single episode download"),
        BotCommand("batch", "Batch download"),
        BotCommand("allep", "All episodes download"),
        BotCommand("monitor", "Background auto-check"),
        BotCommand("monitored", "Monitored list with remove buttons"),
        BotCommand("remove", "Remove monitoring manually"),
        BotCommand("stopmonitor", "Stop monitoring"),
        BotCommand("zip", "Episodes ka quality-wise ZIP banao"),
        BotCommand("cancelzip", "Chalta ZIP job rokein"),
        BotCommand("stats", "Bot stats")
    ]
    await app.set_bot_commands(commands)
    print("🚀 Kaggle Pyrogram Bot Started Successfully!")
    await idle()
    await app.stop()

if __name__ == "__main__":
    os.makedirs(WORK_DIR, exist_ok=True)
    # IMPORTANT: asyncio.run() naya event loop banata hai, jisse Pyrogram ke handlers chalte hi nahi.
    # Client ka apna loop use karna zaroori hai.
    app.loop.run_until_complete(main())
