import json
import os
import re
import time
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
import cloudscraper
import requests
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup


def load_env_file(path=".env"):
  if not os.path.exists(path):
    return
  with open(path, "r", encoding="utf-8") as f:
    for line in f:
      line = line.strip()
      if not line or line.startswith("#") or "=" not in line:
        continue
      k, v = line.split("=", 1)
      k = k.strip()
      v = v.strip().strip('"').strip("'")
      if k and k not in os.environ:
        os.environ[k] = v


load_env_file(".env")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
  raise RuntimeError("BOT_TOKEN missing! .env file check karo.")

bot = telebot.TeleBot(BOT_TOKEN)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

PREFERRED_HOSTS = ["watchmultiquality", "watchnow", "hubcloud"]
EPISODES_PER_PAGE = 10
MAX_MARKUP_BYTES = 14000
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

SESSIONS = {}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13; Mobile) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Mobile Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

EPISODE_RE = re.compile(r"^\s*Episode\s+\d+.*$", re.IGNORECASE)


class BypassError(Exception):
  pass


def make_scraper():
  return cloudscraper.create_scraper(
      browser={"browser": "chrome", "platform": "windows", "desktop": True}
  )


def get_with_retry(scraper, method, url, retries=3, delay=1.5, **kwargs):
  last_err = None
  for attempt in range(1, retries + 1):
    try:
      if method.lower() == "post":
        return scraper.post(url, **kwargs)
      return scraper.get(url, **kwargs)
    except Exception as e:
      last_err = e
      if attempt < retries:
        time.sleep(delay * attempt)
  raise last_err


def safe_edit(chat_id, msg_id, text, reply_markup=None):
  try:
    bot.edit_message_text(
        text,
        chat_id=chat_id,
        message_id=msg_id,
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )
  except Exception:
    pass


def format_eta(seconds):
  if seconds is None or seconds < 0 or seconds == float("inf"):
    return "--:--"
  secs = int(seconds)
  mins, rem = divmod(secs, 60)
  hrs, mins = divmod(mins, 60)
  if hrs > 0:
    return f"{hrs:02d}:{mins:02d}:{rem:02d}"
  return f"{mins:02d}:{rem:02d}"


def build_progress_bar(pct, length=10):
  pct = max(0.0, min(100.0, pct))
  filled = int(round((pct / 100.0) * length))
  filled = max(0, min(length, filled))
  return "█" * filled + "░" * (length - filled)


def cleanup_old_sessions(max_age_sec=3600):
  now = time.time()
  expired = [
      sid
      for sid, data in SESSIONS.items()
      if now - data.get("created_at", now) > max_age_sec
  ]
  for sid in expired:
    SESSIONS.pop(sid, None)


def short_ep_tag(ep_title):
  m = re.search(
      r"Episode\s+(\d+)(?:\s*\(([^)]+)\))?", ep_title, re.IGNORECASE
  )
  if m:
    num = m.group(1)
    extra = f" ({m.group(2).strip()})" if m.group(2) else ""
    return f"Ep {num}{extra}"
  return ep_title[:20].strip()


def pick_best_link(links_in_block):
  for host in PREFERRED_HOSTS:
    for text_l, url in links_in_block:
      if host in text_l or host in url.lower():
        return url
  return None


def parse_episodes_from_html(html_text, base_url):
  soup = BeautifulSoup(html_text, "html.parser")
  container = (
      soup.select_one("article .entry-content")
      or soup.select_one(".entry-content")
      or soup.select_one("article")
      or soup.body
      or soup
  )

  blocks = container.find_all(["p", "div", "h2", "h3", "h4", "li"])
  episodes = []
  current_ep = None

  for blk in blocks:
    if blk.name == "div" and blk.find(["p", "h2", "h3", "h4"]):
      continue

    text = re.sub(r"\s+", " ", blk.get_text(" ", strip=True)).strip()
    if not text:
      continue

    if EPISODE_RE.match(text) and not blk.find("a"):
      current_ep = {
          "title": text,
          "short": short_ep_tag(text),
          "langs": [],
      }
      episodes.append(current_ep)
      continue

    if current_ep is None:
      continue

    anchors = blk.find_all("a", href=True)
    if not anchors:
      continue

    span = blk.find("span")
    lang_raw = span.get_text(" ", strip=True) if span else ""
    lang_clean = re.sub(r"[-–—:\s]+$", "", lang_raw).strip()

    if not lang_clean:
      before_bracket = text.split("[", 1)[0]
      lang_clean = (
          re.sub(r"[-–—:\s]+$", "", before_bracket).strip() or "Watch"
      )

    link_items = []
    for a in anchors:
      href = a["href"].strip()
      if not href or href.startswith(("#", "javascript:", "mailto:")):
        continue
      full_url = urljoin(base_url, href)
      a_text = a.get_text(" ", strip=True).lower()
      link_items.append((a_text, full_url))

    chosen_url = pick_best_link(link_items)
    if chosen_url:
      existing_langs = [item["lang"] for item in current_ep["langs"]]
      final_lang = (
          lang_clean
          if lang_clean not in existing_langs
          else f"{lang_clean} 2"
      )
      current_ep["langs"].append({"lang": final_lang, "page_url": chosen_url})

  return soup, [ep for ep in episodes if ep["langs"]]


def fetch_zipper_link(watch_page_url):
  resp = requests.get(watch_page_url, headers=HEADERS, timeout=20)
  resp.raise_for_status()
  soup = BeautifulSoup(resp.text, "html.parser")

  a_tag = (
      soup.select_one(".actions a#mainActionBtn[href]")
      or soup.select_one(".actions a[href*='/zipper/']")
      or soup.select_one("a#mainActionBtn[href]")
      or soup.select_one("a[href*='/zipper/']")
  )

  if not a_tag or not a_tag.get("href"):
    return None, None

  zipper_url = urljoin(watch_page_url, a_tag["href"].strip())
  btn_label = re.sub(r"\s+", " ", a_tag.get_text(" ", strip=True)).strip()
  return zipper_url, btn_label


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


def resolve_download_links(scraper, user_input_url, on_step=None):
  headers = {"User-Agent": USER_AGENT}

  def notify(msg):
    if on_step:
      on_step(msg)

  if "/zipper/" in user_input_url:
    zipper_link = user_input_url
  else:
    notify("Watch page scan kar raha hoon...")
    resp = get_with_retry(scraper, "get", user_input_url, timeout=30)
    sub_soup = BeautifulSoup(resp.content, "html.parser")
    zipper_link = None

    for a_tag in sub_soup.find_all("a", class_="btn-dl"):
      href = a_tag.get("href")
      if href and "/zipper/" in href:
        zipper_link = urljoin(user_input_url, href)
        break

    if not zipper_link:
      raise BypassError("Zipper link nahi mila page par.")

  notify("Step 1/4: Zipper bypass kar raha hoon...")
  res1 = get_with_retry(
      scraper, "get", zipper_link, headers=headers, timeout=30
  )
  soup1 = BeautifulSoup(res1.text, "html.parser")
  btn1 = soup1.find("a", id="goBtn")
  if not btn1 or not btn1.get("data-href"):
    raise BypassError("Step 1 link (goBtn data-href) nahi mila.")

  step2_url = urljoin(zipper_link, btn1.get("data-href").strip())

  notify("Step 2/4: Next step / Ziptron resolve kar raha hoon...")
  res2 = get_with_retry(scraper, "get", step2_url, headers=headers, timeout=30)
  soup2 = BeautifulSoup(res2.text, "html.parser")
  btn2 = soup2.find("a", id="goBtn")
  if not btn2 or not btn2.get("data-href"):
    raise BypassError("Step 2 / Ziptron link nahi mila.")

  ziptron_url = urljoin(step2_url, btn2.get("data-href").strip())

  notify("Step 3/4: Token aur Argon link generate kar raha hoon...")
  res3 = get_with_retry(
      scraper, "get", ziptron_url, headers=headers, timeout=30
  )
  soup3 = BeautifulSoup(res3.text, "html.parser")
  form = soup3.find("form", id="landing")
  if not form:
    raise BypassError("Form tag (#landing) nahi mila.")

  action_raw = form.get("action") or ziptron_url
  action_url = urljoin(ziptron_url, action_raw)
  input_tag = form.find("input", {"name": "rtiwatch"})
  token_val = input_tag.get("value") if input_tag else ""

  parsed_ziptron = urlparse(ziptron_url)
  ziptron_origin = f"{parsed_ziptron.scheme}://{parsed_ziptron.netloc}"

  post_headers = {
      "Referer": ziptron_url,
      "Origin": ziptron_origin,
      "User-Agent": headers["User-Agent"],
      "X-Requested-With": "XMLHttpRequest",
  }

  res4 = get_with_retry(
      scraper,
      "post",
      action_url,
      data={"rtiwatch": token_val},
      headers=post_headers,
      timeout=30,
  )
  argon_match = re.search(
      r'https?://[^\s"\'<>]+/downlead/[^\s"\'<>]+', res4.text
  )
  if not argon_match:
    raise BypassError("Argon / downlead link generate nahi hua.")

  argon_url = argon_match.group(0)
  parsed_argon = urlparse(argon_url)
  argon_origin = f"{parsed_argon.scheme}://{parsed_argon.netloc}"

  notify("Step 4/4: Final quality download links nikal raha hoon...")
  res_argon = get_with_retry(
      scraper, "get", argon_url, headers=headers, timeout=30
  )
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
        api_url = urljoin(argon_origin, links_route)
        api_headers = {
            "Referer": argon_url,
            "Origin": argon_origin,
            "User-Agent": headers["User-Agent"],
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
        payload = {"_token": token} if token else {}
        api_res = get_with_retry(
            scraper,
            "post",
            api_url,
            data=payload,
            headers=api_headers,
            timeout=30,
        )
        try:
          links_data = api_res.json()
          parsed_links = extract_urls_from_json(links_data)
        except Exception:
          pass
    except Exception:
      pass

  if not parsed_links:
    raise BypassError("Koi download links nahi mile.")

  return parsed_links, argon_url


def download_and_send_file(
    chat_id, status_msg_id, scraper, item, argon_url, ep_label, lang_label
):
  link_name = item["name"]
  link_url = item["url"]

  safe_name = "".join(
      c for c in link_name if c.isalnum() or c in (" ", ".", "_", "-")
  ).strip()
  safe_name = re.sub(r"\s+", "_", safe_name) if safe_name else "episode_file"
  if not safe_name.lower().endswith(
      (".mp4", ".mkv", ".zip", ".pdf", ".webm", ".m4v")
  ):
    safe_name += ".mp4"

  file_path = os.path.join(DOWNLOAD_DIR, f"{int(time.time())}_{safe_name}")
  down_headers = {"Referer": argon_url, "User-Agent": USER_AGENT}

  try:
    safe_edit(
        chat_id,
        status_msg_id,
        f"*{ep_label} • {lang_label}*\n"
        f"📥 *Status:* Connection establish kar raha hoon...\n"
        f"📄 *File:* `{safe_name}`",
    )

    with scraper.get(
        link_url, headers=down_headers, stream=True, timeout=90
    ) as r:
      if r.status_code != 200:
        raise BypassError(f"Download fail (HTTP {r.status_code})")

      total_size = int(r.headers.get("content-length", 0))
      downloaded = 0
      start_t = time.time()
      last_edit = start_t

      with open(file_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=16384):
          if chunk:
            f.write(chunk)
            downloaded += len(chunk)
            now = time.time()
            if (now - last_edit) >= 3:
              elapsed = max(0.1, now - start_t)
              speed_bps = downloaded / elapsed
              speed_mbs = speed_bps / (1024 * 1024)
              mb_done = downloaded / (1024 * 1024)

              if total_size > 0:
                pct = (downloaded / total_size) * 100.0
                mb_tot = total_size / (1024 * 1024)
                rem_bytes = max(0, total_size - downloaded)
                eta_str = format_eta(
                    rem_bytes / speed_bps if speed_bps > 0 else None
                )
                bar = build_progress_bar(pct, length=10)
                status_text = (
                    f"*{ep_label} • {lang_label}*\n"
                    f"📥 *Status:* Downloading...\n"
                    f"📄 *File:* `{safe_name}`\n"
                    f"`[{bar}]` *{pct:.1f}%*\n"
                    f"📦 *Size:* `{mb_done:.1f} MB / {mb_tot:.1f} MB`\n"
                    f"🚀 *Speed:* `{speed_mbs:.2f} MB/s` | ⏳ *ETA:* `{eta_str}`"
                )
              else:
                bar = build_progress_bar(0, length=10)
                status_text = (
                    f"*{ep_label} • {lang_label}*\n"
                    f"📥 *Status:* Downloading...\n"
                    f"📄 *File:* `{safe_name}`\n"
                    f"`[{bar}]` *--%*\n"
                    f"📦 *Downloaded:* `{mb_done:.1f} MB`\n"
                    f"🚀 *Speed:* `{speed_mbs:.2f} MB/s` | ⏳ *ETA:* `--:--`"
                )

              safe_edit(chat_id, status_msg_id, status_text)
              last_edit = now

    final_mb = os.path.getsize(file_path) / (1024 * 1024)
    bar_done = build_progress_bar(100.0, length=10)
    safe_edit(
        chat_id,
        status_msg_id,
        f"*{ep_label} • {lang_label}*\n"
        f"📤 *Status:* Telegram par upload kar raha hoon...\n"
        f"📄 *File:* `{safe_name}`\n"
        f"`[{bar_done}]` *100.0%*\n"
        f"📦 *Size:* `{final_mb:.1f} MB`",
    )

    with open(file_path, "rb") as doc:
      bot.send_document(
          chat_id,
          doc,
          caption=(
              f"*{ep_label} • {lang_label}*\n"
              f"📄 `{safe_name}`\n"
              f"📦 Size: `{final_mb:.1f} MB`"
          ),
          parse_mode="Markdown",
      )

    try:
      bot.delete_message(chat_id, status_msg_id)
    except Exception:
      pass

  finally:
    if os.path.exists(file_path):
      try:
        os.remove(file_path)
      except Exception:
        pass


def make_markup_for_chunk(
    chunk, start_idx, session_id, page_idx, total_pages
):
  markup = InlineKeyboardMarkup(row_width=2)
  for offset, ep in enumerate(chunk):
    ep_idx = start_idx + offset
    row = []
    for lang_idx, item in enumerate(ep["langs"]):
      btn_text = f"{ep['short']} • {item['lang']}"
      cb_data = f"fz|{session_id}|{ep_idx}|{lang_idx}"
      row.append(InlineKeyboardButton(text=btn_text[:40], callback_data=cb_data))
      if len(row) == 2:
        markup.row(*row)
        row = []
    if row:
      markup.row(*row)

  nav_buttons = []
  if page_idx > 0:
    nav_buttons.append(
        InlineKeyboardButton(
            text="⬅️ Prev", callback_data=f"pg|{session_id}|{page_idx - 1}"
        )
    )
  nav_buttons.append(
      InlineKeyboardButton(
          text=f"📄 {page_idx + 1}/{total_pages}",
          callback_data=f"noop|{session_id}|{page_idx}",
      )
  )
  if page_idx < total_pages - 1:
    nav_buttons.append(
        InlineKeyboardButton(
            text="Next ➡️", callback_data=f"pg|{session_id}|{page_idx + 1}"
        )
    )
  markup.row(*nav_buttons)
  return markup


def render_page(session_id, page_idx):
  data = SESSIONS.get(session_id)
  if not data:
    return "Session expire ho gaya hai. Kripya URL dobara bhejein.", None

  title = data["title"]
  episodes = data["episodes"]
  total_eps = len(episodes)

  step = EPISODES_PER_PAGE
  total_pages = max(1, (total_eps + step - 1) // step)
  page_idx = max(0, min(page_idx, total_pages - 1))

  while step >= 1:
    total_pages = max(1, (total_eps + step - 1) // step)
    page_idx = max(0, min(page_idx, total_pages - 1))
    start = page_idx * step
    end = min(start + step, total_eps)
    chunk = episodes[start:end]

    markup = make_markup_for_chunk(
        chunk, start, session_id, page_idx, total_pages
    )
    raw_size = len(markup.to_json().encode("utf-8"))
    if raw_size <= MAX_MARKUP_BYTES or step == 1:
      break
    step -= 1

  start = page_idx * step
  end = min(start + step, total_eps)
  text = (
      f"*{title}*\n"
      f"Page {page_idx + 1}/{total_pages} • Episodes {start + 1}–{end} (Total: {total_eps})\n"
      "_Kisi episode button par click karo — bot bypass karke file bhejega._"
  )
  return text, markup


@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
  bot.reply_to(
      message,
      "Kisi bhi episode page ka **URL** bhejo.\n"
      "Episode button par click karne par bot live status, progress bar, speed aur ETA ke saath file bhejega.",
      parse_mode="Markdown",
  )


@bot.message_handler(
    func=lambda m: bool(m.text) and m.text.strip().startswith(("http://", "https://"))
)
def handle_url(message):
  url = message.text.strip()
  status = bot.reply_to(message, "🔍 Page scan kar raha hoon...")

  try:
    cleanup_old_sessions()
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()

    soup, episodes = parse_episodes_from_html(resp.text, url)
    page_title = (
        soup.title.string.strip() if soup.title and soup.title.string else "Anime Episodes"
    )

    if not episodes:
      safe_edit(
          message.chat.id,
          status.message_id,
          "Is page par expected `Episode ...` + language (`Hindi`/`Tamil`/`Telugu`) format nahi mila.",
      )
      return

    session_id = f"{message.chat.id}_{status.message_id}"
    SESSIONS[session_id] = {
        "title": page_title,
        "episodes": episodes,
        "resolved_qualities": {},
        "created_at": time.time(),
    }

    text, markup = render_page(session_id, 0)
    safe_edit(message.chat.id, status.message_id, text, reply_markup=markup)

  except Exception as e:
    safe_edit(message.chat.id, status.message_id, f"❌ Error: `{e}`")


@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
  data = call.data or ""
  parts = data.split("|")

  if len(parts) == 3:
    action, session_id, pg_str = parts
    if action == "noop":
      bot.answer_callback_query(call.id, f"Page {int(pg_str) + 1}")
      return

    if action == "pg":
      try:
        target_page = int(pg_str)
      except ValueError:
        bot.answer_callback_query(call.id)
        return

      text, markup = render_page(session_id, target_page)
      if markup is None:
        bot.answer_callback_query(
            call.id, "Session expire ho gaya, URL dubara bhejo."
        )
        return

      safe_edit(
          call.message.chat.id,
          call.message.message_id,
          text,
          reply_markup=markup,
      )
      bot.answer_callback_query(call.id)
      return

  if len(parts) == 4 and parts[0] == "fz":
    _, session_id, ep_i_str, lang_i_str = parts
    sess = SESSIONS.get(session_id)
    if not sess:
      bot.answer_callback_query(
          call.id, "Session expire ho gaya, URL dubara bhejo."
      )
      return

    try:
      ep_idx = int(ep_i_str)
      lang_idx = int(lang_i_str)
      ep_obj = sess["episodes"][ep_idx]
      lang_obj = ep_obj["langs"][lang_idx]
    except (ValueError, IndexError):
      bot.answer_callback_query(call.id, "Invalid selection.")
      return

    bot.answer_callback_query(call.id, "Processing start...")
    prog_msg = bot.send_message(
        call.message.chat.id,
        f"*{ep_obj['short']} • {lang_obj['lang']}*\n"
        "⚙️ *Status:* Zipper link nikal raha hoon...",
        parse_mode="Markdown",
    )

    try:
      zipper_url, _ = fetch_zipper_link(lang_obj["page_url"])
      if not zipper_url:
        raise BypassError("Watch page par zipper link nahi mila.")

      def step_cb(step_msg):
        safe_edit(
            call.message.chat.id,
            prog_msg.message_id,
            f"*{ep_obj['short']} • {lang_obj['lang']}*\n⚙️ *Status:* {step_msg}",
        )

      scraper = make_scraper()
      parsed_links, argon_url = resolve_download_links(
          scraper, zipper_url, on_step=step_cb
      )

      key = f"{ep_idx}_{lang_idx}"
      sess["resolved_qualities"][key] = {
          "links": parsed_links,
          "argon_url": argon_url,
          "ep_label": ep_obj["short"],
          "lang_label": lang_obj["lang"],
      }

      if len(parsed_links) == 1:
        download_and_send_file(
            call.message.chat.id,
            prog_msg.message_id,
            scraper,
            parsed_links[0],
            argon_url,
            ep_obj["short"],
            lang_obj["lang"],
        )
      else:
        q_markup = InlineKeyboardMarkup(row_width=2)
        for q_idx, q_item in enumerate(parsed_links):
          q_btn_text = str(q_item.get("name") or f"Quality {q_idx + 1}")[:38]
          q_markup.add(
              InlineKeyboardButton(
                  text=q_btn_text,
                  callback_data=f"dlq|{session_id}|{key}|{q_idx}",
              )
          )

        safe_edit(
            call.message.chat.id,
            prog_msg.message_id,
            f"*{ep_obj['short']} • {lang_obj['lang']}*\n"
            f"✅ *Qualities Found:* *{len(parsed_links)}*\n"
            "Kaunsi quality download karke bheju?",
            reply_markup=q_markup,
        )

    except Exception as e:
      safe_edit(
          call.message.chat.id,
          prog_msg.message_id,
          f"*{ep_obj['short']} • {lang_obj['lang']}*\n❌ *Error:* `{e}`",
      )
    return

  if len(parts) == 4 and parts[0] == "dlq":
    _, session_id, key, q_i_str = parts
    sess = SESSIONS.get(session_id)
    q_store = sess.get("resolved_qualities", {}).get(key) if sess else None
    if not q_store:
      bot.answer_callback_query(call.id, "Session expire ho gaya.")
      return

    try:
      q_idx = int(q_i_str)
      chosen_item = q_store["links"][q_idx]
    except (ValueError, IndexError):
      bot.answer_callback_query(call.id, "Invalid quality.")
      return

    bot.answer_callback_query(call.id, "Download start ho raha hai...")
    try:
      scraper = make_scraper()
      download_and_send_file(
          call.message.chat.id,
          call.message.message_id,
          scraper,
          chosen_item,
          q_store["argon_url"],
          q_store["ep_label"],
          q_store["lang_label"],
      )
    except Exception as e:
      safe_edit(
          call.message.chat.id,
          call.message.message_id,
          f"❌ *Download/Send Error:* `{e}`",
      )
    return

  bot.answer_callback_query(call.id)


if __name__ == "__main__":
  print("Live Status & Progress Episode Bot chal raha hai...")
  bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=25)
