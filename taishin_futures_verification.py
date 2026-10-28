"""One official website verification order, restricted to the test connection."""
import hashlib,json,re,sqlite3,threading,time
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation
from pathlib import Path
from taishin_futures_com import pump_com_events

class VerificationError(Exception):pass

def read_plan(path):
    try:
        raw=json.loads(Path(path).read_text(encoding='utf-8'))
        if raw.get('source_url')!='https://mlapi.tssco.com.tw/web_api/service/twftrade-api-testing':raise ValueError()
        if not re.fullmatch(r'[a-f0-9]{32}',raw['plan_id']):raise ValueError()
        if not re.fullmatch(r'[A-Z0-9]{4,12}',raw['symbol']):raise ValueError()
        if raw['side']!='BUY' or raw['price_type']!='LMT' or raw['tif']!='ROD':raise ValueError()
        if type(raw['quantity']) is not int or not 1<=raw['quantity']<=10:raise ValueError()
        price=Decimal(raw['price'])
        if not price.is_finite() or not 0<price<1000000:raise ValueError()
        if not re.fullmatch(r'[a-f0-9]{64}',raw['account_sha256']):raise ValueError()
        expires=datetime.fromisoformat(raw['expires_at'])
        if expires.tzinfo is None or datetime.now(timezone.utc)>=expires:raise ValueError()
        plan={k:raw[k] for k in ('plan_id','symbol','side','price_type','price','quantity','tif','account_sha256','expires_at','source_url')}
        digest=hashlib.sha256(json.dumps(plan,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        return plan,digest
    except (ValueError,KeyError,TypeError,InvalidOperation,OSError):
        raise VerificationError('VERIFICATION_PLAN_INVALID_OR_EXPIRED') from None

class VerificationService:
    def __init__(self,session,private):
        self.session=session;self.private=Path(private);self.lock=threading.RLock()
        self.private.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.private/'verification.sqlite3',isolation_level=None,check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute("CREATE TABLE IF NOT EXISTS attempts (plan_id TEXT PRIMARY KEY,digest TEXT NOT NULL,tag TEXT UNIQUE NOT NULL,state TEXT NOT NULL,order_no TEXT NOT NULL DEFAULT '',report TEXT NOT NULL DEFAULT '{}')")
        self.session.report_observer=self.on_report
    def status(self):
        plan,digest=read_plan(self.private/'verification-plan.json')
        with self.lock:row=self.db.execute('SELECT * FROM attempts WHERE plan_id=?',(plan['plan_id'],)).fetchone()
        public={k:plan[k] for k in ('symbol','side','price_type','price','quantity','tif','expires_at')}
        return dict(ok=True,validation_only=True,live_enabled=False,plan_digest=digest,plan=public,
            attempt=dict(state=row['state'],order_no=row['order_no'],report=json.loads(row['report'])) if row else None)
    def require_test_connection(self,plan):
        api=self.session.api
        if (self.session.environment!='validation' or api is None or not getattr(api,'_momentum_test_target',False)
                or not api.IsConn_Trade or not api.IsRecoverDone):
            raise VerificationError('VERIFICATION_TEST_CONNECTION_REQUIRED')
        matches=[a for key,a in api.fAccList.items() if hashlib.sha256(str(key).encode()).hexdigest()==plan['account_sha256']]
        if len(matches)!=1:raise VerificationError('VERIFICATION_ACCOUNT_MISMATCH')
        return api,matches[0]
    def submit(self,digest,wait=8):
        plan,expected=read_plan(self.private/'verification-plan.json')
        if not isinstance(digest,str) or digest!=expected:raise VerificationError('VERIFICATION_PLAN_CHANGED')
        api,account=self.require_test_connection(plan)
        tag='MV'+plan['plan_id'][:12]
        with self.lock:
            try:
                self.db.execute('INSERT INTO attempts(plan_id,digest,tag,state) VALUES(?,?,?,?)',(plan['plan_id'],expected,tag,'SUBMITTING'))
            except sqlite3.IntegrityError:raise VerificationError('VERIFICATION_ALREADY_ATTEMPTED') from None
        # The durable SUBMITTING record is committed before entering native I/O.
        try:
            accepted=api.send_website_verification(plan,account,tag)
        except Exception:
            self._state(plan['plan_id'],'STATUS_UNKNOWN')
            raise VerificationError('VERIFICATION_STATUS_UNKNOWN_NO_RETRY') from None
        if not accepted:
            self._state(plan['plan_id'],'TRANSPORT_REJECTED')
            raise VerificationError('VERIFICATION_TRANSPORT_REJECTED_NO_RETRY')
        deadline=time.monotonic()+wait
        while True:
            pump_com_events()
            result=self.status()
            if result['attempt']['order_no'] or time.monotonic()>=deadline:break
            time.sleep(.05)
        if not result['attempt']['order_no']:
            self._state(plan['plan_id'],'STATUS_UNKNOWN');result=self.status()
        return result
    def _state(self,plan_id,state):
        with self.lock:self.db.execute("UPDATE attempts SET state=? WHERE plan_id=? AND order_no=''",(state,plan_id))
    def on_report(self,report):
        user=str(getattr(report,'UserDefine',''))
        with self.lock:
            rows=self.db.execute('SELECT plan_id,tag FROM attempts').fetchall()
            matches=[row for row in rows if user.endswith(row['tag'])]
            if len(matches)!=1:return
            number=str(getattr(report,'OrderID',''))
            if not re.fullmatch(r'[A-Za-z0-9]{1,32}',number):return
            try:status=int(report.OrderStatus);filled=int(report.CumQty)
            except (AttributeError,TypeError,ValueError):return
            state={2:'ACCEPTED',71:'PARTIAL_FILLED',72:'FILLED',11:'REJECTED',6:'CANCELLED'}.get(status,'REPORT_RECEIVED')
            safe=dict(status=status,filled_quantity=filled)
            self.db.execute('UPDATE attempts SET state=?,order_no=?,report=? WHERE plan_id=?',(state,number,json.dumps(safe),matches[0]['plan_id']))
