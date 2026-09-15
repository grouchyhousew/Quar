import base64
import logging
from pathlib import Path

import cv2
import numpy as np
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
    ready = State()   # session сохранена, ждём скриншоты QR


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def decode_qr_from_image_bytes(image_bytes: bytes) -> str | None:
    """Decode a QR code from raw image bytes using OpenCV (no external zbar dep)."""
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None

    detector = cv2.QRCodeDetector()

    data, points, _ = detector.detectAndDecode(img)
    if data:
        return data

    try:
        ok, decoded_info, points, _ = detector.detectAndDecodeMulti(img)
        if ok:
            for d in decoded_info:
                if d:
                    return d
    except cv2.error:
        pass

    return None


def extract_token_bytes(qr_text: str) -> bytes | None:
    """Pull the base64 token out of a tg://login?token=... URL and decode it."""
    if "token=" not in qr_text:
        return None
    token_b64 = qr_text.split("token=", 1)[1]
    token_b64 = token_b64.split("&", 1)[0]
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
            "У меня уже есть сохранённая сессия для тебя. Просто пришли скриншот "
            "с QR-кодом входа, и я подтвержу вход.\n\n"
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
        "Теперь просто пришли скриншот с QR-кодом входа (с web.telegram.org, "
        "Nicegram, десктопного клиента — откуда угодно), и я авторизую этим "
        "аккаунтом устройство, показавшее QR.\n\n"
        "⏱ QR живёт ~30 сек — присылай сразу после того как он появился на экране."
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
    await message.answer("Сессия обновлена ✅ Присылай скриншот QR.")


@router.message(StateFilter(Flow.ready), F.photo | F.document)
async def got_qr_screenshot(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    session_path = session_path_for(user_id)

    if not session_path.exists():
        await message.answer("Не нашёл сохранённую сессию, начни заново: /start")
        await state.clear()
        return

    if message.photo:
        file_id = message.photo[-1].file_id
    else:
        file_id = message.document.file_id
    tg_file = await bot.get_file(file_id)
    buf = await bot.download_file(tg_file.file_path)
    image_bytes = buf.read()

    qr_text = decode_qr_from_image_bytes(image_bytes)
    if not qr_text:
        await message.answer(
            "Не смог распознать QR на картинке. Пришли более чёткий/крупный "
            "скриншот, без обрезки краёв кода."
        )
        return

    token = extract_token_bytes(qr_text)
    if not token:
        await message.answer(f"Распознал код, но это не ссылка входа Telegram:\n{qr_text}")
        return

    status_msg = await message.answer("QR распознан, подтверждаю вход…")

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
        await status_msg.edit_text("QR-код уже истёк (живёт ~30 сек). Обнови QR и пришли скриншот заново.")
    except AuthTokenAlreadyAcceptedError:
        await status_msg.edit_text("Этот QR уже был подтверждён ранее.")
    except AuthTokenInvalidError:
        await status_msg.edit_text("Токен из QR недействителен — убедись, что это именно QR входа в Telegram.")
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
    await message.answer("Жду скриншот (фото или файл-картинку) с QR-кодом.")


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
