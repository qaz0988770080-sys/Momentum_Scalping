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
目前新增 59 項 unittest、原六模組 120 項自我檢查通過；網頁 JavaScript 語法檢查、Tk 啟動與獨立 SDK 載入檢查通過。
測試含假 SDK、測試用本機 HTTP 和 SQLite，並未驗證真實券商登入、成交或對帳。

## 已知阻塞與上線條件
使用者已手動啟動服務並看到啟動訊息，但一般瀏覽器及內建瀏覽器仍回報無法連線。
開發工具環境確認為 Windows AppContainer；其同程序回環 HTTP 成功、連桌面服務逾時。
這不證明使用者桌面的程式也在相同隔離中，須由 Check_LOCAL_WEB.cmd 的桌面診斷結果定位。
啟動訊息、寫入 runtime.json、SDK 載入或單元測試均不等於端到端驗收成功。

正式上線前仍須完成：可用本機網頁與真實唯讀登入／庫存證據、委託回補完成判定、
庫存可賣量、持久化意圖與真實送單回報串接、當沖資格／旗標、費稅與資金／日損風控、
行情授權與斷線重連、獨立 GUI/Runtime 整合、驗證環境端到端及限額正式試運行。
不能因本週或下週的目標日期而跳過上述驗收。

官方文件：https://mlapi.tssco.com.tw/web_api/service/document/python-trade
