"""CLI:  python -m jhub <command> --config config.json

  harvest-irdb   IRDB(OAI-PMH)を収集（--full で全件、通常は差分）
  harvest-jalc   JaLC REST API を収集（--site SI/JST.JSTAGE などで絞込み、--max-dois で上限、--per-prefix で1誌あたりの上限）
  import-jalc-file FILE.json  JaLC検索画面で「JSONで保存」したファイルを生データとして保存（その後 ingest）
  export-topics-input --out FILE  分野の自動推定ツールへ渡す入力JSONLを書き出す（--limit, --site）
  import-topics FILE [--topics-version V]  ツールの出力JSONLを受け取って保存
  ingest         生データを振り分け・整形してDBへ
  enrich-ror     RORが無い所属をROR公式APIで照合して付与（--max-dois で照会数の上限）。生データは変更しない
  load-repositorylist FILE.csv  IRDBの参加リポジトリ一覧CSVから、機関コードに機関名を付ける
  load-institutions FILE.csv   機関台帳（code,name_ja,name_en,ror,url）の読み込み
  serve          OAI-PMH / API を提供
  export         バルクファイルを出力（既定は機関/サイト別、--by journal でジャーナル別）
  stats          件数の確認
"""
import argparse
import json
import sys

from . import __version__
from .db import connect, load_institutions_csv
from .export import export_bulk, export_by_journal
from .harvest import harvest_jalc, harvest_oai
from .ingest import ingest
from .jalc_client import JaLCClient
from .oai_client import OAIClient
from .server import Hub, serve
from .store import make_store


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jhub", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command")
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--reprocess", action="store_true", help="ingest: 取り込み済みの生データも整形し直す")
    ap.add_argument("--site")
    ap.add_argument("--prefix", action="append")
    ap.add_argument("--max-dois", type=int)
    ap.add_argument("--topics-version", default="")
    ap.add_argument("--all", action="store_true", help="export-topics-input: 推定済みの記録も含める")
    ap.add_argument("--per-prefix", type=int, help="harvest-jalc: 1プレフィックスあたりの最大件数（試験用）")
    ap.add_argument("--max-pages", type=int)
    ap.add_argument("--retry-unmatched", action="store_true", help="enrich-ror: 「一致なし」だった所属も、方式を変えた後にやり直す")
    ap.add_argument("--set")
    ap.add_argument("--out", default="export")
    ap.add_argument("--since")
    ap.add_argument("--by", choices=["source", "journal"], default="source", help="export: 出力の分け方（既定: 機関/サイト別、journal: ジャーナル別）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args(argv)
    cfg = json.load(open(a.config, encoding="utf-8"))
    store = make_store(cfg.get("store", {}))
    irdb = cfg.get("irdb", {})
    jalc = cfg.get("jalc", {})
    ua = cfg.get("user_agent", "jhub/%s (+%s)" % (__version__, cfg.get("admin_email", "")))

    if a.command == "harvest-irdb":
        c = OAIClient(irdb["base_url"], user_agent=ua, delay=irdb.get("delay", 1.0))
        harvest_oai(c, store, "irdb", irdb.get("metadata_prefix", "jpcoar_2.0"),
                    set_spec=a.set or irdb.get("set"), full=a.full, max_pages=a.max_pages)
    elif a.command == "harvest-jalc":
        c = JaLCClient(jalc.get("base_url", "https://api.japanlinkcenter.org"), user_agent=ua,
                       max_rps=jalc.get("max_rps", 1.0))
        harvest_jalc(c, store, "jalc", prefixes=a.prefix, site_id=a.site or jalc.get("site_id"),
                     full=a.full, max_dois=a.max_dois, per_prefix=a.per_prefix)
    elif a.command == "import-jalc-file":
        from .util import utc_now
        from .jalc_screen import records as screen_records
        raw = open(a.arg, "rb").read()
        n = sum(1 for _ in screen_records(raw))   # 形式の確認（読めなければここで止まる）
        run = utc_now().replace(":", "").replace("-", "")
        key = "raw/jalc/%s/screen/%s.json" % (run, __import__("os").path.splitext(__import__("os").path.basename(a.arg))[0])
        store.put(key, raw)
        print("保存しました: %s（%d件）。続けて ingest を実行してください。" % (key, n))
    elif a.command == "export-topics-input":
        from .topics import export_topics_input
        n = export_topics_input(cfg["db"], a.out if a.out != "export" else "topics_input.jsonl",
                                only_missing=not a.all, site=a.site, limit=a.max_dois)
        print("書き出しました: %d件（--max-dois で件数、--site でサイト、--all で推定済みも含める）" % n)
    elif a.command == "import-topics":
        from .topics import import_topics
        print(import_topics(cfg["db"], a.arg, version=a.topics_version))
    elif a.command == "ingest":
        conn = connect(cfg["db"])
        ingest(store, conn, "irdb", "oai", irdb.get("metadata_prefix", "jpcoar_2.0"), irdb.get("id_regex"),
               reprocess=a.reprocess, abstracts=cfg.get("abstracts"), ror=cfg.get("ror"))
        ingest(store, conn, "jalc", "jalc", reprocess=a.reprocess, abstracts=cfg.get("abstracts"), ror=cfg.get("ror"))
    elif a.command == "enrich-ror":
        from . import ror as R
        rc = R.resolve_config(dict(cfg.get("ror") or {}, enabled=True))
        client = R.RorClient(rc["base_url"], rc["delay"], rc["client_id"], ua)
        R.enrich(connect(cfg["db"]), client, rc, limit=a.max_dois, retry=a.retry_unmatched)
    elif a.command == "load-repositorylist":
        from .institutions import load_repositorylist
        r = load_repositorylist(cfg["db"], a.arg)
        print("機関名を付けた: %d件 / 一覧の行数: %d" % (r["matched"], r["rows"]))
        if r["unmatched"]:
            print("対応づけられなかったコード（一覧にDOIプレフィックスがない等）:", ", ".join(r["unmatched"]))
        if r["conflicts"]:
            print("複数の候補があり、多いほうを採用したコード:", ", ".join(r["conflicts"]))
    elif a.command == "load-institutions":
        print(load_institutions_csv(connect(cfg["db"]), a.arg), "件")
    elif a.command == "serve":
        hub = Hub(cfg["db"], cfg.get("hub_base_url", "http://%s:%d/oai" % (a.host, a.port)),
                  cfg.get("admin_email", "admin@example.org"), store=store)
        srv = serve(hub, a.host, a.port)
        print("serving on http://%s:%d  (画面: /  、/oai, /api/*, /health)" % (a.host, a.port))
        srv.serve_forever()
    elif a.command == "export":
        (export_by_journal(cfg["db"], a.out, a.since, a.site) if a.by == "journal" else export_bulk(cfg["db"], a.out, a.since))
    elif a.command == "stats":
        code, d = Hub(cfg["db"]).api("/api/stats", {})
        print(json.dumps(d, ensure_ascii=False, indent=1))
    else:
        ap.print_help()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
