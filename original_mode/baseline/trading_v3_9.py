# -*- coding: utf-8 -*-
"""
trading.py - 交易執行、倉位與風險控制模組

用途
----
本模組接收 intraday.py 產生之盤中進出場訊號，搭配 APIGateway 即時行情、
帳戶狀態與固定風險參數，回答：

    「雖然有 BUY / EXIT 訊號，但現在到底能不能真的下單？」

主要責任
--------
1. 重新確認市場多方許可、候選股與當沖資格。
2. 比較 signal_price / current_price / bid / ask，限制價差、滑價與追價。
3. 檢查 available_cash、既有持倉、最大持倉數、每日交易次數與每日損益。
4. 依資金上限與單筆風險上限計算 position_size。
5. BUY 送單前讀最新 MarketState；SELL 共用 Atomic／REST／Gateway／有效快取，通過 Revalidation 才建立 CTSOrderRequest。
6. 只在風控結果與 BUY Revalidation 均通過時，經由 api_gateway.py 送出委託。
7. 成交後使用 CTS 實際成交均價建立 entry_price、stop_price、take_profit_price。
8. STOP / TAKE PROFIT 門檻絕不以 Intraday signal_price 當成最終成交成本。
9. 支援 CTS 成交、委託與庫存回傳標準欄位解析，供 GUI / 主程式對帳。
10. SELL 市價委託未成交／部分成交時，持續查詢委託、成交與庫存。
11. SELL 由送單鎖、委託登錄及庫存核對，避免重複賣出或超賣。

資料邊界
--------
- api_gateway.py：行情取得、CTS 連線、委託與成交／庫存查詢。
- market.py：市場環境與 allow_long。
- scanner.py：候選股與 day_trade_eligible。
- intraday.py：ENTRY / EXIT 訊號。
- trading.py：最後交易允許／拒絕、倉位、CTS 委託與成交後風控價格。

可重現核心
----------
Trade_Decision = f(Signal_Context, Atomic_MarketState, Account_State, Trading_Parameters)

相同訊號、相同 atomic MarketState、相同帳戶狀態與相同風控參數，必須得到相同：
- ALLOW / REJECT
- rejection_codes / rejection_reasons
- order_price
- position_size
- CTS trade_type / quantity / condition
- 預估執行摩擦

成交後：
Position = f(CTS_Fill, Trading_Parameters)

相同 CTS 實際成交均價與相同停損／停利參數，必須得到相同 stop_price / take_profit_price。

注意
----
目前提供的 APIGateway 沒有「可用現金、當日損益、每日交易次數」的帳務查詢封裝，
因此這三項由 GUI / 主程式以 account_state 明確傳入；Trading 不會自行猜測或以 0 冒充。

CLI
---
python trading.py --self-test
python trading.py --diagnose
python trading.py --json

中文重點更新
------------
- 保留 Intraday 出場原因及觸發時行情區間，讓 CTS 成交能追溯停損、停利或動能轉弱。
- 自動買進沿用 Intraday 波段起點，送單前計算最新可執行價格的整波延伸。
"""

# 中文重點更新：送單前及委託檢查點後重讀最新市場與類股許可，保留市價、漲幅上限、行情、風控與委託鎖。

# 中文重點更新：清理歷史相容參數、孤立註解及無效追單顯示，保留測試、診斷、CTS 撤單與委託及持倉對帳。

# 中文重點更新：清除未使用函式、相容介面與無效參數；保留市價買賣、成交對帳、撤買單、清倉及離線驗證。

# 中文重點更新：移除無效固定停損停利參數；清倉後維護未結系統委託，撤買單與成交追蹤保持單序列及身分核對。

# 中文重點更新：新增結構風險部位計算、策略再確認、分批出場、總風險與冷卻，成交身分可供重啟還原。

# 中文重點更新：補齊審查要求之狀態識別、整張委託、資料時序與恢復安全邊界。

# 中文重點更新：Trend BUY 再驗證及 CTS 呼叫前重讀市場許可；保留個股 TREND_RESUME 與結構檢查。

# 中文重點更新：新增兩策略共用當日漲幅上限與送單前驗證，保留原追價限制並統一市場資料安全判定。

# 中文重點更新：Trend 首次進場與 Resume 成對再驗證，保留既有追價、風控及買賣 API 介面。

# 中文重點更新：保存進場指數基準至成交持倉與恢復資料，供成交後 RS 出場證據比對；保留首次 Trend 進場驗證。

# 中文重點更新：逐層保留主要拒絕與 Gate 實值，SELL 行情可信度降級；未知委託持續鎖定至對帳或人工處理。

# 中文重點更新：拒單與送單前失敗明確分類；成交去重、狀態單向合併與跨帳號委託辨識。

# 中文重點更新：進場訊號允許設定範圍內回落，保留結構複驗並記錄下限與最新成交價；買價沿用送單前最新快照 Ask。

# 中文重點更新：有效委託與持倉取代歷史訊號永久鎖；區分送單前拒絕、券商受理與未知，記錄 Intent 全流程。

# 中文重點更新：接受兩策略自訂停利原因並固定為全數 SELL；沿用原 SELL 複驗、可賣數量與整張限制，不重新計算獲利率。

# 中文重點更新：自動與手動最終委託改為市價；賣出前重查實際庫存；市價價格與預留資金分離，成交成本只來自 CTS。

# 中文重點更新：BUY 分別驗證 Trade 與 Book 來源時效；送單準備後再次 fail-closed 驗證。

from __future__ import annotations

import traceback
import uuid
from functools import lru_cache
import argparse
import hashlib
import importlib
import inspect
import json
import math
import os
import re
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# =============================================================================
# 固定參數
# =============================================================================

# 1～2% 極短線的核心出場參數；最終價格一定以 CTS 實際成交均價為基準。

# 執行品質：價差 + 滑價 + 追價合計預設不得超過 0.50%。
MAX_SPREAD_PCT = 0.30
MAX_SLIPPAGE_PCT = 0.20
MAX_CHASE_PCT = 0.40
MAX_EXECUTION_FRICTION_PCT = 0.50

# BUY 真正送進 CTS 前，只以單一 atomic MarketState 重新確認即將下單的股票。
BUY_REVALIDATION_MAX_QUOTE_AGE_SECONDS = 25.0
BUY_REVALIDATION_MAX_BOOK_AGE_SECONDS = 3.0
MAX_BREAKOUT_EXTENSION_PCT = 0.80
# 進場訊號成立後，允許最新成交價相對訊號價最多回落百分比。
# 0 = 不允許跌破訊號價；1.5 = 最多允許回落 1.5%。
MAX_ENTRY_TRIGGER_RETRACE_PCT = 1.50

# 資金與風控。
MAX_BUY_PRICE = 150.0
MAX_POSITION_COUNT = 4
MAX_DAILY_TRADE_COUNT = 20
MAX_DAILY_LOSS_PCT = 2.0
MAX_DAILY_LOSS_AMOUNT = 0.0  # 0 = 不另設固定金額門檻
MAX_SINGLE_POSITION_PCT = 30.0
MAX_SINGLE_POSITION_AMOUNT = 150_000.0
MAX_RISK_PER_TRADE_PCT = 0.50
CASH_UTILIZATION_PCT = 95.0
MAX_POSITION_SHARES = 0  # 0 = 不另設股數上限

# 台股普通股整股預設 1 張 = 1000 股；盤中零股可選擇啟用。
LOT_SIZE = 1000
ALLOW_ODD_LOT_ENTRY = False

# 資料完整性與當沖資格採 fail-closed。
REQUIRE_BID_ASK = True
REQUIRE_DAY_TRADE_ELIGIBLE = True
REQUIRE_COMPLETE_ACCOUNT_STATE = True

# CTS：0=普通、7=盤中零股；0=現股；1=買、2=賣；0=限價；0=ROD/1=FOK/2=IOC。
CTS_ORDER_TYPE = 0
CTS_PRICE_TYPE = 4
CTS_CONDITION = 0


OUTPUT_FIELDS = [
    "decision",
    "action",
    "rejection_codes",
    "rejection_reasons",
    "signal_price",
    "current_price",
    "bid",
    "ask",
    "spread_pct",
    "slippage_pct",
    "chase_pct",
    "execution_friction_pct",
    "order_price",
    "position_size",
    "cts_trade_type",
    "cts_quantity",
    "estimated_position_cost",
]

REJECTION_LABELS = {
    "BOOK_NOT_READY": "委買賣資料尚未就緒或訂閱已失效",
    "BOOK_STALE": "委買賣來源時間已過期",
    "BOOK_TIMESTAMP_INVALID": "委買賣來源時間無效或跨交易日",
    "TRADE_SESSION_INVALID": "成交來源時間跨交易日",
    "BUY_DAY_CHANGE_LIMIT": "可執行買價的當日漲幅已達上限",
    "BUY_DAY_REFERENCE_MISSING": "缺少有效昨收，無法驗證當日漲幅",
    "NO_ACTIONABLE_SIGNAL": "沒有可執行的進場或出場訊號",
    "MARKET_BLOCKED": "市場環境未允許多方進場",
    "INVALID_SIGNAL_PRICE": "盤中訊號價格無效",
    "INVALID_CURRENT_PRICE": "目前成交價無效",
    "MISSING_BID_ASK": "買一／賣一資料不足，無法可靠評估價差與滑價",
    "INVALID_BID_ASK": "買一／賣一價格異常",
    "SPREAD_TOO_WIDE": "買賣價差超過允許上限",
    "SLIPPAGE_TOO_HIGH": "預估滑價超過允許上限",
    "CHASE_TOO_HIGH": "目前價格相對訊號價追價幅度過高",
    "EXECUTION_FRICTION_TOO_HIGH": "價差、滑價與追價合計成本過高",
    "MAX_BUY_PRICE_EXCEEDED": "買進價格超過最高允許價格",
    "DAY_TRADE_INELIGIBLE": "此股票明確不可現股當沖",
    "DAY_TRADE_ELIGIBILITY_UNKNOWN": "當沖資格未知，依風控設定拒絕下單",
    "ACCOUNT_STATE_INCOMPLETE": "帳戶狀態欄位不完整",
    "INSUFFICIENT_CASH": "可用資金不足",
    "EXISTING_POSITION": "同一股票已有持倉，不重複加碼",
    "DUPLICATE_OPEN_ORDER": "同一股票已有未完成買單",
    "DUPLICATE_SIGNAL": "此訊號已處理，避免重複下單",
    "MAX_POSITION_COUNT_REACHED": "已達最大持倉數量",
    "MAX_DAILY_TRADE_COUNT_REACHED": "已達每日最大交易次數",
    "DAILY_LOSS_LIMIT_REACHED": "已達每日虧損上限",
    "POSITION_SIZE_ZERO": "資金或單筆風險限制後無可下單數量",
    "NO_POSITION_TO_EXIT": "出場訊號出現，但找不到可賣出持倉",
    "MIXED_LOT_POSITION_UNSUPPORTED": "持倉股數同時包含整張與零股，單一 CTS 委託無法完整處理",
    "BUY_REVALIDATION_MARKET_STATE_UNAVAILABLE": "BUY 送單前無法取得最新 MarketState atomic snapshot",
    "BUY_REVALIDATION_QUOTE_FETCH_FAILED": "BUY 送單前無法取得最新 MarketState atomic snapshot",
    "BUY_REVALIDATION_QUOTE_TIMESTAMP_INVALID": "BUY 送單前行情時間戳無效或時間在未來",
    "BUY_REVALIDATION_QUOTE_STALE": "BUY 送單前 Fugle WebSocket Trade Event 已超過允許新鮮度",
    "BUY_REVALIDATION_LAST_INVALID": "BUY 送單前最新成交價無效",
    "BUY_REVALIDATION_BID_ASK_INVALID": "BUY 送單前買一／賣一無效",
    "BUY_REVALIDATION_SPREAD_TOO_WIDE": "BUY 送單前重新檢查價差超過上限",
    "BUY_REVALIDATION_SLIPPAGE_TOO_HIGH": "BUY 送單前重新檢查滑價超過上限",
    "BUY_REVALIDATION_CHASE_TOO_HIGH": "BUY 送單前重新檢查追價幅度超過上限",
    "BUY_REVALIDATION_EXECUTION_FRICTION_TOO_HIGH": "BUY 送單前重新檢查執行摩擦超過上限",
    "BUY_REVALIDATION_MAX_BUY_PRICE_EXCEEDED": "BUY 送單前最新委託價超過最高允許價格",
    "BUY_REVALIDATION_ENTRY_MOMENTUM_LOST": "BUY 送單前進場動能已不再成立",
    "BUY_REVALIDATION_WAVE_ANCHOR_INVALID": "AUTO BUY 缺少有效 entry_wave_anchor_price，禁止退回 signal_price",
    "BUY_REVALIDATION_WAVE_EXTENSION_TOO_HIGH": "BUY 送單前最新可執行價格相對 Wave anchor 延伸過高",
    "SELL_REVALIDATION_MARKET_STATE_UNAVAILABLE": "SELL 送單前無法取得最新 MarketState atomic snapshot",
    "SELL_REVALIDATION_LAST_INVALID": "SELL 送單前 MarketState 最新成交價無效",
}


# =============================================================================
# 參數模型
# =============================================================================


@dataclass(frozen=True)
class TradingParameters:
    gap_stress_pct: float = 2.0
    max_stress_risk_pct: float = 0.0
    max_correlated_risk_pct: float = 0.0
    strategy_risk_weights: Mapping[str, float] = field(default_factory=lambda: {"MOMENTUM": 1.0, "TREND": 1.0})
    strategy_daily_loss_budget_pct: Mapping[str, float] = field(default_factory=lambda: {"MOMENTUM": 0.0, "TREND": 0.0})
    sector_correlations: Mapping[str, float] = field(default_factory=dict)
    max_stop_distance_pct: float = 2.5
    emergency_stop_loss_pct: float = 2.5
    max_portfolio_open_risk_pct: float = 1.5
    max_sector_position_count: int = 2
    max_entries_per_symbol_per_day: int = 2
    reentry_cooldown_minutes: float = 10.0
    max_consecutive_losses: int = 3
    loss_cooldown_minutes: float = 30.0


    max_spread_pct: float = MAX_SPREAD_PCT
    max_slippage_pct: float = MAX_SLIPPAGE_PCT
    max_chase_pct: float = MAX_CHASE_PCT
    max_entry_day_change_pct: float = 7.0
    max_execution_friction_pct: float = MAX_EXECUTION_FRICTION_PCT
    buy_revalidation_max_quote_age_seconds: float = (
        BUY_REVALIDATION_MAX_QUOTE_AGE_SECONDS
    )
    buy_revalidation_max_book_age_seconds: float = BUY_REVALIDATION_MAX_BOOK_AGE_SECONDS
    max_breakout_extension_pct: float = MAX_BREAKOUT_EXTENSION_PCT
    max_entry_trigger_retrace_pct: float = MAX_ENTRY_TRIGGER_RETRACE_PCT

    max_buy_price: float = MAX_BUY_PRICE
    max_position_count: int = MAX_POSITION_COUNT
    max_daily_trade_count: int = MAX_DAILY_TRADE_COUNT
    max_daily_loss_pct: float = MAX_DAILY_LOSS_PCT
    max_daily_loss_amount: float = MAX_DAILY_LOSS_AMOUNT
    max_single_position_pct: float = MAX_SINGLE_POSITION_PCT
    max_single_position_amount: float = MAX_SINGLE_POSITION_AMOUNT
    max_risk_per_trade_pct: float = MAX_RISK_PER_TRADE_PCT
    cash_utilization_pct: float = CASH_UTILIZATION_PCT
    max_position_shares: int = MAX_POSITION_SHARES

    lot_size: int = LOT_SIZE
    allow_odd_lot_entry: bool = ALLOW_ODD_LOT_ENTRY
    require_bid_ask: bool = REQUIRE_BID_ASK
    require_day_trade_eligible: bool = REQUIRE_DAY_TRADE_ELIGIBLE
    require_complete_account_state: bool = REQUIRE_COMPLETE_ACCOUNT_STATE

    cts_order_type: int = CTS_ORDER_TYPE
    cts_price_type: int = CTS_PRICE_TYPE
    cts_condition: int = CTS_CONDITION


    def validate(self) -> None:
        if not math.isfinite(self.max_entry_day_change_pct) or not 0 <= self.max_entry_day_change_pct <= 100:
            raise ValueError("max_entry_day_change_pct 須為 0 至 100；0 停用當日漲幅限制")
        for value in (self.gap_stress_pct, self.max_stress_risk_pct, self.max_correlated_risk_pct):
            if not math.isfinite(float(value)) or value < 0:
                raise ValueError("風險參數必須為非負有限數值")
        for strategy in ("MOMENTUM", "TREND"):
            weight = self.strategy_risk_weights.get(strategy)
            budget = self.strategy_daily_loss_budget_pct.get(strategy)
            if weight is None or not math.isfinite(float(weight)) or not 0 < weight <= 1:
                raise ValueError("策略風險權重必須大於零且不超過一")
            if budget is None or not math.isfinite(float(budget)) or budget < 0:
                raise ValueError("策略每日損失預算必須為非負有限數值")
        if any(not math.isfinite(float(v)) or not 0 <= v <= 1 for v in self.sector_correlations.values()):
            raise ValueError("相關曝險係數須介於零與一")
        if self.allow_odd_lot_entry or self.lot_size != 1000:
            raise ValueError("本系統只允許整張，禁止零股委託")
        if not math.isfinite(float(self.max_stop_distance_pct)):
            raise ValueError("max_stop_distance_pct 必須為有限數值")
        if self.max_stop_distance_pct <= 0:
            raise ValueError("max_stop_distance_pct 必須大於零")
        if not math.isfinite(float(self.emergency_stop_loss_pct)):
            raise ValueError("emergency_stop_loss_pct 必須為有限數值")
        if self.emergency_stop_loss_pct <= 0:
            raise ValueError("emergency_stop_loss_pct 必須大於零")
        if not math.isfinite(float(self.max_portfolio_open_risk_pct)):
            raise ValueError("max_portfolio_open_risk_pct 必須為有限數值")
        if self.max_portfolio_open_risk_pct <= 0:
            raise ValueError("max_portfolio_open_risk_pct 必須大於零")
        if not math.isfinite(float(self.max_sector_position_count)):
            raise ValueError("max_sector_position_count 必須為有限數值")
        if self.max_sector_position_count <= 0:
            raise ValueError("max_sector_position_count 必須大於零")
        if not math.isfinite(float(self.max_entries_per_symbol_per_day)):
            raise ValueError("max_entries_per_symbol_per_day 必須為有限數值")
        if self.max_entries_per_symbol_per_day <= 0:
            raise ValueError("max_entries_per_symbol_per_day 必須大於零")
        if not math.isfinite(float(self.reentry_cooldown_minutes)):
            raise ValueError("reentry_cooldown_minutes 必須為有限數值")
        if self.reentry_cooldown_minutes <= 0:
            raise ValueError("reentry_cooldown_minutes 必須大於零")
        if not math.isfinite(float(self.max_consecutive_losses)):
            raise ValueError("max_consecutive_losses 必須為有限數值")
        if self.max_consecutive_losses <= 0:
            raise ValueError("max_consecutive_losses 必須大於零")
        if not math.isfinite(float(self.loss_cooldown_minutes)):
            raise ValueError("loss_cooldown_minutes 必須為有限數值")
        if self.loss_cooldown_minutes <= 0:
            raise ValueError("loss_cooldown_minutes 必須大於零")
        if (
            self.emergency_stop_loss_pct >= 100
            or self.max_stop_distance_pct > self.emergency_stop_loss_pct
        ):
            raise ValueError("結構停損距離不得超過緊急停損，緊急停損必須小於百分之百")
        if not math.isfinite(float(self.buy_revalidation_max_book_age_seconds)):
            raise ValueError("buy_revalidation_max_book_age_seconds 必須為有限數值")
        for name, value in (
            ("MAX_SPREAD_PCT", self.max_spread_pct),
            ("MAX_SLIPPAGE_PCT", self.max_slippage_pct),
            ("MAX_CHASE_PCT", self.max_chase_pct),
            ("MAX_EXECUTION_FRICTION_PCT", self.max_execution_friction_pct),
            (
                "BUY_REVALIDATION_MAX_QUOTE_AGE_SECONDS",
                self.buy_revalidation_max_quote_age_seconds,
            ),
            ("BUY_REVALIDATION_MAX_BOOK_AGE_SECONDS", self.buy_revalidation_max_book_age_seconds),
            ("MAX_BREAKOUT_EXTENSION_PCT", self.max_breakout_extension_pct),
            ("MAX_BUY_PRICE", self.max_buy_price),
            ("MAX_DAILY_LOSS_PCT", self.max_daily_loss_pct),
            ("MAX_SINGLE_POSITION_PCT", self.max_single_position_pct),
            ("MAX_RISK_PER_TRADE_PCT", self.max_risk_per_trade_pct),
            ("CASH_UTILIZATION_PCT", self.cash_utilization_pct),
        ):
            if float(value) <= 0:
                raise ValueError(f"{name} 必須 > 0")

        if (
            not math.isfinite(float(self.max_entry_trigger_retrace_pct))
            or not 0 <= self.max_entry_trigger_retrace_pct <= 1.5
        ):
            raise ValueError("max_entry_trigger_retrace_pct 須介於 0 至 1.5")

        if self.max_execution_friction_pct > (
            self.max_spread_pct + self.max_slippage_pct + self.max_chase_pct
        ):
            # 合計門檻大於三個個別門檻沒有錯，但通常代表合計門檻失去約束力。
            pass
        if self.max_single_position_amount < 0 or self.max_daily_loss_amount < 0:
            raise ValueError(
                "MAX_SINGLE_POSITION_AMOUNT / MAX_DAILY_LOSS_AMOUNT 不得 < 0"
            )
        if self.max_position_count <= 0 or self.max_daily_trade_count <= 0:
            raise ValueError("MAX_POSITION_COUNT / MAX_DAILY_TRADE_COUNT 必須 > 0")
        if self.lot_size <= 0:
            raise ValueError("LOT_SIZE 必須 > 0")
        if self.max_position_shares < 0:
            raise ValueError("MAX_POSITION_SHARES 不得 < 0")
        if not 0 < self.cash_utilization_pct <= 100:
            raise ValueError("CASH_UTILIZATION_PCT 必須介於 0~100")
        if not 0 < self.max_single_position_pct <= 100:
            raise ValueError("MAX_SINGLE_POSITION_PCT 必須介於 0~100")
        if self.cts_order_type not in {0, 1, 2, 16}:
            raise ValueError("CTS_ORDER_TYPE 僅允許 0/1/2/16")
        if self.cts_price_type != 4:
            raise ValueError("CTS_PRICE_TYPE 固定為 4（市價）")
        if self.cts_condition not in {0, 1, 2}:
            raise ValueError("CTS_CONDITION 僅允許 0=ROD / 1=FOK / 2=IOC")

    def snapshot(self) -> Dict[str, Any]:
        return {
            "GAP_STRESS_PCT": self.gap_stress_pct,
            "MAX_STRESS_RISK_PCT": self.max_stress_risk_pct,
            "MAX_CORRELATED_RISK_PCT": self.max_correlated_risk_pct,
            "STRATEGY_RISK_WEIGHTS": dict(self.strategy_risk_weights),
            "STRATEGY_DAILY_LOSS_BUDGET_PCT": dict(self.strategy_daily_loss_budget_pct),
            "SECTOR_CORRELATIONS": dict(self.sector_correlations),
            "MAX_STOP_DISTANCE_PCT": self.max_stop_distance_pct,
            "EMERGENCY_STOP_LOSS_PCT": self.emergency_stop_loss_pct,
            "MAX_PORTFOLIO_OPEN_RISK_PCT": self.max_portfolio_open_risk_pct,
            "MAX_SECTOR_POSITION_COUNT": self.max_sector_position_count,
            "MAX_ENTRIES_PER_SYMBOL_PER_DAY": self.max_entries_per_symbol_per_day,
            "REENTRY_COOLDOWN_MINUTES": self.reentry_cooldown_minutes,
            "MAX_CONSECUTIVE_LOSSES": self.max_consecutive_losses,
            "LOSS_COOLDOWN_MINUTES": self.loss_cooldown_minutes,
            "MAX_SPREAD_PCT": float(self.max_spread_pct),
            "MAX_SLIPPAGE_PCT": float(self.max_slippage_pct),
            "MAX_CHASE_PCT": float(self.max_chase_pct),
            "MAX_ENTRY_DAY_CHANGE_PCT": float(self.max_entry_day_change_pct),
            "MAX_EXECUTION_FRICTION_PCT": float(self.max_execution_friction_pct),
            "BUY_REVALIDATION_MAX_QUOTE_AGE_SECONDS": float(
                self.buy_revalidation_max_quote_age_seconds
            ),
            "BUY_REVALIDATION_MAX_BOOK_AGE_SECONDS": float(self.buy_revalidation_max_book_age_seconds),
            "MAX_BREAKOUT_EXTENSION_PCT": float(self.max_breakout_extension_pct),
            "MAX_ENTRY_TRIGGER_RETRACE_PCT": float(
                self.max_entry_trigger_retrace_pct
            ),
            "MAX_BUY_PRICE": float(self.max_buy_price),
            "MAX_POSITION_COUNT": int(self.max_position_count),
            "MAX_DAILY_TRADE_COUNT": int(self.max_daily_trade_count),
            "MAX_DAILY_LOSS_PCT": float(self.max_daily_loss_pct),
            "MAX_DAILY_LOSS_AMOUNT": float(self.max_daily_loss_amount),
            "MAX_SINGLE_POSITION_PCT": float(self.max_single_position_pct),
            "MAX_SINGLE_POSITION_AMOUNT": float(self.max_single_position_amount),
            "MAX_RISK_PER_TRADE_PCT": float(self.max_risk_per_trade_pct),
            "CASH_UTILIZATION_PCT": float(self.cash_utilization_pct),
            "MAX_POSITION_SHARES": int(self.max_position_shares),
            "LOT_SIZE": int(self.lot_size),
            "ALLOW_ODD_LOT_ENTRY": bool(self.allow_odd_lot_entry),
            "REQUIRE_BID_ASK": bool(self.require_bid_ask),
            "REQUIRE_DAY_TRADE_ELIGIBLE": bool(self.require_day_trade_eligible),
            "REQUIRE_COMPLETE_ACCOUNT_STATE": bool(self.require_complete_account_state),
            "CTS_ORDER_TYPE": int(self.cts_order_type),
            "CTS_PRICE_TYPE": int(self.cts_price_type),
            "CTS_CONDITION": int(self.cts_condition),
        }


# =============================================================================
# 共用工具
# =============================================================================


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


def _to_int(value: Any, default: int = 0) -> int:
    number = _to_float(value)
    if number is None:
        return int(default)
    return int(number)


def _round(value: Optional[float], digits: int = 4) -> Optional[float]:
    return None if value is None else round(float(value), digits)


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


def _tick_size(price: float, symbol: str = "") -> Decimal:
    value = Decimal(str(price))
    if str(symbol).startswith("00"):
        return Decimal("0.01") if value < Decimal("50") else Decimal("0.05")
    if value < Decimal("10"):
        return Decimal("0.01")
    if value < Decimal("50"):
        return Decimal("0.05")
    if value < Decimal("100"):
        return Decimal("0.10")
    if value < Decimal("500"):
        return Decimal("0.50")
    if value < Decimal("1000"):
        return Decimal("1.00")
    return Decimal("5.00")


def _price_to_tick(price: float, *, side: str, symbol: str = "") -> float:
    if price <= 0:
        raise ValueError("price 必須 > 0")
    value = Decimal(str(price))
    tick = _tick_size(price, symbol)
    units = value / tick
    rounding = ROUND_CEILING if str(side).upper() == "BUY" else ROUND_FLOOR
    rounded = units.to_integral_value(rounding=rounding) * tick
    return float(rounded)


def _pct(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return float(numerator) / float(denominator) * 100.0


def _parse_iso(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _trade_date_int(signal: Mapping[str, Any], quote: Mapping[str, Any]) -> int:
    candidates = [signal.get("order_trade_date"), quote.get("trade_date"), signal.get("entry_time")]
    for value in candidates:
        if not value:
            continue
        text = str(value)
        digits = re.sub(r"\D", "", text[:10] if "-" in text else text)
        if len(digits) >= 8:
            return int(digits[:8])
    raise ValueError("無法由行情／訊號取得 YYYYMMDD 交易日期")


def _position_key(position: Mapping[str, Any]) -> Tuple[str, str]:
    return (
        _normalize_market(position.get("market")),
        str(position.get("symbol") or "").strip(),
    )


def _normalize_positions(value: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if isinstance(value, Mapping):
        iterable = []
        for key, item in value.items():
            if isinstance(item, Mapping):
                row = dict(item)
                row.setdefault("symbol", str(key).split(":")[-1])
                iterable.append(row)
        value = iterable
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return rows
    for item in value:
        if not isinstance(item, Mapping):
            continue
        row = dict(item)
        row["market"] = _normalize_market(row.get("market"))
        row["symbol"] = str(row.get("symbol") or "").strip()
        row["quantity"] = max(0, _to_int(row.get("quantity")))
        if row["symbol"]:
            rows.append(row)
    rows.sort(key=lambda row: (str(row.get("market")), str(row.get("symbol"))))
    return rows


def _find_position(
    positions: Sequence[Mapping[str, Any]], symbol: str, market: str
) -> Optional[Dict[str, Any]]:
    wanted = (_normalize_market(market), str(symbol).strip())
    for row in positions:
        if _position_key(row) == wanted and _to_int(row.get("quantity")) > 0:
            return dict(row)
    return None


def _open_order_exists(
    account_state: Mapping[str, Any], symbol: str, market: str
) -> bool:
    rows = account_state.get("open_orders") or []
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return False
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("symbol") or "").strip() != str(symbol).strip():
            continue
        row_market = _normalize_market(row.get("market"))
        if row_market and row_market != _normalize_market(market):
            continue
        side = str(row.get("side") or row.get("action") or "").strip().upper()
        status = str(row.get("status") or "OPEN").strip().upper()
        if side in {"BUY", "1"} and status not in {
            "FILLED",
            "CANCELLED",
            "CANCELED",
            "REJECTED",
        }:
            return True
    return False


def prepare_execution_account_state(
    account_state: Mapping[str, Any],
    system_orders: Sequence[Mapping[str, Any]],
    *,
    account_id: str,
    trade_date: Any,
) -> Dict[str, Any]:
    """以使用者配置的資金預算扣除系統持倉及待確認買單；不冒充券商現金查詢。"""
    state = dict(account_state)
    positions = _normalize_positions(state.get("positions"))
    invested = sum(
        max(0, _to_int(row.get("managed_quantity")))
        * max(0.0, _to_float(row.get("entry_price")) or 0.0)
        for row in positions
        if row.get("managed_by") in {"MOMENTUM_SCALPING", "MANUAL"}
    )
    pending = [
        row
        for row in system_orders
        if row.get("account_id") == account_id
        and _trade_identity_date(row.get("trade_date"))
        == _trade_identity_date(trade_date)
        and row.get("action") == "BUY"
        and row.get("status") not in {"FILLED", "REJECTED", "CANCELED", "CANCELLED", "PRE_SUBMIT_REJECTED", "CONFIRMED_ABSENT"}
    ]
    reserved = sum(
        max(0, _to_int(row.get("requested_shares")) - _to_int(row.get("filled_shares")))
        * max(0.0, _to_float(row.get("reservation_price", row.get("requested_price"))) or 0.0)
        for row in pending
    )
    held = {
        str(row.get("symbol")) for row in positions if _to_int(row.get("quantity")) > 0
    }
    state["pending_position_count"] = len(
        {str(row.get("symbol")) for row in pending} - held
    )
    state["daily_trade_count"] = _to_int(state.get("daily_trade_count")) + sum(
        row.get("status") != "PARTIALLY_FILLED" for row in pending
    )
    configured = _to_float(state.get("available_cash"))
    state["available_cash"] = (
        max(
            0.0,
            configured
            - invested
            - reserved
            + min(0.0, _to_float(state.get("daily_pnl")) or 0.0),
        )
        if configured is not None
        else None
    )
    state["cash_source"] = "USER_CONFIGURED_BUDGET_LESS_MANAGED_EXPOSURE"
    state["pending_open_risk"] = sum(
        max(0, _to_int(r.get("requested_shares")) - _to_int(r.get("filled_shares")))
        * max(
            0,
            float(r.get("reservation_price", r.get("requested_price")) or 0)
            - float(r.get("effective_stop_price") or 0),
        )
        for r in pending
    )
    state["pending_sector_counts"] = {}
    for r in pending:
        sector = str(r.get("sector") or "UNKNOWN")
        if str(r.get("symbol")) not in held:
            state["pending_sector_counts"][sector] = (
                state["pending_sector_counts"].get(sector, 0) + 1
            )
    state["pending_risk_positions"] = [{
        "symbol": row.get("symbol"), "sector": row.get("sector"),
        "strategy_type": row.get("strategy_type"),
        "managed_quantity": max(0, _to_int(row.get("requested_shares")) - _to_int(row.get("filled_shares"))),
        "entry_price": row.get("reservation_price", row.get("requested_price")),
        "effective_stop_price": row.get("effective_stop_price"),
        "open_risk": max(0, _to_int(row.get("requested_shares")) - _to_int(row.get("filled_shares")))
            * max(0, float(row.get("reservation_price", row.get("requested_price")) or 0) - float(row.get("effective_stop_price") or 0))
    } for row in pending]
    state["reserved_buy_amount"] = reserved
    state["managed_position_cost"] = invested
    return state


def _account_state_missing(account_state: Mapping[str, Any]) -> List[str]:
    required = ("available_cash", "daily_trade_count", "daily_pnl", "positions")
    return [name for name in required if name not in account_state]


def _canonical_account_state(account_state: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        **{
            k: account_state.get(k)
            for k in (
                "symbol_entry_counts",
                "symbol_last_exit",
                "loss_cooldown_until",
                "consecutive_losses",
                "pending_open_risk",
                "pending_sector_counts",
            )
        },
        "pending_position_count": _to_int(account_state.get("pending_position_count")),
        "available_cash": _to_float(account_state.get("available_cash")),
        "equity": _to_float(account_state.get("equity")),
        "daily_trade_count": _to_int(account_state.get("daily_trade_count")),
        "daily_pnl": _to_float(account_state.get("daily_pnl")),
        "positions": _normalize_positions(account_state.get("positions")),
        "open_orders": [
            dict(item)
            for item in (account_state.get("open_orders") or [])
            if isinstance(item, Mapping)
        ],
        "processed_signal_fingerprints": sorted(
            str(item)
            for item in (account_state.get("processed_signal_fingerprints") or [])
            if str(item)
        ),
    }


def _reject(codes: Sequence[str]) -> Tuple[List[str], List[str]]:
    unique = list(dict.fromkeys(str(code) for code in codes if str(code)))
    return unique, [REJECTION_LABELS.get(code, code) for code in unique]


# =============================================================================
# APIGateway MarketState atomic snapshot
# =============================================================================


def _event_time_datetime(value: Any) -> Optional[datetime]:
    event_time = _to_int(value, 0)
    if event_time <= 0:
        return None
    try:
        return datetime.fromtimestamp(event_time / 1_000_000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _market_state_snapshot_fingerprint(snapshot: Mapping[str, Any]) -> str:
    return _stable_hash(
        {
            "symbol": snapshot.get("symbol"),
            "market": snapshot.get("market"),
            "event_time": snapshot.get("event_time"),
            "serial": snapshot.get("serial"),
            "quote_sequence": snapshot.get("quote_sequence"),
            "symbol_quote_sequence": snapshot.get("symbol_quote_sequence"),
            "price": snapshot.get("price"),
            "prev_close": snapshot.get("prev_close"),
            "book_event_time": snapshot.get("book_event_time"),
            "book_generation": snapshot.get("book_generation"),
            "book_active": snapshot.get("book_active"),
            "book_ready": snapshot.get("book_ready"),
            "bid": snapshot.get("bid"),
            "ask": snapshot.get("ask"),
            "volume": snapshot.get("volume"),
            "realtime_age_seconds": snapshot.get("realtime_age_seconds"),
            "realtime_fresh": snapshot.get("realtime_fresh"),
        }
    )


def _freeze_atomic_market_state_snapshot(
    gateway: Any,
    symbol: str,
    market: str,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """只從 APIGateway 既有 MarketState 取得一次 atomic copy，不發任何行情 HTTP request。"""
    market_client = getattr(gateway, "market", None)
    state = getattr(market_client, "state", None)
    snapshot_method = getattr(state, "snapshot", None)
    if not callable(snapshot_method):
        raise RuntimeError(
            "APIGateway 缺少 market.state.snapshot；禁止退回外部 Quote request"
        )

    as_of = now or datetime.now(timezone(timedelta(hours=8)))
    snapshot = snapshot_method(str(symbol), as_of=as_of)
    if not isinstance(snapshot, Mapping):
        raise RuntimeError("MarketState.snapshot 未回傳 Mapping")
    frozen = dict(snapshot)
    frozen_market = _normalize_market(frozen.get("market"))
    wanted_market = _normalize_market(market)
    if wanted_market and frozen_market and frozen_market != wanted_market:
        raise RuntimeError(
            f"MarketState market mismatch: {frozen_market} != {wanted_market}"
        )
    frozen["market"] = frozen_market or wanted_market
    frozen["symbol"] = str(frozen.get("symbol") or symbol).strip()
    frozen["snapshot_fingerprint"] = _market_state_snapshot_fingerprint(frozen)
    return frozen


def _event_freshness_from_snapshot(
    snapshot: Mapping[str, Any],
    *,
    now: Optional[datetime],
    max_age_seconds: float,
) -> Dict[str, Any]:
    """WebSocket Event freshness 只看 Fugle event_time；received_at 不作來源時間。"""
    event_time = _to_int(snapshot.get("event_time"), 0)
    serial = _to_int(snapshot.get("serial"), -1)
    event_dt = _event_time_datetime(event_time)
    age = _to_float(snapshot.get("realtime_age_seconds"))
    current = now
    if age is None and event_dt is not None:
        current = current or datetime.now(timezone(timedelta(hours=8)))
        if current.tzinfo is None or current.utcoffset() is None:
            current = current.astimezone()
        age = (current.astimezone(timezone.utc) - event_dt).total_seconds()
    valid = event_dt is not None and serial >= 0 and age is not None and age >= -1.0
    stale = bool(valid and age is not None and age > float(max_age_seconds))
    return {
        "event_time": event_time if event_time > 0 else None,
        "event_time_iso": (
            event_dt.astimezone().isoformat() if event_dt is not None else None
        ),
        "event_serial": serial if serial >= 0 else None,
        "event_age_seconds": _round(age, 6),
        "event_fresh": bool(valid and not stale),
        "event_stale": stale,
        "freshness_basis": "FUGLE_EVENT_TIME",
        "freshness_threshold_seconds": float(max_age_seconds),
        "quote_sequence": _to_int(snapshot.get("quote_sequence"), 0),
        "symbol_quote_sequence": _to_int(snapshot.get("symbol_quote_sequence"), 0),
        "received_at": snapshot.get("received_at"),
    }


def _buy_market_data_contract(snapshot: Mapping[str, Any], parameters: TradingParameters,
                              *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """BUY 專用：依兩個來源時間重新計算，不信任快照內已算好的 age。"""
    current = now or datetime.now(timezone(timedelta(hours=8)))
    if current.tzinfo is None:
        current = current.astimezone()
    session = current.astimezone(timezone(timedelta(hours=8))).date()
    codes = []
    ages = {}
    for kind, limit in (("trade", parameters.buy_revalidation_max_quote_age_seconds),
                        ("book", parameters.buy_revalidation_max_book_age_seconds)):
        stamp = snapshot.get("event_time") if kind == "trade" else snapshot.get("book_event_time")
        source = _event_time_datetime(stamp)
        age = (current.astimezone(timezone.utc) - source).total_seconds() if source else None
        ages[kind + "_age_seconds"] = age
        if source is None or age < -1.0:
            codes.append("BUY_REVALIDATION_QUOTE_TIMESTAMP_INVALID" if kind == "trade" else "BOOK_TIMESTAMP_INVALID")
        elif source.astimezone(timezone(timedelta(hours=8))).date() != session:
            codes.append("TRADE_SESSION_INVALID" if kind == "trade" else "BOOK_TIMESTAMP_INVALID")
        elif age > limit:
            codes.append("BUY_REVALIDATION_QUOTE_STALE" if kind == "trade" else "BOOK_STALE")
    bid, ask = _to_float(snapshot.get("bid")), _to_float(snapshot.get("ask"))
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        codes.append("BUY_REVALIDATION_BID_ASK_INVALID")
    if _to_int(snapshot.get("serial"), -1) < 0:
        codes.append("BUY_REVALIDATION_QUOTE_TIMESTAMP_INVALID")
    if snapshot.get("book_active") is not True or snapshot.get("book_ready") is not True:
        codes.append("BOOK_NOT_READY")
    return dict(ages, ok=not codes, rejection_codes=codes,
                book_event_time=snapshot.get("book_event_time"),
                book_generation=snapshot.get("book_generation"),
                book_ready=snapshot.get("book_ready") is True,
                book_freshness_threshold_seconds=parameters.buy_revalidation_max_book_age_seconds)


def _buy_execution_data_gate(gateway: Any, decision: Mapping[str, Any],
                             parameters: TradingParameters) -> Dict[str, Any]:
    """手動 BUY 同樣須符合資料契約，但不加入自動策略條件。"""
    try:
        snapshot = _freeze_atomic_market_state_snapshot(
            gateway, str(decision.get("symbol") or ""), str(decision.get("market") or ""))
    except Exception as exc:
        return {"ok": False, "rejection_codes": ["BUY_REVALIDATION_MARKET_STATE_UNAVAILABLE"],
                "error": str(exc)}
    result = _buy_market_data_contract(snapshot, parameters)
    result["market_state_snapshot"] = snapshot
    return result


def _atomic_snapshot_execution_fields(snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    """price / bid / ask 永遠由同一份 frozen snapshot 取值，禁止跨時間點混搭。"""
    last_price = _quote_last_price(snapshot)
    return {
        "price": last_price,
        "bid": _to_float(snapshot.get("bid")),
        "ask": _to_float(snapshot.get("ask")),
        "event_time": snapshot.get("event_time"),
        "serial": snapshot.get("serial"),
        "quote_sequence": snapshot.get("quote_sequence"),
        "symbol_quote_sequence": snapshot.get("symbol_quote_sequence"),
        "snapshot_fingerprint": snapshot.get("snapshot_fingerprint")
        or _market_state_snapshot_fingerprint(snapshot),
    }


_EVENT_AGE_SAMPLE_LOCK = threading.RLock()
_EVENT_AGE_SAMPLES: List[Dict[str, Any]] = []
_EVENT_AGE_SAMPLE_LIMIT = 2000


def _record_event_age_sample(
    phase: str, snapshot: Mapping[str, Any], freshness: Mapping[str, Any]
) -> None:
    age = _to_float(freshness.get("event_age_seconds"))
    if age is None:
        return
    row = {
        "phase": str(phase),
        "symbol": str(snapshot.get("symbol") or ""),
        "market": _normalize_market(snapshot.get("market")),
        "event_time": freshness.get("event_time"),
        "serial": freshness.get("event_serial"),
        "event_age_seconds": round(float(age), 6),
        "snapshot_fingerprint": snapshot.get("snapshot_fingerprint")
        or _market_state_snapshot_fingerprint(snapshot),
    }
    with _EVENT_AGE_SAMPLE_LOCK:
        _EVENT_AGE_SAMPLES.append(row)
        if len(_EVENT_AGE_SAMPLES) > _EVENT_AGE_SAMPLE_LIMIT:
            del _EVENT_AGE_SAMPLES[: len(_EVENT_AGE_SAMPLES) - _EVENT_AGE_SAMPLE_LIMIT]


def get_event_age_statistics() -> Dict[str, Any]:
    """回傳 Fugle Event age 觀測分布；只供調參，不參與任何 ALLOW / REJECT。"""
    with _EVENT_AGE_SAMPLE_LOCK:
        rows = [dict(row) for row in _EVENT_AGE_SAMPLES]
    ages = sorted(
        float(row["event_age_seconds"])
        for row in rows
        if _to_float(row.get("event_age_seconds")) is not None
    )
    buckets = {
        "<=0.1s": 0,
        "<=0.25s": 0,
        "<=0.5s": 0,
        "<=1s": 0,
        "<=2s": 0,
        "<=5s": 0,
        "<=10s": 0,
        "<=25s": 0,
        ">25s": 0,
    }
    for age in ages:
        if age <= 0.1:
            buckets["<=0.1s"] += 1
        elif age <= 0.25:
            buckets["<=0.25s"] += 1
        elif age <= 0.5:
            buckets["<=0.5s"] += 1
        elif age <= 1.0:
            buckets["<=1s"] += 1
        elif age <= 2.0:
            buckets["<=2s"] += 1
        elif age <= 5.0:
            buckets["<=5s"] += 1
        elif age <= 10.0:
            buckets["<=10s"] += 1
        elif age <= 25.0:
            buckets["<=25s"] += 1
        else:
            buckets[">25s"] += 1

    def percentile(p: float) -> Optional[float]:
        if not ages:
            return None
        index = min(len(ages) - 1, max(0, math.ceil((len(ages) - 1) * p)))
        return round(ages[index], 6)

    phase_counts: Dict[str, int] = {}
    for row in rows:
        phase = str(row.get("phase") or "UNKNOWN")
        phase_counts[phase] = phase_counts.get(phase, 0) + 1
    return {
        "sample_count": len(ages),
        "max_samples": _EVENT_AGE_SAMPLE_LIMIT,
        "min_seconds": round(ages[0], 6) if ages else None,
        "p50_seconds": percentile(0.50),
        "p90_seconds": percentile(0.90),
        "p95_seconds": percentile(0.95),
        "p99_seconds": percentile(0.99),
        "max_seconds": round(ages[-1], 6) if ages else None,
        "buckets": buckets,
        "phase_counts": dict(sorted(phase_counts.items())),
        "decision_dependency": False,
    }


# =============================================================================
# 系統委託身分與成交歸屬
# =============================================================================


_SYSTEM_ORDER_REGISTRY: Dict[str, Dict[str, Any]] = {}
_SYSTEM_ORDER_REGISTRY_LOCK = threading.RLock()
_SYSTEM_ORDER_REGISTRY_MAX_ROWS = 1000


def _execution_trigger(signal_context: Mapping[str, Any]) -> str:
    for key in ("execution_trigger", "trigger", "control_mode", "mode"):
        value = str(signal_context.get(key) or "").strip().upper()
        if value in {"AUTO", "MANUAL"}:
            return value
    return "SYSTEM"


def _trade_identity_date(value: Any) -> str:
    text = re.sub(r"\D", "", str(value or ""))
    return text[:8] if len(text) >= 8 else ""


def _match_time_key(trade_date: Any, match_time: Any) -> Optional[Tuple[int, int]]:
    date_digits = _trade_identity_date(trade_date)
    time_digits = re.sub(r"\D", "", str(match_time or ""))
    if len(date_digits) != 8 or len(time_digits) < 6:
        return None
    # CTS 實機常見 HHMMSSmmm；不足毫秒時右補 0，超過毫秒則只取前三位。
    time_digits = time_digits[:6] + (time_digits[6:9] + "000")[:3]
    return int(date_digits), int(time_digits)


def _fill_entry_timestamp(fill: Mapping[str, Any]) -> Optional[str]:
    key = _match_time_key(fill.get("match_date"), fill.get("match_time"))
    if key is None:
        return None
    try:
        value = datetime.strptime(f"{key[0]:08d}{key[1]:09d}", "%Y%m%d%H%M%S%f")
        return value.replace(tzinfo=timezone(timedelta(hours=8))).isoformat(
            timespec="milliseconds"
        )
    except ValueError:
        return None


def _submitted_time_key(value: Any) -> Optional[Tuple[int, int]]:
    parsed = _parse_iso(value)
    if parsed is None:
        return None
    date_value = int(parsed.strftime("%Y%m%d"))
    time_value = int(parsed.strftime("%H%M%S")) * 1000 + parsed.microsecond // 1000
    return date_value, time_value


def _match_not_before_submit(
    trade_date: Any,
    match_time: Any,
    submitted_at: Any,
) -> bool:
    submit_key = _submitted_time_key(submitted_at)
    match_key = _match_time_key(trade_date, match_time)
    if submit_key is None or match_key is None:
        return False
    return match_key >= submit_key


ORDER_STATES = frozenset({
    "REQUEST_SENT", "STATUS_UNKNOWN", "WORKING", "PARTIAL_FILLED", "FILLED",
    "CANCEL_PENDING", "CANCELLED", "REJECTED", "SUBMITTING", "PRE_SUBMIT_REJECTED", "CONFIRMED_ABSENT",
})
_ORDER_ALIASES = {
    "PENDING": "SUBMITTING", "PARTIAL": "PARTIAL_FILLED", "SUBMITTED": "WORKING",
    "SUBMITTED_NOT_FILLED": "WORKING", "PARTIALLY_FILLED": "PARTIAL_FILLED",
    "CANCELED": "CANCELLED", "IDENTITY_MISSING": "STATUS_UNKNOWN",
    "PENDING_RECONCILIATION": "STATUS_UNKNOWN",
}


def legal_board_lot(shares, lot_size=LOT_SIZE):
    """所有委託共用整張合法化；不足一張保留殘餘，不建立零股單。"""
    if int(lot_size) != 1000:
        raise ValueError("僅支援每張一千股")
    value = _to_float(shares)
    if value is None or value < 0:
        raise ValueError("無效委託數量")
    return int(value) // lot_size * lot_size


def _register_system_pending_order(
    decision: Mapping[str, Any],
    signal_context: Mapping[str, Any],
    request: Any,
    *,
    submitted_at: str,
    lot_size: int = LOT_SIZE,
) -> Dict[str, Any]:
    request_data = request.to_dict() if hasattr(request, "to_dict") else {}
    trade_date = str(request_data.get("trade_date") or "")
    symbol = str(decision.get("symbol") or request_data.get("symbol") or "").strip()
    market = _normalize_market(decision.get("market"))
    action = str(decision.get("action") or "").strip().upper()
    side = (
        1
        if action == "BUY"
        else 2 if action == "SELL" else _to_int(request_data.get("side"))
    )
    requested_cts_quantity = max(0, _to_int(request_data.get("quantity")))
    trade_type = _to_int(request_data.get("trade_type"))
    requested_shares = (
        requested_cts_quantity * max(1, int(lot_size))
        if trade_type in {0, 2}
        else requested_cts_quantity
    )
    key_payload = {
        "trade_date": trade_date,
        "symbol": symbol,
        "side": side,
        "submitted_at": submitted_at,
        "decision_fingerprint": decision.get("decision_fingerprint"),
        "signal_fingerprint": signal_context.get("signal_fingerprint"),
    }
    registry_key = uuid.uuid4().hex
    intent_id = str(decision.get("order_intent_id") or registry_key)
    row = {
        "registry_key": registry_key,
        "order_intent_id": intent_id,
        "generation": int(decision.get("generation") or 0),
        "position_cycle_id": str(decision.get("position_cycle_id")
            or (decision.get("existing_position") or {}).get("position_cycle_id")
            or uuid.uuid4().hex),
        "order_state": "SUBMITTING",
        "account_id": str(request_data.get("account_id") or ""),
        "owner": "MOMENTUM_SCALPING",
        "management_source": "系統今日",
        "ownership_status": "PENDING",
        "status": "SUBMITTING",
        "trigger": _execution_trigger(signal_context),
        "action": action,
        "side": side,
        "trade_date": trade_date,
        "symbol": symbol,
        "market": market,
        "trade_type": trade_type,
        "requested_cts_quantity": requested_cts_quantity,
        "requested_shares": requested_shares,
        "requested_price": _to_float(request_data.get("price")),
        "reservation_price": _to_float(decision.get("order_price")) or 0.0,
        "request_order_type": request_data.get("order_type"),
        "request_price_type": request_data.get("price_type"),
        "request_condition": request_data.get("condition"),
        "submitted_at": submitted_at,
        "order_no": "",
        "order_id": "",
        "decision_fingerprint": str(decision.get("decision_fingerprint") or ""),
        "signal_fingerprint": str(signal_context.get("signal_fingerprint") or ""),
    }
    for key in STRATEGY_METADATA_FIELDS:
        row[key] = decision.get(key, signal_context.get(key))
    row["initial_quantity"] = (
        requested_shares
        if action == "BUY"
        else (decision.get("existing_position") or {}).get("initial_quantity")
    )
    with _SYSTEM_ORDER_REGISTRY_LOCK:
        _SYSTEM_ORDER_REGISTRY[registry_key] = row
        if len(_SYSTEM_ORDER_REGISTRY) > _SYSTEM_ORDER_REGISTRY_MAX_ROWS:
            oldest = list(_SYSTEM_ORDER_REGISTRY)[
                : len(_SYSTEM_ORDER_REGISTRY) - _SYSTEM_ORDER_REGISTRY_MAX_ROWS
            ]
            for old_key in oldest:
                # 當日成交仍供完整對帳，未決委託無論日期都不能淘汰。
                old = _SYSTEM_ORDER_REGISTRY[old_key]
                if (old.get("trade_date") != trade_date and
                        old.get("order_state") in {"FILLED", "CANCELLED", "REJECTED", "PRE_SUBMIT_REJECTED", "CONFIRMED_ABSENT"}):
                    _SYSTEM_ORDER_REGISTRY.pop(old_key, None)
    return dict(row)


def _update_system_order_identity(
    registry_key: str,
    *,
    order_id: Any = "",
    order_no: Any = "",
    status: Optional[str] = None,
    filled_shares: Optional[int] = None,
    unknown_query_attempts: Optional[int] = None,
    recovery_phase: Optional[str] = None,
) -> Dict[str, Any]:
    clean_key = str(registry_key or "")
    with _SYSTEM_ORDER_REGISTRY_LOCK:
        row = _SYSTEM_ORDER_REGISTRY.get(clean_key)
        if row is None:
            return {}
        if unknown_query_attempts is not None:
            row["unknown_query_attempts"] = unknown_query_attempts
        if recovery_phase is not None:
            row["recovery_phase"] = recovery_phase
        if str(order_id or "").strip():
            row["order_id"] = str(order_id).strip()
        if str(order_no or "").strip():
            row["order_no"] = str(order_no).strip()
        if filled_shares is not None:
            confirmed = int(filled_shares)
            if confirmed < 0 or confirmed > int(row.get("requested_shares") or 0):
                raise ValueError("FILL_QUANTITY_EXCEEDS_REQUEST")
            row["filled_shares"] = max(int(row.get("filled_shares") or 0), confirmed)
        if status:
            next_state = _ORDER_ALIASES.get(str(status), str(status))
            if next_state not in ORDER_STATES:
                next_state = "STATUS_UNKNOWN"
            old_state = row.get("order_state", "REQUEST_SENT")
            allowed = {
                "FILLED": {"FILLED"},
                "CANCELLED": {"CANCELLED", "FILLED"},
                "REJECTED": {"REJECTED"},
                "PRE_SUBMIT_REJECTED": {"PRE_SUBMIT_REJECTED"},
                "CONFIRMED_ABSENT": {"CONFIRMED_ABSENT"},
                "PARTIAL_FILLED": {"PARTIAL_FILLED", "FILLED", "CANCEL_PENDING", "CANCELLED", "STATUS_UNKNOWN"},
                "CANCEL_PENDING": {"CANCEL_PENDING", "FILLED", "CANCELLED", "STATUS_UNKNOWN"},
                "WORKING": ORDER_STATES - {"REQUEST_SENT", "SUBMITTING", "PRE_SUBMIT_REJECTED", "CONFIRMED_ABSENT"},
            }
            if next_state in allowed.get(old_state, ORDER_STATES):
                # 未決狀態恢復後也不可忘記已確認的部分成交。
                if next_state == "WORKING" and int(row.get("filled_shares") or 0) > 0:
                    next_state = "PARTIAL_FILLED"
                    status = "PARTIALLY_FILLED"
                row["order_state"] = next_state
                row["ownership_status"] = str(status)
                row["status"] = str(status)
        return dict(row)


def get_system_order_registry() -> List[Dict[str, Any]]:
    """回傳本次程式執行期間由 Trading 送出的委託身分，供 GUI 對帳使用。"""
    with _SYSTEM_ORDER_REGISTRY_LOCK:
        return [dict(row) for row in _SYSTEM_ORDER_REGISTRY.values()]


def restore_system_order_registry(rows: Sequence[Mapping[str, Any]]) -> int:
    """還原 Runtime 保存的委託身分；仍須以同帳號／同交易日的 CTS 查詢確認成交。"""
    restored = {}
    for raw in rows:
        row = dict(raw)
        required = (
            "registry_key",
            "account_id",
            "trade_date",
            "symbol",
            "market",
            "submitted_at",
        )
        if any(not str(row.get(key) or "").strip() for key in required):
            raise ValueError("委託身分檔缺少必要欄位")
        if row.get("owner") != "MOMENTUM_SCALPING" or row.get("action") not in {
            "BUY",
            "SELL",
        }:
            raise ValueError("委託身分檔的歸屬或買賣別無效")
        row["status"] = str(
            row.get("ownership_status") or row.get("status") or "PENDING"
        )
        restored[str(row["registry_key"])] = row
    with _SYSTEM_ORDER_REGISTRY_LOCK:
        for key, row in restored.items():
            _SYSTEM_ORDER_REGISTRY.setdefault(key, row)
    return len(restored)


def resolve_cts_match_ownership(match: Mapping[str, Any]) -> Dict[str, Any]:
    """以 OID／委託書號優先辨識 CTS 成交是否由本系統送出。

    找不到可靠身分時不直接宣告 CTS 外部；回傳 UNKNOWN，交由上層完成
    庫存／當日委託對帳後再決定。
    """
    order_id = str(match.get("order_id") or match.get("F14") or "").strip()
    order_no = str(match.get("order_no") or match.get("F9") or "").strip()
    symbol = str(match.get("symbol") or match.get("F3") or "").strip()
    side = _to_int(match.get("side") if "side" in match else match.get("F4"), -1)
    trade_date = str(
        match.get("match_date") or match.get("trade_date") or match.get("F10") or ""
    )
    match_time = str(
        match.get("match_time") or match.get("F20") or match.get("F11") or ""
    )

    with _SYSTEM_ORDER_REGISTRY_LOCK:
        rows = [dict(row) for row in _SYSTEM_ORDER_REGISTRY.values()]

    account_id = str(match.get("account_id") or match.get("F0") or "").strip()
    rows = [row for row in rows
            if (not account_id or str(row.get("account_id") or "") == account_id)
            and (not symbol or row.get("symbol") == symbol)
            and (side not in {1, 2} or row.get("side") == side)
            and (not trade_date or _trade_identity_date(row.get("trade_date")) == _trade_identity_date(trade_date))]
    if order_id:
        exact = [row for row in rows if str(row.get("order_id") or "") == order_id]
        if len(exact) == 1:
            return {**exact[0], "ownership_status": "CONFIRMED"}
    if order_no and not order_id:
        exact = [row for row in rows if str(row.get("order_no") or "") == order_no]
        if len(exact) == 1:
            return {**exact[0], "ownership_status": "CONFIRMED"}

    fallback = [
        row
        for row in rows if not (order_id or order_no)
        if str(row.get("symbol") or "") == symbol
        and _to_int(row.get("side"), -1) == side
        and (
            not trade_date
            or _trade_identity_date(row.get("trade_date"))
            == _trade_identity_date(trade_date)
        )
        and _match_not_before_submit(trade_date, match_time, row.get("submitted_at"))
    ]
    if len(fallback) == 1:
        return {**fallback[0], "ownership_status": "TIME_GATED_FALLBACK"}
    return {
        "owner": "",
        "management_source": "待核對",
        "ownership_status": "UNKNOWN" if not fallback else "AMBIGUOUS",
        "order_id": order_id,
        "order_no": order_no,
        "symbol": symbol,
        "side": side,
    }


# =============================================================================
# CTS 成交／庫存解析
# =============================================================================


def summarize_cts_fills(
    parsed_rows: Sequence[Mapping[str, Any]],
    *,
    symbol: Optional[str] = None,
    side: Optional[int] = None,
    order_id: Optional[str] = None,
    order_no: Optional[str] = None,
    trade_date: Optional[str] = None,
    submitted_at: Optional[str] = None,
) -> Dict[str, Any]:
    """解析 Stock_QueryMatch，優先使用 api_gateway 正規化後的成交欄位。

    新版 api_gateway 會提供 filled_shares / cts_quantity，Trading 不再自行
    對已正規化資料乘 LOT_SIZE。舊式 F 欄位仍保留相容，但若存在
    _filled_shares 則優先採用，避免 CTS 實機 F7=1000 股再次放大為 1000 張。
    """
    clean_order_id = str(order_id or "").strip()
    clean_order_no = str(order_no or "").strip()
    clean_trade_date = _trade_identity_date(trade_date)
    matched: List[Dict[str, Any]] = []

    for row in parsed_rows:
        if not isinstance(row, Mapping):
            continue

        normalized_shape = "filled_shares" in row or "cts_quantity" in row
        row_symbol = str(
            row.get("symbol") if normalized_shape else row.get("F3") or ""
        ).strip()
        row_side = _to_int(row.get("side") if normalized_shape else row.get("F4"), -1)
        row_order_id = str(
            row.get("order_id") if normalized_shape else row.get("F14") or ""
        ).strip()
        row_order_no = str(
            row.get("order_no") if normalized_shape else row.get("F9") or ""
        ).strip()
        row_trade_date = str(
            row.get("match_date")
            if normalized_shape and row.get("match_date")
            else row.get("trade_date") if normalized_shape else row.get("F10") or ""
        ).strip()
        row_match_time = str(
            row.get("match_time")
            if normalized_shape
            else row.get("F20") or row.get("F11") or ""
        ).strip()

        if symbol and row_symbol != str(symbol).strip():
            continue
        if side is not None and row_side != int(side):
            continue
        if clean_order_id and row_order_id != clean_order_id:
            continue
        if not clean_order_id and clean_order_no and row_order_no != clean_order_no:
            continue
        if (
            clean_trade_date
            and _trade_identity_date(row_trade_date) != clean_trade_date
        ):
            continue
        if submitted_at and not (clean_order_id or clean_order_no):
            if not _match_not_before_submit(
                row_trade_date, row_match_time, submitted_at
            ):
                continue

        price = _to_float(row.get("price") if normalized_shape else row.get("F6"))
        trade_type = _to_int(
            row.get("trade_type") if normalized_shape else row.get("F2")
        )
        if normalized_shape:
            cts_qty = max(0, _to_int(row.get("cts_quantity")))
            shares = max(0, _to_int(row.get("filled_shares")))
            raw_quantity = max(0, _to_int(row.get("raw_quantity"), cts_qty))
            quantity_normalization = str(
                row.get("quantity_normalization") or "API_NORMALIZED"
            )
        else:
            qty = max(0, _to_int(row.get("F7")))
            normalized_shares = max(0, _to_int(row.get("_filled_shares")))
            if normalized_shares > 0:
                shares = normalized_shares
                cts_qty = qty
                quantity_normalization = str(
                    row.get("_quantity_normalization") or "API_COMPAT_NORMALIZED"
                )
            else:
                cts_qty = qty
                shares = qty * LOT_SIZE if trade_type in {0, 2} else qty
                quantity_normalization = "TRADING_LEGACY_FALLBACK"
            raw_quantity = max(0, _to_int(row.get("_raw_F7"), qty))

        if price is None or price <= 0 or shares <= 0:
            continue

        matched.append(
            {
                "symbol": row_symbol,
                "match_id": str(
                    row.get("match_id") if normalized_shape else row.get("F15") or ""
                ),
                "fee": _to_float(
                    row.get("fee") if normalized_shape else row.get("F12")
                ),
                "tax": _to_float(
                    row.get("tax") if normalized_shape else row.get("F13")
                ),
                "side": row_side,
                "trade_type": trade_type,
                "price": float(price),
                "raw_quantity": raw_quantity,
                "cts_quantity": cts_qty,
                "shares": shares,
                "quantity_normalization": quantity_normalization,
                "order_no": row_order_no,
                "order_id": row_order_id,
                "match_date": row_trade_date,
                "match_time": row_match_time,
            }
        )

    unique = {}
    for item in matched:
        identity = (item["match_date"], item["order_id"], item["order_no"],
                    item["symbol"], item["side"], item["match_id"])
        if not item["match_id"]:
            identity += (item["match_time"], item["price"], item["shares"])
        previous = unique.get(identity)
        if previous is not None and any(previous[k] != item[k] for k in ("shares", "price", "side")):
            raise ValueError("CONFLICTING_MATCH_ID")
        if previous is None:
            unique[identity] = item
    matched = list(unique.values())
    total_cts_qty = sum(item["cts_quantity"] for item in matched)
    total_shares = sum(item["shares"] for item in matched)
    weighted = sum(item["price"] * item["shares"] for item in matched)
    avg = weighted / total_shares if total_shares > 0 else None
    return {
        "filled": total_shares > 0,
        "average_fill_price": _round(avg, 6),
        "filled_shares": total_shares,
        "filled_cts_quantity": total_cts_qty,
        "fills": matched,
        "order_id": clean_order_id,
        "order_no": clean_order_no,
    }


def parse_cts_order_state(
    parsed_rows: Sequence[Mapping[str, Any]],
    *,
    symbol: Optional[str] = None,
    side: Optional[int] = None,
    order_id: Optional[str] = None,
    order_no: Optional[str] = None,
) -> Dict[str, Any]:
    """解析 Stock_QueryOrder，供委託狀態、撤單與成交對帳使用。

    CTS 委託查詢欄位：F5=商品、F4=買賣別、F7=委託價、F8=原委託量、
    F9=委託書號、F10=已成交量、F11=已取消量、F15=OID、F18=有效量。
    改單／刪單所需的 nQcurrent / nQmatch 直接使用查單回傳值，不自行換算單位。
    """
    candidates: List[Mapping[str, Any]] = []
    for row in parsed_rows:
        if not isinstance(row, Mapping):
            continue
        if symbol and str(row.get("F5") or "").strip() != str(symbol).strip():
            continue
        if side is not None and _to_int(row.get("F4"), -1) != int(side):
            continue
        if order_id and str(row.get("F15") or "").strip() != str(order_id).strip():
            continue
        if order_no and str(row.get("F9") or "").strip() != str(order_no).strip():
            continue
        candidates.append(row)

    if not candidates:
        return {
            "found": False,
            "active": False,
            "fully_filled": False,
            "partially_filled": False,
            "unfilled": False,
            "order_id": str(order_id or ""),
            "order_no": str(order_no or ""),
        }

    def _sort_key(row: Mapping[str, Any]) -> Tuple[str, str, str, str]:
        return (
            str(row.get("F19") or row.get("F12") or ""),
            str(row.get("F32") or row.get("F20") or ""),
            str(row.get("F31") or row.get("F13") or ""),
            str(row.get("F15") or ""),
        )

    row = sorted(candidates, key=_sort_key)[-1]
    original_quantity = max(0, _to_int(row.get("F8")))
    matched_quantity = max(0, _to_int(row.get("F10")))
    canceled_quantity = max(0, _to_int(row.get("F11")))
    current_quantity = max(0, _to_int(row.get("F18")))
    order_status = _to_int(row.get("F26"))
    status_desc = str(row.get("F14") or "").strip()
    code = str(row.get("F27") or "").strip()
    code_msg = str(row.get("F28") or "").strip()
    error_order = order_status == 1 or status_desc.upper() == "FALSE"
    fully_filled = original_quantity > 0 and matched_quantity >= original_quantity
    active = current_quantity > 0 and not error_order and not fully_filled

    return {
        "found": True,
        "account_id": str(row.get("F0") or ""),
        "trade_date": _to_int(row.get("F1")),
        "trade_type": _to_int(row.get("F2")),
        "order_type": _to_int(row.get("F3")),
        "side": _to_int(row.get("F4")),
        "symbol": str(row.get("F5") or ""),
        "price_type": _to_int(row.get("F6")),
        "price": _to_float(row.get("F7")),
        "original_quantity": original_quantity,
        "order_no": str(row.get("F9") or ""),
        "matched_quantity": matched_quantity,
        "canceled_quantity": canceled_quantity,
        "status_desc": status_desc,
        "order_id": str(row.get("F15") or ""),
        "pre_order": _to_int(row.get("F16")),
        "average_price": _to_float(row.get("F17")),
        "current_quantity": current_quantity,
        "condition": _to_int(row.get("F30")),
        "order_status": order_status,
        "code": code,
        "code_message": code_msg,
        "active": active,
        "fully_filled": fully_filled,
        "partially_filled": matched_quantity > 0 and not fully_filled,
        "unfilled": matched_quantity == 0 and active,
        "error_order": error_order,
        "raw": dict(row),
    }


def parse_cts_positions(
    parsed_rows: Sequence[Mapping[str, Any]],
    *,
    market: str = "",
) -> List[Dict[str, Any]]:
    """依名稱欄位辨識 CTS 庫存格式；成本原值另存，不視為成交均價。"""
    output: List[Dict[str, Any]] = []
    for row in parsed_rows:
        if not isinstance(row, Mapping):
            continue
        symbol = str(row.get("F1") or "").strip()
        extended = "F31" in row or "F32" in row
        # PDF 格式 F24=名稱；擴充格式 F24=零股今日、F31=名稱。
        name_field = "F31" if extended else "F24"
        ref_field = "F32" if extended else "F25"
        for field in (("F3", "F24") if extended else ("F3",)):
            value = _to_float(row.get(field))
            if value is None or not math.isfinite(value) or value < 0 or not value.is_integer():
                raise ValueError("CTS_POSITION_QUANTITY_INVALID")
        if not symbol:
            raise ValueError("CTS_POSITION_SYMBOL_MISSING")
        quantity = int(float(row["F3"]))
        if extended:
            quantity += int(float(row["F24"]))
        if not symbol or quantity <= 0:
            continue
        output.append(
            {
                "symbol": symbol,
                "market": _normalize_market(market),
                "quantity": quantity,
                "cost": None,
                "reference_price": _to_float(row.get(ref_field)),
                "name": str(row.get(name_field) or ""),
                "reported_cost": None if extended else _to_float(row.get("F23")),
                "trade_type": _to_int(row.get("F0")),
            }
        )
    combined = {}
    for item in output:
        key = (item["symbol"], item["market"])
        if key in combined and combined[key] != item:
            raise ValueError("CTS_POSITION_DUPLICATE_CONFLICT")
        combined[key] = item
    return sorted(combined.values(), key=lambda item: (item["market"], item["symbol"]))


def _unknown_order_candidates(order, rows):
    """僅用原始委託日期／毫秒時間；缺必要欄位不猜測。時間窗固定 30 秒。"""
    submitted = _parse_iso(order.get("submitted_at"))
    if submitted is None or submitted.tzinfo is None:
        return []
    required = {"F0": order.get("account_id"), "F2": order.get("trade_type"),
        "F3": order.get("request_order_type"), "F4": order.get("side"),
        "F5": order.get("symbol"), "F6": order.get("request_price_type"),
        "F8": order.get("requested_cts_quantity"), "F30": order.get("request_condition")}
    if any(v is None or str(v).strip() == "" for v in required.values()):
        return []
    matches = {}
    for row in rows:
        if any(str(row.get(k, "")).strip() != str(v).strip() for k,v in required.items()):
            continue
        day, clock = str(row.get("F12") or ""), str(row.get("F31") or "")
        if len(day) != 8 or len(clock) != 9 or not (day+clock).isdigit():
            continue
        try:
            stamp = datetime.strptime(day+clock, "%Y%m%d%H%M%S%f").replace(tzinfo=timezone(timedelta(hours=8)))
            if day != _trade_identity_date(order.get("trade_date")) or not 0 <= (stamp-submitted).total_seconds() <= 30:
                continue
            if Decimal(str(row["F7"])) != Decimal(str(order["requested_price"])):
                continue
        except (ValueError, KeyError, TypeError, ArithmeticError):
            continue
        oid, ono = str(row.get("F15") or "").strip(), str(row.get("F9") or "").strip()
        if oid and ono:
            matches[(oid, ono)] = row
    return list(matches.values())


def reconcile_system_order_states(
    order_rows: Sequence[Mapping[str, Any]], *, account_id: str, trade_date: Any
) -> list:
    reports = []
    registry = get_system_order_registry()
    unknown = [o for o in registry if o.get("account_id") == account_id
               and _trade_identity_date(o.get("trade_date")) == _trade_identity_date(trade_date)
               and not (o.get("order_id") or o.get("order_no"))
               and o.get("order_state") in {"STATUS_UNKNOWN", "REQUEST_SENT", "SUBMITTING"}]
    proposals = {o["registry_key"]: _unknown_order_candidates(o, order_rows) for o in unknown}
    for order in registry:
        if order.get("account_id") != account_id or _trade_identity_date(
            order.get("trade_date")
        ) != _trade_identity_date(trade_date):
            continue
        oid, ono = str(order.get("order_id") or ""), str(order.get("order_no") or "")
        if not (oid or ono):
            if order.get("order_state") not in {"STATUS_UNKNOWN", "REQUEST_SENT", "SUBMITTING"}:
                continue
            candidates = proposals.get(order["registry_key"], [])
            chosen = candidates[0] if len(candidates) == 1 else None
            if chosen:
                candidate_oid, candidate_no = str(chosen["F15"]), str(chosen["F9"])
                # 同一券商單不可被兩個未知 intent 或已知委託重複認領。
                claimed = any(o["registry_key"] != order["registry_key"] and
                    (o.get("order_id") == candidate_oid or o.get("order_no") == candidate_no)
                    for o in registry if o.get("account_id") == account_id and
                    _trade_identity_date(o.get("trade_date")) == _trade_identity_date(trade_date))
                ambiguous = any(k != order["registry_key"] and any(
                    str(r.get("F15")) == candidate_oid or str(r.get("F9")) == candidate_no for r in rs)
                    for k,rs in proposals.items())
                if claimed or ambiguous:
                    chosen = None
            report = {"registry_key": order["registry_key"], "symbol": order.get("symbol"),
                      "action": order.get("action"), "candidate_count": len(candidates),
                      "submitted_at": order.get("submitted_at"),
                      "status": "IDENTITY_RECOVERED" if chosen else "MANUAL_ATTENTION",
                      "reason": "UNIQUE_ORIGINAL_ORDER_MATCH" if chosen else "MISSING_FIELDS_NO_MATCH_OR_AMBIGUOUS_OWNERSHIP"}
            if not chosen:
                attempts = int(order.get("unknown_query_attempts") or 0) + 1
                _update_system_order_identity(order["registry_key"],
                    unknown_query_attempts=attempts, recovery_phase="MANUAL_ATTENTION")
                reports.append(report)
                continue
            oid, ono = str(chosen["F15"]), str(chosen["F9"])
            _update_system_order_identity(order["registry_key"], order_id=oid, order_no=ono,
                                          recovery_phase="IDENTITY_RECOVERED")
            report.update(order_id=oid, order_no=ono, broker_original_date=chosen["F12"],
                          broker_original_time_ms=chosen["F31"], price=chosen["F7"], quantity=chosen["F8"])
            reports.append(report)
        state = parse_cts_order_state(
            [r for r in order_rows if str(r.get("F0") or "") == account_id],
            symbol=order["symbol"],
            side=order["side"],
            order_id=oid or None,
            order_no=ono if not oid else None,
        )
        if not state.get("found"):
            continue
        if state.get("error_order"):
            status = "REJECTED"
        elif state.get("fully_filled"):
            status = "FILLED"
        elif state.get("active"):
            status = (
                "PARTIALLY_FILLED"
                if state.get("partially_filled")
                else "SUBMITTED_NOT_FILLED"
            )
        elif state.get("canceled_quantity", 0) > 0:
            status = "CANCELED"
        else:
            continue
        _update_system_order_identity(
            order["registry_key"], status=status,
            filled_shares=state["matched_quantity"] * LOT_SIZE,
        )
    return reports


def confirm_unknown_order_absence(order_rows, match_rows, position_rows, *, account_id,
                                  trade_date, cts, queries_complete, now=None):
    """完整強制對帳與已結束 COM 呼叫才可證明不存在；單次 EMPTY 不解鎖。"""
    if not queries_complete or not callable(getattr(cts, "has_inflight_order", None)):
        return []
    current = now or datetime.now(timezone(timedelta(hours=8)))
    reports = []
    for order in get_system_order_registry():
        if (order.get("account_id") != account_id
                or _trade_identity_date(order.get("trade_date")) != _trade_identity_date(trade_date)
                or order.get("order_state") not in {"STATUS_UNKNOWN", "SUBMITTING", "REQUEST_SENT"}
                or order.get("order_id") or order.get("order_no")):
            continue
        key, symbol = order["registry_key"], order["symbol"]
        # 同股票任何券商紀錄均先保留鎖，缺少欄位不當成沒有委託。
        possible = (any(str(row.get("symbol") or row.get("F5") or "") in {"", symbol} for row in order_rows)
                    or any(str(row.get("symbol") or row.get("F3") or "") in {"", symbol} for row in match_rows)
                    or any(str(row.get("symbol") or "") in {"", symbol} for row in position_rows))
        submitted = _parse_iso(order.get("submitted_at"))
        if possible or cts.has_inflight_order(key) or submitted is None:
            with _SYSTEM_ORDER_REGISTRY_LOCK:
                _SYSTEM_ORDER_REGISTRY[key].pop("absence_first_confirmed_at", None)
            continue
        elapsed = (current - submitted).total_seconds()
        first = _parse_iso(order.get("absence_first_confirmed_at"))
        if first is None:
            with _SYSTEM_ORDER_REGISTRY_LOCK:
                _SYSTEM_ORDER_REGISTRY[key]["absence_first_confirmed_at"] = current.isoformat()
            continue
        if elapsed < 30 or (current - first).total_seconds() < 2:
            continue
        updated = _update_system_order_identity(key, status="CONFIRMED_ABSENT",
                                                recovery_phase="THREE_QUERIES_CONFIRMED_ABSENT")
        reports.append({"registry_key": key, "order_intent_id": order["order_intent_id"],
                        "symbol": symbol, "status": updated.get("order_state"),
                        "reason": "NO_INFLIGHT_CALL_AND_REPEATED_COMPLETE_ORDER_MATCH_POSITION_ABSENCE"})
    return reports


def reconcile_managed_positions(
    inventory: Sequence[Mapping[str, Any]],
    matches: Sequence[Mapping[str, Any]],
    *,
    account_id: str,
    trade_date: Any,
    parameters: Optional[TradingParameters] = None,
) -> Dict[str, Any]:
    """以本帳號當日已確認委託身分重建持倉；重複完整對帳不重複累加。

    公式與成交歸屬由 Trading 負責，Runtime 只交換這份完整結果。
    不接管無法確認歸屬的外部成交，也不以庫存成本充當系統成交均價。
    """
    params = parameters or TradingParameters()
    registry = [
        row
        for row in get_system_order_registry()
        if str(row.get("account_id") or "") == str(account_id)
        and _trade_identity_date(row.get("trade_date"))
        == _trade_identity_date(trade_date)
    ]
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    buy_orders: set[str] = set()
    for order in registry:
        oid, ono = str(order.get("order_id") or ""), str(order.get("order_no") or "")
        if not (oid or ono):
            continue
        fill = summarize_cts_fills(
            matches,
            symbol=order["symbol"],
            side=order["side"],
            order_id=oid or None,
            order_no=ono or None,
            trade_date=str(trade_date),
        )
        # 不讓不合理回報擴大持倉。
        if fill["filled_shares"] > int(order.get("requested_shares") or 0):
            raise ValueError("FILL_QUANTITY_EXCEEDS_REQUEST")
        # 舊登錄 CONFIRMED 可能仍保留；只依明確成交數修正，不猜測已撤單。
        confirmed_shares = int(fill.get("filled_shares") or 0)
        requested_shares = int(order.get("requested_shares") or 0)
        if requested_shares > 0 and confirmed_shares == requested_shares:
            order.update(
                _update_system_order_identity(
                    order["registry_key"],
                    status="FILLED",
                    filled_shares=confirmed_shares,
                )
            )
        elif confirmed_shares > 0 and order.get("status") not in {
            "CANCELED",
            "CANCELLED",
            "REJECTED",
        }:
            order.update(
                _update_system_order_identity(
                    order["registry_key"],
                    status="PARTIALLY_FILLED",
                    filled_shares=confirmed_shares,
                )
            )
        for row in fill["fills"]:
            grouped.setdefault((order["symbol"], order["market"]), []).append(
                {**row, "ownership": order}
            )
        if order["side"] == 1 and fill["filled"]:
            buy_orders.add(order["registry_key"])
    output = [dict(row) for row in inventory]
    discrepancies = []
    realized = 0.0
    fees = 0.0
    closed = []
    symbol_entry_counts = {}
    symbol_last_exit = {}
    strategy_daily_pnl = {"MOMENTUM": 0.0, "TREND": 0.0}
    for (symbol, market), fills in sorted(grouped.items()):
        quantity, cost = 0, 0.0
        initial_quantity = 0
        partial_done = False
        partial_stage = ""
        trade_net = 0.0
        ownership = {}
        entry_time = None
        for fill in sorted(
            fills,
            key=lambda row: (
                row["match_date"],
                row["match_time"],
                row["side"],
                row["order_id"],
            ),
        ):
            shares, price = int(fill["shares"]), float(fill["price"])
            fees += float(fill.get("fee") or 0) + float(fill.get("tax") or 0)
            if fill["side"] == 1:
                if quantity == 0:
                    entry_time = _fill_entry_timestamp(fill)
                    initial_quantity = 0
                    partial_done = False
                    partial_stage = ""
                    trade_net = 0.0
                    symbol_entry_counts[symbol] = symbol_entry_counts.get(symbol, 0) + 1
                quantity += shares
                initial_quantity += shares
                cost += shares * price
                trade_net -= float(fill.get("fee") or 0) + float(fill.get("tax") or 0)
                ownership = fill["ownership"]
                strategy = str(ownership.get("strategy_type") or "UNKNOWN")
                strategy_daily_pnl[strategy] = strategy_daily_pnl.get(strategy, 0.0) - float(fill.get("fee") or 0) - float(fill.get("tax") or 0)
            elif quantity:
                sold = min(quantity, shares)
                avg = cost / quantity
                realized += sold * (price - avg)
                strategy = str(ownership.get("strategy_type") or "UNKNOWN")
                strategy_daily_pnl[strategy] = strategy_daily_pnl.get(strategy, 0.0) + sold * (price - avg) - float(fill.get("fee") or 0) - float(fill.get("tax") or 0)
                trade_net += (
                    sold * (price - avg)
                    - float(fill.get("fee") or 0)
                    - float(fill.get("tax") or 0)
                )
                cost -= sold * avg
                quantity -= sold
                if quantity == 0:
                    exit_at = _fill_entry_timestamp(fill)
                    symbol_last_exit[symbol] = exit_at
                    closed.append((exit_at or "", trade_net))
                if fill["ownership"].get("exit_stage") == "MOMENTUM_DECAY_PARTIAL":
                    partial_done = True
                    partial_stage = "MOMENTUM_DECAY_PARTIAL"

        found = next(
            (
                row
                for row in output
                if row.get("symbol") == symbol
                and (
                    not row.get("market")
                    or _normalize_market(row.get("market")) == market
                )
            ),
            None,
        )
        broker_quantity = max(0, int((found or {}).get("quantity") or 0))
        if quantity > broker_quantity:
            discrepancies.append({"symbol": symbol, "market": market,
                                  "fill_quantity": quantity, "broker_quantity": broker_quantity})
        if found is None:
            continue
        found["market"] = market
        if quantity > 0:
            managed = build_position_from_fill(
                {"average_fill_price": cost / quantity, "filled_shares": quantity},
                symbol=symbol,
                market=market,
                parameters=params,
                entry_time=entry_time,
                ownership=ownership,
            )
            managed.update(
                initial_quantity=initial_quantity,
                remaining_quantity=quantity,
                partial_exit_done=partial_done,
                partial_exit_stage=partial_stage,
            )
            # 保留券商實際庫存；成交與庫存不同步時不放大可賣數量。
            found.update(managed)
            found["quantity"] = broker_quantity
            found["managed_quantity"] = min(quantity, broker_quantity)
            found["remaining_quantity"] = min(quantity, broker_quantity)
            found["managed_sellable_quantity"] = min(
                int(found.get("managed_sellable_quantity") or 0), broker_quantity)
            found["position_reconciliation_pending"] = quantity > broker_quantity
        else:
            # 保留外部庫存，但不再讓已平倉的系統部位被自動賣出。
            found.update(
                managed_quantity=0,
                managed_sellable_quantity=0,
                managed_by="",
                entry_price=None,
                stop_price=None,
                take_profit_price=None,
            )
    consecutive = 0
    cooldown = None
    for timestamp, net in sorted(closed):
        consecutive = consecutive + 1 if net < 0 else 0
        dt = _parse_iso(timestamp)
        if consecutive >= params.max_consecutive_losses and dt:
            cooldown = (
                dt + timedelta(minutes=params.loss_cooldown_minutes)
            ).isoformat()
    return {
        "positions": output,
        "consistent": not discrepancies,
        "position_discrepancies": discrepancies,
        "daily_trade_count": sum(symbol_entry_counts.values()),
        "strategy_daily_pnl": strategy_daily_pnl,
        "symbol_entry_counts": symbol_entry_counts,
        "symbol_last_exit": symbol_last_exit,
        "consecutive_losses": consecutive,
        "loss_cooldown_until": cooldown,
        "completed_position_count": len(closed),
        "realized_gross_pnl": round(realized, 6),
        "reported_fees_and_taxes": round(fees, 6),
        "daily_pnl": round(realized - fees, 6),
    }


def build_position_from_fill(
    fill_summary: Mapping[str, Any],
    *,
    symbol: str,
    market: str,
    parameters: Optional[TradingParameters] = None,
    entry_time: Optional[str] = None,
    ownership: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """以 CTS 實際成交均價建立最終停損／停利，不使用 signal_price。"""
    params = parameters or TradingParameters()
    params.validate()
    fill_price = _to_float(fill_summary.get("average_fill_price"))
    filled_shares = _to_int(fill_summary.get("filled_shares"))
    if fill_price is None or fill_price <= 0 or filled_shares <= 0:
        raise ValueError("CTS fill_summary 沒有有效成交價或成交股數")

    position = {
        "symbol": str(symbol),
        "market": _normalize_market(market),
        "quantity": int(filled_shares),
        "entry_price": _round(fill_price, 6),
        "entry_time": entry_time,
        "price_basis": "CTS_ACTUAL_AVERAGE_FILL_PRICE",
    }
    if isinstance(ownership, Mapping):
        owner = str(ownership.get("owner") or "").strip()
        source = str(ownership.get("management_source") or "").strip()
        if owner:
            position["managed_by"] = owner
            position["entry_owner"] = owner
        if source:
            position["management_source"] = source
        position["ownership_status"] = str(ownership.get("ownership_status") or "")
        position["entry_trigger"] = str(ownership.get("trigger") or "")
        position["entry_order_id"] = str(ownership.get("order_id") or "")
        position["entry_order_no"] = str(ownership.get("order_no") or "")
        if owner == "MOMENTUM_SCALPING":
            position["managed_trade_date"] = _trade_identity_date(
                ownership.get("trade_date")
            )
            position["managed_account_id"] = str(ownership.get("account_id") or "")
            position["managed_quantity"] = int(filled_shares)
            position["managed_sellable_quantity"] = int(filled_shares)
    ownership = ownership or {}
    for key in STRATEGY_METADATA_FIELDS:
        if ownership.get(key) is not None:
            position[key] = ownership[key]
    position["strategy_type"] = ownership.get("strategy_type") or "UNKNOWN"
    if position["strategy_type"] == "MANUAL":
        position["managed_by"] = "MANUAL"
        position["management_source"] = "人工市價處置"
    position["initial_quantity"] = int(filled_shares)
    position["position_cycle_id"] = str(ownership.get("position_cycle_id") or "")
    position.setdefault("entry_setup_type", "")
    position.setdefault("exit_stage", "")
    position.setdefault("strategy_stop_price", None)
    position.setdefault("entry_structure_reference", None)
    position["remaining_quantity"] = filled_shares
    position["partial_exit_done"] = bool(ownership.get("partial_exit_done"))
    position["emergency_stop_price"] = fill_price * (
        1 - params.emergency_stop_loss_pct / 100
    )
    structure = _to_float(ownership.get("strategy_stop_price"))
    position["effective_stop_price"] = max(
        structure or 0, position["emergency_stop_price"]
    )
    position["stop_price"] = position["effective_stop_price"]
    position["take_profit_price"] = None
    position["open_risk"] = filled_shares * max(
        0, fill_price - position["effective_stop_price"]
    )
    return position


# =============================================================================
# 純風控核心
# =============================================================================


def _candidate_day_trade_eligibility(
    signal_context: Mapping[str, Any],
) -> Optional[bool]:
    candidate = signal_context.get("candidate")
    if not isinstance(candidate, Mapping):
        return None
    value = candidate.get("day_trade_eligible")
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "eligible", "可", "可當沖"}:
        return True
    if text in {"0", "false", "no", "n", "ineligible", "不可", "不可當沖"}:
        return False
    return None


def _derive_symbol_market(
    signal_context: Mapping[str, Any], current_quote: Mapping[str, Any]
) -> Tuple[str, str]:
    symbol = str(
        signal_context.get("symbol") or current_quote.get("symbol") or ""
    ).strip()
    market = _normalize_market(
        signal_context.get("market") or current_quote.get("market")
    )
    return symbol, market


def _execution_metrics(
    signal_price: float,
    current_price: float,
    bid: Optional[float],
    ask: Optional[float],
    symbol: str = "",
) -> Dict[str, Optional[float]]:
    valid_book = (
        bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid
    )
    if valid_book:
        spread_pct = _pct(ask - bid, current_price)
        raw_order_price = ask
    else:
        spread_pct = None
        raw_order_price = current_price

    order_price = _price_to_tick(raw_order_price, side="BUY", symbol=symbol)
    slippage_pct = max(0.0, _pct(order_price - current_price, current_price))
    chase_pct = max(0.0, _pct(current_price - signal_price, signal_price))
    friction = (spread_pct or 0.0) + slippage_pct + chase_pct
    return {
        "spread_pct": _round(spread_pct, 4),
        "slippage_pct": _round(slippage_pct, 4),
        "chase_pct": _round(chase_pct, 4),
        "execution_friction_pct": _round(friction, 4),
        "order_price": _round(order_price, 4),
    }


def _quote_last_price(quote: Mapping[str, Any]) -> Optional[float]:
    """APIGateway 若提供 last 則優先使用；既有正規化 price 視為 last 別名。"""
    if "last" in quote:
        return _to_float(quote.get("last"))
    return _to_float(quote.get("price"))


def _day_change_gate(quote, executable_price, params):
    """兩策略 BUY 共用；以最新可執行價相對昨收計算，不猜測漲停價。"""
    previous = _to_float(quote.get("prev_close"))
    price = _to_float(executable_price)
    change = (price / previous - 1) * 100 if price and previous and previous > 0 else None
    code = ""
    if params.max_entry_day_change_pct > 0:
        if change is None:
            code = "BUY_DAY_REFERENCE_MISSING"
        elif change + 1e-9 >= params.max_entry_day_change_pct:
            code = "BUY_DAY_CHANGE_LIMIT"
    return {"ok": not code, "rejection_code": code, "day_change_pct": change,
            "max_entry_day_change_pct": params.max_entry_day_change_pct,
            "reference_price": previous, "executable_price": price}


def _entry_momentum_revalidation(
    signal_context,
    last_price,
    params,
    *,
    now=None,
):
    """持倉策略專屬再確認；最新價格不得破壞已確認的結構。"""
    strategy = signal_context.get("strategy_type")
    metrics = signal_context.get("metrics") or {}
    market = signal_context.get("market_context") or {}
    stop = _to_float(signal_context.get("strategy_stop_price"))
    vwap = _to_float(metrics.get("vwap"))
    reasons = []
    if strategy not in {"MOMENTUM", "TREND"}:
        reasons.append("STRATEGY_IDENTITY_MISSING")
    signal_price = float(signal_context.get("signal_price") or 0)
    # 下限不依交易跳動單位取整；只有實際委託價才依 Ask 與跳動單位形成。
    entry_trigger_floor = signal_price * (1 - params.max_entry_trigger_retrace_pct / 100)
    trigger_passed = last_price >= entry_trigger_floor
    retrace_pct = (last_price / signal_price - 1) * 100 if signal_price > 0 else None
    if not trigger_passed:
        reasons.append("ENTRY_TRIGGER_LOST")
    if stop is None or last_price <= stop:
        reasons.append("ENTRY_STRUCTURE_LOST")
    if vwap is None or last_price <= vwap:
        reasons.append("ENTRY_VWAP_LOST")
    if strategy == "TREND":
        sector_reason = load_market_module().candidate_sector_gate(
            signal_context.get("candidate") or {},
            now=now or datetime.now(timezone(timedelta(hours=8))),
        )
        if sector_reason:
            reasons.append(sector_reason)
        setup = signal_context.get("entry_setup_type")
        if setup not in {"TREND_INITIAL_ENTRY", "TREND_RESUME"} or signal_context.get("entry_signal") is not True:
            reasons.append("TREND_ENTRY_SETUP_REQUIRED")
        if setup == "TREND_INITIAL_ENTRY" and metrics.get("trend_structure_valid") is not True:
            reasons.append("TREND_INITIAL_STRUCTURE_INVALID")
        if not load_market_module().strategy_market_permission(market, now=now)["allowed"]:
            reasons.append("TREND_MARKET_INVALID")
        if not (
            metrics.get("ema5", 0) > metrics.get("ema20", 0)
            and (metrics.get("ema_fast_slope") or 0) > 0
        ):
            reasons.append("TREND_NOT_CONFIRMED")
    elif strategy == "MOMENTUM":
        if market.get("momentum_long_allowed") is not True:
            reasons.append("MOMENTUM_MARKET_INVALID")
        anchor = _to_float(signal_context.get("entry_wave_anchor_price"))
        if anchor is None or last_price <= anchor:
            reasons.append("MOMENTUM_STRUCTURE_LOST")
        if signal_context.get("entry_wave_status") == "ENTRY_WAVE_TOO_OLD":
            reasons.append("MOMENTUM_LATE_WAVE")
    return {
        "ok": not reasons,
        "reasons": reasons,
        "revalidation_type": strategy,
        "signal_price": signal_price,
        "entry_trigger_floor": entry_trigger_floor,
        "last_price": last_price,
        "retrace_pct": retrace_pct,
        "max_entry_trigger_retrace_pct": float(params.max_entry_trigger_retrace_pct),
        "trigger_passed": trigger_passed,
    }


def _entry_wave_extension_revalidation(
    signal_context: Mapping[str, Any],
    executable_price: Optional[float],
    params: TradingParameters,
) -> Dict[str, Any]:
    """以 CTS 前最新可執行價格檢查 AUTO ENTRY 整波延伸。

    AUTO BUY 必須直接使用 Intraday 提供的 entry_wave_anchor_price；缺少或無效時
    fail-closed，禁止退回 signal_price。非 AUTO BUY 不套用此 Gate。
    """
    execution_trigger = _execution_trigger(signal_context)
    required = (
        execution_trigger == "AUTO"
        and signal_context.get("strategy_type") == "MOMENTUM"
    )
    raw_anchor = signal_context.get("entry_wave_anchor_price")
    anchor = None if isinstance(raw_anchor, bool) else _to_float(raw_anchor)
    result: Dict[str, Any] = {
        "required": required,
        "execution_trigger": execution_trigger,
        "entry_wave_anchor_price": _round(anchor, 6),
        "executable_price": _round(executable_price, 6),
        "wave_extension_pct": None,
        "max_breakout_extension_pct": float(params.max_breakout_extension_pct),
        "ok": True,
        "rejection_code": "",
    }
    if not required:
        return result

    if anchor is None or anchor <= 0 or not math.isfinite(anchor):
        result["ok"] = False
        result["rejection_code"] = "BUY_REVALIDATION_WAVE_ANCHOR_INVALID"
        return result

    if (
        executable_price is None
        or executable_price <= 0
        or not math.isfinite(executable_price)
    ):
        # 可執行價格本身的失敗由既有 Quote / Bid-Ask Revalidation code 負責。
        return result

    extension_pct = _pct(executable_price - anchor, anchor)
    result["wave_extension_pct"] = _round(extension_pct, 6)
    if extension_pct > params.max_breakout_extension_pct:
        result["ok"] = False
        result["rejection_code"] = "BUY_REVALIDATION_WAVE_EXTENSION_TOO_HIGH"
    return result


def _live_trend_permission(gateway, signal_context, *, now=None):
    """更新：送單端重讀 Runtime 的同一份市場許可，失效或讀取失敗即禁止。"""
    try:
        provider = getattr(gateway, "get_strategy_market_context", None)
        if not callable(provider):
            return {"allowed": False, "reason": "MARKET_PERMISSION_PROVIDER_MISSING"}
        current = provider()
        check = load_market_module().strategy_market_permission(current, now=now or datetime.now(timezone(timedelta(hours=8))))
        signal_context["market_context"] = current
        signal_context["market_permission_snapshot_id"] = current.get("market_permission_snapshot_id")
        if signal_context.get("strategy_type") == "TREND":
            quote = gateway.get_quote(str(signal_context.get("symbol") or ""), signal_context.get("market"))
            candidate = load_market_module().refresh_candidate_sector(signal_context.get("candidate"), current,
                now=now or datetime.now(timezone(timedelta(hours=8))), quote=quote)
            signal_context["candidate"] = candidate
            sector_reason = load_market_module().candidate_sector_gate(candidate,
                now=now or datetime.now(timezone(timedelta(hours=8))))
            if sector_reason:
                check = dict(check, allowed=False, reason=sector_reason)

        if signal_context.get("strategy_type") == "MOMENTUM":
            check = dict(check, allowed=check.get("data_ready") is True and current.get("momentum_long_allowed") is True and current.get("market_state") != "PANIC")
        return check
    except Exception as exc:
        return {"allowed": False, "reason": "MARKET_PERMISSION_READ_FAILED", "error": type(exc).__name__}


def _gate_report(method):
    import functools
    @functools.wraps(method)
    def wrapped(*args, **kwargs):
        result = method(*args, **kwargs)
        codes = list(result.get("rejection_codes") or [])
        for name in ("decision", "revalidation", "buy_revalidation", "sell_revalidation"):
            nested = result.get(name)
            if isinstance(nested, Mapping):
                codes.extend(nested.get("rejection_codes") or [])
        codes = list(dict.fromkeys(codes))
        result["primary_rejection_code"] = codes[0] if codes else None
        result["secondary_rejection_codes"] = codes[1:]
        params = kwargs.get("parameters") or TradingParameters()
        result["gate_interaction"] = {name: {"value": result.get(name), "limit": getattr(params, limit),
              "evaluated": result.get(name) is not None, "passed": result.get(name) is not None and result[name] <= getattr(params,limit)}
              for name,limit in (("spread_pct","max_spread_pct"),("slippage_pct","max_slippage_pct"),
              ("chase_pct","max_chase_pct"),("execution_friction_pct","max_execution_friction_pct"))}
        result["gate_interaction"]["extension"] = dict(result.get("wave_extension") or {})
        result["gate_interaction"]["day_change"] = dict(result.get("day_change_gate") or {})
        return result
    return wrapped


@_gate_report
def revalidate_buy_before_order(
    gateway: Any,
    decision: Mapping[str, Any],
    signal_context: Mapping[str, Any],
    *,
    parameters: Optional[TradingParameters] = None,
    now: Optional[datetime] = None,
    market_state_snapshot: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """BUY 送進 CTS 前，以單一 Fugle MarketState atomic snapshot 做最後 Revalidation。

    不呼叫 Scanner、不重新排名 Candidate，也不向外部行情 API 發 HTTP request。
    price / bid / ask / event_time / serial 必須全部來自同一份 frozen snapshot。
    """
    params = parameters or TradingParameters()
    params.validate()

    symbol = str(decision.get("symbol") or signal_context.get("symbol") or "").strip()
    market = _normalize_market(decision.get("market") or signal_context.get("market"))
    result: Dict[str, Any] = {
        "ok": False,
        "symbol": symbol,
        "market": market,
        "market_state_snapshot": {},
        "market_state_snapshot_fingerprint": "",
        "quote": {},
        "quote_age_seconds": None,
        "event_age_seconds": None,
        "event_time": None,
        "event_serial": None,
        "event_fresh": False,
        "freshness_basis": "FUGLE_EVENT_TIME",
        "freshness_threshold_seconds": float(
            params.buy_revalidation_max_quote_age_seconds
        ),
        "rejection_codes": [],
        "rejection_reasons": [],
        "spread_pct": None,
        "slippage_pct": None,
        "chase_pct": None,
        "execution_friction_pct": None,
        "order_price": None,
        "entry_momentum": {},
        "wave_extension": {},
        "risk_guard": {},
    }

    signal_context = dict(signal_context)
    if signal_context.get("strategy_type") == "TREND" or callable(getattr(gateway, "get_strategy_market_context", None)):
        permission = _live_trend_permission(gateway, signal_context, now=now)
        result["strategy_market_permission"] = permission
        if not permission["allowed"]:
            result["rejection_codes"] = ["TREND_MARKET_INVALID"]
            result["rejection_reasons"] = [permission["reason"]]
            return result

    try:
        snapshot = (
            dict(market_state_snapshot)
            if isinstance(market_state_snapshot, Mapping)
            else _freeze_atomic_market_state_snapshot(
                gateway,
                symbol,
                market,
                now=now,
            )
        )
    except Exception as exc:
        codes, reasons = _reject(["BUY_REVALIDATION_MARKET_STATE_UNAVAILABLE"])
        result.update(
            {
                "rejection_codes": codes,
                "rejection_reasons": reasons,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return result

    snapshot.setdefault(
        "snapshot_fingerprint", _market_state_snapshot_fingerprint(snapshot)
    )
    fields = _atomic_snapshot_execution_fields(snapshot)
    last_price = _to_float(fields.get("price"))
    bid = _to_float(fields.get("bid"))
    ask = _to_float(fields.get("ask"))
    freshness = _event_freshness_from_snapshot(
        snapshot,
        now=now,
        max_age_seconds=params.buy_revalidation_max_quote_age_seconds,
    )
    _record_event_age_sample("BUY_REVALIDATION", snapshot, freshness)
    result.update(
        {
            "market_state_snapshot": dict(snapshot),
            "market_state_snapshot_fingerprint": str(
                fields.get("snapshot_fingerprint") or ""
            ),
            "quote": dict(snapshot),
            "quote_age_seconds": freshness.get("event_age_seconds"),
            "event_age_seconds": freshness.get("event_age_seconds"),
            "event_time": freshness.get("event_time"),
            "event_serial": freshness.get("event_serial"),
            "event_fresh": bool(freshness.get("event_fresh")),
            "freshness_basis": freshness.get("freshness_basis"),
            "freshness_threshold_seconds": freshness.get("freshness_threshold_seconds"),
            "quote_sequence": freshness.get("quote_sequence"),
            "symbol_quote_sequence": freshness.get("symbol_quote_sequence"),
        }
    )

    contract = _buy_market_data_contract(snapshot, params, now=now)
    result.update({key: value for key, value in contract.items() if key not in ("ok", "rejection_codes")})
    rejection_codes: List[str] = list(contract["rejection_codes"])
    if freshness.get("event_time") is None or freshness.get("event_serial") is None:
        rejection_codes.append("BUY_REVALIDATION_QUOTE_TIMESTAMP_INVALID")
    elif (
        _to_float(freshness.get("event_age_seconds")) is None
        or (_to_float(freshness.get("event_age_seconds")) or 0.0) < -1.0
    ):
        rejection_codes.append("BUY_REVALIDATION_QUOTE_TIMESTAMP_INVALID")
    elif bool(freshness.get("event_stale")):
        # 保留既有 rejection code 以維持 log / GUI 相容；語意已改為 WebSocket Event freshness。
        rejection_codes.append("BUY_REVALIDATION_QUOTE_STALE")

    if last_price is None or last_price <= 0:
        rejection_codes.append("BUY_REVALIDATION_LAST_INVALID")
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        rejection_codes.append("BUY_REVALIDATION_BID_ASK_INVALID")

    auto_entry_wave = _entry_wave_extension_revalidation(signal_context, None, params)
    result["wave_extension"] = auto_entry_wave
    if not auto_entry_wave.get("ok"):
        rejection_code = str(auto_entry_wave.get("rejection_code") or "")
        if rejection_code:
            rejection_codes.append(rejection_code)

    signal_price = _to_float(signal_context.get("signal_price"))
    if (
        signal_price is not None
        and signal_price > 0
        and last_price is not None
        and last_price > 0
        and bid is not None
        and ask is not None
        and bid > 0
        and ask > 0
        and ask >= bid
    ):
        metrics = _execution_metrics(signal_price, last_price, bid, ask, symbol)
        result.update(metrics)
        spread_pct = _to_float(metrics.get("spread_pct"))
        slippage_pct = _to_float(metrics.get("slippage_pct")) or 0.0
        chase_pct = _to_float(metrics.get("chase_pct")) or 0.0
        friction_pct = _to_float(metrics.get("execution_friction_pct")) or 0.0
        order_price = _to_float(metrics.get("order_price"))

        if spread_pct is not None and spread_pct > params.max_spread_pct:
            rejection_codes.append("BUY_REVALIDATION_SPREAD_TOO_WIDE")
        if slippage_pct > params.max_slippage_pct:
            rejection_codes.append("BUY_REVALIDATION_SLIPPAGE_TOO_HIGH")
        if chase_pct > params.max_chase_pct:
            rejection_codes.append("BUY_REVALIDATION_CHASE_TOO_HIGH")
        if friction_pct > params.max_execution_friction_pct:
            rejection_codes.append("BUY_REVALIDATION_EXECUTION_FRICTION_TOO_HIGH")
        if order_price is not None and order_price > params.max_buy_price:
            rejection_codes.append("BUY_REVALIDATION_MAX_BUY_PRICE_EXCEEDED")

        account_summary = decision.get("account_summary")
        account_summary = (
            account_summary if isinstance(account_summary, Mapping) else {}
        )
        available_cash = _to_float(account_summary.get("available_cash"))
        equity = _to_float(account_summary.get("equity"))
        if (
            order_price is not None
            and order_price > 0
            and available_cash is not None
            and available_cash > 0
        ):
            if equity is None or equity <= 0:
                equity = available_cash
            sizing = _size_entry(
                order_price,
                available_cash,
                equity,
                params,
                _to_float(signal_context.get("strategy_stop_price")),
                account_summary.get("remaining_entry_risk_budget"),
            )
            planned_size = max(0, _to_int(decision.get("position_size")))
            safe_size = max(0, _to_int(sizing.get("position_size")))
            if planned_size > 0 and safe_size >= planned_size:
                sizing["position_size"] = planned_size
                sizing["cts_trade_type"] = _to_int(decision.get("cts_trade_type"))
                sizing["cts_quantity"] = _to_int(decision.get("cts_quantity"))
                sizing["estimated_position_cost"] = _round(
                    planned_size * order_price, 2
                )
            result["risk_guard"] = sizing
            if (
                _to_int(sizing.get("position_size")) <= 0
                or _to_int(sizing.get("cts_quantity")) <= 0
            ):
                rejection_codes.append("POSITION_SIZE_ZERO")
            if (
                _to_float(sizing.get("estimated_position_cost")) or 0.0
            ) > available_cash:
                rejection_codes.append("INSUFFICIENT_CASH")

        wave_extension = _entry_wave_extension_revalidation(
            signal_context, order_price, params
        )
        result["wave_extension"] = wave_extension
        wave_code = str(wave_extension.get("rejection_code") or "")
        if not wave_extension.get("ok") and wave_code:
            rejection_codes.append(wave_code)

        momentum = _entry_momentum_revalidation(
            signal_context,
            last_price,
            params,
            now=now,
        )
        result["entry_momentum"] = momentum
        trigger_logger = getattr(gateway, "record_entry_trigger_revalidation", None)
        if callable(trigger_logger):
            trigger_logger(dict(momentum, symbol=symbol))
        if not momentum.get("ok"):
            rejection_codes.extend(reason for reason in momentum.get("reasons", []) if reason.startswith("SECTOR_"))
            rejection_codes.append("BUY_REVALIDATION_ENTRY_MOMENTUM_LOST")

    if result.get("order_price"):
        executable = float(result["order_price"])
        day_gate = _day_change_gate(snapshot, executable, params)
        result["day_change_gate"] = day_gate
        if not day_gate["ok"]:
            rejection_codes.append(day_gate["rejection_code"])
        stop = _to_float(signal_context.get("strategy_stop_price"))
        if stop is None or stop <= 0 or stop >= executable:
            rejection_codes.append("STRUCTURAL_STOP_INVALID")
        elif (executable - stop) / executable * 100 > params.max_stop_distance_pct:
            rejection_codes.append("STRUCTURAL_STOP_TOO_WIDE")
        risk = result.get("risk_guard") or {}
        risk_limit = (decision.get("risk_snapshot") or {}).get("portfolio_risk_limit")
        prior_risk = (decision.get("risk_snapshot") or {}).get(
            "portfolio_open_risk", 0
        ) + float((decision.get("risk_snapshot") or {}).get("pending_open_risk") or 0)
        if (
            risk_limit is not None
            and prior_risk
            + float(risk.get("risk_per_share") or 0)
            * int(risk.get("position_size") or 0)
            > risk_limit
        ):
            rejection_codes.append("PORTFOLIO_OPEN_RISK_LIMIT")
    result["event_age_observation"] = {
        "event_age_seconds": result.get("event_age_seconds"),
        "threshold_seconds": float(params.buy_revalidation_max_quote_age_seconds),
        "event_time": result.get("event_time"),
        "serial": result.get("event_serial"),
        "snapshot_fingerprint": result.get("market_state_snapshot_fingerprint"),
    }
    codes, reasons = _reject(rejection_codes)
    result["rejection_codes"] = codes
    result["rejection_reasons"] = reasons
    result["ok"] = not codes
    return result


@_gate_report
def revalidate_sell_before_order(
    gateway: Any,
    decision: Mapping[str, Any],
    signal_context: Mapping[str, Any],
    *,
    parameters: Optional[TradingParameters] = None,
    now: Optional[datetime] = None,
    market_state_snapshot: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """SELL 送單前讀一次最新 MarketState；不因 stale / spread 阻擋既有風控 EXIT。"""
    params = parameters or TradingParameters()
    params.validate()
    symbol = str(decision.get("symbol") or signal_context.get("symbol") or "").strip()
    market = _normalize_market(decision.get("market") or signal_context.get("market"))
    result: Dict[str, Any] = {
        "ok": False,
        "symbol": symbol,
        "market": market,
        "market_state_snapshot": {},
        "market_state_snapshot_fingerprint": "",
        "event_age_seconds": None,
        "event_time": None,
        "event_serial": None,
        "event_fresh": False,
        "freshness_basis": "FUGLE_EVENT_TIME",
        "order_price": None,
        "bid": None,
        "price": None,
        "rejection_codes": [],
        "rejection_reasons": [],
    }
    try:
        snapshot = (
            dict(market_state_snapshot)
            if isinstance(market_state_snapshot, Mapping)
            else _freeze_atomic_market_state_snapshot(gateway, symbol, market, now=now)
        )
    except Exception as exc:
        snapshot = {}
        result["atomic_error"] = f"{type(exc).__name__}: {exc}"
    snapshot, error = resolve_executable_sell_quote(gateway, symbol, market,
        signal_context.get("sell_fallback_quote"), snapshot=snapshot, now=now)
    if error:
        result.update(primary_rejection_code=error, rejection_codes=[error],
                      rejection_reasons=["所有 SELL 行情來源均失效，保持 EXIT_PENDING 下輪重試"],
                      severity="CRITICAL", **snapshot)
        return result
    for key in ("sell_price_source", "sell_source_timestamp", "sell_source_age_seconds", "sell_source_attempts"):
        result[key] = snapshot.get(key)
    snapshot.setdefault(
        "snapshot_fingerprint", _market_state_snapshot_fingerprint(snapshot)
    )
    fields = _atomic_snapshot_execution_fields(snapshot)
    freshness = _event_freshness_from_snapshot(
        snapshot,
        now=now,
        max_age_seconds=params.buy_revalidation_max_quote_age_seconds,
    )
    price = _to_float(fields.get("price"))
    bid = _to_float(fields.get("bid"))
    basis = bid if bid is not None and bid > 0 else price
    rejection_codes: List[str] = []
    if basis is None or basis <= 0:
        rejection_codes.append("SELL_REVALIDATION_LAST_INVALID")
        order_price = None
    else:
        order_price = _price_to_tick(float(basis), side="SELL", symbol=symbol)

    _record_event_age_sample("SELL_REVALIDATION", snapshot, freshness)
    result.update(
        {
            "market_state_snapshot": dict(snapshot),
            "market_state_snapshot_fingerprint": str(
                fields.get("snapshot_fingerprint") or ""
            ),
            "event_age_seconds": freshness.get("event_age_seconds"),
            "event_time": freshness.get("event_time"),
            "event_serial": freshness.get("event_serial"),
            "event_fresh": bool(freshness.get("event_fresh")),
            "freshness_basis": freshness.get("freshness_basis"),
            "freshness_threshold_seconds": freshness.get("freshness_threshold_seconds"),
            "quote_sequence": freshness.get("quote_sequence"),
            "symbol_quote_sequence": freshness.get("symbol_quote_sequence"),
            "order_price": _round(order_price, 4),
            "bid": _round(bid, 6),
            "price": _round(price, 6),
            "event_stale": bool(freshness.get("event_stale")),
        }
    )
    codes, reasons = _reject(rejection_codes)
    result["rejection_codes"] = codes
    result["rejection_reasons"] = reasons
    result["ok"] = not codes
    return result


STRATEGY_METADATA_FIELDS = (
    "entry_index_change_pct",
    "strategy_type",
    "strategy_state",
    "entry_setup_type",
    "entry_wave_id",
    "strategy_stop_price",
    "emergency_stop_price",
    "effective_stop_price",
    "entry_structure_reference",
    "sector",
    "exit_fraction",
    "exit_stage",
    "initial_quantity",
    "partial_exit_done",
    "partial_exit_stage",
    "prev_close",
)


def portfolio_risk_snapshot(account_state, params):
    positions = _normalize_positions(account_state.get("positions"))
    equity = _to_float(account_state.get("equity")) or 0
    risk = 0.0
    sectors = {}
    unknown = False
    for row in positions:
        qty = max(0, _to_int(row.get("managed_quantity")))
        if not qty:
            continue
        basis = _to_float(row.get("entry_price"))
        stop = _to_float(row.get("effective_stop_price"))
        if basis is None or stop is None:
            unknown = True
            continue
        risk += qty * max(0, basis - stop)
        sector = str(row.get("sector") or "UNKNOWN")
        sectors[sector] = sectors.get(sector, 0) + 1
    return {
        "gap_stress_risk": sum(max(0, _to_int(p.get("managed_quantity"))) * max(0,
            float(p.get("entry_price") or 0) - float(p.get("effective_stop_price") or 0)
            + float(p.get("entry_price") or 0) * params.gap_stress_pct / 100) for p in positions),
        "strategy_daily_pnl": dict(account_state.get("strategy_daily_pnl") or {}),
        "portfolio_open_risk": risk,
        "pending_open_risk": float(account_state.get("pending_open_risk") or 0),
        "portfolio_risk_limit": equity * params.max_portfolio_open_risk_pct / 100,
        "remaining_open_risk": max(0, equity * params.max_portfolio_open_risk_pct / 100 - risk - float(account_state.get("pending_open_risk") or 0)),
        "daily_realized_pnl": account_state.get("daily_pnl"),
        "daily_loss_limit": equity * params.max_daily_loss_pct / 100,
        "position_count": len([p for p in positions if _to_int(p.get("quantity")) > 0]),
        "position_limit": params.max_position_count,
        "daily_entry_count": account_state.get("daily_trade_count", 0),
        "daily_entry_limit": params.max_daily_trade_count,
        "consecutive_losses": account_state.get("consecutive_losses", 0),
        "loss_cooldown": account_state.get("loss_cooldown_until"),
        "sector_exposure": sectors,
        "risk_unknown": unknown,
        "risk_gate": (
            "PORTFOLIO_RISK_UNKNOWN"
            if unknown
            else (
                "PORTFOLIO_RISK_LOCK"
                if risk >= equity * params.max_portfolio_open_risk_pct / 100
                else "READY"
            )
        ),
    }


def _strategy_risk_codes(signal, account, price, quantity, params, *, now=None):
    codes = []
    strategy = signal.get("strategy_type")
    equity = float(account.get("equity") or 0)
    weight = float(params.strategy_risk_weights.get(strategy, 1.0))
    budget = float(params.strategy_daily_loss_budget_pct.get(strategy, 0.0))
    pnl = float((account.get("strategy_daily_pnl") or {}).get(strategy, 0))
    if budget > 0 and pnl <= -equity * budget / 100:
        codes.append("STRATEGY_DAILY_RISK_BUDGET")
    held = (_normalize_positions(account.get("positions"))
            + _normalize_positions(account.get("pending_risk_positions")))
    candidate_sector = str((signal.get("candidate") or {}).get("sector") or "UNKNOWN")
    stress = sum(max(0, _to_int(p.get("managed_quantity"))) * max(0,
                 float(p.get("entry_price") or 0) - float(p.get("effective_stop_price") or 0)
                 + float(p.get("entry_price") or 0) * params.gap_stress_pct / 100) for p in held)
    new_stress = quantity * max(0, price - float(signal.get("strategy_stop_price") or 0)
                               + price * params.gap_stress_pct / 100)
    if params.max_stress_risk_pct > 0 and stress + new_stress > equity * params.max_stress_risk_pct / 100:
        codes.append("GAP_STRESS_RISK_LIMIT")
    if params.max_correlated_risk_pct > 0:
        correlated = 0.0
        for p in held:
            sector = str(p.get("sector") or "UNKNOWN")
            pair = "|".join(sorted((candidate_sector, sector)))
            coefficient = 1.0 if sector == candidate_sector else params.sector_correlations.get(pair)
            if coefficient is None:
                codes.append("CORRELATION_DATA_MISSING")
                continue
            correlated += float(coefficient) * float(p.get("open_risk") or 0)
        if correlated + quantity * max(0, price-float(signal.get("strategy_stop_price") or 0)) > equity * params.max_correlated_risk_pct / 100:
            codes.append("CORRELATED_EXPOSURE_LIMIT")
    context = signal.get("market_context") or {}
    if strategy == "TREND" and not load_market_module().strategy_market_permission(context, now=now or _parse_iso(context.get("permission_evaluated_at")) or datetime(1970, 1, 1, tzinfo=timezone.utc))["allowed"]:
        codes.append("TREND_MARKET_INVALID")
    if strategy not in {"MOMENTUM", "TREND"}:
        codes.append("STRATEGY_IDENTITY_MISSING")
    if context.get("market_state") == "PANIC":
        codes.append("MARKET_BLOCKED")
    if (
        context.get(
            "trend_long_allowed" if strategy == "TREND" else "momentum_long_allowed"
        )
        is not True
    ):
        codes.append(
            "TREND_MARKET_INVALID" if strategy == "TREND" else "MOMENTUM_MARKET_INVALID"
        )
    candidate = signal.get("candidate") or {}
    if candidate.get("trade_ready") is not True:
        codes.append("CANDIDATE_STALE_OR_TIME_UNKNOWN")
    if (
        candidate.get("trend_eligible" if strategy == "TREND" else "momentum_eligible")
        is not True
    ):
        codes.extend(candidate.get("trend_eligibility_reasons" if strategy == "TREND" else "momentum_eligibility_reasons") or ["CANDIDATE_STRATEGY_INELIGIBLE"])
    stop = _to_float(signal.get("strategy_stop_price"))
    if stop is None or stop <= 0 or stop >= price:
        codes.append("STRUCTURAL_STOP_INVALID")
    elif (price - stop) / price * 100 > params.max_stop_distance_pct:
        codes.append("STRUCTURAL_STOP_TOO_WIDE")
    risk = portfolio_risk_snapshot(account, params)
    new_risk = quantity * max(0, price - (stop or price))
    if new_risk > equity * params.max_risk_per_trade_pct / 100 * weight:
        codes.append("STRATEGY_WEIGHTED_RISK_LIMIT")
    if risk["risk_unknown"]:
        codes.append("PORTFOLIO_RISK_UNKNOWN")
    if (
        risk["portfolio_open_risk"]
        + new_risk
        + float(account.get("pending_open_risk") or 0)
        > risk["portfolio_risk_limit"]
    ):
        codes.append("PORTFOLIO_OPEN_RISK_LIMIT")
    sector = str(candidate.get("sector") or "UNKNOWN")
    if (
        risk["sector_exposure"].get(sector, 0)
        + int((account.get("pending_sector_counts") or {}).get(sector, 0))
        >= params.max_sector_position_count
    ):
        codes.append("SECTOR_RISK_LIMIT")
    symbol = str(signal.get("symbol") or "")
    if (
        int((account.get("symbol_entry_counts") or {}).get(symbol, 0))
        >= params.max_entries_per_symbol_per_day
    ):
        codes.append("SYMBOL_ENTRY_LIMIT")
    now = _parse_iso(signal.get("entry_time"))
    last = _parse_iso((account.get("symbol_last_exit") or {}).get(symbol))
    cooldown = _parse_iso(account.get("loss_cooldown_until"))
    if (
        now
        and last
        and (now - last).total_seconds() < params.reentry_cooldown_minutes * 60
    ):
        codes.append("REENTRY_COOLDOWN")
    if now and cooldown and now < cooldown:
        codes.append("CONSECUTIVE_LOSS_COOLDOWN")
    return codes


def entry_risk_budget(account, signal, params):
    risk = portfolio_risk_snapshot(account, params)
    if risk["risk_unknown"]:
        return 0.0
    equity = float(account.get("equity") or 0)
    weight = float(params.strategy_risk_weights.get(signal.get("strategy_type"), 1.0))
    return max(0.0, min(equity * params.max_risk_per_trade_pct / 100 * weight,
        risk["portfolio_risk_limit"] - risk["portfolio_open_risk"] - risk["pending_open_risk"]))


def _size_entry(
    order_price: float,
    available_cash: float,
    equity: float,
    params: TradingParameters,
    effective_stop_price: Optional[float] = None,
    remaining_risk_budget: Optional[float] = None,
) -> Dict[str, Any]:
    cash_budget = available_cash * params.cash_utilization_pct / 100.0
    equity_budget = equity * params.max_single_position_pct / 100.0
    budget_candidates = [cash_budget, equity_budget]
    if params.max_single_position_amount > 0:
        budget_candidates.append(params.max_single_position_amount)
    capital_budget = max(0.0, min(budget_candidates))

    capital_shares = int(capital_budget // order_price) if order_price > 0 else 0
    risk_budget = max(0.0, equity * params.max_risk_per_trade_pct / 100.0)
    if remaining_risk_budget is not None:
        risk_budget = min(risk_budget, max(0.0, remaining_risk_budget))
    risk_per_share = max(0.0, order_price - float(effective_stop_price or order_price))
    risk_shares = int(risk_budget // risk_per_share) if risk_per_share > 0 else 0
    shares = min(capital_shares, risk_shares)

    if params.max_position_shares > 0:
        shares = min(shares, params.max_position_shares)

    if params.allow_odd_lot_entry:
        if shares >= params.lot_size:
            shares = (shares // params.lot_size) * params.lot_size
            trade_type = 0
            cts_quantity = shares // params.lot_size
        else:
            trade_type = 7
            cts_quantity = shares
    else:
        shares = (shares // params.lot_size) * params.lot_size
        trade_type = 0
        cts_quantity = shares // params.lot_size

    return {
        "capital_budget": _round(capital_budget, 2),
        "risk_budget": _round(risk_budget, 2),
        "risk_per_share": _round(risk_per_share, 6),
        "capital_shares": capital_shares,
        "risk_shares": risk_shares,
        "position_size": max(0, int(shares)),
        "cts_trade_type": int(trade_type),
        "cts_quantity": max(0, int(cts_quantity)),
        "estimated_position_cost": _round(max(0, shares) * order_price, 2),
    }


def _exit_quantity_plan(
    position: Mapping[str, Any], params: TradingParameters, exit_fraction: float = 1.0, max_shares=None
) -> Dict[str, Any]:
    if position.get("managed_by") != "MOMENTUM_SCALPING":
        return {"ok": False, "reason": "EXTERNAL_POSITION_PROTECTED"}
    shares = min(
        max(0, _to_int(position.get("quantity"))),
        max(0, _to_int(position.get("managed_quantity"))),
        max(0, _to_int(position.get("managed_sellable_quantity"))),
    )
    if shares == 0:
        return {"ok": False, "reason": "NO_POSITION_TO_EXIT"}
    if not math.isfinite(exit_fraction) or not 0 < exit_fraction <= 1:
        return {"ok": False, "reason": "INVALID_EXIT_FRACTION"}
    if exit_fraction < 1:
        requested = max(1, int(shares * exit_fraction))
        if shares >= params.lot_size:
            shares = min(
                shares,
                max(params.lot_size, (requested // params.lot_size) * params.lot_size),
            )
        else:
            shares = requested
    if max_shares is not None:
        shares = min(shares, max(0, int(max_shares)))
    shares = legal_board_lot(shares, params.lot_size)
    if shares <= 0:
        return {"ok": False, "reason": "BOARD_LOT_REMAINDER_PROTECTED"}
    return {"ok": True, "position_size": shares, "cts_trade_type": 0,
            "cts_quantity": shares // params.lot_size}


_CUSTOM_FULL_EXIT_REASONS = frozenset({
    "MOMENTUM_CUSTOM_TAKE_PROFIT", "TREND_CUSTOM_TAKE_PROFIT",
})


def _normalize_custom_full_exit(signal_context):
    """只正規化 Intraday 已確認的自訂停利訊號，不重算報酬率或產生訊號。"""
    reason = signal_context.get("exit_stage") or signal_context.get("exit_reason")
    if signal_context.get("exit_signal") and reason in _CUSTOM_FULL_EXIT_REASONS:
        return dict(signal_context, exit_stage=reason, exit_reason=reason, exit_fraction=1.0)
    return signal_context


@_gate_report
def evaluate_trading_decision(
    signal_context: Mapping[str, Any],
    current_quote: Mapping[str, Any],
    account_state: Mapping[str, Any],
    *,
    parameters: Optional[TradingParameters] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """純函式：不連網、不下單、不讀系統時間。"""
    signal_context = _normalize_custom_full_exit(signal_context)
    params = parameters or TradingParameters()
    params.validate()

    symbol, market = _derive_symbol_market(signal_context, current_quote)
    positions = _normalize_positions(account_state.get("positions"))
    position = _find_position(positions, symbol, market)

    entry_signal = bool(signal_context.get("entry_signal"))
    exit_signal = bool(signal_context.get("exit_signal"))
    action = "SELL" if exit_signal else "BUY" if entry_signal else "NONE"

    missing_account = _account_state_missing(account_state)
    rejection_codes: List[str] = []
    if params.require_complete_account_state and missing_account and action == "BUY":
        rejection_codes.append("ACCOUNT_STATE_INCOMPLETE")

    available_cash = _to_float(account_state.get("available_cash"))
    equity = _to_float(account_state.get("equity"))
    daily_pnl = _to_float(account_state.get("daily_pnl"))
    daily_trade_count = _to_int(account_state.get("daily_trade_count"))
    if equity is None or equity <= 0:
        equity = available_cash

    current_price = _to_float(current_quote.get("price"))
    bid = _to_float(current_quote.get("bid"))
    ask = _to_float(current_quote.get("ask"))
    signal_price = _to_float(signal_context.get("signal_price"))

    base: Dict[str, Any] = {
        "decision": "REJECT",
        "action": action,
        "rejection_codes": [],
        "rejection_reasons": [],
        "symbol": symbol,
        "market": market,
        "signal_price": _round(signal_price, 6),
        "current_price": _round(current_price, 6),
        "bid": _round(bid, 6),
        "ask": _round(ask, 6),
        "spread_pct": None,
        "slippage_pct": None,
        "chase_pct": None,
        "execution_friction_pct": None,
        "order_price": None,
        "position_size": 0,
        "cts_trade_type": None,
        "cts_quantity": 0,
        "estimated_position_cost": 0.0,
        "existing_position": position,
        "account_summary": {
            "remaining_entry_risk_budget": entry_risk_budget(account_state, signal_context, params),
            "available_cash": _round(available_cash, 2),
            "equity": _round(equity, 2),
            "daily_trade_count": daily_trade_count,
            "daily_pnl": _round(daily_pnl, 2),
            "position_count": sum(
                1 for row in positions if _to_int(row.get("quantity")) > 0
            ),
            "pending_position_count": _to_int(
                account_state.get("pending_position_count")
            ),
            "max_position_count": params.max_position_count,
            "max_daily_trade_count": params.max_daily_trade_count,
            "missing_fields": missing_account,
        },
        "parameters": params.snapshot(),
        "decision_fingerprint": "",
    }

    if action == "NONE":
        rejection_codes.append("NO_ACTIONABLE_SIGNAL")
    elif action == "SELL":
        if position is None:
            rejection_codes.append("NO_POSITION_TO_EXIT")
        if current_price is None or current_price <= 0:
            rejection_codes.append("INVALID_CURRENT_PRICE")
        if not rejection_codes:
            plan = _exit_quantity_plan(
                position or {},
                params,
                float(signal_context.get("exit_fraction") or 1.0),
                signal_context.get("retry_exit_max_shares"),
            )
            if not plan.get("ok"):
                rejection_codes.append(str(plan.get("reason")))
            else:
                raw_sell_price = bid if bid is not None and bid > 0 else current_price
                order_price = _price_to_tick(
                    float(raw_sell_price), side="SELL", symbol=symbol
                )
                base.update(
                    {
                        "order_price": _round(order_price, 4),
                        "position_size": int(plan["position_size"]),
                        "cts_trade_type": int(plan["cts_trade_type"]),
                        "cts_quantity": int(plan["cts_quantity"]),
                        "estimated_position_cost": _round(
                            order_price * int(plan["position_size"]), 2
                        ),
                    }
                )
    else:
        # BUY 最終保護：即使 Intraday 已判斷過，Trading 再檢查 market_context。
        market_context = signal_context.get("market_context")
        if (
            isinstance(market_context, Mapping)
            and market_context.get("allow_long") is False
        ):
            rejection_codes.append("MARKET_BLOCKED")
        if signal_price is None or signal_price <= 0:
            rejection_codes.append("INVALID_SIGNAL_PRICE")
        if current_price is None or current_price <= 0:
            rejection_codes.append("INVALID_CURRENT_PRICE")

        eligibility = _candidate_day_trade_eligibility(signal_context)
        if eligibility is False:
            rejection_codes.append("DAY_TRADE_INELIGIBLE")
        elif eligibility is None and params.require_day_trade_eligible:
            rejection_codes.append("DAY_TRADE_ELIGIBILITY_UNKNOWN")

        if position is not None:
            rejection_codes.append("EXISTING_POSITION")
        if _open_order_exists(account_state, symbol, market):
            rejection_codes.append("DUPLICATE_OPEN_ORDER")
        fingerprint = str(signal_context.get("signal_fingerprint") or "").strip()
        processed = {
            str(item)
            for item in (account_state.get("processed_signal_fingerprints") or [])
            if str(item)
        }
        # BUY 不以歷史 fingerprint 當永久鎖；持倉、未結委託與送單鎖各自檢查。

        position_count = sum(
            1 for row in positions if _to_int(row.get("quantity")) > 0
        ) + _to_int(account_state.get("pending_position_count"))
        if position_count >= params.max_position_count:
            rejection_codes.append("MAX_POSITION_COUNT_REACHED")
        if daily_trade_count >= params.max_daily_trade_count:
            rejection_codes.append("MAX_DAILY_TRADE_COUNT_REACHED")

        if daily_pnl is not None and equity is not None and equity > 0:
            loss_pct_amount = equity * params.max_daily_loss_pct / 100.0
            if daily_pnl <= -loss_pct_amount:
                rejection_codes.append("DAILY_LOSS_LIMIT_REACHED")
            if (
                params.max_daily_loss_amount > 0
                and daily_pnl <= -params.max_daily_loss_amount
            ):
                rejection_codes.append("DAILY_LOSS_LIMIT_REACHED")

        if (
            available_cash is None
            or available_cash <= 0
            or equity is None
            or equity <= 0
        ):
            rejection_codes.append("INSUFFICIENT_CASH")

        if (
            signal_price is not None
            and signal_price > 0
            and current_price is not None
            and current_price > 0
        ):
            metrics = _execution_metrics(signal_price, current_price, bid, ask, symbol)
            base.update(metrics)
            spread_pct = _to_float(metrics.get("spread_pct"))
            slippage_pct = _to_float(metrics.get("slippage_pct")) or 0.0
            chase_pct = _to_float(metrics.get("chase_pct")) or 0.0
            friction_pct = _to_float(metrics.get("execution_friction_pct")) or 0.0
            order_price = _to_float(metrics.get("order_price")) or current_price

            if params.require_bid_ask and (
                bid is None or ask is None or bid <= 0 or ask <= 0
            ):
                rejection_codes.append("MISSING_BID_ASK")
            elif (
                bid is not None
                and ask is not None
                and (bid <= 0 or ask <= 0 or ask < bid)
            ):
                rejection_codes.append("INVALID_BID_ASK")
            if spread_pct is not None and spread_pct > params.max_spread_pct:
                rejection_codes.append("SPREAD_TOO_WIDE")
            if slippage_pct > params.max_slippage_pct:
                rejection_codes.append("SLIPPAGE_TOO_HIGH")
            if chase_pct > params.max_chase_pct:
                rejection_codes.append("CHASE_TOO_HIGH")
            if friction_pct > params.max_execution_friction_pct:
                rejection_codes.append("EXECUTION_FRICTION_TOO_HIGH")
            if order_price > params.max_buy_price:
                rejection_codes.append("MAX_BUY_PRICE_EXCEEDED")

            if (
                available_cash is not None
                and available_cash > 0
                and equity is not None
                and equity > 0
            ):
                sizing = _size_entry(
                    order_price,
                    available_cash,
                    equity,
                    params,
                    _to_float(signal_context.get("strategy_stop_price")),
                    entry_risk_budget(account_state, signal_context, params),
                )
                base.update(sizing)
                if sizing["position_size"] <= 0 or sizing["cts_quantity"] <= 0:
                    rejection_codes.append("POSITION_SIZE_ZERO")
                if float(sizing["estimated_position_cost"] or 0) > available_cash:
                    rejection_codes.append("INSUFFICIENT_CASH")

    for key in STRATEGY_METADATA_FIELDS:
        base[key] = signal_context.get(key)
    base["sector"] = (signal_context.get("candidate") or {}).get("sector") or "UNKNOWN"
    base["prev_close"] = (signal_context.get("candidate") or {}).get("prev_close")
    if action == "BUY":
        index = (signal_context.get("market_context") or {}).get(
            "tpex" if base.get("market") == "TPEX" else "taiex") or {}
        base["entry_index_change_pct"] = _to_float(index.get("change_pct"))
    if action == "BUY" and current_price and current_price > 0:
        price = float(base.get("order_price") or current_price)
        base["day_change_gate"] = _day_change_gate(current_quote, price, params)
        if not base["day_change_gate"]["ok"]:
            rejection_codes.append(base["day_change_gate"]["rejection_code"])
        rejection_codes.extend(
            _strategy_risk_codes(
                signal_context, account_state, price, int(base["position_size"]), params,
                now=now or _parse_iso((signal_context.get("market_context") or {}).get("permission_evaluated_at")) or _parse_iso(current_quote.get("timestamp")),
            )
        )
        base["emergency_stop_price"] = price * (
            1 - params.emergency_stop_loss_pct / 100
        )
        base["effective_stop_price"] = max(
            float(base.get("strategy_stop_price") or 0), base["emergency_stop_price"]
        )
    base["risk_snapshot"] = portfolio_risk_snapshot(account_state, params)
    codes, reasons = _reject(rejection_codes)
    base["rejection_codes"] = codes
    base["rejection_reasons"] = reasons
    base["decision"] = "ALLOW" if not codes and action in {"BUY", "SELL"} else "REJECT"

    canonical_input = {
        "signal": {
            "entry_signal": bool(signal_context.get("entry_signal")),
            "exit_signal": bool(signal_context.get("exit_signal")),
            "entry_time": signal_context.get("entry_time"),
            "signal_price": signal_context.get("signal_price"),
            "signal_fingerprint": signal_context.get("signal_fingerprint"),
            "symbol": symbol,
            "market": market,
            "market_context": signal_context.get("market_context"),
            "candidate": signal_context.get("candidate"),
        },
        "quote": {
            "symbol": current_quote.get("symbol"),
            "market": current_quote.get("market"),
            "trade_date": current_quote.get("trade_date"),
            "timestamp": current_quote.get("timestamp"),
            "event_time": current_quote.get("event_time"),
            "serial": current_quote.get("serial"),
            "quote_sequence": current_quote.get("quote_sequence"),
            "symbol_quote_sequence": current_quote.get("symbol_quote_sequence"),
            "price": current_quote.get("price"),
            "bid": current_quote.get("bid"),
            "ask": current_quote.get("ask"),
        },
        "account": _canonical_account_state(account_state),
        "parameters": params.snapshot(),
    }
    base["decision_fingerprint"] = _stable_hash(canonical_input)
    return base


# =============================================================================
# 模組動態連結
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


def api_gateway_candidates() -> List[str]:
    return _module_candidates("api_gateway", "TRADING_API_GATEWAY_MODULE")


def market_candidates() -> List[str]:
    return _module_candidates("market", "TRADING_MARKET_MODULE")


def scanner_candidates() -> List[str]:
    return _module_candidates("scanner", "TRADING_SCANNER_MODULE")


def intraday_candidates() -> List[str]:
    return _module_candidates("intraday", "TRADING_INTRADAY_MODULE")


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
        api_gateway_candidates(),
        ("APIGateway", "CTSOrderRequest"),
    )


@lru_cache(maxsize=1)
def load_market_module() -> Any:
    return _load_module_with_required(
        "market", market_candidates(), ("get_market_context",)
    )


def load_scanner_module() -> Any:
    return _load_module_with_required(
        "scanner", scanner_candidates(), ("get_candidates",)
    )


def load_intraday_module() -> Any:
    return _load_module_with_required(
        "intraday", intraday_candidates(), ("get_signal_context",)
    )


def create_api_gateway(
    *, api_mode: str = "LIVE", replay_dir: str = "api_replay"
) -> Any:
    module = load_api_gateway_module()
    return module.APIGateway(mode=str(api_mode).strip().upper(), replay_dir=replay_dir)


def _api_module_for_gateway(gateway: Any) -> Any:
    module_name = getattr(gateway.__class__, "__module__", "")
    if module_name:
        try:
            module = importlib.import_module(module_name)
            if hasattr(module, "CTSOrderRequest"):
                return module
        except Exception:
            pass
    return load_api_gateway_module()


# =============================================================================
# CTS 委託與成交確認
# =============================================================================


def validate_sell_inventory(gateway, *, account_id, trade_date, symbol, requested_shares, parameters):
    """送單前強制查券商庫存及未結委託；不使用 stale 快照授權賣出。"""
    try:
        orders = gateway.cts.query_order(account_id, force_query=True)
        response = gateway.cts.query_position(account_id, int(trade_date), force_query=True)
        if not response.get("ok") or not orders.get("ok"):
            return {"ok": False, "status": "SELL_ACCOUNT_QUERY_REQUIRED"}
        rows = parse_cts_positions(_parsed_result_rows(response))
        held = sum(int(row["quantity"]) for row in rows if row["symbol"] == symbol)
        # 包含人工在其他平台送出的單；未解身分或未確認結束即不再賣。
        for row in _parsed_result_rows(orders):
            if str(row.get("F5") or "") == symbol and _to_int(row.get("F4")) == 2:
                if any(_to_float(row.get(field)) is None for field in ("F8", "F10", "F11", "F18")):
                    return {"ok": False, "status": "SELL_ORDER_PENDING"}
                state = parse_cts_order_state([row])
                if not state.get("found") or state.get("active"):
                    return {"ok": False, "status": "SELL_ORDER_PENDING"}
        shares = legal_board_lot(min(held, max(0, int(requested_shares))), parameters.lot_size)
        return {"ok": shares > 0, "status": "OK" if shares > 0 else "NO_SELLABLE_BOARD_LOT", "shares": shares}
    except Exception as exc:
        return {"ok": False, "status": "SELL_ACCOUNT_QUERY_REQUIRED", "error": str(exc)}


def evaluate_manual_decision(symbol, market, action, quote, account, *, parameters):
    """人工緊急處置：僅核對數量及資金，不評估策略或進場資格。"""
    params = parameters
    positions = _normalize_positions(account.get("positions"))
    position = _find_position(positions, symbol, market)
    reason = ""
    price = _to_float(quote.get("ask")) or _quote_last_price(quote) or 0.0
    shares = 0
    if action == "SELL":
        shares = legal_board_lot(int((position or {}).get("quantity") or 0), params.lot_size)
        if shares <= 0:
            reason = "NO_SELLABLE_BOARD_LOT"
    elif action == "BUY":
        if position and int(position.get("quantity") or 0) > 0:
            reason = "EXISTING_POSITION"
        cash = max(0.0, _to_float(account.get("available_cash")) or 0.0)
        equity = _to_float(account.get("equity")) or cash
        limits = [cash * params.cash_utilization_pct / 100, equity * params.max_single_position_pct / 100]
        if params.max_single_position_amount > 0:
            limits.append(params.max_single_position_amount)
        if price > 0:
            shares = int(min(limits) // price)
            if params.max_position_shares > 0:
                shares = min(shares, params.max_position_shares)
            shares = legal_board_lot(shares, params.lot_size)
        if shares <= 0:
            reason = reason or "MANUAL_BUY_BUDGET_OR_PRICE_INVALID"
    else:
        reason = "INVALID_ORDER_SIDE"
    return {"decision": "REJECT" if reason else "ALLOW", "action": action,
            "symbol": symbol, "market": market, "position_size": shares,
            "cts_quantity": shares // params.lot_size, "cts_trade_type": 0,
            "order_price": price, "estimated_position_cost": shares * price,
            "existing_position": position, "rejection_codes": [reason] if reason else [],
            "strategy_type": "MANUAL"}


def _create_cts_order_request(
    gateway: Any,
    decision: Mapping[str, Any],
    signal_context: Mapping[str, Any],
    current_quote: Mapping[str, Any],
    *,
    account_id: str,
    parameters: TradingParameters,
) -> Any:
    quantity = _to_float(decision.get("cts_quantity"))
    price = _to_float(decision.get("order_price"))
    if quantity is None or quantity <= 0 or not quantity.is_integer():
        raise ValueError("INVALID_ORDER_QUANTITY")
    if decision.get("action") == "BUY" and (price is None or price <= 0):
        raise ValueError("INVALID_ORDER_PRICE")
    if decision.get("action") not in {"BUY", "SELL"}:
        raise ValueError("INVALID_ORDER_SIDE")
    api_module = _api_module_for_gateway(gateway)
    request_class = getattr(api_module, "CTSOrderRequest")
    side = 1 if decision.get("action") == "BUY" else 2
    return request_class(
        account_id=str(account_id),
        trade_date=_trade_date_int(signal_context, current_quote),
        trade_type=int(decision["cts_trade_type"]),
        order_type=0 if signal_context.get("manual_emergency") else int(parameters.cts_order_type),
        side=side,
        symbol=str(decision["symbol"]),
        quantity=int(decision["cts_quantity"]),
        price_type=4,
        price="0",
        broker="",
        pay_type=0,
        condition=int(parameters.cts_condition),
    )


def _parsed_result_rows(response: Any) -> List[Mapping[str, Any]]:
    rows = response.get("parsed_result") if isinstance(response, Mapping) else None
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def _match_result_rows(response: Any) -> List[Mapping[str, Any]]:
    """QueryMatch 優先採 api_gateway 的 normalized_result。"""
    if isinstance(response, Mapping):
        normalized = response.get("normalized_result")
        if isinstance(normalized, Sequence) and not isinstance(
            normalized, (str, bytes)
        ):
            rows = [row for row in normalized if isinstance(row, Mapping)]
            if rows:
                return rows
    return _parsed_result_rows(response)


def query_cts_order_state(
    gateway: Any,
    *,
    account_id: str,
    symbol: str,
    side: int,
    order_id: Optional[str] = None,
    order_no: Optional[str] = None,
) -> Dict[str, Any]:
    """強制查詢 CTS 委託狀態並解析指定委託。"""
    cts = getattr(gateway, "cts", None)
    query_order = getattr(cts, "query_order", None)
    if not callable(query_order):
        raise RuntimeError("APIGateway.cts 缺少 query_order()")
    response = query_order(str(account_id), force_query=True)
    if isinstance(response, Mapping) and response.get("ok") is False:
        raise RuntimeError(
            f"CTS_QUERY_ORDER_FAILED: {response.get('error') or response.get('raw')}"
        )
    state = parse_cts_order_state(
        _parsed_result_rows(response),
        symbol=str(symbol),
        side=int(side),
        order_id=(str(order_id) if order_id else None),
        order_no=(str(order_no) if order_no else None),
    )
    state["query_response"] = response
    return state


def reconcile_order_fills(
    gateway: Any,
    *,
    account_id: str,
    symbol: str,
    side: int,
    order_ids: Optional[Sequence[str]] = None,
    order_nos: Optional[Sequence[str]] = None,
    submitted_at: Optional[str] = None,
    trade_date: Optional[str] = None,
) -> Dict[str, Any]:
    """強制查詢成交並依 OID／委託書號精準彙總。

    只有在兩者皆缺失時才允許時間閘門 fallback；fallback 若同時出現多個
    不同委託身分，直接標記 ambiguous，不以 symbol + side 猜成交。
    """
    cts = getattr(gateway, "cts", None)
    query_match = getattr(cts, "query_match", None)
    if not callable(query_match):
        raise RuntimeError("APIGateway.cts 缺少 query_match()")
    response = query_match(str(account_id), force_query=True)
    if isinstance(response, Mapping) and response.get("ok") is False:
        raise RuntimeError(
            f"CTS_QUERY_MATCH_FAILED: {response.get('error') or response.get('raw')}"
        )
    rows = _match_result_rows(response)
    clean_ids = {
        str(value).strip() for value in (order_ids or []) if str(value).strip()
    }
    clean_nos = {
        str(value).strip() for value in (order_nos or []) if str(value).strip()
    }

    def identity(row: Mapping[str, Any]) -> Tuple[str, str]:
        normalized = "filled_shares" in row or "cts_quantity" in row
        return (
            str(row.get("order_id") if normalized else row.get("F14") or "").strip(),
            str(row.get("order_no") if normalized else row.get("F9") or "").strip(),
        )

    if clean_ids:
        id_rows = [row for row in rows if identity(row)[0] in clean_ids]
        if id_rows:
            rows = id_rows
            identity_status = "ORDER_ID"
        elif clean_nos:
            rows = [row for row in rows if identity(row)[1] in clean_nos]
            identity_status = "ORDER_NO_FALLBACK" if rows else "NO_MATCH"
        else:
            rows = []
            identity_status = "NO_MATCH"
    elif clean_nos:
        rows = [row for row in rows if identity(row)[1] in clean_nos]
        identity_status = "ORDER_NO"
    else:
        # 僅在沒有任何委託身分時使用嚴格 fallback。
        candidate_rows: List[Mapping[str, Any]] = []
        for row in rows:
            normalized = "filled_shares" in row or "cts_quantity" in row
            row_symbol = str(
                row.get("symbol") if normalized else row.get("F3") or ""
            ).strip()
            row_side = _to_int(row.get("side") if normalized else row.get("F4"), -1)
            row_date = str(
                row.get("match_date")
                if normalized and row.get("match_date")
                else row.get("trade_date") if normalized else row.get("F10") or ""
            )
            row_time = str(
                row.get("match_time")
                if normalized
                else row.get("F20") or row.get("F11") or ""
            )
            if row_symbol != str(symbol).strip() or row_side != int(side):
                continue
            if trade_date and _trade_identity_date(row_date) != _trade_identity_date(
                trade_date
            ):
                continue
            if submitted_at and not _match_not_before_submit(
                row_date, row_time, submitted_at
            ):
                continue
            candidate_rows.append(row)

        identities = {
            item for item in (identity(row) for row in candidate_rows) if any(item)
        }
        if len(identities) > 1:
            summary = summarize_cts_fills([], symbol=str(symbol), side=int(side))
            summary.update(
                {
                    "order_ids": [],
                    "order_nos": [],
                    "query_response": response,
                    "identity_status": "AMBIGUOUS_FALLBACK",
                    "ambiguous_match": True,
                }
            )
            return summary
        rows = candidate_rows
        identity_status = "TIME_GATED_FALLBACK" if rows else "NO_MATCH"

    summary = summarize_cts_fills(
        rows,
        symbol=str(symbol),
        side=int(side),
        trade_date=trade_date,
        submitted_at=(submitted_at if not clean_ids and not clean_nos else None),
    )
    summary["order_ids"] = sorted(clean_ids)
    summary["order_nos"] = sorted(clean_nos)
    summary["query_response"] = response
    summary["identity_status"] = identity_status
    summary["ambiguous_match"] = False
    return summary


def reconcile_order_fill(
    gateway: Any,
    *,
    account_id: str,
    symbol: str,
    side: int,
    order_id: Optional[str] = None,
    order_no: Optional[str] = None,
    submitted_at: Optional[str] = None,
    trade_date: Optional[str] = None,
) -> Dict[str, Any]:
    order_ids = [str(order_id)] if order_id else None
    order_nos = [str(order_no)] if order_no else None
    return reconcile_order_fills(
        gateway,
        account_id=account_id,
        symbol=symbol,
        side=side,
        order_ids=order_ids,
        order_nos=order_nos,
        submitted_at=submitted_at,
        trade_date=trade_date,
    )


def resolve_executable_sell_quote(gateway, symbol, market, fallback_quote=None, *, snapshot=None, now=None):
    """共用賣價來源：來源時間有效 5 秒內；所有失效時保持 EXIT_PENDING。"""
    current = now or datetime.now(timezone.utc)
    attempts = []
    cache = getattr(gateway, "_executable_sell_quotes", {})
    def accept(row, source):
        if not isinstance(row, Mapping) or str(row.get("symbol") or "") != symbol:
            return None
        if row.get("market") and _normalize_market(row["market"]) != _normalize_market(market):
            return None
        fields = _atomic_snapshot_execution_fields(row)
        if not any((_to_float(fields.get(k)) or 0) > 0 for k in ("bid", "price")):
            return None
        stamp = _event_time_datetime(_to_int(row.get("event_time"), 0))
        if stamp is None:
            value = row.get("timestamp") or row.get("realtime_source_at") or row.get("source_at")
            stamp = _parse_iso(value) if isinstance(value, str) and not value.isdigit() else _event_time_datetime(_to_int(value, 0))
        if stamp is None or stamp.tzinfo is None:
            return None
        age = (current-stamp).total_seconds()
        if not -1 <= age <= 5:
            return None
        return dict(row, sell_price_source=source, sell_source_timestamp=stamp.isoformat(), sell_source_age_seconds=age)
    getters = [("ATOMIC_MARKETSTATE", lambda: snapshot if snapshot is not None else _freeze_atomic_market_state_snapshot(gateway, symbol, market)),
               ("REST_QUOTE", lambda: gateway.get_sell_quote(symbol, market)),
               ("GATEWAY_LATEST", lambda: gateway.get_quote(symbol, market)),
               ("LAST_KNOWN_EXECUTABLE", lambda: fallback_quote or {}),
               ("LAST_KNOWN_EXECUTABLE", lambda: cache.get((symbol, market), {}))]
    for source,getter in getters:
        try:
            result = accept(getter(), source)
            if result:
                result["sell_source_attempts"] = attempts
                cache[(symbol, market)] = dict(result)
                gateway._executable_sell_quotes = cache
                return result, None
            attempts.append({"source": source, "error": "INVALID_OR_STALE"})
        except Exception as exc:
            attempts.append({"source": source, "error": type(exc).__name__+": "+str(exc)})
    return {"sell_source_attempts": attempts, "position_state": "EXIT_PENDING"}, "SELL_PRICE_UNAVAILABLE_CRITICAL"


def _create_cts_cancel_request(
    gateway: Any,
    state: Mapping[str, Any],
    *,
    account_id: str,
) -> Any:
    api_module = _api_module_for_gateway(gateway)
    request_class = getattr(api_module, "CTSCancelRequest", None)
    if request_class is None:
        raise RuntimeError("api_gateway 缺少 CTSCancelRequest")
    order_type = _to_int(state.get("order_type"), -1)
    if order_type not in {0, 1, 2}:
        raise RuntimeError("此 CTS order_type 不支援刪單")
    condition = _to_int(state.get("condition"))
    if condition not in {0, 1, 2}:
        condition = 0
    price = _to_float(state.get("price")) or 0.0
    return request_class(
        account_id=str(account_id),
        trade_date=_to_int(state.get("trade_date")),
        trade_type=_to_int(state.get("trade_type")),
        order_type=order_type,
        oid=str(state.get("order_id") or ""),
        order_no=str(state.get("order_no") or ""),
        symbol=str(state.get("symbol") or ""),
        side=_to_int(state.get("side"), 2),
        current_quantity=max(0, _to_int(state.get("current_quantity"))),
        matched_quantity=max(0, _to_int(state.get("matched_quantity"))),
        price=str(price),
        price_type=_to_int(state.get("price_type")),
        condition=condition,
        pre_order=0,
        quantity=0,
    )


def maintain_time_exit_orders(gateway, *, account_id, trade_date, parameters, protected_symbols=(), cancel_buys=True):
    """清倉時間後撤銷未成交系統買單，追蹤系統賣單；不新增替代單。"""
    events = []
    for order in get_system_order_registry():
        if (order.get("owner") != "MOMENTUM_SCALPING" or order.get("account_id") != account_id
                or str(order.get("trade_date") or "").replace("-", "") != str(trade_date)
                or order.get("order_state") in {"REQUEST_SENT", "SUBMITTING", "STATUS_UNKNOWN", "CANCEL_PENDING"}
                or order.get("symbol") in protected_symbols
                or order.get("status") in {"FILLED", "REJECTED", "CANCELED", "CANCELLED", "PRE_SUBMIT_REJECTED", "CONFIRMED_ABSENT"}
                or not (order.get("order_id") or order.get("order_no"))):
            continue
        state = query_cts_order_state(gateway, account_id=account_id, symbol=order["symbol"],
            side=order["side"], order_id=order.get("order_id"), order_no=order.get("order_no"))
        if not state.get("active"):
            continue
        if order.get("action") == "BUY" and cancel_buys:
            request = _create_cts_cancel_request(gateway, state, account_id=account_id)
            result = gateway.cts.cancel_order(request)
            events.append({"symbol": order["symbol"], "action": "CANCEL_BUY", "order_id": order.get("order_id"), "result": result})
    return events


_ENTRY_SUBMISSION_LOCK = threading.RLock()
_ACTIVE_INTENT_STATES = {"REQUEST_SENT", "SUBMITTING", "STATUS_UNKNOWN", "WORKING",
                         "PARTIAL_FILLED", "CANCEL_PENDING"}


def _order_event(gateway, stage, status, **details):
    recorder = getattr(gateway, "record_order_event", None)
    if callable(recorder):
        recorder(stage, status, details)


def _submit_order(gateway, request):
    """正式 CTS 自己記錄 COM 邊界；測試／其他轉接器仍保留可追蹤結果。"""
    cts = gateway.cts
    if getattr(cts, "submission_audit_supported", False):
        cts.record_order_event = getattr(gateway, "record_order_event", None)
        return cts.new_order(request)
    identity = {"order_intent_id": getattr(request, "order_intent_id", ""),
                "registry_key": getattr(request, "registry_key", ""),
                "symbol": request.symbol, "method": "new_order", "price": request.price,
                "quantity": request.quantity}
    _order_event(gateway, "ORDER_SUBMIT_START", "SUBMITTING", **identity)
    try:
        result = cts.new_order(request)
    except Exception as exc:
        result = {"ok": False, "broker_unknown": True, "broker_submit_attempt": True,
                  "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(result, Mapping):
        result = {"ok": False, "error": "CTS_ORDER_RESPONSE_INVALID", "raw_result": str(result)}
    result = dict(result)
    result.setdefault("broker_submit_attempt", not result.get("not_submitted", False))
    state = ("PRE_SUBMIT_REJECTED" if result.get("not_submitted") else
             "REJECTED" if result.get("broker_rejected") else
             "ACCEPTED" if not result.get("error") and result.get("ok", True)
             and (result.get("order_id") or result.get("order_no")) else "STATUS_UNKNOWN")
    _order_event(gateway, "ORDER_SUBMIT_RESULT", state, **identity, result=result)
    return result


def _trace_execution(method):
    from functools import wraps
    @wraps(method)
    def wrapped(gateway, decision, signal_context, current_quote, **kwargs):
        decision = dict(decision)
        intent = str(decision.get("order_intent_id") or uuid.uuid4().hex)
        decision["order_intent_id"] = intent
        identity = {"order_intent_id": intent, "symbol": decision.get("symbol"),
                    "signal_fingerprint": signal_context.get("signal_fingerprint"),
                    "action": decision.get("action")}
        with _ENTRY_SUBMISSION_LOCK:
            active = next((row for row in get_system_order_registry()
                if row.get("account_id") == kwargs.get("account_id")
                and row.get("symbol") == decision.get("symbol")
                and row.get("action") == decision.get("action")
                and row.get("order_state") in _ACTIVE_INTENT_STATES), None)
            if active:
                return {"executed": False, "execution_attempt": True,
                        "broker_submit_attempt": False, "broker_accepted": False,
                        "broker_rejected": False, "broker_unknown": False,
                        "status": "ORDER_INTENT_LOCKED", "primary_rejection_code": "ORDER_INTENT_LOCKED",
                        "order_intent_id": active["order_intent_id"], "ownership": active}
            _order_event(gateway, "ORDER_INTENT_CREATED", "CREATED", **identity)
            try:
                result = method(gateway, decision, signal_context, current_quote, **kwargs)
            except Exception as exc:
                record = next((row for row in get_system_order_registry()
                               if row.get("order_intent_id") == intent), None)
                unknown = bool(record and record.get("order_state") in _ACTIVE_INTENT_STATES)
                result = {"executed": unknown,
                          "status": "ORDER_STATUS_UNKNOWN" if unknown else "PRE_SUBMIT_REJECTED",
                          "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc(),
                          "ownership": record or {}, "primary_rejection_code": "EXECUTION_EXCEPTION"}
            response = result.get("order_response") or {}
            unknown = result.get("status") in {"ORDER_STATUS_UNKNOWN", "IDENTITY_MISSING"}
            attempted = bool(response.get("broker_submit_attempt", result.get("executed")))
            rejected = bool(response.get("broker_rejected"))
            accepted = bool(not rejected and not unknown and attempted
                            and (result.get("order_id") or result.get("order_no")))
            result.update(execution_attempt=True, broker_submit_attempt=attempted,
                          broker_accepted=accepted, broker_rejected=rejected,
                          broker_unknown=unknown, order_intent_id=intent)
            if not attempted and not accepted and not unknown:
                result.setdefault("pre_submit_reason", result.get("primary_rejection_code")
                                  or result.get("status") or "PRE_SUBMIT_REJECTED")
                result["submission_state"] = "PRE_SUBMIT_REJECTED"
            else:
                result["submission_state"] = "STATUS_UNKNOWN" if unknown else "REJECTED" if rejected else "ACCEPTED"
            _order_event(gateway, "EXECUTION_RESULT", result["submission_state"],
                         **identity, primary_reason=result.get("primary_rejection_code")
                         or result.get("pre_submit_reason") or result.get("status"),
                         broker_submit_attempt=attempted, broker_accepted=accepted,
                         broker_rejected=rejected, broker_unknown=unknown,
                         order_id=result.get("order_id"), order_no=result.get("order_no"))
            return result
    return wrapped


@_trace_execution
@_gate_report
def execute_allowed_decision(
    gateway: Any,
    decision: Mapping[str, Any],
    signal_context: Mapping[str, Any],
    current_quote: Mapping[str, Any],
    *,
    account_id: str,
    parameters: Optional[TradingParameters] = None,
    confirm_fill: bool = True,
) -> Dict[str, Any]:
    signal_context = _normalize_custom_full_exit(signal_context)
    params = parameters or TradingParameters()
    params.validate()
    if decision.get("decision") != "ALLOW":
        return {
            "executed": False,
            "status": "NOT_ALLOWED",
            "reason": "Trading decision 不是 ALLOW",
            "order_response": None,
            "fill_summary": None,
            "position": None,
        }
    if not str(account_id or "").strip():
        raise ValueError("實際 CTS 下單必須提供 account_id")

    cts = getattr(gateway, "cts", None)
    new_order = getattr(cts, "new_order", None)
    if not callable(new_order):
        raise RuntimeError("APIGateway.cts 缺少 new_order()")

    signal_context = dict(signal_context)
    signal_context.setdefault("sell_fallback_quote",dict(current_quote))
    execution_decision = dict(decision)
    execution_quote = dict(current_quote)
    revalidation: Optional[Dict[str, Any]] = None
    action = str(decision.get("action") or "").upper()
    manual = bool(signal_context.get("manual_emergency")) and _execution_trigger(signal_context) == "MANUAL"
    if action == "BUY" and manual:
        revalidation = _buy_execution_data_gate(gateway, execution_decision, params)
        if not revalidation["ok"]:
            return {"executed": False, "status": "BUY_REVALIDATION_REJECTED",
                    "rejection_codes": revalidation["rejection_codes"], "revalidation": revalidation}
    if action == "BUY" and not manual:
        status = cts.status() if callable(getattr(cts,"status",None)) else {}
        if status.get("connection_health", "READY") != "READY":
            return {"executed":False,"status":"BUY_REVALIDATION_REJECTED","rejection_codes":["CTS_HEALTH_NOT_READY"]}
        revalidation = revalidate_buy_before_order(
            gateway,
            decision,
            signal_context,
            parameters=params,
        )
        if not revalidation.get("ok"):
            return {
                "executed": False,
                "status": "BUY_REVALIDATION_REJECTED",
                "reason": "BUY 送單前 MarketState Revalidation Gate 未通過",
                "rejection_codes": list(revalidation.get("rejection_codes") or []),
                "rejection_reasons": list(revalidation.get("rejection_reasons") or []),
                "revalidation": revalidation,
                "order_response": None,
                "fill_summary": None,
                "position": None,
            }
        _order_event(gateway, "BUY_REVALIDATION_OK", "OK",
                     order_intent_id=decision.get("order_intent_id"), symbol=decision.get("symbol"),
                     order_price=revalidation.get("order_price"))
        execution_quote = dict(revalidation.get("market_state_snapshot") or {})
        for field_name in (
            "spread_pct",
            "slippage_pct",
            "chase_pct",
            "execution_friction_pct",
            "order_price",
        ):
            execution_decision[field_name] = revalidation.get(field_name)
        execution_decision["current_price"] = _quote_last_price(execution_quote)
        execution_decision["bid"] = _to_float(execution_quote.get("bid"))
        execution_decision["ask"] = _to_float(execution_quote.get("ask"))
        risk_guard = revalidation.get("risk_guard")
        if isinstance(risk_guard, Mapping) and risk_guard:
            for field_name in (
                "position_size",
                "cts_trade_type",
                "cts_quantity",
                "estimated_position_cost",
            ):
                if field_name in risk_guard:
                    execution_decision[field_name] = risk_guard.get(field_name)
        elif execution_decision.get("position_size"):
            execution_decision["estimated_position_cost"] = _round(
                float(execution_decision["position_size"])
                * float(execution_decision["order_price"]),
                2,
            )
    elif action == "SELL" and not manual:
        revalidation = revalidate_sell_before_order(
            gateway,
            decision,
            signal_context,
            parameters=params,
        )
        if not revalidation.get("ok"):
            return {
                "executed": False,
                "status": "SELL_REVALIDATION_REJECTED",
                "reason": "SELL 送單前 MarketState Revalidation 未取得有效退出價格",
                "rejection_codes": list(revalidation.get("rejection_codes") or []),
                "rejection_reasons": list(revalidation.get("rejection_reasons") or []),
                "revalidation": revalidation,
                "order_response": None,
                "fill_summary": None,
                "position": None,
            }
        execution_quote = dict(revalidation.get("market_state_snapshot") or {})
        execution_decision["current_price"] = _quote_last_price(execution_quote)
        execution_decision["bid"] = _to_float(execution_quote.get("bid"))
        execution_decision["ask"] = _to_float(execution_quote.get("ask"))
        execution_decision["order_price"] = revalidation.get("order_price")

    if action == "SELL":
        check = validate_sell_inventory(gateway, account_id=account_id,
            trade_date=_trade_date_int(signal_context, execution_quote),
            symbol=str(decision.get("symbol") or ""),
            requested_shares=int(execution_decision.get("position_size") or 0), parameters=params)
        if not check["ok"]:
            return {"executed": False, "status": check["status"], "inventory_check": check}
        execution_decision.update(position_size=check["shares"], cts_quantity=check["shares"] // params.lot_size)
        decision = dict(decision, position_size=check["shares"], cts_quantity=check["shares"] // params.lot_size)

    request = _create_cts_order_request(
        gateway,
        execution_decision,
        signal_context,
        execution_quote,
        account_id=account_id,
        parameters=params,
    )
    submitted_at = datetime.now(timezone(timedelta(hours=8))).isoformat(
        timespec="milliseconds"
    )
    pending = _register_system_pending_order(
        execution_decision,
        signal_context,
        request,
        submitted_at=submitted_at,
        lot_size=params.lot_size,
    )
    registry_key = str(pending.get("registry_key") or "")
    request = replace(request, **{name: pending[name] for name in
                      ("registry_key", "order_intent_id", "generation")})

    checkpoint = getattr(gateway, "checkpoint_execution_intent", None)
    try:
        if callable(checkpoint):
            checkpoint()
    except Exception:
        _update_system_order_identity(registry_key, status="PRE_SUBMIT_REJECTED")
        raise
    # checkpoint 可能涉及磁碟寫入；完成後、真正呼叫券商前再查一次許可。
    if action == "BUY" and not manual and (signal_context.get("strategy_type") == "TREND" or callable(getattr(gateway, "get_strategy_market_context", None))):
        permission = _live_trend_permission(gateway, dict(signal_context))
        if not permission["allowed"]:
            _update_system_order_identity(registry_key, status="PRE_SUBMIT_REJECTED")
            if callable(checkpoint):
                checkpoint()
            return {"executed": False, "status": "BUY_REVALIDATION_REJECTED",
                    "rejection_codes": ["TREND_MARKET_INVALID"],
                    "rejection_reasons": [permission["reason"]],
                    "strategy_market_permission": permission,
                    "position": None, "order_response": None, "fill_summary": None}
    if action == "BUY":
        final_gate = (_buy_execution_data_gate(gateway, execution_decision, params) if manual else
                      revalidate_buy_before_order(gateway, execution_decision, signal_context, parameters=params))
        final_risk = final_gate.get("risk_guard") or {}
        if final_risk and int(final_risk.get("position_size") or 0) < int(execution_decision.get("position_size") or 0):
            final_gate["ok"] = False
            final_gate["rejection_codes"].append("PRE_SUBMIT_POSITION_SIZE_CHANGED")
        if not final_gate.get("ok"):
            _update_system_order_identity(registry_key, status="PRE_SUBMIT_REJECTED")
            if callable(checkpoint):
                checkpoint()
            return {"executed": False, "status": "BUY_REVALIDATION_REJECTED",
                    "rejection_codes": final_gate["rejection_codes"], "revalidation": final_gate,
                    "position": None, "order_response": None, "fill_summary": None}
        revalidation = final_gate
    try:
        order_response = _submit_order(gateway, request)
    except Exception as exc:
        ownership = _update_system_order_identity(registry_key, status="STATUS_UNKNOWN")
        if callable(checkpoint):
            checkpoint()
        return {"executed": True, "status": "ORDER_STATUS_UNKNOWN",
                "error": str(exc), "traceback": traceback.format_exc(),
                "ownership": ownership, "position": None,
                "reconciliation_required": ["ORDER", "MATCH", "POSITION"]}
    error = (
        str(
            order_response.get("error")
            or ("CTS_ORDER_FAILED" if order_response.get("ok") is False else "")
        )
        if isinstance(order_response, Mapping)
        else ""
    )
    order_id = (
        str(order_response.get("order_id") or "").strip()
        if isinstance(order_response, Mapping)
        else ""
    )
    order_no = (
        str(order_response.get("order_no") or "").strip()
        if isinstance(order_response, Mapping)
        else ""
    )
    proven_rejection = bool(isinstance(order_response, Mapping)
                            and (order_response.get("broker_rejected") is True
                                 or order_response.get("not_submitted") is True))
    if not proven_rejection and (error or not (order_id or order_no)):
        ownership = _update_system_order_identity(registry_key, status="STATUS_UNKNOWN")
        if callable(checkpoint):
            checkpoint()
        return {"executed": True, "status": "ORDER_STATUS_UNKNOWN",
                "ownership": ownership, "order_response": order_response,
                "position": None, "reconciliation_required": ["ORDER", "MATCH", "POSITION"]}
    ownership = _update_system_order_identity(
        registry_key, order_id=order_id, order_no=order_no,
        status=("PRE_SUBMIT_REJECTED" if order_response.get("not_submitted") else "REJECTED") if proven_rejection else "SUBMITTED",
    )

    if callable(checkpoint):
        checkpoint()
    if proven_rejection:
        return {
            "executed": not bool(order_response.get("not_submitted")),
            "status": "PRE_SUBMIT_REJECTED" if order_response.get("not_submitted") else "ORDER_REJECTED",
            "order_id": order_id,
            "order_no": order_no,
            "order_request": (
                request.to_dict() if hasattr(request, "to_dict") else str(request)
            ),
            "order_response": order_response,
            "fill_summary": None,
            "position": None,
            "ownership": ownership,
            "revalidation": revalidation,
        }

    result: Dict[str, Any] = {
        "executed": True,
        "status": "SUBMITTED",
        "order_id": order_id,
        "order_no": order_no,
        "order_request": (
            request.to_dict() if hasattr(request, "to_dict") else str(request)
        ),
        "order_response": order_response,
        "fill_summary": None,
        "position": None,
        "ownership": ownership,
        "submitted_at": submitted_at,
        "revalidation": revalidation,
    }
    if decision.get("action") == "SELL":
        existing_position = decision.get("existing_position")
        if isinstance(existing_position, Mapping):
            result["entry_ownership"] = {
                "managed_by": str(existing_position.get("managed_by") or ""),
                "management_source": str(
                    existing_position.get("management_source") or ""
                ),
                "entry_owner": str(existing_position.get("entry_owner") or ""),
                "entry_trigger": str(existing_position.get("entry_trigger") or ""),
            }
        result["exit_trigger"] = _execution_trigger(signal_context)
        # SYSTEM/AUTO 只描述送單來源；保留真正的 Intraday 出場判斷供日誌對照。
        result["exit_reason"] = str(signal_context.get("exit_reason") or "")
        exit_evidence = signal_context.get("position_exit")
        if not isinstance(exit_evidence, Mapping):
            exit_evidence = signal_context
        result["exit_signal_details"] = {
            key: exit_evidence.get(key)
            for key in (
                "stop_signal",
                "take_profit_signal",
                "vwap_loss_exit",
                "time_exit",
                "stop_price",
                "take_profit_price",
                "R_value",
                "entry_time",
                "exit_price_high",
                "exit_price_low",
                "exit_evidence",
                "exit_price_range_source",
                "post_entry_quote_count",
            )
        }

    if not confirm_fill:
        return result

    side = 1 if decision.get("action") == "BUY" else 2
    trade_date = str(getattr(request, "trade_date", "") or "")
    fill = reconcile_order_fill(
        gateway,
        account_id=account_id,
        symbol=str(decision.get("symbol") or ""),
        side=side,
        order_id=order_id or None,
        order_no=order_no or None,
        submitted_at=submitted_at,
        trade_date=trade_date,
    )
    result["fill_summary"] = fill

    # 若 NewOrder 身分缺失，但嚴格時間閘門只找到唯一成交委託，補回身分。
    fill_rows = [row for row in (fill.get("fills") or []) if isinstance(row, Mapping)]
    fill_order_ids = {
        str(row.get("order_id") or "").strip()
        for row in fill_rows
        if str(row.get("order_id") or "").strip()
    }
    fill_order_nos = {
        str(row.get("order_no") or "").strip()
        for row in fill_rows
        if str(row.get("order_no") or "").strip()
    }
    if not order_id and len(fill_order_ids) == 1:
        order_id = next(iter(fill_order_ids))
        result["order_id"] = order_id
    if not order_no and len(fill_order_nos) == 1:
        order_no = next(iter(fill_order_nos))
        result["order_no"] = order_no
    if order_id or order_no:
        result["ownership"] = _update_system_order_identity(
            registry_key,
            order_id=order_id,
            order_no=order_no,
        )

    if fill.get("ambiguous_match"):
        result["status"] = "MATCH_IDENTITY_AMBIGUOUS"
        result["ownership"] = _update_system_order_identity(
            registry_key,
            status="PENDING_RECONCILIATION",
        )
        return result

    expected_shares = max(0, _to_int(pending.get("requested_shares")))
    filled_shares = max(0, _to_int(fill.get("filled_shares")))
    if expected_shares > 0 and filled_shares > expected_shares:
        result["status"] = "FILL_QUANTITY_EXCEEDS_REQUEST"
        result["ownership"] = _update_system_order_identity(
            registry_key,
            status="PENDING_RECONCILIATION",
        )
        return result

    if decision.get("action") == "SELL":
        expected_qty = max(0, _to_int(decision.get("cts_quantity")))
        filled_qty = max(0, _to_int(fill.get("filled_cts_quantity")))
        if filled_qty >= expected_qty and expected_qty > 0:
            result["status"] = "FILLED"
            result["exit_fill_price"] = fill.get("average_fill_price")
            result["exited_shares"] = fill.get("filled_shares")
            result["ownership"] = _update_system_order_identity(
                registry_key,
                status="FILLED",
            )
            return result


        if fill.get("filled"):
            result["status"] = "PARTIALLY_FILLED"
            result["exit_fill_price"] = fill.get("average_fill_price")
            result["exited_shares"] = fill.get("filled_shares")
            result["ownership"] = _update_system_order_identity(
                registry_key,
                status="PARTIALLY_FILLED",
            )
        else:
            result["status"] = "SUBMITTED_NOT_FILLED"
        return result

    if not fill.get("filled"):
        result["status"] = "SUBMITTED_NOT_FILLED"
        result["ownership"] = _update_system_order_identity(
            registry_key,
            status="SUBMITTED_NOT_FILLED",
        )
        return result

    ownership = _update_system_order_identity(
        registry_key,
        status=(
            "FILLED"
            if filled_shares == expected_shares and expected_shares > 0
            else "PARTIALLY_FILLED"
        ),
        filled_shares=filled_shares,
    )
    result["status"] = "FILLED" if filled_shares == expected_shares else "PARTIALLY_FILLED"
    result["ownership"] = ownership
    result["position"] = build_position_from_fill(
        fill,
        symbol=str(decision.get("symbol") or ""),
        market=str(decision.get("market") or ""),
        parameters=params,
        entry_time=(
            _fill_entry_timestamp(
                min(
                    fill["fills"],
                    key=lambda row: (
                        row.get("match_date", ""),
                        row.get("match_time", ""),
                    ),
                )
            )
            if fill.get("fills")
            else None
        ),
        ownership=ownership,
    )
    return result


# =============================================================================
# 對外統一入口
# =============================================================================


def get_trading_context(
    symbol: Optional[str] = None,
    market: Optional[str] = None,
    *,
    gateway: Any = None,
    api_mode: str = "LIVE",
    replay_dir: str = "api_replay",
    signal_context: Optional[Mapping[str, Any]] = None,
    current_quote: Optional[Mapping[str, Any]] = None,
    account_state: Optional[Mapping[str, Any]] = None,
    parameters: Optional[TradingParameters] = None,
    account_id: Optional[str] = None,
    execute: bool = False,
    confirm_fill: bool = True,
    intraday_kwargs: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Trading 正式入口。

    - signal_context 未提供：呼叫 intraday.get_signal_context()；市場、候選與事件資料須由 intraday_kwargs 提供。
    - current_quote 未提供：透過同一 APIGateway 取得即時行情。
    - account_state 必須由 GUI / 主程式提供資金與當日風控狀態。
    - execute=False 只做決策，不送 CTS；execute=True 且 ALLOW 才實際送單。
    """
    params = parameters or TradingParameters()
    params.validate()
    if account_state is None:
        raise ValueError(
            "必須提供 account_state；Trading 不會猜測可用現金、每日交易次數或每日損益"
        )

    active_gateway = gateway
    integration_warnings: List[str] = []
    direct_signal_input = signal_context is not None
    direct_quote_input = current_quote is not None

    if signal_context is None:
        active_gateway = active_gateway or create_api_gateway(
            api_mode=api_mode, replay_dir=replay_dir
        )
        intraday_module = load_intraday_module()
        kwargs: Dict[str, Any] = {
            "symbol": symbol,
            "market": market,
            "gateway": active_gateway,
        }
        kwargs.update(dict(intraday_kwargs or {}))
        signal_context = intraday_module.get_signal_context(**kwargs)
    signal_context = dict(signal_context or {})

    symbol = str(symbol or signal_context.get("symbol") or "").strip()
    market = _normalize_market(market or signal_context.get("market"))
    if not symbol or not market:
        raise ValueError(
            "signal_context 必須提供 symbol / market，或直接指定 symbol / market"
        )

    if current_quote is None:
        active_gateway = active_gateway or create_api_gateway(
            api_mode=api_mode, replay_dir=replay_dir
        )
        current_quote = _freeze_atomic_market_state_snapshot(
            active_gateway,
            symbol,
            market,
        )
    current_quote = dict(current_quote or {})

    decision = evaluate_trading_decision(
        signal_context,
        current_quote,
        account_state,
        parameters=params,
    )

    execution = {
        "executed": False,
        "status": "DRY_RUN" if not execute else "NOT_ALLOWED",
        "order_response": None,
        "fill_summary": None,
        "position": None,
    }
    if execute and decision.get("decision") == "ALLOW":
        active_gateway = active_gateway or create_api_gateway(
            api_mode=api_mode, replay_dir=replay_dir
        )
        execution = execute_allowed_decision(
            active_gateway,
            decision,
            signal_context,
            current_quote,
            account_id=str(account_id or ""),
            parameters=params,
            confirm_fill=confirm_fill,
        )

    return {
        **decision,
        "signal_context": signal_context,
        "current_quote_data": current_quote,
        "execution": execution,
        "integration": {
            "api_mode": (
                str(api_mode).strip().upper()
                if active_gateway is not None
                else "DIRECT_INPUT"
            ),
            "signal_source": "EXPLICIT" if direct_signal_input else "INTRADAY",
            "quote_source": "EXPLICIT" if direct_quote_input else "APIGATEWAY",
            "execute": bool(execute),
            "confirm_fill": bool(confirm_fill),
            "pipeline": "api_gateway -> market -> scanner -> intraday -> trading -> CTS",
        },
        "warnings": integration_warnings,
    }


# =============================================================================
# 離線自我測試
# =============================================================================


def _sample_signal(*, entry: bool = True, exit_: bool = False) -> Dict[str, Any]:
    return {
        "strategy_type": "MOMENTUM",
        "strategy_stop_price": 98.6,
        "metrics": {"vwap": 99.0},
        "entry_wave_anchor_price": 99.0,
        "entry_signal": entry,
        "exit_signal": exit_,
        "entry_reason": "SELF_TEST",
        "entry_time": "2026-08-31T09:10:00+08:00",
        "signal_price": 99.5,
        "symbol": "2330",
        "market": "TWSE",
        "signal_fingerprint": "self-test-signal-001",
        "candidate": {
            "symbol": "2330",
            "market": "TWSE",
            "day_trade_eligible": True,
            "momentum_eligible": True,
            "trend_eligible": True,
            "trade_ready": True,
            "day_trade_status": "ELIGIBLE",
        },
        "market_context": {
            "market_state": "BULL",
            "trading_mode": "TREND",
            "allow_long": True,
            "intraday_trend": "UP",
            "momentum_long_allowed": True,
            "trend_long_allowed": True,
        },
    }


def _sample_quote(**updates: Any) -> Dict[str, Any]:
    row = {
        "symbol": "2330",
        "market": "TWSE",
        "trade_date": "2026-08-31",
        "timestamp": "2026-08-31T09:10:01+08:00",
        "price": 99.60,
        "prev_close": 99.0,
        "bid": 99.50,
        "ask": 99.60,
        "serial": 1001,
        "quote_sequence": 1,
        "symbol_quote_sequence": 1,
    }
    row.update(updates)
    event_dt = _parse_iso(row.get("timestamp"))
    if event_dt is not None:
        row.setdefault("event_time", int(event_dt.timestamp() * 1_000_000))
        row.setdefault("book_event_time", int(event_dt.timestamp() * 1_000_000))
        row.setdefault("book_active", True)
        row.setdefault("book_ready", True)
        row.setdefault("source_at", event_dt.isoformat())
        row.setdefault("realtime_source_at", event_dt.isoformat())
    return row


def _sample_account(**updates: Any) -> Dict[str, Any]:
    row = {
        "available_cash": 500_000.0,
        "equity": 500_000.0,
        "daily_trade_count": 1,
        "daily_pnl": 0.0,
        "positions": [],
        "open_orders": [],
        "processed_signal_fingerprints": [],
    }
    row.update(updates)
    return row


class _SelfTestMarketState:
    def __init__(self, quote_provider: Any) -> None:
        self.quote_provider = quote_provider

    def snapshot(self, symbol: str, *, as_of: Any = None) -> Dict[str, Any]:
        row = dict(self.quote_provider())
        assert str(row.get("symbol") or symbol) == str(symbol)
        current = as_of or datetime.now(timezone(timedelta(hours=8)))
        event_dt = _event_time_datetime(row.get("event_time"))
        if event_dt is not None:
            row["realtime_age_seconds"] = max(
                0.0,
                (current.astimezone(timezone.utc) - event_dt).total_seconds(),
            )
            row["realtime_fresh"] = (
                row["realtime_age_seconds"] <= BUY_REVALIDATION_MAX_QUOTE_AGE_SECONDS
            )
        return row


class _SelfTestMarketClient:
    def __init__(self, quote_provider: Any) -> None:
        self.state = _SelfTestMarketState(quote_provider)


def run_self_test() -> Dict[str, Any]:
    checks = 0
    params = TradingParameters()
    params.validate()
    checks += 1

    signal = _sample_signal()
    quote = _sample_quote()
    account = _sample_account()
    first = evaluate_trading_decision(signal, quote, account, parameters=params)
    second = evaluate_trading_decision(signal, quote, account, parameters=params)
    assert first == second
    assert first["decision"] == "ALLOW"
    assert first["action"] == "BUY"
    checks += 1

    for field_name in OUTPUT_FIELDS:
        assert field_name in first, field_name
    assert first["decision_fingerprint"]
    checks += 1

    assert first["spread_pct"] is not None and first["spread_pct"] < MAX_SPREAD_PCT
    assert first["execution_friction_pct"] is not None
    assert first["execution_friction_pct"] < MAX_EXECUTION_FRICTION_PCT
    assert first["position_size"] == 1000
    assert first["cts_trade_type"] == 0
    assert first["cts_quantity"] == 1
    checks += 1

    wide = evaluate_trading_decision(
        signal,
        _sample_quote(price=99.5, bid=99.0, ask=100.0),
        account,
        parameters=params,
    )
    assert wide["decision"] == "REJECT"
    assert "SPREAD_TOO_WIDE" in wide["rejection_codes"]
    checks += 1

    chased = evaluate_trading_decision(
        signal,
        _sample_quote(price=100.0, bid=99.5, ask=100.0),
        account,
        parameters=params,
    )
    assert chased["decision"] == "REJECT"
    assert "CHASE_TOO_HIGH" in chased["rejection_codes"]
    checks += 1

    unknown_daytrade = dict(
        signal, candidate=dict(signal["candidate"], day_trade_eligible=None)
    )
    unknown = evaluate_trading_decision(
        unknown_daytrade, quote, account, parameters=params
    )
    assert "DAY_TRADE_ELIGIBILITY_UNKNOWN" in unknown["rejection_codes"]
    checks += 1

    existing_account = _sample_account(
        positions=[
            {
                "symbol": "2330",
                "market": "TWSE",
                "quantity": 1000,
                "entry_price": 99.0,
                "managed_by": "MOMENTUM_SCALPING",
                "managed_quantity": 1000,
                "managed_sellable_quantity": 1000,
            }
        ]
    )
    existing = evaluate_trading_decision(
        signal, quote, existing_account, parameters=params
    )
    assert "EXISTING_POSITION" in existing["rejection_codes"]
    checks += 1

    duplicate = evaluate_trading_decision(
        signal,
        quote,
        _sample_account(processed_signal_fingerprints=["self-test-signal-001"]),
        parameters=params,
    )
    assert "DUPLICATE_SIGNAL" not in duplicate["rejection_codes"]
    assert duplicate["decision"] == "ALLOW"
    checks += 1

    daily_count = evaluate_trading_decision(
        signal,
        quote,
        _sample_account(daily_trade_count=MAX_DAILY_TRADE_COUNT),
        parameters=params,
    )
    assert "MAX_DAILY_TRADE_COUNT_REACHED" in daily_count["rejection_codes"]
    checks += 1

    daily_loss = evaluate_trading_decision(
        signal,
        quote,
        _sample_account(daily_pnl=-10_000.0),
        parameters=params,
    )
    assert "DAILY_LOSS_LIMIT_REACHED" in daily_loss["rejection_codes"]
    checks += 1

    no_cash = evaluate_trading_decision(
        signal,
        quote,
        _sample_account(available_cash=0.0),
        parameters=params,
    )
    assert "INSUFFICIENT_CASH" in no_cash["rejection_codes"]
    checks += 1

    blocked_signal = dict(
        signal, market_context=dict(signal["market_context"], allow_long=False)
    )
    blocked = evaluate_trading_decision(
        blocked_signal, quote, account, parameters=params
    )
    assert "MARKET_BLOCKED" in blocked["rejection_codes"]
    checks += 1

    # BUY 真正送單前只重抓單一股票；驗證 fresh / stale / momentum / spread 固定拒絕碼。
    class _RevalidationGateway:
        def __init__(self, refreshed_quote: Mapping[str, Any]) -> None:
            self.refreshed_quote = dict(refreshed_quote)
            self.calls = 0
            self.market = _SelfTestMarketClient(lambda: dict(self.refreshed_quote))

    revalidation_now = _parse_iso("2026-08-31T09:10:02+08:00")
    assert revalidation_now is not None
    fresh_gate = revalidate_buy_before_order(
        _RevalidationGateway(_sample_quote(timestamp="2026-08-31T09:10:01+08:00")),
        first,
        signal,
        parameters=params,
        now=revalidation_now,
    )
    assert fresh_gate["ok"] is True
    assert fresh_gate["rejection_codes"] == []
    assert fresh_gate["quote_age_seconds"] == 1.0
    checks += 1

    stale_gate = revalidate_buy_before_order(
        _RevalidationGateway(_sample_quote(timestamp="2026-08-31T09:09:30+08:00")),
        first,
        signal,
        parameters=params,
        now=revalidation_now,
    )
    assert stale_gate["ok"] is False
    assert "BUY_REVALIDATION_QUOTE_STALE" in stale_gate["rejection_codes"]
    checks += 1

    momentum_gate = revalidate_buy_before_order(
        _RevalidationGateway(
            _sample_quote(
                timestamp="2026-08-31T09:10:01+08:00",
                price=99.40,
                bid=99.30,
                ask=99.40,
            )
        ),
        first,
        signal,
        parameters=params,
        now=revalidation_now,
    )
    # 訊號價下方的正常回測仍有效；嚴格零回落另行驗證。
    assert momentum_gate["ok"] is True
    assert momentum_gate["entry_momentum"]["trigger_passed"] is True
    checks += 1

    spread_gate = revalidate_buy_before_order(
        _RevalidationGateway(
            _sample_quote(
                timestamp="2026-08-31T09:10:01+08:00",
                price=99.60,
                bid=99.00,
                ask=100.00,
            )
        ),
        first,
        signal,
        parameters=params,
        now=revalidation_now,
    )
    assert spread_gate["ok"] is False
    assert "BUY_REVALIDATION_SPREAD_TOO_WIDE" in spread_gate["rejection_codes"]
    checks += 1

    auto_wave_signal = dict(
        signal,
        execution_trigger="AUTO",
        entry_wave_anchor_price=99.0,
        entry_wave_id="self-test-wave-001",
    )
    wave_ok = revalidate_buy_before_order(
        _RevalidationGateway(
            _sample_quote(
                timestamp="2026-08-31T09:10:01+08:00",
                price=99.50,
                bid=99.50,
                ask=99.60,
            )
        ),
        first,
        auto_wave_signal,
        parameters=params,
        now=revalidation_now,
    )
    assert wave_ok["ok"] is True
    assert wave_ok["wave_extension"]["entry_wave_anchor_price"] == 99.0
    assert wave_ok["wave_extension"]["executable_price"] == 99.6
    assert wave_ok["wave_extension"]["wave_extension_pct"] <= MAX_BREAKOUT_EXTENSION_PCT
    checks += 1

    missing_wave_anchor = dict(auto_wave_signal)
    missing_wave_anchor.pop("entry_wave_anchor_price", None)
    missing_anchor_gate = revalidate_buy_before_order(
        _RevalidationGateway(
            _sample_quote(
                timestamp="2026-08-31T09:10:01+08:00",
                price=99.50,
                bid=99.50,
                ask=99.60,
            )
        ),
        first,
        missing_wave_anchor,
        parameters=params,
        now=revalidation_now,
    )
    assert missing_anchor_gate["ok"] is False
    assert (
        "BUY_REVALIDATION_WAVE_ANCHOR_INVALID" in missing_anchor_gate["rejection_codes"]
    )
    checks += 1

    extended_wave_signal = dict(auto_wave_signal, entry_wave_anchor_price=98.8)
    extension_gate_a = revalidate_buy_before_order(
        _RevalidationGateway(
            _sample_quote(
                timestamp="2026-08-31T09:10:01+08:00",
                price=99.50,
                bid=99.50,
                ask=99.60,
            )
        ),
        first,
        extended_wave_signal,
        parameters=params,
        now=revalidation_now,
    )
    extension_gate_b = revalidate_buy_before_order(
        _RevalidationGateway(
            _sample_quote(
                timestamp="2026-08-31T09:10:01+08:00",
                price=99.50,
                bid=99.50,
                ask=99.60,
            )
        ),
        first,
        extended_wave_signal,
        parameters=params,
        now=revalidation_now,
    )
    assert extension_gate_a["ok"] is False
    assert extension_gate_a["rejection_codes"] == extension_gate_b["rejection_codes"]
    assert extension_gate_a["wave_extension"] == extension_gate_b["wave_extension"]
    assert (
        "BUY_REVALIDATION_WAVE_EXTENSION_TOO_HIGH"
        in extension_gate_a["rejection_codes"]
    )
    checks += 1

    class _WaveRejectCTS:
        def __init__(self) -> None:
            self.new_order_calls = 0

        def new_order(self, request: Any) -> Dict[str, Any]:
            self.new_order_calls += 1
            raise AssertionError("Wave Revalidation REJECT 後不得呼叫 CTS new_order")

    class _WaveRejectGateway:
        def __init__(self) -> None:
            self.cts = _WaveRejectCTS()
            self.refreshed_quote = _sample_quote(
                timestamp=datetime.now(timezone(timedelta(hours=8))).isoformat(
                    timespec="milliseconds"
                )
            )
            self.market = _SelfTestMarketClient(lambda: dict(self.refreshed_quote))

    extension_gateway = _WaveRejectGateway()
    extension_execution = execute_allowed_decision(
        extension_gateway,
        first,
        extended_wave_signal,
        quote,
        account_id="79Z-1234567",
        parameters=params,
        confirm_fill=False,
    )
    assert extension_execution["status"] == "BUY_REVALIDATION_REJECTED"
    assert (
        "BUY_REVALIDATION_WAVE_EXTENSION_TOO_HIGH"
        in extension_execution["rejection_codes"]
    )
    assert extension_gateway.cts.new_order_calls == 0

    anchor_gateway = _WaveRejectGateway()
    anchor_execution = execute_allowed_decision(
        anchor_gateway,
        first,
        missing_wave_anchor,
        quote,
        account_id="79Z-1234567",
        parameters=params,
        confirm_fill=False,
    )
    assert anchor_execution["status"] == "BUY_REVALIDATION_REJECTED"
    assert "BUY_REVALIDATION_WAVE_ANCHOR_INVALID" in anchor_execution["rejection_codes"]
    assert anchor_gateway.cts.new_order_calls == 0
    checks += 1

    fills = summarize_cts_fills(
        [
            {
                "F2": "0",
                "F3": "2330",
                "F4": "1",
                "F6": "100.0",
                "F7": "1",
                "F9": "X001",
                "F14": "OID1",
                "F10": "20260831",
                "F11": "091001",
            },
            {
                "F2": "0",
                "F3": "2330",
                "F4": "1",
                "F6": "101.0",
                "F7": "1",
                "F9": "X001",
                "F14": "OID1",
                "F10": "20260831",
                "F11": "091002",
            },
        ],
        symbol="2330",
        side=1,
        order_id="OID1",
    )
    assert fills["filled"] is True
    assert fills["filled_shares"] == 2000
    assert fills["average_fill_price"] == 100.5
    checks += 1

    position = build_position_from_fill(
        fills,
        symbol="2330",
        market="TWSE",
        parameters=params,
    )
    assert position["entry_price"] == 100.5
    assert position["take_profit_price"] is None
    assert position["stop_price"] == 100.5 * (1 - params.emergency_stop_loss_pct / 100)
    assert position["price_basis"] == "CTS_ACTUAL_AVERAGE_FILL_PRICE"
    checks += 1

    # 出場訊號不因價差過大而阻擋；風控出場優先於執行品質。
    exit_signal = _sample_signal(entry=False, exit_=True)
    exit_decision = evaluate_trading_decision(
        exit_signal,
        _sample_quote(price=99.0, bid=98.0, ask=100.0),
        existing_account,
        parameters=params,
    )
    assert exit_decision["decision"] == "ALLOW"
    assert exit_decision["action"] == "SELL"
    assert exit_decision["cts_quantity"] == 1
    checks += 1

    no_position_exit = evaluate_trading_decision(
        exit_signal, quote, account, parameters=params
    )
    assert no_position_exit["decision"] == "REJECT"
    assert "NO_POSITION_TO_EXIT" in no_position_exit["rejection_codes"]
    checks += 1

    incomplete = evaluate_trading_decision(
        signal,
        quote,
        {"available_cash": 500000.0, "positions": []},
        parameters=params,
    )
    assert "ACCOUNT_STATE_INCOMPLETE" in incomplete["rejection_codes"]
    checks += 1

    order_state = parse_cts_order_state(
        [
            {
                "F0": "79Z-1234567",
                "F1": "20260831",
                "F2": "0",
                "F3": "0",
                "F4": "2",
                "F5": "2330",
                "F6": "0",
                "F7": "98.0",
                "F8": "2",
                "F9": "X-SELL-1",
                "F10": "1",
                "F11": "0",
                "F14": "委託成功",
                "F15": "OID-SELL-1",
                "F16": "0",
                "F17": "98.0",
                "F18": "1",
                "F19": "20260831",
                "F20": "091010",
                "F26": "0",
                "F27": "0",
                "F28": "成功",
                "F30": "0",
            }
        ],
        symbol="2330",
        side=2,
        order_id="OID-SELL-1",
    )
    assert order_state["found"] is True
    assert order_state["active"] is True
    assert order_state["partially_filled"] is True
    assert order_state["current_quantity"] == 1
    checks += 1


    sell_account = _sample_account(
        positions=[
            {
                "symbol": "2330",
                "market": "TWSE",
                "quantity": 2000,
                "managed_by": "MOMENTUM_SCALPING",
                "managed_quantity": 2000,
                "managed_sellable_quantity": 2000,
                "entry_price": 99.0,
            }
        ]
    )
    sell_quote = _sample_quote(price=98.0, bid=98.0, ask=98.1)
    sell_decision = evaluate_trading_decision(
        exit_signal,
        sell_quote,
        sell_account,
        parameters=params,
    )
    assert sell_decision["decision"] == "ALLOW"
    assert sell_decision["cts_quantity"] == 2

    class _RepriceCTS:
        def __init__(self) -> None:
            self.repriced = False
            self.submitted = False

        def new_order(self, request: Any) -> Dict[str, Any]:
            assert request.side == 2
            assert request.quantity == 2
            assert request.price_type == 4 and request.price == "0"
            self.submitted = True
            return {"error": "", "order_id": "OID-R1", "order_response": "OK"}

        def query_order(
            self, account_id: str, force_query: bool = True
        ) -> Dict[str, Any]:
            if not self.submitted:
                return {"ok": True, "parsed_result": []}
            matched = 2 if self.repriced else 1
            current = 0 if self.repriced else 1
            price = "97.9" if self.repriced else "98.0"
            return {
                "parsed_result": [
                    {
                        "F0": account_id,
                        "F1": "20260831",
                        "F2": "0",
                        "F3": "0",
                        "F4": "2",
                        "F5": "2330",
                        "F6": "0",
                        "F7": price,
                        "F8": "2",
                        "F9": "X-R1",
                        "F10": str(matched),
                        "F11": "0",
                        "F14": "委託成功",
                        "F15": "OID-R1",
                        "F16": "0",
                        "F17": price,
                        "F18": str(current),
                        "F19": "20260831",
                        "F20": "091011",
                        "F26": "0",
                        "F27": "0",
                        "F28": "成功",
                        "F30": "0",
                    }
                ]
            }

        def query_position(self, account_id, trade_date, force_query=True):
            return {"ok": True, "parsed_result": [{"F0": "0", "F1": "2330", "F3": "2000"}]}

        def query_match(
            self, account_id: str, force_query: bool = True
        ) -> Dict[str, Any]:
            rows = [
                {
                    "F2": "0",
                    "F3": "2330",
                    "F4": "2",
                    "F6": "98.0",
                    "F7": "1",
                    "F9": "X-R1",
                    "F14": "OID-R1",
                    "F10": "20260831",
                    "F11": "091010",
                }
            ]
            if self.repriced:
                rows.append(
                    {
                        "F2": "0",
                        "F3": "2330",
                        "F4": "2",
                        "F6": "97.9",
                        "F7": "1",
                        "F9": "X-R1",
                        "F14": "OID-R1",
                        "F10": "20260831",
                        "F11": "091011",
                    }
                )
            return {"parsed_result": rows}

        def modify_order(self, request: Any) -> Dict[str, Any]:
            assert request.side == 2
            assert request.modify_type == 2
            assert float(request.new_price) == 97.9
            assert request.current_quantity == 1
            assert request.matched_quantity == 1
            self.repriced = True
            return {"error": "", "order_id": "OID-R1", "order_response": "OK"}

        def cancel_order(self, request: Any) -> Dict[str, Any]:
            raise AssertionError("改價後已全數成交，不應刪單")

    class _RepriceGateway:
        def __init__(self) -> None:
            self.cts = _RepriceCTS()
            self.refreshed_quote = _sample_quote(price=98.0, bid=98.0, ask=98.1, timestamp=datetime.now(timezone.utc).isoformat())
            self.market = _SelfTestMarketClient(lambda: dict(self.refreshed_quote))

    # 市價部分成交只追蹤成交，不改價、不撤單重掛。
    market_gateway = _RepriceGateway()
    market_request = _create_cts_order_request(
        market_gateway, sell_decision, exit_signal, sell_quote,
        account_id="79Z-1234567", parameters=params)
    assert market_request.price_type == 4 and market_request.price == "0"
    market_execution = execute_allowed_decision(
        market_gateway, sell_decision, exit_signal, sell_quote,
        account_id="79Z-1234567", parameters=params, confirm_fill=True)
    assert market_execution["status"] == "PARTIALLY_FILLED"
    assert market_execution["fill_summary"]["filled_shares"] == 1000
    assert not market_gateway.cts.repriced
    checks += 1

    class _CancelReplaceCTS:
        def __init__(self) -> None:
            self.new_order_count = 0
            self.canceled = False
            self.replaced = False

        def new_order(self, request: Any) -> Dict[str, Any]:
            self.new_order_count += 1
            assert request.side == 2
            if self.new_order_count == 1:
                assert request.quantity == 2
                return {"error": "", "order_id": "OID-C1", "order_response": "OK"}
            assert request.quantity == 1
            self.replaced = True
            return {"error": "", "order_id": "OID-C2", "order_response": "OK"}

        def query_order(
            self, account_id: str, force_query: bool = True
        ) -> Dict[str, Any]:
            if self.replaced:
                return {
                    "parsed_result": [
                        self._row(
                            account_id,
                            oid="OID-C2",
                            order_no="X-C2",
                            original=1,
                            matched=1,
                            canceled=0,
                            current=0,
                            price="97.9",
                        )
                    ]
                }
            return {
                "parsed_result": [
                    self._row(
                        account_id,
                        oid="OID-C1",
                        order_no="X-C1",
                        original=2,
                        matched=1,
                        canceled=1 if self.canceled else 0,
                        current=0 if self.canceled else 1,
                        price="98.0",
                    )
                ]
            }

        @staticmethod
        def _row(
            account_id: str,
            *,
            oid: str,
            order_no: str,
            original: int,
            matched: int,
            canceled: int,
            current: int,
            price: str,
        ) -> Dict[str, Any]:
            return {
                "F0": account_id,
                "F1": "20260831",
                "F2": "0",
                "F3": "0",
                "F4": "2",
                "F5": "2330",
                "F6": "0",
                "F7": price,
                "F8": str(original),
                "F9": order_no,
                "F10": str(matched),
                "F11": str(canceled),
                "F14": "委託成功",
                "F15": oid,
                "F16": "0",
                "F17": price,
                "F18": str(current),
                "F19": "20260831",
                "F20": "091012",
                "F26": "0",
                "F27": "0",
                "F28": "成功",
                "F30": "0",
            }

        def query_match(
            self, account_id: str, force_query: bool = True
        ) -> Dict[str, Any]:
            rows = [
                {
                    "F2": "0",
                    "F3": "2330",
                    "F4": "2",
                    "F6": "98.0",
                    "F7": "1",
                    "F9": "X-C1",
                    "F14": "OID-C1",
                    "F10": "20260831",
                    "F11": "091010",
                }
            ]
            if self.replaced:
                rows.append(
                    {
                        "F2": "0",
                        "F3": "2330",
                        "F4": "2",
                        "F6": "97.9",
                        "F7": "1",
                        "F9": "X-C2",
                        "F14": "OID-C2",
                        "F10": "20260831",
                        "F11": "091012",
                    }
                )
            return {"parsed_result": rows}

        def modify_order(self, request: Any) -> Dict[str, Any]:
            raise AssertionError("此測試停用改價，不應呼叫 modify_order")

        def cancel_order(self, request: Any) -> Dict[str, Any]:
            assert request.oid == "OID-C1"
            assert request.quantity == 0
            assert request.current_quantity == 1
            assert request.matched_quantity == 1
            self.canceled = True
            return {"error": "", "order_id": "OID-C1", "order_response": "OK"}

    class _CancelReplaceGateway:
        def __init__(self) -> None:
            self.cts = _CancelReplaceCTS()
            self.refreshed_quote = _sample_quote(price=98.0, bid=98.0, ask=98.1, timestamp=datetime.now(timezone.utc).isoformat())
            self.market = _SelfTestMarketClient(lambda: dict(self.refreshed_quote))

    # 保留 CTS 撤單請求與剩餘量驗證；不再呼叫已移除的追單流程。
    cancel_gateway = _CancelReplaceGateway()
    cancel_state = query_cts_order_state(
        cancel_gateway, account_id="79Z-1234567", symbol="2330",
        side=2, order_id="OID-C1")
    cancel_request = _create_cts_cancel_request(
        cancel_gateway, cancel_state, account_id="79Z-1234567")
    cancel_gateway.cts.cancel_order(cancel_request)
    assert cancel_gateway.cts.canceled
    assert cancel_gateway.cts.new_order_count == 0
    checks += 1

    # 模擬 CTS 同步新單 + 成交查詢，驗證實際成交價會取代 signal_price 成為風控基準。
    class _MockCTS:
        def new_order(self, request: Any) -> Dict[str, Any]:
            assert request.symbol == "2330"
            assert request.side == 1
            assert request.quantity == 1
            return {"error": "", "order_id": "OID-MOCK-1", "order_response": "OK"}

        def query_match(
            self, account_id: str, force_query: bool = True
        ) -> Dict[str, Any]:
            return {
                "parsed_result": [
                    {
                        "F2": "0",
                        "F3": "2330",
                        "F4": "1",
                        "F6": "99.6",
                        "F7": "1",
                        "F9": "X-MOCK-1",
                        "F14": "OID-MOCK-1",
                        "F10": "20260831",
                        "F11": "091002",
                    }
                ]
            }

    class _MockGateway:
        def __init__(self) -> None:
            self.cts = _MockCTS()
            self.refreshed_quote = _sample_quote(
                timestamp=datetime.now(timezone(timedelta(hours=8))).isoformat(
                    timespec="milliseconds"
                )
            )
            self.market = _SelfTestMarketClient(lambda: dict(self.refreshed_quote))

    executed = execute_allowed_decision(
        _MockGateway(),
        first,
        signal,
        quote,
        account_id="79Z-1234567",
        parameters=params,
        confirm_fill=True,
    )
    assert executed["status"] == "FILLED"
    assert executed["position"]["entry_price"] == 99.6
    assert executed["position"]["price_basis"] == "CTS_ACTUAL_AVERAGE_FILL_PRICE"
    assert executed["position"]["take_profit_price"] is None
    assert executed["position"]["managed_by"] == "MOMENTUM_SCALPING"
    assert executed["position"]["management_source"] == "系統今日"
    assert executed["position"]["managed_quantity"] == 1000
    checks += 1

    # api_gateway 正規化後的普通交易 F7=1000 股，不得在 Trading 再乘一次 1000。
    normalized_fill = summarize_cts_fills(
        [
            {
                "trade_type": 0,
                "symbol": "2413",
                "side": 1,
                "price": 49.15,
                "raw_quantity": 1000,
                "cts_quantity": 1,
                "filled_shares": 1000,
                "quantity_normalization": "CTS_QUERYMATCH_RAW_SHARES_COMPAT",
                "order_no": "S0APU",
                "order_id": "109106",
                "match_date": "20260909",
                "match_time": "101355250",
            }
        ],
        symbol="2413",
        side=1,
        order_id="109106",
    )
    assert normalized_fill["filled_shares"] == 1000
    assert normalized_fill["filled_cts_quantity"] == 1
    checks += 1

    # 第二次同股進場：submit_time 前的舊成交必須被時間閘門排除。
    assert (
        _match_not_before_submit(
            "20260909",
            "101355250",
            "2026-09-09T10:13:40.000+08:00",
        )
        is True
    )
    assert (
        _match_not_before_submit(
            "20260909",
            "093654980",
            "2026-09-09T10:13:40.000+08:00",
        )
        is False
    )
    checks += 1

    class _OwnershipCTS:
        def new_order(self, request: Any) -> Dict[str, Any]:
            return {
                "error": "",
                "order_id": "109106",
                "order_no": "S0APU",
                "timestamp": "2026-09-09T10:13:40.000+08:00",
            }

        def query_match(
            self, account_id: str, force_query: bool = True
        ) -> Dict[str, Any]:
            return {
                "normalized_result": [
                    {
                        "trade_type": 0,
                        "symbol": "2413",
                        "side": 1,
                        "price": 49.30,
                        "raw_quantity": 2000,
                        "cts_quantity": 2,
                        "filled_shares": 2000,
                        "quantity_normalization": "CTS_QUERYMATCH_RAW_SHARES_COMPAT",
                        "order_no": "S088W",
                        "order_id": "682399",
                        "match_date": "20260909",
                        "match_time": "093654980",
                    },
                    {
                        "trade_type": 0,
                        "symbol": "2413",
                        "side": 1,
                        "price": 49.15,
                        "raw_quantity": 3000,
                        "cts_quantity": 3,
                        "filled_shares": 3000,
                        "quantity_normalization": "CTS_QUERYMATCH_RAW_SHARES_COMPAT",
                        "order_no": "S0APU",
                        "order_id": "109106",
                        "match_date": "20260909",
                        "match_time": "101421980",
                    },
                ]
            }

    class _OwnershipGateway:
        def __init__(self) -> None:
            self.cts = _OwnershipCTS()
            self.refreshed_quote = _sample_quote(
                symbol="2413",
                market="TWSE",
                trade_date="2026-09-09",
                timestamp=datetime.now(timezone(timedelta(hours=8))).isoformat(
                    timespec="milliseconds"
                ),
                last=49.15,
                price=49.15,
                bid=49.10,
                ask=49.15,
            )
            self.market = _SelfTestMarketClient(lambda: dict(self.refreshed_quote))

    ownership_decision = dict(first)
    ownership_decision.update(
        {
            "symbol": "2413",
            "market": "TWSE",
            "cts_quantity": 3,
            "position_size": 3000,
            "order_price": 49.15,
        }
    )
    ownership_signal = dict(signal)
    ownership_signal.update(
        {
            "symbol": "2413",
            "market": "TWSE",
            "entry_time": "2026-09-09T10:13:40+08:00",
            "signal_price": 49.15,
            "signal_fingerprint": "ownership-test-2413",
        }
    )
    ownership_signal.update(
        strategy_stop_price=48.8, entry_wave_anchor_price=49.0, metrics={"vwap": 49.0}
    )
    ownership_decision.update(strategy_stop_price=48.8, effective_stop_price=48.8)
    ownership_quote = dict(quote)
    ownership_quote.update(
        {
            "symbol": "2413",
            "market": "TWSE",
            "trade_date": "2026-09-09",
            "price": 49.15,
            "bid": 49.10,
            "ask": 49.15,
        }
    )
    ownership_execution = execute_allowed_decision(
        _OwnershipGateway(),
        ownership_decision,
        ownership_signal,
        ownership_quote,
        account_id="79Z-1234567",
        parameters=params,
        confirm_fill=True,
    )
    assert ownership_execution["status"] == "FILLED"
    assert ownership_execution["order_id"] == "109106"
    assert ownership_execution["order_no"] == "S0APU"
    assert ownership_execution["fill_summary"]["filled_shares"] == 3000
    assert ownership_execution["position"]["entry_price"] == 49.15
    assert ownership_execution["position"]["managed_quantity"] == 3000
    assert ownership_execution["position"]["management_source"] == "系統今日"
    checks += 1

    required_parameter_keys = {
        "MAX_SPREAD_PCT",
        "MAX_SLIPPAGE_PCT",
        "MAX_CHASE_PCT",
        "MAX_EXECUTION_FRICTION_PCT",
        "BUY_REVALIDATION_MAX_QUOTE_AGE_SECONDS",
        "MAX_BREAKOUT_EXTENSION_PCT",
        "MAX_ENTRY_TRIGGER_RETRACE_PCT",
        "MAX_BUY_PRICE",
        "MAX_POSITION_COUNT",
        "MAX_DAILY_TRADE_COUNT",
        "MAX_DAILY_LOSS_PCT",
        "MAX_SINGLE_POSITION_PCT",
        "MAX_RISK_PER_TRADE_PCT",
    }
    assert required_parameter_keys.issubset(first["parameters"])
    checks += 1

    return {
        "ok": True,
        "checks": checks,
        "output_fields": OUTPUT_FIELDS,
        "decision_pipeline": (
            "Intraday Signal -> current quote -> day-trade / spread / slippage / chase -> "
            "cash / position / daily limits -> position sizing -> ALLOW / REJECT -> "
            "BUY single-symbol revalidation + Wave Extension Gate -> CTS"
        ),
        "fill_pipeline": "CTS actual fill -> entry_price -> stop_price / take_profit_price",
        "sell_fill_pipeline": "SELL market order -> query match -> partial/full fill -> reconcile",
        "reproducibility": (
            "相同 Signal_Context + Current_Quote + Account_State + Trading_Parameters "
            "= 相同 Trade_Decision"
        ),
        "buy_revalidation_reproducibility": (
            "相同 AUTO Signal_Context + CTS 前最新 Quote + Trading_Parameters "
            "= 相同 Wave Extension ALLOW/REJECT 與 rejection code"
        ),
        "sample": {
            "decision": first["decision"],
            "order_price": first["order_price"],
            "position_size": first["position_size"],
            "execution_friction_pct": first["execution_friction_pct"],
            "fill_price": position["entry_price"],
            "stop_price": position["stop_price"],
            "take_profit_price": position["take_profit_price"],
        },
    }


# =============================================================================
# 連結診斷
# =============================================================================


def diagnose_links() -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "trading_entry": str(inspect.signature(get_trading_context)),
        "pure_core": str(inspect.signature(evaluate_trading_decision)),
        "fill_core": str(inspect.signature(build_position_from_fill)),
        "output_fields": OUTPUT_FIELDS,
        "market_state_revalidation": {
            "source": "APIGateway.market.state.snapshot",
            "atomic": True,
            "external_quote_http": False,
            "freshness_basis": "FUGLE_EVENT_TIME",
            "buy_freshness_threshold_seconds": BUY_REVALIDATION_MAX_QUOTE_AGE_SECONDS,
            "event_age_statistics": get_event_age_statistics(),
        },
        "module_candidates": {
            "api_gateway": api_gateway_candidates(),
            "market": market_candidates(),
            "scanner": scanner_candidates(),
            "intraday": intraday_candidates(),
        },
    }

    try:
        api_module = load_api_gateway_module()
        gateway_class = getattr(api_module, "APIGateway")
        cts_class = getattr(api_module, "CTSClient", None)
        order_class = getattr(api_module, "CTSOrderRequest", None)
        modify_class = getattr(api_module, "CTSModifyRequest", None)
        cancel_class = getattr(api_module, "CTSCancelRequest", None)
        api_ok = (
            callable(getattr(gateway_class, "get_quote", None))
            and cts_class is not None
            and callable(getattr(cts_class, "new_order", None))
            and callable(getattr(cts_class, "modify_order", None))
            and callable(getattr(cts_class, "cancel_order", None))
            and callable(getattr(cts_class, "query_order", None))
            and callable(getattr(cts_class, "query_match", None))
            and callable(getattr(cts_class, "query_position", None))
            and order_class is not None
            and modify_class is not None
            and cancel_class is not None
        )
        result["api_gateway"] = {
            "ok": api_ok,
            "module": "api_gateway",
            "get_quote": callable(getattr(gateway_class, "get_quote", None)),
            "CTSOrderRequest": order_class is not None,
            "CTSModifyRequest": modify_class is not None,
            "CTSCancelRequest": cancel_class is not None,
            "CTS_new_order": cts_class is not None
            and callable(getattr(cts_class, "new_order", None)),
            "CTS_modify_order": cts_class is not None
            and callable(getattr(cts_class, "modify_order", None)),
            "CTS_cancel_order": cts_class is not None
            and callable(getattr(cts_class, "cancel_order", None)),
            "CTS_query_order": cts_class is not None
            and callable(getattr(cts_class, "query_order", None)),
            "CTS_query_match": cts_class is not None
            and callable(getattr(cts_class, "query_match", None)),
            "CTS_query_position": cts_class is not None
            and callable(getattr(cts_class, "query_position", None)),
        }
    except Exception as exc:
        result["api_gateway"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    for name, loader, function_name in (
        ("market", load_market_module, "get_market_context"),
        ("scanner", load_scanner_module, "get_candidates"),
        ("intraday", load_intraday_module, "get_signal_context"),
    ):
        try:
            module = loader()
            func = getattr(module, function_name)
            result[name] = {
                "ok": callable(func),
                "module": name,
                "entry": str(inspect.signature(func)),
            }
        except Exception as exc:
            result[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    result["ok"] = all(
        bool(result.get(name, {}).get("ok"))
        for name in ("api_gateway", "market", "scanner", "intraday")
    )
    result["pipeline"] = (
        "api_gateway -> market -> scanner -> intraday -> trading -> CTS"
    )
    result["account_state_boundary"] = (
        "available_cash / daily_trade_count / daily_pnl 目前須由 GUI / 主程式明確傳入"
    )
    return result


# =============================================================================
# CLI
# =============================================================================


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="交易執行、倉位與風險控制模組")
    parser.add_argument("--self-test", action="store_true", help="執行完全離線自我測試")
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="檢查 api_gateway / market / scanner / intraday 連結",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 顯示結果")
    return parser


def _print_human(result: Mapping[str, Any]) -> None:
    print("=" * 112)
    print("交易執行、倉位與風險控制模組")
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
                "正式使用請由 GUI / main.py 呼叫 get_trading_context()；"
                "預設 execute=False 不會送出 CTS 委託。"
            ),
            "output_fields": OUTPUT_FIELDS,
            "pipeline": "api_gateway -> market -> scanner -> intraday -> trading -> CTS",
        }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        _print_human(result)
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())

# 更新摘要：無識別碼委託唯一對帳、共用五秒有效賣價、剩餘總風險動態縮量；策略條件不變。
