#!/usr/bin/env python3
"""增量收集频道普通视频 ID，生成待下载命令；Python 3.9+，仅使用标准库。

运行前修改顶部 Config 和 build_download_command，再执行：
    python3 Download/download_ch.py
输出：<工作目录>/<key>.json 和 <工作目录>/download_<key>.sh。
JSON 为从旧到新的 ID 数组；TXT 中包含某个 ID 就视为已下载，匹配区分大小写。
频道列表须按新到旧排列，首次须完整抓取；分页失败不提交本轮新增 ID。
开启 skip_fetch 后跳过列表请求，直接使用缓存生成下载脚本。
临时 Cookie 副本和写文件时的中间文件均在正常退出、失败或可捕获的中断时清理。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

SCRIPT_VERSION = "0.1.1"


# ---- 配置：运行前按需修改 ----
@dataclass(frozen=True)
class Config:
    working_directory: Path = Path.home() / "Downloads"
    # 自定义昵称，用于 JSON 和下载脚本的文件名；一个 key 对应一个频道。
    key: str = ""
    # YouTube 频道首页或 /videos；B 站 UP 主空间首页或 /upload/video。
    # 例如 https://www.youtube.com/@ami.moment/videos
    # 或 https://space.bilibili.com/27899754/upload/video
    # YouTube 只读取 /videos，排除 Shorts、直播页及播放列表。
    channel_url: str = ""
    # 开启后跳过频道列表请求，沿用 JSON 缓存；TXT 查重和命令生成规则不变。
    skip_fetch: bool = False
    # 不配置（None 或空字符串）时视为空文本；已配置但文件不可读则报错。
    downloaded_txt_path: Optional[Path] = None
    # 与下载脚本相同的 Netscape 格式 Cookie；源文件只读取，不会改写。
    cookies_file: Path = Path.home() / ".zh/etc/yt-dlp/yt-bili.txt"
    # 分页等网络请求的间隔，单位秒。
    request_interval: float = 5.0


def build_download_command(video_id: str) -> str:
    """可自行修改：输入视频 ID，返回什么命令就写入什么命令，不会自动执行。"""
    if re.fullmatch(r"BV[A-Za-z0-9]{10}", video_id):
        url = f"https://www.bilibili.com/video/{video_id}"
    else:
        url = f"https://www.youtube.com/watch?v={video_id}"
    downloader = Path(__file__).resolve().with_name("download_yt.py")
    # 绝对路径和 Shell 引号使生成的脚本可以在其他目录执行。
    return shlex.join([sys.executable, str(downloader), "video", url])


CONFIG = Config()
ID_PATTERNS = {
    "youtube": re.compile(r"[A-Za-z0-9_-]{11}"),
    "bilibili": re.compile(r"BV[A-Za-z0-9]{10}"),
}


class ChannelError(Exception):
    def __init__(self, message: str, status: int = 1):
        super().__init__(message)
        self.status = status


def say(message: str, *, error: bool = False) -> None:
    print(message, file=sys.stderr if error else sys.stdout, flush=True)


def normalize_channel_url(url: str) -> tuple[str, str]:
    """限定普通视频页，去掉可能改变排序或范围的查询参数。"""
    parsed = urlsplit(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ChannelError("请配置完整的 http:// 或 https:// 频道网址 channel_url。")
    host = parsed.hostname.lower()
    parts = parsed.path.strip("/").split("/")
    if host in ("youtube.com", "www.youtube.com", "m.youtube.com"):
        if parts[0].startswith("@") and len(parts[0]) > 1:
            length = 1
        elif len(parts) >= 2 and parts[0] in ("channel", "user", "c") and parts[1]:
            length = 2
        else:
            raise ChannelError("YouTube 请提供频道首页或 /videos 地址，不能使用单个视频网址。")
        if parts[length:] not in ([], ["videos"]):
            raise ChannelError("YouTube 仅收集普通视频，请使用频道首页或 /videos 地址。")
        return "youtube", f"https://www.youtube.com/{'/'.join(parts[:length])}/videos"
    if host == "space.bilibili.com" and re.fullmatch(r"[1-9][0-9]*", parts[0]):
        if parts[1:] not in ([], ["video"], ["upload", "video"]):
            raise ChannelError("B 站请提供 UP 主空间首页或 /upload/video 地址。")
        return "bilibili", f"https://space.bilibili.com/{parts[0]}/upload/video?order=pubdate"
    raise ChannelError("仅支持 YouTube 普通视频频道页和 Bilibili UP 主投稿页。")


def validate_config(config: Config) -> tuple[Config, str]:
    if type(config.skip_fetch) is not bool:
        raise ChannelError("请将 skip_fetch 设置为 True 或 False。")
    if not isinstance(config.key, str) or not config.key.strip():
        raise ChannelError("请在顶部 Config 中配置频道昵称 key。")
    if config.key in (".", "..") or any(
        char in "/\\" or ord(char) < 32 or ord(char) == 127 for char in config.key
    ):
        raise ChannelError("key 不能包含路径分隔符、换行或其他控制字符，也不能是 . 或 ..。")
    platform, url = normalize_channel_url(config.channel_url)
    if not str(config.working_directory):
        raise ChannelError("working_directory 不能为空。")
    if (
        isinstance(config.request_interval, bool)
        or not isinstance(config.request_interval, (int, float))
        or not math.isfinite(config.request_interval)
        or config.request_interval < 0
    ):
        raise ChannelError("request_interval 必须是大于或等于 0 的有限秒数。")
    cookies = Path(config.cookies_file).expanduser()
    if not config.skip_fetch:
        if not cookies.is_file() or not os.access(cookies, os.R_OK) or cookies.stat().st_size == 0:
            raise ChannelError(f"Cookie 文件不存在、不可读或为空：{cookies}")
        if shutil.which("yt-dlp") is None:
            raise ChannelError(
                "缺少 yt-dlp，请手动下载官方独立可执行文件并加入 PATH：\n"
                "https://github.com/yt-dlp/yt-dlp/releases/latest"
            )
    return replace(
        config, working_directory=Path(config.working_directory).expanduser(),
        cookies_file=cookies, channel_url=url,
    ), platform


def stage_text(destination: Path, text: str) -> Path:
    """先完整写入临时文件；写入失败或中断也会删除中间文件。"""
    descriptor, name = tempfile.mkstemp(prefix=".channel-list-", suffix=".tmp", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def load_cache(path: Path, platform: str) -> list[str]:
    if not path.exists():
        # 新文件先是空数组；首次抓取中断时仍保持为空，下次会重新完整抓取。
        temporary = stage_text(path, "[]\n")
        try:
            if not path.exists():
                temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError) as error:
        raise ChannelError(f"缓存不是有效的 UTF-8 JSON，未修改文件：{path}") from error
    if not isinstance(data, list) or any(
        not isinstance(video_id, str) or not ID_PATTERNS[platform].fullmatch(video_id)
        for video_id in data
    ):
        raise ChannelError(f"缓存必须是当前平台的视频 ID 字符串数组，未修改文件：{path}")
    return list(dict.fromkeys(data))


def read_downloaded_text(path: Optional[Path]) -> str:
    if path is None or path == "":
        return ""
    return Path(path).expanduser().read_text(encoding="utf-8-sig")


def listing_command(config: Config) -> list[str]:
    return [
        "yt-dlp", "--ignore-config", "--no-cache-dir", "--abort-on-error",
        "--flat-playlist", "--lazy-playlist", "--skip-download", "--dump-json", "--no-progress",
        "--cookies", str(config.cookies_file),
        "--sleep-requests", str(config.request_interval), "--socket-timeout", "20",
        "--", config.channel_url,
    ]


def stream_entries(command: list[str]):
    """逐条读取视频列表；调用方提前关闭迭代器时，立即停止分页并回收进程。"""
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
    )
    try:
        for line in process.stdout:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError as error:
                raise ChannelError("yt-dlp 返回了无效 JSON；本次抓取结果不会写入缓存。") from error
            if not isinstance(entry, dict):
                raise ChannelError("yt-dlp 返回了无效列表条目；本次抓取结果不会写入缓存。")
            yield entry
        status = process.wait()
        if status:
            raise ChannelError(f"列表请求失败（yt-dlp 退出码 {status}）；原有清单和下载脚本保留。")
    finally:
        if process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.stdout.close()


def collect_ids(config: Config, platform: str, cached: list[str]) -> list[str]:
    known = set(cached)
    newest_first = []
    seen = set()
    # yt-dlp 可能保存收到的 Cookie；使用临时副本，避免改写用户提供的源文件。
    # TemporaryDirectory 在成功、失败和可捕获的中断时均删除 Cookie 副本。
    with tempfile.TemporaryDirectory(prefix="channel-list-cookies-") as name:
        cookies = Path(name) / "cookies.txt"
        shutil.copyfile(config.cookies_file, cookies)
        request_config = replace(config, cookies_file=cookies)
        with closing(stream_entries(listing_command(request_config))) as entries:
            for entry in entries:
                video_id = entry.get("id")
                if not isinstance(video_id, str) or not ID_PATTERNS[platform].fullmatch(video_id):
                    raise ChannelError(
                        f"列表包含非普通视频条目（ID：{video_id!r}），"
                        "无法保证发布时间顺序，本次不更新清单。请检查是否为合集引用。"
                    )
                # 只与启动时的缓存比较；本轮列表内的重复条目不会触发提前停止。
                if video_id in known:
                    say(f"遇到已有 ID：{video_id}，停止分页。")
                    break
                if video_id not in seen:
                    seen.add(video_id)
                    newest_first.append(video_id)
    return cached + list(reversed(newest_first))


def render_commands(ids: list[str], downloaded_text: str) -> tuple[str, int]:
    commands = ["#!/usr/bin/env bash\n\n"]
    count = 0
    for video_id in ids:
        if video_id in downloaded_text:
            continue
        command = build_download_command(video_id)
        if not isinstance(command, str):
            raise ChannelError(f"build_download_command 必须返回字符串（ID：{video_id}）。")
        commands.append(command)
        if not command.endswith("\n"):
            commands.append("\n")
        count += 1
    return "".join(commands), count


def save_outputs(cache: Path, script: Path, ids: list[str], commands: str) -> None:
    # 两个文件先准备完毕，分别原子替换；JSON 最后提交，避免提前推进增量边界。
    # 下方 finally 无论成功、失败或中断都清理尚未提交的中间文件。
    cache_temporary = stage_text(cache, json.dumps(ids, ensure_ascii=False, indent=2) + "\n")
    try:
        script_temporary = stage_text(script, commands)
        try:
            script_temporary.replace(script)
            cache_temporary.replace(cache)
        finally:
            script_temporary.unlink(missing_ok=True)
    finally:
        cache_temporary.unlink(missing_ok=True)


def handle_termination(signum, _frame) -> None:
    raise ChannelError("收到终止信号；本轮未完成的抓取不会更新清单。", 128 + signum)


def main(argv: Optional[list[str]] = None, config: Config = CONFIG) -> int:
    parser = argparse.ArgumentParser(
        description="增量获取 YouTube 普通视频 / B 站投稿 ID，再按 TXT 查重生成下载命令。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""运行前修改顶部 Config 和 build_download_command，然后执行：
  python3 Download/download_ch.py
  bash ~/Downloads/download_<key>.sh

默认工作目录为 ~/Downloads。频道 URL 和昵称 key 必须配置。
YouTube 仅支持频道首页或 /videos，不收集 Shorts、直播页及播放列表。
<key>.json 是从旧到新的 ID 数组；下载命令也按旧到新输出，每次重新生成。
skip_fetch=True 时跳过请求，使用现有 JSON；无需 Cookie 或 yt-dlp，其他处理不变。
JSON 不存在时仍创建空数组；跳过请求后会生成没有下载命令的脚本。
TXT 按 ID 原样包含匹配、区分大小写；不配置路径时所有 ID 都会生成命令。
脚本只收集 ID 和写出命令，下载脚本由用户另行运行。
请求频道列表需要独立的 yt-dlp 和 Netscape 格式 Cookie；分页失败保留原有清单和下载脚本。
增量依赖列表顺序稳定和缓存完整；旧视频重新公开等情况需要重新完整扫描。""",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {SCRIPT_VERSION}")
    parser.parse_args(argv)
    say(f"download_ch.py 版本：{SCRIPT_VERSION}")
    previous_handler = signal.signal(signal.SIGTERM, handle_termination)
    try:
        config, platform = validate_config(config)
        config.working_directory.mkdir(parents=True, exist_ok=True)
        cache = config.working_directory / f"{config.key}.json"
        script = config.working_directory / f"download_{config.key}.sh"
        cached = load_cache(cache, platform)
        downloaded_text = read_downloaded_text(config.downloaded_txt_path)
        say(f"频道：{config.key}\n视频页：{config.channel_url}")
        if config.skip_fetch:
            say(f"缓存已有 {len(cached)} 个 ID，skip_fetch 已开启，跳过请求列表。")
            ids = cached
        else:
            say(f"缓存已有 {len(cached)} 个 ID，开始请求列表。")
            ids = collect_ids(config, platform, cached)
        commands, count = render_commands(ids, downloaded_text)
        save_outputs(cache, script, ids, commands)
        say(f"完成：共 {len(ids)} 个 ID，新增 {len(ids) - len(cached)} 个，"
            f"TXT 命中 {len(ids) - count} 个，生成 {count} 条命令。\n清单：{cache}\n下载脚本：{script}")
        return 0
    except KeyboardInterrupt:
        say("已中断；本轮未完成的抓取不会更新清单。", error=True)
        return 130
    except (ChannelError, OSError, ValueError) as error:
        say(f"错误：{error}", error=True)
        return error.status if isinstance(error, ChannelError) else 1
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


if __name__ == "__main__":
    sys.exit(main())
