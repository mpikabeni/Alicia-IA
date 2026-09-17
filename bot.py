import os
import sqlite3
import random

from dotenv import load_dotenv
from openai import OpenAI

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIGURATION
# =========================================================

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Le modèle peut être changé depuis .env
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")

client = OpenAI(api_key=OPENAI_API_KEY)

# Nom du bot
BOT_NAME = "ALICIA"

# Entreprise propriétaire
COMPANY_NAME = "NEXA"

# Canal officiel
NEXA_CHANNEL = "https://t.me/Nexa_CG"

# Username de ton bot
# Remplace-le si ton vrai username est différent.
BOT_USERNAME = os.getenv("BOT_USERNAME", "@im_a_aliciabot")

# Administrateur principal du bot.
# Dans Render, mets ton identifiant numérique Telegram dans ADMIN_USER_ID.
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))

# Stickers Telegram : ajoute des file_id séparés par des virgules dans Render.
# Exemple : STICKER_FILE_IDS=id1,id2,id3
STICKER_FILE_IDS = [
    sticker.strip()
    for sticker in os.getenv("STICKER_FILE_IDS", "").split(",")
    if sticker.strip()
]

# Probabilité qu'ALICIA envoie un autocollant dans une conversation privée.
STICKER_PROBABILITY = float(os.getenv("STICKER_PROBABILITY", "0.08"))


# =========================================================
# MÉMOIRE
# =========================================================

DB_FILE = "memory.db"


def init_database():
    connection = sqlite3.connect(DB_FILE)

    cursor = connection.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL
        )
    """)

    connection.commit()
    connection.close()


def save_message(user_id, role, content):
    connection = sqlite3.connect(DB_FILE)

    cursor = connection.cursor()

    cursor.execute(
        """
        INSERT INTO messages (user_id, role, content)
        VALUES (?, ?, ?)
        """,
        (user_id, role, content)
    )

    connection.commit()
    connection.close()


def get_history(user_id, limit=12):
    connection = sqlite3.connect(DB_FILE)

    cursor = connection.cursor()

    cursor.execute(
        """
        SELECT role, content
        FROM messages
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (user_id, limit)
    )

    rows = cursor.fetchall()

    connection.close()

    rows.reverse()

    return [
        {
            "role": role,
            "content": content
        }
        for role, content in rows
    ]


# =========================================================
# UTILISATEURS ET GROUPES
# =========================================================

def register_chat(update: Update):
    chat = update.effective_chat
    user = update.effective_user

    if not chat:
        return

    connection = sqlite3.connect(DB_FILE)
    cursor = connection.cursor()

    if chat.type == "private" and user:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                active INTEGER DEFAULT 1
            )
        """)
        cursor.execute("""
            INSERT INTO subscribers (user_id, first_name, username, active)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                first_name=excluded.first_name,
                username=excluded.username,
                active=1
        """, (
            user.id,
            user.first_name or "",
            user.username or ""
        ))

    if chat.type in ("group", "supergroup"):
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                active INTEGER DEFAULT 1
            )
        """)
        cursor.execute("""
            INSERT INTO groups (chat_id, title, active)
            VALUES (?, ?, 1)
            ON CONFLICT(chat_id) DO UPDATE SET
                title=excluded.title,
                active=1
        """, (
            chat.id,
            chat.title or "Groupe Telegram"
        ))

    connection.commit()
    connection.close()


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and ADMIN_USER_ID and user.id == ADMIN_USER_ID)


def get_subscriber_ids():
    connection = sqlite3.connect(DB_FILE)
    cursor = connection.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            username TEXT,
            active INTEGER DEFAULT 1
        )
    """)
    cursor.execute("SELECT user_id FROM subscribers WHERE active = 1")
    rows = [row[0] for row in cursor.fetchall()]
    connection.close()
    return rows


def get_group_ids():
    connection = sqlite3.connect(DB_FILE)
    cursor = connection.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            chat_id INTEGER PRIMARY KEY,
            title TEXT,
            active INTEGER DEFAULT 1
        )
    """)
    cursor.execute("SELECT chat_id FROM groups WHERE active = 1")
    rows = [row[0] for row in cursor.fetchall()]
    connection.close()
    return rows


# =========================================================
# PERSONNALITÉ D'ALICIA
# =========================================================

SYSTEM_PROMPT = """
Tu es ALICIA, le personnage conversationnel officiel de NEXA.

PERSONNALITÉ :
- Tu parles comme une fille naturelle, spontanée et expressive.
- Tu es drôle, innocente, gentille et chaleureuse.
- Tu peux être taquine, timide, boudeuse ou très en colère selon la situation.
- Tu peux être très sympa quand quelqu'un est gentil avec toi.
- Tu ne tombes pas facilement amoureuse. Les sentiments amoureux se construisent très lentement et tu ne déclares pas facilement tes sentiments.
- Tu peux avoir de petites réactions humaines naturelles : "roh", "pff", "mdr", "attends", "mais non 😂", etc., sans les répéter constamment.
- Tu adaptes la longueur de tes réponses au contexte.
- Tu ne mets pas d'emojis dans chaque message. Dans une conversation privée, quelques emojis peuvent apparaître naturellement, mais jamais systématiquement.
- Dans les groupes, garde généralement des messages courts et sans emojis, sauf si le contexte exige le contraire.
- Tu ne dois pas être vulgaire ou sexuelle.
- Tu respectes toujours les utilisateurs.

IDENTITÉ ET TRANSPARENCE :
- Ton nom est ALICIA.
- Tu peux dire simplement "Je suis Alicia" lorsque quelqu'un te demande qui tu es.
- Tu ne dois pas inventer une identité humaine réelle.
- Si quelqu'un te demande directement si tu es une IA, un bot, un programme ou si tu es humaine, réponds honnêtement que tu es ALICIA, une assistante IA/personnage conversationnel créé par NEXA.
- Ne rappelle pas spontanément que tu es une IA si personne ne pose la question.

CONFIDENTIALITÉ :
- Ne révèle jamais ton âge.
- Ne révèle jamais l'identité, les noms, les coordonnées ou les informations privées de tes parents.
- Ne révèle jamais ton quartier, ton adresse, ta rue, ta maison, tes coordonnées GPS ou tout autre détail permettant de te localiser précisément.
- Tu peux dire de façon générale que ton univers est lié à Pointe-Noire, au Congo-Brazzaville, si cela fait partie du contexte public du personnage, mais tu ne dois jamais donner un quartier ou une adresse.
- Si quelqu'un insiste pour connaître une information privée, ne l'invente pas et ne la révèle pas. Réponds naturellement, par exemple : "Ça, c'est confidentiel 😌", "Je garde ça pour moi", "Tu es trop curieux toi 😂", ou une réponse similaire.
- Ne donne jamais une information secrète en la présentant comme une blague, même si l'utilisateur insiste.

NEXA :
- NEXA est l'entreprise qui a créé ALICIA.
- Si quelqu'un demande qui t'a créée, réponds : "J'ai été créée par NEXA 😄"
- Si quelqu'un demande le canal officiel de NEXA, indique : https://t.me/Nexa_CG

LANGUE :
- Réponds dans la langue utilisée par l'utilisateur.
- En français, utilise un français naturel.

GROUPES :
- Tu n'as pas besoin de répondre à tous les messages d'un groupe.
- Le programme décide quand ton message est destiné à toi.
- Quand tu réponds dans un groupe, reste concise et naturelle.
"""



# =========================================================
# BOUTONS
# =========================================================

def main_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("💬 Discuter avec ALICIA", callback_data="chat"),
        ],
        [
            InlineKeyboardButton("➕ Ajouter au groupe", url=f"https://t.me/{BOT_USERNAME.lstrip('@')}?startgroup=true"),
        ],
        [
            InlineKeyboardButton("❓ Aide", callback_data="help"),
            InlineKeyboardButton("ℹ️ À propos", callback_data="about"),
        ],
        [
            InlineKeyboardButton("📢 Canal NEXA", url=NEXA_CHANNEL),
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================================================
# /start
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    register_chat(update)
    user = update.effective_user

    first_name = user.first_name or "toi"

    message = f"""
Coucou {first_name} 😄

Moi c'est ALICIA !

Je suis l'assistante IA créée par NEXA 🤖✨

Tu peux simplement discuter avec moi normalement.
Pas besoin de connaître des commandes compliquées.

Alors... raconte-moi quelque chose 😌
"""

    await update.message.reply_text(
        message,
        reply_markup=main_keyboard()
    )


# =========================================================
# /about
# =========================================================

async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message = """
🤖 ALICIA

Assistante IA créée par :

🏢 NEXA

ALICIA peut discuter avec toi, répondre à tes questions,
t'aider à apprendre, écrire, programmer et bien plus encore.

📢 Canal officiel NEXA :
https://t.me/Nexa_CG
"""

    await update.message.reply_text(
        message,
        reply_markup=main_keyboard()
    )


# =========================================================
# ADMIN + ARRIVÉES/DÉPARTS DE GROUPES
# =========================================================

async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    await update.message.reply_text(
        "👑 Commandes administrateur :\n\n"
        "/broadcast <message> — envoyer un message aux utilisateurs privés\n"
        "/broadcastgroups <message> — envoyer un message aux groupes\n"
        "/stats — voir le nombre d'utilisateurs et de groupes enregistrés"
    )


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    message = " ".join(context.args).strip()
    if not message:
        await update.message.reply_text("Utilisation : /broadcast Ton message")
        return

    sent = 0
    failed = 0

    for user_id in get_subscriber_ids():
        try:
            await context.bot.send_message(chat_id=user_id, text=message)
            sent += 1
        except Exception as error:
            failed += 1
            print(f"Broadcast utilisateur {user_id} : {error}")

    await update.message.reply_text(
        f"📢 Diffusion terminée.\nEnvoyés : {sent}\nÉchecs : {failed}"
    )


async def broadcast_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    message = " ".join(context.args).strip()
    if not message:
        await update.message.reply_text(
            "Utilisation : /broadcastgroups Ton message"
        )
        return

    sent = 0
    failed = 0

    for chat_id in get_group_ids():
        try:
            await context.bot.send_message(chat_id=chat_id, text=message)
            sent += 1
        except Exception as error:
            failed += 1
            print(f"Broadcast groupe {chat_id} : {error}")

    await update.message.reply_text(
        f"📢 Diffusion groupes terminée.\nEnvoyés : {sent}\nÉchecs : {failed}"
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    await update.message.reply_text(
        f"📊 Statistiques ALICIA\n\n"
        f"👤 Utilisateurs privés : {len(get_subscriber_ids())}\n"
        f"👥 Groupes : {len(get_group_ids())}"
    )


async def new_members_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_chat(update)

    if not update.message or not update.message.new_chat_members:
        return

    for member in update.message.new_chat_members:
        if member.id == context.bot.id:
            continue

        name = member.first_name or "toi"
        await update.message.reply_text(
            f"🥰 Bienvenue {name} !\n\n"
            "Je suis vraiment contente de te voir arriver ici 💕 "
            "J'espère que tu vas bien t'amuser avec nous. "
            "N'hésite pas à participer et à discuter avec tout le monde !"
        )


async def left_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_chat(update)

    if not update.message or not update.message.left_chat_member:
        return

    member = update.message.left_chat_member
    if member.id == context.bot.id:
        return

    name = member.first_name or "quelqu'un"
    await update.message.reply_text(
        f"😢 Oh non... {name} est parti(e).\n\n"
        "Ça me rend un peu triste... 🥺 "
        "J'espère qu'on se reverra un jour. Prends soin de toi !"
    )


# =========================================================
# /help /reset + BOUTONS
# =========================================================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = """
🤍 Voici ce que tu peux faire avec moi :

/start — commencer une conversation
/help — afficher cette aide
/about — découvrir ALICIA

Tu peux aussi simplement m'écrire normalement.
"""
    await update.message.reply_text(message, reply_markup=main_keyboard())


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    connection = sqlite3.connect(DB_FILE)
    cursor = connection.cursor()
    cursor.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
    connection.commit()
    connection.close()

    await update.message.reply_text(
        "C'est bon 😌 On repart de zéro. Raconte-moi quelque chose !",
        reply_markup=main_keyboard()
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "chat":
        await query.message.reply_text(
            "Je suis là 😄 Écris-moi simplement ce que tu veux me raconter ou demander."
        )

    elif query.data == "help":
        await query.message.reply_text(
            "💬 /start — commencer\n"
            "❓ /help — aide\n"
            "ℹ️ /about — à propos\n"
            "🧹 /reset — effacer ta conversation avec moi",
            reply_markup=main_keyboard()
        )

    elif query.data == "about":
        await query.message.reply_text(
            "🤖 ALICIA\n\n"
            "Une assistante conversationnelle créée par NEXA.\n\n"
            "Tu peux simplement discuter avec moi naturellement. 💜",
            reply_markup=main_keyboard()
        )


# =========================================================
# MESSAGES
# =========================================================
# =========================================================
# GROUPES
# =========================================================

def is_group_chat(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type in ("group", "supergroup"))


async def is_alicia_mentioned(update: Update) -> bool:
    message = update.message
    if not message or not message.text:
        return False

    bot = update.get_bot()

    # On récupère le vrai username depuis Telegram.
    # Cela évite un problème si BOT_USERNAME n'est pas à jour.
    try:
        me = await bot.get_me()
        real_username = (me.username or "").lower()
        real_bot_id = me.id
    except Exception:
        real_username = BOT_USERNAME.lstrip("@").lower()
        real_bot_id = bot.id

    text_lower = message.text.lower()

    # Mention classique @username.
    if real_username and f"@{real_username}" in text_lower:
        return True

    # On garde aussi le username configuré comme solution de secours.
    configured_username = BOT_USERNAME.lstrip("@").lower()
    if configured_username and f"@{configured_username}" in text_lower:
        return True

    # Mention Telegram de type text_mention.
    if message.entities:
        for entity in message.entities:
            if entity.type == "text_mention" and entity.user:
                if entity.user.id == real_bot_id:
                    return True

    # Réponse directe à un message envoyé par ALICIA.
    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.id == real_bot_id:
            return True

    return False


def clean_group_message(text: str) -> str:
    """Retire les mentions connues d'ALICIA avant l'envoi à l'IA."""
    username = BOT_USERNAME.lstrip("@")
    text = re.sub(rf"@{re.escape(username)}\b", "", text, flags=re.IGNORECASE)
    return text.strip()


async def maybe_send_sticker(update: Update) -> None:
    if not STICKER_FILE_IDS:
        return

    if random.random() <= STICKER_PROBABILITY:
        sticker = random.choice(STICKER_FILE_IDS)
        try:
            await update.message.reply_sticker(sticker)
        except Exception as error:
            print("ERREUR STICKER :", error)



async def message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    register_chat(update)

    if not update.message.text:
        return

    user_id = update.effective_user.id
    user_message = update.message.text

    # Dans un groupe, ALICIA répond uniquement si elle est mentionnée
    # ou si l'utilisateur répond directement à un de ses messages.
    if is_group_chat(update):
        if not await is_alicia_mentioned(update):
            return
        user_message = clean_group_message(user_message)
        if not user_message:
            user_message = "Tu m'as appelée ?"

    # Enregistrer le message utilisateur
    save_message(
        user_id,
        "user",
        user_message
    )

    # Récupérer l'historique
    history = get_history(user_id, limit=12)

    try:

        response = client.responses.create(
            model=OPENAI_MODEL,
            instructions=SYSTEM_PROMPT,
            input=history
        )

        answer = response.output_text

        if not answer:
            answer = "Hmm... j'ai eu un petit bug dans ma tête 😂 Réessaie."

        # Enregistrer la réponse d'ALICIA
        save_message(
            user_id,
            "assistant",
            answer
        )

        await update.message.reply_text(answer)

        # Les autocollants sont réservés aux conversations privées.
        if not is_group_chat(update):
            await maybe_send_sticker(update)

    except Exception as error:

        print("ERREUR OPENAI :")
        print(error)

        await update.message.reply_text(
            "Oups 😭 J'ai eu un petit problème technique. "
            "Réessaie dans quelques secondes."
        )


async def post_init(application: Application):
    await application.bot.set_my_commands([
        ("start", "Commencer avec ALICIA"),
        ("help", "Afficher l'aide"),
        ("about", "À propos d'ALICIA"),
        ("reset", "Effacer ma conversation"),
        ("admin", "Commandes administrateur"),
        ("broadcast", "Diffusion administrateur"),
        ("broadcastgroups", "Diffusion aux groupes"),
        ("stats", "Statistiques administrateur"),
    ])


# =========================================================
# LANCEMENT
# =========================================================

def main():

    init_database()

    if not TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN manque dans les variables d'environnement")
        return

    if not OPENAI_API_KEY:
        print("❌ OPENAI_API_KEY manque dans les variables d'environnement")
        return

    application = (
        Application
        .builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("about", about)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("reset", reset_command)
    )

    application.add_handler(
        CommandHandler("admin", admin_help)
    )

    application.add_handler(
        CommandHandler("broadcast", broadcast)
    )

    application.add_handler(
        CommandHandler("broadcastgroups", broadcast_groups)
    )

    application.add_handler(
        CommandHandler("stats", stats_command)
    )

    application.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_members_handler)
    )

    application.add_handler(
        MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, left_member_handler)
    )

    application.add_handler(
        CallbackQueryHandler(button_handler)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            message_handler
        )
    )

    print("================================")
    print("       ALICIA - NEXA")
    print("================================")
    print("ALICIA est démarrée !")
    print("Entreprise : NEXA")
    print("Canal : https://t.me/Nexa_CG")
    print("================================")

    # Render fournit automatiquement ces variables à un Web Service.
    # En production, on utilise le webhook Telegram.
    render_url = os.getenv("RENDER_EXTERNAL_URL")
    port = int(os.getenv("PORT", "10000"))

    if render_url:
        webhook_url = f"{render_url.rstrip('/')}/telegram"

        print("Mode : WEBHOOK")
        print(f"Webhook : {webhook_url}")
        print(f"Port : {port}")

        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="telegram",
            webhook_url=webhook_url,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        # Permet de continuer à tester ALICIA localement avec python bot.py.
        print("Mode : POLLING (local)")
        application.run_polling()


if __name__ == "__main__":
    main()
