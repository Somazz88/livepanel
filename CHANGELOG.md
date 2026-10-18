# Changelog

## 0.4.0 — unreleased

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
