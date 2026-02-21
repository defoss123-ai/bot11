import asyncio
import logging
import math
from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite




def create_exchange(api_key: str, api_secret: str):
    import ccxt.async_support as ccxt

    return ccxt.mexc({
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",
            "unifiedAccount": True,
        },
    })


class Trader:
    def __init__(self, exchange: Any, db: aiosqlite.Connection, logger: Optional[logging.Logger] = None) -> None:
        self.exchange = exchange
        self.db = db
        self.logger = logger or logging.getLogger(__name__)
        self.active_orders: dict[str, dict[str, Any]] = {}
        self._monitor_task: Optional[asyncio.Task] = None
        self._running = False

    async def initialize(self) -> None:
        await self._ensure_tables()

    async def close(self) -> None:
        self._running = False
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            await asyncio.gather(self._monitor_task, return_exceptions=True)
            self._monitor_task = None

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        try:
            await self.exchange.set_leverage(leverage, symbol)
            self.logger.info("Leverage set: %s %sx", symbol, leverage)
            return True
        except Exception as exc:
            self.logger.error("Failed to set leverage for %s: %s", symbol, exc)
            return False

    async def get_balance_usdt(self) -> float:
        try:
            balance = await self.exchange.fetch_balance()
            free = float(balance["USDT"]["free"])
            self.logger.info("Fetched USDT balance: free=%s", free)
            return free
        except Exception as exc:
            self.logger.error("Failed to fetch USDT balance: %s", exc)
            return 0.0

    async def calculate_quantity(
        self,
        symbol: str,
        current_price: float,
        leverage: int,
        risk_percent: float,
        free_usdt: Optional[float] = None,
    ) -> float:
        if free_usdt is None:
            free_usdt = await self.get_balance_usdt()

        if free_usdt <= 0 or current_price <= 0:
            self.logger.error(
                "Invalid quantity inputs for %s: free_usdt=%s, current_price=%s",
                symbol,
                free_usdt,
                current_price,
            )
            return 0.0

        max_margin = free_usdt * (risk_percent / 100)
        qty = (max_margin * leverage) / current_price

        try:
            market = self.exchange.market(symbol)
            step = market["precision"]["amount"]
        except Exception as exc:
            self.logger.error("Failed to read market precision for %s: %s", symbol, exc)
            return max(qty, 0.0)

        rounded_qty = self._round_to_step(qty, step)
        self.logger.info(
            "Calculated quantity for %s: qty=%s (raw=%s, step=%s)",
            symbol,
            rounded_qty,
            qty,
            step,
        )
        return max(rounded_qty, 0.0)

    async def place_limit_order(
        self,
        symbol: str,
        signal_side: str,
        leverage: int,
        risk_percent: float,
        cancel_delay: float,
        tp_percent: float,
        sl_percent: float,
    ) -> Optional[dict[str, Any]]:
        side = "buy" if signal_side.upper() == "LONG" else "sell"

        if not await self.set_leverage(symbol, leverage):
            return None

        try:
            ticker = await self.exchange.fetch_ticker(symbol)
            bid = float(ticker["bid"])
            ask = float(ticker["ask"])
        except Exception as exc:
            self.logger.error("Failed to fetch ticker for %s: %s", symbol, exc)
            return None

        if signal_side.upper() == "LONG":
            price = bid * 1.001
        else:
            price = ask * 0.999

        quantity = await self.calculate_quantity(symbol, price, leverage, risk_percent)
        if quantity <= 0:
            self.logger.error("Aborted order placement for %s: zero quantity", symbol)
            return None

        try:
            order = await self.exchange.create_limit_order(symbol, side, quantity, price)
            self.logger.info(
                "Placed limit order: symbol=%s side=%s qty=%s price=%s order_id=%s",
                symbol,
                side,
                quantity,
                price,
                order.get("id"),
            )
        except Exception as exc:
            self.logger.error("Failed to place limit order for %s: %s", symbol, exc)
            return None

        order_id = str(order.get("id"))
        self.active_orders[order_id] = {
            "symbol": symbol,
            "signal_side": signal_side.upper(),
            "quantity": quantity,
            "tp_percent": tp_percent,
            "sl_percent": sl_percent,
        }

        await self._save_order(
            exchange_order_id=order_id,
            symbol=symbol,
            side=side,
            order_type="limit",
            qty=quantity,
            price=price,
            status=str(order.get("status", "open")),
        )

        asyncio.create_task(self._auto_cancel(symbol, order_id, cancel_delay))
        return order

    async def _auto_cancel(self, symbol: str, order_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            order = await self.exchange.fetch_order(order_id, symbol)
            status = str(order.get("status", "")).lower()
            if status == "open":
                await self.exchange.cancel_order(order_id, symbol)
                await self._update_order_status(order_id, "canceled")
                self.active_orders.pop(order_id, None)
                self.logger.info("Auto-canceled order: symbol=%s order_id=%s", symbol, order_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.error("Auto-cancel failed for %s (%s): %s", symbol, order_id, exc)

    async def set_stop_loss_take_profit(
        self,
        symbol: str,
        signal_side: str,
        quantity: float,
        entry_price: float,
        sl_percent: float,
        tp_percent: float,
    ) -> None:
        close_side = "sell" if signal_side.upper() == "LONG" else "buy"

        if signal_side.upper() == "LONG":
            stop_price = entry_price * (1 - sl_percent / 100)
            take_price = entry_price * (1 + tp_percent / 100)
        else:
            stop_price = entry_price * (1 + sl_percent / 100)
            take_price = entry_price * (1 - tp_percent / 100)

        try:
            stop_params = {"stopPrice": stop_price}
            await self.exchange.create_order(
                symbol,
                "stopMarket",
                close_side,
                quantity,
                None,
                stop_params,
            )
            await self.exchange.create_order(
                symbol,
                "limit",
                close_side,
                quantity,
                take_price,
            )
            self.logger.info(
                "Set SL/TP for %s: side=%s qty=%s stop=%s take=%s",
                symbol,
                close_side,
                quantity,
                stop_price,
                take_price,
            )
        except Exception as exc:
            self.logger.error("Failed to set SL/TP for %s: %s", symbol, exc)

    async def has_open_position(self, symbol: str) -> bool:
        try:
            positions = await self.exchange.fetch_positions([symbol])
            for pos in positions or []:
                contracts = pos.get("contracts")
                if contracts is not None and abs(float(contracts)) > 0:
                    return True
                info = pos.get("info", {})
                for key in ("positionAmt", "vol", "holdVol"):
                    if key in info and abs(float(info[key])) > 0:
                        return True
            return False
        except Exception as exc:
            self.logger.error("Failed to check open position for %s: %s", symbol, exc)
            return False

    async def check_positions_and_orders(self) -> None:
        self._running = True
        while self._running:
            try:
                for order_id, meta in list(self.active_orders.items()):
                    symbol = meta["symbol"]
                    order = await self.exchange.fetch_order(order_id, symbol)
                    status = str(order.get("status", "")).lower()

                    if status == "filled":
                        await self._update_order_status(order_id, "filled")
                        entry_price = float(order.get("average") or order.get("price") or 0)
                        quantity = float(order.get("amount") or meta["quantity"])

                        await self.set_stop_loss_take_profit(
                            symbol=symbol,
                            signal_side=meta["signal_side"],
                            quantity=quantity,
                            entry_price=entry_price,
                            sl_percent=float(meta["sl_percent"]),
                            tp_percent=float(meta["tp_percent"]),
                        )
                        self.active_orders.pop(order_id, None)
                        self.logger.info("Order filled and SL/TP placed: %s", order_id)
                    elif status in {"canceled", "cancelled", "closed", "rejected", "expired"}:
                        await self._update_order_status(order_id, status)
                        self.active_orders.pop(order_id, None)

            except Exception as exc:
                self.logger.error("Error in order/position monitor loop: %s", exc)

            await asyncio.sleep(10)

    async def start_monitoring(self) -> None:
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self.check_positions_and_orders(), name="trader-monitor")

    async def close_position(self, symbol: str, side: str, amount: float) -> Optional[dict[str, Any]]:
        close_side = "sell" if side.lower() == "buy" else "buy"
        try:
            order = await self.exchange.create_market_order(symbol, close_side, amount)
            self.logger.info(
                "Position closed by market order: symbol=%s side=%s amount=%s",
                symbol,
                close_side,
                amount,
            )
            return order
        except Exception as exc:
            self.logger.error("Failed to close position for %s: %s", symbol, exc)
            return None

    async def _save_order(
        self,
        exchange_order_id: str,
        symbol: str,
        side: str,
        order_type: str,
        qty: float,
        price: float,
        status: str,
    ) -> None:
        created_at = datetime.now(timezone.utc).isoformat()
        try:
            await self.db.execute(
                """
                INSERT INTO orders (exchange_order_id, symbol, side, order_type, qty, price, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (exchange_order_id, symbol, side, order_type, qty, price, status, created_at),
            )
            await self.db.commit()
        except Exception as exc:
            self.logger.error("Failed to save order to DB: %s", exc)

    async def _update_order_status(self, exchange_order_id: str, status: str) -> None:
        try:
            await self.db.execute(
                "UPDATE orders SET status = ? WHERE exchange_order_id = ?",
                (status, exchange_order_id),
            )
            await self.db.commit()
            self.logger.info("Order status updated: order_id=%s status=%s", exchange_order_id, status)
        except Exception as exc:
            self.logger.error("Failed to update order status in DB: %s", exc)

    async def _ensure_tables(self) -> None:
        await self.db.execute(
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
        cursor = await self.db.execute("PRAGMA table_info(orders)")
        cols = {row[1] for row in await cursor.fetchall()}
        if "exchange_order_id" not in cols:
            await self.db.execute("ALTER TABLE orders ADD COLUMN exchange_order_id TEXT")
        await self.db.commit()

    @staticmethod
    def _round_to_step(quantity: float, step: Any) -> float:
        if isinstance(step, int):
            return round(quantity, step)

        try:
            step_value = float(step)
        except (TypeError, ValueError):
            return quantity

        if step_value <= 0:
            return quantity

        return math.floor(quantity / step_value) * step_value
