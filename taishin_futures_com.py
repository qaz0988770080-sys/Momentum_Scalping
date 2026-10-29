"""Activate official Speedy COM classes privately, without system registration."""
import ctypes,os,threading
from pathlib import Path
from ctypes import wintypes

SPEEDY_CLASSES=('16f0ac41-388c-4d8c-8d9f-43926d3f3212','019e6568-9a7e-495d-9b3e-4e280a25bd68','a713a529-b390-4f97-870b-de03e488290b','334c85bf-3d5c-42bc-a401-29d5cf360551','acd2d304-d269-4b88-93de-46b5cf5c4b0f','cfbb20a0-5d2f-478d-9249-b533f5e102bd','28799f2e-8a9e-4eda-b468-011c976029e1','effb7b42-b557-4359-8a80-399f13afafe4','4eec8062-44f0-4ea5-b8b2-985f8fa6a3b6','ee0edaa2-b6f3-4dfe-b47b-8c778777dfc6','5b1f18e2-274d-44b5-8f74-c365cec81e14')
SPEEDY_TYPELIB='4c3acc93-9ec6-44e5-bfc2-883eed3a05da'
SPEEDY_INTERFACES=('eb336d21-43fc-43ee-9e3f-6feec1f334ff','1b263041-34c8-452b-8712-0927079c89f4','a9ec1c6a-4c4d-4f05-96d5-f247b07e5126','98253f62-229d-4e67-a0b7-f4563aee8145','3331a457-f8f8-4298-acfb-2a287fc97938','96dea5c3-93fb-4962-90ae-f0ebc92897d8','a6a6dee4-33cf-4bac-a64e-aff6fe9b2bf3','3bf3afea-d535-41e3-bd06-e54ba0a133f1','4a819f1f-1497-499d-9bda-3d3f39a67ef6','846fd38f-d1ff-42ab-91b7-a9ebcff354b5','19382553-d24d-420c-adcf-8de6261cf806','1ae89f65-e901-4703-81e5-87a562181f4c')
SPEEDY_INTERFACE_NAMES=('IBaseMessage','INewOrderMessage','IReplaceOrderMessage','ICancelOrderMessage','IQuoteMessage','IQuoteCancelMessage','IQuoteRequestMessage','IExecutionReportMessage','IOrderStatusRequestMessage','INewsMessage','IOrderConnection','IOrderConnectionEvents')
CERTIFICATE_COMPONENTS=(
    ('TWCACAPIX.dll','3423d774-207f-4695-a890-f8c2c202a431','Both','1a75d958-1952-4183-ba01-b274730684a6','ITWCAPI','14bc5986-642a-4b90-aa66-9cdb48d3d451'),
    ('MLSecuritiesATL.dll','c0888c8f-a4f1-480a-b970-434104010000','Apartment','cac09109-a85c-4a78-81c2-434104010000','IMLCapi','df4862fe-8fca-49fb-898b-434104010000'),
)
_local=threading.local()

def speedy_manifest():
    classes=''.join('<comClass clsid="{'+value+'}" threadingModel="Apartment" />' for value in SPEEDY_CLASSES)
    interfaces=''.join('<comInterfaceExternalProxyStub name="'+name+'" iid="{'+value+'}" tlbid="{'+SPEEDY_TYPELIB+'}" proxyStubClsid32="{00020424-0000-0000-C000-000000000046}"/>' for name,value in zip(SPEEDY_INTERFACE_NAMES,SPEEDY_INTERFACES))
    typelib='<typelib tlbid="{'+SPEEDY_TYPELIB+'}" version="3.0" helpdir=""/>'
    certificate_files=''
    for filename,clsid,threading_model,tlbid,name,iid in CERTIFICATE_COMPONENTS:
        certificate_files+='<file name="'+filename+'"><comClass clsid="{'+clsid+'}" threadingModel="'+threading_model+'"/><typelib tlbid="{'+tlbid+'}" version="1.0" helpdir=""/></file>'
        interfaces+='<comInterfaceExternalProxyStub name="'+name+'" iid="{'+iid+'}" tlbid="{'+tlbid+'}" proxyStubClsid32="{00020424-0000-0000-C000-000000000046}"/>'
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0"><assemblyIdentity type="win32" name="Momentum.Taishin.Speedy" version="1.0.0.0" processorArchitecture="x86"/><file name="SpeedyAPI.dll">'+classes+typelib+'</file>'+certificate_files+interfaces+'</assembly>'

def activate_speedy(package):
    if os.name!='nt':return False
    ole=ctypes.OleDLL('ole32')
    ole.CoInitializeEx.argtypes=[ctypes.c_void_p,wintypes.DWORD]
    ole.CoInitializeEx.restype=ctypes.c_long
    ole.CoInitializeEx(None,2) # Official ActiveX components use an STA.
    package=Path(package).resolve()
    if getattr(_local,'package',None)==str(package):return True
    if not (package/'SpeedyAPI.dll').is_file():raise RuntimeError('FUTURES_SPEEDY_COMPONENT_MISSING')
    if any(not (package/item[0]).is_file() for item in CERTIFICATE_COMPONENTS):raise RuntimeError('FUTURES_CERTIFICATE_COMPONENT_MISSING')
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
