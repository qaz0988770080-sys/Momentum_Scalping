import unittest
from types import SimpleNamespace
from taishin_futures_validation import ValidationSession,FuturesLoginError
class FakeAPI:
    def __init__(self):
        self.IsConn_Trade=True;self.IsRecoverDone=True;self.fAccList={'TEST':object()};self.calls=[]
    def Connect(self,*args):self.calls.append(args)
    def Disconnect(self):pass
class FuturesValidationTests(unittest.TestCase):
    def make(self,**flags):
        api=FakeAPI()
        for key,value in flags.items():setattr(api,key,value)
        return ValidationSession(lambda:(lambda:api,{'validation':'TEST_ENUM','formal':'RELEASE_ENUM'})),api
    def test_invalid_environment_rejected_before_sdk_load(self):
        def forbidden():self.fail('SDK must not load for formal environment')
        with self.assertRaisesRegex(FuturesLoginError,'FUTURES_ENVIRONMENT_INVALID'):
            ValidationSession(forbidden).login('FAKE','SECRET','unknown',timeout=0)
    def test_connect_uses_only_test_and_never_force(self):
        session,api=self.make();result=session.login('FAKE','SECRET',timeout=0)
        self.assertEqual(api.calls,[('FAKE','SECRET','TEST_ENUM',False)])
        self.assertTrue(result['connected']);self.assertFalse(result['live_enabled']);self.assertFalse(result['order_enabled'])
        self.assertNotIn('SECRET',str(result));self.assertNotIn('TEST',str(result))
    def test_disconnected_not_ready(self):
        session,_=self.make(IsConn_Trade=False)
        with self.assertRaisesRegex(FuturesLoginError,'FUTURES_LOGIN_NOT_READY'):session.login('FAKE','SECRET',timeout=0)
    def test_missing_recovery_not_ready(self):
        session,_=self.make(IsRecoverDone=False)
        with self.assertRaisesRegex(FuturesLoginError,'FUTURES_LOGIN_NOT_READY'):session.login('FAKE','SECRET',timeout=0)
    def test_empty_accounts_not_ready(self):
        session,_=self.make(fAccList={})
        with self.assertRaisesRegex(FuturesLoginError,'FUTURES_LOGIN_NOT_READY'):session.login('FAKE','SECRET',timeout=0)
    def test_formal_login_keeps_every_order_disabled(self):
        session,api=self.make();result=session.login('FAKE','SECRET','formal',timeout=0)
        self.assertEqual(api.calls[0][2:],('RELEASE_ENUM',False))
        self.assertEqual(result['environment'],'formal')
        self.assertTrue(result['readonly']);self.assertFalse(result['live_enabled']);self.assertFalse(result['order_enabled'])
    def test_system_events_keep_categories_without_raw_details(self):
        session,_=self.make();session.event('FAKE-SECRET FAKE-ACCOUNT SSO檢核失敗')
        self.assertEqual(session.events,['SSO_LOGIN_FAILED'])
        self.assertNotIn('FAKE',str(session.snapshot()))
    def test_sdk_exception_trace_keeps_types_not_exception_messages(self):
        session,_=self.make()
        frame=SimpleNamespace(f_lineno=1,f_code=SimpleNamespace(co_filename='MasterFutTradePY.py',co_name='Connect_Auth'))
        error=NameError('FAKE-SECRET FAKE-ACCOUNT');error.name='known_identifier'
        session.trace(frame,'exception',(NameError,error,None))
        self.assertEqual(session.sdk_exceptions[0]['kind'],'NameError')
        self.assertEqual(session.sdk_exceptions[0]['identifier'],'known_identifier')
        self.assertNotIn('FAKE',str(session.snapshot()))
    def test_trade_sso_failure_is_specific_and_not_success(self):
        session,_=self.make(_momentum_diagnostics={'trade_sso':{'ok':False,'reasons':['TLS_FAILURE']}})
        with self.assertRaisesRegex(FuturesLoginError,'FUTURES_TRADE_SSO_FAILED'):
            session.login('FAKE','SECRET','formal',timeout=0)
    def test_formal_approval_failure_never_becomes_success(self):
        session,_=self.make(_momentum_diagnostics={'formal_approval_required':True})
        with self.assertRaisesRegex(FuturesLoginError,'FUTURES_FORMAL_APPROVAL_REQUIRED'):
            session.login('FAKE','SECRET','formal',timeout=0)
    def test_duplicate_login_requires_new_worker(self):
        session,_=self.make();session.login('FAKE','SECRET',timeout=0)
        with self.assertRaisesRegex(FuturesLoginError,'FUTURES_RESTART_REQUIRED'):session.login('FAKE','SECRET',timeout=0)
    def test_recovery_requested_once_after_real_test_connection(self):
        session,api=self.make(IsConn_Trade=False,IsRecoverDone=False,_momentum_test_target=True,_momentum_diagnostics={})
        requested=[];api.DoRecover=lambda:requested.append('recover')
        session.api=api
        session.snapshot();self.assertEqual(requested,[])
        api.IsConn_Trade=True
        session.snapshot();session.snapshot()
        self.assertEqual(requested,['recover']);self.assertFalse(api.IsRecoverDone)
        self.assertTrue(api._momentum_diagnostics['recovery_requested'])
        api.IsRecoverDone=True
        self.assertTrue(session.snapshot()['recovery_complete'])
    def test_recovery_not_requested_for_unproven_test_or_formal(self):
        session,api=self.make(IsRecoverDone=False,_momentum_test_target=False)
        api.DoRecover=lambda:self.fail('unexpected recovery');session.api=api
        session.snapshot();api._momentum_test_target=True;session.environment='formal';session.snapshot()
    def test_recovery_failure_is_sanitized_and_not_retried(self):
        session,api=self.make(IsRecoverDone=False,_momentum_test_target=True,_momentum_diagnostics={})
        def fail():raise RuntimeError('PRIVATE-SECRET')
        api.DoRecover=fail;session.api=api;session.snapshot()
        api.DoRecover=lambda:self.fail('must not retry');state=session.snapshot()
        self.assertEqual(state['connection_stages']['recovery_request_error'],'RuntimeError')
        self.assertNotIn('PRIVATE',str(state));self.assertFalse(state['recovery_complete'])
if __name__=='__main__':unittest.main()
