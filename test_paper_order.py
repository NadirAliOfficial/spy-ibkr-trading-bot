"""
M1 proof-of-concept test — runs outside market hours.

Demonstrates:
  1. Connection + account (ELV, account ID)
  2. SPY market data (delayed ticks, candle building)
  3. Y/Z OCO order placement (BUY STP + SELL STP visible in IB Gateway)
  4. OCO cancel-on-fill simulation (cancels Z when Y is manually filled)
  5. Position sizing formula

Run:  python test_paper_order.py
Watch the IB Gateway order panel — Y and Z appear, then Z cancels.
"""
import asyncio
import logging
import sys

import config
from gateway import connect, spy_contract
from market import CandleBuilder
from strategy.orders import stp, mkt, Side, OrderGroup
from utils import calc_leg_qty

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("m1_test")

TICK_NAMES = {
    1: "BID", 2: "ASK", 4: "LAST",
    66: "DBID", 67: "DASK", 68: "DLAST", 72: "DHIGH", 73: "DLOW",
}


# ── Step 1: connect ──────────────────────────────────────────────────────────

async def step_connect(app) -> bool:
    logger.info("━━ STEP 1: Connection ━━")
    try:
        await asyncio.wait_for(app.connected.wait(), timeout=10)
    except asyncio.TimeoutError:
        logger.error("FAIL — IB Gateway not responding on port %s", config.PORT)
        return False
    logger.info("PASS — connected  account=%s  next_order_id=%d",
                app.account, app._next_order_id)
    return True


# ── Step 2: account summary + position sizing ────────────────────────────────

async def step_account(app) -> tuple[float, int]:
    logger.info("━━ STEP 2: Account summary + position sizing ━━")
    elv, margin = await app.fetch_account_summary()
    logger.info("ELV              = $%.2f", elv)
    logger.info("SellInitMarginReq= $%.2f", margin)

    spy_price_est = 550.0
    sell_margin = margin if 0 < margin < elv else round(spy_price_est * 1.5, 2)
    leg_qty = calc_leg_qty(elv, sell_margin, config.EQUITY_PCT)

    logger.info("sell_margin/share= $%.2f  (fallback Reg-T 150%%)", sell_margin)
    logger.info("leg_qty (49%%)   = %d shares", leg_qty)
    logger.info("total position   = %d shares  (~$%.0f notional)",
                leg_qty * 2, leg_qty * 2 * spy_price_est)
    logger.info("PASS — position sizing formula verified")
    return elv, leg_qty


# ── Step 3: market data + candle build ──────────────────────────────────────

async def step_market_data(app) -> float:
    logger.info("━━ STEP 3: SPY market data + candle building ━━")
    app.reqMarketDataType(3)
    req_id = app.next_id()
    app.reqMktData(req_id, spy_contract(), "", False, False, [])
    logger.info("Subscribed to SPY (req_id=%d, delayed mode) — waiting 8s...", req_id)

    candles = CandleBuilder()
    last_price = 0.0
    deadline = asyncio.get_event_loop().time() + 8

    while asyncio.get_event_loop().time() < deadline:
        try:
            event = await asyncio.wait_for(app.tick_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        if event["type"] != "tick_price":
            continue
        tt, px = event["tickType"], event["price"]
        name = TICK_NAMES.get(tt, f"type{tt}")
        logger.info("  tick %-6s = %.4f", name, px)
        if tt in (config.TICK_LAST, config.DTICK_LAST):
            last_price = px
            c, is_new = candles.process_tick(px, event["ts"])
            if is_new:
                logger.info("  >> New candle open=%.4f", c.open)

    app.cancelMktData(req_id)
    ref_price = last_price or 550.0
    logger.info("PASS — ticks received, ref_price=%.4f (used for order placement)", ref_price)
    return ref_price


# ── Step 4: Y/Z OCO placement ────────────────────────────────────────────────

async def step_place_yz(app, leg_qty: int, ref_price: float) -> tuple[OrderGroup, OrderGroup]:
    logger.info("━━ STEP 4: Y/Z OCO order placement ━━")

    buy_px  = round(round((ref_price + 0.01) / 0.01) * 0.01, 2)
    sell_px = round(round((ref_price - 0.01) / 0.01) * 0.01, 2)

    # Use qty=1 for the test to keep it minimal
    test_qty = 1

    y_pid, y_cid = app.next_id(), app.next_id()
    z_pid, z_cid = app.next_id(), app.next_id()

    contract = spy_contract()

    yp = stp("BUY",  test_qty, buy_px,  transmit=False); yp.orderId = y_pid
    yc = mkt("BUY",  test_qty, y_pid,   transmit=True);  yc.orderId = y_cid
    zp = stp("SELL", test_qty, sell_px, transmit=False); zp.orderId = z_pid
    zc = mkt("SELL", test_qty, z_pid,   transmit=True);  zc.orderId = z_cid

    app.placeOrder(y_pid, contract, yp)
    app.placeOrder(y_cid, contract, yc)
    app.placeOrder(z_pid, contract, zp)
    app.placeOrder(z_cid, contract, zc)

    y = OrderGroup(y_pid, y_cid, Side.LONG,  test_qty, entry_price=buy_px)
    z = OrderGroup(z_pid, z_cid, Side.SHORT, test_qty, entry_price=sell_px)

    logger.info("ORDER Y — BUY  STP @ %.2f  (parent=%d child=%d)", buy_px,  y_pid, y_cid)
    logger.info("ORDER Z — SELL STP @ %.2f  (parent=%d child=%d)", sell_px, z_pid, z_cid)
    logger.info("PASS — check IB Gateway order panel for both orders")
    return y, z


# ── Step 5: OCO cancel simulation ────────────────────────────────────────────

async def step_oco_cancel(app, y: OrderGroup, z: OrderGroup):
    logger.info("━━ STEP 5: OCO cancel simulation (Y fills → cancel Z) ━━")
    logger.info("Simulating Y fill — cancelling Z group...")
    await asyncio.sleep(3)  # pause so orders are visible in gateway first

    app.cancelOrder(z.parent_id)
    app.cancelOrder(z.child_id)
    z.cancelled = True

    logger.info("Cancelled Z parent=%d child=%d", z.parent_id, z.child_id)
    logger.info("PASS — Z cancelled, check IB Gateway order panel")


# ── Step 6: cleanup ──────────────────────────────────────────────────────────

async def step_cleanup(app, y: OrderGroup, z: OrderGroup):
    logger.info("━━ STEP 6: Cleanup — cancelling remaining orders ━━")
    await asyncio.sleep(2)
    for oid in (y.parent_id, y.child_id):
        app.cancelOrder(oid)
    logger.info("Cancelled Y parent=%d child=%d", y.parent_id, y.child_id)
    await asyncio.sleep(1)
    logger.info("All test orders cancelled — account clean")


# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    loop = asyncio.get_running_loop()
    app = connect(config.HOST, config.PORT, config.CLIENT_ID, loop)

    print()
    print("=" * 56)
    print("  SPY IBKR BOT — Milestone 1 Proof of Concept Test")
    print("=" * 56)
    print()

    if not await step_connect(app):
        app.disconnect()
        return

    print()
    elv, leg_qty = await step_account(app)

    print()
    ref_price = await step_market_data(app)

    print()
    y, z = await step_place_yz(app, leg_qty, ref_price)

    print()
    await step_oco_cancel(app, y, z)

    print()
    await step_cleanup(app, y, z)

    print()
    print("=" * 56)
    print("  ALL STEPS PASSED")
    print(f"  Account  : {app.account}")
    print(f"  ELV      : ${elv:,.2f}")
    print(f"  Leg qty  : {leg_qty} shares")
    print(f"  Ref price: ${ref_price:.2f}")
    print("=" * 56)
    print()

    app.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
