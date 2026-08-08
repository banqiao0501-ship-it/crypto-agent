# Windows 排程設定（工作排程器 / Task Scheduler）

你是Windows環境，`crontab.example`那份是給Linux/Mac用的，不適用。這份改用Windows內建的
「工作排程器」，用`schtasks`指令一次設定好，不用在GUI裡點來點去。

## 批次檔說明

`scripts/`資料夾裡的4個`.bat`檔案，各自對應一個排程工作，內容都是先切到專案資料夾、
再用虛擬環境的python執行對應指令。這幾個檔案用`%~dp0`自動抓自己的路徑，
你不需要手動改裡面的路徑。

## 設定步驟

1. 先確認`.env`、`venv`都已經設定好，且手動測試過4個指令都能成功執行（前面的步驟）
2. 用「系統管理員身分」打開PowerShell（在開始選單搜尋PowerShell，右鍵選「以系統管理員身分執行」）
   ——排程工作需要系統管理員權限才能建立
3. 把下面每一行的路徑`C:\Users\banqi\crypto-agent`改成你實際的專案路徑，
   然後一行一行貼上執行：

```
schtasks /create /tn "CryptoAgent_YouTube_21" /tr "C:\Users\banqi\crypto-agent\scripts\run_collect_youtube.bat" /sc daily /st 21:00 /f
schtasks /create /tn "CryptoAgent_YouTube_22" /tr "C:\Users\banqi\crypto-agent\scripts\run_collect_youtube.bat" /sc daily /st 22:00 /f
schtasks /create /tn "CryptoAgent_YouTube_23" /tr "C:\Users\banqi\crypto-agent\scripts\run_collect_youtube.bat" /sc daily /st 23:00 /f
schtasks /create /tn "CryptoAgent_Jin10" /tr "C:\Users\banqi\crypto-agent\scripts\run_collect_jin10.bat" /sc minute /mo 30 /f
schtasks /create /tn "CryptoAgent_MarketCheck" /tr "C:\Users\banqi\crypto-agent\scripts\run_market_check.bat" /sc minute /mo 30 /f
schtasks /create /tn "CryptoAgent_DailyReport" /tr "C:\Users\banqi\crypto-agent\scripts\run_daily_report.bat" /sc daily /st 08:00 /f
```

## 驗證有沒有設定成功

```
schtasks /query /tn "CryptoAgent_DailyReport"
```

顯示出工作的狀態、下次執行時間，代表設定成功。也可以打開開始選單搜尋「工作排程器」，
在左側「工作排程器程式庫」裡應該會看到這6個以`CryptoAgent_`開頭的工作。

## 重要提醒

- 這些排程只有在**電腦開機且未進入睡眠狀態**時才會執行，如果電腦晚上會關機或睡眠，
  排在那個時段的工作就不會跑。如果需要24小時穩定運作，之後可以考慮換成雲端VPS
  （README裡有提到這個選項，但那是更後面的事，現在先不用管）。
- 如果之後想刪除某個排程工作：`schtasks /delete /tn "CryptoAgent_DailyReport" /f`
- 如果想暫停不刪除：`schtasks /change /tn "CryptoAgent_DailyReport" /disable`
