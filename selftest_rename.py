"""
selftest_rename.py — 在临时目录里搭一套假环境，跑 rename_projects.py 的 plan / apply / verify / rollback，
不碰真实数据。用法：python selftest_rename.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rename_projects as rp  # noqa: E402

ACCT = "aaaaaaaa-0000-0000-0000-000000000001"
ORG = "bbbbbbbb-0000-0000-0000-000000000002"
S1 = "11111111-1111-1111-1111-111111111111"   # 甲乙丙丁，要迁
S2 = "22222222-2222-2222-2222-222222222222"   # 戊己庚辛，要迁
S3 = "33333333-3333-3333-3333-333333333333"   # 旧项目一，退役
S4 = "44444444-4444-4444-4444-444444444444"   # 旧项目一，例外：改挂到 tool
S5 = "55555555-5555-5555-5555-555555555555"   # 英文项目，不动
S6 = "66666666-6666-6666-6666-666666666666"   # 无对应目录
DEAD = "77777777-7777-7777-7777-777777777777"  # 只剩删除标记


def jl(*recs) -> bytes:
    return b"".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n" for r in recs)


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode("utf-8"))


def tree(*roots) -> dict:
    out = {}
    for r in roots:
        for dp, _dns, fns in os.walk(r):
            for f in fns:
                p = os.path.join(dp, f)
                out[os.path.relpath(p, os.path.dirname(r))] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


def build(tmp: str) -> str:
    AI = os.path.join(tmp, "AI")
    HOME = os.path.join(tmp, "home")
    DESK = os.path.join(tmp, "desktop")
    proj = os.path.join(HOME, ".claude", "projects")
    for d in ["甲乙丙丁", "戊己庚辛", "旧项目一", "tool", "eng"]:
        os.makedirs(os.path.join(AI, d))
    write(os.path.join(AI, "甲乙丙丁", "PROJECT_STATE.md"),
          "路径 D:\\AI\\甲乙丙丁\\data\\a.csv 和 D:/AI/甲乙丙丁/b，还有 D:\\AI\\甲乙丙丁改名后 这种不替换。\r\n")
    write(os.path.join(AI, "甲乙丙丁", "PROJECT_HISTORY.md"), "历史：D:\\AI\\甲乙丙丁\\old\n")
    write(os.path.join(AI, "eng", "run.py"), 'DATA = r"D:\\AI\\戊己庚辛\\data"\nX = "D:\\\\AI\\\\戊己庚辛\\\\y"\n')
    write(os.path.join(AI, "eng", "go.bat"), "cd /d D:\\AI\\戊己庚辛\r\n".encode("gbk"))
    # 会话
    d4 = os.path.join(proj, "D--AI-----")
    write(os.path.join(d4, S1 + ".jsonl"), jl(
        {"type": "user", "cwd": "D:\\AI\\甲乙丙丁", "sessionId": S1, "message": {"content": "看 D:\\AI\\甲乙丙丁\\x"}},
        {"type": "assistant", "cwd": "D:\\AI\\甲乙丙丁\\sub", "sessionId": S1,
         "message": {"content": [{"type": "thinking", "thinking": "D:\\AI\\甲乙丙丁", "signature": "sig"}]}},
        {"type": "file-history-snapshot", "snapshot": {"trackedFileBackups": {
            "D:\\AI\\甲乙丙丁\\a.md": {"realParentDir": "D:\\AI\\甲乙丙丁", "backupFileName": "x@v1"},
            "rel.md": {"realParentDir": "D:\\AI\\甲乙丙丁\\sub"}}}},
        {"type": "file-history-delta", "trackingPath": "D:\\AI\\甲乙丙丁\\a.md", "backup": {"realParentDir": "D:\\AI\\甲乙丙丁"}},
    ))
    write(os.path.join(d4, S1, "subagents", "agent-a.jsonl"), jl({"type": "user", "cwd": "D:\\AI\\甲乙丙丁", "sessionId": S1}))
    write(os.path.join(d4, S1, "subagents", "agent-a.meta.json"), '{"agentType":"x"}')
    write(os.path.join(d4, S1, "tool-results", "r.txt"), "输出 D:\\AI\\甲乙丙丁")
    write(os.path.join(d4, S1 + ".desktop-released.json"), '{"v":1}')
    write(os.path.join(d4, S2 + ".jsonl"), jl({"type": "user", "cwd": "D:\\AI\\戊己庚辛", "sessionId": S2}))
    write(os.path.join(d4, S3 + ".jsonl"), jl({"type": "user", "cwd": "D:\\AI\\旧项目一", "sessionId": S3}))
    write(os.path.join(d4, S4 + ".jsonl"), jl({"type": "user", "cwd": "D:\\AI\\旧项目一", "sessionId": S4}))
    write(os.path.join(d4, DEAD + ".desktop-released.json"), '{"v":1}')
    write(os.path.join(d4, "memory", "MEMORY.md"), "- [A](a.md) — 在 D:\\AI\\甲乙丙丁\n- [Drop](drop.md) — 不要\n")
    write(os.path.join(d4, "memory", "a.md"), "见 C:\\Users\\demo\\.claude\\projects\\D--AI-----\\memory\\a.md\n")
    write(os.path.join(d4, "memory", "drop.md"), "x\n")
    write(os.path.join(proj, "D--AI-eng", S5 + ".jsonl"), jl({"type": "user", "cwd": "D:\\AI\\eng", "sessionId": S5}))
    write(os.path.join(proj, "D--AI-gone", S6 + ".jsonl"), jl({"type": "user", "cwd": "D:\\AI\\gone", "sessionId": S6}))
    # 侧栏
    sb = os.path.join(DESK, "claude-code-sessions", ACCT, ORG)
    for sid, cwd in [(S1, "D:\\AI\\甲乙丙丁"), (S2, "D:\\AI\\戊己庚辛"), (S3, "D:\\AI\\旧项目一"),
                     (S4, "D:\\AI\\旧项目一"), (S5, "D:\\AI\\eng")]:
        o = {"sessionId": "local_" + sid, "cliSessionId": sid, "cwd": cwd, "originCwd": cwd, "title": "t" + sid[:1],
             "gitAnchors": [{"gitRoot": cwd, "commonDir": cwd + "\\.git"}], "writtenBranches": [cwd + "\x00main"],
             "sessionPermissionUpdates": [{"directories": [cwd + "\\sub"]}], "postTurnSummary": {"t": "写到了 " + cwd}}
        write(os.path.join(sb, f"local_{sid}.json"), json.dumps(o, ensure_ascii=False, separators=(",", ":")))
    ep = {
        f"code-projects-order.{ACCT}": ["D:\\AI\\eng", "D:\\AI\\甲乙丙丁", "D:\\AI\\旧项目一", "D:\\AI\\gone"],
        f"epitaxy-folder-permission-mode.{ACCT}": {"D:\\AI\\甲乙丙丁": "auto", "D:\\AI\\gone": "auto", "D:\\AI\\eng": "x"},
        f"epitaxy-perm-mode-acks.{ACCT}": ["D:\\AI\\戊己庚辛:bypassPermissions", "D:\\AI\\eng:auto"],
    }
    write(os.path.join(DESK, "claude_desktop_config.json"),
          json.dumps({"mcpServers": {}, "preferences": {"epitaxyPrefs": ep}}, ensure_ascii=False, indent=2))
    gw = {"worktrees": {}, "schemaVersion": 2, "untrackedDirGc": {"sightings": {}, "roots": {},
          "cwds": {"D:\\AI\\甲乙丙丁": 1, "D:\\AI\\旧项目一": 1, "D:\\AI\\eng": 1}},
          "originUrls": {"d:\\ai\\戊己庚辛": {"commonDir": "d:\\ai\\戊己庚辛\\.git", "url": ""}}}
    write(os.path.join(DESK, "git-worktrees.json"), json.dumps(gw, ensure_ascii=False, indent="\t") + "\n")
    cj = {"numStartups": 1, "projects": {"D:\\AI\\甲乙丙丁": {"allowedTools": []}, "D:/AI/甲乙丙丁": {"a": 1},
          "D:\\AI\\eng": {}, "D:/AI/旧项目一": {}, "D:\\AI\\gone": {}}}
    write(os.path.join(HOME, ".claude.json"), json.dumps(cj, ensure_ascii=False, indent=2))
    write(os.path.join(HOME, ".claude", "history.jsonl"), jl({"display": "x", "project": "D:\\AI\\戊己庚辛"}))
    cfg = {
        "root": "D:\\AI", "home": "C:\\Users\\demo", "desktop_dir": "C:\\Users\\demo\\FakeDesktop",
        "backup_roots": {os.path.splitdrive(tmp)[0].rstrip(":"): os.path.join(tmp, "backup")},
        "rename": {"甲乙丙丁": "alpha", "戊己庚辛": "beta"},
        "retire_dirs": ["旧项目一"], "keep_sessions": {S4: "tool"},
        "orphan_encoded_dirs": ["D--AI-gone"],
        "memory": {"D--AI-----": {"owner": "甲乙丙丁", "drop": ["drop.md"]}},
        "drop_missing_under": ["D:\\AI"], "text_scan_roots": ["D:\\AI"],
        "text_exts": [".md", ".py", ".bat"], "text_skip_dirs": ["logs"], "text_skip_files": ["PROJECT_HISTORY.md"],
        "text_skip_suffixes": [".log"],
        "sandbox": {"root": tmp, "map": [["D:\\AI", AI], ["C:\\Users\\demo\\FakeDesktop", DESK], ["C:\\Users\\demo", HOME]]},
    }
    cpath = os.path.join(tmp, "cfg.json")
    write(cpath, json.dumps(cfg, ensure_ascii=False, indent=2))
    return cpath


def check(cond, msg, fails):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


H1 = "a1111111-1111-1111-1111-111111111111"   # Atlas（名字带大写），要搬进 general
J1 = "a2222222-2222-2222-2222-222222222222"   # zulu，要搬
J2 = "a3333333-3333-3333-3333-333333333333"   # zulu2，另一个项目，不搬、不能被 zulu 的规则碰到
G1 = "a4444444-4444-4444-4444-444444444444"   # general 自己的会话，不动


def build_subdir(tmp: str) -> str:
    """搬进子目录的假环境：D:\\AI 下的 Atlas、zulu、media 搬到 D:\\AI\\general\\<原名>；zulu2 和 general 本身不动。"""
    AI = os.path.join(tmp, "AI")
    HOME = os.path.join(tmp, "home")
    DESK = os.path.join(tmp, "desktop")
    proj = os.path.join(HOME, ".claude", "projects")
    for d in ["Atlas", "zulu", "zulu2", "media", "general"]:
        os.makedirs(os.path.join(AI, d))
    write(os.path.join(AI, "Atlas", "PROJECT_STATE.md"),
          "放在 D:\\AI\\Atlas\\a.md、D:/AI/Atlas/b、d:\\ai\\atlas\\low、`D:\\AI\\Atlas`。\r\n"
          "别的项目 D:\\AI\\zulu2\\keep 和 D:\\AI\\general\\keep 不动。\r\n")
    write(os.path.join(AI, "Atlas", "PROJECT_HISTORY.md"), "历史：D:\\AI\\Atlas\\old\n")
    write(os.path.join(AI, "zulu", "build.json"), '{"p": "D:\\\\AI\\\\zulu\\\\app", "q": "D:\\\\AI\\\\zulu2\\\\x"}\n')
    write(os.path.join(AI, "zulu", "go.bat"), "cd /d D:\\AI\\zulu\\app\r\n中文\r\n".encode("gbk"))
    write(os.path.join(AI, "media", "run.py"), 'ROOT = r"D:\\AI\\media"\nATLAS = r"D:\\AI\\Atlas\\out"\n')
    write(os.path.join(AI, "general", "PROJECT_STATE.md"), "general 自己：D:\\AI\\general\\Atlas 已经是新位置，D:\\AI\\zulu 是旧位置。\n")
    write(os.path.join(AI, "zulu2", "x.md"), "D:\\AI\\zulu2\\x 和 D:\\AI\\zulu\\z\n")
    for name, sid, cwd in [("Atlas", H1, "D:\\AI\\Atlas"), ("zulu", J1, "D:\\AI\\zulu"),
                           ("zulu2", J2, "D:\\AI\\zulu2"), ("general", G1, "D:\\AI\\general")]:
        d = os.path.join(proj, "D--AI-" + name)
        write(os.path.join(d, sid + ".jsonl"), jl(
            {"type": "user", "cwd": cwd, "sessionId": sid, "message": {"content": "看 " + cwd + "\\x"}},
            {"type": "assistant", "cwd": cwd + "\\app", "sessionId": sid},
            {"type": "file-history-snapshot", "snapshot": {"trackedFileBackups": {
                cwd + "\\a.md": {"realParentDir": cwd, "backupFileName": "x@v1"}}}}))
        write(os.path.join(d, sid, "subagents", "agent-a.jsonl"), jl({"type": "user", "cwd": cwd, "sessionId": sid}))
    write(os.path.join(proj, "D--AI-Atlas", "memory", "MEMORY.md"), "- [A](a.md) — 在 D:\\AI\\Atlas\n")
    write(os.path.join(proj, "D--AI-Atlas", "memory", "a.md"),
          "见 C:\\Users\\demo\\.claude\\projects\\D--AI-Atlas\\memory\\a.md 和 D:\\AI\\Atlas\\x\n")
    write(os.path.join(proj, "D--AI-zulu2", "memory", "b.md"), "zulu2 的记忆 D:\\AI\\zulu2\n")
    sb = os.path.join(DESK, "claude-code-sessions", ACCT, ORG)
    for sid, cwd in [(H1, "D:\\AI\\Atlas"), (J1, "D:\\AI\\zulu"), (J2, "D:\\AI\\zulu2"), (G1, "D:\\AI\\general")]:
        o = {"sessionId": "local_" + sid, "cliSessionId": sid, "cwd": cwd, "originCwd": cwd, "title": "t" + sid[1],
             "gitAnchors": [{"gitRoot": cwd, "commonDir": cwd + "\\.git"}], "writtenBranches": [cwd + "\x00main"],
             "postTurnSummary": {"t": "写到了 " + cwd}}
        write(os.path.join(sb, f"local_{sid}.json"), json.dumps(o, ensure_ascii=False, separators=(",", ":")))
    ep = {
        f"code-projects-order.{ACCT}": ["D:\\AI\\general", "D:\\AI\\Atlas", "D:\\AI\\zulu2", "D:\\AI\\zulu", "D:\\AI\\media"],
        f"epitaxy-folder-permission-mode.{ACCT}": {"D:\\AI\\Atlas": "auto", "D:\\AI\\zulu2": "x"},
        f"epitaxy-perm-mode-acks.{ACCT}": ["D:\\AI\\zulu:bypassPermissions", "D:\\AI\\zulu2:auto"],
    }
    write(os.path.join(DESK, "claude_desktop_config.json"),
          json.dumps({"mcpServers": {}, "preferences": {"epitaxyPrefs": ep}}, ensure_ascii=False, indent=2))
    gw = {"worktrees": {}, "schemaVersion": 2,
          "untrackedDirGc": {"sightings": {}, "roots": {}, "cwds": {"D:\\AI\\Atlas": 1, "D:\\AI\\zulu2": 1, "D:\\AI\\general": 1}},
          "originUrls": {"d:\\ai\\atlas": {"commonDir": "d:\\ai\\atlas\\.git", "url": ""},
                         "d:\\ai\\zulu2": {"commonDir": "d:\\ai\\zulu2\\.git", "url": ""}}}
    write(os.path.join(DESK, "git-worktrees.json"), json.dumps(gw, ensure_ascii=False, indent="\t") + "\n")
    cj = {"numStartups": 1, "projects": {"D:\\AI\\Atlas": {"a": 1}, "D:/AI/Atlas": {"a": 2}, "D:\\AI\\zulu": {},
          "D:\\AI\\zulu2": {}, "D:\\AI\\general": {}, "D:/AI/media": {}}}
    write(os.path.join(HOME, ".claude.json"), json.dumps(cj, ensure_ascii=False, indent=2))
    write(os.path.join(HOME, ".claude", "history.jsonl"),
          jl({"display": "x", "project": "D:\\AI\\zulu"}, {"display": "y", "project": "D:\\AI\\zulu2"}))
    cfg = {
        "root": "D:\\AI", "home": "C:\\Users\\demo", "desktop_dir": "C:\\Users\\demo\\FakeDesktop",
        "backup_roots": {os.path.splitdrive(tmp)[0].rstrip(":"): os.path.join(tmp, "backup")},
        # 三种写法混用：相对 root 的子路径、完整路径
        "rename": {"Atlas": "general\\Atlas", "D:\\AI\\zulu": "D:\\AI\\general\\zulu", "media": "general/media"},
        "retire_dirs": [], "keep_sessions": {}, "orphan_encoded_dirs": [],
        "memory": {}, "drop_missing_under": ["D:\\AI"], "text_scan_roots": ["D:\\AI"],
        "text_exts": [".md", ".py", ".bat", ".json"], "text_skip_dirs": ["logs"], "text_skip_files": ["PROJECT_HISTORY.md"],
        "text_skip_suffixes": [".log"],
        "sandbox": {"root": tmp, "map": [["D:\\AI", AI], ["C:\\Users\\demo\\FakeDesktop", DESK], ["C:\\Users\\demo", HOME]]},
    }
    cpath = os.path.join(tmp, "cfg.json")
    write(cpath, json.dumps(cfg, ensure_ascii=False, indent=2))
    return cpath


def subdir_tests(fails: list[str]):
    print("\n搬进子目录：映射器单元")
    pm = rp.PathMapper([("D:\\AI\\zulu", "D:\\AI\\general\\zulu"), ("D:\\AI\\Atlas", "D:\\AI\\general\\Atlas")])
    check(pm.map("D:\\AI\\zulu\\app") == "D:\\AI\\general\\zulu\\app", "结构化：子目录跟着搬", fails)
    check(pm.map("D:/AI/zulu") == "D:/AI/general/zulu", "结构化：正斜杠写法保持正斜杠", fails)
    check(pm.map("d:\\ai\\ATLAS\\x") == "d:\\ai\\general\\Atlas\\x", "结构化：大小写不同也认，开头相同的各级保留原写法", fails)
    check(pm.map("D:\\AI\\zulu2\\x") == "D:\\AI\\zulu2\\x", "结构化：zulu2 不被 zulu 的规则碰到", fails)
    check(pm.map("D:\\AI\\general") == "D:\\AI\\general" and pm.map("D:\\AI\\general\\zulu") == "D:\\AI\\general\\zulu",
          "结构化：general 本身和已在新位置的路径不动", fails)
    tm = rp.TextMapper([("D:\\AI\\zulu", "D:\\AI\\general\\zulu")])
    out, n, amb = tm.sub("a D:\\AI\\zulu\\x b D:/AI/zulu/y c D:\\\\AI\\\\zulu\\\\z d D:\\AI\\zulu2\\k e `D:\\AI\\zulu` f")
    check(out == "a D:\\AI\\general\\zulu\\x b D:/AI/general/zulu/y c D:\\\\AI\\\\general\\\\zulu\\\\z d D:\\AI\\zulu2\\k e `D:\\AI\\general\\zulu` f"
          and n == 4 and len(amb) == 1, "文本：反斜杠 / 正斜杠 / JSON 转义写法都换成完整新路径，zulu2 记为待人工看", fails)
    check(rp.find_any_old("D:\\AI\\zulu2\\x", ["D:\\AI\\zulu"]) is False
          and rp.find_any_old("x D:\\AI\\zulu\\x", ["D:\\AI\\zulu"]) is True, "残留检测：zulu 不匹配 zulu2", fails)

    print("搬进子目录：配置校验")
    bad = {
        "新路径在旧目录里面": [("D:\\AI\\a", "D:\\AI\\a\\b")],
        "新路径是旧目录的上级": [("D:\\AI\\a\\b", "D:\\AI\\a")],
        "跨盘": [("D:\\AI\\a", "E:\\AI\\a")],
        "目录名含非法字符": [("D:\\AI\\a", "D:\\AI\\x:y")],
        "保留设备名": [("D:\\AI\\a", "D:\\AI\\general\\con")],
        "以点结尾": [("D:\\AI\\a", "D:\\AI\\b.")],
        "新旧相同": [("D:\\AI\\a", "d:\\ai\\A")],
        "两条新路径相同": [("D:\\AI\\a", "D:\\AI\\g\\x"), ("D:\\AI\\b", "D:\\AI\\g\\x")],
        "新路径落在另一条旧目录里": [("D:\\AI\\a", "D:\\AI\\b\\a"), ("D:\\AI\\b", "D:\\AI\\c")],
    }
    for why, pairs in bad.items():
        try:
            rp.validate_rename(pairs)
            check(False, f"该拒绝：{why}", fails)
        except rp.MigrationError:
            check(True, f"拒绝：{why}", fails)
    try:
        rp.validate_rename([("D:\\AI\\Atlas", "D:\\AI\\general\\Atlas"), ("D:\\AI\\x", "D:\\AI\\general\\Y z")])
        check(True, "带大写、带空格的名字合法，搬进别的目录合法", fails)
    except rp.MigrationError as e:
        check(False, f"带大写 / 空格的合法名被拒：{e}", fails)

    print("搬进子目录：plan / apply / verify / rollback")
    tmp = tempfile.mkdtemp(prefix="rename-selftest-sub-")
    old_cwd = os.getcwd()
    try:
        cpath = build_subdir(tmp)
        runs = os.path.join(tmp, "runs")
        AI, HOME, DESK = (os.path.join(tmp, x) for x in ("AI", "home", "desktop"))
        proj = os.path.join(HOME, ".claude", "projects")
        before = tree(AI, HOME, DESK)
        check(rp.main(["plan", "--config", cpath, "--runs-dir", runs]) == 0, "预演没有阻止执行的问题", fails)
        plan = json.load(open(os.path.join(runs, sorted(os.listdir(runs))[-1], "plan.json"), encoding="utf-8"))
        check(sorted(s["dst_dir"] for s in plan["sessions"]) ==
              ["D--AI-general-Atlas", "D--AI-general-zulu"], "预演：会话挂到子目录的编码目录，zulu2 和 general 的不在内", fails)
        check(rp.main(["apply", "--config", cpath, "--runs-dir", runs, "--yes"]) == 0, "执行成功、核对通过", fails)
        run = sorted(x for x in os.listdir(runs) if x.endswith("-apply"))[-1]
        g = os.path.join(AI, "general")
        check(all(os.path.isdir(os.path.join(g, n)) for n in ("Atlas", "zulu", "media"))
              and not any(os.path.exists(os.path.join(AI, n)) for n in ("Atlas", "zulu", "media")), "三个目录都搬进 general", fails)
        check(os.path.isdir(os.path.join(AI, "zulu2")), "zulu2 没动", fails)
        hd = os.path.join(proj, "D--AI-general-Atlas")
        rec = [json.loads(l) for l in open(os.path.join(hd, H1 + ".jsonl"), encoding="utf-8")]
        check(rec[0]["cwd"] == "D:\\AI\\general\\Atlas" and rec[0]["message"]["content"] == "看 D:\\AI\\Atlas\\x", "会话 cwd 改成完整新路径、正文不改", fails)
        check("D:\\AI\\general\\Atlas\\a.md" in rec[2]["snapshot"]["trackedFileBackups"]
              and rec[2]["snapshot"]["trackedFileBackups"]["D:\\AI\\general\\Atlas\\a.md"]["realParentDir"] == "D:\\AI\\general\\Atlas",
              "file-history 快照改了", fails)
        sub = json.loads(open(os.path.join(hd, H1, "subagents", "agent-a.jsonl"), encoding="utf-8").readline())
        check(sub["cwd"] == "D:\\AI\\general\\Atlas", "子代理记录跟着走", fails)
        jcs = [json.loads(l).get("cwd") for l in open(os.path.join(proj, "D--AI-general-zulu", J1 + ".jsonl"), encoding="utf-8")][:2]
        check(jcs == ["D:\\AI\\general\\zulu", "D:\\AI\\general\\zulu\\app"], "zulu 会话里子目录的 cwd 保留子目录", fails)
        check(os.path.exists(os.path.join(proj, "D--AI-zulu2", J2 + ".jsonl"))
              and json.loads(open(os.path.join(proj, "D--AI-zulu2", J2 + ".jsonl"), encoding="utf-8").readline())["cwd"] == "D:\\AI\\zulu2",
              "zulu2 的会话原样", fails)
        check(os.path.exists(os.path.join(proj, "D--AI-general", G1 + ".jsonl")), "general 自己的会话没动", fails)
        check(os.path.exists(os.path.join(proj, "D--AI-zulu2", "memory", "b.md")), "zulu2 的记忆没动", fails)
        mem = open(os.path.join(hd, "memory", "MEMORY.md"), encoding="utf-8").read()
        ma = open(os.path.join(hd, "memory", "a.md"), encoding="utf-8").read()
        check("D:\\AI\\general\\Atlas" in mem and "projects\\D--AI-general-Atlas\\memory" in ma and "D:\\AI\\general\\Atlas\\x" in ma,
              "记忆跟着 Atlas 走、里面的路径和编码目录名都换了", fails)
        sbdir = os.path.join(DESK, "claude-code-sessions", ACCT, ORG)
        o = json.load(open(os.path.join(sbdir, f"local_{H1}.json"), encoding="utf-8"))
        check(o["cwd"] == "D:\\AI\\general\\Atlas" and o["gitAnchors"][0]["commonDir"] == "D:\\AI\\general\\Atlas\\.git"
              and o["writtenBranches"] == ["D:\\AI\\general\\Atlas\x00main"], "侧栏字段改成完整新路径", fails)
        o2 = json.load(open(os.path.join(sbdir, f"local_{J2}.json"), encoding="utf-8"))
        o3 = json.load(open(os.path.join(sbdir, f"local_{G1}.json"), encoding="utf-8"))
        check(o2["cwd"] == "D:\\AI\\zulu2" and o3["cwd"] == "D:\\AI\\general", "zulu2、general 的侧栏记录不动", fails)
        dc = json.load(open(os.path.join(DESK, "claude_desktop_config.json"), encoding="utf-8"))["preferences"]["epitaxyPrefs"]
        check(dc[f"code-projects-order.{ACCT}"] == ["D:\\AI\\general", "D:\\AI\\general\\Atlas", "D:\\AI\\zulu2",
                                                    "D:\\AI\\general\\zulu", "D:\\AI\\general\\media"], "项目顺序", fails)
        check(dc[f"epitaxy-perm-mode-acks.{ACCT}"] == ["D:\\AI\\general\\zulu:bypassPermissions", "D:\\AI\\zulu2:auto"], "权限确认记录", fails)
        gw = json.load(open(os.path.join(DESK, "git-worktrees.json"), encoding="utf-8"))
        check("d:\\ai\\general\\atlas" in gw["originUrls"] and "d:\\ai\\zulu2" in gw["originUrls"]
              and gw["originUrls"]["d:\\ai\\general\\atlas"]["commonDir"] == "d:\\ai\\general\\atlas\\.git", "originUrls 小写路径", fails)
        cj = json.load(open(os.path.join(HOME, ".claude.json"), encoding="utf-8"))
        check(list(cj["projects"]) == ["D:\\AI\\general\\Atlas", "D:/AI/general/Atlas", "D:\\AI\\general\\zulu",
                                       "D:\\AI\\zulu2", "D:\\AI\\general", "D:/AI/general/media"],
              ".claude.json 键两种斜杠都换、zulu2 和 general 原样", fails)
        hs = [json.loads(l)["project"] for l in open(os.path.join(HOME, ".claude", "history.jsonl"), encoding="utf-8")]
        check(hs == ["D:\\AI\\general\\zulu", "D:\\AI\\zulu2"], "history.jsonl", fails)
        st = open(os.path.join(g, "Atlas", "PROJECT_STATE.md"), "rb").read().decode("utf-8")
        check("D:\\AI\\general\\Atlas\\a.md" in st and "D:/AI/general/Atlas/b" in st and "d:\\ai\\general\\Atlas\\low" in st
              and "`D:\\AI\\general\\Atlas`" in st and "D:\\AI\\zulu2\\keep" in st and "D:\\AI\\general\\keep" in st
              and "\r\n" in st and "general\\general" not in st, "文本：各种写法都换成完整新路径，zulu2 / general 的不动，保留 CRLF", fails)
        check("D:\\AI\\Atlas\\old" in open(os.path.join(g, "Atlas", "PROJECT_HISTORY.md"), encoding="utf-8").read(), "HISTORY 不改", fails)
        bj = open(os.path.join(g, "zulu", "build.json"), encoding="utf-8").read()
        check('"D:\\\\AI\\\\general\\\\zulu\\\\app"' in bj and "D:\\\\AI\\\\zulu2\\\\x" in bj, "JSON 转义写法换了、zulu2 没动", fails)
        check(open(os.path.join(g, "zulu", "go.bat"), "rb").read() == "cd /d D:\\AI\\general\\zulu\\app\r\n中文\r\n".encode("gbk"), "GBK 的 bat 按原编码改", fails)
        py = open(os.path.join(g, "media", "run.py"), encoding="utf-8").read()
        check('r"D:\\AI\\general\\media"' in py and 'r"D:\\AI\\general\\Atlas\\out"' in py, "别的项目脚本里的引用也换了", fails)
        gs = open(os.path.join(g, "PROJECT_STATE.md"), encoding="utf-8").read()
        check("D:\\AI\\general\\Atlas 已经是新位置" in gs and "D:\\AI\\general\\zulu 是旧位置" in gs, "general 里的旧路径换了、已是新路径的不重复换", fails)
        check(rp.main(["verify", "--config", cpath, "--runs-dir", runs]) == 0, "verify 通过", fails)

        # 脚本 / 工作目录 / 运行记录在要搬的目录里：必须拒绝（Windows 上目录里有打开的文件或工作目录，rename 会失败）
        rp_cfg = rp.load_config(cpath)
        # 回滚前目录已经搬走，这里用新位置当"要搬的目录"来模拟：把 maps 的旧路径指到现有目录
        fake = [rp.Mapping("D:\\AI\\general\\Atlas", "D:\\AI\\x", "general\\Atlas", "x")]
        os.chdir(os.path.join(g, "Atlas"))
        c1 = rp.self_conflicts(rp_cfg, fake, runs, cpath)
        os.chdir(old_cwd)
        c2 = rp.self_conflicts(rp_cfg, fake, os.path.join(g, "Atlas", "runs"), cpath)
        c3 = rp.self_conflicts(rp_cfg, fake, runs, cpath)
        check(len(c1) == 1 and "当前工作目录" in c1[0] and len(c2) == 1 and "运行记录" in c2[0] and c3 == [],
              "自检：工作目录 / 运行记录在要搬的目录里报错，在范围外不报", fails)

        check(rp.main(["rollback", "--config", cpath, "--runs-dir", runs, "--run", run]) == 0, "回滚", fails)
        after = tree(AI, HOME, DESK)
        diff = sorted(set(before) ^ set(after)) + sorted(k for k in before if k in after and before[k] != after[k])
        check(not diff, "回滚后与迁移前逐字节一致" + (f"：{diff[:8]}" if diff else ""), fails)
        # 目标已存在 → 预演报问题
        os.makedirs(os.path.join(AI, "general", "zulu"))
        rc = rp.main(["plan", "--config", cpath, "--runs-dir", runs])
        check(rc == 1, "目标目录已存在时预演报阻止执行的问题", fails)
    finally:
        os.chdir(old_cwd)
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    fails: list[str] = []
    tmp = tempfile.mkdtemp(prefix="rename-selftest-")
    try:
        cpath = build(tmp)
        runs = os.path.join(tmp, "runs")
        AI, HOME, DESK = (os.path.join(tmp, x) for x in ("AI", "home", "desktop"))
        proj = os.path.join(HOME, ".claude", "projects")
        before = tree(AI, HOME, DESK)

        print("plan")
        rc = rp.main(["plan", "--config", cpath, "--runs-dir", runs])
        check(rc == 0, "预演没有阻止执行的问题", fails)

        print("apply --only beta（试点）")
        rc = rp.main(["apply", "--config", cpath, "--runs-dir", runs, "--only", "beta", "--yes"])
        check(rc == 0, "试点执行成功", fails)
        check(os.path.isdir(os.path.join(AI, "beta")) and os.path.isdir(os.path.join(AI, "甲乙丙丁")), "试点只改了 beta", fails)
        check(os.path.exists(os.path.join(proj, "D--AI-beta", S2 + ".jsonl")), "试点会话搬过去了", fails)
        check(os.path.exists(os.path.join(proj, "D--AI-----", S1 + ".jsonl")), "其他会话没动", fails)
        pilot_run = sorted(x for x in os.listdir(runs) if x.endswith("-apply"))[-1]

        print("apply（全量）")
        import time as _t
        _t.sleep(1.1)  # 保证运行编号不同
        rc = rp.main(["apply", "--config", cpath, "--runs-dir", runs, "--yes"])
        check(rc == 0, "全量执行成功、核对通过", fails)
        full_run = sorted(x for x in os.listdir(runs) if x.endswith("-apply"))[-1]

        a = os.path.join(proj, "D--AI-alpha")
        check(os.path.isdir(os.path.join(AI, "alpha")) and not os.path.exists(os.path.join(AI, "甲乙丙丁")), "目录改名", fails)
        check(not os.path.exists(os.path.join(AI, "旧项目一")), "退役目录移走", fails)
        check(not os.path.exists(os.path.join(proj, "D--AI-----")), "撞名目录迁空后移走", fails)
        check(not os.path.exists(os.path.join(proj, "D--AI-gone")), "无对应目录的编码目录移走", fails)
        recs = [json.loads(l) for l in open(os.path.join(a, S1 + ".jsonl"), encoding="utf-8")]
        check(recs[0]["cwd"] == "D:\\AI\\alpha" and recs[1]["cwd"] == "D:\\AI\\alpha\\sub", "cwd 改了且保留子目录", fails)
        check(recs[0]["message"]["content"] == "看 D:\\AI\\甲乙丙丁\\x", "正文没改", fails)
        check(recs[1]["message"]["content"][0]["thinking"] == "D:\\AI\\甲乙丙丁", "思考块没改", fails)
        tfb = recs[2]["snapshot"]["trackedFileBackups"]
        check("D:\\AI\\alpha\\a.md" in tfb and tfb["rel.md"]["realParentDir"] == "D:\\AI\\alpha\\sub", "file-history 快照改了", fails)
        check(recs[3]["trackingPath"] == "D:\\AI\\alpha\\a.md", "file-history-delta 改了", fails)
        sub = json.loads(open(os.path.join(a, S1, "subagents", "agent-a.jsonl"), encoding="utf-8").readline())
        check(sub["cwd"] == "D:\\AI\\alpha", "子代理记录改了", fails)
        check(open(os.path.join(a, S1, "tool-results", "r.txt"), encoding="utf-8").read() == "输出 D:\\AI\\甲乙丙丁", "工具输出原样搬", fails)
        check(os.path.exists(os.path.join(a, S1 + ".desktop-released.json")), "伴随文件跟着走", fails)
        check(os.path.exists(os.path.join(proj, "D--AI-tool", S4 + ".jsonl")), "例外会话改挂到 tool", fails)
        check(not any(os.path.exists(os.path.join(proj, d, S3 + ".jsonl")) for d in os.listdir(proj)), "退役会话移走", fails)
        check(os.path.exists(os.path.join(proj, "D--AI-eng", S5 + ".jsonl")), "英文项目会话没动", fails)
        mem = open(os.path.join(a, "memory", "MEMORY.md"), encoding="utf-8").read()
        check("drop.md" not in mem and "D:\\AI\\alpha" in mem, "记忆索引去掉删的那条、路径改了", fails)
        check(not os.path.exists(os.path.join(a, "memory", "drop.md")), "要删的记忆没迁", fails)
        check("projects\\D--AI-alpha\\memory" in open(os.path.join(a, "memory", "a.md"), encoding="utf-8").read(), "记忆里的编码目录名改了", fails)
        sbdir = os.path.join(DESK, "claude-code-sessions", ACCT, ORG)
        o1 = json.load(open(os.path.join(sbdir, f"local_{S1}.json"), encoding="utf-8"))
        check(o1["cwd"] == "D:\\AI\\alpha" and o1["gitAnchors"][0]["commonDir"] == "D:\\AI\\alpha\\.git"
              and o1["writtenBranches"] == ["D:\\AI\\alpha\x00main"]
              and o1["sessionPermissionUpdates"][0]["directories"] == ["D:\\AI\\alpha\\sub"], "侧栏字段都改了", fails)
        check(o1["postTurnSummary"]["t"] == "写到了 D:\\AI\\甲乙丙丁", "侧栏里的文字摘要没改", fails)
        check(not os.path.exists(os.path.join(sbdir, f"local_{S3}.json")), "退役会话的侧栏记录移走", fails)
        o4 = json.load(open(os.path.join(sbdir, f"local_{S4}.json"), encoding="utf-8"))
        check(o4["cwd"] == "D:\\AI\\tool", "例外会话的侧栏改挂", fails)
        dc = json.load(open(os.path.join(DESK, "claude_desktop_config.json"), encoding="utf-8"))["preferences"]["epitaxyPrefs"]
        check(dc[f"code-projects-order.{ACCT}"] == ["D:\\AI\\eng", "D:\\AI\\alpha"], "项目顺序改了、删了退役和没目录的", fails)
        check(dc[f"epitaxy-folder-permission-mode.{ACCT}"] == {"D:\\AI\\alpha": "auto", "D:\\AI\\eng": "x"}, "文件夹权限模式", fails)
        check(dc[f"epitaxy-perm-mode-acks.{ACCT}"] == ["D:\\AI\\beta:bypassPermissions", "D:\\AI\\eng:auto"], "权限确认记录", fails)
        raw = open(os.path.join(DESK, "git-worktrees.json"), encoding="utf-8").read()
        gw = json.loads(raw)
        check(raw.startswith("{\n\t") and raw.endswith("\n"), "git-worktrees.json 保持 Tab 缩进", fails)
        check("d:\\ai\\beta" in gw["originUrls"] and gw["originUrls"]["d:\\ai\\beta"]["commonDir"] == "d:\\ai\\beta\\.git", "originUrls 小写路径", fails)
        check(set(gw["untrackedDirGc"]["cwds"]) == {"D:\\AI\\alpha", "D:\\AI\\eng"}, "cwds 改名并删退役", fails)
        cj = json.load(open(os.path.join(HOME, ".claude.json"), encoding="utf-8"))
        check(list(cj["projects"]) == ["D:\\AI\\alpha", "D:/AI/alpha", "D:\\AI\\eng"], ".claude.json 键改名、保持顺序、删退役和没目录的", fails)
        h = json.loads(open(os.path.join(HOME, ".claude", "history.jsonl"), encoding="utf-8").readline())
        check(h["project"] == "D:\\AI\\beta", "history.jsonl", fails)
        st = open(os.path.join(AI, "alpha", "PROJECT_STATE.md"), "rb").read().decode("utf-8")
        check("D:\\AI\\alpha\\data" in st and "D:/AI/alpha/b" in st and "D:\\AI\\甲乙丙丁改名后" in st and "\r\n" in st,
              "STATE 改路径、不改紧跟汉字的、保留 CRLF", fails)
        check("D:\\AI\\甲乙丙丁" in open(os.path.join(AI, "alpha", "PROJECT_HISTORY.md"), encoding="utf-8").read(), "HISTORY 不改", fails)
        run_py = open(os.path.join(AI, "eng", "run.py"), encoding="utf-8").read()
        check('r"D:\\AI\\beta\\data"' in run_py and '"D:\\\\AI\\\\beta\\\\y"' in run_py, "英文项目脚本里的引用改了（含转义写法）", fails)
        check(open(os.path.join(AI, "eng", "go.bat"), "rb").read() == "cd /d D:\\AI\\beta\r\n".encode("gbk"), "GBK 的 bat 按原编码改", fails)
        snap_ok = any("snapshot" in dp for dp, _d, _f in os.walk(os.path.join(tmp, "backup")))
        check(snap_ok, "有硬链接快照", fails)

        print("verify")
        check(rp.main(["verify", "--config", cpath, "--runs-dir", runs]) == 0, "核对通过", fails)

        print("apply（第二轮：alpha 再改名为 gamma，并从第一轮的备份里恢复退役会话 S3 挂到 gamma）")
        _t.sleep(1.1)
        c2 = json.load(open(cpath, encoding="utf-8"))
        backup_run = os.path.join(tmp, "backup", full_run.split("-apply")[0])
        c2.update({"rename": {"alpha": "gamma"}, "retire_dirs": [], "keep_sessions": {}, "orphan_encoded_dirs": [],
                   "memory": {}, "restore": [{"backup": backup_run, "sessions": [S3], "to": "gamma"}]})
        cpath2 = os.path.join(tmp, "cfg2.json")
        write(cpath2, json.dumps(c2, ensure_ascii=False, indent=2))
        rc = rp.main(["apply", "--config", cpath2, "--runs-dir", runs, "--yes"])
        check(rc == 0, "第二轮执行成功、核对通过", fails)
        second_run = sorted(x for x in os.listdir(runs) if x.endswith("-apply"))[-1]
        g = os.path.join(proj, "D--AI-gamma")
        r3 = json.loads(open(os.path.join(g, S3 + ".jsonl"), encoding="utf-8").readline()) if os.path.exists(os.path.join(g, S3 + ".jsonl")) else {}
        check(r3.get("cwd") == "D:\\AI\\gamma", "退役会话从备份恢复、cwd 改挂到 gamma", fails)
        check(os.path.exists(os.path.join(g, S1 + ".jsonl")) and os.path.isdir(os.path.join(AI, "gamma")), "alpha 再改名为 gamma", fails)
        check(any(f.startswith(S3) for _dp, _d, fs in os.walk(backup_run) for f in fs), "备份里的原件还在（恢复是复制）", fails)
        o3 = json.load(open(os.path.join(sbdir, f"local_{S3}.json"), encoding="utf-8")) if os.path.exists(os.path.join(sbdir, f"local_{S3}.json")) else {}
        check(o3.get("cwd") == "D:\\AI\\gamma" and o3.get("gitAnchors", [{}])[0].get("gitRoot") == "D:\\AI\\gamma",
              "侧栏记录从备份恢复并改挂", fails)

        print("rollback（第二轮、全量、试点依次回滚）")
        check(rp.main(["rollback", "--config", cpath2, "--runs-dir", runs, "--run", second_run]) == 0, "第二轮回滚", fails)
        check(rp.main(["rollback", "--config", cpath, "--runs-dir", runs, "--run", full_run]) == 0, "全量回滚", fails)
        check(rp.main(["rollback", "--config", cpath, "--runs-dir", runs, "--run", pilot_run]) == 0, "试点回滚", fails)
        after = tree(AI, HOME, DESK)
        diff = sorted(set(before) ^ set(after)) + sorted(k for k in before if k in after and before[k] != after[k])
        check(not diff, "回滚后与迁移前逐字节一致" + (f"：{diff[:8]}" if diff else ""), fails)

        print("出错自动还原")
        orig = rp.Executor.replace

        def boom(self, phys, data):
            if phys.endswith(".claude.json"):
                raise RuntimeError("模拟中途出错")
            return orig(self, phys, data)
        rp.Executor.replace = boom
        try:
            _t.sleep(1.1)
            rc = rp.main(["apply", "--config", cpath, "--runs-dir", runs, "--yes"])
        finally:
            rp.Executor.replace = orig
        check(rc == 2, "出错时返回失败", fails)
        after2 = tree(AI, HOME, DESK)
        diff = sorted(set(before) ^ set(after2)) + sorted(k for k in before if k in after2 and before[k] != after2[k])
        check(not diff, "出错后自动还原到迁移前" + (f"：{diff[:8]}" if diff else ""), fails)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    subdir_tests(fails)
    print(f"\n{'全部通过' if not fails else f'{len(fails)} 项失败'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
