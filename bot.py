import asyncio
import html
import importlib.util
import logging
import os
import random
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta

# ==========================================
# АВТОУСТАНОВКА ЗАВИСИМОСТЕЙ
# ==========================================
def _run(cmd: list) -> bool:
    return subprocess.run(cmd).returncode == 0

def bootstrap():
    packages = {"aiogram": "aiogram>=3.7", "playwright": "playwright", "playwright_stealth": "playwright-stealth", "aiohttp": "aiohttp"}
    missing = [pkg for mod, pkg in packages.items() if importlib.util.find_spec(mod) is None]
    if missing:
        print(f"Устанавливаю: {', '.join(missing)}...", flush=True)
        base = [sys.executable, "-m", "pip", "install", "-q"]
        if not _run(base + missing):
            _run(base + ["--break-system-packages"] + missing)
    
    marker = ".chromium_installed"
    if not os.path.exists(marker):
        print("Устанавливаю браузер Chromium (один раз)...", flush=True)
        ok = _run([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"])
        if not ok:
            ok = _run([sys.executable, "-m", "playwright", "install", "chromium"])
        if ok:
            open(marker, "w").close()

bootstrap()

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove)
from playwright.async_api import async_playwright, Browser, BrowserContext
from playwright_stealth import stealth_async

# ==========================================
# КОНФИГУРАЦИЯ
# ==========================================
BOT_TOKEN = "8949252988:AAHwB5upEkrz_o3wSJUEmVEq7EnOOtZNrw8"  # Замени на свой, если что
ADMIN_ID = 8404647058
DB_PATH = "support_bot.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

INVISIBLE_RE = re.compile(r"[\u2060\u200B\u200C\u200D\uFEFF\u00A0]")

DEFAULT_SETTINGS = {
    "target_url": "https://telegram.org/support?setln=ru",
    "sel_problem": "textarea",
    "sel_name": 'input[name="name"], input[placeholder*="имя" i], input[placeholder*="name" i]',
    "sel_email": 'input[name="email"], input[type="email"]',
    "sel_phone": 'input[name="phone"], input[type="tel"]',
    "sel_submit": 'button[type="submit"], input[type="submit"]',
    "captcha_text": "проверка безопасности,cloudflare,turnstile,я не робот,i'm not a robot",
    "success_text": "Спасибо,thank you,отправлено,получили,Ваше сообщение",
    "headless": "True",
    "proxy": "",  # Формат: http://user:pass@ip:port (ОБЯЗАТЕЛЬНО для обхода блокировок!)
    "poll_interval": "3",
    "max_attempts": "2",
    "nav_timeout": "30",
    "success_timeout": "10",
    "type_delay_min": "100",  # Увеличено для имитации человека
    "type_delay_max": "300",
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

# ==========================================
# БАЗА ДАННЫХ
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, sub_until TEXT, is_banned INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS queue (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, problem TEXT, name TEXT, email TEXT, phone TEXT, status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0, created_at TEXT);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
    """)
    for key, val in DEFAULT_SETTINGS.items():
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, str(val)))
    cur.execute("INSERT OR IGNORE INTO users (user_id, username, sub_until) VALUES (?, ?, ?)", (ADMIN_ID, "Yamayk_a", "2099-12-31 23:59:59"))
    cur.execute("UPDATE queue SET status = 'cancelled' WHERE status = 'processing'")
    conn.commit()
    conn.close()

def get_cfg() -> dict:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT key, value FROM settings")
    cfg = DEFAULT_SETTINGS.copy()
    cfg.update({k: v for k, v in cur.fetchall()})
    conn.close()
    return cfg

def set_cfg_param(key: str, value: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, INVISIBLE_RE.sub("", value).strip()))
    conn.commit()
    conn.close()

def set_task_status(task_id: int, status: str, inc_attempts: bool = False):
    conn = sqlite3.connect(DB_PATH)
    if inc_attempts:
        conn.execute("UPDATE queue SET status = ?, attempts = attempts + 1 WHERE id = ?", (status, task_id))
    else:
        conn.execute("UPDATE queue SET status = ? WHERE id = ?", (status, task_id))
    conn.commit()
    conn.close()

# ==========================================
# PLAYWRIGHT STEALTH POOL (ГЛАВНЫЙ СЕКРЕТ)
# ==========================================
class BrowserPool:
    def __init__(self):
        self.playwright = None
        self.browser: Browser = None
        self.lock = asyncio.Lock()

    async def start(self, headless: bool, proxy: str, user_agent: str):
        async with self.lock:
            if self.browser is None:
                self.playwright = await async_playwright().start()
                launch_args = [
                    "--no-sandbox", "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-web-security", "--disable-features=IsolateOrigins,site-per-process"
                ]
                proxy_dict = {"server": proxy} if proxy else None
                self.browser = await self.playwright.chromium.launch(
                    headless=headless,
                    args=launch_args,
                    proxy=proxy_dict
                )
                logger.info("🚀 Браузер запущен в режиме Stealth")

    async def get_context(self, user_agent: str) -> BrowserContext:
        return await self.browser.new_context(
            user_agent=user_agent,
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU",
            timezone_id="Europe/Moscow"
        )

    async def stop(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

browser_pool = BrowserPool()

# ==========================================
# ЧЕЛОВЕЧЕСКОЕ ПОВЕДЕНИЕ
# ==========================================
async def human_type(locator, text: str, delay_min: int, delay_max: int):
    """Имитирует человеческий ввод с рандомными задержками."""
    await locator.click()
    for char in text:
        await locator.press_sequentially(char, delay=random.randint(delay_min, delay_max))
        # Случайные микро-паузы
        if random.random() < 0.2:
            await asyncio.sleep(random.uniform(0.1, 0.4))

async def get_page_text(page) -> str:
    parts = []
    for frame in page.frames:
        try:
            parts.append(await frame.inner_text("body", timeout=2000))
        except Exception:
            pass
    return re.sub(r"\s+", " ", " ".join(parts)).lower()

# ==========================================
# ОБРАБОТКА ЗАДАЧИ
# ==========================================
async def process_report_task(task_id: int, problem: str, name: str, email: str, phone: str):
    try:
        cfg = get_cfg()
        target_url = cfg.get("target_url", "").strip()
        if not target_url:
            return False, "target_url не задан"

        headless_mode = cfg.get("headless", "True").lower() == "true"
        proxy = cfg.get("proxy", "").strip()
        user_agent = cfg.get("user_agent", DEFAULT_SETTINGS["user_agent"])
        
        # Гарантируем запуск пула
        await browser_pool.start(headless_mode, proxy, user_agent)
        context = await browser_pool.get_context(user_agent)
        page = await context.new_page()
        
        # ВКЛЮЧАЕМ STEALTH ДЛЯ ЭТОЙ СТРАНИЦЫ
        await stealth_async(page)

        nav_timeout = int(cfg.get("nav_timeout", 30)) * 1000
        page.set_default_timeout(nav_timeout)

        # 1. Переход
        await page.goto(target_url, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(1.5, 3.0)) # Пауза "человека" после загрузки

        # 2. Заполнение (с человеческой имитацией)
        type_min, type_max = int(cfg["type_delay_min"]), int(cfg["type_delay_max"])
        
        await human_type(page.locator(cfg["sel_problem"]).first, problem, type_min, type_max)
        await asyncio.sleep(random.uniform(0.5, 1.5))
        await human_type(page.locator(cfg["sel_name"]).first, name, type_min, type_max)
        await asyncio.sleep(random.uniform(0.5, 1.5))
        await human_type(page.locator(cfg["sel_email"]).first, email, type_min, type_max)
        await asyncio.sleep(random.uniform(0.5, 1.5))
        await human_type(page.locator(cfg["sel_phone"]).first, phone, type_min, type_max)
        
        await asyncio.sleep(random.uniform(1.0, 2.5)) # Пауза перед отправкой

        # 3. Клик
        await page.locator(cfg["sel_submit"]).first.click(force=True)

        # 4. Проверка результата
        success_timeout = int(cfg.get("success_timeout", 10))
        success_keywords = [k.strip().lower() for k in cfg["success_text"].split(",") if k.strip()]
        captcha_keywords = [k.strip().lower() for k in cfg["captcha_text"].split(",") if k.strip()]

        deadline = asyncio.get_running_loop().time() + success_timeout
        is_success = False
        
        while asyncio.get_running_loop().time() < deadline:
            content = await get_page_text(page)
            
            # Проверка на капчу (если вылезла - сразу фейл, значит прокси плохой)
            if any(ck in content for ck in captcha_keywords):
                await context.close()
                return False, "Cloudflare/CAPTCHA. Смените прокси или увеличьте задержки."
            
            if any(kw in content for kw in success_keywords):
                is_success = True
                break
            await asyncio.sleep(0.5)

        await context.close()

        if is_success:
            set_task_status(task_id, "success")
            return True, None
        else:
            set_task_status(task_id, "failed", inc_attempts=True)
            return False, "Нет подтверждения успеха. Возможно, капча или неверные селекторы."

    except Exception as e:
        logger.exception(f"Ошибка задачи #{task_id}")
        set_task_status(task_id, "failed", inc_attempts=True)
        return False, str(e)

# ==========================================
# WORKER ОЧЕРЕДИ
# ==========================================
active_tasks: dict[int, bool] = {}

async def batch_worker(user_id: int, bot: Bot, first_id: int, last_id: int):
    active_tasks[user_id] = True
    cfg = get_cfg()
    poll_interval = int(cfg.get("poll_interval", 3))

    try:
        while active_tasks.get(user_id, False):
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute(
                "SELECT id, problem, name, email, phone FROM queue WHERE user_id = ? AND status = 'pending' AND id BETWEEN ? AND ? ORDER BY id ASC LIMIT 1",
                (user_id, first_id, last_id)
            )
            task = cur.fetchone()
            conn.close()

            if not task:
                break

            task_id, problem, name, email, phone = task
            
            # Статус processing, чтобы не брать дважды
            conn = sqlite3.connect(DB_PATH)
            conn.execute("UPDATE queue SET status = 'processing' WHERE id = ?", (task_id,))
            conn.commit()
            conn.close()

            success, error_msg = await process_report_task(task_id, problem, name, email, phone)
            
            if not success:
                try:
                    await bot.send_message(user_id, f"❌ Заявка #{task_id} не отправлена.\nПричина: {html.escape(str(error_msg))}\n\n💡 Совет: проверьте прокси или уменьшите кол-во заявок в пакете.")
                except Exception:
                    pass

            await asyncio.sleep(poll_interval)

    finally:
        active_tasks[user_id] = False
        # Итоговая статистика
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*), SUM(CASE WHEN status='success' THEN 1 ELSE 0 END), SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) FROM queue WHERE user_id = ? AND id BETWEEN ? AND ?",
            (user_id, first_id, last_id)
        )
        total, succ, fail = cur.fetchone()
        conn.close()
        
        try:
            await bot.send_message(user_id, f"🏁 <b>Рассылка завершена!</b>\n📦 Всего: {total or 0}\n✅ Успешно: {succ or 0}\n❌ Ошибок: {fail or 0}", reply_markup=get_main_kb(user_id))
        except Exception:
            pass

# ==========================================
# ХЕНДЛЕРЫ (сокращены для краткости, логика та же)
# ==========================================
router = Router()

def get_main_kb(user_id: int):
    kb = [[KeyboardButton(text="🚀 Отправить жалобы"), KeyboardButton(text="📊 Моя статистика")], [KeyboardButton(text="📋 Главное меню")]]
    if user_id == ADMIN_ID: kb.append([KeyboardButton(text="👑 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

@router.message(CommandStart())
@router.message(F.text == "📋 Главное меню")
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(f"👋 <b>Привет, {html.escape(msg.from_user.first_name or '')}!</b>\n\nБот готов к работе.", reply_markup=get_main_kb(msg.from_user.id))

@router.message(F.text == "🚀 Отправить жалобы")
async def start_batch(msg: Message, state: FSMContext):
    if active_tasks.get(msg.from_user.id):
        await msg.answer("⏳ У вас уже идёт рассылка.")
        return
    await state.set_state(FormState.problem)
    await msg.answer("📝 <b>Шаг 1/5:</b> Введите текст проблемы:", reply_markup=ReplyKeyboardRemove())

# ... (Остальные хендлеры FormState.name, email, phone, count оставь как в твоём коде, они рабочие) ...
# Для экономии места я не дублирую 100 строк FSM, они остаются без изменений из твоего кода.
# Просто убедись, что в process_count вызывается:
# asyncio.create_task(batch_worker(msg.from_user.id, bot, first_id, last_id))

@router.callback_query(F.data == "stop_batch")
async def stop_batch_callback(call: CallbackQuery):
    active_tasks[call.from_user.id] = False
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE queue SET status = 'cancelled' WHERE user_id = ? AND status IN ('pending', 'processing')", (call.from_user.id,))
    conn.commit()
    conn.close()
    await call.answer("🛑 Остановлено!")

@router.message(Command("set"))
async def admin_set(msg: Message):
    if msg.from_user.id != ADMIN_ID: return
    parts = msg.text.split(maxsplit=2)
    if len(parts) < 3:
        await msg.answer("Формат: /set КЛЮЧ ЗНАЧЕНИЕ")
        return
    set_cfg_param(parts[1], parts[2])
    await msg.answer(f"✅ Обновлено: {parts[1]} = {parts[2]}")

# ==========================================
# ТОЧКА ВХОДА
# ==========================================
async def main():
    init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    
    logger.info("✅ Бот запущен. Введите /set proxy http://user:pass@ip:port для максимальной защиты!")
    try:
        await dp.start_polling(bot)
    finally:
        await browser_pool.stop()
        await bot.session.close()

if __name__ == "__main__":  # <--- ИСПРАВЛЕНО: было "if name == 'main'"
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен.")
