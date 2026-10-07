"""Discord webhook notifications: one channel for placed trades, another for settled results."""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

import httpx

from .trading import dollars

logger = logging.getLogger(__name__)
# httpx logs every request URL at INFO, and a webhook URL is a secret.
logging.getLogger("httpx").setLevel(logging.WARNING)

_WEBHOOK_PREFIXES = ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")
_GREEN, _RED, _BLUE, _GREY = 0x2ECC71, 0xE74C3C, 0x3498DB, 0x95A5A6
_TIMEOUT_SEC = 5.0
_MAX_RETRY_WAIT_SEC = 5.0


def valid_webhook(url: str) -> str:
    """Return the URL when it is a Discord webhook, else an empty string (notifications off)."""
    url = url.strip()
    if url and not url.startswith(_WEBHOOK_PREFIXES):
        logger.warning("Ignoring a Discord webhook setting that is not a discord.com webhook URL")
        return ""
    return url


def _money(value: Decimal) -> str:
    return f"-${abs(value):.2f}" if value < 0 else f"${value:.2f}"


def trade_embed(position: dict, environment: str) -> dict:
    fill = position["execution_history"][-1]
    filled, average = dollars(fill["filled"]), dollars(fill["average_price"])
    signal = position.get("entry_signal") or {}
    fields = [
        ("Market", position["ticker"], False),
        ("Side", position["side"].upper(), True),
        ("Contracts", f"{filled:f}", True),
        ("Avg price", _money(average), True),
        ("Fees", _money(dollars(fill["fees"])), True),
        ("Position cost", _money(dollars(position["entry_cost"])), True),
        ("Rule", str(position.get("rule") or "-"), True),
    ]
    if signal.get("confidence"):
        fields.append(("Confidence", f"{dollars(signal['confidence']):.0%}", True))
    return _embed(f"Trade placed ({environment})", _BLUE, fields, f"Order {fill['order_id']}")


def result_embed(position: dict, environment: str, today_pnl: Decimal | None = None) -> dict:
    pnl = dollars(position["net_pnl"])
    won = pnl > 0
    outcome = "WIN" if won else "LOSS" if pnl < 0 else "BREAK-EVEN"
    closed_by = position.get("closed_by") or "-"
    fields = [
        ("Market", position["ticker"], False),
        ("Side", position["side"].upper(), True),
        ("Result", str(position.get("result") or "-").upper(), True),
        ("Net P&L", _money(pnl), True),
        ("Total cost", _money(dollars(position["entry_cost"])), True),
        ("Payout", _money(dollars(position["exit_credit"])), True),
        ("Closed by", closed_by if closed_by == "settled" else f"Exit: {closed_by}", True),
    ]
    if today_pnl is not None:
        fields.append(("Today's P&L (UTC)", _money(today_pnl), True))
    color = _GREEN if won else _RED if pnl < 0 else _GREY
    return _embed(f"{outcome} ({environment})", color, fields, str(position.get("rule") or ""))


def _embed(title: str, color: int, fields: list[tuple[str, str, bool]], footer: str) -> dict:
    embed = {
        "title": title, "color": color,
        "fields": [{"name": n, "value": v[:1024] or "-", "inline": i} for n, v, i in fields],
    }
    if footer:
        embed["footer"] = {"text": footer}
    return embed


class DiscordNotifier:
    """Fire-and-forget posts; a Discord outage never blocks or breaks trading."""

    def __init__(self, trade_url: str = "", settlement_url: str = ""):
        self.trade_url = valid_webhook(trade_url)
        self.settlement_url = valid_webhook(settlement_url)
        self._tasks: set[asyncio.Task] = set()

    def trade(self, embed: dict) -> None:
        self._send(self.trade_url, embed)

    def settlement(self, embed: dict) -> None:
        self._send(self.settlement_url, embed)

    def _send(self, url: str, embed: dict) -> None:
        if not url:
            return
        try:
            task = asyncio.get_running_loop().create_task(self._post(url, embed))
        except RuntimeError:
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _post(self, url: str, embed: dict) -> None:
        payload = {"username": "Kalshi Bot", "embeds": [embed], "allowed_mentions": {"parse": []}}
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SEC) as client:
                response = await client.post(url, json=payload)
                if response.status_code == 429:
                    wait = float(response.headers.get("retry-after", "1"))
                    if wait <= _MAX_RETRY_WAIT_SEC:
                        await asyncio.sleep(wait)
                        response = await client.post(url, json=payload)
                if response.status_code >= 400:
                    logger.warning("Discord notification rejected (HTTP %s)", response.status_code)
        except Exception as exc:
            # Exception text can embed the webhook URL, so log only the type.
            logger.warning("Discord notification failed (%s)", type(exc).__name__)
