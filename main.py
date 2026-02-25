import os
import asyncio
import asyncpg
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
pool = None

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS users (
  tg_id BIGINT PRIMARY KEY
);
"""

async def get_pool():
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
    return pool

@dp.message(Command("start"))
async def start(m: Message):
    p = await get_pool()
    await p.execute(CREATE_SQL)
    await p.execute("INSERT INTO users (tg_id) VALUES ($1) ON CONFLICT DO NOTHING", m.from_user.id)
    await m.answer("✅ Render: бот запущен и база подключена!")

@dp.message(Command("ping"))
async def ping(m: Message):
    await m.answer("pong ✅")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
