import asyncio
import os
import re
import html
import logging
import time
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError
from sqlalchemy import String, Integer, Boolean, Float, select, delete
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pricebot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_DIR = os.getenv("SESSION_DIR", "./sessions")

def parse_chat_ref(value: str | None):
    value = (value or "").strip()
    if not value:
        return 0
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value

TARGET_CHAT_ID = parse_chat_ref(os.getenv("TARGET_CHAT_ID"))
SOURCE_CHAT_1 = parse_chat_ref(os.getenv("SOURCE_CHAT_1"))
SOURCE_CHAT_2 = parse_chat_ref(os.getenv("SOURCE_CHAT_2"))
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./pricebot.db").strip()

def normalize_database_url(url: str) -> str:
    """Normalize Railway/Postgres URLs for SQLAlchemy asyncpg."""
    url = (url or "").strip()
    if not url:
        return "sqlite+aiosqlite:///./pricebot.db"
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://"):]
    if url.startswith("postgresql+") and "://" in url and not url.startswith("postgresql+asyncpg://"):
        return "postgresql+asyncpg://" + url.split("://", 1)[1]
    return url

DATABASE_URL = normalize_database_url(DATABASE_URL)
DEFAULT_MARKUP = int(os.getenv("DEFAULT_MARKUP", "2000"))
os.makedirs(SESSION_DIR, exist_ok=True)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if ADMIN_ID <= 0:
    raise RuntimeError("ADMIN_ID is required and must be numeric")
if API_ID < 0:
    raise RuntimeError("API_ID must be numeric")

class Base(DeclarativeBase):
    pass

class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)  # supplier1/supplier2/own
    source_key: Mapped[str] = mapped_column(String(500), index=True)
    name: Mapped[str] = mapped_column(String(500))
    canonical: Mapped[str] = mapped_column(String(500), index=True)
    brand: Mapped[str] = mapped_column(String(120), index=True)
    category: Mapped[str] = mapped_column(String(120), index=True)
    price: Mapped[int] = mapped_column(Integer)
    region: Mapped[str] = mapped_column(String(32), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)

class CategoryOrder(Base):
    __tablename__ = "category_order"
    category: Mapped[str] = mapped_column(String(120), primary_key=True)
    pos: Mapped[int] = mapped_column(Integer, default=999)

class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(String(500))

DATABASE_URL = normalize_database_url(DATABASE_URL)
log.info("Database driver: %s", DATABASE_URL.split("://", 1)[0])
engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)

CATEGORY_EMOJI = {
    "speakers": "🔊",
    "headphones": "🎧",
    "cameras": "📷",
    "smartphones": "📱",
    "tablets": "💻",
    "watches": "⌚",
    "glasses": "🕶",
    "powerbanks": "🔋",
    "accessories": "🧩",
    "other": "📦",
}
CATEGORY_TITLE = {
    "speakers": "Колонки",
    "headphones": "Наушники",
    "cameras": "Камеры",
    "smartphones": "Смартфоны",
    "tablets": "Планшеты",
    "watches": "Часы и трекеры",
    "glasses": "Умные очки",
    "powerbanks": "Power Bank",
    "accessories": "Аксессуары",
    "other": "Другое",
}

BRANDS = [
    "Harman Kardon", "Bose", "Fujifilm", "Insta360", "Insta 360", "Ray-Ban Meta",
    "Ray-Ban", "Samsung", "Galaxy", "Xiaomi", "Redmi", "POCO", "Realme", "Vivo",
    "Garmin", "Coros", "Poly", "LG", "Dreame", "Google Fitbit", "Fitbit", "BEKO", "Netgear"
]

def clean_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()

def parse_price(raw: str) -> int:
    digits = re.sub(r"\D", "", raw)
    return int(digits) if digits else 0

def strip_flags(s: str) -> str:
    return re.sub(r"[\U0001F1E6-\U0001F1FF]{2}", "", s)

def detect_region(s: str) -> str:
    m = re.search(r"([\U0001F1E6-\U0001F1FF]{2})", s)
    return m.group(1) if m else ""

def detect_brand(name: str) -> str:
    low = name.lower()
    if "harman kardon" in low: return "Harman Kardon"
    if "ray-ban" in low or "wayfarer" in low: return "Ray-Ban Meta"
    if "fujifilm" in low or "instax" in low: return "Fujifilm"
    if "insta 360" in low or "insta360" in low: return "Insta360"
    if "galaxy" in low or "samsung" in low: return "Samsung"
    if "redmi" in low: return "Redmi"
    if "poco" in low: return "POCO"
    if "xiaomi" in low: return "Xiaomi"
    if "realme" in low: return "Realme"
    if "vivo" in low: return "Vivo"
    if "garmin" in low: return "Garmin"
    if "coros" in low: return "Coros"
    if "poly" in low: return "Poly"
    if re.search(r"\bbose\b", low): return "Bose"
    if "fitbit" in low: return "Google Fitbit"
    if "dreame" in low: return "Dreame"
    if "beko" in low: return "BEKO"
    if re.search(r"\blg\b", low): return "LG"
    if "netgear" in low: return "Netgear"
    words = clean_spaces(strip_flags(name)).split()
    return " ".join(words[:2]) if len(words) >= 2 else (words[0] if words else "Other")

def detect_category(name: str) -> str:
    low = name.lower()
    if any(x in low for x in ["aura studio", "onyx studio", "soundsticks", "soundlink", "speaker", "колон"]):
        return "speakers"
    if any(x in low for x in ["buds", "quietcomfort", "voyager free", "airpods", "headphone", "earbuds", "науш"]):
        return "headphones"
    if any(x in low for x in ["instax", "insta360", "insta 360", "camera", "cinebeam"]):
        return "cameras"
    if any(x in low for x in ["wayfarer", "ray-ban meta"]):
        return "glasses"
    if any(x in low for x in ["watch", "fit3", "fitbit", "garmin", "forerunner", "vivoactive", "coros pace", "ring"]):
        return "watches"
    if any(x in low for x in ["tab ", "pad ", "galaxy book"]):
        return "tablets"
    if any(x in low for x in ["power bank", "powerbank"]):
        return "powerbanks"
    if any(x in low for x in ["galaxy s", "galaxy z", "redmi ", "note ", "xiaomi 1", "realme ", "vivo ", "poco x"]):
        return "smartphones"
    return "other"

def canonicalize(name: str) -> str:
    s = strip_flags(name).lower()
    s = s.replace("–", "-").replace("—", "-")
    s = re.sub(r"\b(sm-[a-z0-9]+|np[a-z0-9-]+|p/n[:\\]*[a-z0-9]+|010-[0-9-]+)\b", " ", s)
    s = re.sub(r"[^a-zа-я0-9]+", " ", s)
    tokens = [t for t in s.split() if t not in {"slim"}]
    return " ".join(tokens)

@dataclass
class Parsed:
    name: str
    price: int
    region: str = ""

# Supplier 1: 📌**Name** -22.400 ; wholesale lines ignored
def parse_supplier1(text: str) -> list[Parsed]:
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("от ") or "*от " in line.lower():
            continue
        line = line.replace("**", "")
        m = re.match(r"^📌\s*(.+?)\s*-\s*([0-9][0-9 .]*)\s*(?:\([^)]*шт\))?\s*$", line, re.I)
        if not m:
            continue
        name = clean_spaces(m.group(1))
        price = parse_price(m.group(2))
        if name and price:
            out.append(Parsed(name=name, price=price, region=detect_region(name)))
    return out

# Supplier 2: **• Product🇪🇺** then 🇪🇺 От 1 шт - 69 500
def parse_supplier2(text: str) -> list[Parsed]:
    out = []
    pending = None
    for raw in text.splitlines():
        line = raw.strip().replace("**", "")
        if not line:
            continue
        if line.startswith("•"):
            pending = clean_spaces(line.lstrip("•").strip())
            continue
        if pending and re.search(r"От\s*1\s*шт\s*-", line, re.I):
            m = re.search(r"От\s*1\s*шт\s*-\s*([0-9][0-9 ]*)", line, re.I)
            if m:
                region = detect_region(pending) or detect_region(line)
                out.append(Parsed(name=clean_spaces(strip_flags(pending)), price=parse_price(m.group(1)), region=region))
            pending = None
    return out

async def get_setting(key: str, default: str) -> str:
    async with Session() as s:
        row = await s.get(Setting, key)
        return row.value if row else default

async def set_setting(key: str, value: str):
    async with Session() as s:
        row = await s.get(Setting, key)
        if row:
            row.value = value
        else:
            s.add(Setting(key=key, value=value))
        await s.commit()

async def upsert_parsed(source: str, items: list[Parsed]):
    async with Session() as s:
        for p in items:
            source_key = canonicalize(p.name) + "|" + p.region
            q = await s.scalar(select(Product).where(Product.source == source, Product.source_key == source_key))
            brand = detect_brand(p.name)
            category = detect_category(p.name)
            canonical = canonicalize(p.name)
            if q:
                q.name, q.price, q.region, q.brand, q.category, q.canonical = p.name, p.price, p.region, brand, category, canonical
            else:
                s.add(Product(source=source, source_key=source_key, name=p.name, canonical=canonical,
                              brand=brand, category=category, price=p.price, region=p.region, enabled=False))
        await s.commit()

async def effective_products():
    markup = int(await get_setting("default_markup", str(DEFAULT_MARKUP)))
    async with Session() as s:
        rows = (await s.scalars(select(Product).where(Product.enabled == True))).all()
    grouped: dict[str, list[Product]] = {}
    for p in rows:
        grouped.setdefault(p.canonical, []).append(p)
    result = []
    for canonical, candidates in grouped.items():
        own = [p for p in candidates if p.source == "own"]
        if own:
            p = min(own, key=lambda x: x.price)
            final_price = p.price
        else:
            p = min(candidates, key=lambda x: x.price)
            final_price = p.price + markup
        result.append((p, final_price))
    return result

async def category_positions(categories: set[str]):
    async with Session() as s:
        existing = {r.category: r.pos for r in (await s.scalars(select(CategoryOrder))).all()}
        max_pos = max(existing.values(), default=0)
        changed = False
        for c in categories:
            if c not in existing:
                max_pos += 10
                s.add(CategoryOrder(category=c, pos=max_pos))
                existing[c] = max_pos
                changed = True
        if changed:
            await s.commit()
    return existing

def display_name(p: Product) -> str:
    n = clean_spaces(strip_flags(p.name))
    # remove repeated brand from start for cleaner brand block
    brand = p.brand
    if n.lower().startswith(brand.lower()):
        n = n[len(brand):].strip(" -")
    if p.region:
        n = f"{n} {p.region}".strip()
    return n

async def render_catalog() -> list[tuple[str, str]]:
    rows = await effective_products()
    cats = {p.category for p, _ in rows}
    order = await category_positions(cats)
    by_cat: dict[str, dict[str, list[tuple[Product, int]]]] = {}
    for p, final_price in rows:
        by_cat.setdefault(p.category, {}).setdefault(p.brand, []).append((p, final_price))
    output = []
    for cat in sorted(by_cat, key=lambda c: order.get(c, 9999)):
        parts = [f"<b>{CATEGORY_EMOJI.get(cat,'📦')} {html.escape(CATEGORY_TITLE.get(cat, cat))}</b>", ""]
        for brand in sorted(by_cat[cat]):
            parts.append(f"<b>{html.escape(brand)}</b>")
            for p, price in sorted(by_cat[cat][brand], key=lambda x: display_name(x[0]).lower()):
                parts.append(f"{html.escape(display_name(p))} — <b>{price:,}</b>".replace(",", " "))
            parts.append("")
        output.append((cat, "\n".join(parts).strip()))
    return output


async def get_target_chat_id():
    raw = (await get_setting("target_chat_id", "")).strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            return raw
    return TARGET_CHAT_ID or 0

async def get_catalog_slots() -> list[int]:
    raw = await get_setting("catalog_slots", "[]")
    try:
        data = json.loads(raw)
        return [int(x) for x in data if str(x).lstrip("-").isdigit()]
    except Exception:
        return []

async def set_catalog_slots(ids: list[int]):
    await set_setting("catalog_slots", json.dumps(ids))

async def sync_catalog_to_target() -> tuple[bool, str]:
    target = await get_target_chat_id()
    if not target:
        return False, "Группа прайса ещё не привязана"

    blocks = await render_catalog()
    if not blocks:
        return False, "Нет включённых товаров для публикации"

    slots = await get_catalog_slots()
    new_slots: list[int] = []

    for idx, (_, text) in enumerate(blocks):
        msg_id = slots[idx] if idx < len(slots) else None
        if msg_id:
            try:
                await bot.edit_message_text(
                    chat_id=target,
                    message_id=msg_id,
                    text=text,
                    parse_mode="HTML",
                )
                new_slots.append(msg_id)
                continue
            except Exception as e:
                # "message is not modified" is not an error for us.
                if "message is not modified" in str(e).lower():
                    new_slots.append(msg_id)
                    continue
                log.warning("catalog slot %s cannot be edited: %s", msg_id, e)

        sent = await bot.send_message(target, text, parse_mode="HTML")
        new_slots.append(sent.message_id)

    # Delete only catalog messages created/managed by this bot. The fixed
    # warranty/delivery message is never stored here and is never touched.
    for stale_id in slots[len(blocks):]:
        try:
            await bot.delete_message(target, stale_id)
        except Exception as e:
            log.warning("cannot delete stale catalog slot %s: %s", stale_id, e)

    await set_catalog_slots(new_slots)
    return True, f"Прайс обновлён: {len(blocks)} сообщ."

async def maybe_sync_catalog():
    target = await get_target_chat_id()
    if not target:
        return
    try:
        await sync_catalog_to_target()
    except Exception:
        log.exception("automatic catalog sync failed")


class AccountManager:
    def __init__(self):
        self.clients: dict[int, TelegramClient] = {}
        self.listener_attached: set[int] = set()
        self.login: dict[int, dict] = {}
        self.last_request: dict[int, float] = {1: 0.0, 2: 0.0}

    def session_path(self, slot: int) -> str:
        return os.path.join(SESSION_DIR, f"account{slot}")

    def source_chat(self, slot: int):
        return SOURCE_CHAT_1 if slot == 1 else SOURCE_CHAT_2

    def source_name(self, slot: int) -> str:
        return f"supplier{slot}"

    def parser(self, slot: int):
        return parse_supplier1 if slot == 1 else parse_supplier2

    def get_client(self, slot: int) -> TelegramClient:
        client = self.clients.get(slot)
        if client is None:
            client = TelegramClient(self.session_path(slot), API_ID, API_HASH)
            self.clients[slot] = client
        return client

    async def is_authorized(self, slot: int) -> bool:
        if not API_ID or not API_HASH:
            return False
        client = self.get_client(slot)
        if not client.is_connected():
            await client.connect()
        return await client.is_user_authorized()

    async def attach_listener(self, slot: int):
        if slot in self.listener_attached:
            return
        client = self.get_client(slot)
        source_chat = self.source_chat(slot)
        source_name = self.source_name(slot)
        parser = self.parser(slot)
        if not source_chat:
            log.warning("%s authorized but SOURCE_CHAT_%s is not set", source_name, slot)
            return

        async def handler(event):
            text = event.raw_text or ""
            parsed = parser(text)
            if parsed:
                await upsert_parsed(source_name, parsed)
                log.info("%s parsed %d items", source_name, len(parsed))
                await maybe_sync_catalog()

        client.add_event_handler(handler, events.NewMessage(chats=source_chat))
        self.listener_attached.add(slot)
        log.info("%s listener attached to %s", source_name, source_chat)

    async def start_existing(self):
        if not API_ID or not API_HASH:
            log.warning("Telegram user accounts disabled: API_ID/API_HASH missing")
            return
        for slot in (1, 2):
            try:
                if await self.is_authorized(slot):
                    await self.attach_listener(slot)
                    me = await self.get_client(slot).get_me()
                    log.info("account %s authorized as %s", slot, getattr(me, "username", None) or getattr(me, "id", "?"))
            except Exception:
                log.exception("failed to start account %s", slot)

    async def begin_login(self, slot: int, phone: str):
        client = self.get_client(slot)
        if not client.is_connected():
            await client.connect()
        sent = await client.send_code_request(phone)
        self.login[slot] = {"phone": phone, "phone_code_hash": sent.phone_code_hash}

    async def submit_code(self, slot: int, code: str) -> str:
        data = self.login.get(slot)
        if not data:
            return "restart"
        client = self.get_client(slot)
        try:
            await client.sign_in(phone=data["phone"], code=code, phone_code_hash=data["phone_code_hash"])
        except SessionPasswordNeededError:
            return "2fa"
        await self.finish_login(slot)
        return "ok"

    async def submit_password(self, slot: int, password: str):
        client = self.get_client(slot)
        await client.sign_in(password=password)
        await self.finish_login(slot)

    async def finish_login(self, slot: int):
        self.login.pop(slot, None)
        await self.attach_listener(slot)

    async def account_label(self, slot: int) -> str:
        try:
            if not await self.is_authorized(slot):
                return "❌ не подключен"
            me = await self.get_client(slot).get_me()
            username = f"@{me.username}" if getattr(me, "username", None) else ""
            phone = getattr(me, "phone", None)
            tail = f"•••{phone[-4:]}" if phone else ""
            who = " ".join(x for x in (username, tail) if x)
            return f"✅ {who or me.id}"
        except Exception:
            return "⚠️ ошибка"

    async def request_supplier(self, slot: int) -> str:
        source_chat = self.source_chat(slot)
        if not source_chat:
            raise RuntimeError(f"SOURCE_CHAT_{slot} не задан")
        command = (await get_setting(f"supplier{slot}_command", "")).strip()
        if not command:
            raise RuntimeError(f"Команда для поставщика {slot} не настроена")
        if not await self.is_authorized(slot):
            raise RuntimeError(f"Аккаунт {slot} не подключён")
        client = self.get_client(slot)
        await self.attach_listener(slot)
        await client.send_message(source_chat, command)
        self.last_request[slot] = time.monotonic()
        log.info("supplier%s request sent to %s: %s", slot, source_chat, command)
        return command

    async def polling_loop(self, slot: int):
        while True:
            try:
                enabled = (await get_setting(f"supplier{slot}_poll_enabled", "0")) == "1"
                command = (await get_setting(f"supplier{slot}_command", "")).strip()
                interval_min = max(1, int(await get_setting(f"supplier{slot}_interval", "30")))
                if enabled and command and await self.is_authorized(slot):
                    elapsed = time.monotonic() - self.last_request.get(slot, 0.0)
                    if self.last_request.get(slot, 0.0) == 0.0 or elapsed >= interval_min * 60:
                        await self.request_supplier(slot)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("supplier%s polling error", slot)
            await asyncio.sleep(10)

    async def disconnect_and_forget(self, slot: int):
        client = self.clients.pop(slot, None)
        if client:
            try:
                if client.is_connected():
                    await client.log_out()
            except Exception:
                log.exception("logout failed for slot %s", slot)
            try:
                await client.disconnect()
            except Exception:
                pass
        self.listener_attached.discard(slot)
        self.login.pop(slot, None)
        base = self.session_path(slot)
        for suffix in (".session", ".session-journal"):
            try:
                os.remove(base + suffix)
            except FileNotFoundError:
                pass

accounts = AccountManager()

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

USER_STATE: dict[int, dict] = {}

def admin_only(msg_or_cb) -> bool:
    uid = msg_or_cb.from_user.id
    return uid == ADMIN_ID

def main_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="👤 Аккаунты", callback_data="accounts")
    kb.button(text="📦 Поставщик 1", callback_data="src:supplier1")
    kb.button(text="📦 Поставщик 2", callback_data="src:supplier2")
    kb.button(text="🏠 Наш товар", callback_data="own")
    kb.button(text="💰 Наценка", callback_data="markup")
    kb.button(text="⏱ Запросы поставщикам", callback_data="polls")
    kb.button(text="📢 Группа прайса", callback_data="target")
    kb.button(text="🔄 Обновить прайс", callback_data="publish")
    kb.button(text="↕️ Порядок блоков", callback_data="order")
    kb.button(text="👁 Предпросмотр", callback_data="preview")
    kb.adjust(1,2,2,2,2)
    return kb.as_markup()

@dp.message(CommandStart())
@dp.message(Command("menu"))
async def menu(m: Message):
    if not admin_only(m): return
    await m.answer("Управление прайсом", reply_markup=main_kb())

@dp.callback_query(F.data == "target")
async def target_menu(c: CallbackQuery):
    if not admin_only(c): return
    target = await get_target_chat_id()
    title = await get_setting("target_chat_title", "")
    slots = await get_catalog_slots()
    status = f"✅ {html.escape(title or str(target))}" if target else "❌ не привязана"
    kb = InlineKeyboardBuilder()
    kb.button(text="📨 Привязать по сообщению", callback_data="target:bind")
    if target:
        kb.button(text="🔄 Обновить сейчас", callback_data="publish")
        kb.button(text="🗑 Отвязать группу", callback_data="target:clear")
    kb.button(text="⬅️ Меню", callback_data="menu")
    kb.adjust(1)
    await c.message.edit_text(
        "<b>Группа продажного прайса</b>\n\n"
        f"Статус: {status}\n"
        f"Управляемых товарных сообщений: <b>{len(slots)}</b>\n\n"
        "Нажми <b>«📨 Привязать по сообщению»</b> и просто перешли сюда любое сообщение из нужной группы.\n\n"
        "Бот сам сохранит группу. Никаких ID, ссылок и /bindhere.\n\n"
        "Бот управляет только своими товарными сообщениями. "
        "Сообщение ❤️ ГАРАНТИЯ / 📦 ВЫДАЧА не трогается.",
        parse_mode="HTML", reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data == "target:bind")
async def target_bind_start(c: CallbackQuery):
    if not admin_only(c): return
    USER_STATE[c.from_user.id] = {"mode": "bind_target_forward"}
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data="target:bind_cancel")
    await c.message.edit_text(
        "<b>Привязка группы</b>\n\n"
        "Перешли мне <b>любое сообщение из нужной группы</b>.\n"
        "Я сам определю группу и сохраню её.",
        parse_mode="HTML", reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data == "target:bind_cancel")
async def target_bind_cancel(c: CallbackQuery):
    if not admin_only(c): return
    USER_STATE.pop(c.from_user.id, None)
    await target_menu(c)

@dp.message(F.forward_origin)
async def target_bind_forwarded(m: Message):
    if not admin_only(m): return
    state = USER_STATE.get(m.from_user.id, {})
    if state.get("mode") != "bind_target_forward":
        return

    origin = m.forward_origin
    source_chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
    if source_chat is None:
        source_chat = getattr(m, "forward_from_chat", None)

    if source_chat is None or str(getattr(source_chat, "type", "")) not in {"group", "supergroup", "channel", "ChatType.GROUP", "ChatType.SUPERGROUP", "ChatType.CHANNEL"}:
        await m.answer(
            "❌ Telegram не передал источник группы у этого пересланного сообщения. "
            "Попробуй переслать другое сообщение из этой группы."
        )
        return

    chat_id = int(source_chat.id)
    title = getattr(source_chat, "title", None) or str(chat_id)

    try:
        live_chat = await bot.get_chat(chat_id)
        title = getattr(live_chat, "title", None) or title
    except Exception:
        await m.answer(
            "❌ Группу определил, но управляющий бот не имеет к ней доступа. "
            "Сначала добавь этого бота в группу, потом перешли сообщение ещё раз."
        )
        return

    await set_setting("target_chat_id", str(chat_id))
    await set_setting("target_chat_title", title)
    await set_catalog_slots([])
    USER_STATE.pop(m.from_user.id, None)

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить прайс", callback_data="publish")
    kb.button(text="⬅️ Меню", callback_data="menu")
    kb.adjust(1)
    await m.answer(
        f"✅ Группа привязана: <b>{html.escape(title)}</b>\n\n"
        "Теперь прайс будет публиковаться и обновляться там.",
        parse_mode="HTML", reply_markup=kb.as_markup())

@dp.callback_query(F.data == "target:clear")
async def target_clear(c: CallbackQuery):
    if not admin_only(c): return
    await set_setting("target_chat_id", "")
    await set_setting("target_chat_title", "")
    await set_catalog_slots([])
    await c.answer("Группа отвязана", show_alert=True)
    await target_menu(c)

@dp.callback_query(F.data == "publish")
async def publish_now(c: CallbackQuery):
    if not admin_only(c): return
    try:
        ok, msg = await sync_catalog_to_target()
        await c.answer(msg[:190], show_alert=True)
    except Exception as e:
        log.exception("manual catalog publish failed")
        await c.answer(f"Ошибка публикации: {str(e)[:150]}", show_alert=True)

@dp.callback_query(F.data == "accounts")
async def accounts_menu(c: CallbackQuery):
    if not admin_only(c): return
    a1 = await accounts.account_label(1)
    a2 = await accounts.account_label(2)
    kb = InlineKeyboardBuilder()
    kb.button(text=f"1️⃣ {a1}", callback_data="account:1")
    kb.button(text=f"2️⃣ {a2}", callback_data="account:2")
    kb.button(text="⬅️ Меню", callback_data="menu")
    kb.adjust(1)
    await c.message.edit_text(
        "<b>Telegram-аккаунты поставщиков</b>\n\n"
        "Сессии сохраняются автоматически. После перезапуска Railway повторный вход не нужен.",
        parse_mode="HTML", reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data.startswith("account:"))
async def account_slot(c: CallbackQuery):
    if not admin_only(c): return
    slot = int(c.data.split(":")[1])
    label = await accounts.account_label(slot)
    authorized = label.startswith("✅")
    kb = InlineKeyboardBuilder()
    if authorized:
        kb.button(text="🔄 Переподключить", callback_data=f"account_login:{slot}")
        kb.button(text="🗑 Отключить аккаунт", callback_data=f"account_forget:{slot}")
    else:
        kb.button(text="➕ Подключить аккаунт", callback_data=f"account_login:{slot}")
    kb.button(text="⬅️ Аккаунты", callback_data="accounts")
    kb.adjust(1)
    source = accounts.source_chat(slot)
    await c.message.edit_text(
        f"<b>Аккаунт {slot}</b>\n\nСтатус: {html.escape(label)}\n"
        f"Источник: <code>{source or 'не задан'}</code>",
        parse_mode="HTML", reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data.startswith("account_login:"))
async def account_login(c: CallbackQuery):
    if not admin_only(c): return
    if not API_ID or not API_HASH:
        await c.answer("Сначала добавь API_ID и API_HASH в Railway Variables", show_alert=True)
        return
    slot = int(c.data.split(":")[1])
    if await accounts.is_authorized(slot):
        await accounts.disconnect_and_forget(slot)
    USER_STATE[c.from_user.id] = {"mode": "login_phone", "slot": slot}
    await c.message.edit_text(
        f"Подключаем аккаунт {slot}.\n\nПришли номер телефона в международном формате.\n"
        "Например: <code>+31612345678</code>", parse_mode="HTML")
    await c.answer()

@dp.callback_query(F.data.startswith("account_forget:"))
async def account_forget(c: CallbackQuery):
    if not admin_only(c): return
    slot = int(c.data.split(":")[1])
    await accounts.disconnect_and_forget(slot)
    USER_STATE.pop(c.from_user.id, None)
    await c.answer("Аккаунт отключён, сохранённая сессия удалена", show_alert=True)
    a1 = await accounts.account_label(1)
    a2 = await accounts.account_label(2)
    kb = InlineKeyboardBuilder()
    kb.button(text=f"1️⃣ {a1}", callback_data="account:1")
    kb.button(text=f"2️⃣ {a2}", callback_data="account:2")
    kb.button(text="⬅️ Меню", callback_data="menu")
    kb.adjust(1)
    await c.message.edit_text("<b>Telegram-аккаунты поставщиков</b>", parse_mode="HTML", reply_markup=kb.as_markup())

@dp.callback_query(F.data == "polls")
async def polls_menu(c: CallbackQuery):
    if not admin_only(c): return
    slot = 1
    kb = InlineKeyboardBuilder()
    enabled = (await get_setting("supplier1_poll_enabled", "0")) == "1"
    interval = await get_setting("supplier1_interval", "30")
    command = (await get_setting("supplier1_command", "")).strip()
    state = "🟢" if enabled else "⚪️"
    cmd = command if command else "команда не задана"
    kb.button(text=f"{state} Поставщик 1 · {interval} мин · {cmd[:18]}", callback_data="poll:1")
    kb.button(text="⬅️ Меню", callback_data="menu")
    kb.adjust(1)
    await c.message.edit_text(
        "<b>Автозапрос прайса</b>\n\n"
        "Команды и интервалы используются только для <b>Поставщика 1</b>.\n"
        "Поставщик 2 просто читается аккаунтом, когда появляется новый прайс.",
        parse_mode="HTML", reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data.startswith("poll:"))
async def poll_slot(c: CallbackQuery):
    if not admin_only(c): return
    slot = int(c.data.split(":")[1])
    if slot != 1:
        await c.answer("У второго поставщика нет автозапроса", show_alert=True)
        return
    enabled = (await get_setting(f"supplier{slot}_poll_enabled", "0")) == "1"
    interval = await get_setting(f"supplier{slot}_interval", "30")
    command = (await get_setting(f"supplier{slot}_command", "")).strip() or "не задана"
    source = accounts.source_chat(slot) or "не задан"
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Команда", callback_data=f"poll_cmd:{slot}")
    kb.button(text="⏱ Интервал", callback_data=f"poll_int:{slot}")
    kb.button(text="🔄 Запросить сейчас", callback_data=f"poll_now:{slot}")
    kb.button(text=("⏸ Остановить" if enabled else "▶️ Запустить"), callback_data=f"poll_toggle:{slot}")
    kb.button(text="⬅️ Запросы", callback_data="polls")
    kb.adjust(2,1,1,1)
    await c.message.edit_text(
        f"<b>Поставщик {slot}</b>\n\nИсточник: <code>{html.escape(str(source))}</code>\n"
        f"Команда: <code>{html.escape(command)}</code>\nИнтервал: <b>{interval} мин</b>\n"
        f"Автозапрос: <b>{'включён' if enabled else 'выключен'}</b>",
        parse_mode="HTML", reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data.startswith("poll_cmd:"))
async def poll_command(c: CallbackQuery):
    if not admin_only(c): return
    slot = int(c.data.split(":")[1])
    USER_STATE[c.from_user.id] = {"mode":"set_poll_command", "slot":slot}
    await c.message.edit_text(
        f"Пришли команду, которую аккаунт {slot} должен отправлять боту поставщика.\n\n"
        "Например: <code>/price</code> или <code>Актуальный прайс</code>", parse_mode="HTML")
    await c.answer()

@dp.callback_query(F.data.startswith("poll_int:"))
async def poll_interval(c: CallbackQuery):
    if not admin_only(c): return
    slot = int(c.data.split(":")[1])
    USER_STATE[c.from_user.id] = {"mode":"set_poll_interval", "slot":slot}
    await c.message.edit_text(
        f"Пришли интервал для поставщика {slot} в минутах.\nНапример: <code>5</code>, <code>15</code>, <code>30</code>, <code>60</code>.",
        parse_mode="HTML")
    await c.answer()

@dp.callback_query(F.data.startswith("poll_toggle:"))
async def poll_toggle(c: CallbackQuery):
    if not admin_only(c): return
    slot = int(c.data.split(":")[1])
    command = (await get_setting(f"supplier{slot}_command", "")).strip()
    if not command:
        await c.answer("Сначала задай команду", show_alert=True)
        return
    enabled = (await get_setting(f"supplier{slot}_poll_enabled", "0")) == "1"
    await set_setting(f"supplier{slot}_poll_enabled", "0" if enabled else "1")
    if not enabled:
        accounts.last_request[slot] = 0.0
    await c.answer("Автозапрос остановлен" if enabled else "Автозапрос запущен", show_alert=True)
    await poll_slot(c)

@dp.callback_query(F.data.startswith("poll_now:"))
async def poll_now(c: CallbackQuery):
    if not admin_only(c): return
    slot = int(c.data.split(":")[1])
    try:
        command = await accounts.request_supplier(slot)
        await c.answer(f"Отправлено: {command}", show_alert=True)
    except Exception as e:
        await c.answer(str(e)[:180], show_alert=True)

PRODUCTS_PER_PAGE = 10

async def show_source_products(message: Message, source: str, page: int = 0):
    async with Session() as s:
        rows = list((await s.scalars(
            select(Product).where(Product.source == source).order_by(Product.brand, Product.name)
        )).all())

    total = len(rows)
    pages = max(1, (total + PRODUCTS_PER_PAGE - 1) // PRODUCTS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    start = page * PRODUCTS_PER_PAGE
    chunk = rows[start:start + PRODUCTS_PER_PAGE]

    kb = InlineKeyboardBuilder()
    for p in chunk:
        mark = "✅" if p.enabled else "▫️"
        price = f"{p.price:,}".replace(",", " ")
        label = f"{mark} {p.name[:39]} · {price}"
        kb.button(text=label, callback_data=f"src_toggle:{p.id}:{source}:{page}")

    if page > 0:
        kb.button(text="⬅️", callback_data=f"src_page:{source}:{page-1}")
    kb.button(text=f"{page+1}/{pages}", callback_data="noop")
    if page + 1 < pages:
        kb.button(text="➡️", callback_data=f"src_page:{source}:{page+1}")
    kb.button(text="⬅️ Меню", callback_data="menu")
    kb.adjust(*([1] * len(chunk)), 3, 1)

    title = "Поставщик 1" if source == "supplier1" else "Поставщик 2"
    text = (
        f"<b>{title}</b>\n\n"
        f"Выбрано товаров: <b>{sum(1 for p in rows if p.enabled)}</b> из <b>{total}</b>.\n"
        "Нажимай прямо на товар: ✅ — идёт в наш прайс, ▫️ — не используется."
    )
    await message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("src:"))
async def source_menu(c: CallbackQuery):
    if not admin_only(c): return
    source = c.data.split(":",1)[1]
    USER_STATE.pop(c.from_user.id, None)
    await show_source_products(c.message, source, 0)
    await c.answer()

@dp.callback_query(F.data.startswith("src_page:"))
async def source_page(c: CallbackQuery):
    if not admin_only(c): return
    _, source, page = c.data.split(":")
    await show_source_products(c.message, source, int(page))
    await c.answer()

@dp.callback_query(F.data.startswith("src_toggle:"))
async def source_toggle(c: CallbackQuery):
    if not admin_only(c): return
    _, pid, source, page = c.data.split(":")
    async with Session() as s:
        p = await s.get(Product, int(pid))
        if not p or p.source != source:
            await c.answer("Товар не найден", show_alert=True)
            return
        p.enabled = not p.enabled
        enabled = p.enabled
        await s.commit()
    await show_source_products(c.message, source, int(page))
    await c.answer("Добавлен в наш прайс" if enabled else "Убран из нашего прайса")
    await maybe_sync_catalog()

@dp.callback_query(F.data == "noop")
async def noop(c: CallbackQuery):
    await c.answer()

@dp.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery):
    if not admin_only(c): return
    await c.message.edit_text("Управление прайсом", reply_markup=main_kb())
    await c.answer()

@dp.message(F.text)
async def text_router(m: Message):
    if not admin_only(m): return
    state = USER_STATE.get(m.from_user.id, {})
    if state.get("mode") == "bind_target_forward":
        await m.answer("Перешли именно сообщение из нужной группы — вручную ничего вводить не надо.")
        return
    if state.get("mode") == "login_phone":
        slot = state["slot"]
        phone = clean_spaces(m.text)
        try:
            await accounts.begin_login(slot, phone)
            USER_STATE[m.from_user.id] = {"mode": "login_code", "slot": slot}
            await m.answer(
                "Код отправлен Telegram. Пришли его сюда цифрами.\n\n"
                "После успешного входа сообщение с кодом можешь удалить.")
        except Exception as e:
            log.exception("send code failed")
            await m.answer(f"Не получилось отправить код: <code>{html.escape(str(e))}</code>", parse_mode="HTML")
        return
    if state.get("mode") == "login_code":
        slot = state["slot"]
        code = re.sub(r"\D", "", m.text)
        try:
            result = await accounts.submit_code(slot, code)
            if result == "2fa":
                USER_STATE[m.from_user.id] = {"mode": "login_2fa", "slot": slot}
                await m.answer("На аккаунте включена двухэтапная защита. Пришли пароль 2FA.")
            elif result == "ok":
                USER_STATE.pop(m.from_user.id, None)
                label = await accounts.account_label(slot)
                await m.answer(f"Аккаунт {slot} подключён: {label}", reply_markup=main_kb())
            else:
                await m.answer("Авторизация устарела. Начни подключение аккаунта заново.")
        except PhoneCodeInvalidError:
            await m.answer("Код неверный. Пришли код ещё раз.")
        except PhoneCodeExpiredError:
            USER_STATE.pop(m.from_user.id, None)
            await m.answer("Код уже истёк. Нажми «Аккаунты» и начни подключение заново.", reply_markup=main_kb())
        except Exception as e:
            log.exception("sign in failed")
            await m.answer(f"Ошибка входа: <code>{html.escape(str(e))}</code>", parse_mode="HTML")
        return
    if state.get("mode") == "login_2fa":
        slot = state["slot"]
        try:
            await accounts.submit_password(slot, m.text)
            USER_STATE.pop(m.from_user.id, None)
            label = await accounts.account_label(slot)
            await m.answer(f"Аккаунт {slot} подключён: {label}", reply_markup=main_kb())
        except Exception as e:
            log.exception("2fa failed")
            await m.answer(f"Не подошёл пароль 2FA: <code>{html.escape(str(e))}</code>", parse_mode="HTML")
        return
    if state.get("mode") == "set_poll_command":
        slot = state["slot"]
        command = m.text.strip()
        if not command:
            await m.answer("Команда не может быть пустой.")
            return
        await set_setting(f"supplier{slot}_command", command)
        USER_STATE.pop(m.from_user.id, None)
        await m.answer(f"Команда поставщика {slot} сохранена: <code>{html.escape(command)}</code>", parse_mode="HTML", reply_markup=main_kb())
        return
    if state.get("mode") == "set_poll_interval":
        slot = state["slot"]
        try:
            interval = int(re.sub(r"\D", "", m.text))
            if interval < 1 or interval > 1440:
                raise ValueError
            await set_setting(f"supplier{slot}_interval", str(interval))
            USER_STATE.pop(m.from_user.id, None)
            accounts.last_request[slot] = 0.0
            await m.answer(f"Интервал поставщика {slot}: {interval} мин.", reply_markup=main_kb())
        except Exception:
            await m.answer("Пришли число от 1 до 1440 минут. Например: 30")
        return
    if state.get("mode") == "set_markup":
        try:
            v = parse_price(m.text)
            if v <= 0: raise ValueError
            await set_setting("default_markup", str(v))
            USER_STATE.pop(m.from_user.id, None)
            await m.answer(f"Наценка установлена: +{v:,}".replace(","," "), reply_markup=main_kb())
            await maybe_sync_catalog()
        except Exception:
            await m.answer("Пришли число, например 2500")
        return
    if state.get("mode") == "add_own_item":
        raw = clean_spaces(m.text or "")

        # Parse the final price without regex escapes.
        # Supported: 10700, 10 700, 10.700, 10700 ₽, 10 700 руб.
        cleaned = raw.strip()
        lowered = cleaned.lower()
        for suffix in ("руб.", "руб", "₽", "р.", "р"):
            if lowered.endswith(suffix):
                cleaned = cleaned[:-len(suffix)].rstrip()
                break

        parts = cleaned.split()
        price = 0
        name = ""

        if parts:
            last = parts[-1]
            # Plain price: 10700 or 10.700
            digits = last.replace(".", "").replace(",", "")
            if digits.isdigit() and len(digits) >= 4:
                price = int(digits)
                name = " ".join(parts[:-1]).strip(" -—–:|")
            # Spaced thousands: ... 10 700
            elif len(parts) >= 2 and parts[-2].isdigit() and last.isdigit() and len(last) == 3:
                combined = parts[-2] + last
                if combined.isdigit():
                    price = int(combined)
                    name = " ".join(parts[:-2]).strip(" -—–:|")

        if not name or price <= 0:
            await m.answer(
                "Не увидел цену в конце сообщения.\n"
                "Пример: Google Fitbit Air Lavender 10700"
            )
            return
        if not name or price <= 0:
            await m.answer(
                "Не понял товар или цену.\n"
                "Пример: iPhone 17 Pro 256 Black 94500"
            )
            return

        canonical = canonicalize(name)
        async with Session() as s:
            existing = await s.scalar(
                select(Product).where(
                    Product.source == "own",
                    Product.canonical == canonical
                )
            )
            if existing:
                existing.name = name
                existing.price = price
                existing.enabled = True
                existing.brand = detect_brand(name)
                existing.category = detect_category(name)
                existing.region = detect_region(name)
            else:
                s.add(Product(
                    source="own",
                    source_key=canonical,
                    name=name,
                    canonical=canonical,
                    brand=detect_brand(name),
                    category=detect_category(name),
                    price=price,
                    region=detect_region(name),
                    enabled=True
                ))
            await s.commit()

        USER_STATE.pop(m.from_user.id, None)
        await m.answer(
            f"✅ Добавлено: {html.escape(name)} — {price:,} ₽".replace(",", " "),
            reply_markup=main_kb(),
            parse_mode="HTML"
        )
        await maybe_sync_catalog()
        return

@dp.callback_query(F.data.startswith("toggle:"))
async def toggle(c: CallbackQuery):
    if not admin_only(c): return
    pid = int(c.data.split(":")[1])
    async with Session() as s:
        p = await s.get(Product, pid)
        if p:
            p.enabled = not p.enabled
            await s.commit()
            await c.answer("Включено" if p.enabled else "Выключено")
        else:
            await c.answer("Не найдено", show_alert=True)

@dp.callback_query(F.data == "markup")
async def markup(c: CallbackQuery):
    if not admin_only(c): return
    current = await get_setting("default_markup", str(DEFAULT_MARKUP))
    USER_STATE[c.from_user.id] = {"mode":"set_markup"}
    await c.message.edit_text(f"Текущая общая наценка: +{int(current):,}\n\nПришли новую сумму.".replace(","," "))
    await c.answer()

@dp.callback_query(F.data == "own")
async def own(c: CallbackQuery):
    if not admin_only(c): return
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить / обновить", callback_data="own:add")
    kb.button(text="🗑 Удалить наш товар", callback_data="own:delete")
    kb.button(text="⬅️ Меню", callback_data="menu")
    kb.adjust(1)
    await c.message.edit_text("Наш товар имеет приоритет над поставщиками.", reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data == "own:add")
async def own_add(c: CallbackQuery):
    USER_STATE[c.from_user.id] = {"mode":"add_own_item"}
    await c.message.edit_text(
        "Пришли товар и нашу продажную цену одним сообщением.\n\n"
        "Например:\n"
        "Google Fitbit Air Lavender 10700\n"
        "iPhone 17 Pro 256 Black 94500"
    )
    await c.answer()

@dp.callback_query(F.data == "own:delete")
async def own_delete(c: CallbackQuery):
    async with Session() as s:
        rows = (await s.scalars(select(Product).where(Product.source == "own"))).all()
    kb = InlineKeyboardBuilder()
    for p in rows[:50]:
        kb.button(text=f"🗑 {p.name[:50]} — {p.price}", callback_data=f"own_del:{p.id}")
    kb.button(text="⬅️ Меню", callback_data="menu")
    kb.adjust(1)
    await c.message.edit_text("Выбери наш товар, который закончился:", reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data.startswith("own_del:"))
async def own_del(c: CallbackQuery):
    pid = int(c.data.split(":")[1])
    async with Session() as s:
        p = await s.get(Product, pid)
        if p and p.source == "own":
            await s.delete(p)
            await s.commit()
            await c.answer("Удалено. Если есть поставщик — он снова станет активным.", show_alert=True)
            await maybe_sync_catalog()
        else:
            await c.answer("Не найдено", show_alert=True)

@dp.callback_query(F.data == "preview")
async def preview(c: CallbackQuery):
    blocks = await render_catalog()
    if not blocks:
        await c.answer("Пока нет включённых товаров", show_alert=True)
        return
    await c.answer()
    for _, text in blocks:
        await c.message.answer(text, parse_mode="HTML")

@dp.callback_query(F.data == "order")
async def order(c: CallbackQuery):
    blocks = await render_catalog()
    cats = [cat for cat, _ in blocks]
    kb = InlineKeyboardBuilder()
    for cat in cats:
        title = CATEGORY_TITLE.get(cat, cat)
        kb.row(
            InlineKeyboardButton(text=f"⬆️ {title}", callback_data=f"ordup:{cat}"),
            InlineKeyboardButton(text=f"⬇️ {title}", callback_data=f"orddn:{cat}")
        )
    kb.row(InlineKeyboardButton(text="⬅️ Меню", callback_data="menu"))
    await c.message.edit_text("Порядок сообщений в каталоге:", reply_markup=kb.as_markup())
    await c.answer()

async def move_category(cat: str, direction: int):
    async with Session() as s:
        rows = list((await s.scalars(select(CategoryOrder).order_by(CategoryOrder.pos))).all())
        idx = next((i for i,r in enumerate(rows) if r.category == cat), None)
        if idx is None: return
        j = idx + direction
        if j < 0 or j >= len(rows): return
        rows[idx].pos, rows[j].pos = rows[j].pos, rows[idx].pos
        await s.commit()

@dp.callback_query(F.data.startswith("ordup:"))
async def ordup(c: CallbackQuery):
    await move_category(c.data.split(":",1)[1], -1)
    await order(c)
    await maybe_sync_catalog()

@dp.callback_query(F.data.startswith("orddn:"))
async def orddn(c: CallbackQuery):
    await move_category(c.data.split(":",1)[1], 1)
    await order(c)
    await maybe_sync_catalog()

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await set_setting("default_markup", await get_setting("default_markup", str(DEFAULT_MARKUP)))
    slot = 1
    await set_setting("supplier1_interval", await get_setting("supplier1_interval", "30"))
    await set_setting("supplier1_command", await get_setting("supplier1_command", ""))
    await set_setting("supplier1_poll_enabled", await get_setting("supplier1_poll_enabled", "0"))
    if TARGET_CHAT_ID and not (await get_setting("target_chat_id", "")).strip():
        await set_setting("target_chat_id", str(TARGET_CHAT_ID))

async def main():
    await init_db()
    await accounts.start_existing()
    poll_tasks = [asyncio.create_task(accounts.polling_loop(1))]
    try:
        await dp.start_polling(bot)
    finally:
        for task in poll_tasks:
            task.cancel()
        await asyncio.gather(*poll_tasks, return_exceptions=True)

if __name__ == "__main__":
    asyncio.run(main())
