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
    def test_source_shape_distinguishes_empty_value_without_exposing_it(self):
        body=self.body('[SYSTEM]'+chr(10)+'IP='+chr(10)+'Port=9001'+chr(10)+'LoginID='+chr(10)+'OnCreateRun=Y')
        output,flags=normalize_sso_reply(body)
        shape=flags['source_shape'];self.assertTrue(shape['system_section_present']);self.assertTrue(shape['ip_key_present']);self.assertFalse(shape['ip_value_present']);self.assertEqual(shape['line_endings'],'LF')
        data=json.loads(base64.b64decode(json.loads(output)['user_data']).decode(),strict=False)['iniFile'][0]['data']
        self.assertIn(chr(13)+chr(10),data);self.assertEqual(data.splitlines(),self.ini.splitlines())
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
        self.assertEqual(received,['token=SYNTHETIC']);self.assertTrue(diagnostics['sso_format_compatibility']['changed']);self.assertTrue(diagnostics['sso_format_compatibility']['config_present']);self.assertNotIn('SYNTHETIC',str(diagnostics))
    def test_native_host_contract_matches_sdk_generated_url(self):
        from taishin_futures_sso import broker_sso_url
        self.assertEqual(broker_sso_url('broker.invalid:8080'),'http://broker.invalid:8080/tc4grpright/getSsoClassID4Api.jsp?')
        with self.assertRaises(SsoFormatError):broker_sso_url('https://broker.invalid/')
        body=self.body();received=[]
        with legacy_sso_endpoint(broker_sso_url('broker.invalid'),{},lambda query:(received.append(query) or body),sdk_host=True) as host:
            native=urlsplit('http://'+host+'/tc4grpright/getSsoClassID4Api.jsp?TradingAccount=SYNTHETIC&password=FAKE')
            client=http.client.HTTPConnection(native.hostname,native.port,timeout=3);client.request('GET',native.path+'?'+native.query)
            response=client.getresponse();self.assertEqual(response.status,200);response.read();client.close()
        self.assertEqual(received,['TradingAccount=SYNTHETIC&password=FAKE'])
    def test_proxy_never_returns_upstream_error_details(self):
        diagnostics={}
        def fail(query):raise RuntimeError('SECRET-PRIVATE')
        with legacy_sso_endpoint('https://broker.invalid/sso?',diagnostics,fail) as local:
            url=urlsplit(local);client=http.client.HTTPConnection(url.hostname,url.port,timeout=3);client.request('GET',url.path+'?');response=client.getresponse();data=response.read();client.close()
            self.assertEqual(response.status,502);self.assertNotIn(b'SECRET',data)
        self.assertNotIn('SECRET',str(diagnostics));self.assertEqual(diagnostics['sso_format_compatibility']['error'],'SSO_FORMAT_RuntimeError')

if __name__=='__main__':unittest.main()
