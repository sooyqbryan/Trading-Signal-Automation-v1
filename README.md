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

Most of the time is just signing up for two free accounts. The program itself runs in seconds.

### Before you start
- **Python 3.9 or newer.** No Python? Install it from [python.org/downloads](https://www.python.org/downloads/).
  On **Windows**, tick **"Add Python to PATH"** on the first install screen.
- The **Telegram** app (phone or desktop).
- **Open a terminal:** on **macOS** open the **Terminal** app; on **Windows** open **PowerShell**.
- **Go into the project folder:** type `cd ` (with a space), drag the project folder onto the
  terminal window, and press Enter.

### Step 1 — Install
```bash
# macOS / Linux:
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Windows:
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

### Step 2 — Get your three keys
| # | What you need | How to get it | 
|---|---|---|
| 1 | **Telegram bot token** | In Telegram, open a chat with **@BotFather**, send `/newbot`, follow the prompts, and copy the token it gives you. |
| 2 | **Telegram chat id** | Send your new bot *any* message. Then open this link in a browser (paste your token where shown): `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` and copy the number next to `"id"` inside the `"chat"` section. | 
| 3 | **taapi.io secret** | Sign up free at [taapi.io](https://taapi.io) and copy the "API Key" from your dashboard. |

### Step 3 — Save your keys
In the project folder, create a new file named exactly **`.env`** and put these three lines in it:
```
TELEGRAM_BOT_TOKEN=paste_your_bot_token_here
TELEGRAM_CHAT_ID=paste_your_chat_id_here
TAAPI_SECRET=paste_your_taapi_key_here
```
Replace each value with yours and save. This file stays on your machine — it is never shared or uploaded.
*(An example file, `.env.example`, is also included — you can copy it instead: `cp .env.example .env` on macOS, `copy .env.example .env` on Windows.)*

### Step 4 — Check everything works
```bash
# macOS / Linux:
.venv/bin/python check_setup.py

# Windows:
.venv\Scripts\python check_setup.py
```
If a test message appears in Telegram and prices print in the terminal, you're ready.

### Step 5 — Run it
```bash
# macOS / Linux:
.venv/bin/python main.py                          # run ONCE (sets a starting point, sends no alert)
.venv/bin/python main.py --loop --interval 300    # run continuously (how it runs on the server)

# Windows:
.venv\Scripts\python main.py
.venv\Scripts\python main.py --loop --interval 300
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
# macOS / Linux:
.venv/bin/python -m unittest test_reliability -v

# Windows:
.venv\Scripts\python -m unittest test_reliability -v
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
