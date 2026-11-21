# 專案持續規則

## 原作者基準不得污染

使用者要求永久保留可回到原作者的模式。原始基準為 commit 1dea51bf2592d1e0ffa88b3faf97b267c2756d6d，GitHub archive/original-author-1dea51bf 保存分支不得用來開發新功能或移動到台新版。

original_mode/baseline/ 的 11 個檔案必須保持與該 commit byte-for-byte 相同。不得在其內修 bug、加台新接線、改策略、改預設、美化 UI，或加入較高版本同名前綴模組。新功能放在台新版或新的策略模組。除非使用者明確要求升級原始基準，否則不得改 EXPECTED 或 manifest 來掩蓋雜湊差異。

original_mode/original_mode.py 僅為外部啟動、校驗與離線保護層。原作者模式的設定、日誌、重播及執行複本與台新、證券 SDK、其他金融專案分離。每次啟動先檢查來源雜湊；來源或多餘模組不符時 fail closed。不要讓原作者基準模式連接券商或實單。

涉及原作者入口、封存或打包時，執行 original_mode/original_mode.py --self-test；任何基準來源變動都必須是明確使用者要求並可追溯，不可自行更新來源基準。

## 模擬與策略狀態

SWING_RULES_DRAFT 是未回測、未實作的訊號規則提案，不代表可下單或具獲利能力。ORIGINAL_PAPER_DAY 是一日模擬驗收計畫，目前等待真實行情資料／資料適配。不得以自我測試、假資料或空跑宣稱已完成一整天真實行情模擬。

正式期貨下單仍須券商核准、帳務／行情／交易 transport 驗收；live_enabled 不是可直接切換的產品功能。
