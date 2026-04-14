import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urljoin

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv


YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
NDC_ECONOMY_PAGE_URL = (
    "https://www.ndc.gov.tw/News_Content.aspx?n=9D32B61B1E56E558"
    "&s=C367F13BF38C5711&sms=9D3CAFD318C60877"
)
DOWNLOAD_LINK_PATTERN = re.compile(
    r'href="([^"]*Download\.ashx[^"]+)"[^>]*>\s*(zip|xlsx|pdf|ods)\s*<',
    flags=re.IGNORECASE,
)
ROC_DATE_PATTERN = re.compile(r"發布日期：\s*(\d{2,3}-\d{2}-\d{2})")

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
    channel_id: int
    stock_interval_sec: int
    economy_interval_sec: int
    watchlist_path: Path
    state_path: Path
    economy_url: str
    guild_id: int | None

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        token = os.getenv("DISCORD_TOKEN", "").strip()
        channel_id_raw = os.getenv("DISCORD_CHANNEL_ID", "").strip()
        if not token:
            raise ValueError("DISCORD_TOKEN 未設定")
        if not channel_id_raw:
            raise ValueError("DISCORD_CHANNEL_ID 未設定")

        try:
            channel_id = int(channel_id_raw)
        except ValueError as exc:
            raise ValueError("DISCORD_CHANNEL_ID 必須是整數") from exc

        guild_raw = os.getenv("DISCORD_GUILD_ID", "").strip()
        guild_id = int(guild_raw) if guild_raw else None

        stock_interval = int(os.getenv("STOCK_CHECK_INTERVAL_SEC", "300"))
        economy_interval = int(os.getenv("ECONOMY_CHECK_INTERVAL_SEC", "21600"))

        watchlist = Path(os.getenv("WATCHLIST_PATH", "watchlist.json")).resolve()
        state = Path(os.getenv("STATE_PATH", "state.json")).resolve()
        economy_url = os.getenv("ECONOMY_SOURCE_URL", NDC_ECONOMY_PAGE_URL).strip()

        return cls(
            discord_token=token,
            channel_id=channel_id,
            stock_interval_sec=max(30, stock_interval),
            economy_interval_sec=max(300, economy_interval),
            watchlist_path=watchlist,
            state_path=state,
            economy_url=economy_url or NDC_ECONOMY_PAGE_URL,
            guild_id=guild_id,
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
        symbol = str(row.get("symbol", "")).strip().upper()
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


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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


def load_watchlist(path: Path) -> list[StockRule]:
    ensure_watchlist_exists(path)
    payload = read_json(path, {"stocks": []})
    stocks_raw = payload.get("stocks", [])
    if not isinstance(stocks_raw, list):
        raise ValueError("watchlist.json 的 stocks 必須是陣列")

    rules: list[StockRule] = []
    for row in stocks_raw:
        if not isinstance(row, dict):
            continue
        rules.append(StockRule.from_dict(row))
    return rules


def roc_to_iso(roc_date: str) -> str:
    year, month, day = roc_date.split("-")
    ad_year = int(year) + 1911
    return f"{ad_year:04d}-{month}-{day}"


def parse_economy_release_page(html: str, base_url: str) -> dict[str, Any]:
    match = ROC_DATE_PATTERN.search(html)
    roc_date = match.group(1) if match else None
    iso_date = roc_to_iso(roc_date) if roc_date else None

    links: dict[str, str] = {}
    for href, ext in DOWNLOAD_LINK_PATTERN.findall(html):
        full = urljoin(base_url, unescape(href))
        key = ext.lower()
        if key not in links:
            links[key] = full

    return {"roc_date": roc_date, "iso_date": iso_date, "links": links}


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


class StockWarningBot(commands.Bot):
    def __init__(self, settings: Settings):
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self.settings = settings
        self.session: aiohttp.ClientSession | None = None
        self.state_lock = asyncio.Lock()
        self.state = read_json(
            settings.state_path,
            {"stock_alerts": {}, "economy": {"last_release_date": None}},
        )
        self.background_tasks: list[asyncio.Task[Any]] = []

    async def setup_hook(self) -> None:
        timeout = aiohttp.ClientTimeout(total=20)
        self.session = aiohttp.ClientSession(timeout=timeout)
        self.background_tasks.append(
            asyncio.create_task(
                self._run_periodic_loop(
                    loop_name="stock",
                    interval_sec=self.settings.stock_interval_sec,
                    callback=self.check_stocks,
                )
            )
        )
        self.background_tasks.append(
            asyncio.create_task(
                self._run_periodic_loop(
                    loop_name="economy",
                    interval_sec=self.settings.economy_interval_sec,
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

    async def _run_periodic_loop(
        self, loop_name: str, interval_sec: int, callback
    ) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            start = monotonic()
            try:
                await callback()
            except Exception:
                logging.exception("週期任務失敗: %s", loop_name)
            elapsed = monotonic() - start
            await asyncio.sleep(max(1.0, interval_sec - elapsed))

    async def send_alert(self, message: str) -> None:
        channel = self.get_channel(self.settings.channel_id)
        if channel is None:
            channel = await self.fetch_channel(self.settings.channel_id)

        if not isinstance(channel, discord.abc.Messageable):
            raise RuntimeError("DISCORD_CHANNEL_ID 不是可發訊息的頻道")

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
                    if not condition_text:
                        continue
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

    async def check_economy_release(self) -> None:
        if not self.session:
            return

        headers = {"User-Agent": "Mozilla/5.0 StockWarningBot/1.0"}
        async with self.session.get(self.settings.economy_url, headers=headers) as resp:
            resp.raise_for_status()
            html = await resp.text(errors="ignore")

        data = parse_economy_release_page(html, self.settings.economy_url)
        latest = data.get("iso_date")
        if not latest:
            logging.warning("找不到景氣燈號發布日期")
            return

        should_notify = False
        async with self.state_lock:
            economy_state = self.state.setdefault("economy", {})
            previous = economy_state.get("last_release_date")
            if previous is None:
                economy_state["last_release_date"] = latest
                write_json(self.settings.state_path, self.state)
            elif previous != latest:
                economy_state["last_release_date"] = latest
                write_json(self.settings.state_path, self.state)
                should_notify = True

        if not should_notify:
            return

        links = data.get("links", {})
        lines = [
            "[景氣燈號更新通知]",
            f"發布日期: {latest} (民國 {data.get('roc_date')})",
            f"官方頁面: {self.settings.economy_url}",
        ]
        if links.get("xlsx"):
            lines.append(f"xlsx: {links['xlsx']}")
        if links.get("zip"):
            lines.append(f"zip: {links['zip']}")

        await self.send_alert("\n".join(lines))


def build_bot(settings: Settings) -> StockWarningBot:
    bot = StockWarningBot(settings)

    @bot.tree.command(name="status", description="查看機器人監控狀態")
    async def status(interaction: discord.Interaction) -> None:
        rules = load_watchlist(settings.watchlist_path)
        await interaction.response.send_message(
            "\n".join(
                [
                    "StockWarning Bot 狀態:",
                    f"- 追蹤股票數: {len(rules)}",
                    f"- 股票輪詢: {settings.stock_interval_sec} 秒",
                    f"- 景氣燈號輪詢: {settings.economy_interval_sec} 秒",
                    f"- 通知頻道: {settings.channel_id}",
                ]
            ),
            ephemeral=True,
        )

    @bot.tree.command(name="check_now", description="立即檢查一次股票與景氣燈號")
    async def check_now(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await bot.check_stocks()
        await bot.check_economy_release()
        await interaction.followup.send("已完成一次手動檢查。", ephemeral=True)

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
