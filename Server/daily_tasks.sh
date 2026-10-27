#!/usr/bin/env bash
# 用途：顺序执行多个 Bash 任务，合并保存输出，并通过 tg_send.sh 发送每日报告。
# 用法：修改下方 SCRIPTS、LOG_DIR、TG_SEND_SCRIPT，然后执行 bash daily_tasks.sh。
# 路径：可从任意目录启动；上述配置中的相对路径均相对于本脚本所在目录。
# 每个任务在自身目录执行；退出码 0 计为成功，非零计为失败，失败后继续后续任务。
# 相邻任务之间等待 30 秒（TASK_INTERVAL）；最后一个任务结束后直接发送报告。
# 通知顺序：今天是否全部成功 -> 日期时间 -> 成功/总数和失败数 -> 详细日志。
# 标准输出和错误输出写入同一日志；Telegram 消息保留换行，长日志自动分段。
# 日志默认保存在本脚本所在目录的 logs/daily_tasks/，可通过 LOG_DIR 修改。
# 每次运行新建 daily_tasks_jst_YYYYMMDD_HHMMSS_进程ID.log，文件名使用日本时间。
# 日志不会自动清理，会持续累积；需要时请手动删除或另行配置清理任务。
# 退出码：任务全部成功且通知发送成功才返回 0；任务/通知/准备步骤失败均为非零。
# 运行环境：Linux，依赖 Bash、flock、C.UTF-8 locale，以及各任务自身的依赖。
# 定时示例（crontab -e，替换为实际路径）：
# 0 3 * * * /bin/bash /absolute/path/daily_tasks.sh
# 上面是服务器时区的每日 03:00；本脚本的日志和通知使用日本时间。
set -uo pipefail
umask 077

SCRIPT_VERSION="v0.1.0"
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P) || exit 2

# ---------- 按需修改这里 ----------
# 可使用绝对路径，或相对于本脚本目录的路径；按数组顺序执行。
SCRIPTS=(
    "$HOME/wiki/backup_wikijs_volume.sh"
    "$HOME/scripts/check_https_certs.sh"
)
LOG_DIR="logs/daily_tasks"
TG_SEND_SCRIPT="$HOME/scripts/tg_send.sh"
TASK_INTERVAL=30  # 相邻任务之间等待的秒数。

LOG_FILE=""
SUCCESS_COUNT=0
FAILED_COUNT=0

now_jst() {
    TZ=Asia/Tokyo date '+%Y-%m-%d %H:%M:%S JST'
}

log() {
    printf '[%s] %s\n' "$(now_jst)" "$*"
}

# 只转换配置路径的基准，不要求目标目录已经存在。
resolve_path() {
    [[ -n "$1" ]] || return 2
    if [[ "$1" == /* ]]; then
        printf '%s\n' "$1"
    else
        printf '%s/%s\n' "$SCRIPT_DIR" "$1"
    fi
}

prepare() {
    if (( ${#SCRIPTS[@]} == 0 )); then
        log "请先填写 SCRIPTS" >&2
        return 2
    fi
    LOG_DIR=$(resolve_path "$LOG_DIR") || return 2
    TG_SEND_SCRIPT=$(resolve_path "$TG_SEND_SCRIPT") || return 2
    if [[ ! -f "$TG_SEND_SCRIPT" || ! -r "$TG_SEND_SCRIPT" ]]; then
        log "请为 TG_SEND_SCRIPT 配置可读取的脚本路径" >&2
        return 2
    fi
    if ! command -v flock >/dev/null; then
        log "缺少 flock 命令" >&2
        return 2
    fi

    mkdir -p -- "$LOG_DIR" || return 2
    LOG_DIR=$(cd -- "$LOG_DIR" && pwd -P) || return 2
    exec 9>"$LOG_DIR/.daily_tasks.lock" || return 2
    if ! flock -n 9; then
        log "上一次每日任务仍在运行，退出" >&2
        return 1
    fi

    LOG_FILE="$LOG_DIR/daily_tasks_jst_$(TZ=Asia/Tokyo date '+%Y%m%d_%H%M%S')_$$.log"
    : > "$LOG_FILE" || return 2
}

# 每个任务使用独立 Bash，在自己的目录执行，stdout/stderr 都进入日志。
run_task() {
    local script=$1 result=0
    script=$(resolve_path "$script") || script=""
    log "开始执行：$script"
    if [[ ! -f "$script" || ! -r "$script" ]]; then
        log "任务路径无效：需要可读取的脚本路径"
        result=127
    elif (cd -- "$(dirname -- "$script")" && bash -- "$script"); then
        result=0
    else
        result=$?
    fi

    if (( result == 0 )); then
        SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
        log "执行成功：${script##*/}"
    else
        FAILED_COUNT=$((FAILED_COUNT + 1))
        log "执行失败：${script##*/}，退出码 $result"
    fi
    printf '\n'
}

# 发送器接口：tg_send.sh "消息文本"，应按纯文本发送，失败时返回非零。
# 分段读取日志，保留换行；每段 1800 个字符，给标题及 emoji 留出长度余量。
send_report() {
    local LC_ALL=C.UTF-8
    local summary chunk message part=0 overall="今天的任务全部成功"
    if (( FAILED_COUNT > 0 )); then
        overall="今天的任务有失败"
    fi
    printf -v summary '%s\n时间：%s\n成功：%s/%s，失败：%s' \
        "$overall" "$(now_jst)" "$SUCCESS_COUNT" "${#SCRIPTS[@]}" "$FAILED_COUNT"

    while IFS= read -r -N 1800 chunk || [[ -n "$chunk" ]]; do
        part=$((part + 1))
        if (( part > 1 )); then
            sleep 3
        fi
        printf -v message '%s\n\n日志（第 %s 段）：\n%s' "$summary" "$part" "$chunk"
        # 双引号确保整段消息作为一个参数传入，真实换行也会保留。
        if ! (cd -- "$(dirname -- "$TG_SEND_SCRIPT")" && \
            bash -- "$TG_SEND_SCRIPT" "$message") >/dev/null; then
            log "Telegram 第 $part 段发送失败，完整日志：$LOG_FILE" >&2
            return 1
        fi
    done < "$LOG_FILE"
}

main() {
    log "daily_tasks.sh $SCRIPT_VERSION"
    prepare || return $?
    local script remaining=${#SCRIPTS[@]}
    log "每日任务开始（daily_tasks.sh $SCRIPT_VERSION），共 ${#SCRIPTS[@]} 个脚本" >> "$LOG_FILE" || return 1
    for script in "${SCRIPTS[@]}"; do
        run_task "$script" >> "$LOG_FILE" 2>&1 || return 1
        remaining=$((remaining - 1))
        if (( remaining > 0 )); then
            sleep "$TASK_INTERVAL" >> "$LOG_FILE" 2>&1 || return 1
        fi
    done
    log "全部执行结束，成功 $SUCCESS_COUNT/${#SCRIPTS[@]}，失败 $FAILED_COUNT" >> "$LOG_FILE" || return 1

    if ! send_report; then
        log "Telegram 通知发送失败" >> "$LOG_FILE"
        return 1
    fi
    log "每日任务结束，成功 $SUCCESS_COUNT/${#SCRIPTS[@]}，日志：$LOG_FILE"
    (( FAILED_COUNT == 0 ))
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
