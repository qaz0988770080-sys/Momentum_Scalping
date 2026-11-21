"""Isolated read-only Taishin quote diagnostic. No order transport or paper fills."""
from __future__ import annotations
import argparse
import contextlib
from datetime import datetime, timezone
from enum import Enum
import importlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import types
import uuid

ROOT = Path(__file__).resolve().parent
FIELDS = ('Exchange','Symbol','MatchTime','OrderBookTime','TxSeq','ObSeq','CacheStatus',
          'IsTrail','IsTxTrail','MatchPrice','MatchQty','TotalMatchQty','TradingUnit',
          'BuyPrice','BuyQty','SellPrice','SellQty','RefPrice','OpenPrice','DayHigh','DayLow',
          'TodayRefPrice','PreClosePrice','IndustryCategory','StockCategory')


def install_log_guard():
    # The supplied SDK formats passwords in debug messages. Drop the entire SDK log
    # sink before importing it; neither message strings nor credentials reach disk.
    name = 'PY_Trade_package.SolLog'
    if name in sys.modules:
        raise RuntimeError('SDK_LOG_ALREADY_IMPORTED')
    module = types.ModuleType(name)
    class SolLogType(Enum):
        Info=0; Warning=1; Error=2; Debug=3
    class SolaceLog:
        def __init__(self, *args, **kwargs): pass
        def Add(self, **kwargs): pass
        def Wrtie_log(self, *args, **kwargs): pass
        @staticmethod
        def create_logger(*args, **kwargs):
            return logging.getLogger('quote_probe_null')
    module.SolLogType = SolLogType
    module.SolaceLog = SolaceLog
    module.__all__ = ['SolLogType', 'SolaceLog']
    sys.modules[name] = module
    logging.disable(logging.CRITICAL)
    return module


def scalar(value):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Enum): return value.name
    if isinstance(value, str): return value[:120]
    if isinstance(value, (list, tuple)): return [scalar(x) for x in value[:5]]
    return None


def quote_fields(data):
    return {key: scalar(getattr(data, key)) for key in FIELDS if hasattr(data, key)}


def rc_code(value):
    if isinstance(value, Enum): return value.name
    if isinstance(value, int): return value
    return 'UNSPECIFIED'


def worker():
    # Credentials arrive through an anonymous pipe, never the command line or file.
    request = json.loads(sys.stdin.readline())
    run = Path(request['run']).resolve()
    run.relative_to((ROOT / 'private').resolve())
    symbols = request['symbols']
    if not 1 <= len(symbols) <= 5 or any(not re.fullmatch(r'\d{4,6}', s) for s in symbols):
        raise ValueError('INVALID_PROBE_SYMBOLS')
    lock = threading.Lock()
    def emit(kind, **payload):
        event = dict(kind=kind, received_at_utc=datetime.now(timezone.utc).isoformat(),
                     trading_date=None, replay_ready=False, **payload)
        with lock:
            with (run/'events.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(event, ensure_ascii=False)+'\n')
    api = None
    with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            install_log_guard()
            from PY_Trade_package.MarketDataMart import MarketDataMart
            from PY_Trade_package.Sol_D import Sol_D
            from PY_Trade_package.SolPYAPI_Model import RCode
            mdm = MarketDataMart()
            mdm.OnSystemEvent = lambda d: emit('system', code=rc_code(getattr(d,'rcode',None)))
            mdm.OnUpdateBasic = lambda d: emit('basic', fields=quote_fields(d))
            mdm.OnMatch = lambda d: emit('match', fields=quote_fields(d))
            mdm.OnOrderBook = lambda d: emit('orderbook', fields=quote_fields(d))
            mdm.OnUpdateLastSnapshot = lambda d: emit('snapshot', fields=quote_fields(d.TickData))
            api = Sol_D(mdm, str(ROOT/'taishin_quote_probe.py'))
            api.Set_OnLogEvent(lambda *args: None)
            api.Set_OnInterruptEvent(lambda *args: emit('interrupted'))
            api.Set_OnAnnouncementEvent_DAPI(lambda *args: emit('announcement_received'))
            api.Set_OnSystemEvent_DAPI(lambda d: emit('verification_system', code=rc_code(getattr(d,'rcode',None))))
            api.Set_OnLoginResultEvent_DAPI(lambda ok, msg: emit('login_result', success=bool(ok)))
            api.Set_OnVerifiedEvent_DAPI(lambda d: emit('verification_result', success=bool(getattr(d,'IsSucc',False))))
            api.Set_OnUpdateBasic_DAPI(lambda d: emit('verification_basic', fields=quote_fields(d)))
            api.Set_OnMatch_DAPI(lambda d: emit('verification_match', fields=quote_fields(d)))
            result = api.Login(request.pop('username'), request.pop('password'), 'TWS', False)
            emit('login_return', code=rc_code(result), connected=bool(api.IsConnect()))
            if result == RCode.OK and api.IsConnect():
                for symbol in symbols:
                    emit('subscribe', symbol=symbol, code=rc_code(api.Subscribe('TWS',symbol)))
                time.sleep(45)
                for symbol in symbols:
                    api.Unsubscribe('TWS',symbol)
        except Exception as error:
            # Exception messages can contain server/user credentials. Keep type only.
            emit('failed', error_type=type(error).__name__)
        finally:
            request.clear()
            if api is not None:
                with contextlib.suppress(Exception): api.DisConnect()
            emit('finished', orders_sent=0, full_day_complete=False)
    # Vendor threads may remain alive after DisConnect. This isolated process owns
    # no broker order state; terminate it after flushing our bounded capture.
    os._exit(0)


def self_test():
    guard = install_log_guard()
    guard.SolaceLog('unused').Add(arg1=guard.SolLogType.Debug, arg2='Password=SYNTHETIC_TEST_SECRET')
    from PY_Trade_package.Sol_D import Sol_D
    assert callable(Sol_D.Subscribe) and callable(Sol_D.Login)
    assert not any(hasattr(Sol_D, name) for name in ('SendNewOrder','PlaceOrder','CancelOrder'))
    assert not any(name.startswith('MasterFutTradePy') for name in sys.modules)
    sample = types.SimpleNamespace(Symbol='2330', MatchTime='09:00:00.001', MatchPrice='100', Password='SYNTHETIC_TEST_SECRET')
    fields = quote_fields(sample)
    assert 'Password' not in fields
    assert 'trading_date' not in fields
    assert sys.modules['PY_Trade_package.SolLog'] is guard
    assert not (ROOT/'SolPY.log').exists()
    result = dict(ok=True, sdk_imported=True, sdk_log_sink_disabled=True,
                  unknown_fields_excluded=True, dates_not_fabricated=True,
                  futures_sdk_imported=False, network_login_attempted=False,
                  orders_sent=0, full_day_complete=False)
    (ROOT/'self-test-result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result))


def gui(hidden_test=False):
    import tkinter as tk
    from tkinter import ttk, messagebox
    root=tk.Tk(); root.title('Momentum｜台新股票行情檢查・唯讀'); root.geometry('780x550')
    if hidden_test: root.withdraw()
    root.configure(bg='#101827')
    style=ttk.Style(root); style.theme_use('clam')
    style.configure('TFrame',background='#101827'); style.configure('TLabel',background='#101827',foreground='#e2e8f0',font=('Microsoft JhengHei UI',11))
    frame=ttk.Frame(root,padding=24); frame.pack(fill='both',expand=True)
    ttk.Label(frame,text='台新股票行情 · 連線檢查',font=('Microsoft JhengHei UI',19,'bold')).pack(anchor='w')
    ttk.Label(frame,text='只接收行情，不含下單功能。此工具與原作者模式、期貨 APP 分開。',wraplength=710).pack(anchor='w',pady=(8,18))
    user=tk.StringVar(); password=tk.StringVar(); symbols=tk.StringVar(value='2330')
    grid=ttk.Frame(frame);grid.pack(fill='x')
    entries=[]
    for row,(label,var,mask) in enumerate([('登入帳號',user,''),('登入密碼',password,'●'),('股票代號（最多 5 檔）',symbols,'')]):
        ttk.Label(grid,text=label).grid(row=row,column=0,sticky='w',pady=5)
        entry=ttk.Entry(grid,textvariable=var,show=mask,width=43); entry.grid(row=row,column=1,sticky='ew',padx=16,pady=5);entries.append(entry)
    grid.columnconfigure(1,weight=1)
    status=tk.StringVar(value='尚未連線。帳密只用於本次行情登入，關閉即結束。')
    ttk.Label(frame,textvariable=status,wraplength=710).pack(anchor='w',pady=12)
    text=tk.Text(frame,height=10,bg='#182336',fg='#cbd5e1',relief='flat',font=('Consolas',10));text.pack(fill='both',expand=True)
    state={'process':None,'started':0,'run':None,'lines':0,'count':0}
    def stop():
        process=state['process']
        if process and process.poll() is None:
            process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):process.wait(timeout=3)
        state['process']=None
        user.set(''); password.set('')
        status.set('已結束連線；未送出任何交易委託。')
        for entry in entries:entry.configure(state='normal')
        start_button.configure(state='normal')
    def poll():
        process=state['process']
        if process:
            path=state['run']/'events.jsonl'
            lines=path.read_text(encoding='utf-8').splitlines() if path.exists() else []
            for line in lines[state['lines']:]:
                try:event=json.loads(line)
                except json.JSONDecodeError: break
                state['lines']+=1
                kind=event['kind']; f=event.get('fields',{})
                if f:
                    state['count']+=1
                    shown=f"{kind} {f.get('Symbol','')} 成交 {f.get('MatchPrice','—')} 買 {f.get('BuyPrice',[])} 賣 {f.get('SellPrice',[])}"
                else:shown=f"{kind} {event.get('code','')} {event.get('success','')}"
                text.insert('end',shown+'\n');text.see('end')
            if process.poll() is not None:
                stop();status.set(f"本次結束，收到 {state['count']} 筆行情事件；這不是完整一日模擬。")
            elif time.monotonic()-state['started']>90:
                stop();status.set('連線檢查逾時，已結束程序。沒有自動重試登入。')
        root.after(400,poll)
    def start():
        items=list(dict.fromkeys(s.strip() for s in re.split(r'[,，\s]+',symbols.get()) if s.strip()))
        if not user.get().strip() or not password.get():
            messagebox.showinfo('缺少登入資料','請只在這個本機視窗輸入帳密。');return
        if not 1<=len(items)<=5 or any(not re.fullmatch(r'\d{4,6}',s) for s in items):
            messagebox.showinfo('股票代號','請輸入 1～5 個股票代號，以逗號分隔。');return
        config=ROOT/'Configs'/'SYS_SOLACE.ini'
        if not config.exists():
            messagebox.showerror('元件未完整','缺少官方行情主機設定；請讓我檢查，不要修改期貨設定。');return
        run=ROOT/'private'/uuid.uuid4().hex;run.mkdir(parents=True)
        payload=dict(username=user.get().strip(),password=password.get(),symbols=items,run=str(run))
        try:
            process=subprocess.Popen([sys.executable,'-I',str(Path(__file__).resolve()),'--worker'],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,text=True,encoding='utf-8',cwd=ROOT,creationflags=subprocess.CREATE_NO_WINDOW)
            process.stdin.write(json.dumps(payload)+'\n');process.stdin.flush();process.stdin.close()
        except Exception as error:
            if 'process' in locals() and process.poll() is None:
                process.terminate()
            payload.clear();password.set('');messagebox.showerror('啟動失敗',type(error).__name__);return
        payload.clear();user.set('');password.set('')
        state.update(process=process,started=time.monotonic(),run=run,lines=0,count=0)
        text.delete('1.0','end');status.set('正在登入股票行情；最多等候 90 秒，僅嘗試一次。')
        for entry in entries:entry.configure(state='disabled')
        start_button.configure(state='disabled')
    buttons=ttk.Frame(frame);buttons.pack(fill='x',pady=(16,0))
    start_button=ttk.Button(buttons,text='登入並檢查行情',command=start);start_button.pack(side='left')
    ttk.Button(buttons,text='結束連線／清除帳密',command=stop).pack(side='left',padx=12)
    root.protocol('WM_DELETE_WINDOW',lambda:(stop(),root.destroy()))
    if hidden_test:
        root.update_idletasks()
        assert password.get()=='' and state['process'] is None and len(entries)==3
        root.destroy();print(json.dumps(dict(ok=True,gui_loaded=True,network_login_attempted=False,orders_sent=0)));return
    poll();root.mainloop()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--worker',action='store_true');parser.add_argument('--self-test',action='store_true');parser.add_argument('--ui-self-test',action='store_true');args=parser.parse_args()
    if args.worker:worker()
    elif args.self_test:self_test()
    else:gui(args.ui_self_test)
