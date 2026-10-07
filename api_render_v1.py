import asyncio
import hashlib
import hmac
import json
import os
from urllib.parse import parse_qsl

import psycopg
from aiohttp import web


BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]


def init_db():
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS players (
                telegram_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT NOT NULL DEFAULT 'Игрок',
                balance INTEGER NOT NULL DEFAULT 5000,
                total_cases_opened INTEGER NOT NULL DEFAULT 0,
                total_upgrades INTEGER NOT NULL DEFAULT 0,
                successful_upgrades INTEGER NOT NULL DEFAULT 0,
                inventory JSONB NOT NULL DEFAULT '[]'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.commit()


def player_level(total_cases: int):
    return (total_cases // 10) + 1, total_cases % 10


def get_or_create_player(user):
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute("""
            INSERT INTO players (telegram_id, username, first_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (telegram_id)
            DO UPDATE SET
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                updated_at = NOW()
        """, (
            user["id"],
            user.get("username"),
            user.get("first_name") or "Игрок",
        ))
        conn.commit()

        row = conn.execute("""
            SELECT telegram_id, username, first_name, balance,
                   total_cases_opened, total_upgrades,
                   successful_upgrades, inventory
            FROM players
            WHERE telegram_id = %s
        """, (user["id"],)).fetchone()

    level, progress = player_level(row[4])

    return {
        "telegram_id": row[0],
        "username": row[1],
        "first_name": row[2],
        "balance": row[3],
        "total_cases_opened": row[4],
        "total_upgrades": row[5],
        "successful_upgrades": row[6],
        "inventory": row[7] or [],
        "level": level,
        "progress": progress,
        "progress_max": 10,
    }


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
        b"WebAppData",
        BOT_TOKEN.encode(),
        hashlib.sha256,
    ).digest()

    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        return None

    try:
        user = json.loads(pairs["user"])
    except (KeyError, json.JSONDecodeError):
        return None

    if not isinstance(user, dict) or not user.get("id"):
        return None

    return {
        "id": int(user["id"]),
        "username": user.get("username"),
        "first_name": user.get("first_name") or "Игрок",
    }


def cors(data, status=200):
    return web.json_response(
        data,
        status=status,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type, X-Telegram-Init-Data",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        },
    )


async def options(request):
    return cors({"ok": True})


async def health(request):
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute("SELECT 1")
        return cors({"ok": True, "database": "connected"})
    except Exception:
        return cors({"ok": False, "database": "error"}, 500)


async def api_player(request):
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user = validate_telegram_init_data(init_data)

    if user is None:
        return cors({
            "ok": False,
            "error": "INVALID_TELEGRAM_DATA",
        }, 401)

    return cors({
        "ok": True,
        "player": get_or_create_player(user),
    })



async def api_save_player(request):
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user = validate_telegram_init_data(init_data)

    if user is None:
        return cors({
            "ok": False,
            "error": "INVALID_TELEGRAM_DATA",
        }, 401)

    try:
        body = await request.json()

        balance = int(body.get("balance", 0))
        total_cases_opened = int(body.get("total_cases_opened", 0))
        inventory = body.get("inventory", [])

        if balance < 0 or total_cases_opened < 0:
            raise ValueError("negative values")

        if not isinstance(inventory, list):
            raise ValueError("inventory must be a list")

        clean_inventory = []
        for item in inventory:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                clean_inventory.append({
                    "name": item["name"][:100]
                })
            elif isinstance(item, str):
                clean_inventory.append({
                    "name": item[:100]
                })

        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute("""
                UPDATE players
                SET balance = %s,
                    total_cases_opened = %s,
                    inventory = %s::jsonb,
                    updated_at = NOW()
                WHERE telegram_id = %s
            """, (
                balance,
                total_cases_opened,
                json.dumps(clean_inventory, ensure_ascii=False),
                user["id"],
            ))
            conn.commit()

        player = get_or_create_player(user)

        return cors({
            "ok": True,
            "player": player,
        })

    except (ValueError, TypeError, json.JSONDecodeError):
        return cors({
            "ok": False,
            "error": "INVALID_PLAYER_DATA",
        }, 400)
    except Exception:
        return cors({
            "ok": False,
            "error": "DATABASE_ERROR",
        }, 500)


def create_app():
    app = web.Application()

    app.router.add_get("/health", health)

    app.router.add_get("/api/player", api_player)
    app.router.add_post("/api/save-player", api_save_player)
    app.router.add_options("/api/player", options)
    app.router.add_options("/api/save-player", options)

    return app


async def main():
    init_db()

    port = int(os.environ.get("PORT", "10000"))
    app = create_app()

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print(f"Oxide Drop API started on port {port}")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
            
