"""
bot.py — основной файл Telegram-бота.
Технологии: aiogram 3.x, FSM, polling.
Функции: деплой, управление проектами, статус сервера.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

import psutil
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from dotenv import load_dotenv

import database as db
import process_manager as pm
from deploy import DeployResult, deploy_project, validate_env_lines, validate_git_url

# ─────────────────────── Настройка ───────────────────────

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.critical("BOT_TOKEN не задан! Установите переменную окружения.")
    sys.exit(1)

ALLOWED_USERS_RAW = os.getenv("ALLOWED_USERS", "")
ALLOWED_USERS: set[int] = (
    {int(uid.strip()) for uid in ALLOWED_USERS_RAW.split(",") if uid.strip().isdigit()}
    if ALLOWED_USERS_RAW
    else set()
)

Path("projects").mkdir(exist_ok=True)
Path("logs").mkdir(exist_ok=True)

# ─────────────────────── FSM States ──────────────────────

class DeployStates(StatesGroup):
    waiting_repo_url = State()   # Ожидание ссылки на репозиторий
    waiting_env_vars = State()   # Ожидание env-переменных


# ─────────────────────── Keyboards ───────────────────────

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🚀 Deploy проект"), KeyboardButton(text="📦 Мои приложения")],
        [KeyboardButton(text="📊 Статус сервера"), KeyboardButton(text="ℹ️ Помощь")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Выберите действие…",
)


def project_inline(project_id: str) -> InlineKeyboardMarkup:
    """Inline-клавиатура управления проектом."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="▶ Запустить",    callback_data=f"start:{project_id}"),
            InlineKeyboardButton(text="⏹ Остановить",   callback_data=f"stop:{project_id}"),
        ],
        [
            InlineKeyboardButton(text="🔄 Перезапустить", callback_data=f"restart:{project_id}"),
            InlineKeyboardButton(text="📜 Логи",          callback_data=f"logs:{project_id}"),
        ],
        [
            InlineKeyboardButton(text="🗑 Удалить",       callback_data=f"delete:{project_id}"),
            InlineKeyboardButton(text="← Назад",          callback_data="back_to_list"),
        ],
    ])


def confirm_delete_inline(project_id: str) -> InlineKeyboardMarkup:
    """Подтверждение удаления проекта."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"confirm_delete:{project_id}"),
            InlineKeyboardButton(text="❌ Отмена",       callback_data=f"manage:{project_id}"),
        ]
    ])


# ─────────────────────── Middleware guard ────────────────

async def check_access(user_id: int) -> bool:
    """Проверяет, разрешён ли доступ пользователю."""
    if not ALLOWED_USERS:
        return True   # Если список пуст — открытый доступ
    return user_id in ALLOWED_USERS


# ─────────────────────── Bot & DP ────────────────────────

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())


# ─────────────────────── Handlers ────────────────────────

@dp.message(Command("start"))
async def cmd_start(msg: Message) -> None:
    if not await check_access(msg.from_user.id):
        await msg.answer("⛔ Доступ запрещён.")
        return

    await db.upsert_user(msg.from_user.id, msg.from_user.username)
    await msg.answer(
        f"👋 Привет, <b>{msg.from_user.first_name}</b>!\n\n"
        "Я помогу тебе развернуть Git-проекты прямо с Telegram.\n"
        "Используй меню ниже 👇",
        reply_markup=MAIN_KEYBOARD,
    )


# ── Deploy проект ─────────────────────────────────────────

@dp.message(F.text == "🚀 Deploy проект")
async def cmd_deploy(msg: Message, state: FSMContext) -> None:
    if not await check_access(msg.from_user.id):
        return

    count = await db.count_user_projects(msg.from_user.id)
    if count >= db.MAX_PROJECTS_PER_USER:
        await msg.answer(
            f"⚠️ У вас уже {count} проект(а). Максимум — {db.MAX_PROJECTS_PER_USER}.\n"
            "Удалите один через «📦 Мои приложения»."
        )
        return

    await state.set_state(DeployStates.waiting_repo_url)
    await msg.answer(
        "📎 Введите ссылку на Git-репозиторий:\n\n"
        "Поддерживаются:\n"
        "• https://github.com/user/repo\n"
        "• https://gitlab.com/user/repo\n\n"
        "Или /cancel для отмены.",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(DeployStates.waiting_repo_url)
async def process_repo_url(msg: Message, state: FSMContext) -> None:
    if msg.text and msg.text.strip() == "/cancel":
        await state.clear()
        await msg.answer("❌ Деплой отменён.", reply_markup=MAIN_KEYBOARD)
        return

    ok, result = validate_git_url(msg.text or "")
    if not ok:
        await msg.answer(result)
        return

    await state.update_data(repo_url=result)
    await state.set_state(DeployStates.waiting_env_vars)
    await msg.answer(
        "🔧 Введите переменные окружения (по одной на строку):\n\n"
        "<code>TOKEN=123456\nAPI_KEY=abc\nDEBUG=true</code>\n\n"
        "Или отправьте <b>done</b>, если переменные не нужны.\n"
        "Для отмены — /cancel",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(DeployStates.waiting_env_vars)
async def process_env_vars(msg: Message, state: FSMContext) -> None:
    text = (msg.text or "").strip()

    if text == "/cancel":
        await state.clear()
        await msg.answer("❌ Деплой отменён.", reply_markup=MAIN_KEYBOARD)
        return

    # Пустой ввод или "done" — без переменных
    env_vars: dict = {}
    if text.lower() != "done" and text:
        ok, err, env_vars = validate_env_lines(text)
        if not ok:
            await msg.answer(f"❌ Ошибка в env-переменных:\n{err}\n\nПопробуйте ещё раз.")
            return

    data     = await state.get_data()
    repo_url = data["repo_url"]
    await state.clear()

    status_msg = await msg.answer("⏳ Начинаю деплой…", reply_markup=MAIN_KEYBOARD)

    # Прогресс-коллбэк — редактирует сообщение
    async def progress(text_: str):
        try:
            await status_msg.edit_text(text_)
        except Exception:
            pass

    result: DeployResult = await deploy_project(
        user_id     = msg.from_user.id,
        repo_url    = repo_url,
        env_vars    = env_vars,
        progress_cb = progress,
    )

    await msg.answer(result.message, reply_markup=MAIN_KEYBOARD)


# ── Мои приложения ────────────────────────────────────────

@dp.message(F.text == "📦 Мои приложения")
async def cmd_my_apps(msg: Message) -> None:
    if not await check_access(msg.from_user.id):
        return

    projects = await db.get_user_projects(msg.from_user.id)
    if not projects:
        await msg.answer("📭 У вас нет активных проектов.\nНажмите «🚀 Deploy проект» для деплоя.")
        return

    for p in projects:
        real_status = await pm.get_container_status(p["project_id"])
        status_icon = {
            "running":    "🟢",
            "exited":     "🔴",
            "restarting": "🟡",
            "paused":     "⏸",
        }.get(real_status, "⚪")

        text = (
            f"{status_icon} <b>{p['name']}</b>\n"
            f"🆔 <code>{p['project_id']}</code>\n"
            f"📁 {p['repo_url']}\n"
            f"📅 {p['created_at'][:10]}"
        )
        await msg.answer(
            text,
            reply_markup=project_inline(p["project_id"]),
        )


# ── Статус сервера ────────────────────────────────────────

@dp.message(F.text == "📊 Статус сервера")
async def cmd_server_status(msg: Message) -> None:
    if not await check_access(msg.from_user.id):
        return

    cpu     = psutil.cpu_percent(interval=0.5)
    ram     = psutil.virtual_memory()
    disk    = psutil.disk_usage("/")

    # Считаем контейнеры бота
    code, out, _ = await pm._run("docker", "ps", "-q", "--filter", "name=tghost_")
    containers   = len([l for l in out.splitlines() if l.strip()])

    def bar(pct: float) -> str:
        filled = int(pct / 10)
        return "█" * filled + "░" * (10 - filled)

    text = (
        "📊 <b>Статус сервера</b>\n\n"
        f"🖥 <b>CPU:</b>  {cpu:5.1f}%  {bar(cpu)}\n"
        f"💾 <b>RAM:</b>  {ram.percent:5.1f}%  {bar(ram.percent)}\n"
        f"   └ {ram.used // 1024**2} MB / {ram.total // 1024**2} MB\n"
        f"💿 <b>Диск:</b> {disk.percent:5.1f}%  {bar(disk.percent)}\n"
        f"   └ {disk.used // 1024**3:.1f} GB / {disk.total // 1024**3:.1f} GB\n\n"
        f"🐳 <b>Контейнеров:</b> {containers}"
    )
    await msg.answer(text)


# ── Помощь ────────────────────────────────────────────────

@dp.message(F.text == "ℹ️ Помощь")
async def cmd_help(msg: Message) -> None:
    await msg.answer(
        "ℹ️ <b>Справка по боту</b>\n\n"
        "🚀 <b>Deploy проект</b> — задеплоить GitHub/GitLab репозиторий.\n"
        "📦 <b>Мои приложения</b> — список проектов с управлением.\n"
        "📊 <b>Статус сервера</b> — ресурсы CPU / RAM / Disk.\n\n"
        "<b>Ограничения на проект:</b>\n"
        "• RAM: 512 MB\n"
        "• CPU: 0.5 ядра\n"
        f"• Максимум проектов: {db.MAX_PROJECTS_PER_USER}\n\n"
        "<b>Поддерживаемые хостинги:</b> GitHub, GitLab\n\n"
        "По вопросам: /start для перезапуска.",
    )


# ── Inline callbacks ──────────────────────────────────────

@dp.callback_query(F.data.startswith("manage:"))
async def cb_manage(cq: CallbackQuery) -> None:
    project_id = cq.data.split(":", 1)[1]
    project    = await db.get_project(project_id)
    if not project:
        await cq.answer("Проект не найден.", show_alert=True)
        return

    status = await pm.get_container_status(project_id)
    await cq.message.edit_text(
        f"⚙️ <b>{project['name']}</b>\n"
        f"🆔 <code>{project_id}</code>\n"
        f"📁 {project['repo_url']}\n"
        f"🔵 Статус: <b>{status}</b>",
        reply_markup=project_inline(project_id),
    )
    await cq.answer()


@dp.callback_query(F.data == "back_to_list")
async def cb_back(cq: CallbackQuery) -> None:
    await cq.message.delete()
    await cq.answer()


@dp.callback_query(F.data.startswith("start:"))
async def cb_start(cq: CallbackQuery) -> None:
    project_id = cq.data.split(":", 1)[1]
    project    = await db.get_project(project_id)
    if not project:
        await cq.answer("Проект не найден.", show_alert=True)
        return

    await cq.answer("▶ Запускаю…")
    project_dir = pm.PROJECTS_DIR / str(project["user_id"]) / project_id
    log_file    = pm.LOGS_DIR / f"{project_id}.log"
    ok, cid_or_err = await pm.start_container(project_id, project_dir, log_file)

    if ok:
        await db.update_project_status(project_id, "running", cid_or_err)
        await cq.message.answer(f"✅ Проект <code>{project_id}</code> запущен.")
    else:
        await cq.message.answer(
            f"❌ Не удалось запустить:\n<code>{cid_or_err[:300]}</code>"
        )


@dp.callback_query(F.data.startswith("stop:"))
async def cb_stop(cq: CallbackQuery) -> None:
    project_id = cq.data.split(":", 1)[1]
    await cq.answer("⏹ Останавливаю…")
    ok, msg = await pm.stop_container(project_id)
    icon = "✅" if ok else "❌"
    await cq.message.answer(f"{icon} {msg}")


@dp.callback_query(F.data.startswith("restart:"))
async def cb_restart(cq: CallbackQuery) -> None:
    project_id = cq.data.split(":", 1)[1]
    await cq.answer("🔄 Перезапускаю…")
    ok, msg = await pm.restart_container(project_id)
    icon = "✅" if ok else "❌"
    await cq.message.answer(f"{icon} {msg}")


@dp.callback_query(F.data.startswith("logs:"))
async def cb_logs(cq: CallbackQuery) -> None:
    project_id = cq.data.split(":", 1)[1]
    await cq.answer("📜 Получаю логи…")
    logs = await pm.get_logs(project_id, tail=30)
    # Telegram ограничивает сообщение ~4096 символов
    logs_truncated = logs[-3500:] if len(logs) > 3500 else logs
    await cq.message.answer(
        f"📜 <b>Логи</b> <code>{project_id}</code>:\n\n"
        f"<pre>{logs_truncated}</pre>"
    )


@dp.callback_query(F.data.startswith("delete:"))
async def cb_delete(cq: CallbackQuery) -> None:
    project_id = cq.data.split(":", 1)[1]
    await cq.message.edit_text(
        f"⚠️ Вы уверены, что хотите удалить проект <code>{project_id}</code>?\n"
        "Все файлы и контейнер будут уничтожены!",
        reply_markup=confirm_delete_inline(project_id),
    )
    await cq.answer()


@dp.callback_query(F.data.startswith("confirm_delete:"))
async def cb_confirm_delete(cq: CallbackQuery) -> None:
    project_id = cq.data.split(":", 1)[1]
    project    = await db.get_project(project_id)
    if not project:
        await cq.answer("Проект не найден.", show_alert=True)
        return

    await cq.answer("🗑 Удаляю…")
    ok, msg = await pm.remove_project(project_id, project["user_id"])
    if ok:
        await db.delete_project(project_id)
        await cq.message.edit_text(f"✅ Проект <code>{project_id}</code> удалён.")
    else:
        await cq.message.answer(f"❌ Ошибка удаления: {msg}")


# ── Отмена FSM в любом состоянии ─────────────────────────

@dp.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(msg: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current is None:
        await msg.answer("Нечего отменять.", reply_markup=MAIN_KEYBOARD)
        return
    await state.clear()
    await msg.answer("❌ Действие отменено.", reply_markup=MAIN_KEYBOARD)


# ── Fallback ──────────────────────────────────────────────

@dp.message()
async def fallback(msg: Message) -> None:
    await msg.answer(
        "Используйте кнопки меню или /start для перезапуска.",
        reply_markup=MAIN_KEYBOARD,
    )


# ─────────────────────── Entry point ─────────────────────

async def main() -> None:
    logger.info("Инициализация базы данных…")
    await db.init_db()

    logger.info("Бот запускается (polling)…")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен.")
