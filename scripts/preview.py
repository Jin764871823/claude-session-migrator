"""README 截图（开发用）：在临时目录造一份演示用的 claude-code-sessions（假账号、假对话标题），
打开界面走一遍「开始迁移 → 确认 → 记录」，用 PrintWindow 只截本程序的窗口，存成 PNG。

用法：python scripts/preview.py [--out docs/images]
界面把演示目录当成桌面端的真实目录（这样底栏和确认框是平时的样子）；为此把检测桌面端换成固定的
「没在运行」，关闭 / 重开桌面端和 PowerShell 调用都换成直接报错——真实的会话目录和桌面端一概不碰。仅 Windows。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import struct
import sys
import tempfile
import time
import uuid
import zlib
from ctypes import wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import core  # noqa: E402
import gui  # noqa: E402
import ui_theme as ui  # noqa: E402


def _forbidden(*_a, **_k):
    raise RuntimeError("截图脚本不许碰真实的 Claude 桌面端")

DAY = 86400
# (账号, 对话数, 最近一次活动距今秒数, 最近几条对话的标题)；第一个是新账号（桌面端最近登录的）
ACCOUNTS = [
    ("3913640a-7c52-4e1b-9d0a-5b8e2f61c3d4", 3, 2 * 3600, ["新账号的第一个对话", "整理周报模板"]),
    ("3355034b-1f6d-4a2e-8c47-0e9b7d25a816", 186, DAY + 3 * 3600, ["重构登录模块的错误处理", "给 README 补安装说明", "排查单元测试偶发失败"]),
    ("d046b324-8e1a-4f73-b2c5-6a0d9f4e1b27", 42, 39 * DAY, ["排查前端打包变慢", "写数据库迁移脚本"]),
]
SHARED = 1      # 新账号里已有、旧账号里也有的对话数（界面显示「已在新账号」）


def make_demo(root: str) -> str:
    """造演示目录：<root>\\Claude\\config.json（最近登录账号）+ claude-code-sessions\\<账号>\\<组织>\\local_*.json。"""
    claude = os.path.join(root, "Claude")
    base = os.path.join(claude, "claude-code-sessions")
    now = time.time()
    shared = [str(uuid.UUID(int=0xA000 + i)) for i in range(SHARED)]
    for rank, (account, count, age, titles) in enumerate(ACCOUNTS):
        org = os.path.join(base, account, str(uuid.uuid5(uuid.NAMESPACE_DNS, account)))
        os.makedirs(org)
        own = [str(uuid.uuid5(uuid.NAMESPACE_URL, f"{account}/{i}")) for i in range(count)]
        clis = ((shared if rank < 2 else []) + own)[:count]     # 新账号和第一个旧账号共有 SHARED 个对话
        for i, cli in enumerate(clis):
            title = titles[i] if i < len(titles) else f"演示对话 {i + 1}"
            local = str(uuid.uuid5(uuid.NAMESPACE_OID, f"{account}/{cli}"))
            data = {"sessionId": f"local_{local}", "cliSessionId": cli, "title": title,
                    "lastActivityAt": int((now - age - i * 5400) * 1000)}
            path = os.path.join(org, f"local_{local}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.utime(path, (data["lastActivityAt"] / 1000,) * 2)     # 文件时间和记录里的最近活动一致
        stamp = now - rank * 600        # 新账号目录最新
        os.utime(org, (stamp, stamp))
        os.utime(os.path.join(base, account), (stamp, stamp))
    with open(os.path.join(claude, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"lastKnownAccountUuid": ACCOUNTS[0][0]}, f)
    return base


def save_window_png(window: ctypes.c_void_p | int, path: str) -> tuple[int, int]:
    """PrintWindow 截一个顶层窗口（含系统标题栏）存 PNG；只用 ctypes + zlib。"""
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    hwnd = int(window)
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    hdc_screen = user32.GetDC(0)
    hdc = gdi32.CreateCompatibleDC(hdc_screen)
    bmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    gdi32.SelectObject(hdc, bmp)
    user32.PrintWindow(hwnd, hdc, 2)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

    info = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(hdc, bmp, 0, h, buf, ctypes.byref(info), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(hdc)
    user32.ReleaseDC(0, hdc_screen)
    raw = bytearray()
    data = buf.raw
    for y in range(h):
        raw.append(0)
        row = data[y * w * 4:(y + 1) * w * 4]
        for x in range(0, len(row), 4):
            raw.extend((row[x + 2], row[x + 1], row[x]))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(png)
    return w, h


def pump(app: gui.App, seconds: float, until=None) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.update()
        if until is not None and until():
            for _ in range(20):         # 条件满足后再画几帧，等界面稳定
                app.update()
            return
        time.sleep(0.02)
    if until is not None:
        raise SystemExit("等界面就绪超时")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "images"), help="PNG 输出目录（默认 docs/images）")
    out = os.path.abspath(ap.parse_args().out)
    os.makedirs(out, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="csm-demo-") as tmp:
        base = make_demo(tmp)
        core.get_session_bases = lambda: [base]
        core.get_claude_status = lambda: core.ClaudeStatus(
            running=False, pids=[], relaunch_kind="aumid", relaunch_value="Claude_demo!Claude", proc_path=None)
        core.close_claude = core.relaunch_claude = core._run_powershell = _forbidden
        ui.enable_dpi_awareness()
        app = gui.App()
        pump(app, 30, until=lambda: app.plan is not None and not app.busy)
        frame = lambda win: int(win.wm_frame(), 16)     # noqa: E731
        print("main.png", save_window_png(frame(app), os.path.join(out, "main.png")))

        shots = {}

        def grab_dialog() -> None:
            dialogs = [w for w in app.winfo_children() if isinstance(w, ui.Dialog)]
            if not dialogs:
                app.after(100, grab_dialog)
                return
            dialogs[0].update()
            shots["confirm"] = save_window_png(frame(dialogs[0]), os.path.join(out, "confirm.png"))
            dialogs[0]._ok()                            # 在演示数据上真的迁移一次，好截「记录」页

        app.after(300, grab_dialog)
        app.do_migrate()                                # 先在后台查桌面端，查完才弹确认框
        pump(app, 30, until=lambda: not app.busy and app.result is not None)
        print("confirm.png", shots.get("confirm"))
        app.show_page("history")
        app.refresh_backups()
        pump(app, 10, until=lambda: True)
        print("history.png", save_window_png(frame(app), os.path.join(out, "history.png")))
        app.destroy()


if __name__ == "__main__":
    main()
