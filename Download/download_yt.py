#!/usr/bin/env python3
"""下载 YouTube / Bilibili 音频或视频，功能对齐 download_yt.sh。

Python 3.9+，仅使用标准库；下载、转码和上传仍调用独立的命令行工具。
用法：python3 Download/download_yt.py <audio|video> "URL" ["URL" ...]
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

SCRIPT_VERSION = "0.3.1"


# ---- 配置：运行前按需修改 ----
@dataclass(frozen=True)
class Config:
    # 音频、视频共用；两者都开启时为 timestamp_id_title.扩展名。
    # timestamp 为发布时间的 Unix 秒数，网站未提供时该段留空。
    filename_timestamp_enabled: bool = False
    filename_id_enabled: bool = True

    # 仅 video 下载字幕，全部嵌入为可开关、可切换的软字幕。
    # 关闭后仍保留源视频已有字幕。各语言优先人工字幕，其次使用网站的自动字幕。
    # 用空格分隔；zh / en 包括地区变体，日语使用 ja。
    subtitles_enabled: bool = True
    subtitle_languages: str = "en zh ja"

    # 两种模式共用目录，不存在时创建；相对路径以执行位置为基准。
    download_directory: Path = Path.home() / "Downloads"

    # 关闭上传后不检查或调用 rclone；copy 上传后始终保留本地成品。
    upload_enabled: bool = True
    audio_upload_destination: str = "dbx:upload/downloaded_audio"
    video_upload_destination: str = "dbx:upload/downloaded_video"

    # 必须提供可读、非空的 Netscape 格式 Cookie；源文件只读，临时副本在结束后清理。
    # 手动导出（需先登录网站，chrome 可替换为 firefox）：
    # mkdir -p "$HOME/.zh/etc/yt-dlp"
    # yt-dlp --cookies-from-browser chrome --cookies "$HOME/.zh/etc/yt-dlp/yt-bili.txt"
    # 修改路径时同步修改导出命令；导出的文件可能包含其他网站的 Cookie，请勿分享或提交。
    cookies_file: Path = Path.home() / ".zh/etc/yt-dlp/yt-bili.txt"

    # 仅 video：最高高度，支持 1080p、1440p、4k、8k 或纯像素数，竖屏也按高度。
    video_max_resolution: str = "1080p"


CONFIG = Config()

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp",
    ".tif", ".tiff", ".ico",
}
MP4_LANGUAGES = {
    "en": "eng", "zh": "zho", "ja": "jpn", "ko": "kor", "fr": "fra",
    "de": "deu", "es": "spa", "pt": "por", "it": "ita", "ru": "rus",
    "ar": "ara", "hi": "hin", "id": "ind", "vi": "vie", "th": "tha",
}


class DownloadError(Exception):
    def __init__(self, message: str, status: int = 1):
        super().__init__(message)
        self.status = status if status > 0 else 128 - status


def say(message: str, *, error: bool = False) -> None:
    print(message, file=sys.stderr if error else sys.stdout, flush=True)


def run(command, *, capture: bool = False, check: bool = True):
    """使用参数列表调用工具；默认继承终端，保留下载和 FFmpeg 的实时进度。"""
    arguments = [str(part) for part in command]
    process = subprocess.Popen(
        arguments,
        stdout=subprocess.PIPE if capture else None,
        text=True, encoding="utf-8", start_new_session=(os.name == "posix"),
    )
    try:
        stdout, _ = process.communicate()
    except BaseException:
        # yt-dlp 也可能启动 FFmpeg；取消时一并停止，之后再清理或移动源媒体。
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        finally:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()
    result = subprocess.CompletedProcess(arguments, process.returncode, stdout=stdout)
    if check and result.returncode:
        raise DownloadError(
            f"{command[0]} 执行失败（退出码 {result.returncode}）。", result.returncode,
        )
    return result


def resolution_height(resolution: str) -> int:
    if resolution.lower() in ("4k", "8k"):
        return {"4k": 2160, "8k": 4320}[resolution.lower()]
    if not re.fullmatch(r"[1-9][0-9]{0,4}[pP]?", resolution):
        raise DownloadError(
            "video_max_resolution 格式无效，请使用 1080p、1440p、4k、8k 或正整数像素数。"
        )
    return int(resolution.rstrip("pP"))


def subtitle_language_pattern(languages: str) -> str:
    codes = languages.split()
    if not codes:
        raise DownloadError("subtitle_languages 不能为空。")
    for code in codes:
        if not re.fullmatch(r"[a-z]{2,3}([-_][A-Za-z0-9]+)*", code):
            raise DownloadError(f"字幕语言代码无效：{code}；请用空格分隔，如 en zh ja。")
    return ",".join(f"(ai-)?{code}([-_].*)?" for code in codes)


def subtitle_mp4_language(language: str) -> str:
    code = re.split(r"[-_]", language.removeprefix("ai-"), maxsplit=1)[0]
    return MP4_LANGUAGES.get(code, code if re.fullmatch(r"[a-z]{3}", code) else "und")


def filename_template(config: Config) -> str:
    fields = []
    if config.filename_timestamp_enabled:
        fields.append("%(timestamp|)s")
    if config.filename_id_enabled:
        fields.append("%(id)s")
    return "_".join(fields + ["%(title)s"]) + ".%(ext)s"


def yt_dlp_install_hint() -> None:
    say(
        "  请手动下载适合当前系统和架构的 yt-dlp 官方独立可执行文件：\n"
        "  https://github.com/yt-dlp/yt-dlp/releases/latest\n"
        "  Linux x86_64 可选择 yt-dlp_linux，下载后将文件命名为 yt-dlp。\n"
        "  添加执行权限：chmod +x /你的安装目录/yt-dlp\n"
        '  将安装目录放在 PATH 最前面：export PATH="/你的安装目录:$PATH"\n'
        "  可将 export 命令加入 ~/.bashrc；用 command -v yt-dlp 和 yt-dlp --version 确认新版。",
        error=True,
    )


def check_tools(upload_enabled: bool) -> None:
    missing = [
        tool for tool in ("yt-dlp", "ffmpeg", "ffprobe") + (("rclone",) if upload_enabled else ())
        if shutil.which(tool) is None
    ]
    for tool in missing:
        say(f"缺少工具：{tool}", error=True)
        if tool == "yt-dlp":
            yt_dlp_install_hint()
        else:
            package = "ffmpeg" if tool == "ffprobe" else tool
            say(f"  安装命令：sudo apt install {package}", error=True)
    if missing:
        raise DownloadError("请安装缺少的工具，并确保它们位于 PATH 中，再重新运行。")


def validate_config(config: Config, mode: str) -> Config:
    # 在任何工具探测、下载或远端访问前检查 Cookie。
    cookies = Path(config.cookies_file).expanduser()
    if not cookies.is_file():
        raise DownloadError(f"Cookie 文件不存在：{cookies}；请先导出 Cookie 或修改顶部路径。")
    if not os.access(cookies, os.R_OK) or cookies.stat().st_size == 0:
        raise DownloadError(f"Cookie 文件不可读或为空：{cookies}")
    if not str(config.download_directory):
        raise DownloadError("download_directory 不能为空。")
    switches = ["filename_timestamp_enabled", "filename_id_enabled", "upload_enabled"]
    if mode == "video":
        switches.append("subtitles_enabled")
    for name in switches:
        if type(getattr(config, name)) is not bool:
            raise DownloadError(f"请将 {name} 设置为 True 或 False。")
    if mode == "video":
        resolution_height(config.video_max_resolution)
        if config.subtitles_enabled:
            subtitle_language_pattern(config.subtitle_languages)
    return replace(
        config, cookies_file=cookies,
        download_directory=Path(config.download_directory).expanduser(),
    )


def check_upload_remote(destination: str) -> None:
    remote, separator, _ = destination.partition(":")
    if not separator or not remote:
        raise DownloadError("上传目录必须使用「远端名:目录」格式。")
    remotes = run(["rclone", "listremotes"], capture=True).stdout.splitlines()
    if f"{remote}:" not in remotes:
        raise DownloadError(f"尚未配置 {remote} 远端，请运行 rclone config。")


def move_file(source: Path, destination: Path, *, overwrite: bool = False) -> None:
    """同盘直接提交；跨盘复制成功后才删除源文件，默认拒绝覆盖同名文件。"""
    try:
        if overwrite:
            os.replace(source, destination)
        else:
            os.link(source, destination)
            source.unlink()
        return
    except FileExistsError as error:
        raise DownloadError(f"成品已存在，未覆盖：{destination}") from error
    except OSError as error:
        if error.errno not in (errno.EXDEV, errno.EPERM, errno.ENOTSUP):
            raise

    if overwrite:
        # 跨盘更新已有成品时先复制到目标目录，再原子替换，避免损坏已有成品。
        descriptor, name = tempfile.mkstemp(prefix=".download-media-", dir=destination.parent)
        os.close(descriptor)
        staged = Path(name)
        try:
            shutil.copy2(source, staged)
            os.replace(staged, destination)
        finally:
            staged.unlink(missing_ok=True)
    else:
        try:
            target = destination.open("xb")
        except FileExistsError as error:
            raise DownloadError(f"成品已存在，未覆盖：{destination}") from error
        try:
            with target, source.open("rb") as original:
                shutil.copyfileobj(original, target)
            shutil.copystat(source, destination)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
    source.unlink()


def cleanup_download_files(directory: Path, output_directory: Path) -> None:
    # 不自动删除整个工作目录：失败的源媒体需要保留，同名冲突时留在原处并提示。
    metadata = directory / ".metadata"
    try:
        if metadata.exists():
            shutil.rmtree(metadata)
    except OSError as error:
        say(f"无法清理临时元数据：{metadata}：{error}", error=True)
    try:
        artifacts = sorted(directory.iterdir())
    except FileNotFoundError:
        return
    except OSError as error:
        say(f"无法读取临时目录，请检查：{directory}：{error}", error=True)
        return
    for artifact in artifacts:
        if not artifact.is_file():
            continue
        try:
            if artifact.suffix.lower() in IMAGE_EXTENSIONS:
                artifact.unlink()
            else:
                move_file(artifact, output_directory / artifact.name)
        except (OSError, DownloadError) as error:
            say(f"未完成文件保留在：{artifact}：{error}", error=True)
    try:
        directory.rmdir()
    except OSError:
        say(f"临时目录仍有未清理的文件，请检查：{directory}", error=True)


def temporary_media(work_directory: Path) -> Path:
    descriptor, name = tempfile.mkstemp(prefix="transcode.", dir=work_directory)
    os.close(descriptor)
    return Path(name)


def probe_streams(source: Path, selection: str, entries: str) -> list[dict]:
    result = run([
        "ffprobe", "-v", "error", "-select_streams", selection,
        "-show_entries", entries, "-of", "json", source,
    ], capture=True)
    return json.loads(result.stdout).get("streams", [])


def process_audio(
    source: Path, destination: Path, title: str, artist: str,
    cover: Optional[Path], work_directory: Path,
) -> None:
    streams = probe_streams(source, "a:0", "stream=codec_name")
    if not streams or not streams[0].get("codec_name"):
        raise DownloadError(f"文件中没有可用音轨：{source}")
    temporary = temporary_media(work_directory)
    command = ["ffmpeg", "-hide_banner", "-nostdin", "-stats", "-y", "-i", source]
    if cover is not None:
        command += [
            "-i", cover, "-map", "0:a:0", "-map", "1:v:0", "-c:v", "mjpeg",
            "-disposition:v:0", "attached_pic", "-metadata:s:v:0", "title=Album cover",
            "-metadata:s:v:0", "comment=Cover (front)",
        ]
    else:
        command += ["-map", "0:a:0", "-map", "0:v?", "-c:v", "copy"]
    if streams[0]["codec_name"] == "mp3":
        say(f"音轨已是 MP3，复制音轨并更新标签和封面：{source}")
        command += ["-c:a", "copy"]
    else:
        say(f"正在转码为 MP3：{source}")
        command += ["-c:a", "libmp3lame", "-q:a", "0"]
    command += [
        "-map_metadata", "-1", "-map_metadata:s", "-1", "-map_chapters", "-1",
        "-metadata", f"title={title}", "-metadata", f"artist={artist}",
        "-id3v2_version", "3", "-f", "mp3", temporary,
    ]
    run(command)
    move_file(temporary, destination, overwrite=True)
    if source != destination:
        source.unlink()


def process_video(source: Path, destination: Path, work_directory: Path) -> None:
    subtitles = []
    for subtitle in sorted(work_directory.glob("subtitle.*")):
        if not subtitle.is_file():
            continue
        if subtitle.suffix != ".srt" or subtitle.stat().st_size == 0:
            say(f"跳过未完成或未能转为 SRT 的字幕：{subtitle.name}", error=True)
            continue
        subtitles.append(subtitle)
    languages = [path.name[len("subtitle."):-len(".srt")] for path in subtitles]
    if source.suffix.lower() == ".mp4" and not subtitles:
        say(f"视频已是 MP4，跳过转码：{source}")
        if source != destination:
            move_file(source, destination)
        return

    command = ["ffmpeg", "-hide_banner", "-nostdin", "-stats", "-y", "-i", source]
    for subtitle in subtitles:
        command += ["-i", subtitle]
    if source.suffix.lower() == ".mp4":
        say(f"视频已是 MP4，复制音视频轨并嵌入软字幕：{source}")
        command += ["-c:v", "copy", "-c:a", "copy"]
    else:
        say(f"正在转码为 MP4，并保留软字幕：{source}")
        command += ["-c:v", "libx264", "-preset", "medium", "-crf", "23", "-c:a", "aac"]

    maps = ["-map", "0:v:0", "-map", "0:a?"]
    subtitle_args = ["-c:s", "mov_text"]
    subtitle_count = 0
    # 一次读取所有字幕轨的索引和标记；只替换本脚本嵌入的同语言旧字幕。
    for stream in probe_streams(source, "s", "stream=index:stream_tags=handler_name"):
        handler = stream.get("tags", {}).get("handler_name", "")
        if handler.startswith("yt-dlp:") and handler.removeprefix("yt-dlp:") in languages:
            continue
        index = stream["index"]
        maps += ["-map", f"0:{index}", f"-map_metadata:s:s:{subtitle_count}", f"0:s:{index}"]
        subtitle_count += 1
    for input_index, language in enumerate(languages, start=1):
        maps += ["-map", f"{input_index}:s:0"]
        subtitle_args += [
            f"-metadata:s:s:{subtitle_count}", f"language={subtitle_mp4_language(language)}",
            f"-metadata:s:s:{subtitle_count}", f"title={language}",
            f"-metadata:s:s:{subtitle_count}", f"handler_name=yt-dlp:{language}",
            f"-disposition:s:{subtitle_count}", "0",
        ]
        subtitle_count += 1
    temporary = temporary_media(work_directory)
    run(command + maps + subtitle_args + [
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-movflags", "+faststart", "-f", "mp4", temporary,
    ])
    move_file(temporary, destination, overwrite=(source == destination))
    if source != destination:
        source.unlink()


def extract_metadata(command, common_args, config: Config, mode: str, url: str, metadata: Path):
    info_file = metadata / "media.info.json"
    filename_file = metadata / "filename.json"
    arguments = [
        "--skip-download", "--write-info-json", "--output", f"infojson:{metadata}/media.%(ext)s",
        "--print-to-file", "video:%(filename)j", filename_file,
    ]
    if mode == "audio":
        arguments += ["--write-thumbnail", "--output", "thumbnail:cover.%(ext)s"]
    elif config.subtitles_enabled:
        # 字幕只准备一次；该阶段允许字幕失败，随后仍验证必要元数据。
        arguments += [
            "--ignore-errors", "--write-subs", "--write-auto-subs",
            "--sub-langs", subtitle_language_pattern(config.subtitle_languages),
            "--sub-format", "srt/vtt/ass/ssa/ttml/best", "--convert-subs", "srt",
            "--paths", f"temp:{metadata}", "--paths", f"subtitle:{metadata}",
            "--output", "subtitle:subtitle.%(ext)s",
        ]
    result = run(command + common_args + arguments + ["--", url], check=False)
    if result.returncode and not (mode == "video" and config.subtitles_enabled):
        raise DownloadError("无法取得视频信息。", result.returncode)
    if not info_file.is_file() or not filename_file.is_file():
        raise DownloadError(f"未取得可处理的视频信息：{url}")
    info = json.loads(info_file.read_text(encoding="utf-8"))
    filename = json.loads(filename_file.read_text(encoding="utf-8"))
    if not isinstance(info, dict) or not isinstance(filename, str) or not filename:
        raise DownloadError(f"未取得可处理的视频信息：{url}")
    if result.returncode:
        say("提示：字幕准备阶段报告错误；视频信息已取得，将继续处理可用字幕和视频。", error=True)
    return Path(filename).name, info


def process_url(
    config: Config, mode: str, max_height: Optional[int], url: str,
    js_runtime: str, upload_destination: str, directory: Path,
) -> None:
    metadata = directory / ".metadata"
    metadata.mkdir()
    # 与频道脚本一致，避免 yt-dlp 把新 Cookie 保存回用户提供的源文件。
    cookies = metadata / "cookies.txt"
    shutil.copyfile(config.cookies_file, cookies)
    command = ["yt-dlp", "--format"]
    if mode == "audio":
        command += ["bestaudio"]
    else:
        command += [
            f"bv*[height<={max_height}]+ba/b[height<={max_height}]",
            "--format-sort-force", "--format-sort", "height",
        ]
    common_args = [
        "--ignore-config", "--no-playlist", "--abort-on-error",
        "--no-overwrites", "--no-windows-filenames", "--no-simulate", "--check-formats",
        "--cookies", cookies,
        "--paths", f"home:{directory}", "--paths", f"temp:{directory}",
        "--paths", f"thumbnail:{metadata}", "--output", filename_template(config),
    ]
    if js_runtime:
        common_args += ["--js-runtimes", js_runtime]
    filename, info = extract_metadata(command, common_args, config, mode, url, metadata)
    extension = ".mp3" if mode == "audio" else ".mp4"
    destination = config.download_directory / Path(filename).with_suffix(extension)
    if destination.is_file():
        say(f"复用已有成品：{destination}")
        source = destination
    elif (config.download_directory / filename).is_file():
        source = config.download_directory / filename
        say(f"复用已下载的源文件：{source}")
    else:
        downloaded = metadata / "downloaded.json"
        # 复用刚提取的 JSON，避免再次解析网页；JSON 路径记录保留引号和换行。
        run(command + common_args + [
            "--no-write-thumbnail", "--load-info-json", metadata / "media.info.json",
            "--print-to-file", "after_move:%(filepath)j", downloaded,
        ])
        source = Path(json.loads(downloaded.read_text(encoding="utf-8")))
    if not source.is_file():
        raise DownloadError(f"找不到已下载的媒体：{source}")
    if mode == "audio":
        cover = next((path for path in sorted(metadata.glob("cover.*")) if path.is_file()), None)
        process_audio(
            source, destination, info.get("title") or "NA",
            info.get("channel") or info.get("uploader") or "", cover, metadata,
        )
    else:
        process_video(source, destination, metadata)
    if config.upload_enabled:
        run(["rclone", "copy", "--progress", "--", destination, upload_destination])


def parse_arguments(argv: Optional[list[str]], config: Config):
    parser = argparse.ArgumentParser(
        description="下载 YouTube / Bilibili 音频或视频，保存到配置目录，并按开关上传。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""示例：
  python3 Download/download_yt.py audio "视频 URL"
  python3 Download/download_yt.py video "URL 1" "URL 2"

配置位于脚本顶部 Config；音频和视频共用下载目录与文件名前缀开关。
时间戳前缀在 ID 前；缺少时间戳时该段留空。文件名非法字符由 yt-dlp 替换。
video 下载所有配置语言的可用字幕并嵌入软字幕；字幕缺失或下载失败时跳过。
已有 MP4 复制音视频轨嵌字幕，其他格式转 H.264/AAC；成功后删除源文件。
audio 转 MP3，写入标题、频道名和可用封面；已有 MP3 复制音轨。
重跑复用同名成品，更新音频标签和封面或同语言字幕；失败时保留源媒体。
两种模式都强制使用 Netscape 格式 Cookie；请自行导出并及时更新。
需要 yt-dlp、ffmpeg、ffprobe；YouTube 另需 Deno 2.3+ 或 Node.js 22+。
yt-dlp 请手动下载官方独立可执行文件并加入 PATH；上传需要配置好 rclone 远端。

下载目录：{config.download_directory}
Cookie 文件：{config.cookies_file}
视频高度上限：{config.video_max_resolution}
文件名时间戳开关：{config.filename_timestamp_enabled}
文件名 ID 开关：{config.filename_id_enabled}
字幕下载开关：{config.subtitles_enabled}
字幕语言：{config.subtitle_languages}
上传开关：{config.upload_enabled}
音频上传目录：{config.audio_upload_destination}
视频上传目录：{config.video_upload_destination}""",
    )
    parser.add_argument("mode", choices=("audio", "video"), help="audio：MP3；video：MP4")
    parser.add_argument("urls", nargs="+", metavar="URL", help="视频网址，请加引号，可提供多个")
    return parser.parse_args(argv)


def handle_termination(signum, _frame) -> None:
    raise DownloadError("收到终止信号；已下载的媒体文件会保留。", 128 + signum)


def main(argv: Optional[list[str]] = None, config: Config = CONFIG) -> int:
    say(f"download_yt.py 版本：{SCRIPT_VERSION}")
    arguments = parse_arguments(argv, config)
    directory = None
    previous_handler = signal.signal(signal.SIGTERM, handle_termination)
    try:
        config = validate_config(config, arguments.mode)
        check_tools(config.upload_enabled)
        config.download_directory.mkdir(parents=True, exist_ok=True)
        if not os.access(config.download_directory, os.W_OK | os.X_OK):
            raise DownloadError(f"下载目录不可写或无法进入：{config.download_directory}")
        version = run(["yt-dlp", "--ignore-config", "--version"], capture=True).stdout.strip()
        help_text = run(["yt-dlp", "--ignore-config", "--help"], capture=True).stdout
        js_runtime = ""
        if "--js-runtimes" in help_text:
            js_runtime = next((tool for tool in ("deno", "node") if shutil.which(tool)), "")
        else:
            say("提示：当前 yt-dlp 版本较旧，建议手动下载新版二进制文件。", error=True)
            yt_dlp_install_hint()
        for url in arguments.urls:
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise DownloadError(f"请提供完整的 http:// 或 https:// 视频网址：{url}")
            host = parsed.hostname.lower()
            if host in ("youtube.com", "youtu.be") or host.endswith((".youtube.com", ".youtu.be")):
                if "--js-runtimes" in help_text and not js_runtime:
                    raise DownloadError(
                        "缺少 YouTube 所需的 JavaScript 运行时（Deno 2.3+ 或 Node.js 22+）。\n"
                        "  安装命令：sudo apt install nodejs\n"
                        "  若软件源版本低于 22，请使用新版软件源：https://nodejs.org/"
                    )
        mode = arguments.mode
        max_height = resolution_height(config.video_max_resolution) if mode == "video" else None
        destination = config.audio_upload_destination if mode == "audio" else config.video_upload_destination
        if config.upload_enabled:
            check_upload_remote(destination)
        say(f"yt-dlp：{version}（{shutil.which('yt-dlp')}）")
        say("提醒：请及时将 yt-dlp 升级到最新版本，旧版本可能因网站变更而无法下载。\n"
            "新版下载：https://github.com/yt-dlp/yt-dlp/releases/latest")
        label = "音频（MP3，含标题、频道名和可用封面）" if mode == "audio" else f"视频（MP4，高度不超过 {max_height} 像素）"
        say(f"下载类型：{label}\n下载目录：{config.download_directory}")
        say(f"文件名时间戳开关：{config.filename_timestamp_enabled}\n文件名 ID 开关：{config.filename_id_enabled}")
        if mode == "video":
            say(f"字幕下载开关：{config.subtitles_enabled}\n字幕语言：{config.subtitle_languages}")
        say(f"上传目录：{destination}" if config.upload_enabled else "上传已关闭，只保存到本地。")
        for url in arguments.urls:
            directory = Path(tempfile.mkdtemp(prefix="download-media-work.")).absolute()
            process_url(config, mode, max_height, url, js_runtime, destination, directory)
            cleanup_download_files(directory, config.download_directory)
            directory = None
        say(f"处理完成，本地成品保留在：{config.download_directory}")
        return 0
    except KeyboardInterrupt:
        say("已中断；已下载的媒体文件会保留。", error=True)
        return 130
    except (DownloadError, OSError, ValueError) as error:
        say(f"错误：{error}", error=True)
        if directory is not None:
            say("下载、处理或上传失败；已下载的媒体文件会保留，后续 URL 已停止。", error=True)
            if config.upload_enabled:
                destination = (
                    config.audio_upload_destination if arguments.mode == "audio"
                    else config.video_upload_destination
                )
                say(
                    '若仅上传失败，可重跑相同命令，或运行：rclone copy --progress -- '
                    f'"本地文件路径" {shlex.quote(destination)}',
                    error=True,
                )
        return error.status if isinstance(error, DownloadError) else 1
    finally:
        if directory is not None:
            cleanup_download_files(directory, config.download_directory)
        signal.signal(signal.SIGTERM, previous_handler)


if __name__ == "__main__":
    sys.exit(main())
