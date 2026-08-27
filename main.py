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
BUILD_VERSION = "v36"

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
        "galaxy s", "galaxy z", "redmi ", "note ", "xiaomi 1",
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

    # Never publish a generic "Другое" block.
    # Unknown products stay in the database but are hidden until classified properly.
    rows = [(p, price) for p, price in rows if p.category != "unclassified"]

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
                parts.append(f"• {html.escape(display_name(p))} — <b>{price:,}</b>".replace(",", " "))
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


async def sync_catalog_to_target() -> tuple[bool, str]:
    target = await get_target_chat_id()
    if not target:
        return False, "Группа прайса ещё не привязана"

    blocks = await render_catalog()
    if not blocks:
        return False, "Нет включённых товаров для публикации"

    message_map = await migrate_legacy_catalog_slots_if_needed(blocks)
    active_categories = {cat for cat, _ in blocks}
    new_map: dict[str, int] = dict(message_map)

    # One category = one Telegram message.
    for cat, body in blocks:
        msg_id = message_map.get(cat)

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
                if "message is not modified" in str(e).lower():
                    new_map[cat] = msg_id
                    continue

                # If the stored message vanished or can't be edited anymore,
                # create a replacement ONLY for this category.
                log.warning(
                    "Catalog message for category %s (%s) cannot be edited: %s",
                    cat, msg_id, e
                )

        sent = await bot.send_message(target, body, parse_mode="HTML")
        new_map[cat] = sent.message_id

        # If we replaced an old broken message, best-effort delete it.
        if msg_id and msg_id != sent.message_id:
            try:
                await bot.delete_message(target, msg_id)
            except Exception:
                pass

    # Delete messages only for categories that are no longer active.
    # Fixed warranty/delivery message is never part of this map.
    for cat, msg_id in list(message_map.items()):
        if cat not in active_categories:
            try:
                await bot.delete_message(target, msg_id)
            except Exception as e:
                log.warning("Cannot delete stale category %s message %s: %s", cat, msg_id, e)
            new_map.pop(cat, None)

    await set_catalog_message_map(new_map)

    # Old positional slots are no longer used after migration.
    await set_setting("catalog_slots", "[]")

    return True, f"Прайс обновлён: {len(blocks)} сообщ."


async def get_catalog_slots() -> list[int]:
    """
    Compatibility helper for old UI counters.
    Returns current managed category message ids.
    """
    mapping = await get_catalog_message_map()
    return list(mapping.values())


async def set_catalog_slots(ids: list[int]):
    # Kept only so old call sites do not crash; positional slots are deprecated.
    await set_setting("catalog_slots", json.dumps(ids))

async def maybe_sync_catalog():
    target = await get_target_chat_id()
    if not target:
        return
    try:
        await sync_catalog_to_target()
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
            parsed = parser(text or "")
            if parsed:
                await upsert_parsed(source_name, parsed)
                log.info("%s parsed %d items", source_name, len(parsed))
                await maybe_sync_catalog()
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
            await maybe_sync_catalog()

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

            # Parse each fresh message.
            for msg in reversed(fresh):
                body = getattr(msg, "raw_text", "") or ""
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
            await maybe_sync_catalog()

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
    command = (await get_setting("supplier1_command", "/prices")).strip()
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

    if re.search(r"\bGalaxy\s+S\d+", n, re.I):
        return "Samsung"

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
    if TARGET_CHAT_ID and not (await get_setting("target_chat_id", "")).strip():
        await set_setting("target_chat_id", str(TARGET_CHAT_ID))

async def main():
    await init_db()
    await acquire_single_instance_lock()
    poll_tasks = []
    try:
        await accounts.start_existing()
        poll_tasks = [asyncio.create_task(accounts.polling_loop(1))]
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
