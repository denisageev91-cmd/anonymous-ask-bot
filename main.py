import os
import aiosqlite
from datetime import datetime, timedelta
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.webhook.aiohttp_server import SimpleRequestHandler

# === НАСТРОЙКИ ===
TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", f"https://{os.getenv('RENDER_INSTANCE_ID')}.onrender.com")

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
DB = "data.db"

# === БАЗА ===
async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                trial_end TEXT
            );
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_user INT,
                to_user INT,
                text TEXT,
                answer TEXT,
                answered INTEGER DEFAULT 0
            );
        ''')
        await db.commit()

# === СОСТОЯНИЯ ===
class Ask(StatesGroup):
    username = State()
    question = State()

# === КНОПКА ВСЕГДА ===
def get_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Личный кабинет", web_app=WebAppInfo(url=f"{BASE_URL}/miniapp"))],
        [InlineKeyboardButton(text="Задать вопрос", callback_data="ask")]
    ])

# === СТАРТ ===
@dp.message(Command("start"))
async def start(m: types.Message):
    async with aiosqlite.connect(DB) as db:
        username = (m.from_user.username or "").lstrip("@").lower()
        await db.execute(
            "INSERT INTO users (user_id, username) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
            (m.from_user.id, username)
        )
        row = await (await db.execute("SELECT trial_end FROM users WHERE user_id=?", (m.from_user.id,))).fetchone()
        if not row or not row[0]:
            end = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
            await db.execute("UPDATE users SET trial_end=? WHERE user_id=?", (end, m.from_user.id))
        await db.commit()

    await m.answer(
        "Анонимные вопросы\n\n"
        "Пробный безлимит — 3 дня\n"
        "Отвечай прямо на сообщение — ответ уйдёт анонимно",
        reply_markup=get_main_keyboard()
    )

# === ЗАДАТЬ ВОПРОС ===
@dp.callback_query(lambda c: c.data == "ask")
async def ask_cb(c: types.CallbackQuery, state: FSMContext):
    await c.message.edit_text("Напиши username (с @ или без):", reply_markup=None)
    await state.set_state(Ask.username)

@dp.message(Ask.username)
async def get_username(m: types.Message, state: FSMContext):
    username = m.text.lstrip("@").lower()
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id FROM users WHERE LOWER(username)=?", (username,))
        row = await cur.fetchone()
    if not row:
        await m.answer("Этот пользователь ещё не запускал бота", reply_markup=get_main_keyboard())
        await state.clear()
        return
    await state.update_data(to_id=row[0])
    await m.answer("Напиши вопрос:", reply_markup=None)
    await state.set_state(Ask.question)

@dp.message(Ask.question)
async def get_question(m: types.Message, state: FSMContext):
    data = await state.get_data()
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO questions (from_user, to_user, text) VALUES (?, ?, ?)",
                        (m.from_user.id, data["to_id"], m.text))
        await db.commit()

    await bot.send_message(data["to_id"],
        f"Новый анонимный вопрос:\n\n{m.text}\n\n"
        "Ответь на это сообщение — ответ уйдёт анонимно"
    )

    # Кнопка "Задать ещё один"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Задать ещё один вопрос", callback_data="ask")],
        [InlineKeyboardButton(text="Личный кабинет", web_app=WebAppInfo(url=f"{BASE_URL}/miniapp"))]
    ])
    await m.answer("Вопрос отправлен анонимно", reply_markup=kb)
    await state.clear()

# === ОТВЕТ НА ВОПРОС ===
@dp.message()
async def answer_to_question(m: types.Message):
    if m.reply_to_message and "Новый анонимный вопрос" in m.reply_to_message.text:
        question_text = m.reply_to_message.text.split("\n\n")[1].split("\n\n")[0]
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute(
                "SELECT from_user FROM questions WHERE to_user=? AND text=? AND answered=0",
                (m.from_user.id, question_text)
            )
            row = await cur.fetchone()
            if row:
                from_user = row[0]
                await db.execute(
                    "UPDATE questions SET answer=?, answered=1 WHERE from_user=? AND to_user=? AND text=?",
                    (m.text, from_user, m.from_user.id, question_text)
                )
                await db.commit()
                await bot.send_message(from_user, f"Тебе ответили анонимно:\n\n{m.text}")
                await m.answer("Ответ отправлен анонимно", reply_markup=get_main_keyboard())
                return

# === MINI APP — ЛИЧНЫЙ КАБИНЕТ СО СЧЁТЧИКАМИ ===
async def miniapp_handler(request):
    user_id = request.query.get("user_id")
    if not user_id:
        return web.Response(text="Ошибка", status=400)

    async with aiosqlite.connect(DB) as db:
        sent = (await (await db.execute("SELECT COUNT(*) FROM questions WHERE from_user=?", (user_id,))).fetchone())[0]
        received = (await (await db.execute("SELECT COUNT(*) FROM questions WHERE to_user=?", (user_id,))).fetchone())[0]
        answered = (await (await db.execute("SELECT COUNT(*) FROM questions WHERE to_user=? AND answered=1", (user_id,))).fetchone())[0]
        pending = received - answered

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <style>
            body {{font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                  padding: 20px; background: var(--tg-theme-bg-color); color: var(--tg-theme-text-color); text-align: center;}}
            h1 {{margin-bottom: 30px;}}
            .stats {{display: grid; gap: 16px;}}
            .card {{background: var(--tg-theme-secondary-bg-color); border-radius: 16px; padding: 20px;}}
            .num {{font-size: 48px; font-weight: 800; margin: 8px 0; color: var(--tg-theme-accent-text-color);}}
            .label {{font-size: 16px; opacity: 0.9;}}
            button {{margin-top: 30px; padding: 16px; width: 90%; background: var(--tg-theme-button-color);
                    color: var(--tg-theme-button-text-color); border: none; border-radius: 16px; font-size: 18px;}}
        </style>
    </head>
    <body>
        <h1>Личный кабинет</h1>
        <div class="stats">
            <div class="card">
                <div class="num">{sent}</div>
                <div class="label">Отправлено вопросов</div>
            </div>
            <div class="card">
                <div class="num">{received}</div>
                <div class="label">Получено вопросов</div>
            </div>
            <div class="card">
                <div class="num">{answered}</div>
                <div class="label">Отвечено</div>
            </div>
            <div class="card">
                <div class="num" style="color:#e74c3c;">{pending}</div>
                <div class="label">Ждут ответа</div>
            </div>
        </div>
        <button onclick="Telegram.WebApp.close()">Закрыть</button>
        <script>Telegram.WebApp.ready(); Telegram.WebApp.expand();</script>
    </body>
    </html>
    """
    return web.Response(text=html, content_type='text/html')

# === ЗАПУСК ===
async def on_startup(_):
    await init_db()
    await bot.set_webhook(f"{BASE_URL}/webhook")
    print("Бот запущен и готов к бою!")

app = web.Application()
app.router.add_get("/miniapp", miniapp_handler)
SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
app.on_startup.append(on_startup)

if __name__ == "__main__":
    web.run_app(app, port=10000)
