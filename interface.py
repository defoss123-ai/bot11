import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Optional

import questionary
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table


class ConsoleInterface:
    def __init__(
        self,
        pair_manager: Optional[Any] = None,
        config_manager: Optional[Any] = None,
        encrypted_settings: Optional[Any] = None,
        trader: Optional[Any] = None,
        db_logger: Optional[Any] = None,
    ) -> None:
        self.console = Console()
        self.live: Optional[Live] = None
        self.menu_mode = False

        self.pair_manager = pair_manager
        self.config_manager = config_manager
        self.encrypted_settings = encrypted_settings
        self.trader = trader
        self.db_logger = db_logger

        self._running = True
        self._tasks: list[asyncio.Task] = []
        self._lock = asyncio.Lock()
        self.logger = logging.getLogger(__name__)
        self.operations_log_path = Path("operations.log")

    async def start(self) -> None:
        self._running = True
        self._tasks = [
            asyncio.create_task(self._monitor(), name="interface-monitor"),
            asyncio.create_task(self._input_handler(), name="interface-input-handler"),
        ]

        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            raise
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        self._running = False
        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def _monitor(self) -> None:
        layout = self._generate_layout()

        with Live(layout, console=self.console, refresh_per_second=1) as live:
            self.live = live
            while self._running:
                if self.menu_mode:
                    await asyncio.sleep(0.2)
                    continue

                async with self._lock:
                    live.update(self._generate_layout())
                await asyncio.sleep(1)

        self.live = None

    async def _input_handler(self) -> None:
        while self._running:
            try:
                key = await asyncio.to_thread(sys.stdin.read, 1)
            except Exception as exc:
                self.logger.error("Input handler error: %s", exc)
                await asyncio.sleep(0.2)
                continue

            if not key:
                await asyncio.sleep(0.1)
                continue

            key = key.lower()
            if key == "m":
                await self._show_menu()
            elif key == "q":
                self.logger.info("Exit requested from keyboard")
                self._running = False

    def _generate_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="top", ratio=2),
            Layout(name="bottom", ratio=1),
        )

        layout["top"].update(Panel(self._build_pairs_table(), title="Trading Pairs"))
        layout["bottom"].update(Panel(self._build_logs_table(), title="Last operations.log entries"))
        return layout

    def _build_pairs_table(self) -> Table:
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Symbol")
        table.add_column("Leverage", justify="right")
        table.add_column("TP", justify="right")
        table.add_column("SL", justify="right")
        table.add_column("Cancel Time", justify="right")
        table.add_column("Enabled", justify="center")

        pairs = []
        if self.pair_manager is not None:
            pairs = list(getattr(self.pair_manager, "pairs_cache", {}).values())

        if not pairs:
            table.add_row("-", "-", "-", "-", "-", "-")
            return table

        for pair in pairs:
            table.add_row(
                str(pair.get("symbol", "-")),
                str(pair.get("leverage", "-")),
                str(pair.get("tp", "-")),
                str(pair.get("sl", "-")),
                str(pair.get("cancel_time", "-")),
                "✅" if pair.get("enabled", False) else "❌",
            )
        return table

    def _build_logs_table(self) -> Table:
        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("#", justify="right", width=4)
        table.add_column("Log line")

        lines = self._read_last_log_lines(limit=10)
        if not lines:
            table.add_row("1", "operations.log is empty")
            return table

        for idx, line in enumerate(lines, start=1):
            table.add_row(str(idx), line)
        return table

    def _read_last_log_lines(self, limit: int = 10) -> list[str]:
        if not self.operations_log_path.exists():
            return []

        try:
            with self.operations_log_path.open("r", encoding="utf-8") as file:
                lines = [line.rstrip("\n") for line in file.readlines() if line.strip()]
            return lines[-limit:]
        except Exception as exc:
            self.logger.error("Failed to read operations.log: %s", exc)
            return []

    async def _show_menu(self) -> None:
        self.menu_mode = True
        try:
            while self._running:
                action = await asyncio.to_thread(
                    lambda: questionary.select(
                        "Main menu",
                        choices=[
                            "Управление парами",
                            "Настройки",
                            "API",
                            "Позиции",
                            "Логи",
                            "Назад",
                        ],
                    ).ask()
                )

                if action in (None, "Назад"):
                    break

                if action == "Управление парами":
                    await self._pairs_submenu()
                elif action == "Настройки":
                    await self._settings_submenu()
                elif action == "API":
                    await self._api_submenu()
                elif action == "Позиции":
                    await self._positions_submenu()
                elif action == "Логи":
                    await self._logs_submenu()
        finally:
            self.menu_mode = False

    async def _pairs_submenu(self) -> None:
        data = await asyncio.to_thread(
            lambda: questionary.form(
                symbol=questionary.text("Symbol (e.g. BTC/USDT:USDT)"),
                leverage=questionary.text("Leverage"),
                tp=questionary.text("TP (%)"),
                sl=questionary.text("SL (%)"),
                cancel_time=questionary.text("Cancel time (seconds)"),
                operation=questionary.select(
                    "Operation",
                    choices=["add", "update", "remove", "enable", "disable"],
                ),
            ).ask()
        )

        if not data or self.pair_manager is None:
            return

        symbol = data.get("symbol")
        op = data.get("operation")

        try:
            leverage = float(data.get("leverage")) if data.get("leverage") else None
            tp = float(data.get("tp")) if data.get("tp") else None
            sl = float(data.get("sl")) if data.get("sl") else None
            cancel_time = float(data.get("cancel_time")) if data.get("cancel_time") else None
        except ValueError:
            self.console.print("[red]Invalid numeric input for pair settings[/red]")
            return

        if op == "add":
            await self.pair_manager.add_pair(symbol, leverage, tp, sl, cancel_time)
        elif op == "update":
            await self.pair_manager.update_pair(symbol, leverage, tp, sl, cancel_time)
        elif op == "remove":
            await self.pair_manager.remove_pair(symbol)
        elif op == "enable":
            await self.pair_manager.enable_pair(symbol)
        elif op == "disable":
            await self.pair_manager.disable_pair(symbol)

    async def _settings_submenu(self) -> None:
        data = await asyncio.to_thread(
            lambda: questionary.form(
                key=questionary.select(
                    "Key",
                    choices=["lookback", "volume_multiplier", "check_interval", "risk_per_trade"],
                ),
                value=questionary.text("Value"),
            ).ask()
        )

        if not data or self.config_manager is None:
            return

        await self.config_manager.set(data["key"], data["value"])

    async def _api_submenu(self) -> None:
        data = await asyncio.to_thread(
            lambda: questionary.form(
                api_key=questionary.password("API Key"),
                api_secret=questionary.password("API Secret"),
            ).ask()
        )

        if not data or self.encrypted_settings is None:
            return

        await self.encrypted_settings.save_api_keys(data.get("api_key", ""), data.get("api_secret", ""))

    async def _positions_submenu(self) -> None:
        data = await asyncio.to_thread(
            lambda: questionary.form(
                symbol=questionary.text("Symbol"),
                side=questionary.select("Position side", choices=["buy", "sell"]),
                amount=questionary.text("Amount"),
            ).ask()
        )

        if not data or self.trader is None:
            return

        try:
            amount = float(data["amount"])
        except (TypeError, ValueError):
            self.console.print("[red]Invalid amount[/red]")
            return

        await self.trader.close_position(data["symbol"], data["side"], amount)

    async def _logs_submenu(self) -> None:
        if self.db_logger is not None:
            logs = await self.db_logger.get_last_logs(limit=10)
            self.console.print("\n[bold]Last DB logs:[/bold]")
            for level, message, created_at in logs:
                self.console.print(f"{created_at} [{level}] {message}")
        else:
            self.console.print("[yellow]DB logger is not configured[/yellow]")
