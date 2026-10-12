import asyncio
import os
import re

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandStart
from aiogram.types import WebAppInfo

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_API_SECRET = os.getenv("ADMIN_API_SECRET", "").strip()
API_BASE_URL = os.getenv("API_BASE_URL", "https://oxidedrop-api.onrender.com").rstrip("/")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://baba2020a.github.io/oxidedrop/").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "5630820663"))
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN in Render Environment Variables")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    name = (message.from_user.first_name if message.from_user else None) or "Игрок"
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(
            text="🚀 Открыть Oxide Drop",
            web_app=WebAppInfo(url=WEBAPP_URL),
        )
    ]])
    await message.answer(
        f"Привет, {name}!\\n\\n<b>OXIDE DROP</b>\\nОткрывай кейсы и собирай арсенал!",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@dp.message(Command("createpromo"))
async def createpromo_cmd(message: types.Message):
    user = message.from_user
    if user is None or user.id != ADMIN_ID:
        await message.answer("⛔ У тебя нет доступа к этой команде.")
        return
    args = (message.text or "").split()
    if len(args) != 4:
        await message.answer("Использование:\\n/createpromo КОД КОИНЫ ЛИМИТ\\nПример: /createpromo OXIDE1000 1000 10")
        return
    code = args[1].strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{3,32}", code):
        await message.answer("❌ Код: 3–32 символа, только A–Z, 0–9, _ или -.")
        return
    try:
        reward, limit = int(args[2]), int(args[3])
        if not 1 <= reward <= 1_000_000 or not 1 <= limit <= 1_000_000:
            raise ValueError
    except ValueError:
        await message.answer("❌ Награда и лимит должны быть от 1 до 1000000.")
        return
    if not ADMIN_API_SECRET:
        await message.answer("❌ На сервере не задан ADMIN_API_SECRET.")
        return
    try:
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{API_BASE_URL}/api/admin/create-promo",
                json={"code": code, "reward": reward, "max_activations": limit, "created_by": user.id},
                headers={"X-Admin-Secret": ADMIN_API_SECRET},
            ) as response:
                try:
                    data = await response.json(content_type=None)
                except Exception:
                    data = {}
                status = response.status
        if status == 200 and data.get("ok"):
            await message.answer(
                f"✅ Промокод создан!\\n🎟 Код: <code>{code}</code>\\n🪙 Награда: <b>{reward}</b> коинов\\n👥 Лимит: <b>{limit}</b>",
                parse_mode="HTML",
            )
        elif status == 409 or data.get("error") == "PROMO_EXISTS":
            await message.answer("⚠️ Такой промокод уже существует.")
        elif status == 403:
            await message.answer("❌ API отклонил запрос. Проверь ADMIN_API_SECRET в Render.")
        else:
            await message.answer(f"❌ Ошибка API: {data.get('error', f'HTTP {status}')}")
    except (asyncio.TimeoutError, aiohttp.ClientError):
        await message.answer("❌ Не удалось связаться с API Render. Попробуй позже.")


async def health(request):
    return web.json_response({"ok": True, "service": "oxide-drop-bot"})


async def run_health_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    return runner


async def main():
    runner = await run_health_server()
    try:
        await bot.set_chat_menu_button(
            menu_button=types.MenuButtonWebApp(
                text="🚀 Oxide Drop", web_app=WebAppInfo(url=WEBAPP_URL)
            )
        )
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
