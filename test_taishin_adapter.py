import dataclasses
import unittest
from pathlib import Path
from contextlib import nullcontext
from api_gateway_v2_7 import APIGateway, CTSOrderRequest
from taishin_adapter import MappingError, OrderLedger, from_cts_request, normalize_report

def request(**changes):
    values = dict(account_id="TEST-ACCOUNT", trade_date=20261102, trade_type=0,
                  order_type=0, side=1, symbol="2330", quantity=2, price_type=4,
                  price="0", condition=0, order_intent_id="TEST-0001")
    values.update(changes)
    return CTSOrderRequest(**values)

def report(order, **changes):
    r = dict(account=order.account, trade_date=order.trade_date, symbol=order.symbol,
             side=order.side, intent_id=order.intent_id, order_no="TEST01",
             requested_shares=2000, filled_shares=0, remaining_shares=2000, status="101")
    r.update(changes)
    return r

class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.order = from_cts_request(request(), day_trade_flag="Y")

    def test_two_lots_become_two_thousand_shares(self):
        self.assertEqual(self.order.sdk_fields()["qty"], "2000")
        self.assertEqual(self.order.sdk_fields()["tradingUnit"], "COMMON")

    def test_condition_codes_do_not_swap_ioc_and_fok(self):
        for code, name in [(0, "ROD"), (1, "FOK"), (2, "IOC")]:
            with self.subTest(code=code):
                mapped = from_cts_request(request(condition=code), day_trade_flag="Y")
                self.assertEqual(mapped.time_in_force, name)

    def test_sell_mapping(self):
        self.assertEqual(from_cts_request(request(side=2), day_trade_flag="Y").side, "Sell")

    def test_invalid_fields_fail_closed(self):
        for changes in [dict(quantity=0), dict(quantity=-1), dict(quantity=True),
                        dict(quantity=1.5), dict(symbol="../bad"), dict(account_id=""),
                        dict(trade_type=1), dict(order_type=2), dict(price_type=0),
                        dict(price="NaN"), dict(price="1"), dict(condition=3),
                        dict(trade_date=20260230), dict(order_intent_id=""),
                        dict(order_intent_id="a" * 129), dict(pay_type=1),
                        dict(side=0), dict(broker="other")]:
            with self.subTest(changes=changes), self.assertRaises(MappingError):
                from_cts_request(request(**changes), day_trade_flag="Y")

    def test_explicit_day_trade_required(self):
        for flag in [None, "", "X"]:
            with self.subTest(flag=flag), self.assertRaises(MappingError):
                from_cts_request(request(), day_trade_flag=flag)

    def test_preview_redacts_account_and_does_not_submit(self):
        preview = self.order.public_preview()
        self.assertNotIn("TEST-ACCOUNT", str(preview))
        self.assertFalse(preview["broker_submit_attempt"])
        self.assertFalse(preview["live_enabled"])

    def test_report_identity_cannot_cross_accounts_days_or_orders(self):
        for key in ["account", "trade_date", "symbol", "side", "intent_id"]:
            with self.subTest(key=key), self.assertRaises(MappingError):
                normalize_report(report(self.order, **{key: "wrong"}), self.order)

    def test_partial_then_full_and_duplicates(self):
        ledger = OrderLedger()
        self.assertTrue(ledger.register(self.order))
        self.assertFalse(ledger.register(self.order))
        partial = report(self.order, status="110", filled_shares=1000, remaining_shares=1000)
        ledger.apply(partial)
        ledger.apply(partial)
        self.assertEqual(ledger.reports[self.order.intent_id].filled_shares, 1000)
        full = report(self.order, status="111", filled_shares=2000, remaining_shares=0)
        ledger.apply(full)
        ledger.apply(full)
        self.assertEqual(ledger.reports[self.order.intent_id].filled_shares, 2000)

    def test_overfills_and_false_full_fills_are_rejected(self):
        for changes in [dict(filled_shares=3000), dict(status="111"),
                        dict(filled_shares=-1), dict(requested_shares=1000),
                        dict(filled_shares=""), dict(remaining_shares=None)]:
            with self.subTest(changes=changes), self.assertRaises(MappingError):
                normalize_report(report(self.order, **changes), self.order)

    def test_unknown_and_cancelled_have_distinct_meanings(self):
        for status, expected in [("95", "STATUS_UNKNOWN"), ("120", "CANCELLED"),
                                 ("unexpected", "STATUS_UNKNOWN"), ("99", "REJECTED")]:
            row = report(self.order, status=status,
                         remaining_shares=0 if status in {"120", "99"} else 2000)
            self.assertEqual(normalize_report(row, self.order).state, expected)

    def test_no_order_number_rejected_except_explicit_rejection(self):
        with self.assertRaises(MappingError):
            normalize_report(report(self.order, order_no=""), self.order)
        self.assertEqual(normalize_report(report(self.order, order_no="", status="99",
                                                remaining_shares=0), self.order).state, "REJECTED")

    def test_stale_and_conflicting_reports_cannot_erase_fills(self):
        ledger = OrderLedger()
        ledger.register(self.order)
        ledger.apply(report(self.order, status="110", filled_shares=1000, remaining_shares=1000))
        for changes in [dict(status="101"), dict(order_no="different", status="110",
                        filled_shares=1000, remaining_shares=1000)]:
            with self.subTest(changes=changes), self.assertRaises(MappingError):
                ledger.apply(report(self.order, **changes))

    def test_terminal_order_cannot_reopen(self):
        ledger = OrderLedger()
        ledger.register(self.order)
        ledger.apply(report(self.order, status="111", filled_shares=2000, remaining_shares=0))
        with self.assertRaises(MappingError):
            ledger.apply(report(self.order))
        self.assertEqual(ledger.reports[self.order.intent_id].filled_shares, 2000)

    def test_duplicate_intent_cannot_change_order(self):
        ledger = OrderLedger()
        ledger.register(self.order)
        with self.assertRaises(MappingError):
            ledger.register(dataclasses.replace(self.order, quantity_shares=3000))

    def test_unowned_report_rejected(self):
        with self.assertRaises(MappingError):
            OrderLedger().apply(report(self.order))

    def test_gateway_defaults_to_taishin_and_live_is_blocked(self):
        with nullcontext(str(Path(__file__).parent / ".test-replay")) as root:
            gateway = APIGateway(mode="LIVE", replay_dir=root, taishin_day_trade_flag="Y")
            try:
                self.assertEqual(gateway.broker.status()["broker"], "TAISHIN_CORE")
                result = gateway.broker.new_order(request())
                self.assertFalse(result["ok"])
                self.assertTrue(result["not_submitted"])
                self.assertFalse(result["broker_submit_attempt"])
                self.assertEqual(result["error"], "TAISHIN_LIVE_NOT_VERIFIED")
                self.assertFalse(gateway.broker.query_order("TEST-ACCOUNT")["ok"])
                self.assertFalse(gateway.broker.query_position("TEST-ACCOUNT", 20261102)["complete"])
            finally:
                self.assertTrue(gateway.close()["ok"])

    def test_strategy_request_reaches_taishin_preview(self):
        from trading_v3_9 import _create_cts_order_request, TradingParameters
        gateway = APIGateway(mode="OFFLINE", taishin_day_trade_flag="Y",
                             replay_dir=str(Path(__file__).parent / ".test-replay"))
        try:
            decision = dict(action="BUY", symbol="2330", cts_quantity=2,
                            cts_trade_type=0, order_price=100)
            original = _create_cts_order_request(
                gateway, decision, dict(order_trade_date="20261102"), {},
                account_id="TEST-ACCOUNT", parameters=TradingParameters())
            original = dataclasses.replace(original, order_intent_id="TEST-0002")
            preview = gateway.broker.preview_order(original)
            self.assertEqual(preview["sdk_fields"]["qty"], "2000")
            self.assertEqual(preview["sdk_fields"]["priceType"], "MKT")
            result = gateway.broker.new_order(dataclasses.replace(original, quantity=0))
            self.assertTrue(result["not_submitted"])
            self.assertFalse(result["broker_unknown"])
        finally:
            gateway.close()

if __name__ == "__main__":
    unittest.main()
