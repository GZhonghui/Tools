#!/usr/bin/env bash
# 用途：向配置的 Telegram 聊天发送文本、图片或文件；图片和文件的说明文字可省略。
# 准备：填写下方 DEFAULT_BOT_TOKEN、DEFAULT_CHAT_ID，或设置 TG_BOT_TOKEN、TG_CHAT_ID。
#
# 常用用法（在脚本所在目录执行；也可使用脚本的绝对路径）：
#   纯文本：      bash tg_send.sh '消息文本'
#   多行文本：    bash tg_send.sh $'第一行\n第二行'
#   变量文本：    bash tg_send.sh "$message"
#   仅图片：      bash tg_send.sh --image '/path/image.png'
#   图片及说明：  bash tg_send.sh --image '/path/image.png' '图片说明'
#   仅文件：      bash tg_send.sh --file '/path/report.pdf'
#   文件及说明：  bash tg_send.sh --file '/path/report.pdf' '文件说明'
#   管道文本：    printf '%s\n' '消息文本' | bash tg_send.sh
#   管道图片说明：printf '%s\n' '图片说明' | bash tg_send.sh --image '/path/image.png'
#   管道文件说明：printf '%s\n' '文件说明' | bash tg_send.sh --file '/path/report.pdf'
#   查看帮助：    bash tg_send.sh --help
#
# 临时凭据（下方 DEFAULT_BOT_TOKEN、DEFAULT_CHAT_ID 留空时使用）：
#   TG_BOT_TOKEN='你的 token' TG_CHAT_ID='你的会话 ID' bash tg_send.sh '消息文本'
# 这两个变量只对本次命令生效，不需要 export，也不需要执行后 unset。
# 不会在当前 shell 中留下新变量；若原来已有同名变量，其原值保持不变。
#
# 从 env 文件加载（下方 DEFAULT_BOT_TOKEN、DEFAULT_CHAT_ID 留空时使用）：
#   ./tg.env 文件内容（等号两边不要加空格）：
#     TG_BOT_TOKEN='你的 token'
#     TG_CHAT_ID='你的会话 ID'
#   加载并发送（一行）：
#     (set -a; source ./tg.env && bash tg_send.sh '消息文本')
# set -a 将文件中的普通赋值导出为环境变量，供脚本读取，env 文件里不需要 export。
# 括号在子 shell 中执行，结束后当前 shell 的变量和选项保持原样；加载失败时不执行脚本。
#
# 输入规则：最多传一个文本参数，含空格或换行时需加引号；文本参数优先于标准输入。
# 未传文本参数时，若标准输入被管道或重定向连接，则读到 EOF 作为文本/说明。
# 在终端直接发送附件时不等待输入；也可传空字符串 '' 明确不带说明并跳过标准输入。
# 无附件时文本不能为空。每次最多一个附件，--image 与 --file 不能重复或同时使用。
# 附件必须是可读的本地普通文件；相对附件路径以当前工作目录为准。
#
# 默认限制：图片 1 MB，文件 2 MB（1 MB = 1048576 字节，等于上限允许发送）。
# 文件格式默认只允许 pdf，按最后一个扩展名检查且忽略大小写，不检查文件内容。
# 纯文本最多 4096 个 UTF-8 字符，图片/文件说明最多 1024 个字符。
# 修改下方 DEFAULT_* 可调整限制，或用同名 TG_* 环境变量覆盖，例如：
#   TG_MAX_IMAGE_SIZE_MB=2 bash tg_send.sh --image '/path/image.png'
#   TG_MAX_FILE_SIZE_MB=3 TG_ALLOWED_FILE_EXTENSIONS='pdf,txt' bash tg_send.sh --file './note.txt'
#   TG_MAX_TEXT_LENGTH=2048 bash tg_send.sh '消息文本'
#   TG_MAX_CAPTION_LENGTH=512 bash tg_send.sh --file '/path/report.pdf' '文件说明'
# 大小配置为正整数 MB；文本/说明上限可调低，但不能超过上述 Telegram 上限。
#
# 校验失败时在本地报错并返回 2，不发请求；其他发送失败也返回非零退出码。
# 版本和错误写入标准错误，发送时标准输出为 Telegram 返回的 JSON。
# daily_tasks.sh 会调用本脚本发送报告，并根据本脚本的退出码判断发送是否成功。
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

SCRIPT_VERSION="v0.3.2"
printf 'tg_send.sh %s\n' "$SCRIPT_VERSION" >&2

# 在此填写配置；留空时分别读取 TG_BOT_TOKEN、TG_CHAT_ID 环境变量。
# 脚本内配置优先于环境变量。
DEFAULT_BOT_TOKEN=''
DEFAULT_CHAT_ID=''

# 附件和文本的默认限制；对应 TG_* 环境变量可覆盖以下 DEFAULT_* 配置。
# 大小使用正整数 MB，1 MB = 1024 * 1024 字节，等于上限时允许发送。
DEFAULT_MAX_FILE_SIZE_MB=2
DEFAULT_MAX_IMAGE_SIZE_MB=1
# 文件格式按最后一个扩展名检查，不检查文件内容；不区分大小写。
# 多个扩展名用逗号分隔，不带点，例如 'pdf,txt,zip'。
DEFAULT_ALLOWED_FILE_EXTENSIONS='pdf'
# Telegram Bot API 的硬上限：纯文本 4096 字符，图片/文件说明 1024 字符。
# https://core.telegram.org/bots/api#sendmessage
# https://core.telegram.org/bots/api#senddocument
DEFAULT_MAX_TEXT_LENGTH=4096
DEFAULT_MAX_CAPTION_LENGTH=1024

usage() {
    cat <<'EOF'
用法：
  bash tg_send.sh '消息内容'
  printf '%s\n' '消息内容' | bash tg_send.sh
  bash tg_send.sh --image '/path/image.png'
  bash tg_send.sh --image '/path/image.png' '消息内容'
  printf '%s\n' '消息内容' | bash tg_send.sh --image '/path/image.png'
  bash tg_send.sh --file '/path/report.pdf'
  bash tg_send.sh --file '/path/report.pdf' '消息内容'
  printf '%s\n' '消息内容' | bash tg_send.sh --file '/path/report.pdf'

每次最多指定一张图片或一个文件，--image 与 --file 不能同时使用。
附件必须是可读的本地普通文件；有附件时说明文字可省略，纯文本不能为空。
文本参数优先；省略时读取管道/重定向输入，在终端直接发送附件则不等待输入。
可显式传入空字符串 ''，表示不带附件说明并跳过标准输入。
默认图片最多 1 MB，文件最多 2 MB（1 MB = 1048576 字节）。
文件扩展名默认只允许 pdf，不区分大小写；不检查文件内容。
纯文本默认最多 4096 个 UTF-8 字符，附件说明默认最多 1024 个字符。
不符合限制时在本地报错并返回 2，不发送任何请求。

凭据：优先使用脚本顶部的 DEFAULT_BOT_TOKEN、DEFAULT_CHAT_ID。
任一配置留空时，读取对应的 TG_BOT_TOKEN、TG_CHAT_ID 环境变量：
  TG_BOT_TOKEN='你的 token' TG_CHAT_ID='你的会话 ID' bash tg_send.sh '消息内容'
这种写法只为本次命令设置变量，不需要 export 或执行后 unset。
命令结束后，当前 shell 不会留下新变量；原有同名变量保持原值。

也可从 env 文件加载（对应 DEFAULT_* 凭据配置需留空）。./tg.env 内容：
  TG_BOT_TOKEN='你的 token'
  TG_CHAT_ID='你的会话 ID'
等号两边不要加空格，文件中无需 export。一行加载并发送：
  (set -a; source ./tg.env && bash tg_send.sh '消息内容')
set -a 将赋值导出给脚本；括号创建子 shell，结束后当前 shell 的变量和选项保持原样。
source 加载失败时不会执行脚本。发送附件时，将最后的调用改成相应 --image / --file 用法即可。

限制：可修改脚本顶部的 DEFAULT_*，或使用以下环境变量覆盖：
  TG_MAX_FILE_SIZE_MB=2              文件上限，正整数 MB
  TG_MAX_IMAGE_SIZE_MB=1             图片上限，正整数 MB
  TG_ALLOWED_FILE_EXTENSIONS=pdf     扩展名白名单，逗号分隔，不带点
  TG_MAX_TEXT_LENGTH=4096            纯文本上限，1～4096 字符
  TG_MAX_CAPTION_LENGTH=1024         附件说明上限，1～1024 字符
例如：TG_ALLOWED_FILE_EXTENSIONS='pdf,txt' bash tg_send.sh --file './note.txt' '备注'

依赖：Bash、基础命令 cat/wc/tr，以及 curl（7.76.0 或更新版本）。
成功时输出 Telegram 返回的 JSON；失败时返回非零退出码。
EOF
}

# 先检查数字的长度，再做算术比较，防止超大配置值溢出。
validate_limit() {
    local name=$1 value=$2 maximum=$3
    if [[ ! $value =~ ^[1-9][0-9]*$ || ${#value} -gt ${#maximum} ]] || (( value > maximum )); then
        printf '错误：%s 必须为 1～%s 的整数。\n' "$name" "$maximum" >&2
        exit 2
    fi
}

if [[ $# -eq 1 && ( $1 == --help || $1 == -h ) ]]; then
    usage
    exit 0
fi

attachment_path=''
attachment_option=''
message_args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --image|--file)
            if [[ $# -lt 2 || -z "$2" ]]; then
                printf '错误：%s 需要一个本地文件路径。\n' "$1" >&2
                exit 2
            fi
            if [[ -n "$attachment_option" ]]; then
                printf '%s\n' '错误：每次最多指定一个附件，--image 与 --file 不能重复或同时使用。' >&2
                exit 2
            fi
            attachment_option=$1
            attachment_path=$2
            shift 2
            ;;
        --)
            shift
            message_args+=("$@")
            break
            ;;
        *)
            message_args+=("$1")
            shift
            ;;
    esac
done
set -- "${message_args[@]}"

if [[ -n "$attachment_path" ]]; then
    if [[ ! -f "$attachment_path" || ! -r "$attachment_path" ]]; then
        printf '错误：附件不存在、不是普通文件或不可读：%s\n' "$attachment_path" >&2
        exit 2
    fi

    if [[ $attachment_option == --image ]]; then
        attachment_label='图片'
        attachment_field='photo'
        api_method='sendPhoto'
        size_config='TG_MAX_IMAGE_SIZE_MB'
        max_size_mb=${TG_MAX_IMAGE_SIZE_MB-$DEFAULT_MAX_IMAGE_SIZE_MB}
    else
        attachment_label='文件'
        attachment_field='document'
        api_method='sendDocument'
        size_config='TG_MAX_FILE_SIZE_MB'
        max_size_mb=${TG_MAX_FILE_SIZE_MB-$DEFAULT_MAX_FILE_SIZE_MB}

        allowed_extensions=${TG_ALLOWED_FILE_EXTENSIONS-$DEFAULT_ALLOWED_FILE_EXTENSIONS}
        allowed_extensions=$(printf '%s' "$allowed_extensions" | LC_ALL=C tr '[:upper:]' '[:lower:]')
        allowed_extensions=${allowed_extensions//[[:space:]]/}
        if [[ ! $allowed_extensions =~ ^[a-z0-9]+(,[a-z0-9]+)*$ ]]; then
            printf '%s\n' '错误：TG_ALLOWED_FILE_EXTENSIONS 必须是逗号分隔的扩展名，例如 pdf,txt（不带点）。' >&2
            exit 2
        fi

        file_name=${attachment_path##*/}
        file_extension=''
        if [[ $file_name == *.* ]]; then
            file_extension=${file_name##*.}
            file_extension=$(printf '%s' "$file_extension" | LC_ALL=C tr '[:upper:]' '[:lower:]')
        fi
        if [[ ! $file_extension =~ ^[a-z0-9]+$ || ",$allowed_extensions," != *",$file_extension,"* ]]; then
            printf '错误：文件格式不允许：%s；允许的扩展名：%s。\n' "$attachment_path" "$allowed_extensions" >&2
            exit 2
        fi
    fi

    # 64 位整数能安全换算为字节的最大 MB 数。
    validate_limit "$size_config" "$max_size_mb" 8796093022207
    max_size_bytes=$((max_size_mb * 1024 * 1024))
    if ! attachment_size=$(wc -c < "$attachment_path"); then
        printf '错误：无法读取附件大小：%s\n' "$attachment_path" >&2
        exit 2
    fi
    if (( attachment_size > max_size_bytes )); then
        printf '错误：%s大小 %s 字节，超过 %s MB（%s 字节）的上限：%s\n' \
            "$attachment_label" "$attachment_size" "$max_size_mb" "$max_size_bytes" "$attachment_path" >&2
        exit 2
    fi
fi

if [[ $# -gt 1 || ( $# -eq 0 && -t 0 && -z "$attachment_path" ) ]]; then
    usage >&2
    exit 2
fi

if [[ $# -eq 1 ]]; then
    message=$1
elif [[ ! -t 0 ]]; then
    message=$(cat)
else
    message=''
fi

if [[ -z $message && -z "$attachment_path" ]]; then
    printf '%s\n' '错误：无附件时消息内容不能为空。' >&2
    exit 2
fi

if [[ -n "$attachment_path" ]]; then
    max_message_length=${TG_MAX_CAPTION_LENGTH-$DEFAULT_MAX_CAPTION_LENGTH}
    validate_limit TG_MAX_CAPTION_LENGTH "$max_message_length" 1024
    message_label='附件说明'
else
    max_message_length=${TG_MAX_TEXT_LENGTH-$DEFAULT_MAX_TEXT_LENGTH}
    validate_limit TG_MAX_TEXT_LENGTH "$max_message_length" 4096
    message_label='文本'
fi
# 每个 UTF-8 字符恰有一个非续字节；即使在 cron 的 C locale 下也能正确计数。
message_length=$(printf '%s' "$message" | LC_ALL=C tr -d '\200-\277' | wc -c)
if (( message_length > max_message_length )); then
    printf '错误：%s长度 %s 字符，超过 %s 字符的上限。\n' \
        "$message_label" "$message_length" "$max_message_length" >&2
    exit 2
fi

TG_BOT_TOKEN="${DEFAULT_BOT_TOKEN:-${TG_BOT_TOKEN:-}}"
TG_CHAT_ID="${DEFAULT_CHAT_ID:-${TG_CHAT_ID:-}}"
: "${TG_BOT_TOKEN:?请填写 DEFAULT_BOT_TOKEN 或设置 TG_BOT_TOKEN 环境变量}"
: "${TG_CHAT_ID:?请填写 DEFAULT_CHAT_ID 或设置 TG_CHAT_ID 环境变量}"

if [[ -n "$attachment_path" ]]; then
    attachment_form_args=(--form-string "chat_id=${TG_CHAT_ID}")
    if [[ -n "$message" ]]; then
        attachment_form_args+=(--form-string "caption=${message}")
    fi
    # curl 将单独的 - 解释为标准输入，需明确指向已校验的本地文件。
    attachment_form_path=$attachment_path
    if [[ $attachment_form_path == - ]]; then
        attachment_form_path='./-'
    fi
    # curl 的文件路径需额外转义引号和反斜杠，避免逗号等被解析成多个文件。
    attachment_form_path=${attachment_form_path//\\/\\\\}
    attachment_form_path=${attachment_form_path//\"/\\\"}
    exec curl --silent --show-error --fail-with-body \
        --write-out '\n' \
        --connect-timeout 10 --max-time 30 \
        --request POST \
        "${attachment_form_args[@]}" \
        --form "${attachment_field}=@\"${attachment_form_path}\"" \
        "https://api.telegram.org/bot${TG_BOT_TOKEN}/${api_method}"
fi

exec curl --silent --show-error --fail-with-body \
    --write-out '\n' \
    --connect-timeout 10 --max-time 30 \
    --request POST \
    --data-urlencode "chat_id=${TG_CHAT_ID}" \
    --data-urlencode "text=${message}" \
    "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage"
