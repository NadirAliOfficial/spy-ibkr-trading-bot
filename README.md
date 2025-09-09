# SPY IBKR Trading Bot

Automated intraday bot for SPY built on the Interactive Brokers TWS API. Asyncio producer/consumer architecture keeps market data ingestion and order execution fully decoupled.

## Strategy

- **Y/Z OCO** — BUY STP and SELL STP placed simultaneously at each 1-min candle open. First to fill cancels the other.
- **STP3 exit** — 1-share STP parent + MKT child. Cancels when price moves in favor, rolls when price approaches stop.
- **Reverse trades** — on stop-out, enters opposite direction (Y → SELL STP @ Bid−0.03 / Z → BUY STP @ Ask+0.03)
- **Y2/Z2 re-entries** — additional OCO pair fires when simulated SL hits ≥ 2 times in the current candle
- **1-second exit** — halts candle trading if the actual stop fires twice within 1 second
- **59th-second exit** — flattens all positions at candle open + 59s, rolling every candle

## Risk Controls

| Condition | Action |
|-----------|--------|
| PnL ≤ −2% equity | Hard stop — exit all, no re-entry |
| PnL ≥ 4.5% → trails ≤ 4% | TP1 exit |
| PnL ≥ 10.5% → trails ≤ 10% | TP2 exit (extends +10% per new peak) |
| 12:30pm ET, PnL < 4.5% | Exit all, no re-entry |
| 3:59pm ET | Force exit all, no re-entry |

## Architecture

```
IB Gateway / TWS
      │
  IBApp (EWrapper + EClient)
      ├── tick_queue   ◄─ tickPrice callbacks
      └── order_queue  ◄─ orderStatus / execDetails / pnl callbacks
            │
      ┌─────▼──────────────────────┐
      │  Tick Loop (producer)      │  CandleBuilder + SimStopLoss
      │  asyncio.create_task ──►  │  signals OrderManager
      └─────────────────────────── ┘
      ┌─────────────────────────── ┐
      │  Order Loop (consumer)     │  OrderManager + RiskManager
      └─────────────────────────── ┘
```

## Structure

```
├── main.py           entry point — tick loop, order loop, timer tasks
├── config.py         all constants and env vars
├── utils.py          ET time helpers, position sizing, trading hours parser
├── gateway/
│   └── app.py        IBApp (EWrapper + EClient), connect(), spy_contract()
├── market/
│   ├── candle.py     1-min candle builder
│   └── sim_sl.py     simulated stop loss tracker (9:30–12:30 ET)
├── strategy/
│   ├── orders.py     Side, OrderGroup, STP/MKT order factories
│   └── manager.py    OrderManager — Y/Z OCO, STP3, reverses, exits
├── risk/
│   ├── manager.py    RiskManager — hard SL, TP1/TP2, noon gate
│   └── report.py     post-trade report generator
└── test_connection.py  paper account connection verifier
```

## Setup

```bash
pip install -r requirements.txt
```

In IB Gateway: enable **Bypass Order Precautions** for Price and Size.

| Env var | Default | |
|---------|---------|--|
| `IBKR_HOST` | `127.0.0.1` | |
| `IBKR_PORT` | `7497` | 7497 = paper, 7496 = live |
| `IBKR_CLIENT_ID` | `1` | |

## Usage

```bash
# Verify paper account connection first
python test_connection.py

# Run the bot
python main.py
```

The bot waits until 8:25am ET, checks trading hours, skips early-close days, then runs the strategy from 9:30am to 3:59pm ET and prints a post-trade report on close.

## Disclaimer

Trading involves significant risk of loss. Test on a paper account before going live.
