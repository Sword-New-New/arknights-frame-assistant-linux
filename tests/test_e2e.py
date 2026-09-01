#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端测试：真实 App 流水线（监控→抓取→触发→动作→注入）。

造一个假"游戏窗口"（归 d3 所有、持有输入焦点、位置在当前鼠标处），
运行真实 App（配置指向该窗口标题），通过 XTEST 连按 3 次触发键，
统计假窗口收到的真实注入事件：

  - 每次触发应产生 1 组 [左键按下/抬起 + 技能键按下/抬起]
  - 连按 3 次应产生 3 组（回归"点几次后失灵"）

用法：python3 tests/test_e2e.py
"""

import queue
import sys
import threading
import time
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import afa
from Xlib import X, XK, display
from Xlib.ext import xtest

TITLE = "AFA_E2E_TEST"


def main():
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    d2 = display.Display()   # 测试用注入连接
    d3 = display.Display()   # "游戏"所有者
    root3 = d3.screen().root

    px, py = root3.query_pointer().root_x, root3.query_pointer().root_y
    game = root3.create_window(px - 20, py - 20, 60, 60, 1, X.CopyFromParent, X.InputOutput,
                               visual=X.CopyFromParent,
                               background_pixel=d3.screen().black_pixel,
                               event_mask=X.KeyPressMask | X.KeyReleaseMask
                                          | X.ButtonPressMask | X.ButtonReleaseMask)
    game.set_wm_name(TITLE)
    game.change_attributes(override_redirect=1)   # 不受 KWin 摆放，留在鼠标当前位置
    game.map()
    d3.flush()
    time.sleep(0.2)

    prev_focus = d3.get_input_focus().focus
    d3.set_input_focus(game, X.RevertToPointerRoot, X.CurrentTime)
    d3.flush()
    time.sleep(0.2)

    # 把鼠标移到窗口实际中心（动作的"鼠标在客户端内"守卫需要）；真人鼠标会动，后面每次注入前都会重新瞄准
    geo = game.get_geometry()
    c = root3.translate_coords(game, 0, 0)
    cx, cy = c.x + geo.width // 2, c.y + geo.height // 2
    real_px, real_py = root3.query_pointer().root_x, root3.query_pointer().root_y
    xtest.fake_input(d2, X.MotionNotify, x=cx, y=cy)
    d2.flush()
    time.sleep(0.2)
    print(f"[0.5] 窗口实际位置 ({c.x},{c.y}) {geo.width}x{geo.height}，鼠标移至 ({cx},{cy})")

    cfg = dict(afa.DEFAULT_CONFIG)
    cfg["window_title"] = TITLE
    cfg["game_process"] = ""
    cfg["in_level_guard"] = False
    cfg["window_poll_seconds"] = 0.5
    cfg["bindings"] = {k: ("e" if k == "one_click_skill" else "") for k in cfg["bindings"]}

    app = afa.App(cfg)
    t = threading.Thread(target=app.run, daemon=True)
    t.start()
    time.sleep(1.5)
    assert app.hotkeys.game_xid == game.id, f"App 未挂到测试窗口: {hex(app.hotkeys.game_xid)}"
    print(f"[0] App 已挂载到测试窗口 0x{game.id:x}")

    kc_e = d2.keysym_to_keycode(XK.string_to_keysym("e"))
    kc_a = d2.keysym_to_keycode(XK.string_to_keysym("a"))  # 干扰键（未绑定）

    def aim():
        """每次注入前把指针重新瞄到窗口中心（真人在用机器，指针会动）。"""
        geo = game.get_geometry()
        c = root3.translate_coords(game, 0, 0)
        xtest.fake_input(d2, X.MotionNotify, x=c.x + geo.width // 2, y=c.y + geo.height // 2)
        d2.flush()
        time.sleep(0.05)

    def drain():
        evs = []
        while d3.pending_events():
            evs.append(d3.next_event())
        return evs

    # 预热：未绑定键 a 应"透传"直达窗口（键盘事件），但绝不应产生左键注入
    for down in (True, False):
        xtest.fake_input(d2, X.KeyPress if down else X.KeyRelease, kc_a)
        d2.flush()
    time.sleep(0.4)
    noise = [(ev.type, getattr(ev, "detail", None)) for ev in drain()]
    noise_clicks = sum(1 for ty, d in noise if ty == X.ButtonPress and d == 1)
    print(f"[1] 未绑定键 a 的透传事件（键盘事件属正常透传，左键注入应为 0）: {noise}")
    ok_noise = noise_clicks == 0

    # 单次触发
    aim()
    for down in (True, False):
        xtest.fake_input(d2, X.KeyPress if down else X.KeyRelease, kc_e)
        d2.flush()
    time.sleep(0.6)
    evs = drain()
    clicks = sum(1 for ev in evs if ev.type == X.ButtonPress and ev.detail == 1)
    keys = sum(1 for ev in evs if ev.type == X.KeyPress and ev.detail == kc_e)
    print(f"[2] 单次触发收到: 左键按下 x{clicks}，技能键按下 x{keys}（各应为 1）")
    ok_single = clicks == 1 and keys == 1

    # 连按 6 次（人手速间隔）：回归"点几次后失灵"——旧版会衰减到 0，修复后应 6/6。
    # （比动作更快的连击会撞进上一次动作的注入窗口、透传给游戏，与 AFA 行为一致，不计入。）
    drain()
    n = 6
    for _ in range(n):
        aim()
        xtest.fake_input(d2, X.KeyPress, kc_e)
        d2.flush()
        time.sleep(0.03)
        xtest.fake_input(d2, X.KeyRelease, kc_e)
        d2.flush()
        time.sleep(0.35)
    deadline = time.time() + 4
    clicks = keys = 0
    while time.time() < deadline and (clicks < n or keys < n):
        evs = drain()
        clicks += sum(1 for ev in evs if ev.type == X.ButtonPress and ev.detail == 1)
        keys += sum(1 for ev in evs if ev.type == X.KeyPress and ev.detail == kc_e)
        time.sleep(0.05)
    print(f"[3] 连按 {n} 次收到: 左键按下 x{clicks}，技能键按下 x{keys}（各应为 {n}）")
    ok_burst = clicks == n and keys == n

    app._stop.set()
    t.join(timeout=3)
    try:
        xtest.fake_input(d2, X.MotionNotify, x=real_px, y=real_py)   # 归还鼠标位置
        d2.flush()
        d3.set_input_focus(prev_focus, X.RevertToPointerRoot, X.CurrentTime)
        d3.flush()
    except Exception:
        pass
    game.destroy()
    d3.flush()

    ok = ok_noise and ok_single and ok_burst
    print(f"== 端到端测试{'通过' if ok else '失败'} ==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
