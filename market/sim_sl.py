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
    Tracks simulated stop-loss triggers per 1-min candle without placing orders.
    Runs 9:30am–12:30pm ET (phase 1) and 12:30pm–4pm ET (phase 2).
    Drives Y2/Z2 re-entry conditions (fires at >= 2 triggers).

    A trigger = the trade's stop loss firing, i.e. price crossing back through
    the open band to the opposite side after an entry. The band is
    [open-0.01, open+0.01].

    - First cross of the band is the ENTRY, not a trigger.
    - Each subsequent cross to the opposite side is one stop-loss trigger
      (the stop fires and the position reverses).
    - A candle that moves one direction and stays = 0 triggers.
    """

    FLAT, LONG, SHORT = 0, 1, 2

    def __init__(self):
        self.records: list[CandleRecord] = []
        self._current: CandleRecord | None = None
        self._up: float = 0.0
        self._down: float = 0.0
        self._state: int = self.FLAT

    def new_candle(self, open_price: float, minute_ts: float):
        if self._current is not None:
            self.records.append(self._current)
        self._current = CandleRecord(minute_ts=minute_ts, open_price=open_price)
        self._up = round(open_price + 0.01, 2)
        self._down = round(open_price - 0.01, 2)
        self._state = self.FLAT

    def on_tick(self, price: float) -> int:
        if self._current is None:
            return 0

        if self._state == self.FLAT:
            if price >= self._up:
                self._state = self.LONG      # entry, not a trigger
            elif price <= self._down:
                self._state = self.SHORT     # entry, not a trigger
        elif self._state == self.LONG:
            if price <= self._down:          # stop fired, reverse to short
                self._current.sim_sl_hits += 1
                self._state = self.SHORT
        elif self._state == self.SHORT:
            if price >= self._up:            # stop fired, reverse to long
                self._current.sim_sl_hits += 1
                self._state = self.LONG

        return self._current.sim_sl_hits

    def finalize(self):
        if self._current is not None:
            self.records.append(self._current)
            self._current = None
