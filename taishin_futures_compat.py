"""Narrow Windows locale compatibility for Solace's read-only API identity field."""
import os

def read_session_property(reader,instance,name,*,platform=os.name,decode_windows=None):
    try:
        return reader(instance,name)
    except UnicodeDecodeError as error:
        # SESSION_USER_ID is documented by Solace as OS user/hostname/PID metadata,
        # not broker authentication or market/order data. Native Windows ANSI
        # metadata can differ from the Python SDK's unconditional UTF-8 decode.
        if platform!='nt' or name!='SESSION_USER_ID' or error.encoding.lower().replace('_','-') not in {'utf-8','utf8'}:
            raise
        decoder=decode_windows or (lambda raw:raw.decode('mbcs',errors='strict'))
        return decoder(error.object)

def install_windows_identity_decoder():
    if os.name!='nt':return
    from solace.messaging.core._solace_session import _SolaceSession
    original=_SolaceSession.get_session_property
    if getattr(original,'_momentum_identity_decoder',False):return
    def guarded(instance,name):
        return read_session_property(original,instance,name)
    guarded._momentum_identity_decoder=True
    _SolaceSession.get_session_property=guarded


def auth_transport_ready(api):
    client=getattr(api,'_SolAuthClient',None)
    return bool(client and getattr(client,'_is_connected',False)
                and getattr(client,'_message_service',None) is not None)


OPTIONAL_CALLBACKS=('OnAccListEvent','OnTxConnEvent','OnRecoverEvent',
    'OnGWQryConnEvent','OnGWQryResFailEvent','OnGWQryResSuccEvent',
    'OnResultReportEvent','OnAnnouncementEvent')

def _ignore_optional_event(*args):
    return None

def initialize_optional_callbacks(api):
    # The vendor registers these with CLR += even when no UI handler was set.
    # Retain real handlers and the SDK's own state/recovery callbacks unchanged.
    for name in OPTIONAL_CALLBACKS:
        callback=getattr(api,name,None)
        if callback is None:setattr(api,name,_ignore_optional_event)
        elif not callable(callback):raise TypeError('FUTURES_CALLBACK_NOT_CALLABLE')


def configure_tls12(manager=None,protocol=None):
    """Set TLS 1.2 in this SDK process; keep system certificate validation."""
    if manager is None:
        from System.Net import ServicePointManager,SecurityProtocolType
        manager=ServicePointManager;protocol=SecurityProtocolType.Tls12
    manager.SecurityProtocol=protocol
    return dict(protocol=str(manager.SecurityProtocol),
        custom_certificate_validator=manager.ServerCertificateValidationCallback is not None)


def summarize_authorization(reply):
    counts={'not_applied':0,'not_verified':0,'verified':0,'approved':0,'unknown':0}
    labels={0:'not_applied',1:'not_verified',2:'verified',3:'approved'}
    accounts=getattr(reply,'list',[]) or []
    for account in accounts:
        status=account.get('statusCode') if isinstance(account,dict) else None
        counts[labels.get(status,'unknown')]+=1
    return dict(result=bool(getattr(reply,'result',False)),account_count=len(accounts),status_counts=counts)


def summarize_sso(success,message):
    # Return only fixed categories and recognized HTTP statuses, never raw SDK text.
    text=str(message).lower()
    reasons=[]
    catalog={
        'TLS_FAILURE':('ssl','tls','secure channel','安全通道','安全通訊'),
        'CERTIFICATE_FAILURE':('certificate','憑證','認證機構'),
        'NETWORK_FAILURE':('timeout','timed out','逾時','無法連線','連線失敗','name resolution','遠端名稱','remote name'),
        'CREDENTIALS_REJECTED':('密碼錯誤','帳密錯誤','password incorrect','invalid password','密碼不符'),
        'ACCOUNT_LOCKED':('鎖定','locked'),
        'APPROVAL_REQUIRED':('未驗證','未開通','未申請','未授權'),
        'EMPTY_ACCOUNT_LIST':('沒有傳回交易帳號','無期貨帳號'),
        'HTTP_ERROR':('404','403','500','502','503'),
    }
    if not success:
        for category,markers in catalog.items():
            if any(marker in text for marker in markers):reasons.append(category)
        if not reasons:reasons.append('SSO_REJECTED_UNCLASSIFIED')
    return dict(ok=bool(success),reasons=reasons)
