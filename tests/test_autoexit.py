#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""auto_exit 回归测试：绑定过游戏窗口后窗口消失，守护应随配置退出/等待。

  - auto_exit=true：游戏窗口消失 → 守护自动退出
  - auto_exit=false：游戏窗口消失 → 守护继续等待
"""

import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import afa
from Xlib import X, display

TITLE = "AFA_E2E_TEST"


def make_cfg(auto_exit):
    cfg = dict(afa.DEFAULT_CONFIG)
    cfg.update({"window_title": TITLE, "in_level_guard": False,
                "window_poll_seconds": 0.5, "auto_exit": auto_exit,
                "bindings": {k: "" for k in afa.DEFAULT_BINDINGS}})
    return cfg


def spawn_window(d3, root3):
    px, py = root3.query_pointer().root_x, root3.query_pointer().root_y
    game = root3.create_window(px - 20, py - 20, 60, 60, 1, X.CopyFromParent, X.InputOutput,
                               visual=X.CopyFromParent, background_pixel=d3.screen().black_pixel)
    game.set_wm_name(TITLE)
    game.change_attributes(override_redirect=1)
    game.map()
    d3.flush()
    time.sleep(0.3)
    d3.set_input_focus(game, X.RevertToPointerRoot, X.CurrentTime)
    d3.flush()
    time.sleep(0.2)
    return game


def wait_attach(app, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        if app.hotkeys.game_xid:
            return True
        time.sleep(0.1)
    return False


def wait_exit(app, t, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if app._stop.is_set() and not t.is_alive():
            return True
        time.sleep(0.1)
    return False


def main():
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    d3 = display.Display()
    root3 = d3.screen().root
    ok = True

    # 1) auto_exit=True：窗口消失 → 守护退出
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(make_cfg(True), tmp, ensure_ascii=False)
    tmp.close()
    afa.CONFIG_PATH = tmp.name
    game = spawn_window(d3, root3)
    app = afa.App(afa.load_config())
    t = threading.Thread(target=app.run, daemon=True)
    t.start()
    attached = wait_attach(app)
    print(f"[1] auto_exit=true 挂载游戏窗口: {attached}")
    ok &= attached
    game.destroy()
    d3.flush()
    exited = wait_exit(app, t)
    print(f"[2] 窗口消失后守护自动退出: {exited}")
    ok &= exited

    # 2) auto_exit=False：窗口消失 → 守护继续等待
    tmp2 = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(make_cfg(False), tmp2, ensure_ascii=False)
    tmp2.close()
    afa.CONFIG_PATH = tmp2.name
    game = spawn_window(d3, root3)
    app = afa.App(afa.load_config())
    t = threading.Thread(target=app.run, daemon=True)
    t.start()
    attached = wait_attach(app)
    print(f"[3] auto_exit=false 挂载游戏窗口: {attached}")
    ok &= attached
    game.destroy()
    d3.flush()
    time.sleep(3)
    still = not app._stop.is_set()
    print(f"[4] 窗口消失后守护继续等待: {still}")
    ok &= attached and still
    app._stop.set()

    os.unlink(tmp.name)
    os.unlink(tmp2.name)
    print(f"== auto_exit 回归测试{'通过' if ok else '失败'} ==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
