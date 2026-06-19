import logging

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Hard SL, TP1, TP2 trailing exits. All thresholds against 9:30am equity.
    current_pnl is updated by the order loop and read by timed exit tasks.
    """

    def __init__(self, starting_equity: float):
        self._eq = starting_equity
        self.current_pnl: float = 0.0
        self.done: bool = False

        self._hard_sl = -self._eq * 0.02

        self._tp1_armed = False
        self._tp1_exit = self._eq * 0.04

        self._tp2_armed = False
        self._tp2_trail = self._eq * 0.10
        self._tp2_peak_pct = 0.105

    def check(self, pnl: float) -> str | None:
        if self.done:
            return None

        self.current_pnl = pnl
        pct = pnl / self._eq

        if pnl <= self._hard_sl:
            self.done = True
            return f"hard_sl pnl={pnl:.2f}"

        if not self._tp1_armed and pct >= 0.045:
            self._tp1_armed = True
            logger.info("TP1 armed @ pnl=%.2f", pnl)

        if self._tp1_armed and pnl <= self._tp1_exit:
            self.done = True
            return f"tp1 pnl={pnl:.2f}"

        if not self._tp2_armed and pct >= 0.105:
            self._tp2_armed = True
            logger.info("TP2 armed @ pnl=%.2f", pnl)

        if self._tp2_armed:
            while pct >= self._tp2_peak_pct + 0.10:
                self._tp2_peak_pct += 0.10
                self._tp2_trail = self._eq * (self._tp2_peak_pct - 0.005)
                logger.info("TP2 trail extended: peak=%.0f%% trail=%.2f",
                            self._tp2_peak_pct * 100, self._tp2_trail)
            if pnl <= self._tp2_trail:
                self.done = True
                return f"tp2 pnl={pnl:.2f}"

        return None

    def check_noon(self, pnl: float) -> bool:
        if self.done:
            return False
        if pnl / self._eq < 0.045:
            self.done = True
            return True
        return False
