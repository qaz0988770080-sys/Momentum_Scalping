import enum
from pathlib import Path
from types import SimpleNamespace, ModuleType
import threading
import unittest
from unittest.mock import patch
import uuid

from taishin_adapter import MappingError
from taishin_readonly import (InventoryCollector, ReadOnlyViolation, SDKDriver,
                              report_message, status_code, task_row_count)

def inventory(symbol="2330", **changes):
    row = dict(symbol=symbol, qty="2000", qtyCredit="0", qtyDebit="0", qtyZero="0")
    row.update(changes)
    return row

class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.collector = InventoryCollector()
        self.epoch = self.collector.session

    def test_requires_exact_completion_metadata(self):
        for raw in ["rowCount=0", b"header\nrowCount=2\n", "header\x01rowCount=1\x01"]:
            self.assertIsInstance(task_row_count(raw), int)
        for raw in ["", "No Data", "error=rowCount=0", "rowCount=-1",
                    "rowCount=1\nrowCount=1", "rowCount=2garbage"]:
            with self.subTest(raw=raw), self.assertRaises(MappingError):
                task_row_count(raw)

    def test_empty_requires_explicit_completed_task(self):
        with self.assertRaises(TimeoutError):
            self.collector.wait(self.epoch, "qid1", timeout=0)
        self.collector.finish(self.epoch, "qid1", expected_rows=0)
        result = self.collector.wait(self.epoch, "qid1", timeout=0)
        self.assertTrue(result["complete"])
        self.assertEqual(result["rows"], [])
        self.assertFalse(result["sellable_confirmed"])

    def test_callback_before_request_returns_work_id(self):
        self.collector.add(self.epoch, "qid1", inventory())
        self.collector.finish(self.epoch, "qid1", expected_rows=1)
        self.assertEqual(self.collector.wait(self.epoch, "qid1")["rows"][0]["quantity_shares"], 2000)

    def test_completion_waits_for_the_claimed_work_id(self):
        self.collector.finish(self.epoch, "wrong", expected_rows=0)
        with self.assertRaises(TimeoutError):
            self.collector.wait(self.epoch, "wanted", timeout=0)

    def test_row_count_mismatch_rejected(self):
        self.collector.add(self.epoch, "qid1", inventory())
        self.collector.finish(self.epoch, "qid1", expected_rows=2)
        with self.assertRaisesRegex(MappingError, "ROWCOUNT"):
            self.collector.wait(self.epoch, "qid1")

    def test_duplicates_and_invalid_quantities_rejected(self):
        for rows in [[inventory(), inventory()], [inventory(qty="-1")],
                     [inventory(qty="")], [inventory(symbol="")],
                     [inventory(qtyCredit=None)]]:
            epoch = self.collector.reset()
            for row in rows:
                self.collector.add(epoch, "qid1", row)
            self.collector.finish(epoch, "qid1", expected_rows=len(rows))
            with self.subTest(rows=rows), self.assertRaises(MappingError):
                self.collector.wait(epoch, "qid1")

    def test_old_session_is_not_a_new_session_result(self):
        old = self.epoch
        new = self.collector.reset()
        self.collector.finish(old, "qid1", expected_rows=0)
        with self.assertRaises(TimeoutError):
            self.collector.wait(new, "qid1", timeout=0)
        with self.assertRaises(MappingError):
            self.collector.wait(old, "qid1", timeout=0)

    def test_disconnect_cancels_wait(self):
        self.collector.invalidate()
        with self.assertRaises(MappingError):
            self.collector.wait(self.epoch, "qid1", timeout=0)

    def test_duplicate_completion_and_late_rows_rejected(self):
        for mode in ["complete", "row"]:
            epoch = self.collector.reset()
            self.collector.finish(epoch, "qid1", expected_rows=0)
            if mode == "complete":
                self.collector.finish(epoch, "qid1", expected_rows=0)
            else:
                self.collector.add(epoch, "qid1", inventory())
            with self.assertRaises(MappingError):
                self.collector.wait(epoch, "qid1")

    def test_native_parse_error_is_not_empty_inventory(self):
        self.collector.finish(self.epoch, "qid1", expected_rows=0, error="PARSE_FAILED")
        with self.assertRaisesRegex(MappingError, "PARSE_FAILED"):
            self.collector.wait(self.epoch, "qid1")

    def test_real_status_suffixes_are_parsed_without_guessing(self):
        self.assertEqual(status_code("101)交易所已接受"), "101")
        self.assertEqual(status_code("95)狀態不明"), "95")
        for value in ["", "101bad", "101\npassword", None]:
            with self.subTest(value=value), self.assertRaises(MappingError):
                status_code(value)

    def test_callback_keeps_unknown_trade_date(self):
        data = SimpleNamespace(order=SimpleNamespace(ordNo="TEST1", symbol="2330",
                    status="110)部分成交", tableName="RPT:TwsDeal", cumQty="1000",
                    leavesQty="1000", dealPri="100", trxTime="093000000"),
                    orgOrder=SimpleNamespace(tradingAccount="1234567", side="B",
                                             qty="2000", userDef="TEST-1"))
        row = report_message(data)
        self.assertEqual(row["status"], "110")
        self.assertEqual(row["side"], "Buy")
        self.assertIsNone(row["trade_date"])

class FakeInventory:
    def __init__(self, **values):
        self.__dict__.update(values)

class FakeReport:
    pass

class FakeAPI:
    def __init__(self, observer):
        self.observer = observer
        self.sorapi = SimpleNamespace(Codeing="cp950", IsSessionConnected=lambda: True,
                                      Disconnect=lambda: None)
        self.aAccList = ["1234567"]
        self.tradeAccs = SimpleNamespace(Values=lambda: [
            SimpleNamespace(CAErrCode=lambda: SimpleNamespace(name="ERR_Success"))])
        self.native_calls = 0
        self.login_calls = []
        self.connected = False
    def SetConnectionHost(self, host):
        self.host = host
    def Login(self, *args):
        self.login_calls.append(args)
        self.connected = True
        return 0
    def CheckAccs(self, tradingAccounts):
        return 0
    def IsReady(self):
        return self.connected
    def NewOrder(self, *args):
        self.native_calls += 1
    def ChangeOrderQty(self, *args):
        self.native_calls += 1
    def ChangeOrderPrice(self, *args):
        self.native_calls += 1
    def OnSorApReady(self, sorapi, args):
        pass
    def OnSorTaskResult(self, sorapi, args):
        self.observer.OnReqResult("qid1", FakeInventory(**inventory()))
    def ReqInventoryRayinTotal(self, account):
        args = SimpleNamespace(Get=lambda k: {
            "WorkID": "qid1", "OrigResult": b"header\nrowCount=1\n"}[k])
        self.OnSorTaskResult(self.sorapi, args)
        return "qid1"

class DriverTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent / ".test-artifacts" / ("sdk-" + uuid.uuid4().hex)
        (self.root / "MasterTradePy").mkdir(parents=True)
        source = self.root / "MasterTradePy" / "api.py"
        source.write_text("# fake SDK for offline contract tests\n", encoding="utf-8")
        api = ModuleType("MasterTradePy.api")
        api.__file__ = str(source)
        api.MasterTradeAPI = FakeAPI
        model = ModuleType("MasterTradePy.model")
        model.MarketTrader = object
        model.Inventory = model.Inventory_S = FakeInventory
        model.ReportOrder = FakeReport
        constants = ModuleType("MasterTradePy.constant")
        constants.RCode = SimpleNamespace(OK=0, PART_USER_VERIFIED=11, USER_NOT_VERIFIED=-14)
        self.modules = patch.dict("sys.modules", {
            "MasterTradePy.api": api, "MasterTradePy.model": model,
            "MasterTradePy.constant": constants})
        self.modules.start()
        self.driver = SDKDriver(self.root)

    def tearDown(self):
        self.driver.logout()
        self.modules.stop()

    def login(self):
        return self.driver.login("TEST-USER", "TEST-PASSWORD")

    def test_login_waits_for_native_readiness_and_is_readonly(self):
        result = self.login()
        self.assertTrue(result["connected"])
        self.assertFalse(result["live_enabled"])
        self.assertEqual(result["accounts"], ["****567"])
        self.assertNotIn("TEST-PASSWORD", str(result))
        self.assertEqual(self.driver.api.login_calls[0][2:], (False, False, False))

    def test_validation_login_does_not_enable_formal_queries(self):
        result = self.driver.login("TEST-USER", "TEST-PASSWORD", "validation")
        self.assertTrue(result["connected"])
        self.assertEqual(result["environment"], "validation")
        self.assertEqual(self.driver.api.login_calls[0][2:], (True, False, False))
        for query in (self.driver.inventory, self.driver.reports):
            with self.assertRaisesRegex(MappingError, "FORMAL_ENVIRONMENT_REQUIRED"):
                query()

    def test_no_order_methods_can_reach_native_api(self):
        self.login()
        for name in ["NewOrder", "ChangeOrderQty", "ChangeOrderPrice"]:
            with self.subTest(name=name), self.assertRaises(ReadOnlyViolation):
                getattr(self.driver.api, name)(object())
        self.assertEqual(self.driver.api.native_calls, 0)

    def test_real_callback_path_completes_inventory(self):
        self.login()
        result = self.driver.inventory(0)
        self.assertTrue(result["complete"])
        self.assertEqual(result["row_count"], 1)
        self.assertEqual(result["rows"][0]["quantity_shares"], 2000)
        self.assertFalse(result["sellable_confirmed"])

    def test_order_cache_is_never_reported_as_complete(self):
        self.login()
        result = self.driver.reports()
        self.assertFalse(result["ok"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["error"], "ORDER_RECOVERY_COMPLETION_UNVERIFIED")

    def test_account_index_validation(self):
        self.login()
        for index in [True, -1, 1, "0"]:
            with self.subTest(index=index), self.assertRaises(MappingError):
                self.driver.inventory(index)

    def test_old_observer_cannot_invalidate_new_session(self):
        self.login()
        old_observer = self.driver.api.observer
        self.driver.logout()
        self.login()
        old_observer.OnSystemEvent(object())
        self.assertTrue(self.driver.ready())

    def test_disconnect_invalidates_inventory_and_readiness(self):
        self.login()
        self.driver.api.observer.OnSystemEvent(object())
        self.assertFalse(self.driver.ready())
        with self.assertRaises(MappingError):
            self.driver.inventory()

    def test_old_native_task_cannot_complete_new_session(self):
        self.login()
        old_api = self.driver.api
        self.driver.logout()
        self.login()
        args = SimpleNamespace(Get=lambda k: {"WorkID": "qid1", "OrigResult": b"rowCount=1"}[k])
        old_api.OnSorTaskResult(old_api.sorapi, args)
        with self.assertRaises(TimeoutError):
            self.driver.collector.wait(self.driver.session, "qid1", timeout=0)

    def test_connecting_notice_does_not_permanently_invalidate_login(self):
        original = FakeAPI.Login
        def with_progress(api, *args):
            api.observer.OnSystemEvent(SimpleNamespace(
                code=SimpleNamespace(name="IN_PROGRESS"), message="TEST MESSAGE"))
            return original(api, *args)
        with patch.object(FakeAPI, "Login", with_progress):
            self.assertTrue(self.login()["connected"])
        self.assertFalse(self.driver._login_in_progress)

    def test_late_native_readiness_is_waited_for(self):
        original = FakeAPI.Login
        def delayed(api, *args):
            original(api, *args)
            api.connected = False
            threading.Timer(0.05, lambda: setattr(api, "connected", True)).start()
            return 0
        self.driver.host_ready_timeout = 0.5
        with patch.object(FakeAPI, "Login", delayed):
            self.assertTrue(self.login()["connected"])

    def test_real_refusal_remains_blocked_and_raw_message_is_not_saved(self):
        original = FakeAPI.Login
        def refused(api, *args):
            original(api, *args)
            api.observer.OnSystemEvent(SimpleNamespace(code=SimpleNamespace(name="FAIL"),
                message="10061 FAKE_SECRET_USER_AND_PASSWORD"))
            return 0
        with patch.object(FakeAPI, "Login", refused):
            with self.assertRaisesRegex(MappingError, "BROKER_CONNECTION_REFUSED"):
                self.login()
        diagnostics = self.driver.last_login_diagnostics
        self.assertNotIn("FAKE_SECRET", str(diagnostics))
        self.assertEqual(diagnostics["system_events"][0]["category"], "CONNECTION_REFUSED")
        self.assertIsNone(self.driver.api)

    def test_host_timeout_preserves_flags_without_unlocking_session(self):
        def not_ready(api, *args):
            return 0
        self.driver.host_ready_timeout = 0
        with patch.object(FakeAPI, "Login", not_ready):
            with self.assertRaisesRegex(MappingError, "BROKER_HOST_NOT_READY"):
                self.login()
        self.assertFalse(self.driver.last_login_diagnostics["application_ready"])
        self.assertFalse(self.driver.ready())

    def test_post_login_reconnect_requires_fresh_session(self):
        self.login()
        self.driver.api.observer.OnSystemEvent(SimpleNamespace(
            code=SimpleNamespace(name="RECONNECTED"), message="RECONNECTED"))
        self.assertFalse(self.driver.ready())
        self.assertFalse(self.driver.collector.active)

    def test_unexpected_login_error_never_exposes_sdk_message(self):
        with patch.object(FakeAPI, "Login", side_effect=RuntimeError("FAKE_SECRET")):
            with self.assertRaisesRegex(MappingError, "^BROKER_LOGIN_EXCEPTION$"):
                self.login()
        self.assertNotIn("FAKE_SECRET", str(self.driver.last_login_diagnostics))
        self.assertIsNone(self.driver.api)

    def test_native_ready_callback_error_is_recorded_without_raw_sdk_text(self):
        self.login()
        with patch.object(FakeAPI, "OnSorApReady", side_effect=ValueError("FAKE_SECRET")):
            self.driver.api.OnSorApReady(self.driver.api.sorapi, object())
        self.assertFalse(self.driver.ready())
        events = self.driver.login_diagnostics()["native_callback_errors"]
        self.assertEqual(events[0]["stage"], "AP_READY")
        self.assertEqual(events[0]["error"], "ValueError")
        self.assertNotIn("FAKE_SECRET", str(events))


class GatewayReadOnlyTests(unittest.TestCase):
    def setUp(self):
        from taishin_adapter import TaishinClient
        self.session = SimpleNamespace(
            accounts=["1234567"], ready=lambda: True,
            status=lambda: dict(connected=True, readonly=True, live_enabled=False),
            reports=lambda *a, **kw: dict(ok=False, complete=False, observed_rows=[]),
            inventory=lambda index: dict(ok=True, complete=True, rows=[dict(
                symbol="2330", quantity_shares=2000, odd_shares=0, credit_shares=0, debit_shares=0)]))
        self.client = TaishinClient(readonly_session=self.session, day_trade_flag="Y")
        from datetime import datetime, timezone, timedelta
        self.today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")

    def test_inventory_remains_shares_when_parsed_by_original_strategy(self):
        from trading_v3_9 import parse_cts_positions
        result = self.client.query_position("1234567", self.today)
        self.assertTrue(result["ok"])
        rows = parse_cts_positions(result["parsed_result"])
        self.assertEqual(rows[0]["quantity"], 2000)
        self.assertIsNone(rows[0]["cost"])
        self.assertFalse(result["parsed_result"][0]["sellable_confirmed"])

    def test_wrong_account_historical_date_and_incomplete_data_rejected(self):
        self.assertFalse(self.client.query_position("wrong", self.today)["ok"])
        self.assertFalse(self.client.query_position("1234567", "20000101")["ok"])
        self.session.inventory = lambda i: dict(ok=True, complete=False, rows=[])
        self.assertFalse(self.client.query_position("1234567", self.today)["ok"])

    def test_odd_or_credit_inventory_is_not_guessed(self):
        for field in ["odd_shares", "credit_shares", "debit_shares"]:
            row = dict(symbol="2330", quantity_shares=2000, odd_shares=0,
                       credit_shares=0, debit_shares=0)
            row[field] = 100
            self.session.inventory = lambda i: dict(ok=True, complete=True, rows=[row])
            with self.subTest(field=field):
                self.assertFalse(self.client.query_position("1234567", self.today)["ok"])

    def test_readonly_never_unlocks_trading_or_marks_cache_complete(self):
        self.assertEqual(self.client.status()["connection_health"], "READ_ONLY")
        self.assertFalse(self.client.status()["live_enabled"])
        self.assertFalse(self.client.query_order("1234567")["ok"])
        self.assertFalse(self.client.query_match("1234567")["complete"])

class WorkerProtocolTests(unittest.TestCase):
    def test_unsupported_commands_cannot_submit_or_persist_secret_fields(self):
        import json
        import subprocess
        import sys
        base = Path(__file__).parent
        fake_sdk = base / ".test-artifacts" / "worker-protocol-sdk"
        (fake_sdk / "MasterTradePy").mkdir(parents=True, exist_ok=True)
        (fake_sdk / "MasterTradePy" / "api.py").write_text("# must never be imported\n", encoding="utf-8")
        result = subprocess.run([sys.executable, str(base / "taishin_readonly_worker.py"),
                                 "--sdk-root", str(fake_sdk)],
            input=json.dumps(dict(cmd="place_order", password="FAKE_TEST_SECRET")) +
                  "\nnot-json\n" + json.dumps(dict(cmd="logout")) + "\n",
            capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("FAKE_TEST_SECRET", result.stdout + result.stderr)
        messages = [json.loads(line.split("@@MOMENTUM_READONLY@@")[1])
                    for line in result.stdout.splitlines()]
        self.assertEqual(messages[0]["kind"], "ready")
        errors = [x for x in messages if x["kind"] == "error"]
        self.assertEqual(errors[0]["error"], "READ_ONLY_COMMAND_NOT_ALLOWED")
        self.assertEqual(errors[1]["error"], "JSONDecodeError")
        self.assertTrue(all(x.get("live_enabled") is False for x in errors))

if __name__ == "__main__":
    unittest.main()
