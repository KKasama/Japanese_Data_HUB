#!/bin/bash
# ============================================================
#  ノートPCセットアップスクリプト
#  初回のみ実行してください。依存パッケージを自動インストールします。
# ============================================================

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "============================================"
echo "  日本版研究情報ハブ — ノートPCセットアップ"
echo "============================================"
echo ""

# Python の確認
if ! command -v python3 &>/dev/null; then
  echo "❌ python3 が見つかりません。"
  echo "   https://www.python.org/downloads/ からインストール後、"
  echo "   このスクリプトを再実行してください。"
  read -p "Enterで閉じる..." && exit 1
fi

PYVER=$(python3 --version 2>&1)
echo "✅ $PYVER"

# pip で依存パッケージをインストール
echo ""
echo "⚙️  必要なパッケージをインストールしています..."

# requirements.txt があればそちらから
if [ -f "$DIR/requirements.txt" ]; then
  pip3 install -r "$DIR/requirements.txt" --quiet --break-system-packages 2>/dev/null \
    || pip3 install -r "$DIR/requirements.txt" --quiet
else
  pip3 install flask --quiet --break-system-packages 2>/dev/null \
    || pip3 install flask --quiet
fi

echo "✅ パッケージのインストール完了"
echo ""

# data/hub.sqlite の確認
if [ -f "$DIR/data/hub.sqlite" ]; then
  SIZE=$(du -sh "$DIR/data/hub.sqlite" | cut -f1)
  echo "✅ データベース: data/hub.sqlite ($SIZE)"
else
  echo "⚠️  data/hub.sqlite が見つかりません。"
  echo "   メインPCの Japanese_Data_HUB/data/ フォルダをコピーしてください。"
fi

# config.local.json を作成（相対パス版）
python3 -c "
import json, os
base = '$DIR'
# config.json をベースに、パスをこのPCに合わせて上書き
try:
    c = json.load(open(os.path.join(base, 'config.json'), encoding='utf-8'))
except:
    c = {}
c['db'] = os.path.join(base, 'data', 'hub.sqlite')
c['store'] = {'type': 'local', 'path': os.path.join(base, 'data', 'store')}
# hub_base_url をローカルに
c['hub_base_url'] = 'http://127.0.0.1:8080/oai'
out = os.path.join(base, 'config.local.json')
json.dump(c, open(out, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
print('✅ config.local.json を生成しました:', out)
"

echo ""
echo "============================================"
echo "  セットアップ完了！"
echo "  「起動.command」をダブルクリックしてサーバーを起動してください。"
echo "============================================"
echo ""
read -p "Enterで閉じる..."
