# StockWarning Discord Bot (Python)

用 Python 建立的 Discord 機器人，支援：

- 股票漲跌幅/目標價示警（可同時追蹤台股、美股）
- 國發會景氣燈號頁面更新通知（新發布時推播）

## 1. 環境需求

- Python 3.10+
- 一個 Discord Bot（已取得 Token）

## 2. 安裝

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 3. 設定

1. 複製 `.env.example` 為 `.env`
2. 填入：
   - `DISCORD_TOKEN`
   - `DISCORD_CHANNEL_ID`（要收通知的頻道 ID）
3. （可選）調整輪詢秒數：
   - `STOCK_CHECK_INTERVAL_SEC`
   - `ECONOMY_CHECK_INTERVAL_SEC`
   - `MANUAL_CHECK_TIMEOUT_SEC`（`/check_now` 單項檢查逾時秒數）
4. 建立 `watchlist.json`（可先複製 `watchlist.example.json`）

`watchlist.json` 欄位說明：

- `symbol`: 股票代號（台股請用 `2330.TW` 這種 Yahoo 格式）
- `up_pct`: 漲幅達此百分比示警
- `down_pct`: 跌幅達此百分比示警（填正數即可，程式會視為負向門檻）
- `target_high`: 價格大於等於此值示警
- `target_low`: 價格小於等於此值示警

## 4. Discord 權限與邀請

在 Discord Developer Portal：

- Bot 權限至少給：
  - `View Channels`
  - `Send Messages`
- OAuth2 URL 建議 scope：
  - `bot`
  - `applications.commands`

## 5. 啟動

```bash
python bot.py
```

## 6. 可用 Slash Commands

- `/status`：查看目前監控設定與輪詢秒數
- `/check_now`：手動立即檢查一次股票與景氣燈號

如果你有設定 `DISCORD_GUILD_ID`，指令同步會比較快（通常立即）。

## 7. 通知去重機制

- 股票示警採「條件觸發一次」模式：
  - 條件首次成立才通知
  - 必須先回到未觸發狀態，下一次再次達標才再通知
- 狀態儲存在 `state.json`

## 8. 景氣燈號更新來源

目前預設監控國發會景氣指標頁面：

- `https://www.ndc.gov.tw/News_Content.aspx?n=9D32B61B1E56E558&s=C367F13BF38C5711&sms=9D3CAFD318C60877`

偵測到發布日期更新時，會推播通知並附上官方頁面與附件下載連結（若可解析）。
