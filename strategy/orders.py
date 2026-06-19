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


def stp(action: str, qty: int, stop_px: float, transmit: bool, parent_id: int = 0) -> Order:
    o = Order()
    o.action = action
    o.orderType = "STP"
    o.totalQuantity = qty
    o.auxPrice = round(round(stop_px / 0.01) * 0.01, 2)
    o.transmit = transmit
    o.tif = "DAY"
    if parent_id:
        o.parentId = parent_id
    return o


def mkt(action: str, qty: int, parent_id: int, transmit: bool) -> Order:
    o = Order()
    o.action = action
    o.orderType = "MKT"
    o.totalQuantity = qty
    o.parentId = parent_id
    o.transmit = transmit
    o.tif = "DAY"
    return o
