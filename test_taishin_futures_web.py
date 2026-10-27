import http.client,json,re,threading,unittest,uuid
from pathlib import Path
from taishin_readonly_web import LoopbackServer,make_handler
class FuturesPlanPrivacyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root=Path(__file__).parent/'.test-artifacts'/('futures-web-'+uuid.uuid4().hex)
        (cls.root/'private').mkdir(parents=True)
        (cls.root/'taishin_futures.html').write_text('const csrf="__CSRF__"',encoding='utf-8')
        (cls.root/'private'/'futures-plan.json').write_text(json.dumps(dict(symbol='SRF',reported_lots=7,side='BUY',quantity=1,price_type='MKT',month_hint='TEST-MONTH',password='FAKE-SECRET',account='FAKE-ACCOUNT')),encoding='utf-8')
        cls.server=LoopbackServer(('127.0.0.1',0),make_handler(cls.root,None,'taishin_futures.html'))
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(timeout=2)
    def call(self,path,cookie=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        try:
            conn.request('GET',path,headers={'Cookie':cookie} if cookie else {})
            result=conn.getresponse();return result.status,dict(result.getheaders()),result.read()
        finally:conn.close()
    def test_plan_requires_local_authenticated_session(self):
        self.assertEqual(self.call('/api/futures-plan')[0],403)
    def test_only_intent_fields_returned_never_credentials(self):
        _,headers,_=self.call('/');cookie=headers['Set-Cookie'].split(';')[0]
        status,_,body=self.call('/api/futures-plan',cookie)
        self.assertEqual(status,200);self.assertNotIn(b'FAKE-SECRET',body);self.assertNotIn(b'FAKE-ACCOUNT',body)
        data=json.loads(body);self.assertFalse(data['broker_verified']);self.assertEqual(data['source'],'user_instruction')
        self.assertEqual(data['reported_lots'],7)
    def test_unknown_paths_cannot_read_private_file(self):
        _,headers,_=self.call('/');cookie=headers['Set-Cookie'].split(';')[0]
        self.assertEqual(self.call('/private/futures-plan.json',cookie)[0],404)
if __name__=='__main__':unittest.main()
