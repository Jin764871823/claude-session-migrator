"""
gui.py — Claude 会话迁移（图形界面）。

一页完成迁移：左边勾选旧账号，右边确认新账号，下方实时显示会搬多少个对话；点「开始迁移」后
自动 关闭桌面端 → 备份并核对 → 写入 → 重新打开。「记录」页按次撤销，「帮助」页讲原理、数据位置和日志。
逻辑都在 core.py；只搬 sidebar 元数据 local_*.json，绝不触碰 ~/.claude/projects 下的 .jsonl 对话原文。
只用标准库（Tkinter），不需要安装第三方库。
"""

from __future__ import annotations

import argparse
import base64
import os
import platform
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
from datetime import datetime
from tkinter import messagebox, ttk

import app_icon
import core
import ui_theme as ui
from ui_theme import T, CheckBox, CheckRow, ElidedLabel, Link, ScrollList, WrapLabel, badge, card

APP_TITLE = "Claude 会话迁移"
IS_WINDOWS = platform.system() == "Windows"

# ---------------------------------------------------------------- 配色（共用主题模块 ui_theme.py 的令牌；名字沿用原来的）
BG = T["Bg"]                 # 窗口底色
CARD = T["Surface"]          # 卡片
BORDER = T["CardBorder"]     # 卡片描边、分隔线
LINE = T["Divider"]          # 卡片内的分隔线
TEXT = T["Text1"]
TEXT2 = T["Text2"]
TEXT3 = T["Text3"]
ACCENT = T["Accent"]
ACCENT_TINT = T["AccentTint"]
OK = T["Success"]
WARN = T["Warning"]
DANGER = T["Danger"]
ACTION_LABEL = {"copy": "新增", "update": "更新", "exist": "已在新账号", "deleted": "新账号里删过", "invalid": "无法识别"}
ACTION_ORDER = {"copy": 0, "update": 1, "exist": 2, "deleted": 3, "invalid": 4}


def friendly_time(ts: float) -> str:
    """今天 / 昨天 / 几月几日 / 某年某月某日。"""
    if not ts:
        return "—"
    try:
        d = datetime.fromtimestamp(ts)
    except (OverflowError, OSError, ValueError):
        return "—"
    today = datetime.now().date()
    days = (today - d.date()).days
    if days == 0:
        return f"今天 {d:%H:%M}"
    if days == 1:
        return f"昨天 {d:%H:%M}"
    if d.year == today.year:
        return f"{d.month}月{d.day}日 {d:%H:%M}"
    return f"{d.year}年{d.month}月{d.day}日"


def stamp_time(stamp: str) -> str:
    """备份目录名里的时间戳（20260723-115225，可能带 -2 后缀）→ 友好时间。"""
    try:
        return friendly_time(datetime.strptime(stamp[:15], "%Y%m%d-%H%M%S").timestamp())
    except ValueError:
        return stamp


def account_label(name: str) -> str:
    return f"账号 {name[:8]}"


def clear(widget) -> None:
    for w in widget.winfo_children():
        w.destroy()


# ---------------------------------------------------------------- 小部件（在 ui_theme.py 里）
class Dialog:
    """统一样式的确认 / 提示框（ui_theme.Dialog），标题固定为工具名。selftest 会替换 show 来自动回答。"""

    @staticmethod
    def show(app: "App", heading: str, lines=(), ok: str = "确定", cancel: str | None = "取消",
             tone: str = "accent") -> bool:
        return ui.Dialog.show(app, app.theme, heading, lines, ok=ok, cancel=cancel, tone=tone, title=APP_TITLE)


# ---------------------------------------------------------------- 主窗口
class App(tk.Tk):
    def __init__(self, bases: list[str] | None = None) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self._set_icon()
        self.configure(bg=BG)
        self.theme = ui.Theme(self)
        self._init_geometry()

        self.fixed_bases = list(bases) if bases else None   # 命令行 --base 指定的目录（测试副本）
        self.bases = self.fixed_bases or core.get_session_bases()
        real = {os.path.normcase(os.path.abspath(b)) for b in core.get_session_bases()}
        # 只有操作桌面端真实使用的目录时才检测 / 关闭 Claude；--base 指向测试副本时不碰桌面端
        self.guard_claude = any(os.path.normcase(os.path.abspath(b)) in real for b in self.bases)

        self.accounts: list[core.Account] = []
        self.latest: dict[str, tuple[str, float]] = {}   # 账号 → (最近一条会话的标题, 时间)
        self.multi_profile = False
        self.target_var = tk.StringVar()
        self.unchecked: set[str] = set()                 # 用户取消勾选的源账号（新出现的账号默认勾选）
        self.src_vars: dict[str, tk.BooleanVar] = {}
        self.update_newer = tk.BooleanVar(value=True)
        self.all_ws = tk.BooleanVar(value=False)
        self.plan: core.MigrationPlan | None = None
        self.plan_error = ""
        self.planning = False
        self._plan_gen = 0
        self.result: dict | None = None                  # 刚做完的迁移（汇总卡片显示它，改选择后清掉）
        self.busy = ""                                   # 进行中的操作；非空时会改文件的按钮都不响应
        self.claude: core.ClaudeStatus | None = None
        self.last_launch: tuple[str, str | None] = ("none", None)
        self.backups: list[core.BackupInfo] = []
        self._logged_bases: list[str] = []
        self._calls: queue.Queue = queue.Queue()
        self._pages: dict[str, tk.Frame] = {}
        self._tabs: dict[str, tuple[tk.Label, tk.Frame]] = {}
        self.page = "home"
        # 内容没变就不重画：重建控件在 Windows 上会先露出空白再补画，看起来像整页闪一下
        self._hist_rows: dict[str, tuple[tuple, ttk.Frame]] = {}   # 记录页：备份目录 → (内容签名, 卡片)
        self._hist_order: list[ttk.Frame] = []
        self._hist_empty: tk.Label | None = None
        self._home_key = None                            # 迁移页账号卡片的内容签名
        self._summary_key = None                         # 汇总卡片的内容签名
        self._help_key = None                            # 帮助页的内容签名（数据位置）

        self._build_header()
        self._build_bar()
        self.pages = tk.Frame(self, bg=BG)
        self.pages.pack(side="top", fill="both", expand=True)
        self._build_home()
        self._build_history()
        self._build_help()
        self.bind_all("<MouseWheel>", self._on_wheel)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.show_page("home")
        self.after(60, self._pump)
        self.scan()
        self.refresh_claude()
        # 系统标题栏染成 Windows 蓝、白字（共用主题 ui_theme.color_titlebar：外框窗口第一次映射时染上；Windows 11 22000 起，Windows 10 保持系统默认）
        ui.color_titlebar(self)

    def _set_icon(self) -> None:
        """窗口、任务栏和弹出框的图标（app_icon.py 画的，不用 Tk 的羽毛）：打包版直接用 exe 里的多尺寸图标，
        源码运行时现画 32 / 16 两个尺寸（约 30 ms）。"""
        try:
            if getattr(sys, "frozen", False) and IS_WINDOWS:
                self.iconbitmap(default=sys.executable)
            else:
                self._icons = [tk.PhotoImage(master=self, data=base64.b64encode(app_icon.png(size))) for size in (32, 16)]
                self.iconphoto(True, *self._icons)
        except tk.TclError:
            pass

    def _init_geometry(self) -> None:
        # 窗口尺寸按本工具的内容定（自写软件开发规范 3.3，用户 2026-10-15：各工具分别设，默认尺寸有上限）：
        # 默认 800×680、最小 660×540，按 DPI 放大、整个落在工作区内。
        ui.fit_to_work_area(self, 800, 680, 660, 540, scale=self.theme.scale)

    def report_callback_exception(self, exc, val, tb) -> None:
        """界面回调里没接住的异常：写进日志并弹窗（pythonw 下没有控制台，否则用户看不到）。"""
        self.log("[异常] " + "".join(traceback.format_exception(exc, val, tb)).rstrip())
        try:
            messagebox.showerror("程序出错", f"{exc.__name__}: {val}\n\n完整信息在「帮助」页的运行日志里。")
        except Exception:  # noqa: BLE001
            pass

    def _on_close(self) -> None:
        if self.busy:
            self.notify("请稍等", [f"{self.busy.rstrip('…')}，完成后再关闭窗口。"], tone="info")
            return
        self.destroy()

    # ---------------------------------------------------------------- 框架
    def _build_header(self) -> None:
        t = self.theme
        head = tk.Frame(self, bg=BG)
        head.pack(side="top", fill="x", padx=t.px(24))
        nav = tk.Frame(head, bg=BG)
        nav.pack(side="right", anchor="s")
        left = tk.Frame(head, bg=BG)
        left.pack(side="left", fill="x", expand=True, pady=(t.px(16), t.px(12)))
        tk.Label(left, text=APP_TITLE, font=t.f_title, fg=TEXT, bg=BG).pack(anchor="w")
        ElidedLabel(left, "换了账号，旧对话从侧栏消失了？把它们搬到新账号。对话内容本身不会被改动。",
                    t.f_small, fg=TEXT3, bg=BG).pack(fill="x", pady=(t.px(2), 0))
        for key, text in (("home", "迁移"), ("history", "记录"), ("help", "帮助")):
            f = tk.Frame(nav, bg=BG)
            f.pack(side="left", padx=(t.px(4), 0))
            lb = tk.Label(f, text=text, font=t.f_body, fg=TEXT3, bg=BG, padx=t.px(12), pady=t.px(8), cursor="hand2")
            lb.pack()
            bar = tk.Frame(f, bg=BG, height=t.px(2))
            bar.pack(fill="x")
            lb.bind("<Button-1>", lambda e, k=key: self.show_page(k))
            lb.bind("<Enter>", lambda e, k=key: self._tabs[k][0].configure(fg=TEXT))
            lb.bind("<Leave>", lambda e, k=key: self._tabs[k][0].configure(fg=TEXT if self.page == k else TEXT3))
            self._tabs[key] = (lb, bar)
        self.header_sep = tk.Frame(self, bg=BORDER, height=1)
        self.header_sep.pack(side="top", fill="x")

    def _build_bar(self) -> None:
        """底栏（共用主题 ui_theme.BottomBar，任何页面都在）：左边 Claude 桌面端状态，进行中时换成正在做什么、
        进度条在固定位置来回跑；右边「重启 / 打开」链接和「开始迁移」。位置和大小都不随状态变（规范 §4「界面稳定」）。"""
        t = self.theme
        self.bar = ui.BottomBar(self, t)
        self.bar.pack(side="bottom", fill="x")
        self.btn_go = ttk.Button(self.bar.right, text="开始迁移", style="Primary.TButton", command=self.do_migrate,
                                 cursor="hand2")
        self.btn_go.pack(side="right")
        self.claude_link = Link(self.bar.links, t, "", self._claude_action)
        self.claude_link.pack(side="right")

    def show_page(self, key: str) -> None:
        for page in self._pages.values():
            page.pack_forget()
        self._pages[key].pack(fill="both", expand=True)
        self.page = key
        for k, (lb, bar) in self._tabs.items():
            lb.configure(fg=TEXT if k == key else TEXT3)
            bar.configure(bg=ACCENT if k == key else BG)
        if key == "history":
            self.refresh_backups()
        elif key == "help":
            self._fill_help()

    def _on_wheel(self, e) -> None:
        w = self.winfo_containing(e.x_root, e.y_root)
        while w is not None:
            if isinstance(w, ScrollList):
                w.scroll(e.delta)
                return
            if isinstance(w, (tk.Text, ttk.Treeview)):
                return  # 它们自己会滚
            w = w.master

    # ---------------------------------------------------------------- 线程与日志
    def log(self, msg: str) -> None:
        self._calls.put(time.strftime("[%H:%M:%S] ") + msg)

    def _call(self, fn) -> None:
        """从工作线程安排一个在界面线程里执行的调用。"""
        self._calls.put(fn)

    def _pump(self) -> None:
        self.after(60, self._pump)  # 先排下一次：下面的调用可能打开对话框（嵌套事件循环）
        while True:
            try:
                item = self._calls.get_nowait()
            except queue.Empty:
                return
            if callable(item):
                item()
            else:
                self._append_log(item)

    def _set_busy(self, text: str) -> None:
        """进行中的操作：底栏左边写正在做什么、进度条来回跑；做完回到桌面端状态。"""
        self.busy = text
        self._render_claude()
        self._update_go()

    def _worker(self, fn, busy_text: str, done=None) -> None:
        self._set_busy(busy_text)

        def run() -> None:
            result = err = None
            try:
                result = fn()
            except Exception as e:  # noqa: BLE001 —— 统一回界面线程提示
                err = e
                if not isinstance(e, core.MigrationError):
                    self.log("[异常] " + traceback.format_exc().rstrip())
            self._call(lambda: self._finish(result, err, done))

        threading.Thread(target=run, daemon=True).start()

    def _finish(self, result, err, done) -> None:
        self._set_busy("")
        if err is not None:
            self.log(f"[错误] {err}")
            self.notify("出错了", [str(err)], tone="warn")
        elif done is not None:
            done(result)

    def ask(self, heading: str, lines=(), ok: str = "确定", tone: str = "accent") -> bool:
        return Dialog.show(self, heading, lines, ok=ok, cancel="取消", tone=tone)

    def notify(self, heading: str, lines=(), tone: str = "info") -> None:
        Dialog.show(self, heading, lines, ok="知道了", cancel=None, tone=tone)

    # ---------------------------------------------------------------- 迁移页
    def _build_home(self) -> None:
        t = self.theme
        page = tk.Frame(self.pages, bg=BG)
        self._pages["home"] = page

        body = tk.Frame(page, bg=BG)
        body.pack(fill="both", expand=True, padx=t.px(24), pady=(t.px(16), t.px(12)))
        self.view_main = tk.Frame(body, bg=BG)
        self.view_empty = tk.Frame(body, bg=BG)
        self.empty_card = card(self.view_empty, t, 28)
        self.empty_card.pack(fill="x")
        self._build_main_view(self.view_main)

    def _build_main_view(self, v: tk.Frame) -> None:
        t = self.theme
        v.columnconfigure(0, weight=1, uniform="side")
        v.columnconfigure(2, weight=1, uniform="side")
        lh = tk.Frame(v, bg=BG)
        lh.grid(row=0, column=0, sticky="ew", pady=(0, t.px(8)))
        tk.Label(lh, text="从旧账号", font=t.f_h, fg=TEXT, bg=BG).pack(side="left")
        tk.Label(lh, text="勾选要搬的账号", font=t.f_small, fg=TEXT3, bg=BG).pack(side="left", padx=(t.px(8), 0))
        rh = tk.Frame(v, bg=BG)
        rh.grid(row=0, column=2, sticky="ew", pady=(0, t.px(8)))
        tk.Label(rh, text="到新账号", font=t.f_h, fg=TEXT, bg=BG).pack(side="left")
        Link(rh, t, "重新检测", self.rescan, bg=BG, fg=TEXT2, icon="refresh").pack(side="right")

        src = card(v, t, 4)
        src.grid(row=1, column=0, sticky="nsew")
        self.src_list = ScrollList(src, bg=CARD, max_height=t.px(250))
        self.src_list.pack(fill="both", expand=True)
        tk.Label(v, text=t.icon("arrow") or "→", font=t.icon_font(16) if t.icon_family else t.f_h,
                 fg=ACCENT, bg=BG).grid(row=1, column=1, padx=t.px(12))
        self.tgt_card = card(v, t, 16)
        self.tgt_card.grid(row=1, column=2, sticky="new")

        self.adv_link = Link(v, t, "高级选项", self._toggle_adv, bg=BG, fg=TEXT2, icon="right")
        self.adv_link.grid(row=2, column=0, sticky="w", pady=(t.px(14), 0))
        self.adv = card(v, t, 4)
        CheckRow(self.adv, t, self.update_newer, "旧账号里更新过的对话，覆盖新账号里的旧版本",
                 ["比较最后活动时间；被覆盖的旧版本留在备份里，撤销时恢复。默认开启。"],
                 command=self._options_changed).pack(fill="x")
        tk.Frame(self.adv, bg=LINE, height=1).pack(fill="x", padx=t.px(14))
        CheckRow(self.adv, t, self.all_ws, "包含旧账号的全部组织目录",
                 ["默认只搬对话最多的那个组织目录，一般就够了。"], command=self._options_changed).pack(fill="x")

        self.summary = card(v, t, 18)
        self.summary.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(t.px(14), 0))

    def _toggle_adv(self) -> None:
        if self.adv.winfo_manager():
            self.adv.grid_remove()
            self.adv_link.set_icon("right")
        else:
            self.adv.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(self.theme.px(8), 0))
            self.adv_link.set_icon("down")

    def scan(self, keep_result: bool = False) -> None:
        if not self.fixed_bases:
            self.bases = core.get_session_bases()
        if self.bases != self._logged_bases:  # 完整路径写进日志（帮助页也列出）
            self._logged_bases = list(self.bases)
            for b in self.bases:
                self.log(f"[目录] [{core.profile_label(b)}] {b}" + ("" if os.path.isdir(b) else "（不存在）"))
            for note in core.layout_notes(self.bases):
                self.log(f"[提示] {note}")
        self.accounts = core.list_accounts_multi(self.bases)
        self.latest = {a.name: core.peek_latest(a.ws_path) for a in self.accounts}
        self.multi_profile = len({a.profile for a in self.accounts}) > 1
        if self.target_var.get() not in {a.name for a in self.accounts}:
            self.target_var.set(core.choose_target(self.accounts).name if self.accounts else "")
        if not keep_result:
            self.result = None
        self.log(f"[扫描] 找到 {len(self.accounts)} 个账号。")
        if len(self.accounts) >= 2:
            self.view_empty.pack_forget()
            self.view_main.pack(fill="both", expand=True)
            if self._home_signature("main") != self._home_key:  # 「重新检测」后账号没变就不重画卡片
                self._render_sources()
                self._render_target()
            self._schedule_plan()
        else:
            self.view_main.pack_forget()
            self.view_empty.pack(fill="both", expand=True)
            self.plan = None
            if self._home_signature("empty") != self._home_key:
                self._render_empty()
                self._home_key = self._home_signature("empty")
            self._update_go()

    def _home_signature(self, view: str) -> tuple:
        """迁移页卡片显示的全部内容；和上次一样就不重画。"""
        return (view, self.target_var.get(), self.multi_profile, any(os.path.isdir(b) for b in self.bases),
                tuple((a.name, a.sessions, a.last_login, a.maybe_new, a.profile, bool(a.ws_path),
                       self.latest.get(a.name)) for a in self.accounts))

    def rescan(self) -> None:
        if self.busy:
            return
        self.scan()
        self.refresh_claude()

    def _target(self) -> core.Account | None:
        return next((a for a in self.accounts if a.name == self.target_var.get()), None)

    def _badges(self, a: core.Account) -> list[tuple[str, str]]:
        out = []
        if a.last_login:
            out.append(("最近登录", "accent"))
        if a.maybe_new:
            out.append(("新账号", "ok"))
        if self.multi_profile:
            out.append((a.profile, "gray"))
        return out

    def _account_lines(self, a: core.Account) -> list[str]:
        title, when = self.latest.get(a.name, ("", 0.0))
        lines = [f"{a.sessions} 个对话" + (f" · 最近活动 {friendly_time(when)}" if when else "")]
        if title:
            lines.append(f"最近：{title}")
        return lines

    def _candidates(self) -> list[core.Account]:
        """可作源的账号：除目标外、有对话的，按对话数从多到少（多个源有重复时，排在前面的优先）。"""
        target = self.target_var.get()
        return sorted((a for a in self.accounts if a.name != target and a.sessions > 0),
                      key=lambda a: a.sessions, reverse=True)

    def _render_sources(self) -> None:
        t = self.theme
        inner = self.src_list.inner
        clear(inner)
        cands = self._candidates()
        empty = sum(1 for a in self.accounts if a.name != self.target_var.get() and a.sessions == 0)
        if not cands:
            tk.Label(inner, text="其他账号里都没有对话，没有可搬的。", font=t.f_body, fg=TEXT3,
                     bg=CARD).pack(anchor="w", padx=t.px(14), pady=t.px(14))
        for i, a in enumerate(cands):
            if i:
                tk.Frame(inner, bg=LINE, height=1).pack(fill="x", padx=t.px(14))
            # 每个账号的勾选变量只建一次、之后复用：每次重画都新建的话，旧变量被回收时
            # 可能落在后台线程里，Tk 变量的析构要回到界面线程执行，会白白卡住那个线程
            var = self.src_vars.get(a.name)
            if var is None:
                var = self.src_vars[a.name] = tk.BooleanVar()
            var.set(a.name not in self.unchecked)
            CheckRow(inner, t, var, account_label(a.name), self._account_lines(a), self._badges(a),
                     command=lambda n=a.name: self._source_toggled(n)).pack(fill="x")
        if empty:
            tk.Label(inner, text=f"另有 {empty} 个账号没有对话，没有列出。", font=t.f_small, fg=TEXT3,
                     bg=CARD).pack(anchor="w", padx=t.px(14), pady=(0, t.px(10)))

    def _render_target(self) -> None:
        t = self.theme
        c = self.tgt_card
        clear(c)
        self._home_key = self._home_signature("main")  # 两张卡片总是一起画，签名记在这里
        a = self._target()
        if a is None:
            return
        head = tk.Frame(c, bg=CARD)
        head.pack(fill="x")
        tk.Label(head, text=account_label(a.name), font=t.f_bold, fg=TEXT, bg=CARD).pack(side="left")
        for text, tone in self._badges(a):
            badge(head, t, text, tone).pack(side="left", padx=(t.px(6), 0))
        for line in self._account_lines(a):
            ElidedLabel(c, line, t.f_small, fg=TEXT3, bg=CARD).pack(fill="x", pady=(t.px(2), 0))
        if not a.ws_path:
            WrapLabel(c, "这个账号还没有会话目录：先用它在 Claude 桌面端打开一次 Code 标签页。", t.f_small,
                      fg=WARN, bg=CARD).pack(fill="x", pady=(t.px(6), 0))
        self.pick_link = Link(c, t, "换一个账号", self._pick_target, bg=CARD, icon="down")
        self.pick_link.pack(anchor="w", pady=(t.px(10), 0))

    def _pick_target(self) -> None:
        m = tk.Menu(self, tearoff=0)
        for a in self.accounts:
            marks = "、".join(text for text, _ in self._badges(a))
            m.add_radiobutton(label=f"{account_label(a.name)}  ·  {a.sessions} 个对话" + (f"  ·  {marks}" if marks else ""),
                              variable=self.target_var, value=a.name, command=self._target_changed)
        w = self.pick_link
        m.tk_popup(w.winfo_rootx(), w.winfo_rooty() + w.winfo_height())

    def _target_changed(self) -> None:
        self.result = None
        self._render_sources()
        self._render_target()
        self._schedule_plan()

    def _source_toggled(self, name: str) -> None:
        if self.src_vars[name].get():
            self.unchecked.discard(name)
        else:
            self.unchecked.add(name)
        self.result = None
        self._schedule_plan()

    def _options_changed(self) -> None:
        self.result = None
        self._schedule_plan()

    def _schedule_plan(self) -> None:
        """后台重算计划（只读）；读过的记录有缓存，改勾选时很快。过期的结果直接丢掉。"""
        self._plan_gen += 1
        gen = self._plan_gen
        self.plan, self.plan_error = None, ""
        target = self._target()
        sources = [a for a in self._candidates() if self.src_vars.get(a.name) and self.src_vars[a.name].get()]
        if target is None or not sources:
            self.planning = False
            self.plan_error = "勾选至少一个旧账号。" if self._candidates() else "其他账号里都没有对话，不需要迁移。"
            self._render_summary()
            self._update_go()
            return
        opts = dict(all_workspaces=self.all_ws.get(), update_newer=self.update_newer.get())
        self.planning = True
        self._update_go()
        # 读过的记录有缓存，多数情况下几十毫秒就算完：0.25 秒后还没算完才显示「正在统计」，免得汇总卡片闪一下
        self.after(250, lambda: self._render_summary() if self.planning and gen == self._plan_gen else None)

        def work() -> None:
            plan, err = None, ""
            try:
                plan = core.build_plan(self.accounts, target_account=target, source_accounts=sources,
                                       cache=True, **opts)
            except core.MigrationError as e:
                err = str(e)
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
                self.log("[异常] " + traceback.format_exc().rstrip())
            self._call(lambda: self._plan_ready(gen, plan, err))

        threading.Thread(target=work, daemon=True).start()

    def _plan_ready(self, gen: int, plan, err: str) -> None:
        if gen != self._plan_gen:
            return
        self.planning = False
        self.plan, self.plan_error = plan, err
        if plan:
            for w in plan.warnings:
                self.log(f"[警告] {w}")
        self._render_summary()
        self._update_go()

    def _update_go(self) -> None:
        p = self.plan
        ready = (not self.busy and not self.planning and p is not None
                 and p.total_copy + p.total_update > 0 and len(self.accounts) >= 2)
        self.btn_go.state(["!disabled"] if ready else ["disabled"])

    def _summary_head(self, c, icon: tuple[str, str] | None, text: str) -> None:
        t = self.theme
        row = tk.Frame(c, bg=CARD)
        row.pack(fill="x")
        if icon and t.icon_family:
            tk.Label(row, text=t.icon(icon[0]), font=t.icon_font(14), fg=icon[1], bg=CARD).pack(
                side="left", anchor="n", padx=(0, t.px(10)))
        WrapLabel(row, text, t.f_h, fg=TEXT, bg=CARD).pack(side="left", fill="x", expand=True)

    def _chips(self, c, items) -> None:
        t = self.theme
        items = [(f"{label} {n}", tone) for label, n, tone in items if n]
        if not items:
            return
        row = tk.Frame(c, bg=CARD)
        row.pack(fill="x", pady=(t.px(10), 0))
        for text, tone in items:
            badge(row, t, text, tone).pack(side="left", padx=(0, t.px(6)), ipady=t.px(2))

    def _render_summary(self) -> None:
        t = self.theme
        c = self.summary
        p = self.plan
        key = (id(self.result) if self.result else None, self.planning, self.plan_error,
               (p.total_copy, p.total_update, p.total_skip, p.total_deleted, p.total_invalid, len(p.warnings))
               if p else None)
        if key == self._summary_key:  # 数字没变就不重画（重新检测、来回勾选时常见）
            return
        self._summary_key = key
        clear(c)
        if self.result:
            self._render_result(c)
            return
        if self.planning:
            self._summary_head(c, None, "正在统计要搬的对话…")
            pb = ttk.Progressbar(c, mode="indeterminate", style="Accent.Horizontal.TProgressbar")
            pb.pack(fill="x", pady=(t.px(12), 0))
            pb.start(12)
            return
        if self.plan_error:
            self._summary_head(c, ("info", TEXT3), self.plan_error)
            return
        plan = self.plan
        if plan is None:
            return
        n = plan.total_copy + plan.total_update
        if n:
            row = tk.Frame(c, bg=CARD)
            row.pack(fill="x")
            tk.Label(row, text=str(n), font=t.f_big, fg=TEXT, bg=CARD).pack(side="left")
            tk.Label(row, text="个对话将出现在新账号的侧栏", font=t.f_body, fg=TEXT2, bg=CARD).pack(
                side="left", anchor="s", padx=(t.px(8), 0), pady=(0, t.px(6)))
        else:
            self._summary_head(c, ("done", OK), "新账号里已经有这些对话了，不需要迁移。")
        self._chips(c, [("新增", plan.total_copy, "accent"), ("更新", plan.total_update, "accent"),
                        ("已在新账号", plan.total_skip, "gray"), ("新账号里删过", plan.total_deleted, "gray"),
                        ("无法识别", plan.total_invalid, "warn")])
        if plan.warnings:
            WrapLabel(c, f"新账号里有 {len(plan.warnings)} 条记录读不出来，只按文件名判断重复（详见运行日志）。",
                      t.f_small, fg=WARN, bg=CARD).pack(fill="x", pady=(t.px(8), 0))
        foot = tk.Frame(c, bg=CARD)
        foot.pack(fill="x", pady=(t.px(12), 0))
        Link(foot, t, "查看明细", self.show_details, bg=CARD).pack(side="right", anchor="n", padx=(t.px(12), 0))
        if n:
            WrapLabel(foot, "开始后先备份新账号的会话目录并核对，再写入；搬错了可以在「记录」页撤销。",
                      t.f_small, fg=TEXT3, bg=CARD).pack(side="left", fill="x", expand=True)

    def _render_result(self, c) -> None:
        t = self.theme
        r = self.result
        res: core.ApplyResult = r["res"]
        self._summary_head(c, ("done", OK), f"已把 {res.copied + res.updated} 个对话迁移到 {account_label(r['target'])}")
        self._chips(c, [("新增", res.copied, "accent"), ("更新", res.updated, "accent"),
                        ("已在新账号", res.skipped, "gray"), ("新账号里删过", res.skipped_deleted, "gray"),
                        ("无法识别", res.invalid, "warn")])
        if r["reopened"]:
            msg = "Claude 桌面端已重新打开：确认登录的是这个账号，打开 Code 标签页就能看到它们。"
        elif r["closed"]:
            msg = "没能自动重新打开 Claude 桌面端：请从开始菜单打开它，登录这个账号后打开 Code 标签页。"
        elif self.guard_claude:
            msg = "打开 Claude 桌面端（开着的话要彻底退出再开），登录这个账号、打开 Code 标签页就能看到它们。"
        else:
            msg = "这是指定的测试目录，Claude 桌面端没有被关闭或重新打开。"
        WrapLabel(c, msg, t.f_body, fg=TEXT2, bg=CARD).pack(fill="x", pady=(t.px(10), 0))
        links = tk.Frame(c, bg=CARD)
        links.pack(fill="x", pady=(t.px(12), 0))
        Link(links, t, "撤销这次迁移", self._undo_result, bg=CARD, icon="undo").pack(side="left")
        Link(links, t, "打开备份文件夹", lambda: self._open(res.backup_path), bg=CARD, fg=TEXT2).pack(
            side="left", padx=(t.px(18), 0))
        Link(links, t, "查看日志", lambda: self._open(res.log_path), bg=CARD, fg=TEXT2).pack(
            side="left", padx=(t.px(18), 0))

    def _render_empty(self) -> None:
        t = self.theme
        c = self.empty_card
        clear(c)
        found = any(os.path.isdir(b) for b in self.bases)
        if not found:
            heading = "没有找到 Claude 桌面端的会话目录"
            sub = "装好 Claude 桌面端、打开过一次 Code 标签页之后才会有。"
        elif not self.accounts:
            heading = "还没有任何账号的会话记录"
            sub = "用 Claude 桌面端的 Code 标签页聊过之后，这里就能看到。"
        else:
            a = self.accounts[0]
            heading = "只找到 1 个账号，暂时没有要搬的对话"
            title, _ = self.latest.get(a.name, ("", 0.0))
            sub = f"{account_label(a.name)} · {a.sessions} 个对话" + (f" · 最近：{title}" if title else "")
        if t.icon_family:
            tk.Label(c, text=t.icon("chat"), font=t.icon_font(26), fg=ACCENT, bg=CARD).pack(anchor="w")
        tk.Label(c, text=heading, font=t.f_h, fg=TEXT, bg=CARD).pack(anchor="w", pady=(t.px(10), 0))
        ElidedLabel(c, sub, t.f_small, fg=TEXT3, bg=CARD).pack(fill="x", pady=(t.px(4), 0))
        tk.Frame(c, bg=LINE, height=1).pack(fill="x", pady=t.px(18))
        tk.Label(c, text="换了新账号以后：", font=t.f_bold, fg=TEXT, bg=CARD).pack(anchor="w")
        steps = ("在 Claude 桌面端登录新账号",
                 "打开一次 Code 标签页（不用发消息），新账号的目录才会出现",
                 "回到这里点「重新检测」")
        for i, text in enumerate(steps, 1):
            row = tk.Frame(c, bg=CARD)
            row.pack(fill="x", pady=(t.px(10), 0))
            tk.Label(row, image=t.step_img, text=str(i), compound="center", font=t.f_small, fg=ACCENT,
                     bg=CARD).pack(side="left")
            WrapLabel(row, text, t.f_body, fg=TEXT2, bg=CARD).pack(side="left", fill="x", expand=True,
                                                                   padx=(t.px(10), 0))
        ttk.Button(c, text="重新检测", style="Primary.TButton", command=self.rescan, cursor="hand2").pack(
            anchor="w", pady=(t.px(22), 0))

    def show_details(self) -> None:
        """逐条列出计划：每个对话的标题、最近活动、来自哪个账号、怎么处理及原因。"""
        plan = self.plan
        if plan is None:
            return
        t = self.theme
        rows = []
        for src, item in zip(plan.sources, plan.items):
            for action, fp, reason in item.decisions:
                m = core._read_meta(fp, cache=True)
                when = m.activity / 1000 if m.activity else m.mtime
                why = reason.replace("目标", "新账号").replace("源", "旧账号")  # 和界面用词一致
                rows.append((ACTION_ORDER[action], -when, action, m.title or "（没有标题）", friendly_time(when),
                             account_label(src.name), why))
        rows.sort(key=lambda r: (r[0], r[1]))
        win = tk.Toplevel(self)
        win.title("迁移明细")
        win.configure(bg=BG)
        win.transient(self)
        w, h = t.px(860), t.px(540)
        win.geometry(f"{w}x{h}+{self.winfo_rootx() + (self.winfo_width() - w) // 2}"
                     f"+{self.winfo_rooty() + max(0, (self.winfo_height() - h) // 3)}")
        top = tk.Frame(win, bg=BG)
        top.pack(fill="x", padx=t.px(20), pady=(t.px(16), t.px(10)))
        tk.Label(top, text=f"共 {len(rows)} 条记录", font=t.f_h, fg=TEXT, bg=BG).pack(side="left")
        tk.Label(top, text="只读预览，没有改动任何文件", font=t.f_small, fg=TEXT3, bg=BG).pack(
            side="left", padx=(t.px(10), 0))
        bottom = tk.Frame(win, bg=BG)
        bottom.pack(side="bottom", fill="x", padx=t.px(20), pady=t.px(14))
        ttk.Button(bottom, text="关闭", style="Secondary.TButton", command=win.destroy, cursor="hand2").pack(side="right")
        frame = card(win, t, 3)
        frame.pack(fill="both", expand=True, padx=t.px(20))
        cols = {"act": ("处理", 100, False), "title": ("对话", 300, True), "when": ("最近活动", 120, False),
                "src": ("来自", 110, False), "why": ("说明", 240, True)}
        tree = ttk.Treeview(frame, columns=list(cols), show="headings")
        for key, (text, width, stretch) in cols.items():
            tree.heading(key, text=text, anchor="w")
            tree.column(key, width=t.px(width), stretch=stretch, anchor="w")
        for tag, color in (("exist", TEXT3), ("deleted", TEXT3), ("invalid", WARN)):
            tree.tag_configure(tag, foreground=color)
        for r in rows:
            tree.insert("", "end", values=(ACTION_LABEL[r[2]], r[3], r[4], r[5], r[6]), tags=(r[2],))
        vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview, style="Card.Vertical.TScrollbar")
        tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        win.bind("<Escape>", lambda e: win.destroy())

    # ---------------------------------------------------------------- Claude 桌面端
    def refresh_claude(self, delay: int = 0) -> None:
        if not (self.guard_claude and IS_WINDOWS):
            self._render_claude()
            return

        def run() -> None:
            st = core.get_claude_status()
            self._call(lambda: self._set_claude(st))

        self.after(delay, lambda: threading.Thread(target=run, daemon=True).start())

    def _set_claude(self, st: core.ClaudeStatus) -> None:
        self.claude = st
        if st.relaunch_kind != "none":
            self.last_launch = (st.relaunch_kind, st.relaunch_value)
        self.log(f"[Claude] running={st.running} pids={len(st.pids)} kind={st.relaunch_kind} "
                 f"ignored={st.ignored} unknown={st.unknown}" + (f" error={st.error}" if st.error else ""))
        self._render_claude()

    def _render_claude(self) -> None:
        """底栏左边：进行中时写正在做什么（进度条跑），否则写 Claude 桌面端状态；「重启 / 打开」链接没有可做的时置灰。"""
        st = self.claude
        link = ""
        if not self.guard_claude:
            tone, text = "neutral", "正在使用指定的测试目录：不检测、也不关闭 Claude 桌面端"
        elif not IS_WINDOWS:
            tone, text = "warning", "迁移前请先完全退出 Claude 桌面端（包括托盘图标）"
        elif st is None:
            tone, text = "neutral", "正在检测 Claude 桌面端…"
        elif st.error:
            tone, text = "warning", "检测不到 Claude 桌面端的状态；迁移前请确认它已完全退出"
        elif st.running:
            tone, text, link = "success", "Claude 桌面端正在运行 · 迁移时会先关闭，完成后自动重新打开", "重启"
        else:
            tone, text = "neutral", "Claude 桌面端没有运行"
            link = "打开" if self.last_launch[0] != "none" else ""
        if self.busy:
            self.bar.set_status(self.busy, "accent")
            self.bar.set_progress(None, busy=True)
        else:
            self.bar.set_status(text, tone)
            self.bar.set_progress(None)
        self.claude_link.set_text(link)
        self.claude_link.set_enabled(bool(link) and not self.busy)

    def _claude_action(self) -> None:
        if self.busy:
            return
        if self.claude and self.claude.running:
            self.do_restart()
        elif self.last_launch[0] != "none":
            ok, msg = core.relaunch_claude(*self.last_launch)
            self.log(f"[Claude] {msg}")
            if not ok:
                self.notify("没能打开 Claude", [msg], tone="warn")
            self.refresh_claude(3000)

    def do_restart(self) -> None:
        if not self.ask("重启 Claude 桌面端？", ["正在进行的 Claude 对话会被中断。",
                                                "只结束桌面端本体；终端里运行的 Claude Code 不受影响。"], ok="重启"):
            return

        def work() -> None:
            st = core.get_claude_status()
            if st.error:
                raise core.MigrationError(f"检测不到 Claude 桌面端：{st.error}")
            kind, value = (st.relaunch_kind, st.relaunch_value) if st.relaunch_kind != "none" else self.last_launch
            if st.running:
                ok, msg = core.close_claude(st)
                self.log(f"[Claude] {msg}")
                if not ok:
                    raise core.MigrationError(msg)
            ok, msg = core.relaunch_claude(kind, value)
            self.log(f"[Claude] {msg}")
            if not ok:
                raise core.MigrationError(msg)

        self._worker(work, "正在重启 Claude 桌面端…", done=lambda _r: self.refresh_claude(3000))

    def _claude_lines(self, st: core.ClaudeStatus | None) -> tuple[list, bool]:
        """确认框里关于 Claude 桌面端的说明，以及是否要先关闭它。"""
        if not self.guard_claude:
            return [], False
        if not IS_WINDOWS or st is None:
            return [("开始前请先完全退出 Claude 桌面端（包括托盘图标）。", WARN)], False
        lines, close = [], False
        if st.error:
            lines.append((f"检测不到 Claude 桌面端是否在运行（{st.error}）。请先确认它已完全退出，包括托盘图标。", WARN))
        elif st.running:
            close = True
            lines.append("Claude 桌面端正在运行：会先关闭它（正在进行的 Claude 对话会被中断），完成后自动重新打开。"
                         "终端里运行的 Claude Code 不受影响。")
        if st.unknown:
            lines.append((f"另有 {st.unknown} 个名为 Claude 的进程读不到路径（例如以管理员身份运行的），可能是桌面端；"
                          "本工具不会结束它们，请确认桌面端已完全退出。", WARN))
        return lines, close

    def _guarded(self, heading: str, lines: list, ok: str, run, done, busy_text: str, tone: str = "accent") -> None:
        """会改会话文件的操作：先查 Claude 桌面端，把「会先关闭它」写进同一个确认框；
        确认后 关闭桌面端 → run() → 重新打开。done(结果, 是否关闭过, 是否已重新打开)。"""

        def confirm(st: core.ClaudeStatus | None) -> None:
            extra, close = self._claude_lines(st)
            if not self.ask(heading, lines[:1] + extra + lines[1:], ok=ok, tone=tone):
                return

            def work():
                closed = reopened = False
                if close:
                    self._call(lambda: self._set_busy("正在关闭 Claude 桌面端…"))
                    ok_, msg = core.close_claude(st)
                    self.log(f"[Claude] {msg}")
                    if not ok_:
                        raise core.MigrationError(f"{msg}\n已取消，没有改动任何文件。")
                    closed = True
                    self._call(lambda: self._set_busy(busy_text))
                try:
                    result = run()
                finally:
                    if closed and st.relaunch_kind != "none":  # 出错也把桌面端开回来
                        self._call(lambda: self._set_busy("正在重新打开 Claude 桌面端…"))
                        reopened, msg = core.relaunch_claude(st.relaunch_kind, st.relaunch_value)
                        self.log(f"[Claude] {msg}")
                return result, closed, reopened

            def finished(r) -> None:
                done(*r)
                self.refresh_claude(3000 if r[2] else 0)

            self._worker(work, busy_text, done=finished)

        if self.guard_claude and IS_WINDOWS:
            def check() -> None:
                st = core.get_claude_status()

                def back() -> None:
                    self._set_busy("")
                    self._set_claude(st)
                    confirm(st)

                self._call(back)

            self._set_busy("正在检查 Claude 桌面端…")
            threading.Thread(target=check, daemon=True).start()
        else:
            confirm(None)

    # ---------------------------------------------------------------- 迁移 / 撤销 / 还原 / 删除
    def do_migrate(self) -> None:
        plan = self.plan
        if self.busy or plan is None or plan.total_copy + plan.total_update == 0:
            return
        n = plan.total_copy + plan.total_update
        detail = f"先完整备份新账号的会话目录并核对，再写入：新增 {plan.total_copy} 个"
        if plan.total_update:
            detail += f"，更新 {plan.total_update} 个（旧版本留在备份里）"
        target = plan.target.name
        self._guarded(f"把 {n} 个对话迁移到 {account_label(target)}？",
                      [detail + "。", "搬错了可以在「记录」页撤销这次迁移。"], "开始迁移",
                      run=lambda: core.apply_migration(plan, log=self.log),
                      done=lambda res, closed, reopened: self._after_migrate(target, res, closed, reopened),
                      busy_text="正在备份并写入…")

    def _after_migrate(self, target: str, res: core.ApplyResult, closed: bool, reopened: bool) -> None:
        if not res.backup_path:
            self.scan()
            self.notify("不需要迁移", ["按磁盘现状复核后，没有需要写入的对话。没有改动任何文件，也没有创建备份。"])
            return
        self.log(f"[完成] 新增 {res.copied}，更新 {res.updated}，已在新账号 {res.skipped}，"
                 f"新账号里删过 {res.skipped_deleted}，无法识别 {res.invalid}；新账号现共 {res.now_total} 个。")
        self.log(f"[完成] 备份：{res.backup_path}")
        self.log(f"[完成] 日志：{res.log_path}")
        self.result = {"res": res, "target": target, "closed": closed, "reopened": reopened}
        self.scan(keep_result=True)

    def _undo_result(self) -> None:
        path = self.result["res"].backup_path if self.result else ""
        b = next((x for x in core.list_backups_multi(self.bases) if x.backup_path == path), None)
        if b is None:
            self.notify("找不到这次的备份", ["它可能已被删除或改名。可以到「记录」页看看。"], tone="warn")
            return
        self.do_undo(b)

    def do_undo(self, b: core.BackupInfo) -> None:
        if self.busy:
            return
        try:
            pv = core.undo_migration(b, dry_run=True)  # 只算不改，让确认框里的数字是真的
        except core.MigrationError as e:
            self.notify("无法撤销", [str(e)], tone="warn")
            return
        kept = len(pv.modified) + len(pv.broken)
        if pv.moved + pv.restored == 0:
            self.notify("没有可撤销的内容", [f"这次迁移写入的对话里，{kept} 个之后被改动过（例如在新账号里接着聊过），"
                                          f"{len(pv.missing)} 个已经不在了。没有改动任何文件。"])
            return
        lines = []
        if pv.moved:
            lines.append(f"从新账号的侧栏移走 {pv.moved} 个对话（这次迁入、之后没再动过的）。")
        if pv.restored:
            lines.append(f"{pv.restored} 个被这次迁移覆盖的对话，恢复成迁移前的版本。")
        if kept:
            lines.append(f"{kept} 个迁入后又被改动过（例如你接着聊过），保留不动。")
        if pv.missing:
            lines.append(f"{len(pv.missing)} 个已经不在了，跳过。")
        lines.append("移走的文件放进旁边的「撤销」文件夹，不会删除；对话内容本身不受影响。")
        self._guarded(f"撤销 {stamp_time(b.stamp)} 的迁移？", lines, "撤销",
                      run=lambda: core.undo_migration(b, log=self.log), done=self._after_undo,
                      busy_text="正在撤销…")

    def _after_undo(self, res: core.UndoResult, closed: bool, reopened: bool) -> None:
        lines = [f"移走 {res.moved} 个对话，恢复旧版本 {res.restored} 个。"]
        if res.modified:
            lines.append(f"{len(res.modified)} 个写入后被改动过，留在原处（清单在运行日志里）。")
            for n in res.modified:
                self.log(f"[撤销] 已被改动，保留：{n}")
        if res.broken:
            lines.append(f"{len(res.broken)} 个在备份里找不到对得上的旧版本，保留当前文件。")
        if res.missing:
            lines.append(f"{len(res.missing)} 个已经不在了，跳过。")
        if reopened:
            lines.append("Claude 桌面端已重新打开，侧栏已更新。")
        elif self.guard_claude:
            lines.append("重新打开 Claude 桌面端后侧栏生效。")
        self.result = None
        self.refresh_backups()
        self.scan()
        self.notify("撤销完成", lines, tone="ok")

    def do_rollback(self, b: core.BackupInfo) -> None:
        if self.busy:
            return
        lost, cur, old = core.rollback_impact(b)
        lines = [f"当前的会话目录（{cur} 个对话）整体改名移开保留，再把这个备份（{old} 个对话）换回原位。",
                 (f"有 {lost} 个对话会从侧栏消失（包括这次备份之后新建的）。它们不会被删除，"
                  "仍在移开的目录里，「记录」页能看到。", WARN),
                 "一般请优先用「撤销」：它只移走那次迁移写入的对话。"]

        def done(_replaced, closed: bool, reopened: bool) -> None:
            self.result = None
            self.refresh_backups()
            self.scan()
            self.notify("已整体还原", ["Claude 桌面端已重新打开。" if reopened else "重新打开 Claude 桌面端后侧栏生效。"],
                        tone="ok")

        self._guarded("整体还原到这个备份？", lines, "整体还原", run=lambda: core.rollback(b, log=self.log),
                      done=done, busy_text="正在整体还原…", tone="danger")

    def do_delete(self, b: core.BackupInfo) -> None:
        if self.busy:
            return
        title, _ = self._history_text(b)[:2]
        if b.kind == "backup":
            what = "这是迁移前的备份，删除后就不能再用它撤销那次迁移或整体还原。"
        else:
            what = f"里面的 {b.files} 个会话记录已经不在侧栏，删除后不能恢复。"
        if not self.ask(f"永久删除「{title}」（{stamp_time(b.stamp)}）？",
                        [what, "对话内容本身（.jsonl）不受影响。"], ok="删除", tone="danger"):
            return
        self.log(f"[删除] {b.backup_path}")
        self._worker(lambda: core.delete_backup_dir(b.backup_path, bases=self.bases), "正在删除…",
                     done=lambda _r: self.refresh_backups())

    # ---------------------------------------------------------------- 记录页
    def _build_history(self) -> None:
        t = self.theme
        page = tk.Frame(self.pages, bg=BG)
        self._pages["history"] = page
        top = tk.Frame(page, bg=BG)
        top.pack(fill="x", padx=t.px(24), pady=(t.px(16), t.px(12)))
        Link(top, t, "刷新", self.refresh_backups, bg=BG, fg=TEXT2, icon="refresh").pack(side="right", anchor="n")
        tk.Label(top, text="迁移记录", font=t.f_h, fg=TEXT, bg=BG).pack(anchor="w")
        WrapLabel(top, "每次迁移前都会自动备份新账号的会话目录。撤销只移走那次迁入、之后没再动过的对话；"
                       "你在新账号里新建或接着聊过的不受影响。", t.f_small, fg=TEXT3, bg=BG).pack(fill="x", pady=(t.px(4), 0))
        self.hist = ScrollList(page, bg=BG, scrollbar_style="Page.Vertical.TScrollbar")
        self.hist.pack(fill="both", expand=True, padx=(t.px(24), t.px(12)), pady=(0, t.px(16)))

    def _history_text(self, b: core.BackupInfo) -> tuple[str, str, tuple[str, str] | None, str, str]:
        """(标题, 说明, 状态徽章, 图标, 图标颜色)。"""
        when = stamp_time(b.stamp)
        acct = account_label(b.account)
        if b.kind == "backup" and b.has_manifest:
            state = ("已撤销", "gray") if b.undone else ("可撤销", "ok")
            return f"迁移到 {acct}", f"{when} · 写入 {b.manifest_files} 个对话", state, "history", ACCENT
        if b.kind == "backup":
            return ("旧版本留下的备份", f"{when} · {acct} · 备份时有 {b.files} 个对话",
                    ("不能按次撤销", "gray"), "folder", TEXT3)
        if b.kind == "undone":
            return "撤销时移出的对话", f"{when} · {acct} · {b.files} 个对话", None, "undo", TEXT3
        return "整体还原时移开的目录", f"{when} · {acct} · {b.files} 个对话", None, "folder", TEXT3

    def refresh_backups(self) -> None:
        """按备份目录增量更新：内容没变的卡片原样保留，只增删改有变化的那几张（删一条不会整页重画）。"""
        t = self.theme
        self.backups = core.list_backups_multi(self.bases)
        inner = self.hist.inner
        old = self._hist_rows
        rows: dict[str, tuple[tuple, ttk.Frame]] = {}
        for b in self.backups:
            sig = (b.kind, b.stamp, b.account, b.files, b.manifest_files, b.undone, b.workspace_path)
            hit = old.pop(b.backup_path, None)
            if hit and hit[0] == sig:
                rows[b.backup_path] = hit
            else:
                if hit:
                    hit[1].destroy()
                rows[b.backup_path] = (sig, self._history_card(inner, b))
        for _sig, w in old.values():  # 已经不在的（被删除、被换回）
            w.destroy()
        self._hist_rows = rows
        order = [rows[b.backup_path][1] for b in self.backups]
        if order != self._hist_order:  # 顺序或成员变了才重排
            for w in order:
                w.pack_forget()
            for w in order:
                w.pack(fill="x", pady=(0, t.px(8)), padx=(0, t.px(12)))
            self._hist_order = order
        if not self.backups and self._hist_empty is None:
            self._hist_empty = tk.Label(inner, text="还没有迁移记录。", font=t.f_body, fg=TEXT3, bg=BG)
            self._hist_empty.pack(anchor="w", pady=t.px(8))
        elif self.backups and self._hist_empty is not None:
            self._hist_empty.destroy()
            self._hist_empty = None

    def _history_card(self, parent, b: core.BackupInfo) -> ttk.Frame:
        """一条记录的卡片（不 pack，由 refresh_backups 排位置）。"""
        t = self.theme
        title, sub, state, icon, color = self._history_text(b)
        row = card(parent, t, 14)
        row.columnconfigure(1, weight=1)
        if t.icon_family:
            tk.Label(row, text=t.icon(icon), font=t.icon_font(14), fg=color, bg=CARD).grid(
                row=0, column=0, rowspan=2, padx=(t.px(2), t.px(12)))
        head = tk.Frame(row, bg=CARD)
        head.grid(row=0, column=1, sticky="w")
        tk.Label(head, text=title, font=t.f_bold, fg=TEXT, bg=CARD).pack(side="left")
        if state:
            badge(head, t, *state).pack(side="left", padx=(t.px(8), 0))
        ElidedLabel(row, sub, t.f_small, fg=TEXT3, bg=CARD).grid(row=1, column=1, sticky="ew", pady=(t.px(2), 0))
        btns = tk.Frame(row, bg=CARD)
        btns.grid(row=0, column=2, rowspan=2, sticky="e", padx=(t.px(10), 0))
        if b.kind == "backup" and b.has_manifest and not b.undone:
            ttk.Button(btns, text="撤销", style="Secondary.Small.TButton", cursor="hand2",
                       command=lambda: self.do_undo(b)).pack(side="left", padx=(0, t.px(6)))
        more = ttk.Button(btns, text="更多", style="Secondary.Small.TButton", cursor="hand2")
        more.configure(command=lambda: self._history_menu(more, b))
        more.pack(side="left")
        return row

    def _history_menu(self, widget, b: core.BackupInfo) -> None:
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="打开文件夹", command=lambda: self._open(b.backup_path))
        log = os.path.join(b.backup_path, core.LOG_NAME)
        if os.path.isfile(log):
            m.add_command(label="查看迁移日志", command=lambda: self._open(log))
        if b.kind == "backup":
            m.add_separator()
            m.add_command(label="整体还原到这个备份…（高级）", command=lambda: self.do_rollback(b))
        m.add_separator()
        m.add_command(label="删除这个文件夹…", command=lambda: self.do_delete(b))
        m.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())

    # ---------------------------------------------------------------- 帮助页
    def _build_help(self) -> None:
        t = self.theme
        page = tk.Frame(self.pages, bg=BG)
        self._pages["help"] = page
        logbox = tk.Frame(page, bg=BG)
        logbox.pack(side="bottom", fill="x", padx=t.px(24), pady=(0, t.px(14)))
        self.log_link = Link(logbox, t, "运行日志", self._toggle_log, bg=BG, fg=TEXT2, icon="right")
        self.log_link.pack(anchor="w", pady=(t.px(8), 0))
        self.log_card = card(logbox, t, 4)
        self.log_text = tk.Text(self.log_card, height=9, wrap="word", font=t.f_mono, bg=CARD, fg=TEXT2,
                                relief="flat", bd=0, highlightthickness=0, padx=t.px(10), pady=t.px(6),
                                state="disabled")
        vsb = ttk.Scrollbar(self.log_card, orient="vertical", command=self.log_text.yview,
                            style="Card.Vertical.TScrollbar")
        self.log_text.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

        wrap = card(page, t, 4)
        wrap.pack(fill="both", expand=True, padx=t.px(24), pady=(t.px(16), 0))
        self.help_text = tk.Text(wrap, wrap="char", font=t.f_body, bg=CARD, fg=TEXT2, relief="flat", bd=0,
                                 highlightthickness=0, padx=t.px(20), pady=t.px(10), cursor="arrow",
                                 spacing1=t.px(2), spacing3=t.px(2))
        hsb = ttk.Scrollbar(wrap, orient="vertical", command=self.help_text.yview, style="Card.Vertical.TScrollbar")
        self.help_text.configure(yscrollcommand=hsb.set)
        hsb.pack(side="right", fill="y")
        self.help_text.pack(side="left", fill="both", expand=True)
        tx = self.help_text
        tx.tag_configure("h", font=t.f_h, foreground=TEXT, spacing1=t.px(14), spacing3=t.px(4))
        tx.tag_configure("li", lmargin1=t.px(4), lmargin2=t.px(22))
        tx.tag_configure("code", font=t.f_mono, foreground=TEXT)
        tx.tag_configure("link", foreground=ACCENT, underline=True)

    def _toggle_log(self) -> None:
        if self.log_card.winfo_manager():
            self.log_card.pack_forget()
            self.log_link.set_icon("right")
        else:
            self.log_card.pack(fill="x", pady=(self.theme.px(6), 0))
            self.log_link.set_icon("down")
            self.log_text.see("end")

    def _append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _fill_help(self) -> None:
        key = tuple((b, os.path.isdir(b)) for b in self.bases) + (os.path.isdir(core.get_projects_dir()),)
        if key == self._help_key:  # 只有数据位置会变；没变就不重填（每次切到帮助页都会调用）
            return
        self._help_key = key
        tx = self.help_text
        tx.configure(state="normal")
        tx.delete("1.0", "end")
        n_links = 0

        def h(text: str) -> None:
            tx.insert("end", text + "\n", "h")

        def p(text: str) -> None:
            tx.insert("end", text + "\n")

        def li(*parts) -> None:
            nonlocal n_links
            tx.insert("end", "•  ", "li")
            for part in parts:
                if isinstance(part, tuple):  # (文字, 路径)：可点击打开
                    n_links += 1
                    tag = f"link{n_links}"
                    tx.insert("end", part[0], ("li", "link", tag))
                    tx.tag_bind(tag, "<Button-1>", lambda e, path=part[1]: self._open(path))
                    tx.tag_bind(tag, "<Enter>", lambda e: tx.configure(cursor="hand2"))
                    tx.tag_bind(tag, "<Leave>", lambda e: tx.configure(cursor="arrow"))
                elif part.startswith("`"):
                    tx.insert("end", part.strip("`"), ("li", "code"))
                else:
                    tx.insert("end", part, "li")
            tx.insert("end", "\n", "li")

        h("这个工具做什么")
        p("Claude 桌面端按账号分别保存侧栏里的对话列表。换了登录账号，或者在 claude.ai 登录和 API key 模式之间切换后，"
          "侧栏是空的——但对话本身还在这台电脑上。本工具把旧账号的对话列表搬到新账号，桌面端重新打开后旧对话就回来了，"
          "点开能接着聊。")
        h("怎么用")
        li("在 Claude 桌面端登录新账号，打开一次 Code 标签页（不用发消息），新账号的目录才会出现。")
        li("回到本工具：左边勾选要搬的旧账号，右边确认新账号（默认是最近登录的那个）。下方显示要搬多少个对话，"
           "可以点「查看明细」逐条看。")
        li("点「开始迁移」。桌面端开着的话，工具会先关闭它，备份、写入之后再重新打开。")
        li("在桌面端确认登录的是新账号、打开 Code 标签页，旧对话就在侧栏里。搬错了，到「记录」页撤销那次迁移。")
        h("会不会弄坏东西")
        li("只复制侧栏的会话记录（", "`local_*.json`", "）；对话内容（", "`.jsonl`", "）从不读写。")
        li("每次写入前先完整备份新账号的会话目录，并核对文件数和大小，对不上就停下、什么都不写。")
        li("新账号里已有的对话不会被旧版本覆盖；你在新账号里删掉的对话不会被搬回来。")
        li("撤销只移走那次写入、之后没再动过的文件，移到旁边的文件夹里，不删除。")
        li("只结束 Claude 桌面端本体；终端里运行的 Claude Code（进程名同样是 claude.exe）不受影响。")
        h("不会迁移的内容")
        li("定时任务和 backlog、Cowork 的数据、侧栏分组和排序（会话自带的收藏标记会随记录迁移）。")
        li("会话用到的 MCP 服务器或连接器：新账号没有同样的，会话照样能打开、接着聊，只是用不了那些工具。")
        h("常见问题")
        li("侧栏里没出现旧对话：多半是桌面端没彻底退出（托盘里还在）。点底部的「重启」，或从托盘图标退出后再打开；"
           "也确认登录的就是迁入的那个账号。")
        li("某个旧对话没搬过来：你在新账号里删过它，工具按「新账号里删过」跳过，这是有意的。")
        li("点开显示找不到对话：对应的 .jsonl 不在这台电脑上（被删或换了电脑）。本工具只搬列表，救不回原文。")
        li("只找到 1 个账号：新账号还没打开过 Code 标签页。登录新账号、打开一次 Code 标签页，再回来点「重新检测」。")
        h("数据位置")
        for b in self.bases:
            label = f"会话列表（{core.profile_label(b)}）："
            if os.path.isdir(b):
                li(label, (b, b))
            else:
                li(label, "`" + b + "`", "（不存在）")
        projects = core.get_projects_dir()
        li("对话内容：", (projects, projects) if os.path.isdir(projects) else "`" + projects + "`")
        li("备份和日志放在各账号的目录里（名字带 ", "`" + core.BACKUP_MARKER + "`",
           "）。里面有会话标题等内容，分享前请留意。")
        h("命令行")
        p("给脚本或 AI 用，默认只预览，加 --apply 才写入：")
        li("`python core.py --help`")
        tx.configure(state="disabled")

    # ---------------------------------------------------------------- 杂项
    def _open(self, path: str) -> None:
        try:
            if IS_WINDOWS:
                os.startfile(path)  # noqa: S606
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:  # noqa: BLE001
            self.notify("打开失败", [str(e)], tone="warn")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="gui.py", description="Claude 会话迁移（图形界面）")
    ap.add_argument("--base", action="append", default=[],
                    help="改用指定的 claude-code-sessions 目录（测试副本用，可重复）；此时不检测、不关闭 Claude 桌面端")
    args = ap.parse_args(argv)
    ui.enable_dpi_awareness()
    App(bases=args.base or None).mainloop()


if __name__ == "__main__":
    main()
