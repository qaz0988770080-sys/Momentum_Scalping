"""Verified original-author UI with an external offline-only launch boundary."""
from pathlib import Path
import sys,os,json,hashlib,shutil,importlib.util,zipfile
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parent
BASELINE=ROOT/'baseline'
DATA=ROOT/'user-data'
RUN=DATA/'original-run'
REVISION='1dea51bf2592d1e0ffa88b3faf97b267c2756d6d'
EXPECTED={"Momentum Scalping Chinese Instruction Manual":"7dbc37ee54ab48f9d5b7bfb6c89a2ccb3012af3e","README.md":"3ee9d2a4633dabe64889308cda8e5b3c8f9e4486","api_gateway_v2_7.py":"5ecd6b268b9d4f38940b6b2e919b07fbb59897df","gui_v5_5.py":"81e1b9476dfe739b2e19bd4dadfc3acd6fca9479","intraday_v2_1.py":"157f90308b919a6c3e4bc96ad5c9f4417674f992","market_v1_6.py":"0bdecea01ee28c3e44b19786501ebf7f7df6bd98","params_v1_9.py":"cf754676ab3ad68ae49fcddffd42b54b0f252ff1","params_v2_0.md":"b1bfe68165947b4a53b90db20222f7bd74875779","runtime_v4_1.py":"a5b877090afdc1758fdf4c86ad4b5546f1b3890f","scanner_v2_8.py":"54890ec7d89e8bdc168dc76b7951dd5f129b0f3e","trading_v3_9.py":"7923b3986572098b961b5536ceadc9f8d7ce3a29"}

def blob_sha(data):return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
def verify(folder):
 folder=Path(folder).resolve();bad=[]
 for name,expected in EXPECTED.items():
  path=folder/name
  if path.resolve().parent!=folder or not path.is_file() or blob_sha(path.read_bytes())!=expected:bad.append(name)
 extra=[p.relative_to(folder).as_posix() for p in folder.rglob('*') if p.is_file() and p.suffix.lower() in {'.py','.pyc','.pyd','.dll','.zip'} and p.relative_to(folder).as_posix() not in EXPECTED]
 if bad or extra:raise RuntimeError('原作者基準校驗失敗，拒絕啟動：'+', '.join(bad+extra))
 return len(EXPECTED)
def prepare():
 verify(BASELINE);RUN.mkdir(parents=True,exist_ok=True)
 for name in EXPECTED:shutil.copyfile(BASELINE/name,RUN/name)
 verify(RUN)
 return RUN

def load_original():
 run=prepare();sys.dont_write_bytecode=True
 sys.path.insert(0,str(run));os.chdir(run)
 spec=importlib.util.spec_from_file_location('original_gui_entry',run/'gui_v5_5.py')
 module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
 loader=module.CoreModuleLoader(run);modules=loader.load_all()
 # Only the external launch boundary enforces OFFLINE. Original source stays byte-identical.
 original_gateway=modules['api_gateway'].APIGateway
 class OfflineOnlyGateway(original_gateway):
  def __init__(self,*args,**kwargs):
   if str(kwargs.get('mode','LIVE')).upper()!='OFFLINE':raise ValueError('原作者基準模式僅提供 OFFLINE；台新連線請開啟台新版。')
   super().__init__(*args,**kwargs)
 modules['api_gateway'].APIGateway=OfflineOnlyGateway
 registry=module.ParameterRegistry(modules,run)
 settings=module.GUISettings(api_mode='OFFLINE',control_mode='MANUAL',auto_execute=False,replay_dir=str(DATA/'api_replay'))
 settings.validate();registry.validate_all()
 return module,loader,modules,registry,settings

def self_test():
 module,loader,modules,registry,settings=load_original()
 from dataclasses import asdict
 original_defaults=module.GUISettings()
 try:modules['api_gateway'].APIGateway(mode='LIVE')
 except ValueError:blocked=True
 else:raise AssertionError('LIVE must be blocked')
 test=ROOT/'self-test'/'tampered';test.mkdir(parents=True,exist_ok=True)
 for name in EXPECTED:shutil.copyfile(BASELINE/name,test/name)
 with (test/'intraday_v2_1.py').open('ab') as f:f.write(b'\n# simulated accidental edit\n')
 try:verify(test)
 except RuntimeError:tamper_blocked=True
 else:raise AssertionError('Changed source must be rejected')
 extra_test=ROOT/'self-test'/'extra-module';extra_test.mkdir(parents=True,exist_ok=True)
 for name in EXPECTED:shutil.copyfile(BASELINE/name,extra_test/name)
 (extra_test/'scanner_v999.py').write_text('# simulated later module',encoding='utf8')
 try:verify(extra_test)
 except RuntimeError:extra_blocked=True
 else:raise AssertionError('Unexpected module must be rejected')
 result={'ok':True,'unexpected_module_rejected':extra_blocked,'baseline_revision':REVISION,'verified_original_files':verify(BASELINE),'verified_run_files':verify(RUN),'live_transport_blocked':blocked,'tampered_source_rejected':tamper_blocked,'api_mode':settings.api_mode,'control_mode':settings.control_mode,'auto_execute':settings.auto_execute,'strategy_defaults_match_original':all(asdict(bundle.current)==asdict(bundle.default) for bundle in registry.bundles.values()),'settings_root':str(DATA/'settings'),'taishin_settings_shared':False,'orders_sent':0}
 (ROOT/'self-test-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
 print(json.dumps(result,ensure_ascii=True));return 0

def main():
 (ROOT/'bootstrap.json').write_text(json.dumps({'pid':os.getpid(),'at':datetime.now(timezone.utc).isoformat(),'arguments':sys.argv[1:]}),encoding='utf8')
 if '--self-test' in sys.argv:return self_test()
 if '--verify' in sys.argv:print(json.dumps({'verified_files':verify(BASELINE),'revision':REVISION}));return 0
 module,loader,modules,registry,settings=load_original()
 import tkinter as tk
 root=tk.Tk()
 if '--ui-self-test' in sys.argv:root.withdraw()
 root.report_callback_exception=module._install_error_reporting()
 gui=module.TradingGUI(root,loader,modules,registry,settings,module.ConfigStore(DATA/'settings'))
 root.title('原作者基準模式｜原版介面・離線重播')
 original_apply=gui.parameter_component.apply_settings_callback
 def offline_settings_only(updated,**kwargs):
  if str(updated.api_mode).upper()!='OFFLINE':raise ValueError('此模式只允許 OFFLINE；不與台新或證券實單混用。')
  return original_apply(updated,**kwargs)
 gui.parameter_component.apply_settings_callback=offline_settings_only
 gui._set_message('原作者基準 '+REVISION[:8]+'｜原版程式已逐檔校驗｜獨立離線設定，不共用台新版資料。')
 (ROOT/'runtime.json').write_text(json.dumps({'pid':os.getpid(),'revision':REVISION,'verified_files':len(EXPECTED),'offline_only':True,'opened_at':datetime.now(timezone.utc).isoformat()},ensure_ascii=False),encoding='utf8')
 if '--ui-self-test' in sys.argv:
  root.update_idletasks()
  result={'ok':True,'original_gui_loaded':True,'parameter_rows':{k:len(t.get_children()) for k,t in gui.parameter_component.parameter_trees.items()},'offline_only':True,'orders_sent':0}
  (ROOT/'ui-self-test-result.json').write_text(json.dumps(result),encoding='utf8')
  gui._on_close();print(json.dumps(result));return 0
 root.mainloop();return 0

if __name__=='__main__':
 try:raise SystemExit(main())
 except Exception as error:
  (ROOT/'startup-error.txt').write_text(type(error).__name__+': '+str(error),encoding='utf8')
  if '--self-test' in sys.argv or '--ui-self-test' in sys.argv or '--verify' in sys.argv:raise
  import tkinter as tk
  from tkinter import messagebox
  root=tk.Tk();root.withdraw();messagebox.showerror('原作者模式無法啟動',str(error));root.destroy();raise
