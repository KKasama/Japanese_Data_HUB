"""RORが付かなかった所属・助成機関の一覧を CSV に書き出す。
    python3 tools/ror_report.py [config.json]   → ror_unmatched.csv（出現数の多い順）, ror_matched.csv（確認用）
列: 種別(author/funder), 文字列, 出現件数, 状態(未照会/一致なし/複数機関並記/長すぎ)
"""
import csv
import json
import os
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jhub import ror as R

cfg = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "config.json", encoding="utf-8"))
conn = sqlite3.connect(cfg["db"])
R.set_overrides(R.load_overrides((cfg.get("ror") or {}).get("overrides")))
cache = {k: (ror, name) for k, ror, name in conn.execute("SELECT key, ror, display_name FROM ror_cache")}
cnt = Counter()
matched = Counter()
for (wj,) in conn.execute("SELECT work_json FROM records WHERE deleted=0 AND work_json IS NOT NULL"):
    w = json.loads(wj)
    for a in w.get("authorships") or []:
        has = [i for i in a.get("institutions") or [] if i.get("ror")]
        if has:
            for i in has:
                matched[("author", ", ".join(a.get("raw_affiliation_strings") or []) or "(所属文字列なし)",
                         i["ror"], i.get("display_name") or "", i.get("source") or "raw")] += 1
            continue
        strs = a.get("raw_affiliation_strings") or []
        if not strs:
            cnt[("author", "(所属情報なし)")] += 1
        for s in strs:
            cnt[("author", s)] += 1
    for g in w.get("grants") or []:
        if (g.get("funder") or {}).get("ror"):
            matched[("funder", g.get("funder_display_name") or "", g["funder"]["ror"],
                     g["funder"].get("display_name") or "", g["funder"].get("source") or "raw")] += 1
            continue
        for s in R._funder_strings(g)[:1] or ["(助成機関名なし)"]:
            cnt[("funder", s)] += 1


def status(s):
    if s.startswith("("):
        return "情報なし"
    if R.is_compound(s):
        return "複数機関並記"
    if len(s) > 300:
        return "長すぎ"
    k = R.norm_key(s)
    if R._override(k)[0]:
        return "訂正表で付与なし"
    return "一致なし" if k in cache else "未照会"


with open("ror_unmatched.csv", "w", encoding="utf-8-sig", newline="") as f:
    wr = csv.writer(f)
    wr.writerow(["種別", "文字列", "出現件数", "状態"])
    for (kind, s), n in cnt.most_common():
        wr.writerow([kind, s, n, status(s)])
with open("ror_matched.csv", "w", encoding="utf-8-sig", newline="") as f:
    wr = csv.writer(f)
    wr.writerow(["種別", "元の文字列", "ROR", "ROR上の名称", "由来", "出現件数"])
    for (kind, s, r, d, src), n in matched.most_common():
        wr.writerow([kind, s, r, d, src, n])
by = Counter(status(s) for (_k, s) in cnt)
print("RORなし(種類):", dict(by), " / RORあり(種類):", len(matched))
print("上位10件:")
for (kind, s), n in cnt.most_common(10):
    print("  %3d  [%s] %s  (%s)" % (n, kind, s[:70], status(s)))
