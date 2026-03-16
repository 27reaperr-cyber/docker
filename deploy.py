"""
deploy.py — пайплайн деплоя проекта.

ИСПРАВЛЕНО:
- Явная передача абсолютных путей в process_manager.
- Очистка project_dir перед клонированием при повторном деплое.
- Лучшие сообщения об ошибках с кодами.
"""

import logging
import re
import uuid
from pathlib import Path
from typing import Optional

from database import (
    MAX_PROJECTS_PER_USER,
    count_user_projects,
    create_project,
    update_project_status,
)
from process_manager import (
    LOGS_DIR,
    PROJECTS_DIR,
    clone_repository,
    install_requirements,
    start_container,
)

logger = logging.getLogger(__name__)

# Только HTTPS GitHub/GitLab
ALLOWED_GIT_HOSTS = re.compile(
    r"^https://(github\.com|gitlab\.com)/[\w.\-]+/[\w.\-]+(\.git)?/?$",
    re.IGNORECASE,
)

MAX_ENV_LINES    = 50
MAX_ENV_LINE_LEN = 256


def validate_git_url(url: str) -> tuple[bool, str]:
    url = url.strip()
    if not ALLOWED_GIT_HOSTS.match(url):
        return (
            False,
            "❌ Поддерживаются только GitHub/GitLab HTTPS-ссылки.\n"
            "Пример: <code>https://github.com/user/repo</code>",
        )
    return True, url


def validate_env_lines(raw: str) -> tuple[bool, str, dict]:
    env: dict = {}
    lines = [
        l.strip()
        for l in raw.splitlines()
        if l.strip() and not l.strip().startswith("#")
    ]

    if len(lines) > MAX_ENV_LINES:
        return False, f"Слишком много переменных (макс. {MAX_ENV_LINES}).", {}

    valid_key = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    for line in lines:
        if "=" not in line:
            return False, f"Неверный формат: <code>{line[:50]}</code>\nОжидается KEY=value", {}
        if len(line) > MAX_ENV_LINE_LEN:
            return False, f"Строка слишком длинная (макс. {MAX_ENV_LINE_LEN} символов).", {}

        key, _, value = line.partition("=")
        key = key.strip()
        if not valid_key.match(key):
            return False, f"Недопустимое имя переменной: <code>{key}</code>", {}
        env[key] = value

    return True, "", env


def write_env_file(project_dir: Path, env: dict) -> None:
    env_path = project_dir / ".env"
    content  = "\n".join(f"{k}={v}" for k, v in env.items())
    env_path.write_text(content, encoding="utf-8")
    logger.info(".env записан: %d переменных", len(env))


def generate_project_id() -> str:
    return uuid.uuid4().hex[:10]


def extract_repo_name(url: str) -> str:
    name = url.rstrip("/").rstrip(".git").split("/")[-1]
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:30] or "project"


# ──────────────────────────────────────────────────────────
# Deploy result
# ──────────────────────────────────────────────────────────

class DeployResult:
    def __init__(self, success: bool, message: str, project_id: Optional[str] = None):
        self.success    = success
        self.message    = message
        self.project_id = project_id


# ──────────────────────────────────────────────────────────
# Pipeline
# ──────────────────────────────────────────────────────────

async def deploy_project(
    user_id: int,
    repo_url: str,
    env_vars: dict,
    progress_cb=None,
) -> DeployResult:
    """
    Полный пайплайн деплоя:
    1. Проверка лимита
    2. git clone
    3. pip install (если есть requirements.txt)
    4. Запись .env
    5. docker run
    """

    async def notify(text: str):
        if progress_cb:
            try:
                await progress_cb(text)
            except Exception:
                pass

    # ── Лимит проектов ────────────────────────────────────
    count = await count_user_projects(user_id)
    if count >= MAX_PROJECTS_PER_USER:
        return DeployResult(
            False,
            f"❌ Достигнут лимит: {MAX_PROJECTS_PER_USER} проекта на аккаунт.\n"
            "Удалите существующий проект через «📦 Мои приложения».",
        )

    project_id  = generate_project_id()
    repo_name   = extract_repo_name(repo_url)
    project_dir = PROJECTS_DIR / str(user_id) / project_id
    log_file    = LOGS_DIR / f"{project_id}.log"

    LOGS_DIR.mkdir(exist_ok=True)
    await create_project(project_id, user_id, repo_url, repo_name)

    # ── git clone ─────────────────────────────────────────
    await notify(
        f"⏳ <b>Деплой: {repo_name}</b>\n\n"
        f"📥 Шаг 1/3 — клонирование репозитория…"
    )
    ok, err = await clone_repository(repo_url, project_dir)
    if not ok:
        await update_project_status(project_id, "failed")
        return DeployResult(
            False,
            f"❌ <b>Ошибка клонирования</b>\n\n<code>{err[:400]}</code>",
            project_id,
        )

    # ── pip install ───────────────────────────────────────
    await notify(
        f"⏳ <b>Деплой: {repo_name}</b>\n\n"
        f"✅ Репозиторий склонирован\n"
        f"📦 Шаг 2/3 — установка зависимостей…"
    )
    ok, msg = await install_requirements(project_dir)
    if not ok:
        await update_project_status(project_id, "failed")
        return DeployResult(
            False,
            f"❌ <b>Ошибка установки зависимостей</b>\n\n<code>{msg[:400]}</code>",
            project_id,
        )

    # ── .env ──────────────────────────────────────────────
    if env_vars:
        write_env_file(project_dir, env_vars)

    # ── docker run ────────────────────────────────────────
    await notify(
        f"⏳ <b>Деплой: {repo_name}</b>\n\n"
        f"✅ Репозиторий склонирован\n"
        f"✅ {msg}\n"
        f"🐳 Шаг 3/3 — запуск контейнера…"
    )
    ok, cid_or_err = await start_container(project_id, project_dir, log_file)
    if not ok:
        await update_project_status(project_id, "failed")
        return DeployResult(
            False,
            f"❌ <b>Ошибка запуска контейнера</b>\n\n<code>{cid_or_err[:400]}</code>",
            project_id,
        )

    await update_project_status(project_id, "running", cid_or_err)
    return DeployResult(
        True,
        f"✅ <b>Деплой завершён!</b>\n\n"
        f"📦 <b>{repo_name}</b>\n"
        f"🆔 ID: <code>{project_id}</code>\n"
        f"🐳 Container: <code>{cid_or_err}</code>",
        project_id,
    )
