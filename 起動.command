#!/bin/bash
# ============================================================
#  日本版研究情報ハブ — ローカル起動スクリプト
#  ダブルクリックで jhub サーバーを起動し、ブラウザを開きます
#  ※ このファイルが入っているフォルダのどこに置いても動きます
# ============================================================

# このスクリプトが置かれているフォルダを自動検出
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "============================================"
echo "  日本版研究情報ハブ — ローカルサーバー起動"
echo "  場所: $DIR"
echo "============================================"
echo ""

# ── Python の確認 ──────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
  echo "❌ python3 が見つかりません。"
  echo "   https://www.python.org/downloads/ からインストール後、"
  echo "   このスクリプトを再実行してください。"
  read -p "Enterで閉じる..." && exit 1
fi
echo "✅ $(python3 --version)"

# ── 依存パッケージの確認 ────────────────────────────────────
python3 -c "import flask" 2>/dev/null || {
  echo "⚙️  flask をインストールしています..."
  pip3 install flask --quiet --break-system-packages 2>/dev/null \
    || pip3 install flask --quiet
  echo "✅ flask インストール完了"
}

# ── ポートを自動選択（使用中なら次のポートへ）──────────────
PORT=8080
while lsof -iTCP:$PORT -sTCP:LISTEN &>/dev/null 2>/dev/null; do
  PORT=$((PORT + 1))
done

# ── config.local.json を自動生成（このフォルダの絶対パスを使用）──
DB_PATH="$DIR/data/hub.sqlite"
STORE_PATH="$DIR/data/store"

# config.json が存在すればそれをベースに、なければ空から生成
if [ -f "$DIR/config.json" ]; then
  python3 -c "
import json, os
c = json.load(open('$DIR/config.json', encoding='utf-8'))
c['db']   = '$DB_PATH'
c['store'] = {'type': 'local', 'path': '$STORE_PATH'}
c['hub_base_url'] = 'http://127.0.0.1:$PORT/oai'
json.dump(c, open('$DIR/config.local.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
"
else
  python3 -c "
import json
c = {
  'admin_email': 'ops@example.org',
  'user_agent': 'jhub/0.1',
  'hub_base_url': 'http://127.0.0.1:$PORT/oai',
  'db': '$DB_PATH',
  'store': {'type': 'local', 'path': '$STORE_PATH'},
  'abstracts': {'policy': 'off'}
}
json.dump(c, open('$DIR/config.local.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
"
fi

# ── DBの確認 ───────────────────────────────────────────────
if [ -f "$DB_PATH" ]; then
  SIZE=$(du -sh "$DB_PATH" | cut -f1)
  echo "✅ データベース: data/hub.sqlite ($SIZE)"
else
  echo "⚠️  data/hub.sqlite が見つかりません。"
  echo "   メインPCの Japanese_Data_HUB/data/hub.sqlite をコピーしてください。"
  read -p "Enterで閉じる..." && exit 1
fi

echo ""
echo "🌐 http://127.0.0.1:$PORT  でサーバーを起動します"
echo "   停止: Ctrl+C またはこのウィンドウを閉じる"
echo "============================================"
echo ""

# ── ブラウザを2秒後に自動で開く ────────────────────────────
(sleep 2 && open "http://127.0.0.1:$PORT") &

# ── jhub サーバー起動（このフォルダをPythonパスに追加）─────
PYTHONPATH="$DIR" python3 -m jhub serve \
  --config "$DIR/config.local.json" \
  --host 127.0.0.1 \
  --port $PORT
