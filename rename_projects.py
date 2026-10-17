"""
rename_projects.py — 把项目目录改名（如中文名改英文名），并把 Claude Code / Claude 桌面端的
会话、记忆、侧栏和设置一起迁到新路径。

背景：Claude Code 把工作目录里每个非 [A-Za-z0-9] 字符换成 '-'，当作 ~/.claude/projects 下的
目录名。汉字数相同的项目会撞进同一个目录（D:\\Projects\\项目甲一 和 D:\\Projects\\项目乙二 都是
D--Projects-----），会话和记忆混在一起。改成英文名后每个项目各有自己的目录。

迁移内容与做法：
  - 项目目录：同盘 rename。
  - 会话（<id>.jsonl、<id>/ 子目录、<id>.* 伴随文件）：按会话第一条 cwd 归属，搬到新编码目录。
    只改结构化字段（各行 cwd、file-history 快照里的路径），对话正文不动（思考块带签名）。
  - memory/：按配置拆分或整体搬，文件里的旧路径一并替换。
  - 桌面端侧栏 local_*.json、claude_desktop_config.json（epitaxyPrefs）、git-worktrees.json。
  - ~/.claude.json 的 projects 键；~/.claude/history.jsonl 的 project 字段。
  - 项目文件里写死的旧路径（按规则挑出现行文件；历史记录不改）。

安全：
  - plan 只读。apply / rollback 前检查 Claude 进程，有就拒绝。
  - 备份：要改的文件先在同盘备份目录建硬链接（快照），改动一律"写新文件再替换"，所以旧内容
    原样留在快照里；要搬走的文件直接移进备份目录；关键小文件另真复制一份。原文件只移不删。
  - 每一步写日志 journal.jsonl；中途出错自动按日志逆序还原；事后可用 rollback 还原。

配置写在同目录的 rename_config.json（含本机项目名，不进仓库），样例见 rename_config.example.json。
改名表 rename 的键和值既可以写 root 下的名字（"旧名": "新名"，同一父目录下改名），也可以带子路径
（"Atlas": "general/Atlas"）或写带盘符的完整路径，所以能把目录搬进别的目录当子目录；要求同盘，
新路径每一级是合法 Windows 目录名、目标不存在、不在旧目录里面。本工具的目录自己也在搬家范围里时，
脚本、配置、--runs-dir 都要放在范围外，从范围外的目录用绝对路径运行（预演会检查，在里面就报错）。
text_skip_paths 可列出扫描项目文件时整段跳过的路径（目录或文件）。

用法（apply / rollback 必须在 Claude 全部退出后，从普通 PowerShell 运行）：
  python rename_projects.py plan                 只读预演，报告写到 runs\\<时间>-plan\\
  python rename_projects.py apply [--only 新名]  执行（--only 只做一个项目，用于试点）
  python rename_projects.py verify               迁移后核对
  python rename_projects.py rollback --run <runs 下的目录名>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "rename_config.json")
RUNS_DIR = os.path.join(HERE, "runs")
JOURNAL_NAME = "journal.jsonl"

ENC_RE = re.compile(r"[^A-Za-z0-9]")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
SEP = r"[\\/]{1,2}"                    # 路径分隔符：\ 或 /，JSON 转义后的 \\ 也算
STRUCT_END = r"(?=$|[\\/\x00 :])"      # 结构化字段里路径后面可跟：结尾、分隔符、\0（分支记录）、空格、冒号（"路径:模式"）
TEXT_END_CHARS = set("\\/\"'`)]>,;|*，。；：、）】」』！？ \t\r\n")
MAX_PATH = 250
BOM = b"\xef\xbb\xbf"


class MigrationError(Exception):
    pass


def encode_dir(path: str) -> str:
    """Claude Code 的项目目录名：每个非字母数字字符换成 '-'。"""
    return ENC_RE.sub("-", path)


def norm(p: str) -> str:
    return p.replace("/", "\\").rstrip("\\").casefold()


def under(p: str, root: str) -> bool:
    n, r = norm(p), norm(root)
    return n == r or n.startswith(r + "\\")


def split_parent(p: str) -> tuple[str, str]:
    p = p.rstrip("\\/")
    i = max(p.rfind("\\"), p.rfind("/"))
    return p[:i], p[i + 1:]


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def now_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def progress(msg: str):
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


# --------------------------------------------------------------------------- #
# 路径映射
# --------------------------------------------------------------------------- #
def path_parts(p: str) -> list[str]:
    return [x for x in re.split(r"[\\/]+", p) if x]


def _path_pattern(path: str) -> str:
    """完整路径的正则：各级之间允许 \\ 或 / 或 JSON 转义后的 \\\\（SEP），大小写不敏感由调用方加 re.I。"""
    return SEP.join(re.escape(x) for x in path_parts(path))


class _Rule:
    """一条「旧完整路径 → 新完整路径」。新旧不必在同一父目录。"""

    def __init__(self, old: str, new: str):
        self.old, self.new = old, new
        self.old_parts, self.new_parts = path_parts(old), path_parts(new)
        # 开头相同的各级（如 D:\AI）沿用原文的写法（大小写、分隔符），后面的用配置里的新路径
        self.common = 0
        for a, b in zip(self.old_parts, self.new_parts):
            if a.casefold() != b.casefold():
                break
            self.common += 1
        self.pattern = _path_pattern(old)

    def rewrite(self, matched: str) -> str:
        """matched 是匹配到的旧路径原文；返回新路径，分隔符照原文的写法。"""
        pieces = re.split(r"([\\/]{1,2})", matched)
        comps, seps = pieces[0::2], pieces[1::2]
        first_sep = seps[0] if seps else "\\"
        out = []
        for i, nc in enumerate(self.new_parts):
            out.append(comps[i] if i < self.common else nc)
            if i < len(self.new_parts) - 1:
                out.append(seps[i] if i < len(seps) else first_sep)
        return "".join(out)


class PathMapper:
    """结构化字段用：整个字符串以旧目录开头（后面是结尾或分隔符）才替换，保留原来的分隔符写法。"""

    def __init__(self, pairs: list[tuple[str, str]]):
        self.rules = []
        for old, new in pairs:
            r = _Rule(old, new)
            self.rules.append((re.compile("^" + r.pattern + STRUCT_END, re.I), r))

    def map(self, s: str) -> str:
        for rx, r in self.rules:
            m = rx.match(s)
            if m:
                return r.rewrite(m.group(0)) + s[m.end():]
        return s

    def hits(self, s: str) -> bool:
        return any(rx.match(s) for rx, _r in self.rules)


class TextMapper:
    """文本文件用：任意位置出现旧目录完整路径就替换；后面紧跟别的字（可能是另一个目录名）的不替换，记为待人工看。"""

    def __init__(self, pairs: list[tuple[str, str]]):
        self.rules = []
        for old, new in pairs:
            r = _Rule(old, new)
            self.rules.append((re.compile("(?<![A-Za-z0-9])" + r.pattern, re.I), r))

    def sub(self, text: str) -> tuple[str, int, list[str]]:
        count, ambiguous = 0, []
        for rx, r in self.rules:
            out, last = [], 0
            for m in rx.finditer(text):
                nxt = text[m.end()] if m.end() < len(text) else ""
                if nxt == "" or nxt in TEXT_END_CHARS:
                    out.append(text[last:m.start()] + r.rewrite(m.group(0)))
                    last = m.end()
                    count += 1
                else:
                    ambiguous.append(text[max(0, m.start() - 20): m.end() + 20].replace("\n", " "))
            out.append(text[last:])
            text = "".join(out)
        return text, count, ambiguous


_OLD_RX: dict[str, "re.Pattern"] = {}


def find_any_old(s: str, olds: list[str]) -> bool:
    """s 里有没有出现旧路径（整段，后面不紧跟字母数字连字符，zulu 不算 zulu2）。"""
    n = norm(s)
    for o in olds:
        rx = _OLD_RX.get(o)
        if rx is None:
            rx = _OLD_RX[o] = re.compile(re.escape(norm(o)) + r"(?![\w-])")
        if rx.search(n):
            return True
    return False


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    root: str
    home: str
    desktop_dir: str
    backup_roots: dict[str, str]
    rename: dict[str, str]
    retire_dirs: list[str]
    keep_sessions: dict[str, str]
    orphan_encoded_dirs: list[str]
    memory: dict[str, dict]
    drop_missing_under: list[str]
    text_scan_roots: list[str]
    text_exts: list[str]
    text_skip_dirs: list[str]
    text_skip_files: list[str]
    text_skip_suffixes: list[str]
    sandbox_root: str = ""
    sandbox_map: list[tuple[str, str]] = field(default_factory=list)
    desktop_note: str = ""
    max_path: int = MAX_PATH       # 目标路径长度上限；沙盒放在深目录里测试时可调大
    restore: list[dict] = field(default_factory=list)   # 从以前的备份区把会话复制回来，挂到指定项目下
    text_skip_paths: list[str] = field(default_factory=list)   # 项目文件扫描时整段跳过的路径（目录或文件）

    def P(self, logical: str) -> str:
        """逻辑路径（数据里写的）→ 磁盘上的路径。真实运行时两者相同，沙盒测试时按映射换。"""
        for lg, ph in self.sandbox_map:
            if under(logical, lg):
                return ph + logical[len(lg.rstrip("\\/")):]
        return logical

    @property
    def projects_dir(self) -> str:
        return os.path.join(self.P(self.home), ".claude", "projects")

    @property
    def claude_json(self) -> str:
        return os.path.join(self.P(self.home), ".claude.json")

    @property
    def history(self) -> str:
        return os.path.join(self.P(self.home), ".claude", "history.jsonl")

    @property
    def sessions_dir(self) -> str:
        return os.path.join(self.P(self.desktop_dir), "claude-code-sessions")

    def logical(self, name: str) -> str:
        """配置里的写法 → 逻辑完整路径：带盘符的当完整路径，否则是 root 下的相对路径（可带子目录）。"""
        return resolve_path(self.root, name)

    def short(self, path: str) -> str:
        """逻辑完整路径 → 项目名：在 root 下就用相对 root 的路径（Atlas、general\\Atlas），否则用完整路径。"""
        return short_name(self.root, path)


def detect_desktop_dir() -> tuple[str, str]:
    """商店版桌面端的数据实际在 %LOCALAPPDATA%\\Packages\\Claude_*\\LocalCache\\Roaming\\Claude；
    包外进程看到的 %APPDATA%\\Claude 可能是另一份。两份都在且不是同一个文件时，以商店版那份为准并提示。"""
    lad = os.environ.get("LOCALAPPDATA", "")
    cands = []
    pk = os.path.join(lad, "Packages")
    if os.path.isdir(pk):
        for n in sorted(os.listdir(pk)):
            if n.lower().startswith("claude_"):
                d = os.path.join(pk, n, "LocalCache", "Roaming", "Claude")
                if os.path.isdir(os.path.join(d, "claude-code-sessions")):
                    cands.append(d)
    roaming = os.path.join(os.environ.get("APPDATA", ""), "Claude")
    note = ""
    if cands:
        d = cands[0]
        a, b = os.path.join(d, "claude_desktop_config.json"), os.path.join(roaming, "claude_desktop_config.json")
        if os.path.exists(b) and os.path.exists(a):
            sa, sb = os.stat(a), os.stat(b)
            if (sa.st_dev, sa.st_ino) != (sb.st_dev, sb.st_ino):
                note = f"%APPDATA%\\Claude 另有一份数据（与商店版不是同一份），本次只改商店版那份：{d}"
        return d, note
    if os.path.isdir(os.path.join(roaming, "claude-code-sessions")):
        return roaming, note
    raise MigrationError("找不到桌面端数据目录（claude-code-sessions）。")


DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def resolve_path(root: str, name: str) -> str:
    name = name.strip()
    if DRIVE_RE.match(name):
        return name.replace("/", "\\").rstrip("\\")
    return root.rstrip("\\/") + "\\" + name.replace("/", "\\").strip("\\")


def short_name(root: str, path: str) -> str:
    if under(path, root) and norm(path) != norm(root):
        return path.replace("/", "\\")[len(root.rstrip("\\/")) + 1:].rstrip("\\")
    return path.replace("/", "\\").rstrip("\\")


def check_dirname(comp: str) -> Optional[str]:
    """Windows 目录名的真实限制；合法返回 None，否则返回原因。"""
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', comp):
        return '含有 < > : " / \\ | ? * 或控制字符'
    if comp in (".", "..") or comp != comp.rstrip(" ."):
        return "不能是 . / .. 或以空格、点结尾"
    if comp.split(".")[0].upper() in RESERVED_NAMES:
        return "是 Windows 保留设备名"
    if len(comp) > 255:
        return "超过 255 个字符"
    return None


def validate_rename(pairs: list[tuple[str, str]]):
    """改名 / 搬家的真实约束：同盘、新路径每一级都是合法目录名、新旧不嵌套、各条之间不冲突。
    目标是否已存在留给预演检查（那时才知道磁盘现状）。"""
    olds, news = [], []
    for old, new in pairs:
        po, pn = path_parts(old), path_parts(new)
        if not DRIVE_RE.match(old) or not DRIVE_RE.match(new):
            raise MigrationError(f"要带盘符的完整路径：{old} -> {new}")
        if po[0].casefold() != pn[0].casefold():
            raise MigrationError(f"跨盘不支持（目录改名要同盘 rename）：{old} -> {new}")
        for comp in pn[1:]:
            why = check_dirname(comp)
            if why:
                raise MigrationError(f"新路径里的目录名不合法（{why}）：{comp!r}，在 {new}")
        if norm(old) == norm(new):
            raise MigrationError(f"新旧路径相同：{old}")
        if under(new, old):
            raise MigrationError(f"新路径在旧目录里面：{old} -> {new}")
        if under(old, new):
            raise MigrationError(f"新路径是旧目录的上级：{old} -> {new}")
        olds.append(old)
        news.append(new)
    for i, o in enumerate(olds):
        for j, o2 in enumerate(olds):
            if i != j and under(o2, o):
                raise MigrationError(f"改名表里一条的旧路径在另一条里面：{o2} 在 {o} 里")
        for n in news:
            if under(n, o):
                raise MigrationError(f"某条的新路径落在另一条的旧目录里：{n} 在 {o} 里")
    if len({norm(x) for x in news}) != len(news):
        raise MigrationError("改名表里有两条新路径相同")


def load_config(path: str) -> Config:
    if not os.path.exists(path):
        raise MigrationError(f"没有配置文件：{path}（照 rename_config.example.json 写一份）")
    c = json.load(open(path, encoding="utf-8"))
    sb = c.get("sandbox") or {}
    home = c.get("home") or os.path.expanduser("~")
    note = ""
    if c.get("desktop_dir"):
        desk = c["desktop_dir"]
    else:
        desk, note = detect_desktop_dir()
    cfg = Config(
        root=c["root"], home=home, desktop_dir=desk,
        backup_roots={k.upper(): v for k, v in c["backup_roots"].items()},
        rename=c.get("rename", {}), retire_dirs=c.get("retire_dirs", []),
        keep_sessions=c.get("keep_sessions", {}), orphan_encoded_dirs=c.get("orphan_encoded_dirs", []),
        memory=c.get("memory", {}), drop_missing_under=c.get("drop_missing_under", []),
        text_scan_roots=c.get("text_scan_roots", [c["root"]]),
        text_exts=[x.lower() for x in c.get("text_exts", [])],
        text_skip_dirs=[x.casefold() for x in c.get("text_skip_dirs", [])],
        text_skip_files=[x.casefold() for x in c.get("text_skip_files", [])],
        text_skip_suffixes=[x.casefold() for x in c.get("text_skip_suffixes", [])],
        sandbox_root=sb.get("root", ""),
        sandbox_map=sorted([tuple(x) for x in sb.get("map", [])], key=lambda t: -len(t[0])),
        desktop_note=note,
        max_path=int(c.get("max_path", MAX_PATH)),
        restore=c.get("restore", []),
        text_skip_paths=c.get("text_skip_paths", []),
    )
    validate_rename([(cfg.logical(o), cfg.logical(n)) for o, n in cfg.rename.items()])
    return cfg


# --------------------------------------------------------------------------- #
# 会话记录（jsonl）的结构化字段改写
# --------------------------------------------------------------------------- #
def fix_record(o: dict, cwd_map: Callable[[str], str], path_map: Callable[[str], str]) -> bool:
    """只改结构化字段：各行 cwd、file-history 快照里的路径、attachment 里的工作目录。正文不动。"""
    ch = False
    v = o.get("cwd")
    if isinstance(v, str):
        nv = cwd_map(v)
        if nv != v:
            o["cwd"] = nv
            ch = True
    t = o.get("type")
    if t == "file-history-snapshot":
        snap = o.get("snapshot")
        tfb = snap.get("trackedFileBackups") if isinstance(snap, dict) else None
        if isinstance(tfb, dict):
            new, moved = {}, False
            for k, val in tfb.items():
                k2 = path_map(k)
                moved |= k2 != k
                if isinstance(val, dict) and isinstance(val.get("realParentDir"), str):
                    r2 = path_map(val["realParentDir"])
                    if r2 != val["realParentDir"]:
                        val["realParentDir"] = r2
                        ch = True
                new[k2] = val
            if moved:
                snap["trackedFileBackups"] = new
                ch = True
    elif t == "file-history-delta":
        if isinstance(o.get("trackingPath"), str):
            n2 = path_map(o["trackingPath"])
            if n2 != o["trackingPath"]:
                o["trackingPath"] = n2
                ch = True
        b = o.get("backup")
        if isinstance(b, dict) and isinstance(b.get("realParentDir"), str):
            n2 = path_map(b["realParentDir"])
            if n2 != b["realParentDir"]:
                b["realParentDir"] = n2
                ch = True
    elif t == "attachment":
        a = o.get("attachment")
        s = a.get("snapshot") if isinstance(a, dict) else None
        if isinstance(s, dict) and isinstance(s.get("workingDirectory"), str):
            n2 = cwd_map(s["workingDirectory"])
            if n2 != s["workingDirectory"]:
                s["workingDirectory"] = n2
                ch = True
    return ch


def transform_jsonl(data: bytes, cwd_map, path_map, on_record=None) -> tuple[bytes, int, int]:
    """返回（新内容, 改了几行, 总行数）。没改的行原样保留字节；JSON 解析失败直接报错。
    on_record：每条记录改写前先交给它看（预演时顺便统计会话去过哪些目录，省一遍解析）。"""
    out, changed, total = [], 0, 0
    for line in data.splitlines(keepends=True):
        body = line.rstrip(b"\r\n")
        if not body.strip():
            out.append(line)
            continue
        total += 1
        o = json.loads(body)
        if on_record:
            on_record(o)
        if fix_record(o, cwd_map, path_map):
            out.append(json.dumps(o, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + line[len(body):])
            changed += 1
        else:
            out.append(line)
    return b"".join(out), changed, total


def first_cwd(path: str) -> Optional[str]:
    with open(path, "rb") as f:
        for line in f:
            if b'"cwd"' not in line:
                continue
            try:
                c = json.loads(line).get("cwd")
            except Exception:
                continue
            if c:
                return c
    return None


def cwds_encoding_to(path: str, dirname: str) -> set[str]:
    """会话里编码后正好等于 dirname 的那些 cwd。"""
    out = set()
    with open(path, "rb") as f:
        for line in f:
            if b'"cwd"' not in line:
                continue
            try:
                c = json.loads(line).get("cwd")
            except Exception:
                continue
            if isinstance(c, str) and norm(encode_dir(c)) == norm(dirname):
                out.add(c)
    return out


def root_collector(root: str) -> tuple[set[str], Callable[[dict], None]]:
    """收集会话里出现过的全部项目根（root 下第一级目录），用来查有没有跨项目的会话。"""
    roots: set[str] = set()

    def see(o: dict):
        c = o.get("cwd")
        if not isinstance(c, str) or not c:
            return
        if under(c, root):
            rest = c.replace("/", "\\")[len(root.rstrip("\\/")) + 1:]
            roots.add(rest.split("\\")[0].casefold())
        else:
            roots.add(norm(c))
    return roots, see


# --------------------------------------------------------------------------- #
# JSON 文件写回：先确认能逐字节还原原样，再改
# --------------------------------------------------------------------------- #
_STYLES = [
    ("indent2", dict(indent=2), ""), ("indent2+nl", dict(indent=2), "\n"),
    ("tab", dict(indent="\t"), ""), ("tab+nl", dict(indent="\t"), "\n"),
    ("compact", dict(separators=(",", ":")), ""), ("compact+nl", dict(separators=(",", ":")), "\n"),
]


def detect_style(raw: bytes) -> Optional[tuple[dict, str, bytes]]:
    bom = BOM if raw.startswith(BOM) else b""
    text = raw[len(bom):].decode("utf-8")
    o = json.loads(text)
    for _n, kw, tail in _STYLES:
        if json.dumps(o, ensure_ascii=False, **kw) + tail == text:
            return kw, tail, bom
    return None


def dump_style(o, style) -> bytes:
    kw, tail, bom = style
    return bom + (json.dumps(o, ensure_ascii=False, **kw) + tail).encode("utf-8")


# --------------------------------------------------------------------------- #
# 计划
# --------------------------------------------------------------------------- #
@dataclass
class Mapping:
    old: str      # 逻辑路径
    new: str
    old_name: str
    new_name: str


@dataclass
class SessionOp:
    sid: str
    src_dir: str            # 编码目录名
    dst_dir: Optional[str]  # None：移进备份区
    action: str             # move | keep | retire
    owner: str              # 旧项目名
    first_cwd: str
    files: list[str]        # 相对 projects 目录
    lines_changed: int = 0
    lines_total: int = 0


@dataclass
class Plan:
    run_id: str
    mappings: list[Mapping]
    only: Optional[str]
    dir_renames: list[tuple[str, str]] = field(default_factory=list)       # 逻辑路径
    retire_dirs: list[str] = field(default_factory=list)
    sessions: list[SessionOp] = field(default_factory=list)
    memory_ops: list[dict] = field(default_factory=list)
    orphan_dirs: list[str] = field(default_factory=list)
    leftover_dirs: list[dict] = field(default_factory=list)
    sidebar: list[dict] = field(default_factory=list)
    desktop_config: list[str] = field(default_factory=list)
    git_worktrees: list[str] = field(default_factory=list)
    claude_json: list[str] = field(default_factory=list)
    history: int = 0
    text_files: list[dict] = field(default_factory=list)
    text_skipped: list[dict] = field(default_factory=list)
    ambiguous: list[dict] = field(default_factory=list)
    unhandled: list[str] = field(default_factory=list)
    restores: list[dict] = field(default_factory=list)          # 从备份恢复的会话
    restore_sidebar: list[dict] = field(default_factory=list)   # 从备份恢复的侧栏记录
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    processes: list[str] = field(default_factory=list)


class Planner:
    def __init__(self, cfg: Config, only: Optional[str] = None):
        self.cfg = cfg
        self.only = only
        maps = [self._mapping(o, n) for o, n in cfg.rename.items()]
        if only:
            maps = [m for m in maps if norm(only) in (norm(m.new_name), norm(m.old_name), norm(m.new), norm(m.old))]
            if not maps:
                raise MigrationError(f"--only {only} 不在改名表里")
        self.maps = maps
        self.all_maps = [self._mapping(o, n) for o, n in cfg.rename.items()]
        # 文本和 file-history 一律按完整改名表替换（试点时也只换当次的那个）
        self.pm = PathMapper([(m.old, m.new) for m in maps])
        self.tm = TextMapper([(m.old, m.new) for m in maps])
        self.retire = [] if only else [cfg.logical(n) for n in cfg.retire_dirs]
        self.keep = {} if only else {sid: cfg.logical(n) for sid, n in cfg.keep_sessions.items()}
        self.orphans = [] if only else list(cfg.orphan_encoded_dirs)
        self.olds = [m.old for m in self.all_maps] + [cfg.logical(n) for n in cfg.retire_dirs]
        self.restore_cfg = [] if only else list(cfg.restore)
        self.keep_owner: dict[str, str] = {}   # 从备份恢复的会话：会话 ID -> 原来的项目根

    def _mapping(self, o: str, n: str) -> Mapping:
        old, new = self.cfg.logical(o), self.cfg.logical(n)
        return Mapping(old, new, self.cfg.short(old), self.cfg.short(new))

    # 某条会话的 cwd 映射：改名表 + 该会话的改挂（例外）
    def cwd_mapper(self, sid: str, owner_old: str) -> Callable[[str], str]:
        if sid in self.keep:
            custom = PathMapper([(owner_old, self.keep[sid])])
            return lambda s: custom.map(self.pm.map(s))
        return self.pm.map

    def owner_of(self, cwd: str) -> Optional[tuple[str, str]]:
        """返回（旧项目名, 类别 rename|retire）。"""
        for m in self.all_maps:
            if under(cwd, m.old):
                return m.old_name, "rename"
        for n in self.cfg.retire_dirs:
            if under(cwd, self.cfg.logical(n)):
                return self.cfg.short(self.cfg.logical(n)), "retire"
        return None

    def mapping_by_old(self, name: str) -> Optional[Mapping]:
        key = norm(self.cfg.logical(name))
        return next((m for m in self.maps if norm(m.old) == key), None)

    def dropped(self, path: str) -> bool:
        """配置里的路径条目要不要删：属于退役目录、没有对应目录的编码目录，或目录已不存在（只限 drop_missing_under 下）。"""
        if self.only:
            return False
        if any(under(path, r) for r in self.retire):
            return True
        if not any(under(path, r) for r in self.cfg.drop_missing_under):
            return False
        # 执行时目录可能已经改了名：新旧路径都不在才算没有对应目录
        target = self.pm.map(path)
        return not (os.path.exists(self.cfg.P(path)) or os.path.exists(self.cfg.P(target)))

    def build(self) -> Plan:
        cfg = self.cfg
        plan = Plan(now_id(), self.maps, self.only)
        if cfg.desktop_note:
            plan.warnings.append(cfg.desktop_note)
        steps = [("项目目录", self._dirs), ("会话", self._sessions), ("从备份恢复", self._restore), ("记忆", self._memory),
                 ("迁空的旧目录", self._leftovers), ("侧栏", self._sidebar), ("桌面端设置", self._desktop_config),
                 ("git-worktrees.json", self._git_worktrees), (".claude.json", self._claude_json),
                 ("history.jsonl", self._history), ("项目文件里的路径", self._text), ("路径长度", self._lengths)]
        for name, fn in steps:
            t0 = time.time()
            fn(plan)
            progress(f"预演：{name}（{time.time() - t0:.0f} 秒）")
        return plan

    # 项目目录 ------------------------------------------------------------- #
    def _dirs(self, plan: Plan):
        cfg = self.cfg
        existing = {norm(d) for d in os.listdir(cfg.projects_dir)}
        news = [encode_dir(m.new) for m in self.all_maps]
        for m in self.maps:
            po, pn = cfg.P(m.old), cfg.P(m.new)
            e = encode_dir(m.new)
            if news.count(e) > 1:
                plan.problems.append(f"两个新名编码后相同：{e}")
            if os.path.exists(po) and os.path.exists(pn):
                plan.problems.append(f"新旧目录都在：{m.old} 和 {m.new}")
            elif os.path.exists(po):
                plan.dir_renames.append((m.old, m.new))
                if not os.path.isdir(os.path.dirname(pn)):
                    plan.warnings.append(f"新目录的上级不存在，会新建：{os.path.dirname(m.new)}")
                if norm(e) in existing:
                    plan.warnings.append(f"新编码目录已存在，会合并进去：{e}")
            elif os.path.exists(pn):
                plan.warnings.append(f"已改过名（跳过目录改名）：{m.old_name} -> {m.new_name}")
            else:
                plan.problems.append(f"新旧目录都不在：{m.old}")
        for r in self.retire:
            if os.path.exists(cfg.P(r)):
                plan.retire_dirs.append(r)

    # 会话 ---------------------------------------------------------------- #
    def _sessions(self, plan: Plan):
        cfg = self.cfg
        pdir = cfg.projects_dir
        seen: dict[str, str] = {}
        for d in sorted(os.listdir(pdir)):
            full = os.path.join(pdir, d)
            if not os.path.isdir(full):
                continue
            jsonls = [n for n in os.listdir(full) if n.endswith(".jsonl")]
            for n in jsonls:
                sid = n[:-6]
                if sid in seen:
                    plan.problems.append(f"会话 {sid} 同时在 {seen[sid]} 和 {d}")
                seen[sid] = d
            if d in self.orphans:
                plan.orphan_dirs.append(d)
                # 只核对编码成这个目录名的那个路径（会话中途 cd 去别处不算）
                for n in jsonls:
                    for c in cwds_encoding_to(os.path.join(full, n), d):
                        if os.path.exists(cfg.P(c)):
                            plan.problems.append(f"列为无对应目录的 {d}，但它对应的目录还在：{c}")
                continue
            for n in jsonls:
                sid = n[:-6]
                jp = os.path.join(full, n)
                fc = first_cwd(jp)
                if not fc:
                    continue
                ow = self.owner_of(fc)
                if not ow:
                    continue
                owner, kind = ow
                if kind == "rename" and not self.mapping_by_old(owner):
                    continue  # 试点时其他项目不动
                if self.only and kind == "retire":
                    continue
                if norm(encode_dir(fc)) != norm(d):
                    plan.problems.append(f"会话 {sid[:8]} 在 {d}，但第一条 cwd 编码是 {encode_dir(fc)}，不确定归属，未处理")
                    continue
                files = [os.path.relpath(jp, pdir)]
                sub = os.path.join(full, sid)
                if os.path.isdir(sub):
                    for dp, _dns, fns in os.walk(sub):
                        files += [os.path.relpath(os.path.join(dp, f), pdir) for f in fns]
                files += [os.path.join(d, x) for x in os.listdir(full)
                          if x.startswith(sid + ".") and x != n and os.path.isfile(os.path.join(full, x))]
                if sid in self.keep:
                    action, dst = "keep", encode_dir(self.keep[sid])
                elif kind == "retire":
                    action, dst = "retire", None
                else:
                    action, dst = "move", encode_dir(self.pm.map(fc))
                op = SessionOp(sid, d, dst, action, owner, fc, files)
                if dst:
                    cm = self.cwd_mapper(sid, cfg.logical(owner))
                    for f in files:
                        if f.endswith(".jsonl"):
                            fp = os.path.join(pdir, f)
                            roots, see = root_collector(cfg.root)
                            _b, c, t = transform_jsonl(open(fp, "rb").read(), cm, self.pm.map, see)
                            op.lines_changed += c
                            op.lines_total += t
                            if f == files[0] and len(roots) > 1:
                                plan.warnings.append(f"会话 {sid[:8]}（{owner}）去过别的项目目录：{sorted(roots)}")
                            tgt = os.path.join(pdir, dst, os.path.relpath(f, d))
                            if os.path.exists(tgt):
                                plan.problems.append(f"目标已存在：{tgt}")
                plan.sessions.append(op)

    # 从以前的备份区恢复会话 --------------------------------------------------- #
    def _restore(self, plan: Plan):
        """备份区里的原件只复制不移动（备份保持完整）；cwd 等结构化字段从原来的项目根改到新挂靠的项目。"""
        cfg = self.cfg
        pdir = cfg.projects_dir
        live = set()
        for d in os.listdir(pdir):
            full = os.path.join(pdir, d)
            if os.path.isdir(full):
                live.update(n[:-6] for n in os.listdir(full) if n.endswith(".jsonl"))
        for r in self.restore_cfg:
            to = cfg.logical(r["to"])
            bproj = os.path.join(cfg.P(r["backup"]), "moved", "dotclaude", "projects")
            bside = os.path.join(cfg.P(r["backup"]), "moved", "desktop", "claude-code-sessions")
            ids = set(r["sessions"])
            for sid in r["sessions"]:
                hits = [os.path.join(bproj, d, sid + ".jsonl") for d in os.listdir(bproj)
                        if os.path.isfile(os.path.join(bproj, d, sid + ".jsonl"))] if os.path.isdir(bproj) else []
                if len(hits) != 1:
                    plan.problems.append(f"备份里会话 {sid} 有 {len(hits)} 份，应当正好 1 份")
                    continue
                if sid in live:
                    plan.problems.append(f"会话 {sid[:8]} 已经在 projects 里，不能再恢复一份")
                    continue
                jp = hits[0]
                src = os.path.dirname(jp)
                fc = first_cwd(jp) or ""
                if not under(fc, cfg.root):
                    plan.problems.append(f"会话 {sid[:8]} 的目录不在 {cfg.root} 下：{fc}")
                    continue
                first = fc.replace("/", "\\")[len(cfg.root.rstrip("\\/")) + 1:].split("\\")[0]
                old_root = cfg.logical(first)
                self.keep[sid] = to
                self.keep_owner[sid] = old_root
                files = [jp]
                sub = os.path.join(src, sid)
                for dp, _dns, fns in os.walk(sub):
                    files += [os.path.join(dp, f) for f in fns]
                files += [os.path.join(src, x) for x in os.listdir(src)
                          if x.startswith(sid + ".") and x != sid + ".jsonl" and os.path.isfile(os.path.join(src, x))]
                dst = encode_dir(to)
                cm = self.cwd_mapper(sid, old_root)
                lines = 0
                for f in files:
                    if f.endswith(".jsonl"):
                        _b, c, _t = transform_jsonl(open(f, "rb").read(), cm, self.pm.map)
                        lines += c
                    tgt = os.path.join(pdir, dst, os.path.relpath(f, src))
                    if os.path.exists(tgt):
                        plan.problems.append(f"恢复目标已存在：{tgt}")
                plan.restores.append({"sid": sid, "src": src, "dst": dst, "old_root": old_root, "to": to,
                                      "files": files, "lines": lines})
            for dp, _dns, fns in os.walk(bside):
                for fn in fns:
                    if not (fn.startswith("local_") and fn.endswith(".json")):
                        continue
                    f = os.path.join(dp, fn)
                    raw = open(f, "rb").read()
                    o = json.loads(raw.decode("utf-8-sig"))
                    if o.get("cliSessionId") not in ids:
                        continue
                    rel = os.path.relpath(f, bside)
                    if os.path.exists(os.path.join(cfg.sessions_dir, rel)):
                        plan.problems.append(f"侧栏记录已存在，不能再恢复：{rel}")
                    elif detect_style(raw) is None:
                        plan.problems.append(f"备份里的侧栏记录格式还原不了：{rel}")
                    else:
                        plan.restore_sidebar.append({"src": f, "file": rel, "title": o.get("title"),
                                                     "sid": o.get("cliSessionId")})

    # 记忆 ---------------------------------------------------------------- #
    def _memory(self, plan: Plan):
        cfg = self.cfg
        pdir = cfg.projects_dir
        by_dir: dict[str, set[str]] = {}
        for s in plan.sessions:
            if s.action == "move":
                by_dir.setdefault(s.src_dir, set()).add(s.owner)
        # 编码目录里还留着的、不迁的会话（试点时会有）
        for d, owners in by_dir.items():
            mem = os.path.join(pdir, d, "memory")
            if not os.path.isdir(mem) or not any(True for _ in os.scandir(mem)):
                continue
            conf = cfg.memory.get(d, {})
            owner = conf.get("owner")
            if not owner:
                if len(owners) > 1:
                    plan.problems.append(f"{d} 的 memory 归属不清（混着 {sorted(owners)}），配置里 memory.{d}.owner 要写明")
                    continue
                owner = next(iter(owners))
            m = self.mapping_by_old(owner)
            if not m:
                if self.only:
                    continue
                plan.problems.append(f"{d} 的 memory 归 {owner}，但它不在改名表里")
                continue
            dst_dir = encode_dir(m.new)
            drop = {x.casefold() for x in conf.get("drop", [])}
            for dp, _dns, fns in os.walk(mem):
                for f in fns:
                    src = os.path.join(dp, f)
                    rel = os.path.relpath(src, mem)
                    op = {"src": os.path.relpath(src, pdir), "owner": owner}
                    if rel.casefold() in drop:
                        op["dst"] = None
                    else:
                        op["dst"] = os.path.join(dst_dir, "memory", rel)
                        if os.path.exists(os.path.join(pdir, op["dst"])):
                            plan.problems.append(f"记忆目标已存在：{op['dst']}")
                    plan.memory_ops.append(op)
            for x in drop:
                if not os.path.exists(os.path.join(mem, x)):
                    plan.warnings.append(f"要删的记忆不存在：{d}\\memory\\{x}")

    # 迁完后空出来的旧编码目录 ------------------------------------------------ #
    def _leftovers(self, plan: Plan):
        pdir = self.cfg.projects_dir
        moved_files = set()
        for s in plan.sessions:
            moved_files.update(norm(f) for f in s.files)
        for m in plan.memory_ops:
            moved_files.add(norm(m["src"]))
        for d in sorted({s.src_dir for s in plan.sessions}):
            full = os.path.join(pdir, d)
            rest = []
            for dp, _dns, fns in os.walk(full):
                for f in fns:
                    rel = os.path.relpath(os.path.join(dp, f), pdir)
                    if norm(rel) not in moved_files:
                        rest.append(rel)
            live = [r for r in rest if r.endswith(".jsonl") and os.path.dirname(r) == d]
            if live:
                continue  # 还有不迁的会话，目录留着
            plan.leftover_dirs.append({"dir": d, "rest": rest})

    # 桌面端侧栏 ------------------------------------------------------------ #
    SIDEBAR_FIELDS = {
        "cwd", "originCwd", "worktreePath", "gitAnchorsFolderRealpath", "planPath", "keptWorktreeLeftover",
        "gitAnchors[].gitRoot", "gitAnchors[].commonDir", "gitAnchors[].gitDir", "writtenBranches[]",
        "promptAppendSnapshot.cwd", "sessionPermissionUpdates[].directories[]", "rewindEdges[].cwd",
        "worktreeLazy.path",
    }

    def sidebar_files(self) -> list[str]:
        out = []
        for dp, dns, fns in os.walk(self.cfg.sessions_dir):
            dns[:] = [x for x in dns if "__" not in x]   # 跳过换号工具留下的备份目录
            out += [os.path.join(dp, f) for f in fns if f.startswith("local_") and f.endswith(".json")]
        return sorted(out)

    def sidebar_transform(self, o: dict) -> tuple[bool, list[str], list[str]]:
        sid = o.get("cliSessionId") or ""
        owner = self.keep_owner.get(sid)
        if not owner:
            ow = self.owner_of(o.get("cwd") or "")
            if ow:
                owner = self.cfg.logical(ow[0])
        mp = self.cwd_mapper(sid, owner) if (sid in self.keep and owner) else self.pm.map
        changed, fields, unhandled = False, [], []

        def walk(x, key):
            nonlocal changed
            if isinstance(x, dict):
                for k in list(x.keys()):
                    sub = f"{key}.{k}" if key else k
                    v = x[k]
                    if isinstance(v, str):
                        if sub in self.SIDEBAR_FIELDS:
                            nv = mp(v)
                            if nv != v:
                                x[k] = nv
                                changed = True
                                fields.append(sub)
                        elif find_any_old(v, self.olds):
                            unhandled.append(sub)
                    else:
                        walk(v, sub)
            elif isinstance(x, list):
                for i, v in enumerate(x):
                    sub = key + "[]"
                    if isinstance(v, str):
                        if sub in self.SIDEBAR_FIELDS:
                            nv = mp(v)
                            if nv != v:
                                x[i] = nv
                                changed = True
                                fields.append(sub)
                        elif find_any_old(v, self.olds):
                            unhandled.append(sub)
                    else:
                        walk(v, sub)
        walk(o, "")
        return changed, sorted(set(fields)), sorted(set(unhandled))

    def _sidebar(self, plan: Plan):
        retire_ids = {s.sid for s in plan.sessions if s.action == "retire"}
        orphan_ids = set()
        for d in plan.orphan_dirs:
            full = os.path.join(self.cfg.projects_dir, d)
            orphan_ids.update(n[:-6] for n in os.listdir(full) if n.endswith(".jsonl"))
        for f in self.sidebar_files():
            raw = open(f, "rb").read()
            o = json.loads(raw.decode("utf-8-sig"))
            sid = o.get("cliSessionId")
            rel = os.path.relpath(f, self.cfg.sessions_dir)
            if sid in retire_ids or sid in orphan_ids:
                plan.sidebar.append({"file": rel, "action": "retire", "title": o.get("title"), "sid": sid})
                continue
            if self.only and not self.owner_of(o.get("cwd") or ""):
                continue
            if detect_style(raw) is None:
                plan.problems.append(f"侧栏记录格式还原不了，不敢改：{rel}")
                continue
            ch, fields, unh = self.sidebar_transform(o)
            if unh:
                plan.unhandled.append(f"侧栏 {rel}: {unh}")
            if ch:
                plan.sidebar.append({"file": rel, "action": "rewrite", "fields": fields, "title": o.get("title"),
                                     "sid": sid, "cwd": o.get("cwd")})

    # 桌面端设置 ------------------------------------------------------------ #
    def desktop_config_transform(self, o: dict) -> list[str]:
        notes = []
        ep = (o.get("preferences") or {}).get("epitaxyPrefs") or {}
        mp = self.pm.map
        for k in list(ep.keys()):
            v = ep[k]
            if k.startswith("code-projects-order.") and isinstance(v, list):
                new = []
                for x in v:
                    if isinstance(x, str) and self.dropped(x):
                        notes.append(f"删 {k}: {x}")
                        continue
                    y = mp(x) if isinstance(x, str) else x
                    if y != x:
                        notes.append(f"改 {k}: {x} -> {y}")
                    if y not in new:
                        new.append(y)
                ep[k] = new
            elif k.startswith("epitaxy-folder-permission-mode.") and isinstance(v, dict):
                new = {}
                for pk, pv in v.items():
                    if self.dropped(pk):
                        notes.append(f"删 {k}: {pk}")
                        continue
                    nk = mp(pk)
                    if nk != pk:
                        notes.append(f"改 {k}: {pk} -> {nk}")
                    new[nk] = pv
                ep[k] = new
            elif k.startswith("epitaxy-perm-mode-acks.") and isinstance(v, list):
                new = []
                for x in v:
                    if not isinstance(x, str) or ":" not in x[2:]:
                        new.append(x)
                        continue
                    p, mode = x.rsplit(":", 1)
                    if self.dropped(p):
                        notes.append(f"删 {k}: {x}")
                        continue
                    y = mp(p) + ":" + mode
                    if y != x:
                        notes.append(f"改 {k}: {x} -> {y}")
                    new.append(y)
                ep[k] = new
        return notes

    def _desktop_config(self, plan: Plan):
        f = os.path.join(self.cfg.P(self.cfg.desktop_dir), "claude_desktop_config.json")
        if not os.path.exists(f):
            return
        raw = open(f, "rb").read()
        if detect_style(raw) is None:
            plan.problems.append("claude_desktop_config.json 格式还原不了，不敢改")
            return
        o = json.loads(raw.decode("utf-8-sig"))
        plan.desktop_config = self.desktop_config_transform(o)
        plan.unhandled += [f"桌面端设置 {p}" for p in self._scan_unhandled(o)]

    def _scan_unhandled(self, o) -> list[str]:
        """改写后还残留旧路径的位置（键或值），给人看。"""
        hits = []

        def walk(x, key):
            if isinstance(x, dict):
                for k, v in x.items():
                    if find_any_old(k, self.olds_active()):
                        hits.append(f"{key}.{{键}}{k[:60]}")
                    walk(v, f"{key}.{k}"[:120])
            elif isinstance(x, list):
                for v in x:
                    walk(v, key + "[]")
            elif isinstance(x, str) and find_any_old(x, self.olds_active()):
                hits.append(f"{key} = {x[:80]}")
        walk(o, "")
        return hits

    def olds_active(self) -> list[str]:
        return [m.old for m in self.maps] + self.retire

    # git-worktrees.json ---------------------------------------------------- #
    def git_worktrees_transform(self, o) -> tuple[list[str], dict]:
        notes = []
        mp = self.pm.map

        def lower_map(p: str) -> str:
            # originUrls 的键是小写路径
            y = mp(p)
            return y.lower() if p == p.lower() else y

        def fix(x, key):
            if isinstance(x, dict):
                new = {}
                for k, v in x.items():
                    if re.match(r"^[A-Za-z]:[\\/]", k):
                        if self.dropped(k):
                            notes.append(f"删 {key}: {k}")
                            continue
                        nk = lower_map(k)
                        if nk != k:
                            notes.append(f"改 {key}: {k} -> {nk}")
                        new[nk] = fix(v, f"{key}.{{路径}}")
                    else:
                        new[k] = fix(v, f"{key}.{k}")
                return new
            if isinstance(x, list):
                return [fix(v, key + "[]") for v in x]
            if isinstance(x, str) and re.match(r"^[A-Za-z]:[\\/]", x):
                y = lower_map(x)
                if y != x:
                    notes.append(f"改 {key}: {x} -> {y}")
                return y
            return x
        return notes, fix(o, "")

    def _git_worktrees(self, plan: Plan):
        f = os.path.join(self.cfg.P(self.cfg.desktop_dir), "git-worktrees.json")
        if not os.path.exists(f):
            return
        raw = open(f, "rb").read()
        if detect_style(raw) is None:
            plan.problems.append("git-worktrees.json 格式还原不了，不敢改")
            return
        notes, new = self.git_worktrees_transform(json.loads(raw.decode("utf-8-sig")))
        plan.git_worktrees = notes
        plan.unhandled += [f"git-worktrees {p}" for p in self._scan_unhandled(new)]

    # ~/.claude.json -------------------------------------------------------- #
    def claude_json_transform(self, o: dict) -> list[str]:
        notes = []
        projects = o.get("projects")
        if isinstance(projects, dict):
            new = {}
            for k, v in projects.items():
                if self.dropped(k):
                    notes.append(f"删 projects: {k}")
                    continue
                nk = self.pm.map(k)
                if nk != k:
                    notes.append(f"改 projects: {k} -> {nk}")
                if nk in new:
                    notes.append(f"合并重复键 {nk}（保留先出现的那条）")
                    continue
                new[nk] = v
            o["projects"] = new
        return notes

    def _claude_json(self, plan: Plan):
        f = self.cfg.claude_json
        raw = open(f, "rb").read()
        if detect_style(raw) is None:
            plan.problems.append(".claude.json 格式还原不了，不敢改")
            return
        o = json.loads(raw.decode("utf-8-sig"))
        plan.claude_json = self.claude_json_transform(o)
        rest = self._scan_unhandled({k: v for k, v in o.items() if k != "projects"})
        plan.unhandled += [f".claude.json {p}" for p in rest]

    def _history(self, plan: Plan):
        f = self.cfg.history
        if not os.path.exists(f):
            return
        n = 0
        for line in open(f, "rb"):
            try:
                p = json.loads(line).get("project")
            except Exception:
                continue
            if isinstance(p, str) and self.pm.map(p) != p:
                n += 1
        plan.history = n

    # 项目文件里写死的路径 ------------------------------------------------------ #
    def text_skip_reason(self, logical_path: str) -> Optional[str]:
        for sp in self.cfg.text_skip_paths:
            if under(logical_path, sp):
                return f"配置指定跳过 {sp}"
        parts = [x.casefold() for x in re.split(r"[\\/]+", logical_path)]
        name = parts[-1]
        for d in parts[:-1]:
            if d in self.cfg.text_skip_dirs:
                return f"历史/中间产物目录 {d}"
        if name in self.cfg.text_skip_files:
            return "历史记录文件"
        for s in self.cfg.text_skip_suffixes:
            if name.endswith(s):
                return f"后缀 {s}"
        return None

    def _text(self, plan: Plan):
        cfg = self.cfg
        # 退役目录整个移走，不改；本工具自己的目录（脚本、报告里本来就写着旧路径）也不改
        retired_phys = [cfg.P(cfg.logical(n)) for n in cfg.retire_dirs] + [HERE]
        # 预筛：字节里连旧目录名都没有的文件不跑正则（数据文件多时省很多时间）。
        # 两边都转小写再比，只会多筛进来、不会漏。
        needles = []
        for m in self.maps:
            for enc in ("utf-8", "gbk", "utf-16-le"):
                try:
                    needles.append(path_parts(m.old)[-1].lower().encode(enc).lower())
                except UnicodeEncodeError:
                    pass
        for root in cfg.text_scan_roots:
            scanned = 0
            proot = cfg.P(root)
            for dp, dns, fns in os.walk(proot):
                if any(under(dp, r) for r in retired_phys):
                    dns[:] = []
                    continue
                dns[:] = [x for x in dns if x not in (".git", "node_modules", "__pycache__")
                          and not any(under(os.path.join(dp, x), r) for r in retired_phys)]
                for fn in fns:
                    if os.path.splitext(fn)[1].lower() not in cfg.text_exts:
                        continue
                    pp = os.path.join(dp, fn)
                    try:
                        if os.path.getsize(pp) > 10_000_000:
                            continue
                        raw = open(pp, "rb").read()
                    except OSError:
                        continue
                    scanned += 1
                    low = raw.lower()
                    if not any(nd in low for nd in needles):
                        continue
                    dec = decode_text(raw)
                    if dec is None:
                        continue
                    text = dec[0]
                    _new, n, amb = self.tm.sub(text)
                    if not n and not amb:
                        continue
                    logical = root.rstrip("\\/") + pp[len(proot.rstrip("\\/")):]
                    reason = self.text_skip_reason(logical)
                    if reason:
                        plan.text_skipped.append({"path": logical, "count": n, "reason": reason})
                        continue
                    if n:
                        plan.text_files.append({"path": logical, "count": n, "encoding": dec[1]})
                    for a in amb:
                        plan.ambiguous.append({"path": logical, "snippet": a})
            progress(f"  扫完 {root}：读了 {scanned} 个文本文件")

    def _lengths(self, plan: Plan):
        cfg = self.cfg
        pdir = cfg.projects_dir
        for s in plan.sessions:
            for f in s.files:
                if s.dst_dir:
                    tgt = os.path.join(pdir, s.dst_dir, os.path.relpath(f, s.src_dir))
                    if len(tgt) > cfg.max_path:
                        plan.problems.append(f"路径过长：{tgt}")
        for f in plan.sidebar:
            src = os.path.join(cfg.sessions_dir, f["file"])
            if len(backup_path(cfg, "snapshot", src, "x" * 15)) > cfg.max_path:
                plan.problems.append(f"备份路径过长：{src}")


def self_conflicts(cfg: Config, maps: list[Mapping], runs_dir: str, config_path: str) -> list[str]:
    """要搬 / 改名的目录里不能有正在用的东西：脚本、当前工作目录、运行记录（日志文件一直开着）、配置。
    Windows 上目录里有进程的工作目录或打开的文件，rename 会被拒。"""
    out = []
    here = [("脚本所在目录", os.path.dirname(os.path.abspath(__file__))), ("当前工作目录", os.getcwd()),
            ("运行记录目录（--runs-dir）", os.path.abspath(runs_dir)), ("配置文件", os.path.abspath(config_path))]
    for label, p in here:
        for m in maps:
            if under(p, cfg.P(m.old)):
                out.append(f"{label}在要改名的目录里：{p}（在 {cfg.P(m.old)} 下）。"
                           "把脚本和配置拷到范围外，从范围外的目录用绝对路径运行，--runs-dir 也指到范围外")
    return out


def decode_text(raw: bytes) -> Optional[tuple[str, str, bytes]]:
    if raw.startswith(BOM):
        try:
            return raw[3:].decode("utf-8"), "utf-8", BOM
        except UnicodeDecodeError:
            return None
    if raw.startswith(b"\xff\xfe"):
        try:
            return raw[2:].decode("utf-16-le"), "utf-16-le", b"\xff\xfe"
        except UnicodeDecodeError:
            return None
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc), enc, b""
        except UnicodeDecodeError:
            continue
    return None


def encode_text(text: str, enc: str, bom: bytes) -> bytes:
    return bom + text.encode(enc)


# --------------------------------------------------------------------------- #
# 备份与日志
# --------------------------------------------------------------------------- #
def backup_path(cfg: Config, category: str, phys: str, run_id: str) -> str:
    """备份放在和原文件同一块盘的备份根下（硬链接和移动都要求同盘），按来源换成短标签免得路径太长。"""
    drive = os.path.splitdrive(phys)[0].rstrip(":").upper()
    if drive not in cfg.backup_roots:
        raise MigrationError(f"配置里没有 {drive} 盘的备份目录（backup_roots）")
    base = os.path.join(cfg.backup_roots[drive], run_id, category)
    labels = [
        ("desktop", cfg.P(cfg.desktop_dir)),
        ("dotclaude", os.path.join(cfg.P(cfg.home), ".claude")),
        ("home", cfg.P(cfg.home)),
        ("root", cfg.P(cfg.root)),
    ]
    for lab, pre in labels:
        if under(phys, pre):
            rest = phys.replace("/", "\\")[len(pre.rstrip("\\/")):].lstrip("\\")
            return os.path.join(base, lab, rest) if rest else os.path.join(base, lab)
    d, rest = os.path.splitdrive(phys)
    return os.path.join(base, "abs", d.rstrip(":"), rest.lstrip("\\/"))


class Executor:
    """所有改动都经过这里：先记日志、再动手；失败时按日志逆序还原。"""

    def __init__(self, cfg: Config, run_id: str, run_dir: str):
        self.cfg = cfg
        self.run_id = run_id
        self.run_dir = run_dir
        os.makedirs(run_dir, exist_ok=True)
        self.jpath = os.path.join(run_dir, JOURNAL_NAME)
        self.jf = open(self.jpath, "a", encoding="utf-8")
        self.entries: list[dict] = []
        self.log_lines: list[str] = []

    def log(self, msg: str):
        line = f"{datetime.now():%H:%M:%S} {msg}"
        self.log_lines.append(line)
        print(line, flush=True)

    def _j(self, **rec):
        rec["t"] = time.time()
        self.entries.append(rec)
        self.jf.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.jf.flush()
        os.fsync(self.jf.fileno())

    def bpath(self, category: str, phys: str) -> str:
        return backup_path(self.cfg, category, phys, self.run_id)

    def mkdirs(self, d: str):
        todo = []
        while d and not os.path.isdir(d):
            todo.append(d)
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
        for x in reversed(todo):
            os.mkdir(x)
            self._j(op="mkdir", path=x)

    def move(self, src: str, dst: str):
        if os.path.exists(dst):
            raise MigrationError(f"目标已存在，拒绝覆盖：{dst}")
        self.mkdirs(os.path.dirname(dst))
        self._j(op="move", src=src, dst=dst, pending=True)
        os.rename(src, dst)
        self._j(op="move-done", src=src, dst=dst)

    def move_to_backup(self, src: str, category: str = "moved"):
        self.move(src, self.bpath(category, src))

    def write_new(self, dst: str, data: bytes, mtime_ns: Optional[int] = None):
        if os.path.exists(dst):
            raise MigrationError(f"目标已存在，拒绝覆盖：{dst}")
        self.mkdirs(os.path.dirname(dst))
        tmp = dst + f".tmp-{os.getpid()}"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mtime_ns is not None:
            os.utime(tmp, ns=(mtime_ns, mtime_ns))
        self._j(op="new", path=dst, sha256=sha256_bytes(data))
        os.rename(tmp, dst)

    def snapshot(self, phys: str) -> str:
        """硬链接快照：同盘、瞬间、不占空间；inode 不同就退回真复制并核对 sha256。"""
        dst = self.bpath("snapshot", phys)
        self.mkdirs(os.path.dirname(dst))
        try:
            os.link(phys, dst)
            a, b = os.stat(phys), os.stat(dst)
            if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
                raise OSError("硬链接 inode 不一致")
        except OSError:
            if os.path.exists(dst):
                raise
            shutil.copy2(phys, dst)
            if sha256_file(phys) != sha256_file(dst):
                raise MigrationError(f"快照复制后校验不一致：{phys}")
        return dst

    def replace(self, phys: str, data: bytes):
        before = sha256_file(phys)
        snap = self.snapshot(phys)
        tmp = phys + f".tmp-{os.getpid()}"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        self._j(op="replace", path=phys, snapshot=snap, sha_before=before, sha_after=sha256_bytes(data))
        os.replace(tmp, phys)

    def rmdir_if_empty(self, d: str):
        if os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)
            self._j(op="rmdir", path=d)

    def copy_backup(self, phys: str):
        """关键小文件 / 目录另真复制一份（不依赖硬链接）。"""
        dst = self.bpath("copy", phys)
        if os.path.isdir(phys):
            shutil.copytree(phys, dst)
            for dp, _dns, fns in os.walk(phys):
                for f in fns:
                    s = os.path.join(dp, f)
                    t = os.path.join(dst, os.path.relpath(s, phys))
                    if sha256_file(s) != sha256_file(t):
                        raise MigrationError(f"备份校验不一致：{s}")
        else:
            self.mkdirs(os.path.dirname(dst))
            shutil.copy2(phys, dst)
            if sha256_file(phys) != sha256_file(dst):
                raise MigrationError(f"备份校验不一致：{phys}")
        self._j(op="copy", src=phys, dst=dst)

    def close(self):
        self.jf.close()
        with open(os.path.join(self.run_dir, "apply.log"), "a", encoding="utf-8") as f:
            f.write("\n".join(self.log_lines) + "\n")


def read_journal(path: str) -> list[dict]:
    out = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def undo_entries(cfg: Config, entries: list[dict], run_id: str, force: bool = False) -> list[str]:
    """按日志逆序还原。改过的文件如果之后又被别的程序动过（sha 对不上），默认停下不覆盖。"""
    errors = []
    done_moves = {(e["src"], e["dst"]) for e in entries if e["op"] == "move-done"}
    for e in reversed(entries):
        op = e["op"]
        try:
            if op == "move":
                if (e["src"], e["dst"]) not in done_moves and not os.path.exists(e["dst"]):
                    continue  # 记了日志但 rename 没成功
                if os.path.exists(e["dst"]) and not os.path.exists(e["src"]):
                    os.makedirs(os.path.dirname(e["src"]), exist_ok=True)
                    os.rename(e["dst"], e["src"])
                elif os.path.exists(e["src"]):
                    errors.append(f"原位置已有东西，没还原：{e['src']}")
            elif op == "new":
                p = e["path"]
                if os.path.exists(p):
                    if sha256_file(p) != e["sha256"] and not force:
                        errors.append(f"迁移后被改过，没动：{p}")
                        continue
                    discard = backup_path(cfg, "rolled-back", p, run_id)
                    os.makedirs(os.path.dirname(discard), exist_ok=True)
                    os.rename(p, discard)
                for suffix in (f".tmp-{os.getpid()}",):
                    if os.path.exists(p + suffix):
                        os.remove(p + suffix)
            elif op == "replace":
                p = e["path"]
                if os.path.exists(p) and sha256_file(p) != e["sha_after"] and not force:
                    errors.append(f"迁移后被改过，没还原：{p}")
                    continue
                if os.path.exists(p):
                    discard = backup_path(cfg, "rolled-back", p, run_id)
                    os.makedirs(os.path.dirname(discard), exist_ok=True)
                    os.rename(p, discard)
                try:
                    os.link(e["snapshot"], p)
                except OSError:
                    shutil.copy2(e["snapshot"], p)
                if sha256_file(p) != e["sha_before"]:
                    errors.append(f"还原后校验不一致：{p}")
            elif op == "mkdir":
                if os.path.isdir(e["path"]) and not os.listdir(e["path"]):
                    os.rmdir(e["path"])
            elif op == "rmdir":
                os.makedirs(e["path"], exist_ok=True)
        except OSError as ex:
            errors.append(f"{op} {e.get('path') or e.get('src')}: {ex}")
    return errors


# --------------------------------------------------------------------------- #
# 进程检查
# --------------------------------------------------------------------------- #
_PROC_SCRIPT = r"""
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
Get-CimInstance Win32_Process | Where-Object {
  $_.ProcessId -ne $PID -and ($_.Name -match '^claude' -or ($_.CommandLine -and $_.CommandLine -match 'claude-code[\\/]|@anthropic-ai[\\/]claude'))
} | ForEach-Object { "$($_.ProcessId)`t$($_.Name)`t$($_.ExecutablePath)" }
"""


def find_claude_processes() -> list[str]:
    exe = shutil.which("pwsh") or r"D:\Software\PowerShell\7\pwsh.exe"
    if not os.path.exists(exe):
        exe = "powershell.exe"
    cp = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", _PROC_SCRIPT],
                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if cp.returncode != 0:
        raise MigrationError("查不了进程：" + (cp.stderr or "").strip())
    me = {str(os.getpid()), str(os.getppid())}
    return [r for r in cp.stdout.splitlines() if r.strip() and r.split("\t")[0] not in me]


def require_no_claude(cfg: Config):
    if cfg.sandbox_root:
        roots = [cfg.P(cfg.home), cfg.P(cfg.desktop_dir), cfg.P(cfg.root)] + list(cfg.backup_roots.values())
        if all(under(r, cfg.sandbox_root) for r in roots):
            return  # 沙盒测试：所有路径都在沙盒里，不碰真实数据
        raise MigrationError("配置了 sandbox，但有路径不在沙盒目录里，拒绝执行。")
    procs = find_claude_processes()
    if procs:
        raise MigrationError("还有 Claude 进程在运行，先彻底退出桌面端（托盘里也退）和所有 Claude Code：\n  "
                             + "\n  ".join(procs))


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #
def apply_plan(cfg: Config, planner: Planner, plan: Plan, ex: Executor):
    pdir = cfg.projects_dir

    # 1. 关键小文件真复制一份
    ex.log("备份关键文件（真复制并校验）")
    for p in [cfg.claude_json, cfg.history,
              os.path.join(cfg.P(cfg.desktop_dir), "claude_desktop_config.json"),
              os.path.join(cfg.P(cfg.desktop_dir), "git-worktrees.json"),
              cfg.sessions_dir]:
        if os.path.exists(p):
            ex.copy_backup(p)
    for d in sorted({m["src"].split(os.sep)[0] for m in plan.memory_ops}):
        ex.copy_backup(os.path.join(pdir, d, "memory"))

    # 2. 项目目录改名、退役目录移走（先做：目录被占用时在动 Claude 数据之前就停下）
    for old, new in plan.dir_renames:
        ex.log(f"目录改名 {old} -> {new}")
        ex.move(cfg.P(old), cfg.P(new))
    for r in plan.retire_dirs:
        ex.log(f"目录移进备份区 {r}")
        ex.move_to_backup(cfg.P(r), "retired")

    # 3. 会话
    for s in plan.sessions:
        src_root = os.path.join(pdir, s.src_dir)
        if s.action == "retire":
            ex.log(f"会话移进备份区 {s.owner} {s.sid[:8]}")
            for f in [x for x in s.files if os.path.dirname(x) == s.src_dir]:
                ex.move_to_backup(os.path.join(pdir, f))
            sub = os.path.join(src_root, s.sid)
            if os.path.isdir(sub):
                ex.move_to_backup(sub)
            continue
        ex.log(f"会话 {s.owner} {s.sid[:8]} -> {s.dst_dir}")
        cm = planner.cwd_mapper(s.sid, cfg.logical(s.owner))
        for f in s.files:
            src = os.path.join(pdir, f)
            dst = os.path.join(pdir, s.dst_dir, os.path.relpath(f, s.src_dir))
            if f.endswith(".jsonl"):
                st = os.stat(src)
                data, _c, _t = transform_jsonl(open(src, "rb").read(), cm, planner.pm.map)
                ex.write_new(dst, data, st.st_mtime_ns)
                ex.move_to_backup(src)
            else:
                ex.move(src, dst)
        sub = os.path.join(src_root, s.sid)
        for dp, _dns, _fns in sorted(os.walk(sub), key=lambda t: -len(t[0])) if os.path.isdir(sub) else []:
            ex.rmdir_if_empty(dp)

    # 4. 记忆
    for m in plan.memory_ops:
        src = os.path.join(pdir, m["src"])
        if m["dst"] is None:
            ex.log(f"记忆删掉（移进备份区）{m['src']}")
            ex.move_to_backup(src)
            continue
        raw = open(src, "rb").read()
        st = os.stat(src)
        data = raw
        dec = decode_text(raw)
        if dec is not None:
            text, enc, bom = dec
            text, _n, _a = planner.tm.sub(text)
            src_dir = m["src"].split(os.sep)[0]
            dst_dir = m["dst"].split(os.sep)[0]
            text = re.sub(r"(projects" + SEP + ")" + re.escape(src_dir) + r"(?=" + SEP + ")",
                          lambda mm: mm.group(1) + dst_dir, text)
            if os.path.basename(src).upper() == "MEMORY.MD":
                dropped = [os.path.basename(x["src"]) for x in plan.memory_ops
                           if x["dst"] is None and x["src"].split(os.sep)[0] == src_dir]
                lines = text.splitlines(keepends=True)
                text = "".join(l for l in lines if not any(f"({d})" in l for d in dropped))
            data = encode_text(text, enc, bom)
        ex.log(f"记忆 {m['src']} -> {m['dst']}")
        ex.write_new(os.path.join(pdir, m["dst"]), data, st.st_mtime_ns)
        ex.move_to_backup(src)

    # 5. 没有对应目录的编码目录、迁空的旧编码目录
    for d in plan.orphan_dirs:
        ex.log(f"无对应目录的编码目录移进备份区 {d}")
        ex.move_to_backup(os.path.join(pdir, d))
    for lo in plan.leftover_dirs:
        full = os.path.join(pdir, lo["dir"])
        if not os.path.isdir(full):
            continue
        # 前面逐个移走的会话文件已经在备份区的同名目录里，剩下的也逐个移过去，再删空目录
        ex.log(f"迁空的旧编码目录移进备份区 {lo['dir']}（剩 {len(lo['rest'])} 个文件）")
        for rel in lo["rest"]:
            p = os.path.join(pdir, rel)
            if os.path.exists(p):
                ex.move_to_backup(p)
        for dp, _dns, _fns in sorted(os.walk(full), key=lambda t: -len(t[0])):
            ex.rmdir_if_empty(dp)

    # 6. 侧栏
    for sb in plan.sidebar:
        f = os.path.join(cfg.sessions_dir, sb["file"])
        if sb["action"] == "retire":
            ex.log(f"侧栏记录移进备份区 {sb['title']}")
            ex.move_to_backup(f)
            continue
        raw = open(f, "rb").read()
        style = detect_style(raw)
        o = json.loads(raw.decode("utf-8-sig"))
        planner.sidebar_transform(o)
        ex.replace(f, dump_style(o, style))
    ex.log(f"侧栏记录改写 {sum(1 for x in plan.sidebar if x['action'] == 'rewrite')} 条")

    # 6b. 从以前的备份区恢复会话和侧栏记录（复制，不动备份里的原件）
    for r in plan.restores:
        ex.log(f"从备份恢复会话 {r['sid'][:8]} -> {r['dst']}")
        cm = planner.cwd_mapper(r["sid"], r["old_root"])
        for f in r["files"]:
            dst = os.path.join(pdir, r["dst"], os.path.relpath(f, r["src"]))
            data = open(f, "rb").read()
            if f.endswith(".jsonl"):
                data, _c, _t = transform_jsonl(data, cm, planner.pm.map)
            ex.write_new(dst, data, os.stat(f).st_mtime_ns)
    for sb in plan.restore_sidebar:
        raw = open(sb["src"], "rb").read()
        style = detect_style(raw)
        o = json.loads(raw.decode("utf-8-sig"))
        planner.sidebar_transform(o)
        ex.log(f"从备份恢复侧栏记录 {sb['title']}")
        ex.write_new(os.path.join(cfg.sessions_dir, sb["file"]), dump_style(o, style), os.stat(sb["src"]).st_mtime_ns)

    # 7. 桌面端设置、git-worktrees.json
    f = os.path.join(cfg.P(cfg.desktop_dir), "claude_desktop_config.json")
    if plan.desktop_config and os.path.exists(f):
        raw = open(f, "rb").read()
        style = detect_style(raw)
        o = json.loads(raw.decode("utf-8-sig"))
        planner.desktop_config_transform(o)
        ex.replace(f, dump_style(o, style))
        ex.log(f"桌面端设置改 {len(plan.desktop_config)} 处")
    f = os.path.join(cfg.P(cfg.desktop_dir), "git-worktrees.json")
    if plan.git_worktrees and os.path.exists(f):
        raw = open(f, "rb").read()
        style = detect_style(raw)
        _n, new = planner.git_worktrees_transform(json.loads(raw.decode("utf-8-sig")))
        ex.replace(f, dump_style(new, style))
        ex.log(f"git-worktrees.json 改 {len(plan.git_worktrees)} 处")

    # 8. ~/.claude.json、history.jsonl
    if plan.claude_json:
        raw = open(cfg.claude_json, "rb").read()
        style = detect_style(raw)
        o = json.loads(raw.decode("utf-8-sig"))
        planner.claude_json_transform(o)
        ex.replace(cfg.claude_json, dump_style(o, style))
        ex.log(f".claude.json 改 {len(plan.claude_json)} 处")
    if plan.history:
        out = []
        for line in open(cfg.history, "rb").read().splitlines(keepends=True):
            body = line.rstrip(b"\r\n")
            try:
                o = json.loads(body)
            except Exception:
                out.append(line)
                continue
            p = o.get("project")
            if isinstance(p, str) and planner.pm.map(p) != p:
                o["project"] = planner.pm.map(p)
                out.append(json.dumps(o, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + line[len(body):])
            else:
                out.append(line)
        ex.replace(cfg.history, b"".join(out))

    # 9. 项目文件里的路径（目录已改名，按新位置找文件）
    for t in plan.text_files:
        new_logical = planner.pm.map(t["path"])
        phys = cfg.P(new_logical)
        raw = open(phys, "rb").read()
        dec = decode_text(raw)
        text, enc, bom = dec
        text2, n, _a = planner.tm.sub(text)
        if n:
            ex.replace(phys, encode_text(text2, enc, bom))
    ex.log(f"项目文件改路径 {len(plan.text_files)} 个")


# --------------------------------------------------------------------------- #
# 核对
# --------------------------------------------------------------------------- #
def verify(cfg: Config, only: Optional[str] = None) -> tuple[list[str], list[str]]:
    problems, info = [], []
    planner = Planner(cfg, only)
    pdir = cfg.projects_dir
    for m in planner.maps:
        if os.path.exists(cfg.P(m.old)):
            problems.append(f"旧目录还在（或被重建了）：{m.old}")
        if not os.path.exists(cfg.P(m.new)):
            problems.append(f"新目录不在：{m.new}")
    if not only:
        for r in planner.retire:
            if os.path.exists(cfg.P(r)):
                problems.append(f"退役目录还在（或被重建了）：{r}")
    # 会话：编码一致、同一项目、ID 不重复
    seen = {}
    per_dir: dict[str, set] = {}
    olds_b = [("\"cwd\":\"" + m.old.replace("\\", "\\\\")).encode("utf-8") for m in planner.maps]
    for d in sorted(os.listdir(pdir)):
        full = os.path.join(pdir, d)
        if not os.path.isdir(full):
            continue
        for n in os.listdir(full):
            if not n.endswith(".jsonl"):
                continue
            sid = n[:-6]
            if sid in seen:
                problems.append(f"会话 {sid[:8]} 重复：{seen[sid]} 和 {d}")
            seen[sid] = d
            jp = os.path.join(full, n)
            fc = first_cwd(jp)
            if fc and norm(encode_dir(fc)) != norm(d):
                problems.append(f"{d}\\{sid[:8]} 第一条 cwd 编码不一致：{fc}")
            if fc:
                ow = planner.owner_of(fc)
                per_dir.setdefault(d, set()).add(ow[0] if ow else (fc.split("\\")[2] if fc.count("\\") >= 2 else fc))
            # 只认每行顶层的 cwd；正文里工具参数碰巧叫 cwd 的不算
            stale = False
            with open(jp, "rb") as fh:
                for line in fh:
                    if not any(ob in line for ob in olds_b):
                        continue
                    try:
                        c = json.loads(line).get("cwd")
                    except Exception:
                        continue
                    if isinstance(c, str) and find_any_old(c, [m.old for m in planner.maps]):
                        stale = True
                        break
            if stale:
                problems.append(f"{d}\\{sid[:8]} 里还有旧 cwd")
    for d, owners in per_dir.items():
        if len(owners) > 1 and d.startswith(encode_dir(cfg.root)):
            info.append(f"编码目录 {d} 里有多个项目：{sorted(owners)}")
    # 侧栏：目录在、会话文件在
    ok = 0
    for f in planner.sidebar_files():
        o = json.loads(open(f, "rb").read().decode("utf-8-sig"))
        cwd, sid = o.get("cwd") or "", o.get("cliSessionId") or ""
        if find_any_old(cwd, planner.olds_active()):
            problems.append(f"侧栏还指向旧路径：{o.get('title')} {cwd}")
            continue
        if not under(cwd, cfg.root):
            continue
        if not os.path.isdir(cfg.P(cwd)):
            problems.append(f"侧栏的目录不存在：{o.get('title')} {cwd}")
        elif sid and not os.path.exists(os.path.join(pdir, encode_dir(cwd), sid + ".jsonl")):
            problems.append(f"侧栏找不到会话文件：{o.get('title')} {encode_dir(cwd)}\\{sid}.jsonl")
        else:
            ok += 1
    info.append(f"D:\\AI 下的侧栏记录对得上：{ok} 条")
    # 配置文件里不该再有旧路径
    olds = planner.olds_active()
    for name, f in [(".claude.json", cfg.claude_json),
                    ("桌面端设置", os.path.join(cfg.P(cfg.desktop_dir), "claude_desktop_config.json")),
                    ("git-worktrees.json", os.path.join(cfg.P(cfg.desktop_dir), "git-worktrees.json"))]:
        if not os.path.exists(f):
            continue
        o = json.loads(open(f, "rb").read().decode("utf-8-sig"))
        if name == ".claude.json":
            keys = list((o.get("projects") or {}).keys())
            left = [k for k in keys if find_any_old(k, olds)]
        else:
            left = planner._scan_unhandled(o)
        if left:
            problems.append(f"{name} 里还有旧路径：{left[:5]}")
    # 桌面端日志里的目录报错
    # 商店版的日志不在数据目录下，而在 %LOCALAPPDATA%\Claude\logs；两处都看，取最近写过的那份
    logs = [os.path.join(cfg.P(cfg.desktop_dir), "logs", "main.log"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Claude", "logs", "main.log")]
    logs = [x for x in logs if os.path.exists(x)]
    if logs and not cfg.sandbox_root:
        logf = max(logs, key=os.path.getmtime)
        hits = [l.strip() for l in open(logf, encoding="utf-8", errors="replace") if "Working directory no longer exists" in l]
        info.append(f"桌面端日志 {logf}：Working directory no longer exists 共 {len(hits)} 条"
                    + (f"，最近一条：{hits[-1][:200]}" if hits else ""))
    return problems, info


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
def write_report(plan: Plan, cfg: Config, path: str):
    L = []
    add = L.append
    add(f"# 迁移预演报告 {plan.run_id}" + (f"（只做 {plan.only}）" if plan.only else ""))
    add("")
    add(f"- 项目根：{cfg.root}；Claude 数据：{cfg.P(cfg.home)}；桌面端数据：{cfg.P(cfg.desktop_dir)}")
    if plan.processes:
        add(f"- 正在运行的 Claude 进程 {len(plan.processes)} 个（apply 前必须全部退出）")
    add("")
    add("## 阻止执行的问题" if plan.problems else "## 阻止执行的问题：无")
    for p in plan.problems:
        add(f"- {p}")
    if plan.warnings:
        add("\n## 提醒")
        for w in plan.warnings:
            add(f"- {w}")
    add("\n## 目录")
    for o, n in plan.dir_renames:
        add(f"- 改名 `{o}` → `{n}`（新编码目录 `{encode_dir(n)}`）")
    for r in plan.retire_dirs:
        add(f"- 移进备份区 `{r}`")
    add("\n## 会话")
    c = Counter((s.owner, s.action, s.src_dir, s.dst_dir) for s in plan.sessions)
    for (owner, action, src, dst), n in sorted(c.items()):
        verb = {"move": "搬到", "keep": "改挂到", "retire": "移进备份区"}[action]
        add(f"- {owner}：{n} 条，`{src}` {verb}" + (f" `{dst}`" if dst else ""))
    tot_files = sum(len(s.files) for s in plan.sessions)
    tot_lines = sum(s.lines_changed for s in plan.sessions)
    add(f"- 合计 {len(plan.sessions)} 条会话、{tot_files} 个文件；会话记录里改 {tot_lines} 行（只改 cwd 等结构化字段）")
    for s in plan.sessions:
        if s.action == "keep":
            add(f"- 例外：{s.sid} 改挂到 `{s.dst_dir}`")
    if plan.restores or plan.restore_sidebar:
        add("\n## 从备份恢复（复制，备份里的原件不动）")
        for r in plan.restores:
            add(f"- 会话 {r['sid']}：原属 `{r['old_root']}`，恢复到 `{r['dst']}`（{len(r['files'])} 个文件，改 {r['lines']} 行）")
        for sb in plan.restore_sidebar:
            add(f"- 侧栏记录：{sb['title']}")
    add("\n## 记忆")
    mc = defaultdict(list)
    for m in plan.memory_ops:
        mc[(m["src"].split(os.sep)[0], m["dst"].split(os.sep)[0] if m["dst"] else None)].append(os.path.basename(m["src"]))
    for (src, dst), files in sorted(mc.items(), key=lambda t: str(t)):
        if dst:
            add(f"- `{src}\\memory` → `{dst}\\memory`：{len(files)} 个文件")
        else:
            add(f"- 删掉（移进备份区）：{', '.join(files)}")
    add("\n## 移进备份区的编码目录")
    for d in plan.orphan_dirs:
        add(f"- 无对应目录：`{d}`")
    for lo in plan.leftover_dirs:
        add(f"- 迁空的旧目录：`{lo['dir']}`（剩 {len(lo['rest'])} 个文件：{', '.join(os.path.basename(x) for x in lo['rest'][:6])}）")
    add("\n## 桌面端侧栏")
    rw = [x for x in plan.sidebar if x["action"] == "rewrite"]
    rt = [x for x in plan.sidebar if x["action"] == "retire"]
    add(f"- 改写 {len(rw)} 条；移进备份区 {len(rt)} 条")
    fc = Counter(f for x in rw for f in x["fields"])
    add(f"- 改到的字段：{dict(fc)}")
    for x in rt:
        add(f"  - 移走：{x['title']}")
    add("\n## 桌面端设置（claude_desktop_config.json）")
    for x in plan.desktop_config:
        add(f"- {x}")
    add("\n## git-worktrees.json")
    for x in plan.git_worktrees:
        add(f"- {x}")
    add("\n## ~/.claude.json")
    for x in plan.claude_json:
        add(f"- {x}")
    add(f"\n## history.jsonl：改 {plan.history} 行")
    add("\n## 项目文件里的路径：要改的")
    for t in plan.text_files:
        add(f"- `{t['path']}`：{t['count']} 处（{t['encoding']}）")
    add("\n## 项目文件里的路径：不改的（历史记录、中间产物）")
    sk = defaultdict(list)
    for t in plan.text_skipped:
        sk[t["reason"]].append(t["path"])
    for reason, paths in sorted(sk.items()):
        add(f"- {reason}：{len(paths)} 个，如 `{paths[0]}`")
    if plan.ambiguous:
        add("\n## 后面紧跟别的字、没自动替换的（要人工看）")
        for a in plan.ambiguous[:60]:
            add(f"- `{a['path']}`：…{a['snippet']}…")
    if plan.unhandled:
        add("\n## 结构化文件里没处理到的旧路径")
        for u in plan.unhandled[:60]:
            add(f"- {u}")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L) + "\n")


def plan_to_json(plan: Plan) -> dict:
    d = dict(plan.__dict__)
    d["mappings"] = [m.__dict__ for m in plan.mappings]
    d["sessions"] = [s.__dict__ for s in plan.sessions]
    return d


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="项目目录改名并迁移 Claude 数据（默认只读预演）")
    ap.add_argument("command", choices=["plan", "apply", "verify", "rollback"])
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--only", help="只做改名表里的这一个项目（新名或旧名），用于试点")
    ap.add_argument("--yes", action="store_true", help="apply 时不再交互确认")
    ap.add_argument("--run", help="rollback：runs 下的目录名")
    ap.add_argument("--force", action="store_true", help="rollback：迁移后被改过的文件也还原")
    ap.add_argument("--runs-dir", default=RUNS_DIR)
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        cfg = load_config(a.config)
        if a.command == "plan":
            planner = Planner(cfg, a.only)
            plan = planner.build()
            plan.problems += self_conflicts(cfg, planner.maps, a.runs_dir, a.config)
            if not cfg.sandbox_root:
                try:
                    plan.processes = find_claude_processes()
                except MigrationError as e:
                    plan.warnings.append(str(e))
            out = os.path.join(a.runs_dir, plan.run_id + "-plan")
            os.makedirs(out, exist_ok=True)
            write_report(plan, cfg, os.path.join(out, "report.md"))
            json.dump(plan_to_json(plan), open(os.path.join(out, "plan.json"), "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
            print(f"预演报告：{os.path.join(out, 'report.md')}")
            print(f"阻止执行的问题 {len(plan.problems)} 个；会话 {len(plan.sessions)} 条；侧栏 {len(plan.sidebar)} 条；"
                  f"项目文件 {len(plan.text_files)} 个；从备份恢复会话 {len(plan.restores)} 条、侧栏 {len(plan.restore_sidebar)} 条")
            return 1 if plan.problems else 0

        if a.command == "verify":
            problems, info = verify(cfg, a.only)
            for i in info:
                print("·", i)
            for p in problems:
                print("✗", p)
            print("核对通过" if not problems else f"有 {len(problems)} 个问题")
            return 1 if problems else 0

        if a.command == "rollback":
            if not a.run:
                raise MigrationError("rollback 要指定 --run <runs 下的目录名>")
            require_no_claude(cfg)
            run_dir = os.path.join(a.runs_dir, a.run)
            entries = read_journal(os.path.join(run_dir, JOURNAL_NAME))
            run_id = a.run.split("-apply")[0]
            errors = undo_entries(cfg, entries, run_id, a.force)
            for e in errors:
                print("✗", e)
            print("还原完成" if not errors else f"还原时有 {len(errors)} 个问题")
            return 1 if errors else 0

        # apply
        require_no_claude(cfg)
        planner = Planner(cfg, a.only)
        plan = planner.build()
        plan.problems += self_conflicts(cfg, planner.maps, a.runs_dir, a.config)
        run_dir = os.path.join(a.runs_dir, plan.run_id + "-apply")
        os.makedirs(run_dir, exist_ok=True)
        write_report(plan, cfg, os.path.join(run_dir, "report.md"))
        if plan.problems:
            print("有阻止执行的问题，什么都没改：")
            for p in plan.problems:
                print("  ✗", p)
            return 1
        print(f"将改名 {len(plan.dir_renames)} 个目录、迁 {len(plan.sessions)} 条会话、改侧栏 {len(plan.sidebar)} 条、"
              f"改项目文件 {len(plan.text_files)} 个、从备份恢复 {len(plan.restores)} 条会话和 {len(plan.restore_sidebar)} 条侧栏记录。"
              f"详情：{os.path.join(run_dir, 'report.md')}")
        if not a.yes and input("确认执行请输入 YES：").strip() != "YES":
            print("已取消，什么都没改。")
            return 1
        ex = Executor(cfg, plan.run_id, run_dir)
        try:
            apply_plan(cfg, planner, plan, ex)
        except BaseException as e:
            ex.log(f"出错：{e!r}，按日志还原")
            ex.jf.flush()
            errors = undo_entries(cfg, ex.entries, plan.run_id, force=True)
            for x in errors:
                ex.log(f"还原问题：{x}")
            ex.close()
            print("迁移失败，已还原" + ("" if not errors else f"（还原时有 {len(errors)} 个问题，见 apply.log）"))
            return 2
        ex.close()
        problems, info = verify(cfg, a.only)
        for i in info:
            print("·", i)
        for p in problems:
            print("✗", p)
        print(f"迁移完成。运行目录：{run_dir}；回滚：python rename_projects.py rollback --run {os.path.basename(run_dir)}")
        return 0 if not problems else 3
    except MigrationError as e:
        print("✗", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
