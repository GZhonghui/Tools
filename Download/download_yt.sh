#!/usr/bin/env bash
# 支持 YouTube 和 Bilibili：从视频 URL 下载音频或视频，
# 保存到配置的下载目录，并按开关上传到 rclone。
# 兼容 Bash 3.2 及以上版本（包括 macOS 自带的 Bash）。

readonly script_version='0.2.0'

# ---- 配置：运行前按需修改 ----
# 仅 video 模式下载字幕；全部嵌入为可开关、可切换的软字幕，不烧录到画面。
# 关闭下载后，源视频已经包含的字幕仍会保留。
readonly subtitles_enabled=true
# 用空格分隔语言代码，列表中所有可用语言都会下载，不按顺序择一。
# zh 包含简体、繁体等全部变体；en 也包含地区变体。日语使用标准语言代码 ja。
# 同一语言代码优先人工字幕，没有时使用网站提供的自动字幕（可能包含自动翻译）。
readonly subtitle_languages='en zh ja'

# 音频和视频共用下载目录；不存在时自动创建。相对路径以脚本执行位置为基准。
readonly download_directory="$HOME/Downloads"

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
# - 脚本直接调用 FFmpeg 并显示进度；已有 MP3 复制音轨，其他格式以最高质量 VBR 转换。
# - title 使用视频标题，artist 使用频道名；无频道名时用上传者名称，再缺失则留空。
# - 不自动填入专辑、日期等其他网页标签；标签和封面处理不重新编码音频。
# - 封面转为 JPG 后嵌入，完成后删除临时图片；没有封面时保留无封面的 MP3。
#
# 视频流程：下载可用字幕及上限内最高视频 → 必要时合并音视频 → 转为含软字幕的 MP4 → 按开关上传。
# - 优先选择高度最高的格式；没有上限内的可用格式就失败，不下载超出上限的版本。
# - 分离的音视频轨先由 FFmpeg 无损合并；不添加标签或封面。
# - 已是 MP4 时复制音视频轨嵌入新字幕；没有新字幕则直接复用。其他格式转为 H.264/AAC MP4。
# - 转码时直接保留源视频的文字字幕轨，并把下载的字幕一并转为 MP4 的 mov_text 软字幕。
# - 下载字幕临时保存为 SRT，处理结束清理；不存在或下载失败的字幕跳过，仍正常保存视频。
# - 复用成品时更新本脚本嵌入的同语言字幕，避免重复添加；保留其他已有字幕轨。
# - 音频和视频均在处理成功、保存成品后删除源文件；转码失败时保留源文件。
#
# 文件与失败处理：
# - 成品命名为「原视频标题.扩展名」；文件系统不允许的字符由 yt-dlp 替换。
# - 上传使用 rclone copy，音频和视频各用自己的目标目录，本地成品始终保留。
# - 先提取信息，再复用同名成品或按提取结果下载，避免重复解析网页。
# - 下载及后处理使用独立临时目录，完成后移入下载目录；退出时清理临时图片、字幕和元数据。
# - 失败时保留已下载的媒体文件，移入下载目录；遇到同名冲突则留在临时目录并提示位置。
# - 除跳过不可下载的字幕外，任一步失败都会停止后续 URL，返回非零退出码；处理失败的文件不会上传。
# - 重跑时复用同名成品；音频会更新标签和封面。不同视频同名时应配置不同下载目录。
# - yt-dlp 缺失时提示手动下载官方二进制，其余工具逐项给出 apt 安装命令。

set -euo pipefail

usage() {
    printf '用法：%s <audio|video> "视频 URL" ["视频 URL" ...]\n' "${0##*/}"
    printf '%s\n' \
        '支持平台：YouTube、Bilibili。' \
        'audio：下载音频并转为 MP3，写入标题和频道名，嵌入封面并清理临时图片。' \
        'video：下载分辨率上限内的最高可用视频，必要时合并；非 MP4 由 FFmpeg 转码为 MP4，成功后删除原格式。' \
        'video 按顶部开关下载语言列表中所有可用字幕；zh 包含简繁体，日语代码为 ja。' \
        '不存在或下载失败的字幕会跳过，不影响其他字幕和视频下载。' \
        '下载字幕和源视频的文字字幕均嵌入为软字幕；已是 MP4 时复制音视频轨，临时字幕在结束后清理。' \
        '音频和视频均由脚本直接调用 FFmpeg 转码，并实时显示处理进度。' \
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
    printf '字幕下载开关：%s\n字幕语言：%s\n' "$subtitles_enabled" "$subtitle_languages"
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

# yt-dlp 使用完整匹配的正则表达式；同时兼容 Bilibili 的 ai-zh 等字幕代码。
subtitle_language_pattern() {
    local language pattern=''
    local -a languages
    IFS=$' \t' read -r -a languages <<< "$subtitle_languages"
    [[ -n "${languages[0]:-}" ]] || { printf '错误：subtitle_languages 不能为空。\n' >&2; return 1; }
    for language in "${languages[@]}"; do
        [[ "$language" =~ ^[a-z]{2,3}([-_][A-Za-z0-9]+)*$ ]] || \
            { printf '错误：字幕语言代码无效：%s；请用空格分隔，如 en zh ja。\n' "$language" >&2; return 1; }
        pattern+="${pattern:+,}(ai-)?${language}([-_].*)?"
    done
    printf '%s\n' "$pattern"
}

# MP4 的 language 标签使用三字母代码；完整语言变体另存入字幕轨名称。
subtitle_mp4_language() {
    local language=${1#ai-}
    language=${language%%[-_]*}
    case "$language" in
        en) printf 'eng' ;; zh) printf 'zho' ;; ja) printf 'jpn' ;;
        ko) printf 'kor' ;; fr) printf 'fra' ;; de) printf 'deu' ;;
        es) printf 'spa' ;; pt) printf 'por' ;; it) printf 'ita' ;;
        ru) printf 'rus' ;; ar) printf 'ara' ;; hi) printf 'hin' ;;
        id) printf 'ind' ;; vi) printf 'vie' ;; th) printf 'tha' ;;
        *)
            if [[ "$language" =~ ^[a-z]{3}$ ]]; then
                printf '%s' "$language"
            else
                printf 'und'
            fi
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
    # .metadata 只存放本次的信息、封面、临时字幕和未完成的转码文件，不存放源媒体。
    if [[ -d "$directory/.metadata" ]]; then
        rm -f -- "$directory/.metadata/"* "$directory/.metadata/".[!.]* "$directory/.metadata/"..?* || \
            printf '无法清理临时元数据：%s\n' "$directory/.metadata" >&2
        rmdir -- "$directory/.metadata" 2>/dev/null || :
    fi
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

# FFmpeg 直接继承终端输出，-stats 显示 time、speed 等实时进度。
# 转码写入独立临时文件，只有完整成功后才替换成品并删除源文件。
process_audio() {
    local source=$1 destination=$2 title=$3 artist=$4 cover=$5 work_directory=$6
    local codec temporary
    codec=$(ffprobe -v error -select_streams a:0 -show_entries stream=codec_name \
        -of default=noprint_wrappers=1:nokey=1 "$source") || return 1
    [[ -n "$codec" ]] || { printf '错误：文件中没有可用音轨：%s\n' "$source" >&2; return 1; }
    temporary=$(mktemp "$work_directory/transcode.XXXXXXXX") || return 1
    local -a command=(ffmpeg -hide_banner -nostdin -stats -y -i "$source")
    if [[ -f "$cover" ]]; then
        command+=(-i "$cover" -map 0:a:0 -map 1:v:0 -c:v mjpeg -disposition:v:0 attached_pic
            -metadata:s:v:0 'title=Album cover' -metadata:s:v:0 'comment=Cover (front)')
    else
        # 没有新封面时，保留已有 MP3 中的封面。
        command+=(-map 0:a:0 -map '0:v?' -c:v copy)
    fi
    if [[ "$codec" == mp3 ]]; then
        printf '音轨已是 MP3，复制音轨并更新标签和封面：%s\n' "$source"
        command+=(-c:a copy)
    else
        printf '正在转码为 MP3：%s\n' "$source"
        command+=(-c:a libmp3lame -q:a 0)
    fi
    command+=(-map_metadata -1 -map_metadata:s -1 -map_chapters -1
        -metadata "title=$title" -metadata "artist=$artist" -id3v2_version 3 -f mp3 "$temporary")
    "${command[@]}" || return 1
    mv -f -- "$temporary" "$destination" || return 1
    [[ "$source" == "$destination" ]] || rm -f -- "$source" || return 1
}

process_video() {
    local source=$1 destination=$2 work_directory=$3 temporary
    local subtitle language input_index=0 subtitle_count=0 subtitle_indices index handler
    local downloaded_languages='|' is_mp4=false
    local -a command=(ffmpeg -hide_banner -nostdin -stats -y -i "$source")
    local -a maps=(-map 0:v:0 -map '0:a?')
    local -a subtitle_args=(-c:s mov_text)

    for subtitle in "$work_directory"/subtitle.*; do
        [[ -f "$subtitle" ]] || continue
        [[ "$subtitle" == *.srt ]] || \
            { printf '错误：字幕未能转为 SRT：%s\n' "$subtitle" >&2; return 1; }
        language=${subtitle##*/}
        language=${language#subtitle.}
        language=${language%.srt}
        command+=(-i "$subtitle")
        downloaded_languages+="$language|"
    done

    case "$source" in
        *.[mM][pP]4) is_mp4=true ;;
    esac
    if [[ "$is_mp4" == true ]]; then
        if [[ "$downloaded_languages" == '|' ]]; then
            printf '视频已是 MP4，跳过转码：%s\n' "$source"
            [[ "$source" != "$destination" ]] || return 0
            mv -n -- "$source" "$destination" || return 1
            [[ ! -e "$source" ]] || { printf '错误：成品已存在，未覆盖：%s\n' "$destination" >&2; return 1; }
            return 0
        fi
        printf '视频已是 MP4，复制音视频轨并嵌入软字幕：%s\n' "$source"
        command+=(-c:v copy -c:a copy)
    else
        printf '正在转码为 MP4，并保留软字幕：%s\n' "$source"
        command+=(-c:v libx264 -preset medium -crf 23 -c:a aac)
    fi

    # 直接映射源字幕，无须先提取；显式保留字幕流标签，避免被全局元数据清理影响。
    # 用 handler_name 标记下载字幕，仅在本次取得同语言新字幕时替换，避免重跑时重复添加。
    subtitle_indices=$(ffprobe -v error -select_streams s -show_entries stream=index \
        -of csv=p=0 "$source") || return 1
    while IFS= read -r index; do
        [[ -n "$index" ]] || continue
        handler=$(ffprobe -v error -select_streams "$index" -show_entries stream_tags=handler_name \
            -of default=noprint_wrappers=1:nokey=1 "$source") || return 1
        if [[ "$handler" == yt-dlp:* && "$downloaded_languages" == *"|${handler#yt-dlp:}|"* ]]; then
            continue
        fi
        maps+=(-map "0:$index" -map_metadata:s:s:"$subtitle_count" "0:s:$index")
        subtitle_count=$((subtitle_count + 1))
    done <<< "$subtitle_indices"
    for subtitle in "$work_directory"/subtitle.*.srt; do
        [[ -f "$subtitle" ]] || continue
        language=${subtitle##*/}
        language=${language#subtitle.}
        language=${language%.srt}
        input_index=$((input_index + 1))
        maps+=(-map "$input_index:s:0")
        subtitle_args+=(-metadata:s:s:"$subtitle_count" "language=$(subtitle_mp4_language "$language")"
            -metadata:s:s:"$subtitle_count" "title=$language"
            -metadata:s:s:"$subtitle_count" "handler_name=yt-dlp:$language"
            -disposition:s:"$subtitle_count" 0)
        subtitle_count=$((subtitle_count + 1))
    done

    temporary=$(mktemp "$work_directory/transcode.XXXXXXXX") || return 1
    command+=("${maps[@]}" "${subtitle_args[@]}"
        -map_metadata -1 -map_chapters -1 -movflags +faststart -f mp4 "$temporary")
    "${command[@]}" || return 1
    if [[ "$source" == "$destination" ]]; then
        mv -f -- "$temporary" "$destination" || return 1
    else
        mv -n -- "$temporary" "$destination" || return 1
        [[ ! -e "$temporary" ]] || { printf '错误：成品已存在，未覆盖：%s\n' "$destination" >&2; return 1; }
        rm -f -- "$source" || return 1
    fi
}

process_url() {
    local mode=$1 max_height=$2 url=$3 js_runtime=$4 upload_destination=$5
    local metadata_directory="$download_tmp_dir/.metadata"
    mkdir -p -- "$metadata_directory" || return 1
    local -a download_command common_args metadata_args
    if [[ "$mode" == audio ]]; then
        download_command=(download_audio)
    else
        download_command=(download_video "$max_height")
    fi
    common_args=(
        --ignore-config --no-playlist --abort-on-error
        --no-overwrites --no-windows-filenames --no-simulate --check-formats
        --cookies "$cookies_file"
        --paths "home:$download_tmp_dir" --paths "temp:$download_tmp_dir"
        --paths "thumbnail:$metadata_directory" --output '%(title)s.%(ext)s'
    )
    [[ -z "$js_runtime" ]] || common_args+=(--js-runtimes "$js_runtime")
    metadata_args=(--skip-download --write-info-json --output "infojson:$metadata_directory/media.%(ext)s"
        --print-to-file 'video:%(filename)s' "$metadata_directory/filename"
        --print-to-file 'video:%(title)s' "$metadata_directory/title"
        --print-to-file 'video:%(channel,uploader|)s' "$metadata_directory/artist")
    if [[ "$mode" == audio ]]; then
        metadata_args+=(--write-thumbnail --output 'thumbnail:cover.%(ext)s')
    elif [[ "$subtitles_enabled" == true ]]; then
        local subtitle_pattern
        subtitle_pattern=$(subtitle_language_pattern) || return 1
        # 字幕只在信息提取阶段下载一次，复用已有 MP4 时也可补字幕。
        # 连下载中的字幕都放入 .metadata，失败或中断后也能统一清理。
        # 仅此阶段允许跳过下载失败的字幕；下方仍检查必要信息，媒体下载仍使用 --abort-on-error。
        metadata_args+=(--ignore-errors --write-subs --write-auto-subs --sub-langs "$subtitle_pattern"
            --sub-format 'srt/vtt/ass/ssa/ttml/best' --convert-subs srt
            --paths "temp:$metadata_directory" --paths "subtitle:$metadata_directory"
            --output 'subtitle:subtitle.%(ext)s')
    fi
    local metadata_status=0
    "${download_command[@]}" "${common_args[@]}" "${metadata_args[@]}" -- "$url" || metadata_status=$?
    if (( metadata_status != 0 )) && [[ "$mode" != video || "$subtitles_enabled" != true ]]; then
        return "$metadata_status"
    fi

    local filename title artist source destination extension cover=''
    # 每个字段单独读取至 EOF，只去掉 yt-dlp 追加的一个换行；保留标题内的换行和引号。
    [[ -s "$metadata_directory/filename" && -s "$metadata_directory/media.info.json" && \
        -f "$metadata_directory/title" && -f "$metadata_directory/artist" ]] || \
        { printf '错误：未取得可处理的视频信息：%s\n' "$url" >&2; return 1; }
    if (( metadata_status != 0 )); then
        # 部分版本即使启用 --ignore-errors，字幕下载失败后仍返回非零，但已保存有效视频信息。
        printf '提示：字幕准备阶段报告错误；视频信息已取得，将继续处理可用字幕和视频。\n' >&2
    fi
    IFS= read -r -d '' filename < "$metadata_directory/filename" || :
    IFS= read -r -d '' title < "$metadata_directory/title" || :
    IFS= read -r -d '' artist < "$metadata_directory/artist" || :
    filename=${filename%$'\n'}
    title=${title%$'\n'}
    artist=${artist%$'\n'}
    extension=mp4
    [[ "$mode" != audio ]] || extension=mp3
    filename=${filename##*/}
    destination="$download_directory/${filename%.*}.$extension"
    if [[ -f "$destination" ]]; then
        printf '复用已有成品：%s\n' "$destination"
        source=$destination
    elif [[ -f "$download_directory/$filename" ]]; then
        # 上次处理失败时保留的源文件可直接重试，成功后同样删除。
        source="$download_directory/$filename"
        printf '复用已下载的源文件：%s\n' "$source"
    else
        # 从刚提取的信息下载，不再请求网页解析；实际路径在合并完成后记录。
        "${download_command[@]}" "${common_args[@]}" --no-write-thumbnail \
            --load-info-json "$metadata_directory/media.info.json" \
            --print-to-file 'after_move:%(filepath)s' "$metadata_directory/downloaded" || return 1
        [[ -s "$metadata_directory/downloaded" ]] || return 1
        IFS= read -r -d '' source < "$metadata_directory/downloaded" || :
        source=${source%$'\n'}
    fi
    [[ -f "$source" ]] || { printf '错误：找不到已下载的媒体：%s\n' "$source" >&2; return 1; }
    if [[ "$mode" == audio ]]; then
        local thumbnail
        for thumbnail in "$metadata_directory"/cover.*; do
            [[ -f "$thumbnail" ]] || continue
            cover=$thumbnail
            break
        done
        # FFmpeg 在嵌入时把封面编码为 JPEG，不重新编码已有 MP3 音轨。
        process_audio "$source" "$destination" "$title" "$artist" "$cover" "$metadata_directory" || return 1
    else
        process_video "$source" "$destination" "$metadata_directory" || return 1
    fi
    if [[ "$upload_enabled" == true ]]; then
        rclone copy --progress -- "$destination" "$upload_destination" || return 1
    fi
}

main() {
    printf 'download_yt.sh 版本：%s\n' "$script_version"
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
    if [[ "$mode" == audio ]]; then
        mode_label='音频（MP3，含标题、频道名和可用封面）'
        upload_destination=$audio_upload_destination
    else
        case "$subtitles_enabled" in
            true) subtitle_language_pattern >/dev/null || return 1 ;;
            false) ;;
            *) die '请将 subtitles_enabled 设置为 true 或 false。' ;;
        esac
        max_height=$(resolution_height "$video_max_resolution") || return 1
        mode_label="视频（MP4，高度不超过 ${max_height} 像素）"
        upload_destination=$video_upload_destination
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
    fi

    # EXIT trap 在 main 返回后仍需访问此路径，因此它是唯一的全局运行时状态。
    download_tmp_dir=''
    trap '[[ -z "$download_tmp_dir" ]] || cleanup_download_files "$download_tmp_dir" "$download_directory"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM

    printf 'yt-dlp：%s（%s）\n' "$yt_dlp_version" "$(command -v yt-dlp)"
    printf '%s\n' \
        '提醒：请及时将 yt-dlp 升级到最新版本，旧版本可能因网站变更而无法下载。' \
        '新版下载：https://github.com/yt-dlp/yt-dlp/releases/latest'
    printf '下载类型：%s\n下载目录：%s\n' "$mode_label" "$download_directory"
    if [[ "$mode" == video ]]; then
        printf '字幕下载开关：%s\n字幕语言：%s\n' "$subtitles_enabled" "$subtitle_languages"
    fi
    if [[ "$upload_enabled" == true ]]; then
        printf '上传目录：%s\n' "$upload_destination"
    else
        printf '上传已关闭，只保存到本地。\n'
    fi

    local status
    for url in "$@"; do
        download_tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/download-media-work.XXXXXXXX") || die '无法创建下载临时目录。'
        if process_url "$mode" "$max_height" "$url" "$js_runtime" "$upload_destination"; then
            cleanup_download_files "$download_tmp_dir" "$download_directory"
            download_tmp_dir=''
        else
            status=$?
            printf '下载、处理或上传失败，请查看上方错误；已下载的媒体文件会保留。\n' >&2
            if [[ "$upload_enabled" == true ]]; then
                printf '若仅上传失败，可重跑相同命令，或运行：rclone copy --progress -- "本地文件路径" %q\n' "$upload_destination" >&2
            fi
            return "$status"
        fi
    done
    printf '处理完成，本地成品保留在：%s\n' "$download_directory"
}

main "$@"
