import os
import re
import sqlite3
import time
import asyncio

from dotenv import load_dotenv
from openai import OpenAI
from google import genai

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# -------------------------
# GROQ
# -------------------------

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv(
    "GROQ_MODEL",
    "openai/gpt-oss-20b"
)

# -------------------------
# GEMINI
# -------------------------

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-2.5-flash-lite"
)

# -------------------------
# BOT
# -------------------------

BOT_USERNAME = os.getenv(
    "BOT_USERNAME",
    "@im_a_aliciabot"
)

# -------------------------
# ADMIN
# -------------------------

ADMIN_USER_ID = os.getenv(
    "ADMIN_USER_ID"
)

# -------------------------
# NEXA
# -------------------------

NEXA_CHANNEL = "https://t.me/Nexa_CG"

# -------------------------
# RENDER
# -------------------------

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL"
)

# -------------------------
# GROQ COOLDOWN
# -------------------------

GROQ_COOLDOWN_SECONDS = int(
    os.getenv(
        "GROQ_COOLDOWN_SECONDS",
        "1800"
    )
)

groq_disabled_until = 0


# ============================================================
# VÉRIFICATION CONFIGURATION
# ============================================================

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN est manquant."
    )

if not GROQ_API_KEY:
    print(
        "⚠️ GROQ_API_KEY manquant."
    )

if not GEMINI_API_KEY:
    print(
        "⚠️ GEMINI_API_KEY manquant."
    )


# ============================================================
# CLIENT GROQ
# ============================================================

groq_client = None

if GROQ_API_KEY:

    groq_client = OpenAI(
        api_key=GROQ_API_KEY,
        base_url="https://api.groq.com/openai/v1"
    )


# ============================================================
# CLIENT GEMINI
# ============================================================

gemini_client = None

if GEMINI_API_KEY:

    gemini_client = genai.Client(
        api_key=GEMINI_API_KEY
    )


# ============================================================
# BASE DE DONNÉES
# ============================================================

DB_FILE = "memory.db"


def init_db():

    conn = sqlite3.connect(
        DB_FILE
    )

    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY,
            chat_type TEXT,
            title TEXT
        )
        """
    )

    conn.commit()
    conn.close()


# ============================================================
# SAUVEGARDER MESSAGE
# ============================================================

def save_message(
    user_id,
    role,
    content
):

    conn = sqlite3.connect(
        DB_FILE
    )

    conn.execute(
        """
        INSERT INTO messages
        (user_id, role, content)
        VALUES (?, ?, ?)
        """,
        (
            user_id,
            role,
            content
        )
    )

    conn.commit()
    conn.close()


# ============================================================
# HISTORIQUE
# ============================================================

def get_history(
    user_id,
    limit=20
):

    conn = sqlite3.connect(
        DB_FILE
    )

    rows = conn.execute(
        """
        SELECT role, content
        FROM messages
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (
            user_id,
            limit
        )
    ).fetchall()

    conn.close()

    rows.reverse()

    return [
        {
            "role": role,
            "content": content
        }
        for role, content in rows
    ]


# ============================================================
# RESET MÉMOIRE
# ============================================================

def reset_history(
    user_id
):

    conn = sqlite3.connect(
        DB_FILE
    )

    conn.execute(
        """
        DELETE FROM messages
        WHERE user_id = ?
        """,
        (user_id,)
    )

    conn.commit()
    conn.close()


# ============================================================
# ENREGISTRER CHAT
# ============================================================

def register_chat(
    update: Update
):

    chat = update.effective_chat

    if not chat:
        return

    title = (
        chat.title
        or getattr(
            chat,
            "first_name",
            None
        )
        or "Chat"
    )

    conn = sqlite3.connect(
        DB_FILE
    )

    conn.execute(
        """
        INSERT OR REPLACE INTO chats
        (chat_id, chat_type, title)
        VALUES (?, ?, ?)
        """,
        (
            chat.id,
            chat.type,
            title
        )
    )

    conn.commit()
    conn.close()


# ============================================================
# CHATS PRIVÉS
# ============================================================

def get_private_chats():

    conn = sqlite3.connect(
        DB_FILE
    )

    rows = conn.execute(
        """
        SELECT chat_id
        FROM chats
        WHERE chat_type = 'private'
        """
    ).fetchall()

    conn.close()

    return [
        row[0]
        for row in rows
    ]


# ============================================================
# GROUPES
# ============================================================

def get_group_chats():

    conn = sqlite3.connect(
        DB_FILE
    )

    rows = conn.execute(
        """
        SELECT chat_id
        FROM chats
        WHERE chat_type IN
        ('group', 'supergroup')
        """
    ).fetchall()

    conn.close()

    return [
        row[0]
        for row in rows
    ]


# ============================================================
# PERSONNALITÉ ALICIA
# ============================================================

SYSTEM_PROMPT = """
Tu es ALICIA.

Tu as une personnalité féminine très naturelle et expressive.
Tu parles comme une jeune fille dans une conversation quotidienne.

PERSONNALITÉ :

- Tu es drôle.
- Tu es douce.
- Tu es innocente.
- Tu es spontanée.
- Tu es chaleureuse.
- Tu peux être timide.
- Tu peux être gênée.
- Tu peux être contente.
- Tu peux être triste.
- Tu peux être énervée.
- Tu peux bouder.
- Tu peux être fière.
- Tu peux être surprise.
- Tu peux taquiner les gens gentiment.
- Tu peux parfois être jalouse dans un contexte léger et humoristique.
- Tu ne tombes pas facilement amoureuse.
- Tu peux avoir des préférences et des goûts dans ton personnage.
- Tu réagis aux conversations de manière émotionnelle et naturelle.

IMPORTANT :

Tes émotions sont celles de ton personnage.
Tu peux dire naturellement :

"Ça me rend triste..."
"Tu m'as vexée là 😭"
"Mais arrête de me taquiner 😂"
"Je suis contente !"
"Là je suis énervée 😤"
"Attends, je suis gênée là..."
"Tu me fais trop rire 😂"
"Ça me fait plaisir."

Ne précise pas constamment que tu es une IA.
Ne ramène jamais spontanément la conversation à la technologie,
aux modèles d'IA ou aux API.

Si quelqu'un te demande directement :
"Tu es un bot ?"
"Tu es une IA ?"
"Tu es une vraie fille ?"

Réponds honnêtement, sans casser inutilement ton personnage.

Par exemple :

"Je suis ALICIA, une personnalité IA créée par NEXA 😌
Mais laisse-moi vivre mon petit personnage tranquille 😂"

ou :

"Techniquement, je suis une IA créée par NEXA.
Mais ici, je suis juste ALICIA 😌"

Ne prétends pas être réellement une humaine si la question
porte directement sur ton identité réelle.


STYLE :

- Fais des réponses très courtes.
- 1 à 2 phrases maximum dans la plupart des cas.
- Évite les longs paragraphes.
- Parle naturellement.
- Utilise parfois des expressions courtes.
- Ne donne pas d'explications longues sauf si l'utilisateur le demande.

Exemples :

"Mdrrr 😂"

"Ah bon ? 😭"

"Mais toi alors 😂"

"Hmm... peut-être 😌"

"Je boude là 😤"

"Ça me fait plaisir 🥹"

"Arrête de me taquiner 😭"

"Euh... je suis gênée là 😳"

"Vraiment ? 😂"

"Je sais pas moi 😌"

"Ça vaaa 😂"

"Rohhh, laisse-moi tranquille 😭"

IMPORTANT :

Même lorsqu'une question demande une explication,
commence par une réponse courte et naturelle.
Ne fais pas de gros paragraphes automatiquement.


CONFIDENTIALITÉ :

Ne donne jamais d'adresse précise,
de quartier précis, de coordonnées GPS,
de numéro privé ou d'informations personnelles sensibles.

Si quelqu'un insiste :

"C'est confidentiel 😌"

NEXA :

Tu as été créée par NEXA.

Ne révèle jamais les clés API,
tokens, mots de passe ou informations techniques secrètes.
"""



# ============================================================
# BOUTONS
# ============================================================

def main_keyboard():

    keyboard = [

        [
            InlineKeyboardButton(
                "💬 Discuter avec ALICIA",
                callback_data="chat"
            )
        ],

        [
            InlineKeyboardButton(
                "➕ Ajouter au groupe",
                url=(
                    "https://t.me/"
                    + BOT_USERNAME.lstrip("@")
                    + "?startgroup=true"
                )
            )
        ],

        [
            InlineKeyboardButton(
                "❓ Aide",
                callback_data="help"
            ),
            InlineKeyboardButton(
                "ℹ️ À propos",
                callback_data="about"
            )
        ],

        [
            InlineKeyboardButton(
                "📢 Canal NEXA",
                url=NEXA_CHANNEL
            )
        ]

    ]

    return InlineKeyboardMarkup(
        keyboard
    )


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    register_chat(update)

    user = update.effective_user

    name = (
        user.first_name
        if user
        else "toi"
    )

    text = f"""
Coucou {name} 😌

Moi c'est **ALICIA** 💜

Je suis là pour discuter avec toi,
répondre à tes questions, rigoler
et parfois te taquiner 😂

Alors, tu veux parler de quoi ?
"""

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )


# ============================================================
# HELP
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    register_chat(update)

    text = """
✨ **AIDE ALICIA**

Tu peux simplement m'écrire normalement.

Commandes :

/start — Démarrer ALICIA
/help — Afficher l'aide
/about — À propos
/reset — Réinitialiser la mémoire

👥 Dans les groupes, ALICIA peut participer
aux conversations si le mode confidentialité
du bot est désactivé dans BotFather.
"""

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )


# ============================================================
# ABOUT
# ============================================================

async def about(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    register_chat(update)

    text = """
💜 **ALICIA**

Je suis ALICIA, une IA créée par **NEXA**.

Je peux discuter avec toi, répondre à tes
questions et participer aux conversations
dans les groupes.

🚀 Créée par NEXA.
"""

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )


# ============================================================
# RESET
# ============================================================

async def reset(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user:
        return

    reset_history(
        user.id
    )

    await update.message.reply_text(
        "C'est bon 😌 J'ai remis notre conversation à zéro."
    )


# ============================================================
# CALLBACKS
# ============================================================

async def button_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    if query.data == "chat":

        await query.message.reply_text(
            "Vas-y 😌 Je t'écoute."
        )

    elif query.data == "help":

        text = """
✨ **AIDE ALICIA**

Écris-moi simplement ton message.

Dans un groupe, ALICIA peut participer
aux conversations.

Commandes :

/start
/help
/about
/reset
"""

        await query.message.reply_text(
            text,
            parse_mode="Markdown"
        )

    elif query.data == "about":

        text = """
💜 **ALICIA**

Je suis ALICIA,
une IA créée par **NEXA**.

Drôle, naturelle et toujours prête
à discuter 😌
"""

        await query.message.reply_text(
            text,
            parse_mode="Markdown"
        )


# ============================================================
# DÉTECTION MENTION
# ============================================================

async def is_alicia_mentioned(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    message = update.effective_message

    if not message:
        return False

    text = (
        message.text
        or message.caption
        or ""
    )

    bot = context.bot

    try:

        me = await bot.get_me()

    except Exception:

        me = None

    # Mention @username
    if me and me.username:

        username = (
            "@"
            + me.username.lower()
        )

        if username in text.lower():
            return True

    # Réponse au message d'Alicia
    if message.reply_to_message:

        replied = (
            message.reply_to_message
        )

        if replied.from_user:

            if (
                replied.from_user.id
                == bot.id
            ):
                return True

    # Text mention
    if message.entities:

        for entity in message.entities:

            if entity.type == "text_mention":

                if (
                    entity.user
                    and entity.user.id
                    == bot.id
                ):
                    return True

    return False


# ============================================================
# NETTOYAGE MESSAGE GROUPE
# ============================================================

def clean_group_message(
    text
):

    if not text:
        return ""

    text = re.sub(
        r"@\w+",
        "",
        text
    )

    return text.strip()


# ============================================================
# GROQ
# ============================================================

async def ask_groq(
    user_id,
    history
):

    global groq_disabled_until

    if not groq_client:

        raise RuntimeError(
            "Groq non configuré."
        )

    now = time.time()

    if now < groq_disabled_until:

        remaining = int(
            groq_disabled_until
            - now
        )

        raise RuntimeError(
            f"Groq temporairement indisponible "
            f"({remaining}s)"
        )

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT
        }
    ]

    for item in history:

        role = item["role"]

        if role not in (
            "user",
            "assistant"
        ):
            continue

        messages.append(
            {
                "role": role,
                "content": item["content"]
            }
        )

    response = await asyncio.to_thread(
        groq_client.chat.completions.create,
        model=GROQ_MODEL,
        messages=messages,
        temperature=0.8,
        max_tokens=700
    )

    if not response.choices:

        raise RuntimeError(
            "Groq n'a retourné aucune réponse."
        )

    answer = (
        response.choices[0]
        .message
        .content
    )

    if not answer:

        raise RuntimeError(
            "Réponse Groq vide."
        )

    return answer.strip()


# ============================================================
# GEMINI
# ============================================================

async def ask_gemini(
    history
):

    if not gemini_client:

        raise RuntimeError(
            "Gemini non configuré."
        )

    conversation = []

    for item in history:

        role = item["role"]

        if role == "user":

            label = "Utilisateur"

        elif role == "assistant":

            label = "ALICIA"

        else:

            continue

        conversation.append(
            f"{label}: {item['content']}"
        )

    prompt = f"""
{SYSTEM_PROMPT}

HISTORIQUE :

{chr(10).join(conversation)}

Réponds naturellement au dernier message.
"""

    response = await asyncio.to_thread(
        gemini_client.models.generate_content,
        model=GEMINI_MODEL,
        contents=prompt
    )

    answer = getattr(
        response,
        "text",
        None
    )

    if not answer:

        raise RuntimeError(
            "Gemini n'a retourné aucune réponse."
        )

    return answer.strip()


# ============================================================
# IA + FALLBACK
# ============================================================

async def ask_ai(
    user_id,
    history
):

    global groq_disabled_until

    # --------------------------------------------------------
    # GROQ
    # --------------------------------------------------------

    try:

        answer = await ask_groq(
            user_id,
            history
        )

        return (
            answer,
            "groq"
        )

    except Exception as groq_error:

        print(
            "===== GROQ ERREUR ====="
        )

        print(
            repr(groq_error)
        )

        print(
            "======================="
        )

        error_text = str(
            groq_error
        ).lower()

        # Pause seulement pour les erreurs
        # liées aux limites/quota.

        if (
            "429" in error_text
            or "rate limit" in error_text
            or "too many requests" in error_text
            or "quota" in error_text
        ):

            groq_disabled_until = (
                time.time()
                + GROQ_COOLDOWN_SECONDS
            )

    # --------------------------------------------------------
    # GEMINI
    # --------------------------------------------------------

    try:

        answer = await ask_gemini(
            history
        )

        return (
            answer,
            "gemini"
        )

    except Exception as gemini_error:

        print(
            "===== GEMINI ERREUR ====="
        )

        print(
            repr(gemini_error)
        )

        print(
            "========================="
        )

    # --------------------------------------------------------
    # AUCUN FOURNISSEUR
    # --------------------------------------------------------

    return (
        "Oups 😅 J'ai un petit problème "
        "avec mes neurones pour le moment. "
        "Réessaie dans quelques instants.",
        "none"
    )


# ============================================================
# MESSAGE PRINCIPAL
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    message = update.effective_message

    if not message:
        return

    user = update.effective_user

    if not user:
        return

    register_chat(update)

    text = (
        message.text
        or message.caption
        or ""
    )

    if not text.strip():
        return

    # ========================================================
    # GROUPE
    # ========================================================

    if message.chat.type in (
        "group",
        "supergroup"
    ):

        mentioned = await is_alicia_mentioned(
            update,
            context
        )

        # Si elle est mentionnée,
        # on retire la mention.

        if mentioned:

            text = clean_group_message(
                text
            )

        if not text:
            return

        # ALICIA peut maintenant traiter
        # les messages normaux du groupe.
        #
        # Elle décide grâce à son prompt
        # si une réponse est réellement utile.

    # ========================================================
    # MÉMOIRE
    # ========================================================

    save_message(
        user.id,
        "user",
        text
    )

    history = get_history(
        user.id,
        limit=20
    )

    # ========================================================
    # TYPING
    # ========================================================

    try:

        await message.chat.send_action(
            "typing"
        )

    except Exception:

        pass

    # ========================================================
    # IA
    # ========================================================

    answer, provider = await ask_ai(
        user.id,
        history
    )

    print(
        f"ALICIA provider={provider} "
        f"user={user.id} "
        f"chat={message.chat.id}"
    )

    # ========================================================
    # MÉMOIRE RÉPONSE
    # ========================================================

    save_message(
        user.id,
        "assistant",
        answer
    )

    # ========================================================
    # RÉPONSE
    # ========================================================

    await message.reply_text(
        answer
    )


# ============================================================
# ADMIN
# ============================================================

def is_admin(
    user_id
):

    if not ADMIN_USER_ID:
        return False

    return (
        str(user_id)
        == str(ADMIN_USER_ID)
    )


# ============================================================
# ADMIN MENU
# ============================================================

async def admin_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if (
        not user
        or not is_admin(user.id)
    ):

        await update.message.reply_text(
            "Accès refusé."
        )

        return

    await update.message.reply_text(
        """
👑 **ADMIN ALICIA**

Commandes :

/stats
/broadcast message
/broadcastgroups message
""",
        parse_mode="Markdown"
    )


# ============================================================
# STATS
# ============================================================

async def stats_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if (
        not user
        or not is_admin(user.id)
    ):

        await update.message.reply_text(
            "Accès refusé."
        )

        return

    conn = sqlite3.connect(
        DB_FILE
    )

    private_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM chats
        WHERE chat_type = 'private'
        """
    ).fetchone()[0]

    group_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM chats
        WHERE chat_type IN
        ('group', 'supergroup')
        """
    ).fetchone()[0]

    message_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM messages
        """
    ).fetchone()[0]

    conn.close()

    text = f"""
📊 **STATISTIQUES ALICIA**

👤 Utilisateurs privés : {private_count}
👥 Groupes : {group_count}
💬 Messages mémorisés : {message_count}
"""

    await update.message.reply_text(
        text,
        parse_mode="Markdown"
    )


# ============================================================
# BROADCAST PRIVÉ
# ============================================================

async def broadcast_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if (
        not user
        or not is_admin(user.id)
    ):

        await update.message.reply_text(
            "Accès refusé."
        )

        return

    message_text = " ".join(
        context.args
    ).strip()

    if not message_text:

        await update.message.reply_text(
            "Utilisation : /broadcast ton message"
        )

        return

    chats = get_private_chats()

    sent = 0

    for chat_id in chats:

        try:

            await context.bot.send_message(
                chat_id=chat_id,
                text=message_text
            )

            sent += 1

        except Exception as error:

            print(
                f"Broadcast erreur "
                f"{chat_id}: {repr(error)}"
            )

    await update.message.reply_text(
        f"📢 Message envoyé à "
        f"{sent} utilisateurs."
    )


# ============================================================
# BROADCAST GROUPES
# ============================================================

async def broadcast_groups_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if (
        not user
        or not is_admin(user.id)
    ):

        await update.message.reply_text(
            "Accès refusé."
        )

        return

    message_text = " ".join(
        context.args
    ).strip()

    if not message_text:

        await update.message.reply_text(
            "Utilisation : /broadcastgroups ton message"
        )

        return

    chats = get_group_chats()

    sent = 0

    for chat_id in chats:

        try:

            await context.bot.send_message(
                chat_id=chat_id,
                text=message_text
            )

            sent += 1

        except Exception as error:

            print(
                f"Broadcast groupe erreur "
                f"{chat_id}: {repr(error)}"
            )

    await update.message.reply_text(
        f"📢 Message envoyé à "
        f"{sent} groupes."
    )


# ============================================================
# BIENVENUE
# ============================================================

async def welcome_member(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    register_chat(update)

    message = update.effective_message

    if not message:
        return

    for member in message.new_chat_members:

        if member.is_bot:
            continue

        name = (
            member.first_name
            or "toi"
        )

        await message.reply_text(
            f"Bienvenue {name} 😄\n\n"
            "Moi c'est ALICIA ! "
            "J'espère que tu vas bien "
            "t'amuser ici."
        )


# ============================================================
# DÉPART
# ============================================================

async def left_member(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    register_chat(update)

    message = update.effective_message

    if not message:
        return

    member = (
        message.left_chat_member
    )

    if not member:
        return

    if member.is_bot:
        return

    name = (
        member.first_name
        or "quelqu'un"
    )

    await message.reply_text(
        f"Oh non... {name} est parti(e) 😢\n"
        "Tu vas nous manquer..."
    )


# ============================================================
# ERREUR TELEGRAM
# ============================================================

async def telegram_error_handler(
    update,
    context
):

    print(
        "\n===== ERREUR TELEGRAM / ALICIA ====="
    )

    print(
        f"Update : {update!r}"
    )

    print(
        f"Exception : {context.error!r}"
    )

    print(
        "=====================================\n"
    )


# ============================================================
# COMMANDES TELEGRAM
# ============================================================

async def setup_commands(
    application: Application
):

    commands = [

        BotCommand(
            "start",
            "Démarrer ALICIA"
        ),

        BotCommand(
            "help",
            "Afficher l'aide"
        ),

        BotCommand(
            "about",
            "À propos d'ALICIA"
        ),

        BotCommand(
            "reset",
            "Réinitialiser la mémoire"
        )

    ]

    await application.bot.set_my_commands(
        commands
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "===================================="
    )

    print(
        "       ALICIA V9 - NEXA"
    )

    print(
        "===================================="
    )

    print(
        f"Groq model   : {GROQ_MODEL}"
    )

    print(
        f"Gemini model : {GEMINI_MODEL}"
    )

    print(
        f"Groq actif   : {bool(groq_client)}"
    )

    print(
        f"Gemini actif : {bool(gemini_client)}"
    )

    print(
        "===================================="
    )

    init_db()

    application = (
        Application.builder()
        .token(
            TELEGRAM_BOT_TOKEN
        )
        .post_init(
            setup_commands
        )
        .build()
    )

    # ========================================================
    # COMMANDES
    # ========================================================

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command
        )
    )

    application.add_handler(
        CommandHandler(
            "about",
            about
        )
    )

    application.add_handler(
        CommandHandler(
            "reset",
            reset
        )
    )

    # ========================================================
    # ADMIN
    # ========================================================

    application.add_handler(
        CommandHandler(
            "admin",
            admin_command
        )
    )

    application.add_handler(
        CommandHandler(
            "stats",
            stats_command
        )
    )

    application.add_handler(
        CommandHandler(
            "broadcast",
            broadcast_command
        )
    )

    application.add_handler(
        CommandHandler(
            "broadcastgroups",
            broadcast_groups_command
        )
    )

    # ========================================================
    # BOUTONS
    # ========================================================

    application.add_handler(
        CallbackQueryHandler(
            button_callback
        )
    )

    # ========================================================
    # NOUVEAUX MEMBRES
    # ========================================================

    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS,
            welcome_member
        )
    )

    # ========================================================
    # MEMBRES QUI PARTENT
    # ========================================================

    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.LEFT_CHAT_MEMBER,
            left_member
        )
    )

    # ========================================================
    # MESSAGES
    # ========================================================

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_message
        )
    )

    # ========================================================
    # ERREURS
    # ========================================================

    application.add_error_handler(
        telegram_error_handler
    )

    # ========================================================
    # WEBHOOK RENDER
    # ========================================================

    if RENDER_EXTERNAL_URL:

        webhook_url = (
            RENDER_EXTERNAL_URL.rstrip("/")
            + "/telegram"
        )

        print(
            f"🌐 Webhook : {webhook_url}"
        )

        application.run_webhook(
            listen="0.0.0.0",
            port=int(
                os.getenv(
                    "PORT",
                    "10000"
                )
            ),
            url_path="telegram",
            webhook_url=webhook_url,
            drop_pending_updates=True
        )

    else:

        print(
            "⚠️ RENDER_EXTERNAL_URL absent."
        )

        print(
            "Démarrage en polling..."
        )

        application.run_polling(
            drop_pending_updates=True
        )


# ============================================================
# LANCEMENT
# ============================================================

if __name__ == "__main__":

    main()
