import os
import asyncio
import aiosqlite
from aiogram.client.default import DefaultBotProperties
from datetime import datetime, timedelta
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler

TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", f"https://{os.getenv('RENDER_INSTANCE_ID')}.onrender.com")

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
DB = "data.db"

async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript('''
            CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, trial_end TEXT);
            CREATE TABLE IF NOT EXISTS questions (id INTEGER PRIMARY KEY, from_user INT, to_user INT, text TEXT);
        ''')
        await db.commit()

class Ask(StatesGroup):
    username = State()
    question = State()

@dp.message(Command("start"))
async def start(m: types.Message):
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (m.from_user.id,))
        row = await (await db.execute("SELECT trial_end FROM users WHERE user_id=?", (m.from_user.id,))).fetchone()
        if not row or not row[0]:
            end = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
            await db.execute("UPDATE users SET trial_end=?", (end,))
        await db.commit()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Открыть приложение", web_app=WebAppInfo(url=f"{BASE_URL}/miniapp"))],
        [InlineKeyboardButton(text="Задать вопрос", callback_data="ask")]
    ])
    await m.answer("Анонимные вопросы\nПробный безлимит — 3 дня!\nПотом 5 вопросов в сутки.\nВсе функции в приложении ниже", reply_markup=kb)

@dp.callback_query(lambda c: c.data == "ask")
async def ask_cb(c: types.CallbackQuery, state: FSMContext):
    await c.message.edit_text("Напиши username (с @ или без):")
    await state.set_state(Ask.username)

@dp.message(Ask.username)
async def get_username(m: types.Message, state: FSMContext):
    username = m.text.lstrip("@").lower()
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id FROM users WHERE LOWER(username)=?", (username,))
        row = await cur.fetchone()
    if not row:
        await m.answer("Пользователь ещё не запускал бота")
        return
    await state.update_data(to_id=row[0])
    await m.answer("Напиши вопрос:")
    await state.set_state(Ask.question)

@dp.message(Ask.question)
async def get_question(m: types.Message, state: FSMContext):
    data = await state.get_data()
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO questions (from_user, to_user, text) VALUES (?, ?, ?)",
                        (m.from_user.id, data["to_id"], m.text))
        await db.commit()
    await bot.send_message(data["to_id"], f"Новый анонимный вопрос:\n\n{m.text}")
    await m.answer("Вопрос отправлен анонимно!")
    await state.clear()

async def miniapp_handler(request):
    return web.Response(text='''
<!DOCTYPE html><html><head><script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>body{font-family:Arial;padding:20px;background:var(--tg-theme-bg-color);color:var(--tg-theme-text-color);}
button{width:100%;padding:15px;margin:10px 0;background:var(--tg-theme-button-color);color:var(--tg-theme-button-text-color);border:none;border-radius:12px;font-size:18px;}</style>
</head><body>
<h1>Анонимные вопросы</h1>
<p>Пробный период: 3 дня безлимита</p>
<button onclick="Telegram.WebApp.close()">Закрыть</button>
<script>Telegram.WebApp.ready(); Telegram.WebApp.expand();</script>
</body></html>
    ''', content_type='text/html')

async def on_startup(_):
    await init_db()
    await bot.set_webhook(f"{BASE_URL}/webhook")
    print("Бот запущен!")

app = web.Application()
app.router.add_get("/miniapp", miniapp_handler)
SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
app.on_startup.append(on_startup)

if __name__ == "__main__":
    web.run_app(app, port=10000)
