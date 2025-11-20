from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

TOKEN = "8237657814:AAFdY8o-lGsdzKZstQtOSGslXl-5PcQzP88"

# Professional message text
PROFESSIONAL_MESSAGE = (
    "We’ve upgraded our services! "
    "Please visit our new service at @OraAdbot to continue enjoying our offerings."
)

# Function to send the message with inline button
async def send_service_message(update, context):
    keyboard = [
        [InlineKeyboardButton("Go to @OraAdbot", url="https://t.me/OraAdbot")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        PROFESSIONAL_MESSAGE,
        reply_markup=reply_markup
    )

def main():
    app = ApplicationBuilder().token(TOKEN).build()

    # /start command
    app.add_handler(CommandHandler("start", send_service_message))

    # Reply to every text message
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, send_service_message))

    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()

