import asyncio
import logging
import signal
from typing import Optional

import aiosqlite

from config_manager import ConfigManager
from db_logger import DatabaseLogger
from encryption import EncryptedSettings
from interface import ConsoleInterface
from pair_manager import PairManager
from signal_generator import SignalGenerator
from trader import Trader, create_exchange


class AppContext:
    def __init__(self):
        self.running = True
        self.shutting_down = False
        self.tasks: list[asyncio.Task] = []
        self.db: Optional[aiosqlite.Connection] = None
        self.db_logger: Optional[DatabaseLogger] = None

        self.pair_manager: Optional[PairManager] = None
        self.config_manager: Optional[ConfigManager] = None
        self.encrypted_settings: Optional[EncryptedSettings] = None
        self.signal_generator: Optional[SignalGenerator] = None
        self.interface: Optional[ConsoleInterface] = None
        self.trader: Optional[Trader] = None
        self.exchange = None


async def create_tables(db: aiosqlite.Connection) -> None:
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


async def signal_loop(context: AppContext) -> None:
    logger = logging.getLogger(__name__)

    while context.running:
        try:
            if not context.trader or not context.pair_manager or not context.config_manager or not context.signal_generator:
                await asyncio.sleep(1)
                continue

            check_interval = float(await context.config_manager.get("check_interval", 60.0))
            volume_multiplier = float(await context.config_manager.get("volume_multiplier", 1.5))
            lookback = int(float(await context.config_manager.get("lookback", 20.0)))
            risk_per_trade = float(await context.config_manager.get("risk_per_trade", 5.0))

            active_pairs = context.pair_manager.get_active_pairs()
            for pair in active_pairs:
                symbol = str(pair.get("symbol"))
                leverage = int(float(pair.get("leverage", 1)))
                tp = float(pair.get("tp", 1.0))
                sl = float(pair.get("sl", 1.0))
                cancel_time = float(pair.get("cancel_time", 0))

                try:
                    has_position = await context.trader.has_open_position(symbol)
                    if has_position:
                        continue

                    signal_value = await context.signal_generator.generate_signal(
                        symbol=symbol,
                        lookback=lookback,
                        volume_multiplier=volume_multiplier,
                    )
                    if signal_value is None:
                        continue

                    await context.trader.place_limit_order(
                        symbol=symbol,
                        signal_side=signal_value,
                        leverage=leverage,
                        risk_percent=risk_per_trade,
                        cancel_delay=cancel_time,
                        tp_percent=tp,
                        sl_percent=sl,
                    )
                    logger.info("Signal %s processed for %s", signal_value, symbol)
                except Exception:
                    logger.exception("Error processing pair in signal loop: %s", symbol)

            await asyncio.sleep(max(check_interval, 1.0))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unhandled error in signal loop")
            await asyncio.sleep(1)


async def wait_for_api_keys_and_start_trading(context: AppContext) -> None:
    logger = logging.getLogger(__name__)

    while context.running:
        try:
            if context.trader is not None:
                await asyncio.sleep(5)
                continue

            if context.encrypted_settings is None:
                await asyncio.sleep(1)
                continue

            api_key, api_secret = await context.encrypted_settings.get_api_keys()
            if not api_key or not api_secret:
                await asyncio.sleep(5)
                continue

            context.exchange = create_exchange(api_key, api_secret)
            context.signal_generator = SignalGenerator(context.exchange)
            context.trader = Trader(context.exchange, context.db)
            await context.trader.initialize()
            await context.trader.start_monitoring()

            if context.interface is not None:
                context.interface.trader = context.trader

            context.tasks.append(asyncio.create_task(signal_loop(context), name="signal-loop"))
            logger.info("API keys detected. Trader and signal loop started")
            if context.db_logger is not None:
                await context.db_logger.log("INFO", "Trader initialized after API keys became available")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error in API key watcher")

        await asyncio.sleep(5)


async def shutdown(context: AppContext):
    logger = logging.getLogger(__name__)
    if context.shutting_down:
        return

    context.shutting_down = True
    context.running = False

    for task in context.tasks:
        task.cancel()
    if context.tasks:
        await asyncio.gather(*context.tasks, return_exceptions=True)

    if context.interface is not None:
        await context.interface._shutdown()

    if context.trader is not None:
        await context.trader.close()

    if context.exchange is not None:
        close_fn = getattr(context.exchange, "close", None)
        if close_fn is not None:
            try:
                await close_fn()
            except Exception:
                logger.exception("Failed to close exchange")

    if context.db_logger is not None:
        await context.db_logger.close()

    if context.db is not None:
        await context.db.close()

    logger.info("Application shutdown completed")


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()],
    )
    logger = logging.getLogger(__name__)

    context = AppContext()
    loop = asyncio.get_running_loop()

    def _signal_handler(signum, frame):
        logger.info("Received signal %s, starting graceful shutdown", signum)
        loop.call_soon_threadsafe(lambda: asyncio.create_task(shutdown(context)))

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        context.db = await aiosqlite.connect("bot.db")
        await create_tables(context.db)

        context.db_logger = DatabaseLogger(db_path="bot.db", log_file="operations.log")
        await context.db_logger.initialize()

        context.encrypted_settings = EncryptedSettings(db_path="bot.db", key_path="master.key")
        context.pair_manager = PairManager(context.db)
        await context.pair_manager.load_pairs()

        context.config_manager = ConfigManager(context.db)
        await context.config_manager.init_table()

        context.interface = ConsoleInterface(
            pair_manager=context.pair_manager,
            config_manager=context.config_manager,
            encrypted_settings=context.encrypted_settings,
            trader=None,
            db_logger=context.db_logger,
        )

        api_key, api_secret = await context.encrypted_settings.get_api_keys()
        if not api_key or not api_secret:
            logger.warning("API keys are not configured. Waiting in background while interface is running.")
            await context.db_logger.log("WARNING", "API keys missing; waiting for credentials")
        else:
            context.exchange = create_exchange(api_key, api_secret)
            context.signal_generator = SignalGenerator(context.exchange)
            context.trader = Trader(context.exchange, context.db)
            await context.trader.initialize()
            await context.trader.start_monitoring()
            context.interface.trader = context.trader
            context.tasks.append(asyncio.create_task(signal_loop(context), name="signal-loop"))
            await context.db_logger.log("INFO", "Trader initialized on startup")

        context.tasks.append(asyncio.create_task(wait_for_api_keys_and_start_trading(context), name="api-key-watcher"))
        context.tasks.append(asyncio.create_task(context.interface.start(), name="console-interface"))

        while context.running:
            await asyncio.sleep(0.2)

    except Exception:
        logger.exception("Unhandled error in main")
    finally:
        await shutdown(context)


if __name__ == "__main__":
    asyncio.run(main())
