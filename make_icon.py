# -*- coding: utf-8 -*-
"""TECFOLIO（PDF編集ソフト）のアイコン（favicon.ico・icon.png・icon_192.png）を作る。py -3.12 make_icon.py

名前：TECTONICA ＋ FOLIO（紙・ページ）。
形（2026-11-20 作り直し）：画面の色に合わせた朱色のグラデーションの角丸の四角に、白い書類（右上を折り返し）と、
書類に斜めに当てたペン（PDF に書き込む・直す）。TECTRA（紺・線画）とは色も形も分ける。
16px でも書類とペンと読めるよう、小さいアイコンは行を減らしペンを太くする。
"""
import hashlib
import math
import os
import re

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
N = 1024
WHITE = (255, 255, 255, 255)
FOLD = (255, 205, 196, 255)     # 折り返しの裏
LINE = (232, 120, 100, 255)     # 書類の文字の行
INK = (38, 42, 50, 255)         # ペンの軸


def _gradient(c0, c1):
    im = Image.new("RGBA", (N, N))
    px = im.load()
    for y in range(0, N, 2):
        for x in range(0, N, 2):
            t = min(max((x + y) / (2 * N) * 1.15 - 0.05, 0), 1)
            c = tuple(int(c0[i] + (c1[i] - c0[i]) * t) for i in range(3)) + (255,)
            for dx in (0, 1):
                for dy in (0, 1):
                    px[x + dx, y + dy] = c
    return im


def _round_line(d, p, q, w, col):
    d.line([p, q], fill=col, width=w)
    for c in (p, q):
        d.ellipse([c[0] - w / 2, c[1] - w / 2, c[0] + w / 2, c[1] + w / 2], fill=col)


def _pen(d, tip, ang, length, width, small):
    """先を tip に、角度 ang（度、右上向き）で伸びるペン。軸・口金・ペン先。"""
    ux, uy = math.cos(math.radians(ang)), -math.sin(math.radians(ang))
    nx, ny = -uy, ux
    P = lambda s, t: (tip[0] + ux * s + nx * t, tip[1] + uy * s + ny * t)
    w = width / 2
    cone = width * 1.25
    # ペン先（白い三角＋黒い先）
    d.polygon([P(0, 0), P(cone, w), P(cone, -w)], fill=(214, 196, 178, 255))
    d.polygon([P(0, 0), P(cone * 0.38, w * 0.38), P(cone * 0.38, -w * 0.38)], fill=INK)
    # 口金（明るい帯）と軸
    d.polygon([P(cone, w), P(cone + width * 0.35, w), P(cone + width * 0.35, -w), P(cone, -w)], fill=(255, 214, 170, 255))
    body0 = cone + width * 0.35
    d.polygon([P(body0, w), P(length, w), P(length, -w), P(body0, -w)], fill=INK)
    d.ellipse([P(length, 0)[0] - w, P(length, 0)[1] - w, P(length, 0)[0] + w, P(length, 0)[1] + w], fill=INK)
    if not small:   # 軸のつや
        _round_line(d, P(body0 + 40, -w * 0.45), P(length - 30, -w * 0.45), int(width * 0.16), (90, 96, 108, 255))


def draw(small=False):
    mask = Image.new("L", (N, N), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, N - 1, N - 1], radius=int(N * 0.225), fill=255)
    out = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    out.paste(_gradient((240, 104, 74), (150, 28, 22)), (0, 0), mask)
    d = ImageDraw.Draw(out)
    # 影（少しずらした半透明の黒）
    sh = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    x0, y0, x1, y1 = (215, 150, 735, 860) if not small else (190, 120, 770, 880)
    fold = 170 if not small else 210
    page = [(x0, y0), (x1 - fold, y0), (x1, y0 + fold), (x1, y1), (x0, y1)]
    ImageDraw.Draw(sh).polygon([(x + 18, y + 22) for x, y in page], fill=(60, 0, 0, 70))
    out = Image.alpha_composite(out, sh)
    d = ImageDraw.Draw(out)
    # 白い書類と折り返し
    d.rounded_rectangle([x0, y0, x1, y1], radius=26, fill=WHITE)
    d.polygon([(x1 - fold - 2, y0 - 2), (x1 + 2, y0 - 2), (x1 + 2, y0 + fold + 2)], fill=(0, 0, 0, 0))
    d.polygon([(x1 - fold, y0), (x1, y0 + fold), (x1 - fold, y0 + fold)], fill=FOLD)
    # 角を抜いたところに背景を戻す
    bg = _gradient((240, 104, 74), (150, 28, 22))
    corner = Image.new("L", (N, N), 0)
    ImageDraw.Draw(corner).polygon([(x1 - fold, y0), (x1 - fold, y0 - 40), (x1 + 40, y0 - 40), (x1 + 40, y0 + fold), (x1, y0 + fold)], fill=255)
    out.paste(bg, (0, 0), corner)
    d = ImageDraw.Draw(out)
    d.polygon([(x1 - fold, y0), (x1, y0 + fold), (x1 - fold, y0 + fold)], fill=FOLD)
    # 文字の行
    lw = 46 if not small else 70
    left = x0 + 85
    rows = [(y0 + 300, x1 - 85), (y0 + 420, x1 - 85), (y0 + 540, x0 + 330)] if not small else [(y0 + 380, x1 - 95)]
    for y, r in rows:
        _round_line(d, (left, y), (r, y), lw, LINE)
    # ペン（右下から左上の書類へ斜めに当てる）
    if small:
        _pen(d, (430, 760), 40, 640, 170, True)
    else:
        _pen(d, (470, 700), 40, 600, 130, False)
    clip = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    clip.paste(out, (0, 0), mask)
    return clip


def main():
    big, small = draw(), draw(small=True)
    imgs = [(small if n <= 24 else big).resize((n, n), Image.LANCZOS) for n in (256, 128, 64, 48, 40, 32, 24, 20, 16)]
    imgs[0].save(os.path.join(HERE, "favicon.ico"), sizes=[im.size for im in imgs], append_images=imgs[1:])
    big.resize((512, 512), Image.LANCZOS).save(os.path.join(HERE, "icon.png"))
    big.resize((192, 192), Image.LANCZOS).save(os.path.join(HERE, "icon_192.png"))
    # 画面のロゴは中身ごとに URL を変える（ブラウザが前のロゴを覚えていて変わらないため）
    v = hashlib.sha1(open(os.path.join(HERE, "favicon.ico"), "rb").read()).hexdigest()[:8]
    p = os.path.join(HERE, "index.html")
    t = open(p, encoding="utf-8").read()
    t = re.sub(r'(favicon\.ico|icon\.png|icon_192\.png)(\?v=\w+)?"', lambda m: f'{m.group(1)}?v={v}"', t)
    open(p, "w", encoding="utf-8").write(t)
    print("ok", v)


if __name__ == "__main__":
    main()
