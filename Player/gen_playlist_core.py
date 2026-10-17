#!/usr/bin/env python3
"""将 WebDAV 文件/文件夹 URL 转为带标题的 UTF-8 M3U8 播放列表。

仅支持 WebDAV HTTP(S) URL，文件夹不递归；Python 3.10+，仅使用标准库。
文件/文件夹由 PROPFIND 返回的 resourcetype/collection 属性自动识别，无需额外标记。
包装脚本先调用环境激活函数，再使用 PYTHON_BIN 的最终值启动本脚本：
填写解释器绝对路径时固定使用该解释器；填写 python 时按激活后的 PATH 查找。
用法：python gen_playlist_core.py --output-dir ~/Downloads --name playlist URL [URL ...]
账号密码可写入 URL，特殊字符需 URL 编码，例如 https://user:p%40ss@host/dav/。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from http.client import HTTPException
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import (
    HTTPBasicAuthHandler, HTTPDigestAuthHandler, HTTPPasswordMgrWithDefaultRealm,
    HTTPRedirectHandler, Request, build_opener,
)
import xml.etree.ElementTree as ET


VERSION = "0.1.0"

MEDIA_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".wmv", ".flv",
    ".ts", ".m2ts", ".mpg", ".mpeg", ".m3u8",
    ".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus", ".wma",
}
DAV = "{DAV:}"
PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop><d:displayname/>'
    '<d:resourcetype/></d:prop></d:propfind>'
).encode("utf-8")


class PlaylistError(Exception):
    """可以直接向用户展示的错误。"""


@dataclass(frozen=True)
class Item:
    url: str
    title: str


@dataclass(frozen=True)
class Resource:
    url: str
    path: str
    collection: bool
    display_name: str


def http_url(value: str) -> str:
    if re.search(r"[\x00-\x1f\x7f]", value):
        raise PlaylistError("URL 中不能包含换行或控制字符。")
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError
        parsed.port  # 同时检查端口是否合法。
        parsed.hostname.encode("ascii")
    except (ValueError, UnicodeError) as error:
        raise PlaylistError("请提供完整的 WebDAV HTTP(S) URL；域名需使用 ASCII/Punycode。") from error
    # 保留已有百分号编码和查询参数；也接受路径中的中文、空格。
    return quote(value, safe=":/?#[]@!$&'()*+,;=%")


def origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname.lower(), port


def restore_credentials(url: str, source: str) -> str:
    parsed, base = urlsplit(url), urlsplit(source)
    if parsed.username is None and base.username is not None and origin(url) == origin(source):
        parsed = parsed._replace(netloc=base.netloc.rsplit("@", 1)[0] + "@" + parsed.netloc)
    return urlunsplit(parsed)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 自行处理，确保重定向后仍使用 PROPFIND，并按来源隔离认证信息。
        return None


def read_webdav(source: str, timeout: float) -> tuple[ET.Element, str]:
    current = source
    for attempt in range(6):
        parsed = urlsplit(current)
        clean = urlunsplit(parsed._replace(netloc=parsed.netloc.rsplit("@", 1)[-1], fragment=""))
        passwords = HTTPPasswordMgrWithDefaultRealm()
        if parsed.username is not None:
            scope = urlunsplit(urlsplit(clean)._replace(path="/", query=""))
            passwords.add_password(None, scope, unquote(parsed.username), unquote(parsed.password or ""))
        opener = build_opener(NoRedirect(), HTTPDigestAuthHandler(passwords), HTTPBasicAuthHandler(passwords))
        request = Request(clean, data=PROPFIND_BODY, method="PROPFIND", headers={
            "Depth": "1", "Content-Type": "application/xml; charset=utf-8",
        })
        try:
            response = opener.open(request, timeout=timeout)
        except HTTPError as error:
            location = error.headers.get("Location")
            code = error.code
            error.close()
            if code in (301, 302, 303, 307, 308) and location:
                if attempt == 5:
                    raise PlaylistError("WebDAV 重定向次数过多。") from error
                current = restore_credentials(http_url(urljoin(clean, location)), current)
                continue
            raise PlaylistError(f"WebDAV 请求失败：HTTP {code}。") from error
        with response:
            if response.status != 207:
                raise PlaylistError(f"需要 WebDAV 207 Multi-Status 响应，实际为 HTTP {response.status}。")
            document = ET.fromstring(response.read())
        if document.tag != DAV + "multistatus":
            raise PlaylistError("响应不是 WebDAV multistatus XML。")
        return document, current
    raise PlaylistError("WebDAV 重定向失败。")


def display_title(url: str, display_name: str) -> str:
    title = display_name.strip() or unquote(urlsplit(url).path.rsplit("/", 1)[-1]).strip()
    suffix = "." + title.rsplit(".", 1)[-1]
    if "." in title and suffix.lower() in MEDIA_EXTENSIONS:
        title = title[:-len(suffix)]
    return re.sub(r"[\x00-\x1f\x7f]", " ", title).strip() or "未命名文件"


def collect_source(source: str, timeout: float) -> list[Item]:
    source = http_url(source)
    document, base = read_webdav(source, timeout)
    records = []
    for node in document.findall(DAV + "response"):
        href = node.findtext(DAV + "href", "").strip()
        if not href:
            continue
        try:
            item_url = http_url(urljoin(base, href))
        except PlaylistError:
            continue
        if origin(item_url) != origin(base):
            continue
        item_url = restore_credentials(item_url, base)
        props = []
        for propstat in node.findall(DAV + "propstat"):
            status = propstat.findtext(DAV + "status", "").strip()
            prop = propstat.find(DAV + "prop")
            if prop is not None and re.match(r"^\S+\s+2\d\d(?:\s|$)", status):
                props.append(prop)
        if not props:
            continue
        collection = any(prop.find(DAV + "resourcetype/" + DAV + "collection") is not None for prop in props)
        names = ["".join(name.itertext()) for prop in props for name in prop.findall(DAV + "displayname")]
        name = next((name for name in names if name.strip()), "")
        records.append(Resource(item_url, unquote(urlsplit(item_url).path), collection, name))

    base_path = unquote(urlsplit(base).path).rstrip("/")
    root = next((record for record in records if record.path.rstrip("/") == base_path), None)
    if root is None:
        raise PlaylistError("WebDAV 响应缺少目标资源的可用属性，请检查 URL 和访问权限。")
    if not root.collection:
        # 显式指定的文件保留原始查询参数及百分号编码。
        return [Item(source, display_title(source, root.display_name))]
    prefix = base_path + "/"
    items = []
    for record in records:
        if record.collection or not record.path.startswith(prefix):
            continue
        relative = record.path[len(prefix):]
        if not relative or "/" in relative or "." not in relative or "." + relative.rsplit(".", 1)[-1].lower() not in MEDIA_EXTENSIONS:
            continue
        items.append(Item(record.url, display_title(record.url, record.display_name)))
    return items


def output_filename(value: str) -> str:
    name = value.strip()
    if not name or name in (".", "..") or re.search(r'[\\/:*?"<>|\x00-\x1f\x7f]', name):
        raise argparse.ArgumentTypeError("--name 请只填写文件名，不要包含目录或特殊路径字符。")
    return name if name.lower().endswith(".m3u8") else name + ".m3u8"


def positive_timeout(value: str) -> float:
    number = float(value)
    if not 0 < number < float("inf"):
        raise argparse.ArgumentTypeError("--timeout 必须是大于 0 的有限秒数。")
    return number


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("urls", nargs="+", metavar="URL", help="一个或多个 WebDAV 文件/文件夹 URL；不递归")
    parser.add_argument("--output-dir", type=Path, default=Path.home() / "Downloads", help="输出目录（默认：~/Downloads）")
    parser.add_argument("--name", type=output_filename, default="playlist.m3u8", help="输出文件名，允许省略 .m3u8（默认：playlist.m3u8）")
    parser.add_argument("--timeout", type=positive_timeout, default=30, help="每次网络操作的超时秒数（默认：30）")
    return parser


def write_playlist(items: list[Item], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=output.parent,
                                         prefix=".playlist-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write("#EXTM3U\n")
            for item in items:
                stream.write(f"#EXTINF:-1,{item.title}\n{item.url}\n")
        os.replace(temporary, output)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    print(f"gen_playlist_core.py 版本：{VERSION}", flush=True)
    args = argument_parser().parse_args(argv)
    try:
        items, seen = [], set()
        for index, source in enumerate(args.urls, 1):
            try:
                incoming = collect_source(source.strip(), args.timeout)
            except (PlaylistError, OSError, URLError, HTTPException, ValueError, ET.ParseError) as error:
                raise PlaylistError(f"第 {index} 个 URL 处理失败：{error}") from error
            for item in incoming:
                if item.url not in seen:
                    seen.add(item.url)
                    items.append(item)
        if not items:
            raise PlaylistError("没有找到可播放的文件；未修改已有播放列表。")
        output = args.output_dir.expanduser().resolve() / args.name
        write_playlist(items, output)
    except (PlaylistError, OSError) as error:
        print(f"生成失败：{error}", file=sys.stderr)
        return 1
    print(f"已生成 {len(items)} 个条目：{output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
