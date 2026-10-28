#!/usr/bin/env python3
"""Send subscription reminders with GitHub Actions."""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CENT = Decimal("0.01")


class BotError(ValueError):
    """Error safe to log without exposing tokens or configuration."""


def setting(name: str) -> str:
    if name in os.environ:
        return os.environ[name].strip()
    path = Path(".env")
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key.strip() == name:
                return value.strip().strip('"').strip("'")
    return ""


def money(value) -> Decimal:
    try:
        if not isinstance(value, str):
            raise InvalidOperation
        amount = Decimal(value)
        if not amount.is_finite() or not 0 <= amount <= 10000 or amount != amount.quantize(CENT):
            raise InvalidOperation
        return amount.quantize(CENT)
    except (InvalidOperation, ValueError):
        raise BotError('Share must be a string from "0.00" to "10000.00" with at most two decimal places') from None


def validate_prices(raw: dict) -> dict[str, tuple[Decimal, int]]:
    if not isinstance(raw, dict):
        raise BotError("Subscription prices must be a JSON object")
    prices = {}
    for service in ("music", "icloud"):
        entry = raw.get(service)
        if not isinstance(entry, dict):
            raise BotError(f"Missing {service} subscription price")
        total = money(entry.get("total_usd"))
        users = entry.get("users")
        if total <= 0 or type(users) is not int or not 1 <= users <= 6:
            raise BotError(f"Invalid {service} subscription price or user count")
        prices[service] = (total, users)
    return prices


def validate_config(raw: dict, prices: dict[str, tuple[Decimal, int]]) -> dict:
    if not isinstance(raw, dict):
        raise BotError("Config must be a JSON object")
    if type(raw.get("example", True)) is not bool:
        raise BotError("example must be true or false")
    zone = raw.get("timezone", "Europe/Warsaw")
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise BotError("Unknown time zone; install tzdata on Windows") from None
    note = raw.get("payment_note", "")
    if not isinstance(note, str) or len(note) > 400:
        raise BotError("payment_note must be a string of at most 400 characters")
    people = raw.get("participants", [])
    if not isinstance(people, list) or not 1 <= len(people) <= 6:
        raise BotError("Specify 1 to 6 participants")
    clean, seen_ids, seen_telegram = [], set(), set()
    for person in people:
        if not isinstance(person, dict):
            raise BotError("Each participant must be a JSON object")
        pid, name = person.get("id", ""), person.get("name", "")
        if not isinstance(pid, str) or not re.fullmatch(r"[a-z0-9_]{1,20}", pid) or pid in seen_ids:
            raise BotError("Participant id must be unique, up to 20 lowercase Latin letters, digits or underscores")
        if not isinstance(name, str) or not name.strip() or len(name) > 60 or any(c in name for c in "\r\n"):
            raise BotError("Name must be one line of 1 to 60 characters")
        tid = person.get("telegram_id")
        if tid is not None:
            if type(tid) is not int or tid <= 0 or tid in seen_telegram:
                raise BotError("telegram_id must be a unique positive integer or null")
            seen_telegram.add(tid)
        has_music = money(person.get("music_usd")) > 0
        has_cloud = money(person.get("icloud_usd")) > 0
        if not has_music and not has_cloud:
            raise BotError("Participant must use at least one subscription")
        music = Fraction(prices["music"][0]) / prices["music"][1] if has_music else Fraction(0)
        cloud = Fraction(prices["icloud"][0]) / prices["icloud"][1] if has_cloud else Fraction(0)
        clean.append({"id": pid, "name": name.strip(), "telegram_id": tid,
                      "music_usd": music, "icloud_usd": cloud})
        seen_ids.add(pid)
    for service, key in (("music", "music_usd"), ("icloud", "icloud_usd")):
        if sum(bool(person[key]) for person in clean) > prices[service][1]:
            raise BotError(f"More participants use {service} than its configured user count")
    return {"example": raw.get("example", True), "timezone": zone,
            "payment_note": note, "participants": clean}


def load_config(path: str = "config.json", *, from_env: bool = False) -> dict:
    try:
        if from_env:
            text = os.environ.get("BILLING_CONFIG_JSON", "")
            if not text.strip():
                raise BotError("Set the BILLING_CONFIG_JSON secret to your config")
        else:
            text = Path(path).read_text(encoding="utf-8")
        raw = json.loads(text)
    except (OSError, json.JSONDecodeError):
        raise BotError("Could not read JSON config. Check the file or BILLING_CONFIG_JSON secret") from None
    try:
        prices = json.loads(Path("subscription-prices.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise BotError("Could not read subscription-prices.json") from None
    return validate_config(raw, validate_prices(prices))


def parse_rates(payload, day: date) -> tuple[Decimal, Decimal]:
    if not isinstance(payload, list):
        raise BotError("Unexpected NBU response")
    rates = {}
    for row in payload:
        if not isinstance(row, dict) or row.get("cc") not in {"USD", "PLN"}:
            continue
        code = row["cc"]
        if code in rates:
            raise BotError("NBU returned a duplicate rate")
        try:
            effective = datetime.strptime(row["exchangedate"], "%d.%m.%Y").date()
            amount = Decimal(str(row["rate"]))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise BotError("NBU returned invalid data") from None
        if effective != day:
            raise BotError("NBU rate date is not today; sending stopped")
        if not amount.is_finite() or not 0 < amount < 1000000:
            raise BotError("Invalid NBU rate")
        rates[code] = amount
    if set(rates) != {"USD", "PLN"}:
        raise BotError("Today's USD or PLN rate is missing; sending stopped")
    return rates["USD"], rates["PLN"]


def fetch_rates(day: date) -> tuple[Decimal, Decimal]:
    url = f"https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?date={day:%Y%m%d}&json"
    request = urllib.request.Request(url, headers={"User-Agent": "AppleFamilyReminder/2.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read(2_000_000), parse_float=Decimal)
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        raise BotError("Could not fetch today's NBU rates. Sending stopped; try again later") from None
    return parse_rates(payload, day)


def rounded(amount: Fraction) -> str:
    return str((2 * amount.numerator + amount.denominator) // (2 * amount.denominator))


def render(config: dict, day: date, rates: tuple[Decimal, Decimal], *, test=False, demo=False, plain=False) -> str:
    usd_uah, pln_uah = rates
    esc = str if plain else lambda value: html.escape(str(value))
    lines = [f"💳 Apple subscriptions · {day:%m.%Y}"]
    if test or demo:
        lines.insert(0, "🧪 TEST — DO NOT PAY")
    if demo:
        lines.append("Example amounts and rates.")
    lines.append("")
    seen = set()
    for person in config["participants"]:
        music, cloud = person["music_usd"], person["icloud_usd"]
        total = music + cloud
        plan = "☁️ + 🎵" if cloud and music else "☁️ iCloud" if cloud else "🎵 Music"
        uah = rounded(total * Fraction(usd_uah))
        pln = rounded(total * Fraction(usd_uah) / Fraction(pln_uah))
        entry = (plan, uah, pln)
        if entry not in seen:
            lines.append(f"{plan} — {uah} грн / {pln} zł")
            seen.add(entry)
    lines.append("")
    lines.append("Pay in either UAH or PLN.")
    if config["payment_note"]:
        lines.append(esc(config["payment_note"]))
    return "\n".join(lines)


class Telegram:
    def __init__(self, token: str):
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
            raise BotError("Set TELEGRAM_BOT_TOKEN to your BotFather token")
        self._base = "https://api.telegram.org/bot" + token + "/"

    def call(self, method: str, **payload):
        request = urllib.request.Request(self._base + method, data=json.dumps(payload).encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise BotError(f"Telegram rejected the request: HTTP {exc.code}. Check bot permissions and chat ID") from None
        except (urllib.error.URLError, OSError, TimeoutError, ValueError):
            raise BotError("Telegram network error. The message may have been sent; check the group before retrying") from None
        if not isinstance(result, dict) or not result.get("ok") or "result" not in result:
            raise BotError("Invalid Telegram response. Check the group before retrying")
        return result["result"]


def parse_chat_id(value: str) -> int:
    if not re.fullmatch(r"-\d+", value) or int(value) == 0:
        raise BotError("TELEGRAM_CHAT_ID or TELEGRAM_TEST_CHAT_ID must be a negative group ID")
    return int(value)


def send_notification(config: dict, telegram, chat_id: int, *, test=False, enabled=False,
                      rates_provider=fetch_rates, today: date | None = None):
    if not test and (config["example"] or not enabled):
        raise BotError("Production sending is disabled: set example=false and NOTIFICATIONS_ENABLED=true")
    day = today or datetime.now(ZoneInfo(config["timezone"])).date()
    rates = rates_provider(day)
    result = telegram.call("sendMessage", chat_id=chat_id, text=render(config, day, rates, test=test),
                          parse_mode="HTML", link_preview_options={"is_disabled": True})
    if not isinstance(result, dict) or type(result.get("message_id")) is not int:
        raise BotError("Telegram did not confirm delivery. Check the group before retrying")
    return result["message_id"]


def discover_chats(telegram) -> list[tuple[int, str]]:
    me = telegram.call("getMe")
    info = telegram.call("getWebhookInfo")
    if info.get("url"):
        raise BotError("A webhook is already configured. Use a new bot or disable the existing handler to find chat IDs")
    updates = telegram.call("getUpdates", timeout=0, limit=100, allowed_updates=["message"])
    commands = {"/setup", "/setup@" + me["username"].lower()}
    chats = set()
    for update in updates:
        message = update.get("message", {})
        words = message.get("text", "").split()
        chat = message.get("chat", {})
        if words and words[0].lower() in commands and chat.get("type") in {"group", "supergroup"}:
            if type(chat.get("id")) is int and chat["id"] < 0:
                chats.add((chat["id"], chat["type"]))
    return sorted(chats)


def main() -> int:
    parser = argparse.ArgumentParser(description="Music and iCloud payment reminders without a server")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Offline example without a token or network")
    demo.add_argument("--config", default="config.example.json")
    for name, help_text in [("check", "Validate config"), ("preview", "Calculate using today's rates without sending"),
                            ("send", "Send one message")]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--config", default="config.json")
        command.add_argument("--from-env", action="store_true", help="Read BILLING_CONFIG_JSON instead of a file")
        if name == "send":
            command.add_argument("--test", action="store_true", help="Send a test to TELEGRAM_TEST_CHAT_ID")
    commands.add_parser("chats", help="Find group IDs after /setup@bot_username")
    args = parser.parse_args()
    try:
        if args.command == "chats":
            found = discover_chats(Telegram(setting("TELEGRAM_BOT_TOKEN")))
            if not found:
                raise BotError("No groups found. Add the bot, send /setup@bot_username in the group and retry within a day")
            for chat_id, kind in found:
                print(f"Group ID: {chat_id} ({kind})")
            return 0
        config = load_config(args.config, from_env=getattr(args, "from_env", False))
        if args.command == "check":
            print(f"Config valid. Participants: {len(config['participants'])}; example mode: {config['example']}")
        elif args.command == "demo":
            print(render(config, date(2026, 10, 28), (Decimal("42"), Decimal("10.5")), demo=True, plain=True))
        elif args.command == "preview":
            day = datetime.now(ZoneInfo(config["timezone"])).date()
            print("PREVIEW — not sent to Telegram\n")
            print(render(config, day, fetch_rates(day), plain=True))
        else:
            chat_id = parse_chat_id(setting("TELEGRAM_TEST_CHAT_ID" if args.test else "TELEGRAM_CHAT_ID"))
            send_notification(config, Telegram(setting("TELEGRAM_BOT_TOKEN")), chat_id, test=args.test,
                              enabled=setting("NOTIFICATIONS_ENABLED").lower() == "true")
            print("Test message sent." if args.test else "Reminder sent.")
    except BotError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
