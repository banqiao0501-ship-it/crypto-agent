# Crypto Intelligence Agent（V0+V1）

抓取YouTube幣圈KOL逐字稿 + jin10快訊 + CoinGecko現貨數據 + Binance/Bybit衍生品數據，
用Claude彙整成消息面+技術面的每日報告，並在偵測到價格異常時即時推播，透過LINE通知。

## 這個版本包含什麼（V0+V1範圍）

- YouTube逐字稿收集（不需要YouTube API key）
- jin10快訊收集，並過濾出加密貨幣相關項目
- CoinGecko現貨價格 + Binance/Bybit Funding Rate/Open Interest
- 技術指標：RSI、EMA20/50/200、簡易趨勢判斷、簡易支撐壓力（1h / 4h / 1d三個timeframe）
- 衍生品數據（Funding Rate / Open Interest）：BingX優先（使用者實際交易所），抓不到才降級用Binance、再降級用Bybit
- 規則引擎：價格劇烈波動（1h±3% / 4h±5%）、RSI極端值（>75 或 <25），觸發後才呼叫AI解讀
- Claude彙整每日報告，輸出Market Bias（短/中/長線）而非買賣建議
- LINE推播：每日固定報告 + 即時異常警報
- 系統健康狀態會附在每日報告最後，方便你知道有沒有哪個collector掛了

**沒有做的（照之前討論，先留到之後）**：自動發掘新幣種、Event Clustering跨來源去重、
Source Reliability加權判斷、coinglass/followin.io（瀏覽器自動化）、SMC/交易結構分析。

## V2進度

- ✅ Market Regime Detection：用BTC+ETH的趨勢/動能/波動度/成交量，判斷整體市場是
  Bullish Trend / Bearish Trend / Range / High Volatility / Risk-off，結果會顯示在
  每日報告最上面，AI彙整時也會參考這個大盤狀態來解讀個別幣種
- ✅ Event Clustering：每日報告產生前，會先呼叫一次Claude把這批新資料裡談論同一件事的
  內容合併成「事件」（存進`events`/`event_sources`兩張表），避免同一件事被誤判成多則
  獨立利多/利空。這會讓每日報告多花一次AI呼叫（多幾秒鐘、多一點點費用），交換來的是
  更準確的事件計數。目前只套用在每日報告，即時警報分析(alert)仍用原始內容，沒有套用去重。
- ✅ Source Reliability加權：每個事件依底下來源的可信度（`sources`表的`reliability`欄位，
  官方/財經媒體較高、KOL較低）加總（封頂1.0）算出一個可信度分數，分數越高代表可信度越高
  或有越多獨立來源互相證實。分數會附在事件資料裡一起給AI，並在prompt裡明確要求AI對
  低可信度（僅單一低可信度來源）的事件降低權重、標註「僅單一來源」，不要讓它主導market_bias判斷。
- ⬜ coinglass/followin.io：尚未開始（backlog裡最後一塊，需要瀏覽器自動化）

## 安裝

```bash
cd crypto-agent
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

打開 `.env`，依照裡面的註解填入四個金鑰：`ANTHROPIC_API_KEY`、`COINGECKO_API_KEY`、
`LINE_CHANNEL_ACCESS_TOKEN`、`LINE_USER_ID`。

## 初次測試（強烈建議依序手動跑過一次，不要直接上cron）

```bash
python -m app.main init-db          # 建立SQLite資料庫
python -m app.main collect-youtube  # 測試YouTube抓取，第一次跑會比較久（要解析4個頻道的channel_id）
python -m app.main collect-jin10    # 測試jin10抓取，見下方「已知風險」
python -m app.main market-check     # 測試市場數據+技術指標+規則引擎（正常情況下不會觸發警報）
python -m app.main daily-report     # 測試完整每日報告產生+推播，第一次跑建議在有網路+API額度充足時測試
```

每一步結束後看一下 `logs/crypto-agent.log`，確認沒有ERROR訊息。

## 排程

**Windows使用者**：參考`WINDOWS_SCHEDULING.md`，用工作排程器（Task Scheduler）設定，
裡面有現成的`schtasks`指令可以直接複製貼上。

**Mac/Linux使用者**：參考`crontab.example`，把裡面的路徑改成你自己的，`crontab -e`貼上即可。

## 已知風險 / 你上線前需要驗證的地方

這幾個地方是我在寫的當下沒辦法直接連網測試驗證的，列出來讓你心裡有數：

1. **jin10.py 的CSS selector是用猜的**——我看得到jin10首頁渲染出來的文字內容，
   但看不到實際HTML的tag/class名稱。第一次跑 `collect-jin10` 如果log印出「選不到任何項目」
   的警告，代表 `app/collectors/jin10.py` 裡的 `_ITEM_SELECTOR` 猜錯了，需要你用瀏覽器
   開發者工具（F12）看一下jin10首頁快訊區塊的實際結構，回來調整那兩行selector。

2. **youtube-transcript-api是非官方套件**，YouTube偶爾改版會讓它壞掉，也有部分使用者
   回報在雲端主機的IP容易被限流（自己電腦上低頻率使用風險較低）。如果 `collect-youtube`
   持續失敗，先確認套件版本是不是最新的（`pip install -U youtube-transcript-api`），
   再去它的GitHub issue頁面查最新狀況。

3. **CoinGecko免費/Demo方案的interval規則**：我是照官方文件的一般規則寫的（days<=90自動
   給hourly顆粒度），但這類免費方案的細節條款不時會調整，如果 `market-check` 抓到的價格
   序列數量跟預期差很多，去CoinGecko API文件對一下目前規則。

4. **LINE免費額度**：文件上寫「輕用量」方案每月200~500則免費，這個數字不同資訊來源寫的
   不完全一樣，正式跑之前建議去LINE Developers Console的帳單頁面確認一次目前的方案內容。

5. **BingX的衍生品數據端點**：`/openApi/swap/v2/quote/premiumIndex`跟`/openApi/swap/v2/quote/openInterest`
   這兩個端點的回應欄位名稱（`lastFundingRate`、`openInterest`）是照BingX官方文件寫的，但因為
   我這邊沒辦法連網路實際呼叫測試，如果`market-check`跑起來log顯示BingX抓取失敗，
   去 https://bingx-api.github.io/docs/#/swapV2/introduce 對一下目前的實際欄位名稱，
   反正失敗時程式會自動降級用Binance/Bybit，不會讓整個流程掛掉。

其他部分（資料庫schema、技術指標計算、規則引擎邏輯、AI prompt、專案結構）都是可以直接
運作的完整邏輯，不是佔位符。

## 之後可以加的東西（backlog，先不用管）

Event Clustering、Source Reliability加權、Market Regime Detection、coinglass/followin.io
（需要Playwright瀏覽器自動化）、SMC/交易結構分析——這些等V0+V1穩定運作一陣子之後，
有需要再回來加。
