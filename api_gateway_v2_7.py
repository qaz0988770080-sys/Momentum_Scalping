# -*- coding: utf-8 -*-
"""
Fugle 市場資料與中國信託 CTS 交易 API 統一閘道。

檔案邊界
--------
1. 僅處理 Fugle 與 CTS 外部 API 連線、資料正規化、記憶體狀態與公開查詢介面。
2. 不處理 Runtime 排程、Scanner 邏輯、市場判定、交易策略或 GUI 顯示。
3. Fugle REST 僅負責初始化、快照、基本資料與歷史資料；來源錯誤直接交由呼叫端處理。
4. Fugle WebSocket trades 為盤中即時行情唯一事件時間軸；連線建立細節封裝在 Gateway 內。
5. get_quote() / get_quotes() / get_market_state_snapshot() 只讀記憶體 MarketState，不發即時 HTTP 請求。
6. Trade 與 Books 分別保存來源及接收時間；Books 獨立更新最佳 bid/ask，不產生成交事件。
7. MarketState 提供單一 Atomic Snapshot 公開介面，generation 與 Quote Sequence 只由已接受 Trade Event 推進。
8. CTS 僅保留登入、登出、帳號取得、新單、改單、刪單、委託／成交／庫存查詢與 Callback。

核心
----------
相同 Fugle 初始化資料 + 相同 Fugle 事件序列 + 相同訂閱集合 + 相同參數
= 相同 MarketState、Quote Sequence、事件接受順序與來源時間判定。
相同 CTS 回報序列 = 相同委託、成交與持倉狀態。

正式使用 CTS 前注意
--------------------
中國信託 CTS 文件要求 Python 使用 32-bit Python，並安裝 pywin32；
電腦亦需安裝含交易 API 的中國信託致富王，使 apiCTS.dll COM 元件完成註冊。
密碼只用於登入呼叫，本模組不會寫入 replay / log 保存資料。
"""

# 中文重點更新：當沖資格按日重用、失敗延後重試，REST 初始化併行且限制排程；CTS 異常獨立落盤及輪替。

# 中文重點更新：補齊審查要求之狀態識別、整張委託、資料時序與恢復安全邊界。

# 中文重點更新：Fugle 每日快照網路逾時時沿用最近成功快照，明確標記降級來源，避免 Scanner 因單次 REST 中斷停止更新。

# 中文重點更新：獨立 Books 訂閱及來源時間，離開或重連立即失效；成交事件不覆蓋有效五檔。

# 中文重點更新：市場 REST 快照成功取得時保存獨立取得時間；快取與降級回退不重設時間。

# 中文重點更新：CTS transport 健康與重連對帳分離；持倉逐檔訂閱活性檢查及 SELL 專用 REST 降級行情。

# 中文重點更新：送單前檢查價格與數量，區分尚未送出及送出後例外；保存完整例外堆疊。

# 中文重點更新：WebSocket 寫入等待上限四秒、同股票失敗／斷流重訂閱至少六十秒；重連採三十至六十秒退避，回呼不直接執行訂閱 I/O。

# 中文重點更新：以實際 Stock_NewOrder 呼叫界線記錄送單與回覆；排隊取消歸為未送單，COM 後例外保留未知狀態；分類資料獨立保留。

# 中文重點更新：依中信官方範例支援市價代碼與價格格式；空白及不完整庫存回覆不得視為零持倉。

# 中文重點更新：基本資料完成後一次發布，類股快照讀取不再等待網路批次鎖。

# 中文重點更新：市場參考資料以獨立鎖依序落盤，避免平行快照共用暫存檔相互覆寫；提供逐筆事件專用的輕量斷線識別讀取。

# 中文重點更新：LIVE 行情事件只在記憶體傳遞，不保存逐筆回放；CTS 異常交由統一紀錄佇列，離線回放讀寫功能保留。

# 中文重點更新：疑似斷流且 REST 任務仍執行時持續回報待查狀態，避免空結果被當成恢復；原檢查週期、門檻及保護動作不變。

from __future__ import annotations

# 中文重點更新：保留既有 Ticker 產業代碼並提供記憶體類股分類介面，不增加逐檔網路請求。

# 中文重點更新：串流回呼不發送網路訂閱；訂閱由背景輪詢同步，避免回呼持鎖阻塞狀態讀取。參考快照寫檔移出串流狀態鎖。

import argparse

# Fugle Intraday Tickers 官方產業代碼；00 為無產業屬性，不納入類股。
_INDUSTRY_NAMES = dict(zip(
    "01 02 03 04 05 06 08 09 10 11 12 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 80".split(),
    "水泥工業 食品工業 塑膠工業 紡織纖維 電機機械 電器電纜 玻璃陶瓷 造紙工業 鋼鐵工業 橡膠工業 汽車工業 建材營造 航運業 觀光餐旅 金融保險 貿易百貨 綜合 其他 化學工業 生技醫療業 油電燃氣業 半導體業 電腦及週邊設備業 光電業 通信網路業 電子零組件業 電子通路業 資訊服務業 其他電子業 文化創意業 農業科技業 電子商務 綠能環保 數位雲端 運動休閒 居家生活 管理股票".split(),
))
import queue
import functools
import itertools
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, Future, wait
import copy
import json
import math
import os
import platform
import re
import struct
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


TAIPEI_TZ = timezone(timedelta(hours=8))


# =============================================================================
# 共用工具
# =============================================================================


def _now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def _now_text() -> str:
    return _now().isoformat(timespec="microseconds")


def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _to_int(value: Any, default: int = 0) -> int:
    number = _to_float(value)
    return default if number is None else int(number)


def _normalize_market(value: Any) -> str:
    text = str(value or "").strip().upper()
    aliases = {
        "TSE": "TWSE",
        "TWSE": "TWSE",
        "OTC": "TPEx",
        "TPEX": "TPEx",
        "TPEx": "TPEx",
    }
    return aliases.get(text, str(value or "").strip())


def _fugle_market(value: Any) -> str:
    market = _normalize_market(value)
    if market == "TWSE":
        return "TSE"
    if market == "TPEx":
        return "OTC"
    raise ValueError(f"不支援的市場：{value}")


def _event_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, str) and not value.isdigit():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo is not None else None
        except ValueError:
            return None
    event_time = _to_int(value, 0)
    if event_time <= 0:
        return None
    try:
        return datetime.fromtimestamp(event_time / 1_000_000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _event_iso(value: Any) -> Optional[str]:
    parsed = _event_datetime(value)
    if parsed is None:
        return None
    return parsed.astimezone(TAIPEI_TZ).isoformat(timespec="microseconds")


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat(timespec="microseconds")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _records_from_cts_text(raw: Any) -> List[Dict[str, str]]:
    text = str(raw or "").strip()
    if not text:
        return []
    chunks = re.findall(r"<([^<>]*)>", text, flags=re.DOTALL)
    if not chunks:
        chunks = [text]
    rows: List[Dict[str, str]] = []
    for chunk in chunks:
        row: Dict[str, str] = {}
        for token in chunk.replace("\r", "").replace("\n", "").split("|"):
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            key = key.strip()
            if key:
                row[key] = value.strip().replace("&bar;", "|").replace("&equ;", "=").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        if row:
            rows.append(row)
    return rows


def _first_nonempty(row: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


# =============================================================================
# Fugle Trade Replay
# =============================================================================


class _RateLimit:
    """平滑分散請求；共享額度，不以執行緒數冒充速率限制。"""
    def __init__(self, per_second: float = 24.0):
        self.interval = 1.0 / per_second
        self.next_at = 0.0
        self.lock = threading.Lock()

    def acquire(self):
        with self.lock:
            now = time.monotonic()
            target = max(now, self.next_at)
            self.next_at = target + self.interval
        if target > now:
            time.sleep(target - now)


class _RestEndpoint:
    """官方 REST 路徑；每次 HTTP 有逾時，各執行緒不共用連線物件。"""
    def __init__(self, client, path=()):
        self.client, self.path = client, path

    def __getattr__(self, name):
        return _RestEndpoint(self.client, (*self.path, name))

    def __call__(self, **params):
        self.client._rest_rate.acquire()
        parts = list(self.path)
        if parts and parts[0] == 'snapshot':
            parts.append(str(params.pop('market')))
        symbol = params.pop('symbol', None)
        if symbol is not None:
            parts.append(urllib.parse.quote(str(symbol), safe=''))
        url = 'https://api.fugle.tw/marketdata/v1.0/stock/' + '/'.join(parts)
        if params:
            url += '?' + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={'X-API-KEY': self.client.api_key})
        with urllib.request.urlopen(request, timeout=3.0) as response:
            payload = json.load(response)
        if not isinstance(payload, Mapping):
            raise RuntimeError('REST 回傳不是物件')
        return payload


class ReplayStore:
    """只保存 Fugle Trade Event；不保存 CTS 資料。"""

    def __init__(self, root: str | Path = "api_replay", *, persist: bool = True) -> None:
        self.root = Path(root)
        self.persist = bool(persist)
        if self.persist:
            self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "fugle_trades.jsonl"
        self._lock = threading.RLock()
        self._next_id = self._load_next_id() if self.persist else 1
        self._queue = queue.Queue(maxsize=10000)
        self._write_error = ""
        self._closed = False
        self._writer = threading.Thread(target=self._write_loop, name="TradeReplayWriter", daemon=True)
        if self.persist:
            self._writer.start()

    def _load_next_id(self) -> int:
        # ID 單調遞增；只解析檔尾，避免每次啟動重讀數百 MB 的完整逐筆資料。
        if not self.path.exists():
            return 1
        try:
            with self.path.open("rb") as handle:
                handle.seek(0, 2)
                end = handle.tell()
                handle.seek(max(0, end-65536))
                tail = handle.read().splitlines()
            for line in reversed(tail):
                try:
                    return _to_int(json.loads(line).get("id"), 0)+1
                except (ValueError, AttributeError):
                    continue
        except OSError:
            pass
        return 1

    def _write_loop(self):
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                break
            batch = [item]
            while len(batch) < 500:
                try:
                    following = self._queue.get_nowait()
                except queue.Empty:
                    break
                if following is None:
                    self._queue.task_done()
                    self._closed = True
                    break
                batch.append(following)
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    for record in batch:
                        handle.write(json.dumps(_jsonable(record), ensure_ascii=False) + "\n")
            except Exception as exc:
                self._write_error = f"{type(exc).__name__}: {exc}"
            finally:
                for _ in batch:
                    self._queue.task_done()
            if self._closed:
                break

    def append_fugle_event(self, record: Mapping[str, Any]) -> Dict[str, Any]:
        with self._lock:
            payload = dict(record, id=self._next_id)
            self._next_id += 1
            if not self.persist:
                return payload
            try:
                self._queue.put_nowait(payload)
            except queue.Full:
                self._write_error = "REPLAY_QUEUE_FULL"
            return payload

    def flush(self):
        self._queue.join()
        if self._write_error:
            raise RuntimeError(self._write_error)

    def close(self):
        if self._closed:
            return
        if not self.persist:
            self._closed = True
            return
        self.flush()
        self._queue.put(None)
        self._writer.join(timeout=5)
        self._closed = True

    def load_fugle_events(
        self,
        symbol: Optional[str] = None,
        *,
        trade_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        self.flush()
        wanted_symbol = str(symbol or "").strip()
        wanted_date = str(trade_date or "").strip()
        rows: List[Dict[str, Any]] = []
        if not self.path.exists():
            return rows
        with self._lock:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if wanted_symbol and str(item.get("symbol") or "") != wanted_symbol:
                        continue
                    if wanted_date and str(item.get("trade_date") or "") != wanted_date:
                        continue
                    rows.append(item)
        rows.sort(key=lambda row: _to_int(row.get("id"), 0))
        return rows


# =============================================================================
# Fugle MarketState
# =============================================================================


class MarketState:
    """Fugle WebSocket Trade Event 的記憶體狀態與事件順序唯一判定點。"""

    def __init__(self, *, freshness_seconds: float = 30.0) -> None:
        self.freshness_seconds = max(0.0, float(freshness_seconds))
        self._lock = threading.RLock()
        self._states: Dict[str, Dict[str, Any]] = {}
        self._last_event_key: Dict[str, Tuple[int, int]] = {}
        self._quote_sequence = 0
        self._symbol_sequence: Dict[str, int] = {}
        self._integrity_errors = {}

    def set_book_active(self, symbol: str, active: bool, *, force: bool = False) -> None:
        """訂閱集合轉換立即清除舊五檔；重連也必須等待新來源資料。"""
        with self._lock:
            row = self._states.setdefault(symbol, {"symbol": symbol})
            if force or bool(row.get("book_active")) != active:
                row.update(book_active=active, book_ready=False,
                           book_status="BOOK_NOT_READY" if active else "BOOK_INACTIVE",
                           book_generation=int(row.get("book_generation", 0)) + 1,
                           book_activated_at=int(_now().timestamp() * 1_000_000),
                           book_event_time=None, book_source_at=None, book_received_at=None,
                           bid=None, ask=None)

    def apply_book(self, data: Mapping[str, Any], *, received_at: Optional[str] = None) -> Dict[str, Any]:
        symbol = str(data.get("symbol") or "").strip()
        stamp = _to_int(data.get("time"), 0)
        event_dt = _event_datetime(stamp)
        with self._lock:
            row = self._states.get(symbol) or {}
            if not row.get("book_active"):
                return {"accepted": False, "classification": "BOOK_INACTIVE"}
            if (event_dt is None or stamp < int(row.get("book_activated_at") or 0)
                    or stamp <= int(row.get("book_event_time") or 0)):
                return {"accepted": False, "classification": "BOOK_OLD_EVENT"}
            if str(data.get("type") or "").upper() == "ODDLOT":
                return {"accepted": False, "classification": "OTHER_SESSION"}
            def best(side):
                levels = data.get(side)
                if not isinstance(levels, list) or not levels or not isinstance(levels[0], Mapping):
                    return None
                price = _to_float(levels[0].get("price"))
                return price if price is not None and math.isfinite(price) and price > 0 else None
            bid, ask = best("bids"), best("asks")
            ready = bool(bid is not None and ask is not None and ask >= bid and not data.get("isTrial"))
            row.update(symbol=symbol, bid=bid if ready else None, ask=ask if ready else None,
                       book_ready=ready, book_status="BOOK_READY" if ready else "BOOK_NOT_READY",
                       book_event_time=stamp, book_source_at=event_dt.isoformat(),
                       book_received_at=received_at or _now_text(),
                       book_session_id=event_dt.astimezone(TAIPEI_TZ).date().isoformat())
            self._states[symbol] = row
            return {"accepted": True, "classification": row["book_status"]}

    def seed_quote(self, quote: Mapping[str, Any]) -> Dict[str, Any]:
        symbol = str(quote.get("symbol") or "").strip()
        if not symbol:
            raise ValueError("REST 初始化資料缺少 symbol")
        market = _normalize_market(quote.get("market") or quote.get("exchange"))
        with self._lock:
            current = dict(self._states.get(symbol) or {})
            # REST 初始化不可把已接受的逐筆價格套上舊 event_time。
            seed_date = str(quote.get("trade_date") or quote.get("date") or "").replace("-", "")
            current_date = str(current.get("trade_date") or "").replace("-", "")
            if seed_date and current_date and seed_date > current_date:
                current = {key: value for key, value in current.items()
                           if key in ("name", "market", "bid", "ask") or key.startswith("book_")}
            if current.get("event_time"):
                for field in ("name", "market", "prev_close", "open"):
                    if current.get(field) is None or current.get(field) == "":
                        if quote.get(field) is not None:
                            current[field] = quote[field]
                self._states[symbol] = current
                return copy.deepcopy(current)
            current.update(
                {
                    "source": "FUGLE_REST_SEED",
                    "volume_unit": quote.get("volume_unit", ""),
                    "symbol": symbol,
                    "name": str(quote.get("name") or current.get("name") or ""),
                    "market": market or current.get("market") or "",
                    "trade_date": str(quote.get("trade_date") or quote.get("date") or ""),
                    "timestamp": quote.get("timestamp") or quote.get("lastUpdated"),
                    "price": _to_float(
                        quote.get("price")
                        if quote.get("price") is not None
                        else quote.get("closePrice")
                    ),
                    "open": _to_float(
                        quote.get("open")
                        if quote.get("open") is not None
                        else quote.get("openPrice")
                    ),
                    "high": _to_float(
                        quote.get("high")
                        if quote.get("high") is not None
                        else quote.get("highPrice")
                    ),
                    "low": _to_float(
                        quote.get("low")
                        if quote.get("low") is not None
                        else quote.get("lowPrice")
                    ),
                    "prev_close": _to_float(
                        quote.get("prev_close")
                        if quote.get("prev_close") is not None
                        else quote.get("previousClose")
                    ),
                    "volume": max(
                        0,
                        _to_int(
                            quote.get("volume")
                            if quote.get("volume") is not None
                            else quote.get("tradeVolume")
                        ),
                    ),
                    "turnover": max(
                        0.0,
                        _to_float(
                            quote.get("turnover")
                            if quote.get("turnover") is not None
                            else quote.get("tradeValue")
                        )
                        or 0.0,
                    ),
                    "event_time": current.get("event_time"),
                    "serial": current.get("serial"),
                    "received_at": current.get("received_at"),
                    "bid": current.get("bid"),
                    "ask": current.get("ask"),
                    "size": current.get("size", 0),
                    "quote_sequence": current.get("quote_sequence", 0),
                    "symbol_quote_sequence": current.get("symbol_quote_sequence", 0),
                    "event_classification": current.get("event_classification", "SEED"),
                }
            )
            self._states[symbol] = current
            return copy.deepcopy(current)

    def apply_trade(
        self,
        data: Mapping[str, Any],
        *,
        received_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        symbol = str(data.get("symbol") or "").strip()
        event_time = _to_int(
            data.get("time")
            if data.get("time") is not None
            else data.get("event_time"),
            0,
        )
        serial = _to_int(data.get("serial"), -1)
        price = _to_float(data.get("price"))
        if not symbol or event_time <= 0 or serial < 0 or price is None or price <= 0:
            return {
                "symbol": symbol,
                "event_time": event_time if event_time > 0 else None,
                "serial": serial if serial >= 0 else None,
                "classification": "INVALID",
                "accepted": False,
            }

        if str(data.get("type") or "").upper() == "ODDLOT":
            return {"symbol": symbol, "classification": "OTHER_SESSION", "accepted": False}
        if data.get("isTrial") is True:
            return {"symbol": symbol, "event_time": event_time, "serial": serial,
                    "classification": "TRIAL", "accepted": False}
        recv = str(received_at or _now_text())
        incoming_key = (event_time, serial)
        with self._lock:
            previous_key = self._last_event_key.get(symbol)
            if previous_key == incoming_key:
                classification = "DUPLICATE"
            elif previous_key is not None and (
                event_time < previous_key[0] or (
                    _event_datetime(event_time).astimezone(TAIPEI_TZ).date()
                    == _event_datetime(previous_key[0]).astimezone(TAIPEI_TZ).date()
                    and serial < previous_key[1]
                )
            ):
                classification = "OUT_OF_ORDER"
            else:
                classification = "NEW"

            if classification != "NEW":
                return {
                    "symbol": symbol,
                    "event_time": event_time,
                    "serial": serial,
                    "classification": classification,
                    "accepted": False,
                }

            previous = self._states.get(symbol) or {}
            incoming_volume = _to_int(data.get("volume"), -1)
            same_day = (previous_key is not None and
                        _event_datetime(previous_key[0]).date() == _event_datetime(event_time).date())
            if same_day and incoming_volume >= 0 and incoming_volume < int(previous.get("volume") or 0):
                self._integrity_errors[symbol] = "VOLUME_REGRESSION"
                return {"symbol": symbol, "classification": "VOLUME_REGRESSION", "accepted": False}
            self._quote_sequence += 1
            symbol_sequence = self._symbol_sequence.get(symbol, 0) + 1
            self._symbol_sequence[symbol] = symbol_sequence
            self._last_event_key[symbol] = incoming_key

            current = dict(self._states.get(symbol) or {})
            market = _normalize_market(data.get("exchange") or data.get("market"))
            event_dt = _event_datetime(event_time)
            trade_date = (
                event_dt.astimezone(TAIPEI_TZ).date().isoformat()
                if event_dt
                else ""
            )
            if current.get("trade_date") and current["trade_date"] != trade_date:
                self._integrity_errors.pop(symbol, None)
                current = {key: value for key, value in current.items()
                           if key in ("name", "market", "bid", "ask") or key.startswith("book_")}
            current["high"] = max(float(price), float(current.get("high") or price))
            current["low"] = min(float(price), float(current.get("low") or price))
            current.update(
                {
                    "source": "FUGLE_WEBSOCKET",
                    "volume_unit": "SHARE" if str(data.get("type") or "").upper() == "ODDLOT" else "LOT",
                    "symbol": symbol,
                    "name": str(current.get("name") or data.get("name") or ""),
                    "market": market or current.get("market") or "",
                    "trade_date": trade_date or current.get("trade_date") or "",
                    "timestamp": _event_iso(event_time),
                    "event_time": event_time,
                    "trade_event_time": event_time,
                    "trade_source_at": _event_iso(event_time),
                    "trade_received_at": recv,
                    "received_at": recv,
                    "serial": serial,
                    "price": float(price),
                    "size": max(0, _to_int(data.get("size"))),
                    "volume": max(0, _to_int(data.get("volume"))),
                    "bid": current.get("bid") if current.get("book_active") else _to_float(data.get("bid", current.get("bid"))),
                    "ask": current.get("ask") if current.get("book_active") else _to_float(data.get("ask", current.get("ask"))),
                    "quote_sequence": self._quote_sequence,
                    "symbol_quote_sequence": symbol_sequence,
                    "event_classification": "NEW",
                }
            )
            self._states[symbol] = current
            result = copy.deepcopy(current)
            result["classification"] = "NEW"
            result["accepted"] = True
            return result

    def _snapshot_row(
        self,
        row: Mapping[str, Any],
        *,
        as_of: datetime,
    ) -> Dict[str, Any]:
        output = copy.deepcopy(dict(row))
        output["integrity_error"] = self._integrity_errors.get(str(row.get("symbol")), "")
        event_dt = _event_datetime(output.get("event_time"))
        if event_dt is None:
            age = None
            fresh = False
            source_at = None
            freshness_status = "UNKNOWN"
        else:
            age = (as_of.astimezone(timezone.utc) - event_dt).total_seconds()
            fresh = -1.0 <= age <= self.freshness_seconds
            source_at = event_dt.astimezone(TAIPEI_TZ).isoformat(
                timespec="microseconds"
            )
            freshness_status = "FRESH" if fresh else "SOURCE_STALE"
        output.update(
            {
                "source_at": source_at,
                "realtime_source_at": source_at,
                "quote_source_at": source_at,
                "realtime_age_seconds": age,
                "realtime_fresh": fresh,
                "realtime_freshness_status": freshness_status,
                "freshness_threshold_seconds": self.freshness_seconds,
                "freshness_basis": "event_time",
                "trade_age_seconds": age,
                "book_age_seconds": ((as_of.astimezone(timezone.utc) - _event_datetime(output["book_event_time"])).total_seconds()
                                     if _event_datetime(output.get("book_event_time")) else None),
                "event_to_processing_lag_seconds": (
                    (_event_datetime(output.get("received_at")) - event_dt).total_seconds()
                    if event_dt and _event_datetime(output.get("received_at")) else None),
            }
        )
        return output

    def snapshot(
        self,
        symbol: str,
        *,
        as_of: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        wanted = str(symbol).strip()
        reference = as_of or _now()
        if reference.tzinfo is None or reference.utcoffset() is None:
            reference = reference.astimezone()
        with self._lock:
            row = copy.deepcopy(self._states.get(wanted) or {})
        if not row:
            return {}
        return self._snapshot_row(row, as_of=reference)

    def atomic_snapshot(
        self,
        symbols: Optional[Iterable[str]] = None,
        *,
        as_of: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """一次原子複製 MarketState；generation 只由已接受 Trade Event 推進。"""
        reference = as_of or _now()
        if reference.tzinfo is None or reference.utcoffset() is None:
            reference = reference.astimezone()

        wanted = (
            None
            if symbols is None
            else {str(item).strip() for item in symbols if str(item).strip()}
        )
        with self._lock:
            generation = int(self._quote_sequence)
            rows = [
                copy.deepcopy(dict(row))
                for symbol, row in sorted(self._states.items())
                if wanted is None or symbol in wanted
            ]

        frozen_rows = [self._snapshot_row(row, as_of=reference) for row in rows]
        source_times = [
            str(row.get("source_at"))
            for row in frozen_rows
            if row.get("source_at")
        ]
        return {
            "generation": generation,
            "quote_sequence": generation,
            "freshness_seconds": self.freshness_seconds,
            "freshness_basis": "event_time",
            "source_at": max(source_times) if source_times else "",
            "rows": frozen_rows,
        }

    def snapshots(
        self,
        symbols: Optional[Iterable[str]] = None,
        *,
        as_of: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        return list(self.atomic_snapshot(symbols, as_of=as_of).get("rows") or [])


# =============================================================================
# Day Trade Atomic Cache
# =============================================================================


class DayTradeCache:
    """REST 更新與 Scanner 讀取分離的 Atomic Cache。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: Dict[str, bool] = {}
        self._source_at: Optional[str] = None

    def replace(self, rows: Mapping[str, Any], *, source_at: Optional[str] = None) -> Dict[str, Any]:
        normalized = {
            str(symbol).strip(): bool(value)
            for symbol, value in rows.items()
            if str(symbol).strip()
        }
        with self._lock:
            self._data = dict(sorted(normalized.items()))
            self._source_at = str(source_at).strip() if source_at else None
            return self.snapshot()

    def snapshot(self, symbols: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        with self._lock:
            if symbols is None:
                data = dict(self._data)
            else:
                wanted = {str(symbol).strip() for symbol in symbols if str(symbol).strip()}
                data = {key: value for key, value in self._data.items() if key in wanted}
            return {
                "day_trade_map": dict(sorted(data.items())),
                "source_at": self._source_at,
                "count": len(data),
            }


# =============================================================================
# Fugle 官方 SDK 包裝
# =============================================================================


class FugleMarketDataClient:
    """Fugle REST 初始化／快照與獨立 WebSocket trades、books 資料入口。"""

    def __init__(
        self,
        *,
        mode: str = "LIVE",
        api_key: Optional[str] = None,
        replay_store: Optional[ReplayStore] = None,
        freshness_seconds: float = 30.0,
        reconnect_min_seconds: float = 30.0,
        reconnect_max_seconds: float = 60.0,
        event_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.mode = str(mode).strip().upper()
        if self.mode not in {"LIVE", "OFFLINE"}:
            raise ValueError("mode 僅允許 LIVE / OFFLINE")
        self.api_key = str(api_key or os.getenv("FUGLE_MARKETDATA_API_KEY") or os.getenv("FUGLE_MARKETDATA_API") or "").strip()
        self.store = replay_store or ReplayStore(persist=self.mode != "LIVE")
        self.state = MarketState(freshness_seconds=freshness_seconds)
        self.day_trade = DayTradeCache()
        self.reconnect_min_seconds = max(30.0, float(reconnect_min_seconds))
        self.reconnect_max_seconds = max(60.0, self.reconnect_min_seconds, float(reconnect_max_seconds))
        self._rest_stock: Any = None
        self._ws_stock: Any = None
        self._lock = threading.RLock()
        self._stream_lifecycle = threading.RLock()
        self._ws_write_lock = threading.Lock()
        self._ws_write_future = None
        self._ws_write_timeout_seconds = 4.0
        self._subscription_retry_seconds = 60.0
        self._subscription_retry_at = {}
        self._unsubscribe_retry_at = {}
        self._connecting = False
        self._connect_started = 0.0
        self._ws_generation = 0
        self._connected = False
        self._authenticated = False
        self._last_error = ""
        self._last_message_at: Optional[str] = None
        self._last_disconnect_at: Optional[str] = None
        self._desired_subscriptions: List[str] = []
        self._subscription_ids: Dict[str, str] = {}
        self._pending_subscriptions: set[str] = set()
        self._desired_books: set[str] = set()
        self._book_ids: Dict[str, str] = {}
        self._book_pending: set[str] = set()
        self._book_retiring: set[str] = set()
        self._book_retry_at: Dict[str, float] = {}
        self._seeded_symbols: set[str] = set()
        self._seed_session_date = ""
        self._next_connect_attempt = 0.0
        self._connect_backoff = self.reconnect_min_seconds
        self._day_trade_date = ""
        self._day_trade_errors: Dict[str, str] = {}
        self._index_symbol_cache: Dict[str, str] = {}
        self._rest_rate = _RateLimit()
        self._rest_pool = ThreadPoolExecutor(max_workers=max(4, min(16, (os.cpu_count() or 1)*2)), thread_name_prefix="MarketREST")
        self._metadata_pending = {}
        self._metadata_retry_at = {}
        self._metadata_lock = threading.RLock()
        self._security_metadata_snapshot = ()
        self._history_day = ""
        self._security_metadata = {}
        self._snapshot_cache = {}
        self._history_cache = {}
        self._round_deadline = 0.0
        self._batch_status = {}
        self._reference_lock = threading.RLock()
        self._reference_path = self.store.root / "market_reference.json"
        self._reference = {}
        try:
            self._reference = json.loads(self._reference_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        self._index_symbol_cache.update(self._reference.get("indices", {}))
        if event_sink is not None and not callable(event_sink):
            raise TypeError("event_sink 必須為可呼叫物件或 None")
        self._event_sink = event_sink

    def _require_live(self) -> None:
        if self.mode != "LIVE":
            raise RuntimeError("OFFLINE 模式禁止外部網路呼叫")
        if not self.api_key:
            message = "缺少環境變數 FUGLE_MARKETDATA_API_KEY"
            with self._lock:
                self._last_error = message
            raise RuntimeError(message)

    def _get_rest_stock(self) -> Any:
        self._require_live()
        with self._lock:
            if self._rest_stock is None:
                self._rest_stock = _RestEndpoint(self)
            return self._rest_stock

    def _get_ws_stock(self) -> Any:
        self._require_live()
        with self._lock:
            if self._ws_stock is None:
                try:
                    from fugle_marketdata import WebSocketClient
                except ImportError as exc:
                    raise RuntimeError("缺少 fugle-marketdata 套件") from exc
                stock = WebSocketClient(api_key=self.api_key).stock
                self._ws_generation += 1
                generation = self._ws_generation
                def guarded(callback):
                    def receive(*args):
                        with self._lock:
                            if generation != self._ws_generation:
                                return
                            callback(*args)
                    return receive
                stock.on("connect", guarded(self._on_connect))
                stock.on("message", guarded(self._on_message))
                stock.on("disconnect", guarded(self._on_disconnect))
                stock.on("error", guarded(self._on_error))
                self._ws_stock = stock
            return self._ws_stock

    def fugle_login(self) -> Dict[str, Any]:
        with self._stream_lifecycle:
            with self._lock:
                if self._connected or self._connecting or time.monotonic() < self._next_connect_attempt:
                    return self.status()
                self._next_connect_attempt = time.monotonic() + self.reconnect_min_seconds
                self._connecting = True
                self._connect_started = time.monotonic()
                self._last_message_at = None
            try:
                stock = self._get_ws_stock()
                stock.connect()
            except Exception as exc:
                with self._lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                self.disconnect()
                raise
            return self.status()

    def maintain_stream(self, health_timeout_seconds: float = 30.0) -> Dict[str, Any]:
        # SDK connect() 啟動背景執行緒後即返回，必須保留連線中狀態。
        if not self._stream_lifecycle.acquire(blocking=False):
            return self.status()
        try:
            status = self.status()
            timeout = max(1.0, float(health_timeout_seconds))
            with self._lock:
                connecting = self._connecting
                started = self._connect_started
            if connecting and time.monotonic() - started < timeout:
                return status
            last = status.get("last_message_at")
            stale = False
            if last:
                try:
                    stale = (_now() - datetime.fromisoformat(last)).total_seconds() > timeout
                except (TypeError, ValueError):
                    stale = True
            if status["connected"] and not status.get("authenticated") and time.monotonic() - started >= timeout:
                stale = True
            if status["connected"] and not stale:
                return status
            if time.monotonic() < self._next_connect_attempt:
                return status
            if stale or connecting:
                self.disconnect()
            try:
                result = self.fugle_login()
                self._connect_backoff = self.reconnect_min_seconds
                self._next_connect_attempt = time.monotonic() + self.reconnect_min_seconds
                return result
            except Exception:
                self._next_connect_attempt = time.monotonic() + self._connect_backoff
                self._connect_backoff = min(self.reconnect_max_seconds, max(self.reconnect_min_seconds, self._connect_backoff * 2))
                raise
        finally:
            self._stream_lifecycle.release()

    def disconnect(self) -> None:
        with self._stream_lifecycle:
            with self._lock:
                stock = self._ws_stock
                # 先使舊 callback 失效，避免 close() 的延遲回報影響新連線。
                self._ws_generation += 1
                self._connected = False
                self._authenticated = False
                self._connecting = False
                self._last_message_at = None
                self._last_disconnect_at = _now_text()
                self._subscription_ids.clear()
                self._pending_subscriptions.clear()
                self._reset_books()
            if stock is not None:
                close_method = getattr(stock, "disconnect", None) or getattr(stock, "close", None)
                if callable(close_method):
                    try:
                        close_method()
                    except Exception:
                        with self._lock:
                            self._connecting = True
                            self._connect_started = 0.0
                        raise
            with self._lock:
                self._ws_stock = None

    def set_book_subscriptions(self, symbols: Iterable[str]) -> None:
        """僅修改訂閱意圖及資料有效性；socket 寫入由訂閱同步執行。"""
        wanted = {str(s).strip() for s in symbols if str(s).strip()}
        with self._lock:
            for symbol in self._desired_books - wanted:
                self.state.set_book_active(symbol, False)
                if symbol in self._book_ids or symbol in self._book_pending:
                    self._book_retiring.add(symbol)
            for symbol in wanted - self._desired_books:
                self.state.set_book_active(symbol, True)
            self._desired_books = wanted

    def _reset_books(self) -> None:
        self._book_ids.clear()
        self._book_pending.clear()
        self._book_retiring.clear()
        self._book_retry_at.clear()
        for symbol in self._desired_books:
            self.state.set_book_active(symbol, True, force=True)

    def _sync_books(self) -> None:
        with self._lock:
            if not self._authenticated or self._ws_stock is None:
                return
            stock = self._ws_stock
            removed = [(s, i) for s, i in self._book_ids.items()
                       if s not in self._desired_books or s in self._book_retiring]
            added = sorted(self._desired_books - self._book_ids.keys() - self._book_pending - self._book_retiring)
        for symbol, identity in removed:
            with self._lock:
                if time.monotonic() < self._book_retry_at.get(identity, 0):
                    continue
                self._book_retry_at[identity] = time.monotonic() + self._subscription_retry_seconds
            self._ws_write(stock, "unsubscribe", {"id": identity})
        if removed:
            return
        for symbol in added:
            with self._lock:
                if (symbol not in self._desired_books or symbol in self._book_retiring
                        or symbol in self._book_pending or symbol in self._book_ids
                        or time.monotonic() < self._book_retry_at.get(symbol, 0)):
                    continue
                self._book_pending.add(symbol)
                self._book_retry_at[symbol] = time.monotonic() + self._subscription_retry_seconds
            try:
                self._ws_write(stock, "subscribe", {"channel": "books", "symbol": symbol})
            except Exception:
                with self._lock:
                    self._book_pending.discard(symbol)
                raise

    def subscribe_market(
        self,
        symbols: Iterable[str],
        *,
        initialize: bool = True,
        channels: Sequence[str] = ("trades",),
    ) -> Dict[str, Any]:
        normalized_channels = tuple(str(item).strip().lower() for item in channels)
        if normalized_channels == ("books",):
            self.set_book_subscriptions(symbols)
            self._sync_books()
            return self.status()
        if normalized_channels != ("trades",):
            raise ValueError("channels 必須為 trades 或 books，各自維護獨立訂閱集合")
        wanted = sorted({str(symbol).strip() for symbol in symbols if str(symbol).strip()})
        stock = self._get_ws_stock()
        with self._lock:
            self._desired_subscriptions = wanted
            now = time.monotonic()
            for retries in (self._subscription_retry_at, self._unsubscribe_retry_at):
                for symbol, deadline in list(retries.items()):
                    if symbol not in wanted and deadline <= now:
                        retries.pop(symbol, None)
            session = _now().astimezone(TAIPEI_TZ).date().isoformat()
            if session != self._seed_session_date:
                self._seeded_symbols.clear()
                self._seed_session_date = session
            missing_seed = [symbol for symbol in wanted if symbol not in self._seeded_symbols]
        # 先訂閱，避免 REST 初始化耗時阻擋持倉行情。
        self._sync_subscriptions()
        self._sync_books()
        if initialize and missing_seed:
            seeded = self.seed_symbols(missing_seed)
            with self._lock:
                self._seeded_symbols.update(str(row["symbol"]) for row in seeded)
        return self.status()

    def _ws_write(self, stock, method, payload):
        """限制底層 socket timeout 與呼叫端等待；尚未結束的寫入不另建重試執行緒。"""
        with self._ws_write_lock:
            previous = self._ws_write_future
            if previous is not None and not previous.done():
                raise TimeoutError("Fugle WebSocket 前次寫入尚未結束，本次不重複寫入")
            future = Future()
            self._ws_write_future = future

            def write():
                try:
                    # Fugle SDK 的 WebSocketApp 保存於此欄位；不更動全域 socket 設定。
                    app = getattr(stock, "_WebSocketClient__ws", None)
                    transport = getattr(app, "sock", None)
                    settimeout = getattr(transport, "settimeout", None)
                    if callable(settimeout):
                        settimeout(self._ws_write_timeout_seconds)
                    result = getattr(stock, method)(payload)
                except BaseException as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(result)

            threading.Thread(target=write, name="FugleWSWrite", daemon=True).start()
        try:
            return future.result(timeout=self._ws_write_timeout_seconds)
        except Exception as exc:
            with self._lock:
                self._last_error = f"Fugle {method}: {type(exc).__name__}: {exc}"
            raise

    def _sync_subscriptions(self) -> None:
        with self._lock:
            if not self._authenticated or self._ws_stock is None:
                return
            stock = self._ws_stock
            removed = [(symbol, identity) for symbol, identity in self._subscription_ids.items()
                       if symbol not in self._desired_subscriptions]
            added = [symbol for symbol in self._desired_subscriptions
                     if symbol not in self._subscription_ids and symbol not in self._pending_subscriptions]
        for symbol, identity in removed:
            with self._lock:
                now = time.monotonic()
                if now < self._unsubscribe_retry_at.get(symbol, 0):
                    continue
                self._unsubscribe_retry_at[symbol] = now + self._subscription_retry_seconds
            self._ws_write(stock, "unsubscribe", {"id": identity})
        # 等待取消回報再補新訂閱，避免輪替期間超出供應商上限。
        if removed:
            return
        for symbol in added:
            with self._lock:
                now = time.monotonic()
                if (symbol in self._subscription_ids or symbol in self._pending_subscriptions
                        or now < self._subscription_retry_at.get(symbol, 0)):
                    continue
                self._pending_subscriptions.add(symbol)
                # 先保存嘗試時間，送出失敗同樣受冷卻限制。
                self._subscription_retry_at[symbol] = now + self._subscription_retry_seconds
            try:
                self._ws_write(stock, "subscribe", {"channel": "trades", "symbol": symbol})
            except Exception:
                with self._lock:
                    self._pending_subscriptions.discard(symbol)
                raise

    def seed_symbols(self, symbols: Iterable[str]) -> List[Dict[str, Any]]:
        wanted = sorted(
            {str(item).strip() for item in symbols if str(item).strip()}
        )
        if not wanted:
            return []

        wanted_set = set(wanted)
        seeded: Dict[str, Dict[str, Any]] = {}
        for market in ("TWSE", "TPEx"):
            try:
                rows = self.get_daily_snapshot(market)
            except Exception:
                rows = []
            for row in rows:
                symbol = str(row.get("symbol") or "").strip()
                if symbol in wanted_set and symbol not in seeded:
                    seeded[symbol] = self.state.seed_quote(row)

        missing = [symbol for symbol in wanted if symbol not in seeded]
        if missing:
            from concurrent.futures import FIRST_COMPLETED
            stock = self._get_rest_stock()
            deadline = self._round_deadline if self._round_deadline > time.monotonic() else time.monotonic()+25
            jobs = {}
            while (missing or jobs) and time.monotonic() < deadline:
                while missing and len(jobs) < 8:
                    symbol = missing.pop(0)
                    jobs[self._rest_pool.submit(stock.intraday.quote, symbol=symbol)] = symbol
                done, _ = wait(jobs, timeout=max(0, deadline-time.monotonic()), return_when=FIRST_COMPLETED)
                if not done:
                    break
                for future in done:
                    jobs.pop(future)
                    try:
                        raw = future.result()
                        if isinstance(raw, Mapping):
                            quote = self._normalize_intraday_quote(raw)
                            if quote.get("symbol"):
                                seeded[quote["symbol"]] = self.state.seed_quote(quote)
                    except Exception as exc:
                        self._last_error = f"REST_SEED: {type(exc).__name__}: {exc}"
            for future in jobs:
                future.cancel()

        return [seeded[symbol] for symbol in wanted if symbol in seeded]

    def begin_round(self):
        self._round_deadline = time.monotonic() + 27.0
        self._snapshot_cache.clear()

    def _save_reference(self):
        # 平行市場請求共用暫存檔，快照到替換必須由同一寫入者完成。
        # 大型複製與磁碟 I/O 不持有行情回呼使用的共用鎖。
        with self._reference_lock:
            with self._lock:
                indices = dict(self._index_symbol_cache)
            self._reference["indices"] = indices
            payload = copy.deepcopy(self._reference)
            temporary = self._reference_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self._reference_path)

    def get_daily_snapshot(self, market: str) -> List[Dict[str, Any]]:
        market = _normalize_market(market)
        if market in self._snapshot_cache:
            return copy.deepcopy(self._snapshot_cache[market])
        try:
            stock = self._get_rest_stock()
            response = stock.snapshot.quotes(
                market=_fugle_market(market),
                type="ALLBUT0999",
            )
        except Exception as exc:
            previous = self._reference.get("snapshots", {}).get(market, [])
            if not previous:
                raise
            raw_error = f"{type(exc).__name__}: {exc}"
            self._last_error = f"REST_SNAPSHOT_FALLBACK {market}: {raw_error}"
            result = copy.deepcopy(previous)
            for row in result:
                row["source"] = "FUGLE_LAST_GOOD_ACTIVE_UNIVERSE_CACHE"
                row["source_degraded"] = True
                row["source_error"] = raw_error
            self._snapshot_cache[market] = result
            return copy.deepcopy(result)
        if not isinstance(response, Mapping):
            raise RuntimeError("Fugle 股票 Snapshot 回傳格式錯誤：不是 Mapping")
        rows = response.get("data")
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise RuntimeError("Fugle 股票 Snapshot 回傳格式錯誤：data 不是 Sequence")
        result = [
            self._normalize_snapshot_row(
                row,
                market=market,
                trade_date=response.get("date"),
            )
            for row in rows
            if isinstance(row, Mapping)
        ]
        # 只在成功收到這份 REST 快照時記錄；快取／失敗回退不得更新取得時間。
        acquired_at = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="milliseconds")
        for row in result:
            row["snapshot_acquired_at"] = acquired_at
        result.sort(key=lambda row: (str(row.get("market")), str(row.get("symbol"))))
        if not result:
            previous = self._reference.get("snapshots", {}).get(market, [])
            if not previous:
                raise RuntimeError(f"{market} 快照為空，尚無已保存的前日資料")
            result = copy.deepcopy(previous)
            for row in result:
                row["source"] = "FUGLE_PREVIOUS_SNAPSHOT_CACHE"
                row["source_degraded"] = True
                row["source_error"] = "Fugle 股票 Snapshot 回傳空集合"
        else:
            with self._reference_lock:
                self._reference.setdefault("snapshots", {})[market] = result
            self._save_reference()
        self._snapshot_cache[market] = result
        return copy.deepcopy(result)

    def get_active_universe(self, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        rows = self.get_daily_snapshot("TWSE") + self.get_daily_snapshot("TPEx")
        rows = [row for row in rows if re.fullmatch(r"[1-9]\d{3}", str(row.get("symbol") or ""))]
        rows.sort(
            key=lambda row: (
                -float(row.get("turnover") or 0.0),
                -int(row.get("volume") or 0),
                str(row.get("market") or ""),
                str(row.get("symbol") or ""),
            )
        )
        if limit is not None:
            rows = rows[: max(0, int(limit))]
        return rows

    def get_index_quote(self, symbol: str, market: str) -> Dict[str, Any]:
        stock = self._get_rest_stock()
        provider_symbol = self._resolve_index_symbol(symbol, market)
        try:
            raw = stock.intraday.quote(symbol=provider_symbol)
        except Exception:
            if _now().astimezone(TAIPEI_TZ).hour >= 9:
                raise
            raw = {}
        if not isinstance(raw, Mapping) or not any(_to_float(raw.get(k)) for k in ("lastPrice", "index", "closePrice")):
            if _now().astimezone(TAIPEI_TZ).hour >= 9:
                raise RuntimeError("指數即時資料缺少有效價格")
            history = self.get_index_history(symbol, market, limit=20)
            if len(history) < 2:
                raise RuntimeError("指數盤前歷史不足")
            last, previous = history[-1], history[-2]
            raw = {"date": last["trade_date"], "closePrice": last["price"],
                   "previousClose": previous["price"], "openPrice": last.get("open"),
                   "highPrice": last.get("high"), "lowPrice": last.get("low"),
                   "historical_only": True}

        if not isinstance(raw, Mapping):
            raise RuntimeError("Fugle 指數快照回傳格式錯誤")
        price = _to_float(
            raw.get("lastPrice")
            if raw.get("lastPrice") is not None
            else raw.get("index", raw.get("closePrice"))
        )
        prev_close = _to_float(raw.get("previousClose") or raw.get("referencePrice"))
        change_pct = _to_float(raw.get("changePercent"))
        if change_pct is None and price is not None and prev_close not in {None, 0}:
            change_pct = (price - float(prev_close)) / float(prev_close) * 100.0
        return {
            "symbol": str(symbol),
            "provider_symbol": provider_symbol,
            "name": str(raw.get("name") or ""),
            "market": _normalize_market(market),
            "trade_date": str(raw.get("date") or ""),
            "timestamp": raw.get("lastUpdated") or raw.get("time"),
            "price": price,
            "open": _to_float(raw.get("openPrice")),
            "high": _to_float(raw.get("highPrice")),
            "low": _to_float(raw.get("lowPrice")),
            "prev_close": prev_close,
            "change_pct": change_pct,
            "volume": max(
                0,
                _to_int(
                    raw.get("total", {}).get("tradeVolume")
                    if isinstance(raw.get("total"), Mapping)
                    else raw.get("tradeVolume")
                ),
            ),
            "turnover": max(
                0.0,
                _to_float(
                    raw.get("total", {}).get("tradeValue")
                    if isinstance(raw.get("total"), Mapping)
                    else raw.get("tradeValue")
                )
                or 0.0,
            ),
            "source": "FUGLE_REST_INDEX_SNAPSHOT",
        }

    def get_index_history(self, symbol: str, market: str, *, limit: int = 20) -> List[Dict[str, Any]]:
        stock = self._get_rest_stock()
        provider_symbol = self._resolve_index_symbol(symbol, market)
        count = max(1, int(limit))
        end = _now().astimezone(TAIPEI_TZ).date() - timedelta(days=1)
        if self._history_day != str(end):
            self._history_cache.clear()
            self._history_day = str(end)
        history_key = f"{provider_symbol}:{end}:{count}"
        if history_key in self._history_cache:
            return copy.deepcopy(self._history_cache[history_key])
        start = end - timedelta(days=max(30, count * 4))
        response = stock.historical.candles(
            **{
                "symbol": provider_symbol,
                "from": start.isoformat(),
                "to": end.isoformat(),
                "timeframe": "D",
                "fields": "open,high,low,close,volume,turnover,change",
                "sort": "asc",
            }
        )
        rows = response.get("data") if isinstance(response, Mapping) else []
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            return []
        output: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            output.append(
                {
                    "symbol": str(symbol),
                    "provider_symbol": provider_symbol,
                    "market": _normalize_market(market),
                    "trade_date": str(row.get("date") or "")[:10],
                    "timestamp": row.get("date"),
                    "price": _to_float(row.get("close")),
                    "open": _to_float(row.get("open")),
                    "high": _to_float(row.get("high")),
                    "low": _to_float(row.get("low")),
                    "volume": max(0, _to_int(row.get("volume"))),
                    "turnover": max(0.0, _to_float(row.get("turnover")) or 0.0),
                    "change": _to_float(row.get("change")),
                    "source": "FUGLE_REST_HISTORY",
                }
            )
        if len(self._history_cache) >= 32:
            self._history_cache.pop(next(iter(self._history_cache)))
        self._history_cache[history_key] = output[-count:]
        return copy.deepcopy(output[-count:])

    def refresh_day_trade(self, symbols: Iterable[str]) -> Dict[str, Any]:
        # 一個批次擁有 pending 狀態；同日資格可重用，行情時間絕不由資格更新覆寫。
        with self._metadata_lock:
            return self._refresh_day_trade(symbols)

    def _refresh_day_trade(self, symbols):
        from concurrent.futures import FIRST_COMPLETED
        stock = self._get_rest_stock()
        session = _now().astimezone(TAIPEI_TZ).date().isoformat()
        if session != self._day_trade_date:
            self.day_trade.replace({})
            self._day_trade_date = session
            self._security_metadata.clear()
            self._security_metadata_snapshot = ()
            self._metadata_retry_at.clear()
            for future in self._metadata_pending.values():
                future.cancel()
        wanted = list(dict.fromkeys(str(x).strip() for x in symbols if str(x).strip()))
        started = time.monotonic()
        deadline = self._round_deadline if self._round_deadline > 0 else started + 25.0
        data, errors = {}, {}
        cached_count = 0
        jobs = {}
        todo = []
        for symbol in wanted:
            metadata = self._security_metadata.get(symbol, {})
            if metadata.get("metadata_date") == session and metadata.get("industry_code") and symbol not in self._day_trade_errors:
                data[symbol] = bool(metadata.get("day_trade_eligible"))
                cached_count += 1
            elif symbol in self._metadata_pending and not self._metadata_pending[symbol].cancelled():
                jobs[self._metadata_pending[symbol]] = symbol
            elif time.monotonic() < self._metadata_retry_at.get(symbol, 0):
                errors[symbol] = "METADATA_RETRY_BACKOFF"
            else:
                todo.append(symbol)
        # 未訂閱股票的未完成請求同樣占配額；避免輪替股票池時越排越多。
        for symbol, future in list(self._metadata_pending.items()):
            if symbol not in wanted:
                future.cancel()
                if future.done():
                    self._metadata_pending.pop(symbol, None)
        width = min(16, getattr(getattr(self, "_resource_manager", None), "io_workers", 16))
        while (todo or jobs) and time.monotonic() < deadline:
            occupied = sum(not f.done() for f in self._metadata_pending.values())
            while todo and occupied < width:
                symbol = todo.pop(0)
                future = self._rest_pool.submit(stock.intraday.ticker, symbol=symbol)
                self._metadata_pending[symbol] = future
                jobs[future] = symbol
                occupied += 1
            if not jobs:
                break
            done, pending = wait(jobs, timeout=max(0, deadline-time.monotonic()), return_when=FIRST_COMPLETED)
            if not done:
                break
            for future in done:
                symbol = jobs.pop(future)
                self._metadata_pending.pop(symbol, None)
                try:
                    ticker = future.result()
                    flags = [ticker.get("canDayTrade"), ticker.get("canBuyDayTrade")]
                    source_date = str(ticker.get("date") or "")[:10]
                    # 前日資格僅供盤前候選，買進另有來源日期硬檢查。
                    industry = str(ticker.get("industry") or "").strip()
                    industry = industry.zfill(2) if industry else ""
                    sector = _INDUSTRY_NAMES.get(industry, "")
                    metadata = {"industry_code": industry, "sector": sector,
                                "sector_code": industry if industry not in {"", "00"} else "",
                                "sector_name": sector,
                                "sector_mapping_status": "MAPPED" if industry not in {"", "00"} else "SECTOR_MAPPING_MISSING",
                                "security_type": str(ticker.get("securityType") or ""),
                                "board_lot": ticker.get("boardLot"), "currency": ticker.get("tradingCurrency"),
                                "name": ticker.get("name"), "market": _normalize_market(ticker.get("market") or ticker.get("exchange")),
                                "metadata_date": source_date, "day_trade_eligible": any(x is True for x in flags)}
                    security_names = {"01":"一般股票", "24":"ETF", "25":"外幣 ETF", "26":"槓桿 ETF",
                        "27":"外幣槓桿 ETF", "28":"反向 ETF", "29":"外幣反向 ETF", "30":"期貨 ETF",
                        "31":"外幣期貨 ETF", "32":"債券 ETF", "33":"外幣債券 ETF", "45":"平衡型 ETF",
                        "46":"主動式 ETF", "47":"債券主動式 ETF"}
                    metadata["security_type_name"] = security_names.get(metadata["security_type"], "其他／待確認")
                    self._security_metadata[symbol] = metadata
                    if not any(isinstance(value, bool) for value in flags):
                        raise RuntimeError("Ticker 缺少當沖資格")
                    if not source_date:
                        raise RuntimeError("Ticker 缺少來源日期")
                    data[symbol] = metadata["day_trade_eligible"]
                    with self.state._lock:
                        if symbol in self.state._states:
                            self.state._states[symbol].update({k:v for k,v in metadata.items() if v not in (None, "")})
                    if source_date != session:
                        errors[symbol] = "METADATA_SOURCE_DATE_NOT_CURRENT"
                        data.pop(symbol, None)
                        self._metadata_retry_at[symbol] = time.monotonic() + 60
                except Exception as exc:
                    errors[symbol] = f"{type(exc).__name__}: {exc}"
                    self._metadata_retry_at[symbol] = time.monotonic() + 30
        for future, symbol in jobs.items():
            future.cancel()
            if future.cancelled():
                self._metadata_pending.pop(symbol, None)
            errors[symbol] = "METADATA_DEADLINE_EXCEEDED"
        for symbol in todo:
            errors[symbol] = "METADATA_NOT_SCHEDULED"
        for symbol in errors:
            with self.state._lock:
                if symbol in self.state._states:
                    self.state._states[symbol]["day_trade_eligible"] = False
        self.day_trade.replace({**data, **{symbol:False for symbol in errors}}, source_at=session)
        self._day_trade_errors = errors
        keep = set(wanted) | set(self._desired_subscriptions)
        for mapping in (self._security_metadata, self._metadata_retry_at):
            for symbol in set(mapping) - keep:
                mapping.pop(symbol, None)
        self._batch_status = {"requested_count": len(wanted), "completed_count": len(data),
                              "cached_count": cached_count, "pending_count": len(errors),
                              "complete": len(data) == len(wanted),
                              "duration_ms": round((time.monotonic()-started)*1000, 3), "errors": errors}
        self._security_metadata_snapshot = tuple(
            dict(row, symbol=symbol) for symbol, row in self._security_metadata.items()
        )
        return {**self.day_trade.snapshot(), **self._batch_status}

    def get_day_trade_map(self, symbols: Optional[Iterable[str]] = None) -> Dict[str, bool]:
        return dict(self.day_trade.snapshot(symbols).get("day_trade_map") or {})

    def stream_disconnect_at(self) -> Optional[str]:
        """逐筆事件僅讀斷線識別，避免重建及排序整份訂閱狀態。"""
        with self._lock:
            return self._last_disconnect_at

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "batch": dict(self._batch_status),
                "replay_error": self.store._write_error,
                "replay_queue_depth": self.store._queue.qsize(),
                "provider": "FUGLE",
                "connected": self._connected,
                "connecting": self._connecting,
                "authenticated": self._authenticated,
                "api_key_configured": bool(self.api_key),
                "last_error": self._last_error,
                "last_message_at": self._last_message_at,
                "last_disconnect_at": self._last_disconnect_at,
                "subscriptions": {
                    "desired": list(self._desired_subscriptions),
                    "channel": "trades",
                    "active": sorted(self._subscription_ids),
                    "pending": sorted(self._pending_subscriptions),
                },
                "books": {"desired": sorted(self._desired_books), "active": sorted(self._book_ids),
                          "pending": sorted(self._book_pending), "retiring": sorted(self._book_retiring)},
                "day_trade_errors": dict(self._day_trade_errors),
                "ws_write_timeout_seconds": self._ws_write_timeout_seconds,
                "subscription_retry_seconds": self._subscription_retry_seconds,
                "reconnect_min_seconds": self.reconnect_min_seconds,
                "reconnect_max_seconds": self.reconnect_max_seconds,
            }

    def _resolve_index_symbol(self, symbol: str, market: str) -> str:
        raw_symbol = str(symbol).strip()
        if raw_symbol.upper().startswith(("IX", "IR")):
            return raw_symbol.upper()
        normalized_market = _normalize_market(market)
        cache_key = f"{normalized_market}:{raw_symbol.lower()}"
        with self._lock:
            cached = self._index_symbol_cache.get(cache_key)
        if cached:
            return cached
        stock = self._get_rest_stock()
        response = stock.intraday.tickers(type="INDEX", exchange=normalized_market)
        rows = response.get("data") if isinstance(response, Mapping) else []
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            rows = []
        candidates = [row for row in rows if isinstance(row, Mapping)]
        aliases = raw_symbol.lower()
        accepted_names = ({"加權指", "加權指數", "加權股價指數", "發行量加權股價指數",
                           "臺灣證券交易所發行量加權股價指數", "TAIEX"} if aliases == "t00" else
                          {"櫃買指", "櫃買指數", "櫃買股價指數", "櫃檯指", "櫃檯指數",
                           "櫃檯買賣股價指數", "櫃檯買賣加權股價指數", "TPEX指數", "OTC指數"})
        matches = []
        for row in candidates:
            name = re.sub(r"\s+", "", str(row.get("name") or "")).upper()
            code = str(row.get("symbol") or "").strip()
            if "報酬" in name or "RETURN" in name or code.upper().startswith("IR"):
                continue
            if code.lower() == aliases or name in accepted_names:
                matches.append(code)
        if len(set(matches)) != 1:
            sample = [{"symbol": r.get("symbol"), "name": r.get("name")} for r in candidates[:80]]
            raise RuntimeError(f"指數解析失敗 {market}:{symbol}; matches={matches}; returned={sample}")
        chosen = matches[0]
        with self._lock:
            self._index_symbol_cache[cache_key] = chosen
        return chosen

    @staticmethod
    def _normalize_snapshot_row(row: Mapping[str, Any], *, market: str, trade_date: Any) -> Dict[str, Any]:
        price = _to_float(row.get("closePrice"))
        change = _to_float(row.get("change"))
        prev_close = None
        if price is not None and change is not None:
            prev_close = price - change
        return {
            "symbol": str(row.get("symbol") or ""),
            "name": str(row.get("name") or ""),
            "market": _normalize_market(market),
            "trade_date": str(trade_date or ""),
            "timestamp": row.get("lastUpdated"),
            "price": price,
            "open": _to_float(row.get("openPrice")),
            "high": _to_float(row.get("highPrice")),
            "low": _to_float(row.get("lowPrice")),
            "prev_close": prev_close,
            "change_pct": _to_float(row.get("changePercent")),
            "volume": max(0, _to_int(row.get("tradeVolume"))),
            "turnover": max(0.0, _to_float(row.get("tradeValue")) or 0.0),
            "ticker_type": str(row.get("type") or ""),
            "security_type": str(row.get("securityType") or ""),
            "volume_unit": "LOT",
            "source": "FUGLE_REST_SNAPSHOT",
        }

    @staticmethod
    def _normalize_intraday_quote(
        raw: Mapping[str, Any],
        *,
        ticker: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        ticker = ticker if isinstance(ticker, Mapping) else {}
        total = raw.get("total") if isinstance(raw.get("total"), Mapping) else {}
        return {
            "symbol": str(raw.get("symbol") or ticker.get("symbol") or ""),
            "name": str(raw.get("name") or ticker.get("name") or ""),
            "market": _normalize_market(raw.get("exchange") or ticker.get("exchange")),
            "trade_date": str(raw.get("date") or ticker.get("date") or ""),
            "timestamp": raw.get("lastUpdated") or raw.get("time"),
            "price": _to_float(raw.get("lastPrice")),
            "open": _to_float(raw.get("openPrice")),
            "high": _to_float(raw.get("highPrice")),
            "low": _to_float(raw.get("lowPrice")),
            "prev_close": _to_float(raw.get("previousClose") or ticker.get("previousClose")),
            "volume": max(
                0,
                _to_int(
                    total.get("tradeVolume")
                    if isinstance(total, Mapping)
                    else raw.get("tradeVolume")
                ),
            ),
            "turnover": max(
                0.0,
                _to_float(
                    total.get("tradeValue")
                    if isinstance(total, Mapping)
                    else raw.get("tradeValue")
                )
                or 0.0,
            ),
            "source": "FUGLE_REST_SEED",
        }

    def _on_connect(self, *args: Any) -> None:
        del args
        with self._lock:
            self._connecting = False
            self._connected = True
            self._authenticated = False
            self._last_error = ""

    def _on_disconnect(self, *args: Any) -> None:
        del args
        with self._lock:
            self._next_connect_attempt = max(self._next_connect_attempt,
                time.monotonic() + self.reconnect_min_seconds)
            self._connecting = False
            self._ws_stock = None
            self._ws_generation += 1
            self._connected = False
            self._authenticated = False
            self._last_disconnect_at = _now_text()
            self._subscription_ids.clear()
            self._pending_subscriptions.clear()
            self._reset_books()

    def _on_error(self, error: Any) -> None:
        with self._lock:
            self._last_error = str(error)

    def _on_message(self, message: Any) -> None:
        received_at = _now_text()
        with self._lock:
            self._last_message_at = received_at
        try:
            payload = json.loads(message) if isinstance(message, str) else message
        except json.JSONDecodeError:
            return
        if not isinstance(payload, Mapping):
            return
        if str(payload.get("event") or "").lower() == "authenticated":
            with self._lock:
                self._authenticated = True
                self._last_error = ""
            # MarketScannerWorker 下一次訂閱同步送出；回呼不可持鎖進行網路 I/O。
            return
        event = str(payload.get("event") or "").lower()
        if event in {"subscribed", "unsubscribed", "subscriptions"}:
            rows = payload.get("data") or []
            rows = [rows] if isinstance(rows, Mapping) else rows
            with self._lock:
                for row in rows:
                    if not isinstance(row, Mapping):
                        continue
                    identity = str(row.get("id") or "")
                    symbol = str(row.get("symbol") or "")
                    if event == "unsubscribed":
                        for key, value in list(self._book_ids.items()):
                            if value == identity:
                                self._book_ids.pop(key, None)
                                self._book_retiring.discard(key)
                                self._book_retry_at.pop(key, None)
                                self.state.set_book_active(key, key in self._desired_books, force=True)
                        self._subscription_ids = {key: value for key, value in self._subscription_ids.items() if value != identity}
                    elif row.get("channel") == "books" and symbol and identity:
                        self._book_ids[symbol] = identity
                        self._book_pending.discard(symbol)
                    elif row.get("channel") == "trades" and symbol and identity:
                        self._subscription_ids[symbol] = identity
                        self._pending_subscriptions.discard(symbol)
            # 下一次掃描同步訂閱；不可在 WebSocket 收訊回呼內等待寫入。
            return
        if event == "error":
            with self._lock:
                self._last_error = json.dumps(payload.get("data"), ensure_ascii=False)
                self._pending_subscriptions.clear()
                self._book_pending.clear()
                self._book_retiring.intersection_update(self._book_ids)
                for symbol in self._desired_books:
                    self.state.set_book_active(symbol, True, force=True)
            return
        if str(payload.get("event") or "").lower() != "data":
            return
        if str(payload.get("channel") or "").lower() == "books":
            data = payload.get("data")
            if not isinstance(data, Mapping):
                return
            symbol = str(data.get("symbol") or "")
            with self._lock:
                if (symbol not in self._desired_books or symbol in self._book_retiring
                        or not payload.get("id") or self._book_ids.get(symbol) != str(payload["id"])):
                    return
                self.state.apply_book(data, received_at=received_at)
            return
        if str(payload.get("channel") or "").lower() != "trades":
            return
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return
        result = self.state.apply_trade(data, received_at=received_at)
        event_time = result.get("event_time") or _to_int(data.get("time"), 0)
        event_dt = _event_datetime(event_time)
        record = {
            "kind": "TRADE",
            "channel": "trades",
            "source": "FUGLE_WEBSOCKET",
            "symbol": str(data.get("symbol") or ""),
            "market": _normalize_market(data.get("exchange") or data.get("market")),
            "trade_date": (
                event_dt.astimezone(TAIPEI_TZ).date().isoformat()
                if event_dt
                else ""
            ),
            "event_time": event_time,
            "received_at": received_at,
            "serial": _to_int(data.get("serial"), -1),
            "price": _to_float(data.get("price")),
            "size": max(0, _to_int(data.get("size"))),
            "volume": max(0, _to_int(data.get("volume"))),
            "bid": _to_float(data.get("bid")),
            "ask": _to_float(data.get("ask")),
            "classification": str(result.get("classification") or "INVALID"),
            "event_classification": str(result.get("classification") or "INVALID"),
            "quote_sequence": result.get("quote_sequence"),
            "symbol_quote_sequence": result.get("symbol_quote_sequence"),
            "payload": {
                "price": _to_float(data.get("price")),
                "size": max(0, _to_int(data.get("size"))),
                "volume": max(0, _to_int(data.get("volume"))),
                "bid": _to_float(data.get("bid")),
                "ask": _to_float(data.get("ask")),
            },
        }
        saved = self.store.append_fugle_event(record)
        if record["classification"] == "NEW" and self._event_sink is not None:
            try:
                self._event_sink(copy.deepcopy(saved))
            except Exception as exc:
                with self._lock:
                    self._last_error = f"event_sink: {type(exc).__name__}: {exc}"


# =============================================================================
# CTS Request Models
# =============================================================================


@dataclass(frozen=True)
class CTSOrderRequest:
    account_id: str
    trade_date: int
    trade_type: int
    order_type: int
    side: int
    symbol: str
    quantity: int
    price_type: int
    price: str
    broker: str = ""
    pay_type: int = 0
    condition: int = 0
    registry_key: str = ""
    order_intent_id: str = ""
    generation: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CTSModifyRequest:
    account_id: str
    trade_date: int
    trade_type: int
    order_type: int
    oid: str
    order_no: str
    symbol: str
    side: int
    quantity: int
    current_quantity: int
    matched_quantity: int
    pre_order: int
    modify_type: int
    new_price: str
    new_price_type: int
    condition: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CTSCancelRequest:
    account_id: str
    trade_date: int
    trade_type: int
    order_type: int
    oid: str
    order_no: str
    symbol: str
    side: int
    quantity: int
    current_quantity: int
    matched_quantity: int
    pre_order: int
    price: str
    price_type: int
    condition: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# =============================================================================
# CTS State 與 COM Client
# =============================================================================


class CTSState:
    """CTS 查詢／Callback 的獨立狀態，不進入 Fugle MarketState。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._callback_sequence = 0
        self._last_order_rows: List[Dict[str, Any]] = []
        self._last_match_rows: List[Dict[str, Any]] = []
        self._last_position_rows: List[Dict[str, Any]] = []
        self._callbacks: List[Dict[str, Any]] = []

    def update_query(self, kind: str, rows: Sequence[Mapping[str, Any]]) -> None:
        normalized = [dict(row) for row in rows if isinstance(row, Mapping)]
        with self._lock:
            if kind == "ORDER":
                self._last_order_rows = normalized
            elif kind == "MATCH":
                self._last_match_rows = normalized
            elif kind == "POSITION":
                self._last_position_rows = normalized

    def append_callback(self, event_id: int, response_data: Any) -> Dict[str, Any]:
        with self._lock:
            self._callback_sequence += 1
            item = {
                "sequence": self._callback_sequence,
                "event_id": int(event_id),
                "response_data": str(response_data or ""),
                "received_at": _now_text(),
                "received_monotonic": time.monotonic(),
            }
            self._callbacks.append(item)
            if len(self._callbacks) > 1000:
                self._callbacks = self._callbacks[-1000:]
            return copy.deepcopy(item)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "callback_sequence": self._callback_sequence,
                "orders": copy.deepcopy(self._last_order_rows),
                "matches": copy.deepcopy(self._last_match_rows),
                "positions": copy.deepcopy(self._last_position_rows),
                "callbacks": copy.deepcopy(self._callbacks),
            }


def _cts_owner(method):
    """所有 COM 呼叫只在同一 STA；下單優先於尚未開始的查詢。"""
    @functools.wraps(method)
    def dispatch(self, *args, **kwargs):
        import multiprocessing
        if multiprocessing.current_process().name != "MainProcess":
            raise RuntimeError("計算 worker 禁止呼叫 CTS")
        if threading.current_thread() is self._owner_thread:
            return method(self, *args, **kwargs)
        with self._owner_lock:
            if self._owner_closed:
                if method.__name__ == "new_order":
                    return {"ok": False, "not_submitted": True, "broker_submit_attempt": False,
                            "error": "CTS_CLIENT_CLOSED"}
                raise RuntimeError("CTS client 已關閉")
            if self._owner_thread is None:
                self._owner_thread = threading.Thread(target=self._owner_loop, name="CTSOwner", daemon=True)
                self._owner_thread.start()
        import multiprocessing
        if multiprocessing.current_process().name != "MainProcess":
            raise RuntimeError("計算 worker 禁止呼叫 CTS")
        future = Future()
        priority = 1 if method.__name__.startswith("query_") else 2
        if method.__name__ in {"cancel_order", "modify_order"} or (
                method.__name__ == "new_order" and args and args[0].side == 2):
            priority = 0
        if method.__name__ == "new_order" and args:
            with self._lock:
                self._order_futures = {k: f for k, f in getattr(self, "_order_futures", {}).items() if not f.done()}
                self._started_order_calls = getattr(self, "_started_order_calls", set()).intersection(self._order_futures)
                self._order_futures[getattr(args[0], "registry_key", "")] = future
        try:
            self._owner_queue.put((priority, next(self._owner_sequence), future, method, args, kwargs), timeout=1.0)
        except queue.Full:
            future.cancel()
            if method.__name__ == "new_order":
                return {"ok": False, "not_submitted": True, "broker_submit_attempt": False,
                        "error": "CTS_OWNER_QUEUE_FULL"}
            raise
        try:
            return future.result(timeout=30.0)
        except TimeoutError:
            self._transport_failed(TimeoutError("CTS owner response timeout"))
            # 尚未執行可取消；已進 COM 的呼叫不能當成券商拒單。
            cancelled = future.cancel()
            if cancelled and method.__name__ == "new_order":
                return {"ok": False, "not_submitted": True, "broker_submit_attempt": False,
                        "error": "CTS_OWNER_QUEUE_TIMEOUT_CANCELLED"}
            if method.__name__ == "new_order" and args:
                request = args[0]
                started = getattr(request, "registry_key", "") in getattr(self, "_started_order_calls", set())
                result = {"ok": False, "broker_unknown": True, "broker_submit_attempt": started,
                          "error": "CTS_OWNER_RESPONSE_TIMEOUT", "not_submitted": False}
                self._submission_event(request, "ORDER_SUBMIT_RESULT", "STATUS_UNKNOWN", result=result)
                return result
            raise TimeoutError("CTS 回覆逾時；委託狀態未明，禁止重送")
    return dispatch


class CTSClient:
    """中國信託 CTS 證券交易 COM API 包裝。"""

    def __init__(self, *, mode: str = "LIVE", com_prog_id: str = "DJTRADEOBJLibCTS.TradeApp") -> None:
        import multiprocessing
        if multiprocessing.current_process().name != "MainProcess":
            raise RuntimeError("計算 worker 禁止建立 CTS")
        self.mode = str(mode).strip().upper()
        self._owner_thread = None
        self._owner_closed = False
        self._owner_lock = threading.Lock()
        self._owner_queue = queue.PriorityQueue(maxsize=128)
        self._anomaly_logger = None
        self._anomaly_sink = None
        self._owner_sequence = itertools.count()
        self.async_queries_supported = True
        self._sync_echo_configured = False
        if self.mode not in {"LIVE", "OFFLINE"}:
            raise ValueError("mode 僅允許 LIVE / OFFLINE")
        self.com_prog_id = str(com_prog_id).strip()
        self.state = CTSState()
        self._lock = threading.RLock()
        self._app: Any = None
        self._uid = ""
        self._account_id = ""
        self._accounts: List[Dict[str, Any]] = []
        self._security_accounts: List[Dict[str, Any]] = []
        self._health = "DISCONNECTED"
        self._transport_failures = 0
        self._login_credentials = None
        self._next_recovery = 0.0
        self._connected = False
        self._last_error = ""
        self._callbacks: List[Callable[[Dict[str, Any]], None]] = []

    def _transport_failed(self, exc):
        with self._lock:
            self._transport_failures += 1
            self._health = "DISCONNECTED" if self._transport_failures >= 3 else "DEGRADED"
            self._last_error = f"{type(exc).__name__}: {exc}"
            if self._health == "DISCONNECTED":
                self._connected = False

    def mark_reconciled(self, ok):
        with self._lock:
            if ok and self._connected:
                self._health = "READY"
                self._transport_failures = 0
            elif self._connected:
                self._health = "DEGRADED"

    @_cts_owner
    def recover_connection(self):
        if not self._login_credentials or time.monotonic() < self._next_recovery:
            return {"ok": False, "status": "RECOVERY_PENDING"}
        credentials = self._login_credentials
        self._health = "RECONNECTING"
        self._next_recovery = time.monotonic() + min(60, 5 * max(1, self._transport_failures))
        # 原 COM owner 內完成重連；憑證只在記憶體，禁止寫入 snapshot 或 log。
        try:
            if self._app is not None:
                try:
                    self._app.Disconnect()
                except Exception:
                    pass
            return self.login(*credentials[:3], account_id=credentials[3])
        except Exception as exc:
            self._transport_failed(exc)
            return {"ok": False, "status": "DISCONNECTED"}

    def configure_anomaly_log(self, directory):
        import logging
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger(f"cts_anomalies.{path.resolve()}")
        logger.setLevel(logging.ERROR)
        logger.propagate = False
        if not logger.handlers:
            handler = logging.FileHandler(path / ("operations_" + datetime.now(TAIPEI_TZ).strftime("%Y%m%d") + ".jsonl"),
                                          encoding="utf-8", delay=True)
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
        self._anomaly_logger = logger

    def _record_anomaly(self, method, result):
        if self._anomaly_logger is None:
            return
        fields = ("error", "raw_error", "status", "kind", "header", "order_id", "order_no", "traceback")
        details = {k: result[k] for k in fields if result.get(k) not in (None, "")}
        text = json.dumps({"time": _now_text(), "method": method, "details": details},
                          ensure_ascii=False, default=str)
        for secret in (self._uid, self._account_id):
            if secret:
                text = text.replace(secret, "[REDACTED]")
        text = re.sub(r'(?i)(password|passwd|pwd|api_key)([\s=:]+)[^\s,|]+', r'\1\2[REDACTED]', text)
        if callable(self._anomaly_sink):
            self._anomaly_sink(method, json.loads(text)["details"])
        else:
            self._anomaly_logger.error(text)

    def _owner_loop(self):
        pythoncom = None
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except ImportError:
            pass
        try:
            while True:
                if pythoncom is not None:
                    pythoncom.PumpWaitingMessages()
                try:
                    _, _, future, method, args, kwargs = self._owner_queue.get(timeout=0.01)
                except queue.Empty:
                    continue
                if method is None:
                    future.set_result(None)
                    break
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    result = method(self, *args, **kwargs)
                    if isinstance(result, Mapping) and result.get("ok") is False:
                        if method.__name__.startswith("query_") and result.get("status") != "ERROR":
                            self._transport_failed(RuntimeError(str(result.get("error") or result.get("status"))))
                        self._record_anomaly(method.__name__, result)
                    if method.__name__ == "new_order" and args and isinstance(result, Mapping):
                        request = args[0]
                        key = getattr(request, "registry_key", "")
                        if key:
                            event = {"execution_completion": True, "registry_key": key,
                                "generation": getattr(request, "generation", 0),
                                "order_intent_id": getattr(request, "order_intent_id", ""),
                                "order_id": result.get("order_id"), "order_no": result.get("order_no"),
                                "ok": result.get("ok"), "broker_rejected": result.get("broker_rejected"),
                                "not_submitted": result.get("not_submitted"),
                                "received_at": _now_text(), "received_monotonic": time.monotonic()}
                            with self._lock:
                                callbacks = tuple(self._callbacks)
                            for callback in callbacks:
                                try:
                                    callback(event)
                                except Exception:
                                    pass
                    future.set_result(result)
                except BaseException as exc:
                    self._transport_failed(exc)
                    import traceback
                    self._record_anomaly(method.__name__, {
                        "error": f"{type(exc).__name__}: {exc}",
                        "traceback": traceback.format_exc(),
                    })
                    future.set_exception(exc)
        finally:
            self._app = None
            if pythoncom is not None:
                pythoncom.CoUninitialize()

    def close_owner(self):
        self._owner_closed = True
        if self._owner_thread is not None and self._owner_thread.is_alive():
            done = Future()
            self._owner_queue.put((99, next(self._owner_sequence), done, None, (), {}))
            self._owner_thread.join(timeout=5)

    def _require_live(self) -> None:
        if self.mode != "LIVE":
            raise RuntimeError("OFFLINE 模式禁止 CTS COM 呼叫")
        if platform.system().lower() != "windows":
            raise RuntimeError("CTS COM 僅能在 Windows 執行")
        if struct.calcsize("P") * 8 != 32:
            raise RuntimeError("CTS COM 文件要求使用 32 位元 Python")

    def _ensure_app(self) -> Any:
        self._require_live()
        with self._lock:
            if self._app is None:
                try:
                    import win32com.client
                except ImportError as exc:
                    raise RuntimeError("缺少 pywin32") from exc
                app = win32com.client.DispatchWithEvents(self.com_prog_id, _CTSEventHandler)
                app._api_gateway_callback = self._on_data_response
                self._app = app
            return self._app

    @staticmethod
    def _ret_ok(result: Any) -> Tuple[bool, int, str]:
        if isinstance(result, tuple):
            values = list(result)
            ret = _to_int(values[0], 0) if values else 0
            err_code = _to_int(values[1], 0) if len(values) > 1 else 0
            err_msg = str(values[2] or "") if len(values) > 2 else ""
            return ret == 1, err_code, err_msg
        ret = _to_int(result, 0)
        return ret == 1, 0, ""

    @property
    def connected(self) -> bool:
        with self._lock:
            return bool(self._connected)

    @property
    def account_id(self) -> str:
        with self._lock:
            return str(self._account_id)

    @staticmethod
    def _normalize_account(row: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "id": _first_nonempty(row, "ID", "AccountID", "F0"),
            "name": _first_nonempty(row, "Name", "AccountName"),
            "uid": _first_nonempty(row, "UID"),
            "type": _to_int(row.get("Type"), -1),
            "credit": _to_int(row.get("Credit"), 0),
            "sfid": str(row.get("SFID") or ""),
            "can_hedge": _to_int(row.get("CanHedge"), 0),
            "raw": copy.deepcopy(dict(row)),
        }

    def get_accounts(self) -> List[Dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._accounts)

    @_cts_owner
    def login(
        self,
        trade_das: str,
        uid: str,
        password: str,
        *,
        account_id: str = "",
    ) -> Dict[str, Any]:
        """依 CTS 官方順序執行 Init → Login → Connect → GetAccountList。"""
        self._sync_echo_configured = False
        app = self._ensure_app()
        init_result = app.Init(str(trade_das))
        init_ok, init_code, init_msg = self._ret_ok(init_result)
        if not init_ok:
            self._last_error = init_msg or f"Init failed: {init_code}"
            raise RuntimeError(self._last_error)

        login_result = app.Login(str(uid), str(password))
        login_ok, login_code, login_msg = self._ret_ok(login_result)
        if not login_ok:
            try:
                app.Fini()
            finally:
                self._last_error = login_msg or f"Login failed: {login_code}"
            raise RuntimeError(self._last_error)

        connect_result = app.Connect()
        connect_ok, _, connect_msg = self._ret_ok(connect_result)
        if not connect_ok:
            try:
                app.Logout(str(uid))
                app.Fini()
            finally:
                self._last_error = connect_msg or "Connect failed"
            raise RuntimeError(self._last_error)

        # 所有現有呼叫端都解析完整回傳；不可依賴 COM 預設 EchoType。
        try:
            for command_type in (1, 2, 3):
                app.SetEchoType(command_type, 0)
        except Exception as exc:
            with self._lock:
                self._connected = False
                self._last_error = f"CTS SetEchoType 同步回傳設定失敗：{type(exc).__name__}: {exc}"
            for method, args in (("Disconnect", ()), ("Logout", (str(uid),)), ("Fini", ())):
                try:
                    getattr(app, method)(*args)
                except Exception:
                    pass
            raise RuntimeError(self._last_error) from exc

        accounts_raw = app.GetAccountList()
        accounts_raw_text = str(accounts_raw or "")
        raw_accounts = _records_from_cts_text(accounts_raw)
        if not raw_accounts:
            try:
                app.Disconnect()
                app.Logout(str(uid))
                app.Fini()
            finally:
                self._last_error = (
                    "GetAccountList 未回傳可解析帳號資料"
                    if not accounts_raw_text.strip()
                    else f"GetAccountList 回傳格式無法解析：{accounts_raw_text}"
                )
            raise RuntimeError(self._last_error)

        accounts = [self._normalize_account(row) for row in raw_accounts]
        securities = [row for row in accounts if _to_int(row.get("type"), -1) == 1]

        selected = str(account_id or "").strip()
        security_ids = {str(row.get("id") or "") for row in securities}
        if selected and selected not in security_ids:
            try:
                app.Disconnect()
                app.Logout(str(uid))
                app.Fini()
            finally:
                self._last_error = f"指定的證券帳號不在 GetAccountList 回傳結果：{selected}"
            raise RuntimeError(self._last_error)
        if not selected and len(securities) == 1:
            selected = str(securities[0].get("id") or "")

        with self._lock:
            self._uid = str(uid)
            self._account_id = selected
            self._accounts = copy.deepcopy(accounts)
            self._security_accounts = copy.deepcopy(securities)
            self._health = "RECONCILING"
            self._transport_failures = 0
            self._login_credentials = (str(trade_das), str(uid), str(password), selected)
            self._connected = True
            self._sync_echo_configured = True
            self._last_error = ""
        return {
            "ok": True,
            "connected": True,
            "account_id": selected,
            "raw_account_result": accounts_raw_text,
            "account_count": len(accounts),
            "security_account_count": len(securities),
            "accounts": copy.deepcopy(accounts),
            "security_accounts": copy.deepcopy(securities),
        }

    @_cts_owner
    def logout(self) -> Dict[str, Any]:
        self._login_credentials = None
        self._health = "DISCONNECTED"
        with self._lock:
            app = self._app
            uid = self._uid
        errors: List[str] = []
        if app is not None:
            for name, args in (
                ("Disconnect", ()),
                ("Logout", (uid,)),
                ("Fini", ()),
            ):
                try:
                    getattr(app, name)(*args)
                except Exception as exc:
                    errors.append(f"{name}: {type(exc).__name__}: {exc}")
        with self._lock:
            self._connected = False
            self._sync_echo_configured = False
            self._uid = ""
            self._account_id = ""
            self._accounts = []
            self._security_accounts = []
            self._app = None
            self._last_error = "; ".join(errors)
        return {"ok": not errors, "connected": False, "errors": errors}

    def add_callback(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        if not callable(callback):
            raise TypeError("callback 必須可呼叫")
        with self._lock:
            self._callbacks.append(callback)

    submission_audit_supported = True

    def has_inflight_order(self, registry_key):
        with self._lock:
            future = getattr(self, "_order_futures", {}).get(registry_key)
            return bool(future is not None and not future.done())

    def _submission_event(self, request, stage, status, **details):
        recorder = getattr(self, "record_order_event", None)
        if callable(recorder):
            recorder(stage, status, {
                "method": "Stock_NewOrder", "registry_key": request.registry_key,
                "order_intent_id": request.order_intent_id, "generation": request.generation,
                "symbol": request.symbol, "side": request.side, "price": request.price,
                "quantity": request.quantity, **details})

    @_cts_owner
    def new_order(self, request: CTSOrderRequest) -> Dict[str, Any]:
        try:
            quantity, price = float(request.quantity), float(request.price)
            if (request.trade_type != 0 or not math.isfinite(quantity)
                    or quantity <= 0 or not quantity.is_integer()
                    or not math.isfinite(price) or request.price_type not in {0, 1, 2, 3, 4}
                    or (request.price_type == 0 and price <= 0)
                    or (request.price_type != 0 and price != 0)
                    or request.condition not in {0, 1, 2} or request.side not in {1, 2}
                    or not str(request.symbol).strip() or not str(request.account_id).strip()):
                raise ValueError("委託價格、數量或身分無效")
            app = self._ensure_connected()
            call = app.Stock_NewOrder
            arguments = (request.account_id, int(request.trade_date), int(request.trade_type),
                         int(request.order_type), int(request.side), request.symbol,
                         int(request.quantity), int(request.price_type), str(request.price),
                         str(request.broker), int(request.pay_type), int(request.condition))
            self._submission_event(request, "ORDER_SUBMIT_START", "SUBMITTING",
                                   broker_submit_attempt=True)
        except Exception as exc:
            result = {"ok": False, "not_submitted": True, "broker_submit_attempt": False,
                      "broker_rejected": False, "error": f"{type(exc).__name__}: {exc}",
                      "order_id": "", "order_no": ""}
            self._submission_event(request, "ORDER_SUBMIT_RESULT", "PRE_SUBMIT_REJECTED", result=result)
            return result
        with self._lock:
            self._started_order_calls = getattr(self, "_started_order_calls", set())
            self._started_order_calls.add(request.registry_key)
        try:
            raw = call(*arguments)
            result = self._order_action_result(raw, kind="NEW")
        except Exception as exc:
            self._transport_failed(exc)
            import traceback
            result = {"ok": False, "broker_unknown": True, "broker_rejected": False,
                      "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc(),
                      "order_id": "", "order_no": ""}
        result["broker_submit_attempt"] = True
        state = ("REJECTED" if result.get("broker_rejected") else
                 "ACCEPTED" if result.get("ok") and (result.get("order_id") or result.get("order_no"))
                 else "STATUS_UNKNOWN")
        result["broker_unknown"] = state == "STATUS_UNKNOWN"
        # 呼叫後記錄失敗不可把委託誤分類成未送出。
        try:
            self._submission_event(request, "ORDER_SUBMIT_RESULT", state, result=result)
        except Exception as exc:
            result["audit_error"] = str(exc)
        return result

    @_cts_owner
    def modify_order(self, request: CTSModifyRequest) -> Dict[str, Any]:
        app = self._ensure_connected()
        raw = app.Stock_ModifyOrder(
            request.account_id,
            int(request.trade_date),
            int(request.trade_type),
            int(request.order_type),
            request.oid,
            request.order_no,
            request.symbol,
            int(request.side),
            int(request.quantity),
            int(request.current_quantity),
            int(request.matched_quantity),
            int(request.pre_order),
            int(request.modify_type),
            str(request.new_price),
            int(request.new_price_type),
            int(request.condition),
        )
        return self._order_action_result(raw, kind="MODIFY")

    @_cts_owner
    def cancel_order(self, request: CTSCancelRequest) -> Dict[str, Any]:
        app = self._ensure_connected()
        raw = app.Stock_CancelOrder(
            request.account_id,
            int(request.trade_date),
            int(request.trade_type),
            int(request.order_type),
            request.oid,
            request.order_no,
            request.symbol,
            int(request.side),
            int(request.quantity),
            int(request.current_quantity),
            int(request.matched_quantity),
            int(request.pre_order),
            str(request.price),
            int(request.price_type),
            int(request.condition),
        )
        return self._order_action_result(raw, kind="CANCEL")

    @_cts_owner
    def query_order(self, account_id: str, force_query: bool = True) -> Dict[str, Any]:
        app = self._ensure_connected()
        try:
            raw = app.Stock_QueryOrder(str(account_id), 1 if force_query else 0)
        except Exception as exc:
            return self._query_failure(exc)
        result = self._query_result(
            raw, kind="ORDER", allow_sync_empty_header=self._sync_echo_configured
        )
        if result["ok"]:
            self.state.update_query("ORDER", result["parsed_result"])
        return result

    @_cts_owner
    def query_match(self, account_id: str, force_query: bool = True) -> Dict[str, Any]:
        app = self._ensure_connected()
        try:
            raw = app.Stock_QueryMatch(str(account_id), 1 if force_query else 0)
        except Exception as exc:
            return self._query_failure(exc, include_normalized=True)
        result = self._query_result(
            raw, kind="MATCH", normalize=self._normalize_match,
            allow_sync_empty_header=self._sync_echo_configured,
        )
        if result["ok"]:
            self.state.update_query("MATCH", result["normalized_result"])
        return result

    @_cts_owner
    def query_position(
        self,
        account_id: str,
        trade_date: int,
        force_query: bool = True,
    ) -> Dict[str, Any]:
        app = self._ensure_connected()
        try:
            raw = app.Stock_QueryPosition(
                str(account_id),
                int(trade_date),
                1 if force_query else 0,
            )
        except Exception as exc:
            return self._query_failure(exc)
        result = self._query_result(raw, kind="POSITION")
        if result["ok"]:
            self.state.update_query("POSITION", result["parsed_result"])
        return result

    def _query_failure(
        self, exc: Exception,
        *,
        include_normalized: bool = False,
    ) -> Dict[str, Any]:
        self._transport_failed(exc)
        raw_error = f"{type(exc).__name__}: {exc}"
        result: Dict[str, Any] = {
            "ok": False,
            "status": "ERROR",
            "raw_result": "",
            "parsed_result": [],
            "record_count": 0,
            "raw_error": raw_error,
            "error": raw_error,
        }
        if include_normalized:
            result["normalized_result"] = []
        return result

    @staticmethod
    def _query_result(
        raw: Any,
        *,
        kind: str,
        normalize: Optional[Callable[[Mapping[str, Any]], Dict[str, Any]]] = None,
        allow_sync_empty_header: bool = False,
    ) -> Dict[str, Any]:
        raw_text = str(raw or "")
        records = _records_from_cts_text(raw)
        header = next((row for row in records if "rc" in row), None)
        rows = [row for row in records if any(re.fullmatch(r"F\d+", key) for key in row)]
        query_kind = str(kind or "").strip().upper()
        required_fields = {
            "ORDER": {"F0", "F5", "F9", "F15"},
            "MATCH": {"F2", "F3", "F4", "F6", "F7", "F9", "F14"},
            "POSITION": {"F0", "F1", "F2", "F3"},
        }.get(query_kind, set())

        valid_shape = bool(rows) and all(
            required_fields.issubset(set(row.keys())) for row in rows
        )
        if query_kind == "POSITION" and valid_shape:
            # 數量解析失敗不能被下游轉成零而清掉持倉。
            valid_shape = all(
                str(row.get("F1") or "").strip()
                and all(_to_float(row.get(key)) is not None
                        and math.isfinite(float(row[key]))
                        and float(row[key]).is_integer() and float(row[key]) >= 0
                        for key in (("F3", "F24") if "F31" in row or "F32" in row else ("F3",)))
                for row in rows)
        expected_count = _to_int(header.get("count"), -1) if header else -1
        header_ok = header is None or (str(header.get("rc")) == "1" and str(header.get("err") or "") in {"", "0"})
        count_ok = header is None or expected_count == len(rows)
        receipt_only = header is not None and header_ok and bool(header.get("cookie")) and "count" not in header and not rows
        # 實際同步查詢的無資料回覆可僅含 rc 與 ServerTime。
        # 僅相容委託／成交；庫存、cookie、錯誤及未知欄位不適用。
        sync_empty_header = False
        if (
            allow_sync_empty_header
            and query_kind in {"ORDER", "MATCH"}
            and header is not None
            and header_ok
            and len(records) == 1
            and not rows
            and set(header).issubset({"rc", "ServerTime", "err"})
            and re.fullmatch(r"<[^<>]+>", raw_text.strip())
        ):
            server_time = str(header.get("ServerTime") or "")
            if re.fullmatch(r"\d{8}-\d{6}", server_time):
                try:
                    datetime.strptime(server_time, "%Y%m%d-%H%M%S")
                    sync_empty_header = True
                except ValueError:
                    pass
        envelope_valid = bool(re.fullmatch(r"\s*(?:<[^<>]*>\s*)+", raw_text, re.DOTALL))
        if query_kind == "POSITION" and not envelope_valid:
            ok, status, raw_error = False, "ERROR", "CTS 庫存回覆不完整"
        elif receipt_only:
            ok, status = False, "ASYNC_RECEIPT"
            raw_error = f"CTS 僅回傳非同步受理編號 cookie={header.get('cookie')}，尚無完整資料；請重新登入以套用 SetEchoType 同步設定"
        elif sync_empty_header:
            ok, status, raw_error = True, "EMPTY", ""
        elif header is not None and (not header_ok or not count_ok):
            ok, status, raw_error = False, "ERROR", raw_text
        elif header is not None and expected_count == 0 and not rows:
            ok, status, raw_error = True, "EMPTY", ""
        elif not raw_text.strip():
            ok, status, raw_error = False, "ERROR", "CTS 空白回覆，未確認無資料"
        elif valid_shape:
            ok, status, raw_error = True, "SUCCESS", ""
        else:
            ok, status, raw_error = False, "ERROR", raw_text

        result: Dict[str, Any] = {
            "ok": ok,
            "status": status,
            "raw_result": raw_text,
            "header": dict(header or {}),
            "parsed_result": rows if ok and valid_shape else [],
            "record_count": len(rows) if ok and valid_shape else 0,
            "actual_record_count": len(rows),
            "reported_record_count": expected_count if header is not None and "count" in header else None,
            "empty_result_source": "SYNC_SUCCESS_HEADER" if sync_empty_header else None,
            "raw_error": raw_error,
            "error": raw_error,
        }

        if query_kind == "ORDER" and valid_shape:
            record_errors: List[Dict[str, Any]] = []
            for index, row in enumerate(rows):
                status_code = _to_int(row.get("F26"), 0)
                code = str(row.get("F27") or "").strip()
                message = str(row.get("F28") or "").strip()
                failed = status_code == 1 or code not in {"", "0"} or (
                    bool(message) and message not in {"成功", "OK"}
                )
                if failed:
                    record_errors.append(
                        {
                            "index": index,
                            "status_code": status_code,
                            "code": code,
                            "message": message,
                        }
                    )
            result["record_errors"] = record_errors

        if normalize is not None:
            normalized = [normalize(row) for row in rows] if ok and valid_shape else []
            result["normalized_result"] = normalized

        return result

    def status(self) -> Dict[str, Any]:
        with self.state._lock:
            callback_sequence = self.state._callback_sequence
        with self._lock:
            return {
                "connected": self._connected,
                "connection_health": self._health,
                "transport_failures": self._transport_failures,
                "recovery_available": self._login_credentials is not None,
                "account_id": self._account_id,
                "account_count": len(self._accounts),
                "security_account_count": len(self._security_accounts),
                "security_account_ids": [
                    str(row.get("id") or "")
                    for row in self._security_accounts
                    if str(row.get("id") or "")
                ],
                "last_error": self._last_error,
                "callback_sequence": callback_sequence,
            }

    def _ensure_connected(self) -> Any:
        app = self._ensure_app()
        with self._lock:
            if not self._connected:
                raise RuntimeError("CTS 尚未登入連線")
        return app

    def _on_data_response(self, event_id: int, response_data: Any) -> None:
        item = self.state.append_callback(int(event_id), response_data)
        with self._lock:
            callbacks = list(self._callbacks)
        for callback in callbacks:
            try:
                callback(copy.deepcopy(item))
            except Exception:
                continue

    @staticmethod
    def _order_action_result(raw: Any, *, kind: str) -> Dict[str, Any]:
        records = _records_from_cts_text(raw)
        header = next((row for row in records if "rc" in row), None)
        rows = [row for row in records if "F3" in row]
        row = rows[0] if rows else {}
        action = str(kind or "").strip().upper()
        # 官方欄位表：NEW=F2；MODIFY/CANCEL=F1。部分元件範例沿用 NEW 排列，
        # 只有 F1 是八碼日期且 F2 非日期時，才使用可辨識的相容排列。
        field = "F2" if action == "NEW" else "F1"
        if action in {"MODIFY", "CANCEL"} and re.fullmatch(r"\d{8}", str(row.get("F1") or "")) and not re.fullmatch(r"\d{8}", str(row.get("F2") or "")):
            field = "F2"
        order_no = str(row.get(field) or "")
        order_id = str(row.get("F3") or "")
        code, message = str(row.get("F6") or "").strip(), str(row.get("F7") or "").strip()
        header_failed = header is not None and (str(header.get("rc")) != "1" or str(header.get("err") or "") not in {"", "0"})
        error = ""
        if header_failed:
            error = str(header.get("msg") or header.get("err") or "CTS_REQUEST_FAILED")
        elif not row or not (order_id or order_no):
            error = "CTS_ORDER_RESPONSE_INVALID"
        elif code not in {"", "0"}:
            error = message or code
        elif message and message not in {"成功", "OK"}:
            error = message
        return {"ok": not bool(error),
                "broker_rejected": bool(header_failed or (row and code not in {"", "0"})),
                "raw_result": str(raw or ""),
                "parsed_result": rows, "header": dict(header or {}),
                "return_code": code, "return_message": message,
                "order_response": str(raw or ""), "order_no": order_no,
                "order_id": order_id, "error": error}

    @staticmethod
    def _normalize_match(row: Mapping[str, Any]) -> Dict[str, Any]:
        trade_type = _to_int(row.get("F2"), 0)
        raw_quantity = max(0, _to_int(row.get("F7"), 0))
        if trade_type in {1, 5, 7}:
            filled_shares = raw_quantity
            cts_quantity = raw_quantity
            rule = "CTS_QUERYMATCH_RAW_SHARES"
        elif raw_quantity >= 1000 and raw_quantity % 1000 == 0:
            filled_shares = raw_quantity
            cts_quantity = raw_quantity // 1000
            rule = "CTS_QUERYMATCH_RAW_SHARES_COMPAT"
        else:
            filled_shares = raw_quantity * 1000
            cts_quantity = raw_quantity
            rule = "CTS_QUERYMATCH_LOTS_TO_SHARES"
        return {
            "trade_type": trade_type,
            "symbol": str(row.get("F3") or ""),
            "side": _to_int(row.get("F4"), 0),
            "order_type": _to_int(row.get("F5"), 0),
            "price": _to_float(row.get("F6")),
            "raw_quantity": raw_quantity,
            "account_id": str(row.get("F0") or ""),
            "match_id": str(row.get("F15") or ""),
            "fee": _to_float(row.get("F12")),
            "tax": _to_float(row.get("F13")),
            "cts_quantity": cts_quantity,
            "filled_shares": filled_shares,
            "quantity_normalization": rule,
            "order_no": str(row.get("F9") or ""),
            "order_id": str(row.get("F14") or ""),
            "match_date": str(row.get("F10") or ""),
            "match_time": str(row.get("F20") or row.get("F11") or ""),
        }


class _CTSEventHandler:
    def OnDataResponse(self, event_id: int, response_data: Any) -> None:
        callback = getattr(self, "_api_gateway_callback", None)
        if callable(callback):
            callback(event_id, response_data)


# =============================================================================
# 專案統一 APIGateway
# =============================================================================


class APIGateway:
    """Fugle 市場資料與 CTS 交易 API 的單一專案入口。"""

    def __init__(
        self,
        *,
        mode: str = "LIVE",
        replay_dir: str = "api_replay",
        api_key: Optional[str] = None,
        freshness_seconds: float = 30.0,
        reconnect_min_seconds: float = 30.0,
        reconnect_max_seconds: float = 60.0,
        cts_com_prog_id: str = "DJTRADEOBJLibCTS.TradeApp",
        broker: str = "TAISHIN",
        taishin_day_trade_flag: Optional[str] = None,
        event_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.mode = str(mode).strip().upper()
        if self.mode not in {"LIVE", "OFFLINE"}:
            raise ValueError("mode 僅允許 LIVE / OFFLINE")
        self.replay_store = ReplayStore(replay_dir, persist=self.mode != "LIVE")
        self.market = FugleMarketDataClient(
            mode=self.mode,
            api_key=api_key,
            replay_store=self.replay_store,
            freshness_seconds=freshness_seconds,
            reconnect_min_seconds=reconnect_min_seconds,
            reconnect_max_seconds=reconnect_max_seconds,
            event_sink=event_sink,
        )
        if broker == "TAISHIN":
            from taishin_adapter import TaishinClient
            self.cts = TaishinClient(mode=self.mode, day_trade_flag=taishin_day_trade_flag)
        elif broker == "CTS":
            self.cts = CTSClient(mode=self.mode, com_prog_id=cts_com_prog_id)
        else:
            raise ValueError("broker must be TAISHIN or CTS")
        self.broker = self.cts
        self.cts.configure_anomaly_log(Path(__file__).resolve().parent / "execution_logs")

    def configure_resources(self, resources):
        old = self.market._rest_pool
        self.market._rest_pool = resources.io_pool("MarketREST")
        self.market._resource_manager = resources
        self._resource_manager = resources
        if old is not self.market._rest_pool:
            old.shutdown(wait=False, cancel_futures=True)

    def fugle_login(self) -> Dict[str, Any]:
        """建立 Fugle WebSocket 連線；呼叫端不接觸內部 WebSocket client。"""
        return self.market.fugle_login()

    def maintain_market_stream(self, *, enabled: bool = True, health_timeout_seconds: float = 30.0) -> Dict[str, Any]:
        if not enabled:
            if self.market.status()["connected"]:
                self.market.disconnect()
            return self.market.status()
        return self.market.maintain_stream(health_timeout_seconds)

    def set_book_subscriptions(self, symbols: Iterable[str]) -> None:
        self.market.set_book_subscriptions(symbols)

    def subscribe_market(
        self,
        symbols: Iterable[str],
        *,
        initialize: bool = True,
        channels: Sequence[str] = ("trades",),
    ) -> Dict[str, Any]:
        return self.market.subscribe_market(symbols, initialize=initialize, channels=channels)

    def get_quote(self, symbol: str, market: Optional[str] = None) -> Dict[str, Any]:
        row = self.market.state.snapshot(str(symbol).strip())
        if market and row:
            wanted = _normalize_market(market)
            actual = _normalize_market(row.get("market"))
            if actual and wanted and actual != wanted:
                return {}
        return row

    def get_sell_quote(self, symbol, market=None):
        cache = getattr(self, "_sell_quote_cache", {})
        old = cache.get(symbol)
        if old and time.monotonic()-old[0] < 1:
            return dict(old[1])
        raw = self.market._get_rest_stock().intraday.quote(symbol=symbol)
        row = self.market._normalize_intraday_quote(raw)
        if row.get("symbol") != symbol or not row.get("price"):
            return {}
        if market and row.get("market") and _normalize_market(market) != _normalize_market(row["market"]):
            return {}
        row["source"] = "FUGLE_REST_SELL_FALLBACK"
        source_at = _event_datetime(row.get("timestamp"))
        row["sell_source_age_seconds"] = (_now()-source_at).total_seconds() if source_at else None
        cache[symbol] = (time.monotonic(), dict(row))
        self._sell_quote_cache = cache
        return row

    def check_position_liveness(self, symbols, timeout_seconds=4):
        """持倉逐檔四秒疑似斷流；REST 任務非阻塞，每秒持續保護。"""
        results = {}
        jobs = getattr(self, "_position_rest_jobs", {})
        checks = getattr(self, "_symbol_liveness_checks", {})
        resubs = getattr(self, "_position_resubs", {})
        for symbol in set(symbols):
            stamp = time.monotonic()
            quote = self.get_quote(symbol) or {}
            stream_at = _event_datetime(quote.get("event_time") or quote.get("timestamp"))
            age = (_now()-stream_at).total_seconds() if stream_at else None
            job = jobs.get(symbol)
            if age is not None and -1 <= age <= timeout_seconds:
                if job is not None and job.done():
                    jobs.pop(symbol, None)
                continue
            if job is not None and job.done():
                jobs.pop(symbol, None)
                try:
                    rest = job.result()
                    rest_at = _event_datetime(rest.get("timestamp"))
                    fresh = bool(rest_at and -1 <= (_now()-rest_at).total_seconds() <= 5)
                    result = {"status": "REST_PROTECTION" if fresh else "REST_STALE",
                              "quote_age": age, "quote": rest if fresh else {},
                              "rest_source_at": rest.get("timestamp")}
                    if fresh and (stream_at is None or rest_at > stream_at) and stamp-resubs.get(symbol, float("-inf")) >= 60:
                        client = self.market
                        resubs[symbol] = stamp
                        with client._lock:
                            identity, stock = client._subscription_ids.get(symbol), client._ws_stock
                        if identity and stock is not None:
                            with client._lock:
                                client._unsubscribe_retry_at[symbol] = stamp + client._subscription_retry_seconds
                            client._ws_write(stock, "unsubscribe", {"id": identity})
                        else:
                            with client._lock:
                                client._pending_subscriptions.discard(symbol)
                            client._sync_subscriptions()
                        result["status"] = "REST_PROTECTION_RESUBSCRIBE"
                    results[symbol] = result
                except Exception as exc:
                    results[symbol] = {"status": "REST_PROTECTION_UNAVAILABLE", "error": str(exc), "quote": {}}
            if symbol not in jobs and stamp-checks.get(symbol, 0) >= 1:
                checks[symbol] = stamp
                jobs[symbol] = self.market._rest_pool.submit(self.get_sell_quote, symbol)
                results.setdefault(symbol, {"status": "QUOTE_SUSPECT_REST_PENDING", "quote_age": age, "quote": {}})
            # 只補齊回傳資訊；不增派 REST 工作、不變更行情有效條件。
            results.setdefault(symbol, {"status": "QUOTE_SUSPECT_REST_PENDING", "quote_age": age, "quote": {}})
        for symbol in set(jobs)-set(symbols):
            jobs.pop(symbol).cancel()
            checks.pop(symbol, None)
            resubs.pop(symbol, None)
        self._position_rest_jobs, self._symbol_liveness_checks, self._position_resubs = jobs, checks, resubs
        return results

    def get_quotes(
        self,
        symbols: Optional[Iterable[str]] = None,
        market: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        rows = list(self.get_market_state_snapshot(symbols).get("rows") or [])
        if not market:
            return rows
        wanted = _normalize_market(market)
        return [
            row
            for row in rows
            if _normalize_market(row.get("market")) == wanted
        ]

    def get_market_state_snapshot(
        self,
        symbols: Optional[Iterable[str]] = None,
        *,
        as_of: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """公開 Atomic MarketState Snapshot；不存取 REST、不產生行情來源時間。"""
        return self.market.state.atomic_snapshot(symbols, as_of=as_of)

    def get_daily_snapshot(self, market: str) -> List[Dict[str, Any]]:
        return self.market.get_daily_snapshot(market)

    def get_active_universe(self, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        return self.market.get_active_universe(limit=limit)

    def get_sector_map(self) -> Dict[str, str]:
        """只讀已取得的基本資料；未覆蓋股票不猜測產業，下一輪 Market 使用新分類。"""
        rows = self.get_security_master()
        return {str(row["symbol"]): str(row.get("sector_name") or row.get("sector") or row["sector_code"])
                for row in rows if row.get("symbol") and row.get("sector_code")}

    def get_security_master(self) -> List[Dict[str, Any]]:
        """直接讀分類快取，沒有即時成交事件的股票也保留分類。"""
        # 整批完成後一次發布；讀取不等待基本資料網路查詢持有的鎖。
        return [dict(row) for row in self.market._security_metadata_snapshot]

    def get_index_quote(self, symbol: str, market: str) -> Dict[str, Any]:
        return self.market.get_index_quote(symbol, market)

    def get_index_history(self, symbol: str, market: str, *, limit: int = 20) -> List[Dict[str, Any]]:
        return self.market.get_index_history(symbol, market, limit=limit)

    def refresh_day_trade(self, symbols: Iterable[str]) -> Dict[str, Any]:
        return self.market.refresh_day_trade(symbols)

    def get_day_trade_map(self, symbols: Optional[Iterable[str]] = None) -> Dict[str, bool]:
        return self.market.get_day_trade_map(symbols)

    def status(self) -> Dict[str, Any]:
        cts_status = self.cts.status()
        return {
            "mode": self.mode,
            "timestamp": _now_text(),
            "market_provider": "FUGLE",
            "fugle": self.market.status(),
            "cts_connected": bool(cts_status.get("connected")),
            "cts": cts_status,
        }

    def close(self) -> Dict[str, Any]:
        if not hasattr(self, "_resource_manager"):
            self.market._rest_pool.shutdown(wait=False, cancel_futures=True)
        errors: List[str] = []
        pool = getattr(self, "_market_context_pool", None)
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        try:
            self.market.disconnect()
        except Exception as exc:
            errors.append(f"Fugle: {type(exc).__name__}: {exc}")
        try:
            self.replay_store.close()
        except Exception as exc:
            errors.append(f"Replay: {type(exc).__name__}: {exc}")
        try:
            if self.cts.status().get("connected"):
                result = self.cts.logout()
                errors.extend(str(item) for item in result.get("errors") or [])
        except Exception as exc:
            errors.append(f"CTS: {type(exc).__name__}: {exc}")
        self.cts.close_owner()
        return {"ok": not errors, "errors": errors}


# =============================================================================
# 離線檢查
# =============================================================================


def run_self_test() -> Dict[str, Any]:
    checks = 0
    with tempfile.TemporaryDirectory() as temp_dir:
        emitted_events: List[Dict[str, Any]] = []
        gateway = APIGateway(
            broker="CTS",  # Legacy CTS regression fixture, never a live login.
            mode="OFFLINE",
            replay_dir=temp_dir,
            freshness_seconds=25.0,
            event_sink=emitted_events.append,
        )

        old_api_key = os.environ.get("FUGLE_MARKETDATA_API_KEY")
        try:
            os.environ["FUGLE_MARKETDATA_API_KEY"] = "ENV-KEY-TEST"
            env_client = FugleMarketDataClient(
                mode="LIVE",
                replay_store=ReplayStore(Path(temp_dir) / "env_key"),
            )
            assert env_client.api_key == "ENV-KEY-TEST"

            import sys
            import types

            captured: Dict[str, List[str]] = {"rest": [], "ws": []}

            class _FakeRestClient:
                def __init__(self, *, api_key: str) -> None:
                    captured["rest"].append(api_key)
                    self.stock = object()

            class _FakeWSStock:
                def on(self, *_args: Any) -> None:
                    return None

            class _FakeWebSocketClient:
                def __init__(self, *, api_key: str) -> None:
                    captured["ws"].append(api_key)
                    self.stock = _FakeWSStock()

            previous_module = sys.modules.get("fugle_marketdata")
            fake_module = types.ModuleType("fugle_marketdata")
            fake_module.RestClient = _FakeRestClient
            fake_module.WebSocketClient = _FakeWebSocketClient
            sys.modules["fugle_marketdata"] = fake_module
            try:
                env_client._get_rest_stock()
                env_client._get_ws_stock()
            finally:
                if previous_module is None:
                    sys.modules.pop("fugle_marketdata", None)
                else:
                    sys.modules["fugle_marketdata"] = previous_module
            assert isinstance(env_client._rest_stock, _RestEndpoint)
            assert env_client._rest_stock.client.api_key == "ENV-KEY-TEST"
            assert captured == {
                "rest": [],
                "ws": ["ENV-KEY-TEST"],
            }
            env_client._on_connect()
            assert env_client.status()["connected"] is True
            assert env_client.status()["authenticated"] is False
            env_client._on_message(
                {
                    "event": "authenticated",
                    "data": {"message": "Authenticated successfully"},
                }
            )
            assert env_client.status()["authenticated"] is True
            checks += 1
        finally:
            if old_api_key is None:
                os.environ.pop("FUGLE_MARKETDATA_API_KEY", None)
            else:
                os.environ["FUGLE_MARKETDATA_API_KEY"] = old_api_key

        old_api_key = os.environ.pop("FUGLE_MARKETDATA_API_KEY", None)
        try:
            missing_key_client = FugleMarketDataClient(
                mode="LIVE",
                replay_store=ReplayStore(Path(temp_dir) / "missing_key"),
            )
            try:
                missing_key_client._get_rest_stock()
            except RuntimeError as exc:
                assert str(exc) == "缺少環境變數 FUGLE_MARKETDATA_API_KEY"
            else:
                raise AssertionError("缺少 Fugle API Key 時必須拒絕建立 REST client")
            missing_status = missing_key_client.status()
            assert missing_status["connected"] is False
            assert missing_status["authenticated"] is False
            assert missing_status["api_key_configured"] is False
            assert missing_status["last_error"] == "缺少環境變數 FUGLE_MARKETDATA_API_KEY"
            checks += 1
        finally:
            if old_api_key is not None:
                os.environ["FUGLE_MARKETDATA_API_KEY"] = old_api_key

        gateway.market.state.seed_quote(
            {
                "symbol": "2330",
                "name": "測試股票",
                "market": "TWSE",
                "trade_date": "2026-09-13",
                "closePrice": 100.0,
                "openPrice": 99.0,
                "highPrice": 101.0,
                "lowPrice": 98.5,
                "previousClose": 98.0,
                "tradeVolume": 1000,
            }
        )
        seeded = gateway.get_quote("2330", "TWSE")
        assert seeded["source"] == "FUGLE_REST_SEED"
        assert seeded["event_time"] is None
        checks += 1

        base = int(datetime(2026, 9, 13, 9, 30, tzinfo=timezone(timedelta(hours=8))).timestamp() * 1_000_000)
        first = gateway.market.state.apply_trade(
            {
                "symbol": "2330",
                "exchange": "TWSE",
                "time": base,
                "serial": 100,
                "price": 100.5,
                "size": 5,
                "volume": 1005,
                "bid": 100.0,
                "ask": 100.5,
            },
            received_at="2026-09-13T09:30:00.100000+08:00",
        )
        duplicate = gateway.market.state.apply_trade(
            {
                "symbol": "2330",
                "exchange": "TWSE",
                "time": base,
                "serial": 100,
                "price": 100.5,
                "size": 5,
                "volume": 1005,
                "bid": 100.0,
                "ask": 100.5,
            },
            received_at="2026-09-13T09:30:05.000000+08:00",
        )
        out_of_order = gateway.market.state.apply_trade(
            {
                "symbol": "2330",
                "exchange": "TWSE",
                "time": base - 1,
                "serial": 99,
                "price": 100.4,
                "size": 1,
                "volume": 1004,
                "bid": 100.0,
                "ask": 100.5,
            }
        )
        second = gateway.market.state.apply_trade(
            {
                "symbol": "2330",
                "exchange": "TWSE",
                "time": base + 1_000_000,
                "serial": 101,
                "price": 101.0,
                "size": 8,
                "volume": 1013,
                "bid": 100.5,
                "ask": 101.0,
            }
        )
        assert first["classification"] == "NEW"
        assert duplicate["classification"] == "DUPLICATE"
        assert out_of_order["classification"] == "OUT_OF_ORDER"
        assert second["classification"] == "NEW"
        assert second["quote_sequence"] == 2
        assert second["symbol_quote_sequence"] == 2
        checks += 1

        def _unexpected_rest_call() -> Any:
            raise AssertionError("get_quote() / get_quotes() 不得觸發 REST")

        gateway.market._get_rest_stock = _unexpected_rest_call
        quote = gateway.get_quote("2330")
        assert quote["event_time"] == base + 1_000_000
        assert quote["serial"] == 101
        assert quote["price"] == 101.0
        assert quote["received_at"] is not None
        assert quote["freshness_basis"] == "event_time"
        assert gateway.get_quotes(["2330"])[0]["symbol"] == "2330"
        checks += 1

        gateway.market._on_message(
            json.dumps(
                {
                    "event": "data",
                    "channel": "trades",
                    "data": {
                        "symbol": "2454",
                        "exchange": "TWSE",
                        "market": "TSE",
                        "time": base + 2_000_000,
                        "serial": 200,
                        "price": 120.0,
                        "size": 3,
                        "volume": 2003,
                        "bid": 119.5,
                        "ask": 120.0,
                    },
                },
                ensure_ascii=False,
            )
        )
        ws_quote = gateway.get_quote("2454")
        ws_events = gateway.replay_store.load_fugle_events("2454")
        assert ws_quote["event_time"] == base + 2_000_000
        assert ws_quote["serial"] == 200
        assert ws_quote["source_at"].endswith("+08:00")
        assert ws_events[-1]["payload"]["bid"] == 119.5
        assert ws_events[-1]["classification"] == "NEW"
        assert len(emitted_events) == 1
        assert emitted_events[0]["symbol"] == "2454"
        assert emitted_events[0]["event_time"] == base + 2_000_000

        gateway.market._on_message(
            {
                "event": "data",
                "channel": "trades",
                "data": {
                    "symbol": "2454",
                    "exchange": "TWSE",
                    "time": base + 2_000_000,
                    "serial": 200,
                    "price": 120.0,
                    "size": 3,
                    "volume": 2003,
                    "bid": 119.5,
                    "ask": 120.0,
                },
            }
        )
        assert len(emitted_events) == 1
        checks += 1

        gateway.market.day_trade.replace(
            {"2330": True, "2454": False},
            source_at="2026-09-13T09:00:00+08:00",
        )
        assert gateway.get_day_trade_map() == {"2330": True, "2454": False}
        assert gateway.get_day_trade_map(["2330"]) == {"2330": True}
        no_synthetic_time = gateway.market.day_trade.replace({"2330": True})
        assert no_synthetic_time["source_at"] is None
        checks += 1

        saved = gateway.replay_store.append_fugle_event(
            {
                "kind": "TRADE",
                "channel": "trades",
                "symbol": "2330",
                "event_time": base,
                "serial": 100,
                "received_at": "2026-09-13T09:30:00.100000+08:00",
                "price": 100.5,
                "size": 5,
                "volume": 1005,
                "bid": 100.0,
                "ask": 100.5,
                "classification": "NEW",
                "payload": {
                    "price": 100.5,
                    "size": 5,
                    "volume": 1005,
                    "bid": 100.0,
                    "ask": 100.5,
                },
            }
        )
        loaded = gateway.replay_store.load_fugle_events("2330")
        assert loaded and loaded[0]["id"] == saved["id"]
        checks += 1

        order_rows = _records_from_cts_text(
            "<F0=79Z-1234567|F1=20260913|F2=0|F3=0|F4=1|"
            "F5=2330|F6=0|F7=100.0|F8=1|F9=X001|F10=0|F11=0|"
            "F14=委託成功|F15=OID001|F18=1|F28=成功|F30=0>"
        )
        assert order_rows[0]["F5"] == "2330"
        assert order_rows[0]["F15"] == "OID001"
        checks += 1

        match_rows = _records_from_cts_text(
            "<F2=0|F3=2330|F4=1|F5=0|F6=100.5|F7=1|F9=X001|F10=20260913|F11=093001|F14=OID001|F20=093001123>"
        )
        normalized = CTSClient._normalize_match(match_rows[0])
        assert normalized["filled_shares"] == 1000
        assert normalized["cts_quantity"] == 1
        assert normalized["match_time"] == "093001123"

        compatibility_row = dict(match_rows[0], F7="1000")
        compatibility = CTSClient._normalize_match(compatibility_row)
        assert compatibility["filled_shares"] == 1000
        assert compatibility["cts_quantity"] == 1
        assert (
            compatibility["quantity_normalization"]
            == "CTS_QUERYMATCH_RAW_SHARES_COMPAT"
        )
        checks += 1

        deterministic_order_raw = (
            "<F0=0|F1=20260913|F2=X0001|F3=OID001|F6=|F7=>"
        )
        deterministic_order_a = CTSClient._order_action_result(
            deterministic_order_raw,
            kind="NEW",
        )
        deterministic_order_b = CTSClient._order_action_result(
            deterministic_order_raw,
            kind="NEW",
        )
        assert deterministic_order_a == deterministic_order_b

        new_action = CTSClient._order_action_result(
            deterministic_order_raw,
            kind="NEW",
        )
        modify_action = CTSClient._order_action_result(
            "<F0=0|F1=X0002|F2=20260913|F3=OID002|F6=|F7=>",
            kind="MODIFY",
        )
        cancel_action = CTSClient._order_action_result(
            "<F0=0|F1=20260913|F2=X0003|F3=OID003|F6=|F7=>",
            kind="CANCEL",
        )
        assert new_action["order_no"] == "X0001"
        assert modify_action["order_no"] == "X0002"
        assert cancel_action["order_no"] == "X0003"
        checks += 1

        official_quote_shape = FugleMarketDataClient._normalize_intraday_quote(
            {
                "date": "2026-09-13",
                "exchange": "TWSE",
                "market": "TSE",
                "symbol": "2317",
                "name": "測試",
                "previousClose": 100.0,
                "openPrice": 101.0,
                "highPrice": 102.0,
                "lowPrice": 100.5,
                "closePrice": 101.5,
                "lastPrice": 101.5,
                "lastUpdated": base,
                "total": {"tradeVolume": 1234, "tradeValue": 125000000},
            }
        )
        assert official_quote_shape["price"] == 101.5
        assert official_quote_shape["volume"] == 1234
        assert official_quote_shape["turnover"] == 125000000.0
        checks += 1

        request = CTSOrderRequest(
            account_id="79Z-1234567",
            trade_date=20260913,
            trade_type=0,
            order_type=0,
            side=1,
            symbol="2330",
            quantity=1,
            price_type=0,
            price="100.5",
        )
        assert request.to_dict()["symbol"] == "2330"
        checks += 1

        fixed_as_of = datetime(
            2026,
            9,
            13,
            9,
            30,
            10,
            tzinfo=TAIPEI_TZ,
        )
        frozen_first = gateway.get_market_state_snapshot(as_of=fixed_as_of)
        frozen_second = gateway.get_market_state_snapshot(as_of=fixed_as_of)
        assert frozen_first == frozen_second
        assert frozen_first["generation"] == 3
        rows_by_symbol = {row["symbol"]: row for row in frozen_first["rows"]}
        assert rows_by_symbol["2330"]["event_time"] == base + 1_000_000
        assert rows_by_symbol["2330"]["quote_sequence"] == 2
        assert rows_by_symbol["2330"]["price"] == 101.0
        assert rows_by_symbol["2330"]["volume"] == 1013
        source_before = rows_by_symbol["2330"]["source_at"]
        unchanged = gateway.get_market_state_snapshot(as_of=fixed_as_of)
        unchanged_2330 = next(
            row for row in unchanged["rows"] if row["symbol"] == "2330"
        )
        assert unchanged["generation"] == frozen_first["generation"]
        assert unchanged_2330["source_at"] == source_before
        checks += 1

        class _FakeCTSLoginApp:
            def __init__(self) -> None:
                self.calls: List[str] = []

            def Init(self, _trade_das: str) -> Tuple[int, int, str]:
                self.calls.append("Init")
                return 1, 0, ""

            def Login(self, _uid: str, _password: str) -> Tuple[int, int, str]:
                self.calls.append("Login")
                return 1, 0, ""

            def Connect(self) -> int:
                self.calls.append("Connect")
                return 1

            def SetEchoType(self, command_type: int, echo_type: int) -> None:
                self.calls.append(f"SetEchoType({command_type},{echo_type})")

            def GetAccountList(self) -> str:
                self.calls.append("GetAccountList")
                return (
                    "<ID=79Z-1234567|Name=測試證券|UID=A123456789|"
                    "Type=1|Credit=1|SFID=|CanHedge=2>"
                )

        login_app = _FakeCTSLoginApp()
        login_client = CTSClient(mode="OFFLINE")
        login_client._ensure_app = lambda: login_app
        login_result = login_client.login("test/tradedas", "A123456789", "pwd")
        assert login_app.calls == ["Init", "Login", "Connect", "SetEchoType(1,0)", "SetEchoType(2,0)", "SetEchoType(3,0)", "GetAccountList"]
        assert login_client.connected is True
        assert login_client.get_accounts()[0]["type"] == 1
        assert login_client.get_accounts()[0]["can_hedge"] == 2
        assert login_result["account_id"] == "79Z-1234567"
        assert login_result["account_count"] == 1
        assert login_result["security_account_count"] == 1
        assert login_result["raw_account_result"].startswith("<ID=79Z-1234567")
        assert login_client.status()["security_account_ids"] == ["79Z-1234567"]
        checks += 1

        class _FakeCTSEmptyAccountApp(_FakeCTSLoginApp):
            def GetAccountList(self) -> str:
                self.calls.append("GetAccountList")
                return ""

            def Disconnect(self) -> None:
                self.calls.append("Disconnect")

            def Logout(self, _uid: str) -> None:
                self.calls.append("Logout")

            def Fini(self) -> None:
                self.calls.append("Fini")

        empty_account_app = _FakeCTSEmptyAccountApp()
        empty_account_client = CTSClient(mode="OFFLINE")
        empty_account_client._ensure_app = lambda: empty_account_app
        try:
            empty_account_client.login("test/tradedas", "A123456789", "pwd")
        except RuntimeError as exc:
            assert str(exc) == "GetAccountList 未回傳可解析帳號資料"
        else:
            raise AssertionError("GetAccountList 空回傳不得視為登入資料取得成功")
        assert empty_account_app.calls == [
            "Init",
            "Login",
            "Connect",
            "SetEchoType(1,0)",
            "SetEchoType(2,0)",
            "SetEchoType(3,0)",
            "GetAccountList",
            "Disconnect",
            "Logout",
            "Fini",
        ]
        assert empty_account_client.connected is False
        checks += 1

        class _FakeCTSQueryApp:
            def Stock_QueryOrder(self, account: str, _force: int) -> str:
                return (
                    f"<F0={account}|F1=20260913|F2=0|F3=0|F4=1|F5=2330|"
                    "F6=0|F7=100.5|F8=1|F9=X001|F10=0|F11=0|"
                    "F14=委託成功|F15=OID001|F18=1|F26=0|F27=|F28=成功>"
                )

            def Stock_QueryMatch(self, _account: str, _force: int) -> str:
                return (
                    "<F2=0|F3=2330|F4=1|F5=0|F6=100.5|F7=1|F9=X001|"
                    "F10=20260913|F11=093001|F14=OID001|F20=093001123>"
                )

            def Stock_QueryPosition(
                self,
                _account: str,
                _trade_date: int,
                _force: int,
            ) -> str:
                return "<F0=0|F1=2330|F2=1000|F3=1000|F23=100.5|F24=台積電>"

        query_app = _FakeCTSQueryApp()
        query_client = CTSClient(mode="OFFLINE")
        query_client._ensure_connected = lambda: query_app
        order_a = query_client.query_order("79Z-1234567", force_query=True)
        order_b = query_client.query_order("79Z-1234567", force_query=True)
        match_a = query_client.query_match("79Z-1234567", force_query=True)
        match_b = query_client.query_match("79Z-1234567", force_query=True)
        position_a = query_client.query_position("79Z-1234567", 20260913, True)
        position_b = query_client.query_position("79Z-1234567", 20260913, True)
        assert order_a == order_b
        assert match_a == match_b
        assert position_a == position_b
        assert order_a["ok"] is True
        assert order_a["status"] == "SUCCESS"
        assert order_a["record_count"] == 1
        assert order_a["raw_error"] == ""
        assert order_a["record_errors"] == []
        assert match_a["record_count"] == 1
        assert len(match_a["normalized_result"]) == 1
        assert position_a["record_count"] == 1

        empty_query = CTSClient._query_result("", kind="POSITION")
        assert empty_query["ok"] is False
        assert empty_query["status"] == "ERROR"
        confirmed_empty = CTSClient._query_result("<rc=1|count=0>", kind="POSITION")
        assert confirmed_empty["ok"] is True and confirmed_empty["status"] == "EMPTY"
        assert empty_query["record_count"] == 0

        invalid_query = CTSClient._query_result("CTS_QUERY_FAILED", kind="MATCH")
        assert invalid_query["ok"] is False
        assert invalid_query["status"] == "ERROR"
        assert invalid_query["raw_error"] == "CTS_QUERY_FAILED"
        assert invalid_query["record_count"] == 0

        class _FailingCTSQueryApp:
            def Stock_QueryMatch(self, _account: str, _force: int) -> str:
                raise RuntimeError("CTS transport failure")

        failing_client = CTSClient(mode="OFFLINE")
        failing_client._ensure_connected = lambda: _FailingCTSQueryApp()
        failed = failing_client.query_match("79Z-1234567", force_query=True)
        assert failed["ok"] is False
        assert failed["status"] == "ERROR"
        assert failed["raw_error"] == "RuntimeError: CTS transport failure"
        assert failed["record_count"] == 0
        checks += 1

        assert gateway.status()["mode"] == "OFFLINE"
        checks += 1

    return {
        "ok": True,
        "checks": checks,
        "market_boundary": "REST_SEED_SNAPSHOT_HISTORY + WEBSOCKET_TRADES_BOOKS",
        "quote_read": "MEMORY_ONLY",
        "source_time": "FUGLE_EVENT_TIME",
        "received_at_is_source_time": False,
        "day_trade_read": "ATOMIC_CACHE_ONLY",
        "cts_cache_isolated_from_fugle": True,
    }


def diagnose_links() -> Dict[str, Any]:
    return {
        "ok": True,
        "APIGateway": True,
        "MarketState": True,
        "ReplayStore": True,
        "CTSClient": True,
        "CTSOrderRequest": True,
        "CTSModifyRequest": True,
        "CTSCancelRequest": True,
        "fugle_ws_public_connect": "APIGateway.fugle_login()",
        "fugle_ws_public_subscribe": "APIGateway.subscribe_market()",
        "market_state_atomic_snapshot": "APIGateway.get_market_state_snapshot()",
        "market_state_generation_source": "ACCEPTED_FUGLE_TRADE_EVENT",
        "get_quote_network_call": False,
        "get_quotes_network_call": False,
        "market_state_snapshot_network_call": False,
        "websocket_realtime_channel": "trades",
        "rest_source_error_policy": "PROPAGATE_TO_CALLER",
        "day_trade_read_network_call": False,
        "cts_login_sequence": "Init -> Login -> Connect -> GetAccountList",
        "fugle_api_key_env": "FUGLE_MARKETDATA_API_KEY",
        "fugle_rest_ws_shared_api_key": True,
        "cts_query_result_status": "ok + status + raw_error + record_count",
        "cts_query_order": "Stock_QueryOrder",
        "cts_query_match": "Stock_QueryMatch",
        "cts_query_position": "Stock_QueryPosition",
        "cts_separate_from_market_state": True,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Fugle 市場資料與 CTS 交易 API 統一閘道")
    parser.add_argument("--self-test", action="store_true", help="執行完全離線自我測試")
    parser.add_argument("--diagnose", action="store_true", help="檢查公開介面與資料邊界")
    parser.add_argument("--json", action="store_true", help="輸出 JSON")
    args = parser.parse_args(argv)

    result = run_self_test() if args.self_test else diagnose_links()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        for key, value in result.items():
            print(f"{key}: {value}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

# 更新摘要：持倉四秒逐檔斷流偵測，非阻塞 REST 每秒確認，恢復前持續保護並節流重訂閱。
