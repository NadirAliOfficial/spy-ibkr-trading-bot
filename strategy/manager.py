import asyncio
import logging
import time

from gateway import spy_contract
from strategy.orders import Side, OrderGroup, stp, mkt
from utils import calc_leg_qty

logger = logging.getLogger(__name__)

CONTRACT = spy_contract()
_rp = lambda p: round(round(p / 0.01) * 0.01, 2)


class OrderManager:
    def __init__(self, app, leg_qty: int, margin_per_share: float = 0.0, margin_pct: float = 1.6):
        self._app = app
        self._leg = leg_qty
        self._total = leg_qty * 2
        self._margin = margin_per_share
        self._margin_pct = margin_pct   # short SPY margin as fraction of price

        self._open: float = 0.0
        self._entries: int = 0          # position-opens this candle (cap 4)
        self._halted: bool = False
        self._pending: bool = False     # Y/Z placed, awaiting fill

        self._y: OrderGroup | None = None
        self._z: OrderGroup | None = None

        self._s3_pid: int = 0
        self._s3_cid: int = 0
        self._s3_px: float = 0.0
        self._s3_reverse: bool = False

        self._pos: Side = Side.FLAT
        self._pos_qty: int = 0
        self._entry_px: float = 0.0
        self._bot_realized: float = 0.0   # strategy's own realized P&L (this session)

        self._rev_side: Side = Side.FLAT
        self._rev_stp_pid: int = 0
        self._rev_stp_cid: int = 0

        self._sl_count: int = 0
        self._sl_sec: int = -1

        self.last_bid: float = 0.0
        self.last_ask: float = 0.0

    # ── Candle lifecycle ──────────────────────────────────────────────────

    async def on_candle_open(self, open_price: float):
        self._open = open_price
        self._entries = 0
        self._halted = False
        self._pending = False
        self._sl_count = 0
        self._sl_sec = -1
        logger.info("Candle open: %.2f", open_price)
        if self._pos == Side.FLAT and self._rev_side == Side.FLAT:
            await self._place_yz()

    async def on_59th_second(self):
        await self.exit_all("59s timer")
        self._recalc_qty()  # size next candle's orders (spec: at 59th second)

    def _recalc_qty(self):
        # Short SPY margin scales with price; use the live ratio from session start.
        # Size against the lower of live and previous-day equity (Reg-T shorts).
        price = (self.last_bid + self.last_ask) / 2 if self.last_bid > 0 and self.last_ask > 0 else self._open
        elv = self._app.equity_with_loan
        sizing_elv = min(elv, self._app.prev_day_elv) if self._app.prev_day_elv > 0 else elv
        if sizing_elv > 0 and price > 10:
            self._margin = round(price * self._margin_pct, 2)
            new_leg = calc_leg_qty(sizing_elv, self._margin)
            if new_leg != self._leg:
                logger.info("Qty recalc @59s: leg %d->%d (sizingELV=%.2f margin=%.2f)",
                            self._leg, new_leg, sizing_elv, self._margin)
            self._leg = new_leg
            self._total = self._leg * 2

    async def on_tick(self, price: float, sim_hits: int):
        if self._halted:
            return
        if self._rev_side != Side.FLAT:
            pass  # post-rev SL is a live STP @ Open±0.01 — no tick mgmt needed
        elif self._pos == Side.LONG:
            await self._manage_long(price)
        elif self._pos == Side.SHORT:
            await self._manage_short(price)
        elif (self._pos == Side.FLAT and self._rev_side == Side.FLAT and
              not self._pending and sim_hits >= 2 and self._entries < 4):
            await self._place_yz()  # Y2/Z2: re-entry only when sim SL fired 2x

    # ── Fill routing ──────────────────────────────────────────────────────

    async def on_fill(self, order_id: int, fill_price: float):
        if self._y and order_id in (self._y.parent_id, self._y.child_id) and not self._y.filled:
            self._y.filled = True
            self._y.entry_price = fill_price
            await self._on_y_filled(fill_price)

        elif self._z and order_id in (self._z.parent_id, self._z.child_id) and not self._z.filled:
            self._z.filled = True
            self._z.entry_price = fill_price
            await self._on_z_filled(fill_price)

        elif self._s3_cid and order_id == self._s3_cid:
            await self._on_stp3_filled(fill_price)

        elif self._rev_stp_cid and order_id == self._rev_stp_cid:
            await self._on_post_rev_sl_filled(fill_price)

    async def on_partial_fill(self, order_id: int):
        self._app.cancelOrder(order_id)

    def on_reverse_rejected(self, order_id: int):
        pass

    def _register_sl(self) -> bool:
        """Count a stop-loss trigger; return True if 2+ within the same second."""
        sec = int(time.time())
        self._sl_count = self._sl_count + 1 if sec == self._sl_sec else 1
        self._sl_sec = sec
        return self._sl_count >= 2

    def _log_exec(self, side: Side, entry: float, exit_px: float, reason: str):
        """Execution check line: open, fill, exit, labelled Stop Loss or Take Profit.
        Accumulates the strategy's own realized P&L (independent of account PnL)."""
        pnl_sh = (exit_px - entry) if side == Side.LONG else (entry - exit_px)
        trade_pnl = round(pnl_sh * self._total, 2)
        self._bot_realized = round(self._bot_realized + trade_pnl, 2)
        kind = "TAKE PROFIT" if pnl_sh >= 0 else "STOP LOSS"
        logger.info("EXEC %s | open=%.2f fill=%.2f exit=%.2f | %s (%s) | trade=%.2f botPnL=%.2f",
                    side.name, self._open, entry, exit_px, kind, reason, trade_pnl, self._bot_realized)

    # ── Entry fills ───────────────────────────────────────────────────────

    async def _on_y_filled(self, fill_price: float):
        self._cancel_group(self._z)
        self._pos = Side.LONG
        self._pos_qty = self._total
        self._entry_px = fill_price
        self._pending = False
        self._entries += 1
        logger.info("Y LONG filled @ %.2f (entry#%d)", fill_price, self._entries)
        # STP3 armed by _manage_long when SPY <= Open-0.01 (SELL STP @ Bid-0.03)

    async def _on_z_filled(self, fill_price: float):
        self._cancel_group(self._y)
        self._pos = Side.SHORT
        self._pos_qty = self._total
        self._entry_px = fill_price
        self._pending = False
        self._entries += 1
        logger.info("Z SHORT filled @ %.2f (entry#%d)", fill_price, self._entries)
        # STP3 armed by _manage_short when SPY >= Open+0.01 (BUY STP @ Ask+0.03)

    # ── STP3 / reverse logic ──────────────────────────────────────────────

    async def _on_stp3_filled(self, fill_price: float):
        halt_1s = self._register_sl()

        was_long = self._pos == Side.LONG
        is_reverse = self._s3_reverse

        self._log_exec(Side.LONG if was_long else Side.SHORT, self._entry_px, fill_price, "STP3")
        self._pos, self._pos_qty = Side.FLAT, 0
        self._s3_pid = self._s3_cid = 0
        self._s3_px = 0.0
        self._s3_reverse = False
        logger.info("STP3 filled @ %.2f (SL/s: %d, reverse=%s)", fill_price, self._sl_count, is_reverse)

        if halt_1s:
            logger.warning("1-second exit: SL fired 2x — halting candle")
            self._halted = True
            if is_reverse:
                # Combined order already opened the opposite position — flatten it
                action = "BUY" if was_long else "SELL"
                oid = self._app.next_id()
                o = mkt(action, self._total, 0, transmit=True)
                o.orderId = oid
                self._app.placeOrder(oid, CONTRACT, o)
                logger.info("1s halt flatten: %s %d", action, self._total)
            return

        if not is_reverse:
            logger.info("Exit only (no reverse) — candle halted")
            self._halted = True
            return

        # Reverse fired: position is now opposite. This is a new position-open
        # (YA / Y2A) and counts toward the 4-entry cap. Post-rev SL is a normal
        # quantity STP @ Open±0.01 with NO further reverse.
        new_side = Side.SHORT if was_long else Side.LONG
        self._rev_side = new_side
        self._pos_qty = self._total
        self._entry_px = fill_price
        self._entries += 1
        logger.info("Reverse entered: now %s %d (entry#%d)", new_side.name, self._total, self._entries)

        if new_side == Side.SHORT:
            await self._place_post_rev_sl("BUY", _rp(self._open + 0.01))
        else:
            await self._place_post_rev_sl("SELL", _rp(self._open - 0.01))

    async def _on_post_rev_sl_filled(self, fill_price: float):
        halt_1s = self._register_sl()
        self._log_exec(self._rev_side, self._entry_px, fill_price, "post-rev SL")
        logger.info("Post-rev SL filled @ %.2f — FLAT (SL/s: %d)", fill_price, self._sl_count)
        self._rev_side = Side.FLAT
        self._rev_stp_pid = self._rev_stp_cid = 0
        self._pos_qty = 0
        if halt_1s:
            logger.warning("1-second exit: SL fired 2x — halting candle")
            self._halted = True
        # No auto re-entry: Y2/Z2 fire only via on_tick when sim SL >= 2.

    # ── STP3 position management (primary entries Y / Y2 reverse) ──────────

    async def _manage_long(self, price: float):
        favor, arm = _rp(self._open + 0.01), _rp(self._open - 0.01)
        if price >= favor:
            if self._s3_pid:
                self._cancel_stp3()                  # recovered — cancel stop
        elif price <= arm and not self._s3_pid:
            bid = self.last_bid if self.last_bid > 0 else price
            stop = _rp(bid - 0.03)
            logger.info("STP3 arm LONG: SPY=%.2f<=Open-0.01=%.2f  bid=%.2f  stop=bid-0.03=%.2f",
                        price, arm, bid, stop)
            await self._place_stp3("SELL", stop, reverse=self._entries < 4)

    async def _manage_short(self, price: float):
        favor, arm = _rp(self._open - 0.01), _rp(self._open + 0.01)
        if price <= favor:
            if self._s3_pid:
                self._cancel_stp3()                  # recovered — cancel stop
        elif price >= arm and not self._s3_pid:
            ask = self.last_ask if self.last_ask > 0 else price
            stop = _rp(ask + 0.03)
            logger.info("STP3 arm SHORT: SPY=%.2f>=Open+0.01=%.2f  ask=%.2f  stop=ask+0.03=%.2f",
                        price, arm, ask, stop)
            await self._place_stp3("BUY", stop, reverse=self._entries < 4)

    # ── Order placement ───────────────────────────────────────────────────

    async def _place_yz(self):
        if self._entries >= 4 or self._halted:
            return

        y_pid, y_cid = self._app.next_id(), self._app.next_id()
        z_pid, z_cid = self._app.next_id(), self._app.next_id()
        buy_px, sell_px = _rp(self._open + 0.01), _rp(self._open - 0.01)

        oca = f"YZ_{y_pid}"
        yp = stp("BUY",  self._leg, buy_px,  transmit=False);  yp.orderId = y_pid; yp.ocaGroup = oca; yp.ocaType = 1
        yc = mkt("BUY",  self._leg, y_pid,   transmit=True);   yc.orderId = y_cid
        zp = stp("SELL", self._leg, sell_px, transmit=False);  zp.orderId = z_pid; zp.ocaGroup = oca; zp.ocaType = 1
        zc = mkt("SELL", self._leg, z_pid,   transmit=True);   zc.orderId = z_cid

        for oid, order in ((y_pid, yp), (y_cid, yc), (z_pid, zp), (z_cid, zc)):
            self._app.placeOrder(oid, CONTRACT, order)

        self._y = OrderGroup(y_pid, y_cid, Side.LONG,  self._leg, entry_price=buy_px)
        self._z = OrderGroup(z_pid, z_cid, Side.SHORT, self._leg, entry_price=sell_px)
        self._pending = True
        logger.info("Y/Z OCO: BUY=%.2f SELL=%.2f (entries so far=%d)", buy_px, sell_px, self._entries)

    async def _place_stp3(self, action: str, stop_px: float, reverse: bool = False):
        pid, cid = self._app.next_id(), self._app.next_id()
        child_qty = (2 * self._total - 1) if reverse else (self._total - 1)
        p = stp(action, 1, stop_px, transmit=False); p.orderId = pid
        c = mkt(action, child_qty, pid, transmit=True); c.orderId = cid
        self._app.placeOrder(pid, CONTRACT, p)
        self._app.placeOrder(cid, CONTRACT, c)
        self._s3_pid, self._s3_cid, self._s3_px = pid, cid, stop_px
        self._s3_reverse = reverse
        logger.info("STP3: %s @ %.2f qty=1+%d reverse=%s", action, stop_px, child_qty, reverse)

    def _cancel_stp3(self):
        for oid in (self._s3_pid, self._s3_cid):
            if oid:
                self._app.cancelOrder(oid)
        self._s3_pid = self._s3_cid = 0
        self._s3_px = 0.0
        self._s3_reverse = False

    async def _place_post_rev_sl(self, action: str, stop_px: float):
        pid, cid = self._app.next_id(), self._app.next_id()
        p = stp(action, 1, stop_px, transmit=False); p.orderId = pid
        c = mkt(action, self._total - 1, pid, transmit=True); c.orderId = cid
        self._app.placeOrder(pid, CONTRACT, p)
        self._app.placeOrder(cid, CONTRACT, c)
        self._rev_stp_pid = pid
        self._rev_stp_cid = cid
        logger.info("Post-rev SL: %s @ %.2f qty=1+%d", action, stop_px, self._total - 1)

    # ── Global exit ───────────────────────────────────────────────────────

    async def exit_all(self, reason: str = ""):
        self._halted = True  # block concurrent on_tick from re-entering
        logger.info("Exit all: %s", reason)
        self._cancel_group(self._y)
        self._cancel_group(self._z)
        self._cancel_stp3()
        for oid in (self._rev_stp_pid, self._rev_stp_cid):
            if oid:
                self._app.cancelOrder(oid)

        mid = (self.last_bid + self.last_ask) / 2 if self.last_bid > 0 and self.last_ask > 0 else self._open
        if self._pos != Side.FLAT and self._pos_qty > 0:
            action = "SELL" if self._pos == Side.LONG else "BUY"
            oid = self._app.next_id()
            o = mkt(action, self._pos_qty, 0, transmit=True)
            o.orderId = oid
            self._app.placeOrder(oid, CONTRACT, o)
            self._log_exec(self._pos, self._entry_px, mid, reason or "exit all")
            logger.info("Flatten pos: %s %d", action, self._pos_qty)
        elif self._rev_side != Side.FLAT and self._pos_qty > 0:
            action = "BUY" if self._rev_side == Side.SHORT else "SELL"
            oid = self._app.next_id()
            o = mkt(action, self._pos_qty, 0, transmit=True)
            o.orderId = oid
            self._app.placeOrder(oid, CONTRACT, o)
            self._log_exec(self._rev_side, self._entry_px, mid, reason or "exit all")
            logger.info("Flatten rev: %s %d", action, self._pos_qty)

        self._y = self._z = None
        self._pending = False
        self._s3_pid = self._s3_cid = 0
        self._s3_px = 0.0
        self._s3_reverse = False
        self._pos, self._pos_qty = Side.FLAT, 0
        self._rev_side = Side.FLAT
        self._rev_stp_pid = self._rev_stp_cid = 0

    def _cancel_group(self, g: OrderGroup | None):
        if g and not g.filled and not g.cancelled:
            self._app.cancelOrder(g.parent_id)
            self._app.cancelOrder(g.child_id)
            g.cancelled = True
