#!/usr/bin/env python3
"""轮询微信未读角标，发现未读后发送 Telegram 通知并退出。

先在本脚本同目录手动创建 weixin_unread.env（Bash 格式，不要提交到 Git）：
    TG_BOT_TOKEN='你的机器人 token'
    TG_CHAT_ID='你的会话 ID'
建议执行 chmod 600 weixin_unread.env，然后运行：python weixin_unread.py
启动时由 Bash source 并检查两项配置非空，每次发送时重新加载；Python 不读取凭据。

宿主机只需 Python 3、Docker 和 Bash，图像处理使用容器自带的 Pillow。
完整截图只在内存中处理，通知裁剪图临时保存在 /tmp，发送流程结束后清理。
检测区域按微信全屏时的界面标定，调整 UI 缩放后需重新核对坐标。
"""

import base64
from datetime import datetime, timedelta, timezone
import inspect
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time


# ==================== 用户配置 ====================
CONTAINER_NAME = "weixin"
POLL_INTERVAL_SECONDS = 1 * 40 * 60  # 轮询间隔，单位秒。
REMINDER_INTERVAL_SECONDS = 18 * 60 * 60  # 无消息时的运行提醒间隔；启动满此时长后首次提醒。
TG_ENV_FILE = Path(__file__).resolve().with_name("weixin_unread.env")
TG_SEND_SCRIPT = Path.home() / "codes/Tools/Chat/tg_send.sh"  # 相对于当前用户的 home 目录。
TG_RETRY_INTERVAL_SECONDS = 60  # 发送失败后等待 1 分钟再重试。
TG_MAX_RETRIES = 3  # 首次发送失败后最多再重试 3 次，总共最多发送 4 次。
# 4 次发送均失败时，在当前用户 home 目录 touch WEIXIN_SEND_TO_TG_FAILED。

# 以下两个矩形参数专为 1024×768 桌面分辨率、微信窗口最大化状态校准，
# 对应 Docker 镜像 lscr.io/linuxserver/weixin:1a7800ae-ls50（tag：1a7800ae-ls50）。
# 更改分辨率、窗口状态、界面缩放或镜像版本后，需要重新核对并调整两个矩形。
# 检测区域：左、上、右、下；右边和下边不包含在内。
# 覆盖左侧消息图标及其右上角角标，避开会话列表里的头像。
DEFAULT_ROI = (0, 85, 60, 145)
MIN_RED_PIXELS = 50
# 通知附图区域：左、上、右、下；超出截图右/下边界时自动裁到边界。
# 左上角 300×410，包含文件传输助手及前四个会话。
NOTIFICATION_ROI = (0, 0, 300, 410)
# 红色判定：各通道在以下闭区间内，且 R 至少比 G、B 高 RED_MIN_DIFFERENCE。
RED_RGB_MIN = (200, 0, 0)
RED_RGB_MAX = (255, 130, 130)
RED_MIN_DIFFERENCE = 80
# ==================================================

SCRIPT_VERSION = "0.5.2"
# 3001 是 Nginx 对外的 HTTPS 网页入口；8082 是容器内的 Selkies HTTP 后端。
# 截图请求通过 docker exec 在容器内执行，127.0.0.1 指容器自身，无需映射 8082。
# docker ps 只列声明/映射端口，因此未显示 8082 不代表它没有监听。
SCREENSHOT_URL = "http://127.0.0.1:8082/api/screenshot"
JST = timezone(timedelta(hours=9), "JST")
OPERATION_ERRORS = (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired)
ENV_LOADER = '''set +x
set -ae
unset TG_BOT_TOKEN TG_CHAT_ID DEFAULT_BOT_TOKEN DEFAULT_CHAT_ID
source "$1" >/dev/null 2>&1
set +ax
: "${TG_BOT_TOKEN:?TG_BOT_TOKEN 不能为空}"
: "${TG_CHAT_ID:?TG_CHAT_ID 不能为空}"
'''


def capture_worker():
    """在容器内执行；截图和裁剪都只使用内存。"""
    import base64
    import io
    import json
    import sys
    from urllib.request import urlopen
    from PIL import Image

    options = json.loads(sys.argv[1])
    with urlopen(options["url"], timeout=20) as response:
        data = response.read()
    with Image.open(io.BytesIO(data)) as image:
        left, top, right, bottom = options["roi"]
        if not (0 <= left < right <= image.width and 0 <= top < bottom <= image.height):
            raise ValueError(f"检测区域超出截图范围 {image.size}")
        crop = image.crop((left, top, right, bottom)).convert("RGB")
        pixels = crop.load()
        red_min, red_max = options["red_min"], options["red_max"]
        red_pixels = 0
        for y in range(crop.height):
            for x in range(crop.width):
                r, g, b = pixels[x, y]
                if (red_min[0] <= r <= red_max[0]
                        and red_min[1] <= g <= red_max[1]
                        and red_min[2] <= b <= red_max[2]
                        and r - max(g, b) >= options["red_min_difference"]):
                    red_pixels += 1
        result = {"has_unread": red_pixels >= options["min_red_pixels"], "red_pixels": red_pixels}
        if result["has_unread"]:
            left, top, right, bottom = options["notification_roi"]
            right, bottom = min(right, image.width), min(bottom, image.height)
            if not (0 <= left < right and 0 <= top < bottom):
                raise ValueError("通知截图区域不在当前画面内")
            notification_roi = (left, top, right, bottom)
            with io.BytesIO() as buffer:
                image.crop(notification_roi).convert("RGB").save(buffer, format="PNG")
                result["notification_png_base64"] = base64.b64encode(buffer.getvalue()).decode("ascii")
    print(json.dumps(result))


def check():
    """截取当前画面并返回检测结果，不留下截图或容器内脚本文件。"""
    # 只传递图像处理代码；配置中的 TG 凭据不会进入容器命令。
    worker = inspect.getsource(capture_worker) + "\ncapture_worker()\n"
    options = {
        "url": SCREENSHOT_URL,
        "roi": DEFAULT_ROI,
        "min_red_pixels": MIN_RED_PIXELS,
        "notification_roi": NOTIFICATION_ROI,
        "red_min": RED_RGB_MIN,
        "red_max": RED_RGB_MAX,
        "red_min_difference": RED_MIN_DIFFERENCE,
    }
    completed = subprocess.run(
        ["docker", "exec", CONTAINER_NAME, "python3", "-c", worker, json.dumps(options)],
        capture_output=True,
        timeout=30,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.decode("utf-8", errors="replace").strip()
                           or f"容器命令退出码：{completed.returncode}")
    result = json.loads(completed.stdout)
    if result["has_unread"]:
        result["notification_png"] = base64.b64decode(result.pop("notification_png_base64"), validate=True)
    return result


def notification_message(timestamp=None):
    timestamp = int(time.time() if timestamp is None else timestamp)
    jst_time = datetime.fromtimestamp(timestamp, JST).strftime("%Y-%m-%d %H:%M:%S JST")
    return (f"Unix 时间：{timestamp}\n"
            f"JST 时间：{jst_time}\n"
            "微信有新的消息，微信消息检查脚本已经退出。")


def format_duration(seconds):
    if not math.isfinite(seconds) or seconds < 0:
        return f"{seconds:g} 秒"
    hours, remainder = divmod(int(seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return " ".join(f"{value} {unit}" for value, unit in
                    ((hours, "小时"), (minutes, "分钟"), (seconds, "秒")) if value) or "0 秒"


def reminder_message(uptime, since_last_reminder, timestamp=None):
    timestamp = int(time.time() if timestamp is None else timestamp)
    jst_time = datetime.fromtimestamp(timestamp, JST).strftime("%Y-%m-%d %H:%M:%S JST")
    return (f"Unix 时间：{timestamp}\n"
            f"JST 时间：{jst_time}\n"
            f"程序已经运行 {format_duration(uptime)}，现在仍在运行中，"
            f"过去 {format_duration(since_last_reminder)}内没有消息。")


def send_telegram(message, image_path=None):
    # 绝对路径保证不依赖启动目录；set -a 让 env 中的普通赋值也传给发送脚本。
    # 不回显 source 的输出或诊断，避免配置错误时将凭据带入日志。
    loader = ENV_LOADER + '\nexec bash "${@:2}"\n'
    command = ["bash", "-c", loader, "weixin-unread", str(TG_ENV_FILE), str(TG_SEND_SCRIPT)]
    if image_path is not None:
        command.extend(["--image", str(image_path)])
    completed = subprocess.run(
        [*command, message],
        capture_output=True,
        text=True,
        timeout=45,
    )
    if completed.returncode:
        raise RuntimeError(brief_error(completed.stderr.strip())
                           or f"TG 发送脚本退出码：{completed.returncode}")
    try:
        response = json.loads(completed.stdout)
    except ValueError as exc:
        raise RuntimeError("TG 发送脚本未返回有效的 Telegram JSON 响应") from exc
    if not isinstance(response, dict) or response.get("ok") is not True:
        description = response.get("description", "未知错误") if isinstance(response, dict) else "未知错误"
        raise RuntimeError(f"Telegram 拒绝发送：{brief_error(description)}")


def log(message):
    timestamp = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    print(f"[{timestamp}] {message}", flush=True)


def brief_error(error):
    text = re.sub(r"\d{5,}:[A-Za-z0-9_-]{20,}", "<BOT_TOKEN>", str(error))
    return " ".join(text.split())[:240]


def send_with_retries(message, image_path=None):
    for attempt in range(TG_MAX_RETRIES + 1):
        try:
            send_telegram(message, image_path=image_path)
        except OPERATION_ERRORS as exc:
            if attempt == TG_MAX_RETRIES:
                marker = Path.home() / "WEIXIN_SEND_TO_TG_FAILED"
                marker.touch()
                log(f"TG 发送失败：{brief_error(exc)}；"
                    f"已用完 {TG_MAX_RETRIES} 次重试，脚本退出；失败标记：{marker}")
                return 1
            log(f"TG 发送失败：{brief_error(exc)}；"
                f"{format_duration(TG_RETRY_INTERVAL_SECONDS)}后进行第 {attempt + 1}/{TG_MAX_RETRIES} 次重试")
            time.sleep(TG_RETRY_INTERVAL_SECONDS)
        else:
            return 0


def notify_with_image(result):
    """在所有重试中复用同一张裁剪图，离开流程时清理临时目录。"""
    with tempfile.TemporaryDirectory(prefix="weixin-unread-", dir="/tmp") as directory:
        image_path = Path(directory) / "notification.png"
        image_path.write_bytes(result["notification_png"])
        status = send_with_retries(notification_message(), image_path=image_path)
        if status == 0:
            log(f"check 有未读，红色像素={result['red_pixels']}；TG 已发送（含截图），脚本退出")
        return status


def monitor():
    """启动后立即检查；发现未读后发送通知，重试耗尽或发送成功均退出。"""
    started = last_reminder = time.monotonic()
    while True:
        try:
            result = check()
        except OPERATION_ERRORS as exc:
            log(f"check 失败：{brief_error(exc)}；{format_duration(POLL_INTERVAL_SECONDS)}后重试")
        else:
            pixels = result["red_pixels"]
            if result["has_unread"]:
                return notify_with_image(result)
            else:
                now = time.monotonic()
                reminder_note = ""
                if now - last_reminder >= REMINDER_INTERVAL_SECONDS:
                    if send_with_retries(reminder_message(now - started, now - last_reminder)):
                        return 1
                    last_reminder = time.monotonic()
                    reminder_note = "运行状态提醒已发送；"
                log(f"check 无未读，红色像素={pixels}；{reminder_note}{format_duration(POLL_INTERVAL_SECONDS)}后检查")
        time.sleep(POLL_INTERVAL_SECONDS)


def main():
    log(f"weixin_unread.py v{SCRIPT_VERSION} | PID={os.getpid()} | 容器={CONTAINER_NAME} | "
        f"轮询间隔={format_duration(POLL_INTERVAL_SECONDS)} | "
        f"无消息提醒间隔={format_duration(REMINDER_INTERVAL_SECONDS)}")
    print("提醒：请保持微信窗口全屏显示。", flush=True)

    for name, seconds in (("轮询间隔", POLL_INTERVAL_SECONDS), ("提醒间隔", REMINDER_INTERVAL_SECONDS)):
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError(f"{name}必须是大于 0 的有限秒数")
    for name, roi in (("检测区域", DEFAULT_ROI), ("通知截图区域", NOTIFICATION_ROI)):
        left, top, right, bottom = roi
        if not (0 <= left < right and 0 <= top < bottom):
            raise ValueError(f"{name}必须满足 0 <= 左 < 右、0 <= 上 < 下")
    left, top, right, bottom = DEFAULT_ROI
    if not 1 <= MIN_RED_PIXELS <= (right - left) * (bottom - top):
        raise ValueError("红色像素阈值必须在 1 和 ROI 总像素数之间")
    if not all(0 <= low <= high <= 255 for low, high in zip(RED_RGB_MIN, RED_RGB_MAX)):
        raise ValueError("红色 RGB 范围必须满足 0 <= 最小值 <= 最大值 <= 255")
    if not 0 <= RED_MIN_DIFFERENCE <= 255:
        raise ValueError("红色色差必须在 0 到 255 之间")

    if not TG_ENV_FILE.is_file():
        raise ValueError(f"请在脚本同目录创建配置文件：{TG_ENV_FILE}")
    loaded = subprocess.run(
        ["bash", "-c", ENV_LOADER, "weixin-unread", str(TG_ENV_FILE)],
        capture_output=True, timeout=5,
    )
    if loaded.returncode:
        raise ValueError("无法加载 weixin_unread.env，请检查文件格式及 TG_BOT_TOKEN、TG_CHAT_ID 是否为空")
    if not TG_SEND_SCRIPT.is_file():
        raise ValueError(f"TG 发送脚本不存在：{TG_SEND_SCRIPT}")
    return monitor()


def handle_termination(signum, frame):
    # SIGTERM 走正常的异常清理流程，确保 /tmp 中的通知图片也被删除。
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_termination)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("收到中断，脚本已退出")
        sys.exit(130)
    except OPERATION_ERRORS as exc:
        log(f"错误：{brief_error(exc)}")
        sys.exit(2)
