# -*- coding: utf-8 -*-
"""PDF編集ソフト 処理本体（ブラウザ内の Python = Pyodide で動く）

worker.js から呼ばれ、PyMuPDF でページ描画・文字抽出・PDF書き出しを行う。
ファイルはすべてブラウザの中で処理し、外部には送らない。
"""
import contextlib
import json
import math
import os
import re
import uuid

import pymupdf

FONT_DIR = "/fonts"

# 画面のフォント選択肢 → フォントファイル（fonts/ に同梱、SIL OFL）
FONTS = {
    "gothic": "BIZUDPGothic-Regular.ttf",
    "gothic-b": "BIZUDPGothic-Bold.ttf",
    "mincho": "BIZUDPMincho-Regular.ttf",
    "mono": "BIZUDGothic-Regular.ttf",
}
# 英数字専用：PDF標準フォント（埋め込み不要）
BASE14 = {"arial": "helv", "times": "tiro"}

# 書き出しごとのフォント名の接尾辞（このツールで作ったPDFを再編集したとき、
# 既存の部分フォントと名前がぶつかって文字が欠けるのを防ぐ）
RUN = {"tag": "0"}

# 読み込んだPDF・画像（id → Document / bytes）
SOURCES = {}
IMAGES = {}


def font_args(key):
    """insert_text に渡す fontname / fontfile。"""
    if key in BASE14:
        return {"fontname": BASE14[key]}
    key = key if key in FONTS else "gothic"
    return {"fontname": "F" + key.replace("-", "") + RUN["tag"], "fontfile": FONT_DIR + "/" + FONTS[key]}


_FONT_OBJ = {}
FONT_XREF = {}   # 書き出し中の文書に埋め込んだフォント（名前 → xref）。2ページ目からは参照だけ足す


def _add_font_ref(page, name, xref):
    """ページの /Resources/Font に既存フォントへの参照を足す（フォントを再び埋め込まない）。"""
    doc = page.parent
    t, v = doc.xref_get_key(page.xref, "Resources")
    if t == "xref":
        rx = int(v.split()[0])
        t2, v2 = doc.xref_get_key(rx, "Font")
        if t2 == "xref":
            doc.xref_set_key(int(v2.split()[0]), name, f"{xref} 0 R")
        else:
            doc.xref_set_key(rx, "Font/" + name, f"{xref} 0 R")
    elif t == "dict":
        t2, v2 = doc.xref_get_key(page.xref, "Resources/Font")
        if t2 == "xref":
            doc.xref_set_key(int(v2.split()[0]), name, f"{xref} 0 R")
        else:
            doc.xref_set_key(page.xref, "Resources/Font/" + name, f"{xref} 0 R")
    else:
        return False   # 継承された Resources など。安全のため通常の埋め込みにする
    return True


def page_font(page, key):
    """insert_text に渡すフォント指定。同じ文書では1回だけ埋め込み、他のページは参照する。"""
    if key in BASE14:
        return {"fontname": BASE14[key]}
    key = key if key in FONTS else "gothic"
    name = "F" + key.replace("-", "") + RUN["tag"]
    if any(f[4] == name for f in page.get_fonts()):
        return {"fontname": name}
    k = (id(page.parent), name)
    if k in FONT_XREF and _add_font_ref(page, name, FONT_XREF[k]):
        return {"fontname": name}
    FONT_XREF[k] = page.insert_font(fontname=name, fontfile=FONT_DIR + "/" + FONTS[key])
    return {"fontname": name}


def text_width(text, key, size):
    """PDFに書いたときの文字列の幅（中央・右寄せの位置合わせ用）。"""
    if key in BASE14:
        return pymupdf.get_text_length(text, fontname=BASE14[key], fontsize=size)
    key = key if key in FONTS else "gothic"
    if key not in _FONT_OBJ:
        _FONT_OBJ[key] = pymupdf.Font(fontfile=FONT_DIR + "/" + FONTS[key])
    return _FONT_OBJ[key].text_length(text, fontsize=size)


def hex2rgb(h, default=(0, 0, 0)):
    if not h:
        return default
    h = h.lstrip("#")
    if len(h) != 6:
        return default
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


# PDF以外で開けるファイル（読み込み時にPDFへ変換）
OTHER_TYPES = {"svg", "xps", "oxps", "epub", "cbz", "fb2", "txt", "mobi"}


def add_source(data, name, path=None):
    """PDF・画像・その他文書を登録し、ページ情報を返す。

    path を渡すと仮想ファイル（ブラウザ側のメモリ）から開くので、巨大なPDFでも
    Python 側のメモリに丸ごと複製しない。
    """
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    head = data[:1024] if data is not None else open(path, "rb").read(1024)
    if b"%PDF" in head:
        doc = pymupdf.open(path) if path else pymupdf.open(stream=data, filetype="pdf")
        if doc.needs_pass:
            raise ValueError("パスワード付きPDFは開けません（パスワードを解除してから開いてください）")
    else:
        # 画像（複数ページTIFFも可）・SVG・XPS・テキスト等 → PDFに変換
        ft = ext if ext in OTHER_TYPES else None
        src = pymupdf.open(path, filetype=ft) if path else pymupdf.open(stream=data, filetype=ft)
        doc = pymupdf.open("pdf", src.convert_to_pdf())
    sid = uuid.uuid4().hex[:12]
    SOURCES[sid] = doc
    pages = []
    for p in doc:
        info = {"src": sid, "index": p.number,
                "w": p.rect.width, "h": p.rect.height,  # 回転反映後の見た目サイズ
                "rot": p.rotation}
        if doc.is_pdf and p.first_annot:
            anns = import_annots(p)
            if anns:
                info["anns"] = anns
        pages.append(info)
    return {"id": sid, "name": name, "pages": pages}


def _hex(c):
    if not c:
        return ""
    if len(c) == 1:
        c = (c[0], c[0], c[0])
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(v * 255))) for v in c[:3])


def _da(doc, xref):
    """FreeText の /DA から文字サイズと色。"""
    try:
        da = doc.xref_get_key(xref, "DA")[1]
    except Exception:
        da = ""
    m = re.search(r"([\d.]+)\s+Tf", da)
    size = float(m.group(1)) if m else 12.0
    col = "#000000"
    m = re.search(r"([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+rg", da)
    if m:
        col = _hex([float(v) for v in m.groups()])
    else:
        m = re.search(r"([\d.]+)\s+g\b", da)
        if m:
            col = _hex([float(m.group(1))])
    return size or 12.0, col


IMPORT_TYPES = {pymupdf.PDF_ANNOT_FREE_TEXT, pymupdf.PDF_ANNOT_SQUARE, pymupdf.PDF_ANNOT_CIRCLE,
                pymupdf.PDF_ANNOT_LINE, pymupdf.PDF_ANNOT_INK, pymupdf.PDF_ANNOT_HIGHLIGHT}


def import_annots(page):
    """Acrobat などで付けた注釈（文字・四角・円・線・手書き・マーカー）を編集できる形に取り込み、
    元の注釈はページから外す（保存時に取り込んだものを書き直すので二重にならない）。"""
    m = page.rotation_matrix
    out, done = [], []
    for a in page.annots():
        t = a.type[0]
        if t not in IMPORT_TYPES:
            continue
        stroke = _hex((a.colors or {}).get("stroke"))
        fill = _hex((a.colors or {}).get("fill"))
        bw = float((a.border or {}).get("width", 1) or 1)
        if bw < 0:
            bw = 1.0
        r = a.rect * m
        o = None
        if t == pymupdf.PDF_ANNOT_FREE_TEXT:
            size, col = _da(page.parent, a.xref)
            fill = fill or stroke   # FreeText は /C が背景色
            text = (a.info or {}).get("content", "")
            if text.strip():
                o = {"type": "text", "text": text, "font": "gothic", "size": round(size, 1), "color": col,
                     "x": r.x0 + 2, "y": r.y0 + 1}
                if fill:
                    o.update(type="note", fill=fill, w=r.width, h=r.height, x=r.x0, y=r.y0)
        elif t in (pymupdf.PDF_ANNOT_SQUARE, pymupdf.PDF_ANNOT_CIRCLE):
            rr = pymupdf.Rect(r.x0 + bw / 2, r.y0 + bw / 2, r.x1 - bw / 2, r.y1 - bw / 2)
            o = {"type": "rect" if t == pymupdf.PDF_ANNOT_SQUARE else "ellipse", "x": rr.x0, "y": rr.y0,
                 "w": rr.width, "h": rr.height, "color": stroke or "#e00000", "width": bw, "fill": fill}
        elif t == pymupdf.PDF_ANNOT_LINE:
            v = [pymupdf.Point(p) * m for p in (a.vertices or [])]
            if len(v) >= 2:
                ends = a.line_ends or (0, 0)
                p1, p2 = v[0], v[-1]
                arrow = bool(ends[1]) or bool(ends[0])
                if ends[0] and not ends[1]:
                    p1, p2 = p2, p1
                o = {"type": "arrow" if arrow else "line", "x1": p1.x, "y1": p1.y, "x2": p2.x, "y2": p2.y,
                     "color": stroke or "#e00000", "width": bw}
        elif t == pymupdf.PDF_ANNOT_INK:
            for stroke_pts in (a.vertices or []):
                pts = [list(pymupdf.Point(p) * m) for p in stroke_pts]
                if len(pts) >= 2:
                    out.append({"type": "ink", "points": pts, "color": stroke or "#0033cc", "width": bw})
            done.append(a)
            continue
        elif t == pymupdf.PDF_ANNOT_HIGHLIGHT:
            v = [pymupdf.Point(p) * m for p in (a.vertices or [])]
            for i in range(0, len(v) - 3, 4):
                q = pymupdf.Rect(v[i], v[i]) | v[i + 1] | v[i + 2] | v[i + 3]
                out.append({"type": "highlight", "x": q.x0, "y": q.y0, "w": q.width, "h": q.height,
                            "color": stroke or "#ffe600", "opacity": 0.35})
            done.append(a)
            continue
        if o:
            out.append(o)
            done.append(a)
    for a in done:
        page.delete_annot(a)
    return out


@contextlib.contextmanager
def rotated(src, index, extra):
    """元ページを一時的に回転させて使う（ページを複製しないので速い）。"""
    doc = SOURCES[src]
    p = doc[int(index)]
    r0 = p.rotation
    extra = int(extra or 0) % 360
    if extra:
        p.set_rotation((r0 + extra) % 360)
    try:
        yield p
    finally:
        if extra:
            p.set_rotation(r0)


def render_png(q):
    src = q["src"]
    scale = float(q.get("scale", 1))
    rot = int(q.get("rot", 0))
    if src == "blank":
        w = float(q.get("w", 595))
        h = float(q.get("h", 842))
        tmp = pymupdf.open()
        p = tmp.new_page(width=w, height=h)
        p.set_rotation(rot % 360)
        pix = p.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        return pix.tobytes("png")
    with rotated(src, q["index"], rot) as p:
        pix = p.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    return pix.tobytes("png")


def page_spans(q):
    """既存テキスト編集用：見た目座標でのテキスト行（span）一覧。"""
    src = q["src"]
    if src == "blank":
        return []
    with rotated(src, q["index"], q.get("rot", 0)) as p:
        rot_m = p.rotation_matrix
        d = p.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)
    out = []
    for b in d["blocks"]:
        for line in b.get("lines", []):
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            text = "".join(s["text"] for s in line["spans"])
            r = pymupdf.Rect()
            for s in spans:
                r |= pymupdf.Rect(s["bbox"])
            vr = r * rot_m
            s0 = spans[0]
            c = s0["color"]
            base = pymupdf.Point(s0["origin"]) * rot_m
            out.append({
                "text": text.strip(),
                "x0": vr.x0, "y0": vr.y0, "x1": vr.x1, "y1": vr.y1,
                "size": round(s0["size"], 1),
                "color": "#%06x" % c,
                "bold": bool(s0["flags"] & 16),
                "font": s0["font"],
                "base": base.y,
            })
    return out


def draw_text(page, ann, derot):
    size = float(ann.get("size", 12))
    color = hex2rgb(ann.get("color"))
    fa = page_font(page, ann.get("font", "gothic"))
    lh = float(ann.get("lineHeight", 1.25))
    base = float(ann.get("baseline", 0.95))  # 画面で実測した「行の上端→基準線」の比
    pad = float(ann.get("pad", 0))
    x, y = ann["x"] + pad, ann["y"] + pad
    for i, line in enumerate(str(ann.get("text", "")).split("\n")):
        if not line:
            continue
        pt = pymupdf.Point(x, y + size * base + i * size * lh) * derot
        page.insert_text(pt, line, fontsize=size, color=color, rotate=page.rotation, **fa)


def seal_layout(a):
    """日付印の配置（画面側 index.html の sealLayout と同じ計算）。"""
    D = float(a["w"])
    r = D / 2
    cx, cy = a["x"] + r, a["y"] + r
    band = 0.17 * D
    font = a.get("font", "gothic")
    out = []

    def fit(text, size0, avail):
        w1 = text_width(text, font, 1) or 1
        return min(size0, avail / w1)

    if a.get("date"):
        size = fit(a["date"], 0.17 * D, 2 * math.sqrt(r * r - band * band) * 0.9)
        out.append((a["date"], size, cy + size * 0.36))
    for text, sign in ((a.get("top", ""), -1), (a.get("bottom", ""), 1)):
        if not text:
            continue
        dy = (band + r) / 2
        size = fit(text, 0.19 * D, 2 * math.sqrt(r * r - dy * dy) * 0.85)
        out.append((text, size, cy + sign * dy + size * 0.36))
    return cx, cy, r, band, out


def draw_seal(page, a, derot):
    col = hex2rgb(a.get("color"), (0.85, 0, 0))
    cx, cy, r, band, texts = seal_layout(a)
    lw = max(0.8, 2 * r * 0.025)
    page.draw_circle(pymupdf.Point(cx, cy) * derot, r - lw / 2, color=col, width=lw)
    hw = math.sqrt(r * r - band * band)
    for dy in (-band, band):
        page.draw_line(pymupdf.Point(cx - hw, cy + dy) * derot, pymupdf.Point(cx + hw, cy + dy) * derot,
                       color=col, width=lw * 0.8)
    fa = page_font(page, a.get("font", "gothic"))
    for text, size, base in texts:
        x = cx - text_width(text, a.get("font", "gothic"), size) / 2
        page.insert_text(pymupdf.Point(x, base) * derot, text, fontsize=size, color=col,
                         rotate=page.rotation, **fa)


def draw_measure(page, a, derot):
    """計測（距離・面積）：線と数値を書き込む。数値と位置は画面側で計算済み。"""
    col = hex2rgb(a.get("color"), (0, 0.4, 0.8))
    w = float(a.get("width", 1))
    if a["type"] == "dim":
        p1, p2 = pymupdf.Point(a["x1"], a["y1"]), pymupdf.Point(a["x2"], a["y2"])
        page.draw_line(p1 * derot, p2 * derot, color=col, width=w)
        L = math.hypot(p2.x - p1.x, p2.y - p1.y) or 1
        nx, ny = -(p2.y - p1.y) / L * 4, (p2.x - p1.x) / L * 4   # 両端の短い目印
        for q in (p1, p2):
            page.draw_line(pymupdf.Point(q.x - nx, q.y - ny) * derot, pymupdf.Point(q.x + nx, q.y + ny) * derot,
                           color=col, width=w)
    else:
        pts = [pymupdf.Point(x, y) * derot for x, y in a["points"]]
        if len(pts) >= 3:
            page.draw_polyline(pts, color=col, fill=hex2rgb(a.get("fill"), col), width=w, closePath=True,
                               fill_opacity=0.15)
    label = a.get("label")
    if label:
        size = float(a.get("size", 9))
        font = "gothic"
        x = float(a["lx"]) - text_width(label, font, size) / 2
        page.insert_text(pymupdf.Point(x, float(a["ly"])) * derot, label, fontsize=size, color=col,
                         rotate=page.rotation, **page_font(page, font))


def draw_ocr(page, lines):
    """文字認識（OCR）の結果を見えない文字として重ねる（検索・コピーできるPDFになる）。"""
    derot = page.derotation_matrix
    fa = page_font(page, "gothic")
    vertical = page.rotation in (90, 270)
    for ln in lines:
        x0, y0, x1, y1 = ln["r"]
        text = ln["t"]
        h = y1 - y0
        if not text.strip() or h <= 0 or x1 <= x0:
            continue
        size = max(3.0, h * 0.85)
        tw = text_width(text, "gothic", size) or 1
        sx = (x1 - x0) / tw
        pt = pymupdf.Point(x0, y1 - h * 0.15) * derot
        mat = pymupdf.Matrix(1, sx) if vertical else pymupdf.Matrix(sx, 1)
        page.insert_text(pt, text, fontsize=size, render_mode=3, rotate=page.rotation, morph=(pt, mat), **fa)


def vrect(a, derot):
    return pymupdf.Rect(a["x"], a["y"], a["x"] + a["w"], a["y"] + a["h"]) * derot


# 「注釈として保存」で PDF 注釈にする種類（Acrobat・PDF-XChange で後から移動・修正・削除できる）
NATIVE = {"text", "note", "rect", "ellipse", "line", "arrow", "ink", "highlight"}


def add_native(page, a, derot):
    t = a["type"]
    col = hex2rgb(a.get("color"), (1, 0, 0))
    w = float(a.get("width", 2))
    if t in ("text", "note"):
        size = float(a.get("size", 12))
        lines = str(a.get("text", "")).split("\n")
        bw = float(a.get("bw") or max(len(l) for l in lines) * size)
        bh = float(a.get("bh") or len(lines) * size * 1.25)
        pad = float(a.get("pad", 0))
        r = pymupdf.Rect(a["x"], a["y"], a["x"] + bw * 1.06 + 4 + 2 * pad, a["y"] + bh + 4 + 2 * pad) * derot
        kw = dict(fontsize=size, fontname="japan", text_color=hex2rgb(a.get("color")), rotate=page.rotation)
        if t == "note":
            kw.update(fill_color=hex2rgb(a.get("fill"), (1, 0.96, 0.6)))
        an = page.add_freetext_annot(r, str(a.get("text", "")), **kw)
    elif t in ("rect", "ellipse"):
        r = pymupdf.Rect(a["x"], a["y"], a["x"] + a["w"], a["y"] + a["h"]) * derot
        an = page.add_rect_annot(r) if t == "rect" else page.add_circle_annot(r)
        an.set_colors(stroke=col, fill=hex2rgb(a["fill"]) if a.get("fill") else None)
        an.set_border(width=w)
    elif t in ("line", "arrow"):
        an = page.add_line_annot(pymupdf.Point(a["x1"], a["y1"]) * derot, pymupdf.Point(a["x2"], a["y2"]) * derot)
        if t == "arrow":
            an.set_line_ends(pymupdf.PDF_ANNOT_LE_NONE, pymupdf.PDF_ANNOT_LE_OPEN_ARROW)
        an.set_colors(stroke=col)
        an.set_border(width=w)
    elif t == "ink":
        an = page.add_ink_annot([[tuple(pymupdf.Point(x, y) * derot) for x, y in a["points"]]])
        an.set_colors(stroke=hex2rgb(a.get("color"), (0, 0, 1)))
        an.set_border(width=w)
    elif t == "highlight":
        r = pymupdf.Rect(a["x"], a["y"], a["x"] + a["w"], a["y"] + a["h"]) * derot
        an = page.add_highlight_annot(r)
        an.set_colors(stroke=hex2rgb(a.get("color"), (1, 1, 0)))
    else:
        return
    an.set_info(title="PDF編集ソフト")
    an.update()


def apply_annotations(page, anns, native=False):
    derot = page.derotation_matrix
    if native:
        for a in anns:
            if a["type"] in NATIVE:
                add_native(page, a, derot)
        anns = [a for a in anns if a["type"] not in NATIVE]
    # 1) 既存テキストの置換：先に墨消し（文字だけ除去）
    redacts = [a for a in anns if a["type"] == "edittext"]
    if redacts:
        for a in redacts:
            r = pymupdf.Rect(a["ox0"], a["oy0"], a["ox1"], a["oy1"]) * derot
            page.add_redact_annot(r, fill=False)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    # 2) 墨消し：範囲内の文字・画像・図形を本当に削除して塗りつぶす
    blacks = [a for a in anns if a["type"] == "redact"]
    if blacks:
        for a in blacks:
            page.add_redact_annot(vrect(a, derot), fill=hex2rgb(a.get("fill"), (0, 0, 0)))
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED)
    for a in anns:
        t = a["type"]
        if t in ("text", "edittext", "stamp", "note"):
            if t == "stamp":
                r = vrect(a, derot)
                page.draw_rect(r, color=hex2rgb(a.get("color"), (0.8, 0, 0)), width=1.5,
                               radius=min(3 / max(a["w"], 1), 3 / max(a["h"], 1)))
            if t == "note":
                page.draw_rect(vrect(a, derot), color=hex2rgb(a.get("border"), (0.85, 0.7, 0.1)),
                               fill=hex2rgb(a.get("fill"), (1, 0.96, 0.6)), width=0.75)
            draw_text(page, a, derot)
        elif t == "dateseal":
            draw_seal(page, a, derot)
        elif t in ("dim", "area"):
            draw_measure(page, a, derot)
        elif t == "whiteout":
            page.draw_rect(vrect(a, derot), color=None, fill=hex2rgb(a.get("fill"), (1, 1, 1)))
        elif t == "rect":
            fill = hex2rgb(a["fill"]) if a.get("fill") else None
            page.draw_rect(vrect(a, derot), color=hex2rgb(a.get("color"), (1, 0, 0)),
                           fill=fill, width=float(a.get("width", 2)))
        elif t == "ellipse":
            fill = hex2rgb(a["fill"]) if a.get("fill") else None
            page.draw_oval(vrect(a, derot), color=hex2rgb(a.get("color"), (1, 0, 0)),
                           fill=fill, width=float(a.get("width", 2)))
        elif t == "highlight":
            page.draw_rect(vrect(a, derot), color=None, fill=hex2rgb(a.get("color"), (1, 1, 0)),
                           fill_opacity=float(a.get("opacity", 0.35)))
        elif t in ("line", "arrow"):
            p1 = pymupdf.Point(a["x1"], a["y1"]) * derot
            p2 = pymupdf.Point(a["x2"], a["y2"]) * derot
            col = hex2rgb(a.get("color"), (1, 0, 0))
            w = float(a.get("width", 2))
            page.draw_line(p1, p2, color=col, width=w)
            if t == "arrow":
                import math
                vx, vy = a["x2"] - a["x1"], a["y2"] - a["y1"]
                L = math.hypot(vx, vy) or 1
                ux, uy = vx / L, vy / L
                hl = max(8, w * 4)
                for sgn in (1, -1):
                    ang = math.radians(25) * sgn
                    rx = ux * math.cos(ang) - uy * math.sin(ang)
                    ry = ux * math.sin(ang) + uy * math.cos(ang)
                    q = pymupdf.Point(a["x2"] - rx * hl, a["y2"] - ry * hl) * derot
                    page.draw_line(p2, q, color=col, width=w)
        elif t == "ink":
            pts = [pymupdf.Point(x, y) * derot for x, y in a["points"]]
            if len(pts) >= 2:
                page.draw_polyline(pts, color=hex2rgb(a.get("color"), (0, 0, 1)),
                                   width=float(a.get("width", 2)), closePath=False)
        elif t == "image":
            data = IMAGES.get(a["img"])
            if data:
                page.insert_image(vrect(a, derot), stream=data, keep_proportion=False,
                                  rotate=page.rotation)


def mark_pos(mk, W, H, tw):
    """ページ番号・ヘッダー/フッターの基準点（画面側 markPos と同じ計算）。"""
    pos = mk.get("pos", "bottom-center")
    size = float(mk.get("size", 10))
    mx, my = float(mk.get("mx", 20)), float(mk.get("my", 20))
    if pos.endswith("center"):
        x = (W - tw) / 2
    elif pos.endswith("right"):
        x = W - mx - tw
    else:
        x = mx
    y = my + size * 0.8 if pos.startswith("top") else H - my
    return x, y


PAPER = {"A4": (595.28, 841.89), "A3": (841.89, 1190.55)}


def fit_paper(out, paper):
    """全ページを指定の用紙サイズに拡大縮小（縦横は元ページに合わせる）。"""
    W0, H0 = PAPER[paper]
    new = pymupdf.open()
    for p in out:
        W, H = (H0, W0) if p.rect.width > p.rect.height else (W0, H0)
        np_ = new.new_page(width=W, height=H)
        np_.show_pdf_page(np_.rect, out, p.number)
    return new


def stamp_all(out, opts):
    """ページ番号・透かし・ヘッダー等、全ページ共通の書き込み。"""
    wm = opts.get("watermark")
    marks_by_page = opts.get("marks") or []
    for i, page in enumerate(out):
        derot = page.derotation_matrix
        W, H = page.rect.width, page.rect.height  # 見た目サイズ
        # ページ番号・ヘッダー/フッター（ページごとの文字列は画面側で組み立て済み）
        for mk in (marks_by_page[i] if i < len(marks_by_page) else None) or []:
            txt = mk["text"]
            size = float(mk.get("size", 10))
            font = mk.get("font", "gothic")
            x, y = mark_pos(mk, W, H, text_width(txt, font, size))
            page.insert_text(pymupdf.Point(x, y) * derot, txt, fontsize=size, rotate=page.rotation,
                             color=hex2rgb(mk.get("color")), **page_font(page, font))
        if wm and wm.get("enabled") and wm.get("text"):
            size = float(wm.get("size", 60))
            txt = wm["text"]
            col = hex2rgb(wm.get("color"), (0.6, 0.6, 0.6))
            fa = page_font(page, wm.get("font", "gothic"))
            # 中央に斜め45°で配置（Shape + morph で回転）
            tl = len(txt) * size
            cx, cy = W / 2, H / 2
            start = pymupdf.Point(cx - tl / 2, cy + size * 0.35)
            ang = float(wm.get("angle", 45))
            mat = pymupdf.Matrix(ang)  # 左下→右上の向き
            sh = page.new_shape()
            sh.insert_text(start * derot, txt, fontsize=size, color=col, rotate=page.rotation,
                           morph=(pymupdf.Point(cx, cy) * derot, mat),
                           fill_opacity=float(wm.get("opacity", 0.25)),
                           stroke_opacity=float(wm.get("opacity", 0.25)), **fa)
            sh.commit(overlay=True)


def build_pdf(spec, path=None):
    RUN["tag"] = uuid.uuid4().hex[:6]
    FONT_XREF.clear()
    opts = spec.get("options", {})
    native = opts.get("annotMode") == "annot"
    out = pymupdf.open()
    pages = spec["pages"]
    # 同じ元ファイルの連続したページはまとめて取り込む（大きな文書で速い）
    i = 0
    while i < len(pages):
        pg = pages[i]
        if pg["src"] == "blank":
            p = out.new_page(width=float(pg.get("w", 595)), height=float(pg.get("h", 842)))
            p.set_rotation(int(pg.get("rot", 0)) % 360)
            i += 1
            continue
        j = i
        while (j + 1 < len(pages) and pages[j + 1]["src"] == pg["src"]
               and int(pages[j + 1]["index"]) == int(pages[j]["index"]) + 1):
            j += 1
        out.insert_pdf(SOURCES[pg["src"]], from_page=int(pg["index"]), to_page=int(pages[j]["index"]))
        i = j + 1
    for k, pg in enumerate(pages):
        rot = int(pg.get("rot", 0)) % 360
        if not rot and not pg.get("annotations"):
            continue
        p = out[k]
        if rot and pg["src"] != "blank":
            p.set_rotation((p.rotation + rot) % 360)
        if pg.get("annotations"):
            apply_annotations(p, pg["annotations"], native)
    for k, pg in enumerate(pages):
        if pg.get("ocr"):
            draw_ocr(out[k], pg["ocr"])
    if opts.get("paper") in PAPER:
        out = fit_paper(out, opts["paper"])
        RUN["tag"] = uuid.uuid4().hex[:6]  # 縮小前に埋め込んだフォントと名前を分ける
    stamp_all(out, opts)
    # しおり（目次）
    toc = [[1, pg["bookmark"], i + 1] for i, pg in enumerate(spec["pages"]) if pg.get("bookmark")]
    if toc:
        out.set_toc(toc)

    meta = spec.get("options", {}).get("meta")
    if meta:
        m = out.metadata or {}
        m.update({k: v for k, v in meta.items() if v})
        out.set_metadata(m)
    if fonts_needed(spec):
        try:
            out.subset_fonts()
        except Exception:
            pass
    big = len(out) > 300
    kw = dict(garbage=1 if big else 3, deflate=True, clean=bool(opts.get("compress")))
    if opts.get("compress"):
        kw.update(garbage=3, deflate_images=True, deflate_fonts=True)
    pw = opts.get("password")
    if pw:
        kw.update(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=pw, owner_pw=pw,
                  permissions=-1)
    if path:
        out.save(path, **kw)   # 仮想ファイルへ（巨大PDFでも Python 側のメモリを使い切らない）
        out.close()
        return path
    return out.tobytes(**kw)


def add_image(data):
    pix = pymupdf.Pixmap(data)  # 形式チェック兼サイズ取得
    iid = uuid.uuid4().hex[:12]
    IMAGES[iid] = data
    return {"id": iid, "w": pix.width, "h": pix.height}


def fonts_needed(spec):
    """書き出しに必要なフォントファイル名（worker が事前に読み込む）。"""
    keys = set()
    for pg in spec["pages"]:
        for a in pg.get("annotations") or []:
            if a.get("type") in ("text", "edittext", "stamp", "note", "dateseal"):
                keys.add(a.get("font", "gothic"))
            if a.get("type") in ("dim", "area") and a.get("label"):
                keys.add("gothic")
    if any(pg.get("ocr") for pg in spec["pages"]):
        keys.add("gothic")
    o = spec.get("options") or {}
    for marks in o.get("marks") or []:
        for mk in marks or []:
            keys.add(mk.get("font", "gothic"))
    if (o.get("watermark") or {}).get("enabled"):
        keys.add((o["watermark"]).get("font", "gothic"))
    return sorted({FONTS.get(k, FONTS["gothic"]) for k in keys if k not in BASE14})


# ---- worker.js から呼ぶ入口（引数・戻り値は JSON 文字列 / bytes） ----
def _bytes(data):
    return data.to_bytes() if hasattr(data, "to_bytes") else bytes(data)


def api_open(data, name):
    return json.dumps(add_source(_bytes(data), name), ensure_ascii=False)


def api_open_path(path, name):
    return json.dumps(add_source(None, name, path), ensure_ascii=False)


def api_export_file(spec, path):
    return build_pdf(json.loads(spec), path)


def api_search(q):
    """文字検索：ページごとの一致箇所（見た目の座標）。"""
    q = json.loads(q)
    needle = q["q"]
    res = []
    for k, it in enumerate(q["items"]):
        if it["src"] == "blank":
            continue
        with rotated(it["src"], it["index"], it.get("rot", 0)) as p:
            m = p.rotation_matrix
            rects = [r * m for r in p.search_for(needle)]
        if rects:
            res.append({"k": k, "rects": [[r.x0, r.y0, r.x1, r.y1] for r in rects]})
    return json.dumps(res)


def api_textin(q):
    """範囲内の文字を取り出す（文字選択ツール）。"""
    q = json.loads(q)
    if q["src"] == "blank":
        return ""
    with rotated(q["src"], q["index"], q.get("rot", 0)) as p:
        r = pymupdf.Rect(q["rect"]) * p.derotation_matrix
        return p.get_textbox(r).strip()


def api_image(data):
    return json.dumps(add_image(_bytes(data)))


def api_render(q):
    return render_png(json.loads(q))


def api_spans(q):
    return json.dumps(page_spans(json.loads(q)), ensure_ascii=False)


def api_fonts(spec):
    return json.dumps(fonts_needed(json.loads(spec)))


def api_export(spec):
    return build_pdf(json.loads(spec))


def api_raster(q):
    """PDF→画像変換：1ページを PNG / JPEG に。"""
    q = json.loads(q)
    p = SOURCES[q["src"]][int(q["index"])]
    pix = p.get_pixmap(dpi=int(q.get("dpi", 150)), alpha=False)
    if q.get("fmt") == "jpg":
        return pix.tobytes("jpg", jpg_quality=int(q.get("quality", 90)))
    return pix.tobytes("png")


def api_text(src):
    """PDF→テキスト変換。"""
    doc = SOURCES[src]
    parts = []
    for p in doc:
        parts.append(f"===== {p.number + 1} ページ =====\n" + p.get_text().strip())
    return "\n\n".join(parts) + "\n"


def api_close(src):
    SOURCES.pop(src, None)


def api_hastext(q):
    """ページに文字データがあるか（OCR が必要かの判定）。"""
    out = []
    for it in json.loads(q)["items"]:
        if it["src"] == "blank":
            out.append(True)
            continue
        p = SOURCES[it["src"]][int(it["index"])]
        out.append(bool(p.get_text("text").strip()))
    return json.dumps(out)
