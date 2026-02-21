import logging
from typing import Any, Optional


class SignalGenerator:
    def __init__(self, exchange: Any, logger: Optional[logging.Logger] = None) -> None:
        self.exchange = exchange
        self.logger = logger or logging.getLogger(__name__)

    async def fetch_ohlcv(self, symbol: str, lookback: int):
        try:
            candles = await self.exchange.fetch_ohlcv(symbol, timeframe="1m", limit=lookback + 5)
        except Exception as exc:
            self.logger.error("Failed to fetch OHLCV for %s: %s", symbol, exc)
            return None

        if candles is None:
            self.logger.warning("Exchange returned None OHLCV for %s", symbol)
            return None

        if not isinstance(candles, list) or len(candles) < lookback + 2:
            self.logger.warning(
                "Not enough OHLCV data for %s: got=%s required>=%s",
                symbol,
                len(candles) if isinstance(candles, list) else "invalid",
                lookback + 2,
            )
            return None

        return candles

    def calculate_indicators(self, candles: list[list[float]], lookback: int):
        if not candles or len(candles) < lookback + 2:
            return None

        try:
            closes = [float(c[4]) for c in candles]
            volumes = [float(c[5]) for c in candles]
        except (TypeError, ValueError, IndexError) as exc:
            self.logger.error("Malformed OHLCV data: %s", exc)
            return None

        reference_closes = closes[-(lookback + 1) : -1]
        reference_volumes = volumes[-(lookback + 1) : -1]

        if not reference_closes or not reference_volumes:
            return None

        local_high = max(reference_closes)
        local_low = min(reference_closes)
        avg_volume = sum(reference_volumes) / len(reference_volumes)
        momentum = closes[-1] - closes[-2]

        return {
            "close": closes[-1],
            "volume": volumes[-1],
            "local_high": local_high,
            "local_low": local_low,
            "avg_volume": avg_volume,
            "momentum": momentum,
        }

    async def generate_signal(self, symbol: str, lookback: int, volume_multiplier: float):
        candles = await self.fetch_ohlcv(symbol=symbol, lookback=lookback)
        if candles is None:
            return None

        indicators = self.calculate_indicators(candles, lookback)
        if indicators is None:
            return None

        close = indicators["close"]
        volume = indicators["volume"]
        local_high = indicators["local_high"]
        local_low = indicators["local_low"]
        avg_volume = indicators["avg_volume"]
        momentum = indicators["momentum"]

        if close > local_high and volume > avg_volume * volume_multiplier and momentum > 0:
            return "LONG"

        if close < local_low and volume > avg_volume * volume_multiplier and momentum < 0:
            return "SHORT"

        return None
