# -*- coding: utf-8 -*-
"""
market.py - 市場環境、類股強弱與極短線多方許可判定模組

用途
----
本模組負責依據市場指數、市場廣度、成交狀況、類股強弱及既定市場判定參數，
建立當前市場環境資訊，包括市場分數、市場狀態、交易模式與極短線多方許可。

資料邊界
--------
本模組不直接建立 WebSocket、不保存 Fugle Token、不處理訂閱或重連，也不直接連線
TWSE／TPEx／Yahoo 等外部行情來源。正式市場資料由 api_gateway 標準化後提供；
本模組只接受 TAIEX、TPEx、全市場成分股 Snapshot、Index History、Sector 等資料，
並負責 Market Score、Market State、Breadth、Sector Strength、Trading Mode 與
Market Guard 判定。TAIEX／TPEx 僅為市場指標名稱，不代表資料直接來自交易所 API。

可重現核心
----------
Market_Result = f(Market_Data, Market_Parameters)

相同 TAIEX、TPEX、市場廣度、成交狀況、類股資料、權重與門檻，必須得到相同：
market_score、market_state、trading_mode、allow_long。

固定保存參數
------------
MA_PERIOD
MARKET_SCORE_WEIGHT
BREADTH_WEIGHT
SECTOR_WEIGHT
BULL_THRESHOLD
BEAR_THRESHOLD
DISABLED_SECTORS
MODE
MANUAL_MARKET_STATE

相容介面
--------
get_market_context(
    mode="AUTO",
    manual_market_state=None,
    manual_trading_mode=None,
    disabled_sectors=None,
)

CLI
---
python market.py --self-test
python market.py --diagnose
python market.py --probe
python market.py --probe --json
"""

# 中文重點更新：市場許可獨立讀取原子發布資料，補齊許可身分、TTL 與交易日；類股區分 REST 快照及逐筆來源時間。

# 中文重點更新：清除未使用函式、相容介面與無效參數；保留市價買賣、成交對帳、撤買單、清倉及離線驗證。

# 中文重點更新：調整三項任一觸發急跌保護門檻，市場資料查詢使用共用資源管理。

# 中文重點更新：新增日內市場方向與雙策略做多許可，PANIC 維持禁止新買進。

# 中文重點更新：補齊審查要求之狀態識別、整張委託、資料時序與恢復安全邊界。

# 中文重點更新：市場許可統一判定 UP、強勢整理與回檔；綁定來源時間、快照與參數證據，禁止異常資料放行。

# 中文重點更新：分離指數來源與市場廣度取得時間；統一市場許可、有效期限與指定時間重播。

# 中文重點更新：將類股計算使用的對照資料隨同市場快照發布，供 Scanner 共用。

# 中文重點更新：提供固定類股代碼與獨立類股快照，分類、行情缺漏與資料時效分開判定。

# 中文重點更新：市場並行查詢共用截止時間，未完成請求沿用，避免無限等待與排隊累積。

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import argparse
import csv
import hashlib
import importlib
import inspect
import json
import math
import os
import re
import threading
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# =============================================================================
# 固定參數
# =============================================================================

MA_PERIOD = 5

# 最終分數的三大固定權重；可用資料不足時只對可用權重重新正規化，
# score_coverage 仍保留原始覆蓋率，避免把資料缺漏誤認為完整判斷。
MARKET_SCORE_WEIGHT = 0.55
BREADTH_WEIGHT = 0.25
SECTOR_WEIGHT = 0.20

BULL_THRESHOLD = 30.0
BEAR_THRESHOLD = -30.0

DISABLED_SECTORS: Tuple[str, ...] = ()
MODE = "AUTO"
MANUAL_MARKET_STATE: Optional[str] = None

# Market Snapshot 與 Candidate Scan 解耦。Scanner 只讀已 Atomic Publish 的
# Market Context；全市場更新由 Runtime / 背景工作獨立觸發。
MARKET_SNAPSHOT_REFRESH_MIN_INTERVAL_SECONDS = 30.0
MARKET_SNAPSHOT_BACKGROUND_TIMEOUT_SECONDS = 60.0

# 指數與極短線判定的固定次參數。
TAIEX_INDEX_WEIGHT = 0.65
TPEX_INDEX_WEIGHT = 0.35
INDEX_CHANGE_WEIGHT = 0.75
INDEX_DIRECTION_WEIGHT = 0.25
INDEX_CHANGE_FULL_SCALE_PCT = 2.0
DIRECTION_FULL_SCALE_PCT = 1.0

# 市場廣度內部：家數廣度 + 成交值資金流。
BREADTH_COUNT_WEIGHT = 0.65
BREADTH_TURNOVER_WEIGHT = 0.35

# 急跌保護。只要符合任一條件，即使總分尚未低於 BEAR_THRESHOLD，仍視為 PANIC。
PANIC_TAIEX_DROP_PCT = -2.50
PANIC_TPEX_DROP_PCT = -3.00
PANIC_BREADTH_RATIO = -0.75

# 市場廣度預設只納入上市／上櫃普通股。台灣普通股代號採 4 碼且首碼非 0；
# ETF / ETN 多以 0 開頭，權證多為較長代號，特別股常含英文字母。
COMMON_STOCK_ONLY = True
COMMON_STOCK_SYMBOL_PATTERN = r"^[1-9]\d{3}$"

# 類股強勢門檻。類股來源依序為 explicit sector_map、market_data 內 sector_map、
# APIGateway 未來可選的分類方法、本機 sector_map 檔；不以股票代號猜測產業別。
SECTOR_STRONG_SCORE = 20.0
SECTOR_CHANGE_FULL_SCALE_PCT = 2.0
DEFAULT_SECTOR_MAP_FILES = (
    "sector_map.json",
    "sector_map.csv",
    "stock_sector_map.json",
    "stock_sector_map.csv",
)

VALID_MARKET_STATES = {"BULL", "NORMAL", "WEAK", "PANIC"}
VALID_TRADING_MODES = {
    "TREND",
    "SELECTIVE_LONG",
    "DEFENSIVE",
    "OVERSOLD_MEAN_REVERSION",
    "STOP",
}

OUTPUT_FIELDS = [
    "market_state",
    "market_score",
    "score_coverage",
    "trading_mode",
    "allow_long",
    "long_permission",
    "trend_long_allowed",
    "trend_long_reason",
    "strategy_market_permissions",
    "market_momentum",
    "taiex",
    "tpex",
    "breadth",
    "turnover_context",
    "sector_context",
    "strong_sectors",
]


# =============================================================================
# 參數模型
# =============================================================================


@dataclass(frozen=True)
class MarketParameters:
    trend_breadth_min: float = 0.0
    trend_pullback_score_min: float = 30.0
    trend_pullback_breadth_min: float = 0.20
    trend_permission_max_age_seconds: float = 60.0

    ma_period: int = MA_PERIOD
    market_score_weight: float = MARKET_SCORE_WEIGHT
    breadth_weight: float = BREADTH_WEIGHT
    sector_weight: float = SECTOR_WEIGHT
    bull_threshold: float = BULL_THRESHOLD
    bear_threshold: float = BEAR_THRESHOLD
    disabled_sectors: Tuple[str, ...] = DISABLED_SECTORS
    mode: str = MODE
    manual_market_state: Optional[str] = MANUAL_MARKET_STATE
    manual_trading_mode: Optional[str] = None

    taiex_index_weight: float = TAIEX_INDEX_WEIGHT
    tpex_index_weight: float = TPEX_INDEX_WEIGHT
    index_change_weight: float = INDEX_CHANGE_WEIGHT
    index_direction_weight: float = INDEX_DIRECTION_WEIGHT
    index_change_full_scale_pct: float = INDEX_CHANGE_FULL_SCALE_PCT
    direction_full_scale_pct: float = DIRECTION_FULL_SCALE_PCT
    breadth_count_weight: float = BREADTH_COUNT_WEIGHT
    breadth_turnover_weight: float = BREADTH_TURNOVER_WEIGHT
    panic_taiex_drop_pct: float = PANIC_TAIEX_DROP_PCT
    panic_tpex_drop_pct: float = PANIC_TPEX_DROP_PCT
    panic_breadth_ratio: float = PANIC_BREADTH_RATIO
    sector_strong_score: float = SECTOR_STRONG_SCORE
    sector_change_full_scale_pct: float = SECTOR_CHANGE_FULL_SCALE_PCT
    common_stock_only: bool = COMMON_STOCK_ONLY

    def validate(self) -> None:
        if not math.isfinite(float(self.trend_breadth_min)):
            raise ValueError("trend_breadth_min 必須為有限數值")
        if not math.isfinite(self.trend_pullback_score_min) or not -100 <= self.trend_pullback_score_min <= 100:
            raise ValueError("trend_pullback_score_min 必須介於 -100 與 100")
        if (not math.isfinite(self.trend_pullback_breadth_min) or not -1 <= self.trend_pullback_breadth_min <= 1):
            raise ValueError("trend_pullback_breadth_min 必須介於 -1 與 1")
        if not math.isfinite(self.trend_permission_max_age_seconds) or not 0 < self.trend_permission_max_age_seconds <= 300:
            raise ValueError("trend_permission_max_age_seconds 必須大於 0 且不超過 300 秒")
        if self.ma_period <= 0:
            raise ValueError("MA_PERIOD 必須 > 0")

        major_weights = [
            self.market_score_weight,
            self.breadth_weight,
            self.sector_weight,
        ]
        if any(weight < 0 for weight in major_weights):
            raise ValueError("MARKET/BREADTH/SECTOR 權重不得小於 0")
        if not math.isclose(sum(major_weights), 1.0, abs_tol=1e-9):
            raise ValueError(
                "MARKET_SCORE_WEIGHT + BREADTH_WEIGHT + SECTOR_WEIGHT 必須等於 1"
            )

        if not math.isclose(
            self.taiex_index_weight + self.tpex_index_weight, 1.0, abs_tol=1e-9
        ):
            raise ValueError("TAIEX_INDEX_WEIGHT + TPEX_INDEX_WEIGHT 必須等於 1")
        if not math.isclose(
            self.index_change_weight + self.index_direction_weight, 1.0, abs_tol=1e-9
        ):
            raise ValueError("INDEX_CHANGE_WEIGHT + INDEX_DIRECTION_WEIGHT 必須等於 1")
        if not math.isclose(
            self.breadth_count_weight + self.breadth_turnover_weight, 1.0, abs_tol=1e-9
        ):
            raise ValueError(
                "BREADTH_COUNT_WEIGHT + BREADTH_TURNOVER_WEIGHT 必須等於 1"
            )

        if self.bear_threshold >= self.bull_threshold:
            raise ValueError("BEAR_THRESHOLD 必須小於 BULL_THRESHOLD")
        if self.index_change_full_scale_pct <= 0 or self.direction_full_scale_pct <= 0:
            raise ValueError("分數縮放百分比必須 > 0")
        if self.sector_change_full_scale_pct <= 0:
            raise ValueError("SECTOR_CHANGE_FULL_SCALE_PCT 必須 > 0")

        mode = _normalize_mode(self.mode)
        if mode == "MANUAL":
            if self.manual_market_state is None and self.manual_trading_mode is None:
                raise ValueError(
                    "MANUAL 模式至少要提供 manual_market_state 或 manual_trading_mode"
                )
        if self.manual_market_state is not None:
            state = str(self.manual_market_state).strip().upper()
            if state not in VALID_MARKET_STATES:
                raise ValueError(
                    f"manual_market_state 僅允許 {sorted(VALID_MARKET_STATES)}"
                )
        if self.manual_trading_mode is not None:
            trading_mode = str(self.manual_trading_mode).strip().upper()
            if trading_mode not in VALID_TRADING_MODES:
                raise ValueError(
                    f"manual_trading_mode 僅允許 {sorted(VALID_TRADING_MODES)}"
                )

    def snapshot(self) -> Dict[str, Any]:
        """以固定欄位保存有效參數，便於 log / replay / 重現。"""
        return {
            "TREND_BREADTH_MIN": self.trend_breadth_min,
            "TREND_PULLBACK_SCORE_MIN": self.trend_pullback_score_min,
            "TREND_PULLBACK_BREADTH_MIN": self.trend_pullback_breadth_min,
            "TREND_PERMISSION_MAX_AGE_SECONDS": self.trend_permission_max_age_seconds,
            "MA_PERIOD": int(self.ma_period),
            "MARKET_SCORE_WEIGHT": float(self.market_score_weight),
            "BREADTH_WEIGHT": float(self.breadth_weight),
            "SECTOR_WEIGHT": float(self.sector_weight),
            "BULL_THRESHOLD": float(self.bull_threshold),
            "BEAR_THRESHOLD": float(self.bear_threshold),
            "DISABLED_SECTORS": list(self.disabled_sectors),
            "MODE": _normalize_mode(self.mode),
            "MANUAL_MARKET_STATE": (
                None
                if self.manual_market_state is None
                else str(self.manual_market_state).strip().upper()
            ),
            "MANUAL_TRADING_MODE": (
                None
                if self.manual_trading_mode is None
                else str(self.manual_trading_mode).strip().upper()
            ),
            "TAIEX_INDEX_WEIGHT": float(self.taiex_index_weight),
            "TPEX_INDEX_WEIGHT": float(self.tpex_index_weight),
            "INDEX_CHANGE_WEIGHT": float(self.index_change_weight),
            "INDEX_DIRECTION_WEIGHT": float(self.index_direction_weight),
            "INDEX_CHANGE_FULL_SCALE_PCT": float(self.index_change_full_scale_pct),
            "DIRECTION_FULL_SCALE_PCT": float(self.direction_full_scale_pct),
            "BREADTH_COUNT_WEIGHT": float(self.breadth_count_weight),
            "BREADTH_TURNOVER_WEIGHT": float(self.breadth_turnover_weight),
            "PANIC_TAIEX_DROP_PCT": float(self.panic_taiex_drop_pct),
            "PANIC_TPEX_DROP_PCT": float(self.panic_tpex_drop_pct),
            "PANIC_BREADTH_RATIO": float(self.panic_breadth_ratio),
            "SECTOR_STRONG_SCORE": float(self.sector_strong_score),
            "SECTOR_CHANGE_FULL_SCALE_PCT": float(self.sector_change_full_scale_pct),
            "COMMON_STOCK_ONLY": bool(self.common_stock_only),
        }


# =============================================================================
# 共用工具
# =============================================================================


def _normalize_mode(value: str) -> str:
    mode = str(value or "AUTO").strip().upper()
    if mode not in {"AUTO", "MANUAL"}:
        raise ValueError("mode 僅允許 AUTO / MANUAL")
    return mode


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = str(value).strip().replace(",", "").replace("+", "")
    if text in {"", "-", "--", "---", "N/A", "None", "null"}:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _to_int(value: Any) -> int:
    number = _to_float(value)
    return 0 if number is None else int(number)


def _round(value: Optional[float], digits: int = 4) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def _clamp(value: float, lower: float = -100.0, upper: float = 100.0) -> float:
    return max(lower, min(upper, float(value)))


def _pct_change(current: Optional[float], base: Optional[float]) -> Optional[float]:
    if current is None or base is None or base <= 0:
        return None
    return (current - base) / base * 100.0


def _weighted_average(
    pairs: Sequence[Tuple[Optional[float], float]],
) -> Optional[float]:
    numerator = 0.0
    denominator = 0.0
    for value, weight in pairs:
        if value is None or weight <= 0:
            continue
        numerator += float(value) * float(weight)
        denominator += float(weight)
    if denominator <= 0:
        return None
    return numerator / denominator


def _stable_hash(payload: Any) -> str:
    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _clean_disabled(values: Optional[Iterable[str]]) -> Tuple[str, ...]:
    if values is None:
        return ()
    cleaned = sorted({str(value).strip() for value in values if str(value).strip()})
    return tuple(cleaned)


def _row_key(row: Mapping[str, Any]) -> Tuple[str, str]:
    market = str(row.get("market") or "").strip()
    symbol = str(row.get("symbol") or "").strip()
    return market, symbol


def _effective_price(row: Mapping[str, Any]) -> Optional[float]:
    price = _to_float(row.get("price"))
    return price if price is not None and price > 0 else None


def _canonical_quote(row: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(row, Mapping):
        return None
    return {
        "symbol": str(row.get("symbol") or ""),
        "name": str(row.get("name") or ""),
        "market": str(row.get("market") or ""),
        "trade_date": str(row.get("trade_date") or ""),
        "timestamp": row.get("timestamp"),
        "price": _to_float(row.get("price")),
        "open": _to_float(row.get("open")),
        "high": _to_float(row.get("high")),
        "low": _to_float(row.get("low")),
        "prev_close": _to_float(row.get("prev_close")),
        "volume": _to_int(row.get("volume")),
        "turnover": _to_float(row.get("turnover")) or 0.0,
    }


def _canonical_snapshot(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    for row in rows:
        item = _canonical_quote(row)
        if item is not None and item["symbol"]:
            output.append(item)
    output.sort(key=lambda item: (item["market"], item["symbol"]))
    return output


def _parse_source_datetime(value: Any) -> Optional[datetime]:
    """解析真正來源時間；received_at/fetched_at/refreshed_at 不得傳入此函式。"""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            try:
                dt = datetime.fromisoformat(text)
            except ValueError:
                return None
        else:
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                number = _to_float(value)
                if number is None or number <= 0:
                    return None
                try:
                    if number > 10_000_000_000_000:
                        seconds = number / 1_000_000.0
                    elif number > 10_000_000_000:
                        seconds = number / 1000.0
                    else:
                        seconds = number
                    dt = datetime.fromtimestamp(
                        seconds,
                        tz=datetime.now(timezone(timedelta(hours=8))).tzinfo,
                    )
                except (OverflowError, OSError, ValueError):
                    return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now(timezone(timedelta(hours=8))).tzinfo)
    return dt.astimezone()


def _row_source_datetime(row: Mapping[str, Any]) -> Optional[datetime]:
    """只接受資料來源欄位，絕不以 fetched_at/received_at/refreshed_at 補值。"""
    for field in (
        "source_at",
        "realtime_source_at",
        "timestamp",
        "event_time",
        "trade_date",
    ):
        parsed = _parse_source_datetime(row.get(field))
        if parsed is not None:
            return parsed
    return None


def _derive_asof(market_data: Mapping[str, Any]) -> str:
    """只由實際市場來源時間推導 as-of；不得使用本機抓取／GUI 更新完成時間。"""
    candidates: List[datetime] = []
    for key in ("taiex", "tpex"):
        row = market_data.get(key)
        if isinstance(row, Mapping):
            parsed = _row_source_datetime(row)
            if parsed is not None:
                candidates.append(parsed)
    for key in ("twse_rows", "tpex_rows", "taiex_history", "tpex_history"):
        rows = market_data.get(key)
        if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                parsed = _row_source_datetime(row)
                if parsed is not None:
                    candidates.append(parsed)
    if not candidates:
        return "UNKNOWN"
    return max(candidates).isoformat(timespec="milliseconds")


def _snapshot_session_state(
    market_data: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
) -> Tuple[str, str]:
    """依標準化 Snapshot 的交易日標記 CURRENT/PREVIOUS_SESSION；不參與純計算公式。"""
    trade_dates: List[str] = []
    for key in ("taiex", "tpex"):
        row = market_data.get(key)
        if isinstance(row, Mapping):
            text = str(row.get("trade_date") or "").strip()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                trade_dates.append(text)
    for key in ("twse_rows", "tpex_rows"):
        rows = market_data.get(key)
        if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                text = str(row.get("trade_date") or "").strip()
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                    trade_dates.append(text)
    if not trade_dates:
        return "UNKNOWN", ""
    latest = max(trade_dates)
    today = (now or datetime.now(timezone(timedelta(hours=8)))).date().isoformat()
    if latest < today:
        return "PREVIOUS_SESSION", latest
    if latest == today:
        return (
            "CURRENT_SESSION" if min(trade_dates) == today else "MIXED_SESSION"
        ), latest
    return "FUTURE_SESSION", latest


def _runtime_timestamp() -> str:
    """只供執行狀態標記；不納入 Market_Result 可重現計算。"""
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="milliseconds")


def _stable_market_rows(rows: Any) -> List[Dict[str, Any]]:
    """固定全市場資料列順序，避免來源完成順序改變浮點累加與排名結果。"""
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return []
    output = [dict(row) for row in rows if isinstance(row, Mapping)]
    output.sort(
        key=lambda row: (
            str(row.get("market") or ""),
            str(row.get("symbol") or ""),
            str(row.get("trade_date") or ""),
            str(row.get("timestamp") or ""),
            str(row.get("fetched_at") or ""),
        )
    )
    return output


def _stable_history_rows(rows: Any) -> Any:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return rows
    if not all(isinstance(row, Mapping) for row in rows):
        return list(rows)
    output = [dict(row) for row in rows if isinstance(row, Mapping)]
    output.sort(
        key=lambda row: (
            str(row.get("trade_date") or ""),
            str(row.get("timestamp") or ""),
            str(row.get("symbol") or ""),
        )
    )
    return output


def _snapshot_market_data(market_data: Mapping[str, Any]) -> Dict[str, Any]:
    """建立可 Atomic Publish 的不可變語意快照副本。"""
    snapshot = deepcopy(dict(market_data))
    snapshot["taiex"] = (
        dict(snapshot["taiex"]) if isinstance(snapshot.get("taiex"), Mapping) else None
    )
    snapshot["tpex"] = (
        dict(snapshot["tpex"]) if isinstance(snapshot.get("tpex"), Mapping) else None
    )
    snapshot["twse_rows"] = _stable_market_rows(snapshot.get("twse_rows"))
    snapshot["tpex_rows"] = _stable_market_rows(snapshot.get("tpex_rows"))
    snapshot["taiex_history"] = _stable_history_rows(snapshot.get("taiex_history"))
    snapshot["tpex_history"] = _stable_history_rows(snapshot.get("tpex_history"))
    snapshot["warnings"] = sorted(
        {str(item) for item in (snapshot.get("warnings") or [])}
    )
    snapshot["stage_errors"] = sorted(
        [
            {
                "stage": str(item.get("stage") or "UNKNOWN_STAGE"),
                "message": str(item.get("message") or ""),
            }
            for item in (snapshot.get("stage_errors") or [])
            if isinstance(item, Mapping)
        ],
        key=lambda item: (item["stage"], item["message"]),
    )
    embedded_sector_map = _normalize_sector_map_payload(snapshot.get("sector_map"))
    if embedded_sector_map:
        snapshot["sector_map"] = dict(sorted(embedded_sector_map.items()))
    return snapshot


def _market_request_key(
    params: MarketParameters,
    *,
    sector_map: Optional[Mapping[str, str]],
    sector_map_path: Optional[str],
) -> str:
    payload = {
        "parameters": params.snapshot(),
        "sector_map": dict(sorted(_normalize_sector_map_payload(sector_map).items())),
        "sector_map_path": str(sector_map_path or ""),
    }
    return _stable_hash(payload)


def _build_market_guard(
    context: Mapping[str, Any],
    *,
    snapshot_id: str,
) -> Dict[str, Any]:
    """只封裝既有風控結果；PREVIOUS_SESSION 可供分析但不視為即時 BUY 資料。"""
    if context.get("market_guard", {}).get("market_permission_snapshot_id"):
        return deepcopy(context["market_guard"])
    realtime_ready = context.get("realtime_buy_data_ready") is True
    return {
        "status": "READY" if realtime_ready else "DATA_RECOVERY",
        "ready": realtime_ready,
        "snapshot_id": str(snapshot_id),
        "market_state": str(context.get("market_state") or ""),
        "market_score": context.get("market_score"),
        "trading_mode": str(context.get("trading_mode") or ""),
        "allow_long": bool(context.get("allow_long")),
        "long_permission": str(context.get("long_permission") or ""),
        "panic_reason": context.get("panic_reason"),
        "session_state": str(context.get("session_state") or "UNKNOWN"),
        "realtime_buy_data_ready": realtime_ready,
        "realtime_buy_allowed": bool(context.get("allow_long")) and realtime_ready,
    }


class _MarketPublicationStore:
    """保存最近一次完整 Market Snapshot 與 Context；Snapshot/Context 皆採 Atomic Publish。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._generation = 0
        self._snapshot: Optional[Dict[str, Any]] = None
        self._contexts: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._last_refresh_status: Dict[str, Any] = {
            "state": "MISSING",
            "publication_state": "MISSING",
            "refreshing": False,
            "generation": 0,
            "snapshot_id": "",
            "source_at": "",
            "refreshed_at": "",
            "fingerprint": "",
            "session_state": "UNKNOWN",
            "stale": True,
            "error": "",
            "error_stage": "",
            "stage_errors": [],
        }

    def read_snapshot(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return deepcopy(self._snapshot) if self._snapshot is not None else None

    def read_context(self, request_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            if self._snapshot is None:
                return None
            snapshot_id = str(self._snapshot.get("snapshot_id") or "")
            context = self._contexts.get((snapshot_id, str(request_key)))
            if context is None:
                return None
            output = deepcopy(context)
            previous_meta = (
                dict(output.get("market_snapshot") or {})
                if isinstance(output.get("market_snapshot"), Mapping)
                else {}
            )
            output["market_snapshot"] = self._publication_meta_locked(
                calculated_at=str(previous_meta.get("calculated_at") or ""),
            )
            output["market_guard"] = _build_market_guard(
                output,
                snapshot_id=snapshot_id,
            )
            return resolve_strategy_market_context(output)

    def _publication_meta_locked(self, *, calculated_at: str = "") -> Dict[str, Any]:
        if self._snapshot is None:
            return {
                "publication_state": "MISSING",
                "snapshot_id": "",
                "generation": 0,
                "source_at": "",
                "refreshed_at": "",
                "fingerprint": "",
                "session_state": "UNKNOWN",
                "stale": True,
                "calculated_at": str(calculated_at or ""),
            }
        state = str(self._last_refresh_status.get("state") or "READY")
        return {
            "publication_state": state,
            "snapshot_id": str(self._snapshot.get("snapshot_id") or ""),
            "generation": int(self._snapshot.get("generation") or 0),
            "source_at": str(self._snapshot.get("source_at") or ""),
            "refreshed_at": str(self._snapshot.get("refreshed_at") or ""),
            "fingerprint": str(self._snapshot.get("fingerprint") or ""),
            "session_state": str(self._snapshot.get("session_state") or "UNKNOWN"),
            "stale": bool(
                state == "DEGRADED"
                or str(self._snapshot.get("session_state") or "") == "PREVIOUS_SESSION"
            ),
            "calculated_at": str(calculated_at or ""),
        }

    def publish(
        self,
        *,
        data: Mapping[str, Any],
        request_key: str,
        context: Mapping[str, Any],
        source_at: str,
        calculated_at: str,
    ) -> Dict[str, Any]:
        snapshot_data = _snapshot_market_data(data)
        fingerprint = str(
            context.get("data_fingerprint") or _stable_hash(snapshot_data)
        )
        session_state = str(snapshot_data.get("session_state") or "UNKNOWN")
        session_trade_date = str(snapshot_data.get("session_trade_date") or "")
        snapshot_id = _stable_hash(
            {
                "fingerprint": fingerprint,
                "source_at": str(source_at or ""),
                "session_state": session_state,
                "session_trade_date": session_trade_date,
            }
        )
        refreshed_at = _runtime_timestamp()
        stage_errors = [
            deepcopy(dict(item))
            for item in (snapshot_data.get("stage_errors") or [])
            if isinstance(item, Mapping)
        ]
        with self._lock:
            self._generation += 1
            generation = self._generation
            publication = {
                "snapshot_id": snapshot_id,
                "generation": generation,
                "source_at": str(source_at or ""),
                "refreshed_at": refreshed_at,
                "refreshed_monotonic": time.monotonic(),
                "fingerprint": fingerprint,
                "session_state": session_state,
                "session_trade_date": session_trade_date,
                "data": snapshot_data,
            }
            self._snapshot = publication
            self._last_refresh_status = {
                "state": "READY",
                "publication_state": "READY",
                "refreshing": True,
                "generation": generation,
                "snapshot_id": snapshot_id,
                "source_at": str(source_at or ""),
                "refreshed_at": refreshed_at,
                "fingerprint": fingerprint,
                "session_state": session_state,
                "session_trade_date": session_trade_date,
                "stale": session_state == "PREVIOUS_SESSION",
                "error": "",
                "error_stage": "",
                "stage_errors": stage_errors,
            }
            meta = self._publication_meta_locked(calculated_at=calculated_at)
            published_context = deepcopy(dict(context))
            published_context["market_snapshot"] = meta
            published_context["strategy_market_permissions"] = {"TREND": _trend_permission_policy(published_context)}
            published_context = resolve_strategy_market_context(published_context, now=_parse_source_datetime(calculated_at))
            published_context["market_guard"] = _build_market_guard(
                published_context,
                snapshot_id=snapshot_id,
            )
            self._contexts = {(snapshot_id, str(request_key)): published_context}
            return deepcopy(published_context)

    def publish_context_if_current(
        self,
        *,
        snapshot_id: str,
        request_key: str,
        context: Mapping[str, Any],
        calculated_at: str,
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            if self._snapshot is None:
                return None
            current_id = str(self._snapshot.get("snapshot_id") or "")
            if current_id != str(snapshot_id):
                return None
            published_context = deepcopy(dict(context))
            published_context["market_snapshot"] = self._publication_meta_locked(
                calculated_at=calculated_at,
            )
            published_context["strategy_market_permissions"] = {"TREND": _trend_permission_policy(published_context)}
            published_context["market_guard"] = _build_market_guard(
                published_context,
                snapshot_id=current_id,
            )
            self._contexts[(current_id, str(request_key))] = published_context
            published_context = resolve_strategy_market_context(published_context, now=_parse_source_datetime(calculated_at))
            self._contexts[(current_id, str(request_key))] = published_context
            return deepcopy(published_context)

    def try_begin_refresh(self) -> bool:
        acquired = self._refresh_lock.acquire(blocking=False)
        if not acquired:
            return False
        with self._lock:
            status = dict(self._last_refresh_status)
            status["state"] = "REFRESHING"
            status["publication_state"] = "REFRESHING"
            status["refreshing"] = True
            status["error"] = ""
            status["error_stage"] = ""
            self._last_refresh_status = status
        return True

    def end_refresh(
        self,
        error: str = "",
        *,
        error_stage: str = "",
        stage_errors: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> None:
        with self._lock:
            status = dict(self._last_refresh_status)
            status["refreshing"] = False
            if stage_errors is not None:
                status["stage_errors"] = [
                    deepcopy(dict(item))
                    for item in stage_errors
                    if isinstance(item, Mapping)
                ]
            if error:
                has_last_good = self._snapshot is not None
                status["state"] = "DEGRADED" if has_last_good else "MISSING"
                status["publication_state"] = status["state"]
                status["stale"] = True
                status["error"] = str(error)
                status["error_stage"] = str(error_stage or "MARKET_REFRESH_ERROR")
            elif self._snapshot is not None:
                status["state"] = "READY"
                status["publication_state"] = "READY"
                status["stale"] = bool(
                    str(self._snapshot.get("session_state") or "") == "PREVIOUS_SESSION"
                )
                status["error"] = ""
                status["error_stage"] = ""
            else:
                status["state"] = "MISSING"
                status["publication_state"] = "MISSING"
                status["stale"] = True
            self._last_refresh_status = status
        if self._refresh_lock.locked():
            self._refresh_lock.release()

    def snapshot_age_seconds(self) -> Optional[float]:
        with self._lock:
            if self._snapshot is None:
                return None
            refreshed_monotonic = _to_float(self._snapshot.get("refreshed_monotonic"))
            if refreshed_monotonic is None:
                return None
            return max(0.0, time.monotonic() - refreshed_monotonic)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return deepcopy(self._last_refresh_status)


_MARKET_PUBLICATION = _MarketPublicationStore()


# =============================================================================
# api_gateway.py 動態連結
# =============================================================================


def _version_tuple(stem: str, prefix: str) -> Optional[Tuple[int, ...]]:
    match = re.fullmatch(rf"{re.escape(prefix)}_v(\d+(?:_\d+)*)", stem)
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("_"))


def api_gateway_candidates() -> List[str]:
    """優先環境指定，其次同資料夾最新版 api_gateway_v*.py，最後 api_gateway.py。"""
    candidates: List[str] = []
    forced = os.getenv("MARKET_API_GATEWAY_MODULE", "").strip()
    if forced:
        candidates.append(forced[:-3] if forced.endswith(".py") else forced)

    base_dir = Path(__file__).resolve().parent
    versioned: List[Tuple[Tuple[int, ...], str]] = []
    for file_path in base_dir.glob("api_gateway_v*.py"):
        version = _version_tuple(file_path.stem, "api_gateway")
        if version is not None:
            versioned.append((version, file_path.stem))
    for _, module_name in sorted(versioned, key=lambda item: item[0], reverse=True):
        if module_name not in candidates:
            candidates.append(module_name)

    if "api_gateway" not in candidates:
        candidates.append("api_gateway")
    return candidates


def load_api_gateway_module() -> Any:
    errors: List[str] = []
    for module_name in api_gateway_candidates():
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            errors.append(f"{module_name}: {type(exc).__name__}: {exc}")
            continue
        gateway_class = getattr(module, "APIGateway", None)
        if gateway_class is not None:
            return module
        errors.append(f"{module_name}: 無 APIGateway")
    raise RuntimeError(
        "找不到可用的 api_gateway 模組；請將 market.py 與 api_gateway.py / "
        f"api_gateway_v*.py 放在同一資料夾。詳細：{' | '.join(errors)}"
    )


def create_api_gateway(
    *, api_mode: str = "LIVE", replay_dir: str = "api_replay"
) -> Any:
    module = load_api_gateway_module()
    gateway_class = getattr(module, "APIGateway")
    return gateway_class(mode=str(api_mode).strip().upper(), replay_dir=replay_dir)


# =============================================================================
# 市場資料取得：只透過 APIGateway
# =============================================================================


def _deduplicate_daily_history(
    rows: Sequence[Mapping[str, Any]],
    *,
    current_quote: Optional[Mapping[str, Any]],
    period: int,
) -> List[Dict[str, Any]]:
    """將同一指數每日多筆 replay 行情收斂為每天最後一筆，供 MA 使用。"""
    by_date: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        trade_date = str(row.get("trade_date") or "").strip()
        price = _effective_price(row)
        if not trade_date or price is None:
            continue
        by_date[trade_date] = {
            "trade_date": trade_date,
            "price": price,
            "timestamp": row.get("timestamp"),
        }

    if isinstance(current_quote, Mapping):
        trade_date = str(current_quote.get("trade_date") or "").strip()
        price = _effective_price(current_quote)
        if trade_date and price is not None:
            by_date[trade_date] = {
                "trade_date": trade_date,
                "price": price,
                "timestamp": current_quote.get("timestamp"),
            }

    ordered = [by_date[key] for key in sorted(by_date)]
    keep = max(int(period), 1)
    return ordered[-keep:]


def _load_gateway_index_history(
    gateway: Any,
    *,
    symbol: str,
    market: str,
    current_quote: Optional[Mapping[str, Any]],
    period: int,
    stage_errors: Optional[List[Dict[str, str]]] = None,
    stage_name: str = "INDEX_HISTORY_ERROR",
) -> Tuple[List[Dict[str, Any]], str]:
    """只接受 APIGateway.get_index_history()；provider symbol 解析不得落入 Market。"""
    method = getattr(gateway, "get_index_history", None)
    if callable(method):
        try:
            signature = inspect.signature(method)
            kwargs: Dict[str, Any] = {}
            if "limit" in signature.parameters:
                kwargs["limit"] = int(period)
            rows = method(symbol, market, **kwargs)
            if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
                history = _deduplicate_daily_history(
                    [row for row in rows if isinstance(row, Mapping)],
                    current_quote=current_quote,
                    period=period,
                )
                if history:
                    return _stable_history_rows(history), "APIGateway.get_index_history"
        except Exception as exc:
            if stage_errors is not None:
                stage_errors.append(
                    {
                        "stage": str(stage_name),
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                )
    elif stage_errors is not None:
        stage_errors.append(
            {
                "stage": "INDEX_HISTORY_INTERFACE_MISSING",
                "message": "APIGateway 缺少 get_index_history()",
            }
        )

    history = _deduplicate_daily_history(
        [],
        current_quote=current_quote,
        period=period,
    )
    return (
        _stable_history_rows(history),
        "CURRENT_QUOTE_ONLY" if history else "UNAVAILABLE",
    )


def fetch_market_data(gateway: Any, *, ma_period: int = MA_PERIOD) -> Dict[str, Any]:
    """由 APIGateway 取得標準化 Market Snapshot；指數只走 canonical index 介面。"""
    warnings: List[str] = []
    stage_errors: List[Dict[str, str]] = []
    taiex: Optional[Dict[str, Any]] = None
    tpex: Optional[Dict[str, Any]] = None
    twse_rows: List[Dict[str, Any]] = []
    tpex_rows: List[Dict[str, Any]] = []
    sector_map: Dict[str, str] = {}

    def record_error(stage: str, message: str) -> None:
        entry = {"stage": str(stage), "message": str(message)}
        stage_errors.append(entry)
        warnings.append(f"{stage}: {message}")

    jobs = {}
    resources = getattr(gateway, "_resource_manager", None)
    pool = resources.io_pool("MarketContext", 4) if resources else getattr(gateway, "_market_context_pool", None)
    if pool is None:
        pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="MarketContext")
        gateway._market_context_pool = pool
    pending = getattr(gateway, "_market_context_pending", {})
    gateway._market_context_pending = pending
    deadline = time.monotonic() + 20.0
    round_deadline = getattr(getattr(gateway, "market", None), "_round_deadline", 0)
    if round_deadline > 0:
        deadline = min(deadline, round_deadline)
    for key, method, args in (
        ("taiex", "get_index_quote", ("t00", "TWSE")),
        ("tpex", "get_index_quote", ("o00", "TPEx")),
        ("twse_rows", "get_daily_snapshot", ("TWSE",)),
        ("tpex_rows", "get_daily_snapshot", ("TPEx",)),
    ):
        fn = getattr(gateway, method, None)
        if callable(fn):
            previous = pending.get(key)
            if previous is None or previous.done():
                previous = pool.submit(fn, *args)
                pending[key] = previous
            jobs[key] = previous

    get_index_quote = getattr(gateway, "get_index_quote", None)
    if callable(get_index_quote):
        try:
            payload = jobs["taiex"].result(timeout=max(0.0, deadline - time.monotonic()))
            if isinstance(payload, Mapping):
                taiex = dict(payload)
            else:
                record_error("INDEX_QUOTE_TAIEX_ERROR", "回傳格式不是 Mapping")
        except Exception as exc:
            record_error(
                "INDEX_QUOTE_TAIEX_ERROR",
                f"{type(exc).__name__}: {exc}",
            )
        try:
            payload = jobs["tpex"].result(timeout=max(0.0, deadline - time.monotonic()))
            if isinstance(payload, Mapping):
                tpex = dict(payload)
            else:
                record_error("INDEX_QUOTE_TPEX_ERROR", "回傳格式不是 Mapping")
        except Exception as exc:
            record_error(
                "INDEX_QUOTE_TPEX_ERROR",
                f"{type(exc).__name__}: {exc}",
            )
    else:
        record_error(
            "INDEX_QUOTE_INTERFACE_MISSING",
            "APIGateway 未提供 get_index_quote()",
        )

    get_daily_snapshot = getattr(gateway, "get_daily_snapshot", None)
    if callable(get_daily_snapshot):
        try:
            payload = jobs["twse_rows"].result(timeout=max(0.0, deadline - time.monotonic()))
            if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
                twse_rows = _stable_market_rows(payload)
            else:
                record_error("SNAPSHOT_QUOTES_TWSE_ERROR", "回傳格式不是 Sequence")
        except Exception as exc:
            record_error(
                "SNAPSHOT_QUOTES_TWSE_ERROR",
                f"{type(exc).__name__}: {exc}",
            )
        try:
            payload = jobs["tpex_rows"].result(timeout=max(0.0, deadline - time.monotonic()))
            if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
                tpex_rows = _stable_market_rows(payload)
            else:
                record_error("SNAPSHOT_QUOTES_TPEX_ERROR", "回傳格式不是 Sequence")
        except Exception as exc:
            record_error(
                "SNAPSHOT_QUOTES_TPEX_ERROR",
                f"{type(exc).__name__}: {exc}",
            )
    else:
        record_error(
            "SNAPSHOT_QUOTES_INTERFACE_MISSING",
            "APIGateway 未提供 get_daily_snapshot()",
        )

    taiex_history, taiex_history_source = _load_gateway_index_history(
        gateway,
        symbol="t00",
        market="TWSE",
        current_quote=taiex,
        period=ma_period,
        stage_errors=stage_errors,
        stage_name="INDEX_HISTORY_TAIEX_ERROR",
    )
    tpex_history, tpex_history_source = _load_gateway_index_history(
        gateway,
        symbol="o00",
        market="TPEx",
        current_quote=tpex,
        period=ma_period,
        stage_errors=stage_errors,
        stage_name="INDEX_HISTORY_TPEX_ERROR",
    )
    for item in stage_errors:
        text = f"{item.get('stage')}: {item.get('message')}"
        if text not in warnings:
            warnings.append(text)

    get_sector_map = getattr(gateway, "get_sector_map", None)
    if callable(get_sector_map):
        try:
            payload = get_sector_map()
            sector_map = _normalize_sector_map_payload(payload)
        except Exception as exc:
            record_error(
                "SECTOR_MAP_ERROR",
                f"{type(exc).__name__}: {exc}",
            )

    provider = "APIGATEWAY_STANDARDIZED"
    status_method = getattr(gateway, "status", None)
    if callable(status_method):
        try:
            status = status_method()
            if isinstance(status, Mapping):
                fugle_status = status.get("fugle")
                fugle_provider = (
                    fugle_status.get("provider")
                    if isinstance(fugle_status, Mapping)
                    else None
                )
                provider = (
                    str(status.get("market_provider") or fugle_provider or provider)
                    .strip()
                    .upper()
                )
        except Exception as exc:
            record_error(
                "GATEWAY_STATUS_ERROR",
                f"{type(exc).__name__}: {exc}",
            )

    payload: Dict[str, Any] = {
        "taiex": taiex,
        "tpex": tpex,
        "twse_rows": twse_rows,
        "tpex_rows": tpex_rows,
        "taiex_history": taiex_history,
        "tpex_history": tpex_history,
        "taiex_history_source": taiex_history_source,
        "tpex_history_source": tpex_history_source,
        "sector_map": dict(sorted(sector_map.items())),
        "sector_map_source": (
            "APIGateway.get_sector_map" if sector_map else "UNAVAILABLE"
        ),
        "market_provider": provider,
        "input_contract": "APIGATEWAY_CANONICAL_INDEX_AND_STOCK_SNAPSHOT",
        "warnings": warnings,
        "stage_errors": stage_errors,
    }
    session_state, session_trade_date = _snapshot_session_state(payload)
    payload["session_state"] = session_state
    payload["session_trade_date"] = session_trade_date
    now = datetime.now(timezone.utc)
    input_health = {}
    for name, rows in (("taiex", [taiex]), ("tpex", [tpex])):
        times = [_row_source_datetime(row) for row in rows if isinstance(row, Mapping)]
        ages = [(now-t.astimezone(timezone.utc)).total_seconds() for t in times if t]
        input_health[name] = {"complete": bool(times) and all(t is not None for t in times),
                              "oldest_age_seconds": max(ages) if ages else None,
                              "newest_age_seconds": min(ages) if ages else None}
    payload["input_health"] = input_health
    snapshot_times = {}
    for name, rows in (("TWSE", twse_rows), ("TPEX", tpex_rows)):
        times = [_parse_source_datetime(row.get("snapshot_acquired_at")) for row in rows]
        snapshot_times[name] = (min(times).isoformat() if times and all(t is not None for t in times) else None)
        input_health[name] = {"complete": bool(times) and all(t is not None for t in times),
                             "degraded": any(row.get("source_degraded") or str(row.get("trade_date") or "").replace("-", "") != now.astimezone(timezone(timedelta(hours=8))).strftime("%Y%m%d") for row in rows)}
    payload["breadth_snapshot_acquired_at"] = snapshot_times
    payload["realtime_buy_data_ready"] = (session_state == "CURRENT_SESSION"
        and not stage_errors and all(h["complete"] and not h.get("degraded")
        for h in input_health.values()))
    if session_state == "PREVIOUS_SESSION":
        payload["warnings"] = sorted(
            {
                *[str(item) for item in payload.get("warnings") or []],
                "PREVIOUS_SESSION：上一交易日資料可供 Market Context／診斷，但不得視為即時 BUY 資料。",
            }
        )
    return _snapshot_market_data(payload)


# =============================================================================
# 指數分析
# =============================================================================


def _history_ma(history: Any, period: int) -> Optional[float]:
    if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
        return None
    values: List[float] = []
    for item in history:
        if isinstance(item, Mapping):
            value = _effective_price(item)
            if value is None:
                value = _to_float(item.get("close"))
        else:
            value = _to_float(item)
        if value is not None and value > 0:
            values.append(float(value))
    if len(values) < period:
        return None
    sample = values[-period:]
    return sum(sample) / len(sample)


def analyze_index(
    name: str,
    quote: Optional[Mapping[str, Any]],
    history: Any,
    params: MarketParameters,
    *,
    history_source: str = "MARKET_DATA",
) -> Dict[str, Any]:
    if not isinstance(quote, Mapping):
        return {
            "name": name,
            "available": False,
            "reason": "無指數行情",
            "score": None,
        }

    price = _effective_price(quote)
    open_price = _to_float(quote.get("open"))
    prev_close = _to_float(quote.get("prev_close"))
    high = _to_float(quote.get("high"))
    low = _to_float(quote.get("low"))
    change_pct = _pct_change(price, prev_close)
    open_change_pct = _pct_change(price, open_price)
    ma = _history_ma(history, params.ma_period)
    history_count = (
        len(history)
        if isinstance(history, Sequence) and not isinstance(history, (str, bytes))
        else 0
    )
    ma_distance_pct = _pct_change(price, ma)

    if price is None or change_pct is None:
        return {
            "name": name,
            "available": False,
            "reason": "price / prev_close 不完整",
            "symbol": str(quote.get("symbol") or ""),
            "trade_date": str(quote.get("trade_date") or ""),
            "timestamp": quote.get("timestamp"),
            "price": _round(price),
            "open": _round(open_price),
            "high": _round(high),
            "low": _round(low),
            "prev_close": _round(prev_close),
            "change_pct": _round(change_pct),
            "score": None,
        }

    change_score = _clamp(change_pct / params.index_change_full_scale_pct * 100.0)

    direction_basis = "OPEN"
    direction_pct = open_change_pct
    if ma is not None:
        direction_basis = f"MA{params.ma_period}"
        direction_pct = ma_distance_pct

    if direction_pct is None:
        direction_score = 0.0
        short_direction = "FLAT"
    else:
        direction_score = _clamp(
            direction_pct / params.direction_full_scale_pct * 100.0
        )
        if direction_pct > 0.05:
            short_direction = "UP"
        elif direction_pct < -0.05:
            short_direction = "DOWN"
        else:
            short_direction = "FLAT"

    score = (
        change_score * params.index_change_weight
        + direction_score * params.index_direction_weight
    )

    return {
        "name": name,
        "available": True,
        "symbol": str(quote.get("symbol") or ""),
        "market": str(quote.get("market") or ""),
        "trade_date": str(quote.get("trade_date") or ""),
        "timestamp": quote.get("timestamp"),
        "price": _round(price),
        "open": _round(open_price),
        "high": _round(high),
        "low": _round(low),
        "prev_close": _round(prev_close),
        "change_pct": _round(change_pct),
        "ma_period": params.ma_period,
        "history_count": history_count,
        "history_source": history_source,
        "ma_available": ma is not None,
        "ma": _round(ma),
        "ma_distance_pct": _round(ma_distance_pct),
        "short_direction": short_direction,
        "direction_basis": direction_basis,
        "direction_pct": _round(direction_pct),
        "change_score": _round(change_score, 2),
        "direction_score": _round(direction_score, 2),
        "score": _round(score, 2),
    }


# =============================================================================
# 市場廣度商品過濾
# =============================================================================


def _explicit_security_type(row: Mapping[str, Any]) -> str:
    keys = (
        "security_type",
        "product_type",
        "instrument_type",
        "securityType",
        "證券類別",
        "商品類別",
    )
    for container in (row, row.get("raw_response")):
        if not isinstance(container, Mapping):
            continue
        for key in keys:
            value = container.get(key)
            if value is not None and str(value).strip():
                return str(value).strip().upper()
    return ""


def _is_common_stock(row: Mapping[str, Any]) -> Tuple[bool, str]:
    """
    保守辨識台灣上市／上櫃普通股。

    現行 APIGateway 的每日快照沒有標準 security_type 欄位，因此以官方代號
    形態作固定 fallback：普通股須為首碼非 0 的 4 碼數字；另排除名稱明確標示 DR。
    若未來 Gateway 提供 security_type，則優先採用明確類型。
    """
    explicit = _explicit_security_type(row)
    if explicit:
        reject_tokens = (
            "ETF",
            "ETN",
            "WARRANT",
            "權證",
            "PREFERRED",
            "特別股",
            "DEPOSITARY",
            "TDR",
            "存託憑證",
            "BENEFICIARY",
            "受益",
            "BOND",
            "債",
        )
        if any(token in explicit for token in reject_tokens):
            return False, "explicit_non_common_stock"

    symbol = str(row.get("symbol") or "").strip()
    if re.fullmatch(COMMON_STOCK_SYMBOL_PATTERN, symbol) is None:
        return False, "symbol_not_common_stock"

    name = str(row.get("name") or "").strip().upper()
    if "-DR" in name or name.endswith("DR") or "TDR" in name or "存託憑證" in name:
        return False, "depositary_receipt"

    return True, "common_stock"


def filter_common_stock_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    enabled: bool = COMMON_STOCK_ONLY,
) -> Tuple[List[Mapping[str, Any]], Dict[str, Any]]:
    if not enabled:
        output = [row for row in rows if isinstance(row, Mapping)]
        return output, {
            "mode": "ALL_SECURITIES",
            "input_rows": len(output),
            "eligible_rows": len(output),
            "excluded_rows": 0,
            "excluded_reasons": {},
        }

    output: List[Mapping[str, Any]] = []
    reasons: Dict[str, int] = {}
    input_rows = 0
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        input_rows += 1
        allowed, reason = _is_common_stock(row)
        if allowed:
            output.append(row)
        else:
            reasons[reason] = reasons.get(reason, 0) + 1

    return output, {
        "mode": "COMMON_STOCK_ONLY",
        "rule": "首碼非 0 的 4 碼股票代號；優先尊重明確 security_type，排除 DR",
        "input_rows": input_rows,
        "eligible_rows": len(output),
        "excluded_rows": input_rows - len(output),
        "excluded_reasons": dict(sorted(reasons.items())),
    }


# =============================================================================
# 市場廣度與成交狀況
# =============================================================================


def analyze_breadth(
    rows: Sequence[Mapping[str, Any]],
    params: MarketParameters,
) -> Dict[str, Any]:
    advancers = 0
    decliners = 0
    unchanged = 0
    usable = 0
    total_turnover = 0.0
    advancing_turnover = 0.0
    declining_turnover = 0.0
    unchanged_turnover = 0.0
    total_volume = 0
    advancing_volume = 0
    declining_volume = 0

    for row in rows:
        price = _effective_price(row)
        prev_close = _to_float(row.get("prev_close"))
        if price is None or prev_close is None or prev_close <= 0:
            continue
        change_pct = _pct_change(price, prev_close)
        if change_pct is None:
            continue

        usable += 1
        turnover = max(0.0, _to_float(row.get("turnover")) or 0.0)
        volume = max(0, _to_int(row.get("volume")))
        total_turnover += turnover
        total_volume += volume

        if change_pct > 1e-9:
            advancers += 1
            advancing_turnover += turnover
            advancing_volume += volume
        elif change_pct < -1e-9:
            decliners += 1
            declining_turnover += turnover
            declining_volume += volume
        else:
            unchanged += 1
            unchanged_turnover += turnover

    directional_count = advancers + decliners
    # GUI 顯示的 ADR，維持不變
    breadth_ratio = (
        (advancers / decliners) * 100.0 if decliners > 0 else None
    )
    advance_ratio = advancers / usable if usable > 0 else None
    decline_ratio = decliners / usable if usable > 0 else None

    net_breadth_ratio = (
        (advancers - decliners) / directional_count
        if directional_count > 0
        else None
    )

    turnover_flow_ratio = (
        (advancing_turnover - declining_turnover) / total_turnover
        if total_turnover > 0
        else None
    )
    volume_flow_ratio = (
        (advancing_volume - declining_volume) / total_volume
        if total_volume > 0
        else None
    )

    if directional_count <= 0:
        score = None
    else:
        # Market Score 內部市場廣度計分
        count_score =  (
            ((advancers - decliners) / directional_count) * 100.0
            if directional_count > 0
            else 0.0
        )
        turnover_score = (
            turnover_flow_ratio * 100.0
            if turnover_flow_ratio is not None
            else count_score
        )
        score = (
            count_score * params.breadth_count_weight
            + turnover_score * params.breadth_turnover_weight
        )

    return {
        "available": score is not None,
        "total_stocks": usable,
        "advancers": advancers,
        "decliners": decliners,
        "unchanged": unchanged,
        "advance_ratio": _round(advance_ratio),
        "decline_ratio": _round(decline_ratio),
        "ratio": _round(breadth_ratio),
        "net_ratio": _round(net_breadth_ratio),
        "turnover_flow_ratio": _round(turnover_flow_ratio),
        "volume_flow_ratio": _round(volume_flow_ratio),
        "score": _round(score, 2),
        "turnover": {
            "total": _round(total_turnover, 2),
            "advancing": _round(advancing_turnover, 2),
            "declining": _round(declining_turnover, 2),
            "unchanged": _round(unchanged_turnover, 2),
        },
        "volume": {
            "total": total_volume,
            "advancing": advancing_volume,
            "declining": declining_volume,
        },
    }


# =============================================================================
# 類股分類來源
# =============================================================================


def _normalize_sector_map_market(value: Any) -> str:
    text = str(value or "").strip()
    upper = text.upper()
    if upper in {"TWSE", "TSE", "LISTED"}:
        return "TWSE"
    if upper in {"TPEX", "OTC"}:
        return "TPEx"
    return text


def _normalize_sector_map_payload(payload: Any) -> Dict[str, str]:
    output: Dict[str, str] = {}
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if isinstance(value, Mapping):
                market = _normalize_sector_map_market(value.get("market"))
                symbol = str(value.get("symbol") or value.get("code") or key).strip()
                sector = str(
                    value.get("sector")
                    or value.get("industry")
                    or value.get("industry_name")
                    or value.get("產業別")
                    or ""
                ).strip()
                if symbol and sector:
                    output[symbol] = sector
                    if market:
                        output[f"{market}:{symbol}"] = sector
            elif str(value).strip():
                output[str(key).strip()] = str(value).strip()
        return output

    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        for row in payload:
            if not isinstance(row, Mapping):
                continue
            market = _normalize_sector_map_market(row.get("market") or row.get("市場"))
            symbol = str(
                row.get("symbol")
                or row.get("code")
                or row.get("Code")
                or row.get("證券代號")
                or row.get("代號")
                or ""
            ).strip()
            sector = str(
                row.get("sector")
                or row.get("industry")
                or row.get("industry_name")
                or row.get("產業別")
                or row.get("產業分類")
                or ""
            ).strip()
            if symbol and sector:
                output[symbol] = sector
                if market:
                    output[f"{market}:{symbol}"] = sector
    return output


def load_sector_map_file(path: str | os.PathLike[str]) -> Dict[str, str]:
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"找不到類股分類檔：{file_path}")

    suffix = file_path.suffix.lower()
    if suffix == ".json":
        payload = json.loads(file_path.read_text(encoding="utf-8-sig"))
        return _normalize_sector_map_payload(payload)

    if suffix == ".csv":
        with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        return _normalize_sector_map_payload(rows)

    raise ValueError("類股分類檔僅支援 .json / .csv")


def _gateway_sector_map(
    gateway: Any,
) -> Tuple[Dict[str, str], Optional[str], List[str]]:
    warnings: List[str] = []
    for method_name in ("get_sector_map", "get_security_master"):
        method = getattr(gateway, method_name, None)
        if not callable(method):
            continue
        try:
            payload = method()
            mapping = _normalize_sector_map_payload(payload)
            if mapping:
                return mapping, f"APIGateway.{method_name}", warnings
        except Exception as exc:
            warnings.append(
                f"APIGateway.{method_name} 類股資料取得失敗："
                f"{type(exc).__name__}: {exc}"
            )
    return {}, None, warnings


def resolve_sector_map(
    *,
    explicit_map: Optional[Mapping[str, str]],
    market_data: Optional[Mapping[str, Any]],
    gateway: Any = None,
    sector_map_path: Optional[str] = None,
) -> Tuple[Dict[str, str], str, List[str]]:
    """解析類股分類來源，不直接連線外部 API。"""
    warnings: List[str] = []

    if explicit_map:
        mapping = _normalize_sector_map_payload(explicit_map)
        return mapping, "EXPLICIT", warnings

    if isinstance(market_data, Mapping):
        embedded = market_data.get("sector_map")
        mapping = _normalize_sector_map_payload(embedded)
        if mapping:
            return mapping, "MARKET_DATA", warnings

    if gateway is not None:
        mapping, source, gateway_warnings = _gateway_sector_map(gateway)
        warnings.extend(gateway_warnings)
        if mapping and source:
            return mapping, source, warnings

    # 正式模式不再自動掃描環境變數或同資料夾 sector_map 檔，避免與 Fugle
    # 標準化市場 Snapshot 混用不同資料時間線。只有呼叫端明確指定檔案時才讀取。
    explicit_path = str(sector_map_path or "").strip()
    if explicit_path:
        path = Path(explicit_path)
        if path.exists():
            try:
                mapping = load_sector_map_file(path)
            except Exception as exc:
                warnings.append(
                    f"類股分類檔讀取失敗：{path}: {type(exc).__name__}: {exc}"
                )
            else:
                if mapping:
                    return mapping, f"FILE:{path.name}", warnings
        else:
            warnings.append(f"找不到明確指定的類股分類檔：{path}")

    return {}, "UNAVAILABLE", warnings


# =============================================================================
# 類股強度與排名
# =============================================================================


def _lookup_sector(
    row: Mapping[str, Any], sector_map: Optional[Mapping[str, str]]
) -> Optional[str]:
    market, symbol = _row_key(row)
    if sector_map:
        candidates = [f"{market}:{symbol}", symbol]
        for key in candidates:
            sector = sector_map.get(key)
            if sector is not None and str(sector).strip():
                return str(sector).strip()

    # 若未另外提供 sector_map，仍允許 APIGateway 日後直接在標準行情或
    # raw_response 中帶入產業別；不以股票代號自行猜測類股。
    sector_keys = ("sector", "industry", "industry_name", "產業別", "產業分類")
    for key in sector_keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    raw = row.get("raw_response")
    if isinstance(raw, Mapping):
        for key in sector_keys:
            value = raw.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    return None


def read_market_permission_context(*, parameters, mode="AUTO", manual_market_state=None,
                                   manual_trading_mode=None, now=None):
    """只讀 Market 原子發布；不依賴 Scanner 成功與否，也不觸發網路。"""
    effective = _effective_market_parameters(mode=mode, manual_market_state=manual_market_state,
        manual_trading_mode=manual_trading_mode, disabled_sectors=None, parameters=parameters)
    key = _market_request_key(effective, sector_map=None, sector_map_path=None)
    context = _MARKET_PUBLICATION.read_context(key)
    return resolve_strategy_market_context(context or {}, now=now)


def merge_sector_source_rows(universe, state_rows):
    """類股以真正較新的 REST 快照或逐筆資料計算，不以冷門股最後成交冒充整批取得時間。"""
    latest = {_row_key(row): row for row in state_rows}
    output = []
    for base in universe:
        row = dict(base)
        live = latest.get(_row_key(base), {})
        acquired = _parse_source_datetime(base.get("snapshot_acquired_at"))
        event = _parse_source_datetime(live.get("realtime_source_at") or live.get("source_at") or live.get("timestamp"))
        if event and (not acquired or event > acquired):
            row.update(live)
            row["sector_value_source_at"] = event.isoformat()
        elif acquired:
            row["sector_value_source_at"] = acquired.isoformat()
        output.append(row)
    return output


def refresh_candidate_sector(candidate, context, *, now, quote=None):
    """只重驗類股限制；保留候選分數、排名、市場 RS 與其他策略門檻。"""
    row = deepcopy(dict(candidate or {}))
    if not row or "sector_snapshots" not in context:
        return row
    key = f"{_normalize_sector_map_market(row.get('market'))}:{row.get('symbol')}"
    identity = (context.get("sector_identities") or {}).get(key)
    if identity:
        row.update(identity)
    code = row.get("sector_code")
    row["sector_snapshot"] = deepcopy((context.get("sector_snapshots") or {}).get(code) or {})
    row["sector_snapshot_max_age_seconds"] = context.get("sector_snapshot_max_age_seconds", 60)
    row["sector_permission_snapshot_id"] = context.get("sector_permission_snapshot_id")
    change = _to_float(row.get("change_pct", row.get("change_percent")))
    if quote:
        current_change = _pct_change(_effective_price(quote), _to_float(quote.get("prev_close")))
        if current_change is not None:
            change = current_change
    sector_change = _to_float(row["sector_snapshot"].get("change_pct"))
    row["sector_relative_strength"] = round(change-sector_change, 4) if change is not None and sector_change is not None else None
    old = list(row.get("trend_eligibility_reasons") or [])
    sector_codes = {"SECTOR_MAPPING_MISSING", "SECTOR_SNAPSHOT_MISSING", "SECTOR_DATA_STALE", "SECTOR_RS_NOT_STRONG"}
    reasons = [reason for reason in old if reason not in sector_codes]
    reason = candidate_sector_gate(row, now=now)
    if reason:
        reasons.append(reason)
    row["trend_eligibility_reasons"] = list(dict.fromkeys(reasons))
    row["trend_eligible"] = not reasons and (row.get("trend_eligible") is True or bool(set(old) & sector_codes))
    return row


def candidate_sector_gate(candidate, *, now):
    """Intraday／Trading 只讀候選固定代碼與快照，不再重新比對名稱。"""
    if "sector_mapping_status" not in candidate:
        return ""  # 舊離線輸入相容；正式 Scanner 一律產生此欄位。
    if not candidate.get("sector_code") or candidate.get("sector_mapping_status") != "MAPPED":
        return "SECTOR_MAPPING_MISSING"
    snapshot = candidate.get("sector_snapshot") or {}
    if not snapshot or _to_float(snapshot.get("change_pct")) is None:
        return "SECTOR_SNAPSHOT_MISSING"
    stamp = _parse_source_datetime(snapshot.get("source_at"))
    reference = _parse_source_datetime(now)
    age = (reference - stamp).total_seconds() if reference and stamp else None
    session = str(snapshot.get("session_trade_date") or "").replace("-", "")
    if (session and reference and session != reference.astimezone(timezone(timedelta(hours=8))).strftime("%Y%m%d")):
        return "SECTOR_DATA_STALE"
    if age is None or age < -2 or age > float(candidate.get("sector_snapshot_max_age_seconds", 60)):
        return "SECTOR_DATA_STALE"
    relative = _to_float(candidate.get("sector_relative_strength"))
    return "SECTOR_RS_NOT_STRONG" if relative is None or relative <= 0 else ""


def attach_sector_contract(context, rows, security_master, *, parameters, source, reference_at):
    """同輪固定股票分類；保留原本成交金額加權公式與來源選擇。"""
    context = deepcopy(dict(context))
    names = load_api_gateway_module()._INDUSTRY_NAMES
    codes = {name: code for code, name in names.items()}
    master = {str(row.get("symbol")): dict(row) for row in security_master if row.get("symbol")}
    mapping = context.get("sector_map") or {}
    identities, prepared = {}, []
    for raw in rows:
        row = dict(raw)
        market, symbol = _row_key(row)
        market = _normalize_sector_map_market(market)
        meta = master.get(symbol, {})
        name = str(meta.get("sector_name") or meta.get("sector") or _lookup_sector(row, mapping) or "")
        code = str(meta.get("sector_code") or row.get("sector_code") or row.get("industry_code") or codes.get(name) or "")
        if code == "00":
            code = ""
        if not code and name:
            # 舊使用者明確分類保留獨立名稱識別，不猜成官方產業代碼。
            code = "NAME:" + name
        identity = {"sector_code": code, "sector_name": name or names.get(code, code),
                    "sector_mapping_status": "MAPPED" if code else "SECTOR_MAPPING_MISSING"}
        identities[f"{market}:{symbol}"] = identity
        row.update(identity)
        row["sector"] = code
        prepared.append(row)
    snapshots = {}
    if source == "STOCK_DATA":
        # Scanner 原有基準只使用本輪候選輸入，計算責任集中到 Market。
        groups = {}
        for row in prepared:
            code = row["sector_code"]
            change = _pct_change(_effective_price(row), _to_float(row.get("prev_close")))
            if change is None:
                change = _to_float(row.get("change_pct", row.get("change_percent")))
            if not code or change is None:
                continue
            group = groups.setdefault(code, {"changes": [], "weighted": 0.0, "turnover": 0.0, "times": [], "sessions": []})
            turnover = max(0.0, _to_float(row.get("turnover")) or 0.0)
            group["changes"].append(change)
            group["weighted"] += turnover * change
            group["turnover"] += turnover
            stamp = _parse_source_datetime(row.get("sector_value_source_at") or row.get("realtime_source_at") or row.get("source_at") or row.get("timestamp"))
            group["times"].append(None if row.get("source_degraded") else stamp)
            if row.get("trade_date"):
                group["sessions"].append(str(row["trade_date"]).replace("-", ""))
        for code, group in groups.items():
            times = group["times"]
            source_time = min(times).isoformat() if times and all(times) else None
            snapshots[code] = {"sector_code": code, "change_pct": group["weighted"] / group["turnover"]
                               if group["turnover"] > 0 else sum(group["changes"]) / len(group["changes"]),
                               "source_at": source_time, "source": source, "count": len(group["changes"]),
                               "session_trade_date": (group["sessions"][0] if len(set(group["sessions"])) == 1
                                   else "MIXED_SESSION" if group["sessions"] else "")}
    if source != "STOCK_DATA" or not snapshots:
        source_times = [_parse_source_datetime(x) for x in (context.get("breadth_snapshot_acquired_at") or {}).values()]
        source_time = min(source_times).isoformat() if source_times and all(source_times) else None
        for row in (context.get("sector_context") or {}).get("ranking", []):
            name = str(row.get("sector") or "")
            code = str(row.get("sector_code") or codes.get(name) or ("NAME:" + name if name else ""))
            value = _to_float(row.get("turnover_weighted_change_pct"))
            if value is None:
                value = _to_float(row.get("avg_change_pct"))
            if code and value is not None:
                snapshots[code] = {"sector_code": code, "change_pct": value,
                                   "source_at": source_time, "source": source}
    for snapshot in snapshots.values():
        stamp = _parse_source_datetime(snapshot.get("source_at"))
        snapshot["session_trade_date"] = (snapshot.get("session_trade_date")
            or str(context.get("session_trade_date") or "").replace("-", "")
            or (stamp.astimezone(timezone(timedelta(hours=8))).strftime("%Y%m%d") if stamp else ""))
        snapshot["snapshot_id"] = _stable_hash(snapshot)
    context["sector_permission_snapshot_id"] = _stable_hash(snapshots)
    context["sector_identities"] = identities
    context["sector_snapshots"] = snapshots
    context["sector_snapshot_max_age_seconds"] = parameters.trend_permission_max_age_seconds
    context["sector_snapshot_reference_at"] = str(reference_at)
    context["sector_mapping_coverage"] = {"total": len(identities),
        "mapped": sum(bool(row["sector_code"]) for row in identities.values()),
        "missing_symbols": [key for key, row in identities.items() if not row["sector_code"]]}
    return context


def analyze_sectors(
    rows: Sequence[Mapping[str, Any]],
    *,
    sector_map: Optional[Mapping[str, str]],
    disabled_sectors: Sequence[str],
    params: MarketParameters,
    source: str = "UNSPECIFIED",
) -> Dict[str, Any]:
    disabled = set(disabled_sectors)
    grouped: Dict[str, Dict[str, Any]] = {}
    analyzable_stocks = 0
    mapped_stocks = 0
    disabled_stocks = 0

    for row in rows:
        price = _effective_price(row)
        prev_close = _to_float(row.get("prev_close"))
        change_pct = _pct_change(price, prev_close)
        if change_pct is None:
            continue
        analyzable_stocks += 1

        sector = _lookup_sector(row, sector_map)
        if not sector:
            continue
        mapped_stocks += 1
        if sector in disabled:
            disabled_stocks += 1
            continue

        bucket = grouped.setdefault(
            sector,
            {
                "sector": sector,
                "count": 0,
                "advancers": 0,
                "decliners": 0,
                "unchanged": 0,
                "change_sum": 0.0,
                "turnover": 0.0,
                "turnover_change_sum": 0.0,
            },
        )
        bucket["count"] += 1
        bucket["change_sum"] += change_pct
        turnover = max(0.0, _to_float(row.get("turnover")) or 0.0)
        bucket["turnover"] += turnover
        bucket["turnover_change_sum"] += change_pct * turnover
        if change_pct > 1e-9:
            bucket["advancers"] += 1
        elif change_pct < -1e-9:
            bucket["decliners"] += 1
        else:
            bucket["unchanged"] += 1

    ranked: List[Dict[str, Any]] = []
    included_stocks = 0
    weighted_sector_score = 0.0

    for sector, bucket in grouped.items():
        count = int(bucket["count"])
        if count <= 0:
            continue
        avg_change = bucket["change_sum"] / count
        directional_count = bucket["advancers"] + bucket["decliners"]
        breadth_ratio = (
            (bucket["advancers"] - bucket["decliners"]) / directional_count
            if directional_count > 0
            else 0.0
        )
        if bucket["turnover"] > 0:
            turnover_weighted_change = (
                bucket["turnover_change_sum"] / bucket["turnover"]
            )
        else:
            turnover_weighted_change = avg_change

        change_score = _clamp(
            turnover_weighted_change / params.sector_change_full_scale_pct * 100.0
        )
        sector_score = _clamp(change_score * 0.70 + breadth_ratio * 100.0 * 0.30)
        included_stocks += count
        weighted_sector_score += sector_score * count

        ranked.append(
            {
                "sector": sector,
                "count": count,
                "advancers": int(bucket["advancers"]),
                "decliners": int(bucket["decliners"]),
                "unchanged": int(bucket["unchanged"]),
                "avg_change_pct": _round(avg_change),
                "turnover_weighted_change_pct": _round(turnover_weighted_change),
                "breadth_ratio": _round(breadth_ratio),
                "turnover": _round(bucket["turnover"], 2),
                "strength_score": _round(sector_score, 2),
            }
        )

    ranked.sort(
        key=lambda item: (
            float(item["strength_score"] or 0.0),
            float(item["turnover"] or 0.0),
            str(item["sector"]),
        ),
        reverse=True,
    )
    for rank, item in enumerate(ranked, 1):
        item["rank"] = rank

    score = weighted_sector_score / included_stocks if included_stocks > 0 else None
    mapping_coverage = (
        mapped_stocks / analyzable_stocks if analyzable_stocks > 0 else 0.0
    )
    strong = [
        item["sector"]
        for item in ranked
        if float(item["strength_score"] or 0.0) >= params.sector_strong_score
    ]

    return {
        "available": score is not None,
        "source": source,
        "eligible_stocks": analyzable_stocks,
        "mapped_stocks": mapped_stocks,
        "unmapped_stocks": max(0, analyzable_stocks - mapped_stocks),
        "mapping_coverage": _round(mapping_coverage, 4),
        "mapping_complete": analyzable_stocks > 0
        and mapped_stocks == analyzable_stocks,
        "disabled_stocks": disabled_stocks,
        "included_stocks": included_stocks,
        "disabled_sectors": sorted(disabled),
        "sector_count": len(ranked),
        "score": _round(score, 2),
        "strong_sectors": strong,
        "ranking": ranked,
        "mapping_required": mapped_stocks == 0,
    }


# =============================================================================
# 市場狀態、交易模式與多方許可
# =============================================================================


def _auto_market_state(
    score: float,
    taiex: Mapping[str, Any],
    tpex: Mapping[str, Any],
    breadth: Mapping[str, Any],
    params: MarketParameters,
) -> Tuple[str, Optional[str]]:
    taiex_change = _to_float(taiex.get("change_pct"))
    tpex_change = _to_float(tpex.get("change_pct"))
    breadth_ratio = _to_float(breadth.get("net_ratio"))

    panic_reason: Optional[str] = None
    if taiex_change is not None and taiex_change <= params.panic_taiex_drop_pct:
        panic_reason = (
            f"TAIEX {taiex_change:.2f}% <= {params.panic_taiex_drop_pct:.2f}%"
        )
    elif tpex_change is not None and tpex_change <= params.panic_tpex_drop_pct:
        panic_reason = f"TPEX {tpex_change:.2f}% <= {params.panic_tpex_drop_pct:.2f}%"
    elif breadth_ratio is not None and breadth_ratio <= params.panic_breadth_ratio:
        panic_reason = (
            f"breadth {breadth_ratio:.3f} <= {params.panic_breadth_ratio:.3f}"
        )

    if panic_reason is not None:
        return "PANIC", panic_reason
    if score >= params.bull_threshold:
        return "BULL", None
    if score <= params.bear_threshold:
        return "WEAK", None
    return "NORMAL", None


def _default_trading_mode(state: str) -> str:
    return {
        "BULL": "TREND",
        "NORMAL": "SELECTIVE_LONG",
        "WEAK": "DEFENSIVE",
        "PANIC": "STOP",
    }[state]


def _long_permission(state: str, trading_mode: str) -> Tuple[bool, str]:
    if state == "PANIC" or trading_mode == "STOP":
        return False, "BLOCK_LONG"
    if state == "BULL" and trading_mode == "TREND":
        return True, "ALLOW_LONG"
    return True, "SELECTIVE_LONG"


def _market_momentum(
    taiex: Mapping[str, Any],
    tpex: Mapping[str, Any],
    params: MarketParameters,
) -> str:
    weighted = _weighted_average(
        [
            (_to_float(taiex.get("change_pct")), params.taiex_index_weight),
            (_to_float(tpex.get("change_pct")), params.tpex_index_weight),
        ]
    )
    if weighted is None:
        return "UNKNOWN"
    if weighted > 0.15:
        return "UP"
    if weighted < -0.15:
        return "DOWN"
    return "FLAT"


# =============================================================================
# 核心純函式
# =============================================================================


def _trend_permission_policy(context: Mapping[str, Any]) -> Dict[str, Any]:
    """市場層唯一規則；短線方向與當日漲跌分開，個股結構不在此放寬。"""
    parameters = context.get("parameters") or {}
    score = _to_float(context.get("market_score"))
    breadth = _to_float((context.get("breadth") or {}).get("net_ratio"))
    indices = [context.get(name) or {} for name in ("taiex", "tpex")]
    changes = [_to_float(index.get("change_pct")) for index in indices]
    direction = context.get("intraday_trend")
    reason = "MARKET_NOT_STRONG"
    allowed = False
    if context.get("market_state") == "PANIC" or context.get("auto_market_state") == "PANIC" or context.get("panic_reason"):
        reason = "PANIC"
    elif (context.get("realtime_buy_data_ready") is not True
          or context.get("market_state") not in {"BULL", "NORMAL", "WEAK", "PANIC"}
          or context.get("session_state") == "PREVIOUS_SESSION"
          or any(index.get("available") is not True for index in indices)
          or score is None or breadth is None or any(value is None for value in changes)):
        reason = "MARKET_DATA_INVALID"
    elif context.get("market_state") == "WEAK" or context.get("auto_market_state") == "WEAK" or score <= float(parameters.get("BEAR_THRESHOLD", -30)):
        reason = "MARKET_WEAK"
    elif context.get("allow_long") is not True:
        reason = "MARKET_LONG_BLOCKED"
    elif direction == "UP":
        allowed, reason = True, "INTRADAY_UP"
    elif (direction in {"SIDEWAYS", "DOWN"}
          and score >= float(parameters.get("TREND_PULLBACK_SCORE_MIN", 30))
          and breadth >= float(parameters.get("TREND_PULLBACK_BREADTH_MIN", 0.20))
          and changes[0] > float(parameters.get("PANIC_TAIEX_DROP_PCT", PANIC_TAIEX_DROP_PCT))
          and changes[1] > float(parameters.get("PANIC_TPEX_DROP_PCT", PANIC_TPEX_DROP_PCT))):
        allowed = True
        reason = "STRONG_MARKET_PULLBACK" if direction == "DOWN" else "STRONG_MARKET_SIDEWAYS"
    evidence = {key: context.get(key) for key in (
        "market_state", "auto_market_state", "panic_reason", "allow_long",
        "intraday_trend", "market_score", "breadth", "taiex", "tpex",
        "parameters", "data_fingerprint", "parameter_fingerprint", "asof",
        "realtime_buy_data_ready", "session_state", "breadth_snapshot_acquired_at",
    )}
    return {"allowed": allowed, "reason": reason,
            "conditional": allowed and direction != "UP",
            "context_fingerprint": _stable_hash(evidence),
            "data_fingerprint": context.get("data_fingerprint"),
            "snapshot_id": (context.get("market_snapshot") or {}).get("snapshot_id"),
            "source_at": context.get("breadth_snapshot_acquired_at")}


def strategy_market_permission(context: Mapping[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """共用 TREND 許可驗證；只讀已發佈資料，不抓網路、不改股票策略。"""
    try:
        expected = _trend_permission_policy(context)
        stored = (context.get("strategy_market_permissions") or {}).get("TREND") or {}
        reason = expected["reason"]
        allowed = expected["allowed"]
        if not stored or stored != expected or not context.get("data_fingerprint"):
            allowed, reason = False, "PERMISSION_SNAPSHOT_MISMATCH"
        meta = context.get("market_snapshot") or {}
        if meta.get("stale") or meta.get("publication_state") in {"MISSING", "DEGRADED"}:
            allowed, reason = False, "MARKET_SNAPSHOT_STALE"
        current = now or datetime.now(timezone(timedelta(hours=8)))
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone(timedelta(hours=8)))
        # 指數讀來源時間，廣度讀 REST 成功取得時間；不使用冷門股最後成交時間。
        source_times = {name: _parse_source_datetime((context.get(name) or {}).get("timestamp")) for name in ("taiex", "tpex")}
        source_times.update({name: _parse_source_datetime((context.get("breadth_snapshot_acquired_at") or {}).get(name)) for name in ("TWSE", "TPEX")})
        times = list(source_times.values())
        limit = float((context.get("parameters") or {}).get("TREND_PERMISSION_MAX_AGE_SECONDS", 60))
        if not math.isfinite(limit) or not 0 < limit <= 300 or any(value is None for value in times):
            allowed, reason = False, "MARKET_SOURCE_TIME_INVALID"
        else:
            ages = [(current - value).total_seconds() for value in times]
            if min(ages) < -2 or max(ages) > limit:
                allowed, reason = False, "MARKET_PERMISSION_EXPIRED"
        session = str(context.get("session_trade_date") or meta.get("session_trade_date") or "").replace("-", "")
        if session and session != current.astimezone(timezone(timedelta(hours=8))).strftime("%Y%m%d"):
            allowed, reason = False, "MARKET_SESSION_MISMATCH"
        return dict(expected, allowed=allowed, reason=reason,
                    data_ready=reason in {"INTRADAY_UP", "STRONG_MARKET_PULLBACK", "STRONG_MARKET_SIDEWAYS", "MARKET_NOT_STRONG", "MARKET_WEAK", "PANIC", "MARKET_LONG_BLOCKED"} and context.get("realtime_buy_data_ready") is True,
                    snapshot_id=meta.get("snapshot_id"), max_age_seconds=limit,
                    evaluated_at=current.isoformat(),
                    freshness={name: {"source_at": value.isoformat() if value else None,
                                     "age_seconds": (current-value).total_seconds() if value else None}
                               for name, value in source_times.items()})
    except (TypeError, ValueError, AttributeError, OverflowError):
        return {"allowed": False, "reason": "MARKET_DATA_INVALID", "conditional": False}


def resolve_strategy_market_context(context: Mapping[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """由核心發布當次判定；GUI 只顯示，不另外計算許可。"""
    output = deepcopy(dict(context))
    permission = strategy_market_permission(output, now=now)
    output["trend_permission"] = permission
    output["trend_long_allowed"] = permission["allowed"]
    output["trend_long_reason"] = permission["reason"]
    output["permission_evaluated_at"] = permission.get("evaluated_at")
    output["market_buy_data_ready"] = permission.get("data_ready", False)
    meta = output.get("market_snapshot") or {}
    sources = [row.get("source_at") for row in (permission.get("freshness") or {}).values() if row.get("source_at")]
    identity = str(meta.get("snapshot_id") or permission.get("context_fingerprint") or "")
    output["market_permission_snapshot_id"] = identity
    output["market_guard"] = {
        "market_permission_snapshot_id": identity, "snapshot_id": identity,
        "source_at": min(sources) if sources else None,
        "refreshed_at": meta.get("refreshed_at"),
        "ttl_seconds": permission.get("max_age_seconds"),
        "session_trade_date": output.get("session_trade_date") or meta.get("session_trade_date"),
        "ready": permission.get("data_ready", False), "reason": permission.get("reason"),
        "strategy_permissions": {"TREND": permission.get("allowed", False),
            "MOMENTUM": permission.get("data_ready") is True and output.get("momentum_long_allowed") is True
                and output.get("market_state") != "PANIC"},
    }
    return output


def calculate_market_context(
    market_data: Mapping[str, Any],
    *,
    params: Optional[MarketParameters] = None,
    now: Optional[datetime] = None,
    sector_map: Optional[Mapping[str, str]] = None,
    sector_map_source: str = "UNSPECIFIED",
) -> Dict[str, Any]:
    """純市場判定：同一標準化 Snapshot + params + sector_map 必須得到相同輸出。"""
    effective = params or MarketParameters()
    effective.validate()
    market_data = _snapshot_market_data(market_data)

    taiex = analyze_index(
        "TAIEX",
        market_data.get("taiex"),
        market_data.get("taiex_history"),
        effective,
        history_source=str(market_data.get("taiex_history_source") or "MARKET_DATA"),
    )
    tpex = analyze_index(
        "TPEX",
        market_data.get("tpex"),
        market_data.get("tpex_history"),
        effective,
        history_source=str(market_data.get("tpex_history_source") or "MARKET_DATA"),
    )

    twse_rows = _stable_market_rows(market_data.get("twse_rows"))
    tpex_rows = _stable_market_rows(market_data.get("tpex_rows"))
    all_rows: List[Mapping[str, Any]] = list(twse_rows) + list(tpex_rows)
    stock_rows, security_filter = filter_common_stock_rows(
        all_rows,
        enabled=effective.common_stock_only,
    )

    effective_sector_map = _normalize_sector_map_payload(
        sector_map if sector_map is not None else market_data.get("sector_map")
    )
    effective_sector_source = str(sector_map_source or "UNSPECIFIED")
    if effective_sector_map and effective_sector_source == "UNSPECIFIED":
        if sector_map is None:
            effective_sector_source = str(
                market_data.get("sector_map_source") or "MARKET_DATA"
            )
        else:
            effective_sector_source = "CALCULATE_INPUT"
    elif not effective_sector_map and any(
        _lookup_sector(row, None) for row in stock_rows
    ):
        effective_sector_source = "MARKET_ROWS"

    breadth = analyze_breadth(stock_rows, effective)
    breadth["security_filter"] = security_filter
    sectors = analyze_sectors(
        stock_rows,
        sector_map=effective_sector_map,
        disabled_sectors=effective.disabled_sectors,
        params=effective,
        source=effective_sector_source,
    )

    index_score = _weighted_average(
        [
            (_to_float(taiex.get("score")), effective.taiex_index_weight),
            (_to_float(tpex.get("score")), effective.tpex_index_weight),
        ]
    )
    breadth_score = _to_float(breadth.get("score"))
    sector_score = _to_float(sectors.get("score"))

    sector_mapping_coverage = _to_float(sectors.get("mapping_coverage")) or 0.0
    sector_effective_weight = (
        effective.sector_weight * _clamp(sector_mapping_coverage, 0.0, 1.0)
        if sector_score is not None
        else 0.0
    )
    components: List[Tuple[str, Optional[float], float, float]] = [
        (
            "market",
            index_score,
            effective.market_score_weight,
            effective.market_score_weight if index_score is not None else 0.0,
        ),
        (
            "breadth",
            breadth_score,
            effective.breadth_weight,
            effective.breadth_weight if breadth_score is not None else 0.0,
        ),
        (
            "sector",
            sector_score,
            effective.sector_weight,
            sector_effective_weight,
        ),
    ]
    available_weight = sum(effective_weight for _, _, _, effective_weight in components)
    weighted_score_sum = sum(
        float(score) * effective_weight
        for _, score, _, effective_weight in components
        if score is not None and effective_weight > 0
    )
    market_score = (
        weighted_score_sum / available_weight if available_weight > 0 else 0.0
    )
    market_score = _clamp(market_score)
    score_coverage = available_weight * 100.0

    auto_state, panic_reason = _auto_market_state(
        market_score, taiex, tpex, breadth, effective
    )
    mode = _normalize_mode(effective.mode)
    final_state = auto_state
    final_trading_mode = _default_trading_mode(auto_state)
    if mode == "MANUAL":
        if effective.manual_market_state is not None:
            final_state = str(effective.manual_market_state).strip().upper()
        if effective.manual_trading_mode is not None:
            final_trading_mode = str(effective.manual_trading_mode).strip().upper()
        else:
            final_trading_mode = _default_trading_mode(final_state)

    allow_long, long_permission = _long_permission(final_state, final_trading_mode)
    directions = [taiex.get("short_direction"), tpex.get("short_direction")]
    breadth_value = _to_float(breadth.get("net_ratio"))
    intraday_trend = "SIDEWAYS"
    if (
        directions == ["UP", "UP"]
        and breadth_value is not None
        and breadth_value > effective.trend_breadth_min
    ):
        intraday_trend = "UP"
    elif directions == ["DOWN", "DOWN"]:
        intraday_trend = "DOWN"
    momentum_allowed = bool(
        allow_long and final_state != "PANIC" and auto_state != "PANIC"
    )

    turnover_context = {
        "total_turnover": breadth.get("turnover", {}).get("total"),
        "advancing_turnover": breadth.get("turnover", {}).get("advancing"),
        "declining_turnover": breadth.get("turnover", {}).get("declining"),
        "turnover_flow_ratio": breadth.get("turnover_flow_ratio"),
        "total_volume": breadth.get("volume", {}).get("total"),
        "volume_flow_ratio": breadth.get("volume_flow_ratio"),
    }

    warnings = sorted({str(item) for item in (market_data.get("warnings") or [])})
    if str(market_data.get("session_state") or "") == "PREVIOUS_SESSION":
        warnings.append(
            "PREVIOUS_SESSION：資料可供 Market Context／診斷，但 realtime_buy_data_ready=False。"
        )
    if index_score is None:
        warnings.append("市場指數資料不足，MARKET_SCORE_WEIGHT 未納入計分。")
    if breadth_score is None:
        warnings.append("市場廣度資料不足，BREADTH_WEIGHT 未納入計分。")
    if not bool(taiex.get("ma_available")):
        warnings.append(
            f"TAIEX MA{effective.ma_period} 歷史資料不足"
            f"（{taiex.get('history_count', 0)}/{effective.ma_period}），"
            "短線方向改用 OPEN。"
        )
    if not bool(tpex.get("ma_available")):
        warnings.append(
            f"TPEX MA{effective.ma_period} 歷史資料不足"
            f"（{tpex.get('history_count', 0)}/{effective.ma_period}），"
            "短線方向改用 OPEN。"
        )
    if sector_score is None:
        warnings.append(
            "類股分類資料不足；請由 APIGateway 提供正式公司產業資料，或提供 "
            "sector_map.json/csv。SECTOR_WEIGHT 未納入計分。"
        )
    elif sector_mapping_coverage < 1.0:
        warnings.append(
            "類股分類覆蓋率為 "
            f"{sector_mapping_coverage * 100.0:.1f}%；SECTOR_WEIGHT 依實際覆蓋率計入。"
        )

    parameters = effective.snapshot()
    canonical_data = {
        "breadth_snapshot_acquired_at": market_data.get("breadth_snapshot_acquired_at"),
        "taiex": _canonical_quote(market_data.get("taiex")),
        "tpex": _canonical_quote(market_data.get("tpex")),
        "twse_rows": _canonical_snapshot(
            [row for row in twse_rows if isinstance(row, Mapping)]
        ),
        "tpex_rows": _canonical_snapshot(
            [row for row in tpex_rows if isinstance(row, Mapping)]
        ),
        "taiex_history": market_data.get("taiex_history") or [],
        "tpex_history": market_data.get("tpex_history") or [],
        "taiex_history_source": market_data.get("taiex_history_source")
        or "MARKET_DATA",
        "tpex_history_source": market_data.get("tpex_history_source") or "MARKET_DATA",
        "security_filter": security_filter,
        "sector_map_source": effective_sector_source,
        "sector_map": dict(
            sorted((str(k), str(v)) for k, v in effective_sector_map.items())
        ),
        "resolved_sectors": dict(
            sorted(
                (
                    f"{_row_key(row)[0]}:{_row_key(row)[1]}",
                    _lookup_sector(row, effective_sector_map) or "",
                )
                for row in stock_rows
            )
        ),
        "session_state": str(market_data.get("session_state") or "UNSPECIFIED"),
        "session_trade_date": str(market_data.get("session_trade_date") or ""),
        "realtime_buy_data_ready": bool(
            market_data.get("realtime_buy_data_ready", True)
        ),
    }

    context = {
        "market_state": final_state,
        "auto_market_state": auto_state,
        "market_score": round(market_score, 2),
        "score_coverage": round(score_coverage, 1),
        "score_complete": math.isclose(score_coverage, 100.0, abs_tol=1e-9),
        "trading_mode": final_trading_mode,
        "allow_long": allow_long,
        "intraday_trend": intraday_trend,
        "momentum_long_allowed": momentum_allowed,
        "trend_long_allowed": False,
        "long_permission": long_permission,
        "market_momentum": _market_momentum(taiex, tpex, effective),
        "breadth_ratio": breadth.get("ratio"),
        "taiex": taiex,
        "tpex": tpex,
        "breadth": breadth,
        "security_filter": security_filter,
        "turnover_context": turnover_context,
        "sector_context": sectors,
        "sector_map": dict(sorted({**effective_sector_map, **canonical_data["resolved_sectors"]}.items())),
        "sector_map_source": effective_sector_source,
        "strong_sectors": list(sectors.get("strong_sectors") or []),
        "score_components": {
            "market": _round(index_score, 2),
            "breadth": _round(breadth_score, 2),
            "sector": _round(sector_score, 2),
        },
        "score_component_coverage": {
            name: _round(
                effective_weight / nominal_weight if nominal_weight > 0 else 0.0,
                4,
            )
            for name, _, nominal_weight, effective_weight in components
        },
        "panic_reason": panic_reason,
        "mode": mode,
        "parameters": parameters,
        "parameter_fingerprint": _stable_hash(parameters),
        "data_fingerprint": _stable_hash(canonical_data),
        "asof": _derive_asof(market_data),
        "session_state": str(market_data.get("session_state") or "UNSPECIFIED"),
        "session_trade_date": str(market_data.get("session_trade_date") or ""),
        "realtime_buy_data_ready": bool(
            market_data.get("realtime_buy_data_ready", True)
        ),
        "stage_errors": [
            deepcopy(dict(item))
            for item in (market_data.get("stage_errors") or [])
            if isinstance(item, Mapping)
        ],
        "warnings": warnings,
    }
    context["breadth_snapshot_acquired_at"] = deepcopy(market_data.get("breadth_snapshot_acquired_at") or {})
    permission = _trend_permission_policy(context)
    context["strategy_market_permissions"] = {"TREND": permission}
    context["trend_long_allowed"] = permission["allowed"]
    context["trend_long_reason"] = permission["reason"]
    reference_time = now or _parse_source_datetime(market_data.get("permission_evaluated_at")) or _parse_source_datetime(context["asof"])
    # 純計算不讀牆鐘；無來源時間時由 freshness 拒絕。
    return resolve_strategy_market_context(context, now=reference_time or datetime(1970, 1, 1, tzinfo=timezone.utc))


# =============================================================================
# 對外統一入口：Market Snapshot / Market Context 解耦
# =============================================================================


def _effective_market_parameters(
    *,
    mode: str,
    manual_market_state: Optional[str],
    manual_trading_mode: Optional[str],
    disabled_sectors: Optional[Iterable[str]],
    parameters: Optional[MarketParameters],
) -> MarketParameters:
    base = parameters or MarketParameters()
    effective = replace(
        base,
        mode=_normalize_mode(mode),
        manual_market_state=(
            None
            if manual_market_state is None
            else str(manual_market_state).strip().upper()
        ),
        manual_trading_mode=(
            None
            if manual_trading_mode is None
            else str(manual_trading_mode).strip().upper()
        ),
        disabled_sectors=(
            _clean_disabled(disabled_sectors)
            if disabled_sectors is not None
            else tuple(base.disabled_sectors)
        ),
    )
    effective.validate()
    return effective


def _calculate_context_from_snapshot_data(
    data: Mapping[str, Any],
    *,
    effective: MarketParameters,
    sector_map: Optional[Mapping[str, str]],
    sector_map_path: Optional[str],
) -> Dict[str, Any]:
    snapshot_data = _snapshot_market_data(data)
    embedded_sector_map = _normalize_sector_map_payload(snapshot_data.get("sector_map"))
    if sector_map is None and embedded_sector_map:
        resolved_sector_map = embedded_sector_map
        sector_source = str(snapshot_data.get("sector_map_source") or "MARKET_DATA")
        sector_warnings: List[str] = []
    else:
        resolved_sector_map, sector_source, sector_warnings = resolve_sector_map(
            explicit_map=sector_map,
            market_data=snapshot_data,
            gateway=None,
            sector_map_path=sector_map_path,
        )
    if sector_warnings:
        snapshot_data["warnings"] = sorted(
            {
                *[str(item) for item in (snapshot_data.get("warnings") or [])],
                *[str(item) for item in sector_warnings],
            }
        )


    return calculate_market_context(
        snapshot_data,
        params=effective,
        sector_map=resolved_sector_map,
        sector_map_source=sector_source,
    )


class _MarketRefreshError(RuntimeError):
    def __init__(
        self,
        stage: str,
        message: str,
        *,
        stage_errors: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> None:
        super().__init__(message)
        self.stage = str(stage or "MARKET_REFRESH_ERROR")
        self.stage_errors = [
            deepcopy(dict(item))
            for item in (stage_errors or [])
            if isinstance(item, Mapping)
        ]


def _refresh_error_details(exc: Exception) -> Tuple[str, str, List[Dict[str, Any]]]:
    stage = str(getattr(exc, "stage", "") or "MARKET_REFRESH_ERROR")
    message = f"{type(exc).__name__}: {exc}"
    stage_errors = [
        deepcopy(dict(item))
        for item in (getattr(exc, "stage_errors", None) or [])
        if isinstance(item, Mapping)
    ]
    return stage, message, stage_errors


def _validate_market_snapshot_for_publish(data: Mapping[str, Any]) -> None:
    """不讓未完成的背景市場更新覆寫既有 last-good Snapshot。"""
    missing: List[str] = []
    for key in ("taiex", "tpex"):
        quote = data.get(key)
        if not isinstance(quote, Mapping) or _effective_price(quote) is None:
            missing.append(key.upper())
    for key in ("twse_rows", "tpex_rows"):
        rows = data.get(key)
        if (
            not isinstance(rows, Sequence)
            or isinstance(rows, (str, bytes))
            or not any(isinstance(row, Mapping) for row in rows)
        ):
            missing.append(key.upper())
    if not missing:
        return

    stage_errors = [
        deepcopy(dict(item))
        for item in (data.get("stage_errors") or [])
        if isinstance(item, Mapping)
    ]
    stage = "SNAPSHOT_VALIDATION_ERROR"
    stage_priority = {
        "TAIEX": "INDEX_QUOTE_TAIEX_ERROR",
        "TPEX": "INDEX_QUOTE_TPEX_ERROR",
        "TWSE_ROWS": "SNAPSHOT_QUOTES_TWSE_ERROR",
        "TPEX_ROWS": "SNAPSHOT_QUOTES_TPEX_ERROR",
    }
    available_stages = {str(item.get("stage") or "") for item in stage_errors}
    for item in missing:
        candidate = stage_priority.get(item)
        if candidate in available_stages:
            stage = candidate
            break
    raise _MarketRefreshError(
        stage,
        "Market Snapshot 更新不完整，不發布：" + ", ".join(missing),
        stage_errors=stage_errors,
    )


def _build_market_snapshot_payload(
    *,
    effective: MarketParameters,
    request_key: str,
    gateway: Any,
    api_mode: str,
    replay_dir: str,
    sector_map: Optional[Mapping[str, str]],
    sector_map_path: Optional[str],
) -> Dict[str, Any]:
    active_gateway = gateway or create_api_gateway(
        api_mode=api_mode,
        replay_dir=replay_dir,
    )
    data = fetch_market_data(active_gateway, ma_period=effective.ma_period)
    _validate_market_snapshot_for_publish(data)

    resolved_sector_map, sector_source, sector_warnings = resolve_sector_map(
        explicit_map=sector_map,
        market_data=data,
        gateway=active_gateway,
        sector_map_path=sector_map_path,
    )
    if resolved_sector_map:
        data["sector_map"] = dict(sorted(resolved_sector_map.items()))
        data["sector_map_source"] = sector_source
    if sector_warnings:
        data["warnings"] = sorted(
            {
                *[str(item) for item in (data.get("warnings") or [])],
                *[str(item) for item in sector_warnings],
            }
        )

    snapshot_data = _snapshot_market_data(data)
    context = calculate_market_context(
        snapshot_data,
        params=effective,
        sector_map=resolved_sector_map,
        sector_map_source=sector_source,
    )
    calculated_at = _runtime_timestamp()
    return {
        "data": snapshot_data,
        "request_key": request_key,
        "context": context,
        "source_at": _derive_asof(snapshot_data),
        "calculated_at": calculated_at,
    }


def _refresh_market_snapshot_core(
    *,
    effective: MarketParameters,
    request_key: str,
    gateway: Any,
    api_mode: str,
    replay_dir: str,
    sector_map: Optional[Mapping[str, str]],
    sector_map_path: Optional[str],
) -> Dict[str, Any]:
    payload = _build_market_snapshot_payload(
        effective=effective,
        request_key=request_key,
        gateway=gateway,
        api_mode=api_mode,
        replay_dir=replay_dir,
        sector_map=sector_map,
        sector_map_path=sector_map_path,
    )
    return _MARKET_PUBLICATION.publish(**payload)


def refresh_market_snapshot(
    mode: str = "AUTO",
    manual_market_state: Optional[str] = None,
    manual_trading_mode: Optional[str] = None,
    disabled_sectors: Optional[Iterable[str]] = None,
    *,
    gateway: Any = None,
    api_mode: str = "LIVE",
    replay_dir: str = "api_replay",
    sector_map: Optional[Mapping[str, str]] = None,
    sector_map_path: Optional[str] = None,
    parameters: Optional[MarketParameters] = None,
) -> Dict[str, Any]:
    """同步建立完整 Market Snapshot，完成 Context 計算後一次 Atomic Publish。

    本函式供 Runtime / Market Worker 使用。更新失敗時不覆寫既有 last-good
    Snapshot；Candidate Scanner 應持續讀取已發布 Context。
    """
    effective = _effective_market_parameters(
        mode=mode,
        manual_market_state=manual_market_state,
        manual_trading_mode=manual_trading_mode,
        disabled_sectors=disabled_sectors,
        parameters=parameters,
    )
    request_key = _market_request_key(
        effective,
        sector_map=sector_map,
        sector_map_path=sector_map_path,
    )

    while not _MARKET_PUBLICATION.try_begin_refresh():
        time.sleep(0.01)

    error = ""
    error_stage = ""
    stage_errors: List[Dict[str, Any]] = []
    try:
        return _refresh_market_snapshot_core(
            effective=effective,
            request_key=request_key,
            gateway=gateway,
            api_mode=api_mode,
            replay_dir=replay_dir,
            sector_map=sector_map,
            sector_map_path=sector_map_path,
        )
    except Exception as exc:
        error_stage, error, stage_errors = _refresh_error_details(exc)
        raise
    finally:
        _MARKET_PUBLICATION.end_refresh(
            error,
            error_stage=error_stage,
            stage_errors=stage_errors if error else None,
        )


def request_market_snapshot_refresh(
    mode: str = "AUTO",
    manual_market_state: Optional[str] = None,
    manual_trading_mode: Optional[str] = None,
    disabled_sectors: Optional[Iterable[str]] = None,
    *,
    gateway: Any = None,
    api_mode: str = "LIVE",
    replay_dir: str = "api_replay",
    sector_map: Optional[Mapping[str, str]] = None,
    sector_map_path: Optional[str] = None,
    parameters: Optional[MarketParameters] = None,
    min_interval_seconds: float = MARKET_SNAPSHOT_REFRESH_MIN_INTERVAL_SECONDS,
    force_refresh: bool = False,
    timeout_seconds: float = MARKET_SNAPSHOT_BACKGROUND_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """非阻塞背景更新；逾時後必定清除 refreshing，逾時 worker 不得晚到發布。"""
    age = _MARKET_PUBLICATION.snapshot_age_seconds()
    if (
        not force_refresh
        and age is not None
        and age < max(0.0, float(min_interval_seconds))
    ):
        status = _MARKET_PUBLICATION.status()
        status.update(
            {
                "refresh_requested": False,
                "reason": "SNAPSHOT_FRESH",
                "snapshot_age_seconds": round(age, 3),
            }
        )
        return status

    effective = _effective_market_parameters(
        mode=mode,
        manual_market_state=manual_market_state,
        manual_trading_mode=manual_trading_mode,
        disabled_sectors=disabled_sectors,
        parameters=parameters,
    )
    request_key = _market_request_key(
        effective,
        sector_map=sector_map,
        sector_map_path=sector_map_path,
    )

    if not _MARKET_PUBLICATION.try_begin_refresh():
        status = _MARKET_PUBLICATION.status()
        status.update(
            {
                "refresh_requested": False,
                "reason": "REFRESH_IN_PROGRESS",
                "snapshot_age_seconds": age,
            }
        )
        return status

    timeout = max(0.1, float(timeout_seconds))

    def supervisor() -> None:
        result_box: Dict[str, Any] = {}
        completed = threading.Event()

        def build_only() -> None:
            try:
                result_box["payload"] = _build_market_snapshot_payload(
                    effective=effective,
                    request_key=request_key,
                    gateway=gateway,
                    api_mode=api_mode,
                    replay_dir=replay_dir,
                    sector_map=sector_map,
                    sector_map_path=sector_map_path,
                )
            except Exception as exc:
                result_box["exception"] = exc
            finally:
                completed.set()

        worker = threading.Thread(
            target=build_only,
            name="MarketSnapshotFetch",
            daemon=True,
        )
        worker.start()
        if not completed.wait(timeout):
            _MARKET_PUBLICATION.end_refresh(
                f"Market background refresh timeout after {timeout:.3f}s",
                error_stage="BACKGROUND_REFRESH_TIMEOUT",
                stage_errors=[
                    {
                        "stage": "BACKGROUND_REFRESH_TIMEOUT",
                        "message": f"timeout_seconds={timeout:.3f}",
                    }
                ],
            )
            return

        exc = result_box.get("exception")
        if isinstance(exc, Exception):
            error_stage, error, stage_errors = _refresh_error_details(exc)
            _MARKET_PUBLICATION.end_refresh(
                error,
                error_stage=error_stage,
                stage_errors=stage_errors,
            )
            return

        payload = result_box.get("payload")
        if not isinstance(payload, Mapping):
            _MARKET_PUBLICATION.end_refresh(
                "Market background refresh 未產生 publication payload",
                error_stage="BACKGROUND_PUBLICATION_PAYLOAD_ERROR",
            )
            return
        try:
            _MARKET_PUBLICATION.publish(**dict(payload))
        except Exception as exc:
            error_stage, error, stage_errors = _refresh_error_details(exc)
            _MARKET_PUBLICATION.end_refresh(
                error,
                error_stage=error_stage,
                stage_errors=stage_errors,
            )
            return
        _MARKET_PUBLICATION.end_refresh()

    thread = threading.Thread(
        target=supervisor,
        name="MarketSnapshotRefresh",
        daemon=True,
    )
    thread.start()
    status = _MARKET_PUBLICATION.status()
    status.update(
        {
            "refresh_requested": True,
            "reason": "BACKGROUND_REFRESH_STARTED",
            "snapshot_age_seconds": age,
            "timeout_seconds": timeout,
        }
    )
    return status


def get_market_snapshot_status() -> Dict[str, Any]:
    status = _MARKET_PUBLICATION.status()
    age = _MARKET_PUBLICATION.snapshot_age_seconds()
    status["snapshot_age_seconds"] = None if age is None else round(age, 3)
    return status


def get_market_context(
    mode: str = "AUTO",
    manual_market_state: Optional[str] = None,
    manual_trading_mode: Optional[str] = None,
    disabled_sectors: Optional[Iterable[str]] = None,
    *,
    gateway: Any = None,
    api_mode: str = "LIVE",
    replay_dir: str = "api_replay",
    market_data: Optional[Mapping[str, Any]] = None,
    sector_map: Optional[Mapping[str, str]] = None,
    sector_map_path: Optional[str] = None,
    parameters: Optional[MarketParameters] = None,
    auto_refresh_snapshot: bool = True,
    snapshot_refresh_min_interval_seconds: float = (
        MARKET_SNAPSHOT_REFRESH_MIN_INTERVAL_SECONDS
    ),
) -> Dict[str, Any]:
    """取得已發布 Market Context；Candidate Scan 不等待全市場更新。

    測試/重播若直接傳入 market_data，仍維持純函式路徑且不碰快照快取。
    LIVE 正式模式優先讀取最近一次 Atomic Publish 的 Context。若參數改變，
    只用既有 Market Snapshot 重新計算，不重新抓全市場；只有尚無任何 Snapshot
    的首次啟動才同步建立一份初始資料。
    """
    effective = _effective_market_parameters(
        mode=mode,
        manual_market_state=manual_market_state,
        manual_trading_mode=manual_trading_mode,
        disabled_sectors=disabled_sectors,
        parameters=parameters,
    )

    if market_data is not None:
        return _calculate_context_from_snapshot_data(
            dict(market_data),
            effective=effective,
            sector_map=sector_map,
            sector_map_path=sector_map_path,
        )

    request_key = _market_request_key(
        effective,
        sector_map=sector_map,
        sector_map_path=sector_map_path,
    )
    published = _MARKET_PUBLICATION.read_context(request_key)
    if published is not None:
        if auto_refresh_snapshot:
            request_market_snapshot_refresh(
                mode=mode,
                manual_market_state=manual_market_state,
                manual_trading_mode=manual_trading_mode,
                disabled_sectors=disabled_sectors,
                gateway=gateway,
                api_mode=api_mode,
                replay_dir=replay_dir,
                sector_map=sector_map,
                sector_map_path=sector_map_path,
                parameters=parameters,
                min_interval_seconds=snapshot_refresh_min_interval_seconds,
            )
        return published

    snapshot = _MARKET_PUBLICATION.read_snapshot()
    if snapshot is not None:
        snapshot_id = str(snapshot.get("snapshot_id") or "")
        data = snapshot.get("data")
        if isinstance(data, Mapping) and snapshot_id:
            context = _calculate_context_from_snapshot_data(
                data,
                effective=effective,
                sector_map=sector_map,
                sector_map_path=sector_map_path,
            )
            published = _MARKET_PUBLICATION.publish_context_if_current(
                snapshot_id=snapshot_id,
                request_key=request_key,
                context=context,
                calculated_at=_runtime_timestamp(),
            )
            if published is not None:
                if auto_refresh_snapshot:
                    request_market_snapshot_refresh(
                        mode=mode,
                        manual_market_state=manual_market_state,
                        manual_trading_mode=manual_trading_mode,
                        disabled_sectors=disabled_sectors,
                        gateway=gateway,
                        api_mode=api_mode,
                        replay_dir=replay_dir,
                        sector_map=sector_map,
                        sector_map_path=sector_map_path,
                        parameters=parameters,
                        min_interval_seconds=snapshot_refresh_min_interval_seconds,
                    )
                return published

            latest = _MARKET_PUBLICATION.read_context(request_key)
            if latest is not None:
                return latest

    # 尚無 last-good Snapshot 的首次啟動只能同步 seed；之後 Candidate Scan
    # 均走上方 published Context 路徑，不再綁定每輪全市場資料取得。
    return refresh_market_snapshot(
        mode=mode,
        manual_market_state=manual_market_state,
        manual_trading_mode=manual_trading_mode,
        disabled_sectors=disabled_sectors,
        gateway=gateway,
        api_mode=api_mode,
        replay_dir=replay_dir,
        sector_map=sector_map,
        sector_map_path=sector_map_path,
        parameters=parameters,
    )


# =============================================================================
# 離線自我測試
# =============================================================================


def _sample_row(
    symbol: str,
    market: str,
    price: float,
    prev_close: float,
    turnover: float,
    volume: int,
) -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "name": symbol,
        "market": market,
        "trade_date": "2026-08-31",
        "timestamp": "2026-08-31T12:30:00+08:00",
        "price": price,
        "open": prev_close,
        "high": max(price, prev_close),
        "low": min(price, prev_close),
        "prev_close": prev_close,
        "volume": volume,
        "turnover": turnover,
    }


def _self_test_data() -> Tuple[Dict[str, Any], Dict[str, str]]:
    twse = [
        _sample_row("2330", "TWSE", 103.0, 100.0, 3_000_000_000, 100000),
        _sample_row("2317", "TWSE", 101.5, 100.0, 2_000_000_000, 90000),
        _sample_row("2454", "TWSE", 102.0, 100.0, 1_500_000_000, 70000),
        _sample_row("2881", "TWSE", 100.5, 100.0, 900_000_000, 60000),
        _sample_row("2882", "TWSE", 99.8, 100.0, 800_000_000, 50000),
        _sample_row("1301", "TWSE", 99.0, 100.0, 400_000_000, 30000),
    ]
    tpex = [
        _sample_row("6488", "TPEx", 103.5, 100.0, 600_000_000, 25000),
        _sample_row("5347", "TPEx", 102.5, 100.0, 500_000_000, 22000),
        _sample_row("8069", "TPEx", 101.0, 100.0, 450_000_000, 20000),
        _sample_row("4128", "TPEx", 99.5, 100.0, 200_000_000, 15000),
        # 非普通股測試：ETF 與權證不得污染市場廣度。
        _sample_row("0050", "TWSE", 105.0, 100.0, 5_000_000_000, 300000),
        _sample_row("030001", "TWSE", 120.0, 100.0, 1_000_000_000, 50000),
    ]
    market_data = {
        "taiex": {
            "symbol": "t00",
            "market": "TWSE",
            "trade_date": "2026-08-31",
            "timestamp": "2026-08-31T12:30:00+08:00",
            "price": 10250.0,
            "open": 10120.0,
            "high": 10280.0,
            "low": 10100.0,
            "prev_close": 10000.0,
            "volume": 0,
            "turnover": 0.0,
        },
        "tpex": {
            "symbol": "o00",
            "market": "TPEx",
            "trade_date": "2026-08-31",
            "timestamp": "2026-08-31T12:30:00+08:00",
            "price": 202.5,
            "open": 201.0,
            "high": 203.0,
            "low": 200.5,
            "prev_close": 200.0,
            "volume": 0,
            "turnover": 0.0,
        },
        "taiex_history": [9900, 9950, 10000, 10080, 10120],
        "tpex_history": [195, 197, 198, 199, 200],
        "taiex_history_source": "SELF_TEST",
        "tpex_history_source": "SELF_TEST",
        "twse_rows": twse,
        "tpex_rows": tpex,
        "warnings": [],
    }
    sector_map = {
        "2330": "半導體",
        "2454": "半導體",
        "6488": "半導體",
        "5347": "半導體",
        "2317": "電子製造",
        "8069": "電子製造",
        "2881": "金融",
        "2882": "金融",
        "1301": "塑化",
        "4128": "生技醫療",
    }
    return market_data, sector_map


def run_self_test() -> Dict[str, Any]:
    checks = 0
    data, sector_map = _self_test_data()

    params = MarketParameters()
    params.validate()
    checks += 1

    result1 = calculate_market_context(data, params=params, sector_map=sector_map)
    result2 = calculate_market_context(data, params=params, sector_map=sector_map)
    assert result1 == result2
    checks += 1

    # 相同 Snapshot 僅改變資料列／History 回傳順序與本機 fetched_at，市場結果不得改變。
    reordered_data = deepcopy(data)
    reordered_data["twse_rows"] = list(reversed(reordered_data["twse_rows"]))
    reordered_data["tpex_rows"] = list(reversed(reordered_data["tpex_rows"]))
    for row in reordered_data["twse_rows"] + reordered_data["tpex_rows"]:
        row["fetched_at"] = "2099-01-01T00:00:00+08:00"
    reordered_result = calculate_market_context(
        reordered_data,
        params=params,
        sector_map=sector_map,
    )
    assert reordered_result["market_score"] == result1["market_score"]
    assert reordered_result["market_state"] == result1["market_state"]
    assert reordered_result["breadth"] == result1["breadth"]
    assert reordered_result["sector_context"] == result1["sector_context"]
    assert reordered_result["trading_mode"] == result1["trading_mode"]
    assert reordered_result["allow_long"] == result1["allow_long"]
    assert reordered_result["data_fingerprint"] == result1["data_fingerprint"]
    checks += 1

    for field in OUTPUT_FIELDS:
        assert field in result1, field
    checks += 1

    assert result1["score_coverage"] == 100.0
    assert result1["score_complete"] is True
    checks += 1

    assert result1["market_state"] == "BULL"
    assert result1["trading_mode"] == "TREND"
    assert result1["allow_long"] is True
    assert result1["long_permission"] == "ALLOW_LONG"
    checks += 1

    assert result1["taiex"]["short_direction"] == "UP"
    assert result1["tpex"]["short_direction"] == "UP"
    assert result1["taiex"]["ma_period"] == MA_PERIOD
    checks += 1

    assert result1["breadth"]["advancers"] == 7
    assert result1["breadth"]["decliners"] == 3
    assert result1["breadth"]["net_ratio"] == 0.4
    assert result1["security_filter"]["input_rows"] == 12
    assert result1["security_filter"]["eligible_rows"] == 10
    assert result1["security_filter"]["excluded_rows"] == 2
    checks += 1

    assert result1["strong_sectors"]
    assert result1["sector_context"]["ranking"][0]["rank"] == 1
    checks += 1

    disabled_result = get_market_context(
        mode="AUTO",
        disabled_sectors=["半導體"],
        market_data=data,
        sector_map=sector_map,
    )
    assert "半導體" not in disabled_result["strong_sectors"]
    assert "半導體" in disabled_result["sector_context"]["disabled_sectors"]
    checks += 1

    manual_result = get_market_context(
        mode="MANUAL",
        manual_market_state="NORMAL",
        manual_trading_mode="SELECTIVE_LONG",
        market_data=data,
        sector_map=sector_map,
    )
    assert manual_result["market_state"] == "NORMAL"
    assert manual_result["trading_mode"] == "SELECTIVE_LONG"
    assert manual_result["long_permission"] == "SELECTIVE_LONG"
    checks += 1

    weak_state, weak_panic_reason = _auto_market_state(
        -36.2,
        {"change_pct": -1.0422},
        {"change_pct": -0.9731},
        {"ratio": 0.0432},
        params,
    )
    assert weak_state == "WEAK"
    assert weak_panic_reason is None
    assert _default_trading_mode(weak_state) == "DEFENSIVE"
    assert _long_permission(weak_state, "DEFENSIVE") == (True, "SELECTIVE_LONG")
    checks += 1

    panic_data = dict(data)
    panic_data["taiex"] = dict(data["taiex"], price=9700.0)
    panic_result = get_market_context(
        mode="AUTO",
        market_data=panic_data,
        sector_map=sector_map,
    )
    assert panic_result["market_state"] == "PANIC"
    assert panic_result["trading_mode"] == "STOP"
    assert panic_result["allow_long"] is False
    assert panic_result["long_permission"] == "BLOCK_LONG"
    checks += 1

    class _MockGateway:
        def get_index_history(
            self,
            symbol: str,
            market: str,
            *,
            limit: int = 5,
        ) -> List[Dict[str, Any]]:
            rows = [
                {
                    "symbol": symbol,
                    "market": market,
                    "trade_date": "2026-08-27",
                    "price": 9700.0,
                },
                {
                    "symbol": symbol,
                    "market": market,
                    "trade_date": "2026-08-28",
                    "price": 9800.0,
                },
                {
                    "symbol": symbol,
                    "market": market,
                    "trade_date": "2026-08-29",
                    "price": 9900.0,
                },
                {
                    "symbol": symbol,
                    "market": market,
                    "trade_date": "2026-08-30",
                    "price": 10000.0,
                },
            ]
            return rows[-max(1, int(limit)) :]

    history_rows, history_source = _load_gateway_index_history(
        _MockGateway(),
        symbol="t00",
        market="TWSE",
        current_quote=data["taiex"],
        period=5,
    )
    assert len(history_rows) == 5
    assert history_source == "APIGateway.get_index_history"
    assert _history_ma(history_rows, 5) is not None
    checks += 1

    no_sector_result = get_market_context(mode="AUTO", market_data=data)
    assert no_sector_result["score_coverage"] == 80.0
    assert no_sector_result["strong_sectors"] == []
    checks += 1

    partial_sector_map = {
        key: value
        for key, value in sector_map.items()
        if key in {"2330", "2317", "2454", "2881", "6488"}
    }
    partial_sector_result = get_market_context(
        mode="AUTO",
        market_data=data,
        sector_map=partial_sector_map,
    )
    assert partial_sector_result["sector_context"]["mapping_coverage"] == 0.5
    assert partial_sector_result["score_coverage"] == 90.0
    assert partial_sector_result["score_complete"] is False
    checks += 1

    class _MockSectorGateway:
        def get_sector_map(self) -> Dict[str, str]:
            return {"TWSE:2330": "半導體業", "TPEx:6488": "半導體業"}

    resolved_map, resolved_source, resolved_warnings = resolve_sector_map(
        explicit_map=None,
        market_data={},
        gateway=_MockSectorGateway(),
    )
    assert resolved_map["TWSE:2330"] == "半導體業"
    assert resolved_source == "APIGateway.get_sector_map"
    assert resolved_warnings == []
    checks += 1

    parameter_keys = set(result1["parameters"])
    for required in {
        "MA_PERIOD",
        "MARKET_SCORE_WEIGHT",
        "BREADTH_WEIGHT",
        "SECTOR_WEIGHT",
        "BULL_THRESHOLD",
        "BEAR_THRESHOLD",
        "DISABLED_SECTORS",
        "MODE",
        "MANUAL_MARKET_STATE",
    }:
        assert required in parameter_keys
    checks += 1

    return {
        "ok": True,
        "checks": checks,
        "output_fields": OUTPUT_FIELDS,
        "reproducibility": "相同 Market_Data + 相同 Market_Parameters = 相同 Market_Result",
        "sample": {
            "market_state": result1["market_state"],
            "market_score": result1["market_score"],
            "score_coverage": result1["score_coverage"],
            "trading_mode": result1["trading_mode"],
            "allow_long": result1["allow_long"],
            "long_permission": result1["long_permission"],
            "market_momentum": result1["market_momentum"],
            "breadth_ratio": result1["breadth_ratio"],
            "strong_sectors": result1["strong_sectors"],
        },
    }


# =============================================================================
# 診斷
# =============================================================================


def diagnose_links() -> Dict[str, Any]:
    required_methods = (
        "get_index_quote",
        "get_daily_snapshot",
        "get_index_history",
    )
    enhancement_methods = (
        "get_sector_map",
        "get_security_master",
    )
    result: Dict[str, Any] = {
        "required_gateway_methods": list(required_methods),
        "enhancement_gateway_methods": list(enhancement_methods),
        "input_contract": "APIGATEWAY_CANONICAL_INDEX_AND_STOCK_SNAPSHOT",
        "market_network_boundary": (
            "NO_WEBSOCKET_NO_TOKEN_NO_SUBSCRIPTION_NO_RECONNECT_IN_MARKET"
        ),
        "index_labels": ["TAIEX", "TPEx"],
        "index_label_note": (
            "TAIEX／TPEx 為市場指標名稱，不代表 Market 直接呼叫交易所 API"
        ),
        "get_market_context_signature": str(inspect.signature(get_market_context)),
        "fetch_market_data_signature": str(inspect.signature(fetch_market_data)),
        "output_fields": OUTPUT_FIELDS,
    }
    try:
        module = load_api_gateway_module()
        gateway_class = getattr(module, "APIGateway")
        missing = [
            method
            for method in required_methods
            if not callable(getattr(gateway_class, method, None))
        ]
        enhancement_status = {
            name: callable(getattr(gateway_class, name, None))
            for name in enhancement_methods
        }
        status_method = getattr(gateway_class, "status", None)
        result["api_gateway"] = {
            "ok": not missing,
            "APIGateway_signature": str(inspect.signature(gateway_class)),
            "missing_methods": missing,
            "required_methods": {
                name: callable(getattr(gateway_class, name, None))
                for name in required_methods
            },
            "enhancement_methods": enhancement_status,
            "status_ready": callable(status_method),
            "replay_store_direct_read": False,
            "twse_tpex_direct_api": False,
            "market_provider_expected": "FUGLE",
        }
    except Exception as exc:
        result["api_gateway"] = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    result["ok"] = bool(result["api_gateway"].get("ok"))
    return result


# =============================================================================
# CLI
# =============================================================================


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="市場環境、類股強弱與極短線多方許可判定模組"
    )
    parser.add_argument("--self-test", action="store_true", help="執行完全離線自我測試")
    parser.add_argument(
        "--diagnose", action="store_true", help="檢查 api_gateway 連結，不抓市場資料"
    )
    parser.add_argument(
        "--probe", action="store_true", help="透過 APIGateway 實際取得市場資料並計算"
    )
    parser.add_argument(
        "--api-mode",
        default="LIVE",
        choices=["LIVE", "OFFLINE"],
        help="APIGateway 模式",
    )
    parser.add_argument(
        "--replay-dir", default="api_replay", help="APIGateway replay 目錄"
    )
    parser.add_argument("--sector-map", default=None, help="類股分類 JSON/CSV 檔案")
    parser.add_argument("--json", action="store_true", help="以 JSON 顯示結果")
    return parser


def _print_human(result: Mapping[str, Any]) -> None:
    print("=" * 104)
    print("市場環境、類股強弱與極短線多方許可判定模組")
    print("=" * 104)
    for key, value in result.items():
        print(f"{key}: {value}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.self_test:
        result = run_self_test()
    elif args.diagnose:
        result = diagnose_links()
    elif args.probe:
        try:
            result = get_market_context(
                mode="AUTO",
                api_mode=args.api_mode,
                replay_dir=args.replay_dir,
                sector_map_path=args.sector_map,
            )
        except Exception as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    else:
        result = {
            "ok": True,
            "module": "market.py",
            "message": (
                "正式使用請由 runtime / intraday / main.py 呼叫 get_market_context()；"
                "測試請使用 --self-test、--diagnose 或 --probe。"
            ),
            "output_fields": OUTPUT_FIELDS,
        }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        _print_human(result)
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
