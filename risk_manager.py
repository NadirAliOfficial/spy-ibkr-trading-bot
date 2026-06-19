"""
Risk manager: hard SL, TP1, TP2, 12:30pm condition, 3:59pm exit.
All PnL thresholds are against 9:30am ET EquityWithLoanValue.
"""
import logging

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, starting_equity: float):
        self._eq = starting_equity

        # Hard SL: exit if PnL <= -2% of starting equity
        self._hard_sl = -self._eq * 0.02

        # TP1: arm when PnL >= 4.5%; trail exit at 4%
        self._tp1_armed = False
        self._tp1_exit = self._eq * 0.04

        # TP2: arm when PnL >= 10.5%; trail exit at 10%; extends by 10% each new peak
        self._tp2_armed = False
        self._tp2_trail_level = self._eq * 0.10
        self._tp2_peak_pct = 0.105

        self.done = False  # no-re-entry flag

    def check(self, pnl: float) -> str | None:
        """
        Returns exit reason string if an exit condition is triggered, else None.
        Call this on every PnL update.
        """
        if self.done:
            return None

        pnl_pct = pnl / self._eq if self._eq else 0

        # Hard stop loss
        if pnl <= self._hard_sl:
            self.done = True
            return f"hard_sl pnl={pnl:.2f}"

        # TP1
        if not self._tp1_armed and pnl_pct >= 0.045:
            self._tp1_armed = True
            logger.info("TP1 armed: PnL=%.2f (%.2f%%)", pnl, pnl_pct * 100)

        if self._tp1_armed and pnl <= self._tp1_exit:
            self.done = True
            return f"tp1 pnl={pnl:.2f}"

        # TP2
        if not self._tp2_armed and pnl_pct >= 0.105:
            self._tp2_armed = True
            logger.info("TP2 armed: PnL=%.2f (%.2f%%)", pnl, pnl_pct * 100)

        if self._tp2_armed:
            # Extend TP2 trail peak for every +10% gain above 10.5%
            while pnl_pct >= self._tp2_peak_pct + 0.10:
                self._tp2_peak_pct += 0.10
                self._tp2_trail_level = self._eq * (self._tp2_peak_pct - 0.005)
                logger.info("TP2 trail extended: new peak=%.1f%% trail_exit=%.2f",
                            self._tp2_peak_pct * 100, self._tp2_trail_level)

            if pnl <= self._tp2_trail_level:
                self.done = True
                return f"tp2 pnl={pnl:.2f}"

        return None

    def check_noon(self, pnl: float) -> bool:
        """Returns True if 12:30pm exit condition is met (PnL < 4.5%)."""
        if self.done:
            return False
        pnl_pct = pnl / self._eq if self._eq else 0
        if pnl_pct < 0.045:
            self.done = True
            return True
        return False
