import asyncio
import csv
import copy
import io
import json
import logging
import os
import re
import ssl
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from time import monotonic
from typing import Any, Callable
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


TWSE_QUOTE_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
DATA_GOV_DATASET_URL = "https://data.gov.tw/dataset/6099"
NDC_ECONOMY_PAGE_URL = "https://index.ndc.gov.tw/n/zh_tw"
NDC_ECONOMY_JSON_URL = "https://index.ndc.gov.tw/n/json/lightscore"
NDC_DEFAULT_ZIP_URL = (
    "https://ws.ndc.gov.tw/Download.ashx?"
    "u=LzAwMS9hZG1pbmlzdHJhdG9yLzEwL3JlbGZpbGUvNTc4MS82MzkyL2VhMjM1YmQ5LWQwNTItNGE2OS1hYmZjLWQ1Yzc4NWQzZDBlMi56aXA%3D"
    "&n=5pmv5rCj5oyH5qiZ5Y%2BK54eI6JmfLnppcA%3D%3D"
)
NDC_ECONOMY_ZIP_URL_PATTERN = re.compile(
    r"https://ws\.ndc\.gov\.tw/Download\.ashx\?[^\"']+",
    flags=re.IGNORECASE,
)
TW_STOCK_CODE_PATTERN = re.compile(r"^\d{4,6}$")
TAIPEI_TZ = ZoneInfo("Asia/Taipei") if ZoneInfo else timezone(timedelta(hours=8))

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
    economy_zip_url: str | None
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
        economy_zip_url = os.getenv("ECONOMY_ZIP_URL", "").strip() or None

        poll_tick = _bounded_int(os.getenv("POLL_TICK_SEC"), fallback=60, minimum=10)
        manual_timeout = _bounded_int(
            os.getenv("MANUAL_CHECK_TIMEOUT_SEC"), fallback=45, minimum=10
        )
        default_stock_interval = _bounded_int(
            os.getenv("STOCK_CHECK_INTERVAL_SEC"), fallback=600, minimum=30
        )
        default_economy_interval = _bounded_int(
            os.getenv("ECONOMY_CHECK_INTERVAL_SEC"), fallback=43200, minimum=300
        )

        return cls(
            discord_token=token,
            data_path=data_path,
            economy_page_url=economy_page_url or NDC_ECONOMY_PAGE_URL,
            economy_zip_url=economy_zip_url,
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


def normalize_json_key(value: Any) -> str:
    return re.sub(r"[\s_\-:/]+", "", str(value or "").strip().lower())


def parse_year_month(value: Any) -> tuple[int, str, str] | None:
    text = str(value or "").strip()
    if not text:
        return None

    digits = re.sub(r"\D", "", text)

    if len(digits) in {5, 7} and digits[:3].isdigit() and int(digits[:3]) < 1911:
        year = int(digits[:3]) + 1911
        month = int(digits[3:5])
        if 1900 <= year <= 2200 and 1 <= month <= 12:
            raw_id = f"{year:04d}{month:02d}"
            return (year * 100 + month, f"{year:04d}-{month:02d}", raw_id)

    if len(digits) >= 6:
        year = int(digits[:4])
        month = int(digits[4:6])
        if 1900 <= year <= 2200 and 1 <= month <= 12:
            raw_id = f"{year:04d}{month:02d}"
            return (year * 100 + month, f"{year:04d}-{month:02d}", raw_id)

    if len(digits) == 5:
        year = int(digits[:3]) + 1911
        month = int(digits[3:5])
        if 1900 <= year <= 2200 and 1 <= month <= 12:
            raw_id = f"{year:04d}{month:02d}"
            return (year * 100 + month, f"{year:04d}-{month:02d}", raw_id)

    match = re.search(r"(\d{2,4})\D+(\d{1,2})", text)
    if not match:
        return None
    year_raw = int(match.group(1))
    month = int(match.group(2))
    year = year_raw if year_raw >= 1911 else year_raw + 1911
    if 1900 <= year <= 2200 and 1 <= month <= 12:
        raw_id = f"{year:04d}{month:02d}"
        return (year * 100 + month, f"{year:04d}-{month:02d}", raw_id)
    return None


def economy_schedule_datetime(year: int, month: int) -> datetime:
    dt = datetime(year, month, 27, 20, 0, 0, tzinfo=TAIPEI_TZ)
    while dt.weekday() >= 5:
        dt += timedelta(days=1)
    return dt


def latest_due_economy_schedule(now: datetime) -> tuple[str, datetime]:
    current = economy_schedule_datetime(now.year, now.month)
    if now >= current:
        return (f"{now.year:04d}-{now.month:02d}", current)

    if now.month == 1:
        prev_year, prev_month = now.year - 1, 12
    else:
        prev_year, prev_month = now.year, now.month - 1
    prev = economy_schedule_datetime(prev_year, prev_month)
    return (f"{prev_year:04d}-{prev_month:02d}", prev)


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
        "state": {
            "stock_alerts": {},
            "economy": {"last_release_id": None},
            "last_stock_check_ts": 0.0,
            "last_economy_check_ts": 0.0,
            "last_economy_schedule_key": "",
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
            "last_economy_schedule_key": str(
                state.get("last_economy_schedule_key", "") or ""
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

    headers = {
        "User-Agent": "Mozilla/5.0 StockWarningBot/1.0",
        "Referer": "https://mis.twse.com.tw/",
    }
    params = {"ex_ch": "|".join(ex_channels), "json": "1", "delay": "0"}
    try:
        async with session.get(TWSE_QUOTE_URL, params=params, headers=headers) as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)
    except (
        aiohttp.ClientConnectorCertificateError,
        aiohttp.ClientConnectorSSLError,
        ssl.SSLError,
    ):
        logging.warning("TWSE SSL 驗證失敗，改用 ssl=False 重試。")
        async with session.get(
            TWSE_QUOTE_URL, params=params, headers=headers, ssl=False
        ) as resp:
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
        now_local = datetime.now(TAIPEI_TZ)
        due_schedule_key, due_schedule_time = latest_due_economy_schedule(now_local)

        for user_id in user_ids:
            user = await self.store.get_user(user_id)
            state = user["state"]

            due_stock = now_ts - float(state.get("last_stock_check_ts", 0.0)) >= float(
                self.settings.default_stock_interval_sec
            )
            due_economy = (
                now_local >= due_schedule_time
                and str(state.get("last_economy_schedule_key", "") or "")
                != due_schedule_key
            )
            stock_ok = False
            economy_ok = False

            if due_stock:
                try:
                    await self.check_stocks_for_user(user_id, user)
                    stock_ok = True
                except Exception:
                    logging.exception("使用者 %s 股票檢查失敗", user_id)

            if due_economy:
                try:
                    economy_ok = await self.check_economy_for_user(
                        user_id, user, force_notify=True
                    )
                except Exception:
                    logging.exception("使用者 %s 景氣檢查失敗", user_id)

            if due_stock or due_economy:
                await self.store.update_user(
                    user_id,
                    lambda p: p["state"].update(
                        {
                            "last_stock_check_ts": now_ts
                            if stock_ok
                            else p["state"].get("last_stock_check_ts", 0.0),
                            "last_economy_check_ts": now_ts
                            if economy_ok
                            else p["state"].get("last_economy_check_ts", 0.0),
                            "last_economy_schedule_key": due_schedule_key
                            if economy_ok and due_economy
                            else str(
                                p["state"].get("last_economy_schedule_key", "") or ""
                            ),
                        }
                    ),
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
            release_data = await self._fetch_economy_release_from_ndc_json()
        except Exception:
            logging.exception("景氣對策信號官方頁 JSON 來源檢查失敗。")

        if release_data is None:
            try:
                release_data = await self._fetch_economy_release_from_zip()
            except Exception:
                logging.exception("景氣對策信號 ZIP 備援來源檢查失敗。")

        if release_data is None:
            return None

        if release_data:
            self._economy_cache = {"ts": now_ts, "data": release_data}
        return release_data

    def _candidate_economy_json_urls(self) -> list[str]:
        urls: list[str] = []
        source_url = (self.settings.economy_page_url or "").strip()
        if source_url:
            if "/n/json/" in source_url.lower():
                urls.append(source_url)
            match = re.match(r"^(https?://[^/]+)", source_url, flags=re.IGNORECASE)
            if match:
                urls.append(f"{match.group(1)}/n/json/lightscore")
        urls.append(NDC_ECONOMY_JSON_URL)

        deduped: list[str] = []
        for url in urls:
            if url and url not in deduped:
                deduped.append(url)
        return deduped

    def _parse_economy_record_from_dict(self, row: dict[str, Any]) -> dict[str, Any] | None:
        score: int | None = None
        period: tuple[int, str, str] | None = None
        signal_text: str | None = None

        for key, value in row.items():
            if isinstance(value, (dict, list, tuple)):
                continue

            key_norm = normalize_json_key(key)
            key_text = str(key)

            if score is None:
                if (
                    "lightscore" in key_norm
                    or "score" in key_norm
                    or "綜合分數" in key_text
                    or ("景氣" in key_text and "分數" in key_text)
                    or ("信號" in key_text and "分數" in key_text)
                ):
                    parsed_score = parse_int_str(value)
                    if parsed_score is not None:
                        score = parsed_score

            if period is None:
                if (
                    key_norm in {"date", "month", "ym", "yearmonth", "yyyymm", "period"}
                    or "date" in key_norm
                    or "month" in key_norm
                    or "年月" in key_text
                    or "期間" in key_text
                    or "日期" in key_text
                ):
                    parsed_period = parse_year_month(value)
                    if parsed_period is not None:
                        period = parsed_period

            if signal_text is None:
                if (
                    "signal" in key_norm
                    or "light" in key_norm
                    or "color" in key_norm
                    or "燈號" in key_text
                    or "信號" in key_text
                ):
                    signal_text = normalize_signal_color_text(_optional_str(value))

        if score is None or period is None:
            return None

        color_name, score_range = economy_color_range(score)
        return {
            "period_key": period[0],
            "display": period[1],
            "raw_id": period[2],
            "score": score,
            "signal_text": signal_text,
            "color_name": color_name,
            "score_range": score_range,
        }

    def _select_latest_economy_records_from_json(self, payload: Any) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        stack: list[Any] = [payload]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                parsed = self._parse_economy_record_from_dict(node)
                if parsed:
                    candidates.append(parsed)
                for value in node.values():
                    if isinstance(value, (dict, list, tuple)):
                        stack.append(value)
            elif isinstance(node, (list, tuple)):
                for item in node:
                    if isinstance(item, (dict, list, tuple)):
                        stack.append(item)

        if not candidates:
            return []

        candidates.sort(key=lambda item: int(item["period_key"]), reverse=True)
        unique: list[dict[str, Any]] = []
        seen_periods: set[int] = set()
        for item in candidates:
            period_key = int(item["period_key"])
            if period_key in seen_periods:
                continue
            unique.append(item)
            seen_periods.add(period_key)
            if len(unique) >= 2:
                break
        return unique

    def _load_json_flexibly(self, text: str) -> Any:
        stripped = (text or "").strip()
        if not stripped:
            raise ValueError("空白 JSON 回應")

        if stripped.startswith(")]}',"):
            stripped = stripped[5:].strip()

        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

        for open_char, close_char in (("{", "}"), ("[", "]")):
            start = stripped.find(open_char)
            end = stripped.rfind(close_char)
            if start != -1 and end > start:
                candidate = stripped[start : end + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    continue
        raise ValueError("無法解析 JSON 內容")

    async def _fetch_economy_release_from_ndc_json(self) -> dict[str, Any] | None:
        if not self.session:
            return None

        headers = {
            "User-Agent": "Mozilla/5.0 StockWarningBot/1.0",
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Origin": "https://index.ndc.gov.tw",
            "Referer": self.settings.economy_page_url or NDC_ECONOMY_PAGE_URL,
        }

        errors: list[str] = []
        for source_api_url in self._candidate_economy_json_urls():
            for method in ("POST", "GET"):
                try:
                    async with self.session.request(
                        method, source_api_url, headers=headers
                    ) as resp:
                        if resp.status >= 400:
                            raise RuntimeError(f"回應狀態碼 {resp.status}")
                        body = await resp.text(errors="ignore")

                    payload = self._load_json_flexibly(body)
                    records = self._select_latest_economy_records_from_json(payload)
                    if not records:
                        raise RuntimeError("JSON 內找不到可用的景氣分數與月份")
                    latest = records[0]
                    previous = records[1] if len(records) > 1 else None

                    signal_text = latest.get("signal_text") or latest.get("color_name")
                    return {
                        "release_id": f"period:{latest['raw_id']}:score:{latest['score']}",
                        "display": latest["display"],
                        "date_raw": latest["raw_id"],
                        "score": latest["score"],
                        "signal_text": signal_text,
                        "color_name": latest["color_name"],
                        "score_range": latest["score_range"],
                        "previous_display": previous["display"] if previous else None,
                        "previous_score": previous["score"] if previous else None,
                        "previous_color_name": previous["color_name"] if previous else None,
                        "previous_score_range": previous["score_range"] if previous else None,
                        "source_api_url": source_api_url,
                        "source_page_url": self.settings.economy_page_url,
                        "official_page_url": self.settings.economy_page_url,
                    }
                except Exception as exc:
                    errors.append(
                        f"{method} {source_api_url[:100]}... -> {type(exc).__name__}: {exc}"
                    )

        summary = "；".join(errors[:4]) if errors else "無可用 JSON 來源"
        raise RuntimeError(f"景氣資料抓取失敗：{summary}")

    async def _fetch_economy_release_from_zip(self) -> dict[str, Any] | None:
        if not self.session:
            return None

        page_headers = {
            "User-Agent": "Mozilla/5.0 StockWarningBot/1.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://data.gov.tw/",
        }
        download_headers = {
            "User-Agent": "Mozilla/5.0 StockWarningBot/1.0",
            "Accept": "application/zip,application/octet-stream,*/*",
            "Referer": DATA_GOV_DATASET_URL,
        }

        candidate_urls: list[str] = []
        if self.settings.economy_zip_url:
            candidate_urls.append(self.settings.economy_zip_url)

        try:
            async with self.session.get(DATA_GOV_DATASET_URL, headers=page_headers) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"data.gov.tw 回應狀態碼 {resp.status}")
                html = await resp.text(errors="ignore")
            for raw_url in NDC_ECONOMY_ZIP_URL_PATTERN.findall(html):
                parsed = unescape(raw_url).replace("&amp;", "&")
                if parsed not in candidate_urls:
                    candidate_urls.append(parsed)
        except Exception as exc:
            logging.warning("解析 data.gov 下載連結失敗：%s", exc)

        if NDC_DEFAULT_ZIP_URL not in candidate_urls:
            candidate_urls.append(NDC_DEFAULT_ZIP_URL)

        errors: list[str] = []
        for zip_url in candidate_urls:
            try:
                async with self.session.get(zip_url, headers=download_headers) as resp:
                    if resp.status >= 400:
                        raise RuntimeError(f"ZIP 下載回應狀態碼 {resp.status}")
                    zip_bytes = await resp.read()
                return self._parse_economy_zip_bytes(zip_bytes, zip_url)
            except Exception as exc:
                errors.append(f"{zip_url[:120]}... -> {type(exc).__name__}: {exc}")
                continue

        summary = "；".join(errors[:3]) if errors else "無可用 ZIP 來源"
        raise RuntimeError(f"景氣資料抓取失敗：{summary}")

    def _parse_economy_zip_bytes(self, zip_bytes: bytes, zip_url: str) -> dict[str, Any]:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            csv_name = None
            # ZIP 內常同時存在 schema-*.csv 與資料本體，優先拿真正資料檔。
            for name in archive.namelist():
                base = Path(name).name
                if base == "景氣指標與燈號.csv":
                    csv_name = name
                    break
            if not csv_name:
                for name in archive.namelist():
                    base = Path(name).name
                    if base.endswith("景氣指標與燈號.csv") and not base.startswith("schema-"):
                        csv_name = name
                        break
            if not csv_name:
                raise RuntimeError("ZIP 中找不到景氣指標與燈號.csv")

            csv_text = archive.read(csv_name).decode("utf-8-sig", errors="ignore")

        reader = csv.DictReader(io.StringIO(csv_text))
        parsed_rows: list[dict[str, Any]] = []
        for row in reader:
            if not isinstance(row, dict):
                continue
            period = parse_year_month(row.get("Date"))
            if period is None:
                period = parse_year_month(row.get("年月"))
            if period is None:
                continue
            score = parse_int_str(row.get("景氣對策信號綜合分數"))
            if score is None:
                continue
            color_name, score_range = economy_color_range(score)
            parsed_rows.append(
                {
                    "period_key": period[0],
                    "display": period[1],
                    "raw_id": period[2],
                    "score": score,
                    "signal_text": normalize_signal_color_text(
                        _optional_str(row.get("景氣對策信號"))
                    ),
                    "color_name": color_name,
                    "score_range": score_range,
                }
            )

        if not parsed_rows:
            raise RuntimeError("無法從 CSV 解析最新景氣資料")

        parsed_rows.sort(key=lambda item: int(item["period_key"]), reverse=True)
        unique_rows: list[dict[str, Any]] = []
        seen_periods: set[int] = set()
        for item in parsed_rows:
            period_key = int(item["period_key"])
            if period_key in seen_periods:
                continue
            unique_rows.append(item)
            seen_periods.add(period_key)
            if len(unique_rows) >= 2:
                break

        latest = unique_rows[0]
        previous = unique_rows[1] if len(unique_rows) > 1 else None

        return {
            "release_id": f"period:{latest['raw_id']}:score:{latest['score']}",
            "display": latest["display"],
            "date_raw": latest["raw_id"],
            "score": latest["score"],
            "signal_text": latest["signal_text"],
            "color_name": latest["color_name"],
            "score_range": latest["score_range"],
            "previous_display": previous["display"] if previous else None,
            "previous_score": previous["score"] if previous else None,
            "previous_color_name": previous["color_name"] if previous else None,
            "previous_score_range": previous["score_range"] if previous else None,
            "source_zip_url": zip_url,
            "source_api_url": None,
            "source_page_url": DATA_GOV_DATASET_URL,
            "official_page_url": self.settings.economy_page_url,
        }

    async def check_economy_for_user(
        self, user_id: int, user_payload: dict[str, Any], force_notify: bool = False
    ) -> bool:
        release_data = await self.get_latest_economy_release()
        if not release_data:
            return False

        latest_id = release_data.get("release_id")
        if not latest_id:
            return False

        should_notify = False

        def mutator(payload: dict[str, Any]) -> None:
            nonlocal should_notify
            economy_state = payload["state"].setdefault("economy", {})
            previous = economy_state.get("last_release_id")
            if previous is None:
                economy_state["last_release_id"] = latest_id
                should_notify = force_notify
            elif previous != latest_id:
                economy_state["last_release_id"] = latest_id
                should_notify = True
            elif force_notify:
                should_notify = True

        await self.store.update_user(user_id, mutator)
        if not should_notify:
            return True

        lines = [
            "[景氣對策信號更新通知]",
            f"最新月份: {release_data.get('display')}",
            f"景氣對策信號綜合分數: {release_data.get('score')} 分",
            f"燈號區間: {release_data.get('score_range')}（{release_data.get('color_name')}）",
        ]
        if release_data.get("previous_score") is not None:
            lines.append(
                f"前一期分數: {release_data.get('previous_display')} "
                f"{release_data.get('previous_score')} 分"
            )
            if release_data.get("previous_score_range") and release_data.get(
                "previous_color_name"
            ):
                lines.append(
                    f"前一期燈號區間: {release_data.get('previous_score_range')}（{release_data.get('previous_color_name')}）"
                )
        if release_data.get("official_page_url"):
            lines.append(f"官方頁面: {release_data.get('official_page_url')}")
        if release_data.get("source_api_url"):
            lines.append(f"資料 API: {release_data.get('source_api_url')}")
        elif release_data.get("source_page_url"):
            lines.append(f"資料頁: {release_data.get('source_page_url')}")
        if release_data.get("source_zip_url"):
            lines.append(f"原始資料 ZIP: {release_data['source_zip_url']}")

        try:
            await self.send_alert_to_user(user_id, "\n".join(lines))
        except Exception:
            logging.exception("傳送使用者 %s 景氣通知失敗", user_id)
            raise
        return True


async def ensure_dm_interaction(interaction: discord.Interaction) -> bool:
    if interaction.guild_id is not None:
        await interaction.response.send_message(
            "請在與機器人的私訊中使用這個指令。",
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

    async def build_check_now_snapshot(user_id: int) -> str:
        payload = await bot.store.get_user(user_id)
        rows = payload.get("watchlist", [])
        rules: list[StockRule] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                rules.append(StockRule.from_dict(row))
            except ValueError:
                continue

        lines: list[str] = ["追蹤清單與股價:"]
        quote_map: dict[str, dict[str, Any]] = {}
        if bot.session and rules:
            symbols = sorted({rule.symbol for rule in rules})
            try:
                quote_map = await fetch_twse_quotes(bot.session, symbols)
            except Exception as exc:
                lines.append(f"- 股價資料取得失敗: {type(exc).__name__}")

        if not rules:
            lines.append("- 目前沒有追蹤股票")
        else:
            for rule in rules:
                quote = quote_map.get(rule.symbol)
                display_name = rule.name or (str(quote.get("name")) if quote else rule.symbol)
                if not quote:
                    lines.append(f"- {rule.symbol} ({display_name}): 無法取得報價")
                    continue
                price = parse_float_str(quote.get("price"))
                prev_close = parse_float_str(quote.get("prev_close"))
                if price is None:
                    lines.append(f"- {rule.symbol} ({display_name}): 無法取得報價")
                    continue
                if prev_close in (None, 0):
                    lines.append(f"- {rule.symbol} ({display_name}): {price:.2f}")
                    continue
                change_pct = ((price - prev_close) / prev_close) * 100
                lines.append(
                    f"- {rule.symbol} ({display_name}): {price:.2f} ({change_pct:+.2f}%)"
                )

        release_data = await bot.get_latest_economy_release()
        lines.append("")
        lines.append("景氣燈號:")
        if not release_data:
            lines.append("- 無法取得景氣資料")
            return "\n".join(lines)

        lines.append(f"- 最新月份: {release_data.get('display')}")
        lines.append(
            f"- 燈號: {release_data.get('score')} 分，"
            f"{release_data.get('score_range')}（{release_data.get('color_name')}）"
        )
        if release_data.get("previous_score") is not None:
            lines.append(
                f"- 前一期: {release_data.get('previous_display')} "
                f"{release_data.get('previous_score')} 分"
            )
            if release_data.get("previous_score_range") and release_data.get(
                "previous_color_name"
            ):
                lines.append(
                    f"- 前一期區間: {release_data.get('previous_score_range')}（{release_data.get('previous_color_name')}）"
                )
        return "\n".join(lines)

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
        snapshot = await build_check_now_snapshot(interaction.user.id)
        await interaction.followup.send(
            "\n".join(["已完成一次手動檢查。", *results, "", snapshot])
        )

    @bot.tree.command(name="sync_commands", description="手動同步 slash 指令")
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
                "再到私訊使用 `/sync_commands`。"
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
