"""Standalone Taishin Core boundary. No SDK import, login or network in this phase."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Mapping

class MappingError(ValueError):
    pass

def integer(value, name, minimum=0):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        raise MappingError(name + ": expected unsigned integer")
    result = int(value)
    if result < minimum:
        raise MappingError(name + ": below minimum")
    return result

@dataclass(frozen=True)
class TaishinOrder:
    account: str
    trade_date: str
    symbol: str
    side: str
    quantity_shares: int
    price_type: str
    price: str
    time_in_force: str
    day_trade_flag: str
    intent_id: str

    def sdk_fields(self):
        # Enum names are resolved by the future isolated SDK worker.
        return dict(tradingSession="NORMAL", side=self.side, symbol=self.symbol,
                    priceType=self.price_type, price=self.price,
                    tradingUnit="COMMON", qty=str(self.quantity_shares),
                    orderType=self.time_in_force, tradingType="CUSTODY",
                    dayTradeFlag=self.day_trade_flag, tradingAccount=self.account,
                    userDef=self.intent_id)

    def public_preview(self):
        fields = self.sdk_fields()
        fields["tradingAccount"] = "<selected account>"
        return dict(broker="TAISHIN_CORE", trade_date=self.trade_date,
                    quantity_unit="shares", sdk_fields=fields,
                    broker_submit_attempt=False, live_enabled=False)

def from_cts_request(request, *, day_trade_flag):
    r = request.to_dict() if hasattr(request, "to_dict") else dict(request)
    # Only ordinary board-lot cash orders are understood. Never guess legacy codes.
    for field in ("trade_type", "order_type", "pay_type"):
        if integer(r.get(field, 0), field) != 0:
            raise MappingError("unsupported " + field)
    if str(r.get("broker") or ""):
        raise MappingError("broker routing is unsupported")
    if integer(r.get("price_type"), "price_type") != 4:
        raise MappingError("phase 1 only maps upstream market orders")
    side = {1: "Buy", 2: "Sell"}.get(integer(r.get("side"), "side"))
    tif = {0: "ROD", 1: "FOK", 2: "IOC"}.get(integer(r.get("condition", 0), "condition"))
    if side is None or tif is None:
        raise MappingError("unsupported side or condition")
    if day_trade_flag not in {"N", "Y"}:
        raise MappingError("explicit N/Y day-trade flag required")
    symbol = str(r.get("symbol") or "")
    account = str(r.get("account_id") or "")
    identity = str(r.get("order_intent_id") or r.get("registry_key") or "")
    if not re.fullmatch(r"[A-Za-z0-9]{4,8}", symbol) or not account.strip():
        raise MappingError("invalid symbol/account")
    # Keep a stable unique upstream intent; never truncate identities.
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", identity):
        raise MappingError("intent must be 1..32 ASCII letters/digits/_/-")
    day = str(r.get("trade_date") or "")
    try:
        if len(day) != 8:
            raise ValueError()
        datetime.strptime(day, "%Y%m%d")
        price = Decimal(str(r.get("price")))
        if not price.is_finite() or price != 0:
            raise ValueError()
    except (ValueError, InvalidOperation):
        raise MappingError("invalid trade date or market price") from None
    shares = integer(r.get("quantity"), "quantity", 1) * 1000
    return TaishinOrder(account, day, symbol, side, shares, "MKT", "0", tif,
                        day_trade_flag, identity)

@dataclass(frozen=True)
class OrderReport:
    account: str
    trade_date: str
    order_no: str
    symbol: str
    side: str
    intent_id: str
    requested_shares: int
    filled_shares: int
    remaining_shares: int
    state: str
    broker_status: str

def normalize_report(row: Mapping, order: TaishinOrder):
    # This accepts a normalized worker message, not arbitrary SDK object strings.
    required = dict(account=order.account, trade_date=order.trade_date,
                    symbol=order.symbol, side=order.side, intent_id=order.intent_id)
    if any(str(row.get(k, "")) != str(v) for k, v in required.items()):
        raise MappingError("report identity mismatch")
    no = str(row.get("order_no") or "")
    status = str(row.get("status") or "")
    states = {"0": "SUBMITTING", "5": "SUBMITTING", "6": "SUBMITTING",
              "7": "STATUS_UNKNOWN", "95": "STATUS_UNKNOWN", "99": "REJECTED",
              "101": "WORKING", "110": "PARTIAL_FILLED", "111": "FILLED",
              "120": "CANCELLED"}
    state = states.get(status, "STATUS_UNKNOWN")
    if not no and state != "REJECTED":
        raise MappingError("missing order number")
    original = integer(row.get("requested_shares"), "requested_shares", 1)
    filled = integer(row.get("filled_shares"), "filled_shares")
    remaining = integer(row.get("remaining_shares"), "remaining_shares")
    if original != order.quantity_shares or filled + remaining > original:
        raise MappingError("report quantities inconsistent")
    if state == "FILLED" and (filled != original or remaining != 0):
        raise MappingError("full fill without full quantity")
    if state == "PARTIAL_FILLED" and not (0 < filled < original):
        raise MappingError("invalid partial fill")
    if state in {"CANCELLED", "REJECTED"} and remaining != 0:
        raise MappingError("terminal order has remaining quantity")
    if state == "REJECTED" and filled:
        raise MappingError("rejection conflicts with confirmed fills")
    return OrderReport(**required, order_no=no, requested_shares=original,
                       filled_shares=filled, remaining_shares=remaining,
                       state=state, broker_status=status)

class OrderLedger:
    """Cumulative quantities are snapshots; duplicates never add fills."""
    def __init__(self):
        self.orders = {}
        self.reports = {}

    def register(self, order):
        old = self.orders.get(order.intent_id)
        if old is not None and old != order:
            raise MappingError("intent reused with different order")
        if old is not None:
            return False
        self.orders[order.intent_id] = order
        return True

    def apply(self, row):
        identity = str(row.get("intent_id") or "")
        if identity not in self.orders:
            raise MappingError("unowned report")
        report = normalize_report(row, self.orders[identity])
        old = self.reports.get(identity)
        if old:
            if report.order_no != old.order_no and old.order_no:
                raise MappingError("order number changed")
            if report.filled_shares < old.filled_shares:
                raise MappingError("cumulative fills regressed; reconcile required")
            if old.state == "FILLED" and report != old:
                raise MappingError("filled order cannot regress")
            if old.state in {"CANCELLED", "REJECTED"} and report != old:
                raise MappingError("terminal order changed; reconcile required")
            if old.state == "PARTIAL_FILLED" and report.state in {"WORKING", "SUBMITTING"}:
                raise MappingError("partial order cannot regress")
        self.reports[identity] = report
        return report

class TaishinClient:
    """Gateway compatibility boundary. Real submission deliberately unavailable."""
    submission_audit_supported = False

    def __init__(self, *, mode="OFFLINE", day_trade_flag=None):
        self.mode = mode
        self.day_trade_flag = day_trade_flag
        self.ledger = OrderLedger()

    def preview_order(self, request):
        return from_cts_request(request, day_trade_flag=self.day_trade_flag).public_preview()

    def new_order(self, request):
        try:
            preview = self.preview_order(request)
        except MappingError as exc:
            return dict(ok=False, not_submitted=True, broker_submit_attempt=False,
                        broker_rejected=False, broker_unknown=False,
                        error=str(exc), order_no="", order_id="")
        return dict(ok=False, not_submitted=True, broker_submit_attempt=False,
                    broker_rejected=False, broker_unknown=False,
                    error="TAISHIN_LIVE_NOT_VERIFIED", order_no="", order_id="",
                    preview=preview)

    def status(self):
        return dict(broker="TAISHIN_CORE", connected=False,
                    connection_health="NOT_VERIFIED", live_enabled=False,
                    account_id="", security_account_ids=[], last_error="")

    def _unavailable(self, *args, **kwargs):
        return dict(ok=False, complete=False, error="TAISHIN_TRANSPORT_NOT_IMPLEMENTED",
                    not_submitted=True, broker_submit_attempt=False,
                    parsed_result=[], normalized_result=[])

    login = query_order = query_match = query_position = _unavailable
    modify_order = cancel_order = _unavailable

    def has_inflight_order(self, key):
        # Absence cannot be proved while transport/reconciliation is unavailable.
        return True

    def get_accounts(self):
        return []

    def logout(self):
        return dict(ok=True, errors=[])

    def configure_anomaly_log(self, directory):
        pass

    def close_owner(self):
        pass
