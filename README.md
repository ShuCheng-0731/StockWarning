## 使用方式

1. 在 Discord 開啟與機器人的私訊視窗
2. 在私訊裡輸入 `/` 使用下列指令

可用指令：

- `/watchlist_show`：查看你的追蹤清單
- `/watchlist_add`：新增追蹤股票
- `/watchlist_update`：更新追蹤條件
- `/watchlist_remove`：移除追蹤股票
- `/check_now`：立即手動檢查，顯示追蹤清單、股價、景氣燈號（最新與前一期分數、顏色區間）

建議低占用設定（Railway）：

- `POLL_TICK_SEC=60`
- `STOCK_CHECK_INTERVAL_SEC=600`

景氣通知排程（固定）：

- 每月 27 日 20:00（Asia/Taipei）
- 若 27 日為週末，順延至下週一同一時間
