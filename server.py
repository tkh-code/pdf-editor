# -*- coding: utf-8 -*-
"""PDF編集ソフト ローカルサーバー

ブラウザ画面(index.html)からの指示で PyMuPDF を使って
ページ描画・テキスト抽出・PDF書き出しを行う。
起動: python server.py [開きたいPDF ...]
"""
import base64
import io
import json
import os
import socket
import sys
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pymupdf

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")

# 画面のフォント選択肢 → Windows のフォントファイル
FONTS = {
    "gothic": "YuGothM.ttc",
    "gothic-b": "YuGothB.ttc",
    "mincho": "yumin.ttf",
    "meiryo": "meiryo.ttc",
    "msgothic": "msgothic.ttc",
    "arial": "arial.ttf",      # 欧文のみ
    "times": "times.ttf",      # 欧文のみ
}

# 読み込んだPDF・画像（id → bytes / Document）
SOURCES = {}
IMAGES = {}
LOCK = threading.Lock()


def font_file(key):
    path = os.path.join(FONT_DIR, FONTS.get(key, FONTS["gothic"]))
    if not os.path.exists(path):
        path = os.path.join(FONT_DIR, "msgothic.ttc")
    return path


def hex2rgb(h, default=(0, 0, 0)):
    if not h:
        return default
    h = h.lstrip("#")
    if len(h) != 6:
        return default
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def add_source(data, name):
    """PDF または画像のバイト列を登録し、ページ情報を返す。"""
    if data[:1024].lstrip().startswith(b"%PDF") or b"%PDF" in data[:1024]:
        doc = pymupdf.open(stream=data, filetype="pdf")
        if doc.needs_pass:
            raise ValueError("パスワード付きPDFは開けません（パスワードを解除してから開いてください）")
    else:
        # 画像ファイル → PDFに変換（複数ページTIFFも可）
        img = pymupdf.open(stream=data)
        doc = pymupdf.open("pdf", img.convert_to_pdf())
    sid = uuid.uuid4().hex[:12]
    with LOCK:
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
    src = q["src"][0]
    scale = float(q.get("scale", ["1"])[0])
    rot = int(q.get("rot", ["0"])[0])
    if src == "blank":
        w = float(q.get("w", ["595"])[0])
        h = float(q.get("h", ["842"])[0])
        tmp = pymupdf.open()
        p = tmp.new_page(width=w, height=h)
        p.set_rotation(rot % 360)
    else:
        tmp, p = page_for_render(src, int(q["index"][0]), rot)
    pix = p.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    return pix.tobytes("png")


def page_spans(q):
    """既存テキスト編集用：見た目座標でのテキスト行（span）一覧。"""
    src = q["src"][0]
    if src == "blank":
        return []
    tmp, p = page_for_render(src, int(q["index"][0]), int(q.get("rot", ["0"])[0]))
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
    ff = font_file(ann.get("font", "gothic"))
    fname = "F" + ann.get("font", "gothic").replace("-", "")
    lh = float(ann.get("lineHeight", 1.25))
    base = float(ann.get("baseline", 0.95))  # 画面で実測した「行の上端→基準線」の比
    pad = float(ann.get("pad", 0))
    x, y = ann["x"] + pad, ann["y"] + pad
    for i, line in enumerate(str(ann.get("text", "")).split("\n")):
        if not line:
            continue
        pt = pymupdf.Point(x, y + size * base + i * size * lh) * derot
        page.insert_text(pt, line, fontsize=size, fontfile=ff, fontname=fname,
                         color=color, rotate=page.rotation)


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
    for a in anns:
        t = a["type"]
        if t in ("text", "edittext", "stamp"):
            if t == "stamp":
                r = vrect(a, derot)
                page.draw_rect(r, color=hex2rgb(a.get("color"), (0.8, 0, 0)), width=1.5,
                               radius=min(3 / max(a["w"], 1), 3 / max(a["h"], 1)))
            draw_text(page, a, derot)
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


def stamp_all(out, opts):
    """ページ番号・透かし・ヘッダー等、全ページ共通の書き込み。"""
    pn = opts.get("pageNumbers")
    wm = opts.get("watermark")
    n = len(out)
    for i, page in enumerate(out):
        derot = page.derotation_matrix
        W, H = page.rect.width, page.rect.height  # 見た目サイズ
        if pn and pn.get("enabled"):
            start = int(pn.get("start", 1))
            fmt = pn.get("format", "{n}")
            if i + 1 < int(pn.get("from", 1)):
                continue_pn = False
            else:
                continue_pn = True
            if continue_pn:
                num = start + i - (int(pn.get("from", 1)) - 1)
                txt = fmt.replace("{n}", str(num)).replace("{total}", str(n))
                size = float(pn.get("size", 10))
                ff = font_file("gothic")
                tl = pymupdf.get_text_length(txt, fontsize=size)  # 概算（欧文基準）
                tl = max(tl, len(txt) * size * 0.6)
                pos = pn.get("pos", "bottom-center")
                m = float(pn.get("margin", 20))
                if pos.endswith("center"):
                    x = (W - tl) / 2
                elif pos.endswith("right"):
                    x = W - m - tl
                else:
                    x = m
                y = m + size if pos.startswith("top") else H - m
                page.insert_text(pymupdf.Point(x, y) * derot, txt, fontsize=size,
                                 fontfile=ff, fontname="Fgothic", rotate=page.rotation,
                                 color=(0, 0, 0))
        if wm and wm.get("enabled") and wm.get("text"):
            size = float(wm.get("size", 60))
            txt = wm["text"]
            col = hex2rgb(wm.get("color"), (0.6, 0.6, 0.6))
            ff = font_file(wm.get("font", "gothic"))
            # 中央に斜め45°で配置（Shape + morph で回転）
            tl = len(txt) * size
            cx, cy = W / 2, H / 2
            start = pymupdf.Point(cx - tl / 2, cy + size * 0.35)
            ang = float(wm.get("angle", 45))
            mat = pymupdf.Matrix(ang)  # 左下→右上の向き
            sh = page.new_shape()
            sh.insert_text(start * derot, txt, fontsize=size, fontfile=ff,
                           fontname="Fwm", color=col, rotate=page.rotation,
                           morph=(pymupdf.Point(cx, cy) * derot, mat),
                           fill_opacity=float(wm.get("opacity", 0.25)),
                           stroke_opacity=float(wm.get("opacity", 0.25)))
            sh.commit(overlay=True)


def build_pdf(spec):
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
    stamp_all(out, spec.get("options", {}))

    meta = spec.get("options", {}).get("meta")
    if meta:
        m = out.metadata or {}
        m.update({k: v for k, v in meta.items() if v})
        out.set_metadata(m)
    try:
        out.subset_fonts()
    except Exception:
        pass
    opts = spec.get("options", {})
    kw = dict(garbage=4, deflate=True, clean=bool(opts.get("compress")))
    if opts.get("compress"):
        kw.update(deflate_images=True, deflate_fonts=True)
    pw = opts.get("password")
    if pw:
        kw.update(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=pw, owner_pw=pw,
                  permissions=-1)
    return out.tobytes(**kw)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        headers = dict(headers or {})
        headers.setdefault("Cache-Control", "no-store")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/render":
                return self._send(200, render_png(q), "image/png",
                                  headers={"Cache-Control": "max-age=3600"})
            if u.path == "/spans":
                return self._send(200, page_spans(q))
            if u.path == "/initial":
                return self._send(200, INITIAL)
            if u.path == "/image":
                data = IMAGES.get(q["id"][0])
                if not data:
                    return self._send(404, {"error": "not found"})
                return self._send(200, data, "application/octet-stream",
                                  headers={"Cache-Control": "max-age=3600"})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        try:
            if u.path == "/upload":
                name = q.get("name", ["file"])[0]
                return self._send(200, add_source(body, name))
            if u.path == "/upload-image":
                iid = uuid.uuid4().hex[:12]
                pix = pymupdf.Pixmap(body)  # 形式チェック兼サイズ取得
                IMAGES[iid] = body
                return self._send(200, {"id": iid, "w": pix.width, "h": pix.height})
            if u.path == "/export":
                spec = json.loads(body.decode("utf-8"))
                data = build_pdf(spec)
                return self._send(200, data, "application/pdf")
            if u.path == "/export-split":
                # 指定ページ群ごとに別PDF → base64 のリストで返す
                spec = json.loads(body.decode("utf-8"))
                res = []
                for part in spec["parts"]:
                    data = build_pdf({"pages": part["pages"], "options": spec.get("options", {})})
                    res.append({"name": part["name"], "data": base64.b64encode(data).decode()})
                return self._send(200, res)
            if u.path == "/save-to":
                # 指定パスへ直接保存（ブラウザのダウンロードを使わない場合）
                path = q["path"][0]
                with open(path, "wb") as f:
                    f.write(body)
                return self._send(200, {"ok": True, "path": path})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._send(500, {"error": str(e)})


INITIAL = []


def free_port(preferred=8765):
    for port in range(preferred, preferred + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("空きポートがありません")


def main():
    args = [a for a in sys.argv[1:] if a != "--no-browser"]
    for path in args:
        if os.path.isfile(path):
            with open(path, "rb") as f:
                INITIAL.append(add_source(f.read(), os.path.basename(path)))
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"PDF編集ソフト起動中: {url}  （このウィンドウを閉じると終了します）")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
