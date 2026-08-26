# PriceBot

Telegram price aggregator for two supplier accounts.

## What it does
- reads supplier price messages from two Telegram user accounts (Telethon)
- parses both known supplier formats
- stores all products, but publishes only manually enabled products per source
- merges duplicate products by normalized product key
- own products have priority over supplier products
- if an own product is deleted, supplier fallback becomes active again
- applies a configurable default markup
- groups output by product category and then brand
- category groups can be moved up/down in the control bot
- supports preview generation

## Important Telegram limitation
Telegram message order cannot be changed by editing existing messages. Reordering physical channel messages requires resending them. This build keeps ordering in the database and uses it for preview/rebuild logic. A protected guarantee message is never edited by the bot.

## Setup
1. Copy `.env.example` to `.env`.
2. Fill BOT_TOKEN, ADMIN_ID, API_ID, API_HASH, SESSION_1, SESSION_2, SOURCE_CHAT_1, SOURCE_CHAT_2.
3. `pip install -r requirements.txt`
4. `python main.py`

## Railway
Set the same values as Railway Variables. For persistent SQLite, mount a volume and point DATABASE_URL to it, for example:
`sqlite+aiosqlite:////data/pricebot.db`
