#!/usr/bin/env bash
#
# 失败自动重试的下载脚本
#
# 用法:
#   ./download.sh                         # 用下面的默认远程/本地路径
#   ./download.sh <远程路径> <本地路径>      # 临时换文件

DELAY=10        # 每次失败后等待的秒数
MAX_RETRY=64    # 最大重试次数，0 表示无限重试

RCLONE_CONF="$HOME/etc/rclone-extra/rclone.conf"

# Ctrl+C / kill 时立刻退出，不要被下面的重试循环接住
trap 'echo "[retry] 收到中断信号，退出" >&2; exit 130' INT TERM

# retry <命令> [参数...]
#   命令退出码非 0 就等 DELAY 秒重试，直到成功（或达到 MAX_RETRY）。
#   成功返回 0，放弃返回 1；被中断则直接退出整个脚本。
retry() {
    local n=0 rc
    while true; do
        "$@" && return 0
        rc=$?

        # 130=128+SIGINT(2)，143=128+SIGTERM(15)。rclone 会捕获 Ctrl+C
        # 做优雅退出再返回非 0，不排除掉的话循环会把它当成"下载失败"继续重试。
        if [ "$rc" -eq 130 ] || [ "$rc" -eq 143 ]; then
            echo "[retry] 命令被中断（退出码 $rc），不再重试" >&2
            exit "$rc"
        fi

        n=$((n + 1))
        if [ "$MAX_RETRY" -gt 0 ] && [ "$n" -ge "$MAX_RETRY" ]; then
            echo "[retry] 重试 $n 次仍失败，放弃: $1" >&2
            return 1
        fi
        echo "[retry] 第 $n 次失败，${DELAY}s 后重试..." >&2
        sleep "$DELAY"
    done
}

# ---------------- 以下是实际要跑的命令，按需修改 ----------------

REMOTE="${1:-terabox-default:/movies/Heavensward.mp4}"
LOCAL="${2:-$HOME/downloads/movies/Heavensward.mp4}"

retry rclone-extra copyto \
    "$REMOTE" \
    "$LOCAL" \
    --progress \
    --config "$RCLONE_CONF" || exit 1

echo "下载完成: $LOCAL"
