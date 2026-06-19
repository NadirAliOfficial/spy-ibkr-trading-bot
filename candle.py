from dataclasses import dataclass


@dataclass
class Candle:
    minute_ts: float
    open: float = 0.0
    high: float = 0.0
    low: float = float("inf")
    close: float = 0.0
    ticks: int = 0

    def update(self, price: float):
        if self.open == 0.0:
            self.open = self.high = self.low = price
        else:
            self.high = max(self.high, price)
            self.low = min(self.low, price)
        self.close = price
        self.ticks += 1


class CandleBuilder:
    def __init__(self):
        self.current: Candle | None = None
        self.history: list[Candle] = []

    def process_tick(self, price: float, ts: float) -> tuple[Candle, bool]:
        minute_ts = float(int(ts // 60) * 60)
        is_new = self.current is None or minute_ts != self.current.minute_ts

        if is_new:
            if self.current is not None:
                self.history.append(self.current)
            self.current = Candle(minute_ts=minute_ts)

        self.current.update(price)
        return self.current, is_new

    def seconds_into_candle(self, ts: float) -> float:
        return (ts - self.current.minute_ts) if self.current else 0.0

    def finalize(self):
        if self.current is not None:
            self.history.append(self.current)
            self.current = None
