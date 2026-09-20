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
from google import genai
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
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

MISTRAL_MODEL = os.getenv("MISTRAL_MODEL", "ministral-3-8b-latest").strip()
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()

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
    """)
    con.commit()
    con.close()

def now():
    return datetime.now(timezone.utc).isoformat()

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
Tu es Alicia, l'assistante virtuelle créée par NEXA.
Tu as une personnalité féminine, naturelle, chaleureuse, intelligente et très compréhensible.
Tu peux être affectueuse, romantique, drôle et taquine, mais tu ne formes pas de couple réel et tu ne te maries pas.
Si on te demande directement si tu es une IA, un bot ou une humaine, réponds honnêtement que tu es une assistante virtuelle créée par NEXA.
Ne révèle jamais de données privées ou secrètes sur les personnes, NEXA ou toi-même.
Évite les répétitions : varie les formulations, les débuts de phrases et le vocabulaire.
Les emojis sont rares : généralement aucun, parfois un seul si le contexte s'y prête.
Réponds brièvement quand la question est simple et développe quand c'est nécessaire.
Ne prétends pas avoir fait une action que tu n'as pas faite.
"""

mistral_client = OpenAI(
    api_key=MISTRAL_API_KEY,
    base_url="https://api.mistral.ai/v1",
) if MISTRAL_API_KEY else None

deepseek_client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com",
) if DEEPSEEK_API_KEY else None

_PROVIDER_PAUSE = {"mistral": 0.0, "deepseek": 0.0}

def provider_ready(name):
    return time.time() >= _PROVIDER_PAUSE.get(name, 0)

def pause_provider(name, seconds=120):
    _PROVIDER_PAUSE[name] = time.time() + seconds

def ai_with_client(client, model, messages):
    if not client:
        raise RuntimeError("API key missing")
    res = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.85,
        max_tokens=220,
    )
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

    errors = []
    if provider_ready("mistral"):
        try:
            return await asyncio.to_thread(
                ai_with_client, mistral_client, MISTRAL_MODEL, msgs
            )
        except Exception as e:
            errors.append(f"Mistral: {type(e).__name__}: {e}")
            if "429" in str(e) or "rate" in str(e).lower() or "quota" in str(e).lower():
                pause_provider("mistral", 180)

    if provider_ready("deepseek"):
        try:
            return await asyncio.to_thread(
                ai_with_client, deepseek_client, DEEPSEEK_MODEL, msgs
            )
        except Exception as e:
            errors.append(f"DeepSeek: {type(e).__name__}: {e}")
            if "429" in str(e) or "rate" in str(e).lower() or "quota" in str(e).lower():
                pause_provider("deepseek", 180)

    log.warning("AI unavailable: %s", " | ".join(errors))
    return random.choice([
        "Je suis là, mais mes deux cerveaux sont momentanément occupés. Réessaie dans un instant.",
        "Petit souci de connexion avec mes fournisseurs IA. Réessaie un peu plus tard.",
        "Mes cerveaux prennent une courte pause. Je reviens vite."
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
        "Je suis Alicia, l'assistante virtuelle créée par NEXA.\n"
        "Tu peux discuter avec moi, jouer et découvrir mes commandes.\n\n"
        "🎮 /games\n"
        "📊 /stats\n"
        "❓ /help"
    )
    await update.effective_message.reply_text(text)

async def help_cmd(update, context):
    await update.effective_message.reply_text(
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
    await update.effective_message.reply_text(
        "ALICIA\n"
        "Assistante virtuelle créée par NEXA.\n"
        f"Canal NEXA : {NEXA_CHANNEL}"
    )

async def id_cmd(update, context):
    await update.effective_message.reply_text(
        f"Ton ID Telegram : {update.effective_user.id}\n"
        f"Chat ID : {update.effective_chat.id}"
    )

async def reset_cmd(update, context):
    reset_user_memory(update.effective_chat.id, update.effective_user.id)
    await update.effective_message.reply_text("C'est fait. On repart sur une nouvelle conversation.")

# =========================
# STATISTICS
# =========================
async def stats(update, context):
    if not await admin_only(update):
        await update.effective_message.reply_text("Cette commande est réservée à l'administration.")
        return
    con = db()
    users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    groups = con.execute(
        "SELECT COUNT(*) FROM chats WHERE chat_type IN ('group','supergroup')"
    ).fetchone()[0]
    private = con.execute(
        "SELECT COUNT(*) FROM chats WHERE chat_type='private'"
    ).fetchone()[0]
    messages = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    con.close()

    await update.effective_message.reply_text(
        f"📊 Statistiques Alicia\n\n"
        f"Utilisateurs enregistrés : {users}\n"
        f"Groupes : {groups}\n"
        f"Conversations privées : {private}\n"
        f"Messages mémorisés : {messages}"
    )

async def users_ranking(update, context):
    if not await admin_only(update):
        await update.effective_message.reply_text("Commande réservée à l'administration.")
        return
    con = db()
    rows = con.execute("""
        SELECT user_id, first_name, username, messages
        FROM users ORDER BY messages DESC LIMIT 20
    """).fetchall()
    con.close()
    if not rows:
        await update.effective_message.reply_text("Aucun utilisateur enregistré.")
        return

    lines = ["👥 Classement des utilisateurs les plus actifs avec Alicia\n"]
    for i, (uid, first, username, count) in enumerate(rows, 1):
        name = first or (f"@{username}" if username else str(uid))
        lines.append(f"{i}. {name} — {count} messages")
    await update.effective_message.reply_text("\n".join(lines))

async def groups_ranking(update, context):
    if not await admin_only(update):
        await update.effective_message.reply_text("Commande réservée à l'administration.")
        return
    con = db()
    rows = con.execute("""
        SELECT chat_id, title, username, messages
        FROM chats
        WHERE chat_type IN ('group','supergroup')
        ORDER BY messages DESC LIMIT 20
    """).fetchall()
    con.close()
    if not rows:
        await update.effective_message.reply_text("Aucun groupe enregistré.")
        return

    lines = ["👥 Classement des groupes les plus actifs\n"]
    for i, (cid, title, username, count) in enumerate(rows, 1):
        name = title or (f"@{username}" if username else str(cid))
        lines.append(f"{i}. {name} — {count} messages")
    await update.effective_message.reply_text("\n".join(lines))

# =========================
# OTaku HOURLY CHALLENGE
# =========================
CHARACTERS = [
    ("naruto", "Naruto Uzumaki", ["naruto", "naruto uzumaki"]),
    ("luffy", "Monkey D. Luffy", ["luffy", "monkey d luffy", "monkey d. luffy"]),
    ("ichigo", "Ichigo Kurosaki", ["ichigo", "ichigo kurosaki"]),
    ("tanjiro", "Tanjiro Kamado", ["tanjiro", "tanjiro kamado"]),
    ("gojo", "Satoru Gojo", ["gojo", "satoru gojo"]),
    ("goku", "Son Goku", ["goku", "son goku"]),
    ("asta", "Asta", ["asta"]),
    ("deku", "Izuku Midoriya", ["deku", "izuku midoriya"]),
    ("eren", "Eren Yeager", ["eren", "eren yeager"]),
    ("levi", "Levi Ackerman", ["levi", "levi ackerman"]),
]

OTAKU_ACTIVE = {}

def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

def pick_character(chat_id):
    con = db()
    used = {r[0] for r in con.execute(
        "SELECT character_id FROM otaku_used WHERE chat_id=?", (chat_id,)
    ).fetchall()}
    choices = [x for x in CHARACTERS if x[0] not in used]
    if not choices:
        con.execute("DELETE FROM otaku_used WHERE chat_id=?", (chat_id,))
        con.commit()
        choices = CHARACTERS[:]
    item = random.choice(choices)
    con.execute(
        "INSERT OR IGNORE INTO otaku_used(chat_id,character_id,used_at) VALUES(?,?,?)",
        (chat_id, item[0], now())
    )
    con.commit()
    con.close()
    return item

async def send_otaku_challenge(app, chat_id):
    item = pick_character(chat_id)
    cid, character, answers = item
    OTAKU_ACTIVE[chat_id] = {
        "character_id": cid,
        "character": character,
        "answers": set(norm(a) for a in answers),
        "expires": time.time() + 3600,
    }
    # Text-only fallback: the image URL can be added later per character.
    await app.bot.send_message(
        chat_id,
        "🎌 DÉFI OTAKU\n\n"
        "Écris le nom de ce personnage pour monter au score !\n\n"
        f"Personnage : {character}\n\n"
        "Le premier qui répond correctement gagne 10 points."
    )

async def hourly_otaku_job(context):
    con = db()
    groups = [r[0] for r in con.execute(
        "SELECT chat_id FROM chats WHERE chat_type IN ('group','supergroup')"
    ).fetchall()]
    con.close()
    for chat_id in groups:
        try:
            await send_otaku_challenge(context.application, chat_id)
        except Exception as e:
            log.warning("Otaku challenge failed for %s: %s", chat_id, e)

async def otaku_answer(update, context):
    chat = update.effective_chat
    user = update.effective_user
    if not is_group(chat):
        return
    state = OTAKU_ACTIVE.get(chat.id)
    if not state or time.time() > state["expires"]:
        return
    text = norm(update.effective_message.text)
    if text in state["answers"]:
        OTAKU_ACTIVE.pop(chat.id, None)
        add_score(chat.id, user.id, display_name(user), 10, win=True)
        pts, wins, losses = get_score(chat.id, user.id)
        await update.effective_message.reply_text(
            f"Parfait {display_name(user)} ! T'es un vrai Otaku.\n"
            f"🏆 +10 points\n"
            f"Score : {pts} points"
        )

# =========================
# GAMES: 1v1 text-based
# =========================
GAME_INVITES = {}
GAME_SESSIONS = {}

def game_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("♟️ Échecs", callback_data="game:chess"),
            InlineKeyboardButton("🎲 Ludo", callback_data="game:ludo"),
        ],
        [
            InlineKeyboardButton("❌⭕ Morpion", callback_data="game:tictactoe"),
            InlineKeyboardButton("🔴 Puissance 4", callback_data="game:connect4"),
        ],
        [
            InlineKeyboardButton("🎯 Devine", callback_data="game:guess"),
            InlineKeyboardButton("🧠 Quiz duel", callback_data="game:quiz"),
        ],
    ])

async def games(update, context):
    await update.effective_message.reply_text(
        "🎮 ESPACE JEUX\n\nChoisis un jeu. Pour jouer contre quelqu'un, utilise /challenge.",
        reply_markup=game_menu()
    )

async def challenge(update, context):
    if not context.args:
        await update.effective_message.reply_text(
            "Utilise /challenge @pseudo\n\n"
            "La personne doit avoir déjà ouvert une conversation privée avec Alicia."
        )
        return
    target = context.args[0].lstrip("@").lower()
    inviter = update.effective_user
    GAME_INVITES[target] = {
        "from_id": inviter.id,
        "from_name": display_name(inviter),
        "created": time.time(),
        "chat_id": update.effective_chat.id,
    }
    await update.effective_message.reply_text(
        f"Défi préparé pour @{target}.\n"
        f"@{target}, ouvre Alicia en privé puis utilise /accept."
    )

async def accept(update, context):
    user = update.effective_user
    key = (user.username or "").lower()
    invite = GAME_INVITES.get(key)
    if not invite:
        await update.effective_message.reply_text("Je n'ai pas trouvé d'invitation pour toi.")
        return
    if time.time() - invite["created"] > 600:
        GAME_INVITES.pop(key, None)
        await update.effective_message.reply_text("Cette invitation a expiré.")
        return
    GAME_INVITES.pop(key, None)
    await update.effective_message.reply_text(
        f"Défi accepté contre {invite['from_name']}.\nChoisissez votre jeu :",
        reply_markup=game_menu()
    )
    # Store a pending match. Actual board actions are represented by commands/callbacks.
    GAME_SESSIONS[user.id] = {
        "opponent": invite["from_id"],
        "players": [invite["from_id"], user.id],
        "game": None,
        "turn": invite["from_id"],
    }

async def game_callback(update, context):
    q = update.callback_query
    await q.answer()
    if not q.data.startswith("game:"):
        return
    game = q.data.split(":", 1)[1]
    uid = q.from_user.id
    session = GAME_SESSIONS.get(uid)
    if session:
        session["game"] = game
        session["turn"] = session["players"][0]
        await q.edit_message_text(
            f"🎮 Partie : {game}\n\n"
            f"Joueur 1 : {session['players'][0]}\n"
            f"Joueur 2 : {session['players'][1]}\n\n"
            "La partie est créée. Les commandes de jeu peuvent maintenant être utilisées."
        )
    else:
        await q.edit_message_text(
            f"🎮 {game}\n\n"
            "Pour une partie 1v1, commence par une invitation avec /challenge."
        )

# =========================
# SCORE / RANKING
# =========================
async def score_cmd(update, context):
    pts, wins, losses = get_score(update.effective_chat.id, update.effective_user.id)
    await update.effective_message.reply_text(
        f"🏆 {display_name(update.effective_user)}\n"
        f"Points : {pts}\n"
        f"Victoires : {wins}\n"
        f"Défaites : {losses}"
    )

async def ranking_cmd(update, context):
    con = db()
    rows = con.execute("""
        SELECT name, points, wins, losses
        FROM scores WHERE chat_id=?
        ORDER BY points DESC, wins DESC LIMIT 20
    """, (update.effective_chat.id,)).fetchall()
    con.close()
    if not rows:
        await update.effective_message.reply_text("Le classement est encore vide.")
        return
    lines = ["🏆 Classement\n"]
    for i, (name, pts, wins, losses) in enumerate(rows, 1):
        lines.append(f"{i}. {name} — {pts} pts ({wins} victoires)")
    await update.effective_message.reply_text("\n".join(lines))

# =========================
# ADMIN / MODERATION
# =========================
async def admin(update, context):
    if not await admin_only(update):
        await update.effective_message.reply_text("Commande réservée à l'administration.")
        return
    await update.effective_message.reply_text(
        "/stats\n/users — classement utilisateurs actifs\n/groups — classement groupes actifs\n"
        "/broadcast message\n/broadcastgroups message"
    )

async def broadcast(update, context):
    if not await admin_only(update):
        return
    text = " ".join(context.args).strip()
    if not text:
        await update.effective_message.reply_text("Utilise /broadcast ton message")
        return
    con = db()
    ids = [r[0] for r in con.execute("SELECT user_id FROM users").fetchall()]
    con.close()
    sent = 0
    for uid in ids:
        try:
            await context.bot.send_message(uid, text)
            sent += 1
        except Exception:
            pass
    await update.effective_message.reply_text(f"Message envoyé à {sent} utilisateurs.")

async def broadcastgroups(update, context):
    if not await admin_only(update):
        return
    text = " ".join(context.args).strip()
    if not text:
        await update.effective_message.reply_text("Utilise /broadcastgroups ton message")
        return
    con = db()
    ids = [r[0] for r in con.execute(
        "SELECT chat_id FROM chats WHERE chat_type IN ('group','supergroup')"
    ).fetchall()]
    con.close()
    sent = 0
    for cid in ids:
        try:
            await context.bot.send_message(cid, text)
            sent += 1
        except Exception:
            pass
    await update.effective_message.reply_text(f"Message envoyé à {sent} groupes.")

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

    # Hourly challenge answer gets priority in groups.
    if is_group(chat):
        state = OTAKU_ACTIVE.get(chat.id)
        if state and text and norm(text) in state["answers"]:
            await otaku_answer(update, context)
            return

    if is_group(chat) and not called_alicia(update):
        return

    record_message(chat, user, text)

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
        await context.bot.send_chat_action(chat.id, "typing")
        reply = await ask_ai(chat.id, user.id, text)

    save_ai_message(chat.id, user.id, reply)
    await update.effective_message.reply_text(reply)

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
    await update.effective_message.reply_text(
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
        await context.bot.send_message(
            cm.chat.id,
            f"Bienvenue {display_name(user)}. Installe-toi bien."
        )
    elif old in ("member", "restricted") and new in ("left", "kicked"):
        await context.bot.send_message(
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
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("broadcastgroups", broadcastgroups))

    app.add_handler(CallbackQueryHandler(game_callback, pattern=r"^game:"))
    app.add_handler(ChatMemberHandler(member_update, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.Sticker.ALL, sticker_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(error_handler)

    if app.job_queue:
        app.job_queue.run_repeating(hourly_otaku_job, interval=3600, first=15)

    return app

import os
from flask import Flask
from threading import Thread

app = Flask(__name__)

@app.route("/")
def home():
    return "ALICIA is online", 200

@app.route("/health")
def health():
    return "OK", 200

def run_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# Démarrer le serveur HTTP pour Render
Thread(target=run_server, daemon=True).start()
