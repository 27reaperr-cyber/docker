"""
database.py — SQLite-слой для хранения пользователей и проектов.
Используется aiosqlite для асинхронных операций.
"""

import aiosqlite
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = "hosting.db"
MAX_PROJECTS_PER_USER = 3


async def init_db() -> None:
    """Инициализация базы данных: создание таблиц при первом запуске."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                created_at  TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                project_id   TEXT PRIMARY KEY,
                user_id      INTEGER NOT NULL,
                repo_url     TEXT NOT NULL,
                name         TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'stopped',
                container_id TEXT,
                created_at   TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)
        await db.commit()
    logger.info("База данных инициализирована.")


# ───────────────────────── Users ─────────────────────────

async def upsert_user(user_id: int, username: Optional[str]) -> None:
    """Создаёт запись пользователя, если она ещё не существует."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (user_id, username, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username = excluded.username
            """,
            (user_id, username or "unknown", datetime.utcnow().isoformat()),
        )
        await db.commit()


# ───────────────────────── Projects ──────────────────────

async def get_user_projects(user_id: int) -> list[dict]:
    """Возвращает все проекты пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM projects WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_project(project_id: str) -> Optional[dict]:
    """Возвращает один проект по project_id или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM projects WHERE project_id = ?",
            (project_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


async def count_user_projects(user_id: int) -> int:
    """Количество проектов пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM projects WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            result = await cursor.fetchone()
    return result[0] if result else 0


async def create_project(
    project_id: str,
    user_id: int,
    repo_url: str,
    name: str,
) -> None:
    """Создаёт новую запись проекта."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO projects (project_id, user_id, repo_url, name, status, created_at)
            VALUES (?, ?, ?, ?, 'deploying', ?)
            """,
            (project_id, user_id, repo_url, name, datetime.utcnow().isoformat()),
        )
        await db.commit()


async def update_project_status(
    project_id: str,
    status: str,
    container_id: Optional[str] = None,
) -> None:
    """Обновляет статус и (опционально) container_id проекта."""
    async with aiosqlite.connect(DB_PATH) as db:
        if container_id is not None:
            await db.execute(
                "UPDATE projects SET status = ?, container_id = ? WHERE project_id = ?",
                (status, container_id, project_id),
            )
        else:
            await db.execute(
                "UPDATE projects SET status = ? WHERE project_id = ?",
                (status, project_id),
            )
        await db.commit()


async def delete_project(project_id: str) -> None:
    """Удаляет проект из базы данных."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM projects WHERE project_id = ?",
            (project_id,),
        )
        await db.commit()
