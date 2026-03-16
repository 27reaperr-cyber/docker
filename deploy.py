"""
deploy.py — пайплайн деплоя проекта.
Вызывается из bot.py после сбора всех данных от пользователя.
Отвечает за валидацию URL, создание .env и оркестрацию шагов.
"""

import logging
import re
import uuid
from pathlib import Path
from typing import Optional

from database import (
    count_user_projects,
    create_project,
    update_project_status,
    MAX_PROJECTS_PER_USER,
)
from process_manager import (
    LOGS_DIR,
    PROJECTS_DIR,
    clone_repository,
    install_requirements,
    start_container,
)

logger = logging.getLogger(__name__)

# Разрешённые Git-хосты
ALLOWED_GIT_HOSTS = re.compile(
    r"^https?://(github\.com|gitlab\.com)/[\w.\-]+/[\w.\-]+(\.git)?/?$",
    re.IGNORECASE,
)

# Максимальное количество строк .env
MAX_ENV_LINES = 50
# Максимальная длина одной строки .env
MAX_ENV_LINE_LEN = 256


def validate_git_url(url: str) -> tuple[bool, str]:
    """
    Проверяет URL репозитория.
    Допускаются только GitHub и GitLab HTTPS-ссылки.
    """
    url = url.strip()
    if not ALLOWED_GIT_HOSTS.match(url):
        return (
            False,
            "❌ Поддерживаются только GitHub/GitLab HTTPS-ссылки.\n"
            "Пример: https://github.com/user/repo",
        )
    return True, url


def validate_env_lines(raw: str) -> tuple[bool, str, dict]:
    """
    Парсит и валидирует env-переменные.
    Формат: KEY=value (одна пара на строку).
    Возвращает (ok, error_message, env_dict).
    """
    env: dict = {}
    lines = [l.strip() for l in raw.splitlines() if l.strip() and not l.startswith("#")]

    if len(lines) > MAX_ENV_LINES:
        return False, f"Слишком много переменных (макс. {MAX_ENV_LINES}).", {}

    valid_key = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    for line in lines:
        if "=" not in line:
            return False, f"Неверный формат строки: `{line[:40]}`\nОжидается KEY=value", {}
        if len(line) > MAX_ENV_LINE_LEN:
            return False, f"Строка слишком длинная: `{line[:40]}…`", {}

        key, _, value = line.partition("=")
        key = key.strip()
        if not valid_key.match(key):
            return False, f"Недопустимое имя переменной: `{key}`", {}

        env[key] = value

    return True, "", env


def write_env_file(project_dir: Path, env: dict) -> None:
    """Записывает .env файл в директорию проекта."""
    env_path = project_dir / ".env"
    content = "\n".join(f"{k}={v}" for k, v in env.items())
    env_path.write_text(content, encoding="utf-8")
    logger.info(".env записан в %s (%d переменных)", env_path, len(env))


def generate_project_id() -> str:
    """Генерирует короткий уникальный ID проекта."""
    return uuid.uuid4().hex[:10]


def extract_repo_name(url: str) -> str:
    """Извлекает имя репозитория из URL."""
    name = url.rstrip("/").rstrip(".git").split("/")[-1]
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:30] or "project"


# ───────────────────────── Pipeline ──────────────────────

class DeployResult:
    """Результат деплоя проекта."""

    def __init__(self, success: bool, message: str, project_id: Optional[str] = None):
        self.success    = success
        self.message    = message
        self.project_id = project_id


async def deploy_project(
    user_id: int,
    repo_url: str,
    env_vars: dict,
    progress_cb=None,
) -> DeployResult:
    """
    Полный пайплайн деплоя:
    1. Проверка лимита проектов
    2. Клонирование репозитория
    3. pip install (если есть requirements.txt)
    4. Запись .env
    5. Запуск Docker-контейнера

    progress_cb: async callable(str) — для отправки прогресса в Telegram.
    """

    async def notify(msg: str):
        if progress_cb:
            try:
                await progress_cb(msg)
            except Exception:
                pass

    # Лимит проектов
    count = await count_user_projects(user_id)
    if count >= MAX_PROJECTS_PER_USER:
        return DeployResult(
            False,
            f"❌ Достигнут лимит: максимум {MAX_PROJECTS_PER_USER} проекта на пользователя.\n"
            "Удалите существующий проект через «📦 Мои приложения».",
        )

    project_id  = generate_project_id()
    repo_name   = extract_repo_name(repo_url)
    project_dir = PROJECTS_DIR / str(user_id) / project_id
    log_file    = LOGS_DIR / f"{project_id}.log"

    LOGS_DIR.mkdir(exist_ok=True)

    # Записываем проект в БД (статус: deploying)
    await create_project(project_id, user_id, repo_url, repo_name)

    # ── Шаг 1: git clone ──────────────────────────────────
    await notify("📥 Клонирование репозитория…")
    ok, err = await clone_repository(repo_url, project_dir)
    if not ok:
        await update_project_status(project_id, "failed")
        return DeployResult(False, f"❌ Ошибка клонирования:\n<code>{err[:400]}</code>", project_id)

    # ── Шаг 2: pip install ────────────────────────────────
    await notify("📦 Установка зависимостей…")
    ok, msg = await install_requirements(project_dir)
    if not ok:
        await update_project_status(project_id, "failed")
        return DeployResult(
            False,
            f"❌ Ошибка установки зависимостей:\n<code>{msg[:400]}</code>",
            project_id,
        )
    await notify(f"✅ {msg}")

    # ── Шаг 3: .env ───────────────────────────────────────
    if env_vars:
        write_env_file(project_dir, env_vars)
        await notify(f"📝 .env создан ({len(env_vars)} переменных).")

    # ── Шаг 4: docker run ─────────────────────────────────
    await notify("🐳 Запуск Docker-контейнера…")
    ok, cid_or_err = await start_container(project_id, project_dir, log_file)
    if not ok:
        await update_project_status(project_id, "failed")
        return DeployResult(
            False,
            f"❌ Ошибка запуска контейнера:\n<code>{cid_or_err[:400]}</code>",
            project_id,
        )

    await update_project_status(project_id, "running", cid_or_err)
    return DeployResult(
        True,
        f"✅ Проект <b>{repo_name}</b> успешно задеплоен!\n"
        f"🆔 ID: <code>{project_id}</code>\n"
        f"🐳 Container: <code>{cid_or_err}</code>",
        project_id,
    )
