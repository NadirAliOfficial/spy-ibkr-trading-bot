import os

HOST = os.getenv("IBKR_HOST", "127.0.0.1")
PORT = int(os.getenv("IBKR_PORT", "7497"))    # 7497 = paper, 7496 = live
CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID", "1"))

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
SIM_SL_END_HOUR, SIM_SL_END_MIN = 12, 30
EOD_EXIT_HOUR, EOD_EXIT_MIN = 15, 59

# Risk
HARD_SL_PCT = 0.02
TP1_ARM_PCT = 0.045
TP1_TRAIL_PCT = 0.04
TP2_ARM_PCT = 0.105
TP2_TRAIL_PCT = 0.10
