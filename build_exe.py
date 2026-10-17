"""打包成 Windows 窗口程序（PyInstaller onefile + --windowed，不出控制台），只用 Git 里已提交的源码。

用法（用装了 PyInstaller 的 Python 运行）::

    python build_exe.py --out <输出目录>

产物：``<输出目录>\\dist\\Claude会话迁移.exe``，就这一个文件，部署时只复制它。
- 源码取自 ``git archive HEAD``（gui.py、core.py、ui_theme.py、app_icon.py）；这几个文件有未提交的改动时拒绝打包，
  免得 exe 和提交对不上。
- 图标由 app_icon.py 现场画成多尺寸 .ico；spec、build 缓存、图标都写在 ``<输出目录>`` 下，不往项目里写东西；
  输出目录必须是新的空目录（脚本不删除任何文件）。
- 选 onefile：程序目录里只有这一个 exe（自包含，用户 2026-10-16 定「exe 以外的越少越好」）。代价是每次打开先把
  运行环境解压到 ``%TEMP%\\_MEI*``（正常退出自动删），开窗比 onedir 慢约 1 秒（实测约 1.8 s，规范要求 onefile ≤ 4 s）。
"""

from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
import tarfile

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "Claude会话迁移"
SOURCES = ("gui.py", "core.py", "ui_theme.py", "app_icon.py")   # ui_theme.py：作者各工具共用的界面主题副本
# 用不到、体积大的标准库模块
EXCLUDED_MODULES = ("unittest", "pydoc", "test", "lib2to3", "distutils", "setuptools", "pip")


def git(*args: str, text: bool = False):
    return subprocess.run(["git", "-C", PROJECT_DIR, *args], check=True, capture_output=True, text=text).stdout


def export_sources(src_dir: str) -> str:
    """把 HEAD 里的源码导出到 src_dir，返回提交号。"""
    if git("status", "--porcelain", "--", *SOURCES, text=True).strip():
        raise SystemExit("%s 有未提交的改动，先提交再打包。" % "、".join(SOURCES))
    with tarfile.open(fileobj=io.BytesIO(git("archive", "--format=tar", "HEAD", *SOURCES))) as tar:
        tar.extractall(src_dir, filter="data")
    return git("rev-parse", "--short", "HEAD", text=True).strip()


def build(out_dir: str) -> tuple[str, str]:
    out_dir = os.path.abspath(out_dir)
    try:
        inside = os.path.commonpath([PROJECT_DIR, out_dir]) == PROJECT_DIR
    except ValueError:      # 不在同一个盘
        inside = False
    if inside:
        raise SystemExit("输出目录不能在项目目录里：%s" % out_dir)
    os.makedirs(out_dir, exist_ok=True)
    if os.listdir(out_dir):
        raise SystemExit("输出目录必须是新的空目录：%s" % out_dir)
    src_dir, work_dir, dist_dir = (os.path.join(out_dir, d) for d in ("src", "work", "dist"))
    commit = export_sources(src_dir)
    sys.path.insert(0, src_dir)
    import app_icon     # 用导出的那份画图标，和打进 exe 的源码一致
    icon = os.path.join(out_dir, "icon.ico")
    with open(icon, "wb") as f:
        f.write(app_icon.ico())
    env = dict(os.environ, PYINSTALLER_CONFIG_DIR=os.path.join(out_dir, "pyinstaller-config"))
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed",
               "--name", APP_NAME, "--icon", icon,
               "--distpath", dist_dir, "--workpath", work_dir, "--specpath", work_dir, "--paths", src_dir,
               *(arg for module in EXCLUDED_MODULES for arg in ("--exclude-module", module)),
               os.path.join(src_dir, "gui.py")]
    subprocess.run(command, check=True, cwd=out_dir, env=env)
    return os.path.join(dist_dir, APP_NAME + ".exe"), commit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, help="输出目录（新的空目录，不能在项目里）")
    exe, commit = build(parser.parse_args().out)
    print("已生成：%s（源码提交 %s）" % (exe, commit))


if __name__ == "__main__":
    main()
