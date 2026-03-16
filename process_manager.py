"""
process_manager.py — управление Docker-контейнерами проектов.

ИСПРАВЛЕНО:
- Баг с pip: передаём "requirements.txt" (относительный к cwd),
  вместо str(project_dir / "requirements.txt"), который давал двойной путь.
- _run: корректная отмена зависшего процесса через proc.kill().
- get_container_status: быстрый inspect с таймаутом 10с.
- Все команды через create_subprocess_exec — без shell-инъекций.
"""

import asyncio
import logging
import re
import shutil
from pathlib import Path
from typing import Optional

from database import update_project_status

logger = logging.getLogger(__name__)

CONTAINER_MEMORY = "512m"
CONTAINER_CPUS   = "0.5"
CONTAINER_IMAGE  = "python:3.10-slim"

PROJECTS_DIR = Path("projects")
LOGS_DIR     = Path("logs")

ENTRYPOINTS = ("main.py", "app.py", "bot.py", "run.py")


# ──────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────

async def _run(
    *args: str,
    cwd: Optional[Path] = None,
    timeout: int = 120,
) -> tuple[int, str, str]:
    """
    Безопасный запуск внешней команды без shell=True.
    При таймауте процесс корректно завершается через kill().
    """
    proc: Optional[asyncio.subprocess.Process] = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        return (
            proc.returncode or 0,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )
    except asyncio.TimeoutError:
        if proc:
            try:
                proc.kill()
                await proc.communicate()
            except Exception:
                pass
        logger.error("Команда %s превысила таймаут %ds", args[0], timeout)
        return 1, "", f"Timeout after {timeout}s"
    except Exception as exc:
        logger.exception("Ошибка запуска %s: %s", args[0], exc)
        return 1, "", str(exc)


def _safe_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", name)


def container_name(project_id: str) -> str:
    return _safe_name(f"tghost_{project_id}")


# ──────────────────────────────────────────────────────────
# Deploy pipeline steps
# ──────────────────────────────────────────────────────────

async def clone_repository(
    repo_url: str,
    dest: Path,
    timeout: int = 120,
) -> tuple[bool, str]:
    """
    Клонирует репозиторий прямо в dest.
    Создаём только родительскую директорию — git сам создаст dest.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Если dest уже существует (повторный деплой) — очищаем
    if dest.exists():
        shutil.rmtree(dest)

    code, out, err = await _run(
        "git", "clone", "--depth=1", repo_url, str(dest),
        timeout=timeout,
    )
    if code != 0:
        msg = err.strip() or out.strip() or "Неизвестная ошибка git clone"
        logger.error("git clone failed [%s]: %s", repo_url, msg)
        return False, msg

    logger.info("Клонировано: %s -> %s", repo_url, dest)
    return True, "OK"


async def install_requirements(project_dir: Path) -> tuple[bool, str]:
    """
    Устанавливает зависимости из requirements.txt.

    ИСПРАВЛЕНИЕ БАГА: передаём "requirements.txt" как имя файла (без пути),
    потому что cwd уже установлен в project_dir. Предыдущий код передавал
    str(project_dir / "requirements.txt") — относительный путь, который pip
    разворачивал относительно cwd, получая удвоенный путь и FileNotFoundError.
    """
    req_file = project_dir / "requirements.txt"
    if not req_file.exists():
        return True, "requirements.txt не найден — зависимости пропущены."

    code, out, err = await _run(
        "pip", "install", "--quiet", "--no-cache-dir",
        "-r", "requirements.txt",   # <- просто имя файла, cwd=project_dir
        cwd=project_dir,
        timeout=300,
    )
    if code != 0:
        detail = err.strip() or out.strip() or "pip завершился с ошибкой"
        return False, detail

    return True, "Зависимости установлены."


async def find_entrypoint(project_dir: Path) -> Optional[str]:
    for candidate in ENTRYPOINTS:
        if (project_dir / candidate).exists():
            logger.info("Entrypoint найден: %s", candidate)
            return candidate
    return None


async def start_container(
    project_id: str,
    project_dir: Path,
    log_file: Path,
) -> tuple[bool, str]:
    name     = container_name(project_id)
    env_file = project_dir / ".env"
    entry    = await find_entrypoint(project_dir)

    if entry is None:
        candidates = ", ".join(ENTRYPOINTS)
        return False, f"Точка входа не найдена. Ожидается: {candidates}"

    await _run("docker", "rm", "-f", name, timeout=15)

    cmd = [
        "docker", "run", "-d",
        "--name",        name,
        "--memory",      CONTAINER_MEMORY,
        "--memory-swap", CONTAINER_MEMORY,
        "--cpus",        CONTAINER_CPUS,
        "--restart",     "unless-stopped",
        "--log-driver",  "json-file",
        "--log-opt",     "max-size=10m",
        "--log-opt",     "max-file=2",
        "-v", f"{project_dir.resolve()}:/app:ro",
        "-w", "/app",
    ]

    if env_file.exists():
        cmd += ["--env-file", str(env_file.resolve())]

    cmd += [CONTAINER_IMAGE, "python", "-u", entry]

    code, out, err = await _run(*cmd, timeout=60)
    if code != 0:
        msg = err.strip() or out.strip() or "docker run вернул ненулевой код"
        logger.error("docker run failed [%s]: %s", project_id, msg)
        return False, msg

    cid = out.strip()[:12]
    logger.info("Контейнер %s запущен (cid=%s)", name, cid)
    return True, cid


# ──────────────────────────────────────────────────────────
# Container controls
# ──────────────────────────────────────────────────────────

async def stop_container(project_id: str) -> tuple[bool, str]:
    name = container_name(project_id)
    code, _, err = await _run("docker", "stop", "--time", "10", name, timeout=30)
    if code != 0:
        return False, err.strip() or "Не удалось остановить"
    await update_project_status(project_id, "stopped")
    return True, "Контейнер остановлен."


async def restart_container(project_id: str) -> tuple[bool, str]:
    name = container_name(project_id)
    code, _, err = await _run("docker", "restart", "--time", "5", name, timeout=30)
    if code != 0:
        return False, err.strip() or "Не удалось перезапустить"
    await update_project_status(project_id, "running")
    return True, "Контейнер перезапущен."


async def get_container_status(project_id: str) -> str:
    name = container_name(project_id)
    code, out, _ = await _run(
        "docker", "inspect", "--format", "{{.State.Status}}", name,
        timeout=10,
    )
    if code != 0:
        return "stopped"
    return out.strip() or "unknown"


async def get_container_stats(project_id: str) -> dict:
    """Возвращает CPU/RAM контейнера для расширенной карточки."""
    name = container_name(project_id)
    code, out, _ = await _run(
        "docker", "stats", "--no-stream",
        "--format", "{{.CPUPerc}}|{{.MemUsage}}",
        name, timeout=10,
    )
    if code != 0 or not out.strip():
        return {"cpu": "—", "mem": "—"}
    parts = out.strip().split("|")
    return {
        "cpu": parts[0].strip() if len(parts) > 0 else "—",
        "mem": parts[1].strip() if len(parts) > 1 else "—",
    }


async def get_logs(project_id: str, tail: int = 30) -> str:
    name = container_name(project_id)
    code, out, err = await _run(
        "docker", "logs", "--tail", str(tail),
        name, timeout=15,
    )
    if code != 0:
        log_file = LOGS_DIR / f"{project_id}.log"
        if log_file.exists():
            lines = log_file.read_text(errors="replace").splitlines()
            return "\n".join(lines[-tail:]) or "Логи пусты."
        return err.strip() or "Логи недоступны."

    combined = (out.strip() + "\n" + err.strip()).strip()
    lines    = combined.splitlines()[-tail:]
    return "\n".join(lines) or "Логи пусты."


async def remove_project(project_id: str, user_id: int) -> tuple[bool, str]:
    name = container_name(project_id)
    await _run("docker", "rm", "-f", name, timeout=20)

    project_dir = PROJECTS_DIR / str(user_id) / project_id
    if project_dir.exists():
        try:
            shutil.rmtree(project_dir)
        except Exception as exc:
            logger.error("Не удалось удалить %s: %s", project_dir, exc)
            return False, f"Ошибка удаления файлов: {exc}"

    (LOGS_DIR / f"{project_id}.log").unlink(missing_ok=True)
    logger.info("Проект %s удалён.", project_id)
    return True, "Проект удалён."
