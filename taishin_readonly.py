"""Independent, read-only Taishin Core session and inventory completion tracking.

No order entry methods are exposed. Credentials and raw broker messages are never
included in returned diagnostics. A report-cache snapshot is never authoritative.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import logging
import os
from pathlib import Path
import re
import sys
import threading
import time
import uuid

from taishin_adapter import MappingError, integer

TAIPEI = timezone(timedelta(hours=8))

def observed_now():
    return datetime.now(TAIPEI).isoformat()

def masked_account(account):
    text = str(account)
    return "*" * max(0, len(text) - 3) + text[-3:]

def status_code(value):
    # MasterTradePy appends ")Chinese status description" to its numeric status.
    text = str(getattr(value, "value", value) or "")
    match = re.fullmatch(r"([0-9]+)(?:\)[^\r\n]*)?", text)
    if not match:
        raise MappingError("INVALID_BROKER_STATUS")
    return match.group(1)

def task_row_count(raw, encoding="cp950"):
    if isinstance(raw, bytes):
        raw = raw.decode(encoding, errors="strict")
    if not isinstance(raw, str):
        raise MappingError("TASK_RESULT_NOT_TEXT")
    # Only the exact metadata field is accepted. Never infer zero on parse failure.
    counts = re.findall(r"(?:^|[\n\r\x01|])rowCount=([0-9]+)(?=$|[\n\r\x01|])", raw)
    if len(counts) != 1:
        raise MappingError("TASK_ROWCOUNT_MISSING_OR_AMBIGUOUS")
    return integer(counts[0], "rowCount")

@dataclass
class InventoryTask:
    session: str
    rows: list = field(default_factory=list)
    expected_rows: int | None = None
    finished: bool = False
    error: str = ""

class InventoryCollector:
    def __init__(self):
        self.condition = threading.Condition()
        self.reset()

    def reset(self):
        with self.condition:
            self.session = uuid.uuid4().hex
            self.tasks = {}
            self.active = True
            self.condition.notify_all()
            return self.session

    def invalidate(self):
        with self.condition:
            self.active = False
            self.condition.notify_all()

    def add(self, session, work_id, row):
        with self.condition:
            if not self.active or session != self.session:
                return
            task = self.tasks.setdefault(str(work_id), InventoryTask(session))
            if task.finished:
                task.error = "ROW_AFTER_TASK_COMPLETION"
            else:
                task.rows.append(dict(row))
            self.condition.notify_all()

    def finish(self, session, work_id, *, expected_rows=None, error=""):
        with self.condition:
            if not self.active or session != self.session:
                return
            task = self.tasks.setdefault(str(work_id), InventoryTask(session))
            if task.finished:
                task.error = "DUPLICATE_TASK_COMPLETION"
            task.expected_rows = expected_rows
            task.error = task.error or error
            task.finished = True
            self.condition.notify_all()

    def wait(self, session, work_id, timeout=15):
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                if not self.active or session != self.session:
                    raise MappingError("SESSION_CHANGED_DURING_QUERY")
                task = self.tasks.get(str(work_id))
                if task is not None and task.finished:
                    break
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("INVENTORY_TASK_TIMEOUT")
                self.condition.wait(left)
            # Remove only the claimed task; unknown/late callbacks cannot fulfill the next query.
            del self.tasks[str(work_id)]
            if task.error:
                raise MappingError(task.error)
            expected = task.expected_rows
            if (not isinstance(expected, int) or isinstance(expected, bool) or expected < 0
                    or expected != len(task.rows)):
                raise MappingError("INVENTORY_ROWCOUNT_MISMATCH")
            seen = set()
            rows = []
            for raw in task.rows:
                symbol = str(raw.get("symbol") or "")
                if not re.fullmatch(r"[A-Za-z0-9]{4,8}", symbol) or symbol in seen:
                    raise MappingError("INVENTORY_SYMBOL_MISSING_OR_DUPLICATE")
                seen.add(symbol)
                rows.append(dict(symbol=symbol,
                    quantity_shares=integer(raw.get("qty"), "qty"),
                    credit_shares=integer(raw.get("qtyCredit"), "qtyCredit"),
                    debit_shares=integer(raw.get("qtyDebit"), "qtyDebit"),
                    odd_shares=integer(raw.get("qtyZero"), "qtyZero")))
            return dict(ok=True, complete=True, source="TAISHIN_CORE_NATIVE_TASK",
                        completion_evidence="SDK_TASK_RESULT_ROWCOUNT",
                        session=session, work_id=str(work_id), observed_at=observed_now(),
                        rows=rows, row_count=expected, sellable_confirmed=False)

def report_message(data):
    order = data.order
    original = data.orgOrder
    def text(obj, name):
        v = getattr(obj, name, None)
        return None if v is None else str(getattr(v, "value", v))
    side = {"B": "Buy", "S": "Sell"}.get(text(original, "side"))
    return dict(account=text(original, "tradingAccount"),
                order_no=text(order, "ordNo"), symbol=text(order, "symbol"),
                side=side, intent_id=text(original, "userDef"),
                requested_shares=text(original, "qty"),
                filled_shares=text(order, "cumQty"),
                remaining_shares=text(order, "leavesQty"),
                status=status_code(order.status), table=text(order, "tableName"),
                deal_price=text(order, "dealPri"),
                exchange_time=text(order, "trxTime"), observed_at=observed_now(),
                trade_date=None, source="TAISHIN_CORE_CALLBACK")

class ReadOnlyViolation(RuntimeError):
    pass

def require_readonly(*args, **kwargs):
    raise ReadOnlyViolation("READ_ONLY_PROCESS_ORDER_ENTRY_DISABLED")

class SDKDriver:
    """Loads a user-installed SDK from this project's own vendor directory."""
    def __init__(self, sdk_root, *, host="solace140.masterlink.com.tw:55555"):
        root = Path(sdk_root).resolve()
        if not (root / "MasterTradePy" / "api.py").is_file():
            raise MappingError("SDK_DIRECTORY_MISSING")
        if not re.fullmatch(r"[A-Za-z0-9.-]+:[0-9]{1,5}", host):
            raise MappingError("INVALID_CONNECTION_HOST")
        self.sdk_root = root
        self.host = host
        self.api = None
        self.accounts = []
        self.collector = InventoryCollector()
        self.collector.invalidate()
        self.session = ""
        self.invalidated = True
        self._report_lock = threading.RLock()
        self._inventory_lock = threading.Lock()
        self._reports = []
        self._report_errors = 0

    def load(self):
        if hasattr(self, "API"):
            return
        os.environ["MTPY_LOG_PATH"] = os.devnull
        os.environ["LOG_LEVEL"] = "CRITICAL"
        logging.disable(logging.CRITICAL)
        # No lookup of sibling financial-app directories or running services.
        sys.path.insert(0, str(self.sdk_root))
        api_mod = importlib.import_module("MasterTradePy.api")
        model = importlib.import_module("MasterTradePy.model")
        constants = importlib.import_module("MasterTradePy.constant")
        package_path = Path(api_mod.__file__).resolve()
        if not package_path.is_relative_to(self.sdk_root):
            raise MappingError("SDK_IMPORTED_FROM_WRONG_DIRECTORY")
        self.RCode = constants.RCode
        driver = self

        class Observer(model.MarketTrader):
            def __init__(self):
                self.generation = driver.session
            def current(self):
                return self.generation == driver.session
            def OnNewOrderReply(self, data):
                if self.current():
                    driver.invalidated = True
            def OnChangeReply(self, data):
                if self.current():
                    driver.invalidated = True
            def OnCancelReply(self, data):
                if self.current():
                    driver.invalidated = True
            def OnReport(self, data):
                if self.current() and isinstance(data, model.ReportOrder):
                    try:
                        row = report_message(data)
                        with driver._report_lock:
                            driver._reports.append(row)
                    except Exception:
                        driver._report_errors += 1
            def OnReqResult(self, workID, data):
                if self.current() and isinstance(data, (model.Inventory, model.Inventory_S)):
                    driver.collector.add(self.generation, str(workID), vars(data))
            def OnSystemEvent(self, data):
                if self.current():
                    driver.invalidated = True
                    driver.collector.invalidate()
            def OnAnnouncementEvent(self, data):
                pass
            def OnError(self, data):
                if self.current():
                    driver.invalidated = True
                    driver.collector.invalidate()

        class ReadOnlyAPI(api_mod.MasterTradeAPI):
            def OnSorTaskResult(self, sorapi, args):
                if self is not driver.api or self._ms_session != driver.session:
                    return
                task_id = str(args.Get("WorkID"))
                session = self._ms_session
                try:
                    expected = task_row_count(args.Get("OrigResult"), sorapi.Codeing)
                    super().OnSorTaskResult(sorapi, args)
                    driver.collector.finish(session, task_id, expected_rows=expected)
                except Exception:
                    driver.collector.finish(session, task_id, error="NATIVE_TASK_PARSE_FAILED")

        for name in ("NewOrder", "ChangeOrderQty", "ChangeOrderPrice"):
            setattr(ReadOnlyAPI, name, require_readonly)
        self.API = ReadOnlyAPI
        self.Observer = Observer
        self.sdk_source_sha256 = hashlib.sha256(package_path.read_bytes()).hexdigest()

    def ready(self):
        try:
            return (not self.invalidated and self.api is not None
                    and bool(self.api.IsReady()) and bool(self.api.sorapi.IsSessionConnected()))
        except Exception:
            return False

    def login(self, username, password, environment="formal"):
        try:
            return self._login(username, password, environment)
        except Exception as exc:
            self.logout()
            if isinstance(exc, MappingError):
                raise
            raise MappingError("BROKER_LOGIN_EXCEPTION") from None

    def _login(self, username, password, environment="formal"):
        if environment not in {"formal", "validation"}:
            raise MappingError("INVALID_ENVIRONMENT")
        self.logout()
        self.load()
        self.session = self.collector.reset()
        self.invalidated = False
        self._reports = []
        self._report_errors = 0
        self.api = self.API(self.Observer())
        self.api._ms_session = self.session
        self.api.SetConnectionHost(self.host)
        code = self.api.Login(username, password, environment == "validation", False, False)
        accepted = {self.RCode.OK, self.RCode.PART_USER_VERIFIED}
        if environment == "validation":
            accepted.add(self.RCode.USER_NOT_VERIFIED)
        if code not in accepted:
            name = str(getattr(code, "name", "UNKNOWN"))
            name = name if re.fullmatch(r"[A-Z0-9_]+", name) else "UNKNOWN"
            raise MappingError("BROKER_LOGIN_REJECTED_" + name)
        if not self.ready():
            raise MappingError("BROKER_HOST_NOT_READY")
        # Native securities account list is filled by OnSorApReady, never guess by slicing IDs.
        self.accounts = [str(x) for x in self.api.aAccList]
        if not self.accounts or len(set(self.accounts)) != len(self.accounts):
            self.logout()
            raise MappingError("SECURITIES_ACCOUNT_LIST_INVALID")
        if any(getattr(acc.CAErrCode(), "name", "") != "ERR_Success"
               for acc in self.api.tradeAccs.Values()):
            self.logout()
            raise MappingError("CERTIFICATE_NOT_READY")
        if self.api.CheckAccs(tradingAccounts=self.accounts) != self.RCode.OK:
            self.logout()
            raise MappingError("ACCOUNT_CHECK_FAILED")
        return dict(ok=True, connected=self.ready(), readonly=True, live_enabled=False,
                    session=self.session, environment=environment,
                    accounts=[masked_account(x) for x in self.accounts],
                    sdk_source_sha256=self.sdk_source_sha256,
                    order_recovery_complete=False)

    def account(self, index):
        if not self.ready():
            raise MappingError("READONLY_SESSION_NOT_READY")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(self.accounts):
            raise MappingError("ACCOUNT_INDEX_INVALID")
        return self.accounts[index]

    def inventory(self, account_index=0, timeout=15):
        with self._inventory_lock:
            account = self.account(account_index)
            session = self.session
            work_id = self.api.ReqInventoryRayinTotal(account)
            if not work_id:
                raise MappingError("INVENTORY_WORK_ID_MISSING")
            result = self.collector.wait(session, str(work_id), timeout)
            if not self.ready() or self.session != session:
                raise MappingError("SESSION_NOT_READY_AFTER_QUERY")
            result["account_index"] = account_index
            result["account"] = masked_account(account)
            return result

    def reports(self, account_index=0, *, matches_only=False):
        account = self.account(account_index)
        with self._report_lock:
            rows = [dict(x) for x in self._reports if x.get("account") == account
                    and (not matches_only or x.get("table") == "RPT:TwsDeal")]
        # No documented completion marker was found. An ACK or quiet period is not proof.
        return dict(ok=False, complete=False, source="TAISHIN_CORE_CALLBACK_CACHE",
                    error="ORDER_RECOVERY_COMPLETION_UNVERIFIED",
                    observed_rows=rows, row_count=len(rows),
                    callback_parse_errors=self._report_errors,
                    session=self.session, observed_at=observed_now())

    def status(self):
        return dict(connected=self.ready(), readonly=True, live_enabled=False,
                    session=self.session, order_recovery_complete=False,
                    account_count=len(self.accounts))

    def logout(self):
        self.collector.invalidate()
        self.invalidated = True
        api, self.api = self.api, None
        self.accounts = []
        if api is not None:
            for name in ("_MasterTradeAPI__fWorkThr_RecMsg",
                         "_MasterTradeAPI__fWorkThr_RecMsg_RevAnn"):
                thread = getattr(api, name, None)
                if thread is not None:
                    thread._run = False
            for name in ("_client", "_client_RevAnn"):
                client = getattr(api, name, None)
                if client is not None:
                    try:
                        client.disconnect()
                    except Exception:
                        pass
            native = getattr(api, "sorapi", None)
            if native is not None:
                try:
                    native.Disconnect()
                except Exception:
                    pass
        return dict(ok=True, connected=False, readonly=True)
