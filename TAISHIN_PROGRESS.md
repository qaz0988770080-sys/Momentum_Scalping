# 台新獨立專案進度 — 2026-10-27（台灣時間）

專案：qaz0988770080-sys/Momentum_Scalping
開發分支：codex/taishin-standalone-adapter
草稿 PR：https://github.com/qaz0988770080-sys/Momentum_Scalping/pull/1
前一階段 commit：665f03541565d1e4e07856e2dfcda1f065cd16e4

本階段加入獨立唯讀 SDK worker、Tk 與本機網頁、明確庫存查詢完成判定、舊世代回呼隔離、
Host/Origin/CSRF 防護、可見服務啟動器、去識別連線診斷，以及持久化意圖與券商標記映射基礎。
未引用金融助理／AlphaDesk 的程式、資料庫、帳密或登入工作階段。
本機另行複製官方 SDK 與通用依賴到獨立 vendor；這些檔案不進 GitHub 或來源交付包。

驗證：
- python run_offline_checks.py：66 項 unittest + 原六模組 120 項自我檢查通過。
- 網頁 JavaScript 語法檢查通過。
- taishin_readonly_ui.py --smoke-test：程序就緒、登入按鈕、查詢停用及子程序存活檢查通過。
- taishin_readonly_worker.py --sdk-root vendor --check-sdk：獨立 SDK 載入成功，沒有建立登入或送單。
- 回歸測試發現 Windows 拒絕 HTTP 請求時未讀完 body 可能重設連線；已改成有界讀取後回應，重新完整測試通過。

真實操作證據與限制：
- 使用者手動執行服務，顯示 starting 與 URL；瀏覽器仍逾時，尚未看到登入頁。
- 開發環境 AppContainer=true；同程序 HTTP 回環 200，但連桌面服務逾時。
- 已交付 Check_LOCAL_WEB.cmd，等待使用者桌面上的連線診斷，不能把開發隔離直接歸因於使用者防火牆。
- 沒有取得真實台新登入、庫存、委託、成交或完整對帳證據；正式交易仍未啟用。
- 意圖紀錄已離線測試，但仍未與真實交易 transport 整合。
- 現有 SDK 的委託／成交查詢為快取觀測，仍缺可靠的回補完成證據。
- 桌面操作工具禁止啟動命令提示字元；需使用者手動執行已準備的診斷啟動檔，不變更系統安全設定。

目標：使用者希望本週（10/27–10/30）或下週前使用。
可用程度以實際端到端驗收為準；目前為唯讀驗收開發版，尚非正式自動交易版。
下一步：定位瀏覽器可達性 → 真實唯讀登入與庫存 → 回補／可賣量 → 驗證環境下單生命週期。

桌面診斷補充：使用者實際執行結果 appcontainer=false、服務程序 running=true、同程序 HTTP 回環 200，但當時 8786 服務逾時。因此無法把問題直接歸因於使用者隔離或防火牆。
啟動修正：改用 8788、Windows 排他監聽、先啟動服務執行緒再取得 HTTP 200 的自我驗證；只有成功才顯示 SELF_CHECK_OK。服務黑窗關閉 QuickEdit 以避免文字選取停頓，僅調整該視窗、不改系統安全政策。新增重複監聽測試，完整 66 項測試與原 120 項檢查通過。開發環境 --port 0 --self-test 實際取得 HTTP 200；等待桌面新版驗收，不宣告問題已解決。

最新現況：使用者已確認 8788 SELF_CHECK_OK 且網頁可開；runtime 記錄 appcontainer=false、http_self_check=200。券商唯讀登入兩次失敗 BROKER_HOST_NOT_READY，未取得庫存。
已修正登入中進行／重連事件被永久失效，以及只做一次主機就緒判斷的問題，新增等待與事件分類。66 項 unittest、原 120 項檢查及 JavaScript 語法檢查通過。加入原生 AP_READY 回呼錯誤分類及 build=readonly-20261027-r3 的啟動證據。
使用者回報重試失敗，但實際紀錄仍為不含 diagnostics 的舊格式；目前等待手動頁面重整與程序重載，以核對新 build 後再進行一次登入。工具讀取頁面成功但工具重新整理遇到連線拒絕，因此不能以工具網頁操作的失敗直接推定桌面服務已停止。
