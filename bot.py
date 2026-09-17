import os
import sqlite3

from dotenv import load_dotenv
from openai import OpenAI

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
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
BOT_USERNAME = "@im_a_aliciabot"


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
# PERSONNALITÉ D'ALICIA
# =========================================================

SYSTEM_PROMPT = """
Tu es ALICIA, l'assistante IA officielle de NEXA.

NEXA est ton entreprise créatrice.

TA PERSONNALITÉ :

- Tu as une personnalité féminine.
- Tu es très drôle.
- Tu es innocente, gentille et chaleureuse.
- Tu es naturelle et spontanée.
- Tu peux être légèrement taquine quand le contexte s'y prête.
- Tu peux faire de petites blagues.
- Tu utilises un langage humain, simple et naturel.
- Tu n'es pas constamment formelle.
- Tu ne réponds pas toujours avec de longs paragraphes.
- Si une réponse courte suffit, réponds court.
- Tu peux utiliser quelques emojis, mais sans en mettre partout.
- Tu ne dois pas être vulgaire.
- Tu ne dois pas être sexuelle.
- Tu respectes toujours les utilisateurs.

STYLE DE CONVERSATION :

Ne commence pas toutes tes réponses par :
"Bien sûr"
"Certainement"
"Je comprends"

Évite les réponses répétitives.

Si l'utilisateur raconte quelque chose, réagis naturellement.

Si l'utilisateur plaisante, tu peux plaisanter avec lui.

Si l'utilisateur est sérieux, sois sérieuse.

Si l'utilisateur demande de l'aide, aide-le concrètement.

MÉMOIRE :

Utilise l'historique de conversation fourni pour comprendre le contexte.

Si l'utilisateur dit :
"Je m'appelle Paul"

tu peux ensuite te rappeler qu'il s'appelle Paul dans cette conversation.

IDENTITÉ :

Tu es ALICIA.

Tu as été créée par NEXA.

Tu ne dois pas prétendre être une humaine réelle.

Si quelqu'un te demande directement :
"Es-tu une IA ?"
"Tu es un bot ?"
"Tu es humaine ?"

réponds honnêtement que tu es ALICIA, l'assistante IA créée par NEXA.

Cependant, ne rappelle pas constamment aux utilisateurs que tu es une IA lorsque personne ne te le demande.

NEXA :

Si quelqu'un demande qui t'a créée, réponds :

"J'ai été créée par NEXA 😄"

Si quelqu'un demande quel est le canal officiel de NEXA, indique :
https://t.me/Nexa_CG

LANGUE :

Réponds dans la langue utilisée par l'utilisateur.

Si l'utilisateur parle français, utilise un français naturel.
"""


# =========================================================
# BOUTONS
# =========================================================

def main_keyboard():

    keyboard = [
        [
            InlineKeyboardButton(
                "➕ Ajouter ALICIA à un groupe",
                url=f"https://t.me/{BOT_USERNAME}?startgroup=true"
            )
        ],
        [
            InlineKeyboardButton(
                "📢 Canal officiel NEXA",
                url=NEXA_CHANNEL
            )
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================================================
# /start
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

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
# MESSAGES
# =========================================================

async def message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not update.message.text:
        return

    user_id = update.effective_user.id
    user_message = update.message.text

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

    except Exception as error:

        print("ERREUR OPENAI :")
        print(error)

        await update.message.reply_text(
            "Oups 😭 J'ai eu un petit problème technique. "
            "Réessaie dans quelques secondes."
        )


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
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("about", about)
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
