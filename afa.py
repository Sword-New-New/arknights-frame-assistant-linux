#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AFA Linux —— 明日方舟帧操小助手（Linux / Proton 移植版）

动作时序与功能规格移植自 CloudTracey/arknights-frame-assistant（GPL-3.0-only），
本移植版为派生作品，同样以 GPL-3.0-only 发布。

实现原理（与原版 AFA 的对应关系）：
  - 原版用 AHK 的 #HotIf WinActive("ahk_exe Arknights.exe") 把热键限定在游戏进程；
    本版用 X11 被动抓取（XGrabKey / XGrabButton）把触发键挂到游戏窗口上：
    键盘触发仅在游戏窗口（或其子窗口）持有输入焦点时激活，鼠标侧键在鼠标悬停于
    游戏窗口时激活；物理触发键被 X 抓取吞掉、由动作逻辑按精确时序重放，
    与 AFA 的"拦截+重放"语义一致。
  - 输入注入走 XTEST（与 xdotool 相同路径）；Wine/Proton 将 X 事件翻译为 Win32
    输入事件送达游戏。注意 XTEST 事件会像真实按键一样触发被动抓取，因此注入
    "已被自己抓取的键"前必须临时解除抓取，注入完毕后恢复。
  - 帧操延时（16/30/165ms 等）用 time.perf_counter 忙等实现，精度 <1ms，
    对应 AFA 的 USleep（QueryPerformanceCounter 忙等）。
  - 关卡守卫移植 LevelDetector：对 3 个关卡内专属对象做颜色采样，命中 ≥2 判定
    在关卡内；不在关卡内时触发的游戏键被原样透传（保证菜单/输入框可用）。

仅依赖：Python 3.8+ 与 python-xlib（纯 Python）。X11 会话（XWayland 亦可）。
"""

import argparse
import collections
import json
import logging
import os
import queue
import signal
import sys
import threading
import time

from Xlib import X, XK, display, error
from Xlib.ext import xtest

log = logging.getLogger("afa")

# ---------------------------------------------------------------------------
# 与 AFA 对齐的常量
# ---------------------------------------------------------------------------

# AFA TimingService：游戏帧率档位 → CurrentDelay（暂停态动作里 CurrentDelay*1.5 用）
FRAME_DELAY_MS = {"30": 34, "60": 17, "90": 12, "120": 9, "144": 8, "165": 7, "180": 6, "240+": 5}

# AFA PauseButtonPosition*：暂停按钮左半/右半等 UI 元素的客户区比例坐标
POS_PAUSE_LEFT = (0.9400, 0.0700)
POS_PAUSE_RIGHT = (0.9650, 0.0700)
POS_SKIP = (0.959765, 0.05)
POS_HARVEST = (0.1297, 0.9527)
POS_COLLECT = (0.1104, 0.7250)

# AFA level_detector.ahk：关卡内专属对象（比例区域 + 颜色/容差），命中≥2 判定在关卡内
LEVEL_OBJECTS = [
    {
        "colors": [(0xFFFFFF, 2), (0x9B9B9B, 2)],
        "lx": 0.9300, "rx": 0.9420, "uy": 0.7833, "dy": 0.8465,
    },
    {
        "colors": [(0x868686, 5), (0x8C8C8C, 5), (0xB72518, 5), (0xBF2719, 5),
                   (0xD0CF67, 5), (0xD9D86B, 5), (0x555555, 5), (0x515151, 5),
                   (0x74180F, 5), (0x6F160F, 5), (0x848341, 5), (0x7E7E3F, 5)],
        "lx": 0.0531, "rx": 0.0535, "uy": 0.0299, "dy": 0.0750,
    },
    {
        "colors": [(0xFFFFFF, 2), (0xF5F5F5, 2)],
        "lx": 0.9297, "rx": 0.9453, "uy": 0.0590, "dy": 0.0590,
    },
]

# 默认游戏功能键（AFA GameKeys._Defaults；正式运行会读 AFA 同款注册表值可后续扩展）
DEFAULT_GAME_KEYS = {
    "changeSpeed": "f",
    "releaseSkill": "e",
    "retreatChar": "q",
    "pauseBattle": "space",
    "battleLeftPopup": "v",
}

# 默认触发热键（AFA hotkey_schema.ahk defaultKey；空串 = 未绑定）
DEFAULT_BINDINGS = {
    "press_pause": "f",
    "release_pause": "space",
    "game_speed": "d",
    "pause_select": "w",
    "skill": "s",
    "retreat": "a",
    "frame_16ms": "",
    "frame_33ms": "r",
    "frame_166ms": "t",
    "one_click_skill": "e",
    "one_click_retreat": "q",
    "pause_skill": "XButton2",
    "pause_retreat": "XButton1",
    "switch_view": "",
    "l_button_click": "",
    "cease_operations": "",
    "skip": "",
    "harvest": "",
    "collect_collectibles": "",
    "back": "",
}

# 需要关卡守卫的战斗类动作（对应 AFA 所有调用 GuardInLevel 的常规作战动作）
GUARDED_ACTIONS = {
    "press_pause", "release_pause", "game_speed", "pause_select", "skill",
    "retreat", "frame_16ms", "frame_33ms", "frame_166ms",
    "one_click_skill", "one_click_retreat", "pause_skill", "pause_retreat",
    "switch_view",
}

DEFAULT_CONFIG = {
    "window_title": "明日方舟",
    "game_process": "Arknights.exe",   # 窗口按此进程的 _NET_WM_PID 过滤，比标题可靠
    "frame_rate": "120",          # 游戏内帧率设置，与 AFA 的 Frame 档位一致
    "click_delay_ms": 90,         # AFA ClickDelay 默认 90
    "key_tap_delay_ms": 50,       # AFA GameKeys.Tap 默认按下时长
    "touch_tap_ms": 30,           # 触摸等效点击的按下时长（AFA 触摸注入近零，此处保守取 30）
    "frame_skip_16ms_delay": 16,
    "frame_skip_33ms_delay": 30,  # AFA 默认 30（标注"避免一次过两帧"）
    "frame_skip_166ms_delay": 165,
    "in_level_guard": True,       # 关卡守卫：不在关卡内时透传游戏键
    "auto_exit": True,            # 游戏退出后自动退出守护（AFA AutoExit，防反作弊建议保持开启）
    "hover_operate": True,        # 鼠标悬停游戏窗口（未持焦点）时允许鼠标侧键触发
    "window_poll_seconds": 2.0,
    "guard_poll_ms": 333,
    "log_level": "INFO",
    "game_keys": dict(DEFAULT_GAME_KEYS),
    "bindings": dict(DEFAULT_BINDINGS),
}

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# XButton（AHK 名）→ X 鼠标按钮编号：XButton1=返回(8)、XButton2=前进(9)
MOUSE_BUTTONS = {"XButton1": 8, "XButton2": 9}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user = json.load(f)
            for k, v in user.items():
                if k in ("game_keys", "bindings") and isinstance(v, dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            log.warning("读取 config.json 失败（%s），使用默认配置", e)
    else:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        log.info("已生成默认配置 %s（键位/延迟均可改）", CONFIG_PATH)
    return cfg


# ---------------------------------------------------------------------------
# 精确延时（对应 AFA USleep：先睡后忙等）
# ---------------------------------------------------------------------------

def msleep(ms: float):
    if ms <= 0:
        return
    end = time.perf_counter() + ms / 1000.0
    if ms > 3:
        time.sleep((ms - 2) / 1000.0)
    while time.perf_counter() < end:
        pass


# ---------------------------------------------------------------------------
# X11 封装
# ---------------------------------------------------------------------------

def keysym_keycode(d: display.Display, name: str):
    """AHK 风格键名 → X keycode；无法解析返回 0。

    keysym 名大小写敏感（F8 ≠ f8），依次尝试原名/小写/大写。
    """
    n = (name or "").strip()
    if not n or n in MOUSE_BUTTONS:
        return 0
    aliases = {"ESC": "Escape", "Space": "space"}
    base = aliases.get(n, n)
    for cand in dict.fromkeys((base, base.lower(), base.upper())):
        ks = XK.string_to_keysym(cand)
        if ks:
            kc = d.keysym_to_keycode(ks)
            if kc:
                return kc
    return 0


class XHelper:
    """控制连接封装：窗口查询、注入、像素采样。内部加锁，可跨线程使用。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.d = display.Display()
        self.lock = threading.RLock()
        self.root = self.d.screen().root

    # -- 窗口 --

    def game_pids(self):
        """游戏进程的 Linux pid 集合（wine 进程 cmdline 含游戏 exe 名）。"""
        exe = self.cfg.get("game_process") or ""
        pids = set()
        if not exe:
            return pids
        for p in os.listdir("/proc"):
            if not p.isdigit():
                continue
            try:
                with open(f"/proc/{p}/cmdline", "rb") as f:
                    cmd = f.read().decode("utf-8", "ignore").replace("\x00", " ")
                if exe in cmd:
                    pids.add(int(p))
            except OSError:
                continue
        return pids

    def _net_wm_pid(self, win):
        try:
            prop = win.get_full_property(self.d.intern_atom("_NET_WM_PID", True), 0)
            return int(prop.value[0]) if prop and prop.value else None
        except (error.XError, ValueError, IndexError):
            return None

    def find_game_windows(self):
        """返回所有匹配的候选窗口，按 (已映射, 面积) 降序。

        候选 = 标题匹配的窗口。若能在 /proc 里找到游戏进程，则要求窗口的
        _NET_WM_PID 属于游戏进程——Wine 会周期性创建同名 1x1 占位窗口，
        只按标题选会中招（守护进程在真窗口与占位窗口间反复横跳，热键失效）。
        """
        with self.lock:
            wanted = self.cfg["window_title"]
            gpids = self.game_pids()
            out = []
            seen = set()
            frontier = [self.root]
            for _ in range(4):
                nxt = []
                for parent in frontier:
                    try:
                        children = parent.query_tree().children
                    except error.XError:
                        continue
                    for child in children:
                        if child.id in seen:
                            continue
                        seen.add(child.id)
                        try:
                            t = self._window_title(child)
                            if t and wanted in t:
                                pid = self._net_wm_pid(child)
                                if gpids and pid is not None and pid not in gpids:
                                    nxt.append(child)
                                    continue   # 同名但不是游戏进程的窗口（如文件管理器）
                                try:
                                    geo = child.get_geometry()
                                    attrs = child.get_attributes()
                                except error.XError:
                                    continue
                                out.append({
                                    "xid": child.id, "w": geo.width, "h": geo.height,
                                    "mapped": attrs.map_state == 2, "pid": pid,
                                })
                            nxt.append(child)   # 匹配与否都继续深入子窗口
                        except error.XError:
                            continue
                frontier = nxt
                if not frontier:
                    break
            out.sort(key=lambda c: (not c["mapped"], -(c["w"] * c["h"])))
            # 分层：有 pid 匹配的候选时，排除 pid 未知的窗口（如改名为游戏标题的
            # 启动器窗口——无 _NET_WM_PID、会持有焦点，焦点优先启发式曾误选它）
            if gpids:
                matched = [c for c in out if c["pid"] in gpids]
                if matched:
                    out = matched
            return out

    def find_game_window(self):
        """返回最佳游戏窗口 xid；无候选返回 None。

        过滤 1x1/微型占位窗口，优先已映射、面积最大的。
        """
        cands = self.find_game_windows()
        if not cands:
            return None
        big = [c for c in cands if c["w"] > 64 and c["h"] > 64] or cands
        focused = self.focused_xid()
        for c in big:
            if c["xid"] == focused:
                return c["xid"]   # 焦点所在的候选优先
        return big[0]["xid"]

    def window_mapped(self, xid):
        with self.lock:
            try:
                return self.window_obj(xid).get_attributes().map_state == 2
            except error.XError:
                return False

    @staticmethod
    def _repair_mojibake(name):
        """Tk 等程序把 UTF-8 存进 WM_NAME，python-xlib 按 Latin-1 解码会得到乱码；
        尝试还原，失败则原样返回。"""
        try:
            fixed = name.encode("latin-1").decode("utf-8")
            return fixed if fixed != name else name
        except (UnicodeEncodeError, UnicodeDecodeError):
            return name

    def _window_title(self, win):
        candidates = []
        name = win.get_wm_name()
        if name:
            candidates.append(self._repair_mojibake(str(name)))
        try:
            prop = win.get_full_property(self.d.intern_atom("_NET_WM_NAME", True), 0)
            if prop and prop.value:
                candidates.append(prop.value.decode("utf-8", "ignore"))
        except error.XError:
            pass
        for c in candidates:
            if c:
                return c
        return ""

    def window_obj(self, xid):
        return self.d.create_resource_object("window", xid)

    def client_rect(self, xid):
        """游戏窗口客户区的根窗口坐标 (x, y, w, h)；失败返回 None。

        注意方向：python-xlib 的 translate_coords(src_window, x, y) 是把
        src_window 坐标系里的点变换到 self 坐标系，因此求窗口原点要用
        root.translate_coords(win, 0, 0)。（全屏 (0,0) 时两种写法恰好同值，
        窗口化时写反会得到负的镜像位置。）
        """
        with self.lock:
            try:
                win = self.window_obj(xid)
                geo = win.get_geometry()
                c = self.root.translate_coords(win, 0, 0)
                return (c.x, c.y, geo.width, geo.height)
            except error.XError:
                return None

    def focused_xid(self):
        with self.lock:
            try:
                f = self.d.get_input_focus().focus
                return getattr(f, "id", X.NONE)
            except error.XError:
                return X.NONE

    def pointer_pos(self):
        with self.lock:
            p = self.root.query_pointer()
            return p.root_x, p.root_y

    def pointer_over_client(self, xid):
        """对应 AFA IsMouseInClient：鼠标在游戏窗口客户区内。"""
        rect = self.client_rect(xid)
        if not rect:
            return False
        x, y, w, h = rect
        px, py = self.pointer_pos()
        return x <= px < x + w and y <= py < y + h

    # -- 注入（XTEST）--

    def inject_key_raw(self, keycode, down: bool):
        if not keycode:
            return
        with self.lock:
            xtest.fake_input(self.d, X.KeyPress if down else X.KeyRelease, keycode)
            self.d.flush()

    def inject_button(self, button, down: bool):
        with self.lock:
            xtest.fake_input(self.d, X.ButtonPress if down else X.ButtonRelease, button)
            self.d.flush()

    def move_pointer(self, x, y):
        with self.lock:
            xtest.fake_input(self.d, X.MotionNotify, x=int(x), y=int(y))
            self.d.flush()

    # -- 像素采样（关卡守卫）--

    def sample_level_objects(self, xid):
        """返回命中的关卡对象数（0-3）。捕获失败按 0 处理（同 AFA 语义）。"""
        rect = self.client_rect(xid)
        if not rect:
            return 0
        x0, y0, w, h = rect
        hits = 0
        with self.lock:
            win = self.window_obj(xid)
            for obj in LEVEL_OBJECTS:
                rw = max(1, int(round((obj["rx"] - obj["lx"]) * w)))
                rh = max(1, int(round((obj["dy"] - obj["uy"]) * h)))
                cx = int(round(obj["lx"] * w))
                cy = int(round(obj["uy"] * h))
                try:
                    img = win.get_image(cx, cy, rw, rh, X.ZPixmap, 0xFFFFFFFF)
                except error.XError:
                    continue
                if self._region_hit(img, obj["colors"]):
                    hits += 1
        return hits

    @staticmethod
    def _region_hit(img, colors) -> bool:
        if img.depth not in (24, 32):
            return False
        data = img.data
        step = 4  # ZPixmap 24/32bpp 按 4 字节步进（BGRX）
        n = len(data) // step * step
        for (c, v) in colors:
            cr, cg, cb = (c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF
            for i in range(0, n, step):
                b, g, r = data[i], data[i + 1], data[i + 2]
                if abs(r - cr) <= v and abs(g - cg) <= v and abs(b - cb) <= v:
                    return True
        return False


# ---------------------------------------------------------------------------
# 动作实现（时序一一对应 AFA hotkey_actions.ahk）
# ---------------------------------------------------------------------------

class Actions:
    def __init__(self, xh: XHelper, cfg, guard: "LevelGuard"):
        self.xh = xh
        self.cfg = cfg
        self.guard = guard
        self.injector = None   # HotkeyManager，App 装配时设置
        self.queue = collections.deque()   # 元素: (action, wait_kind, code)，主循环单线程消费

    # -- 基础原语 --

    def gkey(self, func):
        return keysym_keycode(self.xh.d, self.cfg["game_keys"].get(func, ""))

    def tap_game_key(self, func, delay=None):
        """AFA GameKeys.Tap：Down → 延迟 → Up"""
        delay = self.cfg["key_tap_delay_ms"] if delay is None else delay
        kc = self.gkey(func)
        if not kc:
            log.warning("游戏键 %s 未配置或无法解析", func)
            return
        self.injector.key_down(kc)
        msleep(delay)
        self.injector.key_up(kc)

    def tap_esc(self):
        kc = keysym_keycode(self.xh.d, "Escape")
        self.injector.key_down(kc)
        msleep(self.cfg["key_tap_delay_ms"])
        self.injector.key_up(kc)

    def left_click_now(self):
        self.xh.inject_button(1, True)
        self.xh.inject_button(1, False)

    def current_delay(self):
        return FRAME_DELAY_MS.get(str(self.cfg["frame_rate"]), 9)

    # -- 触摸等效点击：移动→点击→复位（AFA 用触摸注入避免移动光标，
    #    Linux 版用"移动+点击+移回"等效实现）--

    def tap_at(self, x, y):
        self.xh.move_pointer(x, y)
        self.xh.inject_button(1, True)
        msleep(self.cfg["touch_tap_ms"])
        self.xh.inject_button(1, False)
        msleep(10)

    def client_point(self, frac):
        rect = self.xh.client_rect(self.guard.game_xid)
        if not rect:
            return None
        x0, y0, w, h = rect
        return (x0 + frac[0] * w, y0 + frac[1] * h)

    # -- 动作（名称对应 AFA Action*）--

    def action_press_pause(self):            # 按下时暂停：ESC 点击
        self.tap_esc()

    def action_release_pause(self):          # 松开时暂停：暂停键点击
        self.tap_game_key("pauseBattle")

    def action_game_speed(self):             # 切换倍速
        self.tap_game_key("changeSpeed")

    def action_skill(self):                  # 技能
        self.tap_game_key("releaseSkill")

    def action_retreat(self):                # 撤退
        self.tap_game_key("retreatChar")

    def action_one_click_skill(self):        # 一键技能：左键 → ClickDelay → 技能键
        if not self.xh.pointer_over_client(self.guard.game_xid):
            log.debug("one_click_skill 跳过：鼠标不在客户端")
            return
        self.left_click_now()
        msleep(self.cfg["click_delay_ms"])
        self.tap_game_key("releaseSkill")

    def action_one_click_retreat(self):      # 一键撤退
        if not self.xh.pointer_over_client(self.guard.game_xid):
            log.debug("one_click_retreat 跳过：鼠标不在客户端")
            return
        self.left_click_now()
        msleep(self.cfg["click_delay_ms"])
        self.tap_game_key("retreatChar")

    def _frame_skip(self, delay):            # 过帧：ESC ↓ → delay → 暂停键 ↓50ms → 都 ↑
        kc_esc = keysym_keycode(self.xh.d, "Escape")
        kc_pause = self.gkey("pauseBattle")
        self.injector.key_down(kc_esc)
        msleep(delay)
        self.injector.key_down(kc_pause)
        msleep(self.cfg["key_tap_delay_ms"])
        self.injector.key_up(kc_esc)
        self.injector.key_up(kc_pause)

    def action_frame_16ms(self):
        self._frame_skip(self.cfg["frame_skip_16ms_delay"])

    def action_frame_33ms(self):
        self._frame_skip(self.cfg["frame_skip_33ms_delay"])

    def action_frame_166ms(self):
        self._frame_skip(self.cfg["frame_skip_166ms_delay"])

    def _pause_three_tap(self):
        """暂停态三连击：暂停键左半 → 单位处 → 暂停键右半 → 光标回到单位处。"""
        if not self.xh.pointer_over_client(self.guard.game_xid):
            log.debug("暂停态动作跳过：鼠标不在客户端")
            return None
        rect = self.xh.client_rect(self.guard.game_xid)
        if not rect:
            return None
        mx, my = self.xh.pointer_pos()
        pl = self.client_point(POS_PAUSE_LEFT)
        pr = self.client_point(POS_PAUSE_RIGHT)
        self.tap_at(*pl)
        self.tap_at(mx, my)
        self.tap_at(*pr)
        self.xh.move_pointer(mx, my)
        return (mx, my)

    def action_pause_select(self):           # 暂停时选中
        if self._pause_three_tap() is None:
            log.warning("pause_select 跳过：游戏窗口不存在")
            return
        msleep(self.current_delay() * 1.5)

    def action_pause_skill(self):            # 暂停技能
        if self._pause_three_tap() is None:
            log.warning("pause_skill 跳过：游戏窗口不存在")
            return
        msleep(self.cfg["click_delay_ms"])
        kc = self.gkey("releaseSkill")
        self.injector.key_down(kc)
        msleep(max(self.current_delay() * 1.5 - self.cfg["click_delay_ms"], 0))
        msleep(self.cfg["key_tap_delay_ms"])
        self.injector.key_up(kc)

    def action_pause_retreat(self):          # 暂停撤退
        if self._pause_three_tap() is None:
            log.warning("pause_retreat 跳过：游戏窗口不存在")
            return
        msleep(self.cfg["click_delay_ms"])
        kc = self.gkey("retreatChar")
        self.injector.key_down(kc)
        msleep(max(self.current_delay() * 1.5 - self.cfg["click_delay_ms"], 0))
        msleep(self.cfg["key_tap_delay_ms"])
        self.injector.key_up(kc)

    def action_switch_view(self):            # 视角切换
        if self._pause_three_tap() is None:
            log.warning("switch_view 跳过：游戏窗口不存在")
            return
        mx, my = self.xh.pointer_pos()
        self.tap_at(mx, my)

    def action_l_button_click_down(self):    # 模拟左键按住（触发键按下时）
        self.xh.inject_button(1, True)

    def action_l_button_click_up(self):
        self.xh.inject_button(1, False)

    def action_cease_operations(self):       # 放弃行动
        self.tap_game_key("battleLeftPopup")

    def action_back(self):                   # 返回上级菜单
        self.tap_esc()

    def _click_button_at(self, frac, name):
        """AFA _ClickButton：移动到按钮点击后恢复光标（跳过/基建收取/肉鸽收下）。"""
        if not self.xh.pointer_over_client(self.guard.game_xid):
            log.debug("%s 跳过：鼠标不在客户端", name)
            return
        pos = self.client_point(frac)
        if not pos:
            log.warning("%s 跳过：游戏窗口不存在", name)
            return
        mx, my = self.xh.pointer_pos()
        self.xh.move_pointer(*pos)
        self.xh.inject_button(1, True)
        msleep(20)
        self.xh.inject_button(1, False)
        msleep(40)
        self.xh.move_pointer(mx, my)

    def action_skip(self):
        self._click_button_at(POS_SKIP, "skip")

    def action_harvest(self):
        self._click_button_at(POS_HARVEST, "harvest")

    def action_collect_collectibles(self):
        self._click_button_at(POS_COLLECT, "collect_collectibles")

    ACTION_TABLE = {
        "press_pause": action_press_pause,
        "release_pause": action_release_pause,
        "game_speed": action_game_speed,
        "pause_select": action_pause_select,
        "skill": action_skill,
        "retreat": action_retreat,
        "frame_16ms": action_frame_16ms,
        "frame_33ms": action_frame_33ms,
        "frame_166ms": action_frame_166ms,
        "one_click_skill": action_one_click_skill,
        "one_click_retreat": action_one_click_retreat,
        "pause_skill": action_pause_skill,
        "pause_retreat": action_pause_retreat,
        "switch_view": action_switch_view,
        "cease_operations": action_cease_operations,
        "back": action_back,
        "skip": action_skip,
        "harvest": action_harvest,
        "collect_collectibles": action_collect_collectibles,
    }


# ---------------------------------------------------------------------------
# 关卡守卫（移植 LevelDetector：3 对象颜色采样，命中≥2 判定在关卡内）
# ---------------------------------------------------------------------------

class LevelGuard:
    def __init__(self, xh: XHelper, cfg):
        self.xh = xh
        self.cfg = cfg
        self.game_xid = X.NONE
        self._in_level = True   # 守卫未就绪时放行，避免启动初期吞键
        self._lock = threading.Lock()

    @property
    def in_level(self):
        with self._lock:
            return self._in_level

    def poll_once(self):
        if not self.cfg["in_level_guard"] or not self.game_xid:
            return
        try:
            hits = self.xh.sample_level_objects(self.game_xid)
        except Exception as e:
            log.debug("守卫采样失败：%s", e)
            return
        with self._lock:
            self._in_level = hits >= 2


# ---------------------------------------------------------------------------
# 热键抓取 + 事件分发（事件连接 dx 的主使用者）
# ---------------------------------------------------------------------------

MOD_VARIANTS = [0, X.Mod2Mask, X.LockMask, X.Mod2Mask | X.LockMask]  # 空/NumLock/CapsLock


class HotkeyManager:
    """在游戏窗口上安装 XGrabKey/XGrabButton；处理触发与守卫透传。"""

    def __init__(self, dx: display.Display, xh: XHelper, cfg, actions: Actions):
        self.dx = dx
        self.xh = xh
        self.cfg = cfg
        self.actions = actions
        self.game_xid = X.NONE
        self.key_grabs = {}     # keycode -> action（按下型）
        self.hold_grabs = {}    # keycode -> action（按住型：down/up 两段）
        self.up_grabs = {}      # keycode -> action（松开型）
        self.button_grabs = {}  # button -> action
        self.held = set()       # 已按下（过滤自动重复）
        self.forwarding = {}    # keycode -> True（守卫拦截透传中）
        self.actions.injector = self

    # -- 安装/卸载 --

    def install(self, xid):
        self.uninstall()
        self.game_xid = xid
        win = self.dx.create_resource_object("window", xid)
        conflicts = []
        for action, keyname in self.cfg["bindings"].items():
            if not keyname:
                continue
            if keyname in MOUSE_BUTTONS:
                btn = MOUSE_BUTTONS[keyname]
                self.button_grabs[btn] = action
                for mod in MOD_VARIANTS:
                    err = error.CatchError(error.BadAccess)
                    win.grab_button(btn, mod, True,
                                    X.ButtonPressMask | X.ButtonReleaseMask,
                                    X.GrabModeAsync, X.GrabModeAsync,
                                    X.NONE, X.NONE, onerror=err)
                    if err.get_error():
                        conflicts.append(f"{action}({keyname})")
                continue
            if keyname.lower().startswith("wheel"):
                log.warning("绑定 %s=%s：暂不支持滚轮触发，已忽略", action, keyname)
                continue
            kc = keysym_keycode(self.dx, keyname)
            if not kc:
                log.warning("绑定 %s=%s：无法解析键名，已忽略", action, keyname)
                continue
            if action == "l_button_click":
                self.hold_grabs[kc] = action
            elif action == "release_pause":
                self.up_grabs[kc] = action
            else:
                self.key_grabs[kc] = action
            for mod in MOD_VARIANTS:
                err = error.CatchError(error.BadAccess)
                win.grab_key(kc, mod, True, X.GrabModeAsync, X.GrabModeAsync, onerror=err)
                if err.get_error():
                    conflicts.append(f"{action}({keyname})")
        self.dx.sync()
        n = len(self.key_grabs) + len(self.up_grabs) + len(self.hold_grabs)
        log.info("已对游戏窗口 0x%x 安装 %d 个键盘触发、%d 个鼠标触发%s",
                 xid, n, len(self.button_grabs),
                 f"（冲突未抓取：{', '.join(conflicts)}）" if conflicts else "")

    def uninstall(self):
        if self.game_xid:
            try:
                win = self.dx.create_resource_object("window", self.game_xid)
                for kc in list(self.key_grabs) + list(self.up_grabs) + list(self.hold_grabs):
                    for mod in MOD_VARIANTS:
                        win.ungrab_key(kc, mod)
                for btn in self.button_grabs:
                    for mod in MOD_VARIANTS:
                        win.ungrab_button(btn, mod)
                self.dx.sync()
            except error.XError:
                pass
        self.key_grabs.clear()
        self.up_grabs.clear()
        self.hold_grabs.clear()
        self.button_grabs.clear()
        self.held.clear()
        self.forwarding.clear()
        self.game_xid = X.NONE

    # -- 注入原语（供 Actions 使用）--
    # XTEST 事件与真实按键一样会激活被动抓取，因此注入"已被自己抓取的键"
    # 前必须先解挂，注入完毕再恢复，否则会递归触发自身动作。

    def _is_grabbed(self, keycode):
        return keycode in self.key_grabs or keycode in self.up_grabs or keycode in self.hold_grabs

    def _win(self):
        return self.dx.create_resource_object("window", self.game_xid)

    def _temp_ungrab(self, keycode):
        if not self.game_xid:
            return
        win = self._win()
        for mod in MOD_VARIANTS:
            err = error.CatchError()
            win.ungrab_key(keycode, mod, onerror=err)
        self.dx.sync()
        if err.get_error():
            # 解挂失败意味着注入会激活自己的抓取、递归触发动作——必须大声报错
            log.warning("解挂键码 %d 失败（%s），注入可能递归触发", keycode, err.get_error())

    def _temp_regrab(self, keycode):
        if not self.game_xid or not self._is_grabbed(keycode):
            return
        win = self._win()
        failed = False
        for mod in MOD_VARIANTS:
            err = error.CatchError()
            win.grab_key(keycode, mod, True, X.GrabModeAsync, X.GrabModeAsync, onerror=err)
            if err.get_error():
                failed = True
        self.dx.sync()
        if failed:
            log.warning("重挂键码 %d 失败，该触发键可能失效（不会自动恢复，重启工具可修复）", keycode)

    def key_down(self, keycode):
        if keycode and self._is_grabbed(keycode):
            self._temp_ungrab(keycode)
        self.xh.inject_key_raw(keycode, True)

    def key_up(self, keycode):
        self.xh.inject_key_raw(keycode, False)
        if keycode and self._is_grabbed(keycode):
            self._temp_regrab(keycode)

    # -- 事件处理 --

    def reconcile(self):
        """清理丢失 Release 造成的卡键状态。

        动作注入期间会临时解挂触发键（XTEST 会触发被动抓取）；这个窗口里
        用户松开按键的物理事件被直接送进游戏，我们收不到 Release，导致
        held/forwarding 残留——之后该键的每次按下都被当自动重复过滤。
        以 keymap 物理状态为准清理残留（注入进行中的键物理+注入双重按下，
        keymap 仍为 down，不会被误清）。
        """
        if not (self.held or self.forwarding):
            return
        try:
            keymap = self.dx.query_keymap()
        except Exception:
            return

        def down(kc):
            return bool(keymap[kc // 8] & (1 << (kc % 8)))

        for kc in [k for k in self.held if not down(k)]:
            self.held.discard(kc)
            log.debug("对账：清理丢失 Release 的键 %d", kc)
        for kc in [k for k in self.forwarding if not down(k)]:
            self.forwarding.pop(kc, None)   # 游戏已收到真实 Up，无需补发
            log.debug("对账：清理残留透传键 %d", kc)

    def handle_key_press(self, keycode):
        self.reconcile()
        if keycode in self.held:
            return  # 自动重复（含物理仍按住的情况）
        self.held.add(keycode)
        action = self.key_grabs.get(keycode) or self.hold_grabs.get(keycode)
        if not action:
            return  # 松开型触发：按下时无动作
        if self._guard_blocks(action):
            self._forward_down(keycode)
            return
        if action == "l_button_click":
            self.actions.action_l_button_click_down()
        else:
            self._enqueue(action, "key", keycode)

    def handle_key_release(self, keycode):
        # 注意：不检查 keymap 是否仍为按下。我们自己的连接未开启 detectable
        # auto-repeat，X 只送连续 KeyPress、不会伪造 Release；而 keymap 会被
        # 动作注入中的"同键按下"污染——若据此忽略物理 Release，held 就会残留，
        # 该键从此被当自动重复过滤（表现为"点几次后失灵"）。
        if keycode not in self.held:
            return
        self.held.discard(keycode)
        if keycode in self.forwarding:
            self.forwarding.pop(keycode, None)
            self.xh.inject_key_raw(keycode, False)   # 透传结束：补发 Up
            self._temp_regrab(keycode)
            return
        action = self.up_grabs.get(keycode)
        if action:
            if self._guard_blocks(action):
                self._forward_tap(keycode)   # 松开时暂停被拦截：重放完整按下
            else:
                self._enqueue(action)   # 松开型触发，无需再等释放
            return
        if self.hold_grabs.get(keycode):
            self.actions.action_l_button_click_up()

    def handle_button(self, button, down):
        action = self.button_grabs.get(button)
        if not action:
            return
        if down:
            if button in self.held:
                return
            self.held.add(button)
            focused = self.xh.focused_xid() == self.game_xid
            if not (focused or self.cfg["hover_operate"]):
                return
            if self._guard_blocks(action):
                return  # 鼠标侧键透传无意义，吞掉
            self._enqueue(action, "button", button)
        else:
            self.held.discard(button)

    # -- 守卫与透传 --

    def _guard_blocks(self, action) -> bool:
        if action not in GUARDED_ACTIONS or not self.cfg["in_level_guard"]:
            return False
        ok = self.actions.guard.in_level
        if not ok:
            log.info("%s 被关卡守卫拦截（不在关卡内），触发键透传", action)
        return not ok

    def _forward_down(self, keycode):
        """守卫拦截：把物理触发键透传给游戏（保住菜单/输入框的正常键入）。"""
        if keycode in self.forwarding:
            return
        self.forwarding[keycode] = True
        self._temp_ungrab(keycode)
        self.xh.inject_key_raw(keycode, True)

    def _forward_tap(self, keycode):
        self._temp_ungrab(keycode)
        self.xh.inject_key_raw(keycode, True)
        msleep(self.cfg["key_tap_delay_ms"])
        self.xh.inject_key_raw(keycode, False)
        self._temp_regrab(keycode)

    def _enqueue(self, action, wait_kind=None, code=None):
        # wait_kind/code：动作需等该触发键物理释放后才执行——按住期间活跃抓取
        # 会把 XTEST 注入的"按下"也吞给本进程（游戏收不到），帧操/暂停类动作
        # 会静默失效。等价于 AFA 的 PureKeyWait 语义。
        log.info("触发 %s", action)
        self.actions.queue.append((action, wait_kind, code))


# ---------------------------------------------------------------------------
# App：线程编排
# ---------------------------------------------------------------------------

class App:
    """线程模型：
      - 主循环（单线程）：独占事件连接 dx —— 读取/分发 X 事件、执行抓取安装
        命令、串行执行动作（动作里的解挂/重挂/注入都在本线程）。
        python-xlib 的 Display 不耐跨线程并发使用（next_event 与请求并发会
        偶发协议错误，旧版事件循环因此静默死亡、表现为"点几下就没反应"），
        故 dx 的所有操作必须单线程化。动作阻塞期间 X 事件由内核/库缓冲。
      - 监控线程：只用控制连接 dc（窗口查找、几何、守卫采样），通过
        cmd_queue 请求抓取变更。
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.xh = XHelper(cfg)
        self.dx = display.Display()          # 事件连接（仅主循环线程使用）
        self.guard = LevelGuard(self.xh, cfg)
        self.actions = Actions(self.xh, cfg, self.guard)
        self.hotkeys = HotkeyManager(self.dx, self.xh, cfg, self.actions)
        self.cmd_queue = queue.Queue()
        self._stop = threading.Event()
        self._config_mtime = self._config_mtime_now()
        self._config_checked = False   # 首次检查只记录基线，不触发重载

    @staticmethod
    def _config_mtime_now():
        try:
            return os.path.getmtime(CONFIG_PATH)
        except OSError:
            return 0

    def _dispatch(self, ev):
        if ev.type == X.KeyPress:
            self.hotkeys.handle_key_press(ev.detail)
        elif ev.type == X.KeyRelease:
            self.hotkeys.handle_key_release(ev.detail)
        elif ev.type in (X.ButtonPress, X.ButtonRelease):
            self.hotkeys.handle_button(ev.detail, ev.type == X.ButtonPress)

    def _run_one_action(self):
        if not self.actions.queue:
            return
        action, wait_kind, code = self.actions.queue[0]
        if wait_kind and self._code_still_down(wait_kind, code):
            return  # 触发键仍按着：等释放后再执行
        self.actions.queue.popleft()
        fn = Actions.ACTION_TABLE.get(action)
        if not fn:
            return
        try:
            fn(self.actions)
        except Exception:
            log.exception("动作 %s 执行异常", action)

    def _code_still_down(self, wait_kind, code):
        try:
            if wait_kind == "key":
                keymap = self.dx.query_keymap()
                return bool(keymap[code // 8] & (1 << (code % 8)))
            if wait_kind == "button":
                mask = self.xh.root.query_pointer().mask
                return bool(mask & (1 << (7 + code)))
        except Exception:
            return False
        return False

    def _main_loop(self):
        import selectors
        sel = selectors.DefaultSelector()
        sel.register(self.dx.fileno(), selectors.EVENT_READ)
        while not self._stop.is_set():
            # 1) 监控线程的抓取变更命令
            while True:
                try:
                    kind, xid = self.cmd_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    if kind == "install":
                        self.hotkeys.install(xid)
                    elif kind == "uninstall":
                        self.hotkeys.uninstall()
                    elif kind == "reload":
                        self._apply_reload(xid)
                except error.XError as e:
                    log.warning("抓取操作失败：%s", e)
            # 2) 处理全部待处理 X 事件
            try:
                while self.dx.pending_events():
                    self._dispatch(self.dx.next_event())
            except Exception:
                log.exception("事件处理异常（连接保持，继续运行）")
            # 3) 串行执行至多一个动作；期间到达的触发事件进入缓冲
            self._run_one_action()
            # 4) 等待新事件
            if not self._stop.is_set():
                try:
                    sel.select(timeout=0.02)
                except OSError:
                    time.sleep(0.02)
        sel.close()

    def _apply_reload(self, new_cfg):
        """应用外部（GUI）写入的新配置：原位替换并按新键位重新安装抓取。"""
        self.cfg.clear()
        self.cfg.update(new_cfg)
        xid = self.hotkeys.game_xid
        try:
            self.hotkeys.uninstall()
            if xid:
                self.hotkeys.install(xid)
            log.info("配置已热重载（键位 %d 个）",
                     sum(1 for v in self.cfg["bindings"].values() if v))
        except error.XError as e:
            log.warning("热重载后重装抓取失败：%s", e)

    def _check_config_reload(self):
        """监控线程调用：config.json 变更则请求主循环重载。"""
        mt = self._config_mtime_now()
        if not self._config_checked:
            self._config_mtime = mt
            self._config_checked = True
            return
        if mt != self._config_mtime:
            self._config_mtime = mt
            if mt:
                self.cmd_queue.put(("reload", load_config()))
                log.info("检测到 config.json 变更，准备热重载")

    def _monitor(self):
        poll = float(self.cfg["window_poll_seconds"])
        guard_ms = float(self.cfg["guard_poll_ms"]) / 1000.0
        last_guard = 0.0
        while not self._stop.is_set():
            # 粘滞策略：只要当前窗口仍存活（存在、非 1x1 占位、已映射）就绝不换目标，
            # 避免在真窗口与 Wine 周期性创建的同名占位窗口间反复横跳导致热键失效。
            cur = self.hotkeys.game_xid
            cur_ok = False
            if cur:
                rect = self.xh.client_rect(cur)
                cur_ok = (bool(rect) and rect[2] > 64 and rect[3] > 64
                          and self.xh.window_mapped(cur))
            if cur_ok:
                self.guard.game_xid = cur
            else:
                xid = self.xh.find_game_window()
                if xid and xid != cur:
                    self.guard.game_xid = xid
                    self.cmd_queue.put(("install", xid))
                    log.info("游戏窗口切换为 0x%x", xid)
                elif not xid and cur:
                    self.guard.game_xid = X.NONE
                    self.cmd_queue.put(("uninstall", None))
                    if self.cfg.get("auto_exit", True):
                        log.info("游戏已退出，守护随之退出（auto_exit）")
                        self._stop.set()
                    else:
                        log.info("游戏窗口已消失，等待游戏启动…")
            now = time.monotonic()
            if now - last_guard >= guard_ms:
                last_guard = now
                self.guard.poll_once()
            self._check_config_reload()
            self._stop.wait(poll)

    def run(self):
        log.info("AFA Linux 启动（窗口标题含「%s」，帧率档位 %s，关卡守卫 %s）",
                 self.cfg["window_title"], self.cfg["frame_rate"],
                 "开" if self.cfg["in_level_guard"] else "关")
        xid = self.xh.find_game_window()
        if xid:
            self.guard.game_xid = xid
            self.hotkeys.install(xid)
            log.info("找到游戏窗口 0x%x", xid)
        else:
            log.info("未找到游戏窗口，等待游戏启动…")
        threading.Thread(target=self._monitor, daemon=True).start()
        try:
            self._main_loop()
        finally:
            log.info("退出，清理抓取…")
            try:
                self.hotkeys.uninstall()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 自检（--selftest）：验证 XTEST 注入与抓取链路，不触碰游戏
# ---------------------------------------------------------------------------

def selftest():
    print("== AFA Linux 自检 ==")
    d1 = display.Display()   # 测试窗口/抓取（模拟 AFA 的事件连接）
    d2 = display.Display()   # 注入连接（模拟 XHelper）
    root = d1.screen().root

    # 1) 找游戏窗口（只读）
    xh = XHelper({"window_title": "明日方舟"})
    gxid = xh.find_game_window()
    if gxid:
        game = xh.window_obj(gxid)
        geo = game.get_geometry()
        c = root.translate_coords(game, 0, 0)
        print(f"[1] 游戏窗口: 0x{game.id:x}  {geo.width}x{geo.height} @ ({c.x},{c.y})"
              f"  焦点在游戏窗口: {d1.get_input_focus().focus.id == game.id}")
    else:
        print("[1] 游戏窗口: 未找到（游戏未运行？不影响自检）")

    # 2) 创建测试窗口并取焦点
    prev_focus = d1.get_input_focus().focus
    test = root.create_window(0, 0, 2, 2, 1, X.CopyFromParent, X.InputOutput,
                              visual=X.CopyFromParent,
                              background_pixel=d1.screen().black_pixel)
    test.map()
    test.change_attributes(event_mask=X.KeyPressMask | X.KeyReleaseMask)
    d1.flush()
    time.sleep(0.2)
    d1.set_input_focus(test, X.RevertToPointerRoot, X.CurrentTime)
    d1.flush()
    time.sleep(0.3)
    ok_focus = d1.get_input_focus().focus.id == test.id
    print(f"[2] 测试窗口获得焦点: {ok_focus}")

    # 3) XTEST 注入 → 无抓取时应作为真实按键到达焦点窗口（证明游戏能收到注入）
    kc_a = d1.keysym_to_keycode(XK.string_to_keysym("a"))
    xtest.fake_input(d2, X.KeyPress, kc_a)
    d2.flush()
    time.sleep(0.1)
    xtest.fake_input(d2, X.KeyRelease, kc_a)
    d2.flush()
    got = _wait_key(d1, kc_a)
    print(f"[3] XTEST 注入按键被窗口接收（即游戏将收到注入按键）: {got}")

    # 4) 被动抓取：抓取键码后注入，事件应直达抓取者（多候选，避开被全局快捷键占用的键）
    grabbed = False
    used = None
    for cand in ("F8", "Home", "End", "Pause", "a"):
        kc_g = d1.keysym_to_keycode(XK.string_to_keysym(cand))
        if not kc_g:
            continue
        err = error.CatchError(error.BadAccess)
        test.grab_key(kc_g, 0, True, X.GrabModeAsync, X.GrabModeAsync, onerror=err)
        d1.flush()
        if err.get_error():
            continue
        used = cand
        xtest.fake_input(d2, X.KeyPress, kc_g)
        d2.flush()
        time.sleep(0.1)
        xtest.fake_input(d2, X.KeyRelease, kc_g)
        d2.flush()
        grabbed = _wait_key(d1, kc_g)
        for mod in (0, X.Mod2Mask, X.LockMask, X.Mod2Mask | X.LockMask):
            test.ungrab_key(kc_g, mod)
        d1.flush()
        break
    print(f"[4] 触发键抓取+拦截链路（{used or '无可用键码'}）: {grabbed}")

    # 5) 恢复焦点、销毁测试窗口
    try:
        d1.set_input_focus(prev_focus, X.RevertToPointerRoot, X.CurrentTime)
        d1.flush()
    except error.XError:
        pass
    test.destroy()
    d1.flush()

    ok = ok_focus and got and grabbed
    print(f"== 自检{'通过' if ok else '存在失败项，请查看上方输出'} ==")
    return 0 if ok else 1


def _wait_key(d, keycode, timeout=1.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        while d.pending_events():
            ev = d.next_event()
            if ev.type == X.KeyPress and ev.detail == keycode:
                return True
        time.sleep(0.05)
    return False


def status(cfg):
    xh = XHelper(cfg)
    xid = xh.find_game_window()
    if not xid:
        print("未找到游戏窗口（标题含「%s」）。游戏未运行？" % cfg["window_title"])
        return 1
    rect = xh.client_rect(xid)
    print(f"游戏窗口: 0x{xid:x}  客户区: {rect[2]}x{rect[3]} @ ({rect[0]},{rect[1]})")
    print(f"焦点在游戏窗口: {xh.focused_xid() == xid}")
    print(f"鼠标位置: {xh.pointer_pos()}  在游戏客户区内: {xh.pointer_over_client(xid)}")
    if cfg["in_level_guard"]:
        hits = xh.sample_level_objects(xid)
        print(f"关卡守卫采样: 命中 {hits}/3 → {'关卡内' if hits >= 2 else '关卡外'}")
    else:
        print("关卡守卫: 已关闭")
    print("触发绑定:")
    for action, key in cfg["bindings"].items():
        print(f"  {action:22s} <- {key or '(未绑定)'}")
    return 0


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    ap = argparse.ArgumentParser(description="明日方舟帧操小助手 Linux 版")
    ap.add_argument("--status", action="store_true", help="查看游戏窗口/守卫状态后退出")
    ap.add_argument("--selftest", action="store_true", help="注入与抓取链路自检后退出")
    ap.add_argument("--no-guard", action="store_true", help="本次运行关闭关卡守卫")
    args = ap.parse_args()

    cfg = load_config()
    if args.no_guard:
        cfg["in_level_guard"] = False

    # 文件日志：logs/afa.log（2MB 滚动，保留 2 份），终端与文件双写
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    from logging.handlers import RotatingFileHandler
    fh = RotatingFileHandler(os.path.join(log_dir, "afa.log"),
                             maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root_logger = logging.getLogger()
    root_logger.addHandler(fh)
    lvl = getattr(logging, str(cfg["log_level"]).upper(), logging.INFO)
    root_logger.setLevel(lvl)
    logging.getLogger("afa").setLevel(lvl)

    if args.status:
        return status(cfg)
    if args.selftest:
        return selftest()

    log.info("日志文件：%s", os.path.join(log_dir, "afa.log"))
    app = App(cfg)
    signal.signal(signal.SIGINT, lambda *_: app._stop.set())
    signal.signal(signal.SIGTERM, lambda *_: app._stop.set())
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
