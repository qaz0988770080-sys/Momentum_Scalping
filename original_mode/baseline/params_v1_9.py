# -*- coding: utf-8 -*-
"""
可重現參數模型、保存機制與參數設定 Component。

本模組只負責 GUI／帳戶、Fugle Stream 與四個核心模組參數的顯示、編輯、驗證、
保存、載入、還原及中文說明整合。Runtime、背景 Worker、交易策略、行情取得、
訊號計算、風控決策與 CTS 下單流程不在此模組實作。
"""

# 中文重點更新：清理歷史相容參數、孤立註解及無效追單顯示，保留測試、診斷、CTS 撤單與委託及持倉對帳。

# 中文重點更新：清除未使用函式、相容介面與無效參數；保留市價買賣、成交對帳、撤買單、清倉及離線驗證。

# 中文重點更新：移除停用參數與中文標籤；舊設定首次載入更新指定門檻，後續保存尊重手動設定。

# 中文重點更新：集中管理雙策略參數與中文名稱，遷移歷史候選權重。

# 中文重點更新：Scanner 參數表只隱藏現役內部排序與權重欄位，診斷不再列舉已刪除的分批取價欄位。

# 中文重點更新：補齊審查要求之狀態識別、整張委託、資料時序與恢復安全邊界。

# 中文重點更新：新增強勢回檔市場分數、廣度與許可有效秒數的中文參數標籤。

# 中文重點更新：增加 BUY 當日漲幅控制與舊預設三根至五根的 Wave 參數移轉，保留其他自訂條件。

# 中文重點更新：新增持續推升、健康整理及早期 Trend 參數標籤，Momentum 與 Trend RS 分離。

# 中文重點更新：Fugle 重連預設最短三十秒、退避上限六十秒；保留 Scanner 原有逾時。

# 中文重點更新：新增進場訊號最大回落容許百分比的中文名稱與參數說明。

# 中文重點更新：參數頁新增兩策略自訂停利百分比；沿用核心預設零、驗證、保存與載入機制。

# 中文重點更新：參數表使用精簡重點，完整說明固定區域換行捲動，操作列固定可用；舊限價設定轉為市價。

from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import json
import math
import re
import textwrap
from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

PARAMETER_ROOT_NAME = "gui_parameters"
LATEST_PARAMETER_FILE = "latest.json"
PARAMETER_HISTORY_DIR = "history"
PARAMETER_HELP_FILE = "params.md"

# quote_refresh_seconds 僅控制 GUI 畫面更新，不得作為策略行情取得頻率。
GUI_DISPLAY_ONLY_FIELDS = frozenset({"quote_refresh_seconds"})

# Fugle API Key 僅由環境變數／登入流程提供，禁止保存於參數 JSON 或 params.md。
FUGLE_STREAM_SETTING_FIELDS = frozenset(
    {
        "fugle_websocket_enabled",
        "fugle_rest_seed_enabled",
        "fugle_event_freshness_seconds",
        "fugle_stream_health_timeout_seconds",
        "fugle_reconnect_min_seconds",
        "fugle_reconnect_max_seconds",
        "fugle_subscription_limit",
        "fugle_candidate_subscription_enabled",
        "fugle_books_subscription_mode",
    }
)
VALID_FUGLE_BOOKS_SUBSCRIPTION_MODES = {"P0_P1"}

VALID_CONTROL_MODES = {"AUTO", "MANUAL"}
VALID_API_MODES = {"LIVE", "OFFLINE"}
VALID_MANUAL_MARKET_STATES = {"BULL", "NORMAL", "WEAK", "PANIC"}
VALID_MANUAL_TRADING_MODES = {
    "TREND",
    "SELECTIVE_LONG",
    "DEFENSIVE",
    "OVERSOLD_MEAN_REVERSION",
    "STOP",
}

PARAMETER_GROUP_SPECS: Dict[str, Dict[str, str]] = {
    "market": {
        "parameter_class": "MarketParameters",
        "title": "股票市場參數",
    },
    "scanner": {
        "parameter_class": "ScannerParameters",
        "title": "選股參數",
    },
    "intraday": {
        "parameter_class": "IntradayParameters",
        "title": "盤中資訊參數",
    },
    "trading": {
        "parameter_class": "TradingParameters",
        "title": "交易／風控",
    },
}

CORE_PARAMETER_LABELS: Dict[str, Dict[str, str]] = {
    "market": {
        "ma_period": "移動平均週期",
        "market_score_weight": "指數權重",
        "breadth_weight": "市場廣度權重",
        "sector_weight": "類股權重",
        "bull_threshold": "多頭門檻",
        "bear_threshold": "弱勢門檻",
        "taiex_index_weight": "TAIEX 權重",
        "tpex_index_weight": "TPEx 權重",
        "index_change_weight": "指數漲跌權重",
        "index_direction_weight": "指數方向權重",
        "index_change_full_scale_pct": "指數漲跌滿分尺度",
        "direction_full_scale_pct": "方向滿分尺度",
        "breadth_count_weight": "家數廣度權重",
        "breadth_turnover_weight": "成交值廣度權重",
        "panic_taiex_drop_pct": "TAIEX 急跌門檻",
        "panic_tpex_drop_pct": "TPEx 急跌門檻",
        "panic_breadth_ratio": "市場廣度急跌門檻",
        "sector_strong_score": "強勢類股門檻",
        "sector_change_full_scale_pct": "類股漲跌滿分尺度",
    },
    "scanner": {
        "min_price": "最低股價",
        "max_price": "最高股價",
        "min_volume": "最低成交量",
        "min_turnover": "最低成交金額",
        "min_change": "最低漲幅",
        "min_volume_ratio": "最低量比",
        "min_score": "最低候選分數",
        "min_score_coverage": "最低分數覆蓋率",
        "active_universe_n": "熱門活躍股票池上限",
        "top_n": "最終候選數",
        "near_high_max_distance_pct": "接近高點距離門檻",
        "near_high_score_zero_distance_pct": "高點距離零分尺度",
        "change_score_full_pct": "漲幅滿分尺度",
        "market_rs_full_pct": "相對大盤滿分尺度",
        "sector_rs_full_pct": "相對類股滿分尺度",
        "volume_ratio_full": "量比滿分尺度",
        "volume_score_full_multiplier": "成交量滿分倍數",
        "turnover_score_full_multiplier": "成交金額滿分倍數",
        "spread_good_pct": "良好價差門檻",
        "spread_bad_pct": "不良價差門檻",
    },
    "intraday": {
        "momentum_1m": "1 分鐘動能週期",
        "momentum_3m": "3 分鐘動能週期",
        "momentum_5m": "5 分鐘動能週期",
        "volume_window": "量能平均視窗",
        "breakout_window": "突破回看視窗",
        "min_bars": "最低 K 棒數",
        "breakout_buffer_pct": "突破緩衝",
        "max_entry_wave_bars": "進場 Wave 最大 K 棒數",
    },
    "trading": {
        "max_spread_pct": "最大買賣價差",
        "max_slippage_pct": "最大滑價",
        "max_chase_pct": "最大追價幅度",
        "max_breakout_extension_pct": "Breakout Wave 最大延伸百分比",
        "max_execution_friction_pct": "最大執行摩擦",
        "max_buy_price": "最高買進股價",
        "max_position_count": "最大持倉檔數（上限）",
        "max_daily_trade_count": "每日最大交易次數",
        "max_daily_loss_pct": "每日最大虧損百分比",
        "max_daily_loss_amount": "每日最大虧損金額",
        "max_single_position_pct": "單一持倉權益比例",
        "max_single_position_amount": "單一持倉金額",
        "max_risk_per_trade_pct": "單筆最大風險比例",
        "cash_utilization_pct": "可用現金使用比例",
        "max_position_shares": "單一持倉股數上限",
        "lot_size": "每張股數",
        "cts_order_type": "CTS 交易類型代碼",
        "cts_price_type": "CTS 價格類型代碼",
        "cts_condition": "CTS 委託條件代碼",
    },
}

CORE_PARAMETER_LABELS["market"].update({"trend_breadth_min": "趨勢做多最低市場廣度"})
CORE_PARAMETER_LABELS["trading"]["max_entry_day_change_pct"] = "BUY 當日漲幅上限（%／0 停用）"
CORE_PARAMETER_LABELS["market"].update({
    "trend_pullback_score_min": "強勢整理／回檔最低市場分數",
    "trend_pullback_breadth_min": "強勢整理／回檔最低市場廣度",
    "trend_permission_max_age_seconds": "Trend 市場許可資料有效秒數",
})
CORE_PARAMETER_LABELS["scanner"].update(
    {
        "momentum_candidate_min_score": "動能候選最低分數",
        "trend_candidate_min_score": "趨勢候選最低分數",
    }
)
CORE_PARAMETER_LABELS["intraday"].update(
    {
        "volume_expansion_min": "分鐘成交量擴張倍數",
        "range_expansion_min": "分鐘振幅擴張倍數",
        "ema_fast_period": "快速 EMA 週期",
        "ema_slow_period": "慢速 EMA 週期",
        "pullback_volume_max": "回踩最大量縮倍數",
        "momentum_decay_volume_max": "動能衰退成交量門檻",
        "momentum_partial_exit_fraction": "動能首次減碼比例",
        "momentum_take_profit_pct": "Momentum 自訂停利％（0＝原始出場線）",
        "trend_take_profit_pct": "Trend 自訂停利％（0＝原始出場線）",
        "structure_buffer_pct": "結構停損下緣緩衝百分比",
        "emergency_stop_loss_pct": "緊急硬停損百分比",
        "trend_rs_min": "Trend 相對大盤最低強度",
        "momentum_rs_min": "Momentum 相對大盤最低強度",
        "persistent_bars": "持續推升確認根數",
        "persistent_volume_min": "持續推升固定基準量比",
        "healthy_pullback_pct": "健康回檔單根最大跌幅百分比",
        "healthy_pullback_volume_max": "健康回檔最大量比",
        "trend_initial_bars": "Trend 早期確認最少根數",
    }
)
CORE_PARAMETER_LABELS["trading"].update(
    {
        "max_entry_trigger_retrace_pct": "進場訊號最大回落容許百分比",
        "max_stop_distance_pct": "最大結構停損距離百分比",
        "emergency_stop_loss_pct": "成交後緊急硬停損百分比",
        "max_portfolio_open_risk_pct": "總開放風險權益百分比",
        "max_sector_position_count": "同類股最多持倉數",
        "max_entries_per_symbol_per_day": "同股票每日最多進場次數",
        "reentry_cooldown_minutes": "平倉後重新進場冷卻分鐘",
        "max_consecutive_losses": "連續虧損觸發次數",
        "loss_cooldown_minutes": "連續虧損冷卻分鐘",
    }
)

PARAMETER_TABLE_HIDDEN_FIELDS: Dict[str, frozenset[str]] = {
    "scanner": frozenset({"sort_rule", "pre_sort_rule", "score_weight",
                          "pre_score_weight", "momentum_weight", "trend_weight"}),
}


def _now_text() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def _stable_hash(payload: Any) -> str:
    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


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


def _display_value(value: Any) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if value is None:
        return "None"
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(_jsonable(value), ensure_ascii=False, separators=(",", ":"))
    return str(value)


def resolve_parameter_help_path(base_dir: Path) -> Path:
    """優先使用最新版 params_v*.md；沒有版本檔時才退回 params.md。"""
    root = Path(base_dir).resolve()
    versioned: List[Tuple[Tuple[int, ...], Path]] = []
    for candidate in root.glob("params_v*.md"):
        match = re.fullmatch(r"params_v(\d+(?:_\d+)*)", candidate.stem)
        if match is None:
            continue
        version = tuple(int(part) for part in match.group(1).split("_"))
        versioned.append((version, candidate))
    if versioned:
        versioned.sort(key=lambda item: item[0], reverse=True)
        return versioned[0][1]
    return root / PARAMETER_HELP_FILE


_PARAMETER_HELP_CACHE: Dict[str, Tuple[int, Dict[str, str]]] = {}


def load_parameter_help(path: Path) -> Dict[str, str]:
    """讀取參數說明；每個 ``## group.parameter`` 區塊是一筆中文說明。"""
    help_path = Path(path).resolve()
    try:
        stat = help_path.stat()
    except FileNotFoundError:
        return {}

    cache_key = str(help_path)
    cached = _PARAMETER_HELP_CACHE.get(cache_key)
    if cached is not None and cached[0] == stat.st_mtime_ns:
        return dict(cached[1])

    sections: Dict[str, str] = {}
    current_key = ""
    buffer: List[str] = []

    def flush() -> None:
        nonlocal current_key, buffer
        if current_key:
            text = " ".join(part.strip() for part in buffer if part.strip())
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                sections[current_key] = text
        current_key = ""
        buffer = []

    for raw_line in help_path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("## "):
            flush()
            candidate = stripped[3:].strip().strip("`")
            current_key = (
                candidate
                if re.fullmatch(r"[A-Za-z0-9_]+\.[A-Za-z0-9_]+", candidate)
                else ""
            )
            continue
        if stripped.startswith("# "):
            flush()
            continue
        if current_key and stripped and not stripped.startswith("<!--"):
            buffer.append(stripped.lstrip("- ").strip())
    flush()

    if len(_PARAMETER_HELP_CACHE) >= 8:
        _PARAMETER_HELP_CACHE.pop(next(iter(_PARAMETER_HELP_CACHE)))
    _PARAMETER_HELP_CACHE[cache_key] = (stat.st_mtime_ns, dict(sections))
    return sections


def _parameter_help_text(base_dir: Path, group: str, name: str) -> str:
    path = resolve_parameter_help_path(base_dir)
    return load_parameter_help(path).get(f"{group}.{name}", "")


def _normalize_constraint_text(message: str, field_name: str) -> str:
    text = str(message or "").strip()
    if not text:
        return ""

    text = text.replace("~", "～")
    text = text.replace(" <= ", " 不得大於 ")
    text = text.replace(" >= ", " 不得小於 ")
    text = text.replace(" > ", " 大於 ")
    text = text.replace(" < ", " 小於 ")
    text = re.sub(r"\s+", " ", text).strip()

    field_upper = str(field_name).upper()
    if "+" not in text and text.startswith(field_upper + " "):
        text = text[len(field_upper) :].strip()
    elif "+" not in text and text.startswith(field_name + " "):
        text = text[len(field_name) :].strip()

    replacements = {
        "不得 小於": "不得小於",
        "不得 大於": "不得大於",
        "必須 大於": "必須大於",
        "必須 小於": "必須小於",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text.rstrip("。") + "。"


def _raise_message_text(node: Any, module: Any, field_name: str) -> str:
    exc = getattr(node, "exc", None)
    if not isinstance(exc, ast.Call) or not exc.args:
        return ""
    argument = exc.args[0]
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return str(argument.value)

    try:
        expression = ast.Expression(argument)
        ast.fix_missing_locations(expression)
        namespace = dict(getattr(module, "__dict__", {}) or {})
        return str(
            eval(
                compile(expression, "<parameter-limit>", "eval"),
                namespace,
                {"name": field_name.upper()},
            )
        )
    except Exception:
        try:
            return ast.unparse(argument)
        except Exception:
            return ""


def _extract_module_constraints(module: Any, parameter_class: Any) -> Dict[str, str]:
    field_names = [item.name for item in fields(parameter_class())]
    messages: Dict[str, List[str]] = {name: [] for name in field_names}
    validator = getattr(parameter_class, "validate", None)
    if not callable(validator):
        return {name: "由核心模組驗證。" for name in field_names}

    try:
        source = textwrap.dedent(inspect.getsource(validator))
        tree = ast.parse(source)
    except Exception:
        tree = None

    if tree is not None:
        parents: Dict[Any, Any] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent

        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise):
                continue
            message = _raise_message_text(node, module, "")
            if not message:
                continue

            related: set[str] = set()
            current = parents.get(node)
            depth = 0
            while current is not None and depth < 5:
                if isinstance(current, ast.FunctionDef):
                    break
                for sub in ast.walk(current):
                    if (
                        isinstance(sub, ast.Attribute)
                        and isinstance(sub.value, ast.Name)
                        and sub.value.id == "self"
                        and sub.attr in messages
                    ):
                        related.add(sub.attr)
                current = parents.get(current)
                depth += 1

            upper_message = message.upper()
            for name in field_names:
                if name.upper() in upper_message:
                    related.add(name)

            for name in related:
                rendered = _raise_message_text(node, module, name)
                normalized = _normalize_constraint_text(rendered, name)
                if normalized and normalized not in messages[name]:
                    messages[name].append(normalized)

    result: Dict[str, str] = {}
    defaults = parameter_class()
    for name in field_names:
        collected = messages.get(name) or []
        if collected:
            result[name] = " ".join(collected)
            continue

        default = getattr(defaults, name)
        if isinstance(default, bool):
            result[name] = (
                "布林開關；True＝啟用，False＝停用。"
                "實際允許條件由核心模組 validate() 驗證。"
            )
        elif isinstance(default, (tuple, list, dict)):
            result[name] = (
                "使用 JSON／陣列格式；內容、權重或排序關係由核心模組 validate() 驗證。"
            )
        else:
            result[name] = "由核心模組 validate() 驗證允許值、範圍及與其他參數的關係。"
    return result


def _extract_parameter_usage_functions(
    module: Any,
    parameter_class: Any,
) -> Dict[str, List[str]]:
    field_names = {item.name for item in fields(parameter_class())}
    usage: Dict[str, set[str]] = {name: set() for name in field_names}
    try:
        source = inspect.getsource(module)
        tree = ast.parse(source)
    except Exception:
        return {name: [] for name in field_names}

    excluded = {
        "validate",
        "snapshot",
        "run_self_test",
        "_self_test_data",
        "build_arg_parser",
        "main",
        "diagnose_links",
        "_print_human",
    }
    parameter_aliases = {"params", "effective", "parameters"}
    for function in [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]:
        if function.name in excluded or function.name.startswith("_sample"):
            continue
        for node in ast.walk(function):
            if not isinstance(node, ast.Attribute) or node.attr not in field_names:
                continue
            value = node.value
            if isinstance(value, ast.Name) and value.id in parameter_aliases:
                usage[node.attr].add(function.name)
    return {name: sorted(values) for name, values in usage.items()}


def _translate_parameter_terms(text: str, module_key: str) -> str:
    output = str(text or "").strip()
    labels = CORE_PARAMETER_LABELS.get(module_key, {})
    for name, label in sorted(
        labels.items(), key=lambda item: len(item[0]), reverse=True
    ):
        output = output.replace(name.upper(), label)
        output = output.replace(name, label)
    output = re.sub(r"\s+", " ", output).strip()
    return output


def _usage_fallback_description(
    module_key: str,
    name: str,
    usages: Sequence[str],
) -> str:
    label = CORE_PARAMETER_LABELS.get(module_key, {}).get(name, "此參數")
    if usages:
        return (
            f"核心模組會在實際策略計算／判定流程中使用「{label}」，"
            "會直接影響對應結果。"
        )
    return (
        f"核心模組保存「{label}」作為可重現策略參數；"
        "目前原始碼未找到額外的直接計算用途說明。"
    )


def _compose_parameter_description(
    base_dir: Path,
    module_key: str,
    name: str,
    constraint: str,
    usages: Sequence[str],
) -> str:
    purpose = _parameter_help_text(base_dir, module_key, name)
    if not purpose:
        purpose = _usage_fallback_description(module_key, name, usages)

    if "有效範圍／注意：" in purpose:
        return purpose
    translated = _translate_parameter_terms(constraint, module_key)
    generic_limits = {
        "由核心模組 validate() 驗證允許值、範圍及與其他參數的關係。",
        "由核心模組 validate() 驗證。",
    }
    if translated in generic_limits:
        translated = ""
    if translated:
        return f"{purpose} 簡易限制：{translated}"
    return f"{purpose} 簡易限制：核心模組目前未另設數值範圍限制。"


def _parse_user_text(text: str, template: Any) -> Any:
    raw = str(text).strip()

    if template is None:
        if raw.lower() in {"", "none", "null"}:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return raw

    if isinstance(template, bool):
        lowered = raw.lower()
        if lowered in {"1", "true", "yes", "y", "on", "是", "啟用"}:
            return True
        if lowered in {"0", "false", "no", "n", "off", "否", "停用"}:
            return False
        raise ValueError("布林值請輸入 True / False")

    if isinstance(template, int) and not isinstance(template, bool):
        return int(raw)
    if isinstance(template, float):
        return float(raw)
    if isinstance(template, str):
        return raw

    if isinstance(template, tuple):
        data = json.loads(raw)
        if not isinstance(data, (list, tuple)):
            raise ValueError("Tuple 參數請輸入 JSON 陣列")
        if not template:
            return tuple(data)
        sample = template[0]
        return tuple(_coerce_loaded(item, sample) for item in data)

    if isinstance(template, list):
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError("List 參數請輸入 JSON 陣列")
        if not template:
            return data
        return [_coerce_loaded(item, template[0]) for item in data]

    if isinstance(template, Mapping):
        data = json.loads(raw)
        if not isinstance(data, Mapping):
            raise ValueError("Mapping 參數請輸入 JSON 物件")
        result: Dict[str, Any] = {}
        for key, item in data.items():
            if key in template:
                result[str(key)] = _coerce_loaded(item, template[key])
            else:
                result[str(key)] = item
        return result

    try:
        return json.loads(raw)
    except Exception:
        return raw


def _coerce_loaded(value: Any, template: Any) -> Any:
    if template is None:
        return value
    if isinstance(template, bool):
        if isinstance(value, bool):
            return value
        return _parse_user_text(str(value), template)
    if isinstance(template, int) and not isinstance(template, bool):
        return int(value)
    if isinstance(template, float):
        return float(value)
    if isinstance(template, str):
        return str(value)
    if isinstance(template, tuple):
        if not isinstance(value, (list, tuple)):
            raise ValueError("設定格式錯誤：預期陣列")
        if not template:
            return tuple(value)
        sample = template[0]
        return tuple(_coerce_loaded(item, sample) for item in value)
    if isinstance(template, list):
        if not isinstance(value, list):
            raise ValueError("設定格式錯誤：預期陣列")
        if not template:
            return list(value)
        return [_coerce_loaded(item, template[0]) for item in value]
    if isinstance(template, Mapping):
        if not isinstance(value, Mapping):
            raise ValueError("設定格式錯誤：預期物件")
        output: Dict[str, Any] = {}
        for key, item in value.items():
            output[str(key)] = (
                _coerce_loaded(item, template[key]) if key in template else item
            )
        return output
    return value


@dataclass(frozen=True)
class GUISettings:
    result_expiry_seconds: float = 5.0
    control_mode: str = "MANUAL"
    api_mode: str = "LIVE"
    replay_dir: str = "api_replay"

    market_refresh_seconds: float = 30.0
    # 僅控制 GUI 顯示刷新；行情事件由 Fugle WebSocket 驅動。
    quote_refresh_seconds: float = 3.0
    intraday_watch_count: int =30
    auto_execute: bool = False

    # Fugle Stream 設定。API Token 不屬於參數結構，也不會保存到設定檔。
    fugle_websocket_enabled: bool = True
    fugle_rest_seed_enabled: bool = True
    fugle_event_freshness_seconds: float = 3.0
    fugle_stream_health_timeout_seconds: float = 30.0
    fugle_reconnect_min_seconds: float = 30.0
    fugle_reconnect_max_seconds: float = 60.0
    fugle_subscription_limit: int = 600
    fugle_candidate_subscription_enabled: bool = True
    fugle_books_subscription_mode: str = "P0_P1"

    manual_market_state: str = "NORMAL"
    manual_trading_mode: str = "SELECTIVE_LONG"

    available_cash: float = 0.0
    equity: float = 0.0
    daily_trade_count: int = 0
    daily_pnl: float = 0.0

    cts_trade_das: str = "api.win168.com.tw/tradedas"
    cts_uid: str = ""
    cts_account_id: str = ""
    cts_com_prog_id: str = ""

    def __post_init__(self) -> None:
        # 舊設定載入時升級為必要的持倉＋候選五檔資料契約。
        if str(self.fugle_books_subscription_mode).strip().upper() in {"OFF", "P0", "ALL", "P0_P1"}:
            object.__setattr__(self, "fugle_books_subscription_mode", "P0_P1")

    def validate(self) -> None:
        if not 0 < float(self.result_expiry_seconds) <= 25:
            raise ValueError("計算結果有效期限必須大於零且不超過二十五秒")
        mode = str(self.control_mode).strip().upper()
        if mode not in VALID_CONTROL_MODES:
            raise ValueError("control_mode 僅允許 AUTO / MANUAL")
        api_mode = str(self.api_mode).strip().upper()
        if api_mode not in VALID_API_MODES:
            raise ValueError("api_mode 僅允許 LIVE / OFFLINE")
        if not str(self.replay_dir).strip():
            raise ValueError("replay_dir 不得為空")
        if float(self.market_refresh_seconds) <= 0:
            raise ValueError("market_refresh_seconds 必須 > 0")
        if float(self.quote_refresh_seconds) <= 0:
            raise ValueError("quote_refresh_seconds 必須 > 0")
        if int(self.intraday_watch_count) <= 0:
            raise ValueError("intraday_watch_count 必須 > 0")
        if float(self.fugle_event_freshness_seconds) <= 0:
            raise ValueError("fugle_event_freshness_seconds 必須 > 0")
        if float(self.fugle_stream_health_timeout_seconds) <= 0:
            raise ValueError("fugle_stream_health_timeout_seconds 必須 > 0")
        if float(self.fugle_reconnect_min_seconds) <= 0:
            raise ValueError("fugle_reconnect_min_seconds 必須 > 0")
        if float(self.fugle_reconnect_max_seconds) < float(
            self.fugle_reconnect_min_seconds
        ):
            raise ValueError(
                "fugle_reconnect_max_seconds 不得小於 fugle_reconnect_min_seconds"
            )
        if int(self.fugle_subscription_limit) <= 0:
            raise ValueError("fugle_subscription_limit 必須 > 0")
        books_mode = str(self.fugle_books_subscription_mode).strip().upper()
        if books_mode not in VALID_FUGLE_BOOKS_SUBSCRIPTION_MODES:
            raise ValueError(
                "fugle_books_subscription_mode 固定為 P0_P1"
            )
        if (
            str(self.manual_market_state).strip().upper()
            not in VALID_MANUAL_MARKET_STATES
        ):
            raise ValueError("manual_market_state 值不合法")
        if (
            str(self.manual_trading_mode).strip().upper()
            not in VALID_MANUAL_TRADING_MODES
        ):
            raise ValueError("manual_trading_mode 值不合法")
        if float(self.available_cash) < 0 or float(self.equity) < 0:
            raise ValueError("available_cash / equity 不得 < 0")
        if int(self.daily_trade_count) < 0:
            raise ValueError("daily_trade_count 不得 < 0")
        if not math.isfinite(float(self.daily_pnl)):
            raise ValueError("daily_pnl 必須是有限數值")


class ParameterBundle:
    def __init__(
        self,
        base_dir: Path,
        module_key: str,
        module: Any,
        class_name: str,
    ) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.module_key = module_key
        self.module = module
        self.class_name = class_name
        self.parameter_class = getattr(module, class_name)
        self.default = self.parameter_class()
        self.current = self.parameter_class()
        constraints = _extract_module_constraints(module, self.parameter_class)
        usages = _extract_parameter_usage_functions(module, self.parameter_class)
        self._parameter_descriptions = {
            name: _compose_parameter_description(
                self.base_dir,
                self.module_key,
                name,
                constraints.get(name, ""),
                usages.get(name, ()),
            )
            for name in self.default_values()
        }
        self._validate_instance(self.default)
        self._validate_instance(self.current)

    @staticmethod
    def _validate_instance(instance: Any) -> None:
        validator = getattr(instance, "validate", None)
        if callable(validator):
            validator()

    def default_values(self) -> Dict[str, Any]:
        return {
            field.name: getattr(self.default, field.name)
            for field in fields(self.default)
        }

    def current_values(self) -> Dict[str, Any]:
        return {
            field.name: getattr(self.current, field.name)
            for field in fields(self.current)
        }

    def parameter_description(self, name: str) -> str:
        return self._parameter_descriptions.get(
            name,
            _usage_fallback_description(self.module_key, name, ()),
        )

    def build(self, values: Mapping[str, Any]) -> Any:
        defaults = self.default_values()
        values = dict(values)
        if self.module_key == "scanner":
            old_weight = values.get("momentum_weight")
            if isinstance(old_weight, Mapping) and set(old_weight) == {
                "change",
                "market_relative_strength",
                "sector_relative_strength",
                "near_intraday_high",
            }:
                values["momentum_weight"] = copy.deepcopy(defaults["momentum_weight"])
        if self.module_key == "trading":
            values["cts_price_type"] = 4
        kwargs: Dict[str, Any] = {}
        for name, template in defaults.items():
            if name in values:
                kwargs[name] = _coerce_loaded(values[name], template)
            else:
                kwargs[name] = copy.deepcopy(template)
        instance = self.parameter_class(**kwargs)
        self._validate_instance(instance)
        return instance


    def set_field_from_text(self, name: str, text: str) -> None:
        current_values = self.current_values()
        if name not in current_values:
            raise KeyError(name)
        template = self.default_values()[name]
        current_values[name] = _parse_user_text(text, template)
        self.current = self.build(current_values)

    def restore_defaults(self) -> None:
        self.current = self.parameter_class()
        self._validate_instance(self.current)


class ParameterRegistry:
    def __init__(
        self,
        modules: Mapping[str, Any],
        base_dir: Optional[Path] = None,
    ) -> None:
        self.base_dir = Path(base_dir or Path.cwd()).resolve()
        self.bundles: Dict[str, ParameterBundle] = {}
        for module_key in ("market", "scanner", "intraday", "trading"):
            spec = PARAMETER_GROUP_SPECS[module_key]
            class_name = spec["parameter_class"]
            self.bundles[module_key] = ParameterBundle(
                self.base_dir,
                module_key,
                modules[module_key],
                class_name,
            )

    def validate_all(self) -> None:
        for bundle in self.bundles.values():
            bundle._validate_instance(bundle.current)

    def restore_all(self) -> None:
        for bundle in self.bundles.values():
            bundle.restore_defaults()

    def snapshot(self) -> Dict[str, Any]:
        return {
            key: _jsonable(bundle.current_values())
            for key, bundle in self.bundles.items()
        }

    def fingerprints(self) -> Dict[str, str]:
        """四個核心模組的參數 fingerprint；不含任何 GUI 顯示刷新設定。"""
        snapshot = self.snapshot()
        return {key: _stable_hash(snapshot[key]) for key in sorted(snapshot)}

    def core_fingerprint(self) -> str:
        return _stable_hash(
            {
                "parameters": self.snapshot(),
                "module_fingerprints": self.fingerprints(),
            }
        )

    def load_snapshot(self, payload: Mapping[str, Any]) -> None:
        new_instances: Dict[str, Any] = {}
        thresholds_current = payload.get("_threshold_policy") == "panic_and_candidate_gates"
        for key, bundle in self.bundles.items():
            values = payload.get(key, {})
            if not isinstance(values, Mapping):
                raise ValueError(f"parameters.{key} 必須是 JSON object")
            values = dict(values)
            if key == "intraday" and payload.get("_momentum_wave_policy") != "REARMABLE_FIVE_BAR" and values.get("max_entry_wave_bars") in (3, "3"):
                values["max_entry_wave_bars"] = 5
            if not thresholds_current:
                keys = {"market": ("panic_taiex_drop_pct", "panic_tpex_drop_pct", "panic_breadth_ratio"),
                        "scanner": ("min_score", "min_score_coverage")}.get(key, ())
                defaults = bundle.default_values()
                for name in keys:
                    values[name] = defaults[name]
            new_instances[key] = bundle.build(values)
        for key, instance in new_instances.items():
            self.bundles[key].current = instance


def _signature_accepts_parameter(callable_obj: Any, parameter_name: str) -> bool:
    if not callable(callable_obj):
        return False
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return False
    return parameter_name in signature.parameters


def build_core_parameter_link_report(
    modules: Mapping[str, Any],
    registry: ParameterRegistry,
) -> Dict[str, Any]:
    report: Dict[str, Any] = {}
    all_ok = True

    api_module = modules.get("api_gateway")
    gateway_class = getattr(api_module, "APIGateway", None)
    api_mode_ok = _signature_accepts_parameter(gateway_class, "mode")
    replay_dir_ok = _signature_accepts_parameter(gateway_class, "replay_dir")
    freshness_ok = _signature_accepts_parameter(gateway_class, "freshness_seconds")
    subscribe_market_ok = callable(getattr(gateway_class, "subscribe_market", None))
    api_ok = bool(
        gateway_class is not None
        and api_mode_ok
        and replay_dir_ok
        and freshness_ok
        and subscribe_market_ok
        and callable(getattr(gateway_class, "set_book_subscriptions", None))
    )
    report["api_gateway"] = {
        "ok": api_ok,
        "parameter_model": "GUISettings -> APIGateway / CTS",
        "gui_links": {
            "api_mode": "APIGateway(mode=...)",
            "replay_dir": "APIGateway(replay_dir=...)",
            "fugle_event_freshness_seconds": "APIGateway(freshness_seconds=...)",
            "fugle_websocket_enabled": "runtime -> APIGateway.fugle_login()/subscribe_market()",
            "fugle_rest_seed_enabled": "runtime -> subscribe_market(initialize=...)",
            "fugle_reconnect_min_seconds": "FugleMarketDataClient reconnect_min_seconds",
            "fugle_reconnect_max_seconds": "FugleMarketDataClient reconnect_max_seconds",
            "fugle_subscription_limit": "runtime subscription policy",
            "fugle_candidate_subscription_enabled": "runtime P1 subscription policy",
            "fugle_books_subscription_mode": "runtime set_book_subscriptions(P0 + P1)",
            "cts_trade_das": "CTS.login(trade_das)",
            "cts_uid": "CTS.login(uid)",
            "cts_account_id": "Trading/CTS account_id",
            "cts_com_prog_id": "CTS COM ProgID",
        },
        "APIGateway_mode": api_mode_ok,
        "APIGateway_replay_dir": replay_dir_ok,
        "APIGateway_freshness_seconds": freshness_ok,
        "APIGateway_subscribe_market": subscribe_market_ok,
        "APIGateway_set_book_subscriptions": callable(getattr(gateway_class, "set_book_subscriptions", None)),
        "runtime_module_contract": "runtime",
        "legacy_runtime_contract": False,
        "stream_parameter_note": (
            "Stream 欄位已納入單一 GUISettings；是否真正套用由 runtime 接線報告確認，"
            "本模組不自行控制 WebSocket。"
        ),
    }
    all_ok = all_ok and api_ok

    entry_points = {
        "market": ("get_market_context",),
        "scanner": ("get_candidates",),
        "intraday": ("get_signal_context",),
        "trading": (
            "evaluate_trading_decision",
            "execute_allowed_decision",
            "maintain_time_exit_orders",
        ),
    }
    for module_key, function_names in entry_points.items():
        module = modules.get(module_key)
        bundle = registry.bundles.get(module_key)
        spec = PARAMETER_GROUP_SPECS[module_key]
        class_name = spec["parameter_class"]
        parameter_class = (
            getattr(module, class_name, None) if module is not None else None
        )
        class_fields = (
            [item.name for item in fields(parameter_class())]
            if parameter_class is not None and is_dataclass(parameter_class)
            else []
        )
        registry_fields = list(bundle.current_values()) if bundle is not None else []
        missing_registry_fields = [
            name for name in class_fields if name not in registry_fields
        ]
        extra_registry_fields = [
            name for name in registry_fields if name not in class_fields
        ]
        parameter_entries = {
            name: _signature_accepts_parameter(
                getattr(module, name, None),
                "parameters",
            )
            for name in function_names
        }
        module_ok = bool(
            bundle is not None
            and class_fields
            and not missing_registry_fields
            and not extra_registry_fields
            and all(parameter_entries.values())
        )
        hidden_gui_fields = sorted(
            name
            for name in PARAMETER_TABLE_HIDDEN_FIELDS.get(
                module_key,
                frozenset(),
            )
            if name in class_fields
        )
        visible_gui_fields = [
            name for name in class_fields if name not in hidden_gui_fields
        ]
        report[module_key] = {
            "ok": module_ok,
            "parameter_class": class_name,
            "core_field_count": len(class_fields),
            "gui_registry_field_count": len(registry_fields),
            "gui_visible_field_count": len(visible_gui_fields),
            "core_fields": class_fields,
            "gui_visible_fields": visible_gui_fields,
            "gui_hidden_internal_fields": hidden_gui_fields,
            "compatibility_only_fields": [],
            "missing_registry_fields": missing_registry_fields,
            "extra_registry_fields": extra_registry_fields,
            "runtime_parameter_entries": parameter_entries,
            "gui_table_policy": "DATACLASS_FIELDS_EXCEPT_INTERNAL_FIXED_RULES",
        }
        all_ok = all_ok and module_ok

    report["ok"] = all_ok
    return report


def _gui_settings_payload(settings: GUISettings) -> Dict[str, Any]:
    return _jsonable(asdict(settings))


def build_parameter_fingerprints(
    settings: GUISettings,
    registry: ParameterRegistry,
) -> Dict[str, Any]:
    """分離顯示、Stream 與核心參數，避免 GUI refresh 污染事件計算 fingerprint。"""
    settings.validate()
    registry.validate_all()
    gui_payload = _gui_settings_payload(settings)
    display_payload = {
        name: gui_payload.get(name)
        for name in sorted(GUI_DISPLAY_ONLY_FIELDS)
        if name in gui_payload
    }
    stream_payload = {
        name: gui_payload.get(name)
        for name in sorted(FUGLE_STREAM_SETTING_FIELDS)
        if name in gui_payload
    }
    core_snapshot = registry.snapshot()
    module_fingerprints = registry.fingerprints()
    core_fingerprint = registry.core_fingerprint()
    event_runtime_fingerprint = _stable_hash(
        {
            "stream": stream_payload,
            "parameters": core_snapshot,
            "module_fingerprints": module_fingerprints,
        }
    )
    return {
        "gui": _stable_hash(gui_payload),
        "display": _stable_hash(display_payload),
        "stream": _stable_hash(stream_payload),
        "core": core_fingerprint,
        "event_runtime": event_runtime_fingerprint,
        "modules": module_fingerprints,
    }


class ConfigStore:
    def __init__(self, base_dir: Path) -> None:
        self.root = Path(base_dir) / PARAMETER_ROOT_NAME
        self.history_dir = self.root / PARAMETER_HISTORY_DIR
        self.latest_path = self.root / LATEST_PARAMETER_FILE

    def _ensure_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.history_dir.mkdir(parents=True, exist_ok=True)

    def save(
        self,
        settings: GUISettings,
        registry: ParameterRegistry,
        module_files: Mapping[str, Path],
    ) -> Path:
        settings.validate()
        registry.validate_all()
        self._ensure_dirs()

        payload: Dict[str, Any] = {
            "schema": 2,
            "saved_at": _now_text(),
            "module_files": {key: path.name for key, path in module_files.items()},
            "gui": _gui_settings_payload(settings),
            "parameters": {**registry.snapshot(), "_threshold_policy": "panic_and_candidate_gates", "_momentum_wave_policy": "REARMABLE_FIVE_BAR"},
        }
        payload["fingerprints"] = build_parameter_fingerprints(settings, registry)
        payload["fingerprint"] = _stable_hash(
            {
                "gui": payload["gui"],
                "parameters": payload["parameters"],
                "fingerprints": payload["fingerprints"],
            }
        )

        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        self.latest_path.write_text(text, encoding="utf-8")

        timestamp = datetime.now(timezone(timedelta(hours=8))).strftime(
            "%Y%m%d_%H%M%S_%f"
        )
        history_path = (
            self.history_dir / f"parameters_{timestamp}_{payload['fingerprint']}.json"
        )
        history_path.write_text(text, encoding="utf-8")
        return history_path

    def load_path(
        self,
        path: Path,
        registry: ParameterRegistry,
    ) -> GUISettings:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("參數檔根節點必須是 JSON object")
        gui_values = payload.get("gui")
        parameter_values = payload.get("parameters")
        if not isinstance(gui_values, Mapping) or not isinstance(
            parameter_values, Mapping
        ):
            raise ValueError("參數檔缺少 gui / parameters")

        default_settings = GUISettings()
        gui_kwargs: Dict[str, Any] = {}
        for field in fields(default_settings):
            template = getattr(default_settings, field.name)
            gui_kwargs[field.name] = _coerce_loaded(
                gui_values.get(field.name, template),
                template,
            )
        settings = GUISettings(**gui_kwargs)
        settings.validate()

        registry.load_snapshot(parameter_values)
        registry.validate_all()
        return settings

    def load_latest(self, registry: ParameterRegistry) -> Optional[GUISettings]:
        if not self.latest_path.exists():
            return None
        return self.load_path(self.latest_path, registry)

    def list_versions(self) -> List[Path]:
        if not self.history_dir.exists():
            return []
        return sorted(self.history_dir.glob("parameters_*.json"), reverse=True)


class ParameterSettingsComponent:
    """獨立的參數設定 UI Component；只透過 callback 與主 GUI 協作。"""

    def __init__(
        self,
        *,
        base_dir: Path,
        root: Any,
        tk: Any,
        ttk: Any,
        messagebox: Any,
        registry: ParameterRegistry,
        store: ConfigStore,
        get_settings: Callable[[], GUISettings],
        apply_settings: Callable[..., None],
        read_control_values: Callable[[], Mapping[str, Any]],
        effective_control_mode: Callable[..., str],
        module_files_provider: Callable[[], Mapping[str, Path]],
        set_message: Callable[[str], None],
        request_refresh: Callable[[bool], None],
    ) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.root = root
        self.tk = tk
        self.ttk = ttk
        self.messagebox = messagebox
        self.registry = registry
        self.store = store
        self.get_settings = get_settings
        self.apply_settings_callback = apply_settings
        self.read_control_values = read_control_values
        self.effective_control_mode = effective_control_mode
        self.module_files_provider = module_files_provider
        self.set_message = set_message
        self.request_refresh = request_refresh
        self.parameter_trees: Dict[str, Any] = {}
        self.parameter_tabs: Dict[str, Any] = {}

    def build(self, parent: Any) -> None:
        ttk = self.ttk
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Button(toolbar, text="驗證全部", command=self.validate_all).pack(
            side="left",
            padx=3,
        )
        ttk.Button(toolbar, text="儲存", command=self.save).pack(side="left", padx=3)
        ttk.Button(toolbar, text="載入最新", command=self.load_latest).pack(
            side="left",
            padx=3,
        )
        ttk.Button(toolbar, text="還原預設值", command=self.restore_defaults).pack(
            side="left",
            padx=3,
        )
        ttk.Button(toolbar, text="版本紀錄", command=self.show_versions).pack(
            side="left",
            padx=3,
        )

        note = "雙擊目前值修改；完整說明可捲動。行情由事件驅動，密碼不保存。"
        ttk.Label(toolbar, text=note, wraplength=480).pack(side="left", padx=12)

        notebook = ttk.Notebook(parent)
        notebook.pack(fill="both", expand=True)

        gui_tab = ttk.Frame(notebook, padding=6)
        notebook.add(gui_tab, text="GUI／帳戶資訊")
        self.parameter_tabs["gui"] = gui_tab
        self.parameter_trees["gui"] = self._parameter_tree(gui_tab, "gui")

        for key in ("market", "scanner", "intraday", "trading"):
            tab = ttk.Frame(notebook, padding=6)
            notebook.add(tab, text=PARAMETER_GROUP_SPECS[key]["title"])
            self.parameter_tabs[key] = tab
            self.parameter_trees[key] = self._parameter_tree(tab, key)

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
            frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        vertical = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(
            yscrollcommand=vertical.set,
            xscrollcommand=horizontal.set,
        )
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        for column, heading, width in zip(columns, headings, widths):
            tree.heading(column, text=heading)
            tree.column(column, width=width, minwidth=50, anchor="center")
        return tree

    def _parameter_tree(self, parent: Any, group: str) -> Any:
        tree = self._tree(
            parent,
            columns=("name", "current", "default", "description"),
            headings=("參數", "目前生效值", "預設值", "重點說明"),
            widths=(230, 155, 155, 650),
        )
        tree.bind(
            "<Double-1>",
            lambda event, selected_group=group: self._edit_parameter(
                event,
                selected_group,
            ),
        )
        return tree

    def gui_current_values(self) -> Dict[str, Any]:
        settings = self.get_settings()
        return {field.name: getattr(settings, field.name) for field in fields(settings)}

    def refresh(self) -> None:
        if not self.parameter_trees:
            return
        gui_defaults = GUISettings()
        self._fill_parameter_tree(
            self.parameter_trees["gui"],
            self.gui_current_values(),
            {
                field.name: getattr(gui_defaults, field.name)
                for field in fields(gui_defaults)
            },
            "gui",
        )
        for key, bundle in self.registry.bundles.items():
            self._fill_parameter_tree(
                self.parameter_trees[key],
                bundle.current_values(),
                bundle.default_values(),
                key,
            )

    def _parameter_description(self, group: str, name: str) -> str:
        if group == "gui":
            return _parameter_help_text(self.base_dir, group, name) or (
                "GUI 設定直接控制操作介面或帳戶風控輸入，"
                "並由 GUISettings.validate() 驗證。"
            )
        bundle = self.registry.bundles.get(group)
        if bundle is None:
            return "核心模組負責此參數的策略用途與驗證。"
        return bundle.parameter_description(name)

    def _parameter_summary(self, group: str, name: str) -> str:
        text = re.sub(r"\s+", " ", self._parameter_description(group, name)).strip()
        purpose = text.split("。", 1)[0]
        return purpose if len(purpose) <= 42 else purpose[:39] + "…"

    def _fill_parameter_tree(
        self,
        tree: Any,
        current: Mapping[str, Any],
        defaults: Mapping[str, Any],
        group: str,
    ) -> None:
        tree.delete(*tree.get_children())
        hidden_fields = PARAMETER_TABLE_HIDDEN_FIELDS.get(group, frozenset())
        for name in current:
            if name in hidden_fields:
                continue
            tree.insert(
                "",
                "end",
                iid=str(name),
                values=(
                    name,
                    _display_value(current[name]),
                    _display_value(defaults.get(name)),
                    self._parameter_summary(group, name),
                ),
            )

    def _edit_parameter(self, event: Any, group: str) -> None:
        tree = self.parameter_trees[group]
        item_id = tree.identify_row(event.y)
        column = tree.identify_column(event.x)
        if not item_id or column != "#2":
            return
        current_text = tree.set(item_id, "current")
        self._show_parameter_editor(group, item_id, current_text)

    def _show_parameter_editor(
        self,
        group: str,
        name: str,
        current_text: str,
    ) -> None:
        window = self.tk.Toplevel(self.root)
        window.title(f"修改參數｜{name}")
        window.transient(self.root)
        window.grab_set()
        window.geometry("620x360")
        window.minsize(420, 300)
        window.resizable(True, True)
        ttk = self.ttk

        ttk.Label(window, text=name).pack(anchor="w", padx=12, pady=(12, 4))
        value_var = self.tk.StringVar(value=current_text)
        entry = ttk.Entry(window, textvariable=value_var, width=28)
        entry.pack(anchor="w", padx=12, pady=(0, 6))
        entry.selection_range(0, "end")
        entry.focus_set()

        # 操作列先固定於底部；說明只使用中間剩餘空間。
        buttons = ttk.Frame(window)
        buttons.pack(side="bottom", fill="x", padx=12, pady=(6, 10))
        help_frame = ttk.Frame(window)
        help_frame.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        help_text = self.tk.Text(help_frame, height=8, wrap="word", takefocus=False)
        scrollbar = ttk.Scrollbar(help_frame, orient="vertical", command=help_text.yview)
        help_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        help_text.pack(side="left", fill="both", expand=True)
        help_text.insert("1.0", "說明：" + self._parameter_description(group, name))
        help_text.configure(state="disabled")

        def apply_edit() -> None:
            raw = value_var.get().strip()
            try:
                if group == "gui":
                    defaults = GUISettings()
                    template = getattr(defaults, name)
                    values = self.gui_current_values()
                    values[name] = _parse_user_text(raw, template)
                    new_settings = GUISettings(**values)
                    new_settings.validate()
                    requested_mode = (
                        str(new_settings.control_mode).strip().upper()
                        if name == "control_mode"
                        else None
                    )
                    self.apply_settings_callback(
                        new_settings,
                        requested_mode=requested_mode,
                        announce=name == "control_mode",
                        apply_session_policy=True,
                    )
                else:
                    self.registry.bundles[group].set_field_from_text(name, raw)
                self.refresh()
                self.set_message(f"已套用參數：{group}.{name}")
                window.destroy()
                self.request_refresh(False)
            except Exception as exc:
                self.messagebox.showerror(
                    "參數錯誤",
                    f"{type(exc).__name__}: {exc}",
                    parent=window,
                )

        ttk.Button(buttons, text="套用並驗證", command=apply_edit).pack(side="right")
        ttk.Button(buttons, text="取消", command=window.destroy).pack(
            side="right",
            padx=6,
        )
        entry.bind("<Return>", lambda _event: apply_edit())

    def sync_control_settings(self) -> GUISettings:
        control_values = dict(self.read_control_values() or {})
        requested_mode = (
            str(control_values.get("control_mode") or self.get_settings().control_mode)
            .strip()
            .upper()
        )
        if requested_mode not in VALID_CONTROL_MODES:
            raise ValueError("control_mode 僅允許 AUTO / MANUAL")

        values = self.gui_current_values()
        values["control_mode"] = self.effective_control_mode(
            requested_mode=requested_mode
        )
        values["auto_execute"] = bool(
            control_values.get("auto_execute", self.get_settings().auto_execute)
        )
        settings = GUISettings(**values)
        settings.validate()
        self.apply_settings_callback(
            settings,
            requested_mode=requested_mode,
            announce=False,
            apply_session_policy=False,
        )
        return self.get_settings()

    def validate_all(self) -> None:
        try:
            settings = self.get_settings()
            settings.validate()
            self.registry.validate_all()
            self.apply_settings_callback(
                settings,
                requested_mode=None,
                announce=False,
                apply_session_policy=False,
            )
            self.set_message("所有 GUI／策略／風控參數驗證通過")
        except Exception as exc:
            self.messagebox.showerror(
                "驗證失敗",
                f"{type(exc).__name__}: {exc}",
            )

    def save(self) -> None:
        try:
            settings = self.sync_control_settings()
            path = self.store.save(
                settings,
                self.registry,
                self.module_files_provider(),
            )
            self.refresh()
            self.set_message(f"參數已儲存並建立版本：{path.name}")
        except Exception as exc:
            self.messagebox.showerror(
                "儲存失敗",
                f"{type(exc).__name__}: {exc}",
            )

    def load_latest(self) -> None:
        try:
            settings = self.store.load_latest(self.registry)
            if settings is None:
                self.messagebox.showinfo("載入參數", "尚無已保存參數。")
                return
            self.apply_settings_callback(
                settings,
                requested_mode=str(settings.control_mode).strip().upper(),
                announce=False,
                apply_session_policy=True,
            )
            self.refresh()
            self.set_message("已載入最近一次保存且驗證通過的參數")
        except Exception as exc:
            self.messagebox.showerror(
                "載入失敗",
                f"{type(exc).__name__}: {exc}",
            )

    def restore_defaults(self) -> None:
        if not self.messagebox.askyesno(
            "還原預設值",
            "確定要還原 GUI 與四個核心參數的預設值？",
        ):
            return
        settings = GUISettings()
        self.registry.restore_all()
        self.apply_settings_callback(
            settings,
            requested_mode="MANUAL",
            announce=False,
            apply_session_policy=True,
        )
        self.refresh()
        self.set_message("已還原預設值；尚未儲存")

    def show_versions(self) -> None:
        versions = self.store.list_versions()
        window = self.tk.Toplevel(self.root)
        window.title("參數版本紀錄")
        window.geometry("900x520")
        tree = self._tree(
            window,
            columns=("file", "saved_at", "fingerprint"),
            headings=("版本檔", "保存時間", "Fingerprint"),
            widths=(480, 220, 160),
        )
        for path in versions:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                saved_at = payload.get("saved_at", "")
                fingerprint = payload.get("fingerprint", "")
            except Exception:
                saved_at = "讀取失敗"
                fingerprint = ""
            tree.insert(
                "",
                "end",
                iid=str(path),
                values=(path.name, saved_at, fingerprint),
            )

        def load_selected() -> None:
            selection = tree.selection()
            if not selection:
                return
            path = Path(selection[0])
            try:
                settings = self.store.load_path(path, self.registry)
                self.apply_settings_callback(
                    settings,
                    requested_mode=str(settings.control_mode).strip().upper(),
                    announce=False,
                    apply_session_policy=True,
                )
                self.refresh()
                self.set_message(f"已載入版本：{path.name}")
                window.destroy()
            except Exception as exc:
                self.messagebox.showerror(
                    "載入版本失敗",
                    f"{type(exc).__name__}: {exc}",
                    parent=window,
                )

        self.ttk.Button(
            window,
            text="載入選取版本",
            command=load_selected,
        ).pack(pady=8)

# 更新摘要：持倉上限預設仍四檔，設定上限不代表可突破既有資金或風險限制。
