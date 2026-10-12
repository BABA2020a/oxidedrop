import asyncio
import hashlib
import hmac
import json
import random
import re
import sqlite3
from contextlib import closing
from urllib.parse import parse_qsl

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandStart
from aiogram.types import WebAppInfo, MenuButtonWebApp

# IMPORTANT:
# 1. Revoke the token previously exposed in chat via @BotFather.
# 2. Put your NEW token below and update the matching BOT_TOKEN on Render.
# 3. Keep secrets private; never publish this file with real credentials.

BOT_TOKEN = "PASTE_NEW_BOT_TOKEN_HERE"

GITHUB_NICK = "baba2020a"
REPO_NAME = "oxidedrop"
WEBAPP_URL = "https://baba2020a.github.io/oxidedrop/"

# Hosted API connected to Neon (used for promo-code creation).
API_BASE_URL = "https://oxidedrop-api.onrender.com"
ADMIN_ID = 5630820663
ADMIN_API_SECRET = "PASTE_ADMIN_API_SECRET_FROM_RENDER_HERE"

# This local SQLite API is separate from the hosted Render/Neon API.
DB_FILE = "oxide_drop.db"
API_HOST = "0.0.0.0"
API_PORT = 8080
CASE_PRICE = 500

PRIZES = [
    "Скин Oxide #1",
    "Скин Oxide #2",
    "Скин Oxide #3",
    "Скин Oxide #4",
]


# -------------------- LOCAL SQLITE DATABASE --------------------

def init_db():
    with closing(sqlite3.connect(DB_FILE)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS players (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT NOT NULL DEFAULT 'Игрок',
                balance INTEGER NOT NULL DEFAULT 5000,
                total_cases_opened INTEGER NOT NULL DEFAULT 0,
                total_upgrades INTEGER NOT NULL DEFAULT 0,
                successful_upgrades INTEGER NOT NULL DEFAULT 0,
                inventory TEXT NOT NULL DEFAULT '[]'
            )
        """)
        conn.commit()


def get_player(telegram_id):
    with closing(sqlite3.connect(DB_FILE)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM players WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        try:
            data["inventory"] = json.loads(data["inventory"])
        except (TypeError, json.JSONDecodeError):
            data["inventory"] = []
        return data


def create_or_update_player(user_id, username, first_name):
    with closing(sqlite3.connect(DB_FILE)) as conn:
        row = conn.execute(
            "SELECT telegram_id FROM players WHERE telegram_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            conn.execute("""
                INSERT INTO players (telegram_id, username, first_name)
                VALUES (?, ?, ?)
            """, (user_id, username, first_name or "Игрок"))
        else:
            conn.execute("""
                UPDATE players SET username = ?, first_name = ?
                WHERE telegram_id = ?
            """, (username, first_name or "Игрок", user_id))
        conn.commit()
    return get_player(user_id)


def level_data(total_cases):
    return {
        "level": (total_cases // 10) + 1,
        "progress": total_cases % 10,
        "progress_max": 10,
    }


# -------------------- TELEGRAM WEB APP AUTH --------------------

def validate_telegram_init_data(init_data: str):
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(pairs.items())
    )
    secret_key = hmac.new(
        b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
    ).digest()
    calculated_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        return None

    user_raw = pairs.get("user")
    if not user_raw:
        return None
    try:
        user = json.loads(user_raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(user, dict) or not user.get("id"):
        return None

    return {
        "id": int(user["id"]),
        "username": user.get("username"),
        "first_name": user.get("first_name") or "Игрок",
    }


# -------------------- LOCAL API HELPERS --------------------

def cors_response(data, status=200):
    return web.json_response(
        data,
        status=status,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type, X-Telegram-Init-Data",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        },
    )


async def options_handler(request):
    return cors_response({"ok": True})


def get_authenticated_user(request):
    return validate_telegram_init_data(
        request.headers.get("X-Telegram-Init-Data", "")
    )


async def api_player(request):
    user = get_authenticated_user(request)
    if user is None:
        return cors_response({"ok": False, "error": "INVALID_TELEGRAM_DATA"}, 401)
    player = create_or_update_player(user["id"], user["username"], user["first_name"])
    player.update(level_data(player["total_cases_opened"]))
    return cors_response({"ok": True, "player": player})


async def api_open_case(request):
    user = get_authenticated_user(request)
    if user is None:
        return cors_response({"ok": False, "error": "INVALID_TELEGRAM_DATA"}, 401)

    player = create_or_update_player(user["id"], user["username"], user["first_name"])
    if player["balance"] < CASE_PRICE:
        return cors_response({
            "ok": False,
            "error": "NOT_ENOUGH_COINS",
            "balance": player["balance"],
        }, 400)

    prize = random.choice(PRIZES)
    inventory = player["inventory"]
    inventory.append({"name": prize})
    new_balance = player["balance"] - CASE_PRICE
    new_cases = player["total_cases_opened"] + 1

    with closing(sqlite3.connect(DB_FILE)) as conn:
        conn.execute("""
            UPDATE players
            SET balance = ?, total_cases_opened = ?, inventory = ?
            WHERE telegram_id = ?
        """, (
            new_balance,
            new_cases,
            json.dumps(inventory, ensure_ascii=False),
            user["id"],
        ))
        conn.commit()

    player = get_player(user["id"])
    player.update(level_data(player["total_cases_opened"]))
    return cors_response({"ok": True, "prize": prize, "player": player})


async def api_delete_items(request):
    user = get_authenticated_user(request)
    if user is None:
        return cors_response({"ok": False, "error": "INVALID_TELEGRAM_DATA"}, 401)

    try:
        body = await request.json()
        indexes = body.get("indexes", [])
    except Exception:
        return cors_response({"ok": False, "error": "INVALID_JSON"}, 400)

    if not isinstance(indexes, list):
        return cors_response({"ok": False, "error": "INVALID_INDEXES"}, 400)

    player = create_or_update_player(user["id"], user["username"], user["first_name"])
    inventory = player["inventory"]
    try:
        indexes = sorted({
            int(index) for index in indexes
            if isinstance(index, int) or str(index).isdigit()
        }, reverse=True)
    except (ValueError, TypeError):
        return cors_response({"ok": False, "error": "INVALID_INDEXES"}, 400)

    for index in indexes:
        if 0 <= index < len(inventory):
            inventory.pop(index)

    with closing(sqlite3.connect(DB_FILE)) as conn:
        conn.execute(
            "UPDATE players SET inventory = ? WHERE telegram_id = ?",
            (json.dumps(inventory, ensure_ascii=False), user["id"]),
        )
        conn.commit()

    player = get_player(user["id"])
    player.update(level_data(player["total_cases_opened"]))
    return cors_response({"ok": True, "player": player})


async def health_handler(request):
    return cors_response({"ok": True, "service": "Oxide Drop local API"})


# -------------------- TELEGRAM BOT --------------------

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    user = message.from_user
    player = create_or_update_player(user.id, user.username, user.first_name)
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[
            types.InlineKeyboardButton(
                text="🚀 Открыть Oxide Drop",
                web_app=WebAppInfo(url=WEBAPP_URL),
            )
        ]]
    )
    await message.answer(
        f"Привет, {user.first_name}!\n\n"
        "<b>OXIDE DROP</b>\n"
        "Открывай кейсы и собирай арсенал!\n\n"
        f"🪙 Баланс локального профиля: <b>{player['balance']}</b>\n"
        f"📦 Открыто кейсов: <b>{player['total_cases_opened']}</b>\n"
        f"⭐ Уровень: <b>{level_data(player['total_cases_opened'])['level']}</b>",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@dp.message(Command("createpromo"))
async def createpromo_cmd(message: types.Message):
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        await message.answer("⛔ У тебя нет доступа к этой команде.")
        return

    args = (message.text or "").split()
    if len(args) != 4:
        await message.answer(
            "Использование:\n/createpromo КОД КОИНЫ ЛИМИТ\n"
            "Пример: /createpromo OXIDE1000 1000 10\n"
            "Последнее число — общее количество активаций."
        )
        return

    code = args[1].strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{3,32}", code):
        await message.answer("❌ Код: 3–32 символа, только A–Z, 0–9, _ или -.")
        return

    try:
        reward = int(args[2])
        if not 1 <= reward <= 1_000_000:
            raise ValueError
    except ValueError:
        await message.answer("❌ Награда должна быть от 1 до 1000000 коинов.")
        return

    try:
        max_activations = int(args[3])
        if not 1 <= max_activations <= 1_000_000:
            raise ValueError
    except ValueError:
        await message.answer("❌ Лимит активаций должен быть от 1 до 1000000.")
        return

    if not ADMIN_API_SECRET or ADMIN_API_SECRET == "PASTE_ADMIN_API_SECRET_FROM_RENDER_HERE":
        await message.answer("❌ Вставь ADMIN_API_SECRET из Render в начало файла.")
        return

    timeout = aiohttp.ClientTimeout(total=25)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{API_BASE_URL}/api/admin/create-promo",
                json={
                    "code": code,
                    "reward": reward,
                    "max_activations": max_activations,
                    "created_by": message.from_user.id,
                },
                headers={"X-Admin-Secret": ADMIN_API_SECRET},
            ) as response:
                try:
                    data = await response.json(content_type=None)
                except Exception:
                    data = {}
                status = response.status

        if status == 200 and data.get("ok"):
            await message.answer(
                f"✅ Промокод создан!\n\n"
                f"🎟 Код: <code>{code}</code>\n"
                f"🪙 Награда: <b>{reward}</b> коинов\n"
                f"👥 Лимит активаций: <b>{max_activations}</b>",
                parse_mode="HTML",
            )
        elif status == 409 or data.get("error") == "PROMO_EXISTS":
            await message.answer("⚠️ Такой промокод уже существует.")
        elif status == 403:
            await message.answer("❌ Render отклонил запрос. Проверь совпадение секретов.")
        else:
            await message.answer(
                f"❌ Ошибка API: {data.get('error', f'HTTP {status}')}"
            )
    except asyncio.TimeoutError:
        await message.answer("❌ Render не ответил за 25 секунд. Попробуй позже.")
    except aiohttp.ClientError:
        await message.answer("❌ Не удалось связаться с API Render.")
    except Exception:
        await message.answer("❌ Неожиданная ошибка при создании промокода.")


# -------------------- LOCAL API SERVER --------------------

def create_api_app():
    app = web.Application()
    app.router.add_route("OPTIONS", "/api/player", options_handler)
    app.router.add_route("GET", "/api/player", api_player)
    app.router.add_route("OPTIONS", "/api/open-case", options_handler)
    app.router.add_route("POST", "/api/open-case", api_open_case)
    app.router.add_route("OPTIONS", "/api/delete-items", options_handler)
    app.router.add_route("POST", "/api/delete-items", api_delete_items)
    app.router.add_route("GET", "/health", health_handler)
    return app


async def run_api():
    app = create_api_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, API_HOST, API_PORT)
    await site.start()
    print("---------------------------------------")
    print(f"Local API: http://127.0.0.1:{API_PORT}")
    print(f"Health check: http://127.0.0.1:{API_PORT}/health")
    print("---------------------------------------")
    await asyncio.Event().wait()


async def main():
    if BOT_TOKEN == "PASTE_NEW_BOT_TOKEN_HERE":
        raise RuntimeError("Вставь новый токен бота в BOT_TOKEN в начале файла.")
    if ADMIN_API_SECRET == "PASTE_ADMIN_API_SECRET_FROM_RENDER_HERE":
        print("ВНИМАНИЕ: ADMIN_API_SECRET не задан; команда /createpromo не заработает.")

    init_db()
    print("=======================================")
    print("       OXIDE DROP BOT + LOCAL API")
    print("=======================================")
    print(f"MiniApp: {WEBAPP_URL}")
    print(f"Local SQLite: {DB_FILE}")
    print(f"Render API for promo codes: {API_BASE_URL}")
    print(f"Local API port: {API_PORT}")
    print("Настраиваю постоянную кнопку меню Telegram Web App...")

    # Set the default menu button for the bot to open the Mini App.
    # This should be done before polling starts.
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(
            text="🚀 Oxide Drop",
            web_app=WebAppInfo(url=WEBAPP_URL),
        )
    )
    print("Кнопка меню настроена.")

    await asyncio.gather(dp.start_polling(bot), run_api())


if __name__ == "__main__":
    asyncio.run(main())
