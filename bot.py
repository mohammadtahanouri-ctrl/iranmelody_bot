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

MAX_DURATION = 45 * 60        # پشتیبانی از فایل‌های طولانی (مداحی و فیلم)
MAX_FILE_MB = 48              
MAX_PARALLEL_DL = 4           
SEARCH_TIMEOUT = 12           
MAX_RESULTS = 10

# بیش از ۱۰ منبع معتبر ایرانی و بین‌المللی برای پوشش صد درصدی آهنگ‌ها، مداحی‌ها و ویدیوها
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
PROBE_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"

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


# ───────────────────────── ابزارها و FFMPEG ─────────────────────────
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
        f.write(YT_COOKIES.replace("\\n", "\n") + "\n")


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


def build_opts(workdir, clients=None, download=True, video=False):
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
                o["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                        "preferredcodec": "mp3",
                                        "preferredquality": "192"}]
    return o


# ───────────────────────── موتور جستجوی هوشمند چندمنبعی ─────────────────────────
def clean_title(t):
    t = re.sub(r"\s+", " ", t or "").strip()
    return re.sub(r"^دانلود\s+", "", t)


SKIP_PATH = ("/category/", "/tag/", "/page/", "/author/", "/feed", "/wp-", "/tags", "/cat/")


def search_ydl(prefix, label, q, n=4):
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


def search_all(q):
    # ترکیب جستجوی هم‌زمان در یوتیوب، ساندکلاود و تمامی ۱۰+ سایت بزرگ موزیک
    jobs = [(s["name"], (lambda s=s: search_site(s, q))) for s in SITES]
    jobs.append(("YouTube", lambda: search_ydl("ytsearch", "YouTube", q, 5)))
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

    merged = []
    # اولویت دادن به نتایج یوتیوب و سایت‌های اصلی
    for name in ["YouTube"] + [s["name"] for s in SITES] + ["SoundCloud"]:
        lst = got.get(name, [])
        for item in lst:
            if item not in merged and len(merged) < MAX_RESULTS:
                merged.append(item)
    return merged


# ───────────────────────── استخراج و دانلود مستقیم فایل ─────────────────────────
MP3_RE = re.compile(r"\.mp3(\?|$)", re.I)
PREVIEW_RE = re.compile(r"preview|sample|demo|snippet|teaser", re.I)


def fetch_site_links(page_url):
    links = []
    try:
        r = HTTP.get(page_url, timeout=10)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            seen = set()
            def add(href, text):
                href = requote_uri(urljoin(page_url, href.strip()))
                if not href.startswith("http") or href in seen or PREVIEW_RE.search(href):
                    return
                seen.add(href)
                m = re.search(r"(?<!\d)(320|128)(?!\d)", href + " " + text)
                q = m.group(1) if m else "128"
                links.append({"q": q, "url": href})

            for a in soup.find_all("a", href=True):
                href, text = a["href"], a.get_text(" ", strip=True)
                if MP3_RE.search(href) or re.search(r"320|128", text):
                    add(href, text)
            links.sort(key=lambda l: -(int(l["q"]) if l["q"] else 0))
    except Exception:
        pass
    if not links:
        links.append({"q": "320", "url": page_url})
    return links[:4]


def pick_file(workdir, vid):
    cands = [f for f in os.listdir(workdir)
             if f.startswith(f"{vid}.") and not f.endswith((".part", ".ytdl", ".json"))]
    for ext in (".mp3", ".m4a", ".mp4", ".webm"):
        for f in cands:
            if f.endswith(ext):
                return os.path.join(workdir, f)
    return os.path.join(workdir, cands[0]) if cands else None


def download_media(url, workdir, video=False):
    last = None
    for clients in YT_ATTEMPTS:
        try:
            with make_ydl(build_opts(workdir, clients, download=True, video=video)) as ydl:
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
        while len(CACHE) > 300:
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


@bot.message_handler(commands=["start", "help"])
def on_start(m):
    bot.reply_to(m, "سلام! نام آهنگ، مداحی یا فیلم مورد نظرتان را بفرستید تا از میان بیش از ۱۰ منبع معتبر جستجو شده و فایل مستقیم آن برایتان ارسال شود.")


@bot.message_handler(content_types=["text"])
def on_text(m):
    q = (m.text or "").strip()
    if not q or q.startswith("/"):
        return
    wait = bot.reply_to(m, f"🔎 در حال جستجو در ۱۰+ منبع برای «{q}» ...")
    results = search_all(q)
    if not results:
        safe_edit("نتیجه‌ای پیدا نشد. لطفاً عبارت کوتاه‌تری بفرستید.", m.chat.id, wait.message_id)
        return
    
    sid = cache_put({"results": results, "q": q})
    kb = types.InlineKeyboardMarkup()
    for i, r in enumerate(results):
        icon = "▶️" if r["src"] == "YouTube" else "🎵"
        kb.add(types.InlineKeyboardButton(f"{icon} {r['title'][:45]} ({r['src']})", callback_data=f"sel:{sid}:{i}"))
    
    safe_edit(f"نتایج یافت شده برای «{q}» — گزینه مورد نظر را انتخاب کنید:", m.chat.id, wait.message_id, kb)


@bot.callback_query_handler(func=lambda c: bool(c.data))
def on_callback(c):
    try:
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
            
            kb = types.InlineKeyboardMarkup()
            kb.row(
                types.InlineKeyboardButton("🎧 دانلود مستقیم صوتی (MP3)", callback_data=f"dl:audio:{key}:{idx}"),
                types.InlineKeyboardButton("🎬 دانلود مستقیم تصویری (MP4)", callback_data=f"dl:video:{key}:{idx}")
            )
            kb.add(types.InlineKeyboardButton("🔙 بازگشت به لیست نتایج", callback_data=f"back:{key}"))
            safe_edit(f"📥 گزینه انتخابی:\n<b>{res['title']}</b>\n\nفرمت دلخواه را انتخاب کنید تا مستقیماً دریافت کنید:", chat_id, msg_id, kb)
            
        elif kind == "back":
            results = d["results"]
            kb = types.InlineKeyboardMarkup()
            for i, r in enumerate(results):
                icon = "▶️" if r["src"] == "YouTube" else "🎵"
                kb.add(types.InlineKeyboardButton(f"{icon} {r['title'][:45]} ({r['src']})", callback_data=f"sel:{key}:{i}"))
            safe_edit(f"نتایج جستجو — یکی را انتخاب کنید:", chat_id, msg_id, kb)
            
        elif kind == "dl":
            mode = parts[2]
            idx = int(parts[3])
            res = d["results"][idx]
            is_video = (mode == "video")
            
            safe_edit(f"⏳ در حال آماده‌سازی و ارسال مستقیم فایل {'ویدیو' if is_video else 'صوتی'}... لطفاً شکیبا باشید.", chat_id, msg_id)
            
            workdir = tempfile.mkdtemp(prefix="bot_dl_")
            try:
                with DL_SEM:
                    if res["kind"] == "ydl" or res["src"] == "YouTube" or res["src"] == "SoundCloud":
                        path, info = download_media(res["url"], workdir, video=is_video)
                    else:
                        # اگر از سایت‌های وبلاگی بود، لینک‌های داخل صفحه را استخراج می‌کنیم
                        links = fetch_site_links(res["url"])
                        target_url = links[0]["url"] if links else res["url"]
                        path, info = download_media(target_url, workdir, video=is_video)
                
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
    except Exception:
        pass
    bot.infinity_polling(skip_pending=True, timeout=20, long_polling_timeout=20)
