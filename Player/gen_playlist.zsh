#!/usr/bin/env zsh
# 仅支持 WebDAV 的 HTTP(S) URL；文件夹不递归。输出 UTF-8 .m3u8，供 IINA/VLC 使用。
# 用法：修改配置后运行 zsh gen_playlist.zsh。Python 核心保存在本脚本同目录。

SCRIPT_VERSION='0.1.0'

# ===== 配置开始 =====
CORE_URL='https://raw.githubusercontent.com/GZhonghui/Tools/refs/heads/main/Player/gen_playlist_core.py'  # gen_playlist_core.py 的 HTTP(S) 直链；留空则使用同目录已有文件
# 先调用环境激活函数，再执行 PYTHON_BIN；以函数返回后此变量的值为准。
# 填解释器绝对路径时固定使用该解释器；填 python/python3 时按激活后的 PATH 查找。
PYTHON_BIN='python'  # Python 3.10+；可以改为 python3 或解释器的完整路径（不要附加参数）
# 文件和文件夹 URL 可以混合填写，核心脚本会自动识别，无需额外标记。
# 依据 WebDAV PROPFIND 返回的 resourcetype/collection 属性判断；文件夹只读取当前层。
WEBDAV_URLS=(
    'http://127.0.0.1:8081/upload/downloaded_video/'
    # 'https://user:password@example.com/dav/video.mp4'
)
OUTPUT_DIR="$HOME/Downloads"
OUTPUT_NAME='playlist.m3u8'  # 可以省略 .m3u8
TIMEOUT_SECONDS=30
DOWNLOAD_TIMEOUT_SECONDS=60

activate_python_env() {
    # 按需自行填写；默认不激活环境，直接使用 PYTHON_BIN。
    # 例如：source /path/to/venv/bin/activate
    :
}
# ===== 配置结束 =====

emulate -LR zsh
setopt ERR_EXIT NO_UNSET PIPE_FAIL
print -r -- "gen_playlist.zsh 版本：$SCRIPT_VERSION"
playlist_fail() { print -u2 -r -- "运行失败：$*"; exit 1; }
typeset playlist_dir="${0:A:h}" playlist_temp=''
typeset playlist_core="$playlist_dir/gen_playlist_core.py"
trap '[[ -z "$playlist_temp" ]] || rm -f -- "$playlist_temp"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ -n "$CORE_URL" ]]; then
    [[ "$CORE_URL" == http://* || "$CORE_URL" == https://* ]] || playlist_fail 'CORE_URL 必须是 HTTP(S) 直链。'
    (( $+commands[curl] )) || playlist_fail '下载核心脚本需要 curl。'
    playlist_temp=$(mktemp "$playlist_dir/.gen_playlist_core.XXXXXX")
    print -r -- '正在更新 Python 核心脚本……'
    curl --disable --fail --silent --show-error --location --max-redirs 5 \
        --proto '=http,https' --proto-redir '=http,https' \
        --max-time "$DOWNLOAD_TIMEOUT_SECONDS" --output "$playlist_temp" --url "$CORE_URL" \
        || playlist_fail '下载失败，已有核心脚本保持不变。'
elif [[ ! -f "$playlist_core" ]]; then
    playlist_fail '请填写 CORE_URL，或将 gen_playlist_core.py 放到本脚本同目录。'
fi

activate_python_env
command -v -- "$PYTHON_BIN" >/dev/null || playlist_fail '找不到 PYTHON_BIN 指定的解释器，请修改配置或环境激活函数。'

# 使用选定的解释器检查下载内容；成功后才原子替换旧文件，不执行半份下载。
"$PYTHON_BIN" -c '
import os, sys
from pathlib import Path
if sys.version_info < (3, 10):
    raise SystemExit("需要 Python 3.10 或更新版本。")
if sys.argv[1]:
    source = Path(sys.argv[1])
    data = source.read_bytes()
    if not data.strip():
        raise SystemExit("下载的核心脚本为空。")
    compile(data, "gen_playlist_core.py", "exec")
    os.replace(source, sys.argv[2])
' "$playlist_temp" "$playlist_core" || playlist_fail 'Python 检查或更新失败，未运行核心脚本。'

"$PYTHON_BIN" "$playlist_core" --output-dir "$OUTPUT_DIR" --name "$OUTPUT_NAME" \
    --timeout "$TIMEOUT_SECONDS" -- "${WEBDAV_URLS[@]}"
