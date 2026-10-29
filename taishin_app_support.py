"""Process lifetime support for the desktop application's own server."""
def watch_parent(pid):
    import ctypes,os,threading
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    handle=kernel.OpenProcess(0x100000,False,pid)
    if not handle:raise RuntimeError('APP_PARENT_UNAVAILABLE')
    def watch():
        while kernel.WaitForSingleObject(handle,1000)==258:pass
        os._exit(0)
    threading.Thread(target=watch,daemon=True).start()
