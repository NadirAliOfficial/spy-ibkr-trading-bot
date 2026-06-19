import asyncio
import logging
import threading
import time
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.common import TickerId, BarData
from ibapi.ticktype import TickTypeEnum

logger = logging.getLogger(__name__)


def make_spy_contract():
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

        # Producer: raw tick events -> market_data consumer
        self.tick_queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
        # Producer: order events (fills, status) -> order_manager consumer
        self.order_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)

        # One-shot futures keyed by reqId / orderId
        self._account_futures: dict = {}
        self._contract_futures: dict = {}
        self._margin_futures: dict = {}

        # Account state (updated via reqAccountSummary)
        self.equity_with_loan: float = 0.0
        self.spy_sell_init_margin: float = 0.0

        # Next valid order ID
        self._next_order_id: int = 1
        self._order_id_event = threading.Event()

    # -----------------------------------------------------------------------
    # Connection callbacks
    # -----------------------------------------------------------------------

    def connectAck(self):
        logger.info("Connection acknowledged")

    def nextValidId(self, orderId: int):
        self._next_order_id = orderId
        self._order_id_event.set()
        self._loop.call_soon_threadsafe(self.connected.set)
        logger.info("Connected — next valid order id: %d", orderId)

    def error(self, reqId: TickerId, errorCode: int, errorString: str, advancedOrderRejectJson=""):
        if errorCode in (2104, 2106, 2158, 2119):
            logger.debug("IBKR info [%d]: %s", errorCode, errorString)
        else:
            logger.error("IBKR error reqId=%d code=%d: %s", reqId, errorCode, errorString)
        event = {"type": "error", "reqId": reqId, "code": errorCode, "msg": errorString}
        self._loop.call_soon_threadsafe(self.order_queue.put_nowait, event)

    # -----------------------------------------------------------------------
    # Order ID management
    # -----------------------------------------------------------------------

    def next_id(self) -> int:
        oid = self._next_order_id
        self._next_order_id += 1
        return oid

    # -----------------------------------------------------------------------
    # Tick / market data callbacks  (-> tick_queue)
    # -----------------------------------------------------------------------

    def tickPrice(self, reqId: TickerId, tickType: int, price: float, attrib):
        if price <= 0:
            return
        event = {"type": "tick_price", "reqId": reqId, "tickType": tickType, "price": price,
                 "ts": time.time()}
        self._loop.call_soon_threadsafe(self.tick_queue.put_nowait, event)

    def tickSize(self, reqId: TickerId, tickType: int, size):
        pass  # not needed for strategy

    # -----------------------------------------------------------------------
    # Account summary callbacks
    # -----------------------------------------------------------------------

    def accountSummary(self, reqId: int, account: str, tag: str, value: str, currency: str):
        if tag == "EquityWithLoanValue":
            self.equity_with_loan = float(value)
            logger.info("EquityWithLoanValue: %.2f", self.equity_with_loan)
        if tag == "SellInitMarginReq" and currency == "USD":
            self.spy_sell_init_margin = float(value)
            logger.info("SellInitMarginReq: %.2f", self.spy_sell_init_margin)

    def accountSummaryEnd(self, reqId: int):
        fut = self._account_futures.pop(reqId, None)
        if fut and not fut.done():
            self._loop.call_soon_threadsafe(fut.set_result, True)

    async def fetch_account_summary(self) -> tuple[float, float]:
        req_id = self.next_id()
        fut: asyncio.Future = self._loop.create_future()
        self._account_futures[req_id] = fut
        self.reqAccountSummary(req_id, "All", "EquityWithLoanValue,SellInitMarginReq")
        await asyncio.wait_for(fut, timeout=15)
        return self.equity_with_loan, self.spy_sell_init_margin

    # -----------------------------------------------------------------------
    # Contract / trading hours
    # -----------------------------------------------------------------------

    def contractDetails(self, reqId: int, contractDetails):
        fut = self._contract_futures.get(reqId)
        if fut:
            self._loop.call_soon_threadsafe(fut.set_result, contractDetails)

    def contractDetailsEnd(self, reqId: int):
        pass

    async def fetch_trading_hours(self) -> str:
        req_id = self.next_id()
        fut: asyncio.Future = self._loop.create_future()
        self._contract_futures[req_id] = fut
        self.reqContractDetails(req_id, make_spy_contract())
        details = await asyncio.wait_for(fut, timeout=15)
        self._contract_futures.pop(req_id, None)
        return details.tradingHours  # e.g. "20240101:0930-20240101:1600;..."

    # -----------------------------------------------------------------------
    # Order status callbacks  (-> order_queue)
    # -----------------------------------------------------------------------

    def orderStatus(self, orderId: int, status: str, filled: float,
                    remaining: float, avgFillPrice: float, permId: int,
                    parentId: int, lastFillPrice: float, clientId: int,
                    whyHeld: str, mktCapPrice: float):
        event = {
            "type": "order_status",
            "orderId": orderId,
            "status": status,
            "filled": filled,
            "remaining": remaining,
            "avgFillPrice": avgFillPrice,
        }
        self._loop.call_soon_threadsafe(self.order_queue.put_nowait, event)

    def execDetails(self, reqId: int, contract, execution):
        event = {
            "type": "exec",
            "orderId": execution.orderId,
            "side": execution.side,
            "shares": execution.shares,
            "price": execution.price,
            "execId": execution.execId,
        }
        self._loop.call_soon_threadsafe(self.order_queue.put_nowait, event)

    # -----------------------------------------------------------------------
    # PnL
    # -----------------------------------------------------------------------

    def pnlSingle(self, reqId: int, pos: int, dailyPnL: float,
                  unrealizedPnL: float, realizedPnL: float, value: float):
        event = {
            "type": "pnl",
            "pos": pos,
            "dailyPnL": dailyPnL,
            "unrealizedPnL": unrealizedPnL,
            "realizedPnL": realizedPnL,
        }
        self._loop.call_soon_threadsafe(self.order_queue.put_nowait, event)

    # -----------------------------------------------------------------------
    # Runner
    # -----------------------------------------------------------------------

    def run_in_thread(self):
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()
        return thread


def connect(host: str, port: int, client_id: int, loop: asyncio.AbstractEventLoop) -> IBApp:
    app = IBApp(loop)
    app.connect(host, port, client_id)
    app.run_in_thread()
    return app
