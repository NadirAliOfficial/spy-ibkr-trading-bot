import math
import re
import datetime
import logging
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


def now_et() -> datetime.datetime:
    return datetime.datetime.now(tz=ET)


def et_time(hour: int, minute: int, second: int = 0) -> datetime.datetime:
    now = now_et()
    return now.replace(hour=hour, minute=minute, second=second, microsecond=0)


def calc_qty(equity_with_loan: float, sell_init_margin: float, pct: float = 0.49) -> int:
    """
    Quantity formula: floor(EquityWithLoanValue / SellInitMargin) * pct per leg.
    sell_init_margin is the per-share initial margin requirement for a short.
    """
    if sell_init_margin <= 0:
        logger.warning("sell_init_margin is zero — cannot size position")
        return 0
    total = math.floor(equity_with_loan / sell_init_margin)
    return max(1, math.floor(total * pct))


def parse_trading_hours(trading_hours: str) -> list[tuple[datetime.datetime, datetime.datetime]]:
    """
    Parse IBKR tradingHours string like '20240101:0930-20240101:1600;20240102:CLOSED;...'
    Returns list of (open_dt, close_dt) for today.
    """
    today = now_et().strftime("%Y%m%d")
    segments = [s.strip() for s in trading_hours.split(";") if today in s]
    sessions = []
    for seg in segments:
        if "CLOSED" in seg:
            continue
        m = re.match(r"(\d{8}):(\d{4})-(\d{8}):(\d{4})", seg)
        if not m:
            continue
        open_dt = datetime.datetime.strptime(f"{m.group(1)}{m.group(2)}", "%Y%m%d%H%M").replace(tzinfo=ET)
        close_dt = datetime.datetime.strptime(f"{m.group(3)}{m.group(4)}", "%Y%m%d%H%M").replace(tzinfo=ET)
        sessions.append((open_dt, close_dt))
    return sessions


def is_early_close(trading_hours: str) -> bool:
    """Return True if today's session closes before 4:00pm ET."""
    sessions = parse_trading_hours(trading_hours)
    if not sessions:
        return True   # no session found = treat as closed
    _, close_dt = sessions[0]
    normal_close = et_time(16, 0)
    return close_dt < normal_close


def round_price(price: float, increment: float = 0.01) -> float:
    return round(round(price / increment) * increment, 2)
