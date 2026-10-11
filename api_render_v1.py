import asyncio
import hashlib
import hmac
import json
import os
import re
from urllib.parse import parse_qsl

import psycopg
from aiohttp import web


BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_API_SECRET = os.environ.get("ADMIN_API_SECRET", "")


def init_db():
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY,
                reward INTEGER NOT NULL CHECK (reward > 0),
                active BOOLEAN NOT NULL DEFAULT TRUE,
                max_activations INTEGER CHECK (max_activations IS NULL OR max_activations > 0),
                activation_count INTEGER NOT NULL DEFAULT 0 CHECK (activation_count >= 0),
                created_by BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        # Миграция для уже существующей базы: добавляем лимит без потери промокодов.
        conn.execute("ALTER TABLE promo_codes ADD COLUMN IF NOT EXISTS max_activations INTEGER")
        conn.execute("ALTER TABLE promo_codes ADD COLUMN IF NOT EXISTS activation_count INTEGER NOT NULL DEFAULT 0")
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS promo_redemptions (
                code TEXT NOT NULL REFERENCES promo_codes(code),
                telegram_id BIGINT NOT NULL REFERENCES players(telegram_id),
                redeemed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (code, telegram_id)
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
            "Access-Control-Allow-Headers": "Content-Type, X-Telegram-Init-Data, X-Admin-Secret",
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



async def api_admin_create_promo(request):
    if not ADMIN_API_SECRET or not hmac.compare_digest(
        request.headers.get("X-Admin-Secret", ""), ADMIN_API_SECRET
    ):
        return cors({"ok": False, "error": "FORBIDDEN"}, 403)

    try:
        body = await request.json()
        code = str(body.get("code", "")).strip().upper()
        reward = int(body.get("reward", 0))
        created_by = int(body.get("created_by", 0))
        max_activations_raw = body.get("max_activations", 1)
        max_activations = None if max_activations_raw in (None, "unlimited", "∞") else int(max_activations_raw)
        if not re.fullmatch(r"[A-Z0-9_-]{3,32}", code):
            raise ValueError("invalid code")
        if not 1 <= reward <= 1_000_000 or created_by <= 0:
            raise ValueError("invalid reward or creator")
        if max_activations is not None and not 1 <= max_activations <= 1_000_000:
            raise ValueError("invalid activation limit")
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute(
                "INSERT INTO promo_codes (code, reward, max_activations, created_by) VALUES (%s, %s, %s, %s)",
                (code, reward, max_activations, created_by),
            )
            conn.commit()
        return cors({"ok": True, "code": code, "reward": reward, "max_activations": max_activations})
    except psycopg.errors.UniqueViolation:
        return cors({"ok": False, "error": "PROMO_EXISTS"}, 409)
    except (ValueError, TypeError, json.JSONDecodeError):
        return cors({"ok": False, "error": "INVALID_PROMO_DATA"}, 400)
    except Exception:
        return cors({"ok": False, "error": "DATABASE_ERROR"}, 500)


async def api_redeem_promo(request):
    user = validate_telegram_init_data(
        request.headers.get("X-Telegram-Init-Data", "")
    )
    if user is None:
        return cors({"ok": False, "error": "INVALID_TELEGRAM_DATA"}, 401)

    try:
        body = await request.json()
        code = str(body.get("code", "")).strip().upper()
        if not re.fullmatch(r"[A-Z0-9_-]{3,32}", code):
            raise ValueError("invalid code")

        get_or_create_player(user)
        with psycopg.connect(DATABASE_URL) as conn:
            # Блокировка строки промокода не даёт параллельным запросам превысить лимит.
            promo = conn.execute(
                "SELECT reward, max_activations, activation_count FROM promo_codes WHERE code = %s AND active = TRUE FOR UPDATE",
                (code,),
            ).fetchone()
            if promo is None or (promo[1] is not None and promo[2] >= promo[1]):
                conn.rollback()
                return cors({"ok": False, "error": "PROMO_NOT_FOUND"}, 404)

            inserted = conn.execute(
                "INSERT INTO promo_redemptions (code, telegram_id) VALUES (%s, %s) ON CONFLICT (code, telegram_id) DO NOTHING RETURNING code",
                (code, user["id"]),
            ).fetchone()
            if inserted is None:
                conn.rollback()
                return cors({"ok": False, "error": "PROMO_ALREADY_USED"}, 409)

            conn.execute(
                "UPDATE promo_codes SET activation_count = activation_count + 1 WHERE code = %s",
                (code,),
            )
            row = conn.execute(
                "UPDATE players SET balance = balance + %s, updated_at = NOW() WHERE telegram_id = %s RETURNING balance",
                (promo[0], user["id"]),
            ).fetchone()
            conn.commit()

        return cors({"ok": True, "reward": promo[0], "balance": row[0], "activation_count": promo[2] + 1, "max_activations": promo[1]})
    except (ValueError, TypeError, json.JSONDecodeError):
        return cors({"ok": False, "error": "INVALID_PROMO_DATA"}, 400)
    except Exception:
        return cors({"ok": False, "error": "DATABASE_ERROR"}, 500)




UPGRADE_CATALOG = {
    "Серый приговор": 200,
    "Токсичный коготь": 400,
    "Морозный иней": 600,
    "Ледяной раскол": 1200,
}


async def api_upgrade(request):
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user = validate_telegram_init_data(init_data)
    if user is None:
        return cors({"ok": False, "error": "INVALID_TELEGRAM_DATA"}, 401)

    try:
        body = await request.json()
        mode = body.get("mode")
        target_name = body.get("target_name")
        if mode not in ("coins", "item") or target_name not in UPGRADE_CATALOG:
            return cors({"ok": False, "error": "INVALID_UPGRADE"}, 400)

        amount = int(body.get("amount", 0))
        inventory_index = body.get("inventory_index")
        target_price = UPGRADE_CATALOG[target_name]

        import secrets
        with psycopg.connect(DATABASE_URL) as conn:
            row = conn.execute("""
                SELECT balance, inventory, total_upgrades, successful_upgrades
                FROM players WHERE telegram_id = %s FOR UPDATE
            """, (user["id"],)).fetchone()
            if row is None:
                return cors({"ok": False, "error": "PLAYER_NOT_FOUND"}, 404)

            balance = int(row[0])
            inventory = row[1] or []
            total_upgrades = int(row[2])
            successful_upgrades = int(row[3])

            if mode == "coins":
                stake_value = amount
                if amount <= 0 or amount > balance:
                    return cors({"ok": False, "error": "INSUFFICIENT_BALANCE"}, 400)
            else:
                try:
                    index = int(inventory_index)
                except (TypeError, ValueError):
                    return cors({"ok": False, "error": "INVALID_INVENTORY_ITEM"}, 400)
                if index < 0 or index >= len(inventory):
                    return cors({"ok": False, "error": "INVALID_INVENTORY_ITEM"}, 400)
                item = inventory[index]
                item_name = item.get("name") if isinstance(item, dict) else item
                stake_value = UPGRADE_CATALOG.get(item_name, 0)
                if not stake_value:
                    return cors({"ok": False, "error": "INVALID_INVENTORY_ITEM"}, 400)

            if target_price <= stake_value:
                return cors({"ok": False, "error": "TARGET_NOT_MORE_VALUABLE"}, 400)

            chance = stake_value / target_price * 100
            success = secrets.randbelow(1000000) < int(chance * 10000)

            if mode == "coins":
                balance -= stake_value
            else:
                inventory.pop(index)

            if success:
                inventory.append({"name": target_name})
                successful_upgrades += 1
            total_upgrades += 1

            conn.execute("""
                UPDATE players
                SET balance = %s, inventory = %s::jsonb,
                    total_upgrades = %s, successful_upgrades = %s,
                    updated_at = NOW()
                WHERE telegram_id = %s
            """, (balance, json.dumps(inventory, ensure_ascii=False),
                  total_upgrades, successful_upgrades, user["id"]))
            conn.commit()

        player = get_or_create_player(user)
        return cors({
            "ok": True,
            "success": success,
            "chance": round(chance, 4),
            "player": player,
        })
    except (ValueError, TypeError, json.JSONDecodeError):
        return cors({"ok": False, "error": "INVALID_UPGRADE_DATA"}, 400)
    except Exception:
        return cors({"ok": False, "error": "DATABASE_ERROR"}, 500)



def create_app():
    app = web.Application()

    app.router.add_get("/health", health)

    app.router.add_get("/api/player", api_player)
    app.router.add_post("/api/save-player", api_save_player)
    app.router.add_post("/api/upgrade", api_upgrade)
    app.router.add_post("/api/admin/create-promo", api_admin_create_promo)
    app.router.add_post("/api/redeem-promo", api_redeem_promo)
    app.router.add_options("/api/player", options)
    app.router.add_options("/api/save-player", options)
    app.router.add_options("/api/upgrade", options)
    app.router.add_options("/api/redeem-promo", options)

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
            
