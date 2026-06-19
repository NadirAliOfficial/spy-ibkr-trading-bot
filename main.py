import asyncio
import logging
import sys

import config
from connection import connect, IBApp, spy_contract
from candle import CandleBuilder
from sim_stop_loss import SimStopLoss
from order_manager import OrderManager
from risk_manager import RiskManager
from post_trade import generate_report, save_report
from utils import now_et, et_time, is_early_close, parse_trading_hours, calc_leg_qty

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("spy_bot.log")],
)
logger = logging.getLogger("main")


async def tick_loop(app: IBApp, candle_builder: CandleBuilder, sim_sl: SimStopLoss,
                    order_mgr: OrderManager, risk_mgr: RiskManager,
                    sim_end, session_end):
    logger.info("Tick loop started")
    sim_active = True
    fired_59s = False

    while not risk_mgr.done:
        try:
            event = await asyncio.wait_for(app.tick_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            if now_et() >= session_end:
                break
            continue

        if event["type"] != "tick_price":
            continue

        tick_type = event["tickType"]
        price = event["price"]
        ts = event["ts"]

        if tick_type == config.TICK_BID:
            order_mgr.last_bid = price
            continue
        if tick_type == config.TICK_ASK:
            order_mgr.last_ask = price
            continue
        if tick_type != config.TICK_LAST:
            continue

        candle, is_new = candle_builder.process_tick(price, ts)

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

        secs = candle_builder.seconds_into_candle(ts)
        if secs >= 59.0 and not fired_59s:
            fired_59s = True
            asyncio.create_task(order_mgr.on_59th_second())

        asyncio.create_task(order_mgr.on_tick(price, sim_hits))

    logger.info("Tick loop done")


async def order_loop(app: IBApp, order_mgr: OrderManager, risk_mgr: RiskManager):
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
            pnl = event["dailyPnL"]
            reason = risk_mgr.check(pnl)
            if reason:
                logger.warning("Risk exit: %s", reason)
                await order_mgr.exit_all(reason)

    logger.info("Order loop done")


async def noon_exit_task(order_mgr: OrderManager, risk_mgr: RiskManager):
    target = et_time(config.SIM_SL_END_HOUR, config.SIM_SL_END_MIN)
    wait = (target - now_et()).total_seconds()
    if wait > 0:
        await asyncio.sleep(wait)
    if risk_mgr.check_noon(risk_mgr.current_pnl):
        logger.warning("12:30pm exit: pnl=%.2f < 4.5%%", risk_mgr.current_pnl)
        await order_mgr.exit_all("12:30pm noon exit")


async def eod_exit_task(order_mgr: OrderManager, risk_mgr: RiskManager):
    target = et_time(config.EOD_EXIT_HOUR, config.EOD_EXIT_MIN)
    wait = (target - now_et()).total_seconds()
    if wait > 0:
        await asyncio.sleep(wait)
    if not risk_mgr.done:
        risk_mgr.done = True
        await order_mgr.exit_all("3:59pm eod")
        logger.info("3:59pm exit complete")


async def wait_until(hour: int, minute: int, label: str):
    target = et_time(hour, minute)
    secs = (target - now_et()).total_seconds()
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

    spy_req_id = app.next_id()
    app.reqMktData(spy_req_id, spy_contract(), "", False, False, [])

    await wait_until(config.OPEN_HOUR, config.OPEN_MIN, "9:30am open")

    elv, sell_margin = await app.fetch_account_summary()
    if elv <= 0:
        logger.error("ELV=0 — aborting")
        return

    if sell_margin <= 0 or sell_margin > elv:
        spy_price = app.sell_init_margin if app.sell_init_margin > 0 else 550.0
        sell_margin = round(spy_price * 1.5, 2)
        logger.info("Fallback sell margin: %.2f", sell_margin)

    leg_qty = calc_leg_qty(elv, sell_margin, config.EQUITY_PCT)
    if leg_qty < 1:
        logger.error("leg_qty < 1 (ELV=%.2f margin=%.2f) — aborting", elv, sell_margin)
        return

    logger.info("ELV=%.2f  margin/share=%.2f  leg_qty=%d  total=%d",
                elv, sell_margin, leg_qty, leg_qty * 2)

    if app.account:
        pnl_req_id = app.next_id()
        app.reqPnL(pnl_req_id, app.account, "")

    candle_builder = CandleBuilder()
    sim_sl = SimStopLoss()
    order_mgr = OrderManager(app, leg_qty)
    risk_mgr = RiskManager(elv)

    sim_end = et_time(config.SIM_SL_END_HOUR, config.SIM_SL_END_MIN)
    session_end = et_time(config.EOD_EXIT_HOUR, config.EOD_EXIT_MIN)

    await asyncio.gather(
        tick_loop(app, candle_builder, sim_sl, order_mgr, risk_mgr, sim_end, session_end),
        order_loop(app, order_mgr, risk_mgr),
        noon_exit_task(order_mgr, risk_mgr),
        eod_exit_task(order_mgr, risk_mgr),
    )

    sim_sl.finalize()
    candle_builder.finalize()
    report = generate_report(sim_sl.records, candle_builder.history)
    print(report)
    save_report(report)

    app.disconnect()
    logger.info("Session complete")


if __name__ == "__main__":
    asyncio.run(run())
