import logging
from typing import Any, Optional

import aiosqlite


class ConfigManager:
    DEFAULTS: dict[str, float] = {
        "lookback": 20.0,
        "volume_multiplier": 1.5,
        "check_interval": 60.0,
        "risk_per_trade": 5.0,
    }

    def __init__(self, db: aiosqlite.Connection, logger: Optional[logging.Logger] = None) -> None:
        self.db = db
        self.logger = logger or logging.getLogger(__name__)
        self.cache: dict[str, float] = {}

    async def init_table(self) -> None:
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        await self.db.commit()

        await self.load_all()
        for key, value in self.DEFAULTS.items():
            if key not in self.cache:
                await self.set(key, value)

    async def load_all(self) -> dict[str, float]:
        await self.init_table_if_needed()
        cursor = await self.db.execute("SELECT key, value FROM config")
        rows = await cursor.fetchall()

        loaded: dict[str, float] = {}
        for key, value in rows:
            try:
                numeric_value = float(value)
            except (TypeError, ValueError):
                self.logger.error("Invalid config value for %s: %s", key, value)
                continue

            if numeric_value < 0:
                self.logger.error("Negative config value for %s rejected: %s", key, numeric_value)
                continue

            loaded[key] = numeric_value

        for key, default_value in self.DEFAULTS.items():
            loaded.setdefault(key, float(default_value))

        self.cache = loaded
        return dict(self.cache)

    async def get(self, key: str, default: Any = None) -> Any:
        if key in self.cache:
            return self.cache[key]

        await self.init_table_if_needed()
        cursor = await self.db.execute("SELECT value FROM config WHERE key = ?", (key,))
        row = await cursor.fetchone()

        if row is None:
            if key in self.DEFAULTS:
                default_value = float(self.DEFAULTS[key])
                self.cache[key] = default_value
                return default_value
            return default

        try:
            value = float(row[0])
        except (TypeError, ValueError):
            self.logger.error("Invalid config value for %s: %s", key, row[0])
            return default

        if value < 0:
            self.logger.error("Negative config value for %s rejected: %s", key, value)
            return default

        self.cache[key] = value
        return value

    async def set(self, key: str, value: Any) -> bool:
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            self.logger.error("Config update rejected for %s: non-numeric value=%s", key, value)
            return False

        if numeric_value < 0:
            self.logger.error("Config update rejected for %s: negative value=%s", key, numeric_value)
            return False

        await self.init_table_if_needed()
        await self.db.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            (key, str(numeric_value)),
        )
        await self.db.commit()

        self.cache[key] = numeric_value
        self.logger.info("Config updated: %s=%s", key, numeric_value)
        return True

    async def init_table_if_needed(self) -> None:
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        await self.db.commit()
