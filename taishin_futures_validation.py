"""Futures read-only login. No order or inventory actions are exposed."""
import time,sys,threading,re
from pathlib import Path

BUILD='futures-login-20261028-r2'
class FuturesLoginError(Exception):pass

def load_sdk():
    import logging
    logging.disable(logging.CRITICAL)
    import MasterFutTradePy
    import clr
    from pathlib import Path
    clr.AddReference(str(Path(MasterFutTradePy.__file__).parent / "NLog.dll"))
    import NLog
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
    from MasterFutTradePy.MasterFutTradePY import MasterFutTradePY,TServerKind
    class LoginOnlyAPI(MasterFutTradePY):
        def SendNewOrder(self,*args,**kwargs):raise FuturesLoginError('FUTURES_ORDERS_DISABLED')
        def SendDelOrder(self,*args,**kwargs):raise FuturesLoginError('FUTURES_ORDERS_DISABLED')
        def SendChgOrder(self,*args,**kwargs):raise FuturesLoginError('FUTURES_ORDERS_DISABLED')
    LoginOnlyAPI._ms_logger_class=SilentLog
    return LoginOnlyAPI,{'validation':TServerKind.Test,'formal':TServerKind.Release}

class ValidationSession:
    def __init__(self, loader=load_sdk):
        self.loader=loader;self.api=None;self.diagnostics={};self.environment='validation';self.events=[];self.sdk_exceptions=[];self.event_count=0
    def snapshot(self):
        api=self.api
        return dict(trade_connected=bool(api and api.IsConn_Trade),
            recovery_complete=bool(api and api.IsRecoverDone),
            account_count=len(api.fAccList) if api else 0,
            environment=self.environment,system_events=list(self.events),sdk_event_count=self.event_count,sdk_exceptions=list(self.sdk_exceptions),
            driver_revision='diagnostics-r3',order_enabled=False,live_enabled=False)
    def event(self,message):
        self.event_count+=1
        text=str(getattr(message,'message',message)).lower()
        if '[initapisetting]' in text:self.events=(self.events+['SDK_INITIALIZATION_FAILED'])[-12:]
        for markers,category in ((('80040154','-2147221164','class not registered'),'COMPONENT_NOT_REGISTERED'),
                (('未驗證','驗證失敗','sso檢核失敗'),'AUTHORIZATION_NOT_READY'),
                (('無有效憑證','憑證失敗'),'CERTIFICATE_NOT_READY'),
                (('no response','timed out','timeout'),'CONNECTION_TIMEOUT')):
            if any(marker in text for marker in markers):
                self.events=(self.events+[category])[-12:];break
    def trace(self,frame,event,arg):
        if event=='exception' and Path(frame.f_code.co_filename).name in {'MasterFutTradePY.py','SolClient.py'}:
            kind,exc,_=arg
            if kind not in {StopIteration,GeneratorExit}:
                item=dict(kind=kind.__name__,function=frame.f_code.co_name,
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
        self.api=API();self.api.OnSystemEvent=self.event
        old=sys.gettrace();old_thread=threading.gettrace()
        sys.settrace(self.trace);threading.settrace(self.trace)
        try:
            self.api.Connect(username,password,modes[environment],False)
            deadline=time.monotonic()+timeout
            while True:
                self.diagnostics=self.snapshot()
                if (self.diagnostics['trade_connected'] and self.diagnostics['recovery_complete']
                        and self.diagnostics['account_count']>0):
                    return dict(ok=True,connected=True,environment=environment,accounts=[],
                        readonly=True,live_enabled=False,order_enabled=False,diagnostics=self.diagnostics)
                if 'SDK_INITIALIZATION_FAILED' in self.events:raise FuturesLoginError('FUTURES_SDK_INITIALIZATION_FAILED')
                if time.monotonic()>=deadline:raise FuturesLoginError('FUTURES_LOGIN_NOT_READY')
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
