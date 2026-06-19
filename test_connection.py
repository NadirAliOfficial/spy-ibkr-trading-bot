"""
Paper account connection test.
Verifies: connect, account summary, trading hours, SPY tick stream.
Run: python test_connection.py
"""
import asyncio
import logging
import sys

import config
from gateway import connect, spy_contract
from utils import is_early_close, parse_trading_hours, calc_leg_qty

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    stream=sys.stdout)
logger = logging.getLogger("test")

TICK_NAMES = {
    1: "BID", 2: "ASK", 4: "LAST", 6: "HIGH", 7: "LOW", 9: "CLOSE",
    66: "DBID", 67: "DASK", 68: "DLAST", 72: "DHIGH", 73: "DLOW", 75: "DCLOSE", 76: "DOPEN",
}


async def main():
    loop = asyncio.get_running_loop()
    app = connect(config.HOST, config.PORT, config.CLIENT_ID, loop)

    # 1. Connection
    logger.info("Connecting to %s:%s (client_id=%s)...", config.HOST, config.PORT, config.CLIENT_ID)
    try:
        await asyncio.wait_for(app.connected.wait(), timeout=10)
    except asyncio.TimeoutError:
        logger.error("Connection timed out — is IB Gateway running on port %s?", config.PORT)
        return
    logger.info("Connected. Account: %s", app.account or "(pending)")

    # 2. Trading hours
    logger.info("Fetching SPY trading hours...")
    hours = await app.fetch_trading_hours()
    sessions = parse_trading_hours(hours)
    early = is_early_close(hours)
    if sessions:
        o, c = sessions[0]
        logger.info("Today's session: %s – %s ET  (early_close=%s)",
                    o.strftime("%H:%M"), c.strftime("%H:%M"), early)
    else:
        logger.warning("No session found today")

    # 3. Account summary
    logger.info("Fetching account summary...")
    elv, margin = await app.fetch_account_summary()
    logger.info("ELV=%.2f  SellInitMarginReq=%.2f", elv, margin)

    spy_price_est = 550.0
    sell_margin = margin if 0 < margin < elv else round(spy_price_est * 1.5, 2)
    leg_qty = calc_leg_qty(elv, sell_margin, config.EQUITY_PCT)
    logger.info("Position sizing: leg_qty=%d  total_qty=%d  sell_margin/share=%.2f",
                leg_qty, leg_qty * 2, sell_margin)

    # 4. SPY market data — collect 15 seconds of ticks
    app.reqMarketDataType(3)  # 3 = delayed (paper accounts don't have live subscription)
    req_id = app.next_id()
    app.reqMktData(req_id, spy_contract(), "", False, False, [])
    logger.info("Subscribed to SPY market data (req_id=%d) — collecting 15s of ticks...", req_id)

    tick_count = 0
    last_prices: dict[int, float] = {}
    deadline = asyncio.get_event_loop().time() + 15

    while asyncio.get_event_loop().time() < deadline:
        try:
            event = await asyncio.wait_for(app.tick_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        if event["type"] != "tick_price":
            continue
        tt, px = event["tickType"], event["price"]
        last_prices[tt] = px
        tick_count += 1
        name = TICK_NAMES.get(tt, str(tt))
        logger.info("  tick %-5s = %.4f", name, px)

    app.cancelMktData(req_id)
    logger.info("Received %d ticks in 15s", tick_count)

    if last_prices:
        for tt, name in TICK_NAMES.items():
            if tt in last_prices:
                logger.info("  Latest %-5s = %.4f", name, last_prices[tt])

    app.disconnect()
    logger.info("Test complete")


if __name__ == "__main__":
    asyncio.run(main())
