# Automated Trading Signal

A small service that watches Bitcoin's price and sends **🟢 BUY** or **🔴 SELL**
messages on Telegram whenever a moving-average crossover happens.

---

## What it does

Every few minutes it fetches the latest **closed** 15-minute candles, works out two moving
averages (a fast **EMA 9** and a slow **EMA 21**), and when the fast one crosses the slow
one it sends **one** alert to Telegram and it only records that it did so **after**
Telegram confirms the message actually arrived.

---

## Set it up

Most of that time is just signing up for two free accounts. The program itself runs immediately.

### Before you start
- **Python 3.9 or newer.** Check with `python3 --version` in a terminal.
  (No Python? Get it at [python.org/downloads](https://www.python.org/downloads/).)
- The **Telegram** app on your phone or desktop.

### Step 1 — Install
Open a terminal, move into this project's folder, then run:
```bash
python3 -m venv .venv                       # create a private space for the libraries
.venv/bin/pip install -r requirements.txt   # install what the program needs
```

### Step 2 — Get your three keys
| # | What you need | How to get it | 
|---|---|---|
| 1 | **Telegram bot token** | In Telegram, open a chat with **@BotFather**, send `/newbot`, follow the prompts, and copy the token it gives you. |
| 2 | **Telegram chat id** | Send your new bot *any* message. Then open this link in a browser (paste your token where shown): `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` and copy the number next to `"id"` inside the `"chat"` section. | 
| 3 | **taapi.io secret** | Sign up free at [taapi.io](https://taapi.io) and copy the "API Key" from your dashboard. |

### Step 3 — Save your keys
```bash
cp .env.example .env
```
Open the new `.env` file in any text editor and paste your three values in, then save.
This file stays on your machine and is never shared or uploaded.

### Step 4 — Check everything works
```bash
.venv/bin/python check_setup.py   # sends a test Telegram message AND prints the latest BTC candles
```
If you get the Telegram message and see prices in the terminal, you're ready.

### Step 5 — Run it
```bash
.venv/bin/python main.py                         # run ONCE (just sets a starting point, no alert)
.venv/bin/python main.py --loop --interval 300   # run continuously (how it runs on the server)
```
The very first run only records where the two averages are right now and sends **nothing** —
that's correct. Real alerts start on the next actual crossover.

---

## What each file does
| File | What it's for |
|---|---|
| `main.py` | The main heart: the loop, and the safety checks that keep it reliable. |
| `market_data.py` | Fetches the price candles, with a backup source if the main one fails. |
| `indicators.py` | The math behind the moving averages. |
| `notifier.py` | Sends the Telegram message and builds the alert text. |
| `state.py` | Remembers what it has already alerted, so it never repeats. |
| `config.py` | All the settings in one place. |
| `check_setup.py` | A one-off check that your Telegram + taapi keys work. |
| `test_reliability.py` | 21 tests that check the safety rules. |

## Run the tests
```bash
.venv/bin/python -m unittest test_reliability -v
```
These run offline in about 0.03 seconds and check the reliability rules directly.

---

## If something goes wrong
- **`No taapi key found … cannot start`** → your `TAAPI_SECRET` is empty in `.env`.
- **No Telegram message / `check_setup.py` fails** → the chat id is wrong, or you haven't
  messaged the bot yet (a bot can't text you until you text it first).
- **taapi `429` / "rate limited"** → the free tier allows about one request every 15 seconds;
  wait a few seconds and try again.

---

## A note on how it's built
- **Settings are separate from code** (`config.py` for settings, a private `.env` for secrets).
- It runs **unattended on a free Oracle Cloud server**, kept alive by systemd, so it restarts
  itself and survives reboots.

## More detail
- [`BUGS.md`](Trading-Signal-Automation/BUGS.md) — every bug found while building, and how they were caught.
- [`evidence`](Trading-Signal-Automation/evidence/run_log.txt) — proof of the live 3-day+ run
