"""分野（OpenAlexトピック）の自動推定結果の受け渡し。

推定そのものは、別の分類ツール（OpenAlex-Topics-assignment-for-IRDB2 など。埋め込み＋BM25＋翻訳）が行う。
ハブは (1) 分類ツールへ渡す入力JSONLを書き出し、(2) ツールの出力JSONLを受け取って保存し、
(3) API・画面に「推定」として表示する。推定は原データや書誌の整形結果とは別の表に持つので、
再取り込み(--reprocess)をしても消えず、方法・版・時刻が残る。
"""
import json

from .db import connect
from .util import utc_now


def export_topics_input(db_path, out_path, only_missing=True, site=None, limit=None, with_keywords=True):
    """分類ツール入力（1行1件: id, title, abstract, language, ndc_codes）を書く。件数を返す。

    抄録を出力している記録はその抄録を、無いときは with_keywords=True なら著者キーワードを abstract 欄に入れる
    （題名だけより手がかりが増える。使った場合は結果の入力が分かるよう、キーワード数を記録しない点に注意）。
    """
    c = connect(db_path)
    w, a = ["r.deleted=0", "r.work_json IS NOT NULL"], []
    if only_missing:
        w.append("r.identifier NOT IN (SELECT identifier FROM topics)")
    if site:
        w.append("r.site_id=?")
        a.append(site)
    sql = "SELECT r.identifier, r.work_json FROM records r WHERE %s ORDER BY r.identifier" % " AND ".join(w)
    if limit:
        sql += " LIMIT %d" % int(limit)
    n = 0
    with open(out_path, "w", encoding="utf-8") as f:
        for r in c.execute(sql, a):
            work = json.loads(r["work_json"])
            title = work.get("title") or work.get("display_name")
            if not title:
                continue
            kws = [k.get("keyword") for k in work.get("keywords") or [] if k.get("keyword")]
            ab = next((a["text"] for a in work.get("abstracts") or [] if a.get("text")), "")
            if not ab and with_keywords and kws:
                ab = "、".join(kws)   # 抄録を出力していないときは、著者キーワードで代用する
            rec = {"id": r["identifier"], "title": title,
                   "abstract": ab,
                   "language": work.get("language"),
                   "ndc_codes": ([work["book_classification"]] if work.get("book_classification") else None)}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    c.close()
    return n


def import_topics(db_path, in_path, version="", now=None):
    """分類ツールの出力（1行1件: work_id, primary_topic, topics[], method …）を保存する。

    保存する前に、ハブに存在しない識別子や、トピックのない行は数えて除く。
    戻り値: {"saved": n, "unknown": m, "empty": k}
    """
    c = connect(db_path)
    known = {r[0] for r in c.execute("SELECT identifier FROM records")}
    st = {"saved": 0, "unknown": 0, "empty": 0}
    ts = utc_now(now)
    for line in open(in_path, encoding="utf-8"):
        if not line.strip():
            continue
        d = json.loads(line)
        ident = d.get("work_id") or d.get("id")
        if ident not in known:
            st["unknown"] += 1
            continue
        tops = d.get("topics") or ([d["primary_topic"]] if d.get("primary_topic") else [])
        if not tops:
            st["empty"] += 1
            continue
        out = {"primary_topic": d.get("primary_topic") or tops[0], "topics": tops,
               "method": d.get("method"), "emb_score": d.get("emb_score"), "bm25_score": d.get("bm25_score")}
        c.execute("INSERT INTO topics(identifier,topics_json,method,version,assigned_at) VALUES(?,?,?,?,?) "
                  "ON CONFLICT(identifier) DO UPDATE SET topics_json=excluded.topics_json, method=excluded.method, "
                  "version=excluded.version, assigned_at=excluded.assigned_at",
                  (ident, json.dumps(out, ensure_ascii=False), d.get("method"), version, ts))
        st["saved"] += 1
    c.commit()
    c.close()
    return st
