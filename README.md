# StockWarning Discord Bot (Python, DM Mode)

這版改為「私訊模式」：

- 使用者在與 Bot 的私訊裡下指令
- 每個 Discord 帳號有獨立設定與追蹤清單
- 通知直接發到該使用者的私訊

## 1. 功能

- 股票漲跌幅/目標價示警（台股、美股可混用）
- 景氣對策信號更新通知（預設來源：`https://index.ndc.gov.tw/n/zh_twr`）
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
   - `ECONOMY_API_URL`

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
- `/settings_enable`：啟用/停用你的排程
- `/watchlist_show`：查看你的追蹤清單
- `/watchlist_add`：新增追蹤股票
- `/watchlist_update`：更新追蹤條件
- `/watchlist_remove`：移除追蹤股票
- `/check_now`：立即手動檢查一次

注意：若在伺服器頻道使用，Bot 會提示你改到私訊使用。

## 7. 每帳號資料儲存

預設寫入 `user_data.json`，結構是：

- `users.<discord_user_id>.watchlist`
- `users.<discord_user_id>.config`
- `users.<discord_user_id>.state`

因此每個帳號的設定互不影響。

## 8. 景氣對策信號來源

預設先查 API：

- `https://index.ndc.gov.tw/n/json/lightscore`

若 API 失敗，再備援抓頁面：

- `https://index.ndc.gov.tw/n/zh_twr`
