# 原作者基準模式

雙擊 MomentumOriginal.exe 開啟原作者原版 GUI。來源固定於 commit 1dea51bf2592d1e0ffa88b3faf97b267c2756d6d。

- baseline/：11 個逐檔核對 Git blob SHA 的原始檔，不放後續台新改寫。
- baseline-manifest.json：原始 Git blob SHA-1 與檔案 SHA-256。
- original-author-1dea51bf.zip：本機封存備份。
- user-data/original-run/：每次啟動由基準重建的執行複本；新加入其他 Python 檔或改動來源會在校驗時拒絕。
- user-data/settings/：原作者模式自己的設定儲存區，不讀寫台新模式設定；每次先載入原作者參數預設，可再明確載入本模式另存的草稿。

此入口使用原作者股票介面與原策略，僅作離線重播／參數檢視，不連接券商。外部啟動邊界設定 OFFLINE、MANUAL、自動執行關閉，並拒絕切換 LIVE；這些保護放在 original_mode.py，不修改原作者封存原始碼。沒有導入台新或證券 SDK，不建立共用 COM 註冊。

台新期貨仍從既有「Momentum 台新期貨」入口使用；新波段規則、台新接線及美化只改台新版本。兩模式都保持自己的資料與設定，不能把原作者股票張數、CTS 代碼直接當作期貨口數與台新代碼。

啟動前先檢查基準與執行複本的雜湊，不符就拒絕啟動，不會悄悄改用台新版。模式自我測試已通過 11 檔基準、11 檔複本、策略預設相同、LIVE 入口拒絕與故意改檔拒絕。

GitHub 原版保存分支：https://github.com/qaz0988770080-sys/Momentum_Scalping/tree/archive/original-author-1dea51bf

若本機封存或執行檔遭刪除，仍需從上述指定 commit／保存分支或備份還原；不能把目前台新版直接改名冒充原版。
