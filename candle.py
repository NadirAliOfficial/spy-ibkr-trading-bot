import time
from dataclasses import dataclass, field


@dataclass
class Candle:
    minute_ts: float        # unix timestamp of candle open (floored to minute)
    open: float = 0.0
    high: float = 0.0
    low: float = float("inf")
    close: float = 0.0
    ticks: int = 0
    sim_sl_hits: int = 0    # how many times simulated SL was touched this candle

    def update(self, price: float):
        if self.open == 0.0:
            self.open = price
            self.high = price
            self.low = price
        else:
            self.high = max(self.high, price)
            self.low = min(self.low, price)
        self.close = price
        self.ticks += 1

    @property
    def range(self) -> float:
        return abs(self.close - self.open)


class CandleBuilder:
    def __init__(self):
        self.current: Candle | None = None
        self.history: list[Candle] = []

    def process_tick(self, price: float, ts: float) -> tuple[Candle | None, bool]:
        """
        Feed a tick. Returns (current_candle, is_new_candle).
        is_new_candle=True means a fresh 1-min candle just opened.
        """
        minute_ts = float(int(ts // 60) * 60)
        is_new = False

        if self.current is None or minute_ts != self.current.minute_ts:
            if self.current is not None:
                self.history.append(self.current)
            self.current = Candle(minute_ts=minute_ts)
            is_new = True

        self.current.update(price)
        return self.current, is_new

    def seconds_into_candle(self, ts: float) -> float:
        if self.current is None:
            return 0.0
        return ts - self.current.minute_ts
