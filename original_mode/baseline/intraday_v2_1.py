# -*- coding: utf-8 -*-
"""
intraday.py - 盤中行情與進出場訊號判定模組

用途
----
本模組接收 Scanner 候選股票與 Runtime／APIGateway 保存的 Fugle Trades Event Sequence，
只依 Fugle 原始 event_time／serial 建立盤中時間軸：

1. 建立固定 1 分鐘 K 棒，以及同一錨點聚合之 3 分鐘 / 5 分鐘 K 棒。
2. 由 Fugle Trade Event 的 size／累計 volume 建立每筆成交量增量與固定 VWAP。
3. 計算 1 / 3 / 5 分鐘價格動能、成交量加速度、盤中高低點。
4. 由行情資料本身推導 Momentum／Trend 波段、回踩與再突破結構，不依賴 GUI 暫存狀態。
5. 以動能突破型或回檔再突破型兩條獨立路徑判定 ENTRY，並沿用既有 EXIT 訊號。
6. Position 持倉監控採 P0 路徑，不依賴 Candidate List；Candidate 新進場採 P1 路徑。
7. 可由 APIGateway ReplayStore 重播同一 Fugle Trades Event Sequence，驗證訊號可重現。

資料邊界
--------
- api_gateway.py：Fugle Event 標準化與 Replay 保存；Intraday 不主動抓外部 Quote。
- market.py：市場環境、類股強弱與 allow_long。
- scanner.py：候選股評分與 Candidate_List。
- intraday.py：盤中結構與進出場訊號，不直接下單、不決定倉位大小。

可重現核心
----------
Signal_Result = f(Fugle_Trade_Event_Sequence, Market_Context, Candidate, Intraday_Parameters, Position)

相同 Fugle Trade Event Sequence + 相同事件時間 + 相同參數必須得到相同：
- quote 排序
- 1m / 3m / 5m K
- VWAP
- Momentum 1m / 3m / 5m
- Volume Acceleration
- panic / pullback / reclaim / breakout 結構
- entry / exit 訊號時間、價格與原因
- ENTRY Wave 起點、anchor、age 與 Wave Guard 結果
- Position EXIT 不受 Candidate freshness 或 Candidate List 淘汰影響

CLI
---
python intraday.py --self-test
python intraday.py --diagnose
"""

# 中文重點更新：類股限制使用 Runtime 提供的最新快照重新驗證，保留候選分數、個股結構及持倉出場規則。

# 中文重點更新：清理歷史相容參數、孤立註解及無效追單顯示，保留測試、診斷、CTS 撤單與委託及持倉對帳。

# 中文重點更新：清除未使用函式、相容介面與無效參數；保留市價買賣、成交對帳、撤買單、清倉及離線驗證。

# 中文重點更新：移除已停用策略欄位，分離事件聚合參數與策略參數，變更清倉時間保留已累積 K 棒。

# 中文重點更新：以兩套狀態機取代舊 A/B 反轉核心，加入結構停損與策略出場，限制快取容量並增量延續結構。

# 中文重點更新：補齊審查要求之狀態識別、整張委託、資料時序與恢復安全邊界。

# 中文重點更新：個股雙策略狀態機不變；Trend 進場與市場出場共用許可，資料逾時不誤當市場轉弱。

# 中文重點更新：Momentum Wave Age 預設五根；失效後須經新整理及原突破條件才重建，保留 Trend 個股策略。

# 中文重點更新：補齊 Momentum 持續上升與 Trend 首次確認進場路徑，逐策略提供完整 Entry 判定紀錄。

# 中文重點更新：一般出場須具備成交後證據，隔離成交前結構；緊急停損與收盤出場不受結構隔離限制。

# 中文重點更新：持續強勢可獨立建立 Wave、健康整理保留原 Age；早期趨勢與成熟分形並行，提供子條件實值診斷。

# 中文重點更新：訊號成立不寫入永久去重集合，保留原策略條件並交由有效委託與持倉防重。

# 中文重點更新：新增 Momentum／Trend 自訂全數停利，預設停用；依實際平均成交成本判定，保留原有風險出場優先順序。

from __future__ import annotations

from functools import lru_cache
from collections import OrderedDict

import argparse
import hashlib
import importlib
import inspect
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# =============================================================================
# 固定參數
# =============================================================================

TIMEZONE = "Asia/Taipei"
BAR_INTERVAL = "1m"
VWAP_METHOD = "TRADE_PRICE_X_CUMULATIVE_VOLUME_DELTA"

MOMENTUM_1M = 1
MOMENTUM_3M = 3
MOMENTUM_5M = 5
VOLUME_WINDOW = 6
BREAKOUT_WINDOW = 5

ENTRY_START_TIME = "09:05:00"
ENTRY_END_TIME = "12:15:00"
EXIT_TIME = "13:00:00"
SESSION_START_TIME = "09:00:00"
SESSION_END_TIME = "13:30:00"

MIN_BARS = 7


# VWAP / 量能 / 突破確認。
BREAKOUT_BUFFER_PCT = 0.05
MAX_ENTRY_WAVE_BARS = 5



OUTPUT_FIELDS = [
    "strategy_type",
    "strategy_state",
    "entry_setup_type",
    "exit_stage",
    "exit_fraction",
    "strategy_stop_price",
    "emergency_stop_price",
    "entry_signal",
    "entry_reason",
    "entry_time",
    "signal_price",
    "stop_signal",
    "take_profit_signal",
    "exit_signal",
    "exit_reason",
    "R_value",
]

CONDITION_FIELDS = ["entry_setup", "candidate_ready"]

ENTRY_DIAGNOSTIC_FIELDS = [
    "strategy_type",
    "strategy_state",
    "entry_setup_type",
    "volume_expansion_ratio",
    "range_expansion_ratio",
    "ema5",
    "ema20",
    "strategy_stop_price",
    "emergency_stop_price",
    "exit_stage",
    "exit_fraction",
    "entry_wave_id",
    "entry_wave_age_bars",
    "entry_wave_extension_pct",
    "event_time",
    "event_age_seconds",
]


# =============================================================================
# 參數模型
# =============================================================================


@dataclass(frozen=True)
class IntradayParameters:
    #分鐘成交量擴張倍數
    volume_expansion_min: float = 1.5
    #分鐘價格區間擴張倍數
    range_expansion_min: float = 1.3
    ema_fast_period: int = 5
    #成熟 Trend 結構
    ema_slow_period: int = 10
    pullback_volume_max: float = 0.8
    momentum_decay_volume_max: float = 0.8
    momentum_partial_exit_fraction: float = 0.5
    # 0 停用自訂停利；正值為相對 CTS 實際平均成交成本的全數停利百分比。
    momentum_take_profit_pct: float = 0.0
    trend_take_profit_pct: float = 0.0
    structure_buffer_pct: float = 0.5
    emergency_stop_loss_pct: float = 2.5
    trend_rs_min: float = 0.0
    momentum_rs_min: float = 0.0
    persistent_bars: int = 3
    persistent_volume_min: float = 1.0
    healthy_pullback_pct: float = 0.35
    healthy_pullback_volume_max: float = 1.2
    trend_initial_bars: int = 7

    timezone: str = TIMEZONE
    bar_interval: str = BAR_INTERVAL
    vwap_method: str = VWAP_METHOD

    momentum_1m: int = MOMENTUM_1M
    momentum_3m: int = MOMENTUM_3M
    momentum_5m: int = MOMENTUM_5M
    volume_window: int = VOLUME_WINDOW
    breakout_window: int = BREAKOUT_WINDOW

    entry_start_time: str = ENTRY_START_TIME
    entry_end_time: str = ENTRY_END_TIME
    exit_time: str = EXIT_TIME
    session_start_time: str = SESSION_START_TIME
    session_end_time: str = SESSION_END_TIME

    min_bars: int = MIN_BARS


    breakout_buffer_pct: float = BREAKOUT_BUFFER_PCT
    max_entry_wave_bars: int = MAX_ENTRY_WAVE_BARS
    momentum_setup_ttl_seconds: float = 600.0
    #Trend Setup 有效期
    trend_anchor_timeout_seconds: float = 900.0


    def validate(self) -> None:
        for name in ("momentum_take_profit_pct", "trend_take_profit_pct"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} 必須為大於或等於零的有限數值")
        if self.persistent_bars < 3 or self.trend_initial_bars < self.ema_fast_period:
            raise ValueError("持續確認至少三根；早期趨勢資料量不可小於快速 EMA 週期")
        for value in (self.momentum_rs_min, self.persistent_volume_min, self.healthy_pullback_pct, self.healthy_pullback_volume_max):
            if not math.isfinite(value) or value < 0:
                raise ValueError("持續進場與健康回檔參數必須是非負有限數值")
        for value in (self.momentum_setup_ttl_seconds, self.trend_anchor_timeout_seconds):
            if not math.isfinite(float(value)) or value <= 0:
                raise ValueError("策略時間有效期限須為正有限數值")
        if not math.isfinite(float(self.volume_expansion_min)):
            raise ValueError("volume_expansion_min 必須為有限數值")
        if self.volume_expansion_min <= 0:
            raise ValueError("volume_expansion_min 必須大於零")
        if not math.isfinite(float(self.range_expansion_min)):
            raise ValueError("range_expansion_min 必須為有限數值")
        if self.range_expansion_min <= 0:
            raise ValueError("range_expansion_min 必須大於零")
        if not math.isfinite(float(self.ema_fast_period)):
            raise ValueError("ema_fast_period 必須為有限數值")
        if self.ema_fast_period <= 0:
            raise ValueError("ema_fast_period 必須大於零")
        if not math.isfinite(float(self.ema_slow_period)):
            raise ValueError("ema_slow_period 必須為有限數值")
        if self.ema_slow_period <= 0:
            raise ValueError("ema_slow_period 必須大於零")
        if not math.isfinite(float(self.pullback_volume_max)):
            raise ValueError("pullback_volume_max 必須為有限數值")
        if self.pullback_volume_max <= 0:
            raise ValueError("pullback_volume_max 必須大於零")
        if not math.isfinite(float(self.momentum_decay_volume_max)):
            raise ValueError("momentum_decay_volume_max 必須為有限數值")
        if self.momentum_decay_volume_max <= 0:
            raise ValueError("momentum_decay_volume_max 必須大於零")
        if not math.isfinite(float(self.momentum_partial_exit_fraction)):
            raise ValueError("momentum_partial_exit_fraction 必須為有限數值")
        if self.momentum_partial_exit_fraction <= 0:
            raise ValueError("momentum_partial_exit_fraction 必須大於零")
        if not math.isfinite(float(self.structure_buffer_pct)):
            raise ValueError("structure_buffer_pct 必須為有限數值")
        if self.structure_buffer_pct <= 0:
            raise ValueError("structure_buffer_pct 必須大於零")
        if not math.isfinite(float(self.emergency_stop_loss_pct)):
            raise ValueError("emergency_stop_loss_pct 必須為有限數值")
        if self.emergency_stop_loss_pct <= 0:
            raise ValueError("emergency_stop_loss_pct 必須大於零")
        if not math.isfinite(float(self.trend_rs_min)):
            raise ValueError("trend_rs_min 必須為有限數值")
        if (
            self.ema_fast_period >= self.ema_slow_period
            or not 0 < self.momentum_partial_exit_fraction < 1
        ):
            raise ValueError("EMA 快線週期必須小於慢線，分批比例須介於零與一之間")
        if str(self.timezone).strip() != TIMEZONE:
            raise ValueError(f"TIMEZONE 目前固定為 {TIMEZONE}")

        if self.bar_interval != "1m":
            raise ValueError("BAR_INTERVAL 目前固定只支援 1m")
        if self.vwap_method != VWAP_METHOD:
            raise ValueError(f"VWAP_METHOD 目前固定為 {VWAP_METHOD}")

        for name, value in (
            ("MOMENTUM_1M", self.momentum_1m),
            ("MOMENTUM_3M", self.momentum_3m),
            ("MOMENTUM_5M", self.momentum_5m),
            ("VOLUME_WINDOW", self.volume_window),
            ("BREAKOUT_WINDOW", self.breakout_window),
            ("MIN_BARS", self.min_bars),
            ("MAX_ENTRY_WAVE_BARS", self.max_entry_wave_bars),
        ):
            if int(value) <= 0:
                raise ValueError(f"{name} 必須 > 0")

        if not (
            self.momentum_1m == 1 and self.momentum_3m == 3 and self.momentum_5m == 5
        ):
            raise ValueError("MOMENTUM_1M / 3M / 5M 預設固定為 1 / 3 / 5")

        _parse_clock(self.session_start_time)
        _parse_clock(self.session_end_time)
        _parse_clock(self.entry_start_time)
        _parse_clock(self.entry_end_time)
        _parse_clock(self.exit_time)

        session_start = _parse_clock(self.session_start_time)
        session_end = _parse_clock(self.session_end_time)
        entry_start = _parse_clock(self.entry_start_time)
        entry_end = _parse_clock(self.entry_end_time)
        exit_at = _parse_clock(self.exit_time)
        if not session_start < session_end:
            raise ValueError("SESSION_START_TIME 必須早於 SESSION_END_TIME")
        if not session_start <= entry_start < entry_end <= session_end:
            raise ValueError("ENTRY_START_TIME / ENTRY_END_TIME 必須位於交易時段內")
        if not entry_end <= exit_at <= session_end:
            raise ValueError("EXIT_TIME 必須位於 ENTRY_END_TIME 之後且不晚於收盤")

        for name, value in (
            ("BREAKOUT_BUFFER_PCT", self.breakout_buffer_pct),
        ):
            if float(value) < 0:
                raise ValueError(f"{name} 不得 < 0")


    def snapshot(self) -> Dict[str, Any]:
        return {
            "VOLUME_EXPANSION_MIN": self.volume_expansion_min,
            "RANGE_EXPANSION_MIN": self.range_expansion_min,
            "EMA_FAST_PERIOD": self.ema_fast_period,
            "EMA_SLOW_PERIOD": self.ema_slow_period,
            "PULLBACK_VOLUME_MAX": self.pullback_volume_max,
            "MOMENTUM_DECAY_VOLUME_MAX": self.momentum_decay_volume_max,
            "MOMENTUM_PARTIAL_EXIT_FRACTION": self.momentum_partial_exit_fraction,
            "MOMENTUM_TAKE_PROFIT_PCT": self.momentum_take_profit_pct,
            "TREND_TAKE_PROFIT_PCT": self.trend_take_profit_pct,
            "STRUCTURE_BUFFER_PCT": self.structure_buffer_pct,
            "EMERGENCY_STOP_LOSS_PCT": self.emergency_stop_loss_pct,
            "TREND_RS_MIN": self.trend_rs_min,
            "MOMENTUM_RS_MIN": self.momentum_rs_min,
            "PERSISTENT_BARS": self.persistent_bars,
            "PERSISTENT_VOLUME_MIN": self.persistent_volume_min,
            "HEALTHY_PULLBACK_PCT": self.healthy_pullback_pct,
            "HEALTHY_PULLBACK_VOLUME_MAX": self.healthy_pullback_volume_max,
            "TREND_INITIAL_BARS": self.trend_initial_bars,
            "TIMEZONE": self.timezone,
            "MOMENTUM_SETUP_TTL_SECONDS": self.momentum_setup_ttl_seconds,
            "TREND_ANCHOR_TIMEOUT_SECONDS": self.trend_anchor_timeout_seconds,
            "BAR_INTERVAL": self.bar_interval,
            "VWAP_METHOD": self.vwap_method,
            "MOMENTUM_1M": int(self.momentum_1m),
            "MOMENTUM_3M": int(self.momentum_3m),
            "MOMENTUM_5M": int(self.momentum_5m),
            "VOLUME_WINDOW": int(self.volume_window),
            "BREAKOUT_WINDOW": int(self.breakout_window),
            "ENTRY_START_TIME": self.entry_start_time,
            "ENTRY_END_TIME": self.entry_end_time,
            "EXIT_TIME": self.exit_time,
            "SESSION_START_TIME": self.session_start_time,
            "SESSION_END_TIME": self.session_end_time,
            "MIN_BARS": int(self.min_bars),
            "BREAKOUT_BUFFER_PCT": float(self.breakout_buffer_pct),
            "MAX_ENTRY_WAVE_BARS": int(self.max_entry_wave_bars),
        }


# =============================================================================
# 共用工具
# =============================================================================


@lru_cache(maxsize=128)
def _parse_clock(value: str) -> time:
    text = str(value or "").strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            pass
    raise ValueError(f"時間格式錯誤：{value!r}，需為 HH:MM[:SS]")


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


def _pct_change(current: Optional[float], base: Optional[float]) -> Optional[float]:
    if current is None or base is None or base <= 0:
        return None
    return (float(current) - float(base)) / float(base) * 100.0


def _stable_hash(payload: Any) -> str:
    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _normalize_market(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in {"TWSE", "TSE", "LISTED"}:
        return "TWSE"
    if text in {"TPEX", "OTC"}:
        return "TPEx"
    if str(value or "").strip() == "TPEx":
        return "TPEx"
    return str(value or "").strip()


@lru_cache(maxsize=128)
def _taipei_timezone(timezone_name: str = TIMEZONE) -> timezone:
    """固定台灣交易時區，不依賴 Windows / Linux 外部 IANA tzdata。"""
    if str(timezone_name).strip() != TIMEZONE:
        raise ValueError(f"TIMEZONE 目前固定為 {TIMEZONE}")
    return timezone(timedelta(hours=8), name=TIMEZONE)


def _parse_timestamp(value: Any, timezone_name: str = TIMEZONE) -> Optional[datetime]:
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            # 支援 YYYY-MM-DD HH:MM:SS。
            try:
                dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
    tz = _taipei_timezone(timezone_name)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    else:
        dt = dt.astimezone(tz)
    return dt


def _parse_fugle_event_time(
    value: Any,
    timezone_name: str = TIMEZONE,
) -> Optional[datetime]:
    """解析 Fugle Trade event_time；K 棒歸屬只使用來源事件時間，不使用 received_at。"""
    if isinstance(value, datetime):
        return _parse_timestamp(value, timezone_name)

    number = _to_float(value)
    if number is not None and number > 0:
        try:
            if number > 10_000_000_000_000:
                seconds = number / 1_000_000.0
            elif number > 10_000_000_000:
                seconds = number / 1_000.0
            else:
                seconds = number
            return datetime.fromtimestamp(seconds, tz=_taipei_timezone(timezone_name))
        except (ValueError, OSError, OverflowError):
            return None

    return _parse_timestamp(value, timezone_name)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.isoformat(timespec="seconds")


@lru_cache(maxsize=128)
def _session_datetime(
    trade_date: date, clock_text: str, timezone_name: str
) -> datetime:
    tz = _taipei_timezone(timezone_name)
    return datetime.combine(trade_date, _parse_clock(clock_text), tzinfo=tz)


def _canonical_quote(
    row: Mapping[str, Any],
    params: IntradayParameters,
) -> Optional[Dict[str, Any]]:
    """把 Fugle Trade Event 正規化成 Intraday Tick；event_time 為唯一正式交易時間。"""
    raw_event_time = (
        row.get("event_time") if row.get("event_time") is not None else row.get("time")
    )
    event_dt = _parse_fugle_event_time(raw_event_time, params.timezone)
    legacy_dt = _parse_timestamp(row.get("timestamp"), params.timezone)
    timestamp = event_dt or legacy_dt
    price = _to_float(row.get("price"))
    symbol = str(row.get("symbol") or "").strip()
    market = _normalize_market(row.get("market"))
    if timestamp is None or price is None or price <= 0 or not symbol or not market:
        return None

    session_start = _session_datetime(
        timestamp.date(), params.session_start_time, params.timezone
    )
    session_end = _session_datetime(
        timestamp.date(), params.session_end_time, params.timezone
    )
    if timestamp < session_start or timestamp > session_end:
        return None

    event_time = _to_int(raw_event_time)
    serial = _to_int(row.get("serial")) if row.get("serial") is not None else -1
    received_at = _parse_timestamp(row.get("received_at"), params.timezone)
    size_present = (
        bool(row.get("size_present"))
        if "size_present" in row
        else row.get("size") is not None
    )
    classification = (
        str(row.get("event_classification") or row.get("classification") or "NEW")
        .strip()
        .upper()
    )

    return {
        "source": str(row.get("source") or "FUGLE_WEBSOCKET"),
        "symbol": symbol,
        "name": str(row.get("name") or ""),
        "market": market,
        "trade_date": str(row.get("trade_date") or timestamp.date().isoformat()),
        "timestamp": timestamp.isoformat(timespec="microseconds"),
        "timestamp_dt": timestamp,
        "event_time": event_time,
        "event_time_source": (
            "FUGLE_EVENT_TIME" if event_dt is not None else "LEGACY_TIMESTAMP"
        ),
        "serial": serial,
        "received_at": (
            received_at.isoformat(timespec="microseconds")
            if received_at is not None
            else None
        ),
        "price": float(price),
        "size": max(0, _to_int(row.get("size"))),
        "size_present": bool(size_present),
        "volume": max(0, _to_int(row.get("volume"))),
        "bid": _to_float(row.get("bid")),
        "ask": _to_float(row.get("ask")),
        "quote_sequence": max(0, _to_int(row.get("quote_sequence"))),
        "symbol_quote_sequence": max(0, _to_int(row.get("symbol_quote_sequence"))),
        "event_classification": classification,
    }


def _quote_identity(row):
    return {
        key: row.get(key)
        for key in (
            "source",
            "symbol",
            "market",
            "timestamp",
            "event_time",
            "serial",
            "price",
            "size",
            "volume",
            "bid",
            "ask",
        )
    }


def _canonical_quote_digest(rows):
    if isinstance(rows, IncrementalEventBuffer):
        return {"count": rows.total_count, "digest": rows.digest}
    digest = ""
    for row in rows:
        digest = _stable_hash({"previous": digest, "event": _quote_identity(row)})
    return {"count": len(rows), "digest": digest}


def event_buffer_parameter_key(parameters):
    """事件聚合僅依資料時間與時段設定；改清倉時間或策略門檻不清空當日 K 棒。"""
    return _stable_hash({name: getattr(parameters, name) for name in
                        ("timezone", "bar_interval", "vwap_method", "session_start_time", "session_end_time")})


class IncrementalEventBuffer(list):
    """已排序成交事件的增量正規化與分鐘 OHLC；由策略執行緒單獨持有。"""

    def __init__(self, parameters):
        super().__init__()
        self.parameter_key = event_buffer_parameter_key(parameters)
        self.digest = ""
        self.buckets = {}
        self.all_sizes = True
        self.total_count = 0
        self.first_quote = None

    def clone(self):
        out = IncrementalEventBuffer.__new__(IncrementalEventBuffer)
        list.__init__(out, self)
        out.parameter_key, out.digest = self.parameter_key, self.digest
        out.total_count, out.first_quote = self.total_count, self.first_quote
        out.all_sizes = self.all_sizes
        out.buckets = {k: dict(v) for k, v in self.buckets.items()}
        return out

    def append_sequence(self, sequence, parameters):
        for row in normalize_quote_sequence(sequence, parameters=parameters):
            if self and row["timestamp_dt"].date() != self[-1]["timestamp_dt"].date():
                self.clear()
                self.buckets.clear()
                self.digest = ""
                self.all_sizes = True
                self.total_count = 0
                self.first_quote = None
            if self and (row["timestamp_dt"], row.get("serial", -1)) <= (
                self[-1]["timestamp_dt"],
                self[-1].get("serial", -1),
            ):
                continue
            self.append(row)
            self.total_count += 1
            if self.first_quote is None:
                self.first_quote = row
            self.digest = _stable_hash(
                {"previous": self.digest, "event": _quote_identity(row)}
            )
            self.all_sizes = self.all_sizes and bool(row.get("size_present"))
            bucket = _minute_bucket(row["timestamp_dt"])
            price, size = float(row["price"]), max(0, _to_int(row.get("size")))
            item = self.buckets.get(bucket)
            if item is None:
                self.buckets[bucket] = {
                    "start": _iso(bucket),
                    "end": _iso(bucket + timedelta(minutes=1)),
                    "first_timestamp": _iso(row["timestamp_dt"]),
                    "last_timestamp": _iso(row["timestamp_dt"]),
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": size,
                    "price_volume": price * size,
                    "quote_count": 1,
                }
            else:
                item.update(
                    high=max(item["high"], price),
                    low=min(item["low"], price),
                    close=price,
                    volume=item["volume"] + size,
                    price_volume=item["price_volume"] + price * size,
                    quote_count=item["quote_count"] + 1,
                    last_timestamp=_iso(row["timestamp_dt"]),
                )

        if self and self.all_sizes:
            cutoff = self[-1]["timestamp_dt"] - timedelta(minutes=3)
            remove = 0
            for quote in self:
                if quote["timestamp_dt"] >= cutoff:
                    break
                remove += 1
            if remove:
                del self[:remove]

    def minute_bars(self):
        bars, pv, volume = [], 0.0, 0
        for item in self.buckets.values():
            pv += item["price_volume"]
            volume += item["volume"]
            bar = dict(item)
            for key in ("open", "high", "low", "close"):
                bar[key] = _round(bar[key])
            bar.update(
                price_volume=_round(item["price_volume"], 6),
                bar_vwap=_round(
                    item["price_volume"] / item["volume"] if item["volume"] else None
                ),
                vwap=_round(pv / volume if volume else None),
            )
            bars.append(bar)
        _mark_bar_lifecycle(bars)
        return bars, {
            "quote_count": self.total_count,
            "bar_count": len(bars),
            "volume_policy": VWAP_METHOD,
            "first_quote_policy": "FUGLE_TRADE_SIZE_PER_EVENT",
            "volume_resets": 0,
            "volume_input": "FUGLE_TRADE_EVENT_SIZE",
        }


def normalize_quote_sequence(
    quote_sequence: Sequence[Mapping[str, Any]],
    *,
    parameters: Optional[IntradayParameters] = None,
) -> List[Dict[str, Any]]:
    """正規化 Fugle Trade Event Sequence。

    NEW Trade 才能進入策略時間軸；排序只依來源 event_time／serial 與來源序號，
    received_at 與本機執行時間永遠不參與 K 棒歸屬或事件順序。
    """
    params = parameters or IntradayParameters()
    params.validate()

    if isinstance(
        quote_sequence, IncrementalEventBuffer
    ) and quote_sequence.parameter_key == event_buffer_parameter_key(params):
        return quote_sequence

    prepared: List[Dict[str, Any]] = []
    for row in quote_sequence:
        if not isinstance(row, Mapping):
            continue
        item = _canonical_quote(row, params)
        if item is None:
            continue
        classification = str(item.get("event_classification") or "NEW").upper()
        if classification in {"DUPLICATE", "OUT_OF_ORDER", "INVALID"}:
            continue
        prepared.append(item)

    if not prepared:
        return []

    symbols = {(item["market"], item["symbol"]) for item in prepared}
    if len(symbols) != 1:
        raise ValueError(
            f"Fugle Trade Event Sequence 必須只包含單一股票，實際={sorted(symbols)}"
        )

    trade_dates = {item["timestamp_dt"].date() for item in prepared}
    if len(trade_dates) != 1:
        latest_date = max(trade_dates)
        prepared = [
            item for item in prepared if item["timestamp_dt"].date() == latest_date
        ]

    prepared.sort(
        key=lambda item: (
            item["timestamp_dt"],
            int(item.get("serial") or -1),
            int(item.get("symbol_quote_sequence") or 0),
            int(item.get("quote_sequence") or 0),
            _stable_hash(
                {
                    "event_time": item.get("event_time"),
                    "serial": item.get("serial"),
                    "price": item.get("price"),
                    "size": item.get("size"),
                    "volume": item.get("volume"),
                    "bid": item.get("bid"),
                    "ask": item.get("ask"),
                }
            ),
        )
    )

    output: List[Dict[str, Any]] = []
    seen_event_keys: set[Tuple[int, int]] = set()
    seen_legacy: set[str] = set()
    for item in prepared:
        event_time = int(item.get("event_time") or 0)
        serial = int(item.get("serial") or -1)
        if event_time > 0 and serial >= 0:
            key = (event_time, serial)
            if key in seen_event_keys:
                continue
            seen_event_keys.add(key)
        else:
            identity = _stable_hash(
                {
                    "symbol": item.get("symbol"),
                    "market": item.get("market"),
                    "timestamp": item.get("timestamp"),
                    "price": item.get("price"),
                    "size": item.get("size"),
                    "volume": item.get("volume"),
                    "bid": item.get("bid"),
                    "ask": item.get("ask"),
                }
            )
            if identity in seen_legacy:
                continue
            seen_legacy.add(identity)
        output.append(item)
    return output


# =============================================================================
# K 棒與 VWAP
# =============================================================================


def _minute_bucket(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def _volume_deltas(
    quotes: Sequence[Mapping[str, Any]],
    params: IntradayParameters,
) -> Tuple[List[int], Dict[str, Any]]:
    if not quotes:
        return [], {
            "first_quote_policy": "NONE",
            "volume_resets": 0,
            "volume_input": "NO_DATA",
        }

    if all(bool(row.get("size_present")) for row in quotes):
        deltas = [max(0, _to_int(row.get("size"))) for row in quotes]
        return deltas, {
            "first_quote_policy": "FUGLE_TRADE_SIZE_PER_EVENT",
            "volume_resets": 0,
            "volume_input": "FUGLE_TRADE_EVENT_SIZE",
        }

    first_dt = quotes[0]["timestamp_dt"]
    session_start = _session_datetime(
        first_dt.date(), params.session_start_time, params.timezone
    )
    first_is_opening_minute = first_dt < session_start + timedelta(minutes=1)

    deltas: List[int] = []
    resets = 0
    previous: Optional[int] = None
    for index, quote in enumerate(quotes):
        current = max(0, _to_int(quote.get("volume")))
        if index == 0:
            delta = current if first_is_opening_minute else 0
        elif previous is not None and current >= previous:
            delta = current - previous
        else:
            delta = 0
            resets += 1
        deltas.append(max(0, int(delta)))
        previous = current

    return deltas, {
        "first_quote_policy": (
            "USE_CUMULATIVE_VOLUME_FROM_OPENING_MINUTE"
            if first_is_opening_minute
            else "FIRST_DELTA_ZERO_FOR_PARTIAL_SESSION"
        ),
        "volume_resets": resets,
        "volume_input": "LEGACY_CUMULATIVE_VOLUME_DELTA",
    }


def _mark_bar_lifecycle(bars):
    """區間採左閉右開；以最後來源事件作 watermark，不虛構無成交 K 棒。"""
    if not bars:
        return bars
    watermark = max(str(b.get("last_timestamp") or "") for b in bars)
    for bar in bars:
        closed = str(bar["end"]) <= watermark
        bar["closed_bar"] = closed
        bar["in_progress_bar"] = not closed
    return bars


def build_minute_bars(
    quote_sequence: Sequence[Mapping[str, Any]],
    *,
    parameters: Optional[IntradayParameters] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """建立 1 分鐘 K；OHLC 只使用本地序列中的有效成交價，不使用日高日低冒充分鐘高低。"""
    params = parameters or IntradayParameters()
    params.validate()
    quotes = normalize_quote_sequence(quote_sequence, parameters=params)
    if not quotes:
        return [], {
            "quote_count": 0,
            "bar_count": 0,
            "volume_policy": "NO_DATA",
        }

    if isinstance(quotes, IncrementalEventBuffer) and quotes.all_sizes:
        return quotes.minute_bars()

    deltas, volume_info = _volume_deltas(quotes, params)
    buckets: Dict[datetime, Dict[str, Any]] = {}
    for quote, delta_volume in zip(quotes, deltas):
        bucket = _minute_bucket(quote["timestamp_dt"])
        price = float(quote["price"])
        item = buckets.get(bucket)
        pv = price * delta_volume
        if item is None:
            item = {
                "start_dt": bucket,
                "end_dt": bucket + timedelta(minutes=1),
                "first_timestamp_dt": quote["timestamp_dt"],
                "last_timestamp_dt": quote["timestamp_dt"],
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": int(delta_volume),
                "price_volume": float(pv),
                "quote_count": 1,
            }
            buckets[bucket] = item
        else:
            item["last_timestamp_dt"] = quote["timestamp_dt"]
            item["high"] = max(float(item["high"]), price)
            item["low"] = min(float(item["low"]), price)
            item["close"] = price
            item["volume"] = int(item["volume"]) + int(delta_volume)
            item["price_volume"] = float(item["price_volume"]) + float(pv)
            item["quote_count"] = int(item["quote_count"]) + 1

    ordered = [buckets[key] for key in sorted(buckets)]
    cumulative_pv = 0.0
    cumulative_volume = 0
    bars: List[Dict[str, Any]] = []
    for item in ordered:
        cumulative_pv += float(item["price_volume"])
        cumulative_volume += int(item["volume"])
        bar_vwap = (
            float(item["price_volume"]) / int(item["volume"])
            if int(item["volume"]) > 0
            else None
        )
        session_vwap = (
            cumulative_pv / cumulative_volume if cumulative_volume > 0 else None
        )
        bars.append(
            {
                "start": _iso(item["start_dt"]),
                "end": _iso(item["end_dt"]),
                "first_timestamp": _iso(item["first_timestamp_dt"]),
                "last_timestamp": _iso(item["last_timestamp_dt"]),
                "open": _round(item["open"]),
                "high": _round(item["high"]),
                "low": _round(item["low"]),
                "close": _round(item["close"]),
                "volume": int(item["volume"]),
                "bar_vwap": _round(bar_vwap),
                "vwap": _round(session_vwap),
                "price_volume": _round(item["price_volume"], 6),
                "quote_count": int(item["quote_count"]),
            }
        )

    _mark_bar_lifecycle(bars)
    return bars, {
        "quote_count": len(quotes),
        "bar_count": len(bars),
        "volume_policy": VWAP_METHOD,
        **volume_info,
    }


@lru_cache(maxsize=8192)
def _aggregate_group(group_start, interval_minutes, rows):
    """已完成 K 群組按完整內容快取；回傳者須複製，防止共享資料被改寫。"""
    volume = sum(max(0, _to_int(row[4])) for row in rows)
    pv = sum(max(0.0, _to_float(row[5]) or 0.0) for row in rows)
    return {
        "start": _iso(group_start),
        "end": _iso(group_start + timedelta(minutes=interval_minutes)),
        "interval_minutes": int(interval_minutes),
        "open": _round(_to_float(rows[0][0])),
        "high": _round(max(_to_float(row[1]) or 0 for row in rows)),
        "low": _round(min(_to_float(row[2]) or 0 for row in rows)),
        "close": _round(_to_float(rows[-1][3])),
        "volume": volume,
        "bar_vwap": _round(pv / volume if volume > 0 else None),
        "last_timestamp": rows[-1][6],
        "source_bars": len(rows),
    }


def aggregate_bars(
    one_minute_bars: Sequence[Mapping[str, Any]],
    interval_minutes: int,
    *,
    parameters: Optional[IntradayParameters] = None,
) -> List[Dict[str, Any]]:
    """以每日 09:00 為固定錨點聚合 3m / 5m K。"""
    params = parameters or IntradayParameters()
    params.validate()
    if interval_minutes <= 0:
        raise ValueError("interval_minutes 必須 > 0")
    if not one_minute_bars:
        return []

    grouped: Dict[datetime, List[Mapping[str, Any]]] = {}
    for bar in one_minute_bars:
        start_dt = _parse_timestamp(bar.get("start"), params.timezone)
        if start_dt is None:
            continue
        anchor = _session_datetime(
            start_dt.date(), params.session_start_time, params.timezone
        )
        offset_minutes = int((start_dt - anchor).total_seconds() // 60)
        if offset_minutes < 0:
            continue
        group_offset = (offset_minutes // interval_minutes) * interval_minutes
        group_start = anchor + timedelta(minutes=group_offset)
        grouped.setdefault(group_start, []).append(bar)

    output: List[Dict[str, Any]] = []
    for group_start in sorted(grouped):
        rows = sorted(grouped[group_start], key=lambda row: str(row.get("start") or ""))
        frozen = tuple(
            tuple(
                row.get(k)
                for k in (
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "price_volume",
                    "last_timestamp",
                )
            )
            for row in rows
        )
        output.append(dict(_aggregate_group(group_start, interval_minutes, frozen)))
    return output


# =============================================================================
# 指標與盤中結構
# =============================================================================


def _rolling_momentum(
    bars: Sequence[Mapping[str, Any]], periods: int
) -> Optional[float]:
    if len(bars) <= periods:
        return None
    current = _to_float(bars[-1].get("close"))
    base = _to_float(bars[-1 - periods].get("close"))
    return _pct_change(current, base)


def _candidate_reference_timestamp(
    candidate: Optional[Mapping[str, Any]],
    context: Optional[Mapping[str, Any]],
    params: IntradayParameters,
) -> Tuple[Optional[datetime], Optional[str]]:
    """只供診斷：嘗試取得 Candidate 加入／快照時間，不參與任何交易判斷。"""
    time_fields = (
        "candidate_time",
        "candidate_timestamp",
        "added_at",
        "added_time",
        "snapshot_time",
        "generated_at",
        "asof",
        "timestamp",
    )
    for source_name, source in (
        ("candidate", candidate),
        ("candidate_context", context),
    ):
        if not isinstance(source, Mapping):
            continue
        for field_name in time_fields:
            value = source.get(field_name)
            dt = _parse_timestamp(value, params.timezone)
            if dt is not None:
                return dt, f"{source_name}.{field_name}"
    return None, None


def _latest_event_diagnostics(
    normalized_quotes: Sequence[Mapping[str, Any]],
    params: IntradayParameters,
) -> Dict[str, Any]:
    """Event age 只用持久化 received_at - event_time；不讀 datetime.now()。"""
    if not normalized_quotes:
        return {
            "event_time": None,
            "event_serial": None,
            "event_received_at": None,
            "event_age_seconds": None,
            "event_age_basis": "RECEIVED_AT_MINUS_EVENT_TIME",
            "event_quote_sequence": None,
            "event_size": None,
        }

    latest = normalized_quotes[-1]
    event_dt = latest.get("timestamp_dt")
    received_dt = _parse_timestamp(latest.get("received_at"), params.timezone)
    event_age_seconds: Optional[float] = None
    if isinstance(event_dt, datetime) and received_dt is not None:
        delta = (received_dt - event_dt).total_seconds()
        if delta >= 0:
            event_age_seconds = round(delta, 6)

    return {
        "event_time": int(latest.get("event_time") or 0) or None,
        "event_serial": (
            int(latest.get("serial")) if _to_int(latest.get("serial")) >= 0 else None
        ),
        "event_received_at": latest.get("received_at"),
        "event_age_seconds": event_age_seconds,
        "event_age_basis": "RECEIVED_AT_MINUS_EVENT_TIME",
        "event_quote_sequence": int(latest.get("quote_sequence") or 0) or None,
        "event_size": int(latest.get("size") or 0),
    }


def _breakout_age_diagnostics(
    bars: Sequence[Mapping[str, Any]],
    params: IntradayParameters,
) -> Dict[str, Any]:
    """診斷目前連續 breakout_short_high 已維持幾根 1m K；不修改 Breakout 公式。"""
    if not bars:
        return {
            "breakout_start_time": None,
            "breakout_age_bars": None,
            "breakout_reference_high": None,
            "breakout_status": "NO_BAR",
        }

    def breakout_at(index: int) -> Tuple[bool, Optional[float]]:
        if index < 1:
            return False, None
        current_close = _to_float(bars[index].get("close"))
        recent_prior = bars[max(0, index - params.breakout_window) : index]
        short_high = (
            max(_to_float(row.get("high")) or 0.0 for row in recent_prior)
            if recent_prior
            else None
        )
        active = bool(
            current_close is not None
            and short_high is not None
            and short_high > 0
            and current_close > short_high * (1.0 + params.breakout_buffer_pct / 100.0)
        )
        return active, short_high

    latest_index = len(bars) - 1
    latest_active, latest_short_high = breakout_at(latest_index)
    if not latest_active:
        return {
            "breakout_start_time": None,
            "breakout_age_bars": None,
            "breakout_reference_high": _round(latest_short_high),
            "breakout_status": "NO_ACTIVE_BREAKOUT",
        }

    start_index = latest_index
    start_short_high = latest_short_high
    for index in range(latest_index - 1, -1, -1):
        active, short_high = breakout_at(index)
        if not active:
            break
        start_index = index
        start_short_high = short_high

    return {
        "breakout_start_time": bars[start_index].get("start"),
        "breakout_age_bars": latest_index - start_index + 1,
        "breakout_reference_high": _round(start_short_high),
        "breakout_status": "ACTIVE",
    }


def _data_diagnostics(
    quote_sequence: Sequence[Mapping[str, Any]],
    normalized_quotes: Sequence[Mapping[str, Any]],
    bars_1m: Sequence[Mapping[str, Any]],
    metrics: Mapping[str, Any],
    candidate: Optional[Mapping[str, Any]],
    params: IntradayParameters,
    *,
    candidate_context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """建立 Fugle Event -> 1m K -> Momentum readiness 診斷，不參與 ENTRY / EXIT。"""
    first_quote_dt = (
        (
            normalized_quotes.first_quote
            if isinstance(normalized_quotes, IncrementalEventBuffer)
            else normalized_quotes[0]
        ).get("timestamp_dt")
        if normalized_quotes
        else None
    )
    latest_quote_dt = (
        normalized_quotes[-1].get("timestamp_dt") if normalized_quotes else None
    )
    unique_quote_minutes = (
        set(normalized_quotes.buckets)
        if isinstance(normalized_quotes, IncrementalEventBuffer)
        else {
            _minute_bucket(row["timestamp_dt"])
            for row in normalized_quotes
            if isinstance(row.get("timestamp_dt"), datetime)
        }
    )
    expected_bar_count = len(unique_quote_minutes)
    bar_count = len(bars_1m)

    candidate_dt, candidate_time_field = _candidate_reference_timestamp(
        candidate,
        candidate_context,
        params,
    )
    history_before_candidate: Optional[bool] = None
    if isinstance(first_quote_dt, datetime) and candidate_dt is not None:
        if first_quote_dt.date() == candidate_dt.date():
            history_before_candidate = first_quote_dt < candidate_dt

    event_diagnostics = _latest_event_diagnostics(normalized_quotes, params)
    breakout_diagnostics = _breakout_age_diagnostics(bars_1m, params)

    return {
        "input_quote_count": (
            quote_sequence.total_count
            if isinstance(quote_sequence, IncrementalEventBuffer)
            else len(quote_sequence)
        ),
        "quote_count": (
            normalized_quotes.total_count
            if isinstance(normalized_quotes, IncrementalEventBuffer)
            else len(normalized_quotes)
        ),
        "bar_count_1m": bar_count,
        "expected_bar_count_1m": expected_bar_count,
        "bar_aggregation_matches_quote_minutes": bar_count == expected_bar_count,
        "first_quote_time": _iso(first_quote_dt),
        "latest_quote_time": _iso(latest_quote_dt),
        "candidate_reference_time": _iso(candidate_dt),
        "candidate_reference_time_field": candidate_time_field,
        "history_before_candidate": history_before_candidate,
        "history_before_candidate_status": (
            "YES"
            if history_before_candidate is True
            else "NO" if history_before_candidate is False else "UNKNOWN"
        ),
        "momentum_1m_ready": bool(metrics.get("momentum_1m_ready")),
        "momentum_3m_ready": bool(metrics.get("momentum_3m_ready")),
        "momentum_5m_ready": bool(metrics.get("momentum_5m_ready")),
        "bar_time_basis": "FUGLE_EVENT_TIME",
        **event_diagnostics,
        **breakout_diagnostics,
    }


# =============================================================================
# Entry / Exit 純函式核心
# =============================================================================


def _entry_time_condition(asof: Optional[datetime], params: IntradayParameters) -> bool:
    if asof is None:
        return False
    start = _session_datetime(asof.date(), params.entry_start_time, params.timezone)
    end = _session_datetime(asof.date(), params.entry_end_time, params.timezone)
    return start <= asof <= end


def _position_identity(
    position: Optional[Mapping[str, Any]],
) -> Tuple[str, str]:
    """Position 一旦存在，symbol / market 可直接由持倉本身解析，不依賴 Candidate。"""
    if not isinstance(position, Mapping):
        return "", ""
    symbol = str(
        position.get("symbol") or position.get("stock_no") or position.get("code") or ""
    ).strip()
    market = _normalize_market(position.get("market") or position.get("exchange") or "")
    return symbol, market




def _normalize_position(
    position: Optional[Mapping[str, Any]],
    params: IntradayParameters,
) -> Optional[Dict[str, Any]]:
    if not isinstance(position, Mapping):
        return None
    entry_price = _to_float(position.get("entry_price"))
    if entry_price is None or entry_price <= 0:
        return None
    emergency = _to_float(position.get("emergency_stop_price")) or entry_price * (
        1 - params.emergency_stop_loss_pct / 100
    )
    stop_price = _to_float(position.get("strategy_stop_price")) or emergency
    entry_time = _parse_timestamp(position.get("entry_time"), params.timezone)
    symbol, market = _position_identity(position)
    return {
        "symbol": symbol,
        "market": market,
        "entry_price": float(entry_price),
        "stop_price": float(stop_price),
        "take_profit_price": None,
        "entry_time": entry_time.isoformat() if entry_time is not None else None,
    }


def _post_entry_exit_range(
    bars: Sequence[Mapping[str, Any]],
    quotes: Sequence[Mapping[str, Any]],
    entry_time: Optional[datetime],
    params: IntradayParameters,
) -> Dict[str, Any]:
    """建立 EXIT 價格邊界；進場同分鐘只採 entry_time 之後的 Quote。"""
    if not bars:
        return {
            "high": None,
            "low": None,
            "source": "NO_BAR",
            "post_entry_quote_count": 0,
        }
    if entry_time is None:
        return {
            "high": None,
            "low": None,
            "source": "MISSING_ENTRY_TIME",
            "post_entry_quote_count": 0,
        }

    latest = bars[-1]
    latest_start = _parse_timestamp(latest.get("start"), params.timezone)
    if latest_start is None:
        return {
            "high": None,
            "low": None,
            "source": "INVALID_BAR_TIME",
            "post_entry_quote_count": 0,
        }

    entry_bucket = _minute_bucket(entry_time)
    if latest_start < entry_bucket:
        return {
            "high": None,
            "low": None,
            "source": "BAR_BEFORE_ENTRY",
            "post_entry_quote_count": 0,
        }

    if latest_start == entry_bucket:
        post_entry_prices = [
            float(price)
            for row in quotes
            if isinstance(row.get("timestamp_dt"), datetime)
            and row["timestamp_dt"] > entry_time
            and _minute_bucket(row["timestamp_dt"]) == entry_bucket
            and (price := _to_float(row.get("price"))) is not None
        ]
        if not post_entry_prices:
            return {
                "high": None,
                "low": None,
                "source": "SAME_MINUTE_NO_POST_ENTRY_QUOTE",
                "post_entry_quote_count": 0,
            }
        return {
            "high": max(post_entry_prices),
            "low": min(post_entry_prices),
            "source": "POST_ENTRY_QUOTES_SAME_MINUTE",
            "post_entry_quote_count": len(post_entry_prices),
        }

    return {
        "high": _to_float(latest.get("high")),
        "low": _to_float(latest.get("low")),
        "source": "FULL_BAR_AFTER_ENTRY",
        "post_entry_quote_count": int(latest.get("quote_count") or 0),
    }


def _post_fill_evidence(bars, quotes, position, market, params):
    """僅由成交後事件建立結構證據；歷史指標仍可當門檻，但不能單獨觸發 SELL。"""
    filled_at = _parse_timestamp(position.get("entry_time"), params.timezone)
    evidence = {"fill_time": position.get("entry_time"), "bar_count": 0,
                "source_at": None, "metrics": {}, "market_after_fill": False,
                "rs_deteriorated": False, "status": "MISSING_FILL_TIME"}
    if filled_at is None:
        return evidence
    # 分形、量能與跨 K 動能只採起點晚於成交的分鐘 K；混合分鐘不參與結構。
    after = [dict(b) for b in bars
             if (start := _parse_timestamp(b.get("start"), params.timezone)) is not None
             and start > filled_at]
    points = [q for q in quotes if q["timestamp_dt"] > filled_at]
    if not after and not points:
        evidence["status"] = "NO_POST_FILL_EVENT"
        return evidence
    identity = {"symbol": str(position.get("symbol")) + ":EXIT:" + filled_at.isoformat(),
                "market": position.get("market")}
    post_m, _, fresh = _dual_strategy(after, market, identity, params)
    evidence.update(bar_count=len(after),
                    source_at=(after[-1].get("last_timestamp") if after else points[-1]["timestamp"]),
                    metrics=fresh, post_pullback_low=post_m.get("pullback_low"),
                    status="POST_FILL_EVIDENCE")
    # EMA 保留原本歷史 EMA 門檻，另外要求成交後價格相對成交均價確有轉弱。
    fast = slow = float(position["entry_price"])
    closes = [float(bar["close"]) for bar in after] or [float(points[-1]["price"])]
    for close in closes:
        fast += 2 / (params.ema_fast_period + 1) * (close - fast)
        slow += 2 / (params.ema_slow_period + 1) * (close - slow)
    evidence["ema_post_fill_bearish"] = fast < slow
    times = [(market.get(name) or {}).get("timestamp") for name in ("taiex", "tpex")]
    breadth_times = market.get("breadth_snapshot_acquired_at") or {}
    times.extend(breadth_times.get(name) for name in ("TWSE", "TPEX"))
    evidence["market_after_fill"] = all(
        (stamp := _parse_timestamp(value, params.timezone)) is not None and stamp > filled_at
        for value in times)
    index = market.get("tpex" if position.get("market") == "TPEX" else "taiex") or {}
    index_time = _parse_timestamp(index.get("timestamp"), params.timezone)
    entry_index = _to_float(position.get("entry_index_change_pct"))
    current_index = _to_float(index.get("change_pct"))
    if entry_index is not None and current_index is not None and index_time and index_time > filled_at:
        relative_move = (closes[-1] / float(position["entry_price"]) - 1) * 100 - (
            (100 + current_index) / (100 + entry_index) - 1) * 100
        evidence["relative_move_since_fill"] = relative_move
        # 指數基準取自進場快照；另需成交後個股跌價，避免基準至成交間的指數變動冒充新弱勢。
        evidence["rs_deteriorated"] = relative_move < -0.5 and (
            closes[-1] < float(position["entry_price"])
            or (fresh.get("momentum_5m") or 0) < 0)
    return evidence


_STRATEGY_STATE_CACHE = OrderedDict()
_STRATEGY_STATE_CACHE_LOCK = Lock()


def _dual_strategy(bars, market, candidate, params):
    """依事件時間重播兩套獨立狀態機；只對已存在的分鐘資料計算，不回看未來。"""
    candidate = candidate or {}
    momentum = {"state": "WAITING", "wave": 0, "pullbacks": 0}
    trend = {"state": "WAITING"}
    fast = slow = previous_fast = None
    lows, highs = [], []
    last = {}
    records = tuple(
        tuple(
            bar.get(k)
            for k in (
                "start",
                "last_timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "vwap",
            )
        )
        for bar in bars
    )
    key = (
        str(candidate.get("symbol") or ""),
        str(candidate.get("market") or ""),
        _stable_hash(params.snapshot()),
    )
    begin = 0

    def capture():
        return (
            dict(momentum),
            dict(trend),
            fast,
            slow,
            previous_fast,
            list(lows),
            list(highs),
            dict(last),
        )

    with _STRATEGY_STATE_CACHE_LOCK:
        cached = _STRATEGY_STATE_CACHE.get(key)
    if cached:
        old_records, before, end = cached
        n = len(old_records)
        if n and len(records) >= n and records[:n] == old_records:
            momentum, trend, fast, slow, previous_fast, lows, highs, last = end
            begin = n
        elif n and len(records) >= n and records[: n - 1] == old_records[:-1]:
            momentum, trend, fast, slow, previous_fast, lows, highs, last = before
            begin = n - 1
        momentum = dict(momentum)
        trend = dict(trend)
        lows = list(lows)
        highs = list(highs)
        last = dict(last)
    before_last = cached[1] if cached and begin == len(records) else capture()
    for i in range(begin, len(bars)):
        bar = bars[i]
        if i == len(bars) - 1:
            before_last = capture()
        close = float(bar["close"])
        low, high = float(bar["low"]), float(bar["high"])
        vwap = _to_float(bar.get("vwap"))
        previous_fast = fast
        fast = (
            close
            if fast is None
            else fast + 2 / (params.ema_fast_period + 1) * (close - fast)
        )
        slow = (
            close
            if slow is None
            else slow + 2 / (params.ema_slow_period + 1) * (close - slow)
        )
        prior = bars[max(0, i - params.volume_window) : i]
        av = (
            sum(float(b.get("volume") or 0) for b in prior) / len(prior) if prior else 0
        )
        ar = (
            sum(float(b["high"]) - float(b["low"]) for b in prior) / len(prior)
            if prior
            else 0
        )
        ve = float(bar.get("volume") or 0) / av if av else None
        re = (high - low) / ar if ar else None
        m1 = (close / float(bars[i - 1]["close"]) - 1) * 100 if i else None
        m3 = (close / float(bars[i - 3]["close"]) - 1) * 100 if i >= 3 else None
        # 分形只在右側 K 已出現後確認；不使用尚未發生的高低點。
        if i >= 2:
            a, b, c = bars[i - 2 : i + 1]
            if float(b["low"]) < float(a["low"]) and float(b["low"]) <= float(c["low"]):
                lows.append(float(b["low"]))
                lows = lows[-2:]
            if float(b["high"]) > float(a["high"]) and float(b["high"]) >= float(
                c["high"]
            ):
                highs.append(float(b["high"]))
                highs = highs[-2:]
        hl = len(lows) == 2 and lows[-1] > lows[-2]
        hh = len(highs) == 2 and highs[-1] > highs[-2]
        shape = bool(
            i + 1 >= params.ema_slow_period
            and vwap
            and close > vwap
            and fast > slow
            and previous_fast is not None
            and fast > previous_fast
            and hl
            and hh
        )
        mature_shape = shape
        window = bars[max(0, i-params.persistent_bars+1):i+1]
        rolling_hh = len(window) == params.persistent_bars and all(float(b["high"]) > float(a["high"]) for a,b in zip(window,window[1:]))
        rolling_hl = len(window) == params.persistent_bars and all(float(b["low"]) > float(a["low"]) for a,b in zip(window,window[1:]))
        early_shape = bool(i+1 >= max(params.min_bars, params.trend_initial_bars) and rolling_hh and rolling_hl
                           and vwap and close > vwap and fast > slow and previous_fast is not None and fast > previous_fast)
        shape = mature_shape or early_shape
        trend_anchor = lows[-1] if mature_shape else min(float(b["low"]) for b in window)
        reference = max(
            (float(b["high"]) for b in bars[max(0, i - params.breakout_window) : i]),
            default=0,
        )
        volume_ok = ve is not None and ve >= params.volume_expansion_min
        range_ok = re is not None and re >= params.range_expansion_min
        above = bool(vwap and close > vwap)
        vwap_up = bool(
            i
            and vwap
            and _to_float(bars[i - 1].get("vwap")) is not None
            and vwap > float(bars[i - 1]["vwap"])
        )
        # 持續量能採本次連續推升開始前的固定觀察窗，不以推升中的高量墊高分母。
        run_start = i
        while run_start > 0 and float(bars[run_start]["close"]) > float(bars[run_start-1]["close"]):
            run_start -= 1
        baseline = bars[max(0,run_start-params.volume_window):run_start]
        if not baseline and run_start == 0 and i+1 >= params.min_bars and i+1 >= 2*params.persistent_bars:
            baseline = bars[:params.persistent_bars]
        baseline_volume = sum(float(b.get("volume") or 0) for b in baseline)/len(baseline) if baseline else None
        sustained_volume = min(float(b.get("volume") or 0) for b in window)/baseline_volume if baseline_volume else None
        persistent = bool(rolling_hh and rolling_hl and all(float(b["close"]) > float(b["open"]) for b in window)
                          and sustained_volume is not None and sustained_volume >= params.persistent_volume_min
                          and above and vwap_up)
        initial_gates = {
            "breakout": {"value": close, "threshold": reference*(1+params.breakout_buffer_pct/100), "ok": bool(reference and close > reference*(1+params.breakout_buffer_pct/100))},
            "volume": {"value": ve, "threshold": params.volume_expansion_min, "ok": volume_ok},
            "range": {"value": re, "threshold": params.range_expansion_min, "ok": range_ok},
            "vwap": {"value": close, "threshold": vwap, "ok": above},
            "vwap_slope": {"ok": vwap_up},
            "persistent": {"ok": persistent, "bars": len(window), "higher_high": rolling_hh, "higher_low": rolling_hl,
                           "volume_ratio": sustained_volume, "threshold": params.persistent_volume_min,
                           "baseline_start": baseline[0].get("start") if baseline else None}}
        trend_gates = {"bars": i+1, "mature_bars": params.ema_slow_period, "initial_bars": params.trend_initial_bars,
                       "above_vwap": above, "ema_fast_above_slow": fast > slow,
                       "ema_slope_up": previous_fast is not None and fast > previous_fast,
                       "fractal_hh": hh, "fractal_hl": hl, "rolling_hh": rolling_hh, "rolling_hl": rolling_hl,
                       "early_ready": early_shape, "mature_ready": mature_shape}
        setup_m = setup_t = ""
        if i >= params.min_bars - 1:
            terminal = {"MOMENTUM_DECAY", "STRUCTURE_BROKEN", "TTL_EXPIRED"}
            origin = _parse_timestamp(momentum.get("start_time"), params.timezone)
            event_at = _parse_timestamp(bar.get("last_timestamp"), params.timezone)
            if (momentum["state"] not in terminal | {"WAITING"}
                    and (i - momentum.get("start", i) > params.max_entry_wave_bars
                         or (origin and event_at and (event_at-origin).total_seconds() > params.momentum_setup_ttl_seconds))):
                momentum.update(state="TTL_EXPIRED", invalidated_at=i, reset_seen=False)
            if momentum["state"] in terminal:
                momentum.setdefault("invalidated_at", i)
                if m1 is not None and m1 < 0:
                    momentum["reset_seen"] = True
            # 獨立新波必須先有回落／整理及完整新觀察窗，不能替同一段連漲洗掉 Age。
            restart_ready = (momentum["state"] in terminal and momentum.get("reset_seen")
                             and i - momentum.get("invalidated_at", i) >= params.breakout_window)
            if (
                (momentum["state"] == "WAITING" or restart_ready)
                and reference
                and close > reference * (1 + params.breakout_buffer_pct / 100)
                and ((volume_ok and range_ok) or persistent)
                and above
                and vwap_up
            ):
                next_wave = momentum["wave"] + 1
                momentum.clear()
                momentum.update(
                    state="INITIAL_BREAKOUT",
                    wave=next_wave,
                    pullbacks=0,
                    anchor=reference,
                    stop=min(low, reference) * (1 - params.structure_buffer_pct / 100),
                    start=i,
                    start_time=bar.get("start"),
                )
                setup_m = "INITIAL_BREAKOUT" if volume_ok and range_ok else "PERSISTENT_MOMENTUM_START"
            elif momentum["state"] in {"INITIAL_BREAKOUT", "MOMENTUM_RUN", "REBREAK"}:
                if low <= momentum["stop"]:
                    momentum["state"] = "STRUCTURE_BROKEN"
                elif m1 is not None and m1 < 0:
                    momentum["pullbacks"] += 1
                    if (
                        momentum["pullbacks"] == 1
                        and ve is not None
                        and (ve <= params.pullback_volume_max or (
                            ve <= params.healthy_pullback_volume_max and abs(m1) <= params.healthy_pullback_pct
                            and m3 is not None and m3 > 0 and close > momentum["anchor"]))
                        and above
                    ):
                        momentum.update(
                            state="FIRST_PULLBACK", pullback_high=high, pullback_low=low
                        )
                    else:
                        momentum["state"] = "MOMENTUM_DECAY"
                else:
                    momentum["state"] = "MOMENTUM_RUN"
                    if (momentum.get("pullbacks", 0) == 0 and m1 is not None and m1 > 0
                            and above and vwap_up and close > momentum["anchor"]
                            and low > momentum["stop"]):
                        setup_m = "MOMENTUM_CONTINUATION"
            elif momentum["state"] == "FIRST_PULLBACK":
                if low <= momentum["stop"] or not above:
                    momentum["state"] = "STRUCTURE_BROKEN"
                elif close > momentum["pullback_high"] and volume_ok:
                    momentum.update(
                        state="REBREAK",
                        stop=momentum["pullback_low"]
                        * (1 - params.structure_buffer_pct / 100),
                    )
                    setup_m = "FIRST_PULLBACK_REBREAK"
                else:
                    momentum["pullback_low"] = min(momentum["pullback_low"], low)
                    momentum["pullback_high"] = max(momentum["pullback_high"], high)
            if momentum["state"] in terminal:
                momentum.setdefault("invalidated_at", i)
                if m1 is not None and m1 < 0:
                    momentum["reset_seen"] = True
            if trend["state"] == "WAITING" and shape:
                trend.update(
                    state="TREND_CONFIRMED",
                    anchor=trend_anchor,
                    stop=trend_anchor * (1 - params.structure_buffer_pct / 100),
                    start=i,
                    start_time=bar.get("start"),
                )
            elif trend["state"] in {"TREND_CONFIRMED", "TREND_RUN", "TREND_RESUME"}:
                if low <= trend["stop"] or fast < slow:
                    trend["state"] = "TREND_BROKEN"
                elif (
                    low <= max(fast, vwap or fast)
                    and ve is not None
                    and ve <= params.pullback_volume_max
                ):
                    trend.update(state="PULLBACK", pullback_high=high, pullback_low=low)
                elif trend["state"] == "TREND_RESUME":
                    trend["state"] = "TREND_RUN"
            elif trend["state"] == "PULLBACK":
                if low <= trend["stop"] or fast < slow:
                    trend["state"] = "TREND_BROKEN"
                elif (
                    close > trend["pullback_high"]
                    and above
                    and fast > slow
                    and fast > previous_fast
                ):
                    trend["state"] = "TREND_RESUME"
                    setup_t = "TREND_RESUME"
                else:
                    trend["pullback_high"] = max(trend["pullback_high"], high)
                    trend["pullback_low"] = min(trend["pullback_low"], low)
            elif trend["state"] == "TREND_BROKEN" and shape:
                trend.update(
                    state="TREND_CONFIRMED",
                    anchor=trend_anchor,
                    stop=trend_anchor * (1 - params.structure_buffer_pct / 100),
                    start=i,
                    start_time=bar.get("start"),
                )
            # 首次完整結構確認即可提出進場；回踩分支仍優先並沿用 TREND_RESUME。
            if trend["state"] == "TREND_CONFIRMED" and shape:
                setup_t = "TREND_INITIAL_ENTRY"
        last = dict(
            ema5=fast,
            ema20=slow,
            ema_fast_slope=None if previous_fast is None else fast - previous_fast,
            volume_expansion_ratio=ve,
            range_expansion_ratio=re,
            momentum_1m=m1,
            momentum_3m=m3,
            higher_high=hh,
            higher_low=hl,
            last_higher_low=lows[-1] if hl else None,
            lower_high=len(highs) == 2 and highs[-1] < highs[-2],
            lower_low=len(lows) == 2 and lows[-1] < lows[-2],
            trend_structure_valid=shape,
            trend_structure_mature=mature_shape,
            momentum_initial_gates=initial_gates,
            trend_initial_gates=trend_gates,
            setup_m=setup_m,
            setup_t=setup_t,
            vwap=vwap,
            price=close,
            price_above_vwap=above,
            vwap_slope_up=vwap_up,
            first_pullback=momentum["pullbacks"] == 1,
            pullback_volume_contraction=ve is not None
            and ve <= params.pullback_volume_max,
            breakout_count=momentum["wave"],
            momentum_wave_number=momentum["wave"],
            rebreak=setup_m == "FIRST_PULLBACK_REBREAK",
        )
    with _STRATEGY_STATE_CACHE_LOCK:
        _STRATEGY_STATE_CACHE[key] = (records, before_last, capture())
        _STRATEGY_STATE_CACHE.move_to_end(key)
        while len(_STRATEGY_STATE_CACHE) > 128:
            _STRATEGY_STATE_CACHE.popitem(last=False)
    return momentum, trend, last


def prune_calculation_cache():
    """每分鐘由交易控制序列清理可重算快取；不清除持倉或訊號去重狀態。"""
    _aggregate_group.cache_clear()
    with _STRATEGY_STATE_CACHE_LOCK:
        while len(_STRATEGY_STATE_CACHE) > 64:
            _STRATEGY_STATE_CACHE.popitem(last=False)


def calculate_signal_context(
    quote_sequence,
    market_context,
    candidate,
    *,
    parameters=None,
    position=None,
    include_aggregates=True,
    now=None,
):
    """雙策略共用事件資料，持倉策略身分鎖定；市場與候選限制只管新進場。"""
    params = parameters or IntradayParameters()
    params.validate()
    quotes = normalize_quote_sequence(quote_sequence, parameters=params)
    bars, info = build_minute_bars(quotes, parameters=params)
    sector_now = now or _parse_timestamp(market_context.get("permission_evaluated_at"), params.timezone)
    if sector_now is not None:
        candidate = load_market_module().refresh_candidate_sector(candidate, market_context,
            now=sector_now, quote=quotes[-1] if quotes else None)
    m, t, metrics = _dual_strategy(bars, market_context, candidate or position, params)
    pos = dict(position or {})
    candidate = dict(candidate or {})
    index_context = (
        market_context.get("tpex" if str(pos.get("market")) == "TPEX" else "taiex")
        or {}
    )
    # 更新：保留市場許可證據，避免只傳布林值造成 Trading 無法核對來源。
    market_context = {key: market_context.get(key) for key in (
        "market_state", "auto_market_state", "panic_reason", "market_score",
        "trading_mode", "allow_long", "intraday_trend", "momentum_long_allowed",
        "trend_long_allowed", "strategy_market_permissions", "parameters",
        "data_fingerprint", "parameter_fingerprint", "asof", "breadth",
        "taiex", "tpex", "realtime_buy_data_ready", "session_state", "market_snapshot",
        "breadth_snapshot_acquired_at", "permission_evaluated_at", "session_trade_date",
        "market_permission_snapshot_id", "market_guard", "sector_snapshots",
        "sector_identities", "sector_permission_snapshot_id", "sector_snapshot_max_age_seconds",
    )}
    metrics["momentum_5m"] = _rolling_momentum(bars, params.momentum_5m)
    active = bool(
        _to_float(pos.get("entry_price"))
        and _to_int(pos.get("managed_quantity", pos.get("quantity"))) > 0
    )
    strategy = str(pos.get("strategy_type") or "UNKNOWN") if active else "MOMENTUM"
    ready = candidate.get("trade_ready") is True
    rs = _to_float(candidate.get("market_relative_strength"))
    permitted_m = (
        market_context.get("momentum_long_allowed") is True
        and market_context.get("market_state") != "PANIC"
    )
    permission_time = (now or _parse_timestamp(market_context.get("permission_evaluated_at"), params.timezone)
                       or _parse_timestamp((bars[-1] if bars else {}).get("last_timestamp"), params.timezone)
                       or datetime(1970, 1, 1, tzinfo=_taipei_timezone()))
    trend_permission = load_market_module().strategy_market_permission(market_context, now=permission_time)
    permitted_t = trend_permission["allowed"]
    path_time = _parse_timestamp((bars[-1] if bars else {}).get("last_timestamp"), params.timezone)

    def describe_path(name, node, raw_setup, market_allowed):
        eligible = candidate.get("momentum_eligible" if name == "MOMENTUM" else "trend_eligible") is True
        path_age = len(bars) - 1 - int(node.get("start", len(bars) - 1))
        origin = _parse_timestamp(node.get("start_time"), params.timezone)
        path_ttl = params.momentum_setup_ttl_seconds if name == "MOMENTUM" else params.trend_anchor_timeout_seconds
        reasons = []
        if not raw_setup:
            reasons.append({"WAITING": "WAITING_FOR_BREAKOUT" if name == "MOMENTUM" else "WAITING_FOR_TREND_STRUCTURE",
                            "MOMENTUM_RUN": "CONTINUATION_PRICE_OR_VWAP_NOT_CONFIRMED",
                            "FIRST_PULLBACK": "WAITING_FOR_FIRST_PULLBACK_REBREAK",
                            "PULLBACK": "WAITING_FOR_TREND_RESUME",
                            "TREND_CONFIRMED": "TREND_STRUCTURE_NOT_CONFIRMED"}.get(node["state"], node["state"]))
        if not market_allowed:
            reasons.append("MOMENTUM_MARKET_INVALID" if name == "MOMENTUM" else trend_permission["reason"])
        if not eligible:
            reasons.extend(candidate.get("momentum_eligibility_reasons" if name == "MOMENTUM" else "trend_eligibility_reasons") or ["CANDIDATE_STRATEGY_INELIGIBLE"])
        if name == "TREND":
            sector_reason = load_market_module().candidate_sector_gate(candidate, now=permission_time)
            if sector_reason:
                reasons.append(sector_reason)
                eligible = False
        if not ready: reasons.append("CANDIDATE_NOT_TRADE_READY")
        if rs is None or rs <= (params.momentum_rs_min if name == "MOMENTUM" else params.trend_rs_min): reasons.append("MARKET_RS_NOT_STRONG")
        if active: reasons.append("POSITION_ALREADY_ACTIVE")
        if len(bars) < params.min_bars: reasons.append("MIN_BARS_NOT_READY")
        if not _entry_time_condition(path_time, params): reasons.append("OUTSIDE_ENTRY_TIME")
        if origin and path_time and (path_time-origin).total_seconds() > path_ttl: reasons.append("SETUP_TTL_EXPIRED")
        if name == "MOMENTUM" and path_age > params.max_entry_wave_bars: reasons.append("ENTRY_WAVE_TOO_OLD")
        return {"eligible": eligible, "trade_ready": ready, "state": node["state"], "setup": raw_setup or "",
                "wave_age_bars": path_age, "wave_anchor_price": node.get("anchor"), "wave_start_time": node.get("start_time"), "entry_allowed": not reasons, "entry_signal": False,
                "rejection_reasons": list(dict.fromkeys(reasons)),
                "formation_gates": metrics.get("momentum_initial_gates" if name == "MOMENTUM" else "trend_initial_gates", {})}

    entry_paths = {
        "MOMENTUM": describe_path("MOMENTUM", m, metrics.get("setup_m"), permitted_m),
        "TREND": describe_path("TREND", t, metrics.get("setup_t"), permitted_t),
    }
    setup = ""
    selected = m
    if not active:
        if entry_paths["MOMENTUM"]["entry_allowed"]:
            strategy = "MOMENTUM"
        elif entry_paths["TREND"]["entry_allowed"]:
            strategy = "TREND"
        elif metrics.get("setup_t") and not metrics.get("setup_m"):
            strategy = "TREND"
        elif t["state"] != "WAITING" and (m["state"] == "WAITING" or (candidate.get("trend_eligible") is True and candidate.get("momentum_eligible") is not True)):
            strategy = "TREND"
    selected = t if strategy == "TREND" else m
    setup = entry_paths.get(strategy, {}).get("setup", "") if not active else ""
    latest = bars[-1] if bars else {}
    asof = _parse_timestamp(latest.get("last_timestamp"), params.timezone)
    price = _to_float(latest.get("close"))
    if (
        active
        and rs is None
        and price
        and _to_float(pos.get("prev_close"))
        and _to_float(index_context.get("change_pct")) is not None
    ):
        rs = (price / float(pos["prev_close"]) - 1) * 100 - float(
            index_context["change_pct"]
        )
    symbol = str(
        pos.get("symbol")
        or candidate.get("symbol")
        or (quotes[-1].get("symbol") if quotes else "")
    )
    market = _normalize_market(
        pos.get("market")
        or candidate.get("market")
        or (quotes[-1].get("market") if quotes else "")
    )
    age = len(bars) - 1 - int(selected.get("start", len(bars) - 1))
    entry = bool(not active and entry_paths.get(strategy, {}).get("entry_allowed"))
    anchor_time = _parse_timestamp(selected.get("start_time"), params.timezone)
    ttl = params.momentum_setup_ttl_seconds if strategy == "MOMENTUM" else params.trend_anchor_timeout_seconds
    if not active and (len(bars) < params.min_bars or
            (asof and anchor_time and (asof-anchor_time).total_seconds() > ttl)):
        entry = False
    if strategy == "MOMENTUM" and age > params.max_entry_wave_bars:
        entry = False
    blockers = entry_paths.get(strategy, {}).get("rejection_reasons", ["UNKNOWN_POSITION_STRATEGY"])
    for name, path in entry_paths.items():
        path["selected"] = name == strategy
        path["entry_signal"] = bool(entry and name == strategy)
        if path["entry_allowed"] and name != strategy:
            path["rejection_reasons"] = ["OTHER_STRATEGY_SELECTED"]
    stop = _to_float(pos.get("strategy_stop_price")) if active else selected.get("stop")
    emergency = (
        _to_float(pos.get("emergency_stop_price"))
        if active
        else (price * (1 - params.emergency_stop_loss_pct / 100) if price else None)
    )
    if active and emergency is None:
        emergency = float(pos["entry_price"]) * (
            1 - params.emergency_stop_loss_pct / 100
        )
    exit_stage = ""
    fraction = 0.0
    exit_range = (
        _post_entry_exit_range(
            bars,
            quotes,
            _parse_timestamp(pos.get("entry_time"), params.timezone),
            params,
        )
        if active
        else {}
    )
    evidence = _post_fill_evidence(bars, quotes, pos, market_context, params) if active else {}
    fresh = evidence.get("metrics") or {}
    lo = _to_float(exit_range.get("low"))
    post = lo is not None
    if active and post:
        if emergency and lo <= emergency:
            exit_stage = "EMERGENCY_STOP"
        elif asof and asof >= _session_datetime(
            asof.date(), params.exit_time, params.timezone
        ):
            exit_stage = "TIME_EXIT"
        elif strategy == "MOMENTUM":
            structure = _to_float(evidence.get("post_pullback_low"))
            boundary = (
                max(x for x in (stop, structure) if x is not None)
                if stop or structure
                else None
            )
            if (
                (boundary and lo <= boundary)
                or (fresh.get("lower_high") and fresh.get("lower_low"))
                or (
                    metrics.get("price_above_vwap") is False
                    and (fresh.get("momentum_1m") or 0) < 0
                    and (fresh.get("momentum_3m") or 0) < 0
                    and (fresh.get("momentum_5m") or 0) < 0
                )
            ):
                exit_stage = "MOMENTUM_STRUCTURE_BREAK_FULL"
            elif (
                not pos.get("partial_exit_done")
                and (fresh.get("momentum_1m") or 0) < 0
                and (fresh.get("momentum_3m") or 0) < 0
                and fresh.get("volume_expansion_ratio") is not None
                and fresh["volume_expansion_ratio"]
                <= params.momentum_decay_volume_max
                and fresh.get("range_expansion_ratio") is not None
                and fresh["range_expansion_ratio"] < 1
            ):
                exit_stage = "MOMENTUM_DECAY_PARTIAL"
                fraction = params.momentum_partial_exit_fraction
        elif strategy == "TREND":
            hl = fresh.get("last_higher_low")
            boundary = (
                max(x for x in (stop, hl) if x is not None) if stop or hl else None
            )
            if boundary and lo <= boundary:
                exit_stage = "HIGHER_LOW_BREAK"
            elif (metrics.get("ema5", 0) < metrics.get("ema20", 0)
                  and evidence.get("ema_post_fill_bearish")):
                exit_stage = "EMA_TREND_BREAK"
            elif evidence.get("market_after_fill") and not permitted_t and trend_permission["reason"] in {
                "PANIC", "MARKET_WEAK", "MARKET_LONG_BLOCKED", "MARKET_NOT_STRONG"
            }:
                exit_stage = "MARKET_TREND_INVALIDATED"
            elif rs is not None and rs <= params.trend_rs_min and evidence.get("rs_deteriorated"):
                exit_stage = "RS_FAILURE"
        elif stop and lo <= stop:
            exit_stage = "UNKNOWN_STRATEGY_STOP"
    # entry_price 由 Trading 依 CTS 實際平均成交成本建立；不以訊號價或昨收替代。
    average_fill_price = _to_float(pos.get("entry_price")) if active else None
    position_return_pct = (
        (price / average_fill_price - 1) * 100
        if price is not None and average_fill_price is not None and average_fill_price > 0
        else None
    )
    custom_take_profit_pct = (
        params.momentum_take_profit_pct if strategy == "MOMENTUM"
        else params.trend_take_profit_pct if strategy == "TREND" else 0.0
    )
    custom_take_profit_price = (
        average_fill_price * (1 + custom_take_profit_pct / 100)
        if average_fill_price is not None and custom_take_profit_pct > 0 else None
    )
    # 只用成交後有效行情；原有全數風險出場優先，自訂全數停利優先於部分減碼。
    if (active and post and custom_take_profit_pct > 0
            and position_return_pct is not None
            and (position_return_pct >= custom_take_profit_pct
                 or math.isclose(position_return_pct, custom_take_profit_pct, rel_tol=0, abs_tol=1e-10))
            and exit_stage in {"", "MOMENTUM_DECAY_PARTIAL"}):
        exit_stage = ("MOMENTUM_CUSTOM_TAKE_PROFIT" if strategy == "MOMENTUM"
                      else "TREND_CUSTOM_TAKE_PROFIT")
        fraction = 1.0
    if exit_stage and fraction == 0:
        fraction = 1.0
    state = selected["state"]
    if active and not exit_stage:
        state = (
            "MOMENTUM_RUN"
            if strategy == "MOMENTUM"
            else "TREND_RUN" if strategy == "TREND" else "UNKNOWN"
        )
    data = _data_diagnostics(quote_sequence, quotes, bars, metrics, candidate, params)
    wave_id = _stable_hash(
        [symbol, market, strategy, selected.get("start_time"), selected.get("anchor")]
    )
    diagnostics = dict(
        strategy_type=strategy,
        strategy_state=state,
        entry_setup_type=setup,
        strategy_stop_price=stop,
        emergency_stop_price=emergency,
        exit_stage=exit_stage,
        exit_fraction=fraction,
        entry_structure_reference=selected.get("anchor"),
        trend_structure_reference=t.get("anchor"),
        entry_wave_id=wave_id,
        entry_wave_start_time=selected.get("start_time"),
        entry_wave_anchor_price=selected.get("anchor"),
        entry_wave_age_bars=age,
        entry_wave_extension_pct=_pct_change(price, selected.get("anchor")),
        entry_wave_status=(
            "ACTIVE" if age <= params.max_entry_wave_bars else "ENTRY_WAVE_TOO_OLD"
        ),
        momentum_state=m["state"],
        trend_state=t["state"],
        **metrics,
    )
    exit_result = dict(
        exit_signal=bool(exit_stage),
        exit_reason=exit_stage,
        exit_stage=exit_stage,
        exit_fraction=fraction,
        stop_signal=exit_stage
        in {"EMERGENCY_STOP", "MOMENTUM_STRUCTURE_BREAK_FULL", "HIGHER_LOW_BREAK"},
        take_profit_signal=exit_stage in {"MOMENTUM_CUSTOM_TAKE_PROFIT", "TREND_CUSTOM_TAKE_PROFIT"},
        stop_price=stop,
        take_profit_price=custom_take_profit_price,
        average_fill_price=average_fill_price,
        position_return_pct=position_return_pct,
        custom_take_profit_pct=custom_take_profit_pct,
        R_value=None,
        exit_evidence=evidence,
        exit_price_range_source=exit_range.get("source"),
        post_entry_quote_count=exit_range.get("post_entry_quote_count", 0),
    )
    result = dict(
        **diagnostics,
        entry_signal=entry,
        entry_setup=bool(setup),
        entry_reason=setup if entry else "STRUCTURE_OR_ENTRY_GATE",
        entry_block_reason="" if entry else ",".join(blockers) or "STRUCTURE_OR_ENTRY_GATE",
        entry_time=latest.get("last_timestamp") if entry else None,
        signal_price=price,
        symbol=symbol,
        market=market,
        candidate=candidate,
        market_context=dict(market_context),
        entry_conditions={"entry_setup": bool(setup), "candidate_ready": ready},
        entry_diagnostics={**data, **diagnostics, "trend_market_permission": trend_permission},
        entry_paths=entry_paths,
        data_diagnostics=data,
        metrics=metrics,
        bars={
            "1m": bars,
            "3m": (
                aggregate_bars(bars, 3, parameters=params) if include_aggregates else []
            ),
            "5m": (
                aggregate_bars(bars, 5, parameters=params) if include_aggregates else []
            ),
        },
        bar_info=info,
        suggested_stop_price=stop,
        suggested_take_profit_price=None,
        position_exit=exit_result,
        position_monitor_active=active,
        monitor_priority="P0" if active else "P1",
        monitor_lane="POSITION_MONITOR" if active else "CANDIDATE_ENTRY",
        candidate_required_for_exit=False,
        position_monitor={"active": active, **exit_result},
        entry_signal_key=_stable_hash(
            [symbol, market, strategy, setup, latest.get("start")]
        ),
        entry_duplicate_suppressed=False,
        parameters=params.snapshot(),
        parameter_fingerprint=_stable_hash(params.snapshot()),
        warnings=[],
    )
    result.update(exit_result)
    result["data_fingerprint"] = _stable_hash(
        [_canonical_quote_digest(quotes), market_context, candidate, pos]
    )
    result["position_exit_fingerprint"] = _stable_hash([pos, exit_result])
    result["signal_fingerprint"] = _stable_hash(
        [symbol, market, strategy, wave_id,
         "BUY" if entry else "SELL" if result.get("exit_signal") else "HOLD", exit_stage]
    )
    return result


# =============================================================================
# api_gateway.py / market.py / scanner.py 動態連結
# =============================================================================


def _version_tuple(stem: str, prefix: str) -> Optional[Tuple[int, ...]]:
    match = re.fullmatch(rf"{re.escape(prefix)}_v(\d+(?:_\d+)*)", stem)
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("_"))


def _module_candidates(prefix: str, env_name: str) -> List[str]:
    candidates: List[str] = []
    forced = os.getenv(env_name, "").strip()
    if forced:
        candidates.append(forced[:-3] if forced.endswith(".py") else forced)

    base_dir = Path(__file__).resolve().parent
    versioned: List[Tuple[Tuple[int, ...], str]] = []
    for file_path in base_dir.glob(f"{prefix}_v*.py"):
        version = _version_tuple(file_path.stem, prefix)
        if version is not None:
            versioned.append((version, file_path.stem))
    for _, module_name in sorted(versioned, key=lambda item: item[0], reverse=True):
        if module_name not in candidates:
            candidates.append(module_name)

    if prefix not in candidates:
        candidates.append(prefix)
    return candidates


def _load_module_with_required(
    prefix: str,
    candidates: Sequence[str],
    required: Sequence[str],
) -> Any:
    errors: List[str] = []
    for module_name in candidates:
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            errors.append(f"{module_name}: {type(exc).__name__}: {exc}")
            continue
        missing = [name for name in required if not hasattr(module, name)]
        if not missing:
            return module
        errors.append(f"{module_name}: 缺少 {missing}")
    raise RuntimeError(f"找不到可用的 {prefix} 模組：" + " | ".join(errors))


def load_api_gateway_module() -> Any:
    return _load_module_with_required(
        "api_gateway",
        _module_candidates("api_gateway", "INTRADAY_API_GATEWAY_MODULE"),
        ("APIGateway",),
    )


@lru_cache(maxsize=1)
def load_market_module() -> Any:
    return _load_module_with_required(
        "market",
        _module_candidates("market", "INTRADAY_MARKET_MODULE"),
        ("get_market_context",),
    )






def load_quote_sequence_from_gateway(
    gateway: Any,
    symbol: str,
    market: str,
    *,
    trade_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """從 ReplayStore.load_fugle_events() 建立 Fugle Trades Event Sequence。

    不讀 market_records Snapshot、不呼叫 gateway.get_quote()、不發 HTTP。
    Runtime 的事件恢復與盤中運算共用此入口。
    """
    store = getattr(gateway, "store", None)
    load_events = getattr(store, "load_fugle_events", None)
    if not callable(load_events):
        raise RuntimeError(
            "APIGateway ReplayStore 缺少 load_fugle_events()；"
            "Event Driven Intraday 禁止回退 Snapshot／HTTP Quote Sequence"
        )

    records = load_events(symbol=str(symbol))
    if not isinstance(records, Sequence) or isinstance(
        records, (str, bytes, bytearray)
    ):
        return []

    wanted_market = _normalize_market(market)
    output: List[Dict[str, Any]] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        if str(record.get("kind") or "").upper() != "TRADE":
            continue
        if str(record.get("classification") or "").upper() != "NEW":
            continue
        payload = (
            dict(record.get("payload") or {})
            if isinstance(record.get("payload"), Mapping)
            else {}
        )
        event_time = _to_int(
            record.get("event_time")
            if record.get("event_time") is not None
            else (
                payload.get("event_time")
                if payload.get("event_time") is not None
                else payload.get("time")
            )
        )
        event_dt = _parse_fugle_event_time(event_time, TIMEZONE)
        if event_dt is None:
            continue
        event_trade_date = event_dt.date().isoformat()
        if trade_date is not None and event_trade_date != str(trade_date):
            continue

        event_symbol = str(
            record.get("symbol") or payload.get("symbol") or symbol
        ).strip()
        if event_symbol != str(symbol).strip():
            continue

        output.append(
            {
                "source": "FUGLE_WEBSOCKET",
                "symbol": event_symbol,
                "name": str(payload.get("name") or ""),
                "market": wanted_market,
                "trade_date": event_trade_date,
                "timestamp": event_dt.isoformat(timespec="microseconds"),
                "event_time": event_time,
                "serial": _to_int(
                    record.get("serial")
                    if record.get("serial") is not None
                    else payload.get("serial")
                ),
                "received_at": str(
                    record.get("received_at") or payload.get("received_at") or ""
                ),
                "price": _to_float(payload.get("price")),
                "size": max(0, _to_int(payload.get("size"))),
                "volume": max(0, _to_int(payload.get("volume"))),
                "bid": _to_float(payload.get("bid")),
                "ask": _to_float(payload.get("ask")),
                "event_classification": "NEW",
                "classification": "NEW",
                "replay_event_id": record.get("id"),
            }
        )

    output.sort(
        key=lambda row: (
            _parse_fugle_event_time(row.get("event_time"), TIMEZONE)
            or datetime.min.replace(tzinfo=_taipei_timezone(TIMEZONE)),
            _to_int(row.get("serial")),
            _to_int(row.get("replay_event_id")),
        )
    )
    return output


def _find_candidate(
    candidates_context: Mapping[str, Any],
    symbol: Optional[str],
    market: Optional[str],
) -> Optional[Dict[str, Any]]:
    rows = candidates_context.get("candidates")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return None
    wanted_symbol = str(symbol or "").strip()
    wanted_market = _normalize_market(market) if market else ""
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if wanted_symbol and str(row.get("symbol") or "") != wanted_symbol:
            continue
        if wanted_market and _normalize_market(row.get("market")) != wanted_market:
            continue
        return dict(row)
    if not wanted_symbol:
        for row in rows:
            if isinstance(row, Mapping):
                return dict(row)
    return None


def get_signal_context(
    symbol: Optional[str] = None,
    market: Optional[str] = None,
    *,
    gateway: Any = None,
    candidate: Optional[Mapping[str, Any]] = None,
    candidates_context: Optional[Mapping[str, Any]] = None,
    market_context: Optional[Mapping[str, Any]] = None,
    quote_sequence: Optional[Sequence[Mapping[str, Any]]] = None,
    trade_date: Optional[str] = None,
    parameters: Optional[IntradayParameters] = None,
    position: Optional[Mapping[str, Any]] = None,
    include_aggregates: bool = True,
) -> Dict[str, Any]:
    """對外統一入口；只接受既有 Fugle Event Sequence 或 ReplayStore 事件。

    Runtime 正式路徑應直接傳 quote_sequence + market_context + candidate/position。
    若未傳 quote_sequence，只允許從已提供 gateway 的 ReplayStore.load_fugle_events()
    讀取已保存事件；本函式禁止 gateway.get_quote()、HTTP refresh、Scanner live fetch。
    """
    params = parameters or IntradayParameters()
    params.validate()

    direct_quote_input = quote_sequence is not None
    integration_warnings: List[str] = []
    normalized_position = _normalize_position(position, params)
    position_monitor_active = normalized_position is not None


    if position_monitor_active:
        position_symbol = str(normalized_position.get("symbol") or "").strip()
        position_market = _normalize_market(normalized_position.get("market"))
        symbol = str(symbol or position_symbol or "").strip()
        market = _normalize_market(market or position_market)

    if (not symbol or not market) and quote_sequence:
        first_quote = next(
            (row for row in quote_sequence if isinstance(row, Mapping)),
            None,
        )
        if isinstance(first_quote, Mapping):
            symbol = str(symbol or first_quote.get("symbol") or "").strip()
            market = _normalize_market(market or first_quote.get("market"))

    resolved_candidates_context = dict(candidates_context or {})
    resolved_candidate = dict(candidate) if isinstance(candidate, Mapping) else None
    if (
        not position_monitor_active
        and resolved_candidate is None
        and resolved_candidates_context
    ):
        resolved_candidate = _find_candidate(
            resolved_candidates_context,
            symbol,
            market,
        )

    if not position_monitor_active and resolved_candidate is not None:
        symbol = str(symbol or resolved_candidate.get("symbol") or "").strip()
        market = _normalize_market(market or resolved_candidate.get("market"))

    if not symbol or not market:
        if position_monitor_active:
            raise ValueError(
                "Position P0 必須由 position、明確參數或 Fugle Event Sequence 提供 symbol / market"
            )
        raise ValueError(
            "Candidate P1 必須由 Runtime 提供 symbol / market 與 Candidate；"
            "Intraday 不再自行呼叫 Scanner"
        )

    if market_context is None:
        if position_monitor_active:
            market_context = {
                "market_state": "POSITION_MONITOR",
                "market_score": None,
                "trading_mode": "POSITION_EXIT_ONLY",
                "allow_long": False,
            }
            integration_warnings.append(
                "Position P0 未傳入 Market Context；EXIT 繼續執行，且不開放新的 ENTRY。"
            )
        elif (
            resolved_candidates_context and "allow_long" in resolved_candidates_context
        ):
            market_context = {
                "market_state": resolved_candidates_context.get("market_state"),
                "market_score": resolved_candidates_context.get("market_score"),
                "trading_mode": resolved_candidates_context.get("trading_mode"),
                "allow_long": resolved_candidates_context.get("allow_long"),
            }
        else:
            raise ValueError(
                "Candidate P1 必須由 Runtime 提供 Market Context；Intraday 不再自行取得 Market Context"
            )
    market_context = dict(market_context or {})

    if quote_sequence is None:
        if gateway is None:
            raise ValueError(
                "未提供 Fugle Trade Event Sequence；必須由 Runtime 傳入 quote_sequence，"
                "或提供只供 ReplayStore 讀取的 gateway"
            )
        quote_sequence = load_quote_sequence_from_gateway(
            gateway,
            str(symbol),
            str(market),
            trade_date=trade_date,
        )

    result = calculate_signal_context(
        quote_sequence if quote_sequence is not None else [],
        market_context,
        resolved_candidate,
        parameters=params,
        position=position,
        include_aggregates=include_aggregates,
    )

    if resolved_candidates_context and not position_monitor_active:
        updated_data_diagnostics = _data_diagnostics(
            quote_sequence if quote_sequence is not None else [],
            normalize_quote_sequence(
                quote_sequence if quote_sequence is not None else [],
                parameters=params,
            ),
            result.get("bars", {}).get("1m", []),
            result.get("metrics", {}),
            resolved_candidate,
            params,
            candidate_context=resolved_candidates_context,
        )
        result["data_diagnostics"] = updated_data_diagnostics
        result["entry_diagnostics"].update(updated_data_diagnostics)

    result["integration"] = {
        "symbol": str(symbol),
        "market": str(market),
        "monitor_priority": "P0" if position_monitor_active else "P1",
        "monitor_lane": (
            "POSITION_MONITOR" if position_monitor_active else "CANDIDATE_ENTRY"
        ),
        "candidate_dependency": (
            "NONE_FOR_POSITION_EXIT"
            if position_monitor_active
            else "REQUIRED_FOR_ENTRY"
        ),
        "candidate_source": (
            "NOT_REQUIRED_POSITION_P0"
            if position_monitor_active
            else (
                "EXPLICIT"
                if candidate is not None
                else (
                    "SCANNER_CONTEXT"
                    if candidates_context is not None
                    else "UNAVAILABLE"
                )
            )
        ),
        "quote_source": (
            "FUGLE_TRADES_DIRECT" if direct_quote_input else "FUGLE_TRADES_REPLAY"
        ),
        "event_time_authority": "FUGLE_EVENT_TIME",
        "external_quote_refresh": False,
    }
    result["warnings"] = list(
        dict.fromkeys(integration_warnings + list(result.get("warnings") or []))
    )
    return result


# =============================================================================
# 離線自我測試
# =============================================================================


def run_self_test():
    """離線驗證策略分流、資料時間、持倉鎖定與出場優先順序。"""
    base = datetime(2026, 8, 31, 9, 0, tzinfo=_taipei_timezone())
    market = {
        "market_state": "BULL",
        "allow_long": True,
        "intraday_trend": "UP",
        "momentum_long_allowed": True,
        "trend_long_allowed": True,
    }
    candidate = {
        "symbol": "2317",
        "market": "TWSE",
        "trade_ready": True,
        "momentum_eligible": True,
        "trend_eligible": True,
        "market_relative_strength": 3.0,
    }

    # 共用市場許可契約；固定事件時間，使離線重播不依賴執行日期。
    market.update(market_score=40, auto_market_state="BULL", parameters={},
                  breadth={"ratio": 0.6, "net_ratio": 0.6}, realtime_buy_data_ready=True,
                  data_fingerprint="OFFLINE_FIXTURE", asof=(base + timedelta(minutes=9, seconds=35)).isoformat())
    for index_name in ("taiex", "tpex"):
        market[index_name] = {"available": True, "change_pct": 1,
                              "timestamp": market["asof"]}
    market["breadth_snapshot_acquired_at"] = {"TWSE": market["asof"], "TPEX": market["asof"]}
    market["strategy_market_permissions"] = {"TREND": load_market_module()._trend_permission_policy(market)}

    def events(prices, volumes):
        rows = []
        for i, (p, v) in enumerate(zip(prices, volumes)):
            for j, price in enumerate((p[0], p[1])):
                dt = base + timedelta(minutes=i, seconds=j * 35)
                rows.append(
                    dict(
                        symbol="2317",
                        market="TWSE",
                        event_time=int(dt.timestamp() * 1_000_000),
                        serial=i * 2 + j,
                        price=price,
                        size=v // 2,
                        classification="NEW",
                        received_at=dt.isoformat(),
                    )
                )
        return rows

    prices = [(50 + i * 0.01, 50.02 + i * 0.01) for i in range(9)] + [(50.1, 50.5)]
    ev = events(prices, [100] * 9 + [600])
    out = calculate_signal_context(ev, market, candidate)
    assert out["entry_signal"] and out["entry_setup_type"] == "INITIAL_BREAKOUT"
    assert out["strategy_stop_price"] < out["signal_price"]
    assert out["metrics"]["volume_expansion_ratio"] == 6
    assert out["metrics"]["range_expansion_ratio"] > 10
    reversed_out = calculate_signal_context(list(reversed(ev)), market, candidate)
    assert reversed_out["signal_fingerprint"] == out["signal_fingerprint"]
    assert not calculate_signal_context(
        ev, dict(market, momentum_long_allowed=False), dict(candidate, trend_eligible=False)
    )["entry_signal"]
    assert not calculate_signal_context(ev, market, dict(candidate, trade_ready=False))[
        "entry_signal"
    ]
    # 首次突破後第一次量縮回踩，隨後再突破；不是雙測低反轉。
    ev2 = events(prices + [(50.4, 50.35), (50.4, 50.65)], [100] * 9 + [600, 40, 600])
    again = calculate_signal_context(ev2, market, candidate)
    assert (
        again["entry_signal"] and again["entry_setup_type"] == "FIRST_PULLBACK_REBREAK"
    )
    position = dict(
        symbol="2317",
        market="TWSE",
        entry_price=50.5,
        entry_time=(base + timedelta(minutes=9, seconds=36)).isoformat(),
        quantity=2000,
        managed_quantity=2000,
        strategy_type="TREND",
        strategy_stop_price=49.0,
        emergency_stop_price=48.0,
    )
    # 同分鐘成交前低點不能觸發 EXIT；一根負動能不使 Trend 平倉。
    hold = calculate_signal_context(ev, market, None, position=position)
    assert (
        hold["strategy_type"] == "TREND"
        and not hold["exit_signal"]
        and not hold["entry_signal"]
    )
    extended = events(prices + [(50.5, 50.45)], [100] * 9 + [600, 100])
    hold = calculate_signal_context(extended, market, None, position=position)
    assert hold["strategy_type"] == "TREND" and not hold["exit_signal"]
    weak_market = dict(market, intraday_trend="DOWN", market_score=10, trend_long_allowed=False)
    weak_market["strategy_market_permissions"] = {"TREND": load_market_module()._trend_permission_policy(weak_market)}
    stamp = (base + timedelta(minutes=10, seconds=35)).isoformat()
    for name in ("taiex", "tpex"):
        weak_market[name] = dict(weak_market.get(name) or {}, timestamp=stamp)
    weak_market["breadth_snapshot_acquired_at"] = {"TWSE": stamp, "TPEX": stamp}
    weak_market["strategy_market_permissions"] = {"TREND": load_market_module()._trend_permission_policy(weak_market)}
    down = calculate_signal_context(extended, weak_market, None, position=position)
    assert (
        down["exit_stage"] == "MARKET_TREND_INVALIDATED" and down["exit_fraction"] == 1
    )
    emergency = events(prices + [(48, 47.9)], [100] * 9 + [600, 100])
    stop = calculate_signal_context(
        emergency, dict(market, market_state="PANIC"), None, position=position
    )
    assert stop["exit_stage"] == "EMERGENCY_STOP"
    # 缺少候選不阻擋持倉緊急出場，部分出場後禁止同一衰退階段再次減碼。
    momentum_position = dict(
        position,
        strategy_type="MOMENTUM",
        strategy_stop_price=49,
        partial_exit_done=True,
    )
    after = calculate_signal_context(extended, market, None, position=momentum_position)
    assert after["exit_stage"] != "MOMENTUM_DECAY_PARTIAL"
    buffer = IncrementalEventBuffer(IntradayParameters())
    for row in ev2:
        buffer.append_sequence([row], IntradayParameters())
    inc = calculate_signal_context(buffer, market, candidate)
    assert inc["bars"] == again["bars"] and inc["metrics"] == again["metrics"]
    return {"ok": True, "checks": 17, "policy": "離線雙策略及事件時間驗證"}


# =============================================================================
# 診斷
# =============================================================================


def diagnose_links() -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "intraday_entry": str(inspect.signature(get_signal_context)),
        "pure_core": str(inspect.signature(calculate_signal_context)),
        "output_fields": OUTPUT_FIELDS,
        "condition_fields": CONDITION_FIELDS,
        "entry_diagnostic_fields": ENTRY_DIAGNOSTIC_FIELDS,
        "event_sequence_contract": (
            "FUGLE TRADES NEW EVENTS -> event_time/serial -> Tick -> 1m -> 3m/5m"
        ),
        "event_time_authority": "FUGLE_EVENT_TIME_ONLY_FOR_BAR_ASSIGNMENT",
        "received_at_policy": "DIAGNOSTIC_ONLY_NEVER_BAR_TIME",
        "external_quote_refresh": False,
        "snapshot_quote_sequence": False,
        "entry_paths": {
            "MOMENTUM": "首次有效量價突破，或首次量縮回踩後放量再突破",
            "TREND": "市場向上、EMA 與高低點結構確認後，等待回踩恢復",
        },
        "entry_dedup_owner": "Trading/Runtime position + pending order + submission lock + registry + CTS reconcile",
        "monitor_lanes": {
            "P0": "Position Monitor：不依賴 Candidate List / Candidate freshness / Universe，持續執行 EXIT",
            "P1": "Candidate Entry：市場策略許可、雙策略候選資格與結構條件",
        },
        "candidate_freshness_rule": (
            "realtime_fresh=False 或 trade_ready=False 只阻擋新 ENTRY；既有 Position EXIT 不受影響"
        ),
        "data_diagnostic_pipeline": (
            "Fugle Trade Event Sequence -> event_time/serial normalize -> "
            "1m bars -> 3m/5m -> Momentum readiness"
        ),
        "momentum_readiness_rule": {
            "1m": "bar_count_1m >= 2",
            "3m": "bar_count_1m >= 4",
            "5m": "bar_count_1m >= 6",
        },
        "fixed_parameter_keys": list(IntradayParameters().snapshot().keys()),
        "timezone_runtime": {
            "name": TIMEZONE,
            "offset": "+08:00",
            "external_tzdata_required": False,
        },
        "runtime_module_name": "runtime",
        "legacy_runtime_dependency": False,
    }

    try:
        api_module = load_api_gateway_module()
        gateway_class = getattr(api_module, "APIGateway")
        replay_class = getattr(api_module, "ReplayStore", None)
        result["api_gateway"] = {
            "ok": (
                replay_class is not None
                and callable(getattr(replay_class, "load_fugle_events", None))
            ),
            "module": "api_gateway",
            "APIGateway_signature": str(inspect.signature(gateway_class)),
            "ReplayStore_load_fugle_events": (
                str(inspect.signature(replay_class.load_fugle_events))
                if replay_class is not None
                and callable(getattr(replay_class, "load_fugle_events", None))
                else None
            ),
        }
    except Exception as exc:
        result["api_gateway"] = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    result["ok"] = bool(result.get("api_gateway", {}).get("ok"))
    return result


# =============================================================================
# CLI
# =============================================================================


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="盤中行情與進出場訊號判定模組")
    parser.add_argument("--self-test", action="store_true", help="執行完全離線自我測試")
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="檢查 api_gateway / market / scanner 連結",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 顯示結果")
    return parser


def _print_human(result: Mapping[str, Any]) -> None:
    print("=" * 112)
    print("盤中行情與進出場訊號判定模組")
    print("=" * 112)

    for key, value in result.items():
        print(f"{key}: {value}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.self_test:
        try:
            result = run_self_test()
        except Exception as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    elif args.diagnose:
        result = diagnose_links()
    else:
        result = {
            "ok": True,
            "message": (
                "正式使用請由 runtime / trading 呼叫 get_signal_context()；"
                "測試請使用 --self-test 或 --diagnose。"
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
