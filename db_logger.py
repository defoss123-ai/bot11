import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite


class DatabaseLogger:
    def __init__(self, db_path: str = "bot.db", log_file: str = "operations.log") -> None:
        self.db_path = db_path
        self.log_file = Path(log_file)
        self.queue: asyncio.Queue[tuple[str, str, str]] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self.logger = logging.getLogger(__name__)

    async def initialize(self) -> None:
        await self._ensure_tables()
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker(), name="db-logger-worker")

    async def close(self) -> None:
        if self._worker_task is None:
            return
        await self.queue.put(("__STOP__", "", ""))
        await self._worker_task
        self._worker_task = None

    async def _ensure_tables(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS pairs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    side TEXT,
                    amount REAL,
                    entry_price REAL,
                    created_at TEXT
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    side TEXT,
                    order_type TEXT,
                    qty REAL,
                    price REAL,
                    status TEXT,
                    created_at TEXT
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            await db.commit()

    async def log(self, level: str, message: str) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        try:
            await self.queue.put((level.upper(), message, timestamp))
        except Exception as exc:
            self.logger.error("Failed to enqueue log message: %s", exc)

    async def get_last_logs(self, limit: int = 10):
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT level, message, created_at
                FROM logs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
        return rows

    async def _worker(self) -> None:
        while True:
            level, message, created_at = await self.queue.get()
            if level == "__STOP__":
                self.queue.task_done()
                break

            try:
                async with aiosqlite.connect(self.db_path) as db:
                    await db.execute(
                        "INSERT INTO logs (level, message, created_at) VALUES (?, ?, ?)",
                        (level, message, created_at),
                    )
                    await db.commit()
            except Exception as exc:
                self.logger.error("Failed to write log to SQLite: %s", exc)

            try:
                await asyncio.to_thread(self._append_to_file, level, message, created_at)
            except Exception as exc:
                self.logger.error("Failed to write log to file: %s", exc)

            self.queue.task_done()

    def _append_to_file(self, level: str, message: str, created_at: str) -> None:
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(f"{created_at} [{level}] {message}\n")
