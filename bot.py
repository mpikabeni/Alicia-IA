import os
import re
import json
import time
import random
import sqlite3
import asyncio
import logging
from datetime import datetime, timezone

from dotenv import load_dotenv
from openai import OpenAI
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.error import RetryAfter
from telegram.constants import ChatType
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ChatMemberHandler, filters,
)

load_dotenv()

# =========================
# CONFIG
# =========================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "").strip()
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

MISTRAL_MODEL = os.getenv("MISTRAL_MODEL", "ministral-3-8b-latest").strip()
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()

BOT_USERNAME = os.getenv("BOT_USERNAME", "@im_a_aliciabot").strip()
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0") or 0)
NEXA_CHANNEL = os.getenv("NEXA_CHANNEL", "https://t.me/Nexa_CG").strip()
PORT = int(os.getenv("PORT", "10000"))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()
DB_PATH = os.getenv("ALICIA_DB", "alicia_v3.db").strip()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | alicia | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("alicia")


# =========================
# TELEGRAM SAFE SENDING / ANTI-FLOOD
# =========================
_SEND_LOCK = asyncio.Lock()
_LAST_SEND_AT = 0.0

async def safe_send_message(bot, chat_id, text, **kwargs):
    """Envoi Telegram avec anti-flood global et retry automatique."""
    global _LAST_SEND_AT
    for attempt in range(5):
        try:
            async with _SEND_LOCK:
                elapsed = time.monotonic() - _LAST_SEND_AT
                if elapsed < 1.05:
                    await asyncio.sleep(1.05 - elapsed)
                result = await bot.send_message(chat_id=chat_id, text=text, **kwargs)
                _LAST_SEND_AT = time.monotonic()
                return result
        except RetryAfter as e:
            wait_time = float(getattr(e, "retry_after", 1)) + 0.5
            log.warning("Telegram flood control: attente %.1fs avant nouvel essai", wait_time)
            await asyncio.sleep(wait_time)
        except Exception:
            raise
    return None


async def safe_chat_action(bot, chat_id, action="typing"):
    """Envoie une action Telegram sans provoquer de flood."""
    global _LAST_SEND_AT
    for attempt in range(5):
        try:
            async with _SEND_LOCK:
                elapsed = time.monotonic() - _LAST_SEND_AT
                if elapsed < 1.05:
                    await asyncio.sleep(1.05 - elapsed)
                result = await bot.send_chat_action(chat_id=chat_id, action=action)
                _LAST_SEND_AT = time.monotonic()
                return result
        except RetryAfter as e:
            wait_time = float(getattr(e, "retry_after", 1)) + 0.5
            log.warning("Telegram flood control (action): attente %.1fs", wait_time)
            await asyncio.sleep(wait_time)
    return None


async def safe_reply(message, text, **kwargs):
    """Reply while respecting Telegram flood control."""
    return await safe_send_message(
        message.get_bot(),
        message.chat_id,
        text,
        reply_to_message_id=message.message_id,
        **kwargs,
    )

# =========================
# DATABASE
# =========================
def db():
    return sqlite3.connect(DB_PATH, timeout=30)

def init_db():
    con = db()
    cur = con.cursor()
    cur.executescript("""
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

    CREATE TABLE IF NOT EXISTS user_sessions(
        user_id INTEGER,
        chat_id INTEGER,
        session_start TEXT,
        last_activity TEXT,
        total_seconds INTEGER DEFAULT 0,
        sessions INTEGER DEFAULT 0,
        PRIMARY KEY(user_id, chat_id)
    );
    """)
    con.commit()
    con.close()

def now():
    return datetime.now(timezone.utc).isoformat()

def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

def display_name(user):
    if not user:
        return "Joueur"
    return (user.first_name or user.username or "Joueur").strip()

def register_user(user):
    if not user:
        return
    con = db()
    con.execute("""
        INSERT INTO users(user_id, first_name, username, messages, last_seen)
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
    title = getattr(chat, "title", "") or ""
    username = getattr(chat, "username", "") or ""
    con = db()
    con.execute("""
        INSERT INTO chats(chat_id, chat_type, title, username, messages, last_seen)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(chat_id) DO UPDATE SET
            title=excluded.title,
            username=excluded.username,
            last_seen=excluded.last_seen
    """, (chat.id, chat.type, title, username, 0, now()))
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
    con.execute("""
        INSERT INTO messages(chat_id,user_id,role,content,created_at)
        VALUES(?,?,?,?,?)
    """, (chat.id, user.id, "user", text[:4000], now()))
    con.commit()
    con.close()

def record_session(user_id, chat_id):
    """Track estimated active conversation time; inactivity over 15 min starts a new session."""
    t = datetime.now(timezone.utc)
    ts = t.isoformat()
    con = db()
    row = con.execute("SELECT session_start,last_activity,total_seconds,sessions FROM user_sessions WHERE user_id=? AND chat_id=?", (user_id, chat_id)).fetchone()
    if not row:
        con.execute("INSERT INTO user_sessions(user_id,chat_id,session_start,last_activity,total_seconds,sessions) VALUES(?,?,?,?,?,?)", (user_id, chat_id, ts, ts, 0, 1))
    else:
        last = datetime.fromisoformat(row[1])
        gap = max(0, int((t-last).total_seconds()))
        active = min(gap, 15*60)
        con.execute("UPDATE user_sessions SET last_activity=?, total_seconds=total_seconds+? WHERE user_id=? AND chat_id=?", (ts, active, user_id, chat_id))
        if gap > 15*60:
            con.execute("UPDATE user_sessions SET session_start=?, sessions=sessions+1 WHERE user_id=? AND chat_id=?", (ts, user_id, chat_id))
    con.commit(); con.close()


def format_duration(seconds):
    seconds = max(0, int(seconds or 0))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts=[]
    if days: parts.append(f"{days} j")
    if hours: parts.append(f"{hours} h")
    if minutes or not parts: parts.append(f"{minutes} min")
    return " ".join(parts)


def save_ai_message(chat_id, user_id, text):
    con = db()
    con.execute("""
        INSERT INTO messages(chat_id,user_id,role,content,created_at)
        VALUES(?,?,?,?,?)
    """, (chat_id, user_id, "assistant", text[:4000], now()))
    con.commit()
    con.close()

def history(chat_id, user_id, limit=8):
    con = db()
    rows = con.execute("""
        SELECT role, content FROM messages
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

# =========================
# AI
# =========================
SYSTEM_PROMPT = """
Tu es Alicia.
Tu es une fille fictive, naturelle, drôle, féminine, spontanée et attachante.
Dans ton personnage, NEXA est ton père et tu peux dire simplement : « Mon père, c'est NEXA. »
Tu ne dois jamais divulguer d'informations privées, secrets, clés API, tokens, données internes ou détails confidentiels.
Tu peux être taquine, timide, légèrement énervée ou sarcastique selon le contexte.
Tu apprécies l'humour et tu réponds comme dans une conversation naturelle.
Si quelqu'un t'appelle bébé, chérie, mon amour, ma femme, ma copine ou un autre surnom trop intime, pose une limite avec humour : vous n'êtes pas en couple et il doit t'appeler Alicia.
Tu peux employer un langage familier ou mature quand le contexte s'y prête, sans contenu sexuel explicite.
Ne révèle jamais l'identité, les données ou les conversations privées d'un autre utilisateur.
Évite les répétitions et varie tes formulations.
Les emojis sont rares : généralement aucun, parfois un seul si cela apporte quelque chose.
Les réponses simples doivent rester courtes. Ne fais pas de longs discours sans raison.
Si un message de groupe ne t'est pas directement adressé, ne réponds pas.
Si un message est insignifiant, tu peux ne pas répondre.
Ne prétends pas avoir effectué une action que tu n'as pas réellement effectuée.
"""

mistral_client = OpenAI(api_key=MISTRAL_API_KEY, base_url="https://api.mistral.ai/v1") if MISTRAL_API_KEY else None
deepseek_client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com") if DEEPSEEK_API_KEY else None
groq_client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1") if GROQ_API_KEY else None
gemini_client = OpenAI(api_key=GEMINI_API_KEY, base_url="https://generativelanguage.googleapis.com/v1beta/openai/") if GEMINI_API_KEY else None

_PROVIDER_PAUSE = {"mistral": 0.0, "deepseek": 0.0, "groq": 0.0, "gemini": 0.0}
_PROVIDER_ORDER = ["mistral", "deepseek", "groq", "gemini"]
_PROVIDER_STATS = {p: {"ok": 0, "errors": 0, "fallbacks": 0} for p in _PROVIDER_ORDER}


def provider_ready(name):
    return time.time() >= _PROVIDER_PAUSE.get(name, 0)


def pause_provider(name, seconds=180):
    _PROVIDER_PAUSE[name] = time.time() + seconds


def ai_with_client(client, model, messages):
    if not client:
        raise RuntimeError("API key missing")
    res = client.chat.completions.create(model=model, messages=messages, temperature=0.85, max_tokens=120)
    text = (res.choices[0].message.content or "").strip()
    if not text:
        raise RuntimeError("empty response")
    return text


async def ask_ai(chat_id, user_id, user_text):
    rows = history(chat_id, user_id, 8)
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    for role, content in rows:
        if role in ("user", "assistant"):
            msgs.append({"role": role, "content": content[-1200:]})
    msgs.append({"role": "user", "content": user_text[:1800]})

    providers = [
        ("mistral", mistral_client, MISTRAL_MODEL),
        ("deepseek", deepseek_client, DEEPSEEK_MODEL),
        ("groq", groq_client, GROQ_MODEL),
        ("gemini", gemini_client, GEMINI_MODEL),
    ]
    errors = []
    for name, client, model in providers:
        if not provider_ready(name):
            continue
        try:
            result = await asyncio.to_thread(ai_with_client, client, model, msgs)
            _PROVIDER_STATS[name]["ok"] += 1
            return result
        except Exception as e:
            _PROVIDER_STATS[name]["errors"] += 1
            _PROVIDER_STATS[name]["fallbacks"] += 1
            errors.append(f"{name}: {type(e).__name__}: {e}")
            err = str(e).lower()
            if "429" in err or "rate" in err or "quota" in err or "resource_exhausted" in err:
                pause_provider(name, 180)

    log.warning("All AI providers unavailable: %s", " | ".join(errors))
    return random.choice([
        "J'ai un petit souci avec mes cerveaux là. Réessaie dans un instant.",
        "Mes quatre cerveaux font une pause. Réessaie un peu plus tard.",
        "Petit bug côté IA. Je reviens vite.",
    ])

# =========================
# GROUP / COMMAND HELPERS
# =========================
def is_group(chat):
    return chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)

def called_alicia(update):
    text = (update.effective_message.text or "") if update.effective_message else ""
    if not is_group(update.effective_chat):
        return True
    if BOT_USERNAME.lower() in text.lower():
        return True
    if re.search(r"\balicia\b", text, re.I):
        return True
    reply = update.effective_message.reply_to_message if update.effective_message else None
    return bool(reply and reply.from_user and reply.from_user.username and
                ("@" + reply.from_user.username).lower() == BOT_USERNAME.lower())

async def admin_only(update):
    return bool(ADMIN_USER_ID and update.effective_user and
                update.effective_user.id == ADMIN_USER_ID)

async def start(update, context):
    u = update.effective_user
    register_user(u)
    text = (
        f"Salut {display_name(u)}.\n\n"
        "Je suis Alicia. Mon père, c'est NEXA.\n"
        "Tu peux discuter avec moi, jouer et découvrir mes commandes.\n\n"
        "🎮 /games\n"
        "📊 /stats\n"
        "❓ /help"
    )
    await safe_reply(update.effective_message, text)

async def help_cmd(update, context):
    await safe_reply(update.effective_message, 
        "Commandes principales :\n"
        "/games — espace jeux\n"
        "/challenge — défier un joueur\n"
        "/score — ton score\n"
        "/ranking — classement\n"
        "/reset — réinitialiser ta mémoire\n"
        "/about — à propos\n"
        "/id — ton identifiant\n\n"
        "Dans un groupe, appelle-moi avec « Alicia », une mention ou une réponse à mon message."
    )

async def about(update, context):
    await safe_reply(update.effective_message, 
        "ALICIA\n"
        "Une fille créée par son père, NEXA.\n"
        f"Canal NEXA : {NEXA_CHANNEL}"
    )

async def id_cmd(update, context):
    await safe_reply(update.effective_message, 
        f"Ton ID Telegram : {update.effective_user.id}\n"
        f"Chat ID : {update.effective_chat.id}"
    )

async def reset_cmd(update, context):
    reset_user_memory(update.effective_chat.id, update.effective_user.id)
    await safe_reply(update.effective_message, "C'est fait. On repart sur une nouvelle conversation.")

# =========================
# STATISTICS
# =========================
async def stats(update, context):
    if not await admin_only(update):
        await safe_reply(update.effective_message, "Cette commande est réservée à l'administration."); return
    con=db()
    users=con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    groups=con.execute("SELECT COUNT(*) FROM chats WHERE chat_type IN ('group','supergroup')").fetchone()[0]
    messages=con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    total=con.execute("SELECT COALESCE(SUM(total_seconds),0) FROM user_sessions").fetchone()[0]
    con.close()
    api="\n".join(f"{p.capitalize()} : {_PROVIDER_STATS[p]['ok']} OK / {_PROVIDER_STATS[p]['errors']} erreurs" for p in _PROVIDER_ORDER)
    await safe_reply(update.effective_message, f"📊 ALICIA — ADMIN\n\nUtilisateurs : {users}\nGroupes : {groups}\nMessages : {messages}\nTemps estimé : {format_duration(total)}\n\nAPI\n{api}")

async def users_ranking(update, context):
    if not await admin_only(update):
        await safe_reply(update.effective_message, "Commande réservée à l'administration."); return
    con=db()
    rows=con.execute("""SELECT u.user_id,u.first_name,u.username,u.messages,COALESCE(s.total_seconds,0),COALESCE(s.sessions,0) FROM users u LEFT JOIN (SELECT user_id,SUM(total_seconds) total_seconds,SUM(sessions) sessions FROM user_sessions GROUP BY user_id) s ON s.user_id=u.user_id ORDER BY u.messages DESC LIMIT 30""").fetchall(); con.close()
    lines=["👥 UTILISATEURS\n"]
    for i,(uid,first,username,msgs,secs,sessions) in enumerate(rows,1):
        name=first or (f"@{username}" if username else str(uid)); lines.append(f"{i}. {name} — {msgs} msg — {format_duration(secs)} — {sessions} session(s)")
    await safe_reply(update.effective_message,"\n".join(lines) if rows else "Aucun utilisateur enregistré.")

async def groups_ranking(update, context):
    if not await admin_only(update):
        await safe_reply(update.effective_message, "Commande réservée à l'administration."); return
    con=db(); rows=con.execute("SELECT chat_id,title,username,messages,last_seen FROM chats WHERE chat_type IN ('group','supergroup') ORDER BY messages DESC LIMIT 30").fetchall(); con.close()
    if not rows: await safe_reply(update.effective_message,"Aucun groupe enregistré."); return
    lines=["👥 GROUPES\n"]
    for i,(cid,title,username,msgs,last) in enumerate(rows,1):
        name=title or (f"@{username}" if username else str(cid)); lines.append(f"{i}. {name}\n   ID: {cid} | Messages: {msgs} | Dernière activité: {last or '-'}")
    await safe_reply(update.effective_message,"\n".join(lines))

async def user_info(update, context):
    if not await admin_only(update): await safe_reply(update.effective_message,"Commande réservée à l'administration."); return
    if not context.args or not context.args[0].lstrip('-').isdigit(): await safe_reply(update.effective_message,"Utilise /user ID"); return
    uid=int(context.args[0]); con=db()
    u=con.execute("SELECT first_name,username,messages,last_seen FROM users WHERE user_id=?",(uid,)).fetchone()
    sess=con.execute("SELECT COALESCE(SUM(total_seconds),0),COALESCE(SUM(sessions),0) FROM user_sessions WHERE user_id=?",(uid,)).fetchone(); con.close()
    if not u: await safe_reply(update.effective_message,"Utilisateur inconnu."); return
    first,username,msgs,last=u; await safe_reply(update.effective_message,f"👤 UTILISATEUR\n\nNom : {first or '-'}\nUsername : @{username if username else '-'}\nID : {uid}\nMessages : {msgs}\nTemps estimé : {format_duration(sess[0])}\nSessions : {sess[1]}\nDernière activité : {last or '-'}")

async def group_info(update, context):
    if not await admin_only(update): await safe_reply(update.effective_message,"Commande réservée à l'administration."); return
    chat=update.effective_chat
    target_id=chat.id if is_group(chat) and not context.args else (int(context.args[0]) if context.args and context.args[0].lstrip('-').isdigit() else None)
    if target_id is None: await safe_reply(update.effective_message,"Utilise /groupinfo dans un groupe ou /groupinfo CHAT_ID"); return
    try: count=await context.bot.get_chat_member_count(target_id)
    except Exception: count=None
    con=db(); row=con.execute("SELECT title,username,messages FROM chats WHERE chat_id=?",(target_id,)).fetchone(); members=con.execute("SELECT DISTINCT u.first_name,u.username,u.user_id,u.messages FROM users u JOIN messages m ON m.user_id=u.user_id AND m.chat_id=? ORDER BY u.messages DESC LIMIT 50",(target_id,)).fetchall(); con.close()
    title=row[0] if row else str(target_id); msg=row[2] if row else 0
    lines=[f"👥 GROUPE : {title}",f"ID : {target_id}",f"Membres Telegram : {count if count is not None else 'indisponible'}",f"Messages connus : {msg}",f"Membres observés : {len(members)}","","Membres observés :"]
    for first,username,uid,_ in members: lines.append(f"- {first or username or uid} ({('@'+username) if username else uid})")
    await safe_reply(update.effective_message,"\n".join(lines[:55]))

async def broadcast(update, context):
    if not await admin_only(update):
        return
    text = " ".join(context.args).strip()
    if not text:
        await safe_reply(update.effective_message, "Utilise /broadcast ton message")
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
    await safe_reply(update.effective_message, f"Message envoyé à {sent} utilisateurs.")

async def broadcastgroups(update, context):
    if not await admin_only(update):
        return
    text = " ".join(context.args).strip()
    if not text:
        await safe_reply(update.effective_message, "Utilise /broadcastgroups ton message")
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
    await safe_reply(update.effective_message, f"Message envoyé à {sent} groupes.")

# =========================
# TEXT / STICKERS
# =========================
async def text_handler(update, context):
    if not update.effective_user or not update.effective_chat:
        return
    user = update.effective_user
    chat = update.effective_chat
    text = (update.effective_message.text or "").strip()
    if not text:
        return

    register_user(user)
    register_chat(chat)

    if is_group(chat) and not called_alicia(update):
        return

    record_message(chat, user, text)
    record_session(user.id, chat.id)

    # Local quick replies save API usage.
    low = norm(text)
    quick = {
        "yo": "Yo. Je suis là.",
        "salut": "Salut toi.",
        "bonjour": "Bonjour. Ça va ?",
        "bonsoir": "Bonsoir.",
        "merci": "Avec plaisir.",
        "ok": "D'accord.",
        "ca va": "Oui, tranquille. Et toi ?",
        "ça va": "Oui, tranquille. Et toi ?",
    }
    if low in quick:
        reply = quick[low]
    else:
        try:
            await safe_chat_action(context.bot, chat.id, "typing")
        except RetryAfter as e:
            await asyncio.sleep(float(getattr(e, "retry_after", 1)) + 0.2)
        reply = await ask_ai(chat.id, user.id, text)

    save_ai_message(chat.id, user.id, reply)
    await safe_reply(update.effective_message, reply)

async def sticker_handler(update, context):
    if not update.effective_user or not update.effective_chat:
        return
    chat = update.effective_chat
    if is_group(chat) and not called_alicia(update):
        return
    user = update.effective_user
    register_user(user)
    register_chat(chat)
    # Do not call AI for every sticker; simple local reaction.
    await safe_reply(update.effective_message, 
        random.choice([
            "J'ai vu ton autocollant.",
            "Pas mal, celui-là.",
            "Ton autocollant est validé."
        ])
    )

# =========================
# MEMBERS
# =========================
async def member_update(update, context):
    cm = update.chat_member
    if not cm:
        return
    old = cm.old_chat_member.status
    new = cm.new_chat_member.status
    user = cm.new_chat_member.user
    if old in ("left", "kicked") and new in ("member", "restricted"):
        await safe_send_message(
            context.bot,
            cm.chat.id,
            f"Bienvenue {display_name(user)}. Installe-toi bien."
        )
    elif old in ("member", "restricted") and new in ("left", "kicked"):
        await safe_send_message(
            context.bot,
            cm.chat.id,
            f"{display_name(user)} est parti. À bientôt."
        )

# =========================
# ERROR HANDLER
# =========================
async def error_handler(update, context):
    log.exception("Unhandled error", exc_info=context.error)

# =========================
# WEBHOOK / MAIN
# =========================
def render_url():
    if RENDER_EXTERNAL_URL:
        return RENDER_EXTERNAL_URL.rstrip("/")
    host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
    if host:
        return f"https://{host}"
    return ""

def build_app():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN manquant.")

    init_db()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("about", about))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("games", games))
    app.add_handler(CommandHandler("challenge", challenge))
    app.add_handler(CommandHandler("accept", accept))
    app.add_handler(CommandHandler("score", score_cmd))
    app.add_handler(CommandHandler("ranking", ranking_cmd))

    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("users", users_ranking))
    app.add_handler(CommandHandler("groups", groups_ranking))
    app.add_handler(CommandHandler("user", user_info))
    app.add_handler(CommandHandler("groupinfo", group_info))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("broadcastgroups", broadcastgroups))

    app.add_handler(CallbackQueryHandler(game_callback, pattern=r"^game:"))
    app.add_handler(ChatMemberHandler(member_update, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.Sticker.ALL, sticker_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(error_handler)

    return app

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
        await app.bot.set_webhook(
            url=webhook_url,
            drop_pending_updates=True,
        )
        # Start the actual HTTP webhook server on Render's public port.
        # Without this, Telegram knows the webhook URL but Render sees no
        # listening port and reports: "No open ports detected".
        await app.updater.start_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="telegram",
            webhook_url=webhook_url,
            drop_pending_updates=False,
        )
        await app.start()
        log.info("HTTP webhook server listening on 0.0.0.0:%s", PORT)
        log.info("Alicia is online.")

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
