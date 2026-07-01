import asyncio
import logging
import threading
import time

from ibapi.client import EClient
from ibapi.common import TickerId
from ibapi.contract import Contract
from ibapi.wrapper import EWrapper

import config

logger = logging.getLogger(__name__)


def spy_contract() -> Contract:
    c = Contract()
    c.symbol = "SPY"
    c.secType = "STK"
    c.exchange = "SMART"
    c.currency = "USD"
    return c


class IBApp(EWrapper, EClient):
    def __init__(self, loop: asyncio.AbstractEventLoop):
        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)
        self._loop = loop

        self.connected = asyncio.Event()
        self.account: str = ""

        self.tick_queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self.order_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)

        self._account_futures: dict[int, asyncio.Future] = {}
        self._contract_futures: dict[int, asyncio.Future] = {}
        self._whatif_futures: dict[int, asyncio.Future] = {}
        self._positions_future: asyncio.Future | None = None
        self._positions: list[dict] = []
        self._open_orders_future: asyncio.Future | None = None
        self._open_spy_order_ids: list[int] = []
        self._collecting_open_orders: bool = False

        self.equity_with_loan: float = 0.0
        self.prev_day_elv: float = 0.0
        self.sell_init_margin: float = 0.0
        self.last_price: float = 0.0
        self._next_order_id: int = 1
        self._tbt_active: bool = False

    def next_id(self) -> int:
        oid = self._next_order_id
        self._next_order_id += 1
        return oid

    def _enqueue(self, queue: asyncio.Queue, event: dict):
        def _put():
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass
        self._loop.call_soon_threadsafe(_put)

    # --- Connection ---

    def managedAccounts(self, accountsList: str):
        accts = [a.strip() for a in accountsList.split(",") if a.strip()]
        if config.ACCOUNT_ID and config.ACCOUNT_ID in accts:
            self.account = config.ACCOUNT_ID
        else:
            self.account = accts[0] if accts else ""
        logger.info("Account: %s  (available: %s)", self.account, ",".join(accts))

    def nextValidId(self, orderId: int):
        self._next_order_id = orderId
        self._loop.call_soon_threadsafe(self.connected.set)
        logger.info("Connected — next order id: %d", orderId)

    def error(self, reqId: TickerId, errorCode: int, errorString: str, advancedOrderRejectJson=""):
        if errorCode in (2104, 2106, 2119, 2158):
            logger.debug("[%d] %s", errorCode, errorString)
            return
        logger.error("reqId=%d code=%d: %s", reqId, errorCode, errorString)
        self._enqueue(self.order_queue, {"type": "error", "reqId": reqId, "code": errorCode})

    # --- Market data ---

    def tickPrice(self, reqId: TickerId, tickType: int, price: float, attrib):
        if price > 0:
            if tickType in (4, 68):  # LAST / DELAYED_LAST
                self.last_price = price
                if self._tbt_active:
                    return  # Last prices come from reqTickByTickData
            self._enqueue(self.tick_queue, {
                "type": "tick_price", "tickType": tickType,
                "price": price, "ts": time.time(),
            })

    def tickByTickAllLast(self, reqId: int, tickType: int, time_: int, price: float,
                          size, tickAttribLast, exchange: str, specialConditions: str):
        if price > 0:
            self.last_price = price
            self._enqueue(self.tick_queue, {
                "type": "tick_price", "tickType": config.TICK_LAST,
                "price": price, "ts": float(time_),
            })

    # --- Account summary ---

    def accountSummary(self, reqId: int, account: str, tag: str, value: str, currency: str):
        if self.account and account != self.account:
            return
        if tag == "EquityWithLoanValue":
            self.equity_with_loan = float(value)
        elif tag == "PreviousDayEquityWithLoanValue":
            self.prev_day_elv = float(value)
        elif tag == "SellInitMarginReq" and currency == "USD":
            self.sell_init_margin = float(value)

    def updateAccountValue(self, key: str, val: str, currency: str, accountName: str):
        if self.account and accountName != self.account:
            return
        try:
            if key == "EquityWithLoanValue":
                self.equity_with_loan = float(val)
            elif key == "PreviousDayEquityWithLoanValue":
                self.prev_day_elv = float(val)
        except ValueError:
            pass

    def accountSummaryEnd(self, reqId: int):
        fut = self._account_futures.pop(reqId, None)
        if fut and not fut.done():
            self._loop.call_soon_threadsafe(fut.set_result, True)

    async def fetch_account_summary(self) -> tuple[float, float]:
        req_id = self.next_id()
        fut = self._loop.create_future()
        self._account_futures[req_id] = fut
        self.reqAccountSummary(req_id, "All", "EquityWithLoanValue,PreviousDayEquityWithLoanValue,SellInitMarginReq")
        await asyncio.wait_for(fut, timeout=15)
        return self.equity_with_loan, self.sell_init_margin

    # --- Trading hours ---

    def contractDetails(self, reqId: int, contractDetails):
        fut = self._contract_futures.get(reqId)
        if fut and not fut.done():
            self._loop.call_soon_threadsafe(fut.set_result, contractDetails)

    def contractDetailsEnd(self, reqId: int):
        self._contract_futures.pop(reqId, None)

    async def fetch_trading_hours(self) -> str:
        req_id = self.next_id()
        fut = self._loop.create_future()
        self._contract_futures[req_id] = fut
        self.reqContractDetails(req_id, spy_contract())
        details = await asyncio.wait_for(fut, timeout=15)
        return details.tradingHours

    # --- Session cleanup ---

    def position(self, account: str, contract, pos: float, avgCost: float):
        if contract.symbol == "SPY" and pos != 0:
            self._positions.append({"pos": pos, "avgCost": avgCost})

    def positionEnd(self):
        if self._positions_future and not self._positions_future.done():
            self._loop.call_soon_threadsafe(self._positions_future.set_result, self._positions[:])

    async def cancel_spy_orders(self):
        """Cancel only open SPY orders — never global, so other projects on the
        account are untouched."""
        self._open_spy_order_ids = []
        self._collecting_open_orders = True
        self._open_orders_future = self._loop.create_future()
        self.reqAllOpenOrders()
        try:
            await asyncio.wait_for(self._open_orders_future, timeout=10)
        except asyncio.TimeoutError:
            pass
        self._collecting_open_orders = False
        for oid in self._open_spy_order_ids:
            self.cancelOrder(oid)
        logger.info("Cancelled %d open SPY orders", len(self._open_spy_order_ids))

    async def clean_slate(self):
        await self.cancel_spy_orders()

        self._positions = []
        self._positions_future = self._loop.create_future()
        self.reqPositions()
        try:
            positions = await asyncio.wait_for(self._positions_future, timeout=10)
        except asyncio.TimeoutError:
            positions = []

        for p in positions:
            action = "SELL" if p["pos"] > 0 else "BUY"
            qty = abs(int(p["pos"]))
            oid = self.next_id()
            from ibapi.order import Order
            o = Order()
            o.action = action
            o.totalQuantity = qty
            o.orderType = "MKT"
            o.eTradeOnly = False
            o.firmQuoteOnly = False
            o.transmit = True
            if self.account:
                o.account = self.account
            self.placeOrder(oid, spy_contract(), o)
            logger.info("Clean slate: %s %d SPY @ MKT", action, qty)

        self.cancelPositions()

    # --- Orders ---

    def orderStatus(self, orderId: int, status: str, filled: float, remaining: float,
                    avgFillPrice: float, permId: int, parentId: int, lastFillPrice: float,
                    clientId: int, whyHeld: str, mktCapPrice: float):
        self._enqueue(self.order_queue, {
            "type": "order_status", "orderId": orderId, "status": status,
            "filled": filled, "remaining": remaining, "avgFillPrice": avgFillPrice,
        })

    def openOrder(self, orderId: int, contract, order, orderState):
        fut = self._whatif_futures.pop(orderId, None)
        if fut and not fut.done():
            try:
                init_margin = float(orderState.initMarginChange)
            except (ValueError, TypeError):
                init_margin = 0.0
            self._loop.call_soon_threadsafe(fut.set_result, init_margin)
            return
        if self._collecting_open_orders and contract.symbol == "SPY":
            self._open_spy_order_ids.append(orderId)

    def openOrderEnd(self):
        if self._open_orders_future and not self._open_orders_future.done():
            self._loop.call_soon_threadsafe(self._open_orders_future.set_result, True)

    async def _fetch_margin_per_share(self, action: str, qty: int) -> float:
        from ibapi.order import Order
        oid = self.next_id()
        o = Order()
        o.action = action
        o.orderType = "MKT"
        o.totalQuantity = qty
        o.whatIf = True
        o.eTradeOnly = False
        o.firmQuoteOnly = False
        if self.account:
            o.account = self.account
        fut = self._loop.create_future()
        self._whatif_futures[oid] = fut
        self.placeOrder(oid, spy_contract(), o)
        try:
            init_margin = await asyncio.wait_for(fut, timeout=10)
        except asyncio.TimeoutError:
            self._whatif_futures.pop(oid, None)
            return 0.0
        return init_margin / qty if qty else 0.0

    async def fetch_short_margin_per_share(self, qty: int = 100) -> float:
        return await self._fetch_margin_per_share("SELL", qty)

    async def fetch_long_margin_per_share(self, qty: int = 100) -> float:
        return await self._fetch_margin_per_share("BUY", qty)

    def execDetails(self, reqId: int, contract, execution):
        self._enqueue(self.order_queue, {
            "type": "exec", "orderId": execution.orderId,
            "shares": execution.shares, "price": execution.price,
        })

    # --- PnL (account-level) ---

    def pnl(self, reqId: int, dailyPnL: float, unrealizedPnL: float, realizedPnL: float):
        self._enqueue(self.order_queue, {
            "type": "pnl", "dailyPnL": dailyPnL,
            "unrealizedPnL": unrealizedPnL, "realizedPnL": realizedPnL,
        })

    def run_in_thread(self):
        threading.Thread(target=self.run, daemon=True).start()


def connect(host: str, port: int, client_id: int,
            loop: asyncio.AbstractEventLoop) -> IBApp:
    app = IBApp(loop)
    app.connect(host, port, client_id)
    app.run_in_thread()
    return app
