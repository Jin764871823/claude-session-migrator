"""ui_theme.py — 自写 Python + Tk 工具共用的界面主题（只一套外观，不做主题切换）。

版本见 VERSION。正本在 D:\\AI\\general\\ui-kit\\ui_theme.py；各工具复制一份进自己的项目（各自打包、公开仓库也能独立运行，
不跨项目 import），改动先改正本，再用 D:\\AI\\general\\ui-kit\\sync.py 同步到各副本。视觉和交互规范：
D:\\AI\\general\\专题\\自写软件开发规范.md 第 3、4 节（令牌与 PowerShell 一侧组件管理器的 lib\\Palette.xaml 同名同值）。

外观照 Claude会话迁移（用户 2026-10-15 定）：暖灰窗口底 + 白卡片 + 细描边，浅色大标题页头 + 下划线页签，
Windows 系统蓝主按钮（用户 10-15 夜：取会话迁移的暖白底，强调色换成 Windows 11 浅色主题的强调蓝）。只用标准库（tkinter + ctypes），圆角、勾选框用超采样小图片画（不依赖 Pillow）。用法：

    import ui_theme as ui
    ui.enable_dpi_awareness()          # 在创建 Tk 根窗口之前
    root = tk.Tk()
    theme = ui.Theme(root)             # 设好 ttk 样式、字体、缩放
    header = ui.Header(root, theme, "工具名", "一句话说明", tabs=[("main", "整理"), ("history", "记录"), ("help", "帮助")],
                       on_tab=...)
    ui.fit_to_work_area(root, *ui.WINDOW_SIZE, *ui.WINDOW_MIN_SIZE, scale=theme.scale)

版本：1（10-15）QQ 配色首版；2 小按钮按文字自然宽度、页头收紧；3 选择卡片 ChoiceCards、折叠分节 Disclosure；
4（10-15 夜）改名 qq_theme → ui_theme，整体换成会话迁移的暖白底和部件、Windows 蓝强调色（复选行、滚动列表、确认框、进行中提示条、
底栏、分段选择、明细子窗口），窗口尺寸按工具各定 + 默认尺寸上限。字体只调用系统字体（规范 3.2：不打包雅黑；换思源黑体只改 FONT_FAMILY）。
5（10-15 夜）color_titlebar 不再依赖调用时机：外框窗口还没建出来时等 <Map> 再染（原来会设到内层窗口上，返回成功却不生效）。确认框、明细子窗口也染；
  ChoiceCards 销毁时摘掉变量监听（重建卡片后旧监听不再改已销毁的控件）。
6（10-15 夜）底栏状态点改成直径 10 的抗锯齿圆点图片（Theme.dot；原来是 9 磅「●」字符，用户嫌太小，字符大小还随字体变）；
  ChoiceCards 按内容宽度排、多出的宽度平分（原来强制等宽，要最宽那张的 N 倍宽，英文界面放不下）。
"""

from __future__ import annotations

import platform
import re
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

VERSION = 7
IS_WINDOWS = platform.system() == "Windows"

# 窗口尺寸（规范 3.3，用户 10-15 夜）：各工具按自己的内容定默认（不最大化时的）尺寸和最小尺寸，传给 fit_to_work_area；
# 默认尺寸一律不超过 DEFAULT_WINDOW_MAX（只管第一次打开的大小，不限制手动拉大和最大化）。96 DPI 逻辑像素。
DEFAULT_WINDOW_MAX = (1280, 860)
WINDOW_SIZE = (1200, 780)        # 列表型工具（媒体整理、组件管理器）的参考值；内容少的工具用自己的（会话迁移 800×680）
WINDOW_MIN_SIZE = (960, 620)
STATUS_DOT = 10                  # 底栏状态圆点直径（逻辑像素；规范 3.3，WPF 一侧的 Ellipse 同值）

# ---------------------------------------------------------------- 令牌（规范 3.1，与 Palette.xaml 同名同值）
TOKENS = {
    "Bg": "#F5F5F4",
    "Surface": "#FFFFFF",
    "SurfaceAlt": "#FAFAF9",
    "CardBorder": "#E7E5E4",
    "Divider": "#F0EFED",
    "Track": "#F0EFED",
    "Text1": "#1C1917",
    "Text2": "#57534E",
    "Text3": "#78716C",
    "TextDisabled": "#A8A29E",
    "Accent": "#005FB8",
    "AccentHover": "#1975C5",
    "AccentPressed": "#3183CA",
    "AccentText": "#005FB8",
    "AccentTint": "#EBF3FB",
    "AccentTintBorder": "#B9D5F0",
    "AccentDisabled": "#E7E5E4",
    "TextOnDisabled": "#A8A29E",
    "Success": "#15803D",
    "SuccessTint": "#E8F6EC",
    "SuccessBorder": "#BFE3CB",
    "Warning": "#B45309",
    "WarningTint": "#FEF5E6",
    "WarningBorder": "#F5D7A6",
    "Danger": "#B91C1C",
    "DangerHover": "#991B1B",
    "DangerPressed": "#7F1D1D",
    "DangerTint": "#FCEBEB",
    "DangerBorder": "#F2C4C4",
    "Neutral": "#57534E",
    "NeutralTint": "#F0EFED",
    "Control": "#FFFFFF",
    "ControlHover": "#F5F5F4",
    "ControlPressed": "#E7E5E4",
    "ControlDisabled": "#FAFAF9",
    "ControlBorder": "#D6D3D1",
    "ControlBorderHover": "#A8A29E",
    "RowHover": "#FAFAF9",
    "RowSelected": "#F0EFED",
    "AttentionRow": "#FFFFFF",
    "ScrollThumb": "#D6D3D1",
    "ScrollThumbHover": "#A8A29E",
    "TooltipBg": "#292524",
    "TooltipText": "#F5F5F4",
}
# 版本 3 以前的页头令牌：页头不再用色条（和窗口底同色），留着别名免得旧代码 KeyError
TOKENS.update(HeaderBg=TOKENS["Bg"], HeaderText=TOKENS["Text1"], HeaderTextDim=TOKENS["Text3"],
              HeaderTrack=TOKENS["Bg"], HeaderIcon=TOKENS["AccentTint"], NavSelected=TOKENS["Surface"])
T = TOKENS

# 胶囊标签 / 提示条的色调：(浅底, 文字, 描边)。ok / warn / gray 是会话迁移里的叫法
TONES = {
    "accent": (T["AccentTint"], T["Accent"], T["AccentTintBorder"]),
    "success": (T["SuccessTint"], T["Success"], T["SuccessBorder"]),
    "warning": (T["WarningTint"], T["Warning"], T["WarningBorder"]),
    "danger": (T["DangerTint"], T["Danger"], T["DangerBorder"]),
    "neutral": (T["NeutralTint"], T["Text2"], T["CardBorder"]),
}
TONES.update(ok=TONES["success"], warn=TONES["warning"], gray=TONES["neutral"])

# Segoe Fluent Icons / Segoe MDL2 Assets 的字形（两者码位相同；都没有时不显示图标）。规范：不用 emoji 当图标。
ICONS = {
    "arrow": "\uE72A", "refresh": "\uE72C", "down": "\uE70D", "up": "\uE70E", "right": "\uE76C", "done": "\uE930",
    "warn": "\uE7BA", "info": "\uE946", "chat": "\uE8BD", "folder": "\uE838", "undo": "\uE7A7", "history": "\uE81C",
    "settings": "\uE713", "help": "\uE897", "play": "\uE768", "stop": "\uE71A", "photo": "\uEB9F", "video": "\uE714",
    "search": "\uE721", "clear": "\uE894", "list": "\uEA37", "open": "\uE8E5", "phone": "\uE8EA", "sync": "\uE895",
    "time": "\uE823", "filter": "\uE71C", "error": "\uEA39", "check": "\uE73E", "doc": "\uE8A5",
}

# 字体：只调用系统字体做屏幕显示（规范 3.2，不打包、不分发）。以后改用打包的思源黑体只改这里。
FONT_FAMILY = "Microsoft YaHei UI" if IS_WINDOWS else "TkDefaultFont"
NUMBER_FAMILY = FONT_FAMILY
MONO_FAMILY = "Consolas" if IS_WINDOWS else "TkFixedFont"


# ---------------------------------------------------------------- 系统集成
def enable_dpi_awareness() -> None:
    """创建 Tk 根窗口之前调用：System aware（Tk 8.6 不处理 WM_DPICHANGED，每显示器感知反而错位；规范第 2 节）。"""
    if not IS_WINDOWS:
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
        except (OSError, AttributeError):
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001 - 设不上就按系统默认（位图拉伸）显示
        return


def _colorref(color: str) -> int:
    r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
    return r | (g << 8) | (b << 16)


def color_titlebar(window: tk.Misc, caption: str | None = None, text: str | None = None, border: str | None = None) -> bool:
    """把系统标题栏染色（DwmSetWindowAttribute 35 / 36 / 34，Windows 11 22000 起）：默认染成 Windows 蓝（Accent）、白字，
    边框同色（规范 3.1，用户 10-15 夜看过暖白和蓝两版后选蓝）。

    什么时候调都行，建好窗口后调一次即可：标题栏属于 Tk 在窗口第一次映射时才建出来的外框窗口（``wm frame``，
    没建出来时它返回窗口自己的句柄），所以这里登记颜色并绑定顶层窗口的 ``<Map>``——外框已在就马上染，否则等映射时染，
    以后每次重新映射也再染一次。返回这次是否已经染上（还没映射、Windows 10 或失败时为 False）。"""
    if not IS_WINDOWS:
        return False
    toplevel = window.winfo_toplevel()
    first = not hasattr(toplevel, "_ui_titlebar_colors")
    toplevel._ui_titlebar_colors = (caption or T["Accent"], text or "#FFFFFF", border or caption or T["Accent"])
    if first:
        toplevel.bind("<Map>", lambda event: _apply_titlebar(toplevel) if event.widget is toplevel else None, add="+")
    return _apply_titlebar(toplevel)


def _apply_titlebar(toplevel: tk.Misc) -> bool:
    """外框窗口已在时，把登记的颜色设到它上面；外框还没建出来就返回 False（不往窗口自己的句柄上设：DWM 也会返回成功，但不生效）。"""
    try:
        import ctypes

        frame = int(toplevel.wm_frame(), 16)
        if frame == toplevel.winfo_id():
            return False
        ok = True
        for attribute, color in zip((35, 36, 34), toplevel._ui_titlebar_colors):
            value = ctypes.c_uint(_colorref(color))
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(frame, attribute, ctypes.byref(value), ctypes.sizeof(value))
            ok = ok and result == 0
        return ok
    except (tk.TclError, ValueError, OSError):
        return False


def work_area(window: tk.Misc) -> tuple[int, int, int, int]:
    """主显示器工作区（去掉任务栏）的 (left, top, right, bottom)，物理像素；取不到时用整个屏幕。"""
    if IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes

            rect = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
                return rect.left, rect.top, rect.right, rect.bottom
        except Exception:  # noqa: BLE001
            pass
    return 0, 0, window.winfo_screenwidth(), window.winfo_screenheight()


def fit_to_work_area(window: tk.Misc, width: int = WINDOW_SIZE[0], height: int = WINDOW_SIZE[1],
                     min_width: int | None = WINDOW_MIN_SIZE[0], min_height: int | None = WINDOW_MIN_SIZE[1],
                     scale: float = 1.0) -> None:
    """按设计尺寸（96 DPI 逻辑像素）开窗，并保证整个窗口落在工作区内（规范 3.3：任务栏不能挡住底栏）。
    设计尺寸先夹到 DEFAULT_WINDOW_MAX 以内。标题栏和边框按 40 / 16 像素预留。"""
    left, top, right, bottom = work_area(window)
    avail_w, avail_h = right - left - 16, bottom - top - 40
    width, height = min(width, DEFAULT_WINDOW_MAX[0]), min(height, DEFAULT_WINDOW_MAX[1])
    w = min(int(width * scale), avail_w)
    h = min(int(height * scale), avail_h)
    if min_width or min_height:
        window.minsize(min(int((min_width or 0) * scale), avail_w), min(int((min_height or 0) * scale), avail_h))
    x = left + max(0, (right - left - w) // 2)
    y = top + max(0, (bottom - top - h - 40) // 3)
    window.geometry(f"{w}x{h}+{x}+{y}")


# ---------------------------------------------------------------- 断行（避头尾）
# Tk 的 wraplength 在中文里每个字后面都能断，会把「。」「）」单独挤到下一行开头（会话迁移「记录」页在 125% 缩放下出现过，
# 2026-10-16）。多行文字一律用 wrap_text 自己断行：行首不放收尾标点，行尾不放开头括号（中文排版的避头尾规则）；
# 英文单词、数字、路径（连续的可见 ASCII 字符）连成一段不拆开，只有一段本身比一行还宽时才按字拆。
NO_LINE_START = frozenset("，。、；：？！）］｝】」』》〉〕’”…‥—～·%,.;:?!)]}")
NO_LINE_END = frozenset("（［｛【「『《〈〔‘“([{")
_TOKENS = re.compile(r"[!-~]+|\s+|.")      # 一段可见 ASCII、一段空白，或单个其他字符（汉字、全角标点）


def wrap_text(font, text: str, width: int, cache: dict | None = None) -> str:
    """把 text 按 width 像素断成多行（插入换行符），遵守避头尾；text 里原有的换行保留。
    font 要有 measure()（tkfont.Font）。反复给同一段文字断行时（窗口拖宽拖窄）传同一个 cache，量过的宽度不再量。"""
    widths = cache if cache is not None else {}

    def measure(s: str) -> int:
        value = widths.get(s)
        if value is None:
            value = widths[s] = font.measure(s)
        return value

    out: list[str] = []
    for para in text.split("\n"):
        tokens: list[str] = []
        for tok in _TOKENS.findall(para):
            if len(tok) > 1 and not tok.isspace() and measure(tok) > width:
                tokens.extend(tok)                  # 比一行还宽的一段只好按字拆
            else:
                tokens.append(tok)
        line: list[str] = []
        line_w = 0
        for tok in tokens:
            if not line and tok.isspace():
                continue                            # 行首不留空白
            tok_w = measure(tok)
            if line and not tok.isspace() and line_w + tok_w > width:
                while line and line[-1].isspace():
                    line.pop()
                carry: list[str] = []
                # 下一行开头是收尾标点，或本行结尾是开头括号：把本行最后一段挪到下一行（整行只剩一段时不挪）
                while len(line) > 1 and ((carry[0] if carry else tok)[0] in NO_LINE_START or line[-1][-1] in NO_LINE_END):
                    carry.insert(0, line.pop())
                out.append("".join(line).rstrip())
                line = carry
                line_w = sum(measure(t) for t in line)
            line.append(tok)
            line_w += tok_w
        out.append("".join(line).rstrip())
    return "\n".join(out)


# ---------------------------------------------------------------- 抗锯齿小图（圆角、勾、圆点）
def _rgb(c: str) -> tuple[int, int, int]:
    return int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)


def _rrect(x0: float, y0: float, x1: float, y1: float, r: float):
    """圆角矩形的点包含测试。"""
    def inside(x: float, y: float) -> bool:
        if x < x0 or x > x1 or y < y0 or y > y1:
            return False
        dx = max(x0 + r - x, 0.0, x - (x1 - r))
        dy = max(y0 + r - y, 0.0, y - (y1 - r))
        return dx * dx + dy * dy <= r * r
    return inside


def _circle(cx: float, cy: float, r: float):
    def inside(x: float, y: float) -> bool:
        return (x - cx) ** 2 + (y - cy) ** 2 <= r * r
    return inside


def _stroke(points: list[tuple[float, float]], width: float):
    """圆头折线的点包含测试。"""
    half2 = (width / 2) ** 2
    segs = list(zip(points, points[1:]))

    def inside(x: float, y: float) -> bool:
        for (ax, ay), (bx, by) in segs:
            vx, vy = bx - ax, by - ay
            t = max(0.0, min(1.0, ((x - ax) * vx + (y - ay) * vy) / (vx * vx + vy * vy)))
            dx, dy = x - ax - t * vx, y - ay - t * vy
            if dx * dx + dy * dy <= half2:
                return True
        return False
    return inside


def raster(master, w: int, h: int, bg: str, layers, ss: int = 4) -> tk.PhotoImage:
    """按层（包含测试, 颜色）画一张小图：4×4 超采样，和底色混合出抗锯齿边缘。只在开窗时画，画好缓存。"""
    base = _rgb(bg)
    lay = [(test, _rgb(c)) for test, c in layers]
    offs = [(i + 0.5) / ss for i in range(ss)]
    n = ss * ss
    rows = []
    for py in range(h):
        row = []
        for px in range(w):
            r = g = b = 0
            for oy in offs:
                for ox in offs:
                    c = base
                    for test, col in lay:
                        if test(px + ox, py + oy):
                            c = col
                    r += c[0]
                    g += c[1]
                    b += c[2]
            row.append("#%02x%02x%02x" % (round(r / n), round(g / n), round(b / n)))
        rows.append("{" + " ".join(row) + "}")
    img = tk.PhotoImage(master=master, width=w, height=h)
    img.put(" ".join(rows))
    return img


# ---------------------------------------------------------------- 主题
class Theme:
    """字体、缩放和 ttk 样式（clam 打底 + 图片元素做出圆角按钮、卡片、勾选框）。

    ttk 样式名：按钮 ``Primary.TButton``（主按钮，一屏一个；``Accent.TButton`` 是同一个）、``Secondary.TButton``、
    ``Danger.TButton``，各有 ``.Small`` 版；卡片 ``Card.TFrame``；页面底 ``Page.TFrame``；卡片里的底 ``Surface.TFrame``；
    进度条 ``Accent.Horizontal.TProgressbar``；滚动条 ``Card.Vertical.TScrollbar`` / ``Page.Vertical.TScrollbar``（及 Horizontal）；
    列表 ``Treeview``（表头窗口底色，放在页面上）/ ``Card.Treeview``（表头白底，放在卡片里）；输入 ``TEntry`` / ``TCombobox`` /
    ``TSpinbox``；``TCheckbutton`` / ``TRadiobutton``（页面底上）和 ``Card.TCheckbutton`` / ``Card.TRadiobutton``（卡片上）；
    分段选择 ``Segment.TRadiobutton``；文字 ``TLabel``、``Card.TLabel``、``Secondary.TLabel``、``Card.Secondary.TLabel``、
    ``Section.TLabel``、``Card.Section.TLabel``、``Strong.TLabel``、``Card.Strong.TLabel``。
    自画复选框图片：``check_img[(勾选?, 可用?)]``、``check_hover``；步骤序号圆底 ``step_img``。
    """

    def __init__(self, root: tk.Misc) -> None:
        self.root = root
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        families = set(tkfont.families(root))
        self.icon_family = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in families), None)

        def font(size: float, weight: str = "normal", family: str = FONT_FAMILY) -> tkfont.Font:
            return tkfont.Font(root, family=family, size=int(round(size)), weight=weight)

        # 字号（规范 3.2，磅）：正文 10、次要 9、分节 11 粗、页头工具名 15 粗、结论大数字 20 粗
        self.f_body = font(10)
        self.f_bold = font(10, "bold")
        self.f_small = font(9)
        self.f_h = font(11, "bold")
        self.f_title = font(15, "bold")
        self.f_big = font(20, "bold")
        self.f_number = self.f_big
        self.f_mono = font(9, family=MONO_FAMILY)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkTooltipFont"):
            try:
                tkfont.nametofont(name).configure(family=FONT_FAMILY, size=10)
            except tk.TclError:
                continue
        try:
            tkfont.nametofont("TkFixedFont").configure(family=MONO_FAMILY, size=9)
        except tk.TclError:
            pass
        self._icon_fonts: dict[int, tkfont.Font] = {}
        self._underlined: dict[str, tkfont.Font] = {}
        self.images: list[tk.PhotoImage] = []   # 保持引用，否则图片会被回收
        self._dots: dict[tuple[str, str], tk.PhotoImage] = {}
        self.slices: list[tk.PhotoImage] = []   # 给 ttk 图片元素拉伸用的九宫格大图（见 _grow）
        self._init_styles()

    # ---- 工具
    def px(self, n: float) -> int:
        return int(round(n * self.scale))

    def icon(self, name: str) -> str:
        return ICONS[name] if self.icon_family else ""

    def icon_font(self, size: int = 10) -> tkfont.Font:
        if size not in self._icon_fonts:
            self._icon_fonts[size] = tkfont.Font(self.root, family=self.icon_family or "TkDefaultFont", size=size)
        return self._icon_fonts[size]

    def underlined(self, f: tkfont.Font) -> tkfont.Font:
        key = str(f)
        if key not in self._underlined:
            u = tkfont.Font(self.root, **f.actual())
            u.configure(underline=True)
            self._underlined[key] = u
        return self._underlined[key]

    # ---- 图片元素
    def dot(self, color: str, bg: str = T["Surface"]) -> tk.PhotoImage:
        """状态圆点（底栏左边）：直径 STATUS_DOT 逻辑像素的实心圆，按（颜色, 底色）画一次缓存。"""
        key = (color, bg)
        if key not in self._dots:
            d = self.px(STATUS_DOT)
            self._dots[key] = raster(self.root, d, d, bg, [(_circle(d / 2, d / 2, d / 2), color)])
        return self._dots[key]

    def _box(self, size: int, r: float, fill: str, bg: str, border: str | None = None, extra=(), bw: int | None = None) -> tk.PhotoImage:
        bw = bw or max(1, round(self.scale))
        layers = [(_rrect(0, 0, size, size, r), border or fill)]
        if border:
            layers.append((_rrect(bw, bw, size - bw, size - bw, max(0.0, r - bw)), fill))
        img = raster(self.root, size, size, bg, layers + list(extra))
        self.images.append(img)
        return img

    def _grow(self, small: tk.PhotoImage, b: int, inner: int = 240) -> tk.PhotoImage:
        """把九宫格小图的中间行列铺成大图（四角不变）。ttk 拉伸图片元素时把中间区域一块块复制贴满：
        中间只有几像素时大卡片每次重绘要贴上万块，界面会卡（Claude会话迁移 2026-10-12 的卡顿根因）。"""
        w = small.width()
        big_w = 2 * b + inner
        big = tk.PhotoImage(master=self.root, width=big_w, height=big_w)
        spans = ((0, b, 0, b), (b, w - b, b, b + inner), (w - b, w, b + inner, big_w))
        for sx0, sx1, dx0, dx1 in spans:
            for sy0, sy1, dy0, dy1 in spans:  # 目标比源大时 Tk 平铺源区域（只在开窗时做一次）
                self.root.tk.call(big, "copy", small, "-from", sx0, sy0, sx1, sy1, "-to", dx0, dy0, dx1, dy1)
        self.images.append(big)
        self.slices.append(big)
        return big

    def _slice(self, r: float, fill: str, bg: str, border: str | None = None, bw: int | None = None) -> tk.PhotoImage:
        size = 2 * int(r) + 4
        return self._grow(self._box(size, r, fill, bg, border, bw=bw), int(r) + 1)

    def _button(self, names: tuple[str, ...], fill: str, hover: str, press: str, fg: str,
                border: str | None = None, hover_border: str | None = None) -> None:
        """按钮（会话迁移的尺寸）：大按钮粗体、内边距 22×9、最少 8 个字符宽；小按钮常规、14×5、按文字自然宽度。"""
        r = self.px(6)
        bg = T["Surface"]
        normal = self._slice(r, fill, bg, border)
        active = self._slice(r, hover, bg, hover_border or border)
        pressed = self._slice(r, press, bg, hover_border or border)
        disabled = self._slice(r, T["Surface"] if border else T["AccentDisabled"], bg, border)
        m = 2 * r + 4  # 最小尺寸按小图算，否则按钮会被大图撑大
        element = f"{names[0]}.border"
        self.style.element_create(element, "image", normal, ("disabled", disabled),
                                  ("pressed", pressed), ("active", active), border=r + 1, padding=0,
                                  width=m, height=m, sticky="nsew")
        for name in names:
            for suffix, pad, f, width in (("", (22, 9), self.f_bold, -8), (".Small", (14, 5), self.f_body, 0)):
                st = f"{name}{suffix}.TButton"
                self.style.layout(st, [(element, {"sticky": "nsew", "children": [
                    ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})])
                self.style.configure(st, foreground=fg, font=f, anchor="center", width=width,
                                     padding=(self.px(pad[0]), self.px(pad[1])), focuscolor=fill)
                self.style.map(st, foreground=[("disabled", T["TextOnDisabled"])])

    def _indicators(self, bg: str) -> tuple[dict, dict]:
        """ttk 勾选框和单选圆点的图片：(勾选?, 可用?) → 图。图片右侧带 6 像素空白，指示器和文字之间留出间距。"""
        s = self.px(16)
        gap = self.px(6)
        rr = self.px(4)
        bw = max(1, round(self.scale))
        tick = _stroke([(s * 0.26, s * 0.52), (s * 0.44, s * 0.70), (s * 0.76, s * 0.33)], s * 0.12)

        def square(fill: str, border: str | None, extra=()):
            layers = [(_rrect(0, 0, s, s, rr), border or fill)]
            if border:
                layers.append((_rrect(bw, bw, s - bw, s - bw, max(0.0, rr - bw)), fill))
            img = raster(self.root, s + gap, s, bg, layers + list(extra))
            self.images.append(img)
            return img

        check = {
            (False, True): square(T["Control"], T["ControlBorderHover"]),
            (True, True): square(T["Accent"], None, [(tick, "#FFFFFF")]),
            (False, False): square(T["ControlDisabled"], T["CardBorder"]),
            (True, False): square(T["ControlBorder"], None, [(tick, "#FFFFFF")]),
        }
        c = s / 2
        radio = {}
        for (on, enabled), (ring, fill, dot) in {
            (False, True): (T["ControlBorderHover"], T["Control"], None),
            (True, True): (T["Accent"], T["Control"], T["Accent"]),
            (False, False): (T["CardBorder"], T["ControlDisabled"], None),
            (True, False): (T["ControlBorder"], T["ControlDisabled"], T["ControlBorder"]),
        }.items():
            layers = [(_circle(c, c, c), ring), (_circle(c, c, c - bw), fill)]
            if dot:
                layers.append((_circle(c, c, s * 0.24), dot))
            img = raster(self.root, s + gap, s, bg, layers)
            self.images.append(img)
            radio[(on, enabled)] = img
        return check, radio

    def _init_styles(self) -> None:
        st = self.style = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=T["Bg"], foreground=T["Text1"], font=self.f_body,
                     bordercolor=T["CardBorder"], focuscolor=T["Bg"], troughcolor=T["Track"],
                     selectbackground=T["AccentTint"], selectforeground=T["Text1"])
        # clam 默认给所有样式加了「disabled → 灰底、active → 浅灰底」的背景映射：禁用的卡片里控件（复选框、容器）会露出
        # 灰块（10-15 夜用户截图：档位按钮之间的灰条）。背景只由各样式自己定，禁用只改文字色。
        st.map(".", background=[], foreground=[("disabled", T["TextDisabled"])])
        # 按钮
        self._button(("Primary", "Accent"), T["Accent"], T["AccentHover"], T["AccentPressed"], "#FFFFFF")
        self._button(("Secondary",), T["Control"], T["ControlHover"], T["ControlPressed"], T["Text1"],
                     border=T["ControlBorder"], hover_border=T["ControlBorderHover"])
        self._button(("Danger",), T["Danger"], T["DangerHover"], T["DangerPressed"], "#FFFFFF")
        st.configure("TButton", padding=(self.px(14), self.px(5)))
        # 卡片：白底、10 圆角、1 像素描边，放在暖灰窗口底上（不用阴影，规范 3.3）
        r = self.px(10)
        st.element_create("Card.border", "image", self._slice(r, T["Surface"], T["Bg"], T["CardBorder"]),
                          border=r + 1, width=2 * r + 4, height=2 * r + 4, sticky="nsew")
        st.layout("Card.TFrame", [("Card.border", {"sticky": "nsew"})])
        st.configure("Page.TFrame", background=T["Bg"])
        st.configure("Surface.TFrame", background=T["Surface"])
        # 选择卡片（ChoiceCards）和分段选择（Segmented）：未选白底灰框，悬停 / 聚焦框加深，选中浅蓝底 + 蓝框
        thick = max(2, round(2 * self.scale))
        rc = self.px(8)
        choice = {
            "normal": self._slice(rc, T["Control"], T["Surface"], T["ControlBorder"]),
            "hover": self._slice(rc, T["Control"], T["Surface"], T["ControlBorderHover"]),
            "selected": self._slice(rc, T["AccentTint"], T["Surface"], T["Accent"], bw=thick),
            "disabled": self._slice(rc, T["ControlDisabled"], T["Surface"], T["CardBorder"]),
            "selected_disabled": self._slice(rc, T["ControlDisabled"], T["Surface"], T["ControlBorder"], bw=thick),
        }
        st.element_create("Choice.border", "image", choice["normal"],
                          ("disabled", "selected", choice["selected_disabled"]), ("disabled", choice["disabled"]),
                          ("selected", choice["selected"]), ("focus", choice["hover"]), ("hover", choice["hover"]),
                          border=rc + 1, padding=0, width=2 * rc + 4, height=2 * rc + 4, sticky="nsew")
        st.layout("Choice.TFrame", [("Choice.border", {"sticky": "nsew", "children": [("Frame.padding", {"sticky": "nsew"})]})])
        for name, f, fg, fg_selected in (("Choice.TLabel", self.f_bold, T["Text1"], T["Accent"]),
                                         ("Choice.Secondary.TLabel", self.f_small, T["Text3"], T["Text2"])):
            st.configure(name, background=T["Control"], foreground=fg, font=f)
            st.map(name, background=[("disabled", T["ControlDisabled"]), ("selected", T["AccentTint"])],
                   foreground=[("disabled", T["TextDisabled"]), ("selected", fg_selected)])
        st.configure("Choice.Badge.TLabel", background=T["AccentTint"], foreground=T["Accent"], font=self.f_small,
                     padding=(self.px(6), 0))
        st.map("Choice.Badge.TLabel", background=[("selected", T["Surface"]), ("disabled", T["NeutralTint"])],
               foreground=[("disabled", T["TextDisabled"])])
        rs = self.px(6)
        segment = {
            "normal": self._slice(rs, T["Control"], T["Surface"], T["ControlBorder"]),
            "hover": self._slice(rs, T["ControlHover"], T["Surface"], T["ControlBorderHover"]),
            "selected": self._slice(rs, T["AccentTint"], T["Surface"], T["Accent"]),
            "disabled": self._slice(rs, T["ControlDisabled"], T["Surface"], T["CardBorder"]),
            "selected_disabled": self._slice(rs, T["ControlDisabled"], T["Surface"], T["ControlBorder"]),
        }
        st.element_create("Segment.border", "image", segment["normal"],
                          ("disabled", "selected", segment["selected_disabled"]), ("disabled", segment["disabled"]),
                          ("selected", segment["selected"]), ("active", segment["hover"]),
                          border=rs + 1, padding=0, width=2 * rs + 4, height=2 * rs + 4, sticky="nsew")
        st.layout("Segment.TRadiobutton", [("Segment.border", {"sticky": "nsew", "children": [
            ("Radiobutton.padding", {"sticky": "nsew", "children": [("Radiobutton.label", {"sticky": "nsew"})]})]})])
        st.configure("Segment.TRadiobutton", background=T["Surface"], foreground=T["Text1"], font=self.f_body,
                     padding=(self.px(14), self.px(5)), anchor="center", focuscolor=T["Surface"])
        # 选中只变色和边框、不变粗：粗体更宽，会把旁边的控件挤开（规范 4「界面稳定」）
        st.map("Segment.TRadiobutton", foreground=[("disabled", T["TextDisabled"]), ("selected", T["Accent"])],
               background=[("active", T["Surface"])])
        # 文字
        for prefix, bg in (("", T["Bg"]), ("Card.", T["Surface"])):
            st.configure(f"{prefix}TLabel", background=bg, foreground=T["Text1"], font=self.f_body)
            st.configure(f"{prefix}Secondary.TLabel", background=bg, foreground=T["Text3"], font=self.f_small)
            st.configure(f"{prefix}Section.TLabel", background=bg, foreground=T["Text1"], font=self.f_h)
            st.configure(f"{prefix}Strong.TLabel", background=bg, foreground=T["Text1"], font=self.f_bold)
            st.configure(f"{prefix}TLabelframe", background=bg)
        # ttk 勾选框 / 单选框：统一的图片指示器（页面底和卡片上各一套，底色不同）
        for prefix, bg in (("", T["Bg"]), ("Card.", T["Surface"])):
            check, radio = self._indicators(bg)
            for kind, imgs in (("Checkbutton", check), ("Radiobutton", radio)):
                element = f"{prefix}{kind}.uiindicator"
                st.element_create(element, "image", imgs[(False, True)],
                                  ("selected", "disabled", imgs[(True, False)]), ("disabled", imgs[(False, False)]),
                                  ("selected", imgs[(True, True)]), sticky="w")
                name = f"{prefix}T{kind}"
                st.layout(name, [("Checkbutton.padding", {"sticky": "nswe", "children": [
                    (element, {"side": "left", "sticky": ""}),
                    ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [("Checkbutton.label", {"sticky": "nswe"})]})]})])
                st.configure(name, background=bg, foreground=T["Text1"], font=self.f_body, padding=(0, self.px(2)),
                             focuscolor=bg)
                st.map(name, background=[("active", bg)], foreground=[("disabled", T["TextDisabled"])])
        # 会话迁移的自画复选框（CheckBox / CheckRow 用，画在白卡片上）：(勾选, 可用) → 图
        s = self.px(18)
        rr = self.px(4)
        tick = _stroke([(s * 0.27, s * 0.52), (s * 0.44, s * 0.68), (s * 0.74, s * 0.34)], s * 0.12)
        self.check_img = {
            (False, True): self._box(s, rr, T["Surface"], T["Surface"], T["ControlBorderHover"]),
            (True, True): self._box(s, rr, T["Accent"], T["Surface"], extra=[(tick, "#FFFFFF")]),
            (False, False): self._box(s, rr, T["SurfaceAlt"], T["Surface"], T["CardBorder"]),
            (True, False): self._box(s, rr, T["ControlBorder"], T["Surface"], extra=[(tick, "#FFFFFF")]),
        }
        self.check_hover = self._box(s, rr, T["Surface"], T["Surface"], T["Text2"])
        d = self.px(22)
        self.step_img = self._box(d, d / 2, T["AccentTint"], T["Surface"])   # 步骤序号的圆底
        # 输入控件
        field = dict(fieldbackground=T["Control"], background=T["Control"], foreground=T["Text1"],
                     bordercolor=T["ControlBorder"], lightcolor=T["Control"], darkcolor=T["Control"],
                     arrowcolor=T["Text2"], insertcolor=T["Text1"], padding=(self.px(6), self.px(3)))
        for name in ("TEntry", "TCombobox", "TSpinbox"):
            st.configure(name, **field)
            st.map(name, bordercolor=[("focus", T["Accent"]), ("hover", T["ControlBorderHover"])],
                   lightcolor=[("focus", T["Accent"])], darkcolor=[("focus", T["Accent"])],
                   fieldbackground=[("disabled", T["ControlDisabled"]), ("readonly", T["Control"])],
                   foreground=[("disabled", T["TextDisabled"])],
                   selectbackground=[("focus", T["AccentTint"])], selectforeground=[("focus", T["Text1"])])
        self.root.option_add("*TCombobox*Listbox.background", T["Control"])
        self.root.option_add("*TCombobox*Listbox.foreground", T["Text1"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", T["AccentTint"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", T["Text1"])
        self.root.option_add("*TCombobox*Listbox.font", self.f_body)
        # 进度条、滚动条
        st.configure("Accent.Horizontal.TProgressbar", troughcolor=T["Track"], background=T["Accent"],
                     bordercolor=T["Track"], lightcolor=T["Accent"], darkcolor=T["Accent"], thickness=self.px(4))
        for name, trough in (("Card", T["Surface"]), ("Page", T["Bg"])):
            for orient in ("Vertical", "Horizontal"):
                st.configure(f"{name}.{orient}.TScrollbar", troughcolor=trough, background=T["ScrollThumb"], bordercolor=trough,
                             lightcolor=T["ScrollThumb"], darkcolor=T["ScrollThumb"], arrowcolor=T["Text3"], gripcount=0,
                             arrowsize=self.px(12))
                st.map(f"{name}.{orient}.TScrollbar", background=[("active", T["ScrollThumbHover"])])
        # 列表：白底、选中行浅灰底、表头小一号次要色；Treeview 表头用窗口底色（放在页面上），Card.Treeview 表头白底（放在卡片里）
        for name, head_bg in (("Treeview", T["Bg"]), ("Card.Treeview", T["Surface"])):
            st.configure(name, background=T["Surface"], fieldbackground=T["Surface"], foreground=T["Text1"],
                         bordercolor=T["Surface"], borderwidth=0, rowheight=self.px(30), font=self.f_body)
            st.map(name, background=[("selected", T["RowSelected"])], foreground=[("selected", T["Text1"])])
            st.configure(f"{name}.Heading", background=head_bg, foreground=T["Text2"], font=self.f_small, relief="flat",
                         bordercolor=T["CardBorder"], lightcolor=head_bg, darkcolor=T["CardBorder"],
                         padding=(self.px(8), self.px(6)))
            st.map(f"{name}.Heading", background=[("active", T["Divider"])])
        st.configure("TPanedwindow", background=T["Bg"])
        st.configure("Sash", sashthickness=self.px(6), background=T["Bg"], bordercolor=T["Bg"], lightcolor=T["Bg"], darkcolor=T["Bg"])

    def tree_tags(self, tree: ttk.Treeview) -> None:
        """列表行的标准标签：attention（要处理：琥珀字，不铺底色——10-15 夜用户嫌黄色太多）、failed（失败：红字）、muted（次要：灰字）。"""
        tree.tag_configure("attention", foreground=T["Warning"])
        tree.tag_configure("failed", foreground=T["Danger"])
        tree.tag_configure("muted", foreground=T["Text3"])


# ---------------------------------------------------------------- 小部件
def card(master, theme: Theme, padding: int = 16) -> ttk.Frame:
    return ttk.Frame(master, style="Card.TFrame", padding=theme.px(padding))


def badge(master, theme: Theme, text: str, tone: str = "gray", bg: str | None = None) -> tk.Label:
    """小方角状态标签（会话迁移的「最近登录」「可撤销」）。tone：accent / success(ok) / warning(warn) / danger / neutral(gray)。"""
    tint, fg, _border = TONES[tone]
    return tk.Label(master, text=text, font=theme.f_small, fg=fg, bg=tint, padx=theme.px(6), pady=0)


class Badge(tk.Label):
    """可改文字和色调的状态标签。规范 3.4：只有「要处理」「失败」用实心（``solid=True``），其余用浅底。"""

    def __init__(self, master, theme: Theme, text: str, tone: str = "neutral", *, solid: bool = False, bg: str | None = None) -> None:
        super().__init__(master, text=text, font=theme.f_small, padx=theme.px(6), pady=0)
        self.theme = theme
        self.set(text, tone, solid)

    def set(self, text: str, tone: str = "neutral", solid: bool = False) -> None:
        tint, fg, _border = TONES[tone]
        if solid:
            tint, fg = fg, "#FFFFFF"
        self.configure(text=text, fg=fg, bg=tint)


class Header(tk.Frame):
    """页头（会话迁移的样子）：和窗口底同色；左边工具名（15 磅粗）+ 一句话副标题，右边文字页签，选中的黑字 +
    下面 2 像素蓝色下划线；最下面一条 1 像素分隔线。``tabs``：[(键, 文字)]；点页签调 ``on_tab(键)``。"""

    def __init__(self, master, theme: Theme, title: str, subtitle: str = "", *, tabs=(), on_tab=None,
                 icon: str | None = None, padx: int = 24) -> None:
        super().__init__(master, bg=T["Bg"])
        t = theme
        self.theme = theme
        self.on_tab = on_tab
        self.current: str | None = None
        self._tabs: dict[str, tuple[tk.Label, tk.Frame]] = {}
        head = tk.Frame(self, bg=T["Bg"])
        head.pack(side="top", fill="x", padx=t.px(padx))
        nav = tk.Frame(head, bg=T["Bg"])
        nav.pack(side="right", anchor="s")
        left = tk.Frame(head, bg=T["Bg"])
        left.pack(side="left", fill="x", expand=True, pady=(t.px(16), t.px(12)))
        self.title = tk.Label(left, text=title, font=t.f_title, fg=T["Text1"], bg=T["Bg"], anchor="w")
        self.title.pack(anchor="w")
        self.subtitle = ElidedLabel(left, subtitle, t.f_small, fg=T["Text3"], bg=T["Bg"])
        if subtitle:
            self.subtitle.pack(fill="x", pady=(t.px(2), 0))
        for key, text in tabs:
            f = tk.Frame(nav, bg=T["Bg"])
            f.pack(side="left", padx=(t.px(4), 0))
            lb = tk.Label(f, text=text, font=t.f_body, fg=T["Text3"], bg=T["Bg"], padx=t.px(12), pady=t.px(8), cursor="hand2")
            lb.pack()
            bar = tk.Frame(f, bg=T["Bg"], height=t.px(2))
            bar.pack(fill="x")
            lb.bind("<Button-1>", lambda e, k=key: self.select(k, notify=True))
            lb.bind("<Enter>", lambda e, k=key: self._tabs[k][0].configure(fg=T["Text1"]))
            lb.bind("<Leave>", lambda e, k=key: self._tabs[k][0].configure(fg=T["Text1"] if self.current == k else T["Text3"]))
            self._tabs[key] = (lb, bar)
        tk.Frame(self, bg=T["CardBorder"], height=1).pack(side="top", fill="x")
        if tabs:
            self.select(tabs[0][0])

    def select(self, key: str, notify: bool = False) -> None:
        self.current = key
        for k, (lb, bar) in self._tabs.items():
            lb.configure(fg=T["Text1"] if k == key else T["Text3"])
            bar.configure(bg=T["Accent"] if k == key else T["Bg"])
        if notify and self.on_tab:
            self.on_tab(key)


class BusyStrip(tk.Frame):
    """【10-15 夜起不推荐：出现 / 收起会把整页推上推下，违反规范 4「界面稳定」；进度改用 BottomBar.set_progress。】
    进行中提示条（会话迁移的 busy strip）：页头下方一条白底（下面一条分隔线），左边一句「在干什么」（次要色），
    右边蓝色细进度条——10-15 夜用户嫌黄色 / 彩色块太多，改成只有进度条带颜色；
    任何页签都看得到。``show(文字, 进度)``：进度为 None 时是不定进度（来回跑），0–100 时是定量；``hide()`` 收起。
    ``delay_ms`` > 0 时延迟显示，快操作不闪。调用方负责 pack 的位置：构造时传 ``before``（通常是页面容器）。"""

    def __init__(self, master, theme: Theme, *, before: tk.Misc | None = None, padx: int = 24) -> None:
        super().__init__(master, bg=T["Surface"])
        t = theme
        self.theme = theme
        self._before = before
        self._job = None
        tk.Frame(self, bg=T["CardBorder"], height=1).pack(side="bottom", fill="x")
        inner = tk.Frame(self, bg=T["Surface"])
        inner.pack(fill="x", padx=t.px(padx), pady=t.px(8))
        self.bar = ttk.Progressbar(inner, mode="indeterminate", length=t.px(160), style="Accent.Horizontal.TProgressbar")
        self.bar.pack(side="right")
        self.label = ElidedLabel(inner, "", t.f_body, fg=T["Text2"], bg=T["Surface"])
        self.label.pack(side="left", fill="x", expand=True, padx=(0, t.px(16)))
        self._mode = "indeterminate"

    @property
    def visible(self) -> bool:
        return bool(self.winfo_manager())

    def show(self, text: str, value: float | None = None, delay_ms: int = 0) -> None:
        self.label.set_text(text)
        mode = "indeterminate" if value is None else "determinate"
        if mode != self._mode:
            self.bar.stop()
            self.bar.configure(mode=mode)
            self._mode = mode
        if value is None:
            self.bar.start(15)
        else:
            self.bar.configure(value=max(0.0, min(100.0, value)))
        if self.visible:
            return
        if delay_ms > 0:
            if self._job is None:
                self._job = self.after(delay_ms, self._reveal)
        else:
            self._reveal()

    def _reveal(self) -> None:
        self._job = None
        if not self.visible:
            kw = {"before": self._before} if self._before is not None else {}
            self.pack(side="top", fill="x", **kw)

    def hide(self) -> None:
        if self._job is not None:
            self.after_cancel(self._job)
            self._job = None
        self.bar.stop()
        if self.visible:
            self.pack_forget()


class BottomBar(tk.Frame):
    """底栏（会话迁移的样子）：白底、上面一条分隔线；左边状态点 + 一句话（``set_status``），右边 ``right`` 里放按钮，
    主按钮放最右（先 pack 的在最右：``pack(side="right")``）。``links`` 是状态文字右边放文字链接的位置。
    按钮左边固定预留一条进度条的位置（``set_progress``：None 收起进度条、位置照样空着，不让别的控件移动）。"""

    def __init__(self, master, theme: Theme, padx: int = 24) -> None:
        super().__init__(master, bg=T["Surface"])
        t = theme
        self.theme = theme
        tk.Frame(self, bg=T["CardBorder"], height=1).pack(side="top", fill="x")
        inner = tk.Frame(self, bg=T["Surface"])
        inner.pack(fill="x", padx=t.px(padx), pady=t.px(12))
        self.right = tk.Frame(inner, bg=T["Surface"])
        self.right.pack(side="right")
        # 进度条的位置固定：容器关掉尺寸传播定成 140×4，进度条用 grid / grid_remove 显示和收起，收起时位置照样留着
        self.progress_slot = tk.Frame(inner, bg=T["Surface"], width=t.px(140), height=t.px(4))
        self.progress_slot.pack(side="right", padx=(t.px(12), t.px(16)))
        self.progress_slot.grid_propagate(False)
        self.progress_slot.columnconfigure(0, weight=1)
        self.progress_slot.rowconfigure(0, weight=1)
        self.bar = ttk.Progressbar(self.progress_slot, mode="determinate", maximum=100, style="Accent.Horizontal.TProgressbar")
        self.bar.grid(row=0, column=0, sticky="nsew")
        self.bar.grid_remove()
        self._bar_mode = "determinate"
        self.links = tk.Frame(inner, bg=T["Surface"])
        self.links.pack(side="right", padx=(t.px(10), t.px(16)))
        self.dot = tk.Label(inner, image=t.dot(T["TextDisabled"]), bg=T["Surface"], bd=0)
        self.dot.pack(side="left")
        self.text = ElidedLabel(inner, "", t.f_body, fg=T["Text2"], bg=T["Surface"])
        self.text.pack(side="left", fill="x", expand=True, padx=(t.px(8), 0))

    def set_progress(self, value: float | None, busy: bool = False) -> None:
        """进度条：value 0–100 定量；busy=True 来回跑（不知道总量时）；value 为 None 且不 busy 时收起（位置照样空着）。"""
        if value is None and not busy:
            self.bar.stop()
            self.bar.grid_remove()
            return
        mode = "indeterminate" if busy else "determinate"
        if mode != self._bar_mode:
            self.bar.stop()
            self.bar.configure(mode=mode)
            self._bar_mode = mode
        if busy:
            self.bar.start(15)
        else:
            self.bar.configure(value=max(0.0, min(100.0, float(value))))
        self.bar.grid()

    def set_status(self, text: str, tone: str = "neutral") -> None:
        """tone：success（绿点）/ warning（琥珀）/ danger（红）/ accent（蓝）/ neutral（灰）。"""
        color = {"neutral": T["TextDisabled"], "gray": T["TextDisabled"]}.get(tone) or TONES[tone][1]
        self.dot.configure(image=self.theme.dot(color))
        self.text.set_text(text)


class Segmented(ttk.Frame):
    """分段选择（版本 4）：一排并列的小圆角选项，选中浅蓝底 + 蓝框 + 蓝字（不变粗，免得变宽跳动）。用于主页面上「最影响结果的那个选择」
    （如压缩档位），比下拉框一眼看得到全部选项。底下是 ttk 单选框：Tab 聚焦、方向键 / 空格切换。
    ``options``：[(值, 文字)]；``state(["disabled"])`` 整组禁用。放在卡片（白底）上。"""

    def __init__(self, master, theme: Theme, variable: tk.StringVar, options, command=None, gap: int = 6) -> None:
        super().__init__(master, style="Surface.TFrame")
        self.buttons: dict[str, ttk.Radiobutton] = {}
        for index, (value, text) in enumerate(options):
            rb = ttk.Radiobutton(self, text=text, value=value, variable=variable, style="Segment.TRadiobutton",
                                 command=(lambda v=value: command(v)) if command else None, cursor="hand2")
            rb.pack(side="left", padx=(0 if index == 0 else theme.px(gap), 0))
            self.buttons[value] = rb

    def state(self, statespec=None):
        """和 ttk 控件一样用 state(["disabled"]) 整组禁用 / 启用：状态只设在各个选项上，外面的容器不带状态。"""
        first = next(iter(self.buttons.values()))
        if statespec is None:
            return first.state()
        for rb in self.buttons.values():
            rb.state(statespec)
        return ()

    def instate(self, statespec, callback=None, *args, **kw):
        return next(iter(self.buttons.values())).instate(statespec, callback, *args, **kw)


class ChoiceCards(ttk.Frame):
    """一组单选卡片（版本 3）：每张 = 标题（可带小胶囊）+ 零到两行说明，排成一行：每张按内容宽度、多出的宽度平分
    （版本 6 起不再强制等宽：等宽要最宽那张的 N 倍宽，英文界面放不下）。适合 2–4 个互斥的选项；比下拉框一眼看得出差别。
    说明行可以只给一行很短的、也可以不给（lines 为空）而放悬停说明：按怎么简洁好看来（规范第 0 节第 23 条）。可 Tab 聚焦、方向键切换、空格 / 回车选中；``state(["disabled"])`` 整组禁用。

    options：[(值, 标题, [说明行...], 胶囊文字或 None)]。选中时写 ``variable``（StringVar），再调 ``command(值)``；
    variable 被别处改写时卡片自动跟着变。放在卡片（白底）上。
    """

    def __init__(self, master, theme: Theme, variable: tk.StringVar, options, command=None) -> None:
        super().__init__(master, style="Surface.TFrame")
        self.theme = theme
        self.variable = variable
        self.command = command
        self._disabled = False
        self.cards: dict[str, ttk.Frame] = {}
        self._parts: dict[str, list[tk.Misc]] = {}
        self._order: list[str] = []
        for index, (value, title, lines, badge_text) in enumerate(options):
            card_ = ttk.Frame(self, style="Choice.TFrame", padding=(theme.px(14), theme.px(10)), takefocus=True, cursor="hand2")
            card_.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else theme.px(10), 0))
            self.columnconfigure(index, weight=1)
            card_.columnconfigure(1, weight=1)  # 说明行跨两列，多出的宽度给第 2 列：胶囊紧跟在标题后面
            parts: list[tk.Misc] = [ttk.Label(card_, text=title, style="Choice.TLabel", cursor="hand2")]
            parts[0].grid(row=0, column=0, sticky="w")
            if badge_text:
                parts.append(ttk.Label(card_, text=badge_text, style="Choice.Badge.TLabel", cursor="hand2"))
                parts[-1].grid(row=0, column=1, sticky="w", padx=(theme.px(6), 0))
            for row, line in enumerate(lines, start=1):
                parts.append(ttk.Label(card_, text=line, style="Choice.Secondary.TLabel", cursor="hand2"))
                parts[-1].grid(row=row, column=0, columnspan=2, sticky="w", pady=(theme.px(4) if row == 1 else theme.px(1), 0))
            for widget in (card_, *parts):
                widget.bind("<Button-1>", lambda e, v=value: self.select(v, focus=True))
                widget.bind("<Enter>", lambda e, c=card_: self._hover(c, True))
                widget.bind("<Leave>", lambda e, c=card_: self._hover(c, False))
            for key in ("<space>", "<Return>"):
                card_.bind(key, lambda e, v=value: self.select(v))
            for key, step in (("<Left>", -1), ("<Up>", -1), ("<Right>", 1), ("<Down>", 1)):
                card_.bind(key, lambda e, i=index, d=step: self._move(i + d))
            self.cards[value] = card_
            self._parts[value] = parts
            self._order.append(value)
        # 部件销毁时摘掉变量监听（和 CheckBox 一样），否则重建卡片（如切换语言）后旧监听还会去改已销毁的控件
        self._trace = variable.trace_add("write", lambda *_: self._refresh())
        self.bind("<Destroy>", self._untrace, add="+")
        self._refresh()

    def _untrace(self, event) -> None:
        if event.widget is self:
            try:
                self.variable.trace_remove("write", self._trace)
            except tk.TclError:
                pass

    def _hover(self, card_: ttk.Frame, on: bool) -> None:
        if not on:  # 移到卡片里的文字上也会触发 Leave：指针还在卡片范围内就不算离开
            x, y = card_.winfo_pointerxy()
            on = (card_.winfo_rootx() <= x < card_.winfo_rootx() + card_.winfo_width()
                  and card_.winfo_rooty() <= y < card_.winfo_rooty() + card_.winfo_height())
        card_.state(["hover" if on else "!hover"])

    def _move(self, index: int) -> None:
        if 0 <= index < len(self._order):
            self.select(self._order[index], focus=True)

    def select(self, value: str, focus: bool = False) -> None:
        if self._disabled:
            return
        if focus:
            self.cards[value].focus_set()
        if self.variable.get() != value:
            self.variable.set(value)
            if self.command:
                self.command(value)

    def _refresh(self) -> None:
        current = self.variable.get()
        for value, card_ in self.cards.items():
            spec = ["selected" if value == current else "!selected", "disabled" if self._disabled else "!disabled"]
            for widget in (card_, *self._parts[value]):
                widget.state(spec)
            card_.configure(takefocus=not self._disabled, cursor="" if self._disabled else "hand2")

    def state(self, statespec=None):
        """和 ttk 控件一样用 state(["disabled"]) 整组禁用 / 启用：状态只设在卡片上，外面的容器不带状态。"""
        if statespec is None:
            return ("disabled",) if self._disabled else ()
        for flag in statespec:
            if flag in ("disabled", "!disabled"):
                self._disabled = flag == "disabled"
        self._refresh()
        return ()

    def instate(self, statespec, callback=None, *args, **kw):
        return next(iter(self.cards.values())).instate(statespec, callback, *args, **kw)


class Disclosure(tk.Frame):
    """就地展开的「高级选项」（会话迁移的样子，规范第 4 节第二层）：一个次要色文字链接（箭头 + 标题），右边一句小字说明
    （``set_note``，参数改过时写「已改过」并用 warning 色调）；点它展开 / 收起 ``body``。``head`` 右边可再放按钮（如「恢复默认」）；
    ``lead`` 是标题行最前面的位置，放和这组参数相关的第一层选择（如压缩档位），省掉一行。
    ``bg``：放在窗口底上用 T["Bg"]（body 是一张卡片），放在卡片里用 T["Surface"]（body 是平铺区域）。默认收起。"""

    def __init__(self, master, theme: Theme, title: str, *, expanded: bool = False, on_toggle=None,
                 bg: str = T["Surface"], body_padding: int = 14) -> None:
        super().__init__(master, bg=bg)
        self.theme = theme
        self.on_toggle = on_toggle
        self.expanded = False
        self.head = tk.Frame(self, bg=bg, takefocus=True, highlightthickness=1, highlightbackground=bg,
                             highlightcolor=T["Accent"])
        self.head.pack(fill="x")
        self.lead = tk.Frame(self.head, bg=bg)
        self.lead.pack(side="left")
        self.chevron = tk.Label(self.head, font=theme.icon_font(8) if theme.icon_family else theme.f_small,
                                fg=T["Text2"], bg=bg, cursor="hand2")
        self.chevron.pack(side="left", padx=(0, theme.px(4)))
        self.title_font = theme.f_body
        self.title = tk.Label(self.head, text=title, font=self.title_font, fg=T["Text2"], bg=bg, cursor="hand2")
        self.title.pack(side="left")
        # 说明放不下时截断成「…」，不把窗口撑宽（日志折叠区在这里显示最新一行）；head 右边后放的按钮照样有位置
        self.note = ElidedLabel(self.head, "", theme.f_small, fg=T["Text3"], bg=bg, cursor="hand2")
        self.note.pack(side="left", fill="x", expand=True, padx=(theme.px(10), theme.px(8)))
        for widget in (self.chevron, self.title, self.note):
            widget.bind("<Button-1>", lambda e: self.toggle())
            widget.bind("<Enter>", lambda e: self.title.configure(font=theme.underlined(self.title_font)))
            widget.bind("<Leave>", lambda e: self.title.configure(font=self.title_font))
        for key in ("<space>", "<Return>"):
            self.head.bind(key, lambda e: self.toggle())
        if bg == T["Surface"]:
            self.body = ttk.Frame(self, style="Surface.TFrame")
        else:
            self.body = card(self, theme, body_padding)
        self.set_expanded(expanded, notify=False)

    def set_note(self, text: str, tone: str | None = None) -> None:
        self.note.configure(fg=TONES[tone][1] if tone else T["Text3"])
        self.note.set_text(text)

    def toggle(self) -> None:
        self.set_expanded(not self.expanded)

    def set_expanded(self, expanded: bool, notify: bool = True) -> None:
        self.expanded = expanded
        if self.theme.icon_family:
            self.chevron.configure(text=ICONS["down" if expanded else "right"])
        else:
            self.chevron.configure(text="\u25be" if expanded else "\u25b8")
        if expanded:
            self.body.pack(fill="x", pady=(self.theme.px(8), 0))
        else:
            self.body.pack_forget()
        if notify and self.on_toggle:
            self.on_toggle(expanded)


class InfoBar(tk.Frame):
    """一条提示条（浅色底 + 图标 + 一行字），用于「列表已过期」「缺少工具」这类页内提示。"""

    def __init__(self, master, theme: Theme, tone: str = "accent") -> None:
        tint, fg, border = TONES[tone]
        super().__init__(master, bg=tint, highlightthickness=1, highlightbackground=border)
        self.theme = theme
        self.icon = tk.Label(self, font=theme.icon_font(10), fg=fg, bg=tint)
        self.icon.pack(side="left", padx=(theme.px(10), theme.px(6)), pady=theme.px(6))
        self.text = tk.Label(self, font=theme.f_body, fg=fg, bg=tint, anchor="w", justify="left")
        self.text.pack(side="left", fill="x", expand=True, pady=theme.px(6), padx=(0, theme.px(10)))
        self.set("", tone)

    def set(self, text: str, tone: str = "accent", icon: str | None = None) -> None:
        tint, fg, border = TONES[tone]
        self.configure(bg=tint, highlightbackground=border)
        default_icon = {"accent": "info", "success": "done", "warning": "warn", "danger": "error", "neutral": "info",
                        "ok": "done", "warn": "warn", "gray": "info"}[tone]
        self.icon.configure(text=self.theme.icon(icon or default_icon), fg=fg, bg=tint)
        self.text.configure(text=text, fg=fg, bg=tint)


class ElidedLabel(tk.Label):
    """单行标签：放不下时截断成「…」（mid=True 截中间，适合路径）。width=1 让长文字不把窗口撑宽。"""

    def __init__(self, master, text: str, font, mid: bool = False, **kw) -> None:
        super().__init__(master, text=text, font=font, width=1, anchor="w", **kw)
        self._full = text
        self._font = font
        self._mid = mid
        self.bind("<Configure>", lambda e: self._fit(e.width))

    def set_text(self, text: str) -> None:
        self._full = text
        self._fit(self.winfo_width())

    def get_text(self) -> str:
        """完整文字（cget("text") 拿到的是截断后显示的）。"""
        return self._full

    def _cut(self, keep: int) -> str:
        if self._mid:
            head = keep * 2 // 5
            return self._full[:head] + "…" + self._full[len(self._full) - (keep - head):]
        return self._full[:keep] + "…"

    def _fit(self, width: int) -> None:
        avail = width - 4
        text = self._full
        if avail > 0 and self._font.measure(text) > avail:
            lo, hi = 0, len(text)
            while lo < hi:  # 二分找能放下的最多字符数
                mid = (lo + hi + 1) // 2
                if self._font.measure(self._cut(mid)) <= avail:
                    lo = mid
                else:
                    hi = mid - 1
            text = self._cut(lo)
        if str(self.cget("text")) != text:
            self.configure(text=text)


class WrapLabel(tk.Label):
    """随宽度自动换行的多行标签：宽度变了就用 wrap_text 重新断行（避头尾），不用 Tk 的 wraplength。
    改文字用 set_text()（直接 configure(text=…) 不会断行）；get_text() 取原文。"""

    def __init__(self, master, text: str, font, **kw) -> None:
        kw.setdefault("anchor", "w")
        super().__init__(master, text=text, font=font, justify="left", **kw)
        self._full = text
        self._font = font
        self._widths: dict[str, int] = {}      # wrap_text 的量宽缓存：拖动窗口时只做 Python 计算
        self._width = 0
        self.bind("<Configure>", self._on_configure)

    def set_text(self, text: str) -> None:
        self._full = text
        self._layout()

    def get_text(self) -> str:
        """原文（cget("text") 拿到的是插了换行的）。"""
        return self._full

    def _on_configure(self, event) -> None:
        if event.width != self._width:          # 只在宽度变了时重新断行；文字变高引起的 Configure 不管
            self._width = event.width
            self._layout()

    def _layout(self) -> None:
        avail = self._width - 4
        text = wrap_text(self._font, self._full, avail, self._widths) if avail > 0 else self._full
        if str(self.cget("text")) != text:
            self.configure(text=text)


class Link(tk.Frame):
    """文字链接（可带图标）：悬停加下划线，点击执行 command。规范：文字操作（「重新检测」「查看明细」）用它，不做成按钮。"""

    def __init__(self, master, theme: Theme, text: str, command, bg: str = T["Surface"], fg: str = T["AccentText"],
                 icon: str | None = None, font=None) -> None:
        super().__init__(master, bg=bg, cursor="hand2")
        self.theme = theme
        self.command = command
        self.enabled = True
        self._fg = fg
        self._font = font or theme.f_body
        self.icon_label = tk.Label(self, font=theme.icon_font(8), fg=fg, bg=bg, cursor="hand2")
        self.label = tk.Label(self, text=text, font=self._font, fg=fg, bg=bg, cursor="hand2")
        self.label.pack(side="right")
        self.set_icon(icon)
        for w in (self, self.icon_label, self.label):
            w.bind("<Enter>", lambda e: self.enabled and self.label.configure(font=theme.underlined(self._font)))
            w.bind("<Leave>", lambda e: self.label.configure(font=self._font))
            w.bind("<Button-1>", lambda e: self.enabled and self.command())

    def set_enabled(self, enabled: bool) -> None:
        """不可用时置灰、点了没反应、不显示手形（规范 4「界面稳定」：链接不出现 / 消失，用可用 / 不可用表示）。"""
        self.enabled = enabled
        fg = self._fg if enabled else T["TextDisabled"]
        cursor = "hand2" if enabled else ""
        for w in (self, self.icon_label, self.label):
            w.configure(cursor=cursor)
        self.icon_label.configure(fg=fg)
        self.label.configure(fg=fg, font=self._font)

    def set_icon(self, icon: str | None) -> None:
        if icon and self.theme.icon_family:
            self.icon_label.configure(text=self.theme.icon(icon))
            self.icon_label.pack(side="left", padx=(0, self.theme.px(4)))
        else:
            self.icon_label.pack_forget()

    def set_text(self, text: str) -> None:
        self.label.configure(text=text)


class CheckBox(tk.Label):
    """图片复选框（会话迁移），绑定一个 BooleanVar；画在白卡片上。"""

    def __init__(self, master, theme: Theme, variable: tk.BooleanVar, command=None, bg: str = T["Surface"]) -> None:
        super().__init__(master, bg=bg, bd=0, cursor="hand2", takefocus=1)
        self.theme = theme
        self.var = variable
        self.command = command
        self.hover = False
        self._trace = variable.trace_add("write", lambda *_: self._render())
        self.bind("<Button-1>", lambda e: self.toggle())
        self.bind("<space>", lambda e: self.toggle())
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<Destroy>", self._untrace)
        self._render()

    def _untrace(self, event) -> None:
        if event.widget is self:
            try:
                self.var.trace_remove("write", self._trace)
            except tk.TclError:
                pass

    def _set_hover(self, on: bool) -> None:
        self.hover = on
        self._render()

    def toggle(self) -> None:
        self.var.set(not self.var.get())
        if self.command:
            self.command()

    def _render(self) -> None:
        checked = bool(self.var.get())
        img = self.theme.check_hover if (self.hover and not checked) else self.theme.check_img[(checked, True)]
        self.configure(image=img)


class CheckRow(tk.Frame):
    """一行可勾选的条目（会话迁移）：复选框 + 粗体标题（可带标签）+ 几行说明；点整行任意位置都能切换。
    ``badges``：[(文字, 色调)]。开关型设置（规范第 4 节第二层）用它：标题 + 一句说明 + 「推荐：开 / 关」。"""

    def __init__(self, master, theme: Theme, variable: tk.BooleanVar, title: str, lines=(), badges=(),
                 command=None, bg: str = T["Surface"], padx: int = 14, pady: int = 10) -> None:
        super().__init__(master, bg=bg, cursor="hand2")
        t = theme
        self.box = CheckBox(self, t, variable, command, bg=bg)
        self.box.grid(row=0, column=0, sticky="nw", padx=(t.px(padx), t.px(10)), pady=(t.px(pady), 0))
        head = tk.Frame(self, bg=bg, cursor="hand2")
        head.grid(row=0, column=1, sticky="ew", padx=(0, t.px(padx)), pady=(t.px(pady), 0))
        tk.Label(head, text=title, font=t.f_bold, fg=T["Text1"], bg=bg, cursor="hand2").pack(side="left")
        for text, tone in badges:
            badge(head, t, text, tone).pack(side="left", padx=(t.px(6), 0))
        for i, line in enumerate(lines, 1):
            ElidedLabel(self, line, t.f_small, fg=T["Text3"], bg=bg, cursor="hand2").grid(
                row=i, column=1, sticky="ew", padx=(0, t.px(padx)), pady=(t.px(2), 0))
        tk.Frame(self, bg=bg, height=t.px(pady)).grid(row=len(lines) + 1, column=0, columnspan=2)
        self.columnconfigure(1, weight=1)
        self._bind_toggle(self)

    def _bind_toggle(self, w) -> None:
        if w is self.box:
            return
        w.bind("<Button-1>", lambda e: self.box.toggle())
        for c in w.winfo_children():
            self._bind_toggle(c)


class ScrollList(tk.Frame):
    """可滚动的竖向列表（会话迁移）：子控件放进 .inner。max_height 给定时高度随内容、最高到它；否则填满可用空间。
    滚轮：窗口统一 bind_all("<MouseWheel>") 后找到所在的 ScrollList 调 ``scroll(delta)``。"""

    def __init__(self, master, bg: str, max_height: int | None = None, scrollbar_style: str = "Card.Vertical.TScrollbar"):
        super().__init__(master, bg=bg)
        self.max_height = max_height
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0, height=1 if max_height else 200)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview, style=scrollbar_style)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)

    def _on_inner(self, e) -> None:
        self.canvas.configure(scrollregion=(0, 0, e.width, e.height))
        if self.max_height is not None:
            self.canvas.configure(height=min(e.height, self.max_height))
        self.after_idle(self._toggle_bar)

    def _on_canvas(self, e) -> None:
        self.canvas.itemconfigure(self._win, width=e.width)
        self.after_idle(self._toggle_bar)

    def _toggle_bar(self) -> None:
        need = self.inner.winfo_reqheight() > self.canvas.winfo_height() + 1
        if need and not self.vsb.winfo_manager():
            self.vsb.pack(side="right", fill="y", before=self.canvas)
        elif not need and self.vsb.winfo_manager():
            self.vsb.pack_forget()
            self.canvas.yview_moveto(0)

    def scroll(self, delta: int) -> None:
        if self.inner.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(int(-delta / 120) or (-1 if delta > 0 else 1), "units")


class Dialog(tk.Toplevel):
    """统一样式的确认 / 提示框（代替系统 messagebox；规范第 4 节：只用于执行前确认、必须马上回答的问题、出错说明）。
    lines 每项是一段文字，或 (文字, 颜色)。按钮写动作（「开始迁移」），取消在左；回车 = 确定，Esc = 取消。"""

    ICON = {"accent": ("info", T["Accent"]), "danger": ("warn", T["Danger"]), "warn": ("warn", T["Warning"]),
            "ok": ("done", T["Success"]), "info": ("info", T["Text3"])}

    def __init__(self, master: tk.Misc, theme: Theme, heading: str, lines: list, ok: str, cancel: str | None, tone: str,
                 title: str | None = None) -> None:
        super().__init__(master)
        self.withdraw()
        color_titlebar(self)  # 和主窗口一样染 Windows 蓝（第一次映射、也就是 deiconify 时染上）
        top = master.winfo_toplevel()
        self.title(title or top.title())
        self.configure(bg=T["Surface"])
        self.resizable(False, False)
        self.transient(top)
        self.result = False
        t = theme
        body = tk.Frame(self, bg=T["Surface"])
        body.pack(fill="both", expand=True, padx=t.px(24), pady=(t.px(22), t.px(4)))
        name, color = self.ICON[tone]
        if t.icon_family:
            tk.Label(body, text=t.icon(name), font=t.icon_font(20), fg=color, bg=T["Surface"]).grid(
                row=0, column=0, rowspan=len(lines) + 1, sticky="n", padx=(0, t.px(14)))
        wrap = t.px(420)
        tk.Label(body, text=wrap_text(t.f_h, heading, wrap), font=t.f_h, fg=T["Text1"], bg=T["Surface"],
                 justify="left", anchor="w").grid(row=0, column=1, sticky="w")
        for i, line in enumerate(lines, 1):
            text, fg = line if isinstance(line, tuple) else (line, T["Text2"])
            tk.Label(body, text=wrap_text(t.f_body, text, wrap), font=t.f_body, fg=fg, bg=T["Surface"],
                     justify="left", anchor="w").grid(row=i, column=1, sticky="w", pady=(t.px(10 if i == 1 else 6), 0))
        btns = tk.Frame(self, bg=T["Surface"])
        btns.pack(fill="x", padx=t.px(24), pady=(t.px(16), t.px(20)))
        okb = ttk.Button(btns, text=ok, style="Danger.TButton" if tone == "danger" else "Primary.TButton",
                         command=self._ok, cursor="hand2")
        okb.pack(side="right")
        if cancel:
            ttk.Button(btns, text=cancel, style="Secondary.TButton", command=self.destroy,
                       cursor="hand2").pack(side="right", padx=(0, t.px(8)))
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.update_idletasks()
        x = top.winfo_rootx() + (top.winfo_width() - self.winfo_reqwidth()) // 2
        y = top.winfo_rooty() + (top.winfo_height() - self.winfo_reqheight()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.deiconify()
        self.grab_set()
        okb.focus_set()

    def _ok(self) -> None:
        self.result = True
        self.destroy()

    @staticmethod
    def show(master: tk.Misc, theme: Theme, heading: str, lines=(), ok: str = "确定", cancel: str | None = "取消",
             tone: str = "accent", title: str | None = None) -> bool:
        d = Dialog(master, theme, heading, list(lines), ok, cancel, tone, title)
        master.wait_window(d)
        return d.result


def detail_window(master: tk.Misc, theme: Theme, title: str, width: int = 860, height: int = 540) -> tk.Toplevel:
    """只读明细子窗口（规范第 4 节第三层，会话迁移的「查看明细」）：非模态、暖灰底、相对主窗口居中偏上。
    内容由调用方往里放；关掉不影响主窗口。"""
    top = master.winfo_toplevel()
    win = tk.Toplevel(top)
    win.title(title)
    win.configure(bg=T["Bg"])
    win.transient(top)
    color_titlebar(win)
    w, h = theme.px(width), theme.px(height)
    win.geometry(f"{w}x{h}+{top.winfo_rootx() + (top.winfo_width() - w) // 2}"
                 f"+{top.winfo_rooty() + max(0, (top.winfo_height() - h) // 3)}")
    return win


class Tooltip:
    """悬停提示（深色小框，令牌 TooltipBg / TooltipText）。``text`` 可以是字符串或无参函数（悬停时取）。"""

    def __init__(self, widget: tk.Misc, theme: Theme, text, delay_ms: int = 500) -> None:
        self.widget = widget
        self.theme = theme
        self.text = text
        self.delay_ms = delay_ms
        self._job = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._job = self.widget.after(self.delay_ms, self._show)

    def _cancel(self) -> None:
        if self._job is not None:
            self.widget.after_cancel(self._job)
            self._job = None

    def _show(self) -> None:
        text = self.text() if callable(self.text) else self.text
        if not text:
            return
        t = self.theme
        self._tip = tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.configure(bg=T["TooltipBg"])
        tk.Label(tip, text=wrap_text(t.f_small, text, t.px(360)), font=t.f_small, fg=T["TooltipText"],
                 bg=T["TooltipBg"], justify="left", padx=t.px(8), pady=t.px(5)).pack()
        x = self.widget.winfo_rootx() + t.px(8)
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + t.px(4)
        tip.wm_geometry(f"+{x}+{y}")

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None
