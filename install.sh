#!/usr/bin/env bash
# 競馬自動投票のセットアップ（Ubuntu用）。もう一度実行すると最新版に更新される。
#   curl -fsSL <このファイルのURL> | sudo bash
set -euo pipefail

REPO_URL="${REPO_URL:-__REPO_URL__}"
DIR=/opt/keiba

say() { printf '\n\033[1;32m%s\033[0m\n' "$*"; }
ask() {  # ask 変数名 "質問" [秘密なら1]
  local __v
  if [ "${3:-}" = 1 ]; then
    read -r -s -p "$2: " __v < /dev/tty; echo
  else
    read -r -p "$2: " __v < /dev/tty
  fi
  printf -v "$1" '%s' "$__v"
}

if [ "$(id -u)" != 0 ]; then
  echo "sudo をつけて実行してください"; exit 1
fi

say "1/4 必要なソフトを入れています（数分かかります）"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh >/dev/null
fi
command -v git >/dev/null || (apt-get update -qq && apt-get install -y -qq git >/dev/null)
if command -v ufw >/dev/null && ufw status | grep -q active; then
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
fi

say "2/4 アプリをダウンロードしています"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull -q
else
  git clone -q "$REPO_URL" "$DIR"
fi
cd "$DIR"
mkdir -p data

if [ ! -f .env ]; then
  say "3/4 いくつか質問します（分からないものは空のままEnterで飛ばせます。あとで設定し直せます）"
  while :; do
    ask APP_PASSWORD "アプリのログインパスワードを決めてください（8文字以上）" 1
    [ ${#APP_PASSWORD} -ge 8 ] && break
    echo "8文字以上にしてください"
  done
  echo
  echo "--- 即PAT（中央）---"
  ask IPAT_INET_ID "INET-ID"
  ask IPAT_SUBSCRIBER_NO "加入者番号"
  ask IPAT_PIN "暗証番号（4桁）" 1
  ask IPAT_PARS "P-ARS番号（4桁）" 1
  echo
  echo "--- SPAT4（地方）---"
  ask SPAT4_SUBSCRIBER_NO "加入者番号"
  ask SPAT4_USER_ID "利用者ID"
  ask SPAT4_PIN "暗証番号" 1

  IP=$(curl -4 -fsS https://api.ipify.org || hostname -I | awk '{print $1}')
  DOMAIN="${IP//./-}.sslip.io"   # IPアドレスから自動で決まる無料のアドレス（HTTPSにする用）
  TOPIC="keiba-$(openssl rand -hex 12)"
  umask 077
  cat > .env <<EOF
APP_PASSWORD=$APP_PASSWORD
SECRET_KEY=$(openssl rand -hex 32)
DOMAIN=$DOMAIN
ALLOW_LIVE=
NTFY_URL=https://ntfy.sh/$TOPIC
IPAT_INET_ID=$IPAT_INET_ID
IPAT_SUBSCRIBER_NO=$IPAT_SUBSCRIBER_NO
IPAT_PIN=$IPAT_PIN
IPAT_PARS=$IPAT_PARS
SPAT4_SUBSCRIBER_NO=$SPAT4_SUBSCRIBER_NO
SPAT4_USER_ID=$SPAT4_USER_ID
SPAT4_PIN=$SPAT4_PIN
EOF
  chmod 600 .env
else
  say "3/4 設定はそのまま使います（やり直すときは sudo rm /opt/keiba/.env してから再実行）"
fi

say "4/4 アプリを起動しています（初回は5〜10分かかります）"
docker compose up -d --build

DOMAIN=$(grep '^DOMAIN=' .env | cut -d= -f2)
TOPIC=$(grep '^NTFY_URL=' .env | sed 's#.*/##')
cat <<EOF

========================================================
 完了しました！

 ① スマホでこのアドレスを開いてください
      https://$DOMAIN
    （開けないときは1〜2分待ってから再読み込み）
    開いたら、共有ボタン →「ホーム画面に追加」

 ② 通知を受け取るには、スマホに「ntfy」アプリを入れて
    「+」→ トピック名にこれを入力してください
      $TOPIC

 ③ アプリの「設定」→「接続テスト」を押してください
    （購入はしません）
========================================================
EOF
