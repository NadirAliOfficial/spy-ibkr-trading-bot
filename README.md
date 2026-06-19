# SPY IBKR Trading Bot

Automated intraday trading bot for SPY (SPDR S&P 500 ETF) built on the Interactive Brokers TWS API. Uses a high-frequency asyncio architecture to decouple market data ingestion from order execution with zero latency bloat.

## Features

- **Asyncio producer/consumer pipeline** — tick monitoring loop and order deployment loop run fully independently via `asyncio.Queue`, ensuring market data never waits on order logic
- **OCO order pairs (Y & Z)** — simultaneous BUY STP and SELL STP entries at each 1-minute candle open, one cancels the other on fill
- **STP3 stop management** — 1-share STP parent + MKT child exit structure; cancels when price moves in favor, recalculates and replaces when price approaches stop
- **Reverse trade logic** — after a stop-out, automatically enters the opposite direction (Y reverses to SELL STP @ Bid−0.03; Z reverses to BUY STP @ Ask+0.03)
- **Y2 / Z2 re-entries** — additional OCO pairs triggered when the simulated stop loss fires 2+ times inside the current 1-minute candle
- **1-second exit condition** — halts all trading within a candle if the actual stop fires twice in 1 second (prevents whipsaw losses)
- **59th-second timer exit** — exits all positions at candle open + 59 seconds, rolling each candle
- **Risk management** — Hard SL (−2% equity), TP1 (4.5% entry / 4% trail), TP2 (10.5% entry / 10% trail with automatic extension), 12:30pm PnL gate, 3:59pm EOD exit
- **Simulated stop loss tracker** — runs 9:30am–12:30pm ET, records per-candle SL hit counts without placing orders, drives Y2/Z2 logic
- **Post-trade report** — SL trigger counts per candle sorted by mode, percentage breakdown, mean |Open−Close| per 1-min candle
- **Position sizing** — `EquityWithLoanValue / Sell SPY Initial Margin` rounded down; 49% equity per order leg

## Architecture

```
IBKR Gateway / TWS
       │
  IBApp (EWrapper + EClient)
  ┌────┴────────────┐
  │  tick_queue     │  ◄── tickPrice callbacks (non-blocking)
  │  order_queue    │  ◄── orderStatus / execDetails callbacks
  └────┬────────────┘
       │
  ┌────▼────────────────────────────────┐
  │  Tick Monitoring Loop               │  producer
  │  CandleBuilder / SimStopLoss        │
  │  → signals OrderManager via tasks   │
  └────┬────────────────────────────────┘
       │  asyncio.create_task (fire & forget)
  ┌────▼────────────────────────────────┐
  │  Order Deployment Loop              │  consumer
  │  OrderManager / RiskManager         │
  │  → placeOrder / cancelOrder         │
  └─────────────────────────────────────┘
```

## Requirements

- Python 3.11+
- IB Gateway or TWS (paper or live account)
- `ibapi` — Interactive Brokers official Python API

```bash
pip install -r requirements.txt
```

## Configuration

Edit `config.py` or set environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `IBKR_HOST` | `127.0.0.1` | IB Gateway host |
| `IBKR_PORT` | `7497` | 7497 = paper, 7496 = live |
| `IBKR_CLIENT_ID` | `1` | Client ID for the connection |

**IB Gateway setup:** Enable "Bypass Order Precautions" for Price and Size to prevent confirmation pop-ups from blocking the asyncio loop.

## Usage

```bash
python main.py
```

The bot will:
1. Connect to IB Gateway at 8:25am ET and verify SPY trading hours
2. Skip trading on early-close days (session close before 4:00pm ET)
3. Fetch account equity and calculate position size at 9:30am ET
4. Run the Y/Z OCO strategy from 9:30am to 3:59pm ET
5. Print and save the post-trade report on session close

## Trading Schedule

| Time | Action |
|------|--------|
| 8:25am ET | Trading hours check via `reqContractDetails` |
| 9:30am ET | Strategy starts — Y/Z OCO placed at each candle open |
| 9:30–12:30pm | Simulated SL tracker active |
| 12:30pm | Exit all if daily PnL < 4.5% |
| 3:59pm | Force exit all positions and orders |

## Project Structure

```
├── main.py           # Entry point and session orchestration
├── config.py         # Configuration constants
├── connection.py     # IBKR EWrapper/EClient with asyncio queue bridge
├── candle.py         # 1-minute candle builder
├── order_manager.py  # Y/Z OCO logic, STP3, reverse trades
├── sim_stop_loss.py  # Simulated stop loss tracker
├── risk_manager.py   # Hard SL, TP1, TP2, PnL gates
├── post_trade.py     # Post-session report generation
├── utils.py          # Trading hours parser, position sizing, ET helpers
└── requirements.txt
```

## Disclaimer

This software is for educational and personal use. Trading financial instruments involves significant risk of loss. Always test thoroughly on a paper account before using with live funds.
