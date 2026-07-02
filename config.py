import os
from pathlib import Path

# load .env if present
_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

HOST = os.getenv("IBKR_HOST", "127.0.0.1")
PORT = int(os.getenv("IBKR_PORT", "7497"))    # 7497 = paper, 7496 = live
CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID", "1"))
ACCOUNT_ID = os.getenv("IBKR_ACCOUNT", "")    # target account when login has >1
SIM_ONLY = os.getenv("SIM_ONLY", "") == "1"   # flatten once, then no live orders, sim SL only

SYMBOL = "SPY"
EXCHANGE = "SMART"
CURRENCY = "USD"
SEC_TYPE = "STK"

# Tick types — real-time
TICK_BID = 1
TICK_ASK = 2
TICK_LAST = 4

# Tick types — delayed (paper accounts, reqMarketDataType(3))
DTICK_BID = 66
DTICK_ASK = 67
DTICK_LAST = 68

# Strategy
OPEN_OFFSET = 0.01
EQUITY_PCT = 0.49
CANDLE_SECONDS = 60
MAX_ENTRIES_PER_CANDLE = 4

# Session times (ET)
PRE_CHECK_HOUR, PRE_CHECK_MIN = 8, 25
OPEN_HOUR, OPEN_MIN = 9, 30
_sim_sl_end = os.getenv("SIM_SL_END", "12:30").split(":")
SIM_SL_END_HOUR, SIM_SL_END_MIN = int(_sim_sl_end[0]), int(_sim_sl_end[1])
_eod = os.getenv("EOD_EXIT", "15:59").split(":")
EOD_EXIT_HOUR, EOD_EXIT_MIN = int(_eod[0]), int(_eod[1])

# Risk
HARD_SL_PCT = 0.02
TP1_ARM_PCT = 0.045
TP1_TRAIL_PCT = 0.04
TP2_ARM_PCT = 0.105
TP2_TRAIL_PCT = 0.10

# Email report
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
REPORT_EMAIL_TO = os.getenv("REPORT_EMAIL_TO", "")
