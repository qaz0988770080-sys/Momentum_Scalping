"""Loopback-only compatibility for the official SDK's legacy SSO INI parser."""
import base64,json,secrets,threading,socket,os
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit,urlunsplit
from urllib.request import build_opener,HTTPRedirectHandler,Request,ProxyHandler

class SsoFormatError(Exception):pass

def normalize_sso_reply(body,native_encoding=None):
    """Change JSON formatting only; keep every broker-provided INI value."""
    encoding=native_encoding or ('mbcs' if os.name=='nt' else 'utf-8')
    def decode(raw):
        try:return raw.decode('utf-8')
        except UnicodeDecodeError:return raw.decode(encoding)
    result=json.loads(decode(body))
    encoded=result.get('user_data')
    if not isinstance(encoded,str):return body,dict(changed=False,config_present=False)
    raw=base64.b64decode(encoded,validate=True)
    try:payload=json.loads(decode(raw),strict=False)
    except UnicodeDecodeError:raise SsoFormatError('SSO_CONFIG_ENCODING_UNSUPPORTED') from None
    files=payload.get('iniFile',[])
    if not isinstance(files,list):raise SsoFormatError('SSO_CONFIG_SCHEMA_INVALID')
    selected=[item for item in files if isinstance(item,dict) and item.get('fname')=='SYS_SPDY.ini']
    if not selected:return body,dict(changed=False,config_present=False)
    if len(selected)!=1 or not isinstance(selected[0].get('data'),str):raise SsoFormatError('SSO_CONFIG_SCHEMA_INVALID')
    data=selected[0]['data']
    # The native parser extracts raw INI text between JSON markers. Reject any
    # characters it cannot represent, rather than truncate or alter a value.
    if '"' in data or chr(92) in data:raise SsoFormatError('SSO_CONFIG_LEGACY_VALUE_UNSUPPORTED')
    token='MOMENTUM_INI_'+secrets.token_hex(16)
    rewritten=[]
    for item in files:
        if item is selected[0]:rewritten.append(dict(fname='SYS_SPDY.ini',data=token,**{k:v for k,v in item.items() if k not in {'fname','data'}}))
        else:rewritten.append(item)
    payload=dict(payload,iniFile=rewritten)
    canonical=json.dumps(payload,separators=(',',':'),ensure_ascii=True)
    canonical=canonical.replace(json.dumps(token),'"'+data+'"',1)
    result['user_data']=base64.b64encode(canonical.encode(encoding,errors='strict')).decode('ascii')
    output=json.dumps(result,separators=(',',':'),ensure_ascii=True).encode('utf-8')
    return output,dict(changed=output!=body,config_present=True)

class SameOriginRedirect(HTTPRedirectHandler):
    def __init__(self,origin):self.origin=origin
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        parsed=urlsplit(newurl)
        if (parsed.scheme,parsed.netloc)!=self.origin:raise SsoFormatError('SSO_REDIRECT_ORIGIN_CHANGED')
        return super().redirect_request(req,fp,code,msg,headers,newurl)

class ExclusiveServer(ThreadingHTTPServer):
    allow_reuse_address=False;daemon_threads=True
    def server_bind(self):
        if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        super().server_bind()

@contextmanager
def legacy_sso_endpoint(original,diagnostics,fetch=None,*,sdk_host=False):
    target=urlsplit(str(original))
    if target.scheme not in {'http','https'} or not target.netloc or target.username or target.password or target.fragment:
        raise SsoFormatError('SSO_ENDPOINT_INVALID')
    nonce=secrets.token_hex(32);requests=0;lock=threading.Lock()
    def upstream(query):
        url=urlunsplit((target.scheme,target.netloc,target.path,query,''))
        opener=build_opener(ProxyHandler({}),SameOriginRedirect((target.scheme,target.netloc)))
        with opener.open(Request(url,headers={'Accept':'application/json'}),timeout=8) as response:
            body=response.read(1048577)
            if len(body)>1048576:raise SsoFormatError('SSO_RESPONSE_TOO_LARGE')
            return body
    read=fetch or upstream
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            nonlocal requests
            parsed=urlsplit(self.path)
            expected='127.0.0.1:'+str(self.server.server_port)
            if self.client_address[0]!='127.0.0.1' or self.headers.get('Host')!=expected or parsed.path!=('/'+nonce+target.path if sdk_host else '/'+nonce):
                self.send_error(404);return
            with lock:
                requests+=1
                if requests>4:self.send_error(429);return
            try:
                body=read(parsed.query)
                normalized,flags=normalize_sso_reply(body)
                diagnostics['sso_format_compatibility']=flags
                self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(normalized)));self.end_headers();self.wfile.write(normalized)
            except Exception as exc:
                code=str(exc) if isinstance(exc,SsoFormatError) else 'SSO_FORMAT_'+type(exc).__name__
                diagnostics['sso_format_compatibility']=dict(changed=False,error=code)
                self.send_error(502)
    server=ExclusiveServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    local='http://127.0.0.1:'+str(server.server_port)+'/'+nonce
    if sdk_host:local='127.0.0.1:'+str(server.server_port)+'/'+nonce
    elif '?' in str(original):local+='?'+target.query
    try:yield local
    finally:server.shutdown();server.server_close();thread.join(timeout=1)


def broker_sso_url(host):
    # The official CheckSSO_Coding contract accepts a host, then constructs
    # this exact legacy service path itself. Do not guess or replace hosts.
    text=str(host)
    if not text or any(char in text for char in '/?#@'):
        raise SsoFormatError('SSO_HOST_INVALID')
    parsed=urlsplit('http://'+text)
    if not parsed.hostname:raise SsoFormatError('SSO_HOST_INVALID')
    try:port=parsed.port
    except ValueError:raise SsoFormatError('SSO_HOST_INVALID') from None
    if port is not None and not 1<=port<=65535:raise SsoFormatError('SSO_HOST_INVALID')
    return 'http://'+text+'/tc4grpright/getSsoClassID4Api.jsp?'
