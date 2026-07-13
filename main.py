import os as _os
_log_fd = _os.open(
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "spy_bot.log"),
    _os.O_WRONLY | _os.O_CREAT | _os.O_APPEND, 0o644,
)
_os.dup2(_log_fd, 1)
_os.dup2(_log_fd, 2)
_os.close(_log_fd)
del _log_fd, _os

import asyncio
import json
import logging
import signal
import sys
import time
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
STATE_FILE  = Path(__file__).parent / "day_state.txt"
STOP_FLAG   = Path(__file__).parent / "bot_stop.txt"
STATUS_FILE = Path(__file__).parent / "bot_status.json"


def _write_status(status: str, account: str = "", elv: float = 0.0,
                  leg_qty: int = 0, candle_open: float = 0.0, entries: int = 0,
                  position: str = "FLAT", entry_price: float = 0.0,
                  pos_qty: int = 0, daily_pnl: float = 0.0, bot_pnl: float = 0.0):
    try:
        STATUS_FILE.write_text(json.dumps({
            "ts": time.time(), "status": status, "account": account,
            "elv": elv, "leg_qty": leg_qty, "candle_open": candle_open,
            "entries": entries, "position": position, "entry_price": entry_price,
            "pos_qty": pos_qty, "daily_pnl": daily_pnl, "bot_pnl": bot_pnl,
        }))
    except Exception:
        pass


def _today_str() -> str:
    return now_et().strftime("%Y-%m-%d")


def mark_day_done():
    STATE_FILE.write_text(_today_str())
    logger.info("Day marked done: %s", _today_str())


def already_done_today() -> bool:
    return STATE_FILE.exists() and STATE_FILE.read_text(encoding="utf-8").strip() == _today_str()


async def tick_loop(app, candles: CandleBuilder, sim_sl_one: SimStopLoss, sim_sl_two: SimStopLoss,
                    order_mgr: OrderManager, risk_mgr: RiskManager, session_end):
    logger.info("Tick loop started")
    fired_59s = False
    last_price = 0.0
    session_start_ts = et_time(config.OPEN_HOUR, config.OPEN_MIN).timestamp()
    noon_end = et_time(config.SIM_SL_END_HOUR, config.SIM_SL_END_MIN)  # 12:30pm boundary

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

        # sim_sl_one: 9:30am–12:30pm | sim_sl_two: 12:30pm–3:59pm
        before_noon = now_et() < noon_end
        if is_new:
            if before_noon:
                sim_sl_one.new_candle(candle.open, candle.minute_ts, prev_close)
            else:
                sim_sl_two.new_candle(candle.open, candle.minute_ts, prev_close)
            fired_59s = False

        if before_noon:
            sim_hits = sim_sl_one.on_tick(price)
        else:
            sim_hits = sim_sl_two.on_tick(price)

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
            await order_mgr.on_fill(event["orderId"], event["price"], int(event.get("shares", 0)))

        elif etype == "order_status":
            if event["status"] == "PartiallyFilled" and event["remaining"] > 0:
                await order_mgr.on_partial_fill(event["orderId"])

        elif etype == "error":
            if event.get("code") == 201:
                if risk_mgr.done:
                    continue  # already stopping — clean-slate orders may 201 too
                oid = event.get("reqId", -1)
                kind = "close" if order_mgr.is_close_order(oid) else "entry"
                logger.warning("%s order %d rejected (201) — stopping bot, flattening real broker position", kind, oid)
                risk_mgr.done = True
                mark_day_done()
                await order_mgr.cancel_all_orders()
                await asyncio.sleep(1.5)  # let cancels settle before querying positions
                await app.clean_slate()   # flatten what the broker actually holds

        elif etype == "pnl":
            daily_pnl = event["dailyPnL"]
            logger.info("PnL update: %.2f | bot realized: %.2f", daily_pnl, order_mgr._bot_realized)
            reason = risk_mgr.check(daily_pnl)
            if reason:
                logger.warning("Risk exit: %s", reason)
                await order_mgr.exit_all(reason)

    # Drain for 3s after done — catches fills from exit MKT orders so botPnL is logged
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 3.0
    while loop.time() < deadline:
        try:
            event = await asyncio.wait_for(app.order_queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            break
        if event.get("type") == "exec":
            await order_mgr.on_fill(event["orderId"], event["price"], int(event.get("shares", 0)))

    logger.info("Order loop done")


async def am_report_task(sim_sl_one: SimStopLoss, candles: CandleBuilder, order_mgr: OrderManager):
    target = et_time(config.SIM_SL_END_HOUR, config.SIM_SL_END_MIN)  # 12:30pm
    wait = (target - now_et()).total_seconds()
    if wait <= 0:
        return
    await asyncio.sleep(wait)

    sim_sl_one.finalize()
    am_candles = [c for c in candles.history if c.minute_ts < target.timestamp()]
    report = generate_report(sim_sl_one.records, am_candles,
                             total_bought=order_mgr.total_bought, total_sold=order_mgr.total_sold,
                             mean_slippage=order_mgr.mean_slippage,
                             total_executed_orders=order_mgr.total_executed_orders,
                             total_slippage=order_mgr.total_slippage)
    print(report)
    save_report(report, "post_trade_report_am.txt")
    email_report(sim_sl_one.records, am_candles,
                 subject="SPY Bot — Post-Trade Report (9:30am–12:30pm)",
                 total_bought=order_mgr.total_bought, total_sold=order_mgr.total_sold,
                             mean_slippage=order_mgr.mean_slippage,
                             total_executed_orders=order_mgr.total_executed_orders,
                             total_slippage=order_mgr.total_slippage)
    logger.info("AM report emailed (9:30am-12:30pm)")


async def pm_report_task(sim_sl_two: SimStopLoss, candles: CandleBuilder, order_mgr: OrderManager):
    target = et_time(config.EOD_EXIT_HOUR, config.EOD_EXIT_MIN)  # 3:59pm
    wait = (target - now_et()).total_seconds()
    if wait <= 0:
        return
    await asyncio.sleep(wait)

    sim_sl_two.finalize()
    noon_ts = et_time(config.SIM_SL_END_HOUR, config.SIM_SL_END_MIN).timestamp()
    pm_candles = [c for c in candles.history if c.minute_ts >= noon_ts]
    report = generate_report(sim_sl_two.records, pm_candles,
                             total_bought=order_mgr.total_bought, total_sold=order_mgr.total_sold,
                             mean_slippage=order_mgr.mean_slippage,
                             total_executed_orders=order_mgr.total_executed_orders,
                             total_slippage=order_mgr.total_slippage)
    print(report)
    save_report(report, "post_trade_report_pm.txt")
    email_report(sim_sl_two.records, pm_candles,
                 subject="SPY Bot — Post-Trade Report (12:30pm–4:00pm)",
                 total_bought=order_mgr.total_bought, total_sold=order_mgr.total_sold,
                             mean_slippage=order_mgr.mean_slippage,
                             total_executed_orders=order_mgr.total_executed_orders,
                             total_slippage=order_mgr.total_slippage)
    logger.info("PM report emailed (12:30pm-4pm)")


async def trading_window_report_task(sim_sl: SimStopLoss, candles: CandleBuilder, order_mgr: OrderManager):
    target = et_time(10, 0)  # always fires at 10am ET
    wait = (target - now_et()).total_seconds()
    if wait <= 0:
        return
    await asyncio.sleep(wait)
    sim_sl.finalize()
    open_ts = et_time(config.OPEN_HOUR, config.OPEN_MIN).timestamp()
    close_ts = target.timestamp()
    window_candles = [c for c in candles.history if open_ts <= c.minute_ts < close_ts]
    report = generate_report(sim_sl.records, window_candles,
                             total_bought=order_mgr.total_bought, total_sold=order_mgr.total_sold,
                             mean_slippage=order_mgr.mean_slippage,
                             total_executed_orders=order_mgr.total_executed_orders,
                             total_slippage=order_mgr.total_slippage)
    print(report)
    save_report(report, "post_trade_report_trading.txt")
    email_report(sim_sl.records, window_candles,
                 subject="SPY Bot — Trading Window Report (9:30am–10:00am)",
                 total_bought=order_mgr.total_bought, total_sold=order_mgr.total_sold,
                             mean_slippage=order_mgr.mean_slippage,
                             total_executed_orders=order_mgr.total_executed_orders,
                             total_slippage=order_mgr.total_slippage)
    logger.info("Trading window report emailed (9:30am-%d:%02d)", config.EOD_EXIT_HOUR, config.EOD_EXIT_MIN)


async def ten_thirty_report_task(sim_sl: SimStopLoss, candles: CandleBuilder, order_mgr: OrderManager):
    target = et_time(10, 30)  # fires at 10:30am ET
    wait = (target - now_et()).total_seconds()
    if wait <= 0:
        return
    await asyncio.sleep(wait)
    sim_sl.finalize()
    open_ts = et_time(config.OPEN_HOUR, config.OPEN_MIN).timestamp()
    close_ts = target.timestamp()
    window_candles = [c for c in candles.history if open_ts <= c.minute_ts < close_ts]
    report = generate_report(sim_sl.records, window_candles,
                             total_bought=order_mgr.total_bought, total_sold=order_mgr.total_sold,
                             mean_slippage=order_mgr.mean_slippage,
                             total_executed_orders=order_mgr.total_executed_orders,
                             total_slippage=order_mgr.total_slippage)
    print(report)
    save_report(report, "post_trade_report_1030.txt")
    email_report(sim_sl.records, window_candles,
                 subject="SPY Bot — Post-Trade Report (9:30am–10:30am)",
                 total_bought=order_mgr.total_bought, total_sold=order_mgr.total_sold,
                             mean_slippage=order_mgr.mean_slippage,
                             total_executed_orders=order_mgr.total_executed_orders,
                             total_slippage=order_mgr.total_slippage)
    logger.info("10:30am report emailed (9:30am-10:30am)")



async def ten_thirty_pnl_task(order_mgr: OrderManager, risk_mgr: RiskManager):
    target = et_time(10, 30)
    wait = (target - now_et()).total_seconds()
    if wait > 0:
        await asyncio.sleep(wait)
    if risk_mgr.done:
        return  # already exited earlier
    if risk_mgr.check_noon(risk_mgr.current_pnl):
        logger.warning("10:30am check: pnl=%.2f < 4.5%% — day done, no re-entry", risk_mgr.current_pnl)
        await order_mgr.exit_all("10:30am pnl exit")
        mark_day_done()


async def eod_exit_task(order_mgr: OrderManager, risk_mgr: RiskManager):
    target = et_time(config.EOD_EXIT_HOUR, config.EOD_EXIT_MIN)
    while now_et() < target:
        if risk_mgr.done:
            return
        if STOP_FLAG.exists():
            STOP_FLAG.unlink()
            logger.info("Dashboard stop requested — closing all positions")
            risk_mgr.done = True
            await order_mgr.exit_all("dashboard stop")
            mark_day_done()
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


async def status_writer(app, candles: CandleBuilder, order_mgr: OrderManager,
                        risk_mgr: RiskManager):
    while True:
        ep = 0.0
        if order_mgr._pos.name == "LONG" and order_mgr._y:
            ep = order_mgr._y.entry_price
        elif order_mgr._pos.name == "SHORT" and order_mgr._z:
            ep = order_mgr._z.entry_price
        st = "ended" if risk_mgr.done else "running"
        _write_status(
            status=st,
            account=app.account,
            elv=app.equity_with_loan,
            leg_qty=order_mgr._leg,
            candle_open=candles.history[-1].open if candles.history else 0.0,
            entries=order_mgr._entries,
            position=order_mgr._pos.name,
            entry_price=ep,
            pos_qty=order_mgr._pos_qty,
            daily_pnl=app.equity_with_loan - app.prev_day_elv if app.prev_day_elv > 0 else 0.0,
            bot_pnl=order_mgr._bot_realized,
        )
        if risk_mgr.done:
            break
        await asyncio.sleep(5)


async def run():
    # Attach FileHandler now — all module-level fd activity is complete,
    # so the handler gets a stable descriptor that nothing will close.
    _log_path = Path(__file__).parent / "spy_bot.log"
    _fh = logging.FileHandler(_log_path, mode="a", encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.getLogger().addHandler(_fh)
    _write_status("waiting")

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
    _write_status("waiting", account=app.account)

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

    tbt_req_id = None
    if not config.SIM_ONLY and config.PORT != 7497:
        tbt_req_id = app.next_id()
        app.reqTickByTickData(tbt_req_id, spy_contract(), "AllLast", 0, False)
        app._tbt_active = True
        logger.info("Tick-by-tick Last subscribed (reqId=%d)", tbt_req_id)

    await wait_until(config.OPEN_HOUR, config.OPEN_MIN, "9:30am open")

    if STOP_FLAG.exists():
        STOP_FLAG.unlink()

    # Pre-open position check
    app._positions = []
    app._positions_future = loop.create_future()
    app.reqPositions()
    try:
        pre_positions = await asyncio.wait_for(app._positions_future, timeout=10)
    except asyncio.TimeoutError:
        pre_positions = []
    app.cancelPositions()
    if pre_positions:
        logger.warning("Pre-open: %d open SPY position(s) found — closing now", len(pre_positions))
    else:
        logger.info("Pre-open position check: account is FLAT — no open positions")

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
        # OCO removed (NADIR6) — Y and Z are independent orders. IBKR does NOT
        # net the pending BUY+SELL exposure in practice (2026-07-13 rejection);
        # see MARGIN_SAFETY_MULT. Size against current ELV per client spec
        # (ELV-2%/margin).
        spy_price = app.last_price if app.last_price > 10 else 750.0
        prev_day_elv = app.prev_day_elv
        sizing_elv = min(prev_day_elv, elv) if prev_day_elv > 0 else elv
        probe_qty = max(100, int(sizing_elv * 0.98 / (spy_price * 0.5)))
        short_margin = await app.fetch_short_margin_per_share(probe_qty)
        if short_margin >= 50:
            sell_margin = round(short_margin, 2)
        else:
            sell_margin = max(round(spy_price * 1.6, 2), 950.0)
            logger.warning("whatIf margin unavailable (%.2f) — fallback %.2f", short_margin, sell_margin)
        # single-sided probe -> real Y+Z concurrent margin (see config.MARGIN_SAFETY_MULT)
        sell_margin = round(sell_margin * config.MARGIN_SAFETY_MULT, 2)
        margin_pct = sell_margin / spy_price
        logger.info("Short margin (whatIf, safety-adjusted x%.1f)=%.2f (%.0f%%)  ELV=%.2f  prev_day_ELV=%.2f  sizing_ELV=%.2f",
                    config.MARGIN_SAFETY_MULT, sell_margin, margin_pct * 100, elv, prev_day_elv, sizing_elv)

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
    sim_sl_one = SimStopLoss()  # 9:30am–12:30pm → report at 12:30pm
    sim_sl_two = SimStopLoss()  # 12:30pm–3:59pm  → report at 3:59pm
    risk_mgr = RiskManager(elv)
    order_mgr = OrderManager(app, leg_qty, sell_margin, margin_pct, is_session_done=lambda: risk_mgr.done)
    if config.SIM_ONLY:
        risk_mgr.done = True   # disables order placement; tick loop still runs the sim SL

    session_end = et_time(config.EOD_EXIT_HOUR, config.EOD_EXIT_MIN)  # 3:59pm

    async def _graceful_shutdown(signum):
        if risk_mgr.done:
            return
        logger.info("Signal %d: closing all positions before exit", signum)
        risk_mgr.done = True
        await order_mgr.exit_all(f"signal-{signum}")
        await asyncio.sleep(1)
        mark_day_done()
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: asyncio.ensure_future(_graceful_shutdown(s)))

    await asyncio.gather(
        tick_loop(app, candles, sim_sl_one, sim_sl_two, order_mgr, risk_mgr, session_end),
        order_loop(app, order_mgr, risk_mgr),
        trading_window_report_task(sim_sl_one, candles, order_mgr),
        ten_thirty_report_task(sim_sl_one, candles, order_mgr),
        am_report_task(sim_sl_one, candles, order_mgr),
        pm_report_task(sim_sl_two, candles, order_mgr),
        ten_thirty_pnl_task(order_mgr, risk_mgr),
        eod_exit_task(order_mgr, risk_mgr),
        status_writer(app, candles, order_mgr, risk_mgr),
        return_exceptions=True,
    )

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.remove_signal_handler(sig)

    mark_day_done()
    app.cancelMktData(mkt_req_id)
    if tbt_req_id:
        app.cancelTickByTickData(tbt_req_id)
    candles.finalize()
    await asyncio.sleep(3)
    await app.clean_slate()  # close any positions left by late STP3 fills
    app.disconnect()
    logger.info("Session complete")


if __name__ == "__main__":
    asyncio.run(run())
