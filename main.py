"""
SPY IBKR Trading Bot — Milestone 1
Asyncio producer/consumer architecture:
  - Tick monitoring loop (market data producer -> tick_queue)
  - Order deployment loop (order consumer -> order_queue)
"""
import asyncio
import logging
import math
import sys
import time
import datetime
from zoneinfo import ZoneInfo

import config
from connection import connect, IBApp
from candle import CandleBuilder, Candle
from sim_stop_loss import SimStopLoss
from order_manager import OrderManager
from risk_manager import RiskManager
from post_trade import generate_report, save_report
from utils import (
    now_et, et_time, is_early_close, calc_qty, parse_trading_hours
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("spy_bot.log"),
    ],
)
logger = logging.getLogger("main")

ET = ZoneInfo("America/New_York")

# Tick type constants
TICK_LAST = 4
TICK_BID = 1
TICK_ASK = 2


# ---------------------------------------------------------------------------
# Tick monitoring loop (producer)
# ---------------------------------------------------------------------------

async def tick_loop(app: IBApp, candle_builder: CandleBuilder, sim_sl: SimStopLoss,
                    order_mgr: OrderManager, risk_mgr: RiskManager,
                    market_close_time: datetime.datetime, sim_sl_end: datetime.datetime):
    """
    Consumes raw ticks from tick_queue.
    Updates candles, sim SL, and signals order_manager via direct async calls.
    This loop never waits on order logic — fires and forgets via asyncio tasks.
    """
    logger.info("Tick loop started")
    bid = ask = 0.0
    sim_active = True

    while True:
        try:
            event = await asyncio.wait_for(app.tick_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            # Check session end
            if now_et() >= market_close_time:
                logger.info("Market close time reached — stopping tick loop")
                break
            continue

        if event["type"] != "tick_price":
            continue

        tick_type = event["tickType"]
        price = event["price"]
        ts = event["ts"]

        if tick_type == TICK_BID:
            bid = price
            order_mgr._last_bid = price
            continue
        if tick_type == TICK_ASK:
            ask = price
            order_mgr._last_ask = price
            continue
        if tick_type != TICK_LAST:
            continue

        # ---- Process Last tick ----
        now = now_et()
        candle, is_new = candle_builder.process_tick(price, ts)

        # Sim SL (active until 12:30pm ET)
        sim_sl_hits = 0
        if sim_active:
            if now >= sim_sl_end:
                sim_active = False
                sim_sl.finalize()
                logger.info("Simulated SL window closed at 12:30pm")
            else:
                if is_new:
                    sim_sl.new_candle(candle.open, candle.minute_ts)
                sim_sl_hits = sim_sl.on_tick(price)

        # New candle → signal order manager (fire-and-forget)
        if is_new:
            asyncio.create_task(order_mgr.on_candle_open(candle.open))

        # 59th second → exit all
        secs_in = candle_builder.seconds_into_candle(ts)
        if 59.0 <= secs_in < 60.0:
            asyncio.create_task(order_mgr.on_59th_second())

        # Per-tick order manager update
        asyncio.create_task(order_mgr.on_tick(price, bid, ask, sim_sl_hits))


# ---------------------------------------------------------------------------
# Order event loop (consumer)
# ---------------------------------------------------------------------------

async def order_loop(app: IBApp, order_mgr: OrderManager, risk_mgr: RiskManager):
    """
    Consumes order/fill events from order_queue.
    Manages PnL and global exit conditions.
    """
    logger.info("Order loop started")
    current_pnl = 0.0

    while True:
        try:
            event = await asyncio.wait_for(app.order_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            if risk_mgr.done:
                logger.info("Risk manager done — order loop exiting")
                break
            continue

        etype = event.get("type")

        if etype == "exec":
            order_id = event["orderId"]
            fill_price = event["price"]
            filled_qty = event["shares"]
            await order_mgr.on_fill(order_id, fill_price, filled_qty)

        elif etype == "order_status":
            status = event["status"]
            order_id = event["orderId"]
            filled = event["filled"]
            remaining = event["remaining"]

            if status == "PartiallyFilled" and remaining > 0:
                await order_mgr.on_partial_fill(order_id)
            elif status in ("Cancelled", "Filled", "Inactive"):
                logger.debug("Order %d: %s", order_id, status)

        elif etype == "pnl":
            current_pnl = event["dailyPnL"]
            exit_reason = risk_mgr.check(current_pnl)
            if exit_reason:
                logger.warning("Risk exit triggered: %s", exit_reason)
                await order_mgr.exit_all(exit_reason)

        elif etype == "error":
            pass  # errors already logged in connection.py


# ---------------------------------------------------------------------------
# 12:30pm and 3:59pm timer tasks
# ---------------------------------------------------------------------------

async def noon_exit_task(order_mgr: OrderManager, risk_mgr: RiskManager, app: IBApp):
    """Wait until 12:30pm ET, check PnL, exit if < 4.5%."""
    noon = et_time(12, 30)
    now = now_et()
    wait_secs = (noon - now).total_seconds()
    if wait_secs > 0:
        await asyncio.sleep(wait_secs)
    pnl = _get_daily_pnl(app)
    if risk_mgr.check_noon(pnl):
        logger.warning("12:30pm exit: PnL=%.2f < 4.5%%", pnl)
        await order_mgr.exit_all("12:30pm noon exit")


async def eod_exit_task(order_mgr: OrderManager, risk_mgr: RiskManager):
    """Wait until 3:59pm ET and exit all."""
    eod = et_time(15, 59)
    now = now_et()
    wait_secs = (eod - now).total_seconds()
    if wait_secs > 0:
        await asyncio.sleep(wait_secs)
    if not risk_mgr.done:
        logger.info("3:59pm timer exit")
        risk_mgr.done = True
        await order_mgr.exit_all("3:59pm eod")


def _get_daily_pnl(app: IBApp) -> float:
    return 0.0  # Will be provided via PnL subscription events


# ---------------------------------------------------------------------------
# Startup: 8:25am trading hours check
# ---------------------------------------------------------------------------

async def wait_for_time(target: datetime.datetime, label: str):
    now = now_et()
    wait = (target - now).total_seconds()
    if wait > 0:
        logger.info("Waiting %.0fs until %s", wait, label)
        await asyncio.sleep(wait)


async def run():
    loop = asyncio.get_running_loop()
    app = connect(config.HOST, config.PORT, config.CLIENT_ID, loop)

    logger.info("Waiting for IBKR connection...")
    await asyncio.wait_for(app.connected.wait(), timeout=30)

    # ---- 8:25am: trading hours check ----
    await wait_for_time(et_time(8, 25), "8:25am pre-check")
    logger.info("Checking SPY trading hours...")
    try:
        trading_hours = await app.fetch_trading_hours()
    except asyncio.TimeoutError:
        logger.error("Failed to fetch trading hours — aborting")
        return

    if is_early_close(trading_hours):
        logger.warning("Early close day detected — no trading today")
        return

    sessions = parse_trading_hours(trading_hours)
    if not sessions:
        logger.warning("No trading session found for today — aborting")
        return

    _, session_close = sessions[0]
    logger.info("Session close: %s", session_close.strftime("%H:%M ET"))

    # ---- Subscribe to SPY market data ----
    spy_req_id = app.next_id()
    from connection import make_spy_contract
    contract = make_spy_contract()
    app.reqMktData(spy_req_id, contract, "236", False, False, [])  # 236 = SellInitMarginReq

    # ---- Wait until 9:30am ----
    await wait_for_time(et_time(9, 30), "9:30am market open")

    # ---- Fetch account values ----
    logger.info("Fetching account summary...")
    try:
        elv, sell_margin = await app.fetch_account_summary()
    except asyncio.TimeoutError:
        logger.error("Account summary timeout — aborting")
        return

    if elv <= 0:
        logger.error("EquityWithLoanValue=0 — aborting")
        return

    # Sell SPY Initial Margin per share: use IBKR value if available, else estimate
    if sell_margin <= 0 or sell_margin > elv:
        # Fallback: Reg T short margin = 150% of last SPY price
        spy_price = app.spy_sell_init_margin if app.spy_sell_init_margin > 0 else 550.0
        sell_margin = round(spy_price * 1.5, 2)
        logger.info("Using estimated sell init margin: %.2f", sell_margin)

    total_qty = math.floor(elv / sell_margin)
    leg_qty = math.floor(total_qty * config.EQUITY_PCT)
    if leg_qty < 1:
        logger.error("Calculated leg qty < 1 — check account equity. ELV=%.2f margin=%.2f",
                     elv, sell_margin)
        return

    logger.info("ELV=%.2f  SellInitMargin=%.2f  TotalQty=%d  LegQty=%d",
                elv, sell_margin, total_qty, leg_qty)

    # ---- Subscribe to PnL ----
    pnl_req_id = app.next_id()
    acct = ""  # uses default account
    app.reqPnLSingle(pnl_req_id, acct, "", spy_req_id)  # simplified; full impl uses position tracking

    # ---- Initialize components ----
    candle_builder = CandleBuilder()
    sim_sl = SimStopLoss()
    order_mgr = OrderManager(app, leg_qty)
    risk_mgr = RiskManager(elv)

    sim_sl_end = et_time(12, 30)
    market_close = et_time(15, 59)

    # ---- Launch all async tasks ----
    await asyncio.gather(
        tick_loop(app, candle_builder, sim_sl, order_mgr, risk_mgr, market_close, sim_sl_end),
        order_loop(app, order_mgr, risk_mgr),
        noon_exit_task(order_mgr, risk_mgr, app),
        eod_exit_task(order_mgr, risk_mgr),
    )

    # ---- Post-trade report ----
    sim_sl.finalize()
    report = generate_report(sim_sl.records, candle_builder.history)
    print(report)
    save_report(report)

    app.disconnect()
    logger.info("Session complete")


if __name__ == "__main__":
    asyncio.run(run())
