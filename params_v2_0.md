# 系統參數重點說明

本文件提供參數用途；實際設定與驗證以各模組的參數類別為準。表列預設值為程式預設，載入既有設定後以 GUI「目前生效值」為準。每個參數保留用途、單位、預設、0 值意義與限制；未列出數值上限者不得解讀為交易所允許任意數量。

GUI 表格顯示精簡重點；雙擊目前值可查看自動換行、可捲動的完整說明，確認與取消固定在視窗底部。布林參數以 True／False 設定，0 值欄不適用；集合與文字亦不套用數字零。

自動 Momentum／Trend 的進出場條件不變，最終 BUY／SELL 使用市價。手動 BUY／SELL 確認後直接進行帳戶檢查及市價送單，不經策略、候選池或類股判斷。手動 BUY 依可用資金比例、單倉權益比例、單倉金額及股數上限換算整張；已有同股持倉不重複建立新倉。資金為估算，市價實際成交金額會隨行情改變。手動買入標記人工管理；手動 SELL 可處理系統與外部現股，但不得超過券商最新庫存，已有未結賣單不得再送。未滿一張的餘股保留顯示，不送零股單。

CTS 庫存、委託、成交皆強制重新查詢。timeout、例外、空白或解析失敗保留最後有效庫存並標示 stale；只有明確成功且零筆資料才能清空。個股成交差異不抹去其他股票。系統歸屬由既有委託身分及實際成交恢復，未確認外部庫存不自動接管。

市價代碼為 4，價格字串為 0，Stock_NewOrder 保留 12 個位置參數。依據：[中信 API 交易功能](https://www.ctbcsec.com/2020/n3-service/API/APIDoc_20260714/api_docs/trading_func.html)、[中信官方 Python 範例](https://www.ctbcsec.com/2020/download/API/CTSwithPython.zip)。新單說明的價格代碼列表未列 4，已交叉核對官方範例的「市價」選項與非限價 Price=0 處理。

STATUS_UNKNOWN 保留原委託鎖與資金預留；無 OID／委託書號時仍查 Stock_QueryOrder(accountID, 1)、Stock_QueryMatch、Stock_QueryPosition，未證明原委託不存在不得重送。市價不得跳過部分成交、委託終態與對帳。

自訂百分比停利使用 CTS 成交量加權均價；多筆成交與加碼按持倉成本更新，不用委託價、Ask、Bid 或訊號價冒充成交價。Momentum Decay、結構判斷、VWAP／EMA／回踩、時間退出與原 EMERGENCY_STOP 沿用原邏輯。API Token 與交易密碼不保存於本文件或參數檔。

# 中文重點更新

移除無效開盤區間與限價賣單追價參數；市價委託、成交追蹤、撤銷未成交買單及時間清倉維持原有流程。

# 中文重點更新：快照生命週期

Scanner 的固定輸入與候選快照保留各自身分、來源時間、交易日及有效期限；新的行情與市場 generation 不會單獨使整輪掃描失效。掃描運算仍受 scanner.scanner_round_time_limit_seconds 限制，候選快照有效期限使用 scanner.realtime_freshness_max_age_seconds；Intraday 結果仍使用 gui.result_expiry_seconds，持倉、帳戶風險及設定改變時仍須重算。

Market Guard 直接讀取 Market 原子發布資料，獨立於 Scanner 成敗，保留 market_permission_snapshot_id、source_at、refreshed_at、TTL 與交易日。真正 BUY 送單前再次確認最新策略許可。類股資料由 Runtime 讀取記憶體行情與分類快取重新建立，最多快取一秒；保留原始來源時間，無效、降級、跨交易日或真正過期時仍禁止 Trend 進場。REST 快照使用成功取得該批資料的 snapshot_acquired_at，逐筆資料使用真正行情來源時間，不以重算時間延長資料壽命。

買賣價格、Spread、Quote Freshness、7% 漲幅上限、部位風控、Submission Lock、CTS 撤單與委託及庫存對帳維持既有規則。

# GUI／帳戶

## gui.result_expiry_seconds

盤中策略計算結果的單調時鐘有效期限，預設五秒，必須大於零且不超過二十五秒。 來源行情新鮮度仍由既有獨立 Gate 判斷，兩者不能互相替代。

單位：秒；預設：`5.0`；0 值：不合法。有效範圍／注意：計算結果有效期限必須大於零且不超過二十五秒。

## gui.control_mode

啟動安全預設為 MANUAL；僅允許 AUTO / MANUAL。切換 AUTO 必須由使用者主動操作。

單位：文字／代碼；預設：`'MANUAL'`；0 值：不適用。有效範圍／注意：僅允許 AUTO / MANUAL。

## gui.api_mode

僅允許 LIVE / OFFLINE；LIVE 使用即時 API，OFFLINE 僅讀取重播資料。

單位：文字／代碼；預設：`'LIVE'`；0 值：不適用。有效範圍／注意：僅允許 LIVE / OFFLINE。

## gui.replay_dir

不得為空；作為行情與交易重播資料保存目錄。

單位：文字／代碼；預設：`'api_replay'`；0 值：不適用。有效範圍／注意：不得為空。

## gui.market_refresh_seconds

必須大於 0；Runtime 執行 Market／Active Universe／Scanner 維護週期，單位為秒。這不是 Fugle Trade Event 的取得頻率，單筆 WebSocket Event 不會因本參數而等待。

單位：秒；預設：`30.0`；0 值：不合法。有效範圍／注意：必須大於 0。

## gui.quote_refresh_seconds

必須大於 0；僅控制 GUI 畫面與狀態顯示刷新週期，單位為秒。策略行情由 Fugle WebSocket Event 驅動，Scanner／Intraday／Trading 的事件計算不得使用此值作為行情取得頻率。

單位：秒；預設：`3.0`；0 值：不合法。有效範圍／注意：必須大於 0。

## gui.intraday_watch_count

必須大於 0；只限制候選股票的盤中監控數量。所有已持倉股票不受此上限，必須持續納入行情與出場監控。

單位：檔股票；預設：`30`；0 值：不合法。有效範圍／注意：必須大於 0。

## gui.auto_execute

安全預設為 False；只有 AUTO 模式且此項為 True 時才允許自動送出已通過風控的交易。

單位：開關；預設：`False`；0 值：不適用。有效範圍／注意：True／False。

## gui.fugle_websocket_enabled

預設 True；控制 Runtime 是否啟用 Fugle WebSocket 盤中事件流。此欄位屬 Stream 設定，不保存 Fugle API Token。Runtime 已接線；關閉時由行情 Worker 停止 WebSocket，CTS 連線獨立保留。

單位：開關；預設：`True`；0 值：不適用。有效範圍／注意：True／False。

## gui.fugle_rest_seed_enabled

預設 True；控制新加入訂閱的股票是否由 Fugle REST 建立初始化 Snapshot；WebSocket 先訂閱，再執行 seed，逐筆事件持續更新。REST 只負責 seed／snapshot，不取代事件驅動。Runtime 已接線；同交易日已 seed 的股票不重複抓取，seed 不覆蓋已接受的逐筆價格。

單位：開關；預設：`True`；0 值：不適用。有效範圍／注意：True／False。

## gui.fugle_event_freshness_seconds

對應 APIGateway MarketState 的一般 Event Freshness 門檻。這是 Fugle event_time 的新鮮度基準，不是 GUI 輪詢秒數，也不等於 Trading BUY Revalidation 的 25 秒門檻。

單位：秒；預設：`3.0`；0 值：不合法。有效範圍／注意：必須大於 0。

## gui.fugle_stream_health_timeout_seconds

Stream Health 用來判斷 WebSocket 是否長時間沒有健康訊號／事件。它屬連線健康監控，不得直接改寫 ENTRY、Momentum、Volume 或 Breakout 策略公式。獨立 StreamHealthWorker 每秒檢查健康狀態；失聯時由 Gateway 依重連間隔重試，不占用 CTS Worker。

單位：秒；預設：`30.0`；0 值：不合法。有效範圍／注意：必須大於 0。

## gui.fugle_reconnect_min_seconds

Fugle WebSocket 斷線後重連 backoff 的最小等待秒數。舊設定低於此值時，Gateway 實際執行仍至少等待 30 秒。

單位：秒；預設：`30.0`；0 值：不合法。有效範圍／注意：必須大於 0。fugle_reconnect_max_seconds 不得小於 fugle_reconnect_min_seconds。

## gui.fugle_reconnect_max_seconds

Fugle WebSocket 指數退避重連的最大等待秒數，不得小於 fugle_reconnect_min_seconds。Gateway 退避上限至少為 60 秒；明確設定更長間隔時保留該設定。

單位：秒；預設：`60.0`；0 值：不合法。有效範圍／注意：不得小於 fugle_reconnect_min_seconds。

## gui.fugle_subscription_limit

500 檔股票池之外保留 100 個名額供持倉／既有候選使用。系統內部訂閱安全上限，用來限制 Runtime 同時要求訂閱的 symbol 數量。這是專案內部政策欄位，不宣稱等同 Fugle 官方方案上限；Runtime 已依 P0 持倉、P1 候選、P2 Universe 順序裁切；移出名單的股票依官方訂閱 ID 取消。P0 數量超過上限時列出未訂閱持倉。

單位：檔股票；預設：`600`；0 值：不合法。有效範圍／注意：必須大於 0。

## gui.fugle_candidate_subscription_enabled

預設 True；控制 Candidate／P1 股票是否加入 Fugle Trade 與 Books 訂閱集合。P0 持倉監控不應因 Candidate 設定或 Candidate List 淘汰而停止。Runtime 已接線；持倉變更會通知行情 Worker 更新 P0，不等待下次 Scanner 輪次。

單位：開關；預設：`True`；0 值：不適用。有效範圍／注意：True／False。

## gui.fugle_books_subscription_mode

固定 P0_P1：持倉與 Scanner 候選訂閱 Books；Universe 保留 Trades。舊 OFF／P0／ALL 設定載入時轉為 P0_P1。退出後立即失效，再加入需等待新 Book。

單位：文字／代碼；預設：`'OFF'`；0 值：不適用。有效範圍／注意：僅允許 OFF / P0 / P0_P1 / ALL。

## gui.manual_market_state

MANUAL 市場狀態；僅允許 BULL / NORMAL / WEAK / PANIC。

單位：文字／代碼；預設：`'NORMAL'`；0 值：不適用。有效範圍／注意：BULL／NORMAL／WEAK／PANIC。

## gui.manual_trading_mode

MANUAL 交易模式；僅允許 TREND / SELECTIVE_LONG / DEFENSIVE / OVERSOLD_MEAN_REVERSION / STOP。

單位：文字／代碼；預設：`'SELECTIVE_LONG'`；0 值：不適用。有效範圍／注意：TREND／SELECTIVE_LONG／DEFENSIVE／OVERSOLD_MEAN_REVERSION／STOP。

## gui.available_cash

不得小於 0；由 GUI／主程式提供給 trading.py 作為可用現金風控輸入。

單位：新台幣元；預設：`0.0`；0 值：沒有可用交易預算，無法買進。有效範圍／注意：大於或等於 0；買進須有足夠交易預算。

## gui.equity

不得小於 0；由 GUI／主程式提供給 trading.py 作為帳戶權益與風險計算輸入。

單位：新台幣元；預設：`0.0`；0 值：買進資金換算以可用預算補足；自動策略仍須通過權益風控。有效範圍／注意：大於或等於 0。

## gui.daily_trade_count

不得小於 0；由 GUI／主程式提供給 trading.py 檢查每日交易次數。

單位：次；預設：`0`；0 值：尚無設定的當日交易次數。有效範圍／注意：不得小於 0。

## gui.daily_pnl

必須為有限數值；由 GUI／主程式提供給 trading.py 檢查每日損益限制。

單位：新台幣元；預設：`0.0`；0 值：設定損益為零。有效範圍／注意：必須是有限數值。

## gui.cts_trade_das

CTS 登入主機；只用於登入連線，不是證券帳號。

單位：文字／代碼；預設：`'api.win168.com.tw/tradedas'`；0 值：不適用。有效範圍／注意：有效的 CTS 伺服器位址。

## gui.cts_uid

CTS 登入帳號／身分證字號；可保存以便下次帶入，交易密碼不屬於本設定且永不保存。

單位：文字／代碼；預設：`''`；0 值：不適用。有效範圍／注意：文字，登入時必須提供有效身分。

## gui.cts_account_id

證券帳號；可留空，登入後只有在可唯一判定時才由 CTS 帳號清單自動選取。

單位：文字／代碼；預設：`''`；0 值：不適用。有效範圍／注意：文字，可空白；登入後需確認實際帳號。

## gui.cts_com_prog_id

CTS COM ProgID；留空時使用 api_gateway.py 的預設設定，必要時才覆寫。

單位：文字／代碼；預設：`''`；0 值：不適用。有效範圍／注意：文字，可空白採閘道預設。

# 市場

## market.trend_breadth_min

趨勢做多最低市場廣度。

單位：比率；預設：`0.0`；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：必須為有限數值。

## market.trend_pullback_score_min

強勢整理／回檔的最低市場分數。SIDEWAYS／DOWN 須同時通過此分數、廣度及兩指數正漲幅；PANIC、弱勢或資料異常仍禁止進場。

單位：分；預設：`30.0`；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：必須介於 -100 與 100。

## market.trend_pullback_breadth_min

強勢整理／回檔最低市場廣度 ratio，預設 0.20，範圍 -1 至 1。沿用市場現有 net_ratio 定義，不是上漲家數百分比。 原 trend_breadth_min 仍用於短線 UP 判定；本門檻只用於 SIDEWAYS／DOWN 的條件許可。

單位：比率；預設：`0.2`；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：必須介於 -1 與 1。

## market.trend_permission_max_age_seconds

Trend 市場許可的資料有效秒數。指數檢查來源時間，廣度檢查完整快照取得時間；快取與 GUI 重繪不得延長期限。送單前再確認許可，個股行情新鮮度另行檢查。

單位：秒；預設：`60.0`；0 值：不合法。有效範圍／注意：必須大於 0 且不超過 300 秒。

## market.ma_period

計算 TAIEX／TPEx 移動平均，作為指數短線方向判定的基準週期。

單位：根（輸入序列）；預設：`5`；0 值：不合法。有效範圍／注意：必須大於 0。

## market.market_score_weight

控制市場總分中『指數表現』所占比重；資料缺漏時核心模組會依可用權重重新正規化。

單位：比率；預設：`0.55`；0 值：此項不計分；同組其他權重須調整以維持總和 1。有效範圍／注意：指數權重 + 市場廣度權重 + 類股權重 必須等於 1。

## market.breadth_weight

控制市場總分中『上漲／下跌家數與資金流』所占比重。

單位：比率；預設：`0.25`；0 值：此項不計分；同組其他權重須調整以維持總和 1。有效範圍／注意：指數權重 + 市場廣度權重 + 類股權重 必須等於 1。

## market.sector_weight

控制市場總分中『類股強弱』所占比重；類股資料覆蓋不足時會依覆蓋率降低有效權重。

單位：比率；預設：`0.2`；0 值：此項不計分；同組其他權重須調整以維持總和 1。有效範圍／注意：指數權重 + 市場廣度權重 + 類股權重 必須等於 1。

## market.bull_threshold

市場總分達到此門檻時判為 BULL，進而影響交易模式與多方許可。

單位：分；預設：`30.0`；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：弱勢門檻 必須小於 多頭門檻。

## market.bear_threshold

市場總分低於此門檻時判為 WEAK；急跌保護仍可不經此門檻直接判為 PANIC。

單位：分；預設：`-30.0`；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：必須小於 多頭門檻。

## market.disabled_sectors

停用類股清單；列入的類股不參與 strong_sectors 與 Sector Strength 聚合。

單位：清單；預設：`[]`；0 值：不適用。有效範圍／注意：使用 JSON／陣列格式；內容、權重或排序關係由核心模組 validate() 驗證。

## market.mode

市場判定模式；AUTO 使用市場公式自動判斷，MANUAL 允許人工指定 Market State／Trading Mode。

單位：文字／代碼；預設：`'AUTO'`；0 值：不適用。有效範圍／注意：AUTO／MANUAL；MANUAL 須提供市場狀態或交易模式。

## market.manual_market_state

MANUAL 模式下指定市場狀態；允許 BULL／NORMAL／WEAK／PANIC。AUTO 模式不以此值覆寫公式結果。

單位：次；預設：`None`；0 值：不適用。有效範圍／注意：MANUAL 模式至少要提供 manual_market_state 或 manual_trading_mode。僅允許 ['BULL', 'NORMAL', 'PANIC', 'WEAK']。

## market.manual_trading_mode

MANUAL 模式下指定交易模式；AUTO 模式由 Market State 對應既有 Trading Mode。

單位：次；預設：`None`；0 值：不適用。有效範圍／注意：MANUAL 模式至少要提供 manual_market_state 或 manual_trading_mode。僅允許 ['DEFENSIVE', 'OVERSOLD_MEAN_REVERSION', 'SELECTIVE_LONG', 'STOP', 'TREND']。

## market.taiex_index_weight

合成指數分數與市場動能時，決定 TAIEX 對整體市場判定的影響比例。

單位：比率；預設：`0.65`；0 值：此項不計分；同組其他權重須調整以維持總和 1。有效範圍／注意：TAIEX 權重 + TPEx 權重 必須等於 1。

## market.tpex_index_weight

合成指數分數與市場動能時，決定 TPEx 對整體市場判定的影響比例。

單位：比率；預設：`0.35`；0 值：此項不計分；同組其他權重須調整以維持總和 1。有效範圍／注意：TAIEX 權重 + TPEx 權重 必須等於 1。

## market.index_change_weight

單一指數評分中，控制當日漲跌幅對指數分數的影響比例。

單位：比率；預設：`0.75`；0 值：此項不計分；同組其他權重須調整以維持總和 1。有效範圍／注意：指數漲跌權重 + 指數方向權重 必須等於 1。

## market.index_direction_weight

單一指數評分中，控制相對移動平均／開盤方向對指數分數的影響比例。

單位：比率；預設：`0.25`；0 值：此項不計分；同組其他權重須調整以維持總和 1。有效範圍／注意：指數漲跌權重 + 指數方向權重 必須等於 1。

## market.index_change_full_scale_pct

將指數當日漲跌幅換算為標準化分數的滿分尺度；數值越小，同樣漲跌幅造成的分數反應越大。

單位：％；預設：`2.0`；0 值：不合法。有效範圍／注意：分數縮放百分比必須大於 0。

## market.direction_full_scale_pct

將指數相對 MA／開盤的方向幅度換算為標準化方向分數的滿分尺度。

單位：％；預設：`1.0`；0 值：不合法。有效範圍／注意：分數縮放百分比必須大於 0。

## market.breadth_count_weight

市場廣度分數中，控制上漲家數與下跌家數差異的影響比例。

單位：比率；預設：`0.65`；0 值：此項不計分；同組其他權重須調整以維持總和 1。有效範圍／注意：家數廣度權重 + 成交值廣度權重 必須等於 1。

## market.breadth_turnover_weight

市場廣度分數中，控制上漲／下跌股票成交值資金流差異的影響比例。

單位：比率；預設：`0.35`；0 值：此項不計分；同組其他權重須調整以維持總和 1。有效範圍／注意：家數廣度權重 + 成交值廣度權重 必須等於 1。

## market.panic_taiex_drop_pct

TAIEX 當日跌幅觸及此負值門檻時直接啟動 PANIC 急跌保護。

單位：％；預設：`-2.5`；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：數值；目前未另設數值上下限。

## market.panic_tpex_drop_pct

TPEx 當日跌幅觸及此負值門檻時直接啟動 PANIC 急跌保護。

單位：％；預設：`-3.0`；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：數值；目前未另設數值上下限。

## market.panic_breadth_ratio

市場廣度比率惡化到此門檻時直接啟動 PANIC 急跌保護。

單位：比率；預設：`-0.75`；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：數值；目前未另設數值上下限。

## market.sector_strong_score

類股強度分數達到此值才列入 strong_sectors，供市場與選股流程辨識強勢類股。

單位：分；預設：`20.0`；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：數值；目前未另設數值上下限。

## market.sector_change_full_scale_pct

將類股成交值加權漲跌幅換算為類股強度分數的滿分尺度。

單位：％；預設：`2.0`；0 值：不合法。有效範圍／注意：必須大於 0。

## market.common_stock_only

True 時市場廣度與類股分析只納入普通股，避免 ETF、ETN、權證等商品污染 Breadth／Sector 統計。

單位：開關；預設：`True`；0 值：不適用。有效範圍／注意：True／False。

# 選股

## scanner.momentum_candidate_min_score

動能候選最低分數。

單位：分；預設：`70.0`；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。

## scanner.trend_candidate_min_score

趨勢候選最低分數。

單位：分；預設：`70.0`；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。

## scanner.min_price

候選股價格下限，用來先排除價格過低、不符合策略範圍的股票。

單位：新台幣元／股；預設：`10.0`；0 值：不合法。有效範圍／注意：必須大於 0。最高股價 必須大於 最低股價。

## scanner.max_price

候選股價格上限，用來排除價格過高、單位資金需求過大的股票。

單位：新台幣元／股；預設：`1000.0`；0 值：不合法。有效範圍／注意：必須大於 最低股價。

## scanner.min_volume

候選股最低成交量門檻，用來過濾流動性不足的股票，也作為成交量評分基準。

單位：股；預設：`500000`；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：大於或等於 0。

## scanner.min_turnover

候選股最低成交金額門檻，用來過濾資金承接不足的股票，也作為成交值評分基準。

單位：新台幣元；預設：`500000.0`；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：不得小於 0。

## scanner.min_change

候選股最低漲幅門檻，限定只追蹤已有順勢動能的股票，並作為漲幅評分起點。

單位：％；預設：`1.0`；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：漲幅滿分尺度 必須大於 最低漲幅。

## scanner.min_volume_ratio

量比的最低基準；用於量比評分，啟用量比強制條件時也會作為候選資格門檻。

單位：倍；預設：`1.0`；0 值：不合法。有效範圍／注意：必須大於 0。量比滿分尺度 必須大於 最低量比。

## scanner.min_score

候選總分最低門檻；完成評分後低於此分數的股票不進入最終候選名單。

單位：分；預設：`70.0`；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：0 至 100（含端點）。

## scanner.min_score_coverage

候選評分資料最低覆蓋率，避免在重要欄位缺漏過多時仍進入最終候選。

單位：％；預設：`75.0`；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：必須介於 0～100。

## scanner.active_universe_n

Scanner 接收 Runtime／APIGateway 已建立的 Active Universe Snapshot 後可保留的最大股票數量。Active Universe 可依維護週期重新排序，但單一 Fugle WebSocket Event 不會重建整個 Universe。

單位：檔股票；預設：`500`；0 值：不合法。有效範圍／注意：必須大於 0。

## scanner.top_n

最終輸出的候選股數量，直接決定 GUI 與盤中監控可取得的候選名單上限。

單位：檔股票；預設：`30`；0 值：不合法。有效範圍／注意：必須大於 0。

## scanner.require_market_allow_long

True 時候選資格需配合 Market Context 的 allow_long；False 時不以多方許可作為 Scanner 必要資格 Gate。

單位：開關；預設：`True`；0 值：不適用。有效範圍／注意：True／False。

## scanner.require_day_trade_eligible

True 時只保留明確可現股當沖的股票；資格未知或不可當沖時依核心 fail-closed 規則處理。

單位：開關；預設：`False`；0 值：不適用。有效範圍／注意：True／False。

## scanner.require_volume_ratio

True 時量比必須達到 min_volume_ratio 才具候選資格；False 時量比仍可參與評分但不作強制資格 Gate。

單位：開關；預設：`False`；0 值：不適用。有效範圍／注意：True／False。

## scanner.require_realtime_price

True 時必須存在 MarketState 即時價格才可 trade_ready；行情 stale 可留在 Candidate Ranking，但不得因此變成可交易。

單位：開關；預設：`True`；0 值：不適用。有效範圍／注意：True／False。

## scanner.near_high_max_distance_pct

判定『接近盤中高點』的最大距離；目前價距盤中高點不超過此百分比才視為 near high。

單位：％；預設：`1.0`；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：不得小於 0。高點距離零分尺度 必須大於 near-high 布林門檻。

## scanner.near_high_score_zero_distance_pct

盤中高點距離評分的衰減尺度；距離拉大到此值附近時，near-high 評分降至最低。

單位：％；預設：`3.0`；0 值：不合法。有效範圍／注意：必須大於 near-high 布林門檻。

## scanner.change_score_full_pct

漲幅評分的滿分尺度；漲幅從最低門檻增加到此值時逐步提高分數。

單位：％；預設：`5.0`；0 值：不合法。有效範圍／注意：必須大於 最低漲幅。

## scanner.market_rs_full_pct

相對大盤強度的滿分尺度；個股漲幅領先所屬市場越接近此值，該項分數越高。

單位：％；預設：`3.0`；0 值：不合法。有效範圍／注意：Relative Strength 滿分尺度必須大於 0。

## scanner.sector_rs_full_pct

相對類股強度的滿分尺度；個股漲幅領先類股越接近此值，該項分數越高。

單位：％；預設：`2.0`；0 值：不合法。有效範圍／注意：Relative Strength 滿分尺度必須大於 0。

## scanner.volume_ratio_full

量比評分的滿分尺度；量比由最低門檻升到此值時逐步提高量比項目分數。

單位：倍；預設：`3.0`；0 值：不合法。有效範圍／注意：必須大於 最低量比。

## scanner.volume_score_full_multiplier

成交量相對最低成交量門檻的滿分倍數，用來決定成交量分數成長速度。

單位：分；預設：`5.0`；0 值：不合法。有效範圍／注意：量能滿分倍數必須大於 1。

## scanner.turnover_score_full_multiplier

成交金額相對最低成交金額門檻的滿分倍數，用來決定成交值評分成長速度。

單位：分；預設：`10.0`；0 值：不合法。有效範圍／注意：量能滿分倍數必須大於 1。

## scanner.spread_good_pct

買賣價差低於此值時視為流動性良好，價差項可取得較高分數。

單位：％；預設：`0.2`；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：不良價差門檻 必須大於 良好價差門檻。

## scanner.spread_bad_pct

買賣價差達到或超過此值時視為流動性差，價差項分數會降到低點。

單位：％；預設：`1.0`；0 值：不合法。有效範圍／注意：必須大於 良好價差門檻。

## scanner.scanner_round_time_limit_seconds

Runtime 的 Market／Scanner 回合完成時限（秒），用來判斷 cycle_deadline_met 與本輪是否成功；Snapshot-only Scanner 不使用此值等待行情或進行 HTTP 輪詢。

單位：秒；預設：`30.0`；0 值：不合法。有效範圍／注意：必須大於 0。

## scanner.realtime_freshness_max_age_seconds

MarketState Snapshot 的事件新鮮度門檻；以 Fugle event_time 判斷。stale 股票仍可留在 Ranking，但 trade_ready=False。

單位：秒；預設：`60.0`；0 值：不合法。有效範圍／注意：必須大於 0。

## scanner.sector_change_source

指定 Sector Relative Strength 使用的類股變動來源欄位／規則；不得因此讓 Scanner 自行建立外部資料連線。

單位：文字／代碼；預設：`'STOCK_DATA'`；0 值：不適用。有效範圍／注意：僅允許 STOCK_DATA / MARKET_CONTEXT。

# 盤中策略

## intraday.volume_expansion_min

分鐘成交量擴張倍數。當前一分 K 成交量除以前方 volume_window 根平均量；不使用 Scanner 快照冒充分鐘成交量。

單位：倍；預設：`1.5`；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。

## intraday.range_expansion_min

分鐘振幅擴張倍數。當前一分 K 的高低價差除以前方 volume_window 根平均高低價差。基準為零或資料不足時不產生初始突破進場。

單位：倍；預設：`1.3`；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。

## intraday.ema_fast_period

快速 EMA 週期。

單位：根（輸入序列）；預設：`5`；0 值：不合法。有效範圍／注意：正有限數值，小於慢線週期，且不大於 trend_initial_bars。

## intraday.ema_slow_period

慢速 EMA 週期。

單位：根（輸入序列）；預設：`10`；0 值：不合法。有效範圍／注意：正有限數值，大於快線週期。

## intraday.pullback_volume_max

回踩最大量縮倍數。

單位：倍；預設：`0.8`；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。

## intraday.momentum_decay_volume_max

動能衰退成交量門檻。

單位：倍；預設：`0.8`；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。

## intraday.momentum_partial_exit_fraction

動能首次減碼比例。首次動能衰退的減碼比例。整張先取整張；僅一張時會全出，不足一張不送單；非整張殘餘保留供人工處理。成交確認後才標記已減碼。

單位：比率；預設：`0.5`；0 值：不合法。有效範圍／注意：大於 0 且小於 1。

## intraday.momentum_take_profit_pct

Momentum 持倉自訂停利。0 停用；達門檻觸發 MOMENTUM_CUSTOM_TAKE_PROFIT，賣出剩餘可賣整張。報酬率＝(最新價 / CTS 實際平均成交成本 − 1) × 100；不取代原結構、時間與緊急出場。

單位：％；預設：`0.0`；0 值：停用自訂停利。有效範圍／注意：大於或等於 0 的有限數值。

## intraday.trend_take_profit_pct

Trend 持倉自訂停利。0 停用；達門檻觸發 TREND_CUSTOM_TAKE_PROFIT，賣出剩餘可賣整張。報酬率＝(最新價 / CTS 實際平均成交成本 − 1) × 100；不必等待 Trend 失效，原結構與時間出場仍有效。

單位：％；預設：`0.0`；0 值：停用自訂停利。有效範圍／注意：大於或等於 0 的有限數值。

## intraday.structure_buffer_pct

結構停損下緣緩衝百分比。

單位：％；預設：`0.1`；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。

## intraday.emergency_stop_loss_pct

緊急硬停損百分比。依實際成交均價建立最後硬停損。Trading 的值用於正式持倉；Intraday 的值用於訊號展示與缺少既有緊急停損的相容保護。

單位：％；預設：`2.5`；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。

## intraday.trend_rs_min

相對大盤最低強度。

單位：％（漲幅差值為百分點）；預設：`0.0`；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：必須為有限數值。

## intraday.momentum_rs_min

Momentum 個股相對大盤漲幅的最低差值；0 要求不弱於大盤。

單位：％（漲幅差值為百分點）；預設：`0.0`；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：持續進場與健康回檔參數必須是非負有限數值。

## intraday.persistent_bars

持續推升型態的最低觀察 K 棒數。

單位：根（輸入序列）；預設：`3`；0 值：不合法。有效範圍／注意：至少 3 根。

## intraday.persistent_volume_min

持續推升型態所需的最低成交量倍數。

單位：倍；預設：`1.0`；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：持續進場與健康回檔參數必須是非負有限數值。

## intraday.healthy_pullback_pct

健康整理允許的最大價格回落百分比。

單位：％；預設：`0.35`；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：持續進場與健康回檔參數必須是非負有限數值。

## intraday.healthy_pullback_volume_max

健康整理允許的最大成交量倍數。

單位：倍；預設：`1.2`；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：持續進場與健康回檔參數必須是非負有限數值。

## intraday.trend_initial_bars

Trend 首次進場的最低結構觀察 K 棒數。

單位：根（輸入序列）；預設：`7`；0 值：不合法。有效範圍／注意：不得小於 ema_fast_period。

## intraday.timezone

台股盤中行情、K 棒、訊號與交易時段判定所使用的時區；目前固定為 Asia/Taipei。

單位：文字／代碼；預設：`'Asia/Taipei'`；0 值：不適用。有效範圍／注意：目前固定為 Asia/Taipei。

## intraday.bar_interval

盤中訊號的基礎 K 棒週期；目前固定使用 1 分鐘 K 棒。

單位：文字／代碼；預設：`'1m'`；0 值：不適用。有效範圍／注意：目前固定只支援 1m。

## intraday.vwap_method

VWAP 計算方法欄位；Fugle Event 架構下以真實 Trade Event 的成交價與 size 累積 VWAP，K 棒歸屬以 event_time 為唯一事件時間基準。舊累計成交量差分僅保留相容輸入路徑。

單位：文字／代碼；預設：`'TRADE_PRICE_X_CUMULATIVE_VOLUME_DELTA'`；0 值：不適用。有效範圍／注意：目前固定為 TRADE_PRICE_X_CUMULATIVE_VOLUME_DELTA。

## intraday.momentum_1m

計算 1 分鐘價格動能的回看 K 棒數，作為短線方向確認條件。

單位：根（輸入序列）；預設：`1`；0 值：不合法。有效範圍／注意：固定 1。

## intraday.momentum_3m

計算 3 分鐘價格動能的回看 K 棒數，與 1／5 分鐘動能共同確認順勢。

單位：根（輸入序列）；預設：`3`；0 值：不合法。有效範圍／注意：固定 3。

## intraday.momentum_5m

計算 5 分鐘價格動能的回看 K 棒數，作為較慢一層的順勢確認。

單位：根（輸入序列）；預設：`5`；0 值：不合法。有效範圍／注意：固定 5。

## intraday.volume_window

計算成交量加速度時，取前幾根 1 分鐘 K 棒成交量作平均基準。

單位：根（輸入序列）；預設：`6`；0 值：不合法。有效範圍／注意：必須大於 0。

## intraday.breakout_window

判斷短線突破時，用前幾根 K 棒建立近期高點比較基準。

單位：根（輸入序列）；預設：`5`；0 值：不合法。有效範圍／注意：必須大於 0。

## intraday.entry_start_time

允許產生新進場訊號的開始時間；目前為 09:05。09:05 前只累積盤中行情、K 棒、VWAP與動能，不允許新 BUY。

單位：台灣時間 HH:MM:SS；預設：`'09:05:00'`；0 值：不適用。有效範圍／注意：交易時段內，早於 entry_end_time。

## intraday.entry_end_time

允許產生新進場訊號的截止時間；目前為 12:00。超過此時間禁止新增 BUY，但既有持倉仍持續執行停損、停利與出場監控。

單位：台灣時間 HH:MM:SS；預設：`'12:00:00'`；0 值：不適用。有效範圍／注意：晚於 entry_start_time，且早於 exit_time。

## intraday.exit_time

當沖部位尾盤強制出場時間；目前為 13:00。到達此時間後既有當沖部位應產生時間出場訊號，不等待 13:30 收盤。

單位：台灣時間 HH:MM:SS；預設：`'13:00:00'`；0 值：不適用。有效範圍／注意：晚於 entry_end_time，且不晚於 session_end_time。

## intraday.session_start_time

盤中行情有效 Session 的開始時間；目前為 09:00。早於此時間的行情不得納入正式盤中 K 棒與訊號計算。

單位：台灣時間 HH:MM:SS；預設：`'09:00:00'`；0 值：不適用。有效範圍／注意：必須早於 SESSION_END_TIME。

## intraday.session_end_time

盤中行情有效 Session 的結束時間；目前為 13:30。晚於此時間的行情不得納入當日正式盤中訊號計算。

單位：台灣時間 HH:MM:SS；預設：`'13:30:00'`；0 值：不適用。有效範圍／注意：SESSION_START_TIME 必須早於 SESSION_END_TIME。


## intraday.min_bars

盤中訊號開始判斷前所需的最低 1 分鐘 K 棒數，避免資料不足時產生訊號。

單位：根（輸入序列）；預設：`7`；0 值：不合法。有效範圍／注意：必須大於 0。

## intraday.breakout_buffer_pct

突破近期高點時額外要求的價格緩衝，降低剛好碰到高點造成的假突破。

單位：％；預設：`0.05`；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：不得小於 0。

## intraday.max_entry_wave_bars

同一突破波允許進場的最長 K 棒年齡，起點為 0，超過上限拒絕進場。舊設定值 3 首次移轉為 5；其他自訂值保留。失效波須回落並重新累積 breakout_window 根觀察棒，符合原量價／VWAP 條件才建立新波。

單位：根（輸入序列）；預設：`5`；0 值：不合法。有效範圍／注意：必須大於 0。

## intraday.momentum_setup_ttl_seconds

Momentum setup 來源時間有效期限，預設六百秒。逾期禁止新進場；持倉出場邏輯維持作用。

單位：秒；預設：`600.0`；0 值：不合法。有效範圍／注意：策略時間有效期限須為正有限數值。

## intraday.trend_anchor_timeout_seconds

Trend anchor 來源時間有效期限，預設900秒。逾期禁止使用舊 anchor 新進場；不更換持倉策略。

單位：秒；預設：`900.0`；0 值：不合法。有效範圍／注意：策略時間有效期限須為正有限數值。

# 交易／風控

## trading.gap_stress_pct

停損穿價壓力情境的額外價格跌幅百分比，預設二。壓力風險獨立顯示，不冒充實際停損成交價。

單位：％；預設：`2.0`；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：風險參數必須為非負有限數值。

## trading.max_stress_risk_pct

壓力風險占權益上限，零代表不啟用新增限制。正值時同時計入新單、持倉與尚未成交的保留風險。

單位：％；預設：`0.0`；0 值：停用壓力風險上限。有效範圍／注意：風險參數必須為非負有限數值。

## trading.max_correlated_risk_pct

相關曝險占權益上限，零代表不啟用新增限制。啟用後若不同產業的相關係數缺少，拒絕新買入，不自行推估係數。

單位：％；預設：`0.0`；0 值：停用相關曝險上限。有效範圍／注意：風險參數必須為非負有限數值。

## trading.strategy_risk_weights

各策略單筆風險上限的倍率，MOMENTUM 與 TREND 預設均為一；必須大於零且不超過一。 只影響風險允許條件，不改寫策略訊號。降低倍率可能使原本數量無法通過風控，並非自動最佳化部位。

單位：比率對照表；預設：`{"MOMENTUM": 1.0, "TREND": 1.0}`；0 值：不適用。有效範圍／注意：MOMENTUM／TREND 的各權重為有限數值，0 < 權重 ≤ 1。

## trading.strategy_daily_loss_budget_pct

各策略每日已實現成交淨損失預算（包含部分出場損益及回報費稅），占權益百分比；MOMENTUM 與 TREND 預設零，代表不啟用額外策略限制。 總帳戶每日損失 Gate 仍作用，不因本參數為零而停用。

單位：清單；預設：`{"MOMENTUM": 0.0, "TREND": 0.0}`；0 值：不適用。有效範圍／注意：MOMENTUM／TREND 各預算為非負有限百分比；項目值 0 停用該策略每日損失預算。

## trading.sector_correlations

由使用者提供跨產業風險係數，鍵格式為按字典順序排列的兩個產業名稱以 | 連接，值介於零與一。 相同產業按一計算。預設空字典；未經資料校準，不自動填入相關係數。

單位：比率對照表；預設：`{}`；0 值：不適用。有效範圍／注意：相關曝險係數須介於零與一。

## trading.max_stop_distance_pct

最大結構停損距離百分比。進場可執行價格至結構停損的百分比距離上限；超過就拒絕買進，不改寫結構停損來規避限制。

單位：％；預設：`2.5`；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。結構停損距離不得超過緊急停損，緊急停損必須小於百分之百。

## trading.emergency_stop_loss_pct

成交後緊急硬停損百分比。依實際成交均價建立最後硬停損。Trading 的值用於正式持倉；Intraday 的值用於訊號展示與缺少既有緊急停損的相容保護。

單位：％；預設：`2.5`；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。結構停損距離不得超過緊急停損，緊急停損必須小於百分之百。

## trading.max_portfolio_open_risk_pct

總開放風險權益百分比。所有系統持倉與待成交買單的開放風險總和，不得超過帳戶權益乘此百分比。

單位：％；預設：`1.5`；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。

## trading.max_sector_position_count

同類股最多持倉數。同類股系統持倉與新待成交股票檔數上限；缺少分類者統一歸入 UNKNOWN。

單位：檔股票；預設：`2`；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。

## trading.max_entries_per_symbol_per_day

同股票每日最多進場次數。同股票每日完整進場循環上限；依確認成交重建，兩策略合併計數。

單位：次；預設：`2`；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。

## trading.reentry_cooldown_minutes

平倉後重新進場冷卻分鐘。同股票完整平倉後冷卻分鐘數，兩策略共用以免繞過同股限制。

單位：分鐘；預設：`10.0`；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。

## trading.max_consecutive_losses

連續虧損觸發次數。按完整平倉的淨結果累計；部分賣出不算獨立平倉，獲利平倉將連續虧損數歸零。

單位：次；預設：`3`；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。

## trading.loss_cooldown_minutes

連續虧損冷卻分鐘。到達連續虧損門檻後暫停新買進的分鐘數；不阻擋賣出。

單位：分鐘；預設：`30.0`；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。

## trading.max_spread_pct

限制買一／賣一價差；超過此百分比會拒絕進場，避免流動性成本過高。

單位：％；預設：`0.3`；0 值：不合法。有效範圍／注意：必須大於 0。

## trading.max_slippage_pct

限制預估委託價相對目前成交價的滑價幅度；超標會拒絕進場。

單位：％；預設：`0.2`；0 值：不合法。有效範圍／注意：必須大於 0。

## trading.max_chase_pct

限制 CTS 前最新可執行價格相對「本次 signal_price」的短時間追價幅度；超標時避免 signal 產生後到實際送單前又被快速拉高。此參數只負責本次 signal 後的追價，不衡量整個 Breakout Wave 從第一個 anchor 已延伸多少，與 max_breakout_extension_pct 分工且不可互相取代。

單位：％；預設：`0.4`；0 值：不合法。有效範圍／注意：必須大於 0。

## trading.max_entry_day_change_pct

自動 BUY 的參考買價相對昨收漲幅上限，達門檻即拒絕。0 停用；啟用時缺有效昨收拒絕。此為策略門檻，不代表交易所漲停價；不影響 SELL。

單位：％；預設：`7.0`；0 值：停用當日漲幅限制。有效範圍／注意：須為 0 至 100；0 停用當日漲幅限制。

## trading.max_execution_friction_pct

限制價差、滑價與追價三者合計的執行摩擦成本；超標會拒絕進場。

單位：％；預設：`0.5`；0 值：不合法。有效範圍／注意：必須大於 0。

## trading.buy_revalidation_max_quote_age_seconds

BUY 送進 CTS 前以同一份 atomic MarketState 的 Fugle event_time 計算 Event Freshness。名稱保留 quote_age 相容語意，但不得重新發 HTTP Quote request。第一階段先保留 25 秒並記錄實際 event age 分布，再依實盤結果收緊。

單位：秒；預設：`25.0`；0 值：不合法。有效範圍／注意：必須大於 0。

## trading.buy_revalidation_max_book_age_seconds

BUY 送單前依 Books 自己的來源時間驗證；未 Ready、逾時、跨日或未訂閱一律拒絕。接收時間與 Trade 時間不能替代 Book 來源時間。

單位：秒；預設：`3.0`；必須為大於 0 的有限數值。

## trading.max_breakout_extension_pct

限制送單前參考買價相對 entry_wave_anchor_price 的整波延伸。超過即拒絕；不取代訊號追價與 Wave 年齡限制。

單位：％；預設：`0.8`；0 值：不合法。有效範圍／注意：必須大於 0。

## trading.max_entry_trigger_retrace_pct

限制最新成交價相對訊號價的回落。下限為 signal_price × (1 − 參數 / 100)，等於下限可通過；低於下限拒絕。0 不允許跌破訊號價。結構／VWAP／動能與追價條件仍有效；Ask 僅用於驗證與估算，實際送市價單。

單位：％；預設：`1.5`；0 值：不允許跌破訊號價。有效範圍／注意：須介於 0 至 1.5。

## trading.max_buy_price

允許新倉買進的最高股價，避免單股價格超過策略設定的資金範圍。

單位：新台幣元／股；預設：`150.0`；0 值：不合法。有效範圍／注意：必須大於 0。

## trading.max_position_count

同時允許持有的最大股票檔數；達上限後拒絕新增部位。

單位：檔股票；預設：`4`；0 值：不合法。有效範圍／注意：大於 0。

## trading.max_daily_trade_count

單日最多允許的進場交易次數；達上限後停止新增交易。

單位：次；預設：`20`；0 值：不合法。有效範圍／注意：大於 0。

## trading.max_daily_loss_pct

以帳戶權益計算的單日最大虧損百分比；觸及後停止新增買進。

單位：％；預設：`2.0`；0 值：不合法。有效範圍／注意：必須大於 0。

## trading.max_daily_loss_amount

單日固定金額虧損上限；設為 0 表示不另外啟用固定金額門檻。

單位：新台幣元；預設：`0.0`；0 值：不另設固定金額虧損上限。有效範圍／注意：大於或等於 0。

## trading.max_single_position_pct

單一新倉最多可使用的帳戶權益比例，與可用現金及其他上限共同決定資金預算。

單位：％；預設：`30.0`；0 值：不合法。有效範圍／注意：必須介於 0～100。必須大於 0。

## trading.max_single_position_amount

單一新倉固定金額上限；設為 0 表示不另外限制固定金額。

單位：新台幣元；預設：`150000.0`；0 值：不另設固定金額上限。有效範圍／注意：大於或等於 0。

## trading.max_risk_per_trade_pct

單筆交易最多可承擔的帳戶權益風險比例，搭配停損幅度反推可買股數。

單位：％；預設：`0.5`；0 值：不合法。有效範圍／注意：必須大於 0。

## trading.cash_utilization_pct

計算新倉資金預算時，可動用 available_cash 的最高比例。

單位：％；預設：`95.0`；0 值：不合法。有效範圍／注意：必須介於 0～100。必須大於 0。

## trading.max_position_shares

單一部位最多允許的股數；設為 0 表示不另外設股數上限。

單位：股；預設：`0`；0 值：不另設股數上限。有效範圍／注意：不得小於 0。

## trading.lot_size

整張交易的每張股數，用於把可買股數取整並換算 CTS 委託數量。

單位：股；預設：`1000`；0 值：不合法。有效範圍／注意：固定 1000 股。

## trading.allow_odd_lot_entry

固定為 False。所有 BUY／SELL 均只允許整張；設為 True 將無法通過參數驗證。

單位：開關；預設：`False`；0 值：不適用。有效範圍／注意：固定 False。

## trading.require_bid_ask

True 時 BUY 必須有有效 bid／ask 才允許評估 Spread／Slippage；Revalidation 使用同一份 atomic MarketState，不得混用不同事件時間的委買委賣。

單位：開關；預設：`True`；0 值：不適用。有效範圍／注意：True／False。

## trading.require_day_trade_eligible

True 時新 BUY 必須明確具現股當沖資格；未知資格依 fail-closed 規則拒絕。

單位：開關；預設：`True`；0 值：不適用。有效範圍／注意：True／False。

## trading.require_complete_account_state

True 時 BUY 必須具備 available_cash、daily_trade_count、daily_pnl、positions 等必要帳務輸入；Trading 不自行猜測。

單位：開關；預設：`True`；0 值：不適用。有效範圍／注意：True／False。

## trading.cts_order_type

送入 CTSOrderRequest 的 order_type 代碼，控制下單交易類型；GUI 不自行解讀或改寫此代碼。

單位：代碼；預設：`0`；0 值：現股。有效範圍／注意：僅允許 0/1/2/16。

## trading.cts_price_type

正式自動／手動 BUY、SELL 固定使用市價代碼 4，價格字串為 0；舊設定載入時移轉為 4。

單位：代碼；預設：`4`；0 值：不合法，正式執行不接受限價。有效範圍／注意：固定 4；舊設定載入時自動改為市價。

## trading.cts_condition

市價委託條件：0＝ROD、1＝FOK、2＝IOC。委託未結束前保留鎖；IOC／FOK 的未成交餘量須完成對帳才可再次處理。

單位：代碼；預設：`0`；0 值：ROD。有效範圍／注意：僅允許 0=ROD / 1=FOK / 2=IOC。




