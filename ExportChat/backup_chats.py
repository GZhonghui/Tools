#!/usr/bin/env python3
"""备份用户发送的消息和助手面向用户的回复，每个会话导出一个 Markdown 文件。

python scripts/backup_chats.py --dry-run          # 预览范围和体积，不写文件
python scripts/backup_chats.py                    # 写入本仓库 chat/<当前用户>/{codex,cc}/
python scripts/backup_chats.py --user cone-server # 多台机器使用不同目录标签

启动时输出脚本名和版本号。
备份范围：扫描所配置的 Codex / Claude Code 本地会话目录，覆盖所有项目，
不会只备份当前项目，也不按启动目录或会话所属项目路径筛选。
默认读取当前系统账户的会话；源目录可通过环境变量或 --codex-home / --claude-home 修改，
--only 可限定其中一种工具。仅处理本地仍存在的记录，并按下述规则排除子代理等内容。

默认备份路径：__file__ 所在目录/../chat/<当前用户名或 --user 标签>/{codex,cc}/。
这里 __file__ 是本脚本文件，所在目录为 scripts/，因此 ../chat/ 就是仓库根目录的 chat/。
默认位置不受启动目录影响；显式传入 --output 时可覆盖备份根目录，
其中相对路径按启动时的工作目录解析，绝对路径按指定位置使用。

仅使用 Python 标准库。不调用模型、不执行 Git、不修改源记录。
排除工具调用/输出、思考过程、系统提示、注入的环境说明、子代理对话及附件数据。
保留助手面向用户的进度消息和最终回复。兼容 Codex 新旧消息事件并去除镜像重复；
不会因为两次真的发送了相同文字而删掉其中一次。源记录删除后仍保留已有备份。
这是可阅读的对话归档，不能用于恢复 CLI 状态，正文中的密钥不会自动脱敏。

记录位置与稳定性（2026-10-25 核实；本机 Codex CLI 0.164.0、Claude Code 2.1.280）：
- Codex 的官方默认根目录为 ~/.codex，可通过 CODEX_HOME 改址，本脚本也支持
  --codex-home。本机完整会话在 sessions/YYYY/MM/DD/rollout-*.jsonl，日期对应
  会话创建日期；正常 resume 会追加原会话，不会因当天日期变化而另建文件。
  归档/取消归档会在 sessions 与 archived_sessions 之间移动源日志。
- Claude Code 的官方默认路径为 ~/.claude/projects/<project>/<session-id>.jsonl，
  同一会话正常续聊时继续写入原文件。<project> 通常由工作目录绝对路径编码得到，
  长路径还会截断并附哈希；改项目位置或使用其他 worktree 会影响项目子目录。
  CLAUDE_CONFIG_DIR 可改变根目录（本脚本支持 --claude-home）；同时设置
  CLAUDE_CODE_PROJECT_DIR_NAME 可指定项目目录名，本脚本扫描 projects/ 下所有项目。
- 这些是可依赖的当前默认约定，不是永不改变的存储接口。尤其 CC 官方明确说明
  JSONL 内部格式会随版本改变；Codex 的具体子目录/文件名在此按本机版本核对。
  每次运行应使用相同的 Linux 用户、--user 标签和配置根目录；多台机器使用不同标签。
  未持久化的会话没有可备份的本地文件（如 Codex --ephemeral、CC --no-session-persistence）。
- 路径固定不代表日志永久保留：CC CLI 默认按 cleanupPeriodDays=30 清理旧会话，
  应在清理之前定期备份；已经导出的 Markdown 不会因源日志消失而被本脚本删除。

重复运行时的 Git 差异：
- 默认输出在仓库根目录的 chat/（scripts/ 的上一级），与运行时的当前目录无关；导出文件不写
  “本次备份时间”，内容相同时不改写。因此没有新消息时，不会制造额外差异。
- 在源日志正常追加且解析格式/配置不变时，新会话新增一个 .md；旧会话的新消息
  追加到同一个 .md 的末尾。因此通常看到新增文件，或已有文件新增若干行。
- 本脚本每次从源日志重新生成快照，并非严格 append-only。正在生成的消息被补全、
  镜像事件补齐、源记录重写/截断、元数据修订或解析规则升级，都可能修改已有行。
  源路径改址/归档移动可能导出到新路径；旧副本保留，可能出现重复会话文件。
  分支/fork 会产生新会话，可能包含继承的旧消息。不能保证 diff 永远只有 + 行。
- 先提交一次备份作为 Git 基线。git diff -- chat/ 只显示已跟踪文件的未暂存修改；
  新增且未跟踪的文件请用 git status --short -- chat/ 查看；暂存后看 git diff --cached。
  脚本不自动 git add/commit/push，也不会操作其他已暂存的改动。

核对来源（官方约定与本机观察分开记录）：
https://learn.chatgpt.com/docs/config-file/environment-variables#core-locations
https://learn.chatgpt.com/docs/app-server#api-overview
https://code.claude.com/docs/en/sessions#where-transcripts-are-stored
https://code.claude.com/docs/en/claude-directory#cleaned-up-automatically
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile


SCRIPT_VERSION = "v0.1.0"

DATA_URI = re.compile(r"data:[\w.+-]+/[\w.+-]+(?:;[^,\s\"'<>]*)?,[a-z0-9+/=_%~-]+", re.I)
LONG_BASE64 = re.compile(r"(?<![\w+/=-])[a-zA-Z0-9+/_-]{1024,}={0,2}(?![\w+/=-])")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
INJECTED_PREFIXES = ("# AGENTS.md instructions for ", "<environment_context>", "<skill>", "<subagent_notification>")
TEXT_TYPES = {"text", "input_text", "output_text"}
ATTACHMENT_TYPES = {"image", "input_image", "local_image", "audio", "input_audio", "local_audio", "document", "file"}


def clean_text(text: str):
    text = DATA_URI.sub("[附件数据已省略]", text)
    text = LONG_BASE64.sub("[编码数据已省略]", text)
    return CONTROL.sub(lambda m: f"\\x{ord(m[0]):02x}", text).strip()


def content_text(content, *, filter_injected=False):
    if isinstance(content, str):
        return clean_text(content)
    parts = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type", "")).lower()
        if kind in TEXT_TYPES and isinstance(block.get("text"), str):
            text = block["text"]
            if filter_injected and text.lstrip().startswith(INJECTED_PREFIXES):
                continue
            parts.append(clean_text(text))
        elif kind in ATTACHMENT_TYPES:
            parts.append("[附件已省略]")
        elif kind == "skill" and isinstance(block.get("name"), str):
            parts.append("$" + clean_text(block["name"]))
    return "\n\n".join(part for part in parts if part)


def walk_files(root: Path):
    def fail(error):
        raise error

    if not root.is_dir() or root.is_symlink():
        return
    for parent, dirs, names in os.walk(root, followlinks=False, onerror=fail):
        dirs[:] = sorted(d for d in dirs if d != "life" and not (Path(parent) / d).is_symlink())
        for name in sorted(names):
            path = Path(parent) / name
            if path.is_file() and not path.is_symlink():
                yield path


def candidates(root: Path, tool: str):
    for subdir in ("sessions", "archived_sessions") if tool == "codex" else ("projects",):
        for path in walk_files(root / subdir):
            if path.suffix != ".jsonl":
                continue
            # CC 子代理可位于 subagents/，旧版也可能使用顶层 agent-*.jsonl。
            if tool == "cc" and (len(path.relative_to(root / subdir).parts) != 2 or path.name.startswith("agent-")):
                continue
            yield path


@dataclass
class Message:
    role: str
    text: str
    timestamp: str
    order: int
    source: str
    identity: str = ""
    turn: str = "initial"


@dataclass
class Transcript:
    session_id: str
    source_bytes: int
    project: str = ""
    messages: list[Message] = field(default_factory=list)
    bad_lines: int = 0
    normalized_lines: int = 0
    partial_tail: bool = False
    skipped_reason: str = ""


def merge_messages(messages: list[Message]):
    """按轮次、身份和每种来源中的出现次数合并镜像，保留真实的重复发言。"""
    turns = defaultdict(list)
    for message in messages:
        turns[message.turn].append(message)
    result = []
    for turn_messages in turns.values():
        for role in ("user", "assistant"):
            streams = defaultdict(list)
            for message in turn_messages:
                if message.role == role:
                    streams[message.source].append(message)
            if role == "user" and (streams["display"] or streams["legacy"]):
                # 界面事件准确表示用户输入；底层 user 消息还可能包含环境和提示词。
                streams.pop("response", None)
            priority = ("display", "legacy", "response") if role == "user" else ("response", "display", "legacy")
            chosen, identities = {}, set()
            for source in priority:
                # 同一 ID 再次写入是重放或修订；无 ID 的相同文字仍按次数保留。
                unique, by_id = [], {}
                for message in streams[source]:
                    if message.identity and message.identity in by_id:
                        unique[by_id[message.identity]] = message
                    else:
                        if message.identity:
                            by_id[message.identity] = len(unique)
                        unique.append(message)
                occurrences = Counter()
                for message in unique:
                    occurrences[message.text] += 1
                    key = (message.text, occurrences[message.text])
                    if message.identity and message.identity in identities:
                        continue
                    if key not in chosen:
                        chosen[key] = message
                    if message.identity:
                        identities.add(message.identity)
            result.extend(chosen.values())
    return sorted(result, key=lambda message: message.order)


def codex_message(record, order, turn):
    payload = record.get("payload", {})
    if not isinstance(payload, dict):
        return None
    typ, subtype = record.get("type"), payload.get("type")
    role, source, body = None, None, payload
    if typ == "response_item" and subtype == "message":
        role, source = payload.get("role"), "response"
        attribution = payload.get("internal_chat_message_metadata_passthrough")
        if isinstance(attribution, dict):
            turn = str(attribution.get("turn_id") or turn)
        if payload.get("recipient") not in (None, "all", "user") or payload.get("channel") == "analysis":
            return None
    elif typ == "event_msg" and subtype == "item_completed":
        body = payload.get("item", {})
        if not isinstance(body, dict):
            return None
        role = {"UserMessage": "user", "AgentMessage": "assistant"}.get(body.get("type"))
        source = "display"
        turn = payload.get("turn_id") or turn
    elif typ == "event_msg" and subtype in ("user_message", "agent_message"):
        role = "user" if subtype == "user_message" else "assistant"
        source = "legacy"
    if role not in ("user", "assistant"):
        return None
    if body.get("phase") not in (None, "commentary", "final_answer"):
        return None
    text = content_text(body.get("message", body.get("content", [])), filter_injected=role == "user" and source == "response")
    if not text:
        return None
    return Message(role, text, str(record.get("timestamp") or ""), order, source, str(body.get("id") or ""), turn)


def claude_message(record, order):
    if record.get("isSidechain") or record.get("isMeta") or record.get("isCompactSummary"):
        return None
    role = record.get("type")
    body = record.get("message", {})
    if role not in ("user", "assistant") or not isinstance(body, dict):
        return None
    origin = record.get("origin")
    if role == "user" and isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return None
    text = content_text(body.get("content", []))
    if not text:
        return None
    identity = record.get("uuid") or (f"{body['id']}:{record.get('apiBlockIndex')}" if body.get("id") else "")
    return Message(role, text, str(record.get("timestamp") or ""), order, "response", str(identity))


def read_transcript(source: Path, tool: str):
    with source.open("rb") as stream:
        transcript = Transcript(source.stem, os.fstat(stream.fileno()).st_size)
        remaining, order, turn, got_meta = transcript.source_bytes, 0, "initial", False
        while remaining:
            raw = stream.readline(remaining)
            if not raw:
                raise ValueError("读取期间源文件缩短，请重试")
            remaining -= len(raw)
            order += 1
            try:
                text = raw.decode("utf-8")
            except UnicodeError as error:
                if not remaining and not raw.endswith(b"\n"):
                    transcript.partial_tail = True
                    break
                raise ValueError(f"第 {order} 行不是 UTF-8 文本") from error
            if not text.strip():
                continue
            try:
                record = json.loads(text)
            except json.JSONDecodeError:
                try:
                    record = json.loads(text, strict=False)
                    transcript.normalized_lines += 1
                except json.JSONDecodeError:
                    if not remaining and not raw.endswith(b"\n"):
                        transcript.partial_tail = True
                        break
                    transcript.bad_lines += 1
                    continue
            if not isinstance(record, dict):
                continue
            if tool == "codex":
                payload = record.get("payload", {})
                if not isinstance(payload, dict):
                    continue
                if record.get("type") == "session_meta" and not got_meta:
                    # 子代理文件先写自己的元数据，再复制父会话元数据；必须取第一份。
                    got_meta = True
                    origin = payload.get("source")
                    if isinstance(origin, dict) and "subagent" in origin:
                        transcript.skipped_reason = "子代理会话"
                        return transcript
                    transcript.session_id = str(payload.get("id") or payload.get("session_id") or source.stem)
                    transcript.project = str(payload.get("cwd") or "")
                if record.get("type") == "turn_context" or (record.get("type") == "event_msg" and payload.get("type") in ("task_started", "item_completed")):
                    next_turn = str(payload.get("turn_id") or turn)
                    if turn == "initial" and payload.get("type") == "item_completed":
                        for previous in transcript.messages:
                            if previous.turn == "initial":
                                previous.turn = next_turn
                    turn = next_turn
                message = codex_message(record, order, turn)
            else:
                transcript.session_id = str(record.get("sessionId") or transcript.session_id)
                transcript.project = str(record.get("cwd") or transcript.project)
                message = claude_message(record, order)
            if message:
                transcript.messages.append(message)
        transcript.messages = merge_messages(transcript.messages)
        if not transcript.messages:
            transcript.skipped_reason = "没有用户可见消息"
        return transcript


def render_markdown(transcript: Transcript, tool: str):
    name = "Codex" if tool == "codex" else "Claude Code"
    lines = [f"# {name} 对话", "", f"会话：`{clean_text(transcript.session_id)}`", ""]
    if transcript.project:
        lines.extend([f"项目：`{clean_text(transcript.project)}`", ""])
    for message in transcript.messages:
        label = "用户" if message.role == "user" else "助手"
        heading = f"## {label}" + (f" · {message.timestamp}" if message.timestamp else "")
        lines.extend([heading, "", message.text, ""])
    return "\n".join(lines).encode("utf-8")


def check_destination(path: Path):
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("备份目标路径不能包含符号链接")


class TooLarge(ValueError):
    pass


@dataclass
class Export:
    changed: bool
    output_bytes: int
    transcript: Transcript


def export_file(source: Path, target: Path, *, tool="codex", dry_run=False, max_bytes=50 * 1024**2):
    check_destination(target)
    transcript = read_transcript(source, tool)
    if transcript.skipped_reason:
        return Export(False, 0, transcript)
    data = render_markdown(transcript, tool)
    if len(data) > max_bytes:
        raise TooLarge(f"导出后超过 {max_bytes / 1024**2:g} MiB，保留原有副本")
    changed = not target.is_file() or hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(data).digest()
    if changed and not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(prefix=".chat-backup-", dir=target.parent, delete=False) as stream:
                temp_path = Path(stream.name)
                stream.write(data)
            os.replace(temp_path, target)
            temp_path = None
        finally:
            if temp_path:
                temp_path.unlink(missing_ok=True)
    return Export(changed, len(data), transcript)


def main(argv=None):
    print(f"{Path(__file__).name} {SCRIPT_VERSION}", flush=True)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", default=getpass.getuser(), help="备份目录标签；多台机器建议使用 用户名-主机名")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "chat", help="备份根目录，默认本仓库的 chat/")
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser())
    parser.add_argument("--claude-home", type=Path, default=Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser())
    parser.add_argument("--only", choices=("codex", "cc"))
    parser.add_argument("--dry-run", action="store_true", help="解析、提取并统计，但不创建文件")
    parser.add_argument("--max-file-mib", type=int, default=50, help="单个 Markdown 文件上限，默认 50 MiB")
    parser.add_argument("--verbose", action="store_true", help="显示相对路径和消息数量，不显示聊天正文")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", args.user):
        parser.error("--user 只能包含字母、数字、下划线、点、连字符，且以字母、数字或下划线开头")
    if args.max_file_mib < 1:
        parser.error("--max-file-mib 必须大于 0")
    output = args.output.expanduser().absolute() / args.user
    roots = {"codex": args.codex_home.expanduser().absolute(), "cc": args.claude_home.expanduser().absolute()}
    try:
        check_destination(output)
    except ValueError as error:
        parser.error(str(error))
    for root in roots.values():
        if root.is_symlink() or "life" in root.resolve().parts or "life" in output.resolve().parts:
            parser.error("不访问符号链接源目录或 life 目录")
        if output.resolve().is_relative_to(root.resolve()):
            parser.error("备份目录不能放在源目录中")
    print(f"{'预览' if args.dry_run else '备份'}目标：{output}", flush=True)
    errors = 0
    for tool, root in roots.items():
        if args.only and tool != args.only:
            continue
        files = changed = saved_size = messages = skipped = 0
        if not root.is_dir():
            print(f"{tool}: 源目录不存在，跳过：{root}")
            continue
        try:
            for source in candidates(root, tool):
                relative = source.relative_to(root)
                try:
                    result = export_file(source, (output / tool / relative).with_suffix(".md"), tool=tool,
                                         dry_run=args.dry_run, max_bytes=args.max_file_mib * 1024**2)
                    transcript = result.transcript
                    if transcript.bad_lines:
                        print(f"  提示：{tool}/{relative} 跳过 {transcript.bad_lines} 行损坏 JSON")
                    if transcript.partial_tail:
                        print(f"  提示：{tool}/{relative} 末行尚未写完，下次运行会重试")
                    if transcript.skipped_reason:
                        skipped += 1
                        if args.verbose:
                            print(f"  跳过 {tool}/{relative}：{transcript.skipped_reason}")
                        continue
                    files += 1
                    changed += result.changed
                    saved_size += result.output_bytes
                    messages += len(transcript.messages)
                    if args.verbose:
                        print(f"  {'更新' if result.changed else '相同'} {tool}/{relative.with_suffix('.md')}：{len(transcript.messages)} 条消息")
                except (OSError, ValueError, RecursionError) as error:
                    errors += 1
                    print(f"  错误：{tool}/{relative}: {error}", file=sys.stderr)
        except OSError as error:
            errors += 1
            print(f"  错误：遍历 {root}: {error}", file=sys.stderr)
        print(f"{tool}: {files} 个会话，{messages} 条消息，{saved_size / 1024**2:.2f} MiB，"
              f"{changed} 个{'待更新' if args.dry_run else '已更新'}，跳过 {skipped} 个子代理或空会话", flush=True)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
