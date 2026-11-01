"""迈从 MCHOSE G87 V2 无线键盘电量托盘监控

协议（逆向自官方网页驱动 / 客户端）：
  通道   usagePage 1 / usage 0，interface 1，reportId 0，65 字节写入
  请求   00 55 05 00 38 38 00 00 00 + 56 字节 0
  响应   AA 05 ...  ->  第 40 字节 = 电量百分比
"""

import json
import os
import sys
import threading
import time
import winreg
from datetime import datetime

import hid
import pystray
from PIL import Image, ImageDraw, ImageFont

VID = 0x3837
PID = 0x3033
DEVICE_NAME = "迈从 G87 V2"

APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "MchoseBattery")
CFG_PATH = os.path.join(APP_DIR, "config.json")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "MchoseBattery"

DEFAULT_CFG = {"interval_min": 10, "autostart": False, "low_threshold": 20}
INTERVAL_CHOICES = [1, 5, 10, 15, 30, 60]

cfg = dict(DEFAULT_CFG)
state = {"pct": None, "updated": None, "low_notified": False}
wake = threading.Event()
stop = threading.Event()


# ---------------- 配置 ----------------
def load_cfg():
    global cfg
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass


def save_cfg():
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------- 开机自启 ----------------
def autostart_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pyw = sys.executable
    if pyw.lower().endswith("python.exe"):
        pyw = pyw[:-10] + "pythonw.exe"
    return f'"{pyw}" "{os.path.abspath(__file__)}"'


def apply_autostart(on):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
            else:
                try:
                    winreg.DeleteValue(k, RUN_NAME)
                except FileNotFoundError:
                    pass
    except Exception:
        pass


# ---------------- HID 读数 ----------------
def read_battery():
    """返回 0-100 的整数，失败返回 None。"""
    try:
        for d in hid.enumerate(VID, PID):
            if d.get("usage_page") == 1 and d.get("interface_number") == 1:
                dev = hid.device()
                try:
                    dev.open_path(d["path"])
                    pkt = [0x00, 0x55, 0x05, 0x00, 0x38, 0x38, 0x00, 0x00, 0x00] + [0] * 56
                    dev.write(pkt)
                    deadline = time.time() + 1.5
                    while time.time() < deadline:
                        r = dev.read(64, 250)
                        if r and len(r) >= 41 and r[0] == 0xAA and r[1] == 0x05:
                            val = r[40]
                            if 0 <= val <= 100:
                                return val
                            return None
                finally:
                    dev.close()
                return None
    except Exception:
        return None
    return None


# ---------------- 图标 ----------------
def taskbar_is_light():
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as k:
            v, _ = winreg.QueryValueEx(k, "SystemUsesLightTheme")
            return bool(v)
    except Exception:
        return False


def _load_font(size):
    windir = os.environ.get("WINDIR", r"C:\Windows")
    for name in ("seguibl.ttf", "segoeuib.ttf", "arialbd.ttf", "tahomabd.ttf"):
        p = os.path.join(windir, "Fonts", name)
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _fit_font(dr, text, box_w, box_h, stroke):
    size = 62
    while size > 9:
        f = _load_font(size)
        b = dr.textbbox((0, 0), text, font=f, stroke_width=stroke)
        if (b[2] - b[0]) <= box_w and (b[3] - b[1]) <= box_h:
            return f
        size -= 2
    return _load_font(10)


def make_icon(pct):
    """直接在图标上画电量数字，低电量时整组变红。"""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)

    light_taskbar = taskbar_is_light()
    main = (24, 24, 27, 255) if light_taskbar else (252, 252, 252, 255)

    if pct is None:
        text, fill = "--", main
    else:
        text = str(pct)
        if pct <= cfg["low_threshold"]:
            fill = (206, 38, 38, 255)
        elif pct <= cfg["low_threshold"] + 15:
            fill = (186, 108, 0, 255)
        else:
            fill = main

    font = _fit_font(dr, text, 62, 58, 0)
    dr.text((size / 2, size / 2 + 1), text, font=font, fill=fill, anchor="mm")
    return img


def tooltip():
    p = state["pct"]
    if p is None:
        return f"{DEVICE_NAME} — 未连接"
    t = state["updated"]
    suffix = f"（{t.strftime('%H:%M')} 更新）" if t else ""
    return f"{DEVICE_NAME} — {p}%{suffix}"


def refresh_ui():
    try:
        icon.icon = make_icon(state["pct"])
        icon.title = tooltip()
        # pystray 的 win32 后端会缓存菜单句柄，动态文字只在重建时求值，
        # 所以每次读数变化都要让菜单失效，否则右键菜单第一行永远是旧值。
        icon.update_menu()
    except Exception:
        pass


# ---------------- 刷新循环 ----------------
def do_refresh():
    pct = read_battery()
    state["pct"] = pct
    state["updated"] = datetime.now()

    if pct is not None:
        if pct <= cfg["low_threshold"]:
            if not state["low_notified"]:
                state["low_notified"] = True
                try:
                    icon.notify(f"键盘剩余电量 {pct}%，该充电了", "迈从 G87 V2 电量偏低")
                except Exception:
                    pass
        elif pct > cfg["low_threshold"] + 5:
            state["low_notified"] = False
    refresh_ui()


def worker():
    while not stop.is_set():
        do_refresh()
        minutes = cfg.get("interval_min", 10)
        timeout = minutes * 60 if minutes else None
        woke = wake.wait(timeout)
        wake.clear()
        if stop.is_set():
            break
        if not woke and timeout is None:
            continue


# ---------------- 菜单动作 ----------------
def on_refresh(icon_, item):
    wake.set()


def set_interval(m):
    def handler(icon_, item):
        cfg["interval_min"] = m
        save_cfg()
        wake.set()

    return handler


def toggle_autostart(icon_, item):
    cfg["autostart"] = not cfg["autostart"]
    apply_autostart(cfg["autostart"])
    save_cfg()


def set_threshold(v):
    def handler(icon_, item):
        cfg["low_threshold"] = v
        save_cfg()
        refresh_ui()

    return handler


def on_quit(icon_, item):
    stop.set()
    wake.set()
    icon_.stop()


# ---------------- 组装 ----------------
def build_menu():
    interval_items = [
        pystray.MenuItem(
            f"{m} 分钟",
            set_interval(m),
            checked=lambda item, m=m: cfg.get("interval_min") == m,
            radio=True,
        )
        for m in INTERVAL_CHOICES
    ]
    interval_items.append(pystray.MenuItem(
        "关闭自动刷新",
        set_interval(0),
        checked=lambda item: cfg.get("interval_min") == 0,
        radio=True,
    ))

    threshold_items = [
        pystray.MenuItem(
            f"{v}%",
            set_threshold(v),
            checked=lambda item, v=v: cfg.get("low_threshold") == v,
            radio=True,
        )
        for v in (10, 20, 30)
    ]

    return pystray.Menu(
        pystray.MenuItem(lambda item: tooltip(), lambda icon_, item: None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("立即刷新", on_refresh, default=True),
        pystray.MenuItem("自动刷新间隔", pystray.Menu(*interval_items)),
        pystray.MenuItem("低电量提醒", pystray.Menu(*threshold_items)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("开机自启", toggle_autostart, checked=lambda item: cfg.get("autostart", False)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", on_quit),
    )


load_cfg()
apply_autostart(cfg.get("autostart", False))
icon = pystray.Icon("mchose_battery", make_icon(None), f"{DEVICE_NAME} — 读取中…", build_menu())


def setup(icon_):
    icon_.visible = True
    refresh_ui()
    threading.Thread(target=worker, daemon=True).start()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        print("battery =", read_battery())
        sys.exit(0)
    icon.run(setup=setup)
