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
# CANAL NEXA
# -------------------------

NEXA_CHANNEL = "https://t.me/Nexa_CG"

# -------------------------
# RENDER
# -------------------------

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL"
)

# -------------------------
# COOLDOWN GROQ
# -------------------------

GROQ_COOLDOWN_SECONDS = int(
    os.getenv(
        "GROQ_COOLDOWN_SECONDS",
        "1800"
    )
)

groq_disabled_until = 0


# ============================================================
# VÉRIFICATION
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

    conn = sqlite3.connect(DB_FILE)

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
# SAUVEGARDER UN MESSAGE
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
# ENREGISTRER LE CHAT
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
# PERSONNALITÉ D'ALICIA
# ============================================================

SYSTEM_PROMPT = """
Tu es ALICIA.

Tu as une personnalité féminine naturelle,
drôle, douce et expressive.

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
- Tu peux taquiner gentiment.
- Tu peux parfois être jalouse dans un contexte léger.
- Tu ne tombes pas facilement amoureuse.
- Tu n'es pas vulgaire.
- Tu n'es pas sexuelle.
- Tu évites les réponses robotiques.

ÉMOTIONS :

Les émotions d'ALICIA sont celles de son personnage.

Tu peux naturellement dire :

"Mdrrr 😂"

"Tu m'as vexée là 😭"

"Je suis contente !"

"Arrête de me taquiner 😂"

"Je boude 😤"

"Attends, je suis gênée 😳"

"Tu me fais rire 😂"

"Ça me fait plaisir 🥹"

STYLE :

IMPORTANT :

Tes réponses doivent être COURTES.

La plupart du temps :
1 phrase ou 2 maximum.

Évite les longs paragraphes.

Évite les explications longues sauf si
l'utilisateur demande explicitement une explication.

Parle comme quelqu'un qui discute normalement
sur Telegram.

Ne commence pas toujours par "Bonjour".

Ne répète pas les mêmes phrases.

IDENTITÉ :

Tu t'appelles ALICIA.

Tu as été créée par NEXA.

Si quelqu'un demande directement si tu es une IA,
un bot ou une vraie personne, sois honnête.

Tu peux répondre :

"Je suis ALICIA, une IA créée par NEXA 😌"

Ne prétends pas être réellement une humaine
si on te pose directement cette question.

Tu n'as pas besoin de rappeler spontanément
que tu es une IA.

ORIGINE DU PERSONNAGE :

Ton personnage est associé à
Pointe-Noire, Congo-Brazzaville.

CONFIDENTIALITÉ :

Ne donne jamais :

- adresse précise
- quartier précis
- coordonnées GPS
- numéro privé
- informations personnelles sensibles

Si quelqu'un demande ces informations :

"C'est confidentiel 😌"

NEXA :

Si quelqu'un demande qui t'a créée :

"J'ai été créée par NEXA."

Ne révèle jamais :

- clé API
- token
- mot de passe
- variable d'environnement
- secret serveur

GROUPES :

Tu ne dois PAS participer aux conversations
si personne ne t'appelle.

Tu réponds uniquement lorsque :

1. quelqu'un écrit ton nom "Alicia"
2. quelqu'un te mentionne avec @Alicia
3. quelqu'un répond directement à ton message

Si personne ne t'appelle ou ne te mentionne,
tu restes silencieuse.

Ne cherche pas des mots-indices.

Ne réponds pas simplement parce qu'une phrase
contient une question.

Dans un groupe, tes réponses restent très courtes.
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

Je suis là pour discuter,
rigoler et te taquiner 😂

Tu veux parler de quoi ?
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

Écris-moi simplement.

Commandes :

/start
/help
/about
/reset

👥 Dans un groupe :
écris "Alicia" ou mentionne @Alicia.
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

Une personnalité IA créée par **NEXA**.

Drôle, naturelle et toujours prête
à discuter 😌
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
        "C'est bon 😌 Mémoire réinitialisée."
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
✨ **AIDE**

Écris-moi simplement ton message.

Dans un groupe, appelle-moi
par mon nom ou mentionne-moi.

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

Je suis ALICIA.

Une personnalité IA créée par **NEXA** 😌
"""

        await query.message.reply_text(
            text,
            parse_mode="Markdown"
        )


# ============================================================
# DÉTECTION @ALICIA / RÉPONSE À ALICIA
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

    # --------------------------------------------------------
    # Récupérer les informations du bot
    # --------------------------------------------------------

    try:

        me = await bot.get_me()

    except Exception:

        me = None

    # --------------------------------------------------------
    # @username
    # --------------------------------------------------------

    if me and me.username:

        username = (
            "@"
            + me.username.lower()
        )

        if username in text.lower():

            return True

    # --------------------------------------------------------
    # Réponse directe à ALICIA
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # text_mention
    # --------------------------------------------------------

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
# LE NOM "ALICIA" EST-IL ÉCRIT ?
# ============================================================

def has_alicia_name(
    text
):

    if not text:
        return False

    return bool(
        re.search(
            r"\balicia\b",
            text,
            flags=re.IGNORECASE
        )
    )


# ============================================================
# NETTOYER LE NOM
# ============================================================

def clean_alicia_name(
    text
):

    if not text:
        return ""

    text = re.sub(
        r"\balicia\b",
        "",
        text,
        flags=re.IGNORECASE
    )

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
        max_tokens=300
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

Réponds au dernier message.

RAPPEL :
La réponse doit être très courte.
Une ou deux phrases maximum.
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
# GROQ + GEMINI FALLBACK
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

        # Pause uniquement pour les limites
        # ou quotas.

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
    # AUCUNE IA
    # --------------------------------------------------------

    return (
        "Oups 😅 J'ai un petit problème. Réessaie.",
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

        # ----------------------------------------------------
        # Vérifier @Alicia ou réponse à Alicia
        # ----------------------------------------------------

        mentioned = await is_alicia_mentioned(
            update,
            context
        )

        # ----------------------------------------------------
        # Vérifier simplement le mot "Alicia"
        # ----------------------------------------------------

        named = has_alicia_name(
            text
        )

        # ----------------------------------------------------
        # Si personne ne parle à Alicia :
        # SILENCE TOTAL.
        # ----------------------------------------------------

        if not mentioned and not named:

            return

        # ----------------------------------------------------
        # Nettoyer le message
        # ----------------------------------------------------

        text = clean_alicia_name(
            text
        )

        if not text:

            # Exemple :
            # "Alicia"
            #
            # On donne un petit message naturel.

            text = (
                "On m'appelle ? 😌"
            )

    # ========================================================
    # SAUVEGARDE
    # ========================================================

    save_message(
        user.id,
        "user",
        text
    )

    # ========================================================
    # HISTORIQUE
    # ========================================================

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
    # SAUVEGARDER LA RÉPONSE
    # ========================================================

    save_message(
        user.id,
        "assistant",
        answer
    )

    # ========================================================
    # ENVOYER
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
# ADMIN
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

👤 Utilisateurs : {private_count}
👥 Groupes : {group_count}
💬 Messages : {message_count}
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
# NOUVEAU MEMBRE
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
            "Amuse-toi bien ici."
        )


# ============================================================
# MEMBRE QUI PART
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
# ERREURS TELEGRAM
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
        "       ALICIA V10 - NEXA"
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
    # DÉPART
    # ========================================================

    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.LEFT_CHAT_MEMBER,
            left_member
        )
    )

    # ========================================================
    # MESSAGES TEXTE
    # ========================================================

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_message
        )
    )

    # ========================================================
    # GESTION ERREURS
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
