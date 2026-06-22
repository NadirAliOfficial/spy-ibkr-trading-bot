import asyncio
import logging
import sys

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


async def tick_loop(app, candles: CandleBuilder, sim_sl: SimStopLoss,
                    order_mgr: OrderManager, risk_mgr: RiskManager,
                    sim_end, session_end):
    logger.info("Tick loop started")
    sim_active = True
    fired_59s = False
    session_start_ts = et_time(config.OPEN_HOUR, config.OPEN_MIN).timestamp()

    while not risk_mgr.done:
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

        candle, is_new = candles.process_tick(price, ts)

        if sim_active:
            if now_et() >= sim_end:
                sim_active = False
                sim_sl.finalize()
                logger.info("Sim SL window closed")
            else:
                if is_new:
                    sim_sl.new_candle(candle.open, candle.minute_ts)
                    fired_59s = False

        sim_hits = sim_sl.on_tick(price) if sim_active else 0

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
            elif event["status"] in ("Cancelled", "Inactive") and event["orderId"] == order_mgr._rev_id:
                order_mgr.on_reverse_rejected(event["orderId"])

        elif etype == "pnl":
            daily_pnl = event["dailyPnL"]
            logger.info("PnL update: %.2f", daily_pnl)
            reason = risk_mgr.check(daily_pnl)
            if reason:
                logger.warning("Risk exit: %s", reason)
                await order_mgr.exit_all(reason)

    logger.info("Order loop done")


async def noon_exit_task(order_mgr: OrderManager, risk_mgr: RiskManager):
    target = et_time(config.SIM_SL_END_HOUR, config.SIM_SL_END_MIN)
    wait = (target - now_et()).total_seconds()
    if wait <= 0:
        return  # already past noon window at startup — skip
    await asyncio.sleep(wait)
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
    loop = asyncio.get_running_loop()
    app = connect(config.HOST, config.PORT, config.CLIENT_ID, loop)

    logger.info("Connecting to IBKR...")
    await asyncio.wait_for(app.connected.wait(), timeout=30)

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

    app.reqMarketDataType(3)  # delayed for paper; remove for live with subscription
    mkt_req_id = app.next_id()
    app.reqMktData(mkt_req_id, spy_contract(), "", False, False, [])

    await wait_until(config.OPEN_HOUR, config.OPEN_MIN, "9:30am open")

    elv, _ = await app.fetch_account_summary()
    if elv <= 0:
        logger.error("ELV=0 — aborting")
        return

    sell_margin = await app.fetch_spy_margin()
    if sell_margin <= 0:
        # Paper accounts charge margin for both Y and Z simultaneously regardless
        # of OCA groups. Effective per-share margin is ~$1,038-1,066 for SPY brackets.
        sell_margin = 1150.0
        logger.info("Fallback sell margin: %.2f", sell_margin)
    else:
        sell_margin = round(sell_margin * 2.3, 2)  # scale for combined Y+Z bracket exposure
        logger.info("SPY margin/share (adjusted for Y+Z): %.2f", sell_margin)

    leg_qty = calc_leg_qty(elv, sell_margin)
    if leg_qty < 1:
        logger.error("leg_qty < 1 (ELV=%.2f margin=%.2f) — aborting", elv, sell_margin)
        return

    logger.info("ELV=%.2f  margin/share=%.2f  leg=%d  total=%d",
                elv, sell_margin, leg_qty, leg_qty * 2)

    if app.account:
        app.reqAccountUpdates(True, app.account)
        app.reqPnL(app.next_id(), app.account, "")

    candles = CandleBuilder()
    sim_sl = SimStopLoss()
    order_mgr = OrderManager(app, leg_qty, sell_margin)
    risk_mgr = RiskManager(elv)

    sim_end = et_time(config.SIM_SL_END_HOUR, config.SIM_SL_END_MIN)
    session_end = et_time(config.EOD_EXIT_HOUR, config.EOD_EXIT_MIN)

    await asyncio.gather(
        tick_loop(app, candles, sim_sl, order_mgr, risk_mgr, sim_end, session_end),
        order_loop(app, order_mgr, risk_mgr),
        noon_exit_task(order_mgr, risk_mgr),
        eod_exit_task(order_mgr, risk_mgr),
    )

    app.cancelMktData(mkt_req_id)
    sim_sl.finalize()
    candles.finalize()
    report = generate_report(sim_sl.records, candles.history)
    print(report)
    save_report(report)
    email_report(sim_sl.records, candles.history)

    app.disconnect()
    logger.info("Session complete")


if __name__ == "__main__":
    asyncio.run(run())
