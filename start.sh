#!/usr/bin/env bash
# AFA Linux 启动脚本：自动补装依赖后运行主程序
#   ./start.sh          后台守护（热键生效）
#   ./start.sh gui      设置界面
#   ./start.sh --status / --selftest / --no-guard  同 afa.py
cd "$(dirname "$0")" || exit 1

if ! python3 -c "import Xlib" 2>/dev/null; then
    echo "python-xlib 未安装，尝试安装到用户目录（~/.local）..."
    pip3 install --user --break-system-packages python-xlib \
        || https_proxy=http://127.0.0.1:7897 http_proxy=http://127.0.0.1:7897 \
           pip3 install --user --break-system-packages python-xlib \
        || { echo "安装失败，请手动执行: pip3 install --user --break-system-packages python-xlib"; exit 1; }
fi

if [ "${1:-}" = "gui" ]; then
    exec python3 afa_gui.py
fi

exec python3 afa.py "$@"
