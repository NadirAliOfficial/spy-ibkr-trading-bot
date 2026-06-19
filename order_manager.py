import logging
import time
from dataclasses import dataclass
from enum import Enum, auto

from ibapi.order import Order
from connection import spy_contract
from utils import round_price

logger = logging.getLogger(__name__)

CONTRACT = spy_contract()


class Side(Enum):
    FLAT = auto()
    LONG = auto()
    SHORT = auto()


@dataclass
class OrderGroup:
    parent_id: int
    child_id: int
    side: Side
    qty: int
    entry_price: float = 0.0
    filled: bool = False
    cancelled: bool = False


def _stp(action: str, qty: int, stop_px: float, transmit: bool, parent_id: int = 0) -> Order:
    o = Order()
    o.action = action
    o.orderType = "STP"
    o.totalQuantity = qty
    o.auxPrice = round_price(stop_px)
    o.transmit = transmit
    o.tif = "DAY"
    if parent_id:
        o.parentId = parent_id
    return o


def _mkt(action: str, qty: int, parent_id: int, transmit: bool) -> Order:
    o = Order()
    o.action = action
    o.orderType = "MKT"
    o.totalQuantity = qty
    o.parentId = parent_id
    o.transmit = transmit
    o.tif = "DAY"
    return o


class OrderManager:
    """
    Implements Y/Z OCO candle strategy.

    ORDER Y (LONG):  BUY STP @ Open+0.01 — STP3 exit @ Open-0.01
      - Cancel STP3 when price >= Open+0.01 (in profit zone)
      - Roll STP3 down when price <= Open-0.01
      - On STP3 fire: reverse SHORT via SELL STP @ Bid-0.03
      - Post-reverse MKT stop if price >= Open+0.01

    ORDER Z (SHORT): SELL STP @ Open-0.01 — STP3 exit @ Open+0.01
      - Cancel STP3 when price <= Open-0.01 (in profit zone)
      - Roll STP3 up when price >= Open+0.01
      - On STP3 fire: reverse LONG via BUY STP @ Ask+0.03
      - Post-reverse MKT stop if price <= Open-0.01

    Y2/Z2: re-enter when sim SL hits >= 2 this candle. Max 4 entries/candle.
    1-second exit: halt candle if actual SL fires 2x within 1 second.
    """

    def __init__(self, app, leg_qty: int):
        self._app = app
        self._leg_qty = leg_qty
        self._total_qty = leg_qty * 2

        self._candle_open: float = 0.0
        self._entries: int = 0
        self._halted: bool = False

        self._y: OrderGroup | None = None
        self._z: OrderGroup | None = None

        self._stp3_pid: int = 0
        self._stp3_cid: int = 0
        self._stp3_px: float = 0.0

        self._position: Side = Side.FLAT
        self._position_qty: int = 0

        self._reverse_id: int = 0
        self._reverse_side: Side = Side.FLAT  # direction of reverse position after STP3

        self._sl_count: int = 0    # SL fires in current second
        self._sl_second: int = -1

        self.last_bid: float = 0.0
        self.last_ask: float = 0.0

    async def on_candle_open(self, open_price: float):
        self._candle_open = open_price
        self._entries = 0
        self._halted = False
        logger.info("Candle open: %.2f", open_price)
        if self._position == Side.FLAT and self._reverse_side == Side.FLAT:
            await self._place_yz(open_price)

    async def on_59th_second(self):
        await self.exit_all("59s timer")

    async def on_tick(self, price: float, sim_sl_count: int):
        if self._halted:
            return

        if self._reverse_side != Side.FLAT:
            await self._check_reverse_stop(price)
        elif self._position == Side.LONG:
            await self._manage_long(price)
        elif self._position == Side.SHORT:
            await self._manage_short(price)
        elif self._position == Side.FLAT and sim_sl_count >= 2 and self._entries < 4:
            await self._place_yz(self._candle_open)

    async def on_fill(self, order_id: int, fill_price: float):
        if self._y and order_id in (self._y.parent_id, self._y.child_id) and not self._y.filled:
            self._y.filled = True
            self._y.entry_price = fill_price
            await self._on_y_filled(fill_price)

        elif self._z and order_id in (self._z.parent_id, self._z.child_id) and not self._z.filled:
            self._z.filled = True
            self._z.entry_price = fill_price
            await self._on_z_filled(fill_price)

        elif self._stp3_pid and order_id in (self._stp3_pid, self._stp3_cid):
            await self._on_stp3_filled(fill_price)

        elif order_id == self._reverse_id and self._reverse_side == Side.FLAT:
            await self._on_reverse_filled(fill_price)

    async def on_partial_fill(self, order_id: int):
        self._app.cancelOrder(order_id, "")

    async def _on_y_filled(self, fill_price: float):
        logger.info("Y LONG filled @ %.2f", fill_price)
        self._cancel_group(self._z)
        self._position = Side.LONG
        self._position_qty = self._total_qty
        await self._place_stp3("SELL", round_price(self._candle_open - 0.01))

    async def _on_z_filled(self, fill_price: float):
        logger.info("Z SHORT filled @ %.2f", fill_price)
        self._cancel_group(self._y)
        self._position = Side.SHORT
        self._position_qty = self._total_qty
        await self._place_stp3("BUY", round_price(self._candle_open + 0.01))

    async def _on_stp3_filled(self, fill_price: float):
        sec = int(time.time())
        if sec == self._sl_second:
            self._sl_count += 1
        else:
            self._sl_second = sec
            self._sl_count = 1

        prev_side = self._position
        self._position = Side.FLAT
        self._position_qty = 0
        self._stp3_pid = self._stp3_cid = 0
        self._stp3_px = 0.0

        logger.info("STP3 filled @ %.2f (SL fires this second: %d)", fill_price, self._sl_count)

        if self._sl_count >= 2:
            logger.warning("1-second exit: SL fired 2x — halting candle")
            self._halted = True
            return

        if prev_side == Side.LONG:
            await self._place_reverse("SELL", fill_price)
        else:
            await self._place_reverse("BUY", fill_price)

    async def _on_reverse_filled(self, fill_price: float):
        # Reverse entry confirmed — set position to opposite of what STP3 closed
        if self._reverse_id == 0:
            return
        # Determine which direction the reverse entered
        # SELL reverse = now SHORT; BUY reverse = now LONG
        # We infer from the action of the reverse order (tracked via _reverse_side placeholder)
        # The flag _reverse_side was set in _place_reverse
        self._position = self._reverse_side
        self._position_qty = self._total_qty
        logger.info("Reverse filled @ %.2f — now %s", fill_price, self._reverse_side.name)

    async def _check_reverse_stop(self, price: float):
        """MKT stop for post-reverse position."""
        if self._reverse_side == Side.SHORT and price >= round_price(self._candle_open + 0.01):
            logger.info("Reverse SHORT stop hit @ %.2f", price)
            await self._flatten_reverse()
        elif self._reverse_side == Side.LONG and price <= round_price(self._candle_open - 0.01):
            logger.info("Reverse LONG stop hit @ %.2f", price)
            await self._flatten_reverse()

    async def _flatten_reverse(self):
        action = "BUY" if self._reverse_side == Side.SHORT else "SELL"
        oid = self._app.next_id()
        o = _mkt(action, self._total_qty, 0, transmit=True)
        o.orderId = oid
        self._app.placeOrder(oid, CONTRACT, o)
        self._reverse_side = Side.FLAT
        self._position = Side.FLAT
        self._position_qty = 0
        logger.info("Reverse position flattened: %s %d", action, self._total_qty)
        if self._entries < 4:
            await self._place_yz(self._candle_open)

    async def _manage_long(self, price: float):
        favor = round_price(self._candle_open + 0.01)
        stop = round_price(self._candle_open - 0.01)

        if price >= favor and self._stp3_pid:
            self._cancel_stp3()
        elif price <= stop and self._stp3_pid:
            new_px = round_price(price - 0.01)
            if abs(new_px - self._stp3_px) >= 0.01:
                await self._replace_stp3("SELL", new_px)
        elif price <= stop and not self._stp3_pid:
            await self._place_stp3("SELL", stop)

    async def _manage_short(self, price: float):
        favor = round_price(self._candle_open - 0.01)
        stop = round_price(self._candle_open + 0.01)

        if price <= favor and self._stp3_pid:
            self._cancel_stp3()
        elif price >= stop and self._stp3_pid:
            new_px = round_price(price + 0.01)
            if abs(new_px - self._stp3_px) >= 0.01:
                await self._replace_stp3("BUY", new_px)
        elif price >= stop and not self._stp3_pid:
            await self._place_stp3("BUY", stop)

    async def _place_yz(self, open_price: float):
        if self._entries >= 4 or self._halted:
            return

        y_pid, y_cid = self._app.next_id(), self._app.next_id()
        z_pid, z_cid = self._app.next_id(), self._app.next_id()

        buy_px = round_price(open_price + 0.01)
        sell_px = round_price(open_price - 0.01)

        yp = _stp("BUY", self._leg_qty, buy_px, transmit=False)
        yp.orderId = y_pid
        yc = _mkt("BUY", self._leg_qty, y_pid, transmit=True)
        yc.orderId = y_cid

        zp = _stp("SELL", self._leg_qty, sell_px, transmit=False)
        zp.orderId = z_pid
        zc = _mkt("SELL", self._leg_qty, z_pid, transmit=True)
        zc.orderId = z_cid

        for oid, order in ((y_pid, yp), (y_cid, yc), (z_pid, zp), (z_cid, zc)):
            self._app.placeOrder(oid, CONTRACT, order)

        self._y = OrderGroup(y_pid, y_cid, Side.LONG, self._leg_qty, entry_price=buy_px)
        self._z = OrderGroup(z_pid, z_cid, Side.SHORT, self._leg_qty, entry_price=sell_px)
        self._entries += 1
        logger.info("Y/Z OCO: BUY=%.2f SELL=%.2f entry#%d", buy_px, sell_px, self._entries)

    async def _place_stp3(self, action: str, stop_px: float):
        pid, cid = self._app.next_id(), self._app.next_id()

        p = _stp(action, 1, stop_px, transmit=False)
        p.orderId = pid
        c = _mkt(action, self._total_qty - 1, pid, transmit=True)
        c.orderId = cid

        self._app.placeOrder(pid, CONTRACT, p)
        self._app.placeOrder(cid, CONTRACT, c)
        self._stp3_pid, self._stp3_cid, self._stp3_px = pid, cid, stop_px
        logger.info("STP3: %s @ %.2f", action, stop_px)

    def _cancel_stp3(self):
        if self._stp3_pid:
            self._app.cancelOrder(self._stp3_pid, "")
        if self._stp3_cid:
            self._app.cancelOrder(self._stp3_cid, "")
        self._stp3_pid = self._stp3_cid = 0
        self._stp3_px = 0.0

    async def _replace_stp3(self, action: str, new_px: float):
        self._cancel_stp3()
        await self._place_stp3(action, new_px)
        logger.debug("STP3 replaced -> %.2f", new_px)

    async def _place_reverse(self, action: str, ref_price: float):
        pid = self._app.next_id()
        if action == "SELL":
            px = round_price((self.last_bid or ref_price) - 0.03)
            self._reverse_side = Side.SHORT
        else:
            px = round_price((self.last_ask or ref_price) + 0.03)
            self._reverse_side = Side.LONG

        o = _stp(action, self._total_qty, px, transmit=True)
        o.orderId = pid
        self._app.placeOrder(pid, CONTRACT, o)
        self._reverse_id = pid
        logger.info("Reverse: %s STP @ %.2f", action, px)

    async def exit_all(self, reason: str = ""):
        logger.info("Exit all: %s", reason)
        self._cancel_group(self._y)
        self._cancel_group(self._z)
        self._cancel_stp3()
        if self._reverse_id:
            self._app.cancelOrder(self._reverse_id, "")

        qty = self._position_qty or (self._total_qty if self._reverse_side != Side.FLAT else 0)
        side = self._position if self._position != Side.FLAT else self._reverse_side

        if side != Side.FLAT and qty > 0:
            action = "SELL" if side == Side.LONG else "BUY"
            oid = self._app.next_id()
            o = _mkt(action, qty, 0, transmit=True)
            o.orderId = oid
            self._app.placeOrder(oid, CONTRACT, o)
            logger.info("Flatten: %s %d shares", action, qty)

        self._y = self._z = None
        self._stp3_pid = self._stp3_cid = 0
        self._stp3_px = 0.0
        self._position = Side.FLAT
        self._position_qty = 0
        self._reverse_id = 0
        self._reverse_side = Side.FLAT

    def _cancel_group(self, g: OrderGroup | None):
        if g and not g.filled and not g.cancelled:
            self._app.cancelOrder(g.parent_id, "")
            self._app.cancelOrder(g.child_id, "")
            g.cancelled = True
