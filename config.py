import os

# IBKR connection
HOST = os.getenv("IBKR_HOST", "127.0.0.1")
PORT = int(os.getenv("IBKR_PORT", "7497"))   # 7497 = paper, 7496 = live
CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID", "1"))

# SPY contract
SYMBOL = "SPY"
EXCHANGE = "SMART"
CURRENCY = "USD"
SEC_TYPE = "STK"

# Strategy parameters
OPEN_OFFSET = 0.01           # trigger offset from candle open
EQUITY_PCT = 0.49            # 49% equity per parent/child leg
CANDLE_SECONDS = 60
MAX_ENTRIES_PER_CANDLE = 4

# Tick types used (IBKR constants)
TICK_LAST = 4
TICK_SELL_INIT_MARGIN = 236  # for position sizing

# Market hours ET
MARKET_OPEN_TIME = (9, 30)
MARKET_CLOSE_TIME = (15, 59)  # exit at 3:59pm
PRE_CHECK_TIME = (8, 25)
SIM_SL_END_TIME = (12, 30)
NOON_EXIT_TIME = (12, 30)

# Risk parameters
HARD_SL_PCT = 0.02           # -2% equity
TP1_ENTRY_PCT = 0.045        # 4.5% PnL
TP1_TRAIL_PCT = 0.04         # trail back to 4%
TP2_ENTRY_PCT = 0.105        # 10.5% PnL
TP2_TRAIL_PCT = 0.10         # trail back to 10%
NOON_MIN_PNL_PCT = 0.045     # if PnL < 4.5% at 12:30pm, exit
