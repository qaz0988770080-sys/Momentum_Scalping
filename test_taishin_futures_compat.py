import unittest
from taishin_futures_compat import read_session_property,auth_transport_ready,initialize_optional_callbacks,OPTIONAL_CALLBACKS
from types import SimpleNamespace
class WindowsIdentityDecodeTests(unittest.TestCase):
    def broken(self,instance,name):
        return '系統使用者'.encode('cp950').decode('utf-8')
    def test_windows_metadata_uses_strict_local_codec(self):
        result=read_session_property(self.broken,None,'SESSION_USER_ID',platform='nt',decode_windows=lambda raw:raw.decode('cp950'))
        self.assertEqual(result,'系統使用者')
    def test_valid_utf8_is_not_changed(self):
        result=read_session_property(lambda a,b:'valid-value',None,'SESSION_USER_ID',platform='nt',decode_windows=lambda raw:self.fail('fallback should not run'))
        self.assertEqual(result,'valid-value')
    def test_market_and_authentication_fields_never_use_fallback(self):
        for key in ('SESSION_USERNAME','SESSION_PASSWORD','SESSION_HOST','PRICE','ORDER','unknown'):
            with self.subTest(key=key),self.assertRaises(UnicodeDecodeError):
                read_session_property(self.broken,None,key,platform='nt',decode_windows=lambda raw:self.fail('must not decode broker fields'))
    def test_non_windows_behavior_unchanged(self):
        with self.assertRaises(UnicodeDecodeError):read_session_property(self.broken,None,'SESSION_USER_ID',platform='posix')
    def test_invalid_local_bytes_still_fail_closed(self):
        with self.assertRaises(UnicodeDecodeError):
            read_session_property(self.broken,None,'SESSION_USER_ID',platform='nt',decode_windows=lambda raw:raw.decode('ascii'))
    def test_uninitialized_auth_transport_cannot_be_used(self):
        self.assertFalse(auth_transport_ready(None))
        self.assertFalse(auth_transport_ready(SimpleNamespace(_SolAuthClient=SimpleNamespace(_is_connected=True))))
    def test_disconnected_transport_cannot_be_used(self):
        api=SimpleNamespace(_SolAuthClient=SimpleNamespace(_is_connected=False,_message_service=object()))
        self.assertFalse(auth_transport_ready(api))
    def test_initialized_connected_transport_is_ready(self):
        api=SimpleNamespace(_SolAuthClient=SimpleNamespace(_is_connected=True,_message_service=object()))
        self.assertTrue(auth_transport_ready(api))
    def test_missing_optional_handlers_become_callable_without_faking_events(self):
        api=SimpleNamespace(IsConn_Trade=False,IsRecoverDone=False,fAccList={})
        initialize_optional_callbacks(api)
        for name in OPTIONAL_CALLBACKS:
            callback=getattr(api,name);self.assertTrue(callable(callback));self.assertIsNone(callback(None,None,None))
        self.assertFalse(api.IsConn_Trade);self.assertFalse(api.IsRecoverDone);self.assertEqual(api.fAccList,{})
    def test_existing_handlers_are_preserved(self):
        handler=lambda *args:'real-handler'
        api=SimpleNamespace(OnRecoverEvent=handler);initialize_optional_callbacks(api)
        self.assertIs(api.OnRecoverEvent,handler)
    def test_invalid_handler_is_rejected_before_connect(self):
        with self.assertRaisesRegex(TypeError,'FUTURES_CALLBACK_NOT_CALLABLE'):
            initialize_optional_callbacks(SimpleNamespace(OnTxConnEvent='invalid'))
class ConnectionDiagnosticsTests(unittest.TestCase):
    def test_tls12_changes_protocol_but_retains_certificate_validation(self):
        from types import SimpleNamespace
        from taishin_futures_compat import configure_tls12
        manager=SimpleNamespace(SecurityProtocol='Ssl3, Tls',ServerCertificateValidationCallback=None)
        result=configure_tls12(manager,'Tls12')
        self.assertEqual(result,{'protocol':'Tls12','custom_certificate_validator':False})
        self.assertIsNone(manager.ServerCertificateValidationCallback)
    def test_authorization_summary_excludes_account_identifiers(self):
        from types import SimpleNamespace
        from taishin_futures_compat import summarize_authorization
        value=summarize_authorization(SimpleNamespace(result=True,list=[{'account':'PRIVATE-TEST','statusCode':3},{'account':'SECRET','statusCode':2}]))
        self.assertEqual(value['status_counts']['approved'],1)
        self.assertEqual(value['status_counts']['verified'],1)
        self.assertNotIn('PRIVATE',str(value));self.assertNotIn('SECRET',str(value))
    def test_sso_failure_keeps_only_fixed_reason_categories(self):
        from taishin_futures_compat import summarize_sso
        self.assertEqual(summarize_sso(False,'PRIVATE-TEST password=SECRET 無法建立 SSL 安全通道'),{'ok':False,'reasons':['TLS_FAILURE']})
        self.assertEqual(summarize_sso(False,'SECRET arbitrary detail'),{'ok':False,'reasons':['SSO_REJECTED_UNCLASSIFIED']})
        self.assertEqual(summarize_sso(True,'SECRET'),{'ok':True,'reasons':[]})

class PrivateComManifestTests(unittest.TestCase):
    def test_manifest_maps_only_official_speedy_file_and_unique_classes(self):
        import xml.etree.ElementTree as ET
        from taishin_futures_com import speedy_manifest,SPEEDY_CLASSES
        root=ET.fromstring(speedy_manifest());ns={'a':'urn:schemas-microsoft-com:asm.v1'}
        files=root.findall('a:file',ns);self.assertEqual([f.attrib['name'] for f in files],['SpeedyAPI.dll'])
        self.assertEqual(len(SPEEDY_CLASSES),len(set(SPEEDY_CLASSES)))
        self.assertTrue(all(c.attrib['threadingModel']=='Apartment' for c in files[0]))
        self.assertIn('019e6568-9a7e-495d-9b3e-4e280a25bd68',SPEEDY_CLASSES)

if __name__=='__main__':unittest.main()
