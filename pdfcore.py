# -*- coding: utf-8 -*-
"""PDF編集ソフト 処理本体（ブラウザ内の Python = Pyodide で動く）

worker.js から呼ばれ、PyMuPDF でページ描画・文字抽出・PDF書き出しを行う。
ファイルはすべてブラウザの中で処理し、外部には送らない。
"""
import json
import math
import os
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


def add_source(data, name):
    """PDF・画像・その他文書のバイト列を登録し、ページ情報を返す。"""
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    if b"%PDF" in data[:1024]:
        doc = pymupdf.open(stream=data, filetype="pdf")
        if doc.needs_pass:
            raise ValueError("パスワード付きPDFは開けません（パスワードを解除してから開いてください）")
    else:
        # 画像（複数ページTIFFも可）・SVG・XPS・テキスト等 → PDFに変換
        src = pymupdf.open(stream=data, filetype=ext if ext in OTHER_TYPES else None)
        doc = pymupdf.open("pdf", src.convert_to_pdf())
    sid = uuid.uuid4().hex[:12]
    SOURCES[sid] = doc
    pages = []
    for p in doc:
        pages.append({"src": sid, "index": p.number,
                      "w": p.rect.width, "h": p.rect.height,  # 回転反映後の見た目サイズ
                      "rot": p.rotation})
    return {"id": sid, "name": name, "pages": pages}


def page_for_render(src, index, extra_rot):
    """描画用：元ページを一時文書にコピーして回転を合成。"""
    doc = SOURCES[src]
    tmp = pymupdf.open()
    tmp.insert_pdf(doc, from_page=index, to_page=index)
    p = tmp[0]
    p.set_rotation((p.rotation + extra_rot) % 360)
    return tmp, p


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
    else:
        tmp, p = page_for_render(src, int(q["index"]), rot)
    pix = p.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    return pix.tobytes("png")


def page_spans(q):
    """既存テキスト編集用：見た目座標でのテキスト行（span）一覧。"""
    src = q["src"]
    if src == "blank":
        return []
    tmp, p = page_for_render(src, int(q["index"]), int(q.get("rot", 0)))
    rot_m = p.rotation_matrix
    out = []
    d = p.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)
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
    fa = font_args(ann.get("font", "gothic"))
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
    fa = font_args(a.get("font", "gothic"))
    for text, size, base in texts:
        x = cx - text_width(text, a.get("font", "gothic"), size) / 2
        page.insert_text(pymupdf.Point(x, base) * derot, text, fontsize=size, color=col,
                         rotate=page.rotation, **fa)


def vrect(a, derot):
    return pymupdf.Rect(a["x"], a["y"], a["x"] + a["w"], a["y"] + a["h"]) * derot


def apply_annotations(page, anns):
    derot = page.derotation_matrix
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
                             color=hex2rgb(mk.get("color")), **font_args(font))
        if wm and wm.get("enabled") and wm.get("text"):
            size = float(wm.get("size", 60))
            txt = wm["text"]
            col = hex2rgb(wm.get("color"), (0.6, 0.6, 0.6))
            fa = font_args(wm.get("font", "gothic"))
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


def build_pdf(spec):
    RUN["tag"] = uuid.uuid4().hex[:6]
    out = pymupdf.open()
    for pg in spec["pages"]:
        src = pg["src"]
        rot = int(pg.get("rot", 0))
        if src == "blank":
            p = out.new_page(width=float(pg.get("w", 595)), height=float(pg.get("h", 842)))
            p.set_rotation(rot % 360)
        else:
            doc = SOURCES[src]
            out.insert_pdf(doc, from_page=pg["index"], to_page=pg["index"])
            p = out[-1]
            p.set_rotation((p.rotation + rot) % 360)
        p = out[-1]
        if pg.get("annotations"):
            apply_annotations(p, pg["annotations"])
    opts = spec.get("options", {})
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
    try:
        out.subset_fonts()
    except Exception:
        pass
    kw = dict(garbage=4, deflate=True, clean=bool(opts.get("compress")))
    if opts.get("compress"):
        kw.update(deflate_images=True, deflate_fonts=True)
    pw = opts.get("password")
    if pw:
        kw.update(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=pw, owner_pw=pw,
                  permissions=-1)
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
