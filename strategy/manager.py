import logging
import time

from gateway import spy_contract
from strategy.orders import Side, OrderGroup, stp, mkt

logger = logging.getLogger(__name__)

CONTRACT = spy_contract()
_rp = lambda p: round(round(p / 0.01) * 0.01, 2)


class OrderManager:
    """
    Y/Z OCO candle strategy.

    ORDER Y (LONG):  BUY STP @ Open+0.01 — STP3 exit @ Open-0.01
      Cancel STP3 when price >= Open+0.01 (profit zone); roll down when <= Open-0.01.
      On STP3 fire: reverse SHORT via SELL STP @ Bid-0.03.
      Post-reverse MKT stop if price >= Open+0.01.

    ORDER Z (SHORT): SELL STP @ Open-0.01 — STP3 exit @ Open+0.01
      Cancel STP3 when price <= Open-0.01 (profit zone); roll up when >= Open+0.01.
      On STP3 fire: reverse LONG via BUY STP @ Ask+0.03.
      Post-reverse MKT stop if price <= Open-0.01.

    Y2/Z2: re-enter when sim SL hits >= 2 this candle. Max 4 entries/candle.
    1-second exit: halt candle if actual SL fires 2x in 1 second.
    """

    def __init__(self, app, leg_qty: int):
        self._app = app
        self._leg = leg_qty
        self._total = leg_qty * 2

        self._open: float = 0.0
        self._entries: int = 0
        self._halted: bool = False

        self._y: OrderGroup | None = None
        self._z: OrderGroup | None = None

        self._s3_pid: int = 0
        self._s3_cid: int = 0
        self._s3_px: float = 0.0

        self._pos: Side = Side.FLAT
        self._pos_qty: int = 0

        self._rev_id: int = 0
        self._rev_side: Side = Side.FLAT

        self._sl_count: int = 0
        self._sl_sec: int = -1

        self.last_bid: float = 0.0
        self.last_ask: float = 0.0

    # ── Candle lifecycle ──────────────────────────────────────────────────

    async def on_candle_open(self, open_price: float):
        self._open = open_price
        self._entries = 0
        self._halted = False
        logger.info("Candle open: %.2f", open_price)
        if self._pos == Side.FLAT and self._rev_side == Side.FLAT:
            await self._place_yz()

    async def on_59th_second(self):
        await self.exit_all("59s timer")

    async def on_tick(self, price: float, sim_hits: int):
        if self._halted:
            return
        if self._rev_side != Side.FLAT:
            await self._check_reverse_stop(price)
        elif self._pos == Side.LONG:
            await self._manage_long(price)
        elif self._pos == Side.SHORT:
            await self._manage_short(price)
        elif self._pos == Side.FLAT and sim_hits >= 2 and self._entries < 4:
            await self._place_yz()

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

        elif self._s3_pid and order_id in (self._s3_pid, self._s3_cid):
            await self._on_stp3_filled(fill_price)

        elif order_id == self._rev_id and self._rev_side != Side.FLAT:
            await self._on_reverse_filled(fill_price)

    async def on_partial_fill(self, order_id: int):
        self._app.cancelOrder(order_id)

    def on_reverse_rejected(self, order_id: int):
        logger.warning("Reverse %d rejected — resetting to FLAT", order_id)
        self._rev_side = Side.FLAT
        self._rev_id = 0

    # ── Entry fills ───────────────────────────────────────────────────────

    async def _on_y_filled(self, fill_price: float):
        logger.info("Y LONG filled @ %.2f", fill_price)
        self._cancel_group(self._z)
        self._pos = Side.LONG
        self._pos_qty = self._total
        await self._place_stp3("SELL", _rp(self._open - 0.01))

    async def _on_z_filled(self, fill_price: float):
        logger.info("Z SHORT filled @ %.2f", fill_price)
        self._cancel_group(self._y)
        self._pos = Side.SHORT
        self._pos_qty = self._total
        await self._place_stp3("BUY", _rp(self._open + 0.01))

    # ── STP3 logic ────────────────────────────────────────────────────────

    async def _on_stp3_filled(self, fill_price: float):
        sec = int(time.time())
        self._sl_count = self._sl_count + 1 if sec == self._sl_sec else 1
        self._sl_sec = sec

        prev = self._pos
        self._pos, self._pos_qty = Side.FLAT, 0
        self._s3_pid = self._s3_cid = 0
        self._s3_px = 0.0
        logger.info("STP3 filled @ %.2f (SL/s: %d)", fill_price, self._sl_count)

        if self._sl_count >= 2:
            logger.warning("1-second exit: SL fired 2x — halting candle")
            self._halted = True
            return

        if self._entries >= 4:
            logger.info("4th trade SL — no reverse, candle halted")
            self._halted = True
            return

        await self._place_reverse("SELL" if prev == Side.LONG else "BUY", fill_price)

    async def _on_reverse_filled(self, fill_price: float):
        self._pos = self._rev_side
        self._pos_qty = self._total
        logger.info("Reverse filled @ %.2f — now %s", fill_price, self._rev_side.name)

    async def _check_reverse_stop(self, price: float):
        if self._rev_side == Side.SHORT and price >= _rp(self._open + 0.01):
            logger.info("Reverse SHORT stop @ %.2f", price)
            await self._flatten_reverse()
        elif self._rev_side == Side.LONG and price <= _rp(self._open - 0.01):
            logger.info("Reverse LONG stop @ %.2f", price)
            await self._flatten_reverse()

    async def _flatten_reverse(self):
        action = "BUY" if self._rev_side == Side.SHORT else "SELL"
        oid = self._app.next_id()
        qty = self._pos_qty or self._total
        o = mkt(action, qty, 0, transmit=True)
        o.orderId = oid
        self._app.placeOrder(oid, CONTRACT, o)
        self._rev_side, self._rev_id = Side.FLAT, 0
        self._pos, self._pos_qty = Side.FLAT, 0
        logger.info("Reverse flattened: %s %d", action, self._total)
        if self._entries < 4:
            await self._place_yz()

    # ── STP3 position management ──────────────────────────────────────────

    async def _manage_long(self, price: float):
        favor, stop = _rp(self._open + 0.01), _rp(self._open - 0.01)
        if price >= favor and self._s3_pid:
            self._cancel_stp3()
        elif price <= stop and self._s3_pid:
            new = _rp(price - 0.01)
            if abs(new - self._s3_px) >= 0.05:
                await self._replace_stp3("SELL", new)
        elif price <= stop and not self._s3_pid:
            await self._place_stp3("SELL", stop)

    async def _manage_short(self, price: float):
        favor, stop = _rp(self._open - 0.01), _rp(self._open + 0.01)
        if price <= favor and self._s3_pid:
            self._cancel_stp3()
        elif price >= stop and self._s3_pid:
            new = _rp(price + 0.01)
            if abs(new - self._s3_px) >= 0.05:
                await self._replace_stp3("BUY", new)
        elif price >= stop and not self._s3_pid:
            await self._place_stp3("BUY", stop)

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
        self._entries += 1
        logger.info("Y/Z OCO: BUY=%.2f SELL=%.2f entry#%d", buy_px, sell_px, self._entries)

    async def _place_stp3(self, action: str, stop_px: float):
        pid, cid = self._app.next_id(), self._app.next_id()
        p = stp(action, 1, stop_px, transmit=False);            p.orderId = pid
        c = mkt(action, self._total - 1, pid, transmit=True);  c.orderId = cid
        self._app.placeOrder(pid, CONTRACT, p)
        self._app.placeOrder(cid, CONTRACT, c)
        self._s3_pid, self._s3_cid, self._s3_px = pid, cid, stop_px
        logger.info("STP3: %s @ %.2f", action, stop_px)

    def _cancel_stp3(self):
        for oid in (self._s3_pid, self._s3_cid):
            if oid:
                self._app.cancelOrder(oid)
        self._s3_pid = self._s3_cid = 0
        self._s3_px = 0.0

    async def _replace_stp3(self, action: str, new_px: float):
        self._cancel_stp3()
        await self._place_stp3(action, new_px)
        logger.debug("STP3 rolled -> %.2f", new_px)

    async def _place_reverse(self, action: str, ref: float):
        pid = self._app.next_id()
        if action == "SELL":
            px = _rp((self.last_bid or ref) - 0.03)
            self._rev_side = Side.SHORT
        else:
            px = _rp((self.last_ask or ref) + 0.03)
            self._rev_side = Side.LONG
        o = stp(action, self._total, px, transmit=True)
        o.orderId = pid
        self._app.placeOrder(pid, CONTRACT, o)
        self._rev_id = pid
        logger.info("Reverse: %s STP @ %.2f", action, px)

    # ── Global exit ───────────────────────────────────────────────────────

    async def exit_all(self, reason: str = ""):
        logger.info("Exit all: %s", reason)
        self._cancel_group(self._y)
        self._cancel_group(self._z)
        self._cancel_stp3()
        if self._rev_id:
            self._app.cancelOrder(self._rev_id)

        rev_qty = self._total * 2 if self._rev_side != Side.FLAT else 0
        qty = self._pos_qty or rev_qty
        side = self._pos if self._pos != Side.FLAT else self._rev_side

        if side != Side.FLAT and qty > 0:
            action = "SELL" if side == Side.LONG else "BUY"
            oid = self._app.next_id()
            o = mkt(action, qty, 0, transmit=True)
            o.orderId = oid
            self._app.placeOrder(oid, CONTRACT, o)
            logger.info("Flatten: %s %d", action, qty)

        self._y = self._z = None
        self._s3_pid = self._s3_cid = 0
        self._s3_px = 0.0
        self._pos, self._pos_qty = Side.FLAT, 0
        self._rev_id = 0
        self._rev_side = Side.FLAT

    def _cancel_group(self, g: OrderGroup | None):
        if g and not g.filled and not g.cancelled:
            self._app.cancelOrder(g.parent_id)
            self._app.cancelOrder(g.child_id)
            g.cancelled = True
