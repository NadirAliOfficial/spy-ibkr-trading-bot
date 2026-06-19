import math
import re
import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def now_et() -> datetime.datetime:
    return datetime.datetime.now(tz=ET)


def et_time(hour: int, minute: int, second: int = 0) -> datetime.datetime:
    return now_et().replace(hour=hour, minute=minute, second=second, microsecond=0)


def calc_leg_qty(equity: float, sell_init_margin: float, pct: float = 0.49) -> int:
    if sell_init_margin <= 0:
        return 0
    return max(1, math.floor(math.floor(equity / sell_init_margin) * pct))


def parse_trading_hours(raw: str) -> list[tuple[datetime.datetime, datetime.datetime]]:
    today = now_et().strftime("%Y%m%d")
    sessions = []
    for seg in raw.split(";"):
        seg = seg.strip()
        if today not in seg or "CLOSED" in seg:
            continue
        m = re.match(r"(\d{8}):(\d{4})-(\d{8}):(\d{4})", seg)
        if not m:
            continue
        open_dt  = datetime.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M").replace(tzinfo=ET)
        close_dt = datetime.datetime.strptime(m.group(3) + m.group(4), "%Y%m%d%H%M").replace(tzinfo=ET)
        sessions.append((open_dt, close_dt))
    return sessions


def is_early_close(raw: str) -> bool:
    sessions = parse_trading_hours(raw)
    return not sessions or sessions[0][1] < et_time(16, 0)
