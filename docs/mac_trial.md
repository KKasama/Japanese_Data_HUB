# Macで試す: JaLC（J-STAGE）の少量収集

目的: 実際のJaLC APIから**数十件だけ**取得し、データの形と変換ルールを確認する。
所要時間: 約10分。IP申請は不要（公開API）。件数は少量・低頻度（1リクエスト/秒）に留める。
（JaLC一般利用規約 第7条: 機械的な大量取得はJaLCが許諾する方法に限る。この試行は動作確認のための少量です。）

## 0. 準備（初回のみ）

ターミナルを開き、リポジトリのフォルダへ移動する。

```
cd "/Users/kk/Documents/Claude/Projects/JSTのデータについてのOpenAlexへの投入について/Japanese_Data_HUB"
python3 --version        # 3.9 以上であればOK（追加のインストールは不要）
```

## 1. 設定ファイルを作る

```
cp config.example.json config.json
```

`config.json` はそのままで動く（保存先は `data/` の下。Gitには入らない）。

## 2. J-STAGEのプレフィックスを1つ、20件だけ収集

```
python3 -m jhub harvest-jalc --prefix 10.11162 --max-dois 20
```

`[jalc] 10.11162 完了 (累計DOI 20)` と出れば成功。
（別のJ-STAGEプレフィックスも試す場合: 次のように一覧から選べる。`python3 -m jhub harvest-jalc --site SI/JST.JSTAGE --max-dois 20` は先頭のプレフィックスから順に取る）

## 3. 取り込み（振り分け・整形）と件数の確認

```
python3 -m jhub ingest
python3 -m jhub stats
```

`records: 20` 前後、`by_source: {"jalc": 20}` となる。

## 4. データの形を確認して、報告用ファイルを作る

```
python3 tools/inspect_jalc.py data/store -o jalc_report.txt
```

画面に、項目名・型・値の例が出る。`jalc_report.txt`（個人情報を含まない構造の一覧）が、Claudeに共有する対象。

## 5. 整形結果を見る（任意）

```
python3 -m jhub serve --port 8080
```

ブラウザで次を開く。
- http://127.0.0.1:8080/api/works?site=SI/JST.JSTAGE&limit=3  （整形後のデータ）
- http://127.0.0.1:8080/oai?verb=ListSets  （セット一覧）

終了は Ctrl+C。

## うまくいかないとき

| 症状 | 対処 |
|---|---|
| `command not found: python3` | `python --version` を試す。なければ https://www.python.org からインストール |
| `[jalc] ... 完了 (累計DOI 0)` | そのプレフィックスに該当DOIがない。別のプレフィックスを試す |
| 通信エラー・403 | 社内ネットワーク/プロキシの影響の可能性。エラー全文を共有 |
| `ingest` で `bad_pages` が出る | そのまま `jalc_report.txt` とエラー文を共有 |

## 共有してほしいもの

1. `jalc_report.txt`
2. 実行した画面の出力（エラーがあれば全文）
3. `data/store/raw/jalc/` の下のJSONLファイル1つ（任意。そのまま見られると変換を正確に直せる）
