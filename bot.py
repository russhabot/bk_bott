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
    CREATE TABLE IF NOT EXISTS rq(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        uid INTEGER,
        kind TEXT,
        body TEXT,
        st TEXT DEFAULT 'pending'
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
    return 8 <= datetime.now(TZ).hour < 22


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
    if not TOKEN or not init_data:
        return None
    try:
        fields = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = fields.pop("hash", "")
        # Newer Telegram clients may include a third-party `signature` field;
        # Bot-token HMAC validation uses the hash data-check-string only.
        fields.pop("signature", None)
        if not received_hash:
            return None
        data_check = "\n".join(
            f"{key}={value}" for key, value in sorted(fields.items())
        )
        secret = hmac.new(
            b"WebAppData", TOKEN.encode("utf-8"), hashlib.sha256
        ).digest()
        expected = hmac.new(
            secret, data_check.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, received_hash):
            return None
        auth_date = safe_int(fields.get("auth_date"))
        if auth_date <= 0 or time.time() - auth_date > 86_400:
            return None
        return json.loads(fields["user"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
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
        "name": str(user.get("first_name", ""))[:80],
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
        return "بازار تسلیحات از ساعت ۸ صبح تا ۱۰ شب به وقت تهران باز است."
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
    pending_amount = sum(
        safe_int(json.loads(row[0]).get("amount"))
        for row in db.execute(
            "SELECT body FROM rq WHERE uid=? AND kind='bank_transfer' AND st='pending'",
            (uid,),
        ).fetchall()
    )
    if sent_today + pending_amount + amount > BANK_TRANSFER_LIMIT:
        return f"سقف انتقال روزانه {BANK_TRANSFER_LIMIT:,}$ است."
    if safe_int(player.get("money")) < amount:
        return "موجودی پول نقد کافی نیست."
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
    scenario = str(body.get("sc", "")).strip()[:800]
    reason = str(body.get("why", "")).strip()[:400]
    if len(scenario) < 20:
        return "سناریوی حمله باید حداقل ۲۰ حرف باشد."
    if player["pp"] < WAR_PP:
        return "قدرت سیاسی کافی نیست."
    send = {}
    for key, amount in (body.get("send") or {}).items():
        if key not in UNITS or UNITS[key]["atk"] <= 0:
            continue
        count = safe_int(amount)
        if count > 0:
            if UNITS[key].get("lock") and not player.get("nuke_ok"):
                return "بمب اتم قفل است."
            send[key] = min(count, safe_int(player["u"].get(key)))
    if not sum(send.values()):
        return "هیچ نیرویی برای حمله انتخاب نشده است."
    attack = sum(UNITS[key]["atk"] * count for key, count in send.items())
    defense = sum(
        UNITS[key]["df"] * safe_int(count)
        for key, count in target.get("u", {}).items()
        if key in UNITS
    )
    attack *= 1.1 if "mil_1" in player["t"] else 1
    wins = attack * random.uniform(0.85, 1.15) > (
        max(50, defense) * random.uniform(0.85, 1.15)
    )
    player["pp"] -= WAR_PP
    player["wt"] = safe_int(player.get("wt")) + 1
    for key, count in send.items():
        player["u"][key] = max(
            0,
            safe_int(player["u"].get(key))
            - int(count * (0.12 if wins else 0.30)),
        )
    for key, count in list(target["u"].items()):
        if key in UNITS:
            target["u"][key] = int(
                safe_int(count) * (0.72 if wins else 0.92)
            )
    if wins:
        loot = int(safe_int(target["money"]) * 0.1)
        target["money"] -= loot
        player["money"] += loot
        target["happy"] = max(0, target["happy"] - 8)
    pset(uid, player)
    pset(target_uid, target)
    await announce(
        f"⚔️ {cname(player['c'])} به {cname(target['c'])} حمله کرد!\n\n"
        f"📜 سناریو: {scenario}\n"
        + (f"🇺🇳 دلیل اعلام‌شده: {reason}\n" if reason else "")
        + f"\n{'✅ حمله موفق بود' if wins else '❌ حمله عقب‌نشینی کرد'}"
    )
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
    countries_taken = [p["c"] for _, p in allp() if p.get("c")]
    others = [
        p["c"]
        for other_uid, p in allp()
        if other_uid != uid and p.get("alive") and p.get("c")
    ]
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
        "C": COUNTRIES,
        "others": others,
        "incoming": incoming,
        "outgoing": outgoing,
        "WP": WAR_PP,
        "TP": TRADE_PP,
        "bank_limit": BANK_TRANSFER_LIMIT,
    }


async def api(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"err": "درخواست نامعتبر است."}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"err": "درخواست نامعتبر است."}, status=400)
    user = auth(body.get("init", ""))
    if not user:
        return web.json_response({"err": "احراز هویت ناموفق است."}, status=401)
    uid = safe_int(user.get("id"))
    action = request.match_info["a"]
    if action not in ACTIONS:
        return web.json_response({"err": "عملیات ناشناخته است."}, status=404)
    if not await is_member(uid):
        return web.json_response({"gate": CHATS})

    async with ACTION_LOCK:
        season = kget("season", "wait")
        player = pget(uid)
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
            principal_paid = max(0, payment - interest)
            player["loan"] = max(0, debt + interest - principal_paid)
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


if __name__ == "__main__":
    asyncio.run(main())