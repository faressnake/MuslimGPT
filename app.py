# bot.py
import requests
import telebot
import time
import threading
import re
import uuid
import logging
import random
import os
from collections import deque
from datetime import datetime
from flask import Flask, request, abort

# ------------------- إعدادات أساسية -------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# جلب المتغيرات البيئية
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    logger.error("لم يتم العثور على TELEGRAM_BOT_TOKEN")
    exit(1)

ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, skip_pending=True)

API_URL = "https://themuslimgpt.com/api/chat/send"
BASE_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://themuslimgpt.com",
    "Referer": "https://themuslimgpt.com/chat",
    "x-requested-with": "com.themuslimgpt.app"
}

# ------------------- إدارة الذاكرة والسياق -------------------
user_contexts = {}
MAX_CONTEXT_LEN = 10

def get_user_context(user_id):
    if user_id not in user_contexts:
        user_contexts[user_id] = deque(maxlen=MAX_CONTEXT_LEN)
    return user_contexts[user_id]

def add_to_context(user_id, role, content):
    ctx = get_user_context(user_id)
    ctx.append((role, content))

def clear_context(user_id):
    if user_id in user_contexts:
        del user_contexts[user_id]

def build_context_prompt(user_id, new_message):
    ctx = get_user_context(user_id)
    history = ""
    for role, text in list(ctx)[-6:]:
        if role == 'user':
            history += f"المستخدم: {text}\n"
        else:
            history += f"البوت: {text}\n"
    full_prompt = f"{history}المستخدم: {new_message}\nالبوت:"
    if len(full_prompt) > 3800:
        full_prompt = full_prompt[-3800:]
    return full_prompt

# ------------------- SessionManager (نفسه بالكامل) -------------------
class SessionManager:
    def __init__(self):
        self.session_id = None
        self.lock = threading.Lock()
        self._init_session()

    def _init_session(self):
        try:
            resp = requests.get("https://themuslimgpt.com/api/auth/user", headers=BASE_HEADERS, timeout=10)
            self._extract_sid_from_response(resp)
        except:
            pass
        if not self.session_id:
            self.session_id = "7ffb6e3c-9a51-4d34-a233-78c769983397"
            logger.warning(f"استخدم fallback sid: {self.session_id}")

    def _extract_sid_from_response(self, response):
        if 'set-cookie' in response.headers:
            set_cookie = response.headers['set-cookie']
            match = re.search(r'mgpt_sid=([^;]+)', set_cookie)
            if match:
                new_sid = match.group(1)
                if new_sid != self.session_id:
                    self.session_id = new_sid
                    logger.info(f"تم تحديث sessionId من set-cookie: {self.session_id}")
                    return True
        if 'mgpt_sid' in response.cookies:
            new_sid = response.cookies['mgpt_sid']
            if new_sid != self.session_id:
                self.session_id = new_sid
                logger.info(f"تم تحديث sessionId من cookies: {self.session_id}")
                return True
        return False

    def get_headers(self):
        headers = BASE_HEADERS.copy()
        user_agents = [
            "Mozilla/5.0 (Linux; Android 10; M2006C3LG) AppleWebKit/537.36 Chrome/148.0.7778.120 Mobile Safari/537.36",
            "Mozilla/5.0 (Linux; Android 11; SM-G991B) AppleWebKit/537.36 Chrome/147.0.0.0 Mobile Safari/537.36",
            "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 Chrome/146.0.0.0 Mobile Safari/537.36"
        ]
        headers["User-Agent"] = random.choice(user_agents)
        if self.session_id:
            headers["Cookie"] = f"mgpt_sid={self.session_id}"
        return headers

    def refresh_session(self):
        with self.lock:
            try:
                resp = requests.get("https://themuslimgpt.com/", headers=BASE_HEADERS, timeout=10)
                if self._extract_sid_from_response(resp):
                    logger.info("تم تجديد الجلسة عبر GET /")
                    return True
            except:
                pass
            try:
                resp = requests.get("https://themuslimgpt.com/api/auth/user", headers=BASE_HEADERS, timeout=10)
                if self._extract_sid_from_response(resp):
                    logger.info("تم تجديد الجلسة عبر /api/auth/user")
                    return True
            except:
                pass
            new_sid = str(uuid.uuid4())
            self.session_id = new_sid
            logger.warning(f"تم توليد sessionId عشوائي: {new_sid}")
            return True

    def refresh_from_api_response(self, response):
        with self.lock:
            self._extract_sid_from_response(response)

    def is_session_valid(self):
        if not self.session_id:
            return False
        try:
            resp = requests.get("https://themuslimgpt.com/api/auth/user", headers=self.get_headers(), timeout=10)
            return resp.status_code in (200, 304)
        except:
            return False

# ------------------- SmartRateLimiter (نفسه) -------------------
class SmartRateLimiter:
    def __init__(self):
        self.limit = 15
        self.remaining = 15
        self.reset_time = 0
        self.lock = threading.Lock()

    def update_from_headers(self, headers):
        with self.lock:
            if 'ratelimit-limit' in headers:
                self.limit = int(headers['ratelimit-limit'])
            if 'ratelimit-remaining' in headers:
                self.remaining = int(headers['ratelimit-remaining'])
            if 'ratelimit-reset' in headers:
                self.reset_time = time.time() + int(headers['ratelimit-reset'])

    def wait_if_needed(self):
        with self.lock:
            if self.remaining <= 1 and self.reset_time > time.time():
                sleep_time = self.reset_time - time.time() + 1
                logger.warning(f"Rate limit, ننتظر {sleep_time:.1f} ثانية")
                time.sleep(sleep_time)
                self.remaining = self.limit
            else:
                self.remaining -= 1

rate_limiter = SmartRateLimiter()
session_mgr = SessionManager()

# ------------------- وظيفة الرد -------------------
def get_muslimgpt_response(user_id, user_message, retry=True, attempt=0):
    time.sleep(random.uniform(1, 3))
    rate_limiter.wait_if_needed()

    if not session_mgr.is_session_valid():
        logger.warning("الجلسة غير صالحة، نجددها...")
        if not session_mgr.refresh_session():
            return "❌ تعذر تجديد الجلسة. انتظر دقيقة ثم حاول مجدداً."

    full_message = build_context_prompt(user_id, user_message)
    payload = {
        "sessionId": session_mgr.session_id,
        "message": full_message,
        "isVoice": False,
        "timezone": "Africa/Algiers"
    }
    try:
        response = requests.post(API_URL, json=payload, headers=session_mgr.get_headers(), timeout=25)
        rate_limiter.update_from_headers(response.headers)
        session_mgr.refresh_from_api_response(response)

        if response.status_code == 200:
            data = response.json()
            reply = data.get("text") or data.get("content")
            if reply:
                add_to_context(user_id, "user", user_message)
                add_to_context(user_id, "assistant", reply)
                return reply
            else:
                return f"⚠️ تنسيق غير متوقع: {data}"
        elif response.status_code in (401, 403) and retry and attempt < 3:
            time.sleep(2 ** attempt)
            return get_muslimgpt_response(user_id, user_message, retry=False, attempt=attempt+1)
        elif response.status_code == 429:
            wait = int(response.headers.get('ratelimit-reset', 90)) + 2
            time.sleep(wait)
            return get_muslimgpt_response(user_id, user_message, retry, attempt+1)
        else:
            return f"❌ خطأ {response.status_code}"
    except requests.exceptions.Timeout:
        if retry and attempt < 2:
            wait_time = 5 + (attempt * 5)
            time.sleep(wait_time)
            return get_muslimgpt_response(user_id, user_message, retry=False, attempt=attempt+1)
        else:
            return ("⏰ الخادم لا يستجيب حالياً\nحاول بعد 30 ثانية.")
    except Exception as e:
        logger.error(f"خطأ: {e}")
        if retry and attempt < 2:
            time.sleep(3)
            return get_muslimgpt_response(user_id, user_message, retry=False, attempt=attempt+1)
        return f"❌ خطأ فني: {str(e)[:100]}"

# ------------------- دوال البوت (أزرار وأوامر) -------------------
def start_keyboard():
    kb = telebot.types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        telebot.types.InlineKeyboardButton("🗣️ بدء الدردشة", callback_data="start_chat"),
        telebot.types.InlineKeyboardButton("ℹ️ عن البوت", callback_data="about"),
        telebot.types.InlineKeyboardButton("🧹 مسح السياق", callback_data="clear")
    )
    return kb

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.send_message(message.chat.id,
        "🌟 *أهلاً بك في بوت MuslimGPT المتطور* 🌟\n
        "• /clear - مسح ذاكرة المحادثة\n"
        "• /stats - إحصائيات (للمطور)\n\n"
        "✍️ *طور بواسطة:* `By FaresCodeX`",
        parse_mode="Markdown", reply_markup=start_keyboard())

@bot.message_handler(commands=['clear'])
def cmd_clear(message):
    clear_context(message.from_user.id)
    bot.reply_to(message, "🧹 تم مسح السياق.")

@bot.message_handler(commands=['stats'])
def cmd_stats(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ للمطور فقط.")
        return
    total_users = len(user_contexts)
    bot.reply_to(message, f"📊 المستخدمين النشطين: `{total_users}`", parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    if call.data == "start_chat":
        bot.answer_callback_query(call.id, "✅ ابدأ بالكتابة")
        bot.send_message(call.message.chat.id, "🎤 أرسل رسالتك...", parse_mode="Markdown")
    elif call.data == "about":
        about_text = (
            "🤖 *عن البوت:*\n\n"
            "• تجديد الجلسة تلقائي.\n"
            "• إدارة ذكية للمعدل.\n\n"
            "👨‍💻 المطور: `FaresCodeX`"
        )
        bot.edit_message_text(about_text, call.message.chat.id, call.message.message_id,
                              parse_mode="Markdown", reply_markup=start_keyboard())
        bot.answer_callback_query(call.id)
    elif call.data == "clear":
        clear_context(call.from_user.id)
        bot.answer_callback_query(call.id, "تم مسح السياق")
        bot.edit_message_text("🧹 تم مسح السياق.", call.message.chat.id, call.message.message_id)

def escape_markdown(text):
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return ''.join('\\' + c if c in escape_chars else c for c in text)

def split_long_message(text, limit=4096):
    if len(text) <= limit:
        return [text]
    parts = []
    sentences = re.split(r'(?<=[.!?؟])\s+', text)
    current = ""
    for s in sentences:
        if len(current) + len(s) + 1 <= limit:
            current += (s + " ")
        else:
            if current:
                parts.append(current.strip())
            current = s + " "
    if current:
        parts.append(current.strip())
    return parts

@bot.message_handler(func=lambda message: True)
def handle_all_messages(message):
    if message.text and message.text.startswith('/'):
        return
    bot.send_chat_action(message.chat.id, "typing")
    reply = get_muslimgpt_response(message.from_user.id, message.text)
    try:
        safe_reply = escape_markdown(reply)
        for part in split_long_message(safe_reply):
            bot.reply_to(message, part, parse_mode="MarkdownV2")
    except Exception:
        for part in split_long_message(reply):
            bot.reply_to(message, part)

# ------------------- خادم Flask مع Webhook -------------------
app = Flask(__name__)
WEBHOOK_PATH = f"/{TELEGRAM_BOT_TOKEN}"  # مسار سري

@app.route(WEBHOOK_PATH, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_str = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
        return '', 200
    abort(403)

@app.route('/')
def index():
    return "Bot is running"

if __name__ == "__main__":
    logger.info("✅ تشغيل البوت بنظام Webhook...")
    # إزالة أي webhook قديم وتعيين الجديد
    bot.remove_webhook()
    time.sleep(1)
    # رابط الخدمة على Render (يتم ضبطه تلقائياً عبر متغير البيئة RENDER_EXTERNAL_HOSTNAME)
    hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    if not hostname:
        logger.error("RENDER_EXTERNAL_HOSTNAME غير موجود، تأكد من النشر على Render")
        exit(1)
    webhook_url = f"https://{hostname}{WEBHOOK_PATH}"
    bot.set_webhook(url=webhook_url)
    logger.info(f"Webhook set to {webhook_url}")
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
