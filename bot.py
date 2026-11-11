# -*- coding: utf-8 -*-
"""
ربات دانلود آهنگ، مداحی، فیلم و سریال  (نسخه ۳)

تفاوت‌های مهم نسخه ۳:
  • منابع فیلم و سریال اضافه شد (چند سایت ایرانی + movieland.af) و صفحه‌بندی لینک‌ها برای سریال‌ها
  • نام و آدرس سایت‌ها هیچ‌جا به کاربر نشان داده نمی‌شود؛ کاربر فقط عنوان و دکمه‌ی دانلود می‌بیند
  • دکمه‌های دانلود از آدرس خودِ ربات (/go/...) رد می‌شوند و بعد کاربر به فایل منتقل می‌شود
    (توکن امضا‌شده و منقضی‌شونده؛ نیازی به دیتابیس ندارد)
  • بدون وابستگی به حافظه‌ی سرور: ری‌استارت یا دو نسخه‌ی هم‌زمان مشکلی ایجاد نمی‌کند
  • دستور ادمین /sites برای تست سلامت همه‌ی سایت‌ها، /probe برای تست یوتیوب، /status

متغیرهای محیطی (Render > Environment)، همه اختیاری هستند:
  BOT_TOKEN     توکن ربات (اگر نگذارید همین توکن داخل کد استفاده می‌شود)
  ADMIN_ID      آیدی عددی شما؛ خطاها برایتان ارسال می‌شود + دستورهای /status /probe /sites
  PUBLIC_URL    آدرس سرویس Render شما (اگر نگذارید RENDER_EXTERNAL_URL خودکار استفاده می‌شود)
  LINK_TTL_HOURS مدت اعتبار لینک‌های دانلود (پیش‌فرض ۲۴ ساعت)
  YT_COOKIES    متن cookies.txt یک اکانت گوگل جدا (برای بلاک شدن IP سرور در یوتیوب)
  PROXY_URL     پروکسی برای یوتیوب، مثل http://user:pass@host:port
  POT_BASE_URL  آدرس سرور bgutil (PO Token)
  TG_API_ID, TG_API_HASH, TG_SESSION   جستجوی سراسری تلگرام (با make_session.py)
  TG_CHANNELS   کانال‌های عمومی برای جستجوی بدون اکانت: chan1,chan2
  FILM_SITES    سایت‌های اضافه‌ی فیلم، جدا شده با کاما. برای سایت‌های بدون جستجوی وردپرسی
                (مثل سایت‌های جاوااسکریپتی) آخر آدرس |ddg بگذارید:
                https://site1.com,https://site2.com|ddg
  MUSIC_SITES_EXTRA  سایت‌های اضافه‌ی موزیک (همان قالب)
"""
import asyncio
import base64
import hashlib
import hmac
import html
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
import telebot
import yt_dlp
from bs4 import BeautifulSoup
from requests.utils import requote_uri
from telebot import types

# ───────────────────────── تنظیمات ─────────────────────────
TOKEN = os.environ.get("BOT_TOKEN", "").strip() or "9102489747:AAHVNVKCpSX_FJj596oWrT0uaobwRzM6pwo"

ADMIN_ID = os.environ.get("ADMIN_ID", "").strip()
PROXY_URL = os.environ.get("PROXY_URL", "").strip() or None
YT_COOKIES = os.environ.get("YT_COOKIES", "").strip()
POT_BASE_URL = os.environ.get("POT_BASE_URL", "").strip() or None
TG_API_ID = os.environ.get("TG_API_ID", "").strip()
TG_API_HASH = os.environ.get("TG_API_HASH", "").strip()
TG_SESSION = os.environ.get("TG_SESSION", "").strip()
TG_CHANNELS = [c.strip().lstrip("@") for c in os.environ.get("TG_CHANNELS", "").split(",") if c.strip()]
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
LINK_TTL = int(os.environ.get("LINK_TTL_HOURS", "24") or 24) * 3600
LINK_KEY = hashlib.sha256((os.environ.get("LINK_SECRET") or TOKEN).encode() + b"|go").digest()

MAX_FILE_MB = 48           # سقف آپلود ربات‌های تلگرام ≈ 50MB
MAX_PARALLEL_DL = 3
SEARCH_TIMEOUT = 20
MAX_DURATION = {"a": 45 * 60, "v": 6 * 3600}
SITE_QUOTA = {"a": 6, "v": 7}     # حداکثر نتیجه از مجموع سایت‌ها
LINKS_PER_PAGE = 6
# نمایش نام سایت منبع کنار هر نتیجه (برای تست سلامت سایت‌ها). برای خاموش کردن: SHOW_SITE_NAMES=0
SHOW_SITE_NAMES = os.environ.get("SHOW_SITE_NAMES", "1").strip() != "0"

MUSIC_SITES = [
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

# سایت‌های فیلم و سریال (نام‌ها فقط برای ادمین است و هرگز به کاربر نشان داده نمی‌شود)
FILM_SITES = [
    {"name": "moviesho.com", "base": "https://www.moviesho.com"},
    {"name": "doostihaa.com", "base": "https://www.doostihaa.com"},
    {"name": "hexdl.com", "base": "https://hexdl.com"},
    {"name": "vipofilm.com", "base": "https://vipofilm.com"},
    {"name": "dlrozaneh.ir", "base": "https://dlrozaneh.ir"},
    # سایت جاوااسکریپتی: جستجو از طریق موتور جستجو + استخراج لینک از کد صفحه
    {"name": "movieland.af", "base": "https://movieland.af", "search": "ddg",
     "path_re": r"^/(movies|series)/[^/]+/?$", "strip": ["مووی لند", "Movieland", "movieland"]},
]


def _parse_extra_sites(env_name):
    out = []
    for item in os.environ.get(env_name, "").split(","):
        item = item.strip()
        if not item:
            continue
        url, _, flag = item.partition("|")
        url = url.strip().rstrip("/")
        if url.startswith("http"):
            site = {"name": urlparse(url).netloc.replace("www.", ""), "base": url}
            if flag.strip().lower() == "ddg":
                site["search"] = "ddg"
            out.append(site)
    return out


FILM_SITES += _parse_extra_sites("FILM_SITES")
MUSIC_SITES += _parse_extra_sites("MUSIC_SITES_EXTRA")

# ترتیب تلاش برای یوتیوب: اول پیش‌فرض yt-dlp، بعد کلاینت‌های جایگزین
YT_ATTEMPTS = [None, ["tv"], ["web_embedded"], ["mweb"], ["android_vr"], ["ios"]]
PROBE_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": UA, "Accept-Language": "fa,en;q=0.8"})

WAIT_TEXT = {
    "a": "🔎 در حال پیدا کردن آهنگ (مداحی) مدنظر شما",
    "v": "🔎 در حال پیدا کردن فیلم (سریال) مدنظر شما",
}
SRC_FA = {"Telegram": "تلگرام", "YouTube": "یوتیوب", "SoundCloud": "ساندکلاود"}
ICON = {"YouTube": "▶️", "SoundCloud": "☁️", "Telegram": "✈️"}


def log(*a):
    print(*a, flush=True)


def esc(s):
    return html.escape(str(s or ""), quote=False)


def trunc(s, n):
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


# ───────────────────────── لینک‌های امضاشده‌ی /go/ ─────────────────────────
class LinkExpired(Exception):
    pass


def _keystream(nonce, n):
    out, ctr = b"", 0
    while len(out) < n:
        out += hashlib.blake2b(nonce + ctr.to_bytes(4, "big"), key=LINK_KEY, digest_size=64).digest()
        ctr += 1
    return out[:n]


def make_token(url, ttl=None):
    exp = int(time.time()) + (ttl or LINK_TTL)
    data = exp.to_bytes(5, "big") + url.encode()
    nonce = os.urandom(8)
    ct = bytes(a ^ b for a, b in zip(data, _keystream(nonce, len(data))))
    tag = hmac.new(LINK_KEY, nonce + ct, hashlib.sha256).digest()[:10]
    return base64.urlsafe_b64encode(nonce + ct + tag).decode().rstrip("=")


def read_token(tok):
    raw = base64.urlsafe_b64decode(tok + "=" * (-len(tok) % 4))
    nonce, ct, tag = raw[:8], raw[8:-10], raw[-10:]
    good = hmac.new(LINK_KEY, nonce + ct, hashlib.sha256).digest()[:10]
    if not hmac.compare_digest(tag, good):
        raise ValueError("bad token")
    data = bytes(a ^ b for a, b in zip(ct, _keystream(nonce, len(ct))))
    if int.from_bytes(data[:5], "big") < time.time():
        raise LinkExpired()
    return data[5:].decode()


def wrap_url(url):
    """آدرس نمایش‌داده‌شده به کاربر: از سرور خودِ ربات رد می‌شود تا نام سایت مبدأ داخل تلگرام دیده نشود."""
    if not PUBLIC_URL:
        return url
    try:
        return f"{PUBLIC_URL}/go/{make_token(url)}"
    except Exception:
        return url


# ───────────────────────── وب‌سرور (زنده‌نگه‌دار + /go/) ─────────────────────────
class SimpleHandler(BaseHTTPRequestHandler):
    def _page(self, code, text):
        body = ("<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,"
                "initial-scale=1'><body dir='rtl' style='font-family:sans-serif;text-align:center;"
                f"padding:48px 16px'>{text}</body>").encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _route(self):
        if self.path.startswith("/go/"):
            tok = self.path[4:].split("?")[0].split("#")[0]
            try:
                url = read_token(tok)
            except LinkExpired:
                return self._page(410, "این لینک منقضی شده است.<br>دوباره از داخل ربات دریافتش کنید.")
            except Exception:
                return self._page(404, "لینک نامعتبر است.")
            self.send_response(302)
            self.send_header("Location", url)
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._page(200, "Bot is alive!")

    def do_GET(self):
        self._route()

    def do_HEAD(self):
        self._route()

    def log_message(self, *args):
        pass


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    ThreadingHTTPServer(("0.0.0.0", port), SimpleHandler).serve_forever()


def self_ping():
    """Render رایگان بعد از ۱۵ دقیقه بی‌ترافیکی می‌خوابد؛ هر ۸ دقیقه خودمان را صدا می‌زنیم."""
    url = PUBLIC_URL
    if not url:
        return
    while True:
        time.sleep(8 * 60)
        try:
            requests.get(url, timeout=15)
        except Exception:
            pass


# ───────────────────────── ffmpeg / JS runtime / کوکی ─────────────────────────
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
    with open(COOKIE_SRC, "w", encoding="utf-8") as _f:
        _f.write(YT_COOKIES.replace("\\n", "\n") + "\n")


def cookie_copy(workdir):
    if not os.path.exists(COOKIE_SRC):
        return None
    dst = os.path.join(workdir, "cookies.txt")
    shutil.copy(COOKIE_SRC, dst)
    return dst


def ensure_js_runtime():
    """yt-dlp برای یوتیوب به JS runtime (deno/node) نیاز دارد. اگر روی سرور نبود، deno را دانلود می‌کنیم."""
    if shutil.which("deno") or shutil.which("node"):
        return
    if platform.system() != "Linux":
        return
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(
        platform.machine().lower())
    if not arch:
        return
    d = os.path.join(tempfile.gettempdir(), "deno_bin")
    exe = os.path.join(d, "deno")
    try:
        if not os.path.exists(exe):
            os.makedirs(d, exist_ok=True)
            zp = os.path.join(d, "deno.zip")
            url = f"https://github.com/denoland/deno/releases/latest/download/deno-{arch}-unknown-linux-gnu.zip"
            with HTTP.get(url, stream=True, timeout=180) as r:
                r.raise_for_status()
                with open(zp, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            with zipfile.ZipFile(zp) as z:
                z.extract("deno", d)
            os.chmod(exe, 0o755)
            os.remove(zp)
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        log("✅ deno آماده شد")
    except Exception as e:
        log("نصب deno ناموفق بود:", e)


def make_ydl(opts):
    rt = {}
    if shutil.which("deno"):
        rt["deno"] = {}
    if shutil.which("node"):
        rt["node"] = {}
    if rt:
        try:
            return yt_dlp.YoutubeDL({**opts, "js_runtimes": rt})
        except Exception as e:
            log("js_runtimes نپذیرفته شد:", e)
    return yt_dlp.YoutubeDL(opts)


def build_opts(workdir, clients=None, download=True, mode="a", kind="hq"):
    o = {"quiet": True, "no_warnings": True, "noplaylist": True,
         "socket_timeout": 20, "retries": 2, "geo_bypass": True}
    if PROXY_URL:
        o["proxy"] = PROXY_URL
    ck = cookie_copy(workdir)
    if ck:
        o["cookiefile"] = ck
    ea = {}
    if clients:
        ea["youtube"] = {"player_client": clients}
    if POT_BASE_URL:
        ea["youtubepot-bgutilhttp"] = {"base_url": [POT_BASE_URL]}
    if ea:
        o["extractor_args"] = ea
    if not download:
        return o
    o["outtmpl"] = os.path.join(workdir, "%(id)s.%(ext)s")
    o["max_filesize"] = MAX_FILE_MB * 1024 * 1024
    if FFMPEG_DIR:
        o["ffmpeg_location"] = FFMPEG_DIR
    if mode == "v":
        h = 360 if kind == "360" else 720
        if FFMPEG_DIR:
            o["format"] = (f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
                           f"best[height<={h}][ext=mp4]/best[height<={h}]")
            o["merge_output_format"] = "mp4"
        else:
            o["format"] = f"best[height<={h}][ext=mp4]/best[height<={h}]"
    else:
        if FFMPEG_DIR:
            o["format"] = "bestaudio/best"
            o["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                    "preferredquality": "128" if kind == "lo" else "192"}]
        else:
            o["format"] = "bestaudio[ext=m4a]/bestaudio"
    return o


# ───────────────────────── تلگرام (یوزربات + جستجوی وب کانال‌ها) ─────────────────────────
class UserBot:
    """جستجوی سراسری در همه‌ی کانال‌ها و گروه‌های عمومی تلگرام؛ فقط با اکانت کاربری ممکن است."""

    def __init__(self):
        self.ok = False
        self.loop = None
        self.client = None

    def run(self, coro, timeout=40):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def start(self):
        if not (TG_API_ID and TG_API_HASH and TG_SESSION):
            return
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
        except Exception as e:
            log("telethon نصب نیست:", e)
            return
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

        async def _boot():
            self.client = TelegramClient(StringSession(TG_SESSION), int(TG_API_ID), TG_API_HASH)
            await self.client.connect()
            return await self.client.is_user_authorized()

        try:
            self.ok = bool(self.run(_boot(), 60))
            log("✅ جستجوی سراسری تلگرام فعال شد" if self.ok else "⚠️ TG_SESSION معتبر نیست")
        except Exception as e:
            log("اتصال یوزربات ناموفق:", e)

    def search(self, q, mode, limit=4):
        async def _s():
            from telethon.tl import types as T
            flts = [T.InputMessagesFilterMusic] if mode == "a" else \
                [T.InputMessagesFilterVideo, T.InputMessagesFilterDocument]
            out, seen = [], set()
            for flt in flts:
                try:
                    async for m in self.client.iter_messages(None, search=q, filter=flt, limit=limit * 4):
                        f = m.file
                        if not f:
                            continue
                        if flt is T.InputMessagesFilterDocument and not (f.mime_type or "").startswith("video/"):
                            continue
                        chat = m.chat or await m.get_chat()
                        un = getattr(chat, "username", None)
                        if not un or (un, m.id) in seen:
                            continue
                        seen.add((un, m.id))
                        if f.title:
                            title = f"{f.performer} - {f.title}" if f.performer else f.title
                        else:
                            title = f.name or (m.message or "").split("\n")[0]
                        out.append({"kind": "tg", "src": "Telegram", "chat": un, "mid": m.id,
                                    "title": re.sub(r"\s+", " ", title or "بدون عنوان").strip()[:120],
                                    "size": f.size or 0, "url": f"https://t.me/{un}/{m.id}"})
                        if len(out) >= limit:
                            return out
                except Exception as e:
                    log("[tg-search]", str(e)[:200])
            return out
        return self.run(_s(), 30)

    def meta(self, chat, mid):
        async def _m():
            msg = await self.client.get_messages(chat, ids=mid)
            if not msg or not msg.file:
                return None
            return {"size": msg.file.size or 0, "name": msg.file.name or ""}
        return self.run(_m(), 20)

    def download(self, chat, mid, workdir):
        async def _d():
            msg = await self.client.get_messages(chat, ids=mid)
            if not msg or not msg.media:
                raise RuntimeError("پیام پیدا نشد")
            return await self.client.download_media(msg, file=workdir + os.sep)
        return self.run(_d(), 900)


USERBOT = UserBot()
AUDIO_EXT = re.compile(r"\.(mp3|m4a|flac|ogg|wav|aac)$", re.I)
VIDEO_EXT = re.compile(r"\.(mp4|mkv|avi|mov|webm)$", re.I)


def search_tme_channel(ch, q, mode):
    """جستجوی بدون اکانت: صفحه‌ی عمومی t.me/s/<کانال>?q=..."""
    r = HTTP.get(f"https://t.me/s/{ch}", params={"q": q}, timeout=8)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for w in soup.select("div.tgme_widget_message[data-post]"):
        try:
            un, mid = w["data-post"].split("/")
            mid = int(mid)
        except Exception:
            continue
        doc = w.select_one(".tgme_widget_message_document_title")
        doc_name = doc.get_text(" ", strip=True) if doc else ""
        is_video = bool(w.select_one(".tgme_widget_message_video_player")) or bool(VIDEO_EXT.search(doc_name))
        is_audio = bool(AUDIO_EXT.search(doc_name))
        if (mode == "a" and not is_audio) or (mode == "v" and not is_video):
            continue
        txt = w.select_one(".tgme_widget_message_text")
        title = doc_name or (txt.get_text(" ", strip=True) if txt else "") or "بدون عنوان"
        out.append({"kind": "tg", "src": "Telegram", "chat": un, "mid": mid,
                    "title": trunc(re.sub(r"\s+", " ", title).strip(), 120),
                    "size": 0, "url": f"https://t.me/{un}/{mid}"})
        if len(out) >= 2:
            break
    return out


def search_telegram(q, mode):
    out = []
    if USERBOT.ok:
        try:
            out += USERBOT.search(q, mode, 4)
        except Exception as e:
            log("[tg]", str(e)[:200])
    if not out and TG_CHANNELS:
        for ch in TG_CHANNELS[:8]:
            try:
                out += search_tme_channel(ch, q, mode)
            except Exception:
                pass
            if len(out) >= 4:
                break
    return out[:4]


# ───────────────────────── جستجو ─────────────────────────
def clean_title(t):
    t = re.sub(r"\s+", " ", t or "").strip()
    c = re.sub(r"^دانلود\s+(فیلم|سریال|انیمیشن|انیمه|مستند|آهنگ|مداحی|موزیک|آلبوم)?\s*", "", t)
    return c if len(c) >= 3 else t


SKIP_PATH = ("/category/", "/tag/", "/page/", "/author/", "/feed", "/wp-", "/tags", "/cat/",
             "/genre", "/country", "/year/", "/actor", "/director", "/login", "/register")


def search_site_wp(site, q, mode):
    """جستجوی استاندارد وردپرسی: /?s=عبارت"""
    base = site["base"]
    r = HTTP.get(base + "/", params={"s": q}, timeout=7)
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    host = urlparse(base).netloc.replace("www.", "")
    out, seen = [], set()
    for a in soup.select("article a[href], h2 a[href], h3 a[href]"):
        href = urljoin(base, a["href"]).split("#")[0]
        p = urlparse(href)
        if p.netloc.replace("www.", "") != host:
            continue
        if len(p.path) < 6 or any(x in p.path for x in SKIP_PATH):
            continue
        title = clean_title(a.get_text(" ", strip=True) or a.get("title", ""))
        if len(title) < 4 or href in seen:
            continue
        seen.add(href)
        out.append({"kind": "site", "src": site["name"], "title": title, "url": href})
        if len(out) >= 2:
            break
    return out


def search_site_ddg(site, q, mode):
    """برای سایت‌هایی که جستجوی سمت سرور ندارند: جستجوی site:دامنه در موتور جستجو."""
    host = urlparse(site["base"]).netloc
    r = HTTP.get("https://html.duckduckgo.com/html/", params={"q": f"site:{host} {q}"},
                 headers={"Referer": "https://duckduckgo.com/"}, timeout=8)
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    pre = re.compile(site.get("path_re", "."))
    out, seen = [], set()
    for a in soup.select("a.result__a[href]"):
        href = a["href"]
        if "uddg=" in href:
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [""])[0])
        elif href.startswith("//"):
            href = "https:" + href
        p = urlparse(href)
        if p.netloc.replace("www.", "") != host.replace("www.", "") or not pre.search(p.path):
            continue
        if href in seen:
            continue
        title = a.get_text(" ", strip=True)
        for w in site.get("strip", []):
            title = title.replace(w, " ")
        title = clean_title(re.sub(r"^[\s|\-–—:]+|[\s|\-–—:]+$", "", title))
        if len(title) < 2:
            continue
        seen.add(href)
        out.append({"kind": "site", "src": site["name"], "title": title, "url": href})
        if len(out) >= 2:
            break
    return out


def search_site(site, q, mode):
    if site.get("search") == "ddg":
        return search_site_ddg(site, q, mode)
    res = search_site_wp(site, q, mode)
    return res


def search_ydl(prefix, label, q, mode, n=5):
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
            "skip_download": True, "socket_timeout": 10}
    if PROXY_URL:
        opts["proxy"] = PROXY_URL
    with make_ydl(opts) as ydl:
        info = ydl.extract_info(f"{prefix}{n}:{q}", download=False)
    out = []
    for e in (info or {}).get("entries") or []:
        if not e or not e.get("title"):
            continue
        dur = e.get("duration")
        if dur and dur > MAX_DURATION[mode]:
            continue
        url = e.get("url") or e.get("webpage_url")
        res = {"kind": "ydl", "src": label, "title": clean_title(e["title"])}
        if label == "YouTube":
            if not e.get("id"):
                continue
            res["id"] = e["id"]
            url = f"https://www.youtube.com/watch?v={e['id']}"
        if not url:
            continue
        res["url"] = url
        out.append(res)
    return out


def norm_title(t):
    return re.sub(r"[\W_]+", "", (t or "").lower())


def search_media(q, mode):
    sites = MUSIC_SITES if mode == "a" else FILM_SITES
    jobs = [(s["name"], (lambda s=s: search_site(s, q, mode))) for s in sites]
    jobs.append(("Telegram", lambda: search_telegram(q, mode)))
    jobs.append(("YouTube", lambda: search_ydl("ytsearch", "YouTube", q, mode, 5 if mode == "a" else 6)))
    if mode == "a":
        jobs.append(("SoundCloud", lambda: search_ydl("scsearch", "SoundCloud", q, mode, 3)))
    ex = ThreadPoolExecutor(max_workers=len(jobs))
    futs = {ex.submit(fn): name for name, fn in jobs}
    got = {}
    try:
        for f in as_completed(futs, timeout=SEARCH_TIMEOUT):
            try:
                got[futs[f]] = f.result()
            except Exception as e:
                log(f"[search:{futs[f]}] {str(e)[:160]}")
    except Exception:
        pass
    ex.shutdown(wait=False)

    # سایت‌ها اول (دورانی تا همه‌ی سایت‌ها نماینده داشته باشند)، بعد تلگرام، یوتیوب، ساندکلاود
    site_res = []
    for rnd in range(2):
        for s in sites:
            lst = got.get(s["name"], [])
            if len(lst) > rnd:
                site_res.append(lst[rnd])
    site_res = site_res[:SITE_QUOTA[mode]]
    # اگر یک عنوان در چند سایت بود، فقط شماره‌ی نسخه اضافه می‌کنیم (نام سایت نمایش داده نمی‌شود)
    seen = {}
    for r in site_res:
        k = norm_title(r["title"])
        seen[k] = seen.get(k, 0) + 1
        if seen[k] > 1:
            r["title"] = f"{r['title']} (نسخه {seen[k]})"
    return (site_res + got.get("Telegram", [])[:2] + got.get("YouTube", [])[:3]
            + got.get("SoundCloud", [])[:1])


# ───────────────────────── استخراج لینک دانلود از صفحه‌ی سایت‌ها ─────────────────────────
PREVIEW_RE = re.compile(r"preview|sample|demo|snippet|teaser|trailer|promo|intro", re.I)
RAW_URL_RE = re.compile(r"https?://[^\s\"'<>\\)]+?\.(?:mp4|mkv|avi|mp3|m4a)(?:\?[^\s\"'<>\\)]*)?", re.I)
SRC_TAG_RE = re.compile(r"WEB[-.\s]?DL|WEB[-.\s]?Rip|Blu[-.\s]?Ray|BRRip|HDRip|HDTV|DVDRip|HDCAM|\bCAM\b", re.I)


def quality_of(s, mode):
    pat = r"(?<!\d)(320|256|192|128|64)(?!\d)" if mode == "a" else r"(?<!\d)(2160|1080|720|480|360|240)(?!\d)"
    m = re.search(pat, s)
    return m.group(1) if m else None


def make_label(href, text, ctx, mode):
    """برچسب خوانا برای هر لینک: کیفیت، دوبله/زیرنویس، شماره‌ی قسمت و ..."""
    name = unquote(urlparse(href).path.rsplit("/", 1)[-1])
    s = f"{name} {text} {ctx}"
    q = quality_of(s, mode)
    if mode == "a":
        return f"{q} kbps" if q else "MP3"
    parts = []
    if q:
        parts.append(f"{q}p")
    m = SRC_TAG_RE.search(s)
    if m:
        key = re.sub(r"[-.\s]", "", m.group(0)).lower()
        parts.append({"webdl": "WEB-DL", "webrip": "WEBRip", "bluray": "BluRay", "brrip": "BRRip",
                      "hdrip": "HDRip", "hdtv": "HDTV", "dvdrip": "DVDRip", "hdcam": "HDCAM",
                      "cam": "CAM"}.get(key, key.upper()))
    if re.search(r"دوبله|dubbed|dubbing|farsi.?dub|[._-]dub[._-]", s, re.I):
        parts.append("دوبله")
    elif re.search(r"زیرنویس|softsub|hardsub|subbed|[._-]sub[._-]", s, re.I):
        parts.append("زیرنویس")
    se = re.search(r"S(\d{1,2})[\s._-]*E(\d{1,3})", s, re.I)
    if se:
        parts.append(f"S{int(se.group(1)):02d}E{int(se.group(2)):02d}")
    else:
        ep = re.search(r"(?:قسمت|ep(?:isode)?)[\s._-]*(\d{1,3})", s, re.I)
        if ep:
            parts.append(f"قسمت {int(ep.group(1))}")
    return " · ".join(parts) or "MP4"


def _sort_key(l, mode):
    q = -(int(l["q"]) if l["q"] else 0)
    if mode == "a":
        return (0, 0, q)
    m = re.search(r"S(\d+)E(\d+)", l["label"])
    if m:
        return (int(m.group(1)), int(m.group(2)), q)
    m = re.search(r"قسمت (\d+)", l["label"])
    return (0, int(m.group(1)) if m else 0, q)


def fetch_site_links(page_url, mode):
    ext = re.compile(r"\.(mp3|m4a)(\?|$)" if mode == "a" else r"\.(mp4|mkv|avi)(\?|$)", re.I)
    qtxt = r"320|128" if mode == "a" else r"2160|1080|720|480|360"
    r = HTTP.get(page_url, timeout=20)
    r.raise_for_status()
    raw = r.text.replace("\\/", "/")
    soup = BeautifulSoup(r.text, "html.parser")
    links, seen = [], set()

    def add(href, text, ctx):
        href = requote_uri(urljoin(page_url, href.strip()))
        if not href.startswith("http") or href in seen or PREVIEW_RE.search(unquote(href)):
            return
        seen.add(href)
        links.append({"q": quality_of(unquote(href) + " " + text + " " + ctx, mode),
                      "url": href, "label": make_label(href, text, ctx, mode)})

    for a in soup.find_all("a", href=True):
        href, text = a["href"], a.get_text(" ", strip=True)
        box = a.find_parent(["li", "tr", "p", "div"])
        ctx = ""
        if box is not None and len(box.find_all("a")) <= 3:
            ctx = box.get_text(" ", strip=True)[:160]
        if ext.search(href):
            add(href, text, ctx)
        elif re.search(qtxt, text) and re.search(r"دانلود|download|کیفیت", text, re.I):
            add(href, text, ctx)
    # آدرس‌هایی که فقط داخل کد/JSON صفحه هستند (سایت‌های جاوااسکریپتی)
    for m in RAW_URL_RE.finditer(raw):
        href = m.group(0)
        if ext.search(href):
            add(href, "", "")
    links.sort(key=lambda l: _sort_key(l, mode))
    return links[:60]


LINKS_CACHE = OrderedDict()
LC_LOCK = threading.Lock()


def get_links(url, mode):
    key = (url, mode)
    with LC_LOCK:
        ent = LINKS_CACHE.get(key)
        if ent and time.time() - ent[0] < 900:
            return ent[1]
    links = fetch_site_links(url, mode)
    with LC_LOCK:
        LINKS_CACHE[key] = (time.time(), links)
        while len(LINKS_CACHE) > 120:
            LINKS_CACHE.popitem(last=False)
    return links


def http_download(url, referer, workdir):
    path = os.path.join(workdir, "track.mp3")
    total = 0
    with HTTP.get(url, headers={"Referer": referer}, stream=True, timeout=30) as r:
        r.raise_for_status()
        if "text/html" in r.headers.get("Content-Type", ""):
            raise RuntimeError("پاسخ سایت فایل صوتی نبود")
        with open(path, "wb") as f:
            for chunk in r.iter_content(256 * 1024):
                total += len(chunk)
                if total > MAX_FILE_MB * 1024 * 1024:
                    raise NoFile("حجم فایل بیشتر از حد مجاز است")
                f.write(chunk)
    return path


# ───────────────────────── دانلود با yt-dlp ─────────────────────────
class NoFile(Exception):
    pass


def pick_file(workdir, vid):
    cands = [f for f in os.listdir(workdir)
             if f.startswith(f"{vid}.") and not f.endswith((".part", ".ytdl", ".json"))]
    for ext in (".mp3", ".m4a", ".mp4"):
        for f in cands:
            if f.endswith(ext):
                return os.path.join(workdir, f)
    return os.path.join(workdir, cands[0]) if cands else None


def download_media(url, workdir, mode, kind, youtube=True):
    last = None
    for clients in (YT_ATTEMPTS if youtube else [None]):
        name = ",".join(clients) if clients else "default"
        try:
            with make_ydl(build_opts(workdir, clients, True, mode, kind)) as ydl:
                info = ydl.extract_info(url, download=True)
            if info and info.get("entries"):
                info = next((e for e in info["entries"] if e), None)
            if not info:
                last = RuntimeError("اطلاعاتی دریافت نشد")
                continue
            path = pick_file(workdir, info.get("id"))
            if path:
                return path, info
            raise NoFile("فایل ساخته نشد (حجم زیاد)")  # اطلاعات آمد ولی فایل نه => بیشتر از سقف حجم
        except NoFile:
            raise
        except Exception as e:
            last = e
            log(f"[yt-dlp:{name}] {str(e)[:300]}")
    raise last or RuntimeError("دانلود ناموفق")


def fetch_media(res, workdir, mode, kind, title):
    errs = []
    youtube = res["src"] == "YouTube"
    try:
        return download_media(res["url"], workdir, mode, kind, youtube)
    except NoFile:
        raise
    except Exception as e:
        errs.append(f"{res['src']}: {str(e)[:200]}")
    if youtube and mode == "a" and title:  # فال‌بک: همان نام را در ساندکلاود پیدا کن
        try:
            return download_media(f"scsearch1:{title}", workdir, "a", kind, False)
        except Exception as e:
            errs.append(f"SoundCloud: {str(e)[:200]}")
    raise RuntimeError(" | ".join(errs))


# ───────────────────────── ربات ─────────────────────────
bot = telebot.TeleBot(TOKEN, parse_mode="HTML", threaded=True, num_threads=12)
DL_SEM = threading.Semaphore(MAX_PARALLEL_DL)
BTN = types.InlineKeyboardButton
USER_MODE = {}

REG = OrderedDict()   # فقط برای سرعت؛ نبودنش مشکلی ایجاد نمی‌کند (دوباره جستجو می‌شود)
REG_LOCK = threading.Lock()


def make_ref(res):
    """مرجع کوتاه (≤ ۴۵ بایت) برای callback_data."""
    if res["src"] == "YouTube":
        return "Y" + res["id"]
    if res["kind"] == "tg":
        return f"T{res['chat']}/{res['mid']}"
    h = "H" + hashlib.sha1(res["url"].encode()).hexdigest()[:10]
    with REG_LOCK:
        REG[h] = res
        while len(REG) > 800:
            REG.popitem(last=False)
    return h


def resolve_ref(ref, mode, q=None, title=""):
    t, body = ref[0], ref[1:]
    if t == "Y":
        return {"kind": "ydl", "src": "YouTube", "id": body, "title": title,
                "url": f"https://www.youtube.com/watch?v={body}"}
    if t == "T":
        chat, mid = body.rsplit("/", 1)
        return {"kind": "tg", "src": "Telegram", "chat": chat, "mid": int(mid), "title": title,
                "url": f"https://t.me/{chat}/{mid}", "size": 0}
    with REG_LOCK:
        res = REG.get(ref)
    if res:
        return res
    if q:  # دوباره جستجو و پیدا کردن همان نتیجه
        for r in search_media(q, mode):
            if make_ref(r) == ref:
                return r
    return None


QRE = re.compile(r"«(.+?)»")


def page_query(msg):
    first = ((msg.text or msg.caption or "").split("\n") or [""])[0]
    m = QRE.search(first)
    return m.group(1).strip() if m else None


def page_title(msg):
    lines = (msg.text or "").split("\n")
    return lines[1].strip() if len(lines) > 1 else ""


def clean_query(q):
    return re.sub(r"\s+", " ", re.sub(r"[«»<>]", " ", q or "")).strip()[:80]


def is_admin(m):
    return bool(ADMIN_ID) and str(m.from_user.id) == ADMIN_ID


def notify_admin(text):
    if ADMIN_ID:
        try:
            bot.send_message(int(ADMIN_ID), esc(text[:3500]), parse_mode=None)
        except Exception:
            pass


def safe_edit(text, chat_id, msg_id, kb=None):
    try:
        bot.edit_message_text(text, chat_id, msg_id, reply_markup=kb, disable_web_page_preview=True)
        return True
    except Exception as e:
        if "not modified" not in str(e):
            log("edit failed:", str(e)[:160])
        return False


def nav_row(back_cb=None):
    row = []
    if back_cb:
        row.append(BTN("🔙 بازگشت", callback_data=back_cb))
    row.append(BTN("🏠 شروع مجدد", callback_data="home"))
    return row


def nav_kb(back_cb=None):
    kb = types.InlineKeyboardMarkup()
    kb.row(*nav_row(back_cb))
    return kb


# ───── صفحه‌ها ─────
def home_text(name=""):
    hi = f"سلام {esc(name)} 👋\n" if name else ""
    return (f"{hi}به <b>ربات دانلود آهنگ، مداحی، فیلم و سریال</b> خوش آمدید.\n\n"
            "لطفاً بخش مورد نظر را انتخاب کنید 👇")


def home_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(BTN("🎵  آهنگ و مداحی", callback_data="m:a"),
           BTN("🎬  فیلم و سریال", callback_data="m:v"))
    return kb


def show_home(chat_id, msg_id=None, name=""):
    if msg_id and safe_edit(home_text(name), chat_id, msg_id, home_kb()):
        return
    bot.send_message(chat_id, home_text(name), reply_markup=home_kb())


def show_prompt(chat_id, msg_id, mode):
    if mode == "a":
        text = "🎵 <b>آهنگ و مداحی</b>\n\nنام خواننده، مداح یا آهنگ را بفرستید:"
    else:
        text = "🎬 <b>فیلم و سریال</b>\n\nنام فیلم یا سریال را بفرستید:"
    if not safe_edit(text, chat_id, msg_id, nav_kb("home")):
        bot.send_message(chat_id, text, reply_markup=nav_kb("home"))


def result_label(r, mode):
    icon = ICON.get(r["src"]) or ("🎵" if mode == "a" else "🎬")
    if r["kind"] == "site":
        suffix = f" · {r['src']}" if SHOW_SITE_NAMES else ""
    else:
        suffix = f" · {SRC_FA.get(r['src'], r['src'])}"
    return f"{icon} {trunc(r['title'], 44)}{suffix}"


def do_search(chat_id, msg_id, q, mode):
    safe_edit(WAIT_TEXT[mode], chat_id, msg_id, nav_kb(f"m:{mode}"))
    try:
        results = search_media(q, mode)
    except Exception as e:
        log("search error:", e)
        results = []
    if not results:
        safe_edit(f"🔎 «{esc(q)}»\nنتیجه‌ای پیدا نشد. نام دیگری را بفرستید.",
                  chat_id, msg_id, nav_kb(f"m:{mode}"))
        return
    kb = types.InlineKeyboardMarkup()
    for r in results:
        kb.add(BTN(result_label(r, mode), callback_data=f"i:{mode}:{make_ref(r)}"))
    kb.row(*nav_row(f"m:{mode}"))
    safe_edit(f"🔎 «{esc(q)}»\nنتیجه‌ی مورد نظر را انتخاب کنید:", chat_id, msg_id, kb)


def show_site_links(chat_id, msg_id, mode, ref, res, head, page):
    back = f"r:{mode}"
    try:
        links = get_links(res["url"], mode)
    except Exception as e:
        log(f"[site:{res['src']}] {str(e)[:160]}")
        links = []
    if not links:
        safe_edit(head + "\n\n❌ لینک مستقیمی پیدا نشد.", chat_id, msg_id, nav_kb(back))
        return
    pages = (len(links) + LINKS_PER_PAGE - 1) // LINKS_PER_PAGE
    page = max(0, min(page, pages - 1))
    kb = types.InlineKeyboardMarkup()
    lo, hi = page * LINKS_PER_PAGE, min(len(links), (page + 1) * LINKS_PER_PAGE)
    for i in range(lo, hi):
        row = [BTN(f"⬇️ {trunc(links[i]['label'], 40)}", url=wrap_url(links[i]["url"]))]
        if mode == "a":
            row.append(BTN("📤 ارسال در چت", callback_data=f"x:{mode}:{ref}:{i}"))
        kb.row(*row)
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(BTN("◀️ قبلی", callback_data=f"p:{mode}:{page - 1}:{ref}"))
        nav.append(BTN(f"{page + 1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(BTN("بعدی ▶️", callback_data=f"p:{mode}:{page + 1}:{ref}"))
        kb.row(*nav)
    kb.row(*nav_row(back))
    text = head + (f"\n\n{len(links)} لینک دانلود" if pages > 1 else "")
    if not safe_edit(text, chat_id, msg_id, kb):  # اگر تلگرام دکمه‌ها را نپذیرفت، متنی بفرست
        txt = head + "\n\n" + "\n".join(
            f"{esc(links[i]['label'])}: {esc(wrap_url(links[i]['url']))}" for i in range(lo, hi))
        bot.send_message(chat_id, txt, disable_web_page_preview=True)


def show_item(chat_id, msg_id, mode, ref, q, hint_title, page=0):
    res = resolve_ref(ref, mode, q, hint_title)
    back = f"r:{mode}"
    if not res:
        safe_edit(f"🔎 «{esc(q or '')}»\nاین گزینه دیگر در دسترس نیست؛ به نتایج برگردید.",
                  chat_id, msg_id, nav_kb(back))
        return
    title = res.get("title") or hint_title or "—"
    head = f"🔎 «{esc(q or '')}»\n{esc(trunc(title, 150))}"
    if res["kind"] != "site":
        head += f"\nمنبع: {esc(SRC_FA.get(res['src'], res['src']))}"
    elif SHOW_SITE_NAMES:
        head += f"\nمنبع: {esc(res['src'])}"

    if res["kind"] == "site":
        if page == 0:
            safe_edit(WAIT_TEXT[mode], chat_id, msg_id, nav_kb(back))
        show_site_links(chat_id, msg_id, mode, ref, res, head, page)
        return

    kb = types.InlineKeyboardMarkup()
    if res["kind"] == "tg":
        size = res.get("size") or 0
        if USERBOT.ok and not size:
            try:
                meta = USERBOT.meta(res["chat"], res["mid"])
                size = (meta or {}).get("size", 0)
            except Exception:
                pass
        if size:
            head += f"\nحجم: {size / 1048576:.1f} MB"
        kb.add(BTN("🔗 باز کردن در تلگرام", url=res["url"]))
        if USERBOT.ok and 0 < size <= MAX_FILE_MB * 1048576:
            kb.add(BTN("📥 ارسال در چت", callback_data=f"d:{mode}:tg:{ref}"))
        elif USERBOT.ok and size:
            head += "\n(برای فایل‌های بالای ۵۰ مگابایت از لینک تلگرام استفاده کنید)"
    else:  # یوتیوب / ساندکلاود
        if mode == "a":
            kb.row(BTN("🎧 کیفیت بالا", callback_data=f"d:a:hq:{ref}"),
                   BTN("📥 کم‌حجم (128)", callback_data=f"d:a:lo:{ref}"))
        else:
            kb.row(BTN("🎬 کیفیت 360", callback_data=f"d:v:360:{ref}"),
                   BTN("🎬 کیفیت 720", callback_data=f"d:v:720:{ref}"))
        kb.add(BTN("🔗 باز کردن لینک منبع", url=res["url"]))
    kb.row(*nav_row(back))
    safe_edit(head, chat_id, msg_id, kb)


def send_file(chat_id, path, title, performer=None, kb=None):
    if os.path.getsize(path) > 49 * 1024 * 1024:
        raise NoFile("حجم فایل از سقف تلگرام بیشتر است")
    low = path.lower()
    with open(path, "rb") as f:
        if low.endswith((".mp3", ".m4a")):
            bot.send_audio(chat_id, f, title=(title or "")[:64],
                           performer=(performer or "")[:64] or None, reply_markup=kb, timeout=300)
        elif low.endswith((".mp4", ".mov")):
            bot.send_video(chat_id, f, caption=esc(trunc(title, 200)), supports_streaming=True,
                           reply_markup=kb, timeout=600)
        else:
            bot.send_document(chat_id, f, caption=esc(trunc(title, 200)), reply_markup=kb, timeout=600)


def after_file_kb(mode):
    kb = types.InlineKeyboardMarkup()
    kb.row(BTN("🔄 جستجوی دیگر", callback_data=f"m:{mode}"), BTN("🏠 شروع مجدد", callback_data="home"))
    return kb


def fail_kb(mode, url=None):
    kb = types.InlineKeyboardMarkup()
    if url:
        kb.add(BTN("🔗 باز کردن لینک منبع", url=url))
    kb.row(*nav_row(f"m:{mode}"))
    return kb


def public_src_url(res):
    """آدرس منبع فقط برای یوتیوب/ساندکلاود/تلگرام نمایش داده می‌شود؛ هرگز برای سایت‌ها."""
    return None if (not res or res.get("kind") == "site") else res.get("url")


def start_download(chat_id, mode, kind, ref, q, title):
    res = resolve_ref(ref, mode, q, title)
    status = bot.send_message(chat_id, "⏳ در حال آماده‌سازی و ارسال فایل ...", reply_markup=nav_kb(f"m:{mode}"))
    workdir = tempfile.mkdtemp(prefix="bot_")
    try:
        if not res:
            raise RuntimeError("نتیجه پیدا نشد")
        performer = None
        with DL_SEM:
            if kind == "tg":
                if not USERBOT.ok:
                    raise RuntimeError("یوزربات فعال نیست")
                meta = USERBOT.meta(res["chat"], res["mid"]) or {}
                if meta.get("size", 0) > MAX_FILE_MB * 1048576:
                    raise NoFile("حجم فایل بیشتر از حد مجاز است")
                path = USERBOT.download(res["chat"], res["mid"], workdir)
                ftitle = title or meta.get("name") or "فایل"
            else:
                path, info = fetch_media(res, workdir, mode, kind, title)
                ftitle = info.get("title") or title or "فایل"
                performer = info.get("uploader") or info.get("channel")
        send_file(chat_id, path, ftitle, performer, after_file_kb(mode))
        try:
            bot.delete_message(chat_id, status.message_id)
        except Exception:
            pass
    except NoFile:
        safe_edit("❌ حجم این فایل بیشتر از سقف ۵۰ مگابایتی تلگرام است یا ساخته نشد.\n"
                  "از دکمه‌ی «باز کردن لینک» استفاده کنید.",
                  chat_id, status.message_id, fail_kb(mode, public_src_url(res)))
    except Exception as e:
        log(f"[download] {str(e)[:300]}")
        notify_admin(f"⚠️ دانلود ناموفق\n{(res or {}).get('src')} | {title[:60]}\n{str(e)[:600]}")
        safe_edit("❌ دریافت مستقیم این فایل ممکن نشد. گزینه‌ی دیگری را امتحان کنید "
                  "یا از لینک منبع استفاده کنید.",
                  chat_id, status.message_id, fail_kb(mode, public_src_url(res)))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def send_site_link(chat_id, mode, ref, idx, q, title):
    res = resolve_ref(ref, mode, q, title)
    status = bot.send_message(chat_id, "⏳ در حال دریافت فایل ...", reply_markup=nav_kb(f"m:{mode}"))
    workdir = tempfile.mkdtemp(prefix="bot_")
    try:
        links = get_links(res["url"], mode)
        with DL_SEM:
            path = http_download(links[idx]["url"], res["url"], workdir)
        send_file(chat_id, path, title or res["title"], None, after_file_kb(mode))
        try:
            bot.delete_message(chat_id, status.message_id)
        except Exception:
            pass
    except Exception as e:
        log(f"[send-link] {str(e)[:200]}")
        safe_edit("❌ ارسال در چت نشد؛ از دکمه‌ی دانلود مستقیم استفاده کنید.",
                  chat_id, status.message_id, fail_kb(mode))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ───── هندلرها ─────
@bot.message_handler(commands=["start", "help"])
def on_start(m):
    USER_MODE.pop(m.from_user.id, None)
    show_home(m.chat.id, None, m.from_user.first_name or "")


@bot.message_handler(commands=["status"])
def on_status(m):
    if not is_admin(m):
        return
    try:
        from yt_dlp.version import __version__ as v
    except Exception:
        v = "?"
    bot.reply_to(m, esc(
        f"yt-dlp: {v}\nffmpeg: {'✅' if FFMPEG_DIR else '❌'}\n"
        f"deno: {shutil.which('deno') or '❌'}\nnode: {shutil.which('node') or '❌'}\n"
        f"cookies: {'✅' if os.path.exists(COOKIE_SRC) else '—'}\nproxy: {'✅' if PROXY_URL else '—'}\n"
        f"PO-token server: {'✅' if POT_BASE_URL else '—'}\n"
        f"آدرس عمومی (/go): {PUBLIC_URL or '❌ تنظیم نشده'}\n"
        f"جستجوی سراسری تلگرام: {'✅' if USERBOT.ok else '—'}\nکانال‌های TG_CHANNELS: {len(TG_CHANNELS)}\n"
        f"سایت‌های موزیک: {len(MUSIC_SITES)} | سایت‌های فیلم: {len(FILM_SITES)}"),
        parse_mode=None)


def probe_site(site, mode, q):
    name = site["name"]
    try:
        res = search_site(site, q, mode)
    except Exception as e:
        err = re.sub(r"\s+", " ", str(e))[:90]
        return f"❌ {name}: {err}"
    if not res:
        return f"⚠️ {name}: جستجو نتیجه‌ای نداشت"
    try:
        links = fetch_site_links(res[0]["url"], mode)
    except Exception as e:
        return f"⚠️ {name}: {len(res)} نتیجه، خطا در لینک‌ها ({str(e)[:50]})"
    return f"✅ {name}: {len(res)} نتیجه، {len(links)} لینک" if links else f"⚠️ {name}: {len(res)} نتیجه، ۰ لینک"


@bot.message_handler(commands=["sites"])
def on_sites(m):
    """/sites film avatar   یا   /sites music ebi — تست سلامت همه‌ی سایت‌ها (فقط ادمین)"""
    if not is_admin(m):
        return
    parts = (m.text or "").split(maxsplit=2)
    mode = "a" if len(parts) > 1 and parts[1].lower().startswith("m") else "v"
    q = parts[2] if len(parts) > 2 else ("ebi" if mode == "a" else "avatar")
    msg = bot.reply_to(m, f"⏳ تست سایت‌ها ({q}) ...", parse_mode=None)
    sites = MUSIC_SITES if mode == "a" else FILM_SITES
    ex = ThreadPoolExecutor(max_workers=len(sites))
    futs = [ex.submit(probe_site, s, mode, q) for s in sites]
    lines = []
    for f in futs:
        try:
            lines.append(f.result(timeout=45))
        except Exception:
            lines.append("⚠️ timeout")
    ex.shutdown(wait=False)
    try:
        bot.edit_message_text(esc("\n".join(lines)), m.chat.id, msg.message_id, parse_mode=None)
    except Exception:
        pass


@bot.message_handler(commands=["probe"])
def on_probe(m):
    """کدام کلاینت یوتیوب از روی این سرور جواب می‌دهد؟"""
    if not is_admin(m):
        return
    msg = bot.reply_to(m, "⏳ در حال تست یوتیوب ...", parse_mode=None)
    workdir = tempfile.mkdtemp(prefix="probe_")
    lines = []
    try:
        for clients in YT_ATTEMPTS:
            name = ",".join(clients) if clients else "default"
            try:
                with make_ydl(build_opts(workdir, clients, download=False)) as ydl:
                    ydl.extract_info(PROBE_URL, download=False)
                lines.append(f"✅ {name}")
            except Exception as e:
                err = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")
                lines.append(f"❌ {name}: {err[:110]}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    try:
        bot.edit_message_text(esc("\n".join(lines)), m.chat.id, msg.message_id, parse_mode=None)
    except Exception:
        pass


@bot.message_handler(content_types=["text"])
def on_text(m):
    q = clean_query(m.text)
    if not q or (m.text or "").startswith("/"):
        return
    mode = USER_MODE.get(m.from_user.id)
    if not mode:  # بعد از ری‌استارت؛ بپرس در کدام بخش
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(BTN("🎵  آهنگ و مداحی", callback_data="r:a"), BTN("🎬  فیلم و سریال", callback_data="r:v"))
        kb.row(*nav_row())
        bot.reply_to(m, f"🔎 «{esc(q)}»\nدر کدام بخش جستجو شود؟", reply_markup=kb)
        return
    wait = bot.reply_to(m, WAIT_TEXT[mode], reply_markup=nav_kb(f"m:{mode}"))
    do_search(m.chat.id, wait.message_id, q, mode)


@bot.callback_query_handler(func=lambda c: True)
def on_callback(c):
    try:
        bot.answer_callback_query(c.id)
    except Exception:
        pass
    try:
        p = (c.data or "").split(":")
        act = p[0]
        if act == "noop":
            return
        chat_id, msg_id = c.message.chat.id, c.message.message_id
        uid = c.from_user.id
        if act == "home":
            USER_MODE.pop(uid, None)
            show_home(chat_id, msg_id, c.from_user.first_name or "")
        elif act == "m":
            USER_MODE[uid] = p[1]
            show_prompt(chat_id, msg_id, p[1])
        elif act == "r":  # جستجوی دوباره‌ی عبارتِ نوشته‌شده در متن پیام
            mode = p[1]
            USER_MODE[uid] = mode
            q = page_query(c.message)
            if q:
                do_search(chat_id, msg_id, q, mode)
            else:
                show_prompt(chat_id, msg_id, mode)
        elif act == "i":  # i:{mode}:{ref}
            show_item(chat_id, msg_id, p[1], ":".join(p[2:]), page_query(c.message), page_title_from_button(c))
        elif act == "p":  # p:{mode}:{page}:{ref}  (صفحه‌ی بعد/قبل لینک‌ها)
            show_item(chat_id, msg_id, p[1], ":".join(p[3:]), page_query(c.message),
                      page_title(c.message), page=int(p[2]))
        elif act == "d":  # d:{mode}:{kind}:{ref}
            start_download(chat_id, p[1], p[2], ":".join(p[3:]),
                           page_query(c.message), page_title(c.message))
        elif act == "x":  # x:{mode}:{ref}:{idx}
            send_site_link(chat_id, p[1], p[2], int(p[3]), page_query(c.message), page_title(c.message))
    except Exception as e:
        log("callback error:", repr(e)[:300])


def page_title_from_button(c):
    """عنوان آیتم را از متن خود دکمه‌ای که کاربر زده برمی‌گردانیم (بدون نیاز به حافظه)."""
    try:
        for row in c.message.reply_markup.keyboard:
            for b in row:
                if b.callback_data == c.data:
                    t = re.sub(r"^\S+\s", "", b.text)          # حذف آیکون
                    return re.sub(r"\s·\s[^·]+$", "", t).strip()  # حذف نام منبع
    except Exception:
        pass
    return ""


# ───────────────────────── آپدیت خودکار yt-dlp ─────────────────────────
def auto_update_ytdlp():
    """یوتیوب مدام تغییر می‌کند؛ هر ۱۲ ساعت yt-dlp را آپدیت و در صورت تغییر ربات را ری‌استارت می‌کنیم."""
    time.sleep(120)
    while True:
        try:
            out = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]"],
                                 capture_output=True, text=True, timeout=600)
            if "Successfully installed" in (out.stdout or ""):
                log("yt-dlp آپدیت شد؛ ری‌استارت ...")
                time.sleep(2)
                os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            log("auto-update failed:", e)
        time.sleep(12 * 3600)


if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    threading.Thread(target=ensure_js_runtime, daemon=True).start()
    threading.Thread(target=USERBOT.start, daemon=True).start()
    threading.Thread(target=auto_update_ytdlp, daemon=True).start()
    log("🤖 Bot is running ...")
    try:
        bot.remove_webhook()
        time.sleep(1)
    except Exception:
        pass
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
        except Exception as e:
            log("Polling error:", e)
            time.sleep(5)
