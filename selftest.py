"""
selftest.py — 用合成的临时目录验证 core.py 的探测/计划/迁移/撤销/回滚/进程识别/命令行逻辑。

绝不触碰真实 Claude 目录：全程在系统临时目录里造一棵假的 claude-code-sessions 树。
进程相关的测试给 PowerShell 调用打桩，只喂合成的进程列表，不会结束或启动任何进程。
运行：  python selftest.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import platform
import shutil
import sys
import tempfile
import time
import uuid

import core

# Windows 控制台默认 GBK，重定向为 UTF-8 以便打印中文/符号不崩
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def G(tag: str) -> str:
    """测试用的固定 GUID（账号 / 组织目录名必须是 GUID）。"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "csm-selftest/" + tag))


OLD, NEW, API = G("old"), G("new"), G("api")
WS_OLD, WS_OLD2, WS_NEW, WS_API = G("ws-old"), G("ws-old-2"), G("ws-new"), G("ws-api")
ACT = 1_700_000_000_000  # 默认的 lastActivityAt（毫秒）


def _mk_local(dir_path: str, names: list[str]) -> None:
    os.makedirs(dir_path, exist_ok=True)
    for n in names:
        with open(os.path.join(dir_path, f"local_{n}.json"), "w", encoding="utf-8") as f:
            f.write('{"sessionId":"local_%s","cliSessionId":"%s","lastActivityAt":%d,"title":"t-%s"}' % (n, n, ACT, n))


def _mk_session(dir_path: str, local_id: str, cli: str = "", prior=None, raw: str = "", act=ACT) -> str:
    """按真实结构造一个 local_<id>.json（顶层 sessionId / cliSessionId / priorCliSessionIds / lastActivityAt）。"""
    os.makedirs(dir_path, exist_ok=True)
    path = os.path.join(dir_path, f"local_{local_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        if raw:
            f.write(raw)
        else:
            data = {"sessionId": f"local_{local_id}", "cliSessionId": cli or f"cli-{local_id}",
                    "priorCliSessionIds": prior or [], "title": f"t-{local_id}"}
            if act is not None:
                data["lastActivityAt"] = act
            json.dump(data, f)
    return path


def _mk_tomb(dir_path: str, the_id: str) -> None:
    """桌面端删除会话时写的墓碑：deleted_<id>，内容是 13 位毫秒时间戳。"""
    with open(os.path.join(dir_path, core.DELETED_PREFIX + the_id), "w", encoding="ascii") as f:
        f.write("1760000000000")


def _touch_newer(path: str) -> None:
    time.sleep(0.05)
    os.utime(path, None)


def _names(dir_path: str) -> set[str]:
    return {os.path.basename(p) for p in core._list_local_json(dir_path)}


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _snapshot(root: str) -> list:
    """目录树快照（相对路径、大小、修改时间），用来确认「只预览」时什么都没变。"""
    out = []
    for dirpath, dirs, files in os.walk(root):
        for n in sorted(dirs + files):
            p = os.path.join(dirpath, n)
            st = os.stat(p)
            out.append((os.path.relpath(p, root), st.st_size if os.path.isfile(p) else -1, st.st_mtime_ns))
    return sorted(out)


def _cli(args: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = core.cli_main(args)
    return rc, buf.getvalue()


def build_fixture(base: str) -> None:
    """
    造两个账号：
      OLD 账号：主 ws 有 5 个 session（a,b,c,d,e）
      NEW 账号：主 ws 有 2 个 session（d,e）——与 OLD 重叠 2 个
    NEW 的 mtime 最新（= 目标新号）。
    """
    _mk_local(os.path.join(base, OLD, WS_OLD), ["a", "b", "c", "d", "e"])
    _mk_local(os.path.join(base, NEW, WS_NEW), ["d", "e"])
    # 再给 OLD 账号加一个次要 workspace（少 session），以及带 BACKUP 名的干扰目录
    _mk_local(os.path.join(base, OLD, WS_OLD2), ["z"])
    os.makedirs(os.path.join(base, OLD, WS_OLD + core.BACKUP_MARKER + "x"), exist_ok=True)
    _touch_newer(os.path.join(base, NEW))  # 确保 NEW 的 mtime 比 OLD 新


def check(cond: bool, msg: str) -> None:
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        raise AssertionError(msg)


# --------------------------------------------------------------------------- #
# 进程识别用的合成数据：结构仿照本机实测的 Get-Process -Name Claude 输出（14 个同名进程：
# 桌面端内置 CLI 与商店版桌面端交错，且列表第一个就是 CLI），另加用户自装的 CLI 与无路径进程。
# 路径全是虚构的，即便打桩失效也匹配不到本机任何真实进程。
# --------------------------------------------------------------------------- #
LAD = r"C:\Users\TestUser\AppData\Local"
PKG_DIR = r"C:\Program Files\WindowsApps\Claude_9.9.9.0_x64__testpub00000"
DESKTOP = PKG_DIR + r"\app\Claude.exe"
BUILTIN_CLI = r"C:\Users\TestUser\AppData\Roaming\Claude\claude-code\2.1.295\0123456789ab\claude.exe"
LOCAL_BIN_CLI = r"C:\Users\TestUser\.local\bin\claude.exe"
NPM_CLI = r"C:\Users\TestUser\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe"
CLI_PIDS = [1104, 11208, 23516, 31740]
DESKTOP_PIDS = [2904, 3312, 4428, 5516, 6100, 9472, 26180, 27404, 29012, 30036]


def _ps_output(apps: bool = True, start: bool = False, desktop: bool = True) -> str:
    procs = [(p, BUILTIN_CLI) for p in CLI_PIDS] + ([(p, DESKTOP) for p in DESKTOP_PIDS] if desktop else [])
    procs.sort()
    procs += [(40001, LOCAL_BIN_CLI), (40002, NPM_CLI), (40003, "")]  # 最后一个：无权限读路径
    lines = [f"P\t{pid}\t{path}" for pid, path in procs]
    if apps:
        for app_id, exe in (("Claude", r"app\Claude.exe"),
                            ("SshAskpass", r"app\resources\claude-ssh-askpass.exe"),
                            ("SshProxy", r"app\resources\claude-ssh-proxy.exe")):
            lines.append(f"A\tClaude_testpub00000\t{PKG_DIR}\t{app_id}\t{exe}")
    if start:
        lines += ["S\tclaude.ai-12345678_abcdefghijk!App", "S\tClaude_testpub00000!Claude",
                  "S\tMicrosoft.WindowsCalculator_8wekyb3d8bbwe!App"]
    return "\n".join(lines)


def test_process_control(tmp: str) -> None:
    print("[13] 进程识别：只认桌面端，CLI 一律不碰；读不到路径的单独计数")
    st = core.parse_claude_status(_ps_output(), localappdata=LAD)
    check(st.running and st.pids == sorted(DESKTOP_PIDS), f"只挑出 10 个桌面端 PID（实际 {st.pids}）")
    check(not set(st.pids) & set(CLI_PIDS + [40001, 40002, 40003]), "内置 CLI / 自装 CLI / 无路径进程都不在结束名单里")
    check(st.ignored == 6 and st.unknown == 1,
          f"6 个确认不是桌面端，1 个读不到路径（实际 ignored={st.ignored} unknown={st.unknown}）")
    check(st.relaunch_kind == "aumid" and st.relaunch_value == "Claude_testpub00000!Claude",
          f"重启方式取包清单里 app\\Claude.exe 对应的 AUMID（实际 {st.relaunch_kind} {st.relaunch_value}）")
    check(st.exe_paths == [DESKTOP], "复核用的路径只有桌面端 exe")

    st2 = core.parse_claude_status(_ps_output(desktop=False), localappdata=LAD)
    check(not st2.running and st2.pids == [] and st2.unknown == 1,
          "只有 CLI 和无路径进程时：确认的桌面端没在跑，但读不到路径的仍计为 unknown（迁移前会提示）")

    st3 = core.parse_claude_status(_ps_output(apps=False, start=True), localappdata=LAD)
    check(st3.relaunch_value == "Claude_testpub00000!Claude", f"取不到包清单时按包目录名到开始菜单兜底（实际 {st3.relaunch_value}）")

    squirrel = LAD + r"\AnthropicClaude\app-0.9.3\claude.exe"
    st4 = core.parse_claude_status(f"P\t7001\t{squirrel}\nP\t7002\t{BUILTIN_CLI}\nP\t7003\tD:\\tools\\claude.exe",
                                   localappdata=LAD)
    check(st4.pids == [7001] and st4.relaunch_kind == "path" and st4.relaunch_value == squirrel,
          "非商店版：%LOCALAPPDATA%\\AnthropicClaude 下的 claude.exe 用路径重启；来历不明的 claude.exe 不认")
    check(core.is_desktop_exe(LAD + r"\Programs\Claude\Claude.exe", LAD), "%LOCALAPPDATA%\\Programs\\Claude 也认")
    check(not core.is_desktop_exe(LAD + r"\AnthropicClaude\claude-code\1.0\claude.exe", LAD), "安装版目录里的 claude-code 不认")

    real_ps = core._run_powershell
    real_popen = core.subprocess.Popen
    real_startfile = getattr(core.os, "startfile", None)
    scripts: list[str] = []
    launched: list = []
    reply = {"out": "T\t10\t0"}

    def fake_ps(script: str, timeout: int = 20):
        scripts.append(script)
        if "Stop-Process" in script:
            return 0, reply["out"], ""
        return 0, _ps_output(), ""

    core._run_powershell = fake_ps
    core.subprocess.Popen = lambda args, **kw: launched.append(("popen", list(args)))
    core.os.startfile = lambda path: launched.append(("startfile", path))
    try:
        check(core._run_powershell is fake_ps, "PowerShell 已打桩（不会真的结束进程）")
        if platform.system() == "Windows":
            st5 = core.get_claude_status()
            check(st5.pids == sorted(DESKTOP_PIDS), "get_claude_status 经打桩输出也只认桌面端")
            scripts.clear()
            ok, msg = core.close_claude(st)
            script = scripts[-1]
            check(ok and "已结束 10 个" in msg, f"全部退出时报告成功（{msg}）")
            check("$ids = @(" + ",".join(map(str, sorted(DESKTOP_PIDS))) + ")" in script, "Stop-Process 只针对桌面端 PID")
            check(not any(str(p) in script for p in CLI_PIDS + [40003]), "结束脚本里没有 CLI 或读不到路径的进程")
            check("$allowed = @('" + DESKTOP + "')" in script, "结束前按单引号路径复核")
            check("Start-Sleep" in script and "$deadline" in script, "结束后轮询等进程退出（不是固定 sleep）")
            reply["out"] = "T\t10\t2"
            ok, msg = core.close_claude(st)
            check(not ok and "仍有 2 个" in msg, "超时仍有进程没退出时报告失败")
            odd = r"C:\Users\a'b$env:x\AppData\Local\AnthropicClaude\app-1\claude.exe"
            core.close_claude(core.ClaudeStatus(True, [7001], "path", odd, odd, [odd]))
            check("'C:\\Users\\a''b$env:x\\AppData\\Local\\AnthropicClaude\\app-1\\claude.exe'" in scripts[-1]
                  and '"' + odd not in scripts[-1], "路径里的 $ 与单引号按 PowerShell 单引号字面量传入，不被展开")

            ok, _ = core.relaunch_claude("aumid", "Claude_testpub00000!Claude")
            check(ok and launched[-1] == ("popen", ["explorer.exe", "shell:AppsFolder\\Claude_testpub00000!Claude"]),
                  "商店版用 explorer shell:AppsFolder\\<AUMID> 重启（参数列表，不经 PowerShell）")
            n = len(launched)
            ok, _ = core.relaunch_claude("aumid", 'x"; Start-Process calc; "')
            check(not ok and len(launched) == n, "不像 AUMID 的值被拒绝")
            exe_dir = os.path.join(tmp, "a'b$x", "AnthropicClaude", "app-1")
            os.makedirs(exe_dir)
            exe = os.path.join(exe_dir, "claude.exe")
            open(exe, "wb").close()
            ok, _ = core.relaunch_claude("path", exe)
            check(ok and launched[-1] == ("startfile", exe), "安装版按原样路径启动（含 $ 与单引号）")
        else:
            print("  SKIP 非 Windows：close/relaunch 只在 Windows 上执行")
    finally:
        core._run_powershell = real_ps
        core.subprocess.Popen = real_popen
        if real_startfile is None:
            del core.os.startfile
        else:
            core.os.startfile = real_startfile


def test_last_login_and_layout(tmp: str) -> None:
    print("[14] 最近登录账号、可能是新账号、目录结构校验")
    root = os.path.join(tmp, "t14", "Claude")
    base = os.path.join(root, "claude-code-sessions")
    old, new, ws_old, ws_new = G("old14"), G("new14"), G("ws-old14"), G("ws-new14")
    _mk_local(os.path.join(base, old, ws_old), ["a", "b", "c"])
    _mk_local(os.path.join(base, old, "notes"), ["n"])                   # 不是 GUID 的组织目录 → 忽略
    os.makedirs(os.path.join(base, "skills-plugin", "x"))               # 不是账号的目录 → 忽略，不报错
    os.makedirs(os.path.join(base, new, ws_new))                         # 新账号：刚建、没有记录
    _touch_newer(os.path.join(base, new))
    cfg = os.path.join(root, "config.json")

    def write_cfg(value) -> None:
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"someOtherSetting": "do-not-read", "lastKnownAccountUuid": value}, f)

    accts = core.list_accounts(base)
    check(sorted(a.name for a in accts) == sorted([old, new]), "skills-plugin 这类非 GUID 目录被忽略，不算账号")
    a_old = next(a for a in accts if a.name == old)
    a_new = next(a for a in accts if a.name == new)
    check([w.name for w in a_old.all_workspaces] == [ws_old], "非 GUID 的组织目录（notes）不算 workspace")
    check(a_new.maybe_new and not a_old.maybe_new, "24 小时内新建、无记录的账号标为「可能是新账号」")
    check(not any(a.last_login for a in accts) and core.choose_target(accts).name == new,
          "没有 config.json 时按原逻辑：目标 = 最近活动的账号")
    write_cfg(old)
    accts = core.list_accounts(base)
    check(core.read_last_account(base) == old, "只读出 lastKnownAccountUuid 这一个字段")
    check(next(a for a in accts if a.name == old).last_login, "config.json 指向的账号标为「最近登录」")
    check(core.choose_target(accts).name == old, "最近登录的账号优先作为默认目标（即使它不是 mtime 最新的）")
    check("最近登录" in next(a for a in accts if a.name == old).marks, "标记文字里有「最近登录」")
    write_cfg("not-a-guid")
    check(core.read_last_account(base) is None and core.choose_target(core.list_accounts(base)).name == new,
          "lastKnownAccountUuid 不是 GUID 时忽略，回到原逻辑")
    with open(cfg, "w", encoding="utf-8") as f:
        f.write("[1, 2")
    check(core.read_last_account(base) is None, "config.json 坏掉时不报错，回到原逻辑")
    notes = core.layout_notes([base])
    check(any("skills-plugin" in n for n in notes), "提示里写明忽略了 skills-plugin")
    odd = os.path.join(tmp, "t14b", "claude-code-sessions")
    os.makedirs(os.path.join(odd, "something-else", "x"))
    check(any("不符合预期" in n for n in core.layout_notes([odd])) and core.list_accounts(odd) == [],
          "整个目录都不是 <GUID>\\<GUID> 结构时提示「不符合预期」，不列出任何账号")
    bad_target = core.Account(name=old, path=os.path.join(base, old), mtime=0,
                              workspace=core.Workspace("notes", os.path.join(base, old, "notes"), 1))
    try:
        core.build_plan(accts, target_account=bad_target, source_accounts=[a_new])
        check(False, "目标不是 <GUID>\\<GUID> 时应停止")
    except core.MigrationError:
        check(True, "目标目录不是 <GUID>\\<GUID> 时停止、不动任何东西")


def test_update_newer(tmp: str) -> None:
    print("[15] 源比目标新时更新；撤销时恢复旧版本")
    base = os.path.join(tmp, "t15", "claude-code-sessions")
    src = os.path.join(base, G("src15"), G("ws-s15"))
    dst = os.path.join(base, G("dst15"), G("ws-d15"))
    _mk_session(src, "X", act=ACT + 2000)    # 源更新 → 更新
    _mk_session(src, "Y", act=ACT)           # 目标更新 → 跳过
    _mk_session(src, "Z", act=ACT + 2000)    # 源更新，但目标里删过 → 仍跳过
    _mk_session(src, "V", act=ACT)           # 目标没有 → 新拷
    _mk_session(src, "W", act=None)          # 两边都没有 lastActivityAt → 比文件修改时间
    _mk_session(dst, "X", act=ACT)
    _mk_session(dst, "Y", act=ACT + 2000)
    _mk_session(dst, "Z", act=ACT)
    _mk_session(dst, "W", act=None)
    _mk_tomb(dst, "Z")
    t = time.time()
    os.utime(os.path.join(dst, "local_W.json"), (t - 100, t - 100))
    os.utime(os.path.join(src, "local_W.json"), (t, t))
    _touch_newer(os.path.dirname(dst))
    old_x = _read(os.path.join(dst, "local_X.json"))
    accts = core.list_accounts(base)

    off = core.build_plan(accts, merge_all=True, update_newer=False)
    check(off.total_update == 0 and off.total_copy == 1 and off.total_skip == 3,
          f"关闭更新时：只新拷 V，X/Y/W 都算已存在（实际 拷{off.total_copy} 更{off.total_update} 跳{off.total_skip}）")
    plan = core.build_plan(accts, merge_all=True)
    check(plan.total_update == 2 and plan.total_copy == 1 and plan.total_skip == 1 and plan.total_deleted == 1,
          f"开启更新：更新 X、W，新拷 V，跳过 Y，墓碑 Z 仍跳过（实际 更{plan.total_update} 拷{plan.total_copy} "
          f"跳{plan.total_skip} 删{plan.total_deleted}）")
    reasons = {os.path.basename(fp): r for it in plan.items for a, fp, r in it.decisions}
    check("最后活动时间" in reasons["local_X.json"] and "文件修改时间" in reasons["local_W.json"],
          "有 lastActivityAt 时比它，没有时比文件修改时间")
    check("更新 2 个" in "\n".join(core.describe_plan(plan)), "计划说明里单独列出「更新」个数")
    res = core.apply_migration(plan)
    check(res.updated == 2 and res.copied == 1, f"实际更新 2、新拷 1（实际 更{res.updated} 拷{res.copied}）")
    check(_read(os.path.join(dst, "local_X.json")) == _read(os.path.join(src, "local_X.json")), "X 被源里更新的版本覆盖")
    mf = core.read_manifest(res.backup_path)
    ex = next(e for e in mf["files"] if e["name"] == "local_X.json")
    check(ex["action"] == "update" and ex["old_sha256"] == core._sha256(os.path.join(res.backup_path, "local_X.json")),
          "清单里记了更新动作、旧版本 sha256 和备份位置")
    log = _read(res.log_path)
    check("UPDATE\tlocal_X.json" in log and "COPY\tlocal_V.json" in log and "SKIP\tlocal_Z.json" in log,
          "日志一行一个文件：动作、文件名、原因")
    with open(os.path.join(dst, "local_W.json"), "a", encoding="utf-8") as f:
        f.write(" ")  # 模拟桌面端在迁移后改过 W
    bk = next(b for b in core.list_backups(base) if b.backup_path == res.backup_path)
    pv = core.undo_migration(bk, dry_run=True)
    check(pv.restored == 1 and pv.moved == 1 and pv.modified == ["local_W.json"]
          and _read(os.path.join(dst, "local_X.json")) != old_x, "撤销预览只算不改")
    u = core.undo_migration(bk)
    check(u.restored == 1 and u.moved == 1 and u.modified == ["local_W.json"],
          f"撤销：X 恢复旧版本、V 移出、被改过的 W 保留（实际 恢{u.restored} 移{u.moved} 改{u.modified}）")
    check(_read(os.path.join(dst, "local_X.json")) == old_x, "X 恢复成迁移前的旧内容（不是删除）")
    check(_names(u.undo_dir) == {"local_X.json", "local_V.json"}, "本次写入的 X 和新拷的 V 都移到了撤销目录")
    check(os.path.isfile(u.log_path) and "RESTORE\tlocal_X.json" in _read(u.log_path), "撤销也写了日志")


def test_backup_verify(tmp: str) -> None:
    print("[16] 备份校验失败时什么都不拷")
    base = os.path.join(tmp, "t16", "claude-code-sessions")
    src = os.path.join(base, G("src16"), G("ws-s16"))
    dst = os.path.join(base, G("dst16"), G("ws-d16"))
    _mk_session(src, "A")
    _mk_session(src, "B")
    _mk_session(dst, "keep")
    _touch_newer(os.path.dirname(dst))
    plan = core.build_plan(core.list_accounts(base), merge_all=True)
    real_copytree = core.shutil.copytree

    def lossy_copytree(s, d, *a, **k):
        r = real_copytree(s, d, *a, **k)
        os.remove(os.path.join(d, "local_keep.json"))  # 模拟备份漏了一个文件
        return r

    core.shutil.copytree = lossy_copytree
    try:
        core.apply_migration(plan)
        check(False, "备份不完整时应停止")
    except core.MigrationError as e:
        check("备份校验失败" in str(e), "备份文件数 / 字节数对不上时停止")
    finally:
        core.shutil.copytree = real_copytree
    check(_names(dst) == {"local_keep.json"}, "目标里一个文件都没拷")
    bk = next(b for b in core.list_backups(base) if b.kind == "backup")
    check(not bk.has_manifest and "STOP" in _read(os.path.join(bk.backup_path, core.LOG_NAME)),
          "失败的备份没有清单（不能拿来撤销），日志里记了停止原因")


def test_invalid_records(tmp: str) -> None:
    print("[17] 无效记录计数；全部无效时停止")
    base = os.path.join(tmp, "t17", "claude-code-sessions")
    src = os.path.join(base, G("src17"), G("ws-s17"))
    dst = os.path.join(base, G("dst17"), G("ws-d17"))
    _mk_session(src, "ok")
    _mk_session(src, "broken", raw="{not json")
    _mk_session(src, "nosid", raw='{"cliSessionId": "c-nosid"}')
    _mk_session(src, "array", raw="[1, 2, 3]")
    os.makedirs(dst)
    _touch_newer(os.path.dirname(dst))
    plan = core.build_plan(core.list_accounts(base), merge_all=True)
    check(plan.total_invalid == 3 and plan.total_copy == 1, f"3 个无效、1 个可拷（实际 无效{plan.total_invalid} 拷{plan.total_copy}）")
    check({os.path.basename(f) for it in plan.items for f in it.files} == {"local_ok.json"}, "无效记录不在拷贝名单里")
    for n in ("ok",):
        os.remove(os.path.join(src, f"local_{n}.json"))
    try:
        core.build_plan(core.list_accounts(base), merge_all=True)
        check(False, "源里全是无效记录时应停止")
    except core.MigrationError as e:
        check("都解析不出 sessionId" in str(e), "源里全是无效记录（格式可能变了）时停止、不动任何东西")


def test_cli(tmp: str) -> None:
    print("[18] 命令行：默认只预览，--apply 才写")
    root = os.path.join(tmp, "t18", "Claude")
    base = os.path.join(root, "claude-code-sessions")
    old, new, ws_old, ws_new = G("old18"), G("new18"), G("ws-old18"), G("ws-new18")
    _mk_local(os.path.join(base, old, ws_old), ["p", "q"])
    _mk_local(os.path.join(base, new, ws_new), ["r"])
    _touch_newer(os.path.join(base, new))
    with open(os.path.join(root, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"lastKnownAccountUuid": new}, f)
    rc, out = _cli(["--base", base, "--list"])
    check(rc == 0 and "[1]" in out and "[2]" in out and "最近登录" in out and "默认目标" in out, "--list 列出序号和标记")
    before = _snapshot(base)
    rc, out = _cli(["--base", base])
    check(rc == 0 and "预览结束" in out and "新拷 2 个" in out and _snapshot(base) == before, "不带参数 = 预览，什么都没改")
    sel_target = f"{new}\\{ws_new}"
    rc, out = _cli(["--base", base, "--source", "2", "--target", sel_target, "-v"])
    check(rc == 0 and "COPY" in out and _snapshot(base) == before, "按序号 / accountId\\orgId 指定源和目标，仍只预览")
    rc, out = _cli(["--base", base, "--source", "9"])
    check(rc == 1 and "超出范围" in out, "序号超出范围时报错退出")
    rc, out = _cli(["--base", base, "--source", "2", "--target", sel_target, "--apply"])
    dst = os.path.join(base, new, ws_new)
    check(rc == 0 and _names(dst) == {"local_p.json", "local_q.json", "local_r.json"} and "备份：" in out and "日志 →" in out,
          "--apply 才真正写入，并给出备份和日志位置")
    rc, out = _cli(["--base", base, "--undo"])
    check(rc == 0 and "[1]" in out and "可撤销" in out, "--undo 不带 --backup 时只列出备份")
    before = _snapshot(base)
    rc, out = _cli(["--base", base, "--undo", "--backup", "1"])
    check(rc == 0 and "预览结束" in out and _snapshot(base) == before, "--undo --backup 1 默认只预览")
    rc, out = _cli(["--base", base, "--undo", "--backup", "1", "--apply"])
    check(rc == 0 and _names(dst) == {"local_r.json"}, "--undo --backup 1 --apply 才真正撤销")

    if platform.system() == "Windows":
        real_bases, real_status = core.get_session_bases, core.get_claude_status
        core.get_session_bases = lambda: [base]  # 让命令行以为这就是桌面端在用的目录
        try:
            core.get_claude_status = lambda: core.ClaudeStatus(True, [101], "none", None, None)
            before = _snapshot(base)
            rc, out = _cli(["--base", base, "--apply"])
            check(rc == 2 and "正在运行" in out and _snapshot(base) == before, "桌面端在运行时 --apply 拒绝执行")
            core.get_claude_status = lambda: core.ClaudeStatus(False, [], "none", None, None, unknown=1)
            rc, out = _cli(["--base", base, "--apply"])
            check(rc == 2 and "读不到路径" in out and _snapshot(base) == before, "有读不到路径的 Claude 进程时也拒绝（偏保守）")
            rc, out = _cli(["--base", base, "--apply", "--assume-closed"])
            check(rc == 0 and len(_names(dst)) == 3, "用户确认后（--assume-closed）才执行")
        finally:
            core.get_session_bases, core.get_claude_status = real_bases, real_status


def test_gui_support(tmp: str) -> None:
    print("[19] 界面用到的 core 功能：标题、读取缓存、没在运行时的启动方式")
    ws = os.path.join(tmp, "t19", "claude-code-sessions", G("a19"), G("ws19"))
    old_p = _mk_session(ws, "x1")
    new_p = _mk_session(ws, "x2")
    os.utime(old_p, (time.time() - 100, time.time() - 100))
    check(core.peek_latest(ws)[0] == "t-x2", f"peek_latest 取最近改动那条的标题（实际 {core.peek_latest(ws)[0]}）")
    check(core.peek_latest(None) == ("", 0.0) and core.peek_latest(os.path.join(tmp, "nope")) == ("", 0.0),
          "没有目录 / 没有记录时返回空")
    m1 = core._read_meta(new_p, cache=True)
    check(m1.title == "t-x2" and core._read_meta(new_p, cache=True) is m1, "缓存命中时直接返回同一份解析结果")
    with open(new_p, "w", encoding="utf-8") as f:
        json.dump({"sessionId": "local_x2", "cliSessionId": "cli-x2", "title": "改过的标题"}, f, ensure_ascii=False)
    check(core._read_meta(new_p, cache=True).title == "改过的标题", "文件改了（大小 / 修改时间变了）就重新读")
    check(core._read_meta(new_p).title == "改过的标题", "不带 cache 时总是读盘")

    b = os.path.join(tmp, "t19b", "claude-code-sessions")
    _mk_local(os.path.join(b, G("o19"), G("wso19")), ["a", "b", "c"])
    _mk_local(os.path.join(b, G("n19"), G("wsn19")), ["c"])
    _touch_newer(os.path.join(b, G("n19")))
    accts = core.list_accounts(b)
    p1 = core.build_plan(accts, merge_all=True)
    p2 = core.build_plan(accts, merge_all=True, cache=True)
    p3 = core.build_plan(accts, merge_all=True, cache=True)
    check((p1.total_copy, p1.total_skip) == (p2.total_copy, p2.total_skip) == (p3.total_copy, p3.total_skip) == (2, 1),
          "cache=True 的计划和直接读盘的一致")

    st = core.parse_claude_status(_ps_output(desktop=False), localappdata=LAD)
    check(not st.running and st.relaunch_kind == "aumid" and st.relaunch_value == "Claude_testpub00000!Claude",
          f"桌面端没在运行时，也能从包清单拿到启动方式（实际 {st.relaunch_kind} {st.relaunch_value}）")
    st2 = core.parse_claude_status(_ps_output(apps=False, desktop=False), localappdata=LAD)
    check(st2.relaunch_kind == "none", "没在运行、也没有包清单时不给启动方式")

    import struct
    import app_icon
    data = app_icon.ico()
    count = struct.unpack_from("<HHH", data)[2]
    entries = [struct.unpack_from("<BBBBHHII", data, 6 + 16 * i) for i in range(count)]
    sizes = [(e[0] or 256) for e in entries]
    blobs = [data[e[7]:e[7] + e[6]] for e in entries]
    check(sizes == list(app_icon.ICO_SIZES) and all(e[7] + e[6] <= len(data) for e in entries),
          f"程序图标 .ico 有 {sizes} 这几个尺寸、条目都在文件范围内")
    check(blobs[-1][:4] == bytes((0x89, 0x50, 0x4E, 0x47)) and all(struct.unpack_from("<I", x)[0] == 40 for x in blobs[:-1]),
          "256 用 PNG，其余是 32 位位图")
    px = app_icon.rgba(32)
    check(px[3] == 0 and px[(16 * 32 + 16) * 4 + 3] == 255, "图标四角透明、中间不透明")


def test_gui_smoke(tmp: str) -> None:
    print("[20] 图形界面冒烟：合成目录上走一遍 预览 → 迁移 → 撤销（确认框自动回答，不碰桌面端）")
    try:
        import gui
    except Exception as e:  # noqa: BLE001
        print(f"  SKIP 无法加载图形界面：{e}")
        return
    root = os.path.join(tmp, "t20", "Claude")
    base = os.path.join(root, "claude-code-sessions")
    old, new = G("old20"), G("new20")
    src, dst = os.path.join(base, old, G("ws-old20")), os.path.join(base, new, G("ws-new20"))
    _mk_local(src, ["p", "q", "r"])
    _mk_local(dst, ["r"])
    _touch_newer(os.path.join(base, new))
    with open(os.path.join(root, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"lastKnownAccountUuid": new}, f)

    asked: list[str] = []
    answers: list[bool] = []
    real_show = gui.Dialog.show

    def fake_show(app, heading, lines=(), ok="确定", cancel="取消", tone="accent"):
        asked.append(heading)
        return answers.pop(0) if answers else False

    gui.Dialog.show = staticmethod(fake_show)
    try:
        app = gui.App(bases=[base])
    except Exception as e:  # noqa: BLE001 —— 没有显示环境等
        gui.Dialog.show = real_show
        print(f"  SKIP 无法创建窗口：{e}")
        return
    app.withdraw()

    def pump(cond, timeout: float = 10.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            app.update()
            if cond():
                return True
            time.sleep(0.02)
        return False

    try:
        check(not app.guard_claude, "--base 指向测试目录时不检测、不关闭桌面端")
        check(app.target_var.get() == new, "默认目标 = 最近登录的账号")
        check(bool(app.wm_iconphoto) and len(getattr(app, "_icons", [])) == 2, "源码运行时窗口图标用 app_icon 画的 32 / 16 两张（不用 Tk 的羽毛）")
        # 九宫格图中间只有几像素时，ttk 每次重绘要贴上万块（2026-10-12 实测：切回窗口发黑、最大化卡死）
        check(app.theme.slices and min(i.width() for i in app.theme.slices) >= 200,
              "圆角按钮 / 卡片用的拉伸图中间区域足够大（重绘不会逐像素平铺）")
        app.deiconify()
        app.update()
        t0 = time.process_time()
        for state in ("zoomed", "normal", "zoomed", "normal"):
            app.state(state)
            app.update()
        cost = time.process_time() - t0
        check(cost < 2.0, f"最大化 / 还原两轮的重绘 CPU 在 2 秒内（实际 {cost:.2f}s）")
        app.withdraw()
        check(pump(lambda: app.plan is not None), "后台算出了计划")
        check(app.plan.total_copy == 2 and app.plan.total_skip == 1,
              f"计划：新增 2、已在新账号 1（实际 {app.plan.total_copy}/{app.plan.total_skip}）")
        check("disabled" not in app.btn_go.state(), "有要搬的对话时「开始迁移」可点")
        app.src_vars[old].set(False)
        app._source_toggled(old)
        check(pump(lambda: not app.planning) and app.plan is None and "勾选" in app.plan_error
              and "disabled" in app.btn_go.state(), "取消全部勾选后提示勾选、按钮不可点")
        app.src_vars[old].set(True)
        app._source_toggled(old)
        pump(lambda: app.plan is not None)
        app.show_details()
        app.update()
        details = [w for w in app.winfo_children() if isinstance(w, gui.tk.Toplevel)]
        check(len(details) == 1, "「查看明细」打开明细窗口")
        for w in details:
            w.destroy()

        before = _snapshot(base)
        app.do_migrate()  # 确认框回答「否」
        pump(lambda: not app.busy, 2)
        check(bool(asked) and "迁移到" in asked[-1] and _snapshot(base) == before, "确认框点取消时什么都不改")
        answers.append(True)
        app.do_migrate()
        check(pump(lambda: app.result is not None), "确认后完成迁移，汇总卡片显示结果")
        check(_names(dst) == {"local_p.json", "local_q.json", "local_r.json"}, "新账号里多了 p、q")
        check(not app.busy and not bool(app.bar.bar.grid_info()), "完成后底栏的进度条收起")
        check("disabled" in app.btn_go.state(), "迁移后没有要搬的了，按钮不可点")

        app.show_page("history")
        bk = [b for b in app.backups if b.kind == "backup" and b.has_manifest]
        check(len(bk) == 1, "「记录」页列出这次迁移")
        answers.append(True)
        app.do_undo(bk[0])
        check(pump(lambda: bool(asked) and asked[-1] == "撤销完成"), "撤销后弹出完成提示")
        check(_names(dst) == {"local_r.json"}, "撤销后新账号回到迁移前")
        app.show_page("help")
        check("数据位置" in app.help_text.get("1.0", "end"), "帮助页有数据位置")

        # 不该整页重画的地方（2026-10-12 用户反馈：点删除记录界面会重绘，像是卡了）
        app.show_page("history")
        cards = dict(app._hist_rows)
        check(len(cards) >= 2, f"记录页至少有两条（迁移备份 + 撤销移出的目录）（实际 {len(cards)}）")
        app.show_page("home")
        app.show_page("history")
        check(all(app._hist_rows[k][1] is v[1] for k, v in cards.items()), "来回切到记录页不重建卡片")
        victim = next(b for b in app.backups if b.kind == "undone")
        keep = {k: v[1] for k, v in cards.items() if k != victim.backup_path}
        answers.append(True)
        app.do_delete(victim)
        check(pump(lambda: not app.busy and victim.backup_path not in app._hist_rows), "删除后那一条从列表消失")
        check(all(app._hist_rows[k][1] is w for k, w in keep.items()), "删除一条时其他卡片原样保留（不整页重画）")
        check(not cards[victim.backup_path][1].winfo_exists(), "被删的那张卡片已销毁")
        check(not app.busy and not bool(app.bar.bar.grid_info()), "删除完成后底栏回到桌面端状态、进度条收起")
        rows_before = [w for w in app.src_list.inner.winfo_children()]
        summary_before = list(app.summary.winfo_children())
        app.rescan()
        pump(lambda: not app.planning, 5)
        app.update()
        check(rows_before and all(w.winfo_exists() for w in rows_before), "「重新检测」后账号没变，账号卡片不重建")
        check(all(w.winfo_exists() for w in summary_before), "「重新检测」后数字没变，汇总卡片不重建")
        seen = []
        page_y = app.pages.winfo_rooty()
        app._worker(lambda: time.sleep(0.7), "测试慢操作…")
        pump(lambda: (seen.append((app.bar.text.get_text(), bool(app.bar.bar.grid_info()), app.pages.winfo_rooty()))
                      or not app.busy), 5)
        check(any(text == "测试慢操作…" and mapped for text, mapped, _y in seen) and not bool(app.bar.bar.grid_info()),
              "进行中底栏写正在做什么、进度条在固定位置出现，做完收起")
        check(all(y == page_y for _t, _m, y in seen), "进行中提示不推动页面（页面位置不变）")

        if platform.system() == "Windows":
            # 桌面端在运行：确认框写明会先关闭；顺序必须是 关闭 → 写入 → 重新打开；关不掉就什么都不写。
            # 进程控制全部打桩，不会结束或启动任何真实进程。
            calls: list[str] = []
            real = (core.get_claude_status, core.close_claude, core.relaunch_claude, core.apply_migration)
            running = core.ClaudeStatus(True, [101], "aumid", "Claude_x!Claude", r"C:\x\Claude.exe", [r"C:\x\Claude.exe"])
            close_ok = {"v": True}

            def fake_close(st=None, timeout=15):
                calls.append("close")
                return (True, "已结束 1 个") if close_ok["v"] else (False, "仍有 1 个没有退出")

            def fake_apply(plan, log=None):
                calls.append("apply")
                return real[3](plan, log=log)

            core.get_claude_status = lambda: running
            core.close_claude = fake_close
            core.relaunch_claude = lambda kind, value: (calls.append(f"open {value}"), (True, "已请求"))[1]
            core.apply_migration = fake_apply
            app.guard_claude = True
            try:
                app.show_page("home")
                app.scan()
                pump(lambda: app.plan is not None)
                n_asked = len(asked)
                answers.append(True)
                app.do_migrate()
                check(pump(lambda: app.result is not None), "桌面端在运行时也能完成迁移")
                check(calls[:3] == ["close", "apply", "open Claude_x!Claude"], f"顺序：关闭 → 写入 → 重新打开（实际 {calls[:3]}）")
                check(app.result["closed"] and app.result["reopened"], "结果里记下已关闭并重新打开")
                check(len(asked) == n_asked + 1, "整个过程只弹一次确认框")
                b2 = next(b for b in core.list_backups_multi([base]) if b.kind == "backup" and b.has_manifest and not b.undone)
                answers.append(True)
                app.do_undo(b2)
                pump(lambda: bool(asked) and asked[-1] == "撤销完成")
                calls.clear()
                close_ok["v"] = False
                app.scan()
                pump(lambda: app.plan is not None)
                before = _snapshot(base)
                answers.append(True)
                app.do_migrate()
                check(pump(lambda: bool(asked) and asked[-1] == "出错了"), "关不掉桌面端时提示出错")
                check(calls == ["close"] and _snapshot(base) == before, f"关不掉就不写入、也不去重新打开（实际 {calls}）")
            finally:
                core.get_claude_status, core.close_claude, core.relaunch_claude, core.apply_migration = real
                app.guard_claude = False
    finally:
        gui.Dialog.show = real_show
        app.destroy()


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="clsm_selftest_")
    base = os.path.join(tmp, "claude-code-sessions")
    os.makedirs(base)
    try:
        build_fixture(base)

        print("[1] 探测账号")
        accts = core.list_accounts(base)
        check(len(accts) == 2, f"探测到 2 个账号（实际 {len(accts)}）")
        check(accts[0].name == NEW, "mtime 最新的是 NEW（目标）")
        old = next(a for a in accts if a.name == OLD)
        check(old.workspace.name == WS_OLD, "OLD 主 workspace 选中 session 最多的那个（排除 BACKUP）")
        check(old.sessions == 5, f"OLD 主 ws 有 5 个 session（实际 {old.sessions}）")

        print("[2] 构造迁移计划（合并全部旧号）")
        plan = core.build_plan(accts, merge_all=True)
        check(plan.target.name == NEW, "目标 = NEW")
        check(len(plan.sources) == 1 and plan.sources[0].name == OLD, "源 = OLD")
        check(plan.total_copy == 3, f"应拷 3 个 a/b/c（实际 {plan.total_copy}）")
        check(plan.total_skip == 2, f"应跳 2 个 d/e（实际 {plan.total_skip}）")

        print("[3] 执行迁移（先备份）")
        logs: list[str] = []
        res = core.apply_migration(plan, log=logs.append)
        check(res.copied == 3 and res.skipped == 2, f"实拷 3 跳 2（实际 拷{res.copied} 跳{res.skipped}）")
        check(res.now_total == 5, f"目标 ws 现有 5 个 session（实际 {res.now_total}）")
        check(os.path.isdir(res.backup_path), "备份目录已生成")
        check(core._count_local_json(res.backup_path) == 2, "备份里仍是迁移前的 2 个")
        check(any("备份已校验" in m for m in logs), "备份建好后做了校验")

        print("[4] 幂等：再迁移一次应全跳过")
        accts2 = core.list_accounts(base)
        plan2 = core.build_plan(accts2, merge_all=True)
        check(plan2.total_copy == 0 and plan2.total_update == 0 and plan2.total_skip == 5,
              f"第二次应 0 拷 0 更新 5 跳（实际 拷{plan2.total_copy} 更{plan2.total_update} 跳{plan2.total_skip}）")

        print("[5] 列出备份并整目录换回（高级）")
        backups = core.list_backups(base)
        check(len(backups) >= 1, "能列出至少 1 个备份")
        target_backup = next(b for b in backups if b.account == NEW and b.kind == "backup")
        check(target_backup.manifest_files == 3, f"备份里有拷入清单，记了 3 个（实际 {target_backup.manifest_files}）")
        check(core.rollback_impact(target_backup) == (3, 5, 2), "整目录换回前能算出将有 3 个会话移出侧栏")
        replaced = core.rollback(target_backup, log=logs.append)
        new_ws = os.path.join(base, NEW, WS_NEW)
        check(core._count_local_json(new_ws) == 2, f"换回后目标 ws 恢复为 2 个（实际 {core._count_local_json(new_ws)}）")
        check(any(b.kind == "replaced" and b.backup_path == replaced for b in core.list_backups(base)),
              "被移开的 REPLACED 目录能在备份列表里看到")
        check(not os.path.exists(os.path.join(new_ws, core.MANIFEST_NAME))
              and not os.path.exists(os.path.join(new_ws, core.LOG_NAME))
              and os.path.isfile(os.path.join(replaced, "rollback.log")),
              "换回后工具自己的清单 / 日志不留在 workspace 里，换回本身也写了日志")

        print("[6] 删除保护：拒绝删非备份目录")
        try:
            core.delete_backup_dir(new_ws, base=base)
            check(False, "删除普通 workspace 应被拒绝")
        except core.MigrationError:
            check(True, "普通 workspace 删除被正确拒绝")

        print("[7] 全部 workspace 合并模式")
        accts3 = core.list_accounts(base)
        plan3 = core.build_plan(accts3, merge_all=True, all_workspaces=True)
        names = {os.path.basename(f) for it in plan3.items for f in it.files}
        check("local_z.json" in names, "all_workspaces 模式纳入次要 ws 的 z")

        print("[8] 多 base（登录版 + API版 profile）聚合")
        base3p = os.path.join(tmp, "Claude-3p", "claude-code-sessions")
        _mk_local(os.path.join(base3p, API, WS_API), ["p", "q"])
        _touch_newer(os.path.join(base3p, API))  # API 账号 mtime 最新 = 目标
        multi = core.list_accounts_multi([base, base3p])
        check(len(multi) == 3, f"两 base 共探测到 3 个账号（实际 {len(multi)}）")
        check(multi[0].name == API, "mtime 最新的是 API 账号")
        check(multi[0].profile == "API版", f"API 账号 profile 标签正确（实际 {multi[0].profile}）")
        check(core.profile_label(base) == "登录版", "登录版 profile 标签正确")
        plan4 = core.build_plan(multi, merge_all=True)
        check(plan4.target.name == API, "跨 profile 计划：目标 = API 账号")
        # 跨源去重：OLD 的 a-e 先计入，NEW 的 d/e 与之重名 → 计划里就算跳过，计划数与实拷数一致
        check(plan4.total_copy == 5 and plan4.total_skip == 2,
              f"计划期跨源去重：拷 5 跳 2（实际 拷{plan4.total_copy} 跳{plan4.total_skip}）")
        res4 = core.apply_migration(plan4)
        check(res4.copied == plan4.total_copy == 5 and res4.now_total == 7,
              f"跨 profile 实拷 5、现共 7，与计划一致（实际 拷{res4.copied} 共{res4.now_total}）")
        bks = core.list_backups_multi([base, base3p])
        check(any(b.account == API for b in bks), "多 base 能列出 API 侧备份")
        api_bk = next(b for b in bks if b.account == API)
        try:
            core.delete_backup_dir(os.path.join(base3p, API, WS_API), bases=[base, base3p])
            check(False, "多 base 下删除普通 workspace 应被拒绝")
        except core.MigrationError:
            check(True, "多 base 删除保护生效")
        core.delete_backup_dir(api_bk.backup_path, bases=[base, base3p])
        check(not os.path.isdir(api_bk.backup_path), "多 base 下可删除合法备份")

        print("[9] 墓碑与 cliSessionId 判重")
        b9 = os.path.join(tmp, "t9", "claude-code-sessions")
        src_ws = os.path.join(b9, G("src9"), G("ws-s9"))
        dst_ws = os.path.join(b9, G("dst9"), G("ws-d9"))
        _mk_session(src_ws, "L1")                        # 目标里按 local id 删过
        _mk_session(src_ws, "L2", cli="cli-L2")          # 目标里按 cliSessionId 删过
        _mk_session(src_ws, "L3", prior=["P3"])          # priorCliSessionIds 命中墓碑
        _mk_session(src_ws, "L4")                        # 正常 → 拷
        _mk_session(src_ws, "L9")                        # 正常 → 拷
        _mk_session(src_ws, "L5", cli="cli-t1")          # 目标已有同 cliSessionId 的会话 → 跳过
        _mk_session(src_ws, "t1", cli="cli-t1-old")      # 目标已有同名文件、源不比它新 → 跳过
        _mk_session(src_ws, "L6", raw="{broken")         # JSON 坏 → 无效，不拷
        _mk_session(src_ws, "L7", raw="{broken")         # JSON 坏（文件名也命中墓碑）→ 无效，不拷
        _mk_session(dst_ws, "t1", cli="cli-t1")
        for tomb in ("L1", "cli-L2", "P3", "L7"):
            _mk_tomb(dst_ws, tomb)
        _touch_newer(os.path.dirname(dst_ws))
        plan9 = core.build_plan(core.list_accounts(b9), merge_all=True)
        planned = {os.path.basename(f) for it in plan9.items for f in it.files}
        check(planned == {"local_L4.json", "local_L9.json"}, f"只计划拷 L4、L9（实际 {sorted(planned)}）")
        check(plan9.total_skip == 2, f"已存在跳过 2（同名 t1、同 cliSessionId 的 L5）（实际 {plan9.total_skip}）")
        check(plan9.total_deleted == 3, f"目标已删除跳过 3（local id / cliSessionId / prior）（实际 {plan9.total_deleted}）")
        check(plan9.total_invalid == 2, f"2 个坏 JSON 记为无效（实际 {plan9.total_invalid}）")
        res9 = core.apply_migration(plan9)
        check(res9.copied == 2 and res9.skipped == 2 and res9.skipped_deleted == 3 and res9.invalid == 2,
              f"实拷 2 / 已存在 2 / 目标已删除 3 / 无效 2（实际 {res9.copied}/{res9.skipped}/{res9.skipped_deleted}/{res9.invalid}）")
        check(_names(dst_ws) == {"local_t1.json", "local_L4.json", "local_L9.json"}, "被删过的会话没有被拷回来")
        check(not [n for n in os.listdir(dst_ws) if n.endswith(".csm-tmp")], "没有残留临时文件")
        mf = core.read_manifest(res9.backup_path)
        check(mf is not None and sorted(e["name"] for e in mf["files"]) == ["local_L4.json", "local_L9.json"],
              "备份里的拷入清单记了 L4、L9")
        check(all(e["sha256"] == core._sha256(os.path.join(dst_ws, e["name"])) for e in mf["files"]),
              "清单里的 sha256 与拷入的文件一致")
        log9 = _read(res9.log_path).splitlines()
        check(res9.log_path.startswith(res9.backup_path)
              and len([x for x in log9 if not x.startswith("#")]) == 9
              and any(x.startswith("INVALID\tlocal_L6.json\t") for x in log9),
              "日志在备份目录里，9 个源文件各一行（动作、文件名、原因）")

        print("[10] 0 拷贝不建备份")
        def n_backups() -> int:
            return sum(1 for b in core.list_backups(b9) if b.kind == "backup")
        before = n_backups()
        plan10 = core.build_plan(core.list_accounts(b9), merge_all=True)
        res10 = core.apply_migration(plan10)
        check(plan10.total_copy == 0 and res10.copied == 0 and res10.backup_path == "", "没有要拷的文件时什么都不做")
        check(n_backups() == before, "没有新建备份目录")
        _mk_session(src_ws, "L8")
        plan10b = core.build_plan(core.list_accounts(b9), merge_all=True)
        _mk_session(dst_ws, "L8")  # 预览之后、执行之前，桌面端自己建了同一个会话
        res10b = core.apply_migration(plan10b)
        check(plan10b.total_copy == 1 and res10b.copied == 0 and res10b.backup_path == "" and n_backups() == before,
              "执行时按磁盘现状复核，复核后没得拷也不建备份")
        os.remove(os.path.join(src_ws, "local_L8.json"))
        os.remove(os.path.join(dst_ws, "local_L8.json"))

        print("[11] 按清单撤销（被改过的文件保留）")
        bk9 = next(b for b in core.list_backups(b9) if b.backup_path == res9.backup_path)
        with open(os.path.join(dst_ws, "local_L9.json"), "a", encoding="utf-8") as f:
            f.write(" ")  # 模拟桌面端在拷入后改过这个会话
        check(core.rollback_impact(bk9) == (2, 3, 1), "整目录换回会移出 2 个（L4、L9），撤销只动清单")
        u1 = core.undo_migration(bk9)
        check(u1.moved == 1 and u1.modified == ["local_L9.json"] and u1.missing == [],
              f"移走 1 个（L4），被改过的 L9 保留（实际 移{u1.moved} 改{u1.modified} 缺{u1.missing}）")
        check(_names(dst_ws) == {"local_t1.json", "local_L9.json"}, "workspace 里只少了 L4，其它文件不动")
        check(os.path.isfile(os.path.join(u1.undo_dir, "local_L4.json")) and core.UNDONE_MARKER in u1.undo_dir,
              "L4 移到了 __UNDONE_MIGRATION_ 目录（没删）")
        dst_acct = next(a for a in core.list_accounts(b9) if a.name == G("dst9"))
        check([w.name for w in dst_acct.all_workspaces] == [G("ws-d9")], "撤销目录不会被当成 workspace")
        listed = core.list_backups(b9)
        check(any(b.kind == "undone" and b.backup_path == u1.undo_dir for b in listed), "撤销目录能在备份列表里看到")
        check(next(b for b in listed if b.backup_path == res9.backup_path).undone != "", "备份记录标成已撤销")
        u2 = core.undo_migration(bk9)
        check(u2.moved == 0 and u2.missing == ["local_L4.json"] and u2.undo_dir == "",
              "重复撤销：不再移动任何文件，也不新建目录")
        legacy = dst_ws + core.BACKUP_MARKER + "20250701-000000"
        _mk_session(legacy, "t1", cli="cli-t1")
        legacy_bk = next(b for b in core.list_backups(b9) if b.backup_path == legacy)
        check(not legacy_bk.has_manifest, "没有清单的旧备份被识别出来")
        try:
            core.undo_migration(legacy_bk)
            check(False, "没有清单的旧备份不能按清单撤销")
        except core.MigrationError:
            check(True, "没有清单的旧备份拒绝按清单撤销")
        core.delete_backup_dir(u1.undo_dir, base=b9)
        check(not os.path.isdir(u1.undo_dir), "撤销目录可以用删除功能清理")

        print("[12] 跨源去重（按 cliSessionId）")
        b12 = os.path.join(tmp, "t12", "claude-code-sessions")
        s1, s2, d12 = (os.path.join(b12, G(a), G("ws-" + a)) for a in ("src1", "src2", "dst12"))
        for i in "ABCDE":
            _mk_session(s1, i, cli=f"c{i}")
        _mk_session(s2, "F", cli="cA")   # 与 src1 的 A 是同一会话
        _mk_session(s2, "A", cli="cA")   # 与 src1 同名
        _mk_session(s2, "G", cli="cG")
        _mk_session(d12, "x0")
        _touch_newer(os.path.dirname(d12))
        plan12 = core.build_plan(core.list_accounts(b12), merge_all=True)
        check(plan12.total_copy == 6 and plan12.total_skip == 2,
              f"计划拷 6（A-E、G）跳 2（实际 拷{plan12.total_copy} 跳{plan12.total_skip}）")
        res12 = core.apply_migration(plan12)
        check(res12.copied == plan12.total_copy and res12.now_total == 7, "实拷数与计划（确认框显示的数）一致")

        test_process_control(tmp)
        test_last_login_and_layout(tmp)
        test_update_newer(tmp)
        test_backup_verify(tmp)
        test_invalid_records(tmp)
        test_cli(tmp)
        test_gui_support(tmp)
        test_gui_smoke(tmp)

        print("\n[OK] 全部自测通过")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
