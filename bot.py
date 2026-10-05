"""Burnt Kingdoms Telegram bot and Mini App API.

The player table intentionally remains the original (uid, JSON data) table.
New player fields are added lazily by normalize_player; no game data is reset.
"""

import asyncio
import hashlib
import hmac
import json
import os
import random
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlparse
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from babel import Locale
from dotenv import load_dotenv

from game_data import (
    BANK_TRANSFER_LIMIT,
    BUILDINGS,
    COUNTRIES,
    LOAN_LIMIT,
    START,
    TECHS,
    TRADE_PP,
    UNITS,
    WAR_PP,
)


# ---------------------------------------------------------------------------
# Configuration and persistent storage
# ---------------------------------------------------------------------------

load_dotenv()
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
URL = os.environ.get("WEBAPP_URL", "").strip()
ADMINS = {
    int(value.strip())
    for value in os.environ.get("ADMINS", "").split(",")
    if value.strip().isdigit()
}
CHATS = [
    value.strip()
    for value in os.environ.get(
        "REQUIRED_CHATS", "@bk_chatt,@_burnt_kingdoms"
    ).split(",")
    if value.strip()
]
CHANNEL = os.environ.get("ANNOUNCE_CHANNEL", "@_burnt_kingdoms").strip()
BACKGROUND_MUSIC_URL = os.environ.get("BACKGROUND_MUSIC_URL", "").strip()
DB_PATH = os.environ.get("DB_PATH", "bk.db")
TZ = ZoneInfo("Asia/Tehran")
FA = Locale("fa").territories
bot = Bot(TOKEN) if TOKEN else None
dp = Dispatcher()
ACTION_LOCK = asyncio.Lock()

db = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
db.execute("PRAGMA busy_timeout = 30000")
db.execute("PRAGMA journal_mode = WAL")
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS p(uid INTEGER PRIMARY KEY, d TEXT);
    CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS pending_wars (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        attacker_uid INTEGER,
        defender_uid INTEGER,
        attacker_country TEXT,
        defender_country TEXT,
        units TEXT,
        scenario TEXT,
        reason TEXT,
        status TEXT DEFAULT 'pending',
        created_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS rq(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        uid INTEGER,
        kind TEXT,
        body TEXT,
        st TEXT DEFAULT 'pending'
    );
    CREATE TABLE IF NOT EXISTS season_archives(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        season TEXT NOT NULL,
        players_json TEXT NOT NULL,
        requests_json TEXT NOT NULL
    );
    """
)
db.commit()


def normalize_player(player):
    """Supply new fields without replacing any existing value or inventory."""
    player.setdefault("b", {})
    player.setdefault("u", {"infantry": 100})
    player.setdefault("mf", {})
    player.setdefault("t", [])
    player.setdefault("alive", True)
    player.setdefault("nuke_ok", False)
    player.setdefault("wt", 0)
    player.setdefault("money", START["money"])
    player.setdefault("pp", START["pp"])
    player.setdefault("food", START["food"])
    player.setdefault("happy", START["happy"])
    player.setdefault("name", "")
    player.setdefault("username", "")
    player.setdefault("c", "")
    # Bank and diplomacy data are additive migrations. Legacy `sanc` data is
    # intentionally left untouched for compatibility but is no longer used.
    player.setdefault("vault", 0)
    player.setdefault("loan", 0)
    player.setdefault("loan_original", 0)
    player.setdefault("bank_sent_date", "")
    player.setdefault("bank_sent_today", 0)
    player.setdefault("embassies", [])
    player.setdefault("hosted_embassies", [])
    player.setdefault("access", [])
    player.setdefault("access_grants", [])
    player.setdefault("diplomacy_log", [])
    player.setdefault("statement_history", [])
    player.setdefault("stmt_strikes", 0)
    player.setdefault("stmt_suspended_until", 0)
    player.setdefault("tx", "")
    return player


def telegram_display_name(user):
    parts = [
        str(user.get("first_name") or "").strip(),
        str(user.get("last_name") or "").strip(),
    ]
    return " ".join(part for part in parts if part)[:80]


def sync_player_profile(player, user):
    """Refresh non-sensitive Telegram display fields without touching game data."""
    name = telegram_display_name(user)
    username = str(user.get("username") or "").strip().lstrip("@")[:32]
    changed = False
    if name and player.get("name") != name:
        player["name"] = name
        changed = True
    if player.get("username", "") != username:
        player["username"] = username
        changed = True
    return changed


def kget(key, default=None):
    row = db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return row[0] if row else default


def kset(key, value):
    db.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (key, str(value)))
    db.commit()


def pget(uid):
    row = db.execute("SELECT d FROM p WHERE uid=?", (uid,)).fetchone()
    return normalize_player(json.loads(row[0])) if row else None


def pset(uid, player):
    normalize_player(player)
    db.execute(
        "INSERT OR REPLACE INTO p(uid,d) VALUES(?,?)",
        (uid, json.dumps(player, ensure_ascii=False)),
    )
    db.commit()


def allp():
    return [
        (uid, normalize_player(json.loads(data)))
        for uid, data in db.execute("SELECT uid,d FROM p").fetchall()
    ]


def by_code(code):
    code = str(code or "").upper()
    return next(
        ((uid, player) for uid, player in allp() if player.get("c") == code),
        (None, None),
    )


# ---------------------------------------------------------------------------
# Common helpers and Mini App authentication
# ---------------------------------------------------------------------------

def flag(code):
    return "".join(chr(127397 + ord(char)) for char in code)


def cname(code):
    return f"{flag(code)} {FA.get(code, code)}"


def market_open():
    return 8 <= datetime.now(TZ).hour < 24


def safe_int(value, default=0):
    try:
        # Do not silently accept booleans as amounts.
        if isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def cap(player):
    return sum(
        safe_int(player["b"].get(key)) * data.get("cap", 0)
        for key, data in BUILDINGS.items()
        if data.get("cap")
    )


def used(player):
    return sum(
        UNITS[key]["size"] * max(0, safe_int(amount))
        for key, amount in player.get("u", {}).items()
        if key in UNITS
    )


def pay(player, amount):
    amount = max(0, safe_int(amount))
    if safe_int(player.get("money")) < amount:
        return "پول کافی نیست."
    player["money"] -= amount
    return None


def has_building(player, key):
    return safe_int(player.get("b", {}).get(key)) > 0


def has_logistics(player):
    return has_building(player, "port") or has_building(player, "airport")


def trade_fee_rate(player):
    discount = sum(
        safe_int(player.get("b", {}).get(key))
        * data.get("fee_discount", 0)
        for key, data in BUILDINGS.items()
    )
    if "trade_1" in player.get("t", []):
        discount += 0.01
    return max(0.005, 0.05 - discount)


def fee_for_trade(player, other, value):
    return int(max(0, value) * (trade_fee_rate(player) + trade_fee_rate(other)) / 2)


def auth(init_data):
    if not TOKEN:
        print("[miniapp-auth] rejected: bot_token_missing")
        return None
    if not init_data:
        print("[miniapp-auth] rejected: init_data_missing")
        return None
    try:
        fields = dict(parse_qsl(init_data, keep_blank_values=True, max_num_fields=64))
        received_hash = fields.pop("hash", "")
        if not received_hash:
            print("[miniapp-auth] rejected: hash_missing")
            return None
        secret = hmac.new(
            b"WebAppData", TOKEN.encode("utf-8"), hashlib.sha256
        ).digest()
        # Telegram clients may include the newer `signature` parameter. Verify
        # both documented payload shapes: older HMAC data with every field
        # except hash, and clients where signature is an auxiliary field.
        candidates = [fields]
        if "signature" in fields:
            candidates.append(
                {key: value for key, value in fields.items() if key != "signature"}
            )
        valid_hash = any(
            hmac.compare_digest(
                hmac.new(
                    secret,
                    "\n".join(
                        f"{key}={value}" for key, value in sorted(candidate.items())
                    ).encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest(),
                received_hash,
            )
            for candidate in candidates
        )
        if not valid_hash:
            field_names = ",".join(sorted(fields.keys()))
            print(
                "[miniapp-auth] rejected: hash_mismatch "
                f"signature_present={'signature' in fields} fields={field_names}"
            )
            return None
        auth_date = safe_int(fields.get("auth_date"))
        now = time.time()
        if auth_date <= 0:
            print("[miniapp-auth] rejected: auth_date_invalid")
            return None
        if now - auth_date > 86_400:
            print(f"[miniapp-auth] rejected: auth_date_expired age_seconds={int(now - auth_date)}")
            return None
        if auth_date - now > 300:
            print("[miniapp-auth] rejected: auth_date_in_future")
            return None
        user = json.loads(fields["user"])
        if not isinstance(user, dict) or not safe_int(user.get("id")):
            print("[miniapp-auth] rejected: user_invalid")
            return None
        return user
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"[miniapp-auth] rejected: malformed_payload error={type(exc).__name__}")
        return None


async def is_member(uid):
    if bot is None:
        return True
    for chat in CHATS:
        try:
            member = await bot.get_chat_member(chat, uid)
            if member.status in ("left", "kicked"):
                return False
        except Exception as exc:
            print(f"membership check failed for {chat}: {exc}")
            return False
    return True


async def announce(text):
    if bot is None:
        return False
    try:
        await bot.send_message(CHANNEL, text)
        return True
    except Exception as exc:
        print("channel announcement failed:", exc)
        return False


async def create_admin_request(uid, kind, body):
    cursor = db.execute(
        "INSERT INTO rq(uid,kind,body) VALUES(?,?,?)",
        (uid, kind, json.dumps(body, ensure_ascii=False)),
    )
    db.commit()
    request_id = cursor.lastrowid
    keyboard = None
    if kind == "nuke":
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ تایید", callback_data=f"ok:{request_id}"
                    ),
                    InlineKeyboardButton(
                        text="❌ رد", callback_data=f"no:{request_id}"
                    ),
                ]
            ]
        )
    player = pget(uid)
    who = cname(player["c"]) if player else str(uid)
    for admin in ADMINS:
        try:
            await bot.send_message(
                admin,
                f"📨 درخواست {kind} از {who}\n\n{body.get('text', '')}",
                reply_markup=keyboard,
            )
        except Exception as exc:
            print("admin notification failed:", exc)
    return request_id


def request_message(body):
    details = {
        "talk": "درخواست مذاکره",
        "embassy": "درخواست تأسیس سفارت",
        "access": "درخواست دسترسی نظامی",
        "trade": "پیشنهاد تجارت",
        "bank_transfer": "درخواست انتقال بانکی",
    }
    text = f"🤝 {details.get(body.get('kind'), 'درخواست دیپلماتیک')}"
    if body.get("message"):
        text += f"\n\n{body['message']}"
    if body.get("give"):
        give = body["give"]
        want = body["want"]
        text += (
            f"\n\nپیشنهاد: {asset_label(give)}"
            f"\nدرخواست: {asset_label(want)}"
            f"\nکارمزد تقریبی: {body.get('fee', 0):,}$"
        )
    if body.get("amount"):
        text += f"\n\nمبلغ انتقال: {body['amount']:,}$"
    return text


def asset_label(asset):
    if asset["kind"] == "money":
        return f"{asset['amount']:,}$"
    unit = UNITS.get(asset.get("id"), {})
    return f"{safe_int(asset.get('amount')):,} × {unit.get('fa', asset.get('id', '?'))}"


async def create_diplomacy_request(uid, kind, body):
    cursor = db.execute(
        "INSERT INTO rq(uid,kind,body) VALUES(?,?,?)",
        (uid, kind, json.dumps(body, ensure_ascii=False)),
    )
    db.commit()
    request_id = cursor.lastrowid
    target_uid = body["target_uid"]
    if bot is not None:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ پذیرش", callback_data=f"dreq:yes:{request_id}"
                    ),
                    InlineKeyboardButton(
                        text="❌ رد", callback_data=f"dreq:no:{request_id}"
                    ),
                ]
            ]
        )
        try:
            await bot.send_message(
                target_uid,
                f"📩 پیشنهاد دیپلماتیک از {cname(body['from_country'])}\n"
                f"{request_message(body)}\n\n"
                "پاسخ را از همین دکمه یا بخش مدیریت کشور ثبت کنید.",
                reply_markup=keyboard,
            )
        except Exception as exc:
            # The recipient can still find and answer it in the Mini App.
            print("diplomacy notification failed:", exc)
    return request_id


# ---------------------------------------------------------------------------
# Game actions
# ---------------------------------------------------------------------------

async def a_state(uid, user, player, body):
    return None


async def a_pick(uid, user, player, body):
    country = str(body.get("c", "")).upper()
    if player:
        return "قبلاً کشور انتخاب کرده‌اید."
    if country not in COUNTRIES:
        return "کشور نامعتبر است."
    if by_code(country)[0]:
        return "این کشور قبلاً انتخاب شده است."
    initial = {
        **START,
        "c": country,
        "name": telegram_display_name(user),
        "username": str(user.get("username") or "").strip().lstrip("@")[:32],
        "b": {},
        "u": {"infantry": 100},
        "mf": {},
        "t": [],
        "alive": True,
        "nuke_ok": False,
        "wt": 0,
        "tx": "",
    }
    pset(uid, initial)
    return None


async def a_build(uid, user, player, body):
    key = str(body.get("id", ""))
    data = BUILDINGS.get(key)
    if not data:
        return "ساختمان نامعتبر است."
    if key == "bank" and has_building(player, "bank"):
        return "هر کشور فقط یک بانک ملی می‌تواند داشته باشد."
    if key in ("port", "airport") and has_building(player, key):
        return "این زیرساخت را قبلاً ساخته‌اید."
    if error := pay(player, data["cost"]):
        return error
    player["b"][key] = safe_int(player["b"].get(key)) + 1
    player["happy"] = min(
        100, player["happy"] + data.get("happy", 0)
    )
    pset(uid, player)
    return None


async def a_buy(uid, user, player, body):
    key = str(body.get("id", ""))
    amount = safe_int(body.get("n"))
    data = UNITS.get(key)
    if not data or not 1 <= amount <= 5000:
        return "تعداد یا نوع تجهیزات نامعتبر است."
    if not market_open():
        return "بازار تسلیحات از ساعت ۸ صبح تا ۱۲ شب به وقت تهران باز است."
    if data.get("lock") and not player.get("nuke_ok"):
        return "این تجهیزات قفل است؛ ابتدا مجوز سازمان انرژی اتمی را بگیرید."
    if used(player) + data["size"] * amount > cap(player):
        return "ظرفیت انبار تسلیحات کافی نیست؛ ابتدا انبار بسازید یا ظرفیت خالی کنید."
    if error := pay(player, data["cost"] * amount):
        return error
    player["u"][key] = safe_int(player["u"].get(key)) + amount
    pset(uid, player)
    return None


async def a_mf(uid, user, player, body):
    key = str(body.get("id", ""))
    data = UNITS.get(key)
    if not data:
        return "نوع کارخانه نامعتبر است."
    if not has_building(player, "arsenal"):
        return "برای ذخیره تجهیزات ابتدا انبار تسلیحات بسازید."
    if data.get("lock") and not player.get("nuke_ok"):
        return "تولید این تجهیزات قفل است؛ ابتدا مجوز مربوطه را بگیرید."
    if error := pay(player, data["fcost"]):
        return error
    player["mf"][key] = safe_int(player["mf"].get(key)) + 1
    pset(uid, player)
    return None


async def a_bank(uid, user, player, body):
    if not has_building(player, "bank"):
        return "برای استفاده از خدمات بانکی ابتدا بانک ملی بسازید."
    action = str(body.get("kind", ""))
    amount = safe_int(body.get("amount"))
    if not 1_000 <= amount <= 250_000:
        return "مبلغ باید بین ۱٬۰۰۰ تا ۲۵۰٬۰۰۰ دلار باشد."
    if action == "deposit":
        if error := pay(player, amount):
            return error
        player["vault"] = safe_int(player.get("vault")) + amount
    elif action == "withdraw":
        if safe_int(player.get("vault")) < amount:
            return "موجودی سپرده کافی نیست."
        player["vault"] -= amount
        player["money"] += amount
    elif action == "loan":
        if safe_int(player.get("loan")) > 0:
            return "ابتدا وام فعلی را تسویه کنید."
        if amount > LOAN_LIMIT:
            return f"حداکثر وام قابل دریافت {LOAN_LIMIT:,}$ است."
        player["loan"] = amount
        player["loan_original"] = amount
        player["money"] += amount
    elif action == "repay":
        debt = safe_int(player.get("loan"))
        if debt <= 0:
            return "وام فعالی ندارید."
        payment = min(amount, debt)
        if error := pay(player, payment):
            return error
        player["loan"] -= payment
        if player["loan"] <= 0:
            player["loan"] = 0
            player["loan_original"] = 0
    else:
        return "عملیات بانکی نامعتبر است."
    pset(uid, player)
    return None


async def a_tax(uid, user, player, body):
    today = str(datetime.now(TZ).date())
    if player.get("tx") == today:
        return "امروز مالیات گرفته‌اید."
    player["tx"] = today
    player["money"] += 35_000
    player["happy"] = max(0, player["happy"] - 4)
    pset(uid, player)
    return None


async def a_tech(uid, user, player, body):
    key = str(body.get("id", ""))
    data = TECHS.get(key)
    if not data or key in player["t"]:
        return "فناوری نامعتبر است یا قبلاً آن را گرفته‌اید."
    if data["req"] and data["req"] not in player["t"]:
        return "پیش‌نیاز این فناوری را ندارید."
    if player["pp"] < data["pp"]:
        return "قدرت سیاسی کافی نیست."
    if error := pay(player, data["money"]):
        return error
    player["pp"] -= data["pp"]
    player["t"].append(key)
    pset(uid, player)
    return None


def valid_target(uid, country):
    target_uid, target = by_code(country)
    if not target or target_uid == uid or not target.get("alive"):
        return None, None
    return target_uid, target


async def a_diplomacy(uid, user, player, body):
    kind = str(body.get("kind", ""))
    country = str(body.get("c", "")).upper()
    target_uid, target = valid_target(uid, country)
    if not target:
        return "کشور مقصد معتبر نیست."
    clean_text = str(body.get("text", "")).strip()[:800]
    proposal = {
        "target_uid": target_uid,
        "target_country": country,
        "from_country": player["c"],
        "kind": kind,
    }

    if kind == "talk":
        if len(clean_text) < 5:
            return "متن مذاکره باید حداقل ۵ حرف باشد."
        proposal["message"] = clean_text
    elif kind in ("embassy", "access"):
        list_key = "embassies" if kind == "embassy" else "access"
        pp_cost = 10 if kind == "embassy" else 8
        if country in player[list_key]:
            return "این مجوز یا سفارت از قبل ثبت شده است."
        if player["pp"] < pp_cost:
            return "قدرت سیاسی کافی نیست."
        proposal["pp_cost"] = pp_cost
        proposal["message"] = clean_text
    elif kind == "trade":
        give_kind = str(body.get("give_kind", ""))
        want_kind = str(body.get("want_kind", ""))
        give_id = str(body.get("give_id", "money"))
        want_id = str(body.get("want_id", "money"))
        give_amount = safe_int(body.get("give_amount"))
        want_amount = safe_int(body.get("want_amount"))
        if give_kind not in ("money", "unit") or want_kind not in ("money", "unit"):
            return "نوع دارایی پیشنهادشده معتبر نیست."
        if give_amount <= 0 or want_amount <= 0:
            return "مقدار هر دو طرف معامله باید بیشتر از صفر باشد."
        if give_kind == "unit" and give_id not in UNITS:
            return "تجهیزات پیشنهادی معتبر نیست."
        if want_kind == "unit" and want_id not in UNITS:
            return "تجهیزات درخواستی معتبر نیست."
        if give_kind == "money" and safe_int(player.get("b", {}).get("bank")) < 1:
            return "برای معامله پولی باید بانک بسازید."
        if want_kind == "money" and safe_int(target.get("b", {}).get("bank")) < 1:
            return "کشور مقصد برای معامله پولی باید بانک داشته باشد."
        if give_kind == "money" and safe_int(target.get("b", {}).get("bank")) < 1:
            return "کشور مقصد برای معامله پولی باید بانک داشته باشد."
        if want_kind == "money" and safe_int(player.get("b", {}).get("bank")) < 1:
            return "برای معامله پولی باید بانک بسازید."
        contains_hardware = give_kind == "unit" or want_kind == "unit"
        if contains_hardware and (not has_logistics(player) or not has_logistics(target)):
            return "برای تجارت تجهیزات، هر دو کشور باید بندر یا فرودگاه تجاری داشته باشند."
        if give_kind == "unit" and give_amount > safe_int(player["u"].get(give_id)):
            return "موجودی تجهیزات پیشنهادی شما کافی نیست."
        if want_kind == "unit" and want_amount > safe_int(target["u"].get(want_id)):
            return "موجودی تجهیزات مورد درخواست کشور مقصد کافی نیست."
        if any(
            kind_ == "unit" and id_ == "nuke"
            for kind_, id_ in ((give_kind, give_id), (want_kind, want_id))
        ):
            if not player.get("nuke_ok") or not target.get("nuke_ok"):
                return "انتقال بمب اتم فقط میان کشورهایی با مجوز فعال مجاز است."
        give = {"kind": give_kind, "id": give_id, "amount": give_amount}
        want = {"kind": want_kind, "id": want_id, "amount": want_amount}
        value_give = (
            give_amount
            if give_kind == "money"
            else UNITS[give_id]["cost"] * give_amount
        )
        value_want = (
            want_amount
            if want_kind == "money"
            else UNITS[want_id]["cost"] * want_amount
        )
        fee = fee_for_trade(player, target, max(value_give, value_want))
        proposal.update(
            give=give,
            want=want,
            fee=fee,
            message=clean_text,
        )
        if player["pp"] < TRADE_PP:
            return "قدرت سیاسی کافی برای ثبت پیشنهاد تجارت ندارید."
    else:
        return "نوع درخواست دیپلماتیک نامعتبر است."

    await create_diplomacy_request(uid, kind, proposal)
    return None


async def a_bank_transfer(uid, user, player, body):
    if not has_building(player, "bank"):
        return "برای انتقال پول باید بانک ملی بسازید."
    country = str(body.get("c", "")).upper()
    target_uid, target = valid_target(uid, country)
    if not target:
        return "کشور مقصد معتبر نیست."
    if not has_building(target, "bank"):
        return "کشور مقصد باید بانک ملی داشته باشد."
    amount = safe_int(body.get("amount"))
    if amount < 1_000 or amount > BANK_TRANSFER_LIMIT:
        return f"مبلغ باید بین ۱٬۰۰۰ تا {BANK_TRANSFER_LIMIT:,}$ باشد."
    today = str(datetime.now(TZ).date())
    sent_today = (
        safe_int(player.get("bank_sent_today"))
        if player.get("bank_sent_date") == today
        else 0
    )
    pending_rows = db.execute(
        "SELECT body FROM rq WHERE uid=? AND kind='bank_transfer' AND st='pending'",
        (uid,),
    ).fetchall()
    pending_amount = sum(
        safe_int(json.loads(row[0]).get("amount"))
        for row in pending_rows
    )
    if sent_today + pending_amount + amount > BANK_TRANSFER_LIMIT:
        return f"سقف انتقال روزانه {BANK_TRANSFER_LIMIT:,}$ است."
    if safe_int(player.get("money")) < pending_amount + amount:
        return "با احتساب درخواست‌های انتقالِ در انتظار، موجودی پول نقد کافی نیست."
    proposal = {
        "target_uid": target_uid,
        "target_country": country,
        "from_country": player["c"],
        "kind": "bank_transfer",
        "amount": amount,
        "message": str(body.get("text", "")).strip()[:300],
    }
    await create_diplomacy_request(uid, "bank_transfer", proposal)
    return None


def unit_value(unit_id, amount):
    if unit_id not in UNITS:
        return 0
    return UNITS[unit_id]["cost"] * max(0, safe_int(amount))


def apply_asset(source, destination, asset):
    amount = safe_int(asset.get("amount"))
    if asset["kind"] == "money":
        if safe_int(source.get("money")) < amount:
            return "موجودی پول یکی از طرفین دیگر کافی نیست."
        source["money"] -= amount
        destination["money"] += amount
        return None
    unit_id = asset.get("id")
    data = UNITS.get(unit_id)
    if not data or safe_int(source.get("u", {}).get(unit_id)) < amount:
        return "موجودی تجهیزات یکی از طرفین دیگر کافی نیست."
    if used(destination) + data["size"] * amount > cap(destination):
        return f"ظرفیت انبار {cname(destination['c'])} برای دریافت تجهیزات کافی نیست."
    source["u"][unit_id] -= amount
    destination["u"][unit_id] = safe_int(destination["u"].get(unit_id)) + amount
    return None


def record_diplomacy(player, country, kind, message=""):
    history = player.setdefault("diplomacy_log", [])
    history.append(
        {
            "country": country,
            "kind": kind,
            "message": str(message or "")[:160],
            "time": int(time.time()),
        }
    )
    player["diplomacy_log"] = history[-30:]


async def respond_to_request(request_id, responder_uid, accept):
    row = db.execute(
        "SELECT uid,kind,body,st FROM rq WHERE id=?", (request_id,)
    ).fetchone()
    if not row:
        return "درخواست پیدا نشد."
    sender_uid, kind, raw_body, status = row
    if status != "pending":
        return "این درخواست قبلاً پاسخ داده شده است."
    body = json.loads(raw_body)
    if safe_int(body.get("target_uid")) != responder_uid:
        return "این درخواست برای شما نیست."
    if not accept:
        db.execute("UPDATE rq SET st='no' WHERE id=?", (request_id,))
        db.commit()
        if bot:
            try:
                await bot.send_message(sender_uid, "❌ پیشنهاد دیپلماتیک شما رد شد.")
            except Exception:
                pass
        return "درخواست رد شد."

    sender = pget(sender_uid)
    target = pget(responder_uid)
    if not sender or not target or not sender.get("alive") or not target.get("alive"):
        return "یکی از کشورها دیگر فعال نیست."

    if kind == "trade":
        pp_cost = TRADE_PP
        if sender["pp"] < pp_cost:
            return "پیشنهاددهنده دیگر قدرت سیاسی کافی ندارد."
        if safe_int(sender.get("money")) < safe_int(body.get("fee")):
            return "پیشنهاددهنده برای پرداخت کارمزد پول کافی ندارد."
        give, want = body["give"], body["want"]
        if give["kind"] == "unit" and (
            safe_int(sender["u"].get(give["id"])) < safe_int(give["amount"])
        ):
            return "موجودی پیشنهادی در این مدت تغییر کرده است."
        if want["kind"] == "unit" and (
            safe_int(target["u"].get(want["id"])) < safe_int(want["amount"])
        ):
            return "موجودی کشور شما برای این معامله کافی نیست."
        if give["kind"] == "unit" and (
            used(target) + UNITS[give["id"]]["size"] * safe_int(give["amount"])
            > cap(target)
        ):
            return "ظرفیت انبار شما برای تجهیزات پیشنهادی کافی نیست."
        if want["kind"] == "unit" and (
            used(sender) + UNITS[want["id"]]["size"] * safe_int(want["amount"])
            > cap(sender)
        ):
            return "ظرفیت انبار پیشنهاددهنده برای تجهیزات درخواستی کافی نیست."
        if give["kind"] == "unit" and give["id"] == "nuke" and not target.get("nuke_ok"):
            return "کشور شما مجوز دریافت بمب اتم ندارد."
        if want["kind"] == "unit" and want["id"] == "nuke" and not sender.get("nuke_ok"):
            return "پیشنهاددهنده مجوز دریافت بمب اتم ندارد."
        sender["pp"] -= pp_cost
        if error := pay(sender, body.get("fee", 0)):
            return error
        if error := apply_asset(sender, target, give):
            return error
        if error := apply_asset(target, sender, want):
            # This should only happen if data changed unexpectedly; avoid
            # partial commits and restore all changes from the in-memory copy.
            return error
        record_diplomacy(sender, target["c"], "trade", body.get("message", ""))
        record_diplomacy(target, sender["c"], "trade", body.get("message", ""))
    elif kind == "bank_transfer":
        amount = safe_int(body.get("amount"))
        today = str(datetime.now(TZ).date())
        sent_today = (
            safe_int(sender.get("bank_sent_today"))
            if sender.get("bank_sent_date") == today
            else 0
        )
        if sent_today + amount > BANK_TRANSFER_LIMIT:
            return "سقف انتقال روزانه پیشنهاددهنده پر شده است."
        if not has_building(sender, "bank") or not has_building(target, "bank"):
            return "برای انتقال هر دو کشور باید بانک داشته باشند."
        if safe_int(sender.get("money")) < amount:
            return "موجودی پیشنهاددهنده کافی نیست."
        sender["money"] -= amount
        target["money"] += amount
        sender["bank_sent_date"] = today
        sender["bank_sent_today"] = sent_today + amount
        record_diplomacy(sender, target["c"], "bank_transfer", body.get("message", ""))
        record_diplomacy(target, sender["c"], "bank_transfer", body.get("message", ""))
    elif kind == "embassy":
        country = body["target_country"]
        if country in sender["embassies"]:
            return "این سفارت قبلاً ثبت شده است."
        cost = safe_int(body.get("pp_cost", 10))
        if sender["pp"] < cost:
            return "قدرت سیاسی پیشنهاددهنده کافی نیست."
        sender["pp"] -= cost
        sender["embassies"].append(country)
        target.setdefault("hosted_embassies", []).append(sender["c"])
        record_diplomacy(sender, country, "embassy", body.get("message", ""))
        record_diplomacy(target, sender["c"], "hosted_embassy", body.get("message", ""))
    elif kind == "access":
        country = body["target_country"]
        if country in sender["access"]:
            return "این دسترسی نظامی قبلاً ثبت شده است."
        cost = safe_int(body.get("pp_cost", 8))
        if sender["pp"] < cost:
            return "قدرت سیاسی پیشنهاددهنده کافی نیست."
        sender["pp"] -= cost
        sender["access"].append(country)
        target.setdefault("access_grants", []).append(sender["c"])
        record_diplomacy(sender, country, "access", body.get("message", ""))
        record_diplomacy(target, sender["c"], "access_grant", body.get("message", ""))
    elif kind != "talk":
        return "نوع درخواست نامعتبر است."
    else:
        record_diplomacy(sender, target["c"], "talk", body.get("message", ""))
        record_diplomacy(target, sender["c"], "talk", body.get("message", ""))

    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute(
            "UPDATE p SET d=? WHERE uid=?",
            (json.dumps(sender, ensure_ascii=False), sender_uid),
        )
        db.execute(
            "UPDATE p SET d=? WHERE uid=?",
            (json.dumps(target, ensure_ascii=False), responder_uid),
        )
        db.execute("UPDATE rq SET st='ok' WHERE id=?", (request_id,))
        db.commit()
    except Exception:
        db.rollback()
        raise
    if bot:
        try:
            await bot.send_message(
                sender_uid,
                f"✅ {cname(target['c'])} درخواست شما را پذیرفت.",
            )
        except Exception:
            pass
    return "درخواست پذیرفته شد."


async def a_respond(uid, user, player, body):
    request_id = safe_int(body.get("id"))
    if request_id <= 0:
        return "شناسه درخواست نامعتبر است."
    result = await respond_to_request(
        request_id, uid, str(body.get("answer")) == "yes"
    )
    return None if result in ("درخواست پذیرفته شد.", "درخواست رد شد.") else result


async def a_war(uid, user, player, body):
    if kget("war", "on") != "on":
        return "جنگ‌ها توسط ادمین بسته شده است."
    target_uid, target = valid_target(uid, body.get("c"))
    if not target:
        return "کشور هدف نامعتبر است."
    scenario = str(body.get("sc", "")).strip()[:1000]
    reason = str(body.get("why", "")).strip()[:400]
    if len(scenario) < 20:
        return "سناریوی حمله باید حداقل ۲۰ حرف باشد."
    if player["pp"] < WAR_PP:
        return f"قدرت سیاسی کافی نیست (نیاز به {WAR_PP} PP)."
    send = {}
    for key, amount in (body.get("send") or {}).items():
        if key not in UNITS or UNITS[key]["atk"] <= 0:
            continue
        count = safe_int(amount)
        if count > 0:
            if UNITS[key].get("lock") and not player.get("nuke_ok"):
                return "بمب اتم قفل است."
            avail = safe_int(player["u"].get(key))
            if count > avail:
                return f"تعداد {UNITS[key]['fa']} انتخابی بیشتر از موجودی شماست."
            send[key] = count
    if not sum(send.values()):
        return "هیچ نیرویی برای حمله انتخاب نشده است."

    # کسر قدرت سیاسی و انتقال نیروها به صف نبرد
    player["pp"] -= WAR_PP
    player["wt"] = safe_int(player.get("wt")) + 1
    for k, cnt in send.items():
        player["u"][k] = max(0, safe_int(player["u"].get(k)) - cnt)
    pset(uid, player)

    # ذخیره جنگ در دیتابیس
    now = int(time.time())
    send_json = json.dumps(send, ensure_ascii=False)
    cur = db.execute(
        "INSERT INTO pending_wars (attacker_uid, defender_uid, attacker_country, defender_country, units, scenario, reason, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
        (uid, target_uid, player["c"], target["c"], send_json, scenario, reason, now),
    )
    db.commit()
    war_id = cur.lastrowid

    # ساخت متن تسلیحات
    units_text = "\n".join([f"  ▫️ {UNITS[k]['fa']}: {cnt:,} عدد" for k, cnt in send.items()])

    # اطلاع‌رسانی آنی در کانال
    announce_msg = (
        f"🚨 <b>اعلان رسمی آغاز عملیات نظامی!</b>\n\n"
        f"🚩 <b>متخاصم (مهاجم):</b> {cname(player['c'])}\n"
        f"🎯 <b>کشور هدف (مدافع):</b> {cname(target['c'])}\n\n"
        f"🪖 <b>تسلیحات و نیروهای اعزامی:</b>\n{units_text}\n\n"
        f"📜 <b>سناریوی حمله:</b>\n{scenario}\n"
        + (f"\n🇺🇳 <b>علت اعلام‌شده:</b> {reason}\n" if reason else "")
        + f"\n⏳ <i>سناریو به ستاد کل فرماندهی (ادمین) ارسال شد. نتیجه پس از بررسی کارشناسی اعلام می‌گردد.</i>"
    )
    await announce(announce_msg)

    # ارسال برای ادمین‌ها با دکمه‌های شیشه‌ای
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🏭 تخریب کارخانه مدافع", callback_data=f"war_res:{war_id}:fact"),
                InlineKeyboardButton(text="💥 پیروزی قاطع مهاجم", callback_data=f"war_res:{war_id}:atk"),
            ],
            [
                InlineKeyboardButton(text="🛡 دفاع موفق مدافع", callback_data=f"war_res:{war_id}:def"),
                InlineKeyboardButton(text="❌ رد سناریو و استرداد", callback_data=f"war_res:{war_id}:reject"),
            ],
        ]
    )
    admin_msg = (
        f"⚔️ <b>سناریوی جنگ جدید (شماره #{war_id})</b>\n\n"
        f"🚩 مهاجم: {cname(player['c'])} (@{player.get('username') or 'ندارد'}, ID: <code>{uid}</code>)\n"
        f"🎯 مدافع: {cname(target['c'])} (@{target.get('username') or 'ندارد'}, ID: <code>{target_uid}</code>)\n\n"
        f"🪖 <b>نیروهای ارسالی:</b>\n{units_text}\n\n"
        f"📜 <b>متن سناریو:</b>\n{scenario}\n"
        + (f"🇺🇳 دلیل: {reason}\n" if reason else "")
        + f"\nلطفاً نتیجه را تعیین کنید:"
    )
    for adm in ADMINS:
        try:
            await bot.send_message(adm, admin_msg, reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            print(f"Failed to send war to admin {adm}:", e)

    return None

def spam_violation(player, reason, severe=False):
    player["stmt_strikes"] = safe_int(player.get("stmt_strikes")) + (
        2 if severe else 1
    )
    if player["stmt_strikes"] >= 3:
        player["stmt_suspended_until"] = int(time.time()) + 86_400
        pset(by_code(player["c"])[0], player)
        return "به‌دلیل ارسال تکراری، امکان صدور بیانیه برای ۲۴ ساعت بسته شد."
    pset(by_code(player["c"])[0], player)
    return f"{reason} در صورت تکرار، امکان صدور بیانیه موقتاً بسته می‌شود."


async def a_stmt(uid, user, player, body):
    if not player:
        return "ابتدا کشور انتخاب کنید."
    now = int(time.time())
    if safe_int(player.get("stmt_suspended_until")) > now:
        remain = (safe_int(player["stmt_suspended_until"]) - now) // 3600 + 1
        return f"امکان صدور بیانیه موقتاً بسته است؛ حدود {remain} ساعت دیگر دوباره تلاش کنید."
    text = str(body.get("text", "")).strip()[:850]
    if len(text) < 5:
        return "متن بیانیه باید حداقل ۵ حرف باشد."
    image = str(body.get("img", "")).strip()
    if image and not image.startswith("https://"):
        return "لینک تصویر باید با https:// شروع شود."

    history = [
        item
        for item in player.get("statement_history", [])
        if safe_int(item.get("time")) > now - 86_400
    ]
    digest = hashlib.sha256(text.casefold().encode("utf-8")).hexdigest()
    if any(item.get("hash") == digest for item in history):
        return spam_violation(
            player, "این متن قبلاً ارسال شده است.", severe=True
        )
    last_time = max((safe_int(item.get("time")) for item in history), default=0)
    if last_time and now - last_time < 900:
        return spam_violation(
            player, "برای جلوگیری از اسپم، بین بیانیه‌ها باید ۱۵ دقیقه فاصله باشد."
        )
    if len(history) >= 5:
        return spam_violation(
            player, "سقف مجاز بیانیه روزانه ۵ مورد است."
        )
    if bot is None:
        return "ربات آماده ارسال نیست؛ تنظیمات BOT_TOKEN را بررسی کنید."
    caption = f"📢 {cname(player['c'])} بیانیه‌ای صادر کرد:\n\n{text}"
    try:
        if image:
            try:
                await bot.send_photo(CHANNEL, image, caption=caption)
            except Exception as photo_error:
                print("statement image delivery failed:", photo_error)
                await bot.send_message(
                    CHANNEL,
                    caption + "\n\n(تصویر پیوست نشد.)",
                )
        else:
            await bot.send_message(CHANNEL, caption)
    except Exception as exc:
        print("statement delivery failed:", exc)
        return "ارسال بیانیه به کانال ناموفق بود؛ دسترسی ربات به کانال را بررسی کنید."
    history.append({"time": now, "hash": digest})
    player["statement_history"] = history
    player["stmt_strikes"] = max(0, safe_int(player.get("stmt_strikes")) - 1)
    pset(uid, player)
    return None


async def a_note(uid, user, player, body):
    text = str(body.get("text", "")).strip()[:1_000]
    if len(text) < 5:
        return "متن باید حداقل ۵ حرف باشد."
    await create_admin_request(uid, "note", {"text": text})
    return None


async def a_nuke(uid, user, player, body):
    if not {"nuc_1", "nuc_2"} <= set(player["t"]):
        return "ابتدا تحقیقات هسته‌ای و غنی‌سازی را تکمیل کنید."
    if player.get("nuke_ok"):
        return "مجوز ساخت را دارید."
    text = str(body.get("text", "")).strip()[:800]
    if len(text) < 20:
        return "متن درخواست مجوز باید حداقل ۲۰ حرف باشد."
    await create_admin_request(uid, "nuke", {"text": text})
    return None


ACTIONS = {
    "state": a_state,
    "pick": a_pick,
    "build": a_build,
    "buy": a_buy,
    "mf": a_mf,
    "bank": a_bank,
    "bank_transfer": a_bank_transfer,
    "tax": a_tax,
    "tech": a_tech,
    "diplomacy": a_diplomacy,
    "respond": a_respond,
    "war": a_war,
    "stmt": a_stmt,
    "note": a_note,
    "nuke": a_nuke,
}
FREE_ACTIONS = {"state", "pick", "stmt", "note"}


def api_state(uid, player, season):
    rows = db.execute(
        "SELECT id,uid,kind,body,st FROM rq "
        "WHERE st='pending' AND (uid=? OR json_extract(body,'$.target_uid')=?) "
        "ORDER BY id DESC LIMIT 30",
        (uid, uid),
    ).fetchall()
    incoming, outgoing = [], []
    for request_id, sender_uid, kind, raw_body, _ in rows:
        body = json.loads(raw_body)
        record = {
            "id": request_id,
            "kind": kind,
            "from": body.get("from_country", ""),
            "to": body.get("target_country", ""),
            "message": body.get("message", ""),
            "amount": body.get("amount", 0),
            "give": body.get("give"),
            "want": body.get("want"),
            "fee": body.get("fee", 0),
        }
        if sender_uid == uid:
            outgoing.append(record)
        elif safe_int(body.get("target_uid")) == uid:
            incoming.append(record)
    players = allp()
    countries_taken = [p["c"] for _, p in players if p.get("c")]
    others = [
        p["c"]
        for other_uid, p in players
        if other_uid != uid and p.get("alive") and p.get("c")
    ]
    owners = {
        p["c"]: {
            "name": str(p.get("name") or "")[:80],
            "username": str(p.get("username") or "")[:32],
        }
        for _, p in players
        if p.get("c")
    }
    return {
        "season": season,
        "p": player,
        "B": BUILDINGS,
        "U": UNITS,
        "T": TECHS,
        "open": market_open(),
        "war": kget("war", "on"),
        "cap": cap(player) if player else 0,
        "used": used(player) if player else 0,
        "taken": countries_taken,
        "owners": owners,
        "C": COUNTRIES,
        "others": others,
        "incoming": incoming,
        "outgoing": outgoing,
        "WP": WAR_PP,
        "TP": TRADE_PP,
        "bank_limit": BANK_TRANSFER_LIMIT,
        "loan_limit": LOAN_LIMIT,
        "bg_music": BACKGROUND_MUSIC_URL,
    }


async def api(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"err": "درخواست نامعتبر است."}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"err": "درخواست نامعتبر است."}, status=400)
    if not TOKEN:
        return web.json_response(
            {"err": "تنظیم BOT_TOKEN روی سرور انجام نشده است."}, status=503
        )
    init_data = body.get("init", "")
    if not isinstance(init_data, str) or not init_data:
        return web.json_response(
            {
                "err": "داده‌ی ورود تلگرام دریافت نشد؛ مینی‌اپ را از دکمه‌ی داخل گفتگوی همین ربات باز کنید.",
                "auth_error": True,
            },
            status=401,
        )
    user = auth(init_data)
    if not user:
        return web.json_response(
            {
                "err": "داده‌ی ورود تلگرام معتبر نیست یا منقضی شده است. مینی‌اپ را ببندید و دوباره از داخل گفتگوی همان ربات باز کنید؛ اگر خطا ماند، لاگ Railway را بررسی کنید.",
                "auth_error": True,
            },
            status=401,
        )
    uid = safe_int(user.get("id"))
    action = request.match_info["a"]
    if action not in ACTIONS:
        return web.json_response({"err": "عملیات ناشناخته است."}, status=404)
    if not await is_member(uid):
        return web.json_response({"gate": CHATS})

    async with ACTION_LOCK:
        season = kget("season", "wait")
        player = pget(uid)
        if player and sync_player_profile(player, user):
            pset(uid, player)
        error = None
        if action != "pick" and action != "state" and not player:
            error = "ابتدا کشور انتخاب کنید."
        elif action == "pick" and season == "wait":
            error = "سیزن هنوز شروع نشده است."
        elif action not in FREE_ACTIONS and season != "live":
            error = "سیزن هنوز فعال نیست."
        elif player and not player.get("alive"):
            error = "حکومت شما سقوط کرده است."
        else:
            try:
                error = await ACTIONS[action](uid, user, player, body)
            except Exception as exc:
                print(f"action {action} failed for {uid}:", repr(exc))
                error = "انجام عملیات ناموفق بود؛ موجودی و وضعیت بازی تغییر نکرد."
        player = pget(uid)
        state = api_state(uid, player, season)
    return web.json_response({"s": state, "err": error})


# ---------------------------------------------------------------------------
# Daily economy, bank interest, production, and stability
# ---------------------------------------------------------------------------

async def tick():
    for uid, player in allp():
        if not player.get("alive"):
            continue
        buildings = player["b"]
        research = player["t"]
        cash_income = sum(
            BUILDINGS[key].get("income", 0) * safe_int(count)
            for key, count in buildings.items()
            if key in BUILDINGS
        )
        if "econ_1" in research:
            cash_income = int(cash_income * 1.1)
        player["money"] += int(cash_income)

        if has_building(player, "bank"):
            vault = safe_int(player.get("vault"))
            player["vault"] += int(vault * 0.005)

        food_output = sum(
            BUILDINGS[key].get("food", 0) * safe_int(count)
            for key, count in buildings.items()
            if key in BUILDINGS
        )
        if "agri_1" in research:
            food_output = int(food_output * 1.25)
        upkeep = sum(
            UNITS[key].get("upkeep", 0) * safe_int(count)
            for key, count in player["u"].items()
            if key in UNITS
        )
        player["food"] += int(food_output - 100 - upkeep)

        welfare = sum(
            BUILDINGS[key].get("happy", 0) * safe_int(count)
            for key, count in buildings.items()
            if key in BUILDINGS
        )
        if player["food"] < 0:
            player["food"] = 0
            player["happy"] -= 8
        player["happy"] = max(
            0, min(100, player["happy"] + welfare * 0.5 - 2)
        )
        player["pp"] += (
            10
            + (5 if safe_int(player.get("wt")) == 0 else 0)
            + min(6, 2 * len(player.get("embassies", [])))
        )
        player["wt"] = 0

        for key, factories in list(player["mf"].items()):
            data = UNITS.get(key)
            if not data:
                continue
            room = int(
                max(0, cap(player) - used(player)) / max(0.01, data["size"])
            )
            produced = min(
                safe_int(factories) * data["rate"],
                room,
            )
            if produced > 0:
                player["u"][key] = safe_int(player["u"].get(key)) + produced

        debt = safe_int(player.get("loan"))
        if debt > 0:
            interest = max(1, int(debt * 0.005))
            payment = min(
                safe_int(player.get("money")),
                interest + max(1_000, int(debt * 0.1)),
            )
            player["money"] -= payment
            player["loan"] = max(0, debt + interest - payment)
            if player["loan"] == 0:
                player["loan_original"] = 0

        if player["happy"] < 20 and player["happy"] > 0:
            player["u"] = {
                key: int(safe_int(amount) * 0.9)
                for key, amount in player["u"].items()
            }
            player["money"] = int(player["money"] * 0.95)
            await announce(
                f"🔥 {cname(player['c'])} وارد بحران شد و با ناآرامی داخلی روبه‌رو است."
            )
        if player["happy"] <= 0:
            player["alive"] = False
            await announce(f"💀 حکومت {cname(player['c'])} سقوط کرد!")
        pset(uid, player)


async def ticker():
    while True:
        now = datetime.now(TZ)
        next_midnight = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        await asyncio.sleep(max(1, (next_midnight - now).total_seconds()))
        if kget("season", "wait") == "live":
            async with ACTION_LOCK:
                try:
                    await tick()
                except Exception as exc:
                    print("daily game tick failed:", repr(exc))


# ---------------------------------------------------------------------------
# Telegram commands, admin controls, and inline request buttons
# ---------------------------------------------------------------------------

@dp.message(Command("start"))
async def start(message: Message):
    if message.from_user:
        player = pget(message.from_user.id)
        profile = {
            "first_name": message.from_user.first_name,
            "last_name": message.from_user.last_name,
            "username": message.from_user.username,
        }
        if player and sync_player_profile(player, profile):
            pset(message.from_user.id, player)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔥 ورود به Burnt Kingdoms",
                    web_app=WebAppInfo(url=URL),
                )
            ]
        ]
    )
    await message.answer(
        "به Burnt Kingdoms خوش آمدید 🔥\n"
        "برای بازی باید عضو @bk_chatt و @_burnt_kingdoms باشید.",
        reply_markup=keyboard,
    )


@dp.message(Command("season"))
async def season(message: Message):
    if message.from_user.id not in ADMINS:
        return
    action = (message.text.split() + [""])[1].lower()
    if action not in ("wait", "pick", "live"):
        return await message.answer("/season wait|pick|live")
    kset("season", action)
    announcements = {
        "wait": "⏳ سیزن در انتظار شروع است.",
        "pick": "🗺 مرحله‌ی انتخاب کشور آغاز شد!",
        "live": "🔥 سیزن فعال شد!",
    }
    await announce(announcements[action])
    await message.answer("وضعیت سیزن به‌روزرسانی شد.")


@dp.message(Command("war"))
async def war_command(message: Message):
    if message.from_user.id not in ADMINS:
        return
    action = (message.text.split() + [""])[1].lower()
    if action not in ("on", "off"):
        return await message.answer("/war on|off")
    kset("war", action)
    await announce(
        "⚔️ جنگ‌ها باز شد."
        if action == "on"
        else "🕊 جنگ‌ها توسط ادمین بسته شد."
    )
    await message.answer("وضعیت جنگ به‌روزرسانی شد.")


def resolve_player(target):
    value = str(target or "").strip()
    if not value:
        return None, None
    if value.upper() in COUNTRIES:
        return by_code(value.upper())
    try:
        uid = int(value)
    except ValueError:
        return None, None
    player = pget(uid)
    return (uid, player) if player else (None, None)


@dp.message(Command("resetmoney"))
async def reset_money(message: Message):
    if message.from_user.id not in ADMINS:
        return
    parts = message.text.split()
    if len(parts) not in (2, 3):
        return await message.answer(
            "استفاده: /resetmoney <کد کشور یا آیدی تلگرام> [مبلغ]\n"
            "بدون مبلغ، پول کشور به مقدار شروع بازی برمی‌گردد."
        )
    uid, player = resolve_player(parts[1])
    amount = safe_int(parts[2], START["money"]) if len(parts) == 3 else START["money"]
    if not player or amount < 0 or amount > 1_000_000_000_000:
        return await message.answer("بازیکن یا مبلغ نامعتبر است.")
    player["money"] = amount
    pset(uid, player)
    await message.answer(f"پول {cname(player['c'])} روی {amount:,}$ تنظیم شد.")


@dp.message(Command("addmoney"))
async def add_money(message: Message):
    if message.from_user.id not in ADMINS:
        return
    parts = message.text.split()
    if len(parts) != 3:
        return await message.answer(
            "استفاده: /addmoney <کد کشور یا آیدی تلگرام> <مبلغ>"
        )
    uid, player = resolve_player(parts[1])
    amount = safe_int(parts[2], -1)
    if not player or not 0 < amount <= 1_000_000_000_000:
        return await message.answer("بازیکن یا مبلغ نامعتبر است.")
    player["money"] += amount
    pset(uid, player)
    await message.answer(
        f"{amount:,}$ به خزانه {cname(player['c'])} افزوده شد."
    )


@dp.message(Command("resetunits"))
async def reset_units(message: Message):
    if message.from_user.id not in ADMINS:
        return
    parts = message.text.split()
    if len(parts) != 2:
        return await message.answer(
            "استفاده: /resetunits <کد کشور یا آیدی تلگرام>\n"
            "موجودی ادوات را به ۱۰۰ پیاده‌نظام برمی‌گرداند؛ "
            "ساختمان‌ها و کارخانه‌های تولیدی حفظ می‌شوند."
        )
    uid, player = resolve_player(parts[1])
    if not player:
        return await message.answer("بازیکن یا کد کشور پیدا نشد.")
    player["u"] = {"infantry": 100}
    pset(uid, player)
    await message.answer(
        f"موجودی ادوات {cname(player['c'])} پاک شد؛ ساختمان‌ها و کارخانه‌ها حفظ شدند."
    )


async def answer_in_chunks(message, lines, limit=3500):
    chunk = ""
    for line in lines:
        candidate = f"{chunk}\n{line}" if chunk else line
        if len(candidate) > limit and chunk:
            await message.answer(chunk)
            chunk = line
        else:
            chunk = candidate
    if chunk:
        await message.answer(chunk)


def admin_player_label(player):
    parts = [
        str(player.get("name") or "").strip(),
        f"@{str(player.get('username')).strip().lstrip('@')}"
        if player.get("username")
        else "",
    ]
    return " · ".join(part for part in parts if part) or "نام ثبت‌نشده"


@dp.message(Command("countries"))
async def countries_status(message: Message):
    if message.from_user.id not in ADMINS:
        return
    players = sorted(
        [(uid, p) for uid, p in allp() if p.get("c")],
        key=lambda item: item[1].get("c", ""),
    )
    if not players:
        return await message.answer("هنوز کشوری انتخاب نشده است.")
    lines = [f"🌍 وضعیت کشورها ({len(players)})"]
    for uid, player in players:
        status = "فعال" if player.get("alive") else "سقوط‌کرده"
        troops = sum(safe_int(amount) for amount in player.get("u", {}).values())
        lines.append(
            f"{cname(player['c'])} — {status} · {admin_player_label(player)}"
        )
        lines.append(
            f"  خزانه {safe_int(player.get('money')):,}$ · "
            f"قدرت سیاسی {safe_int(player.get('pp')):,} · "
            f"نیرو {troops:,} · شناسه {uid}"
        )
    await answer_in_chunks(message, lines)


@dp.message(Command("leaderboard"))
async def admin_leaderboard(message: Message):
    if message.from_user.id not in ADMINS:
        return
    players = [
        (uid, p) for uid, p in allp() if p.get("c") and p.get("alive")
    ]
    if not players:
        return await message.answer("فعلاً کشور فعالی برای رتبه‌بندی وجود ندارد.")

    def troop_count(player):
        return sum(safe_int(amount) for amount in player.get("u", {}).values())

    def attack_score(player):
        return sum(
            UNITS.get(key, {}).get("atk", 0) * safe_int(amount)
            for key, amount in player.get("u", {}).items()
        )

    def defense_score(player):
        return sum(
            UNITS.get(key, {}).get("df", 0) * safe_int(amount)
            for key, amount in player.get("u", {}).items()
        )

    def building_income(player):
        income = sum(
            BUILDINGS.get(key, {}).get("income", 0) * safe_int(amount)
            for key, amount in player.get("b", {}).items()
        )
        return int(income * 1.1) if "econ_1" in player.get("t", []) else income

    def building_count(player):
        return sum(safe_int(amount) for amount in player.get("b", {}).values())

    metrics = [
        ("بیشترین خزانه", lambda p: safe_int(p.get("money")), "$"),
        ("بیشترین سپرده", lambda p: safe_int(p.get("vault")), "$"),
        ("بیشترین قدرت سیاسی", lambda p: safe_int(p.get("pp")), ""),
        ("بیشترین تعداد نیرو", troop_count, ""),
        ("بالاترین امتیاز حمله", attack_score, ""),
        ("بالاترین امتیاز دفاع", defense_score, ""),
        ("بیشترین درآمد روزانه‌ی ساختمان‌ها", building_income, "$"),
        ("بیشترین ساختمان", building_count, ""),
        ("بیشترین فناوری تکمیل‌شده", lambda p: len(p.get("t", [])), ""),
        ("بیشترین رضایت", lambda p: float(p.get("happy", 0)), "٪"),
    ]
    lines = ["🏆 لیدربورد کشورها — فقط کشورهایی که هنوز فعال‌اند"]
    for title, score_fn, suffix in metrics:
        ranked = sorted(
            players,
            key=lambda item: (score_fn(item[1]), item[1].get("c", "")),
            reverse=True,
        )[:3]
        lines.append(f"\n{title}")
        for rank, (_, player) in enumerate(ranked, start=1):
            score = score_fn(player)
            rendered = f"{score:,.1f}" if isinstance(score, float) else f"{score:,}"
            lines.append(
                f"{rank}. {cname(player['c'])} — {rendered}{suffix} "
                f"({admin_player_label(player)})"
            )
    await answer_in_chunks(message, lines)


@dp.message(Command("resetseason"))
async def request_season_reset(message: Message):
    if message.from_user.id not in ADMINS:
        return
    admin_id = message.from_user.id
    token = secrets.token_hex(8)
    key = f"season_reset:{admin_id}"
    state = {
        "token": token,
        "stage": 0,
        "expires": int(time.time()) + 180,
    }
    kset(key, json.dumps(state))
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="تأیید اول",
                    callback_data=f"rs1:{token}",
                ),
                InlineKeyboardButton(
                    text="لغو",
                    callback_data=f"rsx:{token}",
                ),
            ]
        ]
    )
    await message.answer(
        "⚠️ ریست سیزن همه‌ی داده‌های جاری بازیکنان و درخواست‌های دیپلماسی را از بازی خارج می‌کند؛ "
        "کشورها، خزانه، ارتش، بانک و فناوری‌ها پاک می‌شوند. یک نسخه‌ی آرشیوی از داده‌ها "
        "در همان پایگاه‌داده ذخیره می‌شود. برای ادامه باید دو تأیید جداگانه بدهید؛ "
        "این درخواست تا ۳ دقیقه معتبر است.",
        reply_markup=keyboard,
    )


@dp.callback_query(F.data.regexp(r"^rs[12x]:[0-9a-f]{16}$"))
async def confirm_season_reset(query: CallbackQuery):
    admin_id = query.from_user.id
    if admin_id not in ADMINS:
        return await query.answer("⛔ دسترسی ندارید.", show_alert=True)
    action, token = query.data.split(":", 1)
    key = f"season_reset:{admin_id}"

    if action == "rs1":
        raw = kget(key)
        try:
            state = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            state = {}
        if (
            state.get("token") != token
            or safe_int(state.get("expires")) < int(time.time())
            or safe_int(state.get("stage")) != 0
        ):
            return await query.answer(
                "درخواست منقضی یا نامعتبر است؛ دستور را دوباره بزنید.",
                show_alert=True,
            )
        state["stage"] = 1
        kset(key, json.dumps(state))
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⚠️ تأیید نهایی و شروع سیزن تازه",
                        callback_data=f"rs2:{token}",
                    ),
                    InlineKeyboardButton(
                        text="لغو",
                        callback_data=f"rsx:{token}",
                    ),
                ]
            ]
        )
        await query.message.edit_text(
            "تأیید اول ثبت شد. برای پاک‌کردن داده‌های جاری و آغاز مرحله‌ی انتخاب کشور، "
            "دکمه‌ی تأیید نهایی را بزنید. این عمل قابل بازگردانی از داخل بازی نیست؛ "
            "نسخه‌ی قبلی فقط در آرشیو پایگاه‌داده باقی می‌ماند.",
            reply_markup=keyboard,
        )
        return await query.answer("تأیید اول ثبت شد.")

    if action == "rsx":
        async with ACTION_LOCK:
            try:
                db.execute("BEGIN IMMEDIATE")
                raw = db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
                state = json.loads(raw[0]) if raw else {}
                if state.get("token") != token:
                    db.rollback()
                    return await query.answer(
                        "این درخواست دیگر معتبر نیست.", show_alert=True
                    )
                db.execute("DELETE FROM kv WHERE k=?", (key,))
                db.commit()
            except Exception:
                db.rollback()
                return await query.answer(
                    "لغو درخواست ممکن نشد؛ دوباره تلاش کنید.", show_alert=True
                )
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return await query.answer("ریست سیزن لغو شد.")

    async with ACTION_LOCK:
        try:
            db.execute("BEGIN IMMEDIATE")
            raw = db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
            state = json.loads(raw[0]) if raw else {}
            if (
                state.get("token") != token
                or safe_int(state.get("expires")) < int(time.time())
                or safe_int(state.get("stage")) != 1
            ):
                db.rollback()
                return await query.answer(
                    "تأیید نهایی نامعتبر یا منقضی شده است؛ هیچ داده‌ای تغییر نکرد.",
                    show_alert=True,
                )
            player_rows = db.execute("SELECT uid,d FROM p ORDER BY uid").fetchall()
            request_rows = db.execute(
                "SELECT id,uid,kind,body,st FROM rq ORDER BY id"
            ).fetchall()
            season_row = db.execute(
                "SELECT v FROM kv WHERE k='season'"
            ).fetchone()
            old_season = season_row[0] if season_row else "wait"
            db.execute(
                "INSERT INTO season_archives(created_at,season,players_json,requests_json) "
                "VALUES(?,?,?,?)",
                (
                    datetime.now(TZ).isoformat(),
                    old_season,
                    json.dumps(player_rows, ensure_ascii=False),
                    json.dumps(request_rows, ensure_ascii=False),
                ),
            )
            db.execute("DELETE FROM p")
            db.execute("DELETE FROM rq")
            db.execute(
                "INSERT OR REPLACE INTO kv(k,v) VALUES('season','pick')"
            )
            db.execute(
                "DELETE FROM kv WHERE substr(k,1,13)='season_reset:'"
            )
            db.commit()
        except Exception as exc:
            db.rollback()
            print("[admin-reset] season reset rolled back:", type(exc).__name__)
            return await query.answer(
                "ریست انجام نشد و پایگاه‌داده تغییر نکرد؛ لاگ Railway را بررسی کنید.",
                show_alert=True,
            )

    try:
        await query.message.edit_text(
            f"✅ سیزن ریست شد؛ {len(player_rows)} کشور در آرشیو پایگاه‌داده ذخیره شد. "
            "مرحله‌ی انتخاب کشور آغاز شده است."
        )
    except Exception:
        pass
    await query.answer("سیزن ریست شد.")
    await announce(
        "🔄 سیزن جدید Burnt Kingdoms آغاز شد. مرحله‌ی انتخاب کشور باز است؛ "
        "برای شروع از دکمه‌ی ورود به بازی استفاده کنید."
    )


@dp.callback_query(F.data.regexp(r"^dreq:(yes|no):\d+$"))
async def diplomacy_reply(query: CallbackQuery):
    _, answer, request_id = query.data.split(":")
    async with ACTION_LOCK:
        result = await respond_to_request(
            int(request_id), query.from_user.id, answer == "yes"
        )
    await query.answer(result[:180], show_alert=result not in (
        "درخواست پذیرفته شد.",
        "درخواست رد شد.",
    ))
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@dp.callback_query(F.data.regexp(r"^(ok|no):\d+$"))
async def admin_review(query: CallbackQuery):
    if query.from_user.id not in ADMINS:
        return await query.answer("⛔ دسترسی ندارید.", show_alert=True)
    answer, request_id = query.data.split(":")
    async with ACTION_LOCK:
        row = db.execute(
            "SELECT uid,kind,body,st FROM rq WHERE id=?", (int(request_id),)
        ).fetchone()
        if not row or row[3] != "pending" or row[1] != "nuke":
            return await query.answer("درخواست قبلاً بررسی شده یا نامعتبر است.")
        uid, kind, raw_body, _ = row
        db.execute(
            "UPDATE rq SET st=? WHERE id=?",
            ("ok" if answer == "ok" else "no", int(request_id)),
        )
        db.commit()
        player = pget(uid)
        if answer == "ok" and player:
            player["nuke_ok"] = True
            pset(uid, player)
            await announce(
                f"☢️ سازمان انرژی اتمی به {cname(player['c'])} مجوز ساخت داد."
            )
    try:
        await bot.send_message(
            uid,
            "✅ درخواست مجوز هسته‌ای شما تأیید شد."
            if answer == "ok"
            else "❌ درخواست مجوز هسته‌ای شما رد شد.",
        )
    except Exception:
        pass
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.answer("ثبت شد.")


async def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Add it to the environment or .env.")
    if not URL or urlparse(URL).scheme != "https":
        raise RuntimeError("WEBAPP_URL must be a public HTTPS URL for the Mini App.")
    bot_identity = await bot.get_me()
    print(f"[startup] Telegram token accepted for @{bot_identity.username}")
    app = web.Application(client_max_size=64 * 1024)
    app.router.add_post("/api/{a}", api)
    app.router.add_get("/", lambda request: web.FileResponse("index.html"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(
        runner, "0.0.0.0", int(os.environ.get("PORT", 8080))
    ).start()
    asyncio.create_task(ticker())
    await dp.start_polling(bot)



@dp.callback_query(F.data.regexp(r"^war_res:(\d+):(fact|atk|def|reject)$"))
async def handle_war_resolution(query: CallbackQuery):
    if query.from_user.id not in ADMINS:
        return await query.answer("شما دسترسی ادمین ندارید.", show_alert=True)

    parts = query.data.split(":")
    war_id = int(parts[1])
    decision = parts[2]

    row = db.execute(
        "SELECT attacker_uid, defender_uid, attacker_country, defender_country, units, scenario, status FROM pending_wars WHERE id=?",
        (war_id,)
    ).fetchone()

    if not row:
        return await query.answer("این رکورد جنگ یافت نشد.", show_alert=True)

    atk_uid, def_uid, atk_c, def_c, units_json, scenario, status = row
    if status != "pending":
        return await query.answer(f"این نبرد قبلاً تعیین وضعیت شده است: {status}", show_alert=True)

    sent_units = json.loads(units_json)
    atk = pget(atk_uid)
    defn = pget(def_uid)
    if not atk or not defn:
        return await query.answer("اطلاعات یکی از طرفین جنگ یافت نشد.", show_alert=True)

    admin_name = query.from_user.full_name
    result_title = ""
    result_details = ""

    if decision == "fact":
        destroyed = []
        for f_key in ["f_chip", "f_oil", "f_steel", "f_light"]:
            if defn.get("b", {}).get(f_key, 0) > 0:
                defn["b"][f_key] -= 1
                destroyed.append(BUILDINGS[f_key]["fa"])
                break
        if not destroyed and defn.get("b"):
            for b_key in list(defn["b"].keys()):
                if defn["b"][b_key] > 0:
                    defn["b"][b_key] -= 1
                    destroyed.append(BUILDINGS.get(b_key, {}).get("fa", b_key))
                    break

        dest_text = "، ".join(destroyed) if destroyed else "زیرساخت‌های اقتصادی"
        loot = int(safe_int(defn.get("money")) * 0.12)
        defn["money"] = max(0, defn.get("money", 0) - loot)
        atk["money"] = atk.get("money", 0) + loot
        defn["happy"] = max(0, defn.get("happy", 50) - 15)

        for k, cnt in sent_units.items():
            atk["u"][k] = safe_int(atk["u"].get(k)) + int(cnt * 0.85)
        for k in list(defn.get("u", {}).keys()):
            defn["u"][k] = int(safe_int(defn["u"][k]) * 0.70)

        result_title = f"🏭 <b>پیروزی قاطع و تخریب زیرساخت‌های {cname(def_c)}!</b>"
        result_details = (
            f"💥 عملیات موفقیت‌آمیز بود و کارخانه‌های مدافع منهدم شد.\n"
            f"🔻 خسارت صنعتی: <b>{dest_text}</b>\n"
            f"💰 غنایم جنگی: {loot:,}$\n"
            f"📉 رضایت عمومی مدافع: ۱۵- واحد کاهش یافت."
        )

    elif decision == "atk":
        loot = int(safe_int(defn.get("money")) * 0.08)
        defn["money"] = max(0, defn.get("money", 0) - loot)
        atk["money"] = atk.get("money", 0) + loot
        defn["happy"] = max(0, defn.get("happy", 50) - 8)

        for k, cnt in sent_units.items():
            atk["u"][k] = safe_int(atk["u"].get(k)) + int(cnt * 0.75)
        for k in list(defn.get("u", {}).keys()):
            defn["u"][k] = int(safe_int(defn["u"][k]) * 0.80)

        result_title = f"💥 <b>پیروزی میدانی {cname(atk_c)} علیه {cname(def_c)}!</b>"
        result_details = (
            f"✅ سناریو تأیید شد و خطوط دفاعی شکسته شد.\n"
            f"💰 غنایم: {loot:,}$\n"
            f"🔻 تلفات به ارتش مدافع وارد آمد."
        )

    elif decision == "def":
        for k, cnt in sent_units.items():
            atk["u"][k] = safe_int(atk["u"].get(k)) + int(cnt * 0.40)
        for k in list(defn.get("u", {}).keys()):
            defn["u"][k] = int(safe_int(defn["u"][k]) * 0.90)
        atk["happy"] = max(0, atk.get("happy", 50) - 8)

        result_title = f"🛡 <b>دفاع موفق {cname(def_c)} و دفع تهاجم!</b>"
        result_details = (
            f"❌ دفاع با موفقیت انجام شد و مهاجم با ۶۰٪ تلفات عقب نشست.\n"
            f"🔻 روحیه عمومی مهاجم کاهش یافت."
        )

    elif decision == "reject":
        atk["pp"] = atk.get("pp", 0) + WAR_PP
        for k, cnt in sent_units.items():
            atk["u"][k] = safe_int(atk["u"].get(k)) + cnt

        result_title = f"🚫 <b>ابطال عملیات نظامی {cname(atk_c)} علیه {cname(def_c)}!</b>"
        result_details = (
            f"⚠️ سناریوی ارسالی توسط ستاد داوری رد شد.\n"
            f" نیروها و امتیاز سیاسی به کشور مبدأ بازگردانده شد."
        )

    pset(atk_uid, atk)
    pset(def_uid, defn)
    db.execute("UPDATE pending_wars SET status=? WHERE id=?", (decision, war_id))
    db.commit()

    channel_res = (
        f"⚖️ <b>اعلام نتیجه نهایی نبرد (پرونده #{war_id})</b>\n\n"
        f"{result_title}\n\n"
        f"🚩 مهاجم: {cname(atk_c)}\n"
        f"🎯 مدافع: {cname(def_c)}\n\n"
        f"{result_details}\n\n"
        f"✍️ ارزیابی ستاد داوری: {admin_name}"
    )
    await announce(channel_res)

    try:
        await query.message.edit_text(
            query.message.html_text + f"\n\n✅ <b>نتیجه توسط {admin_name} ثبت شد: {decision}</b>",
            parse_mode="HTML"
        )
    except Exception:
        pass
    await query.answer("نتیجه جنگ با موفقیت ثبت و در کانال اعلام گردید.")


if __name__ == "__main__":
    asyncio.run(main())
