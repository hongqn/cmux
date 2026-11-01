"""Minimal QR Code encoder (byte mode, versions 1-10) that prints to a terminal.

Standard library only, so the web remote keeps working without pip installs.
The construction follows ISO/IEC 18004; see https://www.nayuki.io/page/qr-code-generator-library
for a readable walkthrough of the same steps.
"""

from typing import List, Tuple

# (EC codewords per block, [(block count, data codewords per block), ...]) by version, for L and M.
EC_BLOCKS = {
    "L": {
        1: (7, [(1, 19)]), 2: (10, [(1, 34)]), 3: (15, [(1, 55)]), 4: (20, [(1, 80)]),
        5: (26, [(1, 108)]), 6: (18, [(2, 68)]), 7: (20, [(2, 78)]), 8: (24, [(2, 97)]),
        9: (30, [(2, 116)]), 10: (18, [(2, 68), (2, 69)]),
    },
    "M": {
        1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]),
        5: (24, [(2, 43)]), 6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]),
        9: (22, [(3, 36), (2, 37)]), 10: (26, [(4, 43), (1, 44)]),
    },
}
FORMAT_EC_BITS = {"L": 1, "M": 0}
ALIGNMENT = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
    7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}
MASKS = [
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
]


def _gf_mul(a: int, b: int) -> int:
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((b >> i) & 1) * a
    return z


def rs_divisor(degree: int) -> List[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def rs_remainder(data: List[int], degree: int) -> List[int]:
    divisor = rs_divisor(degree)
    result = [0] * degree
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _data_capacity(version: int, ecl: str) -> int:
    return sum(n * k for n, k in EC_BLOCKS[ecl][version][1])


def _codewords(payload: bytes, version: int, ecl: str) -> List[int]:
    bits: List[int] = []

    def put(value: int, length: int) -> None:
        bits.extend((value >> i) & 1 for i in range(length - 1, -1, -1))

    put(0b0100, 4)
    put(len(payload), 8 if version < 10 else 16)
    for b in payload:
        put(b, 8)
    capacity = _data_capacity(version, ecl) * 8
    put(0, min(4, capacity - len(bits)))
    put(0, -len(bits) % 8)
    data = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(data) < capacity // 8:
        data.append(pad)
        pad ^= 0xEC ^ 0x11

    ec_len, groups = EC_BLOCKS[ecl][version]
    blocks: List[List[int]] = []
    ecs: List[List[int]] = []
    pos = 0
    for count, size in groups:
        for _ in range(count):
            block = data[pos:pos + size]
            pos += size
            blocks.append(block)
            ecs.append(rs_remainder(block, ec_len))
    out: List[int] = []
    for i in range(max(len(b) for b in blocks)):
        out.extend(b[i] for b in blocks if i < len(b))
    for i in range(ec_len):
        out.extend(e[i] for e in ecs)
    return out


def format_bits(ecl: str, mask: int) -> int:
    data = FORMAT_EC_BITS[ecl] << 3 | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return (data << 10 | rem) ^ 0x5412


class _Grid:
    def __init__(self, version: int) -> None:
        self.version = version
        self.size = version * 4 + 17
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.function = [[False] * self.size for _ in range(self.size)]

    def set_function(self, x: int, y: int, dark: bool) -> None:
        self.dark[y][x] = dark
        self.function[y][x] = True

    def draw_function_patterns(self) -> None:
        n = self.size
        for i in range(n):
            self.set_function(6, i, i % 2 == 0)
            self.set_function(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        self.set_function(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = ALIGNMENT[self.version]
        last = len(pos) - 1
        for i, ax in enumerate(pos):
            for j, ay in enumerate(pos):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set_function(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format("L", 0)  # reserve; redrawn with the real mask
        if self.version >= 7:
            rem = self.version
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.version << 12 | rem
            for i in range(18):
                bit = (bits >> i) & 1 == 1
                a, b = n - 11 + i % 3, i // 3
                self.set_function(a, b, bit)
                self.set_function(b, a, bit)

    def draw_format(self, ecl: str, mask: int) -> None:
        bits = format_bits(ecl, mask)
        bit = lambda i: (bits >> i) & 1 == 1  # noqa: E731
        n = self.size
        for i in range(6):
            self.set_function(8, i, bit(i))
        self.set_function(8, 7, bit(6))
        self.set_function(8, 8, bit(7))
        self.set_function(7, 8, bit(8))
        for i in range(9, 15):
            self.set_function(14 - i, 8, bit(i))
        for i in range(8):
            self.set_function(n - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set_function(8, n - 15 + i, bit(i))
        self.set_function(8, n - 8, True)

    def draw_codewords(self, data: List[int]) -> None:
        n = self.size
        i = 0
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = n - 1 - vert if upward else vert
                    if not self.function[y][x] and i < len(data) * 8:
                        self.dark[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask: int) -> None:
        test = MASKS[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.function[y][x] and test(x, y):
                    self.dark[y][x] = not self.dark[y][x]

    def penalty(self) -> int:
        n = self.size
        score = 0
        lines = [row[:] for row in self.dark] + [[self.dark[y][x] for y in range(n)] for x in range(n)]
        finder = [True, False, True, True, True, False, True]
        for line in lines:
            run = 1
            for k in range(1, n + 1):
                if k < n and line[k] == line[k - 1]:
                    run += 1
                    continue
                if run >= 5:
                    score += run - 2
                run = 1
            padded = [False] * 4 + line + [False] * 4
            for k in range(len(padded) - 10):
                window = padded[k:k + 11]
                if window[:7] == finder and not any(window[7:]):
                    score += 40
                if window[4:] == finder and not any(window[:4]):
                    score += 40
        for y in range(n - 1):
            for x in range(n - 1):
                c = self.dark[y][x]
                if c == self.dark[y][x + 1] == self.dark[y + 1][x] == self.dark[y + 1][x + 1]:
                    score += 3
        dark = sum(map(sum, self.dark))
        total = n * n
        score += ((abs(dark * 20 - total * 10) + total - 1) // total - 1) * 10
        return score


def encode(text: str, ecl: str = "M") -> List[List[bool]]:
    """Return the module matrix (True = dark) for text, in the smallest version that fits."""
    payload = text.encode("utf-8")
    for version in range(1, 11):
        header_bits = 4 + (8 if version < 10 else 16)
        if header_bits + len(payload) * 8 <= _data_capacity(version, ecl) * 8:
            break
    else:
        raise ValueError(f"text too long for a version 1-10 QR code ({len(payload)} bytes)")
    data = _codewords(payload, version, ecl)
    best: Tuple[int, List[List[bool]]] = (-1, [])
    for mask in range(8):
        grid = _Grid(version)
        grid.draw_function_patterns()
        grid.draw_codewords(data)
        grid.apply_mask(mask)
        grid.draw_format(ecl, mask)
        score = grid.penalty()
        if best[0] < 0 or score < best[0]:
            best = (score, grid.dark)
    return best[1]


def render(matrix: List[List[bool]], quiet: int = 4) -> str:
    """Two modules per character cell, black on white so it scans on dark themes too."""
    n = len(matrix)
    size = n + quiet * 2
    at = lambda x, y: 0 <= x - quiet < n and 0 <= y - quiet < n and matrix[y - quiet][x - quiet]  # noqa: E731
    rows = []
    for y in range(0, size, 2):
        chars = []
        for x in range(size):
            top, bottom = at(x, y), at(x, y + 1) if y + 1 < size else False
            chars.append("█" if top and bottom else "▀" if top else "▄" if bottom else " ")
        rows.append("\x1b[38;5;16;48;5;231m" + "".join(chars) + "\x1b[0m")
    return "\n".join(rows)
