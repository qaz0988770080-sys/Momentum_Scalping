# 台新期貨驗收進度（2026-10-28）

目前桌面版 r7：114 項單元測試及原六模組 120 項檢查通過。身分認證已有成功回覆；正式交易 API 仍需完成官網驗證及人工核准。簽署完成與正式 API 核准是不同步驟。

## 本機操作

雙擊桌面「Momentum 台新期貨」，不需要 CMD。上方「官方 API 驗證」區會載入已核對官網的測試參數，與 SRF 實單草稿分開。

1. 按「登入驗證環境」，只在 APP 內輸入帳密。
2. 連線及帳務回補完成後，才可送出官網指定的驗證單。
3. 程式等候券商回報，顯示委託書號及受理／成交／拒絕狀態。傳輸成功不等於券商已受理。
4. 將真正的委託書號回填官方驗證頁，再依官網流程送出驗證及等待審核。

正式環境的新單、改單、刪單入口維持封鎖。驗證功能只接受本機固定計畫，在已確認的驗證連線及相符帳戶執行一次；計畫到期、帳戶不符、正式模式、連線未完成都會拒絕。

## 已完成的相容性修正

- Windows 系統身分資訊的編碼相容性。
- SDK 可選 CLR 事件處理器的初始化。
- 期貨程序啟用 TLS 1.2，保留系統憑證驗證；對官方主機握手通過。
- 官方 Speedy 元件採私有 COM activation context，建立原生委託物件已成功，不修改系統註冊表。STA 訊息處理支援連線與回報事件。
- 驅動版本與分階段診斷在 APP 顯示；不保存帳密、原始 SSO 內容或帳戶識別碼。

## 送單紀錄與尚未完成事項

驗證紀錄在 private/futures/verification.sqlite3，先落盤才進入傳輸。重新開啟程式或重複點選不能重送同一計畫；回覆不明時標示未知，先對帳。官方參數及帳戶雜湊只保存在 private/futures/verification-plan.json，不納入 GitHub。

目前尚未送出或驗收真實的驗證委託。正式庫存、保證金、行情及實單傳輸尚未接通／驗收。介面上的持倉提示為使用者提供，不能當作券商確認庫存。原作者的股票策略沒有期貨預設，不會自動套用。

## 官方參考

- [期貨線上驗證](https://mlapi.tssco.com.tw/web_api/service/twftrade-api-testing)
- [Python 期貨 API 文件](https://mlapi.tssco.com.tw/web_api/service/document/python-trade-twf)
- [Core API 申請流程與 FAQ](https://mlapi.tssco.com.tw/web_api/service/home)：認證核准後下一個工作日才可使用正式服務。
- [Microsoft 私有 COM 啟用文件](https://learn.microsoft.com/en-us/windows/win32/sbscs/creating-registration-free-com-objects)
