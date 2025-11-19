import os,json,aiosqlite,asyncio
from datetime import datetime,timedelta,timezone
from aiohttp import web
from aiogram import Bot,Dispatcher,types
from aiogram.filters import Command
from aiogram.types import WebAppInfo,InlineKeyboardMarkup,InlineKeyboardButton,PreCheckoutQuery,LabeledPrice
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State,StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.webhook.aiohttp_server import SimpleRequestHandler

TOKEN=os.getenv("BOT_TOKEN")
OWNER_ID=int(os.getenv("OWNER_ID","0"))
BASE_URL=os.getenv("RENDER_EXTERNAL_URL",f"https://{os.getenv('RENDER_INSTANCE_ID')}.onrender.com")

bot=Bot(token=TOKEN,default=DefaultBotProperties(parse_mode="HTML"))
dp=Dispatcher(storage=MemoryStorage())
DB="data.db"

async def init_db():
    async with aiosqlite.connect(DB)as db:
        await db.executescript('''
            CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY,username TEXT,trial_end TEXT,premium_until TEXT,referred_by INT,referred_count INT DEFAULT 0);
            CREATE TABLE IF NOT EXISTS questions(id INTEGER PRIMARY KEY AUTOINCREMENT,from_user INT,to_user INT,text TEXT,answer TEXT,answered INT DEFAULT 0,special INT DEFAULT 0,likes INT DEFAULT 0);
            CREATE TABLE IF NOT EXISTS celebs(user_id INTEGER PRIMARY KEY,name TEXT);
        ''')
        await db.commit()

class Ask(StatesGroup):username=State();question=State()

def kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("Личный кабинет",web_app=WebAppInfo(url=f"{BASE_URL}/miniapp"))],
        [InlineKeyboardButton("Задать вопрос",callback_data="ask")]
    ])
    @dp.message(Command("start"))
async def start(m:types.Message):
    ref=None
    if len(m.text.split())>1:
        try:ref=int(m.text.split()[1])
        except:pass
    async with aiosqlite.connect(DB)as db:
        await db.execute("INSERT OR IGNORE INTO users(user_id)VALUES(?)",(m.from_user.id,))
        if ref and ref!=m.from_user.id:
            await db.execute("UPDATE users SET referred_count=referred_count+1,premium_until=datetime(COALESCE(premium_until,'now'),'+1 day')WHERE user_id=?",(ref,))
            try:await bot.send_message(ref,"+1 день безлимита за друга!")
            except:pass
        if not await(await db.execute("SELECT trial_end FROM users WHERE user_id=?",(m.from_user.id,))).fetchone():
            end=(datetime.now(timezone(timedelta(hours=3)))+timedelta(days=3)).strftime("%Y-%m-%d")
            await db.execute("UPDATE users SET trial_end=? WHERE user_id=?",(end,m.from_user.id))
        await db.commit()
    await m.answer("Анонимные вопросы\n• 3 дня безлимит\n• 1 друг = +1 день",reply_markup=kb())

@dp.callback_query(lambda c:c.data=="ask")
async def ask(c:types.CallbackQuery,state:FSMContext):
    await c.message.edit_text("Username (с @ или без):")
    await state.set_state(Ask.username)

@dp.message(Ask.username)
async def get_user(m:types.Message,state:FSMContext):
    u=m.text.lstrip("@").lower()
    async with aiosqlite.connect(DB)as db:
        row=await(await db.execute("SELECT user_id FROM users WHERE LOWER(username)=?",(u,))).fetchone()
        if not row:
            await m.answer("Пользователь ещё не в боте — уведомим при старте!",reply_markup=kb())
            await state.clear();return
        celeb=await(await db.execute("SELECT name FROM celebs WHERE user_id=?",(row[0],))).fetchone()
        if celeb:
            await m.answer(f"Вопрос {celeb[0]} — 250⭐",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton("Оплатить 250⭐",pay=True)]]))
            await state.update_data(to_id=row[0],celeb=True)
        else:
            await state.update_data(to_id=row[0])
            await m.answer("Тип вопроса:",reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton("Обычный",callback_data="normal")],
                [InlineKeyboardButton("Особый — 5⭐",callback_data="special")]
            ]))

@dp.callback_query(lambda c:c.data in ["normal","special"])
async def type_sel(c:types.CallbackQuery,state:FSMContext):
    await state.update_data(special=1 if c.data=="special" else 0)
    await c.message.edit_text("Напиши вопрос:")
    await state.set_state(Ask.question)

@dp.message(Ask.question)
async def send_q(m:types.Message,state:FSMContext):
    data=await state.get_data()
    to_id,special=data["to_id"],data.get("special",0)
    async with aiosqlite.connect(DB)as db:
        await db.execute("INSERT INTO questions(from_user,to_user,text,special)VALUES(?,?,?,?)",
                        (m.from_user.id,to_id,m.text,special))
        await db.commit()
    prefix="Особый вопрос! "if special else""
    await bot.send_message(to_id,f"{prefix}Новый вопрос:\n\n{m.text}\n\nОтветь сюда")
    await m.answer("Отправлено!",reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("Ещё вопрос",callback_data="ask")],
        [InlineKeyboardButton("Кабинет",web_app=WebAppInfo(url=f"{BASE_URL}/miniapp"))]
    ]))
    await state.clear()

@dp.message()
async def reply(m:types.Message):
    if m.reply_to_message and"Новый вопрос" in m.reply_to_message.text:
        qtext=m.reply_to_message.text.split("\n\n")[1].split("\n\n")[0]
        async with aiosqlite.connect(DB)as db:
            row=await(await db.execute("SELECT from_user FROM questions WHERE to_user=? AND text=? AND answered=0",(m.from_user.id,qtext))).fetchone()
            if row:
                await db.execute("UPDATE questions SET answer=?,answered=1 WHERE from_user=? AND to_user=? AND text=?",(m.text,row[0],m.from_user.id,qtext))
                await db.commit()
                await bot.send_message(row[0],f"Ответ:\n\n{m.text}")
                await m.answer("Отправлено!",reply_markup=kb())
                async def miniapp_handler(r):
    d=r.headers.get("X-Telegram-WebApp-Init-Data","")
    uid=None
    if d:
        for p in d.split("&"):
            if p.startswith("user="):
                try:uid=str(json.loads(p[5:])["id"])
                except:pass
    if not uid:return web.Response(text="<h3>Открой через бота</h3>",content_type="text/html")
    async with aiosqlite.connect(DB)as db:
        s=await(await db.execute("""SELECT
            (SELECT COUNT(*)FROM questions WHERE from_user=?),
            (SELECT COUNT(*)FROM questions WHERE to_user=?),
            (SELECT COUNT(*)FROM questions WHERE to_user=? AND answered=1),
            (SELECT COUNT(*)FROM questions WHERE to_user=? AND answered=0)
        """,(uid,)*4)).fetchone()or(0,0,0,0)
    html=f"""
    <!DOCTYPE html><html><head><meta name="viewport"content="width=device-width,initial-scale=1">
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>body{{font-family:system-ui;padding:20px;background:var(--tg-theme-bg-color);color:var(--tg-theme-text-color);text-align:center}}
    .c{{background:var(--tg-theme-secondary-bg-color);border-radius:16px;padding:20px;margin:10px 0}}
    .n{{font-size:44px;font-weight:800;color:var(--tg-theme-accent-text-color)}}
    button{{margin:20px 0;padding:16px;width:90%;background:var(--tg-theme-button-color);color:var(--tg-theme-button-text-color);border:none;border-radius:16px;font-size:18px}}
    </style></head><body>
    <h1>Личный кабинет</h1>
    <div class="c"><div class="n">{s[0]}</div>Отправлено</div>
    <div class="c"><div class="n">{s[1]}</div>Получено</div>
    <div class="c"><div class="n">{s[2]}</div>Отвечено</div>
    <div class="c"><div class="n"style="color:#e74c3c">{s[3]}</div>Ждут ответа</div>
    <button onclick="Telegram.WebApp.openInvoice('stars_invoice',{{title:'Безлимит 1 месяц',payload:'month',prices:[{{label:'1 месяц',amount:135}}]}})">135⭐ — 1 месяц</button>
    <button onclick="Telegram.WebApp.openInvoice('stars_invoice',{{title:'Безлимит 3 месяца',payload:'3month',prices:[{{label:'3 месяца',amount:330}}]}})">330⭐ — 3 месяца</button>
    <button onclick="Telegram.WebApp.openInvoice('stars_invoice',{{title:'Безлимит год',payload:'year',prices:[{{label:'Год',amount:1050}}]}})">1050⭐ — год</button>
    <button onclick="Telegram.WebApp.close()">Закрыть</button>
    <script>Telegram.WebApp.ready();Telegram.WebApp.expand();</script>
    </body></html>
    """
    return web.Response(text=html,content_type="text/html")

# Оплата звёздами (встроенная в Telegram)
@dp.message(lambda m:m.successful_payment and m.successful_payment.currency=="XTR")
async def stars_paid(m:types.Message):
    payload=m.successful_payment.invoice_payload
    days={"month":30,"3month":90,"year":365}.get(payload,0)
    if days:
        end=(datetime.now(timezone(timedelta(hours=3)))+timedelta(days=days)).strftime("%Y-%m-%d")
        async with aiosqlite.connect(DB)as db:
            await db.execute("UPDATE users SET premium_until=? WHERE user_id=?",(end,m.from_user.id))
            await db.commit()
        await m.answer("Безлимит активирован!")

async def on_startup(_):
    await init_db()
    await bot.set_webhook(f"{BASE_URL}/webhook")
    print("БОТ ЗАПУЩЕН — ВСЁ ГОТОВО 2025")

app=web.Application()
app.router.add_get("/miniapp",miniapp_handler)
SimpleRequestHandler(dispatcher=dp,bot=bot).register(app,path="/webhook")
app.on_startup.append(on_startup)
if __name__=="__main__":web.run_app(app,port=10000)
