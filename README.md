# Momentum Scalping 台新獨立專案

此分支適配台新 Core，與金融助理／AlphaDesk 的程式、設定、資料庫和登入工作階段分開。
目前提供**唯讀驗收版**，正式自動下單仍未啟用。

## 本機驗收
交付資料夾已準備獨立 vendor 與 Python 啟動路徑。GitHub 與來源 ZIP 不包含券商 SDK、憑證、帳密或個人資料。

1. 在檔案總管雙擊 Launch_READONLY_WEB.cmd，保持服務視窗開啟。
2. 開啟 http://127.0.0.1:8788/ 。台新帳密只在該本機頁面輸入。
3. 登入後查庫存完成狀態，並觀察委託／成交回報。
4. 網頁打不開時，在服務視窗仍開著的情況下執行 Check_LOCAL_WEB.cmd。
   診斷只測本機 HTTP、程序狀態與隔離環境，不登入券商；結果保存在 private/web-diagnostics.json。
5. 也可使用 Launch_READONLY.cmd 開啟獨立 Tk 唯讀視窗。

來源副本使用者需自行安裝官方 MasterTradePy 及依賴到本專案 vendor；
python_path.txt 可放本機 Python 3.12 的執行檔路徑。此檔與 vendor 不上傳。
不需要調整 PowerShell 執行政策。

## 已完成的程式
- 原策略、市場判定、選股與風控來源保留。
- 普通現股整張張數轉股數、市價與 ROD/FOK/IOC 預覽；當沖旗標需明確指定。
- 原策略長意圖 ID 保留，32 字元券商標記由雜湊產生；SQLite 意圖紀錄會核對碰撞、重複及送單前邊界。
- 獨立 SDK worker 支援唯讀登入、庫存任務回覆與回報觀測。
- 庫存須以同連線世代、同 WorkID、原生 rowCount 與實收筆數一致才判定完成。
  空白、逾時、重複、缺欄、斷線與舊連線回呼都不能當成完整零庫存。
- 委託／成交回報快取一律標記完整性未驗證，不用安靜幾秒或回傳空列表猜測回補完成。
- 本機網頁只綁定 127.0.0.1，檢查 Host、Origin、工作階段與 CSRF；無公開外網服務、無送單端點。
- 原生 SDK stdout/stderr 與 Python 日誌不當成回傳資料；驗收記錄只保留分類結果，不保存帳號、密碼、持股或回報原文。
- 庫存相容介面只映射已確認普通現股欄位；零股或信用欄位非零時拒絕推測合併。可賣量仍未驗證。
- 正式送單硬性停用。SQLite 送單紀錄尚未接到真實下單 transport，不能視為已可交易。

## 驗證
```powershell
python run_offline_checks.py
python taishin_readonly_worker.py --sdk-root vendor --check-sdk
python taishin_readonly_ui.py --smoke-test
python taishin_preview.py --day-trade Y --symbol 2330 --lots 2
```
目前新增 71 項 unittest、原六模組 120 項自我檢查通過；網頁 JavaScript 語法檢查、Tk 啟動與獨立 SDK 載入檢查通過。
測試含假 SDK、測試用本機 HTTP 和 SQLite，並未驗證真實券商登入、成交或對帳。

## 已知阻塞與上線條件
新版 8788 已由使用者驗證 SELF_CHECK_OK 與網頁成功開啟，桌面服務 appcontainer=false、HTTP 200。
目前進入券商唯讀登入階段，收到 BROKER_HOST_NOT_READY；尚未取得台新庫存。
開發工具環境確認為 Windows AppContainer；其同程序回環 HTTP 成功、連桌面服務逾時。
這不證明使用者桌面的程式也在相同隔離中，須由 Check_LOCAL_WEB.cmd 的桌面診斷結果定位。
啟動訊息、寫入 runtime.json、SDK 載入或單元測試均不等於端到端驗收成功。

正式上線前仍須完成：可用本機網頁與真實唯讀登入／庫存證據、委託回補完成判定、
庫存可賣量、持久化意圖與真實送單回報串接、當沖資格／旗標、費稅與資金／日損風控、
行情授權與斷線重連、獨立 GUI/Runtime 整合、驗證環境端到端及限額正式試運行。
不能因本週或下週的目標日期而跳過上述驗收。

官方文件：https://mlapi.tssco.com.tw/web_api/service/document/python-trade

登入診斷更新：登入中 IN_PROGRESS／RECONNECTED 不再直接永久失效；SDK 返回後最多額外等候 8 秒明確就緒，真正錯誤仍封鎖。登入後重連仍使原連線失效，需重新核對。失敗時只保存授權結果、原生連線／應用就緒旗標、去識別事件分類及回呼錯誤類型，不保存原始訊息。連線程序 build=readonly-20261027-r3；重新整理頁面後「重新載入連線程序」會結束舊程序並建立新程序，啟動紀錄可核對 build。


2026-10-28 更新：r4 加入持續心跳、服務／worker 版本核對與正式／驗證環境分開證據；詳細現況以 [TAISHIN_PROGRESS.md](TAISHIN_PROGRESS.md) 為準。

- [原作者與目前參數對照（167 欄）](PARAMETERS_GUIDE.md)
- [期貨模擬連線準備與所需資料](FUTURES_PREPARATION.md)


期貨登入驗收入口：雙擊 Launch_FUTURES_VALIDATION.cmd，再開 http://127.0.0.1:8790/ 。可選驗證／正式唯讀登入，兩者都沒有送單。實際 SDK 與 32 位元 runtime 已在本機交付；來源 ZIP 不包含 runtime。請先閱讀 FUTURES_PREPARATION.md 的限制。


桌面版入口：Momentum.exe，或已建立的桌面捷徑「Momentum 台新期貨」。不再需要 CMD。詳見 [桌面 APP 說明](DESKTOP_APP_GUIDE.md)。介面與桌面啟動完成，正式交易連線與送單仍未驗收或開啟。


## 原作者基準模式與研究草案

使用者要求永久保留可回到原作者的模式。original_mode/baseline 的 11 個檔案固定於 commit 1dea51bf2592d1e0ffa88b3faf97b267c2756d6d，啟動前逐檔校驗，拒絕改檔與額外可匯入模組；原版介面與原策略僅以獨立離線設定開啟。原作者模式不讀寫台新版設定或 SDK。原版保存分支為 archive/original-author-1dea51bf。

執行 python -I original_mode/original_mode.py --self-test 可驗證基準、運行複本、策略預設、LIVE 拒絕與污染拒絕；直接執行 original_mode/original_mode.py 可開啟原版 GUI，需要 Python 與 Tk。MomentumOriginal.cs 是本機免 CMD 啟動器，使用 Codex 的本機 Python runtime。詳見 [模式說明](original_mode/README_原作者模式.md)。

[波段規則草案](SWING_RULES_DRAFT.md) 與其參數檔是未實作／未回測的訊號研究規格，不會改動原策略或啟動交易。[原作者一日模擬計畫](ORIGINAL_PAPER_DAY.md) 尚等待完整行情與資料適配，不代表已完成一整天模擬或取得績效。

### 股票行情前置檢查

`taishin_quote_probe.py` 是獨立唯讀 TWS 行情診斷，最多檢查 5 檔股票；不接交易 SDK、不修改原作者基準。需獨立安裝官方 PY_TradeD 與 Configs，尚待真實帳戶連線；詳見 [QUOTE_PROBE.md](./QUOTE_PROBE.md)。
