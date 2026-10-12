#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DeepSeek 浮窗 v2 —— 自绘无边框浮窗（WinForms + 系统 WebView2）

功能：
  · 顶部工具栏：站点名 / 加载状态、还原比例、夜间模式、快捷键设置、收起、关闭
  · 左侧网页栏：多站点快速切换，可增删网址
  · 右下角把手：拖动调整窗口大小；尺寸 / 位置自动记忆
  · 全局快捷键（可在设置里改）：默认 Alt+` 显示/收起、Ctrl+Alt+R 重载、Ctrl+Alt+Q 退出、Esc 收起
  · 夜间 / 日间模式切换

依赖：pywebview（只用它自带的 WebView2 程序集）、keyboard、pystray、pillow
"""

import ctypes
import json
import os
import socket
import sys
import sysconfig
import threading
import time

# ---------------------------------------------------------------- 路径常量
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "ds_popup_config.json")
LOG_PATH = os.path.join(APP_DIR, "ds_popup.log")
PROFILE_DIR = os.path.join(APP_DIR, "webview_profile")
SCRIPT_PATH = os.path.join(APP_DIR, "ds_popup.py")
SHORTCUT_NAME = "DeepSeek 浮窗.lnk"
SINGLE_INSTANCE_PORT = 47733

DEFAULT_W, DEFAULT_H = 480, 820
MIN_W, MIN_H = 360, 480

# ---------------------------------------------------------------- .NET 程序集
_LIB = os.path.join(sysconfig.get_paths()['purelib'], 'webview', 'lib')
for _plat in ('win-x64', 'win-x86', 'win-arm64'):
    _p = os.path.join(_LIB, 'runtimes', _plat, 'native')
    if os.path.isdir(_p):
        os.environ['Path'] += ';' + _p

import clr  # noqa: E402

clr.AddReference('System.Windows.Forms')
clr.AddReference('System.Drawing')
clr.AddReference(os.path.join(_LIB, 'Microsoft.Web.WebView2.Core.dll'))
clr.AddReference(os.path.join(_LIB, 'Microsoft.Web.WebView2.WinForms.dll'))

import System.Windows.Forms as WinForms  # noqa: E402
from System import Action, String  # noqa: E402
from System.Drawing import (  # noqa: E402
    Color,
    ContentAlignment,
    Font,
    FontStyle,
    Point,
    Size,
)
from System.Threading import ApartmentState, Thread, ThreadStart  # noqa: E402
from System.Threading.Tasks import Task  # noqa: E402
from Microsoft.Web.WebView2.Core import CoreWebView2PreferredColorScheme  # noqa: E402
from Microsoft.Web.WebView2.WinForms import (  # noqa: E402
    CoreWebView2CreationProperties,
    WebView2,
)

try:
    import keyboard
except Exception:  # pragma: no cover
    keyboard = None

_u32 = ctypes.windll.user32
_u32.SendMessageW.argtypes = [
    ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p
]
_u32.SendMessageW.restype = ctypes.c_ssize_t

WM_NCLBUTTONDOWN = 0xA1
HTCAPTION = 2

# Keys.None / FormBorderStyle.None 的 None 与 Python 关键字冲突，只能 getattr
_KEYS_NONE = getattr(WinForms.Keys, 'None')
_FBS_NONE = getattr(WinForms.FormBorderStyle, 'None')

# ---------------------------------------------------------------- 配置
DEFAULT_CONFIG = {
    "sites": [
        {"name": "DeepSeek", "url": "https://chat.deepseek.com", "short": "DS"},
    ],
    "active": 0,
    "window": {"width": DEFAULT_W, "height": DEFAULT_H, "x": None, "y": None},
    "theme": "dark",
    "always_on_top": True,
    "hotkeys": {
        # 显示 / 隐藏：程序运行时生效，可用任意组合键（包括反引号）
        "toggle": "alt+`",
        "reload": "ctrl+alt+r",
        "quit": "ctrl+alt+q",
        "hide": "esc",
    },
    # 启动程序用的键：程序没运行时按下即启动；已在运行时按下即唤出窗口。
    # 由 Windows 快捷方式承载，只能 ctrl/alt/shift + 字母、数字或 F1-F12
    "launch_hotkey": "ctrl+alt+d",
    "persistent_profile": True,
    "webview_args": "--no-sandbox",
    # 开机自动启动（在启动文件夹放快捷方式）
    "autostart": False,
}

THEMES = {
    "dark": {
        "bar": (36, 42, 50), "side": (28, 33, 40), "bg": (24, 28, 34),
        "text": (216, 222, 233), "sub": (139, 149, 165), "hover": (52, 61, 74),
        "accent": (77, 133, 224), "line": (52, 60, 72), "grip": (74, 84, 100),
        "scheme": "Dark",
    },
    "light": {
        "bar": (255, 255, 255), "side": (238, 241, 246), "bg": (246, 248, 251),
        "text": (43, 52, 69), "sub": (122, 134, 153), "hover": (224, 231, 242),
        "accent": (61, 122, 224), "line": (219, 225, 234), "grip": (200, 208, 220),
        "scheme": "Light",
    },
}


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    raw = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for k, v in raw.items():
            if k not in cfg:
                continue          # 丢弃旧版本遗留的字段
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except (OSError, ValueError):
        raw = {}
    # v5 曾把显示键改成 ctrl+alt+d（那时启动/显隐共用一把键）。
    # 若用户配置里还没有 launch_hotkey 而显示键正是 ctrl+alt+d，说明是那次迁移，还原。
    hk = cfg.get("hotkeys") or {}
    if ("launch_hotkey" not in raw
            and (hk.get("toggle") or "").strip().lower() == "ctrl+alt+d"):
        hk["toggle"] = "alt+`"
        cfg["launch_hotkey"] = "ctrl+alt+d"
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError as e:
        log("写配置失败: %s" % e)


def C(rgb):
    return Color.FromArgb(rgb[0], rgb[1], rgb[2])


def mk(cls, **kw):
    """先构造、再赋属性。

    pythonnet 不支持在 .NET 构造函数里传属性参数（会报
    'No method matches given arguments for Xxx..ctor'），必须这样写。
    """
    c = cls()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


# ---------------------------------------------------------------- 快捷方式
def _pythonw_path():
    p = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return p if os.path.exists(p) else sys.executable


def _shell():
    import win32com.client
    return win32com.client.Dispatch("WScript.Shell")


def shortcut_path(folder):
    """folder: Desktop / Programs / Startup"""
    return os.path.join(_shell().SpecialFolders(folder), SHORTCUT_NAME)


def create_shortcut(path, hotkey=None):
    sh = _shell()
    lnk = sh.CreateShortcut(path)
    lnk.TargetPath = _pythonw_path()
    lnk.Arguments = '"%s"' % SCRIPT_PATH
    lnk.WorkingDirectory = APP_DIR
    lnk.Description = "DeepSeek 浮窗 —— 网页版快捷聊天窗口"
    if hotkey:
        lnk.Hotkey = hotkey
    lnk.Save()
    return path


def remove_shortcut(path):
    try:
        if os.path.exists(path):
            os.remove(path)
            return True
    except OSError as e:
        log("删除快捷方式失败: %s" % e)
    return False


def to_lnk_hotkey(combo):
    """把 ctrl+alt+d 转成快捷方式接受的 CTRL+ALT+D；不支持的键返回 None。

    快捷方式热键只认 Ctrl/Alt/Shift + 字母/数字/F1-F12。
    """
    if not combo:
        return None
    mods, key = [], None
    for p in [x.strip().lower() for x in combo.split("+") if x.strip()]:
        if p in ("ctrl", "control"):
            mods.append("CTRL")
        elif p == "alt":
            mods.append("ALT")
        elif p == "shift":
            mods.append("SHIFT")
        elif p in ("win", "windows"):
            return None
        else:
            key = p
    if not key or not mods:
        return None
    if len(key) == 1 and (key.isalpha() or key.isdigit()):
        key = key.upper()
    elif len(key) <= 3 and key[0] == "f" and key[1:].isdigit():
        key = key.upper()
    else:
        return None
    return "+".join(mods + [key])


def apply_shortcuts(cfg):
    """同步「开机自启动」，并用启动键注册开始菜单快捷方式"""
    ok = False
    try:
        startup_lnk = shortcut_path("Startup")
        if cfg.get("autostart"):
            create_shortcut(startup_lnk)
            log("已开启开机自启动")
        else:
            if remove_shortcut(startup_lnk):
                log("已关闭开机自启动")

        combo = (cfg.get("launch_hotkey") or "").strip()
        menu_lnk = shortcut_path("Programs")
        hk = to_lnk_hotkey(combo)
        if hk:
            create_shortcut(menu_lnk, hotkey=hk)
            log("启动快捷键 %s 已注册（程序未运行时按下即启动）" % hk)
            ok = True
        else:
            remove_shortcut(menu_lnk)
            if combo:
                log("!! %r 不能作为启动键（只支持 ctrl/alt/shift + 字母或数字）"
                    % combo)
    except Exception as e:
        log("设置快捷方式失败: %s" % e)
    return ok


def site_short(site):
    s = (site.get("short") or "").strip()
    if s:
        return s
    name = (site.get("name") or "?").strip()
    return (name[0] if name else "?").upper()


# 唤起后把光标落到页面输入框（DeepSeek 用的是唯一的 textarea[name=search]）
FOCUS_INPUT_JS = (
    "(function(){"
    "var e=document.querySelector('textarea[name=\"search\"]')"
    "||document.querySelector('textarea')"
    "||document.querySelector('[contenteditable=\"true\"]');"
    "if(!e)return 'none';"
    "try{e.focus();e.click&&e.click();}catch(err){}"
    "return 'ok';"
    "})()"
)


# ---------------------------------------------------------------- Win32
_EnumWindowsProc = ctypes.WINFUNCTYPE(
    ctypes.c_bool, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
)


def own_window_focused():
    """前台窗口是否属于本进程（用于让 Esc 只在浮窗激活时生效）"""
    fg = _u32.GetForegroundWindow()
    if not fg:
        return False

    def _cb(hwnd, _l):
        return hwnd != fg

    return not _u32.EnumWindows(_EnumWindowsProc(_cb), 0)


_KEY_NAMES = {
    "Oemtilde": "`", "OemMinus": "-", "Oemplus": "=", "OemOpenBrackets": "[",
    "Oem6": "]", "Oem5": "\\", "Oem1": ";", "OemQuotes": "'", "Oemcomma": ",",
    "OemPeriod": ".", "OemQuestion": "/", "Space": "space", "Escape": "esc",
    "Tab": "tab", "Enter": "enter", "Back": "backspace", "Delete": "delete",
    "Insert": "insert", "Home": "home", "End": "end", "PageUp": "page up",
    "PageDown": "page down", "Up": "up", "Down": "down", "Left": "left",
    "Right": "right", "Capital": "caps lock",
}


def key_to_name(code):
    """WinForms.Keys -> keyboard 库认识的键名"""
    s = str(code)
    if s in _KEY_NAMES:
        return _KEY_NAMES[s]
    if len(s) == 2 and s[0] == "D" and s[1].isdigit():
        return s[1]
    if len(s) == 1:
        return s.lower()
    if len(s) <= 3 and s[0] == "F" and s[1:].isdigit():
        return s.lower()
    if s.startswith("NumPad"):
        return "num " + s[6:].lower()
    return s.lower()


# ---------------------------------------------------------------- 小对话框
def _dlg_base(title, w, h, theme):
    f = WinForms.Form()
    f.Text = title
    f.ClientSize = Size(w, h)
    f.FormBorderStyle = WinForms.FormBorderStyle.FixedDialog
    f.StartPosition = WinForms.FormStartPosition.CenterParent
    f.MaximizeBox = False
    f.MinimizeBox = False
    f.TopMost = True
    f.Font = Font("Microsoft YaHei UI", 9)
    f.BackColor = C(theme["bar"])
    f.ForeColor = C(theme["text"])
    return f


class SiteDialog:
    """添加网址"""

    @staticmethod
    def show(parent, theme, title="添加网址"):
        f = _dlg_base(title, 390, 175, theme)
        lb1 = mk(WinForms.Label, Text="名称", Location=Point(18, 20), Size=Size(60, 22))
        tb1 = mk(WinForms.TextBox, Location=Point(84, 18), Size=Size(280, 24))
        lb2 = mk(WinForms.Label, Text="网址", Location=Point(18, 60), Size=Size(60, 22))
        tb2 = mk(WinForms.TextBox, Text="https://", Location=Point(84, 58), Size=Size(280, 24))
        ok = mk(WinForms.Button, Text="确定", Location=Point(196, 112), Size=Size(80, 30))
        cancel = mk(WinForms.Button, Text="取消", Location=Point(284, 112), Size=Size(80, 30))
        for c in (lb1, lb2, tb1, tb2, ok, cancel):
            c.ForeColor = C(theme["text"])
            c.BackColor = C(theme["bar"])
            f.Controls.Add(c)
        for b in (ok, cancel):
            b.FlatStyle = WinForms.FlatStyle.Flat
            b.FlatAppearance.BorderColor = C(theme["line"])

        result = {"ok": False}
        f.AcceptButton = ok
        f.CancelButton = cancel
        refs = []          # 保住委托引用

        def on_ok(s, a):
            nm = tb1.Text.strip()
            u = tb2.Text.strip()
            if u.startswith("https://") and len(u) <= 8:
                u = ""
            if not u:
                WinForms.MessageBox.Show("请填写网址", "提示")
                return
            if not u.startswith(("http://", "https://")):
                u = "https://" + u
            result["name"] = nm or u.split("//")[-1].split("/")[0]
            result["url"] = u
            result["ok"] = True
            f.Close()

        on_cancel = lambda s, a: f.Close()
        refs.append(on_ok)
        refs.append(on_cancel)
        ok.Click += on_ok
        cancel.Click += on_cancel
        f.ShowDialog(parent)
        return result


class HotkeyDialog:
    """快捷键 + 常规设置"""

    ROWS = [
        ("toggle", "显示 / 收起浮窗"),
        ("launch_hotkey", "启动程序（未运行时）"),
        ("reload", "重新加载页面"),
        ("quit", "退出程序"),
        ("hide", "Esc 收起浮窗"),
    ]

    @staticmethod
    def show(parent, theme, cfg):
        values = dict(cfg.get("hotkeys") or {})
        values["launch_hotkey"] = cfg.get("launch_hotkey") or ""
        f = _dlg_base("设置", 452, 402, theme)

        hint = mk(WinForms.Label, Text="点「修改」后直接按新组合键",
                  Location=Point(18, 12), Size=Size(410, 20))
        hint.ForeColor = C(theme["sub"])
        f.Controls.Add(hint)

        hint2 = mk(WinForms.Label,
                   Text="「启动程序」由 Windows 快捷方式承载，只支持 Ctrl/Alt/Shift + 字母、数字或 F1–F12",
                   Location=Point(18, 32), Size=Size(425, 20))
        hint2.ForeColor = C(theme["sub"])
        hint2.Font = Font("Microsoft YaHei UI", 8)
        f.Controls.Add(hint2)

        recording = {"key": None}
        value_labels = {}
        buttons = {}
        refs = []

        def on_record_click(k):
            recording["key"] = k
            buttons[k].Text = "按键…"
            f.Focus()

        for i, (key, desc) in enumerate(HotkeyDialog.ROWS):
            y = 62 + i * 36
            lb = mk(WinForms.Label, Text=desc, Location=Point(18, y + 4), Size=Size(178, 22))
            val = mk(WinForms.Label, Text=values.get(key, ""),
                     Location=Point(206, y + 4), Size=Size(140, 22))
            btn = mk(WinForms.Button, Text="修改", Location=Point(354, y), Size=Size(80, 26))
            lb.ForeColor = C(theme["text"])
            val.ForeColor = C(theme["accent"])
            btn.FlatStyle = WinForms.FlatStyle.Flat
            btn.FlatAppearance.BorderColor = C(theme["line"])
            btn.ForeColor = C(theme["text"])
            btn.BackColor = C(theme["bar"])
            value_labels[key] = val
            buttons[key] = btn
            cb = (lambda s, a, k=key: on_record_click(k))
            refs.append(cb)
            btn.Click += cb
            for c in (lb, val, btn):
                f.Controls.Add(c)

        chk = mk(WinForms.CheckBox, Text="开机自动启动浮窗",
                 Location=Point(18, 256), Size=Size(260, 24),
                 Checked=bool(cfg.get("autostart")))
        chk.ForeColor = C(theme["text"])
        chk.BackColor = C(theme["bar"])
        chk.FlatStyle = WinForms.FlatStyle.Flat
        f.Controls.Add(chk)

        ok = mk(WinForms.Button, Text="保存", Location=Point(260, 308), Size=Size(80, 30))
        cancel = mk(WinForms.Button, Text="取消", Location=Point(350, 308), Size=Size(80, 30))
        for b in (ok, cancel):
            b.FlatStyle = WinForms.FlatStyle.Flat
            b.FlatAppearance.BorderColor = C(theme["line"])
            b.ForeColor = C(theme["text"])
            b.BackColor = C(theme["bar"])
            f.Controls.Add(b)

        def on_key(sender, args):
            k = recording["key"]
            if not k:
                return
            args.SuppressKeyPress = True
            code = args.KeyCode
            if code in (WinForms.Keys.ControlKey, WinForms.Keys.Menu,
                        WinForms.Keys.ShiftKey, _KEYS_NONE,
                        WinForms.Keys.LWin, WinForms.Keys.RWin):
                return
            mods = []
            if args.Control:
                mods.append("ctrl")
            if args.Alt:
                mods.append("alt")
            if args.Shift:
                mods.append("shift")
            combo = "+".join(mods + [key_to_name(code)])
            values[k] = combo
            value_labels[k].Text = combo
            buttons[k].Text = "修改"
            recording["key"] = None

        f.KeyPreview = True
        f.KeyDown += on_key

        result = {"saved": False, "hotkeys": {}, "launch_hotkey": "",
                  "autostart": bool(cfg.get("autostart"))}

        def on_ok(s, a):
            launch = (values.get("launch_hotkey") or "").strip()
            if launch:
                if to_lnk_hotkey(launch) is None:
                    WinForms.MessageBox.Show(
                        "「%s」不能作为启动键。\nWindows 快捷方式只支持 "
                        "Ctrl / Alt / Shift + 字母、数字或 F1–F12。" % launch, "提示")
                    return
                if launch.lower() == (values.get("toggle") or "").strip().lower():
                    WinForms.MessageBox.Show("启动键不能与「显示 / 收起」键相同。", "提示")
                    return
            result["saved"] = True
            result["hotkeys"] = {k: v for k, v in values.items() if k != "launch_hotkey"}
            result["launch_hotkey"] = launch
            result["autostart"] = bool(chk.Checked)
            f.Close()

        cancel_cb = lambda s, a: f.Close()
        refs.append(on_ok)
        refs.append(cancel_cb)
        ok.Click += on_ok
        cancel.Click += cancel_cb
        f.ShowDialog(parent)
        return result


# ---------------------------------------------------------------- 主应用
class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.form = None
        self.wv = None
        self.core = None
        self.tray = None
        self.visible = True
        self.exited = False
        self.ready = threading.Event()
        self._keep = []               # 保持委托引用，防止被 GC
        self._nav_fails = 0
        self._nav_url = ""
        self._last_geom = None
        self._resizing = False
        self._rz_mouse = None
        self._rz_size = None
        self.theme = THEMES.get(cfg.get("theme", "dark"), THEMES["dark"])

    # ---------- 小工具 ----------
    def hold(self, fn):
        self._keep.append(fn)
        return fn

    def ui(self, fn):
        """切到 GUI 线程执行（供快捷键 / 托盘线程调用）"""
        try:
            if self.form is not None:
                self.form.Invoke(Action(fn))
        except Exception:
            pass

    # ---------- 界面 ----------
    def build(self):
        t = self.theme
        f = WinForms.Form()
        f.Text = "DeepSeek 浮窗"
        f.FormBorderStyle = _FBS_NONE
        f.TopMost = bool(self.cfg.get("always_on_top", True))
        f.ShowInTaskbar = True
        f.Font = Font("Microsoft YaHei UI", 9)
        f.BackColor = C(t["bg"])
        f.ForeColor = C(t["text"])
        f.MinimumSize = Size(MIN_W, MIN_H)
        f.StartPosition = WinForms.FormStartPosition.Manual
        self.form = f

        # ---- 顶部工具栏 ----
        bar = WinForms.Panel()
        bar.Dock = WinForms.DockStyle.Top
        bar.Height = 36
        bar.BackColor = C(t["bar"])
        f.Controls.Add(bar)
        self.bar = bar

        # 站点切换按钮（放在顶部，网页因此能占满整宽）
        site_btn = WinForms.Label()
        site_btn.Text = "%s ▾" % site_short(self.active_site())
        site_btn.Location = Point(10, 6)
        site_btn.Size = Size(86, 24)
        site_btn.TextAlign = ContentAlignment.MiddleLeft
        site_btn.ForeColor = C(t["text"])
        site_btn.BackColor = C(t["bar"])
        site_btn.Font = Font("Microsoft YaHei UI", 9.5, FontStyle.Bold)
        site_btn.Cursor = WinForms.Cursors.Hand
        site_btn.Click += self.hold(lambda s, a: self.show_site_menu())
        tip_site = WinForms.ToolTip()
        tip_site.SetToolTip(site_btn, "切换 / 添加 / 删除网址")
        self._keep.append(tip_site)
        bar.Controls.Add(site_btn)
        self.site_btn = site_btn

        # 加载状态文字
        title = WinForms.Label()
        title.Text = ""
        title.Location = Point(98, 4)
        title.Size = Size(140, 28)
        title.TextAlign = ContentAlignment.MiddleLeft
        title.ForeColor = C(t["sub"])
        bar.Controls.Add(title)
        self.title = title

        btns = WinForms.FlowLayoutPanel()
        btns.Dock = WinForms.DockStyle.Right
        btns.Width = 5 * 32 + 6
        btns.FlowDirection = WinForms.FlowDirection.LeftToRight
        btns.WrapContents = False
        btns.Padding = WinForms.Padding(0, 4, 6, 0)
        btns.BackColor = C(t["bar"])
        bar.Controls.Add(btns)
        self.btnbar = btns

        specs = [
            ("⛶", "还原比例与位置", self.reset_geometry),
            ("☾", "夜间 / 日间模式", self.toggle_theme),
            ("⚙", "快捷键设置", self.open_hotkey_dialog),
            ("—", "收起浮窗", self.hide),
            ("×", "关闭到托盘", self.hide),
        ]
        self.theme_btn = None
        for text, tip, cb in specs:
            b = WinForms.Button()
            b.Text = text
            b.Size = Size(30, 28)
            b.Margin = WinForms.Padding(1, 0, 1, 0)
            b.FlatStyle = WinForms.FlatStyle.Flat
            b.FlatAppearance.BorderSize = 0
            b.FlatAppearance.BorderColor = C(t["bar"])
            b.UseVisualStyleBackColor = False
            b.ForeColor = C(t["text"])
            b.BackColor = C(t["bar"])
            b.Font = Font("Segoe UI Symbol", 11)
            b.Cursor = WinForms.Cursors.Hand
            tip_obj = WinForms.ToolTip()
            tip_obj.SetToolTip(b, tip)
            self._keep.append(tip_obj)
            b.Click += self.hold(lambda s, a, c=cb: c())
            btns.Controls.Add(b)
            if text in ("☾", "☀"):
                self.theme_btn = b
        self.update_theme_btn()

        # （原先的左侧网页栏已取消：站点切换移到顶部按钮的下拉菜单里，
        #   网页区域因此不再被挤占宽度）

        # ---- 主区 WebView2 ----
        wv = WebView2()
        props = CoreWebView2CreationProperties()
        persistent = bool(self.cfg.get("persistent_profile", True))
        if persistent:
            props.UserDataFolder = PROFILE_DIR
        else:
            props.UserDataFolder = os.path.join(
                os.environ.get("TEMP", APP_DIR), "ds_popup_tmp_profile"
            )
        try:
            props.set_IsInPrivateModeEnabled(bool(not persistent))
        except Exception:
            pass
        wargs = (self.cfg.get("webview_args") or "").strip()
        if wargs:
            props.AdditionalBrowserArguments = wargs
        wv.CreationProperties = props
        wv.Dock = WinForms.DockStyle.Fill
        f.Controls.Add(wv)
        self.wv = wv

        # WebView2 是原生子窗口，永远画在最上层。工具栏是自绘控件，
        # 一旦两者区域重叠，工具栏底部会被啃掉一条。这里显式把网页区
        # 限制在工具栏下方，并在窗口尺寸变化时重新校正。
        self._layout_webview()

        # ---- 右下角缩放把手 ----
        grip = WinForms.Panel()
        grip.Size = Size(18, 18)
        grip.BackColor = C(t["grip"])
        grip.Cursor = WinForms.Cursors.SizeNWSE
        f.Controls.Add(grip)
        grip.BringToFront()
        self.grip = grip
        grip.MouseDown += self.on_grip_down
        grip.MouseMove += self.on_grip_move
        grip.MouseUp += self.on_grip_up

        # ---- 事件绑定 ----
        bar.MouseDown += self.on_bar_down
        title.MouseDown += self.on_bar_down
        wv.CoreWebView2InitializationCompleted += self.on_wv_ready
        f.Resize += self.on_resize
        f.FormClosing += self.on_closing
        f.Move += self.on_move

        timer = WinForms.Timer()
        timer.Interval = 1200
        timer.Tick += self.on_tick
        timer.Start()
        self._keep.append(timer)

        self.apply_geometry()
        self.apply_theme_colors()

    def dump_controls(self):
        def walk(ctrl, depth=0):
            for c in ctrl.Controls:
                try:
                    log("%s%s bounds=%s bg=%s text=%r" % (
                        "  " * depth, type(c).__name__, c.Bounds, c.BackColor,
                        (getattr(c, "Text", "") or "")[:20]))
                except Exception as e:
                    log("%s  ? %s" % ("  " * depth, e))
                walk(c, depth + 1)

        log("---- 控件树 ----")
        walk(self.form)
        log("---- 控件树结束 ----")

    # ---------- 站点栏 ----------
    def active_site(self):
        sites = self.cfg.get("sites") or []
        if not sites:
            return {"name": "DeepSeek", "url": "https://chat.deepseek.com"}
        idx = max(0, min(int(self.cfg.get("active", 0)), len(sites) - 1))
        return sites[idx]

    def update_theme_btn(self):
        """主题按钮显示当前模式：☾ = 夜间，☀ = 日间"""
        if self.theme_btn is None:
            return
        try:
            self.theme_btn.Text = "☾" if self.cfg.get("theme") == "dark" else "☀"
        except Exception:
            pass

    def update_site_button(self):
        """刷新顶部站点按钮的文字与配色"""
        try:
            self.site_btn.Text = "%s ▾" % site_short(self.active_site())
            self.site_btn.ForeColor = C(self.theme["text"])
            self.site_btn.BackColor = C(self.theme["bar"])
        except Exception:
            pass

    def show_site_menu(self):
        """站点切换 / 增删菜单（挂在顶部按钮上，不占用网页宽度）"""
        t = self.theme
        menu = WinForms.ContextMenuStrip()
        menu.BackColor = C(t["bar"])
        menu.ForeColor = C(t["text"])
        menu.Font = Font("Microsoft YaHei UI", 9)
        sites = self.cfg.get("sites") or []
        active = max(0, min(int(self.cfg.get("active", 0)), max(0, len(sites) - 1)))

        for i, site in enumerate(sites):
            mark = "● " if i == active else "    "
            item = WinForms.ToolStripMenuItem("%s%s" % (mark, site.get("name") or "?"))
            item.ForeColor = C(t["accent"] if i == active else t["text"])
            item.Click += self.hold(lambda s, a, idx=i: self.switch_site(idx))
            menu.Items.Add(item)

        menu.Items.Add(WinForms.ToolStripSeparator())

        add_item = WinForms.ToolStripMenuItem("添加网址…")
        add_item.ForeColor = C(t["text"])
        add_item.Click += self.hold(lambda s, a: self.add_site())
        menu.Items.Add(add_item)

        del_item = WinForms.ToolStripMenuItem("删除当前网址")
        del_item.ForeColor = C(t["text"])
        del_item.Enabled = len(sites) > 1
        del_item.Click += self.hold(lambda s, a: self.remove_site(int(self.cfg.get("active", 0))))
        menu.Items.Add(del_item)

        self._keep.append(menu)
        try:
            menu.Show(self.site_btn, Point(0, self.site_btn.Height))
        except Exception as e:
            log("显示站点菜单失败: %s" % e)

    def switch_site(self, idx):
        sites = self.cfg.get("sites") or []
        if not (0 <= idx < len(sites)):
            return
        self.cfg["active"] = idx
        save_config(self.cfg)
        self.update_site_button()
        self.title.Text = ""
        self.navigate(sites[idx].get("url", ""))
        log("切换站点 -> %s" % sites[idx].get("name"))

    def add_site(self):
        try:
            res = SiteDialog.show(self.form, self.theme)
        except Exception as e:
            log("添加网址窗口出错: %s" % e)
            WinForms.MessageBox.Show("打开窗口失败：%s" % e, "错误")
            return
        if not res.get("ok"):
            return
        self.cfg.setdefault("sites", []).append({"name": res["name"], "url": res["url"]})
        save_config(self.cfg)
        self.update_site_button()
        log("添加站点: %s %s" % (res["name"], res["url"]))

    def remove_site(self, idx):
        sites = self.cfg.get("sites") or []
        if len(sites) <= 1:
            WinForms.MessageBox.Show("至少保留一个网址", "提示")
            return
        if WinForms.MessageBox.Show(
            "确定删除「%s」？" % sites[idx].get("name"), "删除网址",
            WinForms.MessageBoxButtons.OKCancel,
        ) != WinForms.DialogResult.OK:
            return
        sites.pop(idx)
        self.cfg["active"] = max(0, min(int(self.cfg.get("active", 0)), len(sites) - 1))
        save_config(self.cfg)
        self.update_site_button()
        self.title.Text = ""
        log("删除站点: %s" % idx)

    # ---------- 几何 ----------
    def apply_geometry(self):
        w = self.cfg.get("window") or {}
        area = WinForms.Screen.PrimaryScreen.WorkingArea
        width = int(w.get("width") or DEFAULT_W)
        height = int(w.get("height") or DEFAULT_H)
        x, y = w.get("x"), w.get("y")
        if x is None or y is None:
            x = area.Right - width - 16
            y = area.Bottom - height - 16
        self.form.Size = Size(width, height)
        self.form.Location = Point(int(x), int(y))
        self.reposition_grip()

    def reposition_grip(self):
        if self.form is None or self.grip is None:
            return
        cs = self.form.ClientSize
        self.grip.Location = Point(max(0, cs.Width - 18), max(0, cs.Height - 18))

    def reset_geometry(self):
        self.cfg["window"] = {"width": DEFAULT_W, "height": DEFAULT_H, "x": None, "y": None}
        self.apply_geometry()
        save_config(self.cfg)
        log("已还原默认比例")

    def _layout_webview(self):
        """把网页区固定在工具栏下方（不依赖 Dock，避免与工具栏重叠）"""
        if self.form is None or self.wv is None:
            return
        try:
            cs = self.form.ClientSize
            top = self.bar.Height if self.bar is not None else 0
            self.wv.Dock = getattr(WinForms.DockStyle, 'None')
            self.wv.Location = Point(0, top)
            self.wv.Size = Size(max(1, cs.Width), max(1, cs.Height - top))
        except Exception as e:
            log("布局网页区失败: %s" % e)

    def on_resize(self, s, a):
        self.reposition_grip()
        self._layout_webview()

    def on_move(self, s, a):
        self.reposition_grip()

    def on_tick(self, s, a):
        if self.form is None:
            return
        geom = (int(self.form.Width), int(self.form.Height),
                int(self.form.Location.X), int(self.form.Location.Y))
        if self._last_geom is None:
            self._last_geom = geom
            return
        if geom != self._last_geom:
            self._last_geom = geom
            self.cfg["window"] = {"width": geom[0], "height": geom[1],
                                 "x": geom[2], "y": geom[3]}
            save_config(self.cfg)

    # ---------- 拖动 / 缩放 ----------
    def on_bar_down(self, sender, args):
        if args.Button != WinForms.MouseButtons.Left:
            return
        try:
            hwnd = int(self.form.Handle.ToInt64())
            _u32.ReleaseCapture()
            _u32.SendMessageW(ctypes.c_void_p(hwnd), WM_NCLBUTTONDOWN,
                              ctypes.c_void_p(HTCAPTION), ctypes.c_void_p(0))
        except Exception as e:
            log("拖动窗口失败: %s" % e)

    def on_grip_down(self, sender, args):
        if args.Button != WinForms.MouseButtons.Left:
            return
        self._resizing = True
        self._rz_mouse = WinForms.Cursor.Position
        self._rz_size = self.form.Size
        self.grip.Capture = True

    def on_grip_move(self, sender, args):
        if not self._resizing:
            return
        cur = WinForms.Cursor.Position
        dw = cur.X - self._rz_mouse.X
        dh = cur.Y - self._rz_mouse.Y
        self.form.Size = Size(max(MIN_W, self._rz_size.Width + dw),
                              max(MIN_H, self._rz_size.Height + dh))

    def on_grip_up(self, sender, args):
        if not self._resizing:
            return
        self._resizing = False
        self.grip.Capture = False
        self.on_tick(None, None)

    # ---------- WebView2 ----------
    def on_wv_ready(self, sender, args):
        if not args.IsSuccess:
            log("WebView2 初始化失败: %s" % args.InitializationException)
            return
        self.core = self.wv.CoreWebView2
        try:
            self.core.Settings.AreDevToolsEnabled = False
            self.core.Settings.IsStatusBarEnabled = False
            self.core.Settings.AreDefaultContextMenusEnabled = True
            self.core.Settings.IsZoomControlEnabled = True
        except Exception as e:
            log("WebView2 设置项失败: %s" % e)
        self.core.NavigationCompleted += self.on_nav_done
        self.core.ProcessFailed += self.on_proc_failed
        self.apply_theme_to_page()
        log("WebView2 就绪，运行时 %s" % self.core.Environment.BrowserVersionString)
        self.ready.set()
        self.navigate(self.active_site().get("url", ""))

    def navigate(self, url):
        if not url:
            return
        self._nav_url = url
        self._nav_fails = 0
        if self.core is None:
            return
        try:
            self.title.Text = "%s · 加载中…" % self.active_site().get("name", "")
            self.core.Navigate(url)
            log("导航 -> %s" % url)
        except Exception as e:
            log("导航失败: %s" % e)

    def reload_page(self):
        self.ui(lambda: self.navigate(self.active_site().get("url", "")))

    def on_nav_done(self, sender, args):
        name = self.active_site().get("name", "")
        if args.IsSuccess:
            self._nav_fails = 0
            self.title.Text = name
            log("加载完成: %s" % name)
            if self.visible:
                self.focus_input(delay=0.6)
            return
        self._nav_fails += 1
        log("加载失败(%d/%d): %s" % (self._nav_fails, 6, args.WebErrorStatus))
        if self._nav_fails <= 6:
            url = self._nav_url

            def retry():
                time.sleep(1.5)
                self.ui(lambda: self.navigate(url))

            threading.Thread(target=retry, daemon=True).start()
        else:
            self.title.Text = "%s · 加载失败" % name

    def on_proc_failed(self, sender, args):
        log("!! WebView2 进程异常: %s" % args.ProcessFailedKind)

    # ---------- 主题 ----------
    def toggle_theme(self):
        self.cfg["theme"] = "light" if self.cfg.get("theme") == "dark" else "dark"
        self.theme = THEMES[self.cfg["theme"]]
        save_config(self.cfg)
        self.apply_theme_colors()
        self.apply_theme_to_page()
        log("切换主题 -> %s" % self.cfg["theme"])

    def apply_theme_colors(self):
        t = self.theme
        self.form.BackColor = C(t["bg"])
        self.form.ForeColor = C(t["text"])
        self.bar.BackColor = C(t["bar"])
        self.title.ForeColor = C(t["sub"])
        self.grip.BackColor = C(t["grip"])
        for c in self.btnbar.Controls:
            c.BackColor = C(t["bar"])
            c.ForeColor = C(t["text"])
        if self.theme_btn is not None:
            self.theme_btn.BackColor = C(t["bar"])
            self.theme_btn.ForeColor = C(t["text"])
        self.update_theme_btn()
        self.update_site_button()

    def apply_theme_to_page(self):
        if self.core is None:
            return
        try:
            dark = self.theme["scheme"] == "Dark"
            self.core.Profile.PreferredColorScheme = (
                CoreWebView2PreferredColorScheme.Dark if dark
                else CoreWebView2PreferredColorScheme.Light
            )
        except Exception as e:
            log("页面配色设置失败（可忽略）: %s" % e)

    # ---------- 显示控制 ----------
    def show(self):
        def _do():
            self.form.Show()
            self.form.Activate()
            self.form.TopMost = bool(self.cfg.get("always_on_top", True))
            self.visible = True
        self.ui(_do)
        self.focus_input(delay=0.25)

    def hide(self):
        def _do():
            self.form.Hide()
            self.visible = False
        self.ui(_do)

    def toggle(self):
        if self.visible:
            self.hide()
        else:
            self.show()

    def focus_window(self):
        """显示并置于前台（供启动快捷键唤起）"""
        def _do():
            try:
                self.form.Show()
                if self.form.WindowState == WinForms.FormWindowState.Minimized:
                    self.form.WindowState = WinForms.FormWindowState.Normal
                self.form.TopMost = bool(self.cfg.get("always_on_top", True))
                self.form.Activate()
                self.form.BringToFront()
                self.visible = True
            except Exception as e:
                log("唤起窗口失败: %s" % e)
        self.ui(_do)
        self.focus_input(delay=0.3)

    def focus_input(self, delay=0.0):
        """把系统焦点交给网页，并把光标落到输入框（唤起后可直接打字）"""
        def _do():
            try:
                self.wv.Focus()
                self.form.ActiveControl = self.wv
            except Exception as e:
                log("聚焦网页控件失败: %s" % e)
            if self.core is not None:
                try:
                    def _cb(t):
                        try:
                            log("聚焦输入框: %s" % t.Result)
                        except Exception:
                            pass

                    self.core.ExecuteScriptAsync(FOCUS_INPUT_JS).ContinueWith(
                        Action[Task[String]](_cb))
                except Exception as e:
                    log("聚焦输入框失败: %s" % e)

        if delay and delay > 0:
            def later():
                time.sleep(delay)
                self.ui(_do)

            threading.Thread(target=later, daemon=True).start()
        else:
            self.ui(_do)

    def quit(self):
        if self.exited:
            return
        self.exited = True
        log("正在退出…")
        save_config(self.cfg)
        try:
            if keyboard is not None:
                keyboard.unhook_all()
        except Exception:
            pass
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass

        def _hard_exit():
            # Application.Exit() 会被 GUI/键盘钩子线程拖住，直接终止进程最可靠
            time.sleep(0.5)
            os._exit(0)

        threading.Thread(target=_hard_exit, daemon=True).start()

    def on_closing(self, sender, args):
        args.Cancel = True
        self.hide()

    def mark_not_ready(self):
        try:
            self.title.Text = "内核未就绪 · 重开程序可重试"
        except Exception:
            pass

    def _ensure_core(self, attempt):
        try:
            self.wv.EnsureCoreWebView2Async(None)
        except Exception as e:
            log("EnsureCoreWebView2Async 异常(%d): %s" % (attempt, e))

    def start_webview(self, attempt=1):
        """主动初始化 WebView2（必须走 UI 线程）；卡住时重试几次"""
        self.ui(lambda: self._ensure_core(attempt))
        if self.ready.wait(15):
            return True
        if attempt < 3:
            log("WebView2 %d 秒未就绪，重试第 %d 次" % (15 * attempt, attempt + 1))
            return self.start_webview(attempt + 1)
        log("WebView2 多次未就绪：界面与快捷键仍可用")
        self.ui(self.mark_not_ready)
        return False

    # ---------- 快捷键 ----------
    def open_hotkey_dialog(self):
        old_hk = dict(self.cfg.get("hotkeys") or {})
        old_launch = self.cfg.get("launch_hotkey") or ""
        old_auto = bool(self.cfg.get("autostart"))
        try:
            res = HotkeyDialog.show(self.form, self.theme, self.cfg)
        except Exception as e:
            log("设置窗口出错: %s" % e)
            WinForms.MessageBox.Show("打开设置窗口失败：%s" % e, "错误")
            return
        if not res.get("saved"):
            return
        self.cfg["hotkeys"] = res["hotkeys"]
        self.cfg["launch_hotkey"] = res["launch_hotkey"]
        self.cfg["autostart"] = res["autostart"]
        save_config(self.cfg)
        apply_shortcuts(self.cfg)
        register_hotkeys(self)
        if (self.cfg["hotkeys"] != old_hk or self.cfg["autostart"] != old_auto
                or self.cfg["launch_hotkey"] != old_launch):
            log("设置已更新: %s / 启动键=%s / 开机自启=%s"
                % (self.cfg["hotkeys"], self.cfg["launch_hotkey"],
                   self.cfg["autostart"]))


# ---------------------------------------------------------------- 托盘
def run_tray(app):
    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception as e:
        log("托盘不可用: %s" % e)
        return
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    brand = (77, 133, 224, 255)
    d.rounded_rectangle([4, 6, 60, 44], radius=12, fill=brand)
    d.polygon([(16, 40), (14, 58), (32, 43)], fill=brand)
    for x in (20, 30, 40):
        d.ellipse([x, 20, x + 6, 26], fill=(255, 255, 255, 255))
    menu = pystray.Menu(
        pystray.MenuItem("显示 / 收起", lambda icon, item: app.toggle(), default=True),
        pystray.MenuItem("退出", lambda icon, item: app.quit()),
    )
    icon = pystray.Icon("DeepSeekPopup", img, "DeepSeek 浮窗", menu)
    app.tray = icon
    try:
        icon.run()
    except Exception as e:
        log("托盘退出: %s" % e)


# ---------------------------------------------------------------- 快捷键
def register_hotkeys(app):
    """注册程序运行时的全局快捷键（启动键由快捷方式负责，不在这里）"""
    if keyboard is None:
        log("未安装 keyboard 库，全局快捷键不可用")
        return
    hk = app.cfg.get("hotkeys") or {}
    try:
        keyboard.unhook_all_hotkeys()
    except Exception:
        pass
    try:
        launch = (app.cfg.get("launch_hotkey") or "").strip().lower()
        toggle = (hk.get("toggle") or "").strip().lower()
        # 两把键相同时只让启动快捷方式负责，避免一次按键被两边同时响应
        if hk.get("toggle") and toggle != launch:
            keyboard.add_hotkey(hk["toggle"], app.toggle)
        if hk.get("reload"):
            keyboard.add_hotkey(hk["reload"], app.reload_page)
        if hk.get("quit"):
            keyboard.add_hotkey(hk["quit"], app.quit)

        if hk.get("hide"):
            def on_hide():
                if app.visible and own_window_focused():
                    app.hide()

            keyboard.add_hotkey(hk["hide"], on_hide, suppress=False)
        log("全局快捷键: [%s] 显隐 / [%s] 重载 / [%s] 退出 / [%s] 收起 / [%s] 启动"
            % (hk.get("toggle"), hk.get("reload"), hk.get("quit"),
               hk.get("hide"), app.cfg.get("launch_hotkey")))
    except Exception as e:
        log("快捷键注册失败: %s" % e)


# ---------------------------------------------------------------- 单实例
def try_bind_single_instance():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        s.listen(2)
    except OSError:
        return None
    return s


def notify_existing_instance():
    """已有实例在跑时通知它把窗口显示出来（供启动键唤出）"""
    try:
        c = socket.create_connection(("127.0.0.1", SINGLE_INSTANCE_PORT), timeout=1.0)
        c.sendall(b"show")
        c.close()
        return True
    except Exception:
        return False


def serve_single_instance(guard, app):
    def loop():
        while not app.exited:
            try:
                conn, _ = guard.accept()
            except Exception:
                return
            try:
                data = conn.recv(16)
                conn.close()
            except Exception:
                data = b""
            if b"show" in data or b"toggle" in data:
                log("收到唤起请求，显示窗口")
                app.ui(app.focus_window)

    threading.Thread(target=loop, daemon=True).start()


# ---------------------------------------------------------------- 主流程
def main():
    guard = try_bind_single_instance()
    if guard is None:
        if notify_existing_instance():
            log("浮窗已在运行，已请求唤出窗口")
        else:
            log("浮窗已在运行")
        return

    cfg = load_config()
    save_config(cfg)
    apply_shortcuts(cfg)              # 同步开机自启动 + 启动键
    app = App(cfg)

    def gui():
        app.build()
        WinForms.Application.Run(app.form)

    t = Thread(ThreadStart(gui))
    t.SetApartmentState(ApartmentState.STA)
    t.Start()

    # 先把快捷键和托盘挂上，界面立刻可用，不等内核
    register_hotkeys(app)
    threading.Thread(target=run_tray, args=(app,), daemon=True).start()
    serve_single_instance(guard, app)
    log("浮窗已启动：站点 %d 个，主题 %s"
        % (len(cfg.get("sites") or []), cfg.get("theme")))

    # 初始化 WebView2（卡住会自动重试，期间界面与快捷键已可用）
    app.start_webview()

    while not app.exited:
        time.sleep(0.5)
    log("浮窗已退出")


if __name__ == "__main__":
    main()
