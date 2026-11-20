#!/usr/bin/env python3
"""使用同目录 config.json 中的 SMTP 帐号发送邮件。"""

import argparse
import json
import mimetypes
import re
import smtplib
import ssl
import sys
from email import policy
from email.errors import MessageError
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--to", action="append", required=True, help="收件邮箱，可重复")
    parser.add_argument("--subject", required=True, help="邮件标题")
    parser.add_argument("--body", help="纯文本正文，也可用作 HTML 邮件的备用正文")
    parser.add_argument("--html-file", type=Path, help="UTF-8 编码的 HTML 正文文件")
    parser.add_argument("--image", action="append", default=[], metavar="ID=PATH",
                        help="内嵌图片，在 HTML 中用 cid:ID 引用，可重复")
    parser.add_argument("--attach", action="append", type=Path, default=[], metavar="PATH",
                        help="附件路径，可重复")
    return parser.parse_args()


def has_control(value):
    return any(ord(c) < 32 or ord(c) == 127 for c in value)


def validate_mailbox(value, label):
    try:
        addr = Address(addr_spec=value)
    except (ValueError, MessageError, IndexError):
        addr = None
    if (not value.isascii() or has_control(value) or addr is None or
            not addr.username or not addr.domain or addr.addr_spec != value):
        raise ValueError(f"{label} 需要单个完整的 ASCII 邮箱地址")


def load_config():
    path = Path(__file__).resolve().with_name("config.json")
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("config.json 须为 JSON 对象")
    for key in ("host", "security", "username", "password"):
        if not isinstance(config.get(key), str) or not config[key]:
            raise ValueError(f"config.json 中的 {key} 须为非空字符串")
    if has_control(config["host"]) or config["host"].strip() != config["host"]:
        raise ValueError("host 须为 SMTP 主机名")
    port = config.get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port 须为 1 到 65535 之间的整数")
    if config["security"] not in ("ssl", "starttls"):
        raise ValueError("security 须为 ssl 或 starttls")
    validate_mailbox(config["username"], "username")
    return config


def media_type(path):
    if has_control(path.name):
        raise ValueError("图片和附件的文件名不能包含控制字符")
    content_type, encoding = mimetypes.guess_type(path.name)
    if not content_type or encoding:
        return "application", "octet-stream"
    return content_type.split("/", 1)


def build_message(args, sender):
    for recipient in args.to:
        validate_mailbox(recipient, "--to")
    if not args.subject.strip() or has_control(args.subject):
        raise ValueError("--subject 需要非空的单行标题")
    if args.image and not args.html_file:
        raise ValueError("--image 需要同时提供 --html-file")

    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = sender
    msg["To"] = ", ".join(args.to)
    msg["Subject"] = args.subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    if args.body is not None or not args.html_file:
        msg.set_content(args.body or "", cte="quoted-printable")
    if args.html_file:
        html = args.html_file.read_text(encoding="utf-8")
        if args.body is None:
            msg.set_content(html, subtype="html", cte="quoted-printable")
            html_part = msg
        else:
            msg.add_alternative(html, subtype="html", cte="quoted-printable")
            html_part = msg.get_payload()[-1]
        seen = set()
        for spec in args.image:
            cid, _, name = spec.partition("=")
            if not name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", cid):
                raise ValueError("--image 需要 ID=PATH，ID 仅可含 ASCII 字母、数字、_.@- 并以字母或数字开头")
            if cid in seen:
                raise ValueError(f"重复的图片 ID {cid}")
            seen.add(cid)
            path = Path(name)
            main, sub = media_type(path)
            if main != "image":
                raise ValueError(f"无法识别内嵌图片的扩展名 {path}")
            html_part.add_related(path.read_bytes(), maintype=main, subtype=sub,
                                  cid=f"<{cid}>", filename=path.name, disposition="inline")
        if args.image:
            html_part.set_param("type", "text/html")
    for path in args.attach:
        main, sub = media_type(path)
        msg.add_attachment(path.read_bytes(), maintype=main, subtype=sub, filename=path.name)
    return msg


def send(config, msg, recipients):
    context = ssl.create_default_context()
    host, port = config["host"], config["port"]
    if config["security"] == "ssl":
        smtp = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
    else:
        smtp = smtplib.SMTP(host, port, timeout=30)
    try:
        if config["security"] == "starttls":
            smtp.starttls(context=context)
        try:
            smtp.login(config["username"], config["password"])
        except UnicodeEncodeError:
            raise ValueError("SMTP 授权码须使用 ASCII 字符") from None
        refused = smtp.send_message(msg, from_addr=config["username"], to_addrs=recipients)
        # DATA 已获服务器确认。QUIT 时断开连接不会撤回邮件。
        try:
            smtp.quit()
        except OSError:
            pass
    finally:
        smtp.close()
    if refused:
        raise ValueError("服务器已接受其余收件人，以下地址被拒收：" + ", ".join(refused))


def main():
    args = parse_args()
    try:
        config = load_config()
        msg = build_message(args, config["username"])
        send(config, msg, args.to)
    except smtplib.SMTPRecipientsRefused as exc:
        error = "服务器拒收以下地址：" + ", ".join(exc.recipients)
    except smtplib.SMTPAuthenticationError as exc:
        error = f"SMTP 认证失败（{exc.smtp_code}），请检查帐号及授权码"
    except smtplib.SMTPResponseException as exc:
        error = f"SMTP 服务器返回错误 {exc.smtp_code}"
    except (OSError, ValueError, MessageError) as exc:
        error = str(exc)
    else:
        print("SMTP 服务器已接受邮件")
        return 0
    print(f"发送失败：{error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
