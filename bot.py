# -*- coding: utf-8 -*-
"""
iranmelody_bot — جستجوی آهنگ و مداحی با چند منبع و فال‌بک خودکار

منابع:
  1) سایت‌های موزیک فارسی (لینک مستقیم 128/320)  -> SITES
  2) یوتیوب   (yt-dlp + چند کلاینت + کوکی/پروکسی اختیاری)
  3) ساندکلاود (yt-dlp)  و فال‌بک خودکار وقتی یوتیوب بلاک بود

متغیرهای محیطی (در Render > Environment):
  BOT_TOKEN   (الزامی)  توکن ربات از BotFather
  ADMIN_ID    (اختیاری) آیدی عددی خودت؛ خطاها برایت فرستاده می‌شود + دستورهای /status و /probe
  YT_COOKIES  (اختیاری) متن کامل cookies.txt (فرمت Netscape) از یک اکانت گوگل جدا
  PROXY_URL   (اختیاری) مثل http://user:pass@host:port  برای عوض کردن IP یوتیوب
"""
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
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not TOKEN:
    sys.exit("BOT_TOKEN تنظیم نشده است. آن را در Environment سرویس Render اضافه کنید.")

ADMIN_ID = os.environ.get("ADMIN_ID", "").strip()
PROXY_URL = os.environ.get("PROXY_URL", "").strip() or None
YT_COOKIES = os.environ.get("YT_COOKIES", "").strip()

MAX_DURATION = 25 * 60        # ثانیه؛ ویدیوهای بلندتر در نتایج یوتیوب نمایش داده نمی‌شوند
MAX_FILE_MB = 48              # سقف آپلود ربات تلگرام ~50MB
MAX_PARALLEL_DL = 2           # دانلود هم‌زمان (برای RAM کم Render)
SEARCH_TIMEOUT = 25
MAX_RESULTS = 9

# سایت‌های وردپرسی موزیک فارسی (جستجو با ?s=). سایت جدید؟ یک خط اضافه کن.
SITES = [
    {"name": "UpMusics", "base": "https://upmusics.com"},
    {"name": "MusicDel", "base": "https://musicdel.ir"},
    {"name": "Musics-Fa", "base": "https://musics-fa.com"},
]

# ترتیب تلاش برای یوتیوب: اول پیش‌فرض خود yt-dlp (بهترین انتخاب سازنده‌ها)، بعد کلاینت‌های جایگزین
YT_ATTEMPTS = [None, ["tv"], ["web_embedded"], ["mweb"], ["android_vr"], ["ios"]]
PROBE_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"

UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36")
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


# ───────────────────────── ffmpeg / JS runtime / کوکی ─────────────────────────
def setup_ffmpeg():
    """مسیر پوشه‌ای که داخلش فایل با نام دقیق ffmpeg باشد (imageio-ffmpeg باینری را همراه دارد)."""
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
    """yt-dlp ممکن است فایل کوکی را بازنویسی کند؛ برای هر تلاش یک کپی می‌دهیم."""
    if not os.path.exists(COOKIE_SRC):
        return None
    dst = os.path.join(workdir, "cookies.txt")
    shutil.copy(COOKIE_SRC, dst)
    return dst


def make_ydl(opts):
    """نسخه‌های جدید yt-dlp برای یوتیوب به JS runtime (deno/node) نیاز دارند؛ اگر نصب بود فعالش می‌کنیم."""
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


def build_opts(workdir, clients=None, download=True):
    o = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
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
        o["format"] = "bestaudio/best" if FFMPEG_DIR else "bestaudio[ext=m4a]/bestaudio"
        o["outtmpl"] = os.path.join(workdir, "%(id)s.%(ext)s")
        o["max_filesize"] = MAX_FILE_MB * 1024 * 1024
        if FFMPEG_DIR:
            o["ffmpeg_location"] = FFMPEG_DIR
            o["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                    "preferredcodec": "mp3",
                                    "preferredquality": "192"}]
    return o


# ───────────────────────── جستجو ─────────────────────────
def clean_title(t):
    t = re.sub(r"\s+", " ", t or "").strip()
    return re.sub(r"^دانلود\s+", "", t)


SKIP_PATH = ("/category/", "/tag/", "/page/", "/author/", "/feed", "/wp-", "/tags", "/cat/")


def search_site(site, q):
    base = site["base"]
    r = HTTP.get(base + "/", params={"s": q}, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    host = urlparse(base).netloc.replace("www.", "")
    out, seen = [], set()
    for a in soup.select("article a[href], h2 a[href], h3 a[href]"):
        href = urljoin(base, a["href"]).split("#")[0]
        p = urlparse(href)
        if p.netloc.replace("www.", "") != host:
            continue
        if len(p.path) < 8 or any(x in p.path for x in SKIP_PATH):
            continue
        title = clean_title(a.get_text(" ", strip=True) or a.get("title", ""))
        if len(title) < 5 or href in seen:
            continue
        seen.add(href)
        out.append({"kind": "site", "src": site["name"], "title": title, "url": href})
        if len(out) >= 3:
            break
    return out


def search_ydl(prefix, label, q, n=4):
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
            "skip_download": True, "socket_timeout": 15}
    if PROXY_URL:
        opts["proxy"] = PROXY_URL
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
    return out[:3]


def search_all(q):
    jobs = [(s["name"], (lambda s=s: search_site(s, q))) for s in SITES]
    jobs.append(("YouTube", lambda: search_ydl("ytsearch", "YouTube", q)))
    jobs.append(("SoundCloud", lambda: search_ydl("scsearch", "SoundCloud", q)))
    ex = ThreadPoolExecutor(max_workers=len(jobs))
    futs = {ex.submit(fn): name for name, fn in jobs}
    got = {}
    try:
        for f in as_completed(futs, timeout=SEARCH_TIMEOUT):
            try:
                got[futs[f]] = f.result()
            except Exception as e:
                log(f"[search:{futs[f]}] {e}")
    except Exception as e:
        log("search timeout:", e)
    ex.shutdown(wait=False)
    order = [s["name"] for s in SITES] + ["YouTube", "SoundCloud"]
    merged = []
    for rnd in range(3):  # round-robin تا همه منابع نماینده داشته باشند
        for name in order:
            lst = got.get(name, [])
            if len(lst) > rnd and len(merged) < MAX_RESULTS:
                merged.append(lst[rnd])
    return merged


# ───────────────────────── لینک‌های سایت‌های فارسی ─────────────────────────
MP3_RE = re.compile(r"\.mp3(\?|$)", re.I)
PREVIEW_RE = re.compile(r"preview|sample|demo|snippet|teaser", re.I)


def quality_of(s):
    m = re.search(r"(?<!\d)(320|256|192|128|64)(?!\d)", s)
    return m.group(1) if m else None


def fetch_site_links(page_url):
    r = HTTP.get(page_url, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    links, seen = [], set()

    def add(href, text):
        href = requote_uri(urljoin(page_url, href.strip()))
        if not href.startswith("http") or href in seen or PREVIEW_RE.search(href):
            return
        seen.add(href)
        links.append({"q": quality_of(href + " " + text), "url": href})

    for a in soup.find_all("a", href=True):
        href, text = a["href"], a.get_text(" ", strip=True)
        if MP3_RE.search(href):
            add(href, text)
        elif re.search(r"320|128", text) and re.search(r"دانلود|download|کیفیت", text, re.I):
            add(href, text)
    links.sort(key=lambda l: -(int(l["q"]) if l["q"] else 0))
    return links[:6]


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
                    raise RuntimeError("حجم فایل بیشتر از حد مجاز تلگرام است")
                f.write(chunk)
    return path


# ───────────────────────── دانلود با yt-dlp (یوتیوب/ساندکلاود) ─────────────────────────
def pick_file(workdir, vid):
    cands = [f for f in os.listdir(workdir)
             if f.startswith(f"{vid}.") and not f.endswith((".part", ".ytdl", ".json"))]
    for ext in (".mp3", ".m4a"):
        for f in cands:
            if f.endswith(ext):
                return os.path.join(workdir, f)
    return os.path.join(workdir, cands[0]) if cands else None


def download_audio(url, workdir, youtube=True):
    last = None
    for clients in (YT_ATTEMPTS if youtube else [None]):
        name = ",".join(clients) if clients else "default"
        try:
            with make_ydl(build_opts(workdir, clients)) as ydl:
                info = ydl.extract_info(url, download=True)
            if info and info.get("entries"):
                info = next((e for e in info["entries"] if e), None)
            path = pick_file(workdir, info.get("id")) if info else None
            if path:
                return path, info
            last = RuntimeError("فایلی ساخته نشد (حجم زیاد یا فرمت نامناسب)")
        except Exception as e:
            last = e
            log(f"[yt-dlp:{name}] {str(e)[:300]}")
    raise last


def fetch_audio(res, workdir):
    errs = []
    try:
        return download_audio(res["url"], workdir, youtube=(res["src"] == "YouTube"))
    except Exception as e:
        errs.append(f"{res['src']}: {str(e)[:200]}")
    if res["src"] == "YouTube":  # فال‌بک: همان نام را در ساندکلاود پیدا کن
        try:
            return download_audio(f"scsearch1:{res['title']}", workdir, youtube=False)
        except Exception as e:
            errs.append(f"SoundCloud: {str(e)[:200]}")
    raise RuntimeError(" | ".join(errs))


# ───────────────────────── ربات ─────────────────────────
bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=8)
DL_SEM = threading.Semaphore(MAX_PARALLEL_DL)

CACHE = OrderedDict()
CACHE_LOCK = threading.Lock()


def cache_put(data):
    key = uuid.uuid4().hex[:8]
    with CACHE_LOCK:
        CACHE[key] = data
        while len(CACHE) > 400:
            CACHE.popitem(last=False)
    return key


def cache_get(key):
    with CACHE_LOCK:
        return CACHE.get(key)


def is_admin(m):
    return bool(ADMIN_ID) and str(m.from_user.id) == ADMIN_ID


def notify_admin(text):
    if ADMIN_ID:
        try:
            bot.send_message(int(ADMIN_ID), text[:3500])
        except Exception:
            pass


def safe_edit(text, chat_id, msg_id, kb=None):
    try:
        bot.edit_message_text(text, chat_id, msg_id, reply_markup=kb, disable_web_page_preview=True)
        return True
    except Exception as e:
        log("edit failed:", str(e)[:200])
        return False


ICON = {"YouTube": "▶️", "SoundCloud": "☁️"}


def results_keyboard(sid):
    kb = types.InlineKeyboardMarkup()
    for i, r in enumerate(cache_get(sid)["results"]):
        label = f"{ICON.get(r['src'], '🎵')} {r['title'][:48]} · {r['src']}"
        kb.add(types.InlineKeyboardButton(label, callback_data=f"r:{sid}:{i}"))
    return kb


def show_results(chat_id, msg_id, sid, header):
    safe_edit(header, chat_id, msg_id, results_keyboard(sid))


def send_file(chat_id, path, title, performer=None):
    if os.path.getsize(path) > 49 * 1024 * 1024:
        raise RuntimeError("حجم فایل از سقف تلگرام بیشتر است")
    with open(path, "rb") as f:
        if path.lower().endswith((".mp3", ".m4a")):
            bot.send_audio(chat_id, f, title=(title or "")[:64],
                           performer=(performer or "")[:64] or None, timeout=180)
        else:
            bot.send_document(chat_id, f, caption=(title or "")[:200], timeout=180)


WELCOME = ("سلام! 👋\nنام خواننده، آهنگ یا مداح را بفرستید تا از چند منبع جستجو کنم "
           "و لینک دانلود (128 / 320) یا خود فایل MP3 را برایتان بیاورم.")


@bot.message_handler(commands=["start", "help"])
def on_start(m):
    bot.reply_to(m, WELCOME)


@bot.message_handler(commands=["status"])
def on_status(m):
    if not is_admin(m):
        return
    try:
        from yt_dlp.version import __version__ as v
    except Exception:
        v = "?"
    bot.reply_to(m, (
        f"yt-dlp: {v}\nffmpeg: {'✅' if FFMPEG_DIR else '❌'}\n"
        f"node: {shutil.which('node') or '❌'}\ndeno: {shutil.which('deno') or '❌'}\n"
        f"cookies: {'✅' if os.path.exists(COOKIE_SRC) else '—'}\n"
        f"proxy: {'✅' if PROXY_URL else '—'}"))


@bot.message_handler(commands=["probe"])
def on_probe(m):
    """تست سریع: کدام کلاینت یوتیوب از روی سرور فعلی جواب می‌دهد؟"""
    if not is_admin(m):
        return
    msg = bot.reply_to(m, "⏳ در حال تست یوتیوب ...")
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
    safe_edit("\n".join(lines), m.chat.id, msg.message_id)


@bot.message_handler(content_types=["text"])
def on_text(m):
    q = (m.text or "").strip()
    if not q or q.startswith("/"):
        return
    if len(q) > 100:
        bot.reply_to(m, "متن جستجو خیلی طولانی است؛ فقط نام خواننده یا آهنگ را بفرستید.")
        return
    wait = bot.reply_to(m, f"🔎 در حال جستجوی «{q}» از چند منبع ...")
    results = search_all(q)
    if not results:
        safe_edit("نتیجه‌ای پیدا نشد 😕 نام را کوتاه‌تر یا با املای دیگری بفرستید.",
                  m.chat.id, wait.message_id)
        return
    sid = cache_put({"results": results, "q": q})
    show_results(m.chat.id, wait.message_id, sid, f"نتایج «{q}» — یکی را انتخاب کنید:")


def handle_site_pick(chat_id, msg_id, sid, res):
    safe_edit(f"⏳ در حال استخراج لینک‌های «{res['title'][:60]}» ...", chat_id, msg_id)
    try:
        links = fetch_site_links(res["url"])
    except Exception as e:
        log(f"[site:{res['src']}] {e}")
        links = []
    back = types.InlineKeyboardButton("🔙 بازگشت به نتایج", callback_data=f"b:{sid}")
    kb = types.InlineKeyboardMarkup()
    if not links:
        kb.add(back)
        safe_edit("❌ لینک مستقیمی پیدا نشد. گزینه‌ی دیگری را امتحان کنید.", chat_id, msg_id, kb)
        return
    lid = cache_put({"title": res["title"], "page": res["url"], "links": links})
    for i, l in enumerate(links):
        lab = f"{l['q']}kbps" if l["q"] else "MP3"
        kb.row(types.InlineKeyboardButton(f"⬇️ لینک {lab}", url=l["url"]),
               types.InlineKeyboardButton(f"📤 ارسال {lab} در چت", callback_data=f"s:{lid}:{i}"))
    kb.add(back)
    header = f"🎵 {res['title'][:120]}\nمنبع: {res['src']}"
    if not safe_edit(header, chat_id, msg_id, kb):  # اگر تلگرام URLها را نپذیرفت، متنی بفرست
        txt = header + "\n\n" + "\n".join(
            f"{(l['q'] or 'MP3')}: {l['url']}" for l in links)
        bot.send_message(chat_id, txt, disable_web_page_preview=True)


def handle_ydl_pick(chat_id, msg_id, sid, res):
    safe_edit(f"⏳ در حال آماده‌سازی «{res['title'][:60]}» ...", chat_id, msg_id)
    workdir = tempfile.mkdtemp(prefix="bot_")
    try:
        with DL_SEM:
            path, info = fetch_audio(res, workdir)
        send_file(chat_id, path, info.get("title") or res["title"],
                  info.get("uploader") or info.get("channel"))
        show_results(chat_id, msg_id, sid, "✅ ارسال شد. اگر خواستید گزینه‌ی دیگری را انتخاب کنید:")
    except Exception as e:
        log(f"[pick:{res['src']}] {e}")
        notify_admin(f"⚠️ دانلود ناموفق\n{res['src']} | {res['title'][:60]}\n{str(e)[:600]}")
        show_results(chat_id, msg_id, sid, "❌ این گزینه قابل دریافت نبود. گزینه‌ی دیگری را امتحان کنید:")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def handle_send(chat_id, lid, i):
    d = cache_get(lid)
    if not d:
        bot.send_message(chat_id, "این درخواست منقضی شده؛ دوباره جستجو کنید.")
        return
    link = d["links"][i]
    status = bot.send_message(chat_id, "⏳ در حال دریافت فایل ...")
    workdir = tempfile.mkdtemp(prefix="bot_")
    try:
        with DL_SEM:
            path = http_download(link["url"], d["page"], workdir)
        send_file(chat_id, path, d["title"])
        bot.delete_message(chat_id, status.message_id)
    except Exception as e:
        log(f"[send] {e}")
        safe_edit("❌ ارسال در چت نشد؛ از دکمه‌ی «لینک» برای دانلود مستقیم استفاده کنید.",
                  chat_id, status.message_id)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@bot.callback_query_handler(func=lambda c: bool(c.data))
def on_callback(c):
    try:
        kind, key, *rest = c.data.split(":")
        chat_id, msg_id = c.message.chat.id, c.message.message_id
        d = cache_get(key)
        if not d:
            bot.answer_callback_query(c.id, "نتایج منقضی شده؛ دوباره جستجو کنید.", show_alert=True)
            return
        bot.answer_callback_query(c.id)
        if kind == "b":
            show_results(chat_id, msg_id, key, f"نتایج «{d['q']}» — یکی را انتخاب کنید:")
        elif kind == "r":
            res = d["results"][int(rest[0])]
            (handle_site_pick if res["kind"] == "site" else handle_ydl_pick)(chat_id, msg_id, key, res)
        elif kind == "s":
            handle_send(chat_id, key, int(rest[0]))
    except Exception as e:
        log("callback error:", e)


# ───────────────────────── آپدیت خودکار yt-dlp ─────────────────────────
def auto_update_ytdlp():
    """یوتیوب مدام تغییر می‌کند؛ هر ۱۲ ساعت yt-dlp را آپدیت و در صورت تغییر ربات را ری‌استارت می‌کنیم."""
    time.sleep(90)
    while True:
        try:
            out = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]"],
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
    threading.Thread(target=auto_update_ytdlp, daemon=True).start()
    log("🤖 Bot and Web Server are running...")
    try:
        bot.remove_webhook()
    except Exception:
        pass
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
