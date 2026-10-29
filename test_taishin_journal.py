import dataclasses
from pathlib import Path
import unittest
import uuid
from taishin_adapter import MappingError, broker_tag, from_cts_request
from taishin_journal import IntentJournal
from test_taishin_adapter import request

class JournalTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(__file__).parent / ".test-artifacts" / (uuid.uuid4().hex + ".sqlite3")
        self.journal = IntentJournal(self.path)
        self.order = from_cts_request(request(order_intent_id="a" * 64), day_trade_flag="Y")
    def tearDown(self):
        self.journal.close()
    def test_full_strategy_identity_maps_to_stable_broker_tag(self):
        self.assertEqual(len(self.order.sdk_fields()["userDef"]), 32)
        self.assertEqual(self.order.intent_id, "a" * 64)
        self.journal.reserve(self.order)
        self.assertEqual(self.journal.resolve_tag(broker_tag(self.order.intent_id)), self.order.intent_id)
    def test_duplicate_intent_never_creates_second_reservation(self):
        self.assertTrue(self.journal.reserve(self.order)["created"])
        self.assertFalse(self.journal.reserve(self.order)["created"])
        with self.assertRaises(MappingError):
            self.journal.reserve(dataclasses.replace(self.order, quantity_shares=3000))
    def test_crash_after_submit_boundary_survives_restart_and_cannot_resend(self):
        self.journal.reserve(self.order)
        self.journal.begin_submission(self.order.intent_id)
        self.journal.close()
        self.journal = IntentJournal(self.path)
        self.assertEqual(self.journal.needs_reconciliation()[0]["state"], "SUBMITTING")
        self.assertFalse(self.journal.reserve(self.order)["created"])
        with self.assertRaises(MappingError):
            self.journal.begin_submission(self.order.intent_id)
    def test_exception_is_unknown_never_not_submitted(self):
        self.journal.reserve(self.order)
        self.journal.begin_submission(self.order.intent_id)
        self.journal.mark_unknown(self.order.intent_id)
        with self.assertRaises(MappingError):
            self.journal.record_state(self.order.intent_id, state="NOT_SUBMITTED")
        self.assertEqual(self.journal.get(self.order.intent_id)["state"], "STATUS_UNKNOWN")
    def test_only_pre_boundary_rejection_can_be_not_submitted(self):
        self.journal.reserve(self.order)
        self.journal.record_state(self.order.intent_id, state="NOT_SUBMITTED")
        self.assertEqual(self.journal.needs_reconciliation(), [])
    def test_broker_evidence_requires_account_date_and_order_number(self):
        self.journal.reserve(self.order)
        self.journal.begin_submission(self.order.intent_id)
        for evidence in [{},dict(account="wrong",trade_date=self.order.trade_date,order_no="TEST1"),
                         dict(account=self.order.account,trade_date="20000101",order_no="TEST1")]:
            with self.subTest(evidence=evidence), self.assertRaises(MappingError):
                self.journal.record_state(self.order.intent_id,state="WORKING",**evidence)
    def test_fills_cannot_regress_or_overfill(self):
        self.journal.reserve(self.order)
        self.journal.begin_submission(self.order.intent_id)
        evidence=dict(account=self.order.account,trade_date=self.order.trade_date,order_no="TEST1")
        self.journal.record_state(self.order.intent_id,state="PARTIAL_FILLED",filled_shares=1000,**evidence)
        for value in [0,3000]:
            with self.subTest(value=value), self.assertRaises(MappingError):
                self.journal.record_state(self.order.intent_id,state="PARTIAL_FILLED",filled_shares=value,**evidence)
        self.journal.record_state(self.order.intent_id,state="FILLED",filled_shares=2000,**evidence)
        with self.assertRaises(MappingError):
            self.journal.record_state(self.order.intent_id,state="WORKING",**evidence)
    def test_second_connection_observes_reserved_intent(self):
        self.journal.reserve(self.order)
        second=IntentJournal(self.path)
        try:
            self.assertFalse(second.reserve(self.order)["created"])
        finally:
            second.close()

if __name__ == "__main__":
    unittest.main()
