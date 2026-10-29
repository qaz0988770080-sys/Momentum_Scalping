import hashlib,json,tempfile,unittest,uuid
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime,timezone,timedelta
from taishin_futures_verification import VerificationService,VerificationError,read_plan

class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(__file__).parent/'.test-artifacts'/('verify-'+uuid.uuid4().hex);self.root.mkdir(parents=True)
        self.account='SYNTHETIC-ACCOUNT'
        self.plan=dict(source_url='https://mlapi.tssco.com.tw/web_api/service/twftrade-api-testing',plan_id=uuid.uuid4().hex,symbol='TESTA9',side='BUY',price_type='LMT',price='100.00',quantity=2,tif='ROD',account_sha256=hashlib.sha256(self.account.encode()).hexdigest(),expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat())
        self.save()
        self.api=SimpleNamespace(_momentum_test_target=True,IsConn_Trade=True,IsRecoverDone=True,fAccList={self.account:object()})
        self.session=SimpleNamespace(api=self.api,environment='validation')
        self.service=VerificationService(self.session,self.root);self.sent=0
        self.api.send_website_verification=self.send
    def tearDown(self):self.service.db.close()
    def save(self):(self.root/'verification-plan.json').write_text(json.dumps(self.plan),encoding='utf-8')
    def send(self,plan,account,tag):
        self.sent+=1
        self.assertEqual(self.service.db.execute('SELECT state FROM attempts').fetchone()[0],'SUBMITTING')
        self.service.on_report(SimpleNamespace(UserDefine=self.account+tag,OrderID='TEST123',OrderStatus=2,CumQty=0))
        return True
    def submit(self):return self.service.submit(self.service.status()['plan_digest'],wait=0)
    def test_late_recovery_updates_connection_without_new_login_or_order(self):
        self.api.IsConn_Trade=False;self.api.IsRecoverDone=False
        self.assertFalse(self.service.status()['connected']);self.assertTrue(self.service.status()['keep_session'])
        self.api.IsConn_Trade=True;self.api.IsRecoverDone=True
        self.assertTrue(self.service.status()['connected']);self.assertFalse(self.service.status()['keep_session']);self.assertEqual(self.sent,0)
    def test_test_submission_waits_for_real_report_and_durable_record(self):
        result=self.submit();self.assertEqual(result['attempt']['state'],'ACCEPTED');self.assertEqual(result['attempt']['order_no'],'TEST123')
        self.assertNotIn(self.account,str(result));self.assertFalse(result['live_enabled'])
    def test_formal_environment_cannot_submit(self):
        self.session.environment='formal'
        with self.assertRaisesRegex(VerificationError,'TEST_CONNECTION'):self.submit()
        self.assertEqual(self.sent,0)
    def test_no_proven_test_target_cannot_submit(self):
        self.api._momentum_test_target=False
        with self.assertRaisesRegex(VerificationError,'TEST_CONNECTION'):self.submit()
        self.assertEqual(self.sent,0)
    def test_account_mismatch_rejected_before_native_call(self):
        self.api.fAccList={'OTHER':object()}
        with self.assertRaisesRegex(VerificationError,'ACCOUNT_MISMATCH'):self.submit()
        self.assertEqual(self.sent,0)
    def test_duplicate_and_process_restart_cannot_resend(self):
        self.submit();self.service.db.close();self.service=VerificationService(self.session,self.root)
        with self.assertRaisesRegex(VerificationError,'ALREADY_ATTEMPTED'):self.submit()
        self.assertEqual(self.sent,1)
    def test_exception_after_transport_stays_unknown_and_cannot_resend(self):
        def fail(*args):self.sent+=1;raise TimeoutError('SECRET')
        self.api.send_website_verification=fail
        with self.assertRaisesRegex(VerificationError,'STATUS_UNKNOWN'):self.submit()
        with self.assertRaisesRegex(VerificationError,'ALREADY_ATTEMPTED'):self.submit()
        self.assertEqual(self.sent,1)
    def test_transport_acceptance_without_broker_report_is_unknown(self):
        self.api.send_website_verification=lambda *args:True
        self.assertEqual(self.submit()['attempt']['state'],'STATUS_UNKNOWN')
    def test_changed_or_expired_plan_cannot_submit(self):
        with self.assertRaisesRegex(VerificationError,'PLAN_CHANGED'):self.service.submit('0'*64,wait=0)
        self.plan['expires_at']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat();self.save()
        with self.assertRaisesRegex(VerificationError,'EXPIRED'):self.submit()
        self.assertEqual(self.sent,0)
    def test_unrelated_report_cannot_supply_an_order_number(self):
        self.api.send_website_verification=lambda *args:True;self.submit()
        self.service.on_report(SimpleNamespace(UserDefine='OTHER',OrderID='OTHER1',OrderStatus=2,CumQty=0))
        self.assertEqual(self.service.status()['attempt']['order_no'],'')
    def test_rejected_report_is_not_accepted(self):
        def reject(plan,account,tag):
            self.service.on_report(SimpleNamespace(UserDefine=tag,OrderID='REJ123',OrderStatus=11,CumQty=0));return True
        self.api.send_website_verification=reject
        self.assertEqual(self.submit()['attempt']['state'],'REJECTED')

    def test_expired_plan_keeps_accepted_receipt_without_allowing_resubmit(self):
        self.submit()
        self.plan['expires_at']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat();self.save()
        status=self.service.status()
        self.assertTrue(status['plan_expired']);self.assertEqual(status['attempt']['order_no'],'TEST123')
        self.assertEqual(status['attempt']['state'],'ACCEPTED')
        with self.assertRaisesRegex(VerificationError,'EXPIRED'):self.service.submit(status['plan_digest'],wait=0)
        self.assertEqual(self.sent,1)
    def test_expired_plan_without_attempt_is_readable_but_not_submittable(self):
        self.plan['expires_at']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat();self.save()
        status=self.service.status()
        self.assertTrue(status['plan_expired']);self.assertIsNone(status['attempt'])
        with self.assertRaisesRegex(VerificationError,'EXPIRED'):self.service.submit(status['plan_digest'],wait=0)
        self.assertEqual(self.sent,0)
        with self.assertRaisesRegex(VerificationError,'EXPIRED'):read_plan(self.root/'verification-plan.json')
    def test_matched_report_retains_actual_order_fields_without_identity_or_message(self):
        self.submit()
        tag='MV'+self.plan['plan_id'][:12]
        self.service.on_report(SimpleNamespace(UserDefine=tag,OrderID='TEST123',OrderStatus=2,CumQty=0,Symbol='TESTA9',Month='202611',OriginalQty=2,OrderQty=2,OrderPrice=100.0,BSType=1,PriType=2,TimeInForce=1,PositionEffect=0,AccNo7='PRIVATE-ID',Message='SECRET'))
        report=self.service.status()['attempt']['report']
        self.assertEqual(report['order_fields']['OriginalQty'],2)
        self.assertEqual(report['order_fields']['OrderPrice'],'100.0')
        self.assertEqual(report['order_fields']['TimeInForce'],1)
        self.assertNotIn('PRIVATE',str(report));self.assertNotIn('SECRET',str(report))
    def test_malformed_order_details_do_not_replace_valid_acceptance(self):
        self.submit()
        tag='MV'+self.plan['plan_id'][:12]
        self.service.on_report(SimpleNamespace(UserDefine=tag,OrderID='TEST123',OrderStatus=2,CumQty=0,Symbol='PRIVATE/SECRET',OrderPrice=float('nan'),OriginalQty=-1))
        status=self.service.status()['attempt']
        self.assertEqual(status['state'],'ACCEPTED');self.assertEqual(status['report']['order_fields'],{})
if __name__=='__main__':unittest.main()
