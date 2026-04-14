import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from time import monotonic
from typing import Any, Callable
from urllib.parse import urljoin

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
NDC_ECONOMY_PAGE_URL = "https://index.ndc.gov.tw/n/zh_twr"
NDC_ECONOMY_API_URL = "https://index.ndc.gov.tw/n/json/lightscore"
DOWNLOAD_LINK_PATTERN = re.compile(
    r'href="([^"]*Download\.ashx[^"]+)"[^>]*>\s*(zip|xlsx|pdf|ods)\s*<',
    flags=re.IGNORECASE,
)
ROC_DATE_PATTERN = re.compile(r"發布日期：\s*(\d{2,3}-\d{2}-\d{2})")
AD_DATE_PATTERN = re.compile(r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})")
PERIOD_PATTERNS: list[tuple[re.Pattern[str], bool]] = [
    (re.compile(r"\b(20\d{2})[./-]?M?([01]?\d)\b", flags=re.IGNORECASE), False),
    (re.compile(r"\b(1\d{2})[./-]?M?([01]?\d)\b", flags=re.IGNORECASE), True),
    (re.compile(r"\b(20\d{2})([01]\d)\b"), False),
    (re.compile(r"\b(1\d{2})([01]\d)\b"), True),
]

DEFAULT_WATCHLIST = {
    "stocks": [
        {
            "symbol": "2330.TW",
            "name": "TSMC",
            "up_pct": 3.0,
            "down_pct": 3.0,
            "target_high": None,
            "target_low": None,
        },
        {
            "symbol": "AAPL",
            "name": "Apple",
            "up_pct": 2.0,
            "down_pct": 2.0,
            "target_high": None,
            "target_low": None,
        },
    ]
}


@dataclass
class Settings:
    discord_token: str
    watchlist_path: Path
    state_path: Path
    config_path: Path
    economy_page_url: str
    economy_api_url: str
    guild_id: int | None
    manual_check_timeout_sec: int
    default_channel_id: int | None
    default_stock_interval_sec: int
    default_economy_interval_sec: int

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise ValueError("DISCORD_TOKEN 未設定")

        channel_id_raw = os.getenv("DISCORD_CHANNEL_ID", "").strip()
        default_channel_id = _optional_int(channel_id_raw)

        guild_raw = os.getenv("DISCORD_GUILD_ID", "").strip()
        guild_id = int(guild_raw) if guild_raw else None

        default_stock_interval = _bounded_int(
            os.getenv("STOCK_CHECK_INTERVAL_SEC"), fallback=300, minimum=30
        )
        default_economy_interval = _bounded_int(
            os.getenv("ECONOMY_CHECK_INTERVAL_SEC"), fallback=21600, minimum=300
        )
        manual_timeout = _bounded_int(
            os.getenv("MANUAL_CHECK_TIMEOUT_SEC"), fallback=45, minimum=10
        )

        watchlist = Path(os.getenv("WATCHLIST_PATH", "watchlist.json")).resolve()
        state = Path(os.getenv("STATE_PATH", "state.json")).resolve()
        config = Path(os.getenv("RUNTIME_CONFIG_PATH", "config.json")).resolve()

        economy_page_url = os.getenv("ECONOMY_SOURCE_URL", NDC_ECONOMY_PAGE_URL).strip()
        economy_api_url = os.getenv("ECONOMY_API_URL", NDC_ECONOMY_API_URL).strip()

        return cls(
            discord_token=token,
            watchlist_path=watchlist,
            state_path=state,
            config_path=config,
            economy_page_url=economy_page_url or NDC_ECONOMY_PAGE_URL,
            economy_api_url=economy_api_url or NDC_ECONOMY_API_URL,
            guild_id=guild_id,
            manual_check_timeout_sec=manual_timeout,
            default_channel_id=default_channel_id,
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
        if not symbol:
            raise ValueError("watchlist 中有股票缺少 symbol")

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
    return str(value or "").strip().upper()


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


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


def ensure_watchlist_exists(path: Path) -> None:
    if path.exists():
        return
    write_json(path, DEFAULT_WATCHLIST)


def load_watchlist_rows(path: Path) -> list[dict[str, Any]]:
    ensure_watchlist_exists(path)
    payload = read_json(path, {"stocks": []})
    stocks_raw = payload.get("stocks", [])
    if not isinstance(stocks_raw, list):
        raise ValueError("watchlist.json 的 stocks 必須是陣列")

    rows: list[dict[str, Any]] = []
    for row in stocks_raw:
        if not isinstance(row, dict):
            continue
        symbol = normalize_stock_symbol(row.get("symbol"))
        if not symbol:
            continue
        rows.append(
            {
                "symbol": symbol,
                "name": _optional_str(row.get("name")),
                "up_pct": _optional_float(row.get("up_pct")),
                "down_pct": _optional_float(row.get("down_pct")),
                "target_high": _optional_float(row.get("target_high")),
                "target_low": _optional_float(row.get("target_low")),
            }
        )
    return rows


def save_watchlist_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    write_json(path, {"stocks": rows})


def load_watchlist(path: Path) -> list[StockRule]:
    rows = load_watchlist_rows(path)
    return [StockRule.from_dict(row) for row in rows]


def load_runtime_config(settings: Settings) -> dict[str, Any]:
    raw = read_json(settings.config_path, {})
    channel_id = _optional_int(raw.get("notification_channel_id"))
    if channel_id is None:
        channel_id = settings.default_channel_id

    config = {
        "notification_channel_id": channel_id,
        "stock_interval_sec": _bounded_int(
            raw.get("stock_interval_sec"),
            fallback=settings.default_stock_interval_sec,
            minimum=30,
        ),
        "economy_interval_sec": _bounded_int(
            raw.get("economy_interval_sec"),
            fallback=settings.default_economy_interval_sec,
            minimum=300,
        ),
    }
    write_json(settings.config_path, config)
    return config


def roc_to_iso(roc_date: str) -> str:
    year, month, day = roc_date.split("-")
    ad_year = int(year) + 1911
    return f"{ad_year:04d}-{month}-{day}"


def parse_economy_release_page(html: str, base_url: str) -> dict[str, Any]:
    match = ROC_DATE_PATTERN.search(html)
    roc_date = match.group(1) if match else None
    iso_date = roc_to_iso(roc_date) if roc_date else None

    if not iso_date:
        ad_match = AD_DATE_PATTERN.search(html)
        if ad_match:
            iso_date = (
                f"{int(ad_match.group(1)):04d}-"
                f"{int(ad_match.group(2)):02d}-"
                f"{int(ad_match.group(3)):02d}"
            )

    links: dict[str, str] = {}
    for href, ext in DOWNLOAD_LINK_PATTERN.findall(html):
        full = urljoin(base_url, unescape(href))
        key = ext.lower()
        if key not in links:
            links[key] = full

    return {"roc_date": roc_date, "iso_date": iso_date, "links": links}


def extract_latest_period(payload: Any) -> str | None:
    candidates: set[tuple[int, int]] = set()
    stack: list[Any] = [payload]

    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, (str, int, float)):
            text = str(node)
            for pattern, is_roc_year in PERIOD_PATTERNS:
                for match in pattern.finditer(text):
                    year = int(match.group(1))
                    month = int(match.group(2))
                    if not (1 <= month <= 12):
                        continue
                    if is_roc_year:
                        year += 1911
                    if 1990 <= year <= 2100:
                        candidates.add((year, month))

    if not candidates:
        return None

    year, month = max(candidates)
    return f"{year:04d}-{month:02d}"


async def fetch_quotes(
    session: aiohttp.ClientSession, symbols: list[str]
) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}

    headers = {"User-Agent": "Mozilla/5.0 StockWarningBot/1.0"}
    params = {"symbols": ",".join(symbols)}
    async with session.get(YAHOO_QUOTE_URL, params=params, headers=headers) as resp:
        resp.raise_for_status()
        payload = await resp.json()

    results = payload.get("quoteResponse", {}).get("result", [])
    output: dict[str, dict[str, Any]] = {}
    for row in results:
        symbol = str(row.get("symbol", "")).upper()
        if symbol:
            output[symbol] = row
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
        self.session: aiohttp.ClientSession | None = None
        self.state_lock = asyncio.Lock()
        self.config_lock = asyncio.Lock()
        self.state = read_json(
            settings.state_path,
            {"stock_alerts": {}, "economy": {"last_release_id": None}},
        )
        self.runtime_config = load_runtime_config(settings)
        self.background_tasks: list[asyncio.Task[Any]] = []

    async def setup_hook(self) -> None:
        timeout = aiohttp.ClientTimeout(total=20)
        self.session = aiohttp.ClientSession(timeout=timeout)
        self.background_tasks.append(
            asyncio.create_task(
                self._run_periodic_loop(
                    loop_name="stock",
                    interval_getter=self.get_stock_interval_sec,
                    callback=self.check_stocks,
                )
            )
        )
        self.background_tasks.append(
            asyncio.create_task(
                self._run_periodic_loop(
                    loop_name="economy",
                    interval_getter=self.get_economy_interval_sec,
                    callback=self.check_economy_release,
                )
            )
        )

        if self.settings.guild_id:
            guild = discord.Object(id=self.settings.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

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

    def get_notification_channel_id(self) -> int | None:
        return _optional_int(self.runtime_config.get("notification_channel_id"))

    def get_stock_interval_sec(self) -> int:
        return _bounded_int(
            self.runtime_config.get("stock_interval_sec"),
            fallback=self.settings.default_stock_interval_sec,
            minimum=30,
        )

    def get_economy_interval_sec(self) -> int:
        return _bounded_int(
            self.runtime_config.get("economy_interval_sec"),
            fallback=self.settings.default_economy_interval_sec,
            minimum=300,
        )

    async def set_notification_channel(self, channel_id: int | None) -> None:
        async with self.config_lock:
            self.runtime_config["notification_channel_id"] = channel_id
            write_json(self.settings.config_path, self.runtime_config)

    async def set_intervals(
        self, stock_interval_sec: int | None = None, economy_interval_sec: int | None = None
    ) -> None:
        async with self.config_lock:
            if stock_interval_sec is not None:
                self.runtime_config["stock_interval_sec"] = max(30, int(stock_interval_sec))
            if economy_interval_sec is not None:
                self.runtime_config["economy_interval_sec"] = max(
                    300, int(economy_interval_sec)
                )
            write_json(self.settings.config_path, self.runtime_config)

    async def clear_symbol_alert_state(self, symbol: str) -> None:
        async with self.state_lock:
            stock_state = self.state.setdefault("stock_alerts", {})
            keys_to_remove = [
                key for key in list(stock_state.keys()) if key.startswith(f"{symbol}|")
            ]
            if not keys_to_remove:
                return
            for key in keys_to_remove:
                stock_state.pop(key, None)
            write_json(self.settings.state_path, self.state)

    async def _run_periodic_loop(
        self,
        loop_name: str,
        interval_getter: Callable[[], int],
        callback: Callable[[], Any],
    ) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            start = monotonic()
            try:
                await callback()
            except Exception:
                logging.exception("週期任務失敗: %s", loop_name)
            elapsed = monotonic() - start
            interval_sec = max(1, int(interval_getter()))
            await asyncio.sleep(max(1.0, interval_sec - elapsed))

    async def send_alert(self, message: str) -> None:
        channel_id = self.get_notification_channel_id()
        if not channel_id:
            logging.info("通知頻道尚未設定，略過通知。")
            return

        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)

        if not isinstance(channel, discord.abc.Messageable):
            raise RuntimeError("設定的通知頻道不是可發訊息頻道")

        await channel.send(message)

    async def check_stocks(self) -> None:
        if not self.session:
            return

        rules = load_watchlist(self.settings.watchlist_path)
        if not rules:
            return

        symbols = sorted({rule.symbol for rule in rules})
        quotes = await fetch_quotes(self.session, symbols)
        pending_alerts: list[str] = []

        async with self.state_lock:
            stock_state = self.state.setdefault("stock_alerts", {})
            changed = False

            for rule in rules:
                quote = quotes.get(rule.symbol)
                if not quote:
                    continue

                price = _optional_float(quote.get("regularMarketPrice"))
                prev_close = _optional_float(quote.get("regularMarketPreviousClose"))
                if price is None or prev_close in (None, 0):
                    continue

                change_pct = ((price - prev_close) / prev_close) * 100
                display_name = rule.name or str(
                    quote.get("shortName") or quote.get("longName") or rule.symbol
                )

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
                        changed = True
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
                        changed = True

            if changed:
                write_json(self.settings.state_path, self.state)

        for message in pending_alerts:
            await self.send_alert(message)

    async def _fetch_economy_release_from_api(self) -> dict[str, Any] | None:
        if not self.session:
            return None

        headers = {
            "User-Agent": "Mozilla/5.0 StockWarningBot/1.0",
            "Accept": "application/json,text/plain,*/*",
        }
        async with self.session.post(
            self.settings.economy_api_url, headers=headers, data={}
        ) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"API 回應狀態碼 {resp.status}")
            payload = await resp.json(content_type=None)

        latest_period = extract_latest_period(payload)
        if not latest_period:
            raise RuntimeError("無法從景氣對策信號 API 解析最新月份")

        return {
            "release_id": f"period:{latest_period}",
            "display": latest_period,
        }

    async def _fetch_economy_release_from_page(self) -> dict[str, Any] | None:
        if not self.session:
            return None

        headers = {"User-Agent": "Mozilla/5.0 StockWarningBot/1.0"}
        async with self.session.get(self.settings.economy_page_url, headers=headers) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"網頁回應狀態碼 {resp.status}")
            html = await resp.text(errors="ignore")

        page_data = parse_economy_release_page(html, self.settings.economy_page_url)
        if page_data.get("iso_date"):
            return {
                "release_id": f"date:{page_data['iso_date']}",
                "display": page_data["iso_date"],
                "roc_date": page_data.get("roc_date"),
                "links": page_data.get("links", {}),
            }
        return None

    async def check_economy_release(self) -> None:
        release_data: dict[str, Any] | None = None

        try:
            release_data = await self._fetch_economy_release_from_api()
        except Exception:
            logging.exception("景氣對策信號 API 檢查失敗，改用網頁備援。")

        if release_data is None:
            try:
                release_data = await self._fetch_economy_release_from_page()
            except Exception:
                logging.exception("景氣對策信號網頁備援檢查也失敗。")
                return

        if not release_data:
            logging.warning("找不到可用的景氣對策信號最新資料。")
            return

        latest_id = release_data.get("release_id")
        if not latest_id:
            return

        should_notify = False
        async with self.state_lock:
            economy_state = self.state.setdefault("economy", {})
            previous = economy_state.get("last_release_id") or economy_state.get(
                "last_release_date"
            )
            if previous is None:
                economy_state["last_release_id"] = latest_id
                write_json(self.settings.state_path, self.state)
            elif previous != latest_id:
                economy_state["last_release_id"] = latest_id
                write_json(self.settings.state_path, self.state)
                should_notify = True

        if not should_notify:
            return

        lines = [
            "[景氣對策信號更新通知]",
            f"最新月份/日期: {release_data.get('display')}",
            f"來源頁面: {self.settings.economy_page_url}",
        ]
        if release_data.get("roc_date"):
            lines.append(f"民國發布日期: {release_data['roc_date']}")
        links = release_data.get("links", {})
        if links.get("xlsx"):
            lines.append(f"xlsx: {links['xlsx']}")
        if links.get("zip"):
            lines.append(f"zip: {links['zip']}")

        await self.send_alert("\n".join(lines))


def build_bot(settings: Settings) -> StockWarningBot:
    bot = StockWarningBot(settings)

    async def run_manual_check(label: str, callback: Callable[[], Any]) -> str:
        try:
            await asyncio.wait_for(
                callback(), timeout=settings.manual_check_timeout_sec
            )
            return f"- {label}: 完成"
        except asyncio.TimeoutError:
            logging.warning("手動檢查逾時: %s", label)
            return f"- {label}: 逾時（>{settings.manual_check_timeout_sec} 秒）"
        except Exception as exc:
            logging.exception("手動檢查失敗: %s", label)
            return f"- {label}: 失敗（{type(exc).__name__}: {exc}）"

    @bot.tree.command(name="status", description="查看機器人監控狀態")
    async def status(interaction: discord.Interaction) -> None:
        rules = load_watchlist(settings.watchlist_path)
        channel_id = bot.get_notification_channel_id()
        channel_text = f"<#{channel_id}>" if channel_id else "未設定"
        await interaction.response.send_message(
            "\n".join(
                [
                    "StockWarning Bot 狀態:",
                    f"- 追蹤股票數: {len(rules)}",
                    f"- 股票輪詢: {bot.get_stock_interval_sec()} 秒",
                    f"- 景氣燈號輪詢: {bot.get_economy_interval_sec()} 秒",
                    f"- 通知頻道: {channel_text}",
                ]
            ),
            ephemeral=True,
        )

    @bot.tree.command(name="settings_show", description="查看通知頻道與輪詢秒數")
    async def settings_show(interaction: discord.Interaction) -> None:
        channel_id = bot.get_notification_channel_id()
        channel_text = f"<#{channel_id}>" if channel_id else "未設定"
        await interaction.response.send_message(
            "\n".join(
                [
                    "目前設定:",
                    f"- 通知頻道: {channel_text}",
                    f"- 股票輪詢秒數: {bot.get_stock_interval_sec()}",
                    f"- 景氣對策信號輪詢秒數: {bot.get_economy_interval_sec()}",
                ]
            ),
            ephemeral=True,
        )

    @bot.tree.command(name="settings_set_channel", description="設定通知頻道")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def settings_set_channel(
        interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        await bot.set_notification_channel(channel.id)
        await interaction.response.send_message(
            f"已設定通知頻道為 {channel.mention}", ephemeral=True
        )

    @bot.tree.command(
        name="settings_set_interval",
        description="設定股票/景氣對策信號輪詢秒數（可只改一個）",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def settings_set_interval(
        interaction: discord.Interaction,
        stock_seconds: int | None = None,
        economy_seconds: int | None = None,
    ) -> None:
        if stock_seconds is None and economy_seconds is None:
            await interaction.response.send_message(
                "請至少填一個參數：`stock_seconds` 或 `economy_seconds`。",
                ephemeral=True,
            )
            return
        if stock_seconds is not None and stock_seconds < 30:
            await interaction.response.send_message(
                "`stock_seconds` 最小值是 30 秒。", ephemeral=True
            )
            return
        if economy_seconds is not None and economy_seconds < 300:
            await interaction.response.send_message(
                "`economy_seconds` 最小值是 300 秒。", ephemeral=True
            )
            return

        await bot.set_intervals(stock_seconds, economy_seconds)
        await interaction.response.send_message(
            "\n".join(
                [
                    "已更新輪詢設定。",
                    f"- 股票輪詢秒數: {bot.get_stock_interval_sec()}",
                    f"- 景氣對策信號輪詢秒數: {bot.get_economy_interval_sec()}",
                ]
            ),
            ephemeral=True,
        )

    @bot.tree.command(name="watchlist_show", description="查看追蹤股票清單")
    async def watchlist_show(interaction: discord.Interaction) -> None:
        rules = load_watchlist(settings.watchlist_path)
        if not rules:
            await interaction.response.send_message("目前沒有追蹤股票。", ephemeral=True)
            return

        lines = ["追蹤股票清單:"]
        for index, rule in enumerate(rules, start=1):
            lines.append(format_stock_rule_line(index, rule))
            if len("\n".join(lines)) > 1700:
                lines.append("...清單過長，請縮小追蹤數量或拆分管理。")
                break

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @bot.tree.command(name="watchlist_add", description="新增追蹤股票")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def watchlist_add(
        interaction: discord.Interaction,
        symbol: str,
        name: str | None = None,
        up_pct: float | None = None,
        down_pct: float | None = None,
        target_high: float | None = None,
        target_low: float | None = None,
    ) -> None:
        symbol = normalize_stock_symbol(symbol)
        if not symbol:
            await interaction.response.send_message("`symbol` 不能是空值。", ephemeral=True)
            return
        if up_pct is not None and up_pct < 0:
            await interaction.response.send_message(
                "`up_pct` 請填正數或 0。", ephemeral=True
            )
            return
        if down_pct is not None and down_pct < 0:
            await interaction.response.send_message(
                "`down_pct` 請填正數或 0。", ephemeral=True
            )
            return
        if (
            target_high is not None
            and target_low is not None
            and target_high < target_low
        ):
            await interaction.response.send_message(
                "`target_high` 不能小於 `target_low`。", ephemeral=True
            )
            return

        rows = load_watchlist_rows(settings.watchlist_path)
        if any(row.get("symbol") == symbol for row in rows):
            await interaction.response.send_message(
                f"{symbol} 已在追蹤清單中。", ephemeral=True
            )
            return

        new_rule = StockRule(
            symbol=symbol,
            name=_optional_str(name),
            up_pct=up_pct,
            down_pct=down_pct,
            target_high=target_high,
            target_low=target_low,
        )
        rows.append(new_rule.to_dict())
        save_watchlist_rows(settings.watchlist_path, rows)

        await interaction.response.send_message(
            f"已新增追蹤：{format_stock_rule_line(len(rows), new_rule)}",
            ephemeral=True,
        )

    @bot.tree.command(name="watchlist_update", description="更新追蹤股票條件")
    @app_commands.checks.has_permissions(manage_guild=True)
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
        symbol = normalize_stock_symbol(symbol)
        if not symbol:
            await interaction.response.send_message("`symbol` 不能是空值。", ephemeral=True)
            return

        rows = load_watchlist_rows(settings.watchlist_path)
        target_row = None
        for row in rows:
            if row.get("symbol") == symbol:
                target_row = row
                break

        if target_row is None:
            await interaction.response.send_message(
                f"找不到 {symbol}，請先用 `/watchlist_add` 新增。",
                ephemeral=True,
            )
            return

        if up_pct is not None and up_pct < 0:
            await interaction.response.send_message(
                "`up_pct` 請填正數或 0。", ephemeral=True
            )
            return
        if down_pct is not None and down_pct < 0:
            await interaction.response.send_message(
                "`down_pct` 請填正數或 0。", ephemeral=True
            )
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
            await interaction.response.send_message(
                "`target_high` 不能小於 `target_low`。", ephemeral=True
            )
            return

        save_watchlist_rows(settings.watchlist_path, rows)
        updated = StockRule.from_dict(target_row)
        await interaction.response.send_message(
            f"已更新：{format_stock_rule_line(1, updated)[3:]}",
            ephemeral=True,
        )

    @bot.tree.command(name="watchlist_remove", description="移除追蹤股票")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def watchlist_remove(interaction: discord.Interaction, symbol: str) -> None:
        symbol = normalize_stock_symbol(symbol)
        if not symbol:
            await interaction.response.send_message("`symbol` 不能是空值。", ephemeral=True)
            return

        rows = load_watchlist_rows(settings.watchlist_path)
        before = len(rows)
        rows = [row for row in rows if row.get("symbol") != symbol]
        if len(rows) == before:
            await interaction.response.send_message(
                f"{symbol} 不在追蹤清單中。", ephemeral=True
            )
            return

        save_watchlist_rows(settings.watchlist_path, rows)
        await bot.clear_symbol_alert_state(symbol)
        await interaction.response.send_message(
            f"已移除追蹤股票：{symbol}", ephemeral=True
        )

    @bot.tree.command(name="check_now", description="立即檢查一次股票與景氣對策信號")
    async def check_now(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        results = await asyncio.gather(
            run_manual_check("股票檢查", bot.check_stocks),
            run_manual_check("景氣對策信號檢查", bot.check_economy_release),
        )
        await interaction.followup.send(
            "\n".join(["已完成一次手動檢查。", *results]),
            ephemeral=True,
        )

    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        logging.exception("Slash 指令失敗", exc_info=error)

        if isinstance(error, app_commands.MissingPermissions):
            message = "你需要 `管理伺服器` 權限才能使用這個指令。"
        else:
            message = f"指令執行失敗：{type(error).__name__}"

        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            logging.exception("無法回覆 slash 指令錯誤訊息")

    return bot


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    settings = Settings.from_env()
    ensure_watchlist_exists(settings.watchlist_path)
    bot = build_bot(settings)
    bot.run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
