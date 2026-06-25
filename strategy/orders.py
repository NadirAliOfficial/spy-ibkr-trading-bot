from dataclasses import dataclass
from enum import Enum, auto

from ibapi.order import Order


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


_ACCOUNT = ""


def set_account(account: str):
    """Target account for all orders — required when the login has >1 account."""
    global _ACCOUNT
    _ACCOUNT = account


def _base(action: str, qty: int, transmit: bool) -> Order:
    o = Order()
    o.action = action
    o.totalQuantity = qty
    o.transmit = transmit
    o.tif = "DAY"
    o.eTradeOnly = False
    o.firmQuoteOnly = False
    if _ACCOUNT:
        o.account = _ACCOUNT
    return o


def stp(action: str, qty: int, stop_px: float, transmit: bool, parent_id: int = 0) -> Order:
    o = _base(action, qty, transmit)
    o.orderType = "STP"
    o.auxPrice = round(round(stop_px / 0.01) * 0.01, 2)
    if parent_id:
        o.parentId = parent_id
    return o


def mkt(action: str, qty: int, parent_id: int, transmit: bool) -> Order:
    o = _base(action, qty, transmit)
    o.orderType = "MKT"
    o.parentId = parent_id
    return o
