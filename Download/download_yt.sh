#!/usr/bin/env bash
# 支持 YouTube 和 Bilibili：从视频 URL 下载音频或视频，
# 保存到配置的下载目录，并按开关上传到 rclone。
# 兼容 Bash 3.2 及以上版本（包括 macOS 自带的 Bash）。

# ---- 配置：运行前按需修改 ----
# 音频和视频共用下载目录；不存在时自动创建。相对路径以脚本执行位置为基准。
readonly download_directory="$HOME/Downlaods"

# 两种下载类型共享上传开关；关闭后不检查或调用 rclone。
readonly upload_enabled=true
readonly audio_upload_destination='dbx:upload/downloaded_audio'
readonly video_upload_destination='dbx:upload/downloaded_video'

# 必须提供 Netscape 格式的 Cookie 文件；路径从当前用户的 $HOME 开始。
# 文件不存在或不可读时直接失败。脚本不会创建 Cookie，也不会上传该文件。
# 手动导出：先在本机 Chrome 登录所需网站；chrome 可替换为 firefox。
#   mkdir -p "$HOME/.zh/etc/yt-dlp"
#   yt-dlp --cookies-from-browser chrome --cookies "$HOME/.zh/etc/yt-dlp/yt-bili.txt"
# 修改 cookies_file 后，导出命令的保存路径也要对应修改。
# 导出包含浏览器全部站点的 Cookie（应该把Youtube和Bilibili以外的Cookies记录都手动删除了），
# 请勿分享或提交到 Git。
readonly cookies_file="$HOME/.zh/etc/yt-dlp/yt-bili.txt"

# 视频高度上限（包含此高度）：支持 1080p、1440p、4k、8k 或纯像素数。
# 4k 对应 2160，8k 对应 4320；竖屏视频也按高度计算。仅影响 video 模式。
readonly video_max_resolution='1080p'

# ---- 用法 ----
# 在脚本所在目录执行，audio / video 为必填的第一个参数：
#   ./download_yt.sh audio "视频 URL"
#   ./download_yt.sh video "URL 1" "URL 2"
#   ./download_yt.sh --help
# 在其他目录可使用脚本的绝对路径；文件保存到 download_directory 指定的位置。
# URL 请加引号，避免其中的 & 等字符被 Shell 解释。
#
# 公共流程：检查参数、Cookie 文件和工具，准备下载目录、检查上传远端，然后依次处理 URL。
# 始终携带配置的 Cookie；先试读候选格式，跳过无法下载的格式。
# Cookie 需要用户自行导出、更新；携带 Cookie 不保证网站一定允许下载。
#
# 音频流程：下载最佳独立音轨 → 转为 MP3 → 写入标签 → 嵌入封面 → 按开关上传。
# - 已是 MP3 时跳过转码；其他格式由 FFmpeg 以高质量 VBR 转换，成功后删除原格式。
# - title 使用视频标题，artist 使用频道名；无频道名时用上传者名称，再缺失则留空。
# - 不自动填入专辑、日期等其他网页标签；标签和封面处理不重新编码音频。
# - 封面转为 JPG 后嵌入，完成后删除临时图片；没有封面时保留无封面的 MP3。
#
# 视频流程：下载上限内实际可下载的最高视频 → 必要时合并音视频 → 按开关上传。
# - 优先选择高度最高的格式；没有上限内的可用格式就失败，不下载超出上限的版本。
# - 分离的音视频轨由 FFmpeg 无损合并；不转码、不添加标签或封面。
# - 保留下载或合并所得的容器格式，不强制统一为 MP4。
#
# 文件与失败处理：
# - 成品命名为「原视频标题.扩展名」；文件系统不允许的字符由 yt-dlp 替换。
# - 上传使用 rclone copy，音频和视频各用自己的目标目录，本地成品始终保留。
# - 下载及后处理使用独立临时目录，完成后移入下载目录；退出时清理临时图片。
# - 失败时保留已下载的媒体文件，移入下载目录；遇到同名冲突则留在临时目录并提示位置。
# - 任一步失败都会停止后续 URL，返回非零退出码；处理失败的文件不会上传。
# - 重跑时复用同名成品；音频会更新标签和封面。不同视频同名时应配置不同下载目录。
# - yt-dlp 缺失时提示手动下载官方二进制，其余工具逐项给出 apt 安装命令。

set -euo pipefail

usage() {
    printf '用法：%s <audio|video> "视频 URL" ["视频 URL" ...]\n' "${0##*/}"
    printf '%s\n' \
        '支持平台：YouTube、Bilibili。' \
        'audio：下载音频并转为 MP3，写入标题和频道名，嵌入封面并清理临时图片。' \
        'video：下载分辨率上限内的最高可用视频，仅在需要时无损合并音视频。' \
        '必须指定 audio 或 video；文件按视频标题命名，保存到配置的下载目录。' \
        '音频和视频共用 download_directory；目录不存在时自动创建。' \
        '两种模式都强制使用脚本顶部配置的 Netscape 格式 Cookie 文件。' \
        '需要 yt-dlp、ffmpeg、ffprobe；YouTube 另需 Deno 2.3+ 或 Node.js 22+。' \
        'yt-dlp 请手动下载官方独立可执行文件，并加入 PATH。' \
        '开启上传时需要 rclone，并事先用 rclone config 配置对应远端。'
    printf '下载目录：%s\nCookie 文件：%s\n视频高度上限：%s\n' \
        "$download_directory" "$cookies_file" "$video_max_resolution"
    printf '上传开关：%s\n音频上传目录：%s\n视频上传目录：%s\n' \
        "$upload_enabled" "$audio_upload_destination" "$video_upload_destination"
}

die() {
    printf '错误：%s\n' "$*" >&2
    exit 1
}

yt_dlp_install_hint() {
    printf '%s\n' \
        '  请手动下载适合当前系统和架构的 yt-dlp 官方独立可执行文件：' \
        '  https://github.com/yt-dlp/yt-dlp/releases/latest' \
        '  Linux x86_64 可选择 yt-dlp_linux，下载后将文件命名为 yt-dlp。' \
        '  添加执行权限：chmod +x /你的安装目录/yt-dlp' \
        '  将安装目录放在 PATH 最前面：export PATH="/你的安装目录:$PATH"' \
        '  可将 export 命令加入 ~/.bashrc；用 command -v yt-dlp 和 yt-dlp --version 确认调用的是新版。' >&2
}

check_tools() {
    local tool missing=0
    for tool in "$@"; do
        if command -v "$tool" >/dev/null 2>&1; then
            continue
        fi
        printf '缺少工具：%s\n' "$tool" >&2
        case "$tool" in
            yt-dlp) yt_dlp_install_hint ;;
            ffprobe) printf '  安装命令：sudo apt install ffmpeg\n' >&2 ;;
            mkdir|mktemp|rm|mv|rmdir) printf '  安装命令：sudo apt install coreutils\n' >&2 ;;
            *) printf '  安装命令：sudo apt install %s\n' "$tool" >&2 ;;
        esac
        missing=1
    done
    (( missing == 0 )) || die '请安装缺少的工具，并确保它们位于 PATH 中，再重新运行。'
}

resolution_height() {
    local resolution=$1
    case "$resolution" in
        4[kK]) printf '2160\n' ;;
        8[kK]) printf '4320\n' ;;
        *)
            [[ "$resolution" =~ ^([1-9][0-9]{0,4})[pP]?$ ]] || \
                die 'video_max_resolution 格式无效，请使用 1080p、1440p、4k、8k 或正整数像素数。'
            printf '%s\n' "${BASH_REMATCH[1]}"
            ;;
    esac
}

check_upload_remote() {
    local destination=$1 remote_name=${1%%:*} remotes remote
    [[ "$destination" == *:* && -n "$remote_name" ]] || die '上传目录必须使用「远端名:目录」格式。'
    remotes=$(rclone listremotes) || die '无法读取 rclone 配置，请检查 rclone config。'
    while IFS= read -r remote; do
        [[ "$remote" != "$remote_name:" ]] || return 0
    done <<< "$remotes"
    die "尚未配置 $remote_name 远端，请运行 rclone config。"
}

cleanup_download_files() (
    local directory=$1 output_directory=$2 artifact destination
    # 在子 Shell 中忽略扩展名大小写，兼容 Bash 3.2，且不改变调用方的匹配设置。
    shopt -s nocasematch
    # 只清理本次创建的临时目录，不触碰下载目录里的同名图片或已有媒体。
    for artifact in "$directory"/* "$directory"/.[!.]* "$directory"/..?*; do
        [[ -f "$artifact" ]] || continue
        case "$artifact" in
            *.jpg|*.jpeg|*.png|*.webp|*.gif|*.avif|*.bmp|*.tif|*.tiff|*.ico)
                rm -f -- "$artifact" || printf '无法清理临时图片：%s\n' "$artifact" >&2
                ;;
            *)
                destination="$output_directory/${artifact##*/}"
                [[ ! -e "$destination" && ! -L "$destination" ]] || continue
                mv -n -- "$artifact" "$destination" || printf '无法移入下载目录，未完成文件保留在：%s\n' "$artifact" >&2
                ;;
        esac
    done
    if ! rmdir -- "$directory" 2>/dev/null; then
        printf '临时目录仍有未清理的文件，请检查：%s\n' "$directory" >&2
    fi
)

# 两条流程只声明各自的处理参数；$@ 接收公共参数和 URL。
download_audio() {
    yt-dlp \
        --format bestaudio \
        --extract-audio --audio-format mp3 --audio-quality 0 --no-keep-video \
        --embed-metadata --no-embed-chapters --no-embed-info-json \
        --parse-metadata ':(?P<meta_>)' \
        --parse-metadata 'title:(?s)(?P<meta_title>.*)' \
        --parse-metadata '%(channel,uploader|)s:(?s)(?P<meta_artist>.*)' \
        --convert-thumbnails jpg --embed-thumbnail \
        "$@"
}

download_video() {
    local max_height=$1
    shift
    # 两个分支都限制高度：优先取最佳视频和音轨，或使用已包含音频的视频。
    # 强制按高度排序，避免站点自定义的质量优先级覆盖分辨率要求。
    yt-dlp \
        --format "bv*[height<=$max_height]+ba/b[height<=$max_height]" \
        --format-sort-force --format-sort height \
        "$@"
}

main() {
    if (( $# == 1 )) && [[ "$1" == '-h' || "$1" == '--help' ]]; then
        usage
        return 0
    fi
    if (( $# < 2 )); then
        usage >&2
        printf '错误：必须指定 audio 或 video，并提供至少一个视频 URL。\n' >&2
        return 2
    fi
    local mode=$1
    shift
    case "$mode" in
        audio|video) ;;
        *) printf '错误：第一个参数必须是 audio 或 video。\n' >&2; return 2 ;;
    esac

    # Cookie 检查在任何下载、工具探测或远端访问之前完成。
    [[ -f "$cookies_file" ]] || die "Cookie 文件不存在：$cookies_file；请先导出 Cookie 或修改脚本顶部路径。"
    [[ -r "$cookies_file" && -s "$cookies_file" ]] || die "Cookie 文件不可读或为空：$cookies_file"
    [[ -n "$download_directory" ]] || die 'download_directory 不能为空。'
    case "$upload_enabled" in
        true|false) ;;
        *) die '请将 upload_enabled 设置为 true 或 false。' ;;
    esac

    local upload_destination mode_label max_height=''
    local -a download_command
    if [[ "$mode" == audio ]]; then
        mode_label='音频（MP3，含标题、频道名和可用封面）'
        upload_destination=$audio_upload_destination
        download_command=(download_audio)
    else
        max_height=$(resolution_height "$video_max_resolution") || return 1
        mode_label="视频（高度不超过 ${max_height} 像素）"
        upload_destination=$video_upload_destination
        download_command=(download_video "$max_height")
    fi

    local -a required_tools=(yt-dlp ffmpeg ffprobe mkdir mktemp rm mv rmdir)
    [[ "$upload_enabled" != true ]] || required_tools+=(rclone)
    check_tools "${required_tools[@]}"
    mkdir -p -- "$download_directory" || die "无法创建下载目录：$download_directory"
    [[ -w "$download_directory" && -x "$download_directory" ]] || die "下载目录不可写或无法进入：$download_directory"

    local yt_dlp_version yt_dlp_help url js_runtime=''
    yt_dlp_version=$(yt-dlp --ignore-config --version) || die '无法运行 yt-dlp，请检查安装。'
    yt_dlp_help=$(yt-dlp --ignore-config --help) || die '无法运行 yt-dlp，请检查安装。'
    if [[ "$yt_dlp_help" == *'--js-runtimes'* ]]; then
        if command -v deno >/dev/null 2>&1; then
            js_runtime=deno
        elif command -v node >/dev/null 2>&1; then
            js_runtime=node
        fi
    else
        printf '提示：当前 yt-dlp 版本较旧，建议手动下载新版二进制文件。\n' >&2
        yt_dlp_install_hint
    fi
    for url in "$@"; do
        case "$url" in
            http://*|https://*) ;;
            *) die "请提供完整的 http:// 或 https:// 视频网址：$url" ;;
        esac
        case "$url" in
            *://youtube.com/*|*://*.youtube.com/*|*://youtu.be/*|*://*.youtu.be/*)
                if [[ "$yt_dlp_help" == *'--js-runtimes'* && -z "$js_runtime" ]]; then
                    printf '%s\n' \
                        '缺少 YouTube 所需的 JavaScript 运行时（Deno 2.3+ 或 Node.js 22+）。' \
                        '  安装命令：sudo apt install nodejs' \
                        '  若软件源版本低于 22，请使用提供新版 Node.js 的软件源：https://nodejs.org/' >&2
                    return 1
                fi
                ;;
        esac
    done

    if [[ "$upload_enabled" == true ]]; then
        check_upload_remote "$upload_destination"
        export DOWNLOAD_MEDIA_UPLOAD_DESTINATION="$upload_destination"
    fi

    # EXIT trap 在 main 返回后仍需访问此路径，因此它是唯一的全局运行时状态。
    download_tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/download-media-work.XXXXXXXX") || die '无法创建下载临时目录。'
    trap 'cleanup_download_files "$download_tmp_dir" "$download_directory"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM

    local -a common_args=(
        --ignore-config --no-playlist --abort-on-error
        --no-overwrites --no-windows-filenames --no-simulate --check-formats
        --cookies "$cookies_file"
        --paths "home:$download_directory"
        --paths "temp:$download_tmp_dir"
        --paths "thumbnail:$download_tmp_dir"
        --output '%(title)s.%(ext)s'
    )
    # 可选参数按需追加，避免 Bash 3.2 在 set -u 下展开空数组时报错。
    [[ -z "$js_runtime" ]] || common_args+=(--js-runtimes "$js_runtime")
    if [[ "$upload_enabled" == true ]]; then
        # 成品移到下载目录后才上传；路径由 yt-dlp 引用，目标通过环境变量传递。
        common_args+=(--exec 'after_move:rclone copy --progress -- %(filepath)q "$DOWNLOAD_MEDIA_UPLOAD_DESTINATION"')
    fi
    printf 'yt-dlp：%s（%s）\n下载类型：%s\n下载目录：%s\n' \
        "$yt_dlp_version" "$(command -v yt-dlp)" "$mode_label" "$download_directory"
    if [[ "$upload_enabled" == true ]]; then
        printf '上传目录：%s\n' "$upload_destination"
    else
        printf '上传已关闭，只保存到本地。\n'
    fi

    local status
    if "${download_command[@]}" "${common_args[@]}" -- "$@"; then
        printf '处理完成，本地成品保留在：%s\n' "$download_directory"
    else
        status=$?
        printf '下载、处理或上传失败，请查看上方错误；已下载的媒体文件会保留。\n' >&2
        if [[ "$upload_enabled" == true ]]; then
            printf '若仅上传失败，可重跑相同命令，或运行：rclone copy --progress -- "本地文件路径" %q\n' "$upload_destination" >&2
        fi
        return "$status"
    fi
}

main "$@"
