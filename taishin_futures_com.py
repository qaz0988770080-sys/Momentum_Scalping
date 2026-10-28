"""Activate official Speedy COM classes privately, without system registration."""
import ctypes,os,threading
from pathlib import Path
from ctypes import wintypes

SPEEDY_CLASSES=('16f0ac41-388c-4d8c-8d9f-43926d3f3212','019e6568-9a7e-495d-9b3e-4e280a25bd68','a713a529-b390-4f97-870b-de03e488290b','334c85bf-3d5c-42bc-a401-29d5cf360551','acd2d304-d269-4b88-93de-46b5cf5c4b0f','cfbb20a0-5d2f-478d-9249-b533f5e102bd','28799f2e-8a9e-4eda-b468-011c976029e1','effb7b42-b557-4359-8a80-399f13afafe4','4eec8062-44f0-4ea5-b8b2-985f8fa6a3b6','ee0edaa2-b6f3-4dfe-b47b-8c778777dfc6','5b1f18e2-274d-44b5-8f74-c365cec81e14')
_local=threading.local()

def speedy_manifest():
    classes=''.join('<comClass clsid="{'+value+'}" threadingModel="Apartment" />' for value in SPEEDY_CLASSES)
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0"><assemblyIdentity type="win32" name="Momentum.Taishin.Speedy" version="1.0.0.0" processorArchitecture="x86"/><file name="SpeedyAPI.dll">'+classes+'</file></assembly>'

def activate_speedy(package):
    if os.name!='nt':return False
    ole=ctypes.OleDLL('ole32')
    ole.CoInitializeEx.argtypes=[ctypes.c_void_p,wintypes.DWORD]
    ole.CoInitializeEx.restype=ctypes.c_long
    ole.CoInitializeEx(None,2) # Official ActiveX components use an STA.
    package=Path(package).resolve()
    if getattr(_local,'package',None)==str(package):return True
    if not (package/'SpeedyAPI.dll').is_file():raise RuntimeError('FUTURES_SPEEDY_COMPONENT_MISSING')
    manifest=package/'momentum-speedy.manifest'
    manifest.write_text(speedy_manifest(),encoding='utf-8')
    class ACTCTX(ctypes.Structure):
        _fields_=[('cbSize',wintypes.ULONG),('dwFlags',wintypes.DWORD),('lpSource',wintypes.LPCWSTR),('wProcessorArchitecture',wintypes.WORD),('wLangId',wintypes.WORD),('lpAssemblyDirectory',wintypes.LPCWSTR),('lpResourceName',wintypes.LPCWSTR),('lpApplicationName',wintypes.LPCWSTR),('hModule',wintypes.HMODULE)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateActCtxW.argtypes=[ctypes.POINTER(ACTCTX)];kernel.CreateActCtxW.restype=wintypes.HANDLE
    kernel.ActivateActCtx.argtypes=[wintypes.HANDLE,ctypes.POINTER(ctypes.c_size_t)];kernel.ActivateActCtx.restype=wintypes.BOOL
    kernel.ReleaseActCtx.argtypes=[wintypes.HANDLE]
    config=ACTCTX();config.cbSize=ctypes.sizeof(config);config.dwFlags=4
    config.lpSource=str(manifest);config.lpAssemblyDirectory=str(package)
    handle=kernel.CreateActCtxW(ctypes.byref(config))
    if handle==ctypes.c_void_p(-1).value:raise OSError(ctypes.get_last_error(),'FUTURES_SPEEDY_ACTIVATION_CONTEXT_FAILED')
    cookie=ctypes.c_size_t()
    if not kernel.ActivateActCtx(handle,ctypes.byref(cookie)):
        kernel.ReleaseActCtx(handle);raise OSError(ctypes.get_last_error(),'FUTURES_SPEEDY_ACTIVATION_FAILED')
    # The dedicated worker owns this context for its entire lifetime. Child
    # threads inherit the context; OS process exit releases the resources.
    _local.package=str(package);_local.handle=handle;_local.cookie=cookie
    return True


def pump_com_events():
    if os.name!='nt':return
    class MSG(ctypes.Structure):
        _fields_=[('hwnd',wintypes.HWND),('message',wintypes.UINT),('wParam',wintypes.WPARAM),('lParam',wintypes.LPARAM),('time',wintypes.DWORD),('pt',wintypes.POINT),('lPrivate',wintypes.DWORD)]
    user=ctypes.WinDLL('user32',use_last_error=True)
    user.PeekMessageW.argtypes=[ctypes.POINTER(MSG),wintypes.HWND,wintypes.UINT,wintypes.UINT,wintypes.UINT];user.PeekMessageW.restype=wintypes.BOOL
    user.TranslateMessage.argtypes=[ctypes.POINTER(MSG)];user.TranslateMessage.restype=wintypes.BOOL
    user.DispatchMessageW.argtypes=[ctypes.POINTER(MSG)];user.DispatchMessageW.restype=wintypes.LPARAM
    message=MSG()
    for _ in range(50):
        if not user.PeekMessageW(ctypes.byref(message),None,0,0,1):break
        if message.message!=0x12:
            user.TranslateMessage(ctypes.byref(message));user.DispatchMessageW(ctypes.byref(message))
