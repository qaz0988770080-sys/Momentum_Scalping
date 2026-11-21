"""Credential-free futures SDK load check; never connects or submits orders."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import sys


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--check-native', action='store_true')
    parser.add_argument('--output')
    args=parser.parse_args()
    result=dict(stage='environment', python_bits=struct.calcsize('P')*8,
                connected=False, live_enabled=False, order_enabled=False,
                intended_environment='validation', ok=False)
    if result['python_bits'] != 32:
        result['error']='FUTURES_PYTHON_32_REQUIRED'
    elif not args.check_native:
        result.update(ok=True, stage='python_32_ready', native_checked=False)
    else:
        protocol=os.dup(sys.stdout.fileno())
        null=os.open(os.devnull,os.O_WRONLY)
        os.dup2(null,1);os.dup2(null,2);os.close(null)
        try:
            import MasterFutTradePy
            result['package_loaded']=True
            from MasterFutTradePy.MasterFutTradePY import MasterFutTradePY,TServerKind
            source=Path(sys.modules[MasterFutTradePY.__module__].__file__)
            result.update(ok=True,stage='native_driver_imported',native_checked=True,
                validation_enum=TServerKind.Test.value,
                sdk_source_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
        except Exception as exc:
            chain=[]
            for _ in range(5):
                chain.append(dict(kind=type(exc).__name__,
                    hresult=hex(getattr(exc,'HResult',0)&0xffffffff)))
                inner=getattr(exc,'InnerException',None)
                if inner is None:break
                exc=inner
            result.update(error='FUTURES_SDK_LOAD_FAILED', exception_chain=chain)
        payload=json.dumps(result,ensure_ascii=False)
        if args.output:
            destination=Path(args.output)
            destination.parent.mkdir(parents=True,exist_ok=True)
            destination.write_text(payload,encoding='utf-8')
        os.write(protocol,(payload+'\n').encode('utf-8'))
        os.close(protocol)
        os._exit(0 if result['ok'] else 1)
    payload=json.dumps(result,ensure_ascii=False)
    if args.output:
        destination=Path(args.output);destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_text(payload,encoding='utf-8')
    print(payload)
    return 0 if result['ok'] else 1

if __name__=='__main__':
    raise SystemExit(main())
