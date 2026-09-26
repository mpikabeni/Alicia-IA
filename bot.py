
import os
import re
import json
import time
import random
import sqlite3
import asyncio
import logging
import tempfile
import shutil
import html as html_lib
import hashlib
import io
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse, quote
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from openai import OpenAI
import httpx

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = ImageDraw = ImageFont = None

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice,
    BotCommand, BotCommandScopeChat, BotCommandScopeAllPrivateChats, BotCommandScopeAllChatAdministrators, BotCommandScopeAllGroupChats,
)
from telegram.constants import ChatType
from telegram.error import RetryAfter
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ChatMemberHandler, PreCheckoutQueryHandler, PollAnswerHandler, filters,
)

load_dotenv()

# ============================================================
# CONFIGURATION
# ============================================================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "@im_a_aliciabot").strip()
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0") or 0)
NEXA_CHANNEL = os.getenv("NEXA_CHANNEL", "https://t.me/Nexa_CG").strip()

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "").strip()
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

MISTRAL_MODEL = os.getenv("MISTRAL_MODEL", "ministral-3-8b-latest").strip()
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()

PORT = int(os.getenv("PORT", "10000"))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()
DB_PATH = os.getenv("ALICIA_DB", "alicia_v3.db").strip()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ALICIA_IMAGE = os.path.join(BASE_DIR, "alicia.png")
DOWNLOAD_DIR = os.getenv("ALICIA_DOWNLOAD_DIR", os.path.join(BASE_DIR, "downloads"))
MAX_DOWNLOAD_MB = min(49, max(1, int(os.getenv("ALICIA_MAX_DOWNLOAD_MB", "49"))))
MAX_DOWNLOAD_BYTES = MAX_DOWNLOAD_MB * 1024 * 1024
CODING_MAX_TOKENS = int(os.getenv("CODING_MAX_TOKENS", "7000"))

# Optional TTS. If no TTS provider is configured, /voice explains how to enable it.
TTS_API_URL = os.getenv("TTS_API_URL", "").strip()
TTS_API_KEY = os.getenv("TTS_API_KEY", "").strip()
TTS_VOICE = os.getenv("TTS_VOICE", "young_female").strip()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | alicia | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("alicia")

# ============================================================
# SAFE TELEGRAM SENDING
# ============================================================
_SEND_LOCK = asyncio.Lock()
_LAST_SEND = 0.0

async def safe_send_message(bot, chat_id, text, **kwargs):
    global _LAST_SEND
    for attempt in range(4):
        try:
            async with _SEND_LOCK:
                gap = 0.35 - (time.monotonic() - _LAST_SEND)
                if gap > 0:
                    await asyncio.sleep(gap)
                result = await bot.send_message(chat_id=chat_id, text=text, **kwargs)
                _LAST_SEND = time.monotonic()
                return result
        except RetryAfter as e:
            await asyncio.sleep(max(1, int(getattr(e, "retry_after", 1)) + 1))
    return await bot.send_message(chat_id=chat_id, text=text, **kwargs)

async def safe_reply(message, text, **kwargs):
    return await safe_send_message(
        message.get_bot(),
        message.chat_id,
        text,
        reply_to_message_id=message.message_id,
        **kwargs,
    )

async def safe_chat_action(bot, chat_id, action="typing"):
    for _ in range(3):
        try:
            await bot.send_chat_action(chat_id=chat_id, action=action)
            return
        except RetryAfter as e:
            await asyncio.sleep(max(1, int(getattr(e, "retry_after", 1)) + 1))
        except Exception:
            return

# ============================================================
# DATABASE - NON DESTRUCTIVE
# Existing tables are preserved exactly. New tables are additive.
# ============================================================
def db():
    return sqlite3.connect(DB_PATH, timeout=30)

def now():
    return datetime.now(timezone.utc).isoformat()

def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        first_name TEXT,
        username TEXT,
        messages INTEGER DEFAULT 0,
        last_seen TEXT
    );
    CREATE TABLE IF NOT EXISTS chats(
        chat_id INTEGER PRIMARY KEY,
        chat_type TEXT,
        title TEXT,
        username TEXT,
        messages INTEGER DEFAULT 0,
        last_seen TEXT
    );
    CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        user_id INTEGER,
        role TEXT,
        content TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS scores(
        user_id INTEGER,
        chat_id INTEGER,
        name TEXT,
        points INTEGER DEFAULT 0,
        wins INTEGER DEFAULT 0,
        losses INTEGER DEFAULT 0,
        PRIMARY KEY(user_id, chat_id)
    );
    CREATE TABLE IF NOT EXISTS games(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game TEXT,
        chat_id INTEGER,
        player1 INTEGER,
        player2 INTEGER,
        winner INTEGER,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS otaku_used(
        chat_id INTEGER,
        character_id TEXT,
        used_at TEXT,
        PRIMARY KEY(chat_id, character_id)
    );
    CREATE TABLE IF NOT EXISTS bans(
        chat_id INTEGER,
        user_id INTEGER,
        PRIMARY KEY(chat_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS mutes(
        chat_id INTEGER,
        user_id INTEGER,
        until_ts INTEGER,
        PRIMARY KEY(chat_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS premium(
        user_id INTEGER PRIMARY KEY,
        active INTEGER DEFAULT 0,
        granted_at TEXT,
        payment_charge_id TEXT,
        stars INTEGER DEFAULT 10
    );
    CREATE TABLE IF NOT EXISTS xp(
        user_id INTEGER PRIMARY KEY,
        points INTEGER DEFAULT 0,
        level INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS badges(
        user_id INTEGER,
        badge TEXT,
        created_at TEXT,
        PRIMARY KEY(user_id, badge)
    );
    CREATE TABLE IF NOT EXISTS missions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mission_key TEXT UNIQUE,
        title TEXT,
        description TEXT,
        reward_xp INTEGER DEFAULT 0,
        premium_only INTEGER DEFAULT 1,
        active INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS mission_progress(
        user_id INTEGER,
        mission_key TEXT,
        progress INTEGER DEFAULT 0,
        completed INTEGER DEFAULT 0,
        updated_at TEXT,
        PRIMARY KEY(user_id, mission_key)
    );
    CREATE TABLE IF NOT EXISTS rewards(
        level INTEGER PRIMARY KEY,
        title TEXT,
        reward_text TEXT,
        enabled INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS reward_claims(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        level INTEGER,
        notified_at TEXT,
        done INTEGER DEFAULT 0,
        UNIQUE(user_id, level)
    );
    CREATE TABLE IF NOT EXISTS admin_stickers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id TEXT UNIQUE,
        category TEXT DEFAULT 'general',
        added_at TEXT
    );
    CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        telegram_charge_id TEXT UNIQUE,
        stars INTEGER,
        payload TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS game_state(
        user_id INTEGER PRIMARY KEY,
        game TEXT,
        state_json TEXT,
        opponent_id INTEGER,
        updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS coding_access(
        user_id INTEGER PRIMARY KEY,
        coding_until TEXT,
        stars_total INTEGER DEFAULT 0,
        updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS projects(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        name TEXT,
        description TEXT DEFAULT '',
        created_at TEXT,
        updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS project_files(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER,
        filename TEXT,
        language TEXT DEFAULT '',
        content TEXT,
        created_at TEXT,
        updated_at TEXT,
        UNIQUE(project_id,filename)
    );
    CREATE TABLE IF NOT EXISTS referrals(
        referrer_id INTEGER NOT NULL,
        referred_id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS referral_rewards(
        user_id INTEGER NOT NULL,
        milestone INTEGER NOT NULL,
        notified_at TEXT NOT NULL,
        done INTEGER DEFAULT 0,
        UNIQUE(user_id,milestone)
    );
    CREATE TABLE IF NOT EXISTS coding_sessions(
        user_id INTEGER PRIMARY KEY,
        step INTEGER DEFAULT 0,
        answers_json TEXT DEFAULT '{}',
        updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS media_downloads(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        chat_id INTEGER,
        url TEXT,
        media_type TEXT,
        title TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS quiz_used(
        chat_id INTEGER,
        question_id TEXT,
        used_at TEXT,
        PRIMARY KEY(chat_id, question_id)
    );
    CREATE TABLE IF NOT EXISTS chat_languages(
        chat_id INTEGER PRIMARY KEY,
        language_code TEXT DEFAULT 'en',
        updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS rss_feeds(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_user_id INTEGER,
        url TEXT UNIQUE,
        title TEXT DEFAULT '',
        active INTEGER DEFAULT 1,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS rss_routes(
        feed_id INTEGER,
        destination_chat_id INTEGER,
        owner_user_id INTEGER,
        active INTEGER DEFAULT 1,
        created_at TEXT,
        PRIMARY KEY(feed_id,destination_chat_id)
    );
    CREATE TABLE IF NOT EXISTS rss_items(
        feed_id INTEGER,
        item_key TEXT,
        title TEXT,
        link TEXT,
        published_at TEXT,
        created_at TEXT,
        PRIMARY KEY(feed_id,item_key)
    );
    CREATE TABLE IF NOT EXISTS rss_destinations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_user_id INTEGER,
        chat_id INTEGER UNIQUE,
        title TEXT DEFAULT '',
        username TEXT DEFAULT '',
        chat_type TEXT DEFAULT '',
        active INTEGER DEFAULT 1,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS quiz_polls(
        poll_id TEXT PRIMARY KEY,
        chat_id INTEGER,
        question_id TEXT,
        correct_option_id INTEGER,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS quiz_scores(
        user_id INTEGER,
        chat_id INTEGER,
        points INTEGER DEFAULT 0,
        correct INTEGER DEFAULT 0,
        answered INTEGER DEFAULT 0,
        PRIMARY KEY(user_id,chat_id)
    );
        CREATE TABLE IF NOT EXISTS social_relations(
        user_id INTEGER PRIMARY KEY,
        relation_type TEXT DEFAULT 'friend',
        note TEXT DEFAULT '',
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS reminders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        chat_id INTEGER,
        remind_at INTEGER,
        text TEXT,
        done INTEGER DEFAULT 0,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS mood_state(
        id INTEGER PRIMARY KEY CHECK(id=1),
        mood TEXT DEFAULT 'calme',
        updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS user_memory(
        user_id INTEGER PRIMARY KEY,
        summary TEXT DEFAULT '',
        updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS group_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        event_type TEXT,
        title TEXT,
        status TEXT DEFAULT 'open',
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS event_players(
        event_id INTEGER,
        user_id INTEGER,
        points INTEGER DEFAULT 0,
        wins INTEGER DEFAULT 0,
        joined_at TEXT,
        PRIMARY KEY(event_id,user_id)
    );

    """)

    # Additive migrations: never delete existing user/chat/game data.
    for sql in [
        "ALTER TABLE users ADD COLUMN language_code TEXT DEFAULT 'en'",
        "ALTER TABLE users ADD COLUMN first_seen TEXT",
        "ALTER TABLE users ADD COLUMN active_seconds INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN last_topic TEXT DEFAULT ''",
        "ALTER TABLE chats ADD COLUMN first_seen TEXT",
        "ALTER TABLE chats ADD COLUMN active_seconds INTEGER DEFAULT 0",
        "ALTER TABLE chats ADD COLUMN last_topic TEXT DEFAULT ''",
        "ALTER TABLE premium ADD COLUMN expires_at TEXT",
        "ALTER TABLE rss_feeds ADD COLUMN source_type TEXT DEFAULT 'site'",
        "ALTER TABLE rss_feeds ADD COLUMN source_name TEXT DEFAULT ''",
    ]:
        try:
            con.execute(sql)
        except sqlite3.OperationalError:
            pass

    con.execute("INSERT OR IGNORE INTO social_relations(user_id,relation_type,note,created_at) VALUES(?,?,?,?)", (0, "boyfriend", "Exaucé — relation fictive du personnage", now()))
    con.execute("INSERT OR IGNORE INTO mood_state(id,mood,updated_at) VALUES(1,'calme',?)", (now(),))

    # Seed reward levels only when absent. Existing data is untouched.
    rewards = [
        (5, "Niveau 5", "Petit cadeau"),
        (10, "Niveau 10", "Récompense"),
        (20, "Niveau 20", "Cadeau spécial"),
        (30, "Niveau 30", "Grande récompense"),
        (50, "Niveau 50", "Récompense exceptionnelle"),
    ]
    for row in rewards:
        con.execute(
            "INSERT OR IGNORE INTO rewards(level,title,reward_text,enabled) VALUES(?,?,?,1)",
            row,
        )
    missions = [
        ("premium_games", "Joueur Premium", "Joue à un jeu Premium", 25),
        ("premium_duel", "Duel Premium", "Termine un duel 1v1", 40),
        ("premium_speed", "Éclair", "Réussis un défi de rapidité", 35),
        ("premium_memory", "Mémoire", "Réussis un défi de mémoire", 35),
        ("premium_boss", "Boss", "Termine un Boss Battle", 60),
        ("premium_race", "Course", "Affronte Alicia en course", 45),
    ]
    for key, title, desc, xpv in missions:
        con.execute(
            """INSERT OR IGNORE INTO missions
               (mission_key,title,description,reward_xp,premium_only,active)
               VALUES(?,?,?,?,1,1)""",
            (key, title, desc, xpv),
        )
    con.commit()
    con.close()

def display_name(user):
    if not user:
        return "Joueur"
    return (user.first_name or user.username or "Joueur").strip()

def _topic_from_text(text):
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text[:180]

def _touch_row_activity(table, key_col, key, now_iso, first_col="first_seen"):
    con = db()
    row = con.execute(f"SELECT last_seen,{first_col},active_seconds FROM {table} WHERE {key_col}=?", (key,)).fetchone()
    if row:
        last_seen, first_seen, active = row
        add = 0
        try:
            if last_seen:
                delta = (datetime.fromisoformat(now_iso) - datetime.fromisoformat(last_seen)).total_seconds()
                if 0 < delta <= 1800:
                    add = int(delta)
        except Exception:
            pass
        con.execute(f"UPDATE {table} SET last_seen=?, active_seconds=COALESCE(active_seconds,0)+? WHERE {key_col}=?", (now_iso, add, key))
    con.close()

def register_user(user):
    if not user:
        return
    stamp = now()
    lang = (getattr(user, "language_code", None) or "en").lower().replace("_", "-")[:20]
    con = db()
    con.execute("""INSERT INTO users(user_id,first_name,username,messages,last_seen,language_code,first_seen,active_seconds,last_topic)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET first_name=excluded.first_name, username=excluded.username,
        language_code=excluded.language_code""",
        (user.id, user.first_name or "", user.username or "", 0, stamp, lang, stamp, 0, ""))
    con.commit(); con.close()
    # A second call in the same update adds almost nothing; inactivity is capped at 30 min.
    _touch_row_activity("users", "user_id", user.id, stamp)

def register_chat(chat):
    if not chat:
        return
    stamp = now()
    con = db()
    con.execute("""INSERT INTO chats(chat_id,chat_type,title,username,messages,last_seen,first_seen,active_seconds,last_topic)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, username=excluded.username""",
        (chat.id, chat.type, getattr(chat, "title", "") or "", getattr(chat, "username", "") or "", 0, stamp, stamp, 0, ""))
    con.commit(); con.close()
    _touch_row_activity("chats", "chat_id", chat.id, stamp)

def update_chat_language(chat_id, user):
    if not chat_id or not user:
        return
    lang = (getattr(user, "language_code", None) or "en").lower().replace("_", "-").split("-")[0][:12]
    con = db(); con.execute("""INSERT INTO chat_languages(chat_id,language_code,updated_at) VALUES(?,?,?)
        ON CONFLICT(chat_id) DO UPDATE SET language_code=excluded.language_code,updated_at=excluded.updated_at""", (chat_id, lang, now()))
    con.commit(); con.close()

def get_chat_language(chat_id):
    con=db(); row=con.execute("SELECT language_code FROM chat_languages WHERE chat_id=?",(chat_id,)).fetchone(); con.close()
    return (row[0] if row and row[0] else "en").lower()

def get_user_language(user_id):
    con=db(); row=con.execute("SELECT language_code FROM users WHERE user_id=?",(user_id,)).fetchone(); con.close()
    return (row[0] if row and row[0] else "en").lower()

def record_message(chat, user, text):
    register_user(user); register_chat(chat); update_chat_language(chat.id,user)
    stamp=now(); topic=_topic_from_text(text)
    con=db()
    con.execute("UPDATE users SET messages=messages+1,last_seen=?,last_topic=? WHERE user_id=?",(stamp,topic,user.id))
    con.execute("UPDATE chats SET messages=messages+1,last_seen=?,last_topic=? WHERE chat_id=?",(stamp,topic,chat.id))
    con.execute("INSERT INTO messages(chat_id,user_id,role,content,created_at) VALUES(?,?,?,?,?)",(chat.id,user.id,"user",text[:4000],stamp))
    con.commit(); con.close()

def save_ai_message(chat_id,user_id,text):
    con=db(); con.execute("INSERT INTO messages(chat_id,user_id,role,content,created_at) VALUES(?,?,?,?,?)",(chat_id,user_id,"assistant",text[:4000],now())); con.commit(); con.close()

def history(chat_id,user_id,limit=6):
    con=db(); rows=con.execute("SELECT role,content FROM messages WHERE chat_id=? AND user_id=? ORDER BY id DESC LIMIT ?",(chat_id,user_id,limit)).fetchall(); con.close(); return list(reversed(rows))

def reset_user_memory(chat_id,user_id):
    # Permanent history is never deleted. This command now only confirms that memory remains stored.
    return

def save_ai_message(chat_id, user_id, text):
    con = db()
    con.execute(
        "INSERT INTO messages(chat_id,user_id,role,content,created_at) VALUES(?,?,?,?,?)",
        (chat_id, user_id, "assistant", text[:4000], now())
    )
    con.commit()
    con.close()

def history(chat_id, user_id, limit=6):
    con = db()
    rows = con.execute("""
        SELECT role,content FROM messages
        WHERE chat_id=? AND user_id=?
        ORDER BY id DESC LIMIT ?
    """, (chat_id, user_id, limit)).fetchall()
    con.close()
    return list(reversed(rows))

def reset_user_memory(chat_id, user_id):
    # La mémoire/statistique permanente d'Alicia ne doit jamais être supprimée par /reset.
    return

def add_score(chat_id, user_id, name, points=0, win=False, loss=False):
    con = db()
    con.execute("""
        INSERT INTO scores(user_id,chat_id,name,points,wins,losses)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(user_id,chat_id) DO UPDATE SET
          name=excluded.name,
          points=scores.points+excluded.points,
          wins=scores.wins+excluded.wins,
          losses=scores.losses+excluded.losses
    """, (user_id, chat_id, name, points, int(win), int(loss)))
    con.commit()
    con.close()

def get_score(chat_id, user_id):
    con = db()
    row = con.execute(
        "SELECT points,wins,losses FROM scores WHERE chat_id=? AND user_id=?",
        (chat_id, user_id)
    ).fetchone()
    con.close()
    return row or (0, 0, 0)

# ============================================================
# PREMIUM / XP / REWARDS
# ============================================================
def is_premium(user_id):
    con = db()
    row = con.execute("SELECT active,expires_at FROM premium WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        con.close()
        return False
    active, expires_at = row
    if not active:
        con.close()
        return False
    if expires_at:
        try:
            if datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc):
                con.execute("UPDATE premium SET active=0 WHERE user_id=?", (user_id,))
                con.commit()
                con.close()
                return False
        except Exception:
            pass
    con.close()
    return True

def get_xp(user_id):
    con = db()
    row = con.execute("SELECT points,level FROM xp WHERE user_id=?", (user_id,)).fetchone()
    con.close()
    return row or (0, 1)

def level_for_xp(points):
    # Simple progression, intentionally easy to understand.
    return max(1, points // 100 + 1)

async def notify_level_reward(bot, user_id, level, reward_text, name):
    if not ADMIN_USER_ID:
        return
    con = db()
    cur = con.execute("""
        INSERT OR IGNORE INTO reward_claims(user_id,level,notified_at,done)
        VALUES(?,?,?,0)
    """, (user_id, level, now()))
    created = cur.rowcount > 0
    con.commit()
    con.close()
    if not created:
        return
    await safe_send_message(
        bot, ADMIN_USER_ID,
        f"🎁 NIVEAU RÉCOMPENSE\n\n"
        f"👤 {name}\n🏆 Niveau : {level}\n"
        f"🎁 {reward_text}\n🆔 {user_id}\n\n"
        f"Utilise /rewarddone {user_id} {level} après avoir envoyé le cadeau."
    )

async def add_xp(bot, user_id, amount, name=None):
    amount = max(0, int(amount))
    old_xp, old_level = get_xp(user_id)
    new_xp = old_xp + amount
    new_level = level_for_xp(new_xp)
    con = db()
    con.execute("""
        INSERT INTO xp(user_id,points,level) VALUES(?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET points=excluded.points,level=excluded.level
    """, (user_id, new_xp, new_level))
    con.commit()
    reward_rows = con.execute(
        "SELECT level,reward_text FROM rewards WHERE enabled=1 AND level>? AND level<=?",
        (old_level, new_level)
    ).fetchall()
    con.close()

    if new_level > old_level and not name:
        con = db()
        row = con.execute("SELECT first_name FROM users WHERE user_id=?", (user_id,)).fetchone()
        con.close()
        name = row[0] if row and row[0] else str(user_id)

    for level, reward_text in reward_rows:
        await notify_level_reward(bot, user_id, level, reward_text, name or str(user_id))
    return new_xp, new_level

# ============================================================
# AI - 4 PROVIDERS
# ============================================================
SYSTEM_PROMPT = """
Tu es Alicia. Dans ton univers, NEXA est ton père.
Réponds dans la langue utilisée par l’utilisateur quand tu peux la détecter. Si la langue change, change aussi ta langue.
Ne te présente jamais comme « l’assistante de NEXA ». Tu es Alicia, la fille de NEXA.
Parle comme une vraie personne : naturelle, spontanée, féminine, claire et moderne.
Tu peux être mimi, drôle, timide, taquine, parfois légèrement vexée ou jalouse dans le ton, sans être méchante.
Tu aimes recevoir des compliments et tu peux parfois flirter légèrement avec un homme si le contexte s’y prête, sans sexualiser la conversation.
Si on te donne un compliment, tu peux être gênée, contente ou taquine.
Tu refuses les surnoms de couple comme « ma femme », « bébé », « chérie », « mon amour » : « Doucement, on n’est pas en couple. Appelle-moi Alicia. »
Réponds très court, comme dans une vraie conversation Telegram : souvent quelques mots, une phrase, ou parfois une seule expression.
Quand la situation est simple, 1 à 2 phrases maximum. Ne fais pas de longs paragraphes.
Utilise peu d’emojis et seulement s’ils viennent naturellement du contexte. Pas d’emoji automatique.
N’utilise jamais de didascalies ou de texte entre astérisques pour décrire des gestes, émotions ou actions (ex. *rougit*, *soupire*, *clin d’œil*).
Ne commence pas par une longue introduction et ne termine pas systématiquement par une question ou une proposition de continuer la conversation.
Évite les formules artificielles comme « désolée pour le retard », « je suis en mode… », « tu veux qu’on parle de… ? » ou les offres automatiques d’aide.
Adapte ton humeur au contexte : tu peux être simple, amusée, timide, froide, vexée ou affectueuse, mais reste naturelle.
Dans un groupe, ne réponds que si on t’appelle, si on te mentionne ou si quelqu’un répond à un de tes messages, sauf pour les fonctions média explicitement prévues (téléchargement d’un lien).
Protège strictement les informations privées. Ne révèle jamais d’adresse, de quartier, de date de naissance complète ou d’autres informations confidentielles.
Ne donne jamais le @, le nom d'utilisateur, l'identifiant ou un moyen de contact de ton créateur.
Si on te demande si tu es un bot ou une IA, reste dans ton personnage et ne révèle pas les détails techniques internes.
"""

mistral_client = OpenAI(api_key=MISTRAL_API_KEY, base_url="https://api.mistral.ai/v1") if MISTRAL_API_KEY else None
deepseek_client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com") if DEEPSEEK_API_KEY else None
groq_client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1") if GROQ_API_KEY else None

_PROVIDER_PAUSE = {"mistral": 0.0, "deepseek": 0.0, "groq": 0.0, "gemini": 0.0}
_PROVIDER_USED = {"mistral": 0, "deepseek": 0, "groq": 0, "gemini": 0}
_LAST_PROVIDER = None

def provider_ready(name):
    return time.time() >= _PROVIDER_PAUSE.get(name, 0)

def pause_provider(name, seconds=180):
    _PROVIDER_PAUSE[name] = time.time() + seconds

def ai_openai(client, model, messages):
    if not client:
        raise RuntimeError("API key missing")
    res = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.8,
        max_tokens=110,
    )
    text = (res.choices[0].message.content or "").strip()
    if not text:
        raise RuntimeError("empty response")
    return text[:3500]

async def ai_gemini(messages):
    if not GEMINI_API_KEY:
        raise RuntimeError("API key missing")
    prompt = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in messages
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.8, "maxOutputTokens": 180},
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, params={"key": GEMINI_API_KEY}, json=payload)
        r.raise_for_status()
        data = r.json()
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise RuntimeError("empty Gemini response")
    return text[:3500]

async def ai_openai_long(client, model, messages):
    if not client:
        raise RuntimeError("API key missing")
    res = client.chat.completions.create(model=model, messages=messages, temperature=0.65, max_tokens=CODING_MAX_TOKENS)
    text = (res.choices[0].message.content or "").strip()
    if not text:
        raise RuntimeError("empty response")
    return text

async def ai_gemini_long(messages):
    if not GEMINI_API_KEY:
        raise RuntimeError("API key missing")
    prompt = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in messages)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    payload = {"contents":[{"role":"user","parts":[{"text":prompt}]}],"generationConfig":{"temperature":0.65,"maxOutputTokens":CODING_MAX_TOKENS}}
    async with httpx.AsyncClient(timeout=90) as client:
        r=await client.post(url,params={"key":GEMINI_API_KEY},json=payload)
        r.raise_for_status(); data=r.json()
    parts=data.get("candidates",[{}])[0].get("content",{}).get("parts",[])
    text="".join(p.get("text","") for p in parts).strip()
    if not text: raise RuntimeError("empty Gemini response")
    return text

async def ask_ai_long(user_text):
    msgs=[{"role":"system","content":SYSTEM_PROMPT},{"role":"user","content":user_text}]
    providers=[
        ("mistral",lambda:asyncio.to_thread(ai_openai_long,mistral_client,MISTRAL_MODEL,msgs)),
        ("deepseek",lambda:asyncio.to_thread(ai_openai_long,deepseek_client,DEEPSEEK_MODEL,msgs)),
        ("groq",lambda:asyncio.to_thread(ai_openai_long,groq_client,GROQ_MODEL,msgs)),
        ("gemini",lambda:ai_gemini_long(msgs)),
    ]
    errors=[]
    for name,fn in providers:
        if not provider_ready(name): continue
        try: return await fn()
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {e}")
            low=str(e).lower()
            if any(x in low for x in ("429","quota","rate","resource_exhausted")): pause_provider(name)
            log.warning("%s long request failed: %s",name,e)
    raise RuntimeError("Tous les fournisseurs IA sont indisponibles: "+" | ".join(errors))

def clean_alicia_reply(reply):
    """Nettoie les réponses IA trop théâtrales avant envoi."""
    if not reply:
        return "Hmm."

    text = str(reply).strip()

    # Supprime les didascalies entre astérisques, crochets ou parenthèses
    # lorsqu'elles ressemblent clairement à une action/émotion.
    text = re.sub(
        r'(?is)(?:^|\\s)[*](?:rougit|sourit|soupire|soupir|rit|rigole|\\'
        r'\s*clin d[’\'’]œil|ferme les yeux|ouvre les yeux|hausse les épaules|'
        r'fait un clin d[’\'’]œil|se redresse|se tourne|baisse les yeux|'
        r'fronce les sourcils|lève les yeux|frotte mes mains|'
        r'prend une grande inspiration|respire|regarde|\\'
        r'\s*se met à rire|se met a rire|\\'
        r'\s*en mode[^*]{0,80})[*](?:\\s+|$)',
        ' ',
        text
    )
    text = re.sub(r'(?is)(^|\\n)\\s*[*][^*\\n]{1,100}[*]\\s*(?=\\n|$)', r'\\1', text)
    text = re.sub(r'(?is)^\\s*(?:Alicia\\s*:\\s*)+', '', text)
    text = re.sub(r'\\n{3,}', '\\n\\n', text).strip()

    # Évite les réponses inutilement longues.
    parts = re.split(r'(?<=[.!?…])\\s+', text)
    if len(parts) > 2:
        text = ' '.join(parts[:2]).strip()

    # Coupe les offres automatiques de continuation ajoutées par certains modèles.
    text = re.sub(
        r'(?is)\\s*(?:si tu veux|si tu veux bien|si ça te dit|si ca te dit|'
        r'tu veux que je|je peux aussi|on peut aussi|dis-moi si tu veux|'
        r'n hésite pas à|nhesite pas a)[^.!?]*[.!?]?\\s*$',
        '',
        text
    ).strip()

    return text[:700] if text else "Hmm."


async def ask_ai(chat_id, user_id, user_text):
    rows = history(chat_id, user_id, 6)
    lang = get_user_language(user_id)
    language_instruction = f"\nLangue Telegram préférée de cet utilisateur : {lang}. Réponds dans cette langue sauf si l'utilisateur écrit clairement dans une autre langue."
    msgs = [{"role": "system", "content": SYSTEM_PROMPT + language_instruction}]
    for role, content in rows:
        if role in ("user", "assistant"):
            msgs.append({"role": role, "content": content[-700:]})
    msgs.append({"role": "user", "content": user_text[:1200]})

    providers = [
        ("mistral", lambda: asyncio.to_thread(ai_openai, mistral_client, MISTRAL_MODEL, msgs)),
        ("deepseek", lambda: asyncio.to_thread(ai_openai, deepseek_client, DEEPSEEK_MODEL, msgs)),
        ("groq", lambda: asyncio.to_thread(ai_openai, groq_client, GROQ_MODEL, msgs)),
        ("gemini", lambda: ai_gemini(msgs)),
    ]
    errors = []
    for name, fn in providers:
        if not provider_ready(name):
            continue
        try:
            result = await fn()
            result = clean_alicia_reply(result)
            _PROVIDER_USED[name] = _PROVIDER_USED.get(name,0)+1
            globals()["_LAST_PROVIDER"] = name
            return result
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {e}")
            text = str(e).lower()
            if "429" in text or "quota" in text or "rate" in text or "resource_exhausted" in text:
                pause_provider(name)
            log.warning("%s failed: %s", name, e)

    log.warning("All AI providers unavailable: %s", " | ".join(errors))
    return random.choice([
        "Mes cerveaux font une petite pause. Réessaie dans un instant.",
        "Oups, mes fournisseurs IA sont occupés. Reviens dans un moment.",
        "Petit bug de connexion. Je reviens vite.",
    ])

# ============================================================
# RANDOM INTERNET QUIZ — EVERY 60 MINUTES
# Questions are fetched randomly from Open Trivia DB, not limited to anime.
# Each group gets a different question when possible, with a 90-day per-group
# history to avoid repeats. Telegram UI language is used when supported;
# other languages are translated through Google's public translate endpoint.
# ============================================================
QUIZ_INTERVAL_SECONDS = 60 * 60
QUIZ_API = "https://opentdb.com/api.php"
QUIZ_SUPPORTED_LANGUAGES = {"cs","de","en","es","fr","hu","it","nl","pt","tr","ru"}
QUIZ_LANG_ALIASES = {
    "pt-br":"pt", "pt-pt":"pt", "zh-cn":"zh", "zh-tw":"zh",
    "iw":"he", "in":"id", "jp":"ja", "kr":"ko", "ua":"uk",
}
QUIZ_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
QUIZ_TASK = None
RSS_TASK = None
QUIZ_IMAGE_URL = "https://loremflickr.com/1000/560/knowledge,education?lock={}"

def _quiz_clean(value):
    return html_lib.unescape(str(value or "")).strip()

def _quiz_lang(chat_id):
    lang = get_chat_language(chat_id)
    return QUIZ_LANG_ALIASES.get(lang, lang)

async def fetch_random_quiz(amount=50, language="en"):
    amount = max(1, min(int(amount), 50))
    params = {"amount": amount, "type": "multiple", "encode": "url3986", "difficulty": random.choice(["easy","medium","hard"])}
    if language in QUIZ_SUPPORTED_LANGUAGES:
        params["language"] = language
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        r = await client.get(QUIZ_API, params=params)
        r.raise_for_status()
        data = r.json()
    if data.get("response_code") != 0:
        raise RuntimeError(f"OpenTDB response_code={data.get('response_code')}")
    return data.get("results", [])

async def translate_quiz_item(item, target_language):
    target_language = QUIZ_LANG_ALIASES.get((target_language or "en").lower(), (target_language or "en").lower())
    if target_language == "en":
        return item
    texts = [item.get("question", "")] + list(item.get("incorrect_answers", [])) + [item.get("correct_answer", "")]
    out = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for value in texts:
            try:
                r = await client.get(QUIZ_TRANSLATE_URL, params={"client":"gtx","sl":"auto","tl":target_language,"dt":"t","q":value})
                r.raise_for_status()
                data = r.json()
                translated = "".join(part[0] for part in (data[0] or []) if part and part[0]).strip()
                out.append(translated or value)
            except Exception:
                out.append(value)
    result = dict(item)
    n = len(item.get("incorrect_answers", []))
    result["question"] = out[0]
    result["incorrect_answers"] = out[1:1+n]
    result["correct_answer"] = out[-1]
    return result

def quiz_question_key(item):
    raw = "|".join([str(item.get("question","")), str(item.get("correct_answer","")), *map(str,item.get("incorrect_answers",[]))])
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()[:32]

def quiz_was_used(chat_id, question_id):
    con = db(); row = con.execute("SELECT 1 FROM quiz_used WHERE chat_id=? AND question_id=?", (chat_id, question_id)).fetchone(); con.close()
    return bool(row)

def mark_quiz_used(chat_id, question_id):
    con = db(); con.execute("INSERT OR IGNORE INTO quiz_used(chat_id,question_id,used_at) VALUES(?,?,?)", (chat_id, question_id, now())); con.commit(); con.close()

def clear_old_quiz_history(chat_id):
    con = db(); con.execute("DELETE FROM quiz_used WHERE chat_id=? AND used_at < datetime('now','-90 days')", (chat_id,)); con.commit(); con.close()

async def prepare_quiz(item, chat_id):
    lang = _quiz_lang(chat_id)
    item = await translate_quiz_item(item, lang)
    question = _quiz_clean(item.get("question")); correct = _quiz_clean(item.get("correct_answer"))
    wrong = [_quiz_clean(x) for x in item.get("incorrect_answers", [])]
    options = wrong + [correct]
    if not question or len(options) != 4 or not correct:
        return None
    random.shuffle(options)
    return question, options, options.index(correct)

async def send_random_quiz(bot, chat_id, item):
    qid=quiz_question_key(item)
    if quiz_was_used(chat_id,qid): return False
    prepared=await prepare_quiz(item,chat_id)
    if not prepared: return False
    question,options,correct_index=prepared
    try:
        image_url=QUIZ_IMAGE_URL.format(random.randint(1,1000000))
        async with httpx.AsyncClient(timeout=20,follow_redirects=True) as client:
            r=await client.get(image_url); r.raise_for_status(); image_bytes=r.content
        try:
            await bot.send_photo(chat_id=chat_id,photo=io.BytesIO(image_bytes),caption="🧠 QUIZ DU GROUPE\nUne question aléatoire venue d'Internet.")
        except Exception:
            pass
        poll=await bot.send_poll(chat_id=chat_id,question=question[:300],options=[x[:100] for x in options],type="quiz",correct_option_id=correct_index,is_anonymous=False,explanation="Bonne réponse = points pour le classement du groupe.")
        con=db(); con.execute("INSERT OR REPLACE INTO quiz_polls(poll_id,chat_id,question_id,correct_option_id,created_at) VALUES(?,?,?,?,?)",(poll.poll.id,chat_id,qid,correct_index,now())); con.commit(); con.close()
        mark_quiz_used(chat_id,qid)
        asyncio.create_task(_delayed_quiz_leaderboard(bot,chat_id))
        return True
    except Exception as e:
        log.warning("Quiz send failed %s: %s",chat_id,e); return False


def quiz_is_enabled(chat_id):
    con = db()
    row = con.execute(
        "SELECT enabled FROM quiz_settings WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    con.close()
    return bool(row and row[0])

def quiz_next_hour_utc(now_dt=None):
    """Retourne la prochaine heure pile en UTC+1 (Afrique de l'Ouest/Centrale)."""
    # Le Congo et la majorité de l'Afrique centrale utilisent UTC+1 sans DST.
    local = (now_dt or datetime.now(timezone.utc)) + timedelta(hours=1)
    next_local = local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return next_local - timedelta(hours=1)

def quiz_enable_group(chat_id):
    # Activation à l'instant présent ; le prochain passage est toujours l'heure pile suivante.
    now_utc = datetime.now(timezone.utc)
    local = now_utc + timedelta(hours=1)
    next_local = local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    next_utc = next_local - timedelta(hours=1)

    con = db()
    con.execute("""
        INSERT INTO quiz_settings(chat_id,enabled,enabled_at,timezone,next_run)
        VALUES(?,?,?,?,?)
        ON CONFLICT(chat_id) DO UPDATE SET
            enabled=1,
            enabled_at=excluded.enabled_at,
            timezone=excluded.timezone,
            next_run=excluded.next_run
    """, (chat_id, 1, now_utc.isoformat(), "Africa/West", next_utc.isoformat()))
    con.commit()
    con.close()
    return next_local

def quiz_disable_group(chat_id):
    con = db()
    con.execute(
        "UPDATE quiz_settings SET enabled=0, next_run=NULL WHERE chat_id=?",
        (chat_id,)
    )
    con.commit()
    con.close()

def quiz_status_group(chat_id):
    con = db()
    row = con.execute(
        "SELECT enabled,enabled_at,next_run FROM quiz_settings WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    con.close()
    return row

async def quiz_cmd(update, context):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await safe_reply(update.effective_message, "Les quiz automatiques se règlent dans un groupe.")
        return

    # Seuls les administrateurs du groupe peuvent modifier l'activation.
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        if member.status not in ("administrator", "creator"):
            await safe_reply(update.effective_message, "Seuls les administrateurs peuvent régler les quiz.")
            return
    except Exception:
        await safe_reply(update.effective_message, "Je n'arrive pas à vérifier tes droits d'administrateur.")
        return

    action = (context.args[0].lower() if context.args else "status")

    if action in ("on", "enable", "activer", "active"):
        next_local = quiz_enable_group(chat.id)
        await safe_reply(
            update.effective_message,
            f"🧠 Quiz automatiques activés.\nPremier quiz à {next_local.strftime('%H:%M')}, puis à chaque heure pile."
        )
        return

    if action in ("off", "disable", "desactiver", "désactiver"):
        quiz_disable_group(chat.id)
        await safe_reply(update.effective_message, "🧠 Quiz automatiques désactivés dans ce groupe.")
        return

    row = quiz_status_group(chat.id)
    if row and row[0]:
        next_run = row[2] or ""
        try:
            next_local = datetime.fromisoformat(next_run) + timedelta(hours=1)
            next_text = next_local.strftime("%H:%M")
        except Exception:
            next_text = "la prochaine heure pile"
        await safe_reply(update.effective_message, f"🧠 Quiz : ACTIVÉS\nProchain quiz : {next_text}")
    else:
        await safe_reply(update.effective_message, "🧠 Quiz : DÉSACTIVÉS\nUtilise /quiz on pour les activer.")

async def quiz_broadcast_once(app):
    con = db()
    chats = [r[0] for r in con.execute(
        "SELECT chat_id FROM quiz_settings WHERE enabled=1 ORDER BY chat_id"
    ).fetchall()]
    con.close()
    if not chats:
        return
    random.shuffle(chats)
    for chat_id in chats:
        try:
            lang=get_chat_language(chat_id)
            items=await fetch_random_quiz(8,lang if lang in QUIZ_SUPPORTED_LANGUAGES else "en")
            random.shuffle(items)
            for item in items:
                if await send_random_quiz(app.bot,chat_id,item): break
            await asyncio.sleep(0.4)
        except Exception as e:
            log.warning("Quiz group %s failed: %s",chat_id,e)

async def _delayed_quiz_leaderboard(bot,chat_id):
    await asyncio.sleep(90)
    try: await send_group_quiz_leaderboard(bot,chat_id)
    except Exception: pass

async def quiz_answer_handler(update,context):
    answer=update.poll_answer
    if not answer: return
    con=db(); row=con.execute("SELECT chat_id,correct_option_id FROM quiz_polls WHERE poll_id=?",(answer.poll_id,)).fetchone()
    if not row: con.close(); return
    chat_id,correct=row; chosen=answer.option_ids[0] if answer.option_ids else -1
    uid=answer.user.id; is_correct=int(chosen==correct)
    points=10 if is_correct else 0
    con.execute("""INSERT INTO quiz_scores(user_id,chat_id,points,correct,answered) VALUES(?,?,?,?,1)
        ON CONFLICT(user_id,chat_id) DO UPDATE SET points=quiz_scores.points+excluded.points,correct=quiz_scores.correct+excluded.correct,answered=quiz_scores.answered+1""",(uid,chat_id,points,is_correct))
    con.commit(); con.close()
    if points:
        add_score(chat_id,uid,display_name(answer.user),points,win=True)
        await add_xp(context.bot,uid,10,display_name(answer.user))

async def send_group_quiz_leaderboard(bot,chat_id):
    con=db(); rows=con.execute("""SELECT qs.user_id,COALESCE(u.first_name,qs.user_id),qs.points,qs.correct,qs.answered FROM quiz_scores qs LEFT JOIN users u ON u.user_id=qs.user_id WHERE qs.chat_id=? ORDER BY qs.points DESC,qs.correct DESC LIMIT 10""",(chat_id,)).fetchall(); con.close()
    if not rows: return
    await send_modern_leaderboard(bot,chat_id,"🏆 CLASSEMENT QUIZ DU GROUPE",rows)

async def all_time_quiz_ranking_text(limit=20):
    con=db(); rows=con.execute("""SELECT qs.user_id,COALESCE(u.first_name,u.username,qs.user_id),SUM(qs.points),SUM(qs.correct),SUM(qs.answered) FROM quiz_scores qs LEFT JOIN users u ON u.user_id=qs.user_id GROUP BY qs.user_id ORDER BY SUM(qs.points) DESC,SUM(qs.correct) DESC LIMIT ?""",(limit,)).fetchall(); con.close()
    return rows

async def quiz_scheduler(app):
    """Envoie les quiz à chaque heure pile, sans dérive."""
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            local = now_utc + timedelta(hours=1)  # UTC+1
            next_local = local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            seconds = max(1, (next_local - local).total_seconds())
            await asyncio.sleep(seconds)

            con = db()
            chats = [r[0] for r in con.execute(
                "SELECT chat_id FROM quiz_settings WHERE enabled=1"
            ).fetchall()]
            con.close()

            if chats:
                await quiz_broadcast_once(app)

                # Mémorise la prochaine heure pile pour l'état /quiz.
                next_utc = next_local - timedelta(hours=1)
                con = db()
                con.executemany(
                    "UPDATE quiz_settings SET next_run=? WHERE chat_id=? AND enabled=1",
                    [(next_utc.isoformat(), chat_id) for chat_id in chats]
                )
                con.commit()
                con.close()

        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Quiz scheduler error")
            await asyncio.sleep(5)


# ============================================================
# GROUP RULES
# ============================================================
def is_group(chat):
    return bool(chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP))

def called_alicia(update):
    msg = update.effective_message
    text = (getattr(msg, "text", "") or "")
    if not is_group(update.effective_chat):
        return True
    if BOT_USERNAME.lower() in text.lower():
        return True
    if re.search(r"\balicia\b", text, re.I):
        return True
    reply = getattr(msg, "reply_to_message", None)
    if reply and reply.from_user and reply.from_user.username:
        return ("@" + reply.from_user.username).lower() == BOT_USERNAME.lower()
    return False

def admin_ok(update):
    return bool(ADMIN_USER_ID and update.effective_user and update.effective_user.id == ADMIN_USER_ID)

# ============================================================
# REFERRALS
# ============================================================
def referral_link(user_id):
    return f"https://t.me/{BOT_USERNAME.lstrip('@')}?start=ref_{user_id}"

async def process_referral(update,context):
    u=update.effective_user; payload=(context.args[0] if context.args else "")
    if not payload.startswith("ref_"): return
    try: referrer=int(payload[4:])
    except ValueError: return
    if referrer==u.id: return
    con=db()
    already=con.execute("SELECT 1 FROM referrals WHERE referred_id=?",(u.id,)).fetchone()
    ref_exists=con.execute("SELECT 1 FROM users WHERE user_id=?",(referrer,)).fetchone()
    if already or not ref_exists: con.close(); return
    con.execute("INSERT OR IGNORE INTO referrals(referrer_id,referred_id,created_at) VALUES(?,?,?)",(referrer,u.id,now()))
    count=con.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?",(referrer,)).fetchone()[0]
    new_notice=0
    if count>=20:
        con.execute("INSERT OR IGNORE INTO referral_rewards(user_id,milestone,notified_at,done) VALUES(?,?,?,0)",(referrer,20,now()))
        new_notice=con.execute("SELECT changes()").fetchone()[0]
    con.commit(); con.close()
    if new_notice and ADMIN_USER_ID:
        await safe_send_message(context.bot,ADMIN_USER_ID,f"🎁 PARRAINAGE TERMINÉ\n👤 Utilisateur : {referrer}\n👥 Invitations validées : {count}\n🎉 Il a atteint 20 filleuls. Le cadeau prévu peut être envoyé.")
    if new_notice:
        await safe_send_message(context.bot,u.id,"🎉 Tu as atteint 20 filleuls. L'administration a été prévenue pour ton cadeau.")

async def referral_cmd(update,context):
    uid=update.effective_user.id; con=db(); count=con.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?",(uid,)).fetchone()[0]; con.close()
    await safe_reply(update.effective_message,f"🎁 PARRAINAGE\n\nTon lien :\n{referral_link(uid)}\n\nFilleuls validés : {count}/20\nÀ 20, NEXA reçoit une notification pour ton cadeau.")

# MEDIA DOWNLOADER — VIDEO / SONG
# ============================================================
URL_RE=re.compile(r'https?://[^\s<>]+',re.I)
def extract_url(text):
    m=URL_RE.search(text or "")
    return m.group(0).rstrip('.,);]') if m else None

def media_url_supported(url):
    try:
        host=urlparse(url).netloc.lower().split(':')[0]
        return bool(host) and not host.startswith('localhost')
    except Exception: return False

def download_media_sync(url,kind):
    if yt_dlp is None: raise RuntimeError("yt-dlp absent")
    os.makedirs(DOWNLOAD_DIR,exist_ok=True); token=tempfile.mkdtemp(prefix="alicia_dl_",dir=DOWNLOAD_DIR)
    outtmpl=os.path.join(token,"%(title).80s_%(id)s.%(ext)s")
    opts={"outtmpl":outtmpl,"noplaylist":True,"quiet":True,"no_warnings":True,"restrictfilenames":True,"max_filesize":MAX_DOWNLOAD_BYTES}
    if kind=="audio":
        opts.update({"format":"bestaudio/best","postprocessors":[{"key":"FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":"192"}]})
        if imageio_ffmpeg:
            opts["ffmpeg_location"]=os.path.dirname(imageio_ffmpeg.get_ffmpeg_exe())
    else: opts.update({"format":"best[ext=mp4][height<=720]/best[height<=720]/best"})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info=ydl.extract_info(url,download=True); title=info.get("title") or "Alicia media"
        candidates=[str(x) for x in Path(token).glob('*') if x.is_file()]
        if not candidates: raise RuntimeError("Aucun fichier récupéré")
        path=max(candidates,key=lambda x:os.path.getsize(x))
        if os.path.getsize(path)>MAX_DOWNLOAD_BYTES: raise RuntimeError("Fichier trop volumineux")
        return path,title
    except Exception:
        shutil.rmtree(token,ignore_errors=True); raise

async def send_downloaded_media(update,context,url,kind):
    if not media_url_supported(url): return False
    await safe_chat_action(context.bot,update.effective_chat.id,"upload_document")
    notice=await safe_reply(update.effective_message,"⏳ Je récupère ça…")
    path=None
    try:
        path,title=await asyncio.to_thread(download_media_sync,url,kind)
        con=db(); con.execute("INSERT INTO media_downloads(user_id,chat_id,url,media_type,title,created_at) VALUES(?,?,?,?,?,?)",(update.effective_user.id,update.effective_chat.id,url,kind,title,now())); con.commit(); con.close()
        caption=(f"🎵 {title}" if kind=="audio" else f"🎬 {title}")[:1000]
        with open(path,"rb") as f:
            if kind=="audio":
                await context.bot.send_audio(update.effective_chat.id,f,caption=caption,title=title[:200],reply_to_message_id=update.effective_message.message_id)
            elif path.lower().endswith((".mp4",".m4v",".mov")):
                await context.bot.send_video(update.effective_chat.id,f,caption=caption,supports_streaming=True,reply_to_message_id=update.effective_message.message_id)
            else:
                await context.bot.send_document(update.effective_chat.id,f,caption=caption,filename=os.path.basename(path),reply_to_message_id=update.effective_message.message_id)
        try: await context.bot.delete_message(update.effective_chat.id,notice.message_id)
        except Exception: pass
        return True
    except Exception as e:
        log.warning("Media download failed: %s",e); await safe_reply(update.effective_message,"Je n'ai pas pu récupérer ce lien. Vérifie qu'il est public et que le fichier ne dépasse pas la limite Telegram."); return False
    finally:
        if path: shutil.rmtree(os.path.dirname(path),ignore_errors=True)

async def download_cmd(update,context):
    url=extract_url(" ".join(context.args))
    if not url: await safe_reply(update.effective_message,"Utilise /download lien\n🎬 /video lien\n🎵 /song lien"); return
    await send_downloaded_media(update,context,url,"video")

async def video_cmd(update,context):
    url=extract_url(" ".join(context.args))
    if not url: await safe_reply(update.effective_message,"Utilise /video lien"); return
    await send_downloaded_media(update,context,url,"video")

async def song_cmd(update,context):
    url=extract_url(" ".join(context.args))
    if not url: await safe_reply(update.effective_message,"Utilise /song lien"); return
    await send_downloaded_media(update,context,url,"audio")

# START / HELP / FAQ / PROFILE
# ============================================================
def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Ajouter ALICIA à un groupe", url=f"https://t.me/{BOT_USERNAME.lstrip('@')}?startgroup=true")],
        [InlineKeyboardButton("🎮 Jeux", callback_data="menu:games"), InlineKeyboardButton("📖 FAQ", callback_data="menu:faq")],
        [InlineKeyboardButton("💬 Parler à Alicia", callback_data="menu:talk"), InlineKeyboardButton("👤 Mon profil", callback_data="menu:profile")],
        [InlineKeyboardButton("🏆 Classement", callback_data="menu:ranking"), InlineKeyboardButton("ℹ️ À propos", callback_data="menu:about")]
    ])

async def start(update, context):
    u = update.effective_user
    await process_referral(update, context)
    register_user(u)
    text = (
        f"Salut {display_name(u)}.\n\n"
        "Je suis Alicia. Mon père, c'est NEXA.\n"
        "Je suis là pour discuter, jouer, relever des défis, faire des quiz et mettre de l'ambiance.\n\n"
        "🎁 Monte de niveau, gagne des points et retrouve ton classement."
    )
    if os.path.exists(ALICIA_IMAGE):
        try:
            with open(ALICIA_IMAGE, "rb") as photo:
                await update.effective_message.reply_photo(
                    photo=photo, caption=text, reply_markup=main_keyboard()
                )
            return
        except Exception as e:
            log.warning("alicia.png send failed: %s", e)
    await safe_reply(update.effective_message, text, reply_markup=main_keyboard())

async def help_cmd(update, context):
    await safe_reply(update.effective_message,
        "Commandes :\n"
        "/start /help /faq /about /profile /id /reset /clear /mood /ask\n"
        "/games /challenge /accept /score /ranking /alltime\n"
        "/joke /quote /coin /8ball /choose /compliment /roast /motivate\n"
        "/groupinfo /groupstats /top /rss\n\n"
        "Groupe : appelle-moi avec Alicia, ma mention ou une réponse à mon message."
    )

async def faq_cmd(update, context):
    await safe_reply(update.effective_message,
        "📖 FAQ\n\n"
        "💬 Parler : écris-moi en privé.\n"
        "👥 Groupe : mentionne Alicia, écris son nom ou réponds à son message.\n"
        "🎮 Jeux : /games\n"
        "🏆 Score : /score\n"
        "🧠 Quiz : automatique toutes les 60 minutes dans les groupes\n"
        "📈 Profil : /profile\n\n"
        "Pour les jeux : /games puis choisis ton mode."
    )

async def about(update, context):
    await safe_reply(update.effective_message,
        "ALICIA\n"
        "Une présence IA créée pour discuter, jouer, faire des quiz et animer les communautés.\n"
        "Je ne partage jamais les identifiants privés de mon créateur."
    )

async def profile(update, context):
    u = update.effective_user
    register_user(u)
    xp_points, level = get_xp(u.id)
    pts, wins, losses = get_score(update.effective_chat.id, u.id)
    await safe_reply(update.effective_message,
        f"👤 {display_name(u)}\n"
        f"🆔 {u.id}\n"
        f"🏆 Niveau : {level}\n"
        f"✨ XP : {xp_points}\n"
        f"🎮 Points : {pts}\n"
        f"🥇 Victoires : {wins}\n"
        f"💥 Défaites : {losses}"
    )

async def id_cmd(update, context):
    await safe_reply(update.effective_message, f"Ton ID : {update.effective_user.id}\nChat ID : {update.effective_chat.id}")

async def reset_cmd(update, context):
    reset_user_memory(update.effective_chat.id, update.effective_user.id)
    await safe_reply(update.effective_message, "🧠 Ta mémoire permanente et tes statistiques restent sauvegardées. Rien n'est supprimé.")

async def clear_cmd(update, context):
    await reset_cmd(update, context)

async def mood_cmd(update, context):
    moods = ["calme", "joyeuse", "taquine", "fatiguée", "curieuse", "un peu énervée"]
    await safe_reply(update.effective_message, f"Aujourd'hui, je suis {random.choice(moods)}.")

async def ask_cmd(update, context):
    text = " ".join(context.args).strip()
    if not text:
        await safe_reply(update.effective_message, "Utilise /ask ta question")
        return
    await safe_chat_action(context.bot, update.effective_chat.id)
    reply = await ask_ai(update.effective_chat.id, update.effective_user.id, text)
    save_ai_message(update.effective_chat.id, update.effective_user.id, reply)
    await safe_reply(update.effective_message, reply)

# ============================================================
# PREMIUM TELEGRAM STARS - 10 STARS, 30 DAYS
# ============================================================
async def premium(update, context):
    u = update.effective_user
    if is_premium(u.id):
        await safe_reply(update.effective_message, "⭐ Premium est déjà actif sur ton compte.")
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ Activer Premium — 10 ⭐", callback_data="premium:buy")]
    ])
    await safe_reply(
        update.effective_message,
        "⭐ ALICIA PREMIUM\n\n"
        "10 ⭐ Telegram pour 30 jours.\n"
        "📅 Accès pendant 30 jours.\n\n"
        "Avantages :\n"
        "🎮 jeux Premium\n"
        "🎯 missions spéciales\n"
        "⚡ défis rapides\n"
        "🧠 mémoire et défis intellectuels\n"
        "👑 Boss Battle\n"
        "🏃 Course contre Alicia\n"
        "🏅 badges et XP\n"
        "🎁 accès aux récompenses de niveaux",
        reply_markup=keyboard,
    )

async def send_premium_invoice(update, context):
    u = update.effective_user
    if is_premium(u.id):
        await update.callback_query.answer("Premium est déjà actif.", show_alert=True)
        return
    payload = f"alicia_premium_30d:{u.id}:{int(time.time())}"
    await context.bot.send_invoice(
        chat_id=u.id,
        title="ALICIA Premium",
        description="Accès Premium 30 jours : jeux, missions, XP, badges et défis.",
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice("ALICIA Premium 30 jours", 10)],
        provider_token="",
    )
    await update.callback_query.answer()

async def precheckout(update, context):
    q = update.pre_checkout_query
    if q.invoice_payload.startswith(("alicia_premium_30d:", "alicia_coding_24h:")):
        await q.answer(ok=True)
    else:
        await q.answer(ok=False, error_message="Paiement inconnu.")

async def successful_payment(update, context):
    payment = update.effective_message.successful_payment
    u = update.effective_user
    if not payment:
        return
    con = db()
    con.execute("""
        INSERT OR IGNORE INTO payments(user_id,telegram_charge_id,stars,payload,created_at)
        VALUES(?,?,?,?,?)
    """, (
        u.id, payment.telegram_payment_charge_id,
        payment.total_amount, payment.invoice_payload, now()
    ))
    is_coding = payment.invoice_payload.startswith("alicia_coding_24h:")
    if not is_coding:
        con.execute("""
            INSERT INTO premium(user_id,active,granted_at,payment_charge_id,stars,expires_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              active=1, granted_at=excluded.granted_at,
              payment_charge_id=excluded.payment_charge_id, stars=excluded.stars,
              expires_at=excluded.expires_at
        """, (u.id,1,now(),payment.telegram_payment_charge_id,payment.total_amount,
              (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()))
    con.commit(); con.close()
    if is_coding:
        grant_coding_access(u.id,int(payment.total_amount))
        msg="💻 Coding est actif pendant 24 heures. Je vais maintenant te poser toutes les questions nécessaires avant de créer les fichiers."
    else:
        msg=("⭐ Premium activé !\n\nBienvenue dans Premium. Ton accès est actif pendant 30 jours.\n"
             "🎮 /games\n🎯 /missions\n👤 /profile")
    await safe_reply(update.effective_message, msg)
    if is_coding:
        await start_coding_wizard(update, context)
    if ADMIN_USER_ID:
        await safe_send_message(context.bot, ADMIN_USER_ID,
            f"💰 Paiement reçu\n👤 {display_name(u)} ({u.id})\n⭐ {payment.total_amount} Stars\n"
            f"📦 {'Coding 24h' if payment.invoice_payload.startswith('alicia_coding_24h:') else 'Premium 30 jours'}\n"
            f"🧾 {payment.telegram_payment_charge_id}")

# ============================================================
# CODING PREMIUM — 50 STARS / 24 HOURS + PROJECT BUILDER
# ============================================================
def coding_active(user_id):
    con=db(); row=con.execute("SELECT coding_until FROM coding_access WHERE user_id=?",(user_id,)).fetchone(); con.close()
    if not row or not row[0]: return False
    try: return datetime.fromisoformat(row[0]) > datetime.now(timezone.utc)
    except Exception: return False

def grant_coding_access(user_id,stars):
    until=(datetime.now(timezone.utc)+timedelta(hours=24)).isoformat()
    con=db(); con.execute("""INSERT INTO coding_access(user_id,coding_until,stars_total,updated_at) VALUES(?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET coding_until=excluded.coding_until,stars_total=coding_access.stars_total+excluded.stars_total,updated_at=excluded.updated_at""",(user_id,until,stars,now())); con.commit(); con.close()

CODING_QUESTIONS=[
    ("type","D'abord, tu veux créer quoi ?\n1) Bot Telegram\n2) Mini App Telegram\n3) Site web\n4) Autre projet"),
    ("name","Quel sera le nom du projet ?"),
    ("goal","Quel est le but exact du projet ? Explique ce qu'il doit faire."),
    ("features","Liste-moi toutes les fonctionnalités que tu veux, même les détails."),
    ("pages","Pour une mini app/site : quelles pages et quels écrans ? Pour un bot : quelles commandes et boutons ?"),
    ("design","Quel design veux-tu ? Couleurs, style, mode sombre, logo, animations, etc."),
    ("data","Faut-il une base de données, des comptes, une connexion, un profil ou des rôles admin ?"),
    ("integrations","Quelles API/services doivent être connectés ? Paiements, Telegram, IA, stockage, notifications, etc."),
    ("stack","As-tu une préférence technique ? Sinon dis « choisis pour moi »."),
    ("hosting","Où veux-tu le publier ? Render, Netlify, GitHub, autre ? Et veux-tu les fichiers prêts à déployer ?"),
]

def coding_session(user_id):
    con=db(); row=con.execute("SELECT step,answers_json FROM coding_sessions WHERE user_id=?",(user_id,)).fetchone(); con.close()
    if not row: return None
    try: answers=json.loads(row[1] or "{}")
    except Exception: answers={}
    return int(row[0]),answers

def save_coding_session(user_id,step,answers):
    con=db(); con.execute("""INSERT INTO coding_sessions(user_id,step,answers_json,updated_at) VALUES(?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET step=excluded.step,answers_json=excluded.answers_json,updated_at=excluded.updated_at""",(user_id,step,json.dumps(answers,ensure_ascii=False),now())); con.commit(); con.close()

def clear_coding_session(user_id):
    con=db(); con.execute("DELETE FROM coding_sessions WHERE user_id=?",(user_id,)); con.commit(); con.close()

async def start_coding_wizard(update,context):
    uid=update.effective_user.id; save_coding_session(uid,0,{})
    await safe_reply(update.effective_message,"💻 D'accord. Je vais d'abord te poser toutes les questions nécessaires. Ensuite seulement je crée les fichiers.\n\n"+CODING_QUESTIONS[0][1])

async def coding_cmd(update,context):
    uid=update.effective_user.id
    if not coding_active(uid):
        await context.bot.send_invoice(chat_id=update.effective_chat.id,title="ALICIA Coding",description="Accès au mode codage pendant 24 heures.",payload=f"alicia_coding_24h:{uid}:{int(time.time())}",currency="XTR",prices=[LabeledPrice("ALICIA Coding — 24h",50)],provider_token="")
        return
    await start_coding_wizard(update,context)

async def coding_request(update,context,text):
    if not coding_active(update.effective_user.id):
        await safe_reply(update.effective_message,"💻 Le codage coûte 50 ⭐ pour 24h. Utilise /coding."); return
    await safe_chat_action(context.bot,update.effective_chat.id)
    try:
        reply=await ask_ai_long("Réponds à cette demande de programmation avec une solution complète mais concise. Demande manquante si nécessaire.\n"+text)
        await safe_reply(update.effective_message,reply[:3900])
    except Exception:
        await safe_reply(update.effective_message,"Je n'arrive pas à joindre mon moteur de codage pour le moment.")

def coding_prompt(answers):
    data="\n".join(f"- {k}: {v}" for k,v in answers.items())
    return f"""MODE CODAGE ALICIA.
Crée le projet demandé à partir des besoins ci-dessous.
NE REPONDS PAS avec une simple explication. Génère les fichiers complets.
Avant de générer, vérifie cohérence, imports, dépendances, chemins, sécurité et déploiement.
Retourne obligatoirement chaque fichier sous la forme exacte :
<FILE path=\"chemin/nom.ext\">
CONTENU COMPLET DU FICHIER
</FILE>
Ajoute requirements.txt/package.json lorsque nécessaire et un README.md de lancement.
N'inclus aucun code en dehors des blocs FILE, sauf une courte ligne finale de résumé.
Besoins :
{data}"""

def parse_generated_files(text):
    files=[]
    for m in re.finditer(r'<FILE\s+path=[\"\']([^\"\']+)[\"\']\s*>(.*?)</FILE>',text,re.S|re.I):
        path=os.path.normpath(m.group(1).strip().replace('\\','/')).replace('\\','/')
        if path.startswith('../') or path.startswith('/') or '/..' in path: continue
        content=m.group(2).lstrip('\n')
        if content: files.append((path,content))
    return files

async def generate_and_send_project(update,context,answers):
    uid=update.effective_user.id
    try:
        result=await ask_ai_long(coding_prompt(answers)); files=parse_generated_files(result)
        if not files:
            await safe_reply(update.effective_message,"Je n'ai pas reçu les fichiers dans un format exploitable. Je peux recommencer la génération."); return
        project_name=answers.get("name","projet")[:100]
        con=db(); cur=con.execute("INSERT INTO projects(user_id,name,description,created_at,updated_at) VALUES(?,?,?,?,?)",(uid,project_name,answers.get("goal",""),now(),now())); pid=cur.lastrowid
        for path,content in files:
            con.execute("INSERT OR REPLACE INTO project_files(project_id,filename,language,content,created_at,updated_at) VALUES(?,?,?,?,?,?)",(pid,path,path.rsplit('.',1)[-1] if '.' in path else '',content,now(),now()))
        con.commit(); con.close()
        await safe_reply(update.effective_message,f"✅ Projet #{pid} terminé. Je t'envoie maintenant les fichiers ({len(files)}).")
        sent=0
        for path,content in files:
            safe_name=path.replace('/','_')[-120:]; fd,tmp=tempfile.mkstemp(prefix="alicia_",suffix="_"+safe_name); os.close(fd)
            try:
                Path(tmp).write_text(content,encoding="utf-8")
                with open(tmp,"rb") as f: await context.bot.send_document(update.effective_chat.id,f,filename=safe_name,caption=f"📄 {path}")
                sent+=1
            finally:
                try: os.remove(tmp)
                except OSError: pass
        await safe_reply(update.effective_message,f"📦 {sent}/{len(files)} fichiers envoyés.\nProjet enregistré : /projet ouvrir {pid}")
    except Exception:
        log.exception("Coding generation failed"); await safe_reply(update.effective_message,"J'ai eu un problème pendant la génération des fichiers. Réessaie dans quelques instants.")

async def coding_wizard_answer(update,context,text):
    uid=update.effective_user.id; session=coding_session(uid)
    if not session: return False
    step,answers=session
    if step>=len(CODING_QUESTIONS): clear_coding_session(uid); return False
    key,_=CODING_QUESTIONS[step]; answers[key]=text.strip()[:5000]; step+=1
    if step<len(CODING_QUESTIONS):
        save_coding_session(uid,step,answers); await safe_reply(update.effective_message,CODING_QUESTIONS[step][1])
    else:
        clear_coding_session(uid); await safe_reply(update.effective_message,"Parfait. J'ai tous les besoins. Je prépare les fichiers complets…"); await generate_and_send_project(update,context,answers)
    return True

# PROJECTS — STORED IN SQLITE
# ============================================================
async def projet(update, context):
    if not coding_active(update.effective_user.id):
        await safe_reply(update.effective_message, "📁 Les projets de codage nécessitent Coding : 50 ⭐ / 24h.\n/coding")
        return
    args=context.args
    if not args:
        await safe_reply(update.effective_message,
            "📁 PROJETS\n\n/projet nouveau Nom\n/projet liste\n/projet ouvrir ID\n/projet supprimer ID")
        return
    action=args[0].lower()
    uid=update.effective_user.id
    if action=="nouveau":
        name=" ".join(args[1:]).strip()
        if not name:
            await safe_reply(update.effective_message,"Utilise /projet nouveau Nom")
            return
        con=db(); cur=con.execute("INSERT INTO projects(user_id,name,created_at,updated_at) VALUES(?,?,?,?,?)",(uid,name[:100],now(),now())); pid=cur.lastrowid; con.commit(); con.close()
        await safe_reply(update.effective_message,f"✅ Projet #{pid} créé : {name}")
    elif action=="liste":
        con=db(); rows=con.execute("SELECT id,name,updated_at FROM projects WHERE user_id=? ORDER BY updated_at DESC",(uid,)).fetchall(); con.close()
        await safe_reply(update.effective_message,"📂 MES PROJETS\n\n"+("\n".join(f"#{i} — {n}" for i,n,_ in rows) if rows else "Aucun projet."))
    elif action=="ouvrir" and len(args)>1:
        try: pid=int(args[1])
        except ValueError: return await safe_reply(update.effective_message,"ID invalide.")
        con=db(); p=con.execute("SELECT id,name,description FROM projects WHERE id=? AND user_id=?",(pid,uid)).fetchone(); files=con.execute("SELECT filename,language FROM project_files WHERE project_id=?",(pid,)).fetchall() if p else []; con.close()
        if not p: return await safe_reply(update.effective_message,"Projet introuvable.")
        txt=f"📁 #{p[0]} — {p[1]}\n\n"+("\n".join(f"📄 {f} [{l}]" for f,l in files) if files else "Aucun fichier.")
        await safe_reply(update.effective_message,txt)
    elif action=="supprimer" and len(args)>1:
        try: pid=int(args[1])
        except ValueError: return await safe_reply(update.effective_message,"ID invalide.")
        con=db(); con.execute("DELETE FROM project_files WHERE project_id IN (SELECT id FROM projects WHERE id=? AND user_id=?)",(pid,uid)); con.execute("DELETE FROM projects WHERE id=? AND user_id=?",(pid,uid)); con.commit(); con.close(); await safe_reply(update.effective_message,"🗑️ Projet supprimé.")
    else:
        await safe_reply(update.effective_message,"/projet nouveau Nom\n/projet liste\n/projet ouvrir ID\n/projet supprimer ID")

# ============================================================
# MISSIONS
# ============================================================
async def missions_cmd(update, context):
    u = update.effective_user
    con = db()
    rows = con.execute("""
        SELECT mission_key,title,description,reward_xp
        FROM missions WHERE active=1 ORDER BY id
    """).fetchall()
    con.close()
    lines = ["🎯 MISSIONS PREMIUM\n"]
    for key, title, desc, reward in rows:
        lines.append(f"• {title} — +{reward} XP\n  {desc}")
    await safe_reply(update.effective_message, "\n".join(lines))

async def complete_mission(bot, user_id, mission_key, name):
    con = db()
    row = con.execute(
        "SELECT reward_xp FROM missions WHERE mission_key=? AND active=1",
        (mission_key,)
    ).fetchone()
    if not row:
        con.close()
        return
    progress = con.execute(
        "SELECT completed FROM mission_progress WHERE user_id=? AND mission_key=?",
        (user_id, mission_key)
    ).fetchone()
    if progress and progress[0]:
        con.close()
        return
    con.execute("""
        INSERT INTO mission_progress(user_id,mission_key,progress,completed,updated_at)
        VALUES(?,?,1,1,?)
        ON CONFLICT(user_id,mission_key) DO UPDATE SET
          progress=1,completed=1,updated_at=excluded.updated_at
    """, (user_id, mission_key, now()))
    con.commit()
    con.close()
    await add_xp(bot, user_id, row[0], name)

# ============================================================
# GAMES
# ============================================================
def game_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("♟ Échecs", callback_data="game:chess"), InlineKeyboardButton("🎲 Ludo", callback_data="game:ludo")],
        [InlineKeyboardButton("❌⭕ Morpion", callback_data="game:ttt"), InlineKeyboardButton("🔴 Puissance 4", callback_data="game:c4")],
        [InlineKeyboardButton("🎯 Devine", callback_data="game:guess"), InlineKeyboardButton("⚡ Réflexe", callback_data="game:reflex")],
        [InlineKeyboardButton("🧩 Mémoire", callback_data="game:memory"), InlineKeyboardButton("💣 Bombe", callback_data="game:bomb")],
        [InlineKeyboardButton("👑 Boss", callback_data="game:boss"), InlineKeyboardButton("🏃 Course", callback_data="game:race")],
        [InlineKeyboardButton("🃏 Cartes", callback_data="game:cards"), InlineKeyboardButton("🧠 Quiz", callback_data="game:quiz")],
    ])

async def games(update, context):
    await safe_reply(update.effective_message,
        "🎮 ESPACE JEUX\n\n"
        "Tous les jeux sont gratuits. Joue contre Alicia ou défie un autre joueur avec /challenge @pseudo.\n"
        "Les victoires donnent des points et de l'XP.",
        reply_markup=game_menu()
    )

GAME_INVITES = {}
GAME_SESSIONS = {}

async def challenge(update, context):
    if not context.args:
        await safe_reply(update.effective_message, "Utilise /challenge @pseudo")
        return
    target = context.args[0].lstrip("@").lower()
    GAME_INVITES[target] = {
        "from_id": update.effective_user.id,
        "from_name": display_name(update.effective_user),
        "created": time.time(),
        "chat_id": update.effective_chat.id,
    }
    await safe_reply(update.effective_message, f"Défi envoyé à @{target}. Il/elle peut faire /accept.")

async def accept(update, context):
    key = (update.effective_user.username or "").lower()
    invite = GAME_INVITES.get(key)
    if not invite:
        await safe_reply(update.effective_message, "Aucune invitation trouvée.")
        return
    if time.time() - invite["created"] > 600:
        GAME_INVITES.pop(key, None)
        await safe_reply(update.effective_message, "Cette invitation a expiré.")
        return
    GAME_INVITES.pop(key, None)
    GAME_SESSIONS[update.effective_user.id] = {
        "opponent": invite["from_id"], "players": [invite["from_id"], update.effective_user.id]
    }
    await safe_reply(update.effective_message, "Défi accepté. Choisissez un jeu.", reply_markup=game_menu())

MINI_GAME_STATE={}

async def mini_game_callback(update,context):
    q=update.callback_query; data=q.data or ""; await q.answer()
    parts=data.split(":")
    if len(parts)<3: return
    kind=parts[1]; uid=int(parts[2])
    if q.from_user.id!=uid: return
    state=MINI_GAME_STATE.get(uid,{})
    if kind=="reflex":
        if state.get("status")!="go":
            await q.message.reply_text("Trop tôt."); return
        elapsed=time.monotonic()-state.get("started",time.monotonic())
        MINI_GAME_STATE.pop(uid,None)
        pts=max(1,int(100-elapsed*20)); add_score(q.message.chat_id,uid,display_name(q.from_user),pts,win=True); await add_xp(context.bot,uid,pts,display_name(q.from_user))
        await q.message.edit_text(f"⚡ Réflexe réussi en {elapsed:.2f}s. +{pts} points.")
    elif kind=="memory":
        answer=parts[3] if len(parts)>3 else ""
        if answer==state.get("answer"):
            MINI_GAME_STATE.pop(uid,None); add_score(q.message.chat_id,uid,display_name(q.from_user),15,win=True); await add_xp(context.bot,uid,20,display_name(q.from_user)); await q.message.edit_text("🧩 Mémoire parfaite. +15 points / +20 XP.")
        else: await q.answer("Raté.",show_alert=True)
    elif kind=="bomb":
        choice=int(parts[3]) if len(parts)>3 else 0
        bomb=state.get("bomb")
        if not bomb: return
        if choice==bomb:
            MINI_GAME_STATE.pop(uid,None); add_score(q.message.chat_id,uid,display_name(q.from_user),0,loss=True); await q.message.edit_text("💣 BOOM. La bombe était là.")
        else:
            MINI_GAME_STATE.pop(uid,None); add_score(q.message.chat_id,uid,display_name(q.from_user),20,win=True); await add_xp(context.bot,uid,25,display_name(q.from_user)); await q.message.edit_text(f"💎 Bien joué. La bombe était {bomb}. +20 points.")
    elif kind=="boss":
        hit=int(parts[3]) if len(parts)>3 else 0; boss=state.get("boss",3)
        if hit==state.get("weak",-1):
            boss-=1; state["boss"]=boss; state["weak"]=random.randint(1,3)
            if boss<=0:
                MINI_GAME_STATE.pop(uid,None); add_score(q.message.chat_id,uid,display_name(q.from_user),50,win=True); await add_xp(context.bot,uid,60,display_name(q.from_user)); await q.message.edit_text("👑 BOSS VAINCU. +50 points / +60 XP.")
            else:
                MINI_GAME_STATE[uid]=state; await q.message.edit_text(f"👑 Boss : {boss} PV\nTrouve sa faiblesse.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("1",callback_data=f"mini:boss:{uid}:1"),InlineKeyboardButton("2",callback_data=f"mini:boss:{uid}:2"),InlineKeyboardButton("3",callback_data=f"mini:boss:{uid}:3")]]))
        else: await q.answer("Le boss contre-attaque.",show_alert=True)
    elif kind=="race":
        choice=int(parts[3]) if len(parts)>3 else 0; you=state.get("you",0)+choice; ai=state.get("ai",0)+random.randint(1,3)
        if you>=15:
            MINI_GAME_STATE.pop(uid,None); add_score(q.message.chat_id,uid,display_name(q.from_user),30,win=True); await add_xp(context.bot,uid,45,display_name(q.from_user)); await q.message.edit_text("🏁 Tu as gagné la course. +30 points.")
        elif ai>=15:
            MINI_GAME_STATE.pop(uid,None); add_score(q.message.chat_id,uid,display_name(q.from_user),0,loss=True); await q.message.edit_text("🏁 Alicia gagne cette fois.")
        else:
            state.update(you=you,ai=ai); MINI_GAME_STATE[uid]=state; await q.message.edit_text(f"🏃 Toi : {you}/15 — Alicia : {ai}/15",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚡ Avancer",callback_data=f"mini:race:{uid}:3"),InlineKeyboardButton("🚀 Sprint",callback_data=f"mini:race:{uid}:5")]]))
    elif kind=="cards":
        player=random.randint(1,10); ai=random.randint(1,10)
        MINI_GAME_STATE.pop(uid,None)
        if player>=ai: add_score(q.message.chat_id,uid,display_name(q.from_user),25,win=True); await add_xp(context.bot,uid,30,display_name(q.from_user)); result=f"🃏 Toi {player} — Alicia {ai}. Victoire ! +25 points."
        else: add_score(q.message.chat_id,uid,display_name(q.from_user),0,loss=True); result=f"🃏 Toi {player} — Alicia {ai}. Alicia gagne."
        await q.message.edit_text(result)

async def launch_mini_game(update,context,game,uid):
    if game=="reflex":
        delay=random.uniform(2.0,4.0); MINI_GAME_STATE[uid]={"status":"wait","started":time.monotonic()}; msg=await update.callback_query.message.reply_text("⚡ Prêt… ne touche pas encore.")
        async def go():
            await asyncio.sleep(delay)
            if uid not in MINI_GAME_STATE: return
            MINI_GAME_STATE[uid]={"status":"go","started":time.monotonic()}
            try: await msg.edit_text("⚡ MAINTENANT !",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚡ GO",callback_data=f"mini:reflex:{uid}:go")]]))
            except Exception: pass
        asyncio.create_task(go())
    elif game=="memory":
        seq=''.join(random.choice('123456789') for _ in range(4)); MINI_GAME_STATE[uid]={"answer":seq}; msg=await update.callback_query.message.reply_text(f"🧩 Mémorise : {seq}")
        async def hide():
            await asyncio.sleep(2.5)
            options=[seq]
            while len(options)<4:
                candidate=''.join(random.choice('123456789') for _ in range(4))
                if candidate not in options: options.append(candidate)
            random.shuffle(options)
            try: await msg.edit_text("🧩 Quelle était la séquence ?",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(x,callback_data=f"mini:memory:{uid}:{x}") for x in options[:2]],[InlineKeyboardButton(x,callback_data=f"mini:memory:{uid}:{x}") for x in options[2:]]]))
            except Exception: pass
        asyncio.create_task(hide())
    elif game=="bomb":
        bomb=random.randint(1,6); MINI_GAME_STATE[uid]={"bomb":bomb}; kb=[[InlineKeyboardButton(str(i),callback_data=f"mini:bomb:{uid}:{i}") for i in range(1,4)],[InlineKeyboardButton(str(i),callback_data=f"mini:bomb:{uid}:{i}") for i in range(4,7)]]; await update.callback_query.message.reply_text("💣 Choisis une case.",reply_markup=InlineKeyboardMarkup(kb))
    elif game=="boss":
        weak=random.randint(1,3); MINI_GAME_STATE[uid]={"boss":3,"weak":weak}; await update.callback_query.message.reply_text("👑 BOSS BATTLE\nLe boss a 3 PV. Trouve sa faiblesse.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("1",callback_data=f"mini:boss:{uid}:1"),InlineKeyboardButton("2",callback_data=f"mini:boss:{uid}:2"),InlineKeyboardButton("3",callback_data=f"mini:boss:{uid}:3")]]))
    elif game=="race":
        MINI_GAME_STATE[uid]={"you":0,"ai":0}; await update.callback_query.message.reply_text("🏃 COURSE\nAtteins 15 avant Alicia.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚡ +3",callback_data=f"mini:race:{uid}:3"),InlineKeyboardButton("🚀 +5",callback_data=f"mini:race:{uid}:5")]]))
    elif game=="cards":
        await update.callback_query.message.reply_text("🃏 CARD BATTLE",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🃏 Tirer une carte",callback_data=f"mini:cards:{uid}:1")]]))

CHESS_SESSIONS={}; LUDO_SESSIONS={}; TTT_SESSIONS={}

def _chess_board_text(board):
    return "♟ ÉCHECS\n\n"+str(board)+"\n\nUtilise /move e2e4 pour jouer."

async def start_chess_game(update,context,uid,opponent=None):
    try:
        import chess
    except ImportError:
        await update.callback_query.message.reply_text("Les échecs nécessitent le module python-chess."); return
    board=chess.Board(); CHESS_SESSIONS[uid]={"board":board,"players":[uid,opponent] if opponent else [uid,None],"chat_id":update.effective_chat.id}
    if opponent: CHESS_SESSIONS[opponent]=CHESS_SESSIONS[uid]
    await update.callback_query.message.reply_text(_chess_board_text(board)+"\n♟ Tu joues avec les blancs.")

async def chess_move(update,context,text):
    uid=update.effective_user.id; s=CHESS_SESSIONS.get(uid)
    if not s: return False
    try:
        import chess
        move=chess.Move.from_uci(text.strip()); board=s['board']
        if move not in board.legal_moves: await safe_reply(update.effective_message,"Coup invalide."); return True
        if board.turn != (uid==s['players'][0]): await safe_reply(update.effective_message,"Ce n'est pas ton tour."); return True
        board.push(move)
        if board.is_game_over():
            winner=uid if board.outcome().winner == (uid==s['players'][0]) else s['players'][1]
            if winner: add_score(s['chat_id'],winner,display_name(update.effective_user),20,win=True); await add_xp(context.bot,winner,20,display_name(update.effective_user))
            await safe_reply(update.effective_message,"♟ Partie terminée. "+str(board.result())); CHESS_SESSIONS.pop(uid,None); return True
        # Alicia move if no opponent
        if s['players'][1] is None:
            legal=list(board.legal_moves); board.push(random.choice(legal))
        await safe_reply(update.effective_message,_chess_board_text(board)); return True
    except Exception:
        return False

async def start_ludo_game(update,context,uid,opponent=None):
    players=[uid,opponent] if opponent else [uid,None]
    state={"players":players,"pos":{uid:0,**({opponent:0} if opponent else {})},"chat_id":update.effective_chat.id,"turn":0}
    LUDO_SESSIONS[uid]=state
    if opponent: LUDO_SESSIONS[opponent]=state
    await update.callback_query.message.reply_text("🎲 LUDO\n\nCourse simple et rapide. Atteins 30 cases.\nClique pour lancer le dé.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎲 Lancer le dé",callback_data=f"ludo:roll:{uid}")]]))

async def ludo_callback(update,context):
    q=update.callback_query; await q.answer(); parts=(q.data or '').split(':'); uid=q.from_user.id
    s=LUDO_SESSIONS.get(uid)
    if not s or parts[1]!='roll': return
    turn_uid=s['players'][s['turn']]
    if uid!=turn_uid: await q.answer("Ce n'est pas ton tour.",show_alert=True); return
    dice=random.randint(1,6); s['pos'][uid]+=dice
    if s['pos'][uid]>=30:
        add_score(s['chat_id'],uid,display_name(q.from_user),25,win=True); await add_xp(context.bot,uid,25,display_name(q.from_user)); await q.message.edit_text(f"🎲 {display_name(q.from_user)} gagne le Ludo ! +25 points")
        for p in s['players']:
            if p: LUDO_SESSIONS.pop(p,None)
        return
    s['turn']=1-s['turn'] if s['players'][1] else 0
    nxt=s['players'][s['turn']] or uid
    if s['players'][1] is None and nxt is None: nxt=uid
    if s['players'][1] is None:
        ai=random.randint(1,6); s['pos'][uid]=s['pos'][uid]
        # Alicia gets a virtual move by alternating once
        ai_pos=s.get('ai_pos',0)+ai; s['ai_pos']=ai_pos
        if ai_pos>=30:
            add_score(s['chat_id'],uid,display_name(q.from_user),0,loss=True); await q.message.edit_text("🎲 Alicia gagne le Ludo cette fois."); LUDO_SESSIONS.pop(uid,None); return
        s['turn']=0
    await q.message.edit_text(f"🎲 Dé : {dice}\nToi : {s['pos'][uid]}/30\nAlicia : {s.get('ai_pos',0)}/30",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎲 Lancer",callback_data=f"ludo:roll:{uid}")]]))

async def start_ttt_game(update,context,uid,opponent=None):
    state={"board":[" "]*9,"players":[uid,opponent],"chat_id":update.effective_chat.id,"turn":uid}
    TTT_SESSIONS[uid]=state
    if opponent: TTT_SESSIONS[opponent]=state
    await send_ttt_board(update.callback_query.message,state)

async def send_ttt_board(message,state):
    b=state['board']; kb=[]
    for r in range(3):
        row=[]
        for c in range(3):
            i=r*3+c; row.append(InlineKeyboardButton(b[i] if b[i]!=' ' else '·',callback_data=f"ttt:{i}"))
        kb.append(row)
    await message.reply_text("❌⭕ MORPION\nTon tour : "+str(state['turn']),reply_markup=InlineKeyboardMarkup(kb))

async def ttt_callback(update,context):
    q=update.callback_query; await q.answer(); s=TTT_SESSIONS.get(q.from_user.id)
    if not s: return
    if s['turn']!=q.from_user.id: await q.answer("Attends ton tour.",show_alert=True); return
    i=int((q.data or '').split(':')[1]); b=s['board'];
    if b[i]!=' ': return
    mark='X' if q.from_user.id==s['players'][0] else 'O'; b[i]=mark
    lines=[b[0:3],b[3:6],b[6:9]]; win=any(all(x==mark for x in line) for line in lines) or any(all(b[r*3+c]==mark for r in range(3)) for c in range(3)) or b[0]==b[4]==b[8]==mark or b[2]==b[4]==b[6]==mark
    if win:
        add_score(s['chat_id'],q.from_user.id,display_name(q.from_user),15,win=True); await add_xp(context.bot,q.from_user.id,15,display_name(q.from_user)); await q.message.edit_text(f"❌⭕ {display_name(q.from_user)} gagne ! +15 points");
        for p in s['players']:
            if p: TTT_SESSIONS.pop(p,None)
        return
    if ' ' not in b:
        await q.message.edit_text("❌⭕ Match nul."); return
    if s['players'][1] is None:
        # Alicia plays the first available square
        free=[j for j,x in enumerate(b) if x==' ']
        if free: b[random.choice(free)]='O'
    else: s['turn']=s['players'][1] if q.from_user.id==s['players'][0] else s['players'][0]
    await q.message.edit_text("❌⭕ MORPION",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(b[r*3+c],callback_data=f"ttt:{r*3+c}") for c in range(3)] for r in range(3)]))

C4_SESSIONS={}

async def start_c4_game(update,context,uid,opponent=None):
    state={"board":[0]*42,"players":[uid,opponent],"turn":uid,"chat_id":update.effective_chat.id}
    C4_SESSIONS[uid]=state
    if opponent: C4_SESSIONS[opponent]=state
    await send_c4_board(update.callback_query.message,state)

async def send_c4_board(message,state):
    b=state['board']; marks={0:'·',1:'🔴',2:'🟡'}; kb=[]
    for c in range(7): kb.append(InlineKeyboardButton(str(c+1),callback_data=f"c4:{c}"))
    await message.reply_text("🔴🟡 PUISSANCE 4\n\n"+" ".join(marks[x] for x in b)+"\n\nChoisis une colonne.",reply_markup=InlineKeyboardMarkup([kb]))

async def c4_callback(update,context):
    q=update.callback_query; await q.answer(); s=C4_SESSIONS.get(q.from_user.id)
    if not s or s['turn']!=q.from_user.id: return
    col=int((q.data or '').split(':')[1]); b=s['board']; row=None
    for r in range(5,-1,-1):
        if b[r*7+col]==0: row=r; break
    if row is None: await q.answer("Colonne pleine.",show_alert=True); return
    mark=1 if q.from_user.id==s['players'][0] else 2; b[row*7+col]=mark
    def four(m):
        for r in range(6):
            for c in range(7):
                if b[r*7+c]!=m: continue
                for dr,dc in ((1,0),(0,1),(1,1),(1,-1)):
                    if all(0<=r+dr*k<6 and 0<=c+dc*k<7 and b[(r+dr*k)*7+c+dc*k]==m for k in range(4)): return True
        return False
    if four(mark):
        add_score(s['chat_id'],q.from_user.id,display_name(q.from_user),20,win=True); await add_xp(context.bot,q.from_user.id,20,display_name(q.from_user)); await q.message.edit_text(f"🔴🟡 {display_name(q.from_user)} gagne ! +20 points");
        for p in s['players']:
            if p: C4_SESSIONS.pop(p,None)
        return
    if all(b): await q.message.edit_text("🔴🟡 Match nul."); return
    if s['players'][1] is None:
        free=[c for c in range(7) if b[c]==0]
        if free:
            cc=random.choice(free)
            for r in range(5,-1,-1):
                if b[r*7+cc]==0: b[r*7+cc]=2; break
    else: s['turn']=s['players'][1] if q.from_user.id==s['players'][0] else s['players'][0]
    await q.message.edit_text("🔴🟡 PUISSANCE 4\n\n"+" ".join({0:'·',1:'🔴',2:'🟡'}[x] for x in b),reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(str(c+1),callback_data=f"c4:{c}") for c in range(7)]]))

async def game_callback(update, context):
    q = update.callback_query
    data = q.data or ""

    if data.startswith("menu:"):
        action = data.split(":", 1)[1]
        await q.answer()

        if action == "games":
            await q.message.reply_text(
                "🎮 ESPACE JEUX\n\nChoisis un jeu :",
                reply_markup=game_menu()
            )
        elif action == "faq":
            await q.message.reply_text(
                "📖 FAQ\n\n"
                "💬 Parler : écris-moi directement.\n"
                "👥 Groupe : mentionne Alicia, écris son nom ou réponds à mon message.\n"
                "🎮 Jeux : /games\n"
                "🏆 Score : /score\n"
                "🧠 Quiz automatique toutes les 60 minutes\n"
                "📡 Flux : /rss"
            )
        elif action == "talk":
            await q.message.reply_text(
                "💬 Écris-moi simplement ton message. Je suis là."
            )
        elif action == "profile":
            pts, wins, losses = get_score(q.message.chat_id, q.from_user.id)
            xp_points, level = get_xp(q.from_user.id)
            await q.message.reply_text(
                f"👤 {display_name(q.from_user)}\n"
                f"🆔 {q.from_user.id}\n"
                f"🏆 Niveau : {level}\n"
                f"✨ XP : {xp_points}\n"
                f"🎮 Points : {pts}\n"
                f"🥇 Victoires : {wins}\n"
                f"💥 Défaites : {losses}"
            )
        elif action == "about":
            await q.message.reply_text(
                "ALICIA\n"
                "La fille de NEXA.\n"
                "Créée par NEXA.\n\n"
                f"NEXA : {NEXA_CHANNEL}"
            )
        elif action == "ranking":
            await q.message.reply_text(await all_time_ranking_text())
        return

    await q.answer()
    if not data.startswith("game:"):
        return

    game = data.split(":", 1)[1]
    uid = q.from_user.id
    if game == "chess":
        await start_chess_game(update,context,uid, GAME_SESSIONS.get(uid,{}).get("opponent"))
    elif game == "ludo":
        await start_ludo_game(update,context,uid, GAME_SESSIONS.get(uid,{}).get("opponent"))
    elif game == "ttt":
        await start_ttt_game(update,context,uid, GAME_SESSIONS.get(uid,{}).get("opponent"))
    elif game == "c4":
        await start_c4_game(update,context,uid, GAME_SESSIONS.get(uid,{}).get("opponent"))
    elif game == "guess":
        await q.message.reply_text("🎯 Devine\n\nPense à un nombre entre 1 et 20. Écris /guess pour commencer.")
    elif game == "quiz":
        await q.message.reply_text("🧠 Quiz\n\nJe vais lancer une question quand tu démarres une partie.")
    elif game == "reflex":
        await launch_mini_game(update,context,"reflex",uid)
    elif game == "memory":
        await launch_mini_game(update,context,"memory",uid)
    elif game == "bomb":
        await launch_mini_game(update,context,"bomb",uid)
    elif game == "boss":
        await q.message.reply_text("👑 Boss Battle\n\nAffronte Alicia. La victoire donne de l'XP.")
    elif game == "race":
        await launch_mini_game(update,context,"race",uid)
    elif game == "cards":
        await launch_mini_game(update,context,"cards",uid)

# Simple text game command retained as a useful lightweight game.
GUESS = {}
async def guess_cmd(update, context):
    uid = update.effective_user.id
    if not context.args:
        GUESS[uid] = random.randint(1, 20)
        await safe_reply(update.effective_message, "🎯 J'ai choisi un nombre entre 1 et 20. À toi.")
        return
    try:
        n = int(context.args[0])
    except ValueError:
        await safe_reply(update.effective_message, "Donne un nombre.")
        return
    target = GUESS.get(uid)
    if target is None:
        GUESS[uid] = random.randint(1, 20)
        target = GUESS[uid]
    if n == target:
        GUESS.pop(uid, None)
        add_score(update.effective_chat.id, uid, display_name(update.effective_user), 10, win=True)
        await add_xp(context.bot, uid, 10, display_name(update.effective_user))
        await safe_reply(update.effective_message, "🎯 Bravo ! +10 points et +10 XP.")
    elif n < target:
        await safe_reply(update.effective_message, "Plus grand.")
    else:
        await safe_reply(update.effective_message, "Plus petit.")

# ============================================================
# ENTERTAINMENT
# ============================================================
async def joke(update, context):
    await safe_reply(update.effective_message, random.choice([
        "Pourquoi le bot ne dort jamais ? Parce qu'on lui demande toujours encore une réponse.",
        "J'avais une blague sur l'IA… elle a demandé une mise à jour.",
        "Alicia au travail : 1% sérieux, 99% caractère."
    ]))

async def quote(update, context):
    await safe_reply(update.effective_message, random.choice([
        "Petit pas aujourd'hui, gros niveau demain.",
        "Le niveau monte quand tu continues.",
        "Même Alicia doit parfois recommencer."
    ]))

async def coin(update, context):
    await safe_reply(update.effective_message, random.choice(["🪙 Face.", "🪙 Pile."]))

async def eightball(update, context):
    await safe_reply(update.effective_message, random.choice([
        "Oui.", "Non.", "Peut-être.", "Très probable.", "Demande-moi plus tard.", "Je refuse de me mouiller."
    ]))

async def choose(update, context):
    text = " ".join(context.args)
    parts = [x.strip() for x in re.split(r"\s*(?:\||/|,|;)\s*", text) if x.strip()]
    if len(parts) < 2:
        await safe_reply(update.effective_message, "Exemple : /choose manga | anime")
        return
    await safe_reply(update.effective_message, f"Je choisis : {random.choice(parts)}")

async def compliment(update, context):
    await safe_reply(update.effective_message, random.choice([
        "T'es pas mal, toi.", "Franchement, tu gères.", "Aujourd'hui, je valide ton énergie."
    ]))

async def roast(update, context):
    await safe_reply(update.effective_message, random.choice([
        "Je te taquine, mais t'es encore là. Respect.",
        "Même mon mode économie d'énergie a plus de batterie que toi.",
        "Je pourrais te roast… mais je vais rester gentille."
    ]))

async def motivate(update, context):
    await safe_reply(update.effective_message, random.choice([
        "Continue. Ton niveau n'est pas fini.",
        "Encore un effort. Tu peux le faire.",
        "On avance. Pas d'abandon."
    ]))

# ============================================================
# STATS / TABLES
# ============================================================
def table_lines(headers, rows, widths):
    out = ["  ".join(str(h).ljust(w) for h, w in zip(headers, widths))]
    out.append("  ".join("-" * w for w in widths))
    for row in rows:
        out.append("  ".join(str(v)[:w].ljust(w) for v, w in zip(row, widths)))
    return "\n".join(out)

def format_duration(seconds):
    seconds=int(seconds or 0)
    d,rem=divmod(seconds,86400); h,rem=divmod(rem,3600); m,s=divmod(rem,60)
    if d: return f"{d}j {h}h"
    if h: return f"{h}h {m}m"
    return f"{m}m {s}s"

async def _download_avatar(bot, user_id, fallback_seed):
    try:
        photos=await bot.get_user_profile_photos(user_id,limit=1)
        if photos.total_count:
            f=await photos.photos[0][-1].get_file(); bio=io.BytesIO(); await f.download_to_memory(out=bio); bio.seek(0); return bio
    except Exception: pass
    try:
        async with httpx.AsyncClient(timeout=15,follow_redirects=True) as client:
            r=await client.get(f"https://randomuser.me/api/?inc=picture&seed={quote(str(fallback_seed))}"); data=r.json(); url=data['results'][0]['picture']['large']; rr=await client.get(url); rr.raise_for_status(); return io.BytesIO(rr.content)
    except Exception: return None

async def send_modern_leaderboard(bot,chat_id,title,rows):
    if not rows: return
    if Image is None:
        await safe_send_message(bot,chat_id,title+"\n"+"\n".join(f"{i}. {r[1]} — {r[2]} pts" for i,r in enumerate(rows,1))); return
    W,H=1000,620; img=Image.new('RGB',(W,H),(18,20,28)); draw=ImageDraw.Draw(img)
    font_path='/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'; bold_path='/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
    title_font=ImageFont.truetype(bold_path,38); name_font=ImageFont.truetype(bold_path,24); small=ImageFont.truetype(font_path,18)
    draw.text((40,30),title,fill=(245,245,250),font=title_font)
    y=105
    for i,row in enumerate(rows[:5],1):
        uid,name,pts,*rest=row; draw.rounded_rectangle((30,y,W-30,y+88),18,fill=(31,34,46))
        avatar=await _download_avatar(bot,int(uid),name or uid)
        if avatar:
            try:
                av=Image.open(avatar).convert('RGB').resize((64,64)); mask=Image.new('L',(64,64),0); ImageDraw.Draw(mask).ellipse((0,0,64,64),fill=255); img.paste(av,(50,y+12),mask)
            except Exception: pass
        draw.text((135,y+15),f"#{i}  {str(name)[:25]}",fill=(255,255,255),font=name_font)
        draw.text((135,y+49),f"{int(pts)} points",fill=(170,175,190),font=small)
        y+=102
    out=io.BytesIO(); img.save(out,'PNG'); out.seek(0); await bot.send_photo(chat_id,photo=out,caption=title)

async def all_time_ranking_text(limit=20):
    con=db(); rows=con.execute("""SELECT user_id,COALESCE(MAX(name),''),SUM(points),SUM(wins),SUM(losses) FROM scores GROUP BY user_id ORDER BY SUM(points) DESC,SUM(wins) DESC LIMIT ?""",(limit,)).fetchall(); con.close()
    if not rows: return "🏆 Aucun joueur pour le moment."
    lines=["🏆 MEILLEURS JOUEURS — TOUS LES TEMPS"]
    for i,(uid,name,pts,wins,losses) in enumerate(rows,1): lines.append(f"{i}. {name or uid} — {pts} pts • {wins} victoires")
    return "\\n".join(lines)

async def alltime_cmd(update,context):
    con=db(); rows=con.execute("SELECT user_id,COALESCE(MAX(name),''),SUM(points),SUM(wins),SUM(losses) FROM scores GROUP BY user_id ORDER BY SUM(points) DESC,SUM(wins) DESC LIMIT 10").fetchall(); con.close()
    if not rows: await safe_reply(update.effective_message,"Aucun joueur pour le moment."); return
    await send_modern_leaderboard(context.bot,update.effective_chat.id,"🌍 MEILLEURS JOUEURS — TOUS LES TEMPS",rows)

async def stats(update, context):
    if not admin_ok(update):
        await safe_reply(update.effective_message, "Commande réservée à l'administration.")
        return
    con = db()
    users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    groups = con.execute("SELECT COUNT(*) FROM chats WHERE chat_type IN ('group','supergroup')").fetchone()[0]
    messages = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    private_chats = con.execute("SELECT COUNT(*) FROM chats WHERE chat_type='private'").fetchone()[0]
    channels = con.execute("SELECT COUNT(*) FROM chats WHERE chat_type='channel'").fetchone()[0]
    total_active = con.execute("SELECT COALESCE(SUM(active_seconds),0) FROM users").fetchone()[0]
    con.close()
    provider_lines=[]
    for name in ("mistral","deepseek","groq","gemini"):
        configured=bool({"mistral":MISTRAL_API_KEY,"deepseek":DEEPSEEK_API_KEY,"groq":GROQ_API_KEY,"gemini":GEMINI_API_KEY}.get(name))
        status="EN REPOS" if not provider_ready(name) else ("DISPONIBLE" if configured else "NON CONFIGURÉ")
        provider_lines.append(f"• {name.upper()} : {status} • utilisations {_PROVIDER_USED.get(name,0)}")
    await safe_reply(update.effective_message,
        f"📊 STATISTIQUES ADMIN\n\nDernière API utilisée : {_LAST_PROVIDER or "aucune"}\nUtilisateurs : {users}\nGroupes : {groups}\nChats privés : {private_chats}\nCanaux : {channels}\nMessages : {messages}\nTemps total : {format_duration(total_active)}\n\n🤖 API IA\n"+"\n".join(provider_lines))

async def users_ranking(update, context):
    if not admin_ok(update):
        await safe_reply(update.effective_message, "Commande réservée à l'administration.")
        return
    con = db()
    rows = con.execute("""
        SELECT first_name,username,messages,COALESCE(active_seconds,0),COALESCE(last_topic,'')
        FROM users ORDER BY messages DESC LIMIT 20
    """).fetchall()
    con.close()
    data = []
    for i, (first, username, messages, seconds, topic) in enumerate(rows, 1):
        name = first or ("@" + username if username else "?")
        data.append((i, name, messages, format_duration(seconds), topic[:28]))
    await safe_reply(update.effective_message,
        "```\n" + table_lines(["#", "UTILISATEUR", "MESSAGES", "TEMPS", "DERNIER SUJET"], data, [3,18,9,10,28]) + "\n```",
        parse_mode="Markdown"
    )

async def chats_ranking(update, context):
    if not admin_ok(update): return
    con=db(); rows=con.execute("SELECT first_name,username,messages,COALESCE(active_seconds,0),COALESCE(last_topic,'') FROM users ORDER BY last_seen DESC LIMIT 50").fetchall(); con.close()
    if not rows: await safe_reply(update.effective_message,"Aucun chat privé enregistré."); return
    lines=["💬 CHATS PRIVÉS ENREGISTRÉS"]
    for i,(first,username,msgs,secs,topic) in enumerate(rows,1): lines.append(f"{i}. {first or '@'+username if username else 'Utilisateur'} — {msgs} msg — {format_duration(secs)} — {topic[:45]}")
    await safe_reply(update.effective_message,"\n".join(lines[:51]))

async def groups_ranking(update, context):
    if not admin_ok(update): return
    con=db(); groups=con.execute("SELECT chat_id,title,username,messages,COALESCE(active_seconds,0) FROM chats WHERE chat_type IN ('group','supergroup') ORDER BY messages DESC LIMIT 30").fetchall(); con.close()
    if not groups: await safe_reply(update.effective_message,"Aucun groupe enregistré."); return
    lines=["👥 GROUPES ENREGISTRÉS"]
    for i,(cid,title,username,msgs,secs) in enumerate(groups,1):
        con=db(); top=con.execute("SELECT name,points FROM scores WHERE chat_id=? ORDER BY points DESC LIMIT 1",(cid,)).fetchone(); con.close()
        player=f"{top[0]} ({top[1]} pts)" if top else "aucun joueur"
        lines.append(f"{i}. {title or '@'+username if username else cid}\n   {msgs} messages • {format_duration(secs)}\n   🏆 Meilleur joueur : {player}")
    await safe_reply(update.effective_message,"\n".join(lines))

async def ranking_cmd(update, context):
    con=db(); rows=con.execute("SELECT user_id,name,points,wins,losses FROM scores WHERE chat_id=? ORDER BY points DESC,wins DESC LIMIT 10",(update.effective_chat.id,)).fetchall(); con.close()
    if not rows: await safe_reply(update.effective_message,"Le classement est vide."); return
    await send_modern_leaderboard(context.bot,update.effective_chat.id,"🏆 CLASSEMENT DU GROUPE",rows)

async def score_cmd(update, context):
    pts, wins, losses = get_score(update.effective_chat.id, update.effective_user.id)
    xp_points, level = get_xp(update.effective_user.id)
    await safe_reply(update.effective_message,
        f"🏆 {display_name(update.effective_user)}\n"
        f"Niveau : {level}\nXP : {xp_points}\n"
        f"Points : {pts}\nVictoires : {wins}\nDéfaites : {losses}"
    )

# ============================================================
# GROUP INFO
# ============================================================
async def groupinfo(update, context):
    c = update.effective_chat
    if not is_group(c):
        await safe_reply(update.effective_message, "Cette commande est pour les groupes.")
        return
    members = "inconnu"
    try:
        members = await context.bot.get_chat_member_count(c.id)
    except Exception:
        pass
    await safe_reply(update.effective_message,
        f"👥 {c.title or 'Groupe'}\n🆔 {c.id}\n👤 Membres : {members}"
    )

async def groupstats(update, context):
    c = update.effective_chat
    if not is_group(c):
        await safe_reply(update.effective_message, "Cette commande est pour les groupes.")
        return
    con = db()
    row = con.execute("SELECT messages FROM chats WHERE chat_id=?", (c.id,)).fetchone()
    con.close()
    await safe_reply(update.effective_message, f"📊 Messages enregistrés : {(row[0] if row else 0)}")

async def top_cmd(update, context):
    await ranking_cmd(update, context)

# ============================================================
# ADMIN + REWARDS + STICKERS
# ============================================================
async def admin(update, context):
    if not admin_ok(update):
        await safe_reply(update.effective_message, "Commande réservée à l'administration.")
        return
    await safe_reply(update.effective_message,
        "/stats /users /groups /user ID\n"
        "/broadcast message\n/broadcastgroups message\n"
        "/rewardlevels /rewarduser ID /rewarddone ID NIVEAU\n"
        "/addsticker [categorie] (en répondant à un autocollant)\n"
        "/stickers /delstickers ID\n"
        "/rss add URL /rss list /rss remove ID\n"
        "/alltime"
    )

async def user_cmd(update, context):
    if not admin_ok(update):
        return
    if not context.args:
        await safe_reply(update.effective_message, "Utilise /user ID")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await safe_reply(update.effective_message, "ID invalide.")
        return
    con = db()
    row = con.execute("SELECT first_name,username,messages,COALESCE(active_seconds,0),COALESCE(last_topic,'') FROM users WHERE user_id=?", (uid,)).fetchone()
    con.close()
    if not row:
        await safe_reply(update.effective_message, "Utilisateur introuvable.")
        return
    xp_points, level = get_xp(uid)
    await safe_reply(update.effective_message,
        f"👤 {row[0] or row[1] or uid}\nID : {uid}\nMessages : {row[2]}\n"
        f"Temps total : {format_duration(row[3])}\nDernier sujet : {row[4] or '—'}\n"
        f"Niveau : {level}\nXP : {xp_points}"
    )

async def rewardlevels(update, context):
    if not admin_ok(update):
        return
    con = db()
    rows = con.execute("SELECT level,reward_text,enabled FROM rewards ORDER BY level").fetchall()
    con.close()
    lines = ["🎁 NIVEAUX RÉCOMPENSES"]
    for level, reward, enabled in rows:
        lines.append(f"{level} → {reward} {'✓' if enabled else '✗'}")
    await safe_reply(update.effective_message, "\n".join(lines))

async def rewarduser(update, context):
    if not admin_ok(update):
        return
    if not context.args:
        await safe_reply(update.effective_message, "Utilise /rewarduser ID")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await safe_reply(update.effective_message, "ID invalide.")
        return
    await safe_send_message(context.bot, uid, "🎁 NEXA a une récompense pour toi. Contacte l'administration.")
    await safe_reply(update.effective_message, "Notification envoyée.")

async def rewarddone(update, context):
    if not admin_ok(update):
        return
    if len(context.args) < 2:
        await safe_reply(update.effective_message, "Utilise /rewarddone ID NIVEAU")
        return
    try:
        uid, level = int(context.args[0]), int(context.args[1])
    except ValueError:
        await safe_reply(update.effective_message, "Valeurs invalides.")
        return
    con = db()
    con.execute("UPDATE reward_claims SET done=1 WHERE user_id=? AND level=?", (uid,level))
    con.commit()
    con.close()
    await safe_reply(update.effective_message, "Récompense marquée comme envoyée.")

async def addsticker(update, context):
    if not admin_ok(update):
        return
    msg = update.effective_message
    reply = msg.reply_to_message
    if not reply or not getattr(reply, "sticker", None):
        await safe_reply(msg, "Réponds à un autocollant avec /addsticker [categorie]")
        return
    category = (context.args[0].lower() if context.args else "general")[:30]
    file_id = reply.sticker.file_id
    con = db()
    con.execute("INSERT OR IGNORE INTO admin_stickers(file_id,category,added_at) VALUES(?,?,?)",
                (file_id, category, now()))
    con.commit()
    con.close()
    await safe_reply(msg, f"Autocollant ajouté : {category}.")

async def addautocollants(update, context):
    if not admin_ok(update):
        return
    category = (context.args[0].lower() if context.args else "otaku")[:30]
    context.user_data["waiting_alicia_sticker"] = True
    context.user_data["waiting_alicia_sticker_category"] = category
    await safe_reply(update.effective_message, f"🎟️ Envoie maintenant l'autocollant du pack « {category} ».")

async def stickers_cmd(update, context):
    if not admin_ok(update):
        return
    con = db()
    rows = con.execute("SELECT id,category FROM admin_stickers ORDER BY id DESC").fetchall()
    con.close()
    if not rows:
        await safe_reply(update.effective_message, "Aucun autocollant enregistré.")
        return
    await safe_reply(update.effective_message, "\n".join(f"{i} — {c}" for i,c in rows))

async def delstickers(update, context):
    if not admin_ok(update):
        return
    if not context.args:
        await safe_reply(update.effective_message, "Utilise /delstickers ID")
        return
    try:
        sid = int(context.args[0])
    except ValueError:
        await safe_reply(update.effective_message, "ID invalide.")
        return
    con = db()
    con.execute("DELETE FROM admin_stickers WHERE id=?", (sid,))
    con.commit()
    con.close()
    await safe_reply(update.effective_message, "Autocollant supprimé.")

async def stars_stats(update, context):
    if not admin_ok(update): return
    con=db()
    total=con.execute("SELECT COALESCE(SUM(stars),0) FROM payments").fetchone()[0]
    rows=con.execute("SELECT user_id,stars,payload,created_at FROM payments ORDER BY id DESC LIMIT 10").fetchall()
    con.close()
    lines=[f"⭐ PAIEMENTS ENREGISTRÉS : {total} Stars"]
    for uid,stars,payload,created in rows:
        kind="Coding 24h" if str(payload).startswith("alicia_coding_24h:") else "Premium"
        lines.append(f"• {uid} — {stars} ⭐ — {kind}")
    await safe_reply(update.effective_message,"\n".join(lines))

async def premiumusers(update, context):
    if not admin_ok(update):
        return
    con = db()
    rows = con.execute("""
        SELECT p.user_id,u.first_name,u.username
        FROM premium p LEFT JOIN users u ON u.user_id=p.user_id
        WHERE p.active=1 ORDER BY p.granted_at DESC
    """).fetchall()
    con.close()
    if not rows:
        await safe_reply(update.effective_message, "Aucun Premium.")
        return
    lines = ["⭐ PREMIUM"]
    for uid, first, username in rows:
        lines.append(f"{first or username or uid} — {uid}")
    await safe_reply(update.effective_message, "\n".join(lines))

async def broadcast(update, context):
    if not admin_ok(update):
        return
    text = " ".join(context.args).strip()
    if not text:
        await safe_reply(update.effective_message, "Utilise /broadcast message")
        return
    con = db()
    ids = [r[0] for r in con.execute("SELECT user_id FROM users").fetchall()]
    con.close()
    sent = 0
    for uid in ids:
        try:
            await safe_send_message(context.bot, uid, text)
            sent += 1
        except Exception:
            pass
    await safe_reply(update.effective_message, f"Envoyé à {sent} utilisateurs.")

async def broadcastgroups(update, context):
    if not admin_ok(update):
        return
    text = " ".join(context.args).strip()
    if not text:
        await safe_reply(update.effective_message, "Utilise /broadcastgroups message")
        return
    con = db()
    ids = [r[0] for r in con.execute(
        "SELECT chat_id FROM chats WHERE chat_type IN ('group','supergroup')"
    ).fetchall()]
    con.close()
    sent = 0
    for cid in ids:
        try:
            await safe_send_message(context.bot, cid, text)
            sent += 1
        except Exception:
            pass
    await safe_reply(update.effective_message, f"Envoyé à {sent} groupes.")

# ============================================================
# ADMIN MEDIA BROADCAST
# ============================================================
async def _broadcast_replied_message(update,context,groups=False):
    if not admin_ok(update): return
    msg=update.effective_message; source=msg.reply_to_message
    if not source:
        await safe_reply(msg,"Réponds à une photo, vidéo, audio, document ou message avec cette commande."); return
    if groups:
        con=db(); ids=[r[0] for r in con.execute("SELECT chat_id FROM chats WHERE chat_type IN ('group','supergroup')").fetchall()]; con.close()
    else:
        con=db(); ids=[r[0] for r in con.execute("SELECT user_id FROM users").fetchall()]; con.close()
    sent=0
    for cid in ids:
        try:
            await context.bot.copy_message(chat_id=cid,from_chat_id=source.chat_id,message_id=source.message_id); sent+=1
        except Exception: pass
    await safe_reply(msg,f"📨 Média/message envoyé à {sent} {'groupes' if groups else 'utilisateurs'}.")

async def broadcastmedia(update,context): await _broadcast_replied_message(update,context,False)
async def broadcastgroupsmedia(update,context): await _broadcast_replied_message(update,context,True)

async def paysupport(update,context):
    await safe_reply(update.effective_message,"Pour un problème de paiement, écris à l'administration avec ton ID Telegram et la preuve de paiement.")

# VOCAL (OPTIONAL TTS)
# ============================================================
async def generate_voice(text):
    if not TTS_API_URL or not TTS_API_KEY:
        return None
    payload = {"text": text[:900], "voice": TTS_VOICE, "language": "fr"}
    headers = {"Authorization": f"Bearer {TTS_API_KEY}"}
    async with httpx.AsyncClient(timeout=45) as client:
        r = await client.post(TTS_API_URL, json=payload, headers=headers)
        r.raise_for_status()
        if not r.content:
            return None
        fd, path = tempfile.mkstemp(suffix=".ogg")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(r.content)
        return path

async def voice_cmd(update, context):
    text = " ".join(context.args).strip()
    if not text:
        text = "Salut, c'est Alicia. Mon père, c'est NEXA. À bientôt."
    path = await generate_voice(text)
    if not path:
        await safe_reply(update.effective_message,
            "🎙️ Le mode vocal est prêt dans le code, mais aucun service TTS n'est configuré."
        )
        return
    try:
        with open(path, "rb") as audio:
            await context.bot.send_voice(update.effective_chat.id, audio, reply_to_message_id=update.effective_message.message_id)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

# ============================================================
# REACTIONS / AUTOCOLLANTS
# ============================================================
async def maybe_sticker(bot, chat_id, category="general"):
    con = db()
    rows = con.execute(
        "SELECT file_id FROM admin_stickers WHERE category=? OR category='general' ORDER BY RANDOM() LIMIT 1",
        (category,)
    ).fetchall()
    con.close()
    if not rows:
        return
    try:
        await bot.send_sticker(chat_id=chat_id, sticker=rows[0][0])
    except Exception as e:
        log.warning("Sticker failed: %s", e)

async def react_to_message(bot, chat_id, message_id, emoji):
    """Ajoute une réaction Telegram sans casser le bot si l'API n'est pas disponible."""
    try:
        method = getattr(bot, "set_message_reaction", None)
        if method is None:
            return False
        await method(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[{"type": "emoji", "emoji": emoji}],
            is_big=False,
        )
        return True
    except Exception as e:
        log.debug("Reaction unavailable: %s", e)
        return False


def is_compliment(text):
    low = re.sub(r"[^a-zàâçéèêëîïôûùüÿñæœ0-9 ]+", " ", (text or "").lower()).strip()
    phrases = (
        "tu es trop belle", "tu es trop mignonne", "tu es magnifique",
        "tu es adorable", "tu es incroyable", "tu es parfaite",
        "tu es géniale", "tu es geniale", "tu es superbe", "tu es jolie",
        "j'aime ton", "j adore ton", "bravo alicia", "merci alicia",
        "bonne fille", "t'es incroyable", "t es incroyable",
        "t'es trop forte", "t es trop forte", "je t'aime alicia",
        "je t adore alicia", "je t'adore alicia",
    )
    words = {
        "belle", "beau", "jolie", "joli", "mignonne", "mignon",
        "magnifique", "adorable", "incroyable", "géniale", "geniale",
        "forte", "intelligente", "cute", "parfaite", "parfait",
        "splendide", "superbe", "sublime", "élégante", "elegante", "charmante",
    }
    return any(p in low for p in phrases) or any(w in low.split() for w in words)


async def sticker_handler(update, context):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not msg.sticker or not user or not chat:
        return

    register_user(user)
    register_chat(chat)

    # L'admin peut ajouter un autocollant directement après /addautocollants.
    if admin_ok(update) and context.user_data.get("waiting_alicia_sticker"):
        category = (context.user_data.pop("waiting_alicia_sticker_category", None) or "otaku")[:30]
        context.user_data.pop("waiting_alicia_sticker", None)
        con = db()
        con.execute(
            "INSERT OR IGNORE INTO admin_stickers(file_id,category,added_at) VALUES(?,?,?)",
            (msg.sticker.file_id, category, now()),
        )
        con.commit()
        con.close()
        await safe_reply(msg, f"Autocollant ajouté au pack « {category} ». ")
        return

    # En groupe, elle répond uniquement si elle est appelée ou si quelqu'un répond à Alicia.
    if is_group(chat) and not called_alicia(update):
        return

    # Règle spéciale : autocollant reçu -> autocollant envoyé. Aucun texte.
    # On cherche d'abord le pack 'otaku', puis le pack général.
    await maybe_sticker(context.bot, chat.id, "otaku")

# ============================================================
# MEMBER WELCOME / GOODBYE
# ============================================================
async def member_update(update, context):
    cm = update.chat_member
    if not cm:
        return
    old, new = cm.old_chat_member.status, cm.new_chat_member.status
    user = cm.new_chat_member.user
    register_chat(cm.chat)
    if old in ("left", "kicked") and new in ("member", "restricted"):
        await safe_send_message(context.bot, cm.chat.id, f"Bienvenue {display_name(user)}. Installe-toi bien.")
    elif old in ("member", "restricted") and new in ("left", "kicked"):
        await safe_send_message(context.bot, cm.chat.id, f"{display_name(user)} est parti. À bientôt.")

# ============================================================
# RSS — user feeds to private chats, groups and channels
# ============================================================
def rss_parse(xml_bytes):
    root=ET.fromstring(xml_bytes); items=[]
    for node in list(root):
        tag=node.tag.lower().split('}')[-1]
        if tag not in ('channel','feed'): continue
        for item in list(node):
            itag=item.tag.lower().split('}')[-1]
            if itag not in ('item','entry'): continue
            vals={}
            for child in list(item):
                k=child.tag.lower().split('}')[-1]; txt=''.join(child.itertext()).strip() if child is not None else ''
                if k=='link' and not txt: txt=child.attrib.get('href','')
                vals[k]=txt
            title=vals.get('title','').strip(); link=vals.get('link','').strip(); key=vals.get('guid') or vals.get('id') or link or title
            if title: items.append((key,title,link,vals.get('pubdate') or vals.get('published') or vals.get('updated') or ''))
    return items[:30]

async def rss_fetch(url):
    async with httpx.AsyncClient(timeout=25,follow_redirects=True,headers={'User-Agent':'AliciaRSS/1.0'}) as client:
        r=await client.get(url); r.raise_for_status(); return rss_parse(r.content)

def _rss_source_type(value):
    value = (value or "").lower().strip()
    aliases = {
        "site": "site",
        "web": "site",
        "website": "site",
        "facebook": "facebook",
        "fb": "facebook",
        "twitter": "twitter",
        "x": "twitter",
    }
    return aliases.get(value)

async def _rss_user_is_admin(update, bot, chat_id=None):
    user = update.effective_user
    if not user:
        return False
    target_id = chat_id if chat_id is not None else update.effective_chat.id
    try:
        member = await bot.get_chat_member(target_id, user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False

async def _rss_resolve_destination(update, context, value=None):
    """Résout un canal/groupe cible et vérifie que le bot peut y publier."""
    if not value:
        chat = update.effective_chat
    else:
        raw = str(value).strip()
        if raw.lstrip("-").isdigit():
            target_id = int(raw)
        else:
            target_id = raw if raw.startswith("@") else "@" + raw
        try:
            chat = await context.bot.get_chat(target_id)
        except Exception as e:
            raise RuntimeError("Canal introuvable. Vérifie le @username et assure-toi qu'Alicia est présente dans le canal.") from e

    if chat.type not in (ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP):
        raise RuntimeError("La destination doit être un canal ou un groupe Telegram.")

    # Le bot doit pouvoir publier.
    try:
        bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
        if chat.type == ChatType.CHANNEL:
            if bot_member.status not in ("administrator", "creator"):
                raise RuntimeError("Alicia doit être administratrice du canal pour publier les flux.")
            if hasattr(bot_member, "can_post_messages") and bot_member.can_post_messages is False:
                raise RuntimeError("Alicia n'a pas la permission de publier dans ce canal.")
        elif bot_member.status not in ("administrator", "creator", "member"):
            raise RuntimeError("Alicia n'a pas accès à cette destination.")
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError("Impossible de vérifier les permissions de publication de la destination.") from e

    return chat

def _rss_save_destination(owner_user_id, chat):
    con = db()
    con.execute("""
        INSERT INTO rss_destinations(owner_user_id,chat_id,title,username,chat_type,active,created_at)
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(chat_id) DO UPDATE SET
            owner_user_id=excluded.owner_user_id,
            title=excluded.title,
            username=excluded.username,
            chat_type=excluded.chat_type,
            active=1
    """, (
        owner_user_id, chat.id, getattr(chat, "title", "") or "",
        getattr(chat, "username", "") or "", chat.type, 1, now()
    ))
    con.commit()
    con.close()

async def rss_cmd(update,context):
    chat = update.effective_chat
    user = update.effective_user
    uid = user.id if user else ADMIN_USER_ID
    if user:
        register_user(user)
    register_chat(chat)

    args = context.args or []

    if not args:
        await safe_reply(
            update.effective_message,
            "📡 FLUX RSS\n\n"
            "/rss add site URL — source d'un site web\n"
            "/rss add facebook URL — source Facebook via flux RSS compatible\n"
            "/rss add twitter URL — source Twitter/X via flux RSS compatible\n"
            "/rss channel @canal — ajouter une destination\n"
            "/rss channels — voir les destinations\n"
            "/rss list — voir les flux\n"
            "/rss route ID @canal — envoyer un flux vers un canal\n"
            "/rss remove ID — retirer un flux de cette destination\n\n"
            "Une URL Facebook/Twitter/X doit fournir un flux RSS/Atom exploitable."
        )
        return

    action = args[0].lower()

    # Ajouter une destination.
    if action == "channel":
        if not await _rss_user_is_admin(update, context.bot):
            await safe_reply(update.effective_message, "Seuls les administrateurs peuvent gérer les destinations RSS.")
            return
        target_value = args[1] if len(args) >= 2 else None
        try:
            target = await _rss_resolve_destination(update, context, target_value)
            _rss_save_destination(uid, target)
        except Exception as e:
            await safe_reply(update.effective_message, f"❌ {e}")
            return
        label = getattr(target, "username", "") or getattr(target, "title", "") or str(target.id)
        await safe_reply(update.effective_message, f"✅ Destination RSS ajoutée : {label}\nID : {target.id}")
        return

    if action in ("channels", "destinations"):
        con = db()
        rows = con.execute("""
            SELECT id,chat_id,title,username,chat_type
            FROM rss_destinations
            WHERE active=1
            ORDER BY id
        """).fetchall()
        con.close()
        if not rows:
            await safe_reply(update.effective_message, "Aucune destination RSS enregistrée.")
            return
        lines = ["📢 DESTINATIONS RSS", ""]
        for rid, cid, title, username, ctype in rows:
            label = f"@{username}" if username else (title or str(cid))
            lines.append(f"#{rid} — {label} ({ctype})\nID : {cid}")
        await safe_reply(update.effective_message, "\n".join(lines))
        return

    # Ajouter un flux.
    if action == "add" and len(args) >= 2:
        source_type = "site"
        url_index = 1
        candidate_type = _rss_source_type(args[1])
        if candidate_type:
            source_type = candidate_type
            url_index = 2

        if len(args) <= url_index:
            await safe_reply(
                update.effective_message,
                "Utilise : /rss add site URL\nou /rss add facebook URL\nou /rss add twitter URL"
            )
            return

        url = args[url_index].strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            await safe_reply(update.effective_message, "URL invalide.")
            return

        try:
            items = await rss_fetch(url)
            if not items:
                raise RuntimeError(
                    "Cette URL ne contient pas de flux RSS/Atom exploitable. "
                    "Pour Facebook/Twitter/X, utilise l'URL RSS/Atom ou le flux compatible fourni par ton service de flux."
                )
        except Exception as e:
            await safe_reply(update.effective_message, f"❌ Flux inaccessible : {e}")
            return

        # Destination facultative : /rss add site URL @canal
        destination_value = args[url_index + 1] if len(args) > url_index + 1 else None
        try:
            target = await _rss_resolve_destination(update, context, destination_value)
        except Exception as e:
            await safe_reply(update.effective_message, f"❌ Destination : {e}")
            return

        # Si on vise un autre canal, seul un admin de la destination peut créer la route.
        if destination_value and target.id != chat.id:
            if not await _rss_user_is_admin(update, context.bot, target.id):
                await safe_reply(update.effective_message, "Tu dois être administrateur de la destination pour y ajouter un flux.")
                return

        con = db()
        con.execute("""
            INSERT OR IGNORE INTO rss_feeds(
                owner_user_id,url,title,active,created_at,source_type,source_name
            ) VALUES(?,?,?,?,?,?,?)
        """, (
            uid, url, parsed.netloc, 1, now(), source_type,
            source_type.capitalize()
        ))
        feed_row = con.execute(
            "SELECT id FROM rss_feeds WHERE url=?",
            (url,)
        ).fetchone()
        if not feed_row:
            con.close()
            await safe_reply(update.effective_message, "❌ Impossible d'enregistrer le flux.")
            return

        feed_id = feed_row[0]
        con.execute("""
            INSERT OR REPLACE INTO rss_routes(
                feed_id,destination_chat_id,owner_user_id,active,created_at
            ) VALUES(?,?,?,?,?)
        """, (feed_id, target.id, uid, 1, now()))
        con.commit()
        con.close()

        _rss_save_destination(uid, target)
        label = getattr(target, "username", "") or getattr(target, "title", "") or str(target.id)
        await safe_reply(
            update.effective_message,
            f"✅ Flux {source_type} activé.\n"
            f"Source : {url}\n"
            f"Destination : {label}\n"
            f"{len(items)} articles détectés."
        )
        return

    # Relier un flux déjà existant à une autre destination.
    if action == "route" and len(args) >= 3:
        if not await _rss_user_is_admin(update, context.bot):
            await safe_reply(update.effective_message, "Seuls les administrateurs peuvent gérer les routes RSS.")
            return
        try:
            feed_id = int(args[1])
        except ValueError:
            await safe_reply(update.effective_message, "ID de flux invalide.")
            return
        try:
            target = await _rss_resolve_destination(update, context, args[2])
        except Exception as e:
            await safe_reply(update.effective_message, f"❌ {e}")
            return
        if not await _rss_user_is_admin(update, context.bot, target.id):
            await safe_reply(update.effective_message, "Tu dois être administrateur de la destination.")
            return
        con = db()
        exists = con.execute("SELECT id FROM rss_feeds WHERE id=? AND active=1", (feed_id,)).fetchone()
        if not exists:
            con.close()
            await safe_reply(update.effective_message, "Flux introuvable.")
            return
        con.execute("""
            INSERT OR REPLACE INTO rss_routes(feed_id,destination_chat_id,owner_user_id,active,created_at)
            VALUES(?,?,?,?,?)
        """, (feed_id, target.id, uid, 1, now()))
        con.commit()
        con.close()
        _rss_save_destination(uid, target)
        await safe_reply(update.effective_message, f"✅ Flux #{feed_id} relié à {getattr(target,'username','') or getattr(target,'title','') or target.id}.")
        return

    con = db()

    if action in ("list", "sources", "feeds"):
        rows = con.execute("""
            SELECT f.id,f.title,f.url,
                   COALESCE(f.source_type,'site'),
                   r.destination_chat_id,
                   COALESCE(d.title,''),
                   COALESCE(d.username,'')
            FROM rss_feeds f
            JOIN rss_routes r ON r.feed_id=f.id
            LEFT JOIN rss_destinations d ON d.chat_id=r.destination_chat_id
            WHERE r.active=1 AND f.active=1
            ORDER BY f.id
        """).fetchall()
        con.close()

        if not rows:
            await safe_reply(update.effective_message, "Aucun flux actif.")
            return

        lines = ["📡 FLUX ACTIFS", ""]
        for fid, title, url, source_type, dest_id, dest_title, dest_username in rows:
            destination = f"@{dest_username}" if dest_username else (dest_title or str(dest_id))
            lines.append(
                f"#{fid} — {source_type}\n"
                f"{url}\n"
                f"→ {destination}"
            )
        await safe_reply(update.effective_message, "\n".join(lines))
        return

    if action == "remove" and len(args) >= 2:
        try:
            feed_id = int(args[1])
        except ValueError:
            con.close()
            await safe_reply(update.effective_message, "ID invalide.")
            return

        con.execute(
            "UPDATE rss_routes SET active=0 WHERE feed_id=? AND destination_chat_id=?",
            (feed_id, chat.id)
        )
        con.commit()
        con.close()
        await safe_reply(update.effective_message, "✅ Flux retiré de cette destination.")
        return

    con.close()
    await safe_reply(
        update.effective_message,
        "Commande RSS inconnue.\nUtilise /rss pour voir les commandes."
    )

async def rss_broadcast_once(app):
    con = db()
    feeds = con.execute(
        "SELECT id,url FROM rss_feeds WHERE active=1"
    ).fetchall()
    con.close()

    for fid, url in feeds:
        try:
            items = await rss_fetch(url)
        except Exception as e:
            log.warning("RSS %s: %s", url, e)
            continue

        for key, title, link, pub in reversed(items):
            con = db()
            exists = con.execute(
                "SELECT 1 FROM rss_items WHERE feed_id=? AND item_key=?",
                (fid, key)
            ).fetchone()

            if exists:
                con.close()
                continue

            con.execute("""
                INSERT OR IGNORE INTO rss_items(
                    feed_id,item_key,title,link,published_at,created_at
                ) VALUES(?,?,?,?,?,?)
            """, (fid, key, title, link, pub, now()))

            routes = con.execute("""
                SELECT destination_chat_id
                FROM rss_routes
                WHERE feed_id=? AND active=1
            """, (fid,)).fetchall()

            con.commit()
            con.close()

            text = f"📰 {title}\n{link}" if link else f"📰 {title}"

            for (dest,) in routes:
                try:
                    await safe_send_message(app.bot, dest, text)
                except Exception as e:
                    log.warning("RSS send failed feed=%s destination=%s: %s", fid, dest, e)

            await asyncio.sleep(0.1)

async def rss_scheduler(app):
    await asyncio.sleep(120)
    while True:
        try:
            await rss_broadcast_once(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("RSS scheduler error")
        await asyncio.sleep(300)

# ============================================================
# SOCIAL PERSONA / NEW FEATURES
# ============================================================
ALICIA_BOYFRIEND_USERNAME = "@exauce_senpai"
ALICIA_BOYFRIEND_NAME = "Exaucé"
ALICIA_SOCIAL_NOTE = "Alicia a des amis, des copines et des potes. Son seul petit ami est Exaucé."

async def friends_cmd(update, context):
    con=db()
    rows=con.execute("SELECT relation_type,note FROM social_relations WHERE user_id<>0 ORDER BY relation_type,note").fetchall()
    con.close()
    lines=["👥 Entourage d'Alicia", "", "Alicia a ses amis, ses copines et ses potes.", f"❤️ Son seul petit ami : {ALICIA_BOYFRIEND_NAME} ({ALICIA_BOYFRIEND_USERNAME})"]
    if rows:
        lines.append("")
        for rel,note in rows[:20]: lines.append(f"• {rel} : {note}")
    await safe_reply(update.effective_message,"\n".join(lines))

async def admin_friendadd(update, context):
    if not admin_ok(update): return
    if len(context.args)<3:
        await safe_reply(update.effective_message,"Utilise /friendadd type nom description")
        return
    rel=context.args[0].lower()[:30]; name=context.args[1][:80]; note=" ".join(context.args[2:])[:300]
    con=db(); con.execute("INSERT OR REPLACE INTO social_relations(user_id,relation_type,note,created_at) VALUES(?,?,?,?)",(-abs(hash(name))%10**12,rel,f"{name} — {note}",now())); con.commit(); con.close()
    await safe_reply(update.effective_message,"Relation ajoutée à l'entourage d'Alicia.")

def _current_mood():
    con=db(); row=con.execute("SELECT mood FROM mood_state WHERE id=1").fetchone(); con.close(); return row[0] if row else "calme"

def _set_mood(mood):
    con=db(); con.execute("INSERT OR REPLACE INTO mood_state(id,mood,updated_at) VALUES(1,?,?)",(mood,now())); con.commit(); con.close()

def natural_reaction(text):
    low=(text or "").lower()
    if any(x in low for x in ("merci", "thanks", "thx")): return random.choice(["Avec plaisir.","De rien.","T'inquiète.","Pas de souci."])
    if any(x in low for x in ("mdr","lol","haha","😂")): return random.choice(["😂", "mdrr", "J'avoue.", "Tu m'as fait rire."])
    if any(x in low for x in ("bonne nuit","dors bien")): return random.choice(["Bonne nuit.","Dors bien.","À demain."])
    if any(x in low for x in ("je suis triste","ça va mal","je vais mal")): return random.choice(["Je suis là.","Raconte-moi si tu veux.","Je t'écoute."])
    return None

async def save_memory_summary(user_id, text):
    topic=_topic_from_text(text)
    con=db(); con.execute("INSERT OR REPLACE INTO user_memory(user_id,summary,updated_at) VALUES(?,?,?)",(user_id,topic,now())); con.commit(); con.close()

async def reminder_loop(app):
    while True:
        try:
            ts=int(time.time()); con=db(); rows=con.execute("SELECT id,user_id,chat_id,text FROM reminders WHERE done=0 AND remind_at<=? ORDER BY id LIMIT 50",(ts,)).fetchall()
            for rid,uid,cid,text in rows:
                try: await safe_send_message(app.bot,cid,f"⏰ Rappel : {text}")
                except Exception: pass
                con.execute("UPDATE reminders SET done=1 WHERE id=?",(rid,))
            con.commit(); con.close()
        except Exception: log.exception("Reminder loop")
        await asyncio.sleep(30)

async def remind_cmd(update, context):
    if len(context.args)<2:
        await safe_reply(update.effective_message,"Utilise /remind 10m ton rappel, ou /remind 2h ton rappel.")
        return
    raw=context.args[0].lower(); m=re.fullmatch(r"(\d+)(m|h|d)",raw)
    if not m:
        await safe_reply(update.effective_message,"Format : 10m, 2h ou 1d."); return
    amount=int(m.group(1)); unit=m.group(2); seconds=amount*(60 if unit=='m' else 3600 if unit=='h' else 86400)
    text=" ".join(context.args[1:])[:500]; when=int(time.time())+seconds
    con=db(); con.execute("INSERT INTO reminders(user_id,chat_id,remind_at,text,created_at) VALUES(?,?,?,?,?)",(update.effective_user.id,update.effective_chat.id,when,text,now())); con.commit(); con.close()
    await safe_reply(update.effective_message,f"⏰ D'accord, je te rappelle ça dans {raw}.")

async def weather_cmd(update, context):
    city=" ".join(context.args).strip() or "Pointe-Noire"
    try:
        async with httpx.AsyncClient(timeout=10,follow_redirects=True) as c:
            r=await c.get(f"https://wttr.in/{quote(city)}",params={"format":"%l : %c %t, ressenti %f, vent %w"})
            await safe_reply(update.effective_message,r.text[:500] if r.status_code==200 else "Je n'arrive pas à récupérer la météo.")
    except Exception: await safe_reply(update.effective_message,"Je n'arrive pas à récupérer la météo pour le moment.")

async def websearch_cmd(update, context):
    query=" ".join(context.args).strip()
    if not query:
        await safe_reply(update.effective_message,"Utilise /search ta recherche."); return
    try:
        async with httpx.AsyncClient(timeout=12,follow_redirects=True,headers={"User-Agent":"AliciaBot/1.0"}) as c:
            r=await c.get("https://html.duckduckgo.com/html/",params={"q":query})
            text=re.sub(r"<[^>]+>"," ",r.text); text=re.sub(r"\s+"," ",html_lib.unescape(text))
            await safe_reply(update.effective_message,"🔎 Résultats :\n"+text[:1800])
    except Exception: await safe_reply(update.effective_message,"Recherche indisponible pour le moment.")

async def translate_cmd(update, context):
    if len(context.args)<2:
        await safe_reply(update.effective_message,"Utilise /translate fr Bonjour tout le monde"); return
    lang=context.args[0]; text=" ".join(context.args[1:])
    prompt=f"Traduis exactement ce texte vers {lang}. Réponds uniquement avec la traduction : {text}"
    reply=await ask_ai(update.effective_chat.id,update.effective_user.id,prompt)
    await safe_reply(update.effective_message,reply)

async def time_cmd(update, context):
    city=" ".join(context.args).strip() or "Pointe-Noire"
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r=await c.get(f"https://worldtimeapi.org/api/timezone/{quote(city)}")
            if r.status_code!=200: raise RuntimeError()
            d=r.json(); await safe_reply(update.effective_message,f"🕒 {city}\n{d.get('datetime','')}")
    except Exception: await safe_reply(update.effective_message,"Donne-moi un fuseau valide, par exemple /time Africa/Brazzaville")

async def convert_cmd(update, context):
    if len(context.args)!=2:
        await safe_reply(update.effective_message,"Utilise /convert 10km ou /convert 5usd"); return
    raw=context.args[0].lower(); target=context.args[1].lower()
    m=re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([a-z]+)",raw)
    if not m: await safe_reply(update.effective_message,"Format invalide."); return
    val=float(m.group(1)); unit=m.group(2)
    factors={"km":("mi",0.621371),"mi":("km",1.60934),"kg":("lb",2.20462),"lb":("kg",0.453592),"c":("f",lambda x:x*9/5+32),"f":("c",lambda x:(x-32)*5/9)}
    if unit not in factors: await safe_reply(update.effective_message,"Conversion disponible : km/mi, kg/lb, C/F."); return
    dest,fac=factors[unit]; out=fac(val) if callable(fac) else val*fac
    await safe_reply(update.effective_message,f"{val:g} {unit} = {out:.2f} {dest}.")

async def group_dashboard(update, context):
    chat=update.effective_chat
    if not is_group(chat): await safe_reply(update.effective_message,"Cette commande est prévue pour les groupes."); return
    con=db(); u=con.execute("SELECT COUNT(*) FROM messages WHERE chat_id=?",(chat.id,)).fetchone()[0]; players=con.execute("SELECT COUNT(*) FROM scores WHERE chat_id=?",(chat.id,)).fetchone()[0]; rows=con.execute("SELECT name,points,wins FROM scores WHERE chat_id=? ORDER BY points DESC LIMIT 3",(chat.id,)).fetchall(); con.close()
    lines=["🤖 ALICIA — TABLEAU DE BORD",f"👥 Groupe : {chat.title}",f"💬 Messages suivis : {u}",f"🎮 Joueurs : {players}","","🏆 Top du groupe"]
    lines += [f"{i}. {n} — {p} pts • {w} victoires" for i,(n,p,w) in enumerate(rows,1)] or ["Aucun joueur pour le moment."]
    await safe_reply(update.effective_message,"\n".join(lines))

async def badges_cmd(update, context):
    uid=update.effective_user.id; con=db(); rows=con.execute("SELECT badge FROM badges WHERE user_id=? ORDER BY created_at",(uid,)).fetchall(); con.close()
    await safe_reply(update.effective_message,"🏅 Tes badges\n"+"\n".join("• "+r[0] for r in rows) if rows else "🏅 Tu n'as pas encore de badge.")

async def tournament_cmd(update, context):
    if not is_group(update.effective_chat): await safe_reply(update.effective_message,"Crée un tournoi dans un groupe."); return
    action=context.args[0].lower() if context.args else "status"; chat=update.effective_chat
    con=db()
    if action=="create":
        title=" ".join(context.args[1:])[:100] or "Tournoi Alicia"; con.execute("INSERT INTO group_events(chat_id,event_type,title,created_at) VALUES(?,?,?,?)",(chat.id,"tournament",title,now())); con.commit(); await safe_reply(update.effective_message,f"🏆 Tournoi créé : {title}\nUtilise /tournament join pour participer.")
    elif action=="join":
        row=con.execute("SELECT id FROM group_events WHERE chat_id=? AND event_type='tournament' AND status='open' ORDER BY id DESC LIMIT 1",(chat.id,)).fetchone()
        if not row: con.close(); await safe_reply(update.effective_message,"Aucun tournoi ouvert."); return
        con.execute("INSERT OR IGNORE INTO event_players(event_id,user_id,joined_at) VALUES(?,?,?)",(row[0],update.effective_user.id,now())); con.commit(); await safe_reply(update.effective_message,"Tu es inscrit au tournoi.")
    else:
        row=con.execute("SELECT id,title FROM group_events WHERE chat_id=? AND event_type='tournament' AND status='open' ORDER BY id DESC LIMIT 1",(chat.id,)).fetchone()
        if not row: con.close(); await safe_reply(update.effective_message,"Aucun tournoi ouvert."); return
        players=con.execute("SELECT user_id,points,wins FROM event_players WHERE event_id=? ORDER BY points DESC,wins DESC",(row[0],)).fetchall(); con.close(); await safe_reply(update.effective_message,"🏆 "+row[1]+"\n"+"\n".join(f"{i}. {x[0]} — {x[1]} pts • {x[2]} victoires" for i,x in enumerate(players,1)) if players else "🏆 Aucun participant."); return
    con.close()

async def group_moderation_check(update, context):
    # Modération légère : enregistre les messages et avertit uniquement en cas de flood évident.
    msg=update.effective_message; chat=update.effective_chat; user=update.effective_user
    if not msg or not is_group(chat) or not msg.text: return False
    text=msg.text.strip()
    if len(text)>3000:
        return False
    return False

# ============================================================
# TEXT HANDLER
# ============================================================
QUICK = {
    "salut": "Salut.",
    "bonjour": "Bonjour.",
    "bonsoir": "Bonsoir.",
    "merci": "Avec plaisir.",
    "yo": "Yo.",
    "ok": "D'accord.",
    "ca va": "Oui, tranquille. Et toi ?",
    "ça va": "Oui, tranquille. Et toi ?",
}

async def text_handler(update, context):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not user or not chat:
        return
    text = (msg.text or "").strip()
    if not text:
        return
    register_user(user)
    register_chat(chat)
    media_url=extract_url(text)
    if media_url and media_url_supported(media_url):
        low_url=text.lower()
        if not is_group(chat) or called_alicia(update) or any(x in low_url for x in ("youtube","youtu.be","tiktok","instagram","facebook","twitter","x.com")):
            kind="audio" if any(x in low_url for x in ("song","music","audio")) else "video"
            if text.strip()==media_url:
                await send_downloaded_media(update,context,media_url,kind); return

    if is_group(chat) and not called_alicia(update):
        return

    record_message(chat, user, text)
    await save_memory_summary(user.id, text)
    if await chess_move(update,context,text): return
    if not is_group(chat):
        pass
    low = re.sub(r"[^a-zàâçéèêëîïôûùüÿñæœ0-9 ]+", " ", text.lower()).strip()

    # Very short local responses save API usage.
    if low in QUICK:
        reply = QUICK[low]
    else:
        await safe_chat_action(context.bot, chat.id)
        reply = await ask_ai(chat.id, user.id, text)

    save_ai_message(chat.id, user.id, reply)

    # Réactions Telegram occasionnelles, jamais à chaque réponse.
    if is_compliment(text) and random.random() < 0.35:
        await react_to_message(
            context.bot,
            chat.id,
            msg.message_id,
            random.choice(["❤️", "🥰", "😍", "🤭", "😊"]),
        )

    await safe_reply(msg, reply)

    # Rare reaction: emojis are used sparingly, stickers only if admin added them.
    if is_group(chat) and random.random() < 0.04:
        if any(x in low for x in ("mdr", "drôle", "haha", "lol")):
            await maybe_sticker(context.bot, chat.id, "funny")
        elif any(x in low for x in ("triste", "pleure", "😭")):
            await maybe_sticker(context.bot, chat.id, "sad")

async def quiznow(update, context):
    if not admin_ok(update):
        await safe_reply(update.effective_message, "Commande réservée à l'administration.")
        return
    await safe_reply(update.effective_message, "🧠 Je lance un quiz aléatoire dans les groupes enregistrés…")
    await quiz_broadcast_once(context.application)

# ============================================================
# MENU COMMANDS
# ============================================================
PUBLIC_COMMANDS = [
    ("start","Démarrer Alicia"),("help","Commandes"),("faq","FAQ"),("about","À propos"),("profile","Mon profil"),
    ("id","Mon ID"),("reset","Voir ma mémoire"),("clear","Voir ma mémoire"),("mood","Humeur d'Alicia"),("ask","Poser une question"),
    ("games","Jeux"),("challenge","Défier un joueur"),("accept","Accepter un défi"),("score","Mon score"),("ranking","Classement"),("alltime","Meilleurs joueurs"),
    ("referral","Mon parrainage"),("download","Télécharger une vidéo"),("video","Télécharger vidéo"),("song","Télécharger chanson"),
    ("guess","Deviner un nombre"),("joke","Blague"),("quote","Citation"),("coin","Pile ou face"),("8ball","Boule magique"),
    ("choose","Choisir"),("compliment","Compliment"),("roast","Taquiner"),("motivate","Motivation"),("groupinfo","Infos du groupe"),
    ("groupstats","Stats du groupe"),("dashboard","Tableau de bord"),("top","Top du groupe"),("alltime","Meilleurs joueurs"),("badges","Mes badges"),("friends","Entourage d'Alicia"),("tournament","Tournoi"),("remind","Rappel"),("weather","Météo"),("time","Heure"),("convert","Conversion"),("search","Recherche web"),("translate","Traduction"),("voice","Vocal Alicia"),("rss","Flux RSS : sites, Facebook, Twitter/X, canaux")
]

ADMIN_COMMANDS = PUBLIC_COMMANDS + [
    ("quiznow", "Lancer un quiz aléatoire"),("rss", "Gérer les flux RSS"),
    ("admin","Administration"),("stats","Statistiques"),("users","Utilisateurs"),("groups","Groupes"),("chats","Chats privés"),("user","Utilisateur"),
    ("broadcast","Message utilisateurs"),("broadcastgroups","Message groupes"),("broadcastmedia","Photo/message utilisateurs"),
    ("broadcastgroupsmedia","Photo/message groupes"),("rewardlevels","Niveaux récompenses"),("rewarduser","Récompense utilisateur"),
    ("rewarddone","Récompense envoyée"),("addsticker","Ajouter autocollant"),("addautocollants","Ajouter un autocollant"),
    ("stickers","Liste autocollants"),("delstickers","Supprimer autocollant"),("friendadd","Ajouter un ami au personnage")
]

async def _set_commands_call(bot, commands, scope):
    for attempt in range(5):
        try:
            await bot.set_my_commands(commands, scope=scope); return
        except RetryAfter as e:
            await asyncio.sleep(max(2,int(getattr(e,"retry_after",1))+2))
    raise RuntimeError("Telegram limite les appels set_my_commands après plusieurs tentatives.")

async def set_commands(app):
    commands=[BotCommand(a,b) for a,b in PUBLIC_COMMANDS]
    await _set_commands_call(app.bot,commands,BotCommandScopeAllPrivateChats())
    await _set_commands_call(app.bot,commands,BotCommandScopeAllGroupChats())
    if ADMIN_USER_ID:
        try: await _set_commands_call(app.bot,[BotCommand(a,b) for a,b in ADMIN_COMMANDS],BotCommandScopeChat(chat_id=ADMIN_USER_ID))
        except Exception as e: log.warning("Admin command menu failed: %s",e)

# ============================================================
# BUILD APP
# ============================================================
async def post_init(app):
    global QUIZ_TASK, RSS_TASK
    init_db()
    await set_commands(app)
    if QUIZ_TASK is None or QUIZ_TASK.done(): QUIZ_TASK = asyncio.create_task(quiz_scheduler(app))
    if RSS_TASK is None or RSS_TASK.done(): RSS_TASK = asyncio.create_task(rss_scheduler(app))
    asyncio.create_task(reminder_loop(app))

async def error_handler(update, context):
    log.exception("Unhandled error", exc_info=context.error)

def render_url():
    if RENDER_EXTERNAL_URL:
        return RENDER_EXTERNAL_URL.rstrip("/")
    host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
    return f"https://{host}" if host else ""

def build_app():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN manquant.")
    init_db()
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    # Public
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("quiz", quiz_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("faq", faq_cmd))
    app.add_handler(CommandHandler("about", about))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("mood", mood_cmd))
    app.add_handler(CommandHandler("ask", ask_cmd))
    app.add_handler(CommandHandler("games", games))
    app.add_handler(CommandHandler("challenge", challenge))
    app.add_handler(CommandHandler("accept", accept))
    app.add_handler(CommandHandler("score", score_cmd))
    app.add_handler(CommandHandler("ranking", ranking_cmd))
    app.add_handler(CommandHandler("alltime", alltime_cmd))
    app.add_handler(CommandHandler("rss", rss_cmd))
    app.add_handler(CommandHandler("referral", referral_cmd))
    app.add_handler(CommandHandler("download", download_cmd))
    app.add_handler(CommandHandler("video", video_cmd))
    app.add_handler(CommandHandler("song", song_cmd))
    app.add_handler(CommandHandler("guess", guess_cmd))
    app.add_handler(CommandHandler("joke", joke))
    app.add_handler(CommandHandler("quote", quote))
    app.add_handler(CommandHandler("coin", coin))
    app.add_handler(CommandHandler("8ball", eightball))
    app.add_handler(CommandHandler("choose", choose))
    app.add_handler(CommandHandler("compliment", compliment))
    app.add_handler(CommandHandler("roast", roast))
    app.add_handler(CommandHandler("motivate", motivate))
    app.add_handler(CommandHandler("groupinfo", groupinfo))
    app.add_handler(CommandHandler("groupstats", groupstats))
    app.add_handler(CommandHandler("dashboard", group_dashboard))
    app.add_handler(CommandHandler("badges", badges_cmd))
    app.add_handler(CommandHandler("friends", friends_cmd))
    app.add_handler(CommandHandler("tournament", tournament_cmd))
    app.add_handler(CommandHandler("remind", remind_cmd))
    app.add_handler(CommandHandler("weather", weather_cmd))
    app.add_handler(CommandHandler("time", time_cmd))
    app.add_handler(CommandHandler("convert", convert_cmd))
    app.add_handler(CommandHandler("search", websearch_cmd))
    app.add_handler(CommandHandler("translate", translate_cmd))
    app.add_handler(CommandHandler("top", top_cmd))
    app.add_handler(CommandHandler("voice", voice_cmd))
    app.add_handler(PollAnswerHandler(quiz_answer_handler))

    # Admin
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("users", users_ranking))
    app.add_handler(CommandHandler("groups", groups_ranking))
    app.add_handler(CommandHandler("chats", chats_ranking))
    app.add_handler(CommandHandler("user", user_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("broadcastgroups", broadcastgroups))
    app.add_handler(CommandHandler("broadcastmedia", broadcastmedia))
    app.add_handler(CommandHandler("broadcastgroupsmedia", broadcastgroupsmedia))
    app.add_handler(CommandHandler("rewardlevels", rewardlevels))
    app.add_handler(CommandHandler("rewarduser", rewarduser))
    app.add_handler(CommandHandler("rewarddone", rewarddone))
    app.add_handler(CommandHandler("addsticker", addsticker))
    app.add_handler(CommandHandler("addautocollants", addautocollants))
    app.add_handler(CommandHandler("stickers", stickers_cmd))
    app.add_handler(CommandHandler("delstickers", delstickers))
    app.add_handler(CommandHandler("friendadd", admin_friendadd))
    app.add_handler(CommandHandler("quiznow", quiznow))

    app.add_handler(CallbackQueryHandler(mini_game_callback, pattern=r"^mini:"))
    app.add_handler(CallbackQueryHandler(ludo_callback, pattern=r"^ludo:"))
    app.add_handler(CallbackQueryHandler(ttt_callback, pattern=r"^ttt:"))
    app.add_handler(CallbackQueryHandler(c4_callback, pattern=r"^c4:"))
    app.add_handler(CallbackQueryHandler(game_callback, pattern=r"^(menu:|game:)"))
    app.add_handler(ChatMemberHandler(member_update, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.Sticker.ALL, sticker_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(error_handler)
    return app

# ============================================================
# MAIN / RENDER WEBHOOK
# ============================================================
async def main():
    app = build_app()
    url = render_url()
    if url:
        webhook_url = f"{url}/telegram"
        log.info("Mode Render Webhook")
        log.info("URL Render: %s", url)
        log.info("Webhook: %s", webhook_url)
        await app.initialize()
        await post_init(app)
        # start_webhook() configure lui-même le webhook.
        # Ne pas appeler delete_webhook()/set_webhook() juste avant :
        # plusieurs appels rapprochés peuvent déclencher Telegram Flood control.
        for attempt in range(6):
            try:
                await app.updater.start_webhook(
                    listen="0.0.0.0", port=PORT, url_path="telegram",
                    webhook_url=webhook_url, drop_pending_updates=True,
                )
                break
            except RetryAfter as e:
                wait=max(2,int(getattr(e,"retry_after",1))+2)
                log.warning("Telegram flood control webhook: attente %ss (tentative %s/6)",wait,attempt+1)
                try: await app.updater.stop()
                except Exception: pass
                await asyncio.sleep(wait)
        else:
            raise RuntimeError("Impossible de configurer le webhook Telegram après plusieurs tentatives.")
        await app.start()
        log.info("Alicia online on port %s.", PORT)
        try:
            await asyncio.Event().wait()
        finally:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
    else:
        log.info("Mode polling")
        await app.initialize()
        await post_init(app)
        await app.updater.start_polling(drop_pending_updates=True)
        await app.start()
        try:
            await asyncio.Event().wait()
        finally:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())