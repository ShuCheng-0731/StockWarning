# StockWarning Discord Bot (Python)

用 Python 建立的 Discord 機器人，支援：

- 股票漲跌幅/目標價示警（台股、美股皆可）
- 景氣對策信號更新通知（預設來源：`https://index.ndc.gov.tw/n/zh_twr`）
- 在 Discord 內直接管理追蹤股票、輪詢秒數、通知頻道

## 1. 環境需求

- Python 3.10+
- Discord Bot Token

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
2. 必填：
   - `DISCORD_TOKEN`
3. 可選：
   - `DISCORD_CHANNEL_ID`（初始通知頻道，可改用 Discord 指令設定）
   - `DISCORD_GUILD_ID`（單一伺服器快速同步 slash commands）
   - `STOCK_CHECK_INTERVAL_SEC`
   - `ECONOMY_CHECK_INTERVAL_SEC`
   - `MANUAL_CHECK_TIMEOUT_SEC`
   - `WATCHLIST_PATH`
   - `STATE_PATH`
   - `RUNTIME_CONFIG_PATH`
   - `ECONOMY_SOURCE_URL`
   - `ECONOMY_API_URL`

程式執行後會建立：

- `watchlist.json`：追蹤股票清單
- `state.json`：示警去重狀態
- `config.json`：通知頻道與輪詢秒數（可透過 Discord 指令修改）

## 4. Discord 權限

在 Discord Developer Portal 建議至少給：

- `View Channels`
- `Send Messages`

OAuth2 scopes：

- `bot`
- `applications.commands`

## 5. 啟動

```bash
python bot.py
```

## 6. Slash Commands

一般成員可用：

- `/status`：查看整體狀態（追蹤數、輪詢秒數、通知頻道）
- `/settings_show`：查看通知頻道與輪詢設定
- `/watchlist_show`：查看追蹤清單
- `/check_now`：立即手動檢查一次

需要「管理伺服器」權限：

- `/settings_set_channel`：設定通知頻道
- `/settings_set_interval`：設定股票與景氣對策信號輪詢秒數
- `/watchlist_add`：新增追蹤股票
- `/watchlist_update`：更新追蹤條件
- `/watchlist_remove`：移除追蹤股票

## 7. watchlist 規格

每一檔股票欄位：

- `symbol`: 股票代號（例：`2330.TW`、`AAPL`）
- `name`: 顯示名稱（可空）
- `up_pct`: 漲幅門檻（%）
- `down_pct`: 跌幅門檻（%）
- `target_high`: 目標高價
- `target_low`: 目標低價

示例：`watchlist.example.json`

## 8. 通知去重邏輯

- 同一條件首次達標才通知一次
- 回落到未達標後，再次達標才會再通知
- 相關狀態保存在 `state.json`

## 9. 景氣對策信號來源

預設先查 API：

- `https://index.ndc.gov.tw/n/json/lightscore`

若 API 失敗，再備援抓頁面：

- `https://index.ndc.gov.tw/n/zh_twr`

偵測到最新月份/日期變更時就推播通知。
