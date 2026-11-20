#!/usr/bin/env python3
# ========== fetch mails v0.1 ==========
# 从 Gmail 的服务器下载 1h 内收到的所有邮件
# 使用 IMAP 协议，无需安装任何第三方依赖
# 邮箱地址在脚本开头配置，应用密码从 keys/ 下的 txt 文件第一行读取
# 作者：ChatGPT 5.6 Sol
# ======================================

"""Download recently received messages from Gmail as ``.eml`` files.

Edit the configuration section below, then run::

    python3 game/fetch_mails.py

Only Python's standard library is used.  Gmail's IMAP ``SINCE`` search works at
day granularity, so each result is additionally filtered by its IMAP
``INTERNALDATE`` to enforce the configured one-hour window exactly.
"""

from __future__ import annotations

import email.policy
import imaplib
import re
import ssl
import sys
from datetime import datetime, timedelta, timezone
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Sequence


# =========================== Configuration ============================
# Gmail address and the file holding a 16-character app password on its
# first line.  Spaces in the app password are ignored, so it may be pasted in
# the grouped form shown by Google.  Do not use the normal password of the
# Google account here.
GMAIL_ADDRESS = ""
GMAIL_APP_PASSWORD_PATH = Path(__file__).resolve().parent / "keys" / "gm.txt"

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
MAILBOX = "INBOX"
LOOKBACK = timedelta(hours=24)
# 邮件下载到脚本所在目录的 ./mails/
OUTPUT_DIR = Path(__file__).resolve().parent / "mails"
# =====================================================================


_INTERNALDATE_RE = re.compile(rb'INTERNALDATE "([^"]+)"', re.IGNORECASE)
_UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]+')
_PLACEHOLDER_VALUES = {
    "",
    "your.name@gmail.com",
    "xxxx xxxx xxxx xxxx",
    "xxxxxxxxxxxxxxxx",
}


class FetchError(RuntimeError):
    """Raised when Gmail returns an incomplete or unsuccessful response."""


def _read_app_password() -> str:
    """Return the first line of the app password file."""
    path = GMAIL_APP_PASSWORD_PATH.expanduser()
    try:
        # utf-8-sig tolerates the BOM that Windows editors may add.
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ValueError(f"未找到应用密码文件：{path}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"无法读取应用密码文件 {path}：{exc}") from exc

    lines = text.splitlines()
    return lines[0] if lines else ""


def _validate_config() -> str:
    """Validate user-editable settings and return the normalized password."""
    address = GMAIL_ADDRESS.strip()
    password = "".join(_read_app_password().split())
    password_path = GMAIL_APP_PASSWORD_PATH.expanduser()

    if address.lower() in _PLACEHOLDER_VALUES or "@" not in address:
        raise ValueError("请先在脚本顶部填写有效的 GMAIL_ADDRESS")
    if password.lower() in _PLACEHOLDER_VALUES:
        raise ValueError(
            f"请在 {password_path} 的第一行填写 Google 应用专用密码"
        )
    if len(password) != 16:
        raise ValueError(f"{password_path} 第一行的应用密码去除空格后应为 16 个字符")
    if LOOKBACK <= timedelta(0):
        raise ValueError("LOOKBACK 必须大于 0")

    return password


def _require_ok(status: str, data: Sequence[object], operation: str) -> None:
    if status != "OK":
        detail = data[0] if data else "unknown error"
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        raise FetchError(f"{operation}失败：{detail}")


def _extract_internaldate(fetch_data: Sequence[object]) -> datetime:
    """Extract an aware INTERNALDATE from a FETCH response."""
    metadata_parts: list[bytes] = []

    for item in fetch_data:
        if isinstance(item, bytes):
            metadata_parts.append(item)
        elif isinstance(item, tuple) and item and isinstance(item[0], bytes):
            metadata_parts.append(item[0])

    metadata = b" ".join(metadata_parts)
    match = _INTERNALDATE_RE.search(metadata)
    if match is None:
        raise FetchError("FETCH 响应中缺少 INTERNALDATE")

    try:
        received_at = parsedate_to_datetime(match.group(1).decode("ascii"))
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise FetchError("无法解析邮件的 INTERNALDATE") from exc

    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)
    return received_at


def _extract_raw_message(fetch_data: Sequence[object]) -> bytes:
    """Extract the raw RFC 822 message from a FETCH response."""
    for item in fetch_data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    raise FetchError("FETCH 响应中缺少邮件正文")


def _message_subject(raw_message: bytes) -> str:
    try:
        message = BytesParser(policy=email.policy.default).parsebytes(
            raw_message, headersonly=True
        )
        subject = str(message.get("Subject", "")).strip()
    except (LookupError, UnicodeError, ValueError):
        subject = ""
    return subject or "no-subject"


def _safe_filename_part(value: str, max_length: int = 80) -> str:
    value = _UNSAFE_FILENAME_RE.sub("_", value)
    value = re.sub(r"\s+", " ", value).strip(" ._")
    return (value[:max_length].rstrip(" ._") or "no-subject")


def _destination(uid: bytes, received_at: datetime, raw_message: bytes) -> Path:
    timestamp = received_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    uid_text = uid.decode("ascii", errors="replace")
    subject = _safe_filename_part(_message_subject(raw_message))
    return OUTPUT_DIR.expanduser() / f"{timestamp}_uid-{uid_text}_{subject}.eml"


def _save_message(path: Path, raw_message: bytes) -> bool:
    """Save without overwriting; return False when the message already exists."""
    try:
        with path.open("xb") as output:
            output.write(raw_message)
        path.chmod(0o600)
    except FileExistsError:
        return False
    except BaseException:
        # Do not leave a partial email behind if writing was interrupted.
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return True


def fetch_recent_mails(now: datetime | None = None) -> tuple[int, int]:
    """Download recent messages and return ``(downloaded, already_present)``."""
    password = _validate_config()
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        raise ValueError("now 必须包含时区信息")
    threshold = current_time.astimezone(timezone.utc) - LOOKBACK

    output_dir = OUTPUT_DIR.expanduser()
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    downloaded = 0
    already_present = 0
    connection: imaplib.IMAP4_SSL | None = None

    try:
        context = ssl.create_default_context()
        connection = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=context)
        status, data = connection.login(GMAIL_ADDRESS.strip(), password)
        _require_ok(status, data, "登录")

        status, data = connection.select(MAILBOX, readonly=True)
        _require_ok(status, data, f"打开邮箱 {MAILBOX}")

        # IMAP dates contain no time zone or time component.  Start one calendar
        # day earlier so Gmail's server-side date interpretation cannot omit a
        # message around midnight; the exact UTC window is enforced below.
        since_date = (threshold - timedelta(days=1)).strftime("%d-%b-%Y")
        status, data = connection.uid("search", None, "SINCE", since_date)
        _require_ok(status, data, "搜索邮件")
        uids = data[0].split() if data and isinstance(data[0], bytes) else []

        for uid in uids:
            uid_text = uid.decode("ascii", errors="replace")
            status, metadata = connection.uid("fetch", uid, "(INTERNALDATE)")
            _require_ok(status, metadata, f"读取 UID {uid_text} 的时间")
            received_at = _extract_internaldate(metadata)
            received_utc = received_at.astimezone(timezone.utc)
            if received_utc < threshold or received_utc > current_time:
                continue

            # Fetch the potentially large body only after the exact time check.
            status, message_data = connection.uid("fetch", uid, "(BODY.PEEK[])")
            _require_ok(status, message_data, f"下载 UID {uid_text}")
            raw_message = _extract_raw_message(message_data)
            destination = _destination(uid, received_at, raw_message)
            if _save_message(destination, raw_message):
                downloaded += 1
                print(f"已下载：{destination}")
            else:
                already_present += 1
                print(f"已存在：{destination}")
    finally:
        if connection is not None:
            try:
                connection.logout()
            except (imaplib.IMAP4.error, OSError):
                pass

    return downloaded, already_present


def main() -> int:
    try:
        downloaded, already_present = fetch_recent_mails()
    except (
        ValueError,
        FetchError,
        imaplib.IMAP4.error,
        OSError,
        ssl.SSLError,
    ) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    print(f"完成：下载 {downloaded} 封，已存在 {already_present} 封。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
