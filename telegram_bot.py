
import os
import asyncio
import logging
import getpass
import requests

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

API_BASE_URL = "https://win-ik7n6sd2ubu.zeedonk-carat.ts.net/v1"
HEALTH_URL = "https://win-ik7n6sd2ubu.zeedonk-carat.ts.net/health"

REQUEST_TIMEOUT = 180
MAX_TELEGRAM_MESSAGE = 4000
MAX_HISTORY_MESSAGES = 10  # recent user/assistant messages

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("aivi_bot")


def get_secret(env_name: str, prompt: str) -> str:
    """Read a secret from an environment variable or hidden input."""
    value = os.environ.get(env_name, "").strip()

    if value and value.lower() not in {
        "paste_your_new_token_here",
        "your_local_api_key",
        "your_actual_api_key",
    }:
        return value

    value = getpass.getpass(prompt).strip()
    if not value:
        raise RuntimeError(f"{env_name} was not provided.")

    return value


def api_headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def check_local_server(api_key: str) -> str:
    """Check the proxy and retrieve the currently loaded model ID."""
    try:
        health_response = requests.get(HEALTH_URL, timeout=8)
        health_response.raise_for_status()

        response = requests.get(
            f"{API_BASE_URL}/models",
            headers=api_headers(api_key),
            timeout=15,
        )
        response.raise_for_status()

        models = response.json().get("data", [])
        if not models:
            raise RuntimeError(
                "The API responded, but no model was listed."
            )

        model_id = models[0].get("id")
        if not model_id:
            raise RuntimeError("The model response did not contain an ID.")

        return model_id

    except requests.HTTPError as exc:
        status = (
            exc.response.status_code
            if exc.response is not None
            else "unknown"
        )

        if status == 401:
            raise RuntimeError(
                "API key rejected (401). Check LOCAL_AI_API_KEY "
                "against the key configured on llama-server.exe."
            ) from exc

        raise RuntimeError(
            f"Local API returned HTTP {status}. "
            "Check that llama-server.exe and proxy.py are running."
        ) from exc

    except requests.RequestException as exc:
        raise RuntimeError(
            "Cannot reach the local API. Start llama-server.exe "
            "on port 8080 and proxy.py on port 8081."
        ) from exc


def ask_local_ai(
    messages: list[dict],
    model_id: str,
    api_key: str,
) -> str:
    """Send chat messages to the OpenAI-compatible local API."""
    payload = {
        "model": model_id,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 700,
        "stream": False,
    }

    response = requests.post(
        f"{API_BASE_URL}/chat/completions",
        headers=api_headers(api_key),
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
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
    """Split long replies into Telegram-safe chunks."""
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
# TELEGRAM COMMANDS
# --------------------------------------------------

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    context.user_data["history"] = []

    await update.effective_message.reply_text(
        "Hello! I'm your local AI assistant.\n\n"
        "Send me a message to chat with your local model.\n\n"
        "Commands:\n"
        "/start - Start a conversation\n"
        "/help - Show help\n"
        "/reset - Clear conversation history"
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await update.effective_message.reply_text(
        "Send a text message and I'll forward it to your local AI.\n\n"
        "/start - Start chatting\n"
        "/help - Show these instructions\n"
        "/reset - Clear your conversation history"
    )


async def reset(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    context.user_data["history"] = []
    await update.effective_message.reply_text(
        "Conversation history cleared. Start a new chat!"
    )


# --------------------------------------------------
# MESSAGE HANDLER
# --------------------------------------------------

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return

    user_text = message.text.strip()
    if not user_text:
        return

    history = context.user_data.setdefault("history", [])
    history.append({"role": "user", "content": user_text})

    # Keep only the most recent conversation messages.
    history[:] = history[-MAX_HISTORY_MESSAGES:]

    await message.reply_text("Thinking...")

    model_id = context.application.bot_data["model_id"]
    api_key = context.application.bot_data["api_key"]

    try:
        answer = await asyncio.to_thread(
            ask_local_ai,
            list(history),
            model_id,
            api_key,
        )

        history.append({"role": "assistant", "content": answer})
        history[:] = history[-MAX_HISTORY_MESSAGES:]

        for chunk in split_message(answer):
            await message.reply_text(chunk)

    except requests.HTTPError as exc:
        status = (
            exc.response.status_code
            if exc.response is not None
            else "unknown"
        )
        logger.exception("Local AI returned an HTTP error")
        await message.reply_text(
            f"Local AI API error (HTTP {status}). "
            "Check the server and API-key configuration."
        )

    except (requests.RequestException, RuntimeError) as exc:
        logger.exception("Local AI request failed")
        await message.reply_text(
            f"Local AI request failed: {str(exc)[:700]}"
        )

    except TelegramError:
        logger.exception("Telegram could not send the response")


# --------------------------------------------------
# START BOT
# --------------------------------------------------

def main() -> None:
    telegram_token = get_secret(
        "TELEGRAM_BOT_TOKEN",
        "Enter your NEW Telegram bot token (input hidden): ",
    )

    api_key = get_secret(
        "LOCAL_AI_API_KEY",
        "Enter your local llama.cpp API key (input hidden): ",
    )

    print("Checking local AI server...")

    try:
        model_id = check_local_server(api_key)
    except RuntimeError as exc:
        print(f"\nERROR: {exc}")
        print("Check ports 8080 and 8081, then run the bot again.")
        return

    print(f"Local model found: {model_id}")
    print("Starting Telegram bot...")

    try:
        application = (
            Application.builder()
            .token(telegram_token)
            .build()
        )

        application.bot_data["model_id"] = model_id
        application.bot_data["api_key"] = api_key

        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("reset", reset))
        application.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                handle_message,
            )
        )

        application.run_polling()

    except InvalidToken:
        print(
            "ERROR: Telegram rejected the bot token. "
            "Create a new token with @BotFather and enter it locally."
        )
    except KeyboardInterrupt:
        print("\nBot stopped.")
    except Exception:
        logger.exception("Bot stopped because of an unexpected error")
        raise


if __name__ == "__main__":
    main()
