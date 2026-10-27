"""Futures read-only login. No order or inventory actions are exposed."""
import time

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
        def __init__(self,*args,**kwargs):self._run=False
        def Add(self,*args,**kwargs):pass
        def Wrtie_log(self,*args,**kwargs):pass
    SolLog.SolaceLog=SilentLog
    from MasterFutTradePy.MasterFutTradePY import MasterFutTradePY,TServerKind
    class LoginOnlyAPI(MasterFutTradePY):
        def SendNewOrder(self,*args,**kwargs):raise FuturesLoginError('FUTURES_ORDERS_DISABLED')
        def SendDelOrder(self,*args,**kwargs):raise FuturesLoginError('FUTURES_ORDERS_DISABLED')
        def SendChgOrder(self,*args,**kwargs):raise FuturesLoginError('FUTURES_ORDERS_DISABLED')
    return LoginOnlyAPI,{'validation':TServerKind.Test,'formal':TServerKind.Release}

class ValidationSession:
    def __init__(self, loader=load_sdk):
        self.loader=loader;self.api=None;self.diagnostics={};self.environment='validation';self.events=[]
    def snapshot(self):
        api=self.api
        return dict(trade_connected=bool(api and api.IsConn_Trade),
            recovery_complete=bool(api and api.IsRecoverDone),
            account_count=len(api.fAccList) if api else 0,
            environment=self.environment,system_events=list(self.events),order_enabled=False,live_enabled=False)
    def event(self,message):
        text=str(message).lower()
        for markers,category in ((('80040154','-2147221164','class not registered'),'COMPONENT_NOT_REGISTERED'),
                (('未驗證','驗證失敗','sso檢核失敗'),'AUTHORIZATION_NOT_READY'),
                (('無有效憑證','憑證失敗'),'CERTIFICATE_NOT_READY'),
                (('no response','timed out','timeout'),'CONNECTION_TIMEOUT')):
            if any(marker in text for marker in markers):
                self.events=(self.events+[category])[-12:];break
    def login(self,username,password,environment='validation',timeout=35):
        if environment not in {'validation','formal'}:raise FuturesLoginError('FUTURES_ENVIRONMENT_INVALID')
        if self.api is not None:raise FuturesLoginError('FUTURES_RESTART_REQUIRED')
        self.environment=environment
        API,modes=self.loader();self.api=API()
        self.api.OnSystemEvent=self.event
        self.api.Connect(username,password,modes[environment],False)
        deadline=time.monotonic()+timeout
        while True:
            self.diagnostics=self.snapshot()
            if (self.diagnostics['trade_connected'] and self.diagnostics['recovery_complete']
                    and self.diagnostics['account_count']>0):
                return dict(ok=True,connected=True,environment=environment,accounts=[],
                    readonly=True,live_enabled=False,order_enabled=False,diagnostics=self.diagnostics)
            if time.monotonic()>=deadline:raise FuturesLoginError('FUTURES_LOGIN_NOT_READY')
            time.sleep(.05)
    def close(self):
        if self.api is not None:
            try:self.api.Disconnect()
            except Exception:pass
            for name in ('fLoginID','fLoginPwd','fLoginMd5Pwd'):
                try:setattr(self.api,name,'')
                except Exception:pass
            self.api=None
