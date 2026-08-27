import asyncio
import os
import re
import html
import logging
import time
import json
import hashlib
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError
from sqlalchemy import String, Integer, Boolean, Float, select, delete, text as sql_text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pricebot")
BUILD_VERSION = "v53"
PRICE_TZ = ZoneInfo(os.getenv("PRICE_TIMEZONE", "Europe/Moscow"))
AUTO_CLOSE_HOUR = int(os.getenv("AUTO_CLOSE_HOUR", "19"))
SUPPLIER1_QUIET_START = int(os.getenv("SUPPLIER1_QUIET_START", "20"))
SUPPLIER1_QUIET_END = int(os.getenv("SUPPLIER1_QUIET_END", "10"))

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_DIR = os.getenv("SESSION_DIR", "./sessions")

def parse_chat_ref(value: str | None):
    value = (value or "").strip()
    if not value:
        return 0

    # Numeric Telegram peer id.
    if re.fullmatch(r"-?\d+", value):
        return int(value)

    # Private Telegram message/chat link:
    # https://t.me/c/1860042299/123  ->  -1001860042299
    # https://t.me/c/1860042299/     ->  -1001860042299
    m = re.search(r"(?:https?://)?t\.me/c/(\d+)(?:/\d+)?/?$", value, re.I)
    if m:
        return int("-100" + m.group(1))

    # Public t.me links can be resolved by username.
    m = re.search(r"(?:https?://)?t\.me/([A-Za-z0-9_]+)(?:/\d+)?/?$", value, re.I)
    if m:
        username = m.group(1)
        if username.lower() not in {"c", "joinchat"}:
            return "@" + username

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

# Held for the whole process lifetime on PostgreSQL.
# Prevents two Railway containers from using the same Telethon sessions at once.
INSTANCE_LOCK_CONN = None
INSTANCE_LOCK_KEY = 740315927

CATEGORY_EMOJI = {
    "speakers": "🔊",
    "headphones": "🎧",
    "action_cameras": "🎥",
    "cameras": "📷",
    
    "smartphones": "📱",
    "tablets": "💻",
    "laptops": "💻",
    "watches": "⌚",
    "glasses": "🕶",
    "powerbanks": "🔋",
    "vacuum_cleaners": "🧹",
    "lego": "🧱",
    "projectors": "📽",
    "network": "🌐",
    "climate": "❄️",
    "smart_home": "🏠",
    "accessories": "🧩",
    "gaming": "🎮",
    "beauty": "💨",
}
CATEGORY_TITLE = {
    "speakers": "Колонки",
    "headphones": "Наушники",
    "action_cameras": "Экшн-камеры и стабилизаторы",
    "cameras": "Камеры",
    "smartphones": "Смартфоны",
    "tablets": "Планшеты",
    "laptops": "Ноутбуки",
    "watches": "Часы и трекеры",
    "glasses": "Умные очки",
    "powerbanks": "Power Bank",
    "vacuum_cleaners": "Пылесосы",
    "lego": "LEGO",
    "projectors": "Проекторы",
    "network": "Сетевое оборудование",
    "climate": "Климатическая техника",
    "smart_home": "Умный дом",
    "accessories": "Аксессуары",
    "gaming": "Игровые устройства",
    "beauty": "Фены и стайлеры",
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
    if re.search(r"\bkodak\b", low): return "Kodak"
    if re.search(r"\bmiele\b", low): return "Miele"
    if re.search(r"\bdyson\b", low): return "Dyson"
    if re.search(r"\bdji\b", low): return "DJI"
    if re.search(r"\blego\b", low): return "LEGO"
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
    low = clean_spaces(name).lower()

    # Dedicated blocks first — most specific rules win.
    if "lego" in low:
        return "lego"

    # Vacuum cleaners / floor care.
    if any(x in low for x in [
        "miele l1", "miele hx", "miele cx", "miele triflex",
        "dyson v8", "dyson v10", "dyson v11", "dyson v12", "dyson v15",
        "gen5detect", "vacuum", "пылесос", "robot vacuum", "roborock"
    ]):
        return "vacuum_cleaners"

    # Hair care. Keep Dyson hair devices out of vacuum block.
    if any(x in low for x in [
        "airwrap", "airstrait", "supersonic", "dyson hs", "dyson hd",
        "фен", "стайлер"
    ]):
        return "beauty"

    # Action cameras / compact creator cameras.
    if any(x in low for x in [
        "insta360", "insta 360", "gopro", "osmo action", "dji action",
        "action camera", "экшн"
    ]):
        return "action_cameras"

    # Gimbals/stabilizers. DJI Osmo Mobile belongs here, not cameras.
    if any(x in low for x in [
        "osmo mobile", "dji om ", "gimbal", "стабилизатор"
    ]):
        return "action_cameras"

    # Regular / instant cameras.
    if any(x in low for x in [
        "instax", "fujifilm", "camera", "фотоаппарат"
    ]):
        return "cameras"

    if any(x in low for x in [
        "aura studio", "onyx studio", "soundsticks", "soundlink",
        "speaker", "колон"
    ]):
        return "speakers"

    if any(x in low for x in [
        "buds", "quietcomfort", "voyager free", "airpods",
        "headphone", "earbuds", "науш"
    ]):
        return "headphones"

    if any(x in low for x in ["wayfarer", "skyler", "headliner", "ray-ban meta"]):
        return "glasses"

    if any(x in low for x in [
        "watch", "fit3", "fitbit", "garmin", "forerunner",
        "vivoactive", "coros pace", "galaxy ring"
    ]):
        return "watches"

    if any(x in low for x in ["galaxy book", "macbook", "notebook", "ноутбук"]):
        return "laptops"

    if any(x in low for x in ["tab ", "pad ", "tablet", "планшет"]):
        return "tablets"

    if any(x in low for x in ["power bank", "powerbank", "powerbank", "пауэрбанк"]):
        return "powerbanks"

    if any(x in low for x in ["cinebeam", "projector", "проектор"]):
        return "projectors"

    if any(x in low for x in [
        "netgear", "router", "wi-fi", "wifi", "switch ", "ethernet",
        "prosafe", "роутер", "маршрутизатор"
    ]):
        return "network"

    if any(x in low for x in [
        "сплит система", "air conditioner", "conditioner", "кондиционер",
        "очиститель воздуха", "air purifier", "humidifier", "увлажнитель"
    ]):
        return "climate"

    if any(x in low for x in ["smarttag", "smart tag", "airtag", "умный дом"]):
        return "smart_home"

    if any(x in low for x in [
        "playstation", "xbox", "nintendo", "steam deck", "rog ally",
        "meta quest", "gaming"
    ]):
        return "gaming"

    if any(x in low for x in [
        "galaxy a", "galaxy s", "galaxy z", "redmi ", "note ", "xiaomi 1",
        "realme ", "vivo ", "poco x", "iphone", "pixel "
    ]):
        return "smartphones"

    if any(x in low for x in [
        "case", "cover", "charger", "adapter", "cable", "кабель",
        "чехол", "заряд"
    ]):
        return "accessories"

    # No generic "Другое": leave unknown items unpublished until we add a proper rule.
    return "unclassified"

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
    """
    Supplier 1 real format:

      • Coros Pace 4 Black Nylon 🇨🇳
      🇨🇳 От 1 шт - 20 700

    The product name and retail/base price are on separate lines.
    Only the "От 1 шт" price is used. Wholesale tiers are ignored.
    Also keeps compatibility with one-line "Product - 22 400" entries.
    """
    out: list[Parsed] = []
    pending_name: str | None = None

    for raw in text.splitlines():
        line = clean_spaces(
            raw.replace("**", "")
               .replace("__", "")
               .replace("`", "")
               .replace("\u00a0", " ")
               .strip()
        )
        if not line:
            continue

        # Price line belonging to the previous product.
        price_line = re.search(
            r"\bот\s*1\s*шт\.?\s*[-–—:]\s*"
            r"([0-9]{1,3}(?:[ .][0-9]{3})+|[0-9]{4,9})\b",
            line,
            re.I,
        )
        if price_line and pending_name:
            price = parse_price(price_line.group(1))
            if price:
                out.append(
                    Parsed(
                        name=pending_name,
                        price=price,
                        region=detect_region(pending_name),
                    )
                )
            pending_name = None
            continue

        # Ignore any wholesale tiers / unrelated quantity-price lines.
        if re.search(r"\bот\s+\d+\s*шт", line, re.I):
            continue

        # Compatibility: product and price on one line.
        one_line = re.search(
            r"\s*[-–—]\s*"
            r"([0-9]{1,3}(?:[ .][0-9]{3})+|[0-9]{4,9})"
            r"(?:\s*\([^)]*шт[^)]*\))?\s*$",
            line,
            re.I,
        )
        if one_line:
            name = line[:one_line.start()].lstrip("📌📍•- ").strip(" -–—:")
            price = parse_price(one_line.group(1))
            if name and price:
                out.append(
                    Parsed(
                        name=clean_spaces(name),
                        price=price,
                        region=detect_region(name),
                    )
                )
            pending_name = None
            continue

        # Product-name line. Supplier 1 normally prefixes it with a bullet.
        if line.startswith(("•", "📌", "📍")):
            name = line.lstrip("📌📍•- ").strip()
            if name:
                pending_name = clean_spaces(name)
            continue

        # Some responses can omit the bullet; accept obvious product text,
        # but never treat headings/status lines as products.
        low = line.lower()
        if not any(x in low for x in [
            "актуальный прайс", "прайс", "наличие", "цены",
            "доставка", "заказы", "работаем", "закрыты"
        ]):
            pending_name = clean_spaces(line.lstrip("- ").strip())

    return out

# Supplier 2: **• Product🇪🇺** then 🇪🇺 От 1 шт - 69 500
def parse_supplier2(text: str) -> list[Parsed]:
    """Parse supplier 2 lines such as:
    📌601/7I50 Ray Ban Meta Wayfarer ... UPC: 8056... -31.000
    от 5 шт 30.800
    от 10 шт 30.300

    We use the base/single-item price on the product line and ignore wholesale tiers.
    """
    out = []
    for raw in text.splitlines():
        line = clean_spaces(raw.replace("**", "").replace("\u00a0", " ").strip())
        if not line:
            continue

        # Wholesale tier continuation lines are not separate products.
        if re.match(r"^от\s+\d+\s*шт", line, re.I):
            continue

        # Product rows in this supplier begin with a pin and end in a price.
        if not line.startswith(("📌", "📍")):
            continue

        body = line.lstrip("📌📍 ").strip()

        # Final price can be 31.000 / 31 000 / 31000, optionally "(4шт)" after it.
        m = re.search(
            r"\s+-\s*([0-9]{1,3}(?:[ .][0-9]{3})+|[0-9]{4,9})"
            r"(?:\s*\([^)]*шт[^)]*\))?\s*$",
            body,
            re.I,
        )
        if not m:
            continue

        price = parse_price(m.group(1))
        name = body[:m.start()].strip(" -—–")
        if not name or not price:
            continue

        # Preserve all model/variant identifiers exactly as supplied.
        # Codes such as "601/7I50", "RW4012" and the trailing identifier
        # are part of the product identity and must not be stripped.
        region = detect_region(name)
        out.append(
            Parsed(
                name=clean_spaces(strip_flags(name)),
                price=price,
                region=region,
            )
        )
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

def now_local() -> datetime:
    return datetime.now(PRICE_TZ)


def iso_now_local() -> str:
    return now_local().isoformat()


def supplier1_quiet_hours() -> bool:
    """No automatic /prices requests from 20:00 until 10:00 Moscow time."""
    hour = now_local().hour
    start = SUPPLIER1_QUIET_START
    end = SUPPLIER1_QUIET_END

    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def supplier_closed_text(text: str) -> bool:
    low = clean_spaces(text).lower()
    closed_markers = [
        "мы закрыты",
        "сейчас закрыты",
        "продажи закрыты",
        "поставщик закрыт",
        "дождитесь старта продаж",
        "в данный момент мы закрыты",
        "приём заказов закрыт",
        "прием заказов закрыт",
    ]
    return any(x in low for x in closed_markers)


async def mark_supplier_open(source: str):
    await set_setting(f"{source}_status", "open")
    await set_setting(f"{source}_last_open_at", iso_now_local())


async def mark_supplier_closed(source: str):
    await set_setting(f"{source}_status", "closed")
    await set_setting(f"{source}_last_closed_at", iso_now_local())


async def suppliers_status() -> tuple[str, str]:
    s1 = await get_setting("supplier1_status", "unknown")
    s2 = await get_setting("supplier2_status", "unknown")
    return s1, s2


async def get_catalog_closed_at() -> datetime | None:
    raw = (await get_setting("catalog_closed_at", "")).strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        return None


async def set_catalog_closed_now(reason: str):
    await set_setting("catalog_closed_at", iso_now_local())
    await set_setting("catalog_closed_reason", reason)


async def clear_catalog_closed():
    await set_setting("catalog_closed_at", "")
    await set_setting("catalog_closed_reason", "")


async def any_supplier_opened_after(moment: datetime | None) -> bool:
    if moment is None:
        return True
    for source in ("supplier1", "supplier2"):
        raw = (await get_setting(f"{source}_last_open_at", "")).strip()
        if not raw:
            continue
        try:
            opened = datetime.fromisoformat(raw)
            if opened > moment:
                return True
        except Exception:
            pass
    return False


async def should_catalog_be_open() -> tuple[bool, str]:
    if (await get_setting("catalog_manual_off", "0")) == "1":
        return False, "прайс выключен вручную"

    auto_enabled = (await get_setting("catalog_auto_enabled", "1")) == "1"

    if auto_enabled:
        now = now_local()

        if now.hour >= AUTO_CLOSE_HOUR:
            return False, f"автозакрытие после {AUTO_CLOSE_HOUR:02d}:00"

        s1, s2 = await suppliers_status()
        if s1 == "closed" and s2 == "closed":
            return False, "оба поставщика закрыты"

        closed_at = await get_catalog_closed_at()
        if closed_at and not await any_supplier_opened_after(closed_at):
            return False, "ждём открытия поставщика"

    return True, "прайс разрешён"


async def delete_all_catalog_messages() -> int:
    """Delete only bot-managed catalog messages. Warranty/delivery is never mapped here."""
    target = await get_target_chat_id()
    if not target:
        return 0

    mapping = await get_catalog_message_map()
    deleted = 0
    for cat, msg_id in list(mapping.items()):
        try:
            await bot.delete_message(target, msg_id)
            deleted += 1
        except Exception as e:
            err = str(e).lower()
            if not any(x in err for x in [
                "message to delete not found",
                "message_id_invalid",
                "message id invalid",
                "message not found",
            ]):
                log.warning("Cannot delete catalog message %s/%s: %s", cat, msg_id, e)

    # Clear mapping so reopening creates a clean fresh catalog.
    await set_catalog_message_map({})
    await set_setting("catalog_slots", "[]")
    return deleted


async def enforce_catalog_availability():
    should_open, reason = await should_catalog_be_open()
    mapping = await get_catalog_message_map()

    if not should_open:
        if mapping:
            deleted = await delete_all_catalog_messages()
            await set_catalog_closed_now(reason)
            log.info("Catalog auto-closed (%s), deleted %d messages", reason, deleted)
        elif not (await get_setting("catalog_closed_at", "")).strip():
            await set_catalog_closed_now(reason)
        return

    # If allowed to open and catalog is currently absent, restore it.
    if not mapping:
        await clear_catalog_closed()
        try:
            ok, result = await sync_catalog_to_target(skip_availability_check=True)
            log.info("Catalog auto-opened: %s / %s", ok, result)
        except Exception:
            log.exception("Automatic catalog reopening failed")


async def upsert_parsed(source: str, items: list[Parsed]):
    if items and source in {"supplier1", "supplier2"}:
        await mark_supplier_open(source)

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

def samsung_display_model(name: str) -> str | None:
    """Human-readable Samsung model heading inside the Smartphones message."""
    n = clean_spaces(strip_flags(name))

    # Galaxy A27 / A56 / A57...
    m = re.search(r"\bGalaxy\s+(A\d+)\b", n, re.I)
    if m:
        return f"Samsung Galaxy {m.group(1).upper()}"

    # Galaxy S25 / S25 Ultra / S26 FE / S26+ / S26 Ultra...
    m = re.search(
        r"\bGalaxy\s+(S\d+)(\+)?(?:\s+(Ultra|FE|Plus))?",
        n,
        re.I,
    )
    if m:
        model = m.group(1).upper()
        if m.group(2):
            model += "+"
        suffix = clean_spaces(m.group(3) or "")
        if suffix:
            model += f" {suffix}"
        return f"Samsung Galaxy {model}"

    # Galaxy Z Flip8 / Z Fold 8 / Z Fold8 Ultra...
    m = re.search(
        r"\bGalaxy\s+Z\s*(Flip|Fold)\s*(\d+)(?:\s+(Ultra))?",
        n,
        re.I,
    )
    if m:
        model = f"Z {m.group(1).title()}{m.group(2)}"
        if m.group(3):
            model += " Ultra"
        return f"Samsung Galaxy {model}"

    return None


def samsung_display_variant(p: Product, heading: str) -> str:
    """Remove repeated Samsung/model prefix and technical Samsung SKU codes."""
    n = clean_spaces(strip_flags(p.name))

    # Customer-facing price list does not need Samsung technical model codes.
    # Covers SM-S947B/DS, SM-A276B/DS, SM-A566B/DS, SM-S938B, etc.
    n = re.sub(
        r"(?<![A-Za-z0-9])SM-[A-Z0-9]+(?:/[A-Z0-9]+)*(?![A-Za-z0-9])",
        "",
        n,
        flags=re.I,
    )
    n = clean_spaces(n)
    # Remove optional leading Samsung.
    n = re.sub(r"^\s*Samsung\s+", "", n, flags=re.I)

    # Remove the heading's "Samsung " prefix and then matching Galaxy model.
    target = heading
    if target.lower().startswith("samsung "):
        target = target[len("Samsung "):]
    # Flexible spacing for Z Fold8 vs Z Fold 8.
    pattern = re.escape(target)
    pattern = pattern.replace(r"\ ", r"\s*")
    n = re.sub(r"^" + pattern + r"\s*", "", n, count=1, flags=re.I)

    variant = n.strip(" -—–") or clean_spaces(strip_flags(p.name))
    if p.region and p.region not in variant:
        variant = f"{variant} {p.region}".strip()
    return variant


def bw_headphone_model(name: str) -> str | None:
    """Model prefix for B&W/Bowers & Wilkins headphones."""
    n = clean_spaces(strip_flags(name))
    m = re.search(r"\b(Px7\s+S2e|Px7\s+S3|Px8)\b", n, re.I)
    if not m:
        return None
    model = clean_spaces(m.group(1))
    # Normalize capitalization exactly as product family names.
    if model.lower() == "px7 s2e":
        return "Px7 S2e"
    if model.lower() == "px7 s3":
        return "Px7 S3"
    if model.lower() == "px8":
        return "Px8"
    return model


def bw_headphone_variant(name: str, model: str) -> str:
    n = clean_spaces(strip_flags(name))
    # Remove brand heading/prefix if present.
    n = re.sub(r"^(?:B&W|Bowers\s*&\s*Wilkins)(?:\s+Headphones?)?\s*", "", n, flags=re.I)
    n = re.sub(r"^" + re.escape(model) + r"\s*", "", n, count=1, flags=re.I)
    return n.strip(" -—–") or clean_spaces(strip_flags(name))



def generic_display_model(p: Product) -> str:
    """
    Extract a compact base model for customer-facing grouping.

    Examples:
      Px7 S2e (Cloud Grey)                 -> Px7 S2e
      Harman Kardon Onyx Studio 9 Black   -> Onyx Studio 9
      Insta360 X5 Black                    -> X5
      Dyson HS08 Ceramic Pink              -> HS08
      Miele L1 Guard Cat & Dog             -> L1
      Google Fitbit Air Lavender           -> Air
      Galaxy A27 5G 6/128 Black            -> A27
    """
    n = clean_spaces(strip_flags(p.name))
    brand = p.brand or detect_brand(n)

    # Remove leading brand text for model detection.
    tail = n
    brand_patterns = [
        brand,
        "Samsung",
        "Google Fitbit",
        "Harman Kardon",
        "Bowers & Wilkins",
        "Bowers and Wilkins",
        "B&W Headphones",
        "B&W",
        "Insta360",
        "Insta 360",
        "Ray-Ban Meta",
        "Ray Ban Meta",
        "Dyson",
        "Miele",
        "Kodak",
        "DJI",
        "Fujifilm",
        "Garmin",
        "Coros",
        "Bose",
    ]
    for bp in sorted({x for x in brand_patterns if x}, key=len, reverse=True):
        if tail.lower().startswith(bp.lower()):
            tail = tail[len(bp):].strip(" -—–")
            break

    # Samsung families.
    m = re.search(r"\bGalaxy\s+(A\d+)\b", n, re.I)
    if m:
        return m.group(1).upper()

    m = re.search(r"\bGalaxy\s+(S\d+)(\+)?(?:\s+(Ultra|FE|Plus))?", n, re.I)
    if m:
        model = m.group(1).upper()
        if m.group(2):
            model += "+"
        if m.group(3):
            model += " " + m.group(3)
        return clean_spaces(model)

    m = re.search(r"\bGalaxy\s+Z\s*(Flip|Fold)\s*(\d+)(?:\s+(Ultra))?", n, re.I)
    if m:
        model = f"Z {m.group(1).title()}{m.group(2)}"
        if m.group(3):
            model += " Ultra"
        return model

    # Apple / Pixel.
    m = re.search(r"\biPhone\s+(\d+[A-Za-z]?)(?:\s+(Pro\s+Max|Pro|Plus|Mini|Air|e))?", n, re.I)
    if m:
        suffix = clean_spaces(m.group(2) or "")
        return f"iPhone {m.group(1)}" + (f" {suffix}" if suffix else "")

    m = re.search(r"\b(?:Google\s+)?Pixel\s+(\d+[A-Za-z]?)(?:\s+(Pro\s+XL|Pro|XL|a))?", n, re.I)
    if m:
        suffix = clean_spaces(m.group(2) or "")
        return f"Pixel {m.group(1)}" + (f" {suffix}" if suffix else "")

    # Common model-code families.
    patterns = [
        r"\b(Px7\s+S2e|Px7\s+S3|Px8)\b",
        r"\b(HS\d+[A-Z]?|HD\d+[A-Z]?|HT\d+[A-Z]?|HP\d+[A-Z]?|TP\d+[A-Z]?|BP\d+[A-Z]?|V\d+[A-Z]?)\b",
        r"\b(CX\d+|HX\d+|L\d+)\b",
        r"\b(X\d+|GO\s+Ultra|Go3S|GO\s+3S)\b",
        r"\b(Aura\s+Studio\s+\d+|Onyx\s+Studio\s+\d+|Soundsticks\s+\d+)\b",
        r"\b(QuietComfort(?:\s+Ultra)?|SoundLink\s+\w+)\b",
        r"\b(Pace\s+\d+|Forerunner\s+\d+|Vivoactive\s+\d+)\b",
        r"\b(Osmo\s+Mobile\s+\d+(?:\s+Pro)?|Osmo\s+Action\s+\d+(?:\s+Pro)?)\b",
        r"\b(Instax\s+Mini\s+\d+|Instax\s+Mini\s+Film)\b",
        r"\b(Fitbit\s+Air|Air)\b",
        r"\b(Wayfarer|Skyler|Headliner)(?:\s*\(Gen\s*\d+\))?\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, n, re.I)
        if m:
            return clean_spaces(m.group(1))

    # Storage/model tokens for Android brands.
    m = re.search(
        r"\b(Xiaomi|Redmi|POCO|Realme|Vivo)\s+"
        r"([A-Za-z0-9]+(?:\s+(?:Pro\+?|Ultra|Plus|T|FE|5G))?)\b",
        n, re.I
    )
    if m:
        return clean_spaces(m.group(2))

    # Generic fallback:
    # take the part before obvious variant details such as parentheses, color,
    # memory, region/model code separators.
    candidate = tail

    # Cut at parentheses (usually color/variant), commas, or obvious memory sizes.
    candidate = re.split(
        r"\s+\(|,\s*|\s+\d+\s*/\s*\d+|\s+\d+(?:GB|TB)\b",
        candidate,
        maxsplit=1,
        flags=re.I,
    )[0]

    words = candidate.split()

    # Keep a compact but useful model stem.
    if len(words) >= 3:
        return " ".join(words[:3])
    if len(words) >= 2:
        return " ".join(words[:2])
    if words:
        return words[0]

    return brand or "Модель"


def generic_display_variant(p: Product, model: str) -> str:
    """Return the rest of the product name after the displayed model."""
    n = clean_spaces(strip_flags(p.name))
    brand = p.brand or detect_brand(n)

    # Remove known leading brand prefix.
    prefixes = [
        brand,
        "Samsung",
        "Google Fitbit",
        "Harman Kardon",
        "Bowers & Wilkins",
        "Bowers and Wilkins",
        "B&W Headphones",
        "B&W",
        "Insta360",
        "Insta 360",
        "Ray-Ban Meta",
        "Ray Ban Meta",
        "Dyson",
        "Miele",
        "Kodak",
        "DJI",
        "Fujifilm",
        "Garmin",
        "Coros",
        "Bose",
    ]
    for prefix in sorted({x for x in prefixes if x}, key=len, reverse=True):
        if n.lower().startswith(prefix.lower()):
            n = n[len(prefix):].strip(" -—–")
            break

    # Samsung often still begins with Galaxy.
    if model.startswith(("A", "S", "Z ")):
        n = re.sub(r"^Galaxy\s+", "", n, flags=re.I)

    # Remove displayed model from the start, flexibly.
    pattern = re.escape(model).replace(r"\ ", r"\s*")
    n = re.sub(r"^" + pattern + r"\s*", "", n, count=1, flags=re.I)

    return n.strip(" -—–")


def render_brand_with_model_groups(parts: list[str], brand: str, items: list[tuple[Product, int]]):
    """
    Universal renderer:
      Brand heading once;
      same model repeated in bold on each variant line;
      blank line between model families.
    """
    parts.append(f"<b>{html.escape(brand)}</b>")

    groups: dict[str, list[tuple[Product, int]]] = {}
    for p, price in items:
        model = generic_display_model(p)
        groups.setdefault(model, []).append((p, price))

    for model in sorted(groups, key=lambda x: x.lower()):
        for p, price in sorted(
            groups[model],
            key=lambda x: generic_display_variant(x[0], model).lower()
        ):
            variant = generic_display_variant(p, model)
            if variant:
                line = (
                    f"<b>{html.escape(model)}</b> {html.escape(variant)} — "
                    f"<b>{price:,}</b>"
                ).replace(",", " ")
            else:
                line = (
                    f"<b>{html.escape(model)}</b> — <b>{price:,}</b>"
                ).replace(",", " ")
            parts.append(line)
        parts.append("")

async def render_catalog() -> list[tuple[str, str]]:
    rows = await effective_products()
    rows = [(p, price) for p, price in rows if p.category != "unclassified"]

    cats = {p.category for p, _ in rows}
    order = await category_positions(cats)

    by_cat: dict[str, list[tuple[Product, int]]] = {}
    for p, final_price in rows:
        by_cat.setdefault(p.category, []).append((p, final_price))

    output = []

    for cat in sorted(by_cat, key=lambda c: order.get(c, 9999)):
        cat_items = by_cat[cat]

        # Detect Samsung smartphones by product NAME, not by stored brand.
        # This is robust even if older DB rows have stale/wrong brand values.
        samsung_items = []
        other_items = []
        if cat == "smartphones":
            for item in cat_items:
                p, price = item
                if re.search(r"\bGalaxy\s+(?:A\d+|S\d+|Z\s*(?:Flip|Fold)\s*\d+)", p.name, re.I):
                    samsung_items.append(item)
                else:
                    other_items.append(item)
        else:
            other_items = cat_items

        if cat == "smartphones" and samsung_items and not other_items:
            category_heading = "📱 Samsung"
        else:
            category_heading = f"{CATEGORY_EMOJI.get(cat,'📦')} {CATEGORY_TITLE.get(cat, cat)}"

        parts = [f"<b>{html.escape(category_heading)}</b>", ""]

        # Samsung gets its own fixed structure.
        if samsung_items:
            series_groups: dict[str, dict[str, list[tuple[Product, int]]]] = {
                "Galaxy A": {},
                "Galaxy S": {},
                "Galaxy Fold": {},
            }

            for p, price in samsung_items:
                name = clean_spaces(strip_flags(p.name))

                m = re.search(r"\bGalaxy\s+(A\d+)\b", name, re.I)
                if m:
                    series = "Galaxy A"
                    model = m.group(1).upper()
                    series_groups[series].setdefault(model, []).append((p, price))
                    continue

                m = re.search(
                    r"\bGalaxy\s+(S\d+)(\+)?(?:\s+(Ultra|FE|Plus))?",
                    name, re.I
                )
                if m:
                    series = "Galaxy S"
                    model = m.group(1).upper()
                    if m.group(2):
                        model += "+"
                    if m.group(3):
                        model += " " + m.group(3)
                    series_groups[series].setdefault(model, []).append((p, price))
                    continue

                m = re.search(
                    r"\bGalaxy\s+Z\s*(Flip|Fold)\s*(\d+)(?:\s+(Ultra))?",
                    name, re.I
                )
                if m:
                    series = "Galaxy Fold"
                    model = f"Z {m.group(1).title()}{m.group(2)}"
                    if m.group(3):
                        model += " Ultra"
                    series_groups[series].setdefault(model, []).append((p, price))
                    continue

            for series in ("Galaxy A", "Galaxy S", "Galaxy Fold"):
                models = series_groups[series]
                if not models:
                    continue

                parts.append(f"<b>{series}</b>")

                for model in sorted(models, key=lambda x: x.lower()):
                    full_heading = f"Samsung Galaxy {model}"
                    for p, price in sorted(
                        models[model],
                        key=lambda x: samsung_display_variant(x[0], full_heading).lower()
                    ):
                        variant = samsung_display_variant(p, full_heading)
                        parts.append(
                            f"<b>{html.escape(model)}</b>"
                            + (f" {html.escape(variant)}" if variant else "")
                            + f" — <b>{price:,}</b>".replace(",", " ")
                        )
                    parts.append("")

        # Render all non-Samsung products with the universal brand/model formatter.
        by_brand: dict[str, list[tuple[Product, int]]] = {}
        for p, price in other_items:
            brand = p.brand or detect_brand(p.name)
            by_brand.setdefault(brand, []).append((p, price))

        for brand in sorted(by_brand, key=lambda x: x.lower()):
            render_brand_with_model_groups(parts, brand, by_brand[brand])

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

async def get_catalog_message_map() -> dict[str, int]:
    """
    Persistent mapping: category -> Telegram message_id.
    This avoids duplicate catalog posts when order changes or categories appear/disappear.
    """
    raw = await get_setting("catalog_message_map", "{}")
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            result = {}
            for cat, msg_id in data.items():
                try:
                    result[str(cat)] = int(msg_id)
                except Exception:
                    continue
            return result
    except Exception:
        pass
    return {}


async def set_catalog_message_map(mapping: dict[str, int]):
    await set_setting("catalog_message_map", json.dumps(mapping, ensure_ascii=False))


async def migrate_legacy_catalog_slots_if_needed(blocks: list[tuple[str, str]]) -> dict[str, int]:
    """
    One-time migration from the old positional catalog_slots list.
    We map existing slots to the CURRENT category order once, then stop using slots forever.
    """
    existing = await get_catalog_message_map()
    if existing:
        return existing

    raw = await get_setting("catalog_slots", "[]")
    try:
        slots = [int(x) for x in json.loads(raw)]
    except Exception:
        slots = []

    if not slots:
        return {}

    mapping: dict[str, int] = {}
    for idx, (cat, _) in enumerate(blocks):
        if idx < len(slots):
            mapping[cat] = slots[idx]

    if mapping:
        await set_catalog_message_map(mapping)
        log.info("Migrated %d legacy catalog slots to category mapping", len(mapping))

    return mapping


async def sync_catalog_to_target(skip_availability_check: bool = False) -> tuple[bool, str]:
    target = await get_target_chat_id()
    if not target:
        return False, "Группа прайса ещё не привязана"

    if not skip_availability_check:
        allowed, reason = await should_catalog_be_open()
        if not allowed:
            if await get_catalog_message_map():
                await delete_all_catalog_messages()
            await set_catalog_closed_now(reason)
            return False, f"Каталог закрыт: {reason}"

    blocks = await render_catalog()
    if not blocks:
        return False, "Нет включённых товаров для публикации"

    message_map = await migrate_legacy_catalog_slots_if_needed(blocks)
    active_categories = {cat for cat, _ in blocks}
    new_map: dict[str, int] = dict(message_map)

    for cat, body in blocks:
        msg_id = message_map.get(cat)

        # If this category already has a Telegram message, ALWAYS edit it.
        if msg_id:
            try:
                await bot.edit_message_text(
                    chat_id=target,
                    message_id=msg_id,
                    text=body,
                    parse_mode="HTML",
                )
                new_map[cat] = msg_id
                continue
            except Exception as e:
                err = str(e).lower()

                if "message is not modified" in err:
                    new_map[cat] = msg_id
                    continue

                # The operator may manually delete catalog messages from the group.
                # In that case the DB still points to a dead Telegram message_id.
                # Recreate exactly this category and replace the stale id.
                missing_message = any(x in err for x in [
                    "message to edit not found",
                    "message_id_invalid",
                    "message id invalid",
                    "message not found",
                    "message to be replied not found",
                ])

                if missing_message:
                    log.warning(
                        "Catalog message disappeared; recreating category=%s old_message_id=%s",
                        cat, msg_id
                    )
                    sent = await bot.send_message(
                        target,
                        body,
                        parse_mode="HTML",
                    )
                    new_map[cat] = sent.message_id
                    continue

                # For any other edit error do not create a duplicate.
                log.error(
                    "Cannot edit existing catalog message: category=%s message_id=%s error=%s",
                    cat, msg_id, e
                )
                new_map[cat] = msg_id
                continue

        # Create only when this category has never had a managed message.
        sent = await bot.send_message(
            target,
            body,
            parse_mode="HTML",
        )
        new_map[cat] = sent.message_id

    # Category removed from the current catalog -> delete only its managed message.
    for cat, msg_id in list(message_map.items()):
        if cat not in active_categories:
            try:
                await bot.delete_message(target, msg_id)
            except Exception as e:
                log.warning(
                    "Cannot delete removed category message: category=%s message_id=%s error=%s",
                    cat, msg_id, e
                )
            new_map.pop(cat, None)

    await set_catalog_message_map(new_map)
    await set_setting("catalog_slots", "[]")

    return True, f"Прайс обновлён: {len(blocks)} сообщ."

async def maybe_sync_catalog():
    target = await get_target_chat_id()
    if not target:
        return
    try:
        await enforce_catalog_availability()
        # If already open and mapped, edit the existing catalog with fresh data.
        allowed, _ = await should_catalog_be_open()
        if allowed and await get_catalog_message_map():
            await sync_catalog_to_target(skip_availability_check=True)
    except Exception:
        log.exception("automatic catalog sync failed")


async def acquire_single_instance_lock():
    global INSTANCE_LOCK_CONN

    if not DATABASE_URL.startswith("postgresql+asyncpg://"):
        log.warning("Single-instance DB lock skipped: non-PostgreSQL database")
        return

    conn = await engine.connect()
    try:
        result = await conn.execute(
            sql_text("SELECT pg_try_advisory_lock(:key)"),
            {"key": INSTANCE_LOCK_KEY},
        )
        locked = bool(result.scalar())
        if not locked:
            await conn.close()
            raise RuntimeError(
                "Другой экземпляр pricebot уже запущен и держит Telegram-сессии. "
                "На Railway оставь только 1 replica/instance."
            )

        INSTANCE_LOCK_CONN = conn
        log.info("Single-instance advisory lock acquired")
    except Exception:
        if not conn.closed:
            await conn.close()
        raise


async def release_single_instance_lock():
    global INSTANCE_LOCK_CONN
    conn = INSTANCE_LOCK_CONN
    INSTANCE_LOCK_CONN = None
    if conn is None:
        return
    try:
        await conn.execute(
            sql_text("SELECT pg_advisory_unlock(:key)"),
            {"key": INSTANCE_LOCK_KEY},
        )
    except Exception:
        log.exception("Failed to release advisory lock")
    finally:
        await conn.close()


class AccountManager:
    def __init__(self):
        self.clients: dict[int, TelegramClient] = {}
        self.listener_attached: set[int] = set()
        self.login: dict[int, dict] = {}
        self.last_request: dict[int, float] = {1: 0.0, 2: 0.0}
        # Telethon StringSession values are persisted in the existing DB.
        # This survives Railway redeploys without relying on local files.
        self.session_strings: dict[int, str] = {}

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
            saved = self.session_strings.get(slot, "")
            session = StringSession(saved) if saved else StringSession()
            client = TelegramClient(session, API_ID, API_HASH)
            self.clients[slot] = client
        return client

    async def load_saved_sessions(self):
        for slot in (1, 2):
            saved = (await get_setting(f"telegram_session_{slot}", "")).strip()
            if saved:
                self.session_strings[slot] = saved

    async def save_session(self, slot: int):
        client = self.get_client(slot)
        try:
            saved = client.session.save()
        except Exception as e:
            raise RuntimeError(f"Не удалось сохранить сессию аккаунта {slot}: {e}") from e
        if not saved:
            raise RuntimeError(f"Пустая сессия аккаунта {slot}")
        self.session_strings[slot] = saved
        await set_setting(f"telegram_session_{slot}", saved)
        log.info("account %s session saved to database", slot)

    async def is_authorized(self, slot: int) -> bool:
        if not API_ID or not API_HASH:
            return False
        client = self.get_client(slot)
        if not client.is_connected():
            await client.connect()
        authorized = await client.is_user_authorized()
        if not authorized and self.session_strings.get(slot):
            log.error(
                "Account %s has a saved session in DB but Telegram reports it unauthorized",
                slot,
            )
        return authorized

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

        # Resolve once so @username, numeric ids and converted t.me/c links work consistently.
        try:
            entity = await client.get_entity(source_chat)
        except Exception:
            # For private channels/groups, try dialogs as a fallback. This also
            # verifies that the logged-in account actually has access to the source.
            entity = None
            try:
                async for dialog in client.iter_dialogs():
                    if getattr(dialog.entity, "id", None) == abs(int(str(source_chat).replace("-100", "", 1))) if str(source_chat).startswith("-100") else False:
                        entity = dialog.entity
                        break
            except Exception:
                pass

            if entity is None:
                log.exception("%s cannot resolve source chat %r", source_name, source_chat)
                return

        async def process_text(text: str):
            raw = text or ""

            if supplier_closed_text(raw):
                await mark_supplier_closed(source_name)
                log.info("%s reported CLOSED", source_name)
                await enforce_catalog_availability()
                return 0

            parsed = parser(raw)
            if parsed:
                await upsert_parsed(source_name, parsed)
                log.info("%s parsed %d items", source_name, len(parsed))
                await enforce_catalog_availability()
                return len(parsed)
            return 0

        async def handler(event):
            try:
                await process_text(event.raw_text or "")
            except Exception:
                log.exception("%s failed to process source message", source_name)

        # Catch both new posts and edited price posts.
        client.add_event_handler(handler, events.NewMessage(chats=entity))
        client.add_event_handler(handler, events.MessageEdited(chats=entity))
        self.listener_attached.add(slot)
        log.info("%s listener attached to %s", source_name, source_chat)

        # Supplier 2 is passive: immediately import existing history too.
        if slot == 2:
            try:
                await self.refresh_supplier2_history()
            except Exception:
                log.exception("%s failed initial history import", source_name)

    async def refresh_supplier2_history(self) -> int:
        """Force-import the passive second supplier from recent chat history."""
        slot = 2
        if not await self.is_authorized(slot):
            raise RuntimeError("Аккаунт 2 не подключён")

        client = self.get_client(slot)
        source_chat = self.source_chat(slot)
        if not source_chat:
            raise RuntimeError("SOURCE_CHAT_2 не задан")

        try:
            entity = await client.get_entity(source_chat)
        except Exception:
            entity = None
            target_id = None
            if str(source_chat).startswith("-100"):
                try:
                    target_id = int(str(source_chat)[4:])
                except Exception:
                    target_id = None
            if target_id is not None:
                async for dialog in client.iter_dialogs():
                    if getattr(dialog.entity, "id", None) == target_id:
                        entity = dialog.entity
                        break
            if entity is None:
                raise RuntimeError(
                    f"Аккаунт 2 не видит источник {source_chat}. "
                    "Проверь, что этот Telegram-аккаунт состоит в нужной группе/канале."
                )

        messages = await client.get_messages(entity, limit=500)
        parser = self.parser(slot)
        source_name = self.source_name(slot)

        found: dict[str, Parsed] = {}

        # Individual messages.
        for msg in reversed(messages):
            body = getattr(msg, "raw_text", "") or ""
            for item in parser(body):
                found[canonicalize(item.name)] = item

        # Combined chronological history catches a product name and its
        # "От 1 шт" price when they are split over adjacent messages.
        combined = "\n".join(
            (getattr(msg, "raw_text", "") or "")
            for msg in reversed(messages)
            if (getattr(msg, "raw_text", "") or "").strip()
        )
        for item in parser(combined):
            found[canonicalize(item.name)] = item

        if found:
            await upsert_parsed(source_name, list(found.values()))
            await enforce_catalog_availability()

        log.info("supplier2 forced history refresh: %d unique items", len(found))
        return len(found)

    async def start_existing(self):
        if not API_ID or not API_HASH:
            log.warning("Telegram user accounts disabled: API_ID/API_HASH missing")
            return
        await self.load_saved_sessions()
        log.info(
            "Saved Telegram sessions loaded: account1=%s account2=%s",
            bool(self.session_strings.get(1)),
            bool(self.session_strings.get(2)),
        )
        for slot in (1, 2):
            try:
                if await self.is_authorized(slot):
                    await self.attach_listener(slot)
                    me = await self.get_client(slot).get_me()
                    log.info("account %s authorized as %s", slot, getattr(me, "username", None) or getattr(me, "id", "?"))
                    if slot == 1:
                        if supplier1_quiet_hours():
                            log.info(
                                "supplier1 initial /prices skipped: quiet hours %02d:00-%02d:00",
                                SUPPLIER1_QUIET_START,
                                SUPPLIER1_QUIET_END,
                            )
                        else:
                            try:
                                # Supplier 1 is a bot. Request a fresh price immediately.
                                await self.request_supplier(1)
                            except Exception:
                                log.exception("initial /prices request to supplier1 failed")
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
        await self.save_session(slot)
        await self.attach_listener(slot)
        if slot == 1:
            if supplier1_quiet_hours():
                log.info(
                    "supplier1 post-login /prices skipped: quiet hours %02d:00-%02d:00",
                    SUPPLIER1_QUIET_START,
                    SUPPLIER1_QUIET_END,
                )
            else:
                try:
                    await self.request_supplier(1)
                except Exception:
                    log.exception("initial /prices request after account1 login failed")
        elif slot == 2:
            try:
                await self.refresh_supplier2_history()
            except Exception:
                log.exception("initial supplier2 history refresh after login failed")

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

    async def collect_supplier1_response(self, entity, after_id: int, timeout_sec: int = 12) -> int:
        """
        Poll recent messages after /prices so we do not rely only on event delivery.
        Useful when the supplier bot answers with multiple messages or edits quickly.
        """
        client = self.get_client(1)
        deadline = time.monotonic() + timeout_sec
        found: dict[str, Parsed] = {}
        last_seen_id = after_id

        while time.monotonic() < deadline:
            try:
                messages = await client.get_messages(entity, limit=30, min_id=after_id)
            except TypeError:
                # Older Telethon fallback: fetch recent and filter manually.
                messages = await client.get_messages(entity, limit=30)

            fresh = []
            for msg in messages:
                mid = getattr(msg, "id", 0) or 0
                if mid > after_id:
                    fresh.append(msg)
                    last_seen_id = max(last_seen_id, mid)

            # Parse each fresh message and detect supplier closure.
            for msg in reversed(fresh):
                body = getattr(msg, "raw_text", "") or ""
                if supplier_closed_text(body):
                    await mark_supplier_closed("supplier1")
                    await enforce_catalog_availability()
                for item in parse_supplier1(body):
                    found[canonicalize(item.name)] = item

            # Also parse combined response in case the bot splits a price over messages.
            combined = "\n".join(
                (getattr(msg, "raw_text", "") or "")
                for msg in reversed(fresh)
                if (getattr(msg, "raw_text", "") or "").strip()
            )
            if combined:
                for item in parse_supplier1(combined):
                    found[canonicalize(item.name)] = item

            # If we already got products, give the supplier a short grace period
            # for possible extra messages, then finish.
            if found:
                await asyncio.sleep(1.5)
                try:
                    extra = await client.get_messages(entity, limit=30, min_id=last_seen_id)
                except TypeError:
                    extra = []
                for msg in reversed(extra):
                    body = getattr(msg, "raw_text", "") or ""
                    for item in parse_supplier1(body):
                        found[canonicalize(item.name)] = item
                break

            await asyncio.sleep(1)

        if found:
            await upsert_parsed("supplier1", list(found.values()))
            await enforce_catalog_availability()

        log.info("supplier1 active response collection parsed %d unique items", len(found))
        return len(found)

    async def request_supplier(self, slot: int) -> str:
        source_chat = self.source_chat(slot)
        if not source_chat:
            raise RuntimeError(f"SOURCE_CHAT_{slot} не задан")

        command = (await get_setting(f"supplier{slot}_command", "/prices" if slot == 1 else "")).strip()
        if slot == 1 and not command:
            command = "/prices"
            await set_setting("supplier1_command", command)
        if not command:
            raise RuntimeError(f"Команда для поставщика {slot} не настроена")

        if not await self.is_authorized(slot):
            raise RuntimeError(f"Аккаунт {slot} не подключён")

        client = self.get_client(slot)
        await self.attach_listener(slot)

        entity = await client.get_entity(source_chat)

        before = await client.get_messages(entity, limit=1)
        after_id = 0
        if before:
            try:
                after_id = before[0].id
            except Exception:
                after_id = 0

        await client.send_message(entity, command)
        self.last_request[slot] = time.monotonic()
        log.info("supplier%s request sent to %s: %s", slot, source_chat, command)

        if slot == 1:
            count = await self.collect_supplier1_response(entity, after_id)
            log.info("supplier1 /prices response parsed: %d items", count)

        return command

    async def polling_loop(self, slot: int):
        while True:
            try:
                enabled = (await get_setting(f"supplier{slot}_poll_enabled", "0")) == "1"
                command = (await get_setting(f"supplier{slot}_command", "/prices" if slot == 1 else "")).strip()
                if slot == 1 and not command:
                    command = "/prices"
                    await set_setting("supplier1_command", command)
                interval_min = max(1, int(await get_setting(f"supplier{slot}_interval", "30")))
                if enabled and command and await self.is_authorized(slot):
                    if slot == 1 and supplier1_quiet_hours():
                        await asyncio.sleep(10)
                        continue

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
        self.session_strings.pop(slot, None)
        await set_setting(f"telegram_session_{slot}", "")

        # Clean up legacy file sessions from older versions if they exist.
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
    kb.button(text="🛡 Управление прайсом", callback_data="catalog_control")
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

@dp.callback_query(F.data == "catalog_control")
async def catalog_control(c: CallbackQuery):
    if not admin_only(c): return

    auto_enabled = (await get_setting("catalog_auto_enabled", "1")) == "1"
    manual_off = (await get_setting("catalog_manual_off", "0")) == "1"

    kb = InlineKeyboardBuilder()
    kb.button(
        text=("⏱ Автовыключение: ВКЛ" if auto_enabled else "⏱ Автовыключение: ВЫКЛ"),
        callback_data="catalog_auto_toggle",
    )
    if manual_off:
        kb.button(text="✅ Включить прайс", callback_data="catalog_manual_on")
    else:
        kb.button(text="⛔ Выключить прайс", callback_data="catalog_manual_off")
    kb.button(text="⬅️ Меню", callback_data="menu")
    kb.adjust(1)

    await c.message.edit_text(
        "<b>Управление прайсом</b>\n\n"
        f"Автовыключение: <b>{'включено' if auto_enabled else 'выключено'}</b>\n"
        f"Ручное состояние: <b>{'выключен' if manual_off else 'автоматически'}</b>\n\n"
        f"При включённом автовыключении каталог скрывается после {AUTO_CLOSE_HOUR:02d}:00 "
        "или когда оба поставщика закрыты.\n"
        "Гарантия/выдача не удаляется.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
    await c.answer()


@dp.callback_query(F.data == "catalog_auto_toggle")
async def catalog_auto_toggle(c: CallbackQuery):
    if not admin_only(c): return
    enabled = (await get_setting("catalog_auto_enabled", "1")) == "1"
    await set_setting("catalog_auto_enabled", "0" if enabled else "1")
    await c.answer("Автовыключение выключено" if enabled else "Автовыключение включено", show_alert=True)
    await catalog_control(c)


@dp.callback_query(F.data == "catalog_manual_off")
async def catalog_manual_off(c: CallbackQuery):
    if not admin_only(c): return
    await set_setting("catalog_manual_off", "1")
    deleted = await delete_all_catalog_messages()
    await set_catalog_closed_now("ручное выключение")
    await c.answer(f"Прайс выключен. Удалено сообщений: {deleted}", show_alert=True)
    await catalog_control(c)


@dp.callback_query(F.data == "catalog_manual_on")
async def catalog_manual_on(c: CallbackQuery):
    if not admin_only(c): return
    await set_setting("catalog_manual_off", "0")
    await clear_catalog_closed()
    try:
        ok, result = await sync_catalog_to_target(skip_availability_check=True)
        await c.answer("Прайс включён" if ok else result[:150], show_alert=True)
    except Exception as e:
        await c.answer(f"Не удалось включить: {str(e)[:140]}", show_alert=True)
    await catalog_control(c)


@dp.callback_query(F.data == "target")
async def target_menu(c: CallbackQuery):
    if not admin_only(c): return
    target = await get_target_chat_id()
    title = await get_setting("target_chat_title", "")
    slots = await get_catalog_slots()
    status = f"✅ {html.escape(title or str(target))}" if target else "❌ не привязана"
    kb = InlineKeyboardBuilder()
    kb.button(
        text=("🔁 Сменить группу по сообщению" if target else "📨 Привязать по сообщению"),
        callback_data="target:bind"
    )
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
        "<b>Привязка / смена группы</b>\n\n"
        "Перешли мне <b>любое сообщение из нужной группы или канала</b>.\n"
        "Я сам определю источник, сохраню его и сброшу старые ID товарных сообщений.",
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

    old_target = await get_target_chat_id()

    await set_setting("target_chat_id", str(chat_id))
    await set_setting("target_chat_title", title)

    # Message IDs belong to the OLD Telegram chat. Never reuse them in a new target.
    # We intentionally do not delete anything from the old group here.
    if str(old_target) != str(chat_id):
        await set_catalog_message_map({})
        await set_catalog_slots([])
        await set_setting("catalog_slots", "[]")
        await clear_catalog_closed()

    USER_STATE.pop(m.from_user.id, None)

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить прайс", callback_data="publish")
    kb.button(text="⬅️ Меню", callback_data="menu")
    kb.adjust(1)
    await m.answer(
        f"✅ Группа привязана: <b>{html.escape(title)}</b>\n\n"
        "Старые ID товарных сообщений сброшены. "
        "Теперь прайс будет создаваться и дальше редактироваться уже здесь.",
        parse_mode="HTML", reply_markup=kb.as_markup())

@dp.callback_query(F.data == "target:clear")
async def target_clear(c: CallbackQuery):
    if not admin_only(c): return
    await set_setting("target_chat_id", "")
    await set_setting("target_chat_title", "")
    await set_catalog_message_map({})
    await set_catalog_slots([])
    await set_setting("catalog_slots", "[]")
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
    command = (await get_setting("supplier1_command", "/prices")).strip()
    state = "🟢" if enabled else "⚪️"
    cmd = command if command else "команда не задана"
    kb.button(text=f"{state} Поставщик 1 · {interval} мин · {cmd[:18]}", callback_data="poll:1")
    kb.button(text="⬅️ Меню", callback_data="menu")
    kb.adjust(1)
    await c.message.edit_text(
        "<b>Автозапрос прайса</b>\n\n"
        "Команды и интервалы используются только для <b>Поставщика 1</b>.\n"
        f"🌙 Тихий режим: <b>{SUPPLIER1_QUIET_START:02d}:00–{SUPPLIER1_QUIET_END:02d}:00</b> — автоматические /prices не отправляются.\n"
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
    command = (await get_setting(f"supplier{slot}_command", "/prices" if slot == 1 else "")).strip() or ("/prices" if slot == 1 else "не задана")
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
        "Для Поставщика 1 по умолчанию используется <code>/prices</code>.", parse_mode="HTML")
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
    command = (await get_setting(f"supplier{slot}_command", "/prices" if slot == 1 else "")).strip()
    if slot == 1 and not command:
        command = "/prices"
        await set_setting("supplier1_command", command)
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


def model_group_label(name: str) -> str:
    """
    Selection rule:
      - smartphones are split by model;
      - every other product type is selected by whole brand.
    """
    n = clean_spaces(strip_flags(name).replace("**", "").replace("\u00a0", " "))
    category = detect_category(n)
    brand = detect_brand(n)

    if category != "smartphones":
        return brand

    # Apple iPhone
    m = re.search(
        r"\biPhone\s+(\d+[A-Za-z]?)(?:\s+(Pro\s+Max|Pro|Plus|Mini|Air|e))?",
        n, re.I
    )
    if m:
        suffix = clean_spaces(m.group(2) or "")
        return f"iPhone {m.group(1)}" + (f" {suffix.title()}" if suffix else "")

    # Samsung smartphones:
    # all regular Galaxy S-series variants -> one "Samsung" group;
    # all foldables (Z Fold / Z Flip) -> one "Samsung Fold" group.
    if re.search(r"\bGalaxy\s+Z\s*(?:Flip|Fold)\s*\d+", n, re.I):
        return "Samsung Fold"

    if re.search(r"\bGalaxy\s+A\d+", n, re.I):
        return "Samsung A"

    if re.search(r"\bGalaxy\s+S\d+", n, re.I):
        return "Samsung S"

    # Google Pixel
    m = re.search(
        r"\b(?:Google\s+)?Pixel\s+(\d+[A-Za-z]?)(?:\s+(Pro\s+XL|Pro|XL|a))?",
        n, re.I
    )
    if m:
        suffix = clean_spaces(m.group(2) or "")
        return f"Google Pixel {m.group(1)}" + (f" {suffix}" if suffix else "")

    # Xiaomi / Redmi / POCO / Realme / Vivo.
    m = re.search(
        r"\b(Xiaomi|Redmi|POCO|Realme|Vivo)\s+"
        r"([A-Za-z0-9]+(?:\s+(?:Pro\+?|Ultra|Plus|T|FE|5G))?)\b",
        n, re.I
    )
    if m:
        return clean_spaces(f"{m.group(1)} {m.group(2)}")

    # Redmi Note may arrive without Redmi prefix in some supplier lists.
    m = re.search(
        r"\bNote\s+(\d+[A-Za-z]?)(?:\s+(Pro\+?|Pro|5G))?",
        n, re.I
    )
    if m:
        suffix = clean_spaces(m.group(2) or "")
        return f"Redmi Note {m.group(1)}" + (f" {suffix}" if suffix else "")

    # Fallback for a smartphone: brand + first two model tokens.
    tail = n
    if tail.lower().startswith(brand.lower()):
        tail = tail[len(brand):].strip(" -")
    words = tail.split()
    stem = " ".join(words[:2]) if words else ""
    return clean_spaces(f"{brand} {stem}") if stem else brand

def model_group_token(label: str) -> str:
    return hashlib.sha1(label.encode("utf-8")).hexdigest()[:12]



def samsung_series_label(name: str) -> str | None:
    n = clean_spaces(strip_flags(name))
    if re.search(r"\bGalaxy\s+Z\s*(?:Flip|Fold)\s*\d+", n, re.I):
        return "Samsung Fold"
    if re.search(r"\bGalaxy\s+A\d+", n, re.I):
        return "Samsung A"
    if re.search(r"\bGalaxy\s+S\d+", n, re.I):
        return "Samsung S"
    return None


def group_samsung_series(rows: list[Product]):
    groups: dict[str, dict] = {}
    for p in rows:
        label = samsung_series_label(p.name)
        if not label:
            continue
        token = model_group_token(label)
        g = groups.setdefault(token, {"label": label, "items": []})
        g["items"].append(p)
    return sorted(groups.items(), key=lambda kv: kv[1]["label"].lower())


def group_source_models(rows: list[Product]):
    groups: dict[str, dict] = {}
    for p in rows:
        label = model_group_label(p.name)
        token = model_group_token(label)
        g = groups.setdefault(token, {"label": label, "items": []})
        g["items"].append(p)
    return sorted(groups.items(), key=lambda kv: kv[1]["label"].lower())

PRODUCTS_PER_PAGE = 10

async def show_source_products(message: Message, source: str, page: int = 0):
    async with Session() as s:
        rows = list((await s.scalars(
            select(Product).where(Product.source == source).order_by(Product.brand, Product.name)
        )).all())

    grouped = group_source_models(rows)

    # Collapse Samsung smartphone groups into one top-level Samsung button.
    samsung_items = [p for p in rows if samsung_series_label(p.name)]
    filtered_groups = []
    for token, g in grouped:
        if g["label"] in {"Samsung", "Samsung Fold", "Samsung S"}:
            continue
        filtered_groups.append((token, g))

    if samsung_items:
        filtered_groups.append((
            model_group_token("Samsung"),
            {"label": "Samsung", "items": samsung_items, "submenu": "samsung"}
        ))

    grouped = sorted(filtered_groups, key=lambda kv: kv[1]["label"].lower())

    total_groups = len(grouped)
    selected_groups = sum(
        1 for _, g in grouped
        if g["items"] and all(p.enabled for p in g["items"])
    )

    pages = max(1, (total_groups + PRODUCTS_PER_PAGE - 1) // PRODUCTS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    start_idx = page * PRODUCTS_PER_PAGE
    chunk = grouped[start_idx:start_idx + PRODUCTS_PER_PAGE]

    kb = InlineKeyboardBuilder()
    for token, g in chunk:
        items = g["items"]
        enabled_count = sum(1 for p in items if p.enabled)

        if enabled_count == len(items) and items:
            mark = "✅"
        elif enabled_count:
            mark = "🟡"
        else:
            mark = "▫️"

        if g.get("submenu") == "samsung":
            label = f"{mark} Samsung"
            kb.button(text=label, callback_data=f"src_samsung:{source}:{page}")
        else:
            label = f"{mark} {g['label'][:42]} · {len(items)} поз."
            kb.button(text=label, callback_data=f"src_model:{token}:{source}:{page}")

    if source == "supplier2":
        kb.button(text="🔄 Перечитать прайс", callback_data="src_refresh:supplier2")

    if page > 0:
        kb.button(text="⬅️", callback_data=f"src_page:{source}:{page-1}")
    kb.button(text=f"{page+1}/{pages}", callback_data="noop")
    if page + 1 < pages:
        kb.button(text="➡️", callback_data=f"src_page:{source}:{page+1}")
    kb.button(text="⬅️ Меню", callback_data="menu")
    kb.adjust(1)

    title = "Поставщик 1" if source == "supplier1" else "Поставщик 2"
    body = (
        f"<b>{title}</b> <code>{BUILD_VERSION}</code>\n\n"
        f"Выбрано групп: <b>{selected_groups}</b> из <b>{total_groups}</b>.\n"
        "Смартфоны Samsung открываются отдельным подменю по сериям.\n\n"
        "✅ — включено всё, 🟡 — включено частично, ▫️ — выключено."
    )

    try:
        await message.edit_text(body, parse_mode="HTML", reply_markup=kb.as_markup())
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise


async def show_samsung_series(message: Message, source: str, parent_page: int = 0):
    async with Session() as s:
        rows = list((await s.scalars(
            select(Product).where(Product.source == source).order_by(Product.name)
        )).all())

    groups = group_samsung_series(rows)

    kb = InlineKeyboardBuilder()
    for token, g in groups:
        items = g["items"]
        enabled_count = sum(1 for p in items if p.enabled)
        if enabled_count == len(items) and items:
            mark = "✅"
        elif enabled_count:
            mark = "🟡"
        else:
            mark = "▫️"

        kb.button(
            text=f"{mark} {g['label']} · {len(items)} поз.",
            callback_data=f"src_samsung_toggle:{token}:{source}:{parent_page}",
        )

    kb.button(text="⬅️ Назад к брендам", callback_data=f"src_page:{source}:{parent_page}")
    kb.adjust(1)

    await message.edit_text(
        "<b>Samsung</b>\n\n"
        "Выбери серию:\n"
        "• <b>Samsung A</b> — серия Galaxy A\n"
        "• <b>Samsung S</b> — серия Galaxy S\n"
        "• <b>Samsung Fold</b> — Galaxy Z Fold и Z Flip",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@dp.callback_query(F.data.startswith("src:"))
async def source_menu(c: CallbackQuery):
    if not admin_only(c): return
    source = c.data.split(":",1)[1]
    USER_STATE.pop(c.from_user.id, None)
    if source == "supplier2":
        try:
            await accounts.refresh_supplier2_history()
        except Exception as e:
            log.exception("supplier2 refresh on menu open failed")
            await c.answer(f"Не удалось прочитать источник 2: {str(e)[:120]}", show_alert=True)
    await show_source_products(c.message, source, 0)
    try:
        await c.answer()
    except Exception:
        pass

@dp.callback_query(F.data == "src_refresh:supplier2")
async def source2_refresh(c: CallbackQuery):
    if not admin_only(c): return
    try:
        count = await accounts.refresh_supplier2_history()
        await show_source_products(c.message, "supplier2", 0)
        await c.answer(f"Найдено позиций: {count}", show_alert=True)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            await c.answer("Прайс уже актуален", show_alert=True)
        else:
            raise
    except Exception as e:
        log.exception("manual supplier2 refresh failed")
        await c.answer(f"Ошибка источника 2: {str(e)[:140]}", show_alert=True)

@dp.callback_query(F.data.startswith("src_samsung:"))
async def source_samsung_menu(c: CallbackQuery):
    if not admin_only(c): return
    _, source, page = c.data.split(":")
    await show_samsung_series(c.message, source, int(page))
    await c.answer()


@dp.callback_query(F.data.startswith("src_samsung_toggle:"))
async def source_samsung_toggle(c: CallbackQuery):
    if not admin_only(c): return
    _, token, source, parent_page = c.data.split(":")

    async with Session() as s:
        rows = list((await s.scalars(
            select(Product).where(Product.source == source)
        )).all())

        matched = [
            p for p in rows
            if samsung_series_label(p.name)
            and model_group_token(samsung_series_label(p.name)) == token
        ]

        if not matched:
            await c.answer("Серия не найдена", show_alert=True)
            return

        new_state = not all(p.enabled for p in matched)
        for p in matched:
            p.enabled = new_state
        await s.commit()

    await show_samsung_series(c.message, source, int(parent_page))
    await c.answer(
        f"{'Включена' if new_state else 'Выключена'} серия: {samsung_series_label(matched[0].name)}",
        show_alert=True,
    )
    await maybe_sync_catalog()


@dp.callback_query(F.data.startswith("src_page:"))
async def source_page(c: CallbackQuery):
    if not admin_only(c): return
    _, source, page = c.data.split(":")
    await show_source_products(c.message, source, int(page))
    await c.answer()

@dp.callback_query(F.data.startswith("src_model:"))
async def source_model_toggle(c: CallbackQuery):
    if not admin_only(c): return
    _, token, source, page = c.data.split(":")

    async with Session() as s:
        rows = list((await s.scalars(
            select(Product).where(Product.source == source)
        )).all())

        matched = [p for p in rows if model_group_token(model_group_label(p.name)) == token]
        if not matched:
            await c.answer("Модель не найдена", show_alert=True)
            return

        # If every variant is enabled -> disable the whole model.
        # Otherwise enable every variant.
        new_state = not all(p.enabled for p in matched)
        for p in matched:
            p.enabled = new_state
        await s.commit()

    await show_source_products(c.message, source, int(page))
    await c.answer(
        f"{'Включены' if new_state else 'Выключены'} все варианты: {len(matched)}",
        show_alert=True
    )
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
        raw_lines = []
        for line in (m.text or "").splitlines():
            normalized_line = clean_spaces(line.replace("\u00a0", " "))
            if normalized_line:
                raw_lines.append(normalized_line)

        def parse_own_line(raw: str):
            # Normalize pasted Telegram/Markdown text:
            # **bold**, __bold__, `code`, and non-breaking spaces.
            cleaned = raw.replace("\u00a0", " ")
            cleaned = cleaned.replace("**", "").replace("__", "").replace("`", "")
            cleaned = clean_spaces(cleaned).strip()
            lowered = cleaned.lower()
            for suffix in ("руб.", "руб", "₽", "р.", "р"):
                if lowered.endswith(suffix):
                    cleaned = cleaned[:-len(suffix)].rstrip()
                    break
            parts = cleaned.split()
            if not parts:
                return None
            last = parts[-1]
            digits = last.replace(".", "").replace(",", "")
            if digits.isdigit() and len(digits) >= 4:
                name = " ".join(parts[:-1]).strip(" -—–:|")
                return (name, int(digits)) if name else None
            if len(parts) >= 2 and parts[-2].isdigit() and last.isdigit() and len(last) == 3:
                name = " ".join(parts[:-2]).strip(" -—–:|")
                return (name, int(parts[-2] + last)) if name else None
            return None

        parsed_items, bad_lines = [], []
        for line in raw_lines:
            item = parse_own_line(line)
            (parsed_items if item else bad_lines).append(item if item else line)

        if not parsed_items:
            await m.answer(
                "Не увидел ни одной позиции с ценой. Каждый товар должен быть с новой строки.\n"
                "Пример: Google Fitbit Air Lavender 10700"
            )
            return

        async with Session() as s:
            for name, price in parsed_items:
                canonical = canonicalize(name)
                existing = await s.scalar(select(Product).where(
                    Product.source == "own", Product.canonical == canonical
                ))
                if existing:
                    existing.name = name
                    existing.price = price
                    existing.enabled = True
                    existing.brand = detect_brand(name)
                    existing.category = detect_category(name)
                    existing.region = detect_region(name)
                else:
                    s.add(Product(
                        source="own", source_key=canonical, name=name, canonical=canonical,
                        brand=detect_brand(name), category=detect_category(name),
                        price=price, region=detect_region(name), enabled=True
                    ))
            await s.commit()

        USER_STATE.pop(m.from_user.id, None)
        result_lines = [f"✅ {html.escape(name)} — {price:,} ₽".replace(",", " ") for name, price in parsed_items]
        if bad_lines:
            result_lines.append(f"⚠️ Не распознано строк: {len(bad_lines)}")
        await m.answer("\n".join(result_lines), reply_markup=main_kb(), parse_mode="HTML")
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
        "Пришли товар и нашу продажную цену. Можно несколько позиций — каждая с новой строки.\n\n"
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

    if not cats:
        kb.row(InlineKeyboardButton(text="⬅️ Меню", callback_data="menu"))
        await c.message.edit_text(
            "<b>Порядок сообщений в каталоге</b>\n\nПока нет активных категорий.",
            parse_mode="HTML",
            reply_markup=kb.as_markup()
        )
        await c.answer()
        return

    lines = ["<b>Порядок сообщений в каталоге:</b>", ""]
    for i, cat in enumerate(cats, start=1):
        emoji = CATEGORY_EMOJI.get(cat, "📦")
        title = CATEGORY_TITLE.get(cat, cat)
        lines.append(f"<b>{i}. {emoji} {html.escape(title)}</b>")

        controls = []
        if i > 1:
            controls.append(InlineKeyboardButton(text="⬆️ Выше", callback_data=f"ordup:{cat}"))
        if i < len(cats):
            controls.append(InlineKeyboardButton(text="⬇️ Ниже", callback_data=f"orddn:{cat}"))
        if controls:
            kb.row(*controls)

    kb.row(InlineKeyboardButton(text="⬅️ Меню", callback_data="menu"))

    await c.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup()
    )
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
    supplier1_command = (await get_setting("supplier1_command", "/prices")).strip()
    if not supplier1_command:
        supplier1_command = "/prices"
    await set_setting("supplier1_command", supplier1_command)
    await set_setting("supplier1_poll_enabled", await get_setting("supplier1_poll_enabled", "0"))
    await set_setting("catalog_auto_enabled", await get_setting("catalog_auto_enabled", "1"))
    await set_setting("catalog_manual_off", await get_setting("catalog_manual_off", "0"))
    if TARGET_CHAT_ID and not (await get_setting("target_chat_id", "")).strip():
        await set_setting("target_chat_id", str(TARGET_CHAT_ID))

async def availability_loop():
    """Check 19:00 close and supplier-driven reopen once per minute."""
    while True:
        try:
            await enforce_catalog_availability()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("availability loop error")
        await asyncio.sleep(60)


async def main():
    await init_db()
    await acquire_single_instance_lock()
    poll_tasks = []
    try:
        await accounts.start_existing()
        poll_tasks = [
            asyncio.create_task(accounts.polling_loop(1)),
            asyncio.create_task(availability_loop()),
        ]
        await dp.start_polling(bot)
    finally:
        for task in poll_tasks:
            task.cancel()
        if poll_tasks:
            await asyncio.gather(*poll_tasks, return_exceptions=True)

        for client in list(accounts.clients.values()):
            try:
                if client.is_connected():
                    await client.disconnect()
            except Exception:
                log.exception("Failed to disconnect Telegram client on shutdown")

        await release_single_instance_lock()

if __name__ == "__main__":
    asyncio.run(main())
