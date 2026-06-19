import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class CandleRecord:
    minute_ts: float
    open_price: float
    sim_sl_hits: int = 0


class SimStopLoss:
    """
    Tracks simulated SL events per 1-min candle without placing orders.
    Runs 9:30am–12:30pm ET. Drives Y2/Z2 re-entry conditions.

    At each candle open: sim_long_sl = Open-0.01, sim_short_sl = Open+0.01.
    Each boundary touch increments hit count and rolls the level outward.
    Y2/Z2 fire when candle hits >= 2.
    """

    def __init__(self):
        self.records: list[CandleRecord] = []
        self._current: CandleRecord | None = None
        self._long_sl: float = 0.0
        self._short_sl: float = 0.0

    def new_candle(self, open_price: float, minute_ts: float):
        if self._current is not None:
            self.records.append(self._current)
        self._current = CandleRecord(minute_ts=minute_ts, open_price=open_price)
        self._long_sl = round(open_price - 0.01, 2)
        self._short_sl = round(open_price + 0.01, 2)

    def on_tick(self, price: float) -> int:
        if self._current is None:
            return 0
        if price <= self._long_sl:
            self._current.sim_sl_hits += 1
            self._long_sl = round(price - 0.01, 2)
        if price >= self._short_sl:
            self._current.sim_sl_hits += 1
            self._short_sl = round(price + 0.01, 2)
        return self._current.sim_sl_hits

    def finalize(self):
        if self._current is not None:
            self.records.append(self._current)
            self._current = None
