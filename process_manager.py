"""
process_manager.py — управление проектами.

Два режима запуска (выбирается автоматически при старте):
  • DOCKER  — docker run с изоляцией памяти/CPU
  • SUBPROCESS — обычный python-процесс (если Docker недоступен)

Subprocess-режим полностью поддерживает start/stop/restart/logs/remove.
PIDs хранятся в памяти (словарь _procs) и в PID-файлах на диске,
чтобы пережить перезапуск самого бота.
"""

import asyncio
import logging
import os
import re
import shutil
import signal
import sys
from pathlib import Path
from typing import Optional

from database import update_project_status

logger = logging.getLogger(__name__)

CONTAINER_MEMORY = "512m"
CONTAINER_CPUS   = "0.5"
CONTAINER_IMAGE  = "python:3.10-slim"

PROJECTS_DIR = Path("projects")
LOGS_DIR     = Path("logs")
PIDS_DIR     = Path("pids")         # PID-файлы subprocess-режима

ENTRYPOINTS  = ("main.py", "app.py", "bot.py", "run.py")

# Живые asyncio.Process объекты: project_id → Process
_procs: dict[str, asyncio.subprocess.Process] = {}

# Флаг режима — устанавливается один раз в check_docker()
_use_docker: Optional[bool] = None


# ──────────────────────────────────────────────────────────
# Runtime mode detection
# ──────────────────────────────────────────────────────────

async def check_docker() -> bool:
    """
    Проверяет доступность Docker.
    Результат кэшируется в _use_docker.
    Вызывать один раз при старте бота.
    """
    global _use_docker
    if _use_docker is not None:
        return _use_docker

    code, _, _ = await _run("docker", "info", timeout=8)
    _use_docker = (code == 0)

    mode = "DOCKER" if _use_docker else "SUBPROCESS"
    logger.info("Режим запуска проектов: %s", mode)
    if not _use_docker:
        logger.warning(
            "Docker недоступен (/var/run/docker.sock не найден или демон не запущен). "
            "Проекты будут запускаться как subprocess."
        )
        PIDS_DIR.mkdir(exist_ok=True)
    return _use_docker


def running_mode() -> str:
    """Возвращает строку режима для отображения в боте."""
    if _use_docker is None:
        return "неизвестен"
    return "🐳 Docker" if _use_docker else "⚙️ Subprocess"


# ──────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────

async def _run(
    *args: str,
    cwd: Optional[Path] = None,
    timeout: int = 120,
) -> tuple[int, str, str]:
    proc: Optional[asyncio.subprocess.Process] = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
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
        logger.exception("Ошибка _run(%s): %s", args[0], exc)
        return 1, "", str(exc)


def _safe_name(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", s)


def container_name(project_id: str) -> str:
    return _safe_name(f"tghost_{project_id}")


def _pid_file(project_id: str) -> Path:
    return PIDS_DIR / f"{project_id}.pid"


def _load_pid(project_id: str) -> Optional[int]:
    f = _pid_file(project_id)
    try:
        return int(f.read_text().strip()) if f.exists() else None
    except Exception:
        return None


def _save_pid(project_id: str, pid: int) -> None:
    PIDS_DIR.mkdir(exist_ok=True)
    _pid_file(project_id).write_text(str(pid))


def _remove_pid(project_id: str) -> None:
    _pid_file(project_id).unlink(missing_ok=True)


def _is_pid_alive(pid: int) -> bool:
    """Проверяет, жив ли процесс с данным PID."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# ──────────────────────────────────────────────────────────
# Common deploy steps (одинаковы для обоих режимов)
# ──────────────────────────────────────────────────────────

async def clone_repository(
    repo_url: str,
    dest: Path,
    timeout: int = 120,
) -> tuple[bool, str]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)

    code, out, err = await _run(
        "git", "clone", "--depth=1", repo_url, str(dest),
        timeout=timeout,
    )
    if code != 0:
        msg = err.strip() or out.strip() or "Неизвестная ошибка git clone"
        logger.error("git clone failed: %s", msg)
        return False, msg

    logger.info("Клонировано → %s", dest)
    return True, "OK"


async def install_requirements(project_dir: Path) -> tuple[bool, str]:
    """
    Устанавливает зависимости.
    ВАЖНО: "-r", "requirements.txt" + cwd=project_dir — не передаём полный путь,
    иначе pip получает удвоенный путь и падает с FileNotFoundError.
    """
    if not (project_dir / "requirements.txt").exists():
        return True, "requirements.txt не найден — зависимости пропущены."

    code, out, err = await _run(
        "pip", "install", "--quiet", "--no-cache-dir",
        "-r", "requirements.txt",
        cwd=project_dir,
        timeout=300,
    )
    if code != 0:
        return False, err.strip() or out.strip() or "pip завершился с ошибкой"
    return True, "Зависимости установлены."


async def find_entrypoint(project_dir: Path) -> Optional[str]:
    for name in ENTRYPOINTS:
        if (project_dir / name).exists():
            return name
    return None


# ──────────────────────────────────────────────────────────
# start_container — роутер Docker / Subprocess
# ──────────────────────────────────────────────────────────

async def start_container(
    project_id: str,
    project_dir: Path,
    log_file: Path,
) -> tuple[bool, str]:
    """Запускает проект в Docker или как subprocess — в зависимости от режима."""
    use_docker = await check_docker()
    if use_docker:
        return await _start_docker(project_id, project_dir, log_file)
    return await _start_subprocess(project_id, project_dir, log_file)


# ── Docker ────────────────────────────────────────────────

async def _start_docker(
    project_id: str,
    project_dir: Path,
    log_file: Path,
) -> tuple[bool, str]:
    name     = container_name(project_id)
    env_file = project_dir / ".env"
    entry    = await find_entrypoint(project_dir)

    if entry is None:
        return False, f"Точка входа не найдена. Ожидается: {', '.join(ENTRYPOINTS)}"

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
        msg = err.strip() or out.strip() or "docker run завершился с ошибкой"
        logger.error("docker run failed [%s]: %s", project_id, msg)
        return False, msg

    cid = out.strip()[:12]
    logger.info("Docker-контейнер %s запущен (cid=%s)", name, cid)
    return True, cid


# ── Subprocess ────────────────────────────────────────────

async def _start_subprocess(
    project_id: str,
    project_dir: Path,
    log_file: Path,
) -> tuple[bool, str]:
    """
    Запускает проект как фоновый Python-процесс.
    stdout/stderr перенаправляются в log_file.
    Переменные из .env загружаются в окружение процесса.
    """
    # Убиваем старый процесс (если был)
    await _kill_subprocess(project_id)

    entry = await find_entrypoint(project_dir)
    if entry is None:
        return False, f"Точка входа не найдена. Ожидается: {', '.join(ENTRYPOINTS)}"

    # Формируем окружение: системное + из .env файла
    env = os.environ.copy()
    env_file = project_dir / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v

    # Открываем лог-файл для записи
    LOGS_DIR.mkdir(exist_ok=True)
    log_fd = open(log_file, "a", encoding="utf-8")

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", entry,
            stdout=log_fd,
            stderr=log_fd,
            cwd=str(project_dir),
            env=env,
        )
    except Exception as exc:
        log_fd.close()
        logger.error("Subprocess start failed [%s]: %s", project_id, exc)
        return False, str(exc)

    log_fd.close()  # asyncio держит файл открытым через proc, закрываем наш дескриптор

    _procs[project_id] = proc
    _save_pid(project_id, proc.pid)
    logger.info("Subprocess %s запущен (pid=%d)", project_id, proc.pid)

    # Даём процессу 1 сек — если упал сразу, сообщаем об ошибке
    await asyncio.sleep(1)
    if proc.returncode is not None:
        # Процесс уже завершился
        logs = _read_log_tail(log_file, 10)
        _remove_pid(project_id)
        return False, f"Процесс завершился сразу (code={proc.returncode}):\n{logs}"

    return True, f"pid:{proc.pid}"


def _read_log_tail(log_file: Path, n: int = 30) -> str:
    try:
        if log_file.exists():
            lines = log_file.read_text(errors="replace").splitlines()
            return "\n".join(lines[-n:])
    except Exception:
        pass
    return ""


# ──────────────────────────────────────────────────────────
# Controls — роутят в Docker или Subprocess
# ──────────────────────────────────────────────────────────

async def stop_container(project_id: str) -> tuple[bool, str]:
    if _use_docker:
        name = container_name(project_id)
        code, _, err = await _run("docker", "stop", "--time", "10", name, timeout=30)
        if code != 0:
            return False, err.strip() or "Не удалось остановить"
    else:
        await _kill_subprocess(project_id)

    await update_project_status(project_id, "stopped")
    return True, "Остановлен."


async def restart_container(project_id: str) -> tuple[bool, str]:
    if _use_docker:
        name = container_name(project_id)
        code, _, err = await _run("docker", "restart", "--time", "5", name, timeout=30)
        if code != 0:
            return False, err.strip() or "Не удалось перезапустить"
        await update_project_status(project_id, "running")
        return True, "Перезапущен."
    else:
        # subprocess: stop + start
        from database import get_project
        project = await get_project(project_id)
        if not project:
            return False, "Проект не найден в БД."
        project_dir = PROJECTS_DIR / str(project["user_id"]) / project_id
        log_file    = LOGS_DIR / f"{project_id}.log"
        ok, msg = await _start_subprocess(project_id, project_dir, log_file)
        if ok:
            await update_project_status(project_id, "running")
        return ok, msg


async def get_container_status(project_id: str) -> str:
    if _use_docker:
        name = container_name(project_id)
        code, out, _ = await _run(
            "docker", "inspect", "--format", "{{.State.Status}}", name, timeout=10
        )
        return out.strip() if code == 0 else "stopped"
    else:
        # subprocess: проверяем живой ли процесс
        proc = _procs.get(project_id)
        if proc and proc.returncode is None:
            return "running"
        # Проверяем pid-файл (процесс мог запуститься до перезапуска бота)
        pid = _load_pid(project_id)
        if pid and _is_pid_alive(pid):
            return "running"
        return "stopped"


async def get_container_stats(project_id: str) -> dict:
    if _use_docker:
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
            "cpu": parts[0].strip() if parts else "—",
            "mem": parts[1].strip() if len(parts) > 1 else "—",
        }
    else:
        # subprocess: используем psutil если доступен
        pid = _load_pid(project_id)
        if pid and _is_pid_alive(pid):
            try:
                import psutil
                p = psutil.Process(pid)
                cpu = f"{p.cpu_percent(interval=0.2):.1f}%"
                mem = f"{p.memory_info().rss // 1024 // 1024} MB"
                return {"cpu": cpu, "mem": mem}
            except Exception:
                pass
        return {"cpu": "—", "mem": "—"}


async def get_logs(project_id: str, tail: int = 30) -> str:
    if _use_docker:
        name = container_name(project_id)
        code, out, err = await _run(
            "docker", "logs", "--tail", str(tail), name, timeout=15
        )
        if code != 0:
            # Fallback на файл
            return _read_log_tail(LOGS_DIR / f"{project_id}.log", tail) or \
                   err.strip() or "Логи недоступны."
        combined = (out.strip() + "\n" + err.strip()).strip()
        lines = combined.splitlines()[-tail:]
        return "\n".join(lines) or "Логи пусты."
    else:
        return _read_log_tail(LOGS_DIR / f"{project_id}.log", tail) or "Логи пусты."


async def remove_project(project_id: str, user_id: int) -> tuple[bool, str]:
    if _use_docker:
        await _run("docker", "rm", "-f", container_name(project_id), timeout=20)
    else:
        await _kill_subprocess(project_id)
        _remove_pid(project_id)

    project_dir = PROJECTS_DIR / str(user_id) / project_id
    if project_dir.exists():
        try:
            shutil.rmtree(project_dir)
        except Exception as exc:
            return False, f"Ошибка удаления файлов: {exc}"

    (LOGS_DIR / f"{project_id}.log").unlink(missing_ok=True)
    logger.info("Проект %s удалён.", project_id)
    return True, "Проект удалён."


# ──────────────────────────────────────────────────────────
# Subprocess helpers
# ──────────────────────────────────────────────────────────

async def _kill_subprocess(project_id: str) -> None:
    """Убивает subprocess проекта (если запущен)."""
    proc = _procs.pop(project_id, None)
    if proc and proc.returncode is None:
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except Exception as exc:
            logger.warning("Не удалось завершить процесс %s: %s", project_id, exc)
        return

    # Fallback: убить по PID из файла
    pid = _load_pid(project_id)
    if pid and _is_pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            await asyncio.sleep(2)
            if _is_pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
        except Exception as exc:
            logger.warning("kill pid=%d: %s", pid, exc)
