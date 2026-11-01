#!/usr/bin/env bash
# 用途：向配置的 Telegram 聊天发送一条文本消息，可选附带一张本地图片。
# 用法：先配置本文件中的 token、聊天目标等信息，再执行 bash tg_send.sh "消息文本"。
# 附图用法：bash tg_send.sh --image "/path/image.png" "消息文本"。
# 多行用法：bash tg_send.sh $'第一行\n第二行'；传变量时使用 bash tg_send.sh "$message"。
# daily_tasks.sh 会调用本脚本发送报告，并根据本脚本的退出码判断发送是否成功。
# 路径：可从任意目录启动；相对图片路径以当前工作目录为准。
# 版本信息写入标准错误，标准输出仍为 Telegram 返回的 JSON。
#
# 获取会话 ID（Bot API 中叫 chat_id）：
# 1. 在 Telegram 私聊机器人，点击 Start 或发送 /start。
# 2. 浏览器访问以下 URL，将 <BOT_TOKEN> 替换为机器人 token：
#    https://api.telegram.org/bot<BOT_TOKEN>/getUpdates
# 3. 在 result 中找到刚发的消息，取 message.chat.id（不要取 message_id 或 update_id）。
# 4. 将完整数字（保留可能的负号）填入 DEFAULT_CHAT_ID，或设置 TG_CHAT_ID。
# 群聊：先把机器人加入群，在群内发送 /start@机器人用户名，再执行第 2、3 步。
# result 为空时先发一条新消息再试；若启用了 webhook，getUpdates 不可用。
# 官方说明：https://core.telegram.org/bots/api#getupdates
set -euo pipefail

SCRIPT_VERSION="v0.2.0"
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
  bash tg_send.sh --image '/path/image.png' '消息内容'
  printf '%s\n' '消息内容' | bash tg_send.sh --image '/path/image.png'

附图时最多指定一张可读的本地图片，消息作为图片说明（最多 1024 字符）。

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

image_path=''
message_args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --image)
            if [[ $# -lt 2 || -z "$2" || -n "$image_path" ]]; then
                printf '%s\n' '错误：--image 需要一个图片路径，且最多只能指定一张图片。' >&2
                exit 2
            fi
            image_path=$2
            shift 2
            ;;
        *)
            message_args+=("$1")
            shift
            ;;
    esac
done
set -- "${message_args[@]}"

if [[ -n "$image_path" && ( ! -f "$image_path" || ! -r "$image_path" ) ]]; then
    printf '错误：图片文件不存在或不可读：%s\n' "$image_path" >&2
    exit 2
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

if [[ -n "$image_path" ]]; then
    # curl 的文件路径需额外转义引号和反斜杠，避免逗号等被解析成多个文件。
    image_form_path=${image_path//\\/\\\\}
    image_form_path=${image_form_path//\"/\\\"}
    exec curl --silent --show-error --fail-with-body \
        --write-out '\n' \
        --connect-timeout 10 --max-time 30 \
        --request POST \
        --form-string "chat_id=${TG_CHAT_ID}" \
        --form-string "caption=${message}" \
        --form "photo=@\"${image_form_path}\"" \
        "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendPhoto"
fi

exec curl --silent --show-error --fail-with-body \
    --write-out '\n' \
    --connect-timeout 10 --max-time 30 \
    --request POST \
    --data-urlencode "chat_id=${TG_CHAT_ID}" \
    --data-urlencode "text=${message}" \
    "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage"
