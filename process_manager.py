"""
process_manager.py — управление Docker-контейнерами проектов.
Все команды выполняются через asyncio.create_subprocess_exec,
что исключает shell-инъекции.
"""

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Optional

from database import update_project_status

logger = logging.getLogger(__name__)

# Ограничения контейнера
CONTAINER_MEMORY = "512m"
CONTAINER_CPUS   = "0.5"
CONTAINER_IMAGE  = "python:3.10-slim"

PROJECTS_DIR = Path("projects")
LOGS_DIR     = Path("logs")


# ─────────────────────── Helpers ─────────────────────────

async def _run(
    *args: str,
    cwd: Optional[Path] = None,
    timeout: int = 120,
) -> tuple[int, str, str]:
    """
    Безопасный запуск внешней команды без shell.
    Возвращает (returncode, stdout, stderr).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")
    except asyncio.TimeoutError:
        logger.error("Команда %s превысила таймаут %d с", args[0], timeout)
        return 1, "", f"Timeout after {timeout}s"
    except Exception as exc:
        logger.exception("Ошибка запуска команды %s: %s", args, exc)
        return 1, "", str(exc)


def _sanitize_container_name(name: str) -> str:
    """Оставляет только безопасные символы для имени контейнера."""
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", name)


def container_name(project_id: str) -> str:
    return _sanitize_container_name(f"tghost_{project_id}")


# ─────────────────────── Deploy ──────────────────────────

async def clone_repository(
    repo_url: str,
    dest: Path,
    timeout: int = 120,
) -> tuple[bool, str]:
    """
    Клонирует репозиторий в dest.
    Возвращает (success, message).
    """
    dest.mkdir(parents=True, exist_ok=True)

    code, out, err = await _run(
        "git", "clone", "--depth=1", repo_url, str(dest),
        timeout=timeout,
    )
    if code != 0:
        msg = err.strip() or out.strip() or "Неизвестная ошибка git clone"
        logger.error("git clone failed: %s", msg)
        return False, msg
    logger.info("Репозиторий склонирован в %s", dest)
    return True, "OK"


async def install_requirements(project_dir: Path) -> tuple[bool, str]:
    """
    Устанавливает зависимости из requirements.txt (если файл существует).
    """
    req_file = project_dir / "requirements.txt"
    if not req_file.exists():
        return True, "requirements.txt не найден, пропуск установки."

    code, out, err = await _run(
        "pip", "install", "--quiet", "-r", str(req_file),
        cwd=project_dir,
        timeout=180,
    )
    if code != 0:
        return False, err.strip() or "pip install завершился с ошибкой"
    return True, "Зависимости установлены."


async def find_entrypoint(project_dir: Path) -> Optional[str]:
    """
    Ищет точку входа проекта в порядке приоритета:
    main.py → app.py → bot.py → run.py
    """
    for candidate in ("main.py", "app.py", "bot.py", "run.py"):
        if (project_dir / candidate).exists():
            return candidate
    return None


async def start_container(
    project_id: str,
    project_dir: Path,
    log_file: Path,
) -> tuple[bool, str]:
    """
    Запускает Docker-контейнер для проекта.
    Возвращает (success, container_id_or_error).
    """
    name      = container_name(project_id)
    env_file  = project_dir / ".env"
    entry     = await find_entrypoint(project_dir)

    if entry is None:
        return False, "Точка входа не найдена (main.py/app.py/bot.py/run.py)."

    # Убиваем старый контейнер с таким же именем, если есть
    await _run("docker", "rm", "-f", name)

    cmd = [
        "docker", "run", "-d",
        "--name", name,
        "--memory", CONTAINER_MEMORY,
        "--cpus", CONTAINER_CPUS,
        "--restart", "unless-stopped",
        "--log-driver", "json-file",
        "--log-opt", "max-size=10m",
        "--log-opt", "max-file=3",
        "-v", f"{project_dir.resolve()}:/app:ro",
        "-w", "/app",
    ]

    if env_file.exists():
        cmd += ["--env-file", str(env_file.resolve())]

    cmd += [CONTAINER_IMAGE, "python", entry]

    code, out, err = await _run(*cmd, timeout=60)
    if code != 0:
        msg = err.strip() or "docker run завершился с ошибкой"
        logger.error("docker run failed [%s]: %s", project_id, msg)
        return False, msg

    cid = out.strip()[:12]
    logger.info("Контейнер %s запущен (id=%s)", name, cid)
    return True, cid


# ─────────────────────── Controls ────────────────────────

async def stop_container(project_id: str) -> tuple[bool, str]:
    """Останавливает контейнер проекта."""
    name = container_name(project_id)
    code, _, err = await _run("docker", "stop", name, timeout=30)
    if code != 0:
        return False, err.strip()
    await update_project_status(project_id, "stopped")
    return True, "Остановлен."


async def restart_container(project_id: str) -> tuple[bool, str]:
    """Перезапускает контейнер проекта."""
    name = container_name(project_id)
    code, _, err = await _run("docker", "restart", name, timeout=30)
    if code != 0:
        return False, err.strip()
    await update_project_status(project_id, "running")
    return True, "Перезапущен."


async def get_container_status(project_id: str) -> str:
    """Возвращает статус контейнера через docker inspect."""
    name = container_name(project_id)
    code, out, _ = await _run(
        "docker", "inspect", "--format", "{{.State.Status}}", name
    )
    if code != 0:
        return "stopped"
    return out.strip() or "unknown"


async def get_logs(project_id: str, tail: int = 30) -> str:
    """Возвращает последние N строк логов контейнера."""
    name = container_name(project_id)
    code, out, err = await _run(
        "docker", "logs", "--tail", str(tail), name, timeout=15
    )
    if code != 0:
        # Попробуем файловый лог как резервный вариант
        log_file = LOGS_DIR / f"{project_id}.log"
        if log_file.exists():
            lines = log_file.read_text(errors="replace").splitlines()
            return "\n".join(lines[-tail:]) or "Логи пусты."
        return err.strip() or "Логи недоступны."

    combined = (out + "\n" + err).strip()
    lines = combined.splitlines()[-tail:]
    return "\n".join(lines) or "Логи пусты."


async def remove_project(project_id: str, user_id: int) -> tuple[bool, str]:
    """
    Полностью удаляет проект:
    останавливает и удаляет контейнер + директорию.
    """
    name = container_name(project_id)

    # Остановить и удалить контейнер (ошибки игнорируются)
    await _run("docker", "rm", "-f", name, timeout=30)

    # Удалить директорию проекта
    project_dir = PROJECTS_DIR / str(user_id) / project_id
    if project_dir.exists():
        try:
            shutil.rmtree(project_dir)
        except Exception as exc:
            logger.error("Не удалось удалить директорию %s: %s", project_dir, exc)
            return False, f"Ошибка удаления файлов: {exc}"

    # Удалить лог-файл
    log_file = LOGS_DIR / f"{project_id}.log"
    log_file.unlink(missing_ok=True)

    logger.info("Проект %s удалён.", project_id)
    return True, "Проект удалён."
