# Apple family reminders

A small Telegram reminder for my family's Apple Music and iCloud shares. It converts each person's USD share to UAH and PLN using the National Bank of Ukraine's daily rates. GitHub Actions runs it on a schedule, so no server or database is needed.

The bot sends one message for everyone; it does not track payments. The participants in the example are fictional.

## Setup

1. Create a bot with [BotFather](https://t.me/BotFather) and add it to your main and test groups. Make it an admin in both groups with the "Pin messages" permission; it does not need any other admin right or Privacy Mode disabled.
2. Put this project in a private GitHub repository with `main` as the default branch. Keep `.github/workflows` at the repository root.
3. Under **Settings → Secrets and variables → Actions → Secrets**, add `TELEGRAM_BOT_TOKEN` from BotFather.
4. Send `/setup@your_bot_username` in each group. Within a day, run **Actions → Payment reminder → Run workflow → chats** on `main`. The log shows group IDs, not message contents. To distinguish them, run `chats` after adding the first group, then repeat for the second.
5. Add the group IDs as `TELEGRAM_CHAT_ID` and `TELEGRAM_TEST_CHAT_ID` secrets.
6. Copy [config.example.json](config.example.json), replace the fictional participants, and store the complete JSON in the `BILLING_CONFIG_JSON` secret. Set `"example": false` before production sending. Never commit your token or personal config.

Set the total USD price and total number of users for each subscription in [subscription-prices.json](subscription-prices.json). The total user count includes users who do not receive a reminder. Each total is split equally among its users.

For each participant in the secret config, `music_usd` and `icloud_usd` indicate membership: use `"0.00"` for a service they don't use and `"1.00"` for a service they do use. Existing positive values also indicate membership; their magnitude is not used in the calculation. Include only people who need reminders; the number using a service cannot exceed its configured user count. Names and `telegram_id` remain in the configuration but are not shown or mentioned in the message. `payment_note` is optional.

The message lists each subscription combination once, without names. Each sent reminder is pinned with a notification, and the previous reminder is unpinned first. If the bot lacks the "Pin messages" permission, the reminder is still sent, but the run fails with a clear error.

To discover chat IDs, stop any other handler using the same bot token. If a webhook is configured, disable the existing handler or use a new bot; this script does not remove webhooks.

## Test and enable

In **Actions → Payment reminder → Run workflow**, run these actions on `main`:

- `demo`: offline example with fictional rates; logs only.
- `preview`: your amounts at today's NBU rates; logs only.
- `test`: sends a clearly marked test to the test group.

Check the names and both currencies. Test messages can be sent while production sending is disabled.

The schedule in [.github/workflows/reminders.yml](.github/workflows/reminders.yml) is 09:00 Warsaw time on the 28th of each month. Change the cron and IANA time zone as needed; also set `timezone` in the config. Choose days 1–28 for a monthly reminder.

After testing, create an Actions **variable** named `NOTIFICATIONS_ENABLED` with value `true`. Scheduled runs and the manual `send` action can then send to the main group. Set it to `false` to pause. Both `"example": false` and `NOTIFICATIONS_ENABLED=true` are required for production sending. Tests run before each scheduled or manual action; a failure stops that run.

GitHub schedules can be delayed or missed, and scheduled workflows may be disabled in inactive public repositories. GitHub Free includes limited Actions minutes for private repositories; check your account's usage and [current limits](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

## Rates and retries

```text
Music share = Music total USD / Music users
iCloud share = iCloud total USD / iCloud users
UAH = (applicable Music share + iCloud share) × UAH per USD
PLN = (applicable Music share + iCloud share) × UAH per USD / UAH per PLN
```

The USD/PLN cross-rate uses NBU reference rates, not bank fees or buy/sell rates. Final UAH and PLN amounts are rounded to the nearest whole unit, with halves rounded up. Pay either the UAH or PLN amount, not both.

Each notification fetches rates for the current date in the configured time zone. If the rates are unavailable or have the wrong date, sending stops. If Telegram has a network error, check the group before retrying: the message may already have arrived. There is no automatic retry or persistent send history, so a manual rerun may send a duplicate. Messages remain in Telegram and GitHub keeps workflow logs.

## Local use

Python 3.10+ is required. Windows may also need `py -m pip install tzdata`; use `py` in place of `python3`.

```bash
python3 bot.py demo
python3 -m unittest discover -s tests -v
```

For local settings, copy `config.example.json` to `config.json` and `.env.example` to `.env`. Both personal files are ignored by git.

```bash
python3 bot.py check
python3 bot.py preview
python3 bot.py chats
python3 bot.py send --test
```

For a local production send, set `"example": false` and `NOTIFICATIONS_ENABLED=true`, then run `python3 bot.py send`.

## References

- [GitHub Actions schedule syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onschedule)
- [Telegram bots](https://core.telegram.org/bots/features) and [Bot API](https://core.telegram.org/bots/api)
- [NBU exchange-rate API](https://bank.gov.ua/admin_uploads/article/Instr_API_KURS_VAL_Full_eng.pdf)
