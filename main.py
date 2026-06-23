import asyncio
import logging
import sys
from datetime import timedelta

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


async def tick_loop(app, candles: CandleBuilder, sim_sl_1: SimStopLoss, sim_sl_2: SimStopLoss,
                    order_mgr: OrderManager, risk_mgr: RiskManager,
                    sim_end, session_end):
    logger.info("Tick loop started")
    phase = 1  # 1 = 9:30am-12:30pm, 2 = 12:30pm-4pm
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

        if now_et() >= session_end:
            break

        candle, is_new = candles.process_tick(price, ts)

        if phase == 1:
            if now_et() >= sim_end:
                phase = 2
                sim_sl_1.finalize()
                logger.info("Sim SL phase 1 closed (9:30am-12:30pm)")
            else:
                if is_new:
                    sim_sl_1.new_candle(candle.open, candle.minute_ts)
                    fired_59s = False
            sim_hits = sim_sl_1.on_tick(price)
        else:
            if is_new:
                sim_sl_2.new_candle(candle.open, candle.minute_ts)
                fired_59s = False
            sim_hits = sim_sl_2.on_tick(price)

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

        elif etype == "pnl":
            daily_pnl = event["dailyPnL"]
            logger.info("PnL update: %.2f", daily_pnl)
            reason = risk_mgr.check(daily_pnl)
            if reason:
                logger.warning("Risk exit: %s", reason)
                await order_mgr.exit_all(reason)

    logger.info("Order loop done")


async def noon_report_task(sim_sl_1: SimStopLoss, candles: CandleBuilder,
                           order_mgr: OrderManager, risk_mgr: RiskManager):
    target = et_time(config.SIM_SL_END_HOUR, config.SIM_SL_END_MIN)
    wait = (target - now_et()).total_seconds()
    if wait <= 0:
        return  # already past 12:30pm at startup — skip
    await asyncio.sleep(wait)

    sim_sl_1.finalize()
    report = generate_report(sim_sl_1.records, candles.history)
    print(report)
    save_report(report, "post_trade_report_am.txt")
    email_report(sim_sl_1.records, candles.history,
                 subject="SPY Bot — Post-Trade Report (9:30am–12:30pm)")
    logger.info("Phase 1 report emailed (9:30am-12:30pm)")

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
    # If restarted after market close, sleep until tomorrow's pre-check
    now = now_et()
    eod = et_time(config.EOD_EXIT_HOUR, config.EOD_EXIT_MIN)
    if now >= eod:
        tomorrow = (now + timedelta(days=1)).replace(
            hour=config.PRE_CHECK_HOUR, minute=config.PRE_CHECK_MIN,
            second=0, microsecond=0)
        wait = (tomorrow - now).total_seconds()
        logger.info("Past EOD — sleeping %.0fs until tomorrow %d:%02d ET",
                    wait, config.PRE_CHECK_HOUR, config.PRE_CHECK_MIN)
        await asyncio.sleep(wait)

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

    await app.clean_slate()
    await asyncio.sleep(2)

    elv, _ = await app.fetch_account_summary()
    if elv <= 0:
        logger.error("ELV=0 — aborting")
        return

    # Paper charges Y and Z margin simultaneously regardless of OCA.
    # Observed: ~143% of SPY price per bracket slot. Use 150% for buffer.
    spy_price = app.last_price if app.last_price > 10 else 750.0
    sell_margin = max(round(spy_price * 1.6, 2), 950.0)
    logger.info("SPY last=%.2f  margin/share (Y+Z effective)=%.2f", spy_price, sell_margin)

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
    sim_sl_1 = SimStopLoss()  # 9:30am-12:30pm
    sim_sl_2 = SimStopLoss()  # 12:30pm-4pm
    order_mgr = OrderManager(app, leg_qty, sell_margin)
    risk_mgr = RiskManager(elv)

    sim_end = et_time(config.SIM_SL_END_HOUR, config.SIM_SL_END_MIN)
    session_end = et_time(config.EOD_EXIT_HOUR, config.EOD_EXIT_MIN)

    await asyncio.gather(
        tick_loop(app, candles, sim_sl_1, sim_sl_2, order_mgr, risk_mgr, sim_end, session_end),
        order_loop(app, order_mgr, risk_mgr),
        noon_report_task(sim_sl_1, candles, order_mgr, risk_mgr),
        eod_exit_task(order_mgr, risk_mgr),
    )

    app.cancelMktData(mkt_req_id)
    sim_sl_2.finalize()
    candles.finalize()
    if sim_sl_2.records:
        report = generate_report(sim_sl_2.records, candles.history)
        print(report)
        save_report(report, "post_trade_report_pm.txt")
        email_report(sim_sl_2.records, candles.history,
                     subject="SPY Bot — Post-Trade Report (12:30pm–4pm)")
        logger.info("Phase 2 report emailed (12:30pm-4pm)")
    else:
        logger.info("No phase 2 data — skipping PM report")

    app.disconnect()
    logger.info("Session complete")


if __name__ == "__main__":
    asyncio.run(run())
