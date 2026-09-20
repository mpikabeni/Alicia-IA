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
# TELEGRAM SAFE SENDING / ANTI-FLOOD
# =========================
async def safe_send_message(bot, chat_id, text, **kwargs):
    """Send a message while respecting Telegram flood-control responses."""
    attempts = 0
    while attempts < 3:
        try:
            return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        except RetryAfter as e:
            attempts += 1
            wait_time = max(1, int(getattr(e, "retry_after", 1)) + 1)
            log.warning("Telegram flood control for %s: waiting %ss", chat_id, wait_time)
            await asyncio.sleep(wait_time)
    return await bot.send_message(chat_id=chat_id, text=text, **kwargs)


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
        "Assistante virtuelle créée par NEXA.\n"
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
        await safe_reply(update.effective_message, "Cette commande est réservée à l'administration.")
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

    await safe_reply(update.effective_message, 
        f"📊 Statistiques Alicia\n\n"
        f"Utilisateurs enregistrés : {users}\n"
        f"Groupes : {groups}\n"
        f"Conversations privées : {private}\n"
        f"Messages mémorisés : {messages}"
    )

async def users_ranking(update, context):
    if not await admin_only(update):
        await safe_reply(update.effective_message, "Commande réservée à l'administration.")
        return
    con = db()
    rows = con.execute("""
        SELECT user_id, first_name, username, messages
        FROM users ORDER BY messages DESC LIMIT 20
    """).fetchall()
    con.close()
    if not rows:
        await safe_reply(update.effective_message, "Aucun utilisateur enregistré.")
        return

    lines = ["👥 Classement des utilisateurs les plus actifs avec Alicia\n"]
    for i, (uid, first, username, count) in enumerate(rows, 1):
        name = first or (f"@{username}" if username else str(uid))
        lines.append(f"{i}. {name} — {count} messages")
    await safe_reply(update.effective_message, "\n".join(lines))

async def groups_ranking(update, context):
    if not await admin_only(update):
        await safe_reply(update.effective_message, "Commande réservée à l'administration.")
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
        await safe_reply(update.effective_message, "Aucun groupe enregistré.")
        return

    lines = ["👥 Classement des groupes les plus actifs\n"]
    for i, (cid, title, username, count) in enumerate(rows, 1):
        name = title or (f"@{username}" if username else str(cid))
        lines.append(f"{i}. {name} — {count} messages")
    await safe_reply(update.effective_message, "\n".join(lines))

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
    await safe_send_message(
        app.bot,
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
            await asyncio.sleep(1)
        except RetryAfter as e:
            wait_time = max(1, int(getattr(e, "retry_after", 1)) + 1)
            log.warning("Otaku flood control for %s: waiting %ss", chat_id, wait_time)
            await asyncio.sleep(wait_time)
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
        await safe_reply(update.effective_message, 
            f"Parfait {display_name(user)} ! T'es un vrai Otaku.\n"
            f"🏆 +10 points\n"
            f"Score : {pts} points"
        )

# =========================
# GAMES: ALICIA IA + 1v1
# =========================
GAME_INVITES = {}
GAME_SESSIONS = {}

def game_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 Jouer contre Alicia", callback_data="gameai:menu")],
        [InlineKeyboardButton("👤 Jouer contre un joueur", callback_data="gamepvp:menu")],
        [InlineKeyboardButton("🏆 Mon score", callback_data="gamescore:show")],
    ])

def game_choice_menu(mode):
    prefix = "gameai:" if mode == "ai" else "gamepvp:"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌⭕ Morpion", callback_data=prefix + "tictactoe")],
        [InlineKeyboardButton("🔴 Puissance 4", callback_data=prefix + "connect4")],
        [InlineKeyboardButton("🎯 Devine le nombre", callback_data=prefix + "guess")],
        [InlineKeyboardButton("🧠 Quiz duel", callback_data=prefix + "quiz")],
    ])

def ttt_board(board, game_id):
    rows = []
    for r in range(3):
        row = []
        for c in range(3):
            v = board[r * 3 + c]
            label = v if v != " " else str(r * 3 + c + 1)
            row.append(InlineKeyboardButton(label, callback_data=f"playttt:{game_id}:{r*3+c}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)

def connect4_board(board, game_id):
    rows = []
    for r in range(6):
        rows.append([
            InlineKeyboardButton(board[r][c] if board[r][c] != " " else "·",
                                 callback_data=f"playc4:{game_id}:{c}")
            for c in range(7)
        ])
    rows.append([
        InlineKeyboardButton(str(i + 1), callback_data=f"dropc4:{game_id}:{i}")
        for i in range(7)
    ])
    return InlineKeyboardMarkup(rows)

def new_game(game, mode, user_id, opponent_id=None):
    gid = f"{user_id}_{int(time.time()*1000)}"
    state = {
        "id": gid, "game": game, "mode": mode,
        "players": [user_id] + ([opponent_id] if opponent_id else []),
        "turn": user_id, "created": time.time(),
    }
    if game == "tictactoe":
        state["board"] = [" "] * 9
        state["symbols"] = {user_id: "X", 0: "O"}
        if opponent_id:
            state["symbols"][opponent_id] = "O"
    elif game == "connect4":
        state["board"] = [[" "] * 7 for _ in range(6)]
        state["symbols"] = {user_id: "🔴", 0: "🟡"}
        if opponent_id:
            state["symbols"][opponent_id] = "🟡"
    elif game == "guess":
        state["number"] = random.randint(1, 100)
        state["tries"] = 0
    elif game == "quiz":
        state["quiz"] = random.choice([
            ("Quel est le village de Naruto ?", ["konoha", "konoha village"]),
            ("Comment s'appelle le capitaine du Chapeau de paille ?", ["luffy", "monkey d luffy"]),
            ("Comment s'appelle le frère de Sasuke ?", ["itachi", "itachi uchiha"]),
            ("Comment s'appelle le héros de Bleach ?", ["ichigo", "ichigo kurosaki"]),
        ])
    GAME_SESSIONS[gid] = state
    return state

def ttt_winner(board):
    for a,b,c in ((0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)):
        if board[a] != " " and board[a] == board[b] == board[c]:
            return board[a]
    return "draw" if " " not in board else None

def c4_winner(board, symbol):
    for r in range(6):
        for c in range(7):
            if board[r][c] != symbol:
                continue
            for dr, dc in ((1,0),(0,1),(1,1),(1,-1)):
                cells = []
                for k in range(4):
                    rr, cc = r + dr*k, c + dc*k
                    if 0 <= rr < 6 and 0 <= cc < 7:
                        cells.append(board[rr][cc])
                if len(cells) == 4 and all(x == symbol for x in cells):
                    return True
    return False

def c4_drop(board, col, symbol):
    for r in range(5, -1, -1):
        if board[r][col] == " ":
            board[r][col] = symbol
            return r
    return None

def ai_ttt_move(board):
    empty = [i for i, v in enumerate(board) if v == " "]
    for i in empty:
        b = board[:]; b[i] = "O"
        if ttt_winner(b) == "O": return i
    for i in empty:
        b = board[:]; b[i] = "X"
        if ttt_winner(b) == "X": return i
    if 4 in empty: return 4
    corners = [i for i in (0,2,6,8) if i in empty]
    return random.choice(corners or empty) if empty else None

async def games(update, context):
    await safe_reply(update.effective_message,
        "🎮 ESPACE JEUX\n\nChoisis ton adversaire :",
        reply_markup=game_menu())

async def challenge(update, context):
    if not context.args:
        await safe_reply(update.effective_message, "Utilise /challenge @pseudo")
        return
    target = context.args[0].lstrip("@").lower()
    inviter = update.effective_user
    GAME_INVITES[target] = {"from_id": inviter.id, "from_name": display_name(inviter),
                            "created": time.time()}
    await safe_reply(update.effective_message,
        f"Défi envoyé à @{target}. Il doit ouvrir Alicia en privé puis faire /accept.")

async def accept(update, context):
    user = update.effective_user
    key = (user.username or "").lower()
    invite = GAME_INVITES.get(key)
    if not invite:
        await safe_reply(update.effective_message, "Je n'ai pas trouvé d'invitation pour toi.")
        return
    if time.time() - invite["created"] > 600:
        GAME_INVITES.pop(key, None)
        await safe_reply(update.effective_message, "Cette invitation a expiré.")
        return
    GAME_INVITES.pop(key, None)
    GAME_SESSIONS[f"match_{user.id}"] = {"pending_players": [invite["from_id"], user.id]}
    await safe_reply(update.effective_message,
        f"Défi accepté contre {invite['from_name']}.\nChoisissez votre jeu :",
        reply_markup=game_choice_menu("pvp"))

async def game_callback(update, context):
    q = update.callback_query
    await q.answer()
    data, uid = q.data, q.from_user.id

    if data == "gameai:menu":
        await q.edit_message_text("🤖 Tu joues contre Alicia.\n\nChoisis un jeu :",
                                  reply_markup=game_choice_menu("ai"))
        return
    if data == "gamepvp:menu":
        await q.edit_message_text("👤 Pour jouer contre un autre joueur :\n/challenge @pseudo")
        return
    if data == "gamescore:show":
        pts, wins, losses = get_score(q.message.chat.id, uid)
        await q.edit_message_text(f"🏆 Ton score\n\nPoints : {pts}\nVictoires : {wins}\nDéfaites : {losses}")
        return

    if data.startswith("gameai:"):
        game = data.split(":",1)[1]
        if game not in ("tictactoe","connect4","guess","quiz"):
            await q.edit_message_text("Ce jeu sera ajouté prochainement.")
            return
        await render_game(q, new_game(game, "ai", uid))
        return

    if data.startswith("gamepvp:"):
        game = data.split(":",1)[1]
        key = next((k for k,v in GAME_SESSIONS.items()
                    if v.get("pending_players") and uid in v["pending_players"]), None)
        if not key:
            await q.edit_message_text("Aucune invitation 1v1 active.")
            return
        players = GAME_SESSIONS.pop(key)["pending_players"]
        await render_game(q, new_game(game, "pvp", players[0], players[1]))

async def render_game(q, state):
    if state["game"] == "tictactoe":
        await q.edit_message_text("❌⭕ MORPION\n\nÀ toi de jouer.",
                                  reply_markup=ttt_board(state["board"], state["id"]))
    elif state["game"] == "connect4":
        await q.edit_message_text("🔴 PUISSANCE 4\n\nÀ toi de jouer.",
                                  reply_markup=connect4_board(state["board"], state["id"]))
    elif state["game"] == "guess":
        await q.edit_message_text("🎯 DEVINE LE NOMBRE\n\nJ'ai choisi un nombre entre 1 et 100.\nÉcris ton nombre.")
    else:
        await q.edit_message_text(f"🧠 QUIZ DUEL\n\n{state['quiz'][0]}\n\nÉcris ta réponse.")

async def play_ttt(update, context):
    q = update.callback_query
    await q.answer()
    _, gid, pos = q.data.split(":")
    state = GAME_SESSIONS.get(gid)
    if not state:
        await q.answer("Partie terminée.", show_alert=True); return
    uid, pos = q.from_user.id, int(pos)
    if state["turn"] != uid or state["board"][pos] != " ":
        await q.answer("Ce n'est pas ton tour ou cette case est prise.", show_alert=True); return
    state["board"][pos] = state["symbols"].get(uid, "X")
    winner = ttt_winner(state["board"])
    if winner:
        await finish_game(q, state, uid if winner != "draw" else None, winner == "draw"); return

    if state["mode"] == "ai":
        ai = ai_ttt_move(state["board"])
        if ai is not None: state["board"][ai] = "O"
        winner = ttt_winner(state["board"])
        if winner:
            await finish_game(q, state, uid if winner == "X" else 0, winner == "draw"); return
    else:
        state["turn"] = state["players"][1] if uid == state["players"][0] else state["players"][0]

    await q.edit_message_text("❌⭕ MORPION\n\nÀ toi de jouer.",
                              reply_markup=ttt_board(state["board"], gid))

async def play_c4(update, context):
    q = update.callback_query
    await q.answer()
    _, gid, col = q.data.split(":")
    state = GAME_SESSIONS.get(gid)
    if not state:
        await q.answer("Partie terminée.", show_alert=True); return
    uid, col = q.from_user.id, int(col)
    if state["turn"] != uid:
        await q.answer("Ce n'est pas ton tour.", show_alert=True); return
    symbol = state["symbols"].get(uid, "🔴")
    if c4_drop(state["board"], col, symbol) is None:
        await q.answer("Colonne pleine.", show_alert=True); return
    if c4_winner(state["board"], symbol):
        await finish_game(q, state, uid); return

    if state["mode"] == "ai":
        cols = [c for c in range(7) if state["board"][0][c] == " "]
        if cols:
            ai_col = random.choice(cols); c4_drop(state["board"], ai_col, "🟡")
            if c4_winner(state["board"], "🟡"):
                await finish_game(q, state, 0); return
    else:
        state["turn"] = state["players"][1] if uid == state["players"][0] else state["players"][0]

    await q.edit_message_text("🔴 PUISSANCE 4\n\nÀ toi de jouer.",
                              reply_markup=connect4_board(state["board"], gid))

async def finish_game(q, state, winner_id, draw=False):
    chat_id = q.message.chat.id
    gid = state["id"]
    if draw:
        msg = "🤝 Match nul !"
    elif state["mode"] == "ai":
        uid = state["players"][0]
        if winner_id == 0:
            add_score(chat_id, uid, q.from_user.first_name or str(uid), 0, loss=True)
            msg = "Alicia gagne cette manche."
        else:
            add_score(chat_id, uid, q.from_user.first_name or str(uid), 10, win=True)
            msg = "Tu as gagné contre Alicia ! 🏆\n+10 points"
    else:
        loser = next((p for p in state["players"] if p != winner_id), None)
        add_score(chat_id, winner_id, str(winner_id), 10, win=True)
        if loser: add_score(chat_id, loser, str(loser), 0, loss=True)
        msg = f"Joueur {winner_id} gagne ! 🏆\n+10 points"
    GAME_SESSIONS.pop(gid, None)
    await q.edit_message_text(msg)

async def text_game_handler(update, context):
    user = update.effective_user
    text = (update.effective_message.text or "").strip()
    if not user or not text: return False
    for gid, state in list(GAME_SESSIONS.items()):
        if user.id not in state.get("players", []): continue
        if state["game"] == "guess":
            try: guess = int(text)
            except ValueError: continue
            state["tries"] += 1
            if guess == state["number"]:
                add_score(update.effective_chat.id, user.id, display_name(user), 10, win=True)
                GAME_SESSIONS.pop(gid, None)
                await safe_reply(update.effective_message, f"Bravo ! C'était {state['number']}. +10 points")
            elif guess < state["number"]:
                await safe_reply(update.effective_message, "Plus grand.")
            else:
                await safe_reply(update.effective_message, "Plus petit.")
            return True
        if state["game"] == "quiz":
            if norm(text) in [norm(a) for a in state["quiz"][1]]:
                add_score(update.effective_chat.id, user.id, display_name(user), 10, win=True)
                GAME_SESSIONS.pop(gid, None)
                await safe_reply(update.effective_message, "Bonne réponse ! +10 points")
            else:
                await safe_reply(update.effective_message, "Pas cette fois. Partie terminée.")
                GAME_SESSIONS.pop(gid, None)
            return True
    return False

# =========================
# SCORE / RANKING
# =========================
async def score_cmd(update, context):
    pts, wins, losses = get_score(update.effective_chat.id, update.effective_user.id)
    await safe_reply(update.effective_message, 
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
        await safe_reply(update.effective_message, "Le classement est encore vide.")
        return
    lines = ["🏆 Classement\n"]
    for i, (name, pts, wins, losses) in enumerate(rows, 1):
        lines.append(f"{i}. {name} — {pts} pts ({wins} victoires)")
    await safe_reply(update.effective_message, "\n".join(lines))

# =========================
# ADMIN / MODERATION
# =========================
async def admin(update, context):
    if not await admin_only(update):
        await safe_reply(update.effective_message, "Commande réservée à l'administration.")
        return
    await safe_reply(update.effective_message, 
        "/stats\n/users — classement utilisateurs actifs\n/groups — classement groupes actifs\n"
        "/broadcast message\n/broadcastgroups message"
    )

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

    # Active game answers get priority.
    if await text_game_handler(update, context):
        return

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
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("broadcastgroups", broadcastgroups))

    app.add_handler(CallbackQueryHandler(play_ttt, pattern=r"^playttt:"))
    app.add_handler(CallbackQueryHandler(play_c4, pattern=r"^playc4:|^dropc4:"))
    app.add_handler(CallbackQueryHandler(game_callback, pattern=r"^gameai:|^gamepvp:|^gamescore:"))
    app.add_handler(ChatMemberHandler(member_update, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.Sticker.ALL, sticker_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(error_handler)

    if app.job_queue:
        app.job_queue.run_repeating(hourly_otaku_job, interval=3600, first=15)
        log.info("Otaku hourly challenge enabled.")
    else:
        log.warning("JobQueue unavailable: install python-telegram-bot[job-queue].")

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
