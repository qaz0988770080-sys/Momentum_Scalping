"""Futures read-only login. No order or inventory actions are exposed."""
import time,sys,threading,re
from pathlib import Path
from taishin_futures_com import pump_com_events
from taishin_futures_compat import auth_transport_ready,initialize_optional_callbacks,configure_tls12,summarize_authorization,summarize_sso,connection_setting_flags

BUILD='futures-login-20261028-r2'
DRIVER_REVISION='verification-report-audit-r19'
class FuturesLoginError(Exception):pass

def load_sdk():
    import logging
    logging.disable(logging.CRITICAL)
    from importlib.util import find_spec
    from taishin_futures_com import activate_speedy
    sdk_spec=find_spec('MasterFutTradePy')
    if sdk_spec is None:raise FuturesLoginError('FUTURES_SDK_MISSING')
    activate_speedy(Path(sdk_spec.origin).parent)
    import MasterFutTradePy
    import clr
    clr.AddReference(str(Path(MasterFutTradePy.__file__).parent / "NLog.dll"))
    import NLog
    recorder=None
    diagnostic_dll=Path(__file__).resolve().parent/'MomentumDiagnostics.r15.dll'
    if diagnostic_dll.is_file():
        clr.AddReference(str(diagnostic_dll))
        from MomentumDiagnostics import ExceptionRecorder
        recorder=ExceptionRecorder;recorder.Start()
    NLog.LogManager.Configuration=NLog.Config.LoggingConfiguration()
    from MasterFutTradePy import SolLog
    class SilentLog:
        observer=None
        def __init__(self,*args,**kwargs):self._run=False
        def Add(self,*args,**kwargs):
            if self.observer:
                value=kwargs.get('arg2',args[-1] if args else '')
                self.observer(value)
        def Wrtie_log(self,*args,**kwargs):self.Add(*args,**kwargs)
    SolLog.SolaceLog=SilentLog
    from taishin_futures_compat import install_windows_identity_decoder
    install_windows_identity_decoder()
    from MasterFutTradePy.MasterFutTradePY import MasterFutTradePY,TServerKind
    from MasterFutTradePy.constant import RCode
    tls_settings=configure_tls12()
    class LoginOnlyAPI(MasterFutTradePY):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            initialize_optional_callbacks(self)
            self._momentum_diagnostics={'tls':dict(tls_settings)}
            self._momentum_test_target=False
            self._momentum_recorder=recorder
        def CheckDAPIResult(self):
            if not auth_transport_ready(self):
                self.Fire_OnSystemEvent('MOMENTUM_AUTH_CHANNEL_NOT_READY')
                return RCode.FAIL
            return super().CheckDAPIResult()
        def TradeMgr_DoConn(self,connect,reply):
            if connect:
                summary=summarize_authorization(reply)
                self._momentum_diagnostics['authorization']=summary
                if self.fServerKind==TServerKind.Release and (
                        not summary['account_count'] or summary['status_counts']['approved']!=summary['account_count']):
                    self._momentum_diagnostics['formal_approval_required']=True
                    return False,'MOMENTUM_FORMAL_APPROVAL_REQUIRED'
                self._momentum_test_target=bool(self.fServerKind==TServerKind.Test and summary['result'] and summary['account_count']>0)
                self._momentum_diagnostics['test_target_requested']=self._momentum_test_target
                self._momentum_diagnostics['trade_login_started']=True
            if connect and self.fServerKind==TServerKind.Test:
                from taishin_futures_sso import legacy_sso_endpoint,broker_sso_url
                original=reply.ssoUrl
                with legacy_sso_endpoint(broker_sso_url(original),self._momentum_diagnostics,sdk_host=True) as endpoint:
                    reply.ssoUrl=endpoint
                    try:
                        result=super().TradeMgr_DoConn(connect,reply)
                        if self._momentum_recorder:self._momentum_recorder.ObserveConnection(self.fSpdyMgr)
                    finally:reply.ssoUrl=original
            else:result=super().TradeMgr_DoConn(connect,reply)
            if connect:
                self.capture_native_settings()
            return result
        def capture_native_settings(self):
            from System.Reflection import BindingFlags,Assembly
            assembly=Assembly.LoadFile(str(Path(MasterFutTradePy.__file__).parent/'MLApi_Fut_SpdySSO.dll'))
            typ=assembly.GetType('MLApi_Fut_SpdySSO.TTradeConnData')
            values={str(f.Name):f.GetValue(None) for f in typ.GetFields(BindingFlags.Static|BindingFlags.Public|BindingFlags.NonPublic) if str(f.Name) in {'IP','ProxyIP','SrvType','NeedCA','Port','LoginID','LoginPwd','BrokerID','ClearMemberID','OnCreateRun'}}
            self._momentum_diagnostics['native_settings']=connection_setting_flags(values)
            if self._momentum_recorder:
                self._momentum_diagnostics['native_connection_object_created']=bool(self._momentum_recorder.HasConnectionObject(self.fSpdyMgr))
                self._momentum_recorder.ObserveConnection(self.fSpdyMgr)
                manager=self._momentum_recorder.ManagerSnapshot(self.fSpdyMgr)
                self._momentum_diagnostics['native_manager_state']=dict(primary_logged_in=bool(manager.PrimaryLoggedIn),primary_recovery=str(manager.PrimaryRecovery),identity_count=int(manager.IdentityCount),identity_object_count=int(manager.IdentityObjectCount),identity_attempts=int(manager.IdentityAttempts),identity_states=[str(x) for x in manager.IdentityStates])
                observed=self._momentum_recorder.ConnectionSnapshot()
                self._momentum_diagnostics['native_transport_events']=dict(attached=bool(observed.Attached),connected_seen=bool(observed.ConnectedSeen),disconnected_seen=bool(observed.DisconnectedSeen),logon_seen=bool(observed.LogonSeen),logon_result=int(observed.LogonResult),logon_result_name=str(observed.ResultName),connection_id=int(observed.BrokerCode),recovery_seen=bool(observed.RecoverySeen),recovery_code=int(observed.RecoveryCode),attach_error=str(observed.AttachError))
                previous=self._momentum_diagnostics.get('managed_exceptions',[])
                for item in self._momentum_recorder.Take():
                    record=dict(kind=str(item.Kind),hresult=str(item.HResult),source=str(item.Source),member=str(item.Member))
                    if record not in previous:previous.append(record)
                self._momentum_diagnostics['managed_exceptions']=previous[-20:]
            values.clear()
            return dict(self._momentum_diagnostics['native_settings'])
        def probe_native_endpoint(self):
            import socket
            from System.Reflection import BindingFlags,Assembly
            typ=Assembly.LoadFile(str(Path(MasterFutTradePy.__file__).parent/'MLApi_Fut_SpdySSO.dll')).GetType('MLApi_Fut_SpdySSO.TTradeConnData')
            flags=BindingFlags.Static|BindingFlags.Public|BindingFlags.NonPublic
            options=self.capture_native_settings()
            if not options['broker_allows_connection'] or not options['Port_present']:return
            key='ProxyIP' if options['server_type']=='stSpdyProxy' else 'IP'
            if not options.get(key+'_present'):return
            host=str(typ.GetField(key,flags).GetValue(None))
            try:port=int(str(typ.GetField('Port',flags).GetValue(None)))
            except ValueError:
                self._momentum_diagnostics['endpoint_probe']='PORT_INVALID';return
            if not 1<=port<=65535:return
            try:
                connection=socket.create_connection((host,port),timeout=2);connection.close();status='TCP_REACHABLE'
            except socket.gaierror:status='DNS_FAILURE'
            except TimeoutError:status='TCP_TIMEOUT'
            except OSError:status='TCP_UNAVAILABLE'
            self._momentum_diagnostics['endpoint_probe']=status
        def DoGWWork(self,connect,reply):
            if connect and self._momentum_diagnostics.get('formal_approval_required'):
                return False,'MOMENTUM_FORMAL_APPROVAL_REQUIRED'
            return super().DoGWWork(connect,reply)
        def SpdyMgr_OnConnEvent(self,sender,connected):
            self._momentum_diagnostics['native_connection_callback']=dict(seen=True,connected=bool(connected))
            return super().SpdyMgr_OnConnEvent(sender,connected)
        def SpdyMgr_OnRecoverEvent(self,sender,finished,message):
            self._momentum_diagnostics['native_recovery_callback']=dict(seen=True,finished=bool(finished))
            return super().SpdyMgr_OnRecoverEvent(sender,finished,message)
        def SpdyMgr_OnSSOLoginResultEvent(self,sender,success,message):
            self._momentum_diagnostics['trade_sso']=summarize_sso(success,message)
            return super().SpdyMgr_OnSSOLoginResultEvent(sender,success,message)
        def GWQryMgr_OnSSOLoginResultEvent(self,sender,success,message):
            self._momentum_diagnostics['query_sso']=summarize_sso(success,message)
            return super().GWQryMgr_OnSSOLoginResultEvent(sender,success,message)
        def build_website_verification_message(self,plan):
            from MasterSpeedyAPI import MSpeedyAPI,SubNewOrderMessage
            from System import Double
            from System.Globalization import CultureInfo
            from decimal import Decimal
            import SpeedyAPI
            item=SubNewOrderMessage()
            item.AccountFlag='1';item.Market=SpeedyAPI.MarketEnum.mFutures
            item.Side=SpeedyAPI.SideEnum.sBuy;item.Symbol=plan['symbol'];item.OrderQty=plan['quantity']
            item.Price=Double.Parse(format(Decimal(plan['price']),'.8f'),CultureInfo.InvariantCulture)
            item.OrderType=SpeedyAPI.OrderTypeEnum.otLimit
            item.TimeInForce=SpeedyAPI.TimeInForceEnum.tifROD
            item.PositionEffect=SpeedyAPI.PositionEffectEnum.peOpen
            item.TradingSessionID=SpeedyAPI.TradingSessionIDEnum.tsAuto
            message=MSpeedyAPI.GetNewOrderMessage(item)
            if (str(message.Symbol)!=plan['symbol'] or int(message.OrderQty)!=plan['quantity']
                    or Decimal(str(message.Price))!=Decimal(plan['price'])
                    or int(message.Side)!=int(SpeedyAPI.SideEnum.sBuy)
                    or int(message.OrderType)!=int(SpeedyAPI.OrderTypeEnum.otLimit)
                    or int(message.TimeInForce)!=int(SpeedyAPI.TimeInForceEnum.tifROD)):
                raise FuturesLoginError('VERIFICATION_NATIVE_PAYLOAD_MISMATCH')
            return message
        def send_website_verification(self,plan,account,tag):
            # Ordinary SDK entrypoints remain blocked. This is test mode only.
            if (self.fServerKind!=TServerKind.Test or not self._momentum_test_target
                    or not self.IsConn_Trade or not self.IsRecoverDone):
                raise FuturesLoginError('VERIFICATION_TEST_CONNECTION_REQUIRED')
            if plan['side']!='BUY' or plan['price_type']!='LMT' or plan['tif']!='ROD':
                raise FuturesLoginError('VERIFICATION_PLAN_INVALID_OR_EXPIRED')
            message=self.build_website_verification_message(plan)
            accepted,_=MasterFutTradePY.SendNewOrder(self,message,account,tag,'')
            return bool(accepted)
        def SendNewOrder(self,*args,**kwargs):raise FuturesLoginError('FUTURES_ORDERS_DISABLED')
        def SendDelOrder(self,*args,**kwargs):raise FuturesLoginError('FUTURES_ORDERS_DISABLED')
        def SendChgOrder(self,*args,**kwargs):raise FuturesLoginError('FUTURES_ORDERS_DISABLED')
    LoginOnlyAPI._ms_logger_class=SilentLog
    return LoginOnlyAPI,{'validation':TServerKind.Test,'formal':TServerKind.Release}

class ValidationSession:
    def __init__(self, loader=load_sdk):
        self.loader=loader;self.api=None;self.diagnostics={};self.environment='validation';self.events=[];self.sdk_exceptions=[];self.event_count=0;self.report_observer=None;self._recovery_requested=False
    def report(self,value):
        if self.report_observer:self.report_observer(value)
    def collect_native_diagnostics(self):
        api=self.api
        if (api and not self._recovery_requested and self.environment=='validation'
                and getattr(api,'_momentum_test_target',False) and api.IsConn_Trade
                and not api.IsRecoverDone):
            # SDK recovery is explicit. Never infer completion or repeat on polling.
            recover=getattr(api,'DoRecover',None)
            if callable(recover):
                self._recovery_requested=True
                stages=getattr(api,'_momentum_diagnostics',None)
                if stages is not None:stages['recovery_requested']=True
                try:recover()
                except Exception as exc:
                    if stages is not None:stages['recovery_request_error']=type(exc).__name__
        capture=getattr(self.api,'capture_native_settings',None)
        if capture:capture()
    def snapshot(self):
        self.collect_native_diagnostics()
        api=self.api
        return dict(trade_connected=bool(api and api.IsConn_Trade),
            recovery_complete=bool(api and api.IsRecoverDone),
            account_count=len(api.fAccList) if api else 0,
            environment=self.environment,auth_transport_connected=auth_transport_ready(api),system_events=list(self.events),sdk_event_count=self.event_count,sdk_exceptions=list(self.sdk_exceptions),
            connection_stages=dict(getattr(api,'_momentum_diagnostics',{})),
            driver_revision=DRIVER_REVISION,order_enabled=False,live_enabled=False)
    def event(self,message):
        self.event_count+=1
        text=str(getattr(message,'message',message)).lower()
        if text=='momentum_auth_channel_not_ready':self.events=(self.events+['AUTH_CHANNEL_NOT_READY'])[-12:]
        if '[initapisetting]' in text:self.events=(self.events+['SDK_INITIALIZATION_FAILED'])[-12:]
        for markers,category in ((('80040154','-2147221164','class not registered'),'COMPONENT_NOT_REGISTERED'),
                (('sso檢核失敗',),'SSO_LOGIN_FAILED'),
                (('未驗證','驗證失敗'),'AUTHORIZATION_NOT_READY'),
                (('無有效憑證','憑證失敗'),'CERTIFICATE_NOT_READY'),
                (('no response','timed out','timeout'),'CONNECTION_TIMEOUT')):
            if any(marker in text for marker in markers):
                self.events=(self.events+[category])[-12:];break
    def trace(self,frame,event,arg):
        if event=='exception' and Path(frame.f_code.co_filename).name in {'MasterFutTradePY.py','SolClient.py'}:
            kind,exc,_=arg
            if kind not in {StopIteration,GeneratorExit}:
                item=dict(kind=kind.__name__,function=frame.f_code.co_name,line=frame.f_lineno,
                    hresult=hex(getattr(exc,'HResult',0)&0xffffffff))
                name=getattr(exc,'name',None)
                if isinstance(name,str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.]{0,99}',name):item['identifier']=name
                if item not in self.sdk_exceptions:self.sdk_exceptions=(self.sdk_exceptions+[item])[-20:]
        return self.trace
    def login(self,username,password,environment='validation',timeout=35):
        if environment not in {'validation','formal'}:raise FuturesLoginError('FUTURES_ENVIRONMENT_INVALID')
        if self.api is not None:raise FuturesLoginError('FUTURES_RESTART_REQUIRED')
        self.environment=environment
        API,modes=self.loader()
        if hasattr(API,'_ms_logger_class'):API._ms_logger_class.observer=self.event
        self.api=API();self.api.OnSystemEvent=self.event;self.api.OnResultReportEvent=self.report
        old=sys.gettrace();old_thread=threading.gettrace()
        sys.settrace(self.trace);threading.settrace(self.trace)
        try:
            self.api.Connect(username,password,modes[environment],False)
            deadline=time.monotonic()+timeout
            while True:
                pump_com_events()
                self.diagnostics=self.snapshot()
                stages=self.diagnostics['connection_stages']
                if stages.get('formal_approval_required'):raise FuturesLoginError('FUTURES_FORMAL_APPROVAL_REQUIRED')
                if stages.get('trade_sso',{}).get('ok') is False:raise FuturesLoginError('FUTURES_TRADE_SSO_FAILED')
                if (self.diagnostics['trade_connected'] and self.diagnostics['recovery_complete']
                        and self.diagnostics['account_count']>0):
                    return dict(ok=True,connected=True,environment=environment,accounts=[],
                        readonly=True,live_enabled=False,order_enabled=False,diagnostics=self.diagnostics)
                if 'AUTH_CHANNEL_NOT_READY' in self.events:raise FuturesLoginError('FUTURES_AUTH_CHANNEL_NOT_READY')
                if 'SDK_INITIALIZATION_FAILED' in self.events:raise FuturesLoginError('FUTURES_SDK_INITIALIZATION_FAILED')
                if time.monotonic()>=deadline:
                    probe=getattr(self.api,'probe_native_endpoint',None)
                    if probe and self.environment=='validation':probe()
                    raise FuturesLoginError('FUTURES_LOGIN_NOT_READY')
                time.sleep(.05)
        finally:
            sys.settrace(old);threading.settrace(old_thread)
    def close(self):
        if self.api is not None:
            try:self.api.Disconnect()
            except Exception:pass
            for name in ('fLoginID','fLoginPwd','fLoginMd5Pwd'):
                try:setattr(self.api,name,'')
                except Exception:pass
            self.api=None
