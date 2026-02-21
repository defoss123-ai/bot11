import logging
from typing import Any, Optional

import aiosqlite


class PairManager:
    def __init__(self, db: aiosqlite.Connection, logger: Optional[logging.Logger] = None) -> None:
        self.db = db
        self.logger = logger or logging.getLogger(__name__)
        self.pairs_cache: dict[str, dict[str, Any]] = {}

    async def load_pairs(self) -> None:
        await self._ensure_pairs_table()
        self.pairs_cache.clear()

        cursor = await self.db.execute(
            """
            SELECT symbol, leverage, tp, sl, cancel_time, enabled
            FROM pairs
            """
        )
        rows = await cursor.fetchall()

        for symbol, leverage, tp, sl, cancel_time, enabled in rows:
            self.pairs_cache[symbol] = {
                "symbol": symbol,
                "leverage": leverage,
                "tp": tp,
                "sl": sl,
                "cancel_time": cancel_time,
                "enabled": bool(enabled),
            }

    async def add_pair(self, symbol, leverage, tp, sl, cancel_time) -> bool:
        validation_error = self._validate_pair_values(leverage, tp, sl, cancel_time)
        if validation_error:
            self.logger.error("Validation error while adding pair %s: %s", symbol, validation_error)
            return False

        await self.db.execute(
            """
            INSERT OR REPLACE INTO pairs (symbol, leverage, tp, sl, cancel_time, enabled)
            VALUES (?, ?, ?, ?, ?, COALESCE((SELECT enabled FROM pairs WHERE symbol = ?), 1))
            """,
            (symbol, leverage, tp, sl, cancel_time, symbol),
        )
        await self.db.commit()
        await self.load_pairs()

        self.logger.info(
            "Pair added: %s (leverage=%s, tp=%s, sl=%s, cancel_time=%s)",
            symbol,
            leverage,
            tp,
            sl,
            cancel_time,
        )
        return True

    async def update_pair(
        self,
        symbol: str,
        leverage: Optional[float] = None,
        tp: Optional[float] = None,
        sl: Optional[float] = None,
        cancel_time: Optional[float] = None,
    ) -> bool:
        current = self.get_pair_settings(symbol)
        if current is None:
            self.logger.error("Cannot update pair %s: pair not found", symbol)
            return False

        new_leverage = current["leverage"] if leverage is None else leverage
        new_tp = current["tp"] if tp is None else tp
        new_sl = current["sl"] if sl is None else sl
        new_cancel_time = current["cancel_time"] if cancel_time is None else cancel_time

        validation_error = self._validate_pair_values(new_leverage, new_tp, new_sl, new_cancel_time)
        if validation_error:
            self.logger.error("Validation error while updating pair %s: %s", symbol, validation_error)
            return False

        await self.db.execute(
            """
            UPDATE pairs
            SET leverage = ?, tp = ?, sl = ?, cancel_time = ?
            WHERE symbol = ?
            """,
            (new_leverage, new_tp, new_sl, new_cancel_time, symbol),
        )
        await self.db.commit()
        await self.load_pairs()

        self.logger.info(
            "Pair updated: %s (leverage=%s, tp=%s, sl=%s, cancel_time=%s)",
            symbol,
            new_leverage,
            new_tp,
            new_sl,
            new_cancel_time,
        )
        return True

    async def remove_pair(self, symbol) -> bool:
        cursor = await self.db.execute("DELETE FROM pairs WHERE symbol = ?", (symbol,))
        await self.db.commit()
        await self.load_pairs()

        if cursor.rowcount:
            self.logger.info("Pair removed: %s", symbol)
            return True
        return False

    async def enable_pair(self, symbol) -> bool:
        updated = await self._set_enabled(symbol, True)
        if updated:
            self.logger.info("Pair enabled: %s", symbol)
        return updated

    async def disable_pair(self, symbol) -> bool:
        updated = await self._set_enabled(symbol, False)
        if updated:
            self.logger.info("Pair disabled: %s", symbol)
        return updated

    def get_active_pairs(self) -> list[dict[str, Any]]:
        return [pair for pair in self.pairs_cache.values() if pair["enabled"]]

    def get_pair_settings(self, symbol) -> Optional[dict[str, Any]]:
        return self.pairs_cache.get(symbol)

    async def _set_enabled(self, symbol: str, enabled: bool) -> bool:
        cursor = await self.db.execute(
            "UPDATE pairs SET enabled = ? WHERE symbol = ?",
            (1 if enabled else 0, symbol),
        )
        await self.db.commit()
        await self.load_pairs()
        return bool(cursor.rowcount)

    async def _ensure_pairs_table(self) -> None:
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS pairs (
                symbol TEXT PRIMARY KEY,
                leverage INTEGER NOT NULL,
                tp REAL NOT NULL,
                sl REAL NOT NULL,
                cancel_time REAL NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1
            )
            """
        )

        cursor = await self.db.execute("PRAGMA table_info(pairs)")
        columns = {row[1] for row in await cursor.fetchall()}

        migrations = [
            ("leverage", "ALTER TABLE pairs ADD COLUMN leverage INTEGER NOT NULL DEFAULT 1"),
            ("tp", "ALTER TABLE pairs ADD COLUMN tp REAL NOT NULL DEFAULT 1"),
            ("sl", "ALTER TABLE pairs ADD COLUMN sl REAL NOT NULL DEFAULT 1"),
            ("cancel_time", "ALTER TABLE pairs ADD COLUMN cancel_time REAL NOT NULL DEFAULT 0"),
            ("enabled", "ALTER TABLE pairs ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"),
        ]
        for column_name, statement in migrations:
            if column_name not in columns:
                await self.db.execute(statement)

        await self.db.commit()

    def _validate_pair_values(self, leverage: float, tp: float, sl: float, cancel_time: float) -> Optional[str]:
        if not 1 <= leverage <= 125:
            return "leverage must be in range 1..125"
        if tp <= 0:
            return "tp must be greater than 0"
        if sl <= 0:
            return "sl must be greater than 0"
        if cancel_time < 0:
            return "cancel_time must be >= 0"
        return None
