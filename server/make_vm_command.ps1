# VM（Rocky Linux / Ubuntu）に Tera Term などで SSH ログインした後、貼り付け 1 回で TeloCut アセットサーバーを入れるための準備。
#
#   NFS でつなぐ（おすすめ・パスワード不要）＋ NFS で公開していない共有は SMB で:
#     powershell -ExecutionPolicy Bypass -File server\make_vm_command.ps1 -NasHost 192.168.0.10 -Nfs ES_General,ES_Edit -Smb ES_Render -NasUser edit -Output ES_Render
#   -Output: 変換・書き出しの結果を置く共有（その共有だけ書き込みできるようにつなぐ。中の TeloCut フォルダに置く）
#   SMB だけ（旧形式）:
#     powershell -ExecutionPolicy Bypass -File server\make_vm_command.ps1 -Nas //192.168.0.10/ES_General -NasUser edit
#
# やること:
#   1. サーバー用のファイルを 1 つにまとめて NAS（最初の共有）の TeloCutServer フォルダに置く
#   2. VM で貼り付ける 1 行を作ってクリップボードに入れる（画面にも表示）
#   この PC でその共有をつないでいるドライブ（Z: など）は自動で調べ、VM 側のパスとの対応も設定する
param(
    [string]$NasHost = "",
    [string[]]$Nfs = @(),                 # NFS でつなぐ共有名（TrueNAS の NFS 共有のパスは <NfsBase>/<共有名>）
    [string[]]$Smb = @(),                 # SMB でつなぐ共有名
    [string]$NfsBase = "/mnt/EditShare",
    [string]$Nas = "",                    # 旧形式: //NAS/共有名（SMB 1 つ）
    [string]$NasUser = "",
    [string]$Drive = "",                  # 旧形式: その共有のドライブ（自動で調べるので通常は不要）
    [int]$Port = 8770,
    [switch]$RenderNode,
    [string]$Output = "",                 # 結果を置く共有の名前（例 ES_Render）
    [string]$Git = "",                    # VM が取得する Git のリポジトリ（省略時はこのリポジトリの origin）
    [switch]$ViaNas                       # Git を使わず、NAS にファイルを置いて渡す（以前の方式）
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

# ── 旧形式（-Nas //IP/共有名）を新形式に
if ($Nas) {
    $parts = $Nas.Replace("\", "/").TrimStart("/").Split("/") | Where-Object { $_ -ne "" }
    if ($parts.Count -lt 2) { throw "-Nas は //NASのIP/共有名 の形で指定してください（例 //192.168.0.10/ES_General）" }
    $NasHost = $parts[0]
    $sh = $parts[$parts.Count - 1]
    if ($parts.Count -gt 2) {
        Write-Host "注意: -Nas に NAS 内部のフォルダパスが入っていたので、共有名「$sh」として扱います" -ForegroundColor Yellow
    }
    $Smb = @($sh) + $Smb
}
$Nfs = @($Nfs | ForEach-Object { $_ -split "," } | Where-Object { $_ })
$Smb = @($Smb | ForEach-Object { $_ -split "," } | Where-Object { $_ })
if (-not $NasHost) { throw "-NasHost（NAS の IP）を指定してください" }
if (($Nfs.Count + $Smb.Count) -eq 0) { throw "-Nfs か -Smb で共有名を指定してください（例 -Nfs ES_General,ES_Edit）" }

# ── この PC のドライブ割り当て（\\IP\共有名 → Z:）
$drives = @{}
try {
    foreach ($m in Get-SmbMapping -ErrorAction Stop) {
        $drives[$m.RemotePath.TrimEnd("\").ToLower()] = $m.LocalPath
    }
} catch { }

$shares = @()
foreach ($s in $Nfs) { $shares += [pscustomobject]@{ Name = $s; Type = "nfs"; Src = "${NasHost}:$($NfsBase.TrimEnd('/'))/$s" } }
foreach ($s in $Smb) { $shares += [pscustomobject]@{ Name = $s; Type = "cifs"; Src = "//$NasHost/$s" } }
$entries = @()
$pathFrom = @()
Write-Host "== つなぐ共有"
foreach ($s in $shares) {
    $mp = "/mnt/telocut/$($s.Name)"
    $unc = "\\$NasHost\$($s.Name)"
    $d = $drives[$unc.ToLower()]
    if (-not $d -and $Drive -and $s.Name -eq $shares[0].Name) { $d = $Drive.TrimEnd("\", ":") + ":" }
    $entries += "$($s.Type)|$($s.Src)|$mp"
    $pathFrom += "$unc=$mp"
    if ($d) { $pathFrom += "$d=$mp" }
    Write-Host ("   {0,-4} {1,-45} → VM {2}   （この PC: {3}）" -f $s.Type.ToUpper().Replace("CIFS", "SMB"), $s.Src, $mp, $(if ($d) { "$d / $unc" } else { $unc }))
}

# ── Git（Gitea）から取得するか
if (-not $ViaNas -and -not $Git) {
    try { $Git = (& git -C $root remote get-url origin 2>$null) } catch { $Git = "" }
}
if (-not $ViaNas -and $Git) {
    $ahead = (& git -C $root rev-list --count "@{u}..HEAD" 2>$null)
    $dirty = (& git -C $root status --porcelain 2>$null)
    if ($ahead -and [int]$ahead -gt 0) { Write-Host "注意: まだ push していないコミットが $ahead 個あります（VM には push 済みの内容が入ります）" -ForegroundColor Yellow }
    if ($dirty) { Write-Host "注意: コミットしていない変更があります（VM には push 済みの内容が入ります）" -ForegroundColor Yellow }
    $hasSmb = @($shares | Where-Object { $_.Type -eq "cifs" }).Count -gt 0
    $user = if ($NasUser) { $NasUser } else { "guest" }
    $pre = 'cd ~; ' + $(if ($hasSmb) { 'read -rsp "NAS（SMB・__USER__）のパスワード: " NP; echo; ' } else { 'NP=""; ' })   # 消えたフォルダの中にいると git が動かないので、まずホームへ
    $cmd = $pre +
           '(sudo dnf -y -q install git >/dev/null 2>&1 || sudo apt-get -y -qq install git >/dev/null) && ' +
           '(if [ -d ~/telocut-src/.git ]; then git -C ~/telocut-src pull -q --ff-only; else rm -rf ~/telocut-src && git clone -q __GIT__ ~/telocut-src; fi) && ' +
           'sudo env SHARES=''__SHARES__'' NAS_USER=''__NASUSER__'' NAS_PASS="$NP" PORT=__PORT__ RENDER_NODE=__RN__ PATH_FROM=''__PF__'' OUTPUT_SHARE=''__OUT__'' bash ~/telocut-src/server/setup_linux.sh'
    $cmd = $cmd.Replace("__OUT__", $Output).Replace("__GIT__", $Git).Replace("__USER__", $user).Replace("__SHARES__", ($entries -join ";")).
                Replace("__NASUSER__", $NasUser).Replace("__PORT__", "$Port").Replace("__RN__", "$([int][bool]$RenderNode)").
                Replace("__PF__", ($pathFrom -join ";"))
    Set-Clipboard -Value $cmd
    Write-Host ""
    Write-Host "== Git（$Git）から取得する 1 行をクリップボードに入れました。Tera Term で VM にログインして貼り付けてください:" -ForegroundColor Green
    Write-Host ""
    Write-Host $cmd
    Write-Host ""
    Write-Host "2 回目からの更新は VM で: cd ~/telocut-src && git pull && sudo bash server/setup_linux.sh（設定は前回のまま）"
    return
}

# ── 置き場所（最初の共有。この PC から見える場所）
$first = $shares[0]
$firstDrive = $drives[("\\$NasHost\$($first.Name)").ToLower()]
$dropRoot = if ($firstDrive) { "$firstDrive\" } else { "\\$NasHost\$($first.Name)\" }
if (-not (Test-Path $dropRoot)) { throw "NAS が見えません: $dropRoot（エクスプローラーで開けるか確認）" }
$drop = Join-Path $dropRoot "TeloCutServer"
New-Item -ItemType Directory -Force $drop | Out-Null

# ── まとめる
$items = @("core", "editor", "telocut.py", "server")
$fontDir = Join-Path $root "fonts"
$madeFonts = $false
$pkg = Join-Path $env:TEMP "telocut_server.tgz"
Push-Location $root
try {
    if ($RenderNode) {
        $userFonts = Join-Path $env:LOCALAPPDATA "Microsoft\Windows\Fonts"
        if ((Test-Path $userFonts) -and -not (Test-Path $fontDir)) {
            New-Item -ItemType Directory $fontDir | Out-Null
            $madeFonts = $true
            Get-ChildItem $userFonts -Include *.ttf, *.otf, *.ttc -Recurse | Copy-Item -Destination $fontDir
            $items += "fonts"
        }
    }
    Write-Host "== ファイルをまとめています"
    # Windows 標準の tar.exe を使う（Git の GNU tar が先に見つかると C:\... を接続先と勘違いする）
    $tarExe = Join-Path $env:SystemRoot "System32\tar.exe"
    if (-not (Test-Path $tarExe)) { $tarExe = "tar" }
    & $tarExe --no-fflags --exclude=__pycache__ --exclude=*.pyc --exclude=._* -czf $pkg @items
    if ($LASTEXITCODE -ne 0) { throw "tar に失敗しました" }
} finally {
    if ($madeFonts) { Remove-Item $fontDir -Recurse -Force }
    Pop-Location
}
Copy-Item $pkg (Join-Path $drop "telocut_server.tgz") -Force
Remove-Item $pkg -ErrorAction SilentlyContinue
Write-Host "== NAS に置きました: $(Join-Path $drop 'telocut_server.tgz')"

# ── VM で貼り付ける 1 行（最初の共有を一時的につないで、まとめたファイルを取り出す）
$hasSmb = @($shares | Where-Object { $_.Type -eq "cifs" }).Count -gt 0     # @() が無いと 1 件のとき Count が取れない（PowerShell 5.1）
$user = if ($NasUser) { $NasUser } else { "guest" }
$pre = 'cd ~; ' + $(if ($hasSmb) { 'read -rsp "NAS（SMB・__USER__）のパスワード: " NP; echo; ' } else { 'NP=""; ' })   # 消えたフォルダの中にいると git が動かないので、まずホームへ
if ($first.Type -eq "nfs") {
    $tmpMount = 'sudo mount -t nfs -o ro __FIRSTSRC__ /mnt/telocut_tmp'
} else {
    $tmpMount = 'sudo mount -t cifs __FIRSTSRC__ /mnt/telocut_tmp -o username=__USER__,password="$NP",ro,vers=3.0,iocharset=utf8'
}
$cmd = $pre +
       '(sudo dnf -y -q install cifs-utils nfs-utils tar >/dev/null 2>&1 || sudo apt-get -y -qq install cifs-utils nfs-common tar >/dev/null) && ' +
       'sudo mkdir -p /mnt/telocut_tmp && (mountpoint -q /mnt/telocut_tmp && sudo umount /mnt/telocut_tmp; true) && ' + $tmpMount + ' && ' +
       'rm -rf ~/telocut-src && mkdir -p ~/telocut-src && tar --warning=no-unknown-keyword -xzf /mnt/telocut_tmp/TeloCutServer/telocut_server.tgz -C ~/telocut-src; ' +
       'sudo umount /mnt/telocut_tmp; ' +
       'sed -i ''s/\r$//'' ~/telocut-src/server/setup_linux.sh && ' +
       'sudo env SHARES=''__SHARES__'' NAS_USER=''__NASUSER__'' NAS_PASS="$NP" PORT=__PORT__ RENDER_NODE=__RN__ PATH_FROM=''__PF__'' OUTPUT_SHARE=''__OUT__'' bash ~/telocut-src/server/setup_linux.sh'
$cmd = $cmd.Replace("__OUT__", $Output).Replace("__FIRSTSRC__", $first.Src).Replace("__USER__", $user).Replace("__SHARES__", ($entries -join ";")).
            Replace("__NASUSER__", $NasUser).Replace("__PORT__", "$Port").Replace("__RN__", "$([int][bool]$RenderNode)").
            Replace("__PF__", ($pathFrom -join ";"))

Set-Clipboard -Value $cmd
Write-Host ""
Write-Host "== 次の 1 行をクリップボードに入れました。Tera Term で VM にログインして、貼り付けて Enter してください:" -ForegroundColor Green
Write-Host "   （Tera Term の貼り付け: 右クリック または Alt+V）"
Write-Host ""
Write-Host $cmd
Write-Host ""
if ($hasSmb) {
    Write-Host "聞かれるパスワード: 1) NAS の SMB（$user） 2) sudo（VM のログインパスワード）"
} else {
    Write-Host "聞かれるパスワード: sudo（VM のログインパスワード）だけ（NFS は NAS のパスワード不要）"
}
