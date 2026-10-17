"""
core.py — Claude 换号会话迁移的纯逻辑层（无界面依赖，可单独测试），也带一个薄命令行入口。

背景与原理见 README.md。核心机制：

  桌面端 session 分两层存：
    - sidebar 元数据 local_*.json  →  按账号分目录：<base>/<账号GUID>/<组织GUID>/local_*.json
    - 对话原文 *.jsonl             →  ~/.claude/projects/...，账号无关，全程不动
  换号后新账号目录是空的，sidebar 就看不到旧 session。把旧账号的 local_*.json
  拷到新账号目录即可让 sidebar 重新列出（靠每个 json 里的 cliSessionId 与 .jsonl 配对）。

  桌面端删除会话时，会在同一 workspace 里写墓碑文件 deleted_<id>（id 为 local id
  或 cliSessionId）。计划阶段据此跳过「目标里已经删掉」的会话，免得迁移时又拷回来。

本模块只做「元数据文件」的探测、计划、备份拷贝、撤销 / 回滚，绝不触碰 .jsonl 对话原文。

命令行（给脚本 / AI 用，默认只预览，加 --apply 才写入）：python core.py --help
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# 备份 / 回滚 / 撤销目录的命名标记（这些目录名不是 GUID，探测 workspace 时自然被排除）
BACKUP_MARKER = "__BACKUP_BEFORE_MIGRATION_"
REPLACED_MARKER = "__REPLACED_"
UNDONE_MARKER = "__UNDONE_MIGRATION_"
LOCAL_PREFIX = "local_"
LOCAL_SUFFIX = ".json"
DELETED_PREFIX = "deleted_"                  # 桌面端删除会话时留下的墓碑文件
MANIFEST_NAME = "migration_manifest.json"    # 写在备份目录里：这次迁移拷入 / 更新了哪些文件
LOG_NAME = "migration.log"                   # 写在备份目录里：这次迁移逐个文件的动作和原因
NEW_ACCOUNT_WINDOW = 24 * 3600               # 「可能是新账号」：这么久以内新建、且没有任何记录
GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Workspace:
    name: str
    path: str
    sessions: int


@dataclass
class Account:
    name: str                       # 完整账号目录名（GUID）
    path: str
    mtime: float
    workspace: Optional[Workspace]  # 主 workspace（local_*.json 最多的组织目录）
    all_workspaces: list[Workspace] = field(default_factory=list)
    base: str = ""                  # 所属 session 根目录（多 profile 时区分来源）
    last_login: bool = False        # config.json 的 lastKnownAccountUuid 指向它（桌面端最近登录的账号）
    created: float = 0.0            # 账号目录的创建时间

    @property
    def profile(self) -> str:
        return profile_label(self.base or self.path)

    @property
    def short(self) -> str:
        return (self.name[:8] + "…") if len(self.name) > 8 else self.name

    @property
    def sessions(self) -> int:
        return self.workspace.sessions if self.workspace else 0

    @property
    def ws_path(self) -> Optional[str]:
        return self.workspace.path if self.workspace else None

    @property
    def ws_name(self) -> str:
        return self.workspace.name if self.workspace else "(无)"

    @property
    def mtime_str(self) -> str:
        return datetime.fromtimestamp(self.mtime).strftime("%Y-%m-%d %H:%M")

    @property
    def maybe_new(self) -> bool:
        """24 小时内新建、且所有组织目录里都没有记录——多半就是刚登录的新账号。"""
        return (sum(w.sessions for w in self.all_workspaces) == 0
                and self.created > time.time() - NEW_ACCOUNT_WINDOW)

    @property
    def marks(self) -> str:
        out = []
        if self.last_login:
            out.append("最近登录")
        if self.maybe_new:
            out.append("可能是新账号（24 小时内新建、无记录）")
        return "；".join(out)


@dataclass
class SourcePlan:
    account: str
    ws_name: str
    files: list[str]                # 计划拷入或更新的源文件绝对路径
    copy: int                       # 目标里没有 → 新拷入
    skip: int                       # 目标已有（同名或同 cliSessionId，且源不比它新），或与排在前面的源重复
    skip_deleted: int = 0           # 目标里已删除（命中 deleted_ 墓碑）
    update: int = 0                 # 目标已有同名记录、但源更新 → 覆盖（旧版本在备份里，可撤销）
    invalid: int = 0                # 不是带 sessionId 的会话记录 JSON → 不拷
    decisions: list[tuple[str, str, str]] = field(default_factory=list)  # (动作, 源文件, 原因)


@dataclass
class MigrationPlan:
    target: Account
    sources: list[Account]
    items: list[SourcePlan]
    total_copy: int
    total_skip: int
    all_workspaces: bool = False
    total_deleted: int = 0
    warnings: list[str] = field(default_factory=list)   # 目标里读不了的记录等提示
    total_update: int = 0
    total_invalid: int = 0
    update_newer: bool = True


# --------------------------------------------------------------------------- #
# 平台路径
# --------------------------------------------------------------------------- #
def get_session_base() -> str:
    """返回当前平台的 claude-code-sessions 根目录（不保证存在）。"""
    system = platform.system()
    home = os.path.expanduser("~")
    if system == "Windows":
        appdata = os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming"))
        return os.path.join(appdata, "Claude", "claude-code-sessions")
    if system == "Darwin":
        return os.path.join(home, "Library", "Application Support", "Claude", "claude-code-sessions")
    # Linux / 其它
    return os.path.join(home, ".config", "Claude", "claude-code-sessions")


def _msix_fallbacks(pattern: str) -> list[str]:
    """在 MSIX 包的 LocalCache 里找虚拟化的 session 目录（按包名排序保证稳定）。"""
    import glob as _glob

    home = os.path.expanduser("~")
    localappdata = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
    return sorted(
        p for p in _glob.glob(os.path.join(localappdata, "Packages", "*", "LocalCache", pattern))
        if os.path.isdir(p)
    )


def get_session_bases() -> list[str]:
    """
    返回所有候选 session 根目录（可能不存在，调用方自行过滤）。

    桌面端有两套独立 profile，各自一份 claude-code-sessions：
      - claude.ai 登录态：Windows 在 %APPDATA%\\Claude\\
      - API key（第三方）模式：Windows 在 %LOCALAPPDATA%\\Claude-3p\\
    「登录 ↔ API」互相看不到对方的对话，正是因为分属两个 base。

    另外，商店版（MSIX）应用的子进程会被文件系统虚拟化：从 Claude 桌面端内部
    启动本工具时，%APPDATA%\\Claude 可能解析不到真实目录。因此当主路径不可见时，
    回退到 %LOCALAPPDATA%\\Packages\\<pkg>\\LocalCache\\ 下的虚拟化副本（同一份数据）。
    """
    system = platform.system()
    home = os.path.expanduser("~")
    if system == "Windows":
        localappdata = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
        login = get_session_base()
        api = os.path.join(localappdata, "Claude-3p", "claude-code-sessions")
        bases: list[str] = []
        for primary, pattern in (
            (login, os.path.join("Roaming", "Claude", "claude-code-sessions")),
            (api, os.path.join("Local", "Claude-3p", "claude-code-sessions")),
        ):
            if os.path.isdir(primary):
                bases.append(primary)
            else:
                fallbacks = _msix_fallbacks(pattern)
                bases.extend(fallbacks)
                if not fallbacks:
                    bases.append(primary)  # 保留缺失的主路径，界面据此提示
        return bases
    if system == "Darwin":
        return [get_session_base(),
                os.path.join(home, "Library", "Application Support", "Claude-3p", "claude-code-sessions")]
    return [get_session_base(), os.path.join(home, ".config", "Claude-3p", "claude-code-sessions")]


def profile_label(base_or_path: str) -> str:
    """根据路径判断它属于哪个 profile，返回给界面显示的短标签。"""
    return "API版" if "Claude-3p" in base_or_path else "登录版"


def get_projects_dir() -> str:
    """返回对话原文 .jsonl 的根目录（仅供界面说明，本工具不写入）。"""
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def read_last_account(base: str) -> Optional[str]:
    """读 base 上一级目录 config.json 里的 lastKnownAccountUuid（只取这一个字段，其它内容不读出、不记录）。"""
    try:
        with open(os.path.join(os.path.dirname(base.rstrip("\\/")), "config.json"), encoding="utf-8") as f:
            value = json.load(f).get("lastKnownAccountUuid")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and GUID_RE.match(value) else None


# --------------------------------------------------------------------------- #
# 探测
# --------------------------------------------------------------------------- #
def _count_local_json(dir_path: str) -> int:
    try:
        return sum(
            1
            for e in os.scandir(dir_path)
            if e.is_file() and e.name.startswith(LOCAL_PREFIX) and e.name.endswith(LOCAL_SUFFIX)
        )
    except OSError:
        return 0


def _list_local_json(dir_path: str) -> list[str]:
    try:
        return sorted(
            e.path
            for e in os.scandir(dir_path)
            if e.is_file() and e.name.startswith(LOCAL_PREFIX) and e.name.endswith(LOCAL_SUFFIX)
        )
    except OSError:
        return []


def list_workspaces(account_path: str) -> list[Workspace]:
    """列出账号下的组织目录（名字必须是 GUID；备份 / 替换 / 撤销目录和其它目录都不算）。按名称排序。"""
    out: list[Workspace] = []
    try:
        entries = sorted(os.scandir(account_path), key=lambda e: e.name)
    except OSError:
        return out
    for e in entries:
        if e.is_dir() and GUID_RE.match(e.name):
            out.append(Workspace(name=e.name, path=e.path, sessions=_count_local_json(e.path)))
    return out


def list_accounts(base: str) -> list[Account]:
    """列出所有账号（目录名必须是 GUID，其它目录如 skills-plugin 忽略），按 mtime 降序（平局取名称升序）。"""
    accounts: list[Account] = []
    if not os.path.isdir(base):
        return accounts
    try:
        dirs = [e for e in os.scandir(base) if e.is_dir() and GUID_RE.match(e.name)]
    except OSError:
        return accounts
    last = read_last_account(base)
    # 先按名称升序，稳定排序后再按 mtime 降序 → 平局时保持名称顺序
    dirs.sort(key=lambda e: e.name)
    dirs.sort(key=lambda e: e.stat().st_mtime, reverse=True)
    for e in dirs:
        st = e.stat()
        all_ws = list_workspaces(e.path)
        primary = None
        for ws in all_ws:
            if primary is None or ws.sessions > primary.sessions:
                primary = ws
        accounts.append(
            Account(
                name=e.name,
                path=e.path,
                mtime=st.st_mtime,
                workspace=primary,
                all_workspaces=all_ws,
                base=base,
                last_login=bool(last) and e.name.lower() == last.lower(),
                created=getattr(st, "st_birthtime", None) or st.st_ctime,
            )
        )
    return accounts


def list_accounts_multi(bases: list[str]) -> list[Account]:
    """聚合多个 base（登录版 + API版 profile）下的账号，统一按 mtime 降序。"""
    accounts: list[Account] = []
    for b in bases:
        accounts.extend(list_accounts(b))
    accounts.sort(key=lambda a: a.name)
    accounts.sort(key=lambda a: a.mtime, reverse=True)
    return accounts


def layout_notes(bases: list[str]) -> list[str]:
    """探测时被忽略的目录、以及整体结构不符合预期的 base（给界面 / 命令行提示用）。"""
    notes: list[str] = []
    for b in bases:
        try:
            dirs = [e.name for e in os.scandir(b) if e.is_dir()]
        except OSError:
            continue
        other = sorted(n for n in dirs if not GUID_RE.match(n))
        if other:
            notes.append(f"[{profile_label(b)}] 忽略不是账号的目录：{', '.join(other)}")
        if dirs and len(other) == len(dirs):
            notes.append(f"[{profile_label(b)}] 目录结构不符合预期（没有 <账号GUID>\\<组织GUID>），已跳过：{b}")
    return notes


def _check_ws_layout(ws: str) -> None:
    """要写入 / 读取的 workspace 必须是 <账号GUID>\\<组织GUID>，否则停下、什么都不动。"""
    org = os.path.basename(ws.rstrip("\\/"))
    acct = os.path.basename(os.path.dirname(ws.rstrip("\\/")))
    if not (GUID_RE.match(org) and GUID_RE.match(acct)):
        raise MigrationError(f"目录结构不符合预期（应为 <账号GUID>\\<组织GUID>）：{ws}\n未做任何改动。")


# --------------------------------------------------------------------------- #
# 会话元数据：判重、墓碑、新旧比较
# --------------------------------------------------------------------------- #
def _strip_prefix(s: str, prefix: str) -> str:
    return s[len(prefix):] if s.startswith(prefix) else s


def _fmt_ts(t: float) -> str:
    try:
        return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return str(t)


@dataclass
class _Meta:
    path: str
    name: str                 # local_<id>.json
    cli: Optional[str]        # cliSessionId
    ids: set                  # 墓碑可能引用的全部 id：local id、sessionId、cliSessionId、priorCliSessionIds
    valid: bool               # 能解析成带 sessionId 的 JSON 对象
    activity: Optional[float] # lastActivityAt（毫秒）；没有为 None
    mtime: float
    error: str = ""
    title: str = ""           # 会话标题（界面显示用）


# 界面里每次改勾选都要重算计划：按 (修改时间, 大小) 缓存解析结果，免得把几百个记录反复读一遍。
# 只有 cache=True 的调用（界面预览）会用它；执行迁移时的复核一律重新读盘。
_META_CACHE: dict[str, tuple[tuple[int, int], "_Meta"]] = {}


def _read_meta(path: str, cache: bool = False) -> _Meta:
    if not cache:
        return _parse_meta(path)
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return _parse_meta(path)
    hit = _META_CACHE.get(path)
    if hit and hit[0] == key:
        return hit[1]
    m = _parse_meta(path)
    _META_CACHE[path] = (key, m)
    return m


def _parse_meta(path: str) -> _Meta:
    name = os.path.basename(path)
    ids = {_strip_prefix(name[: -len(LOCAL_SUFFIX)], LOCAL_PREFIX)}  # 文件名里的 local id
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("顶层不是 JSON 对象")
    except (OSError, ValueError) as e:
        return _Meta(path, name, None, ids, False, None, mtime, str(e) or type(e).__name__)
    sid = data.get("sessionId")
    if not (isinstance(sid, str) and sid):
        return _Meta(path, name, None, ids, False, None, mtime, "没有 sessionId")
    ids.add(_strip_prefix(sid, LOCAL_PREFIX))
    cli = data.get("cliSessionId")
    cli = cli if isinstance(cli, str) and cli else None
    if cli:
        ids.add(cli)
    prior = data.get("priorCliSessionIds")
    if isinstance(prior, list):
        ids.update(x for x in prior if isinstance(x, str) and x)
    act = data.get("lastActivityAt")
    act = act if isinstance(act, (int, float)) and not isinstance(act, bool) else None
    title = data.get("title")
    return _Meta(path, name, cli, ids, True, act, mtime, title=title.strip() if isinstance(title, str) else "")


def peek_latest(ws_path: Optional[str]) -> tuple[str, float]:
    """workspace 里最近改动的那条记录：(会话标题, 修改时间)；没有记录时为 ("", 0.0)。界面用它帮人认出账号。"""
    best, best_t = "", 0.0
    for fp in _list_local_json(ws_path) if ws_path else []:
        try:
            t = os.path.getmtime(fp)
        except OSError:
            continue
        if t > best_t:
            best, best_t = fp, t
    return (_read_meta(best, cache=True).title, best_t) if best else ("", 0.0)


def _newer(src: _Meta, dst: _Meta) -> tuple[bool, str]:
    """源记录是否比目标新：两边都有 lastActivityAt 就比它，否则比文件修改时间。"""
    if src.activity is not None and dst.activity is not None:
        a, b, what = src.activity / 1000, dst.activity / 1000, "最后活动时间"
    else:
        a, b, what = src.mtime, dst.mtime, "文件修改时间"
    return a > b, f"源的{what}更新（{_fmt_ts(a)} > {_fmt_ts(b)}）"


class _TargetIndex:
    """目标 workspace 的现状（已有记录、cliSessionId、墓碑），以及本次已计划写入的会话（跨源去重）。"""

    def __init__(self, ws: str, cache: bool = False) -> None:
        self.by_name: dict[str, _Meta] = {}
        self.clis: set[str] = set()
        self.tombstones: set[str] = set()
        self.planned: set[str] = set()      # 本次已计划拷入 / 更新的文件名和 cliSessionId
        self.warnings: list[str] = []
        try:
            entries = list(os.scandir(ws))
        except OSError:
            entries = []
        for e in entries:
            if not e.is_file():
                continue
            if e.name.startswith(DELETED_PREFIX):
                self.tombstones.add(_strip_prefix(e.name[len(DELETED_PREFIX):], LOCAL_PREFIX))
            elif e.name.startswith(LOCAL_PREFIX) and e.name.endswith(LOCAL_SUFFIX):
                m = _read_meta(e.path, cache)
                if not m.valid:
                    self.warnings.append(f"目标里的 {m.name} 读取失败（{m.error}），只按文件名判重。")
                self.by_name[m.name] = m
                if m.cli:
                    self.clis.add(m.cli)

    def classify(self, m: _Meta, update_newer: bool = True) -> tuple[str, str]:
        """返回 (动作, 原因)。动作：copy / update / exist / deleted / invalid。"""
        if not m.valid:
            return "invalid", f"不是带 sessionId 的会话记录（{m.error}），不拷贝"
        if m.ids & self.tombstones:
            return "deleted", "目标里删过这个会话（deleted_ 墓碑）"
        if m.name in self.planned or (m.cli and m.cli in self.planned):
            return "exist", "与排在前面的源重复"
        old = self.by_name.get(m.name)
        if old is not None:
            if update_newer:
                newer, why = _newer(m, old)
                if newer:
                    return "update", why
                return "exist", "目标已有同名记录，且不比源旧"
            return "exist", "目标已有同名记录（未开启更新）"
        if m.cli and m.cli in self.clis:
            return "exist", "目标已有同 cliSessionId 的记录"
        return "copy", "目标里没有"

    def add(self, m: _Meta) -> None:
        self.planned.add(m.name)
        if m.cli:
            self.planned.add(m.cli)


# --------------------------------------------------------------------------- #
# 目标 / 源选择 + 计划
# --------------------------------------------------------------------------- #
class MigrationError(Exception):
    """迁移前的可预期错误（探测不到账号、源目标相同等），界面据此提示用户。"""


def choose_target(accounts: list[Account], target_prefix: str = "") -> Account:
    if not accounts:
        raise MigrationError("没有探测到任何账号目录。")
    if target_prefix:
        for a in accounts:
            if a.name.startswith(target_prefix):
                return a
        raise MigrationError(f"指定的目标账号前缀 “{target_prefix}” 没找到。")
    # 默认：桌面端记录的最近登录账号；读不到时取 mtime 最新的（= 刚登录进去的新号）
    for a in accounts:
        if a.last_login:
            return a
    return accounts[0]


def choose_sources(
    accounts: list[Account],
    target: Account,
    source_prefix: str = "",
    merge_all: bool = False,
) -> list[Account]:
    others = [a for a in accounts if a.name != target.name]
    others.sort(key=lambda a: a.sessions, reverse=True)
    if source_prefix:
        picked = [a for a in others if a.name.startswith(source_prefix)][:1]
    elif merge_all:
        picked = others
    else:
        picked = others[:1]
    # 只保留有 workspace 且有 session 的源
    picked = [a for a in picked if a.ws_path and a.sessions > 0]
    return picked


def build_plan(
    accounts: list[Account],
    target_prefix: str = "",
    source_prefix: str = "",
    merge_all: bool = False,
    all_workspaces: bool = False,
    update_newer: bool = True,
    target_account: Optional[Account] = None,
    source_accounts: Optional[list[Account]] = None,
    cache: bool = False,
) -> MigrationPlan:
    """根据选项构造迁移计划（只读，不动任何文件）。异常抛 MigrationError。

    判重规则：命中目标的 deleted_ 墓碑（源会话的 local id、cliSessionId、priorCliSessionIds
    任一）→ 跳过；目标已有同名记录 → 源更新且开启 update_newer 时列为「更新」，否则跳过；
    目标已有同 cliSessionId → 跳过；多个源之间去重（排在前面、session 多的源优先）；
    不是带 sessionId 的 JSON → 记为无效、不拷。
    target_account / source_accounts 给出时直接使用（命令行可指定具体的组织目录）。
    cache=True 复用按 (修改时间, 大小) 缓存的解析结果（界面反复预览用；执行时 apply_migration 会重新读盘复核）。
    """
    if target_account is None and len(accounts) < 2:
        raise MigrationError(
            f"只探测到 {len(accounts)} 个账号目录，无需迁移。\n"
            "（换号后，用新账号打开一次 Code 标签页，新账号目录才会出现。）"
        )
    target = target_account or choose_target(accounts, target_prefix)
    if source_accounts is not None:
        sources = [a for a in source_accounts if a.ws_path]
    else:
        sources = choose_sources(accounts, target, source_prefix, merge_all)
    if not sources:
        raise MigrationError("找不到可迁移的源账号（旧号没有 session？或与目标是同一个）。")
    if not target.ws_path:
        raise MigrationError(
            "目标账号还没有 workspace 目录。\n"
            "请先用新账号打开一次 Code 标签页生成目录后再迁移。"
        )
    if any(os.path.normcase(s.ws_path) == os.path.normcase(target.ws_path) for s in sources):
        raise MigrationError("源和目标是同一个目录。请改用「手动指定」分别选择。")
    for ws in [target.ws_path] + [s.ws_path for s in sources]:
        _check_ws_layout(ws)

    index = _TargetIndex(target.ws_path, cache)
    items: list[SourcePlan] = []
    for src in sources:
        # 默认只取主 workspace；勾选「全部 workspace」则合并该账号所有 workspace（按文件名去重）
        if all_workspaces:
            seen: dict[str, str] = {}
            for ws in src.all_workspaces:
                for fp in _list_local_json(ws.path):
                    seen.setdefault(os.path.basename(fp), fp)
            files = [seen[k] for k in sorted(seen)]
        else:
            files = _list_local_json(src.ws_path)
        item = SourcePlan(account=src.name, ws_name=src.ws_name, files=[], copy=0, skip=0)
        for fp in files:
            m = _read_meta(fp, cache)
            action, reason = index.classify(m, update_newer)
            item.decisions.append((action, fp, reason))
            if action in ("copy", "update"):
                item.files.append(fp)
                index.add(m)
            if action == "copy":
                item.copy += 1
            elif action == "update":
                item.update += 1
            elif action == "exist":
                item.skip += 1
            elif action == "deleted":
                item.skip_deleted += 1
            else:
                item.invalid += 1
        items.append(item)
    total_records = sum(len(i.decisions) for i in items)
    total_invalid = sum(i.invalid for i in items)
    if total_records and total_invalid == total_records:
        raise MigrationError(
            f"源里的 {total_records} 条记录都解析不出 sessionId，可能桌面端改了存储格式。\n为安全起见未做任何改动。"
        )
    return MigrationPlan(
        target=target,
        sources=sources,
        items=items,
        total_copy=sum(i.copy for i in items),
        total_skip=sum(i.skip for i in items),
        all_workspaces=all_workspaces,
        total_deleted=sum(i.skip_deleted for i in items),
        warnings=list(index.warnings),
        total_update=sum(i.update for i in items),
        total_invalid=total_invalid,
        update_newer=update_newer,
    )


def describe_plan(plan: MigrationPlan) -> list[str]:
    """计划的文字说明（界面预览 / 确认框与命令行共用）。"""
    t = plan.target
    lines = [f"目标(新号): [{t.profile}] {t.short}\\{t.ws_name[:8]}…  (已有 {t.sessions} 个)"]
    for src, it in zip(plan.sources, plan.items):
        lines.append(f"  源: [{src.profile}] {src.short}\\{src.ws_name[:8]}…  → 新拷 {it.copy} / 更新 {it.update}"
                     f" / 已存在 {it.skip} / 目标已删除 {it.skip_deleted} / 无效 {it.invalid}")
    lines.append(f"合计：新拷 {plan.total_copy} 个；更新 {plan.total_update} 个（源比目标新）；"
                 f"已存在跳过 {plan.total_skip} 个；目标已删除跳过 {plan.total_deleted} 个；无效 {plan.total_invalid} 个。")
    if plan.all_workspaces:
        lines.append("（已启用：合并源账号全部 workspace）")
    if not plan.update_newer:
        lines.append("（未开启「源更新时覆盖」：目标已有的记录一律保留）")
    if plan.warnings:
        lines.append(f"（目标里有 {len(plan.warnings)} 个记录读取失败，只按文件名判重）")
    return lines


# --------------------------------------------------------------------------- #
# 执行 / 撤销 / 回滚 / 备份管理
# --------------------------------------------------------------------------- #
@dataclass
class ApplyResult:
    copied: int
    skipped: int
    backup_path: str                # 没有文件要写时为空串（此时不建备份）
    now_total: int
    skipped_deleted: int = 0
    manifest_path: str = ""
    updated: int = 0
    invalid: int = 0
    log_path: str = ""


_LOG_ACTION = {"copy": "COPY", "update": "UPDATE", "exist": "SKIP", "deleted": "SKIP", "invalid": "INVALID"}


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_json_atomic(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _write_log(path: str, lines: list[str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _tree_stats(path: str) -> tuple[int, int]:
    """目录树里的文件数和总字节数（校验备份用）。"""
    n = size = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            n += 1
            size += os.path.getsize(os.path.join(root, name))
    return n, size


def _unique_path(path: str) -> str:
    """同一秒内重复操作时目录名会撞车：加 -2、-3… 后缀。"""
    candidate, n = path, 2
    while os.path.exists(candidate):
        candidate, n = f"{path}-{n}", n + 1
    return candidate


def _is_record_name(name) -> bool:
    return (isinstance(name, str) and os.path.basename(name) == name
            and name.startswith(LOCAL_PREFIX) and name.endswith(LOCAL_SUFFIX))


def apply_migration(plan: MigrationPlan, log=None) -> ApplyResult:
    """
    执行迁移：先按磁盘现状复核计划；确有文件要写，才整目录备份目标 workspace 并校验备份
    （文件数 + 总字节数，不一致就停下、什么都不拷）；再逐个「先写临时文件、再 os.replace」
    拷入或更新；最后在备份目录里写拷入清单（文件名 + sha256，更新的还记旧版本的 sha256）
    和逐个文件的日志。
    """
    def emit(msg: str) -> None:
        if log:
            log(msg)

    target = plan.target
    ws = target.ws_path
    if not ws or not os.path.isdir(ws):
        raise MigrationError("目标 workspace 目录不存在，无法迁移。")
    _check_ws_layout(ws)

    counts = {"copy": 0, "update": 0, "exist": 0, "deleted": 0, "invalid": 0}
    lines: list[str] = []
    for item in plan.items:
        for action, fp, reason in item.decisions:
            if action not in ("copy", "update"):
                counts[action] += 1
                lines.append(f"{_LOG_ACTION[action]}\t{os.path.basename(fp)}\t{reason}")
    # 以磁盘现状重新判定，避免与预览时刻的偏差（这期间桌面端可能新建或删除了会话）
    index = _TargetIndex(ws)
    todo: list[tuple[str, _Meta, str, str]] = []
    for item in plan.items:
        for fp in item.files:
            m = _read_meta(fp)
            action, reason = index.classify(m, plan.update_newer)
            if action in ("copy", "update"):
                todo.append((fp, m, action, reason))
                index.add(m)
            else:
                counts[action] += 1
                lines.append(f"{_LOG_ACTION[action]}\t{m.name}\t{reason}（执行前复核）")

    def result(backup: str = "", log_path: str = "", manifest_path: str = "") -> ApplyResult:
        return ApplyResult(copied=counts["copy"], skipped=counts["exist"], backup_path=backup,
                           now_total=_count_local_json(ws), skipped_deleted=counts["deleted"],
                           manifest_path=manifest_path, updated=counts["update"],
                           invalid=counts["invalid"], log_path=log_path)

    if not todo:
        emit(f"没有需要拷贝或更新的文件（已存在 {counts['exist']}，目标已删除 {counts['deleted']}，"
             f"无效 {counts['invalid']}），未创建备份。")
        return result()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = _unique_path(ws + BACKUP_MARKER + stamp)
    log_path = os.path.join(backup, LOG_NAME)
    manifest_path = os.path.join(backup, MANIFEST_NAME)
    header = [f"# Claude 会话迁移 {stamp}", f"# 目标 {ws}"] + [f"# 源 {s.ws_path}" for s in plan.sources]
    header.append("# 动作\t文件名\t原因")
    manifest = {"version": 2, "created": stamp, "workspace": ws, "files": [], "undone": ""}
    emit(f"备份目标 workspace → {backup}")
    shutil.copytree(ws, backup)
    verified = False
    try:
        n_ws, size_ws = _tree_stats(ws)
        n_bk, size_bk = _tree_stats(backup)
        if (n_ws, size_ws) != (n_bk, size_bk):
            msg = (f"备份校验失败：原目录 {n_ws} 个文件 / {size_ws} 字节，备份 {n_bk} 个 / {size_bk} 字节。"
                   "为安全起见什么都没拷。")
            lines.append(f"STOP\t-\t{msg}")
            raise MigrationError(f"{msg}\n备份目录：{backup}")
        verified = True
        emit(f"备份已校验：{n_bk} 个文件，{size_bk} 字节。")
        for fp, m, action, reason in todo:
            dst = os.path.join(ws, m.name)
            entry = {"name": m.name, "action": action, "source": fp}
            if action == "copy" and os.path.exists(dst):
                counts["exist"] += 1
                lines.append(f"SKIP\t{m.name}\t执行时目标里已出现同名文件")
                continue
            if action == "update":
                old = os.path.join(backup, m.name)
                old_sha = _sha256(old) if os.path.isfile(old) else ""
                if not old_sha or not os.path.isfile(dst) or _sha256(dst) != old_sha:
                    counts["exist"] += 1
                    lines.append(f"SKIP\t{m.name}\t目标文件在备份之后变了，未更新")
                    continue
                entry["old_sha256"] = old_sha
                entry["old_backup"] = m.name   # 旧版本就在本备份目录里
            tmp = os.path.join(ws, "." + m.name + ".csm-tmp")  # 不以 local_ 开头，桌面端不会当成会话
            try:
                shutil.copy2(fp, tmp)
                entry["sha256"] = _sha256(tmp)
                os.replace(tmp, dst)
            except BaseException:
                if os.path.exists(tmp):
                    os.remove(tmp)
                raise
            manifest["files"].append(entry)
            counts[action] += 1
            lines.append(f"{_LOG_ACTION[action]}\t{m.name}\t{reason}")
    except Exception as e:
        if not isinstance(e, MigrationError):
            lines.append(f"FAIL\t-\t{type(e).__name__}: {e}")
        raise
    finally:
        if verified:
            _write_json_atomic(manifest_path, manifest)  # 中途出错也记下已写入的部分，撤销时用
        _write_log(log_path, header + lines)
    res = result(backup, log_path, manifest_path)
    emit(f"完成：新拷 {res.copied}，更新 {res.updated}，已存在跳过 {res.skipped}，"
         f"目标已删除跳过 {res.skipped_deleted}，无效 {res.invalid}。新号现共 {res.now_total} 个 session。")
    emit(f"日志 → {log_path}")
    return res


@dataclass
class BackupInfo:
    backup_path: str            # 目录本身
    workspace_path: str         # 它对应的 workspace 目录
    account: str
    stamp: str
    files: int                  # 目录里的 local_*.json 个数
    kind: str = "backup"        # 'backup' 迁移前备份 | 'replaced' 整目录换回时移开的 | 'undone' 撤销时移出的
    manifest_files: int = -1    # 备份里拷入清单记录的文件数；-1 = 没有清单（旧版本 / 上游脚本的备份）
    undone: str = ""            # 已按清单撤销过的时间

    @property
    def has_manifest(self) -> bool:
        return self.manifest_files >= 0


_DIR_KINDS = ((BACKUP_MARKER, "backup"), (REPLACED_MARKER, "replaced"), (UNDONE_MARKER, "undone"))


def read_manifest(backup_path: str) -> Optional[dict]:
    """读备份目录里的拷入清单；没有或损坏时返回 None。"""
    try:
        with open(os.path.join(backup_path, MANIFEST_NAME), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        return None
    return data


def list_backups(base: str) -> list[BackupInfo]:
    """扫描 base 下各账号目录里的备份 / 被替换 / 撤销移出目录，按时间倒序。"""
    out: list[BackupInfo] = []
    if not os.path.isdir(base):
        return out
    for acct in os.scandir(base):
        if not acct.is_dir():
            continue
        try:
            entries = list(os.scandir(acct.path))
        except OSError:
            continue
        for e in entries:
            if not e.is_dir():
                continue
            for marker, kind in _DIR_KINDS:
                if marker in e.name:
                    break
            else:
                continue
            idx = e.name.index(marker)
            info = BackupInfo(
                backup_path=e.path,
                workspace_path=os.path.join(acct.path, e.name[:idx]),
                account=acct.name,
                stamp=e.name[idx + len(marker):],
                files=_count_local_json(e.path),
                kind=kind,
            )
            if kind == "backup":
                mf = read_manifest(e.path)
                if mf is not None:
                    info.manifest_files = len(mf["files"])
                    info.undone = str(mf.get("undone") or "")
            out.append(info)
    out.sort(key=lambda b: b.stamp, reverse=True)
    return out


def list_backups_multi(bases: list[str]) -> list[BackupInfo]:
    """聚合多个 base 下的备份，按时间倒序。"""
    out: list[BackupInfo] = []
    for b in bases:
        out.extend(list_backups(b))
    out.sort(key=lambda b: b.stamp, reverse=True)
    return out


@dataclass
class UndoResult:
    moved: int                  # 本次拷入、内容未变 → 移到撤销目录
    undo_dir: str               # 没移动任何文件时为空串
    modified: list[str]         # 写入后被改过（sha256 不符）→ 留在原处
    missing: list[str]          # 已不在 workspace（之前被删或撤销过）
    restored: int = 0           # 本次更新过、内容未变 → 从备份恢复迁移前的旧版本
    broken: list[str] = field(default_factory=list)   # 备份里的旧版本缺失或不符 → 当前文件留在原处
    log_path: str = ""


def undo_migration(backup: BackupInfo, log=None, dry_run: bool = False) -> UndoResult:
    """
    按拷入清单撤销一次迁移，只处理清单里「内容仍是本次写入的」文件：
      - 新拷入的 → 移到 <workspace>__UNDONE_MIGRATION_<时间>（移走而非删除）；
      - 更新过的 → 从本备份恢复迁移前的旧版本，本次写入的版本移到上述撤销目录。
    写入后被桌面端改过的文件留在原处并报告；workspace 里其它文件一概不动。
    dry_run=True 只算出会做什么，不改任何文件。
    """
    def emit(msg: str) -> None:
        if log:
            log(msg)

    manifest = read_manifest(backup.backup_path)
    if manifest is None:
        raise MigrationError("这个备份没有拷入清单（旧版本或上游脚本留下的），无法按清单撤销。")
    ws = backup.workspace_path
    if not os.path.isdir(ws):
        raise MigrationError(f"workspace 目录不存在：{ws}")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    undo_dir = _unique_path(ws + UNDONE_MARKER + stamp)
    res = UndoResult(moved=0, undo_dir="", modified=[], missing=[])
    lines = [f"# 撤销迁移 {stamp}（备份 {backup.backup_path}）", "# 动作\t文件名\t原因"]
    for entry in manifest["files"]:
        name = entry.get("name") if isinstance(entry, dict) else None
        digest = entry.get("sha256") if isinstance(entry, dict) else None
        # 清单只认 workspace 里的 local_*.json 文件名，防止被改坏的清单指向别处
        if not (_is_record_name(name) and isinstance(digest, str)):
            continue
        dst = os.path.join(ws, name)
        if not os.path.isfile(dst):
            res.missing.append(name)
            lines.append(f"MISSING\t{name}\t已不在 workspace（之前被删除或撤销过）")
            continue
        if _sha256(dst) != digest:
            res.modified.append(name)
            lines.append(f"KEEP\t{name}\t写入后被改过，保留原处")
            continue
        if entry.get("action") == "update":
            old_name = entry.get("old_backup")
            old = os.path.join(backup.backup_path, old_name) if _is_record_name(old_name) else ""
            if not old or not os.path.isfile(old) or _sha256(old) != entry.get("old_sha256"):
                res.broken.append(name)
                lines.append(f"KEEP\t{name}\t备份里的旧版本缺失或不符，保留当前文件")
                continue
            if not dry_run:
                tmp = os.path.join(ws, "." + name + ".csm-tmp")
                shutil.copy2(old, tmp)
                os.makedirs(undo_dir, exist_ok=True)
                os.replace(dst, os.path.join(undo_dir, name))
                os.replace(tmp, dst)
            res.restored += 1
            lines.append(f"RESTORE\t{name}\t恢复迁移前的旧版本（本次写入的版本移到撤销目录）")
        else:
            if not dry_run:
                os.makedirs(undo_dir, exist_ok=True)
                os.replace(dst, os.path.join(undo_dir, name))
            res.moved += 1
            lines.append(f"MOVE\t{name}\t本次拷入、内容未变，移出侧栏")
    prefix = "撤销预览" if dry_run else "撤销"
    emit(f"{prefix}：移走 {res.moved} 个，恢复旧版本 {res.restored} 个，写入后被改过而保留 {len(res.modified)} 个，"
         f"旧版本缺失 {len(res.broken)} 个，已不在 {len(res.missing)} 个。")
    if dry_run:
        return res
    if res.moved or res.restored:
        res.undo_dir = undo_dir
        emit(f"移走的文件在 → {undo_dir}")
    manifest["undone"] = stamp
    manifest["undo_dir"] = res.undo_dir
    _write_json_atomic(os.path.join(backup.backup_path, MANIFEST_NAME), manifest)
    res.log_path = _unique_path(os.path.join(backup.backup_path, f"undo-{stamp}.log"))
    _write_log(res.log_path, lines)
    emit(f"日志 → {res.log_path}")
    return res


def rollback_impact(backup: BackupInfo) -> tuple[int, int, int]:
    """整目录换回的影响：(将被移出侧栏的会话数, 当前会话数, 备份里的会话数)。"""
    cur = {os.path.basename(p) for p in _list_local_json(backup.workspace_path)}
    old = {os.path.basename(p) for p in _list_local_json(backup.backup_path)}
    return len(cur - old), len(cur), len(old)


def rollback(backup: BackupInfo, log=None) -> str:
    """
    高级：整目录换回。把当前 workspace 改名让位，再把备份改名顶上。
    不做递归删除——被替换下来的目录改名为 *__REPLACED_<stamp> 保留，用户可事后手动删。
    注意：备份之后新增的会话会随当前目录一起移出侧栏（界面确认框会写明个数）。
    返回被移开的旧目录路径。
    """
    def emit(msg: str) -> None:
        if log:
            log(msg)

    ws = backup.workspace_path
    bk = backup.backup_path
    if BACKUP_MARKER not in os.path.basename(bk.rstrip("\\/")):
        raise MigrationError(f"只能用「迁移前备份」整目录换回：{bk}")
    if not os.path.isdir(bk):
        raise MigrationError(f"备份目录不存在：{bk}")
    lost = sorted({os.path.basename(p) for p in _list_local_json(ws)}
                  - {os.path.basename(p) for p in _list_local_json(bk)})
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    replaced = _unique_path(ws + REPLACED_MARKER + stamp)
    if os.path.isdir(ws):
        emit(f"当前 workspace 移开 → {replaced}")
        os.rename(ws, replaced)
    else:
        replaced = ""
    emit(f"备份还原到位 → {ws}")
    os.rename(bk, ws)
    if replaced:
        # 备份里本工具自己的清单和日志不属于 workspace，随被移开的目录一起放（仍可在界面里打开）
        for n in os.listdir(ws):
            if n in (MANIFEST_NAME, LOG_NAME) or (n.startswith("undo-") and ".log" in n):
                os.replace(os.path.join(ws, n), os.path.join(replaced, "from-backup-" + n))
        _write_log(os.path.join(replaced, "rollback.log"),
                   [f"# 整目录换回 {stamp}：{bk} → {ws}", "# 动作\t文件名\t原因"]
                   + [f"MOVE\t{n}\t换回后不在侧栏，留在本目录" for n in lost])
    emit("整目录换回完成。对话原文 .jsonl 全程未动。")
    return replaced


def _is_safe_removable(path: str, base: str) -> bool:
    """只允许删除 session 根目录下、名字带备份 / 替换 / 撤销标记的单个目录。"""
    try:
        real = os.path.realpath(path)
        base_real = os.path.realpath(base)
    except OSError:
        return False
    if not os.path.isdir(real):
        return False
    if os.path.commonpath([real, base_real]) != base_real:
        return False
    name = os.path.basename(real.rstrip("\\/"))
    return any(marker in name for marker, _ in _DIR_KINDS)


def delete_backup_dir(path: str, base: Optional[str] = None, bases: Optional[list[str]] = None) -> None:
    """删除单个备份 / 替换 / 撤销目录（界面上用户显式确认后调用；带路径与命名双重校验）。

    bases 提供多 profile 根目录列表时，目标只需落在其中任意一个之内即可。
    """
    if bases is None:
        bases = [base] if base else get_session_bases()
    if not any(_is_safe_removable(path, b) for b in bases):
        raise MigrationError(f"拒绝删除：{path}\n只允许删除 session 目录下的备份 / 替换 / 撤销目录。")
    shutil.rmtree(path)


# --------------------------------------------------------------------------- #
# Claude 桌面端进程控制（仅 Windows 完整支持；其它平台给出手动提示）
# --------------------------------------------------------------------------- #
# 进程名同样是 claude.exe 的还有 Claude Code 命令行：桌面端内置的
# %APPDATA%\Claude\claude-code\<版本>\...\claude.exe，以及用户自己装的
# ~\.local\bin\claude.exe、npm 全局安装等。这里只认桌面端本体，其余一律不碰。
_CLI_PATH_MARKERS = ("\\claude-code\\", "\\.local\\bin\\", "\\npm\\", "\\node_modules\\")
_MSIX_EXE_RE = re.compile(r"\\windowsapps\\claude_[^\\]+\\app\\claude\.exe$")
_INSTALLED_DIRS = ("anthropicclaude", "programs\\claude")   # 非商店版：%LOCALAPPDATA% 下的安装目录
_AUMID_RE = re.compile(r"^[\w.\-]+![\w.\-]+$")


def is_desktop_exe(path: Optional[str], localappdata: Optional[str] = None) -> bool:
    """判断进程路径是不是 Claude 桌面端本体（商店版 MSIX，或 %LOCALAPPDATA% 下的安装版）。"""
    if not path:
        return False
    p = path.replace("/", "\\").lower()
    if not p.endswith("\\claude.exe") or any(m in p for m in _CLI_PATH_MARKERS):
        return False
    if _MSIX_EXE_RE.search(p):
        return True
    lad = localappdata if localappdata is not None else os.environ.get("LOCALAPPDATA", "")
    lad = lad.replace("/", "\\").lower().rstrip("\\")
    return bool(lad) and any(p.startswith(f"{lad}\\{d}\\") for d in _INSTALLED_DIRS)


def _run_powershell(script: str, timeout: int = 20) -> tuple[int, str, str]:
    exe = "powershell.exe"
    try:
        cp = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return cp.returncode, (cp.stdout or "").strip(), (cp.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", "powershell.exe 未找到"
    except subprocess.TimeoutExpired:
        return 124, "", "PowerShell 执行超时"


def _ps_quote(s: str) -> str:
    """PowerShell 单引号字面量：内容原样，$ 不会被展开；单引号本身写两遍。"""
    return "'" + s.replace("'", "''") + "'"


@dataclass
class ClaudeStatus:
    running: bool
    pids: list[int]               # 只含确认是桌面端的进程
    relaunch_kind: str            # 'aumid' | 'path' | 'none'
    relaunch_value: Optional[str]
    proc_path: Optional[str]
    exe_paths: list[str] = field(default_factory=list)  # 桌面端进程的可执行文件（结束前复核用）
    ignored: int = 0              # 同名但确认不是桌面端的 claude.exe（Claude Code 命令行等），不会被结束
    error: str = ""               # 探测失败的原因（此时 running 不可信）
    unknown: int = 0              # 名为 Claude 但读不到路径的进程：迁移前按「可能是桌面端」对待，但不会被结束


# 只读探测脚本：每行一条记录，制表符分隔，避免 PowerShell 5.1 ConvertTo-Json 的数组怪癖
#   P <pid> <exe 路径>                         名为 Claude 的进程（路径可能为空）
#   A <PackageFamilyName> <安装目录> <AppId> <Executable>   商店版包清单里的 Application
#   S <AppID>                                  开始菜单条目（取不到包清单时兜底）
_STATUS_SCRIPT = r"""
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
Get-Process -Name Claude -ErrorAction SilentlyContinue | ForEach-Object { "P`t$($_.Id)`t$($_.Path)" }
$found = $false
try {
  foreach ($pkg in @(Get-AppxPackage -Name Claude -ErrorAction Stop)) {
    $apps = @((Get-AppxPackageManifest -Package $pkg.PackageFullName).Package.Applications.Application)
    foreach ($app in $apps) {
      "A`t$($pkg.PackageFamilyName)`t$($pkg.InstallLocation)`t$($app.Id)`t$($app.Executable)"
      $found = $true
    }
  }
} catch {}
if (-not $found) {
  try { Get-StartApps | Where-Object { $_.AppID -like '*!*' } | ForEach-Object { "S`t$($_.AppID)" } } catch {}
}
"""


def _msix_aumid(exe: str, apps: list[tuple[str, str, str, str]], start: list[str]) -> Optional[str]:
    """商店版：取包清单里 Executable 与进程路径一致的 Application，拼 AUMID = PackageFamilyName!AppId。"""
    p = exe.replace("/", "\\").lower()
    for pfn, loc, app_id, app_exe in apps:
        root = loc.replace("/", "\\").lower().rstrip("\\") + "\\"
        if pfn and app_id and loc and p == root + app_exe.replace("/", "\\").lower():
            return f"{pfn}!{app_id}"
    # 兜底：由包目录名 <Name>_<版本>_<架构>__<发布者ID> 推出 PackageFamilyName，再到开始菜单条目里找
    m = re.search(r"\\windowsapps\\([^\\]+)\\", p)
    mm = re.match(r"^(.+?)_\d[^_]*_[^_]*__(.+)$", m.group(1)) if m else None
    if mm:
        prefix = f"{mm.group(1)}_{mm.group(2)}!"
        for aid in start:
            if aid.lower().startswith(prefix):
                return aid
    return None


def parse_claude_status(out: str, localappdata: Optional[str] = None) -> ClaudeStatus:
    """把探测脚本的输出整理成 ClaudeStatus（纯函数，便于自测）。"""
    procs: list[tuple[int, str]] = []
    apps: list[tuple[str, str, str, str]] = []
    start: list[str] = []
    for line in out.splitlines():
        parts = [x.strip() for x in line.split("\t")]
        if parts[0] == "P" and len(parts) >= 2 and parts[1].isdigit():
            procs.append((int(parts[1]), parts[2] if len(parts) >= 3 else ""))
        elif parts[0] == "A" and len(parts) >= 5:
            apps.append((parts[1], parts[2], parts[3], parts[4]))
        elif parts[0] == "S" and len(parts) >= 2:
            start.append(parts[1])
    desktop = [(pid, path) for pid, path in procs if is_desktop_exe(path, localappdata)]
    unknown = sum(1 for _, path in procs if not path)
    ignored = len(procs) - len(desktop) - unknown
    if not desktop:
        # 没在运行时也给出商店版的启动方式（包清单里 Executable 为 Claude.exe 的 Application），界面据此提供「打开」
        aumid = next((f"{pfn}!{app_id}" for pfn, _loc, app_id, app_exe in apps
                      if pfn and app_id and app_exe.replace("/", "\\").lower().endswith("claude.exe")), None)
        kind = "aumid" if aumid and _AUMID_RE.match(aumid) else "none"
        return ClaudeStatus(False, [], kind, aumid if kind == "aumid" else None, None, [], ignored, unknown=unknown)
    pids = sorted({pid for pid, _ in desktop})
    exe_paths = list(dict.fromkeys(path for _, path in desktop))
    exe = exe_paths[0]
    if "\\windowsapps\\" in exe.lower():
        aumid = _msix_aumid(exe, apps, start)
        kind, value = ("aumid", aumid) if aumid else ("none", None)
    else:
        kind, value = "path", exe
    return ClaudeStatus(True, pids, kind, value, exe, exe_paths, ignored, unknown=unknown)


def get_claude_status() -> ClaudeStatus:
    """只读探测 Claude 桌面端进程与重启目标（AUMID 或 exe 路径）。"""
    if platform.system() != "Windows":
        return ClaudeStatus(False, [], "none", None, None)
    rc, out, err = _run_powershell(_STATUS_SCRIPT)
    if rc != 0:
        return ClaudeStatus(False, [], "none", None, None, error=err or f"PowerShell 返回 {rc}")
    return parse_claude_status(out)


def close_claude(status: Optional[ClaudeStatus] = None, timeout: int = 15) -> tuple[bool, str]:
    """
    只结束确认是 Claude 桌面端本体的进程（不碰 Claude Code 命令行和读不到路径的进程），
    并轮询等它们真正退出。status 为调用方刚探测到的状态；不传则现场探测一次。
    返回 (是否已全部退出, 说明)。
    """
    if platform.system() != "Windows":
        return False, "非 Windows 平台请手动退出 Claude 桌面端。"
    st = status if status is not None else get_claude_status()
    if st.error:
        return False, f"无法检测 Claude 进程：{st.error}"
    note = (f"（另有 {st.ignored} 个同名进程不是桌面端，如 Claude Code 命令行，未受影响）"
            if st.ignored else "")
    if not st.pids:
        return True, "Claude 桌面端未在运行。" + note
    ids = ",".join(str(int(p)) for p in st.pids)
    allowed = ",".join(_ps_quote(p) for p in st.exe_paths)
    # 结束前按 PID + 可执行文件路径复核，防止 PID 已被别的进程复用
    script = (
        f"$ids = @({ids})\n"
        f"$allowed = @({allowed})\n"
        "function Get-Targets { @(Get-Process -Id $ids -ErrorAction SilentlyContinue |"
        " Where-Object { $allowed -contains $_.Path }) }\n"
        "$n = (Get-Targets).Count\n"
        "Get-Targets | Stop-Process -Force -ErrorAction SilentlyContinue\n"
        f"$deadline = (Get-Date).AddSeconds({int(timeout)})\n"
        "while (((Get-Targets).Count -gt 0) -and ((Get-Date) -lt $deadline)) { Start-Sleep -Milliseconds 200 }\n"
        "\"T`t$n`t$((Get-Targets).Count)\"\n"
    )
    rc, out, err = _run_powershell(script, timeout=int(timeout) + 20)
    m = re.search(r"^T\t(\d+)\t(\d+)", out, re.M)
    if rc != 0 or not m:
        return False, err or "结束进程失败。"
    stopped, left = int(m.group(1)), int(m.group(2))
    if left:
        return False, f"仍有 {left} 个 Claude 桌面端进程没有退出，请用任务管理器结束后再继续。" + note
    return True, f"已结束 {stopped} 个 Claude 桌面端进程。" + note


def relaunch_claude(kind: str, value: Optional[str]) -> tuple[bool, str]:
    """重新打开桌面端：商店版用 AUMID（explorer shell:AppsFolder\\<AUMID>），安装版直接启动 exe。
    参数直接交给系统、不经 PowerShell 拼接，路径里的 $ 和引号不会被展开。"""
    if platform.system() != "Windows":
        return False, "非 Windows 平台请手动重新打开 Claude 桌面端。"
    try:
        if kind == "aumid" and value and _AUMID_RE.match(value):
            subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + value])
        elif kind == "path" and value and os.path.isfile(value):
            os.startfile(value)  # noqa: S606
        else:
            return False, "未能解析 Claude 启动方式，请从开始菜单手动打开。"
    except OSError as e:
        return False, f"启动失败（{e}），请从开始菜单手动打开。"
    return True, "已请求重新启动 Claude 桌面端。"


# --------------------------------------------------------------------------- #
# 命令行（薄入口：复用上面同一套计划 / 执行 / 撤销逻辑；默认只预览，--apply 才写入）
# --------------------------------------------------------------------------- #
def _resolve_account(sel: str, accounts: list[Account]) -> Account:
    """--source / --target：--list 里的序号，或 accountId[\\orgId]（可写前缀）。"""
    s = sel.strip().replace("/", "\\").rstrip("\\")
    if s.isdigit():
        n = int(s)
        if 1 <= n <= len(accounts):
            return accounts[n - 1]
        raise MigrationError(f"序号 {n} 超出范围（1–{len(accounts)}），先用 --list 看一下。")
    acct_id, _, org = s.partition("\\")
    hits = [a for a in accounts if acct_id and a.name.lower().startswith(acct_id.lower())]
    if len(hits) != 1:
        raise MigrationError(f"“{sel}” 没有唯一匹配到账号；请用 --list 里的序号或完整的 accountId\\orgId。")
    a = hits[0]
    if not org:
        return a
    ws = [w for w in a.all_workspaces if w.name.lower().startswith(org.lower())]
    if len(ws) != 1:
        raise MigrationError(f"“{sel}” 没有唯一匹配到该账号下的组织目录。")
    return dataclasses.replace(a, workspace=ws[0])


def _cli_claude_closed(bases: list[str], assume_closed: bool) -> bool:
    """写入前确认桌面端已退出。只针对桌面端真实使用的目录；--base 指向别处（测试副本）时不检查。"""
    real = {os.path.normcase(os.path.abspath(b)) for b in get_session_bases()}
    if not any(os.path.normcase(os.path.abspath(b)) in real for b in bases):
        return True
    if platform.system() != "Windows":
        if not assume_closed:
            print("非 Windows 平台无法自动检测，请确认 Claude 桌面端已完全退出后加 --assume-closed 再执行。")
        return assume_closed
    st = get_claude_status()
    if st.error:
        print(f"无法检测 Claude 进程：{st.error}")
        return assume_closed
    if st.running:
        print(f"Claude 桌面端正在运行（{len(st.pids)} 个进程）。请先从托盘图标彻底退出它"
              "（或在图形界面里让本工具代为关闭），再执行。")
        return False
    if st.unknown and not assume_closed:
        print(f"有 {st.unknown} 个名为 Claude 的进程读不到路径，可能是桌面端。"
              "确认桌面端已完全退出后，加 --assume-closed 再执行。")
        return False
    return True


def cli_main(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="core.py",
        description="Claude 会话迁移（命令行）。默认只预览、不改任何文件；加 --apply 才执行。",
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="列出账号（序号、账号\\组织、会话数、最近登录等标记）")
    mode.add_argument("--plan", action="store_true", help="预览迁移计划（默认动作）")
    mode.add_argument("--undo", action="store_true", help="列出备份 / 预览按清单撤销；加 --backup 和 --apply 执行")
    ap.add_argument("--apply", action="store_true", help="真正执行迁移或撤销")
    ap.add_argument("--source", action="append", default=[],
                    help="源：--list 里的序号或 accountId[\\orgId]；可重复；省略 = 合并所有旧账号")
    ap.add_argument("--target", help="目标：序号或 accountId[\\orgId]；省略 = 最近登录的账号（读不到时取最近活动）")
    ap.add_argument("--backup", help="--undo 用：备份序号（见 --undo 的列表）或备份目录路径")
    ap.add_argument("--all-workspaces", action="store_true", help="合并源账号的全部组织目录（默认只取主目录）")
    ap.add_argument("--no-update", action="store_true", help="源记录更新时也不覆盖目标里已有的记录")
    ap.add_argument("--assume-closed", action="store_true",
                    help="确认桌面端已退出：忽略读不到路径的 Claude 进程（确认是桌面端的仍会拒绝）")
    ap.add_argument("--base", action="append", default=[],
                    help="改用指定的 claude-code-sessions 目录（测试副本用，可重复）")
    ap.add_argument("-v", "--verbose", action="store_true", help="逐个文件列出计划")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass
    bases = args.base or get_session_bases()
    try:
        accounts = list_accounts_multi(bases)
        if args.list:
            if args.apply:
                ap.error("--list 不能和 --apply 一起用")
            for note in layout_notes(bases):
                print(note)
            if not accounts:
                print("没有探测到账号目录。")
                return 0
            default = choose_target(accounts)
            for i, a in enumerate(accounts, 1):
                marks = "；".join(x for x in (a.marks, "默认目标" if a is default else "") if x)
                print(f"[{i}] [{a.profile}] {a.sessions:4d} 个  最近活动 {a.mtime_str}  {a.name}\\{a.ws_name}"
                      + (f"   <- {marks}" if marks else ""))
                for w in a.all_workspaces:
                    if w is not a.workspace:
                        print(f"      其它组织目录：{a.name}\\{w.name}（{w.sessions} 个）")
            return 0

        if args.undo:
            backups = [b for b in list_backups_multi(bases) if b.kind == "backup"]
            if not args.backup:
                if not backups:
                    print("没有找到迁移前备份。")
                for i, b in enumerate(backups, 1):
                    state = ("无清单（只能在图形界面里整目录换回）" if not b.has_manifest
                             else f"已撤销 {b.undone}" if b.undone else f"可撤销，清单 {b.manifest_files} 个")
                    print(f"[{i}] {b.stamp}  {state}  {b.backup_path}")
                print("用 --undo --backup <序号> 预览撤销；确认后再加 --apply 执行。")
                return 0
            sel = args.backup.strip()
            if sel.isdigit() and 1 <= int(sel) <= len(backups):
                b = backups[int(sel) - 1]
            else:
                hits = [x for x in backups if os.path.normcase(x.backup_path) == os.path.normcase(os.path.abspath(sel))]
                if not hits:
                    raise MigrationError(f"没找到备份：{sel}")
                b = hits[0]
            if args.apply and not _cli_claude_closed(bases, args.assume_closed):
                return 2
            undo_migration(b, log=print, dry_run=not args.apply)
            if not args.apply:
                print("预览结束：没有改动任何文件。确认无误后加 --apply 执行。")
            return 0

        target = _resolve_account(args.target, accounts) if args.target else None
        sources = [_resolve_account(s, accounts) for s in args.source] or None
        plan = build_plan(accounts, merge_all=True, all_workspaces=args.all_workspaces,
                          update_newer=not args.no_update, target_account=target, source_accounts=sources)
        for line in describe_plan(plan):
            print(line)
        for w in plan.warnings:
            print(f"[警告] {w}")
        if args.verbose:
            for it in plan.items:
                for action, fp, reason in it.decisions:
                    print(f"  {_LOG_ACTION[action]:8s} {os.path.basename(fp)}  （{reason}）")
        if not args.apply:
            print("预览结束：没有改动任何文件。确认无误后加 --apply 执行。")
            return 0
        if plan.total_copy + plan.total_update == 0:
            print("没有需要拷贝或更新的记录，未做任何改动。")
            return 0
        if not _cli_claude_closed(bases, args.assume_closed):
            return 2
        res = apply_migration(plan, log=print)
        if res.backup_path:
            print(f"备份：{res.backup_path}")
            print(f"撤销：python core.py --undo --backup \"{res.backup_path}\" --apply")
        print("下一步：打开 Claude 桌面端、登录目标账号、打开 Code 标签页，点开一个旧会话确认能续上。")
        return 0
    except MigrationError as e:
        print(f"错误：{e}")
        return 1


if __name__ == "__main__":
    sys.exit(cli_main())
