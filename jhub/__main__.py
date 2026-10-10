"""CLI:  python -m jhub <command> --config config.json

  harvest-irdb   IRDB(OAI-PMH)を収集（--full で全件、通常は差分）
  harvest-jalc   JaLC REST API を収集（--site SI/JST.JSTAGE などで絞込み、--max-dois で上限）
  ingest         生データを振り分け・整形してDBへ
  load-institutions FILE.csv   機関台帳（code,name_ja,name_en,ror,url）の読み込み
  serve          OAI-PMH / API を提供
  export         機関別バルクファイルを出力
  stats          件数の確認
"""
import argparse
import json
import sys

from . import __version__
from .db import connect, load_institutions_csv
from .export import export_bulk
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
    ap.add_argument("--max-pages", type=int)
    ap.add_argument("--set")
    ap.add_argument("--out", default="export")
    ap.add_argument("--since")
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
                     full=a.full, max_dois=a.max_dois)
    elif a.command == "ingest":
        conn = connect(cfg["db"])
        ingest(store, conn, "irdb", "oai", irdb.get("metadata_prefix", "jpcoar_2.0"), irdb.get("id_regex"),
               reprocess=a.reprocess)
        ingest(store, conn, "jalc", "jalc", reprocess=a.reprocess)
    elif a.command == "load-institutions":
        print(load_institutions_csv(connect(cfg["db"]), a.arg), "件")
    elif a.command == "serve":
        hub = Hub(cfg["db"], cfg.get("hub_base_url", "http://%s:%d/oai" % (a.host, a.port)),
                  cfg.get("admin_email", "admin@example.org"))
        srv = serve(hub, a.host, a.port)
        print("serving on http://%s:%d  (/oai, /api/*, /health)" % (a.host, a.port))
        srv.serve_forever()
    elif a.command == "export":
        export_bulk(cfg["db"], a.out, a.since)
    elif a.command == "stats":
        code, d = Hub(cfg["db"]).api("/api/stats", {})
        print(json.dumps(d, ensure_ascii=False, indent=1))
    else:
        ap.print_help()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
