import asyncio
import csv
import copy
import io
import json
import logging
import os
import re
import time
import zipfile
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from time import monotonic
from typing import Any, Callable

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


TWSE_QUOTE_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
DATA_GOV_DATASET_URL = "https://data.gov.tw/dataset/6099"
NDC_ECONOMY_PAGE_URL = "https://index.ndc.gov.tw/n/zh_twr"
NDC_ECONOMY_ZIP_URL_PATTERN = re.compile(
    r"https://ws\.ndc\.gov\.tw/Download\.ashx\?[^\"']*icon=\.zip[^\"']*",
    flags=re.IGNORECASE,
)
TW_STOCK_CODE_PATTERN = re.compile(r"^\d{4,6}$")

DEFAULT_WATCHLIST: list[dict[str, Any]] = [
    {
        "symbol": "2330",
        "name": "TSMC",
        "up_pct": 3.0,
        "down_pct": 3.0,
        "target_high": None,
        "target_low": None,
    },
    {
        "symbol": "0050",
        "name": "元大台灣50",
        "up_pct": 2.0,
        "down_pct": 2.0,
        "target_high": None,
        "target_low": None,
    },
]


@dataclass
class Settings:
    discord_token: str
    data_path: Path
    economy_page_url: str
    guild_id: int | None
    poll_tick_sec: int
    manual_check_timeout_sec: int
    default_stock_interval_sec: int
    default_economy_interval_sec: int

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise ValueError("DISCORD_TOKEN 未設定")

        guild_raw = os.getenv("DISCORD_GUILD_ID", "").strip()
        guild_id = int(guild_raw) if guild_raw else None

        data_path = Path(os.getenv("USER_DATA_PATH", "user_data.json")).resolve()
        economy_page_url = os.getenv("ECONOMY_SOURCE_URL", NDC_ECONOMY_PAGE_URL).strip()

        poll_tick = _bounded_int(os.getenv("POLL_TICK_SEC"), fallback=30, minimum=10)
        manual_timeout = _bounded_int(
            os.getenv("MANUAL_CHECK_TIMEOUT_SEC"), fallback=45, minimum=10
        )
        default_stock_interval = _bounded_int(
            os.getenv("STOCK_CHECK_INTERVAL_SEC"), fallback=300, minimum=30
        )
        default_economy_interval = _bounded_int(
            os.getenv("ECONOMY_CHECK_INTERVAL_SEC"), fallback=21600, minimum=300
        )

        return cls(
            discord_token=token,
            data_path=data_path,
            economy_page_url=economy_page_url or NDC_ECONOMY_PAGE_URL,
            guild_id=guild_id,
            poll_tick_sec=poll_tick,
            manual_check_timeout_sec=manual_timeout,
            default_stock_interval_sec=default_stock_interval,
            default_economy_interval_sec=default_economy_interval,
        )


@dataclass
class StockRule:
    symbol: str
    name: str | None
    up_pct: float | None
    down_pct: float | None
    target_high: float | None
    target_low: float | None

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "StockRule":
        symbol = normalize_stock_symbol(row.get("symbol"))
        if not symbol or not is_tw_stock_symbol(symbol):
            raise ValueError("僅支援台股代號 symbol")
        return cls(
            symbol=symbol,
            name=_optional_str(row.get("name")),
            up_pct=_optional_float(row.get("up_pct")),
            down_pct=_optional_float(row.get("down_pct")),
            target_high=_optional_float(row.get("target_high")),
            target_low=_optional_float(row.get("target_low")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "up_pct": self.up_pct,
            "down_pct": self.down_pct,
            "target_high": self.target_high,
            "target_low": self.target_low,
        }


def normalize_stock_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper().replace(" ", "")
    if symbol.endswith(".TW"):
        symbol = symbol[:-3]
    elif symbol.endswith(".TWO"):
        symbol = symbol[:-4]
    return symbol


def is_tw_stock_symbol(symbol: str) -> bool:
    return bool(TW_STOCK_CODE_PATTERN.fullmatch(symbol))


def parse_float_str(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"-", "--", "X", "x", "null", "None"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_int_str(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"-", "--", "X", "x", "null", "None"}:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def economy_color_range(score: int) -> tuple[str, str]:
    if score <= 16:
        return ("藍燈", "9-16")
    if score <= 22:
        return ("黃藍燈", "17-22")
    if score <= 31:
        return ("綠燈", "23-31")
    if score <= 37:
        return ("黃紅燈", "32-37")
    return ("紅燈", "38-45")


def normalize_signal_color_text(signal_text: str | None) -> str | None:
    if not signal_text:
        return None
    text = signal_text.strip()
    mapping = {
        "藍": "藍燈",
        "黃藍": "黃藍燈",
        "綠": "綠燈",
        "黃紅": "黃紅燈",
        "紅": "紅燈",
    }
    if text in mapping:
        return mapping[text]
    return text


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _bounded_int(value: Any, fallback: int, minimum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = fallback
    return max(minimum, result)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    temp_path.replace(path)


def default_user_payload(settings: Settings) -> dict[str, Any]:
    return {
        "watchlist": copy.deepcopy(DEFAULT_WATCHLIST),
        "config": {
            "enabled": True,
            "stock_interval_sec": settings.default_stock_interval_sec,
            "economy_interval_sec": settings.default_economy_interval_sec,
        },
        "state": {
            "stock_alerts": {},
            "economy": {"last_release_id": None},
            "last_stock_check_ts": 0.0,
            "last_economy_check_ts": 0.0,
        },
    }


class UserDataStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.data_path
        self.lock = asyncio.Lock()

        raw = read_json(self.path, {"users": {}})
        if not isinstance(raw, dict):
            raw = {"users": {}}
        users = raw.get("users", {})
        if not isinstance(users, dict):
            users = {}
        self.data: dict[str, Any] = {"users": users}

        for user_id in list(self.data["users"].keys()):
            self._normalize_user_nolock(user_id)
        write_json(self.path, self.data)

    def _normalize_user_nolock(self, user_id: str) -> None:
        users = self.data.setdefault("users", {})
        payload = users.get(user_id)
        if not isinstance(payload, dict):
            payload = default_user_payload(self.settings)
            users[user_id] = payload

        watchlist = payload.get("watchlist")
        if not isinstance(watchlist, list):
            watchlist = copy.deepcopy(DEFAULT_WATCHLIST)
        normalized_watchlist: list[dict[str, Any]] = []
        for row in watchlist:
            if not isinstance(row, dict):
                continue
            symbol = normalize_stock_symbol(row.get("symbol"))
            if not symbol or not is_tw_stock_symbol(symbol):
                continue
            normalized_watchlist.append(
                {
                    "symbol": symbol,
                    "name": _optional_str(row.get("name")),
                    "up_pct": _optional_float(row.get("up_pct")),
                    "down_pct": _optional_float(row.get("down_pct")),
                    "target_high": _optional_float(row.get("target_high")),
                    "target_low": _optional_float(row.get("target_low")),
                }
            )
        if not normalized_watchlist:
            normalized_watchlist = copy.deepcopy(DEFAULT_WATCHLIST)
        payload["watchlist"] = normalized_watchlist

        config = payload.get("config")
        if not isinstance(config, dict):
            config = {}
        payload["config"] = {
            "enabled": bool(config.get("enabled", True)),
            "stock_interval_sec": _bounded_int(
                config.get("stock_interval_sec"),
                fallback=self.settings.default_stock_interval_sec,
                minimum=30,
            ),
            "economy_interval_sec": _bounded_int(
                config.get("economy_interval_sec"),
                fallback=self.settings.default_economy_interval_sec,
                minimum=300,
            ),
        }

        state = payload.get("state")
        if not isinstance(state, dict):
            state = {}
        stock_alerts = state.get("stock_alerts")
        if not isinstance(stock_alerts, dict):
            stock_alerts = {}
        economy = state.get("economy")
        if not isinstance(economy, dict):
            economy = {}
        payload["state"] = {
            "stock_alerts": stock_alerts,
            "economy": {"last_release_id": economy.get("last_release_id")},
            "last_stock_check_ts": float(state.get("last_stock_check_ts", 0.0) or 0.0),
            "last_economy_check_ts": float(
                state.get("last_economy_check_ts", 0.0) or 0.0
            ),
        }

    def _ensure_user_nolock(self, user_id: int) -> str:
        key = str(user_id)
        users = self.data.setdefault("users", {})
        if key not in users:
            users[key] = default_user_payload(self.settings)
        self._normalize_user_nolock(key)
        return key

    async def ensure_user(self, user_id: int) -> dict[str, Any]:
        async with self.lock:
            key = self._ensure_user_nolock(user_id)
            write_json(self.path, self.data)
            return copy.deepcopy(self.data["users"][key])

    async def get_user(self, user_id: int) -> dict[str, Any]:
        async with self.lock:
            key = self._ensure_user_nolock(user_id)
            return copy.deepcopy(self.data["users"][key])

    async def list_user_ids(self) -> list[int]:
        async with self.lock:
            ids: list[int] = []
            for key in self.data.get("users", {}).keys():
                try:
                    ids.append(int(key))
                except ValueError:
                    continue
            return ids

    async def update_user(
        self, user_id: int, mutator: Callable[[dict[str, Any]], None]
    ) -> dict[str, Any]:
        async with self.lock:
            key = self._ensure_user_nolock(user_id)
            payload = self.data["users"][key]
            mutator(payload)
            self._normalize_user_nolock(key)
            write_json(self.path, self.data)
            return copy.deepcopy(self.data["users"][key])


async def fetch_twse_quotes(
    session: aiohttp.ClientSession, symbols: list[str]
) -> dict[str, dict[str, Any]]:
    tw_symbols = [symbol for symbol in symbols if is_tw_stock_symbol(symbol)]
    if not tw_symbols:
        return {}

    ex_channels: list[str] = []
    for symbol in tw_symbols:
        ex_channels.append(f"tse_{symbol}.tw")
        ex_channels.append(f"otc_{symbol}.tw")

    headers = {"User-Agent": "Mozilla/5.0 StockWarningBot/1.0", "Referer": "https://mis.twse.com.tw/"}
    params = {"ex_ch": "|".join(ex_channels), "json": "1", "delay": "0"}
    async with session.get(TWSE_QUOTE_URL, params=params, headers=headers) as resp:
        resp.raise_for_status()
        payload = await resp.json(content_type=None)

    results = payload.get("msgArray", [])
    output: dict[str, dict[str, Any]] = {}
    for row in results:
        if not isinstance(row, dict):
            continue
        symbol = normalize_stock_symbol(row.get("c"))
        if not symbol or not is_tw_stock_symbol(symbol):
            continue
        if symbol not in tw_symbols:
            continue

        latest = parse_float_str(row.get("z"))
        prev_close = parse_float_str(row.get("y"))
        # 無成交時 z 可能為 "-"，改用開盤價補
        if latest is None:
            latest = parse_float_str(row.get("o"))

        if latest is None or prev_close in (None, 0):
            continue

        output[symbol] = {
            "symbol": symbol,
            "name": _optional_str(row.get("n")) or _optional_str(row.get("nf")) or symbol,
            "price": latest,
            "prev_close": prev_close,
            "market": _optional_str(row.get("ex")) or "tse",
        }

    return output


def format_stock_rule_line(index: int, rule: StockRule) -> str:
    name = rule.name or "-"
    conditions: list[str] = []
    if rule.up_pct is not None:
        conditions.append(f"漲幅>={rule.up_pct:.2f}%")
    if rule.down_pct is not None:
        conditions.append(f"跌幅<=-{abs(rule.down_pct):.2f}%")
    if rule.target_high is not None:
        conditions.append(f"高於{rule.target_high:.2f}")
    if rule.target_low is not None:
        conditions.append(f"低於{rule.target_low:.2f}")
    condition_text = "、".join(conditions) if conditions else "未設定條件"
    return f"{index}. {rule.symbol} ({name}) | {condition_text}"


class StockWarningBot(commands.Bot):
    def __init__(self, settings: Settings):
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self.settings = settings
        self.store = UserDataStore(settings)
        self.session: aiohttp.ClientSession | None = None
        self.background_tasks: list[asyncio.Task[Any]] = []
        self._guild_sync_done = False
        self._economy_cache: dict[str, Any] = {"ts": 0.0, "data": None}

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        self.background_tasks.append(asyncio.create_task(self._run_scheduler_loop()))

        try:
            if self.settings.guild_id:
                synced = await self.sync_commands_to_guild(self.settings.guild_id)
                logging.info(
                    "已同步 %s 個指令到 guild=%s", synced, self.settings.guild_id
                )
                self._guild_sync_done = True
            else:
                synced = await self.sync_global_commands()
                logging.info("已同步 %s 個全域指令", synced)
        except Exception:
            logging.exception("setup_hook 指令同步失敗，稍後 on_ready 會再嘗試。")

    async def close(self) -> None:
        for task in self.background_tasks:
            task.cancel()
        if self.background_tasks:
            await asyncio.gather(*self.background_tasks, return_exceptions=True)
        if self.session:
            await self.session.close()
        await super().close()

    async def on_ready(self) -> None:
        logging.info("Bot 已上線：%s", self.user)
        if self._guild_sync_done:
            return
        if not self.settings.guild_id and self.guilds:
            synced_guilds = 0
            for guild in self.guilds:
                try:
                    await self.sync_commands_to_guild(guild.id)
                    synced_guilds += 1
                except Exception:
                    logging.exception("Guild 指令同步失敗: guild=%s", guild.id)
            logging.info("on_ready 完成 guild 指令同步：%s/%s", synced_guilds, len(self.guilds))
            self._guild_sync_done = True

    async def sync_global_commands(self) -> int:
        synced = await self.tree.sync()
        return len(synced)

    async def sync_commands_to_guild(self, guild_id: int) -> int:
        guild = discord.Object(id=guild_id)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        return len(synced)

    async def _run_scheduler_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            started = monotonic()
            try:
                await self.run_due_checks()
            except Exception:
                logging.exception("排程檢查失敗")
            elapsed = monotonic() - started
            await asyncio.sleep(max(1.0, self.settings.poll_tick_sec - elapsed))

    async def run_due_checks(self) -> None:
        user_ids = await self.store.list_user_ids()
        if not user_ids:
            return
        now_ts = time.time()

        for user_id in user_ids:
            user = await self.store.get_user(user_id)
            config = user["config"]
            state = user["state"]
            if not config.get("enabled", True):
                continue

            if now_ts - float(state.get("last_stock_check_ts", 0.0)) >= float(
                config.get("stock_interval_sec", self.settings.default_stock_interval_sec)
            ):
                try:
                    await self.check_stocks_for_user(user_id, user)
                except Exception:
                    logging.exception("使用者 %s 股票檢查失敗", user_id)
                await self.store.update_user(
                    user_id, lambda p: p["state"].update({"last_stock_check_ts": now_ts})
                )

            user = await self.store.get_user(user_id)
            config = user["config"]
            state = user["state"]
            if now_ts - float(state.get("last_economy_check_ts", 0.0)) >= float(
                config.get("economy_interval_sec", self.settings.default_economy_interval_sec)
            ):
                try:
                    await self.check_economy_for_user(user_id, user)
                except Exception:
                    logging.exception("使用者 %s 景氣檢查失敗", user_id)
                await self.store.update_user(
                    user_id, lambda p: p["state"].update({"last_economy_check_ts": now_ts})
                )

    async def send_alert_to_user(self, user_id: int, message: str) -> None:
        target_user = self.get_user(user_id)
        if target_user is None:
            target_user = await self.fetch_user(user_id)
        await target_user.send(message)

    async def check_stocks_for_user(self, user_id: int, user_payload: dict[str, Any]) -> None:
        if not self.session:
            return

        rows = user_payload.get("watchlist", [])
        rules: list[StockRule] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                rules.append(StockRule.from_dict(row))
            except ValueError:
                continue
        if not rules:
            return

        symbols = sorted({rule.symbol for rule in rules})
        quotes = await fetch_twse_quotes(self.session, symbols)
        pending_alerts: list[str] = []

        def mutator(payload: dict[str, Any]) -> None:
            stock_state = payload["state"].setdefault("stock_alerts", {})
            for rule in rules:
                quote = quotes.get(rule.symbol)
                if not quote:
                    continue

                price = parse_float_str(quote.get("price"))
                prev_close = parse_float_str(quote.get("prev_close"))
                if price is None or prev_close in (None, 0):
                    continue

                change_pct = ((price - prev_close) / prev_close) * 100
                display_name = rule.name or str(quote.get("name") or rule.symbol)

                checks: list[tuple[str, bool, str]] = []
                if rule.up_pct is not None:
                    checks.append(
                        ("up_pct", change_pct >= rule.up_pct, f"漲幅 >= {rule.up_pct:.2f}%")
                    )
                if rule.down_pct is not None:
                    checks.append(
                        (
                            "down_pct",
                            change_pct <= -abs(rule.down_pct),
                            f"跌幅 <= -{abs(rule.down_pct):.2f}%",
                        )
                    )
                if rule.target_high is not None:
                    checks.append(
                        (
                            "target_high",
                            price >= rule.target_high,
                            f"價格 >= {rule.target_high:.2f}",
                        )
                    )
                if rule.target_low is not None:
                    checks.append(
                        (
                            "target_low",
                            price <= rule.target_low,
                            f"價格 <= {rule.target_low:.2f}",
                        )
                    )

                for check_name, is_hit, condition_text in checks:
                    state_key = f"{rule.symbol}|{check_name}"
                    was_hit = bool(stock_state.get(state_key, False))
                    if is_hit and not was_hit:
                        stock_state[state_key] = True
                        pending_alerts.append(
                            "\n".join(
                                [
                                    "[股價示警]",
                                    f"股票: {display_name} ({rule.symbol})",
                                    f"現價: {price:.2f}",
                                    f"漲跌幅: {change_pct:+.2f}%",
                                    f"觸發條件: {condition_text}",
                                ]
                            )
                        )
                    elif (not is_hit) and was_hit:
                        stock_state[state_key] = False

        await self.store.update_user(user_id, mutator)
        for message in pending_alerts:
            try:
                await self.send_alert_to_user(user_id, message)
            except Exception:
                logging.exception("傳送使用者 %s 股票通知失敗", user_id)

    async def get_latest_economy_release(self) -> dict[str, Any] | None:
        now_ts = time.time()
        cached = self._economy_cache.get("data")
        cached_ts = float(self._economy_cache.get("ts", 0.0) or 0.0)
        if cached and (now_ts - cached_ts) < 300:
            return cached

        release_data: dict[str, Any] | None = None
        try:
            release_data = await self._fetch_economy_release_from_zip()
        except Exception:
            logging.exception("景氣對策信號 ZIP 來源檢查失敗。")

        if release_data is None:
            return None

        if release_data:
            self._economy_cache = {"ts": now_ts, "data": release_data}
        return release_data

    async def _fetch_economy_release_from_zip(self) -> dict[str, Any] | None:
        if not self.session:
            return None

        headers = {"User-Agent": "Mozilla/5.0 StockWarningBot/1.0"}
        async with self.session.get(DATA_GOV_DATASET_URL, headers=headers) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"data.gov.tw 回應狀態碼 {resp.status}")
            html = await resp.text(errors="ignore")

        url_match = NDC_ECONOMY_ZIP_URL_PATTERN.search(html)
        if not url_match:
            raise RuntimeError("找不到景氣指標 ZIP 下載連結")
        zip_url = unescape(url_match.group(0))

        async with self.session.get(zip_url, headers=headers) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"ZIP 下載回應狀態碼 {resp.status}")
            zip_bytes = await resp.read()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            csv_name = None
            for name in archive.namelist():
                if name.endswith("景氣指標與燈號.csv"):
                    csv_name = name
                    break
            if not csv_name:
                raise RuntimeError("ZIP 中找不到景氣指標與燈號.csv")

            csv_text = archive.read(csv_name).decode("utf-8-sig", errors="ignore")

        reader = csv.DictReader(io.StringIO(csv_text))
        latest_row: dict[str, str] | None = None
        latest_date_value = -1
        for row in reader:
            if not isinstance(row, dict):
                continue
            date_raw = str(row.get("Date", "")).strip()
            if not date_raw.isdigit():
                continue
            date_value = int(date_raw)
            if date_value > latest_date_value:
                latest_date_value = date_value
                latest_row = row

        if not latest_row:
            raise RuntimeError("無法從 CSV 解析最新景氣資料")

        date_raw = str(latest_row.get("Date", "")).strip()
        if len(date_raw) == 6:
            display = f"{date_raw[:4]}-{date_raw[4:6]}"
        else:
            display = date_raw

        score = parse_int_str(latest_row.get("景氣對策信號綜合分數"))
        if score is None:
            raise RuntimeError("無法解析景氣對策信號綜合分數")

        signal_text = normalize_signal_color_text(
            _optional_str(latest_row.get("景氣對策信號"))
        )
        color_name, score_range = economy_color_range(score)

        return {
            "release_id": f"period:{date_raw}:score:{score}",
            "display": display,
            "date_raw": date_raw,
            "score": score,
            "signal_text": signal_text,
            "color_name": color_name,
            "score_range": score_range,
            "source_zip_url": zip_url,
            "source_page_url": DATA_GOV_DATASET_URL,
            "official_page_url": self.settings.economy_page_url,
        }

    async def check_economy_for_user(
        self, user_id: int, user_payload: dict[str, Any]
    ) -> None:
        release_data = await self.get_latest_economy_release()
        if not release_data:
            return

        latest_id = release_data.get("release_id")
        if not latest_id:
            return

        should_notify = False

        def mutator(payload: dict[str, Any]) -> None:
            nonlocal should_notify
            economy_state = payload["state"].setdefault("economy", {})
            previous = economy_state.get("last_release_id")
            if previous is None:
                economy_state["last_release_id"] = latest_id
            elif previous != latest_id:
                economy_state["last_release_id"] = latest_id
                should_notify = True

        await self.store.update_user(user_id, mutator)
        if not should_notify:
            return

        lines = [
            "[景氣對策信號更新通知]",
            f"最新月份: {release_data.get('display')}",
            f"景氣對策信號綜合分數: {release_data.get('score')} 分",
            f"燈號區間: {release_data.get('score_range')}（{release_data.get('color_name')}）",
        ]
        if release_data.get("signal_text"):
            lines.append(f"官方燈號文字: {release_data['signal_text']}")
        if release_data.get("official_page_url"):
            lines.append(f"官方頁面: {release_data.get('official_page_url')}")
        lines.append(f"資料頁: {release_data.get('source_page_url')}")
        if release_data.get("source_zip_url"):
            lines.append(f"原始資料 ZIP: {release_data['source_zip_url']}")

        try:
            await self.send_alert_to_user(user_id, "\n".join(lines))
        except Exception:
            logging.exception("傳送使用者 %s 景氣通知失敗", user_id)


async def ensure_dm_interaction(interaction: discord.Interaction) -> bool:
    if interaction.guild_id is not None:
        await interaction.response.send_message(
            "這個機器人改為私訊模式，請在與機器人的私訊中使用指令。",
            ephemeral=True,
        )
        return False
    return True


def build_bot(settings: Settings) -> StockWarningBot:
    bot = StockWarningBot(settings)

    async def run_manual_check(user_id: int, label: str, callback: Callable[[], Any]) -> str:
        try:
            await asyncio.wait_for(
                callback(), timeout=settings.manual_check_timeout_sec
            )
            await bot.store.update_user(
                user_id,
                lambda payload: payload["state"].update(
                    {
                        "last_stock_check_ts": time.time()
                        if label == "股票檢查"
                        else payload["state"].get("last_stock_check_ts", 0.0),
                        "last_economy_check_ts": time.time()
                        if label == "景氣對策信號檢查"
                        else payload["state"].get("last_economy_check_ts", 0.0),
                    }
                ),
            )
            return f"- {label}: 完成"
        except asyncio.TimeoutError:
            logging.warning("手動檢查逾時: %s", label)
            return f"- {label}: 逾時（>{settings.manual_check_timeout_sec} 秒）"
        except Exception as exc:
            logging.exception("手動檢查失敗: %s", label)
            return f"- {label}: 失敗（{type(exc).__name__}: {exc}）"

    @bot.tree.command(name="status", description="查看你的監控狀態（私訊模式）")
    async def status(interaction: discord.Interaction) -> None:
        if not await ensure_dm_interaction(interaction):
            return
        user = await bot.store.ensure_user(interaction.user.id)
        config = user["config"]
        lines = [
            "你的 StockWarning 狀態:",
            f"- 追蹤股票數: {len(user['watchlist'])}",
            f"- 股票輪詢秒數: {config['stock_interval_sec']}",
            f"- 景氣輪詢秒數: {config['economy_interval_sec']}",
            f"- 啟用通知: {'是' if config.get('enabled', True) else '否'}",
        ]
        await interaction.response.send_message("\n".join(lines))

    @bot.tree.command(name="settings_show", description="查看你目前的個人設定")
    async def settings_show(interaction: discord.Interaction) -> None:
        if not await ensure_dm_interaction(interaction):
            return
        user = await bot.store.ensure_user(interaction.user.id)
        config = user["config"]
        await interaction.response.send_message(
            "\n".join(
                [
                    "你的個人設定:",
                    f"- 股票輪詢秒數: {config['stock_interval_sec']}",
                    f"- 景氣輪詢秒數: {config['economy_interval_sec']}",
                    f"- 啟用通知: {'是' if config.get('enabled', True) else '否'}",
                ]
            )
        )

    @bot.tree.command(name="settings_set_interval", description="設定你的輪詢秒數")
    async def settings_set_interval(
        interaction: discord.Interaction,
        stock_seconds: int | None = None,
        economy_seconds: int | None = None,
    ) -> None:
        if not await ensure_dm_interaction(interaction):
            return
        if stock_seconds is None and economy_seconds is None:
            await interaction.response.send_message(
                "請至少填一個參數：`stock_seconds` 或 `economy_seconds`。"
            )
            return
        if stock_seconds is not None and stock_seconds < 30:
            await interaction.response.send_message("`stock_seconds` 最小值是 30 秒。")
            return
        if economy_seconds is not None and economy_seconds < 300:
            await interaction.response.send_message("`economy_seconds` 最小值是 300 秒。")
            return

        def mutator(payload: dict[str, Any]) -> None:
            if stock_seconds is not None:
                payload["config"]["stock_interval_sec"] = stock_seconds
            if economy_seconds is not None:
                payload["config"]["economy_interval_sec"] = economy_seconds

        updated = await bot.store.update_user(interaction.user.id, mutator)
        config = updated["config"]
        await interaction.response.send_message(
            "\n".join(
                [
                    "已更新你的輪詢設定。",
                    f"- 股票輪詢秒數: {config['stock_interval_sec']}",
                    f"- 景氣輪詢秒數: {config['economy_interval_sec']}",
                ]
            )
        )

    @bot.tree.command(
        name="settings_set_channel",
        description="舊版相容：DM 模式固定通知到你的私訊",
    )
    async def settings_set_channel_compat(interaction: discord.Interaction) -> None:
        if not await ensure_dm_interaction(interaction):
            return
        await interaction.response.send_message(
            "目前是 DM 模式，不需要設定頻道。通知會直接發到你的私訊。"
        )

    @bot.tree.command(name="settings_enable", description="開啟或關閉你的通知排程")
    async def settings_enable(interaction: discord.Interaction, enabled: bool) -> None:
        if not await ensure_dm_interaction(interaction):
            return
        await bot.store.update_user(
            interaction.user.id, lambda payload: payload["config"].update({"enabled": enabled})
        )
        await interaction.response.send_message(
            f"已將你的通知排程設定為：{'啟用' if enabled else '停用'}。"
        )

    @bot.tree.command(name="watchlist_show", description="查看你的追蹤股票清單")
    async def watchlist_show(interaction: discord.Interaction) -> None:
        if not await ensure_dm_interaction(interaction):
            return
        user = await bot.store.ensure_user(interaction.user.id)
        rows = user.get("watchlist", [])
        rules: list[StockRule] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                rules.append(StockRule.from_dict(row))
            except ValueError:
                continue

        if not rules:
            await interaction.response.send_message("你目前沒有追蹤股票。")
            return

        lines = ["你的追蹤股票清單:"]
        for index, rule in enumerate(rules, start=1):
            lines.append(format_stock_rule_line(index, rule))
            if len("\n".join(lines)) > 1700:
                lines.append("...清單過長，請縮小追蹤數量。")
                break
        await interaction.response.send_message("\n".join(lines))

    @bot.tree.command(name="watchlist_add", description="新增你的追蹤股票")
    async def watchlist_add(
        interaction: discord.Interaction,
        symbol: str,
        name: str | None = None,
        up_pct: float | None = None,
        down_pct: float | None = None,
        target_high: float | None = None,
        target_low: float | None = None,
    ) -> None:
        if not await ensure_dm_interaction(interaction):
            return
        symbol = normalize_stock_symbol(symbol)
        if not symbol:
            await interaction.response.send_message("`symbol` 不能是空值。")
            return
        if not is_tw_stock_symbol(symbol):
            await interaction.response.send_message(
                "目前僅支援台股代號（4~6碼），例如 `2330`、`0050`。"
            )
            return
        if up_pct is not None and up_pct < 0:
            await interaction.response.send_message("`up_pct` 請填正數或 0。")
            return
        if down_pct is not None and down_pct < 0:
            await interaction.response.send_message("`down_pct` 請填正數或 0。")
            return
        if (
            target_high is not None
            and target_low is not None
            and target_high < target_low
        ):
            await interaction.response.send_message("`target_high` 不能小於 `target_low`。")
            return

        duplicate = False

        def mutator(payload: dict[str, Any]) -> None:
            nonlocal duplicate
            rows = payload["watchlist"]
            if any(normalize_stock_symbol(row.get("symbol")) == symbol for row in rows):
                duplicate = True
                return
            rows.append(
                StockRule(
                    symbol=symbol,
                    name=_optional_str(name),
                    up_pct=up_pct,
                    down_pct=down_pct,
                    target_high=target_high,
                    target_low=target_low,
                ).to_dict()
            )

        updated = await bot.store.update_user(interaction.user.id, mutator)
        if duplicate:
            await interaction.response.send_message(f"{symbol} 已在你的追蹤清單中。")
            return

        rules = [StockRule.from_dict(row) for row in updated["watchlist"]]
        await interaction.response.send_message(
            f"已新增追蹤：{format_stock_rule_line(len(rules), rules[-1])}"
        )

    @bot.tree.command(name="watchlist_update", description="更新你的追蹤股票條件")
    async def watchlist_update(
        interaction: discord.Interaction,
        symbol: str,
        name: str | None = None,
        up_pct: float | None = None,
        down_pct: float | None = None,
        target_high: float | None = None,
        target_low: float | None = None,
        clear_up_pct: bool = False,
        clear_down_pct: bool = False,
        clear_target_high: bool = False,
        clear_target_low: bool = False,
    ) -> None:
        if not await ensure_dm_interaction(interaction):
            return
        symbol = normalize_stock_symbol(symbol)
        if not symbol:
            await interaction.response.send_message("`symbol` 不能是空值。")
            return
        if not is_tw_stock_symbol(symbol):
            await interaction.response.send_message(
                "目前僅支援台股代號（4~6碼），例如 `2330`、`0050`。"
            )
            return
        if up_pct is not None and up_pct < 0:
            await interaction.response.send_message("`up_pct` 請填正數或 0。")
            return
        if down_pct is not None and down_pct < 0:
            await interaction.response.send_message("`down_pct` 請填正數或 0。")
            return

        payload = await bot.store.get_user(interaction.user.id)
        rows = copy.deepcopy(payload["watchlist"])
        target_row = None
        for row in rows:
            if normalize_stock_symbol(row.get("symbol")) == symbol:
                target_row = row
                break

        if target_row is None:
            await interaction.response.send_message(f"找不到 {symbol}，請先新增。")
            return

        if name is not None:
            lowered = name.strip().lower()
            target_row["name"] = None if lowered in {"none", "null", "-"} else name.strip()
        if clear_up_pct:
            target_row["up_pct"] = None
        if clear_down_pct:
            target_row["down_pct"] = None
        if clear_target_high:
            target_row["target_high"] = None
        if clear_target_low:
            target_row["target_low"] = None
        if up_pct is not None:
            target_row["up_pct"] = up_pct
        if down_pct is not None:
            target_row["down_pct"] = down_pct
        if target_high is not None:
            target_row["target_high"] = target_high
        if target_low is not None:
            target_row["target_low"] = target_low

        high = _optional_float(target_row.get("target_high"))
        low = _optional_float(target_row.get("target_low"))
        if high is not None and low is not None and high < low:
            await interaction.response.send_message("`target_high` 不能小於 `target_low`。")
            return

        updated = await bot.store.update_user(
            interaction.user.id, lambda p: p.update({"watchlist": rows})
        )
        rules = [StockRule.from_dict(row) for row in updated["watchlist"]]
        target_rule = next((rule for rule in rules if rule.symbol == symbol), None)
        if target_rule is None:
            await interaction.response.send_message("更新失敗，請重試。")
            return
        await interaction.response.send_message(
            f"已更新：{format_stock_rule_line(1, target_rule)[3:]}"
        )

    @bot.tree.command(name="watchlist_remove", description="移除你的追蹤股票")
    async def watchlist_remove(interaction: discord.Interaction, symbol: str) -> None:
        if not await ensure_dm_interaction(interaction):
            return
        symbol = normalize_stock_symbol(symbol)
        if not symbol:
            await interaction.response.send_message("`symbol` 不能是空值。")
            return
        if not is_tw_stock_symbol(symbol):
            await interaction.response.send_message(
                "目前僅支援台股代號（4~6碼），例如 `2330`、`0050`。"
            )
            return

        removed = False

        def mutator(payload: dict[str, Any]) -> None:
            nonlocal removed
            rows = payload["watchlist"]
            before = len(rows)
            payload["watchlist"] = [
                row
                for row in rows
                if normalize_stock_symbol(row.get("symbol")) != symbol
            ]
            removed = len(payload["watchlist"]) != before
            if removed:
                stock_alerts = payload["state"].setdefault("stock_alerts", {})
                for key in list(stock_alerts.keys()):
                    if key.startswith(f"{symbol}|"):
                        stock_alerts.pop(key, None)

        await bot.store.update_user(interaction.user.id, mutator)
        if not removed:
            await interaction.response.send_message(f"{symbol} 不在你的追蹤清單中。")
            return
        await interaction.response.send_message(f"已移除追蹤股票：{symbol}")

    @bot.tree.command(name="check_now", description="立即檢查一次你的股票與景氣對策信號")
    async def check_now(interaction: discord.Interaction) -> None:
        if not await ensure_dm_interaction(interaction):
            return
        await bot.store.ensure_user(interaction.user.id)
        await interaction.response.defer(thinking=True)

        user_payload = await bot.store.get_user(interaction.user.id)
        results = await asyncio.gather(
            run_manual_check(
                interaction.user.id,
                "股票檢查",
                lambda: bot.check_stocks_for_user(interaction.user.id, user_payload),
            ),
            run_manual_check(
                interaction.user.id,
                "景氣對策信號檢查",
                lambda: bot.check_economy_for_user(interaction.user.id, user_payload),
            ),
        )
        await interaction.followup.send("\n".join(["已完成一次手動檢查。", *results]))

    @bot.tree.command(name="sync_commands", description="手動同步 slash 指令（私訊可用）")
    async def sync_commands(interaction: discord.Interaction) -> None:
        if interaction.guild_id is not None:
            await interaction.response.send_message(
                "請在與機器人的私訊中使用這個指令。", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)
        try:
            synced = await bot.sync_global_commands()
            await interaction.followup.send(f"已嘗試同步全域指令，數量：{synced}")
        except Exception as exc:
            logging.exception("手動同步全域指令失敗")
            await interaction.followup.send(
                f"同步失敗：{type(exc).__name__}: {exc}"
            )

    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        logging.exception("Slash 指令失敗", exc_info=error)
        if isinstance(error, app_commands.MissingPermissions):
            message = (
                "你目前看到的是舊版指令權限檢查。請稍等幾分鐘後重開 Discord，"
                "再到私訊使用 `/sync_commands` 或 `/status`。"
            )
        else:
            message = f"指令執行失敗：{type(error).__name__}"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=interaction.guild_id is not None)
            else:
                await interaction.response.send_message(
                    message, ephemeral=interaction.guild_id is not None
                )
        except Exception:
            logging.exception("無法回覆 slash 指令錯誤訊息")

    return bot


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    settings = Settings.from_env()
    bot = build_bot(settings)
    bot.run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
