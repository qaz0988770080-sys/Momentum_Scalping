# 參數對照與調整位置

核對日期：2026-10-28。原作者基準 commit：1dea51bf2592d1e0ffa88b3faf97b267c2756d6d。

已用 Git blob SHA 核對四個策略模組、params_v1_9.py 與 params_v2_0.md，六檔與基準完全相同。本表直接讀取參數類別的預設值；原文件的預設若不同，以程式為準。

「目前程式預設」不是記憶體生效值。唯讀驗收網頁目前不啟動股票策略，這些策略參數不影響該網頁。載入自訂參數後，須以策略 GUI 的目前生效值為準。原作者是股票策略，沒有期貨參數預設值。

修改位置：原策略 GUI 的參數設定頁。保存：獨立專案資料夾的 gui_parameters/latest.json；歷史版本：gui_parameters/history/。由 GUI 驗證後套用；修改原始碼預設則必須重新啟動。正式交易的資金、庫存與成交須來自可靠帳務資料。

目前台新網頁可調的是左側正式／驗證環境及登入後的帳戶選擇；切換環境先登出。8788 為服務啟動參數 --port，修改後須重啟並使用相同埠號網址。live_enabled=False 表示目前能力尚未開通實單，不是改成 True 就能交易的設定。

「可調」只表示原股票模型允許設定，不表示已接上台新實單。CTS 代碼不能直接套到期貨；1000 股不能改為 1 來冒充期貨 1 口。

## gui

| 參數／修改位置 | 原作者程式預設 | 目前程式預設 | 調整性質 | 用途、單位與限制 |
|---|---|---|---|---|
| gui.result_expiry_seconds (params_v1_9.py) | 5.0 | 5.0 | 可於原策略參數頁調整，須通過驗證 | 盤中策略計算結果的單調時鐘有效期限，預設五秒，必須大於零且不超過二十五秒。 來源行情新鮮度仍由既有獨立 Gate 判斷，兩者不能互相替代。  單位：秒；0 值：不合法。有效範圍／注意：計算結果有效期限必須大於零且不超過二十五秒。 |
| gui.control_mode (params_v1_9.py) | "MANUAL" | "MANUAL" | 可於原策略參數頁調整，須通過驗證 | 啟動安全預設為 MANUAL；僅允許 AUTO / MANUAL。切換 AUTO 必須由使用者主動操作。  單位：文字／代碼；0 值：不適用。有效範圍／注意：僅允許 AUTO / MANUAL。 |
| gui.api_mode (params_v1_9.py) | "LIVE" | "LIVE" | 可於原策略參數頁調整，須通過驗證 | 僅允許 LIVE / OFFLINE；LIVE 使用即時 API，OFFLINE 僅讀取重播資料。  單位：文字／代碼；0 值：不適用。有效範圍／注意：僅允許 LIVE / OFFLINE。 |
| gui.replay_dir (params_v1_9.py) | "api_replay" | "api_replay" | 可於原策略參數頁調整，須通過驗證 | 不得為空；作為行情與交易重播資料保存目錄。  單位：文字／代碼；0 值：不適用。有效範圍／注意：不得為空。 |
| gui.market_refresh_seconds (params_v1_9.py) | 30.0 | 30.0 | 可於原策略參數頁調整，須通過驗證 | 必須大於 0；Runtime 執行 Market／Active Universe／Scanner 維護週期，單位為秒。這不是 Fugle Trade Event 的取得頻率，單筆 WebSocket Event 不會因本參數而等待。  單位：秒；0 值：不合法。有效範圍／注意：必須大於 0。 |
| gui.quote_refresh_seconds (params_v1_9.py) | 3.0 | 3.0 | 可於原策略參數頁調整，須通過驗證 | 必須大於 0；僅控制 GUI 畫面與狀態顯示刷新週期，單位為秒。策略行情由 Fugle WebSocket Event 驅動，Scanner／Intraday／Trading 的事件計算不得使用此值作為行情取得頻率。  單位：秒；0 值：不合法。有效範圍／注意：必須大於 0。 |
| gui.intraday_watch_count (params_v1_9.py) | 30 | 30 | 可於原策略參數頁調整，須通過驗證 | 必須大於 0；只限制候選股票的盤中監控數量。所有已持倉股票不受此上限，必須持續納入行情與出場監控。  單位：檔股票；0 值：不合法。有效範圍／注意：必須大於 0。 |
| gui.auto_execute (params_v1_9.py) | false | false | 可於原策略參數頁調整，須通過驗證 | 安全預設為 False；只有 AUTO 模式且此項為 True 時才允許自動送出已通過風控的交易。  單位：開關；0 值：不適用。有效範圍／注意：True／False。 |
| gui.fugle_websocket_enabled (params_v1_9.py) | true | true | 可於原策略參數頁調整，須通過驗證 | 預設 True；控制 Runtime 是否啟用 Fugle WebSocket 盤中事件流。此欄位屬 Stream 設定，不保存 Fugle API Token。Runtime 已接線；關閉時由行情 Worker 停止 WebSocket，CTS 連線獨立保留。  單位：開關；0 值：不適用。有效範圍／注意：True／False。 |
| gui.fugle_rest_seed_enabled (params_v1_9.py) | true | true | 可於原策略參數頁調整，須通過驗證 | 預設 True；控制新加入訂閱的股票是否由 Fugle REST 建立初始化 Snapshot；WebSocket 先訂閱，再執行 seed，逐筆事件持續更新。REST 只負責 seed／snapshot，不取代事件驅動。Runtime 已接線；同交易日已 seed 的股票不重複抓取，seed 不覆蓋已接受的逐筆價格。  單位：開關；0 值：不適用。有效範圍／注意：True／False。 |
| gui.fugle_event_freshness_seconds (params_v1_9.py) | 3.0 | 3.0 | 可於原策略參數頁調整，須通過驗證 | 對應 APIGateway MarketState 的一般 Event Freshness 門檻。這是 Fugle event_time 的新鮮度基準，不是 GUI 輪詢秒數，也不等於 Trading BUY Revalidation 的 25 秒門檻。  單位：秒；0 值：不合法。有效範圍／注意：必須大於 0。 |
| gui.fugle_stream_health_timeout_seconds (params_v1_9.py) | 30.0 | 30.0 | 可於原策略參數頁調整，須通過驗證 | Stream Health 用來判斷 WebSocket 是否長時間沒有健康訊號／事件。它屬連線健康監控，不得直接改寫 ENTRY、Momentum、Volume 或 Breakout 策略公式。獨立 StreamHealthWorker 每秒檢查健康狀態；失聯時由 Gateway 依重連間隔重試，不占用 CTS Worker。  單位：秒；0 值：不合法。有效範圍／注意：必須大於 0。 |
| gui.fugle_reconnect_min_seconds (params_v1_9.py) | 30.0 | 30.0 | 可於原策略參數頁調整，須通過驗證 | Fugle WebSocket 斷線後重連 backoff 的最小等待秒數。舊設定低於此值時，Gateway 實際執行仍至少等待 30 秒。  單位：秒；0 值：不合法。有效範圍／注意：必須大於 0。fugle_reconnect_max_seconds 不得小於 fugle_reconnect_min_seconds。 |
| gui.fugle_reconnect_max_seconds (params_v1_9.py) | 60.0 | 60.0 | 可於原策略參數頁調整，須通過驗證 | Fugle WebSocket 指數退避重連的最大等待秒數，不得小於 fugle_reconnect_min_seconds。Gateway 退避上限至少為 60 秒；明確設定更長間隔時保留該設定。  單位：秒；0 值：不合法。有效範圍／注意：不得小於 fugle_reconnect_min_seconds。 |
| gui.fugle_subscription_limit (params_v1_9.py) | 600 | 600 | 可於原策略參數頁調整，須通過驗證 | 500 檔股票池之外保留 100 個名額供持倉／既有候選使用。系統內部訂閱安全上限，用來限制 Runtime 同時要求訂閱的 symbol 數量。這是專案內部政策欄位，不宣稱等同 Fugle 官方方案上限；Runtime 已依 P0 持倉、P1 候選、P2 Universe 順序裁切；移出名單的股票依官方訂閱 ID 取消。P0 數量超過上限時列出未訂閱持倉。  單位：檔股票；0 值：不合法。有效範圍／注意：必須大於 0。 |
| gui.fugle_candidate_subscription_enabled (params_v1_9.py) | true | true | 可於原策略參數頁調整，須通過驗證 | 預設 True；控制 Candidate／P1 股票是否加入 Fugle Trade 與 Books 訂閱集合。P0 持倉監控不應因 Candidate 設定或 Candidate List 淘汰而停止。Runtime 已接線；持倉變更會通知行情 Worker 更新 P0，不等待下次 Scanner 輪次。  單位：開關；0 值：不適用。有效範圍／注意：True／False。 |
| gui.fugle_books_subscription_mode (params_v1_9.py) | "P0_P1" | "P0_P1" | 可於原策略參數頁調整，須通過驗證 | 固定 P0_P1：持倉與 Scanner 候選訂閱 Books；Universe 保留 Trades。舊 OFF／P0／ALL 設定載入時轉為 P0_P1。退出後立即失效，再加入需等待新 Book。  單位：文字／代碼；0 值：不適用。有效範圍／注意：僅允許 OFF / P0 / P0_P1 / ALL。 |
| gui.manual_market_state (params_v1_9.py) | "NORMAL" | "NORMAL" | 可於原策略參數頁調整，須通過驗證 | MANUAL 市場狀態；僅允許 BULL / NORMAL / WEAK / PANIC。  單位：文字／代碼；0 值：不適用。有效範圍／注意：BULL／NORMAL／WEAK／PANIC。 |
| gui.manual_trading_mode (params_v1_9.py) | "SELECTIVE_LONG" | "SELECTIVE_LONG" | 可於原策略參數頁調整，須通過驗證 | MANUAL 交易模式；僅允許 TREND / SELECTIVE_LONG / DEFENSIVE / OVERSOLD_MEAN_REVERSION / STOP。  單位：文字／代碼；0 值：不適用。有效範圍／注意：TREND／SELECTIVE_LONG／DEFENSIVE／OVERSOLD_MEAN_REVERSION／STOP。 |
| gui.available_cash (params_v1_9.py) | 0.0 | 0.0 | 帳務輸入，正式使用須取得可靠帳務資料 | 不得小於 0；由 GUI／主程式提供給 trading.py 作為可用現金風控輸入。  單位：新台幣元；0 值：沒有可用交易預算，無法買進。有效範圍／注意：大於或等於 0；買進須有足夠交易預算。 |
| gui.equity (params_v1_9.py) | 0.0 | 0.0 | 帳務輸入，正式使用須取得可靠帳務資料 | 不得小於 0；由 GUI／主程式提供給 trading.py 作為帳戶權益與風險計算輸入。  單位：新台幣元；0 值：買進資金換算以可用預算補足；自動策略仍須通過權益風控。有效範圍／注意：大於或等於 0。 |
| gui.daily_trade_count (params_v1_9.py) | 0 | 0 | 帳務輸入，正式使用須取得可靠帳務資料 | 不得小於 0；由 GUI／主程式提供給 trading.py 檢查每日交易次數。  單位：次；0 值：尚無設定的當日交易次數。有效範圍／注意：不得小於 0。 |
| gui.daily_pnl (params_v1_9.py) | 0.0 | 0.0 | 帳務輸入，正式使用須取得可靠帳務資料 | 必須為有限數值；由 GUI／主程式提供給 trading.py 檢查每日損益限制。  單位：新台幣元；0 值：設定損益為零。有效範圍／注意：必須是有限數值。 |
| gui.cts_trade_das (params_v1_9.py) | "api.win168.com.tw/tradedas" | "api.win168.com.tw/tradedas" | 舊中信欄位；台新不直接使用此連線設定或代碼 | CTS 登入主機；只用於登入連線，不是證券帳號。  單位：文字／代碼；0 值：不適用。有效範圍／注意：有效的 CTS 伺服器位址。 |
| gui.cts_uid (params_v1_9.py) | "" | "" | 舊中信欄位；台新不直接使用此連線設定或代碼 | CTS 登入帳號／身分證字號；可保存以便下次帶入，交易密碼不屬於本設定且永不保存。  單位：文字／代碼；0 值：不適用。有效範圍／注意：文字，登入時必須提供有效身分。 |
| gui.cts_account_id (params_v1_9.py) | "" | "" | 舊中信欄位；台新不直接使用此連線設定或代碼 | 證券帳號；可留空，登入後只有在可唯一判定時才由 CTS 帳號清單自動選取。  單位：文字／代碼；0 值：不適用。有效範圍／注意：文字，可空白；登入後需確認實際帳號。 |
| gui.cts_com_prog_id (params_v1_9.py) | "" | "" | 舊中信欄位；台新不直接使用此連線設定或代碼 | CTS COM ProgID；留空時使用 api_gateway.py 的預設設定，必要時才覆寫。  單位：文字／代碼；0 值：不適用。有效範圍／注意：文字，可空白採閘道預設。 |

## market

| 參數／修改位置 | 原作者程式預設 | 目前程式預設 | 調整性質 | 用途、單位與限制 |
|---|---|---|---|---|
| market.trend_breadth_min (market_v1_6.py) | 0.0 | 0.0 | 可於原策略參數頁調整，須通過驗證 | 趨勢做多最低市場廣度。  單位：比率；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：必須為有限數值。 |
| market.trend_pullback_score_min (market_v1_6.py) | 30.0 | 30.0 | 可於原策略參數頁調整，須通過驗證 | 強勢整理／回檔的最低市場分數。SIDEWAYS／DOWN 須同時通過此分數、廣度及兩指數正漲幅；PANIC、弱勢或資料異常仍禁止進場。  單位：分；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：必須介於 -100 與 100。 |
| market.trend_pullback_breadth_min (market_v1_6.py) | 0.2 | 0.2 | 可於原策略參數頁調整，須通過驗證 | 強勢整理／回檔最低市場廣度 ratio，預設 0.20，範圍 -1 至 1。沿用市場現有 net_ratio 定義，不是上漲家數百分比。 原 trend_breadth_min 仍用於短線 UP 判定；本門檻只用於 SIDEWAYS／DOWN 的條件許可。  單位：比率；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：必須介於 -1 與 1。 |
| market.trend_permission_max_age_seconds (market_v1_6.py) | 60.0 | 60.0 | 可於原策略參數頁調整，須通過驗證 | Trend 市場許可的資料有效秒數。指數檢查來源時間，廣度檢查完整快照取得時間；快取與 GUI 重繪不得延長期限。送單前再確認許可，個股行情新鮮度另行檢查。  單位：秒；0 值：不合法。有效範圍／注意：必須大於 0 且不超過 300 秒。 |
| market.ma_period (market_v1_6.py) | 5 | 5 | 可於原策略參數頁調整，須通過驗證 | 計算 TAIEX／TPEx 移動平均，作為指數短線方向判定的基準週期。  單位：根（輸入序列）；0 值：不合法。有效範圍／注意：必須大於 0。 |
| market.market_score_weight (market_v1_6.py) | 0.55 | 0.55 | 可於原策略參數頁調整，須通過驗證 | 控制市場總分中『指數表現』所占比重；資料缺漏時核心模組會依可用權重重新正規化。  單位：比率；0 值：此項不計分；同組其他權重須調整以維持總和 1。有效範圍／注意：指數權重 + 市場廣度權重 + 類股權重 必須等於 1。 |
| market.breadth_weight (market_v1_6.py) | 0.25 | 0.25 | 可於原策略參數頁調整，須通過驗證 | 控制市場總分中『上漲／下跌家數與資金流』所占比重。  單位：比率；0 值：此項不計分；同組其他權重須調整以維持總和 1。有效範圍／注意：指數權重 + 市場廣度權重 + 類股權重 必須等於 1。 |
| market.sector_weight (market_v1_6.py) | 0.2 | 0.2 | 可於原策略參數頁調整，須通過驗證 | 控制市場總分中『類股強弱』所占比重；類股資料覆蓋不足時會依覆蓋率降低有效權重。  單位：比率；0 值：此項不計分；同組其他權重須調整以維持總和 1。有效範圍／注意：指數權重 + 市場廣度權重 + 類股權重 必須等於 1。 |
| market.bull_threshold (market_v1_6.py) | 30.0 | 30.0 | 可於原策略參數頁調整，須通過驗證 | 市場總分達到此門檻時判為 BULL，進而影響交易模式與多方許可。  單位：分；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：弱勢門檻 必須小於 多頭門檻。 |
| market.bear_threshold (market_v1_6.py) | -30.0 | -30.0 | 可於原策略參數頁調整，須通過驗證 | 市場總分低於此門檻時判為 WEAK；急跌保護仍可不經此門檻直接判為 PANIC。  單位：分；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：必須小於 多頭門檻。 |
| market.disabled_sectors (market_v1_6.py) | [] | [] | 可於原策略參數頁調整，須通過驗證 | 停用類股清單；列入的類股不參與 strong_sectors 與 Sector Strength 聚合。  單位：清單；0 值：不適用。有效範圍／注意：使用 JSON／陣列格式；內容、權重或排序關係由核心模組 validate() 驗證。 |
| market.mode (market_v1_6.py) | "AUTO" | "AUTO" | 可於原策略參數頁調整，須通過驗證 | 市場判定模式；AUTO 使用市場公式自動判斷，MANUAL 允許人工指定 Market State／Trading Mode。  單位：文字／代碼；0 值：不適用。有效範圍／注意：AUTO／MANUAL；MANUAL 須提供市場狀態或交易模式。 |
| market.manual_market_state (market_v1_6.py) | null | null | 可於原策略參數頁調整，須通過驗證 | MANUAL 模式下指定市場狀態；允許 BULL／NORMAL／WEAK／PANIC。AUTO 模式不以此值覆寫公式結果。  單位：次；0 值：不適用。有效範圍／注意：MANUAL 模式至少要提供 manual_market_state 或 manual_trading_mode。僅允許 ['BULL', 'NORMAL', 'PANIC', 'WEAK']。 |
| market.manual_trading_mode (market_v1_6.py) | null | null | 可於原策略參數頁調整，須通過驗證 | MANUAL 模式下指定交易模式；AUTO 模式由 Market State 對應既有 Trading Mode。  單位：次；0 值：不適用。有效範圍／注意：MANUAL 模式至少要提供 manual_market_state 或 manual_trading_mode。僅允許 ['DEFENSIVE', 'OVERSOLD_MEAN_REVERSION', 'SELECTIVE_LONG', 'STOP', 'TREND']。 |
| market.taiex_index_weight (market_v1_6.py) | 0.65 | 0.65 | 可於原策略參數頁調整，須通過驗證 | 合成指數分數與市場動能時，決定 TAIEX 對整體市場判定的影響比例。  單位：比率；0 值：此項不計分；同組其他權重須調整以維持總和 1。有效範圍／注意：TAIEX 權重 + TPEx 權重 必須等於 1。 |
| market.tpex_index_weight (market_v1_6.py) | 0.35 | 0.35 | 可於原策略參數頁調整，須通過驗證 | 合成指數分數與市場動能時，決定 TPEx 對整體市場判定的影響比例。  單位：比率；0 值：此項不計分；同組其他權重須調整以維持總和 1。有效範圍／注意：TAIEX 權重 + TPEx 權重 必須等於 1。 |
| market.index_change_weight (market_v1_6.py) | 0.75 | 0.75 | 可於原策略參數頁調整，須通過驗證 | 單一指數評分中，控制當日漲跌幅對指數分數的影響比例。  單位：比率；0 值：此項不計分；同組其他權重須調整以維持總和 1。有效範圍／注意：指數漲跌權重 + 指數方向權重 必須等於 1。 |
| market.index_direction_weight (market_v1_6.py) | 0.25 | 0.25 | 可於原策略參數頁調整，須通過驗證 | 單一指數評分中，控制相對移動平均／開盤方向對指數分數的影響比例。  單位：比率；0 值：此項不計分；同組其他權重須調整以維持總和 1。有效範圍／注意：指數漲跌權重 + 指數方向權重 必須等於 1。 |
| market.index_change_full_scale_pct (market_v1_6.py) | 2.0 | 2.0 | 可於原策略參數頁調整，須通過驗證 | 將指數當日漲跌幅換算為標準化分數的滿分尺度；數值越小，同樣漲跌幅造成的分數反應越大。  單位：％；0 值：不合法。有效範圍／注意：分數縮放百分比必須大於 0。 |
| market.direction_full_scale_pct (market_v1_6.py) | 1.0 | 1.0 | 可於原策略參數頁調整，須通過驗證 | 將指數相對 MA／開盤的方向幅度換算為標準化方向分數的滿分尺度。  單位：％；0 值：不合法。有效範圍／注意：分數縮放百分比必須大於 0。 |
| market.breadth_count_weight (market_v1_6.py) | 0.65 | 0.65 | 可於原策略參數頁調整，須通過驗證 | 市場廣度分數中，控制上漲家數與下跌家數差異的影響比例。  單位：比率；0 值：此項不計分；同組其他權重須調整以維持總和 1。有效範圍／注意：家數廣度權重 + 成交值廣度權重 必須等於 1。 |
| market.breadth_turnover_weight (market_v1_6.py) | 0.35 | 0.35 | 可於原策略參數頁調整，須通過驗證 | 市場廣度分數中，控制上漲／下跌股票成交值資金流差異的影響比例。  單位：比率；0 值：此項不計分；同組其他權重須調整以維持總和 1。有效範圍／注意：家數廣度權重 + 成交值廣度權重 必須等於 1。 |
| market.panic_taiex_drop_pct (market_v1_6.py) | -2.5 | -2.5 | 可於原策略參數頁調整，須通過驗證 | TAIEX 當日跌幅觸及此負值門檻時直接啟動 PANIC 急跌保護。  單位：％；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：數值；目前未另設數值上下限。 |
| market.panic_tpex_drop_pct (market_v1_6.py) | -3.0 | -3.0 | 可於原策略參數頁調整，須通過驗證 | TPEx 當日跌幅觸及此負值門檻時直接啟動 PANIC 急跌保護。  單位：％；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：數值；目前未另設數值上下限。 |
| market.panic_breadth_ratio (market_v1_6.py) | -0.75 | -0.75 | 可於原策略參數頁調整，須通過驗證 | 市場廣度比率惡化到此門檻時直接啟動 PANIC 急跌保護。  單位：比率；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：數值；目前未另設數值上下限。 |
| market.sector_strong_score (market_v1_6.py) | 20.0 | 20.0 | 可於原策略參數頁調整，須通過驗證 | 類股強度分數達到此值才列入 strong_sectors，供市場與選股流程辨識強勢類股。  單位：分；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：數值；目前未另設數值上下限。 |
| market.sector_change_full_scale_pct (market_v1_6.py) | 2.0 | 2.0 | 可於原策略參數頁調整，須通過驗證 | 將類股成交值加權漲跌幅換算為類股強度分數的滿分尺度。  單位：％；0 值：不合法。有效範圍／注意：必須大於 0。 |
| market.common_stock_only (market_v1_6.py) | true | true | 可於原策略參數頁調整，須通過驗證 | True 時市場廣度與類股分析只納入普通股，避免 ETF、ETN、權證等商品污染 Breadth／Sector 統計。  單位：開關；0 值：不適用。有效範圍／注意：True／False。 |

## scanner

| 參數／修改位置 | 原作者程式預設 | 目前程式預設 | 調整性質 | 用途、單位與限制 |
|---|---|---|---|---|
| scanner.momentum_candidate_min_score (scanner_v2_8.py) | 70.0 | 70.0 | 可於原策略參數頁調整，須通過驗證 | 動能候選最低分數。  單位：分；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。 |
| scanner.trend_candidate_min_score (scanner_v2_8.py) | 70.0 | 70.0 | 可於原策略參數頁調整，須通過驗證 | 趨勢候選最低分數。  單位：分；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。 |
| scanner.min_price (scanner_v2_8.py) | 10.0 | 10.0 | 可於原策略參數頁調整，須通過驗證 | 候選股價格下限，用來先排除價格過低、不符合策略範圍的股票。  單位：新台幣元／股；0 值：不合法。有效範圍／注意：必須大於 0。最高股價 必須大於 最低股價。 |
| scanner.max_price (scanner_v2_8.py) | 1000.0 | 1000.0 | 可於原策略參數頁調整，須通過驗證 | 候選股價格上限，用來排除價格過高、單位資金需求過大的股票。  單位：新台幣元／股；0 值：不合法。有效範圍／注意：必須大於 最低股價。 |
| scanner.min_volume (scanner_v2_8.py) | 500000 | 500000 | 可於原策略參數頁調整，須通過驗證 | 候選股最低成交量門檻，用來過濾流動性不足的股票，也作為成交量評分基準。  單位：股；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：大於或等於 0。 |
| scanner.min_turnover (scanner_v2_8.py) | 500000.0 | 500000.0 | 可於原策略參數頁調整，須通過驗證 | 候選股最低成交金額門檻，用來過濾資金承接不足的股票，也作為成交值評分基準。  單位：新台幣元；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：不得小於 0。 |
| scanner.min_change (scanner_v2_8.py) | 1.0 | 1.0 | 可於原策略參數頁調整，須通過驗證 | 候選股最低漲幅門檻，限定只追蹤已有順勢動能的股票，並作為漲幅評分起點。  單位：％；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：漲幅滿分尺度 必須大於 最低漲幅。 |
| scanner.min_volume_ratio (scanner_v2_8.py) | 1.0 | 1.0 | 可於原策略參數頁調整，須通過驗證 | 量比的最低基準；用於量比評分，啟用量比強制條件時也會作為候選資格門檻。  單位：倍；0 值：不合法。有效範圍／注意：必須大於 0。量比滿分尺度 必須大於 最低量比。 |
| scanner.min_score (scanner_v2_8.py) | 70.0 | 70.0 | 可於原策略參數頁調整，須通過驗證 | 候選總分最低門檻；完成評分後低於此分數的股票不進入最終候選名單。  單位：分；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：0 至 100（含端點）。 |
| scanner.min_score_coverage (scanner_v2_8.py) | 75.0 | 75.0 | 可於原策略參數頁調整，須通過驗證 | 候選評分資料最低覆蓋率，避免在重要欄位缺漏過多時仍進入最終候選。  單位：％；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：必須介於 0～100。 |
| scanner.active_universe_n (scanner_v2_8.py) | 500 | 500 | 可於原策略參數頁調整，須通過驗證 | Scanner 接收 Runtime／APIGateway 已建立的 Active Universe Snapshot 後可保留的最大股票數量。Active Universe 可依維護週期重新排序，但單一 Fugle WebSocket Event 不會重建整個 Universe。  單位：檔股票；0 值：不合法。有效範圍／注意：必須大於 0。 |
| scanner.top_n (scanner_v2_8.py) | 30 | 30 | 可於原策略參數頁調整，須通過驗證 | 最終輸出的候選股數量，直接決定 GUI 與盤中監控可取得的候選名單上限。  單位：檔股票；0 值：不合法。有效範圍／注意：必須大於 0。 |
| scanner.require_market_allow_long (scanner_v2_8.py) | true | true | 可於原策略參數頁調整，須通過驗證 | True 時候選資格需配合 Market Context 的 allow_long；False 時不以多方許可作為 Scanner 必要資格 Gate。  單位：開關；0 值：不適用。有效範圍／注意：True／False。 |
| scanner.require_day_trade_eligible (scanner_v2_8.py) | false | false | 可於原策略參數頁調整，須通過驗證 | True 時只保留明確可現股當沖的股票；資格未知或不可當沖時依核心 fail-closed 規則處理。  單位：開關；0 值：不適用。有效範圍／注意：True／False。 |
| scanner.require_volume_ratio (scanner_v2_8.py) | false | false | 可於原策略參數頁調整，須通過驗證 | True 時量比必須達到 min_volume_ratio 才具候選資格；False 時量比仍可參與評分但不作強制資格 Gate。  單位：開關；0 值：不適用。有效範圍／注意：True／False。 |
| scanner.require_realtime_price (scanner_v2_8.py) | true | true | 可於原策略參數頁調整，須通過驗證 | True 時必須存在 MarketState 即時價格才可 trade_ready；行情 stale 可留在 Candidate Ranking，但不得因此變成可交易。  單位：開關；0 值：不適用。有效範圍／注意：True／False。 |
| scanner.near_high_max_distance_pct (scanner_v2_8.py) | 1.0 | 1.0 | 可於原策略參數頁調整，須通過驗證 | 判定『接近盤中高點』的最大距離；目前價距盤中高點不超過此百分比才視為 near high。  單位：％；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：不得小於 0。高點距離零分尺度 必須大於 near-high 布林門檻。 |
| scanner.near_high_score_zero_distance_pct (scanner_v2_8.py) | 3.0 | 3.0 | 可於原策略參數頁調整，須通過驗證 | 盤中高點距離評分的衰減尺度；距離拉大到此值附近時，near-high 評分降至最低。  單位：％；0 值：不合法。有效範圍／注意：必須大於 near-high 布林門檻。 |
| scanner.change_score_full_pct (scanner_v2_8.py) | 5.0 | 5.0 | 可於原策略參數頁調整，須通過驗證 | 漲幅評分的滿分尺度；漲幅從最低門檻增加到此值時逐步提高分數。  單位：％；0 值：不合法。有效範圍／注意：必須大於 最低漲幅。 |
| scanner.market_rs_full_pct (scanner_v2_8.py) | 3.0 | 3.0 | 可於原策略參數頁調整，須通過驗證 | 相對大盤強度的滿分尺度；個股漲幅領先所屬市場越接近此值，該項分數越高。  單位：％；0 值：不合法。有效範圍／注意：Relative Strength 滿分尺度必須大於 0。 |
| scanner.sector_rs_full_pct (scanner_v2_8.py) | 2.0 | 2.0 | 可於原策略參數頁調整，須通過驗證 | 相對類股強度的滿分尺度；個股漲幅領先類股越接近此值，該項分數越高。  單位：％；0 值：不合法。有效範圍／注意：Relative Strength 滿分尺度必須大於 0。 |
| scanner.volume_ratio_full (scanner_v2_8.py) | 3.0 | 3.0 | 可於原策略參數頁調整，須通過驗證 | 量比評分的滿分尺度；量比由最低門檻升到此值時逐步提高量比項目分數。  單位：倍；0 值：不合法。有效範圍／注意：必須大於 最低量比。 |
| scanner.volume_score_full_multiplier (scanner_v2_8.py) | 5.0 | 5.0 | 可於原策略參數頁調整，須通過驗證 | 成交量相對最低成交量門檻的滿分倍數，用來決定成交量分數成長速度。  單位：分；0 值：不合法。有效範圍／注意：量能滿分倍數必須大於 1。 |
| scanner.turnover_score_full_multiplier (scanner_v2_8.py) | 10.0 | 10.0 | 可於原策略參數頁調整，須通過驗證 | 成交金額相對最低成交金額門檻的滿分倍數，用來決定成交值評分成長速度。  單位：分；0 值：不合法。有效範圍／注意：量能滿分倍數必須大於 1。 |
| scanner.spread_good_pct (scanner_v2_8.py) | 0.2 | 0.2 | 可於原策略參數頁調整，須通過驗證 | 買賣價差低於此值時視為流動性良好，價差項可取得較高分數。  單位：％；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：不良價差門檻 必須大於 良好價差門檻。 |
| scanner.spread_bad_pct (scanner_v2_8.py) | 1.0 | 1.0 | 可於原策略參數頁調整，須通過驗證 | 買賣價差達到或超過此值時視為流動性差，價差項分數會降到低點。  單位：％；0 值：不合法。有效範圍／注意：必須大於 良好價差門檻。 |
| scanner.scanner_round_time_limit_seconds (scanner_v2_8.py) | 30.0 | 30.0 | 可於原策略參數頁調整，須通過驗證 | Runtime 的 Market／Scanner 回合完成時限（秒），用來判斷 cycle_deadline_met 與本輪是否成功；Snapshot-only Scanner 不使用此值等待行情或進行 HTTP 輪詢。  單位：秒；0 值：不合法。有效範圍／注意：必須大於 0。 |
| scanner.realtime_freshness_max_age_seconds (scanner_v2_8.py) | 60.0 | 60.0 | 可於原策略參數頁調整，須通過驗證 | MarketState Snapshot 的事件新鮮度門檻；以 Fugle event_time 判斷。stale 股票仍可留在 Ranking，但 trade_ready=False。  單位：秒；0 值：不合法。有效範圍／注意：必須大於 0。 |
| scanner.sector_change_source (scanner_v2_8.py) | "STOCK_DATA" | "STOCK_DATA" | 可於原策略參數頁調整，須通過驗證 | 指定 Sector Relative Strength 使用的類股變動來源欄位／規則；不得因此讓 Scanner 自行建立外部資料連線。  單位：文字／代碼；0 值：不適用。有效範圍／注意：僅允許 STOCK_DATA / MARKET_CONTEXT。 |
| scanner.sort_rule (scanner_v2_8.py) | [["score", "DESC"], ["momentum", "DESC"], ["turnover", "DESC"], ["symbol", "ASC"], ["market", "ASC"]] | [["score", "DESC"], ["momentum", "DESC"], ["turnover", "DESC"], ["symbol", "ASC"], ["market", "ASC"]] | 可於原策略參數頁調整，須通過驗證 | 原說明未列出；須以此模組 validate() 及使用處確認，不自行推定範圍。 |
| scanner.pre_sort_rule (scanner_v2_8.py) | [["preliminary_score", "DESC"], ["momentum", "DESC"], ["turnover", "DESC"], ["symbol", "ASC"], ["market", "ASC"]] | [["preliminary_score", "DESC"], ["momentum", "DESC"], ["turnover", "DESC"], ["symbol", "ASC"], ["market", "ASC"]] | 可於原策略參數頁調整，須通過驗證 | 原說明未列出；須以此模組 validate() 及使用處確認，不自行推定範圍。 |
| scanner.score_weight (scanner_v2_8.py) | {"change": 0.16, "volume": 0.1, "turnover": 0.15, "volume_ratio": 0.16, "market_relative_strength": 0.18, "sector_relative_strength": 0.14, "near_intraday_high": 0.05, "liquidity": 0.06} | {"change": 0.16, "volume": 0.1, "turnover": 0.15, "volume_ratio": 0.16, "market_relative_strength": 0.18, "sector_relative_strength": 0.14, "near_intraday_high": 0.05, "liquidity": 0.06} | 可於原策略參數頁調整，須通過驗證 | 原說明未列出；須以此模組 validate() 及使用處確認，不自行推定範圍。 |
| scanner.pre_score_weight (scanner_v2_8.py) | {"change": 0.25, "volume": 0.15, "turnover": 0.2, "market_relative_strength": 0.2, "sector_relative_strength": 0.2} | {"change": 0.25, "volume": 0.15, "turnover": 0.2, "market_relative_strength": 0.2, "sector_relative_strength": 0.2} | 可於原策略參數頁調整，須通過驗證 | 原說明未列出；須以此模組 validate() 及使用處確認，不自行推定範圍。 |
| scanner.trend_weight (scanner_v2_8.py) | {"market_relative_strength": 0.3, "sector_relative_strength": 0.25, "volume_ratio": 0.15, "liquidity": 0.2, "near_intraday_high": 0.1} | {"market_relative_strength": 0.3, "sector_relative_strength": 0.25, "volume_ratio": 0.15, "liquidity": 0.2, "near_intraday_high": 0.1} | 可於原策略參數頁調整，須通過驗證 | 原說明未列出；須以此模組 validate() 及使用處確認，不自行推定範圍。 |
| scanner.momentum_weight (scanner_v2_8.py) | {"change": 0.2, "market_relative_strength": 0.25, "volume_ratio": 0.25, "turnover": 0.2, "near_intraday_high": 0.1} | {"change": 0.2, "market_relative_strength": 0.25, "volume_ratio": 0.25, "turnover": 0.2, "near_intraday_high": 0.1} | 可於原策略參數頁調整，須通過驗證 | 原說明未列出；須以此模組 validate() 及使用處確認，不自行推定範圍。 |

## intraday

| 參數／修改位置 | 原作者程式預設 | 目前程式預設 | 調整性質 | 用途、單位與限制 |
|---|---|---|---|---|
| intraday.volume_expansion_min (intraday_v2_1.py) | 1.5 | 1.5 | 可於原策略參數頁調整，須通過驗證 | 分鐘成交量擴張倍數。當前一分 K 成交量除以前方 volume_window 根平均量；不使用 Scanner 快照冒充分鐘成交量。  單位：倍；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。 |
| intraday.range_expansion_min (intraday_v2_1.py) | 1.3 | 1.3 | 可於原策略參數頁調整，須通過驗證 | 分鐘振幅擴張倍數。當前一分 K 的高低價差除以前方 volume_window 根平均高低價差。基準為零或資料不足時不產生初始突破進場。  單位：倍；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。 |
| intraday.ema_fast_period (intraday_v2_1.py) | 5 | 5 | 可於原策略參數頁調整，須通過驗證 | 快速 EMA 週期。  單位：根（輸入序列）；0 值：不合法。有效範圍／注意：正有限數值，小於慢線週期，且不大於 trend_initial_bars。 |
| intraday.ema_slow_period (intraday_v2_1.py) | 10 | 10 | 可於原策略參數頁調整，須通過驗證 | 慢速 EMA 週期。  單位：根（輸入序列）；0 值：不合法。有效範圍／注意：正有限數值，大於快線週期。 |
| intraday.pullback_volume_max (intraday_v2_1.py) | 0.8 | 0.8 | 可於原策略參數頁調整，須通過驗證 | 回踩最大量縮倍數。  單位：倍；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。 |
| intraday.momentum_decay_volume_max (intraday_v2_1.py) | 0.8 | 0.8 | 可於原策略參數頁調整，須通過驗證 | 動能衰退成交量門檻。  單位：倍；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。 |
| intraday.momentum_partial_exit_fraction (intraday_v2_1.py) | 0.5 | 0.5 | 可於原策略參數頁調整，須通過驗證 | 動能首次減碼比例。首次動能衰退的減碼比例。整張先取整張；僅一張時會全出，不足一張不送單；非整張殘餘保留供人工處理。成交確認後才標記已減碼。  單位：比率；0 值：不合法。有效範圍／注意：大於 0 且小於 1。 |
| intraday.momentum_take_profit_pct (intraday_v2_1.py) | 0.0 | 0.0 | 可於原策略參數頁調整，須通過驗證 | Momentum 持倉自訂停利。0 停用；達門檻觸發 MOMENTUM_CUSTOM_TAKE_PROFIT，賣出剩餘可賣整張。報酬率＝(最新價 / CTS 實際平均成交成本 − 1) × 100；不取代原結構、時間與緊急出場。  單位：％；0 值：停用自訂停利。有效範圍／注意：大於或等於 0 的有限數值。 |
| intraday.trend_take_profit_pct (intraday_v2_1.py) | 0.0 | 0.0 | 可於原策略參數頁調整，須通過驗證 | Trend 持倉自訂停利。0 停用；達門檻觸發 TREND_CUSTOM_TAKE_PROFIT，賣出剩餘可賣整張。報酬率＝(最新價 / CTS 實際平均成交成本 − 1) × 100；不必等待 Trend 失效，原結構與時間出場仍有效。  單位：％；0 值：停用自訂停利。有效範圍／注意：大於或等於 0 的有限數值。 |
| intraday.structure_buffer_pct (intraday_v2_1.py) | 0.5 | 0.5 | 可於原策略參數頁調整，須通過驗證 | 結構停損下緣緩衝百分比。  單位：％；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。 |
| intraday.emergency_stop_loss_pct (intraday_v2_1.py) | 2.5 | 2.5 | 可於原策略參數頁調整，須通過驗證 | 緊急硬停損百分比。依實際成交均價建立最後硬停損。Trading 的值用於正式持倉；Intraday 的值用於訊號展示與缺少既有緊急停損的相容保護。  單位：％；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。 |
| intraday.trend_rs_min (intraday_v2_1.py) | 0.0 | 0.0 | 可於原策略參數頁調整，須通過驗證 | 相對大盤最低強度。  單位：％（漲幅差值為百分點）；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：必須為有限數值。 |
| intraday.momentum_rs_min (intraday_v2_1.py) | 0.0 | 0.0 | 可於原策略參數頁調整，須通過驗證 | Momentum 個股相對大盤漲幅的最低差值；0 要求不弱於大盤。  單位：％（漲幅差值為百分點）；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：持續進場與健康回檔參數必須是非負有限數值。 |
| intraday.persistent_bars (intraday_v2_1.py) | 3 | 3 | 可於原策略參數頁調整，須通過驗證 | 持續推升型態的最低觀察 K 棒數。  單位：根（輸入序列）；0 值：不合法。有效範圍／注意：至少 3 根。 |
| intraday.persistent_volume_min (intraday_v2_1.py) | 1.0 | 1.0 | 可於原策略參數頁調整，須通過驗證 | 持續推升型態所需的最低成交量倍數。  單位：倍；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：持續進場與健康回檔參數必須是非負有限數值。 |
| intraday.healthy_pullback_pct (intraday_v2_1.py) | 0.35 | 0.35 | 可於原策略參數頁調整，須通過驗證 | 健康整理允許的最大價格回落百分比。  單位：％；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：持續進場與健康回檔參數必須是非負有限數值。 |
| intraday.healthy_pullback_volume_max (intraday_v2_1.py) | 1.2 | 1.2 | 可於原策略參數頁調整，須通過驗證 | 健康整理允許的最大成交量倍數。  單位：倍；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：持續進場與健康回檔參數必須是非負有限數值。 |
| intraday.trend_initial_bars (intraday_v2_1.py) | 7 | 7 | 可於原策略參數頁調整，須通過驗證 | Trend 首次進場的最低結構觀察 K 棒數。  單位：根（輸入序列）；0 值：不合法。有效範圍／注意：不得小於 ema_fast_period。 |
| intraday.timezone (intraday_v2_1.py) | "Asia/Taipei" | "Asia/Taipei" | 固定值；不能視為一般可調參數 | 台股盤中行情、K 棒、訊號與交易時段判定所使用的時區；目前固定為 Asia/Taipei。  單位：文字／代碼；0 值：不適用。有效範圍／注意：目前固定為 Asia/Taipei。 |
| intraday.bar_interval (intraday_v2_1.py) | "1m" | "1m" | 固定值；不能視為一般可調參數 | 盤中訊號的基礎 K 棒週期；目前固定使用 1 分鐘 K 棒。  單位：文字／代碼；0 值：不適用。有效範圍／注意：目前固定只支援 1m。 |
| intraday.vwap_method (intraday_v2_1.py) | "TRADE_PRICE_X_CUMULATIVE_VOLUME_DELTA" | "TRADE_PRICE_X_CUMULATIVE_VOLUME_DELTA" | 固定值；不能視為一般可調參數 | VWAP 計算方法欄位；Fugle Event 架構下以真實 Trade Event 的成交價與 size 累積 VWAP，K 棒歸屬以 event_time 為唯一事件時間基準。舊累計成交量差分僅保留相容輸入路徑。  單位：文字／代碼；0 值：不適用。有效範圍／注意：目前固定為 TRADE_PRICE_X_CUMULATIVE_VOLUME_DELTA。 |
| intraday.momentum_1m (intraday_v2_1.py) | 1 | 1 | 固定值；不能視為一般可調參數 | 計算 1 分鐘價格動能的回看 K 棒數，作為短線方向確認條件。  單位：根（輸入序列）；0 值：不合法。有效範圍／注意：固定 1。 |
| intraday.momentum_3m (intraday_v2_1.py) | 3 | 3 | 固定值；不能視為一般可調參數 | 計算 3 分鐘價格動能的回看 K 棒數，與 1／5 分鐘動能共同確認順勢。  單位：根（輸入序列）；0 值：不合法。有效範圍／注意：固定 3。 |
| intraday.momentum_5m (intraday_v2_1.py) | 5 | 5 | 固定值；不能視為一般可調參數 | 計算 5 分鐘價格動能的回看 K 棒數，作為較慢一層的順勢確認。  單位：根（輸入序列）；0 值：不合法。有效範圍／注意：固定 5。 |
| intraday.volume_window (intraday_v2_1.py) | 6 | 6 | 可於原策略參數頁調整，須通過驗證 | 計算成交量加速度時，取前幾根 1 分鐘 K 棒成交量作平均基準。  單位：根（輸入序列）；0 值：不合法。有效範圍／注意：必須大於 0。 |
| intraday.breakout_window (intraday_v2_1.py) | 5 | 5 | 可於原策略參數頁調整，須通過驗證 | 判斷短線突破時，用前幾根 K 棒建立近期高點比較基準。  單位：根（輸入序列）；0 值：不合法。有效範圍／注意：必須大於 0。 |
| intraday.entry_start_time (intraday_v2_1.py) | "09:05:00" | "09:05:00" | 可於原策略參數頁調整，須通過驗證 | 允許產生新進場訊號的開始時間；目前為 09:05。09:05 前只累積盤中行情、K 棒、VWAP與動能，不允許新 BUY。  單位：台灣時間 HH:MM:SS；0 值：不適用。有效範圍／注意：交易時段內，早於 entry_end_time。 |
| intraday.entry_end_time (intraday_v2_1.py) | "12:15:00" | "12:15:00" | 可於原策略參數頁調整，須通過驗證 | 允許產生新進場訊號的截止時間；目前為 12:00。超過此時間禁止新增 BUY，但既有持倉仍持續執行停損、停利與出場監控。  單位：台灣時間 HH:MM:SS；0 值：不適用。有效範圍／注意：晚於 entry_start_time，且早於 exit_time。 |
| intraday.exit_time (intraday_v2_1.py) | "13:00:00" | "13:00:00" | 可於原策略參數頁調整，須通過驗證 | 當沖部位尾盤強制出場時間；目前為 13:00。到達此時間後既有當沖部位應產生時間出場訊號，不等待 13:30 收盤。  單位：台灣時間 HH:MM:SS；0 值：不適用。有效範圍／注意：晚於 entry_end_time，且不晚於 session_end_time。 |
| intraday.session_start_time (intraday_v2_1.py) | "09:00:00" | "09:00:00" | 可於原策略參數頁調整，須通過驗證 | 盤中行情有效 Session 的開始時間；目前為 09:00。早於此時間的行情不得納入正式盤中 K 棒與訊號計算。  單位：台灣時間 HH:MM:SS；0 值：不適用。有效範圍／注意：必須早於 SESSION_END_TIME。 |
| intraday.session_end_time (intraday_v2_1.py) | "13:30:00" | "13:30:00" | 可於原策略參數頁調整，須通過驗證 | 盤中行情有效 Session 的結束時間；目前為 13:30。晚於此時間的行情不得納入當日正式盤中訊號計算。  單位：台灣時間 HH:MM:SS；0 值：不適用。有效範圍／注意：SESSION_START_TIME 必須早於 SESSION_END_TIME。 |
| intraday.min_bars (intraday_v2_1.py) | 7 | 7 | 可於原策略參數頁調整，須通過驗證 | 盤中訊號開始判斷前所需的最低 1 分鐘 K 棒數，避免資料不足時產生訊號。  單位：根（輸入序列）；0 值：不合法。有效範圍／注意：必須大於 0。 |
| intraday.breakout_buffer_pct (intraday_v2_1.py) | 0.05 | 0.05 | 可於原策略參數頁調整，須通過驗證 | 突破近期高點時額外要求的價格緩衝，降低剛好碰到高點造成的假突破。  單位：％；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：不得小於 0。 |
| intraday.max_entry_wave_bars (intraday_v2_1.py) | 5 | 5 | 可於原策略參數頁調整，須通過驗證 | 同一突破波允許進場的最長 K 棒年齡，起點為 0，超過上限拒絕進場。舊設定值 3 首次移轉為 5；其他自訂值保留。失效波須回落並重新累積 breakout_window 根觀察棒，符合原量價／VWAP 條件才建立新波。  單位：根（輸入序列）；0 值：不合法。有效範圍／注意：必須大於 0。 |
| intraday.momentum_setup_ttl_seconds (intraday_v2_1.py) | 600.0 | 600.0 | 可於原策略參數頁調整，須通過驗證 | Momentum setup 來源時間有效期限，預設六百秒。逾期禁止新進場；持倉出場邏輯維持作用。  單位：秒；0 值：不合法。有效範圍／注意：策略時間有效期限須為正有限數值。 |
| intraday.trend_anchor_timeout_seconds (intraday_v2_1.py) | 900.0 | 900.0 | 可於原策略參數頁調整，須通過驗證 | Trend anchor 來源時間有效期限，預設900秒。逾期禁止使用舊 anchor 新進場；不更換持倉策略。  單位：秒；0 值：不合法。有效範圍／注意：策略時間有效期限須為正有限數值。 |

## trading

| 參數／修改位置 | 原作者程式預設 | 目前程式預設 | 調整性質 | 用途、單位與限制 |
|---|---|---|---|---|
| trading.gap_stress_pct (trading_v3_9.py) | 2.0 | 2.0 | 可於原策略參數頁調整，須通過驗證 | 停損穿價壓力情境的額外價格跌幅百分比，預設二。壓力風險獨立顯示，不冒充實際停損成交價。  單位：％；0 值：以 0 作為門檻／比率，非另行停用開關。有效範圍／注意：風險參數必須為非負有限數值。 |
| trading.max_stress_risk_pct (trading_v3_9.py) | 0.0 | 0.0 | 可於原策略參數頁調整，須通過驗證 | 壓力風險占權益上限，零代表不啟用新增限制。正值時同時計入新單、持倉與尚未成交的保留風險。  單位：％；0 值：停用壓力風險上限。有效範圍／注意：風險參數必須為非負有限數值。 |
| trading.max_correlated_risk_pct (trading_v3_9.py) | 0.0 | 0.0 | 可於原策略參數頁調整，須通過驗證 | 相關曝險占權益上限，零代表不啟用新增限制。啟用後若不同產業的相關係數缺少，拒絕新買入，不自行推估係數。  單位：％；0 值：停用相關曝險上限。有效範圍／注意：風險參數必須為非負有限數值。 |
| trading.strategy_risk_weights (trading_v3_9.py) | {"MOMENTUM": 1.0, "TREND": 1.0} | {"MOMENTUM": 1.0, "TREND": 1.0} | 可於原策略參數頁調整，須通過驗證 | 各策略單筆風險上限的倍率，MOMENTUM 與 TREND 預設均為一；必須大於零且不超過一。 只影響風險允許條件，不改寫策略訊號。降低倍率可能使原本數量無法通過風控，並非自動最佳化部位。  單位：比率對照表；0 值：不適用。有效範圍／注意：MOMENTUM／TREND 的各權重為有限數值，0 < 權重 ≤ 1。 |
| trading.strategy_daily_loss_budget_pct (trading_v3_9.py) | {"MOMENTUM": 0.0, "TREND": 0.0} | {"MOMENTUM": 0.0, "TREND": 0.0} | 可於原策略參數頁調整，須通過驗證 | 各策略每日已實現成交淨損失預算（包含部分出場損益及回報費稅），占權益百分比；MOMENTUM 與 TREND 預設零，代表不啟用額外策略限制。 總帳戶每日損失 Gate 仍作用，不因本參數為零而停用。  單位：清單；0 值：不適用。有效範圍／注意：MOMENTUM／TREND 各預算為非負有限百分比；項目值 0 停用該策略每日損失預算。 |
| trading.sector_correlations (trading_v3_9.py) | {} | {} | 可於原策略參數頁調整，須通過驗證 | 由使用者提供跨產業風險係數，鍵格式為按字典順序排列的兩個產業名稱以 ／ 連接，值介於零與一。 相同產業按一計算。預設空字典；未經資料校準，不自動填入相關係數。  單位：比率對照表；0 值：不適用。有效範圍／注意：相關曝險係數須介於零與一。 |
| trading.max_stop_distance_pct (trading_v3_9.py) | 2.5 | 2.5 | 可於原策略參數頁調整，須通過驗證 | 最大結構停損距離百分比。進場可執行價格至結構停損的百分比距離上限；超過就拒絕買進，不改寫結構停損來規避限制。  單位：％；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。結構停損距離不得超過緊急停損，緊急停損必須小於百分之百。 |
| trading.emergency_stop_loss_pct (trading_v3_9.py) | 2.5 | 2.5 | 可於原策略參數頁調整，須通過驗證 | 成交後緊急硬停損百分比。依實際成交均價建立最後硬停損。Trading 的值用於正式持倉；Intraday 的值用於訊號展示與缺少既有緊急停損的相容保護。  單位：％；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。結構停損距離不得超過緊急停損，緊急停損必須小於百分之百。 |
| trading.max_portfolio_open_risk_pct (trading_v3_9.py) | 1.5 | 1.5 | 可於原策略參數頁調整，須通過驗證 | 總開放風險權益百分比。所有系統持倉與待成交買單的開放風險總和，不得超過帳戶權益乘此百分比。  單位：％；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。 |
| trading.max_sector_position_count (trading_v3_9.py) | 2 | 2 | 可於原策略參數頁調整，須通過驗證 | 同類股最多持倉數。同類股系統持倉與新待成交股票檔數上限；缺少分類者統一歸入 UNKNOWN。  單位：檔股票；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。 |
| trading.max_entries_per_symbol_per_day (trading_v3_9.py) | 2 | 2 | 可於原策略參數頁調整，須通過驗證 | 同股票每日最多進場次數。同股票每日完整進場循環上限；依確認成交重建，兩策略合併計數。  單位：次；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。 |
| trading.reentry_cooldown_minutes (trading_v3_9.py) | 10.0 | 10.0 | 可於原策略參數頁調整，須通過驗證 | 平倉後重新進場冷卻分鐘。同股票完整平倉後冷卻分鐘數，兩策略共用以免繞過同股限制。  單位：分鐘；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。 |
| trading.max_consecutive_losses (trading_v3_9.py) | 3 | 3 | 可於原策略參數頁調整，須通過驗證 | 連續虧損觸發次數。按完整平倉的淨結果累計；部分賣出不算獨立平倉，獲利平倉將連續虧損數歸零。  單位：次；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。 |
| trading.loss_cooldown_minutes (trading_v3_9.py) | 30.0 | 30.0 | 可於原策略參數頁調整，須通過驗證 | 連續虧損冷卻分鐘。到達連續虧損門檻後暫停新買進的分鐘數；不阻擋賣出。  單位：分鐘；0 值：不合法。有效範圍／注意：必須為有限數值。必須大於零。 |
| trading.max_spread_pct (trading_v3_9.py) | 0.3 | 0.3 | 可於原策略參數頁調整，須通過驗證 | 限制買一／賣一價差；超過此百分比會拒絕進場，避免流動性成本過高。  單位：％；0 值：不合法。有效範圍／注意：必須大於 0。 |
| trading.max_slippage_pct (trading_v3_9.py) | 0.2 | 0.2 | 可於原策略參數頁調整，須通過驗證 | 限制預估委託價相對目前成交價的滑價幅度；超標會拒絕進場。  單位：％；0 值：不合法。有效範圍／注意：必須大於 0。 |
| trading.max_chase_pct (trading_v3_9.py) | 0.4 | 0.4 | 可於原策略參數頁調整，須通過驗證 | 限制 CTS 前最新可執行價格相對「本次 signal_price」的短時間追價幅度；超標時避免 signal 產生後到實際送單前又被快速拉高。此參數只負責本次 signal 後的追價，不衡量整個 Breakout Wave 從第一個 anchor 已延伸多少，與 max_breakout_extension_pct 分工且不可互相取代。  單位：％；0 值：不合法。有效範圍／注意：必須大於 0。 |
| trading.max_entry_day_change_pct (trading_v3_9.py) | 7.0 | 7.0 | 可於原策略參數頁調整，須通過驗證 | 自動 BUY 的參考買價相對昨收漲幅上限，達門檻即拒絕。0 停用；啟用時缺有效昨收拒絕。此為策略門檻，不代表交易所漲停價；不影響 SELL。  單位：％；0 值：停用當日漲幅限制。有效範圍／注意：須為 0 至 100；0 停用當日漲幅限制。 |
| trading.max_execution_friction_pct (trading_v3_9.py) | 0.5 | 0.5 | 可於原策略參數頁調整，須通過驗證 | 限制價差、滑價與追價三者合計的執行摩擦成本；超標會拒絕進場。  單位：％；0 值：不合法。有效範圍／注意：必須大於 0。 |
| trading.buy_revalidation_max_quote_age_seconds (trading_v3_9.py) | 25.0 | 25.0 | 可於原策略參數頁調整，須通過驗證 | BUY 送進 CTS 前以同一份 atomic MarketState 的 Fugle event_time 計算 Event Freshness。名稱保留 quote_age 相容語意，但不得重新發 HTTP Quote request。第一階段先保留 25 秒並記錄實際 event age 分布，再依實盤結果收緊。  單位：秒；0 值：不合法。有效範圍／注意：必須大於 0。 |
| trading.buy_revalidation_max_book_age_seconds (trading_v3_9.py) | 3.0 | 3.0 | 可於原策略參數頁調整，須通過驗證 | BUY 送單前依 Books 自己的來源時間驗證；未 Ready、逾時、跨日或未訂閱一律拒絕。接收時間與 Trade 時間不能替代 Book 來源時間。  單位：秒；必須為大於 0 的有限數值。 |
| trading.max_breakout_extension_pct (trading_v3_9.py) | 0.8 | 0.8 | 可於原策略參數頁調整，須通過驗證 | 限制送單前參考買價相對 entry_wave_anchor_price 的整波延伸。超過即拒絕；不取代訊號追價與 Wave 年齡限制。  單位：％；0 值：不合法。有效範圍／注意：必須大於 0。 |
| trading.max_entry_trigger_retrace_pct (trading_v3_9.py) | 1.5 | 1.5 | 可於原策略參數頁調整，須通過驗證 | 限制最新成交價相對訊號價的回落。下限為 signal_price × (1 − 參數 / 100)，等於下限可通過；低於下限拒絕。0 不允許跌破訊號價。結構／VWAP／動能與追價條件仍有效；Ask 僅用於驗證與估算，實際送市價單。  單位：％；0 值：不允許跌破訊號價。有效範圍／注意：須介於 0 至 1.5。 |
| trading.max_buy_price (trading_v3_9.py) | 150.0 | 150.0 | 可於原策略參數頁調整，須通過驗證 | 允許新倉買進的最高股價，避免單股價格超過策略設定的資金範圍。  單位：新台幣元／股；0 值：不合法。有效範圍／注意：必須大於 0。 |
| trading.max_position_count (trading_v3_9.py) | 4 | 4 | 可於原策略參數頁調整，須通過驗證 | 同時允許持有的最大股票檔數；達上限後拒絕新增部位。  單位：檔股票；0 值：不合法。有效範圍／注意：大於 0。 |
| trading.max_daily_trade_count (trading_v3_9.py) | 20 | 20 | 可於原策略參數頁調整，須通過驗證 | 單日最多允許的進場交易次數；達上限後停止新增交易。  單位：次；0 值：不合法。有效範圍／注意：大於 0。 |
| trading.max_daily_loss_pct (trading_v3_9.py) | 2.0 | 2.0 | 可於原策略參數頁調整，須通過驗證 | 以帳戶權益計算的單日最大虧損百分比；觸及後停止新增買進。  單位：％；0 值：不合法。有效範圍／注意：必須大於 0。 |
| trading.max_daily_loss_amount (trading_v3_9.py) | 0.0 | 0.0 | 可於原策略參數頁調整，須通過驗證 | 單日固定金額虧損上限；設為 0 表示不另外啟用固定金額門檻。  單位：新台幣元；0 值：不另設固定金額虧損上限。有效範圍／注意：大於或等於 0。 |
| trading.max_single_position_pct (trading_v3_9.py) | 30.0 | 30.0 | 可於原策略參數頁調整，須通過驗證 | 單一新倉最多可使用的帳戶權益比例，與可用現金及其他上限共同決定資金預算。  單位：％；0 值：不合法。有效範圍／注意：必須介於 0～100。必須大於 0。 |
| trading.max_single_position_amount (trading_v3_9.py) | 150000.0 | 150000.0 | 可於原策略參數頁調整，須通過驗證 | 單一新倉固定金額上限；設為 0 表示不另外限制固定金額。  單位：新台幣元；0 值：不另設固定金額上限。有效範圍／注意：大於或等於 0。 |
| trading.max_risk_per_trade_pct (trading_v3_9.py) | 0.5 | 0.5 | 可於原策略參數頁調整，須通過驗證 | 單筆交易最多可承擔的帳戶權益風險比例，搭配停損幅度反推可買股數。  單位：％；0 值：不合法。有效範圍／注意：必須大於 0。 |
| trading.cash_utilization_pct (trading_v3_9.py) | 95.0 | 95.0 | 可於原策略參數頁調整，須通過驗證 | 計算新倉資金預算時，可動用 available_cash 的最高比例。  單位：％；0 值：不合法。有效範圍／注意：必須介於 0～100。必須大於 0。 |
| trading.max_position_shares (trading_v3_9.py) | 0 | 0 | 可於原策略參數頁調整，須通過驗證 | 單一部位最多允許的股數；設為 0 表示不另外設股數上限。  單位：股；0 值：不另設股數上限。有效範圍／注意：不得小於 0。 |
| trading.lot_size (trading_v3_9.py) | 1000 | 1000 | 固定值；不能視為一般可調參數 | 整張交易的每張股數，用於把可買股數取整並換算 CTS 委託數量。  單位：股；0 值：不合法。有效範圍／注意：固定 1000 股。 |
| trading.allow_odd_lot_entry (trading_v3_9.py) | false | false | 固定值；不能視為一般可調參數 | 固定為 False。所有 BUY／SELL 均只允許整張；設為 True 將無法通過參數驗證。  單位：開關；0 值：不適用。有效範圍／注意：固定 False。 |
| trading.require_bid_ask (trading_v3_9.py) | true | true | 可於原策略參數頁調整，須通過驗證 | True 時 BUY 必須有有效 bid／ask 才允許評估 Spread／Slippage；Revalidation 使用同一份 atomic MarketState，不得混用不同事件時間的委買委賣。  單位：開關；0 值：不適用。有效範圍／注意：True／False。 |
| trading.require_day_trade_eligible (trading_v3_9.py) | true | true | 可於原策略參數頁調整，須通過驗證 | True 時新 BUY 必須明確具現股當沖資格；未知資格依 fail-closed 規則拒絕。  單位：開關；0 值：不適用。有效範圍／注意：True／False。 |
| trading.require_complete_account_state (trading_v3_9.py) | true | true | 可於原策略參數頁調整，須通過驗證 | True 時 BUY 必須具備 available_cash、daily_trade_count、daily_pnl、positions 等必要帳務輸入；Trading 不自行猜測。  單位：開關；0 值：不適用。有效範圍／注意：True／False。 |
| trading.cts_order_type (trading_v3_9.py) | 0 | 0 | 舊中信欄位；台新不直接使用此連線設定或代碼 | 送入 CTSOrderRequest 的 order_type 代碼，控制下單交易類型；GUI 不自行解讀或改寫此代碼。  單位：代碼；0 值：現股。有效範圍／注意：僅允許 0/1/2/16。 |
| trading.cts_price_type (trading_v3_9.py) | 4 | 4 | 固定值；不能視為一般可調參數 | 正式自動／手動 BUY、SELL 固定使用市價代碼 4，價格字串為 0；舊設定載入時移轉為 4。  單位：代碼；0 值：不合法，正式執行不接受限價。有效範圍／注意：固定 4；舊設定載入時自動改為市價。 |
| trading.cts_condition (trading_v3_9.py) | 0 | 0 | 舊中信欄位；台新不直接使用此連線設定或代碼 | 市價委託條件：0＝ROD、1＝FOK、2＝IOC。委託未結束前保留鎖；IOC／FOK 的未成交餘量須完成對帳才可再次處理。  單位：代碼；0 值：ROD。有效範圍／注意：僅允許 0=ROD / 1=FOK / 2=IOC。 |

總計 167 個參數欄位。

期貨版須另設商品、到期月份、單次與總持倉口數、新倉／平倉、日夜盤、委託類型、停損停利點數、每日損失上限及換月規則。這些尚未實作、尚未選定數值；不沿用股票預設。