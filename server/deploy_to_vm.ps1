# 編集 PC から Linux VM（Rocky Linux 推奨 / Ubuntu も可）に TeloCut アセットサーバーを一発でインストールする。
#   powershell -ExecutionPolicy Bypass -File server\deploy_to_vm.ps1 -Vm ユーザー@VMのIP [-Nas //NASのIP/共有名 -NasUser ユーザー] [-Drive Z:] [-RenderNode]
# 必要なもの: VM に SSH でつながること（Windows 標準の ssh / scp / tar を使う）
# SSH の鍵が無ければ作って VM に登録する（以後パスワードを聞かれない）
param(
    [Parameter(Mandatory = $true)][string]$Vm,
    [string]$Nas = "",
    [string]$NasUser = "",
    [string]$Drive = "",          # 編集 PC でその共有をつないでいるドライブ（例 Z:）。レンダーノードのパス読み替え用
    [int]$Port = 8770,
    [switch]$RenderNode
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pkg = Join-Path $env:TEMP "telocut_server.tgz"
$items = @("core", "editor", "telocut.py", "server")

# ── SSH の鍵 ──────────────────────────────
$sshDir = Join-Path $env:USERPROFILE ".ssh"
$key = Join-Path $sshDir "id_ed25519"
if (-not (Test-Path $key)) {
    Write-Host "== SSH の鍵を作ります（$key）"
    New-Item -ItemType Directory -Force $sshDir | Out-Null
    ssh-keygen -q -t ed25519 -f $key -N '""' -C "telocut-deploy@$env:COMPUTERNAME"
    if (-not (Test-Path "$key.pub")) { throw "SSH の鍵を作れませんでした" }
}
$pub = (Get-Content "$key.pub" -Raw).Trim()

ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new $Vm "true" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "== VM に鍵を登録します（VM のログインパスワードを入力）"
    $reg = "umask 077; mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && (command -v restorecon >/dev/null && restorecon -R ~/.ssh || true)"
    $pub | ssh -o PreferredAuthentications=keyboard-interactive,password -o StrictHostKeyChecking=accept-new $Vm $reg
    ssh -o BatchMode=yes -o ConnectTimeout=8 $Vm "true" 2>$null
    if ($LASTEXITCODE -ne 0) {
        $user = $Vm.Split("@")[0]
        Write-Host ""
        Write-Host "VM がパスワードでのログインを受け付けていないため、鍵を登録できませんでした。" -ForegroundColor Yellow
        Write-Host "VM の画面（Hyper-V / VMware などのコンソール）で $user としてログインし、次の 1 行を貼り付けて Enter してください:" -ForegroundColor Yellow
        Write-Host ""
        Write-Host "mkdir -p ~/.ssh && echo '$pub' >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys && (restorecon -R ~/.ssh 2>/dev/null; true)"
        Write-Host ""
        Write-Host "（貼り付けられない場合は、代わりに次を打って一時的にパスワードログインを許可してから、もう一度このスクリプトを実行）"
        Write-Host "sudo sed -i 's/^PasswordAuthentication no/PasswordAuthentication yes/' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf; sudo systemctl restart sshd"
        exit 1
    }
    Write-Host "   鍵を登録しました。次からパスワードは聞かれません（sudo のときだけ聞かれます）"
}

# ── 送るものをまとめる ─────────────────────
Push-Location $root
$fontDir = Join-Path $root "fonts"
$madeFonts = $false
try {
    if ($RenderNode) {
        # 自分で入れたフォント（フリーフォント等）をレンダーノードに送る。Windows 標準フォントは送らない（ライセンス）
        $userFonts = Join-Path $env:LOCALAPPDATA "Microsoft\Windows\Fonts"
        if ((Test-Path $userFonts) -and -not (Test-Path $fontDir)) {
            New-Item -ItemType Directory $fontDir | Out-Null
            $madeFonts = $true
            Get-ChildItem $userFonts -Include *.ttf, *.otf, *.ttc -Recurse | Copy-Item -Destination $fontDir
            Write-Host "== フォント $((Get-ChildItem $fontDir).Count) 個を送ります"
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

Write-Host "== $Vm に送っています"
scp -q $pkg "${Vm}:/tmp/telocut_server.tgz"
if ($LASTEXITCODE -ne 0) { throw "scp に失敗しました（ssh $Vm でログインできるか確認）" }

# 編集 PC から見た NAS の場所（\\192.168.0.10\ES_General と Z:）。レンダーノードのパスの読み替えに使う
$from = @()
if ($Nas) { $from += $Nas.Replace("/", "\") }
if ($Drive) { $from += ($Drive.TrimEnd("\", ":") + ":") }
$pathFrom = $from -join ";"
$envs = "NAS_SHARE='$Nas' NAS_USER='$NasUser' PORT=$Port RENDER_NODE=$([int][bool]$RenderNode) PATH_FROM='$pathFrom'"
$cmd = "(command -v tar >/dev/null || sudo dnf install -y tar || sudo apt-get install -y tar) && " +
       "rm -rf ~/telocut-src && mkdir -p ~/telocut-src && tar --warning=no-unknown-keyword -xzf /tmp/telocut_server.tgz -C ~/telocut-src && " +
       "sed -i 's/\r$//' ~/telocut-src/server/setup_linux.sh && sudo env $envs bash ~/telocut-src/server/setup_linux.sh"
Write-Host "== VM でインストールしています（sudo のパスワードを聞かれたら VM のパスワード）"
ssh -t $Vm $cmd
if ($LASTEXITCODE -ne 0) { throw "インストールに失敗しました（上のメッセージを確認）" }
Remove-Item $pkg -ErrorAction SilentlyContinue
