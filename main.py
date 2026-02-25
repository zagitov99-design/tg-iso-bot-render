import os
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import asyncpg
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder


BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
DEFAULT_TZ = os.environ.get("TZ", "Europe/Berlin")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

pool: asyncpg.Pool | None = None

# --- DB schema ---
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS users (
  tg_id BIGINT PRIMARY KEY,
  tz TEXT NOT NULL DEFAULT 'Europe/Berlin',
  reminders_enabled BOOLEAN NOT NULL DEFAULT TRUE,
  remind1 TEXT NOT NULL DEFAULT '09:00',
  remind2 TEXT NOT NULL DEFAULT '21:00',
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Каждое напоминание создаёт запись (planned_at + slot), дальше она превращается в taken/skip
CREATE TABLE IF NOT EXISTS intakes (
  id SERIAL PRIMARY KEY,
  tg_id BIGINT NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
  planned_at TIMESTAMPTZ NOT NULL,
  slot SMALLINT NOT NULL,              -- 1 или 2 (какое из двух напоминаний)
  status TEXT NOT NULL DEFAULT 'sent', -- sent/taken/skip
  snoozed_until TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Очередь “отложенных” повторов
CREATE TABLE IF NOT EXISTS pending_jobs (
  id SERIAL PRIMARY KEY,
  tg_id BIGINT NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
  intake_id INT NOT NULL REFERENCES intakes(id) ON DELETE CASCADE,
  run_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pending_jobs_run_at ON pending_jobs(run_at);
CREATE INDEX IF NOT EXISTS idx_intakes_tg_planned ON intakes(tg_id, planned_at);
"""

# --- In-memory states for setting times / calc ---
@dataclass
class UserState:
  mode: str  # idle | set_time1 | set_time2 | calc

states: dict[int, UserState] = {}

# --- helpers ---
async def get_pool() -> asyncpg.Pool:
  global pool
  if pool is None:
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
  return pool

async def ensure_user(tg_id: int):
  p = await get_pool()
  await p.execute(
    "INSERT INTO users (tg_id, tz) VALUES ($1, $2) ON CONFLICT (tg_id) DO NOTHING",
    tg_id, DEFAULT_TZ
  )

def parse_hhmm(s: str) -> str:
  s = s.strip()
  hh, mm = s.split(":")
  h = int(hh); m = int(mm)
  if not (0 <= h <= 23 and 0 <= m <= 59):
    raise ValueError("bad time")
  return f"{h:02d}:{m:02d}"

def now_in_tz(tz_name: str) -> datetime:
  return datetime.now(ZoneInfo(tz_name))

def today_slot_dt(tz_name: str, hhmm: str) -> datetime:
  # planned_at в tz, но вернём aware datetime (TIMESTAMPTZ)
  t = parse_hhmm(hhmm)
  h, m = map(int, t.split(":"))
  now = now_in_tz(tz_name)
  return now.replace(hour=h, minute=m, second=0, microsecond=0)

def kb_main():
  kb = InlineKeyboardBuilder()
  kb.button(text="🧮 Калькулятор", callback_data="m:calc")
  kb.button(text="⏰ Напоминания", callback_data="m:rem")
  kb.button(text="📊 Журнал", callback_data="m:journal")
  kb.adjust(2, 1)
  return kb.as_markup()

def kb_reminders(enabled: bool):
  kb = InlineKeyboardBuilder()
  kb.button(text=("🔔 Включены" if enabled else "🔕 Выключены"), callback_data="r:toggle")
  kb.button(text="🕘 Время #1", callback_data="r:set1")
  kb.button(text="🕘 Время #2", callback_data="r:set2")
  kb.button(text="⬅️ Назад", callback_data="m:back")
  kb.adjust(1, 2, 1)
  return kb.as_markup()

def kb_intake_actions(intake_id: int):
  kb = InlineKeyboardBuilder()
  kb.button(text="✅ Принял", callback_data=f"a:taken:{intake_id}")
  kb.button(text="❌ Пропустил", callback_data=f"a:skip:{intake_id}")
  kb.button(text="⏰ +10м", callback_data=f"a:snooze:{intake_id}:10")
  kb.button(text="⏰ +30м", callback_data=f"a:snooze:{intake_id}:30")
  kb.button(text="⏰ +60м", callback_data=f"a:snooze:{intake_id}:60")
  kb.adjust(2, 3)
  return kb.as_markup()

async def get_user(tg_id: int) -> dict:
  await ensure_user(tg_id)
  p = await get_pool()
  row = await p.fetchrow("SELECT * FROM users WHERE tg_id=$1", tg_id)
  return dict(row)

async def set_user_time(tg_id: int, which: int, hhmm: str):
  hhmm = parse_hhmm(hhmm)
  p = await get_pool()
  col = "remind1" if which == 1 else "remind2"
  await p.execute(f"UPDATE users SET {col}=$1 WHERE tg_id=$2", hhmm, tg_id)

async def toggle_reminders(tg_id: int):
  p = await get_pool()
  await p.execute(
    "UPDATE users SET reminders_enabled = NOT reminders_enabled WHERE tg_id=$1",
    tg_id
  )

# --- Reminder engine ---
async def create_intake_if_needed(tg_id: int, tz: str, slot: int, hhmm: str) -> int | None:
  """
  Создаёт intake запись только если сегодня для этого слота ещё не создавали.
  Возвращает intake_id или None.
  """
  planned = today_slot_dt(tz, hhmm)

  p = await get_pool()
  # Проверим, есть ли intake за этот день и слот (по planned_at date в tz).
  # Упростим: сравним по диапазону суток в tz.
  start = planned.replace(hour=0, minute=0, second=0, microsecond=0)
  end = start + timedelta(days=1)

  existing = await p.fetchval(
    """
    SELECT id FROM intakes
    WHERE tg_id=$1 AND slot=$2 AND planned_at >= $3 AND planned_at < $4
    LIMIT 1
    """,
    tg_id, slot, start, end
  )
  if existing:
    return None

  intake_id = await p.fetchval(
    "INSERT INTO intakes (tg_id, planned_at, slot) VALUES ($1, $2, $3) RETURNING id",
    tg_id, planned, slot
  )
  return int(intake_id)

async def send_intake_reminder(tg_id: int, intake_id: int, slot: int):
  await bot.send_message(
    tg_id,
    f"⏰ Напоминание #{slot}: время приёма.\nОтметь действие:",
    reply_markup=kb_intake_actions(intake_id)
  )

async def scheduler_tick():
  """
  Каждые 30 секунд:
  1) если настало время remind1/remind2 — создаём intake и шлём напоминание
  2) если есть pending_jobs, у которых run_at <= now — шлём повтор и удаляем job
  """
  p = await get_pool()
  users = await p.fetch("SELECT tg_id, tz, reminders_enabled, remind1, remind2 FROM users")

  # 2) pending jobs
  # Берём немного, чтобы не упереться в лимиты
  pending = await p.fetch(
    "SELECT id, tg_id, intake_id FROM pending_jobs WHERE run_at <= NOW() ORDER BY run_at ASC LIMIT 100"
  )
  for job in pending:
    jid = job["id"]
    tg_id = job["tg_id"]
    intake_id = job["intake_id"]

    # если intake уже закрыт — просто удаляем job
    status = await p.fetchval("SELECT status FROM intakes WHERE id=$1", intake_id)
    if status in ("taken", "skip"):
      await p.execute("DELETE FROM pending_jobs WHERE id=$1", jid)
      continue

    # узнаем slot
    slot = await p.fetchval("SELECT slot FROM intakes WHERE id=$1", intake_id)
    try:
      await send_intake_reminder(tg_id, intake_id, int(slot))
    finally:
      await p.execute("DELETE FROM pending_jobs WHERE id=$1", jid)

  # 1) time-based reminders
  for u in users:
    if not u["reminders_enabled"]:
      continue

    tg_id = int(u["tg_id"])
    tz = u["tz"] or DEFAULT_TZ
    now = now_in_tz(tz)

    # Если сейчас ровно HH:MM (в пределах первых 30 секунд минуты) — шлём
    for slot, hhmm in [(1, u["remind1"]), (2, u["remind2"])]:
      try:
        hhmm = parse_hhmm(hhmm)
      except Exception:
        continue

      h, m = map(int, hhmm.split(":"))
      if now.hour == h and now.minute == m and now.second < 30:
        intake_id = await create_intake_if_needed(tg_id, tz, slot, hhmm)
        if intake_id:
          await send_intake_reminder(tg_id, intake_id, slot)

# --- handlers ---
@dp.message(Command("start"))
async def cmd_start(m: Message):
  await ensure_user(m.from_user.id)
  states[m.from_user.id] = UserState(mode="idle")
  u = await get_user(m.from_user.id)
  await m.answer(
    "✅ Бот работает.\n\n"
    f"⏰ Напоминания: {'включены' if u['reminders_enabled'] else 'выключены'}\n"
    f"• Время #1: {u['remind1']}\n"
    f"• Время #2: {u['remind2']}\n\n"
    "Выбери действие:",
    reply_markup=kb_main()
  )

@dp.message(Command("menu"))
async def cmd_menu(m: Message):
  await cmd_start(m)

@dp.callback_query(F.data == "m:back")
async def cb_back(q: CallbackQuery):
  await q.answer()
  await cmd_start(Message.model_validate(q.message.model_dump()))

@dp.callback_query(F.data == "m:calc")
async def cb_calc(q: CallbackQuery):
  await q.answer()
  states[q.from_user.id] = UserState(mode="calc")
  await bot.send_message(
    q.message.chat.id,
    "🧮 Калькулятор\n"
    "Отправь одной строкой:\n"
    "`вес_кг мг_на_кг_в_день цель_мг_на_кг`\n"
    "Пример: `70 0.5 120`",
    parse_mode="Markdown"
  )

@dp.callback_query(F.data == "m:rem")
async def cb_rem(q: CallbackQuery):
  await q.answer()
  u = await get_user(q.from_user.id)
  await bot.send_message(
    q.message.chat.id,
    f"⏰ Напоминания\n"
    f"Сейчас: {'включены' if u['reminders_enabled'] else 'выключены'}\n"
    f"• #1: {u['remind1']}\n"
    f"• #2: {u['remind2']}\n\n"
    "Настрой:",
    reply_markup=kb_reminders(bool(u["reminders_enabled"]))
  )

@dp.callback_query(F.data == "m:journal")
async def cb_journal(q: CallbackQuery):
  await q.answer()
  await ensure_user(q.from_user.id)
  p = await get_pool()

  # последние 7 дней (taken/skip)
  rows = await p.fetch(
    """
    SELECT status, COUNT(*) AS c
    FROM intakes
    WHERE tg_id=$1
      AND created_at >= NOW() - INTERVAL '7 days'
      AND status IN ('taken','skip')
    GROUP BY status
    """,
    q.from_user.id
  )
  taken = 0
  skip = 0
  for r in rows:
    if r["status"] == "taken":
      taken = int(r["c"])
    elif r["status"] == "skip":
      skip = int(r["c"])

  total = taken + skip
  adherence = (taken / total * 100.0) if total else 0.0

  last = await p.fetchrow(
    "SELECT status, updated_at FROM intakes WHERE tg_id=$1 ORDER BY updated_at DESC LIMIT 1",
    q.from_user.id
  )
  last_line = "нет"
  if last:
    last_line = f"{last['status']} • {last['updated_at']}"

  await bot.send_message(
    q.message.chat.id,
    "📊 Журнал (7 дней)\n"
    f"✅ Принял: {taken}\n"
    f"❌ Пропустил: {skip}\n"
    f"📈 Соблюдение: {adherence:.0f}%\n"
    f"🕒 Последняя отметка: {last_line}",
    reply_markup=kb_main()
  )

@dp.callback_query(F.data == "r:toggle")
async def cb_toggle(q: CallbackQuery):
  await q.answer()
  await toggle_reminders(q.from_user.id)
  u = await get_user(q.from_user.id)
  await bot.send_message(
    q.message.chat.id,
    f"Готово. Напоминания теперь: {'включены' if u['reminders_enabled'] else 'выключены'}",
    reply_markup=kb_reminders(bool(u["reminders_enabled"]))
  )

@dp.callback_query(F.data == "r:set1")
async def cb_set1(q: CallbackQuery):
  await q.answer()
  states[q.from_user.id] = UserState(mode="set_time1")
  await bot.send_message(q.message.chat.id, "Введи время #1 в формате HH:MM (например 09:00)")

@dp.callback_query(F.data == "r:set2")
async def cb_set2(q: CallbackQuery):
  await q.answer()
  states[q.from_user.id] = UserState(mode="set_time2")
  await bot.send_message(q.message.chat.id, "Введи время #2 в формате HH:MM (например 21:00)")

@dp.callback_query(F.data.startswith("a:taken:"))
async def cb_taken(q: CallbackQuery):
  await q.answer("✅")
  intake_id = int(q.data.split(":")[2])
  p = await get_pool()
  await p.execute(
    "UPDATE intakes SET status='taken', updated_at=NOW(), snoozed_until=NULL WHERE id=$1 AND tg_id=$2",
    intake_id, q.from_user.id
  )
  await bot.send_message(q.message.chat.id, "✅ Отметил: принял.")

@dp.callback_query(F.data.startswith("a:skip:"))
async def cb_skip(q: CallbackQuery):
  await q.answer("Ок")
  intake_id = int(q.data.split(":")[2])
  p = await get_pool()
  await p.execute(
    "UPDATE intakes SET status='skip', updated_at=NOW(), snoozed_until=NULL WHERE id=$1 AND tg_id=$2",
    intake_id, q.from_user.id
  )
  await bot.send_message(q.message.chat.id, "❌ Отметил: пропуск.")

@dp.callback_query(F.data.startswith("a:snooze:"))
async def cb_snooze(q: CallbackQuery):
  parts = q.data.split(":")
  intake_id = int(parts[2])
  minutes = int(parts[3])
  await q.answer(f"+{minutes}м")

  p = await get_pool()
  # если уже закрыто — не откладываем
  status = await p.fetchval("SELECT status FROM intakes WHERE id=$1 AND tg_id=$2", intake_id, q.from_user.id)
  if status in ("taken", "skip"):
    await bot.send_message(q.message.chat.id, "Уже отмечено, откладывать не нужно.")
    return

  run_at = datetime.now(tz=ZoneInfo("UTC")) + timedelta(minutes=minutes)
  await p.execute("UPDATE intakes SET snoozed_until=$1, updated_at=NOW() WHERE id=$2 AND tg_id=$3", run_at, intake_id, q.from_user.id)
  await p.execute(
    "INSERT INTO pending_jobs (tg_id, intake_id, run_at) VALUES ($1, $2, $3)",
    q.from_user.id, intake_id, run_at
  )
  await bot.send_message(q.message.chat.id, f"⏰ Ок, напомню через {minutes} минут.")

@dp.message(F.text)
async def on_text(m: Message):
  st = states.get(m.from_user.id, UserState(mode="idle"))

  if st.mode == "calc":
    try:
      parts = m.text.replace(",", ".").split()
      if len(parts) != 3:
        raise ValueError("need 3 numbers")
      w, d, c = map(float, parts)
      if w <= 0 or d <= 0 or c <= 0:
        raise ValueError("bad values")
      daily = w * d
      target = w * c
      days = int((target / daily) + 0.999999)
      await m.answer(
        f"🧮 Дневная доза: {daily:.1f} мг\n"
        f"🎯 Целевая: {target:.0f} мг\n"
        f"⏳ Примерно дней: {days}"
      )
      states[m.from_user.id] = UserState(mode="idle")
    except Exception:
      await m.answer("Формат: `70 0.5 120`", parse_mode="Markdown")
    return

  if st.mode in ("set_time1", "set_time2"):
    try:
      hhmm = parse_hhmm(m.text)
      which = 1 if st.mode == "set_time1" else 2
      await set_user_time(m.from_user.id, which, hhmm)
      states[m.from_user.id] = UserState(mode="idle")
      u = await get_user(m.from_user.id)
      await m.answer(
        f"✅ Сохранено.\n"
        f"• #1: {u['remind1']}\n"
        f"• #2: {u['remind2']}"
      )
    except Exception:
      await m.answer("Ошибка. Введи время HH:MM, например 09:00")
    return

  # дефолт
  await m.answer("Открой меню: /menu", reply_markup=kb_main())

async def on_startup():
  p = await get_pool()
  await p.execute(CREATE_SQL)

  # --- simple migrations (safe if columns already exist) ---
  await p.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS tz TEXT NOT NULL DEFAULT 'Europe/Berlin'")
  await p.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS reminders_enabled BOOLEAN NOT NULL DEFAULT TRUE")
  await p.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS remind1 TEXT NOT NULL DEFAULT '09:00'")
  await p.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS remind2 TEXT NOT NULL DEFAULT '21:00'")
  await p.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT NOW()")

async def main():
  await on_startup()
  scheduler.add_job(scheduler_tick, "interval", seconds=30)
  scheduler.start()
  await dp.start_polling(bot)

if __name__ == "__main__":
  asyncio.run(main())
