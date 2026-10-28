import base64,json,http.client,unittest
from urllib.parse import urlsplit
from taishin_futures_sso import normalize_sso_reply,legacy_sso_endpoint,SsoFormatError

class SsoFormattingTests(unittest.TestCase):
    def body(self,data=None):
        self.ini=data if data is not None else (chr(13)+chr(10)).join(['[SYSTEM]','IP=127.0.0.1','LoginID=SYNTHETIC','LoginPwd=SECRET','OnCreateRun=N',''])
        self.payload={'accounts':[{'account':'PRIVATE-ACCOUNT'}],'iniFile':[{'data':self.ini,'fname':'SYS_SPDY.ini'}]}
        return json.dumps({'result':True,'user_data':base64.b64encode(json.dumps(self.payload,indent=2).encode()).decode()}).encode()
    def test_only_format_changes_and_ini_values_stay_identical(self):
        before=self.body();after,flags=normalize_sso_reply(before)
        parsed=json.loads(after);payload=json.loads(base64.b64decode(parsed['user_data']),strict=False)
        self.assertEqual(payload,self.payload);self.assertTrue(parsed['result'])
        self.assertTrue(flags['changed']);self.assertTrue(flags['config_present'])
        self.assertNotIn('SECRET',str(flags));self.assertNotIn('PRIVATE',str(flags))
        decoded=base64.b64decode(parsed['user_data']).decode()
        self.assertIn('"fname":"SYS_SPDY.ini","data":"[SYSTEM]'+chr(13)+chr(10),decoded)
    def test_chinese_comments_preserved_in_native_windows_encoding(self):
        before=self.body('[SYSTEM]'+chr(13)+chr(10)+';測試註解'+chr(13)+chr(10)+'IP=127.0.0.1')
        after,_=normalize_sso_reply(before,native_encoding='cp950')
        decoded=base64.b64decode(json.loads(after)['user_data']).decode('cp950')
        payload=json.loads(decoded,strict=False)
        self.assertEqual(payload['iniFile'][0]['data'],self.ini)
    def test_without_config_is_not_fabricated(self):
        before=b'{"result":false}'
        self.assertEqual(normalize_sso_reply(before),(before,{'changed':False,'config_present':False}))
    def test_unsupported_ini_value_rejected_instead_of_changed(self):
        with self.assertRaises(SsoFormatError):normalize_sso_reply(self.body('IP="127.0.0.1"'))
    def test_duplicate_config_rejected(self):
        self.body();self.payload['iniFile']*=2
        body=json.dumps({'user_data':base64.b64encode(json.dumps(self.payload).encode()).decode()}).encode()
        with self.assertRaises(SsoFormatError):normalize_sso_reply(body)
    def test_proxy_preserves_query_and_has_no_public_route(self):
        body=self.body();received=[];diagnostics={}
        def fetch(query):received.append(query);return body
        with legacy_sso_endpoint('https://broker.invalid/sso?token=SYNTHETIC',diagnostics,fetch) as local:
            url=urlsplit(local)
            client=http.client.HTTPConnection(url.hostname,url.port,timeout=3)
            client.request('GET','/wrong');response=client.getresponse();self.assertEqual(response.status,404);response.read();client.close()
            self.assertEqual(received,[])
            client=http.client.HTTPConnection(url.hostname,url.port,timeout=3)
            client.request('GET',url.path+'?'+url.query);response=client.getresponse();self.assertEqual(response.status,200);response.read();client.close()
        self.assertEqual(received,['token=SYNTHETIC']);self.assertEqual(diagnostics,{'sso_format_compatibility':{'changed':True,'config_present':True}})
    def test_proxy_never_returns_upstream_error_details(self):
        diagnostics={}
        def fail(query):raise RuntimeError('SECRET-PRIVATE')
        with legacy_sso_endpoint('https://broker.invalid/sso?',diagnostics,fail) as local:
            url=urlsplit(local);client=http.client.HTTPConnection(url.hostname,url.port,timeout=3);client.request('GET',url.path+'?');response=client.getresponse();data=response.read();client.close()
            self.assertEqual(response.status,502);self.assertNotIn(b'SECRET',data)
        self.assertNotIn('SECRET',str(diagnostics));self.assertEqual(diagnostics['sso_format_compatibility']['error'],'SSO_FORMAT_RuntimeError')

if __name__=='__main__':unittest.main()
