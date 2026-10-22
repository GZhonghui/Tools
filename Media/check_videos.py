#!/usr/bin/env python3
"""完整解码检查当前目录中的视频，生成 JSON 报告；Python 3.9+，仅使用标准库。

运行前修改顶部 Config，再执行 python3 Media/check_videos.py。
只扫描指定目录的直接子文件，不递归；视频只读，不修复、不删除、不生成转码文件。
ffprobe 读取编码和分辨率，ffmpeg 完整解码所有视频及音轨，忽略字幕、封面和数据轨。
默认分块读取原始文件计算 MD5，可通过 md5_enabled 关闭；关闭时新记录的 md5 为 null。
通过仅表示本次未发现解码问题，不能证明文件与服务器原始视频一致或内容没有缺失。
JSON 为检查记录数组，仅按 filename 精确判重；已有记录不修改，文件已不存在的记录也保留。
检查及启用的 MD5 计算均结束后才追加并原子保存；程序出错或中断时不写入未完成结果。
发现视频问题、已正常结束的 failed / warning 检查也会记录，下次同样跳过。
诊断日志和 JSON 写入中间文件在正常成功、失败及可捕获的中断时均清理。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Optional

SCRIPT_VERSION = "0.1.1"
MAX_DIAGNOSTIC_BYTES = 16 * 1024
MD5_CHUNK_BYTES = 1024 * 1024


# ---- 配置：运行前按需修改 ----
@dataclass(frozen=True)
class Config:
    # 只扫描这一层目录，不递归；相对路径以执行位置为基准。
    video_directory: Path = Path.home() / "Downloads"
    # 扩展名不区分大小写，可使用带点或不带点的写法。
    video_extensions: tuple[str, ...] = (".mp4", ".mkv")
    # 计算原始文件的 MD5；每次读取 1 MiB，不把整个视频加载到内存。
    # 关闭时新记录的 md5 为 null；已有记录仍跳过，不补算或修改。
    md5_enabled: bool = True
    # JSON 为记录数组；按文件名（区分大小写）跳过已有项，不比较文件大小、时间或内容。
    # 只追加完整检查后的记录；已有项及磁盘上已不存在的文件记录均原样保留。
    # 要重新检查某个同名文件，请先手动删除它在 JSON 中的记录。
    report_file: Path = Path.home() / "Downloads" / "video_check.json"


CONFIG = Config()
RESULT_LABELS = {
    "passed": "通过", "warning": "警告，需复核", "failed": "失败",
}


class CheckError(Exception):
    pass


class Terminated(Exception):
    pass


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def say(message: str, *, error: bool = False) -> None:
    print(message, file=sys.stderr if error else sys.stdout, flush=True)


def run_tool(command: list[str], *, capture_stdout: bool = False) -> subprocess.CompletedProcess:
    # 日志写入自动清理的临时文件，避免长视频的大量警告占满内存；JSON 最多保留 16 KiB。
    with tempfile.TemporaryFile() as diagnostics:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            stderr=diagnostics,
        )
        try:
            stdout, _ = process.communicate()
        except BaseException:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
        size = diagnostics.seek(0, os.SEEK_END)
        diagnostics.seek(0)
        if size <= MAX_DIAGNOSTIC_BYTES:
            data = diagnostics.read()
        else:
            head = diagnostics.read(MAX_DIAGNOSTIC_BYTES // 2)
            diagnostics.seek(-MAX_DIAGNOSTIC_BYTES // 2, os.SEEK_END)
            data = head + "\n（中间诊断输出已截断）\n".encode() + diagnostics.read()
    message = data.decode("utf-8", errors="replace").strip()
    return subprocess.CompletedProcess(
        command, process.returncode,
        stdout=(stdout or b"").decode("utf-8", errors="replace"), stderr=message,
    )


def validate_config(config: Config) -> Config:
    if type(config.md5_enabled) is not bool:
        raise CheckError("请将 md5_enabled 设置为 True 或 False。")
    if not str(config.video_directory) or not str(config.report_file):
        raise CheckError("video_directory 和 report_file 不能为空。")
    directory = Path(config.video_directory).expanduser().resolve()
    report = Path(config.report_file).expanduser().resolve()
    if not directory.is_dir():
        raise CheckError(f"视频目录不存在或不是目录：{directory}")
    if report.suffix.lower() != ".json" or report.is_dir():
        raise CheckError("report_file 必须指向一个 .json 文件。")
    if not isinstance(config.video_extensions, (list, tuple)) or not config.video_extensions:
        raise CheckError("video_extensions 必须是非空扩展名列表或元组。")
    extensions = []
    for extension in config.video_extensions:
        if not isinstance(extension, str) or not re.fullmatch(r"\.?[A-Za-z0-9]+", extension):
            raise CheckError(f"视频扩展名无效：{extension!r}")
        extensions.append("." + extension.lower().lstrip("."))
    return replace(config, video_directory=directory, report_file=report,
                   video_extensions=tuple(dict.fromkeys(extensions)))


def check_tools() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            command = "brew install ffmpeg" if sys.platform == "darwin" else "sudo apt install ffmpeg"
            raise CheckError(f"缺少 {tool}，请先安装 FFmpeg 并加入 PATH。安装命令：{command}")
        result = run_tool([tool, "-version"])
        if result.returncode:
            raise CheckError(f"{tool} 无法正常运行：{result.stderr}")


def load_report(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        records = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError) as error:
        raise CheckError(f"报告不是有效的 UTF-8 JSON，未修改文件：{path}") from error
    if not isinstance(records, list) or any(
        not isinstance(record, dict) or not isinstance(record.get("filename"), str)
        or not record["filename"] for record in records
    ):
        raise CheckError(f"报告必须是记录数组，每条包含非空 filename，未修改文件：{path}")
    return records


def probe_video(path: Path, record: dict) -> str:
    result = run_tool([
        "ffprobe", "-hide_banner", "-v", "warning", "-show_entries",
        "stream=index,codec_type,codec_name,width,height:stream_disposition=attached_pic,default:format=duration",
        "-of", "json", str(path),
    ], capture_stdout=True)
    if result.returncode:
        raise CheckError(f"ffprobe 无法读取视频信息：{result.stderr or result.returncode}")
    info = json.loads(result.stdout)
    videos = [stream for stream in info.get("streams", [])
              if stream.get("codec_type") == "video"
              and not stream.get("disposition", {}).get("attached_pic")]
    if not videos:
        raise CheckError("文件中没有视频轨道（封面图片不算视频）。")
    primary = next((stream for stream in videos if stream.get("disposition", {}).get("default")), videos[0])
    record["codec"] = primary.get("codec_name")
    record["width"], record["height"] = primary.get("width"), primary.get("height")
    dimensions = [record["width"], record["height"]]
    if all(isinstance(value, int) and value > 0 for value in dimensions):
        # 与下载脚本相同，按短边命名：1080×1920 竖屏同样显示为 1080p。
        record["resolution"] = f"{min(dimensions)}p"
    record["audio_codecs"] = list(dict.fromkeys(
        stream.get("codec_name") for stream in info.get("streams", [])
        if stream.get("codec_type") == "audio"
    ))
    record["video_streams"] = [
        {key: stream.get(key) for key in ("index", "codec_name", "width", "height")}
        for stream in videos
    ]
    try:
        duration = float(info.get("format", {}).get("duration", "nan"))
        if math.isfinite(duration) and duration >= 0:
            record["duration_seconds"] = duration
    except (TypeError, ValueError):
        pass
    return result.stderr


def calculate_md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(MD5_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def check_video(path: Path, *, md5_enabled: bool = True) -> dict:
    record = {
        "filename": path.name, "codec": None, "audio_codecs": [], "video_streams": [],
        "width": None, "height": None, "resolution": None, "duration_seconds": None,
        "result": None, "checked_at": None, "elapsed_seconds": None,
        "ffmpeg_exit_code": None, "md5": None, "details": "",
    }
    started = time.monotonic()
    try:
        before = path.stat()
        try:
            warning = probe_video(path, record)
            result = run_tool([
                "ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-v", "warning",
                "-xerror", "-abort_on", "empty_output_stream", "-err_detect", "explode",
                "-i", str(path), "-map", "0:V?", "-map", "0:a?", "-sn", "-dn", "-f", "null", "-",
            ])
            record["ffmpeg_exit_code"] = result.returncode
            record["details"] = "\n".join(message for message in (warning, result.stderr) if message)
            if result.returncode:
                record["result"] = "failed"
                if not record["details"]:
                    record["details"] = f"ffmpeg 退出码：{result.returncode}"
            else:
                record["result"] = "warning" if record["details"] else "passed"
        except CheckError as error:
            record["result"] = "failed"
            record["details"] = str(error)
        # 已完成但发现损坏的检查也计算 MD5；读取失败或中断会向外抛出，不保存当前记录。
        if md5_enabled:
            record["md5"] = calculate_md5(path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            if record["result"] == "passed":
                record["result"] = "warning"
            # 文件发生变化时，MD5 可能与解码时的内容不对应，不保留这个校验值。
            record["md5"] = None
            record["details"] += "\n文件在检查期间被修改，本次结果不能用于确认完整性，请稍后重新检查。"
    finally:
        record["checked_at"] = now()
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
    # 中断或程序异常会直接向外抛出；只有正常结束的检查才返回可保存的完整记录。
    return record


def write_report(path: Path, records: list[dict]) -> None:
    # 逻辑上仅追加记录；落盘时完整写入临时文件，再原子替换，避免留下半个 JSON。
    descriptor, name = tempfile.mkstemp(prefix=".video-check-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(records, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def handle_termination(_signum, _frame) -> None:
    raise Terminated()


def main(argv: Optional[list[str]] = None, config: Config = CONFIG) -> int:
    parser = argparse.ArgumentParser(
        description="完整解码检查本层目录中的视频，结果写入 JSON，不修改源视频。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""运行前修改顶部 Config 中的 video_directory、video_extensions 和 report_file。
需要 ffmpeg 和 ffprobe；默认检查 MP4、MKV，不递归。
结果：passed=本次检查通过，warning=有警告需复核，failed=检查失败。
所有音视频轨都会解码；JSON 顶层编码和分辨率描述默认视频轨，其余视频轨列在 video_streams。
分辨率按短边表示，横屏 1920×1080 和竖屏 1080×1920 均为 1080p。
md5_enabled 默认开启，分块读取原始文件计算 MD5；关闭时新记录的 md5 为 null。
文件在检查或 MD5 计算期间被修改时，md5 置空，并在 details 中注明。
checked_at 为带时区的检查日期，elapsed_seconds 为耗时秒数（包含 MD5 计算）。
JSON 为记录数组，按 filename 精确匹配；同名文件直接跳过，旧记录和已不存在文件的记录保留。
已有记录即使缺少 MD5 也不会补算；需要时先手动删除对应 JSON 记录。
每个文件的检查及启用的 MD5 计算均结束后才追加保存；程序异常或中断时不写入未完成结果。
已正常结束的 failed / warning 检查也会记录；要重查，请手动删除对应 JSON 记录。
完整解码耗时取决于视频时长、编码和设备性能；通过不代表与服务器源文件完全一致。
退出码：0=本次无新增失败或警告（含全部跳过），1=程序或报告写入失败，
2=本次检查有失败或警告，130/143=中断。""",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {SCRIPT_VERSION}")
    parser.parse_args(argv)
    say(f"check_videos.py 版本：{SCRIPT_VERSION}")
    previous_handler = signal.signal(signal.SIGTERM, handle_termination)
    try:
        config = validate_config(config)
        check_tools()
        records = load_report(config.report_file)
        known = {record["filename"] for record in records}
        files = sorted((path for path in config.video_directory.iterdir()
                        if path.is_file() and path.suffix.lower() in config.video_extensions
                        and path.resolve() != config.report_file), key=lambda path: path.name.casefold())
        pending = [path for path in files if path.name not in known]
        counts = dict.fromkeys(RESULT_LABELS, 0)
        say(f"扫描目录：{config.video_directory}\nJSON 报告：{config.report_file}\n"
            f"MD5 计算开关：{config.md5_enabled}\n"
            f"已有 {len(records)} 条记录，找到 {len(files)} 个视频，"
            f"跳过 {len(files) - len(pending)} 个同名文件，待检查 {len(pending)} 个。")
        for index, path in enumerate(pending, start=1):
            say(f"[{index}/{len(pending)}] 检查中：{path.name}")
            record = check_video(path, md5_enabled=config.md5_enabled)
            config.report_file.parent.mkdir(parents=True, exist_ok=True)
            write_report(config.report_file, records + [record])
            records.append(record)
            counts[record["result"]] += 1
            say(f"  {RESULT_LABELS[record['result']]}：{record['codec'] or '未知编码'}，"
                f"{record['width']}×{record['height']}（{record['resolution'] or '未知分辨率'}），"
                f"耗时 {record['elapsed_seconds']} 秒")
        say(f"检查结束：本次通过 {counts['passed']}，警告 {counts['warning']}，失败 {counts['failed']}。\n"
            f"新增 {sum(counts.values())} 条记录，已有记录保留。报告路径：{config.report_file}")
        return 2 if counts["warning"] or counts["failed"] else 0
    except (KeyboardInterrupt, Terminated) as error:
        say("已中断；当前文件不写入 JSON，之前已保存的记录保留，源视频不作修改。", error=True)
        return 143 if isinstance(error, Terminated) else 130
    except (CheckError, OSError, ValueError) as error:
        say(f"错误：{error}", error=True)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


if __name__ == "__main__":
    sys.exit(main())
