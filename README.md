# Live Panel

A local, read-mostly positions panel for **moomoo / Futu** and **Interactive Brokers**, with a careful
one-click exit. Runs on your own machine, talks only to your broker's local gateway, never sees your password.

- Positions, today's P&L, this week / this month P&L (deposits stripped out), across several accounts and brokers.
- Freezes on closing marks after the bell instead of re-pulling quotes that can't change.
- Exit button (off by default): limit orders only — start at the bid + 70% of the spread, step one tick toward
  the bid every 10 s, never below it. Short positions are bought back the mirror way.

> **Status**
> - moomoo: read side verified against a real OpenD; exit engine verified against a simulated broker only.
> - IBKR: written against `ib_async` and tested against a fake gateway. **Not yet run against a real TWS / IB Gateway.**
> - Crypto exchanges: not supported yet (the data model has room for it).

## Connect your broker

### moomoo / Futu
1. Install and log in to **OpenD** (the GUI version is simplest).
2. First time only: complete the *API questionnaire and agreements* in your moomoo account.
3. `pip install moomoo-api`
4. In `config.toml`: `use = ["moomoo"]`. Default address `127.0.0.1:11111`.
5. Quotes: US options L1 is free if your account has assets or US positions; otherwise buy OPRA.
6. Live orders: the GUI OpenD has to be unlocked by hand (top-right of the OpenD window).

Accounts are discovered automatically. A universal account's futures sub-account is added to its equity.

### Interactive Brokers
1. Install TWS or IB Gateway and log in.
2. *Configure → API → Settings*: tick **Enable ActiveX and Socket Clients**.
3. Port: TWS 7496 live / 7497 paper, Gateway 4001 live / 4002 paper.
4. `pip install ib_async`
5. Real-time option quotes over the API need **OPRA Top of Book** in Client Portal (and API market data enabled).
6. Leave **Read-Only API** ticked unless you want the exit button to send orders.
7. With 2FA, the Sunday auto-restart asks you to re-authenticate on your phone; until you do, the panel can't connect.

The TWS API has no deposit history, so IBKR week/month P&L is labelled *excludes deposits*.

## Install and run

You need Python 3.11 or newer ([python.org](https://www.python.org/downloads/); on Windows tick
*Add python.exe to PATH*). Then, in a terminal (PowerShell on Windows):

```
pip install "livepanel[moomoo] @ https://github.com/Somazz88/livepanel/archive/refs/heads/main.zip"
livepanel --init           # creates ~/.livepanel/config.toml — open it and set your broker
livepanel --window         # starts the panel in its own small window
```

Use `[ibkr]` instead of `[moomoo]` for Interactive Brokers, or `[moomoo,ibkr]` for both.
Want to look first? `livepanel --demo --window` shows fake positions with nothing to log into.

**Desktop icon (Windows):** `livepanel --make-shortcut` puts a *Live Panel* icon on the desktop.
Double-click it after your broker gateway is logged in. Clicking it again while it runs just brings the window back.
Move and resize the window once; it reopens there next time.

The window is Chrome or Edge in app mode (no tabs, no address bar). Wide windows show every column;
narrow ones keep symbol, size, value and today's P&L. Without Chrome / Edge it opens as a normal browser tab.

From a source checkout instead: `python -m livepanel --window` in the repo folder.

Week / month numbers need history: the app records equity while the market is open and shows a period once
it has a close from before that period.

## Exit button — safety

- `trading.enabled = false` by default; with `dry_run = true` it uses real positions and quotes but only simulates fills.
- Orders start only from the page, after a confirm dialog. The server listens on 127.0.0.1 only and needs a
  per-run token, so another website open in your browser can't trigger it.
- Right before the first order it re-reads positions and uses `min(what you saw, closable now)`. If it can't re-read, it places nothing.
- Options only during 09:30–16:00 ET. Stocks in pre/post market only if `allow_extended_hours = true` (limit orders only).
- Non-US listings are shown but not exited (no session table, often no API quote permission).
- Stop = fetch fresh order states, cancel what's still working, record fills that happened during the cancel.

Rehearse the exit on your real positions and live quotes without sending anything (market hours):

```
python scripts/dry_run_exit.py --config config.toml
```

## Tests

```
python -m pytest -q
```

## Disclaimer

This software can send orders to your brokerage account. It is provided as is, without warranty
(see LICENSE). Test it in dry-run and on a paper account first. You are responsible for every order it sends.
Not affiliated with moomoo, Futu or Interactive Brokers.
