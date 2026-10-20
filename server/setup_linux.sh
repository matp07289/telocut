#!/usr/bin/env bash
# TeloCut アセットサーバーを Linux（Rocky Linux 8/9 推奨・Ubuntu も可。VM 可）に入れて常時起動にする。
# 通常は編集 PC から server/deploy_to_vm.ps1 で実行される（手動なら: sudo bash setup_linux.sh）
#
# 環境変数:
#   SHARES      つなぐ共有（; 区切りで複数）。1 つは「種類|場所|マウント先」
#                 NFS: nfs|192.168.0.10:/mnt/EditShare/ES_General|/mnt/telocut/ES_General（パスワード不要）
#                 SMB: cifs|//192.168.0.10/ES_Render|/mnt/telocut/ES_Render
#   NAS_SHARE   （旧形式）SMB の共有 1 つ。/mnt/nas につなぐ
#   NAS_USER    SMB のユーザー名（空ならゲスト）。パスワードは NAS_PASS かこの場で聞く
#   MEDIA_DIR   共有を使わないときの素材フォルダ
#   PORT        既定 8770
#   RENDER_NODE 1 ならこの VM をレンダーノードにもする
#   PATH_FROM   レンダーノード用: 編集 PC のパス → VM のパス（; 区切り。「元=先」、= が無ければ先は最初の共有）
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/telocut
DATA=/var/lib/telocut
PORT="${PORT:-8770}"
MEDIA_DIR="${MEDIA_DIR:-/srv/telocut/media}"

if [ "$(id -u)" -ne 0 ]; then
  echo "sudo で実行してください"; exit 1
fi
. /etc/os-release
echo "== OS: $PRETTY_NAME"

echo "== パッケージをインストール"
if command -v dnf >/dev/null; then
  # Rocky / Alma / RHEL
  dnf install -y -q python3.11 python3.11-pip cifs-utils nfs-utils tar curl google-noto-sans-cjk-ttc-fonts >/dev/null \
    || dnf install -y -q python3 python3-pip cifs-utils nfs-utils tar curl >/dev/null
  PY="$(command -v python3.11 || command -v python3)"
elif command -v apt-get >/dev/null; then
  apt-get update -qq
  apt-get install -y -qq python3-venv python3-pip cifs-utils nfs-common curl fonts-noto-cjk >/dev/null
  PY="$(command -v python3)"
else
  echo "dnf / apt が見つかりません（対応: Rocky Linux・Alma・RHEL・Ubuntu・Debian）"; exit 1
fi
echo "   Python: $($PY --version)"

echo "== NAS をマウント"
id telocut >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /sbin/nologin telocut
if [ -z "${SHARES:-}" ] && [ -n "${NAS_SHARE:-}" ]; then
  SHARES="cifs|$NAS_SHARE|/mnt/nas"            # 旧形式
fi
ROOTS=""
FIRST_MP=""
if [ -n "${SHARES:-}" ]; then
  # 入れ直しのとき: 動いているサービスが NAS を使っているので、止めてからつなぎ直す
  systemctl stop telocut-node.service telocut-asset.service 2>/dev/null || true
  CRED=/etc/telocut-nas.cred
  if echo "$SHARES" | grep -q "cifs|"; then
    if [ -n "${NAS_USER:-}" ]; then
      if [ -z "${NAS_PASS:-}" ]; then
        read -r -s -p "NAS（SMB・$NAS_USER）のパスワード: " NAS_PASS; echo
      fi
      printf 'username=%s\npassword=%s\n' "$NAS_USER" "$NAS_PASS" > "$CRED"
    else
      printf 'username=guest\npassword=\n' > "$CRED"
    fi
    chmod 600 "$CRED"
  fi
  # 前の形式（/mnt/nas）で入れていて、今回は使わない場合は外す
  if ! echo "$SHARES" | grep -q "|/mnt/nas$\||/mnt/nas;" && grep -q " /mnt/nas " /etc/fstab; then
    umount /mnt/nas 2>/dev/null || umount -l /mnt/nas 2>/dev/null || true
    sed -i "\# /mnt/nas #d" /etc/fstab
    echo "   前の接続（/mnt/nas）を外しました"
  fi
  IFS=';' read -ra ENTRIES <<< "$SHARES"
  for ent in "${ENTRIES[@]}"; do
    [ -z "$ent" ] && continue
    IFS='|' read -r TYP SRCP MP <<< "$ent"
    mkdir -p "$MP"
    sed -i "\# $MP #d" /etc/fstab
    if [ "$TYP" = "nfs" ]; then
      echo "$SRCP $MP nfs ro,_netdev,nofail,soft,timeo=150,retrans=3 0 0" >> /etc/fstab
    else
      echo "$SRCP $MP cifs credentials=$CRED,iocharset=utf8,ro,_netdev,nofail,vers=3.0,file_mode=0644,dir_mode=0755 0 0" >> /etc/fstab
    fi
    systemctl daemon-reload
    if mountpoint -q "$MP"; then
      umount "$MP" 2>/dev/null || umount -l "$MP" 2>/dev/null || true
    fi
    if mountpoint -q "$MP"; then
      echo "   $MP はすでにつながっているので、そのまま使います"
    else
      mount "$MP" || { echo "NAS をマウントできませんでした: $SRCP（共有名 / NFS の公開設定 / ユーザー・パスワードを確認）"; exit 1; }
    fi
    # サーバーを動かすユーザー（telocut）から読めるか（NFS は NAS 側の権限がそのまま効く）
    if su -s /bin/sh telocut -c "ls '$MP' >/dev/null 2>&1"; then
      echo "   $SRCP → $MP（読み取り専用・$TYP）"
    else
      echo "   ⚠ $MP はつながりましたが、サーバー用ユーザーから読めません。"
      echo "     TrueNAS の NFS 共有の設定で「Mapall User」を読めるユーザーにするか、SMB でつないでください"
    fi
    ROOTS="$ROOTS --root $MP"
    [ -z "$FIRST_MP" ] && FIRST_MP="$MP"
  done
  MEDIA_DIR="$FIRST_MP"
else
  mkdir -p "$MEDIA_DIR"
  ROOTS="--root $MEDIA_DIR"
fi

echo "== メモリの確認"
MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
SWAP_MB=$(awk '/SwapTotal/ {print int($2/1024)}' /proc/meminfo)
echo "   メモリ ${MEM_MB} MB / スワップ ${SWAP_MB} MB"
if [ "$MEM_MB" -lt 3000 ] && [ "$SWAP_MB" -lt 512 ] && [ ! -e /swapfile ]; then
  echo "   メモリが少なくスワップも無いので、2 GB のスワップファイルを作ります（/swapfile）"
  if fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none; then
    chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
    grep -q "^/swapfile " /etc/fstab || echo "/swapfile none swap defaults 0 0" >> /etc/fstab
  fi
fi

echo "== TeloCut を配置"
mkdir -p "$APP" "$DATA/home" "$DATA/fonts"
rm -rf "$APP/core" "$APP/editor"
cp -r "$SRC/core" "$SRC/editor" "$SRC/telocut.py" "$APP/"
find "$APP" -name "__pycache__" -prune -exec rm -rf {} +
# 編集 PC から送られたフォント（テロップの見た目をそろえる）
if [ -d "$SRC/fonts" ]; then
  cp -f "$SRC"/fonts/* "$DATA/fonts/" 2>/dev/null || true
  echo "   フォント $(ls "$DATA/fonts" | wc -l) 個"
fi
if [ ! -x "$APP/venv/bin/python" ]; then
  "$PY" -m venv "$APP/venv"
fi
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$SRC/server/requirements-server.txt"
chown -R telocut:telocut "$DATA"
# SELinux（Rocky）: 置いたファイルのラベルを既定に戻す
command -v restorecon >/dev/null && restorecon -R "$APP" "$DATA" || true

echo "== サービスを登録"
cat > /etc/systemd/system/telocut-asset.service <<EOF
[Unit]
Description=TeloCut Asset Server
After=network-online.target remote-fs.target
Wants=network-online.target

[Service]
User=telocut
WorkingDirectory=$APP
Environment=TELOCUT_HOME=$DATA/home
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP/venv/bin/python -m editor.assetserver $ROOTS --data $DATA --port $PORT
Restart=always
RestartSec=5
# メモリを使いすぎたときは、VM 全体ではなくこのサービスだけが抑えられ / 再起動されるように
MemoryHigh=800M
MemoryMax=1200M

[Install]
WantedBy=multi-user.target
EOF

if [ "${RENDER_NODE:-0}" = "1" ]; then
  # 編集 PC のパス（例 \\NAS\video\a.mp4）を VM のパス（/mnt/nas/a.mp4）に読み替える
  if [ -n "${PATH_FROM:-}" ]; then
    "$APP/venv/bin/python" - "$DATA/home/telocut_settings.json" "$PATH_FROM" "$MEDIA_DIR" <<'PY'
import json, sys
p, items, default_dst = sys.argv[1:]
try:
    d = json.load(open(p, encoding="utf-8"))
except Exception:
    d = {}
for it in [x for x in items.split(";") if x]:     # 「元=先」（例 Z:=/mnt/telocut/ES_General）。= が無ければ先は最初の共有
    src, _, dst = it.partition("=")
    d["path_maps"] = [m for m in d.get("path_maps", []) if m[0] != src] + [[src, dst or default_dst]]
json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
PY
    chown telocut:telocut "$DATA/home/telocut_settings.json"
  fi
  cat > /etc/systemd/system/telocut-node.service <<EOF
[Unit]
Description=TeloCut Render Node
After=telocut-asset.service

[Service]
User=telocut
WorkingDirectory=$APP
Environment=TELOCUT_HOME=$DATA/home
Environment=TELOCUT_FONT_DIR=$DATA/fonts
Environment=QT_QPA_PLATFORM=offscreen
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP/venv/bin/python telocut.py --render-node --server http://127.0.0.1:$PORT --name $(hostname -s)
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
fi

systemctl daemon-reload
systemctl enable telocut-asset.service >/dev/null 2>&1
systemctl restart telocut-asset.service
if [ "${RENDER_NODE:-0}" = "1" ]; then
  systemctl enable telocut-node.service >/dev/null 2>&1
  systemctl restart telocut-node.service
fi

echo "== ファイアウォール"
if systemctl is-active --quiet firewalld; then
  firewall-cmd --permanent --add-port="$PORT/tcp" >/dev/null
  firewall-cmd --reload >/dev/null
  echo "   firewalld: $PORT/tcp を開けました"
elif command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow "$PORT/tcp" >/dev/null
  echo "   ufw: $PORT/tcp を開けました"
fi

sleep 3
IP="$(hostname -I | awk '{print $1}')"
if curl -fsS "http://127.0.0.1:$PORT/api/info" >/dev/null 2>&1; then
  echo
  echo "======================================================"
  echo " 完了: http://$IP:$PORT/  （ブラウザで素材を見られます）"
  echo " 素材フォルダ:${ROOTS//--root/}"
  [ "${RENDER_NODE:-0}" = "1" ] && echo " レンダーノード: $(hostname -s)（ブラウザの「ファーム」タブで確認）"
  echo " TeloCut のアセットパネル / 書き出し（レンダーファーム）にこの URL を入れてください"
  echo "======================================================"
else
  echo "起動に失敗しました。ログ: journalctl -u telocut-asset -n 50"
  exit 1
fi
