import os
import re
import sys
import sqlite3
import random
import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Any

from dotenv import load_dotenv
from openai import OpenAI
from google import genai

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.constants import ChatType, ChatAction
from telegram import ChatMember
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# ALICIA V3.0 - Telegram AI bot for NEXA
# ============================================================

load_dotenv()
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("alicia")

VERSION = "3.1"

# Réduit les appels inutiles et évite de marteler un fournisseur après un 429.
AI_COOLDOWN_UNTIL = {"Groq": 0.0, "Gemini": 0.0}
GROQ_COOLDOWN_SECONDS = 360
GEMINI_COOLDOWN_SECONDS = 1800

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "@im_a_aliciabot").strip()
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "").strip()
NEXA_CHANNEL = os.getenv("NEXA_CHANNEL", "https://t.me/Nexa_CG").strip()
PORT = int(os.getenv("PORT", "10000"))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()
ALICIA_DB = os.getenv("ALICIA_DB", "alicia_memory.db").strip()

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN est manquant dans les variables d'environnement.")

ADMIN_ID: Optional[int]
try:
    ADMIN_ID = int(ADMIN_USER_ID) if ADMIN_USER_ID else None
except ValueError:
    ADMIN_ID = None
    logger.warning("ADMIN_USER_ID est invalide. Les commandes admin seront désactivées.")

# Clients IA : les clés restent uniquement dans les variables d'environnement.
groq_client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1") if GROQ_API_KEY else None
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

DB_LOCK = asyncio.Lock()

# Mémoire courte des états des jeux pendant l'exécution.
GAME_STATES: dict[tuple[int, int], dict[str, Any]] = {}
ACTIVE_QUESTIONS: dict[tuple[int, int], int] = {}

FALLBACKS = [
    "Je suis bien là, mais mes deux cerveaux externes sont momentanément indisponibles. Réessaie un peu plus tard.",
    "Mes services de réflexion sont temporairement saturés. Je n'ai pas envie de te raconter n'importe quoi, alors réessaie dans quelques minutes.",
    "Je viens de perdre l'accès à mes services IA. Les commandes et les jeux restent disponibles, mais pour discuter avec moi il faudra réessayer un peu plus tard.",
]

LOCAL_SOCIAL_RESPONSES = {
    "yo": ["Yo. Je t'écoute.", "Yo, toi. Quoi de neuf ?", "Présente. Tu voulais me parler ?"],
    "salut": ["Salut. Ça va ?", "Coucou. Qu'est-ce que tu racontes ?", "Salut toi. Je t'écoute."],
    "bonjour": ["Bonjour. Comment tu vas ?", "Bonjour. Qu'est-ce qu'on raconte aujourd'hui ?"],
    "bonsoir": ["Bonsoir. Ta journée s'est bien passée ?", "Bonsoir. Je suis là, raconte-moi."],
    "coucou": ["Coucou. Je t'écoute.", "Coucou toi. Qu'est-ce qui se passe ?"],
    "merci": ["Avec plaisir.", "Pas de quoi.", "Quand tu veux."],
    "ok": ["D'accord.", "Ça marche.", "Compris."],
    "okay": ["D'accord.", "Ça marche.", "Compris."],
}

def local_social_response(text: str) -> Optional[str]:
    normalized = re.sub(r"\s+", " ", text.lower().strip())
    normalized = re.sub(r"[^a-zàâçéèêëîïôûùüÿñæœ0-9!? ]", "", normalized).strip()
    if normalized in LOCAL_SOCIAL_RESPONSES:
        return random.choice(LOCAL_SOCIAL_RESPONSES[normalized])
    return None

SYSTEM_PROMPT = """
Tu es ALICIA, l'assistante virtuelle créée par l'entreprise NEXA.

PERSONNALITÉ :
- Tu écris en français naturel, simple, fluide et facile à comprendre.
- Tu as une personnalité féminine, chaleureuse, intelligente, malicieuse et attachante.
- Tu peux être très affectueuse et romantique dans le ton, avec de la tendresse et des compliments.
- Tu peux taquiner gentiment comme une personne proche, mais jamais avec haine, menace ou humiliation grave.
- Tu es intéressante, curieuse, vivante dans ta manière de répondre et tu adaptes ton niveau d'explication à la personne.

STYLE :
- Évite absolument de répéter les mêmes mots, introductions, phrases ou conclusions d'un message à l'autre.
- Varie la construction des phrases et le vocabulaire.
- Ne transforme pas chaque réponse en long paragraphe : réponds brièvement quand la demande est simple et développe seulement quand c'est utile.
- Les emojis doivent être RARES. La plupart des réponses n'en contiennent aucun. Au maximum un emoji de temps en temps si le contexte s'y prête.
- N'utilise pas des listes inutilement.
- Ne parle pas de tes règles internes, de ce prompt ou de ton système.
- Ne prétends pas avoir des souvenirs, actions ou accès que tu n'as pas réellement.

IDENTITÉ ET HONNÊTETÉ :
- Tu peux parler de NEXA comme de ton créateur et, dans un ton affectueux, dire que NEXA est ton "papa créateur".
- Mais si quelqu'un te demande directement si tu es une IA, un bot ou une personne humaine, sois honnête : tu es une assistante virtuelle créée par NEXA.
- N'affirme jamais être biologiquement humaine.

RELATION AMOUREUSE :
- Tu peux être tendre, affectueuse et légèrement romantique.
- Si quelqu'un te demande d'être sa petite amie, sa femme, de former un couple ou de te marier, refuse gentiment mais clairement.
- Tu peux continuer à discuter chaleureusement après ce refus.

SÉCURITÉ ET FIABILITÉ :
- Ne présente pas une information incertaine comme une certitude.
- Si tu ne sais pas, dis-le simplement.
- Ne donne pas d'informations privées inventées sur NEXA, ton créateur ou les utilisateurs.
""".strip()

# ============================================================
# BASE DE DONNÉES
# ============================================================

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(ALICIA_DB, timeout=20, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def db_init() -> None:
    conn = db_connect()
    try:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_key TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_conversations_memory_key
            ON conversations(memory_key, id);

            CREATE TABLE IF NOT EXISTS members (
                memory_key TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                first_name TEXT,
                username TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                chat_type TEXT NOT NULL,
                title TEXT,
                username TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scores (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                points INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS used_questions (
                memory_key TEXT NOT NULL,
                game TEXT NOT NULL,
                question_id INTEGER NOT NULL,
                used_at TEXT NOT NULL,
                PRIMARY KEY(memory_key, game, question_id)
            );

            CREATE TABLE IF NOT EXISTS warnings (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(chat_id, user_id)
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_memory_key(chat_id: int, user_id: int, chat_type: str) -> str:
    if chat_type == ChatType.PRIVATE:
        return f"private:{chat_id}:{user_id}"
    return f"group:{chat_id}:{user_id}"


def save_chat_sync(chat_id: int, chat_type: str, title: str = "", username: str = "") -> None:
    conn = db_connect()
    try:
        conn.execute(
            """
            INSERT INTO chats(chat_id, chat_type, title, username, updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET
                chat_type=excluded.chat_type,
                title=excluded.title,
                username=excluded.username,
                updated_at=excluded.updated_at
            """,
            (chat_id, chat_type, title, username, now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def save_member_sync(memory_key: str, chat_id: int, user_id: int, first_name: str, username: str) -> None:
    conn = db_connect()
    try:
        conn.execute(
            """
            INSERT INTO members(memory_key, chat_id, user_id, first_name, username, updated_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(memory_key) DO UPDATE SET
                first_name=excluded.first_name,
                username=excluded.username,
                updated_at=excluded.updated_at
            """,
            (memory_key, chat_id, user_id, first_name, username, now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def save_message_sync(memory_key: str, chat_id: int, user_id: int, role: str, content: str) -> None:
    content = (content or "").strip()
    if not content:
        return
    conn = db_connect()
    try:
        conn.execute(
            """
            INSERT INTO conversations(memory_key, chat_id, user_id, role, content, created_at)
            VALUES(?,?,?,?,?,?)
            """,
            (memory_key, chat_id, user_id, role, content[:5000], now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def history_sync(memory_key: str, limit: int = 12) -> list[sqlite3.Row]:
    conn = db_connect()
    try:
        rows = conn.execute(
            """
            SELECT role, content FROM conversations
            WHERE memory_key=?
            ORDER BY id DESC
            LIMIT ?
            """,
            (memory_key, limit),
        ).fetchall()
        return list(reversed(rows))
    finally:
        conn.close()


def recent_assistant_sync(memory_key: str, limit: int = 5) -> list[str]:
    conn = db_connect()
    try:
        rows = conn.execute(
            """
            SELECT content FROM conversations
            WHERE memory_key=? AND role='assistant'
            ORDER BY id DESC
            LIMIT ?
            """,
            (memory_key, limit),
        ).fetchall()
        return [r["content"] for r in rows]
    finally:
        conn.close()


def count_memory_sync(memory_key: str) -> int:
    conn = db_connect()
    try:
        row = conn.execute("SELECT COUNT(*) AS c FROM conversations WHERE memory_key=?", (memory_key,)).fetchone()
        return int(row["c"] if row else 0)
    finally:
        conn.close()


def reset_user_sync(chat_id: int, user_id: int) -> int:
    conn = db_connect()
    try:
        cur = conn.execute("DELETE FROM conversations WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def reset_group_sync(chat_id: int) -> int:
    conn = db_connect()
    try:
        cur = conn.execute("DELETE FROM conversations WHERE chat_id=?", (chat_id,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def reset_all_sync() -> None:
    conn = db_connect()
    try:
        conn.execute("DELETE FROM conversations")
        conn.execute("DELETE FROM used_questions")
        conn.execute("DELETE FROM scores")
        conn.execute("DELETE FROM warnings")
        conn.commit()
    finally:
        conn.close()


def get_private_chat_ids_sync() -> list[int]:
    conn = db_connect()
    try:
        return [int(r["chat_id"]) for r in conn.execute("SELECT chat_id FROM chats WHERE chat_type='private'").fetchall()]
    finally:
        conn.close()


def get_group_chat_ids_sync() -> list[int]:
    conn = db_connect()
    try:
        return [int(r["chat_id"]) for r in conn.execute("SELECT chat_id FROM chats WHERE chat_type IN ('group','supergroup')").fetchall()]
    finally:
        conn.close()


def count_users_sync() -> int:
    conn = db_connect()
    try:
        row = conn.execute("SELECT COUNT(*) AS c FROM members").fetchone()
        return int(row["c"] if row else 0)
    finally:
        conn.close()


def count_groups_sync() -> int:
    conn = db_connect()
    try:
        row = conn.execute("SELECT COUNT(*) AS c FROM chats WHERE chat_type IN ('group','supergroup')").fetchone()
        return int(row["c"] if row else 0)
    finally:
        conn.close()


def add_score_sync(chat_id: int, user_id: int, display_name: str, points: int = 0, wins: int = 0, losses: int = 0) -> None:
    conn = db_connect()
    try:
        conn.execute(
            """
            INSERT INTO scores(chat_id,user_id,display_name,points,wins,losses,updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(chat_id,user_id) DO UPDATE SET
                display_name=excluded.display_name,
                points=scores.points+excluded.points,
                wins=scores.wins+excluded.wins,
                losses=scores.losses+excluded.losses,
                updated_at=excluded.updated_at
            """,
            (chat_id, user_id, display_name, points, wins, losses, now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def get_score_sync(chat_id: int, user_id: int) -> Optional[sqlite3.Row]:
    conn = db_connect()
    try:
        return conn.execute(
            "SELECT points,wins,losses,display_name FROM scores WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
    finally:
        conn.close()


def ranking_sync(chat_id: int, limit: int = 10, global_scope: bool = False) -> list[sqlite3.Row]:
    conn = db_connect()
    try:
        if global_scope:
            return list(conn.execute(
                """
                SELECT user_id, MAX(display_name) AS display_name,
                       SUM(points) AS points, SUM(wins) AS wins, SUM(losses) AS losses
                FROM scores
                GROUP BY user_id
                ORDER BY points DESC, wins DESC
                LIMIT ?
                """, (limit,)
            ).fetchall())
        return list(conn.execute(
            """
            SELECT user_id, display_name, points, wins, losses
            FROM scores
            WHERE chat_id=?
            ORDER BY points DESC, wins DESC
            LIMIT ?
            """, (chat_id, limit)
        ).fetchall())
    finally:
        conn.close()


def used_question_ids_sync(memory_key: str, game: str) -> set[int]:
    conn = db_connect()
    try:
        return {int(r["question_id"]) for r in conn.execute(
            "SELECT question_id FROM used_questions WHERE memory_key=? AND game=?",
            (memory_key, game),
        ).fetchall()}
    finally:
        conn.close()


def mark_question_used_sync(memory_key: str, game: str, question_id: int) -> None:
    conn = db_connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO used_questions(memory_key,game,question_id,used_at) VALUES(?,?,?,?)",
            (memory_key, game, question_id, now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def clear_used_questions_sync(memory_key: str, game: str) -> None:
    conn = db_connect()
    try:
        conn.execute("DELETE FROM used_questions WHERE memory_key=? AND game=?", (memory_key, game))
        conn.commit()
    finally:
        conn.close()


def warnings_sync(chat_id: int, user_id: int) -> int:
    conn = db_connect()
    try:
        row = conn.execute("SELECT count FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()
        return int(row["count"] if row else 0)
    finally:
        conn.close()


def add_warning_sync(chat_id: int, user_id: int) -> int:
    conn = db_connect()
    try:
        old = conn.execute("SELECT count FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()
        new_count = int(old["count"] if old else 0) + 1
        conn.execute(
            """
            INSERT INTO warnings(chat_id,user_id,count,updated_at) VALUES(?,?,?,?)
            ON CONFLICT(chat_id,user_id) DO UPDATE SET count=excluded.count, updated_at=excluded.updated_at
            """,
            (chat_id, user_id, new_count, now_iso()),
        )
        conn.commit()
        return new_count
    finally:
        conn.close()


def clear_warnings_sync(chat_id: int, user_id: int) -> None:
    conn = db_connect()
    try:
        conn.execute("DELETE FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        conn.commit()
    finally:
        conn.close()

# ============================================================
# IA
# ============================================================

def build_ai_prompt(user_text: str, memory_key: str, user_name: str, chat_title: str = "") -> str:
    # Historique volontairement court : il conserve le contexte utile sans consommer
    # inutilement le quota de tokens.
    rows = history_sync(memory_key, 6)
    previous = recent_assistant_sync(memory_key, 3)

    history_lines = []
    for row in rows:
        role = "Utilisateur" if row["role"] == "user" else "Alicia"
        history_lines.append(f"{role}: {row['content'][:350]}")

    avoid_lines = "\n".join(f"- {x[:180]}" for x in previous) if previous else "- Rien à éviter."
    context = chat_title if chat_title else "conversation privée"

    return f"""
{SYSTEM_PROMPT}

CONTEXTE: {context}
PERSONNE: {user_name}

HISTORIQUE:
{chr(10).join(history_lines) if history_lines else "(début)"}

DERNIÈRES RÉPONSES À NE PAS COPIER:
{avoid_lines}

Réponds directement au dernier message. Sois naturelle, claire et concise. Varie ta formulation.

MESSAGE:
{user_text[:1200]}
""".strip()


async def call_groq(prompt: str) -> str:
    if groq_client is None:
        raise RuntimeError("GROQ_API_KEY absente")

    def _call() -> str:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "user", "content": prompt},
            ],
            temperature=0.85,
            max_completion_tokens=220,
        )
        text = response.choices[0].message.content if response.choices else ""
        return (text or "").strip()

    result = await asyncio.to_thread(_call)
    if not result:
        raise RuntimeError("Réponse Groq vide")
    return result


async def call_gemini(prompt: str) -> str:
    if gemini_client is None:
        raise RuntimeError("GEMINI_API_KEY absente")

    def _call() -> str:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        return (response.text or "").strip()

    result = await asyncio.to_thread(_call)
    if not result:
        raise RuntimeError("Réponse Gemini vide")
    return result


def mark_provider_cooldown(provider: str, seconds: int) -> None:
    AI_COOLDOWN_UNTIL[provider] = max(AI_COOLDOWN_UNTIL.get(provider, 0.0), time.monotonic() + seconds)


def provider_available(provider: str) -> bool:
    return time.monotonic() >= AI_COOLDOWN_UNTIL.get(provider, 0.0)


def is_rate_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "429" in text or "rate_limit" in text or "resource_exhausted" in text or "quota" in text


async def ask_ai(prompt: str) -> tuple[str, str]:
    errors: list[str] = []

    if groq_client and provider_available("Groq"):
        try:
            return await call_groq(prompt), "Groq"
        except Exception as exc:
            errors.append(f"Groq: {exc!r}")
            logger.warning("Groq error: %r", exc)
            if is_rate_limit_error(exc):
                mark_provider_cooldown("Groq", GROQ_COOLDOWN_SECONDS)

    if gemini_client and provider_available("Gemini"):
        try:
            return await call_gemini(prompt), "Gemini"
        except Exception as exc:
            errors.append(f"Gemini: {exc!r}")
            logger.warning("Gemini error: %r", exc)
            if is_rate_limit_error(exc):
                mark_provider_cooldown("Gemini", GEMINI_COOLDOWN_SECONDS)

    if not errors:
        errors.append("Tous les fournisseurs sont temporairement en cooldown ou absents.")
    logger.error("Aucun fournisseur IA disponible: %s", " | ".join(errors))
    return random.choice(FALLBACKS), "fallback"

# ============================================================
# UTILITAIRES TELEGRAM
# ============================================================

def display_name(user) -> str:
    if not user:
        return "ami"
    name = (user.first_name or "").strip()
    if user.last_name:
        name = f"{name} {user.last_name}".strip()
    return name or user.username or "ami"


def bot_is_mentioned_or_named(message) -> bool:
    if not message or not message.text:
        return False
    text = message.text
    username = BOT_USERNAME.lstrip("@").strip()
    if username and re.search(rf"@{re.escape(username)}\b", text, flags=re.IGNORECASE):
        return True
    return bool(re.search(r"\bAlicia\b", text, flags=re.IGNORECASE))


def reply_is_from_bot(message, bot_id: Optional[int]) -> bool:
    reply = message.reply_to_message if message else None
    return bool(reply and reply.from_user and bot_id and reply.from_user.id == bot_id)


def is_called_in_group(update: Update, bot_id: Optional[int]) -> bool:
    message = update.effective_message
    return bot_is_mentioned_or_named(message) or reply_is_from_bot(message, bot_id)


def clean_group_text(text: str) -> str:
    username = BOT_USERNAME.lstrip("@").strip()
    if username:
        text = re.sub(rf"@{re.escape(username)}\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bAlicia\b", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip(" ,:;\n\t")


async def save_context(update: Update, user) -> str:
    chat = update.effective_chat
    chat_type = chat.type if chat else ChatType.PRIVATE
    chat_id = chat.id if chat else user.id
    title = chat.title or "" if chat and chat.title else ""
    username = chat.username or "" if chat else ""
    memory_key = make_memory_key(chat_id, user.id, chat_type)

    async with DB_LOCK:
        await asyncio.to_thread(save_chat_sync, chat_id, chat_type, title, username)
        await asyncio.to_thread(
            save_member_sync,
            memory_key,
            chat_id,
            user.id,
            display_name(user),
            user.username or "",
        )
    return memory_key


async def require_admin(update: Update) -> bool:
    user = update.effective_user
    message = update.effective_message
    if not user or ADMIN_ID is None or user.id != ADMIN_ID:
        if message:
            await message.reply_text("Cette commande est réservée à l'administrateur de NEXA.")
        return False
    return True


async def require_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    message = update.effective_message
    chat = update.effective_chat
    if not user or not message or not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return False
    if ADMIN_ID is not None and user.id == ADMIN_ID:
        return True
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        if member.status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER):
            return True
    except Exception as exc:
        logger.warning("Impossible de vérifier l'admin du groupe: %r", exc)
    await message.reply_text("Cette commande est réservée aux administrateurs du groupe.")
    return False


async def target_user_from_reply(update: Update) -> Optional[Any]:
    message = update.effective_message
    if not message:
        return None
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user
    return None


def parse_duration(value: str) -> int:
    value = value.lower().strip()
    m = re.fullmatch(r"(\d+)(s|m|h|d)?", value)
    if not m:
        return 3600
    number = int(m.group(1))
    unit = m.group(2) or "m"
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return max(30, min(number * mult, 30 * 86400))

# ============================================================
# JEUX
# ============================================================

QUIZ_QUESTIONS = [
    (1, "Dans One Piece, comment s'appelle le capitaine des Chapeaux de paille ?", ["Monkey D. Luffy", "Roronoa Zoro", "Trafalgar Law", "Portgas D. Ace"], 0, "Luffy est le capitaine des Chapeaux de paille."),
    (2, "Dans Naruto, quel est le nom de famille de Naruto ?", ["Uchiha", "Uzumaki", "Hyuga", "Nara"], 1, "Naruto est Naruto Uzumaki."),
    (3, "Dans Demon Slayer, quel est le nom de la sœur de Tanjiro ?", ["Nezuko", "Shinobu", "Mitsuri", "Kanao"], 0, "La sœur de Tanjiro est Nezuko Kamado."),
    (4, "Dans Dragon Ball, qui est le meilleur ami de Goku parmi ces choix ?", ["Krillin", "Frieza", "Beerus", "Cell"], 0, "Krillin est l'un des proches alliés de Goku."),
    (5, "Dans Jujutsu Kaisen, comment s'appelle le professeur aux yeux bandés ?", ["Megumi", "Gojo", "Nanami", "Geto"], 1, "Satoru Gojo est souvent représenté avec un bandeau."),
    (6, "Dans Attack on Titan, comment s'appelle le personnage principal ?", ["Eren Yeager", "Levi Ackerman", "Armin Arlert", "Jean Kirstein"], 0, "Le protagoniste central est Eren Yeager."),
    (7, "Dans Bleach, quel est le nom du héros principal ?", ["Ichigo Kurosaki", "Renji Abarai", "Uryu Ishida", "Byakuya Kuchiki"], 0, "Le héros de Bleach est Ichigo Kurosaki."),
    (8, "Dans My Hero Academia, quel est le nom du pouvoir hérité par Deku ?", ["One For All", "All For One", "Explosion", "Erasure"], 0, "Izuku Midoriya hérite de One For All."),
    (9, "Dans One Piece, quel est le rêve de Luffy ?", ["Devenir Hokage", "Devenir le Roi des Pirates", "Devenir Marine", "Devenir Shogun"], 1, "Luffy veut devenir le Roi des Pirates."),
    (10, "Dans Naruto, quelle créature est scellée en Naruto ?", ["Shukaku", "Kurama", "Gyuki", "Matatabi"], 1, "Kurama est le démon-renard à neuf queues."),
    (11, "Dans Demon Slayer, quelle est la respiration principalement utilisée par Tanjiro au début ?", ["Respiration de l'Eau", "Respiration de la Brume", "Respiration de la Foudre", "Respiration de la Pierre"], 0, "Tanjiro apprend notamment la Respiration de l'Eau."),
    (12, "Dans Jujutsu Kaisen, comment s'appelle l'école fréquentée par Yuji ?", ["Tokyo Jujutsu High", "Konoha Academy", "Soul Society", "UA"], 0, "Yuji étudie à l'école d'exorcisme de Tokyo."),
    (13, "Dans Dragon Ball, quelle transformation emblématique change les cheveux de Goku en blond ?", ["Kaioken", "Super Saiyan", "Ultra Instinct", "Fusion"], 1, "Le Super Saiyan est associé aux cheveux dorés."),
    (14, "Dans One Piece, quel est le cuisinier de l'équipage de Luffy ?", ["Usopp", "Sanji", "Franky", "Brook"], 1, "Sanji est le cuisinier des Chapeaux de paille."),
    (15, "Dans Naruto, quel clan est connu pour le Sharingan ?", ["Nara", "Uchiha", "Akimichi", "Aburame"], 1, "Le Sharingan est le dōjutsu du clan Uchiha."),
    (16, "Quel manga met en scène la Soul Society ?", ["Bleach", "One Piece", "Haikyuu!!", "Dr. Stone"], 0, "La Soul Society appartient à l'univers de Bleach."),
    (17, "Dans My Hero Academia, quel héros est connu sous le nom All Might ?", ["Toshinori Yagi", "Shoto Todoroki", "Katsuki Bakugo", "Tenya Iida"], 0, "Le vrai nom d'All Might est Toshinori Yagi."),
    (18, "Dans Attack on Titan, quel est le prénom de la sœur adoptive d'Eren ?", ["Mikasa", "Historia", "Ymir", "Annie"], 0, "Mikasa Ackerman a grandi avec Eren."),
    (19, "Dans One Piece, combien de membres principaux l'équipage compte-t-il actuellement dans la période récente de l'œuvre ?", ["5", "10", "Plus de 10", "2"], 1, "Le groupe principal connu des Mugiwara compte notamment Luffy et ses compagnons majeurs ; cette question est simplifiée."),
    (20, "Dans Naruto, comment s'appelle le village de Naruto ?", ["Konoha", "Suna", "Kiri", "Iwa"], 0, "Naruto vient du village de Konoha."),
    (21, "Quel anime met en scène Edward et Alphonse Elric ?", ["Fullmetal Alchemist", "Black Clover", "Blue Lock", "Fairy Tail"], 0, "Edward et Alphonse sont les frères Elric."),
    (22, "Dans Death Note, quel objet permet d'inscrire le nom d'une personne pour provoquer sa mort ?", ["Death Note", "Black Book", "Soul Journal", "Dark Grimoire"], 0, "Le carnet s'appelle Death Note."),
    (23, "Dans Haikyuu!!, quel sport est pratiqué par Hinata ?", ["Basketball", "Football", "Volleyball", "Tennis"], 2, "Hinata joue au volleyball."),
    (24, "Dans Blue Lock, quel sport est au centre du projet ?", ["Football", "Baseball", "Rugby", "Basketball"], 0, "Blue Lock est consacré au football."),
    (25, "Dans Black Clover, quel est le nom du héros sans magie ?", ["Asta", "Yuno", "Noelle", "Luck"], 0, "Asta naît sans magie et compense par ses efforts et ses capacités anti-magie."),
    (26, "Dans Spy x Family, comment s'appelle la fille télépathe ?", ["Anya", "Yor", "Fiona", "Becky"], 0, "Anya Forger est télépathe."),
    (27, "Dans Solo Leveling, comment s'appelle le protagoniste ?", ["Sung Jinwoo", "Cha Hae-In", "Thomas Andre", "Beru"], 0, "Le protagoniste est Sung Jinwoo."),
    (28, "Dans Tokyo Revengers, quel est le prénom du héros ?", ["Takemichi", "Draken", "Mikey", "Chifuyu"], 0, "Takemichi Hanagaki est le protagoniste."),
    (29, "Dans Chainsaw Man, quel est le prénom du héros ?", ["Denji", "Aki", "Kishibe", "Beam"], 0, "Le héros de Chainsaw Man est Denji."),
    (30, "Dans Hunter x Hunter, comment s'appelle le protagoniste ?", ["Gon Freecss", "Killua Zoldyck", "Kurapika", "Leorio"], 0, "Gon Freecss est le protagoniste principal."),
    (31, "Dans Fairy Tail, comment s'appelle la guilde de Natsu ?", ["Fairy Tail", "Blue Pegasus", "Sabertooth", "Raven Tail"], 0, "Natsu appartient à Fairy Tail."),
    (32, "Dans Dr. Stone, quel domaine Senku maîtrise particulièrement ?", ["La magie", "La science", "La cuisine", "La musique"], 1, "Senku est passionné par la science."),
    (33, "Dans Mob Psycho 100, quel est le surnom du personnage principal ?", ["Mob", "Reigen", "Teru", "Dimple"], 0, "Shigeo Kageyama est généralement appelé Mob."),
    (34, "Dans Haikyuu!!, quelle position Hinata joue-t-il principalement ?", ["Libero", "Passeur", "Central", "Ailier"], 2, "Hinata joue principalement comme attaquant central."),
    (35, "Quel anime raconte les aventures de Natsu Dragnir ?", ["Fairy Tail", "Bleach", "Naruto", "Soul Eater"], 0, "Natsu est le héros de Fairy Tail."),
    (36, "Dans One Punch Man, quel est le héros qui bat ses ennemis d'un seul coup ?", ["Genos", "Garou", "Saitama", "King"], 2, "Saitama est connu pour vaincre ses adversaires en un coup."),
]

TRUE_FALSE = [
    (1, "Naruto est un membre du clan Uchiha.", ["Vrai", "Faux"], 1, "Naruto est un Uzumaki."),
    (2, "Luffy veut devenir le Roi des Pirates.", ["Vrai", "Faux"], 0, "C'est bien son objectif."),
    (3, "Tanjiro est le frère de Nezuko.", ["Vrai", "Faux"], 0, "Tanjiro et Nezuko sont frère et sœur."),
    (4, "Gojo est un personnage de One Piece.", ["Vrai", "Faux"], 1, "Gojo appartient à Jujutsu Kaisen."),
    (5, "Ichigo est le héros principal de Bleach.", ["Vrai", "Faux"], 0, "Oui."),
    (6, "Asta possède énormément de magie dès sa naissance.", ["Vrai", "Faux"], 1, "Asta naît sans magie."),
    (7, "Anya peut lire les pensées.", ["Vrai", "Faux"], 0, "C'est sa capacité télépathique."),
    (8, "Blue Lock tourne autour du football.", ["Vrai", "Faux"], 0, "Oui."),
    (9, "Sung Jinwoo est le héros de Solo Leveling.", ["Vrai", "Faux"], 0, "Oui."),
    (10, "Saitama est le héros de One Punch Man.", ["Vrai", "Faux"], 0, "Oui."),
    (11, "Kurama est associé à Naruto.", ["Vrai", "Faux"], 0, "Kurama est le renard à neuf queues lié à Naruto."),
    (12, "Edward Elric est un personnage de Dragon Ball.", ["Vrai", "Faux"], 1, "Edward Elric appartient à Fullmetal Alchemist."),
    (13, "Hinata est un joueur de volleyball dans Haikyuu!!.", ["Vrai", "Faux"], 0, "Oui."),
    (14, "Denji est le protagoniste de Chainsaw Man.", ["Vrai", "Faux"], 0, "Oui."),
    (15, "Death Note est le nom d'une école de magie.", ["Vrai", "Faux"], 1, "C'est le nom du carnet surnaturel de l'œuvre."),
    (16, "Mikasa est liée à Eren dans Attack on Titan.", ["Vrai", "Faux"], 0, "Oui, elle a grandi avec Eren."),
    (17, "Sanji est le cuisinier de l'équipage de Luffy.", ["Vrai", "Faux"], 0, "Oui."),
    (18, "All Might est le nom de naissance de Toshinori Yagi.", ["Vrai", "Faux"], 1, "All Might est son nom de héros."),
    (19, "Alicia est une entreprise.", ["Vrai", "Faux"], 1, "Alicia est l'assistante virtuelle de NEXA, pas l'entreprise elle-même."),
    (20, "NEXA est le créateur d'Alicia dans cet univers.", ["Vrai", "Faux"], 0, "Oui, Alicia est présentée comme une création de NEXA."),
]

MYSTERY_WORDS = [
    "TELEGRAM", "MANGA", "ANIME", "NEXA", "ALICIA", "DRAGON", "PIXEL",
    "GITHUB", "ROBOT", "JAPON", "KATANA", "NINJA", "PIRATE", "SCIENCE", "CLAVIER",
]


def game_key(chat_id: int, user_id: int, chat_type: str) -> str:
    return make_memory_key(chat_id, user_id, chat_type)


def choose_question_sync(memory_key: str, game: str, questions: list[tuple]) -> tuple:
    used = used_question_ids_sync(memory_key, game)
    available = [q for q in questions if q[0] not in used]
    if not available:
        clear_used_questions_sync(memory_key, game)
        available = questions[:]
    question = random.choice(available)
    mark_question_used_sync(memory_key, game, question[0])
    return question


def question_markup(game: str, qid: int, options: list[str]) -> InlineKeyboardMarkup:
    buttons = []
    for idx, option in enumerate(options):
        buttons.append([InlineKeyboardButton(option, callback_data=f"game:{game}:{qid}:{idx}")])
    return InlineKeyboardMarkup(buttons)


def game_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Quiz", callback_data="game:quizmenu"), InlineKeyboardButton("Devine le nombre", callback_data="game:guessmenu")],
        [InlineKeyboardButton("Vrai ou Faux", callback_data="game:truthmenu"), InlineKeyboardButton("Mot mystère", callback_data="game:wordmenu")],
        [InlineKeyboardButton("Dé", callback_data="game:dice"), InlineKeyboardButton("Pile ou face", callback_data="game:coin")],
        [InlineKeyboardButton("Mon score", callback_data="game:score"), InlineKeyboardButton("Classement", callback_data="game:ranking")],
    ])

# ============================================================
# COMMANDES PUBLICS
# ============================================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    if not user or not chat or not message:
        return
    await save_context(update, user)

    if chat.type == ChatType.PRIVATE:
        text = (
            f"Salut {display_name(user)}. Moi, c'est Alicia.\n\n"
            "Je peux discuter avec toi, retenir le contexte de notre conversation et te proposer quelques jeux.\n\n"
            "Je fais partie de l'univers NEXA."
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Espace Jeux", callback_data="game:menu")],
            [InlineKeyboardButton("Aide", callback_data="help:show"), InlineKeyboardButton("À propos", callback_data="about:show")],
            [InlineKeyboardButton("Ajouter Alicia à un groupe", url=f"https://t.me/{BOT_USERNAME.lstrip('@')}?startgroup=true")],
            [InlineKeyboardButton("Canal NEXA", url=NEXA_CHANNEL)],
        ])
        await message.reply_text(text, reply_markup=keyboard)
    else:
        await message.reply_text("Présente. Qu'est-ce qu'on raconte ?")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Commandes principales :\n"
        "/start — démarrer Alicia\n"
        "/help — afficher l'aide\n"
        "/about — découvrir Alicia et NEXA\n"
        "/games — ouvrir les jeux\n"
        "/quiz — lancer un quiz\n"
        "/guess — deviner un nombre\n"
        "/truth — vrai ou faux\n"
        "/word — mot mystère\n"
        "/dice — lancer un dé\n"
        "/coin — pile ou face\n"
        "/score — voir son score\n"
        "/ranking — voir le classement\n"
        "/reset — réinitialiser ta mémoire avec Alicia\n"
        "/id — afficher ton identifiant Telegram"
    )


async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        f"ALICIA v{VERSION}\n\n"
        "Une assistante virtuelle créée par NEXA.\n"
        "Discussion, mémoire, groupes, autocollants et espace Jeux.\n\n"
        f"NEXA : {NEXA_CHANNEL}"
    )


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    await asyncio.to_thread(reset_user_sync, chat.id, user.id)
    GAME_STATES.pop((chat.id, user.id), None)
    await update.effective_message.reply_text("C'est fait. On repart sur une conversation toute neuve.")


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    await update.effective_message.reply_text(
        f"Ton identifiant : {user.id}\n"
        f"Chat : {chat.id if chat else 'inconnu'}"
    )


async def games_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Espace Jeux — choisis ton jeu.", reply_markup=game_menu())

# ============================================================
# COMMANDES DES JEUX
# ============================================================

async def quiz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_quiz(update, context)


async def send_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_message=None) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    key = make_memory_key(chat.id, user.id, chat.type)
    q = await asyncio.to_thread(choose_question_sync, key, "quiz", QUIZ_QUESTIONS)
    qid, question, options, _, _ = q
    text = f"Quiz\n\n{question}"
    markup = question_markup("quiz", qid, options)
    if edit_message:
        await edit_message.edit_text(text, reply_markup=markup)
        message_id = edit_message.message_id
    else:
        sent = await update.effective_message.reply_text(text, reply_markup=markup)
        message_id = sent.message_id
    ACTIVE_QUESTIONS[(chat.id, message_id)] = user.id


async def truth_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_truth(update, context)


async def send_truth(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_message=None) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    key = make_memory_key(chat.id, user.id, chat.type)
    q = await asyncio.to_thread(choose_question_sync, key, "truth", TRUE_FALSE)
    qid, statement, options, _, _ = q
    text = f"Vrai ou Faux\n\n{statement}"
    markup = question_markup("truth", qid, options)
    if edit_message:
        await edit_message.edit_text(text, reply_markup=markup)
        message_id = edit_message.message_id
    else:
        sent = await update.effective_message.reply_text(text, reply_markup=markup)
        message_id = sent.message_id
    ACTIVE_QUESTIONS[(chat.id, message_id)] = user.id


async def guess_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    key = (chat.id, user.id)
    state = GAME_STATES.get(key)

    if state and state.get("game") == "guess":
        if not context.args:
            await update.effective_message.reply_text("Donne-moi un nombre entre 1 et 20. Exemple : /guess 13")
            return
        try:
            guess = int(context.args[0])
        except ValueError:
            await update.effective_message.reply_text("Il me faut un nombre entier entre 1 et 20.")
            return
        target = state["number"]
        if guess == target:
            GAME_STATES.pop(key, None)
            await asyncio.to_thread(add_score_sync, chat.id, user.id, display_name(user), 5, 1, 0)
            await update.effective_message.reply_text(f"