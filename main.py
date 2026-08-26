import asyncio
import os
import re
import html
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from sqlalchemy import String, Integer, Boolean, Float, select, delete
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pricebot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "0")) if os.getenv("TARGET_CHAT_ID") else 0
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_1 = os.getenv("SESSION_1", "")
SESSION_2 = os.getenv("SESSION_2", "")
SOURCE_CHAT_1 = int(os.getenv("SOURCE_CHAT_1", "0")) if os.getenv("SOURCE_CHAT_1") else 0
SOURCE_CHAT_2 = int(os.getenv("SOURCE_CHAT_2", "0")) if os.getenv("SOURCE_CHAT_2") else 0
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./pricebot.db")
DEFAULT_MARKUP = int(os.getenv("DEFAULT_MARKUP", "2000"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")

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

engine = create_async_engine(DATABASE_URL)
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

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

USER_STATE: dict[int, dict] = {}

def admin_only(msg_or_cb) -> bool:
    uid = msg_or_cb.from_user.id
    return uid == ADMIN_ID

def main_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📦 Поставщик 1", callback_data="src:supplier1")
    kb.button(text="📦 Поставщик 2", callback_data="src:supplier2")
    kb.button(text="🏠 Наш товар", callback_data="own")
    kb.button(text="💰 Наценка", callback_data="markup")
    kb.button(text="↕️ Порядок блоков", callback_data="order")
    kb.button(text="👁 Предпросмотр", callback_data="preview")
    kb.adjust(2,2,2)
    return kb.as_markup()

@dp.message(CommandStart())
@dp.message(Command("menu"))
async def menu(m: Message):
    if not admin_only(m): return
    await m.answer("Управление прайсом", reply_markup=main_kb())

@dp.callback_query(F.data.startswith("src:"))
async def source_menu(c: CallbackQuery):
    if not admin_only(c): return
    source = c.data.split(":",1)[1]
    USER_STATE[c.from_user.id] = {"mode":"search_source", "source":source}
    await c.message.edit_text(
        f"{source}\n\nОтправь часть названия товара для поиска.\nНапример: <code>Galaxy Buds</code> или <code>Harman</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Меню", callback_data="menu")]])
    )
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
    if state.get("mode") == "search_source":
        source = state["source"]
        q = m.text.strip().lower()
        async with Session() as s:
            rows = (await s.scalars(select(Product).where(Product.source == source))).all()
        rows = [p for p in rows if q in p.name.lower()][:30]
        if not rows:
            await m.answer("Ничего не найдено. Попробуй другой запрос.")
            return
        kb = InlineKeyboardBuilder()
        for p in rows:
            mark = "✅" if p.enabled else "❌"
            kb.button(text=f"{mark} {p.name[:45]} — {p.price}", callback_data=f"toggle:{p.id}")
        kb.button(text="⬅️ Меню", callback_data="menu")
        kb.adjust(1)
        await m.answer("Найденные позиции. Нажми, чтобы включить/выключить:", reply_markup=kb.as_markup())
        return
    if state.get("mode") == "set_markup":
        try:
            v = parse_price(m.text)
            if v <= 0: raise ValueError
            await set_setting("default_markup", str(v))
            USER_STATE.pop(m.from_user.id, None)
            await m.answer(f"Наценка установлена: +{v:,}".replace(","," "), reply_markup=main_kb())
        except Exception:
            await m.answer("Пришли число, например 2500")
        return
    if state.get("mode") == "add_own_name":
        USER_STATE[m.from_user.id] = {"mode":"add_own_price", "name":m.text.strip()}
        await m.answer("Теперь пришли нашу продажную цену числом.")
        return
    if state.get("mode") == "add_own_price":
        price = parse_price(m.text)
        if not price:
            await m.answer("Не понял цену. Пример: 28500")
            return
        name = state["name"]
        canonical = canonicalize(name)
        async with Session() as s:
            existing = await s.scalar(select(Product).where(Product.source == "own", Product.canonical == canonical))
            if existing:
                existing.name = name; existing.price = price; existing.enabled = True
                existing.brand = detect_brand(name); existing.category = detect_category(name)
            else:
                s.add(Product(source="own", source_key=canonical, name=name, canonical=canonical,
                              brand=detect_brand(name), category=detect_category(name), price=price,
                              region=detect_region(name), enabled=True))
            await s.commit()
        USER_STATE.pop(m.from_user.id, None)
        await m.answer("Наш товар добавлен. Он будет иметь приоритет над поставщиками.", reply_markup=main_kb())

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
    USER_STATE[c.from_user.id] = {"mode":"add_own_name"}
    await c.message.edit_text("Пришли точное название нашего товара.")
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

@dp.callback_query(F.data.startswith("orddn:"))
async def orddn(c: CallbackQuery):
    await move_category(c.data.split(":",1)[1], 1)
    await order(c)

async def listen_supplier(session_string: str, source_chat: int, source_name: str, parser):
    if not session_string or not source_chat or not API_ID or not API_HASH:
        log.warning("%s disabled: missing session/chat/api settings", source_name)
        return
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    await client.start()
    log.info("%s connected", source_name)

    @client.on(events.NewMessage(chats=source_chat))
    async def handler(event):
        text = event.raw_text or ""
        parsed = parser(text)
        if parsed:
            await upsert_parsed(source_name, parsed)
            log.info("%s parsed %d items", source_name, len(parsed))

    await client.run_until_disconnected()

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await set_setting("default_markup", await get_setting("default_markup", str(DEFAULT_MARKUP)))

async def main():
    await init_db()
    tasks = [
        asyncio.create_task(dp.start_polling(bot)),
        asyncio.create_task(listen_supplier(SESSION_1, SOURCE_CHAT_1, "supplier1", parse_supplier1)),
        asyncio.create_task(listen_supplier(SESSION_2, SOURCE_CHAT_2, "supplier2", parse_supplier2)),
    ]
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())
