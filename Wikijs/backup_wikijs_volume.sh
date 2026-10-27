#!/usr/bin/env bash
# 用途：停止 wiki-prod 容器，清理旧备份，打包数据库卷，再恢复容器启动。
# 用法：修改下方 BACKUP_DIR 等配置，然后执行 bash backup_wikijs_volume.sh。
# 可从任意工作目录执行；BACKUP_DIR 若为相对路径，以本脚本所在目录为基准。
# 流程：停止（最多等 120 秒）-> 等 30 秒 -> 保留两个旧备份 -> 备份 -> 等 30 秒 -> 启动。
# 备份文件为 tar.gz，文件名和日志时间均使用日本时间；成功后共保留三个备份。
# 退出码：停止、清理旧备份、归档、恢复启动等步骤全部成功才返回 0；失败或中断返回非零。
# 出错后仍会尝试恢复原先运行的容器，但恢复成功不会把本次失败改为成功。
# 运行环境：Linux，依赖 Docker、flock、GNU find/sort；当前账户需能操作 Docker。
set -Eeuo pipefail
umask 077

SCRIPT_VERSION="v0.1.0"
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)

# ---------- 按需修改这里 ----------
BACKUP_DIR="$HOME/wiki/backups/"
PROJECT_NAME="wiki-prod"
VOLUME_NAME="wiki-prod_db-data"
BACKUP_IMAGE="ubuntu:24.04"
STOP_TIMEOUT=120  # 最多等待 120 秒；超时后 Docker 会强制停止。
WAIT_SECONDS=30
KEEP_OLD=2  # 备份前保留两个旧文件；本次成功后共三个。

RUNNING_CONTAINERS=()
NEED_RESTART=0
TEMP_FILE=""
HELPER_CONTAINER="wikijs-volume-backup-$$-$(date +%s)"

log() {
    printf '[%s] %s\n' "$(TZ=Asia/Tokyo date '+%Y-%m-%d %H:%M:%S JST')" "$*"
}

# 停机前检查环境、准备镜像，并加锁防止脚本同时运行。
prepare() {
    if [[ -z "$BACKUP_DIR" ]]; then
        log "BACKUP_DIR 不能为空" >&2
        return 2
    fi
    command -v docker >/dev/null
    command -v flock >/dev/null
    docker info >/dev/null
    docker volume inspect "$VOLUME_NAME" >/dev/null

    if [[ "$BACKUP_DIR" != /* ]]; then
        BACKUP_DIR="$SCRIPT_DIR/$BACKUP_DIR"
    fi
    mkdir -p -- "$BACKUP_DIR"
    BACKUP_DIR=$(cd -- "$BACKUP_DIR" && pwd -P)
    exec 9>"$BACKUP_DIR/.wikijs-backup.lock"
    if ! flock -n 9; then
        log "已有备份任务运行，退出" >&2
        exit 1
    fi

    if ! docker image inspect "$BACKUP_IMAGE" >/dev/null 2>&1; then
        log "准备备份镜像 $BACKUP_IMAGE"
        docker pull "$BACKUP_IMAGE" >/dev/null
    fi

    local ids id
    ids=$(docker ps -q --filter "label=com.docker.compose.project=$PROJECT_NAME")
    while IFS= read -r id; do
        if [[ -n "$id" ]]; then
            RUNNING_CONTAINERS+=("$id")
        fi
    done <<< "$ids"
    if (( ${#RUNNING_CONTAINERS[@]} == 0 )); then
        log "没有找到正在运行的 $PROJECT_NAME 容器，退出" >&2
        exit 1
    fi
    TEMP_FILE=$(mktemp "$BACKUP_DIR/.wikijs-volume.XXXXXX")
}

# 只恢复备份前正在运行的容器。
start_cluster() {
    if (( NEED_RESTART )); then
        if docker start "${RUNNING_CONTAINERS[@]}" >/dev/null; then
            NEED_RESTART=0
            log "已经恢复启动"
        else
            log "恢复启动失败，请检查 $PROJECT_NAME 容器" >&2
            return 1
        fi
    fi
}

# 出错或收到 Ctrl+C / TERM 时也尝试恢复；保留原来的失败退出码。
cleanup() {
    local status=$?
    trap - EXIT INT TERM
    set +e
    docker rm -f "$HELPER_CONTAINER" >/dev/null 2>&1
    [[ -z "$TEMP_FILE" ]] || rm -f -- "$TEMP_FILE"
    if (( status != 0 )); then
        log "备份流程中断（退出码 $status）" >&2
    fi
    start_cluster || status=1
    exit "$status"
}

stop_cluster() {
    # 在 stop 前设置标志，确保只停止了一部分时也会尝试恢复。
    NEED_RESTART=1
    docker stop --timeout "$STOP_TIMEOUT" "${RUNNING_CONTAINERS[@]}" >/dev/null
    log "已经停止容器"
    sleep "$WAIT_SECONDS"

    # 防止外部操作重新启动容器后，继续打包正在写入的卷。
    local active
    active=$(docker ps -q --filter "volume=$VOLUME_NAME")
    if [[ -n "$active" ]]; then
        log "卷仍被运行中的容器使用，停止备份" >&2
        return 1
    fi
}

# 按修改时间从新到旧，只清理本目录的 wikijs_volume_* 普通文件。
prune_backups() {
    local item file kept=0
    find "$BACKUP_DIR" -maxdepth 1 -type f -name 'wikijs_volume_*' \
        -printf '%T@ %p\0' | sort -z -nr |
    while IFS= read -r -d '' item; do
        file=${item#* }  # 去掉 find 输出的修改时间，保留完整路径。
        if (( kept < KEEP_OLD )); then
            kept=$((kept + 1))
            continue
        fi
        rm -- "$file"
        log "删除文件 ${file##*/}"
    done
}

backup_volume() {
    local filename destination
    filename="wikijs_volume_jst_$(TZ=Asia/Tokyo date '+%Y%m%d_%H%M%S').tar.gz"
    destination="$BACKUP_DIR/$filename"
    if [[ -e "$destination" ]]; then
        log "备份文件已存在：$destination" >&2
        return 1
    fi

    # tar 将压缩数据输出到标准输出，由宿主机写入临时文件。
    # 卷只读挂载，归档保留数字 UID/GID；实际格式为 tar.gz，不是 zip。
    docker run --rm --name "$HELPER_CONTAINER" \
        -v "$VOLUME_NAME:/data:ro" \
        "$BACKUP_IMAGE" \
        tar --numeric-owner -czf - -C /data . > "$TEMP_FILE"

    mv -- "$TEMP_FILE" "$destination"
    TEMP_FILE=""
    log "备份得到文件 $filename，完整路径是 $destination"
    sleep "$WAIT_SECONDS"
}

main() {
    log "${BASH_SOURCE[0]##*/} $SCRIPT_VERSION"
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    prepare
    stop_cluster
    prune_backups
    backup_volume
    start_cluster
    return 0
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
