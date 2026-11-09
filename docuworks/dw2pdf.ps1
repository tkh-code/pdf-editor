# DocuWorks → PDF 変換（PDF編集ソフト https://tkh-code.github.io/pdf-editor/ の付属ツール）
#
# DocuWorks文書（.xdw）・バインダー（.xbd）を PDF にします。DocuWorks が入っている Windows PC で動きます。
# DocuWorks 本体の部品（xdwapi.dll）でページを画像にし、それを並べた PDF を作ります。
#   ・白黒のページは 300dpi の白黒（G4 圧縮、1ページ数十KB）、色のあるページはカラー JPEG（自動判定）
#   ・ページを複数同時に変換するので、構造計算書のような数千ページの文書でも数分で終わります
#
# 使い方
#   setup.bat          : 最初に1回。PDF編集ソフトの画面から呼び出せるようにし、ショートカットも作る
#   -Serve             : PDF編集ソフトから使う変換ヘルパー（127.0.0.1 だけで待ち受け。ログオン時に自動起動）
#   ファイル・フォルダ : ショートカット「DocuWorks→PDF変換」にドロップ／右クリック「送る」でまとめて変換
param([switch]$Setup, [switch]$Serve, [Parameter(ValueFromRemainingArguments = $true)][string[]]$Paths)

$ErrorActionPreference = 'Stop'
$AppName = 'DocuWorks→PDF変換'
$Port = 52871
$Protocol = 'pdfeditor-dw'

function Find-XdwDll {
    foreach ($d in @("$env:WINDIR\System32", "$env:WINDIR\SysWOW64")) {
        if (Test-Path "$d\xdwapi.dll") { return "$d\xdwapi.dll" }
    }
    return $null
}

# ---------- 変換エンジン（C#） ----------
$CS = @'
using System;
using System.IO;
using System.IO.Compression;
using System.Text;
using System.Threading;
using System.Collections.Generic;
using System.Net;
using System.Net.Sockets;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;

public class DwOptions {
    public string Mode = "auto";   // auto（白黒/カラー自動）, mono, gray, color
    public int MonoDpi = 300;
    public int ColorDpi = 200;
    public int OverlayDpi = 150;   // 自動のとき白黒に重ねるカラー部分の解像度
    public int Threads = 0;        // 0 = CPU数に合わせる
}

public class DwJob {
    public string Id, Name, Src, Dst, Dir, State = "queued", Error = "";
    public volatile int Page, Pages, ColorPages;
    public volatile bool Cancel;
    public DwOptions Opt = new DwOptions();
    public DateTime Finished = DateTime.MaxValue;
    public DateTime Started = DateTime.Now;
    public void Run() {
        try {
            State = "running"; Started = DateTime.Now;
            DwPdf.Convert(this);
            State = "done";
        } catch (Exception e) {
            State = Cancel ? "canceled" : "error"; Error = e.Message;
            try { if (File.Exists(Dst)) File.Delete(Dst); } catch { }
        }
        Finished = DateTime.Now;
    }
}

public static class DwPdf {
    public const string Version = "4";

    [StructLayout(LayoutKind.Sequential)] struct OPEN_MODE_EX { public int nSize, nOption, nAuthMode; }
    [StructLayout(LayoutKind.Sequential)] struct DOC_INFO { public int nSize, nPages, nVersion, nOriginalData; }
    [StructLayout(LayoutKind.Sequential)] struct PAGE_INFO { public int nSize, nWidth, nHeight, nPageType, nHorRes, nVerRes, nCompressType, nAnnotations; }
    [StructLayout(LayoutKind.Sequential)] struct IMAGE_OPTION { public int nSize, nDpi, nColor, nImageType; public IntPtr pDetailOption; }

    [DllImport(@"__DLL__", CharSet = CharSet.Unicode)] static extern int XDW_OpenDocumentHandleW(string path, out IntPtr h, ref OPEN_MODE_EX mode);
    [DllImport(@"__DLL__")] static extern int XDW_CloseDocumentHandle(IntPtr h, IntPtr reserved);
    [DllImport(@"__DLL__")] static extern int XDW_GetDocumentInformation(IntPtr h, ref DOC_INFO info);
    [DllImport(@"__DLL__")] static extern int XDW_GetPageInformation(IntPtr h, int page, ref PAGE_INFO info);
    [DllImport(@"__DLL__", CharSet = CharSet.Unicode)] static extern int XDW_ConvertPageToImageFileW(IntPtr h, int page, string path, ref IMAGE_OPTION opt);

    const int IMG_TIFF = 1, IMG_JPEG = 2, COL_MONO = 0, COL_COLOR = 1, COMPRESS_G4 = 7;
    const double MaxColorPixels = 50e6, MaxMonoPixels = 150e6;   // 大判図面で重くなりすぎないよう制限

    static string Hex(int r) { return "0x" + ((uint)r).ToString("X8"); }

    static IntPtr Open(string src) {
        OPEN_MODE_EX om = new OPEN_MODE_EX(); om.nSize = Marshal.SizeOf(om);
        IntPtr h;
        int r = XDW_OpenDocumentHandleW(src, out h, ref om);
        if (r != 0) throw new Exception("DocuWorks文書を開けません（エラー " + Hex(r) + "。パスワード付き・破損・別のソフトで開いている可能性）");
        return h;
    }

    static int Render(IntPtr h, int page, string path, int dpi, int color, int type, int[] detail) {
        IMAGE_OPTION io = new IMAGE_OPTION(); io.nSize = Marshal.SizeOf(io); io.nDpi = dpi; io.nColor = color; io.nImageType = type;
        GCHandle g = new GCHandle();
        if (detail != null) { g = GCHandle.Alloc(detail, GCHandleType.Pinned); io.pDetailOption = g.AddrOfPinnedObject(); }
        try { return XDW_ConvertPageToImageFileW(h, page, path, ref io); }
        finally { if (detail != null) g.Free(); }
    }

    static byte[] ReadRgb(string jpg, out int w, out int h) {
        using (Bitmap bmp = new Bitmap(jpg)) {
            w = bmp.Width; h = bmp.Height;
            BitmapData d = bmp.LockBits(new Rectangle(0, 0, w, h), ImageLockMode.ReadOnly, PixelFormat.Format24bppRgb);
            byte[] px = new byte[(long)w * h * 3];
            try { for (int y = 0; y < h; y++) Marshal.Copy(new IntPtr(d.Scan0.ToInt64() + (long)y * d.Stride), px, y * w * 3, w * 3); }
            finally { bmp.UnlockBits(d); }
            return px;
        }
    }

    // 色の付いた画素（1）と、中間の灰色（2: 塗りつぶしの候補）を分ける
    static byte[] Classify(byte[] px, int n) {
        byte[] c = new byte[n];
        for (int i = 0, k = 0; i < n; i++, k += 3) {
            int b = px[k], g = px[k + 1], r = px[k + 2];
            int mx = Math.Max(r, Math.Max(g, b)), mn = Math.Min(r, Math.Min(g, b));
            if (mx - mn > 50 && (mx - mn) * 100 > mx * 35) c[i] = 1;   // 彩度の高い色（文字の縁の色にじみは除く）
            else { int l = (r + g + b) / 3; if (l >= 50 && l <= 246) c[i] = 2; }   // 薄い塗りも（白黒にすると黒くつぶれるため）
        }
        return c;
    }

    // a[i] == v の画素数の積分画像
    static int[] Integral(byte[] a, byte v, int w, int h) {
        int[] s = new int[(w + 1) * (h + 1)];
        for (int y = 0; y < h; y++) {
            int row = 0, o = (y + 1) * (w + 1), u = y * (w + 1);
            for (int x = 0; x < w; x++) { if (a[y * w + x] == v) row++; s[o + x + 1] = s[u + x + 1] + row; }
        }
        return s;
    }
    static int Box(int[] s, int w, int h, int x, int y, int r, out int area) {
        int x0 = Math.Max(0, x - r), y0 = Math.Max(0, y - r), x1 = Math.Min(w, x + r + 1), y1 = Math.Min(h, y + r + 1);
        area = (x1 - x0) * (y1 - y0);
        int W = w + 1;
        return s[y1 * W + x1] - s[y0 * W + x1] - s[y1 * W + x0] + s[y0 * W + x0];
    }

    // 色を残す範囲（色の付いた所＋灰色の塗りつぶし、少し広げる）を 1 にした配列。画素が無ければ null
    static byte[] ColorRegion(byte[] px, int w, int h, int fillR, int grow, out int count) {
        int n = w * h, area;
        byte[] c = Classify(px, n);
        int[] im = Integral(c, 2, w, h), ic = Integral(c, 1, w, h);
        byte[] reg = new byte[n];
        for (int y = 0; y < h; y++) for (int x = 0; x < w; x++) {
            int i = y * w + x;
            if (c[i] == 1 && Box(ic, w, h, x, y, 1, out area) * 3 >= area * 2) reg[i] = 1;   // 色の画素がまとまっている所だけ
            else if (c[i] == 2 && Box(im, w, h, x, y, fillR, out area) == area) reg[i] = 1;   // 文字の線ではなく面になっている灰色
        }
        im = Integral(reg, 1, w, h);
        count = 0;
        for (int y = 0; y < h; y++) for (int x = 0; x < w; x++) {
            int i = y * w + x;
            c[i] = (byte)(Box(im, w, h, x, y, grow, out area) > 0 ? 1 : 0);
            if (c[i] == 1) count++;
        }
        return count > 0 ? c : null;
    }

    static ImageCodecInfo jpegCodec;
    static void SaveJpeg(Bitmap b, string path, long quality) {
        if (jpegCodec == null) foreach (ImageCodecInfo ci in ImageCodecInfo.GetImageEncoders()) if (ci.MimeType == "image/jpeg") jpegCodec = ci;
        EncoderParameters ep = new EncoderParameters(1);
        ep.Param[0] = new EncoderParameter(System.Drawing.Imaging.Encoder.Quality, quality);
        b.Save(path, jpegCodec, ep);
    }

    static byte[] Zlib(byte[] data) {
        using (MemoryStream ms = new MemoryStream()) {
            ms.WriteByte(0x78); ms.WriteByte(0x9C);
            using (DeflateStream ds = new DeflateStream(ms, CompressionMode.Compress, true)) ds.Write(data, 0, data.Length);
            uint a = 1, b = 0;
            foreach (byte x in data) { a = (a + x) % 65521; b = (b + a) % 65521; }
            uint ad = (b << 16) | a;
            ms.WriteByte((byte)(ad >> 24)); ms.WriteByte((byte)(ad >> 16)); ms.WriteByte((byte)(ad >> 8)); ms.WriteByte((byte)ad);
            return ms.ToArray();
        }
    }

    // カラー画像のうち色を残す範囲だけを、白黒ページの上に重ねる画像にする（範囲外は白＋透明）
    // 戻り値：色の範囲の割合（0 = 重ねる物なし）
    static double BuildOverlay(string raw, Layer L) {
        int w, h, cnt;
        byte[] px = ReadRgb(raw, out w, out h);
        int fillR = Math.Max(2, L.Dpi / 70), grow = fillR + Math.Max(2, L.Dpi / 60);   // 塗りの判定で削れた縁も戻す
        byte[] reg = ColorRegion(px, w, h, fillR, grow, out cnt);
        if (reg == null || cnt < w * h / 3000) return 0;
        double cov = cnt / (double)(w * h);
        if (cov > 0.45) return cov;   // ほとんどカラーのページは、カラー画像をそのまま使う
        for (int i = 0, k = 0; i < reg.Length; i++, k += 3) {
            if (reg[i] == 0) { px[k] = 255; px[k + 1] = 255; px[k + 2] = 255; }
            else reg[i] = 255;
        }
        using (Bitmap o = new Bitmap(w, h, PixelFormat.Format24bppRgb)) {
            BitmapData d = o.LockBits(new Rectangle(0, 0, w, h), ImageLockMode.WriteOnly, PixelFormat.Format24bppRgb);
            try { for (int y = 0; y < h; y++) Marshal.Copy(px, y * w * 3, new IntPtr(d.Scan0.ToInt64() + (long)y * d.Stride), w * 3); }
            finally { o.UnlockBits(d); }
            SaveJpeg(o, L.Path, 72);
        }
        L.SMask = Zlib(reg); L.SW = w; L.SH = h;
        return cov;
    }

    class Layer { public string Path; public bool G4; public int Dpi; public byte[] SMask; public int SW, SH; }
    class PageImg { public List<Layer> Layers = new List<Layer>(); }

    static int CapDpi(int dpi, PAGE_INFO pi, double maxPx) {
        if (pi.nWidth <= 0 || pi.nHeight <= 0) return dpi;
        double wi = pi.nWidth / 2540.0, hi = pi.nHeight / 2540.0;   // 1/100 mm → inch
        while (wi * dpi * hi * dpi > maxPx && dpi > 100) dpi -= 25;
        return dpi;
    }

    // 1 ページを画像にする（DocuWorks の描画は並列にならないページがあるので、描画の回数をなるべく減らす）
    //   auto : 150dpi のカラーで色の範囲を調べ、白黒(G4 300dpi)＋色の部分だけのカラーにする（ほぼカラーのページはカラー画像）
    //   mono / gray / color : そのとおり
    static PageImg RenderPage(IntPtr h, int p, PAGE_INFO pi, DwOptions opt, string tmp, ref bool color) {
        PageImg im = new PageImg();
        string mode = opt.Mode;
        int r;
        if (mode == "auto") {
            Layer c = new Layer(); c.Dpi = CapDpi(Math.Min(opt.ColorDpi, opt.OverlayDpi), pi, MaxColorPixels); c.Path = Path.Combine(tmp, p + "_ov.jpg");
            string raw = Path.Combine(tmp, p + "_c.jpg");
            r = Render(h, p, raw, c.Dpi, COL_COLOR, IMG_JPEG, null);
            double cov = r == 0 ? BuildOverlay(raw, c) : 1;
            try { File.Delete(raw); } catch { }
            if (cov <= 0.45) {
                Layer g = new Layer(); g.G4 = true; g.Dpi = CapDpi(opt.MonoDpi, pi, MaxMonoPixels); g.Path = Path.Combine(tmp, p + ".tif");
                r = Render(h, p, g.Path, g.Dpi, COL_MONO, IMG_TIFF, new int[] { 12, COMPRESS_G4, 0 });
                if (r == 0) {
                    im.Layers.Add(g);
                    if (cov > 0) { im.Layers.Add(c); color = true; }
                    return im;
                }
            }
            mode = "color";   // ほとんどカラーのページ（または白黒にできないとき）はカラー画像
        }
        if (mode == "mono") {
            Layer g = new Layer(); g.G4 = true; g.Dpi = CapDpi(opt.MonoDpi, pi, MaxMonoPixels); g.Path = Path.Combine(tmp, p + ".tif");
            r = Render(h, p, g.Path, g.Dpi, COL_MONO, IMG_TIFF, new int[] { 12, COMPRESS_G4, 0 });
            if (r == 0) im.Layers.Add(g); else mode = "gray";   // 白黒にできないときはグレーで
        }
        if (mode == "gray" || mode == "color") {
            Layer f = new Layer(); f.Dpi = CapDpi(opt.ColorDpi, pi, MaxColorPixels); f.Path = Path.Combine(tmp, p + ".jpg");
            r = Render(h, p, f.Path, f.Dpi, mode == "gray" ? COL_MONO : COL_COLOR, IMG_JPEG, null);
            if (r != 0) throw new Exception(p + " ページ目を変換できません（エラー " + Hex(r) + "）");
            im.Layers.Add(f); color = mode == "color";
        }
        if (im.Layers.Count == 0) throw new Exception(p + " ページ目を変換できません");
        return im;
    }

    public static void Convert(DwJob job) {
        DwOptions opt = job.Opt;
        IntPtr h0 = Open(job.Src);
        string tmp = Path.Combine(Path.GetTempPath(), "dw2pdf_" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(tmp);
        try {
            DOC_INFO di = new DOC_INFO(); di.nSize = Marshal.SizeOf(di);
            XDW_GetDocumentInformation(h0, ref di);
            int n = di.nPages;
            if (n <= 0) throw new Exception("ページがありません");
            string lim = Environment.GetEnvironmentVariable("DW2PDF_MAXPAGES");   // 試験用
            int li; if (lim != null && int.TryParse(lim, out li) && li > 0 && li < n) n = li;
            job.Pages = n;
            PAGE_INFO[] info = new PAGE_INFO[n];
            for (int i = 0; i < n; i++) { info[i].nSize = Marshal.SizeOf(typeof(PAGE_INFO)); XDW_GetPageInformation(h0, i + 1, ref info[i]); }

            PageImg[] imgs = new PageImg[n];
            int next = 0, done = 0, colorPages = 0;
            Exception err = null;
            int cores = Environment.ProcessorCount;
            int threads = opt.Threads > 0 ? opt.Threads : (Environment.Is64BitProcess ? Math.Min(Math.Max(cores - 1, 1), 12) : Math.Min(cores, 4));
            threads = Math.Max(1, Math.Min(threads, n));
            List<Thread> ws = new List<Thread>();
            for (int t = 0; t < threads; t++) {
                Thread th = new Thread(() => {
                    IntPtr h = IntPtr.Zero;
                    try {
                        h = Open(job.Src);
                        while (true) {
                            int p = Interlocked.Increment(ref next);
                            if (p > n || err != null || job.Cancel) break;
                            bool color = false;
                            imgs[p - 1] = RenderPage(h, p, info[p - 1], opt, tmp, ref color);
                            if (color) Interlocked.Increment(ref colorPages);
                            job.ColorPages = colorPages;
                            job.Page = Interlocked.Increment(ref done);
                        }
                    } catch (Exception e) { if (err == null) err = e; }
                    finally { if (h != IntPtr.Zero) XDW_CloseDocumentHandle(h, IntPtr.Zero); }
                }, 16 << 20);
                th.IsBackground = true; th.Start(); ws.Add(th);
            }
            foreach (Thread th in ws) th.Join();
            if (job.Cancel) throw new Exception("中止しました");
            if (err != null) throw err;
            WritePdf(job.Dst, imgs, Path.GetFileNameWithoutExtension(job.Name ?? job.Src));
        } finally {
            XDW_CloseDocumentHandle(h0, IntPtr.Zero);
            try { Directory.Delete(tmp, true); } catch { }
        }
    }

    // ---- 画像ファイルの読み取り ----
    static void JpegInfo(string path, out int w, out int hgt, out int comps) {
        using (FileStream f = File.OpenRead(path)) {
            BinaryReader b = new BinaryReader(f);
            if (b.ReadByte() != 0xFF || b.ReadByte() != 0xD8) throw new Exception("JPEG ではありません");
            while (true) {
                int m; do { m = b.ReadByte(); } while (m != 0xFF);
                do { m = b.ReadByte(); } while (m == 0xFF);
                int len = (b.ReadByte() << 8) | b.ReadByte();
                if (m >= 0xC0 && m <= 0xCF && m != 0xC4 && m != 0xC8 && m != 0xCC) {
                    b.ReadByte();
                    hgt = (b.ReadByte() << 8) | b.ReadByte();
                    w = (b.ReadByte() << 8) | b.ReadByte();
                    comps = b.ReadByte();
                    return;
                }
                f.Seek(len - 2, SeekOrigin.Current);
            }
        }
    }

    class Tiff { public int W, H, RowsPerStrip, Compression = 1, FillOrder = 1, Photometric = 0; public long[] Offsets, Counts; }

    static Tiff ReadTiff(string path) {
        byte[] d = File.ReadAllBytes(path);
        bool le = d[0] == 0x49;
        Func<long, int> U16 = o => le ? (d[o] | d[o + 1] << 8) : (d[o] << 8 | d[o + 1]);
        Func<long, long> U32 = o => le ? (long)(uint)(d[o] | d[o + 1] << 8 | d[o + 2] << 16 | d[o + 3] << 24) : (long)(uint)(d[o] << 24 | d[o + 1] << 16 | d[o + 2] << 8 | d[o + 3]);
        Tiff t = new Tiff();
        long ifd = U32(4);
        int cnt = U16(ifd);
        for (int i = 0; i < cnt; i++) {
            long e = ifd + 2 + i * 12;
            int tag = U16(e), type = U16(e + 2); long num = U32(e + 4);
            int size = type == 3 ? 2 : 4;
            long vo = num * size <= 4 ? e + 8 : U32(e + 8);
            long[] vals = new long[num];
            for (long k = 0; k < num; k++) vals[k] = size == 2 ? U16(vo + k * 2) : U32(vo + k * 4);
            switch (tag) {
                case 256: t.W = (int)vals[0]; break;
                case 257: t.H = (int)vals[0]; break;
                case 259: t.Compression = (int)vals[0]; break;
                case 262: t.Photometric = (int)vals[0]; break;
                case 266: t.FillOrder = (int)vals[0]; break;
                case 273: t.Offsets = vals; break;
                case 278: t.RowsPerStrip = (int)vals[0]; break;
                case 279: t.Counts = vals; break;
            }
        }
        if (t.RowsPerStrip <= 0 || t.RowsPerStrip > t.H) t.RowsPerStrip = t.H;
        if (t.Compression != 4 || t.FillOrder != 1 || t.Offsets == null || t.Counts == null) throw new Exception("想定外の TIFF 形式です");
        return t;
    }

    static string Num(double v) { return v.ToString("0.###", System.Globalization.CultureInfo.InvariantCulture); }

    // ---- PDF の書き出し（画像を 1 ページ 1 枚で並べる） ----
    static void WritePdf(string dst, PageImg[] imgs, string title) {
        Dictionary<int, long> offs = new Dictionary<int, long>();
        int nextId = 4;   // 1 Catalog, 2 Pages, 3 Info
        List<int> pageIds = new List<int>();
        using (FileStream f = new FileStream(dst, FileMode.CreateNew, FileAccess.Write, FileShare.None, 1 << 20)) {
            Action<string> W = s => { byte[] bb = Encoding.ASCII.GetBytes(s); f.Write(bb, 0, bb.Length); };
            Action<int> Obj = id => { offs[id] = f.Position; W(id + " 0 obj\n"); };
            Action<string, long, long> Copy = (path, off, len) => {
                using (FileStream s = File.OpenRead(path)) {
                    s.Seek(off, SeekOrigin.Begin);
                    byte[] buf = new byte[1 << 16]; long left = len;
                    while (left > 0) { int k = s.Read(buf, 0, (int)Math.Min(buf.Length, left)); if (k <= 0) break; f.Write(buf, 0, k); left -= k; }
                }
            };
            W("%PDF-1.4\n"); f.Write(new byte[] { 0x25, 0xE2, 0xE3, 0xCF, 0xD3, 0x0A }, 0, 6);
            Obj(1); W("<< /Type /Catalog /Pages 2 0 R >>\nendobj\n");
            StringBuilder t = new StringBuilder("FEFF");
            foreach (char c in title) t.Append(((int)c).ToString("X4"));
            Obj(3); W("<< /Title <" + t + "> /Producer (DocuWorks to PDF / pdf-editor) >>\nendobj\n");
            foreach (PageImg im in imgs) {
                int pid = nextId++;
                pageIds.Add(pid);
                StringBuilder content = new StringBuilder(), xobj = new StringBuilder();
                double pw = 0, ph = 0;
                List<Action> pending = new List<Action>();
                for (int li = 0; li < im.Layers.Count; li++) {
                    Layer L = im.Layers[li];
                    string path = L.Path;
                    if (L.G4) {
                        Tiff tf = ReadTiff(path);
                        if (li == 0) { pw = tf.W * 72.0 / L.Dpi; ph = tf.H * 72.0 / L.Dpi; }
                        double sy = ph / tf.H;
                        for (int k = 0; k < tf.Offsets.Length; k++) {
                            int rows = Math.Min(tf.RowsPerStrip, tf.H - k * tf.RowsPerStrip);
                            if (rows <= 0) break;
                            int iid = nextId++;
                            double y0 = ph - (k * tf.RowsPerStrip + rows) * sy, hh = rows * sy;
                            string nm = "/I" + li + "_" + k;
                            xobj.Append(nm + " " + iid + " 0 R ");
                            content.Append("q " + Num(pw) + " 0 0 " + Num(hh) + " 0 " + Num(y0) + " cm " + nm + " Do Q\n");
                            long off = tf.Offsets[k], len = tf.Counts[k]; int w = tf.W; bool blackIs1 = tf.Photometric == 1;
                            pending.Add(() => {
                                Obj(iid); W("<< /Type /XObject /Subtype /Image /Width " + w + " /Height " + rows + " /ColorSpace /DeviceGray /BitsPerComponent 1 /Filter /CCITTFaxDecode /DecodeParms << /K -1 /Columns " + w + " /Rows " + rows + " /BlackIs1 " + (blackIs1 ? "true" : "false") + " >> /Length " + len + " >>\nstream\n");
                                Copy(path, off, len); W("\nendstream\nendobj\n");
                            });
                        }
                    } else {
                        int w, hgt, comps; JpegInfo(path, out w, out hgt, out comps);
                        if (li == 0) { pw = w * 72.0 / L.Dpi; ph = hgt * 72.0 / L.Dpi; }
                        int iid = nextId++, sid = L.SMask != null ? nextId++ : 0;
                        string nm = "/I" + li;
                        xobj.Append(nm + " " + iid + " 0 R ");
                        content.Append("q " + Num(pw) + " 0 0 " + Num(ph) + " 0 0 cm " + nm + " Do Q\n");
                        string cs = comps == 1 ? "/DeviceGray" : comps == 4 ? "/DeviceCMYK /Decode [1 0 1 0 1 0 1 0]" : "/DeviceRGB";
                        long len = new FileInfo(path).Length;
                        byte[] sm = L.SMask; int sw = L.SW, sh = L.SH;
                        pending.Add(() => {
                            Obj(iid); W("<< /Type /XObject /Subtype /Image /Width " + w + " /Height " + hgt + " /ColorSpace " + cs + " /BitsPerComponent 8 /Filter /DCTDecode" + (sm != null ? " /SMask " + sid + " 0 R" : "") + " /Length " + len + " >>\nstream\n");
                            Copy(path, 0, len); W("\nendstream\nendobj\n");
                            if (sm != null) {
                                Obj(sid); W("<< /Type /XObject /Subtype /Image /Width " + sw + " /Height " + sh + " /ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode /Length " + sm.Length + " >>\nstream\n");
                                f.Write(sm, 0, sm.Length); W("\nendstream\nendobj\n");
                            }
                        });
                    }
                }
                int cid = nextId++;
                Obj(pid); W("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 " + Num(pw) + " " + Num(ph) + "] /Resources << /XObject << " + xobj + ">> >> /Contents " + cid + " 0 R >>\nendobj\n");
                foreach (Action a in pending) a();
                string cs2 = content.ToString();
                Obj(cid); W("<< /Length " + cs2.Length + " >>\nstream\n" + cs2 + "endstream\nendobj\n");
                im.Layers.ForEach(x => x.SMask = null);   // メモリを早めに返す
            }
            StringBuilder kids = new StringBuilder();
            foreach (int id in pageIds) kids.Append(id + " 0 R ");
            Obj(2); W("<< /Type /Pages /Count " + pageIds.Count + " /Kids [ " + kids + "] >>\nendobj\n");
            long xref = f.Position;
            int size = nextId;
            W("xref\n0 " + size + "\n0000000000 65535 f \n");
            for (int id = 1; id < size; id++) W((offs.ContainsKey(id) ? offs[id] : 0).ToString("D10") + (offs.ContainsKey(id) ? " 00000 n \n" : " 00000 f \n"));
            W("trailer\n<< /Size " + size + " /Root 1 0 R /Info 3 0 R >>\nstartxref\n" + xref + "\n%%EOF\n");
        }
    }
}

// ---- PDF編集ソフトから呼ぶための小さな HTTP サーバー（127.0.0.1 のみ） ----
public static class DwServer {
    static Dictionary<string, DwJob> jobs = new Dictionary<string, DwJob>();
    static object lk = new object();
    static object runLock = new object();
    static DateTime last = DateTime.Now;
    static string root;

    static bool Allowed(string origin) {
        if (string.IsNullOrEmpty(origin)) return true;
        return origin == "https://tkh-code.github.io" || origin.StartsWith("http://localhost:") || origin.StartsWith("http://127.0.0.1:")
            || origin == "http://localhost" || origin == "http://127.0.0.1";
    }

    public static void Run(int port, int idleMinutes, string tempRoot) {
        root = tempRoot; Directory.CreateDirectory(root);
        TcpListener l = new TcpListener(IPAddress.Loopback, port);
        l.Start();
        while (true) {
            if (l.Pending()) {
                TcpClient c = l.AcceptTcpClient();
                ThreadPool.QueueUserWorkItem(o => { try { Handle((TcpClient)o); } catch { } }, c);
                continue;
            }
            Thread.Sleep(50);
            bool active = false;
            lock (lk) {
                List<string> old = new List<string>();
                foreach (KeyValuePair<string, DwJob> kv in jobs) {
                    if (kv.Value.State == "queued" || kv.Value.State == "running") active = true;
                    else if ((DateTime.Now - kv.Value.Finished).TotalMinutes > 30) old.Add(kv.Key);
                }
                foreach (string k in old) Remove(k);
            }
            if (idleMinutes > 0 && !active && (DateTime.Now - last).TotalMinutes > idleMinutes) break;
        }
        l.Stop();
    }

    static void Remove(string id) {
        DwJob j; if (!jobs.TryGetValue(id, out j)) return;
        jobs.Remove(id);
        try { Directory.Delete(j.Dir, true); } catch { }
    }

    static string Js(string s) {
        if (s == null) return "null";
        StringBuilder b = new StringBuilder("\"");
        foreach (char c in s) {
            if (c == '"' || c == '\\') b.Append('\\').Append(c);
            else if (c < 0x20) b.Append("\\u" + ((int)c).ToString("x4"));
            else b.Append(c);
        }
        return b.Append('"').ToString();
    }

    static void Send(Stream s, int code, string origin, string type, byte[] body, string file) {
        long len = file != null ? new FileInfo(file).Length : (body != null ? body.Length : 0);
        StringBuilder h = new StringBuilder();
        h.Append("HTTP/1.1 " + code + " " + (code == 200 ? "OK" : code == 204 ? "No Content" : code == 404 ? "Not Found" : code == 403 ? "Forbidden" : "Error") + "\r\n");
        if (!string.IsNullOrEmpty(origin)) {
            h.Append("Access-Control-Allow-Origin: " + origin + "\r\nVary: Origin\r\n");
            h.Append("Access-Control-Allow-Methods: GET, POST, OPTIONS\r\nAccess-Control-Allow-Headers: Content-Type\r\n");
            h.Append("Access-Control-Allow-Private-Network: true\r\nAccess-Control-Max-Age: 600\r\n");
        }
        h.Append("Cache-Control: no-store\r\nConnection: close\r\n");
        if (type != null) h.Append("Content-Type: " + type + "\r\n");
        h.Append("Content-Length: " + len + "\r\n\r\n");
        byte[] hb = Encoding.ASCII.GetBytes(h.ToString());
        s.Write(hb, 0, hb.Length);
        if (file != null) using (FileStream fs = File.OpenRead(file)) fs.CopyTo(s, 1 << 20);
        else if (body != null) s.Write(body, 0, body.Length);
        s.Flush();
    }

    static void Json(Stream s, int code, string origin, string json) { Send(s, code, origin, "application/json; charset=utf-8", Encoding.UTF8.GetBytes(json), null); }

    static void Handle(TcpClient c) {
        using (c) {
            NetworkStream s = c.GetStream();
            s.ReadTimeout = 60000;
            // ヘッダーを読む
            List<byte> hb = new List<byte>();
            while (hb.Count < 65536) {
                int b = s.ReadByte(); if (b < 0) return;
                hb.Add((byte)b);
                int n = hb.Count;
                if (n >= 4 && hb[n - 4] == 13 && hb[n - 3] == 10 && hb[n - 2] == 13 && hb[n - 1] == 10) break;
            }
            string[] lines = Encoding.ASCII.GetString(hb.ToArray()).Split(new string[] { "\r\n" }, StringSplitOptions.None);
            string[] rl = lines[0].Split(' ');
            if (rl.Length < 2) return;
            string method = rl[0], target = rl[1];
            Dictionary<string, string> hd = new Dictionary<string, string>();
            for (int i = 1; i < lines.Length; i++) { int k = lines[i].IndexOf(':'); if (k > 0) hd[lines[i].Substring(0, k).Trim().ToLowerInvariant()] = lines[i].Substring(k + 1).Trim(); }
            string origin; hd.TryGetValue("origin", out origin);
            if (!Allowed(origin)) { Json(s, 403, null, "{\"error\":\"forbidden\"}"); return; }
            last = DateTime.Now;
            if (method == "OPTIONS") { Send(s, 204, origin, null, null, null); return; }
            string path = target, qs = "";
            int qi = target.IndexOf('?'); if (qi >= 0) { path = target.Substring(0, qi); qs = target.Substring(qi + 1); }
            Dictionary<string, string> q = new Dictionary<string, string>();
            foreach (string kv in qs.Split('&')) { if (kv.Length == 0) continue; int e = kv.IndexOf('='); string k = e < 0 ? kv : kv.Substring(0, e), v = e < 0 ? "" : kv.Substring(e + 1); q[Uri.UnescapeDataString(k)] = Uri.UnescapeDataString(v.Replace('+', ' ')); }
            string id; q.TryGetValue("id", out id);
            DwJob job = null;
            if (id != null) lock (lk) jobs.TryGetValue(id, out job);

            if (path == "/ping") {
                Json(s, 200, origin, "{\"ok\":true,\"version\":" + Js(DwPdf.Version) + ",\"threads\":" + Math.Min(Math.Max(Environment.ProcessorCount - 1, 1), 12) + "}");
            } else if (path == "/convert" && method == "POST") {
                string name; if (!q.TryGetValue("name", out name) || name == "") name = "document.xdw";
                string ext = Path.GetExtension(name).ToLowerInvariant(); if (ext != ".xbd") ext = ".xdw";
                long len = 0; string cl; if (hd.TryGetValue("content-length", out cl)) long.TryParse(cl, out len);
                if (len <= 0) { Json(s, 400, origin, "{\"error\":\"ファイルが空です\"}"); return; }
                DwJob j = new DwJob();
                j.Id = Guid.NewGuid().ToString("N"); j.Name = name;
                j.Dir = Path.Combine(root, j.Id); Directory.CreateDirectory(j.Dir);
                j.Src = Path.Combine(j.Dir, "in" + ext); j.Dst = Path.Combine(j.Dir, "out.pdf");
                using (FileStream fs = new FileStream(j.Src, FileMode.CreateNew)) {
                    byte[] buf = new byte[1 << 16]; long left = len;
                    while (left > 0) { int k = s.Read(buf, 0, (int)Math.Min(buf.Length, left)); if (k <= 0) break; fs.Write(buf, 0, k); left -= k; }
                    if (left > 0) { fs.Close(); try { Directory.Delete(j.Dir, true); } catch { } return; }
                }
                string v;
                if (q.TryGetValue("mode", out v) && (v == "auto" || v == "mono" || v == "gray" || v == "color")) j.Opt.Mode = v;
                int dpi; if (q.TryGetValue("color", out v) && int.TryParse(v, out dpi) && dpi >= 72 && dpi <= 600) j.Opt.ColorDpi = dpi;
                if (q.TryGetValue("mono", out v) && int.TryParse(v, out dpi) && dpi >= 100 && dpi <= 600) j.Opt.MonoDpi = dpi;
                lock (lk) jobs[j.Id] = j;
                Thread th = new Thread(() => { lock (runLock) { if (!j.Cancel) j.Run(); else { j.State = "canceled"; j.Finished = DateTime.Now; } } });
                th.IsBackground = true; th.Start();
                Json(s, 200, origin, "{\"id\":" + Js(j.Id) + "}");
            } else if (path == "/status" && job != null) {
                long size = job.State == "done" && File.Exists(job.Dst) ? new FileInfo(job.Dst).Length : 0;
                Json(s, 200, origin, "{\"state\":" + Js(job.State) + ",\"page\":" + job.Page + ",\"pages\":" + job.Pages + ",\"colorPages\":" + job.ColorPages
                    + ",\"seconds\":" + (int)(DateTime.Now - job.Started).TotalSeconds + ",\"size\":" + size + ",\"error\":" + Js(job.Error) + "}");
            } else if (path == "/result" && job != null && job.State == "done") {
                Send(s, 200, origin, "application/pdf", null, job.Dst);
                lock (lk) Remove(job.Id);
            } else if (path == "/cancel" && job != null) {
                job.Cancel = true;
                Json(s, 200, origin, "{\"ok\":true}");
            } else {
                Json(s, 404, origin, "{\"error\":\"not found\"}");
            }
        }
    }
}
'@

function Load-Engine($dll) {
    Add-Type -TypeDefinition $CS.Replace('__DLL__', $dll) -Language CSharp -ReferencedAssemblies System.Drawing
}

# ---------- セットアップ ----------
if ($Setup) {
    Write-Host "$AppName のセットアップ"
    Write-Host ''
    if (-not (Find-XdwDll)) { Write-Host '※ このPCには DocuWorks が見つかりません。DocuWorks が入ったPCで使ってください。'; Write-Host '' }
    # 64bit 用の部品が無く 32bit 用だけのPCでは 32bit の PowerShell で動かす
    $ps = "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
    if ([Environment]::Is64BitOperatingSystem -and -not (Test-Path "$env:WINDIR\System32\xdwapi.dll") -and (Test-Path "$env:WINDIR\SysWOW64\xdwapi.dll")) {
        $ps = "$env:WINDIR\SysWOW64\WindowsPowerShell\v1.0\powershell.exe"
    }
    $dest = Join-Path $env:LOCALAPPDATA 'DocuWorksPDF'
    New-Item -ItemType Directory -Force $dest | Out-Null
    # 動いているヘルパーがあれば止めてから入れ替える
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*DocuWorksPDF\dw2pdf.ps1*-Serve*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    $script = Join-Path $dest 'dw2pdf.ps1'
    Copy-Item -LiteralPath $PSCommandPath -Destination $script -Force
    # ヘルパーを黒い画面なしで起動する VBS（パスに日本語があってもよいよう UTF-16 で保存）
    $vbs = Join-Path $dest 'start_helper.vbs'
    $line = 'CreateObject("WScript.Shell").Run """' + $ps + '"" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""' + $script + '"" -Serve", 0, False'
    [IO.File]::WriteAllText($vbs, $line, [Text.Encoding]::Unicode)
    Get-ChildItem $dest | Unblock-File -ErrorAction SilentlyContinue
    # PDF編集ソフトの画面から起動できるようにする（pdfeditor-dw:// を登録。管理者権限は不要）
    $key = "HKCU:\Software\Classes\$Protocol"
    New-Item -Path "$key\shell\open\command" -Force | Out-Null
    Set-ItemProperty -Path $key -Name '(default)' -Value 'URL:PDF編集ソフト DocuWorks変換'
    Set-ItemProperty -Path $key -Name 'URL Protocol' -Value ''
    Set-ItemProperty -Path "$key\shell\open\command" -Name '(default)' -Value ('"' + "$env:WINDIR\System32\wscript.exe" + '" "' + $vbs + '"')
    # ドロップ・送る 用のショートカット
    $sh = New-Object -ComObject WScript.Shell
    foreach ($dir in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('SendTo'))) {
        $lnk = $sh.CreateShortcut((Join-Path $dir "$AppName.lnk"))
        $lnk.TargetPath = $ps
        $lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
        $lnk.WorkingDirectory = $dest
        $lnk.IconLocation = "$env:WINDIR\System32\imageres.dll,-68"
        $lnk.Description = 'DocuWorks文書・バインダーをPDFに変換（ドロップ）'
        $lnk.Save()
    }
    # ログオン時に変換ヘルパーを自動で起動（スタートアップ）
    $st = $sh.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Startup')) 'PDF編集ソフト_DocuWorks変換.lnk'))
    $st.TargetPath = "$env:WINDIR\System32\wscript.exe"
    $st.Arguments = "`"$vbs`""
    $st.WorkingDirectory = $dest
    $st.Description = 'PDF編集ソフトの DocuWorks→PDF 変換ヘルパー'
    $st.Save()
    Start-Process -FilePath "$env:WINDIR\System32\wscript.exe" -ArgumentList "`"$vbs`""
    Write-Host '準備ができました。'
    Write-Host '  ・PDF編集ソフト（https://tkh-code.github.io/pdf-editor/）に .xdw / .xbd をドロップすると、その場で PDF にして開きます'
    Write-Host '  ・デスクトップの「DocuWorks→PDF変換」にドロップ／右クリック「送る」でも、まとめて PDF にできます'
    Write-Host ''
    Write-Host "（本体の保存先: $dest）"
    if (-not $env:DW2PDF_NOPAUSE) { Read-Host "`nEnter キーで閉じます" }
    exit 0
}

$dllPath = Find-XdwDll

# ---------- 変換ヘルパー（PDF編集ソフトから使う） ----------
if ($Serve) {
    if (-not $dllPath) { exit 1 }
    Load-Engine $dllPath
    try { [DwServer]::Run($Port, 0, (Join-Path $env:TEMP 'dw2pdf_helper')) } catch { }   # 既に起動していればそのまま終わる
    exit 0
}

# ---------- ドロップ・送る でまとめて変換 ----------
function Get-UniquePath([string]$p) {
    if (-not (Test-Path -LiteralPath $p)) { return $p }
    $base = [IO.Path]::Combine([IO.Path]::GetDirectoryName($p), [IO.Path]::GetFileNameWithoutExtension($p))
    $ext = [IO.Path]::GetExtension($p)
    $i = 2
    while (Test-Path -LiteralPath "$base ($i)$ext") { $i++ }
    return "$base ($i)$ext"
}

function Get-Targets([string[]]$items) {
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($p in $items) {
        if (-not $p) { continue }
        if (Test-Path -LiteralPath $p -PathType Container) {
            # フォルダは中を全部。すでに同じ名前の PDF があるものは飛ばす
            Get-ChildItem -LiteralPath $p -Recurse -File | Where-Object { $_.Extension -match '^\.(xdw|xbd)$' } | Sort-Object FullName | ForEach-Object {
                if (-not (Test-Path -LiteralPath ([IO.Path]::ChangeExtension($_.FullName, '.pdf')))) { $out.Add($_.FullName) }
            }
        } elseif ((Test-Path -LiteralPath $p -PathType Leaf) -and ([IO.Path]::GetExtension($p) -match '^\.(xdw|xbd)$')) {
            $out.Add((Resolve-Path -LiteralPath $p).ProviderPath)
        }
    }
    return ,([string[]]($out | Select-Object -Unique))
}

$exitCode = 1
try {
    Write-Host "$AppName"
    Write-Host ''
    if (-not $dllPath) { throw 'DocuWorks が見つかりません（このPCに DocuWorks をインストールしてください）' }
    if (-not $Paths -or $Paths.Count -eq 0) {
        Add-Type -AssemblyName System.Windows.Forms
        $dlg = New-Object System.Windows.Forms.OpenFileDialog
        $dlg.Title = 'PDFにする DocuWorks 文書・バインダーを選んでください'
        $dlg.Filter = 'DocuWorks文書・バインダー (*.xdw;*.xbd)|*.xdw;*.xbd|すべて (*.*)|*.*'
        $dlg.Multiselect = $true
        if ($dlg.ShowDialog() -ne 'OK') { exit 0 }
        $Paths = $dlg.FileNames
    }
    $files = Get-Targets $Paths
    if (-not $files -or $files.Count -eq 0) { throw '変換できるファイル（.xdw / .xbd）がありませんでした' }
    try { Load-Engine $dllPath } catch {
        if (([Environment]::Is64BitProcess) -and -not (Test-Path "$env:WINDIR\System32\xdwapi.dll")) { throw '64bit 用の DocuWorks 部品がありません。setup.bat をもう一度実行してショートカットを作り直してください' }
        throw
    }
    $ok = 0; $ng = 0
    for ($k = 0; $k -lt $files.Count; $k++) {
        $job = New-Object DwJob
        $job.Src = $files[$k]; $job.Name = [IO.Path]::GetFileName($job.Src)
        $job.Dst = Get-UniquePath ([IO.Path]::ChangeExtension($job.Src, '.pdf'))
        Write-Host ("[{0}/{1}] {2}" -f ($k + 1), $files.Count, $job.Name)
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $th = New-Object Threading.Thread ([Threading.ThreadStart] [Delegate]::CreateDelegate([Threading.ThreadStart], $job, 'Run'))
        $th.Start()
        while ($th.IsAlive) { Start-Sleep -Milliseconds 300; if ($job.Pages) { Write-Host -NoNewline ("`r    {0} / {1} ページ" -f $job.Page, $job.Pages) } }
        if ($job.State -eq 'done') {
            Write-Host ("`r    → {0}（{1} ページ、うちカラー {2}、{3:0} 秒）          " -f [IO.Path]::GetFileName($job.Dst), $job.Pages, $job.ColorPages, $sw.Elapsed.TotalSeconds)
            $ok++
        } else {
            Write-Host "`r    × 失敗：$($job.Error)                    "
            $ng++
        }
    }
    Write-Host ''
    Write-Host "完了：成功 $ok 件 / 失敗 $ng 件"
    Write-Host 'PDF は元のファイルと同じフォルダにできています（元のファイルはそのまま）。'
    Write-Host '※ ページを画像にして変換しているため、PDF の文字は選択・検索できません。'
    $exitCode = if ($ng) { 2 } else { 0 }
} catch {
    Write-Host "エラー：$($_.Exception.Message)"
}
if (-not $env:DW2PDF_NOPAUSE) { Read-Host "`nEnter キーで閉じます" }
exit $exitCode
