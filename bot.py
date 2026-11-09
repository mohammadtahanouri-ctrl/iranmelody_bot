import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import telebot
import yt_dlp

# --- وب‌سرور سبک برای رندر (Render Heartbeat) ---
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

threading.Thread(target=run_web_server, daemon=True).start()
# -----------------------------------------------

TOKEN = '9102489747:AAHVNVKCpSX_FJj596oWrT0uaobwRzM6pwo'
bot = telebot.TeleBot(TOKEN)

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, 'سلام! نام خواننده یا آهنگ مورد نظرتان را بفرستید تا آن را جستجو کرده و فایل MP3 اش را برایتان ارسال کنم.')

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    query = message.text
    processing_msg = bot.reply_to(message, f'در حال جستجو و دریافت آهنگ: {query} ⏳')
    
    # تنظیمات پیشرفته yt-dlp برای دور زدن محدودیت‌ها و جستجوی پایداتر
    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192'
        }],
        'outtmpl': 'downloads/%(id)s.%(ext)s',
        'default_search': 'ytsearch1',
        'quiet': True,
        'extractor_args': {'youtube': {'player_client': ['android', 'ios', 'web']}},
        'geo_bypass': True,
        'socket_timeout': 30
    }
    
    try:
        os.makedirs('downloads', exist_ok=True)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=True)
            if 'entries' in info:
                info = info['entries'][0]
            filename = ydl.prepare_filename(info)
            mp3_file = os.path.splitext(filename)[0] + '.mp3'
            
        with open(mp3_file, 'rb') as audio:
            bot.send_audio(
                message.chat.id, 
                audio, 
                title=info.get('title', query), 
                performer=info.get('uploader', 'Music Bot')
            )
            
        bot.delete_message(message.chat.id, processing_msg.message_id)
        
        if os.path.exists(mp3_file):
            os.remove(mp3_file)
            
    except Exception as e:
        print(f"Download Error: {e}")
        bot.edit_message_text(
            'متأسفانه آهنگ مورد نظر پیدا نشد یا در دریافت آن خطایی رخ داد. لطفاً نام دیگری را امتحان کنید.', 
            message.chat.id, 
            processing_msg.message_id
        )

if __name__ == '__main__':
    print('🤖 Bot and Web Server are running...')
    try:
        bot.remove_webhook()
    except:
        pass
    bot.infinity_polling(skip_pending=True)
