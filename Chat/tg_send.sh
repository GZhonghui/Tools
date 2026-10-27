#!/usr/bin/env bash
# 用途：向配置的 Telegram 聊天发送第一个参数中的消息文本。
# 用法：先配置本文件中的 token、聊天目标等信息，再执行 bash tg_send.sh "消息文本"。
# 多行用法：bash tg_send.sh $'第一行\n第二行'；传变量时使用 bash tg_send.sh "$message"。
# daily_tasks.sh 会调用本脚本发送报告，并根据本脚本的退出码判断发送是否成功。
# 路径：不读取相对路径文件，可从任意目录启动。
# 版本信息写入标准错误，标准输出仍为 Telegram 返回的 JSON。
set -euo pipefail

SCRIPT_VERSION="v0.1.0"
printf 'tg_send.sh %s\n' "$SCRIPT_VERSION" >&2

# 在此填写配置；留空时分别读取 TG_BOT_TOKEN、TG_CHAT_ID 环境变量。
# 脚本内配置优先于环境变量。
DEFAULT_BOT_TOKEN=''
DEFAULT_CHAT_ID=''

usage() {
    cat <<'EOF'
用法：
  bash tg_send.sh '消息内容'
  printf '%s\n' '消息内容' | bash tg_send.sh

配置：优先使用脚本顶部的 DEFAULT_BOT_TOKEN、DEFAULT_CHAT_ID。
任一配置留空时，读取对应的 TG_BOT_TOKEN、TG_CHAT_ID 环境变量：
  TG_BOT_TOKEN='你的 token' TG_CHAT_ID='你的会话 ID' bash tg_send.sh '消息内容'

依赖：Bash、curl 7.76.0 或更新版本。
成功时输出 Telegram 返回的 JSON；失败时返回非零退出码。
EOF
}

if [[ $# -eq 1 && ( $1 == --help || $1 == -h ) ]]; then
    usage
    exit 0
fi

if [[ $# -gt 1 || ( $# -eq 0 && -t 0 ) ]]; then
    usage >&2
    exit 2
fi

TG_BOT_TOKEN="${DEFAULT_BOT_TOKEN:-${TG_BOT_TOKEN:-}}"
TG_CHAT_ID="${DEFAULT_CHAT_ID:-${TG_CHAT_ID:-}}"
: "${TG_BOT_TOKEN:?请填写 DEFAULT_BOT_TOKEN 或设置 TG_BOT_TOKEN 环境变量}"
: "${TG_CHAT_ID:?请填写 DEFAULT_CHAT_ID 或设置 TG_CHAT_ID 环境变量}"

if [[ $# -eq 1 ]]; then
    message=$1
else
    message=$(cat)
fi

if [[ -z $message ]]; then
    printf '%s\n' '错误：消息内容不能为空。' >&2
    exit 2
fi

exec curl --silent --show-error --fail-with-body \
    --connect-timeout 10 --max-time 30 \
    --request POST \
    --data-urlencode "chat_id=${TG_CHAT_ID}" \
    --data-urlencode "text=${message}" \
    "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage"
