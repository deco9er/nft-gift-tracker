# NFT Gift Tracker

[Русский](README.md) · **English**

A Telegram bot for monitoring collectible gift listings on Telegram Market. A single process refreshes the catalog, checks Crypto Pay invoices, and manages access to a Telegram forum.

## Features

- `/start` and `/live` show recent listings and recheck price and availability. `/gifts` opens the full catalog and `/changes` opens the change history.
- Filter by collection, listing price, seller rating, or collection floor. The floor bands are up to 10 TON, over 10 through 50 TON, and over 50 through 1,500 TON.
- `/group` sets up the forum. The bot posts price changes and new listings in the feed and in pinned price-band topics. The first eligible member to claim a listing gets the seller and gift links in a private message. A claim reserves the listing in the tracker.
- `/admin` lets administrators check access, ban or unban a user, and manage the whitelist. Users can activate a 15-minute trial once a day, based on Moscow time, or purchase access through Crypto Pay.

## Configuration

Download the ZIP from [Releases](https://github.com/deco9er/nft-gift-tracker/releases) or clone the repository.

```bash
git clone https://github.com/deco9er/nft-gift-tracker.git
cd nft-gift-tracker
```

Use Python 3.10–3.14. Copy `.env.example` to `.env` for a fresh installation and fill in these settings.

| Setting | Where to get it |
| --- | --- |
| `BOT_TOKEN` | Your bot token from [BotFather](https://t.me/BotFather). |
| `API_ID`, `API_HASH` | Your Telegram API application at [my.telegram.org](https://my.telegram.org). |
| `ADMIN_ID` | One or more numeric Telegram user IDs, separated by commas. |
| `CRYPTO_PAY_API_TOKEN` | Your Crypto Pay app token. For testing, use a token from @CryptoTestnetBot and set `CRYPTO_PAY_TESTNET=1`. Mainnet is the default, and test payments use a separate database. |

Run `parser/catalog_bot.py` as the application entry point. A lock prevents two instances in the same project directory. When moving to another host, stop the previous instance first.

For a migration, copy `.env`, `market.session`, `catalog.sqlite3`, and the relevant `access.sqlite3` or `access-testnet.sqlite3` after stopping the bot. Copy any remaining `-wal` files alongside their databases. Recreate the virtual environment on the new host and restrict access to these files to the account running the bot.

## Windows

Open Command Prompt in the project directory.

```cmd
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
if not exist .env copy .env.example .env
notepad .env
```

If `market.session` is missing, sign in to the Telegram user account once.

```cmd
.venv\Scripts\python.exe parser\create_session.py
```

Start the bot.

```cmd
.venv\Scripts\python.exe parser\catalog_bot.py --log-file tracker.log
```

For an always-on Windows host, create a task in Task Scheduler that starts at system startup and runs whether the user is logged on or not. Set the program to the absolute path of `.venv\Scripts\python.exe`, arguments to `parser\catalog_bot.py --log-file tracker.log`, and **Start in** to the project directory. Under task settings, choose **Do not start a new instance** and clear the run-time limit. Use an account with access to the project and the network. [Windows Task Scheduler documentation](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtaskaction)

## Linux

On Debian or Ubuntu, install Python and virtual environment support.

```bash
sudo apt update
sudo apt install python3 python3-venv
```

From the project directory:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
test -f .env || cp .env.example .env
chmod 600 .env
nano .env
```

If `market.session` is missing, sign in to the Telegram user account once.

```bash
.venv/bin/python parser/create_session.py
chmod 600 market.session
```

Start the bot manually.

```bash
.venv/bin/python parser/catalog_bot.py
```

For hosting with systemd, save the unit below as `/etc/systemd/system/nft-gift-tracker.service`. Replace `nfttracker` and `/opt/nft-gift-tracker` with your service account and project directory. The service account needs write access to the project directory and its databases.

```ini
[Unit]
Description=NFT Gift Tracker
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=nfttracker
WorkingDirectory=/opt/nft-gift-tracker
ExecStart=/opt/nft-gift-tracker/.venv/bin/python /opt/nft-gift-tracker/parser/catalog_bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Stop the manually started instance, then enable the service.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nft-gift-tracker
sudo systemctl status nft-gift-tracker
sudo journalctl -u nft-gift-tracker -f
```

Use `sudo systemctl restart nft-gift-tracker` after changing configuration. Payment polling requires outbound access to Telegram and Crypto Pay.

## First run

Open the bot in Telegram and send `/start`. As an admin, open `/live` to see the catalog and scan progress. Run `/group` to set up the forum and its topics. Send `/start` from a regular account to check the onboarding and access menu.

## Payments and access

Plans are **1 day for 1.5 TON**, **3 days for 4 TON**, and **7 days for 10 TON**. The bot sends a **Pay** button that opens the invoice in Crypto Bot. When Crypto Pay confirms payment, the bot credits access and sends a confirmation. It normally checks invoices every 15 seconds. [Crypto Pay API documentation](https://help.cr.bot/en/articles/10279948-crypto-pay-api)

Admins, whitelisted users, active subscribers, and users in an active trial can access the catalog and request to join the forum. Once access expires, the bot removes the user from the group. They can request to rejoin when access becomes active again.

To compare up to 100 recent invoices against Crypto Pay while the bot runs:

```cmd
.venv\Scripts\python.exe parser\catalog_bot.py --check-payments
```

```bash
.venv/bin/python parser/catalog_bot.py --check-payments
```

For an end-to-end test, create an invoice from a regular account, pay it in the selected network, and wait for the activation message. The diagnostic should report an increase in both the provider's paid count and the locally credited count. Use testnet to do this without a mainnet payment.

## Market data

The catalog and notifications show listings with a confirmed seller `@username`. When the marketplace supplies only a name resembling a username, the tracker compares the exact gift and owner ID against the profile's public gifts. Prices from other listings contribute to the collection floor. Seller ratings load in the background.

Live polling scans the first pages of each collection, ordered by the latest price change, with a pause controlled by `TELEGRAM_LIVE_REQUEST_SECONDS`. By default, the next full scan starts 10 minutes after the previous one finishes. The tracker follows Telegram's FloodWait pauses and saves progress in SQLite. A listing can change after a live check.

For the user session, `TELEGRAM_PROXY=socks5://user:password@host:port` configures a proxy. HTTP, SOCKS4, and SOCKS5 are supported. Set custom animated emoji IDs in `.env`. The bot falls back to regular emoji when required.

## Provenance

The tracker is based on a source snapshot from Moon's portfolio. See [PUBLICATION.md](PUBLICATION.md) for details.
