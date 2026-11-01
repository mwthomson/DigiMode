# DigiMode

A system-tray control panel for amateur radio digital modes on Linux.

Choose an application and a mode: Pat (over ARDOP, VARA HF, VARA FM, packet or
telnet), JS8Call, WSJT-X or Fldigi. DigiMode starts everything that mode needs,
in order: flrig, rigctld, the modem, then the app. It stops programs that would
fight over the same sound card and tunes the rig through flrig. Every program
is a button that turns green while it runs, and clicking a green button stops
it.

## Requirements

- Python 3.11+ and PyQt5 (`sudo apt install python3-pyqt5`)
- flrig, plus the programs you plan to use: Pat, ardopcf, VARA (Wine or
  Bottles), Direwolf, JS8Call, WSJT-X, Fldigi
- A desktop with a system tray (on GNOME, install the AppIndicator extension)

## Install

```bash
git clone https://github.com/mwthomson/DigiMode.git
DigiMode/digimode.py --install              # add to the app menu
DigiMode/digimode.py --install --autostart  # ...and start at login
```

The first run creates `~/.config/digimode/modes.toml` from
`modes.default.toml`. Set your sound card, VARA path and the rig's
`data_mode` there, or use **Programs…** in the tray menu.

## License

Copyright © 2026 Marc Thomson

DigiMode is free software, licensed under the
**GNU General Public License v3.0** ([LICENSE](LICENSE)). In short:

- You may use, study, share and modify it.
- If you distribute DigiMode or any work based on it, you must release the
  complete source code under the same GPLv3 license, keep this copyright
  notice and the license, and state what you changed.
- It cannot be redistributed as closed-source or proprietary software, or
  relicensed under other terms.
- It comes with **no warranty**, to the extent permitted by law.

This summary is not legal advice. The [LICENSE](LICENSE) text is the binding
version.
