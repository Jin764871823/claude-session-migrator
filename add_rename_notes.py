"""
add_rename_notes.py — 迁移后给每个改名项目的 PROJECT_STATE.md 开头补一句改名说明（读 rename_config.json 的改名表）。
放在文件开头"更新"那一行（或它所在的引用块）之后；没有"更新"行就放在标题块之后。
保留原编码、BOM 和行尾；已经有这句的跳过。默认只预览，加 --apply 才写。
"""

from __future__ import annotations

import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MARK = "目录改名："


def note_for(old: str, new: str, date: str, tool_dir: str) -> str:
    """old / new 是完整路径（改名、搬进别的目录都适用）；tool_dir 是迁移工具项目现在的位置。"""
    from rename_projects import encode_dir
    return (f"> {date} {MARK}本项目从 `{old}` 改为 `{new}`，Claude 会话和记忆迁到 "
            f"`~\\.claude\\projects\\{encode_dir(new)}`。本文件、README 和脚本里的路径已换成新路径；"
            f"HISTORY、日志、归档和旧会话里仍是旧路径，按此换算。迁移记录见 `{tool_dir}`。")


def insert_pos(lines: list[str]) -> int:
    """返回插入位置（在这一行之前插）。"""
    head = min(len(lines), 12)
    idx = next((i for i in range(head) if "更新" in lines[i]), None)
    if idx is None:
        idx = 0                      # 标题行
        while idx + 1 < len(lines) and lines[idx + 1].startswith(">"):
            idx += 1
    elif lines[idx].startswith(">"):
        while idx + 1 < len(lines) and lines[idx + 1].startswith(">"):
            idx += 1
    return idx + 1


def main(argv=None) -> int:
    sys.path.insert(0, HERE)
    args = list(argv if argv is not None else sys.argv[1:])
    apply = "--apply" in args
    cfg_path = os.path.join(HERE, "rename_config.json")
    if "--config" in args:
        cfg_path = os.path.abspath(args[args.index("--config") + 1])
    date = datetime.date.today().isoformat()
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    from rename_projects import resolve_path
    root = cfg["root"]
    pairs = [(resolve_path(root, o), resolve_path(root, n)) for o, n in cfg["rename"].items()]
    # 迁移工具项目自己的位置：在改名表里就用它的新路径，不在就是 root 下的原位置
    tool_dir = next((n for o, n in pairs if os.path.basename(o).casefold() == "claude-session-migrator"),
                    resolve_path(root, "claude-session-migrator"))
    for old, new in pairs:
        f = os.path.join(new, "PROJECT_STATE.md")
        if not os.path.exists(f):
            print(f"跳过（没有 STATE）：{new}")
            continue
        raw = open(f, "rb").read()
        bom = raw.startswith(b"\xef\xbb\xbf")
        text = raw[3:].decode("utf-8") if bom else raw.decode("utf-8")
        nl = "\r\n" if "\r\n" in text else "\n"
        if MARK in text:
            print(f"已有说明，跳过：{new}")
            continue
        lines = text.split(nl)
        pos = insert_pos(lines)
        block = ["", note_for(old, new, date, tool_dir)]
        if pos < len(lines) and lines[pos].strip():
            block.append("")
        new_lines = lines[:pos] + block + lines[pos:]
        print(f"{new}：插在第 {pos + 1} 行前（上一行：{lines[pos - 1][:50]!r}）")
        if apply:
            data = nl.join(new_lines).encode("utf-8")
            tmp = f + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write((b"\xef\xbb\xbf" if bom else b"") + data)
            os.replace(tmp, f)
    if not apply:
        print("\n只是预览；加 --apply 写入。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
