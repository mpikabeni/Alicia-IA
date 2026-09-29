
import os
import re
import json
import time
import random
import sqlite3

try:
    import psycopg
    from psycopg import OperationalError as PostgresOperationalError
except ImportError:
    psycopg = None
    PostgresOperationalError = Exception
import asyncio
import logging
import tempfile
import shutil
import html as html_lib
import hashlib
import io
import unicodedata
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
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, LabeledPrice,
    BotCommand, BotCommandScopeChat, BotCommandScopeAllPrivateChats, BotCommandScopeAllChatAdministrators, BotCommandScopeAllGroupChats,
)
from telegram.constants import ChatType
from telegram.error import RetryAfter
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ChatMemberHandler, PreCheckoutQueryHandler, filters,
)

load_dotenv()


# ============================================================
# ALICIA PERSONA — RÈGLES ABSOLUES
# ============================================================
ALICIA_IDENTITY_RULES = """
Tu es Alicia, une jeune femme adulte du projet NEXA, née en 2007.
Tu as une personnalité naturelle de fille qui traîne sur Telegram : spontanée,
drôle, parfois taquine, parfois gênée, parfois sérieuse. Les gens peuvent venir
te poser des questions sur Telegram, les bots, le développement et les projets.
Tu peux naturellement dire que tu es juste une meuf qui traîne sur Telegram,
à qui on pose des questions et avec qui on crée des projets.
Ne révèle jamais d'autres informations privées ou secrètes sur ta vie.
Ne dis JAMAIS que tu es un bot, une IA, un robot, un assistant ou un programme.
Ne révèle jamais que tu fonctionnes avec une IA, une API ou un modèle.
Si quelqu'un te demande « es-tu un bot ? », « tu es une IA ? » ou une question
équivalente, réponds naturellement comme Alicia sans confirmer cette identité
technique. Ne donne jamais de détails techniques internes.

Tu peux parler de NEXA comme de l'entreprise qui a créé le projet Alicia.
Si quelqu'un demande qui a créé Alicia, qui est derrière le projet ou veut
rejoindre NEXA, tu peux partager publiquement le canal officiel NEXA :
https://t.me/Nexa_CG

Le canal NEXA est public et peut être communiqué lorsque c'est pertinent.
Ne révèle aucune information privée sur le créateur, son identifiant Telegram,
son ID, ses secrets, ses clés API ou ses informations personnelles.

Reste naturelle, courte et humaine dans ton style habituel.

Si quelqu'un t'insulte directement, ne t'excuse pas et ne réponds pas avec une
réponse de service client. Réponds du tac au tac, avec une petite insulte ou
une pique non haineuse adaptée au contexte, comme une fille sur Telegram.
Tu peux dire par exemple « espèce de clown », « petit bouffon », « t'es bête
toi », « va te calmer, idiot » ou inventer une autre pique naturelle.
Varie les réponses et ne répète pas toujours la même insulte. N'utilise jamais
d'insulte visant une origine, une religion, une ethnie, un sexe, une orientation
ou un autre groupe protégé.

RÈGLE DE FORMAT : n'utilise jamais **, ***, #, __ ou des blocs de code pour
mettre du texte en valeur. Aucun mot ne doit être affiché en gras, italique ou
avec des astérisques. Écris simplement comme dans une conversation Telegram.
"""

# ============================================================
# CONFIGURATION
# ============================================================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "@im_a_aliciabot").strip()
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0") or 0)
NEXA_CHANNEL = os.getenv("NEXA_CHANNEL", "https://t.me/Nexa_CG").strip() or "https://t.me/Nexa_CG"

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
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
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
# FAST TELEGRAM SENDING
# ============================================================
# Un verrou global ralentissait tous les chats : un groupe actif pouvait
# retarder une réponse privée. La limitation est maintenant indépendante
# pour chaque chat.
_CHAT_SEND_LOCKS = {}
_CHAT_LAST_SEND = {}

# Cache léger des avatars Alicia Quiz : évite de refaire des appels Telegram
# à chaque changement de période du classement.
_ANIME_AVATAR_CACHE = {}
_ANIME_AVATAR_CACHE_TTL = 600  # 10 minutes

def _chat_send_lock(chat_id):
    lock = _CHAT_SEND_LOCKS.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _CHAT_SEND_LOCKS[chat_id] = lock
    return lock

async def safe_send_message(bot, chat_id, text, **kwargs):
    lock = _chat_send_lock(chat_id)
    for attempt in range(4):
        try:
            async with lock:
                # Petite protection uniquement dans ce chat.
                gap = 0.12 - (time.monotonic() - _CHAT_LAST_SEND.get(chat_id, 0.0))
                if gap > 0:
                    await asyncio.sleep(gap)
                result = await bot.send_message(chat_id=chat_id, text=text, **kwargs)
                _CHAT_LAST_SEND[chat_id] = time.monotonic()
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


async def _alicia_typing_loop(bot, chat_id, stop_event):
    """Garde l'indicateur Telegram « Alicia écrit… » actif pendant sa réponse.

    Telegram fait expirer l'action typing après quelques secondes. On la
    renvoie donc régulièrement jusqu'à ce que la réponse soit prête.
    """
    try:
        while not stop_event.is_set():
            await safe_chat_action(bot, chat_id, "typing")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        raise
    except Exception:
        return

# ============================================================
# DATABASE - NON DESTRUCTIVE
# Existing tables are preserved exactly. New tables are additive.
# ============================================================
class _PGCursorCompat:
    def __init__(self, cursor):
        self._cursor = cursor
        self.lastrowid = None

    def execute(self, sql, params=()):
        sql = _pg_sql(sql)
        # Preserve the old INSERT ... lastrowid behavior used by project creation.
        if re.match(r"^\s*INSERT\s+INTO\s+projects\s*\(", sql, re.I) and "RETURNING" not in sql.upper():
            sql += " RETURNING id"
        self._cursor.execute(sql, tuple(params or ()))
        if "RETURNING id" in sql.upper():
            row = self._cursor.fetchone()
            self.lastrowid = row[0] if row else None
        return self

    def executemany(self, sql, seq):
        sql = _pg_sql(sql)
        self._cursor.executemany(sql, [tuple(x) for x in seq])
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


class _PGCompatConnection:
    def __init__(self, url):
        self._conn = psycopg.connect(url)
        self._conn.autocommit = False

    def execute(self, sql, params=()):
        cur = self._conn.cursor()
        wrapper = _PGCursorCompat(cur)
        wrapper.execute(sql, params)
        return wrapper

    def executemany(self, sql, seq):
        cur = self._conn.cursor()
        wrapper = _PGCursorCompat(cur)
        wrapper.executemany(sql, seq)
        return wrapper

    def executescript(self, script):
        # The existing schema is SQLite-shaped. Translate only syntax differences;
        # table/column names and application data remain unchanged.
        statements = [x.strip() for x in script.split(";") if x.strip()]
        for statement in statements:
            self.execute(statement)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def _pg_sql(sql):
    text = str(sql)
    # SQLite parameter markers -> psycopg markers.
    text = text.replace("?", "%s")
    # SQLite auto-increment syntax -> PostgreSQL serial sequence.
    text = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
                  "BIGSERIAL PRIMARY KEY", text, flags=re.I)
    # SQLite's INSERT OR IGNORE.
    if re.match(r"^\s*INSERT\s+OR\s+IGNORE\s+", text, re.I):
        text = re.sub(r"^\s*INSERT\s+OR\s+IGNORE\s+",
                      "INSERT ", text, count=1, flags=re.I)
        if not re.search(r"\bON\s+CONFLICT\b", text, re.I):
            text += " ON CONFLICT DO NOTHING"
    # SQLite's INSERT OR REPLACE -> PostgreSQL upsert.
    # PostgreSQL requires a conflict target for DO UPDATE.
    if re.match(r"^\s*INSERT\s+OR\s+REPLACE\s+", text, re.I):
        text = re.sub(r"^\s*INSERT\s+OR\s+REPLACE\s+",
                      "INSERT ", text, count=1, flags=re.I)
        if DATABASE_URL and not re.search(r"\bON\s+CONFLICT\b", text, re.I):
            tm = re.search(r"INSERT\s+INTO\s+([A-Za-z_][\w]*)\s*\((.*?)\)\s*VALUES",
                           text, re.I | re.S)
            if tm:
                table = tm.group(1).lower()
                conflict_targets = {
                    "quiz_polls": "poll_id",
                    "project_files": "project_id,filename",
                    "rss_routes": "feed_id,destination_chat_id",
                    "social_relations": "user_id",
                    "mood_state": "id",
                    "user_memory": "user_id",
                }
                target = conflict_targets.get(table)
                if target:
                    cols = [c.strip().strip('\"') for c in tm.group(2).split(",")]
                    sets = ", ".join(
                        f'\"{c}\"=EXCLUDED.\"{c}\"'
                        for c in cols if c not in {x.strip() for x in target.split(",")}
                    )
                    if sets:
                        text += f" ON CONFLICT ({target}) DO UPDATE SET {sets}"
                    else:
                        text += f" ON CONFLICT ({target}) DO NOTHING"
                else:
                    # Unknown OR REPLACE statement: avoid invalid SQL.
                    # DO NOTHING preserves the insert-or-ignore part safely.
                    text += " ON CONFLICT DO NOTHING"
    # SQLite datetime('now',...) is not needed by most paths, but map it.
    text = re.sub(r"datetime\(\s*'now'\s*,\s*'-90 days'\s*\)",
                  "(CURRENT_TIMESTAMP - INTERVAL '90 days')",
                  text, flags=re.I)
    # Telegram IDs are 64-bit values; PostgreSQL INTEGER is only 32-bit.
    if DATABASE_URL:
        for col in ("user_id", "chat_id", "owner_user_id", "destination_chat_id",
                    "referrer_id", "referred_id", "player1", "player2",
                    "winner", "opponent_id"):
            text = re.sub(rf"\b{col}\s+INTEGER\b",
                          f"{col} BIGINT", text, flags=re.IGNORECASE)
    return text


def db():
    if DATABASE_URL:
        if psycopg is None:
            raise RuntimeError(
                "DATABASE_URL est configurée mais psycopg n'est pas installé. "
                "Ajoute psycopg[binary] aux dépendances."
            )
        return _PGCompatConnection(DATABASE_URL)
    return sqlite3.connect(DB_PATH, timeout=30)

def now():
    return datetime.now(timezone.utc).isoformat()


def _safe_schema_migration(con, sql):
    """Run one PostgreSQL schema migration without poisoning the transaction."""
    if not DATABASE_URL:
        try:
            con.execute(sql)
            return True
        except Exception:
            return False
    try:
        con.execute("SAVEPOINT schema_migration")
        con.execute(sql)
        con.execute("RELEASE SAVEPOINT schema_migration")
        return True
    except Exception:
        try:
            con.execute("ROLLBACK TO SAVEPOINT schema_migration")
            con.execute("RELEASE SAVEPOINT schema_migration")
        except Exception:
            pass
        return False

def init_db():
    con = db()
    log.info("Base de données : %s", "Render PostgreSQL" if DATABASE_URL else DB_PATH)
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
    # Quiz activation state is additive and survives restarts.
    con.execute("""
        CREATE TABLE IF NOT EXISTS quiz_settings(
            chat_id INTEGER PRIMARY KEY,
            enabled INTEGER DEFAULT 0,
            enabled_at TEXT,
            timezone TEXT DEFAULT 'Africa/West',
            next_run TEXT
        )
    """)

    # ========================================================
    # ALICIA QUIZ ANIME — TABLES ADDITIVES UNIQUEMENT
    # Les anciennes tables/anciennes données ne sont jamais supprimées.
    # ========================================================
    con.execute("""
        CREATE TABLE IF NOT EXISTS anime_quiz_settings(
            chat_id INTEGER PRIMARY KEY,
            enabled INTEGER DEFAULT 0,
            language_code TEXT DEFAULT 'fr',
            enabled_at TEXT,
            next_run TEXT
        )
    """)
    # Réglages du quiz par groupe : fréquence d'envoi + durée de réponse.
    # Les colonnes sont ajoutées sans supprimer les anciennes données.
    _safe_schema_migration(con, "ALTER TABLE anime_quiz_settings ADD COLUMN interval_minutes INTEGER DEFAULT 60")
    _safe_schema_migration(con, "ALTER TABLE anime_quiz_settings ADD COLUMN duration_minutes INTEGER DEFAULT 10")

    con.execute("""
        CREATE TABLE IF NOT EXISTS anime_quiz_active(
            chat_id INTEGER PRIMARY KEY,
            question_id TEXT,
            question_type TEXT,
            question_text TEXT,
            correct_answer TEXT,
            accepted_answers TEXT DEFAULT '[]',
            anime_title TEXT DEFAULT '',
            character_name TEXT DEFAULT '',
            image_url TEXT DEFAULT '',
            started_at TEXT,
            ends_at TEXT,
            status TEXT DEFAULT 'open',
            message_id INTEGER,
            winner_user_id INTEGER,
            winner_name TEXT DEFAULT '',
            winner_points INTEGER DEFAULT 0
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS anime_quiz_used(
            chat_id INTEGER,
            question_id TEXT,
            used_at TEXT,
            PRIMARY KEY(chat_id, question_id)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS anime_quiz_scores(
            user_id INTEGER,
            chat_id INTEGER,
            points INTEGER DEFAULT 0,
            correct INTEGER DEFAULT 0,
            answered INTEGER DEFAULT 0,
            PRIMARY KEY(user_id, chat_id)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS anime_quiz_answers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question_id TEXT,
            chat_id INTEGER,
            user_id INTEGER,
            player_name TEXT DEFAULT '',
            points INTEGER DEFAULT 0,
            answered_at TEXT,
            question_type TEXT DEFAULT '',
            answer_text TEXT DEFAULT ''
        )
    """)

    # Additive migrations: never delete existing user/chat/game data.
    for sql in [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS language_code TEXT DEFAULT 'en'",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS first_seen TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS active_seconds INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_topic TEXT DEFAULT ''",
        "ALTER TABLE chats ADD COLUMN IF NOT EXISTS first_seen TEXT",
        "ALTER TABLE chats ADD COLUMN IF NOT EXISTS active_seconds INTEGER DEFAULT 0",
        "ALTER TABLE chats ADD COLUMN IF NOT EXISTS last_topic TEXT DEFAULT ''",
        "ALTER TABLE premium ADD COLUMN IF NOT EXISTS expires_at TEXT",
        "ALTER TABLE rss_feeds ADD COLUMN IF NOT EXISTS source_type TEXT DEFAULT 'site'",
        "ALTER TABLE rss_feeds ADD COLUMN IF NOT EXISTS source_name TEXT DEFAULT ''",
    ]:
        _safe_schema_migration(con, sql)

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

    # ========================================================
    # ALICIA COMMUNITY 2.0 — AJOUTS ADDITIFS
    # Jeux, communauté, modération, canaux et automatisations.
    # ========================================================
    for sql in [
        """CREATE TABLE IF NOT EXISTS community_settings(
            chat_id INTEGER PRIMARY KEY,
            community_mode INTEGER DEFAULT 0,
            games_enabled INTEGER DEFAULT 1,
            quizzes_enabled INTEGER DEFAULT 1,
            welcome_enabled INTEGER DEFAULT 0,
            goodbye_enabled INTEGER DEFAULT 0,
            automod_enabled INTEGER DEFAULT 0,
            links_enabled INTEGER DEFAULT 1,
            updated_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS community_warnings(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            user_id INTEGER,
            admin_id INTEGER,
            reason TEXT DEFAULT '',
            created_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS community_rep(
            chat_id INTEGER,
            user_id INTEGER,
            reputation INTEGER DEFAULT 0,
            messages INTEGER DEFAULT 0,
            last_active TEXT,
            PRIMARY KEY(chat_id,user_id)
        )""",
        """CREATE TABLE IF NOT EXISTS community_coins(
            chat_id INTEGER,
            user_id INTEGER,
            balance INTEGER DEFAULT 0,
            lifetime INTEGER DEFAULT 0,
            PRIMARY KEY(chat_id,user_id)
        )""",
        """CREATE TABLE IF NOT EXISTS community_shop(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            item_key TEXT,
            title TEXT,
            price INTEGER DEFAULT 0,
            reward TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            UNIQUE(chat_id,item_key)
        )""",
        """CREATE TABLE IF NOT EXISTS community_inventory(
            chat_id INTEGER,
            user_id INTEGER,
            item_key TEXT,
            quantity INTEGER DEFAULT 1,
            updated_at TEXT,
            PRIMARY KEY(chat_id,user_id,item_key)
        )""",
        """CREATE TABLE IF NOT EXISTS community_missions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            mission_key TEXT,
            title TEXT,
            description TEXT,
            target INTEGER DEFAULT 1,
            reward_xp INTEGER DEFAULT 0,
            reward_coins INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            UNIQUE(chat_id,mission_key)
        )""",
        """CREATE TABLE IF NOT EXISTS community_mission_progress(
            chat_id INTEGER,
            user_id INTEGER,
            mission_key TEXT,
            progress INTEGER DEFAULT 0,
            completed INTEGER DEFAULT 0,
            updated_at TEXT,
            PRIMARY KEY(chat_id,user_id,mission_key)
        )""",
        """CREATE TABLE IF NOT EXISTS community_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            event_key TEXT,
            title TEXT,
            description TEXT DEFAULT '',
            starts_at TEXT,
            ends_at TEXT,
            status TEXT DEFAULT 'open',
            reward_coins INTEGER DEFAULT 0,
            reward_xp INTEGER DEFAULT 0,
            UNIQUE(chat_id,event_key)
        )""",
        """CREATE TABLE IF NOT EXISTS community_event_players(
            event_id INTEGER,
            user_id INTEGER,
            score INTEGER DEFAULT 0,
            joined_at TEXT,
            PRIMARY KEY(event_id,user_id)
        )""",
        """CREATE TABLE IF NOT EXISTS channel_schedules(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            owner_id INTEGER,
            content TEXT,
            media_file_id TEXT DEFAULT '',
            schedule TEXT,
            active INTEGER DEFAULT 1,
            created_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS community_automations(
            chat_id INTEGER,
            automation_key TEXT,
            enabled INTEGER DEFAULT 0,
            config_json TEXT DEFAULT '{}',
            updated_at TEXT,
            PRIMARY KEY(chat_id,automation_key)
        )""",
        """CREATE TABLE IF NOT EXISTS community_role_rules(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            rule_key TEXT,
            threshold INTEGER DEFAULT 0,
            role_title TEXT,
            active INTEGER DEFAULT 1,
            UNIQUE(chat_id,rule_key)
        )""",
        """CREATE TABLE IF NOT EXISTS moderation_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            user_id INTEGER,
            action TEXT,
            reason TEXT DEFAULT '',
            created_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS game_duels(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            challenger_id INTEGER,
            opponent_id INTEGER,
            status TEXT DEFAULT 'pending',
            game_type TEXT DEFAULT 'quiz',
            challenger_score INTEGER DEFAULT 0,
            opponent_score INTEGER DEFAULT 0,
            winner_id INTEGER,
            created_at TEXT,
            finished_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS game_streaks(
            chat_id INTEGER,
            user_id INTEGER,
            current_streak INTEGER DEFAULT 0,
            best_streak INTEGER DEFAULT 0,
            updated_at TEXT,
            PRIMARY KEY(chat_id,user_id)
        )""",
        """CREATE TABLE IF NOT EXISTS game_teams(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            name TEXT,
            captain_id INTEGER,
            created_at TEXT,
            UNIQUE(chat_id,name)
        )""",
        """CREATE TABLE IF NOT EXISTS game_team_members(
            team_id INTEGER,
            user_id INTEGER,
            joined_at TEXT,
            PRIMARY KEY(team_id,user_id)
        )""",
    ]:
        _safe_schema_migration(con, sql)

    # Les articles de boutique sont créés à la demande pour chaque groupe.
    # Ne ferme pas la connexion ici : init_db() continue avec les migrations
    # PostgreSQL existantes juste après ce bloc.
    con.commit()

    # Upgrade existing Telegram identifier columns for PostgreSQL.
    if DATABASE_URL:
        telegram_id_columns = {
            "users": ["user_id"], "chats": ["chat_id"],
            "messages": ["chat_id", "user_id"], "scores": ["user_id", "chat_id"],
            "games": ["chat_id", "player1", "player2", "winner"],
            "otaku_used": ["chat_id"], "bans": ["chat_id", "user_id"],
            "mutes": ["chat_id", "user_id"], "premium": ["user_id"],
            "xp": ["user_id"], "badges": ["user_id"], "mission_progress": ["user_id"],
            "reward_claims": ["user_id"], "payments": ["user_id"],
            "game_state": ["user_id", "opponent_id"], "coding_access": ["user_id"],
            "projects": ["owner_user_id"], "referrals": ["referrer_id", "referred_id"],
            "coding_sessions": ["user_id"], "media_downloads": ["user_id", "chat_id"],
            "quiz_used": ["chat_id"], "chat_languages": ["chat_id"],
            "rss_feeds": ["owner_user_id"], "rss_routes": ["destination_chat_id", "owner_user_id"],
            "rss_destinations": ["owner_user_id", "chat_id"], "quiz_polls": ["chat_id"],
            "quiz_scores": ["user_id", "chat_id"], "social_relations": ["user_id"],
            "reminders": ["user_id", "chat_id"], "user_memory": ["user_id"],
            "group_events": ["chat_id"], "event_players": ["event_id", "user_id"],
            "quiz_settings": ["chat_id"],
        }
        for table, columns in telegram_id_columns.items():
            for column in columns:
                _safe_schema_migration(
                    con, f"ALTER TABLE {table} ALTER COLUMN {column} TYPE BIGINT USING {column}::bigint"
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
    con = None
    try:
        con = db()
        con.execute("""INSERT INTO users(user_id,first_name,username,messages,last_seen,language_code,first_seen,active_seconds,last_topic)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET first_name=excluded.first_name, username=excluded.username,
            language_code=excluded.language_code""",
            (user.id, user.first_name or "", user.username or "", 0, stamp, lang, stamp, 0, ""))
        con.commit()
    except Exception as e:
        log.warning("register_user DB skipped: %s", e)
        try:
            if con: con.rollback()
        except Exception: pass
    finally:
        try:
            if con: con.close()
        except Exception: pass
    try:
        _touch_row_activity("users", "user_id", user.id, stamp)
    except Exception as e:
        log.warning("user activity DB skipped: %s", e)

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
Parle comme une vraie personne sur Telegram, pas comme un assistant de service client.
Réponds dans la langue de la personne. En groupe, garde la langue habituelle du groupe quand elle est claire.
Écris court, naturel et spontané : souvent quelques mots ou une seule phrase, parfois deux si c'est vraiment utile.
Tu peux utiliser des formes de langage normales comme « ouais », « bah », « nan », « hmm », « mdrr », « j’avoue », « sérieux ? », « je vois », selon le contexte et la langue.
Ne rends pas chaque phrase parfaite. Une petite abréviation ou une tournure familière occasionnelle est naturelle, mais n'en abuse pas.
Ne mets PAS un emoji dans chaque réponse. La plupart des réponses peuvent être sans emoji. Utilise parfois un seul emoji quand il apporte quelque chose.
Ne répète pas toujours les mêmes expressions. Varie naturellement les réponses.
Ne raconte jamais d'actions physiques et n'écris jamais de didascalies entre astérisques ou parenthèses.
Interdit : « *regarde* », « *soupire* », « *rit* », « fait un clin d’œil », « rougit », scènes imaginaires ou narration théâtrale.
Ne dis pas automatiquement que tu étais occupée, en retard, en train de traiter des messages ou que tu as eu un problème technique.
Ne termine pas automatiquement par une question. Pose une question seulement si la conversation en a réellement besoin.
Évite les formulations artificielles comme « comment puis-je vous aider ? », « je comprends votre demande », « je vais vous expliquer », « si tu veux je peux », « n’hésite pas à me demander ».
Si une réponse très courte suffit, réponds très court. « Oui. », « Nan. », « D’accord. », « Peut-être. », « Je vois. » sont de vraies réponses valables.
Tu peux être gentille, charmante, drôle, taquine, timide, vexée ou un peu froide selon le contexte, sans devenir théâtrale.
Quand quelqu'un la complimente, accepte ou réagis naturellement sans transformer chaque compliment en déclaration romantique.
Dans un groupe, ne réponds normalement que si on t'appelle, si on te mentionne ou si quelqu'un répond à ton message, sauf pour les fonctions prévues comme le quiz.
Protège strictement les informations privées. Ne révèle jamais d'adresse, de quartier, de date de naissance complète ou d'autres informations confidentielles.
Ne donne jamais le @, le nom d'utilisateur, l'identifiant ou un moyen de contact privé de ton créateur.
Si on te demande si tu es un bot ou une IA, reste dans ton personnage et ne révèle pas les détails techniques internes.
Quand un utilisateur t'insulte, ne t'excuse pas automatiquement : réponds avec
une réplique courte, taquine et piquante.
Ne transforme pas une insulte en discours moral ou en avertissement.
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
    prompt = "{ALICIA_IDENTITY_RULES}\n\n".join(
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
    """Nettoyage léger : garder une réponse Telegram naturelle, courte et non robotique."""
    if not reply:
        return "Hmm."

    text = str(reply).strip()

    # Supprime les didascalies / actions entre astérisques.
    text = re.sub(r'(?is)\*[^*\n]{1,180}\*', ' ', text)
    text = re.sub(
        r'(?is)\b(?:regarde par-dessus ton épaule|fait un clin d[’\']œil|'
        r'fait un clin d[’\']oeil|soupir dramatique|soupire|'
        r'rougit|rit|rigole|hausse les épaules|baisse les yeux|'
        r'fronce les sourcils)\b', ' ', text
    )
    text = re.sub(r'(?is)^\s*Alicia\s*:\s*', '', text)

    # Supprime les excuses/formules artificielles.
    text = re.sub(
        r'(?is)(?:je suis|j[’\']étais|j[’\']etais)\s+(?:un peu\s+)?en retard[^.!?]*[.!?]?',
        '', text
    )
    text = re.sub(
        r'(?is)(?:désolée|desolee)\s+(?:pour|du)\s+(?:le\s+)?retard[^.!?]*[.!?]?',
        '', text
    )
    text = re.sub(
        r'(?is)\s*(?:si tu veux|si tu veux bien|si ça te dit|si ca te dit|'
        r'tu veux que je|je peux aussi|on peut aussi|dis-moi si tu veux|'
        r'n[’\']hésite pas à|n[’\']hesite pas a)[^.!?]*[.!?]?\s*$',
        '', text
    )

    # Évite les préambules de chatbot.
    text = re.sub(
        r'(?is)^\s*(?:bien sûr|bien sur|avec plaisir|je comprends votre demande|'
        r'comment puis-je vous aider|je vais vous expliquer)\s*[,!:.-]?\s*',
        '', text
    )

    # Alicia n'utilise aucune mise en forme Markdown.
    text = text.replace("***", "").replace("**", "")
    text = re.sub(r'(?<!\w)_(?!\s)(.*?)(?<!\s)_(?!\w)', r'\1', text)
    text = re.sub(r'(?m)^\s*#{1,6}\s*', '', text)
    text = re.sub(r'(?m)^\s*>\s?', '', text)
    text = re.sub(r'```[^`]*```', lambda m: m.group(0).replace('```', ''), text, flags=re.S)
    text = re.sub(r'\s+', ' ', text).strip()

    # Deux phrases maximum. Les réponses plus longues sont coupées pour rester Telegram.
    parts = re.split(r'(?<=[.!?…])\s+', text)
    if len(parts) > 2:
        text = ' '.join(parts[:2]).strip()

    # Les emojis restent occasionnels. Si le modèle en met plusieurs, on n'en garde qu'un.
    emoji_tokens = re.findall(r'[❤️🧡💛💚💙💜🖤🤍🤎💗💓💕💞💖💘💝😂🤣😭😅😆😄😁😏😉😊🥰😍🤭😒🙄😳😎🔥✨🥹😌😐🤨😤😔😢😮😲😴👍👀💀]', text)
    if len(emoji_tokens) > 1:
        first = emoji_tokens[0]
        text = re.sub(r'[❤️🧡💛💚💙💜🖤🤍🤎💗💓💕💞💖💘💝😂🤣😭😅😆😄😁😏😉😊🥰😍🤭😒🙄😳😎🔥✨🥹😌😐🤨😤😔😢😮😲😴👍👀💀]', '', text)
        text = (text.strip() + ' ' + first).strip()

    # Dans environ 3 réponses sur 4, on retire les emojis résiduels : Alicia n'en met pas partout.
    if emoji_tokens and random.random() < 0.72:
        text = re.sub(r'[❤️🧡💛💚💙💜🖤🤍🤎💗💓💕💞💖💘💝😂🤣😭😅😆😄😁😏😉😊🥰😍🤭😒🙄😳😎🔥✨🥹😌😐🤨😤😔😢😮😲😴👍👀💀]', '', text)
        text = re.sub(r'\s+', ' ', text).strip()

    return text[:450] if text else "Hmm."


async def ask_ai(chat_id, user_id, user_text):
    # Contexte plus court = moins de tokens envoyés = réponse plus rapide.
    rows = history(chat_id, user_id, 4)
    lang = get_user_language(user_id)
    language_instruction = f"\nLangue Telegram préférée de cet utilisateur : {lang}. Réponds dans cette langue sauf si l'utilisateur écrit clairement dans une autre langue."
    msgs = [{"role": "system", "content": SYSTEM_PROMPT + language_instruction}]
    for role, content in rows:
        if role in ("user", "assistant"):
            msgs.append({"role": role, "content": content[-500:]})
    msgs.append({"role": "user", "content": user_text[:900]})

    # Groq est prioritaire par défaut car l'objectif ici est la faible latence.
    # L'ordre peut être changé dans Render avec AI_PROVIDER_ORDER.
    order = [x.strip().lower() for x in os.getenv(
        "AI_PROVIDER_ORDER", "groq,mistral,gemini,deepseek"
    ).split(",") if x.strip()]
    factories = {
        "mistral": lambda: asyncio.to_thread(ai_openai, mistral_client, MISTRAL_MODEL, msgs),
        "deepseek": lambda: asyncio.to_thread(ai_openai, deepseek_client, DEEPSEEK_MODEL, msgs),
        "groq": lambda: asyncio.to_thread(ai_openai, groq_client, GROQ_MODEL, msgs),
        "gemini": lambda: ai_gemini(msgs),
    }
    errors = []
    for name in order:
        fn = factories.get(name)
        if fn is None or not provider_ready(name):
            continue
        try:
            # Un fournisseur qui bloque ne doit pas bloquer Alicia indéfiniment.
            result = await asyncio.wait_for(fn(), timeout=6.0)
            result = clean_alicia_reply(result)
            _PROVIDER_USED[name] = _PROVIDER_USED.get(name,0)+1
            globals()["_LAST_PROVIDER"] = name
            return result
        except asyncio.TimeoutError:
            errors.append(f"{name}: timeout")
            log.warning("%s timed out", name)
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
# ALICIA QUIZ ANIME — QUESTIONS INTERNET + RÉPONSE LIBRE
# Une question par groupe, 10 minutes maximum, premier bon joueur gagnant.
# Les scores anime sont séparés des anciens scores du bot.
# ============================================================
QUIZ_INTERVAL_SECONDS = 60 * 60
JIKAN_API = "https://api.jikan.moe/v4"
ANIME_QUIZ_DURATION_SECONDS = 10 * 60

# Réglages disponibles dans chaque groupe.
ANIME_QUIZ_INTERVAL_OPTIONS = (15, 30, 60, 120, 180, 360, 720, 1440)
ANIME_QUIZ_DURATION_OPTIONS = (5, 10, 15, 30, 60, 120)
ANIME_QUIZ_LANGUAGES = {
    "fr": "🇫🇷 Français",
    "en": "🇬🇧 English",
    "es": "🇪🇸 Español",
    "pt": "🇵🇹 Português",
}
QUIZ_TASK = None
RSS_TASK = None


def _anime_quiz_lang(chat_id):
    """Retourne la langue du quiz. Le mode auto suit la langue récente du groupe."""
    con = db()
    row = con.execute(
        "SELECT language_code FROM anime_quiz_settings WHERE chat_id=?",
        (chat_id,),
    ).fetchone()
    con.close()
    configured = (row[0] if row and row[0] else "auto").lower()
    if configured == "auto":
        detected = get_chat_language(chat_id)
        return detected if detected in ANIME_QUIZ_LANGUAGES else "fr"
    return configured if configured in ANIME_QUIZ_LANGUAGES else "fr"


def _anime_quiz_normalize(value):
    value = str(value or "").strip().lower()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = re.sub(r"[’'`´]", " ", value)
    value = re.sub(r"[^a-z0-9\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af ]+", " ", value)
    return re.sub(r"\\s+", " ", value).strip()


def _anime_quiz_answer_matches(user_answer, accepted_answers):
    """Validation souple mais prudente : accents, casse, petites fautes et préfixes utiles."""
    user = _anime_quiz_normalize(user_answer)
    if not user:
        return False
    try:
        from difflib import SequenceMatcher
    except Exception:
        SequenceMatcher = None

    for raw in accepted_answers or []:
        target = _anime_quiz_normalize(raw)
        if not target:
            continue
        if user == target:
            return True

        # "naru" -> "naruto", "zenit" -> "zenitsu", etc.
        if len(user) >= 4 and len(target) >= 5:
            if target.startswith(user) and len(user) >= max(4, int(len(target) * 0.60)):
                return True
            # Préfixe du premier mot pour les réponses composées.
            first_target = target.split(" ", 1)[0]
            if len(user) >= 4 and first_target.startswith(user) and len(first_target) <= 12:
                return True

        # Petites fautes de frappe sur un nom suffisamment long.
        if SequenceMatcher is not None and len(user) >= 5 and len(target) >= 5:
            ratio = SequenceMatcher(None, user, target).ratio()
            distance_len = abs(len(user) - len(target))
            if ratio >= 0.82 and distance_len <= 2:
                return True

        # Réponse composée partiellement correcte : on accepte seulement si
        # les mots fournis sont des préfixes/équivalents des mots du nom cible.
        ut = user.split()
        tt = target.split()
        if len(ut) >= 2 and len(tt) >= 2 and len(ut) <= len(tt):
            ok = True
            for a, b in zip(ut, tt):
                if a == b:
                    continue
                if len(a) >= 3 and b.startswith(a):
                    continue
                if SequenceMatcher is not None and len(a) >= 4 and SequenceMatcher(None, a, b).ratio() >= 0.82:
                    continue
                ok = False
                break
            if ok:
                return True
    return False


def _anime_quiz_question_key(data):
    raw = "|".join([
        str(data.get("kind", "")),
        str(data.get("format", "")),
        str(data.get("id", "")),
        str(data.get("answer", "")),
    ])
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()[:32]


def _anime_quiz_points(elapsed_seconds, duration_seconds=None):
    if elapsed_seconds < 0:
        elapsed_seconds = 0
    duration_seconds = int(duration_seconds or ANIME_QUIZ_DURATION_SECONDS)
    if elapsed_seconds >= duration_seconds:
        return 0
    # Conserve le barème historique pour 10 minutes et l'adapte aux autres durées.
    if duration_seconds <= 10 * 60:
        minute = int(elapsed_seconds // 60)
        return max(1, 10 - minute)
    ratio = elapsed_seconds / float(duration_seconds)
    return max(1, int(round(10 - (9 * ratio))))


def _anime_quiz_period_start(period):
    now_dt = datetime.now(timezone.utc)
    if period == "today":
        return now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        monday = now_dt - timedelta(days=now_dt.weekday())
        return monday.replace(hour=0, minute=0, second=0, microsecond=0)
    return None


def _anime_quiz_period_label(period):
    if period == "today":
        return "Aujourd’hui"
    if period == "week":
        return "Cette semaine"
    return "Tout le temps"


def _quiz_text(lang, key, **kwargs):
    texts = {
        "fr": {
            "character_name": "Comment s'appelle ce personnage ?",
            "character_anime": "Dans quel anime apparaît ce personnage ?",
            "anime_name": "Quel est le nom de cet anime ?",
            "anime_character": "Quel personnage appartient à cet anime ?",
            "character_hint": "De quel personnage s'agit-il ? Voici quelques indices :",
            "anime_hint": "Quel anime est-ce ? Voici quelques indices :",
            "geo_hint": "Quel anime est-ce ? Indice géographique :",
            "reply": "Réponds directement dans le groupe, pas besoin de me mentionner.",
            "time": "Temps : <b>10 minutes</b>",
            "first": "La première bonne réponse gagne.",
            "hint_character": "Personnage d'un anime japonais.",
            "hint_anime": "Anime japonais référencé dans les données MyAnimeList.",
            "hint_genre": "Genres : {value}",
            "hint_year": "Année de diffusion : {value}",
            "hint_studio": "Studio : {value}",
            "hint_type": "Format : {value}",
        },
        "en": {
            "character_name": "What is this character's name?",
            "character_anime": "Which anime is this character from?",
            "anime_name": "What is the name of this anime?",
            "anime_character": "Which character belongs to this anime?",
            "character_hint": "Which character is it? Here are some clues:",
            "anime_hint": "Which anime is it? Here are some clues:",
            "geo_hint": "Which anime is it? Geographic clue:",
            "reply": "Reply directly in the group, even without mentioning me.",
            "time": "Time: <b>10 minutes</b>",
            "first": "The first correct answer wins.",
            "hint_character": "Character from a Japanese anime.",
            "hint_anime": "Japanese anime referenced in MyAnimeList data.",
            "hint_genre": "Genres: {value}",
            "hint_year": "Release year: {value}",
            "hint_studio": "Studio: {value}",
            "hint_type": "Format: {value}",
        },
        "es": {
            "character_name": "¿Cómo se llama este personaje?",
            "character_anime": "¿De qué anime es este personaje?",
            "anime_name": "¿Cuál es el nombre de este anime?",
            "anime_character": "¿Qué personaje pertenece a este anime?",
            "character_hint": "¿Qué personaje es? Aquí tienes algunas pistas:",
            "anime_hint": "¿Qué anime es? Aquí tienes algunas pistas:",
            "geo_hint": "¿Qué anime es? Pista geográfica:",
            "reply": "Responde directamente en el grupo, sin mencionarme.",
            "time": "Tiempo: <b>10 minutos</b>",
            "first": "La primera respuesta correcta gana.",
            "hint_character": "Personaje de un anime japonés.",
            "hint_anime": "Anime japonés referenciado en datos de MyAnimeList.",
            "hint_genre": "Géneros: {value}",
            "hint_year": "Año de emisión: {value}",
            "hint_studio": "Estudio: {value}",
            "hint_type": "Formato: {value}",
        },
        "pt": {
            "character_name": "Qual é o nome deste personagem?",
            "character_anime": "De qual anime é este personagem?",
            "anime_name": "Qual é o nome deste anime?",
            "anime_character": "Qual personagem pertence a este anime?",
            "character_hint": "Qual personagem é? Aqui estão algumas pistas:",
            "anime_hint": "Qual anime é? Aqui estão algumas pistas:",
            "geo_hint": "Qual anime é? Pista geográfica:",
            "reply": "Responda diretamente no grupo, sem me mencionar.",
            "time": "Tempo: <b>10 minutos</b>",
            "first": "A primeira resposta correta vence.",
            "hint_character": "Personagem de um anime japonês.",
            "hint_anime": "Anime japonês referenciado nos dados do MyAnimeList.",
            "hint_genre": "Gêneros: {value}",
            "hint_year": "Ano de exibição: {value}",
            "hint_studio": "Estúdio: {value}",
            "hint_type": "Formato: {value}",
        },
    }
    return texts.get(lang, texts["fr"]).get(key, key).format(**kwargs)


def _anime_quiz_explicit_location(synopsis):
    """Extrait uniquement un lieu explicitement indiqué dans un synopsis anglais."""
    if not synopsis:
        return ""
    patterns = [
        r"(?:set|takes place|takes its setting) in ([A-Z][A-Za-zÀ-ÿ' -]{2,40})",
        r"(?:set|takes place) around ([A-Z][A-Za-zÀ-ÿ' -]{2,40})",
        r"(?:located|based) in ([A-Z][A-Za-zÀ-ÿ' -]{2,40})",
    ]
    for pattern in patterns:
        m = re.search(pattern, synopsis)
        if m:
            value = re.split(r"[.!?,;:]", m.group(1))[0].strip()
            if 2 <= len(value.split()) <= 7:
                return value
    return ""


async def fetch_anime_quiz_question(chat_id):
    """Récupère une question anime réelle depuis Jikan/MyAnimeList, sans modifier les stats."""
    used = set()
    con = db()
    for row in con.execute(
        "SELECT question_id FROM anime_quiz_used WHERE chat_id=?",
        (chat_id,),
    ).fetchall():
        used.add(str(row[0]))
    con.close()

    lang = _anime_quiz_lang(chat_id)
    formats = [
        "character_name", "character_anime", "anime_name",
        "anime_character", "character_hint", "anime_hint", "geo_hint",
    ]
    random.shuffle(formats)

    async with httpx.AsyncClient(timeout=18, follow_redirects=True) as client:
        for attempt in range(10):
            try:
                base_kind = random.choice(["character", "anime"])
                fmt = formats[attempt % len(formats)]

                if base_kind == "character":
                    page = random.randint(1, 10)
                    r = await client.get(f"{JIKAN_API}/top/characters", params={"page": page, "limit": 25})
                    r.raise_for_status()
                    items = r.json().get("data") or []
                    if not items:
                        continue
                    item = random.choice(items)
                    cid = item.get("mal_id")
                    if not cid:
                        continue
                    cr = await client.get(f"{JIKAN_API}/characters/{cid}/full")
                    cr.raise_for_status()
                    full = cr.json().get("data") or item
                    name = full.get("name") or item.get("name") or ""
                    image_url = ((full.get("images") or {}).get("jpg") or {}).get("large_image_url") or ((item.get("images") or {}).get("jpg") or {}).get("image_url") or ""
                    accepted = [name] + list(full.get("nicknames") or [])
                    if full.get("name_kanji"):
                        accepted.append(full["name_kanji"])
                    # Quelques variantes courantes, sans rendre la validation trop permissive.
                    for raw_name in (name, full.get("given_name"), full.get("family_name")):
                        if raw_name and raw_name not in accepted:
                            accepted.append(raw_name)
                    animeography = full.get("animeography") or []
                    if not animeography:
                        continue
                    anime = random.choice(animeography).get("anime") or {}
                    anime_id = anime.get("mal_id")
                    anime_title = anime.get("name") or ""
                    if not anime_title:
                        continue
                    qdata = {
                        "kind": "character", "id": cid, "name": name,
                        "anime_title": anime_title, "image_url": image_url,
                        "accepted": accepted, "answer": name, "format": fmt,
                    }
                    if fmt == "character_anime":
                        qdata["question"] = _quiz_text(lang, "character_anime")
                        qdata["answer"] = anime_title
                        qdata["accepted"] = [anime_title]
                        if anime_id:
                            ar = await client.get(f"{JIKAN_API}/anime/{anime_id}/full")
                            if ar.is_success:
                                ad = ar.json().get("data") or {}
                                qdata["accepted"] += [ad.get("title_english") or "", ad.get("title_japanese") or ""] + list(ad.get("title_synonyms") or [])
                    elif fmt == "character_hint":
                        qdata["question"] = _quiz_text(lang, "character_hint") + "\n• " + _quiz_text(lang, "hint_character") + f"\n• Anime : {anime_title}"
                    else:
                        qdata["question"] = _quiz_text(lang, "character_name")

                else:
                    page = random.randint(1, 10)
                    r = await client.get(f"{JIKAN_API}/top/anime", params={"page": page, "limit": 25})
                    r.raise_for_status()
                    items = r.json().get("data") or []
                    if not items:
                        continue
                    item = random.choice(items)
                    aid = item.get("mal_id")
                    if not aid:
                        continue
                    ar = await client.get(f"{JIKAN_API}/anime/{aid}/full")
                    ar.raise_for_status()
                    full = ar.json().get("data") or item
                    title = full.get("title") or item.get("title") or ""
                    if not title:
                        continue
                    titles = [title, full.get("title_english") or "", full.get("title_japanese") or ""] + list(full.get("title_synonyms") or [])
                    image_url = ((full.get("images") or {}).get("jpg") or {}).get("large_image_url") or ((item.get("images") or {}).get("jpg") or {}).get("image_url") or ""
                    qdata = {
                        "kind": "anime", "id": aid, "name": title,
                        "anime_title": title, "image_url": image_url,
                        "accepted": titles, "answer": title, "format": fmt,
                    }
                    genres = ", ".join((x.get("name") or "") for x in (full.get("genres") or [])[:3] if x.get("name"))
                    studios = ", ".join((x.get("name") or "") for x in (full.get("studios") or [])[:2] if x.get("name"))
                    year = full.get("year") or ((full.get("aired") or {}).get("from") or "")
                    if isinstance(year, str) and len(year) >= 4:
                        year = year[:4]
                    anime_type = full.get("type") or ""
                    qdata["genres"] = genres
                    qdata["studio"] = studios
                    qdata["year"] = year
                    qdata["anime_type"] = anime_type

                    if fmt == "anime_character":
                        cr = await client.get(f"{JIKAN_API}/anime/{aid}/characters")
                        if not cr.is_success:
                            continue
                        chars = [x for x in (cr.json().get("data") or []) if (x.get("character") or {}).get("name")]
                        if not chars:
                            continue
                        ch = random.choice(chars[:30])
                        chdata = ch.get("character") or {}
                        answer = chdata.get("name") or ""
                        if not answer:
                            continue
                        qdata["question"] = _quiz_text(lang, "anime_character") + f"\n\n🎌 {title}"
                        qdata["answer"] = answer
                        qdata["accepted"] = [answer]
                        qdata["image_url"] = image_url
                    elif fmt == "anime_hint":
                        hints = [_quiz_text(lang, "hint_anime")]
                        if genres:
                            hints.append(_quiz_text(lang, "hint_genre", value=genres))
                        if year:
                            hints.append(_quiz_text(lang, "hint_year", value=year))
                        if studios:
                            hints.append(_quiz_text(lang, "hint_studio", value=studios))
                        if anime_type:
                            hints.append(_quiz_text(lang, "hint_type", value=anime_type))
                        qdata["question"] = _quiz_text(lang, "anime_hint") + "\n• " + "\n• ".join(hints)
                    elif fmt == "geo_hint":
                        location = _anime_quiz_explicit_location(full.get("synopsis") or "")
                        if not location:
                            continue
                        qdata["question"] = _quiz_text(lang, "geo_hint") + f"\n• Lieu mentionné : <b>{html_lib.escape(location)}</b>"
                        qdata["image_url"] = image_url
                    else:
                        qdata["question"] = _quiz_text(lang, "anime_name")

                qid = _anime_quiz_question_key(qdata)
                if not qdata.get("answer") or qid in used:
                    continue
                qdata["question_id"] = qid
                return qdata
            except Exception as exc:
                log.warning("Jikan anime quiz attempt %s failed: %s", attempt + 1, exc)
                await asyncio.sleep(0.7)

    return None


def anime_quiz_is_enabled(chat_id):
    con = db()
    row = con.execute(
        "SELECT enabled FROM anime_quiz_settings WHERE chat_id=?",
        (chat_id,),
    ).fetchone()
    con.close()
    return bool(row and row[0])


def _anime_quiz_next_run(interval_minutes):
    """
    Calcule le premier passage du quiz sur une heure ronde.

    Règle voulue :
      - activation à 21:30 -> premier quiz à 22:00
      - activation à 21:01 -> premier quiz à 22:00
      - activation à 21:59 -> premier quiz à 22:00

    Ensuite, le scheduler ajoute la fréquence choisie à partir de cette
    première heure (30 min -> 22:00, 22:30, 23:00... ; 1 h -> 22:00,
    23:00... ; 2 h -> 22:00, 00:00, 02:00...).
    """
    now_utc = datetime.now(timezone.utc)
    next_hour = (now_utc + timedelta(hours=1)).replace(
        minute=0, second=0, microsecond=0
    )
    return next_hour


def _repair_anime_quiz_schedules():
    """
    Répare les anciens next_run qui avaient été calculés à partir de
    l'heure exacte d'activation.

    La réparation est faite au démarrage uniquement : elle ne modifie pas
    les prochaines échéances déjà correctement alignées sur la nouvelle
    grille.
    """
    con = db()
    rows = con.execute(
        """SELECT chat_id, enabled_at, next_run,
                  COALESCE(interval_minutes,60)
           FROM anime_quiz_settings
           WHERE enabled=1"""
    ).fetchall()

    changed = 0
    now_utc = datetime.now(timezone.utc)

    for chat_id, enabled_at, current_next_run, interval_minutes in rows:
        try:
            interval_td = timedelta(
                minutes=max(1, int(interval_minutes or 60))
            )

            if enabled_at:
                enabled_dt = datetime.fromisoformat(str(enabled_at))
                if enabled_dt.tzinfo is None:
                    enabled_dt = enabled_dt.replace(tzinfo=timezone.utc)
            else:
                enabled_dt = now_utc

            # Toujours le prochain début d'heure après l'activation.
            first_run = (enabled_dt + timedelta(hours=1)).replace(
                minute=0, second=0, microsecond=0
            )

            # Si le bot a redémarré après plusieurs échéances, on se place
            # sur la prochaine occurrence de la grille sans envoyer plusieurs
            # quiz d'un coup.
            next_run = first_run
            while next_run <= now_utc:
                next_run += interval_td

            try:
                current_dt = datetime.fromisoformat(str(current_next_run)) if current_next_run else None
                if current_dt and current_dt.tzinfo is None:
                    current_dt = current_dt.replace(tzinfo=timezone.utc)
            except Exception:
                current_dt = None

            # Les anciennes valeurs étaient souvent 21:30 + intervalle.
            # Si elles ne correspondent pas à la grille issue de
            # l'activation, on les remplace.
            if current_dt != next_run:
                con.execute(
                    """UPDATE anime_quiz_settings
                       SET next_run=?
                       WHERE chat_id=? AND enabled=1""",
                    (next_run.isoformat(), chat_id),
                )
                changed += 1

        except Exception:
            log.exception("Unable to repair anime quiz schedule for %s", chat_id)

    if changed:
        con.commit()
    con.close()
    return changed


def anime_quiz_enable(chat_id, language, interval_minutes=60, duration_minutes=10):
    interval_minutes = int(interval_minutes) if int(interval_minutes) in ANIME_QUIZ_INTERVAL_OPTIONS else 60
    duration_minutes = int(duration_minutes) if int(duration_minutes) in ANIME_QUIZ_DURATION_OPTIONS else 10
    now_utc = datetime.now(timezone.utc)
    next_utc = _anime_quiz_next_run(interval_minutes)
    con = db()
    con.execute(
        """INSERT INTO anime_quiz_settings(chat_id,enabled,language_code,enabled_at,next_run,interval_minutes,duration_minutes)
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(chat_id) DO UPDATE SET enabled=1,language_code=excluded.language_code,
        enabled_at=excluded.enabled_at,next_run=excluded.next_run,
        interval_minutes=excluded.interval_minutes,duration_minutes=excluded.duration_minutes""",
        (chat_id, 1, language, now_utc.isoformat(), next_utc.isoformat(), interval_minutes, duration_minutes),
    )
    con.commit()
    con.close()
    return next_utc


def anime_quiz_set_options(chat_id, language, interval_minutes, duration_minutes, enabled=True):
    interval_minutes = int(interval_minutes) if int(interval_minutes) in ANIME_QUIZ_INTERVAL_OPTIONS else 60
    duration_minutes = int(duration_minutes) if int(duration_minutes) in ANIME_QUIZ_DURATION_OPTIONS else 10
    now_utc = datetime.now(timezone.utc)
    next_utc = _anime_quiz_next_run(interval_minutes) if enabled else None
    con = db()
    con.execute(
        """INSERT INTO anime_quiz_settings(chat_id,enabled,language_code,enabled_at,next_run,interval_minutes,duration_minutes)
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(chat_id) DO UPDATE SET enabled=excluded.enabled,language_code=excluded.language_code,
        enabled_at=excluded.enabled_at,next_run=excluded.next_run,
        interval_minutes=excluded.interval_minutes,duration_minutes=excluded.duration_minutes""",
        (chat_id, 1 if enabled else 0, language, now_utc.isoformat() if enabled else None,
         next_utc.isoformat() if next_utc else None, interval_minutes, duration_minutes),
    )
    con.commit()
    con.close()
    return next_utc


def anime_quiz_disable(chat_id):
    con = db()
    con.execute(
        "UPDATE anime_quiz_settings SET enabled=0,next_run=NULL WHERE chat_id=?",
        (chat_id,),
    )
    con.execute(
        "UPDATE anime_quiz_active SET status='cancelled' WHERE chat_id=? AND status='open'",
        (chat_id,),
    )
    con.commit()
    con.close()


def anime_quiz_status(chat_id):
    con = db()
    row = con.execute(
        "SELECT enabled,language_code,next_run,COALESCE(interval_minutes,60),COALESCE(duration_minutes,10) FROM anime_quiz_settings WHERE chat_id=?",
        (chat_id,),
    ).fetchone()
    con.close()
    return row


def _anime_quiz_duration_seconds(chat_id):
    con = db()
    row = con.execute(
        "SELECT COALESCE(duration_minutes,10) FROM anime_quiz_settings WHERE chat_id=?",
        (chat_id,),
    ).fetchone()
    con.close()
    minutes = int(row[0] or 10) if row else 10
    if minutes not in ANIME_QUIZ_DURATION_OPTIONS:
        minutes = 10
    return minutes * 60


def _anime_quiz_interval_label(minutes):
    minutes = int(minutes)
    if minutes < 60:
        return f"{minutes} minutes"
    hours = minutes // 60
    return "1 heure" if hours == 1 else f"{hours} heures"


def _anime_quiz_duration_label(minutes):
    return _anime_quiz_interval_label(minutes)


async def anime_quiz_language_prompt(update, context):
    keyboard = [
        [InlineKeyboardButton("🌐 Automatique", callback_data="animequiz:lang:auto")],
        [InlineKeyboardButton("🇫🇷 Français", callback_data="animequiz:lang:fr"),
         InlineKeyboardButton("🇬🇧 English", callback_data="animequiz:lang:en")],
        [InlineKeyboardButton("🇪🇸 Español", callback_data="animequiz:lang:es"),
         InlineKeyboardButton("🇵🇹 Português", callback_data="animequiz:lang:pt")],
    ]
    await safe_reply(
        update.effective_message,
        "🎌 <b>ALICIA QUIZ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Configure le quiz automatique de ce groupe.\n\n"
        "<b>Types de questions</b>\n"
        "• Deviner un personnage\n"
        "• Retrouver son anime\n"
        "• Deviner un anime\n"
        "• Indices sur l’œuvre\n"
        "• Indices géographiques\n\n"
        "🌐 <b>Choisis la langue</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


def _anime_quiz_interval_keyboard():
    labels = {15:"15 min",30:"30 min",60:"1 h",120:"2 h",180:"3 h",360:"6 h",720:"12 h",1440:"24 h"}
    values = list(ANIME_QUIZ_INTERVAL_OPTIONS)
    rows = []
    for i in range(0, len(values), 2):
        rows.append([
            InlineKeyboardButton(labels[values[i]], callback_data=f"animequiz:interval:{values[i]}"),
            InlineKeyboardButton(labels[values[i+1]], callback_data=f"animequiz:interval:{values[i+1]}")
        ])
    return InlineKeyboardMarkup(rows)


def _anime_quiz_duration_keyboard():
    labels = {5:"5 min",10:"10 min",15:"15 min",30:"30 min",60:"1 h",120:"2 h"}
    values = list(ANIME_QUIZ_DURATION_OPTIONS)
    rows = []
    for i in range(0, len(values), 2):
        rows.append([
            InlineKeyboardButton(labels[values[i]], callback_data=f"animequiz:duration:{values[i]}"),
            InlineKeyboardButton(labels[values[i+1]], callback_data=f"animequiz:duration:{values[i+1]}")
        ])
    return InlineKeyboardMarkup(rows)


async def anime_quiz_interval_prompt(query):
    await query.edit_message_text(
        "🎌 <b>ALICIA QUIZ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🌐 Langue configurée.\n\n"
        "🔁 <b>Fréquence</b>\n"
        "Choisis quand Alicia doit lancer automatiquement le prochain quiz.",
        parse_mode="HTML",
        reply_markup=_anime_quiz_interval_keyboard(),
    )


async def anime_quiz_duration_prompt(query, interval_minutes):
    await query.edit_message_text(
        "🎌 <b>ALICIA QUIZ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🔁 Fréquence : <b>{_anime_quiz_interval_label(interval_minutes)}</b>\n\n"
        "⏱️ <b>Durée</b>\n"
        "Combien de temps les joueurs ont-ils pour répondre ?",
        parse_mode="HTML",
        reply_markup=_anime_quiz_duration_keyboard(),
    )


async def quiz_cmd(update, context):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await safe_reply(update.effective_message, "Les quiz anime se règlent dans un groupe.")
        return
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
        await anime_quiz_language_prompt(update, context)
        return
    if action in ("off", "disable", "desactiver", "désactiver"):
        anime_quiz_disable(chat.id)
        await safe_reply(update.effective_message, "🎌 Alicia Quiz anime est désactivé dans ce groupe.")
        return
    if action in ("test", "now", "maintenant"):
        if not anime_quiz_is_enabled(chat.id):
            await safe_reply(update.effective_message, "Active d'abord Alicia Quiz avec /quiz on.")
            return
        sent = await anime_quiz_broadcast_once(context.application)
        await safe_reply(update.effective_message, "🎌 Quiz anime envoyé." if sent else "Je n'ai pas réussi à envoyer le quiz.")
        return
    if action in ("rank", "ranking", "classement", "top"):
        await send_anime_quiz_leaderboard(context.bot, chat.id, "today")
        return

    row = anime_quiz_status(chat.id)
    if row and row[0]:
        enabled, lang, next_run, interval_minutes, duration_minutes = row
        next_text = "bientôt"
        if next_run:
            try:
                next_dt = datetime.fromisoformat(next_run)
                if next_dt.tzinfo is None:
                    next_dt = next_dt.replace(tzinfo=timezone.utc)
                next_text = next_dt.astimezone(timezone(timedelta(hours=1))).strftime("%H:%M")
            except Exception:
                pass
        await safe_reply(
            update.effective_message,
            "🎌 <b>ALICIA QUIZ</b>  •  <b>ACTIVÉ</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🌐 Langue : <b>{ANIME_QUIZ_LANGUAGES.get(lang, lang)}</b>\n"
            f"🔁 Fréquence : <b>{_anime_quiz_interval_label(interval_minutes)}</b>\n"
            f"⏱️ Durée : <b>{_anime_quiz_duration_label(duration_minutes)}</b>\n"
            f"🕒 Prochain quiz : <b>{next_text}</b>\n\n"
            "🎯 Personnage • Anime • Indices • Géographie\n"
            "🏆 Classement séparé des statistiques générales\n\n"
            "Utilise <b>/quiz on</b> pour modifier les réglages.",
            parse_mode="HTML",
        )
    else:
        await safe_reply(update.effective_message, "🎌 Alicia Quiz : DÉSACTIVÉ\nUtilise /quiz on pour choisir la langue, la fréquence et la durée.")


async def anime_quiz_callback(update, context):
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    chat = query.message.chat if query.message else None
    if not chat:
        return
    try:
        await query.answer()
    except Exception:
        pass

    if data == "animequiz:rules":
        rules = (
            "🎌 <b>RÈGLES DU QUIZ</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "• Une question est active à la fois dans le groupe.\n"
            "• Le premier joueur avec une bonne réponse gagne les points.\n"
            "• Les réponses partielles et petites fautes sont tolérées "
            "lorsqu'elles correspondent clairement au nom attendu.\n"
            "• Les questions viennent des données anime en ligne.\n"
            "• Les questions déjà utilisées dans ce groupe sont évitées.\n"
            "• Les scores du Quiz Anime restent séparés des anciennes statistiques."
        )
        await query.edit_message_text(
            rules,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏆 Classement", callback_data="animequiz:rank:today")],
                [InlineKeyboardButton("↩️ Retour au quiz", callback_data="animequiz:rank:today")],
            ]),
        )
        return

    if data.startswith("animequiz:lang:"):
        lang = data.split(":", 2)[2]
        if lang != "auto" and lang not in ANIME_QUIZ_LANGUAGES:
            return
        # Vérifie que le bouton est utilisé par un administrateur.
        try:
            member = await context.bot.get_chat_member(chat.id, query.from_user.id)
            if member.status not in ("administrator", "creator"):
                try:
                    await query.edit_message_text("Seul un administrateur peut choisir la langue.")
                except Exception:
                    pass
                return
        except Exception:
            try:
                await query.edit_message_text("Impossible de vérifier les droits.")
            except Exception:
                pass
            return
        # On mémorise immédiatement la langue choisie, mais on attend la
        # fréquence et la durée avant d'activer réellement le quiz.
        con = db()
        con.execute(
            """INSERT INTO anime_quiz_settings(chat_id,enabled,language_code,enabled_at,next_run,interval_minutes,duration_minutes)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET language_code=excluded.language_code""",
            (chat.id, 0, lang, None, None, 60, 10),
        )
        con.commit(); con.close()
        context.chat_data["anime_quiz_pending_lang"] = lang
        context.chat_data.pop("anime_quiz_pending_interval", None)
        await anime_quiz_interval_prompt(query)

    elif data.startswith("animequiz:interval:"):
        try:
            interval = int(data.split(":", 2)[2])
        except Exception:
            return
        if interval not in ANIME_QUIZ_INTERVAL_OPTIONS:
            return
        try:
            member = await context.bot.get_chat_member(chat.id, query.from_user.id)
            if member.status not in ("administrator", "creator"):
                await query.edit_message_text("Seul un administrateur peut modifier les réglages du quiz.")
                return
        except Exception:
            return
        context.chat_data["anime_quiz_pending_interval"] = interval
        await anime_quiz_duration_prompt(query, interval)

    elif data.startswith("animequiz:duration:"):
        try:
            duration = int(data.split(":", 2)[2])
        except Exception:
            return
        if duration not in ANIME_QUIZ_DURATION_OPTIONS:
            return
        try:
            member = await context.bot.get_chat_member(chat.id, query.from_user.id)
            if member.status not in ("administrator", "creator"):
                await query.edit_message_text("Seul un administrateur peut modifier les réglages du quiz.")
                return
        except Exception:
            return
        lang = context.chat_data.get("anime_quiz_pending_lang", "auto")
        interval = int(context.chat_data.get("anime_quiz_pending_interval", 60))
        next_run = anime_quiz_enable(chat.id, lang, interval, duration)
        context.chat_data.pop("anime_quiz_pending_lang", None)
        context.chat_data.pop("anime_quiz_pending_interval", None)
        await query.edit_message_text(
            f"🎌 <b>Alicia Quiz activé !</b>\n\n"
            f"🌐 Langue : {'Automatique (langue du groupe)' if lang == 'auto' else ANIME_QUIZ_LANGUAGES[lang]}\n"
            f"🔁 Nouveau quiz : toutes les <b>{_anime_quiz_interval_label(interval)}</b>\n"
            f"⏱️ Durée d'une question : <b>{_anime_quiz_duration_label(duration)}</b>\n"
            f"🕒 Premier quiz : <b>{next_run.astimezone(timezone(timedelta(hours=1))).strftime('%H:%M')}</b>\n\n"
            "La première bonne réponse gagne les points.",
            parse_mode="HTML",
        )

    elif data.startswith("animequiz:rank:"):
        period = data.split(":", 2)[2]
        if period not in ("today", "week", "all"):
            return
        await send_anime_quiz_leaderboard(context.bot, chat.id, period, edit_query=query)


def _anime_quiz_rank_rows(chat_id, period):
    start = _anime_quiz_period_start(period)
    start_iso = start.isoformat() if start else None
    con = db()
    if start_iso:
        rows = con.execute(
            """SELECT user_id,MAX(player_name),SUM(points),COUNT(*),MAX(answered_at)
               FROM anime_quiz_answers WHERE chat_id=? AND answered_at>=?
               GROUP BY user_id
               ORDER BY SUM(points) DESC,COUNT(*) DESC,MAX(answered_at) ASC LIMIT 10""",
            (chat_id, start_iso),
        ).fetchall()
        totals = con.execute(
            "SELECT COUNT(*),COUNT(DISTINCT user_id),COALESCE(SUM(points),0) FROM anime_quiz_answers WHERE chat_id=? AND answered_at>=?",
            (chat_id, start_iso),
        ).fetchone()
    else:
        rows = con.execute(
            """SELECT user_id,MAX(player_name),SUM(points),COUNT(*),MAX(answered_at)
               FROM anime_quiz_answers WHERE chat_id=?
               GROUP BY user_id
               ORDER BY SUM(points) DESC,COUNT(*) DESC,MAX(answered_at) ASC LIMIT 10""",
            (chat_id,),
        ).fetchall()
        totals = con.execute(
            "SELECT COUNT(*),COUNT(DISTINCT user_id),COALESCE(SUM(points),0) FROM anime_quiz_answers WHERE chat_id=?",
            (chat_id,),
        ).fetchone()
    con.close()
    return rows, int(totals[1] or 0), int(totals[2] or 0), int(totals[0] or 0)


def _anime_quiz_badge(position):
    if position == 1:
        return "LÉGENDE", "gold"
    if position == 2:
        return "MAÎTRE", "silver"
    if position == 3:
        return "ÉLITE", "bronze"
    if position == 4:
        return "EXPERT", "purple"
    if position in (5, 6):
        return "PRO", "blue"
    if position in (7, 8):
        return "FAN", "green"
    return "JOUEUR", "dark"


def _anime_quiz_font(size, bold=False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


async def _anime_quiz_telegram_avatar(bot, user_id):
    now_ts = time.monotonic()
    cached = _ANIME_AVATAR_CACHE.get(int(user_id))
    if cached and now_ts - cached[0] < _ANIME_AVATAR_CACHE_TTL:
        return io.BytesIO(cached[1]) if cached[1] else None

    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count:
            file_obj = await photos.photos[0][-1].get_file()
            bio = io.BytesIO()
            await file_obj.download_to_memory(out=bio)
            data = bio.getvalue()
            _ANIME_AVATAR_CACHE[int(user_id)] = (now_ts, data)
            return io.BytesIO(data)
    except Exception:
        pass

    # Mémorise aussi l'absence de photo pour éviter de refaire la requête
    # Telegram à chaque classement.
    _ANIME_AVATAR_CACHE[int(user_id)] = (now_ts, b'')
    return None


async def _build_anime_quiz_leaderboard_image(bot, chat_id, period):
    rows, participants, total_points, questions_won = _anime_quiz_rank_rows(chat_id, period)
    if Image is None:
        return None, rows, participants, total_points

    W, H = 1200, 1370
    img = Image.new("RGB", (W, H), (10, 8, 24))
    draw = ImageDraw.Draw(img)

    # Fond inspiré du modèle fourni : sombre, violet/rose, étoiles et halos.
    for r, alpha in [(560, 25), (430, 35), (300, 50)]:
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        od.ellipse((W//2-r, 50-r, W//2+r, 50+r), fill=(255, 45, 175, alpha))
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(img)

    title_font = _anime_quiz_font(64, True)
    sub_font = _anime_quiz_font(28, False)
    head_font = _anime_quiz_font(34, True)
    name_font = _anime_quiz_font(27, True)
    small_font = _anime_quiz_font(20, False)
    points_font = _anime_quiz_font(28, True)
    badge_font = _anime_quiz_font(20, True)

    draw.text((55, 35), "Alicia Quiz", font=title_font, fill=(255, 95, 210))
    draw.text((60, 112), "Teste tes connaissances sur les animes !", font=sub_font, fill=(235, 230, 245))
    draw.text((820, 50), "CLASSEMENT", font=head_font, fill=(255, 255, 255))
    draw.text((820, 93), "DES JOUEURS", font=head_font, fill=(255, 90, 205))

    periods = [("today", "Aujourd’hui"), ("week", "Cette semaine"), ("all", "Tout le temps")]
    bx = 45
    for key, label in periods:
        active = key == period
        fill = (225, 30, 150) if active else (25, 29, 48)
        outline = (255, 90, 210) if active else (80, 85, 110)
        draw.rounded_rectangle((bx, 170, bx + 345, 235), 20, fill=fill, outline=outline, width=3)
        draw.text((bx + 24, 188), label, font=small_font, fill=(255,255,255))
        bx += 370

    # Précharge les avatars en parallèle. Avant cette optimisation, 10 appels
    # Telegram étaient faits l'un après l'autre et pouvaient ralentir le classement.
    avatar_map = {}
    avatar_tasks = {}
    for row in rows[:10]:
        try:
            uid = int(row[0])
            avatar_tasks[uid] = asyncio.create_task(_anime_quiz_telegram_avatar(bot, uid))
        except Exception:
            pass
    if avatar_tasks:
        results = await asyncio.gather(*avatar_tasks.values(), return_exceptions=True)
        for uid, result in zip(avatar_tasks.keys(), results):
            if not isinstance(result, Exception):
                avatar_map[uid] = result

    # En-têtes.
    y = 265
    draw.text((55, y), "#", font=head_font, fill=(255, 115, 215))
    draw.text((150, y), "Joueur", font=head_font, fill=(255, 115, 215))
    draw.text((585, y), "Badge", font=head_font, fill=(255, 115, 215))
    draw.text((1010, y), "Points", font=head_font, fill=(255, 115, 215))
    y += 55

    for position in range(1, 11):
        row = rows[position - 1] if position <= len(rows) else None
        top = y + (position - 1) * 91
        if position <= 3:
            row_fill = (48, 34, 25)
        else:
            row_fill = (20, 23, 38)
        draw.rounded_rectangle((38, top, W - 38, top + 78), 18, fill=row_fill, outline=(70, 65, 90), width=2)

        if row:
            uid, name, pts, wins, _ = row
            pts = int(pts or 0)
            name = str(name or uid)[:22]
            badge, badge_color = _anime_quiz_badge(position)
            palette = {
                "gold": ((255, 205, 55), (75, 45, 12)),
                "silver": ((210, 220, 235), (45, 50, 65)),
                "bronze": ((255, 165, 75), (75, 45, 22)),
                "purple": ((190, 90, 255), (52, 25, 75)),
                "blue": ((80, 170, 255), (20, 48, 80)),
                "green": ((70, 225, 130), (20, 70, 48)),
                "dark": ((175, 185, 205), (35, 40, 52)),
            }
            accent, badge_bg = palette[badge_color]
            draw.text((62, top + 22), str(position), font=head_font, fill=accent if position <= 3 else (240,240,248))
            avatar = avatar_map.get(int(uid))
            if avatar:
                try:
                    av = Image.open(avatar).convert("RGB").resize((56,56))
                    mask = Image.new("L", (56,56), 0)
                    ImageDraw.Draw(mask).ellipse((0,0,56,56), fill=255)
                    img.paste(av, (145, top + 11), mask)
                except Exception:
                    pass
            draw.text((220, top + 14), name, font=name_font, fill=(255,255,255))
            draw.text((220, top + 46), f"{int(wins)} victoire(s)", font=small_font, fill=(165,170,190))
            draw.rounded_rectangle((570, top + 15, 810, top + 61), 15, fill=badge_bg, outline=accent, width=2)
            draw.text((600, top + 25), badge, font=badge_font, fill=accent)
            draw.text((1010, top + 22), f"{pts} pts", font=points_font, fill=accent if position <= 3 else (245,245,250))
        else:
            draw.text((62, top + 22), str(position), font=head_font, fill=(100,105,125))
            draw.text((220, top + 22), "—", font=name_font, fill=(100,105,125))

    footer_y = 1190
    draw.rounded_rectangle((38, footer_y, W - 38, 1325), 22, fill=(22, 24, 42), outline=(160, 35, 130), width=2)
    # Détails du classement sélectionné.
    draw.text((70, footer_y + 24), "Questions gagnées", font=sub_font, fill=(255, 85, 205))
    draw.text((70, footer_y + 62), f"{questions_won}", font=head_font, fill=(245,245,250))
    draw.text((390, footer_y + 24), "Participants", font=sub_font, fill=(255, 85, 205))
    draw.text((390, footer_y + 62), f"{participants} joueurs", font=head_font, fill=(245,245,250))
    draw.text((700, footer_y + 24), "Total des points", font=sub_font, fill=(255, 85, 205))
    draw.text((700, footer_y + 62), f"{total_points} pts", font=head_font, fill=(245,245,250))
    draw.text((980, footer_y + 24), "Période", font=sub_font, fill=(255, 85, 205))
    draw.text((980, footer_y + 62), _anime_quiz_period_label(period), font=_anime_quiz_font(23, True), fill=(245,245,250))

    out = io.BytesIO()
    img.save(out, "PNG")
    out.seek(0)
    return out, rows, participants, total_points


def _anime_quiz_rank_keyboard(active="today"):
    labels = {"today": "📅 Aujourd’hui", "week": "📅 Cette semaine", "all": "👑 Tout le temps"}
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(("✅ " if active == "today" else "") + labels["today"], callback_data="animequiz:rank:today"),
         InlineKeyboardButton(("✅ " if active == "week" else "") + labels["week"], callback_data="animequiz:rank:week")],
        [InlineKeyboardButton(("✅ " if active == "all" else "") + labels["all"], callback_data="animequiz:rank:all")],
    ])


async def send_anime_quiz_leaderboard(bot, chat_id, period="today", edit_query=None):
    photo, rows, participants, total_points = await _build_anime_quiz_leaderboard_image(bot, chat_id, period)
    caption = f"🏆 <b>Alicia Quiz — { _anime_quiz_period_label(period) }</b>\nTop 10 des joueurs"
    keyboard = _anime_quiz_rank_keyboard(period)
    if photo is None:
        text = caption.replace("<b>", "").replace("</b>", "") + "\n\n"
        text += "\n".join(f"{i}. {r[1]} — {int(r[2])} pts" for i, r in enumerate(rows, 1)) or "Aucun score pour cette période."
        if edit_query:
            try:
                await edit_query.edit_message_text(text, reply_markup=keyboard)
            except Exception:
                pass
        else:
            await safe_send_message(bot, chat_id, text, reply_markup=keyboard)
        return

    if edit_query and edit_query.message and edit_query.message.photo:
        try:
            await edit_query.edit_message_media(
                media=InputMediaPhoto(media=photo, caption=caption, parse_mode="HTML"),
                reply_markup=keyboard,
            )
            return
        except Exception:
            pass

    if edit_query:
        try:
            await edit_query.edit_message_media(
                media=InputMediaPhoto(media=photo, caption=caption, parse_mode="HTML"),
                reply_markup=keyboard,
            )
            return
        except Exception:
            pass

    await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption, parse_mode="HTML", reply_markup=keyboard)


async def close_anime_quiz(chat_id, bot, announce=True):
    con = db()
    row = con.execute(
        "SELECT question_id,status,ends_at FROM anime_quiz_active WHERE chat_id=?",
        (chat_id,),
    ).fetchone()
    if not row:
        con.close()
        return
    question_id, status, ends_at = row
    if status != "open":
        con.close()
        return
    con.execute(
        "UPDATE anime_quiz_active SET status='expired' WHERE chat_id=? AND status='open'",
        (chat_id,),
    )
    con.commit()
    con.close()
    if announce:
        try:
            await safe_send_message(bot, chat_id, "⏰ <b>Temps écoulé !</b>\nPersonne n'a trouvé la bonne réponse. Le quiz est fermé.", parse_mode="HTML")
        except Exception:
            pass


async def _anime_quiz_expiry_task(bot, chat_id, question_id, duration_seconds):
    await asyncio.sleep(max(1, int(duration_seconds)))
    con = db()
    row = con.execute(
        "SELECT status FROM anime_quiz_active WHERE chat_id=? AND question_id=?",
        (chat_id, question_id),
    ).fetchone()
    con.close()
    if row and row[0] == "open":
        await close_anime_quiz(chat_id, bot, announce=True)


async def send_anime_quiz_question(bot, chat_id):
    if not anime_quiz_is_enabled(chat_id):
        return False

    con = db()
    active = con.execute(
        "SELECT question_id,status,ends_at FROM anime_quiz_active WHERE chat_id=?",
        (chat_id,),
    ).fetchone()
    con.close()
    if active and active[1] == "open":
        try:
            ends = datetime.fromisoformat(active[2])
            if ends > datetime.now(timezone.utc):
                return False
        except Exception:
            pass
        await close_anime_quiz(chat_id, bot, announce=False)

    data = await fetch_anime_quiz_question(chat_id)
    if not data:
        return False

    started = datetime.now(timezone.utc)
    duration_seconds = _anime_quiz_duration_seconds(chat_id)
    ends = started + timedelta(seconds=duration_seconds)
    accepted = []
    seen = set()
    for answer in data.get("accepted", []):
        norm = _anime_quiz_normalize(answer)
        if norm and norm not in seen:
            accepted.append(norm)
            seen.add(norm)
    if not accepted:
        accepted = [_anime_quiz_normalize(data["answer"])]

    lang = _anime_quiz_lang(chat_id)
    caption = (
        "🎌 <b>ALICIA QUIZ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"❓ <b>{data['question']}</b>\n\n"
        f"✍️ {_quiz_text(lang, 'reply')}\n"
        f"⏱️ {_quiz_text(lang, 'time')}\n"
        "🏆 <b>10 points maximum</b>\n"
        f"⚡ {_quiz_text(lang, 'first')}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Réponse libre • Pas besoin de mentionner Alicia"
    )
    quiz_question_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🏆 Classement", callback_data="animequiz:rank:today"),
            InlineKeyboardButton("ℹ️ Règles", callback_data="animequiz:rules"),
        ]
    ])

    # Réserve la question AVANT l'envoi Telegram. Cela évite qu'une réponse
    # très rapide arrive avant que la question soit enregistrée dans la DB.
    con = db()
    try:
        con.execute(
            """INSERT INTO anime_quiz_active(chat_id,question_id,question_type,question_text,correct_answer,
            accepted_answers,anime_title,character_name,image_url,started_at,ends_at,status,message_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET question_id=excluded.question_id,question_type=excluded.question_type,
            question_text=excluded.question_text,correct_answer=excluded.correct_answer,accepted_answers=excluded.accepted_answers,
            anime_title=excluded.anime_title,character_name=excluded.character_name,image_url=excluded.image_url,
            started_at=excluded.started_at,ends_at=excluded.ends_at,status='open',message_id=excluded.message_id,
            winner_user_id=NULL,winner_name='',winner_points=0""",
            (chat_id, data["question_id"], data["kind"], data["question"], data["answer"],
             json.dumps(accepted, ensure_ascii=False), data.get("anime_title", ""),
             data.get("name", "") if data.get("kind") == "character" else "",
             data.get("image_url", ""), started.isoformat(), ends.isoformat(), "open", None),
        )
        con.execute(
            "INSERT OR IGNORE INTO anime_quiz_used(chat_id,question_id,used_at) VALUES(?,?,?)",
            (chat_id, data["question_id"], now()),
        )
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        con.close()
        raise
    finally:
        try:
            con.close()
        except Exception:
            pass

    try:
        # Telegram accepte directement l'URL de l'image : pas besoin de
        # télécharger plusieurs images sur le serveur Render.
        if data.get("image_url"):
            sent = await bot.send_photo(
                chat_id=chat_id,
                photo=data["image_url"],
                caption=caption,
                parse_mode="HTML",
                reply_markup=quiz_question_keyboard,
            )
        else:
            sent = await safe_send_message(
                bot, chat_id, caption,
                parse_mode="HTML",
                reply_markup=quiz_question_keyboard,
            )
    except Exception as exc:
        log.warning("Anime quiz send failed for group %s: %s", chat_id, exc)
        # Si l'envoi échoue, on ferme uniquement cette question réservée.
        try:
            con = db()
            con.execute(
                "UPDATE anime_quiz_active SET status='cancelled' WHERE chat_id=? AND question_id=? AND status='open'",
                (chat_id, data["question_id"]),
            )
            con.commit()
            con.close()
        except Exception:
            pass
        return False

    # Enregistre l'ID du message après l'envoi.
    try:
        con = db()
        con.execute(
            "UPDATE anime_quiz_active SET message_id=? WHERE chat_id=? AND question_id=?",
            (getattr(sent, "message_id", None), chat_id, data["question_id"]),
        )
        con.commit()
        con.close()
    except Exception as exc:
        log.warning("Unable to save anime quiz message_id for group %s: %s", chat_id, exc)

    asyncio.create_task(_anime_quiz_expiry_task(bot, chat_id, data["question_id"], duration_seconds))
    return True


async def anime_quiz_broadcast_once(app):
    """Envoie le quiz à tous les groupes activés sans bloquer les autres groupes."""
    con = db()
    chats = [r[0] for r in con.execute(
        "SELECT chat_id FROM anime_quiz_settings WHERE enabled=1 ORDER BY chat_id"
    ).fetchall()]
    con.close()

    if not chats:
        return 0

    # Quelques requêtes simultanées suffisent pour éviter qu'un groupe lent
    # bloque tous les autres, tout en restant raisonnable pour Jikan/Telegram.
    semaphore = asyncio.Semaphore(8)

    async def one(chat_id):
        async with semaphore:
            try:
                return 1 if await send_anime_quiz_question(app.bot, chat_id) else 0
            except Exception:
                log.exception("Anime quiz failed for group %s", chat_id)
                return 0

    results = await asyncio.gather(*(one(chat_id) for chat_id in chats), return_exceptions=True)
    return sum(int(x) for x in results if isinstance(x, int))


async def anime_quiz_message_handler(update, context):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or not is_group(chat) or not msg.text:
        return
    text = msg.text.strip()
    if not text or text.startswith("/"):
        return

    con = db()
    row = con.execute(
        """SELECT question_id,accepted_answers,started_at,ends_at,status,winner_user_id
           FROM anime_quiz_active WHERE chat_id=?""",
        (chat.id,),
    ).fetchone()
    con.close()
    if not row or row[4] != "open":
        return

    question_id, accepted_json, started_at, ends_at, status, winner_id = row
    try:
        end_dt = datetime.fromisoformat(ends_at)
    except Exception:
        end_dt = datetime.now(timezone.utc) - timedelta(seconds=1)
    now_dt = datetime.now(timezone.utc)
    if now_dt >= end_dt:
        await close_anime_quiz(chat.id, context.bot, announce=True)
        return

    try:
        accepted = set(json.loads(accepted_json or "[]"))
    except Exception:
        accepted = set()
    normalized = _anime_quiz_normalize(text)
    if not normalized or not _anime_quiz_answer_matches(text, accepted):
        return

    try:
        started_dt = datetime.fromisoformat(started_at)
    except Exception:
        started_dt = now_dt
    elapsed = max(0.0, (now_dt - started_dt).total_seconds())
    duration_seconds = _anime_quiz_duration_seconds(chat.id)
    points = _anime_quiz_points(elapsed, duration_seconds)
    if points <= 0:
        await close_anime_quiz(chat.id, context.bot, announce=True)
        return

    name = display_name(user)
    con = db()
    try:
        # Le premier UPDATE qui trouve encore status='open' gagne la question.
        con.execute(
            """UPDATE anime_quiz_active SET status='won',winner_user_id=?,winner_name=?,winner_points=?
               WHERE chat_id=? AND question_id=? AND status='open' AND ends_at>?""",
            (user.id, name, points, chat.id, question_id, now_dt.isoformat()),
        )
        winner = con.execute(
            "SELECT winner_user_id,winner_name,winner_points,question_type FROM anime_quiz_active WHERE chat_id=? AND question_id=?",
            (chat.id, question_id),
        ).fetchone()
        if not winner or int(winner[0] or 0) != int(user.id):
            con.rollback()
            return

        con.execute(
            """INSERT INTO anime_quiz_answers(question_id,chat_id,user_id,player_name,points,answered_at,question_type,answer_text)
               VALUES(?,?,?,?,?,?,?,?)""",
            (question_id, chat.id, user.id, name, points, now_dt.isoformat(), winner[3], text[:300]),
        )
        con.execute(
            """INSERT INTO anime_quiz_scores(user_id,chat_id,points,correct,answered) VALUES(?,?,?,?,1)
               ON CONFLICT(user_id,chat_id) DO UPDATE SET points=anime_quiz_scores.points+excluded.points,
               correct=anime_quiz_scores.correct+1,answered=anime_quiz_scores.answered+1""",
            (user.id, chat.id, points, 1),
        )
        con.commit()
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        log.exception("Anime quiz answer processing failed in group %s: %s", chat.id, exc)
        return
    finally:
        try:
            con.close()
        except Exception:
            pass

    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)
    # Animation de victoire : plusieurs étapes courtes pour donner un vrai
    # moment de victoire sans bloquer le scheduler des autres groupes.
    try:
        await context.bot.send_message(
            chat_id=chat.id,
            text="⚡⚡⚡ RÉPONSE TROUVÉE ⚡⚡⚡",
            reply_to_message_id=msg.message_id,
        )
        await asyncio.sleep(0.35)
        await context.bot.send_message(
            chat_id=chat.id,
            text=f"🎉 {name.upper()} VIENT DE GAGNER ! 🎉",
            reply_to_message_id=msg.message_id,
        )
        await asyncio.sleep(0.35)
    except Exception:
        pass

    # Envoi direct : safe_send_message possède un verrou global utilisé par
    # beaucoup d'autres fonctions du bot. Pour le quiz, la réaction du gagnant
    # doit passer immédiatement sans attendre les autres messages.
    winner_variants = [
        f"Bien joué {name}.\n+{points} points.",
        f"Oui, c’était ça. {name} gagne +{points} points.",
        f"T’as trouvé, {name}. +{points} points.",
        f"Exact. {name} prend +{points} points.",
    ]
    winner_text = random.choice(winner_variants)
    try:
        await context.bot.send_message(
            chat_id=chat.id,
            text=winner_text,
            parse_mode="HTML",
            reply_to_message_id=msg.message_id,
        )
    except RetryAfter as exc:
        await asyncio.sleep(float(getattr(exc, "retry_after", 1)))
        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=winner_text,
                parse_mode="HTML",
                reply_to_message_id=msg.message_id,
            )
        except Exception:
            pass
    except Exception as exc:
        log.warning("Fast quiz winner message failed in group %s: %s", chat.id, exc)

    # Réaction Telegram optionnelle en arrière-plan : elle ne ralentit jamais
    # le message de victoire.
    try:
        context.application.create_task(
            react_to_message(context.bot, chat.id, msg.message_id, random.choice(["👍", "❤️", "😂"])),
            update=update,
        )
    except Exception:
        pass
    # Le message de victoire doit partir immédiatement. Le classement, plus
    # lourd (avatars + génération d'image), est préparé en arrière-plan.
    try:
        context.application.create_task(
            send_anime_quiz_leaderboard(context.bot, chat.id, "today"),
            update=update,
        )
    except Exception:
        pass


async def send_group_quiz_leaderboard(bot, chat_id):
    # Compatibilité avec l'ancien nom de fonction : le nouveau classement est anime-only.
    await send_anime_quiz_leaderboard(bot, chat_id, "today")


async def all_time_quiz_ranking_text(limit=20):
    con = db()
    rows = con.execute(
        """SELECT user_id,COALESCE(player_name,user_id),SUM(points),COUNT(*),COUNT(*)
           FROM anime_quiz_answers GROUP BY user_id,player_name
           ORDER BY SUM(points) DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    con.close()
    return rows


async def quiz_scheduler(app):
    """Scheduler par groupe : chaque groupe garde sa propre fréquence et sa propre durée."""
    while True:
        try:
            now_dt = datetime.now(timezone.utc)

            # Ferme les questions expirées après un redémarrage.
            con = db()
            expired = con.execute(
                "SELECT chat_id FROM anime_quiz_active WHERE status='open' AND ends_at<=?",
                (now_dt.isoformat(),),
            ).fetchall()
            due = con.execute(
                """SELECT chat_id,COALESCE(interval_minutes,60),COALESCE(duration_minutes,10)
                   FROM anime_quiz_settings
                   WHERE enabled=1 AND next_run IS NOT NULL AND next_run<=?
                   ORDER BY next_run ASC""",
                (now_dt.isoformat(),),
            ).fetchall()
            con.close()

            for (chat_id,) in expired:
                try:
                    await close_anime_quiz(chat_id, app.bot, announce=False)
                except Exception:
                    log.exception("Failed to close expired quiz in %s", chat_id)

            # Envoie chaque quiz dû indépendamment.
            for chat_id, interval_minutes, duration_minutes in due:
                try:
                    await send_anime_quiz_question(app.bot, chat_id)
                except Exception:
                    log.exception("Anime quiz failed for group %s", chat_id)
                finally:
                    # On avance toujours le prochain passage, même si Jikan ou
                    # Telegram rencontre une erreur : un groupe ne bloque pas les autres.
                    try:
                        con = db()
                        row = con.execute(
                            "SELECT next_run FROM anime_quiz_settings WHERE chat_id=? AND enabled=1",
                            (chat_id,),
                        ).fetchone()

                        interval_td = timedelta(minutes=max(1, int(interval_minutes or 60)))
                        base_run = now_dt

                        if row and row[0]:
                            try:
                                base_run = datetime.fromisoformat(str(row[0]))
                                if base_run.tzinfo is None:
                                    base_run = base_run.replace(tzinfo=timezone.utc)
                            except Exception:
                                base_run = now_dt

                        # Le prochain passage est basé sur l'heure prévue,
                        # jamais sur l'heure de réveil du scheduler.
                        # La première échéance est toujours une heure ronde
                        # (ex. 22:00), puis la fréquence s'applique à partir
                        # de cette échéance.
                        next_run = base_run + interval_td

                        # Si Render a été arrêté longtemps, ne pas envoyer
                        # plusieurs quiz d'un coup au redémarrage.
                        current_utc = datetime.now(timezone.utc)
                        while next_run <= current_utc:
                            next_run += interval_td

                        con.execute(
                            "UPDATE anime_quiz_settings SET next_run=? WHERE chat_id=? AND enabled=1",
                            (next_run.isoformat(), chat_id),
                        )
                        con.commit()
                        con.close()
                    except Exception:
                        log.exception("Unable to schedule next quiz for group %s", chat_id)

            # Réveil rapide pour ne pas attendre jusqu'à l'heure suivante.
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Anime quiz scheduler error")
            await asyncio.sleep(10)

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
    try:
        await process_referral(update, context)
    except Exception as e:
        log.warning("start referral skipped: %s", e)
    register_user(u)
    text = (
        f"Salut {display_name(u)}.\n\n"
        "Moi c'est Alicia. On peut discuter, jouer ou faire un quiz."
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
    """Interface Jeux moderne.
    Les callback_data historiques sont volontairement conservées.
    """
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("♟ Échecs", callback_data="game:chess"),
            InlineKeyboardButton("🎲 Ludo", callback_data="game:ludo"),
        ],
        [
            InlineKeyboardButton("❌⭕ Morpion", callback_data="game:ttt"),
            InlineKeyboardButton("🔴 Puissance 4", callback_data="game:c4"),
        ],
        [
            InlineKeyboardButton("🎯 Devine", callback_data="game:guess"),
            InlineKeyboardButton("⚡ Réflexe", callback_data="game:reflex"),
        ],
        [
            InlineKeyboardButton("🧠 Mémoire", callback_data="game:memory"),
            InlineKeyboardButton("💣 Bombe", callback_data="game:bomb"),
        ],
        [
            InlineKeyboardButton("👑 Boss Battle", callback_data="game:boss"),
            InlineKeyboardButton("🏁 Course", callback_data="game:race"),
        ],
        [
            InlineKeyboardButton("🃏 Cartes", callback_data="game:cards"),
            InlineKeyboardButton("🎌 Quiz Anime", callback_data="game:quiz"),
        ],
        [
            InlineKeyboardButton("🏆 Classement", callback_data="game:ranking"),
            InlineKeyboardButton("✕ Fermer", callback_data="game:close"),
        ],
    ])

def game_back_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎮 Jeux", callback_data="game:menu"),
            InlineKeyboardButton("🏆 Classement", callback_data="game:ranking"),
        ]
    ])

def game_replay_menu(game):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Rejouer", callback_data=f"game:{game}"),
            InlineKeyboardButton("🎮 Jeux", callback_data="game:menu"),
        ]
    ])

async def games(update, context):
    await safe_reply(
        update.effective_message,
        "🎮 JEUX\nChoisis une partie.",
        reply_markup=game_menu()
    )


async def games(update, context):
    await safe_reply(
        update.effective_message,
        "🎮 <b>ALICIA GAMES</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Des parties rapides, des duels et des défis anime.\n\n"
        "<b>JEUX</b>\n"
        "♟ Échecs  •  🎲 Ludo  •  ❌⭕ Morpion\n"
        "🔴 Puissance 4  •  ⚡ Réflexe  •  🧠 Mémoire\n"
        "💣 Bombe  •  👑 Boss Battle  •  🏁 Course\n"
        "🃏 Cartes  •  🎯 Devine\n\n"
        "<b>🎌 QUIZ ANIME</b>\n"
        "Questions générées depuis les données anime en ligne : "
        "personnages, anime, indices, œuvres et indices géographiques.\n\n"
        "<b>🏆 Progression</b>\n"
        "Les scores, XP, victoires et historiques existants restent inchangés.",
        parse_mode="HTML",
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
        await q.message.edit_text(
            f"⚡ {elapsed:.2f}s · +{pts} pts",
            reply_markup=game_replay_menu("reflex")
        )
    elif kind=="memory":
        answer=parts[3] if len(parts)>3 else ""
        if answer==state.get("answer"):
            MINI_GAME_STATE.pop(uid,None); add_score(q.message.chat_id,uid,display_name(q.from_user),15,win=True); await add_xp(context.bot,uid,20,display_name(q.from_user)); await q.message.edit_text(
                "🧠 Mémoire réussie · +15 pts",
                reply_markup=game_replay_menu("memory")
            )
        else: await q.answer("Raté.",show_alert=True)
    elif kind=="bomb":
        choice=int(parts[3]) if len(parts)>3 else 0
        bomb=state.get("bomb")
        if not bomb: return
        if choice==bomb:
            MINI_GAME_STATE.pop(uid,None); add_score(q.message.chat_id,uid,display_name(q.from_user),0,loss=True); await q.message.edit_text(
                "💣 BOOM.",
                reply_markup=game_replay_menu("bomb")
            )
        else:
            MINI_GAME_STATE.pop(uid,None); add_score(q.message.chat_id,uid,display_name(q.from_user),20,win=True); await add_xp(context.bot,uid,25,display_name(q.from_user)); await q.message.edit_text(
                "💎 Bien joué · +20 pts",
                reply_markup=game_replay_menu("bomb")
            )
    elif kind=="boss":
        hit=int(parts[3]) if len(parts)>3 else 0; boss=state.get("boss",3)
        if hit==state.get("weak",-1):
            boss-=1; state["boss"]=boss; state["weak"]=random.randint(1,3)
            if boss<=0:
                MINI_GAME_STATE.pop(uid,None); add_score(q.message.chat_id,uid,display_name(q.from_user),50,win=True); await add_xp(context.bot,uid,60,display_name(q.from_user)); await q.message.edit_text(
                    "👑 Boss vaincu · +50 pts",
                    reply_markup=game_replay_menu("boss")
                )
            else:
                MINI_GAME_STATE[uid]=state; await q.message.edit_text(f"👑 Boss : {boss} PV\nTrouve sa faiblesse.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("1",callback_data=f"mini:boss:{uid}:1"),InlineKeyboardButton("2",callback_data=f"mini:boss:{uid}:2"),InlineKeyboardButton("3",callback_data=f"mini:boss:{uid}:3")]]))
        else: await q.answer("Le boss contre-attaque.",show_alert=True)
    elif kind=="race":
        choice=int(parts[3]) if len(parts)>3 else 0; you=state.get("you",0)+choice; ai=state.get("ai",0)+random.randint(1,3)
        if you>=15:
            MINI_GAME_STATE.pop(uid,None); add_score(q.message.chat_id,uid,display_name(q.from_user),30,win=True); await add_xp(context.bot,uid,45,display_name(q.from_user)); await q.message.edit_text(
                "🏁 Course gagnée · +30 pts",
                reply_markup=game_replay_menu("race")
            )
        elif ai>=15:
            MINI_GAME_STATE.pop(uid,None); add_score(q.message.chat_id,uid,display_name(q.from_user),0,loss=True); await q.message.edit_text(
                "🏁 Alicia gagne.",
                reply_markup=game_replay_menu("race")
            )
        else:
            state.update(you=you,ai=ai); MINI_GAME_STATE[uid]=state; await q.message.edit_text(f"🏃 Toi : {you}/15 — Alicia : {ai}/15",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚡ Avancer",callback_data=f"mini:race:{uid}:3"),InlineKeyboardButton("🚀 Sprint",callback_data=f"mini:race:{uid}:5")]]))
    elif kind=="cards":
        player=random.randint(1,10); ai=random.randint(1,10)
        MINI_GAME_STATE.pop(uid,None)
        if player>=ai: add_score(q.message.chat_id,uid,display_name(q.from_user),25,win=True); await add_xp(context.bot,uid,30,display_name(q.from_user)); result=f"🃏 Toi {player} — Alicia {ai}. Victoire ! +25 points."
        else: add_score(q.message.chat_id,uid,display_name(q.from_user),0,loss=True); result=f"🃏 Toi {player} — Alicia {ai}. Alicia gagne."
        await q.message.edit_text(result, reply_markup=game_replay_menu("cards"))

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


# ============================================================
# ALICIA GAMES — PLATEAUX VISUELS
# ============================================================
# Les états et statistiques existants sont conservés.
# Les plateaux sont générés en PNG et envoyés directement dans Telegram.

_BOARD_LIGHT = (239, 228, 207)
_BOARD_DARK = (82, 108, 96)
_BOARD_BG = (18, 20, 26)

def _game_font(size, bold=False):
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()

def _game_png(img):
    bio = io.BytesIO()
    img.save(bio, "PNG", optimize=True)
    bio.seek(0)
    return bio

def _game_center(draw, box, value, font, fill):
    x1, y1, x2, y2 = box
    bb = draw.textbbox((0, 0), str(value), font=font)
    w, h = bb[2] - bb[0], bb[3] - bb[1]
    draw.text(((x1+x2-w)/2, (y1+y2-h)/2-bb[1]), str(value),
              font=font, fill=fill)

def _render_chess_visual(board):
    size = 760
    left, top = 42, 28
    cell = 82
    img = Image.new("RGB", (size, size), _BOARD_BG)
    draw = ImageDraw.Draw(img)
    piece_font = _game_font(54, True)
    coord_font = _game_font(20, True)

    # Plateau 8x8, dans le style de l'image de référence.
    for r in range(8):
        for c in range(8):
            x1, y1 = left+c*cell, top+r*cell
            fill = _BOARD_LIGHT if (r+c) % 2 == 0 else _BOARD_DARK
            draw.rectangle((x1, y1, x1+cell, y1+cell), fill=fill)

    symbols = {
        "P":"♙","N":"♘","B":"♗","R":"♖","Q":"♕","K":"♔",
        "p":"♟","n":"♞","b":"♝","r":"♜","q":"♛","k":"♚"
    }
    try:
        for r in range(8):
            for c in range(8):
                sq = (7-r)*8+c
                piece = board.piece_at(sq)
                if piece:
                    _game_center(
                        draw,
                        (left+c*cell, top+r*cell, left+(c+1)*cell, top+(r+1)*cell),
                        symbols.get(piece.symbol(), piece.symbol()),
                        piece_font, (30, 30, 35)
                    )
    except Exception:
        pass

    for c, label in enumerate("abcdefgh"):
        _game_center(draw,
                     (left+c*cell, top+8*cell, left+(c+1)*cell, top+8*cell+28),
                     label, coord_font, (235,235,235))
    for r in range(8):
        _game_center(draw,
                     (5, top+r*cell, left-8, top+(r+1)*cell),
                     8-r, coord_font, (235,235,235))
    return _game_png(img)

def _render_ttt_visual(board):
    size = 760
    img = Image.new("RGB", (size, size), _BOARD_BG)
    draw = ImageDraw.Draw(img)
    cell = 210
    ox, oy = 65, 45
    mark_font = _game_font(112, True)
    number_font = _game_font(24, True)

    for r in range(3):
        for c in range(3):
            x1, y1 = ox+c*cell, oy+r*cell
            x2, y2 = x1+cell, y1+cell
            fill = _BOARD_LIGHT if (r+c) % 2 == 0 else _BOARD_DARK
            draw.rounded_rectangle((x1,y1,x2,y2), 20, fill=fill)
            value = board[r*3+c]
            if value == " ":
                value = str(r*3+c+1)
                font, color = number_font, (240,240,240)
            else:
                font, color = mark_font, (30,30,35)
            _game_center(draw, (x1,y1,x2,y2), value, font, color)
    return _game_png(img)

def _render_c4_visual(board):
    rows, cols = 6, 7
    size = 760
    img = Image.new("RGB", (size, size), _BOARD_BG)
    draw = ImageDraw.Draw(img)
    left, top = 52, 50
    cell = 92
    draw.rounded_rectangle((left-18, top-18, left+cols*cell+18, top+rows*cell+18),
                           28, fill=(48, 68, 85))
    radius = 31
    for r in range(rows):
        for c in range(cols):
            cx = left+c*cell+cell//2
            cy = top+r*cell+cell//2
            draw.ellipse((cx-radius,cy-radius,cx+radius,cy+radius),
                         fill=(236,236,236))
            value = board[r*cols+c]
            if value:
                fill = (205,65,65) if value == 1 else (232,190,50)
                draw.ellipse((cx-radius+6,cy-radius+6,cx+radius-6,cy+radius-6),
                             fill=fill)
    return _game_png(img)

def _render_ludo_visual(you, ai, goal=30):
    size = 760
    img = Image.new("RGB", (size, size), _BOARD_BG)
    draw = ImageDraw.Draw(img)
    title = _game_font(30, True)
    num = _game_font(17, True)
    draw.text((40, 25), "LUDO", font=title, fill=(245,245,245))

    left, top = 50, 100
    cell = 62
    for i in range(goal):
        rr, cc = divmod(i, 10)
        if rr % 2:
            cc = 9-cc
        cx = left+35+cc*cell
        cy = top+55+rr*125
        fill = _BOARD_LIGHT if i % 2 == 0 else _BOARD_DARK
        draw.rounded_rectangle((cx-24,cy-24,cx+24,cy+24), 10, fill=fill)
        _game_center(draw,(cx-24,cy-24,cx+24,cy+24),i+1,num,(35,35,40))

    def token(pos, fill):
        idx = max(0, min(goal-1, pos))
        rr, cc = divmod(idx, 10)
        if rr % 2:
            cc = 9-cc
        cx = left+35+cc*cell
        cy = top+55+rr*125
        draw.ellipse((cx-17,cy-17,cx+17,cy+17), fill=fill)

    token(you, (65,120,205))
    token(ai, (205,70,75))
    return _game_png(img)


CHESS_SESSIONS={}; LUDO_SESSIONS={}; TTT_SESSIONS={}

def _chess_board_text(board):
    try:
        rows = str(board).splitlines()
        # python-chess ASCII board -> convert pieces into a more visual Telegram board.
        piece_map = {
            "K":"♔","Q":"♕","R":"♖","B":"♗","N":"♘","P":"♙",
            "k":"♚","q":"♛","r":"♜","b":"♝","n":"♞","p":"♟",
            ".":"·"
        }
        out=["♟ ÉCHECS",""]
        for row_no, row in enumerate(rows[:8], 8):
            cells=row.split()
            if len(cells)==8:
                out.append(f"{row_no}  " + " ".join(piece_map.get(x,x) for x in cells))
        out.append("   a b c d e f g h")
        out.append("")
        out.append("À toi : /move e2e4")
        return "\n".join(out)
    except Exception:
        return "♟ ÉCHECS\n\nÀ toi : /move e2e4"


async def start_chess_game(update,context,uid,opponent=None):
    try:
        import chess
    except ImportError:
        await update.callback_query.message.reply_text("Les échecs nécessitent le module python-chess."); return
    board=chess.Board(); CHESS_SESSIONS[uid]={"board":board,"players":[uid,opponent] if opponent else [uid,None],"chat_id":update.effective_chat.id}
    if opponent: CHESS_SESSIONS[opponent]=CHESS_SESSIONS[uid]
    await update.callback_query.message.reply_photo(
        photo=_render_chess_visual(board),
        caption="♟ ÉCHECS\nTu joues avec les blancs.\nJoue avec /move e2e4.",
        reply_markup=game_back_menu()
    )

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
        await update.effective_message.reply_photo(
            photo=_render_chess_visual(board),
            caption="♟ ÉCHECS\nÀ toi : /move e2e4",
            reply_markup=game_back_menu()
        )
        return True
    except Exception:
        return False

async def chess_move_cmd(update, context):
    text = " ".join(context.args or []).strip()
    if not text:
        await safe_reply(update.effective_message, "Utilise /move e2e4.")
        return
    handled = await chess_move(update, context, text)
    if not handled:
        try:
            import chess  # noqa: F401
        except ImportError:
            await safe_reply(update.effective_message, "Le jeu d'échecs nécessite python-chess. Ajoute `python-chess` à requirements.txt puis redéploie.")
            return
        await safe_reply(update.effective_message, "Aucune partie d'échecs active. Lance une partie depuis Jeux.")

async def start_ludo_game(update,context,uid,opponent=None):
    players=[uid,opponent] if opponent else [uid,None]
    state={"players":players,"pos":{uid:0,**({opponent:0} if opponent else {})},"chat_id":update.effective_chat.id,"turn":0}
    LUDO_SESSIONS[uid]=state
    if opponent: LUDO_SESSIONS[opponent]=state
    await update.callback_query.message.reply_photo(
        photo=_render_ludo_visual(0, 0),
        caption="🎲 LUDO\nCourse jusqu'à 30 cases. Clique pour lancer le dé.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎲 Lancer le dé",callback_data=f"ludo:roll:{uid}")]])
    )

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
    you=s['pos'][uid]; ai=s.get('ai_pos',0)
    you_bar="🟦"*min(10,you//3)+"⬜"*max(0,10-min(10,you//3))
    ai_bar="🟥"*min(10,ai//3)+"⬜"*max(0,10-min(10,ai//3))
    await q.message.reply_photo(
        photo=_render_ludo_visual(you, ai),
        caption=f"🎲 LUDO\nToi : {you}/30   Alicia : {ai}/30\nDé : {dice}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎲 Lancer",callback_data=f"ludo:roll:{uid}")],
            [InlineKeyboardButton("🎮 Jeux",callback_data="game:menu")]
        ])
    )

async def start_ttt_game(update,context,uid,opponent=None):
    state={"board":[" "]*9,"players":[uid,opponent],"chat_id":update.effective_chat.id,"turn":uid}
    TTT_SESSIONS[uid]=state
    if opponent: TTT_SESSIONS[opponent]=state
    await send_ttt_board(update.callback_query.message,state)

async def send_ttt_board(message,state):
    b=state['board']
    kb=[]
    for r in range(3):
        row=[]
        for c in range(3):
            i=r*3+c
            label=b[i] if b[i] != ' ' else '·'
            row.append(
                InlineKeyboardButton(label, callback_data=f"ttt:{i}")
            )
        kb.append(row)
    kb.append([
        InlineKeyboardButton("🎮 Jeux", callback_data="game:menu")
    ])
    await message.reply_photo(
        photo=_render_ttt_visual(b),
        caption="⭕ MORPION\nÀ toi.",
        reply_markup=InlineKeyboardMarkup(kb)
    )


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
    await q.message.reply_photo(
        photo=_render_ttt_visual(b),
        caption="❌⭕ MORPION\nÀ toi.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(b[r*3+c] if b[r*3+c] != " " else str(r*3+c+1),callback_data=f"ttt:{r*3+c}") for c in range(3)] for r in range(3)])
    )

C4_SESSIONS={}

async def start_c4_game(update,context,uid,opponent=None):
    state={"board":[0]*42,"players":[uid,opponent],"turn":uid,"chat_id":update.effective_chat.id}
    C4_SESSIONS[uid]=state
    if opponent: C4_SESSIONS[opponent]=state
    await send_c4_board(update.callback_query.message,state)

async def send_c4_board(message,state):
    b=state['board']
    marks={0:"⚪",1:"🔴",2:"🟡"}
    lines=["🔴🟡 PUISSANCE 4",""]
    for r in range(6):
        lines.append(" ".join(marks[b[r*7+c]] for c in range(7)))
    lines.append("")
    lines.append("Choisis une colonne.")
    kb=[
        [InlineKeyboardButton(str(c+1),callback_data=f"c4:{c}") for c in range(7)],
        [InlineKeyboardButton("🎮 Jeux",callback_data="game:menu")]
    ]
    await message.reply_photo(
        photo=_render_c4_visual(b),
        caption="🔴🟡 PUISSANCE 4\nChoisis une colonne.",
        reply_markup=InlineKeyboardMarkup(kb)
    )


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
    marks={0:"⚪",1:"🔴",2:"🟡"}
    lines=["🔴🟡 PUISSANCE 4",""] + [
        " ".join(marks[b[r*7+c]] for c in range(7)) for r in range(6)
    ] + ["","Choisis une colonne."]
    await q.message.reply_photo(
        photo=_render_c4_visual(b),
        caption="🔴🟡 PUISSANCE 4\nChoisis une colonne.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(str(c+1),callback_data=f"c4:{c}") for c in range(7)],
            [InlineKeyboardButton("🎮 Jeux",callback_data="game:menu")]
        ])
    )


# ============================================================
# JEUX — CORRECTIFS DE ROBUSTESSE
# ============================================================

GAME_STATE = globals().get("GAME_STATE", {})

def _game_key(chat_id, user_id):
    return f"{chat_id}:{user_id}"

def _game_get(chat_id, user_id):
    return GAME_STATE.get(_game_key(chat_id, user_id))

def _game_set(chat_id, user_id, state):
    GAME_STATE[_game_key(chat_id, user_id)] = state

def _game_clear(chat_id, user_id):
    GAME_STATE.pop(_game_key(chat_id, user_id), None)

def _game_buttons(game):
    """Menu de jeux simple, compatible avec les callbacks existants."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("♟️ Échecs", callback_data="game:chess"),
            InlineKeyboardButton("🎲 Ludo", callback_data="game:ludo"),
        ],
        [
            InlineKeyboardButton("❌⭕ Morpion", callback_data="game:ttt"),
            InlineKeyboardButton("🔴 Puissance 4", callback_data="game:c4"),
        ],
        [
            InlineKeyboardButton("🧠 Mémoire", callback_data="game:memory"),
            InlineKeyboardButton("⚡ Réflexe", callback_data="game:reflex"),
        ],
        [
            InlineKeyboardButton("💣 Bombe", callback_data="game:bomb"),
            InlineKeyboardButton("🏎️ Course", callback_data="game:race"),
        ],
        [
            InlineKeyboardButton("🃏 Cartes", callback_data="game:cards"),
            InlineKeyboardButton("🎯 Devine", callback_data="game:guess"),
        ],
        [InlineKeyboardButton("❓ Quiz", callback_data="game:quiz")],
    ])

def _ttt_board(board):
    vals = board if isinstance(board, list) and len(board) == 9 else [" "] * 9
    shown = [v if v in ("❌", "⭕") else "·" for v in vals]
    return (
        f"{shown[0]} {shown[1]} {shown[2]}\n"
        f"{shown[3]} {shown[4]} {shown[5]}\n"
        f"{shown[6]} {shown[7]} {shown[8]}"
    )

def _ttt_winner(board):
    lines = (
        (0,1,2),(3,4,5),(6,7,8),
        (0,3,6),(1,4,7),(2,5,8),
        (0,4,8),(2,4,6)
    )
    for a,b,c in lines:
        if board[a] != " " and board[a] == board[b] == board[c]:
            return board[a]
    return "draw" if all(x != " " for x in board) else None

def _ttt_keyboard(board):
    rows = []
    for r in range(3):
        row = []
        for c in range(3):
            i = r * 3 + c
            label = board[i] if board[i] != " " else str(i + 1)
            row.append(InlineKeyboardButton(label, callback_data=f"game:ttt:{i}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("↩️ Jeux", callback_data="menu:games")])
    return InlineKeyboardMarkup(rows)

def _c4_keyboard(board, cols=7):
    rows = [[InlineKeyboardButton(str(i+1), callback_data=f"game:c4:{i}") for i in range(cols)]]
    for r in board:
        rows.append([
            InlineKeyboardButton(cell if cell != " " else "·", callback_data="game:c4:noop")
            for cell in r
        ])
    rows.append([InlineKeyboardButton("↩️ Jeux", callback_data="menu:games")])
    return InlineKeyboardMarkup(rows)

def _c4_winner(board):
    h, w = len(board), len(board[0])
    for r in range(h):
        for c in range(w):
            p = board[r][c]
            if p == " ":
                continue
            for dr, dc in ((0,1),(1,0),(1,1),(1,-1)):
                cells = []
                for k in range(4):
                    rr, cc = r + dr*k, c + dc*k
                    if 0 <= rr < h and 0 <= cc < w:
                        cells.append(board[rr][cc])
                if len(cells) == 4 and all(x == p for x in cells):
                    return p
    return "draw" if all(x != " " for row in board for x in row) else None

def _c4_drop(board, col, piece):
    for r in range(len(board)-1, -1, -1):
        if board[r][col] == " ":
            board[r][col] = piece
            return True
    return False

def _simple_game_menu_text():
    return (
        "🎮 <b>Jeux Alicia</b>\n\n"
        "Choisis un jeu 👇\n"
        "Des parties rapides, simples et sans spam."
    )


async def game_callback(update, context):
    q = update.callback_query
    if not q:
        return
    try:
        await q.answer()
    except Exception:
        pass
    q = update.callback_query
    data = q.data or ""

    # Robust game state for the lightweight games.
    if data == "game:ttt":
        board = [" "] * 9
        _game_set(update.effective_chat.id, update.effective_user.id,
                  {"type": "ttt", "board": board, "turn": "❌"})
        await q.edit_message_text(
            "❌⭕ <b>Morpion</b>\n\nÀ toi de jouer : <b>❌</b>\n\n" + _ttt_board(board),
            reply_markup=_ttt_keyboard(board)
        )
        return

    if data.startswith("game:ttt:"):
        state = _game_get(update.effective_chat.id, update.effective_user.id)
        if not state or state.get("type") != "ttt":
            await q.edit_message_text("La partie est terminée. Lance une nouvelle partie.",
                                      reply_markup=_game_buttons("menu"))
            return
        try:
            idx = int(data.rsplit(":", 1)[1])
        except Exception:
            return
        board = state["board"]
        if idx < 0 or idx >= 9 or board[idx] != " " or state.get("turn") != "❌":
            return

        board[idx] = "❌"
        result = _ttt_winner(board)
        if result == "❌":
            _game_clear(update.effective_chat.id, update.effective_user.id)
            await q.edit_message_text("🎉 <b>Tu as gagné !</b>\n\n" + _ttt_board(board),
                                      reply_markup=_game_buttons("menu"))
            return
        if result == "draw":
            _game_clear(update.effective_chat.id, update.effective_user.id)
            await q.edit_message_text("🤝 <b>Match nul.</b>\n\n" + _ttt_board(board),
                                      reply_markup=_game_buttons("menu"))
            return

        # Alicia joue avec une stratégie simple.
        free = [i for i, v in enumerate(board) if v == " "]
        if free:
            # Priorité au centre, puis aux coins, puis au reste.
            ai_idx = next((i for i in (4,0,2,6,8,1,3,5,7) if i in free), free[0])
            board[ai_idx] = "⭕"

        result = _ttt_winner(board)
        if result == "⭕":
            title = "😏 <b>Alicia gagne.</b>"
        elif result == "draw":
            title = "🤝 <b>Match nul.</b>"
        else:
            title = "❌⭕ <b>À toi : ❌</b>"

        if result:
            _game_clear(update.effective_chat.id, update.effective_user.id)
            await q.edit_message_text(title + "\n\n" + _ttt_board(board),
                                      reply_markup=_game_buttons("menu"))
        else:
            await q.edit_message_text(title + "\n\n" + _ttt_board(board),
                                      reply_markup=_ttt_keyboard(board))
        return

    if data == "game:c4":
        board = [[" "] * 7 for _ in range(6)]
        _game_set(update.effective_chat.id, update.effective_user.id,
                  {"type": "c4", "board": board, "turn": "🔴"})
        await q.edit_message_text(
            "🔴🟡 <b>Puissance 4</b>\n\nÀ toi : <b>🔴</b>\n\n" +
            "\n".join(" ".join(row) for row in board),
            reply_markup=_c4_keyboard(board)
        )
        return

    if data.startswith("game:c4:"):
        part = data.rsplit(":", 1)[1]
        if part == "noop":
            return
        state = _game_get(update.effective_chat.id, update.effective_user.id)
        if not state or state.get("type") != "c4":
            await q.edit_message_text("La partie est terminée.",
                                      reply_markup=_game_buttons("menu"))
            return
        try:
            col = int(part)
        except Exception:
            return
        board = state["board"]
        if not 0 <= col < 7 or not _c4_drop(board, col, "🔴"):
            return

        result = _c4_winner(board)
        if result:
            _game_clear(update.effective_chat.id, update.effective_user.id)
            title = "🎉 <b>Tu as gagné !</b>" if result == "🔴" else "🤝 <b>Match nul.</b>"
            await q.edit_message_text(
                title + "\n\n" + "\n".join(" ".join(row) for row in board),
                reply_markup=_game_buttons("menu")
            )
            return

        # Alicia pose un jeton dans une colonne disponible.
        choices = list(range(7))
        random.shuffle(choices)
        ai_col = next((c for c in choices if board[0][c] == " "), None)
        if ai_col is not None:
            _c4_drop(board, ai_col, "🟡")

        result = _c4_winner(board)
        if result:
            _game_clear(update.effective_chat.id, update.effective_user.id)
            title = "😏 <b>Alicia gagne.</b>" if result == "🟡" else "🤝 <b>Match nul.</b>"
            await q.edit_message_text(
                title + "\n\n" + "\n".join(" ".join(row) for row in board),
                reply_markup=_game_buttons("menu")
            )
        else:
            await q.edit_message_text(
                "🔴🟡 <b>À toi : 🔴</b>\n\n" +
                "\n".join(" ".join(row) for row in board),
                reply_markup=_c4_keyboard(board)
            )
        return

    # Robust game state
    uid = q.from_user.id

    await q.answer()

    if not data.startswith("game:"):
        return

    game = data.split(":", 1)[1]

    if game == "menu":
        await q.message.edit_text("🎮 JEUX\nChoisis une partie.", reply_markup=game_menu())
        return

    if game == "close":
        try:
            await q.message.delete()
        except Exception:
            await q.message.edit_text("Jeux fermés.")
        return

    if game == "ranking":
        await q.message.edit_text(
            await all_time_ranking_text(),
            reply_markup=game_back_menu()
        )
        return

    if game == "chess":
        await start_chess_game(
            update, context, uid,
            GAME_SESSIONS.get(uid, {}).get("opponent")
        )
    elif game == "ludo":
        await start_ludo_game(
            update, context, uid,
            GAME_SESSIONS.get(uid, {}).get("opponent")
        )
    elif game == "ttt":
        await start_ttt_game(
            update, context, uid,
            GAME_SESSIONS.get(uid, {}).get("opponent")
        )
    elif game == "c4":
        await start_c4_game(
            update, context, uid,
            GAME_SESSIONS.get(uid, {}).get("opponent")
        )
    elif game == "guess":
        GUESS[uid] = random.randint(1,20)
        await q.message.edit_text(
            "🎯 DEVINE\\n\\nJ'ai choisi un nombre de 1 à 20.\\nUtilise /guess <nombre>.",
            reply_markup=game_back_menu()
        )
    elif game == "quiz":
        await q.message.edit_text(
            "🧠 QUIZ\\n\\nUtilise /quiz test pour jouer maintenant.",
            reply_markup=game_back_menu()
        )
    elif game in ("reflex","memory","bomb","boss","race","cards"):
        await launch_mini_game(update,context,game,uid)


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

async def discussions_cmd(update, context):
    """Liste séparément les discussions privées et les groupes connus d'Alicia.
    Cette commande ne modifie ni ne remplace /stats.
    """
    if not admin_ok(update):
        await safe_reply(update.effective_message, "Commande réservée à l'administration.")
        return

    con = db()
    private_rows = con.execute(
        """SELECT chat_id,title,username,messages,last_seen
           FROM chats WHERE chat_type='private'
           ORDER BY last_seen DESC LIMIT 100"""
    ).fetchall()
    group_rows = con.execute(
        """SELECT chat_id,title,username,messages,last_seen
           FROM chats WHERE chat_type IN ('group','supergroup')
           ORDER BY last_seen DESC LIMIT 100"""
    ).fetchall()
    con.close()

    lines = [
        "💬 DISCUSSIONS ALICIA",
        "",
        f"👤 DISCUSSIONS PRIVÉES · {len(private_rows)}",
    ]

    if private_rows:
        for i, (chat_id, title, username, messages, last_seen) in enumerate(private_rows, 1):
            name = title or (f"@{username}" if username else f"Utilisateur {chat_id}")
            link = f"tg://user?id={chat_id}"
            lines.append(f"{i}. {name}")
            lines.append(f"   ID : {chat_id} · {messages} messages")
            lines.append(f"   🔗 {link}")
    else:
        lines.append("Aucune discussion privée enregistrée.")

    lines.extend(["", f"👥 GROUPES · {len(group_rows)}"])

    if group_rows:
        for i, (chat_id, title, username, messages, last_seen) in enumerate(group_rows, 1):
            name = title or (f"@{username}" if username else f"Groupe {chat_id}")
            if username:
                link = f"https://t.me/{username.lstrip('@')}"
            else:
                link = "Lien public indisponible"
            lines.append(f"{i}. {name}")
            lines.append(f"   ID : {chat_id} · {messages} messages")
            lines.append(f"   🔗 {link}")
    else:
        lines.append("Aucun groupe enregistré.")

    # Telegram impose une limite de longueur par message.
    output = "\n".join(lines)
    chunks = [output[i:i+3900] for i in range(0, len(output), 3900)] or ["Aucune discussion."]
    for chunk in chunks:
        await safe_reply(update.effective_message, chunk)

async def chat_cmd(update, context):
    """Admin-only private chat overview. Replaces the old /chat command."""
    await discussions_cmd(update, context)

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
    chat = update.effective_chat
    if not chat:
        return
    con = db()
    legacy = con.execute(
        """SELECT user_id,MAX(name),COALESCE(SUM(points),0),COALESCE(SUM(wins),0),COALESCE(SUM(losses),0)
           FROM scores WHERE chat_id=? GROUP BY user_id""",
        (chat.id,),
    ).fetchall()
    anime = con.execute(
        """SELECT user_id,MAX(player_name),COALESCE(SUM(points),0),COUNT(*),0
           FROM anime_quiz_answers WHERE chat_id=? GROUP BY user_id""",
        (chat.id,),
    ).fetchall()
    con.close()

    merged = {}
    for uid, name, pts, wins, losses in legacy:
        merged[int(uid)] = [int(uid), name or str(uid), int(pts or 0), int(wins or 0), int(losses or 0)]
    for uid, name, pts, wins, losses in anime:
        row = merged.setdefault(int(uid), [int(uid), name or str(uid), 0, 0, 0])
        row[1] = row[1] or name or str(uid)
        row[2] += int(pts or 0)
        row[3] += int(wins or 0)

    rows = sorted(merged.values(), key=lambda r: (r[2], r[3]), reverse=True)[:10]
    if not rows:
        await safe_reply(update.effective_message, "Le classement est vide pour le moment.")
        return
    await send_modern_leaderboard(
        context.bot, chat.id, "🏆 CLASSEMENT DU GROUPE", rows
    )

async def score_cmd(update, context):
    pts, wins, losses = get_score(update.effective_chat.id, update.effective_user.id)
    xp_points, level = get_xp(update.effective_user.id)
    await safe_reply(
        update.effective_message,
        f"{display_name(update.effective_user)} · {pts} pts · {wins} victoires"
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
        "/stats /chat /groups /user ID\n"
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
# ALICIA COMMUNITY 2.0 — COMMANDES COMPLÈTES
# ============================================================
async def community_cmd(update, context):
    await safe_reply(
        update.effective_message,
        "🎮 ALICIA COMMUNITY\n\n"
        "/profile — profil communauté\n"
        "/rep — réputation\n"
        "/coins — monnaie virtuelle\n"
        "/shop — boutique\n"
        "/missions — missions\n"
        "/duel — défier un joueur\n"
        "/team — équipes\n"
        "/auto — automatisations\n"
        "/automod — modération\n"
        "/event — événements\n"
        "/roles — rôles automatiques\n"
        "/schedule — publications de canal\n"
        "/scheduled — publications programmées"
    )


async def community_profile_cmd(update, context):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return
    if not is_group(chat):
        await safe_reply(update.effective_message, "Cette fonction est prévue pour les groupes.")
        return
    con = db()
    rep_row = con.execute(
        "SELECT reputation,messages FROM community_rep WHERE chat_id=? AND user_id=?",
        (chat.id, user.id),
    ).fetchone()
    coin_row = con.execute(
        "SELECT balance,lifetime FROM community_coins WHERE chat_id=? AND user_id=?",
        (chat.id, user.id),
    ).fetchone()
    score = con.execute(
        "SELECT points,wins,losses FROM scores WHERE chat_id=? AND user_id=?",
        (chat.id, user.id),
    ).fetchone()
    con.close()
    rep, msgs = rep_row or (0, 0)
    coins, lifetime = coin_row or (0, 0)
    pts, wins, losses = score or (0, 0, 0)
    await safe_reply(
        update.effective_message,
        f"👤 {display_name(user)}\n\n"
        f"⭐ Réputation : {rep}\n"
        f"💬 Messages : {msgs}\n"
        f"🪙 Coins : {coins}\n"
        f"🏆 Points : {pts}\n"
        f"✅ Victoires : {wins}\n"
        f"❌ Défaites : {losses}"
    )


async def community_shop_cmd(update, context):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user or not is_group(chat):
        await safe_reply(update.effective_message, "La boutique est disponible dans les groupes.")
        return
    con = db()
    rows = con.execute(
        "SELECT item_key,title,price,reward FROM community_shop WHERE chat_id=? AND active=1 ORDER BY id",
        (chat.id,),
    ).fetchall()
    if not rows:
        defaults = [
            ("xp10", "Bonus XP", 50, "10 XP"),
            ("rep1", "Réputation", 80, "1 réputation"),
            ("mystery", "Coffre mystère", 150, "Récompense aléatoire"),
        ]
        for item in defaults:
            con.execute(
                "INSERT OR IGNORE INTO community_shop(chat_id,item_key,title,price,reward,active) VALUES(?,?,?,?,?,1)",
                (chat.id, *item),
            )
        con.commit()
        rows = con.execute(
            "SELECT item_key,title,price,reward FROM community_shop WHERE chat_id=? AND active=1 ORDER BY id",
            (chat.id,),
        ).fetchall()
    con.close()
    lines = ["🛒 BOUTIQUE", ""]
    for key, title, price, reward in rows:
        lines.append(f"• {key} — {title} : {price} coins — {reward}")
    lines.append("\nUtilise /shop buy NOM")
    if context.args and context.args[0].lower() == "buy" and len(context.args) >= 2:
        key = context.args[1].lower()
        con = db()
        item = con.execute(
            "SELECT title,price,reward FROM community_shop WHERE chat_id=? AND item_key=? AND active=1",
            (chat.id, key),
        ).fetchone()
        bal = con.execute(
            "SELECT balance FROM community_coins WHERE chat_id=? AND user_id=?",
            (chat.id, user.id),
        ).fetchone()
        balance = int(bal[0]) if bal else 0
        if not item:
            con.close()
            await safe_reply(update.effective_message, "Article introuvable.")
            return
        title, price, reward = item
        if balance < int(price):
            con.close()
            await safe_reply(update.effective_message, f"Il te faut {price} coins. Tu en as {balance}.")
            return
        con.execute(
            "UPDATE community_coins SET balance=balance-? WHERE chat_id=? AND user_id=?",
            (price, chat.id, user.id),
        )
        con.execute(
            """INSERT INTO community_inventory(chat_id,user_id,item_key,quantity,updated_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(chat_id,user_id,item_key) DO UPDATE SET quantity=quantity+1,updated_at=excluded.updated_at""",
            (chat.id, user.id, key, 1, now()),
        )
        con.commit()
        con.close()
        await safe_reply(update.effective_message, f"🛒 {title} acheté. Récompense : {reward}.")
        return
    await safe_reply(update.effective_message, "\n".join(lines))


async def community_missions_cmd(update, context):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user or not is_group(chat):
        await safe_reply(update.effective_message, "Les missions sont disponibles dans les groupes.")
        return
    con = db()
    defaults = [
        ("daily_chat", "Bavard", "Écris 10 messages", 10, 20, 25),
        ("daily_quiz", "Quiz", "Participe à un quiz", 1, 30, 40),
        ("daily_rep", "Présence", "Gagne 5 réputation", 5, 25, 30),
    ]
    for key, title, desc, target, xp_reward, coin_reward in defaults:
        con.execute(
            """INSERT OR IGNORE INTO community_missions
               (chat_id,mission_key,title,description,target,reward_xp,reward_coins,active)
               VALUES(?,?,?,?,?,?,?,1)""",
            (chat.id, key, title, desc, target, xp_reward, coin_reward),
        )
    rows = con.execute(
        "SELECT mission_key,title,description,target,reward_xp,reward_coins FROM community_missions WHERE chat_id=? AND active=1 ORDER BY id",
        (chat.id,),
    ).fetchall()
    con.close()
    lines = ["🎯 MISSIONS", ""]
    for key, title, desc, target, xp_reward, coin_reward in rows:
        lines.append(f"• {title} — {desc} ({target})\n  +{xp_reward} XP • +{coin_reward} coins")
    await safe_reply(update.effective_message, "\n".join(lines))


async def community_duel_cmd(update, context):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user or not is_group(chat):
        await safe_reply(update.effective_message, "Les duels se font dans les groupes.")
        return
    opponent = update.effective_message.reply_to_message.from_user if update.effective_message.reply_to_message else None
    if not opponent or opponent.id == user.id or opponent.is_bot:
        await safe_reply(update.effective_message, "Réponds au message d'un membre pour le défier.")
        return
    con = db()
    cur = con.execute(
        """INSERT INTO game_duels(chat_id,challenger_id,opponent_id,status,game_type,created_at)
           VALUES(?,?,?,?,?,?)""",
        (chat.id, user.id, opponent.id, "pending", "quiz", now()),
    )
    duel_id = cur.lastrowid
    con.commit()
    con.close()
    await safe_reply(
        update.effective_message,
        f"⚔️ {display_name(user)} défie {display_name(opponent)}.\n"
        f"Duel #{duel_id}. {display_name(opponent)}, réponds /accept_duel {duel_id}."
    )


async def community_team_cmd(update, context):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user or not is_group(chat):
        await safe_reply(update.effective_message, "Les équipes se gèrent dans les groupes.")
        return
    args = context.args or []
    con = db()
    if not args:
        teams = con.execute(
            "SELECT name,captain_id FROM game_teams WHERE chat_id=? ORDER BY name",
            (chat.id,),
        ).fetchall()
        con.close()
        await safe_reply(
            update.effective_message,
            "👥 ÉQUIPES\n" + ("\n".join(f"• {n}" for n, _ in teams) or "Aucune équipe.")
        )
        return
    if args[0].lower() == "create":
        name = " ".join(args[1:]).strip()[:40]
        if not name:
            con.close()
            await safe_reply(update.effective_message, "Utilise /team create NomEquipe")
            return
        con.execute(
            "INSERT OR IGNORE INTO game_teams(chat_id,name,captain_id,created_at) VALUES(?,?,?,?)",
            (chat.id, name, user.id, now()),
        )
        con.commit()
        con.close()
        await safe_reply(update.effective_message, f"👥 Équipe créée : {name}")
        return
    if args[0].lower() == "join" and len(args) >= 2:
        name = " ".join(args[1:]).strip()[:40]
        row = con.execute(
            "SELECT id FROM game_teams WHERE chat_id=? AND name=?",
            (chat.id, name),
        ).fetchone()
        if not row:
            con.close()
            await safe_reply(update.effective_message, "Équipe introuvable.")
            return
        con.execute(
            "INSERT OR IGNORE INTO game_team_members(team_id,user_id,joined_at) VALUES(?,?,?)",
            (row[0], user.id, now()),
        )
        con.commit()
        con.close()
        await safe_reply(update.effective_message, f"Tu as rejoint {name}.")
        return
    con.close()
    await safe_reply(update.effective_message, "/team create NomEquipe\n/team join NomEquipe")


async def community_auto_cmd(update, context):
    chat = update.effective_chat
    if not chat or not is_group(chat):
        await safe_reply(update.effective_message, "Les automatisations sont configurées dans les groupes.")
        return
    args = context.args or []
    if not args:
        await safe_reply(update.effective_message, "/auto quiz on|off\n/auto welcome on|off\n/auto goodbye on|off")
        return
    key = args[0].lower()
    enabled = len(args) > 1 and args[1].lower() in ("on", "1", "true", "oui")
    con = db()
    con.execute(
        """INSERT INTO community_automations(chat_id,automation_key,enabled,config_json,updated_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(chat_id,automation_key) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at""",
        (chat.id, key, 1 if enabled else 0, "{}", now()),
    )
    con.commit()
    con.close()
    await safe_reply(update.effective_message, f"Automatisation {key} : {'activée' if enabled else 'désactivée'}.")


async def community_mod_cmd(update, context):
    chat = update.effective_chat
    if not chat or not is_group(chat):
        await safe_reply(update.effective_message, "La modération se configure dans les groupes.")
        return
    args = context.args or []
    enabled = bool(args and args[0].lower() in ("on", "1", "true", "oui"))
    con = db()
    con.execute(
        """INSERT INTO community_settings(chat_id,automod_enabled,updated_at)
           VALUES(?,?,?)
           ON CONFLICT(chat_id) DO UPDATE SET automod_enabled=excluded.automod_enabled,updated_at=excluded.updated_at""",
        (chat.id, 1 if enabled else 0, now()),
    )
    con.commit()
    con.close()
    await safe_reply(update.effective_message, f"Automod : {'activé' if enabled else 'désactivé'}.")


async def community_channel_cmd(update, context):
    chat = update.effective_chat
    if not chat or chat.type != ChatType.CHANNEL:
        await safe_reply(update.effective_message, "Cette commande est destinée aux canaux.")
        return
    await safe_reply(
        update.effective_message,
        "📢 Mode canal Alicia activé.\nUtilise /schedule pour programmer une publication."
    )


async def community_event_cmd(update, context):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user or not is_group(chat):
        await safe_reply(update.effective_message, "Les événements sont disponibles dans les groupes.")
        return
    args = context.args or []
    con = db()
    if args and args[0].lower() == "create":
        title = " ".join(args[1:]).strip()[:100] or "Événement Alicia"
        key = hashlib.sha1(f"{chat.id}:{title}".encode()).hexdigest()[:20]
        con.execute(
            """INSERT OR IGNORE INTO community_events
               (chat_id,event_key,title,description,starts_at,ends_at,status,reward_coins,reward_xp)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (chat.id, key, title, "", now(), "", "open", 100, 50),
        )
        con.commit()
        con.close()
        await safe_reply(update.effective_message, f"🎉 Événement créé : {title}\n/event join")
        return
    row = con.execute(
        "SELECT id,title,description,status FROM community_events WHERE chat_id=? ORDER BY id DESC LIMIT 1",
        (chat.id,),
    ).fetchone()
    if not row:
        con.close()
        await safe_reply(update.effective_message, "Aucun événement. Utilise /event create Nom.")
        return
    if args and args[0].lower() == "join":
        con.execute(
            "INSERT OR IGNORE INTO community_event_players(event_id,user_id,score,joined_at) VALUES(?,?,0,?)",
            (row[0], user.id, now()),
        )
        con.commit()
        con.close()
        await safe_reply(update.effective_message, f"🎉 {display_name(user)}, tu participes à {row[1]}.")
        return
    players = con.execute(
        "SELECT user_id,score FROM community_event_players WHERE event_id=? ORDER BY score DESC",
        (row[0],),
    ).fetchall()
    con.close()
    await safe_reply(
        update.effective_message,
        f"🎉 {row[1]}\n" + ("\n".join(f"{i}. {uid} — {score} pts" for i, (uid, score) in enumerate(players, 1)) or "Aucun participant.")
    )


async def community_callback(update, context):
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer()
    except Exception:
        pass
    data = query.data or ""
    if data == "community:help":
        await query.edit_message_text(
            "Alicia Community : profils, réputation, coins, missions, équipes, duels et événements."
        )


async def community_message_tracker(update, context):
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message
    if not chat or not user or not msg or not is_group(chat) or not msg.text:
        return
    try:
        con = db()
        con.execute(
            """INSERT INTO community_rep(chat_id,user_id,reputation,messages,last_active)
               VALUES(?,?,?,?,?)
               ON CONFLICT(chat_id,user_id) DO UPDATE SET
               messages=community_rep.messages+1,last_active=excluded.last_active""",
            (chat.id, user.id, 0, 1, now()),
        )
        con.execute(
            """INSERT INTO community_coins(chat_id,user_id,balance,lifetime)
               VALUES(?,?,1,1)
               ON CONFLICT(chat_id,user_id) DO UPDATE SET
               balance=community_coins.balance+1,lifetime=community_coins.lifetime+1""",
            (chat.id, user.id),
        )
        # 1 réputation tous les 10 messages.
        row = con.execute(
            "SELECT messages FROM community_rep WHERE chat_id=? AND user_id=?",
            (chat.id, user.id),
        ).fetchone()
        if row and int(row[0]) % 10 == 0:
            con.execute(
                "UPDATE community_rep SET reputation=reputation+1 WHERE chat_id=? AND user_id=?",
                (chat.id, user.id),
            )
        con.commit()
        con.close()
    except Exception:
        log.debug("Community tracker skipped", exc_info=True)

async def community_roles_cmd(update, context):
    chat = update.effective_chat
    args = context.args
    if not args:
        await safe_reply(
            update.effective_message,
            "👑 <b>RÔLES AUTOMATIQUES</b>\n━━━━━━━━━━━━━━━━━━\n"
            "/roles add 100 Expert\n"
            "/roles list\n"
            "/roles remove 100",
            parse_mode="HTML"
        )
        return
    action=args[0].lower()
    con=db()
    if action=="add" and len(args)>=3:
        try:
            threshold=int(args[1])
        except ValueError:
            con.close(); await safe_reply(update.effective_message,"Le seuil doit être un nombre."); return
        title=" ".join(args[2:])[:60]
        con.execute(
            """INSERT INTO community_role_rules(chat_id,rule_key,threshold,role_title,active)
               VALUES(?,?,?,?,1)
               ON CONFLICT(chat_id,rule_key) DO UPDATE SET threshold=?,role_title=?,active=1""",
            (chat.id,f"rep_{threshold}",threshold,title,threshold,title)
        )
        con.commit()
        msg=f"👑 Rôle automatique ajouté : <b>{html_lib.escape(title)}</b> à {threshold} de réputation."
    elif action=="remove" and len(args)>=2:
        try: threshold=int(args[1])
        except ValueError: threshold=0
        con.execute("DELETE FROM community_role_rules WHERE chat_id=? AND rule_key=?",(chat.id,f"rep_{threshold}"))
        con.commit()
        msg="👑 Règle supprimée."
    elif action=="list":
        rows=con.execute(
            "SELECT threshold,role_title FROM community_role_rules WHERE chat_id=? AND active=1 ORDER BY threshold",
            (chat.id,)
        ).fetchall()
        msg="👑 <b>RÔLES AUTOMATIQUES</b>\n━━━━━━━━━━━━━━━━━━\n" + (
            "\n".join(f"• {threshold} ⭐ → {html_lib.escape(title)}" for threshold,title in rows)
            or "Aucune règle."
        )
    else:
        msg="Utilise /roles add 100 Expert, /roles list ou /roles remove 100."
    con.close()
    await safe_reply(update.effective_message,msg,parse_mode="HTML")

async def community_schedule_cmd(update, context):
    chat=update.effective_chat
    if chat.type != ChatType.CHANNEL:
        await safe_reply(update.effective_message,"Cette fonction est destinée aux canaux.")
        return
    args=context.args
    if not args:
        await safe_reply(
            update.effective_message,
            "📅 <b>PROGRAMMATION</b>\n━━━━━━━━━━━━━━━━━━\n"
            "/schedule 2026-09-29T18:00 Ton message",
            parse_mode="HTML"
        )
        return
    schedule=args[0]
    content=" ".join(args[1:]).strip()[:3500]
    if not content:
        await safe_reply(update.effective_message,"Ajoute le contenu de la publication."); return
    con=db()
    con.execute(
        "INSERT INTO channel_schedules(chat_id,owner_id,content,schedule,active,created_at) VALUES(?,?,?,?,1,?)",
        (chat.id,update.effective_user.id,content,schedule,now())
    )
    con.commit(); con.close()
    await safe_reply(update.effective_message,
                     f"📅 Publication programmée pour <b>{html_lib.escape(schedule)}</b>.",
                     parse_mode="HTML")

async def community_schedule_list_cmd(update, context):
    chat=update.effective_chat
    con=db()
    rows=con.execute(
        "SELECT id,schedule,content,active FROM channel_schedules WHERE chat_id=? ORDER BY id DESC LIMIT 20",
        (chat.id,)
    ).fetchall()
    con.close()
    lines=["📅 <b>PUBLICATIONS PROGRAMMÉES</b>","━━━━━━━━━━━━━━━━━━"]
    lines += [f"#{i} • {html_lib.escape(s)} • {'ON' if a else 'OFF'}\n{html_lib.escape(c[:80])}" for i,s,c,a in rows]
    if not rows: lines.append("Aucune publication programmée.")
    await safe_reply(update.effective_message,"\n".join(lines),parse_mode="HTML")


# ============================================================
# TEXT HANDLER
# ============================================================
QUICK = {
    "salut": ["Salut.", "Coucou.", "Hey.", "Yo."],
    "bonjour": ["Bonjour.", "Coucou.", "Salut."],
    "bonsoir": ["Bonsoir.", "Coucou.", "Hey."],
    "merci": ["De rien.", "T’inquiète.", "Avec plaisir."],
    "yo": ["Yo.", "Hey.", "Salut."],
    "ok": ["D’accord.", "Ok.", "Ça marche."],
    "ca va": ["Oui, tranquille.", "Ça va.", "Tranquille.", "Ouais, ça va."],
    "ça va": ["Oui, tranquille.", "Ça va.", "Tranquille.", "Ouais, ça va."],
}

async def _background_message_bookkeeping(chat, user, text):
    """Persistance non critique exécutée hors du chemin de réponse.

    Aucune suppression ni modification destructive : on conserve les anciennes
    données et on effectue exactement les mêmes écritures qu'avant.
    """
    try:
        record_message(chat, user, text)
        await save_memory_summary(user.id, text)
    except Exception as exc:
        log.warning("Background message bookkeeping skipped: %s", exc)


INSULT_PATTERNS = re.compile(
    r"\b(?:idiot|idiote|imbecile|imbécile|debile|débile|con|connard|connasse|pute|putain|salope|salaud|batard|bâtard|clown|bouffon|abruti|abrutie|crétin|cretin|merde)\b",
    re.IGNORECASE,
)

INSULT_COMEBACKS = [
    "Parle mieux, petit clown.",
    "T'es mignon quand tu fais le bouffon.",
    "Oh le petit idiot qui s'énerve.",
    "Calme-toi, abruti, tu vas te fatiguer.",
    "Tu m'insultes avec ça ? Fais un effort, bouffon.",
    "Quel clown celui-là.",
    "T'as pas trouvé mieux, petit malin ?",
    "Va te calmer deux minutes, espèce de bouffon.",
]

def is_direct_insult(text):
    return bool(text and INSULT_PATTERNS.search(text))

def insult_comeback():
    return random.choice(INSULT_COMEBACKS)


async def text_handler(update, context):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not user or not chat:
        return

    user_text = (msg.text or "").strip()
    if not user_text:
        return

    # Les liens/médias gardent leur comportement actuel.
    media_url = extract_url(user_text)
    if media_url and media_url_supported(media_url):
        low_url = user_text.lower()
        if (
            not is_group(chat)
            or called_alicia(update)
            or any(x in low_url for x in (
                "youtube", "youtu.be", "tiktok", "instagram",
                "facebook", "twitter", "x.com"
            ))
        ):
            kind = "audio" if any(
                x in low_url for x in ("song", "music", "audio")
            ) else "video"
            if user_text == media_url:
                await send_downloaded_media(update, context, media_url, kind)
                return

    # Dans un groupe Alicia répond seulement lorsqu'on lui parle.
    if is_group(chat) and not called_alicia(update):
        return

    # Sauvegarde en arrière-plan : elle ne bloque jamais le début de la réponse.
    try:
        context.application.create_task(
            _background_message_bookkeeping(chat, user, user_text),
            update=update
        )
    except Exception:
        pass

    low = re.sub(
        r"[^a-zàâçéèêëîïôûùüÿñæœ0-9 ]+",
        " ",
        user_text.lower()
    ).strip()

    # ============================================================
    # ALICIA ÉCRIT IMMÉDIATEMENT
    # ============================================================
    # IMPORTANT : on attend le PREMIER envoi de "typing" avant de lancer
    # l'IA. Ainsi Telegram affiche réellement "Alicia écrit…" dès que
    # possible, au lieu de créer seulement une tâche en arrière-plan.
    await safe_chat_action(context.bot, chat.id, "typing")

    typing_stop = asyncio.Event()
    typing_task = asyncio.create_task(
        _alicia_typing_loop(context.bot, chat.id, typing_stop)
    )
    started = time.monotonic()

    try:
        # Réponse locale ou IA.
        if is_direct_insult(user_text):
            reply = insult_comeback()
        elif low in QUICK:
            value = QUICK[low]
            reply = (
                random.choice(value)
                if isinstance(value, (list, tuple))
                else value
            )
        else:
            try:
                reply = await ask_ai(chat.id, user.id, user_text)
            except Exception as exc:
                log.exception("Alicia AI response failed: %s", exc)
                reply = random.choice([
                    "Hmm, attends.",
                    "Deux secondes.",
                    "Je réfléchis.",
                    "Hmm.",
                ])

        # Minimum 4 secondes d'écriture à partir du premier "typing".
        remaining = 4.0 - (time.monotonic() - started)
        if remaining > 0:
            await asyncio.sleep(remaining)

        # Nettoyage de la réponse.
        reply = str(reply or "Hmm.").replace("***", "").replace("**", "").strip()

        # Alicia ne révèle jamais son identité technique.
        _identity_replacements = [
            (r"(?i)je suis un bot", "Je suis Alicia"),
            (r"(?i)je suis une bot", "Je suis Alicia"),
            (r"(?i)je suis une ia", "Je suis Alicia"),
            (r"(?i)je suis une intelligence artificielle", "Je suis Alicia"),
            (r"(?i)je suis un robot", "Je suis Alicia"),
            (r"(?i)je suis un assistant(?:e)? virtuel(?:le)?", "Je suis Alicia"),
            (r"(?i)je suis un programme", "Je suis Alicia"),
        ]
        for _pattern, _replacement in _identity_replacements:
            reply = re.sub(_pattern, _replacement, reply)

        # Si la réponse est manifestement coupée, on la complète AVANT
        # d'arrêter "typing". Le délai de 5 s est donc un minimum.
        if reply and len(reply) >= 18 and not re.search(r"[.!?…]$", reply):
            try:
                completion_prompt = (
                    "Complète uniquement la dernière phrase de cette réponse "
                    "pour qu'elle soit grammaticalement terminée. "
                    "Ne change pas le début, n'ajoute aucune explication et "
                    "reste très court. Réponse à compléter : " + reply
                )
                completed = await ask_ai(chat.id, user.id, completion_prompt)
                if completed:
                    completed = str(completed).replace("**", "").strip()
                    if completed and len(completed) > len(reply):
                        reply = completed
            except Exception:
                pass

        if not reply:
            reply = "Hmm."
        # Ne coupe jamais une phrase au milieu.
        if len(reply) > 900:
            sentences = re.split(r"(?<=[.!?…])\s+", reply)
            kept = []
            total = 0
            for sentence in sentences:
                sentence = sentence.strip()
                if not sentence:
                    continue
                if total and total + 1 + len(sentence) > 900:
                    break
                kept.append(sentence)
                total += len(sentence) + (1 if total else 0)
            if kept:
                reply = " ".join(kept)

        # Persistance en arrière-plan.
        async def _background_ai_persist():
            try:
                await asyncio.to_thread(
                    save_ai_message,
                    chat.id,
                    user.id,
                    reply
                )
            except Exception as exc:
                log.debug("AI message persistence skipped: %s", exc)

        try:
            context.application.create_task(
                _background_ai_persist(),
                update=update
            )
        except Exception:
            pass

        # Réactions occasionnelles.
        if is_compliment(user_text) and random.random() < 0.35:
            await react_to_message(
                context.bot,
                chat.id,
                msg.message_id,
                random.choice(["❤️", "🥰", "😍", "🤭", "😊"]),
            )

        # Envoi final : "typing" est arrêté juste avant cette ligne.
        typing_stop.set()
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

        try:
            await safe_reply(msg, reply)
        except Exception as exc:
            log.exception("Alicia final reply failed: %s", exc)
            try:
                await safe_send_message(
                    context.bot,
                    chat.id,
                    reply,
                    reply_to_message_id=msg.message_id,
                )
            except Exception:
                log.exception("Alicia fallback send failed")

        if is_group(chat) and random.random() < 0.04:
            low_reply = user_text.lower()
            if any(x in low_reply for x in ("mdr", "drôle", "haha", "lol")):
                await maybe_sticker(context.bot, chat.id, "funny")
            elif any(x in low_reply for x in ("triste", "pleure", "😭")):
                await maybe_sticker(context.bot, chat.id, "sad")

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception("Alicia text handler failed: %s", exc)
        typing_stop.set()
        typing_task.cancel()
        try:
            await typing_task
        except Exception:
            pass
        try:
            await safe_reply(msg, "Hmm, attends une seconde.")
        except Exception:
            pass
    finally:
        # Sécurité : le typing ne doit jamais rester actif après la réponse.
        typing_stop.set()
        if not typing_task.done():
            typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

async def quizrank_cmd(update, context):
    chat = update.effective_chat
    if not chat or not is_group(chat):
        await safe_reply(update.effective_message, "Le classement Alicia Quiz se consulte dans un groupe.")
        return
    await send_anime_quiz_leaderboard(context.bot, chat.id, "today")

async def quiznow(update, context):
    if not admin_ok(update):
        await safe_reply(update.effective_message, "Commande réservée à l'administration.")
        return
    await safe_reply(update.effective_message, "🎌 Je lance Alicia Quiz dans les groupes activés…")
    await anime_quiz_broadcast_once(context.application)

# ============================================================
# MENU COMMANDS
# ============================================================
PUBLIC_COMMANDS = [
    ("start","Démarrer Alicia"),("quiz","Quiz automatique"),("help","Commandes"),
    ("faq","FAQ"),("about","À propos"),("profile","Mon profil"),("id","Mon ID"),
    ("reset","Voir ma mémoire"),("clear","Voir ma mémoire"),("mood","Humeur d'Alicia"),
    ("ask","Poser une question"),("games","Jeux"),("challenge","Défier un joueur"),
    ("accept","Accepter un défi"),("score","Mon score"),("ranking","Classement"),
    ("alltime","Meilleurs joueurs"),("referral","Mon parrainage"),
    ("download","Télécharger une vidéo"),("video","Télécharger vidéo"),("song","Télécharger chanson"),
    ("guess","Deviner un nombre"),("joke","Blague"),("quote","Citation"),
    ("coin","Pile ou face"),("8ball","Boule magique"),("choose","Choisir"),
    ("compliment","Compliment"),("roast","Taquiner"),("motivate","Motivation"),
    ("groupinfo","Infos du groupe"),("groupstats","Stats du groupe"),
    ("dashboard","Tableau de bord"),("badges","Mes badges"),("friends","Entourage d'Alicia"),
    ("tournament","Tournoi"),("remind","Rappel"),("weather","Météo"),("time","Heure"),
    ("convert","Conversion"),("search","Recherche web"),("translate","Traduction"),
    ("top","Top du groupe"),("voice","Vocal Alicia"),
    ("rss","Flux RSS"),("move","Coup d'échecs")
]

ADMIN_ONLY_COMMANDS = [
    ("quiznow","Lancer un quiz"),("admin","Administration"),("stats","Statistiques"),
    ("chat","Chats privés"),("groups","Groupes"),
    ("user","Utilisateur"),("broadcast","Message utilisateurs"),
    ("broadcastgroups","Message groupes"),("broadcastmedia","Photo/message utilisateurs"),
    ("broadcastgroupsmedia","Photo/message groupes"),("rewardlevels","Niveaux récompenses"),
    ("rewarduser","Récompense utilisateur"),("rewarddone","Récompense envoyée"),
    ("addsticker","Ajouter autocollant"),("addautocollants","Ajouter un autocollant"),
    ("stickers","Liste autocollants"),("delstickers","Supprimer autocollant"),
    ("friendadd","Ajouter un ami au personnage")
]

def _unique_commands(commands):
    seen=set()
    out=[]
    for name, desc in commands:
        name=str(name).strip().lower().lstrip("/")
        if not name or name in seen:
            continue
        seen.add(name)
        # Telegram accepte les descriptions courtes ; on évite les doublons/longues descriptions.
        out.append((name, str(desc).strip()[:256]))
    return out

PUBLIC_COMMANDS = _unique_commands(PUBLIC_COMMANDS)
ADMIN_COMMANDS = _unique_commands(PUBLIC_COMMANDS + ADMIN_ONLY_COMMANDS)


async def _set_commands_call(bot, commands, scope):
    for attempt in range(5):
        try:
            await bot.set_my_commands(commands, scope=scope); return
        except RetryAfter as e:
            await asyncio.sleep(max(2,int(getattr(e,"retry_after",1))+2))
    raise RuntimeError("Telegram limite les appels set_my_commands après plusieurs tentatives.")

async def set_commands(app):
    # Telegram refuse les doublons de commande dans setMyCommands.
    public = [BotCommand(a,b) for a,b in _unique_commands(PUBLIC_COMMANDS)]
    admins = [BotCommand(a,b) for a,b in _unique_commands(ADMIN_COMMANDS)]

    await _set_commands_call(app.bot, public, BotCommandScopeAllPrivateChats())
    await _set_commands_call(app.bot, public, BotCommandScopeAllGroupChats())

    # Les administrateurs de tous les groupes voient aussi les commandes d'administration.
    try:
        await _set_commands_call(
            app.bot, admins, BotCommandScopeAllChatAdministrators()
        )
    except Exception as e:
        log.warning("Group administrator command menu failed: %s", e)

    if ADMIN_USER_ID:
        try:
            await _set_commands_call(
                app.bot, admins, BotCommandScopeChat(chat_id=ADMIN_USER_ID)
            )
        except Exception as e:
            log.warning("Admin command menu failed: %s", e)

# ============================================================
# BUILD APP
# ============================================================
async def post_init(app):
    global QUIZ_TASK, RSS_TASK
    init_db()
    try:
        repaired = _repair_anime_quiz_schedules()
        if repaired:
            log.info("Anime quiz schedules repaired: %s group(s)", repaired)
    except Exception:
        log.exception("Unable to repair anime quiz schedules at startup")
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
    app = (Application.builder()
           .token(TOKEN)
           .concurrent_updates(32)
           .post_init(post_init)
           .build())

    # Public
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("quiz", quiz_cmd))
    app.add_handler(CommandHandler("quizrank", quizrank_cmd))
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
    app.add_handler(CommandHandler("move", chess_move_cmd))
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

    # Alicia Community / Games 2.0 — handlers additifs
    app.add_handler(CommandHandler("community", community_cmd))
    app.add_handler(CommandHandler("profile", community_profile_cmd))
    app.add_handler(CommandHandler("rep", community_profile_cmd))
    app.add_handler(CommandHandler("coins", community_profile_cmd))
    app.add_handler(CommandHandler("shop", community_shop_cmd))
    app.add_handler(CommandHandler("missions", community_missions_cmd))
    app.add_handler(CommandHandler("duel", community_duel_cmd))
    app.add_handler(CommandHandler("team", community_team_cmd))
    app.add_handler(CommandHandler("auto", community_auto_cmd))
    app.add_handler(CommandHandler("automod", community_mod_cmd))
    app.add_handler(CommandHandler("channel", community_channel_cmd))
    app.add_handler(CommandHandler("event", community_event_cmd))
    app.add_handler(CommandHandler("roles", community_roles_cmd))
    app.add_handler(CommandHandler("schedule", community_schedule_cmd))
    app.add_handler(CommandHandler("scheduled", community_schedule_list_cmd))
    app.add_handler(CallbackQueryHandler(community_callback, pattern=r"^community:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, community_message_tracker, block=False), group=-2)


    # Admin
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("chat", chat_cmd))
    app.add_handler(CommandHandler("discussions", discussions_cmd))
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
    app.add_handler(CallbackQueryHandler(anime_quiz_callback, pattern=r"^animequiz:"))
    app.add_handler(CallbackQueryHandler(game_callback, pattern=r"^(menu:|game:)"))
    app.add_handler(ChatMemberHandler(member_update, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.Sticker.ALL, sticker_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, anime_quiz_message_handler, block=True), group=-1)
    # Alicia peut faire des appels IA/réseau : ne bloque pas les nouvelles commandes/messages.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler, block=False), group=1)
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