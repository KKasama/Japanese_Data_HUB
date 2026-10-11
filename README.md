# jhub — 日本版研究情報ハブ（試作 v0.1）

JaLC（J-STAGE等を含む）とIRDBのメタデータを、**収集 → 生データ保存 → 振り分け → OpenAlex互換形式へ整形 → OAI-PMH／APIで提供**する試作です。Pythonの標準ライブラリのみで動きます（S3/Wasabiを使う場合だけ `boto3`）。

## 現状（2026-10-10）

| 項目 | 状態 |
|---|---|
| 自動テスト 28件（モックのIRDB/JaLCで、収集→再開→差分→再整形→削除→振り分け→提供→出力まで） | 通過 |
| **JaLC（公開API）の実データ** | **確認済み**: J-STAGE、J-STAGE Data、Jxiv、NDL（4種のsiteId・計30件）で、収集→生データ保存→整形→OAI-PMH/API提供まで動作 |
| **IRDBの実データ** | **未確認**: IRDBのレコード取得（ListRecords等）は申請IPからのみ。固定IPの環境を準備し、NIIに手続きを確認中 |
| JaLC JSONの項目 | 実データで確認した項目に対応（ORCID、助成、権利、主題、日付、関連、参考文献DOI、版 など）。他のsiteIdは順次確認 |
| irdbProvenance の構造 | 未確認。**原データをそのまま保存**する設計 |

## 画面（ブラウザ）
`python3 -m jhub serve --port 8080` のあと、http://127.0.0.1:8080/ を開く。ダッシュボード、検索・閲覧（整形後と原データの並列表示）、ダウンロード（JSONL/JSON/CSV、API・OAI-PMHのURL例）、収集状況の4画面。

## 試す（外部接続なし）

```
python3 demo.py                       # モックを収集して http://127.0.0.1:8080 で確認
python3 -I -m unittest discover -s tests
```

## 実データで使う

```
cp config.example.json config.json    # base_url、保存先などを編集
python3 -m jhub harvest-irdb --max-pages 2     # まず2ページだけ（申請IPのマシンから）
python3 -m jhub harvest-jalc --site SI/JST.JSTAGE --prefix 10.11162 --max-dois 20
python3 -m jhub ingest                  # 取り込み。整形ルールを変えた後は --reprocess で再整形
python3 -m jhub load-institutions institutions.csv   # 機関台帳(code,name_ja,name_en,ror,url)
python3 -m jhub stats
python3 -m jhub serve --port 8080
python3 -m jhub export --out export                         # 機関/サイト別
python3 -m jhub export --by journal --out export_jnl        # ジャーナル別（1誌=1ファイル）
```

中断しても、同じコマンドで続きから再開します。ingestは何度実行しても同じ結果になります。

## 構成

| 部品 | ファイル |
|---|---|
| 収集（IRDB: OAI-PMH、再開・リトライ・差分） | `oai_client.py` `harvest.py` |
| 収集（JaLC: REST。prefix→doilist→DOI別v2。既定は低頻度） | `jalc_client.py` `harvest.py` |
| 調査ツール（siteId別の少量収集、実データの項目一覧） | `tools/sample_sites.py` `tools/inspect_jalc.py` |
| 生データ保存（ローカル／S3互換） | `store.py` |
| 振り分け（機関: ヘッダーの機関コード。ジャーナル: ISSN→ジャーナルID） | `classify.py` |
| 整形（JPCOAR→work、JaLC JSON→work。HTMLタグ除去、プレースホルダ除去。抄録は出力せず「有無」のみ） | `normalize.py` |
| DB（SQLite。変更時のみ更新日時を進める） | `db.py` `ingest.py` |
| 提供（OAI-PMH 2.0、取得API） | `server.py` |
| 引渡し（バルクJSONL.gz） | `export.py` |

### OAI-PMH のセット
`irdb`（IRDB由来全部）／`inst:<5桁機関コード>`／`site:<JST.JSTAGE など>`／`jnl:<ISSN>`／`pfx:<DOIプレフィックス>`。
形式は `jpcoar_2.0`（IRDBの原XML。機関別セットで配る）と `jhub_work`（整形済み。JaLC等）。削除はヘッダーの `status="deleted"` で伝えます。

## 既知の限界・要確認（実データで確認するもの）

1. **IRDBのレコード取得は申請IPのみ**（Identify等は通るがListRecordsは `badArgument`）。固定IPの環境の登録手続きをNIIに確認中。
2. **機関の判別**は、ヘッダーの機関コード（setSpec／識別子）を使う。`irdbProvenance` は構造確認後に追加する。
3. **JaLCの全件取得**はDOI1件ごとの取得になり現実的でない（約1,470万DOI）。JaLC OAI-PMH（`/v2/oai`）の利用可否をJSTに確認する。規約第7条により、大量取得はJaLCが許諾する方法に限る。
4. JaLC REST では**削除されたDOIを検出できない**。OAI-PMHなら可能な可能性がある。
5. JaLC JSON の項目は、J-STAGE等4種の実データで確認済み。他のsiteId（約2,400のうちJAIRO等）は順次確認する。
6. OpenAlexへの引渡し方式（OAI-PMH提供／バルクファイル）は、OpenAlexとの合意が前提。
7. 整形は主要項目に加え、ORCID・助成・権利・主題・関連・参考文献DOIなどに対応。JST対応表（79項目）との照合は今後。抄録は出力しない（JSTの了承後に別途）。
8. 型の対応は暫定（JA→article、RD→dataset、BK→book、Jxiv→preprint）。
9. SQLite・単一プロセスの試作。全件規模ではPostgreSQL等と、並列取得の設計が必要。

## 試用手順
`docs/mac_trial.md`（JaLCの少量収集）と、`tools/sample_sites.py --list`（siteId別の件数一覧）を参照。
