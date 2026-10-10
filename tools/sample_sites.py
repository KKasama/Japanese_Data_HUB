"""JaLCのsiteId（J-STAGE, J-STAGE Data, Jxiv, NDL ...）ごとに、少量ずつ取得して構造の違いを調べる。

    python3 tools/sample_sites.py --list                 # siteIdごとのプレフィックス数を表示（取得はしない）
    python3 tools/sample_sites.py                        # 既定の4つのsiteIdを、各2プレフィックス×5DOI取得
    python3 tools/sample_sites.py --site SI/JST.preprint --prefixes 3 --dois 10

取得後:  python3 -m jhub ingest  →  python3 tools/inspect_jalc.py data/store -o jalc_report.txt
"""
import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from jhub.harvest import harvest_jalc
from jhub.jalc_client import JaLCClient
from jhub.store import make_store

DEFAULT_SITES = ["SI/JST.JSTAGE_Data", "SI/JST.preprint", "SI/NDL.NDL.Search", "SI/JST.JSTAGE"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--site", action="append")
    ap.add_argument("--prefixes", type=int, default=2, help="siteIdごとに試すプレフィックス数")
    ap.add_argument("--dois", type=int, default=5, help="プレフィックスごとの取得DOI数")
    a = ap.parse_args()
    cfg = json.load(open(a.config, encoding="utf-8"))
    j = cfg.get("jalc", {})
    ua = cfg.get("user_agent", "jhub/0.1")
    c = JaLCClient(j.get("base_url", "https://api.japanlinkcenter.org"), user_agent=ua, max_rps=j.get("max_rps", 1.0))
    by = defaultdict(list)
    for i in c.prefixes("JaLC"):
        by[i.get("siteId")].append(i["prefix"])
    if a.list:
        for k, v in sorted(by.items(), key=lambda kv: -len(kv[1])):
            print("%-28s %4d  例: %s" % (k, len(v), ", ".join(v[:4])))
        return
    store = make_store(cfg.get("store", {}))
    for site in a.site or DEFAULT_SITES:
        pf = by.get(site, [])
        print("== %s: プレフィックス %d 件中 %d 件を試行" % (site, len(pf), min(a.prefixes, len(pf))))
        for p in pf[: a.prefixes]:
            try:
                harvest_jalc(c, store, "jalc", prefixes=[p], max_dois=a.dois)
            except Exception as e:  # 1つ失敗しても他を続ける
                print("  ! %s: %s" % (p, e))


if __name__ == "__main__":
    main()
