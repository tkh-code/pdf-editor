# -*- coding: utf-8 -*-
"""TECFOLIO（PDF編集ソフト）のアイコン（favicon.ico・icon.png・icon_192.png）を作る。py -3.12 make_icon.py

名前：TECTONICA ＋ FOLIO（紙・ページ）。TECTRA（構造BIM一貫ツール）と同じ家族のロゴにする。
形：TECTRA と同じ紺のグラデーションの角丸の四角に、白い線の書類を 2 枚重ね（奥は半透明）。
手前の書類は右上を折り返し、文字の行 3 本のうち 1 本を水色のマーカー（PDF に書き込む）。
16px でも書類と読めるよう、小さいアイコンは行を減らして線を太くする。
"""
import hashlib
import os
import re

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
N = 1024
WHITE = (255, 255, 255, 255)
CYAN = (70, 220, 238, 255)


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


def _seg(d, p, q, w, col):
    d.line([p, q], fill=col, width=w)
    for c in (p, q):
        d.ellipse([c[0] - w / 2 + 1, c[1] - w / 2 + 1, c[0] + w / 2 - 1, c[1] + w / 2 - 1], fill=col)


def _sheet(x0, y0, x1, y1, fold):
    """右上を折り返した書類の外形（折り返しの斜め線つき）。"""
    return [(x0, y0), (x1 - fold, y0), (x1, y0 + fold), (x1, y1), (x0, y1)]


def draw(small=False):
    mask = Image.new("L", (N, N), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, N - 1, N - 1], radius=int(N * 0.225), fill=255)
    out = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    out.paste(_gradient((34, 110, 166), (8, 22, 40)), (0, 0), mask)
    lw = 56 if small else 32
    fold = 150
    # 奥の書類（半透明）。手前と少しずらす
    glass = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    g = ImageDraw.Draw(glass)
    bx0, by0, bx1, by1 = 340, 170, 800, 760
    if not small:
        pts = _sheet(bx0, by0, bx1, by1, fold)
        for a, b in zip(pts, pts[1:] + pts[:1]):
            _seg(g, a, b, 24, (255, 255, 255, 110))
    # 手前の書類の面（うっすら白）
    fx0, fy0, fx1, fy1 = (250, 250, 730, 850) if not small else (230, 190, 790, 840)
    g.polygon(_sheet(fx0, fy0, fx1, fy1, fold), fill=(255, 255, 255, 34))
    out = Image.alpha_composite(out, glass)
    d = ImageDraw.Draw(out)
    # 手前の書類の外形と折り返し
    pts = _sheet(fx0, fy0, fx1, fy1, fold)
    # 奥の書類が手前に透けないよう、手前の面の内側を背景色で塗り直してから線を描く
    for a, b in zip(pts, pts[1:] + pts[:1]):
        _seg(d, a, b, lw, WHITE)
    _seg(d, (fx1 - fold, fy0), (fx1 - fold, fy0 + fold), lw, WHITE)
    _seg(d, (fx1 - fold, fy0 + fold), (fx1, fy0 + fold), lw, WHITE)
    # 文字の行：白・水色（マーカー）・白。小さいアイコンは 2 本
    left, right = fx0 + 95, fx1 - 95
    if small:
        rows = [(fy0 + 330, right, WHITE), (fy0 + 480, right - 70, CYAN)]
    else:
        rows = [(fy0 + 280, right, WHITE), (fy0 + 400, right, CYAN), (fy0 + 520, right - 110, WHITE)]
    for y, r, col in rows:
        _seg(d, (left, y), (r, y), (lw + 14) if col == CYAN else lw, col)
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
