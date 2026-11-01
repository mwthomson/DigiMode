#!/usr/bin/env python3
# DigiMode - system-tray control panel for amateur radio digital modes.
# Copyright (C) 2026 Marc Thomson
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version. It is distributed WITHOUT ANY WARRANTY; see the LICENSE
# file or <https://www.gnu.org/licenses/> for details.
import copy
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import tomllib
import xmlrpc.client
from pathlib import Path

HERE = Path(__file__).resolve().parent
XDG = lambda var, default: Path(os.environ.get(var) or Path.home() / default)
CONFIG = XDG("XDG_CONFIG_HOME", ".config") / "digimode" / "modes.toml"
LOGDIR = XDG("XDG_CACHE_HOME", ".cache") / "digimode"


def load():
    if not CONFIG.exists():
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(HERE / "modes.default.toml", CONFIG)
    return tomllib.loads(CONFIG.read_text())


def install(autostart):
    entry = ("[Desktop Entry]\nType=Application\nName=DigiMode\n"
             "Comment=Switch ham radio digital modes from the tray\n"
             f'Exec="{sys.executable}" "{HERE / "digimode.py"}"\n'
             f"Icon={HERE / 'icon.svg'}\nCategories=HamRadio;Utility;\n")
    dirs = [XDG("XDG_DATA_HOME", ".local/share") / "applications"]
    if autostart:
        dirs.append(XDG("XDG_CONFIG_HOME", ".config") / "autostart")
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        (d / "digimode.desktop").write_text(entry)
        print("wrote", d / "digimode.desktop")


class Rig:

    def __init__(self, cfg, notify=print):
        self.cfg, self.notify = cfg, notify
        self.services = cfg["services"]
        self.lock = threading.Lock()

    def running(self, name):
        s = self.services[name]
        return subprocess.run(["pgrep", "-f", s.get("match", s["cmd"])],
                              capture_output=True).returncode == 0

    def order(self, names):
        out = []
        def visit(n):
            for d in self.services[n].get("needs", []):
                visit(d)
            if n not in out:
                out.append(n)
        for n in names:
            visit(n)
        return out

    def stop(self, name):
        s = self.services[name]
        subprocess.run(["pkill", "-f", s.get("match", s["cmd"])])
        for _ in range(20):
            if not self.running(name):
                return
            time.sleep(0.25)
        self.notify(f"{name} did not exit; still running")

    def start(self, name):
        if self.running(name):
            return False
        s = self.services[name]
        group = s.get("group")
        if group:
            for other, o in self.services.items():
                if other != name and o.get("group") == group and self.running(other):
                    self.notify(f"Stopping {other} (shares {group} with {name})")
                    self.stop(other)
        LOGDIR.mkdir(parents=True, exist_ok=True)
        log = open(LOGDIR / f"{name}.log", "ab")
        subprocess.Popen(["bash", "-c", s["cmd"]], cwd=Path.home(), stdout=log,
                         stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        if "port" in s and not wait_port(s["port"]):
            raise RuntimeError(f"{name} started but port {s['port']} never opened "
                               f"(see {LOGDIR / name}.log)")
        return True

    def flrig(self):
        return xmlrpc.client.ServerProxy(self.cfg.get("flrig_url", "http://127.0.0.1:12345"))

    def tune(self, freq=None, mode=None):
        if mode == "data":
            mode = self.cfg["data_mode"]
        if mode:
            modes = self.flrig().rig.get_modes()
            if mode not in modes:
                raise RuntimeError(f"flrig has no mode {mode!r}; set data_mode in {CONFIG} "
                                   f"to one of: {' '.join(modes)}")
            self.flrig().rig.set_mode(mode)
        if freq:
            self.flrig().rig.set_vfo(float(freq))

    def status(self):
        rig = self.flrig().rig
        return float(rig.get_vfo()), rig.get_mode()

    def apply(self, preset):
        with self.lock:
            launched = [self.start(n) for n in self.order(preset["start"])]
            if "freq" in preset or "mode" in preset:
                if any(launched):
                    time.sleep(6)
                self.tune(preset.get("freq"), preset.get("mode"))
            self.notify(f"{preset['app']} {preset['name']} ready")


    def apps(self):
        out = {}
        for p in self.cfg.get("presets", []):
            out.setdefault(p["app"], []).append(p)
        return out

    def app_services(self, app):
        return {n for p in self.apps()[app] for n in p["start"]}

    def helpers(self):
        mains = {n for p in self.cfg.get("presets", []) for n in p["start"]}
        return [n for n in self.services if n not in mains]

    def active(self, up, freq=None):
        out = {}
        for app, presets in self.apps().items():
            if not self.app_services(app) & up:
                continue
            cands = [p for p in presets if set(p["start"]) <= up]
            if len(cands) > 1 and freq:
                cands = [p for p in cands if abs(p.get("freq", 0) - freq) < 5000] or [None]
            out[app] = cands[0] if len(cands) == 1 else None
        return out

    def stop_app(self, app):
        with self.lock:
            for n in self.app_services(app):
                self.stop(n)
            self.notify(f"{app} stopped")

    def stop_all(self):
        with self.lock:
            for n in reversed(self.order(list(self.services))):
                self.stop(n)
            self.notify("Everything stopped")


def wait_port(port, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 0.5).close()
            return True
        except OSError:
            time.sleep(0.5)
    return False


def toml_value(v):
    if isinstance(v, list):
        return "[" + ", ".join(map(toml_value, v)) + "]"
    return json.dumps(v)


def dump(cfg):
    header = []
    if CONFIG.exists():
        for line in CONFIG.read_text().splitlines():
            if line and not line.startswith("#"):
                break
            header.append(line)
    kv = lambda d: [f"{k} = {toml_value(v)}" for k, v in d.items()]
    out = header + kv({k: v for k, v in cfg.items() if k not in ("services", "presets")})
    for name, s in cfg["services"].items():
        key = name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else json.dumps(name)
        out += ["", f"[services.{key}]"] + kv(s)
    for p in cfg.get("presets", []):
        out += ["", "[[presets]]"] + kv(p)
    return "\n".join(out) + "\n"


def rename(cfg, old, new):
    cfg["services"] = {new if k == old else k: v for k, v in cfg["services"].items()}
    for lst in [s.get("needs", []) for s in cfg["services"].values()] + \
               [p["start"] for p in cfg.get("presets", [])]:
        lst[:] = [new if n == old else n for n in lst]


def users(cfg, name):
    return [k for k, s in cfg["services"].items() if name in s.get("needs", [])] + \
           [f"preset {p['app']} {p['name']}" for p in cfg.get("presets", []) if name in p["start"]]


def gui():
    from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
    from PyQt5.QtGui import QIcon
    from PyQt5.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QFormLayout,
                                 QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                                 QLineEdit, QListWidget, QMenu, QMessageBox, QPushButton,
                                 QSpinBox, QSystemTrayIcon, QToolButton, QVBoxLayout,
                                 QWidget)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    icon = QIcon(str(HERE / "icon.svg"))
    tray = QSystemTrayIcon(icon)

    class Bus(QObject):
        msg = pyqtSignal(str)
        done = pyqtSignal()
    bus = Bus()
    bus.msg.connect(lambda m: tray.showMessage("DigiMode", m, icon, 4000))
    state = {"rig": Rig(load(), bus.msg.emit), "busy": set(), "mode": {}, "active": {}, "up": set()}

    def bg(fn, *a, busy=None):
        if busy:
            state["busy"].add(busy)
        def run():
            try:
                fn(*a)
            except Exception as e:
                bus.msg.emit(f"Error: {e}")
            finally:
                state["busy"].discard(busy)
                bus.done.emit()
        threading.Thread(target=run, daemon=True).start()
        refresh()

    css = lambda c, b: (f"QPushButton {{color: {c}; font-weight: bold; border: 2px {b} {c};"
                        " border-radius: 4px; padding: 6px;}")
    ON, BUSY = css("#2a9d3a", "solid"), css("#c98a00", "dashed")
    win = QWidget()
    win.setWindowTitle("DigiMode")
    win.setWindowIcon(icon)
    outer = QVBoxLayout(win)
    nowline, rigline = QLabel(), QLabel()
    nowline.setStyleSheet("font-size: 15px; font-weight: bold;")
    rigline.setStyleSheet("color: gray;")
    outer.addWidget(nowline)
    outer.addWidget(rigline)
    panel = QVBoxLayout()
    outer.addLayout(panel)
    outer.addStretch()
    stopall, progs = QPushButton("Stop everything"), QPushButton("Programs…")
    bottom = QHBoxLayout()
    bottom.addWidget(progs)
    bottom.addWidget(stopall)
    outer.addLayout(bottom)
    widgets = {}

    stopall.clicked.connect(lambda: bg(state["rig"].stop_all))
    progs.clicked.connect(lambda: programs())

    def selected(a):
        presets = state["rig"].apps()[a]
        return next((p for p in presets if p["name"] == state["mode"].get(a)), presets[0])

    def toggle(key, on, stop, start):
        if key not in state["busy"]:
            bg(*(stop if on else start), busy=key)

    def pick_mode(a, p):
        state["mode"][a] = p["name"]
        if a in state["active"] and a not in state["busy"]:
            bg(state["rig"].apply, p, busy=a)
        refresh()

    def build():
        rig = state["rig"]
        apps = rig.apps()
        menu = QMenu()
        for a, presets in apps.items():
            sub = menu.addMenu(a)
            for p in presets:
                sub.addAction(p["name"]).triggered.connect(lambda _, p=p: bg(rig.apply, p, busy=p["app"]))
        menu.addSeparator()
        menu.addAction("Control panel").triggered.connect(lambda: (win.show(), win.raise_()))
        menu.addAction("Stop everything").triggered.connect(lambda: bg(rig.stop_all))
        menu.addAction("Programs…").triggered.connect(programs)
        menu.addAction("Reload config").triggered.connect(reload)
        menu.addAction("Quit DigiMode").triggered.connect(app.quit)
        tray.setContextMenu(menu)
        state["menu"] = menu

        while panel.count():
            panel.takeAt(0).widget().deleteLater()
        widgets.clear()
        for a, presets in apps.items():
            box = QFrame()
            box.setFrameShape(QFrame.StyledPanel)
            row = QHBoxLayout(box)
            btn = QPushButton(a)
            btn.setMinimumHeight(38)
            btn.clicked.connect(lambda _, a=a: toggle(a, a in state["active"], (rig.stop_app, a),
                                                      (rig.apply, selected(a))))
            row.addWidget(btn, 1)
            mb = None
            if len(presets) > 1:
                mb = QToolButton()
                mb.setPopupMode(QToolButton.InstantPopup)
                mb.setMinimumSize(120, 38)
                mb.setToolTip("Mode")
                m = QMenu(mb)
                for p in presets:
                    m.addAction(p["name"]).triggered.connect(lambda _, a=a, p=p: pick_mode(a, p))
                mb.setMenu(m)
                row.addWidget(mb)
            widgets[a] = (btn, mb)
            panel.addWidget(box)
        helpers = QGroupBox("Support programs")
        grid = QGridLayout(helpers)
        for i, n in enumerate(rig.helpers()):
            btn = QPushButton(n)
            btn.setMinimumHeight(36)
            btn.clicked.connect(lambda _, n=n: toggle(n, n in state["up"], (rig.stop, n),
                                                      (lambda: [rig.start(s) for s in rig.order([n])],)))
            grid.addWidget(btn, i // 3, i % 3)
            widgets[n] = (btn, None)
        panel.addWidget(helpers)
        refresh()

    def programs():
        cfg = copy.deepcopy(state["rig"].cfg)
        dlg = QDialog(win)
        dlg.setWindowTitle("DigiMode – Programs")
        dlg.setWindowIcon(icon)
        dlg.resize(760, 420)
        lst = QListWidget()
        lst.addItems(cfg["services"])
        name, cmd, match, group, needs = (QLineEdit() for _ in range(5))
        port = QSpinBox()
        port.setRange(0, 65535)
        port.setSpecialValueText("none")
        found = QLabel()
        found.setWordWrap(True)
        found.setTextInteractionFlags(Qt.TextSelectableByMouse)
        cmd.setPlaceholderText("e.g. wine ~/.wine/drive_c/VARA/VARA.exe")
        match.setPlaceholderText("regex for the running process; blank = same as command")
        group.setPlaceholderText("e.g. hf-audio — one program per group runs at a time")
        needs.setPlaceholderText("comma-separated, e.g. flrig, rigctld")
        test = QPushButton("Test")
        mrow = QHBoxLayout()
        mrow.addWidget(match)
        mrow.addWidget(test)
        form = QFormLayout()
        form.addRow("Name", name)
        form.addRow("Start command", cmd)
        form.addRow("Running when matches", mrow)
        form.addRow("", found)
        form.addRow("Wait for TCP port", port)
        form.addRow("Group", group)
        form.addRow("Start first", needs)

        add, remove = QPushButton("Add"), QPushButton("Remove")
        left = QVBoxLayout()
        left.addWidget(lst)
        lbtn = QHBoxLayout()
        lbtn.addWidget(add)
        lbtn.addWidget(remove)
        left.addLayout(lbtn)
        body = QHBoxLayout()
        body.addLayout(left, 1)
        body.addLayout(form, 3)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        top = QVBoxLayout(dlg)
        top.addLayout(body)
        top.addWidget(buttons)
        cur = {"name": None}

        def commit():
            old = cur["name"]
            if old is None:
                return True
            new = name.text().strip()
            if not new or (new != old and new in cfg["services"]):
                QMessageBox.warning(dlg, "DigiMode", f"Name '{new}' is empty or already used.")
                return False
            if new != old:
                rename(cfg, old, new)
                lst.findItems(old, Qt.MatchExactly)[0].setText(new)
                cur["name"] = new
            s = {"cmd": cmd.text().strip()}
            for k, v in (("match", match.text().strip()), ("group", group.text().strip())):
                if v:
                    s[k] = v
            if port.value():
                s["port"] = port.value()
            deps = [d.strip() for d in needs.text().split(",") if d.strip()]
            bad = [d for d in deps if d not in cfg["services"]]
            if bad:
                QMessageBox.warning(dlg, "DigiMode", f"Unknown program in 'Start first': {', '.join(bad)}")
                return False
            if deps:
                s["needs"] = deps
            cfg["services"][new] = s
            return True

        def show(n):
            cur["name"] = n
            s = cfg["services"].get(n, {})
            name.setText(n or "")
            cmd.setText(s.get("cmd", ""))
            match.setText(s.get("match", ""))
            port.setValue(s.get("port", 0))
            group.setText(s.get("group", ""))
            needs.setText(", ".join(s.get("needs", [])))
            found.clear()

        def select(row):
            if row < 0:
                return
            n = lst.item(row).text()
            if n == cur["name"]:
                return
            if not commit():
                lst.blockSignals(True)
                lst.setCurrentRow(list(cfg["services"]).index(cur["name"]))
                lst.blockSignals(False)
                return
            show(n)

        def do_test():
            pat = match.text().strip() or cmd.text().strip()
            r = subprocess.run(["pgrep", "-af", pat], capture_output=True, text=True)
            hits = r.stdout.strip().splitlines()
            found.setText("Matches now running:\n" + "\n".join(h[:120] for h in hits)
                          if hits else "Nothing running matches. Start the program, then look "
                          "for its name in `ps -ef` (Wine/Bottles show C:\\…\\NAME.exe).")

        def do_add():
            if not commit():
                return
            n = "new-program"
            while n in cfg["services"]:
                n += "-2"
            cfg["services"][n] = {"cmd": ""}
            lst.addItem(n)
            cur["name"] = None
            lst.setCurrentRow(lst.count() - 1)
            name.setFocus()
            name.selectAll()

        def do_remove():
            n = cur["name"]
            if n is None:
                return
            u = users(cfg, n)
            if u:
                QMessageBox.warning(dlg, "DigiMode", f"{n} is still used by: {', '.join(u)}")
                return
            del cfg["services"][n]
            cur["name"] = None
            lst.takeItem(lst.currentRow())

        def do_save():
            if not commit():
                return
            missing = [k for k, s in cfg["services"].items() if not s.get("cmd")]
            if missing:
                QMessageBox.warning(dlg, "DigiMode", f"No start command for: {', '.join(missing)}")
                return
            CONFIG.write_text(dump(cfg))
            dlg.accept()
            reload()

        lst.currentRowChanged.connect(select)
        test.clicked.connect(do_test)
        add.clicked.connect(do_add)
        remove.clicked.connect(do_remove)
        buttons.accepted.connect(do_save)
        buttons.rejected.connect(dlg.reject)
        lst.setCurrentRow(0)
        dlg.exec_()

    def reload():
        try:
            state["rig"] = Rig(load(), bus.msg.emit)
        except Exception as e:
            bus.msg.emit(f"Config error: {e}")
            return
        build()
        bus.msg.emit("Config reloaded")

    def refresh():
        rig = state["rig"]
        up = {n for n in rig.services if rig.running(n)}
        freq = None
        if win.isVisible():
            try:
                freq, m = rig.status()
                rigline.setText(f"{freq / 1e6:.4f} MHz  {m}")
            except Exception:
                rigline.setText("flrig not reachable")
        act = rig.active(up, freq)
        state.update(up=up, active=act)
        apps = rig.apps()
        now = " · ".join(f"{a} — {p['name']}" if p and len(apps[a]) > 1 else a
                         for a, p in act.items()) or "Nothing running"
        nowline.setText(now)
        win.setWindowTitle(f"DigiMode — {now}")
        tray.setToolTip(f"DigiMode: {now}")
        for key, (btn, mb) in widgets.items():
            busy = key in state["busy"]
            on = key in act if key in apps else key in up
            btn.setStyleSheet(BUSY if busy else ON if on else "")
            btn.setText(key + (" …" if busy else ""))
            if mb:
                if act.get(key) and not busy:
                    state["mode"][key] = act[key]["name"]
                mb.setText(selected(key)["name"])

    timer = QTimer()
    timer.timeout.connect(refresh)
    timer.start(2000)

    bus.done.connect(refresh)
    tray.activated.connect(lambda r: r == QSystemTrayIcon.Trigger and
                           (win.hide() if win.isVisible() else (win.show(), refresh())))
    build()
    tray.show()
    sys.exit(app.exec_())


def selftest():
    cfg = tomllib.loads((HERE / "modes.default.toml").read_text())
    r = Rig(cfg)
    for p in cfg["presets"]:
        for n in r.order(p["start"]):
            assert n in cfg["services"], f"preset {p['name']}: unknown service {n}"
    o = r.order(["pat-ardop"])
    assert o.index("flrig") < o.index("rigctld") < o.index("pat-ardop"), o
    assert o.index("ardopcf") < o.index("pat-ardop") and len(o) == len(set(o)), o
    assert tomllib.loads(dump(cfg)) == cfg, "TOML round-trip changed the config"
    c = copy.deepcopy(cfg)
    rename(c, "flrig", "rigctl-gui")
    assert "flrig" not in json.dumps(c["services"]["rigctld"]) and "rigctl-gui" in c["services"]
    assert users(cfg, "ardopcf") == ["pat-ardop"], users(cfg, "ardopcf")
    assert "pat-ardop" not in r.helpers() and "flrig" in r.helpers()
    act = r.active({"flrig", "rigctld", "pat-ardop", "wsjtx"}, 14074000)
    assert act["Pat"]["name"] == "ARDOP" and act["WSJT-X"]["name"] == "FT8 20m", act
    assert r.active({"wsjtx"}, 3_000_000) == {"WSJT-X": None}
    print("ok:", len(cfg["services"]), "services,", len(cfg["presets"]), "presets")


if __name__ == "__main__":
    if "--test" in sys.argv:
        selftest()
    elif "--install" in sys.argv:
        install("--autostart" in sys.argv)
    else:
        gui()
