import copy
import io
import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

import bot

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 10, 28)
TOKEN = "123:" + "a" * 40


def raw_config():
    raw = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    raw["example"] = False
    return raw


def raw_prices():
    return json.loads((ROOT / "subscription-prices.json").read_text(encoding="utf-8"))


def validated_config(raw):
    return bot.validate_config(raw, bot.validate_prices(raw_prices()))


def rates(day):
    return Decimal("42"), Decimal("10.5")


class FakeTelegram:
    def __init__(self, pinned_message_id=None):
        self.calls = []
        self.pinned_message_id = pinned_message_id

    def call(self, method, **payload):
        self.calls.append((method, payload))
        if method == "getChat":
            if self.pinned_message_id is None:
                return {}
            return {"pinned_message": {"message_id": self.pinned_message_id}}
        return {"message_id": 7}


class CalculationsTests(unittest.TestCase):
    def setUp(self):
        self.config = validated_config(raw_config())

    def test_individual_music_and_icloud_shares(self):
        text = bot.render(self.config, DAY, rates(DAY), plain=True)
        self.assertEqual(text.count("☁️ + 🎵 — 161 грн / 15 zł"), 1)
        self.assertIn("🎵 Music — 56 грн / 5 zł", text)
        self.assertNotIn("Max", text)
        self.assertNotIn("$", text)

    def test_cross_rate_is_not_rounded_before_conversion(self):
        rate = Decimal("3.375"), Decimal("1")
        text = bot.render(self.config, DAY, rate, plain=True)
        self.assertIn("🎵 Music — 5 грн / 5 zł", text)
        self.assertEqual(bot.rounded(Fraction("1.49")), "1")
        self.assertEqual(bot.rounded(Fraction("1.50")), "2")
        self.assertEqual(bot.rounded(Fraction("1.99")), "2")

    def test_names_are_not_shown_and_note_is_escaped(self):
        raw = raw_config()
        raw["participants"][0]["name"] = "Anna <b>& Olya"
        raw["participants"][0]["telegram_id"] = 1234
        raw["payment_note"] = "<script>"
        text = bot.render(validated_config(raw), DAY, rates(DAY))
        self.assertNotIn("Anna", text)
        self.assertNotIn("tg://user?id=1234", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertNotIn("<script>", text)

    def test_identical_prices_are_grouped_and_icloud_only_is_shown(self):
        raw = raw_config()
        raw["participants"][1].update(music_usd="2.00", icloud_usd="1.00")
        raw["participants"][2].update(music_usd="0.00", icloud_usd="1.00")
        text = bot.render(validated_config(raw), DAY, rates(DAY), plain=True)
        self.assertEqual(text.count("☁️ + 🎵 — 161 грн / 15 zł"), 1)
        self.assertIn("☁️ iCloud — 105 грн / 10 zł", text)

    def test_subscription_prices_and_user_counts_are_validated(self):
        for service, key, value in [("music", "total_usd", "0.00"),
                                    ("icloud", "users", 0), ("music", "users", True)]:
            raw = raw_prices()
            raw[service][key] = value
            with self.subTest(service=service, key=key), self.assertRaises(bot.BotError):
                bot.validate_prices(raw)
        raw = raw_prices()
        raw["icloud"]["users"] = 1
        with self.assertRaises(bot.BotError):
            bot.validate_config(raw_config(), bot.validate_prices(raw))

    def test_invalid_shares_are_rejected(self):
        for value in ["NaN", "sNaN", "Infinity", "-1", "1.005", 2.5, None, "10001"]:
            with self.subTest(value=value), self.assertRaises(bot.BotError):
                bot.money(value)

    def test_invalid_configuration_is_rejected(self):
        for key, value in [("example", "false"), ("timezone", "No/SuchZone"),
                           ("participants", []), ("participants", [None]), ("payment_note", None)]:
            with self.subTest(key=key), self.assertRaises(bot.BotError):
                validated_config(dict(raw_config(), **{key: value}))
        raw = raw_config()
        raw["participants"][1]["id"] = "anna"
        with self.assertRaises(bot.BotError):
            validated_config(raw)

    def test_empty_share_and_duplicate_telegram_ids_are_rejected(self):
        raw = raw_config()
        raw["participants"][0].update(music_usd="0.00", icloud_usd="0.00")
        with self.assertRaises(bot.BotError):
            validated_config(raw)
        raw = raw_config()
        raw["participants"][0]["telegram_id"] = 123
        raw["participants"][1]["telegram_id"] = 123
        with self.assertRaises(bot.BotError):
            validated_config(raw)

    def test_maximum_message_fits_telegram_limit_with_emoji(self):
        raw = raw_config()
        template = copy.deepcopy(raw["participants"][0])
        raw["participants"] = [dict(template, id=f"person_{n}", name="😀" * 60,
                                    music_usd="10000.00", icloud_usd="0.00") for n in range(6)]
        raw["payment_note"] = "😀" * 400
        text = bot.render(validated_config(raw), DAY, rates(DAY), plain=True)
        self.assertLess(len(text.encode("utf-16-le")) // 2, 4096)

    def test_demo_explicitly_labels_fake_rates(self):
        text = bot.render(self.config, DAY, rates(DAY), demo=True)
        self.assertIn("DO NOT PAY", text)
        self.assertIn("Example amounts and rates.", text)


class RatesTests(unittest.TestCase):
    def payload(self):
        return [{"cc": "USD", "rate": "42.1234", "exchangedate": "28.10.2026"},
                {"cc": "PLN", "rate": "10.5", "exchangedate": "28.10.2026"}]

    def test_valid_rates(self):
        self.assertEqual(bot.parse_rates(self.payload(), DAY), (Decimal("42.1234"), Decimal("10.5")))

    def test_missing_duplicate_stale_and_future_rates_are_rejected(self):
        cases = [[], {}, self.payload()[:1], self.payload() * 2]
        for stale in ["27.10.2026", "29.10.2026", None]:
            data = self.payload()
            data[0]["exchangedate"] = stale
            cases.append(data)
        for value in ["NaN", "Infinity", "0", "-1", None]:
            data = self.payload()
            data[0]["rate"] = value
            cases.append(data)
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(bot.BotError):
                bot.parse_rates(payload, DAY)

    def test_http_query_requests_exact_date(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, *args): return json.dumps(RatesTests().payload()).encode()
        with patch("bot.urllib.request.urlopen", return_value=Response()) as mocked:
            bot.fetch_rates(DAY)
        self.assertIn("date=20261028", mocked.call_args.args[0].full_url)

    def test_no_fallback_on_network_failure(self):
        with patch("bot.urllib.request.urlopen", side_effect=TimeoutError), self.assertRaises(bot.BotError):
            bot.fetch_rates(DAY)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.config = validated_config(raw_config())
        self.api = FakeTelegram()

    def test_sends_one_message_without_payment_buttons(self):
        result = bot.send_notification(self.config, self.api, -500, enabled=True, rates_provider=rates, today=DAY)
        self.assertEqual(result, 7)
        method, payload = self.api.calls[0]
        self.assertEqual(method, "sendMessage")
        self.assertEqual(payload["chat_id"], -500)
        self.assertNotIn("reply_markup", payload)
        self.assertNotIn("confirmed", payload["text"])

    def test_sent_message_is_pinned_with_notification(self):
        bot.send_notification(self.config, self.api, -500, enabled=True, rates_provider=rates, today=DAY)
        methods = [call[0] for call in self.api.calls]
        self.assertEqual(methods, ["sendMessage", "getChat", "pinChatMessage"])
        pin_payload = self.api.calls[2][1]
        self.assertEqual(pin_payload["message_id"], 7)
        self.assertFalse(pin_payload["disable_notification"])

    def test_previously_pinned_reminder_is_unpinned_first(self):
        api = FakeTelegram(pinned_message_id=3)
        bot.send_notification(self.config, api, -500, enabled=True, rates_provider=rates, today=DAY)
        methods = [call[0] for call in api.calls]
        self.assertEqual(methods, ["sendMessage", "getChat", "unpinChatMessage", "pinChatMessage"])
        self.assertEqual(api.calls[2][1]["message_id"], 3)

    def test_pin_failure_gives_a_clear_error_after_sending(self):
        class NoPinRights(FakeTelegram):
            def call(self, method, **payload):
                if method == "pinChatMessage":
                    raise bot.BotError("HTTP 400")
                return super().call(method, **payload)
        with self.assertRaises(bot.BotError) as caught:
            bot.send_notification(self.config, NoPinRights(), -500, enabled=True, rates_provider=rates, today=DAY)
        self.assertIn("sent but could not be pinned", str(caught.exception))
        self.assertIn("admin", str(caught.exception))

    def test_current_rate_is_requested_again_for_every_notice(self):
        days = []
        def provider(day):
            days.append(day)
            return (Decimal("42") if len(days) == 1 else Decimal("44")), Decimal("10.5")
        bot.send_notification(self.config, self.api, -500, enabled=True, rates_provider=provider, today=DAY)
        tomorrow = date(2026, 10, 29)
        bot.send_notification(self.config, self.api, -500, enabled=True, rates_provider=provider, today=tomorrow)
        self.assertEqual(days, [DAY, tomorrow])
        sent = [payload["text"] for method, payload in self.api.calls if method == "sendMessage"]
        self.assertIn("161 грн", sent[0])
        self.assertIn("169 грн", sent[1])

    def test_disabled_or_example_config_blocks_real_send(self):
        for enabled, example in [(False, False), (True, True), (False, True)]:
            self.config["example"] = example
            with self.subTest(enabled=enabled, example=example), self.assertRaises(bot.BotError):
                bot.send_notification(self.config, self.api, -500, enabled=enabled, rates_provider=rates, today=DAY)
        self.assertEqual(self.api.calls, [])

    def test_test_message_does_not_require_production_enabled(self):
        self.config["example"] = True
        bot.send_notification(self.config, self.api, -700, test=True, rates_provider=rates, today=DAY)
        self.assertIn("DO NOT PAY", self.api.calls[0][1]["text"])

    def test_rate_failure_prevents_sending(self):
        def broken(day): raise bot.BotError("No rate")
        with self.assertRaises(bot.BotError):
            bot.send_notification(self.config, self.api, -500, enabled=True, rates_provider=broken, today=DAY)
        self.assertEqual(self.api.calls, [])

    def test_send_creates_no_payment_storage(self):
        with tempfile.TemporaryDirectory() as temp:
            old = Path.cwd()
            try:
                os.chdir(temp)
                bot.send_notification(self.config, self.api, -500, enabled=True, rates_provider=rates, today=DAY)
                self.assertEqual(list(Path(temp).iterdir()), [])
            finally:
                os.chdir(old)

    def test_timeout_does_not_auto_retry_or_print_token(self):
        api = bot.Telegram(TOKEN)
        with patch("bot.urllib.request.urlopen", side_effect=TimeoutError(TOKEN)) as mocked:
            with self.assertRaises(bot.BotError) as caught:
                api.call("sendMessage", chat_id=-500, text="test")
        self.assertEqual(mocked.call_count, 1)
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertIn("check", str(caught.exception))

    def test_only_numeric_group_ids_are_accepted(self):
        self.assertEqual(bot.parse_chat_id("-1001234567890"), -1001234567890)
        for value in ["123", "0", "-0", "@group", "-1; echo x", ""]:
            with self.subTest(value=value), self.assertRaises(bot.BotError):
                bot.parse_chat_id(value)

    def test_discovery_returns_only_groups_with_setup_command(self):
        class DiscoveryAPI:
            def call(self, method, **payload):
                if method == "getMe": return {"username": "family_bot"}
                if method == "getWebhookInfo": return {"url": ""}
                def msg(text, cid, kind="group"):
                    return {"message": {"text": text, "chat": {"id": cid, "type": kind}}}
                return [msg("/setup@family_bot", -500), msg("hello", -600),
                        msg("/setup@another_bot", -700), msg("/setup", 123, "private"),
                        msg("/setup@family_bot", -500)]
        self.assertEqual(bot.discover_chats(DiscoveryAPI()), [(-500, "group")])


class CliTests(unittest.TestCase):
    def test_invalid_secret_config_does_not_expose_its_contents(self):
        secret = '{"private_name": "secret-value" INVALID}'
        with patch.dict(os.environ, {"BILLING_CONFIG_JSON": secret}):
            with self.assertRaises(bot.BotError) as caught:
                bot.load_config(from_env=True)
        self.assertNotIn("secret-value", str(caught.exception))

    def test_reads_valid_config_from_secret(self):
        with patch.dict(os.environ, {"BILLING_CONFIG_JSON": json.dumps(raw_config())}):
            config = bot.load_config(from_env=True)
        self.assertEqual(config["participants"][0]["music_usd"], Fraction(4, 3))

    def test_preview_does_not_initialize_telegram(self):
        output = io.StringIO()
        args = ["bot.py", "preview", "--config", str(ROOT / "config.example.json")]
        with patch("sys.argv", args), patch("bot.fetch_rates", return_value=rates(DAY)), \
             patch("bot.Telegram") as api, redirect_stdout(output):
            self.assertEqual(bot.main(), 0)
        api.assert_not_called()
        self.assertIn("not sent", output.getvalue())

    def test_test_cli_uses_only_test_group(self):
        args = ["bot.py", "send", "--test", "--from-env"]
        values = {"BILLING_CONFIG_JSON": json.dumps(raw_config()), "TELEGRAM_BOT_TOKEN": TOKEN,
                  "TELEGRAM_CHAT_ID": "-500", "TELEGRAM_TEST_CHAT_ID": "-700"}
        with patch.dict(os.environ, values), patch("sys.argv", args), \
             patch("bot.Telegram", return_value=FakeTelegram()), patch("bot.send_notification") as send, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(bot.main(), 0)
        self.assertEqual(send.call_args.args[2], -700)
        self.assertTrue(send.call_args.kwargs["test"])

    def test_invalid_cli_configuration_exits_with_clear_error(self):
        with patch("sys.argv", ["bot.py", "check", "--config", "missing-config.json"]), redirect_stderr(io.StringIO()):
            self.assertEqual(bot.main(), 1)


if __name__ == "__main__":
    unittest.main()
