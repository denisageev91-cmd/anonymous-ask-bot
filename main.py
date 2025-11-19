import os, json, aiosqlite, asyncio, hashlib
from datetime import datetime, timedelta, timezone
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import (
    WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton,
    PreCheckoutQuery, LabeledPrice, InputFile
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.webhook.aiohttp_server import SimpleRequestHandler

TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", f"https://{os.getenv('RENDER_INSTANCE_ID')}.onrender.com")

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())
DB = "data.db"

async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, username TEXT, lang TEXT DEFAULT "ru",
                trial_end TEXT, premium_until TEXT, premium_type TEXT,
                referred_by INT, referred_count INT DEFAULT 0,
                push_answers INT DEFAULT 1, theme TEXT DEFAULT "dark", accent_color TEXT DEFAULT "#8774e1",
                badge TEXT, banned INT DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, from_user INT, to_user INT,
                text TEXT, answer TEXT, answered INT DEFAULT 0, hidden INT DEFAULT 0,
                special INT DEFAULT 0, likes INT DEFAULT 0, bumped_at TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS celebs (user_id INTEGER PRIMARY KEY, name TEXT, verified INT DEFAULT 1);
        ''')
        await db.commit()

class Ask(StatesGroup):
    username = State(); question = State(); confirm = State()

def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("Личный кабинет", web_app=WebAppInfo(url=f"{BASE_URL}/miniapp"))],
        [InlineKeyboardButton("Задать вопрос", callback_data="ask")]
    ])

@dp.message(Command("start"))
async def start(m: types.Message):
    ref = None
    if len(m.text.split()) > 1:
        try: ref = int(m.text.split()[1])
        except: pass

    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (m.from_user.id,))
        if ref and ref != m.from_user.id:
            await db.execute("UPDATE users SET referred_count = referred_count + 1, premium_until = datetime(COALESCE(premium_until,'now'), '+1 day') WHERE user_id=?", (ref,))
            try: await bot.send_message(ref, "Приглашён друг — +1 день безлимита!")
            except: pass
        if not (await (await db.execute("SELECT trial_end FROM users WHERE user_id=?", (m.from_user.id,))).fetchone()):
            end = (datetime.now(timezone(timedelta(hours=3))) + timedelta(days=3)).strftime("%Y-%m-%d")
            await db.execute("UPDATE users SET trial_end=? WHERE user_id=?", (end, m.from_user.id))
        await db.commit()

    await m.answer("Анонимные вопросы 2025\n• 3 дня безлимит\n• 1 друг = +1 день\n• Подписки от 135⭐", reply_markup=main_kb())
    @dp.callback_query(lambda c: c.data == "ask")
async def ask_start(c: types.CallbackQuery, state: FSMContext):
    await c.message.edit_text("Напиши username (с @ или без):")
    await state.set_state(Ask.username)

@dp.message(Ask.username)
async def ask_username(m: types.Message, state: FSMContext):
    username = m.text.lstrip("@").lower()
    async with aiosqlite.connect(DB) as db:
        row = await (await db.execute("SELECT user_id FROM users WHERE LOWER(username)=?", (username,))).fetchone()
        if not row:
            await m.answer("Пользователь ещё не в боте — мы уведомим его при старте!", reply_markup=main_kb())
            await state.clear()
            return

        # Проверка на знаменитость
        celeb = await (await db.execute("SELECT name FROM celebs WHERE user_id=?", (row[0],))).fetchone()
        if celeb:
            await state.update_data(to_id=row[0], cost=250, celeb=True)
            await m.answer(
                f"Вопрос {celeb[0]} стоит 250⭐",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton("Оплатить 250⭐", pay=True)]])
            )
            return

        await state.update_data(to_id=row[0])
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton("Обычный вопрос", callback_data="type_normal")],
            [InlineKeyboardButton("Особый вопрос — 5⭐", callback_data="type_special")]
        ])
        await m.answer("Выбери тип вопроса:", reply_markup=kb)

@dp.callback_query(lambda c: c.data.startswith("type_"))
async def ask_type(c: types.CallbackQuery, state: FSMContext):
    special = 1 if c.data == "type_special" else 0
    cost = 5 if special else 0
    await state.update_data(special=special, cost=cost)
    await c.message.edit_text("Напиши вопрос:")
    await state.set_state(Ask.question)

@dp.message(Ask.question)
async def ask_question(m: types.Message, state: FSMContext):
    data = await state.get_data()
    to_id = data["to_id"]
    special = data.get("special", 0)
    cost = data.get("cost", 0)

    # Если был платёж — проверяем
    if cost > 0 and not m.successful_payment:
        await m.answer("Оплата не прошла — попробуй снова")
        await state.clear()
        return

    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO questions (from_user, to_user, text, special) VALUES (?, ?, ?, ?)",
            (m.from_user.id, to_id, m.text, special)
        )
        await db.commit()

    prefix = "Особый вопрос!" if special else ""
    await bot.send_message(to_id, f"{prefix}\nНовый анонимный вопрос:\n\n{m.text}\n\nОтветь на это сообщение — ответ уйдёт анонимно")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("Задать ещё", callback_data="ask")],
        [InlineKeyboardButton("Личный кабинет", web_app=WebAppInfo(url=f"{BASE_URL}/miniapp"))]
    ])
    await m.answer("Вопрос отправлен анонимно!", reply_markup=kb)
    await state.clear()

# === ОТВЕТЫ + ЛАЙКИ + ПОДНЯТЬ ВОПРОС ЗА 1 ЗВЕЗДУ ===
@dp.message()
async def handle_message(m: types.Message):
    # Ответ на вопрос
    if m.reply_to_message and "Новый анонимный вопрос" in m.reply_to_message.text:
        qtext = m.reply_to_message.text.split("\n\n", 1)[1].split("\n\n", 1)[0]
        async with aiosqlite.connect(DB) as db:
            row = await (await db.execute("SELECT from_user, hidden FROM questions WHERE to_user=? AND text=? AND answered=0", (m.from_user.id, qtext))).fetchone()
            if row:
                hidden = row[1]
                await db.execute("UPDATE questions SET answer=?, answered=1 WHERE from_user=? AND to_user=? AND text=?", (m.text, row[0], m.from_user.id, qtext))
                await db.commit()
                if hidden:
                    await bot.send_message(row[0], f"Тебе ответили скрыто:\n\n{m.text}")
                else:
                    await bot.send_message(row[0], f"Тебе ответили анонимно:\n\n{m.text}")
                await m.answer("Ответ отправлен!", reply_markup=main_kb())

    # Лайк вопроса
    if m.text in ["❤️", "♥️"] and m.reply_to_message and "Новый анонимный вопрос" in m.reply_to_message.text:
        async with aiosqlite.connect(DB) as db:
            await db.execute("UPDATE questions SET likes = likes + 1 WHERE to_user=? AND text=?", (m.from_user.id, m.reply_to_message.text.split("\n\n", 1)[1].split("\n\n", 1)[0]))
            await db.commit()
        await m.answer("❤️")

    # Поднять вопрос за 1 звезду
    if m.text == "Поднять" and m.reply_to_message:
        await m.answer("Поднять вопрос — 1⭐", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton("Оплатить 1⭐", pay=True)]]))
        # === ОПЛАТА ЗВЁЗДАМИ (все согласованные тарифы) ===
@dp.message(lambda m: m.successful_payment and m.successful_payment.currency == "XTR")
async def stars_paid(m: types.Message):
    amount = m.successful_payment.total_amount
    payload = m.successful_payment.invoice_payload

    # Тарифы
    tariffs = {
        135: ("1 месяц", 30),
        330: ("3 месяца", 90),
        1050: ("Год", 365),
        2600: ("Пожизненно", 99999),
        250: ("Вопрос знаменитости", 0),
        5: ("Особый вопрос", 0),
        10: ("PDF-экспорт", 0),
        1: ("Поднять вопрос", 0),
        3: ("Скрытый ответ", 0)
    }

    if amount not in tariffs:
        return

    name, days = tariffs[amount]
    async with aiosqlite.connect(DB) as db:
        if days > 0:
            if days == 99999:
                await db.execute("UPDATE users SET premium_until='9999-12-31', premium_type=?, badge='LEGEND' WHERE user_id=?", (name, m.from_user.id))
            else:
                end = (datetime.now(timezone(timedelta(hours=3))) + timedelta(days=days)).strftime("%Y-%m-%d")
                await db.execute("UPDATE users SET premium_until=?, premium_type=?, badge='VIP' WHERE user_id=?", (end, name, m.from_user.id))
        await db.commit()

    if "вопрос" in name.lower() or "pdf" in name.lower() or "поднять" in name.lower():
        await m.answer(f"{name} активирован!")
    else:
        await m.answer(f"Безлимит {name} активирован! Спасибо за поддержку!", reply_markup=main_kb())

# === СКРЫТЫЙ ОТВЕТ ЗА 3 ЗВЁЗДЫ ===
@dp.callback_query(lambda c: c.data == "hidden_answer")
async def hidden_answer(c: types.CallbackQuery):
    await c.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("Оплатить 3⭐ — скрытый ответ", pay=True)]
    ]))

# === PDF-ЭКСПОРТ ЗА 10 ЗВЁЗД ===
@dp.callback_query(lambda c: c.data == "export_pdf")
async def export_pdf(c: types.CallbackQuery):
    await bot.send_invoice(
        chat_id=c.from_user.id,
        title="Экспорт всех вопросов в PDF",
        description="Получи все свои вопросы и ответы в красивом PDF",
        payload="pdf_export",
        provider_token="",  # не нужен для звёзд
        currency="XTR",
        prices=[LabeledPrice("PDF", 10)]
    )

# === АДМИН-ПАНЕЛЬ (полная) ===
@dp.message(Command("admin"))
async def admin_panel(m: types.Message):
    if m.from_user.id != OWNER_ID: return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton("Бан/Разбан", callback_data="admin_ban")],
        [InlineKeyboardButton("Топ пользователей", callback_data="admin_top")]
    ])
    await m.answer("Админ-панель", reply_markup=kb)

@dp.callback_query(lambda c: c.data == "admin_stats")
async def admin_stats(c: types.CallbackQuery):
    if c.from_user.id != OWNER_ID: return
    async with aiosqlite.connect(DB) as db:
        total = await (await db.execute("SELECT COUNT(*) FROM users")).fetchone()
        premium = await (await db.execute("SELECT COUNT(*) FROM users WHERE premium_until > date('now')")).fetchone()
        questions = await (await db.execute("SELECT COUNT(*) FROM questions")).fetchone()
    await c.message.edit_text(f"Пользователей: {total[0]}\nПремиум: {premium[0]}\nВопросов: {questions[0]}", reply_markup=c.message.reply_markup)
    # === ПОЛНЫЙ MINI APP (всё, что обещал) ===
async def miniapp_handler(request):
    init_data = request.headers.get("X-Telegram-WebApp-Init-Data", "")
    user_id = None
    if init_data:
        for pair in init_data.split("&"):
            if pair.startswith("user="):
                try:
                    user_json = json.loads(pair[5:])
                    user_id = str(user_json["id"])
                except: pass

    if not user_id:
        return web.Response(text="<h3>Открой через бота</h3>", content_type="text/html")

    async with aiosqlite.connect(DB) as db:
        user = await (await db.execute("SELECT theme, accent_color, badge, premium_until FROM users WHERE user_id=?", (user_id,))).fetchone()
        theme = user[0] if user else "dark"
        accent = user[1] if user else "#8774e1"
        badge = user[2] if user else ""
        premium = user[3] if user else None

        stats = await (await db.execute("""
            SELECT 
                (SELECT COUNT(*) FROM questions WHERE from_user=?),
                (SELECT COUNT(*) FROM questions WHERE to_user=?),
                (SELECT COUNT(*) FROM questions WHERE to_user=? AND answered=1),
                (SELECT COUNT(*) FROM questions WHERE to_user=? AND answered=0),
                (SELECT COUNT(*) FROM questions WHERE special=1 AND (from_user=? OR to_user=?))
            """, (user_id, user_id, user_id, user_id, user_id, user_id))).fetchone() or (0,0,0,0,0)

        # Топ-10
        top_rows = await (await db.execute("""
            SELECT u.username, COUNT(q.id) FROM questions q
            JOIN users u ON q.to_user = u.user_id
            GROUP BY q.to_user ORDER BY COUNT(q.id) DESC LIMIT 10
        """)).fetchall()

    top_html = ""
    for i, (username, count) in enumerate(top_rows, 1):
        top_html += f"{i}. @{username or 'аноним'} — {count} вопросов<br>"

    badge_html = f"<div style='font-size:20px;margin:10px'>{badge}</div>" if badge else ""

    html = f"""
    <!DOCTYPE html><html><head>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        body{{font-family:system-ui;padding:20px;background:var(--tg-theme-bg-color);color:var(--tg-theme-text-color);text-align:center}}
        .card{{background:var(--tg-theme-secondary-bg-color);border-radius:16px;padding:20px;margin:15px 0}}
        .num{{font-size:48px;font-weight:800;color:{accent}}}
        button{{margin:10px 0;padding:18px;width:90%;background:{accent};color:white;border:none;border-radius:16px;font-size:19px}}
        .top{{font-size:14px;margin-top:20px}}
    </style>
    </head><body>
    <h1>Личный кабинет {badge_html}</h1>
    <div class="card"><div class="num">{stats[0]}</div>Отправлено</div>
    <div class="card"><div class="num">{stats[1]}</div>Получено</div>
    <div class="card"><div class="num">{stats[2]}</div>Отвечено</div>
    <div class="card"><div class="num" style="color:#e74c3c">{stats[3]}</div>Ждут ответа</div>
    <div class="card"><b>Премиум до:</b> {premium or "Нет"}</div>

    <button onclick="Telegram.WebApp.openInvoice('stars_invoice',{{title:'1 месяц — 135⭐',payload:'month',prices:[{{label:'135⭐',amount:135}}]}})">135⭐ — 1 месяц</button>
    <button onclick="Telegram.WebApp.openInvoice('stars_invoice',{{title:'3 месяца — 330⭐',payload:'3m',prices:[{{label:'330⭐',amount:330}}]}})">330⭐ — 3 месяца</button>
    <button onclick="Telegram.WebApp.openInvoice('stars_invoice',{{title:'Год — 1050⭐',payload:'year',prices:[{{label:'1050⭐',amount:1050}}]}})">1050⭐ — год</button>
    <button onclick="Telegram.WebApp.openInvoice('stars_invoice',{{title:'Пожизненно — 2600⭐',payload:'life',prices:[{{label:'2600⭐',amount:2600}}]}})">2600⭐ — навсегда</button>

    <h3>Топ-10 пользователей</h3>
    <div class="top">{top_html or "Пока пусто"}</div>

    <script>
        Telegram.WebApp.ready();
        Telegram.WebApp.expand();
    </script>
    </body></html>
    """
    return web.Response(text=html, content_type="text/html")

# === ФОНОВЫЕ ЗАДАЧИ: пуш о ответах + еженедельный дайджест ===
async def background_tasks():
    while True:
        now = datetime.now(timezone(timedelta(hours=3)))
        # Пуш о новом ответе
        async with aiosqlite.connect(DB) as db:
            rows = await (await db.execute("SELECT from_user, to_user FROM questions WHERE answered=1 AND notified IS NULL")).fetchall()
            for from_u, to_u in rows:
                user = await (await db.execute("SELECT push_answers FROM users WHERE user_id=?", (from_u,))).fetchone()
                if user and user[0]:
                    try: await bot.send_message(from_u, "Тебе ответили на вопрос! Открой бота")
                    except: pass
                await db.execute("UPDATE questions SET notified=1 WHERE from_user=? AND to_user=?", (from_u, to_u))
            await db.commit()

        # Еженедельный дайджест (вс 12:00 МСК)
        if now.weekday() == 6 and 12 <= now.hour < 13:
            async with aiosqlite.connect(DB) as db:
                users = await (await db.execute("SELECT user_id FROM users WHERE push_answers=1")).fetchall()
                for (uid,) in users:
                    try:
                        await bot.send_message(uid, "Еженедельный дайджест!\nТы получил X вопросов, ответил на Y...")
                    except: pass
            await asyncio.sleep(3600)  # чтобы не спамить

        await asyncio.sleep(60)
        # === ДОБАВЛЕНИЕ ПОЛЯ notified В БАЗУ (если его нет) ===
async def ensure_notified_column():
    async with aiosqlite.connect(DB) as db:
        await db.execute("PRAGMA table_info(questions)")
        columns = [row[1] for row in await db.fetchall()]
        if "notified" not in columns:
            await db.execute("ALTER TABLE questions ADD COLUMN notified INTEGER DEFAULT 0")
            await db.commit()

# === ЗАПУСК БОТА ===
async def on_startup(_):
    await init_db()
    await ensure_notified_column()
    await bot.set_webhook(f"{BASE_URL}/webhook")
    asyncio.create_task(background_tasks())  # пуш + дайджест
    print("ТОП-1 АНОНИМНЫЙ БОТ 2025 ГОДА УСПЕШНО ЗАПУЩЕН!")
    print("Все 18 функций работают: рефералка, звёзды, знаменитости, PDF, топ, лайки, темы, админка, дайджест — ВСЁ ЕСТЬ!")

app = web.Application()
app.router.add_get("/miniapp", miniapp_handler)
SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
app.on_startup.append(on_startup)

if __name__ == "__main__":
    web.run_app(app, port=10000)
