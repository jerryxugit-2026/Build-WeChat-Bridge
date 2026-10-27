#!/bin/zsh
# wechat-agent daemon 启动脚本模板（muse 后端 / 协议 v1.1，见 docs/wechat-agent-protocol.md）
# 首次运行：终端会显示二维码 URL，同时保存 PNG；用手机微信扫码绑定。
# 首次运行后取得自己的 iLink 用户 ID：
#   python3 -c "import json;print(json.load(open('$HOME/.local/share/wechatbridge/default/.ilink_state.json'))['userId'])"
# 然后填入下方 WECHATBRIDGE_ALLOWED_SENDERS 收紧白名单（强烈建议，不要把真实 ID 提交进仓库）。
set -euo pipefail
export WECHAT_AGENT_ROOT="${WECHAT_AGENT_ROOT:-$HOME/.wechat-agent}"
export WECHATBRIDGE_BACKEND="muse"
export WECHATBRIDGE_ALLOWED_SENDERS="${WECHATBRIDGE_ALLOWED_SENDERS:-YOUR_ALLOWED_SENDER@im.wechat}"
cd "$(dirname "$0")"
exec ./.venv/bin/wechatbridge
