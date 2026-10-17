"""
app_icon.py — 程序图标：Windows 蓝圆角方块上一个白色对话气泡，气泡里一个向右的箭头（把对话搬到新账号）。

只用标准库：按 4×4 超采样画出 RGBA，再编码成 PNG（窗口图标）或多尺寸 ICO（打包 exe 时用）。
不把图片文件放进仓库，改图标只改这里的几何参数。
"""

from __future__ import annotations

import struct
import zlib

BLUE = (0x00, 0x5F, 0xB8)    # 共用主题的强调色 Accent（Windows 11 浅色主题强调按钮色）
WHITE = (0xFF, 0xFF, 0xFF)
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 256)


def _round_rect(x0: float, y0: float, x1: float, y1: float, r: float):
    def inside(x: float, y: float) -> bool:
        if x < x0 or x > x1 or y < y0 or y > y1:
            return False
        dx = max(x0 + r - x, 0.0, x - (x1 - r))
        dy = max(y0 + r - y, 0.0, y - (y1 - r))
        return dx * dx + dy * dy <= r * r
    return inside


def _triangle(a, b, c):
    def side(p, q, x, y):
        return (q[0] - p[0]) * (y - p[1]) - (q[1] - p[1]) * (x - p[0])

    def inside(x: float, y: float) -> bool:
        s1, s2, s3 = side(a, b, x, y), side(b, c, x, y), side(c, a, x, y)
        return (s1 >= 0 and s2 >= 0 and s3 >= 0) or (s1 <= 0 and s2 <= 0 and s3 <= 0)
    return inside


def _stroke(points, width: float):
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


def rgba(size: int, ss: int = 4) -> bytes:
    """画 size×size 的图标，返回逐行 RGBA 字节（从上到下）。坐标按 0–1 的比例定，各尺寸同一套几何。"""
    s = float(size)
    tile = _round_rect(0.04 * s, 0.04 * s, 0.96 * s, 0.96 * s, 0.22 * s)
    bubble = _round_rect(0.18 * s, 0.22 * s, 0.82 * s, 0.66 * s, 0.13 * s)
    tail = _triangle((0.30 * s, 0.60 * s), (0.46 * s, 0.60 * s), (0.27 * s, 0.80 * s))
    line = max(1.4, 0.075 * s)
    arrow = [_stroke([(0.32 * s, 0.44 * s), (0.64 * s, 0.44 * s)], line),
             _stroke([(0.54 * s, 0.34 * s), (0.65 * s, 0.44 * s), (0.54 * s, 0.54 * s)], line)]
    offs = [(i + 0.5) / ss for i in range(ss)]
    n = ss * ss
    out = bytearray()
    for py in range(size):
        for px in range(size):
            cover = r = g = b = 0
            for oy in offs:
                y = py + oy
                for ox in offs:
                    x = px + ox
                    if not tile(x, y):
                        continue
                    color = BLUE
                    if bubble(x, y) or tail(x, y):
                        color = BLUE if any(test(x, y) for test in arrow) else WHITE
                    cover += 1
                    r += color[0]
                    g += color[1]
                    b += color[2]
            if cover:
                out += bytes((round(r / cover), round(g / cover), round(b / cover), round(255 * cover / n)))
            else:
                out += b"\0\0\0\0"
    return bytes(out)


def png(size: int) -> bytes:
    """size×size 的 PNG（RGBA，带透明）。"""
    data = rgba(size)
    raw = b"".join(b"\0" + data[y * size * 4:(y + 1) * size * 4] for y in range(size))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _dib(size: int) -> bytes:
    """ICO 里的 32 位位图：BITMAPINFOHEADER（高度写两倍）+ 自下而上的 BGRA + 全 0 的 AND 掩码（透明靠 alpha）。"""
    data = rgba(size)
    rows = []
    for y in range(size - 1, -1, -1):
        row = data[y * size * 4:(y + 1) * size * 4]
        rows.append(b"".join(bytes((row[i + 2], row[i + 1], row[i], row[i + 3])) for i in range(0, len(row), 4)))
    mask_row = ((size + 31) // 32) * 4
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    return header + b"".join(rows) + b"\0" * (mask_row * size)


def ico(sizes=ICO_SIZES) -> bytes:
    """多尺寸 ICO：256 用 PNG 压缩（Vista 起支持），其余用 32 位位图（资源管理器、任务栏各取所需）。"""
    images = [png(s) if s >= 256 else _dib(s) for s in sizes]
    offset = 6 + 16 * len(images)
    entries = b""
    for size, img in zip(sizes, images):
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(img), offset)
        offset += len(img)
    return struct.pack("<HHH", 0, 1, len(images)) + entries + b"".join(images)
