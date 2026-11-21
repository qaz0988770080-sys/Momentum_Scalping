"""One validation login per isolated 32-bit process, no trading commands."""
import argparse,json,os,sys,queue,threading
from pathlib import Path
from datetime import datetime,timezone
sys.path.insert(0,str(Path(__file__).resolve().parent))
from taishin_futures_validation import ValidationSession,FuturesLoginError,BUILD,DRIVER_REVISION
from taishin_futures_com import pump_com_events
from taishin_futures_compat import keep_validation_session
from taishin_futures_verification import VerificationService,VerificationError
PREFIX='@@MOMENTUM_READONLY@@'
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--sdk-root');parser.add_argument('--parent-pid',type=int);args=parser.parse_args()
    protocol=os.dup(sys.stdout.fileno());null=os.open(os.devnull,os.O_WRONLY)
    os.dup2(null,1);os.dup2(null,2);os.close(null)
    root=Path(__file__).resolve().parent;private=root/'private'/'futures';private.mkdir(parents=True,exist_ok=True);os.chdir(private)
    if args.parent_pid and os.name=='nt':
        def watch():
            import ctypes
            from ctypes import wintypes
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
            kernel.OpenProcess.restype=wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
            handle=kernel.OpenProcess(0x100000,False,args.parent_pid)
            if not handle:os._exit(0)
            while kernel.WaitForSingleObject(handle,1000)==258:pass
            os._exit(0)
        import threading
        threading.Thread(target=watch,daemon=True).start()
    session=ValidationSession()
    verification=VerificationService(session,private)
    def emit(kind,**values):
        result=dict(kind=kind,worker_build=BUILD,readonly=True,live_enabled=False,order_enabled=False,**values)
        os.write(protocol,(PREFIX+json.dumps(result)+'\n').encode())
    emit('ready',driver_revision=DRIVER_REVISION)
    (private/'worker-runtime.json').write_text(json.dumps(dict(worker_pid=os.getpid(),server_pid=args.parent_pid,worker_build=BUILD,driver_revision=DRIVER_REVISION,connected=False,order_enabled=False)),encoding='utf-8')
    incoming=queue.Queue()
    def read_commands():
        for line in sys.stdin:
            incoming.put(line);line=''
        incoming.put(None)
    threading.Thread(target=read_commands,daemon=True).start()
    while True:
        pump_com_events()
        try:raw=incoming.get(timeout=.05)
        except queue.Empty:
            session.collect_native_diagnostics()
            continue
        if raw is None:break
        command={};action=''
        try:
            if len(raw)>8192:raise FuturesLoginError('COMMAND_TOO_LARGE')
            command=json.loads(raw);raw=''
            if not isinstance(command,dict):raise FuturesLoginError('INVALID_COMMAND')
            action=command.get('cmd')
            if action=='logout':emit('logout',ok=True,connected=False);break
            if action in {'verification_status','verification_submit'}:
                if action=='verification_status':result=verification.status()
                else:result=verification.submit(command.get('plan_digest'))
                result.pop('live_enabled',None)
                if session.api is not None and session.environment=='validation':
                    evidence=dict(ok=bool(result.get('connected')),waiting=bool(result.get('keep_session')),worker_pid=os.getpid(),server_pid=args.parent_pid,worker_build=BUILD,checked_at=datetime.now(timezone.utc).isoformat(),**session.snapshot())
                    (private/'readiness.json').write_text(json.dumps(evidence),encoding='utf-8')
                emit(action,**result)
                continue
            if action!='login':raise FuturesLoginError('FUTURES_LOGIN_ONLY')
            username=command.pop('username','');password=command.pop('password','')
            try:
                if not isinstance(username,str) or not username.strip() or not isinstance(password,str) or not password:
                    raise FuturesLoginError('MISSING_CREDENTIALS')
                result=session.login(username.strip(),password,command.get('environment'))
            finally:username=password=''
            result.pop('readonly',None);result.pop('live_enabled',None);result.pop('order_enabled',None)
            emit('login',**result)
            evidence=dict(ok=True,worker_pid=os.getpid(),server_pid=args.parent_pid,worker_build=BUILD,checked_at=datetime.now(timezone.utc).isoformat(),**session.snapshot())
        except Exception as exc:
            code=str(exc) if isinstance(exc,(FuturesLoginError,VerificationError)) else 'FUTURES_SDK_'+type(exc).__name__
            evidence=dict(ok=False,error=code,worker_pid=os.getpid(),server_pid=args.parent_pid,worker_build=BUILD,checked_at=datetime.now(timezone.utc).isoformat(),**session.snapshot())
            keep_session=bool(action=='login' and keep_validation_session(code,evidence))
            if keep_session:
                code='FUTURES_LOGIN_PENDING';evidence['error']=code;evidence['keep_session']=True
            emit('error',ok=False,connected=False if action=='login' else bool(session.api and session.api.IsConn_Trade),environment=session.environment,keep_session=keep_session,error=code,diagnostics=evidence)
            if action=='login' and not keep_session:break
        finally:
            if isinstance(command,dict):command.clear()
            raw=''
        (private/'readiness.json').write_text(json.dumps(evidence),encoding='utf-8')
    if 'evidence' in locals():(private/'readiness.json').write_text(json.dumps(evidence),encoding='utf-8')
    # Native SDK can leave non-daemon threads; parent process isolation is intentional.
    # Exit closes connections without calling an unbounded Disconnect before the response.
    os.close(protocol);os._exit(0)
if __name__=='__main__':main()
