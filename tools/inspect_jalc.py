"""収集済みのJaLC生データ(JSONL)の「形」を確認するツール。

    python3 tools/inspect_jalc.py data/store            # 画面に表示
    python3 tools/inspect_jalc.py data/store -o report.txt   # ファイルにも保存（共有用）

各項目のキー名・型・値の例（長い値は切り詰め）を出す。実データの構造を確かめ、変換ルールを固定するために使う。
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter


def short(v, n=60):
    s = json.dumps(v, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "…"


def walk(x, path, out):
    if isinstance(x, dict):
        for k, v in x.items():
            walk(v, path + "." + k, out)
    elif isinstance(x, list):
        out[path + "[]"].append(("list", len(x)))
        for it in x[:3]:
            walk(it, path + "[]", out)
    else:
        out[path].append((type(x).__name__, short(x)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("store")
    ap.add_argument("-o", "--out")
    ap.add_argument("-n", type=int, default=50, help="調べるレコード数の上限")
    a = ap.parse_args()
    files = sorted(glob.glob(os.path.join(a.store, "raw", "jalc", "**", "*.jsonl"), recursive=True))
    if not files:
        print("JaLCの生データが見つかりません:", a.store)
        return 1
    recs = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    recs.append(json.loads(line))
                if len(recs) >= a.n:
                    break
    out = {}
    from collections import defaultdict
    out = defaultdict(list)
    for r in recs:
        walk(r.get("data", r), "data", out)
    lines = ["レコード数: %d  ファイル数: %d" % (len(recs), len(files)), ""]
    types = Counter((r.get("data") or {}).get("content_type") for r in recs)
    sites = Counter((r.get("data") or {}).get("siteId") for r in recs)
    lines += ["content_type: %s" % dict(types), "siteId: %s" % dict(sites), "",
              "項目（パス / 型 / 値の例 / 出現数）:"]
    for p in sorted(out):
        vals = out[p]
        ex = vals[0]
        lines.append("  %-45s %-6s %-64s x%d" % (p, ex[0], ex[1], len(vals)))
    text = "\n".join(lines)
    print(text)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print("\n保存しました:", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
