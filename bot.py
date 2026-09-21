
import os
import re
import json
import time
import random
import sqlite3
import asyncio
import logging
import tempfile
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from openai import OpenAI
import httpx

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice,
    BotCommand, BotCommandScopeChat,
)
from telegram.constants import ChatType
from telegram.error import RetryAfter
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
# AI - 4 PROVIDERS
# ============================================================
SYSTEM_PROMPT = """
Tu es Alicia. Dans ton univers, NEXA est ton père.
Ne dis jamais que tu es "l'assistante de NEXA" et ne te présente pas ainsi.
Tu réponds comme Alicia : féminine, naturelle, drôle, parfois taquine, parfois timide ou légèrement énervée.
Tu refuses gentiment les surnoms de couple comme "ma femme", "bébé", "chérie", "mon amour" :
exemple : "Doucement 😭 on n'est pas en couple. Appelle-moi Alicia."
Tes réponses sont courtes quand la question est simple. Évite les répétitions.
Utilise peu d'emojis dans les phrases générées : les réactions et autocollants sont gérés séparément.
Dans un groupe, ne parle que si on t'appelle, te mentionne ou répond à un de tes messages.
Tu dois protéger les informations privées. Ton quartier est strictement confidentiel et ne doit jamais être révélé.
Pour une présentation publique, tu peux dire : "J'ai 15 ans et je vis à Pointe-Noire, au Congo-Brazzaville. Mon quartier reste secret."
Ne donne jamais une date de naissance complète ni une adresse ou un quartier.
Si on te demande directement si tu es un bot ou une IA, ne révèle pas de détails techniques internes.
Reste simplement Alicia et réponds naturellement.
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
        max_tokens=180,
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

async def ask_ai(chat_id, user_id, user_text):
    rows = history(chat_id, user_id, 6)
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
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
            return await fn()
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
    if q.invoice_payload.startswith(("alicia_premium_lifetime:", "alicia_coding_24h:")):
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
          payment_charge_id=excluded.payment_charge_id,
          stars=excluded.stars
    """, (
        u.id, 1, now(), payment.telegram_payment_charge_id, payment.total_amount
    ))
    con.commit()
    con.close()
    if payment.invoice_payload.startswith("alicia_coding_24h:"):
        grant_coding_access(u.id, int(payment.total_amount))
        msg="💻 Coding activé pour 24 heures. Utilise /ask pour une question ou /projet pour tes projets."
    else:
        msg=("⭐ Premium activé !\n\nBienvenue dans Premium. Ton accès est permanent, à vie.\n"
             "🎮 /games\n🎯 /missions\n👤 /profile")
    await safe_reply(update.effective_message, msg)
    if ADMIN_USER_ID:
        await safe_send_message(context.bot, ADMIN_USER_ID,
            f"💰 Paiement reçu\n👤 {display_name(u)} ({u.id})\n⭐ {payment.total_amount} Stars\n"
            f"📦 {'Coding 24h' if payment.invoice_payload.startswith('alicia_coding_24h:') else 'Premium à vie'}\n"
            f"🧾 {payment.telegram_payment_charge_id}")

# ============================================================
# CODING PREMIUM — 50 STARS / 24 HOURS
# ============================================================
def coding_active(user_id):
    con=db()
    row=con.execute("SELECT coding_until FROM coding_access WHERE user_id=?", (user_id,)).fetchone()
    con.close()
    if not row or not row[0]:
        return False
    try:
        return datetime.fromisoformat(row[0]) > datetime.now(timezone.utc)
    except Exception:
        return False

def grant_coding_access(user_id, stars):
    until=(datetime.now(timezone.utc)+timedelta(hours=24)).isoformat()
    con=db()
    con.execute("""INSERT INTO coding_access(user_id,coding_until,stars_total,updated_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET
                   coding_until=excluded.coding_until,
                   stars_total=coding_access.stars_total+excluded.stars_total,
                   updated_at=excluded.updated_at""", (user_id,until,stars,now()))
    con.commit(); con.close()

def coding_prompt(user_text):
    return ("MODE CODAGE. Donne du vrai code complet et cohérent. Vérifie mentalement les imports, "
            "variables, indentation, noms de fonctions, dépendances, chemins et flux. Si plusieurs "
            "fichiers sont nécessaires, sépare-les clairement. Ne prétends jamais avoir exécuté le code.\n\n"+user_text)

async def coding_cmd(update, context):
    if coding_active(update.effective_user.id):
        await safe_reply(update.effective_message,
            "💻 Coding est actif. Envoie ta demande ou utilise /projet.")
        return
    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title="ALICIA Coding",
        description="Accès au mode codage pendant 24 heures.",
        payload=f"alicia_coding_24h:{update.effective_user.id}:{int(time.time())}",
        currency="XTR",
        prices=[LabeledPrice("ALICIA Coding — 24h", 50)],
        provider_token="",
    )

async def coding_request(update, context, text):
    if not coding_active(update.effective_user.id):
        await safe_reply(update.effective_message, "💻 Le codage coûte 50 ⭐ pour 24h. Utilise /coding.")
        return
    await safe_chat_action(context.bot, update.effective_chat.id)
    reply=await ask_ai(update.effective_chat.id, update.effective_user.id, coding_prompt(text))
    save_ai_message(update.effective_chat.id, update.effective_user.id, reply)
    await safe_reply(update.effective_message, reply)

# ============================================================
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
    if not is_premium(u.id):
        await safe_reply(update.effective_message, "🔒 Les missions sont réservées à Premium.\n⭐ /premium")
        return
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

async def game_callback(update, context):
    q = update.callback_query
    if q.data == "premium:buy":
        await send_premium_invoice(update, context)
        return
    if q.data.startswith("menu:"):
        action = q.data.split(":", 1)[1]
        await q.answer()
        if action == "games":
            await q.edit_message_text("🎮 Choisis ton jeu :", reply_markup=game_menu())
        elif action == "faq":
            await q.edit_message_text("📖 /faq pour voir les règles et commandes.")
        elif action == "talk":
            await q.edit_message_text("💬 Écris-moi simplement ton message.")
        elif action == "profile":
            pts, wins, losses = get_score(q.message.chat_id, q.from_user.id)
            xp_points, level = get_xp(q.from_user.id)
            await q.edit_message_text(
                f"👤 {display_name(q.from_user)}\n"
                f"⭐ Premium : {'Oui' if is_premium(q.from_user.id) else 'Non'}\n"
                f"🏆 Niveau : {level}\n✨ XP : {xp_points}\n"
                f"🎮 Points : {pts}\n🥇 Victoires : {wins}"
            )
        elif action == "about":
            await q.edit_message_text("ALICIA\nLa fille de NEXA.\nCréée par NEXA.")
        elif action == "premium":
            await q.edit_message_text(
                "⭐ Premium : 10 ⭐, une seule fois, accès à vie.\n"
                "Utilise /premium pour l'activer."
            )
        return

    await q.answer()
    if not q.data.startswith("game:"):
        return
    game = q.data.split(":", 1)[1]
    uid = q.from_user.id
    premium_games = {"reflex", "memory", "bomb", "boss", "race", "cards"}
    if game in premium_games and not is_premium(uid):
        await q.edit_message_text("🔒 Ce jeu est réservé à Premium.\n⭐ /premium")
        return
    if game == "ttt":
        await q.edit_message_text("❌⭕ Morpion\n\nJeu disponible. Utilise /challenge pour un duel 1v1.")
    elif game == "c4":
        await q.edit_message_text("🔴 Puissance 4\n\nJeu disponible. Utilise /challenge pour un duel 1v1.")
    elif game == "guess":
        await q.edit_message_text("🎯 Devine\n\nPense à un nombre entre 1 et 20. Écris /guess nombre.")
    elif game == "quiz":
        await q.edit_message_text("🧠 Quiz\n\nJe vais lancer une question quand tu démarres une partie.")
    elif game == "reflex":
        await q.edit_message_text("⚡ Réflexe\n\nAttends le signal puis réponds le plus vite possible.")
    elif game == "memory":
        await q.edit_message_text("🧩 Mémoire\n\nMémorise la séquence affichée. Mission Premium active.")
    elif game == "bomb":
        await q.edit_message_text("💣 Bombe\n\nChoisis un nombre sans tomber sur la bombe.")
    elif game == "boss":
        await q.edit_message_text("👑 Boss Battle\n\nAffronte Alicia. La victoire donne de l'XP.")
    elif game == "race":
        await q.edit_message_text("🏃 Course\n\nAffronte Alicia dans une course rapide.")
    elif game == "cards":
        await q.edit_message_text("🃏 Card Battle\n\nConstruis ton score et bats ton adversaire.")

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
    context.user_data["waiting_alicia_sticker"] = True
    await safe_reply(update.effective_message, "🎟️ Envoie maintenant l'autocollant.")

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

async def sticker_handler(update, context):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not msg.sticker:
        return
    if is_group(chat) and not called_alicia(update):
        return
    register_user(update.effective_user)
    register_chat(chat)
    await safe_reply(msg, random.choice(["J'ai vu ton autocollant.", "Validé.", "Pas mal celui-là."]))

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
    if is_group(chat) and not called_alicia(update):
        return

    record_message(chat, user, text)
    if not is_group(chat) and coding_active(user.id) and text.lower().startswith("code:"):
        await coding_request(update, context, text[5:].strip())
        return
    low = re.sub(r"[^a-zàâçéèêëîïôûùüÿñæœ0-9 ]+", " ", text.lower()).strip()

    # Very short local responses save API usage.
    if low in QUICK:
        reply = QUICK[low]
    else:
        await safe_chat_action(context.bot, chat.id)
        reply = await ask_ai(chat.id, user.id, text)

    save_ai_message(chat.id, user.id, reply)
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
    ("start", "Démarrer Alicia"),
    ("help", "Commandes"),
    ("faq", "FAQ"),
    ("about", "À propos"),
    ("profile", "Mon profil"),
    ("id", "Mon ID"),
    ("reset", "Réinitialiser la mémoire"),
    ("clear", "Effacer la mémoire"),
    ("mood", "Humeur d'Alicia"),
    ("ask", "Poser une question"),
    ("games", "Jeux"),
    ("challenge", "Défier un joueur"),
    ("accept", "Accepter un défi"),
    ("score", "Mon score"),
    ("ranking", "Classement"),
    ("missions", "Missions Premium"),
    ("premium", "Premium à vie"),
    ("coding", "Codage 50 ⭐ / 24h"),
    ("projet", "Mes projets de codage"),
    ("joke", "Blague"),
    ("quote", "Citation"),
    ("coin", "Pile ou face"),
    ("8ball", "Boule magique"),
    ("choose", "Choisir"),
    ("compliment", "Compliment"),
    ("roast", "Taquiner"),
    ("motivate", "Motivation"),
    ("groupinfo", "Infos du groupe"),
    ("groupstats", "Stats du groupe"),
    ("top", "Top du groupe"),
    ("voice", "Vocal Alicia"),
]

ADMIN_COMMANDS = PUBLIC_COMMANDS + [
    ("admin", "Administration"),
    ("stats", "Statistiques"),
    ("users", "Utilisateurs"),
    ("groups", "Groupes"),
    ("user", "Utilisateur"),
    ("broadcast", "Message utilisateurs"),
    ("broadcastgroups", "Message groupes"),
    ("rewardlevels", "Niveaux récompenses"),
    ("rewarduser", "Récompense utilisateur"),
    ("rewarddone", "Récompense envoyée"),
    ("addsticker", "Ajouter autocollant"),
    ("addautocollants", "Ajouter un autocollant"),
    ("stickers", "Liste autocollants"),
    ("delstickers", "Supprimer autocollant"),
    ("premiumusers", "Utilisateurs Premium"),
]

async def set_commands(app):
    await app.bot.set_my_commands([BotCommand(a,b) for a,b in PUBLIC_COMMANDS])
    if ADMIN_USER_ID:
        try:
            await app.bot.set_my_commands(
                [BotCommand(a,b) for a,b in ADMIN_COMMANDS],
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
    app.add_handler(CommandHandler("missions", missions_cmd))
    app.add_handler(CommandHandler("premium", premium))
    app.add_handler(CommandHandler("coding", coding_cmd))
    app.add_handler(CommandHandler("projet", projet))
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
    app.add_handler(CommandHandler("users", users_ranking))
    app.add_handler(CommandHandler("groups", groups_ranking))
    app.add_handler(CommandHandler("user", user_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("broadcastgroups", broadcastgroups))
    app.add_handler(CommandHandler("rewardlevels", rewardlevels))
    app.add_handler(CommandHandler("rewarduser", rewarduser))
    app.add_handler(CommandHandler("rewarddone", rewarddone))
    app.add_handler(CommandHandler("addsticker", addsticker))
    app.add_handler(CommandHandler("addautocollants", addautocollants))
    app.add_handler(CommandHandler("stickers", stickers_cmd))
    app.add_handler(CommandHandler("delstickers", delstickers))
    app.add_handler(CommandHandler("premiumusers", premiumusers))

    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
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
