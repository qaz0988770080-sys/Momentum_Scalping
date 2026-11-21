import io,json,queue,unittest
from types import SimpleNamespace
from taishin_readonly_web import WorkerBridge

class SessionReconnectTests(unittest.TestCase):
    def make(self):
        bridge=WorkerBridge('.',worker_script='taishin_futures_worker.py')
        bridge._start=lambda:None;bridge._stop=lambda:None
        bridge.process=SimpleNamespace(stdin=io.StringIO())
        return bridge
    def response(self,bridge,authorized=True,connected=False):
        bridge.messages.put(dict(kind='login',ok=connected,connected=connected,environment='validation',diagnostics=dict(auth_transport_connected=True,connection_stages=dict(authorization=dict(result=authorized,account_count=1)))))
    def login(self,bridge,remember=False,environment='validation'):
        self.response(bridge)
        return bridge.request(dict(cmd='login',username='SYNTHETIC-ID',password='SYNTHETIC-SECRET',environment=environment,remember_session=remember))
    def test_no_opt_in_means_no_cached_credentials(self):
        b=self.make();result=self.login(b);self.assertIsNone(b._session_credentials);self.assertFalse(result['reconnect_available'])
    def test_opt_in_keeps_memory_only_and_state_never_contains_credentials(self):
        b=self.make();result=self.login(b,True);self.assertTrue(result['reconnect_available'])
        self.assertNotIn('SYNTHETIC',json.dumps(b.state()))
        sent=json.loads(b.process.stdin.getvalue());self.assertNotIn('remember_session',sent)
    def test_failed_identity_and_formal_mode_are_not_cached(self):
        b=self.make();self.response(b,False)
        b.request(dict(cmd='login',username='SYNTHETIC',password='SECRET',environment='validation',remember_session=True))
        self.assertIsNone(b._session_credentials)
        b=self.make();self.login(b,True,'formal');self.assertIsNone(b._session_credentials)
    def test_explicit_reconnect_uses_same_session_without_exposing_secret(self):
        b=self.make();self.login(b,True);self.response(b,True,True)
        result=b.reconnect();self.assertTrue(result['connected']);self.assertTrue(result['reconnect_available'])
        commands=[json.loads(line) for line in b.process.stdin.getvalue().splitlines()]
        self.assertEqual(len(commands),2);self.assertEqual(commands[1]['environment'],'validation')
        self.assertNotIn('_session_reconnect',commands[1]);self.assertNotIn('SYNTHETIC',json.dumps(result))
    def test_logout_and_close_clear_cached_credentials(self):
        b=self.make();self.login(b,True);b.messages.put(dict(kind='logout',ok=True,connected=False))
        b.request(dict(cmd='logout'));self.assertIsNone(b._session_credentials)
        b=self.make();self.login(b,True);b.close();self.assertIsNone(b._session_credentials)
    def test_reconnect_without_opt_in_is_rejected(self):
        b=self.make();result=b.reconnect();self.assertEqual(result['error'],'SESSION_RECONNECT_NOT_AVAILABLE');self.assertEqual(b.process.stdin.getvalue(),'')

if __name__=='__main__':unittest.main()
