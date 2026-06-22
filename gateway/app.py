import asyncio
import logging
import threading
import time

from ibapi.client import EClient
from ibapi.common import TickerId
from ibapi.contract import Contract
from ibapi.wrapper import EWrapper

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

        self.equity_with_loan: float = 0.0
        self.sell_init_margin: float = 0.0
        self._next_order_id: int = 1

    def next_id(self) -> int:
        oid = self._next_order_id
        self._next_order_id += 1
        return oid

    def _enqueue(self, queue: asyncio.Queue, event: dict):
        self._loop.call_soon_threadsafe(queue.put_nowait, event)

    # --- Connection ---

    def managedAccounts(self, accountsList: str):
        self.account = accountsList.split(",")[0].strip()
        logger.info("Account: %s", self.account)

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
            self._enqueue(self.tick_queue, {
                "type": "tick_price", "tickType": tickType,
                "price": price, "ts": time.time(),
            })

    # --- Account summary ---

    def accountSummary(self, reqId: int, account: str, tag: str, value: str, currency: str):
        if tag == "EquityWithLoanValue":
            self.equity_with_loan = float(value)
        elif tag == "SellInitMarginReq" and currency == "USD":
            self.sell_init_margin = float(value)

    def updateAccountValue(self, key: str, val: str, currency: str, accountName: str):
        if key == "EquityWithLoanValue":
            try:
                self.equity_with_loan = float(val)
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
        self.reqAccountSummary(req_id, "All", "EquityWithLoanValue,SellInitMarginReq")
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

    async def clean_slate(self):
        self.reqGlobalCancel()
        logger.info("Global cancel sent")

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
            o.transmit = True
            self.placeOrder(oid, spy_contract(), o)
            logger.info("Clean slate: %s %d SPY @ MKT", action, qty)

        self.cancelPositions()

    # --- whatIf margin probe ---

    def openOrder(self, orderId: int, contract, order, orderState):
        fut = self._whatif_futures.pop(orderId, None)
        if fut and not fut.done():
            self._loop.call_soon_threadsafe(fut.set_result, orderState)

    async def fetch_spy_margin(self) -> float:
        from ibapi.order import Order
        req_id = self.next_id()
        fut = self._loop.create_future()
        self._whatif_futures[req_id] = fut
        o = Order()
        o.action = "SELL"
        o.totalQuantity = 1
        o.orderType = "MKT"
        o.whatIf = True
        self.placeOrder(req_id, spy_contract(), o)
        try:
            state = await asyncio.wait_for(fut, timeout=15)
            margin = abs(float(state.initMarginChange or 0))
            logger.info("SPY sell margin/share: %.2f", margin)
            return margin if margin > 10 else 0.0
        except asyncio.TimeoutError:
            logger.warning("whatIf margin probe timed out")
            return 0.0

    # --- Orders ---

    def orderStatus(self, orderId: int, status: str, filled: float, remaining: float,
                    avgFillPrice: float, permId: int, parentId: int, lastFillPrice: float,
                    clientId: int, whyHeld: str, mktCapPrice: float):
        self._enqueue(self.order_queue, {
            "type": "order_status", "orderId": orderId, "status": status,
            "filled": filled, "remaining": remaining, "avgFillPrice": avgFillPrice,
        })

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
