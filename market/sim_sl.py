import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_rp = lambda p: round(round(p / 0.01) * 0.01, 2)


@dataclass
class CandleRecord:
    minute_ts: float
    open_price: float
    sim_sl_hits: int = 0
    close_price: float = 0.0  # set at candle close for open-close stat


class SimStopLoss:
    """
    Simulated stop loss — records, per 1-min candle, how many times the trade's
    stop would have triggered. Runs 9:30am–12:30pm and 12:30pm–4pm ET.
    Drives Y2/Z2 re-entry (fires at >= 2 triggers).

    Logic (long, mirror for short):
      - Enter long when SPY >= Open+0.01.
      - When SPY <= Open-0.01, arm the stop at Bid-0.03 (capture Bid at that
        moment).
      - Record one trigger when SPY <= the armed stop level, then reverse to
        short and follow the same process (arm at Ask+0.03 when SPY >= Open+0.01).
      - If SPY recovers to the favourable side before the stop fires, the stop
        disarms.
      - A candle that moves one direction and never hits the stop records 0.
    """

    FLAT, LONG, SHORT = 0, 1, 2

    def __init__(self):
        self.records: list[CandleRecord] = []
        self._current: CandleRecord | None = None
        self._up: float = 0.0
        self._down: float = 0.0
        self._state: int = self.FLAT
        self._stop: float | None = None

    def new_candle(self, open_price: float, minute_ts: float, prev_close: float = 0.0):
        if self._current is not None:
            if prev_close > 0:
                self._current.close_price = prev_close
            self.records.append(self._current)
        self._current = CandleRecord(minute_ts=minute_ts, open_price=open_price)
        self._up = _rp(open_price + 0.01)
        self._down = _rp(open_price - 0.01)
        self._state = self.FLAT
        self._stop = None

    def on_tick(self, price: float, bid: float = 0.0, ask: float = 0.0) -> int:
        if self._current is None:
            return 0

        b = bid if bid > 0 else price
        a = ask if ask > 0 else price

        if self._state == self.FLAT:
            if price >= self._up:
                self._state = self.LONG
            elif price <= self._down:
                self._state = self.SHORT

        elif self._state == self.LONG:
            if price >= self._up:
                self._stop = None                       # recovered — disarm
            elif self._stop is None and price <= self._down:
                self._stop = _rp(b - 0.03)              # arm at Bid-0.03
            if self._stop is not None and price <= self._stop:
                self._current.sim_sl_hits += 1          # stop fired
                self._state = self.SHORT
                self._stop = None

        elif self._state == self.SHORT:
            if price <= self._down:
                self._stop = None                       # recovered — disarm
            elif self._stop is None and price >= self._up:
                self._stop = _rp(a + 0.03)              # arm at Ask+0.03
            if self._stop is not None and price >= self._stop:
                self._current.sim_sl_hits += 1          # stop fired
                self._state = self.LONG
                self._stop = None

        return self._current.sim_sl_hits

    def finalize(self):
        if self._current is not None:
            self.records.append(self._current)
            self._current = None
