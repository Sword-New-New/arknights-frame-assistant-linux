# AFA Linux —— 明日方舟帧操小助手（Linux / Proton 移植版）

把 Windows 上的 [arknights-frame-assistant](https://github.com/CloudTracey/arknights-frame-assistant)（AFA，明日方舟帧操小助手）的核心功能移植到 Linux 原生环境，供通过 Proton/Wine 运行明日方舟 PC 版时使用。

动作时序与功能规格逐一对照 AFA 源码移植（`upstream/` 内为参考用的上游源码克隆）。AFA 采用 **GPL-3.0-only** 许可证，本移植版为派生作品，同样以 **GPL-3.0-only** 发布。

## 原理

| AFA（Windows/AHK） | AFA Linux |
|---|---|
| `#HotIf WinActive("ahk_exe Arknights.exe")` 进程限定 | `XGrabKey`/`XGrabButton` 挂在游戏窗口上（仅游戏窗口持焦点/悬停时激活） |
| `Send`（SendInput）/ `mouse_event` / 触摸注入 | XTEST 注入（与 xdotool 同路径，Wine/Proton 会翻译给游戏） |
| `USleep`（QPC 忙等，<1ms） | `time.perf_counter` 忙等（实测误差 ≈0.02ms） |
| `LevelDetector` PixelSearch 三对象关卡检测 | XGetImage 三区域颜色采样（同区域、同颜色、同容差、同投票规则） |
| 触摸注入"不移动光标"的暂停三连击 | 移动→点击→复位等效实现 |

不注入游戏进程、不读游戏内存，与 Windows 版一样属于外部输入模拟。

## 安装与运行

```bash
# 依赖：Python 3.8+、python-xlib、tkinter（GUI 用，系统一般自带）。首次运行 start.sh 会自动装 python-xlib。
./start.sh            # 后台守护：热键生效（日志同写终端和 logs/afa.log）
./start.sh gui        # 设置界面（键位/延迟/守卫可视化编辑）
./start.sh --status   # 查看：游戏窗口/几何/焦点/守卫采样/键位表
./start.sh --selftest # 注入与抓取链路自检（不触碰游戏）
./start.sh --no-guard # 本次运行关闭关卡守卫
```

**设置界面**：四个标签页——常规作战（14 键位）、快捷操作（6 键位）、游戏功能键（5 个）、延迟与守卫。点按键按钮弹出捕获窗（按任意键绑定、Del 清除、可绑鼠标侧键），冲突标红；顶部实时显示游戏窗口与后台状态。「保存并应用」后后台守护 **2 秒内热重载**，无需重启。

**日志**：`logs/afa.log`（2MB 滚动 × 2 份），出问题时把这个文件发出来即可定位。每次触发、守卫拦截、抓取异常都有记录；`config.json` 里 `log_level` 改成 `DEBUG` 可看更细的跳过原因。

**测试**：`python3 tests/test_race.py`（触发状态机回归）、`python3 tests/test_e2e.py`（完整流水线端到端，含连按 6 次不衰减回归）、`python3 tests/test_reload.py`（配置热重载回归）。

- 与游戏无启动顺序要求：游戏没开时工具会等待，窗口出现后自动挂载热键；游戏关闭后自动解除。
- 需要 X11 会话（`echo $XDG_SESSION_TYPE` 应为 `x11`；XWayland 下理论可用但未验证）。

## 默认键位（与 AFA 默认一致，见 `config.json`）

| 触发键 | 功能 | 实际发给游戏的输入 |
|---|---|---|
| `F` | 按下时暂停 | `ESC` 点击 |
| `空格`（松开时） | 松开时暂停（模拟器手感） | `空格` 点击 |
| `D` | 切换倍速 | `F` 点击 |
| `W` | 暂停时选中 | 暂停键左半 → 光标处 → 暂停键右半 三连击 |
| `S` / `A` | 技能 / 撤退 | `E` / `Q` 点击 |
| `R` / `T` | 暂停时前进 33ms / 166ms | `ESC`↓ → 30/165ms → `空格`↓50ms → 都↑ |
| （默认未绑定） | 前进 16ms | 同上，16ms |
| `E` / `Q` | 一键技能 / 一键撤退 | 左键点击 → 90ms → `E` / `Q` |
| `XButton2` / `XButton1` | 暂停技能 / 暂停撤退 | 暂停三连击 → 技能 / 撤退键 |

键位全部在 `config.json` 的 `bindings` 里改（键名规则：字母小写、`Space`、`ESC`、`F8`、`XButton1/2` 等）。

## 配置要点（`config.json`）

- `frame_rate`：**改成游戏内实际帧率设置**（30/60/90/120/144/165/180/240+），影响暂停态动作里 `CurrentDelay*1.5` 的取值，与 AFA 的"帧率档位"同义。
- `game_keys`：游戏功能键（`releaseSkill`/`retreatChar`/`pauseBattle`/`changeSpeed`/`battleLeftPopup`）。AFA 会自动读游戏内按键设置，本版暂未实现注册表解析——**如果你改过游戏内键位，请手动同步这里**。
- `in_level_guard`：关卡守卫（默认开）。开时不在关卡内按 `E/Q/F/空格…` 会原样透传给游戏（菜单、输入框可用）；关时触发键在游戏窗口内一律被吞。
- `click_delay_ms` / `frame_skip_*_delay`：与 AFA 的 ClickDelay（默认 90）/ 过帧延迟（16/30/165）一致。

## 游戏内验证步骤（建议顺序）

1. `./start.sh --status` 确认窗口与守卫正常。
2. 运行 `./start.sh`，进入游戏主界面，按 `E`：**不应**触发技能（关卡守卫拦截并透传），且 `E` 的正常功能还在。
3. 进一场战斗，鼠标悬停任意已部署干员按 `E`：应选中并开技能（一键技能）。
4. 暂停（`F`），按 `R`：游戏应只前进约 1 帧（2 倍速下）；多按几次观察是否偶发跳 2 帧，若跳则把 `frame_skip_33ms_delay` 从 30 微调到 28~29（帧调度在 Proton 下与 Windows 略有差异）。
5. 暂停中按 `W`：应选中干员且不解除暂停。

## 已知差异与注意事项

- **比动作更快的连击**：上一次动作的注入窗口（约 90~150ms）内再次按下触发键，该次按下会直接透传给游戏（AFA 的 PureKeyWait 同样不允许动作叠加），属预期行为。
- **暂停三连击（W / 侧键 / 视角切换）**：AFA 用 Windows 触摸注入实现"不动光标"，本版用"移动+点击+复位"等效，点击目标相同但实现路径不同；如行为异常请反馈（这是最可能与原版有差异的功能）。
- **滚轮触发**暂不支持（AFA 支持把功能绑到滚轮）。
- **自动适配游戏内按键**暂未实现（需手动同步 `config.json` 的 `game_keys`）。
- 反作弊提醒与 Windows 版一致：本工具不注入、不修改游戏，但 AHK 类外部模拟输入存在被误判的小概率风险，游玩时保持警惕。
- 退出工具用 `Ctrl+C`（会自动解除所有键位抓取）。

## 目录

- `afa.py` — 主程序（单文件）
- `afa_gui.py` — 设置界面（tkinter）
- `config.json` — 首次运行自动生成（不入库，含个人键位）
- `start.sh` — 启动脚本（`gui` 参数打开设置界面）
- `logs/afa.log` — 运行日志（自动滚动，不入库）
- `tests/` — 回归测试（竞态状态机、端到端流水线、配置热重载）

上游 AFA 源码：<https://github.com/CloudTracey/arknights-frame-assistant>（GPL-3.0-only；本仓库的移植规格以该源码为准，需要时可自行克隆对照）。
