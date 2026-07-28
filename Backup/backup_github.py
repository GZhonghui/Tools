#!/usr/bin/env python3

# ========== backup_github v0.1 2026/07/24 ==========
# 本脚本用于把指定 GitHub 个人账号拥有的全部仓库同步为本地 Git mirror。
# 首次运行时使用 SSH 执行 `git clone --mirror`；以后运行时执行
# `git remote update --prune`，让本地分支、标签和其他 Git refs 与远端保持一致。
#
# 这个脚本只备份 Git 数据，不备份 Git LFS、Wiki、Issues、Pull Requests、
# Release 附件或 GitHub 仓库设置，也不会保留多次运行的历史快照。远端删除的
# refs 会在下一次同步时从本地 mirror 中删除，但远端已删除、转让或因权限变化而
# 不再返回的整个仓库不会被自动删除，只会在报告中标记出来。
#
# 使用前：
#   1. 检查并修改下方“用户配置”中的 GitHub 用户名和备份目录。
#      `~` 会展开为当前运行用户的主目录，因此不要使用 sudo 运行本脚本。
#   2. 创建 Fine-grained GitHub Token：Repository access 必须选择 All repositories，
#      Repository permissions 只需 Metadata: read。然后设置环境变量：
#        export GITHUB_BACKUP_TOKEN='你的 Token'
#   3. 确保该账号的 SSH Key 已加入 ssh-agent，并已连接过 github.com。
#   4. 运行：python3 backup_github.py
#
# Token 只用于调用 GitHub API 获取仓库列表；仓库内容始终通过 SSH 下载。
# Token 选择 All repositories 后，以后新建的个人仓库会在下次运行时自动被发现。
# 每次运行都会在备份目录的 _reports 子目录中生成 JSON 报告，并在终端打印摘要。
# 所有外部命令都会在实际执行前以 `$ ...` 的形式完整打印出来，便于人工检查。
# 创建、重命名、原子写入和清理临时目录等 Python 文件操作也会单独打印。
#
# 备份目录中会产生以下内容：
#   <仓库名>.git/       仓库的 bare mirror，是实际备份数据。
#   manifest.json       脚本自动维护的状态文件，以稳定的 GitHub 仓库 ID 记录名称、
#                       本地路径和同步状态，用于识别仓库改名、名称复用和远端消失。
#                       第一次全量备份时可以不存在，脚本会自动创建；它不包含
#                       Token、SSH 私钥或仓库内容，后续增量更新必须保留。
#   _reports/*.json     每次运行的详细报告。脚本以后不会读取这些文件，可以随时删除。
#   .backup.lock        防止两个脚本实例同时修改备份目录的锁文件。文件可以一直保留，
#                       真正的锁只在脚本运行期间有效，也不包含敏感信息。
#
# 如果想手动进行一次全量重新克隆，可以保留 manifest.json，但必须完整删除或移走
# 对应的 `<仓库名>.git` 目录。只清空目录内容却留下空目录时，脚本会把它视为异常
# 路径并报告冲突，不会擅自覆盖。manifest 中记录的 mirror 路径不存在不会报错，
# 脚本会重新执行 `git clone --mirror` 并在成功后刷新记录。
#
# 脚本只接受两种安全状态，并会把模式写入终端摘要和 JSON 报告：
#   full         没有 manifest.json，且备份目录除 .backup.lock 外完全为空；执行全量克隆。
#   incremental  manifest.json 存在；按稳定仓库 ID 执行增量同步或补回缺失的 mirror。
# 如果 manifest.json 不存在但目录中已有任何其他文件、目录或符号链接，脚本会立即
# 终止，不会猜测这些内容属于哪个仓库。损坏或属于其他用户的 manifest 也会终止。

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


# ---------------------------------------------------------------------------
# 用户配置：运行前请检查这些值
# ---------------------------------------------------------------------------

GITHUB_USERNAME = "GZhonghui"
BACKUP_ROOT = Path("~/backups/github")
SLEEP_SECONDS = 1

# Token 不应硬编码进脚本或提交到 Git 仓库。
TOKEN_ENV_VAR = "GITHUB_BACKUP_TOKEN"


# ---------------------------------------------------------------------------
# 一般不需要修改的配置
# ---------------------------------------------------------------------------

GITHUB_API_ROOT = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
API_TIMEOUT_SECONDS = 30
API_RETRY_COUNT = 3
SSH_TIMEOUT_SECONDS = 20
MANIFEST_FILENAME = "manifest.json"
REPORT_DIRECTORY_NAME = "_reports"
LOCK_FILENAME = ".backup.lock"
REPORT_ERROR_LIMIT = 4000
BACKUP_MODE_FULL = "full"
BACKUP_MODE_INCREMENTAL = "incremental"


class BackupError(RuntimeError):
    """无法继续整个备份任务的错误。"""


class RepositoryConflict(RuntimeError):
    """为避免覆盖本地数据而跳过仓库的错误。"""


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return "\n".join(part for part in (self.stdout, self.stderr) if part).strip()


class BackupLock:
    """防止两个脚本实例同时修改同一个备份目录。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> "BackupLock":
        if self.path.is_symlink():
            raise BackupError(f"锁文件不能是符号链接：{self.path}")

        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            file_descriptor = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise BackupError(f"无法安全打开锁文件 {self.path}：{exc}") from exc

        try:
            lock_stat = os.fstat(file_descriptor)
            if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
                raise BackupError(f"锁文件必须是未被硬链接的普通文件：{self.path}")
            self.handle = os.fdopen(file_descriptor, "r+", encoding="utf-8")
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            if self.handle is not None:
                self.handle.close()
            raise BackupError(
                f"另一个备份进程正在使用目录：{self.path.parent}"
            ) from exc
        except BaseException:
            if self.handle is not None:
                self.handle.close()
            else:
                os.close(file_descriptor)
            raise

        try:
            self.handle.seek(0)
            self.handle.truncate()
            self.handle.write(f"pid={os.getpid()}\n")
            self.handle.flush()
            log_file_action(f"已获取运行锁：{self.path}")
        except BaseException:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_time(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def truncate_error(message: str) -> str:
    message = message.strip()
    if len(message) <= REPORT_ERROR_LIMIT:
        return message
    return "..." + message[-REPORT_ERROR_LIMIT:]


def log_file_action(message: str) -> None:
    print(f"[文件操作] {message}", flush=True)


def path_entry_exists(path: Path) -> bool:
    """目录项存在即返回 True，包括指向不存在目标的符号链接。"""
    return os.path.lexists(path)


def ensure_real_directory(path: Path, *, parents: bool = False) -> None:
    if path_entry_exists(path):
        if path.is_symlink() or not path.is_dir():
            raise BackupError(f"路径必须是普通目录且不能是符号链接：{path}")
        return

    log_file_action(f"创建目录：{path}")
    path.mkdir(mode=0o700, parents=parents, exist_ok=False)


def human_size(byte_count: int, *, signed: bool = False) -> str:
    sign = ""
    value = float(byte_count)
    if signed:
        sign = "+" if byte_count > 0 else "-" if byte_count < 0 else ""
        value = abs(value)

    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024

    if unit == "B":
        rendered = f"{int(value)} {unit}"
    else:
        rendered = f"{value:.2f} {unit}"
    return sign + rendered


def run_command(
    arguments: list[str], *, timeout: int | None = None
) -> CommandResult:
    # subprocess.run 接收参数列表且不启用 shell；shlex.join 仅用于忠实地显示
    # 参数边界，即使参数中出现分号、管道符等内容也不会执行额外命令。
    print(f"$ {shlex.join(arguments)}", flush=True)

    environment = os.environ.copy()
    # Token 只供当前 Python 进程调用 API，不传给 git、ssh 或其他子进程。
    environment.pop(TOKEN_ENV_VAR, None)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            timeout=timeout,
            env=environment,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BackupError(
            f"命令执行超时（{timeout} 秒）：{' '.join(arguments)}"
        ) from exc
    except OSError as exc:
        raise BackupError(f"无法执行命令 {' '.join(arguments)}：{exc}") from exc

    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout.strip(),
        stderr=completed.stderr.strip(),
    )


def require_executable(name: str) -> None:
    if shutil.which(name) is None:
        raise BackupError(f"找不到必需命令：{name}")


def validate_configuration() -> tuple[Path, str]:
    username = GITHUB_USERNAME.strip()
    if not username or username in {"YOUR_GITHUB_USERNAME", "你的用户名"}:
        raise BackupError("请先在脚本顶部设置 GITHUB_USERNAME")

    expanded_root = BACKUP_ROOT.expanduser()
    if not expanded_root.is_absolute():
        raise BackupError("BACKUP_ROOT 必须是绝对路径")
    if expanded_root.is_symlink():
        raise BackupError("BACKUP_ROOT 不能是符号链接")
    backup_root = expanded_root.resolve(strict=False)
    if backup_root == Path("/"):
        raise BackupError("BACKUP_ROOT 不能是文件系统根目录 /")

    token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if not token:
        raise BackupError(
            f"环境变量 {TOKEN_ENV_VAR} 未设置；Token 不应写入脚本"
        )

    require_executable("git")
    require_executable("ssh")
    return backup_root, token


def api_get(url: str, token: str) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "github-mirror-backup-script",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }

    last_error = "未知 API 错误"
    for attempt in range(API_RETRY_COUNT):
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=API_TIMEOUT_SECONDS) as response:
                body = response.read().decode("utf-8")
            return json.loads(body)
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last_error = f"GitHub API 返回 HTTP {exc.code}：{truncate_error(body)}"
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt == API_RETRY_COUNT - 1:
                raise BackupError(last_error) from exc
        except (URLError, TimeoutError) as exc:
            last_error = f"连接 GitHub API 失败：{exc}"
            if attempt == API_RETRY_COUNT - 1:
                raise BackupError(last_error) from exc
        except json.JSONDecodeError as exc:
            raise BackupError("GitHub API 返回了无法解析的 JSON") from exc

        time.sleep(2**attempt)

    raise BackupError(last_error)


def verify_api_user(token: str) -> dict[str, Any]:
    user = api_get(f"{GITHUB_API_ROOT}/user", token)
    if not isinstance(user, dict) or not isinstance(user.get("login"), str):
        raise BackupError("GitHub API 的用户信息格式不符合预期")

    login = user["login"]
    if login.casefold() != GITHUB_USERNAME.casefold():
        raise BackupError(
            f"Token 属于 GitHub 用户 {login}，但脚本配置的是 {GITHUB_USERNAME}"
        )
    return user


def list_owned_repositories(token: str) -> list[dict[str, Any]]:
    repositories: list[dict[str, Any]] = []
    page = 1
    per_page = 100

    while True:
        query = urlencode(
            {
                "affiliation": "owner",
                "visibility": "all",
                "sort": "full_name",
                "direction": "asc",
                "per_page": per_page,
                "page": page,
            }
        )
        data = api_get(f"{GITHUB_API_ROOT}/user/repos?{query}", token)
        if not isinstance(data, list):
            raise BackupError("GitHub 仓库列表格式不符合预期")

        for repository in data:
            if not isinstance(repository, dict):
                raise BackupError("GitHub 仓库信息格式不符合预期")
            required_fields = ("id", "name", "full_name", "ssh_url", "owner")
            if any(field not in repository for field in required_fields):
                raise BackupError("GitHub 仓库信息缺少必需字段")
            if not isinstance(repository["id"], int) or any(
                not isinstance(repository[field], str) or not repository[field]
                for field in ("name", "full_name", "ssh_url")
            ):
                raise BackupError("GitHub 仓库字段类型不符合预期")
            default_branch = repository.get("default_branch")
            if default_branch is not None and (
                not isinstance(default_branch, str) or not default_branch
            ):
                raise BackupError("GitHub 仓库 default_branch 格式不符合预期")
            owner = repository.get("owner")
            if not isinstance(owner, dict) or not isinstance(owner.get("login"), str):
                raise BackupError("GitHub 仓库 owner 信息格式不符合预期")
            if owner["login"].casefold() != GITHUB_USERNAME.casefold():
                raise BackupError(
                    f"API 返回了不属于 {GITHUB_USERNAME} 的仓库："
                    f"{repository.get('full_name')}"
                )
            repositories.append(repository)

        if len(data) < per_page:
            break
        page += 1

    return repositories


def verify_ssh_connection() -> None:
    result = run_command(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={SSH_TIMEOUT_SECONDS}",
            "-T",
            "git@github.com",
        ],
        timeout=SSH_TIMEOUT_SECONDS + 5,
    )
    output = result.output
    match = re.search(
        r"Hi\s+([^!]+)!.*successfully authenticated", output, re.IGNORECASE | re.DOTALL
    )
    if match is None:
        detail = truncate_error(output) or f"ssh 退出码：{result.returncode}"
        raise BackupError(
            "GitHub SSH 认证失败。请先手动运行 `ssh -T git@github.com` "
            f"确认主机指纹和 SSH Key。详细信息：{detail}"
        )

    ssh_username = match.group(1).strip()
    if ssh_username.casefold() != GITHUB_USERNAME.casefold():
        raise BackupError(
            f"SSH Key 属于 GitHub 用户 {ssh_username}，"
            f"但脚本配置的是 {GITHUB_USERNAME}"
        )


def validate_manifest_path(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise BackupError("manifest.json 中存在无效的仓库路径")
    path = Path(value)
    if path.is_absolute() or path.name != value or value in {".", ".."}:
        raise BackupError(f"manifest.json 中存在不安全的仓库路径：{value}")
    if not value.endswith(".git"):
        raise BackupError(f"manifest.json 中的仓库路径不是 mirror 目录：{value}")
    return value


def load_manifest(path: Path) -> dict[str, Any]:
    if not path_entry_exists(path):
        return {
            "version": 1,
            "github_username": GITHUB_USERNAME,
            "repositories": {},
        }
    if path.is_symlink() or not path.is_file():
        raise BackupError(f"manifest 路径不是普通文件：{path}")

    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"无法读取 manifest：{path}：{exc}") from exc

    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise BackupError("manifest.json 的格式或版本不受支持")
    manifest_username = manifest.get("github_username")
    if (
        not isinstance(manifest_username, str)
        or manifest_username.casefold() != GITHUB_USERNAME.casefold()
    ):
        raise BackupError(
            f"manifest 属于用户 {manifest_username!r}，"
            f"当前脚本配置的是 {GITHUB_USERNAME}"
        )

    repositories = manifest.get("repositories")
    if not isinstance(repositories, dict):
        raise BackupError("manifest.json 中的 repositories 不是对象")

    claimed_paths: dict[str, str] = {}
    for repository_id, entry in repositories.items():
        if not isinstance(repository_id, str) or not isinstance(entry, dict):
            raise BackupError("manifest.json 中存在无效的仓库记录")
        local_path = validate_manifest_path(entry.get("path"))
        if local_path is not None:
            previous_id = claimed_paths.get(local_path)
            if previous_id is not None:
                raise BackupError(
                    f"manifest 中的仓库 {previous_id} 和 {repository_id} "
                    f"同时使用路径 {local_path}"
                )
            claimed_paths[local_path] = repository_id

    return manifest


def determine_backup_mode(backup_root: Path, manifest_path: Path) -> str:
    if path_entry_exists(manifest_path):
        return BACKUP_MODE_INCREMENTAL

    unexpected_entries = sorted(
        (
            path.name
            for path in backup_root.iterdir()
            if path.name != LOCK_FILENAME
        ),
        key=str.casefold,
    )
    if unexpected_entries:
        preview = ", ".join(repr(name) for name in unexpected_entries[:10])
        if len(unexpected_entries) > 10:
            preview += f", ...（共 {len(unexpected_entries)} 项）"
        raise BackupError(
            "备份目录中没有 manifest.json，但目录并非空目录。为避免错误接管或覆盖"
            f"现有数据，脚本已终止。现有内容：{preview}"
        )
    return BACKUP_MODE_FULL


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    ensure_real_directory(path.parent, parents=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        log_file_action(f"原子写入 JSON：{temporary_path} -> {path}")
        os.replace(temporary_path, path)
    except BaseException:
        try:
            if path_entry_exists(temporary_path):
                log_file_action(f"清理 JSON 临时文件：{temporary_path}")
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def ssh_repository_identity(url: str) -> str | None:
    """把支持的 GitHub SSH URL 归一化为小写的 owner/repository。"""
    value = url.strip()
    scp_match = re.fullmatch(r"git@github\.com:(.+)", value, re.IGNORECASE)
    if scp_match:
        repository_path = scp_match.group(1)
    else:
        parsed = urlsplit(value)
        if (
            parsed.scheme.casefold() != "ssh"
            or (parsed.hostname or "").casefold() != "github.com"
            or (parsed.username or "").casefold() != "git"
        ):
            return None
        repository_path = parsed.path.lstrip("/")

    if repository_path.endswith("/"):
        repository_path = repository_path[:-1]
    if repository_path.casefold().endswith(".git"):
        repository_path = repository_path[:-4]
    if repository_path.count("/") != 1:
        return None
    owner, name = repository_path.split("/", 1)
    if not owner or not name:
        return None
    return f"{owner}/{name}".casefold()


def inspect_mirror(path: Path) -> str:
    if path.is_symlink():
        raise RepositoryConflict(f"本地路径是符号链接，不会操作：{path}")
    if not path.is_dir():
        raise RepositoryConflict(f"本地路径存在但不是目录：{path}")

    bare_result = run_command(
        ["git", "-C", str(path), "rev-parse", "--is-bare-repository"]
    )
    if bare_result.returncode != 0 or bare_result.stdout.casefold() != "true":
        raise RepositoryConflict(f"本地目录不是 bare Git 仓库：{path}")

    mirror_result = run_command(
        ["git", "-C", str(path), "config", "--bool", "--get", "remote.origin.mirror"]
    )
    if mirror_result.returncode != 0 or mirror_result.stdout.casefold() != "true":
        raise RepositoryConflict(f"本地目录不是 `git clone --mirror` 仓库：{path}")

    remote_result = run_command(
        ["git", "-C", str(path), "remote", "get-url", "origin"]
    )
    if remote_result.returncode != 0:
        raise RepositoryConflict(
            f"本地 mirror 没有可读取的 origin：{path}：{remote_result.output}"
        )
    identity = ssh_repository_identity(remote_result.stdout)
    if identity is None:
        raise RepositoryConflict(
            f"本地 mirror 的 origin 不是 GitHub SSH 地址：{remote_result.stdout}"
        )
    return identity


def safe_repository_name(name: Any) -> str:
    if not isinstance(name, str) or not name:
        raise RepositoryConflict("GitHub 返回了空仓库名")
    if Path(name).name != name or name in {".", ".."}:
        raise RepositoryConflict(f"GitHub 返回了不安全的仓库名：{name!r}")
    return name


def command_failure(prefix: str, result: CommandResult) -> RuntimeError:
    detail = truncate_error(result.output) or f"退出码 {result.returncode}"
    return RuntimeError(f"{prefix}：{detail}")


def sync_mirror_head(repository: dict[str, Any], path: Path) -> None:
    """让 bare mirror 的 HEAD 跟随 GitHub 当前默认分支。"""
    default_branch = repository.get("default_branch")
    if default_branch is None:
        return

    head_reference = f"refs/heads/{default_branch}"
    check_result = run_command(["git", "check-ref-format", head_reference])
    if check_result.returncode != 0:
        raise command_failure(
            f"GitHub 默认分支无法转换为安全 ref：{default_branch}", check_result
        )

    head_result = run_command(
        ["git", "-C", str(path), "symbolic-ref", "HEAD", head_reference]
    )
    if head_result.returncode != 0:
        raise command_failure("更新 mirror HEAD 失败", head_result)


def clone_mirror(repository: dict[str, Any], destination: Path) -> None:
    temporary_path = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
    )
    log_file_action(f"创建克隆临时目录：{temporary_path}")
    try:
        result = run_command(
            ["git", "clone", "--mirror", str(repository["ssh_url"]), str(temporary_path)]
        )
        if result.returncode != 0:
            raise command_failure("git clone --mirror 失败", result)

        actual_identity = inspect_mirror(temporary_path)
        expected_identity = ssh_repository_identity(str(repository["ssh_url"]))
        if expected_identity is None or actual_identity != expected_identity:
            raise RepositoryConflict(
                f"新 mirror 的 origin 与仓库不一致：{repository['full_name']}"
            )
        sync_mirror_head(repository, temporary_path)
        if path_entry_exists(destination):
            raise RepositoryConflict(f"克隆期间目标路径被其他内容占用：{destination}")
        log_file_action(f"启用完整 mirror：{temporary_path} -> {destination}")
        temporary_path.rename(destination)
    finally:
        if path_entry_exists(temporary_path):
            log_file_action(f"清理克隆临时路径：{temporary_path}")
            if temporary_path.is_symlink() or not temporary_path.is_dir():
                temporary_path.unlink()
            else:
                shutil.rmtree(temporary_path)


def update_mirror(
    repository: dict[str, Any], path: Path, allowed_identities: set[str]
) -> None:
    actual_identity = inspect_mirror(path)
    if actual_identity not in allowed_identities:
        expected = ", ".join(sorted(allowed_identities))
        raise RepositoryConflict(
            f"本地 mirror 的 origin 是 {actual_identity}，预期为 {expected}：{path}"
        )

    set_url_result = run_command(
        [
            "git",
            "-C",
            str(path),
            "remote",
            "set-url",
            "origin",
            str(repository["ssh_url"]),
        ]
    )
    if set_url_result.returncode != 0:
        raise command_failure("更新 origin SSH 地址失败", set_url_result)

    update_result = run_command(
        ["git", "-C", str(path), "remote", "update", "--prune"]
    )
    if update_result.returncode != 0:
        raise command_failure("git remote update --prune 失败", update_result)
    sync_mirror_head(repository, path)


def previous_repository_identity(entry: dict[str, Any] | None) -> str | None:
    if not entry:
        return None
    ssh_url = entry.get("ssh_url")
    if isinstance(ssh_url, str):
        identity = ssh_repository_identity(ssh_url)
        if identity is not None:
            return identity
    full_name = entry.get("full_name")
    if isinstance(full_name, str) and full_name.count("/") == 1:
        return full_name.casefold()
    return None


def sync_repository(
    repository: dict[str, Any],
    backup_root: Path,
    previous_entry: dict[str, Any] | None,
) -> dict[str, Any]:
    started = time.monotonic()
    name = safe_repository_name(repository.get("name"))
    destination = backup_root / f"{name}.git"
    current_path = destination
    renamed_from: str | None = None
    action = "updated"

    try:
        old_relative_path = (
            validate_manifest_path(previous_entry.get("path"))
            if previous_entry is not None
            else None
        )
        if old_relative_path and old_relative_path != destination.name:
            old_path = backup_root / old_relative_path
            if path_entry_exists(old_path):
                if path_entry_exists(destination):
                    raise RepositoryConflict(
                        f"仓库疑似由 {old_relative_path} 改名为 {destination.name}，"
                        "但新旧路径同时存在"
                    )

                allowed_before_rename = {
                    identity
                    for identity in (
                        previous_repository_identity(previous_entry),
                        ssh_repository_identity(str(repository["ssh_url"])),
                    )
                    if identity is not None
                }
                actual_identity = inspect_mirror(old_path)
                if actual_identity not in allowed_before_rename:
                    raise RepositoryConflict(
                        f"manifest 中的路径 {old_relative_path} 指向其他仓库："
                        f"{actual_identity}"
                    )
                log_file_action(f"仓库目录改名：{old_path} -> {destination}")
                old_path.rename(destination)
                current_path = destination
                renamed_from = old_relative_path
                action = "renamed_and_updated"
            elif path_entry_exists(destination):
                current_path = destination

        expected_identity = ssh_repository_identity(str(repository["ssh_url"]))
        if expected_identity is None:
            raise RepositoryConflict(
                f"GitHub 返回的 ssh_url 格式不受支持：{repository['ssh_url']}"
            )
        allowed_identities = {expected_identity}
        previous_identity = previous_repository_identity(previous_entry)
        if previous_identity is not None:
            allowed_identities.add(previous_identity)

        if path_entry_exists(current_path):
            update_mirror(repository, current_path, allowed_identities)
        else:
            action = "created"
            clone_mirror(repository, current_path)

        status = "success"
        error = None
    except RepositoryConflict as exc:
        status = "conflict"
        error = truncate_error(str(exc))
    except (BackupError, OSError, RuntimeError) as exc:
        status = "failed"
        error = truncate_error(str(exc))

    result: dict[str, Any] = {
        "id": repository["id"],
        "full_name": repository["full_name"],
        "status": status,
        "action": action,
        "path": current_path.name,
        "duration_seconds": round(time.monotonic() - started, 3),
    }
    if renamed_from is not None:
        result["renamed_from"] = renamed_from
    if error is not None:
        result["error"] = error
    return result


def allocated_size(path: Path, seen_inodes: set[tuple[int, int]] | None = None) -> int:
    """统计 Linux 文件系统实际分配的数据块，而不是文件的逻辑长度。"""
    if seen_inodes is None:
        seen_inodes = set()

    try:
        entry_stat = path.lstat()
    except FileNotFoundError:
        return 0

    inode = (entry_stat.st_dev, entry_stat.st_ino)
    if inode in seen_inodes:
        return 0
    seen_inodes.add(inode)
    total = entry_stat.st_blocks * 512

    if not stat.S_ISDIR(entry_stat.st_mode) or stat.S_ISLNK(entry_stat.st_mode):
        return total

    try:
        with os.scandir(path) as entries:
            for entry in entries:
                total += allocated_size(Path(entry.path), seen_inodes)
    except PermissionError as exc:
        raise BackupError(f"没有权限统计目录大小：{path}") from exc
    return total


def mirror_directories(backup_root: Path) -> list[Path]:
    directories: list[Path] = []
    for path in backup_root.iterdir():
        if path.name.endswith(".git") and path.is_dir() and not path.is_symlink():
            directories.append(path)
    return sorted(directories, key=lambda item: item.name.casefold())


def measure_mirrors(backup_root: Path) -> tuple[dict[str, int], int]:
    directories = mirror_directories(backup_root)
    per_repository = {path.name: allocated_size(path) for path in directories}

    # 单独使用共享 inode 集合计算总量，避免极少见的跨目录硬链接被重复统计。
    seen_inodes: set[tuple[int, int]] = set()
    total = sum(allocated_size(path, seen_inodes) for path in directories)
    return per_repository, total


def result_for_claim_conflict(
    repository: dict[str, Any], path: str, other_repository_id: str
) -> dict[str, Any]:
    return {
        "id": repository["id"],
        "full_name": repository["full_name"],
        "status": "conflict",
        "action": "updated",
        "path": None,
        "requested_path": path,
        "duration_seconds": 0.0,
        "error": (
            f"本地路径 {path} 已由 manifest 中的另一个仓库 ID "
            f"{other_repository_id} 占用；可能是仓库名被重新使用"
        ),
    }


def update_manifest_entry(
    manifest: dict[str, Any],
    repository: dict[str, Any],
    result: dict[str, Any],
    timestamp: str,
) -> None:
    repository_id = str(repository["id"])
    repositories = manifest["repositories"]
    entry = dict(repositories.get(repository_id, {}))
    if result["status"] != "conflict":
        entry.update(
            {
                "id": repository["id"],
                "name": repository["name"],
                "full_name": repository["full_name"],
                "ssh_url": repository["ssh_url"],
            }
        )
        entry["path"] = result["path"]
    else:
        # 已有仓库发生冲突时保留旧名称、URL 和路径，以便问题解决后仍能根据
        # manifest 识别改名前的 mirror；新仓库冲突则只记录其基本身份。
        entry.setdefault("id", repository["id"])
        entry.setdefault("name", repository["name"])
        entry.setdefault("full_name", repository["full_name"])
        entry.setdefault("ssh_url", repository["ssh_url"])
    entry["last_seen_at"] = timestamp
    entry["last_status"] = result["status"]
    if result["status"] == "success":
        entry["last_success_at"] = timestamp
    repositories[repository_id] = entry


def stale_results(
    manifest: dict[str, Any],
    current_repository_ids: set[str],
    backup_root: Path,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for repository_id, entry in manifest["repositories"].items():
        if repository_id in current_repository_ids:
            continue
        local_path = validate_manifest_path(entry.get("path"))
        if local_path is None:
            continue
        path = backup_root / local_path
        results.append(
            {
                "id": entry.get("id", repository_id),
                "full_name": entry.get("full_name", local_path[:-4]),
                "status": "not_returned_by_api",
                "action": "kept",
                "path": local_path,
                "local_path_exists": path_entry_exists(path),
                "note": "本次 API 未返回该仓库；本地数据未删除",
            }
        )
    return results


def unmanaged_local_results(
    backup_root: Path,
    known_paths: set[str],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for path in mirror_directories(backup_root):
        if path.name in known_paths:
            continue
        results.append(
            {
                "id": None,
                "full_name": None,
                "status": "unmanaged_local_mirror",
                "action": "kept",
                "path": path.name,
                "note": "该 mirror 不在 manifest 或本次 API 结果中；本地数据未删除",
            }
        )
    return results


def unique_report_path(report_directory: Path, started_at: datetime) -> Path:
    stem = started_at.strftime("backup-%Y%m%dT%H%M%SZ")
    candidate = report_directory / f"{stem}.json"
    suffix = 2
    while path_entry_exists(candidate):
        candidate = report_directory / f"{stem}-{suffix}.json"
        suffix += 1
    return candidate


def add_disk_usage_to_results(
    results: Iterable[dict[str, Any]], sizes: dict[str, int]
) -> None:
    for result in results:
        path = result.get("path")
        if (
            result.get("status") != "conflict"
            and isinstance(path, str)
            and path in sizes
        ):
            byte_count = sizes[path]
            result["disk_usage_bytes"] = byte_count
            result["disk_usage_human"] = human_size(byte_count)
        else:
            result["disk_usage_bytes"] = None
            result["disk_usage_human"] = None


def print_repository_result(index: int, total: int, result: dict[str, Any]) -> None:
    status_labels = {
        "success": "成功",
        "failed": "失败",
        "conflict": "冲突",
    }
    action_labels = {
        "created": "首次克隆",
        "updated": "已同步",
        "renamed_and_updated": "改名并同步",
    }
    status = status_labels.get(result["status"], result["status"])
    action = action_labels.get(result["action"], result["action"])
    print(f"[{index}/{total}] {result['full_name']}：{status}（{action}）", flush=True)
    if result.get("error"):
        print(f"    {result['error']}", flush=True)


def build_summary(
    backup_mode: str,
    repositories: list[dict[str, Any]],
    results: list[dict[str, Any]],
    stale: list[dict[str, Any]],
    unmanaged: list[dict[str, Any]],
    before_bytes: int,
    after_bytes: int,
    started_at: datetime,
    finished_at: datetime,
    interrupted: bool,
) -> dict[str, Any]:
    successful = [result for result in results if result["status"] == "success"]
    return {
        "backup_mode": backup_mode,
        "repositories_returned_by_api": len(repositories),
        "repositories_attempted": len(results),
        "successful": len(successful),
        "created": sum(result["action"] == "created" for result in successful),
        "updated": sum(
            result["action"] in {"updated", "renamed_and_updated"}
            for result in successful
        ),
        "renamed": sum(
            result["action"] == "renamed_and_updated" for result in successful
        ),
        "failed": sum(result["status"] == "failed" for result in results),
        "conflicts": sum(result["status"] == "conflict" for result in results),
        "not_returned_by_api": len(stale),
        "unmanaged_local_mirrors": len(unmanaged),
        "interrupted": interrupted,
        "duration_seconds": round((finished_at - started_at).total_seconds(), 3),
        "space_before_bytes": before_bytes,
        "space_before_human": human_size(before_bytes),
        "space_after_bytes": after_bytes,
        "space_after_human": human_size(after_bytes),
        "space_change_bytes": after_bytes - before_bytes,
        "space_change_human": human_size(after_bytes - before_bytes, signed=True),
    }


def print_summary(summary: dict[str, Any], report_path: Path) -> None:
    print("\nGitHub Git mirror 备份完成")
    mode_label = {
        BACKUP_MODE_FULL: "全量备份（full）",
        BACKUP_MODE_INCREMENTAL: "增量更新（incremental）",
    }[summary["backup_mode"]]
    print(f"备份模式：     {mode_label}")
    print(f"API 返回仓库： {summary['repositories_returned_by_api']}")
    print(f"已尝试：       {summary['repositories_attempted']}")
    print(f"成功：         {summary['successful']}")
    print(f"首次克隆：     {summary['created']}")
    print(f"同步更新：     {summary['updated']}")
    print(f"其中改名：     {summary['renamed']}")
    print(f"失败：         {summary['failed']}")
    print(f"冲突：         {summary['conflicts']}")
    print(f"API 未返回：   {summary['not_returned_by_api']}")
    print(f"未管理 mirror：{summary['unmanaged_local_mirrors']}")
    print(f"执行前空间：   {summary['space_before_human']}")
    print(f"执行后空间：   {summary['space_after_human']}")
    print(f"空间变化：     {summary['space_change_human']}")
    print(f"总耗时：       {summary['duration_seconds']:.3f} 秒")
    if summary["interrupted"]:
        print("状态：         用户中断，报告仅包含已尝试的仓库")
    print(f"报告：         {report_path}")


def perform_backup(backup_root: Path, token: str) -> int:
    ensure_real_directory(backup_root, parents=True)

    with BackupLock(backup_root / LOCK_FILENAME):
        started_at = utc_now()
        started_timestamp = iso_time(started_at)
        manifest_path = backup_root / MANIFEST_FILENAME
        backup_mode = determine_backup_mode(backup_root, manifest_path)
        mode_label = {
            BACKUP_MODE_FULL: "全量备份（full）：目录为空且没有 manifest",
            BACKUP_MODE_INCREMENTAL: "增量更新（incremental）：读取现有 manifest",
        }[backup_mode]
        print(f"备份模式：{mode_label}")
        manifest = load_manifest(manifest_path)
        before_sizes, before_bytes = measure_mirrors(backup_root)

        user = verify_api_user(token)
        print(f"GitHub API 用户：{user['login']}")
        repositories = list_owned_repositories(token)
        # 先处理 manifest 中已有的仓库。这样仓库改名后又立即复用了旧名称时，
        # 旧 mirror 会先完成改名，新仓库随后可以在同一次运行中正常克隆。
        repositories.sort(
            key=lambda repository: (
                str(repository["id"]) not in manifest["repositories"],
                str(repository["full_name"]).casefold(),
            )
        )
        print(f"找到 {len(repositories)} 个由该账号拥有的仓库")

        verify_ssh_connection()
        print("GitHub SSH 认证成功")

        current_ids = {str(repository["id"]) for repository in repositories}
        path_claims = {
            entry["path"]: repository_id
            for repository_id, entry in manifest["repositories"].items()
            if isinstance(entry.get("path"), str)
        }

        results: list[dict[str, Any]] = []
        interrupted = False
        total = len(repositories)
        try:
            for index, repository in enumerate(repositories, start=1):
                repository_id = str(repository["id"])
                destination_name = f"{safe_repository_name(repository['name'])}.git"
                claimed_by = path_claims.get(destination_name)

                if (
                    claimed_by is not None
                    and claimed_by != repository_id
                    and path_entry_exists(backup_root / destination_name)
                ):
                    result = result_for_claim_conflict(
                        repository, destination_name, claimed_by
                    )
                else:
                    if claimed_by is not None and claimed_by != repository_id:
                        old_entry = manifest["repositories"].get(claimed_by)
                        if (
                            isinstance(old_entry, dict)
                            and old_entry.get("path") == destination_name
                        ):
                            old_entry.pop("path", None)
                        path_claims.pop(destination_name, None)
                    previous_entry = manifest["repositories"].get(repository_id)
                    result = sync_repository(
                        repository,
                        backup_root,
                        previous_entry if isinstance(previous_entry, dict) else None,
                    )

                results.append(result)
                update_manifest_entry(
                    manifest, repository, result, started_timestamp
                )
                if result["status"] != "conflict":
                    for claimed_path, owner_id in list(path_claims.items()):
                        if owner_id == repository_id and claimed_path != result["path"]:
                            path_claims.pop(claimed_path)
                    path_claims[result["path"]] = repository_id
                print_repository_result(index, total, result)

                if index < total and SLEEP_SECONDS > 0:
                    time.sleep(SLEEP_SECONDS)
        except KeyboardInterrupt:
            interrupted = True
            print("\n收到中断信号，正在保存 manifest 和本次报告……", flush=True)

        manifest["last_run_at"] = started_timestamp
        manifest["last_run_mode"] = backup_mode
        write_json_atomic(manifest_path, manifest)

        stale = stale_results(manifest, current_ids, backup_root)
        known_paths = {
            entry["path"]
            for entry in manifest["repositories"].values()
            if isinstance(entry.get("path"), str)
        }
        unmanaged = unmanaged_local_results(backup_root, known_paths)

        after_sizes, after_bytes = measure_mirrors(backup_root)
        add_disk_usage_to_results(results, after_sizes)
        add_disk_usage_to_results(stale, after_sizes)
        add_disk_usage_to_results(unmanaged, after_sizes)

        finished_at = utc_now()
        summary = build_summary(
            backup_mode,
            repositories,
            results,
            stale,
            unmanaged,
            before_bytes,
            after_bytes,
            started_at,
            finished_at,
            interrupted,
        )
        report = {
            "version": 1,
            "github_username": GITHUB_USERNAME,
            "backup_root": str(backup_root),
            "backup_mode": backup_mode,
            "started_at": iso_time(started_at),
            "finished_at": iso_time(finished_at),
            "summary": summary,
            "repositories": results,
            "not_returned_by_api": stale,
            "unmanaged_local_mirrors": unmanaged,
            "space_before_by_path_bytes": before_sizes,
            "space_after_by_path_bytes": after_sizes,
        }
        report_directory = backup_root / REPORT_DIRECTORY_NAME
        report_path = unique_report_path(report_directory, started_at)
        write_json_atomic(report_path, report)
        print_summary(summary, report_path)

        if interrupted:
            return 130
        if summary["failed"] or summary["conflicts"]:
            return 1
        return 0


def main() -> int:
    try:
        backup_root, token = validate_configuration()
        return perform_backup(backup_root, token)
    except BackupError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"文件系统错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n操作已由用户中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
