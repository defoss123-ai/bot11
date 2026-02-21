import logging
from pathlib import Path
from typing import Optional, Tuple

import aiosqlite
from cryptography.fernet import Fernet, InvalidToken


class EncryptedSettings:
    def __init__(self, db_path: str = "bot.db", key_path: str = "master.key") -> None:
        self.db_path = db_path
        self.key_path = Path(key_path)
        self.logger = logging.getLogger(__name__)
        self.fernet = Fernet(self._load_or_create_key())

    def _load_or_create_key(self) -> bytes:
        if not self.key_path.exists():
            key = Fernet.generate_key()
            self.key_path.write_bytes(key)
            self.logger.info("master.key created at %s", self.key_path)
            return key
        return self.key_path.read_bytes()

    async def _ensure_table(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            await db.commit()

    async def get_api_keys(self) -> Tuple[Optional[str], Optional[str]]:
        await self._ensure_table()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT key, value FROM settings WHERE key IN (?, ?)",
                ("api_key", "api_secret"),
            )
            rows = await cursor.fetchall()

        data = {k: v for k, v in rows}
        encrypted_key = data.get("api_key")
        encrypted_secret = data.get("api_secret")

        api_key: Optional[str] = None
        api_secret: Optional[str] = None

        if encrypted_key:
            try:
                api_key = self.fernet.decrypt(encrypted_key.encode()).decode()
            except (InvalidToken, ValueError, TypeError) as exc:
                self.logger.error("Failed to decrypt api_key: %s", exc)

        if encrypted_secret:
            try:
                api_secret = self.fernet.decrypt(encrypted_secret.encode()).decode()
            except (InvalidToken, ValueError, TypeError) as exc:
                self.logger.error("Failed to decrypt api_secret: %s", exc)

        return api_key, api_secret

    async def save_api_keys(self, api_key: str, api_secret: str) -> None:
        await self._ensure_table()
        encrypted_key = self.fernet.encrypt(api_key.encode()).decode()
        encrypted_secret = self.fernet.encrypt(api_secret.encode()).decode()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                ("api_key", encrypted_key),
            )
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                ("api_secret", encrypted_secret),
            )
            await db.commit()

        self.logger.info("API keys encrypted and saved to settings")
