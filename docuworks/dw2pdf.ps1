# DocuWorks → PDF 変換（PDF編集ソフト https://tkh-code.github.io/pdf-editor/ の付属ツール）
#
# DocuWorks文書（.xdw）・バインダー（.xbd）を PDF にします。
# DocuWorks 本体の部品（xdwapi.dll）でページを画像（JPEG 300dpi）にし、それを並べた PDF を作ります。
# Python などのインストールは不要。DocuWorks が入っている Windows PC で動きます。
#
#   ショートカット「DocuWorks→PDF変換」にファイル・フォルダをドロップ／右クリック「送る」
#   引数なしで起動するとファイルを選ぶ画面が出ます。
#   -Setup  : ショートカット（デスクトップ・送る）を作る（setup.bat から呼ぶ）
param([switch]$Setup, [Parameter(ValueFromRemainingArguments = $true)][string[]]$Paths)

$ErrorActionPreference = 'Stop'
$AppName = 'DocuWorks→PDF変換'

function Find-XdwDll {
    foreach ($d in @("$env:WINDIR\System32", "$env:WINDIR\SysWOW64")) {
        if (Test-Path "$d\xdwapi.dll") { return "$d\xdwapi.dll" }
    }
    return $null
}

# ---------- セットアップ（ショートカット作成） ----------
if ($Setup) {
    Write-Host "$AppName のセットアップ"
    Write-Host ''
    $dll = Find-XdwDll
    if (-not $dll) { Write-Host '※ このPCには DocuWorks が見つかりません。DocuWorks が入ったPCで使ってください。' }
    # 64bit 用の部品が無く 32bit 用だけのPCでは 32bit の PowerShell で動かす
    $ps = "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
    if ([Environment]::Is64BitOperatingSystem -and -not (Test-Path "$env:WINDIR\System32\xdwapi.dll") -and (Test-Path "$env:WINDIR\SysWOW64\xdwapi.dll")) {
        $ps = "$env:WINDIR\SysWOW64\WindowsPowerShell\v1.0\powershell.exe"
    }
    $dest = Join-Path $env:LOCALAPPDATA 'DocuWorksPDF'
    New-Item -ItemType Directory -Force $dest | Out-Null
    Copy-Item -LiteralPath $PSCommandPath -Destination (Join-Path $dest 'dw2pdf.ps1') -Force
    Get-ChildItem $dest | Unblock-File -ErrorAction SilentlyContinue
    $script = Join-Path $dest 'dw2pdf.ps1'
    $sh = New-Object -ComObject WScript.Shell
    $made = @()
    foreach ($dir in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('SendTo'))) {
        $lnk = $sh.CreateShortcut((Join-Path $dir "$AppName.lnk"))
        $lnk.TargetPath = $ps
        $lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
        $lnk.WorkingDirectory = $dest
        $lnk.IconLocation = "$env:WINDIR\System32\imageres.dll,-68"
        $lnk.Description = 'DocuWorks文書・バインダーをPDFに変換（ドロップ）'
        $lnk.Save()
        $made += $dir
    }
    Write-Host '次の場所にショートカット「DocuWorks→PDF変換」を作りました。'
    Write-Host "  ・デスクトップ（ファイルやフォルダをドロップ）"
    Write-Host "  ・右クリックの「送る」"
    Write-Host ''
    Write-Host "（本体の保存先: $dest。このフォルダとショートカットを消せばアンインストールです）"
    Read-Host "`nEnter キーで閉じます"
    exit 0
}

# ---------- 変換本体 ----------
$dllPath = Find-XdwDll
$code = @"
using System;
using System.IO;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public static class Dw2Pdf {
    [StructLayout(LayoutKind.Sequential)] struct OPEN_MODE_EX { public int nSize, nOption, nAuthMode; }
    [StructLayout(LayoutKind.Sequential)] struct DOC_INFO { public int nSize, nPages, nVersion, nOriginalData; }
    [StructLayout(LayoutKind.Sequential)] struct PAGE_INFO { public int nSize, nWidth, nHeight, nPageType, nHorRes, nVerRes, nCompressType, nAnnotations; }
    [StructLayout(LayoutKind.Sequential)] struct IMAGE_OPTION { public int nSize, nDpi, nColor, nImageType; public IntPtr pDetailOption; }

    [DllImport(@"$dllPath", CharSet = CharSet.Unicode)] static extern int XDW_OpenDocumentHandleW(string path, out IntPtr h, ref OPEN_MODE_EX mode);
    [DllImport(@"$dllPath")] static extern int XDW_CloseDocumentHandle(IntPtr h, IntPtr reserved);
    [DllImport(@"$dllPath")] static extern int XDW_GetDocumentInformation(IntPtr h, ref DOC_INFO info);
    [DllImport(@"$dllPath")] static extern int XDW_GetPageInformation(IntPtr h, int page, ref PAGE_INFO info);
    [DllImport(@"$dllPath", CharSet = CharSet.Unicode)] static extern int XDW_ConvertPageToImageFileW(IntPtr h, int page, string path, ref IMAGE_OPTION opt);

    const int IMAGE_COLOR = 1, IMAGE_JPEG = 2;
    public static int Dpi = 300;
    public static long MaxPixels = 50000000;   // 大判図面で重くなりすぎないよう 1ページの画素数を制限

    static string Hex(uint r) { return "0x" + r.ToString("X8"); }

    // 変換してページ数を返す。progress(i, n) で進み具合を知らせる
    public static int Convert(string src, string dst, Action<int, int> progress) {
        OPEN_MODE_EX om = new OPEN_MODE_EX(); om.nSize = Marshal.SizeOf(om);
        IntPtr h;
        int r = XDW_OpenDocumentHandleW(src, out h, ref om);
        if (r != 0) throw new Exception("DocuWorks文書を開けません（エラー " + Hex((uint)r) + "。パスワード付き・破損・別のソフトで開いている可能性）");
        string tmp = Path.Combine(Path.GetTempPath(), "dw2pdf_" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(tmp);
        try {
            DOC_INFO di = new DOC_INFO(); di.nSize = Marshal.SizeOf(di);
            XDW_GetDocumentInformation(h, ref di);
            int n = di.nPages;
            if (n <= 0) throw new Exception("ページがありません");
            List<string> jpgs = new List<string>(); List<int> dpis = new List<int>();
            for (int i = 1; i <= n; i++) {
                if (progress != null) progress(i, n);
                int dpi = Dpi;
                PAGE_INFO pi = new PAGE_INFO(); pi.nSize = Marshal.SizeOf(pi);
                if (XDW_GetPageInformation(h, i, ref pi) == 0 && pi.nWidth > 0 && pi.nHeight > 0) {
                    double wi = pi.nWidth / 2540.0, hi = pi.nHeight / 2540.0;   // 1/100 mm → inch
                    while (wi * dpi * hi * dpi > MaxPixels && dpi > 100) dpi -= 25;
                }
                IMAGE_OPTION io = new IMAGE_OPTION(); io.nSize = Marshal.SizeOf(io); io.nDpi = dpi; io.nColor = IMAGE_COLOR; io.nImageType = IMAGE_JPEG;
                string jp = Path.Combine(tmp, i + ".jpg");
                r = XDW_ConvertPageToImageFileW(h, i, jp, ref io);
                if (r != 0) throw new Exception(i + " ページ目を変換できません（エラー " + Hex((uint)r) + "）");
                jpgs.Add(jp); dpis.Add(dpi);
            }
            WritePdf(dst, jpgs, dpis, Path.GetFileNameWithoutExtension(src));
            return n;
        } finally {
            XDW_CloseDocumentHandle(h, IntPtr.Zero);
            try { Directory.Delete(tmp, true); } catch { }
        }
    }

    // JPEG の幅・高さ・色数を読む
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

    static string Num(double v) { return v.ToString("0.###", System.Globalization.CultureInfo.InvariantCulture); }

    // JPEG を 1 ページ 1 枚で並べた PDF を書く
    static void WritePdf(string dst, List<string> jpgs, List<int> dpis, string title) {
        int n = jpgs.Count;
        // オブジェクト番号: 1 Catalog, 2 Pages, 3 Info, 以降 1 ページにつき Page・Image・Contents の 3 つ
        List<long> offs = new List<long>();
        using (FileStream f = new FileStream(dst, FileMode.CreateNew, FileAccess.Write)) {
            Action<string> W = s => { byte[] bb = Encoding.ASCII.GetBytes(s); f.Write(bb, 0, bb.Length); };
            Action<int> Obj = id => { while (offs.Count < id) offs.Add(0); offs[id - 1] = f.Position; W(id + " 0 obj\n"); };
            W("%PDF-1.4\n"); f.Write(new byte[] { 0x25, 0xE2, 0xE3, 0xCF, 0xD3, 0x0A }, 0, 6);
            Obj(1); W("<< /Type /Catalog /Pages 2 0 R >>\nendobj\n");
            StringBuilder kids = new StringBuilder();
            for (int i = 0; i < n; i++) kids.Append((4 + i * 3) + " 0 R ");
            Obj(2); W("<< /Type /Pages /Count " + n + " /Kids [ " + kids + "] >>\nendobj\n");
            StringBuilder t = new StringBuilder("FEFF");
            foreach (char c in title) t.Append(((int)c).ToString("X4"));
            Obj(3); W("<< /Title <" + t + "> /Producer (DocuWorks to PDF) >>\nendobj\n");
            for (int i = 0; i < n; i++) {
                int pid = 4 + i * 3, iid = pid + 1, cid = pid + 2;
                int w, hgt, comps; JpegInfo(jpgs[i], out w, out hgt, out comps);
                double pw = w * 72.0 / dpis[i], ph = hgt * 72.0 / dpis[i];
                Obj(pid); W("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 " + Num(pw) + " " + Num(ph) + "] /Resources << /XObject << /Im0 " + iid + " 0 R >> >> /Contents " + cid + " 0 R >>\nendobj\n");
                string cs = comps == 1 ? "/DeviceGray" : comps == 4 ? "/DeviceCMYK /Decode [1 0 1 0 1 0 1 0]" : "/DeviceRGB";
                long len = new FileInfo(jpgs[i]).Length;
                Obj(iid); W("<< /Type /XObject /Subtype /Image /Width " + w + " /Height " + hgt + " /ColorSpace " + cs + " /BitsPerComponent 8 /Filter /DCTDecode /Length " + len + " >>\nstream\n");
                using (FileStream j = File.OpenRead(jpgs[i])) j.CopyTo(f);
                W("\nendstream\nendobj\n");
                string content = "q " + Num(pw) + " 0 0 " + Num(ph) + " 0 0 cm /Im0 Do Q";
                Obj(cid); W("<< /Length " + content.Length + " >>\nstream\n" + content + "\nendstream\nendobj\n");
            }
            long xref = f.Position;
            W("xref\n0 " + (offs.Count + 1) + "\n0000000000 65535 f \n");
            foreach (long o in offs) W(o.ToString("D10") + " 00000 n \n");
            W("trailer\n<< /Size " + (offs.Count + 1) + " /Root 1 0 R /Info 3 0 R >>\nstartxref\n" + xref + "\n%%EOF\n");
        }
    }
}
"@

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

$code_ok = 1
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
    if ($files.Count -eq 0) { throw '変換できるファイル（.xdw / .xbd）がありませんでした' }
    try { Add-Type -TypeDefinition $code -Language CSharp } catch {
        if (([Environment]::Is64BitProcess) -and -not (Test-Path "$env:WINDIR\System32\xdwapi.dll")) { throw '64bit 用の DocuWorks 部品がありません。setup.bat をもう一度実行してショートカットを作り直してください' }
        throw
    }
    $ok = 0; $ng = @()
    for ($k = 0; $k -lt $files.Count; $k++) {
        $src = $files[$k]
        $dst = Get-UniquePath ([IO.Path]::ChangeExtension($src, '.pdf'))
        Write-Host ("[{0}/{1}] {2}" -f ($k + 1), $files.Count, [IO.Path]::GetFileName($src))
        try {
            $n = [Dw2Pdf]::Convert($src, $dst, [Action[int, int]] { param($i, $m) Write-Host -NoNewline ("`r    {0}/{1} ページ" -f $i, $m) })
            Write-Host ("`r    → {0}（{1} ページ）          " -f [IO.Path]::GetFileName($dst), $n)
            $ok++
        } catch {
            if (Test-Path -LiteralPath $dst) { Remove-Item -LiteralPath $dst -Force -ErrorAction SilentlyContinue }   # 途中までのファイルは消す
            $msg = $_.Exception.Message; if ($_.Exception.InnerException) { $msg = $_.Exception.InnerException.Message }
            Write-Host "`r    × 失敗：$msg"
            $ng += $src
        }
    }
    Write-Host ''
    Write-Host "完了：成功 $ok 件 / 失敗 $($ng.Count) 件"
    Write-Host 'PDF は元のファイルと同じフォルダにできています（元のファイルはそのまま）。'
    Write-Host '※ ページを画像にして変換しているため、PDF の文字は選択・検索できません。'
    $code_ok = if ($ng.Count) { 2 } else { 0 }
} catch {
    Write-Host "エラー：$($_.Exception.Message)"
}
if (-not $env:DW2PDF_NOPAUSE) { Read-Host "`nEnter キーで閉じます" }
exit $code_ok
