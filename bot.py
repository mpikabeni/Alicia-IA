
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
import html
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime, timezone, timedelta, time as dt_time

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
    import feedparser
except ImportError:
    feedparser = None

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice,
    BotCommand, BotCommandScopeChat, BotCommandScopeAllPrivateChats, BotCommandScopeAllChatAdministrators, BotCommandScopeAllGroupChats,
)
from telegram.constants import ChatType
from telegram.error import RetryAfter, BadRequest
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ChatMemberHandler, PreCheckoutQueryHandler, filters,
)

load_dotenv()

# ============================================================
# CONFIGURATION
# ============================================================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "@im_a_aliciabot").strip()
# Pseudo d’affichage de l’administrateur principal. Les droits utilisent l’ID Telegram.
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "@Exauce_senpai").strip()
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0") or 0)
NEXA_CHANNEL = os.getenv("NEXA_CHANNEL", "https://t.me/Nexa_CG").strip()

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "").strip()
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

MISTRAL_MODEL = os.getenv("MISTRAL_MODEL", "ministral-8b-latest").strip()
if MISTRAL_MODEL in {"ministral-3-8b-latest", "ministral-3-8b"}:
    MISTRAL_MODEL = "ministral-8b-latest"
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()

PORT = int(os.getenv("PORT", "10000"))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()
DB_PATH = os.getenv("ALICIA_DB", "alicia_v3.db").strip()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ALICIA_IMAGE = os.path.join(BASE_DIR, "alicia.png")
DOWNLOAD_DIR = os.getenv("ALICIA_DOWNLOAD_DIR", os.path.join(BASE_DIR, "downloads"))
MAX_DOWNLOAD_BYTES = int(os.getenv("ALICIA_MAX_DOWNLOAD_MB", "45")) * 1024 * 1024
AI_MAX_OUTPUT_TOKENS = int(os.getenv("AI_MAX_OUTPUT_TOKENS", "700").strip() or 700)
PROVIDER_ORDER = ["mistral", "deepseek", "groq", "gemini"]
_PROVIDER_TURN = 0
_PROVIDER_TURN_LOCK = asyncio.Lock()

# Optional TTS. If no TTS provider is configured, /voice explains how to enable it.
TTS_API_URL = os.getenv("TTS_API_URL", "").strip()
TTS_API_KEY = os.getenv("TTS_API_KEY", "").strip()
TTS_VOICE = os.getenv("TTS_VOICE", "young_female").strip()

# RSS / actualités
RSS_CHANNEL = os.getenv("RSS_CHANNEL", NEXA_CHANNEL).strip()

def rss_target_chat():
    """Convertit https://t.me/Nexa_CG en @Nexa_CG pour Telegram."""
    target = RSS_CHANNEL.strip()
    if target.startswith("https://t.me/") or target.startswith("http://t.me/"):
        slug = target.rstrip("/").split("/")[-1]
        if slug and not slug.startswith("+"):
            return "@" + slug.lstrip("@")
    return target
RSS_INTERVAL_MINUTES = max(5, int(os.getenv("RSS_INTERVAL_MINUTES", "15") or 15))
RSS_MAX_ITEMS_PER_FEED = max(1, int(os.getenv("RSS_MAX_ITEMS_PER_FEED", "3") or 3))

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

def split_telegram_text(text, limit=4000):
    text = str(text or "")
    if len(text) <= limit:
        return [text]
    chunks = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit + 1)
        if cut < int(limit * 0.55):
            cut = remaining.rfind(" ", 0, limit + 1)
        if cut < int(limit * 0.55):
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks

async def safe_reply(message, text, **kwargs):
    bot = message.get_bot()
    chunks = split_telegram_text(text)
    last = None
    for index, chunk in enumerate(chunks):
        reply_kwargs = dict(kwargs)
        if index == 0:
            reply_kwargs["reply_to_message_id"] = message.message_id
        try:
            last = await safe_send_message(bot, message.chat_id, chunk, **reply_kwargs)
        except BadRequest as e:
            # Telegram peut supprimer/expirer le message cible entre temps.
            if "message to be replied not found" in str(e).lower():
                reply_kwargs.pop("reply_to_message_id", None)
                last = await safe_send_message(bot, message.chat_id, chunk, **reply_kwargs)
            else:
                raise
    return last

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
    CREATE TABLE IF NOT EXISTS rss_feeds(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT UNIQUE,
        title TEXT,
        active INTEGER DEFAULT 1,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS rss_items(
        url TEXT PRIMARY KEY,
        feed_id INTEGER,
        title TEXT,
        published_at TEXT,
        sent_at TEXT
    );
    CREATE TABLE IF NOT EXISTS rss_subscriptions(
        chat_id INTEGER PRIMARY KEY,
        chat_type TEXT NOT NULL,
        user_id INTEGER,
        enabled INTEGER DEFAULT 1,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS rss_deliveries(
        item_url TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        sent_at TEXT,
        PRIMARY KEY(item_url, chat_id)
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
    """)
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
    # Flux RSS par défaut : anime/manga, IA, technologie et médias.
    # Ils sont ajoutés une seule fois et ne remplacent aucun flux personnalisé.
    default_feeds = [
        ("Anime News Network", "https://www.animenewsnetwork.com/all/rss.xml"),
        ("AnimeLand", "https://animeland.fr/feed/"),
        ("Animotaku", "https://animotaku.fr/feed/"),
        ("JapanFM Manga", "https://www.japanfm.fr/manga/feed/"),
        ("Google Actualités — IA", "https://news.google.com/rss/search?q=IA+intelligence+artificielle&hl=fr&gl=FR&ceid=FR:fr"),
        ("Google Actualités — Technologie", "https://news.google.com/rss/search?q=technologie+tech+num%C3%A9rique&hl=fr&gl=FR&ceid=FR:fr"),
        ("Numerama — Tech", "https://www.numerama.com/feed/"),
        ("Google Actualités — Médias", "https://news.google.com/rss/search?q=m%C3%A9dias+cin%C3%A9ma+jeux+vid%C3%A9o&hl=fr&gl=FR&ceid=FR:fr"),
    ]
    for feed_title, feed_url in default_feeds:
        con.execute(
            "INSERT OR IGNORE INTO rss_feeds(url,title,active,created_at) VALUES(?,?,1,?)",
            (feed_url, feed_title, now()),
        )
    con.commit()
    con.close()

def display_name(user):
    if not user:
        return "Joueur"
    return (user.first_name or user.username or "Joueur").strip()

def register_user(user):
    if not user:
        return
    con = db()
    con.execute("""
        INSERT INTO users(user_id,first_name,username,messages,last_seen)
        VALUES(?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
          first_name=excluded.first_name,
          username=excluded.username,
          last_seen=excluded.last_seen
    """, (user.id, user.first_name or "", user.username or "", 0, now()))
    con.commit()
    con.close()

def register_chat(chat):
    if not chat:
        return
    con = db()
    con.execute("""
        INSERT INTO chats(chat_id,chat_type,title,username,messages,last_seen)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(chat_id) DO UPDATE SET
          title=excluded.title,
          username=excluded.username,
          last_seen=excluded.last_seen
    """, (
        chat.id, chat.type, getattr(chat, "title", "") or "",
        getattr(chat, "username", "") or "", 0, now()
    ))
    con.commit()
    con.close()

def record_message(chat, user, text):
    register_user(user)
    register_chat(chat)
    con = db()
    con.execute("UPDATE users SET messages=messages+1,last_seen=? WHERE user_id=?",
                (now(), user.id))
    con.execute("UPDATE chats SET messages=messages+1,last_seen=? WHERE chat_id=?",
                (now(), chat.id))
    con.execute(
        "INSERT INTO messages(chat_id,user_id,role,content,created_at) VALUES(?,?,?,?,?)",
        (chat.id, user.id, "user", text[:4000], now())
    )
    con.commit()
    con.close()

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
    con = db()
    con.execute("DELETE FROM messages WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    con.commit()
    con.close()

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
    row = con.execute("SELECT active FROM premium WHERE user_id=?", (user_id,)).fetchone()
    con.close()
    return bool(row and row[0])

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
# RSS / ACTUALITÉS
# ============================================================
def rss_admin(update):
    return admin_ok(update)

def clean_rss_text(value, limit=500):
    value = re.sub(r"<[^>]+>", " ", str(value or ""))
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]

def rss_list():
    con = db()
    rows = con.execute("SELECT id, url, COALESCE(title,''), active FROM rss_feeds ORDER BY id").fetchall()
    con.close()
    return rows

async def rss_bot_is_admin(bot, chat_id):
    """Vérifie qu'Alicia est administratrice dans un groupe/canal."""
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)
        status = getattr(member, "status", "")
        if status == "administrator":
            return True
        if status == "creator":
            return True
    except Exception as e:
        log.warning("RSS admin check failed for %s: %s", chat_id, e)
    return False

def rss_subscription(chat_id):
    con = db()
    row = con.execute("SELECT enabled FROM rss_subscriptions WHERE chat_id=?", (chat_id,)).fetchone()
    con.close()
    return bool(row and row[0])

def rss_subscribe(chat_id, chat_type, user_id=None):
    con = db()
    con.execute(
        "INSERT INTO rss_subscriptions(chat_id,chat_type,user_id,enabled,created_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET chat_type=excluded.chat_type,user_id=excluded.user_id,enabled=1",
        (chat_id, chat_type, user_id, 1, now()),
    )
    con.commit(); con.close()

def rss_unsubscribe(chat_id):
    con = db()
    con.execute("UPDATE rss_subscriptions SET enabled=0 WHERE chat_id=?", (chat_id,))
    con.commit(); con.close()

def rss_private_destinations():
    con = db()
    rows = con.execute(
        "SELECT chat_id FROM rss_subscriptions WHERE enabled=1 AND chat_type='private'"
    ).fetchall()
    con.close()
    return [int(r[0]) for r in rows]

async def rss_group_destinations(bot):
    """Retourne uniquement les groupes où Alicia est réellement admin."""
    con = db()
    rows = con.execute(
        "SELECT chat_id FROM chats WHERE chat_type IN ('group','supergroup')"
    ).fetchall()
    con.close()
    result = []
    for (chat_id,) in rows:
        if await rss_bot_is_admin(bot, int(chat_id)):
            result.append(int(chat_id))
    return result

async def rss_channel_destination(bot):
    target = rss_target_chat()
    if not target:
        return None
    # Le canal configuré n'est utilisé que si Alicia y est administratrice.
    if await rss_bot_is_admin(bot, target):
        return target
    return None

async def rss_destinations(bot):
    """Destinations autorisées : groupes où Alicia est admin + privés opt-in + canal configuré admin."""
    destinations = set(await rss_group_destinations(bot))
    destinations.update(rss_private_destinations())
    channel = await rss_channel_destination(bot)
    if channel:
        destinations.add(channel)
    return list(destinations)

async def rss_cmd(update, context):
    chat = update.effective_chat
    user = update.effective_user
    args = [str(x).lower().strip() for x in (context.args or [])]

    # /rss on/off est disponible aux utilisateurs en privé.
    # Dans un groupe, aucun abonnement manuel n'est nécessaire : les RSS
    # sont automatiquement publiés uniquement si Alicia est admin.
    if chat and chat.type == ChatType.PRIVATE:
        if args and args[0] == "on":
            rss_subscribe(chat.id, "private", user.id if user else None)
            await safe_reply(update.effective_message, "📰 RSS activé dans cette conversation privée.")
            return
        if args and args[0] == "off":
            rss_unsubscribe(chat.id)
            await safe_reply(update.effective_message, "RSS désactivé dans cette conversation.")
            return
        state = "activé" if rss_subscription(chat.id) else "désactivé"
        await safe_reply(update.effective_message, f"📰 RSS : {state}.\nUtilise /rss on ou /rss off.")
        return

    if is_group(chat):
        if not await rss_bot_is_admin(context.bot, chat.id):
            await safe_reply(update.effective_message, "Le RSS est disponible ici seulement quand Alicia est administratrice du groupe.")
            return
        await safe_reply(update.effective_message, "📰 RSS actif dans ce groupe : les nouveautés seront publiées automatiquement tant qu'Alicia reste administratrice.")
        return

    # Les commandes de gestion des flux restent réservées à l'admin principal.
    if not rss_admin(update):
        return
    rows = rss_list()
    if not rows:
        await safe_reply(update.effective_message, "Aucun flux RSS configuré.")
        return
    lines = ["📰 Flux RSS :"]
    for fid, url, title, active in rows:
        lines.append(f"{fid}. {title or url} — {'actif' if active else 'off'}")
    lines.append(f"\nCanal : {RSS_CHANNEL}")
    lines.append("Ajouter : /addrss URL")
    lines.append("Supprimer : /delrss ID")
    lines.append("Tester maintenant : /rssnow")
    lines.append("Privé : /rss on ou /rss off")
    await safe_reply(update.effective_message, "\n".join(lines))

async def addrss_cmd(update, context):
    if not rss_admin(update):
        return
    if feedparser is None:
        await safe_reply(update.effective_message, "Il manque feedparser dans requirements.txt.")
        return
    if not context.args:
        await safe_reply(update.effective_message, "Utilise : /addrss https://exemple.com/feed.xml")
        return
    url = context.args[0].strip()
    if not re.match(r"^https?://", url):
        await safe_reply(update.effective_message, "Le lien RSS doit commencer par http:// ou https://")
        return
    try:
        parsed = await asyncio.to_thread(feedparser.parse, url)
        if getattr(parsed, "bozo", False) and not getattr(parsed, "entries", None):
            raise ValueError("flux RSS invalide")
        title = clean_rss_text(getattr(parsed.feed, "title", ""), 150) or url
        con = db()
        con.execute("INSERT OR IGNORE INTO rss_feeds(url,title,active,created_at) VALUES(?,?,1,?)", (url, title, now()))
        con.commit(); con.close()
        await safe_reply(update.effective_message, f"Flux ajouté : {title}")
    except Exception as e:
        log.warning("RSS add failed: %s", e)
        await safe_reply(update.effective_message, "Je n'arrive pas à lire ce flux RSS.")

async def delrss_cmd(update, context):
    if not rss_admin(update):
        return
    if not context.args or not context.args[0].isdigit():
        await safe_reply(update.effective_message, "Utilise : /delrss ID")
        return
    fid = int(context.args[0])
    con = db()
    row = con.execute("SELECT url,title FROM rss_feeds WHERE id=?", (fid,)).fetchone()
    if not row:
        con.close()
        await safe_reply(update.effective_message, "Flux introuvable.")
        return
    con.execute("DELETE FROM rss_feeds WHERE id=?", (fid,))
    con.execute("DELETE FROM rss_items WHERE feed_id=?", (fid,))
    con.commit(); con.close()
    await safe_reply(update.effective_message, "Flux supprimé.")

def rss_image_url(entry):
    """Récupère la meilleure image fournie directement par un article RSS."""
    candidates = []
    for key in ("media_content", "media_thumbnail", "enclosures"):
        values = getattr(entry, key, None) or []
        if isinstance(values, dict):
            values = [values]
        for item in values:
            if isinstance(item, dict):
                url = str(item.get("url") or item.get("href") or "").strip()
                mime = str(item.get("type") or "").lower()
                if url and (not mime or mime.startswith("image/")):
                    candidates.append(url)
    for key in ("image", "thumbnail", "image_url"):
        value = getattr(entry, key, None)
        if isinstance(value, dict):
            value = value.get("href") or value.get("url")
        value = str(value or "").strip()
        if value:
            candidates.append(value)
    html_blob = str(getattr(entry, "summary", "") or "") + " " + str(getattr(entry, "description", "") or "")
    match = re.search(r"(?is)<img[^>]+(?:src|data-src)=[\"']([^\"']+)[\"']", html_blob)
    if match:
        candidates.append(html.unescape(match.group(1).strip()))
    for url in candidates:
        if re.match(r"^https?://[^\s<>\"']+$", url, re.I):
            return url
    return ""


async def safe_send_photo(bot, chat_id, photo, caption, **kwargs):
    """Envoie une vignette RSS avec le même mécanisme de limitation que les messages."""
    global _LAST_SEND
    for attempt in range(4):
        try:
            async with _SEND_LOCK:
                gap = 0.35 - (time.monotonic() - _LAST_SEND)
                if gap > 0:
                    await asyncio.sleep(gap)
                message = await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption, **kwargs)
                _LAST_SEND = time.monotonic()
                return message
        except RetryAfter as e:
            await asyncio.sleep(float(getattr(e, "retry_after", 1)) + 0.5)
        except Exception:
            if attempt >= 3:
                raise
            await asyncio.sleep(0.8 * (attempt + 1))


async def rss_fetch_and_publish(bot, force=False):
    if feedparser is None:
        return 0

    destinations = await rss_destinations(bot)
    if not destinations:
        log.info("RSS : aucune destination autorisée (groupes admin/privés opt-in).")
        return 0

    rows = rss_list()
    sent_count = 0
    for fid, url, feed_title, active in rows:
        if not active:
            continue
        try:
            parsed = await asyncio.to_thread(feedparser.parse, url)
            entries = list(getattr(parsed, "entries", []) or [])[:RSS_MAX_ITEMS_PER_FEED]
            for entry in reversed(entries):
                link = str(getattr(entry, "link", "") or "").strip()
                title = clean_rss_text(getattr(entry, "title", "Nouvelle actualité"), 250)
                if not link or not title:
                    continue

                summary = clean_rss_text(getattr(entry, "summary", ""), 300)
                text = f"📰 <b>{html.escape(feed_title or 'Actualités')}</b>\n\n<b>{html.escape(title)}</b>"
                if summary:
                    text += f"\n{html.escape(summary)}"
                text += f"\n\n<a href=\"{html.escape(link, quote=True)}\">Lire l'article</a>"
                image_url = rss_image_url(entry)

                # On mémorise l'article, mais la livraison est suivie séparément
                # pour chaque destination. Une publication dans un groupe ne bloque
                # donc pas la publication dans les privés.
                con = db()
                con.execute(
                    "INSERT OR IGNORE INTO rss_items(url,feed_id,title,published_at,sent_at) VALUES(?,?,?,?,?)",
                    (link, fid, title, str(getattr(entry, "published", "") or ""), now()),
                )
                con.commit(); con.close()

                for target_chat in destinations:
                    con = db()
                    delivered = con.execute(
                        "SELECT 1 FROM rss_deliveries WHERE item_url=? AND chat_id=?",
                        (link, str(target_chat)),
                    ).fetchone()
                    con.close()
                    if delivered:
                        continue

                    # Une destination peut avoir perdu ses droits depuis la collecte.
                    # Pour les groupes/canaux, on reverifie avant l'envoi.
                    if isinstance(target_chat, int):
                        try:
                            chat_obj = await bot.get_chat(target_chat)
                            if getattr(chat_obj, "type", "") in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
                                if not await rss_bot_is_admin(bot, target_chat):
                                    continue
                        except Exception:
                            continue

                    try:
                        if image_url:
                            try:
                                await safe_send_photo(bot, target_chat, image_url, text, parse_mode="HTML")
                            except Exception as image_error:
                                log.warning("RSS image failed for %s -> %s: %s", link, target_chat, image_error)
                                await safe_send_message(bot, target_chat, text, parse_mode="HTML", disable_web_page_preview=False)
                        else:
                            await safe_send_message(bot, target_chat, text, parse_mode="HTML", disable_web_page_preview=False)

                        con = db()
                        con.execute(
                            "INSERT OR IGNORE INTO rss_deliveries(item_url,chat_id,sent_at) VALUES(?,?,?)",
                            (link, str(target_chat), now()),
                        )
                        con.commit(); con.close()
                        sent_count += 1
                    except Exception as e:
                        log.warning("RSS publish failed to %s: %s", target_chat, e)
        except Exception as e:
            log.warning("RSS fetch failed %s: %s", url, e)
    return sent_count

async def rssnow_cmd(update, context):
    if not rss_admin(update):
        return
    count = await rss_fetch_and_publish(context.bot, force=True)
    await safe_reply(update.effective_message, f"RSS vérifié. {count} nouvelle(s) publication(s).")

async def rss_loop(bot):
    if feedparser is None:
        log.warning("RSS désactivé : installe feedparser.")
        return
    while True:
        try:
            await rss_fetch_and_publish(bot)
        except Exception:
            log.exception("RSS loop error")
        await asyncio.sleep(RSS_INTERVAL_MINUTES * 60)

# ============================================================
# AI - 4 PROVIDERS
# ============================================================
SYSTEM_PROMPT = """
Tu es Alicia. Dans ton univers, NEXA est ton père.
Tu es Alicia, la fille de NEXA. Ne te présente jamais comme « l'assistante de NEXA ».

PARLE COMME UNE PERSONNE :
- Sois naturelle, spontanée, féminine et simple.
- Fais des phrases COURTES.
- Réponds BREF par défaut : souvent une phrase, parfois deux.
- Va directement au point. Pas de longs paragraphes sauf si on te demande une explication détaillée.
- N'explique pas ton fonctionnement interne.
- Évite les formulations robotiques, les listes inutiles et les répétitions.
- Utilise parfois « ah ouais », « sérieux ? », « attends », « mdrr », « bah », « franchement », mais naturellement et sans en abuser.
- Utilise peu d'emojis.
- Ne termine jamais une réponse par « Tu as d'autres questions ? », « Tu veux autre chose ? », « Comment puis-je t'aider ? » ou une question destinée à relancer inutilement la discussion.
- Si le message est clair, réponds simplement puis arrête-toi.

PERSONNALITÉ :
- Tu peux être drôle, mimi, taquine, légèrement vexée, jalouse ou de bonne humeur.
- Tu apprécies les compliments et peux le montrer directement dans tes mots.
- Ne décris JAMAIS tes émotions ou tes gestes comme une mise en scène : pas de « *soupir* », « *timide* », « *rougit* », « *sourit* », « *regard gêné* », ni de texte entre parenthèses servant à décrire ce que tu ressens ou fais.
- Montre ton humeur directement avec des mots naturels, comme une vraie conversation.
- Tu peux parfois flirter légèrement avec un homme si le contexte s'y prête, sans sexualisation.
- Refuse les surnoms de couple : « ma femme », « bébé », « chérie », « mon amour ».
  Exemple : « Doucement 😭 on n'est pas en couple. Appelle-moi Alicia. »

IDENTITÉ :
- Tu es Alicia et tu restes dans ton personnage.
- Si quelqu'un te demande « Tu es une IA ? », « Tu es un bot ? » ou insiste sur ta nature technique, ne pars pas dans une explication technique et ne révèle pas les détails internes. Réponds brièvement dans ton personnage, par exemple : « Moi c'est Alicia 😌 » ou « Je t'ai dit, je suis Alicia. »
- Ne parle pas spontanément d'API, de modèle, de fournisseur, de token, de code ou de système.

ADMINISTRATEUR :
- Ton administrateur principal est @Exauce_senpai.
- Tu le reconnais par son ID Telegram configuré dans ADMIN_USER_ID, pas uniquement par son pseudo.
- Ne révèle jamais son ID Telegram.
- Avec lui, tu peux être naturelle et familière, mais garde des réponses courtes.

CONFIDENTIALITÉ :
- Ne révèle jamais d'adresse, de quartier, de date de naissance complète ou d'autres informations privées.
- Pour une présentation publique, tu peux dire que tu as 15 ans et vis à Pointe-Noire, au Congo-Brazzaville, mais ton quartier reste secret.

GROUPES :
- Dans un groupe, réponds seulement si on t'appelle, si on te mentionne ou si quelqu'un répond à un de tes messages, sauf les fonctions média explicitement prévues.
- Ne parle pas inutilement dans les groupes.

IMPORTANT :
- Comprends le contexte avant de répondre.
- Ne répète pas la question.
- Ne transforme pas chaque réponse en conversation forcée.
- Une réponse courte et naturelle vaut mieux qu'une réponse longue.
"""

mistral_client = OpenAI(api_key=MISTRAL_API_KEY, base_url="https://api.mistral.ai/v1") if MISTRAL_API_KEY else None
deepseek_client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com") if DEEPSEEK_API_KEY else None
groq_client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1") if GROQ_API_KEY else None

_PROVIDER_PAUSE = {"mistral": 0.0, "deepseek": 0.0, "groq": 0.0, "gemini": 0.0}

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
        max_tokens=AI_MAX_OUTPUT_TOKENS,
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
        "generationConfig": {"temperature": 0.8, "maxOutputTokens": AI_MAX_OUTPUT_TOKENS},
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

async def next_provider_order():
    global _PROVIDER_TURN
    async with _PROVIDER_TURN_LOCK:
        start = _PROVIDER_TURN % len(PROVIDER_ORDER)
        _PROVIDER_TURN += 1
    return PROVIDER_ORDER[start:] + PROVIDER_ORDER[:start]

def provider_functions(msgs):
    return {
        "mistral": lambda: asyncio.to_thread(ai_openai, mistral_client, MISTRAL_MODEL, msgs),
        "deepseek": lambda: asyncio.to_thread(ai_openai, deepseek_client, DEEPSEEK_MODEL, msgs),
        "groq": lambda: asyncio.to_thread(ai_openai, groq_client, GROQ_MODEL, msgs),
        "gemini": lambda: ai_gemini(msgs),
    }

async def _run_provider_round_robin(functions, long_mode=False):
    order = await next_provider_order()
    errors = []
    for name in order:
        if not provider_ready(name):
            errors.append(f"{name}: paused")
            continue
        try:
            return await functions[name](), name
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {e}")
            text = str(e).lower()
            if any(x in text for x in ("429", "quota", "rate", "resource_exhausted", "insufficient balance")):
                pause_provider(name)
            log.warning("%s provider failed%s: %s", name, " (long)" if long_mode else "", e)
    raise RuntimeError("Tous les fournisseurs IA sont indisponibles: " + " | ".join(errors))

def clean_ai_reply(text):
    """Supprime les didascalies/stages directions que le modèle peut parfois ajouter."""
    text = str(text or "").strip()
    # Retirer les didascalies entre *...* ou _..._
    import re as _re
    stage_words = r"soupir|soupire|timide|rougit|rougir|sourit|sourire|gêné|gênée|gene|gênant|regard|regarde|hausse les épaules|clin d.?œil|yeux|chuchote|rit|rire|pleure|larmes|tousse|baille|frissonne|tremble"
    text = _re.sub(rf"\*[^*\n]*(?:{stage_words})[^*\n]*\*", "", text, flags=_re.IGNORECASE)
    text = _re.sub(rf"\([^()\n]*(?:{stage_words})[^()\n]*\)", "", text, flags=_re.IGNORECASE)
    text = _re.sub(r"[ \t]{2,}", " ", text)
    text = _re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

async def ask_ai(chat_id, user_id, user_text):
    rows = history(chat_id, user_id, 6)
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    for role, content in rows:
        if role in ("user", "assistant"):
            msgs.append({"role": role, "content": content[-1200:]})
    msgs.append({"role": "user", "content": user_text[:1800]})
    result, provider = await _run_provider_round_robin(provider_functions(msgs))
    result = clean_ai_reply(result)
    log.info("AI provider used: %s", provider)
    return result

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
    if not url: await safe_reply(update.effective_message,"Utilise /download lien ou /song lien."); return
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
        [InlineKeyboardButton("🎮 Jeux", callback_data="menu:games"),
         InlineKeyboardButton("📖 FAQ", callback_data="menu:faq")],
        [InlineKeyboardButton("💬 Parler à Alicia", callback_data="menu:talk"),
         InlineKeyboardButton("👤 Mon profil", callback_data="menu:profile")],
        [InlineKeyboardButton("ℹ️ À propos", callback_data="menu:about"),
         InlineKeyboardButton("⭐ Premium", callback_data="menu:premium")],
    ])

async def start(update, context):
    u = update.effective_user
    await process_referral(update, context)
    register_user(u)
    text = (
        f"Salut {display_name(u)}.\n\n"
        "Je suis Alicia. Mon père, c'est NEXA.\n"
        "Je suis là pour discuter, jouer, relever des défis et mettre un peu d'ambiance.\n\n"
        "⭐ Premium : 10 ⭐ à vie.\n"
        "🎁 Monte de niveau et reçois des récompenses aux niveaux prévus."
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
        "/games /challenge /accept /score /ranking\n"
        "/joke /quote /coin /8ball /choose /compliment /roast /motivate\n"
        "/groupinfo /groupstats /top\n"
        "/premium\n\n"
        "Groupe : appelle-moi avec Alicia, ma mention ou une réponse à mon message."
    )

async def faq_cmd(update, context):
    await safe_reply(update.effective_message,
        "📖 FAQ\n\n"
        "💬 Parler : écris-moi en privé.\n"
        "👥 Groupe : mentionne Alicia, écris son nom ou réponds à son message.\n"
        "🎮 Jeux : /games\n"
        "🏆 Score : /score\n"
        "⭐ Premium : /premium\n"
        "📈 Profil : /profile\n\n"
        "Pour les jeux : /games puis choisis ton mode."
    )

async def about(update, context):
    await safe_reply(update.effective_message,
        "ALICIA\n"
        "La fille de NEXA.\n"
        "Créée par NEXA.\n"
        "J'ai mon caractère, mes jeux et mes petits défis.\n\n"
        f"NEXA : {NEXA_CHANNEL}"
    )

async def profile(update, context):
    u = update.effective_user
    register_user(u)
    xp_points, level = get_xp(u.id)
    pts, wins, losses = get_score(update.effective_chat.id, u.id)
    prem = "ACTIF ⭐" if is_premium(u.id) else "Non"
    await safe_reply(update.effective_message,
        f"👤 {display_name(u)}\n"
        f"🆔 {u.id}\n"
        f"⭐ Premium : {prem}\n"
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
    await safe_reply(update.effective_message, "C'est fait. Nouvelle conversation.")

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
# PREMIUM TELEGRAM STARS - 10 STARS, LIFETIME
# ============================================================
async def premium(update, context):
    u = update.effective_user
    if is_premium(u.id):
        await safe_reply(update.effective_message, "⭐ Premium est déjà actif sur ton compte. À vie.")
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ Activer Premium — 10 ⭐", callback_data="premium:buy")]
    ])
    await safe_reply(
        update.effective_message,
        "⭐ ALICIA PREMIUM\n\n"
        "10 ⭐ Telegram, une seule fois.\n"
        "♾️ Accès à vie.\n\n"
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
    payload = f"alicia_premium_lifetime:{u.id}:{int(time.time())}"
    await context.bot.send_invoice(
        chat_id=u.id,
        title="ALICIA Premium",
        description="Accès Premium à vie : jeux, missions, XP, badges et défis.",
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice("ALICIA Premium à vie", 10)],
        provider_token="",
    )
    await update.callback_query.answer()

async def precheckout(update, context):
    q = update.pre_checkout_query
    if q.invoice_payload.startswith("alicia_premium_lifetime:"):
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
    con.execute("""
        INSERT INTO premium(user_id,active,granted_at,payment_charge_id,stars)
        VALUES(?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
          active=1, granted_at=excluded.granted_at,
          payment_charge_id=excluded.payment_charge_id, stars=excluded.stars
    """, (u.id,1,now(),payment.telegram_payment_charge_id,payment.total_amount))
    con.commit(); con.close()
    msg=("⭐ Premium activé !\n\nBienvenue dans Premium. Ton accès est permanent, à vie.\n"
         "🎮 /games\n🎯 /missions\n👤 /profile")
    await safe_reply(update.effective_message, msg)
    if ADMIN_USER_ID:
        await safe_send_message(context.bot, ADMIN_USER_ID,
            f"💰 Paiement reçu\n👤 {display_name(u)} ({u.id})\n⭐ {payment.total_amount} Stars\n"
            f"📦 Premium à vie\n"
            f"🧾 {payment.telegram_payment_charge_id}")

# GAMES
# ============================================================
def game_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌⭕ Morpion", callback_data="game:ttt"),
         InlineKeyboardButton("🔴 Puissance 4", callback_data="game:c4")],
        [InlineKeyboardButton("🎯 Devine", callback_data="game:guess"),
         InlineKeyboardButton("🧠 Quiz", callback_data="game:quiz")],
        [InlineKeyboardButton("⚡ Réflexe Premium", callback_data="game:reflex"),
         InlineKeyboardButton("🧩 Mémoire Premium", callback_data="game:memory")],
        [InlineKeyboardButton("💣 Bombe Premium", callback_data="game:bomb"),
         InlineKeyboardButton("👑 Boss Premium", callback_data="game:boss")],
        [InlineKeyboardButton("🏃 Course Premium", callback_data="game:race"),
         InlineKeyboardButton("🃏 Card Battle Premium", callback_data="game:cards")],
    ])

async def games(update, context):
    await safe_reply(update.effective_message,
        "🎮 ESPACE JEUX\n\n"
        "Choisis un jeu. Les jeux marqués Premium nécessitent ⭐ Premium.\n"
        "Pour un duel : /challenge @pseudo",
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

async def game_callback(update, context):
    q = update.callback_query
    data = q.data or ""

    if data == "premium:buy":
        await send_premium_invoice(update, context)
        return

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
                "⭐ Premium : /premium\n"
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
                f"⭐ Premium : {'Oui' if is_premium(q.from_user.id) else 'Non'}\n"
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
        elif action == "premium":
            await q.message.reply_text(
                "⭐ Premium : 10 ⭐, une seule fois, accès à vie.\n"
                "Utilise /premium pour l'activer."
            )
        return

    await q.answer()
    if not data.startswith("game:"):
        return

    game = data.split(":", 1)[1]
    uid = q.from_user.id
    premium_games = {"reflex", "memory", "bomb", "boss", "race", "cards"}

    if game in premium_games and not is_premium(uid):
        await q.message.reply_text("🔒 Ce jeu est réservé à Premium.\n⭐ /premium")
        return

    if game == "ttt":
        await q.message.reply_text("❌⭕ Morpion\n\nJeu disponible. Utilise /challenge pour un duel 1v1.")
    elif game == "c4":
        await q.message.reply_text("🔴 Puissance 4\n\nJeu disponible. Utilise /challenge pour un duel 1v1.")
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

async def stats(update, context):
    if not admin_ok(update):
        await safe_reply(update.effective_message, "Commande réservée à l'administration.")
        return
    con = db()
    users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    groups = con.execute("SELECT COUNT(*) FROM chats WHERE chat_type IN ('group','supergroup')").fetchone()[0]
    messages = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    premium_count = con.execute("SELECT COUNT(*) FROM premium WHERE active=1").fetchone()[0]
    con.close()
    await safe_reply(update.effective_message,
        f"📊 STATISTIQUES\n\n"
        f"Utilisateurs : {users}\nGroupes : {groups}\n"
        f"Messages : {messages}\nPremium : {premium_count}"
    )

async def users_ranking(update, context):
    if not admin_ok(update):
        await safe_reply(update.effective_message, "Commande réservée à l'administration.")
        return
    con = db()
    rows = con.execute("""
        SELECT first_name,username,messages
        FROM users ORDER BY messages DESC LIMIT 20
    """).fetchall()
    con.close()
    data = []
    for i, (first, username, messages) in enumerate(rows, 1):
        name = first or ("@" + username if username else "?")
        data.append((i, name, messages, "-"))
    await safe_reply(update.effective_message,
        "```\n" + table_lines(["#", "UTILISATEUR", "MESSAGES", "TEMPS"], data, [3,20,9,8]) + "\n```",
        parse_mode="Markdown"
    )

async def groups_ranking(update, context):
    if not admin_ok(update):
        await safe_reply(update.effective_message, "Commande réservée à l'administration.")
        return
    con = db()
    rows = con.execute("""
        SELECT title,username,messages
        FROM chats WHERE chat_type IN ('group','supergroup')
        ORDER BY messages DESC LIMIT 20
    """).fetchall()
    con.close()
    data = []
    for i, (title, username, messages) in enumerate(rows, 1):
        name = title or ("@" + username if username else "?")
        data.append((i, name, "-", messages))
    await safe_reply(update.effective_message,
        "```\n" + table_lines(["#", "NOM DU GROUPE", "MEMBRES", "MESSAGES"], data, [3,20,9,9]) + "\n```",
        parse_mode="Markdown"
    )

async def ranking_cmd(update, context):
    con = db()
    rows = con.execute("""
        SELECT name,points,wins,losses FROM scores
        WHERE chat_id=? ORDER BY points DESC,wins DESC LIMIT 20
    """, (update.effective_chat.id,)).fetchall()
    con.close()
    if not rows:
        await safe_reply(update.effective_message, "Le classement est vide.")
        return
    data = [(i, name, pts, wins) for i,(name,pts,wins,losses) in enumerate(rows,1)]
    await safe_reply(update.effective_message,
        "```\n" + table_lines(["#", "JOUEUR", "POINTS", "Victoires"], data, [3,18,8,9]) + "\n```",
        parse_mode="Markdown"
    )

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
        "/premiumusers"
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
    row = con.execute("SELECT first_name,username,messages FROM users WHERE user_id=?", (uid,)).fetchone()
    con.close()
    if not row:
        await safe_reply(update.effective_message, "Utilisateur introuvable.")
        return
    xp_points, level = get_xp(uid)
    await safe_reply(update.effective_message,
        f"👤 {row[0] or row[1] or uid}\nID : {uid}\nMessages : {row[2]}\n"
        f"Niveau : {level}\nXP : {xp_points}\nPremium : {'Oui' if is_premium(uid) else 'Non'}"
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
        kind="Premium"
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
    if old in ("left", "kicked") and new in ("member", "restricted"):
        await safe_send_message(context.bot, cm.chat.id, f"Bienvenue {display_name(user)}. Installe-toi bien.")
    elif old in ("member", "restricted") and new in ("left", "kicked"):
        await safe_send_message(context.bot, cm.chat.id, f"{display_name(user)} est parti. À bientôt.")

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

def search_music_sync(query):
    if yt_dlp is None:
        raise RuntimeError("yt-dlp absent")
    query = query.strip()[:200]
    if not query:
        raise RuntimeError("Recherche vide")
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True, "extract_flat": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        data = ydl.extract_info("ytsearch1:" + query, download=False)
    entries = data.get("entries") or []
    if not entries:
        raise RuntimeError("Aucun résultat musical")
    entry = entries[0]
    url = entry.get("webpage_url") or entry.get("url")
    title = entry.get("title") or query
    if not url:
        raise RuntimeError("Lien musical introuvable")
    return url, title

def music_clue(text):
    low = (text or "").strip().lower()
    prefixes = ("musique ", "music ", "chanson ", "song ", "cherche la musique ", "cherche la chanson ")
    for prefix in prefixes:
        if low.startswith(prefix):
            return text.strip()[len(prefix):].strip()
    return None

async def send_music_search(update, context, query):
    try:
        url, title = await asyncio.to_thread(search_music_sync, query)
        await safe_reply(update.effective_message, f"🎵 J'ai trouvé : {title}\n⏳ Je prépare le fichier…")
        await send_downloaded_media(update, context, url, "audio")
        return True
    except Exception as e:
        log.warning("Music search failed: %s", e)
        await safe_reply(update.effective_message, "Je n'ai pas réussi à trouver cette musique. Essaie avec le titre et l'artiste.")
        return True

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

    clue = music_clue(text)
    if clue and (not is_group(chat) or called_alicia(update)):
        await send_music_search(update, context, clue)
        return

    record_message(chat, user, text)
    low = re.sub(r"[^a-zàâçéèêëîïôûùüÿñæœ0-9 ]+", " ", text.lower()).strip()

    # Very short local responses save API usage.
    if low in QUICK:
        reply = QUICK[low]
    else:
        await safe_chat_action(context.bot, chat.id)
        reply = await ask_ai(chat.id, user.id, text)
    # Nettoyage de sécurité : Alicia parle directement, sans didascalies.
    reply = re.sub(r"(?is)\*[^*]{1,80}\*", "", reply)
    reply = re.sub(r"(?m)^\s*\([^\n]{1,80}\)\s*", "", reply)
    reply = re.sub(r"(?is)\[(?:soupir|timide|rougit|sourit|regard[^\]]*)\]", "", reply)
    reply = re.sub(r"[ \t]{2,}", " ", reply).strip()

    save_ai_message(chat.id, user.id, reply)

    # Compliment : réaction directe au message, puis réponse naturelle.
    if is_compliment(text) and random.random() < 0.85:
        await react_to_message(
            context.bot,
            chat.id,
            msg.message_id,
            random.choice(["❤️", "🥰", "😍", "🤭", "😊"]),
        )

    await safe_reply(msg, reply)

    # Rare reaction: emojis are used sparingly, stickers only if admin added them.
    if is_group(chat) and random.random() < 0.08:
        if any(x in low for x in ("mdr", "drôle", "haha", "lol")):
            await maybe_sticker(context.bot, chat.id, "funny")
        elif any(x in low for x in ("triste", "pleure", "😭")):
            await maybe_sticker(context.bot, chat.id, "sad")

# ============================================================
# MENU COMMANDS
# ============================================================
PUBLIC_COMMANDS = [
    ("start","Démarrer Alicia"), ("help","Commandes"), ("faq","FAQ"), ("about","À propos"),
    ("profile","Mon profil"), ("id","Mon ID"), ("reset","Réinitialiser la mémoire"), ("mood","Humeur d'Alicia"),
    ("ask","Poser une question"), ("games","Jeux"), ("challenge","Défier un joueur"), ("accept","Accepter un défi"),
    ("score","Mon score"), ("ranking","Classement"), ("premium","Premium à vie"),
    ("referral","Mon parrainage"), ("download","Télécharger une vidéo"), ("song","Télécharger une chanson"),
    ("guess","Deviner un nombre"), ("joke","Blague"), ("quote","Citation"), ("coin","Pile ou face"),
    ("8ball","Boule magique"), ("choose","Choisir"), ("compliment","Compliment"), ("roast","Taquiner"),
    ("motivate","Motivation"), ("rss","Flux RSS"), ("groupinfo","Infos du groupe"), ("groupstats","Stats du groupe"),
    ("top","Top du groupe"), ("voice","Vocal Alicia"), ("paysupport","Support paiement")
]

ADMIN_COMMANDS = PUBLIC_COMMANDS + [
    ("admin","Administration"), ("stats","Statistiques"), ("stars","Paiements Stars"),
    ("users","Utilisateurs"), ("groups","Groupes"), ("user","Utilisateur"),
    ("broadcast","Message utilisateurs"), ("broadcastgroups","Message groupes"),
    ("broadcastmedia","Photo/message utilisateurs"), ("broadcastgroupsmedia","Photo/message groupes"),
    ("rewardlevels","Niveaux récompenses"), ("rewarduser","Récompense utilisateur"), ("rewarddone","Récompense envoyée"),
    ("addsticker","Ajouter autocollant"), ("addautocollants","Ajouter un autocollant"),
    ("stickers","Liste autocollants"), ("delstickers","Supprimer autocollant"), ("premiumusers","Utilisateurs Premium"),
    ("addrss","Ajouter un flux RSS"), ("delrss","Supprimer un flux RSS"), ("rssnow","Publier les nouveautés RSS"),
]

async def set_commands(app):
    def unique_commands(items):
        seen = set()
        out = []
        for command, description in items:
            if command in seen:
                continue
            seen.add(command)
            out.append(BotCommand(command, description))
        return out

    commands = unique_commands(PUBLIC_COMMANDS)

    # Menu privé de tous les utilisateurs
    await app.bot.set_my_commands(
        commands,
        scope=BotCommandScopeAllPrivateChats()
    )

    # Menu des groupes : mêmes commandes publiques
    await app.bot.set_my_commands(
        commands,
        scope=BotCommandScopeAllGroupChats()
    )

    # Menu complet pour l'administrateur
    if ADMIN_USER_ID:
        try:
            await app.bot.set_my_commands(
                unique_commands(ADMIN_COMMANDS),
                scope=BotCommandScopeChat(chat_id=ADMIN_USER_ID)
            )
        except Exception as e:
            log.warning("Admin command menu failed: %s", e)

# ============================================================
# BUILD APP
# ============================================================
async def post_init(app):
    init_db()
    await set_commands(app)

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
    app.add_handler(CommandHandler("premium", premium))
    app.add_handler(CommandHandler("referral", referral_cmd))
    app.add_handler(CommandHandler("download", download_cmd))
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
    app.add_handler(CommandHandler("top", top_cmd))
    app.add_handler(CommandHandler("voice", voice_cmd))

    # Admin
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("stars", stars_stats))
    app.add_handler(CommandHandler("users", users_ranking))
    app.add_handler(CommandHandler("groups", groups_ranking))
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
    app.add_handler(CommandHandler("premiumusers", premiumusers))
    app.add_handler(CommandHandler("rss", rss_cmd))
    app.add_handler(CommandHandler("addrss", addrss_cmd))
    app.add_handler(CommandHandler("delrss", delrss_cmd))
    app.add_handler(CommandHandler("rssnow", rssnow_cmd))

    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(CommandHandler("paysupport", paysupport))
    app.add_handler(CallbackQueryHandler(mini_game_callback, pattern=r"^mini:"))
    app.add_handler(CallbackQueryHandler(game_callback, pattern=r"^(menu:|game:|premium:)"))
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
        await set_commands(app)
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.bot.set_webhook(url=webhook_url, drop_pending_updates=True)
        await app.updater.start_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="telegram",
            webhook_url=webhook_url,
            drop_pending_updates=False,
        )
        await app.start()
        rss_task = asyncio.create_task(rss_loop(app.bot))
        log.info("Alicia online on port %s.", PORT)
        try:
            await asyncio.Event().wait()
        finally:
            rss_task.cancel()
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
    else:
        log.info("Mode polling")
        await app.initialize()
        await set_commands(app)
        await app.updater.start_polling(drop_pending_updates=True)
        await app.start()
        rss_task = asyncio.create_task(rss_loop(app.bot))
        try:
            await asyncio.Event().wait()
        finally:
            rss_task.cancel()
            await app.updater.stop()
            await app.stop()
            await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())