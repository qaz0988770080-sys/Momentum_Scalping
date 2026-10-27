"""Independent local futures validation login, fixed to test environment."""
import argparse,json,os,sys,threading
from pathlib import Path
from datetime import datetime,timezone
from taishin_readonly_web import WorkerBridge,LoopbackServer,make_handler
from taishin_futures_validation import BUILD
from taishin_web_diagnose import http_check,appcontainer

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8790);parser.add_argument('--self-test',action='store_true');args=parser.parse_args()
    root=Path(__file__).resolve().parent
    interpreter=root/'futures-runtime'/'python.exe'
    if not interpreter.is_file():raise RuntimeError('FUTURES_RUNTIME_MISSING')
    bridge=WorkerBridge(root,interpreter=str(interpreter),worker_script='taishin_futures_worker.py',worker_build=BUILD)
    server=LoopbackServer(('127.0.0.1',args.port),make_handler(root,bridge,'taishin_futures.html'))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url='http://127.0.0.1:'+str(server.server_port)
    private=root/'private';private.mkdir(exist_ok=True)
    def save(phase):
        state=dict(pid=os.getpid(),url=url,phase=phase,checked_at=datetime.now(timezone.utc).isoformat(),
            server_build=BUILD,serving_thread_alive=thread.is_alive(),worker_build=bridge.worker_build,
            available_environments=['validation','formal_readonly'],appcontainer=appcontainer(),live_enabled=False,order_enabled=False)
        temp=private/'futures-web-runtime.tmp';temp.write_text(json.dumps(state),encoding='utf-8');os.replace(temp,private/'futures-web-runtime.json')
    try:
        check=http_check(url+'/')
        if not check.get('ok'):raise RuntimeError('FUTURES_HTTP_CHECK_FAILED')
        bridge.state();save('serving')
        print('FUTURES_SELF_CHECK_OK '+url,flush=True)
        if not args.self_test:
            while thread.is_alive():thread.join(timeout=2);save('serving')
    finally:
        server.shutdown();bridge.close();server.server_close();thread.join(timeout=2);save('stopped')
    return 0
if __name__=='__main__':raise SystemExit(main())
