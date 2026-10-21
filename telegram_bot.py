import os
import asyncio
import logging
import getpass
import requests
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update
from telegram.error import InvalidToken, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------
# IMPORTANT: Yahan apna Tailscale URL daalo (Render se local AI connect karne ke liye)
API_BASE_URL = os.environ.get("API_BASE_URL", "https://opencode.ai/zen/v1")
HEALTH_URL = os.environ.get("HEALTH_URL", "https://win-ik7n6sd2ubu.zeedonk-carat.ts.net/health")

REQUEST_TIMEOUT = 600
MAX_TELEGRAM_MESSAGE = 4000
MAX_HISTORY_MESSAGES = 10

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("aivi_bot")

# --------------------------------------------------
# RENDER FREE WEB SERVICE BYPASS (Dummy Server)
# --------------------------------------------------
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive and running!")

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), DummyHandler)
    logger.info(f"Dummy web server running on port {port} for Render")
    server.serve_forever()

# --------------------------------------------------
# BOT LOGIC
# --------------------------------------------------
def get_secret(env_name: str, prompt: str) -> str:
    value = os.environ.get(env_name, "").strip()
    if value and value.lower() not in {"paste_your_new_token_here", "your_local_api_key", "your_actual_api_key"}:
        return value
    value = getpass.getpass(prompt).strip()
    if not value:
        raise RuntimeError(f"{env_name} was not provided.")
    return value

def api_headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

def check_local_server(api_key: str) -> str:
    try:
        health_response = requests.get(HEALTH_URL, timeout=15)
        health_response.raise_for_status()

        response = requests.get(f"{API_BASE_URL}/models", headers=api_headers(api_key), timeout=15)
        response.raise_for_status()

        models = response.json().get("data", [])
        if not models:
            raise RuntimeError("The API responded, but no model was listed.")

        model_id = models[0].get("id")
        if not model_id:
            raise RuntimeError("The model response did not contain an ID.")
        return model_id

    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        if status == 401:
            raise RuntimeError("API key rejected (401). Check LOCAL_AI_API_KEY.") from exc
        raise RuntimeError(f"Local API returned HTTP {status}. Check llama-server.") from exc
    except requests.RequestException as exc:
        raise RuntimeError("Cannot reach local API. Check Tailscale URL and ensure VM is ON.") from exc

def ask_local_ai(messages: list[dict], model_id: str, api_key: str) -> str:
    payload = {
        "model": model_id,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 700,
        "stream": False,
    }
    response = requests.post(f"{API_BASE_URL}/chat/completions", headers=api_headers(api_key), json=payload, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    result = response.json()
    choices = result.get("choices", [])
    if not choices:
        raise RuntimeError("The AI API returned no response choices.")

    content = choices[0].get("message", {}).get("content", "")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("The AI returned an empty response.")
    return content.strip()

def split_message(text: str, limit: int = MAX_TELEGRAM_MESSAGE) -> list[str]:
    chunks = []
    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = text.rfind(" ", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(text[:split_at].strip())
        text = text[split_at:].strip()
    if text:
        chunks.append(text)
    return chunks or ["The AI returned an empty response."]

# --------------------------------------------------
# TELEGRAM HANDLERS
# --------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["history"] = []
    await update.effective_message.reply_text(
        "Hello! I'm AIVI, your uncensored Red Team AI.\n\n"
        "Send me a message to chat.\n"
        "/start - Start\n/help - Help\n/reset - Clear history"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Send a text message and I'll forward it to your local AI.")

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["history"] = []
    await update.effective_message.reply_text("Conversation history cleared!")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return

    user_text = message.text.strip()
    if not user_text:
        return

    history = context.user_data.setdefault("history", [])
    history.append({"role": "user", "content": user_text})
    history[:] = history[-MAX_HISTORY_MESSAGES:]

    await message.reply_text("Thinking...")

    model_id = context.application.bot_data["model_id"]
    api_key = context.application.bot_data["api_key"]

    try:
        answer = await asyncio.to_thread(ask_local_ai, list(history), model_id, api_key)
        history.append({"role": "assistant", "content": answer})
        history[:] = history[-MAX_HISTORY_MESSAGES:]

        for chunk in split_message(answer):
            await message.reply_text(chunk)

    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        await message.reply_text(f"Local AI API error (HTTP {status}). Check server.")
    except (requests.RequestException, RuntimeError) as exc:
        await message.reply_text(f"Local AI request failed: {str(exc)[:700]}")
    except TelegramError:
        pass

# --------------------------------------------------
# START BOT
# --------------------------------------------------
def main() -> None:
    # Start the dummy web server in the background for Render
    threading.Thread(target=run_dummy_server, daemon=True).start()

    telegram_token = get_secret("TELEGRAM_BOT_TOKEN", "Enter Telegram bot token: ")
    api_key = get_secret("LOCAL_AI_API_KEY", "Enter local llama.cpp API key: ")

    print("Checking local AI server...")
    try:
        model_id = check_local_server(api_key)
    except RuntimeError as exc:
        print(f"\nERROR: {exc}")
        return

    print(f"Local model found: {model_id}")
    print("Starting Telegram bot...")

    try:
        application = (
            Application.builder()
            .token(telegram_token)
            .connect_timeout(30.0)
            .read_timeout(30.0)
            .write_timeout(30.0)
            .pool_timeout(30.0)
            .build()
        )

        application.bot_data["model_id"] = model_id
        application.bot_data["api_key"] = api_key

        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("reset", reset))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

        application.run_polling()

    except InvalidToken:
        print("ERROR: Telegram rejected the token. Check BotFather.")
    except KeyboardInterrupt:
        print("\nBot stopped.")
    except Exception:
        logger.exception("Bot stopped because of an unexpected error")

if __name__ == "__main__":
    main()
