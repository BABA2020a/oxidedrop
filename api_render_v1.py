import asyncio
import hashlib
import hmac
import json
import os
import re
import math
import secrets
from datetime import datetime, timezone, timedelta
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS crash_rounds (
                id BIGSERIAL PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'waiting',
                starts_at TIMESTAMPTZ NOT NULL,
                crash_at NUMERIC(10,2) NOT NULL,
                crashed_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS crash_bets (
                id BIGSERIAL PRIMARY KEY,
                round_id BIGINT NOT NULL REFERENCES crash_rounds(id) ON DELETE CASCADE,
                telegram_id BIGINT NOT NULL REFERENCES players(telegram_id),
                username TEXT,
                first_name TEXT NOT NULL DEFAULT 'Игрок',
                stake INTEGER NOT NULL CHECK (stake > 0),
                auto_cashout NUMERIC(10,2),
                payout INTEGER NOT NULL DEFAULT 0,
                cashout_at NUMERIC(10,2),
                status TEXT NOT NULL DEFAULT 'active',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(round_id, telegram_id)
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




CRASH_WAIT_SECONDS = 8
CRASH_NEXT_ROUND_SECONDS = 5
CRASH_MAX_MULTIPLIER = 100.0


def crash_multiplier(starts_at, now=None):
    now = now or datetime.now(timezone.utc)
    elapsed = max(0.0, (now - starts_at).total_seconds())
    return min(CRASH_MAX_MULTIPLIER, math.floor(math.exp(elapsed * 0.16) * 100) / 100)


def ensure_crash_round(conn):
    conn.execute("SELECT pg_advisory_xact_lock(734221)")
    row = conn.execute("SELECT id, status, starts_at, crash_at, crashed_at FROM crash_rounds ORDER BY id DESC LIMIT 1 FOR UPDATE").fetchone()
    now = datetime.now(timezone.utc)
    if row is None or (row[1] == 'crashed' and row[4] and (now-row[4]).total_seconds() >= CRASH_NEXT_ROUND_SECONDS):
        starts = now + timedelta(seconds=CRASH_WAIT_SECONDS)
        # Crash point generated server-side and never disclosed until the round ends.
        crash_at = round(1.0 + (secrets.randbelow(9901) / 100), 2)
        row_id = conn.execute("INSERT INTO crash_rounds(status, starts_at, crash_at) VALUES ('waiting', %s, %s) RETURNING id", (starts, crash_at)).fetchone()[0]
        row = (row_id, 'waiting', starts, crash_at, None)
    if row[1] != 'crashed' and now >= row[2]:
        current = crash_multiplier(row[2], now)
        if row[1] == 'waiting':
            conn.execute("UPDATE crash_rounds SET status='running' WHERE id=%s", (row[0],))
            row = (row[0], 'running', row[2], row[3], row[4])
        # Auto-cashouts reached before the crash must be paid first.
        auto_bets = conn.execute("""
            SELECT id, telegram_id, stake, auto_cashout FROM crash_bets
            WHERE round_id=%s AND status='active' AND auto_cashout IS NOT NULL
              AND auto_cashout <= %s AND auto_cashout < %s FOR UPDATE
        """, (row[0], current, float(row[3]))).fetchall()
        for bet_id, telegram_id, stake, target in auto_bets:
            payout = int(stake * float(target))
            changed = conn.execute("UPDATE crash_bets SET status='cashed_out', payout=%s, cashout_at=%s WHERE id=%s AND status='active' RETURNING id", (payout, target, bet_id)).fetchone()
            if changed:
                conn.execute("UPDATE players SET balance=balance+%s, updated_at=NOW() WHERE telegram_id=%s", (payout, telegram_id))
        if current >= float(row[3]):
            conn.execute("UPDATE crash_rounds SET status='crashed', crashed_at=%s WHERE id=%s", (now, row[0]))
            conn.execute("UPDATE crash_bets SET status='lost' WHERE round_id=%s AND status='active'", (row[0],))
            row = (row[0], 'crashed', row[2], row[3], now)
    return row


def crash_state_payload(conn, round_row, user_id=None):
    rid, status, starts_at, crash_at, crashed_at = round_row
    now = datetime.now(timezone.utc)
    multiplier = 1.0 if now < starts_at else crash_multiplier(starts_at, now)
    if status == 'crashed': multiplier = float(crash_at)
    bets = conn.execute("""
        SELECT telegram_id, username, first_name, stake, payout, cashout_at, status
        FROM crash_bets WHERE round_id=%s ORDER BY created_at DESC LIMIT 100
    """, (rid,)).fetchall()
    total = sum(int(b[3]) for b in bets)
    public_bets = [{"telegram_id":b[0],"username":b[1],"first_name":b[2],"stake":b[3],"payout":b[4],"cashout_at":float(b[5]) if b[5] is not None else None,"status":b[6]} for b in bets]
    own = next((b for b in public_bets if b["telegram_id"] == user_id), None)
    return {"id":rid,"status":status,"starts_at":starts_at.isoformat(),"server_time":now.isoformat(),"multiplier":round(multiplier,2),"crash_at":float(crash_at) if status=='crashed' else None,"players_count":len(bets),"total_stake":total,"bets":public_bets,"my_bet":own}


async def crash_state(request):
    user = validate_telegram_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    if user is None: return cors({"ok":False,"error":"INVALID_TELEGRAM_DATA"},401)
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.transaction():
                row = ensure_crash_round(conn)
                state = crash_state_payload(conn, row, user["id"])
        return cors({"ok":True,"state":state,"player":get_or_create_player(user)})
    except Exception as exc:
        print(f"crash_state error: {exc}")
        return cors({"ok":False,"error":"CRASH_DATABASE_ERROR"},500)


async def crash_bet(request):
    user = validate_telegram_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    if user is None: return cors({"ok":False,"error":"INVALID_TELEGRAM_DATA"},401)
    try:
        body = await request.json()
        stake = int(body.get("stake", 0))
        auto = body.get("auto_cashout")
        auto = round(float(auto), 2) if auto not in (None, "", 0, "0") else None
        if stake < 1 or stake > 1_000_000 or (auto is not None and not 1.01 <= auto <= 100):
            return cors({"ok":False,"error":"INVALID_BET"},400)
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.transaction():
                row = ensure_crash_round(conn)
                if row[1] != 'waiting' or datetime.now(timezone.utc) >= row[2]:
                    return cors({"ok":False,"error":"BETTING_CLOSED"},409)
                player = conn.execute("SELECT balance FROM players WHERE telegram_id=%s FOR UPDATE",(user["id"],)).fetchone()
                if not player: return cors({"ok":False,"error":"PLAYER_NOT_FOUND"},404)
                if player[0] < stake: return cors({"ok":False,"error":"NOT_ENOUGH_COINS"},400)
                inserted = conn.execute("INSERT INTO crash_bets(round_id,telegram_id,username,first_name,stake,auto_cashout) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(round_id,telegram_id) DO NOTHING RETURNING id",(row[0],user["id"],user.get("username"),user.get("first_name") or "Игрок",stake,auto)).fetchone()
                if not inserted:
                    return cors({"ok":False,"error":"BET_ALREADY_PLACED"},409)
                conn.execute("UPDATE players SET balance=balance-%s,updated_at=NOW() WHERE telegram_id=%s",(stake,user["id"]))
        return cors({"ok":True,"player":get_or_create_player(user)})
    except (ValueError, TypeError, json.JSONDecodeError):
        return cors({"ok":False,"error":"INVALID_BET"},400)
    except Exception as exc:
        print(f"crash_bet error: {exc}")
        return cors({"ok":False,"error":"CRASH_DATABASE_ERROR"},500)


async def crash_cashout(request):
    user = validate_telegram_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    if user is None: return cors({"ok":False,"error":"INVALID_TELEGRAM_DATA"},401)
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.transaction():
                row = ensure_crash_round(conn)
                bet = conn.execute("SELECT id,stake,status,auto_cashout FROM crash_bets WHERE round_id=%s AND telegram_id=%s FOR UPDATE",(row[0],user["id"])).fetchone()
                if not bet: return cors({"ok":False,"error":"NO_BET"},404)
                if bet[2] != 'active': return cors({"ok":False,"error":"BET_NOT_ACTIVE"},409)
                now = datetime.now(timezone.utc)
                mult = crash_multiplier(row[2], now)
                if row[1] == 'crashed' or now < row[2] or mult >= float(row[3]):
                    conn.execute("UPDATE crash_bets SET status='lost' WHERE id=%s",(bet[0],))
                    return cors({"ok":False,"error":"TOO_LATE"},409)
                if bet[3] is not None: mult = min(mult, float(bet[3]))
                payout = int(bet[1] * mult)
                conn.execute("UPDATE crash_bets SET status='cashed_out',payout=%s,cashout_at=%s WHERE id=%s",(payout,mult,bet[0]))
                conn.execute("UPDATE players SET balance=balance+%s,updated_at=NOW() WHERE telegram_id=%s",(payout,user["id"]))
        return cors({"ok":True,"multiplier":round(mult,2),"payout":payout,"player":get_or_create_player(user)})
    except Exception as exc:
        print(f"crash_cashout error: {exc}")
        return cors({"ok":False,"error":"CRASH_DATABASE_ERROR"},500)


def create_app():
    app = web.Application()

    app.router.add_get("/health", health)

    app.router.add_get("/api/player", api_player)
    app.router.add_get("/api/crash/state", crash_state)
    app.router.add_post("/api/crash/bet", crash_bet)
    app.router.add_post("/api/crash/cashout", crash_cashout)
    app.router.add_post("/api/save-player", api_save_player)
    app.router.add_post("/api/upgrade", api_upgrade)
    app.router.add_post("/api/admin/create-promo", api_admin_create_promo)
    app.router.add_post("/api/redeem-promo", api_redeem_promo)
    app.router.add_options("/api/player", options)
    app.router.add_options("/api/crash/state", options)
    app.router.add_options("/api/crash/bet", options)
    app.router.add_options("/api/crash/cashout", options)
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
            
