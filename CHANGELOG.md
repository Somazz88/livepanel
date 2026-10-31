# Changelog

## Unreleased

- The exit confirmation and error messages are drawn in the page. The browser's own dialogs were cut off in the
  small window (no OK button without maximizing); now the leg list scrolls inside the box and the buttons stay visible.
  The exit page's buttons stay at the bottom while the list scrolls.
- A login on the GUI OpenD can't be unlocked from the panel (moomoo refuses it there). The panel now sees which
  program serves the port: for the GUI OpenD it shows no 🔒 and no password box, only "use the Unlock button at the
  top right of the OpenD window". If it can't tell, the first refusal does the same.

## 0.4.2 — 2026-10-21

- First run is easier: the settings page starts in the computer's language (中文 / English) and time zone, and lists
  the four steps before a moomoo connection works (install OpenD, the API questionnaire, log in, test), with links.
- The settings page can turn the exit buttons on **as a rehearsal** (real quotes, nothing is sent) and off again.
  It always writes `dry_run = true`; live orders still need `dry_run = false` typed into the file, and while orders
  are live the switch is hidden and ignored.
- Taking a login out of the config also drops its join deposit (it kept being subtracted from week / month).
- Test connection and save errors are shown in plain words, including "orders are live: can't change logins here".

## 0.4.1 — 2026-10-19

- The panel starts each login's OpenD when it isn't running (`opend = "…OpenD.exe"`), minimized, and again within
  five minutes if it gets closed. Only an OpenD program is accepted there.
- Windows notification when an exit job ends: all closed, or what is still held.
- Broker errors in plain words (not unlocked, no permission, price not accepted, OpenD closed …).
- "Update available" link when GitHub has a newer full release (`check_updates`).
- Read-only phone view (`[phone]`, off by default): key in the link, nothing on it can act.
- Readability pass (reviewed as a stock-only trader, an options trader, a US trader and a first-time user):
  - A login that drops out keeps its last-seen positions on screen, greyed and marked *offline*, out of every total
    and never selectable; the totals name the missing login. Before, its rows simply vanished — which reads as
    "closed".
  - *Open P&L* column in dollars on every row, the % beside it; each leg ends with its dollar P&L. A symbol held
    as options and stock shows both parts (`options +1,000 · stock +760`).
  - Option legs within 3 days of expiry are flagged on the leg and on the symbol row (visible when collapsed).
  - A rehearsal's results say *simulated fill — nothing was sent*, not ✅ filled.
  - English uses long / short and sell / buy to close; Chinese keeps 买方 / 卖方, short stock is 卖空.
    *Close selected* / 平仓所选; the confirm warns that spread legs are separate orders.
  - 🔒 until every login is unlocked; the sparkline no longer calls an earlier close "now".
  - Fixed: a % was rounded twice (3.46 → 3.5 → "4%").
- Exit works like the original exit window: the main view only shows; 🧹 *Exit* opens a page to pick legs — options
  and stock in separate sections, a symbol ticked once across every login, live bid / ask, spread and first price
  per leg, the total to be received / paid — then the confirm, then the job's progress on the same page.
  All buttons one grey; ticked rows lit.
- The ladder is a setting: `start_frac` (first price, % of the spread from the bid / ask, default 70%) and
  `step_sec` (default 10 s), editable on the settings page.
- Fixed: in a rehearsal with two logins, both simulators numbered orders `sim-1, sim-2 …`, so one leg could read
  another leg's fills ("filled 20" on a 10-lot). Order states are now looked up by account and order id, and
  simulated ids are unique.

## 0.4.0 — 2026-10-19

- Several moomoo logins in one panel (one OpenD each, `[[moomoo.logins]]`), grouped per symbol across logins;
  one exit sends each leg to the login that holds it. A login added later is booked as a deposit (`since`).
- Settings page (⚙): language, time zone, refresh, brokers, logins and ports, *Test connection*, *Save & restart*,
  desktop icon. `[trading]` stays file-only. First run creates the config and opens the page.
- `LivePanel.exe` (PyInstaller, `scripts/build_exe.py`): no Python needed. App icon, tray icon (open, start with
  Windows, quit), second launch brings the existing window back instead of opening another.
- Exit: tick a whole symbol; the confirm lists every leg; sold options are bought back (moomoo's negative
  closable size is read as a count); after the job it re-reads positions and says *All closed* or what is left;
  🔓 unlock per login with its trade password (sent to OpenD as an MD5, not saved).
- Legs say *buyer* / *seller*; a seller's price is the price sold at; spreads show dollars, not a % of net cost.
- A closed OpenD no longer stops the whole panel from starting: that login shows "not reachable".

## 0.3.0

- Grouped positions, account chips, equity chart with daily history and deposit markers, recent fills,
  dense layout for the small window.

## 0.1.0

- First public version: moomoo and IBKR positions, week / month P&L, careful exit engine (dry run by default).
