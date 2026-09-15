import base64
import logging
from pathlib import Path

import asyncio
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from telethon import TelegramClient
from telethon.errors import (
    AuthTokenExpiredError,
    AuthTokenInvalidError,
    AuthTokenAlreadyAcceptedError,
    SessionPasswordNeededError,
)
from telethon.tl.functions.auth import AcceptLoginTokenRequest

import config  # BOT_TOKEN, API_ID, API_HASH — правишь прямо в config.py

# ---------------------------------------------------------------------------
# Config sanity check
# ---------------------------------------------------------------------------

if "PUT_YOUR" in config.BOT_TOKEN or "PUT_YOUR" in config.API_HASH or config.API_ID == 123456:
    raise SystemExit(
        "Открой config.py и впиши свои реальные BOT_TOKEN, API_ID, API_HASH "
        "перед запуском."
    )

SESSIONS_DIR = Path("sessions")
SESSIONS_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("qrlogin_bot")

router = Router()


# ---------------------------------------------------------------------------
# FSM states
# ---------------------------------------------------------------------------

class Flow(StatesGroup):
    waiting_session = State()
    ready = State()   # сессия сохранена, ждём ссылку из QR


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_token_bytes(text: str) -> bytes | None:
    """Pull the base64 token out of a tg://login?token=... URL and decode it."""
    text = text.strip()
    if "token=" not in text:
        return None
    token_b64 = text.split("token=", 1)[1]
    token_b64 = token_b64.split("&", 1)[0].strip()
    padded = token_b64 + "=" * (-len(token_b64) % 4)
    try:
        return base64.urlsafe_b64decode(padded)
    except Exception:
        return None


def session_path_for(user_id: int) -> Path:
    return SESSIONS_DIR / f"{user_id}.session"


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    session_path = session_path_for(message.from_user.id)
    if session_path.exists():
        await state.set_state(Flow.ready)
        await message.answer(
            "У меня уже есть сохранённая сессия для тебя. Просто пришли ссылку "
            "из QR-кода (текстом, вида tg://login?token=...), и я подтвержу вход.\n\n"
            "Хочешь заменить файл сессии — пришли новый .session (или /reset)."
        )
        return

    await message.answer(
        "Пришли мне файл .session (Telethon) для аккаунта, который хочешь логинить по QR.\n\n"
        "⚠️ Каждая .session — это полный доступ к аккаунту. Бот хранит файл у себя, "
        "используй это только со своими аккаунтами и на доверенном сервере."
    )
    await state.set_state(Flow.waiting_session)


@router.message(StateFilter(Flow.waiting_session), F.document)
async def got_session_file(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    if not doc.file_name.endswith(".session"):
        await message.answer("Это не похоже на .session файл. Пришли файл с расширением .session")
        return

    session_path = session_path_for(message.from_user.id)
    file = await bot.get_file(doc.file_id)
    await bot.download_file(file.file_path, destination=session_path)

    await state.set_state(Flow.ready)
    await message.answer(
        "Сессия сохранена ✅\n\n"
        "Теперь пришли ссылку из QR-кода входа текстом — вида\n"
        "tg://login?token=AAAA...\n\n"
        "Как её достать:\n"
        "• web.telegram.org — открой инструменты разработчика (F12) → Elements, "
        "найди атрибут со ссылкой tg://login?token=...\n"
        "• либо отсканируй QR любым обычным QR-сканером (камера телефона / "
        "приложение-сканер из Play Store) и просто скопируй распознанный текст.\n\n"
        "⏱ Токен живёт ~30 сек — присылай сразу после того как QR появился."
    )


@router.message(StateFilter(Flow.waiting_session))
async def waiting_session_wrong_type(message: Message):
    await message.answer("Жду именно файл .session (отправь его как документ).")


@router.message(F.document.file_name.endswith(".session"))
async def replace_session_anywhere(message: Message, state: FSMContext, bot: Bot):
    """Позволяет прислать новый .session в любой момент, не только на старте."""
    session_path = session_path_for(message.from_user.id)
    file = await bot.get_file(message.document.file_id)
    await bot.download_file(file.file_path, destination=session_path)
    await state.set_state(Flow.ready)
    await message.answer("Сессия обновлена ✅ Присылай ссылку из QR (tg://login?token=...).")


@router.message(StateFilter(Flow.ready), F.text)
async def got_qr_link(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    session_path = session_path_for(user_id)

    if not session_path.exists():
        await message.answer("Не нашёл сохранённую сессию, начни заново: /start")
        await state.clear()
        return

    token = extract_token_bytes(message.text)
    if not token:
        await message.answer(
            "Это не похоже на ссылку входа Telegram. Нужна строка вида "
            "tg://login?token=AAAA..."
        )
        return

    status_msg = await message.answer("Ссылка распознана, подтверждаю вход…")

    client = TelegramClient(str(session_path.with_suffix("")), config.API_ID, config.API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await status_msg.edit_text(
                "Эта .session не авторизована (протухла/не залогинена) — "
                "подтвердить вход ей нельзя."
            )
            return

        result = await client(AcceptLoginTokenRequest(token=token))
        await status_msg.edit_text(f"Готово ✅ Устройство авторизовано.\n{result}")

    except AuthTokenExpiredError:
        await status_msg.edit_text("QR-код уже истёк (живёт ~30 сек). Обнови QR и пришли ссылку заново.")
    except AuthTokenAlreadyAcceptedError:
        await status_msg.edit_text("Этот QR уже был подтверждён ранее.")
    except AuthTokenInvalidError:
        await status_msg.edit_text("Токен из ссылки недействителен — убедись, что это именно ссылка входа Telegram.")
    except SessionPasswordNeededError:
        await status_msg.edit_text(
            "На аккаунте включён облачный пароль (2FA) для подтверждения входа — "
            "он не помешал этому шагу, но если новое устройство просит пароль отдельно, "
            "введи его там вручную."
        )
    except Exception as e:
        log.exception("Login confirmation failed")
        await status_msg.edit_text(f"Ошибка при подтверждении входа: {e}")
    finally:
        await client.disconnect()


@router.message(StateFilter(Flow.ready))
async def ready_wrong_type(message: Message):
    await message.answer("Жду текстом ссылку вида tg://login?token=...")


@router.message(Command("reset"))
async def cmd_reset(message: Message, state: FSMContext):
    session_path_for(message.from_user.id).unlink(missing_ok=True)
    await state.clear()
    await message.answer("Сброшено. Пришли /start чтобы загрузить сессию заново.")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def main():
    bot = Bot(config.BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
