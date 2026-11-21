# -*- coding: utf-8 -*-
"""
交易系統執行期元件與工作流程編排（Runtime Components）。

本模組負責 Momentum Scalping 交易系統執行期間的資料流、狀態與背景工作協調，
將市場／Scanner、MarketState Trade Event、持倉／CTS 對帳、交易執行、
執行紀錄與背景 Worker 分離為獨立 Runtime Component。

模組職責：
1. 編排 Market／Scanner 執行期資料流程與 Snapshot 狀態。
2. 接收並分派 MarketState Trade Event，維持執行期事件處理邊界。
3. 協調系統管理持倉、CTS 庫存／成交對帳與交易執行工作。
4. 維護 Runtime Snapshot、執行紀錄與背景 Worker 間的狀態交換。
5. 保持 GUI 主執行緒與 Gateway／CTS／背景工作隔離，避免阻塞 Tk 事件迴圈。

模組邊界：
- 不建立或操作 Tk 畫面。
- 不實作 market / scanner / intraday / trading 的核心策略公式。
- 不取代各核心模組既有的策略參數與交易規則。
- GUI 中文說明統一維護於 params.md，本檔不另建 GUI 說明。
- GUI 主執行緒只建立物件，不做外部工作
- Gateway 必須 Lazy Initialization
- CTS／Gateway 工作只能由指定 Worker 執行
- WorkerCoordinator.start() 只能「啟動 Worker」
- runtime.snapshot() 必須是純記憶體讀取
- GUI Snapshot Queue 絕對不能形成 feedback loop
- GUI 每輪處理 Queue 必須有上限
- Runtime 不自行建立策略參數
- Scanner 每輪只使用一次 APIGateway 公開 Atomic MarketState Snapshot，後到行情只進下一輪
- Runtime 不讀取 APIGateway / MarketState 私有欄位，只使用正式公開介面
- Market 更新失敗只記錄原始錯誤、error_stage 與 stage_errors，不自行補抓
"""

# 中文重點更新：以輸入身分、TTL、交易日及設定驗收快照，不再以行情與市場序號完全相同限制交接；送單重讀當下市場與類股。

# 中文重點更新：清理歷史相容參數、孤立註解及無效追單顯示，保留測試、診斷、CTS 撤單與委託及持倉對帳。

# 中文重點更新：清除未使用函式、相容介面與無效參數；保留市價買賣、成交對帳、撤買單、清倉及離線驗證。

# 中文重點更新：統一偵測核心與固定程序池，計算分批降載；清倉時鐘獨立於行情，持倉更新集中交易序列，短期資料及紀錄有界。

# 中文重點更新：保存策略與減碼狀態、排除送單前舊對帳回覆、消化 CTS 回呼佇列並減少重複運算及寫入。

# 中文重點更新：市場掃描回合完成判定讀取 Scanner 設定的時限，並回報實際使用秒數。
# 中文重點更新：自動訊號標記 AUTO 使整波延伸保護生效；已確認成交之每日淨損益納入風控。
# 中文重點更新：保留出場判斷與券商成交的對照資訊，方便查明短線停損或動能出場。

# 中文重點更新：補齊審查要求之狀態識別、整張委託、資料時序與恢復安全邊界。

# 中文重點更新：市場掃描紀錄加入 Candidate 管線、資格淘汰與評分閘門摘要，使候選為零時可直接判斷實際阻擋層。

# 中文重點更新：近期異常與恢復事件另存固定紀錄檔；外部 universe 快照降級時保留 Candidate 更新並維持買進失效關閉。

# 中文重點更新：提供原子市場快照給 Trading 最終許可檢查，並將許可原因寫入掃描狀態。

# 中文重點更新：發布核心市場許可供 GUI 顯示與交易再次檢查，不把資料逾時當成強制賣出訊號。

# 中文重點更新：凍結傳遞類股對照與候選指紋；Entry 路徑變化寫入紀錄以追蹤未進場原因。

# 中文重點更新：完整 Entry 歷程、CTS 恢復對帳、策略 checkpoint 與可解除的行情／日誌健康鎖。


# 中文重點更新：掃描紀錄加入類股可用性、分類涵蓋與最強類股，便於區分來源缺漏及未達門檻。

# 中文重點更新：一般診斷紀錄取消逐筆磁碟強制同步，交易狀態檢查點維持原耐久性；串流健康與掃描錯誤分開追蹤，重連限由健康工作執行緒處理。


# 中文重點更新：送單前失敗回呼維持拒絕終態；對帳異常保存完整堆疊供追查。

# 中文重點更新：Fugle 重連設定缺省值對齊三十至六十秒，Scanner 三十秒逾時維持原設定。

# 中文重點更新：送單前記錄訊號回落複驗實值與結果，沿用非同步執行流程紀錄。

# 中文重點更新：依有效委託鎖定進場，分開統計執行與券商呼叫；保存逐階段紀錄，同輪凍結分類與類股快照。

# 中文重點更新：保留失敗查詢前的持倉快照；手動市價繞過策略評估；實際多筆成交以差額更新成本並保留重送鎖。

# 中文重點更新：持倉與候選集合訂閱 Books；退出立即失效，成交 Universe 訂閱維持獨立。

# 中文重點更新：消除 Books 訂閱與行情回呼鎖順序相反造成的互鎖；計算池逾時後回收並於下一輪重建。

# 中文重點更新：單一顯示排程、工作例外隔離與恢復；行情回呼改用有界佇列，一般狀態保存節流但委託落盤保留。

# 中文重點更新：逐筆成交讀取輕量斷線識別，保留重連時清除舊事件緩衝的行為，避免每筆重建並排序完整訂閱狀態。

# 中文重點更新：交易與異常統一按日批次寫入；停止常態偵測歷程及重複紀錄，顯示快照依原刷新間隔合併；交易與安全狀態保存不變。

# 中文重點更新：持倉健康僅於異常與恢復轉換記錄原因；交易顯示優先，取消重複刷新等待；狀態檔多行保存並重用已驗證備份，操作紀錄分段且保留成交復原。

from __future__ import annotations
import traceback
import uuid
import functools
import argparse
import copy
import hashlib
import json
import os
import queue
import threading
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


# =============================================================================
# Runtime 固定契約
# =============================================================================

DAILY_TRADE_REPORT_PREFIX = "daily_trade_report"
# GUI 目前仍匯入此名稱供 diagnose 顯示；Runtime 不使用它判斷 Candidate freshness。
# 實際新鮮度與 trade_ready 由 ScannerParameters.realtime_freshness_max_age_seconds 決定。
CANDIDATE_MAX_AGE_SECONDS = 60.0

_RUNTIME_EVENT_QUEUE_SIZE = 4096
_RUNTIME_COMMAND_QUEUE_SIZE = 128
_RUNTIME_RESULT_QUEUE_SIZE = 256
_RUNTIME_RECENT_LOG_LIMIT = 500
_TRADING_LOOP_IDLE_SECONDS = 0.01
_CTS_RECONCILE_MIN_INTERVAL_SECONDS = 5.0
_TRADING_CYCLE_HEARTBEAT_SECONDS = 60.0
_MARKET_ERROR_SUMMARY_SECONDS = 60.0


# =============================================================================
# 共用工具
# =============================================================================


def _now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def _now_text() -> str:
    return _now().isoformat(timespec="milliseconds")


def _trade_date_int(now: Optional[datetime] = None) -> int:
    return int((now or _now()).strftime("%Y%m%d"))


def _trade_date_text(now: Optional[datetime] = None) -> str:
    return (now or _now()).strftime("%Y%m%d")


def _hour_key(now: Optional[datetime] = None) -> str:
    return (now or _now()).strftime("%Y%m%d_%H")


def _normalize_market(value: Any) -> str:
    text = str(value or "").strip()
    upper = text.upper()
    if upper in {"TWSE", "TSE", "LISTED"}:
        return "TWSE"
    if upper in {"TPEX", "OTC"}:
        return "TPEx"
    return text


def _symbol_key(symbol: Any, market: Any) -> Tuple[str, str]:
    return str(symbol or "").strip(), _normalize_market(market)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, set):
        return sorted(_jsonable(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _stable_hash(payload: Any) -> str:
    text = json.dumps(
        _jsonable(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _settings_mapping(settings: Any) -> Dict[str, Any]:
    if is_dataclass(settings):
        return dict(asdict(settings))
    if isinstance(settings, Mapping):
        return dict(settings)
    try:
        return dict(vars(settings))
    except TypeError:
        return {}


def _setting(settings: Any, name: str, default: Any = None) -> Any:
    if isinstance(settings, Mapping):
        return settings.get(name, default)
    return getattr(settings, name, default)


def _parameter(registry: Any, key: str) -> Any:
    bundles = getattr(registry, "bundles", {})
    bundle = bundles.get(key) if isinstance(bundles, Mapping) else None
    if bundle is None:
        return None
    return getattr(bundle, "current", None)


def _parameter_fingerprint(registry: Any, key: str) -> str:
    fingerprints = getattr(registry, "fingerprints", None)
    if callable(fingerprints):
        try:
            values = fingerprints()
            if isinstance(values, Mapping):
                return str(values.get(key) or "")
        except Exception:
            pass
    parameter = _parameter(registry, key)
    if parameter is None:
        return ""
    if hasattr(parameter, "snapshot") and callable(parameter.snapshot):
        try:
            return _stable_hash(parameter.snapshot())
        except Exception:
            pass
    return _stable_hash(parameter)


def _response_rows(
    response: Any, *, normalized_first: bool = False
) -> List[Dict[str, Any]]:
    if not isinstance(response, Mapping):
        return []
    keys = (
        ("normalized_result", "parsed_result")
        if normalized_first
        else ("parsed_result",)
    )
    for key in keys:
        rows = response.get(key)
        if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
            return [dict(row) for row in rows if isinstance(row, Mapping)]
    return []


def _candidate_rows(context: Mapping[str, Any]) -> List[Dict[str, Any]]:
    rows = context.get("candidates")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return []
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def _redact(value: Any) -> Any:
    secret_names = {
        "password",
        "passwd",
        "pwd",
        "token",
        "api_key",
        "apikey",
        "secret",
    }
    if isinstance(value, Mapping):
        output: Dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            output[name] = "***" if name.lower() in secret_names else _redact(item)
        return output
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    return _jsonable(value)


def _quote_sequence_value(row: Mapping[str, Any]) -> int:
    for name in ("symbol_quote_sequence", "quote_sequence"):
        try:
            return int(row.get(name) or 0)
        except (TypeError, ValueError):
            continue
    return 0


def _event_sort_key(row: Mapping[str, Any]) -> Tuple[int, int, int, str, str]:
    try:
        event_time = int(row.get("event_time") or 0)
    except (TypeError, ValueError):
        event_time = 0
    try:
        serial = int(row.get("serial") or -1)
    except (TypeError, ValueError):
        serial = -1
    return (
        _quote_sequence_value(row),
        event_time,
        serial,
        str(row.get("symbol") or ""),
        _normalize_market(row.get("market")),
    )


def _source_age_seconds(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_now().tzinfo)
    return max(0.0, (_now() - dt.astimezone(_now().tzinfo)).total_seconds())


def _module_base_dir(modules: Mapping[str, Any]) -> Path:
    for key in ("api_gateway", "market", "scanner", "intraday", "trading"):
        module = modules.get(key)
        path = str(getattr(module, "__file__", "") or "").strip()
        if path:
            return Path(path).resolve().parent
    return Path.cwd().resolve()


# =============================================================================
# 執行紀錄 Component
# =============================================================================


STATE_OWNERS = {
    "market": "RuntimeMarketScannerComponent", "strategy": "RuntimeIntradayComponent",
    "position_order": "RuntimePositionComponent", "risk": "RuntimePositionComponent",
    "config": "TradingSystemRuntime", "execution": "CTSOwner",
}


def _serialized_state(method):
    """Runtime 狀態交易共用序列，讀取、驗證、保留與送單不可交錯。"""
    @functools.wraps(method)
    def call(self, *args, **kwargs):
        with self._state_transaction:
            return method(self, *args, **kwargs)
    return call


def _serialized_execution(method):
    @functools.wraps(method)
    def call(self, runtime, *args, **kwargs):
        with runtime._state_transaction:
            return method(self, runtime, *args, **kwargs)
    return call


def _compute_envelope(fn, batch, token):
    return {"token": token, "value": fn(batch)}


class ResourceManager:
    """單一資源配額；固定程序池，只調整每波送件數，交易狀態不跨程序。"""

    def __init__(self):
        self.logical_cores = max(1, os.cpu_count() or 1)
        self.physical_cores = self.logical_cores
        self.core_detection = "logical_fallback"
        self._psutil = None
        try:
            import psutil
            self._psutil = psutil
            self.physical_cores = psutil.cpu_count(logical=False) or self.logical_cores
            self.core_detection = "psutil"
            allowed = len(psutil.Process().cpu_affinity()) if hasattr(psutil.Process(), "cpu_affinity") else self.logical_cores
            self.logical_cores = max(1, min(self.logical_cores, allowed))
            psutil.cpu_percent(interval=None)
        except Exception:
            self._psutil = None
            try:
                if os.name == "nt":
                    import ctypes
                    from ctypes import wintypes
                    class ProcessorInfo(ctypes.Structure):
                        _fields_ = [("mask", ctypes.c_size_t), ("relationship", ctypes.c_int), ("data", ctypes.c_ulonglong * 2)]
                    size = wintypes.DWORD(0)
                    fn = ctypes.windll.kernel32.GetLogicalProcessorInformation
                    fn(None, ctypes.byref(size))
                    items = (ProcessorInfo * (size.value // ctypes.sizeof(ProcessorInfo)))()
                    if not fn(items, ctypes.byref(size)):
                        raise OSError("CPU topology unavailable")
                    self.physical_cores = sum(x.relationship == 0 for x in items) or self.logical_cores
                    self.core_detection = "windows_topology"
                else:
                    cores = set()
                    affinity = os.sched_getaffinity(0) if hasattr(os, 'sched_getaffinity') else None
                    for line in Path('/proc/cpuinfo').read_text().split('\n\n'):
                        pairs = dict(x.split(':', 1) for x in line.splitlines() if ':' in x)
                        pairs = {k.strip(): v.strip() for k, v in pairs.items()}
                        if 'core id' in pairs and (affinity is None or int(pairs.get('processor', -1)) in affinity):
                            cores.add((pairs.get('physical id', '0'), pairs['core id']))
                    if cores:
                        self.physical_cores = len(cores)
                        self.core_detection = "linux_topology"
                    if hasattr(os, 'sched_getaffinity'):
                        self.logical_cores = min(self.logical_cores, len(os.sched_getaffinity(0)))
            except (OSError, ValueError, AttributeError):
                pass
        self.physical_cores = max(1, min(self.physical_cores, self.logical_cores))
        self.reserved_cores = 2 if self.logical_cores >= 4 else 1
        cap = 4 if __import__('struct').calcsize('P') == 4 else 8
        if self._psutil is not None:
            cap = min(cap, max(1, int(self._psutil.virtual_memory().available // (384 * 1024 * 1024))))
        self.cpu_workers = max(1, min(cap, self.physical_cores - self.reserved_cores,
                                      self.logical_cores - self.reserved_cores))
        self.io_workers = max(4, min(16, self.logical_cores * 2))
        self._io_pools = {}
        self._cpu_pool = None
        self._cpu_lock = threading.Lock()
        self._pool_lock = threading.Lock()
        self._outstanding = set()
        self._closed = False
        self._cpu_failed = False
        self.compute_token = {}
        self._load = 0.0
        self._last_sample = 0.0
        self._system_times = None
        self._last_error = ""

    def io_pool(self, name, workers=None):
        with self._pool_lock:
            if self._closed:
                raise RuntimeError("資源管理已關閉")
            if name not in self._io_pools:
                self._io_pools[name] = ThreadPoolExecutor(
                    max_workers=min(workers or self.io_workers, self.io_workers),
                    thread_name_prefix=name)
            return self._io_pools[name]

    def sample_load(self):
        if time.monotonic() - self._last_sample < 1:
            return self._load
        self._last_sample = time.monotonic()
        try:
            if self._psutil is not None:
                self._load = float(self._psutil.cpu_percent(interval=None))
            elif os.name == 'nt':
                import ctypes
                times = [ctypes.c_ulonglong() for _ in range(3)]
                if ctypes.windll.kernel32.GetSystemTimes(*(ctypes.byref(x) for x in times)):
                    current = tuple(x.value for x in times)
                    if self._system_times:
                        idle, kernel, user = [a-b for a,b in zip(current, self._system_times)]
                        self._load = 100 * (1-idle / max(1,kernel+user))
                    self._system_times = current
            else:
                self._load = min(100.0, os.getloadavg()[0] / self.logical_cores * 100)
        except (OSError, AttributeError):
            pass
        return self._load

    def compute_batches(self, fn, batches, timeout):
        # 一個掃描回合持有此鎖；回合最多保留一波 work，逾時工作未結束不再累積。
        from concurrent.futures import wait, TimeoutError
        with self._cpu_lock:
            self._outstanding = {f for f in self._outstanding if not f.done()}
            if self._closed or self._cpu_failed or self._outstanding:
                raise RuntimeError("計算池尚未就緒或前次工作未完成")
            if self._cpu_pool is None:
                import multiprocessing
                from concurrent.futures import ProcessPoolExecutor
                for env_name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
                    os.environ[env_name] = "1"
                self._cpu_pool = ProcessPoolExecutor(
                    max_workers=self.cpu_workers,
                    mp_context=multiprocessing.get_context('spawn'))
            deadline = time.monotonic() + max(0.01, timeout)
            results = []
            offset = 0
            try:
                while offset < len(batches):
                    load = self.sample_load()
                    width = 1 if load >= 85 else max(1, self.cpu_workers // 2) if load >= 70 else self.cpu_workers
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Scanner 計算超過本輪時限")
                    token = dict(self.compute_token, batch_id=uuid.uuid4().hex)
                    wave = [self._cpu_pool.submit(_compute_envelope, fn, copy.deepcopy(batch), token)
                            for batch in batches[offset:offset+width]]
                    self._outstanding.update(wave)
                    done, pending = wait(wave, timeout=max(0, deadline-time.monotonic()))
                    if pending:
                        for future in pending:
                            future.cancel()
                        raise TimeoutError("Scanner 計算逾時；不發布過時結果")
                    envelopes = [f.result() for f in wave]
                    if any(e["token"] != token for e in envelopes):
                        raise RuntimeError("計算結果識別不符")
                    results.extend(e["value"] for e in envelopes)
                    self._outstanding.difference_update(wave)
                    offset += len(wave)
                self._last_error = ""
                return results
            except Exception as exc:
                from concurrent.futures.process import BrokenProcessPool
                if isinstance(exc, (BrokenProcessPool, TimeoutError)):
                    self._discard_cpu_pool()
                self._last_error = f"{type(exc).__name__}: {exc}"
                for future in self._outstanding:
                    future.cancel()
                raise

    def _discard_cpu_pool(self):
        """僅終止純計算子程序；逾時或程序損壞後下一輪重建，不影響 CTS。"""
        pool, self._cpu_pool = self._cpu_pool, None
        self._cpu_failed = False
        for future in self._outstanding:
            future.cancel()
        self._outstanding.clear()
        if pool is not None:
            processes = tuple((getattr(pool, "_processes", None) or {}).values())
            pool.shutdown(wait=False, cancel_futures=True)
            for process in processes:
                if process.is_alive():
                    process.terminate()
            for process in processes:
                process.join(timeout=0.2)

    def snapshot(self):
        return {"logical_cores": self.logical_cores, "physical_cores": self.physical_cores,
                "core_detection": self.core_detection, "reserved_cores": self.reserved_cores,
                "cpu_workers": self.cpu_workers, "io_workers": self.io_workers,
                "cpu_load_pct": round(self.sample_load(), 1),
                "pending_compute": sum(not f.done() for f in tuple(self._outstanding)),
                "error": self._last_error}

    def close(self):
        with self._pool_lock:
            self._closed = True
            pools = list(self._io_pools.values())
            if self._cpu_pool is not None:
                pools.append(self._cpu_pool)
        for pool in pools:
            pool.shutdown(wait=False, cancel_futures=True)


def _compact_log(value, depth=0):
    """保留動作、價格、數量、委託識別、拒絕碼及耗時；完整行情不寫入操作紀錄。"""
    if depth > 5:
        return None
    if isinstance(value, Mapping):
        omitted = {"parameters", "parameter_snapshot", "bars", "aggregates", "quotes",
                   "market_state_snapshot", "market_context", "candidate", "candidates",
                   "account_state", "existing_position", "account_summary", "position",
                   "raw", "raw_result", "raw_match_result", "parsed", "parsed_result",
                   "records", "rows", "query_attempts", "trading_input", "component_scores",
                   "order_registry", "system_order_registry", "market_state", "source_data"}
        return {str(k): (v if str(k) == "traceback" and isinstance(v, str) else _compact_log(v, depth+1)) for k,v in value.items()
                if k not in omitted and v not in (None, "", [], {})}
    if isinstance(value, (list, tuple)):
        return [_compact_log(x, depth+1) for x in value[:12]]
    if isinstance(value, str):
        return value[:800]
    return value


class RuntimeJournalComponent:
    """負責 Runtime 執行流程、交易執行與當日成交紀錄。"""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.log_dir = self.base_dir / "execution_logs"
        self._lock = threading.RLock()
        self._run_flow_seq = 0
        self._execution_seq = 0
        self._recent_run_flow: List[Dict[str, Any]] = []
        self._recent_errors: List[Dict[str, Any]] = []
        self._recent_execution: List[Dict[str, Any]] = []
        self._recent_daily_trade: List[Dict[str, Any]] = []
        self._daily_fingerprints: set[str] = set()
        self._daily_ledger: Dict[str, Dict[str, Any]] = {}
        self._ledger_day = ""
        self._metrics_cache: Dict[str, Any] = {}
        self._last_write_error = ""
        self._historical_write_errors = []
        self._journal_recovery_pending = False
        self._registry_digest = None
        self._log_repeat = {}
        self._log_queue = queue.Queue(maxsize=2048)
        self._log_thread = None
        self._log_closed = False
        self._state_good_signature = None
        self._trade_revision = 0

    def save_runtime_state(self, payload):
        with self._lock:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            target = self.log_dir / "runtime_state.json"
            temporary = target.with_suffix(".tmp")
            try:
                envelope = {"payload": payload, "checksum": _stable_hash(payload)}
                with temporary.open("w", encoding="utf-8") as handle:
                    json.dump(envelope, handle, ensure_ascii=False, default=str, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                previous_valid = False
                previous_payload = None
                if target.exists():
                    stat = target.stat()
                    signature = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
                    previous_valid = signature == self._state_good_signature
                    if not previous_valid:
                        try:
                            with target.open("r", encoding="utf-8") as handle:
                                old = json.load(handle)
                            previous_valid = old.get("checksum") == _stable_hash(old.get("payload"))
                            if previous_valid:
                                previous_payload = old
                        except ValueError:
                            pass
                if previous_valid:
                    backup = target.with_suffix(".good.tmp")
                    if previous_payload is not None:
                        # 首次接續舊單行檔時也將備份排版，不刪除任何狀態。
                        with backup.open("w", encoding="utf-8") as handle:
                            json.dump(previous_payload, handle, ensure_ascii=False, indent=2)
                            handle.flush()
                            os.fsync(handle.fileno())
                    else:
                        # 已驗證且未被外部更動的檔案逐塊複製，避免反覆解析及常駐大型副本。
                        import shutil
                        with target.open("rb") as source, backup.open("wb") as handle:
                            shutil.copyfileobj(source, handle, length=1024 * 1024)
                            handle.flush()
                            os.fsync(handle.fileno())
                    os.replace(backup, target.with_suffix(".good"))
                os.replace(temporary, target)
                stat = target.stat()
                self._state_good_signature = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
                if self._last_write_error and self._journal_recovery_pending:
                    self._append_jsonl(self.execution_log_path, {"time":_now_text(),"status":"RECOVERED","previous_error":self._last_write_error})
                    self._historical_write_errors.append(self._last_write_error)
                    self._historical_write_errors = self._historical_write_errors[-100:]
                    self._last_write_error = ""
                    self._journal_recovery_pending = False
            except (OSError, ValueError) as exc:
                self._last_write_error = str(exc)
                raise

    def load_runtime_state(self):
        target = self.log_dir / "runtime_state.json"
        if not target.exists() and not target.with_suffix(".good").exists():
            return {}
        for path in (target, target.with_suffix(".good")):
            try:
                envelope = json.loads(path.read_text(encoding="utf-8"))
                if envelope["checksum"] == _stable_hash(envelope["payload"]):
                    return envelope["payload"]
            except (OSError, ValueError, KeyError):
                continue
        self._last_write_error = "JOURNAL_INTEGRITY_FAILED"
        raise RuntimeError(self._last_write_error)


    def load_order_registry(self) -> List[Dict[str, Any]]:
        path = self.log_dir / "system_orders.json"
        if not path.is_file():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list) or not all(
            isinstance(row, dict) for row in payload
        ):
            raise RuntimeError("系統委託身分檔格式錯誤，無法完成對帳")
        return payload

    def save_order_registry(self, rows: Sequence[Mapping[str, Any]]) -> None:
        with self._lock:
            digest = _stable_hash(rows)
            if digest == self._registry_digest:
                return
            self.log_dir.mkdir(parents=True, exist_ok=True)
            target = self.log_dir / "system_orders.json"
            temporary = target.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(
                    _redact(list(rows)), stream, ensure_ascii=False, sort_keys=True, indent=2
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            self._registry_digest = digest

    @property
    def execution_log_path(self) -> Path:
        return self.log_dir / f"operations_{_trade_date_text()}.jsonl"

    @property
    def daily_trade_report_path(self) -> Path:
        return self.execution_log_path

    @property
    def run_flow_log_path(self) -> Path:
        return self.execution_log_path

    @property
    def recent_error_log_path(self) -> Path:
        return self.execution_log_path

    def _append_jsonl(self, path: Path, payload: Mapping[str, Any]) -> bool:
        # 接受紀錄與關閉使用同一把鎖，避免紀錄排在終止標記後方。
        with self._lock:
            if self._log_closed:
                self._last_write_error = "JOURNAL_CLOSED"
                return False
            if self._log_thread is None:
                self._log_thread = threading.Thread(target=self._log_loop, name="RuntimeLogWriter", daemon=True)
                self._log_thread.start()
            try:
                self._log_queue.put_nowait((path, copy.deepcopy(dict(payload))))
                return True
            except queue.Full:
                self._last_write_error = "JOURNAL_QUEUE_FULL"
                return False

    def _log_loop(self):
        while True:
            item = self._log_queue.get()
            batch = [item]
            if item is not None:
                while len(batch) < 128:
                    try:
                        following = self._log_queue.get_nowait()
                    except queue.Empty:
                        break
                    batch.append(following)
                    if following is None:
                        break
            stop = batch[-1] is None
            try:
                groups = []
                for record in batch:
                    if record is None:
                        continue
                    path, payload = record
                    if not groups or groups[-1][0] != path:
                        groups.append((path, []))
                    groups[-1][1].append(payload)
                for path, payloads in groups:
                    self._write_jsonl_batch(path, payloads)
            except Exception as exc:
                # 寫入失敗仍交由原有 journal 健康鎖處理，Writer 不靜默死亡。
                self._last_write_error = f"{type(exc).__name__}: {exc}"
            finally:
                for _ in batch:
                    self._log_queue.task_done()
            if stop:
                return

    def close(self):
        with self._lock:
            if self._log_closed:
                return
            self._log_closed = True
        if self._log_thread:
            try:
                self._log_queue.put(None, timeout=1)
                self._log_thread.join(timeout=5)
                if self._log_thread.is_alive():
                    self._last_write_error = "JOURNAL_DRAIN_TIMEOUT"
            except queue.Full:
                self._last_write_error = "JOURNAL_DRAIN_TIMEOUT"

    def _write_jsonl(self, path: Path, payload: Mapping[str, Any]) -> bool:
        return self._write_jsonl_batch(path, [payload])

    def _write_jsonl_batch(self, path: Path, payloads) -> bool:
        try:
            lines = [(json.dumps(_redact(payload), ensure_ascii=False, default=str) + "\n").encode("utf-8")
                     for payload in payloads]
            path.parent.mkdir(parents=True, exist_ok=True)
            limit = 8 * 1024 * 1024
            size = path.stat().st_size if path.exists() else 0
            handle = path.open("ab")
            try:
                for line in lines:
                    if size and size + len(line) > limit:
                        handle.flush()
                        handle.close()
                        parts = sorted(path.parent.glob(path.stem + ".part*.jsonl"))
                        number = int(parts[-1].stem.rsplit("part", 1)[1]) + 1 if parts else 1
                        os.replace(path, path.with_name(f"{path.stem}.part{number:06d}.jsonl"))
                        handle = path.open("ab")
                        size = 0
                    handle.write(line)
                    size += len(line)
                handle.flush()
                if self._last_write_error:
                    os.fsync(handle.fileno())
                    self._journal_recovery_pending = True
            finally:
                handle.close()
        except (OSError, ValueError, TypeError) as exc:
            self._last_write_error = f"{type(exc).__name__}: {exc}"
            return False
        return True

    @staticmethod
    def keep_run_flow(stage, status, details=None):
        details = details or {}
        if stage == "POSITION_LIVENESS":
            return status in {"WARN", "INFO"}
        if any(details.get(key) for key in ("error", "errors", "market_error", "stage_errors", "traceback")):
            return True
        if stage in {"ENTRY_SIGNAL_CREATED", "ENTRY_PATH_TRACE", "ENTRY_TRIGGER_REVALIDATION",
                     "BUY_REVALIDATION_OK", "PRE_SUBMIT_RESULT"}:
            return False
        if stage.startswith("ORDER_") or stage == "EXECUTION_RESULT":
            return True
        if stage == "TRADING_CYCLE":
            return details.get("cts_reason") == "CTS_QUERY_FAILED"
        return status in {"ERROR", "ERROR_SUMMARY", "WARN", "WARNING", "DEGRADED",
                          "FAILED", "TIMEOUT", "RECOVERED", "MISSING"}

    @staticmethod
    def execution_is_error(payload):
        execution = payload.get("execution") or payload
        return bool(execution.get("error") or execution.get("traceback") or
                    execution.get("status") in {"ORDER_STATUS_UNKNOWN", "ORDER_REJECTED",
                    "POSITION_MATCH_MISMATCH", "MARKETSTATE_QUOTE_MISSING", "CTS_QUERY_FAILED"} or
                    (execution.get("revalidation") or {}).get("severity") == "CRITICAL")

    @classmethod
    def keep_execution(cls, payload):
        execution = payload.get("execution") or payload
        if cls.execution_is_error(payload):
            return True
        if any(execution.get(key) for key in ("broker_submit_attempt", "broker_unknown", "executed",
                                              "order_id", "order_no", "order_intent_id", "events", "identity_recovery")):
            return True
        return execution.get("status") in {"FILLED", "SUBMITTED", "SUBMITTED_NOT_FILLED",
                    "PARTIALLY_FILLED", "CANCELLED", "CANCEL_REQUESTED"}

    @staticmethod
    def _bounded_append(rows: List[Dict[str, Any]], item: Dict[str, Any]) -> None:
        rows.append(item)
        if len(rows) > _RUNTIME_RECENT_LOG_LIMIT:
            del rows[: len(rows) - _RUNTIME_RECENT_LOG_LIMIT]

    def record_run_flow(
        self,
        *,
        run_id: str,
        stage: str,
        status: str,
        details: Optional[Mapping[str, Any]] = None,
        duration_ms: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not self.keep_run_flow(str(stage), str(status), details):
            return {}
        with self._lock:
            self._run_flow_seq += 1
            timestamp = _now_text()
            item = {
                "seq": self._run_flow_seq,
                "sequence": self._run_flow_seq,
                "time": timestamp,
                "timestamp": timestamp,
                "run_id": str(run_id),
                "stage": str(stage),
                "status": str(status),
                "duration_ms": (
                    None if duration_ms is None else round(float(duration_ms), 3)
                ),
                "details": _redact(dict(details or {})),
            }
            item["details"] = _compact_log(item["details"])
            item["record_type"] = "FLOW"
            # 相同異常只合併記錄；交易生命週期不套用此去重。
            is_trade = str(stage).startswith("ORDER_") or stage == "EXECUTION_RESULT"
            if is_trade:
                self._trade_revision += 1
            if not is_trade and stage != "POSITION_LIVENESS":
                signature = _stable_hash([stage, status, item["details"]])
                previous = self._log_repeat.get(signature, (0, 0))
                now_mono = time.monotonic()
                if now_mono - previous[0] < 60:
                    self._log_repeat[signature] = (previous[0], previous[1] + 1)
                    return item
                if previous[1]:
                    item["suppressed_count"] = previous[1]
                self._log_repeat[signature] = (now_mono, 0)
                if len(self._log_repeat) > 256:
                    self._log_repeat.pop(next(iter(self._log_repeat)))
            self._append_jsonl(self.run_flow_log_path, item)
            self._bounded_append(self._recent_run_flow, copy.deepcopy(item))
            if not is_trade or status in {"ERROR", "REJECTED", "STATUS_UNKNOWN"}:
                self._bounded_append(self._recent_errors, copy.deepcopy(item))
            return copy.deepcopy(item)

    def record_execution(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        if not self.keep_execution(payload):
            return {}
        with self._lock:
            self._execution_seq += 1
            timestamp = _now_text()
            item = {
                "seq": self._execution_seq,
                "sequence": self._execution_seq,
                "time": timestamp,
                "timestamp": timestamp,
                **_redact(dict(payload)),
            }
            compact = _compact_log(item)
            execution = item.get("execution") or item
            decision = item.get("decision") or {}
            for name in ("symbol", "market", "action", "status", "order_id", "order_no", "exit_stage"):
                if execution.get(name) is not None:
                    compact[name] = execution[name]
                elif decision.get(name) is not None:
                    compact[name] = decision[name]
            keep = ("seq", "sequence", "time", "timestamp", "trigger", "symbol", "market", "action", "status", "order_id", "order_no", "exit_stage")
            compact = {k: compact[k] for k in keep if k in compact}
            compact["decision"] = {k: decision[k] for k in ("decision", "symbol", "market", "action", "order_price", "position_size", "cts_quantity", "rejection_codes") if k in decision}
            execution_keys = ("ok", "status", "symbol", "action", "order_id", "order_no", "exit_stage", "exit_fraction", "exited_shares", "exit_fill_price", "rejection_codes", "error", "reason", "execution_attempt", "broker_submit_attempt", "broker_accepted", "broker_rejected", "broker_unknown", "order_intent_id", "submission_state", "primary_rejection_code")
            compact["execution"] = {k: _compact_log(execution[k]) for k in execution_keys if k in execution}
            for key in ("identity_recovery", "query_cross_check", "events", "revalidation", "price_sources", "position_discrepancies"):
                if key in execution:
                    compact["execution"][key] = _compact_log(execution[key])
            ownership = execution.get("ownership") or {}
            compact["execution"]["ownership"] = {k: ownership[k] for k in
                ("registry_key", "order_intent_id", "order_state", "filled_shares", "signal_fingerprint", "generation") if k in ownership}
            if execution.get("order_request"):
                compact["execution"]["order_request"] = _redact(execution["order_request"])
            if execution.get("traceback"):
                compact["execution"]["traceback"] = execution["traceback"]
            fill = execution.get("fill_summary") or {}
            if isinstance(fill, Mapping) and fill:
                compact["execution"]["fill_summary"] = {k: fill[k] for k in ("filled_shares", "filled_cts_quantity", "average_fill_price", "filled", "identity_status") if k in fill}
            validation = execution.get("revalidation") or {}
            if isinstance(validation, Mapping) and validation:
                compact["execution"]["revalidation"] = {k: validation[k] for k in ("event_age_seconds", "event_fresh", "order_price", "rejection_codes", "error") if k in validation}
            if item.get("events"):
                compact["events"] = _compact_log(item["events"])
            self._trade_revision += 1
            compact["record_type"] = "EXECUTION"
            self._append_jsonl(self.execution_log_path, compact)
            if self.execution_is_error(item):
                self._bounded_append(self._recent_errors, copy.deepcopy(compact))
            self._bounded_append(self._recent_execution, copy.deepcopy(compact))
            return copy.deepcopy(item)

    @staticmethod
    def _fill_key(row: Mapping[str, Any]) -> str:
        match = row.get("match") or {}
        identity = [
            row.get("trade_date"),
            match.get("account_id"),
            row.get("symbol"),
            row.get("action"),
            row.get("order_id") or row.get("order_no"),
            match.get("match_id"),
        ]
        if match.get("match_id"):
            return _stable_hash(identity)
        return str(row.get("fingerprint") or _stable_hash(row))

    @staticmethod
    def _merge_fill(
        prior: Optional[Mapping[str, Any]], row: Mapping[str, Any]
    ) -> Dict[str, Any]:
        result = copy.deepcopy(dict(row))
        if prior:
            match = result.setdefault("match", {})
            for field in ("fee", "tax"):
                old = float((prior.get("match") or {}).get(field) or 0)
                if old > 0 and float(match.get(field) or 0) <= 0:
                    match[field] = old
            result["market"] = result.get("market") or prior.get("market")
        return result

    def restore_daily_report(self) -> None:
        """只由背景對帳呼叫；Snapshot 不讀檔，也不重算整日帳本。"""
        with self._lock:
            day = _trade_date_text()
            if self._ledger_day == day:
                return
            self._ledger_day = day
            self._daily_ledger.clear()
            self._daily_fingerprints.clear()
            self._recent_daily_trade.clear()
            legacy = self.log_dir / f"{DAILY_TRADE_REPORT_PREFIX}_{day}.jsonl"
            parts = sorted(self.log_dir.glob(self.daily_trade_report_path.stem + ".part*.jsonl"))
            for path in (legacy, *parts, self.daily_trade_report_path):
                if path.is_file():
                    with path.open(encoding="utf-8-sig") as stream:
                        for line in stream:
                            try:
                                row = json.loads(line)
                                if (
                                    isinstance(row, dict)
                                    and str(row.get("trade_date")) == day
                                    and (path == legacy or row.get("record_type") == "FILL")
                                ):
                                    key = self._fill_key(row)
                                    self._daily_ledger[key] = self._merge_fill(
                                        self._daily_ledger.get(key), row
                                    )
                            except (ValueError, TypeError):
                                self._last_write_error = (
                                    "當日成交檔含損壞紀錄，績效可能不完整"
                                )
            self._refresh_daily_metrics()

    def _refresh_daily_metrics(self) -> None:
        rows = sorted(
            self._daily_ledger.values(), key=lambda r: str(r.get("time") or "")
        )
        self._recent_daily_trade = rows[-_RUNTIME_RECENT_LOG_LIMIT:]
        self._metrics_cache = self._daily_metrics(rows)

    def record_daily_trade(
        self, payload: Mapping[str, Any], *, fingerprint: Optional[str] = None
    ) -> bool:
        item = _redact(dict(payload))
        with self._lock:
            self.restore_daily_report()
            if str(item.get("trade_date") or _trade_date_text()) != self._ledger_day:
                return False
            item["fingerprint"] = str(fingerprint or _stable_hash(item))
            key = self._fill_key(item)
            prior = self._daily_ledger.get(key)
            item = self._merge_fill(prior, item)
            # 回補或重啟不新增成交；費用、價格、股數更正則更新同一成交。
            if prior and all(
                prior.get(k) == item.get(k) for k in ("price", "quantity", "match")
            ):
                return False
            item["timestamp"] = item.get("timestamp") or _now_text()
            item["record_type"] = "FILL"
            if not self._append_jsonl(self.daily_trade_report_path, item):
                return False
            self._daily_ledger[key] = copy.deepcopy(item)
            self._execution_seq += 1
            self._trade_revision += 1
            self._bounded_append(self._recent_execution, {
                "seq": self._execution_seq, "time": item.get("time") or item["timestamp"],
                "record_type": "FILL", "symbol": item.get("symbol"), "market": item.get("market"),
                "action": item.get("action"), "status": "FILL_UPDATED" if prior else "FILLED",
                "execution": {"status": "FILLED", "order_id": item.get("order_id") or item.get("order_no"),
                    "fill_summary": {"filled_shares": item.get("quantity"), "average_fill_price": item.get("price")}},
            })
            self._refresh_daily_metrics()
            return True

    @staticmethod
    def _daily_metrics(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        unique: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            if isinstance(r, Mapping):
                key = RuntimeJournalComponent._fill_key(r)
                unique[key] = RuntimeJournalComponent._merge_fill(unique.get(key), r)
        ordered = sorted(
            unique.values(),
            key=lambda r: (str(r.get("trade_date") or ""), str(r.get("time") or "")),
        )
        orders: Dict[tuple, Dict[str, Any]] = {}

        def order_key(r: Mapping[str, Any]) -> tuple:
            m = r.get("match") or {}
            return (
                r.get("trade_date"),
                m.get("account_id"),
                r.get("market") or (r.get("ownership") or {}).get("market"),
                r.get("symbol"),
                r.get("action"),
                r.get("order_id")
                or r.get("order_no")
                or RuntimeJournalComponent._fill_key(r),
            )

        for r in ordered:
            m = r.get("match") or {}
            o = orders.setdefault(
                order_key(r), {"value": 0.0, "fee": 0.0, "reported": True}
            )
            o["value"] += float(r.get("price") or 0) * int(r.get("quantity") or 0)
            o["fee"] += float(m.get("fee") or 0)
            o["reported"] &= float(m.get("fee") or 0) > 0
        # 費用回報為 0 表示尚未確認。牌告未折扣估算只用於顯示，不改交易／風控參數。
        for o in orders.values():
            if not o["reported"]:
                o["fee"] = max(20.0, o["value"] * 0.001425)
        lots: Dict[tuple, list] = {}
        exits: Dict[tuple, Dict[str, Any]] = {}
        gross = net = 0.0
        unknown = 0
        estimated = False
        buy_fills = sell_fills = 0
        for r in ordered:
            m = r.get("match") or {}
            key = order_key(r)
            book = lots.setdefault(key[:4], [])
            q, price = int(r.get("quantity") or 0), float(r.get("price") or 0)
            if q <= 0 or price <= 0:
                continue
            o = orders[key]
            fee = o["fee"] * (q * price / o["value"]) if o["value"] else 0.0
            if r.get("action") == "BUY":
                buy_fills += 1
                book.append([q, price, fee / q, not o["reported"]])
                continue
            if r.get("action") != "SELL":
                continue
            sell_fills += 1
            remaining, cost, buy_fee, est = q, 0.0, 0.0, not o["reported"]
            while remaining and book:
                lot = book[0]
                take = min(remaining, lot[0])
                cost += take * lot[1]
                buy_fee += take * lot[2]
                est |= lot[3]
                remaining -= take
                lot[0] -= take
                if lot[0] == 0:
                    book.pop(0)
            tax = float(m.get("tax") or 0)
            if tax <= 0:
                # 只估算已配對整張現股普通股票；ETF／信用／未知商品不得套同一稅率。
                eligible = (
                    not remaining
                    and len(str(r.get("symbol") or "")) == 4
                    and str(m.get("trade_type", "")) == "0"
                    and str(m.get("order_type", "")) == "0"
                    and "20170428" <= str(r.get("trade_date") or "") <= "20271231"
                    and q % 1000 == 0
                )
                if eligible:
                    tax = q * price * 0.0015
                    est = True
                else:
                    remaining = max(1, remaining)
            e = exits.setdefault(
                key,
                {
                    "gross": 0.0,
                    "net": 0.0,
                    "cost": 0.0,
                    "estimated": False,
                    "unknown": False,
                },
            )
            if remaining:
                unknown += 1
                e["unknown"] = True
                continue
            g = q * price - cost
            n = g - buy_fee - fee - tax
            gross += g
            net += n
            estimated |= est
            e["gross"] += g
            e["net"] += n
            e["cost"] += cost + buy_fee
            e["estimated"] |= est
        last = ordered[-1] if ordered else {}
        last_exit = exits.get(order_key(last)) if last else None
        last_valid = last_exit is not None and not last_exit["unknown"]
        return {
            "buy_count": sum(k[4] == "BUY" for k in orders),
            "sell_count": sum(k[4] == "SELL" for k in orders),
            "buy_fill_count": buy_fills,
            "sell_fill_count": sell_fills,
            "unknown_sell_rows": unknown,
            "last_trade": last,
            "daily_gross_pnl": round(gross, 6),
            "daily_realized_pnl": round(net, 6),
            "fees_estimated": estimated,
            "last_trade_estimated": bool(last_valid and last_exit["estimated"]),
            "last_trade_pnl": round(last_exit["net"], 6) if last_valid else None,
            "last_trade_return_pct": (
                last_exit["net"] / last_exit["cost"] * 100
                if last_valid and last_exit["cost"]
                else None
            ),
            "fee_basis": "費用為0時：未折扣手續費0.1425%、每單最低20元；符合整張現股當沖條件之股票稅0.15%；均為估算，依券商結算為準",
        }

    def managed_daily_metrics(self) -> Dict[str, Any]:
        """只以已確認由本系統管理的成交計算風控用淨損益。"""
        with self._lock:
            rows = [
                row
                for row in self._daily_ledger.values()
                if (row.get("ownership") or {}).get("owner") == "MOMENTUM_SCALPING"
            ]
            return self._daily_metrics(rows)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            daily_rows = copy.deepcopy(
                self._recent_daily_trade[-_RUNTIME_RECENT_LOG_LIMIT:]
            )
            return {
                "execution_log_path": str(self.execution_log_path),
                "daily_trade_report_path": str(self.daily_trade_report_path),
                "run_flow_log_path": str(self.run_flow_log_path),
                "recent_error_log_path": str(self.recent_error_log_path),
                "run_flow_seq": self._run_flow_seq,
                "execution_seq": self._execution_seq,
                "trade_revision": self._trade_revision,
                "recent_run_flow": copy.deepcopy(self._recent_run_flow[-50:]),
                "recent_errors": copy.deepcopy(self._recent_errors[-50:]),
                "recent_execution": copy.deepcopy(self._recent_execution[-50:]),
                "run_flow_log": copy.deepcopy(self._recent_run_flow[-50:]),
                "execution_log": copy.deepcopy(self._recent_execution[-50:]),
                "daily_trade_report": daily_rows,
                "daily_trade_metrics": copy.deepcopy(self._metrics_cache),
            }


# =============================================================================
# 持倉／CTS 對帳 Component
# =============================================================================


class RuntimePositionComponent:
    """保存執行期持倉、委託、人工接管與帳戶狀態。"""

    def __init__(self, settings: Any) -> None:
        self._generation = 0
        self._applied_execution_fills = {}
        self._applied_execution_costs = {}
        self._position_stale = True
        self._position_snapshot_at = None
        self._risk_state = {}
        self._lock = threading.RLock()
        self._positions: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._open_orders: List[Dict[str, Any]] = []
        self._manual_takeover: set[Tuple[str, str]] = set()
        self._processed_signals: set[str] = set()
        self._settings = settings
        self._runtime_daily_trade_count = int(
            _setting(settings, "daily_trade_count", 0) or 0
        )
        self._runtime_daily_pnl = float(_setting(settings, "daily_pnl", 0.0) or 0.0)

    def update_settings(self, settings: Any) -> None:
        with self._lock:
            self._settings = settings
            configured_count = int(_setting(settings, "daily_trade_count", 0) or 0)
            configured_pnl = float(_setting(settings, "daily_pnl", 0.0) or 0.0)
            self._runtime_daily_trade_count = max(
                self._runtime_daily_trade_count, configured_count
            )
            if self._runtime_daily_pnl == 0.0:
                self._runtime_daily_pnl = configured_pnl

    def keys(self) -> List[Tuple[str, str]]:
        with self._lock:
            return list(self._positions)

    def get(self, symbol: str, market: str) -> Optional[Dict[str, Any]]:
        key = _symbol_key(symbol, market)
        with self._lock:
            row = self._positions.get(key)
            return copy.deepcopy(row) if row is not None else None

    def is_manual_takeover(self, symbol: str, market: str) -> bool:
        with self._lock:
            return _symbol_key(symbol, market) in self._manual_takeover

    def mark_manual_takeover(self, symbol: str, market: str) -> Dict[str, Any]:
        key = _symbol_key(symbol, market)
        if not key[0]:
            raise ValueError("人工接管需要 symbol")
        with self._lock:
            self._manual_takeover.add(key)
            row = self._positions.get(key)
            if row is not None:
                row["managed_by"] = "MANUAL"
                row["management_source"] = "人工接管"
            return {
                "ok": True,
                "symbol": key[0],
                "market": key[1],
                "position_found": row is not None,
            }

    def set_open_orders(self, rows: Sequence[Mapping[str, Any]]) -> None:
        with self._lock:
            self._open_orders = [dict(row) for row in rows if isinstance(row, Mapping)]

    def mark_position_stale(self, error=""):
        with self._lock:
            self._position_stale = True
            for row in self._positions.values():
                row["stale"] = True
                row["position_query_error"] = str(error)

    def replace_cts_positions(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        market_resolver: Optional[Any] = None,
    ) -> None:
        incoming: Dict[Tuple[str, str], Dict[str, Any]] = {}
        with self._lock:
            existing = copy.deepcopy(self._positions)
            takeover = set(self._manual_takeover)

        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            symbol = str(row.get("symbol") or "").strip()
            if not symbol:
                continue
            market = _normalize_market(row.get("market"))
            if not market and callable(market_resolver):
                try:
                    market = _normalize_market(market_resolver(symbol))
                except Exception:
                    market = ""
            key = _symbol_key(symbol, market)
            previous = existing.get(key)
            if previous is None:
                same_symbol = [
                    value for old_key, value in existing.items() if old_key[0] == symbol
                ]
                if len(same_symbol) == 1:
                    previous = same_symbol[0]
            merged = dict(previous or {})
            merged.update(row)
            merged["symbol"] = symbol
            merged["market"] = market
            merged["stale"] = False
            merged["position_snapshot_at"] = _now_text()
            merged.pop("position_query_error", None)
            # 當成交查詢尚未成功時也不得保留超過 CTS 現存股數的可賣量。
            for field in ("managed_quantity", "managed_sellable_quantity", "remaining_quantity"):
                if field in merged:
                    merged[field] = min(int(merged.get(field) or 0), int(merged.get("quantity") or 0))
            if previous is None:
                merged.setdefault("managed_by", "")
                merged.setdefault("management_source", "CTS 對帳")
                merged.setdefault("managed_quantity", 0)
                merged.setdefault("entry_price", None)
            if key in takeover:
                merged["managed_by"] = "MANUAL"
                merged["management_source"] = "人工接管"
            incoming[key] = merged

        with self._lock:
            if self._positions != incoming:
                self._generation += 1
            self._positions = incoming
            self._position_stale = False
            self._position_snapshot_at = _now_text()
            self._manual_takeover.intersection_update(set(incoming))

    def apply_execution(self, execution: Mapping[str, Any]) -> None:
        action = str(execution.get("action") or execution.get("side") or "").upper()
        position = execution.get("position")
        with self._lock:
            ownership = execution.get("ownership") or {}
            identity = str(ownership.get("registry_key") or ownership.get("order_intent_id") or "")
            cumulative = int((execution.get("fill_summary") or {}).get("filled_shares")
                             or execution.get("exited_shares") or 0)
            if identity and cumulative <= self._applied_execution_fills.get(identity, 0):
                return
            old = 0
            if identity:
                old = self._applied_execution_fills.get(identity, 0)
                self._applied_execution_fills[identity] = cumulative
                if action == "SELL":
                    execution = dict(execution, exited_shares=max(0, cumulative-old))
            if cumulative:
                self._generation += 1
            if isinstance(position, Mapping):
                row = dict(position)
                key = _symbol_key(row.get("symbol"), row.get("market"))
                if key[0]:
                    previous = self._positions.get(key) or {}
                    if action == "BUY":
                        avg = float((execution.get("fill_summary") or {}).get("average_fill_price") or 0)
                        total_cost = cumulative * avg
                        old_cost = self._applied_execution_costs.get(identity, 0.0) if identity else 0.0
                        added = cumulative - old
                        managed = int(previous.get("managed_quantity") or previous.get("quantity") or 0)
                        previous_avg = previous.get("entry_price")
                        if previous and added > 0:
                            if managed and not previous_avg:
                                # 無實際成本證據時保留原快照，交由完整 CTS 成交重建。
                                self.mark_position_stale("BUY_COST_RECONCILIATION_REQUIRED")
                                return
                            new_quantity = managed + added
                            actual_avg = (managed * float(previous_avg or 0) + total_cost - old_cost) / new_quantity
                            merged = dict(previous)
                            for name in ("quantity", "managed_quantity", "managed_sellable_quantity", "remaining_quantity"):
                                merged[name] = int(previous.get(name) or 0) + added
                            merged["initial_quantity"] = int(previous.get("initial_quantity") or managed) + added
                            merged["entry_price"] = actual_avg
                            merged["price_basis"] = "CTS_ACTUAL_AVERAGE_FILL_PRICE"
                            # 結構／時間及既有緊急停損不因新增成本重設。
                            merged["open_risk"] = new_quantity * max(0, actual_avg - float(merged.get("effective_stop_price") or 0))
                            row = merged
                        if identity:
                            self._applied_execution_costs[identity] = total_cost
                    self._positions[key] = row
                    if action == "BUY" and not (identity and old):
                        self._runtime_daily_trade_count += 1
                return

            if action != "SELL":
                return
            symbol = str(execution.get("symbol") or "").strip()
            market = _normalize_market(execution.get("market"))
            if not symbol:
                return
            key = _symbol_key(symbol, market)
            row = self._positions.get(key)
            if row is None:
                return
            try:
                exited = int(execution.get("exited_shares") or 0)
            except (TypeError, ValueError):
                exited = 0
            if exited <= 0:
                return
            current = int(row.get("quantity") or 0)
            remaining = max(0, current - exited)
            if remaining <= 0:
                self._positions.pop(key, None)
                self._manual_takeover.discard(key)
            else:
                row["initial_quantity"] = row.get("initial_quantity") or current
                row["remaining_quantity"] = remaining
                if (
                    execution.get("exit_stage")
                    or (execution.get("ownership") or {}).get("exit_stage")
                ) == "MOMENTUM_DECAY_PARTIAL":
                    row["partial_exit_done"] = True
                    row["partial_exit_stage"] = "MOMENTUM_DECAY_PARTIAL"
                row["open_risk"] = remaining * max(
                    0,
                    float(row.get("entry_price") or 0)
                    - float(row.get("effective_stop_price") or 0),
                )
                row["quantity"] = remaining
                for field in ("managed_quantity", "managed_sellable_quantity"):
                    if field in row:
                        row[field] = max(0, int(row[field] or 0) - exited)

    def mark_signal_processed(self, fingerprint: str) -> None:
        clean = str(fingerprint or "").strip()
        if clean:
            with self._lock:
                self._processed_signals.add(clean)
                if len(self._processed_signals) > 5000:
                    self._processed_signals = set(
                        sorted(self._processed_signals)[-2500:]
                    )

    def signal_processed(self, fingerprint: str) -> bool:
        clean = str(fingerprint or "").strip()
        with self._lock:
            return bool(clean and clean in self._processed_signals)

    def account_state(self) -> Dict[str, Any]:
        with self._lock:
            positions = [copy.deepcopy(row) for row in self._positions.values()]
            return {
                **copy.deepcopy(self._risk_state),
                "available_cash": float(
                    _setting(self._settings, "available_cash", 0.0) or 0.0
                ),
                "equity": float(_setting(self._settings, "equity", 0.0) or 0.0),
                "daily_trade_count": self._runtime_daily_trade_count,
                "daily_pnl": self._runtime_daily_pnl,
                "positions": positions,
                "open_orders": copy.deepcopy(self._open_orders),
                "processed_signal_fingerprints": sorted(self._processed_signals),
            }

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "positions": copy.deepcopy(self._positions),
                "stale": self._position_stale,
                "snapshot_at": self._position_snapshot_at,
                "open_orders": copy.deepcopy(self._open_orders),
                "manual_takeover": [list(key) for key in sorted(self._manual_takeover)],
                "account_state": self.account_state(),
            }


# =============================================================================
# Market／Scanner Component
# =============================================================================


class RuntimeMarketScannerComponent:
    """負責較慢週期的 Market Snapshot、Active Universe 與 Scanner Atomic Publish。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._market_context: Dict[str, Any] = {}
        self._candidates_context: Dict[str, Any] = {"candidates": []}
        self._active_universe: List[Dict[str, Any]] = []
        self._market_source_status: Dict[str, Any] = {}
        self._generation = 0
        self._last_refresh_at = ""
        self._last_frozen_inputs_fingerprint = ""
        self._last_scanner_output_fingerprint = ""

    @property
    def market_context(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._market_context)

    @property
    def candidates_context(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._candidates_context)

    def candidate(self, symbol: str, market: str) -> Optional[Dict[str, Any]]:
        wanted = _symbol_key(symbol, market)
        with self._lock:
            rows = _candidate_rows(self._candidates_context)
        for row in rows:
            if _symbol_key(row.get("symbol"), row.get("market")) == wanted:
                return row
        return None

    def market_for_symbol(self, symbol: str) -> str:
        clean = str(symbol or "").strip()
        with self._lock:
            for row in _candidate_rows(self._candidates_context):
                if str(row.get("symbol") or "").strip() == clean:
                    return _normalize_market(row.get("market"))
            for row in self._active_universe:
                if str(row.get("symbol") or "").strip() == clean:
                    return _normalize_market(row.get("market"))
        return ""

    def refresh(
        self,
        runtime: "TradingSystemRuntime",
        *,
        force: bool = False,
        reason: str = "INTERVAL",
    ) -> Dict[str, Any]:
        del force
        market_module = runtime.modules["market"]
        scanner_module = runtime.modules["scanner"]
        market_params = _parameter(runtime.registry, "market")
        scanner_params = _parameter(runtime.registry, "scanner")
        gateway = runtime._ensure_gateway_ready()
        mode = str(
            _setting(runtime.settings, "control_mode", "MANUAL") or "MANUAL"
        ).upper()
        api_mode = str(_setting(runtime.settings, "api_mode", "LIVE") or "LIVE").upper()
        replay_dir = str(
            _setting(runtime.settings, "replay_dir", "api_replay") or "api_replay"
        )

        started = time.perf_counter()
        refresh_fn = getattr(market_module, "refresh_market_snapshot", None)
        if not callable(refresh_fn):
            raise RuntimeError("Market 缺少 refresh_market_snapshot() 正式介面")

        market_client = getattr(gateway, "market", None)
        begin_round = getattr(market_client, "begin_round", None)
        if callable(begin_round):
            begin_round()
            if hasattr(market_client, "_round_deadline"):
                market_client._round_deadline = time.monotonic() + max(0.1, scanner_params.scanner_round_time_limit_seconds * 0.8)
        limit = int(getattr(scanner_params, "active_universe_n", 500) or 500)
        active_universe_payload = gateway.get_active_universe(limit=limit)
        active_universe = [
            copy.deepcopy(dict(row))
            for row in active_universe_payload
            if isinstance(row, Mapping)
        ]
        degraded_universe_rows = [
            row for row in active_universe if bool(row.get("source_degraded"))
        ]
        active_universe_degraded = bool(degraded_universe_rows)
        active_universe_errors = sorted(
            {
                str(row.get("source_error") or "").strip()
                for row in degraded_universe_rows
                if str(row.get("source_error") or "").strip()
            }
        )
        runtime._apply_subscriptions(p2_rows=active_universe)

        symbols = list(
            dict.fromkeys(
                [symbol for symbol, _market in runtime.positions.keys()]
                + [
                    str(row.get("symbol") or "")
                    for row in _candidate_rows(self.candidates_context)
                ]
                + [str(row.get("symbol") or "") for row in active_universe]
            )
        )
        symbols = [symbol for symbol in symbols if symbol]
        day_trade_map: Mapping[str, bool] = {}
        batch = {}
        if symbols:
            try:
                batch = gateway.refresh_day_trade(symbols)
            except Exception as exc:
                runtime._record_run_flow(
                    run_id=runtime._new_run_id("DAYTRADE"),
                    stage="DAY_TRADE_REFRESH",
                    status="DEGRADED",
                    details={"error": f"{type(exc).__name__}: {exc}"},
                )
            try:
                day_trade_payload = gateway.get_day_trade_map(symbols)
                if isinstance(day_trade_payload, Mapping):
                    day_trade_map = copy.deepcopy(dict(day_trade_payload))
            except Exception as exc:
                runtime._record_run_flow(
                    run_id=runtime._new_run_id("DAYTRADE"),
                    stage="DAY_TRADE_READ",
                    status="DEGRADED",
                    details={"error": f"{type(exc).__name__}: {exc}"},
                )
                day_trade_map = {}


        market_error = ""
        try:
            context_payload = refresh_fn(
                mode=mode,
                manual_market_state=_setting(
                    runtime.settings, "manual_market_state", None
                ),
                manual_trading_mode=_setting(
                    runtime.settings, "manual_trading_mode", None
                ),
                gateway=gateway,
                api_mode=api_mode,
                replay_dir=replay_dir,
                parameters=market_params,
            )
        except Exception as exc:
            market_error = f"{type(exc).__name__}: {exc}"
            runtime._record_market_failure(
                run_id=runtime._new_run_id("MARKET"),
                details=runtime._market_failure_details(exc),
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            context_payload = dict(self.market_context or {})
            context_payload.update(
                allow_long=False,
                realtime_buy_data_ready=False,
                market_state="UNAVAILABLE",
                trading_mode="OBSERVE",
                market_error=market_error,
            )
        if not isinstance(context_payload, Mapping):
            raise RuntimeError("Market refresh_market_snapshot() 回傳格式錯誤")
        context = copy.deepcopy(dict(context_payload))
        if active_universe_degraded:
            context.update(
                allow_long=False,
                realtime_buy_data_ready=False,
                active_universe_degraded=True,
                active_universe_error=" | ".join(active_universe_errors),
            )
        market_snapshot_meta = (
            copy.deepcopy(dict(context.get("market_snapshot") or {}))
            if isinstance(context.get("market_snapshot"), Mapping)
            else {}
        )

        get_market_state_snapshot = getattr(gateway, "get_market_state_snapshot", None)
        if not callable(get_market_state_snapshot):
            raise RuntimeError("APIGateway 缺少 get_market_state_snapshot() 正式介面")

        snapshot_reference = _now()
        snapshot_reference_text = snapshot_reference.isoformat(timespec="microseconds")
        frozen_payload = get_market_state_snapshot(as_of=snapshot_reference)
        if not isinstance(frozen_payload, Mapping):
            raise RuntimeError("APIGateway.get_market_state_snapshot() 回傳格式錯誤")
        frozen_market_state = copy.deepcopy(dict(frozen_payload))
        frozen_rows = frozen_market_state.get("rows")
        if not isinstance(frozen_rows, Sequence) or isinstance(
            frozen_rows, (str, bytes)
        ):
            raise RuntimeError("MarketState Snapshot 缺少 rows")
        frozen_rows = [
            copy.deepcopy(dict(row)) for row in frozen_rows if isinstance(row, Mapping)
        ]
        frozen_market_state["rows"] = frozen_rows
        frozen_market_state["snapshot_at"] = snapshot_reference_text
        frozen_market_state["as_of"] = snapshot_reference_text
        runtime._merge_quotes(frozen_rows)
        attach = getattr(market_module, "attach_sector_contract", None)
        if callable(attach):
            universe_rows, _ = scanner_module._prepare_active_universe(active_universe, scanner_params.active_universe_n)
            sector_rows = market_module.merge_sector_source_rows(universe_rows, frozen_market_state["rows"])
            get_master = getattr(gateway, "get_security_master", None)
            context = attach(context, sector_rows, get_master() if callable(get_master) else [],
                             parameters=market_params, source=scanner_params.sector_change_source,
                             reference_at=snapshot_reference_text)
            runtime._record_run_flow(run_id=runtime._new_run_id("SECTOR"),
                stage="SECTOR_MAPPING_COVERAGE", status="OK" if not context["sector_mapping_coverage"]["missing_symbols"] else "DEGRADED",
                details=context["sector_mapping_coverage"])


        # 市場與類股資料先發布，不等待 Scanner 計算成功。
        with self._lock:
            self._market_context = copy.deepcopy(context)
            self._active_universe = copy.deepcopy(active_universe)
        session_phase = str(context.get("session_phase") or "REGULAR")
        frozen_inputs = {
            "active_universe_snapshot": copy.deepcopy(active_universe),
            "market_state_snapshot": copy.deepcopy(frozen_market_state),
            "market_context": copy.deepcopy(context),
            "sector_map": copy.deepcopy(context.get("sector_map") or {}),
            "day_trade_map": copy.deepcopy(dict(day_trade_map)),
            "scanner_parameter_fingerprint": _parameter_fingerprint(
                runtime.registry, "scanner"
            ),
            "session_phase": session_phase,
        }
        frozen_inputs_fingerprint = _stable_hash(frozen_inputs)

        compute_token = runtime.input_token()
        compute_token["purpose"] = "SCANNER"
        compute_token["ttl_seconds"] = float(scanner_params.scanner_round_time_limit_seconds)
        runtime.resources.compute_token = copy.deepcopy(compute_token)
        scanner_result = scanner_module.get_candidates(
            active_universe_snapshot=copy.deepcopy(
                frozen_inputs["active_universe_snapshot"]
            ),
            market_state_snapshot=copy.deepcopy(frozen_inputs["market_state_snapshot"]),
            market_context=copy.deepcopy(frozen_inputs["market_context"]),
            day_trade_map=copy.deepcopy(frozen_inputs["day_trade_map"]),
            sector_map=copy.deepcopy(frozen_inputs["sector_map"]),
            parameters=scanner_params,
            resource_manager=runtime.resources,
            compute_timeout_seconds=max(0.01, scanner_params.scanner_round_time_limit_seconds-(time.perf_counter()-started)),
            session_phase=session_phase,
        )
        if not isinstance(scanner_result, Mapping):
            raise RuntimeError("Scanner get_candidates() 回傳格式錯誤")
        candidates_context = copy.deepcopy(dict(scanner_result))
        candidates_context.setdefault("candidates", [])
        scanner_output_fingerprint = str(
            candidates_context.get("fingerprint") or _stable_hash(candidates_context)
        )

        with self._lock:
            if (
                self._last_frozen_inputs_fingerprint
                and self._last_frozen_inputs_fingerprint == frozen_inputs_fingerprint
                and self._last_scanner_output_fingerprint
                and self._last_scanner_output_fingerprint != scanner_output_fingerprint
            ):
                raise RuntimeError("相同 Frozen Inputs 與相同參數產生不同 Scanner 輸出")

        integration = (
            copy.deepcopy(dict(candidates_context.get("integration") or {}))
            if isinstance(candidates_context.get("integration"), Mapping)
            else {}
        )
        integration.update(
            {
                "runtime_frozen_inputs_fingerprint": frozen_inputs_fingerprint,
                "market_state_generation": int(
                    frozen_market_state.get("generation") or 0
                ),
                "market_state_quote_sequence": int(
                    frozen_market_state.get("quote_sequence") or 0
                ),
                "market_state_snapshot_at": snapshot_reference_text,
                "market_state_source_at": str(
                    frozen_market_state.get("source_at") or ""
                ),
                "late_market_events_apply_next_round_only": True,
            }
        )
        candidates_context["integration"] = integration

        runtime._apply_subscriptions(
            p1_rows=_candidate_rows(candidates_context),
            p2_rows=active_universe,
        )

        source_values = sorted(
            {
                str(row.get("source") or "").strip()
                for row in active_universe
                if str(row.get("source") or "").strip()
            }
        )
        market_source_status = {
            "state": "DEGRADED" if active_universe_degraded else ("READY" if active_universe else "MISSING"),
            "active_universe_state": "LAST_GOOD_CACHE" if active_universe_degraded else ("READY" if active_universe else "MISSING"),
            "active_universe_source": ",".join(source_values)
            or "APIGateway.get_active_universe",
            "active_universe_rows": len(active_universe),
            "active_universe_degraded": active_universe_degraded,
            "active_universe_errors": active_universe_errors,
            "market_snapshot_source_at": market_snapshot_meta.get("source_at"),
            "market_snapshot_calculated_at": market_snapshot_meta.get("calculated_at"),
            "market_snapshot_generation": market_snapshot_meta.get("generation"),
            "market_snapshot_fingerprint": market_snapshot_meta.get("fingerprint"),
            "strategy_market_permissions": copy.deepcopy(context.get("strategy_market_permissions") or {}),
            "market_state_generation": int(frozen_market_state.get("generation") or 0),
            "market_state_quote_sequence": int(
                frozen_market_state.get("quote_sequence") or 0
            ),
            "market_state_source_at": str(frozen_market_state.get("source_at") or ""),
            "market_state_snapshot_at": snapshot_reference_text,
            "market_state_row_count": len(frozen_rows),
            "frozen_inputs_fingerprint": frozen_inputs_fingerprint,
            "scanner_output_fingerprint": scanner_output_fingerprint,
            "late_market_events_apply_next_round_only": True,
            "reason": str(reason),
        }

        round_limit = float(scanner_params.scanner_round_time_limit_seconds)
        if time.perf_counter() - started > round_limit:
            raise TimeoutError("市場掃描回合逾時；本輪結果不發布")
        if not runtime.result_valid(compute_token):
            raise RuntimeError("Scanner 輸入身分、參數、交易日或 TTL 已失效")
        if _stable_hash(frozen_inputs) != frozen_inputs_fingerprint:
            raise RuntimeError("Frozen Scanner Input 被修改")
        candidates_context["runtime_token"] = compute_token
        candidates_context["candidate_snapshot"] = {
            "snapshot_id": scanner_output_fingerprint,
            "input_snapshot_id": frozen_inputs_fingerprint,
            "market_permission_snapshot_id": context.get("market_permission_snapshot_id"),
            "captured_at": snapshot_reference_text,
            "created_monotonic": compute_token["created_monotonic"],
            "session_id": compute_token["session_id"], "trade_date": compute_token["trade_date"],
            "ttl_seconds": float(getattr(scanner_params, "realtime_freshness_max_age_seconds", 60.0)),
        }
        latest_context = runtime.current_strategy_market_context()
        if latest_context.get("market_buy_data_ready") is False:
            for row in candidates_context["candidates"]:
                row["trade_ready"] = False
                row["trade_block_reasons"] = list(dict.fromkeys(list(row.get("trade_block_reasons") or []) + ["MARKET_GUARD_NOT_READY"]))
        if _parameter_fingerprint(runtime.registry, "scanner") != frozen_inputs["scanner_parameter_fingerprint"]:
            raise RuntimeError("掃描期間參數已變更，丟棄本輪結果")
        with self._lock:
            self._generation += 1
            self._market_context = context
            self._candidates_context = candidates_context
            self._active_universe = active_universe
            self._market_source_status = market_source_status
            self._last_refresh_at = _now_text()
            self._last_frozen_inputs_fingerprint = frozen_inputs_fingerprint
            self._last_scanner_output_fingerprint = scanner_output_fingerprint
            generation = self._generation

        round_limit = float(
            getattr(scanner_params, "scanner_round_time_limit_seconds", 30.0)
        )
        round_duration = time.perf_counter() - started
        return {
            "ok": not market_error
            and not active_universe_degraded
            and len(active_universe) == limit
            and bool(batch.get("complete", True))
            and round_duration <= round_limit,
            "requested_universe_rows": limit,
            "universe_complete": len(active_universe) == limit,
            "market_error": market_error,
            "batch": batch,
            "cycle_deadline_met": round_duration <= round_limit,
            "cycle_deadline_seconds": round_limit,
            "generation": generation,
            "candidate_count": len(_candidate_rows(candidates_context)),
            "candidate_pipeline": copy.deepcopy(
                dict(candidates_context.get("pipeline") or {})
            ),
            "candidate_rejection_summary": copy.deepcopy(
                dict(candidates_context.get("rejection_summary") or {})
            ),
            "candidate_score_gate_summary": copy.deepcopy(
                dict(candidates_context.get("score_gate_summary") or {})
            ),
            "active_universe_rows": len(active_universe),
            "active_universe_degraded": active_universe_degraded,
            "active_universe_errors": active_universe_errors,
            "candidate_update_published": True,
            "sector_diagnostics": {
                key: (context.get("sector_context") or {}).get(key)
                for key in ("available", "source", "mapped_stocks", "eligible_stocks", "mapping_coverage", "strong_sectors")
            },
            "buy_fail_closed": bool(active_universe_degraded or market_error),
            "market_state_generation": int(frozen_market_state.get("generation") or 0),
            "market_state_row_count": len(frozen_rows),
            "frozen_inputs_fingerprint": frozen_inputs_fingerprint,
            "scanner_output_fingerprint": scanner_output_fingerprint,
            "reason": str(reason),
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "market_context": copy.deepcopy(self._market_context),
                "candidates_context": copy.deepcopy(self._candidates_context),
                "active_universe": copy.deepcopy(self._active_universe),
                "market_source_status": copy.deepcopy(self._market_source_status),
                "market_scanner_generation": self._generation,
                "market_scanner_refreshed_at": self._last_refresh_at,
            }


# =============================================================================
# Intraday Component
# =============================================================================


class RuntimeIntradayComponent:
    """將 P0 持倉與 P1 候選交給 intraday 核心產生訊號。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._signals: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._event_buffers = {}
        self._event_offsets = {}
        self._last_errors: Dict[Tuple[str, str], str] = {}
        self._generation = 0

    def get(self, symbol: str, market: str) -> Optional[Dict[str, Any]]:
        key = _symbol_key(symbol, market)
        with self._lock:
            row = self._signals.get(key)
            return copy.deepcopy(row) if row is not None else None

    def process(
        self,
        runtime: "TradingSystemRuntime",
        *,
        changed_keys: Optional[Iterable[Tuple[str, str]]] = None,
        force: bool = False,
        only_keys: Optional[Iterable[Tuple[str, str]]] = None,
    ) -> List[Dict[str, Any]]:
        intraday = runtime.modules["intraday"]
        parameters = _parameter(runtime.registry, "intraday")
        market_context = runtime.current_strategy_market_context()
        candidates_context = runtime.market_scanner.candidates_context
        candidate_rows = _candidate_rows(candidates_context)
        watch_n = max(
            0, int(_setting(runtime.settings, "intraday_watch_count", 20) or 0)
        )

        position_keys = runtime.positions.keys()
        candidate_keys = [
            _symbol_key(row.get("symbol"), row.get("market"))
            for row in candidate_rows[:watch_n]
        ]
        ordered_keys: List[Tuple[str, str]] = []
        for key in [*position_keys, *candidate_keys]:
            if key[0] and key not in ordered_keys:
                ordered_keys.append(key)

        watch_keys = set(ordered_keys)
        with self._lock:
            for old_key in set(self._signals) - watch_keys:
                self._signals.pop(old_key, None)
                self._last_errors.pop(old_key, None)
        if only_keys is not None:
            wanted = set(only_keys)
            ordered_keys = [key for key in ordered_keys if key in wanted]
        elif changed_keys is not None and not force:
            changed = set(changed_keys)
            ordered_keys = [key for key in ordered_keys if key in changed]

        candidate_map = {
            _symbol_key(row.get("symbol"), row.get("market")): row
            for row in candidate_rows
        }
        updated: List[Dict[str, Any]] = []
        errors: Dict[Tuple[str, str], str] = {}
        gateway = runtime._ensure_gateway() if ordered_keys else None

        for key in ordered_keys:
            position = runtime.positions.get(*key)
            candidate = copy.deepcopy(candidate_map.get(key))
            candidate_snapshot = candidates_context.get("candidate_snapshot")
            if candidate and candidate_snapshot and not runtime.candidate_snapshot_valid(candidate_snapshot):
                candidate["trade_ready"] = False
            try:
                with runtime._lock:
                    live_buffer = runtime._market_event_buffers.get(key)
                    if (
                        live_buffer is not None
                        and live_buffer.parameter_key
                        == intraday.event_buffer_parameter_key(parameters)
                    ):
                        quote_sequence = live_buffer.clone()
                    else:
                        events = list(runtime._event_history.get(key, []))
                        memory_store = SimpleNamespace(
                            load_fugle_events=lambda **_kwargs: events
                        )
                        quote_sequence = intraday.load_quote_sequence_from_gateway(
                            SimpleNamespace(store=memory_store), key[0], key[1]
                        )
                input_token = runtime.input_token(*key)
                signal = intraday.get_signal_context(
                    symbol=key[0],
                    market=key[1],
                    gateway=gateway,
                    candidate=candidate,
                    candidates_context=candidates_context,
                    market_context=market_context,
                    quote_sequence=quote_sequence,
                    include_aggregates=False,
                    parameters=parameters,
                    position=position,
                )
                if isinstance(signal, Mapping):
                    row = dict(signal)
                    if not runtime.result_valid(input_token, *key):
                        continue
                    row["runtime_token"] = input_token
                    row["candidate_snapshot"] = copy.deepcopy(candidate_snapshot)
                    row["market_permission_snapshot_id"] = market_context.get("market_permission_snapshot_id")
                    row["candidate_snapshot_fingerprint"] = candidates_context.get("data_fingerprint") or _stable_hash(candidates_context)
                    row["candidate_input_fingerprint"] = _stable_hash(row.get("candidate") or {})
                    row.setdefault("symbol", key[0])
                    row.setdefault("market", key[1])
                    if position is not None and row.get(
                        "strategy_type"
                    ) == position.get("strategy_type"):
                        with runtime.positions._lock:
                            held = runtime.positions._positions.get(key)
                            if held is not None:
                                held["strategy_state"] = row.get("strategy_state")
                    updated.append(row)
            except Exception as exc:
                errors[key] = f"{type(exc).__name__}: {exc}"

        with self._lock:
            for row in updated:
                self._signals[_symbol_key(row.get("symbol"), row.get("market"))] = row
            self._last_errors.update(errors)
            if updated or errors:
                self._generation += 1

        return updated

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "signals": {
                    key: copy.deepcopy(
                        {
                            k: v
                            for k, v in row.items()
                            if k not in {"bars", "parameters", "market_context"}
                        }
                    )
                    for key, row in self._signals.items()
                },
                "intraday_errors": {
                    f"{market}:{symbol}": message
                    for (symbol, market), message in self._last_errors.items()
                },
                "intraday_generation": self._generation,
            }


# =============================================================================
# Trading 執行 Component
# =============================================================================


class RuntimeExecutionComponent:
    """編排 Trading 決策與 CTS 執行，不重寫 Trading 核心規則。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._last_decision: Dict[str, Any] = {}
        self._last_execution: Dict[str, Any] = {}
        self._last_status = "IDLE"
        self._sell_lifecycles = {}

    def process_signal(self, runtime, signal, *, manual_action=None):
        identity = {"symbol": signal.get("symbol"), "signal_fingerprint": signal.get("signal_fingerprint")}
        if signal.get("entry_signal"):
            runtime.record_order_event("ENTRY_SIGNAL_CREATED", "CREATED", identity)
        try:
            result = self._process_signal(runtime, signal, manual_action=manual_action)
        except Exception as exc:
            result = {"ok": False, "executed": False, "status": "PRE_SUBMIT_REJECTED",
                      "primary_rejection_code": "EXECUTION_EXCEPTION",
                      "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()}
        result.setdefault("execution_attempt", True)
        for name in ("broker_submit_attempt", "broker_accepted", "broker_rejected", "broker_unknown"):
            result.setdefault(name, False)
        if not result.get("broker_submit_attempt") and not result.get("broker_unknown"):
            primary = (result.get("primary_rejection_code") or
                       (result.get("decision") or {}).get("primary_rejection_code") or result.get("status"))
            runtime.record_order_event("PRE_SUBMIT_RESULT", "PRE_SUBMIT_REJECTED",
                                       dict(identity, primary_reason=primary, error=result.get("error")))
        if signal.get("exit_signal"):
            key = _symbol_key(signal.get("symbol"), signal.get("market"))
            rv = result.get("revalidation") or (result.get("execution") or {}).get("revalidation") or {}
            if result.get("status") == "MARKETSTATE_QUOTE_MISSING" or rv.get("severity") == "CRITICAL":
                runtime._unsent_exits[key] = dict(signal, signal_fingerprint="", asof=signal.get("asof") or _now_text())
            elif result.get("executed") or (result.get("execution") or {}).get("executed"):
                runtime._unsent_exits.pop(key, None)
        if signal.get("exit_signal") and (result.get("status")=="MARKETSTATE_QUOTE_MISSING" or (result.get("revalidation") or {}).get("severity")=="CRITICAL"):
            runtime._record_run_flow(run_id=runtime._new_run_id("SELL"),stage="SELL_ALL_PRICE_SOURCES_UNAVAILABLE",status="ERROR",details={"severity":"CRITICAL","symbol":signal.get("symbol"),"action":"MANUAL_ATTENTION"})
        return result

    @_serialized_execution
    def _process_signal(
        self,
        runtime: "TradingSystemRuntime",
        signal: Mapping[str, Any],
        *,
        manual_action: Optional[str] = None,
    ) -> Dict[str, Any]:
        trading = runtime.modules["trading"]
        parameters = _parameter(runtime.registry, "trading")
        signal_context = dict(signal)
        signal_context["order_trade_date"] = runtime._session_trade_date_int()
        signal_context["market_context"] = runtime.current_strategy_market_context()
        symbol = str(signal_context.get("symbol") or "").strip()
        market = _normalize_market(signal_context.get("market"))
        if not symbol:
            return {"ok": False, "status": "SIGNAL_SYMBOL_MISSING"}

        action_hint = str(manual_action or "").strip().upper()
        if action_hint:
            if action_hint not in {"BUY", "SELL"}:
                raise ValueError("manual_action 僅允許 BUY / SELL")
            signal_context["entry_signal"] = action_hint == "BUY"
            signal_context["exit_signal"] = action_hint == "SELL"
            signal_context["execution_trigger"] = "MANUAL"
            if action_hint == "SELL":
                signal_context.update(exit_fraction=1.0, exit_stage="MANUAL_FULL_EXIT")
        else:
            # Intraday 自動訊號未自行標記來源；缺少此值會讓 Trading 的 Wave Gate 略過。
            signal_context["execution_trigger"] = "AUTO"

        if signal_context.get("exit_signal"):
            owned = runtime.positions.get(symbol, market) or {}
            if (
                owned.get("managed_by") != "MOMENTUM_SCALPING"
                or int(owned.get("managed_quantity") or 0) <= 0
            ):
                return {
                    "ok": False,
                    "status": "EXTERNAL_POSITION_PROTECTED",
                    "symbol": symbol,
                }
            if owned.get("managed_account_id") and owned.get(
                "managed_account_id"
            ) != runtime._cts_status().get("account_id"):
                return {
                    "ok": False,
                    "status": "POSITION_ACCOUNT_MISMATCH",
                    "symbol": symbol,
                }
            entry_day = str(owned.get("managed_trade_date") or "").replace("-", "")
            if not entry_day:
                try:
                    entry_day = (
                        datetime.fromisoformat(str(owned.get("entry_time")))
                        .astimezone(timezone(timedelta(hours=8)))
                        .strftime("%Y%m%d")
                    )
                except (ValueError, TypeError):
                    entry_day = ""
            if entry_day != str(runtime._session_trade_date_int()):
                return {
                    "ok": False,
                    "status": "NON_TODAY_POSITION_PROTECTED",
                    "symbol": symbol,
                }

        quote = runtime.quote(symbol, market)
        if signal_context.get("exit_signal"):
            quote, quote_error = trading.resolve_executable_sell_quote(runtime._ensure_gateway(), symbol, market,
                signal_context.get("sell_fallback_quote") or quote)
            if quote_error:
                result = {"ok": False, "status": quote_error, "symbol": symbol, "market": market,
                          "revalidation": dict(quote, severity="CRITICAL"), "position_state": "EXIT_PENDING"}
                runtime.journal.record_execution(result)
                return result
        if not quote:
            gateway = runtime._ensure_gateway()
            try:
                quote = gateway.get_quote(symbol, market or None) or {}
            except Exception:
                quote = {}
            if not quote and signal_context.get("exit_signal") and callable(getattr(gateway,"get_sell_quote",None)):
                quote = gateway.get_sell_quote(symbol, market or None) or {}
            if isinstance(quote, Mapping) and quote:
                runtime._merge_quotes([quote])
        if not quote:
            result = {
                "ok": False,
                "status": "MARKETSTATE_QUOTE_MISSING",
                "symbol": symbol,
                "market": market,
            }
            runtime.journal.record_execution(result)
            return result

        signal_context["sell_fallback_quote"] = dict(quote)
        if signal_context.get("entry_signal"):
            health = runtime._cts_status().get("connection_health", "READY")
            if health != "READY":
                return {"ok": False, "status": "CTS_HEALTH_NOT_READY", "health": health}
            if (symbol, market) in runtime._strategy_restore_required:
                buffer = runtime._market_event_buffers.get((symbol,market))
                after = [b for b in getattr(buffer,"buckets",{}).values() if str(b.get("start")) > runtime._strategy_restore_required[(symbol,market)]]
                if len(after) < _parameter(runtime.registry,"intraday").min_bars:
                    return {"ok": False, "status": "STRATEGY_RESTORE_WARMUP"}
                runtime._strategy_restore_required.pop((symbol,market),None)
            if (runtime._recovery_state != "READY" or runtime._kill_switch
                    or runtime._daily_loss_lock or runtime.journal._last_write_error
                    or runtime._worker_buy_blocked or runtime._ingress_failed
                    or not runtime._raw_trade_events.empty()):
                return {"ok": False, "status": "RUNTIME_BUY_LOCK", "safety": runtime.safety_status()}
            if quote.get("integrity_error"):
                return {"ok": False, "status": "MARKET_INTEGRITY_ERROR"}
            if signal_context.get("strategy_type") not in {"MOMENTUM", "TREND"}:
                return {"ok": False, "status": "STRATEGY_OWNER_REQUIRED"}
            token = signal_context.get("runtime_token")
            if not token or not runtime.result_valid(token, symbol, market):
                return {"ok": False, "status": "STALE_STRATEGY_RESULT"}
            if signal_context.get("candidate_snapshot") and not runtime.candidate_snapshot_valid(signal_context["candidate_snapshot"]):
                return {"ok": False, "status": "CANDIDATE_SNAPSHOT_EXPIRED"}
            intraday_params = _parameter(runtime.registry, "intraday")
            status = runtime._gateway_status().get("fugle") or {}
            disconnected = status.get("last_disconnect_at")
            if disconnected:
                with runtime._lock:
                    buffer = runtime._market_event_buffers.get((symbol, market))
                    buckets = list(getattr(buffer, "buckets", {}).values())
                after_gap = [b for b in buckets if str(b.get("first_timestamp") or "") > str(disconnected)]
                if runtime._data_recovery_epoch != disconnected or len(after_gap) < intraday_params.min_bars:
                    return {"ok": False, "status": "DATA_RECOVERY_WARMUP"}
            exit_clock = datetime.strptime(intraday_params.exit_time, "%H:%M:%S").time()
            if _now().time() >= exit_clock:
                return {"ok": True, "status": "ENTRY_AFTER_EXIT_TIME", "symbol": symbol}
            market_client = getattr(runtime.gateway, "market", None)
            if runtime._pipeline_degraded or getattr(
                getattr(market_client, "store", None), "_write_error", ""
            ):
                return {
                    "ok": False,
                    "status": "MARKET_EVENT_PIPELINE_DEGRADED",
                    "symbol": symbol,
                }
            if (
                signal_context["market_context"].get("market_buy_data_ready", signal_context["market_context"].get("realtime_buy_data_ready"))
                is False
            ):
                return {
                    "ok": False,
                    "status": "MARKET_GUARD_NOT_READY",
                    "symbol": symbol,
                }
            metadata = getattr(
                getattr(runtime.gateway, "market", None), "_security_metadata", {}
            ).get(symbol, {})
            if str(symbol).startswith("00") and (
                metadata.get("security_type")
                not in {"24", "26", "28", "30", "32", "45", "46", "47"}
                or metadata.get("currency") != "TWD"
                or metadata.get("board_lot") != parameters.lot_size
            ):
                return {
                    "ok": False,
                    "status": "ETF_TRADING_METADATA_UNSUPPORTED",
                    "symbol": symbol,
                }
            if metadata and (
                str(metadata.get("metadata_date", "")).replace("-", "")
                != str(runtime._session_trade_date_int())
                or not metadata.get("day_trade_eligible")
            ):
                return {
                    "ok": False,
                    "status": "DAY_TRADE_METADATA_NOT_CURRENT",
                    "symbol": symbol,
                }

        account_state = runtime.positions.account_state()
        prepare_account = getattr(trading, "prepare_execution_account_state", None)
        if callable(prepare_account):
            account_state = prepare_account(
                account_state,
                trading.get_system_order_registry(),
                account_id=str(runtime._cts_status().get("account_id") or ""),
                trade_date=runtime._session_trade_date_int(),
            )
        refresh_sector = getattr(runtime.modules["market"], "refresh_candidate_sector", None)
        if callable(refresh_sector) and signal_context.get("entry_signal"):
            signal_context["candidate"] = refresh_sector(signal_context.get("candidate"),
                signal_context["market_context"], now=_now(), quote=quote)
        decision = trading.evaluate_trading_decision(
            signal_context,
            quote,
            account_state,
            parameters=parameters,
        )
        decision = dict(decision or {})
        action = str(decision.get("action") or action_hint or "").upper()

        with self._lock:
            self._last_decision = copy.deepcopy(decision)

        if str(decision.get("decision") or "").upper() != "ALLOW":
            result = {
                "ok": True,
                "status": "DECISION_REJECTED",
                "symbol": symbol,
                "market": market,
                "action": action,
                "decision": decision,
            }
            result["trading_input"] = {
                "signal_context": {
                    k: v
                    for k, v in signal_context.items()
                    if k not in {"bars", "market_context", "parameters"}
                }
            }
            runtime.journal.record_execution(result)
            with self._lock:
                self._last_execution = copy.deepcopy(result)
                self._last_status = result["status"]
            return result

        if not action_hint:
            if not runtime.strategy_enabled:
                return {"ok": True, "status": "STRATEGY_DISABLED", "decision": decision}
            if action == "SELL":
                position = runtime.positions.get(symbol, market) or {}
                if position.get("managed_by") != "MOMENTUM_SCALPING":
                    return {
                        "ok": True,
                        "status": "EXTERNAL_POSITION_MANUAL_ONLY",
                        "decision": decision,
                    }
            if runtime.positions.is_manual_takeover(symbol, market):
                return {"ok": True, "status": "MANUAL_TAKEOVER", "decision": decision}
            mode = str(
                _setting(runtime.settings, "control_mode", "MANUAL") or "MANUAL"
            ).upper()
            if mode != "AUTO":
                return {
                    "ok": True,
                    "status": "AUTO_MODE_DISABLED",
                    "decision": decision,
                }
            if not bool(_setting(runtime.settings, "auto_execute", False)):
                return {
                    "ok": True,
                    "status": "AUTO_EXECUTION_DISABLED",
                    "decision": decision,
                }
            if action == "BUY" and runtime.pause_blocks_buy:
                return {"ok": True, "status": "PAUSE_BLOCKS_BUY", "decision": decision}

        cts_status = runtime._cts_status()
        if not bool(cts_status.get("connected")):
            return {"ok": False, "status": "CTS_NOT_CONNECTED", "decision": decision}
        account_id = str(
            cts_status.get("account_id")
            or _setting(runtime.settings, "cts_account_id", "")
            or ""
        ).strip()
        if not account_id:
            return {"ok": False, "status": "CTS_ACCOUNT_MISSING", "decision": decision}

        registry_fn = getattr(trading, "get_system_order_registry", None)
        if callable(registry_fn):
            pending = [
                row
                for row in registry_fn()
                if row.get("symbol") == symbol
                and row.get("account_id") == account_id
                and row.get("action") == action
                and str(row.get("status") or "")
                not in {"FILLED", "REJECTED", "CANCELED", "CANCELLED", "PRE_SUBMIT_REJECTED", "CONFIRMED_ABSENT"}
            ]
            if pending:
                return {
                    "ok": True,
                    "status": "ORDER_PENDING_RECONCILIATION",
                    "symbol": symbol,
                }
        if action == "BUY" and not runtime._last_cts_reconcile.get("query_ok"):
            return {
                "ok": False,
                "status": "ACCOUNT_RECONCILIATION_REQUIRED",
                "symbol": symbol,
            }
        gateway = runtime._ensure_gateway()
        gateway.checkpoint_execution_intent = runtime._checkpoint_order_registry
        # 更新：只讀已發佈市場快照；交易熱路徑不重新抓取網路行情。
        gateway.get_strategy_market_context = runtime.current_strategy_market_context
        gateway.record_entry_trigger_revalidation = runtime.record_entry_trigger_revalidation
        gateway.record_order_event = runtime.record_order_event
        runtime._restore_order_registry()
        try:
            execution = trading.execute_allowed_decision(
                gateway,
                decision,
                signal_context,
                quote,
                account_id=account_id,
                parameters=parameters,
                confirm_fill=True,
            )
        finally:
            runtime._checkpoint_order_registry()
        execution = dict(execution or {})
        execution.setdefault(
            "ok",
            str(execution.get("status") or "")
            not in {
                "ORDER_REJECTED",
                "BUY_REVALIDATION_REJECTED",
                "SELL_REVALIDATION_REJECTED",
                "SELL_ORDER_ID_MISSING",
                "FILL_QUANTITY_EXCEEDS_REQUEST",
                "MATCH_IDENTITY_AMBIGUOUS",
            },
        )
        execution.setdefault("symbol", symbol)
        execution.setdefault("market", market)
        execution.setdefault("action", action)
        execution["exit_stage"] = signal_context.get("exit_stage")
        execution["exit_fraction"] = signal_context.get("exit_fraction")
        if execution.get("executed") or execution.get("order_id"):
            runtime._account_generation += 1
            runtime._last_cts_reconcile["query_ok"] = False
        runtime.positions.apply_execution(execution)
        runtime._refresh_book_targets()
        runtime._subscription_refresh_needed.set()
        fingerprint = str(signal_context.get("signal_fingerprint") or "")
        if action == "SELL" and (execution.get("executed") or execution.get("order_id")):
            runtime.positions.mark_signal_processed(fingerprint)
        runtime.journal.record_execution(
            {
                "trading_input": {
                    "signal_context": {
                        k: v
                        for k, v in signal_context.items()
                        if k not in {"bars", "market_context", "parameters"}
                    }
                },
                "trigger": "MANUAL" if action_hint else "AUTO",
                "decision": decision,
                "execution": execution,
            }
        )

        with self._lock:
            self._last_execution = copy.deepcopy(execution)
            self._last_status = str(execution.get("status") or "EXECUTED")
            if action == "SELL":
                key = _symbol_key(symbol, market)
                self._sell_lifecycles.pop(key, None)
                self._sell_lifecycles[key] = {
                    "decision": copy.deepcopy(decision),
                    "execution": copy.deepcopy(execution),
                }
                while len(self._sell_lifecycles) > 128:
                    self._sell_lifecycles.pop(next(iter(self._sell_lifecycles)))
        return execution

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "sell_lifecycles": copy.deepcopy(self._sell_lifecycles),
                "trading_status": self._last_status,
                "last_decision": copy.deepcopy(self._last_decision),
                "last_execution": copy.deepcopy(self._last_execution),
            }


# =============================================================================
# Runtime 主編排器
# =============================================================================


class TradingSystemRuntime:
    """交易系統執行期共享狀態與 Component 編排器。"""

    REQUIRED_MODULES = ("api_gateway", "market", "scanner", "intraday", "trading")

    def __init__(
        self, modules: Mapping[str, Any], registry: Any, settings: Any
    ) -> None:
        self._state_transaction = threading.RLock()
        self._boot_id = uuid.uuid4().hex
        self._snapshot_seq = 0
        self._recovery_state = "RECOVERY"
        self._kill_switch = False
        self._daily_loss_lock = False
        self._session_id = ""
        self._journal_loaded = False
        self._data_recovery_epoch = None
        self._execution_heartbeat = 0.0
        self.modules = dict(modules)
        missing = [name for name in self.REQUIRED_MODULES if name not in self.modules]
        if missing:
            raise ValueError(f"Runtime 缺少核心模組：{missing}")

        self.registry = registry
        self.settings = settings
        validate_settings = getattr(settings, "validate", None)
        if callable(validate_settings):
            validate_settings()
        validate_registry = getattr(registry, "validate_all", None)
        if callable(validate_registry):
            validate_registry()

        self.base_dir = _module_base_dir(self.modules)
        self.resources = ResourceManager()
        self.journal = RuntimeJournalComponent(self.base_dir)
        self.positions = RuntimePositionComponent(settings)
        self.market_scanner = RuntimeMarketScannerComponent()
        self.intraday = RuntimeIntradayComponent()
        self.execution = RuntimeExecutionComponent()

        self._lock = threading.RLock()
        self._gateway_lock = threading.RLock()
        self._raw_trade_events = queue.Queue(maxsize=10000)
        self._ingress_failed = False
        self._worker_buy_blocked = False
        self._worker_faults = {}
        self._worker_fault_times = {}
        self._last_state_checkpoint = 0.0
        self._book_targets_lock = threading.RLock()
        self._display_lock = threading.Lock()
        self._display_snapshot = {}
        self.gateway: Any = None
        self._gateway_signature: Tuple[Any, ...] = ()
        self._cts_callback_gateway_id = 0
        self.gateway_status_cache: Dict[str, Any] = {
            "mode": str(_setting(settings, "api_mode", "LIVE") or "LIVE").upper(),
            "timestamp": _now_text(),
            "market_provider": "Fugle",
            "fugle": {"connected": False},
            "cts_connected": False,
            "cts_account_id": "",
            "cts": {"connected": False, "account_id": ""},
        }
        self._quotes: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._last_quote_sequences: Dict[Tuple[str, str], int] = {}
        self._subscription_error_fingerprint = ""
        self._subscription_error_logged_at = 0.0
        self._subscription_refresh_needed = threading.Event()
        self._event_overflow_count = 0
        self._pipeline_degraded = False
        self._overflow_keys = set()
        self._pipeline_good_rounds = 0
        self._rest_emergency_queue = queue.Queue(maxsize=100)
        self._strategy_restore_required = {}
        self._strategy_checkpoint_at = 0.0
        self._strategy_checkpoint = {}
        self._queued_event_keys = set()
        self._latency_metrics = {}
        self._market_event_buffers = {}
        self._event_buffer_parameters = None
        self._event_buffer_key = ""
        self._event_history: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        self._trade_event_queue: queue.Queue[Dict[str, Any]] = queue.Queue(
            maxsize=_RUNTIME_EVENT_QUEUE_SIZE
        )
        self._cts_callback_queue: queue.Queue[Dict[str, Any]] = queue.Queue(
            maxsize=_RUNTIME_EVENT_QUEUE_SIZE
        )
        self._subscriptions: Dict[str, Any] = {
            "symbols": [],
            "p0": 0,
            "p1": 0,
            "p2": 0,
            "p0_symbols": [],
            "p1_symbols": [],
            "p2_symbols": [],
            "total": 0,
            "books_requested": str(
                _setting(settings, "fugle_books_subscription_mode", "OFF") or "OFF"
            ),
            "books_effective": "P0_P1",
            "books_supported": True,
        }
        self._strategy_enabled = False
        self._pause_blocks_buy = False
        self._run_seq = 0
        self._cts_query_pool = self.resources.io_pool("CTSReconcile", 1)
        self._cts_query_future = None
        self._last_cts_reconcile_monotonic = 0.0
        self._last_cts_reconcile: Dict[str, Any] = {}
        self._last_cts_reconcile_fingerprint = ""
        self._gateway_startup_signature: Tuple[Any, ...] = ()
        self._gateway_startup_checked = False
        self._gateway_startup_error = ""
        self._gateway_startup_status: Dict[str, Any] = {}
        self._market_error_fingerprint = ""
        self._market_error_first_monotonic = 0.0
        self._market_error_last_log_monotonic = 0.0
        self._market_error_suppressed = 0
        self._market_error_notification_due = False
        self._last_trading_cycle_log_monotonic = 0.0
        self._last_trading_cycle_state_fingerprint = ""
        self._order_registry_restored = False
        self._closed = False
        self._account_generation = 0
        self._gateway_event_sink_enabled = False
        self._last_maintenance = 0.0
        self._last_clock_exit = 0.0
        self._last_clock_orders = 0.0
        self._last_sell_maintenance = 0.0
        self._unsent_exits = {}
        self._pending_position_settings = None

    @property
    def strategy_enabled(self) -> bool:
        with self._lock:
            return self._strategy_enabled

    @property
    def pause_blocks_buy(self) -> bool:
        with self._lock:
            return self._pause_blocks_buy

    @property
    def execution_log_path(self) -> Path:
        return self.journal.execution_log_path

    @property
    def daily_trade_report_path(self) -> Path:
        return self.journal.daily_trade_report_path

    @property
    def run_flow_log_path(self) -> Path:
        return self.journal.run_flow_log_path

    @property
    def recent_error_log_path(self) -> Path:
        return self.journal.recent_error_log_path

    def _gateway_config(self, settings: Any) -> Tuple[Tuple[Any, ...], Dict[str, Any]]:
        replay_value = str(
            _setting(settings, "replay_dir", "api_replay") or "api_replay"
        ).strip()
        replay_path = Path(replay_value).expanduser()
        if not replay_path.is_absolute():
            replay_path = self.base_dir / replay_path

        fugle_api_key = str(
            os.getenv("FUGLE_MARKETDATA_API_KEY")
            or os.getenv("FUGLE_MARKETDATA_API")
            or ""
        ).strip()
        kwargs: Dict[str, Any] = {
            "mode": str(_setting(settings, "api_mode", "LIVE") or "LIVE").upper(),
            "replay_dir": str(replay_path),
            "api_key": fugle_api_key,
            "freshness_seconds": float(
                _setting(settings, "fugle_event_freshness_seconds", 3.0) or 3.0
            ),
            "reconnect_min_seconds": float(
                _setting(settings, "fugle_reconnect_min_seconds", 30.0) or 30.0
            ),
            "reconnect_max_seconds": float(
                _setting(settings, "fugle_reconnect_max_seconds", 60.0) or 60.0
            ),
            "cts_com_prog_id": str(
                _setting(settings, "cts_com_prog_id", "") or "DJTRADEOBJLibCTS.TradeApp"
            ),
            "event_sink": self._on_fugle_event,
        }
        signature = (
            kwargs["mode"],
            kwargs["replay_dir"],
            kwargs["freshness_seconds"],
            kwargs["reconnect_min_seconds"],
            kwargs["reconnect_max_seconds"],
            kwargs["cts_com_prog_id"],
            bool(fugle_api_key),
            _stable_hash(fugle_api_key) if fugle_api_key else "",
            True,
        )
        return signature, kwargs

    def _build_gateway(self, settings: Any) -> Any:
        gateway_class = getattr(self.modules["api_gateway"], "APIGateway")
        _signature, kwargs = self._gateway_config(settings)
        gateway = gateway_class(**kwargs)
        configure = getattr(gateway, "configure_resources", None)
        if callable(configure):
            configure(self.resources)
        cts = getattr(gateway, "cts", None)
        if cts is not None:
            cts._anomaly_sink = lambda method, details: self.journal.record_run_flow(
                run_id="CTS", stage="CTS_" + str(method),
                status="ERROR", details=details)
        return gateway

    def _ensure_gateway(self) -> Any:
        signature, _kwargs = self._gateway_config(self.settings)
        with self._gateway_lock:
            if self.gateway is not None and self._gateway_signature == signature:
                return self.gateway

            previous = self.gateway
            gateway = self._build_gateway(self.settings)
            self.gateway = gateway
            self._gateway_signature = signature
            self._gateway_event_sink_enabled = bool(signature[-1])
            self._gateway_startup_signature = ()
            self._gateway_startup_checked = False
            self._gateway_startup_error = ""
            self._gateway_startup_status = {}
            self._cts_callback_gateway_id = 0
            self._register_cts_callback(gateway)

            if previous is not None and previous is not gateway:
                close = getattr(previous, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
            return gateway

    def _ensure_gateway_ready(self) -> Any:
        signature, _kwargs = self._gateway_config(self.settings)
        with self._gateway_lock:
            if (
                self._gateway_startup_checked
                and self._gateway_startup_signature == signature
            ):
                if self._gateway_startup_error:
                    raise RuntimeError(self._gateway_startup_error)
                if self.gateway is not None:
                    return self.gateway

        gateway = self._ensure_gateway()
        status_fn = getattr(gateway, "status", None)
        if not callable(status_fn):
            error = "APIGateway 缺少 status() 正式介面"
            with self._gateway_lock:
                self._gateway_startup_signature = signature
                self._gateway_startup_checked = True
                self._gateway_startup_error = error
                self._gateway_startup_status = {}
            raise RuntimeError(error)

        try:
            payload = status_fn()
            status = dict(payload) if isinstance(payload, Mapping) else {}
            if not status:
                raise RuntimeError("APIGateway.status() 回傳格式錯誤")
            mode = str(
                status.get("mode")
                or _setting(self.settings, "api_mode", "LIVE")
                or "LIVE"
            ).upper()
            fugle = (
                dict(status.get("fugle") or {})
                if isinstance(status.get("fugle"), Mapping)
                else {}
            )
            if mode == "LIVE" and not bool(fugle.get("api_key_configured")):
                raise RuntimeError("缺少環境變數 FUGLE_MARKETDATA_API_KEY")
        except Exception as exc:
            error = (
                f"{type(exc).__name__}: {exc}"
                if not isinstance(exc, RuntimeError)
                else str(exc)
            )
            with self._gateway_lock:
                self._gateway_startup_signature = signature
                self._gateway_startup_checked = True
                self._gateway_startup_error = error
                self._gateway_startup_status = {}
            raise RuntimeError(error) from exc

        with self._lock:
            self.gateway_status_cache = copy.deepcopy(status)
        with self._gateway_lock:
            self._gateway_startup_signature = signature
            self._gateway_startup_checked = True
            self._gateway_startup_error = ""
            self._gateway_startup_status = copy.deepcopy(status)
        return gateway

    def _enqueue_trade_event(self, item):
        item = dict(item)
        item.setdefault("received_monotonic", time.monotonic())
        key = _symbol_key(item.get("symbol"), item.get("market"))
        with self._lock:
            if key in self._queued_event_keys:
                return
            try:
                self._trade_event_queue.put_nowait(
                    {
                        "symbol": key[0],
                        "market": key[1],
                        "enqueued_at": time.monotonic(),
                    }
                )
                self._queued_event_keys.add(key)
            except queue.Full:
                self._event_overflow_count += 1
                self._pipeline_degraded = True
                self._pipeline_good_rounds = 0
                self._overflow_keys.add(key)

    def _on_fugle_event(self, payload: Mapping[str, Any]) -> None:
        """接收回呼只排入事件；K 棒與策略資料由交易工作依序處理。"""
        try:
            self._raw_trade_events.put_nowait(dict(payload))
        except queue.Full:
            self._ingress_failed = True

    def _consume_fugle_event(self, payload: Mapping[str, Any]) -> None:
        item = dict(payload)
        symbol = str(item.get("symbol") or "").strip()
        market = str(item.get("market") or "").strip()
        if not symbol:
            return
        gateway = getattr(self, "gateway", None)
        get_quote = getattr(gateway, "get_quote", None)
        if callable(get_quote):
            try:
                quote = get_quote(symbol, market or None)
            except Exception:
                quote = {}
            if isinstance(quote, Mapping) and quote:
                self._merge_quotes([quote])
                key = _symbol_key(quote.get("symbol"), quote.get("market"))
                sequence = _quote_sequence_value(quote)
                with self._lock:
                    self._last_quote_sequences[key] = max(
                        sequence, self._last_quote_sequences.get(key, 0)
                    )
        if (
            str(
                item.get("classification") or item.get("event_classification") or ""
            ).upper()
            != "NEW"
        ):
            return
        key = _symbol_key(symbol, market)
        market_client = getattr(self.gateway, "market", None)
        read_disconnect = getattr(market_client, "stream_disconnect_at", None)
        if callable(read_disconnect):
            disconnected = read_disconnect()
        else:
            status = market_client.status() if market_client and hasattr(market_client, "status") else {}
            disconnected = status.get("last_disconnect_at")
        with self._lock:
            if disconnected and disconnected != self._data_recovery_epoch:
                self._market_event_buffers.clear()
                self._event_history.clear()
                self._data_recovery_epoch = disconnected
            history = self._event_history.setdefault(key, [])
            if history and history[-1].get("trade_date") != item.get("trade_date"):
                history.clear()
            history.append(item)
            if len(history) > 2048:
                del history[:-2048]
            # 原始事件只保留近三分鐘；完整日內 OHLC/VWAP 與事件摘要增量保存。
            params = _parameter(self.registry, "intraday")
            buffer_cls = getattr(
                self.modules["intraday"], "IncrementalEventBuffer", None
            )
            if buffer_cls is not None:
                if self._event_buffer_parameters is not params:
                    self._event_buffer_parameters = params
                    self._event_buffer_key = self.modules["intraday"].event_buffer_parameter_key(params)
                parameter_key = self._event_buffer_key
                buffer = self._market_event_buffers.get(key)
                if buffer is None or buffer.parameter_key != parameter_key:
                    buffer = buffer_cls(params)
                    buffer.append_sequence(history, params)
                    self._market_event_buffers[key] = buffer
                else:
                    buffer.append_sequence([item], params)
                cutoff = int(item.get("event_time") or 0) - 180_000_000
                remove = 0
                for previous in history:
                    if int(previous.get("event_time") or 0) >= cutoff:
                        break
                    remove += 1
                if remove:
                    del history[:remove]
                # Universe 全部保留分鐘累積值；未監控股票不保留大量逐筆副本。
                watch = symbol in self._subscriptions.get("p0_symbols", ()) or symbol in self._subscriptions.get("p1_symbols", ()) or self.positions.get(*key) is not None
                raw_limit = 2048 if watch else 2
                if len(history) > raw_limit:
                    del history[:-raw_limit]
                if buffer.all_sizes and len(buffer) > raw_limit:
                    del buffer[:-raw_limit]
        self._enqueue_trade_event(item)

    def _register_cts_callback(self, gateway: Any) -> None:
        gateway_id = id(gateway)
        if self._cts_callback_gateway_id == gateway_id:
            return
        cts = getattr(gateway, "cts", None)
        add_callback = getattr(cts, "add_callback", None)
        if callable(add_callback):
            add_callback(self._on_cts_callback)
            self._cts_callback_gateway_id = gateway_id

    def _on_cts_callback(self, payload: Mapping[str, Any]) -> None:
        item = dict(payload)
        item.setdefault("received_monotonic", time.monotonic())
        try:
            self._cts_callback_queue.put_nowait(item)
        except queue.Full:
            self._recovery_state = "CALLBACK_OVERFLOW"
            self._last_cts_reconcile["query_ok"] = False
            try:
                self._cts_callback_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._cts_callback_queue.put_nowait(item)
            except queue.Full:
                pass

    def _cts_status(self) -> Dict[str, Any]:
        gateway = self.gateway
        if gateway is None:
            with self._lock:
                cached = self.gateway_status_cache.get("cts")
                return (
                    copy.deepcopy(dict(cached)) if isinstance(cached, Mapping) else {}
                )
        cts = getattr(gateway, "cts", None)
        status = getattr(cts, "status", None)
        if not callable(status):
            return {}
        try:
            result = status()
            output = dict(result) if isinstance(result, Mapping) else {}
        except Exception:
            return {}
        with self._lock:
            self.gateway_status_cache["cts"] = copy.deepcopy(output)
            self.gateway_status_cache["cts_connected"] = bool(output.get("connected"))
            self.gateway_status_cache["cts_account_id"] = str(
                output.get("account_id") or ""
            )
            self.gateway_status_cache["timestamp"] = _now_text()
        return output

    def record_order_event(self, stage, status, details):
        details = dict(details)
        if stage == "ORDER_RECONCILIATION":
            identity = str(details.get("order_intent_id") or "")
            signature = _stable_hash([status, details.get("order_id"), details.get("order_no"),
                                      details.get("queue_evidence"), details.get("reason")])
            cache = getattr(self, "_order_reconcile_log_states", {})
            if cache.get(identity) == signature:
                return
            cache[identity] = signature
            if len(cache) > 2048:
                cache.pop(next(iter(cache)))
            self._order_reconcile_log_states = cache
        response = details.get("result") or {}
        for key in ("order_id", "order_no", "return_code", "return_message", "error", "header"):
            if response.get(key) is not None:
                details[key] = response[key]
        self._record_run_flow(run_id=str(details.get("order_intent_id") or self._new_run_id("ORDER")),
                              stage=stage, status=status, details=details)
        with self._lock:
            counts = getattr(self, "_broker_audit_counts", {})
            if stage == "ORDER_SUBMIT_START":
                counts["broker_submit_attempt"] = counts.get("broker_submit_attempt", 0) + 1
            if stage == "ORDER_SUBMIT_RESULT":
                key = {"ACCEPTED": "broker_accepted", "REJECTED": "broker_rejected",
                       "STATUS_UNKNOWN": "broker_unknown"}.get(status)
                if key:
                    counts[key] = counts.get(key, 0) + 1
            self._broker_audit_counts = counts

    def record_entry_trigger_revalidation(self, details: Mapping[str, Any]) -> None:
        """PASS 僅代表訊號回落門檻通過，其餘進場風控各自判定。"""
        status = "PASS" if details["trigger_passed"] else "FAIL"
        message = (
            f"ENTRY_TRIGGER_REVALIDATION symbol={details.get('symbol', '')} "
            f"signal={details['signal_price']:.4f} "
            f"floor={details['entry_trigger_floor']:.4f} "
            f"last={details['last_price']:.4f} "
            f"retrace={details['retrace_pct']:+.2f}% {status}"
        )
        self._record_run_flow(
            run_id=self._new_run_id("ENTRY"),
            stage="ENTRY_TRIGGER_REVALIDATION",
            status=status,
            details=dict(details, message=message),
        )

    def _new_run_id(self, prefix: str) -> str:
        with self._lock:
            self._run_seq += 1
            seq = self._run_seq
        return (
            f"{str(prefix or 'RUN').upper()}-{_now().strftime('%Y%m%dT%H%M%S%f')}-{seq}"
        )

    def _record_run_flow(
        self,
        *,
        run_id: str,
        stage: str,
        status: str,
        details: Optional[Mapping[str, Any]] = None,
        duration_ms: Optional[float] = None,
    ) -> Dict[str, Any]:
        return self.journal.record_run_flow(
            run_id=run_id,
            stage=stage,
            status=status,
            details=details,
            duration_ms=duration_ms,
        )

    def set_runtime_policy(
        self, *, strategy_enabled: bool, pause_blocks_buy: bool
    ) -> None:
        with self._lock:
            self._strategy_enabled = bool(strategy_enabled)
            self._pause_blocks_buy = bool(pause_blocks_buy)

    @_serialized_state
    def update_settings(self, settings: Any) -> None:
        validate = getattr(settings, "validate", None)
        if callable(validate):
            validate()
        with self._lock:
            self.settings = settings
            self._subscriptions["books_requested"] = str(
                _setting(settings, "fugle_books_subscription_mode", "OFF") or "OFF"
            )
            self._subscriptions["books_effective"] = "P0_P1"
            self._subscriptions["books_supported"] = True
        with self._lock:
            self._pending_position_settings = copy.deepcopy(settings)

    def _merge_quotes(self, rows: Sequence[Mapping[str, Any]]) -> None:
        with self._lock:
            for raw in rows:
                if not isinstance(raw, Mapping):
                    continue
                row = dict(raw)
                key = _symbol_key(row.get("symbol"), row.get("market"))
                if not key[0]:
                    continue
                row["symbol"] = key[0]
                row["market"] = key[1]
                previous = self._quotes.get(key)
                if previous is None or _event_sort_key(row) >= _event_sort_key(
                    previous
                ):
                    self._quotes[key] = row

    def quote(self, symbol: str, market: str) -> Dict[str, Any]:
        key = _symbol_key(symbol, market)
        gateway = getattr(self, "gateway", None)
        if gateway is not None and callable(getattr(gateway, "get_quote", None)):
            latest = gateway.get_quote(symbol, market=market)
            if isinstance(latest, Mapping) and latest:
                return dict(latest)
        with self._lock:
            row = self._quotes.get(key)
            if row is not None:
                return copy.deepcopy(row)
            for candidate_key, candidate in self._quotes.items():
                if candidate_key[0] == key[0] and (
                    not key[1] or candidate_key[1] == key[1]
                ):
                    return copy.deepcopy(candidate)
        return {}

    def _refresh_book_targets(self, p1: Optional[Sequence[str]] = None) -> None:
        """訂閱更新獨立序列化；禁止持 Runtime 鎖等待 Gateway 或持倉鎖。"""
        with self._book_targets_lock:
            with self._lock:
                if p1 is not None:
                    self._book_candidate_symbols = set(p1)
                candidates = set(getattr(self, "_book_candidate_symbols", set()))
                settings, gateway = self.settings, self.gateway
            wanted = candidates | {symbol for symbol, _ in self.positions.keys() if symbol}
            if (str(_setting(settings, "api_mode", "LIVE")).upper() != "LIVE"
                    or not bool(_setting(settings, "fugle_websocket_enabled", True))):
                wanted = set()
            setter = getattr(gateway, "set_book_subscriptions", None)
            if callable(setter):
                setter(sorted(wanted))

    def _apply_subscriptions(
        self,
        *,
        p1_rows: Optional[Sequence[Mapping[str, Any]]] = None,
        p2_rows: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> Dict[str, Any]:
        p0 = [symbol for symbol, _market in self.positions.keys() if symbol]
        p1 = []
        if bool(_setting(self.settings, "fugle_candidate_subscription_enabled", True)):
            for row in (
                p1_rows
                if p1_rows is not None
                else _candidate_rows(self.market_scanner.candidates_context)
            ):
                symbol = str(row.get("symbol") or "").strip()
                if symbol and symbol not in p1:
                    p1.append(symbol)

        p2 = []
        for row in (
            p2_rows
            if p2_rows is not None
            else self.market_scanner.snapshot().get("active_universe", [])
        ):
            symbol = str(row.get("symbol") or "").strip()
            if symbol and symbol not in p2:
                p2.append(symbol)

        limit = max(
            1, int(_setting(self.settings, "fugle_subscription_limit", 500) or 500)
        )
        ordered: List[str] = []
        for symbol in [*p0, *p1, *p2]:
            if symbol not in ordered:
                ordered.append(symbol)
        selected = ordered[:limit]

        api_mode = str(_setting(self.settings, "api_mode", "LIVE") or "LIVE").upper()
        ws_enabled = bool(_setting(self.settings, "fugle_websocket_enabled", True))
        # 連線維護只由 StreamHealthWorker 執行，避免掃描工作等待 SDK 認證。
        if api_mode == "LIVE" and ws_enabled:
            try:
                gateway = self._ensure_gateway()
                # 先更新記憶體有效性，不能讓 REST 初始化或 socket 寫入延遲退出失效。
                self._refresh_book_targets(p1)
                gateway_status = gateway.status()
                fugle = (
                    dict(gateway_status.get("fugle") or {})
                    if isinstance(gateway_status, Mapping)
                    and isinstance(gateway_status.get("fugle"), Mapping)
                    else {}
                )
                if not bool(fugle.get("connected")) and not callable(
                    getattr(gateway, "maintain_market_stream", None)
                ):
                    gateway.fugle_login()
                gateway.subscribe_market(
                    selected,
                    initialize=bool(
                        _setting(self.settings, "fugle_rest_seed_enabled", True)
                    ),
                    channels=("trades",),
                )
                if self._subscription_error_fingerprint:
                    self._record_run_flow(
                        run_id=self._new_run_id("STREAM"),
                        stage="FUGLE_SUBSCRIPTION",
                        status="RECOVERED",
                        details={"symbols": selected},
                    )
                    self._subscription_error_fingerprint = ""
            except Exception as exc:
                fingerprint = _stable_hash(
                    {"error": f"{type(exc).__name__}: {exc}", "symbols": selected}
                )
                now_mono = time.monotonic()
                if (
                    fingerprint != self._subscription_error_fingerprint
                    or now_mono - self._subscription_error_logged_at >= 60.0
                ):
                    self._record_run_flow(
                        run_id=self._new_run_id("STREAM"),
                        stage="FUGLE_SUBSCRIPTION",
                        status="DEGRADED",
                        details={
                            "error": f"{type(exc).__name__}: {exc}",
                            "symbols": selected,
                        },
                    )
                    self._subscription_error_logged_at = now_mono
                self._subscription_error_fingerprint = fingerprint

        if api_mode != "LIVE" or not ws_enabled:
            gateway = getattr(self, "gateway", None)
            setter = getattr(gateway, "set_book_subscriptions", None)
            if callable(setter):
                setter([])

        p0_selected = [symbol for symbol in p0 if symbol in selected]
        p1_selected = [
            symbol for symbol in p1 if symbol in selected and symbol not in p0
        ]
        p2_selected = [
            symbol
            for symbol in p2
            if symbol in selected and symbol not in p0 and symbol not in p1
        ]
        with self._lock:
            self._subscriptions = {
                "symbols": selected,
                "p0": len(p0_selected),
                "p1": len(p1_selected),
                "p2": len(p2_selected),
                "p0_symbols": p0_selected,
                "p1_symbols": p1_selected,
                "p2_symbols": p2_selected,
                "total": len(selected),
                "limit": limit,
                "unsubscribed_position_symbols": sorted(set(p0) - set(selected)),
                "books_requested": str(
                    _setting(self.settings, "fugle_books_subscription_mode", "OFF")
                    or "OFF"
                ),
                "books_effective": "P0_P1",
                "books_symbols": sorted(set(p0) | set(p1)),
                "books_supported": True,
            }
            return copy.deepcopy(self._subscriptions)

    def _scan_marketstate_events(self) -> List[Tuple[str, str]]:
        """Fugle Trade Event 只由 APIGateway event_sink 進入 Runtime Queue。"""
        return []

    def _drain_trade_events(self) -> List[Tuple[str, str]]:
        deadline = time.monotonic() + 0.02
        for _ in range(1000):
            if time.monotonic() >= deadline:
                break
            try:
                event = self._raw_trade_events.get_nowait()
            except queue.Empty:
                break
            try:
                self._consume_fugle_event(event)
            except Exception:
                self._ingress_failed = True
                raise
        keys: List[Tuple[str, str]] = []
        for _ in range(self._trade_event_queue.qsize()):
            try:
                event = self._trade_event_queue.get_nowait()
            except queue.Empty:
                break
            key = _symbol_key(event.get("symbol"), event.get("market"))
            with self._lock:
                self._queued_event_keys.discard(key)
            if event.get("enqueued_at"):
                self._latency_metrics["latest_queue_wait_ms"] = round(
                    (time.monotonic() - event["enqueued_at"]) * 1000, 3
                )
            if key[0] and key not in keys:
                keys.append(key)
        # Queue 丟的是通知，事件本體先前已保存於 buffer；把漏通知的 symbol 補回處理。
        with self._lock:
            for key in sorted(self._overflow_keys):
                if key not in keys:
                    keys.append(key)
            if self._trade_event_queue.empty():
                self._pipeline_good_rounds += 1
                if self._pipeline_good_rounds >= 3 and all(self.quote(*key).get("realtime_fresh") is True for key in self._overflow_keys):
                    self._pipeline_degraded = False
                    self._overflow_keys.clear()
            else:
                self._pipeline_good_rounds = 0
        return keys

    def _market_failure_details(self, exc: BaseException) -> Dict[str, Any]:
        raw_error = f"{type(exc).__name__}: {exc}"
        details: Dict[str, Any] = {
            "error": raw_error,
            "raw_error": str(exc),
            "error_stage": str(getattr(exc, "stage", "") or ""),
            "stage_errors": [],
            "candidate_snapshot_preserved": True,
            "candidate_count": len(_candidate_rows(self.market_scanner.candidates_context)),
            "candidate_generation": self.market_scanner.snapshot().get("market_scanner_generation", 0),
            "buy_fail_closed": True,
        }
        market_module = self.modules.get("market")
        status_fn = getattr(market_module, "get_market_snapshot_status", None)
        if callable(status_fn):
            try:
                status_payload = status_fn()
                if isinstance(status_payload, Mapping):
                    status = copy.deepcopy(dict(status_payload))
                    details["market_snapshot_status"] = status
                    details["error_stage"] = str(
                        status.get("error_stage") or details["error_stage"] or ""
                    )
                    stage_errors = status.get("stage_errors")
                    if isinstance(stage_errors, Sequence) and not isinstance(
                        stage_errors, (str, bytes)
                    ):
                        details["stage_errors"] = [
                            copy.deepcopy(dict(item))
                            for item in stage_errors
                            if isinstance(item, Mapping)
                        ]
                    market_error = str(status.get("error") or "").strip()
                    if market_error:
                        details["market_error"] = market_error
            except Exception as status_exc:
                details["market_status_error"] = (
                    f"{type(status_exc).__name__}: {status_exc}"
                )
        return details

    def _record_market_failure(
        self,
        *,
        run_id: str,
        details: Mapping[str, Any],
        duration_ms: float,
    ) -> bool:
        fingerprint = _stable_hash(
            {
                "error": details.get("error"),
                "raw_error": details.get("raw_error"),
                "error_stage": details.get("error_stage"),
                "stage_errors": details.get("stage_errors"),
                "market_error": details.get("market_error"),
            }
        )
        now_mono = time.monotonic()
        with self._lock:
            previous = self._market_error_fingerprint
            changed = bool(previous and previous != fingerprint)
            first = not previous
            same = previous == fingerprint
            if first or changed:
                suppressed_previous = self._market_error_suppressed
                self._market_error_fingerprint = fingerprint
                self._market_error_first_monotonic = now_mono
                self._market_error_last_log_monotonic = now_mono
                self._market_error_suppressed = 0
                self._market_error_notification_due = True
                should_log = True
                status = "ERROR"
            elif same and (
                now_mono - self._market_error_last_log_monotonic
                >= _MARKET_ERROR_SUMMARY_SECONDS
            ):
                suppressed_previous = self._market_error_suppressed + 1
                self._market_error_last_log_monotonic = now_mono
                self._market_error_suppressed = 0
                self._market_error_notification_due = True
                should_log = True
                status = "ERROR_SUMMARY"
            else:
                self._market_error_suppressed += 1
                self._market_error_notification_due = False
                return False

        payload = dict(details)
        payload.update(
            {
                "error_fingerprint": fingerprint,
                "error_changed": changed,
                "suppressed_repeats": suppressed_previous,
            }
        )
        self._record_run_flow(
            run_id=run_id,
            stage="MARKET_SCANNER",
            status=status,
            details=payload,
            duration_ms=duration_ms,
        )
        return should_log

    def _record_market_recovery(self, *, run_id: str) -> bool:
        now_mono = time.monotonic()
        with self._lock:
            fingerprint = self._market_error_fingerprint
            if not fingerprint:
                self._market_error_notification_due = False
                return False
            suppressed = self._market_error_suppressed
            first_mono = self._market_error_first_monotonic
            outage_seconds = max(0.0, now_mono - first_mono) if first_mono else 0.0
            self._market_error_fingerprint = ""
            self._market_error_first_monotonic = 0.0
            self._market_error_last_log_monotonic = 0.0
            self._market_error_suppressed = 0
            self._market_error_notification_due = True
        self._record_run_flow(
            run_id=run_id,
            stage="MARKET_SCANNER",
            status="RECOVERED",
            details={
                "recovered_error_fingerprint": fingerprint,
                "suppressed_repeats": suppressed,
                "outage_seconds": round(outage_seconds, 3),
            },
        )
        return True

    def market_error_notification_due(self) -> bool:
        with self._lock:
            return bool(self._market_error_notification_due)

    def _restore_order_registry(self) -> None:
        self.journal.restore_daily_report()
        with self._lock:
            if self._order_registry_restored:
                return
            restore = getattr(
                self.modules["trading"], "restore_system_order_registry", None
            )
            if callable(restore):
                restore(self.journal.load_order_registry())
            self._order_registry_restored = True

    def _checkpoint_order_registry(self) -> None:
        get_registry = getattr(
            self.modules["trading"], "get_system_order_registry", None
        )
        if callable(get_registry):
            try:
                self.journal.save_order_registry(get_registry())
                self._checkpoint_state(force=True)
            except Exception as exc:
                self.journal._last_write_error = str(exc)
                self._recovery_state = "JOURNAL_ERROR"
                raise

    def input_token(self, symbol="", market=""):
        with self._state_transaction:
            self._snapshot_seq += 1
            quote = self.quote(symbol, market) if symbol else {}
            return {
                "input_snapshot_id": f"{self._boot_id}:{self._snapshot_seq}",
                "config_id": _stable_hash({"settings": _settings_mapping(self.settings),
                    "parameters": self.registry.snapshot() if hasattr(self.registry, "snapshot") else {}}),
                "position_version": self.positions._generation,
                "risk_version": self._account_generation,
                "market_seq": self.market_scanner._generation,
                "quote_seq": _quote_sequence_value(quote),
                "created_monotonic": time.monotonic(),
                "session_id": self._session_id, "trade_date": self._session_trade_date_int(),
            }

    def result_valid(self, token, symbol="", market=""):
        """輸入身分、TTL 與交易日必須一致；新行情／Market generation 不等於過期。"""
        try:
            if not isinstance(token, Mapping):
                return False
            current = self.input_token(symbol, market)
            scanner = token.get("purpose") == "SCANNER"
            ttl = float(_parameter(self.registry, "scanner").scanner_round_time_limit_seconds if scanner
                        else _setting(self.settings, "result_expiry_seconds", 5.0))
            age = time.monotonic() - float(token.get("created_monotonic", 0))
            keys = ("config_id", "session_id", "trade_date") if scanner else (
                "config_id", "session_id", "trade_date", "position_version", "risk_version")
            return (str(token.get("input_snapshot_id", "")).startswith(self._boot_id + ":")
                    and 0 <= age <= ttl and all(token.get(k) == current[k] for k in keys))
        except (ValueError, TypeError, OverflowError):
            return False

    def candidate_snapshot_valid(self, snapshot):
        if not isinstance(snapshot, Mapping) or not snapshot.get("snapshot_id"):
            return False
        try:
            age = time.monotonic() - float(snapshot["created_monotonic"])
            return (0 <= age <= float(snapshot["ttl_seconds"])
                    and snapshot.get("session_id") == self._session_id
                    and snapshot.get("trade_date") == self._session_trade_date_int())
        except (KeyError, TypeError, ValueError):
            return False

    def safety_status(self):
        def oldest_age(q):
            with q.mutex:
                rows = list(q.queue)
            values = [time.monotonic() - float(row["received_monotonic"])
                      for row in rows if isinstance(row, Mapping) and row.get("received_monotonic")]
            return max(values) if values else None
        return {"worker_faults": dict(self._worker_faults),
                "worker_buy_blocked": self._worker_buy_blocked,
                "ingress_failed": self._ingress_failed,
                "raw_trade_queue_depth": self._raw_trade_events.qsize(),
                "recovery": self._recovery_state, "kill_switch": self._kill_switch,
                "oldest_callback_age": oldest_age(self._cts_callback_queue),
                "oldest_market_event_age": oldest_age(self._trade_event_queue),
                "log_queue_depth": self.journal._log_queue.qsize(),
                "daily_loss_lock": self._daily_loss_lock,
                "journal_error": self.journal._last_write_error,
                "state_owners": dict(STATE_OWNERS), "session_id": self._session_id,
                "execution_heartbeat_age": (time.monotonic() - self._execution_heartbeat
                    if self._execution_heartbeat else None),
                "callback_queue_depth": self._cts_callback_queue.qsize(),
                "market_queue_depth": self._trade_event_queue.qsize()}

    def _checkpoint_state(self, *, force=False):
        if not force and time.monotonic() - self._last_state_checkpoint < 5.0:
            return
        payload = {"session_id": self._session_id, "kill_switch": self._kill_switch,
                   "daily_loss_lock": self._daily_loss_lock,
                   "unsent_exits": list(self._unsent_exits.values()),
                   "positions": list(self.positions.snapshot()["positions"].values()),
                   "orders": self.modules["trading"].get_system_order_registry(),
                   "strategy": self._capture_strategy_checkpoint() if not getattr(self, "_ingress_failed", False) else {}}
        self.journal.save_runtime_state(payload)
        self._last_state_checkpoint = time.monotonic()


    def _capture_strategy_checkpoint(self):
        intraday = self.modules["intraday"]
        # 一分鐘保存完整監控 K 棒；委託鎖由獨立 Order Registry 保存。
        if time.monotonic()-self._strategy_checkpoint_at >= 60:
            watch = set(self.positions.keys()) | {_symbol_key(r.get("symbol"),r.get("market")) for r in _candidate_rows(self.market_scanner.candidates_context)}
            with self._lock:
                buffers = [{"symbol":k[0],"market":k[1],"buckets":list(b.buckets.values()),
                    "tail":list(b),"digest":b.digest,"count":b.total_count,"first":b.first_quote,"all_sizes":b.all_sizes}
                    for k,b in self._market_event_buffers.items() if k in watch]
            self._strategy_checkpoint = {"date":_now().strftime("%Y%m%d"),
                "parameters":_stable_hash(_parameter(self.registry,"intraday").snapshot()),"buffers":buffers}
            self._strategy_checkpoint_at = time.monotonic()
        return dict(self._strategy_checkpoint)

    def _restore_strategy_checkpoint(self, saved, trade_date):
        state = saved.get("strategy") or {}
        intraday = self.modules["intraday"]
        params = _parameter(self.registry,"intraday")
        if state.get("date") != str(trade_date):
            return
        compatible = state.get("parameters") == _stable_hash(params.snapshot())
        for row in state.get("buffers") or []:
            key = (row["symbol"], row["market"])
            buffer = intraday.IncrementalEventBuffer(params)
            buffer.extend(intraday.normalize_quote_sequence(row.get("tail") or [],parameters=params))
            buffer.buckets = {intraday._parse_timestamp(b["start"]):dict(b) for b in row.get("buckets") or []}
            buffer.digest,buffer.total_count = row.get("digest",""),row.get("count",0)
            buffer.first_quote = (intraday.normalize_quote_sequence([row["first"]],parameters=params)[0] if row.get("first") else None)
            buffer.all_sizes = row.get("all_sizes",False)
            if buffer and key not in self._market_event_buffers:
                self._market_event_buffers[key] = buffer
                last = buffer[-1]["timestamp_dt"]
                self._strategy_restore_required[key] = _now_text()

    def _resolve_position_market(self, symbol: str) -> str:
        market = self.market_scanner.market_for_symbol(symbol)
        if market:
            return market
        quote = self.quote(symbol, "")
        if not quote and self.gateway is not None:
            quote = self.gateway.get_quote(symbol) or {}
        return _normalize_market(quote.get("market"))

    def _session_trade_date_int(self) -> Optional[int]:
        # 委託日期與資料來源日期分開；不能把前日行情日期送入今日委託。
        return int(_now().strftime("%Y%m%d"))

    def _position_query_date(self) -> int:
        # 盤前先查本地今日；非交易日或券商尚未切日由查詢失敗再查最近資料日期。
        return self._session_trade_date_int()

    @staticmethod
    def _cts_query_summary(response: Any) -> Dict[str, Any]:
        if not isinstance(response, Mapping):
            return {
                "ok": False,
                "status": "INVALID_RESPONSE",
                "record_count": 0,
                "raw_error": "CTS query response is not Mapping",
            }
        parsed_rows = _response_rows(response)
        normalized_rows = _response_rows(response, normalized_first=True)
        ok = response.get("ok") is True
        try:
            record_count = int(response.get("record_count"))
        except (TypeError, ValueError):
            record_count = len(parsed_rows)
        return {
            "ok": ok,
            "status": str(response.get("status") or ("SUCCESS" if ok else "ERROR")),
            "record_count": record_count,
            "parsed_count": len(parsed_rows),
            "normalized_count": len(normalized_rows),
            "raw_error": str(response.get("raw_error") or response.get("error") or ""),
            "query_trade_date": response.get("query_trade_date"),
            "query_attempts": copy.deepcopy(response.get("query_attempts") or []),
        }

    def _should_record_trading_cycle(
        self,
        *,
        status: str,
        details: Mapping[str, Any],
        reconcile: Mapping[str, Any],
    ) -> Tuple[bool, str]:
        now_mono = time.monotonic()
        actual_event = bool(
            int(details.get("executions") or 0) or bool(reconcile.get("changed"))
        )
        effective_reconcile = dict(reconcile)
        if str(reconcile.get("reason") or "") == "THROTTLED":
            with self._lock:
                previous_reconcile = copy.deepcopy(self._last_cts_reconcile)
            if previous_reconcile:
                effective_reconcile = previous_reconcile
            else:
                effective_reconcile = {
                    "reason": "IDLE",
                    "reconciled": False,
                    "query_ok": None,
                }
        state_fingerprint = _stable_hash(
            {
                "status": status,
                "cts_reason": effective_reconcile.get("reason"),
                "cts_reconciled": bool(effective_reconcile.get("query_ok")),
                "cts_query_ok": effective_reconcile.get("query_ok"),
                "strategy_enabled": self.strategy_enabled,
                "pause_blocks_buy": self.pause_blocks_buy,
            }
        )
        with self._lock:
            state_changed = (
                state_fingerprint != self._last_trading_cycle_state_fingerprint
            )
            heartbeat_due = (
                self._last_trading_cycle_log_monotonic <= 0.0
                or now_mono - self._last_trading_cycle_log_monotonic
                >= _TRADING_CYCLE_HEARTBEAT_SECONDS
            )
            if actual_event:
                reason = "EVENT"
            elif state_changed:
                reason = "STATE_CHANGE"
            elif heartbeat_due:
                reason = "HEARTBEAT"
            else:
                return False, "SUPPRESSED_IDLE"
            self._last_trading_cycle_state_fingerprint = state_fingerprint
            self._last_trading_cycle_log_monotonic = now_mono
        return True, reason

    def run_market_scanner_cycle(
        self, *, force: bool = False, reason: str = "INTERVAL"
    ) -> Dict[str, Any]:
        run_id = self._new_run_id("MARKET")
        started = time.perf_counter()
        self._record_run_flow(
            run_id=run_id,
            stage="MARKET_SCANNER",
            status="START",
            details={"force": bool(force), "reason": str(reason)},
        )
        try:
            result = self.market_scanner.refresh(self, force=force, reason=reason)
        except Exception as exc:
            if not callable(getattr(self.modules["market"], "read_market_permission_context", None)):
                with self.market_scanner._lock:
                    self.market_scanner._market_context.update(allow_long=False, realtime_buy_data_ready=False)
            duration = (time.perf_counter() - started) * 1000.0
            details = self._market_failure_details(exc)
            self._record_market_failure(
                run_id=run_id,
                details=details,
                duration_ms=duration,
            )
            raise
        duration = (time.perf_counter() - started) * 1000.0
        if result.get("ok"):
            self._record_market_recovery(run_id=run_id)
        self._record_run_flow(
            run_id=run_id,
            stage="MARKET_SCANNER",
            status="OK" if result.get("ok") else "DEGRADED",
            details=result,
            duration_ms=duration,
        )
        return result

    def _clock_exit_due(self, now=None):
        now = now or _now()
        params = _parameter(self.registry, "intraday")
        exit_clock = datetime.strptime(params.exit_time, "%H:%M:%S").time()
        local = now.astimezone(timezone(timedelta(hours=8)))
        return bool(self.strategy_enabled and local.weekday() < 5 and local.time() >= exit_clock)

    def _maintain_pending_sells(self):
        # 與交易週期同一序列；未知委託只對帳，不重送。
        if time.monotonic()-self._last_sell_maintenance < 2:
            return []
        self._last_sell_maintenance = time.monotonic()
        status = self._cts_status()
        if (not self.strategy_enabled or not status.get("connected") or
            str(_setting(self.settings, "control_mode", "MANUAL")).upper() != "AUTO" or
            not _setting(self.settings, "auto_execute", False)):
            return []
        protected = [symbol for symbol,market in self.positions.keys() if self.positions.is_manual_takeover(symbol,market)]
        events = self.modules["trading"].maintain_time_exit_orders(self._ensure_gateway(),
            account_id=str(status.get("account_id") or ""), trade_date=self._session_trade_date_int(),
            parameters=_parameter(self.registry,"trading"), protected_symbols=protected, cancel_buys=False)
        if events:
            self._account_generation += 1
            self._last_cts_reconcile["query_ok"] = False
            self.journal.record_execution({"status": "EXIT_PENDING_MAINTENANCE", "events": events})
        if not self._last_cts_reconcile.get("query_ok"):
            return events
        for key,signal in list(self._unsent_exits.items()):
            pos = self.positions.get(*key) or {}
            if not pos.get("managed_quantity"):
                self._unsent_exits.pop(key, None)
                continue
            if key[0] not in protected:
                events.append(self.execution.process_signal(self, signal))
        return events

    def _run_clock_exits(self):
        if not self._clock_exit_due() or time.monotonic()-self._last_clock_exit < 2.0:
            return []
        self._last_clock_exit = time.monotonic()
        results = []
        automatic = str(_setting(self.settings, "control_mode", "MANUAL")).upper() == "AUTO" and bool(_setting(self.settings, "auto_execute", False))
        status = self._cts_status()
        if not automatic or not status.get("connected"):
            return results
        if time.monotonic()-self._last_clock_orders >= 10:
            self._last_clock_orders = time.monotonic()
            maintain = getattr(self.modules["trading"], "maintain_time_exit_orders", None)
            if callable(maintain):
                protected = [symbol for symbol, market in self.positions.keys() if self.positions.is_manual_takeover(symbol, market)]
                events = maintain(self._ensure_gateway(), account_id=str(status.get("account_id") or ""),
                    trade_date=self._session_trade_date_int(), parameters=_parameter(self.registry, "trading"),
                    protected_symbols=protected)
                if events:
                    self._account_generation += 1
                    self._last_cts_reconcile["query_ok"] = False
                    self.journal.record_execution({"trigger": "CLOCK", "status": "TIME_EXIT_MAINTENANCE", "events": events})
                    # 等待委託完整對帳；禁止使用舊可賣數量重送。
                    return events
        for symbol, market in self.positions.keys():
            position = self.positions.get(symbol, market) or {}
            if position.get("managed_by") != "MOMENTUM_SCALPING" or int(position.get("managed_quantity") or 0) <= 0:
                continue
            signal = {"symbol": symbol, "market": market, "entry_signal": False,
                      "exit_signal": True, "exit_stage": "TIME_EXIT", "exit_fraction": 1.0,
                      "signal_price": (self.quote(symbol, market) or {}).get("price"),
                      "strategy_type": position.get("strategy_type", "UNKNOWN"),
                      "asof": _now_text(), "signal_fingerprint": ""}
            # execution 仍檢查 AUTO、帳戶歸屬、接管狀態、可賣數量及未結委託。
            results.append(self.execution.process_signal(self, signal))
        return results

    def _maintain_memory(self):
        now_mono = time.monotonic()
        if now_mono-self._last_maintenance < 60:
            return
        self._last_maintenance = now_mono
        watch = set(self.positions.keys()) | {
            _symbol_key(r.get("symbol"), r.get("market"))
            for r in _candidate_rows(self.market_scanner.candidates_context)}
        today = _now().date()
        removed = 0
        with self._lock:
            for key, buffer in list(self._market_event_buffers.items()):
                last = buffer[-1].get("timestamp_dt") if buffer else None
                if key not in watch and (last is None or last.date() != today or
                        (_now()-last).total_seconds() > 600):
                    self._market_event_buffers.pop(key, None)
                    self._event_history.pop(key, None)
                    self._last_quote_sequences.pop(key, None)
                    self._quotes.pop(key, None)
                    removed += 1
            for key, quote in list(self._quotes.items()):
                source = quote.get("realtime_source_at") or quote.get("quote_source_at") or quote.get("timestamp")
                age = _source_age_seconds(source)
                if key not in watch and age is not None and age > 600:
                    self._quotes.pop(key, None)
            for key, rows in self._event_history.items():
                if len(rows) > 2048:
                    del rows[:-2048]
        module = self.modules["intraday"]
        cleanup = getattr(module, "prune_calculation_cache", None)
        if callable(cleanup):
            cleanup()
        self._latency_metrics["memory_cleanup_removed_symbols"] = removed

    def run_trading_cycle(self, *, force: bool = False) -> Dict[str, Any]:
        self._execution_heartbeat = time.monotonic()
        run_id = self._new_run_id("TRADING")
        started = time.perf_counter()
        with self._lock:
            pending_settings = self._pending_position_settings
            self._pending_position_settings = None
        if pending_settings is not None:
            self.positions.update_settings(pending_settings)
        self._maintain_memory()
        for _ in range(min(10,self._rest_emergency_queue.qsize())):
            signal = self._rest_emergency_queue.get_nowait()
            self._merge_quotes([signal["sell_fallback_quote"]])
            self.execution.process_signal(self,signal)
        callbacks_seen = False
        for _ in range(min(self._cts_callback_queue.qsize(), 256)):
            try:
                callback = self._cts_callback_queue.get_nowait()
                if callback.get("execution_completion"):
                    trading = self.modules["trading"]
                    with self._state_transaction:
                        records = trading.get_system_order_registry()
                        record = next((r for r in records if r.get("registry_key") == callback.get("registry_key")), None)
                        if (record and record.get("generation") == callback.get("generation")
                                and record.get("order_intent_id") == callback.get("order_intent_id")):
                            proven = bool(callback.get("ok") and (callback.get("order_id") or callback.get("order_no")))
                            trading._update_system_order_identity(record["registry_key"],
                                order_id=callback.get("order_id"), order_no=callback.get("order_no"),
                                status="PRE_SUBMIT_REJECTED" if callback.get("not_submitted") else "REJECTED" if callback.get("broker_rejected") else "SUBMITTED" if proven else "STATUS_UNKNOWN")
                callbacks_seen = True
            except queue.Empty:
                break
        # 回呼只通知重新對帳；成交真相仍由 CTS 查詢確認，避免長期堆積回呼。
        reconcile = self._poll_cts_reconcile(force=force or callbacks_seen)
        changed = self._scan_marketstate_events()
        event_keys = self._drain_trade_events()
        for key in changed:
            if key not in event_keys:
                event_keys.append(key)

        updated_signals: List[Dict[str, Any]] = []
        executions: List[Dict[str, Any]] = self._maintain_pending_sells() + self._run_clock_exits()
        should_process_intraday = bool(force or event_keys)
        if (self.strategy_enabled or force) and should_process_intraday:
            updated_signals = self.intraday.process(
                self,
                changed_keys=event_keys if event_keys else None,
                force=force,
            )
            if self.strategy_enabled:
                for signal in sorted(updated_signals, key=lambda row: not bool(row.get("exit_signal"))):
                    fingerprint = str(signal.get("signal_fingerprint") or "")
                    has_action = bool(
                        signal.get("entry_signal") or signal.get("exit_signal")
                    )
                    if not has_action:
                        continue
                    if signal.get("exit_signal") and fingerprint and self.positions.signal_processed(fingerprint):
                        continue
                    executions.append(self.execution.process_signal(self, signal))

        self._latency_metrics["latest_strategy_cycle_ms"] = round(
            (time.perf_counter() - started) * 1000, 3
        )
        status = "OK"
        if any(not bool(item.get("ok", True)) for item in executions):
            status = "DEGRADED"
        if reconcile.get("reason") == "CTS_QUERY_FAILED":
            status = "DEGRADED"

        duration = (time.perf_counter() - started) * 1000.0
        details = {
            "changed_quotes": len(event_keys),
            "updated_signals": len(updated_signals),
            "executions": len(executions),
            "execution_attempt": len(executions),
            "executions_meaning": "流程嘗試次數，非券商受理數",
            **{name: sum(bool(item.get(name)) for item in executions) for name in
               ("broker_submit_attempt", "broker_accepted", "broker_rejected", "broker_unknown")},
            "broker_audit_totals": dict(getattr(self, "_broker_audit_counts", {})),
            "cts_reconciled": bool(reconcile.get("reconciled")),
            "cts_changed": bool(reconcile.get("changed")),
            "cts_reason": str(reconcile.get("reason") or ""),
            "cts_query_status": reconcile.get("query_status", {}),
            "event_queue_depth": self._trade_event_queue.qsize(),
            "cycle_ms": round((time.perf_counter() - started) * 1000, 3),
        }
        should_log, log_reason = self._should_record_trading_cycle(
            status=status,
            details=details,
            reconcile=reconcile,
        )
        if should_log:
            self._record_run_flow(
                run_id=run_id,
                stage="TRADING_CYCLE",
                status=status,
                details={**details, "log_reason": log_reason},
                duration_ms=duration,
            )

        return {
            "ok": status == "OK",
            "changed_quotes": len(event_keys),
            "updated_signals": len(updated_signals),
            "executions": executions,
            "cts": reconcile,
            "run_flow_logged": should_log,
            "run_flow_log_reason": log_reason,
        }

    def _query_cts_snapshot(self, account_id):
        generation = self._account_generation
        cts = self.gateway.cts
        if cts.status().get("connection_health") in {"DISCONNECTED", "DEGRADED"} and callable(getattr(cts,"recover_connection",None)):
            cts.recover_connection()

        def query(fn):
            try:
                response = fn()
                if not isinstance(response, Mapping):
                    raise ValueError("CTS_QUERY_RESPONSE_INVALID")
                return response
            except Exception as exc:
                return {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                    "parsed": [],
                }

        day = self._position_query_date()
        position = dict(
            query(lambda: cts.query_position(account_id, day, force_query=True)),
            query_trade_date=day,
        )
        attempts = [
            {
                "trade_date": day,
                "ok": bool(position.get("ok")),
                "error": str(position.get("raw_error") or position.get("error") or ""),
            }
        ]

        if not position.get("ok"):
            reference = str(
                self.market_scanner.market_context.get("session_trade_date") or ""
            ).replace("-", "")
            # 日期 fallback 只查詢庫存，不授權買賣。
            if len(reference) == 8 and reference.isdigit() and int(reference) != day:
                fallback = query(
                    lambda: cts.query_position(
                        account_id, int(reference), force_query=True
                    )
                )
                attempts.append(
                    {
                        "trade_date": int(reference),
                        "ok": bool(fallback.get("ok")),
                        "error": str(
                            fallback.get("raw_error") or fallback.get("error") or ""
                        ),
                    }
                )
                if fallback.get("ok"):
                    position = dict(fallback, query_trade_date=int(reference))
        position["query_attempts"] = attempts
        return (
            account_id,
            self._session_trade_date_int(),
            position,
            query(lambda: cts.query_order(account_id, force_query=True)),
            query(lambda: cts.query_match(account_id, force_query=True)),
            generation,
        )

    def _poll_cts_reconcile(self, force=False):
        gateway = self._ensure_gateway()
        if not getattr(gateway.cts, "async_queries_supported", False):
            return self.reconcile_cts(force=force)
        future = self._cts_query_future
        if future is not None and future.done():
            self._cts_query_future = None
            try:
                responses = future.result()
            except Exception as exc:
                self.positions.mark_position_stale(exc)
                self._last_cts_reconcile = dict(self._last_cts_reconcile, query_ok=False,
                    reconciled=False, reason="CTS_QUERY_FAILED", position_stale=True, error=str(exc))
                return dict(self._last_cts_reconcile)
            return self.reconcile_cts(force=True, _responses=responses)
        last = dict(self._last_cts_reconcile)
        if future is None and (
            force
            or time.monotonic() - self._last_cts_reconcile_monotonic
            >= _CTS_RECONCILE_MIN_INTERVAL_SECONDS
        ):
            status = self._cts_status()
            account = str(status.get("account_id") or "")
            if (status.get("connected") or status.get("recovery_available")) and account:
                self._last_cts_reconcile_monotonic = time.monotonic()
                self._cts_query_future = self._cts_query_pool.submit(
                    self._query_cts_snapshot, account
                )
        return {
            **last,
            "changed": False,
            "reconciled": False,
            "reason": last.get("reason", "CTS_QUERY_PENDING"),
            "query_pending": self._cts_query_future is not None,
        }

    @_serialized_state
    def reconcile_cts(self, *, force: bool = False, _responses=None) -> Dict[str, Any]:
        now_mono = time.monotonic()
        if (
            not force
            and now_mono - self._last_cts_reconcile_monotonic
            < _CTS_RECONCILE_MIN_INTERVAL_SECONDS
        ):
            return {
                **self._last_cts_reconcile,
                "reconciled": False,
                "changed": False,
                "throttled": True,
                "reason": self._last_cts_reconcile.get("reason", "CTS_QUERY_PENDING"),
            }
        self._last_cts_reconcile_monotonic = now_mono

        previous_position_keys = set(self.positions.keys())
        cts_status = self._cts_status()
        if not bool(cts_status.get("connected")):
            self.positions.mark_position_stale("CTS_NOT_CONNECTED")
            return {
                "reconciled": False,
                "changed": False,
                "reason": "CTS_NOT_CONNECTED",
            }
        account_id = str(
            cts_status.get("account_id")
            or _setting(self.settings, "cts_account_id", "")
            or ""
        ).strip()
        if not account_id:
            return {
                "reconciled": False,
                "changed": False,
                "reason": "CTS_ACCOUNT_MISSING",
            }

        session_trade_date = self._session_trade_date_int()
        trading = self.modules["trading"]
        gateway = self._ensure_gateway()
        cts = gateway.cts
        responses = (
            _responses
            if _responses is not None
            else self._query_cts_snapshot(account_id)
        )
        if len(responses) == 6:
            if responses[5] != self._account_generation:
                return {
                    "reconciled": False,
                    "changed": False,
                    "reason": "CTS_RESPONSE_BEFORE_EXECUTION",
                    "query_ok": False,
                }
            responses = responses[:5]
        (
            response_account,
            response_day,
            position_response,
            order_response,
            match_response,
        ) = responses
        if response_account != account_id or response_day != session_trade_date:
            return {
                "reconciled": False,
                "changed": False,
                "reason": "CTS_RESPONSE_SESSION_CHANGED",
            }

        query_status = {
            "position": self._cts_query_summary(position_response),
            "order": self._cts_query_summary(order_response),
            "match": self._cts_query_summary(match_response),
        }
        if not query_status["position"]["ok"]:
            self.positions.mark_position_stale(query_status["position"].get("raw_error"))
        query_ok = all(bool(item.get("ok")) for item in query_status.values())
        if not query_ok:
            for order in trading.get_system_order_registry():
                if order.get("order_state") in {"REQUEST_SENT","SUBMITTING","STATUS_UNKNOWN"}:
                    attempts = int(order.get("unknown_query_attempts") or 0)+1
                    trading._update_system_order_identity(order["registry_key"],unknown_query_attempts=attempts,
                        recovery_phase="MANUAL_ATTENTION" if attempts>=3 else "QUERY_PENDING")
        if not self._journal_loaded:
            if not query_ok:
                self._recovery_state = "RECOVERY"
                return {"query_ok": False, "reason": "RECOVERY_QUERY_REQUIRED"}
            saved = self.journal.load_runtime_state()
            self._kill_switch = bool(saved.get("kill_switch"))
            self._daily_loss_lock = bool(saved.get("daily_loss_lock"))
            self._session_id = str(saved.get("session_id") or session_trade_date or "")
            self.modules["trading"].restore_system_order_registry(saved.get("orders", []))
            self._restore_order_registry()
            self._restore_strategy_checkpoint(saved, session_trade_date)
            self._unsent_exits = {_symbol_key(x.get("symbol"),x.get("market")): x for x in saved.get("unsent_exits", []) if str(x.get("asof") or "").replace("-", "").startswith(str(session_trade_date))}
            self._journal_loaded = True

        position_rows: List[Dict[str, Any]] = []
        if query_status["position"]["ok"]:
            try:
                position_rows = trading.parse_cts_positions(_response_rows(position_response))
            except Exception as exc:
                query_ok = False
                query_status["position"].update(ok=False, status="POSITION_PARSE_FAILED", raw_error=str(exc))
                self.positions.mark_position_stale(exc)

        open_orders: List[Dict[str, Any]] = []
        if query_status["order"]["ok"]:
            reconcile_orders = getattr(trading, "reconcile_system_order_states", None)
            if callable(reconcile_orders):
                identity_reports = reconcile_orders(
                    _response_rows(order_response) if query_ok else [],
                    account_id=account_id,
                    trade_date=session_trade_date,
                )
                identity_key = _stable_hash(identity_reports)
                if identity_reports and identity_key != getattr(self, "_identity_report_key", None):
                    self._identity_report_key, self._identity_report_at = identity_key, time.monotonic()
                    self.journal.record_execution({"status": "ORDER_IDENTITY_RECONCILIATION",
                        "identity_recovery": identity_reports, "query_cross_check": query_status})
            for row in _response_rows(order_response):
                try:
                    state = trading.parse_cts_order_state([row])
                except Exception:
                    continue
                if state.get("found") and state.get("active"):
                    open_orders.append(dict(state))
            self.positions.set_open_orders(open_orders)

        if query_ok:
            confirm_absence = getattr(trading, "confirm_unknown_order_absence", None)
            if callable(confirm_absence):
                released = confirm_absence(_response_rows(order_response),
                    _response_rows(match_response, normalized_first=True), position_rows,
                    account_id=account_id, trade_date=session_trade_date, cts=cts,
                    queries_complete=True)
                for report in released:
                    self.record_order_event("ORDER_RECONCILIATION", report["status"], report)
            for record in trading.get_system_order_registry():
                if record.get("account_id") == account_id and str(record.get("trade_date")) == str(session_trade_date):
                    self.record_order_event("ORDER_RECONCILIATION", record.get("order_state", "STATUS_UNKNOWN"),
                        {"order_intent_id": record.get("order_intent_id"), "symbol": record.get("symbol"),
                         "order_id": record.get("order_id"), "order_no": record.get("order_no"),
                         "query_status": query_status,
                         "queue_evidence": "ORDER_FOUND" if any(str(row.get("F15") or "") == record.get("order_id")
                           and record.get("order_id") for row in _response_rows(order_response)) else "NO_ORDER_EVIDENCE"})

        matches: List[Dict[str, Any]] = []
        if query_status["match"]["ok"]:
            matches = _response_rows(match_response, normalized_first=True)
            for match in matches:
                try:
                    ownership = trading.resolve_cts_match_ownership(match)
                except Exception:
                    ownership = {"owner": "UNKNOWN"}
                fingerprint = _stable_hash(
                    {
                        "symbol": match.get("symbol") or match.get("F3"),
                        "side": match.get("side") or match.get("F4"),
                        "price": match.get("price") or match.get("F6"),
                        "quantity": match.get("filled_shares") or match.get("F7"),
                        "order_no": match.get("order_no") or match.get("F9"),
                        "order_id": match.get("order_id") or match.get("F14"),
                        "match_id": match.get("match_id") or match.get("F15"),
                        "match_date": match.get("match_date") or match.get("F10"),
                        "match_time": match.get("match_time") or match.get("F11"),
                    }
                )
                raw_side = (
                    match.get("side")
                    if match.get("side") is not None
                    else match.get("F4")
                )
                action = (
                    "BUY"
                    if str(raw_side) == "1"
                    else "SELL" if str(raw_side) == "2" else ""
                )
                symbol = str(match.get("symbol") or match.get("F3") or "").strip()
                self.journal.record_daily_trade(
                    {
                        "source": "CTS_QUERY_MATCH",
                        "trade_date": match.get("match_date") or match.get("F10"),
                        "time": match.get("match_time")
                        or match.get("F20")
                        or match.get("F11"),
                        "symbol": symbol,
                        "market": self.market_scanner.market_for_symbol(symbol),
                        "action": action,
                        "price": match.get("price") or match.get("F6"),
                        "quantity": match.get("filled_shares") or match.get("F7"),
                        "order_no": match.get("order_no") or match.get("F9"),
                        "order_id": match.get("order_id") or match.get("F14"),
                        "trigger": "CTS_QUERY_MATCH",
                        "match": match,
                        "ownership": ownership,
                        "return_measurement_status": "CALCULATED_IN_DAILY_METRICS",
                    },
                    fingerprint=fingerprint,
                )

        if query_status["position"]["ok"] and query_status["match"]["ok"]:
            reconcile_positions = getattr(trading, "reconcile_managed_positions", None)
            if callable(reconcile_positions):
                try:
                    managed = reconcile_positions(
                        position_rows, matches, account_id=account_id,
                        trade_date=session_trade_date,
                        parameters=_parameter(self.registry, "trading"))
                except Exception as exc:
                    # 成交對帳異常也不可讓 GUI 失去既有持倉；下一輪強制再查。
                    self.positions.mark_position_stale(exc)
                    query_status["match"].update(ok=False, status="MATCH_RECONCILIATION_FAILED", raw_error=str(exc))
                    self._last_cts_reconcile = {"reconciled": False, "query_ok": False,
                        "reason": "CTS_QUERY_FAILED", "query_status": query_status,
                        "position_count": len(self.positions.keys()), "position_stale": True}
                    self.journal.record_execution(dict(self._last_cts_reconcile, error=str(exc)))
                    return dict(self._last_cts_reconcile)
                position_rows = managed["positions"]
                if managed.get("consistent") is False:
                    query_ok = False
                    query_status["position"]["reconciliation_status"] = "POSITION_MATCH_MISMATCH"
                    self.journal.record_execution({
                        "status": "POSITION_MATCH_MISMATCH",
                        "position_discrepancies": managed.get("position_discrepancies", []),
                    })
                managed_metrics = self.journal.managed_daily_metrics()
                # 帳本須能完整配對同一批系統成交；尚未同步時不使用不完整淨損益。
                gross_matches = (
                    managed_metrics["unknown_sell_rows"] == 0
                    and abs(
                        float(managed_metrics["daily_gross_pnl"])
                        - float(managed["realized_gross_pnl"])
                    )
                    < 0.01
                )
                managed_pnl = (
                    managed_metrics["daily_realized_pnl"]
                    if gross_matches
                    else managed["daily_pnl"]
                )
                with self.positions._lock:
                    self.positions._runtime_daily_trade_count = max(
                        int(_setting(self.settings, "daily_trade_count", 0) or 0),
                        int(managed["daily_trade_count"]),
                    )
                    self.positions._risk_state = {
                        k: managed.get(k)
                        for k in (
                            "symbol_entry_counts",
                            "symbol_last_exit",
                            "consecutive_losses",
                            "loss_cooldown_until",
                            "completed_position_count",
                            "strategy_daily_pnl",
                        )
                    }
                    self.positions._runtime_daily_pnl = float(
                        _setting(self.settings, "daily_pnl", 0.0) or 0.0
                    ) + float(managed_pnl)
            self.positions.replace_cts_positions(
                position_rows, market_resolver=self._resolve_position_market
            )
            with self.positions._lock:
                for order in trading.get_system_order_registry():
                    if order.get("account_id") != account_id or not (order.get("order_id") or order.get("order_no")):
                        continue
                    fill = trading.summarize_cts_fills(matches, symbol=order.get("symbol"),
                        side=order.get("side"), order_id=order.get("order_id") or None,
                        order_no=order.get("order_no") or None, trade_date=str(session_trade_date))
                    identity = order["registry_key"]
                    self.positions._applied_execution_fills[identity] = int(fill["filled_shares"])
                    self.positions._applied_execution_costs[identity] = int(fill["filled_shares"]) * float(fill.get("average_fill_price") or 0)
        elif query_status["position"]["ok"]:
            # CTS 庫存成功即可更新清單；既有成本／管理資訊由同股票合併保留。
            for row in position_rows:
                row["position_reconciliation_pending"] = True
            self.positions.replace_cts_positions(
                position_rows, market_resolver=self._resolve_position_market
            )

        if set(self.positions.keys()) != previous_position_keys:
            self._refresh_book_targets()
            self._subscription_refresh_needed.set()
        reconcile_fingerprint = _stable_hash(
            {
                "account_id": account_id,
                "session_trade_date": session_trade_date,
                "query_status": {
                    key: {
                        k: v
                        for k, v in value.items()
                        if k not in {"raw_error", "query_attempts"}
                    }
                    for key, value in query_status.items()
                },
                "positions": position_rows if query_status["position"]["ok"] else None,
                "open_orders": open_orders if query_status["order"]["ok"] else None,
                "matches": matches if query_status["match"]["ok"] else None,
                "order_states": [{k: row.get(k) for k in ("registry_key", "order_state", "order_id", "order_no", "filled_shares", "absence_first_confirmed_at")}
                                 for row in trading.get_system_order_registry()],
            }
        )
        with self._lock:
            changed = reconcile_fingerprint != self._last_cts_reconcile_fingerprint
            self._last_cts_reconcile_fingerprint = reconcile_fingerprint

        if changed:
            self._checkpoint_order_registry()
        unresolved = [o for o in trading.get_system_order_registry()
                      if o.get("order_state") in {"REQUEST_SENT", "SUBMITTING", "STATUS_UNKNOWN", "CANCEL_PENDING"}]
        for order in sorted(unresolved, key=lambda row: row.get("action") != "SELL"):
            if order.get("recovery_phase") == "MANUAL_ATTENTION":
                self._record_run_flow(run_id=self._new_run_id("UNKNOWN"),stage="ORDER_MANUAL_ATTENTION",status="ERROR",
                    details={"symbol":order.get("symbol"),"action":order.get("action"),"registry_key":order.get("registry_key")})
        if query_ok and not unresolved and not self.journal._last_write_error:
            new_session = str(session_trade_date or "")
            if new_session and new_session != self._session_id:
                if self.positions.keys():
                    self._recovery_state = "CARRY_POSITION_REVIEW"
                else:
                    self._session_id = new_session
                    self._daily_loss_lock = False
                    self._recovery_state = "READY"
            else:
                self._recovery_state = "READY"
        else:
            self._recovery_state = "RECOVERY"
        params = _parameter(self.registry, "trading")
        account = self.positions.account_state()
        loss_limits = [value for value in (float(params.max_daily_loss_amount),
                       float(account.get("equity") or 0) * params.max_daily_loss_pct / 100)
                       if value > 0]
        loss_limit = min(loss_limits) if loss_limits else 0.0
        if loss_limit > 0 and float(account.get("daily_pnl") or 0) <= -loss_limit:
            self._daily_loss_lock = True
        marker = getattr(cts, "mark_reconciled", None)
        if callable(marker):
            marker(query_ok and not unresolved)
        if query_ok:
            self._checkpoint_state()
        result = {
            "reconciled": query_ok,
            "changed": changed,
            "reason": "OK" if query_ok else "CTS_QUERY_FAILED",
            "query_ok": query_ok,
            "query_status": query_status,
            "position_count": len(self.positions.keys()),
            "position_stale": self.positions.snapshot()["stale"],
            "open_order_count": len(open_orders),
            "match_count": len(matches),
            "account_id": account_id,
            "session_trade_date": session_trade_date,
            "state_fingerprint": reconcile_fingerprint,
            "timestamp": _now_text(),
        }
        with self._lock:
            self._last_cts_reconcile = copy.deepcopy(result)
        return result


    @_serialized_state
    def manual_trade(self, symbol: str, market: str, action: str, strategy_owner: str = "") -> Dict[str, Any]:
        action = str(action or "").strip().upper()
        symbol = str(symbol or "").strip()
        if action not in {"BUY", "SELL"} or not symbol:
            raise ValueError("請提供股票與 BUY / SELL")
        market = _normalize_market(market) or self.market_scanner.market_for_symbol(symbol)
        trading = self.modules["trading"]
        params = _parameter(self.registry, "trading")
        status = self._cts_status()
        account_id = str(status.get("account_id") or "")
        if not status.get("connected") or not account_id:
            return {"ok": False, "status": "CTS_NOT_CONNECTED"}
        gateway = self._ensure_gateway()
        self._restore_order_registry()
        report = self.reconcile_cts(force=True)
        query_status = report.get("query_status") or {}
        if not all((query_status.get(kind) or {}).get("ok") for kind in ("position", "order")):
            return {"ok": False, "status": "MANUAL_ACCOUNT_QUERY_REQUIRED", "query_status": query_status}
        if query_status["position"].get("query_trade_date") != self._position_query_date():
            return {"ok": False, "status": "MANUAL_CURRENT_POSITION_REQUIRED"}
        orders = self.positions.account_state().get("open_orders") or []
        if any(row.get("symbol") == symbol for row in orders):
            return {"ok": False, "status": "ORDER_PENDING_RECONCILIATION"}
        quote = self.quote(symbol, market)
        if action == "BUY":
            try:
                quote = gateway.get_quote(symbol, market or None) or quote
            except Exception:
                pass
        account = trading.prepare_execution_account_state(self.positions.account_state(),
            trading.get_system_order_registry(), account_id=account_id, trade_date=self._session_trade_date_int())
        decision = trading.evaluate_manual_decision(symbol, market, action, quote, account, parameters=params)
        if decision["decision"] != "ALLOW":
            return {"ok": False, "status": "MANUAL_QUANTITY_REJECTED", "decision": decision}
        signal = {"symbol": symbol, "market": market, "asof": _now_text(),
                  "order_trade_date": self._position_query_date(), "manual_emergency": True,
                  "execution_trigger": "MANUAL", "strategy_type": "MANUAL",
                  "entry_signal": action == "BUY", "exit_signal": action == "SELL",
                  "exit_fraction": 1.0, "exit_reason": "MANUAL_EMERGENCY", "exit_stage": "MANUAL_EMERGENCY"}
        gateway.checkpoint_execution_intent = self._checkpoint_order_registry
        gateway.record_order_event = self.record_order_event
        try:
            execution = trading.execute_allowed_decision(gateway, decision, signal, quote,
                account_id=account_id, parameters=params, confirm_fill=True)
        finally:
            self._checkpoint_order_registry()
        execution = dict(execution, symbol=symbol, market=market, action=action)
        self.positions.apply_execution(execution)
        if execution.get("executed"):
            self._account_generation += 1
            self._last_cts_reconcile["query_ok"] = False
        self._refresh_book_targets()
        self._subscription_refresh_needed.set()
        self.journal.record_execution({"trigger": "MANUAL", "decision": decision, "execution": execution})
        with self.execution._lock:
            self.execution._last_decision = copy.deepcopy(decision)
            self.execution._last_execution = copy.deepcopy(execution)
            self.execution._last_status = str(execution.get("status") or "")
        return execution

    def cancel_all_orders(self) -> Dict[str, Any]:
        cts_status = self._cts_status()
        if not bool(cts_status.get("connected")):
            return {"ok": False, "status": "CTS_NOT_CONNECTED", "cancelled": []}
        account_id = str(cts_status.get("account_id") or "").strip()
        trading = self.modules["trading"]
        request_class = getattr(self.modules["api_gateway"], "CTSCancelRequest")
        gateway = self._ensure_gateway()
        response = gateway.cts.query_order(account_id, force_query=True)
        query_status = self._cts_query_summary(response)
        if not query_status["ok"]:
            return {
                "ok": False,
                "status": "CTS_QUERY_ORDER_FAILED",
                "cancelled": [],
                "query_status": query_status,
            }
        session_trade_date = self._session_trade_date_int()
        cancelled: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        for row in _response_rows(response):
            state = trading.parse_cts_order_state([row])
            if not state.get("found") or not state.get("active"):
                continue
            trade_date = int(state.get("trade_date") or session_trade_date or 0)
            if trade_date <= 0:
                errors.append(
                    {
                        "symbol": state.get("symbol"),
                        "order_id": state.get("order_id"),
                        "error": "SESSION_TRADE_DATE_MISSING",
                    }
                )
                continue
            request = request_class(
                account_id=account_id,
                trade_date=trade_date,
                trade_type=int(state.get("trade_type") or 0),
                order_type=int(state.get("order_type") or 0),
                oid=str(state.get("order_id") or ""),
                order_no=str(state.get("order_no") or ""),
                symbol=str(state.get("symbol") or ""),
                side=int(state.get("side") or 0),
                quantity=0,
                current_quantity=int(state.get("current_quantity") or 0),
                matched_quantity=int(state.get("matched_quantity") or 0),
                pre_order=int(state.get("pre_order") or 0),
                price=str(state.get("price") or ""),
                price_type=int(state.get("price_type") or 0),
                condition=int(state.get("condition") or 0),
            )
            try:
                result = gateway.cts.cancel_order(request)
                cancelled.append(dict(result or {}))
            except Exception as exc:
                errors.append(
                    {
                        "symbol": state.get("symbol"),
                        "order_id": state.get("order_id"),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        return {
            "ok": not errors,
            "status": "DONE",
            "cancelled": cancelled,
            "errors": errors,
        }

    def close_all_positions(self) -> Dict[str, Any]:
        results: List[Dict[str, Any]] = []
        snapshot = self.positions.snapshot().get("positions", {})
        rows = snapshot.values() if isinstance(snapshot, Mapping) else []
        for row in list(rows):
            if not isinstance(row, Mapping):
                continue
            managed_by = str(row.get("managed_by") or "")
            source = str(row.get("management_source") or "")
            if managed_by != "MOMENTUM_SCALPING" and source != "系統今日":
                continue
            results.append(
                self.manual_trade(
                    str(row.get("symbol") or ""),
                    _normalize_market(row.get("market")),
                    "SELL",
                )
            )
        return {
            "ok": all(bool(item.get("ok", True)) for item in results),
            "results": results,
        }

    def execute_command(
        self, command: str, payload: Optional[Mapping[str, Any]] = None
    ) -> Dict[str, Any]:
        name = str(command or "").strip().lower()
        data = dict(payload or {})
        if name == "kill_switch":
            enabled = bool(data.get("enabled", True))
            with self._state_transaction:
                if not enabled and (self._recovery_state != "READY" or self.journal._last_write_error):
                    return {"ok": False, "status": "RECOVERY_REQUIRED"}
                self._kill_switch = enabled
                self._checkpoint_state(force=True)
            return {"ok": True, "status": "BUY_LOCKED" if enabled else "BUY_UNLOCKED"}
        if name == "cts_login":
            self._restore_order_registry()
            gateway = self._ensure_gateway()
            cts = gateway.cts
            prog_id = str(
                data.get("prog_id") or getattr(cts, "com_prog_id", "") or ""
            ).strip()
            if prog_id:
                status = self._cts_status()
                if not bool(status.get("connected")):
                    cts.com_prog_id = prog_id
            result = cts.login(
                str(data.get("trade_das") or ""),
                str(data.get("uid") or ""),
                str(data.get("password") or ""),
                account_id=str(data.get("account_id") or ""),
            )
            output = dict(result or {})
            output["prog_id"] = prog_id
            output["account_source"] = "CTS GetAccountList"
            account_id = str(output.get("account_id") or "").strip()
            if bool(output.get("connected")) and account_id:
                with self._lock:
                    self.gateway_status_cache["cts"] = {
                        "connected": True,
                        "account_id": account_id,
                    }
                    self.gateway_status_cache["cts_connected"] = True
                    self.gateway_status_cache["cts_account_id"] = account_id
                    self.gateway_status_cache["timestamp"] = _now_text()
            output["reconcile"] = self.reconcile_cts(force=True)
            return output
        if name == "cts_logout":
            gateway = self._ensure_gateway()
            result = dict(gateway.cts.logout() or {})
            self._gateway_status(refresh=True)
            return result
        if name == "manual_trade":
            return self.manual_trade(
                str(data.get("symbol") or ""),
                _normalize_market(data.get("market")),
                str(data.get("action") or ""),
                strategy_owner=str(data.get("strategy_owner") or ""),
            )
        if name == "cancel_all":
            return self.cancel_all_orders()
        if name == "manual_takeover":
            return self.positions.mark_manual_takeover(
                str(data.get("symbol") or ""),
                _normalize_market(data.get("market")),
            )
        if name == "close_all":
            return self.close_all_positions()
        raise ValueError(f"不支援的 Runtime command：{command}")

    @staticmethod
    def _daily_report_record_sort_key(
        record: Mapping[str, Any],
    ) -> Tuple[str, str, str, str, str]:
        if not isinstance(record, Mapping):
            return "", "", "", "", ""
        match = record.get("match")
        match = dict(match) if isinstance(match, Mapping) else {}
        trade_date = str(
            record.get("trade_date")
            or record.get("match_date")
            or match.get("match_date")
            or match.get("F10")
            or ""
        )
        trade_time = str(
            record.get("time")
            or record.get("match_time")
            or match.get("match_time")
            or match.get("F20")
            or match.get("F11")
            or record.get("timestamp")
            or ""
        )
        return (
            trade_date,
            trade_time,
            str(record.get("symbol") or match.get("symbol") or match.get("F3") or ""),
            str(record.get("action") or ""),
            str(record.get("fingerprint") or ""),
        )

    def _gateway_status(self, *, refresh: bool = False) -> Dict[str, Any]:
        can_refresh = threading.current_thread().name == "TradingSystemWorker"
        if not refresh or not can_refresh:
            with self._lock:
                return copy.deepcopy(self.gateway_status_cache)

        gateway = self.gateway
        if gateway is None:
            with self._lock:
                return copy.deepcopy(self.gateway_status_cache)
        try:
            status = gateway.status()
            result = dict(status) if isinstance(status, Mapping) else {}
        except Exception as exc:
            with self._lock:
                cached = copy.deepcopy(self.gateway_status_cache)
            cached["error"] = f"{type(exc).__name__}: {exc}"
            return cached
        result.setdefault(
            "mode", str(_setting(self.settings, "api_mode", "LIVE") or "LIVE").upper()
        )
        result.setdefault("market_provider", "Fugle")
        cts = result.get("cts") if isinstance(result.get("cts"), Mapping) else {}
        result["cts_connected"] = bool(
            result.get("cts_connected", cts.get("connected"))
        )
        result["cts_account_id"] = str(
            result.get("cts_account_id") or cts.get("account_id") or ""
        )
        with self._lock:
            self.gateway_status_cache = copy.deepcopy(result)
        return result

    def display_snapshot(self) -> Dict[str, Any]:
        """GUI 只取背景發布的快照，不取得交易資料鎖或呼叫外部 API。"""
        with self._display_lock:
            return dict(self._display_snapshot)

    def publish_display_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        with self._display_lock:
            self._display_snapshot = dict(snapshot)

    def current_strategy_market_context(self, *, now=None):
        current = now or _now()
        module = self.modules["market"]
        reader = getattr(module, "read_market_permission_context", None)
        if not callable(reader):
            context = self.market_scanner.market_context
            resolve = getattr(module, "resolve_strategy_market_context", None)
            return resolve(context, now=current) if callable(resolve) else context
        context = reader(parameters=_parameter(self.registry, "market"),
            mode=str(_setting(self.settings, "control_mode", "AUTO")),
            manual_market_state=_setting(self.settings, "manual_market_state", None),
            manual_trading_mode=_setting(self.settings, "manual_trading_mode", None), now=current)
        # 類股快取獨立於 Scanner 成敗，保留真正來源時間。
        with self._lock:
            cached = getattr(self, "_live_sector_cache", None)
        key = (context.get("market_permission_snapshot_id"), self._session_id,
               _parameter_fingerprint(self.registry, "scanner"), _parameter_fingerprint(self.registry, "market"))
        if cached and cached[0] == key and 0 <= time.monotonic()-cached[1] < 1.0:
            context.update(copy.deepcopy(cached[2]))
            return context
        context.update(sector_snapshots={}, sector_identities={})
        gateway = self.gateway
        if gateway is not None:
            try:
                snapshot = gateway.get_market_state_snapshot(as_of=current)
                with self.market_scanner._lock:
                    universe = copy.deepcopy(self.market_scanner._active_universe)
                rows = module.merge_sector_source_rows(universe, snapshot.get("rows") or [])
                context = module.attach_sector_contract(context, rows, gateway.get_security_master(),
                    parameters=_parameter(self.registry, "market"),
                    source=_parameter(self.registry, "scanner").sector_change_source,
                    reference_at=current.isoformat())
            except Exception as exc:
                context["sector_refresh_error"] = type(exc).__name__ + ": " + str(exc)
        sector = {k: copy.deepcopy(v) for k, v in context.items() if k.startswith("sector_")}
        with self._lock:
            self._live_sector_cache = (key, time.monotonic(), sector)
        return context

    def snapshot(self) -> Dict[str, Any]:
        market_snapshot = self.market_scanner.snapshot()
        market_snapshot["market_context"] = self.current_strategy_market_context()
        position_snapshot = self.positions.snapshot()
        intraday_snapshot = self.intraday.snapshot()
        execution_snapshot = self.execution.snapshot()
        journal_snapshot = self.journal.snapshot()
        with self._lock:
            quotes = copy.deepcopy(self._quotes)
            subscriptions = copy.deepcopy(self._subscriptions)
            strategy_enabled = self._strategy_enabled
            pause_blocks_buy = self._pause_blocks_buy
            reconcile = copy.deepcopy(self._last_cts_reconcile)
        account = position_snapshot["account_state"]
        prepare = getattr(self.modules["trading"], "prepare_execution_account_state", None)
        if callable(prepare):
            account = prepare(account, self.modules["trading"].get_system_order_registry(),
                account_id=str(self._cts_status().get("account_id") or ""),
                trade_date=self._session_trade_date_int())
        risk = self.modules["trading"].portfolio_risk_snapshot(account, _parameter(self.registry, "trading"))
        risk["exit_pending_symbols"] = ",".join(sorted({key[0] for key in self._unsent_exits} | {o["symbol"] for o in self.modules["trading"].get_system_order_registry() if o.get("action")=="SELL" and o.get("order_state") not in {"FILLED","CANCELLED","REJECTED","PRE_SUBMIT_REJECTED","CONFIRMED_ABSENT"}})) or "-"
        risk["reserved_buy_amount"] = account.get("reserved_buy_amount", 0)
        risk["available_cash"] = account.get("available_cash")
        last_codes = (execution_snapshot.get("last_decision") or {}).get(
            "rejection_codes"
        ) or []
        risk["risk_gate"] = ",".join(last_codes) if last_codes else risk["risk_gate"]
        return {
            "snapshot_identity": self.input_token(),
            "safety_status": self.safety_status(),
            "resource_status": self.resources.snapshot(),
            "cts_anomaly_log_path": str(self.journal.execution_log_path),
            "risk_snapshot": risk,
            "timestamp": _now_text(),
            "gateway_status": self._gateway_status(
                refresh=threading.current_thread().name == "TradingSystemWorker"
            ),
            "market_source_status": market_snapshot["market_source_status"],
            "market_context": market_snapshot["market_context"],
            "scanner_context": market_snapshot["candidates_context"],
            "candidates_context": market_snapshot["candidates_context"],
            "active_universe": market_snapshot["active_universe"],
            "quotes": quotes,
            "positions": position_snapshot["positions"],
            "account_state": position_snapshot["account_state"],
            "open_orders": position_snapshot["open_orders"],
            "signals": intraday_snapshot["signals"],
            "intraday_errors": intraday_snapshot["intraday_errors"],
            "trading_status": execution_snapshot["trading_status"],
            "last_decision": execution_snapshot["last_decision"],
            "last_execution": execution_snapshot["last_execution"],
            "sell_lifecycles": execution_snapshot["sell_lifecycles"],
            "strategy_runtime": {
                strategy: sum(
                    row.get("strategy_type") == strategy
                    for row in intraday_snapshot["signals"].values()
                )
                for strategy in ("MOMENTUM", "TREND")
            },
            "trading_parameter_fingerprint": _parameter_fingerprint(
                self.registry, "trading"
            ),
            "runtime_policy": {
                "strategy_enabled": strategy_enabled,
                "pause_blocks_buy": pause_blocks_buy,
            },
            "event_driven": {
                "latency": dict(self._latency_metrics),
                "event_overflow_count": self._event_overflow_count,
                "pipeline_degraded": self._pipeline_degraded,
                "subscriptions": subscriptions,
                "trade_event_queue_size": self._trade_event_queue.qsize(),
                "cts_callback_queue_size": self._cts_callback_queue.qsize(),
            },
            "cts_reconcile": reconcile,
            "execution_log_path": journal_snapshot["execution_log_path"],
            "daily_trade_report_path": journal_snapshot["daily_trade_report_path"],
            "daily_trade_report": journal_snapshot["daily_trade_report"],
            "daily_trade_metrics": journal_snapshot["daily_trade_metrics"],
            "run_flow_log_path": journal_snapshot["run_flow_log_path"],
            "recent_error_log_path": journal_snapshot["recent_error_log_path"],
            "recent_errors": journal_snapshot["recent_errors"],
            "run_flow_log": journal_snapshot["run_flow_log"],
            "execution_log": journal_snapshot["execution_log"],
            "run_flow_seq": journal_snapshot["run_flow_seq"],
            "execution_seq": journal_snapshot["execution_seq"],
            "trade_revision": journal_snapshot["trade_revision"],
        }

    def _pump_cts_messages(self) -> None:
        try:
            import pythoncom  # type: ignore
        except ImportError:
            return
        try:
            pythoncom.PumpWaitingMessages()
        except Exception:
            return

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.resources.close()
        self.journal.close()
        gateway = self.gateway
        if gateway is None:
            return
        close = getattr(gateway, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


# =============================================================================
# 背景 Worker Component
# =============================================================================


class WorkerCoordinator:
    """維持 Market/Scanner 慢週期與 Trading/CTS 單一執行緒。"""

    def __init__(self, gui: Any, runtime: TradingSystemRuntime) -> None:
        self.gui = gui
        self.runtime = runtime
        self.worker_shutdown = threading.Event()
        self._market_requests: queue.Queue[Dict[str, Any]] = queue.Queue(maxsize=1)
        self._trading_requests: queue.Queue[bool] = queue.Queue(maxsize=1)
        self._commands: queue.Queue[Tuple[str, Dict[str, Any]]] = queue.Queue(
            maxsize=_RUNTIME_COMMAND_QUEUE_SIZE
        )
        self._results: queue.Queue[Dict[str, Any]] = queue.Queue(
            maxsize=_RUNTIME_RESULT_QUEUE_SIZE
        )
        self._display_lock = threading.Lock()
        self._pending_display = None
        self._last_display_at = 0.0
        self._last_snapshot_at = 0.0
        self._last_trade_revision = 0
        self._liveness_active = {}
        self._market_thread: Optional[threading.Thread] = None
        self._trading_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._market_thread is not None and self._market_thread.is_alive():
            return
        self.worker_shutdown.clear()
        self._market_thread = threading.Thread(
            target=self._market_loop,
            name="MarketScannerWorker",
            daemon=True,
        )
        self._trading_thread = threading.Thread(
            target=self._trading_loop,
            name="TradingSystemWorker",
            daemon=True,
        )
        self._stream_thread = threading.Thread(
            target=self._stream_loop, name="StreamHealthWorker", daemon=True
        )
        self._stream_thread.start()
        self._market_thread.start()
        self._trading_thread.start()

    def _put_result(self, payload: Mapping[str, Any]) -> None:
        item = dict(payload)
        snapshot = item.pop("snapshot", None)
        if isinstance(snapshot, Mapping):
            self.runtime.publish_display_snapshot(snapshot)
            with self._display_lock:
                self._pending_display = snapshot
        # 行情更新合併為最新快照；指令結果及錯誤另外保留。
        if item.get("type") in {"snapshot", "market_refresh", "trading_refresh"}:
            return
        try:
            self._results.put_nowait(item)
        except queue.Full:
            try:
                self._results.get_nowait()
            except queue.Empty:
                pass
            try:
                self._results.put_nowait(item)
            except queue.Full:
                pass

    def request_market_refresh(self, *, force: bool, reason: str) -> None:
        item = {"force": bool(force), "reason": str(reason)}
        try:
            self._market_requests.put_nowait(item)
            return
        except queue.Full:
            pass
        if not force:
            return
        try:
            pending = self._market_requests.get_nowait()
        except queue.Empty:
            pending = None
        if pending is None or not bool(pending.get("force")):
            try:
                self._market_requests.put_nowait(item)
            except queue.Full:
                pass
        elif pending is not None:
            try:
                self._market_requests.put_nowait(pending)
            except queue.Full:
                pass

    def request_trading_refresh(self) -> None:
        try:
            self._trading_requests.put_nowait(True)
        except queue.Full:
            pass

    def update_runtime_status(self) -> None:
        """只更新 GUI 狀態文字；不得把 Snapshot 再送回 result queue。

        GUI 每次 render Snapshot 後都會呼叫本方法。若此處再次 enqueue Snapshot，
        poll_results() 會在 Tk 主執行緒形成 render -> status -> enqueue -> render 的無限迴圈。
        """
        update_var = getattr(self.gui, "update_var", None)
        if not hasattr(update_var, "set"):
            return

        market_pending = not self._market_requests.empty()
        trading_pending = (
            not self._trading_requests.empty() or not self._commands.empty()
        )
        market_text = "等待更新" if market_pending else "待命"
        trading_text = "等待處理" if trading_pending else "待命"
        try:
            resources = self.runtime.resources
            physical = str(resources.physical_cores) if resources.core_detection != "logical_fallback" else "未辨識"
            update_var.set(f"市場掃描：{market_text}｜交易監控：{trading_text}｜CPU {physical}實體/{resources.logical_cores}邏輯，計算 {resources.cpu_workers} 程序")
        except Exception:
            return

    def submit_command(
        self, command: str, payload: Optional[Mapping[str, Any]] = None
    ) -> bool:
        try:
            self._commands.put_nowait((str(command), dict(payload or {})))
        except queue.Full:
            return False
        return True

    def _record_liveness_transitions(self, findings, held_symbols):
        # Gateway 每次回傳所有仍疑似斷流的持倉，包括 REST 尚未完成者。
        # 價格、經過秒數與 REST 子狀態變化不重新觸發同一段異常。
        active = self._liveness_active
        held = set(held_symbols)
        reasons = {
            "QUOTE_SUSPECT_REST_PENDING": "串流行情逾時或缺少時間，REST 保護查詢中",
            "REST_PROTECTION": "串流行情逾時，暫由有效 REST 行情提供保護",
            "REST_PROTECTION_RESUBSCRIBE": "串流行情逾時，已使用 REST 保護並要求重新訂閱",
            "REST_STALE": "串流行情逾時，REST 行情亦已逾時或缺少時間",
            "REST_PROTECTION_UNAVAILABLE": "串流行情逾時，REST 保護查詢失敗",
        }
        for symbol, finding in findings.items():
            if symbol not in held or symbol in active:
                continue
            status = str(finding.get("status") or "UNKNOWN")
            age = finding.get("quote_age")
            age_text = "未知" if age is None else f"{age:.3f} 秒"
            reason = reasons.get(status, "持倉行情健康檢查異常：" + status)
            message = f"{symbol} {reason}；行情年齡={age_text}"
            if finding.get("error"):
                message += "；錯誤=" + str(finding["error"])
            details = {"symbol": symbol, "reason": reason, "message": message,
                       "liveness_status": status, "quote_age": age, "error": finding.get("error")}
            record = self.runtime._record_run_flow(run_id="LIVENESS", stage="POSITION_LIVENESS",
                                                   status="WARN", details=details)
            active[symbol] = reason
            self._put_result({"type": "liveness_transition", "record": record})
        for symbol in list(active):
            if symbol in findings:
                continue
            previous = active.pop(symbol)
            if symbol not in held:
                continue  # 已移除持倉不冒充行情恢復。
            record = self.runtime._record_run_flow(run_id="LIVENESS", stage="POSITION_LIVENESS",
                status="INFO", details={"symbol": symbol, "message": f"recovered {symbol} 串流行情恢復正常",
                                         "previous_reason": previous})
            self._put_result({"type": "liveness_transition", "record": record})

    def _stream_loop(self):
        failure = ""
        failed_at = 0.0
        while not self.worker_shutdown.wait(1.0):
            gateway = self.runtime.gateway
            if gateway is None:
                continue
            try:
                gateway.maintain_market_stream(
                    enabled=bool(
                        _setting(self.runtime.settings, "fugle_websocket_enabled", True)
                    ),
                    health_timeout_seconds=float(
                        _setting(
                            self.runtime.settings,
                            "fugle_stream_health_timeout_seconds",
                            30.0,
                        )
                    ),
                )
                check = getattr(gateway,"check_position_liveness",None)
                if callable(check):
                    held_symbols = [symbol for symbol,_ in self.runtime.positions.keys()]
                    findings = check(held_symbols)
                    for symbol,finding in findings.items():
                        quote = finding.get("quote") or {}
                        for key in self.runtime.positions.keys():
                            if key[0] != symbol:
                                continue
                            pos = self.runtime.positions.get(*key) or {}
                            price = quote.get("price")
                            intraday = self.runtime.modules["intraday"]
                            source = intraday._parse_fugle_event_time(quote.get("timestamp"))
                            entered = intraday._parse_timestamp(pos.get("entry_time"))
                            emergency = pos.get("emergency_stop_price")
                            structural = pos.get("strategy_stop_price") or pos.get("effective_stop_price")
                            stop_reason = ("EMERGENCY_STOP" if price and emergency and float(price)<=float(emergency)
                                else "STRATEGY_STOP" if price and structural and float(price)<=float(structural) else None)
                            if stop_reason and source and entered and source>entered:
                                signal = dict(symbol=key[0],market=key[1],strategy_type=pos.get("strategy_type"),entry_signal=False,
                                    exit_signal=True,exit_reason=stop_reason,exit_stage=stop_reason,exit_fraction=1,
                                    signal_price=price,sell_fallback_quote=quote,signal_fingerprint=_stable_hash([key,source,price,"REST_STOP"]))
                                try:
                                    self.runtime._rest_emergency_queue.put_nowait(signal)
                                except queue.Full:
                                    pass
                    self._record_liveness_transitions(findings, held_symbols)

                status = gateway.status().get("fugle", {})
                if failure and status.get("connected") and status.get("authenticated"):
                    self.runtime._record_run_flow(
                        run_id=self.runtime._new_run_id("STREAM"),
                        stage="STREAM_HEALTH", status="RECOVERED",
                        details={"previous_error": failure,
                                 "outage_seconds": round(time.monotonic() - failed_at, 3)},
                    )
                    failure = ""
            except Exception as exc:
                if not failure:
                    failed_at = time.monotonic()
                failure = str(exc)
                self._worker_fault("STREAM_HEALTH", exc)

    def _market_loop(self) -> None:
        last_stream_check = 0.0
        while not self.worker_shutdown.is_set():
            if (
                self.runtime._subscription_refresh_needed.is_set()
                or time.monotonic() - last_stream_check >= 1.0
            ):
                self.runtime._subscription_refresh_needed.clear()
                last_stream_check = time.monotonic()
                try:
                    if self.runtime.gateway is not None:
                        self.runtime._apply_subscriptions()
                except Exception as exc:
                    self._worker_fault("STREAM_SUBSCRIPTIONS", exc)
            try:
                request = self._market_requests.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                result = self.runtime.run_market_scanner_cycle(
                    force=bool(request.get("force")),
                    reason=str(request.get("reason") or "INTERVAL"),
                )
                self._put_result(
                    {
                        "type": "market_refresh",
                        "result": result,
                        "snapshot": self.runtime.snapshot(),
                    }
                )
                self.request_trading_refresh()
            except Exception as exc:
                self._worker_fault("MARKET_LOOP", exc)
                if self.runtime.market_error_notification_due():
                    self._put_result(
                        {
                            "type": "worker_error",
                            "command": "market_refresh",
                            "message": f"{type(exc).__name__}: {exc}",
                        }
                    )

    def _process_commands(self) -> None:
        for _ in range(4):
            try:
                command, payload = self._commands.get_nowait()
            except queue.Empty:
                break
            try:
                result = self.runtime.execute_command(command, payload)
                self._put_result(
                    {
                        "type": "command_result",
                        "command": command,
                        "result": result,
                        "snapshot": self.runtime.snapshot(),
                    }
                )
            except Exception as exc:
                self.runtime._record_run_flow(run_id=self.runtime._new_run_id("COMMAND"),
                    stage="COMMAND", status="ERROR", details={"command": command, "error": f"{type(exc).__name__}: {exc}"})
                self._put_result(
                    {
                        "type": "worker_error",
                        "command": command,
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                )

    def _trading_loop(self) -> None:
        pythoncom = None
        try:
            import pythoncom as imported_pythoncom  # type: ignore

            pythoncom = imported_pythoncom
            pythoncom.CoInitialize()
        except Exception:
            pythoncom = None

        try:
            try:
                self.runtime._ensure_gateway_ready()
                status = self.runtime._gateway_status(refresh=True)
                fugle = (
                    dict(status.get("fugle") or {})
                    if isinstance(status.get("fugle"), Mapping)
                    else {}
                )
                self.runtime._record_run_flow(
                    run_id=self.runtime._new_run_id("GATEWAY"),
                    stage="GATEWAY_STARTUP",
                    status="OK",
                    details={
                        "mode": str(status.get("mode") or ""),
                        "market_provider": str(status.get("market_provider") or ""),
                        "api_key_configured": bool(fugle.get("api_key_configured")),
                    },
                )
            except Exception as exc:
                self.runtime._record_run_flow(
                    run_id=self.runtime._new_run_id("GATEWAY"),
                    stage="GATEWAY_STARTUP",
                    status="ERROR",
                    details={"error": f"{type(exc).__name__}: {exc}"},
                )
                self._put_result(
                    {
                        "type": "worker_error",
                        "command": "gateway_startup",
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                )

            while not self.worker_shutdown.is_set():
                try:
                    self._process_commands()
                    with self.runtime._lock:
                        pending_settings = self.runtime._pending_position_settings
                        self.runtime._pending_position_settings = None
                    if pending_settings is not None:
                        self.runtime.positions.update_settings(pending_settings)
                    self.runtime._pump_cts_messages()
                    if self.runtime.gateway is not None:
                        self.runtime._gateway_status(refresh=True)
                    force = False
                    try:
                        self._trading_requests.get_nowait()
                        force = True
                    except queue.Empty:
                        pass

                    has_trade_event = (not self.runtime._trade_event_queue.empty()
                        or not self.runtime._raw_trade_events.empty())
                    cts_connected = bool(self.runtime._cts_status().get("connected"))
                    if not force and not has_trade_event and not cts_connected and not self.runtime._clock_exit_due():
                        self.worker_shutdown.wait(_TRADING_LOOP_IDLE_SECONDS)
                        continue

                    try:
                        result = self.runtime.run_trading_cycle(force=force or self.runtime._worker_buy_blocked)
                        if (result.get("cts") or {}).get("reconciled"):
                            self.runtime._worker_buy_blocked = False
                        display_interval = max(
                            0.1,
                            float(
                                _setting(
                                    self.runtime.settings, "quote_refresh_seconds", 1.0
                                )
                            ),
                        )
                        now_display = time.monotonic()
                        trade_revision = self.runtime.journal._trade_revision
                        if (
                            force
                            or trade_revision != self._last_trade_revision
                            or now_display - self._last_snapshot_at >= display_interval
                        ):
                            self._last_snapshot_at = now_display
                            self._last_trade_revision = trade_revision
                            self._put_result(
                                {
                                    "type": "trading_refresh",
                                    "result": result,
                                    "snapshot": self.runtime.snapshot(),
                                }
                            )
                    except Exception as exc:
                        self._worker_fault("TRADING_CYCLE", exc)
                        self.runtime._record_run_flow(run_id=self.runtime._new_run_id("TRADING"),
                            stage="TRADING_ERROR", status="ERROR", details={"error": f"{type(exc).__name__}: {exc}"})
                        self._put_result(
                            {
                                "type": "worker_error",
                                "command": "trading_refresh",
                                "message": f"{type(exc).__name__}: {exc}",
                            }
                        )
                    self.worker_shutdown.wait(_TRADING_LOOP_IDLE_SECONDS)
                except Exception as exc:
                    self._worker_fault("TRADING_LOOP", exc)
                    self.worker_shutdown.wait(0.2)
        finally:
            try:
                if self.worker_shutdown.is_set():
                    self.runtime.close()
            finally:
                if pythoncom is not None:
                    try:
                        pythoncom.CoUninitialize()
                    except Exception:
                        pass

    def _worker_fault(self, stage, exc):
        if stage.startswith("TRADING"):
            self.runtime._worker_buy_blocked = True
        details = {"error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()}
        previous = self.runtime._worker_faults.get(stage)
        stamp = time.monotonic()
        if previous == details["error"] and stamp - self.runtime._worker_fault_times.get(stage, 0) < 30:
            return
        self.runtime._worker_faults[stage] = details["error"]
        self.runtime._worker_fault_times[stage] = stamp
        try:
            self.runtime._record_run_flow(run_id=self.runtime._new_run_id("WORKER"),
                stage=stage, status="ERROR", details=details)
        except Exception:
            traceback.print_exc()

    def _render_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        handler = getattr(self.gui, "_render_snapshot", None)
        if not callable(handler):
            return
        # 先檢查介面，函式內部 TypeError 不得被當成重試理由。
        import inspect
        try:
            inspect.signature(handler).bind(snapshot, force=True)
        except TypeError:
            handler(snapshot)
        else:
            handler(snapshot, force=True)

    def poll_results(self) -> None:
        try:
            processed = 0
            poll_started = time.monotonic()
            while processed < 64 and time.monotonic() - poll_started < 0.01:
                try:
                    item = self._results.get_nowait()
                except queue.Empty:
                    break
                processed += 1
                kind = str(item.get("type") or "")
                if kind == "liveness_transition":
                    handler = getattr(self.gui, "_append_run_flow_record", None)
                    if callable(handler):
                        handler(item["record"])
                    continue
                if kind == "worker_error":
                    handler = getattr(self.gui, "_handle_worker_error", None)
                    if callable(handler):
                        handler(item)
                    else:
                        setter = getattr(self.gui, "_set_message", None)
                        if callable(setter):
                            setter(str(item.get("message") or "Runtime worker error"))
                    continue

                if kind == "command_result":
                    command = str(item.get("command") or "")
                    result = item.get("result")
                    if command == "cts_login":
                        login_handler = getattr(self.gui, "_handle_cts_login_result", None)
                        if callable(login_handler) and isinstance(result, Mapping):
                            login_handler(result)
                    elif command == "cts_logout":
                        cts_var = getattr(self.gui, "cts_var", None)
                        if hasattr(cts_var, "set"):
                            cts_var.set("CTS 未登入")
                        setter = getattr(self.gui, "_set_message", None)
                        if callable(setter):
                            setter("CTS 已登出")
                    else:
                        setter = getattr(self.gui, "_set_message", None)
                        if callable(setter) and isinstance(result, Mapping):
                            setter(str(result.get("status") or "Runtime 指令完成"))

            # 背景已按設定合併快照，Tk 不再等待第二個刷新週期。
            # 實際交易／成交由 trade_revision 提前發布，下一次 Tk 輪詢立即呈現。
            with self._display_lock:
                snapshot = self._pending_display
                self._pending_display = None
            if isinstance(snapshot, Mapping):
                self._last_display_at = time.monotonic()
                self._render_snapshot(snapshot)

        except Exception as exc:
            self._worker_fault("GUI_CALLBACK", exc)
        finally:
            if not self.worker_shutdown.is_set():
                root = getattr(self.gui, "root", None)
                after = getattr(root, "after", None)
                if callable(after):
                    after(150, self.poll_results)

    def shutdown(self) -> None:
        self.worker_shutdown.set()
        for thread in (self._market_thread, self._trading_thread, getattr(self, "_stream_thread", None)):
            if thread is not None and thread.is_alive():
                thread.join(timeout=2.0)


# =============================================================================
# 離線自我測試
# =============================================================================


def run_self_test() -> Dict[str, Any]:
    checks = 0

    class _Param:
        max_daily_loss_amount = 10000.0
        max_daily_loss_pct = 2.0
        def __init__(self, **values: Any) -> None:
            self.__dict__.update(values)

        def snapshot(self) -> Dict[str, Any]:
            return dict(self.__dict__)

    class _Bundle:
        def __init__(self, current: Any) -> None:
            self.current = current

    class _Registry:
        def __init__(self) -> None:
            self.bundles = {
                "market": _Bundle(_Param()),
                "scanner": _Bundle(_Param(active_universe_n=2, scanner_round_time_limit_seconds=30.0)),
                "intraday": _Bundle(_Param(exit_time="23:59:59")),
                "trading": _Bundle(_Param()),
            }

        def validate_all(self) -> None:
            return None

        def fingerprints(self) -> Dict[str, str]:
            return {
                name: _stable_hash(bundle.current.snapshot())
                for name, bundle in self.bundles.items()
            }

    class _CTS:
        def __init__(self) -> None:
            self.com_prog_id = "TEST"
            self._connected = False
            self._account_id = ""
            self._callbacks: List[Any] = []
            self.query_calls: List[Tuple[Any, ...]] = []

        def add_callback(self, callback: Any) -> None:
            self._callbacks.append(callback)

        def status(self) -> Dict[str, Any]:
            return {"connected": self._connected, "account_id": self._account_id}

        def login(
            self, trade_das: str, uid: str, password: str, *, account_id: str = ""
        ) -> Dict[str, Any]:
            del trade_das, uid, password
            self._connected = True
            self._account_id = account_id or "TEST-ACCOUNT"
            return {
                "ok": True,
                "connected": True,
                "account_id": self._account_id,
                "accounts": [],
            }

        def logout(self) -> Dict[str, Any]:
            self._connected = False
            self._account_id = ""
            return {"ok": True, "connected": False}

        def query_position(
            self, account_id: str, trade_date: int, force_query: bool = True
        ) -> Dict[str, Any]:
            self.query_calls.append(("POSITION", account_id, trade_date, force_query))
            return {
                "ok": True,
                "status": "EMPTY",
                "record_count": 0,
                "raw_error": "",
                "parsed_result": [],
            }

        def query_order(
            self, account_id: str, force_query: bool = True
        ) -> Dict[str, Any]:
            self.query_calls.append(("ORDER", account_id, force_query))
            return {
                "ok": True,
                "status": "EMPTY",
                "record_count": 0,
                "raw_error": "",
                "parsed_result": [],
            }

        def query_match(
            self, account_id: str, force_query: bool = True
        ) -> Dict[str, Any]:
            self.query_calls.append(("MATCH", account_id, force_query))
            return {
                "ok": True,
                "status": "EMPTY",
                "record_count": 0,
                "raw_error": "",
                "parsed_result": [],
                "normalized_result": [],
            }

        def cancel_order(self, request: Any) -> Dict[str, Any]:
            del request
            return {"error": ""}

    class _Gateway:
        def __init__(self, **kwargs: Any) -> None:
            self.cts = _CTS()
            self.replay_store = SimpleNamespace()
            self.event_sink = kwargs.get("event_sink")
            self.mode = str(kwargs.get("mode") or "OFFLINE").upper()
            self.api_key = str(kwargs.get("api_key") or "")
            self.snapshot_calls = 0
            self._connected = False
            self.state_rows = [
                {
                    "symbol": "2330",
                    "market": "TWSE",
                    "price": 100.0,
                    "event_time": 1,
                    "serial": 1,
                    "quote_sequence": 1,
                    "symbol_quote_sequence": 1,
                    "source_at": "2026-09-13T09:00:00+08:00",
                    "realtime_source_at": "2026-09-13T09:00:00+08:00",
                    "realtime_age_seconds": 1.0,
                    "realtime_fresh": True,
                    "realtime_freshness_status": "FRESH",
                },
                {
                    "symbol": "2454",
                    "market": "TWSE",
                    "price": 90.0,
                    "event_time": 2,
                    "serial": 1,
                    "quote_sequence": 2,
                    "symbol_quote_sequence": 1,
                    "source_at": "2026-09-13T09:00:01+08:00",
                    "realtime_source_at": "2026-09-13T09:00:01+08:00",
                    "realtime_age_seconds": 1.0,
                    "realtime_fresh": True,
                    "realtime_freshness_status": "FRESH",
                },
            ]

        def status(self) -> Dict[str, Any]:
            return {
                "mode": self.mode,
                "market_provider": "FUGLE",
                "fugle": {
                    "connected": self._connected,
                    "authenticated": self._connected,
                    "api_key_configured": bool(self.api_key),
                    "subscriptions": {"desired": []},
                },
                "cts_connected": bool(self.cts.status().get("connected")),
                "cts": self.cts.status(),
            }

        def fugle_login(self) -> Dict[str, Any]:
            self._connected = True
            return self.status()

        def set_book_subscriptions(self, symbols: Iterable[str]) -> None:
            self.book_symbols = set(symbols)

        def subscribe_market(
            self,
            symbols: Iterable[str],
            *,
            initialize: bool = True,
            channels: Sequence[str] = ("trades",),
        ) -> Dict[str, Any]:
            del symbols, initialize
            assert tuple(channels) == ("trades",)
            return self.status()

        def get_active_universe(
            self, *, limit: Optional[int] = None
        ) -> List[Dict[str, Any]]:
            rows = [
                {"symbol": "2330", "market": "TWSE", "source": "SELF_TEST"},
                {"symbol": "2454", "market": "TWSE", "source": "SELF_TEST"},
            ]
            return copy.deepcopy(rows[:limit] if limit else rows)

        def get_market_state_snapshot(
            self,
            symbols: Optional[Iterable[str]] = None,
            *,
            as_of: Optional[datetime] = None,
        ) -> Dict[str, Any]:
            assert symbols is None
            assert isinstance(as_of, datetime)
            self.snapshot_calls += 1
            rows = copy.deepcopy(self.state_rows)
            return {
                "generation": max(
                    (int(row.get("quote_sequence") or 0) for row in rows), default=0
                ),
                "quote_sequence": max(
                    (int(row.get("quote_sequence") or 0) for row in rows), default=0
                ),
                "freshness_seconds": 3.0,
                "freshness_basis": "event_time",
                "source_at": max(
                    (str(row.get("source_at") or "") for row in rows), default=""
                ),
                "rows": rows,
            }

        def get_quote(
            self, symbol: str, market: Optional[str] = None
        ) -> Optional[Dict[str, Any]]:
            for row in self.state_rows:
                if row["symbol"] != symbol:
                    continue
                if market and _normalize_market(row.get("market")) != _normalize_market(
                    market
                ):
                    continue
                return copy.deepcopy(row)
            return None

        def refresh_day_trade(self, symbols: Iterable[str]) -> Dict[str, Any]:
            return {"day_trade_map": {str(symbol): True for symbol in symbols}}

        def get_day_trade_map(
            self, symbols: Optional[Iterable[str]] = None
        ) -> Dict[str, bool]:
            return {str(symbol): True for symbol in (symbols or [])}

        def close(self) -> Dict[str, Any]:
            return {"ok": True, "errors": []}

    class _CancelRequest:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    api_module = SimpleNamespace(APIGateway=_Gateway, CTSCancelRequest=_CancelRequest)
    market_status = {
        "state": "READY",
        "publication_state": "READY",
        "generation": 1,
        "source_at": "2026-09-13T09:00:00+08:00",
        "fingerprint": "SELF-TEST-MARKET",
        "error": "",
        "error_stage": "",
        "stage_errors": [],
    }

    def _refresh_market_snapshot(**kwargs: Any) -> Dict[str, Any]:
        del kwargs
        return {
            "allow_long": True,
            "session_phase": "REGULAR",
            "session_state": "PREVIOUS_SESSION",
            "session_trade_date": "2026-09-11",
            "market_state": "BULL",
            "market_score": 50.0,
            "sector_map": {"TWSE:2330": "半導體業", "TPEx:6488": "半導體業"},
            "market_snapshot": {
                "source_at": "2026-09-13T09:00:00+08:00",
                "calculated_at": "2026-09-13T09:00:00+08:00",
                "generation": 1,
                "fingerprint": "SELF-TEST-MARKET",
            },
        }

    market_module = SimpleNamespace(
        refresh_market_snapshot=_refresh_market_snapshot,
        get_market_snapshot_status=lambda: copy.deepcopy(market_status),
    )

    captured_scanner_inputs: List[Dict[str, Any]] = []

    def _get_candidates(**kwargs: Any) -> Dict[str, Any]:
        captured_scanner_inputs.append(copy.deepcopy({k:v for k,v in kwargs.items() if k != "resource_manager"}))
        market_state = kwargs["market_state_snapshot"]
        rows = market_state.get("rows") if isinstance(market_state, Mapping) else []
        candidates = []
        for index, row in enumerate(rows or [], 1):
            if not isinstance(row, Mapping):
                continue
            candidates.append(
                {
                    **dict(row),
                    "rank": index,
                    "trade_ready": True,
                    "realtime_source_at": row.get("realtime_source_at"),
                    "realtime_age_seconds": row.get("realtime_age_seconds"),
                }
            )
        fingerprint = _stable_hash(candidates)
        return {
            "candidates": candidates,
            "candidate_source_status": "READY",
            "candidate_fingerprint": fingerprint,
            "fingerprint": fingerprint,
        }

    scanner_module = SimpleNamespace(get_candidates=_get_candidates)

    def _get_signal_context(**kwargs: Any) -> Dict[str, Any]:
        return {
            "symbol": kwargs["symbol"],
            "market": kwargs["market"],
            "entry_signal": False,
            "exit_signal": False,
            "signal_fingerprint": _stable_hash((kwargs["symbol"], kwargs["market"])),
        }

    intraday_module = SimpleNamespace(get_signal_context=_get_signal_context)

    trading_module = SimpleNamespace(
        restore_system_order_registry=lambda rows: len(rows),
        get_system_order_registry=lambda: [],
        portfolio_risk_snapshot=lambda *args, **kwargs: {"risk_gate": "READY"},
        evaluate_trading_decision=lambda *args, **kwargs: {
            "decision": "REJECT",
            "action": "",
            "rejection_codes": ["SELF_TEST"],
        },
        execute_allowed_decision=lambda *args, **kwargs: {"status": "UNUSED"},
        parse_cts_order_state=lambda rows, **kwargs: {"found": False, "active": False},
        parse_cts_positions=lambda rows, **kwargs: [],
        resolve_cts_match_ownership=lambda row: {"owner": "UNKNOWN"},
    )

    settings = SimpleNamespace(
        api_mode="OFFLINE",
        replay_dir="api_replay",
        fugle_event_freshness_seconds=3.0,
        fugle_reconnect_min_seconds=1.0,
        fugle_reconnect_max_seconds=30.0,
        cts_com_prog_id="TEST",
        control_mode="MANUAL",
        manual_market_state=None,
        manual_trading_mode=None,
        fugle_subscription_limit=500,
        fugle_candidate_subscription_enabled=True,
        fugle_websocket_enabled=False,
        fugle_rest_seed_enabled=False,
        fugle_books_subscription_mode="OFF",
        intraday_watch_count=2,
        auto_execute=False,
        available_cash=1_000_000.0,
        equity=1_000_000.0,
        daily_trade_count=0,
        daily_pnl=0.0,
        cts_account_id="",
    )

    with TemporaryDirectory() as temp_dir:
        fake_file = str(Path(temp_dir) / "core.py")
        for module in (
            api_module,
            market_module,
            scanner_module,
            intraday_module,
            trading_module,
        ):
            module.__file__ = fake_file
        modules = {
            "api_gateway": api_module,
            "market": market_module,
            "scanner": scanner_module,
            "intraday": intraday_module,
            "trading": trading_module,
        }
        runtime = TradingSystemRuntime(modules, _Registry(), settings)
        assert runtime.gateway is None
        checks += 1

        original_api_key = os.environ.get("FUGLE_MARKETDATA_API_KEY")
        try:
            os.environ["FUGLE_MARKETDATA_API_KEY"] = "SELF-TEST-FUGLE-KEY"
            live_settings = SimpleNamespace(**vars(settings))
            live_settings.api_mode = "LIVE"
            live_runtime = TradingSystemRuntime(modules, _Registry(), live_settings)
            live_gateway = live_runtime._ensure_gateway_ready()
            assert live_gateway.api_key == "SELF-TEST-FUGLE-KEY"
            assert live_gateway.status()["fugle"]["api_key_configured"] is True
            live_runtime.close()

            os.environ.pop("FUGLE_MARKETDATA_API_KEY", None)
            missing_key_runtime = TradingSystemRuntime(
                modules, _Registry(), live_settings
            )
            try:
                missing_key_runtime._ensure_gateway_ready()
            except RuntimeError as exc:
                assert "FUGLE_MARKETDATA_API_KEY" in str(exc)
            else:
                raise AssertionError(
                    "LIVE Gateway 缺少 Fugle API Key 必須在啟動檢查階段失敗"
                )
            missing_key_runtime.close()
        finally:
            if original_api_key is None:
                os.environ.pop("FUGLE_MARKETDATA_API_KEY", None)
            else:
                os.environ["FUGLE_MARKETDATA_API_KEY"] = original_api_key
        checks += 1

        market_result = runtime.run_market_scanner_cycle(force=True, reason="SELF_TEST")
        assert market_result["ok"] is True
        assert market_result["candidate_count"] == 2
        assert runtime.gateway.snapshot_calls == 1
        assert callable(runtime.gateway.event_sink)
        assert len(captured_scanner_inputs) == 1
        assert captured_scanner_inputs[0]["sector_map"] == captured_scanner_inputs[0]["market_context"]["sector_map"]
        assert captured_scanner_inputs[0]["sector_map"]["TWSE:2330"] == "半導體業"
        frozen_input = captured_scanner_inputs[0]["market_state_snapshot"]
        assert isinstance(frozen_input, Mapping)
        assert frozen_input.get("snapshot_at") == frozen_input.get("as_of")
        assert len(frozen_input.get("rows") or []) == 2
        checks += 1

        first_snapshot = runtime.snapshot()
        first_price = first_snapshot["candidates_context"]["candidates"][0]["price"]
        runtime.gateway.state_rows[0]["price"] = 111.0
        unchanged_snapshot = runtime.snapshot()
        assert (
            unchanged_snapshot["candidates_context"]["candidates"][0]["price"]
            == first_price
        )
        second_market_result = runtime.run_market_scanner_cycle(
            force=True, reason="SELF_TEST_NEXT_ROUND"
        )
        assert second_market_result["ok"] is True
        assert runtime.gateway.snapshot_calls == 2
        second_snapshot = runtime.snapshot()
        assert second_snapshot["candidates_context"]["candidates"][0]["price"] == 111.0
        checks += 1

        same_input_a = _get_candidates(**copy.deepcopy(captured_scanner_inputs[-1]))
        same_input_b = _get_candidates(**copy.deepcopy(captured_scanner_inputs[-1]))
        assert same_input_a == same_input_b
        assert same_input_a["fingerprint"] == same_input_b["fingerprint"]
        checks += 1

        runtime.set_runtime_policy(strategy_enabled=True, pause_blocks_buy=False)
        trading_result = runtime.run_trading_cycle(force=True)
        assert trading_result["changed_quotes"] == 0
        runtime.set_runtime_policy(strategy_enabled=False, pause_blocks_buy=False)
        first_idle = runtime.run_trading_cycle(force=False)
        second_idle = runtime.run_trading_cycle(force=False)
        assert first_idle["changed_quotes"] == 0
        assert first_idle["updated_signals"] == 0
        assert first_idle["executions"] == []
        assert first_idle["cts"]["reconciled"] is False
        assert second_idle["run_flow_logged"] is False
        assert second_idle["run_flow_log_reason"] == "SUPPRESSED_IDLE"
        checks += 1

        login_result = runtime.execute_command(
            "cts_login",
            {
                "trade_das": "TEST",
                "uid": "TEST",
                "password": "SECRET",
                "account_id": "TEST-ACCOUNT",
                "prog_id": "TEST",
            },
        )
        assert login_result["ok"] is True
        assert login_result["account_id"] == "TEST-ACCOUNT"
        assert login_result["reconcile"]["reconciled"] is True
        assert login_result["reconcile"]["session_trade_date"] == _trade_date_int()
        reconcile_calls = runtime.gateway.cts.query_calls[-3:]
        assert reconcile_calls[0] == (
            "POSITION",
            "TEST-ACCOUNT",
            _trade_date_int(),
            True,
        )
        assert reconcile_calls[1] == ("ORDER", "TEST-ACCOUNT", True)
        assert reconcile_calls[2] == ("MATCH", "TEST-ACCOUNT", True)
        checks += 1

        captured_triggers: List[str] = []
        original_decision = trading_module.evaluate_trading_decision

        def _capture_trigger(
            signal_context: Mapping[str, Any], *_args: Any, **_kwargs: Any
        ) -> Dict[str, Any]:
            captured_triggers.append(str(signal_context.get("execution_trigger") or ""))
            return {
                "decision": "REJECT",
                "action": "BUY",
                "rejection_codes": ["SELF_TEST"],
            }

        trading_module.evaluate_trading_decision = _capture_trigger
        try:
            runtime.execution.process_signal(
                runtime,
                {
                    "symbol": "2330",
                    "market": "TWSE",
                    "entry_signal": True,
                    "entry_wave_anchor_price": 99.0,
                    "strategy_type": "MOMENTUM",
                    "runtime_token": runtime.input_token("2330", "TWSE"),
                },
            )
        finally:
            trading_module.evaluate_trading_decision = original_decision
        assert captured_triggers == ["AUTO"]
        checks += 1

        journal = RuntimeJournalComponent(Path(temp_dir))
        journal._daily_ledger = {
            "buy": {
                "trade_date": "20260914",
                "time": "090000000",
                "symbol": "2330",
                "market": "TWSE",
                "action": "BUY",
                "price": 100.0,
                "quantity": 1000,
                "order_id": "BUY",
                "match": {"account_id": "TEST", "match_id": "B", "fee": 0},
                "ownership": {"owner": "MOMENTUM_SCALPING"},
            },
            "sell": {
                "trade_date": "20260914",
                "time": "090100000",
                "symbol": "2330",
                "market": "TWSE",
                "action": "SELL",
                "price": 101.0,
                "quantity": 1000,
                "order_id": "SELL",
                "match": {
                    "account_id": "TEST",
                    "match_id": "S",
                    "fee": 0,
                    "tax": 150,
                    "trade_type": 0,
                    "order_type": 0,
                },
                "ownership": {"owner": "MOMENTUM_SCALPING"},
            },
        }
        metrics = journal.managed_daily_metrics()
        assert metrics["daily_gross_pnl"] == 1000.0
        assert 0 < metrics["daily_realized_pnl"] < 1000.0
        checks += 1

        snapshot = runtime.snapshot()
        assert len(snapshot["candidates_context"]["candidates"]) == 2
        assert snapshot["scanner_context"] == snapshot["candidates_context"]
        assert len(snapshot["quotes"]) == 2
        assert snapshot["event_driven"]["subscriptions"]["p1"] == 2
        assert isinstance(snapshot["event_driven"]["subscriptions"]["p1_symbols"], list)
        assert "processed_signal_fingerprints" in snapshot["account_state"]
        assert snapshot["event_driven"]["subscriptions"]["books_supported"] is True
        assert snapshot["event_driven"]["subscriptions"]["books_effective"] == "P0_P1"
        assert "run_flow_log" in snapshot and "execution_log" in snapshot
        checks += 1

        runtime._record_run_flow(
            run_id=runtime._new_run_id("SELFTEST"),
            stage="SELF_TEST",
            status="ERROR",
            details={"password": "SECRET", "checks": checks},
        )
        runtime.journal._log_queue.join()
        log_text = runtime.run_flow_log_path.read_text(encoding="utf-8")
        assert "SECRET" not in log_text
        assert "***" in log_text
        checks += 1

        original_refresh = market_module.refresh_market_snapshot
        market_status.update(
            {
                "state": "DEGRADED",
                "publication_state": "DEGRADED",
                "error": "Fugle snapshot failed",
                "error_stage": "TWSE_SNAPSHOT_ERROR",
                "stage_errors": [
                    {"stage": "TWSE_SNAPSHOT_ERROR", "error": "provider failure"}
                ],
            }
        )

        def _failing_market_refresh(**kwargs: Any) -> Dict[str, Any]:
            del kwargs
            raise RuntimeError("Fugle snapshot failed")

        market_module.refresh_market_snapshot = _failing_market_refresh
        failed_round = runtime.run_market_scanner_cycle(
            force=True, reason="SELF_TEST_ERROR"
        )
        assert failed_round["ok"] is False
        assert runtime.market_scanner.market_context["allow_long"] is False
        error_record = next(
            item
            for item in reversed(runtime.journal.snapshot()["recent_run_flow"])
            if item["status"] == "ERROR"
        )
        assert error_record["status"] == "ERROR"
        assert error_record["details"]["error_stage"] == "TWSE_SNAPSHOT_ERROR"
        assert error_record["details"]["stage_errors"][0]["error"] == "provider failure"
        assert error_record["details"]["market_error"] == "Fugle snapshot failed"
        first_error_fingerprint = error_record["details"]["error_fingerprint"]
        error_count_before_repeat = sum(
            1
            for item in runtime.journal.snapshot()["recent_run_flow"]
            if item.get("stage") == "MARKET_SCANNER" and item.get("status") == "ERROR"
        )
        failed_repeat = runtime.run_market_scanner_cycle(
            force=True, reason="SELF_TEST_ERROR_REPEAT"
        )
        assert failed_repeat["ok"] is False
        error_count_after_repeat = sum(
            1
            for item in runtime.journal.snapshot()["recent_run_flow"]
            if item.get("stage") == "MARKET_SCANNER" and item.get("status") == "ERROR"
        )
        assert error_count_after_repeat == error_count_before_repeat
        assert runtime._market_error_fingerprint == first_error_fingerprint
        assert runtime.market_error_notification_due() is False

        market_module.refresh_market_snapshot = original_refresh
        market_status.update(
            {
                "state": "READY",
                "publication_state": "READY",
                "error": "",
                "error_stage": "",
                "stage_errors": [],
            }
        )
        recovery_result = runtime.run_market_scanner_cycle(
            force=True,
            reason="SELF_TEST_RECOVERY",
        )
        assert recovery_result["ok"] is True
        assert any(
            item.get("stage") == "MARKET_SCANNER" and item.get("status") == "RECOVERED"
            for item in runtime.journal.snapshot()["recent_run_flow"]
        )
        checks += 1

        class _Var:
            def __init__(self) -> None:
                self.value = ""

            def set(self, value: Any) -> None:
                self.value = str(value)

        class _Root:
            def after(self, delay: int, callback: Any) -> None:
                del delay, callback

        class _GUI:
            def __init__(self) -> None:
                self.root = _Root()
                self.update_var = _Var()
                self.render_count = 0
                self.workers: Any = None

            def _render_snapshot(
                self, payload: Mapping[str, Any], force: bool = False
            ) -> None:
                del payload, force
                self.render_count += 1
                self.workers.update_runtime_status()

        fake_gui = _GUI()
        coordinator = WorkerCoordinator(fake_gui, runtime)
        fake_gui.workers = coordinator
        coordinator._put_result({"type": "snapshot", "snapshot": runtime.snapshot()})
        coordinator.poll_results()
        assert fake_gui.render_count == 1
        assert coordinator._results.empty()
        assert fake_gui.update_var.value.startswith("市場掃描：")
        checks += 1

        runtime.close()
        checks += 1

    required_classes = (
        RuntimeJournalComponent,
        RuntimePositionComponent,
        RuntimeMarketScannerComponent,
        RuntimeIntradayComponent,
        RuntimeExecutionComponent,
        WorkerCoordinator,
        TradingSystemRuntime,
    )
    assert all(isinstance(item, type) for item in required_classes)
    checks += 1

    return {
        "ok": True,
        "checks": checks,
        "components": [item.__name__ for item in required_classes],
        "worker_lanes": {
            "market_scanner": "MarketScannerWorker",
            "trading_cts": "TradingSystemWorker",
        },
        "marketstate_policy": "APIGATEWAY_ATOMIC_SNAPSHOT_ONCE_PER_SCANNER_ROUND",
        "fugle_stream_channel": "APIGateway.event_sink + trades",
        "books_subscription_supported": True,
        "strategy_formula_in_runtime": False,
        "tk_widget_in_runtime": False,
    }


# =============================================================================
# 診斷
# =============================================================================


def diagnose_links() -> Dict[str, Any]:
    required = {
        "TradingSystemRuntime": TradingSystemRuntime,
        "WorkerCoordinator": WorkerCoordinator,
        "RuntimeJournalComponent": RuntimeJournalComponent,
        "RuntimePositionComponent": RuntimePositionComponent,
        "RuntimeMarketScannerComponent": RuntimeMarketScannerComponent,
        "RuntimeIntradayComponent": RuntimeIntradayComponent,
        "RuntimeExecutionComponent": RuntimeExecutionComponent,
    }
    return {
        "ok": all(isinstance(value, type) for value in required.values()),
        "components": sorted(required),
        "constants": {
            "DAILY_TRADE_REPORT_PREFIX": DAILY_TRADE_REPORT_PREFIX,
        },
        "boundaries": {
            "tk_ui": False,
            "market_formula": False,
            "scanner_formula": False,
            "intraday_formula": False,
            "trading_formula": False,
            "cts_thread": "TradingSystemWorker",
            "market_scanner_thread": "MarketScannerWorker",
            "fugle_marketstate_read": "APIGateway.get_market_state_snapshot()",
            "fugle_marketstate_snapshot_policy": "ONCE_PER_SCANNER_ROUND_ATOMIC_FROZEN_INPUT",
            "fugle_websocket_channel": "APIGateway.event_sink + trades",
            "api_gateway_private_state_access": False,
            "late_market_event_policy": "NEXT_SCANNER_ROUND_ONLY",
            "gateway_startup_policy": "WORKER_STARTUP_VALIDATE_PUBLIC_STATUS_AND_FUGLE_KEY",
            "market_error_policy": "FINGERPRINT_FIRST_CHANGED_PERIODIC_SUMMARY_RECOVERY",
            "cts_reconcile_policy": "SAME_ACCOUNT_SESSION_TRADE_DATE_PRESERVE_QUERY_STATUS",
            "trading_cycle_log_policy": "EVENT_STATE_CHANGE_OR_LOW_FREQUENCY_HEARTBEAT",
            "gui_status_feedback": "DIRECT_STATUS_ONLY_NO_SNAPSHOT_REQUEUE",
        },
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="交易系統 Runtime Components")
    parser.add_argument(
        "--self-test", action="store_true", help="執行完全離線 Runtime 自我測試"
    )
    parser.add_argument(
        "--diagnose", action="store_true", help="檢查 Runtime Component 契約"
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 顯示結果")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.self_test:
        try:
            result = run_self_test()
        except Exception as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    else:
        result = diagnose_links()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        for key, value in result.items():
            print(f"{key}: {value}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

# 更新摘要：完整 CTS 查詢後執行身分恢復並記錄依據；未送出的 EXIT 重試行情，已送出的 SELL 持續對帳。
