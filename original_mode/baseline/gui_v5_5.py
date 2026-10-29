# -*- coding: utf-8 -*-
"""
交易系統圖形化主框架。

本模組只負責 Tk 畫面、AUTO／MANUAL 與 Session Policy、操作事件、
參數 Component 與 Runtime Component 的接線，以及執行結果顯示。
Market / Scanner / Intraday / Position / Execution / Journal 與背景 Worker
均由獨立 Runtime 模組負責；參數模型、驗證與保存由獨立參數模組負責。

CLI
---
python gui.py
python gui.py --diagnose
python gui.py --diagnose --json
"""

# 中文重點更新：清理歷史相容參數、孤立註解及無效追單顯示，保留測試、診斷、CTS 撤單與委託及持倉對帳。

# 中文重點更新：清除未使用函式、相容介面與無效參數；保留市價買賣、成交對帳、撤買單、清倉及離線驗證。

# 中文重點更新：新增核心配額顯示，精簡執行與成交訊息，支援 Windows 多程序啟動。
# 中文重點更新：市場／系統／風險三區整合，策略與減碼欄位同步更新，依欄名選股並增量更新表格。
# 中文重點更新：補齊審查要求之狀態識別、整張委託、資料時序與恢復安全邊界。
# 中文重點更新：執行紀錄頁顯示固定近期錯誤檔路徑，異常與恢復歷程可跨畫面清除及跨時段追查。
# 中文重點更新：Trend Long 顯示直接或條件允許與禁止原因，固定欄位自動換行。
# 中文重點更新：直接顯示核心 Trend 許可；新增 BUY 當日漲幅與上限欄位，顯示進場阻擋原因。
# 中文重點更新：顯示策略資格原因及本次輸入，區分候選新舊快照並只配對同訊號的交易拒絕碼。
# 中文重點更新：可拖曳策略／持倉區，分列雙策略與 Trading 主因，選股顯示子條件及當日進場歷程。
# 中文重點更新：核心載入與 GUI 啟動失敗時保存完整錯誤堆疊，便於定位初始化問題。
# 中文重點更新：最強類股依欄寬換行，區分分類缺漏與無強勢類股，顯示實際分類涵蓋範圍。


# ------重點說明不要跨過函式--我是分界線-----
# 中文重點更新：Log 顯示執行嘗試與券商受理分類，以及委託識別、主要原因與進場複驗實值。

# 中文重點更新：手動確認後直接市價處置；持倉查詢失敗保留清單並顯示 stale。

# 中文重點更新：合併顯示更新入口，保存 Tk、執行緒與原生崩潰診斷紀錄。

# 中文重點更新：移除選股條件實值與歷程欄位；紀錄固定只顯示交易及異常，取消重複列搬移與暫用 Tk 變數，未處理例外併入統一紀錄。

# 中文重點更新：移除已刪除歷程函式的殘留呼叫，修復持倉及後續畫面更新；刪除已整合的交易紀錄檔與當日買賣報表重複欄位，交易與風控不變。

# 中文重點更新：持倉健康異常與恢復由獨立事件顯示，正常檢查不更新畫面；快照中的相同健康紀錄不重複顯示。

from __future__ import annotations

import argparse
import importlib.util
import inspect
import json
import math
import re
import os
import sys
import threading
import time
import traceback
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

BASE_DIR = Path(__file__).resolve().parent


def _format_sector_status(market):
    context = market.get("sector_context") or {}
    names = market.get("strong_sectors") or []
    if not context.get("available"):
        return "產業分類尚未取得，無法判定強勢類股"
    title = "、".join(str(name) for name in names) if names else "目前無類股達強勢門檻"
    return f"{title}\n分類涵蓋 {context.get('mapped_stocks', 0)}/{context.get('eligible_stocks', 0)} 檔"

AUTO_SWITCH_HOUR = 9
AUTO_SWITCH_MINUTE = 0
AUTO_BUY_START_HOUR = 9
AUTO_BUY_START_MINUTE = 5
SESSION_POLICY_POLL_MS = 1000


CORE_SPECS: Dict[str, Dict[str, Any]] = {
    "api_gateway": {
        "required": ("APIGateway", "CTSClient", "CTSCancelRequest"),
        "parameter_class": None,
        "title": "API Gateway",
    },
    "market": {
        "required": ("get_market_context", "MarketParameters"),
        "parameter_class": "MarketParameters",
        "title": "股票市場參數",
    },
    "scanner": {
        "required": ("get_candidates", "ScannerParameters"),
        "parameter_class": "ScannerParameters",
        "title": "選股參數",
    },
    "intraday": {
        "required": ("get_signal_context", "IntradayParameters"),
        "parameter_class": "IntradayParameters",
        "title": "盤中資訊參數",
    },
    "trading": {
        "required": (
            "evaluate_trading_decision",
            "execute_allowed_decision",
            "maintain_time_exit_orders",
            "parse_cts_order_state",
            "parse_cts_positions",
            "summarize_cts_fills",
            "build_position_from_fill",
            "get_system_order_registry",
            "resolve_cts_match_ownership",
            "TradingParameters",
        ),
        "parameter_class": "TradingParameters",
        "title": "交易／風控",
    },
}


def _now_text() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def _current_trade_date_text(now: Optional[datetime] = None) -> str:
    current = now or datetime.now(timezone(timedelta(hours=8)))
    return current.strftime("%Y%m%d")


def _parse_datetime_value(value: Any) -> Optional[datetime]:
    """解析 ISO 時間；無時區時沿用本機時區。"""
    text = str(value or "").strip()
    if not text or text.upper() == "UNKNOWN":
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(
            tzinfo=datetime.now(timezone(timedelta(hours=8))).tzinfo
        )
    return parsed.astimezone()


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _safe_int(value: Any, default: int = 0) -> int:
    number = _safe_float(value)
    return default if number is None else int(number)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
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


def _log_safe(value: Any) -> Any:
    """建立可保存的執行紀錄副本，排除密碼與 UID 等登入識別資料。"""
    if is_dataclass(value):
        return _log_safe(asdict(value))
    if isinstance(value, Mapping):
        output: Dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            lowered = name.lower()
            if "password" in lowered or lowered in {"pwd", "uid"}:
                continue
            output[name] = _log_safe(item)
        return output
    if isinstance(value, (list, tuple, set)):
        return [_log_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _clean_security_name(value: Any) -> str:
    """清理 CTS／行情常見空白占位值，避免名稱欄被 0 或 '-' 錯誤覆蓋。"""
    text = str(value or "").strip()
    if text.lower() in {"", "0", "0.0", "-", "--", "none", "null", "n/a"}:
        return ""
    return text


def _strategy_passthrough(
    signal_context: Optional[Mapping[str, Any]],
    decision: Optional[Mapping[str, Any]] = None,
    execution: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """只抽取 Intraday／Trading 已計算欄位；GUI 不計算或保存 Wave 策略狀態。"""
    signal = dict(signal_context or {})
    decision_map = dict(decision or {})
    execution_map = dict(execution or {})
    revalidation = (
        dict(execution_map.get("revalidation") or {})
        if isinstance(execution_map.get("revalidation"), Mapping)
        else {}
    )
    wave_extension = (
        dict(revalidation.get("wave_extension") or {})
        if isinstance(revalidation.get("wave_extension"), Mapping)
        else {}
    )
    rejection_codes: List[str] = []
    for source in (
        decision_map.get("rejection_codes"),
        execution_map.get("rejection_codes"),
        revalidation.get("rejection_codes"),
    ):
        if isinstance(source, Sequence) and not isinstance(
            source, (str, bytes, bytearray)
        ):
            for value in source:
                code = str(value or "").strip()
                if code and code not in rejection_codes:
                    rejection_codes.append(code)
    return {
        **{
            key: signal.get(key, decision_map.get(key))
            for key in (
                "strategy_type",
                "strategy_state",
                "entry_setup_type",
                "exit_stage",
                "exit_fraction",
                "strategy_stop_price",
                "emergency_stop_price",
                "volume_expansion_ratio",
                "range_expansion_ratio",
                "trend_structure_reference",
                "revalidation_type",
            )
        },
        "entry_wave_start_time": signal.get("entry_wave_start_time"),
        "entry_wave_anchor_price": signal.get("entry_wave_anchor_price"),
        "entry_wave_age_bars": signal.get("entry_wave_age_bars"),
        "entry_wave_id": signal.get("entry_wave_id"),
        "entry_wave_extension_pct": wave_extension.get("wave_extension_pct"),
        "trading_rejection_codes": rejection_codes,
        "day_change_gate": revalidation.get("day_change_gate") or decision_map.get("day_change_gate") or {},
    }


def _version_tuple(stem: str, prefix: str) -> Optional[Tuple[int, ...]]:
    match = re.fullmatch(rf"{re.escape(prefix)}_v(\d+(?:_\d+)*)", stem)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("_"))


def resolve_latest_module_path(base_dir: Path, prefix: str) -> Path:
    versioned: List[Tuple[Tuple[int, ...], Path]] = []
    for path in base_dir.glob(f"{prefix}_v*.py"):
        version = _version_tuple(path.stem, prefix)
        if version is not None:
            versioned.append((version, path))

    if versioned:
        versioned.sort(key=lambda item: item[0], reverse=True)
        return versioned[0][1]

    fallback = base_dir / f"{prefix}.py"
    if fallback.exists():
        return fallback
    raise FileNotFoundError(f"找不到 {prefix}_v*.py，也找不到 {prefix}.py：{base_dir}")


def _module_missing_members(module: Any, required: Sequence[str]) -> List[str]:
    return [name for name in required if not hasattr(module, name)]


class CoreModuleLoader:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.modules: Dict[str, Any] = {}
        self.paths: Dict[str, Path] = {}

    def resolve(self, prefix: str) -> Path:
        return resolve_latest_module_path(self.base_dir, prefix)

    def load_one(self, prefix: str) -> Any:
        if str(self.base_dir) not in sys.path:
            sys.path.insert(0, str(self.base_dir))

        path = self.resolve(prefix)
        module_name = path.stem

        if module_name in sys.modules:
            module = sys.modules[module_name]
            module_file = Path(getattr(module, "__file__", "") or "").resolve()
            if module_file == path.resolve():
                self.modules[prefix] = module
                self.paths[prefix] = path
                return module

        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"無法建立模組載入規格：{path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        required = CORE_SPECS[prefix]["required"]
        missing = _module_missing_members(module, required)
        if missing:
            raise RuntimeError(f"{path.name} 缺少必要介面：{missing}")

        if prefix == "api_gateway":
            cts_client = getattr(module, "CTSClient", None)
            required_cts_methods = (
                "query_position",
                "query_match",
                "query_order",
            )
            missing_cts_methods = [
                name
                for name in required_cts_methods
                if not callable(getattr(cts_client, name, None))
            ]
            if missing_cts_methods:
                raise RuntimeError(
                    f"{path.name} 的 CTSClient 缺少必要查詢介面："
                    f"{missing_cts_methods}"
                )

        self.modules[prefix] = module
        self.paths[prefix] = path
        return module

    def load_all(self) -> Dict[str, Any]:
        loaded: Dict[str, Any] = {}
        for prefix in ("api_gateway", "market", "scanner", "intraday", "trading"):
            loaded[prefix] = self.load_one(prefix)
        return loaded

    def diagnose(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        all_ok = True
        for prefix, spec in CORE_SPECS.items():
            try:
                path = self.resolve(prefix)
                module = self.load_one(prefix)
                missing = _module_missing_members(module, spec["required"])
                item = {
                    "ok": not missing,
                    "module": prefix,
                    "required": list(spec["required"]),
                    "missing": missing,
                }
                if spec["parameter_class"]:
                    cls = getattr(module, spec["parameter_class"], None)
                    item["parameter_class"] = spec["parameter_class"]
                    item["parameter_class_ok"] = cls is not None and is_dataclass(cls)
                result[prefix] = item
                all_ok = all_ok and bool(item["ok"])
            except Exception as exc:
                result[prefix] = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                all_ok = False
        result["ok"] = all_ok
        return result


# =============================================================================
# 參數 Component 動態載入
# =============================================================================


def _load_parameter_component_module(base_dir: Path) -> Any:
    path = resolve_latest_module_path(Path(base_dir).resolve(), "params")
    module_name = path.stem
    existing = sys.modules.get(module_name)
    if existing is not None:
        module_file = Path(getattr(existing, "__file__", "") or "").resolve()
        if module_file == path.resolve():
            return existing

    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"無法建立參數 Component 載入規格：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_PARAMETER_COMPONENT_MODULE = _load_parameter_component_module(BASE_DIR)
GUISettings = _PARAMETER_COMPONENT_MODULE.GUISettings
ParameterRegistry = _PARAMETER_COMPONENT_MODULE.ParameterRegistry
ConfigStore = _PARAMETER_COMPONENT_MODULE.ConfigStore
ParameterSettingsComponent = _PARAMETER_COMPONENT_MODULE.ParameterSettingsComponent
PARAMETER_ROOT_NAME = _PARAMETER_COMPONENT_MODULE.PARAMETER_ROOT_NAME
PARAMETER_TABLE_HIDDEN_FIELDS = (
    _PARAMETER_COMPONENT_MODULE.PARAMETER_TABLE_HIDDEN_FIELDS
)
VALID_CONTROL_MODES = _PARAMETER_COMPONENT_MODULE.VALID_CONTROL_MODES
resolve_parameter_help_path = _PARAMETER_COMPONENT_MODULE.resolve_parameter_help_path
load_parameter_help = _PARAMETER_COMPONENT_MODULE.load_parameter_help
build_core_parameter_link_report = (
    _PARAMETER_COMPONENT_MODULE.build_core_parameter_link_report
)


# =============================================================================
# Runtime Component 動態載入
# =============================================================================


def _load_runtime_component_module(base_dir: Path) -> Any:
    path = resolve_latest_module_path(Path(base_dir).resolve(), "runtime")
    module_name = path.stem
    existing = sys.modules.get(module_name)
    if existing is not None:
        module_file = Path(getattr(existing, "__file__", "") or "").resolve()
        if module_file == path.resolve():
            return existing

    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"無法建立 Runtime Component 載入規格：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_RUNTIME_COMPONENT_MODULE = _load_runtime_component_module(BASE_DIR)
TradingSystemRuntime = _RUNTIME_COMPONENT_MODULE.TradingSystemRuntime
WorkerCoordinator = _RUNTIME_COMPONENT_MODULE.WorkerCoordinator
CANDIDATE_MAX_AGE_SECONDS = _RUNTIME_COMPONENT_MODULE.CANDIDATE_MAX_AGE_SECONDS
DAILY_TRADE_REPORT_PREFIX = _RUNTIME_COMPONENT_MODULE.DAILY_TRADE_REPORT_PREFIX


def _epoch_event_datetime(value: Any) -> Optional[datetime]:
    number = _safe_float(value)
    if number is None or number <= 0:
        return None
    try:
        if number > 10_000_000_000_000:
            seconds = number / 1_000_000.0
        elif number > 10_000_000_000:
            seconds = number / 1000.0
        else:
            seconds = number
        return datetime.fromtimestamp(
            seconds, tz=datetime.now(timezone(timedelta(hours=8))).tzinfo
        )
    except (ValueError, OSError, OverflowError):
        return None


def _latest_runtime_quote(snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    """只從 Runtime Snapshot 挑出最新 MarketState 行情；不讀 Gateway、不讀系統時間。"""
    quotes = snapshot.get("quotes")
    if not isinstance(quotes, Mapping):
        return {}
    rows: List[Dict[str, Any]] = []
    for key, raw in quotes.items():
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        if isinstance(key, tuple) and len(key) == 2:
            row.setdefault("symbol", str(key[0]))
            row.setdefault("market", str(key[1]))
        rows.append(row)
    if not rows:
        return {}
    rows.sort(
        key=lambda row: (
            _safe_int(row.get("quote_sequence"), 0),
            _safe_int(row.get("event_time"), 0),
            _safe_int(row.get("serial"), -1),
            str(row.get("symbol") or ""),
            str(row.get("market") or ""),
        )
    )
    return rows[-1]


def _event_latency_seconds(quote: Mapping[str, Any]) -> Optional[float]:
    """以同一 Runtime Snapshot 內的 received_at - event_time 計算顯示延遲。"""
    received = _parse_datetime_value(quote.get("received_at"))
    event_dt = _epoch_event_datetime(quote.get("event_time"))
    if event_dt is None:
        event_dt = _parse_datetime_value(quote.get("timestamp"))
    if received is None or event_dt is None:
        return _safe_float(quote.get("realtime_age_seconds"))
    try:
        return max(
            0.0, (received - event_dt.astimezone(received.tzinfo)).total_seconds()
        )
    except Exception:
        return None


def _snapshot_clock_age_seconds(snapshot_time: Any, event_time: Any) -> Optional[float]:
    current = _parse_datetime_value(snapshot_time)
    event = _parse_datetime_value(event_time)
    if current is None or event is None:
        return None
    try:
        return max(0.0, (current - event.astimezone(current.tzinfo)).total_seconds())
    except Exception:
        return None


def _stream_state_from_snapshot(
    snapshot: Mapping[str, Any],
    *,
    health_timeout_seconds: float,
) -> str:
    """將 Runtime 已發布的 Gateway/Stream 狀態轉成固定 GUI 顯示字串。"""
    gateway = snapshot.get("gateway_status")
    gateway = dict(gateway) if isinstance(gateway, Mapping) else {}
    mode = str(gateway.get("mode") or "").strip().upper()
    if mode == "OFFLINE":
        return "STOPPED"
    fugle = gateway.get("fugle")
    fugle = dict(fugle) if isinstance(fugle, Mapping) else {}
    connected = bool(fugle.get("connected"))
    authenticated = bool(fugle.get("authenticated"))
    last_error = str(fugle.get("last_error") or "").strip()
    subscriptions = fugle.get("subscriptions")
    subscriptions = dict(subscriptions) if isinstance(subscriptions, Mapping) else {}
    desired = subscriptions.get("desired")
    desired_count = (
        len(desired)
        if isinstance(desired, Sequence)
        and not isinstance(desired, (str, bytes, bytearray))
        else 0
    )

    if connected:
        last_message_at = fugle.get("last_message_at")
        snapshot_at = gateway.get("timestamp")
        age = _snapshot_clock_age_seconds(snapshot_at, last_message_at)
        if age is not None and age > max(0.0, float(health_timeout_seconds)):
            return "STALE"
        if last_message_at or desired_count == 0:
            return "READY"
        return "CONNECTING"
    if last_error:
        return "ERROR"
    if authenticated and (fugle.get("last_disconnect_at") or desired_count > 0):
        return "RECONNECTING"
    if authenticated:
        return "CONNECTING"
    return "STOPPED"


def _system_display_from_snapshot(
    snapshot: Mapping[str, Any],
    *,
    health_timeout_seconds: float,
) -> Dict[str, str]:
    """純顯示轉換：相同 Runtime Snapshot 必須產生完全相同系統設定內容。"""
    gateway = snapshot.get("gateway_status")
    gateway = dict(gateway) if isinstance(gateway, Mapping) else {}
    fugle = gateway.get("fugle")
    fugle = dict(fugle) if isinstance(fugle, Mapping) else {}
    source_status = snapshot.get("market_source_status")
    source_status = dict(source_status) if isinstance(source_status, Mapping) else {}
    event_driven = snapshot.get("event_driven")
    event_driven = dict(event_driven) if isinstance(event_driven, Mapping) else {}

    connected = bool(fugle.get("connected"))
    ws_text = "CONNECTED" if connected else "DISCONNECTED"
    provider = (
        str(gateway.get("market_provider") or fugle.get("provider") or "FUGLE").strip()
        or "FUGLE"
    )
    quote_status = fugle.get("quote_status")
    quote_status = dict(quote_status) if isinstance(quote_status, Mapping) else {}
    quote_source = str(quote_status.get("source") or "MARKET_STATE").strip()
    market_source = f"{provider} REST／{quote_source}"

    universe_source = str(source_status.get("active_universe_source") or "").strip()
    universe_state = (
        str(source_status.get("active_universe_state") or "").strip().upper()
    )
    universe_rows = _safe_int(source_status.get("active_universe_rows"), 0)
    if universe_source:
        parts = [universe_source]
        if universe_rows > 0:
            parts.append(f"{universe_rows} 檔")
        if universe_state:
            parts.append(universe_state)
        universe_text = "｜".join(parts)
    else:
        active_universe = fugle.get("active_universe")
        active_universe = (
            dict(active_universe) if isinstance(active_universe, Mapping) else {}
        )
        source = str(active_universe.get("source") or "FUGLE REST").strip()
        rows = _safe_int(
            active_universe.get("rows"), _safe_int(active_universe.get("count"), 0)
        )
        universe_text = source + (f"｜{rows} 檔" if rows > 0 else "")

    latest = _latest_runtime_quote(snapshot)
    if latest:
        symbol = str(latest.get("symbol") or "-")
        market = str(latest.get("market") or "-")
        event_time = str(latest.get("timestamp") or latest.get("event_time") or "-")
        serial = str(latest.get("serial") if latest.get("serial") is not None else "-")
        sequence = _safe_int(latest.get("quote_sequence"), 0)
        latest_event = (
            f"{market} {symbol}｜{event_time}｜serial {serial}｜seq {sequence}"
        )
        latency = _event_latency_seconds(latest)
        if latency is None:
            latency_text = "-"
        elif latency < 1.0:
            latency_text = f"{latency * 1000.0:.1f} ms"
        else:
            latency_text = f"{latency:.3f} s"
    else:
        latest_event = "尚無 Trade Event"
        latency_text = "-"

    priorities = event_driven.get("subscriptions")
    priorities = dict(priorities) if isinstance(priorities, Mapping) else {}
    total = _safe_int(priorities.get("total"), 0)
    p0 = _safe_int(priorities.get("p0"), 0)
    p1 = _safe_int(priorities.get("p1"), 0)
    p2 = _safe_int(priorities.get("p2"), 0)
    fugle_subscriptions = fugle.get("subscriptions")
    fugle_subscriptions = (
        dict(fugle_subscriptions) if isinstance(fugle_subscriptions, Mapping) else {}
    )
    active = fugle_subscriptions.get("active")
    active_count = (
        len(active)
        if isinstance(active, Sequence)
        and not isinstance(active, (str, bytes, bytearray))
        else 0
    )
    subscriptions_text = (
        f"Runtime {total}｜P0 {p0}／P1 {p1}／P2 {p2}｜WS active {active_count}"
    )

    return {
        "fugle_ws": ws_text,
        "market_source": market_source,
        "universe_source": universe_text or "-",
        "latest_event": latest_event,
        "event_latency": latency_text,
        "subscriptions": subscriptions_text,
        "stream_state": _stream_state_from_snapshot(
            snapshot,
            health_timeout_seconds=health_timeout_seconds,
        ),
        "cts": str((gateway.get("cts") or {}).get("connection_health") or ("已登入" if bool(gateway.get("cts_connected")) else "未登入")),
    }


# =============================================================================
# GUI 主框架
# =============================================================================


def _cts_query_errors(query_status: Any) -> str:
    if not isinstance(query_status, Mapping):
        return ""
    messages = []
    for key, label in (("position", "庫存"), ("order", "委託"), ("match", "成交")):
        item = query_status.get(key)
        if not isinstance(item, Mapping) or item.get("ok"):
            continue
        reason = str(item.get("raw_error") or item.get("status") or "未取得錯誤內容")
        day = item.get("query_trade_date")
        messages.append(f"{label}{'[' + str(day) + ']' if day else ''}：{reason}")
    return "；".join(messages)


class TradingGUI:

    def __init__(
        self,
        root: Any,
        loader: CoreModuleLoader,
        modules: Mapping[str, Any],
        registry: ParameterRegistry,
        settings: GUISettings,
        store: ConfigStore,
    ) -> None:
        import tkinter as tk
        from tkinter import messagebox, ttk

        self.tk = tk
        self.ttk = ttk
        self.messagebox = messagebox
        self.root = root
        self.loader = loader
        self.modules = dict(modules)
        self.registry = registry
        self.settings = settings
        self.store = store
        self.runtime = TradingSystemRuntime(modules, registry, settings)

        self.run_state = "STOPPED"
        self.market_refresh_after_id: Optional[str] = None
        self.session_policy_after_id: Optional[str] = None
        self.requested_control_mode = str(self.settings.control_mode).strip().upper()
        self.last_session_phase = ""
        self.pending_login_window: Any = None
        self.pending_login_values: Dict[str, Any] = {}
        self.selected_symbol = ""
        self.selected_market = ""
        self.last_rendered_run_flow_seq = 0
        self.last_rendered_execution_seq = 0
        self.last_gui_repaint_monotonic = 0.0
        self.pending_display_snapshot: Dict[str, Any] = {}

        self.root.title("自動交易系統｜GUI、自動策略／手動市價與可重現參數控制")
        self.root.geometry("1500x960")
        self.root.minsize(1180, 760)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.status_var = tk.StringVar(value="STOPPED")
        self.mode_var = tk.StringVar(value=self.requested_control_mode)
        self.cts_var = tk.StringVar(value="CTS 未登入")
        self.update_var = tk.StringVar(value="市場掃描：待命｜交易監控：待命")
        self.message_var = tk.StringVar(value="系統就緒")
        self.auto_execute_var = tk.BooleanVar(value=self.settings.auto_execute)

        self.workers = WorkerCoordinator(self, self.runtime)

        self.parameter_component = ParameterSettingsComponent(
            base_dir=BASE_DIR,
            root=self.root,
            tk=self.tk,
            ttk=self.ttk,
            messagebox=self.messagebox,
            registry=self.registry,
            store=self.store,
            get_settings=lambda: self.settings,
            apply_settings=self._apply_parameter_settings,
            read_control_values=self._read_parameter_control_values,
            effective_control_mode=self._effective_control_mode,
            module_files_provider=lambda: dict(self.loader.paths),
            set_message=self._set_message,
            request_refresh=self._request_refresh,
        )

        global _ERROR_JOURNAL
        _ERROR_JOURNAL = self.runtime.journal
        self._build_ui()
        self.workers.start()
        self.mode_var.set(self.requested_control_mode)
        self.auto_execute_var.set(self.settings.auto_execute)
        self._apply_session_policy(announce=False)
        self.parameter_component.refresh()
        self._update_system_labels()
        self.root.after(150, self.workers.poll_results)
        self._schedule_session_policy_check()

    def _build_ui(self) -> None:
        ttk = self.ttk

        top = ttk.Frame(self.root, padding=(10, 8))
        top.pack(fill="x")

        ttk.Label(top, text="模式").pack(side="left")
        mode_box = ttk.Combobox(
            top,
            textvariable=self.mode_var,
            values=("AUTO", "MANUAL"),
            width=8,
            state="readonly",
        )
        mode_box.pack(side="left", padx=(4, 8))
        mode_box.bind("<<ComboboxSelected>>", self._on_mode_changed)

        ttk.Button(top, text="START", command=self.start).pack(side="left", padx=2)
        ttk.Button(top, text="PAUSE", command=self.pause).pack(side="left", padx=2)
        ttk.Button(top, text="STOP", command=self.stop).pack(side="left", padx=2)
        ttk.Button(
            top, text="立即更新", command=lambda: self._request_refresh(True)
        ).pack(side="left", padx=2)
        ttk.Button(top, text="BUY", command=lambda: self._manual_action("BUY")).pack(
            side="left", padx=2
        )
        ttk.Button(top, text="SELL", command=lambda: self._manual_action("SELL")).pack(
            side="left", padx=2
        )
        ttk.Button(top, text="全部撤單", command=self._cancel_all_orders).pack(
            side="left", padx=2
        )
        ttk.Button(top, text="CLOSE ALL", command=self._close_all).pack(
            side="left", padx=2
        )
        ttk.Button(top, text="人工接管", command=self._manual_takeover).pack(
            side="left", padx=2
        )
        ttk.Checkbutton(
            top,
            text="AUTO 實際下單",
            variable=self.auto_execute_var,
            command=self._on_auto_execute_changed,
        ).pack(side="left", padx=4)
        ttk.Button(top, text="CTS 登出", command=self._cts_logout).pack(
            side="left", padx=2
        )
        ttk.Button(top, text="CTS 登入", command=self._show_login_dialog).pack(
            side="left", padx=2
        )

        status_bar = ttk.Frame(self.root, padding=(10, 2, 10, 2))
        status_bar.pack(fill="x")
        ttk.Label(status_bar, textvariable=self.status_var, width=12).pack(side="left")
        ttk.Label(status_bar, textvariable=self.cts_var, width=24).pack(side="left")
        ttk.Label(status_bar, textvariable=self.update_var).pack(
            side="left", fill="x", expand=True
        )

        self.safety_var = self.tk.StringVar(value="安全狀態：等待 Runtime")
        safety_bar = ttk.Frame(self.root, padding=(10, 2))
        safety_bar.pack(fill="x")
        ttk.Label(safety_bar, textvariable=self.safety_var).pack(side="left", fill="x", expand=True)
        ttk.Button(safety_bar, text="鎖定買入", command=lambda: self._submit_worker_command(
            "kill_switch", {"enabled": True})).pack(side="right")
        ttk.Button(safety_bar, text="解除買入鎖定", command=lambda: self._submit_worker_command(
            "kill_switch", {"enabled": False})).pack(side="right")

        # 系統提示獨立第二行，避免與 RUNNING／CTS／市場掃描狀態擠在同一列。
        message_bar = ttk.Frame(self.root, padding=(10, 0, 10, 6))
        message_bar.pack(fill="x")
        ttk.Label(message_bar, text="系統訊息：", width=10).pack(side="left")
        ttk.Label(message_bar, textvariable=self.message_var).pack(
            side="left", fill="x", expand=True
        )

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        dashboard = ttk.Frame(notebook, padding=8)
        settings_tab = ttk.Frame(notebook, padding=8)
        logs_tab = ttk.Frame(notebook, padding=8)
        notebook.add(dashboard, text="交易狀態")
        notebook.add(settings_tab, text="系統設定／參數")
        notebook.add(logs_tab, text="執行紀錄")

        self._build_dashboard(dashboard)
        self.parameter_component.build(settings_tab)
        self._build_logs(logs_tab)

    def _build_dashboard(self, parent: Any) -> None:
        ttk = self.ttk
        parent.rowconfigure(0, weight=0)
        parent.rowconfigure(1, weight=1)
        parent.rowconfigure(2, weight=1)
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=2)
        parent.columnconfigure(2, weight=1)

        market_frame = ttk.LabelFrame(parent, text="市場情況", padding=8)
        market_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 5), pady=(0, 5))
        self.market_vars: Dict[str, Any] = {}
        market_fields = [
            ("Market State", "market_state"),
            ("Intraday Trend", "intraday_trend"),
            ("Momentum Long", "momentum_long_allowed"),
            ("Trend Long", "trend_long_allowed"),
            ("Market Score", "market_score"),
            ("TAIEX", "taiex"),
            ("TPEX", "tpex"),
            ("市場廣度", "breadth"),
            ("最強類股", "strong_sectors"),
        ]
        for row_idx, (label, key) in enumerate(market_fields):
            ttk.Label(market_frame, text=label, width=16).grid(
                row=row_idx, column=0, sticky="w"
            )
            var = self.tk.StringVar(value="-")
            self.market_vars[key] = var
            if key == "strong_sectors":
                cell = ttk.Frame(market_frame, width=180, height=104)
                cell.grid(row=row_idx, column=1, sticky="ew")
                cell.grid_propagate(False)
                text = self.tk.Text(cell, wrap="char", height=5, width=1,
                                    borderwidth=0, highlightthickness=0,
                                    font="TkDefaultFont", takefocus=False)
                scroll = ttk.Scrollbar(cell, orient="vertical", command=text.yview)
                scroll.pack(side="right", fill="y")
                text.configure(yscrollcommand=scroll.set)
                text.pack(fill="both", expand=True)
                def update_sector_text(*_args, widget=text, variable=var):
                    if widget.get("1.0", "end-1c") == variable.get():
                        return
                    widget.configure(state="normal")
                    widget.delete("1.0", "end")
                    widget.insert("1.0", variable.get())
                    widget.configure(state="disabled")
                var.trace_add("write", update_sector_text)
                update_sector_text()
            elif key == "trend_long_allowed":
                cell = ttk.Frame(market_frame, width=180, height=42)
                cell.grid(row=row_idx, column=1, sticky="ew")
                cell.grid_propagate(False)
                label_widget = ttk.Label(cell, textvariable=var, anchor="w", justify="left", wraplength=180)
                label_widget.pack(fill="both", expand=True)
                cell.bind("<Configure>", lambda event, label=label_widget: label.configure(wraplength=max(40, event.width - 4)))
            else:
                ttk.Label(market_frame, textvariable=var).grid(
                    row=row_idx, column=1, sticky="w"
                )
        market_frame.columnconfigure(1, weight=1)

        system_frame = ttk.LabelFrame(parent, text="系統狀態", padding=8)
        system_frame.grid(row=0, column=1, sticky="nsew", padx=(5, 0), pady=(0, 5))
        self.system_vars: Dict[str, Any] = {}
        system_fields = [
            ("Fugle WS", "fugle_ws"),
            ("Fugle REST／行情來源", "market_source"),
            ("Universe 來源", "universe_source"),
            ("最新行情事件", "latest_event"),
            ("行情延遲", "event_latency"),
            ("本輪資格完成", "batch_status"),
            ("策略處理耗時", "strategy_latency"),
            ("訂閱數量", "subscriptions"),
            ("Stream 狀態", "stream_state"),
            ("CTS", "cts"),
            ("AUTO / MANUAL", "mode"),
            ("核心模組", "modules"),
            ("Trading 狀態", "trading_status"),
            ("策略監控", "strategy_runtime"),
            ("Trading 參數指紋", "trading_fingerprint"),
        ]
        for row_idx, (label, key) in enumerate(system_fields):
            ttk.Label(system_frame, text=label, width=18).grid(
                row=row_idx, column=0, sticky="w"
            )
            var = self.tk.StringVar(value="-")
            self.system_vars[key] = var
            ttk.Label(system_frame, textvariable=var).grid(
                row=row_idx, column=1, sticky="w"
            )

        risk_frame = ttk.LabelFrame(parent, text="風險狀態", padding=8)
        risk_frame.grid(row=0, column=2, sticky="nsew", padx=5, pady=(0, 5))
        self.risk_vars = {}
        for index, (key, label) in enumerate(
            (
                ("daily_realized_pnl", "當日已實現損益"),
                ("daily_loss_limit", "每日損失上限"),
                ("portfolio_open_risk", "總開放風險"),
                ("portfolio_risk_limit", "總風險上限"),
                ("position_count", "持倉檔數"),
                ("position_limit", "持倉檔數上限（非保證）"),
                ("remaining_open_risk", "剩餘可用風險"),
                ("exit_pending_symbols", "待完成出場"),
                ("daily_entry_count", "當日進場次數"),
                ("daily_entry_limit", "進場上限"),
                ("consecutive_losses", "連續虧損"),
                ("loss_cooldown", "冷卻截止"),
                ("sector_exposure", "類股曝險"),
                ("risk_gate", "Risk Gate"),
            )
        ):
            ttk.Label(risk_frame, text=label).grid(row=index, column=0, sticky="w")
            var = self.tk.StringVar(value="-")
            self.risk_vars[key] = var
            ttk.Label(risk_frame, textvariable=var, wraplength=260).grid(
                row=index, column=1, sticky="w"
            )
        candidate_frame = ttk.LabelFrame(parent, text="候選股票", padding=6)
        candidate_frame.grid(row=1, column=0, columnspan=3, sticky="nsew", pady=5)
        self.candidate_tree = self._tree(
            candidate_frame,
            columns=(
                "rank",
                "symbol",
                "name",
                "price",
                "change",
                "volume_ratio",
                "market_rs",
                "sector_rs",
                "score",
                "momentum_score",
                "trend_score",
                "momentum_eligible",
                "trend_eligible",
                "trade_ready",
                "day_trade",
                "market",
                "eligibility_reason",
            ),
            headings=(
                "排名",
                "股票",
                "名稱",
                "價格",
                "漲幅",
                "量比",
                "Market RS",
                "Sector RS",
                "Candidate Score",
                "Momentum Score",
                "Trend Score",
                "Momentum資格",
                "Trend資格",
                "Trade Ready",
                "當沖資格",
                "市場",
                "資格未成立原因",
            ),
            widths=(
                55,
                75,
                100,
                75,
                75,
                75,
                85,
                85,
                100,
                100,
                100,
                100,
                100,
                90,
                90,
                70,
                310,
            ),
        )
        self.candidate_tree.bind("<<TreeviewSelect>>", self._on_candidate_selected)

        bottom = ttk.Panedwindow(parent, orient="horizontal")
        bottom.grid(row=2, column=0, columnspan=3, sticky="nsew", pady=(5, 0))


        signal_frame = ttk.LabelFrame(bottom, text="盤中策略", padding=6)
        bottom.add(signal_frame, weight=55)
        self.signal_tree = self._tree(
            signal_frame,
            columns=(
                "symbol",
                "name",
                "strategy",
                "state",
                "setup",
                "vwap",
                "ema5",
                "ema20",
                "m1",
                "m3",
                "m5",
                "volume_exp",
                "range_exp",
                "structure",
                "wave",
                "pullback",
                "entry",
                "exit",
                "exit_stage",
                "exit_fraction",
                "signal_price",
                "stop",
                "emergency",
                "extension",
                "day_change",
                "candidate_input",
                "momentum_trace",
                "trend_trace",
                "rejection",
            ),
            headings=(
                "股票",
                "名稱",
                "Strategy",
                "State",
                "Entry Type",
                "VWAP",
                "EMA5",
                "EMA20",
                "1m Mom",
                "3m Mom",
                "5m Mom",
                "Volume Exp",
                "Range Exp",
                "Structure / Anchor / Age",
                "Wave No.",
                "Pullback",
                "Entry",
                "Exit",
                "Exit Stage",
                "Exit %",
                "Signal Price",
                "Strategy Stop",
                "Emergency Stop",
                "Extension %",
                "BUY 漲幅／上限",
                "本次輸入 M／T／Ready",
                "Momentum 判定",
                "Trend 判定",
                "Trading 阻擋",
            ),
            widths=(
                70,
                100,
                90,
                150,
                170,
                75,
                75,
                75,
                75,
                75,
                75,
                90,
                90,
                250,
                75,
                80,
                60,
                60,
                230,
                65,
                90,
                100,
                110,
                90,
                150,
                180,
                190,
                190,
                170,
            ),
        )
        self.signal_tree.configure(height=5)
        self.signal_tree.bind("<<TreeviewSelect>>", self._on_signal_selected)
        cols = list(self.signal_tree["columns"])
        priority = ["symbol","name","momentum_trace","trend_trace","rejection"]
        self.signal_tree.configure(displaycolumns=priority+[c for c in cols if c not in priority])
        trade_frame = ttk.LabelFrame(bottom, text="持倉", padding=6)
        bottom.add(trade_frame, weight=45)
        bottom.after_idle(lambda: bottom.sashpos(0, int(bottom.winfo_width()*0.55)))

        performance = ttk.LabelFrame(trade_frame, text="交易績效／紀錄", padding=5)
        performance.pack(fill="x", pady=(0, 6))
        self.trade_vars = {
            "last_trade": self.tk.StringVar(value="-"),
            "trade_pnl": self.tk.StringVar(value="-"),
            "trade_return_pct": self.tk.StringVar(value="-"),
            "daily_realized_pnl": self.tk.StringVar(value="-"),
            "daily_return_pct": self.tk.StringVar(value="-"),
            "daily_pnl": self.tk.StringVar(value="-"),
            "available_cash": self.tk.StringVar(value="-"),
            "daily_trade_count": self.tk.StringVar(value="0"),
            "position_count": self.tk.StringVar(value="0"),
            "realized_exit_count": self.tk.StringVar(value="0"),
            "completed_position_count": self.tk.StringVar(value="0"),
        }
        summary_items = [
            ("最近交易", "last_trade"),
            ("本次淨效益", "trade_pnl"),
            ("本次報酬率", "trade_return_pct"),
            ("當日已實現淨效益", "daily_realized_pnl"),
            ("當日報酬率", "daily_return_pct"),
            ("當日總損益", "daily_pnl"),
            ("可用資金", "available_cash"),
            ("今日進場次數", "daily_trade_count"),
            ("目前持倉檔數", "position_count"),
            ("SELL 委託數", "realized_exit_count"),
            ("完整平倉次數", "completed_position_count"),
        ]
        for idx, (label, key) in enumerate(summary_items):
            row, col = divmod(idx, 3)
            base = col * 2
            ttk.Label(performance, text=label).grid(
                row=row, column=base, sticky="w", padx=(4, 2), pady=2
            )
            ttk.Label(
                performance,
                textvariable=self.trade_vars[key],
                wraplength=150,
                justify="left",
            ).grid(row=row, column=base + 1, sticky="w", padx=(2, 10), pady=2)

        self.position_tree = self._tree(
            trade_frame,
            columns=(
                "symbol",
                "name",
                "strategy",
                "state",
                "entry",
                "initial",
                "remaining",
                "managed",
                "current",
                "return_pct",
                "unrealized",
                "stop",
                "emergency",
                "open_risk",
                "partial",
                "sell_summary",
                "oid",
                "management_source",
                "security_type",
            ),
            headings=(
                "股票",
                "名稱",
                "Strategy",
                "State",
                "成交價",
                "初始張數",
                "剩餘張數",
                "系統管理張數",
                "現價",
                "報酬率",
                "未實現損益",
                "Strategy Stop",
                "Emergency Stop",
                "Open Risk",
                "Partial Stage",
                "SELL",
                "OID",
                "管理來源",
                "證券類型",
            ),
            widths=(
                70,
                100,
                90,
                140,
                75,
                80,
                80,
                95,
                75,
                75,
                95,
                100,
                110,
                90,
                180,
                250,
                100,
                100,
                100,
            ),
        )
        self.position_tree.bind("<<TreeviewSelect>>", self._on_position_selected)

    def _update_tree_row(self, tree, iid, *, values, index=None):
        rendered = tuple(str(x) for x in values)
        if tree.exists(iid):
            if tuple(str(x) for x in tree.item(iid, "values")) != rendered:
                tree.item(iid, values=values)
            if index is not None:
                # 只有目前位置與目標順序不同才搬動；避免每次刷新都對每一列
                # 呼叫 tree.move()（大量列時會造成畫面週期性頓卡）。
                if tree.index(iid) != index:
                    tree.move(iid, "", index)
        else:
            if index is None:
                tree.insert("", "end", iid=iid, values=values)
            else:
                tree.insert("", "end", iid=iid, values=values)
                tree.move(iid, "", index)

    def _tree(
        self,
        parent: Any,
        *,
        columns: Sequence[str],
        headings: Sequence[str],
        widths: Sequence[int],
    ) -> Any:
        ttk = self.ttk
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="browse"
        )
        vs = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        hs = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        for column, heading, width in zip(columns, headings, widths):
            tree.heading(column, text=heading)
            tree.column(column, width=width, minwidth=50, anchor="center")
        return tree

    def _build_logs(self, parent: Any) -> None:
        ttk = self.ttk
        top = ttk.Frame(parent)
        top.pack(fill="x", pady=(0, 6))
        ttk.Button(top, text="清除畫面", command=self._clear_logs).pack(side="left")
        self.log_follow_var = self.tk.BooleanVar(value=True)
        self.last_error_var = self.tk.StringVar(value="最近錯誤：無")
        ttk.Checkbutton(top, text="自動捲動", variable=self.log_follow_var).pack(
            side="left", padx=6
        )
        ttk.Label(top, text="只顯示買賣委託、成交及異常").pack(side="left", padx=6)
        ttk.Label(
            parent,
            textvariable=self.last_error_var,
            foreground="#a00000",
            wraplength=1100,
        ).pack(fill="x")
        ttk.Button(top, text="診斷核心模組", command=self._show_diagnose).pack(
            side="left", padx=6
        )

        paths = ttk.Frame(parent)
        paths.pack(fill="x", pady=(0, 6))
        self.run_log_file_var = self.tk.StringVar(
            value=str(self.runtime.run_flow_log_path)
        )
        self.execution_log_file_var = self.tk.StringVar(
            value=str(self.runtime.execution_log_path)
        )
        self.recent_error_file_var = self.tk.StringVar(
            value=str(self.runtime.recent_error_log_path)
        )
        ttk.Label(paths, text="交易／異常統一紀錄檔：").grid(row=0, column=0, sticky="w")
        ttk.Label(paths, textvariable=self.execution_log_file_var).grid(row=0, column=1, sticky="w")
        paths.columnconfigure(1, weight=1)

        self.log_text = self.tk.Text(parent, wrap="none", height=20)
        self.log_text.pack(fill="both", expand=True)

    def _read_parameter_control_values(self) -> Dict[str, Any]:
        return {
            "control_mode": self.mode_var.get().strip().upper(),
            "auto_execute": bool(self.auto_execute_var.get()),
        }

    def _apply_parameter_settings(
        self,
        settings: GUISettings,
        *,
        requested_mode: Optional[str] = None,
        announce: bool = False,
        apply_session_policy: bool = False,
    ) -> None:
        settings.validate()
        if requested_mode is not None:
            normalized = str(requested_mode).strip().upper()
            if normalized not in VALID_CONTROL_MODES:
                raise ValueError("control_mode 僅允許 AUTO / MANUAL")
            self.requested_control_mode = normalized
        self.settings = settings
        self.runtime.update_settings(settings)
        if apply_session_policy:
            self._apply_session_policy(announce=announce)
        self.mode_var.set(self.requested_control_mode)
        self.auto_execute_var.set(bool(self.settings.auto_execute))
        self._update_system_labels()

    @staticmethod
    def _clock_minutes(now: Optional[datetime] = None) -> int:
        current = now or datetime.now(timezone(timedelta(hours=8)))
        return int(current.hour) * 60 + int(current.minute)

    @classmethod
    def _session_phase(cls, now: Optional[datetime] = None) -> str:
        minutes = cls._clock_minutes(now)
        auto_switch = AUTO_SWITCH_HOUR * 60 + AUTO_SWITCH_MINUTE
        buy_start = AUTO_BUY_START_HOUR * 60 + AUTO_BUY_START_MINUTE
        if minutes < auto_switch:
            return "PREOPEN"
        if minutes < buy_start:
            return "AUTO_WARMUP"
        return "BUY_OPEN"

    def _effective_control_mode(
        self,
        *,
        requested_mode: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> str:
        requested = str(requested_mode or self.requested_control_mode).strip().upper()
        if requested not in VALID_CONTROL_MODES:
            requested = "MANUAL"
        if requested == "AUTO" and self._session_phase(now) == "PREOPEN":
            return "MANUAL"
        return requested

    def _automatic_new_buy_allowed(self, now: Optional[datetime] = None) -> bool:
        return (
            self.run_state == "RUNNING"
            and str(self.settings.control_mode).strip().upper() == "AUTO"
            and bool(self.settings.auto_execute)
            and self._session_phase(now) == "BUY_OPEN"
        )


    def _apply_session_policy(
        self, *, now: Optional[datetime] = None, announce: bool = False
    ) -> None:
        current = now or datetime.now(timezone(timedelta(hours=8)))
        phase = self._session_phase(current)
        effective_mode = self._effective_control_mode(now=current)
        previous_mode = str(self.settings.control_mode).strip().upper()
        previous_phase = self.last_session_phase

        if previous_mode != effective_mode:
            values = asdict(self.settings)
            values["control_mode"] = effective_mode
            self.settings = GUISettings(**values)
            self.settings.validate()
            self.runtime.update_settings(self.settings)

        self.mode_var.set(self.requested_control_mode)
        self.last_session_phase = phase
        self._update_system_labels()

        mode_changed = previous_mode != effective_mode
        phase_changed = previous_phase != phase
        if announce and mode_changed:
            if effective_mode == "MANUAL" and self.requested_control_mode == "AUTO":
                self._set_message(
                    "AUTO 已待命；開盤前實際維持 MANUAL，09:00 自動切 AUTO。"
                )
            elif effective_mode == "AUTO" and phase == "AUTO_WARMUP":
                self._set_message("09:00 已自動切 AUTO；09:05 前禁止新的自動 BUY。")
            elif effective_mode == "AUTO" and phase == "BUY_OPEN":
                self._set_message(
                    "AUTO 已啟用；09:05 後符合全部條件才允許新的自動 BUY。"
                )

        if mode_changed or phase_changed:
            self._record_gui_event(
                "SESSION_POLICY",
                details={
                    "previous_mode": previous_mode,
                    "effective_mode": effective_mode,
                    "previous_phase": previous_phase,
                    "phase": phase,
                    "policy_time": current.isoformat(timespec="seconds"),
                },
            )

        if (mode_changed or phase_changed) and self.run_state in {"RUNNING", "PAUSED"}:
            # 09:00 MANUAL→AUTO 時 MarketGuard 必須立即重算；Trading 迴圈也立即跑一次。
            self._request_market_refresh(force=True, reason="SESSION_POLICY")
            self._request_trading_refresh()

    def _schedule_session_policy_check(self) -> None:
        if (
            self.session_policy_after_id is not None
            or self.workers.worker_shutdown.is_set()
        ):
            return

        def callback() -> None:
            self.session_policy_after_id = None
            if self.workers.worker_shutdown.is_set():
                return
            try:
                self._apply_session_policy(announce=False)
            finally:
                self._schedule_session_policy_check()

        self.session_policy_after_id = self.root.after(SESSION_POLICY_POLL_MS, callback)

    def _cancel_session_policy_check(self) -> None:
        if self.session_policy_after_id is None:
            return
        try:
            self.root.after_cancel(self.session_policy_after_id)
        except Exception:
            pass
        self.session_policy_after_id = None

    def start(self) -> None:
        try:
            self.parameter_component.sync_control_settings()
            self._apply_session_policy(announce=False)
        except Exception as exc:
            self.messagebox.showerror("設定錯誤", f"{type(exc).__name__}: {exc}")
            return
        self._cancel_scheduled_refresh()
        self.run_state = "RUNNING"
        self.runtime.set_runtime_policy(strategy_enabled=True, pause_blocks_buy=False)
        self.status_var.set("RUNNING")
        if self.requested_control_mode == "AUTO" and self._session_phase() == "PREOPEN":
            self._set_message(
                "系統已啟動；AUTO 已待命，開盤前實際維持 MANUAL，09:00 自動切 AUTO。"
            )
        elif (
            self.settings.control_mode == "AUTO"
            and self._session_phase() == "AUTO_WARMUP"
        ):
            self._set_message("系統已啟動；目前 AUTO，09:05 前禁止新的自動 BUY。")
        else:
            self._set_message("系統已啟動")
        self._record_gui_event(
            "START",
            details={
                "session_phase": self._session_phase(),
                "automatic_new_buy_allowed": self._automatic_new_buy_allowed(),
            },
        )
        self._request_market_refresh(force=True, reason="START")
        self._request_trading_refresh()
        self._schedule_market_refresh()

    def pause(self) -> None:
        self.run_state = "PAUSED"
        self.runtime.set_runtime_policy(strategy_enabled=True, pause_blocks_buy=True)
        self.status_var.set("PAUSED")
        self._set_message(
            "PAUSE：禁止新的 BUY；Market、Scanner、行情、持倉監控與既有 SELL 委託追蹤持續"
        )
        self._record_gui_event("PAUSE", details={"pause_blocks_buy": True})
        self._schedule_market_refresh()

    def stop(self) -> None:
        self._cancel_scheduled_refresh()
        self.run_state = "STOPPED"
        self.runtime.set_runtime_policy(strategy_enabled=False, pause_blocks_buy=False)
        self.status_var.set("STOPPED")
        self._set_message(
            "STOP：停止策略循環；不自動撤單、不自動平倉；若有持倉仍持續被動監控"
        )
        self._record_gui_event("STOP", details={"strategy_enabled": False})

    def _cancel_scheduled_refresh(self) -> None:
        for attr in ("market_refresh_after_id",):
            after_id = getattr(self, attr, None)
            if after_id is None:
                continue
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
            setattr(self, attr, None)

    def _request_refresh(self, force_market: bool = False) -> None:
        """更新入口：force_market=True 要求 Market/Scanner；否則要求 Trading 快迴圈。"""
        if force_market:
            self._request_market_refresh(force=True, reason="MANUAL_REFRESH")
        else:
            self._request_trading_refresh()

    def _request_trading_refresh(self) -> None:
        self.workers.request_trading_refresh()

    def _schedule_market_refresh(self) -> None:
        if self.market_refresh_after_id is not None or self.run_state not in {
            "RUNNING",
            "PAUSED",
        }:
            return
        delay_ms = max(1000, int(float(self.settings.market_refresh_seconds) * 1000))

        def callback() -> None:
            self.market_refresh_after_id = None
            if self.run_state in {"RUNNING", "PAUSED"}:
                self._request_market_refresh(force=False, reason="INTERVAL")
                self._schedule_market_refresh()

        self.market_refresh_after_id = self.root.after(delay_ms, callback)

    def _request_market_refresh(self, *, force: bool, reason: str) -> None:
        self.workers.request_market_refresh(force=force, reason=reason)

    def _update_runtime_status(self) -> None:
        self.workers.update_runtime_status()

    def _submit_worker_command(
        self, command: str, payload: Optional[Mapping[str, Any]] = None
    ) -> bool:
        return self.workers.submit_command(command, payload)

    def _on_mode_changed(self, _event: Any = None) -> None:
        requested_mode = self.mode_var.get().strip().upper()
        if requested_mode not in VALID_CONTROL_MODES:
            self.messagebox.showerror(
                "模式設定錯誤", "control_mode 僅允許 AUTO / MANUAL"
            )
            return
        try:
            self.requested_control_mode = requested_mode
            self._apply_session_policy(announce=True)
            self.parameter_component.refresh()
            self._update_system_labels()
            self._record_gui_event(
                "MODE_REQUEST",
                details={
                    "requested_mode": requested_mode,
                    "session_phase": self._session_phase(),
                },
            )
        except Exception as exc:
            self.messagebox.showerror("模式設定錯誤", f"{type(exc).__name__}: {exc}")

    def _on_auto_execute_changed(self) -> None:
        values = asdict(self.settings)
        values["auto_execute"] = bool(self.auto_execute_var.get())
        try:
            self.settings = GUISettings(**values)
            self.settings.validate()
            self.runtime.update_settings(self.settings)
            self._apply_session_policy(announce=False)
            self.parameter_component.refresh()
            self._record_gui_event(
                "AUTO_EXECUTE_TOGGLE",
                details={
                    "enabled": bool(self.settings.auto_execute),
                    "session_phase": self._session_phase(),
                },
            )
            if self.requested_control_mode == "AUTO" and self.settings.auto_execute:
                phase = self._session_phase()
                if phase == "PREOPEN":
                    self._set_message(
                        "AUTO 實際下單已啟用；目前仍為 MANUAL，09:00 自動切 AUTO，09:05 才開放自動新 BUY。"
                    )
                elif phase == "AUTO_WARMUP":
                    self._set_message(
                        "AUTO 實際下單已啟用；目前 AUTO，但 09:05 前新的自動 BUY 仍封鎖。"
                    )
        except Exception as exc:
            self.messagebox.showerror("AUTO 設定錯誤", f"{type(exc).__name__}: {exc}")

    def _show_login_dialog(self) -> None:
        window = self.tk.Toplevel(self.root)
        window.title("中國信託 CTS 登入")
        window.transient(self.root)
        window.grab_set()
        window.resizable(False, False)
        ttk = self.ttk

        try:
            gateway = self.runtime.gateway
            cts = getattr(gateway, "cts", None) if gateway is not None else None
            api_prog_id = str(getattr(cts, "com_prog_id", "") or "")
        except Exception:
            api_prog_id = ""

        uid_var = self.tk.StringVar(value=self.settings.cts_uid)
        password_var = self.tk.StringVar(value="")
        account_var = self.tk.StringVar(value=self.settings.cts_account_id)
        prog_id_var = self.tk.StringVar(
            value=self.settings.cts_com_prog_id or api_prog_id
        )
        das_var = self.tk.StringVar(
            value=self.settings.cts_trade_das or "api.win168.com.tw/tradedas"
        )

        fields_data = [
            ("登入帳號／身分證字號", uid_var, False),
            ("交易密碼", password_var, True),
            ("證券帳號（空白＝登入後自動取得）", account_var, False),
            ("COM ProgID（API 預設，可覆寫）", prog_id_var, False),
            ("Trade DAS", das_var, False),
        ]
        first_entry = None
        for idx, (label, var, secret) in enumerate(fields_data):
            ttk.Label(window, text=label, width=29).grid(
                row=idx, column=0, sticky="e", padx=10, pady=5
            )
            entry = ttk.Entry(
                window,
                textvariable=var,
                width=34,
                show="*" if secret else "",
            )
            entry.grid(row=idx, column=1, sticky="w", padx=10, pady=5)
            if first_entry is None:
                first_entry = entry
        ttk.Label(
            window,
            text=(
                "登入成功後由 CTS GetAccountList() 取得證券帳號；"
                "Password 只存在目前登入流程，不寫入 GUI 參數檔。"
            ),
            wraplength=545,
        ).grid(row=5, column=0, columnspan=2, sticky="w", padx=12, pady=(5, 2))

        def do_login() -> None:
            uid = uid_var.get().strip()
            password = password_var.get()
            trade_das = das_var.get().strip()
            if not uid or not password:
                self.messagebox.showerror(
                    "CTS 登入",
                    "登入帳號／身分證字號與交易密碼不得空白。",
                    parent=window,
                )
                return
            if not trade_das:
                self.messagebox.showerror(
                    "CTS 登入", "Trade DAS 不得空白。", parent=window
                )
                return

            payload = {
                "trade_das": trade_das,
                "uid": uid,
                "password": password,
                "account_id": account_var.get().strip(),
                "prog_id": prog_id_var.get().strip(),
            }
            self.pending_login_window = window
            self.pending_login_values = {
                "password_var": password_var,
                "trade_das": trade_das,
                "uid": uid,
                "prog_id": prog_id_var.get().strip(),
            }
            window.configure(cursor="watch")
            self._set_message("CTS 登入中…")
            if not self._submit_worker_command("cts_login", payload):
                window.configure(cursor="")
                password_var.set("")
                self.pending_login_window = None
                self.pending_login_values = {}

        buttons = ttk.Frame(window)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", padx=12, pady=8)
        ttk.Button(buttons, text="登入", command=do_login).pack(side="right")
        ttk.Button(buttons, text="取消", command=window.destroy).pack(
            side="right", padx=6
        )

        if first_entry is not None:
            first_entry.focus_set()

    def _handle_cts_login_result(self, result: Mapping[str, Any]) -> None:
        window = self.pending_login_window
        values_cache = dict(self.pending_login_values)
        password_var = values_cache.get("password_var")
        try:
            if password_var is not None:
                password_var.set("")
            values = asdict(self.settings)
            values["cts_trade_das"] = str(values_cache.get("trade_das") or "")
            values["cts_uid"] = str(values_cache.get("uid") or "")
            values["cts_account_id"] = str(result.get("account_id") or "").strip()
            values["cts_com_prog_id"] = str(
                result.get("prog_id") or values_cache.get("prog_id") or ""
            ).strip()
            self.settings = GUISettings(**values)
            self.settings.validate()
            self.runtime.update_settings(self.settings)
            self.cts_var.set(f"CTS 已登入｜{self.settings.cts_account_id}")
            self._update_system_labels(self.runtime.display_snapshot())
            self.parameter_component.refresh()
            self._set_message(
                "CTS 登入成功｜證券帳號 "
                + self.settings.cts_account_id
                + (
                    f"｜{result.get('account_source')}"
                    if result.get("account_source")
                    else ""
                )
            )
            if window is not None:
                window.destroy()
        finally:
            self.pending_login_window = None
            self.pending_login_values = {}

    def _handle_worker_error(self, payload: Mapping[str, Any]) -> None:
        command = str(payload.get("command") or "")
        message = str(payload.get("message") or "更新失敗")
        if command == "cts_login":
            window = self.pending_login_window
            password_var = self.pending_login_values.get("password_var")
            if password_var is not None:
                try:
                    password_var.set("")
                except Exception:
                    pass
            if window is not None:
                try:
                    window.configure(cursor="")
                except Exception:
                    pass
                self.messagebox.showerror("CTS 登入失敗", message, parent=window)
            self.pending_login_window = None
            self.pending_login_values = {}
            self._set_message("CTS 登入失敗")
            return
        if command == "cts_logout":
            self.messagebox.showerror("CTS 登出失敗", message)
        else:
            self._set_message(message)

    def _cts_logout(self) -> None:
        self._submit_worker_command("cts_logout")

    def _manual_action(self, action: str) -> None:
        if not self.selected_symbol or not self.selected_market:
            self.messagebox.showinfo(
                "手動交易", "請先在候選股、盤中訊號或持倉表選擇股票。"
            )
            return
        strategy_owner = ""
        if not self.messagebox.askyesno(
            "確認交易",
            f"確認送出 {action}：{self.selected_symbol} {self.selected_market}？\n"
            "將直接送出市價委託；BUY 依單倉資金上限換算整張，SELL 依 CTS 可賣整張數。",
        ):
            return
        self._submit_worker_command(
            "manual_trade",
            {
                "symbol": self.selected_symbol,
                "market": self.selected_market,
                "action": action,
                "strategy_owner": strategy_owner,
            },
        )

    def _cancel_all_orders(self) -> None:
        if not self.messagebox.askyesno(
            "全部撤單",
            "確定要取消 CTS 目前所有未完成委託？\n此操作只撤單，不會自動平倉。",
        ):
            return
        self._submit_worker_command("cancel_all")

    def _manual_takeover(self) -> None:
        if not self.selected_symbol or not self.selected_market:
            self.messagebox.showinfo("人工接管", "請先在持倉表選擇要接管的股票。")
            return
        if not self.messagebox.askyesno(
            "人工接管",
            f"確認人工接管 {self.selected_symbol} 的有效 SELL 委託？\n"
            "接管後 GUI 不再對該選定 OID 啟動自動 SELL 委託／成交追蹤；不會自動撤單或平倉。",
        ):
            return
        self._submit_worker_command(
            "manual_takeover",
            {"symbol": self.selected_symbol, "market": self.selected_market},
        )

    def _close_all(self) -> None:
        if not self.messagebox.askyesno(
            "CLOSE ALL",
            "確定要平倉本系統『今日管理部位』？\n"
            "CTS 原有／外部庫存不會被 CLOSE ALL 賣出；實際賣出量仍會先與 CTS 庫存核對。\n"
            "CLOSE ALL 不會先執行全部撤單。",
        ):
            return
        self._submit_worker_command("close_all")

    def _render_snapshot(
        self,
        snapshot: Mapping[str, Any],
        *,
        force: bool = False,
    ) -> None:
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("GUI Widget 更新只能在 Tk main thread 執行")
        frozen_snapshot = dict(snapshot or {})
        self.pending_display_snapshot = frozen_snapshot
        interval = max(0.1, float(self.settings.quote_refresh_seconds))
        now_monotonic = time.monotonic()
        if (
            not force
            and self.last_gui_repaint_monotonic > 0
            and now_monotonic - self.last_gui_repaint_monotonic < interval
        ):
            return
        self.last_gui_repaint_monotonic = now_monotonic
        snapshot = frozen_snapshot
        market = snapshot.get("market_context") or {}
        scanner = snapshot.get("scanner_context") or {}
        signals = snapshot.get("signals") or {}
        positions = snapshot.get("positions") or {}
        gateway_status = snapshot.get("gateway_status") or {}
        lifecycles = snapshot.get("sell_lifecycles") or {}
        quotes = snapshot.get("quotes") or {}

        self.market_vars["market_state"].set(str(market.get("market_state") or "-"))
        self.market_vars["market_score"].set(self._fmt(market.get("market_score"), 2))
        self.market_vars["taiex"].set(self._format_index(market.get("taiex")))
        self.market_vars["tpex"].set(self._format_index(market.get("tpex")))
        self.market_vars["breadth"].set(self._format_breadth(market.get("breadth")))
        self.market_vars["strong_sectors"].set(
            _format_sector_status(market)
        )

        for key in ("intraday_trend", "momentum_long_allowed", "trend_long_allowed"):
            value = market.get(key)
            self.market_vars[key].set(
                ("允許" if value else "禁止")
                if isinstance(value, bool)
                else str(value or "-")
            )
        # 更新：顯示同一套市場許可原因，長文字限制在原欄寬自動換行。
        permission = market.get("trend_permission")
        if isinstance(permission, Mapping):
            labels = {
                "INTRADAY_UP": "允許（短線向上）",
                "STRONG_MARKET_PULLBACK": "條件允許（強勢回檔）",
                "STRONG_MARKET_SIDEWAYS": "條件允許（強勢整理）",
                "MARKET_NOT_STRONG": "禁止（強度未達門檻）",
                "PANIC": "禁止（恐慌盤）", "MARKET_WEAK": "禁止（弱勢盤）",
                "MARKET_LONG_BLOCKED": "禁止（市場關閉做多）",
                "MARKET_PERMISSION_EXPIRED": "禁止（市場資料已逾時）",
            }
            self.market_vars["trend_long_allowed"].set(labels.get(permission["reason"], "禁止（市場許可資料失效）"))
        safety = snapshot.get("safety_status") or {}
        risk = dict(snapshot.get("risk_snapshot") or {})
        if safety:
            heartbeat = safety.get("execution_heartbeat_age")
            heartbeat_text = "尚未啟動" if heartbeat is None else f"{heartbeat:.1f} 秒"
            self.safety_var.set(f"{safety.get('recovery', '')}｜交易心跳 {heartbeat_text}｜"
                f"委託佇列 {safety.get('callback_queue_depth', 0)}｜行情佇列 {safety.get('market_queue_depth', 0)}｜"
                f"保留資金 {risk.get('reserved_buy_amount', 0):,.0f}｜"
                f"寫入狀態 {safety.get('journal_error') or '正常'}｜"
                f"行情待處理 {safety.get('raw_trade_queue_depth', 0)}｜"
                f"背景恢復 {'等待對帳' if safety.get('worker_buy_blocked') else '正常'}｜"
                f"行情完整性 {'異常' if safety.get('ingress_failed') else '正常'}")
            risk["risk_gate"] = (str(risk.get("risk_gate") or "") + " / "
                + str(safety.get("recovery") or "")
                + f" / 委託佇列 {safety.get('callback_queue_depth', 0)}"
                + f" / 行情佇列 {safety.get('market_queue_depth', 0)}"
                + (" / 日損鎖定" if safety.get("daily_loss_lock") else "")
                + (" / 停機鎖定" if safety.get("kill_switch") else ""))
        
        for key, var in self.risk_vars.items():
            var.set(str(risk.get(key) if risk.get(key) is not None else "-"))
        candidate_active = set()

        for candidate_index, row in enumerate(scanner.get("candidates") or []):
            if not isinstance(row, Mapping):
                continue
            rs = row.get("market_relative_strength")
            if rs is None:
                rs = row.get("sector_relative_strength")
            eligible = row.get("day_trade_eligible")
            status = str(row.get("day_trade_status") or "").strip().upper()
            if status in {"ELIGIBLE", "YES", "TRUE"} or eligible is True:
                day_trade = "可當沖"
            elif status in {"INELIGIBLE", "NO", "FALSE"} or eligible is False:
                day_trade = "不可當沖"
            else:
                day_trade = str(row.get("day_trade_status") or "未知")
            iid = f"{row.get('market','')}:{row.get('symbol','')}"
            candidate_active.add(iid)
            self._update_tree_row(
                self.candidate_tree,
                iid,
                index=candidate_index,
                values=(
                    row.get("rank", ""),
                    row.get("symbol", ""),
                    row.get("name", ""),
                    self._fmt(row.get("price"), 2),
                    self._fmt_pct(row.get("change_pct")),
                    self._fmt(row.get("volume_ratio"), 2),
                    self._fmt(row.get("market_relative_strength"), 2),
                    self._fmt(row.get("sector_relative_strength"), 2),
                    self._fmt(row.get("candidate_score"), 2),
                    self._fmt(row.get("momentum_candidate_score"), 2),
                    self._fmt(row.get("trend_candidate_score"), 2),
                    str(row.get("momentum_eligible", "-")),
                    str(row.get("trend_eligible", "-")),
                    str(row.get("trade_ready", "-")),
                    day_trade,
                    row.get("market", ""),
                    "；".join(("M:" + ",".join(row.get("momentum_eligibility_reasons") or []), "T:" + ",".join(row.get("trend_eligibility_reasons") or []))),
                ),
            )

        candidate_names = {
            (str(row.get("symbol") or ""), str(row.get("market") or "")): str(
                row.get("name") or ""
            )
            for row in scanner.get("candidates") or []
            if isinstance(row, Mapping)
        }
        external_names = {
            str(row.get("symbol") or ""): _clean_security_name(row.get("name"))
            for row in snapshot.get("external_position_rows") or []
            if isinstance(row, Mapping) and str(row.get("symbol") or "")
        }
        trading_diagnostics_by_signal: Dict[str, Dict[str, Any]] = {}
        trading_diagnostics_by_wave: Dict[str, Dict[str, Any]] = {}
        execution_records = [
            record
            for record in (snapshot.get("execution_log") or [])
            if isinstance(record, Mapping)
        ]
        execution_records.sort(key=lambda item: _safe_int(item.get("seq"), 0))
        for record in execution_records:
            trading_input = (
                record.get("trading_input")
                if isinstance(record.get("trading_input"), Mapping)
                else {}
            )
            logged_signal = (
                trading_input.get("signal_context")
                if isinstance(trading_input.get("signal_context"), Mapping)
                else {}
            )
            passthrough = (
                dict(record.get("wave_passthrough") or {})
                if isinstance(record.get("wave_passthrough"), Mapping)
                else _strategy_passthrough(
                    logged_signal,
                    (
                        record.get("decision")
                        if isinstance(record.get("decision"), Mapping)
                        else {}
                    ),
                    (
                        record.get("execution")
                        if isinstance(record.get("execution"), Mapping)
                        else {}
                    ),
                )
            )
            signal_fingerprint = str(logged_signal.get("signal_fingerprint") or "")
            wave_id = str(logged_signal.get("entry_wave_id") or "")
            if signal_fingerprint:
                trading_diagnostics_by_signal[signal_fingerprint] = passthrough
            if wave_id:
                trading_diagnostics_by_wave[wave_id] = passthrough

        signal_active = set()
        for signal_index, (key, signal) in enumerate(signals.items()):
            if not isinstance(signal, Mapping):
                continue
            if isinstance(key, tuple) and len(key) == 2:
                symbol, market_name = str(key[0]), str(key[1])
            else:
                symbol = str(signal.get("symbol") or "")
                market_name = str(signal.get("market") or "")
            metrics = signal.get("metrics") or {}
            conditions = signal.get("entry_conditions") or {}
            name = candidate_names.get((symbol, market_name)) or str(
                (signal.get("candidate") or {}).get("name") or ""
            )
            breakout = metrics.get("breakout_short_high")
            if breakout is None:
                breakout = conditions.get("breakout_short_high")
            iid = f"{market_name}:{symbol}"
            signal_fingerprint = str(signal.get("signal_fingerprint") or "")
            wave_id = str(signal.get("entry_wave_id") or "")
            trading_diagnostics = (
                trading_diagnostics_by_signal.get(signal_fingerprint)
                or {}
            )
            rejection_codes = [
                str(value)
                for value in (trading_diagnostics.get("trading_rejection_codes") or [])
                if str(value)
            ]
            signal_active.add(iid)
            self._update_tree_row(
                self.signal_tree,
                iid,
                index=signal_index,
                values=(
                    symbol,
                    name,
                    signal.get("strategy_type", "-"),
                    signal.get("strategy_state", "-"),
                    signal.get("entry_setup_type", "-"),
                    self._fmt(metrics.get("vwap"), 2),
                    self._fmt(metrics.get("ema5"), 2),
                    self._fmt(metrics.get("ema20"), 2),
                    self._fmt_pct(metrics.get("momentum_1m")),
                    self._fmt_pct(metrics.get("momentum_3m")),
                    self._fmt_pct(metrics.get("momentum_5m")),
                    self._fmt(metrics.get("volume_expansion_ratio"), 2),
                    self._fmt(metrics.get("range_expansion_ratio"), 2),
                    str(signal.get("entry_wave_start_time") or "-")
                    + " / "
                    + str(signal.get("entry_structure_reference") or "-")
                    + " / "
                    + str(signal.get("entry_wave_age_bars", "-")),
                    (
                        metrics.get("momentum_wave_number", "-")
                        if signal.get("strategy_type") == "MOMENTUM"
                        else "-"
                    ),
                    str(metrics.get("first_pullback", "-")),
                    "YES" if signal.get("entry_signal") else "NO",
                    "YES" if signal.get("exit_signal") else "NO",
                    signal.get("exit_stage", "-"),
                    self._fmt_pct(100 * float(signal.get("exit_fraction") or 0)),
                    self._fmt(signal.get("signal_price"), 2),
                    self._fmt(signal.get("strategy_stop_price"), 2),
                    self._fmt(signal.get("emergency_stop_price"), 2),
                    self._fmt_pct(signal.get("entry_wave_extension_pct")),
                    self._fmt_pct((trading_diagnostics.get("day_change_gate") or {}).get("day_change_pct")) + " / " + self._fmt_pct((trading_diagnostics.get("day_change_gate") or {}).get("max_entry_day_change_pct")),
                    ("待本輪重算：" if signal.get("candidate_snapshot_fingerprint") and signal.get("candidate_snapshot_fingerprint") != scanner.get("data_fingerprint") else "本輪：") + " / ".join(str((signal.get("candidate") or {}).get(field, "-")) for field in ("momentum_eligible", "trend_eligible", "trade_ready")),
                    self._entry_summary((signal.get("entry_paths") or {}).get("MOMENTUM") or {}),
                    self._entry_summary((signal.get("entry_paths") or {}).get("TREND") or {}),
                    (rejection_codes[0] if rejection_codes else "-"),
                ),
            )

        self._update_trade_summary(snapshot, candidate_names)
        self.trade_vars["completed_position_count"].set(
            str(
                (snapshot.get("account_state") or {}).get("completed_position_count", 0)
            )
        )
        self.system_vars["strategy_runtime"].set(
            str(snapshot.get("strategy_runtime") or "-")
        )

        position_active = set()
        lot_size = self._current_lot_size()
        for position_index, (key, position) in enumerate(positions.items()):
            if not isinstance(position, Mapping):
                continue
            if isinstance(key, tuple) and len(key) == 2:
                symbol, market_name = str(key[0]), str(key[1])
                lifecycle = (
                    lifecycles.get(key) if isinstance(lifecycles, Mapping) else None
                )
                quote = quotes.get(key) if isinstance(quotes, Mapping) else None
            else:
                symbol = str(position.get("symbol") or "")
                market_name = str(position.get("market") or "")
                lifecycle = None
                quote = None
            quote_name = (
                _clean_security_name(quote.get("name"))
                if isinstance(quote, Mapping)
                else ""
            )
            name = (
                _clean_security_name(position.get("name"))
                or _clean_security_name(candidate_names.get((symbol, market_name), ""))
                or _clean_security_name(external_names.get(symbol, ""))
                or quote_name
            )
            quantity = position.get("quantity")
            if quantity is None:
                quantity = position.get("filled_shares")
            shares = _safe_float(quantity)
            lots = None if shares is None else shares / lot_size
            managed_shares = _safe_float(position.get("managed_quantity"))
            managed_lots = None if managed_shares is None else managed_shares / lot_size
            management_source = str(position.get("management_source") or "CTS 外部")
            if position.get("stale"):
                management_source += "｜stale：保留上次有效庫存"
            elif position.get("position_reconciliation_pending"):
                management_source += "｜個股成交待核對"

            entry_price = _safe_float(position.get("entry_price"))
            current_price = _safe_float(position.get("current_price"))
            if current_price is None and isinstance(quote, Mapping):
                current_price = _safe_float(quote.get("price"))

            position_value = _safe_float(position.get("position_value"))
            if (
                position_value is None
                and entry_price is not None
                and entry_price > 0
                and shares is not None
                and shares > 0
            ):
                position_value = entry_price * shares

            unrealized_pnl = _safe_float(position.get("unrealized_pnl"))
            if unrealized_pnl is None:
                unrealized_pnl = _safe_float(position.get("unrealized_profit_loss"))
            if (
                unrealized_pnl is None
                and entry_price is not None
                and entry_price > 0
                and current_price is not None
                and current_price > 0
                and shares is not None
                and shares > 0
            ):
                unrealized_pnl = (current_price - entry_price) * shares

            return_pct = _safe_float(position.get("return_pct"))
            if return_pct is None:
                return_pct = _safe_float(position.get("pnl_pct"))
            if (
                return_pct is None
                and entry_price is not None
                and entry_price > 0
                and current_price is not None
                and current_price > 0
            ):
                return_pct = (current_price - entry_price) / entry_price * 100.0

            lifecycle = lifecycle if isinstance(lifecycle, Mapping) else {}
            execution = (
                lifecycle.get("execution")
                if isinstance(lifecycle.get("execution"), Mapping)
                else {}
            )
            sell_status = (
                "人工接管"
                if lifecycle.get("manual_takeover")
                else str(execution.get("status") or "-")
            )
            decision = (
                lifecycle.get("decision")
                if isinstance(lifecycle.get("decision"), Mapping)
                else {}
            )
            sell_price = decision.get("order_price")
            fill_summary = (
                execution.get("fill_summary")
                if isinstance(execution.get("fill_summary"), Mapping)
                else {}
            )
            sell_fill_price = fill_summary.get("average_fill_price")
            if sell_fill_price is None:
                sell_fill_price = execution.get("exit_fill_price")
            sell_filled = fill_summary.get("filled_cts_quantity")
            expected_cts_quantity = _safe_int(decision.get("cts_quantity"), 0)
            filled_cts_quantity = _safe_int(sell_filled, 0)
            sell_remaining = (
                max(0, expected_cts_quantity - filled_cts_quantity)
                if expected_cts_quantity > 0 else None
            )
            sell_summary = self._fmt_sell_summary(
                status=sell_status,
                order_price=sell_price,
                fill_price=sell_fill_price,
                remaining=sell_remaining,
            )
            oid = str(execution.get("order_id") or "")

            iid = f"{market_name}:{symbol}"
            position_active.add(iid)
            self._update_tree_row(
                self.position_tree,
                iid,
                index=position_index,
                values=(
                    symbol,
                    name,
                    position.get("strategy_type", "-"),
                    position.get("strategy_state", "-"),
                    self._fmt(entry_price, 2),
                    self._fmt_quantity(
                        float(position.get("initial_quantity") or 0) / lot_size
                    ),
                    self._fmt_quantity(
                        float(position.get("remaining_quantity", quantity) or 0)
                        / lot_size
                    ),
                    self._fmt_quantity(managed_lots),
                    self._fmt(current_price, 2),
                    self._fmt_pct(return_pct),
                    self._fmt_money(unrealized_pnl),
                    self._fmt(position.get("strategy_stop_price"), 2),
                    self._fmt(position.get("emergency_stop_price"), 2),
                    self._fmt_money(position.get("open_risk")),
                    position.get("partial_exit_stage", "-"),
                    (
                        "PARTIAL｜"
                        if 0
                        < float(
                            execution.get("exit_fraction")
                            or decision.get("exit_fraction")
                            or 1
                        )
                        < 1
                        else "FULL｜"
                    )
                    + sell_summary,
                    oid or "-",
                    management_source,
                    str(
                        position.get("security_type_name")
                        or (quote or {}).get("security_type_name")
                        or "待確認"
                    ),
                ),
            )

        for tree, active in (
            (self.candidate_tree, candidate_active),
            (self.signal_tree, signal_active),
            (self.position_tree, position_active),
        ):
            obsolete = set(tree.get_children()) - active
            if obsolete:
                tree.delete(*obsolete)
        connected = bool(gateway_status.get("cts_connected"))
        if connected:
            account_id = str(self.settings.cts_account_id or "").strip()
            self.cts_var.set(
                f"CTS 已登入｜{account_id}" if account_id else "CTS 已登入"
            )
        else:
            self.cts_var.set("CTS 未登入")
        self._update_system_labels(snapshot)
        self._render_new_log_records(snapshot)

    @staticmethod
    def _run_flow_record_is_abnormal(record: Mapping[str, Any]) -> bool:
        status = str(record.get("status") or "").strip().upper()
        stage = str(record.get("stage") or "").strip().upper()
        details = record.get("details")
        detail_map = dict(details) if isinstance(details, Mapping) else {}

        # BUY freshness 的正常政策性封鎖（MANUAL、AUTO OFF、PAUSE、市場不允許多方）
        # 僅用於最近錯誤提示；一般政策性封鎖不加入紀錄。
        if stage == "BUY_FRESHNESS_GATE" and status == "BLOCK":
            reasons = {str(item) for item in (detail_map.get("reasons") or [])}
            abnormal_reasons = {
                "CANDIDATE_NOT_READY",
                "CANDIDATE_STALE_OR_TIME_UNKNOWN",
                "MARKET_GUARD_NOT_READY",
            }
            return bool(reasons & abnormal_reasons)

        if status in {
            "WARN",
            "WARNING",
            "ERROR",
            "ERROR_SUMMARY",
            "REJECTED",
            "STATUS_UNKNOWN",
            "BLOCK",
            "DEGRADED",
            "FAILED",
            "FAIL",
            "TIMEOUT",
            "MISSING",
            "REJECT",
        }:
            return True
        if detail_map.get("error") or detail_map.get("errors"):
            return True
        if bool(detail_map.get("timed_out")):
            return True
        return False

    def _render_new_log_records(self, snapshot: Mapping[str, Any]) -> None:
        flow_records = [
            record
            for record in (snapshot.get("run_flow_log") or [])
            if isinstance(record, Mapping)
        ]
        flow_records.sort(key=lambda item: _safe_int(item.get("seq"), 0))
        for record in flow_records:
            sequence = _safe_int(record.get("seq"), 0)
            if sequence <= self.last_rendered_run_flow_seq:
                continue
            if record.get("stage") != "POSITION_LIVENESS":
                self._append_run_flow_record(record)
            self.last_rendered_run_flow_seq = max(
                self.last_rendered_run_flow_seq, sequence
            )

        execution_records = [
            record
            for record in (snapshot.get("execution_log") or [])
            if isinstance(record, Mapping)
        ]
        execution_records.sort(key=lambda item: _safe_int(item.get("seq"), 0))
        for record in execution_records:
            sequence = _safe_int(record.get("seq"), 0)
            if sequence <= self.last_rendered_execution_seq:
                continue
            self._append_execution_record(record)
            self.last_rendered_execution_seq = max(
                self.last_rendered_execution_seq, sequence
            )

        if hasattr(self, "run_log_file_var"):
            self.run_log_file_var.set(
                str(snapshot.get("run_flow_log_path") or self.runtime.run_flow_log_path)
            )
        if hasattr(self, "execution_log_file_var"):
            self.execution_log_file_var.set(
                str(
                    snapshot.get("execution_log_path")
                    or self.runtime.execution_log_path
                )
            )
        if hasattr(self, "recent_error_file_var"):
            self.recent_error_file_var.set(
                str(
                    snapshot.get("recent_error_log_path")
                    or self.runtime.recent_error_log_path
                )
            )

    def _append_run_flow_record(self, record: Mapping[str, Any]) -> None:
        run_id = str(record.get("run_id") or "-")
        stage = str(record.get("stage") or "-")
        status = str(record.get("status") or "-")
        duration = _safe_float(record.get("duration_ms"))
        duration_text = "" if duration is None else f" {duration:.1f}ms"
        details = record.get("details")
        detail_text = ""
        if isinstance(details, Mapping) and details:
            keys = ("candidate_count", "active_universe_rows", "execution_attempt", "broker_submit_attempt", "broker_accepted", "broker_rejected", "broker_unknown", "symbol", "order_intent_id", "order_id", "order_no", "return_code", "return_message", "primary_reason", "message", "cts_reason", "reason", "error", "market_error")
            detail_text = " " + " ".join(f"{k}={details[k]}" for k in keys if details.get(k) not in (None, ""))[:400]
        if stage == "POSITION_LIVENESS":
            detail_text = " " + str((details or {}).get("message") or (details or {}).get("reason") or status)
        if self._run_flow_record_is_abnormal(record):
            message = str(
                (details or {}).get("error")
                or (details or {}).get("message")
                or (details or {}).get("reason")
                or (details or {}).get("market_error")
                or (details or {}).get("cts_reason")
                or status
            )
            query_errors = _cts_query_errors((details or {}).get("cts_query_status"))
            if query_errors:
                message += "｜" + query_errors
            self.last_error_var.set(
                f"最近錯誤：{record.get('time', '')} {stage} {message[:1200]}"
            )
        self._append_log(
            f"[RUN {run_id}] [{stage}] [{status}]{duration_text}{detail_text}",
            timestamp=str(record.get("time") or _now_text()),
        )

    def _append_execution_record(self, record: Mapping[str, Any]) -> None:
        decision = (
            record.get("decision")
            if isinstance(record.get("decision"), Mapping)
            else {}
        )
        execution = (
            record.get("execution")
            if isinstance(record.get("execution"), Mapping)
            else {}
        )
        symbol = str(record.get("symbol") or (decision or {}).get("symbol") or "")
        market = str(record.get("market") or (decision or {}).get("market") or "")
        action = str(record.get("action") or (decision or {}).get("action") or "")
        decision_state = str((decision or {}).get("decision") or "")
        execution_state = (
            str(record.get("status") or (execution or {}).get("status") or "")
            if isinstance(execution, Mapping)
            else ""
        )
        summary = " ".join(
            part
            for part in (symbol, market, action, decision_state, execution_state)
            if part
        )
        passthrough = (
            dict(record.get("wave_passthrough") or {})
            if isinstance(record.get("wave_passthrough"), Mapping)
            else _strategy_passthrough(
                (
                    (record.get("trading_input") or {}).get("signal_context")
                    if isinstance(record.get("trading_input"), Mapping)
                    else {}
                ),
                decision,
                execution,
            )
        )
        rejection_codes = [
            str(value)
            for value in (passthrough.get("trading_rejection_codes") or [])
            if str(value)
        ]
        extension = _safe_float(passthrough.get("entry_wave_extension_pct"))
        if extension is not None:
            summary = f"{summary} WAVE_EXT={extension:.4f}%".strip()
        if rejection_codes:
            summary = f"{summary} REJECT={','.join(rejection_codes)}".strip()
        fill = execution.get("fill_summary") or {}
        order_price = execution.get("order_price") or decision.get("order_price") or (execution.get("order_request") or {}).get("price")
        quantity = fill.get("filled_shares")
        average = fill.get("average_fill_price")
        order_id = execution.get("order_id") or record.get("order_id")
        for label, value in (("委託價", order_price), ("成交股數", quantity), ("成交均價", average), ("委託", order_id), ("出場", execution.get("exit_stage"))):
            if value is not None:
                summary += f" {label}={value}"
        trigger = str(record.get("trigger") or "-")
        self._append_log(
            f"[TRADE {trigger}] {summary or str(record.get("status") or "交易狀態更新")}",
            timestamp=str(record.get("time") or _now_text()),
        )

    def _current_lot_size(self) -> float:
        bundle = self.registry.bundles.get("trading")
        value = (
            getattr(bundle.current, "lot_size", 1000) if bundle is not None else 1000
        )
        number = _safe_float(value)
        return number if number is not None and number > 0 else 1000.0

    def _fmt_sell_summary(
        self,
        *,
        status: Any,
        order_price: Any,
        fill_price: Any,
        remaining: Any,
    ) -> str:
        status_text = str(status or "").strip()
        order_value = _safe_float(order_price)
        fill_value = _safe_float(fill_price)
        remaining_value = _safe_float(remaining)
        if (
            status_text in {"", "-"}
            and order_value is None
            and fill_value is None
            and remaining_value is None
        ):
            return "-"
        return (
            f"{status_text or '-'}｜"
            f"委託 {self._fmt(order_value, 2)}｜"
            f"成交 {self._fmt(fill_value, 2)}｜"
            f"剩餘 {self._fmt_quantity(remaining_value)}"
        )

    @staticmethod
    def _fmt_quantity(value: Any) -> str:
        number = _safe_float(value)
        if number is None:
            return "-"
        if float(number).is_integer():
            return str(int(number))
        return f"{number:.3f}".rstrip("0").rstrip(".")

    @staticmethod
    def _fmt_money(value: Any) -> str:
        number = _safe_float(value)
        return "-" if number is None else f"{number:,.0f}"

    def _update_trade_summary(
        self,
        snapshot: Mapping[str, Any],
        candidate_names: Mapping[Tuple[str, str], str],
    ) -> None:
        metrics = snapshot.get("daily_trade_metrics")
        if not isinstance(metrics, Mapping):
            metrics = {}

        report_rows = [
            dict(row)
            for row in (snapshot.get("daily_trade_report") or [])
            if isinstance(row, Mapping)
        ]
        report_rows.sort(key=self.runtime._daily_report_record_sort_key)
        last_trade = metrics.get("last_trade")
        if not isinstance(last_trade, Mapping):
            last_trade = report_rows[-1] if report_rows else {}

        symbol = (
            str(last_trade.get("symbol") or "")
            if isinstance(last_trade, Mapping)
            else ""
        )
        market = (
            str(last_trade.get("market") or "")
            if isinstance(last_trade, Mapping)
            else ""
        )
        name = (
            _clean_security_name(last_trade.get("name"))
            if isinstance(last_trade, Mapping)
            else ""
        ) or candidate_names.get((symbol, market), "")
        action = (
            str(last_trade.get("action") or "")
            if isinstance(last_trade, Mapping)
            else ""
        )
        trigger = (
            str(last_trade.get("trigger") or "")
            if isinstance(last_trade, Mapping)
            else ""
        )
        last_text = (
            " ".join(part for part in (symbol, name, action, trigger) if part).strip()
            or "-"
        )
        self.trade_vars["last_trade"].set(last_text)

        trade_pnl = _safe_float(metrics.get("last_trade_pnl"))
        trade_return = _safe_float(metrics.get("last_trade_return_pct"))
        self.trade_vars["trade_pnl"].set(
            "-"
            if trade_pnl is None
            else f"{trade_pnl:,.2f}{'（估）' if metrics.get('last_trade_estimated') else ''}"
        )
        self.trade_vars["trade_return_pct"].set(
            "-"
            if trade_return is None
            else f"{trade_return:.4f}%{'（估）' if metrics.get('last_trade_estimated') else ''}"
        )

        daily_realized = _safe_float(metrics.get("daily_realized_pnl"))
        equity = _safe_float((snapshot.get("account_state") or {}).get("equity"))
        daily_return = (
            daily_realized / equity * 100
            if daily_realized is not None and equity and equity > 0
            else None
        )
        incomplete = _safe_int(metrics.get("unknown_sell_rows"), 0) > 0
        suffix = ("（估）" if metrics.get("fees_estimated") else "") + (
            "（部分）" if incomplete else ""
        )
        self.trade_vars["daily_realized_pnl"].set(
            "-" if daily_realized is None else f"{daily_realized:,.2f}{suffix}"
        )
        self.trade_vars["daily_return_pct"].set(
            "-" if daily_return is None else f"{daily_return:.4f}%{suffix}"
        )

        # 當日總損益僅用可證明成本的已實現報表 + 目前可證明成本的未實現部位；
        # CTS 外部未知成本不臆測，若存在未知項則標記「部分」。
        positions = snapshot.get("positions") or {}
        position_rows = (
            list(positions.values())
            if isinstance(positions, Mapping)
            else (
                list(positions)
                if isinstance(positions, Sequence)
                and not isinstance(positions, (str, bytes, bytearray))
                else []
            )
        )
        unrealized_total = 0.0
        known_open = 0
        unknown_open = 0
        for row in position_rows:
            if not isinstance(row, Mapping):
                continue
            quantity = max(0, _safe_int(row.get("quantity"), 0))
            if quantity <= 0:
                continue
            entry_price = _safe_float(row.get("entry_price"))
            current_price = _safe_float(row.get("current_price"))
            if current_price is None:
                quote = (
                    (snapshot.get("quotes") or {}).get(
                        (str(row.get("symbol") or ""), str(row.get("market") or "")),
                        {},
                    )
                    if isinstance(snapshot.get("quotes"), Mapping)
                    else {}
                )
                current_price = (
                    _safe_float(quote.get("price"))
                    if isinstance(quote, Mapping)
                    else None
                )
            if entry_price is None or entry_price <= 0 or current_price is None:
                unknown_open += 1
                continue
            known_open += 1
            unrealized_total += (current_price - entry_price) * quantity

        daily_total = None
        if daily_realized is not None or known_open > 0:
            daily_total = (daily_realized or 0.0) + unrealized_total
        total_partial = incomplete or unknown_open > 0
        self.trade_vars["daily_pnl"].set(
            "-"
            if daily_total is None
            else f"{daily_total:,.2f}{'（估）' if metrics.get('fees_estimated') else ''}{'（部分）' if total_partial else ''}"
        )

        account_state = snapshot.get("account_state") or {}
        available_cash = (
            _safe_float(account_state.get("available_cash"))
            if isinstance(account_state, Mapping)
            else None
        )
        self.trade_vars["available_cash"].set(
            "-" if available_cash is None else f"{available_cash:,.0f}"
        )
        report_buy_count = _safe_int(metrics.get("buy_count"), 0)
        account_trade_count = (
            _safe_int(account_state.get("daily_trade_count"), 0)
            if isinstance(account_state, Mapping)
            else 0
        )
        self.trade_vars["daily_trade_count"].set(
            str(max(report_buy_count, account_trade_count))
        )
        position_count = sum(
            1
            for item in position_rows
            if isinstance(item, Mapping) and _safe_int(item.get("quantity"), 0) > 0
        )
        reconcile = snapshot.get("cts_reconcile") or {}
        query = reconcile.get("query_status", {}).get("position", {})
        self.trade_vars["position_count"].set(
            str(position_count) if query.get("ok") else f"{position_count}（尚未確認）"
        )
        self.trade_vars["realized_exit_count"].set(
            str(_safe_int(metrics.get("sell_count"), 0))
        )

    def _update_system_labels(
        self, snapshot: Optional[Mapping[str, Any]] = None
    ) -> None:
        # GUI 僅呈現 Runtime Snapshot；不得從 GUI 直接讀 Gateway 或改變 Stream／策略狀態。
        frozen = dict(self.runtime.display_snapshot() if snapshot is None else snapshot)
        health_timeout = _safe_float(
            getattr(self.settings, "fugle_stream_health_timeout_seconds", 30.0)
        )
        display = _system_display_from_snapshot(
            frozen,
            health_timeout_seconds=(30.0 if health_timeout is None else health_timeout),
        )
        for key in (
            "fugle_ws",
            "market_source",
            "universe_source",
            "latest_event",
            "event_latency",
            "subscriptions",
            "stream_state",
            "cts",
        ):
            variable = self.system_vars.get(key)
            if variable is not None:
                variable.set(display.get(key, "-"))

        gateway_status = frozen.get("gateway_status")
        gateway_status = (
            dict(gateway_status) if isinstance(gateway_status, Mapping) else {}
        )
        batch = gateway_status.get("fugle", {}).get("batch", {})
        errors = _cts_query_errors(
            (frozen.get("cts_reconcile") or {}).get("query_status")
        )
        if errors and hasattr(self, "last_error_var"):
            self.last_error_var.set("CTS 查詢異常：" + errors[:1200])
        self.system_vars["batch_status"].set(
            f"{batch.get('completed_count',0)}/{batch.get('requested_count',0)}；{batch.get('duration_ms',0):.0f} ms；"
            + ("完成" if batch.get("complete") else "等待／缺漏")
        )
        latency = frozen.get("event_driven", {}).get("latency", {})
        self.system_vars["strategy_latency"].set(
            f"{latency.get('latest_strategy_cycle_ms',0):.1f} ms；佇列等待 {latency.get('latest_queue_wait_ms',0):.1f} ms"
        )
        cts_connected = bool(gateway_status.get("cts_connected"))
        account_id = str(self.settings.cts_account_id or "").strip()
        self.cts_var.set(
            f"CTS 已登入｜{account_id}"
            if cts_connected and account_id
            else "CTS 已登入" if cts_connected else "CTS 未登入"
        )
        health = (gateway_status.get("cts") or {}).get("connection_health")
        if health:
            self.cts_var.set("CTS "+str(health)+(f"｜{account_id}" if account_id else ""))

        self.system_vars["mode"].set(
            str(self.settings.control_mode).strip().upper()
        )
        module_names = ", ".join(key for key in self.loader.paths)
        self.system_vars["modules"].set(module_names or "-")
        self.system_vars["trading_status"].set(
            str(frozen.get("trading_status") or "IDLE")
        )
        self.system_vars["trading_fingerprint"].set(
            str(frozen.get("trading_parameter_fingerprint") or "-")
        )
        self._update_runtime_status()

    @staticmethod
    def _fmt(value: Any, digits: int = 2) -> str:
        number = _safe_float(value)
        return "-" if number is None else f"{number:.{digits}f}"

    @classmethod
    def _fmt_pct(cls, value: Any) -> str:
        number = _safe_float(value)
        return "-" if number is None else f"{number:.2f}%"

    @classmethod
    def _format_index(cls, value: Any) -> str:
        if not isinstance(value, Mapping):
            return "-"
        price = value.get("price")
        change = value.get("change_pct")
        return f"{cls._fmt(price, 2)} ({cls._fmt_pct(change)})"

    @classmethod
    def _format_breadth(cls, value: Any) -> str:
        if not isinstance(value, Mapping):
            return "-"
        adv = value.get("advancers")
        dec = value.get("decliners")
        ratio = value.get("ratio")
        return (
            f"上漲 {adv if adv is not None else '-'} / "
            f"下跌 {dec if dec is not None else '-'} / "
            f"Ratio {cls._fmt(ratio, 3)}"
        )

    def _on_candidate_selected(self, _event: Any = None) -> None:
        selection = self.candidate_tree.selection()
        if not selection:
            return
        self.selected_symbol = str(self.candidate_tree.set(selection[0], "symbol"))
        self.selected_market = str(self.candidate_tree.set(selection[0], "market"))
        self._set_message(f"已選擇 {self.selected_symbol} {self.selected_market}")

    @staticmethod
    def _entry_summary(path):
        reasons = path.get("rejection_reasons") or []
        if not path.get("eligible"):
            reason = next((r for r in reasons if any(x in r for x in ("SCORE","SECTOR","RS","INELIGIBLE"))), "資格未符")
            return "資格："+reason
        if path.get("entry_signal"):
            return "進場："+str(path.get("setup"))
        gates = path.get("formation_gates") or {}
        labels = {"breakout":"突破", "volume":"量能", "range":"振幅", "vwap":"VWAP", "vwap_slope":"VWAP 方向"}
        failed = [label for name,label in labels.items() if isinstance(gates.get(name),Mapping) and not gates[name].get("ok")]
        if "early_ready" in gates:
            failed = [label for name,label in (("above_vwap","VWAP"),("ema_fast_above_slow","EMA"),("ema_slope_up","斜率"),("rolling_hh","HH"),("rolling_hl","HL")) if not gates.get(name)]
        return str(path.get("state") or "等待")+"："+("/".join(failed) or (reasons[0] if reasons else "等待"))

    def _on_signal_selected(self, _event: Any = None) -> None:
        selection = self.signal_tree.selection()
        if not selection:
            return
        iid = selection[0]
        if ":" in iid:
            market, symbol = iid.split(":", 1)
            self.selected_symbol = symbol
            self.selected_market = market
            self._set_message(f"已選擇 {symbol} {market}")

    def _on_position_selected(self, _event: Any = None) -> None:
        selection = self.position_tree.selection()
        if not selection:
            return
        iid = selection[0]
        if ":" in iid:
            market, symbol = iid.split(":", 1)
            self.selected_symbol = symbol
            self.selected_market = market
            self._set_message(f"已選擇持倉 {symbol} {market}")

    def _set_message(self, text: str) -> None:
        self.message_var.set(str(text))

    def _record_gui_event(
        self,
        action: str,
        *,
        status: str = "EVENT",
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "action": str(action),
            "run_state": str(self.run_state),
            "requested_control_mode": str(self.requested_control_mode),
            "effective_control_mode": str(self.settings.control_mode),
            "auto_execute": bool(self.settings.auto_execute),
        }
        if details:
            payload.update(dict(details))
        run_id = self.runtime._new_run_id("GUI")
        self.runtime._record_run_flow(
            run_id=run_id,
            stage="GUI_CONTROL",
            status=status,
            details=payload,
        )

    def _append_log(self, text: str, *, timestamp: str = "") -> None:
        # 超長錯誤／批次 JSON 留在完整檔案；避免 Tk 橫向排版累積巨量字元。
        if len(text) > 2000:
            text = text[:2000] + " …（完整內容見執行紀錄檔）"
        if not text:
            return
        stamp = str(timestamp or _now_text())
        self.log_text.insert("end", f"[{stamp}] {text}\n")
        if self.log_follow_var.get():
            self.log_text.see("end")
        if int(self.log_text.index("end-1c").split(".")[0]) > 1500:
            self.log_text.delete("1.0", "201.0")

    def _clear_logs(self) -> None:
        self.log_text.delete("1.0", "end")

    def _show_diagnose(self) -> None:
        result = self.loader.diagnose()
        self._append_log(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        self._set_message("核心模組診斷完成")

    def _on_close(self) -> None:
        self.run_state = "STOPPED"
        self._cancel_scheduled_refresh()
        self._cancel_session_policy_check()
        self.runtime.set_runtime_policy(strategy_enabled=False, pause_blocks_buy=False)
        self.workers.shutdown()
        self.root.destroy()


# =============================================================================
# 診斷
# =============================================================================


def diagnose(base_dir: Path = BASE_DIR) -> Dict[str, Any]:
    loader = CoreModuleLoader(base_dir)
    result = loader.diagnose()
    result["base_dir"] = str(Path(base_dir).resolve())
    result["dynamic_load_rule"] = (
        "prefix_v*.py 最新版優先；不存在版本檔才退回 prefix.py"
    )
    result["parameter_store"] = str(Path(base_dir) / PARAMETER_ROOT_NAME)
    result["component_modules"] = {
        "params": "params",
        "runtime": "runtime",
    }
    help_path = resolve_parameter_help_path(Path(base_dir))
    help_sections = load_parameter_help(help_path)
    result["parameter_help"] = {
        "ok": help_path.is_file() and bool(help_sections),
        "resource": "參數說明",
        "sections": len(help_sections),
    }
    resolve_latest_module_path(Path(base_dir).resolve(), "runtime")
    runtime_classes = (
        "RuntimeJournalComponent",
        "RuntimePositionComponent",
        "RuntimeMarketScannerComponent",
        "RuntimeIntradayComponent",
        "RuntimeExecutionComponent",
        "WorkerCoordinator",
        "TradingSystemRuntime",
    )
    result["runtime_components"] = {
        "ok": all(hasattr(_RUNTIME_COMPONENT_MODULE, name) for name in runtime_classes),
        "classes": list(runtime_classes),
        "policy": "主 GUI 不持有 Runtime／Worker 實作；TradingSystemRuntime 保存共享狀態、Event Queue 與高階 cycle，功能由獨立 Component 分責",
    }
    result["position_monitoring"] = (
        "intraday_watch_count 只限制候選股；所有持倉持續加入盤中／出場監控；"
        "STOP 僅保留被動行情、CTS 庫存與委託狀態監控"
    )
    result["cts_threading"] = (
        "CTSOwner 為唯一 COM 執行緒；背景對帳讀取、交易命令優先佇列、Callback pump 都由此執行緒處理"
    )
    result["runtime_workers"] = (
        "MarketScannerWorker 依 market_refresh_seconds 維護 Market/Scanner；TradingSystemWorker 由行情事件及清倉時鐘執行 Intraday/Trading；Scanner 透過固定 ProcessPool 分批計算；StreamHealthWorker 獨立維護連線；quote_refresh_seconds 僅控制 GUI 顯示重繪"
    )
    result["candidate_atomic_swap"] = (
        "CandidateSnapshot 完整完成後 Atomic Swap；自動 INTERVAL busy/queued 時 coalesce/skip，"
        "最多 1 running + 1 人工/force pending market refresh"
    )
    result["candidate_freshness_gate"] = (
        f"Universe / Candidate / Quote freshness 分開；Candidate 超過 {CANDIDATE_MAX_AGE_SECONDS:.0f} 秒由 GUI 封鎖新 BUY；Universe 使用最近成功快照時仍更新 Candidate，但整輪 BUY 失效關閉；最終 Quote freshness 由 Trading Revalidation Gate 負責；completed_at 不代表行情新鮮"
    )
    result["wave_passthrough_policy"] = (
        "GUI 不計算 Wave、不保存 Wave 策略狀態；max_entry_wave_bars 直接傳給 Intraday，"
        "max_breakout_extension_pct 直接傳給 Trading；Wave 起點、anchor、age、extension 與 rejection code 僅透傳／顯示"
    )
    result["stopped_manual_refresh"] = (
        "立即更新只要求最新 Market/Scanner refresh；STOP 仍只保留既有被動 CTS 庫存、委託與持倉監控"
    )
    result["auto_session_policy"] = (
        "盤前 AUTO 僅待命且實際維持 MANUAL；09:00 自動切 AUTO；09:05 前封鎖新的自動 BUY"
    )
    result["ok"] = bool(
        result.get("ok")
        and result["parameter_help"]["ok"]
        and result["runtime_components"]["ok"]
    )
    try:
        modules = loader.modules if loader.modules else loader.load_all()
        registry = ParameterRegistry(modules, base_dir)
        result["core_parameter_links"] = build_core_parameter_link_report(
            modules, registry
        )
        gateway_class = getattr(modules.get("api_gateway"), "APIGateway", None)
        get_quote_method = getattr(gateway_class, "get_quote", None)
        status_method = getattr(gateway_class, "status", None)
        result["market_state_interface"] = {
            "ok": callable(get_quote_method) and callable(status_method),
            "quote_signature": (
                str(inspect.signature(get_quote_method))
                if callable(get_quote_method)
                else "UNAVAILABLE"
            ),
            "status_signature": (
                str(inspect.signature(status_method))
                if callable(status_method)
                else "UNAVAILABLE"
            ),
            "policy": (
                "GUI 不直接呼叫行情介面；Runtime 透過 APIGateway MarketState 與 Fugle Trade Event Queue 決定策略狀態，"
                "GUI 只顯示 Runtime Snapshot。"
            ),
        }
        active_universe_method = getattr(gateway_class, "get_active_universe", None)
        result["active_universe_interface"] = {
            "ok": callable(active_universe_method),
            "name": "get_active_universe",
            "signature": (
                str(inspect.signature(active_universe_method))
                if callable(active_universe_method)
                else "UNAVAILABLE"
            ),
            "policy": "Market 全市場資料只供 Market Context；Scanner 股票母體只使用 APIGateway Active Universe，Fallback 由 API 內部處理",
        }
        result["scanner_gui_hidden_internal_fields"] = sorted(
            PARAMETER_TABLE_HIDDEN_FIELDS.get("scanner", frozenset())
        )
    except Exception as exc:
        result["core_parameter_links"] = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    result["ok"] = bool(
        result.get("ok")
        and result["core_parameter_links"].get("ok")
        and result.get("market_state_interface", {}).get("ok")
        and result.get("active_universe_interface", {}).get("ok")
    )
    result["gui_main_thread_policy"] = (
        "Fugle WebSocket callback 不得直接修改 Widget；Worker／Event 只更新 Runtime 狀態或 GUI queue，"
        "Tk Widget 只由 root.after()/WorkerCoordinator.poll_results() 所在 main thread 重繪"
    )
    result["gui_display_refresh_policy"] = (
        "quote_refresh_seconds 僅為 GUI Display Refresh；不得觸發 MarketState 取得、Trade Event 排序、Intraday 或 Trading 計算"
    )
    result["password_persistence"] = False
    result["gui_boundary"] = (
        "主 GUI 只負責畫面、Session Policy、操作事件與 Component 接線；Runtime 執行細節由 runtime 負責"
    )
    result["trading_boundary"] = (
        "主 GUI 不計算 Trading 決策、不解析 CTS Match、不管理 Replay/Position/SELL lifecycle；均由 Runtime Component 編排核心模組"
    )
    unified_path = str(Path(base_dir) / "execution_logs" / f"operations_{_current_trade_date_text()}.jsonl")
    result["execution_journal"] = unified_path
    result["daily_trade_report"] = unified_path
    result["daily_trade_report_policy"] = (
        "GUI/AUTO 成交與 CTS QueryMatch 外部人工成交統一寫入；OID/成交指紋去重；"
        "只有可由系統進場成本或 CTS 同日 BUY 完整配對的 SELL 才計算報酬"
    )
    result["run_flow_journal"] = unified_path
    result["recent_error_journal"] = unified_path
    result["recent_error_logging"] = (
        "交易、成交、異常與恢復事件統一按日保存；常態偵測不留存，原生崩潰另存故障檔"
    )
    return result


# =============================================================================
# 主程式
# =============================================================================


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="GUI、自動策略／手動市價與可重現參數控制模組"
    )
    parser.add_argument(
        "--diagnose", action="store_true", help="診斷五個核心模組連結，不抓市場資料"
    )
    parser.add_argument("--json", action="store_true", help="CLI 結果以 JSON 顯示")
    parser.add_argument(
        "--no-auto-load",
        action="store_true",
        help="GUI 啟動時不自動套用最近一次保存參數",
    )
    return parser


def _print_cli(result: Mapping[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    print("=" * 112)
    print("GUI、自動策略／手動市價與可重現參數控制模組")
    print("=" * 112)
    for key, value in result.items():
        print(f"{key}: {value}")


def _write_unhandled_error(item):
    journal = _ERROR_JOURNAL
    if journal is not None and not journal._log_closed:
        journal.record_run_flow(run_id="UNHANDLED", stage=item["stage"], status="ERROR", details=item)
        return
    directory = BASE_DIR / "execution_logs"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("operations_" + datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d") + ".jsonl")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(item, ensure_ascii=False) + "\n")


def _record_startup_error():
    _write_unhandled_error({"time": datetime.now().astimezone().isoformat(),
        "stage": "GUI_STARTUP", "status": "ERROR", "traceback": traceback.format_exc()})


_FAULT_STREAM = None
_ERROR_JOURNAL = None


def _install_error_reporting():
    """保存 Python、背景執行緒與原生故障紀錄，不從背景操作 Tk。"""
    import faulthandler
    global _FAULT_STREAM
    directory = BASE_DIR / "execution_logs"
    directory.mkdir(parents=True, exist_ok=True)
    def report(kind, value, tb):
        _write_unhandled_error({"time": datetime.now().astimezone().isoformat(),
            "stage": "UNHANDLED_EXCEPTION", "status": "ERROR",
            "error": str(value), "traceback": "".join(traceback.format_exception(kind, value, tb))})
    sys.excepthook = report
    threading.excepthook = lambda args: report(args.exc_type, args.exc_value, args.exc_traceback)
    if _FAULT_STREAM is None:
        trace_path = directory / "native_fault.log"
        if trace_path.exists():
            trace_path.replace(directory / "native_fault.previous.log")
        _FAULT_STREAM = trace_path.open("a", encoding="utf-8")
        faulthandler.enable(file=_FAULT_STREAM, all_threads=True)
    return report


def launch_gui(*, auto_load: bool = True) -> int:
    import tkinter as tk
    from tkinter import messagebox

    report_exception = _install_error_reporting()
    loader = CoreModuleLoader(BASE_DIR)
    try:
        modules = loader.load_all()
        registry = ParameterRegistry(modules, BASE_DIR)
    except Exception as exc:
        _record_startup_error()
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "核心模組載入失敗",
            f"{type(exc).__name__}: {exc}\n\n"
            "請確認 gui.py 與 params / runtime / api_gateway / market / scanner / intraday / trading 放在同一資料夾。",
        )
        root.destroy()
        return 1

    store = ConfigStore(BASE_DIR)
    settings = GUISettings()
    autoload_message = ""
    if auto_load:
        try:
            loaded = store.load_latest(registry)
            if loaded is not None:
                loaded_values = asdict(loaded)
                loaded_values["control_mode"] = "MANUAL"
                loaded_values["auto_execute"] = False
                settings = GUISettings(**loaded_values)
                settings.validate()
                autoload_message = (
                    "已自動套用最近一次保存且驗證通過的參數；"
                    "為避免啟動後直接交易，控制模式已安全重設為 MANUAL，AUTO 實際下單已關閉。"
                )
        except Exception as exc:
            registry.restore_all()
            settings = GUISettings()
            autoload_message = (
                f"最近一次保存參數驗證失敗，已使用預設值：{type(exc).__name__}: {exc}"
            )

    root = tk.Tk()
    root.report_callback_exception = report_exception
    gui = TradingGUI(root, loader, modules, registry, settings, store)
    if autoload_message:
        gui._set_message(autoload_message)
    root.mainloop()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.diagnose:
        result = diagnose(BASE_DIR)
        _print_cli(result, args.json)
        return 0 if result.get("ok") else 1
    try:
        return launch_gui(auto_load=not args.no_auto_load)
    except Exception:
        _record_startup_error()
        raise


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main())

# 更新摘要：顯示剩餘總風險及未完成出場；持倉上限為使用者設定的天花板。
