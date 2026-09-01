#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AFA Linux 设置界面（tkinter）。

与后台守护进程（afa.py）独立运行：
  - 读取/编辑 config.json，"保存并应用"后原子写入；
  - 守护进程每 2 秒检查文件变更并热重载（无需重启）；
  - 键位点击后弹捕获窗：按任意键绑定，Esc 取消，Del 清除，也可绑定鼠标侧键；
  - 冲突检测：两个动作绑同一键时标红。

运行：python3 afa_gui.py   （或 ./start.sh gui）
"""

import json
import os
import signal
import subprocess
import sys
import tkinter as tk
from tkinter import ttk, messagebox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import afa  # noqa: E402  复用 load_config / CONFIG_PATH / XHelper

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# -- 主题：深色 + 方舟金（基于 ttk clam，零额外依赖） --

BG = "#17181d"          # 窗口底色
CARD = "#202228"        # 卡片底色
INPUT = "#2b2e37"       # 输入框 / 幽灵按钮底
BORDER = "#353944"      # 控件描边
SEP = "#3b404a"         # 卡片内行分隔线
FG = "#e9ebef"          # 主文字
DIM = "#9aa1ae"         # 次要说明文字
FAINT = "#6d7280"       # 更弱的占位文字
ACCENT = "#f4c04c"      # 方舟金（主按钮 / 焦点 / 未运行提示）
ACCENT_HOVER = "#ffd766"
ACCENT_PRESSED = "#d8a63d"
OK_GREEN = "#5fd08a"
DANGER = "#ff7676"
DANGER_BG = "#45272c"
KEYCAP_BG = "#33373f"
KEYCAP_HOVER = "#3f444e"

FONT_UI = ("Source Han Sans SC", 11)
FONT_BOLD = ("Source Han Sans SC", 11, "bold")
FONT_TITLE = ("Source Han Sans SC", 13, "bold")
FONT_KEYCAP = ("Source Han Mono SC", 11, "bold")


def apply_theme(root):
    """在 clam 基础上定制深色扁平主题。"""
    root.configure(bg=BG)
    style = ttk.Style(root)
    style.theme_use("clam")

    style.configure(".", background=BG, foreground=FG, font=FONT_UI)
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)

    style.configure("TLabel", background=BG, foreground=FG, font=FONT_UI)
    style.configure("Dim.TLabel", background=BG, foreground=DIM)
    style.configure("Card.TLabel", background=CARD, foreground=FG)
    style.configure("CardDim.TLabel", background=CARD, foreground=DIM)
    style.configure("RowTitle.TLabel", background=CARD, foreground=FG, font=FONT_BOLD)
    style.configure("Sidebar.TLabel", background=BG)

    # clam 的立体感来自 lightcolor（默认近白），三个边框色全部压平
    flat = dict(bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)

    # 幽灵按钮（常规操作）
    style.configure("TButton", font=FONT_UI, padding=(14, 7),
                    background=INPUT, foreground=FG, focusthickness=0, **flat)
    style.map("TButton",
              background=[("active", "#3a3f4a"), ("pressed", "#262932")],
              bordercolor=[("active", "#4a5060"), ("pressed", BORDER)],
              lightcolor=[("active", "#3a3f4a"), ("pressed", "#262932")],
              darkcolor=[("active", "#3a3f4a"), ("pressed", "#262932")])
    # 金色主按钮
    style.configure("Accent.TButton", font=FONT_BOLD, padding=(20, 9),
                    background=ACCENT, foreground="#1a1b1f",
                    bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT)
    style.map("Accent.TButton",
              background=[("active", ACCENT_HOVER), ("pressed", ACCENT_PRESSED)],
              bordercolor=[("active", ACCENT_HOVER), ("pressed", ACCENT_PRESSED)],
              lightcolor=[("active", ACCENT_HOVER), ("pressed", ACCENT_PRESSED)],
              darkcolor=[("active", ACCENT_HOVER), ("pressed", ACCENT_PRESSED)])

    # 扁平标签页：选中页与卡片同色，视觉连成一体
    style.configure("TNotebook", background=BG, borderwidth=0, relief="flat",
                    bordercolor=BG, lightcolor=BG, darkcolor=BG)
    style.configure("TNotebook.Tab", font=FONT_UI, padding=(22, 10),
                    background=BG, foreground=DIM, borderwidth=0,
                    bordercolor=BG, lightcolor=BG, darkcolor=BG)
    style.map("TNotebook.Tab",
              padding=[("selected", (22, 10)), ("active", (22, 10))],
              background=[("selected", CARD), ("active", "#23262e")],
              foreground=[("selected", FG), ("active", FG)],
              bordercolor=[("selected", CARD), ("active", "#23262e")],
              lightcolor=[("selected", CARD), ("active", "#23262e")],
              darkcolor=[("selected", CARD), ("active", "#23262e")])

    # 输入控件
    style.configure("TEntry", fieldbackground=INPUT, foreground=FG,
                    insertcolor=FG, padding=6, **flat)
    style.map("TEntry",
              bordercolor=[("focus", ACCENT)],
              lightcolor=[("focus", ACCENT)],
              darkcolor=[("focus", ACCENT)])
    style.configure("TCombobox", fieldbackground=INPUT, foreground=FG,
                    background=INPUT, arrowcolor=FG, padding=6,
                    selectbackground=ACCENT, selectforeground="#1a1b1f", **flat)
    style.map("TCombobox",
              fieldbackground=[("readonly", INPUT), ("focus", INPUT),
                               ("active", INPUT)],
              foreground=[("readonly", FG)],
              background=[("active", "#3a3f4a"), ("pressed", "#3a3f4a")],
              bordercolor=[("focus", ACCENT), ("active", "#4a5060")],
              lightcolor=[("focus", ACCENT), ("active", "#3a3f4a")],
              darkcolor=[("focus", ACCENT), ("active", "#3a3f4a")])
    root.option_add("*TCombobox*Listbox.background", INPUT)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "#1a1b1f")



# 动作元数据（分组、名称、说明），与 afa.py DEFAULT_BINDINGS 对齐
ACTION_META = {
    "press_pause":         ("常规作战", "按下时暂停", "按下时切换暂停（发 ESC）"),
    "release_pause":       ("常规作战", "松开时暂停", "松开时切换暂停，模拟器「点击」手感"),
    "game_speed":          ("常规作战", "切换倍速", "切换游戏倍速（1倍/2倍）"),
    "pause_select":        ("常规作战", "暂停时选中", "暂停时鼠标移到单位上点击选中"),
    "skill":               ("常规作战", "技能", "点击技能键"),
    "retreat":             ("常规作战", "撤退", "点击撤退键"),
    "frame_16ms":          ("常规作战", "前进 16ms", "暂停时使用，游戏时间前进 16ms"),
    "frame_33ms":          ("常规作战", "前进 33ms", "暂停时使用，游戏时间前进 33ms"),
    "frame_166ms":         ("常规作战", "前进 166ms", "暂停时使用，游戏时间前进 166ms"),
    "one_click_skill":     ("常规作战", "一键技能", "鼠标移到单位上按下即选中并开技能"),
    "one_click_retreat":   ("常规作战", "一键撤退", "鼠标移到单位上按下即选中并撤退"),
    "pause_skill":         ("常规作战", "暂停技能", "暂停时自动选中并开技能"),
    "pause_retreat":       ("常规作战", "暂停撤退", "暂停时自动选中并撤退"),
    "switch_view":         ("常规作战", "视角切换", "暂停时把摄像头中心切到该单位"),
    "l_button_click":      ("快捷操作", "模拟左键点击", "模拟按下鼠标左键（按住跟随）"),
    "cease_operations":    ("快捷操作", "放弃行动", "放弃当前作战"),
    "skip":                ("快捷操作", "跳过招募动画/剧情", "快速点击右上角跳过按钮"),
    "harvest":             ("快捷操作", "基建快速收取", "点击左下角基建收取按钮"),
    "collect_collectibles": ("快捷操作", "肉鸽收取道具", "点击集成战略的「收下」按钮"),
    "back":                ("快捷操作", "返回上级菜单", "模拟 ESC 返回上一级菜单"),
}

GAME_KEY_META = {
    "changeSpeed":     ("游戏功能键", "变速", "游戏内的倍速快捷键"),
    "releaseSkill":    ("游戏功能键", "技能", "游戏内的技能快捷键"),
    "retreatChar":     ("游戏功能键", "撤退", "游戏内的撤退快捷键"),
    "pauseBattle":     ("游戏功能键", "暂停", "游戏内的暂停快捷键"),
    "battleLeftPopup": ("游戏功能键", "放弃行动", "游戏内的放弃行动快捷键"),
}

MODIFIER_KEYSYMS = {"shift_l", "shift_r", "control_l", "control_r", "alt_l", "alt_r",
                    "meta_l", "meta_r", "super_l", "super_r", "hyper_l", "hyper_r",
                    "caps_lock", "num_lock"}


def keysym_to_config_name(keysym):
    """tkinter event.keysym（字符串，如 'a' / 'F8' / 'space' / 'Escape'）→ config 键名。

    修饰键返回 None（不可单独作为触发键）；无法识别返回 None。
    """
    if not keysym or keysym == "??":
        return None
    if keysym.lower() in MODIFIER_KEYSYMS:
        return None
    special = {"space": "Space", "Escape": "ESC"}
    if keysym in special:
        return special[keysym]
    if len(keysym) == 1 and keysym.isalpha():
        return keysym.lower()
    return keysym  # F8 / Home / Return / KP_1 等，X keysym 体系原生可解析


class CaptureDialog(tk.Toplevel):
    """按键捕获窗：按任意键绑定；Esc 取消；Del 清除；支持鼠标侧键预设。"""

    def __init__(self, master, current):
        super().__init__(master)
        self.title("捕获按键")
        self.resizable(False, False)
        self.result = None
        self.grab_set()
        self.configure(bg=BG)
        frm = ttk.Frame(self, padding=24)
        frm.pack()
        ttk.Label(frm, text="按下任意按键绑定", font=FONT_TITLE,
                  background=BG, foreground=FG).pack(pady=(0, 6))
        ttk.Label(frm, text="Esc 取消 ｜ Del 清除绑定\n鼠标侧键请用下方按钮",
                  style="Dim.TLabel", justify="center").pack()
        side = tk.Frame(frm, bg=BG)
        side.pack(pady=16)
        for label, val in (("绑 XButton1（侧键返回）", "XButton1"),
                           ("绑 XButton2（侧键前进）", "XButton2"),
                           ("清除绑定", "")):
            ttk.Button(side, text=label,
                       command=lambda v=val: self._done(v or None)).pack(
                           side="left", padx=4)
        self.bind("<Key>", self._on_key)
        self.focus_force()

    def _on_key(self, event):
        try:
            if event.keysym == "Escape":
                self._done(None)
            elif event.keysym == "Delete":
                self._done("")
            else:
                name = keysym_to_config_name(event.keysym)
                if name:
                    self._done(name)
                elif event.keysym == "??":
                    self.title("捕获按键 —— 该按键无法识别，请换一个键")
                else:
                    self.title("捕获按键 —— 修饰键不能单独作为触发键")
        except Exception as e:  # 捕获窗永不因单个按键崩溃
            self.title(f"捕获按键 —— 该键无法识别（{e}）")

    def _done(self, value):
        self.result = value
        self.destroy()


class Toggle(tk.Frame):
    """自绘复选框。

    clam 的 indicator 元素在部分 Tk/字体环境下勾选标记会渲染成怪符号，
    这里直接用 Canvas 画方块与 ✓，外观完全跟随主题色。
    """

    def __init__(self, master, text, variable, bg=CARD, **kwargs):
        super().__init__(master, bg=bg, **kwargs)
        self.var = variable
        self._bg = bg
        self._hover = False
        self.cv = tk.Canvas(self, width=24, height=24, bg=bg,
                            highlightthickness=0, cursor="hand2")
        self.cv.pack(side="left")
        self.lbl = tk.Label(self, text=text, bg=bg, fg=FG, font=FONT_UI,
                            cursor="hand2")
        self.lbl.pack(side="left", padx=(10, 0))
        for w in (self, self.cv, self.lbl):
            w.bind("<Button-1>", self._on_click)
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)
        self.var.trace_add("write", lambda *_: self._draw())
        self._draw()

    def _on_click(self, _event=None):
        self.var.set(not self.var.get())

    def _on_enter(self, _event=None):
        self._hover = True
        self._draw()

    def _on_leave(self, _event=None):
        self._hover = False
        self._draw()

    def _draw(self):
        cv = self.cv
        cv.delete("all")
        on = bool(self.var.get())
        if on:
            cv.create_rectangle(3, 3, 21, 21, fill=ACCENT, outline=ACCENT)
            cv.create_line(7, 12, 10.5, 16, fill="#1a1b1f", width=2.4,
                           capstyle="round")
            cv.create_line(10.5, 16, 18, 8, fill="#1a1b1f", width=2.4,
                           capstyle="round")
        else:
            outline = "#5a6170" if self._hover else BORDER
            cv.create_rectangle(3, 3, 21, 21, fill=INPUT, outline=outline)


class App:
    def __init__(self, root):
        self.root = root
        root.title("AFA Linux 设置 —— 明日方舟帧操小助手")
        root.minsize(720, 620)
        root.geometry("+160+80")   # 固定在主屏打开，避免落到副屏负坐标
        self.cfg = afa.load_config()
        self._log_handles = []
        self.key_buttons = {}      # (kind, name) -> tk.Button
        self._build()
        self._refresh_status()
        root.after(3000, self._refresh_status)

    # -- 界面构建 --

    def _make_sidebar_photo(self):
        """右侧美术位：assets/ 目录放了图片（推荐 sidebar.png，也认 png/jpg/gif/webp/bmp）
        就显示在键位表右侧；没有素材或加载失败则维持原布局。多文件取排序第一个。"""
        try:
            from PIL import Image, ImageTk
        except ImportError:
            return None
        assets = os.path.join(BASE_DIR, "assets")
        if not os.path.isdir(assets):
            return None
        files = sorted(f for f in os.listdir(assets)
                       if f.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")))
        if not files:
            return None
        try:
            from PIL import Image
            img = Image.open(os.path.join(assets, files[0])).convert("RGBA")
            img.thumbnail((460, 620), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self._sidebar_refs = [photo]   # 防 GC
            return photo
        except Exception as e:
            print(f"素材加载失败（{files[0]}）：{e}", file=sys.stderr)
            return None

    def _build_sidebar(self, wrap, sidebar):
        if sidebar:
            lbl = ttk.Label(wrap, image=sidebar, style="Sidebar.TLabel", anchor="center")
            lbl.pack(side="right", padx=(0, 18), pady=10)

    def _build(self):
        # 顶部状态条
        top = ttk.Frame(self.root, padding=(18, 12))
        top.pack(fill="x")
        bar = tk.Frame(top, bg=BG)
        bar.pack(side="left")
        self.dot_game = tk.Label(bar, text="●", bg=BG, fg=FAINT, font=FONT_UI)
        self.dot_game.pack(side="left")
        self.lbl_game = tk.Label(bar, text="游戏窗口检测中…", bg=BG, fg=DIM, font=FONT_UI)
        self.lbl_game.pack(side="left", padx=(6, 0))
        tk.Label(bar, text="｜", bg=BG, fg=FAINT, font=FONT_UI).pack(side="left", padx=10)
        self.dot_daemon = tk.Label(bar, text="●", bg=BG, fg=FAINT, font=FONT_UI)
        self.dot_daemon.pack(side="left")
        self.lbl_daemon = tk.Label(bar, text="热键后台检测中…", bg=BG, fg=DIM, font=FONT_UI)
        self.lbl_daemon.pack(side="left", padx=(6, 0))

        ttk.Button(top, text="停止后台", command=self._stop_daemon).pack(side="right")
        ttk.Button(top, text="启动后台", command=self._start_daemon).pack(side="right", padx=8)
        ttk.Button(top, text="刷新", command=self._refresh_status).pack(side="right")

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=18, pady=(0, 6))

        sidebar = self._make_sidebar_photo()

        def new_tab(text):
            wrap = ttk.Frame(nb)
            nb.add(wrap, text=text)
            self._build_sidebar(wrap, sidebar)   # 先占右侧，卡片再填充剩余
            card = ttk.Frame(wrap, style="Card.TFrame", padding=(20, 14))
            card.pack(side="left", fill="both", expand=True)
            card.columnconfigure(1, weight=1)
            return card

        groups = {}
        for name in ACTION_META:
            g = ACTION_META[name][0]
            groups.setdefault(g, []).append(name)
        for g in ("常规作战", "快捷操作"):
            card = new_tab(g)
            for row, name in enumerate(groups[g]):
                self._binding_row(card, row, "binding", name)
        card = new_tab("游戏功能键")
        ttk.Label(card, text="AFA 会自动读游戏内按键；本版请与游戏内设置保持一致（改过游戏键位务必同步）",
                  style="CardDim.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        for row, name in enumerate(GAME_KEY_META):
            self._binding_row(card, row + 1, "game_key", name)
        card = new_tab("延迟与守卫")
        self._delay_tab(card)

        # 底部操作条
        bottom = ttk.Frame(self.root, padding=(18, 8, 18, 14))
        bottom.pack(fill="x")
        ttk.Button(bottom, text="恢复默认键位", command=self._reset_defaults).pack(side="left")
        ttk.Button(bottom, text="从文件重新加载",
                   command=self._reload_from_file).pack(side="left", padx=(10, 0))
        self.save_var = tk.StringVar(value="")
        tk.Label(bottom, textvariable=self.save_var, bg=BG, fg=OK_GREEN,
                 font=FONT_UI).pack(side="right", padx=(0, 16))
        ttk.Button(bottom, text="保存并应用", style="Accent.TButton",
                   command=self._save).pack(side="right")

    def _make_keycap(self, parent):
        """键帽风格按钮：扁平、等宽字体、细描边；hover/按下有反馈。"""
        btn = tk.Button(parent, font=FONT_KEYCAP, relief="flat", bd=0,
                        bg=KEYCAP_BG, fg=FG, activebackground=ACCENT,
                        activeforeground="#1a1b1f", highlightthickness=1,
                        highlightbackground=BORDER, padx=12, pady=5, width=10,
                        cursor="hand2")
        btn._base_bg = KEYCAP_BG
        btn._hover_bg = KEYCAP_HOVER
        btn.bind("<Enter>", lambda e: btn.config(bg=btn._hover_bg))
        btn.bind("<Leave>", lambda e: btn.config(bg=btn._base_bg))
        return btn

    def _binding_row(self, parent, row, kind, name):
        meta = (ACTION_META if kind == "binding" else GAME_KEY_META)[name]
        section, title, desc = meta
        r = row * 2
        ttk.Label(parent, text=title, style="RowTitle.TLabel", width=15).grid(
            row=r, column=0, sticky="w", pady=(10, 9))
        ttk.Label(parent, text=desc, style="CardDim.TLabel").grid(
            row=r, column=1, sticky="w", padx=(4, 12))
        container = tk.Frame(parent, bg=CARD)
        container.grid(row=r, column=2, sticky="e")
        source = self.cfg["bindings"] if kind == "binding" else self.cfg["game_keys"]
        btn = self._make_keycap(container)
        btn.config(text=self._key_label(source.get(name, "")),
                   command=lambda: self._capture(kind, name))
        btn.pack(side="left")
        self.key_buttons[(kind, name)] = btn
        tk.Frame(parent, height=1, bg=SEP).grid(
            row=r + 1, column=0, columnspan=3, sticky="ew")

    def _delay_tab(self, card):
        c = self.cfg
        rows = [
            ("游戏内帧率档位", "frame_rate", ("30", "60", "90", "120", "144", "165", "180", "240+"),
             "与游戏内帧率设置一致，影响暂停态动作时序"),
            ("点击延迟 ms", "click_delay_ms", None, "一键技能/撤退里左键到技能键的间隔（AFA 默认 90）"),
            ("按键按下时长 ms", "key_tap_delay_ms", None, "发游戏功能键时按住的时长（AFA 默认 50）"),
            ("触摸等效点击 ms", "touch_tap_ms", None, "暂停三连击每次点击按下的时长"),
            ("前进16ms 延迟", "frame_skip_16ms_delay", None, "过帧实际暂停时长（默认 16）"),
            ("前进33ms 延迟", "frame_skip_33ms_delay", None, "默认 30，避免一次过两帧"),
            ("前进166ms 延迟", "frame_skip_166ms_delay", None, "默认 165"),
        ]
        self.delay_vars = {}
        for row, (label, key, options, tip) in enumerate(rows):
            ttk.Label(card, text=label, style="Card.TLabel").grid(
                row=row, column=0, sticky="w", pady=8)
            if options:
                var = tk.StringVar(value=str(c[key]))
                box = ttk.Combobox(card, textvariable=var, values=options,
                                   state="readonly", width=10)
                box.grid(row=row, column=1, sticky="w")
                self.delay_vars[key] = var
            else:
                var = tk.StringVar(value=str(c[key]))
                ttk.Entry(card, textvariable=var, width=10).grid(row=row, column=1, sticky="w")
                self.delay_vars[key] = var
            ttk.Label(card, text=tip, style="CardDim.TLabel").grid(
                row=row, column=2, sticky="w", padx=(12, 0))
        base = len(rows)
        self.guard_var = tk.BooleanVar(value=bool(c["in_level_guard"]))
        Toggle(card, text="关卡守卫（不在关卡内时透传触发键，菜单可正常打字）",
               variable=self.guard_var).grid(row=base, column=0, columnspan=3,
                                             sticky="w", pady=(16, 5))
        self.hover_var = tk.BooleanVar(value=bool(c["hover_operate"]))
        Toggle(card, text="鼠标悬停游戏窗口时允许鼠标侧键触发（未持焦点）",
               variable=self.hover_var).grid(row=base + 1, column=0, columnspan=3,
                                             sticky="w", pady=5)
        self.autoexit_var = tk.BooleanVar(value=bool(c.get("auto_exit", True)))
        Toggle(card, text="游戏退出后自动退出后台守护（防反作弊建议保持开启）",
               variable=self.autoexit_var).grid(row=base + 2, column=0, columnspan=3,
                                               sticky="w", pady=5)
        self.title_var = tk.StringVar(value=c["window_title"])
        ttk.Label(card, text="窗口标题匹配", style="Card.TLabel").grid(
            row=base + 3, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(card, textvariable=self.title_var, width=18).grid(
            row=base + 3, column=1, sticky="w", pady=(10, 0))

    # -- 行为 --

    @staticmethod
    def _key_label(name):
        return name if name else "未绑定"

    def _capture(self, kind, name):
        source = self.cfg["bindings"] if kind == "binding" else self.cfg["game_keys"]
        dlg = CaptureDialog(self.root, source.get(name, ""))
        self.root.wait_window(dlg)
        if dlg.result is None:
            return
        source[name] = dlg.result
        self._refresh_key_buttons()

    def _refresh_key_buttons(self):
        conflicts = self._conflicts()
        for (kind, name), btn in self.key_buttons.items():
            source = self.cfg["bindings"] if kind == "binding" else self.cfg["game_keys"]
            val = source.get(name, "")
            if not val:
                btn.config(text="未绑定", bg=CARD, fg=FAINT,
                           highlightbackground=BORDER)
                btn._base_bg, btn._hover_bg = CARD, INPUT
            elif val.lower() in conflicts:
                btn.config(text=val, bg=DANGER_BG, fg=DANGER,
                           highlightbackground="#6e3a40")
                btn._base_bg, btn._hover_bg = DANGER_BG, "#572e34"
            else:
                btn.config(text=val, bg=KEYCAP_BG, fg=FG,
                           highlightbackground="#464b56")
                btn._base_bg, btn._hover_bg = KEYCAP_BG, KEYCAP_HOVER

    def _conflicts(self):
        """返回 bindings 内出现 ≥2 次的键名（小写）。

        只查 bindings：触发键与 game_keys 是不同命名空间（AFA 默认就有
        触发键 f + 游戏功能键"变速 f"并存），不算冲突。
        """
        seen = {}
        for v in self.cfg["bindings"].values():
            if v:
                seen[v.lower()] = seen.get(v.lower(), 0) + 1
        return {k for k, n in seen.items() if n >= 2}

    def _daemon_status(self):
        """扫描 /proc 查 afa.py 守护进程 → (running, pid)。

        不用 pgrep 正则：daemon 以绝对路径启动时命令行是
        'python3 /path/afa.py'，匹配 'python3 afa.py' 会漏判。
        """
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read().decode("utf-8", "ignore").replace("\x00", " ")
            except OSError:
                continue
            if "afa_gui" in cmd:
                continue
            if "afa.py" in cmd and "python" in cmd:
                return True, int(pid)
        return False, None

    def _start_daemon(self):
        running, pid = self._daemon_status()
        if running:
            self.save_var.set(f"后台已在运行（pid {pid}）")
            return
        os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
        logf = open(os.path.join(BASE_DIR, "logs", "afa.log"), "a")
        self._log_handles.append(logf)   # 防 GC 关闭句柄导致守护 stdout 断裂
        subprocess.Popen([sys.executable, os.path.join(BASE_DIR, "afa.py")],
                         stdout=logf, stderr=logf, start_new_session=True)
        self.save_var.set("后台守护已启动，日志见 logs/afa.log")

    def _stop_daemon(self):
        running, pid = self._daemon_status()
        if not running:
            self.save_var.set("后台未在运行")
            return
        os.kill(pid, signal.SIGTERM)
        self.save_var.set(f"已发送退出信号（pid {pid}）")

    def _refresh_status(self):
        try:
            xh = afa.XHelper({"window_title": self.cfg["window_title"]})
            xid = xh.find_game_window()
            rect = xh.client_rect(xid) if xid else None
            if xid:
                focused = xh.focused_xid() == xid
                self.dot_game.config(fg=OK_GREEN)
                self.lbl_game.config(
                    text=f"游戏窗口 0x{xid:x} · {rect[2]}×{rect[3]}{' · 前台' if focused else ''}",
                    fg=FG)
            else:
                self.dot_game.config(fg=FAINT)
                self.lbl_game.config(text="未找到游戏窗口", fg=DIM)
        except Exception as e:
            self.dot_game.config(fg=DANGER)
            self.lbl_game.config(text=f"窗口检测失败：{e}", fg=DANGER)
        running, pid = self._daemon_status()
        if running:
            self.dot_daemon.config(fg=OK_GREEN)
            self.lbl_daemon.config(text=f"热键后台运行中 · pid {pid}", fg=FG)
        else:
            self.dot_daemon.config(fg=ACCENT)
            self.lbl_daemon.config(text="热键后台未运行", fg=DIM)
        self.root.after(3000, self._refresh_status)

    def _reset_defaults(self):
        if not messagebox.askyesno("恢复默认", "把所有键位恢复为 AFA 默认？延迟设置不变。"):
            return
        self.cfg["bindings"] = dict(afa.DEFAULT_BINDINGS)
        self.cfg["game_keys"] = dict(afa.DEFAULT_GAME_KEYS)
        self._refresh_key_buttons()

    def _reload_from_file(self):
        self.cfg = afa.load_config()
        self._refresh_key_buttons()
        self.save_var.set("已从文件重新加载")

    def _save(self):
        # 收集延迟/开关
        for key, var in self.delay_vars.items():
            val = var.get().strip()
            if key == "frame_rate":
                self.cfg[key] = val
                continue
            try:
                self.cfg[key] = int(val)
            except ValueError:
                messagebox.showerror("无效数值", f"「{key}」不是整数：{val!r}")
                return
        self.cfg["in_level_guard"] = bool(self.guard_var.get())
        self.cfg["hover_operate"] = bool(self.hover_var.get())
        self.cfg["auto_exit"] = bool(self.autoexit_var.get())
        self.cfg["window_title"] = self.title_var.get().strip() or "明日方舟"
        if self._conflicts():
            if not messagebox.askyesno("存在键位冲突",
                                       "两个动作绑定了同一个键（红色），冲突的键将只有后一个生效。仍要保存？"):
                return
        # 原子写入
        tmp = afa.CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, afa.CONFIG_PATH)
        self.save_var.set("已保存，后台 2 秒内自动生效")


def main():
    root = tk.Tk()
    apply_theme(root)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
