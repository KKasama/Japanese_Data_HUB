#!/bin/bash
# ============================================================
#  外部公開スクリプト — Cloudflare Tunnel を使って
#  外出先のノートPCからこのサーバーへアクセスできるようにします
#  ※ このMac（自宅・オフィスサーバー）で実行してください
# ============================================================

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "============================================"
echo "  日本版研究情報ハブ — 外部公開（Cloudflare Tunnel）"
echo "============================================"
echo ""

# cloudflared の確認・インストール
if ! command -v cloudflared &>/dev/null; then
  echo "⚙️  cloudflared をインストールしています..."
  if command -v brew &>/dev/null; then
    brew install cloudflared
  else
    echo "❌ Homebrew が必要です: https://brew.sh"
    echo "   インストール後、このスクリプトを再実行してください。"
    read -p "Enterで閉じる..." && exit 1
  fi
fi

# jhub サーバーが起動しているか確認
PORT=8080
if ! lsof -iTCP:$PORT -sTCP:LISTEN &>/dev/null 2>/dev/null; then
  echo "⚠️  jhub サーバーが起動していません。"
  echo "   先に「起動.command」でサーバーを起動してください。"
  echo ""
  echo "   同時に起動しますか？（別ウィンドウで起動します）"
  read -p "   起動する場合は Enter を押してください..."
  open "$DIR/起動.command"
  echo "   サーバーの起動を待っています..."
  sleep 5
fi

echo "✅ Cloudflare Tunnel を開始します"
echo "   起動後に表示される URL（https://xxxx.trycloudflare.com）"
echo "   をノートPCのブラウザで開いてください。"
echo ""
echo "   停止するには: Ctrl+C またはこのウィンドウを閉じる"
echo "============================================"
echo ""

cloudflared tunnel --url http://127.0.0.1:$PORT
