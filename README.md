# jhub — 日本版研究情報ハブ（試作 v0.1）

JaLC（J-STAGE等を含む）とIRDBのメタデータを、**収集 → 生データ保存 → 振り分け → OpenAlex互換形式へ整形 → OAI-PMH／APIで提供**する試作です。Pythonの標準ライブラリのみで動きます（S3/Wasabiを使う場合だけ `boto3`）。

## 現状（2026-10-10）

| 項目 | 状態 |
|---|---|
| 自動テスト 48件（モックのIRDB/JaLCで、収集→再開→差分→再整形→削除→振り分け→提供→出力まで） | 通過 |
| **JaLC（公開API）の実データ** | **確認済み**: J-STAGE、J-STAGE Data、Jxiv、NDL（4種のsiteId・計30件）で、収集→生データ保存→整形→OAI-PMH/API提供まで動作 |
| **IRDBの実データ** | **未確認**: IRDBのレコード取得（ListRecords等）は申請IPからのみ。固定IPの環境を準備し、NIIに手続きを確認中 |
| JaLC JSONの項目 | 実データで確認した項目に対応（ORCID、助成、権利、主題、日付、関連、参考文献DOI、版 など）。他のsiteIdは順次確認 |
| **ROR補完**（著者所属・助成機関） | 実データ（JaLC 228件）で確認済み。照会266種類のうち約190種類にRORが付与。付かないものの一覧は `tools/ror_report.py` で出力 |
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
python3 -m jhub import-jalc-file FILE.json   # JaLC検索画面で「JSONで保存」したファイルを生データとして保存（その後 ingest）
python3 -m jhub export-topics-input --out topics_input.jsonl   # 分野の自動推定ツールへ渡す入力
python3 -m jhub import-topics 推定結果.jsonl --topics-version v3   # ツールの出力を受け取って保存
python3 -m jhub load-repositorylist repositorylist.csv   # IRDBの参加リポジトリ一覧CSVから、機関コードに機関名を付ける
python3 -m jhub ingest                  # 取り込み。整形ルールを変えた後は --reprocess で再整形
python3 -m jhub load-institutions institutions.csv   # 機関台帳(code,name_ja,name_en,ror,url)
python3 -m jhub enrich-ror --retry-unmatched   # RORが無い所属・助成機関をROR公式APIで照合して付与（下の「ROR補完」）
python3 tools/ror_report.py              # RORが付かなかった一覧 ror_unmatched.csv と付いたもの ror_matched.csv を出力
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
| 整形（JPCOAR→work、JaLC JSON→work。HTMLタグ除去、プレースホルダ除去。抄録は `policy.py` の方針で出力可否を切替。有無は常に記録） | `normalize.py` |
| 機関名の台帳（IRDB参加リポジトリ一覧CSV → 機関コードに対応づけ） | `institutions.py` |
| 分野の自動推定の受け渡し（別表に保存。API・画面に「推定」として表示） | `topics.py` |
| JaLC検索画面のJSON形式 → REST v2形式への変換（REST由来の記録は上書きしない） | `jalc_screen.py` |
| ROR補完（生データにRORが無い所属・助成機関のみ。ROR公式APIで照合、結果はDBにキャッシュ） | `ror.py` `tools/ror_report.py` `ror_overrides.csv` |
| DB（SQLite。変更時のみ更新日時を進める） | `db.py` `ingest.py` |
| 提供（OAI-PMH 2.0、取得API） | `server.py` |
| 引渡し（バルクJSONL.gz） | `export.py` |

### OAI-PMH のセット
`irdb`（IRDB由来全部）／`inst:<5桁機関コード>`／`site:<JST.JSTAGE など>`／`jnl:<ISSN>`／`pfx:<DOIプレフィックス>`。
形式は `jpcoar_2.0`（IRDBの原XML。機関別セットで配る）と `jhub_work`（整形済み。JaLC等）。削除はヘッダーの `status="deleted"` で伝えます。

## ROR補完（著者所属・助成機関）

**方針: 生データは変えない。** ROR付与は整形後のJSON（`work_json`）にだけ行います。

- 生データにRORが既にある所属（JPCOARの`affiliation`/`nameIdentifier`[ROR]、`funderIdentifier`[ROR]）は、そのまま使い、触りません。
- RORが無い所属（`raw_affiliation_strings`）と助成機関名（`grants[].funder_display_name` / `funder_names`）だけを、**ROR公式API（v2 `affiliation`）**で照合します。IRDB登録機関の台帳（`institutions.ror`）が空なら、同様に補います。
- 付与したものには `"source": "ror_affiliation_api"`、`score`、照合に使った文字列（`matched_string`）が付き、生データ由来と区別できます。助成機関は `grants[].funder` に入ります。
- 照合結果は `ror_cache` に保存します（同じ文字列は二度と照会しません。「一致なし」も記録）。`ingest --reprocess` で整形し直してもキャッシュから再付与されます。**ingest中は通信しません。** API照会は `enrich-ror` だけです。
- 更新したレコードは `hub_datestamp` が進むため、OAI-PMHの差分取得で配信されます。
- 後継組織（例: 東京工業大学→Institute of Science Tokyo）は、ここでは扱いません。OpenAlex等での機関の統合に任せ、**旧名のRORをそのまま付与**します。

### 設定（`config.json`）

```json
"ror": {"enabled": true, "min_score": 0.9, "delay": 0.2, "client_id": null, "overrides": "ror_overrides.csv"}
```

`enabled` が true のとき、ingestがキャッシュを適用します。`client_id` はROR APIの利用上限を緩めたいときに設定します。

### 誤付与を防ぐ仕組み

1. APIが「確信あり（`chosen: true`）」と判定し、かつ `min_score` 以上のものだけを採用します（ROR公式は`chosen`で選ぶことを推奨。`min_score`は安全側の追加条件で、0にすれば`chosen`のみになります）。
2. 採用した組織の名称（別名・日本語名を含む）が所属文字列と無関係なものは捨てます（例: Purdue University→無関係な大学、を防ぐ）。
3. 複数機関が1文字列に並んだもの（`/` `、` `;` `·` など）は自動照合しません。
4. APIが同点で`chosen`を付けなかった場合は、所属文字列がちょうど1組織の正式名・別名と**完全一致**するときだけ採用します。
5. 一致しない場合の再照会は**名称の完全一致**だけ: 日本語は先頭の機関名（「東京大学大学院工学系研究科」→「東京大学」）、英語は末尾側のカンマ区切り（「Dept of X, Chiba University」→「Chiba University」）。
6. 手動の訂正表 `ror_overrides.csv` が、常に最優先です。

### 訂正表 `ror_overrides.csv`

列は `query,ror`。`ror` が `none` の行は「付与しない」。`query` が `re:` で始まる行は正規表現（NFKC・小文字化した文字列に`re.search`）。**カンマを含む文字列は書けません**（`.?` などで代用）。

```
query,ror
re:tokyo institute of technology|東京工業大学,https://ror.org/0112mx960
University of Occupational and Environmental Health,https://ror.org/020p3h829
re:茜会|akane medical,none
```

編集後は `python3 -m jhub enrich-ror --retry-unmatched` を実行すると反映されます。誤付与が見つかったら、DBの `ror_cache` を空にして（`delete from ror_cache`）再実行してください。

### 付かなかったものの確認

```
python3 tools/ror_report.py        # ror_unmatched.csv（付かなかった一覧、件数順） ror_matched.csv（付いたもの）
```

付かない主な理由は、①所属情報が無い、②RORに未登録（企業・病院・小さな団体）、③歴史的な名称（帝國大學など）、④複数機関の並記、⑤日本語の部局付き表記（APIの照合が弱い。先頭の機関名の完全一致で補っている）です。件数が多いものから、ROR上でIDを確認して訂正表に足していく運用を想定しています。

## 既知の限界・要確認（実データで確認するもの）

1. **IRDBのレコード取得は申請IPのみ**（Identify等は通るがListRecordsは `badArgument`）。固定IPの環境の登録手続きをNIIに確認中。
2. **機関の判別**は、ヘッダーの機関コード（setSpec／識別子）を使う。`irdbProvenance` は構造確認後に追加する。
3. **JaLCの全件取得**はDOI1件ごとの取得になり現実的でない（約1,470万DOI）。JaLC OAI-PMH（`/v2/oai`）の利用可否をJSTに確認する。規約第7条により、大量取得はJaLCが許諾する方法に限る。
4. JaLC REST では**削除されたDOIを検出できない**。OAI-PMHなら可能な可能性がある。
5. JaLC JSON の項目は、J-STAGE等4種の実データで確認済み。他のsiteId（約2,400のうちJAIRO等）は順次確認する。
6. OpenAlexへの引渡し方式（OAI-PMH提供／バルクファイル）は、OpenAlexとの合意が前提。
7. 整形は主要項目に加え、ORCID・助成・権利・主題・関連・参考文献DOIなどに対応。JST対応表（79項目）との照合は今後。抄録は供給元ごとに切替可能（config の `abstracts`）。既定は IRDB=出力、JaLC=出さない（JSTから書誌情報のみと連絡を受けているため）。JaLCは `permitted_only`（検索画面形式の第三者利用「可」のみ）／`include` に切替でき、変更後は `ingest --reprocess` で生データから反映する（再収集不要）。REST v2 には第三者利用の可否項目がないため、`permitted_only` では REST 由来の記録は出力されない。各記録に `abstract_status`（included / withheld_by_policy / not_permitted / none）が付く。
8. 型の対応は暫定（JA→article、RD→dataset、BK→book、Jxiv→preprint）。
9. SQLite・単一プロセスの試作。全件規模ではPostgreSQL等と、並列取得の設計が必要。

## 試用手順
`docs/mac_trial.md`（JaLCの少量収集）と、`tools/sample_sites.py --list`（siteId別の件数一覧）を参照。
