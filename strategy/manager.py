import asyncio
import logging
import time

import config
from gateway import spy_contract
from strategy.orders import Side, OrderGroup, stp, mkt
from utils import calc_leg_qty

logger = logging.getLogger(__name__)

CONTRACT = spy_contract()
_rp = lambda p: round(round(p / 0.01) * 0.01, 2)


class OrderManager:
    def __init__(self, app, leg_qty: int, margin_per_share: float = 0.0, margin_pct: float = 1.6):
        self._app = app
        self._leg = leg_qty          # total position size (ELV-2%/margin)
        self._margin = margin_per_share
        self._margin_pct = margin_pct   # short SPY margin as fraction of price

        self._open: float = 0.0
        self._entries: int = 0          # position-opens this candle (cap 5)
        self._halted: bool = False
        self._pending: bool = False     # Y/Z placed, awaiting fill

        self._y: OrderGroup | None = None
        self._z: OrderGroup | None = None

        self._s3_pid: int = 0
        self._s3_cid: int = 0
        self._s3_px: float = 0.0
        self._s3_qty: int = 0
        self._s3_reverse: bool = False
        self._placing_stp3: bool = False
        self._s3_cancel_ts: float = 0.0

        self._pos: Side = Side.FLAT
        self._pos_qty: int = 0
        self._entry_px: float = 0.0
        self._bot_realized: float = 0.0   # strategy's own realized P&L (this session)

        self._exit_orders: dict[int, tuple] = {}  # oid → (side, entry_px, reason)
        self._close_order_ids: set[int] = set()  # MKT close orders — 201 here should not halt bot

        self._sl_count: int = 0
        self._sl_sec: int = -1
        self._tp_count: int = 0         # take-profit exits this candle (cap 2)

        self.total_bought: int = 0
        self.total_sold: int = 0
        self._slippage: list[float] = []

        self.last_bid: float = 0.0
        self.last_ask: float = 0.0

    @property
    def mean_slippage(self) -> float | None:
        return round(sum(self._slippage) / len(self._slippage), 4) if self._slippage else None

    @property
    def total_executed_orders(self) -> int:
        return len(self._slippage)

    @property
    def total_slippage(self) -> float:
        return round(sum(self._slippage), 4)

    # ── Candle lifecycle ──────────────────────────────────────────────────

    async def on_candle_open(self, open_price: float):
        self._open = open_price
        self._entries = 0
        self._halted = False
        self._pending = False
        self._sl_count = 0
        self._sl_sec = -1
        self._tp_count = 0
        logger.info("Candle open: %.2f", open_price)
        if self._pos == Side.FLAT:
            await self._place_yz()

    async def on_59th_second(self):
        await self.exit_all("59s timer")
        self._recalc_qty()  # size next candle's orders (spec: at 59th second)

    def _recalc_qty(self):
        price = (self.last_bid + self.last_ask) / 2 if self.last_bid > 0 and self.last_ask > 0 else self._open
        elv = self._app.equity_with_loan
        prev_day_elv = self._app.prev_day_elv
        if elv > 0 and price > 10:
            sizing_elv = min(prev_day_elv, elv) if prev_day_elv > 0 else elv
            self._margin = round(price * self._margin_pct, 2)
            new_leg = calc_leg_qty(sizing_elv, self._margin)
            if new_leg != self._leg:
                logger.info("Qty recalc @59s: leg %d->%d (sizingELV=%.2f margin=%.2f)",
                            self._leg, new_leg, sizing_elv, self._margin)
            self._leg = new_leg

    async def on_tick(self, price: float, sim_hits: int):
        if self._halted:
            return
        if self._pos == Side.LONG:
            await self._manage_long(price)
        elif self._pos == Side.SHORT:
            await self._manage_short(price)
        elif (self._pos == Side.FLAT and
              not self._pending and
              self._entries < config.MAX_ENTRIES_PER_CANDLE and
              self._tp_count < 2):
            await self._place_yz()

    # ── Fill routing ──────────────────────────────────────────────────────

    async def on_fill(self, order_id: int, fill_price: float, fill_qty: int = 0):
        if order_id in self._exit_orders:
            side, entry, qty, reason = self._exit_orders.pop(order_id)
            self._log_exec(side, entry, fill_price, reason, qty)
            self._pos = Side.FLAT
            self._pos_qty = 0
            return

        # Y parent first fill — starts LONG position
        if self._y and not self._y.cancelled and not self._y.parent_filled and order_id == self._y.parent_id:
            self._y.parent_filled = True
            self._y.entry_price = fill_price
            qty = max(1, int(fill_qty)) if fill_qty else 1
            await self._on_y_parent_filled(fill_price, qty)
            return

        # Y parent subsequent partial fills (same STOP order filling in parts)
        if self._y and self._y.parent_filled and not self._y.filled and order_id == self._y.parent_id:
            qty = max(1, int(fill_qty)) if fill_qty else 1
            self._pos_qty += qty
            self.total_bought += qty
            logger.info("Y LONG fill +%d shares → total pos=%d", qty, self._pos_qty)
            if self._pos_qty >= self._leg:
                self._y.filled = True
            return

        # Z parent first fill — starts SHORT position
        if self._z and not self._z.cancelled and not self._z.parent_filled and order_id == self._z.parent_id:
            self._z.parent_filled = True
            self._z.entry_price = fill_price
            qty = max(1, int(fill_qty)) if fill_qty else 1
            await self._on_z_parent_filled(fill_price, qty)
            return

        # Z parent subsequent partial fills
        if self._z and self._z.parent_filled and not self._z.filled and order_id == self._z.parent_id:
            qty = max(1, int(fill_qty)) if fill_qty else 1
            self._pos_qty += qty
            self.total_sold += qty
            logger.info("Z SHORT fill +%d shares → total pos=%d", qty, self._pos_qty)
            if self._pos_qty >= self._leg:
                self._z.filled = True
            return

        if self._s3_pid and order_id == self._s3_pid:
            qty = max(1, int(fill_qty)) if fill_qty else 1
            await self._on_stp3_filled(fill_price, qty)

    async def on_partial_fill(self, order_id: int):
        pass  # Y/Z and STP3 use single STOP orders — partial fills are fine, let them complete

    def is_close_order(self, order_id: int) -> bool:
        return order_id in self._close_order_ids

    def on_reverse_rejected(self, order_id: int):
        pass

    def _register_sl(self) -> bool:
        """Count a stop-loss trigger; return True if 2+ within the same second."""
        sec = int(time.time())
        self._sl_count = self._sl_count + 1 if sec == self._sl_sec else 1
        self._sl_sec = sec
        return self._sl_count >= 2

    def _log_exec(self, side: Side, entry: float, exit_px: float, reason: str, qty: int = 0):
        pnl_sh = (exit_px - entry) if side == Side.LONG else (entry - exit_px)
        trade_pnl = round(pnl_sh * (qty or self._pos_qty), 2)
        self._bot_realized = round(self._bot_realized + trade_pnl, 2)
        kind = "TAKE PROFIT" if pnl_sh >= 0 else "STOP LOSS"
        if pnl_sh >= 0:
            self._tp_count += 1
            if self._tp_count >= 2:
                logger.info("TP limit 2/candle reached — no re-entry this candle")
        logger.info("EXEC %s | open=%.2f fill=%.2f exit=%.2f | %s (%s) | trade=%.2f botPnL=%.2f",
                    side.name, self._open, entry, exit_px, kind, reason, trade_pnl, self._bot_realized)

    # ── Entry fills ───────────────────────────────────────────────────────

    async def _on_y_parent_filled(self, fill_price: float, fill_qty: int = 1):
        self._cancel_group(self._z)
        self._pos = Side.LONG
        self._pos_qty = fill_qty
        self._entry_px = fill_price  # actual fill price — slippage naturally in strategy PnL
        self._pending = False
        self._entries += 1
        self.total_bought += fill_qty
        self._slippage.append(abs(fill_price - _rp(self._open + 0.01)) * self._leg)
        if self._pos_qty >= self._leg:
            self._y.filled = True
        logger.info("Y LONG filled %d shares @ %.2f (entry#%d)", fill_qty, fill_price, self._entries)

    async def _on_z_parent_filled(self, fill_price: float, fill_qty: int = 1):
        self._cancel_group(self._y)
        self._pos = Side.SHORT
        self._pos_qty = fill_qty
        self._entry_px = fill_price  # actual fill price — slippage naturally in strategy PnL
        self._pending = False
        self._entries += 1
        self.total_sold += fill_qty
        self._slippage.append(abs(fill_price - _rp(self._open - 0.01)) * self._leg)
        if self._pos_qty >= self._leg:
            self._z.filled = True
        logger.info("Z SHORT filled %d shares @ %.2f (entry#%d)", fill_qty, fill_price, self._entries)

    # ── STP3 / reverse logic ──────────────────────────────────────────────

    async def _on_stp3_filled(self, fill_price: float, fill_qty: int = 1):
        halt_1s = self._register_sl()

        was_long = self._pos == Side.LONG
        is_reverse = self._s3_reverse
        old_qty = self._pos_qty
        s3_px = self._s3_px  # save before clearing — used for PnL and reverse entry_px

        self._log_exec(Side.LONG if was_long else Side.SHORT, self._entry_px, fill_price, "STP3")
        self._slippage.append(abs(fill_price - s3_px) * self._leg)
        if was_long:
            self.total_sold += fill_qty
        else:
            self.total_bought += fill_qty
        self._pos, self._pos_qty = Side.FLAT, 0
        self._s3_pid = self._s3_cid = 0
        self._s3_px = 0.0
        self._s3_qty = 0
        self._s3_reverse = False
        logger.info("STP3 @ %.2f qty=%d (SL/s: %d, reverse=%s)", fill_price, fill_qty, self._sl_count, is_reverse)

        if halt_1s:
            logger.warning("1-second exit: SL fired 2x — halting candle")
            self._halted = True
            if is_reverse:
                action = "BUY" if was_long else "SELL"
                oid = self._app.next_id()
                o = mkt(action, old_qty, 0, transmit=True)
                o.orderId = oid
                self._app.placeOrder(oid, CONTRACT, o)
                if action == "BUY":
                    self.total_bought += old_qty
                else:
                    self.total_sold += old_qty
                logger.info("1s halt flatten: %s %d", action, old_qty)
            return

        if not is_reverse:
            logger.info("Exit only (no reverse) — candle halted")
            self._halted = True
            return

        # Reverse fired: opens the opposite position
        new_side = Side.SHORT if was_long else Side.LONG
        self._pos_qty = old_qty
        self._entry_px = fill_price  # actual fill price for the reversed position
        self._entries += 1
        logger.info("Reverse entered: now %s %d (entry#%d)", new_side.name, self._pos_qty, self._entries)
        self._pos = new_side

    # ── STP3 position management (primary entries Y / Y2 reverse) ──────────

    async def _manage_long(self, price: float):
        favor, arm = _rp(self._open + 0.01), _rp(self._open - 0.01)
        if price >= favor:
            if self._s3_pid:
                self._cancel_stp3()                  # recovered — cancel stop
        elif price <= arm and not self._s3_pid and not self._placing_stp3:
            if time.time() - self._s3_cancel_ts < 0.5:  # wait 500ms after cancel before re-arming
                return
            stop = _rp(price - 0.03)                 # NADIR11: use Last price, not bid
            logger.info("STP3 arm LONG: SPY=%.2f<=Open-0.01=%.2f  stop=last-0.03=%.2f",
                        price, arm, stop)
            await self._place_stp3("SELL", stop, reverse=self._entries < config.MAX_ENTRIES_PER_CANDLE)

    async def _manage_short(self, price: float):
        favor, arm = _rp(self._open - 0.01), _rp(self._open + 0.01)
        if price <= favor:
            if self._s3_pid:
                self._cancel_stp3()                  # recovered — cancel stop
        elif price >= arm and not self._s3_pid and not self._placing_stp3:
            if time.time() - self._s3_cancel_ts < 0.5:
                return
            stop = _rp(price + 0.03)                 # NADIR11: use Last price, not ask
            logger.info("STP3 arm SHORT: SPY=%.2f>=Open+0.01=%.2f  stop=last+0.03=%.2f",
                        price, arm, stop)
            await self._place_stp3("BUY", stop, reverse=self._entries < config.MAX_ENTRIES_PER_CANDLE)

    # ── Order placement ───────────────────────────────────────────────────

    async def _place_yz(self):
        if self._entries >= config.MAX_ENTRIES_PER_CANDLE or self._halted or self._tp_count >= 2:
            return

        if self._entries > 0:
            await asyncio.sleep(0.5)
            if self._halted or self._entries >= config.MAX_ENTRIES_PER_CANDLE:
                return

        y_pid = self._app.next_id()
        z_pid = self._app.next_id()
        buy_px, sell_px = _rp(self._open + 0.01), _rp(self._open - 0.01)

        yp = stp("BUY",  self._leg, buy_px,  transmit=True);  yp.orderId = y_pid
        zp = stp("SELL", self._leg, sell_px, transmit=True);  zp.orderId = z_pid

        self._app.placeOrder(y_pid, CONTRACT, yp)
        self._app.placeOrder(z_pid, CONTRACT, zp)

        self._y = OrderGroup(y_pid, 0, Side.LONG,  self._leg, entry_price=buy_px)
        self._z = OrderGroup(z_pid, 0, Side.SHORT, self._leg, entry_price=sell_px)
        self._pending = True
        logger.info("Y/Z placed: BUY=%.2f SELL=%.2f (entries so far=%d)", buy_px, sell_px, self._entries)

    async def _place_stp3(self, action: str, stop_px: float, reverse: bool = False):
        self._placing_stp3 = True
        pid = self._app.next_id()
        qty = (2 * self._pos_qty) if reverse else self._pos_qty
        p = stp(action, qty, stop_px, transmit=True); p.orderId = pid
        self._app.placeOrder(pid, CONTRACT, p)
        self._s3_pid = pid
        self._s3_cid = 0
        self._s3_px = stop_px
        self._s3_qty = qty
        self._s3_reverse = reverse
        self._placing_stp3 = False
        logger.info("STP3: %s @ %.2f qty=%d reverse=%s", action, stop_px, qty, reverse)

    def _cancel_stp3(self):
        for oid in (self._s3_pid, self._s3_cid):
            if oid:
                self._app.cancelOrder(oid)
        self._s3_pid = self._s3_cid = 0
        self._s3_cancel_ts = time.time()
        self._s3_px = 0.0
        self._s3_reverse = False
        self._placing_stp3 = False

    # ── Global exit ───────────────────────────────────────────────────────

    async def exit_all(self, reason: str = ""):
        self._halted = True  # block concurrent on_tick from re-entering
        logger.info("Exit all: %s", reason)
        self._cancel_group(self._y)
        self._cancel_group(self._z)
        self._cancel_stp3()

        if self._pos != Side.FLAT and self._pos_qty > 0:
            action = "SELL" if self._pos == Side.LONG else "BUY"
            oid = self._app.next_id()
            o = mkt(action, self._pos_qty, 0, transmit=True)
            o.orderId = oid
            self._app.placeOrder(oid, CONTRACT, o)
            self._exit_orders[oid] = (self._pos, self._entry_px, self._pos_qty, reason or "exit all")
            self._close_order_ids.add(oid)
            if action == "SELL":
                self.total_sold += self._pos_qty
            else:
                self.total_bought += self._pos_qty
            logger.info("Flatten pos: %s %d (pending fill for real PnL)", action, self._pos_qty)

        self._y = self._z = None
        self._pending = False
        self._s3_pid = self._s3_cid = 0
        self._s3_px = 0.0
        self._s3_reverse = False
        self._pos, self._pos_qty = Side.FLAT, 0

    def _cancel_group(self, g: OrderGroup | None):
        if g and not g.filled and not g.cancelled:
            self._app.cancelOrder(g.parent_id)
            if g.child_id:
                self._app.cancelOrder(g.child_id)
            g.cancelled = True
