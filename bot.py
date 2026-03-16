"""
bot.py — основной файл Telegram-бота.

ИСПРАВЛЕНИЯ v3:
- StateFilter() явно на всех FSM-хендлерах — State-объект без обёртки
  в некоторых версиях aiogram 3 не применяется как фильтр.
- fallback охраняется StateFilter(default_state) — не перехватывает
  сообщения внутри FSM-диалога.
- Regex для URL расширен: разрешает любые символы GitHub-юзернеймов
  (цифры в начале, точки, дефисы).
- Кнопки меню ("🚀 Deploy проект" и др.) добавлены StateFilter(default_state),
  чтобы не прерывать активный FSM-диалог.
- /cancel работает в любом состоянии через StateFilter("*").
"""

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import psutil
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup, default_state
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    TelegramObject,
)
from dotenv import load_dotenv

import database as db
import process_manager as pm
from deploy import DeployResult, deploy_project, validate_env_lines, validate_git_url

# ──────────────────────────────────────────────────────────
# Настройка
# ──────────────────────────────────────────────────────────

load_dotenv()

Path("projects").mkdir(exist_ok=True)
Path("logs").mkdir(exist_ok=True)

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
    logger.critical("BOT_TOKEN не задан!")
    sys.exit(1)

ALLOWED_USERS_RAW = os.getenv("ALLOWED_USERS", "")
ALLOWED_USERS: set[int] = (
    {int(u.strip()) for u in ALLOWED_USERS_RAW.split(",") if u.strip().isdigit()}
    if ALLOWED_USERS_RAW else set()
)

# Ограничение одновременных деплоев — не кладём VPS при наплыве
DEPLOY_SEMAPHORE = asyncio.Semaphore(3)


# ──────────────────────────────────────────────────────────
# Rate Limit Middleware
# ──────────────────────────────────────────────────────────

class RateLimitMiddleware(BaseMiddleware):
    """
    Один запрос в секунду на пользователя.
    Для Message молча игнорирует лишние; для CallbackQuery отвечает всплывашкой.
    """
    def __init__(self, rate: float = 1.0):
        self._last: dict[int, float] = {}
        self._rate = rate

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user:
            now  = time.monotonic()
            last = self._last.get(user.id, 0.0)
            if now - last < self._rate:
                if isinstance(event, CallbackQuery):
                    await event.answer("⏳ Не так быстро!", show_alert=False)
                return None
            self._last[user.id] = now
        return await handler(event, data)


# ──────────────────────────────────────────────────────────
# TTL-кэш для статуса сервера (не спамим psutil)
# ──────────────────────────────────────────────────────────

_server_cache: dict = {}
_SERVER_CACHE_TTL = 10.0   # секунд


async def _get_server_stats() -> dict:
    now = time.monotonic()
    if _server_cache and now - _server_cache.get("_ts", 0) < _SERVER_CACHE_TTL:
        return _server_cache

    # psutil блокирует I/O — выносим в thread
    def _collect():
        return {
            "cpu":  psutil.cpu_percent(interval=0.3),
            "ram":  psutil.virtual_memory(),
            "disk": psutil.disk_usage("/"),
            "_ts":  time.monotonic(),
        }

    data = await asyncio.to_thread(_collect)
    _server_cache.clear()
    _server_cache.update(data)
    return _server_cache


# ──────────────────────────────────────────────────────────
# FSM
# ──────────────────────────────────────────────────────────

class DeployStates(StatesGroup):
    waiting_repo_url = State()
    waiting_env_vars = State()


# ──────────────────────────────────────────────────────────
# Keyboards
# ──────────────────────────────────────────────────────────

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🚀 Deploy проект"), KeyboardButton(text="📦 Мои приложения")],
        [KeyboardButton(text="📊 Статус сервера"), KeyboardButton(text="ℹ️ Помощь")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Выберите действие…",
)

STATUS_ICON = {
    "running":    "🟢",
    "exited":     "🔴",
    "restarting": "🟡",
    "paused":     "⏸",
    "deploying":  "🔵",
    "failed":     "💀",
    "stopped":    "⚫",
}


def _status_icon(status: str) -> str:
    return STATUS_ICON.get(status, "⚪")


def _bar(pct: float) -> str:
    filled = int(pct / 10)
    return "█" * filled + "░" * (10 - filled)


def _project_card_text(project: dict, status: str, stats: Optional[dict] = None) -> str:
    icon  = _status_icon(status)
    lines = [
        f"{icon} <b>{project['name']}</b>",
        f"🆔 <code>{project['project_id']}</code>",
        f"📁 {project['repo_url']}",
        f"📅 {project['created_at'][:10]}",
        f"🔵 Статус: <b>{status}</b>",
    ]
    if stats and stats.get("cpu") != "—":
        lines.append(f"🖥 CPU: {stats['cpu']}  💾 RAM: {stats['mem']}")
    return "\n".join(lines)


def _project_inline(project_id: str, loading: Optional[str] = None) -> InlineKeyboardMarkup:
    """
    loading: если задан — показываем текст вместо кнопок (индикация загрузки).
    """
    if loading:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=loading, callback_data="noop")]
        ])

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="▶ Запустить",     callback_data=f"start:{project_id}"),
            InlineKeyboardButton(text="⏹ Остановить",    callback_data=f"stop:{project_id}"),
        ],
        [
            InlineKeyboardButton(text="🔄 Перезапустить", callback_data=f"restart:{project_id}"),
            InlineKeyboardButton(text="📜 Логи",          callback_data=f"logs:{project_id}"),
        ],
        [
            InlineKeyboardButton(text="🔃 Обновить",      callback_data=f"refresh:{project_id}"),
            InlineKeyboardButton(text="🗑 Удалить",        callback_data=f"delete:{project_id}"),
        ],
    ])


def _confirm_delete_inline(project_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить",  callback_data=f"confirm_delete:{project_id}"),
            InlineKeyboardButton(text="❌ Отмена",        callback_data=f"refresh:{project_id}"),
        ]
    ])


# ──────────────────────────────────────────────────────────
# Bot & Dispatcher
# ──────────────────────────────────────────────────────────

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())

# Регистрируем middleware
dp.message.middleware(RateLimitMiddleware(rate=0.7))
dp.callback_query.middleware(RateLimitMiddleware(rate=0.5))


async def _check_access(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS


# ──────────────────────────────────────────────────────────
# /start
# ──────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(msg: Message) -> None:
    if not await _check_access(msg.from_user.id):
        await msg.answer("⛔ Доступ запрещён.")
        return
    await db.upsert_user(msg.from_user.id, msg.from_user.username)
    await msg.answer(
        f"👋 Привет, <b>{msg.from_user.first_name}</b>!\n\n"
        "Разворачиваю Git-проекты прямо из Telegram.\n"
        "Используй меню ниже 👇",
        reply_markup=MAIN_KB,
    )


# ──────────────────────────────────────────────────────────
# Deploy — FSM
# ──────────────────────────────────────────────────────────

@dp.message(StateFilter(default_state), F.text == "🚀 Deploy проект")
async def cmd_deploy(msg: Message, state: FSMContext) -> None:
    if not await _check_access(msg.from_user.id):
        return
    count = await db.count_user_projects(msg.from_user.id)
    if count >= db.MAX_PROJECTS_PER_USER:
        await msg.answer(
            f"⚠️ Достигнут лимит: <b>{db.MAX_PROJECTS_PER_USER}</b> проекта.\n"
            "Удалите один через «📦 Мои приложения».",
        )
        return

    await state.set_state(DeployStates.waiting_repo_url)
    await msg.answer(
        "📎 Введите ссылку на Git-репозиторий:\n\n"
        "<code>https://github.com/user/repo</code>\n"
        "<code>https://gitlab.com/user/repo</code>\n\n"
        "Для отмены: /cancel",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(StateFilter(DeployStates.waiting_repo_url))
async def fsm_repo_url(msg: Message, state: FSMContext) -> None:
    if (msg.text or "").strip() == "/cancel":
        await state.clear()
        await msg.answer("❌ Деплой отменён.", reply_markup=MAIN_KB)
        return

    ok, result = validate_git_url(msg.text or "")
    if not ok:
        await msg.answer(result)
        return

    await state.update_data(repo_url=result)
    await state.set_state(DeployStates.waiting_env_vars)
    await msg.answer(
        "🔧 Введите переменные окружения:\n\n"
        "<code>TOKEN=123456\nAPI_KEY=abc</code>\n\n"
        "Или отправьте <b>done</b> — без переменных.\n"
        "Для отмены: /cancel",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(StateFilter(DeployStates.waiting_env_vars))
async def fsm_env_vars(msg: Message, state: FSMContext) -> None:
    text = (msg.text or "").strip()

    if text == "/cancel":
        await state.clear()
        await msg.answer("❌ Деплой отменён.", reply_markup=MAIN_KB)
        return

    env_vars: dict = {}
    if text.lower() != "done" and text:
        ok, err, env_vars = validate_env_lines(text)
        if not ok:
            await msg.answer(f"❌ Ошибка в переменных:\n{err}\n\nПопробуйте ещё раз.")
            return

    data     = await state.get_data()
    repo_url = data["repo_url"]
    await state.clear()

    # Показываем прогресс — редактируемое сообщение
    progress_msg = await msg.answer("⏳ Подготовка к деплою…", reply_markup=MAIN_KB)

    async def on_progress(text_: str):
        try:
            await progress_msg.edit_text(text_)
        except Exception:
            pass

    # Глобальный семафор — максимум 3 деплоя одновременно
    async with DEPLOY_SEMAPHORE:
        result: DeployResult = await deploy_project(
            user_id     = msg.from_user.id,
            repo_url    = repo_url,
            env_vars    = env_vars,
            progress_cb = on_progress,
        )

    # Финальный результат — отдельным сообщением
    kb = None
    if result.success and result.project_id:
        kb = _project_inline(result.project_id)
    await msg.answer(result.message, reply_markup=kb)


# ──────────────────────────────────────────────────────────
# Мои приложения
# ──────────────────────────────────────────────────────────

@dp.message(StateFilter(default_state), F.text == "📦 Мои приложения")
async def cmd_my_apps(msg: Message) -> None:
    if not await _check_access(msg.from_user.id):
        return

    projects = await db.get_user_projects(msg.from_user.id)
    if not projects:
        await msg.answer(
            "📭 Нет активных проектов.\n"
            "Нажмите «🚀 Deploy проект» для деплоя."
        )
        return

    for p in projects:
        status = await pm.get_container_status(p["project_id"])
        text   = _project_card_text(p, status)
        await msg.answer(text, reply_markup=_project_inline(p["project_id"]))


# ──────────────────────────────────────────────────────────
# Статус сервера
# ──────────────────────────────────────────────────────────

@dp.message(StateFilter(default_state), F.text == "📊 Статус сервера")
async def cmd_server_status(msg: Message) -> None:
    if not await _check_access(msg.from_user.id):
        return

    stats = await _get_server_stats()
    cpu   = stats["cpu"]
    ram   = stats["ram"]
    disk  = stats["disk"]

    code, out, _ = await pm._run("docker", "ps", "-q", "--filter", "name=tghost_")
    containers   = len([l for l in out.splitlines() if l.strip()])

    text = (
        "📊 <b>Статус сервера</b>\n\n"
        f"🖥 <b>CPU</b>   {cpu:5.1f}%  {_bar(cpu)}\n"
        f"💾 <b>RAM</b>   {ram.percent:5.1f}%  {_bar(ram.percent)}\n"
        f"    └ {ram.used // 1024**2} MB / {ram.total // 1024**2} MB\n"
        f"💿 <b>Диск</b>  {disk.percent:5.1f}%  {_bar(disk.percent)}\n"
        f"    └ {disk.used // 1024**3:.1f} GB / {disk.total // 1024**3:.1f} GB\n\n"
        f"🐳 <b>Контейнеров:</b> {containers}"
    )
    refresh_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔃 Обновить", callback_data="refresh_server")]
    ])
    await msg.answer(text, reply_markup=refresh_kb)


@dp.callback_query(F.data == "refresh_server")
async def cb_refresh_server(cq: CallbackQuery) -> None:
    # Инвалидируем кэш принудительно
    _server_cache.clear()
    stats = await _get_server_stats()
    cpu   = stats["cpu"]
    ram   = stats["ram"]
    disk  = stats["disk"]

    code, out, _ = await pm._run("docker", "ps", "-q", "--filter", "name=tghost_")
    containers   = len([l for l in out.splitlines() if l.strip()])

    text = (
        "📊 <b>Статус сервера</b>\n\n"
        f"🖥 <b>CPU</b>   {cpu:5.1f}%  {_bar(cpu)}\n"
        f"💾 <b>RAM</b>   {ram.percent:5.1f}%  {_bar(ram.percent)}\n"
        f"    └ {ram.used // 1024**2} MB / {ram.total // 1024**2} MB\n"
        f"💿 <b>Диск</b>  {disk.percent:5.1f}%  {_bar(disk.percent)}\n"
        f"    └ {disk.used // 1024**3:.1f} GB / {disk.total // 1024**3:.1f} GB\n\n"
        f"🐳 <b>Контейнеров:</b> {containers}"
    )
    refresh_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔃 Обновить", callback_data="refresh_server")]
    ])
    await cq.message.edit_text(text, reply_markup=refresh_kb)
    await cq.answer("Обновлено!")


# ──────────────────────────────────────────────────────────
# Помощь
# ──────────────────────────────────────────────────────────

@dp.message(StateFilter(default_state), F.text == "ℹ️ Помощь")
async def cmd_help(msg: Message) -> None:
    await msg.answer(
        "ℹ️ <b>Справка</b>\n\n"
        "🚀 <b>Deploy проект</b> — задеплоить GitHub/GitLab репозиторий.\n"
        "📦 <b>Мои приложения</b> — список проектов + управление.\n"
        "📊 <b>Статус сервера</b> — CPU / RAM / Disk.\n\n"
        "<b>Ограничения на проект:</b>\n"
        f"• RAM: {pm.CONTAINER_MEMORY} / CPU: {pm.CONTAINER_CPUS} ядра\n"
        f"• Максимум проектов: {db.MAX_PROJECTS_PER_USER}\n\n"
        "<b>Точки входа:</b> main.py, app.py, bot.py, run.py\n"
        "<b>Хостинги:</b> GitHub, GitLab (только HTTPS)\n\n"
        "/cancel — отменить текущее действие",
    )


# ──────────────────────────────────────────────────────────
# Inline callbacks — всё редактирует сообщение в-месте
# ──────────────────────────────────────────────────────────

@dp.callback_query(F.data == "noop")
async def cb_noop(cq: CallbackQuery) -> None:
    await cq.answer()


async def _refresh_card(cq: CallbackQuery, project_id: str) -> None:
    """Обновляет карточку проекта в-месте."""
    project = await db.get_project(project_id)
    if not project:
        await cq.message.edit_text("❌ Проект не найден.")
        return
    status = await pm.get_container_status(project_id)
    stats  = await pm.get_container_stats(project_id)
    text   = _project_card_text(project, status, stats)
    try:
        await cq.message.edit_text(text, reply_markup=_project_inline(project_id))
    except Exception:
        pass  # Текст не изменился — Telegram вернёт ошибку, это норма


@dp.callback_query(F.data.startswith("refresh:"))
async def cb_refresh(cq: CallbackQuery) -> None:
    project_id = cq.data.split(":", 1)[1]
    await _refresh_card(cq, project_id)
    await cq.answer("Обновлено!")


@dp.callback_query(F.data.startswith("start:"))
async def cb_start(cq: CallbackQuery) -> None:
    project_id = cq.data.split(":", 1)[1]
    project    = await db.get_project(project_id)
    if not project:
        await cq.answer("Проект не найден.", show_alert=True)
        return

    # Показываем loading в кнопке
    await cq.message.edit_reply_markup(
        reply_markup=_project_inline(project_id, loading="⏳ Запускаю…")
    )
    await cq.answer()

    project_dir = pm.PROJECTS_DIR / str(project["user_id"]) / project_id
    log_file    = pm.LOGS_DIR / f"{project_id}.log"
    ok, cid_or_err = await pm.start_container(project_id, project_dir, log_file)

    if ok:
        await db.update_project_status(project_id, "running", cid_or_err)
    # В любом случае — обновляем карточку с актуальным статусом
    await _refresh_card(cq, project_id)


@dp.callback_query(F.data.startswith("stop:"))
async def cb_stop(cq: CallbackQuery) -> None:
    project_id = cq.data.split(":", 1)[1]

    await cq.message.edit_reply_markup(
        reply_markup=_project_inline(project_id, loading="⏳ Останавливаю…")
    )
    await cq.answer()

    await pm.stop_container(project_id)
    await _refresh_card(cq, project_id)


@dp.callback_query(F.data.startswith("restart:"))
async def cb_restart(cq: CallbackQuery) -> None:
    project_id = cq.data.split(":", 1)[1]

    await cq.message.edit_reply_markup(
        reply_markup=_project_inline(project_id, loading="⏳ Перезапускаю…")
    )
    await cq.answer()

    await pm.restart_container(project_id)
    await _refresh_card(cq, project_id)


@dp.callback_query(F.data.startswith("logs:"))
async def cb_logs(cq: CallbackQuery) -> None:
    project_id = cq.data.split(":", 1)[1]
    await cq.answer("📜 Получаю логи…")

    logs = await pm.get_logs(project_id, tail=30)
    # Обрезаем: Telegram лимит ~4096 символов
    if len(logs) > 3500:
        logs = "…(обрезано)\n" + logs[-3400:]

    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Назад к проекту", callback_data=f"refresh:{project_id}")]
    ])
    await cq.message.edit_text(
        f"📜 <b>Логи</b> <code>{project_id}</code>:\n\n<pre>{logs}</pre>",
        reply_markup=back_kb,
    )


@dp.callback_query(F.data.startswith("delete:"))
async def cb_delete(cq: CallbackQuery) -> None:
    project_id = cq.data.split(":", 1)[1]
    await cq.message.edit_text(
        f"⚠️ <b>Удалить проект?</b>\n\n"
        f"🆔 <code>{project_id}</code>\n\n"
        "Контейнер и все файлы будут уничтожены безвозвратно.",
        reply_markup=_confirm_delete_inline(project_id),
    )
    await cq.answer()


@dp.callback_query(F.data.startswith("confirm_delete:"))
async def cb_confirm_delete(cq: CallbackQuery) -> None:
    project_id = cq.data.split(":", 1)[1]
    project    = await db.get_project(project_id)
    if not project:
        await cq.answer("Проект не найден.", show_alert=True)
        return

    await cq.message.edit_reply_markup(
        reply_markup=_project_inline(project_id, loading="🗑 Удаляю…")
    )
    await cq.answer()

    ok, msg_text = await pm.remove_project(project_id, project["user_id"])
    if ok:
        await db.delete_project(project_id)
        await cq.message.edit_text(
            f"✅ Проект <code>{project_id}</code> удалён.",
            reply_markup=None,
        )
    else:
        await cq.message.edit_text(
            f"❌ Ошибка удаления:\n<code>{msg_text}</code>",
            reply_markup=_project_inline(project_id),
        )


# ──────────────────────────────────────────────────────────
# /cancel — сброс FSM
# ──────────────────────────────────────────────────────────

@dp.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(msg: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await msg.answer("Нечего отменять.", reply_markup=MAIN_KB)
        return
    await state.clear()
    await msg.answer("❌ Действие отменено.", reply_markup=MAIN_KB)


# ──────────────────────────────────────────────────────────
# Fallback
# ──────────────────────────────────────────────────────────

@dp.message(StateFilter(default_state))
async def fallback(msg: Message) -> None:
    await msg.answer(
        "Используйте кнопки меню или /start.",
        reply_markup=MAIN_KB,
    )


# ──────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────

async def main() -> None:
    logger.info("Инициализация БД…")
    await db.init_db()
    logger.info("Бот запущен (polling).")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен.")
