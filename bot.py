import os
import re
import json
import time
import random
import sqlite3
import asyncio
import logging
from pathlib import Path
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
ALICIA_IMAGE = os.getenv("ALICIA_IMAGE", "alicia.png").strip()

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


async def safe_send_photo(bot, chat_id, photo, **kwargs):
    global _LAST_SEND_AT
    async with _SEND_LOCK:
        elapsed=time.monotonic()-_LAST_SEND_AT
        if elapsed<1.05: await asyncio.sleep(1.05-elapsed)
        try:
            result=await bot.send_photo(chat_id=chat_id,photo=photo,**kwargs)
            _LAST_SEND_AT=time.monotonic()
            return result
        except RetryAfter as e:
            await asyncio.sleep(float(getattr(e,'retry_after',1))+0.5)
            result=await bot.send_photo(chat_id=chat_id,photo=photo,**kwargs)
            _LAST_SEND_AT=time.monotonic()
            return result


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
    username = BOT_USERNAME.lstrip("@")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Ajouter ALICIA à un groupe", url=f"https://t.me/{username}?startgroup=true")],
        [InlineKeyboardButton("🎮 Jeux", callback_data="menu:games"), InlineKeyboardButton("📖 FAQ", callback_data="menu:faq")],
        [InlineKeyboardButton("💬 Parler à Alicia", callback_data="menu:talk"), InlineKeyboardButton("👤 Mon profil", callback_data="menu:profile")],
        [InlineKeyboardButton("ℹ️ À propos", callback_data="menu:about")],
    ])
    text = (
        f"👋 Salut {display_name(u)} !\n\n"
        "Je suis ALICIA. Je suis la fille de NEXA. ❤️\n\n"
        "J'ai mon caractère, mes jeux et plein de choses à découvrir. "
        "Tu peux discuter avec moi, jouer, me lancer un défi ou découvrir mes fonctionnalités.\n\n"
        "Bienvenue dans mon univers."
    )
    image = Path(ALICIA_IMAGE)
    if image.exists():
        try:
            with image.open("rb") as photo:
                return await safe_send_photo(context.bot, update.effective_chat.id, photo, caption=text, reply_markup=kb)
        except Exception as e:
            log.warning("Envoi alicia.png impossible: %r", e)
    await safe_reply(update.effective_message, text, reply_markup=kb)

async def help_cmd(update, context):
    await safe_reply(update.effective_message, """📚 COMMANDES D'ALICIA

👤 Général
/start — Accueil
/help — Commandes
/faq — Guide d'utilisation
/about — À propos
/profile — Mon profil
/id — Mon identifiant
/reset — Nouvelle conversation
/clear — Effacer le contexte
/mood — Humeur d'Alicia
/ask — Poser une question

🎮 Jeux
/games — Menu des jeux
/challenge — Défier un joueur
/accept — Accepter un défi
/score — Mon score
/ranking — Classement

😂 Divertissement
/joke — Blague
/quote — Citation
/coin — Pile ou face
/8ball — Question à Alicia
/choose — Choix entre deux options
/compliment — Compliment
/roast — Taquinerie
/motivate — Motivation

👥 Groupes
/groupinfo — Informations du groupe
/groupstats — Statistiques du groupe
/top — Membres les plus actifs

Dans un groupe, appelle-moi avec « Alicia », @im_a_aliciabot ou réponds à un de mes messages.""")

async def faq_cmd(update, context):
    await safe_reply(update.effective_message, """📖 FAQ ALICIA

💬 En privé : écris-moi normalement.
👥 En groupe : mentionne @im_a_aliciabot, écris « Alicia » ou réponds à mon message.
🎮 Pour jouer : ouvre /games puis choisis un jeu.
👥 Pour inviter un joueur : /challenge @pseudo puis /accept.
🏆 Les victoires rapportent des points selon le jeu.
🔐 Alicia ne révèle pas les informations privées, secrets, tokens ou clés API.
👨‍👧 Mon père : NEXA.

Pour les règles détaillées, ouvre le menu Jeux puis « 📖 Comment jouer ? ».""")

async def about(update, context):
    await safe_reply(update.effective_message, f"ALICIA\n\nJe suis la fille de NEXA.\n\nCanal NEXA : {NEXA_CHANNEL}")

async def id_cmd(update, context):
    await safe_reply(update.effective_message, f"Ton ID Telegram : {update.effective_user.id}\nChat ID : {update.effective_chat.id}")

async def profile_cmd(update, context):
    u = update.effective_user
    pts, wins, losses = get_score(update.effective_chat.id, u.id)
    con = db(); row = con.execute("SELECT messages FROM users WHERE user_id=?", (u.id,)).fetchone(); con.close()
    await safe_reply(update.effective_message, f"👤 PROFIL\n\nNom : {display_name(u)}\nID : {u.id}\nMessages : {row[0] if row else 0}\n🏆 {pts} points\nVictoires : {wins}\nDéfaites : {losses}")

async def clear_cmd(update, context):
    reset_user_memory(update.effective_chat.id, update.effective_user.id)
    await safe_reply(update.effective_message, "Contexte effacé. On repart à zéro.")

async def mood_cmd(update, context):
    await safe_reply(update.effective_message, random.choice(["Aujourd'hui je suis de bonne humeur.", "Je suis calme… pour l'instant.", "Un peu taquine aujourd'hui.", "Je suis concentrée. Vas-y, parle."]))

async def ask_cmd(update, context):
    question = " ".join(context.args).strip()
    if not question:
        await safe_reply(update.effective_message, "Utilise /ask suivi de ta question.")
        return
    await safe_chat_action(context.bot, update.effective_chat.id, "typing")
    reply = await ask_ai(update.effective_chat.id, update.effective_user.id, question)
    await safe_reply(update.effective_message, reply)

# =========================
# STATISTICS / TABLEAUX ADMIN
# =========================
async def stats(update, context):
    if not await admin_only(update):
        await safe_reply(update.effective_message, "Cette commande est réservée à l'administration.")
        return
    con=db()
    users=con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    groups=con.execute("SELECT COUNT(*) FROM chats WHERE chat_type IN ('group','supergroup')").fetchone()[0]
    messages=con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    total=con.execute("SELECT COALESCE(SUM(total_seconds),0) FROM user_sessions").fetchone()[0]
    con.close()
    api="\n".join(f"{p.capitalize()} : {_PROVIDER_STATS[p]['ok']} OK / {_PROVIDER_STATS[p]['errors']} erreurs" for p in _PROVIDER_ORDER)
    await safe_reply(update.effective_message, f"📊 ALICIA — ADMIN\n\nUtilisateurs : {users}\nGroupes actifs : {groups}\nMessages : {messages}\nTemps estimé : {format_duration(total)}\n\nAPI\n{api}")

async def users_ranking(update, context):
    if not await admin_only(update):
        await safe_reply(update.effective_message, "Commande réservée à l'administration."); return
    con=db(); rows=con.execute("SELECT first_name,username,messages,last_seen FROM users ORDER BY messages DESC LIMIT 30").fetchall(); con.close()
    if not rows: await safe_reply(update.effective_message,"Aucun utilisateur enregistré."); return
    lines=["👤 UTILISATEURS ACTIFS","","#  NOM                 MSG      DERNIÈRE ACTIVITÉ","-- ------------------ ------   -------------------"]
    for i,(first,username,count,last) in enumerate(rows,1):
        name=(first or ("@"+username if username else "Utilisateur"))[:18]; last=(last or "-").replace("T"," ")[:19]
        lines.append(f"{i:<2} {name:<18} {count:>6}   {last}")
    await safe_reply(update.effective_message,"```text\n"+"\n".join(lines)+"\n```")

async def groups_ranking(update, context):
    if not await admin_only(update):
        await safe_reply(update.effective_message, "Commande réservée à l'administration."); return
    con=db(); rows=con.execute("SELECT chat_id,title,username,messages,last_seen FROM chats WHERE chat_type IN ('group','supergroup') ORDER BY messages DESC LIMIT 30").fetchall(); con.close()
    if not rows: await safe_reply(update.effective_message,"Aucun groupe enregistré."); return
    lines=["👥 GROUPES ACTIFS","","#  GROUPE              MSG      DERNIÈRE ACTIVITÉ","-- ------------------ ------   -------------------"]
    for i,(cid,title,username,count,last) in enumerate(rows,1):
        name=(title or ("@"+username if username else str(cid)))[:19]; last=(last or "-").replace("T"," ")[:19]
        lines.append(f"{i:<2} {name:<19} {count:>6}   {last}")
    await safe_reply(update.effective_message,"```text\n"+"\n".join(lines)+"\n```")

async def user_info(update, context):
    if not await admin_only(update): await safe_reply(update.effective_message,"Commande réservée à l'administration."); return
    if not context.args or not context.args[0].lstrip('-').isdigit(): await safe_reply(update.effective_message,"Utilise /user ID"); return
    uid=int(context.args[0]); con=db(); row=con.execute("SELECT first_name,username,messages,last_seen FROM users WHERE user_id=?",(uid,)).fetchone(); sess=con.execute("SELECT COALESCE(SUM(total_seconds),0),COALESCE(SUM(sessions),0) FROM user_sessions WHERE user_id=?",(uid,)).fetchone(); con.close()
    if not row: await safe_reply(update.effective_message,"Utilisateur inconnu."); return
    await safe_reply(update.effective_message,f"👤 UTILISATEUR\n\nNom : {row[0] or '-'}\nUsername : @{row[1] or '-'}\nID : {uid}\nMessages : {row[2]}\nTemps estimé : {format_duration(sess[0])}\nSessions : {sess[1]}\nDernière activité : {row[3] or '-'}")

async def group_info(update, context):
    if not await admin_only(update): await safe_reply(update.effective_message,"Commande réservée à l'administration."); return
    target_id=update.effective_chat.id if is_group(update.effective_chat) and not context.args else None
    if context.args and context.args[0].lstrip('-').isdigit(): target_id=int(context.args[0])
    if target_id is None: await safe_reply(update.effective_message,"Utilise /groupinfo dans un groupe ou /groupinfo CHAT_ID"); return
    try: members=await context.bot.get_chat_member_count(target_id)
    except Exception: members="inconnu"
    con=db(); row=con.execute("SELECT title,username,messages,last_seen FROM chats WHERE chat_id=?",(target_id,)).fetchone(); con.close()
    if not row: await safe_reply(update.effective_message,"Groupe introuvable dans ma base."); return
    await safe_reply(update.effective_message,f"👥 GROUPE\n\nNom : {row[0] or '-'}\nUsername : @{row[1] or '-'}\nID : {target_id}\nMembres : {members}\nMessages observés : {row[2]}\nDernière activité : {row[3] or '-'}")

async def groupstats(update, context):
    if not is_group(update.effective_chat): await safe_reply(update.effective_message,"Cette commande s'utilise dans un groupe."); return
    con=db(); row=con.execute("SELECT title,messages,last_seen FROM chats WHERE chat_id=?",(update.effective_chat.id,)).fetchone(); con.close()
    try: members=await context.bot.get_chat_member_count(update.effective_chat.id)
    except Exception: members="inconnu"
    await safe_reply(update.effective_message,f"👥 STATISTIQUES DU GROUPE\n\nNom : {row[0] if row else update.effective_chat.title}\nMembres : {members}\nMessages observés : {row[1] if row else 0}\nDernière activité : {row[2] if row else '-'}")

async def top_cmd(update, context):
    con=db(); rows=con.execute("SELECT first_name,username,messages FROM users ORDER BY messages DESC LIMIT 10").fetchall(); con.close()
    lines=["🔥 TOP MEMBRES ACTIFS","","#  NOM                  MESSAGES","-- ------------------   --------"]
    for i,(first,username,count) in enumerate(rows,1): lines.append(f"{i:<2} {(first or ('@'+username if username else 'Utilisateur'))[:18]:<18} {count:>8}")
    await safe_reply(update.effective_message,"```text\n"+"\n".join(lines)+"\n```")

async def joke_cmd(update, context): await safe_reply(update.effective_message, random.choice(["Pourquoi les développeurs aiment le café ? Parce que sans lui, ils compilent leur fatigue.", "J'allais raconter une blague sur les bugs… mais elle a planté."]))
async def quote_cmd(update, context): await safe_reply(update.effective_message, random.choice(["Avance à ton rythme, mais avance.", "Les petites améliorations font les grandes différences."]))
async def coin_cmd(update, context): await safe_reply(update.effective_message, random.choice(["🪙 Face.", "🪙 Pile."]))
async def ball_cmd(update, context): await safe_reply(update.effective_message, random.choice(["Oui.", "Non.", "Peut-être.", "Je dirais que oui.", "Essaie encore."]))
async def choose_cmd(update, context):
    parts=" ".join(context.args).split(" ou ")
    await safe_reply(update.effective_message, random.choice(parts).strip() if len(parts)>=2 else "Donne-moi deux choix séparés par « ou ».")
async def compliment_cmd(update, context): await safe_reply(update.effective_message, random.choice(["T'as l'air sympa aujourd'hui.", "Franchement, t'as de bonnes idées.", "Je valide ton énergie."]))
async def roast_cmd(update, context): await safe_reply(update.effective_message, random.choice(["Toi, même Google aurait besoin d'une pause pour te comprendre.", "T'es incroyable… surtout quand tu te trompes avec confiance."]))
async def motivate_cmd(update, context): await safe_reply(update.effective_message, "Allez. Une étape à la fois. Tu peux le faire.")

async def reset_cmd(update, context):
    reset_user_memory(update.effective_chat.id, update.effective_user.id)
    await safe_reply(update.effective_message, "C'est fait. On repart sur une nouvelle conversation.")

async def clear_cmd(update, context):
    reset_user_memory(update.effective_chat.id, update.effective_user.id)
    await safe_reply(update.effective_message, "Le contexte de cette conversation a été effacé.")

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

    if await handle_game_text(update, context):
        return

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
# GAMES
# =========================
GAME_SESSIONS={}
GAME_INVITES={}
QUIZ_QUESTIONS=[
    ("Quelle est la capitale de la France ?",["Paris"]),
    ("Combien font 7 x 8 ?",["56"]),
    ("Quelle planète est surnommée la planète rouge ?",["Mars"]),
    ("Quel est le plus grand océan ?",["Pacifique","océan Pacifique","ocean pacifique"]),
]

def game_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 Jouer contre Alicia",callback_data="game:ai")],
        [InlineKeyboardButton("👥 Jouer contre un joueur",callback_data="game:pvp")],
        [InlineKeyboardButton("❌ Tic-Tac-Toe",callback_data="game:ttt"),InlineKeyboardButton("🔴 Connect 4",callback_data="game:c4")],
        [InlineKeyboardButton("🔢 Devine le nombre",callback_data="game:guess"),InlineKeyboardButton("🧠 Quiz",callback_data="game:quiz")],
        [InlineKeyboardButton("🏆 Mon score",callback_data="game:score"),InlineKeyboardButton("🥇 Classement",callback_data="game:ranking")],
        [InlineKeyboardButton("📖 Comment jouer ?",callback_data="game:faq")],
    ])

async def games(update, context):
    await safe_reply(update.effective_message,"🎮 JEUX D'ALICIA\n\nChoisis un jeu ou consulte le guide.",reply_markup=game_menu())

async def challenge(update, context):
    if not context.args:
        await safe_reply(update.effective_message,"Utilise /challenge @pseudo")
        return
    target=context.args[0].lstrip('@').lower()
    GAME_INVITES[target]={"from_id":update.effective_user.id,"from_name":display_name(update.effective_user),"created":time.time()}
    await safe_reply(update.effective_message,f"Défi envoyé à @{target}. Il doit ouvrir Alicia et utiliser /accept.")

async def accept(update, context):
    key=(update.effective_user.username or '').lower()
    inv=GAME_INVITES.get(key)
    if not inv:
        await safe_reply(update.effective_message,"Aucune invitation trouvée pour toi.")
        return
    if time.time()-inv['created']>600:
        GAME_INVITES.pop(key,None); await safe_reply(update.effective_message,"Cette invitation a expiré."); return
    GAME_INVITES.pop(key,None)
    GAME_SESSIONS[update.effective_user.id]={"mode":"pvp","players":[inv['from_id'],update.effective_user.id],"opponent":inv['from_id']}
    await safe_reply(update.effective_message,f"Défi accepté contre {inv['from_name']}. Choisissez un jeu.",reply_markup=game_menu())

async def game_faq(update, context):
    await safe_reply(update.effective_message,"""📖 GUIDE DES JEUX

❌ Tic-Tac-Toe : aligne 3 symboles.
🔴 Connect 4 : aligne 4 jetons.
🔢 Devine le nombre : trouve le nombre secret avec les indices.
🧠 Quiz : donne la bonne réponse.

🤖 Contre Alicia : choisis un jeu et la partie commence.
👥 Contre un joueur : /challenge @pseudo puis /accept.
🏆 Les victoires donnent des points et le classement est visible avec /ranking.

Chaque jeu affiche ses règles et un exemple avant la partie.""")

def ttt_board(board):
    return InlineKeyboardMarkup([[InlineKeyboardButton(board[r*3+c] or '·',callback_data=f"ttt:{r*3+c}") for c in range(3)] for r in range(3)])

def ttt_winner(b):
    lines=[(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]
    for a,c,d in lines:
        if b[a] and b[a]==b[c]==b[d]: return b[a]
    return 'draw' if all(b) else None

def c4_board(board):
    return InlineKeyboardMarkup([[InlineKeyboardButton(str(i+1),callback_data=f"c4:{i}") for i in range(7)], [InlineKeyboardButton(' '.join(board[r][c] or '·' for c in range(7)),callback_data='noop') for r in range(6)]])

def c4_winner(b):
    for r in range(6):
        for c in range(7):
            if not b[r][c]: continue
            p=b[r][c]
            for dr,dc in ((0,1),(1,0),(1,1),(1,-1)):
                if all(0<=r+dr*k<6 and 0<=c+dc*k<7 and b[r+dr*k][c+dc*k]==p for k in range(4)): return p
    return 'draw' if all(b[r][c] for r in range(6) for c in range(7)) else None

async def start_ttt(q, uid):
    state={"game":"ttt","board":[None]*9,"uid":uid}
    GAME_SESSIONS[uid]=state
    await q.edit_message_text("❌ TIC-TAC-TOE\n\nTu es X. Aligne 3 symboles pour gagner.\n\nExemple : clique sur une case.",reply_markup=ttt_board(state['board']))

async def start_c4(q, uid):
    state={"game":"c4","board":[[None]*7 for _ in range(6)],"uid":uid}
    GAME_SESSIONS[uid]=state
    await q.edit_message_text("🔴 CONNECT 4\n\nTu es 🔴. Aligne 4 jetons. Choisis une colonne.",reply_markup=c4_board(state['board']))

async def finish_ttt(q,state,result,uid):
    GAME_SESSIONS.pop(uid,None)
    if result=='X': add_score(q.message.chat.id,uid,str(uid),10,win=True); text="🏆 Tu as gagné ! +10 points"
    elif result=='O': add_score(q.message.chat.id,uid,str(uid),0,loss=True); text="Alicia gagne cette manche."
    else: text="🤝 Match nul."
    await q.edit_message_text(text)

async def game_callback(update, context):
    q=update.callback_query; data=q.data or ''; uid=q.from_user.id
    try: await q.answer()
    except Exception: pass
    if data=='menu:games': await q.edit_message_text("🎮 JEUX D'ALICIA\n\nChoisis un jeu :",reply_markup=game_menu()); return
    if data=='menu:faq': await q.edit_message_text("📖 Pour utiliser Alicia : écris-lui en privé. En groupe, mentionne-la, écris Alicia ou réponds à son message. Pour les jeux, ouvre /games puis « Comment jouer ? »."); return
    if data=='menu:talk': await q.edit_message_text("💬 Écris-moi simplement ton message. Je t'écoute."); return
    if data=='menu:profile':
        pts,w,l=get_score(update.effective_chat.id,uid); await q.edit_message_text(f"👤 Ton profil\n\n🏆 {pts} points\nVictoires : {w}\nDéfaites : {l}"); return
    if data=='menu:about': await q.edit_message_text(f"ALICIA\n\nJe suis la fille de NEXA.\nCanal : {NEXA_CHANNEL}"); return
    if data=='game:faq': await game_faq(update,context); return
    if data=='game:score':
        pts,w,l=get_score(update.effective_chat.id,uid); await q.edit_message_text(f"🏆 Score\n\n{pts} points\n{w} victoires\n{l} défaites"); return
    if data=='game:ranking':
        con=db(); rows=con.execute("SELECT name,points,wins,losses FROM scores WHERE chat_id=? ORDER BY points DESC,wins DESC LIMIT 10",(update.effective_chat.id,)).fetchall(); con.close()
        text="🏆 CLASSEMENT\n\n"+"\n".join(f"{i}. {n} — {p} pts | {w}V / {l}D" for i,(n,p,w,l) in enumerate(rows,1)) if rows else "🏆 Le classement est encore vide."
        await q.edit_message_text(text); return
    if data=='game:ai':
        await q.edit_message_text("🤖 JE JOUe CONTRE ALICIA\n\nChoisis un jeu.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ Tic-Tac-Toe',callback_data='game:ttt')],[InlineKeyboardButton('🔴 Connect 4',callback_data='game:c4')],[InlineKeyboardButton('🔢 Devine le nombre',callback_data='game:guess')],[InlineKeyboardButton('🧠 Quiz',callback_data='game:quiz')]])); return
    if data=='game:pvp': await q.edit_message_text("👥 Pour inviter un joueur : /challenge @pseudo\n\nLe joueur devra utiliser /accept."); return
    if data=='game:ttt': await start_ttt(q,uid); return
    if data=='game:c4': await start_c4(q,uid); return
    if data=='game:guess':
        GAME_SESSIONS[uid]={"game":"guess","number":random.randint(1,100),"tries":0}
        await q.edit_message_text("🔢 DEVINE LE NOMBRE\n\nJ'ai choisi un nombre entre 1 et 100. Écris ton essai.\n\nExemple : 50"); return
    if data=='game:quiz':
        question=random.choice(QUIZ_QUESTIONS); GAME_SESSIONS[uid]={"game":"quiz","question":question}
        await q.edit_message_text(f"🧠 QUIZ\n\n{question[0]}\n\nÉcris ta réponse."); return
    if data.startswith('ttt:'):
        state=GAME_SESSIONS.get(uid)
        if not state or state.get('game')!='ttt': return
        pos=int(data.split(':')[1]); b=state['board']
        if b[pos]: return
        b[pos]='X'; result=ttt_winner(b)
        if result: await finish_ttt(q,state,result,uid); return
        free=[i for i,x in enumerate(b) if not x]
        if free: b[random.choice(free)]='O'
        result=ttt_winner(b)
        if result: await finish_ttt(q,state,result,uid); return
        await q.edit_message_text("❌ TIC-TAC-TOE\n\nÀ toi.",reply_markup=ttt_board(b)); return
    if data.startswith('c4:'):
        state=GAME_SESSIONS.get(uid)
        if not state or state.get('game')!='c4': return
        col=int(data.split(':')[1]); b=state['board']
        row=next((r for r in range(5,-1,-1) if not b[r][col]),None)
        if row is None: return
        b[row][col]='🔴'; result=c4_winner(b)
        if result: GAME_SESSIONS.pop(uid,None); add_score(update.effective_chat.id,uid,display_name(update.effective_user),10 if result=='🔴' else 0,win=result=='🔴',loss=result=='🟡'); await q.edit_message_text("🏆 Tu as gagné ! +10 points" if result=='🔴' else ("🤝 Match nul." if result=='draw' else "Alicia gagne.")); return
        cols=[c for c in range(7) if any(not b[r][c] for r in range(6))]
        if cols:
            ac=random.choice(cols); ar=next(r for r in range(5,-1,-1) if not b[r][ac]); b[ar][ac]='🟡'
        result=c4_winner(b)
        if result: GAME_SESSIONS.pop(uid,None); add_score(update.effective_chat.id,uid,display_name(update.effective_user),0,loss=result=='🟡'); await q.edit_message_text("Alicia gagne." if result=='🟡' else "🤝 Match nul."); return
        await q.edit_message_text("🔴 CONNECT 4\n\nÀ toi.",reply_markup=c4_board(b)); return

async def handle_game_text(update,context):
    uid=update.effective_user.id; state=GAME_SESSIONS.get(uid); text=(update.effective_message.text or '').strip()
    if not state: return False
    if state.get('game')=='guess':
        try: n=int(text)
        except ValueError: return False
        state['tries']+=1
        if n==state['number']:
            GAME_SESSIONS.pop(uid,None); add_score(update.effective_chat.id,uid,display_name(update.effective_user),10,win=True); await safe_reply(update.effective_message,f"Bravo ! C'était {n}. +10 points"); return True
        await safe_reply(update.effective_message,"Plus grand." if n<state['number'] else "Plus petit."); return True
    if state.get('game')=='quiz':
        if norm(text) in [norm(x) for x in state['question'][1]]:
            GAME_SESSIONS.pop(uid,None); add_score(update.effective_chat.id,uid,display_name(update.effective_user),10,win=True); await safe_reply(update.effective_message,"Bonne réponse ! +10 points"); return True
        GAME_SESSIONS.pop(uid,None); add_score(update.effective_chat.id,uid,display_name(update.effective_user),0,loss=True); await safe_reply(update.effective_message,"Pas cette fois. La partie est terminée."); return True
    return False

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


async def score_cmd(update, context):
    pts,wins,losses=get_score(update.effective_chat.id,update.effective_user.id)
    await safe_reply(update.effective_message,f"🏆 {display_name(update.effective_user)}\n\nPoints : {pts}\nVictoires : {wins}\nDéfaites : {losses}")

async def ranking_cmd(update, context):
    con=db(); rows=con.execute("SELECT name,points,wins,losses FROM scores WHERE chat_id=? ORDER BY points DESC,wins DESC LIMIT 20",(update.effective_chat.id,)).fetchall(); con.close()
    if not rows: await safe_reply(update.effective_message,"🏆 Le classement est encore vide."); return
    lines=["🏆 CLASSEMENT","","#  JOUEUR               POINTS   V   D","-- ------------------   ------   -   -"]
    for i,(name,pts,wins,losses) in enumerate(rows,1): lines.append(f"{i:<2} {name[:18]:<18}   {pts:>6}   {wins:>1}   {losses:>1}")
    await safe_reply(update.effective_message,"```text\n"+"\n".join(lines)+"\n```")

async def admin(update, context):
    if not await admin_only(update):
        await safe_reply(update.effective_message,"Commande réservée à l'administration."); return
    await safe_reply(update.effective_message,"👑 ADMIN ALICIA\n\n/stats — statistiques générales\n/users — tableau utilisateurs actifs\n/groups — tableau groupes actifs\n/user ID — détail utilisateur\n/groupinfo ID — détail groupe\n/broadcast texte — message privé\n/broadcastgroups texte — message groupes")

async def broadcast(update, context):
    if not await admin_only(update): return
    text=" ".join(context.args).strip()
    if not text: await safe_reply(update.effective_message,"Utilise /broadcast ton message"); return
    con=db(); ids=[r[0] for r in con.execute("SELECT user_id FROM users").fetchall()]; con.close(); sent=0
    for uid in ids:
        try: await safe_send_message(context.bot,uid,text); sent+=1
        except Exception: pass
    await safe_reply(update.effective_message,f"Message envoyé à {sent} utilisateurs.")

async def broadcastgroups(update, context):
    if not await admin_only(update): return
    text=" ".join(context.args).strip()
    if not text: await safe_reply(update.effective_message,"Utilise /broadcastgroups ton message"); return
    con=db(); ids=[r[0] for r in con.execute("SELECT chat_id FROM chats WHERE chat_type IN ('group','supergroup')").fetchall()]; con.close(); sent=0
    for cid in ids:
        try: await safe_send_message(context.bot,cid,text); sent+=1
        except Exception: pass
    await safe_reply(update.effective_message,f"Message envoyé à {sent} groupes.")

async def post_init(application):
    commands=[
        ("start","Accueil d'Alicia"),("help","Commandes"),("faq","Guide"),("about","À propos"),("profile","Mon profil"),("id","Mon identifiant"),("reset","Nouvelle conversation"),("clear","Effacer le contexte"),("mood","Humeur"),("ask","Poser une question"),
        ("games","Jeux"),("challenge","Défier un joueur"),("accept","Accepter un défi"),("score","Mon score"),("ranking","Classement"),
        ("joke","Blague"),("quote","Citation"),("coin","Pile ou face"),("8ball","Question"),("choose","Choisir"),("compliment","Compliment"),("roast","Taquinerie"),("motivate","Motivation"),
    ]
    try: await application.bot.set_my_commands(commands)
    except Exception as e: log.warning("Impossible de définir les commandes Telegram: %r",e)

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
    app=Application.builder().token(TOKEN).post_init(post_init).build()
    public=[
        ("start",start),("help",help_cmd),("faq",faq_cmd),("about",about),("profile",profile_cmd),("id",id_cmd),("reset",reset_cmd),("clear",clear_cmd),("mood",mood_cmd),("ask",ask_cmd),
        ("games",games),("challenge",challenge),("accept",accept),("score",score_cmd),("ranking",ranking_cmd),
        ("joke",joke_cmd),("quote",quote_cmd),("coin",coin_cmd),("8ball",ball_cmd),("choose",choose_cmd),("compliment",compliment_cmd),("roast",roast_cmd),("motivate",motivate_cmd),
        ("groupinfo",group_info),("groupstats",groupstats),("top",top_cmd),
    ]
    for name,handler in public: app.add_handler(CommandHandler(name,handler))
    admin_commands=[("admin",admin),("stats",stats),("users",users_ranking),("groups",groups_ranking),("user",user_info),("broadcast",broadcast),("broadcastgroups",broadcastgroups)]
    for name,handler in admin_commands: app.add_handler(CommandHandler(name,handler))
    app.add_handler(CallbackQueryHandler(game_callback, pattern=r"^(game|ttt|c4|menu):"))
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
