# StockWarning Discord Bot (Python, DM Mode)

這版改為「私訊模式」：

- 使用者在與 Bot 的私訊裡下指令
- 每個 Discord 帳號有獨立設定與追蹤清單
- 通知直接發到該使用者的私訊

## 1. 功能

- 台股漲跌幅/目標價示警（僅台股，代號可直接輸入 `2330`、`0050`）
- 景氣對策信號更新通知（含綜合分數 + 對應燈號顏色區間）
- 每位使用者獨立保存：
  - 追蹤股票清單
  - 輪詢秒數
  - 去重狀態（避免重複狂發）
  - 啟用/停用排程

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
   - `DISCORD_GUILD_ID`（指令同步加速用）
   - `POLL_TICK_SEC`
   - `STOCK_CHECK_INTERVAL_SEC`（新使用者預設）
   - `ECONOMY_CHECK_INTERVAL_SEC`（新使用者預設）
   - `MANUAL_CHECK_TIMEOUT_SEC`
   - `USER_DATA_PATH`（預設 `user_data.json`）
   - `ECONOMY_SOURCE_URL`

## 4. 啟動

```bash
python bot.py
```

## 5. Discord 權限

OAuth2 scopes：

- `bot`
- `applications.commands`

Bot 權限至少包含：

- `Send Messages`

## 6. 使用方式（私訊）

1. 在 Discord 開啟與機器人的私訊視窗
2. 在私訊裡輸入 `/` 使用下列指令

可用指令：

- `/status`：查看你的監控狀態
- `/settings_show`：查看你的個人設定
- `/settings_set_interval`：設定你的輪詢秒數
- `/settings_set_channel`：舊版相容提示（DM 模式不需設定頻道）
- `/settings_enable`：啟用/停用你的排程
- `/watchlist_show`：查看你的追蹤清單
- `/watchlist_add`：新增追蹤股票
- `/watchlist_update`：更新追蹤條件
- `/watchlist_remove`：移除追蹤股票
- `/check_now`：立即手動檢查一次
- `/sync_commands`：手動同步全域指令

注意：若在伺服器頻道使用，Bot 會提示你改到私訊使用。

如果在 DM 看到「需要管理伺服器權限」：

1. 代表你目前點到的是舊版快取指令
2. 先重開 Discord 客戶端
3. 在私訊輸入 `/sync_commands`
4. 再測試 `/status`

## 7. 每帳號資料儲存

預設寫入 `user_data.json`，結構是：

- `users.<discord_user_id>.watchlist`
- `users.<discord_user_id>.config`
- `users.<discord_user_id>.state`

因此每個帳號的設定互不影響。

## 8. 景氣對策信號來源

景氣資料來源：

- 資料集頁面：`https://data.gov.tw/dataset/6099`
- 官方頁面：`https://index.ndc.gov.tw/n/zh_twr`

通知內容包含：

- 最新月份（例如 `2026-02`）
- 景氣對策信號綜合分數（例如 `40`）
- 燈號對應區間（例如 `38-45（紅燈）`）

## 9. 股價來源（台股）

使用 TWSE 即時報價 API：

- `https://mis.twse.com.tw/stock/api/getStockInfo.jsp`

支援上市/上櫃自動判斷，代號輸入可省略 `.TW`，例如輸入 `2330` 即可。
