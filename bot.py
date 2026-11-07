import asyncio
import html
import io
import logging
import os
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import psycopg
from aiohttp import ClientSession, ClientTimeout, web
from psycopg import errors as pgerr
from psycopg.rows import dict_row

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

try:
    import qrcode
except ImportError:  # QR na ho to bhi bot chalega
    qrcode = None

load_dotenv()

# ================= CONFIG: Render ke Environment Variables se aata hai =================
try:
    import settings as _S
except ImportError:
    _S = None


def cfg(name, default=""):
    v = os.getenv(name)  # Render env var ho to wo pehle
    if v not in (None, ""):
        return str(v).strip()
    return str(getattr(_S, name, default)).strip() if _S else str(default)


def _placeholder(v):
    return (not v) or v.startswith("yaha_")


BOT_TOKEN = cfg("BOT_TOKEN")
ADMIN_ID = int(cfg("ADMIN_ID", "0")) if cfg("ADMIN_ID", "0").lstrip("-").isdigit() else 0
BRAND = cfg("ROHAN 04 SHOP", "Premium Store")
UPI_ID = cfg("UPI_ID", "yourupi@bank")
UPI_NAME = cfg("UPI_NAME", "Your Name")
MIN_DEPOSIT = int(cfg("MIN_DEPOSIT", "50"))
MAX_DEPOSIT = int(cfg("MAX_DEPOSIT", "10000"))
BONUS_MIN = int(cfg("BONUS_MIN", "500"))          # itna ya zyada deposit pe bonus
BONUS_PERCENT = int(cfg("BONUS_PERCENT", "0"))    # 0 = bonus band
SUPPORT = cfg("SUPPORT_USERNAME", "@your_username")
DATABASE_URL = cfg("DATABASE_URL")                # Render Postgres ka Internal Database URL
DB_LIFETIME_DAYS = int(cfg("DB_LIFETIME_DAYS", "30"))  # free Postgres kitne din chalta hai
DASH_KEY = "" if _placeholder(cfg("DASH_KEY")) else cfg("DASH_KEY")  # private dashboard: /admin?key=DASH_KEY
QUICK_AMOUNTS = [50, 100, 200, 500, 1000]
LINE = "━━━━━━━━━━━━━━━━━━━"

logging.basicConfig(level=logging.INFO)

# ================= DATABASE (PostgreSQL) =================
class DB:
    """Chhota wrapper: ? placeholders, commit/rollback aur auto-reconnect."""

    def __init__(self, dsn):
        self.dsn = dsn
        self.conn = None
        self.in_tx = False
        self._connect()

    def _connect(self):
        self.conn = psycopg.connect(
            self.dsn,
            autocommit=True,          # transaction hum khud BEGIN/COMMIT se chalate hain
            row_factory=dict_row,
            connect_timeout=15,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
        )
        self.in_tx = False

    def execute(self, sql, args=()):
        sql = sql.replace("?", "%s")
        is_write = sql.lstrip()[:6].upper() in ("INSERT", "UPDATE", "DELETE")
        for attempt in (1, 2):
            try:
                if is_write and not self.in_tx:
                    self.conn.execute("BEGIN")
                    self.in_tx = True
                return self.conn.execute(sql, args or None)
            except (psycopg.OperationalError, psycopg.InterfaceError):
                lost_tx = self.in_tx      # tx beech me toot gaya to retry nahi karenge
                self._connect()
                if lost_tx or attempt == 2:
                    raise
            except psycopg.Error:
                self.rollback()
                raise

    def commit(self):
        if self.in_tx:
            self.in_tx = False
            self.conn.execute("COMMIT")

    def rollback(self):
        if self.in_tx:
            self.in_tx = False
            try:
                self.conn.execute("ROLLBACK")
            except psycopg.Error:
                self._connect()


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users(
        id BIGINT PRIMARY KEY,
        username TEXT,
        balance BIGINT NOT NULL DEFAULT 0,
        banned INTEGER NOT NULL DEFAULT 0,
        joined TIMESTAMPTZ NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS categories(
        id SERIAL PRIMARY KEY,
        name TEXT UNIQUE NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS products(
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        price INTEGER NOT NULL,
        kind TEXT NOT NULL,
        content TEXT NOT NULL,
        descr TEXT NOT NULL DEFAULT '',
        cat_id INTEGER,
        active INTEGER NOT NULL DEFAULT 1)""",
    """CREATE TABLE IF NOT EXISTS stock(
        id SERIAL PRIMARY KEY,
        product_id INTEGER NOT NULL,
        data TEXT NOT NULL,
        sold INTEGER NOT NULL DEFAULT 0,
        sold_to BIGINT)""",
    """CREATE TABLE IF NOT EXISTS deposits(
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        amount INTEGER NOT NULL,
        utr TEXT NOT NULL,
        shot TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        created TIMESTAMPTZ NOT NULL DEFAULT now())""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_utr ON deposits(utr) WHERE status <> 'rejected'",
    """CREATE TABLE IF NOT EXISTS codes(
        code TEXT PRIMARY KEY,
        amount INTEGER NOT NULL,
        used INTEGER NOT NULL DEFAULT 0,
        used_by BIGINT,
        created TIMESTAMPTZ NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS purchases(
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        product_id INTEGER NOT NULL,
        price INTEGER NOT NULL,
        keydata TEXT,
        created TIMESTAMPTZ NOT NULL DEFAULT now())""",
    "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
]

if _placeholder(DATABASE_URL):
    raise SystemExit("DATABASE_URL set nahi hai. Render -> Environment me DATABASE_URL daalo (Internal Database URL).")
db = DB(DATABASE_URL)
for _stmt in SCHEMA:
    db.execute(_stmt)
# database kab bana (expiry countdown ke liye); naye DB me apne aap naya date set hota hai
db.execute(
    "INSERT INTO meta(key,value) VALUES('db_created', ?) ON CONFLICT(key) DO NOTHING",
    (datetime.now(timezone.utc).isoformat(),),
)
db.commit()


# ================= HELPERS =================
def e(s):
    return html.escape(str(s))


def card(title, body=""):
    return f"💎 <b>{e(title)}</b>\n{LINE}\n{body}"


def q1(sql, args=()):
    r = db.execute(sql, args).fetchone()   # row dict hoti hai, isliye r[0] nahi chalta
    if not r:
        return 0
    v = next(iter(r.values()))
    return int(v) if v is not None else 0


def ensure_user(uid, username):
    db.execute(
        "INSERT INTO users(id, username) VALUES(?,?) "
        "ON CONFLICT(id) DO UPDATE SET username=excluded.username",
        (uid, username),
    )
    db.commit()


def get_balance(uid):
    return q1("SELECT balance FROM users WHERE id=?", (uid,))


def is_banned(uid):
    return bool(q1("SELECT banned FROM users WHERE id=?", (uid,)))


def stock_left(pid):
    return q1("SELECT COUNT(*) FROM stock WHERE product_id=? AND sold=0", (pid,))


def get_cat(name):
    db.execute("INSERT INTO categories(name) VALUES(?) ON CONFLICT(name) DO NOTHING", (name.strip(),))
    db.commit()
    return db.execute("SELECT id FROM categories WHERE name=?", (name.strip(),)).fetchone()["id"]


def bonus_for(amount):
    if BONUS_PERCENT > 0 and amount >= BONUS_MIN:
        return amount * BONUS_PERCENT // 100
    return 0


def make_code(amount):
    code = secrets.token_hex(4).upper()
    db.execute("INSERT INTO codes(code, amount) VALUES(?,?)", (code, amount))
    db.commit()
    return code


def redeem_code(uid, code):
    row = db.execute(
        "UPDATE codes SET used=1, used_by=? WHERE code=? AND used=0 RETURNING amount", (uid, code)
    ).fetchone()
    if not row:
        db.rollback()
        return None
    db.execute("UPDATE users SET balance = balance + ? WHERE id=?", (row["amount"], uid))
    db.commit()
    return row["amount"]


def buy_product(uid, pid):
    """Return (status, product, info). Sab kuch ek saath hota hai, double-buy nahi."""
    p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
        return "gone", None, None
    if p["kind"] == "keys" and stock_left(pid) == 0:
        return "nostock", p, None
    # neeche ke sab steps ek transaction me: ya to sab hote hain ya kuch nahi
    cur = db.execute(
        "UPDATE users SET balance = balance - ? WHERE id=? AND balance >= ?",
        (p["price"], uid, p["price"]),
    )
    if cur.rowcount == 0:
        db.rollback()
        return "nobalance", p, None
    key = stock_id = None
    if p["kind"] == "keys":
        s = db.execute(
            "UPDATE stock SET sold=1, sold_to=? WHERE id=("
            "SELECT id FROM stock WHERE product_id=? AND sold=0 ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED"
            ") RETURNING id, data",
            (uid, pid),
        ).fetchone()
        if not s:
            db.rollback()
            return "nostock", p, None
        stock_id, key = s["id"], s["data"]
    row = db.execute(
        "INSERT INTO purchases(user_id, product_id, price, keydata) VALUES(?,?,?,?) RETURNING id",
        (uid, pid, p["price"], key),
    ).fetchone()
    db.commit()
    return "ok", p, {"purchase_id": row["id"], "stock_id": stock_id, "key": key}


def undo_purchase(uid, price, info):
    db.execute("DELETE FROM purchases WHERE id=?", (info["purchase_id"],))
    if info["stock_id"]:
        db.execute("UPDATE stock SET sold=0, sold_to=NULL WHERE id=?", (info["stock_id"],))
    db.execute("UPDATE users SET balance = balance + ? WHERE id=?", (price, uid))
    db.commit()


async def deliver(bot: Bot, uid: int, p, key=None):
    cap = f"📦 <b>{e(p['name'])}</b>"
    if p["kind"] == "keys":
        await bot.send_message(uid, f"{cap}\n{LINE}\n🔑 <code>{e(key)}</code>", protect_content=True)
    elif p["kind"] == "text":
        await bot.send_message(uid, f"{cap}\n{LINE}\n{e(p['content'])}", protect_content=True)
    elif p["kind"] == "document":
        await bot.send_document(uid, p["content"], caption=cap, protect_content=True)
    elif p["kind"] == "video":
        await bot.send_video(uid, p["content"], caption=cap, protect_content=True)
    elif p["kind"] == "photo":
        await bot.send_photo(uid, p["content"], caption=cap, protect_content=True)


async def show(c: CallbackQuery, text, markup=None):
    """Message edit kare; agar photo wala message ho to delete karke naya bheje."""
    m = c.message
    try:
        if m.text is None:
            try:
                await m.delete()
            except Exception:
                pass
            await m.answer(text, reply_markup=markup)
        else:
            await m.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as ex:
        if "not modified" not in str(ex):
            await m.answer(text, reply_markup=markup)


# ================= KEYBOARDS / TEXTS =================
def main_menu(uid):
    b = InlineKeyboardBuilder()
    b.button(text="🛍 Shop Products", callback_data="products")
    b.button(text="➕ Add Balance", callback_data="addbal")
    b.button(text="💰 Wallet", callback_data="wallet")
    b.button(text="📦 My Orders", callback_data="orders")
    b.button(text="🎟 Redeem Code", callback_data="redeem")
    b.button(text="🆘 Support", callback_data="support")
    rows = [1, 2, 2, 1]
    if uid == ADMIN_ID:
        b.button(text="🛠 Admin Panel", callback_data="adm:home")
        rows.append(1)
    b.adjust(*rows)
    return b.as_markup()


def back_menu(extra=None):
    b = InlineKeyboardBuilder()
    if extra:
        b.button(text=extra[0], callback_data=extra[1])
    b.button(text="⬅️ Menu", callback_data="menu")
    b.adjust(1)
    return b.as_markup()


def home_text(uid, name):
    orders = q1("SELECT COUNT(*) FROM purchases WHERE user_id=?", (uid,))
    return card(
        BRAND,
        f"👋 Hello, <b>{e(name)}</b>\n\n"
        f"💰 Balance: <b>₹{get_balance(uid)}</b>\n"
        f"📦 Orders: <b>{orders}</b>\n\n"
        "⚡ Instant Delivery  •  🔒 Secure  •  🕒 24/7",
    )


# ================= STATES =================
class Dep(StatesGroup):
    amount = State()
    utr = State()
    shot = State()


class Red(StatesGroup):
    code = State()


class AddP(StatesGroup):
    name = State()
    price = State()
    cat = State()
    descr = State()
    kind = State()
    content = State()


class StockAdd(StatesGroup):
    data = State()


class EditP(StatesGroup):
    price = State()


class Gen(StatesGroup):
    amount = State()


class Bcast(StatesGroup):
    text = State()


class BanMW(BaseMiddleware):
    async def __call__(self, handler, event, data):
        db.rollback()  # pichli update ka koi adhura transaction ho to saaf
        u = data.get("event_from_user")
        if u and u.id != ADMIN_ID and is_banned(u.id):
            if isinstance(event, CallbackQuery):
                await event.answer("🚫 Tumhara account band hai.", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("🚫 Tumhara account band hai.")
            return
        return await handler(event, data)


cmd_r = Router()    # /start, /cancel hamesha state clear karte hain
admin_r = Router()
user_r = Router()
admin_r.message.filter(F.from_user.id == ADMIN_ID)
admin_r.callback_query.filter(F.from_user.id == ADMIN_ID)


# ================= COMMON =================
@cmd_r.message(CommandStart())
@cmd_r.message(Command("cancel"))
async def start(m: Message, state: FSMContext):
    await state.clear()
    ensure_user(m.from_user.id, m.from_user.username)
    await m.answer(home_text(m.from_user.id, m.from_user.first_name), reply_markup=main_menu(m.from_user.id))


@user_r.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    ensure_user(c.from_user.id, c.from_user.username)
    await show(c, home_text(c.from_user.id, c.from_user.first_name), main_menu(c.from_user.id))
    await c.answer()


@user_r.callback_query(F.data == "support")
async def cb_support(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, card("Support", f"Koi problem ho to yaha contact karo:\n👉 {e(SUPPORT)}\n\nOrder ya deposit ka issue ho to apna UTR saath bhejo."), back_menu())
    await c.answer()


# ================= WALLET / ORDERS =================
@user_r.callback_query(F.data == "wallet")
async def cb_wallet(c: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = c.from_user.id
    spent = q1("SELECT SUM(price) FROM purchases WHERE user_id=?", (uid,))
    added = q1("SELECT SUM(amount) FROM deposits WHERE user_id=? AND status='approved'", (uid,))
    deps = db.execute("SELECT amount, status FROM deposits WHERE user_id=? ORDER BY id DESC LIMIT 5", (uid,)).fetchall()
    icon = {"approved": "✅", "pending": "⏳", "rejected": "❌"}
    t = (
        f"Balance: <b>₹{get_balance(uid)}</b>\n"
        f"Total added: ₹{added}  •  Total spent: ₹{spent}\n\n"
        "<b>Recent deposits</b>\n"
        + ("\n".join(f"{icon.get(x['status'], '•')} ₹{x['amount']} ({x['status']})" for x in deps) or "Abhi koi nahi")
    )
    await show(c, card("Wallet", t), back_menu(("➕ Add Balance", "addbal")))
    await c.answer()


@user_r.callback_query(F.data == "orders")
async def cb_orders(c: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = db.execute(
        "SELECT pu.id, p.name, pu.price FROM purchases pu JOIN products p ON p.id=pu.product_id "
        "WHERE pu.user_id=? ORDER BY pu.id DESC LIMIT 10",
        (c.from_user.id,),
    ).fetchall()
    if not rows:
        await show(c, card("My Orders", "Abhi tak koi order nahi."), back_menu(("🛍 Shop", "products")))
        return await c.answer()
    b = InlineKeyboardBuilder()
    for r in rows:
        b.button(text=f"📥 {r['name']} • ₹{r['price']}", callback_data=f"ord:{r['id']}")
    b.button(text="⬅️ Menu", callback_data="menu")
    b.adjust(1)
    await show(c, card("My Orders", "Kisi par tap karo, wo dobara mil jayega:"), b.as_markup())
    await c.answer()


@user_r.callback_query(F.data.startswith("ord:"))
async def cb_order_again(c: CallbackQuery, bot: Bot):
    pu = db.execute("SELECT * FROM purchases WHERE id=? AND user_id=?", (int(c.data[4:]), c.from_user.id)).fetchone()
    if not pu:
        return await c.answer("Order nahi mila", show_alert=True)
    p = db.execute("SELECT * FROM products WHERE id=?", (pu["product_id"],)).fetchone()
    try:
        await deliver(bot, c.from_user.id, p, pu["keydata"])
        await c.answer("✅ Dobara bhej diya")
    except Exception as ex:
        logging.exception(ex)
        await c.answer("Bhejne me dikkat aayi, support se baat karo.", show_alert=True)


# ================= SHOP =================
@user_r.callback_query(F.data == "products")
async def cb_products(c: CallbackQuery, state: FSMContext):
    await state.clear()
    cats = db.execute(
        "SELECT c.id, c.name, COUNT(p.id) n FROM categories c "
        "JOIN products p ON p.cat_id=c.id AND p.active=1 GROUP BY c.id ORDER BY c.name"
    ).fetchall()
    if not cats:
        return await show_products(c, "all")
    b = InlineKeyboardBuilder()
    for r in cats:
        b.button(text=f"📂 {r['name']} ({r['n']})", callback_data=f"pl:{r['id']}")
    if q1("SELECT COUNT(*) FROM products WHERE active=1 AND cat_id IS NULL"):
        b.button(text="🗂 Other", callback_data="pl:u")
    b.button(text="📚 All Products", callback_data="pl:all")
    b.button(text="⬅️ Menu", callback_data="menu")
    b.adjust(1)
    await show(c, card("Categories", "Category chuno:"), b.as_markup())
    await c.answer()


async def show_products(c: CallbackQuery, cat):
    sql, args = "SELECT * FROM products WHERE active=1", ()
    if cat == "u":
        sql += " AND cat_id IS NULL"
    elif cat != "all":
        sql, args = sql + " AND cat_id=?", (int(cat),)
    rows = db.execute(sql + " ORDER BY id", args).fetchall()
    if not rows:
        await show(c, card("Shop", "Abhi koi product nahi hai."), back_menu())
        return await c.answer()
    b = InlineKeyboardBuilder()
    for r in rows:
        tag = ""
        if r["kind"] == "keys" and stock_left(r["id"]) == 0:
            tag = " • 🔴 Sold out"
        b.button(text=f"{r['name']} • ₹{r['price']}{tag}", callback_data=f"buy:{r['id']}")
    b.button(text="⬅️ Back", callback_data="products" if cat != "all" else "menu")
    b.adjust(1)
    await show(c, card("Shop", "Kharidne ke liye product chuno:"), b.as_markup())
    await c.answer()


@user_r.callback_query(F.data.startswith("pl:"))
async def cb_plist(c: CallbackQuery):
    await show_products(c, c.data[3:])


@user_r.callback_query(F.data.startswith("buy:"))
async def cb_buy(c: CallbackQuery):
    pid = int(c.data[4:])
    p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
        return await c.answer("Product nahi mila", show_alert=True)
    bal = get_balance(c.from_user.id)
    t = f"<b>{e(p['name'])}</b>\n"
    if p["descr"]:
        t += f"{e(p['descr'])}\n"
    t += f"\n💵 Price: <b>₹{p['price']}</b>\n"
    soldout = False
    if p["kind"] == "keys":
        left = stock_left(pid)
        soldout = left == 0
        t += f"📦 Stock: <b>{'🔴 Sold out' if soldout else str(left) + ' left'}</b>\n"
    t += f"💰 Tumhara balance: ₹{bal}\n\n⚠️ Digital product hai, kharidne ke baad refund nahi hota."
    b = InlineKeyboardBuilder()
    if soldout:
        pass
    elif bal >= p["price"]:
        b.button(text=f"✅ Buy Now • ₹{p['price']}", callback_data=f"buyok:{pid}")
    else:
        b.button(text=f"➕ Add Balance (₹{p['price'] - bal} aur chahiye)", callback_data="addbal")
    b.button(text="⬅️ Back", callback_data="products")
    b.adjust(1)
    await show(c, card("Product", t), b.as_markup())
    await c.answer()


@user_r.callback_query(F.data.startswith("buyok:"))
async def cb_buyok(c: CallbackQuery, bot: Bot):
    uid = c.from_user.id
    status, p, info = buy_product(uid, int(c.data[6:]))
    if status == "gone":
        return await c.answer("Product available nahi", show_alert=True)
    if status == "nostock":
        return await c.answer("🔴 Stock khatam ho gaya.", show_alert=True)
    if status == "nobalance":
        return await c.answer(f"Balance kam hai! Chahiye ₹{p['price']}, hai ₹{get_balance(uid)}.", show_alert=True)
    try:
        await deliver(bot, uid, p, info["key"])
    except Exception as ex:
        logging.exception(ex)
        undo_purchase(uid, p["price"], info)  # delivery fail hui to paisa + stock wapas
        await show(c, card("Failed", "❌ Delivery fail hui. Paisa wapas wallet me aa gaya. Support se baat karo."), back_menu())
        return await c.answer()
    await show(c, card("Order Complete", f"✅ <b>{e(p['name'])}</b> mil gaya!\n💰 Bacha balance: ₹{get_balance(uid)}\n\n📦 Dobara chahiye to <b>My Orders</b> me dekho."), back_menu(("🛍 Aur Shopping", "products")))
    try:
        await bot.send_message(ADMIN_ID, f"🛒 <b>Sale</b>: {e(p['name'])} • ₹{p['price']}\nUser: <code>{uid}</code> @{e(c.from_user.username)}")
    except Exception:
        pass
    await c.answer()


# ================= REDEEM =================
@user_r.callback_query(F.data == "redeem")
async def cb_redeem(c: CallbackQuery, state: FSMContext):
    await state.set_state(Red.code)
    await show(c, card("Redeem", "🎟 Apna balance code bhejo:"), back_menu())
    await c.answer()


async def do_redeem(m: Message, code: str, state: FSMContext):
    ensure_user(m.from_user.id, m.from_user.username)
    amt = redeem_code(m.from_user.id, code.strip().upper())
    await state.clear()
    if amt is None:
        return await m.answer("❌ Code galat hai ya pehle use ho chuka hai.", reply_markup=back_menu())
    await m.answer(card("Redeemed", f"✅ ₹{amt} wallet me add ho gaye!\n💰 Balance: <b>₹{get_balance(m.from_user.id)}</b>"), reply_markup=main_menu(m.from_user.id))


@user_r.message(Command("redeem"))
async def cmd_redeem(m: Message, state: FSMContext):
    parts = (m.text or "").split()
    if len(parts) < 2:
        await state.set_state(Red.code)
        return await m.answer("🎟 Apna balance code bhejo:")
    await do_redeem(m, parts[1], state)


@user_r.message(Red.code, F.text)
async def red_code(m: Message, state: FSMContext):
    await do_redeem(m, m.text, state)


# ================= ADD BALANCE =================
@user_r.callback_query(F.data == "addbal")
async def cb_addbal(c: CallbackQuery, state: FSMContext):
    await state.clear()
    b = InlineKeyboardBuilder()
    for a in QUICK_AMOUNTS:
        if MIN_DEPOSIT <= a <= MAX_DEPOSIT:
            b.button(text=f"₹{a}", callback_data=f"amt:{a}")
    b.button(text="✏️ Custom amount", callback_data="amt:custom")
    b.button(text="⬅️ Menu", callback_data="menu")
    b.adjust(3, 3, 1)
    t = f"Kitna balance add karna hai?\nMin ₹{MIN_DEPOSIT}  •  Max ₹{MAX_DEPOSIT}"
    if BONUS_PERCENT > 0:
        t += f"\n\n🎁 ₹{BONUS_MIN}+ deposit pe <b>{BONUS_PERCENT}% extra bonus</b>!"
    await show(c, card("Add Balance", t), b.as_markup())
    await c.answer()


async def send_pay(target: Message, amt: int, state: FSMContext):
    await state.update_data(amount=amt)
    bonus = bonus_for(amt)
    t = card(
        "Payment",
        f"Amount: <b>₹{amt}</b>\n"
        + (f"🎁 Bonus: <b>+₹{bonus}</b>\n" if bonus else "")
        + f"\nUPI ID: <code>{e(UPI_ID)}</code>\nName: {e(UPI_NAME)}\n\n"
        f"1️⃣ QR scan karo ya UPI ID pe <b>exact ₹{amt}</b> bhejo\n"
        "2️⃣ Neeche <b>Maine Pay Kar Diya</b> dabao\n"
        "3️⃣ UTR number + screenshot bhejo",
    )
    b = InlineKeyboardBuilder()
    b.button(text="✅ Maine Pay Kar Diya", callback_data="paid")
    b.button(text="❌ Cancel", callback_data="menu")
    b.adjust(1)
    link = f"upi://pay?pa={quote(UPI_ID)}&pn={quote(UPI_NAME)}&am={amt}&cu=INR&tn={quote('Wallet topup')}"
    if qrcode:
        buf = io.BytesIO()
        qrcode.make(link).save(buf, format="PNG")
        await target.answer_photo(BufferedInputFile(buf.getvalue(), "upi_qr.png"), caption=t, reply_markup=b.as_markup())
    else:
        await target.answer(t, reply_markup=b.as_markup())


@user_r.callback_query(F.data.startswith("amt:"))
async def cb_amt(c: CallbackQuery, state: FSMContext):
    v = c.data[4:]
    if v == "custom":
        await state.set_state(Dep.amount)
        await show(c, card("Add Balance", f"Amount likho (₹{MIN_DEPOSIT} se ₹{MAX_DEPOSIT}), sirf number:"), back_menu())
        return await c.answer()
    amt = int(v)
    if not MIN_DEPOSIT <= amt <= MAX_DEPOSIT:
        return await c.answer("Amount sahi nahi", show_alert=True)
    try:
        await c.message.delete()
    except Exception:
        pass
    await send_pay(c.message, amt, state)
    await c.answer()


@user_r.message(Dep.amount, F.text)
async def dep_amount(m: Message, state: FSMContext):
    if not m.text.isdigit() or not MIN_DEPOSIT <= int(m.text) <= MAX_DEPOSIT:
        return await m.answer(f"Sahi amount likho (₹{MIN_DEPOSIT} se ₹{MAX_DEPOSIT}).")
    await send_pay(m, int(m.text), state)


@user_r.callback_query(F.data == "paid")
async def cb_paid(c: CallbackQuery, state: FSMContext):
    if "amount" not in await state.get_data():
        await state.clear()
        await show(c, home_text(c.from_user.id, c.from_user.first_name), main_menu(c.from_user.id))
        return await c.answer("Session expire ho gaya, dobara try karo.", show_alert=True)
    await state.set_state(Dep.utr)
    await show(c, card("Verify", "Apna <b>UTR / Transaction ID</b> bhejo\n(UPI app me payment detail me milta hai, 12 digit)"), back_menu())
    await c.answer()


@user_r.message(Dep.utr, F.text)
async def dep_utr(m: Message, state: FSMContext):
    utr = m.text.strip()
    if not utr.isalnum() or not 8 <= len(utr) <= 30:
        return await m.answer("UTR sahi nahi lag raha. 12 digit ka transaction ID bhejo.")
    if q1("SELECT 1 FROM deposits WHERE utr=? AND status!='rejected'", (utr,)):
        return await m.answer("❌ Ye UTR pehle hi use ho chuka hai.")
    if q1("SELECT COUNT(*) FROM deposits WHERE user_id=? AND status='pending'", (m.from_user.id,)) >= 3:
        await state.clear()
        return await m.answer("⏳ Tumhare 3 deposit pehle se pending hain. Approve hone ka wait karo.", reply_markup=back_menu())
    await state.update_data(utr=utr)
    await state.set_state(Dep.shot)
    await m.answer("📸 Ab payment ka <b>screenshot</b> bhejo.")


def dep_caption(d):
    u = db.execute("SELECT username FROM users WHERE id=?", (d["user_id"],)).fetchone()
    uname = f"@{u['username']}" if u and u["username"] else "-"
    bonus = bonus_for(d["amount"])
    return (
        f"💸 <b>Deposit #{d['id']}</b>\nUser: <code>{d['user_id']}</code> {e(uname)}\n"
        f"Paid: <b>₹{d['amount']}</b>" + (f"  (+₹{bonus} bonus)" if bonus else "") +
        f"\nUTR: <code>{e(d['utr'])}</code>\n\n⚠️ Pehle bank app me paisa check karo, phir Approve dabao."
    )


def dep_buttons(dep_id):
    b = InlineKeyboardBuilder()
    b.button(text="✅ Approve", callback_data=f"ap:{dep_id}")
    b.button(text="❌ Reject", callback_data=f"rj:{dep_id}")
    return b.as_markup()


@user_r.message(Dep.shot, F.photo)
async def dep_shot(m: Message, state: FSMContext, bot: Bot):
    d = await state.get_data()
    ensure_user(m.from_user.id, m.from_user.username)
    try:
        dep_id = db.execute(
            "INSERT INTO deposits(user_id, amount, utr, shot) VALUES(?,?,?,?) RETURNING id",
            (m.from_user.id, d["amount"], d["utr"], m.photo[-1].file_id),
        ).fetchone()["id"]
        db.commit()
    except (pgerr.UniqueViolation, KeyError):
        await state.clear()
        return await m.answer("❌ Ye UTR pehle hi use ho chuka hai.")
    await state.clear()
    dep = db.execute("SELECT * FROM deposits WHERE id=?", (dep_id,)).fetchone()
    await bot.send_photo(ADMIN_ID, dep["shot"], caption=dep_caption(dep), reply_markup=dep_buttons(dep["id"]))
    await m.answer(card("Submitted", f"✅ Request #{dep['id']} bhej di gayi.\nAdmin verify karke jaldi balance code bhej dega."), reply_markup=back_menu())


@user_r.message(Dep.shot)
async def dep_shot_bad(m: Message):
    await m.answer("Screenshot (photo) bhejo.")


# ================= ADMIN =================
def admin_menu():
    b = InlineKeyboardBuilder()
    b.button(text="➕ Add Product", callback_data="adm:add")
    b.button(text="🗑 Remove Product", callback_data="adm:del")
    b.button(text="📋 Product List", callback_data="adm:list")
    b.button(text="📊 Stats", callback_data="adm:stats")
    b.button(text="👥 Users List", callback_data="adm:users")
    b.button(text="📢 Broadcast", callback_data="adm:bc")
    b.button(text="🔑 Add Stock", callback_data="adm:stock")
    b.button(text="💲 Edit Price", callback_data="adm:price")
    b.button(text="💸 Pending", callback_data="adm:pend")
    b.button(text="🎟 Make Code", callback_data="adm:gen")
    b.button(text="💾 Backup DB", callback_data="adm:backup")
    b.button(text="⬅️ Menu", callback_data="menu")
    b.adjust(2)
    return b.as_markup()


def today_clause(col):
    """IST ke hisaab se 'aaj' ka SQL condition."""
    return f"({col} AT TIME ZONE 'Asia/Kolkata')::date = (now() AT TIME ZONE 'Asia/Kolkata')::date"


def admin_text():
    return "🔧 <b>ADMIN PANEL</b>"


@admin_r.message(Command("admin"))
async def admin_panel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(admin_text(), reply_markup=admin_menu())


@admin_r.message(Command("addbal"))
async def admin_addbal(m: Message):
    p = (m.text or "").split()
    if len(p) != 3 or not p[1].isdigit() or not p[2].lstrip("-").isdigit():
        return await m.answer("Use: /addbal USER_ID AMOUNT")
    db.execute("UPDATE users SET balance = balance + ? WHERE id=?", (int(p[2]), int(p[1])))
    db.commit()
    await m.answer(f"Done. New balance: ₹{get_balance(int(p[1]))}")


@admin_r.message(Command("ban"))
@admin_r.message(Command("unban"))
async def admin_ban(m: Message):
    p = (m.text or "").split()
    if len(p) != 2 or not p[1].isdigit():
        return await m.answer("Use: /ban USER_ID  ya  /unban USER_ID")
    flag = 1 if (m.text or "").startswith("/ban") else 0
    db.execute("UPDATE users SET banned=? WHERE id=?", (flag, int(p[1])))
    db.commit()
    await m.answer("🚫 Banned" if flag else "✅ Unbanned")


def void_code(code):
    """Unused code ko cancel karta hai. Return: (status, value)."""
    row = db.execute(
        "UPDATE codes SET used=1, used_by=0 WHERE code=? AND used=0 RETURNING amount", (code,)
    ).fetchone()
    db.commit()
    if row:
        return "voided", row["amount"]
    r = db.execute("SELECT used_by FROM codes WHERE code=?", (code,)).fetchone()
    if not r:
        return "missing", None
    return "used", r["used_by"]


@admin_r.message(Command("delcode"))
async def admin_delcode(m: Message):
    p = (m.text or "").split()
    if len(p) != 2:
        return await m.answer("Use: /delcode CODE\nJaise: /delcode 95854B64")
    code = p[1].strip().upper()
    st, v = void_code(code)
    if st == "voided":
        return await m.answer(f"✅ Code <code>{e(code)}</code> (₹{v}) cancel ho gaya. Ab ise koi redeem nahi kar sakta.")
    if st == "missing":
        return await m.answer("❌ Ye code database me nahi mila.")
    if v == 0:
        return await m.answer("Ye code pehle hi cancel ho chuka hai.")
    await m.answer(
        f"⚠️ Ye code pehle hi redeem ho chuka hai (user <code>{v}</code>).\n"
        f"Uska balance wapas lena ho to: <code>/addbal {v} -50</code> (50 ki jagah jitna kaatna ho)\n"
        f"Details dekhne ke liye: <code>/user {v}</code>"
    )


@admin_r.message(Command("user"))
async def admin_user(m: Message):
    p = (m.text or "").split()
    if len(p) != 2 or not p[1].isdigit():
        return await m.answer("Use: /user USER_ID")
    uid = int(p[1])
    u = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        return await m.answer("User nahi mila.")
    n_orders = q1("SELECT COUNT(*) FROM purchases WHERE user_id=?", (uid,))
    spent = q1("SELECT SUM(price) FROM purchases WHERE user_id=?", (uid,))
    added = q1("SELECT SUM(amount) FROM deposits WHERE user_id=? AND status='approved'", (uid,))
    banned = "Yes 🚫" if u["banned"] else "No"
    await m.answer(
        card(
            "User",
            f"ID: <code>{uid}</code>  @{e(u['username'])}\n"
            f"Balance: <b>₹{u['balance']}</b>\n"
            f"Orders: {n_orders} (₹{spent})\n"
            f"Deposits: ₹{added}\n"
            f"Banned: {banned}\n\n"
            f"/ban {uid}  •  /unban {uid}  •  /addbal {uid} 100",
        )
    )


@admin_r.callback_query(F.data == "adm:home")
async def adm_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, admin_text(), admin_menu())
    await c.answer()


# ---- Deposit approve / reject ----
@admin_r.callback_query(F.data.startswith("ap:"))
async def adm_approve(c: CallbackQuery, bot: Bot):
    dep_id = int(c.data[3:])
    cur = db.execute("UPDATE deposits SET status='approved' WHERE id=? AND status='pending'", (dep_id,))
    if cur.rowcount == 0:
        db.rollback()
        return await c.answer("Ye pehle hi handle ho chuka hai", show_alert=True)
    db.commit()
    d = db.execute("SELECT * FROM deposits WHERE id=?", (dep_id,)).fetchone()
    bonus = bonus_for(d["amount"])
    credit = d["amount"] + bonus
    code = make_code(credit)
    try:
        await bot.send_message(
            d["user_id"],
            card(
                "Payment Approved",
                f"✅ ₹{d['amount']} receive ho gaye!"
                + (f"\n🎁 Bonus: +₹{bonus}" if bonus else "")
                + f"\n\nTumhara balance code (₹{credit}):\n<code>{code}</code>\n\n"
                f"<b>Redeem Code</b> dabao ya <code>/redeem {code}</code> likho.",
            ),
        )
        sent = "user ko bhej diya"
    except Exception:
        sent = "user ko bhej NAHI paya (bot block?), code manually do"
    await c.message.edit_caption(caption=(c.message.caption or "") + f"\n\n✅ APPROVED • code: {code}\n{sent}", reply_markup=None)
    await c.answer("Approved")


@admin_r.callback_query(F.data.startswith("rj:"))
async def adm_reject(c: CallbackQuery, bot: Bot):
    dep_id = int(c.data[3:])
    cur = db.execute("UPDATE deposits SET status='rejected' WHERE id=? AND status='pending'", (dep_id,))
    if cur.rowcount == 0:
        db.rollback()
        return await c.answer("Ye pehle hi handle ho chuka hai", show_alert=True)
    db.commit()
    d = db.execute("SELECT * FROM deposits WHERE id=?", (dep_id,)).fetchone()
    try:
        await bot.send_message(d["user_id"], f"❌ Tumhara ₹{d['amount']} ka deposit reject ho gaya. Galti lage to {e(SUPPORT)} se baat karo.")
    except Exception:
        pass
    await c.message.edit_caption(caption=(c.message.caption or "") + "\n\n❌ REJECTED", reply_markup=None)
    await c.answer("Rejected")


@admin_r.callback_query(F.data == "adm:pend")
async def adm_pending(c: CallbackQuery, bot: Bot):
    rows = db.execute("SELECT * FROM deposits WHERE status='pending' ORDER BY id LIMIT 10").fetchall()
    if not rows:
        await c.message.answer("✅ Koi pending deposit nahi.")
        return await c.answer()
    for d in rows:
        if d["shot"]:
            await bot.send_photo(ADMIN_ID, d["shot"], caption=dep_caption(d), reply_markup=dep_buttons(d["id"]))
        else:
            await bot.send_message(ADMIN_ID, dep_caption(d), reply_markup=dep_buttons(d["id"]))
    await c.answer()


# ---- Add product ----
@admin_r.callback_query(F.data == "adm:add")
async def adm_add(c: CallbackQuery, state: FSMContext):
    await state.set_state(AddP.name)
    await c.message.answer("📦 Product ka naam likho:")
    await c.answer()


@admin_r.message(AddP.name, F.text)
async def addp_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip())
    await state.set_state(AddP.price)
    await m.answer("💵 Price (₹) likho, sirf number:")


@admin_r.message(AddP.price, F.text)
async def addp_price(m: Message, state: FSMContext):
    if not m.text.isdigit() or int(m.text) <= 0:
        return await m.answer("Sahi number likho.")
    await state.update_data(price=int(m.text))
    await state.set_state(AddP.cat)
    cats = [r["name"] for r in db.execute("SELECT name FROM categories ORDER BY name")]
    hint = ("\nMojuda: " + ", ".join(cats)) if cats else ""
    await m.answer(f"📂 Category ka naam likho (ya <code>-</code> agar nahi chahiye).{e(hint)}")


@admin_r.message(AddP.cat, F.text)
async def addp_cat(m: Message, state: FSMContext):
    t = m.text.strip()
    await state.update_data(cat_id=None if t == "-" else get_cat(t))
    await state.set_state(AddP.descr)
    await m.answer("📝 Chhota description likho (ya <code>-</code>):")


@admin_r.message(AddP.descr, F.text)
async def addp_descr(m: Message, state: FSMContext):
    t = m.text.strip()
    await state.update_data(descr="" if t == "-" else t)
    await state.set_state(AddP.kind)
    b = InlineKeyboardBuilder()
    b.button(text="📁 File / APK / Video / Text (ek hi cheez)", callback_data="pk:single")
    b.button(text="🔑 Keys / Accounts (stock, ek sale = ek key)", callback_data="pk:keys")
    b.adjust(1)
    await m.answer("Delivery type chuno:", reply_markup=b.as_markup())


@admin_r.callback_query(AddP.kind, F.data.startswith("pk:"))
async def addp_kind(c: CallbackQuery, state: FSMContext):
    k = c.data[3:]
    await state.update_data(pkind=k)
    await state.set_state(AddP.content)
    if k == "keys":
        await c.message.answer("🔑 Keys bhejo, <b>har line me ek key</b> (ek message me jitni chaho):")
    else:
        await c.message.answer("📎 Ab wo cheez bhejo jo customer ko milegi: <b>file / APK / video / photo</b> ya <b>text</b> (link, code).")
    await c.answer()


@admin_r.message(AddP.content)
async def addp_content(m: Message, state: FSMContext):
    d = await state.get_data()
    lines = []
    if d.get("pkind") == "keys":
        if not m.text:
            return await m.answer("Text bhejo, har line me ek key.")
        lines = [x.strip() for x in m.text.splitlines() if x.strip()]
        if not lines:
            return await m.answer("Koi key nahi mili, dobara bhejo.")
        kind, content = "keys", ""
    elif m.document:
        kind, content = "document", m.document.file_id
    elif m.video:
        kind, content = "video", m.video.file_id
    elif m.photo:
        kind, content = "photo", m.photo[-1].file_id
    elif m.text:
        kind, content = "text", m.text
    else:
        return await m.answer("File ya text bhejo.")
    new_id = db.execute(
        "INSERT INTO products(name, price, kind, content, descr, cat_id) VALUES(?,?,?,?,?,?) RETURNING id",
        (d["name"], d["price"], kind, content, d.get("descr", ""), d.get("cat_id")),
    ).fetchone()["id"]
    for ln in lines:
        db.execute("INSERT INTO stock(product_id, data) VALUES(?,?)", (new_id, ln))
    db.commit()
    await state.clear()
    extra = f"\n🔑 Stock: {len(lines)}" if lines else ""
    await m.answer(card("Product Added", f"✅ {e(d['name'])} • ₹{d['price']}{extra}"), reply_markup=admin_menu())


# ---- Remove ----
@admin_r.callback_query(F.data == "adm:del")
async def adm_del(c: CallbackQuery):
    rows = db.execute("SELECT id, name, price FROM products WHERE active=1").fetchall()
    if not rows:
        await c.message.answer("Koi product nahi hai.")
        return await c.answer()
    b = InlineKeyboardBuilder()
    for r in rows:
        b.button(text=f"🗑 {r['name']} (₹{r['price']})", callback_data=f"dl:{r['id']}")
    b.adjust(1)
    await c.message.answer("Kaunsa remove karna hai?", reply_markup=b.as_markup())
    await c.answer()


@admin_r.callback_query(F.data.startswith("dl:"))
async def adm_del_do(c: CallbackQuery):
    db.execute("UPDATE products SET active=0 WHERE id=?", (int(c.data[3:]),))
    db.commit()
    await c.message.edit_text("🗑 Remove ho gaya.")
    await c.answer()


# ---- Add stock ----
@admin_r.callback_query(F.data == "adm:stock")
async def adm_stock(c: CallbackQuery):
    rows = db.execute("SELECT id, name FROM products WHERE active=1 AND kind='keys'").fetchall()
    if not rows:
        await c.message.answer("Koi 'Keys' type product nahi hai.")
        return await c.answer()
    b = InlineKeyboardBuilder()
    for r in rows:
        b.button(text=f"🔑 {r['name']} ({stock_left(r['id'])} left)", callback_data=f"st:{r['id']}")
    b.adjust(1)
    await c.message.answer("Kis product me stock add karna hai?", reply_markup=b.as_markup())
    await c.answer()


@admin_r.callback_query(F.data.startswith("st:"))
async def adm_stock_pick(c: CallbackQuery, state: FSMContext):
    await state.set_state(StockAdd.data)
    await state.update_data(pid=int(c.data[3:]))
    await c.message.answer("Keys bhejo, har line me ek:")
    await c.answer()


@admin_r.message(StockAdd.data, F.text)
async def stock_data(m: Message, state: FSMContext):
    d = await state.get_data()
    lines = [x.strip() for x in m.text.splitlines() if x.strip()]
    for ln in lines:
        db.execute("INSERT INTO stock(product_id, data) VALUES(?,?)", (d["pid"], ln))
    db.commit()
    await state.clear()
    await m.answer(f"✅ {len(lines)} key add hui. Ab stock: {stock_left(d['pid'])}", reply_markup=admin_menu())


# ---- Edit price ----
@admin_r.callback_query(F.data == "adm:price")
async def adm_price(c: CallbackQuery):
    rows = db.execute("SELECT id, name, price FROM products WHERE active=1").fetchall()
    if not rows:
        await c.message.answer("Koi product nahi hai.")
        return await c.answer()
    b = InlineKeyboardBuilder()
    for r in rows:
        b.button(text=f"{r['name']} (₹{r['price']})", callback_data=f"pr:{r['id']}")
    b.adjust(1)
    await c.message.answer("Kiska price badalna hai?", reply_markup=b.as_markup())
    await c.answer()


@admin_r.callback_query(F.data.startswith("pr:"))
async def adm_price_pick(c: CallbackQuery, state: FSMContext):
    await state.set_state(EditP.price)
    await state.update_data(pid=int(c.data[3:]))
    await c.message.answer("Naya price (₹) likho:")
    await c.answer()


@admin_r.message(EditP.price, F.text)
async def editp_price(m: Message, state: FSMContext):
    if not m.text.isdigit() or int(m.text) <= 0:
        return await m.answer("Sahi number likho.")
    d = await state.get_data()
    db.execute("UPDATE products SET price=? WHERE id=?", (int(m.text), d["pid"]))
    db.commit()
    await state.clear()
    await m.answer(f"✅ Price ₹{m.text} ho gaya.", reply_markup=admin_menu())


# ---- Manual code ----
@admin_r.callback_query(F.data == "adm:gen")
async def adm_gen(c: CallbackQuery, state: FSMContext):
    await state.set_state(Gen.amount)
    await c.message.answer("Code ki value (₹) likho:")
    await c.answer()


@admin_r.message(Gen.amount, F.text)
async def gen_amount(m: Message, state: FSMContext):
    if not m.text.isdigit() or int(m.text) <= 0:
        return await m.answer("Sahi number likho.")
    code = make_code(int(m.text))
    await state.clear()
    await m.answer(f"🎟 Code (₹{m.text}):\n<code>{code}</code>", reply_markup=admin_menu())


# ---- Stats ----
@admin_r.callback_query(F.data == "adm:stats")
async def adm_stats(c: CallbackQuery):
    users = q1("SELECT COUNT(*) FROM users")
    new_today = q1("SELECT COUNT(*) FROM users WHERE " + today_clause("joined"))
    banned = q1("SELECT COUNT(*) FROM users WHERE banned=1")
    sale_today = q1("SELECT SUM(price) FROM purchases WHERE " + today_clause("created"))
    sale_total = q1("SELECT SUM(price) FROM purchases")
    orders = q1("SELECT COUNT(*) FROM purchases")
    dep_total = q1("SELECT SUM(amount) FROM deposits WHERE status='approved'")
    pending = q1("SELECT COUNT(*) FROM deposits WHERE status='pending'")
    wallets = q1("SELECT SUM(balance) FROM users")
    prods = q1("SELECT COUNT(*) FROM products WHERE active=1")
    t = (
        "📊 <b>Stats</b>\n\n"
        f"👥 Total users: {users} (aaj naye: {new_today})\n"
        f"📱 Total products: {prods}\n"
        f"🚫 Banned: {banned}\n\n"
        f"💵 Aaj ki sale: ₹{sale_today}\n"
        f"💵 Total sale: ₹{sale_total}\n"
        f"📦 Total orders: {orders}\n\n"
        f"💳 Total deposit (approved): ₹{dep_total}\n"
        f"⏳ Pending deposits: {pending}\n"
        f"🏦 Users ke wallet me total: ₹{wallets}\n\n"
        f"🗄 Database: {db_days_left()[0]} din baaki ({DB_LIFETIME_DAYS} din ka plan)"
    )
    await c.message.answer(t)
    await c.answer()


@admin_r.callback_query(F.data == "adm:list")
async def adm_list(c: CallbackQuery):
    rows = db.execute("SELECT * FROM products WHERE active=1 ORDER BY id").fetchall()
    if not rows:
        await c.message.answer("📋 Abhi koi product nahi hai.")
        return await c.answer()
    lines = []
    for r in rows:
        extra = f" • 🔑 {stock_left(r['id'])} left" if r["kind"] == "keys" else ""
        lines.append(f"{r['id']}. {e(r['name'])} — ₹{r['price']}{extra}")
    await c.message.answer("📋 <b>Product List</b>\n\n" + "\n".join(lines[:60]))
    await c.answer()


@admin_r.callback_query(F.data == "adm:users")
async def adm_users(c: CallbackQuery):
    rows = db.execute("SELECT * FROM users ORDER BY joined DESC, id DESC LIMIT 30").fetchall()
    total = q1("SELECT COUNT(*) FROM users")
    lines = []
    for u in rows:
        name = f"@{u['username']}" if u["username"] else "-"
        ban = " 🚫" if u["banned"] else ""
        lines.append(f"<code>{u['id']}</code> {e(name)} • ₹{u['balance']}{ban}")
    await c.message.answer(f"👥 <b>Users List</b> (last 30 / {total})\n\n" + "\n".join(lines) + "\n\nDetail: /user ID")
    await c.answer()


# ---- Broadcast ----
@admin_r.callback_query(F.data == "adm:bc")
async def adm_bc(c: CallbackQuery, state: FSMContext):
    await state.set_state(Bcast.text)
    await c.message.answer("📢 Jo message sab users ko bhejna hai wo likho.\n\n/cancel se band karo")
    await c.answer()


@admin_r.message(Bcast.text, F.text)
async def bc_send(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    ids = [r["id"] for r in db.execute("SELECT id FROM users WHERE banned=0").fetchall()]
    await m.answer(f"📥 {len(ids)} users ko bhej raha hoon...")
    ok = fail = 0
    for uid in ids:
        try:
            await bot.send_message(uid, m.text)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await m.answer(f"✅ <b>Broadcast done</b>\nSent: {ok}\nFailed: {fail}")


# ---- Backup / Restore (JSON) ----
TABLES = ["users", "categories", "products", "stock", "deposits", "codes", "purchases"]
SERIAL_TABLES = ["categories", "products", "stock", "deposits", "purchases"]


def export_json():
    data = {t: db.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in TABLES}
    return json.dumps(data, default=str, ensure_ascii=False).encode()


async def send_backup(bot: Bot, caption="💾 Database backup"):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    users = q1("SELECT COUNT(*) FROM users")
    await bot.send_document(
        ADMIN_ID,
        BufferedInputFile(export_json(), f"shop_backup_{stamp}.json"),
        caption=f"{caption}\n👥 Users: {users}\n\nRestore: ye file bot ko bhejo, caption me /restore likho.",
    )


@admin_r.callback_query(F.data == "adm:backup")
async def adm_backup(c: CallbackQuery, bot: Bot):
    await send_backup(bot)
    await c.answer("Backup bhej diya")


@admin_r.message(F.document, F.caption.startswith("/restore"))
async def admin_restore(m: Message, bot: Bot):
    force = "force" in (m.caption or "").lower()
    if (q1("SELECT COUNT(*) FROM users") + q1("SELECT COUNT(*) FROM products")) > 0 and not force:
        return await m.answer(
            "⚠️ Database khali nahi hai. Purana data hata kar restore karna ho to caption me "
            "<code>/restore force</code> likho."
        )
    buf = io.BytesIO()
    await bot.download(m.document, destination=buf)
    try:
        data = json.loads(buf.getvalue().decode())
        assert all(isinstance(data.get(t), list) for t in TABLES)
    except Exception:
        return await m.answer("❌ Ye valid backup file nahi hai.")
    try:
        for t in TABLES:
            db.execute(f"DELETE FROM {t}")
        for t in TABLES:
            for row in data[t]:
                cols = list(row.keys())
                assert all(col.isidentifier() for col in cols)
                db.execute(
                    f"INSERT INTO {t}({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                    tuple(row[col] for col in cols),
                )
        for t in SERIAL_TABLES:
            db.execute(
                f"SELECT setval(pg_get_serial_sequence('{t}','id'), "
                f"COALESCE((SELECT MAX(id) FROM {t}),1), (SELECT MAX(id) FROM {t}) IS NOT NULL)"
            )
        db.commit()
    except Exception as ex:
        db.rollback()
        logging.exception(ex)
        return await m.answer(f"❌ Restore fail hua: {e(ex)}")
    await m.answer(
        "✅ <b>Restore ho gaya</b>\n"
        f"👥 Users: {q1('SELECT COUNT(*) FROM users')}\n"
        f"📱 Products: {q1('SELECT COUNT(*) FROM products')}\n"
        f"📦 Orders: {q1('SELECT COUNT(*) FROM purchases')}"
    )


async def backup_loop(bot: Bot):
    await asyncio.sleep(90)  # start hote hi ek backup, phir har 12 ghante
    while True:
        try:
            if q1("SELECT COUNT(*) FROM users") > 0:
                await send_backup(bot, "💾 Auto backup")
        except Exception as ex:
            logging.warning("backup fail: %s", ex)
        await asyncio.sleep(12 * 3600)


# ================= WEBSITE / DASHBOARD (Render URL par dikhega) =================
IST = timezone(timedelta(hours=5, minutes=30))
START_TIME = time.time()
BOT_USERNAME = ""
_cache = {}


def cached(key, ttl, fn):
    """Page par bahut hits aaye to DB par load na pade (15 sec cache)."""
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val


def fmt(n):
    return f"{int(n):,}"


def uptime_text():
    s = int(time.time() - START_TIME)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    return f"{d}d {h}h {m}m" if d else f"{h}h {m}m"


# ---- Database expiry (Render free Postgres) ----
def meta_get(k):
    r = db.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
    return r["value"] if r else None


def meta_set(k, v):
    db.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (k, v),
    )
    db.commit()


def db_created_at():
    raw = os.getenv("DB_CREATED_ON") or meta_get("db_created")
    dt = datetime.fromisoformat(raw)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def db_days_left():
    """(bache hue din, ab tak ke din). DB_LIFETIME_DAYS .env se (default 30)."""
    used = (datetime.now(timezone.utc) - db_created_at()).total_seconds() / 86400
    return max(0, DB_LIFETIME_DAYS - int(used)), int(used)


async def expiry_loop(bot: Bot):
    """Database expire hone se pehle admin ko warning + backup bhejta hai."""
    await asyncio.sleep(120)
    while True:
        try:
            left, _ = db_days_left()
            level = "warn0" if left <= 0 else "warn2" if left <= 2 else "warn7" if left <= 7 else None
            if level and not meta_get(level):
                meta_set(level, "1")
                if left <= 0:
                    txt = "🚨 <b>Database expire ho gaya/ho raha hai!</b>"
                else:
                    txt = f"⚠️ <b>Database {left} din me expire hoga!</b>"
                txt += (
                    "\n\nKya karna hai:\n1️⃣ Render me naya free Postgres banao\n"
                    "2️⃣ Service me <code>DATABASE_URL</code> naya daalo, redeploy karo\n"
                    "3️⃣ Neeche wali backup file bot ko bhejo, caption me <code>/restore</code>"
                )
                await bot.send_message(ADMIN_ID, txt)
                await send_backup(bot, "💾 Expiry se pehle backup")
        except Exception as ex:
            logging.warning("expiry check fail: %s", ex)
        await asyncio.sleep(6 * 3600)


# ---- Stats ----
def get_public_stats():
    return {
        "users": q1("SELECT COUNT(*) FROM users"),
        "new_today": q1("SELECT COUNT(*) FROM users WHERE " + today_clause("joined")),
        "products": q1("SELECT COUNT(*) FROM products WHERE active=1"),
        "orders": q1("SELECT COUNT(*) FROM purchases"),
    }


def get_private_stats():
    left, used = db_days_left()
    return {
        "sale_today": q1("SELECT SUM(price) FROM purchases WHERE " + today_clause("created")),
        "orders_today": q1("SELECT COUNT(*) FROM purchases WHERE " + today_clause("created")),
        "sale_total": q1("SELECT SUM(price) FROM purchases"),
        "dep_total": q1("SELECT SUM(amount) FROM deposits WHERE status='approved'"),
        "pending": q1("SELECT COUNT(*) FROM deposits WHERE status='pending'"),
        "wallets": q1("SELECT SUM(balance) FROM users"),
        "banned": q1("SELECT COUNT(*) FROM users WHERE banned=1"),
        "db_left": left,
        "db_used": used,
        "top": db.execute(
            "SELECT p.name, COUNT(*) AS n, SUM(pu.price) AS rev FROM purchases pu "
            "JOIN products p ON p.id=pu.product_id GROUP BY p.id ORDER BY n DESC LIMIT 5"
        ).fetchall(),
        "recent": db.execute(
            "SELECT p.name, pu.price, pu.user_id, pu.created FROM purchases pu "
            "JOIN products p ON p.id=pu.product_id ORDER BY pu.id DESC LIMIT 8"
        ).fetchall(),
    }


# ---- HTML ----
PAGE_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#080b14;color:#e8ecf5;
min-height:100vh;background-image:radial-gradient(900px 500px at 15% -10%,#2a1f6b55,transparent),
radial-gradient(800px 500px at 100% 0%,#0b6a8a44,transparent);padding:24px 16px 48px}
.wrap{max-width:920px;margin:0 auto}
.hero{text-align:center;padding:36px 8px 28px}
.logo{font-size:44px;margin-bottom:6px}
h1{font-size:clamp(26px,6vw,40px);font-weight:800;letter-spacing:-.5px;
background:linear-gradient(90deg,#a99bff,#5ee1ff);-webkit-background-clip:text;background-clip:text;color:transparent}
.sub{color:#8b94ad;margin-top:8px;font-size:15px}
.pill{display:inline-flex;align-items:center;gap:8px;margin-top:18px;padding:8px 16px;border-radius:999px;
background:#ffffff0d;border:1px solid #ffffff1a;font-size:14px}
.dot{width:10px;height:10px;border-radius:50%;background:#2ee59d;box-shadow:0 0 0 0 #2ee59d99;animation:pulse 1.8s infinite}
.pill.bad .dot{background:#ff5c6c;box-shadow:none;animation:none}
@keyframes pulse{70%{box-shadow:0 0 0 10px #2ee59d00}100%{box-shadow:0 0 0 0 #2ee59d00}}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin-top:26px}
.card{background:linear-gradient(160deg,#ffffff10,#ffffff05);border:1px solid #ffffff14;border-radius:18px;
padding:20px 18px;backdrop-filter:blur(8px);transition:transform .2s,border-color .2s}
.card:hover{transform:translateY(-3px);border-color:#a99bff66}
.ico{font-size:24px}
.num{font-size:32px;font-weight:800;margin-top:8px;font-variant-numeric:tabular-nums}
.lbl{color:#8b94ad;font-size:13px;margin-top:2px}
.btn{display:inline-block;margin-top:22px;padding:13px 26px;border-radius:14px;color:#fff;text-decoration:none;
font-weight:700;background:linear-gradient(90deg,#7c5cff,#00b8ff);box-shadow:0 8px 30px #7c5cff44}
.feat{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-top:30px}
.feat div{padding:14px 16px;border-radius:14px;background:#ffffff08;border:1px solid #ffffff10;font-size:14px;color:#c4cbe0}
h2{font-size:18px;margin:38px 0 4px}
.bar{height:12px;border-radius:99px;background:#ffffff14;overflow:hidden;margin-top:12px}
.bar i{display:block;height:100%;border-radius:99px}
table{width:100%;border-collapse:collapse;margin-top:12px;font-size:14px}
th,td{text-align:left;padding:10px 8px;border-bottom:1px solid #ffffff12}
th{color:#8b94ad;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.5px}
.foot{text-align:center;color:#5d6682;font-size:12px;margin-top:40px}
"""

COUNT_JS = (
    "document.querySelectorAll('[data-n]').forEach(function(el){var t=+el.dataset.n,i=0,s=28;"
    "if(!t)return;(function f(){i++;el.textContent=(el.dataset.p||'')+Math.round(t*i/s).toLocaleString('en-US');"
    "if(i<s)requestAnimationFrame(f)})()});"
)


def stat_card(icon, label, value, prefix=""):
    return (
        f'<div class="card"><div class="ico">{icon}</div>'
        f'<div class="num" data-n="{int(value)}" data-p="{prefix}">{prefix}{fmt(value)}</div>'
        f'<div class="lbl">{e(label)}</div></div>'
    )


def render_page(pub, priv=None, ok=True):
    now = datetime.now(IST).strftime("%d %b %Y, %I:%M %p")
    status = "Online" if ok else "Database error"
    cards = "".join(
        [
            stat_card("👥", "Total Users", pub["users"]),
            stat_card("🆕", "New Users Today", pub["new_today"]),
            stat_card("🛍", "Products", pub["products"]),
            stat_card("📦", "Orders Delivered", pub["orders"]),
        ]
    )
    btn = f'<a class="btn" href="https://t.me/{e(BOT_USERNAME)}">Open on Telegram</a>' if BOT_USERNAME else ""
    private = ""
    robots = ""
    if priv:
        robots = '<meta name="robots" content="noindex,nofollow">'
        left, used = priv["db_left"], priv["db_used"]
        pct = min(100, int(used * 100 / max(1, DB_LIFETIME_DAYS)))
        color = "#2ee59d" if left > 10 else "#ffb020" if left > 3 else "#ff5c6c"
        top = "".join(
            f"<tr><td>{e(r['name'])}</td><td>{r['n']}</td><td>₹{fmt(r['rev'])}</td></tr>" for r in priv["top"]
        ) or '<tr><td colspan="3">Abhi koi sale nahi</td></tr>'
        recent = "".join(
            f"<tr><td>{e(r['name'])}</td><td>₹{fmt(r['price'])}</td><td>…{str(r['user_id'])[-4:]}</td>"
            f"<td>{r['created'].astimezone(IST).strftime('%d %b %I:%M %p')}</td></tr>"
            for r in priv["recent"]
        ) or '<tr><td colspan="4">Abhi koi order nahi</td></tr>'
        private = (
            "<h2>🔒 Private Dashboard</h2>"
            '<div class="grid">'
            + stat_card("💵", "Sales Today", priv["sale_today"], "₹")
            + stat_card("🧾", "Orders Today", priv["orders_today"])
            + stat_card("💰", "Total Sales", priv["sale_total"], "₹")
            + stat_card("💳", "Deposits (approved)", priv["dep_total"], "₹")
            + stat_card("⏳", "Pending Deposits", priv["pending"])
            + stat_card("🏦", "Users Wallet Total", priv["wallets"], "₹")
            + stat_card("🚫", "Banned Users", priv["banned"])
            + "</div>"
            '<div class="card" style="margin-top:14px"><div class="lbl">🗄 Database expiry</div>'
            f'<div class="num" style="color:{color}">{left} din baaki</div>'
            f'<div class="lbl">{used} din use hue • plan {DB_LIFETIME_DAYS} din ka</div>'
            f'<div class="bar"><i style="width:{pct}%;background:{color}"></i></div></div>'
            '<h2>🏆 Top Products</h2><div class="card"><table><tr><th>Product</th><th>Sold</th><th>Revenue</th></tr>'
            f"{top}</table></div>"
            '<h2>🕒 Recent Orders</h2><div class="card"><table><tr><th>Product</th><th>Price</th><th>User</th>'
            f"<th>Time</th></tr>{recent}</table></div>"
        )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="refresh" content="30">'
        f"{robots}<title>{e(BRAND)}</title><style>{PAGE_CSS}</style></head><body><div class=\"wrap\">"
        f'<div class="hero"><div class="logo">💎</div><h1>{e(BRAND)}</h1>'
        '<p class="sub">Instant digital delivery • Secure wallet • 24/7 Telegram store</p>'
        f'<div class="pill{"" if ok else " bad"}"><span class="dot"></span>{status} &nbsp;•&nbsp; Uptime {uptime_text()}</div>'
        f"<br>{btn}</div>"
        f'<div class="grid">{cards}</div>'
        '<div class="feat"><div>⚡ Payment ke baad turant delivery</div><div>🔒 Safe wallet system</div>'
        "<div>🎟 One-time redeem codes</div><div>🕒 Bot 24/7 online</div></div>"
        f"{private}"
        f'<p class="foot">Last updated {now} IST • auto-refresh 30s</p>'
        f"</div><script>{COUNT_JS}</script></body></html>"
    )


async def h_health(_request):
    return web.Response(text="ok")


async def h_home(_request):
    ok = True
    try:
        pub = cached("pub", 15, get_public_stats)
    except Exception as ex:
        logging.warning("dashboard db error: %s", ex)
        pub, ok = {"users": 0, "new_today": 0, "products": 0, "orders": 0}, False
    return web.Response(text=render_page(pub, None, ok), content_type="text/html", charset="utf-8")


async def h_admin(request):
    if not DASH_KEY:
        return web.Response(text="DASH_KEY set nahi hai.", status=404)
    if not secrets.compare_digest(request.query.get("key", "").encode(), DASH_KEY.encode()):
        return web.Response(text="Unauthorized", status=401)
    try:
        pub = cached("pub", 15, get_public_stats)
        priv = cached("priv", 15, get_private_stats)
        html_page = render_page(pub, priv, True)
    except Exception as ex:
        logging.warning("admin dashboard error: %s", ex)
        return web.Response(text="Database error", status=500)
    return web.Response(
        text=html_page, content_type="text/html", charset="utf-8", headers={"Cache-Control": "no-store"}
    )


async def start_web():
    port = os.getenv("PORT")
    if not port:
        return
    app = web.Application()
    app.router.add_get("/", h_home)
    app.router.add_get("/health", h_health)
    app.router.add_get("/api/health", h_health)  # Render ke automatic health check path ke liye fix
    app.router.add_get("/admin", h_admin)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    logging.info("Website chalu, port %s", port)


async def self_ping():
    url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("SELF_URL")
    if not url:
        return
    while True:  # Render free service 15 min me so jati hai, isliye har 10 min me ping
        await asyncio.sleep(600)
        try:
            async with ClientSession(timeout=ClientTimeout(total=20)) as s:
                await s.get(url.rstrip("/") + "/health")
        except Exception:
            pass


# ================= RUN =================
async def main():
    if _placeholder(BOT_TOKEN) or not ADMIN_ID:
        raise SystemExit("BOT_TOKEN ya ADMIN_ID set nahi hai. Render -> Environment me daalo.")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(BanMW())
    dp.callback_query.outer_middleware(BanMW())
    dp.include_router(cmd_r)
    dp.include_router(admin_r)
    dp.include_router(user_r)
    global BOT_USERNAME
    await start_web()  # Render ko turant port chahiye, isliye sabse pehle
    await bot.delete_webhook(drop_pending_updates=True)
    BOT_USERNAME = (await bot.get_me()).username or ""
    tasks = [
        asyncio.create_task(backup_log := backup_loop(bot)),
        asyncio.create_task(expiry_loop(bot)),
        asyncio.create_task(self_ping()),
    ]
    logging.info("Bot start ho gaya: @%s", BOT_USERNAME)
    while True:  # kuch bhi gadbad ho to 5 sec baad khud dobara chalu
        try:
            await dp.start_polling(bot)
            break
        except Exception as ex:
            logging.exception("polling band hui: %s", ex)
            await asyncio.sleep(5)
    for t in tasks:
        t.cancel()


if __name__ == "__main__":
    asyncio.run(main())
