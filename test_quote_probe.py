"""Subprocess contract test with fake SDK; no account or network access."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

FAKE_WORKER = "import importlib.util, io, json, sys, types\nfrom pathlib import Path\nfrom enum import Enum\nsource=Path(sys.argv[1]); root=Path(sys.argv[2]);run=root/'private'/'fake-test';run.mkdir(parents=True,exist_ok=True)\nspec=importlib.util.spec_from_file_location('probe',source);probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe);probe.ROOT=root\nclass RC(Enum):OK=0\nclass MDM:pass\nclass API:\n def __init__(self,mdm,path):self.mdm=mdm;self.calls=0\n def __getattr__(self,name):\n  if name.startswith('Set_On'):return lambda cb:None\n  raise AttributeError(name)\n def Login(self,user,pwd,market,sim):\n  assert user=='FAKE_USER' and pwd=='FAKE_SECRET_ONLY_FOR_TEST' and market=='TWS' and sim is False\n  self.calls+=1;assert self.calls==1\n  sys.modules['PY_Trade_package.SolLog'].SolaceLog('unused').Add(arg2='FAKE_SECRET_ONLY_FOR_TEST')\n  return RC.OK\n def IsConnect(self):return True\n def Subscribe(self,market,symbol):\n  self.mdm.OnMatch(types.SimpleNamespace(Symbol=symbol,MatchPrice='100',MatchTime='09:01:02.003',Password='FAKE_SECRET_ONLY_FOR_TEST'))\n  return RC.OK\n def Unsubscribe(self,*args):return RC.OK\n def DisConnect(self):return None\nfor name,attrs in [('MarketDataMart',{'MarketDataMart':MDM}),('Sol_D',{'Sol_D':API}),('SolPYAPI_Model',{'RCode':RC})]:\n mod=types.ModuleType('PY_Trade_package.'+name)\n for k,v in attrs.items():setattr(mod,k,v)\n sys.modules[mod.__name__]=mod\nsys.stdin=io.StringIO(json.dumps(dict(username='FAKE_USER',password='FAKE_SECRET_ONLY_FOR_TEST',symbols=['2330'],run=str(run)))+'\\n')\nprobe.time.sleep=lambda _:None\nprobe.worker()\n"

class QuoteProbeWorkerTests(unittest.TestCase):
    def test_credentials_never_reach_capture_and_no_dates_are_invented(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            runner=root/"fake_worker.py"
            runner.write_text(FAKE_WORKER,encoding="utf-8")
            result=subprocess.run([sys.executable,"-I",str(runner),str(Path(__file__).with_name("taishin_quote_probe.py")),str(root)],capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            captured=(root/"private/fake-test/events.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("FAKE_SECRET_ONLY_FOR_TEST",captured+result.stdout+result.stderr)
            self.assertNotIn("FAKE_USER",captured)
            events=[json.loads(line) for line in captured.splitlines()]
            self.assertTrue(any(event["kind"]=="match" for event in events))
            self.assertEqual(events[-1]["orders_sent"],0)
            self.assertFalse(events[-1]["full_day_complete"])
            self.assertTrue(all(event["trading_date"] is None and not event["replay_ready"] for event in events))
            self.assertEqual(list(root.rglob("SolPY.log")),[])

if __name__=="__main__":unittest.main()
