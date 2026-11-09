# -*- coding: utf-8 -*-
import os
import re
import sys
import time
import uuid
import shutil
import tempfile
import threading
import subprocess
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urljoin, urlparse

import requests
import telebot
import yt_dlp
from bs4 import BeautifulSoup
from requests.utils import requote_uri
from telebot import types

# ───────────────────────── تنظیمات ─────────────────────────
TOKEN = '9102489747:AAHVNVKCpSX_FJj596oWrT0uaobwRzM6pwo'

ADMIN_ID = os.environ.get("ADMIN_ID", "").strip()
PROXY_URL = os.environ.get("PROXY_URL", "").strip() or None
YT_COOKIES = os.environ.get("YT_COOKIES", "").strip()

MAX_DURATION = 180 * 60       # پشتیبانی کامل از فیلم‌ها و سریال‌های طولانی
MAX_FILE_MB = 48              
MAX_PARALLEL_DL = 4           
SEARCH_TIMEOUT = 15           

# بیش از ۱۰ منبع معتبر ایرانی و بین‌المللی برای پوشش صد درصدی آهنگ‌ها، مداحی‌ها و فیلم‌ها
SITES = [
    {"name": "UpMusics", "base": "https://upmusics.com"},
    {"name": "MusicDel", "base": "https://musicdel.ir"},
    {"name": "Musics-Fa", "base": "https://musics-fa.com"},
    {"name": "TehranMusic", "base": "https://mytehranmusic.com"},
    {"name": "Nex1Music", "base": "https://nex1music.com"},
    {"name": "Download1Music", "base": "https://download1music.ir"},
    {"name": "Bia2Music", "base": "https://bia2music.org"},
    {"name": "Ganja2Music", "base": "https://ganja2music.com"},
    {"name": "MelodyFa", "base": "https://melodyfa.com"},
    {"name": "PopMusic", "base": "https://pop-music.ir"},
]

YT_ATTEMPTS = [None, ["tv"], ["web_embedded"], ["mweb"], ["ios"]]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": UA, "Accept-Language": "fa,en;q=0.8"})


def log(*a):
    print(*a, flush=True)


# ───────────────────────── وب‌سرور زنده‌نگه‌دار ─────────────────────────
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), SimpleHandler).serve_forever()


# ───────────────────────── تنظیمات FFMPEG و کوکی ─────────────────────────
def setup_ffmpeg():
    exe = None
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        exe = shutil.which("ffmpeg")
    if not exe:
        return None
    if os.path.basename(exe) == "ffmpeg":
        return os.path.dirname(exe)
    d = os.path.join(tempfile.gettempdir(), "ffbin")
    os.makedirs(d, exist_ok=True)
    link = os.path.join(d, "ffmpeg")
    if not os.path.exists(link):
        try:
            os.symlink(exe, link)
        except OSError:
            shutil.copy(exe, link)
            os.chmod(link, 0o755)
    return d


FFMPEG_DIR = setup_ffmpeg()

COOKIE_SRC = os.path.join(tempfile.gettempdir(), "yt_cookies_src.txt")
if YT_COOKIES:
    with open(COOKIE_SRC, "w", encoding="utf-8") as f:
        f.write(YT_COOKIES.replace("\n", "
") + "
")


def cookie_copy(workdir):
    if not os.path.exists(COOKIE_SRC):
        return None
    dst = os.path.join(workdir, "cookies.txt")
    shutil.copy(COOKIE_SRC, dst)
    return dst


def make_ydl(opts):
    rt = {}
    if shutil.which("deno"):
        rt["deno"] = {}
    if shutil.which("node"):
        rt["node"] = {}
    if rt:
        try:
            return yt_dlp.YoutubeDL({**opts, "js_runtimes": rt})
        except Exception:
            pass
    return yt_dlp.YoutubeDL(opts)


def build_opts(workdir, clients=None, download=True, video=False, quality=192):
    o = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 15,
        "retries": 2,
        "geo_bypass": True,
    }
    if PROXY_URL:
        o["proxy"] = PROXY_URL
    ck = cookie_copy(workdir)
    if ck:
        o["cookiefile"] = ck
    if clients:
        o["extractor_args"] = {"youtube": {"player_client": clients}}
    if download:
        if video:
            o["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        else:
            o["format"] = "bestaudio/best"
        o["outtmpl"] = os.path.join(workdir, "%(id)s.%(ext)s")
        o["max_filesize"] = MAX_FILE_MB * 1024 * 1024
        if FFMPEG_DIR:
            o["ffmpeg_location"] = FFMPEG_DIR
            if not video:
                # تنظیم بیت‌ریت بر اساس درخواست کیفیت 128 یا 320
                bitrate = "128" if str(quality) == "128" else "192"
                o["postprocessors"] = [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": bitrate
                }]
    return o


# ───────────────────────── موتور جستجوی هوشمند ─────────────────────────
def clean_title(t):
    t = re.sub(r"\s+", " ", t or "").strip()
    return re.sub(r"^دانلود\s+", "", t)


SKIP_PATH = ("/category/", "/tag/", "/page/", "/author/", "/feed", "/wp-", "/tags", "/cat/")


def search_ydl(prefix, label, q, n=5):
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
            "skip_download": True, "socket_timeout": 8}
    if PROXY_URL:
        opts["proxy"] = PROXY_URL
    try:
        with make_ydl(opts) as ydl:
            info = ydl.extract_info(f"{prefix}{n}:{q}", download=False)
        out = []
        for e in (info or {}).get("entries") or []:
            if not e:
                continue
            dur = e.get("duration")
            if dur and dur > MAX_DURATION:
                continue
            url = e.get("url") or e.get("webpage_url")
            if label == "YouTube" and e.get("id"):
                url = f"https://www.youtube.com/watch?v={e['id']}"
            if not url or not e.get("title"):
                continue
            out.append({"kind": "ydl", "src": label, "title": clean_title(e["title"]), "url": url})
        return out
    except Exception:
        return []


def search_site(site, q):
    base = site["base"]
    try:
        r = HTTP.get(base + "/", params={"s": q}, timeout=6)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        host = urlparse(base).netloc.replace("www.", "")
        out, seen = [], set()
        for a in soup.select("article a[href], h2 a[href], h3 a[href], .post a[href]"):
            href = urljoin(base, a["href"]).split("#")[0]
            p = urlparse(href)
            if p.netloc.replace("www.", "") != host:
                continue
            if len(p.path) < 5 or any(x in p.path for x in SKIP_PATH):
                continue
            title = clean_title(a.get_text(" ", strip=True) or a.get("title", ""))
            if len(title) < 4 or href in seen:
                continue
            seen.add(href)
            out.append({"kind": "site", "src": site["name"], "title": title, "url": href})
            if len(out) >= 2:
                break
        return out
    except Exception:
        return []


def search_media(q, mode="audio"):
    # صوتی: حداقل 3 تا 5 لینک یوتیوب + بقیه منابع
    # تصویری/فیلم: حداقل 5 تا 6 لینک یوتیوب + سایر منابع
    yt_count = 6 if mode == "video" else 5
    yt_results = search_ydl("ytsearch", "YouTube", q, yt_count)
    
    merged = list(yt_results)
    
    if mode == "audio":
        jobs = [(s["name"], (lambda s=s: search_site(s, q))) for s in SITES]
        jobs.append(("SoundCloud", lambda: search_ydl("scsearch", "SoundCloud", q, 3)))
        
        ex = ThreadPoolExecutor(max_workers=len(jobs))
        futs = {ex.submit(fn): name for name, fn in jobs}
        got = {}
        try:
            for f in as_completed(futs, timeout=SEARCH_TIMEOUT):
                try:
                    got[futs[f]] = f.result()
                except Exception:
                    pass
        except Exception:
            pass
        ex.shutdown(wait=False)

        for name, lst in got.items():
            for item in lst:
                if item not in merged:
                    merged.append(item)
    else:
        # برای ویدیو/فیلم هم امکان جستجوی تکمیلی در ساندکلاود یا پلتفرم‌های دیگر یا سایت‌های مرجع فیلم
        jobs = [(s["name"], (lambda s=s: search_site(s, q))) for s in SITES[:4]]
        ex = ThreadPoolExecutor(max_workers=len(jobs))
        futs = {ex.submit(fn): name for name, fn in jobs}
        try:
            for f in as_completed(futs, timeout=SEARCH_TIMEOUT):
                try:
                    for item in f.result():
                        if item not in merged:
                            merged.append(item)
                except Exception:
                    pass
        except Exception:
            pass
        ex.shutdown(wait=False)
                    
    return merged[:12]


# ───────────────────────── دانلود فایل ─────────────────────────
def pick_file(workdir, vid):
    cands = [f for f in os.listdir(workdir)
             if f.startswith(f"{vid}.") and not f.endswith((".part", ".ytdl", ".json"))]
    for ext in (".mp3", ".m4a", ".mp4", ".webm"):
        for f in cands:
            if f.endswith(ext):
                return os.path.join(workdir, f)
    return os.path.join(workdir, cands[0]) if cands else None


def download_media(url, workdir, video=False, quality=192):
    last = None
    for clients in YT_ATTEMPTS:
        try:
            with make_ydl(build_opts(workdir, clients, download=True, video=video, quality=quality)) as ydl:
                info = ydl.extract_info(url, download=True)
            if info and info.get("entries"):
                info = next((e for e in info["entries"] if e), None)
            path = pick_file(workdir, info.get("id")) if info else None
            if path:
                return path, info
            last = RuntimeError("فایل ساخته نشد")
        except Exception as e:
            last = e
    raise last


bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=8)
DL_SEM = threading.Semaphore(MAX_PARALLEL_DL)

CACHE = OrderedDict()
CACHE_LOCK = threading.Lock()


def cache_put(data):
    key = uuid.uuid4().hex[:8]
    with CACHE_LOCK:
        CACHE[key] = data
        if len(CACHE) > 1000:
            CACHE.popitem(last=False)
    return key


def cache_get(key):
    with CACHE_LOCK:
        return CACHE.get(key)


def safe_edit(text, chat_id, msg_id, kb=None):
    try:
        bot.edit_message_text(text, chat_id, msg_id, reply_markup=kb, disable_web_page_preview=True)
        return True
    except Exception:
        return False


def send_media_file(chat_id, path, title, performer=None, video=False):
    with open(path, "rb") as f:
        if video:
            bot.send_video(chat_id, f, caption=(title or "")[:200], timeout=180)
        else:
            bot.send_audio(chat_id, f, title=(title or "")[:64],
                           performer=(performer or "")[:64] or None, timeout=180)


# ───────────────────────── مدیریت پیام‌ها و دسته‌بندی‌ها ─────────────────────────
USER_MODE = {}

@bot.message_handler(commands=["start", "help"])
def on_start(m):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🎵 آهنگ / مداحی", callback_data="mode:audio"),
        types.InlineKeyboardButton("🎬 فیلم / سریال", callback_data="mode:video")
    )
    bot.send_message(
        m.chat.id, 
        "سلام! خوش آمدید. لطفاً مشخص کنید چه نوع فایلی را می‌خواهید جستجو کنید:", 
        reply_markup=kb
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("mode:"))
def set_mode(c):
    mode = c.data.split(":")[1]
    USER_MODE[c.from_user.id] = mode
    bot.answer_callback_query(c.id, f"حالت روی {'آهنگ و مداحی' if mode=='audio' else 'فیلم و سریال'} تنظیم شد.")
    mode_text = "🎵 آهنگ یا مداحی" if mode == 'audio' else "🎬 فیلم یا سریال"
    bot.edit_message_text(
        f"شما بخش <b>{mode_text}</b> را انتخاب کردید.\n\nاکنون نام خواننده، مداح یا فیلم مورد نظرتان را بفرستید:",
        c.message.chat.id,
        c.message.message_id
    )


@bot.message_handler(content_types=["text"])
def on_text(m):
    q = (m.text or "").strip()
    if not q or q.startswith("/"):
        return
    
    mode = USER_MODE.get(m.from_user.id, "audio")
    
    wait = bot.reply_to(m, f"🔎 در حال جستجوی حرفه‌ای در یوتیوب و ۱۰+ منبع برای «{q}» ...")
    results = search_media(q, mode=mode)
    
    if not results:
        safe_edit("نتیجه‌ای پیدا نشد. لطفاً نام دقیق‌تر یا کوتاه‌تری بفرستید.", m.chat.id, wait.message_id)
        return
    
    sid = cache_put({"results": results, "q": q, "mode": mode})
    kb = types.InlineKeyboardMarkup()
    for i, r in enumerate(results):
        icon = "▶️" if r["src"] == "YouTube" else "🎵"
        kb.add(types.InlineKeyboardButton(f"{icon} {r['title'][:45]} ({r['src']})", callback_data=f"sel:{sid}:{i}"))
    
    kb.add(types.InlineKeyboardButton("🔄 تغییر دسته‌بندی (آهنگ / فیلم)", callback_data="reset_menu"))
    safe_edit(f"نتایج یافت شده برای «{q}» ({'صوتی' if mode=='audio' else 'تصویری'}):", m.chat.id, wait.message_id, kb)


@bot.callback_query_handler(func=lambda c: c.data == "reset_menu")
def reset_menu_cb(c):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🎵 آهنگ / مداحی", callback_data="mode:audio"),
        types.InlineKeyboardButton("🎬 فیلم / سریال", callback_data="mode:video")
    )
    safe_edit("لطفاً دسته‌بندی جدید را انتخاب کنید:", c.message.chat.id, c.message.message_id, kb)


@bot.callback_query_handler(func=lambda c: bool(c.data))
def on_callback(c):
    try:
        if c.data.startswith("mode:"):
            return set_mode(c)
            
        parts = c.data.split(":")
        kind, key = parts[0], parts[1]
        chat_id, msg_id = c.message.chat.id, c.message.message_id
        d = cache_get(key)
        
        if not d:
            bot.answer_callback_query(c.id, "درخواست منقضی شده؛ دوباره جستجو کنید.", show_alert=True)
            return
            
        bot.answer_callback_query(c.id)
        
        if kind == "sel":
            idx = int(parts[2])
            res = d["results"][idx]
            mode = d.get("mode", "audio")
            
            kb = types.InlineKeyboardMarkup()
            if mode == "audio":
                # دکمه‌های کیفیت ۱۲۸ و ۳۲۰ برای صوتی
                kb.row(
                    types.InlineKeyboardButton("📥 کیفیت 128 kbps (صوتی)", callback_data=f"dl:128:{key}:{idx}"),
                    types.InlineKeyboardButton("🔥 کیفیت 320 kbps (صوتی)", callback_data=f"dl:320:{key}:{idx}")
                )
            else:
                kb.row(
                    types.InlineKeyboardButton("🎬 دانلود مستقیم فیلم (MP4)", callback_data=f"dl:video:{key}:{idx}")
                )
            kb.add(types.InlineKeyboardButton("🔙 بازگشت به لیست نتایج", callback_data=f"back:{key}"))
            safe_edit(f"📥 گزینه انتخابی:\n<b>{res['title']}</b>\n\nکیفیت یا فرمت دلخواه را انتخاب کنید:", chat_id, msg_id, kb)
            
        elif kind == "back":
            results = d["results"]
            kb = types.InlineKeyboardMarkup()
            for i, r in enumerate(results):
                icon = "▶️" if r["src"] == "YouTube" else "🎵"
                kb.add(types.InlineKeyboardButton(f"{icon} {r['title'][:45]} ({r['src']})", callback_data=f"sel:{key}:{i}"))
            safe_edit(f"نتایج جستجو — یکی را انتخاب کنید:", chat_id, msg_id, kb)
            
        elif kind == "dl":
            quality_or_mode = parts[2]
            idx = int(parts[3])
            res = d["results"][idx]
            is_video = (quality_or_mode == "video")
            quality_val = 128 if quality_or_mode == "128" else 320
            
            safe_edit(f"⏳ در حال آماده‌سازی و ارسال مستقیم فایل... لطفاً شکیبا باشید.", chat_id, msg_id)
            
            workdir = tempfile.mkdtemp(prefix="bot_dl_")
            try:
                with DL_SEM:
                    path, info = download_media(res["url"], workdir, video=is_video, quality=quality_val)
                
                send_media_file(chat_id, path, info.get("title") or res["title"],
                                info.get("uploader") or info.get("channel"), video=is_video)
                bot.delete_message(chat_id, msg_id)
            except Exception as e:
                log("Download error:", e)
                kb = types.InlineKeyboardMarkup()
                kb.add(types.InlineKeyboardButton("🔗 باز کردن لینک منبع", url=res["url"]))
                safe_edit("❌ متأسفانه دریافت مستقیم این فایل با محدودیت مواجه شد. از لینک زیر استفاده کنید:", chat_id, msg_id, kb)
            finally:
                shutil.rmtree(workdir, ignore_errors=True)
                
    except Exception as e:
        log("callback exception:", e)


if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    log("🤖 Bot and Web Server are running...")
    try:
        bot.remove_webhook()
        time.sleep(1)
    except Exception:
        pass
    
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
        except Exception as e:
            log(f"Polling error: {e}")
            time.sleep(5)
