# -*- coding: utf-8 -*-
"""
觀察選股與候選股評分模組。

本模組只處理呼叫端已提供的 Snapshot，不建立任何市場資料連線，也不自行取得行情。
正式輸入由 Runtime / APIGateway 在 Scanner 外部準備：
1. Active Universe Snapshot：決定本輪要評估的股票集合與靜態/排行資訊。
2. MarketState Snapshot：提供各股票最新 Fugle Trade Event 已形成的行情狀態。
3. Market Context：提供大盤、類股與 Market Guard 上下文。
4. Day Trade Map：提供現股當沖資格。

Scanner 不因單一 WebSocket Event 自行重建 Active Universe；是否重新執行 Scanner，完全由
Runtime 的較慢週期決定。Scanner 亦不使用執行完成時間補行情來源時間。

可重現核心：
Candidate_List = f(Active_Universe_Snapshot, MarketState_Snapshot,
                   Market_Context, Scanner_Parameters, Day_Trade_Map)

相同輸入 Snapshot 與相同參數，必須得到相同候選集合、Score、排序、trade_ready、
source time、stale 判斷與 fingerprint。Fugle event_time 是 realtime_source_at 的第一來源；
received_at / fetched_at 只能作接收或診斷資訊，不能冒充行情來源時間。
"""

# 中文重點更新：掃描輸入序列化為不可變快照，計算期間的新行情留待下一輪，保留輸入身分與來源時間。

# 中文重點更新：清理歷史相容參數、孤立註解及無效追單顯示，保留測試、診斷、CTS 撤單與委託及持倉對帳。

# 中文重點更新：清除未使用函式、相容介面與無效參數；保留市價買賣、成交對帳、撤買單、清倉及離線驗證。

# 中文重點更新：評分切成可序列化純計算批次，整合後統一排序與指紋；基本總分與分數涵蓋率門檻同步提高。

# 中文重點更新：雙策略候選分數與資格分流，量價權重獨立，取消舊漲幅硬淘汰。

# 中文重點更新：掃描回合時限須大於零，避免無效設定進入 Runtime。

# 中文重點更新：補齊審查要求之狀態識別、整張委託、資料時序與恢復安全邊界。

# 中文重點更新：評分覆蓋率改以本輪 Snapshot 實際可供應的評分欄位為母體，避免 Runtime 未提供類股對照與量比時，候選股被不可達的固定覆蓋率門檻全數清空；另補齊評分閘門診斷。

# 中文重點更新：接收凍結類股對照資料，補齊策略資格原因並統一 RS 判定與輸出精度。

# 中文重點更新：候選固定攜帶類股代碼、中文名稱與分類狀態；分類、快照、過期及相對強度拒絕碼分離。

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# =============================================================================
# 固定參數
# =============================================================================

MIN_PRICE = 10.0
MAX_PRICE = 1000.0
MIN_VOLUME = 500_000
MIN_TURNOVER = 500_000.0
MIN_CHANGE = 1.0
MIN_VOLUME_RATIO = 1.0
MIN_SCORE = 70.0
MIN_SCORE_COVERAGE = 75.0

ACTIVE_UNIVERSE_N = 500
TOP_N = 30

REQUIRE_MARKET_ALLOW_LONG = True
REQUIRE_DAY_TRADE_ELIGIBLE = False
REQUIRE_VOLUME_RATIO = False
REQUIRE_REALTIME_PRICE = True

NEAR_HIGH_MAX_DISTANCE_PCT = 1.0
NEAR_HIGH_SCORE_ZERO_DISTANCE_PCT = 3.0
CHANGE_SCORE_FULL_PCT = 5.0
MARKET_RS_FULL_PCT = 3.0
SECTOR_RS_FULL_PCT = 2.0
VOLUME_RATIO_FULL = 3.0
VOLUME_SCORE_FULL_MULTIPLIER = 5.0
TURNOVER_SCORE_FULL_MULTIPLIER = 10.0
SPREAD_GOOD_PCT = 0.20
SPREAD_BAD_PCT = 1.00

# 單輪運算期限與行情事件新鮮度；不觸發外部請求。
SCANNER_ROUND_TIME_LIMIT_SECONDS = 30.0
REALTIME_FRESHNESS_MAX_AGE_SECONDS = 60.0

SECTOR_CHANGE_SOURCE = "STOCK_DATA"

SORT_RULE: Tuple[Tuple[str, str], ...] = (
    ("score", "DESC"),
    ("momentum", "DESC"),
    ("turnover", "DESC"),
    ("symbol", "ASC"),
    ("market", "ASC"),
)
PRE_SORT_RULE: Tuple[Tuple[str, str], ...] = (
    ("preliminary_score", "DESC"),
    ("momentum", "DESC"),
    ("turnover", "DESC"),
    ("symbol", "ASC"),
    ("market", "ASC"),
)

SORT_NUMERIC_FIELDS = {
    "score",
    "preliminary_score",
    "momentum",
    "turnover",
    "change_pct",
    "volume",
    "volume_ratio",
    "market_relative_strength",
    "sector_relative_strength",
}
SORT_TEXT_FIELDS = {"symbol", "market", "name", "sector"}

SCORE_WEIGHT: Dict[str, float] = {
    "change": 0.16,
    "volume": 0.10,
    "turnover": 0.15,
    "volume_ratio": 0.16,
    "market_relative_strength": 0.18,
    "sector_relative_strength": 0.14,
    "near_intraday_high": 0.05,
    "liquidity": 0.06,
}
PRE_SCORE_WEIGHT: Dict[str, float] = {
    "change": 0.25,
    "volume": 0.15,
    "turnover": 0.20,
    "market_relative_strength": 0.20,
    "sector_relative_strength": 0.20,
}
MOMENTUM_WEIGHT: Dict[str, float] = {
    "change": 0.20,
    "market_relative_strength": 0.25,
    "volume_ratio": 0.25,
    "turnover": 0.20,
    "near_intraday_high": 0.10,
}
TREND_WEIGHT: Dict[str, float] = {
    "market_relative_strength": 0.30,
    "sector_relative_strength": 0.25,
    "volume_ratio": 0.15,
    "liquidity": 0.20,
    "near_intraday_high": 0.10,
}


COMMON_STOCK_SYMBOL_PATTERN = r"^[1-9]\d{3}$"
TAIPEI_TZ = timezone(timedelta(hours=8))

OUTPUT_FIELDS = [
    "symbol",
    "name",
    "market",
    "sector",
    "score",
    "score_coverage",
    "score_coverage_raw",
    "momentum",
    "change_pct",
    "volume",
    "turnover",
    "volume_ratio",
    "market_relative_strength",
    "sector_relative_strength",
    "distance_to_intraday_high_pct",
    "near_intraday_high",
    "liquidity_score",
    "day_trade_eligible",
    "day_trade_status",
    "event_time",
    "serial",
    "quote_sequence",
    "realtime_price_available",
    "realtime_source_at",
    "realtime_observed_at",
    "realtime_age_seconds",
    "realtime_fresh",
    "realtime_freshness_status",
    "quote_source_at",
    "quote_source_status",
    "quote_source_degraded",
    "trade_ready",
    "trade_block_reasons",
    "rank",
]


# =============================================================================
# 參數模型
# =============================================================================


@dataclass(frozen=True)
class ScannerParameters:
    momentum_candidate_min_score: float = 70.0
    trend_candidate_min_score: float = 70.0

    min_price: float = MIN_PRICE
    max_price: float = MAX_PRICE
    min_volume: int = MIN_VOLUME
    min_turnover: float = MIN_TURNOVER
    min_change: float = MIN_CHANGE
    min_volume_ratio: float = MIN_VOLUME_RATIO
    min_score: float = MIN_SCORE
    min_score_coverage: float = MIN_SCORE_COVERAGE

    active_universe_n: int = ACTIVE_UNIVERSE_N
    top_n: int = TOP_N

    require_market_allow_long: bool = REQUIRE_MARKET_ALLOW_LONG
    require_day_trade_eligible: bool = REQUIRE_DAY_TRADE_ELIGIBLE
    require_volume_ratio: bool = REQUIRE_VOLUME_RATIO
    require_realtime_price: bool = REQUIRE_REALTIME_PRICE

    near_high_max_distance_pct: float = NEAR_HIGH_MAX_DISTANCE_PCT
    near_high_score_zero_distance_pct: float = NEAR_HIGH_SCORE_ZERO_DISTANCE_PCT
    change_score_full_pct: float = CHANGE_SCORE_FULL_PCT
    market_rs_full_pct: float = MARKET_RS_FULL_PCT
    sector_rs_full_pct: float = SECTOR_RS_FULL_PCT
    volume_ratio_full: float = VOLUME_RATIO_FULL
    volume_score_full_multiplier: float = VOLUME_SCORE_FULL_MULTIPLIER
    turnover_score_full_multiplier: float = TURNOVER_SCORE_FULL_MULTIPLIER
    spread_good_pct: float = SPREAD_GOOD_PCT
    spread_bad_pct: float = SPREAD_BAD_PCT

    scanner_round_time_limit_seconds: float = SCANNER_ROUND_TIME_LIMIT_SECONDS
    realtime_freshness_max_age_seconds: float = REALTIME_FRESHNESS_MAX_AGE_SECONDS
    sector_change_source: str = SECTOR_CHANGE_SOURCE

    sort_rule: Tuple[Tuple[str, str], ...] = SORT_RULE
    pre_sort_rule: Tuple[Tuple[str, str], ...] = PRE_SORT_RULE

    score_weight: Mapping[str, float] = field(
        default_factory=lambda: dict(SCORE_WEIGHT)
    )
    pre_score_weight: Mapping[str, float] = field(
        default_factory=lambda: dict(PRE_SCORE_WEIGHT)
    )
    trend_weight: Mapping[str, float] = field(
        default_factory=lambda: dict(TREND_WEIGHT)
    )
    momentum_weight: Mapping[str, float] = field(
        default_factory=lambda: dict(MOMENTUM_WEIGHT)
    )

    def validate(self) -> None:
        if not math.isfinite(float(self.momentum_candidate_min_score)):
            raise ValueError("momentum_candidate_min_score 必須為有限數值")
        if self.momentum_candidate_min_score <= 0:
            raise ValueError("momentum_candidate_min_score 必須大於零")
        if not math.isfinite(float(self.trend_candidate_min_score)):
            raise ValueError("trend_candidate_min_score 必須為有限數值")
        if self.trend_candidate_min_score <= 0:
            raise ValueError("trend_candidate_min_score 必須大於零")
        if self.min_price <= 0:
            raise ValueError("MIN_PRICE 必須 > 0")
        if self.max_price <= self.min_price:
            raise ValueError("MAX_PRICE 必須 > MIN_PRICE")
        if self.min_volume < 0:
            raise ValueError("MIN_VOLUME 不得 < 0")
        if self.min_turnover < 0:
            raise ValueError("MIN_TURNOVER 不得 < 0")
        if self.min_volume_ratio <= 0:
            raise ValueError("MIN_VOLUME_RATIO 必須 > 0")
        if not 0 <= self.min_score <= 100:
            raise ValueError("MIN_SCORE 必須介於 0~100")
        if not 0 <= self.min_score_coverage <= 100:
            raise ValueError("MIN_SCORE_COVERAGE 必須介於 0~100")
        if self.active_universe_n <= 0:
            raise ValueError("ACTIVE_UNIVERSE_N 必須 > 0")
        if self.scanner_round_time_limit_seconds <= 0:
            raise ValueError("SCANNER_ROUND_TIME_LIMIT_SECONDS 必須 > 0")
        if self.top_n <= 0:
            raise ValueError("TOP_N 必須 > 0")
        if self.near_high_max_distance_pct < 0:
            raise ValueError("NEAR_HIGH_MAX_DISTANCE_PCT 不得 < 0")
        if self.near_high_score_zero_distance_pct <= self.near_high_max_distance_pct:
            raise ValueError(
                "NEAR_HIGH_SCORE_ZERO_DISTANCE_PCT 必須大於 near-high 布林門檻"
            )
        if self.change_score_full_pct <= self.min_change:
            raise ValueError("CHANGE_SCORE_FULL_PCT 必須 > MIN_CHANGE")
        if self.market_rs_full_pct <= 0 or self.sector_rs_full_pct <= 0:
            raise ValueError("Relative Strength 滿分尺度必須 > 0")
        if self.volume_ratio_full <= self.min_volume_ratio:
            raise ValueError("VOLUME_RATIO_FULL 必須 > MIN_VOLUME_RATIO")
        if (
            self.volume_score_full_multiplier <= 1
            or self.turnover_score_full_multiplier <= 1
        ):
            raise ValueError("量能滿分倍數必須 > 1")
        if self.spread_bad_pct <= self.spread_good_pct:
            raise ValueError("SPREAD_BAD_PCT 必須 > SPREAD_GOOD_PCT")
        if self.realtime_freshness_max_age_seconds <= 0:
            raise ValueError("REALTIME_FRESHNESS_MAX_AGE_SECONDS 必須 > 0")
        if _normalize_sector_change_source(self.sector_change_source) not in {
            "STOCK_DATA",
            "MARKET_CONTEXT",
        }:
            raise ValueError("SECTOR_CHANGE_SOURCE 僅允許 STOCK_DATA / MARKET_CONTEXT")
        _validate_sort_rule(self.sort_rule, "SORT_RULE")
        _validate_sort_rule(self.pre_sort_rule, "PRE_SORT_RULE")
        _validate_weights(self.trend_weight, TREND_WEIGHT.keys(), "TREND_WEIGHT")
        _validate_weights(self.score_weight, SCORE_WEIGHT.keys(), "SCORE_WEIGHT")
        _validate_weights(
            self.pre_score_weight, PRE_SCORE_WEIGHT.keys(), "PRE_SCORE_WEIGHT"
        )
        _validate_weights(
            self.momentum_weight, MOMENTUM_WEIGHT.keys(), "MOMENTUM_WEIGHT"
        )

    def snapshot(self) -> Dict[str, Any]:
        return {
            "MOMENTUM_CANDIDATE_MIN_SCORE": self.momentum_candidate_min_score,
            "TREND_CANDIDATE_MIN_SCORE": self.trend_candidate_min_score,
            "MIN_PRICE": float(self.min_price),
            "MAX_PRICE": float(self.max_price),
            "MIN_VOLUME": int(self.min_volume),
            "MIN_TURNOVER": float(self.min_turnover),
            "MIN_CHANGE": float(self.min_change),
            "MIN_VOLUME_RATIO": float(self.min_volume_ratio),
            "MIN_SCORE": float(self.min_score),
            "MIN_SCORE_COVERAGE": float(self.min_score_coverage),
            "ACTIVE_UNIVERSE_N": int(self.active_universe_n),
            "TOP_N": int(self.top_n),
            "REQUIRE_MARKET_ALLOW_LONG": bool(self.require_market_allow_long),
            "REQUIRE_DAY_TRADE_ELIGIBLE": bool(self.require_day_trade_eligible),
            "REQUIRE_VOLUME_RATIO": bool(self.require_volume_ratio),
            "REQUIRE_REALTIME_PRICE": bool(self.require_realtime_price),
            "NEAR_HIGH_MAX_DISTANCE_PCT": float(self.near_high_max_distance_pct),
            "NEAR_HIGH_SCORE_ZERO_DISTANCE_PCT": float(
                self.near_high_score_zero_distance_pct
            ),
            "CHANGE_SCORE_FULL_PCT": float(self.change_score_full_pct),
            "MARKET_RS_FULL_PCT": float(self.market_rs_full_pct),
            "SECTOR_RS_FULL_PCT": float(self.sector_rs_full_pct),
            "VOLUME_RATIO_FULL": float(self.volume_ratio_full),
            "VOLUME_SCORE_FULL_MULTIPLIER": float(self.volume_score_full_multiplier),
            "TURNOVER_SCORE_FULL_MULTIPLIER": float(
                self.turnover_score_full_multiplier
            ),
            "SPREAD_GOOD_PCT": float(self.spread_good_pct),
            "SPREAD_BAD_PCT": float(self.spread_bad_pct),
            "SCANNER_ROUND_TIME_LIMIT_SECONDS": float(
                self.scanner_round_time_limit_seconds
            ),
            "REALTIME_FRESHNESS_MAX_AGE_SECONDS": float(
                self.realtime_freshness_max_age_seconds
            ),
            "SECTOR_CHANGE_SOURCE": _normalize_sector_change_source(
                self.sector_change_source
            ),
            "SORT_RULE": [list(rule) for rule in self.sort_rule],
            "PRE_SORT_RULE": [list(rule) for rule in self.pre_sort_rule],
            "SCORE_WEIGHT": dict(
                sorted((str(k), float(v)) for k, v in self.score_weight.items())
            ),
            "PRE_SCORE_WEIGHT": dict(
                sorted((str(k), float(v)) for k, v in self.pre_score_weight.items())
            ),
            "TREND_WEIGHT": dict(self.trend_weight),
            "MOMENTUM_WEIGHT": dict(
                sorted((str(k), float(v)) for k, v in self.momentum_weight.items())
            ),
            "SNAPSHOT_ONLY": True,
        }


# =============================================================================
# 共用工具
# =============================================================================


def _validate_weights(
    weights: Mapping[str, float], required_keys: Iterable[str], name: str
) -> None:
    required = set(required_keys)
    actual = set(weights)
    if actual != required:
        raise ValueError(
            f"{name} 欄位不完整：missing={sorted(required-actual)}, extra={sorted(actual-required)}"
        )
    values = [float(weights[key]) for key in sorted(required)]
    if any(value < 0 for value in values):
        raise ValueError(f"{name} 權重不得小於 0")
    if not math.isclose(sum(values), 1.0, abs_tol=1e-9):
        raise ValueError(f"{name} 權重總和必須等於 1")


def _validate_sort_rule(rules: Sequence[Sequence[str]], name: str) -> None:
    if not rules:
        raise ValueError(f"{name} 不得為空")
    allowed_fields = SORT_NUMERIC_FIELDS | SORT_TEXT_FIELDS
    seen: set[str] = set()
    for rule in rules:
        if (
            not isinstance(rule, Sequence)
            or isinstance(rule, (str, bytes))
            or len(rule) != 2
        ):
            raise ValueError(f"{name} 每個規則必須是 (field, direction)")
        field_name = str(rule[0]).strip()
        direction = str(rule[1]).strip().upper()
        if field_name not in allowed_fields:
            raise ValueError(f"{name} 不支援排序欄位：{field_name}")
        if direction not in {"ASC", "DESC"}:
            raise ValueError(f"{name} 排序方向僅允許 ASC / DESC")
        if field_name in seen:
            raise ValueError(f"{name} 排序欄位重複：{field_name}")
        seen.add(field_name)


def _normalize_session_phase(value: Optional[str]) -> str:
    text = str(value or "REGULAR").strip().upper()
    aliases = {
        "PRE": "PREOPEN",
        "PRE_OPEN": "PREOPEN",
        "PRE-MARKET": "PREOPEN",
        "OPENING": "OPEN",
        "INTRADAY": "REGULAR",
        "TRADING": "REGULAR",
        "POST": "POSTCLOSE",
        "POST_CLOSE": "POSTCLOSE",
        "AFTERHOURS": "POSTCLOSE",
        "CLOSE": "POSTCLOSE",
        "CLOSED": "POSTCLOSE",
    }
    return aliases.get(text, text)


def _session_trade_ready(session_phase: str) -> bool:
    return _normalize_session_phase(session_phase) in {"OPEN", "REGULAR"}


def _normalize_sector_change_source(value: str) -> str:
    text = str(value or "STOCK_DATA").strip().upper()
    aliases = {
        "ROWS": "STOCK_DATA",
        "STOCK": "STOCK_DATA",
        "MARKET": "MARKET_CONTEXT",
        "CONTEXT": "MARKET_CONTEXT",
    }
    return aliases.get(text, text)


def _normalize_market(value: Any) -> str:
    text = str(value or "").strip()
    upper = text.upper()
    if upper in {"TWSE", "TSE", "LISTED"}:
        return "TWSE"
    if upper in {"TPEX", "OTC"}:
        return "TPEx"
    return text


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    try:
        number = float(str(value).replace(",", "").replace("+", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _to_int(value: Any, default: int = 0) -> int:
    number = _to_float(value)
    return default if number is None else int(number)


def _round(value: Optional[float], digits: int = 4) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def _clamp(value: float, lower: float = 0.0, upper: float = 100.0) -> float:
    return max(lower, min(upper, float(value)))


def _pct_change(current: Optional[float], base: Optional[float]) -> Optional[float]:
    if current is None or base is None or base <= 0:
        return None
    return (current - base) / base * 100.0


def _stable_hash(payload: Any) -> str:
    text = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _row_key(row: Mapping[str, Any]) -> Tuple[str, str]:
    return _normalize_market(row.get("market")), str(row.get("symbol") or "").strip()


def _market_symbol_key(market: str, symbol: str) -> str:
    return f"{_normalize_market(market)}:{str(symbol).strip()}"


def _effective_price(row: Mapping[str, Any]) -> Optional[float]:
    value = _to_float(row.get("price"))
    return value if value is not None and value > 0 else None


def _canonical_time_text(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TAIPEI_TZ)
        return dt.astimezone(TAIPEI_TZ).isoformat(timespec="microseconds")
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = text.replace(" ", "T", 1)
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TAIPEI_TZ)
    return dt.astimezone(TAIPEI_TZ).isoformat(timespec="microseconds")


def _event_time_to_source_at(value: Any) -> str:
    number = _to_float(value)
    if number is None or number <= 0:
        return ""
    try:
        if number > 10_000_000_000_000:
            seconds = number / 1_000_000.0
        elif number > 10_000_000_000:
            seconds = number / 1000.0
        else:
            seconds = number
        return datetime.fromtimestamp(seconds, tz=TAIPEI_TZ).isoformat(
            timespec="microseconds"
        )
    except (ValueError, OSError, OverflowError):
        return ""


def _parse_datetime(value: Any) -> Optional[datetime]:
    text = _canonical_time_text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _age_seconds(source_at: Any, reference_at: Any) -> Optional[float]:
    source = _parse_datetime(source_at)
    reference = _parse_datetime(reference_at)
    if source is None or reference is None:
        return None
    value = (reference - source).total_seconds()
    if value < -1.0:
        return None
    return round(max(0.0, value), 6)


def _source_at(row: Mapping[str, Any]) -> str:
    # event_time 是唯一第一優先；received_at / fetched_at 永遠不會出現在此函式。
    event_source = _event_time_to_source_at(row.get("event_time"))
    if event_source:
        return event_source
    for field_name in ("realtime_source_at", "source_at", "timestamp"):
        resolved = _canonical_time_text(row.get(field_name))
        if resolved:
            return resolved
    raw_timestamp = row.get("raw_timestamp")
    return _event_time_to_source_at(raw_timestamp)


def _observed_at(row: Mapping[str, Any]) -> str:
    for field_name in ("freshness_reference_at", "snapshot_at", "as_of", "received_at"):
        resolved = _canonical_time_text(row.get(field_name))
        if resolved:
            return resolved
    return ""


def _source_at_sort_key(value: Any) -> Tuple[int, int, int, int, int, int, int, str]:
    text = str(value or "").strip()
    match = re.match(
        r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?", text
    )
    if not match:
        return (0, 0, 0, 0, 0, 0, 0, text)
    year, month, day, hour, minute, second, fraction = match.groups()
    microsecond = int(((fraction or "") + "000000")[:6])
    return (
        int(year),
        int(month),
        int(day),
        int(hour),
        int(minute),
        int(second),
        microsecond,
        text,
    )


def _normalized_sort_rule(
    rules: Sequence[Sequence[str]],
) -> Tuple[Tuple[str, str], ...]:
    return tuple((str(rule[0]).strip(), str(rule[1]).strip().upper()) for rule in rules)


def _sort_value(item: Mapping[str, Any], field_name: str) -> Any:
    if field_name in SORT_NUMERIC_FIELDS:
        value = _to_float(item.get(field_name))
        return float(value) if value is not None else float("-inf")
    return str(item.get(field_name) or "")


def _sort_items(
    items: Sequence[Mapping[str, Any]], rules: Sequence[Sequence[str]]
) -> List[Dict[str, Any]]:
    output = [dict(item) for item in items]
    for field_name, direction in reversed(_normalized_sort_rule(rules)):
        output.sort(
            key=lambda item, field_name=field_name: _sort_value(item, field_name),
            reverse=direction == "DESC",
        )
    return output


def _extract_snapshot_rows(
    snapshot: Any,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    metadata: Dict[str, Any] = {}
    if snapshot is None:
        return [], metadata
    if isinstance(snapshot, Mapping):
        for field_name in ("snapshot_at", "as_of", "source_at", "sequence"):
            if field_name in snapshot:
                metadata[field_name] = snapshot.get(field_name)
        for key in ("rows", "quotes", "data", "items", "results", "market_state"):
            value = snapshot.get(key)
            if isinstance(value, Sequence) and not isinstance(
                value, (str, bytes, bytearray)
            ):
                return [
                    dict(row) for row in value if isinstance(row, Mapping)
                ], metadata
            if isinstance(value, Mapping):
                rows, nested_meta = _extract_snapshot_rows(value)
                metadata.update(nested_meta)
                if rows:
                    return rows, metadata
        rows: List[Dict[str, Any]] = []
        for key, value in snapshot.items():
            if not isinstance(value, Mapping):
                continue
            row = dict(value)
            if isinstance(key, tuple) and len(key) >= 2:
                row.setdefault("symbol", str(key[0]))
                row.setdefault("market", str(key[1]))
            elif (
                not row.get("symbol")
                and isinstance(key, str)
                and re.fullmatch(r"[A-Za-z0-9._-]+", key)
            ):
                row.setdefault("symbol", key)
            if row.get("symbol"):
                rows.append(row)
        if rows:
            return rows, metadata
        if snapshot.get("symbol"):
            return [dict(snapshot)], metadata
        return [], metadata
    if isinstance(snapshot, Sequence) and not isinstance(
        snapshot, (str, bytes, bytearray)
    ):
        return [dict(row) for row in snapshot if isinstance(row, Mapping)], metadata
    raise TypeError("Snapshot 必須是 Mapping 或 Sequence[Mapping]")


def _normalize_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    item = dict(row)
    item["symbol"] = str(item.get("symbol") or item.get("code") or "").strip()
    item["market"] = _normalize_market(item.get("market") or item.get("exchange"))
    aliases = {
        "closePrice": "price",
        "lastPrice": "price",
        "openPrice": "open",
        "highPrice": "high",
        "lowPrice": "low",
        "previousClose": "prev_close",
        "referencePrice": "prev_close",
        "tradeVolume": "volume",
        "tradeValue": "turnover",
        "changePercent": "change_percent",
        "lastUpdated": "event_time",
    }
    for source, target in aliases.items():
        if item.get(target) in (None, "") and item.get(source) not in (None, ""):
            item[target] = item.get(source)
    total = item.get("total")
    if isinstance(total, Mapping):
        item.setdefault("volume", total.get("tradeVolume"))
        item.setdefault("turnover", total.get("tradeValue"))
    last_trade = item.get("lastTrade")
    if isinstance(last_trade, Mapping):
        for source, target in (
            ("time", "event_time"),
            ("serial", "serial"),
            ("price", "price"),
            ("bid", "bid"),
            ("ask", "ask"),
        ):
            if item.get(target) in (None, "") and last_trade.get(source) not in (
                None,
                "",
            ):
                item[target] = last_trade.get(source)
    return item


def _canonical_row_for_hash(row: Mapping[str, Any]) -> Dict[str, Any]:
    fields = (
        "symbol",
        "name",
        "market",
        "sector",
        "industry",
        "industry_name",
        "source",
        "trade_date",
        "event_time",
        "serial",
        "quote_sequence",
        "symbol_quote_sequence",
        "timestamp",
        "source_at",
        "realtime_source_at",
        "received_at",
        "realtime_age_seconds",
        "realtime_fresh",
        "freshness_basis",
        "price",
        "open",
        "high",
        "low",
        "prev_close",
        "change_percent",
        "volume",
        "turnover",
        "bid",
        "ask",
        "volume_ratio",
        "avg_volume",
        "day_trade_eligible",
        "rank_value",
        "rank_volume",
    )
    return {field: row.get(field) for field in fields}


def _row_freshness_key(row: Mapping[str, Any]) -> Tuple[int, int, str, str]:
    return (
        _to_int(row.get("quote_sequence"), 0),
        _to_int(row.get("event_time"), 0),
        str(row.get("source_at") or row.get("timestamp") or ""),
        _stable_hash(_canonical_row_for_hash(row)),
    )


def _deduplicate_rows(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        row = _normalize_row(raw)
        key = _row_key(row)
        if not all(key):
            continue
        current = grouped.get(key)
        if current is None or _row_freshness_key(row) > _row_freshness_key(current):
            grouped[key] = row
    return [grouped[key] for key in sorted(grouped)]


def _prepare_active_universe(
    snapshot: Any, limit: int
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    rows, metadata = _extract_snapshot_rows(snapshot)
    input_count = len(rows)
    filtered: List[Dict[str, Any]] = []
    invalid_key = invalid_market = non_common = 0
    for raw in rows:
        row = _normalize_row(raw)
        market, symbol = _row_key(row)
        if not market or not symbol:
            invalid_key += 1
            continue
        if market not in {"TWSE", "TPEx"}:
            invalid_market += 1
            continue
        if re.fullmatch(COMMON_STOCK_SYMBOL_PATTERN, symbol) is None:
            non_common += 1
            continue
        filtered.append(row)
    deduplicated = _deduplicate_rows(filtered)

    # Active Universe 的重新排序由外部較慢週期決定；Scanner 不因單一事件重建。
    # 若呼叫端提供超過上限的集合，僅依 Snapshot 已含排行欄位做固定、可重現截取。
    def active_sort_key(row: Mapping[str, Any]) -> Tuple[int, float, int, str, str]:
        rank_value = _to_int(row.get("rank_value"), 10**9) or 10**9
        rank_volume = _to_int(row.get("rank_volume"), 10**9) or 10**9
        best_rank = min(rank_value, rank_volume)
        return (
            best_rank,
            -float(_to_float(row.get("turnover")) or 0.0),
            -_to_int(row.get("volume"), 0),
            str(row.get("symbol") or ""),
            _normalize_market(row.get("market")),
        )

    kept = sorted(deduplicated, key=active_sort_key)[: max(1, int(limit))]
    return kept, {
        **metadata,
        "input_rows": input_count,
        "valid_rows": len(filtered),
        "scanner_universe_rows": len(kept),
        "invalid_key_rows": invalid_key,
        "invalid_market_rows": invalid_market,
        "non_common_rows": non_common,
        "duplicate_rows_removed": max(0, len(filtered) - len(deduplicated)),
        "rule": "EXTERNAL_ACTIVE_UNIVERSE_SNAPSHOT_ONLY",
    }


def _market_state_index(
    snapshot: Any,
) -> Tuple[Dict[Tuple[str, str], Dict[str, Any]], Dict[str, Any]]:
    rows, metadata = _extract_snapshot_rows(snapshot)
    normalized = _deduplicate_rows(rows)
    index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    for row in normalized:
        key = _row_key(row)
        if all(key):
            index[key] = row
            by_symbol.setdefault(key[1], []).append(row)
    # 市場欄位缺失但同 symbol 唯一時，在 merge 階段可回退 symbol 唯一命中。
    metadata["rows"] = len(normalized)
    metadata["_by_symbol"] = by_symbol
    return index, metadata


def _merge_universe_market_state(
    universe_rows: Sequence[Mapping[str, Any]],
    market_state_snapshot: Any,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    state_index, state_meta = _market_state_index(market_state_snapshot)
    by_symbol = state_meta.pop("_by_symbol", {})
    merged_rows: List[Dict[str, Any]] = []
    matched = 0
    missing = 0
    for raw in universe_rows:
        base = _normalize_row(raw)
        key = _row_key(base)
        state = state_index.get(key)
        if state is None:
            symbol_matches = by_symbol.get(key[1]) or []
            if len(symbol_matches) == 1:
                state = symbol_matches[0]
        item = dict(base)
        item["market_state_available"] = state is not None
        if state is not None:
            matched += 1
            state_row = _normalize_row(state)
            dynamic_fields = (
                "security_type",
                "security_type_name",
                "metadata_date",
                "source",
                "trade_date",
                "timestamp",
                "raw_timestamp",
                "event_time",
                "received_at",
                "serial",
                "quote_sequence",
                "symbol_quote_sequence",
                "event_classification",
                "price",
                "open",
                "high",
                "low",
                "prev_close",
                "volume",
                "turnover",
                "bid",
                "ask",
                "five_level",
                "volume_ratio",
                "avg_volume",
                "realtime_age_seconds",
                "realtime_fresh",
                "freshness_threshold_seconds",
                "freshness_basis",
                "realtime_source_at",
                "source_at",
                "trade_ready",
            )
            for field_name in dynamic_fields:
                if field_name in state_row and state_row.get(field_name) not in (
                    None,
                    "",
                ):
                    item[field_name] = state_row.get(field_name)
            if state_row.get("name"):
                item["name"] = state_row.get("name")
            item["market_state_source"] = str(state_row.get("source") or "")
        else:
            missing += 1
            item["market_state_source"] = ""
        merged_rows.append(item)
    return merged_rows, {
        **state_meta,
        "matched_active_universe_rows": matched,
        "missing_active_universe_rows": missing,
        "rule": "ACTIVE_UNIVERSE_LEFT_JOIN_MARKETSTATE_BY_MARKET_SYMBOL",
    }


def _normalize_sector_map(payload: Optional[Mapping[str, str]]) -> Dict[str, str]:
    output: Dict[str, str] = {}
    for key, value in (payload or {}).items():
        text = str(value or "").strip()
        if text:
            output[str(key).strip()] = text
    return output


def _lookup_sector(
    row: Mapping[str, Any], sector_map: Mapping[str, str]
) -> Optional[str]:
    market, symbol = _row_key(row)
    for key in (_market_symbol_key(market, symbol), symbol):
        if str(sector_map.get(key) or "").strip():
            return str(sector_map[key]).strip()
    for field_name in ("sector", "industry", "industry_name"):
        value = str(row.get(field_name) or "").strip()
        if value:
            return value
    return None


def _parse_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(int(value))
    text = str(value).strip().upper()
    if text in {"1", "Y", "YES", "TRUE", "T", "可", "是", "允許", "ELIGIBLE"}:
        return True
    if text in {"0", "N", "NO", "FALSE", "F", "不可", "否", "不允許", "INELIGIBLE"}:
        return False
    return None


def _normalize_day_trade_payload(
    payload: Optional[Mapping[str, Any]],
) -> Dict[str, bool]:
    output: Dict[str, bool] = {}
    for key, value in (payload or {}).items():
        if isinstance(value, Mapping):
            symbol = str(value.get("symbol") or value.get("code") or key).strip()
            market = _normalize_market(value.get("market"))
            eligible = _parse_bool(
                value.get("day_trade_eligible")
                if "day_trade_eligible" in value
                else value.get("eligible")
            )
            if symbol and eligible is not None:
                output[symbol] = eligible
                if market:
                    output[_market_symbol_key(market, symbol)] = eligible
        else:
            eligible = _parse_bool(value)
            if eligible is not None:
                output[str(key).strip()] = eligible
    return output


def _lookup_day_trade_eligibility(
    row: Mapping[str, Any], day_trade_map: Mapping[str, bool]
) -> Tuple[Optional[bool], str]:
    for field_name in ("day_trade_eligible", "day_trade", "intraday_eligible"):
        if field_name in row:
            value = _parse_bool(row.get(field_name))
            if value is not None:
                return value, f"ROW:{field_name}"
    market, symbol = _row_key(row)
    for key in (_market_symbol_key(market, symbol), symbol):
        if key in day_trade_map:
            return bool(day_trade_map[key]), "MAP"
    return None, "UNAVAILABLE"


def _index_change_from_context(
    market_context: Mapping[str, Any], market: str
) -> Tuple[Optional[float], str]:
    if _normalize_market(market) == "TPEx":
        node = market_context.get("tpex")
        benchmark = "TPEX"
    else:
        node = market_context.get("taiex")
        benchmark = "TAIEX"
    if isinstance(node, Mapping):
        return _to_float(node.get("change_pct")), benchmark
    return None, benchmark


def _sector_change_map(market_context: Mapping[str, Any]) -> Dict[str, float]:
    output: Dict[str, float] = {}
    sector_context = market_context.get("sector_context")
    if not isinstance(sector_context, Mapping):
        return output
    ranking = sector_context.get("ranking")
    if not isinstance(ranking, Sequence) or isinstance(
        ranking, (str, bytes, bytearray)
    ):
        return output
    for row in ranking:
        if not isinstance(row, Mapping):
            continue
        sector = str(row.get("sector") or "").strip()
        value = _to_float(row.get("turnover_weighted_change_pct"))
        if value is None:
            value = _to_float(row.get("avg_change_pct"))
        if sector and value is not None:
            output[sector] = value
    return output


def _row_change_pct(row: Mapping[str, Any]) -> Optional[float]:
    price = _effective_price(row)
    prev_close = _to_float(row.get("prev_close"))
    calculated = _pct_change(price, prev_close)
    if calculated is not None:
        return calculated
    for field_name in ("change_pct", "change_percent"):
        value = _to_float(row.get(field_name))
        if value is not None:
            return value
    return None


def _sector_change_map_from_stock_data(
    rows: Sequence[Mapping[str, Any]], sector_map: Mapping[str, str]
) -> Dict[str, float]:
    grouped: Dict[str, Dict[str, float]] = {}
    for row in rows:
        sector = _lookup_sector(row, sector_map)
        change_pct = _row_change_pct(row)
        if not sector or change_pct is None:
            continue
        turnover = max(0.0, _to_float(row.get("turnover")) or 0.0)
        bucket = grouped.setdefault(
            sector,
            {
                "count": 0.0,
                "change_sum": 0.0,
                "turnover": 0.0,
                "turnover_change_sum": 0.0,
            },
        )
        bucket["count"] += 1.0
        bucket["change_sum"] += change_pct
        bucket["turnover"] += turnover
        bucket["turnover_change_sum"] += change_pct * turnover
    output: Dict[str, float] = {}
    for sector in sorted(grouped):
        bucket = grouped[sector]
        if bucket["count"] <= 0:
            continue
        output[sector] = (
            bucket["turnover_change_sum"] / bucket["turnover"]
            if bucket["turnover"] > 0
            else bucket["change_sum"] / bucket["count"]
        )
    return output


def _resolve_sector_changes(
    rows: Sequence[Mapping[str, Any]],
    market_context: Mapping[str, Any],
    sector_map: Mapping[str, str],
    source: str,
) -> Tuple[Dict[str, float], str]:
    if "sector_snapshots" in market_context:
        return {code: row["change_pct"] for code, row in market_context["sector_snapshots"].items()
                if _to_float(row.get("change_pct")) is not None}, "MARKET_SECTOR_SNAPSHOT"
    normalized = _normalize_sector_change_source(source)
    context_changes = _sector_change_map(market_context)
    if normalized == "MARKET_CONTEXT":
        return context_changes, "MARKET_CONTEXT"
    stock_changes = _sector_change_map_from_stock_data(rows, sector_map)
    if stock_changes:
        return stock_changes, "STOCK_DATA"
    return context_changes, "MARKET_CONTEXT_FALLBACK"


def _resolve_volume_ratio(row: Mapping[str, Any]) -> Tuple[Optional[float], str]:
    explicit = _to_float(row.get("volume_ratio"))
    if explicit is not None and explicit > 0:
        return explicit, "SNAPSHOT:volume_ratio"
    avg_volume = _to_float(row.get("avg_volume"))
    volume = _to_float(row.get("volume"))
    if avg_volume is not None and avg_volume > 0 and volume is not None and volume >= 0:
        return volume / avg_volume, "SNAPSHOT:avg_volume"
    return None, "UNAVAILABLE"


def _linear_score(
    value: Optional[float], minimum: float, full: float
) -> Optional[float]:
    if value is None or full <= minimum:
        return None
    return _clamp((float(value) - minimum) / (full - minimum) * 100.0)


def _positive_score(value: Optional[float], full: float) -> Optional[float]:
    if value is None or full <= 0:
        return None
    return _clamp(float(value) / full * 100.0)


def _log_multiplier_score(
    value: Optional[float], minimum: float, full_multiplier: float
) -> Optional[float]:
    if value is None or minimum <= 0 or full_multiplier <= 1:
        return None
    if value <= minimum:
        return 0.0
    return _clamp(math.log(float(value) / minimum) / math.log(full_multiplier) * 100.0)


def _near_high_score(
    distance_pct: Optional[float], zero_distance_pct: float
) -> Optional[float]:
    if distance_pct is None or zero_distance_pct <= 0:
        return None
    return _clamp(
        (zero_distance_pct - max(0.0, distance_pct)) / zero_distance_pct * 100.0
    )


def _spread_score(
    spread_pct: Optional[float], good_pct: float, bad_pct: float
) -> Optional[float]:
    if spread_pct is None:
        return None
    if spread_pct <= good_pct:
        return 100.0
    if spread_pct >= bad_pct:
        return 0.0
    return _clamp((bad_pct - spread_pct) / (bad_pct - good_pct) * 100.0)


def _weighted_available_score(
    components: Mapping[str, Optional[float]], weights: Mapping[str, float]
) -> Tuple[float, float, Dict[str, float]]:
    numerator = denominator = 0.0
    used: Dict[str, float] = {}
    for name in sorted(weights):
        value = components.get(name)
        weight = float(weights[name])
        if value is None or weight <= 0:
            continue
        numerator += float(value) * weight
        denominator += weight
        used[name] = weight
    if denominator <= 0:
        return 0.0, 0.0, used
    return numerator / denominator, denominator * 100.0, used


def _freshness_fields(
    row: Mapping[str, Any], params: ScannerParameters, snapshot_reference_at: Any = None
) -> Dict[str, Any]:
    state_available = bool(row.get("market_state_available"))
    # realtime_source_at 只允許 MarketState 的 Fugle event_time / event source time。
    # Active Universe 的 source_at 不得冒充即時行情來源時間。
    source_at = _source_at(row) if state_available else ""
    observed_at = _observed_at(row) or _canonical_time_text(snapshot_reference_at)
    price_available = bool(state_available and _effective_price(row) is not None)
    provided_age = _to_float(row.get("realtime_age_seconds"))
    provided_fresh = (
        row.get("realtime_fresh")
        if isinstance(row.get("realtime_fresh"), bool)
        else None
    )

    if not state_available:
        status = "MARKET_STATE_MISSING"
        age_seconds = None
    elif not source_at:
        status = "SOURCE_TIME_UNKNOWN"
        age_seconds = None
    elif provided_age is not None and provided_age >= 0:
        age_seconds = round(provided_age, 6)
        status = (
            "FRESH"
            if age_seconds <= params.realtime_freshness_max_age_seconds
            else "SOURCE_STALE"
        )
    else:
        age_seconds = _age_seconds(source_at, observed_at)
        if age_seconds is not None:
            status = (
                "FRESH"
                if age_seconds <= params.realtime_freshness_max_age_seconds
                else "SOURCE_STALE"
            )
        elif provided_fresh is not None:
            status = "FRESH" if provided_fresh else "SOURCE_STALE"
        else:
            status = "REFERENCE_TIME_UNKNOWN"

    fresh = status == "FRESH"
    return {
        "realtime_price_available": price_available,
        "realtime_source_at": source_at or None,
        "realtime_observed_at": observed_at or None,
        "realtime_age_seconds": age_seconds,
        "realtime_fresh": fresh,
        "realtime_freshness_status": status,
        "quote_source_at": source_at or None,
        "quote_source_status": (
            "MARKET_STATE" if state_available and source_at else status
        ),
        "quote_source_degraded": not bool(state_available and source_at),
    }


# =============================================================================
# 指標與評分
# =============================================================================


def _metric_components(
    row: Mapping[str, Any],
    *,
    market_context: Mapping[str, Any],
    sector_map: Mapping[str, str],
    sector_changes: Mapping[str, float],
    sector_change_source: str,
    day_trade_map: Mapping[str, bool],
    params: ScannerParameters,
    snapshot_reference_at: Any = None,
) -> Dict[str, Any]:
    market, symbol = _row_key(row)
    name = str(row.get("name") or "").strip()
    price = _effective_price(row)
    open_price = _to_float(row.get("open"))
    high = _to_float(row.get("high"))
    low = _to_float(row.get("low"))
    prev_close = _to_float(row.get("prev_close"))
    raw_volume = max(0, _to_int(row.get("volume"), 0))
    # Scanner MIN_VOLUME 以股數定義；保留 Gateway 原始成交量及單位。
    volume = raw_volume * 1000 if row.get("volume_unit") == "LOT" else raw_volume
    turnover = max(0.0, _to_float(row.get("turnover")) or 0.0)
    bid = _to_float(row.get("bid"))
    ask = _to_float(row.get("ask"))

    change_pct = _row_change_pct(row)
    open_change_pct = _pct_change(price, open_price)
    benchmark_change, benchmark_name = _index_change_from_context(
        market_context, market
    )
    market_rs = (
        change_pct - benchmark_change
        if change_pct is not None and benchmark_change is not None
        else None
    )

    identity = (market_context.get("sector_identities") or {}).get(f"{market}:{symbol}", {})
    sector = identity.get("sector_name") or _lookup_sector(row, sector_map)
    sector_code = str(identity.get("sector_code") or row.get("sector_code") or row.get("industry_code")
                      or ("NAME:" + sector if sector else ""))
    if identity:
        sector_code = str(identity.get("sector_code") or "")
    if sector_code == "00":
        sector_code = ""
    sector_mapping_status = "MAPPED" if sector_code else "SECTOR_MAPPING_MISSING"
    contract = "sector_snapshots" in market_context
    snapshot = (market_context.get("sector_snapshots") or {}).get(sector_code)
    sector_change = sector_changes.get(sector_code if contract else sector) if sector_code else None
    sector_age = None
    sector_stale = False
    if contract and snapshot is not None:
        source_time = _parse_datetime(snapshot.get("source_at"))
        reference = _parse_datetime(snapshot_reference_at or market_context.get("sector_snapshot_reference_at"))
        sector_age = (reference - source_time).total_seconds() if source_time and reference else None
        sector_stale = sector_age is None or sector_age < -2 or sector_age > float(market_context.get("sector_snapshot_max_age_seconds", 60))
    sector_rs = (
        change_pct - sector_change
        if change_pct is not None and sector_change is not None
        else None
    )
    # 資格判定與輸出精度一致，避免浮點尾差讓顯示 0 的 RS 被判為正值。
    market_rs, sector_rs = _round(market_rs), _round(sector_rs)
    sector_reason = ("SECTOR_MAPPING_MISSING" if not sector_code else
                     "SECTOR_SNAPSHOT_MISSING" if sector_change is None else
                     "SECTOR_DATA_STALE" if sector_stale else
                     "SECTOR_RS_NOT_STRONG" if sector_rs is None or sector_rs <= 0 else "")

    if price is not None and high is not None and high > 0:
        distance_to_high = max(0.0, (high - price) / high * 100.0)
    else:
        distance_to_high = None
    near_high = bool(
        distance_to_high is not None
        and distance_to_high <= params.near_high_max_distance_pct
    )

    spread_pct: Optional[float] = None
    if bid is not None and ask is not None and bid > 0 and ask >= bid:
        midpoint = (bid + ask) / 2.0
        if midpoint > 0:
            spread_pct = (ask - bid) / midpoint * 100.0

    volume_ratio, volume_ratio_source = _resolve_volume_ratio(row)
    day_trade_eligible, day_trade_source = _lookup_day_trade_eligibility(
        row, day_trade_map
    )

    change_score = _linear_score(
        change_pct, params.min_change, params.change_score_full_pct
    )
    volume_score = _log_multiplier_score(
        float(volume),
        max(float(params.min_volume), 1.0),
        params.volume_score_full_multiplier,
    )
    turnover_score = _log_multiplier_score(
        turnover,
        max(float(params.min_turnover), 1.0),
        params.turnover_score_full_multiplier,
    )
    volume_ratio_score = _linear_score(
        volume_ratio, params.min_volume_ratio, params.volume_ratio_full
    )
    market_rs_score = _positive_score(market_rs, params.market_rs_full_pct)
    sector_rs_score = _positive_score(sector_rs, params.sector_rs_full_pct)
    near_high_component = _near_high_score(
        distance_to_high, params.near_high_score_zero_distance_pct
    )
    spread_component = _spread_score(
        spread_pct, params.spread_good_pct, params.spread_bad_pct
    )

    liquidity_inputs = [
        score
        for score in (volume_score, turnover_score, spread_component)
        if score is not None
    ]
    liquidity_score = (
        sum(liquidity_inputs) / len(liquidity_inputs) if liquidity_inputs else None
    )

    components: Dict[str, Optional[float]] = {
        "change": change_score,
        "volume": volume_score,
        "turnover": turnover_score,
        "volume_ratio": volume_ratio_score,
        "market_relative_strength": market_rs_score,
        "sector_relative_strength": sector_rs_score,
        "near_intraday_high": near_high_component,
        "liquidity": liquidity_score,
    }
    candidate_score, score_coverage, used_weights = _weighted_available_score(
        components, params.score_weight
    )
    preliminary_score, preliminary_coverage, _ = _weighted_available_score(
        components, params.pre_score_weight
    )
    momentum, momentum_coverage, _ = _weighted_available_score(
        components, params.momentum_weight
    )
    trend_score, _, _ = _weighted_available_score(components, params.trend_weight)
    freshness = _freshness_fields(
        row, params, snapshot_reference_at=snapshot_reference_at
    )

    return {
        "source": str(row.get("source") or ""),
        "symbol": symbol,
        "security_type": row.get("security_type"),
        "security_type_name": row.get("security_type_name"),
        "name": name,
        "market": market,
        "trade_date": str(row.get("trade_date") or ""),
        "timestamp": row.get("timestamp"),
        "event_time": _to_int(row.get("event_time"), 0),
        "serial": _to_int(row.get("serial"), -1),
        "quote_sequence": _to_int(row.get("quote_sequence"), 0),
        "symbol_quote_sequence": _to_int(row.get("symbol_quote_sequence"), 0),
        "market_state_available": bool(row.get("market_state_available")),
        "market_state_source": str(row.get("market_state_source") or ""),
        "sector": sector,
        "sector_code": sector_code,
        "sector_name": sector or "",
        "sector_mapping_status": sector_mapping_status,
        "sector_gate_reason": sector_reason,
        "sector_data_age_seconds": sector_age,
        "sector_snapshot_max_age_seconds": float(market_context.get("sector_snapshot_max_age_seconds", 60)),
        "sector_snapshot": dict(snapshot or {}),
        "benchmark": benchmark_name,
        "benchmark_change_pct": _round(benchmark_change),
        "sector_change_pct": _round(sector_change),
        "sector_change_source": sector_change_source,
        "price": _round(price),
        "open": _round(open_price),
        "high": _round(high),
        "low": _round(low),
        "prev_close": _round(prev_close),
        "change_pct": _round(change_pct),
        "open_change_pct": _round(open_change_pct),
        "volume": volume,
        "raw_volume": raw_volume,
        "source_volume_unit": str(row.get("volume_unit") or "LEGACY_SHARE"),
        "volume_unit": "SHARE",
        "turnover": round(turnover, 2),
        "volume_ratio": _round(volume_ratio),
        "volume_ratio_source": volume_ratio_source,
        "market_relative_strength": _round(market_rs),
        "sector_relative_strength": _round(sector_rs),
        "momentum_eligibility_reasons": (["MOMENTUM_SCORE_BELOW_MIN"] if momentum < params.momentum_candidate_min_score else [])
            + (["MARKET_RS_MISSING"] if market_rs is None else ["MARKET_RS_NOT_POSITIVE"] if market_rs <= 0 else []),
        "trend_eligibility_reasons": (["TREND_SCORE_BELOW_MIN"] if trend_score < params.trend_candidate_min_score else [])
            + (["MARKET_RS_MISSING"] if market_rs is None else ["MARKET_RS_NOT_POSITIVE"] if market_rs <= 0 else [])
            + ([sector_reason] if sector_reason else []),
        "distance_to_intraday_high_pct": _round(distance_to_high),
        "near_intraday_high": near_high,
        "bid": _round(bid),
        "ask": _round(ask),
        "spread_pct": _round(spread_pct),
        "liquidity_score": _round(liquidity_score, 2),
        "day_trade_eligible": day_trade_eligible,
        "day_trade_status": (
            "ELIGIBLE"
            if day_trade_eligible is True
            else "INELIGIBLE" if day_trade_eligible is False else "UNKNOWN"
        ),
        "day_trade_source": day_trade_source,
        **freshness,
        "trade_ready": False,
        "trade_block_reasons": [],
        "component_scores": {
            key: _round(value, 2) for key, value in components.items()
        },
        "score": round(candidate_score, 2),
        "candidate_score": round(candidate_score, 2),
        "momentum_candidate_score": round(momentum, 2),
        "trend_candidate_score": round(trend_score, 2),
        "momentum_eligible": momentum >= params.momentum_candidate_min_score
        and market_rs is not None
        and market_rs > 0,
        "trend_eligible": trend_score >= params.trend_candidate_min_score
        and market_rs is not None
        and market_rs > 0
        and sector_rs is not None
        and sector_rs > 0
        and not sector_reason,
        "score_coverage": round(score_coverage, 1),
        "score_used_weights": dict(sorted(used_weights.items())),
        "preliminary_score": round(preliminary_score, 2),
        "preliminary_score_coverage": round(preliminary_coverage, 1),
        "momentum": round(momentum, 2),
        "momentum_coverage": round(momentum_coverage, 1),
    }


def _qualification_reasons(
    item: Mapping[str, Any], params: ScannerParameters
) -> List[str]:
    reasons: List[str] = []
    symbol = str(item.get("symbol") or "")
    price = _to_float(item.get("price"))
    volume = _to_int(item.get("volume"), 0)
    turnover = _to_float(item.get("turnover")) or 0.0
    change_pct = _to_float(item.get("change_pct"))
    volume_ratio = _to_float(item.get("volume_ratio"))
    day_trade_eligible = item.get("day_trade_eligible")

    if re.fullmatch(COMMON_STOCK_SYMBOL_PATTERN, symbol) is None:
        reasons.append("NON_COMMON_STOCK")
    if item.get("security_type") not in (None, "", "01"):
        reasons.append("NON_COMMON_STOCK_SECURITY_TYPE")
    if price is None:
        reasons.append("NO_PRICE")
    elif price < params.min_price:
        reasons.append("PRICE_BELOW_MIN")
    elif price > params.max_price:
        reasons.append("PRICE_ABOVE_MAX")
    if volume < params.min_volume:
        reasons.append("VOLUME_BELOW_MIN")
    if turnover < params.min_turnover:
        reasons.append("TURNOVER_BELOW_MIN")
    if change_pct is None:
        reasons.append("NO_CHANGE_PCT")
    elif change_pct < params.min_change:
        reasons.append("CHANGE_BELOW_MIN")

    if params.require_volume_ratio:
        if volume_ratio is None:
            reasons.append("NO_VOLUME_RATIO")
        elif volume_ratio < params.min_volume_ratio:
            reasons.append("VOLUME_RATIO_BELOW_MIN")
    if params.require_day_trade_eligible and day_trade_eligible is not True:
        reasons.append("DAY_TRADE_NOT_ELIGIBLE")
    # MarketState stale / missing 不屬 Candidate Ranking 排除條件，只在 trade_ready 阻擋。
    return reasons


def _candidate_source_summary(
    candidates: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    source_times: List[str] = []
    stale_symbols: List[str] = []
    stale_market_symbols: List[str] = []
    unknown = 0
    degraded = 0
    for candidate in candidates:
        source_at = str(
            candidate.get("realtime_source_at")
            or candidate.get("quote_source_at")
            or ""
        ).strip()
        if source_at:
            source_times.append(source_at)
        else:
            unknown += 1
        status = str(candidate.get("realtime_freshness_status") or "").upper()
        if status == "SOURCE_STALE":
            symbol = str(candidate.get("symbol") or "").strip()
            market = str(candidate.get("market") or "").strip()
            stale_symbols.append(symbol)
            stale_market_symbols.append(_market_symbol_key(market, symbol))
        if status not in {"FRESH", "SOURCE_STALE"}:
            degraded += 1
    ordered = sorted(source_times, key=_source_at_sort_key)
    oldest = ordered[0] if ordered else None
    latest = ordered[-1] if ordered else None
    if stale_symbols:
        status = "SOURCE_STALE"
    elif unknown or degraded:
        status = "DEGRADED"
    else:
        status = "READY"
    return {
        "candidate_source_at": oldest,
        "candidate_source_at_oldest": oldest,
        "candidate_source_at_latest": latest,
        "candidate_source_known_count": len(source_times),
        "candidate_source_unknown_count": unknown,
        "candidate_source_degraded_count": degraded,
        "candidate_freshness_unknown_count": degraded,
        "stale_candidate_count": len(set(stale_symbols)),
        "stale_symbols": sorted(set(stale_symbols)),
        "stale_market_symbols": sorted(set(stale_market_symbols)),
        "candidate_source_status": status,
        "rule": "OLDEST_ACTUAL_CANDIDATE_FUGLE_EVENT_TIME",
    }


def _candidate_fingerprint_payload(
    candidates: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    fields = (
        "symbol",
        "market",
        "rank",
        "score",
        "score_coverage",
        "momentum",
        "trade_ready",
        "trade_block_reasons",
        "event_time",
        "serial",
        "quote_sequence",
        "realtime_source_at",
        "realtime_age_seconds",
        "realtime_fresh",
        "realtime_freshness_status",
        "quote_source_at",
        "quote_source_status",
    )
    return [
        {field: candidate.get(field) for field in fields} for candidate in candidates
    ]


# =============================================================================
# 純 Snapshot Scanner
# =============================================================================


def score_batch(payload):
    """可序列化的純計算工作；不持有 Gateway、GUI 或交易狀態。"""
    rows, context, sector_map, sector_changes, source, day_trade, values, reference = payload
    params = ScannerParameters(**values)
    evaluated = []
    for row in rows:
        item = _metric_components(row, market_context=context, sector_map=sector_map,
                                  sector_changes=sector_changes, sector_change_source=source,
                                  day_trade_map=day_trade, params=params,
                                  snapshot_reference_at=reference)
        reasons = _qualification_reasons(item, params)
        item["qualification"] = not reasons
        item["rejection_reasons"] = reasons
        evaluated.append(item)
    return evaluated


def scan_candidates(
    stock_data: Sequence[Mapping[str, Any]],
    market_context: Mapping[str, Any],
    *,
    parameters: Optional[ScannerParameters] = None,
    sector_map: Optional[Mapping[str, str]] = None,
    day_trade_map: Optional[Mapping[str, bool]] = None,
    session_phase: str = "REGULAR",
    snapshot_reference_at: Any = None,
    resource_manager: Any = None,
    compute_timeout_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """純函式評分核心；不連外、不讀檔、不呼叫 Gateway。"""
    params = parameters or ScannerParameters()
    params.validate()
    rows = _deduplicate_rows(stock_data)
    clean_sector_map = _normalize_sector_map(market_context.get("sector_map") if sector_map is None else sector_map)
    clean_day_trade_map = _normalize_day_trade_payload(day_trade_map)
    sector_changes, sector_change_source = _resolve_sector_changes(
        rows,
        market_context,
        clean_sector_map,
        params.sector_change_source,
    )

    from dataclasses import asdict
    values = asdict(params)
    batches = [(rows[i:i+32], dict(market_context), clean_sector_map, sector_changes,
                sector_change_source, clean_day_trade_map, values, snapshot_reference_at)
               for i in range(0, len(rows), 32)]
    if resource_manager is not None and batches:
        parts = resource_manager.compute_batches(score_batch, batches,
                                                  params.scanner_round_time_limit_seconds if compute_timeout_seconds is None else compute_timeout_seconds)
    else:
        parts = [score_batch(batch) for batch in batches]
    evaluated = [item for part in parts for item in part]
    rejection_counter = Counter(reason for item in evaluated for reason in item["rejection_reasons"])

    # score 本身原已只依可用欄位重新正規化，但舊 score_coverage 卻固定以全部
    # SCORE_WEIGHT 為分母。類股對照資料可能缺漏，MarketState 也不保證
    # volume_ratio，因此全市場理論上最多只有 70% 覆蓋率，永遠無法通過預設 75%。
    # 此處只校正「資料完整度」的分母，不改任何分數、權重或資格條件。
    coverage_fields = tuple(
        name
        for name in sorted(params.score_weight)
        if any(
            isinstance(item.get("component_scores"), Mapping)
            and item["component_scores"].get(name) is not None
            for item in evaluated
        )
    )
    coverage_capacity_weight = sum(
        float(params.score_weight[name]) for name in coverage_fields
    )
    coverage_capacity_pct = round(coverage_capacity_weight * 100.0, 1)
    for item in evaluated:
        components = item.get("component_scores")
        components = components if isinstance(components, Mapping) else {}
        raw_coverage = float(item.get("score_coverage") or 0.0)
        available_weight = sum(
            float(params.score_weight[name])
            for name in coverage_fields
            if components.get(name) is not None
        )
        item["score_coverage_raw"] = round(raw_coverage, 1)
        item["score_coverage"] = round(
            available_weight / coverage_capacity_weight * 100.0,
            1,
        ) if coverage_capacity_weight > 0 else 0.0

    eligible = [item for item in evaluated if item.get("qualification")]
    # 不再 PRESELECT -> 再抓行情 -> SCORE_POOL；所有 eligible 都直接以同一 MarketState Snapshot 評分。
    scored = [
        item
        for item in eligible
        if float(item.get("score") or 0.0) >= params.min_score
        and float(item.get("score_coverage") or 0.0) >= params.min_score_coverage
    ]
    score_gate_summary = {
        "qualification_passed": len(eligible),
        "score_passed": sum(
            1
            for item in eligible
            if float(item.get("score") or 0.0) >= params.min_score
        ),
        "coverage_passed": sum(
            1
            for item in eligible
            if float(item.get("score_coverage") or 0.0)
            >= params.min_score_coverage
        ),
        "all_score_gates_passed": len(scored),
        "below_min_score": sum(
            1
            for item in eligible
            if float(item.get("score") or 0.0) < params.min_score
        ),
        "below_min_score_coverage": sum(
            1
            for item in eligible
            if float(item.get("score_coverage") or 0.0)
            < params.min_score_coverage
        ),
        "coverage_capacity_pct": coverage_capacity_pct,
        "coverage_fields": list(coverage_fields),
        "unavailable_coverage_fields": sorted(
            set(params.score_weight) - set(coverage_fields)
        ),
        "coverage_basis": "ROUND_SNAPSHOT_SCOREABLE_FIELDS",
    }
    candidates = _sort_items(scored, params.sort_rule)[: params.top_n]

    phase = _normalize_session_phase(session_phase)
    market_blocked = bool(
        params.require_market_allow_long
        and not bool(market_context.get("allow_long", True))
    )
    for rank, item in enumerate(candidates, 1):
        item["rank"] = rank
        item["session_phase"] = phase
        reasons: List[str] = []
        if not _session_trade_ready(phase):
            reasons.append(f"SESSION_{phase}_NOT_TRADE_READY")
        if market_blocked:
            reasons.append("MARKET_ALLOW_LONG_FALSE")
        if (
            params.require_realtime_price
            and item.get("realtime_price_available") is not True
        ):
            reasons.append("NO_MARKETSTATE_REALTIME_PRICE")
        freshness_status = str(
            item.get("realtime_freshness_status") or "UNKNOWN"
        ).upper()
        if freshness_status == "SOURCE_STALE":
            reasons.append("REALTIME_SOURCE_STALE")
        elif freshness_status != "FRESH":
            reasons.append(f"REALTIME_FRESHNESS_{freshness_status}")
        item["trade_ready"] = not reasons
        item["trade_block_reasons"] = reasons

    candidate_keys = {_row_key(item) for item in candidates}
    for item in evaluated:
        if _row_key(item) not in candidate_keys:
            item.pop("rank", None)

    source_summary = _candidate_source_summary(candidates)
    parameter_snapshot = params.snapshot()
    parameter_fingerprint = _stable_hash(parameter_snapshot)
    canonical_market_context = {
        "market_state": market_context.get("market_state"),
        "market_score": market_context.get("market_score"),
        "trading_mode": market_context.get("trading_mode"),
        "allow_long": market_context.get("allow_long"),
        "taiex": market_context.get("taiex"),
        "tpex": market_context.get("tpex"),
        "sector_context": market_context.get("sector_context"),
    }
    data_fingerprint = _stable_hash(
        {
            "market_context": canonical_market_context,
            "stock_data": [_canonical_row_for_hash(row) for row in rows],
            "sector_map": dict(sorted(clean_sector_map.items())),
            "day_trade_map": dict(sorted(clean_day_trade_map.items())),
            "session_phase": phase,
        }
    )
    candidate_fingerprint = _stable_hash(_candidate_fingerprint_payload(candidates))
    fingerprint = _stable_hash(
        {
            "parameter_fingerprint": parameter_fingerprint,
            "data_fingerprint": data_fingerprint,
            "candidate_fingerprint": candidate_fingerprint,
            "candidate_source": source_summary,
        }
    )

    trade_block_counter: Counter[str] = Counter()
    for item in candidates:
        trade_block_counter.update(item.get("trade_block_reasons") or [])
    warnings: List[str] = []
    stale_count = int(source_summary.get("stale_candidate_count") or 0)
    if stale_count:
        warnings.append(
            f"{stale_count} 檔 Candidate 行情 stale；保留排名，但 trade_ready=False。"
        )
    if any(not bool(row.get("market_state_available")) for row in rows):
        warnings.append(
            "部分 Active Universe 股票沒有 MarketState Snapshot；Scanner 不自行補抓行情。"
        )
    if market_blocked:
        warnings.append("Market Context allow_long=False；保留候選排名，但禁止新交易。")

    return {
        "ok": True,
        "market_state": market_context.get("market_state"),
        "market_score": market_context.get("market_score"),
        "trading_mode": market_context.get("trading_mode"),
        "allow_long": market_context.get("allow_long"),
        "blocked_by_market": market_blocked,
        "session_phase": phase,
        "pipeline": {
            "all_market": len(rows),
            "input_universe": len(rows),
            "evaluated": len(evaluated),
            "eligible": len(eligible),
            "scored": len(scored),
            "top_n_limit": params.top_n,
            "candidates": len(candidates),
            "score_gate_summary": score_gate_summary,
            "realtime_refetch": False,
            "preselect_refetch": False,
        },
        "candidates": candidates,
        **source_summary,
        "candidate_fingerprint": candidate_fingerprint,
        "fingerprint": fingerprint,
        "rejection_summary": dict(sorted(rejection_counter.items())),
        "score_gate_summary": score_gate_summary,
        "trade_ready_count": sum(1 for item in candidates if item.get("trade_ready")),
        "trade_blocked_count": sum(
            1 for item in candidates if not item.get("trade_ready")
        ),
        "trade_block_summary": dict(sorted(trade_block_counter.items())),
        "sector_change_source": sector_change_source,
        "parameters": parameter_snapshot,
        "parameter_fingerprint": parameter_fingerprint,
        "data_fingerprint": data_fingerprint,
        "sort_rule": [
            f"{field_name} {direction}"
            for field_name, direction in _normalized_sort_rule(params.sort_rule)
        ],
        "pre_sort_rule": [
            f"{field_name} {direction}"
            for field_name, direction in _normalized_sort_rule(params.pre_sort_rule)
        ],
        "output_fields": OUTPUT_FIELDS,
        "warnings": warnings,
    }


def get_candidates(
    *,
    resource_manager: Any = None,
    compute_timeout_seconds: Optional[float] = None,
    active_universe_snapshot: Any = None,
    market_state_snapshot: Any = None,
    market_context: Optional[Mapping[str, Any]] = None,
    day_trade_map: Optional[Mapping[str, bool]] = None,
    sector_map: Optional[Mapping[str, str]] = None,
    parameters: Optional[ScannerParameters] = None,
    session_phase: Optional[str] = None,
    stock_data: Optional[Sequence[Mapping[str, Any]]] = None,
    # 保留既有 Self-test 明確使用的相容輸入；不觸發連外或檔案查詢。
    gateway: Any = None,
    api_mode: str = "LIVE",
    replay_dir: str = "api_replay",
) -> Dict[str, Any]:
    """Scanner 統一入口：只接受外部已形成的 Snapshot，永不主動抓行情。"""
    del (
        gateway,
        api_mode,
        replay_dir,
    )
    # 輸入固定為不可變位元組；計算端只取得獨立副本。
    import pickle
    frozen_input = pickle.dumps((active_universe_snapshot, market_state_snapshot, market_context,
        day_trade_map, sector_map, stock_data), protocol=pickle.HIGHEST_PROTOCOL)
    (active_universe_snapshot, market_state_snapshot, market_context,
     day_trade_map, sector_map, stock_data) = pickle.loads(frozen_input)
    started = time.perf_counter()
    params = parameters or ScannerParameters()
    params.validate()

    if active_universe_snapshot is None:
        active_universe_snapshot = stock_data
    if active_universe_snapshot is None:
        raise ValueError(
            "Scanner 需要 active_universe_snapshot（舊介面可用 stock_data 傳入）"
        )
    if market_context is None:
        raise ValueError("Scanner 需要 market_context")

    universe_rows, universe_info = _prepare_active_universe(
        active_universe_snapshot, params.active_universe_n
    )
    if not universe_rows:
        raise RuntimeError("Active Universe Snapshot 無可用普通股")

    # 若呼叫端尚未切到 market_state_snapshot，新 Scanner 不會自行補抓；只允許使用 stock_data 內已嵌入的 MarketState 欄位。
    if market_state_snapshot is None:
        embedded = [
            row
            for row in universe_rows
            if _to_int(row.get("event_time"), 0) > 0
            or _to_int(row.get("quote_sequence"), 0) > 0
            or str(row.get("realtime_source_at") or "").strip()
        ]
        market_state_snapshot = embedded
        market_state_mode = "EMBEDDED_COMPAT" if embedded else "MISSING"
    else:
        market_state_mode = "EXPLICIT"

    merged_rows, market_state_info = _merge_universe_market_state(
        universe_rows, market_state_snapshot
    )
    snapshot_reference_at = market_state_info.get(
        "snapshot_at"
    ) or market_state_info.get("as_of")
    sector_map = _normalize_sector_map(market_context.get("sector_map") if sector_map is None else sector_map)
    result = scan_candidates(
        merged_rows,
        dict(market_context),
        parameters=params,
        sector_map=sector_map,
        day_trade_map=day_trade_map,
        session_phase=_normalize_session_phase(
            session_phase or market_context.get("session_phase")
        ),
        snapshot_reference_at=snapshot_reference_at,
        resource_manager=resource_manager,
        compute_timeout_seconds=compute_timeout_seconds,
    )

    # 最終 fingerprint 明確納入原始 Active Universe / MarketState Snapshot，使相同輸入必須完全一致。
    clean_day_trade = _normalize_day_trade_payload(day_trade_map)
    external_input_fingerprint = _stable_hash(
        {
            "active_universe": [_canonical_row_for_hash(row) for row in universe_rows],
            "market_state": [
                _canonical_row_for_hash(row)
                for row in _deduplicate_rows(
                    _extract_snapshot_rows(market_state_snapshot)[0]
                )
            ],
            "market_context": {
                "market_state": market_context.get("market_state"),
                "market_score": market_context.get("market_score"),
                "trading_mode": market_context.get("trading_mode"),
                "allow_long": market_context.get("allow_long"),
                "taiex": market_context.get("taiex"),
                "tpex": market_context.get("tpex"),
                "sector_context": market_context.get("sector_context"),
            },
            "day_trade_map": dict(sorted(clean_day_trade.items())),
            "sector_map": dict(sorted(_normalize_sector_map(sector_map).items())),
            "parameters": params.snapshot(),
            "session_phase": result.get("session_phase"),
        }
    )
    result["input_snapshot_id"] = external_input_fingerprint
    result["frozen_input_snapshot"] = {"snapshot_id": external_input_fingerprint,
        "captured_at": snapshot_reference_at, "source_at": market_state_info.get("source_at"),
        "market_permission_snapshot_id": market_context.get("market_permission_snapshot_id"),
        "session_trade_date": market_context.get("session_trade_date"),
        "ttl_seconds": params.realtime_freshness_max_age_seconds}
    result["config_id"] = _stable_hash(params.snapshot())
    result["result_source_at"] = result.get("candidate_source_at")
    result["result_max_age_seconds"] = params.realtime_freshness_max_age_seconds
    result["data_fingerprint"] = external_input_fingerprint
    result["fingerprint"] = _stable_hash(
        {
            "data_fingerprint": external_input_fingerprint,
            "parameter_fingerprint": result.get("parameter_fingerprint"),
            "candidate_fingerprint": result.get("candidate_fingerprint"),
            "candidate_source": {
                "candidate_source_at": result.get("candidate_source_at"),
                "candidate_source_at_latest": result.get("candidate_source_at_latest"),
                "candidate_source_status": result.get("candidate_source_status"),
                "stale_candidate_count": result.get("stale_candidate_count"),
            },
        }
    )

    candidate_status = str(result.get("candidate_source_status") or "DEGRADED").upper()
    phase = str(result.get("session_phase") or "REGULAR").upper()
    if phase == "PREOPEN":
        snapshot_status = "PREOPEN_OBSERVATION"
    elif candidate_status == "SOURCE_STALE":
        snapshot_status = "SOURCE_STALE"
    elif candidate_status == "READY":
        snapshot_status = "COMPLETE"
    else:
        snapshot_status = "DEGRADED"
    result["snapshot_status"] = snapshot_status
    result["integration"] = {
        "mode": "SNAPSHOT_ONLY",
        "network_calls": False,
        "gateway_calls": False,
        "runtime_module_contract": "runtime",
        "active_universe": universe_info,
        "market_state": {
            **market_state_info,
            "mode": market_state_mode,
            "source_time_policy": "FUGLE_EVENT_TIME_ONLY",
            "received_at_is_source_time": False,
            "fetched_at_is_source_time": False,
        },
        "candidate_data_time": {
            "candidate_source_at": result.get("candidate_source_at"),
            "candidate_source_at_oldest": result.get("candidate_source_at_oldest"),
            "candidate_source_at_latest": result.get("candidate_source_at_latest"),
            "candidate_source_status": result.get("candidate_source_status"),
            "stale_candidate_count": result.get("stale_candidate_count"),
            "stale_symbols": result.get("stale_symbols"),
            "rule": "OLDEST_ACTUAL_CANDIDATE_FUGLE_EVENT_TIME",
        },
        "publication_contract": "ATOMIC_SNAPSHOT_RESULT",
        "active_universe_rebuild_policy": "EXTERNAL_RUNTIME_SCHEDULE_ONLY",
        "single_trade_event_rebuilds_universe": False,
        "ranking_uses_event_arrival_speed": False,
    }
    result["performance_ms"] = {
        "total_ms": round((time.perf_counter() - started) * 1000.0, 3)
    }
    return result


# =============================================================================
# 離線自我測試與診斷
# =============================================================================


def run_self_test() -> Dict[str, Any]:
    params = ScannerParameters(
        min_volume=100_000,
        min_turnover=10_000_000.0,
        min_change=0.5,
        min_score=0.0,
        min_score_coverage=0.0,
        top_n=3,
        realtime_freshness_max_age_seconds=60.0,
    )
    context = {
        "market_state": "BULL",
        "market_score": 42.5,
        "trading_mode": "TREND",
        "allow_long": True,
        "taiex": {"change_pct": 0.5},
        "tpex": {"change_pct": 0.8},
        "sector_context": {
            "ranking": [{"sector": "半導體業", "turnover_weighted_change_pct": 1.2}]
        },
    }
    universe = [
        {
            "symbol": "2330",
            "market": "TWSE",
            "name": "A",
            "sector": "半導體業",
            "avg_volume": 800_000,
        },
        {
            "symbol": "2454",
            "market": "TWSE",
            "name": "B",
            "sector": "半導體業",
            "avg_volume": 700_000,
        },
        {
            "symbol": "6488",
            "market": "TPEx",
            "name": "C",
            "sector": "半導體業",
            "avg_volume": 500_000,
        },
    ]

    def state(
        symbol: str,
        market: str,
        price: float,
        prev_close: float,
        volume: int,
        turnover: float,
        event_time: int,
        serial: int,
        age: float,
    ) -> Dict[str, Any]:
        return {
            "source": "FUGLE_WEBSOCKET",
            "symbol": symbol,
            "market": market,
            "event_time": event_time,
            "serial": serial,
            "quote_sequence": serial,
            "price": price,
            "open": prev_close * 1.002,
            "high": price * 1.002,
            "low": prev_close * 0.998,
            "prev_close": prev_close,
            "volume": volume,
            "turnover": turnover,
            "bid": price - 0.05,
            "ask": price + 0.05,
            "received_at": "2026-09-11T09:31:00+08:00",
            "realtime_age_seconds": age,
            "realtime_fresh": age <= 60.0,
            "freshness_basis": "event_time",
        }

    base_us = int(
        datetime(2026, 9, 11, 9, 30, 0, tzinfo=TAIPEI_TZ).timestamp() * 1_000_000
    )
    market_state = [
        state(
            "2330", "TWSE", 103.0, 100.0, 1_500_000, 150_000_000.0, base_us, 101, 1.0
        ),
        state(
            "2454",
            "TWSE",
            102.6,
            100.0,
            1_300_000,
            130_000_000.0,
            base_us + 1_000_000,
            102,
            2.0,
        ),
        state(
            "6488",
            "TPEx",
            103.8,
            100.0,
            1_100_000,
            120_000_000.0,
            base_us + 2_000_000,
            103,
            75.0,
        ),
    ]
    day_trade = {"2330": True, "2454": True, "6488": True}

    first = get_candidates(
        active_universe_snapshot=universe,
        market_state_snapshot=market_state,
        market_context=context,
        day_trade_map=day_trade,
        parameters=params,
        session_phase="REGULAR",
    )
    second = get_candidates(
        active_universe_snapshot=list(reversed(universe)),
        market_state_snapshot=list(reversed(market_state)),
        market_context=context,
        day_trade_map=dict(reversed(list(day_trade.items()))),
        parameters=params,
        session_phase="REGULAR",
    )
    assert first["candidates"] == second["candidates"]
    assert first["fingerprint"] == second["fingerprint"]
    assert first["candidate_fingerprint"] == second["candidate_fingerprint"]

    stale = next(item for item in first["candidates"] if item["symbol"] == "6488")
    assert stale["realtime_freshness_status"] == "SOURCE_STALE"
    assert stale["trade_ready"] is False
    assert "REALTIME_SOURCE_STALE" in stale["trade_block_reasons"]
    assert stale["realtime_source_at"] == "2026-09-11T09:30:02.000000+08:00"
    assert first["candidate_source_at"] == "2026-09-11T09:30:00.000000+08:00"
    assert first["integration"]["gateway_calls"] is False
    assert first["integration"]["single_trade_event_rebuilds_universe"] is False

    # 接收時間改變不得直接改變 Score / 排序；freshness 是獨立交易門檻。
    delayed_state = [
        dict(row, received_at="2026-09-11T09:31:30+08:00") for row in market_state
    ]
    delayed = get_candidates(
        active_universe_snapshot=universe,
        market_state_snapshot=delayed_state,
        market_context=context,
        day_trade_map=day_trade,
        parameters=params,
        session_phase="REGULAR",
    )
    assert [(x["symbol"], x["score"], x["rank"]) for x in first["candidates"]] == [
        (x["symbol"], x["score"], x["rank"]) for x in delayed["candidates"]
    ]

    # 舊 Runtime 仍傳 gateway 等參數時，Scanner 不使用它們，且沒有 MarketState 時 fail-closed trade_ready。
    compat = get_candidates(
        gateway=object(),
        api_mode="LIVE",
        replay_dir="ignored",
        market_context=context,
        stock_data=[
            dict(
                row,
                price=101.0,
                volume=1_000_000,
                turnover=100_000_000.0,
                change_percent=1.5,
            )
            for row in universe
        ],
        day_trade_map=day_trade,
        parameters=params,
        session_phase="REGULAR",
    )
    assert compat["integration"]["market_state"]["mode"] == "MISSING"
    assert all(item["trade_ready"] is False for item in compat["candidates"])

    # 正式 Runtime 不傳 sector_map，MarketState 也不保證 volume_ratio；可用核心欄位
    # 完整時仍必須能通過 75% 覆蓋率門檻，且保留原始絕對覆蓋率供診斷。
    coverage_params = replace(params, min_score=70.0, min_score_coverage=75.0)
    coverage_universe = [
        {
            key: value
            for key, value in universe[0].items()
            if key not in {"sector", "avg_volume"}
        }
    ]
    coverage_result = get_candidates(
        active_universe_snapshot=coverage_universe,
        market_state_snapshot=[market_state[0]],
        market_context=context,
        day_trade_map={"2330": True},
        parameters=coverage_params,
        session_phase="REGULAR",
    )
    assert len(coverage_result["candidates"]) == 1
    coverage_candidate = coverage_result["candidates"][0]
    assert coverage_candidate["score_coverage_raw"] == 70.0
    assert coverage_candidate["score_coverage"] == 100.0
    assert coverage_result["score_gate_summary"]["coverage_capacity_pct"] == 70.0
    assert set(
        coverage_result["score_gate_summary"]["unavailable_coverage_fields"]
    ) == {"sector_relative_strength", "volume_ratio"}

    return {
        "ok": True,
        "checks": 18,
        "pipeline": "Active Universe Snapshot + MarketState Snapshot -> 全集合評分 -> Top N",
        "snapshot_only": True,
        "gateway_calls": False,
        "reproducibility": "相同 Active Universe + MarketState + Market Context + Scanner Parameters + Day Trade Map = 相同結果",
        "candidate_source_rule": "Fugle event_time",
        "sample_top": first["candidates"][0] if first["candidates"] else None,
    }


def diagnose_links() -> Dict[str, Any]:
    return {
        "ok": True,
        "scanner_entry": "get_candidates(active_universe_snapshot, market_state_snapshot, market_context, day_trade_map, ...)",
        "pure_core": "scan_candidates(stock_data, market_context, ...)",
        "runtime_module_contract": "runtime",
        "legacy_runtime_module_dependency": False,
        "network_calls": False,
        "gateway_calls": False,
        "market_api_calls": False,
        "active_universe_source": "CALLER_SNAPSHOT_ONLY",
        "market_state_source": "CALLER_SNAPSHOT_ONLY",
        "realtime_source_time": "FUGLE_EVENT_TIME",
        "scanner_completed_at_is_source_time": False,
        "received_at_is_source_time": False,
        "batch_quote_pipeline": False,
        "preselect_realtime_refetch": False,
        "active_universe_rebuild_on_single_event": False,
        "stale_candidate_policy": "KEEP_RANKING_AND_TRADE_READY_FALSE",
        "output_fields": OUTPUT_FIELDS,
    }


# =============================================================================
# CLI
# =============================================================================


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Snapshot-only 觀察選股與候選股評分模組"
    )
    parser.add_argument("--self-test", action="store_true", help="執行完全離線自我測試")
    parser.add_argument(
        "--diagnose", action="store_true", help="顯示 Snapshot-only 介面契約"
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 顯示結果")
    return parser


def _print_human(result: Mapping[str, Any]) -> None:
    for key, value in result.items():
        print(f"{key}: {value}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.self_test:
        result = run_self_test()
    elif args.diagnose:
        result = diagnose_links()
    else:
        result = {
            "ok": True,
            "message": "正式使用由 runtime / APIGateway 準備 Snapshot，再呼叫 get_candidates()；Scanner 不自行取得市場資料。",
            "output_fields": OUTPUT_FIELDS,
        }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        _print_human(result)
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
