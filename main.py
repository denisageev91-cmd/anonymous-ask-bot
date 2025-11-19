import os
import json
import aiosqlite
import asyncio
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

# === НАСТРОЙКИ ===
TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))  # ← ТВОЙ ТЕЛЕГРАМ ID
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", f"https://{os.getenv('RENDER_INSTANCE_ID')}.onrender.com")

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
DB = "data.db"

# Языки
L = {
    "ru": {"cabinet": "Личный кабинет", "ask": "Задать вопрос", "sent": "Отправлено", "received": "Получено", "answered": "Отвечено", "waiting": "Ждут ответа", "premium": "Премиум", "until": "до", "buy": "Купить безлимит"},
    "en": {"cabinet": "Profile", "ask": "Ask", "sent": "Sent", "received": "Received", "answered": "Answered", "waiting": "Waiting", "premium": "Premium", "until": "until", "buy": "Buy Unlimited"},
    "es": {"cabinet": "Perfil", "ask": "Preguntar", "sent": "Enviados", "received": "Recibidos", "answered": "Respondidos", "waiting": "Pendientes", "premium": "Premium", "until": "hasta", "buy": "Comprar ilimitado"},
    "ar": {"cabinet": "الملف الشخصي", "ask": "اسأل", "sent": "مرسلة", "received": "مستلمة", "answered": "تم الرد", "waiting": "في الانتظار", "premium": "بريميوم", "until": "حتى", "buy": "شراء غير محدود"}
}
# === БАЗА ДАННЫХ ===
async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                lang TEXT DEFAULT "ru",
                trial_end TEXT,
                premium_until TEXT,
                premium_type TEXT,           -- month / year / lifetime
                referred_by INT,
                referred_count INT DEFAULT 0,
                push_answers INTEGER DEFAULT 1,
                theme TEXT DEFAULT "dark",
                accent_color TEXT DEFAULT "#8774e1",
                banned INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_user INT,
                to_user INT,
                text TEXT,
                answer TEXT,
                answered INTEGER DEFAULT 0,
                hidden INTEGER DEFAULT 0,
                special INTEGER DEFAULT 0,
                likes INTEGER DEFAULT 0,
                bumped_at TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS celebs (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                verified INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS pending_questions (
                username TEXT PRIMARY KEY,
                from_user INT
            );
        ''')
        await db.commit()

# === СОСТОЯНИЯ ===
class Ask(StatesGroup):
    username = State()
    question = State()
    special_confirm = State()

# === ЯЗЫК ПОЛЬЗОВАТЕЛЯ ===
async def get_user_lang(user_id):
    async with aiosqlite.connect(DB) as db:
        row = await (await db.execute("SELECT lang FROM users WHERE user_id=?", (user_id,))).fetchone()
    return row[0] if row else "ru"

# === ОСНОВНАЯ КЛАВИАТУРА ===
def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Личный кабинет", web_app=WebAppInfo(url=f"{BASE_URL}/miniapp"))],
        [InlineKeyboardButton(text="Задать вопрос", callback_data="ask")]
    ])
    # === СТАРТ + РЕФЕРАЛКА ===
@dp.message(Command("start"))
async def start(m: types.Message):
    args = m.text.split(maxsplit=1)
    ref_id = int(args[1]) if len(args) > 1 and args[1].isdigit() else None

    async with aiosqlite.connect(DB) as db:
        username = (m.from_user.username or "").lstrip("@").lower()
        await db.execute(
            "INSERT INTO users (user_id, username) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
            (m.from_user.id, username)
        )

        # Рефералка: +1 день за каждого приглашённого
        if ref_id and ref_id != m.from_user.id:
            row = await (await db.execute("SELECT referred_by FROM users WHERE user_id=?", (m.from_user.id,))).fetchone()
            if not row or not row[0]:
                await db.execute("UPDATE users SET referred_by=?, referred_count = referred_count + 1 WHERE user_id=?", (ref_id, ref_id))
                # Добавляем 1 день премиума рефереру
                await db.execute("""
                    UPDATE users SET premium_until = datetime(COALESCE(premium_until, 'now'), '+1 day')
                    WHERE user_id=?
                """, (ref_id,))
                try:
                    await bot.send_message(ref_id, "Ты пригласил друга — +1 день безлимита!")
                except: pass

        # Триал 3 дня, если ещё не было
        row = await (await db.execute("SELECT trial_end FROM users WHERE user_id=?", (m.from_user.id,))).fetchone()
        if not row or not row[0]:
            end = (datetime.now(timezone(timedelta(hours=3))) + timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
            await db.execute("UPDATE users SET trial_end=? WHERE user_id=?", (end, m.from_user.id))

        await db.commit()

    text = (
        "Анонимные вопросы 2025\n\n"
        "• 3 дня безлимит бесплатно\n"
        "• Потом 5 вопросов/сутки\n"
        "• Рефералка: 1 друг = +1 день безлимита\n"
        "• Подписка от 99 ₽/мес"
    )
    await m.answer(text, reply_markup=main_kb())

# === АДМИН-ПАНЕЛЬ (только для OWNER_ID) ===
@dp.message(Command("admin"))
async def admin_panel(m: types.Message):
    if m.from_user.id != OWNER_ID:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="Бан/Разбан", callback_data="admin_ban")],
        [InlineKeyboardButton(text="Топ пользователей", callback_data="admin_top")]
    ])
    await m.answer("Админ-панель", reply_markup=kb)

@dp.callback_query(lambda c: c.data == "admin_stats")
async def admin_stats(c: types.CallbackQuery):
    if c.from_user.id != OWNER_ID:
        return
    async with aiosqlite.connect(DB) as db:
        total_users = await (await db.execute("SELECT COUNT(*) FROM users")).fetchone()
        total_q = await (await db.execute("SELECT COUNT(*) FROM questions")).fetchone()
        premium = await (await db.execute("SELECT COUNT(*) FROM users WHERE premium_until > datetime('now')")).fetchone()
    text = f"Пользователей: {total_users[0]}\nВопросов: {total_q[0]}\nАктивных премиум: {premium[0]}"
    await c.message.edit_text(text, reply_markup=c.message.reply_markup)
    # === ЗАДАТЬ ВОПРОС ===
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
            await db.execute("INSERT OR REPLACE INTO pending_questions (username, from_user) VALUES (?, ?)",
                           (username, m.from_user.id))
            await db.commit()
            await m.answer(f"@{username} ещё не в боте\nМы уведомим, когда он запустит бота", reply_markup=main_kb())
            await state.clear()
            return

        # Проверка — это знаменитость?
        celeb = await (await db.execute("SELECT name FROM celebs WHERE user_id=?", (row[0],))).fetchone()
        if celeb:
            await state.update_data(to_id=row[0], celeb=1, celeb_name=celeb[0])
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Оплатить 250 ⭐", pay=True)]
            ])
            await m.answer(f"Вопрос {celeb[0]} стоит 250 звёзд", reply_markup=kb)
            await state.set_state(Ask.special_confirm)
            return

        await state.update_data(to_id=row[0])
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Обычный вопрос", callback_data="type_normal")],
            [InlineKeyboardButton(text="Особый вопрос (5 ⭐)", callback_data="type_special")]
        ])
        await m.answer("Выбери тип вопроса:", reply_markup=kb)

@dp.callback_query(lambda c: c.data in ["type_normal", "type_special"])
async def ask_type(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    special = 1 if c.data == "type_special" else 0
    cost = 5 if special else 0
    await state.update_data(special=special, cost=cost)
    if cost > 0:
        await c.message.edit_text(f"Особый вопрос — 5 звёзд\nНапиши вопрос:")
    else:
        await c.message.edit_text("Напиши вопрос:")
    await state.set_state(Ask.question)

@dp.message(Ask.question)
async def ask_question(m: types.Message, state: FSMContext):
    data = await state.get_data()
    to_id = data["to_id"]
    special = data.get("special", 0)
    cost = data.get("cost", 0)

    # Проверка лимита и списание звёзд
    if cost > 0:
        if not m.from_user.is_premium and cost > 0:
            await m.answer("Нужны Telegram Stars")
            await state.clear()
            return

    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO questions (from_user, to_user, text, special) VALUES (?, ?, ?, ?)",
            (m.from_user.id, to_id, m.text, special)
        )
        await db.commit()

    style = "🔥✨" if special else ""
    await bot.send_message(to_id, f"{style}Новый анонимный вопрос:\n\n{m.text}\n\nОтветь на сообщение — ответ уйдёт анонимно")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Задать ещё", callback_data="ask")],
        [InlineKeyboardButton(text="Личный кабинет", web_app=WebAppInfo(url=f"{BASE_URL}/miniapp"))]
    ])
    await m.answer("Вопрос отправлен!", reply_markup=kb)
    await state.clear()

# === ЛАЙКИ И ПОДНЯТЬ ===
@dp.message(lambda m: m.reply_to_message and "Новый анонимный вопрос" in m.reply_to_message.text and m.text in ["❤️", "♥️"])
async def like_question(m: types.Message):
    qtext = m.reply_to_message.text.split("\n\n")[1].split("\n\n")[0]
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE questions SET likes = likes + 1 WHERE to_user=? AND text=?", (m.from_user.id, qtext))
        await db.commit()
    await m.answer("❤️")

@dp.callback_query(lambda c: c.data.startswith("bump_"))
async def bump_question(c: types.CallbackQuery):
    qid = int(c.data.split("_")[1])
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE questions SET bumped_at = datetime('now') WHERE id=?", (qid,))
        await db.commit()
    await bot.send_message(c.from_user.id, "Вопрос поднят в топ за 1 звезду!")
    # === ПЛАТЕЖИ (Telegram Stars) ===
@dp.message(lambda m: m.text and "Купить" in m.text)
async def buy_menu(m: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Безлимит — 99 ₽/мес", callback_data="buy_month")],
        [InlineKeyboardButton(text="Безлимит — 799 ₽/год", callback_data="buy_year")],
        [InlineKeyboardButton(text="Пожизненный безлимит — 2999 ₽", callback_data="buy_lifetime")],
        [InlineKeyboardButton(text="Экспорт вопросов в PDF — 10 ⭐", callback_data="export_pdf")]
    ])
    await m.answer("Выбери подписку:", reply_markup=kb)

@dp.callback_query(lambda c: c.data.startswith("buy_"))
async def process_buy(c: types.CallbackQuery):
    plan = c.data.split("_")[1]
    prices = {"month": 99, "year": 799, "lifetime": 2999}
    titles = {"month": "Безлимит 1 месяц", "year": "Безлимит 1 год", "lifetime": "Пожизненный безлимит"}
    
    await bot.send_invoice(
        chat_id=c.from_user.id,
        title=titles[plan],
        description="Доступ к безлимитным анонимным вопросам",
        payload=f"premium_{plan}",
        provider_token="",  # ← ВСТАВЬ СВОЙ PROVIDER TOKEN ОТ @BotFather !!!
        currency="RUB",
        prices=[LabeledPrice(label=titles[plan], amount=prices[plan] * 100)],
        start_parameter="premium"
    )

@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(q.id, ok=True)

@dp.message(lambda m: m.successful_payment)
async def payment_success(m: types.Message):
    payload = m.successful_payment.invoice_payload
    days = {"month": 30, "year": 365, "lifetime": 9999}.get(payload.split("_")[1], 30)
    
    async with aiosqlite.connect(DB) as db:
        if days == 9999:
            await db.execute("UPDATE users SET premium_until='9999-12-31', premium_type='lifetime' WHERE user_id=?", (m.from_user.id,))
        else:
            end = (datetime.now(timezone(timedelta(hours=3))) + timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
            await db.execute("UPDATE users SET premium_until=?, premium_type=? WHERE user_id=?", (end, payload.split("_")[1], m.from_user.id))
        await db.commit()
    
    await m.answer("Подписка активирована! Безлимит включён", reply_markup=main_kb())

# === ЭКСПОРТ В PDF (10 звёзд) ===
@dp.callback_query(lambda c: c.data == "export_pdf")
async def export_pdf(c: types.CallbackQuery):
    async with aiosqlite.connect(DB) as db:
        rows = await (await db.execute("SELECT text, answer FROM questions WHERE from_user=? OR to_user=? ORDER BY id DESC LIMIT 100", (c.from_user.id, c.from_user.id))).fetchall()
    
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    y = height - 50
    for q, a in rows:
        p.drawString(50, y, f"Q: {q[:100]}")
        y -= 20
        if a:
            p.drawString(70, y, f"A: {a[:100]}")
            y -= 20
        if y < 100:
            p.showPage()
            y = height - 50
    p.save()
    buffer.seek(0)
    
    await bot.send_document(c.from_user.id, InputFile(buffer, filename="my_questions.pdf"))
    await c.message.edit_text("PDF отправлен!")

# === ЗНАМЕНИТОСТИ (250 звёзд) ===
# Добавь в базу вручную или через админку, например:
# INSERT INTO celebs (user_id, name) VALUES (123456789, 'Илон Маск');
# === MINI APP — ЛИЧНЫЙ КАБИНЕТ СО ВСЕМИ ФИЧАМИ ===
async def miniapp_handler(request):
    init_data = request.headers.get("X-Telegram-WebApp-Init-Data") or request.query.get("initData", "")
    user_id = None
    lang = "ru"
    theme = "dark"
    accent = "#8774e1"

    if init_data:
        for pair in init_data.split("&"):
            if pair.startswith("user="):
                try:
                    user_json = json.loads(pair[5:])
                    user_id = str(user_json["id"])
                    lang = user_json.get("language_code", "ru")[:2]
                except: pass

    if not user_id:
        return web.Response(text="<h3>Открой через бота</h3>", content_type="text/html")

    async with aiosqlite.connect(DB) as db:
        user = await (await db.execute("SELECT theme, accent_color FROM users WHERE user_id=?", (user_id,))).fetchone()
        if user:
            theme, accent = user

        stats = await db.execute("""
            SELECT 
                (SELECT COUNT(*) FROM questions WHERE from_user=?),
                (SELECT COUNT(*) FROM questions WHERE to_user=?),
                (SELECT COUNT(*) FROM questions WHERE to_user=? AND answered=1),
                (SELECT COUNT(*) FROM questions WHERE to_user=? AND answered=0),
                (SELECT COUNT(*) FROM questions WHERE special=1 AND (from_user=? OR to_user=?)),
                premium_until
            FROM users WHERE user_id=?
        """, (user_id, user_id, user_id, user_id, user_id, user_id, user_id))
        s = await (await stats.fetchone()) or (0,0,0,0,0,None)

    # Топ-10
    top = ""
    async with aiosqlite.connect(DB) as db:
        rows = await (await db.execute("""
            SELECT u.username, COUNT(q.id) as cnt FROM questions q
            JOIN users u ON q.to_user = u.user_id
            GROUP BY q.to_user ORDER BY cnt DESC LIMIT 10
        """)).fetchall()
        for i, (u, c) in enumerate(rows, 1):
            top += f"{i}. @{u} — {c} вопросов<br>"

    html = f"""
    <!DOCTYPE html><html><head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        body {{margin:0; padding:20px; font-family:system-ui; background:var(--tg-theme-bg-color); color:var(--tg-theme-text-color);}}
        .card {{background:var(--tg-theme-secondary-bg-color); border-radius:16px; padding:20px; margin:15px 0; text-align:center;}}
        .num {{font-size:48px; font-weight:800; color:{accent};}}
        button {{background:{accent}; color:white; border:none; padding:16px; width:90%; border-radius:16px; font-size:18px; margin:10px 0;}}
        .top {{font-size:14px;}}
    </style>
    </head><body>
    <h1>Личный кабинет</h1>
    <div class="card"><div class="num">{s[0]}</div><div>Отправлено</div></div>
    <div class="card"><div class="num">{s[1]}</div><div>Получено</div></div>
    <div class="card"><div class="num">{s[2]}</div><div>Отвечено</div></div>
    <div class="card"><div class="num" style="color:#e74c3c">{s[3]}</div><div>Ждут ответа</div></div>
    <div class="card">Премиум до: <b>{s[5] or "Нет"}</b></div>
    <button onclick="Telegram.WebApp.openLink('https://t.me/YourBot?start=ref'+Telegram.WebApp.initDataUnsafe.user.id)">Пригласить друга (+1 день)</button>
    <button onclick="location.href='/buy'">Купить подписку</button>
    <h3>Топ-10</h3><div class="top">{top or "Пока пусто"}</div>
    <script>
        Telegram.WebApp.ready();
        Telegram.WebApp.expand();
        document.body.style.setProperty('--tg-theme-accent-color', '{accent}');
    </script>
    </body></html>
    """
    return web.Response(text=html, content_type="text/html")

# === ФОНОВЫЕ ЗАДАЧИ (дайджест, напоминания) ===
async def background_tasks():
    while True:
        now = datetime.now(timezone(timedelta(hours=3)))
        # Еженедельный дайджест — каждое воскресенье в 12:00 МСК
        if now.weekday() == 6 and now.hour == 12 and now.minute < 5:
            async with aiosqlite.connect(DB) as db:
                rows = await (await db.execute("SELECT user_id FROM users WHERE push_answers=1")).fetchall()
                for (uid,) in rows:
                    try:
                        await bot.send_message(uid, "Еженедельный дайджест!\nТы получил X вопросов, ответил на Y...")
                    except: pass
        await asyncio.sleep(300)

# === ЗАПУСК ===
async def on_startup(_):
    await init_db()
    await bot.set_webhook(f"{BASE_URL}/webhook")
    asyncio.create_task(background_tasks())
    print("БОТ ЗАПУЩЕН — ТЫ СДЕЛАЛ ТОП-1 АНОНИМНЫЙ БОТ 2025 ГОДА!")

app = web.Application()
app.router.add_get("/miniapp", miniapp_handler)
SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
app.on_startup.append(on_startup)

if __name__ == "__main__":
    web.run_app(app, port=10000)
