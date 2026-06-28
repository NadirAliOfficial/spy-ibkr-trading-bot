import asyncio
import logging
import sys
from datetime import timedelta
from pathlib import Path

import config
from gateway import connect, spy_contract
from market import CandleBuilder, SimStopLoss
from strategy import OrderManager
from risk import RiskManager, generate_report, save_report, email_report
from utils import now_et, et_time, is_early_close, parse_trading_hours, calc_leg_qty

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logging.getLogger("ibapi").setLevel(logging.WARNING)
logger = logging.getLogger("main")

# Marks the day as finished so a mid-session restart does not re-enter trading
# after a terminal exit (12:30pm exit, hard SL, TP, or EOD). No re-entry.
STATE_FILE = Path(__file__).parent / "day_state.txt"


def _today_str() -> str:
    return now_et().strftime("%Y-%m-%d")


def mark_day_done():
    STATE_FILE.write_text(_today_str())
    logger.info("Day marked done: %s", _today_str())


def already_done_today() -> bool:
    return STATE_FILE.exists() and STATE_FILE.read_text(encoding="utf-8").strip() == _today_str()


async def tick_loop(app, candles: CandleBuilder, sim_sl_short: SimStopLoss, sim_sl_noon: SimStopLoss,
                    order_mgr: OrderManager, risk_mgr: RiskManager, session_end):
    logger.info("Tick loop started")
    fired_59s = False
    last_price = 0.0
    session_start_ts = et_time(config.OPEN_HOUR, config.OPEN_MIN).timestamp()
    eod_end = et_time(config.EOD_EXIT_HOUR, config.EOD_EXIT_MIN)

    # Run until 12:30pm — sim SL noon keeps tracking after trading stops at 10am.
    while now_et() < session_end:
        try:
            event = await asyncio.wait_for(app.tick_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            if now_et() >= session_end:
                break
            continue

        if event["type"] != "tick_price":
            continue

        tick_type, price, ts = event["tickType"], event["price"], event["ts"]

        if ts < session_start_ts:
            continue

        if tick_type in (config.TICK_BID, config.DTICK_BID):
            order_mgr.last_bid = price
            continue
        if tick_type in (config.TICK_ASK, config.DTICK_ASK):
            order_mgr.last_ask = price
            continue
        if tick_type not in (config.TICK_LAST, config.DTICK_LAST):
            continue

        if now_et() >= session_end:
            break

        candle, is_new = candles.process_tick(price, ts)
        prev_close = last_price
        last_price = price

        before_eod = now_et() < eod_end
        if is_new:
            if before_eod:
                sim_sl_short.new_candle(candle.open, candle.minute_ts, prev_close)
            sim_sl_noon.new_candle(candle.open, candle.minute_ts, prev_close)
            fired_59s = False

        if before_eod:
            sim_sl_short.on_tick(price, order_mgr.last_bid, order_mgr.last_ask)
        sim_hits = sim_sl_noon.on_tick(price, order_mgr.last_bid, order_mgr.last_ask)

        if not risk_mgr.done:
            if is_new:
                asyncio.create_task(order_mgr.on_candle_open(candle.open))
            if candles.seconds_into_candle(ts) >= 59.0 and not fired_59s:
                fired_59s = True
                asyncio.create_task(order_mgr.on_59th_second())
            asyncio.create_task(order_mgr.on_tick(price, sim_hits))

    logger.info("Tick loop done")


async def order_loop(app, order_mgr: OrderManager, risk_mgr: RiskManager):
    logger.info("Order loop started")

    while not risk_mgr.done:
        try:
            event = await asyncio.wait_for(app.order_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue

        etype = event.get("type")

        if etype == "exec":
            await order_mgr.on_fill(event["orderId"], event["price"])

        elif etype == "order_status":
            if event["status"] == "PartiallyFilled" and event["remaining"] > 0:
                await order_mgr.on_partial_fill(event["orderId"])

        elif etype == "error":
            if event.get("code") == 201:
                logger.warning("Order rejected (201) — stopping bot immediately per client directive")
                risk_mgr.done = True
                await order_mgr.exit_all("order rejected 201")

        elif etype == "pnl":
            daily_pnl = event["dailyPnL"]
            logger.info("PnL update: %.2f | bot realized: %.2f", daily_pnl, order_mgr._bot_realized)
            reason = risk_mgr.check(daily_pnl)
            if reason:
                logger.warning("Risk exit: %s", reason)
                await order_mgr.exit_all(reason)

    logger.info("Order loop done")


async def short_report_task(sim_sl_short: SimStopLoss, candles: CandleBuilder):
    target = et_time(config.EOD_EXIT_HOUR, config.EOD_EXIT_MIN)
    wait = (target - now_et()).total_seconds()
    if wait > 0:
        await asyncio.sleep(wait)

    sim_sl_short.finalize()
    trading_candles = [c for c in candles.history if c.minute_ts < target.timestamp()]
    report = generate_report(sim_sl_short.records, trading_candles)
    print(report)
    save_report(report, "post_trade_report_trading.txt")
    email_report(sim_sl_short.records, trading_candles,
                 subject="SPY Bot — Post-Trade Report (9:30am–10:00am)")
    logger.info("Trading window report emailed (9:30am-10am)")


async def noon_report_task(sim_sl_noon: SimStopLoss, candles: CandleBuilder,
                           order_mgr: OrderManager, risk_mgr: RiskManager):
    target = et_time(config.SIM_SL_END_HOUR, config.SIM_SL_END_MIN)
    wait = (target - now_et()).total_seconds()
    if wait <= 0:
        return  # already past 12:30pm at startup — skip
    await asyncio.sleep(wait)

    sim_sl_noon.finalize()
    am_candles = list(candles.history)
    report = generate_report(sim_sl_noon.records, am_candles)
    print(report)
    save_report(report, "post_trade_report_am.txt")
    email_report(sim_sl_noon.records, am_candles,
                 subject="SPY Bot — Post-Trade Report (9:30am–12:30pm)")
    logger.info("Noon report emailed (9:30am-12:30pm)")

    if risk_mgr.check_noon(risk_mgr.current_pnl):
        logger.warning("12:30pm exit: pnl=%.2f < 4.5%%", risk_mgr.current_pnl)
        await order_mgr.exit_all("12:30pm noon exit")


async def eod_exit_task(order_mgr: OrderManager, risk_mgr: RiskManager):
    target = et_time(config.EOD_EXIT_HOUR, config.EOD_EXIT_MIN)
    while now_et() < target:
        if risk_mgr.done:
            return
        await asyncio.sleep(5.0)
    if not risk_mgr.done:
        risk_mgr.done = True
        await order_mgr.exit_all("3:59pm eod")
        logger.info("3:59pm exit complete")


async def wait_until(hour: int, minute: int, label: str):
    secs = (et_time(hour, minute) - now_et()).total_seconds()
    if secs > 0:
        logger.info("Waiting %.0fs until %s", secs, label)
        await asyncio.sleep(secs)


async def run():
    # If restarted after market close OR the day already finished (terminal
    # exit), sleep until tomorrow's pre-check so we never re-enter the session.
    now = now_et()
    eod = et_time(config.EOD_EXIT_HOUR, config.EOD_EXIT_MIN)
    if now >= eod or already_done_today():
        reason = "past EOD" if now >= eod else "day already done"
        tomorrow = (now + timedelta(days=1)).replace(
            hour=config.PRE_CHECK_HOUR, minute=config.PRE_CHECK_MIN,
            second=0, microsecond=0)
        wait = (tomorrow - now).total_seconds()
        logger.info("%s — sleeping %.0fs until tomorrow %d:%02d ET",
                    reason, wait, config.PRE_CHECK_HOUR, config.PRE_CHECK_MIN)
        await asyncio.sleep(wait)

    loop = asyncio.get_running_loop()
    app = connect(config.HOST, config.PORT, config.CLIENT_ID, loop)

    logger.info("Connecting to IBKR...")
    await asyncio.wait_for(app.connected.wait(), timeout=30)

    from strategy.orders import set_account
    await asyncio.sleep(1)  # let managedAccounts arrive
    set_account(app.account)
    logger.info("Orders target account: %s", app.account)

    await wait_until(config.PRE_CHECK_HOUR, config.PRE_CHECK_MIN, "8:25am pre-check")

    trading_hours = await app.fetch_trading_hours()
    if is_early_close(trading_hours):
        logger.warning("Early close day — no trading")
        return

    sessions = parse_trading_hours(trading_hours)
    if not sessions:
        logger.warning("No session found — aborting")
        return
    logger.info("Session close: %s ET", sessions[0][1].strftime("%H:%M"))

    app.reqMarketDataType(1 if not config.SIM_ONLY and config.PORT != 7497 else 3)
    mkt_req_id = app.next_id()
    app.reqMktData(mkt_req_id, spy_contract(), "", False, False, [])

    await wait_until(config.OPEN_HOUR, config.OPEN_MIN, "9:30am open")

    await app.clean_slate()
    await asyncio.sleep(2)

    elv, _ = await app.fetch_account_summary()

    if config.SIM_ONLY:
        # clean_slate above flattened SPY; from here, no live orders — sim SL only.
        logger.info("SIM-ONLY mode: live orders disabled, simulated stop loss only")
        leg_qty, sell_margin, margin_pct = 1, 1000.0, 1.6
        spy_price = app.last_price if app.last_price > 10 else 750.0
        sizing_elv = elv
    else:
        if elv <= 0:
            logger.error("ELV=0 — aborting")
            return
        # OCO removed (NADIR6) — Y and Z are independent orders, IBKR nets BUY+SELL
        # pending exposure. Size against current ELV per client spec (ELV-2%/margin).
        spy_price = app.last_price if app.last_price > 10 else 750.0
        sizing_elv = elv
        probe_qty = max(100, int(sizing_elv * 0.98 / (spy_price * 0.5)))
        short_margin = await app.fetch_short_margin_per_share(probe_qty)
        if short_margin >= 50:
            sell_margin = round(short_margin, 2)
        else:
            sell_margin = max(round(spy_price * 1.6, 2), 950.0)
            logger.warning("whatIf margin unavailable (%.2f) — fallback %.2f", short_margin, sell_margin)
        margin_pct = sell_margin / spy_price
        logger.info("Short margin (whatIf)=%.2f (%.0f%%)  ELV=%.2f (sizing against current ELV)",
                    sell_margin, margin_pct * 100, elv)

        leg_qty = calc_leg_qty(sizing_elv, sell_margin)
        if leg_qty < 1:
            logger.error("leg_qty < 1 (ELV=%.2f margin=%.2f) — aborting", sizing_elv, sell_margin)
            return

        logger.info("ELV=%.2f  margin/share=%.2f  qty=%d",
                    sizing_elv, sell_margin, leg_qty)

    if app.account:
        app.reqAccountUpdates(True, app.account)
        app.reqPnL(app.next_id(), app.account, "")

    candles = CandleBuilder()
    sim_sl_short = SimStopLoss()  # 9:30am-10:00am (report sent at EOD_EXIT)
    sim_sl_noon = SimStopLoss()   # 9:30am-12:30pm (report sent at 12:30pm)
    order_mgr = OrderManager(app, leg_qty, sell_margin, margin_pct)
    risk_mgr = RiskManager(elv)
    if config.SIM_ONLY:
        risk_mgr.done = True   # disables order placement; tick loop still runs the sim SL

    session_end = et_time(config.SIM_SL_END_HOUR, config.SIM_SL_END_MIN)

    await asyncio.gather(
        tick_loop(app, candles, sim_sl_short, sim_sl_noon, order_mgr, risk_mgr, session_end),
        order_loop(app, order_mgr, risk_mgr),
        short_report_task(sim_sl_short, candles),
        noon_report_task(sim_sl_noon, candles, order_mgr, risk_mgr),
        eod_exit_task(order_mgr, risk_mgr),
    )

    mark_day_done()
    app.cancelMktData(mkt_req_id)
    candles.finalize()
    app.disconnect()
    logger.info("Session complete")


if __name__ == "__main__":
    asyncio.run(run())
