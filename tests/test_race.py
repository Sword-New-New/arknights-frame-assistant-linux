#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复现并验证"快速点击几次后失灵"的回归测试。

真实拓扑模拟：
  d1 = 工具的事件连接（抓取与事件读取）
  d2 = XTEST 注入连接（动作重放的真实注入）
  d3 = 模拟 Wine：拥有"游戏窗口"并持有输入焦点

根因：动作重放期间注入的同键"按下"会污染 keymap。旧版在处理物理 Release
时用 keymap 判断"是否真松开"，恰好撞上注入窗口就误判为"可检测自动重复"
而忽略释放 → held 残留 → 之后该键所有按下被当自动重复过滤（表现为失灵）。
修复：释放一律处理（自身连接未开启 detectable auto-repeat，X 不会伪造
Release），并以 keymap 对账兜底（reconcile）。

用法：python3 tests/test_race.py [--no-fix]   （--no-fix 模拟修复前的行为）
"""

import argparse
import queue
import sys
import time
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import afa
from Xlib import X, XK, display
from Xlib.ext import xtest


class RealInjectXH:
    """XHelper 替身：键/按钮注入真实走 XTEST（d2），其余只记录。"""

    def __init__(self, d2):
        self.d = d2            # 供 keysym 查询
        self.d2 = d2
        self.calls = []

    def _rec(self, *item):
        self.calls.append(item)

    def inject_key_raw(self, kc, down):
        self._rec("key", kc, down)
        xtest.fake_input(self.d2, X.KeyPress if down else X.KeyRelease, kc)
        self.d2.flush()

    def inject_button(self, b, down):
        self._rec("btn", b, down)
        xtest.fake_input(self.d2, X.ButtonPress if down else X.ButtonRelease, b)
        self.d2.flush()

    def move_pointer(self, x, y):
        self._rec("move", x, y)

    def client_rect(self, xid):
        return (0, 0, 3840, 2160)

    def pointer_pos(self):
        return (1000, 1000)

    def pointer_over_client(self, xid):
        return True


def pump(d1, mgr, duration, no_fix=False):
    """模拟工具的事件线程。no_fix=True 时复刻旧版：用 keymap 判断后忽略 Release。"""
    end = time.time() + duration
    while time.time() < end:
        while d1.pending_events():
            ev = d1.next_event()
            if ev.type == X.KeyPress:
                mgr.handle_key_press(ev.detail)
            elif ev.type == X.KeyRelease:
                km = d1.query_keymap()
                still = bool(km[ev.detail // 8] & (1 << (ev.detail % 8)))
                if no_fix:
                    if ev.detail in mgr.held and not still:   # 旧版语义
                        mgr.held.discard(ev.detail)
                else:
                    mgr.handle_key_release(ev.detail)          # 新版：一律处理
        time.sleep(0.01)


def wait_trigger(q, d1, mgr, timeout=2.0, no_fix=False):
    end = time.time() + timeout
    while time.time() < end:
        pump(d1, mgr, 0.05, no_fix)
        try:
            return q.popleft()
        except queue.Empty:
            pass
    return None


def main():
    logging_on = True
    import logging
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")

    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fix", action="store_true")
    args = ap.parse_args()

    d1 = display.Display()   # 工具
    d2 = display.Display()   # 注入
    d3 = display.Display()   # 模拟 Wine
    root3 = d3.screen().root

    game = root3.create_window(0, 0, 100, 100, 1, X.CopyFromParent, X.InputOutput,
                               visual=X.CopyFromParent,
                               background_pixel=d3.screen().black_pixel)
    game.map()
    d3.flush()
    time.sleep(0.2)
    d3.set_input_focus(game, X.RevertToPointerRoot, X.CurrentTime)
    d3.flush()
    time.sleep(0.2)

    cfg = dict(afa.DEFAULT_CONFIG)
    cfg["bindings"] = {k: ("e" if k == "one_click_skill" else "") for k in cfg["bindings"]}
    cfg["key_tap_delay_ms"] = 50
    cfg["click_delay_ms"] = 90

    xh = RealInjectXH(d2)
    guard = afa.LevelGuard(xh, cfg)
    guard._in_level = True
    act = afa.Actions(xh, cfg, guard)
    mgr = afa.HotkeyManager(d1, xh, cfg, act)
    if args.no_fix:
        mgr.reconcile = lambda: None   # 旧版没有对账
    mgr.install(game.id)

    kc_e = d1.keysym_to_keycode(XK.string_to_keysym("e"))
    ok = True

    # 1) 第一次按下：触发动作，动作开始执行（这里手动驱动其注入阶段）
    xtest.fake_input(d2, X.KeyPress, kc_e)
    d2.flush()
    r1 = wait_trigger(act.queue, d1, mgr, no_fix=args.no_fix)
    print(f"[1] 首次按下触发: {r1}")
    ok &= r1 and r1[0] == "one_click_skill"

    # 2) 动作注入阶段：同键真实注入"按下"（keymap 被污染），此时用户松开
    mgr.key_down(kc_e)
    xtest.fake_input(d2, X.KeyRelease, kc_e)   # 物理 Release 恰落在注入按下期间
    d2.flush()
    pump(d1, mgr, 0.3, no_fix=args.no_fix)
    print(f"[2] 注入期间物理释放后 held={mgr.held}  （残留=旧版卡键条件）")

    # 3) 注入抬起、恢复抓取
    mgr.key_up(kc_e)
    pump(d1, mgr, 0.1, no_fix=args.no_fix)

    # 4) 再次按下：修复后应正常触发；旧版被当自动重复过滤
    xtest.fake_input(d2, X.KeyPress, kc_e)
    d2.flush()
    r2 = wait_trigger(act.queue, d1, mgr, no_fix=args.no_fix)
    print(f"[4] 再次按下触发: {r2}  （{'修复生效' if r2 else '卡键复现'}）")
    ok &= r2 and r2[0] == "one_click_skill"

    # 收尾
    xtest.fake_input(d2, X.KeyRelease, kc_e)
    d2.flush()
    pump(d1, mgr, 0.2, no_fix=args.no_fix)
    mgr.uninstall()

    if args.no_fix:
        print("== 注：--no-fix 模拟的旧释放处理路径已从产品代码移除，此模式仅供历史参考，当前无法复现 ==")
        return 0
    print(f"== 回归测试{'通过' if ok else '失败'} ==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
