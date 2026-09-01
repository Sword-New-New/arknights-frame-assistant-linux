#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置热重载端到端测试：模拟 GUI 保存 config.json → 守护进程 2 秒内热生效。

流程（假"游戏窗口"拓扑，同 test_e2e）：
  1. 临时 config（绑定 one_click_skill=e），monkeypatch afa.CONFIG_PATH，启动真实 App
  2. 按 e → 动作注入到达窗口
  3. 改写临时 config：新增 frame_33ms=r（模拟 GUI 保存）
  4. 等待热重载 → 按 r → 应收到 ESC+空格 的过帧注入；e 仍有效
"""

import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import afa
from Xlib import X, XK, display
from Xlib.ext import xtest

TITLE = "AFA_E2E_TEST"


def main():
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    base_cfg = dict(afa.DEFAULT_CONFIG)
    base_cfg.update({"window_title": TITLE, "in_level_guard": False,
                     "window_poll_seconds": 0.5,
                     "bindings": {k: ("e" if k == "one_click_skill" else "")
                                  for k in afa.DEFAULT_BINDINGS}})
    json.dump(base_cfg, tmp, ensure_ascii=False)
    tmp.close()
    afa.CONFIG_PATH = tmp.name          # 让守护的热重载读测试配置

    d2 = display.Display()
    d3 = display.Display()
    root3 = d3.screen().root
    px, py = root3.query_pointer().root_x, root3.query_pointer().root_y
    game = root3.create_window(px - 20, py - 20, 60, 60, 1, X.CopyFromParent, X.InputOutput,
                               visual=X.CopyFromParent, background_pixel=d3.screen().black_pixel,
                               event_mask=X.KeyPressMask | X.KeyReleaseMask
                                          | X.ButtonPressMask | X.ButtonReleaseMask)
    game.set_wm_name(TITLE)
    game.change_attributes(override_redirect=1)
    game.map()
    d3.flush()
    time.sleep(0.2)
    prev_focus = d3.get_input_focus().focus
    d3.set_input_focus(game, X.RevertToPointerRoot, X.CurrentTime)
    d3.flush()
    time.sleep(0.2)

    def aim():
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

    def tap(keyname):
        kc = d2.keysym_to_keycode(XK.string_to_keysym(keyname))
        xtest.fake_input(d2, X.KeyPress, kc)
        d2.flush()
        time.sleep(0.04)
        xtest.fake_input(d2, X.KeyRelease, kc)
        d2.flush()
        time.sleep(0.35)

    aim()
    app = afa.App(afa.load_config())
    t = threading.Thread(target=app.run, daemon=True)
    t.start()
    time.sleep(1.5)
    assert app.hotkeys.game_xid == game.id, "App 未挂到测试窗口"
    print("[0] App 已挂载，初始绑定 one_click_skill=e")

    kc_e = d2.keysym_to_keycode(XK.string_to_keysym("e"))

    # 1) 初始键位生效
    aim(); drain(); tap("e")
    evs = drain()
    clicks = sum(1 for ev in evs if ev.type == X.ButtonPress and ev.detail == 1)
    print(f"[1] 初始键位 e 触发（左键 x{clicks}，应为 1）")
    ok = clicks == 1

    # 2) 模拟 GUI 保存：新增 frame_33ms=r
    time.sleep(0.5)  # 让 mtime 与上次检查错开
    base_cfg["bindings"]["frame_33ms"] = "r"
    with open(tmp.name, "w", encoding="utf-8") as f:
        json.dump(base_cfg, f, ensure_ascii=False)
    print("[2] 已改写 config（新增 frame_33ms=r），等待热重载…")
    deadline = time.time() + 6
    while time.time() < deadline and app.hotkeys.key_grabs.get(
            d2.keysym_to_keycode(XK.string_to_keysym("r"))) != "frame_33ms":
        time.sleep(0.2)
    reloaded = app.hotkeys.key_grabs.get(
        d2.keysym_to_keycode(XK.string_to_keysym("r"))) == "frame_33ms"
    print(f"[3] 热重载生效（r 已挂为 frame_33ms）: {reloaded}")
    ok &= reloaded

    # 3) 新键位 r 真的能触发过帧注入（ESC+空格 各一次按下）
    aim(); drain(); tap("r")
    time.sleep(0.3)
    evs = drain()
    print(f"    [debug] 窗口收到事件: {[(ev.type, getattr(ev, 'detail', None)) for ev in evs]}")
    esc = sum(1 for ev in evs if ev.type == X.KeyPress and ev.detail ==
              d2.keysym_to_keycode(XK.string_to_keysym("Escape")))
    space = sum(1 for ev in evs if ev.type == X.KeyPress and ev.detail == 65)
    print(f"[4] 按 r 收到过帧注入: ESC x{esc}，空格 x{space}（各应为 1）")
    ok &= esc == 1 and space == 1

    # 4) 原键位 e 仍有效
    aim(); drain(); tap("e")
    evs = drain()
    clicks = sum(1 for ev in evs if ev.type == X.ButtonPress and ev.detail == 1)
    print(f"[5] 热重载后 e 仍触发（左键 x{clicks}，应为 1）")
    ok &= clicks == 1

    app._stop.set()
    t.join(timeout=3)
    try:
        xtest.fake_input(d2, X.MotionNotify, x=px, y=py)
        d2.flush()
        d3.set_input_focus(prev_focus, X.RevertToPointerRoot, X.CurrentTime)
        d3.flush()
    except Exception:
        pass
    game.destroy()
    d3.flush()
    os.unlink(tmp.name)

    print(f"== 热重载端到端{'通过' if ok else '失败'} ==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
