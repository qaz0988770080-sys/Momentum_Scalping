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

class VerificationRouteTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(__file__).parent/'.test-artifacts'/('verify-route-'+uuid.uuid4().hex);self.root.mkdir(parents=True)
        for name in ('taishin_futures.html','taishin_readonly.html'):(self.root/name).write_text('const csrf="__CSRF__"',encoding='utf-8')
        class Bridge:
            def __init__(self):self.calls=[]
            def request(self,value):self.calls.append(dict(value));return dict(ok=True)
        self.bridge=Bridge();self.servers=[]
    def tearDown(self):
        for server,thread in self.servers:server.shutdown();server.server_close();thread.join(timeout=2)
    def start(self,page='taishin_futures.html'):
        server=LoopbackServer(('127.0.0.1',0),make_handler(self.root,self.bridge,page))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();self.servers.append((server,thread))
        conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3);conn.request('GET','/');response=conn.getresponse()
        cookie=response.getheader('Set-Cookie').split(';')[0];csrf=re.search(rb'const csrf="([^"]+)"',response.read()).group(1).decode();conn.close()
        return server,cookie,csrf
    def post(self,server,cookie,csrf,data):
        conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
        conn.request('POST','/api/verification-submit',body=json.dumps(data),headers={'Cookie':cookie,'X-Momentum-CSRF':csrf,'Origin':'http://127.0.0.1:'+str(server.server_port),'Content-Type':'application/json'})
        response=conn.getresponse();status=response.status;response.read();conn.close();return status
    def test_verification_route_keeps_only_plan_digest(self):
        server,cookie,csrf=self.start()
        self.assertEqual(self.post(server,cookie,csrf,{'plan_digest':'a'*64,'symbol':'EVIL','quantity':99999}),200)
        self.assertEqual(self.bridge.calls,[{'cmd':'verification_submit','plan_digest':'a'*64}])
    def test_stock_server_never_exposes_verification_submit(self):
        server,cookie,csrf=self.start('taishin_readonly.html')
        self.assertEqual(self.post(server,cookie,csrf,{'plan_digest':'a'*64}),404);self.assertEqual(self.bridge.calls,[])
    def test_verification_submit_requires_csrf(self):
        server,cookie,csrf=self.start()
        self.assertEqual(self.post(server,cookie,'WRONG',{'plan_digest':'a'*64}),403);self.assertEqual(self.bridge.calls,[])
    def test_invalid_plan_digest_rejected_before_worker(self):
        server,cookie,csrf=self.start()
        self.assertEqual(self.post(server,cookie,csrf,{'plan_digest':'NOT-A-DIGEST'}),400);self.assertEqual(self.bridge.calls,[])

if __name__=='__main__':unittest.main()
