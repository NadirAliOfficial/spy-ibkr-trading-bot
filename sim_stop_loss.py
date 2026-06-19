"""
Simulated Stop Loss — runs 9:30am to 12:30pm ET.
Mirrors the real strategy stops without placing actual orders.

Logic:
  At each 1-min candle open:
    sim_long_sl  = Open - 0.01  (mirrors Y's STP3 stop)
    sim_short_sl = Open + 0.01  (mirrors Z's STP3 stop)

  On each tick:
    If price <= sim_long_sl : long SL hit, roll sim_long_sl down (price - 0.01)
    If price >= sim_short_sl: short SL hit, roll sim_short_sl up (price + 0.01)

  Count is per-candle. Y2/Z2 fire when total candle hits >= 2.
  No max entry limit applies to this sim tracker.
"""
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class CandleRecord:
    minute_ts: float
    open_price: float
    sim_sl_hits: int = 0


class SimStopLoss:
    def __init__(self):
        self.records: list[CandleRecord] = []
        self._current: CandleRecord | None = None
        self._sim_long_sl: float = 0.0
        self._sim_short_sl: float = 0.0

    def new_candle(self, open_price: float, minute_ts: float):
        if self._current is not None:
            self.records.append(self._current)
        self._current = CandleRecord(minute_ts=minute_ts, open_price=open_price)
        self._sim_long_sl = round(open_price - 0.01, 2)
        self._sim_short_sl = round(open_price + 0.01, 2)
        logger.debug("SimSL candle open=%.2f long_sl=%.2f short_sl=%.2f",
                     open_price, self._sim_long_sl, self._sim_short_sl)

    def on_tick(self, price: float) -> int:
        """
        Update sim SL state. Returns total sim SL hits for the current candle.
        """
        if self._current is None:
            return 0

        if price <= self._sim_long_sl:
            self._current.sim_sl_hits += 1
            self._sim_long_sl = round(price - 0.01, 2)
            logger.debug("SimSL long hit #%d @ %.2f new_sl=%.2f",
                         self._current.sim_sl_hits, price, self._sim_long_sl)

        if price >= self._sim_short_sl:
            self._current.sim_sl_hits += 1
            self._sim_short_sl = round(price + 0.01, 2)
            logger.debug("SimSL short hit #%d @ %.2f new_sl=%.2f",
                         self._current.sim_sl_hits, price, self._sim_short_sl)

        return self._current.sim_sl_hits

    def candle_hits(self) -> int:
        return self._current.sim_sl_hits if self._current else 0

    def finalize(self):
        if self._current is not None:
            self.records.append(self._current)
            self._current = None
