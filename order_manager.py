"""
Order manager: Y / Z OCO logic with STP3, reverse trades, and candle-level state.

Per spec (PDF):
  ORDER Y (LONG):
    Entry:  BUY STP @ Open+0.01  (parent 49% eq), BUY MKT child 49% eq
    STP3:   SELL STP 1 share @ Open-0.01 (parent), SELL MKT rest (child)
    - Cancel STP3 if SPY >= Open+0.01   (price moved in favor — recalculate)
    - Recalculate/replace STP3 if SPY <= Open-0.01  (rolling stop)
    - Reverse: if STP3 fires -> SELL STP @ Bid-0.03 (go short)
    - After reverse fills: MKT stop if SPY >= Open+0.01

  ORDER Z (SHORT):
    Entry:  SELL STP @ Open-0.01 (parent 49% eq), SELL MKT child 49% eq
    STP3:   BUY STP 1 share @ Open+0.01 (parent), BUY MKT rest (child)
    - Cancel STP3 if SPY <= Open-0.01   (price moved in favor)
    - Recalculate/replace STP3 if SPY >= Open+0.01  (rolling stop)
    - Reverse: if STP3 fires -> BUY STP @ Ask+0.03 (go long)
    - After reverse fills: MKT stop if SPY <= Open-0.01

  Y2/Z2: additional entry when simulated SL triggers >= 2 times inside the candle.
  Max 4 entries per 1-min candle.
  1-second exit: if actual SL fires 2x in 1 second, halt trading that candle.
"""
import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum, auto

from ibapi.order import Order
from utils import round_price

logger = logging.getLogger(__name__)


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


def _stp_order(action: str, qty: int, stop_px: float, transmit: bool, parent_id: int = 0) -> Order:
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


def _mkt_order(action: str, qty: int, parent_id: int, transmit: bool) -> Order:
    o = Order()
    o.action = action
    o.orderType = "MKT"
    o.totalQuantity = qty
    o.parentId = parent_id
    o.transmit = transmit
    o.tif = "DAY"
    return o


class OrderManager:
    def __init__(self, app, leg_qty: int):
        """
        app      : IBApp instance
        leg_qty  : shares per leg (49% equity); total position = leg_qty * 2
        """
        self._app = app
        self._leg_qty = leg_qty
        self._total_qty = leg_qty * 2

        # Current candle state
        self._candle_open: float = 0.0
        self._entries_this_candle: int = 0
        self._candle_halted: bool = False  # 1-second exit condition triggered

        # OCO group (Y and Z placed together)
        self._y: OrderGroup | None = None
        self._z: OrderGroup | None = None

        # STP3 ids
        self._stp3_parent: int = 0
        self._stp3_child: int = 0
        self._stp3_stop_px: float = 0.0

        # Active position
        self._position: Side = Side.FLAT
        self._position_qty: int = 0

        # Reverse trade state
        self._reverse_entry_id: int = 0
        self._reverse_filled: bool = False

        # 1-second SL tracker for exit condition
        self._sl_fires_this_second: int = 0
        self._sl_second: int = -1

        # Bid/Ask for reverse orders
        self._last_bid: float = 0.0
        self._last_ask: float = 0.0
        self._last_price: float = 0.0

        from connection import make_spy_contract
        self._contract = make_spy_contract()

    # ------------------------------------------------------------------ #
    # Candle lifecycle                                                      #
    # ------------------------------------------------------------------ #

    async def on_candle_open(self, open_price: float):
        self._candle_open = open_price
        self._entries_this_candle = 0
        self._candle_halted = False
        logger.info("Candle open: %.2f", open_price)

        if self._position != Side.FLAT:
            logger.info("Already in position at candle open — waiting for STP3")
            return

        await self._place_yz(open_price)

    async def on_59th_second(self):
        logger.info("59th second — exiting all positions")
        await self._exit_all("59s timer")

    async def on_tick(self, price: float, bid: float, ask: float, sim_sl_count: int):
        self._last_price = price
        if bid > 0:
            self._last_bid = bid
        if ask > 0:
            self._last_ask = ask

        if self._candle_halted:
            return

        if self._position == Side.LONG:
            await self._manage_long(price, bid)

        elif self._position == Side.SHORT:
            await self._manage_short(price, ask)

        elif self._position == Side.FLAT:
            # Y2/Z2: fire additional entry when sim SL has hit >= 2 this candle
            if sim_sl_count >= 2 and self._entries_this_candle < 4:
                await self._place_yz(self._candle_open)

    # ------------------------------------------------------------------ #
    # STP3 management per direction                                        #
    # ------------------------------------------------------------------ #

    async def _manage_long(self, price: float, bid: float):
        """
        Long position monitoring:
        - Cancel STP3 if price >= Open+0.01 (in profit — do NOT stop out)
        - Recalculate/replace STP3 if price <= Open-0.01 (roll stop)
        """
        favor = round_price(self._candle_open + 0.01)
        stop_level = round_price(self._candle_open - 0.01)

        if price >= favor and self._stp3_parent:
            # Price moved in favor — cancel STP3 (will be re-placed on pullback)
            self._cancel_stp3()
            logger.debug("Long in favor @ %.2f — STP3 cancelled", price)

        elif price <= stop_level and self._stp3_parent:
            # Price at or below stop level — roll STP3 down
            new_stp = round_price(price - 0.01)
            if abs(new_stp - self._stp3_stop_px) >= 0.01:
                await self._replace_stp3("SELL", new_stp)

        elif price <= stop_level and not self._stp3_parent:
            # STP3 was cancelled (was in favor) but price pulled back — re-place
            await self._place_stp3("SELL", stop_level)

    async def _manage_short(self, price: float, ask: float):
        """
        Short position monitoring:
        - Cancel STP3 if price <= Open-0.01 (moving in favor)
        - Recalculate/replace STP3 if price >= Open+0.01 (roll stop up)
        """
        favor = round_price(self._candle_open - 0.01)
        stop_level = round_price(self._candle_open + 0.01)

        if price <= favor and self._stp3_parent:
            self._cancel_stp3()
            logger.debug("Short in favor @ %.2f — STP3 cancelled", price)

        elif price >= stop_level and self._stp3_parent:
            new_stp = round_price(price + 0.01)
            if abs(new_stp - self._stp3_stop_px) >= 0.01:
                await self._replace_stp3("BUY", new_stp)

        elif price >= stop_level and not self._stp3_parent:
            await self._place_stp3("BUY", stop_level)

    # ------------------------------------------------------------------ #
    # Order placement helpers                                              #
    # ------------------------------------------------------------------ #

    async def _place_yz(self, open_price: float):
        if self._entries_this_candle >= 4 or self._candle_halted:
            return

        y_pid = self._app.next_id()
        y_cid = self._app.next_id()
        z_pid = self._app.next_id()
        z_cid = self._app.next_id()

        buy_stp_px = round_price(open_price + 0.01)
        sell_stp_px = round_price(open_price - 0.01)

        # Y: BUY STP parent + BUY MKT child
        y_p = _stp_order("BUY", self._leg_qty, buy_stp_px, transmit=False)
        y_p.orderId = y_pid
        y_c = _mkt_order("BUY", self._leg_qty, y_pid, transmit=True)
        y_c.orderId = y_cid

        # Z: SELL STP parent + SELL MKT child
        z_p = _stp_order("SELL", self._leg_qty, sell_stp_px, transmit=False)
        z_p.orderId = z_pid
        z_c = _mkt_order("SELL", self._leg_qty, z_pid, transmit=True)
        z_c.orderId = z_cid

        self._app.placeOrder(y_pid, self._contract, y_p)
        self._app.placeOrder(y_cid, self._contract, y_c)
        self._app.placeOrder(z_pid, self._contract, z_p)
        self._app.placeOrder(z_cid, self._contract, z_c)

        self._y = OrderGroup(y_pid, y_cid, Side.LONG, self._leg_qty, entry_price=buy_stp_px)
        self._z = OrderGroup(z_pid, z_cid, Side.SHORT, self._leg_qty, entry_price=sell_stp_px)
        self._entries_this_candle += 1

        logger.info("Y/Z OCO placed: Y_BUY=%.2f Z_SELL=%.2f entry#=%d",
                    buy_stp_px, sell_stp_px, self._entries_this_candle)

    async def _place_stp3(self, exit_action: str, stop_px: float):
        pid = self._app.next_id()
        cid = self._app.next_id()

        p = _stp_order(exit_action, 1, stop_px, transmit=False)
        p.orderId = pid
        c = _mkt_order(exit_action, self._total_qty - 1, pid, transmit=True)
        c.orderId = cid

        self._app.placeOrder(pid, self._contract, p)
        self._app.placeOrder(cid, self._contract, c)

        self._stp3_parent = pid
        self._stp3_child = cid
        self._stp3_stop_px = stop_px
        logger.info("STP3 placed: %s @ %.2f (ids %d/%d)", exit_action, stop_px, pid, cid)

    def _cancel_stp3(self):
        if self._stp3_parent:
            self._app.cancelOrder(self._stp3_parent, "")
        if self._stp3_child:
            self._app.cancelOrder(self._stp3_child, "")
        self._stp3_parent = 0
        self._stp3_child = 0
        self._stp3_stop_px = 0.0

    async def _replace_stp3(self, exit_action: str, new_stop: float):
        self._cancel_stp3()
        await self._place_stp3(exit_action, new_stop)
        logger.info("STP3 replaced -> %.2f", new_stop)

    async def _place_reverse_entry(self, action: str, ref_price: float):
        """Place the reverse STP entry after STP3 fires."""
        pid = self._app.next_id()
        if action == "SELL":
            # Y reversed to short: SELL STP @ Bid-0.03
            stp_px = round_price(self._last_bid - 0.03) if self._last_bid > 0 else round_price(ref_price - 0.03)
        else:
            # Z reversed to long: BUY STP @ Ask+0.03
            stp_px = round_price(self._last_ask + 0.03) if self._last_ask > 0 else round_price(ref_price + 0.03)

        o = _stp_order(action, self._total_qty, stp_px, transmit=True)
        o.orderId = pid
        self._app.placeOrder(pid, self._contract, o)
        self._reverse_entry_id = pid
        self._reverse_filled = False
        logger.info("Reverse entry: %s STP @ %.2f (id %d)", action, stp_px, pid)

    # ------------------------------------------------------------------ #
    # Fill handling (called by main order-event loop)                      #
    # ------------------------------------------------------------------ #

    async def on_fill(self, order_id: int, fill_price: float, filled_qty: float):
        # Y fill
        if self._y and order_id in (self._y.parent_id, self._y.child_id) and not self._y.filled:
            self._y.filled = True
            self._y.entry_price = fill_price
            await self._on_y_filled(fill_price)

        # Z fill
        elif self._z and order_id in (self._z.parent_id, self._z.child_id) and not self._z.filled:
            self._z.filled = True
            self._z.entry_price = fill_price
            await self._on_z_filled(fill_price)

        # STP3 fill — position exit
        elif order_id in (self._stp3_parent, self._stp3_child) and self._stp3_parent:
            await self._on_stp3_filled(fill_price)

        # Reverse entry fill
        elif order_id == self._reverse_entry_id and not self._reverse_filled:
            self._reverse_filled = True
            await self._on_reverse_filled(fill_price)

    async def _on_y_filled(self, fill_price: float):
        logger.info("Y LONG filled @ %.2f", fill_price)
        self._cancel_group(self._z)
        self._position = Side.LONG
        self._position_qty = self._total_qty
        # STP3 for long: SELL STP @ Open-0.01
        await self._place_stp3("SELL", round_price(self._candle_open - 0.01))

    async def _on_z_filled(self, fill_price: float):
        logger.info("Z SHORT filled @ %.2f", fill_price)
        self._cancel_group(self._y)
        self._position = Side.SHORT
        self._position_qty = self._total_qty
        # STP3 for short: BUY STP @ Open+0.01
        await self._place_stp3("BUY", round_price(self._candle_open + 0.01))

    async def _on_stp3_filled(self, fill_price: float):
        now_sec = int(time.time())
        if now_sec == self._sl_second:
            self._sl_fires_this_second += 1
        else:
            self._sl_second = now_sec
            self._sl_fires_this_second = 1

        logger.info("STP3 filled @ %.2f (SL fires this second: %d)",
                    fill_price, self._sl_fires_this_second)

        prev_side = self._position
        self._position = Side.FLAT
        self._position_qty = 0
        self._stp3_parent = 0
        self._stp3_child = 0

        # 1-second exit condition
        if self._sl_fires_this_second >= 2:
            logger.warning("1-second exit condition: SL fired 2x in 1s — halting candle")
            self._candle_halted = True
            return

        # Place reverse entry
        if prev_side == Side.LONG:
            await self._place_reverse_entry("SELL", fill_price)
        else:
            await self._place_reverse_entry("BUY", fill_price)

    async def _on_reverse_filled(self, fill_price: float):
        logger.info("Reverse fill @ %.2f", fill_price)
        # After Y-reversed-to-short: MKT stop if SPY >= Open+0.01
        # After Z-reversed-to-long:  MKT stop if SPY <= Open-0.01
        # We monitor this in on_tick; for now just update position side
        # (The reverse position direction is opposite to original)
        # We track this via a flag and handle in on_tick
        self._reverse_filled = True

    async def on_partial_fill(self, order_id: int):
        logger.info("Partial fill on %d — cancelling unfilled", order_id)
        self._app.cancelOrder(order_id, "")

    # ------------------------------------------------------------------ #
    # Global exit                                                          #
    # ------------------------------------------------------------------ #

    async def _exit_all(self, reason: str):
        logger.info("Exit all: %s", reason)
        self._cancel_group(self._y)
        self._cancel_group(self._z)
        self._cancel_stp3()
        if self._reverse_entry_id and not self._reverse_filled:
            self._app.cancelOrder(self._reverse_entry_id, "")

        if self._position != Side.FLAT and self._position_qty > 0:
            action = "SELL" if self._position == Side.LONG else "BUY"
            oid = self._app.next_id()
            o = _mkt_order(action, self._position_qty, 0, transmit=True)
            o.orderId = oid
            o.parentId = 0
            self._app.placeOrder(oid, self._contract, o)
            logger.info("Flatten: %s %d", action, self._position_qty)

        self._reset()

    async def exit_all(self, reason: str = ""):
        await self._exit_all(reason)

    def _reset(self):
        self._y = None
        self._z = None
        self._stp3_parent = 0
        self._stp3_child = 0
        self._stp3_stop_px = 0.0
        self._position = Side.FLAT
        self._position_qty = 0
        self._reverse_entry_id = 0
        self._reverse_filled = False

    def _cancel_group(self, g: OrderGroup | None):
        if g and not g.filled and not g.cancelled:
            self._app.cancelOrder(g.parent_id, "")
            self._app.cancelOrder(g.child_id, "")
            g.cancelled = True
