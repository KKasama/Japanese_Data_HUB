"""バルクファイル出力（OpenAlexへの引渡し方式の候補その2）。機関別・データ源別の JSONL.gz。"""
import gzip
import json
import os

from .db import connect


def export_bulk(db_path, out_dir, since=None, log=print):
    os.makedirs(out_dir, exist_ok=True)
    conn = connect(db_path)
    files, counts = {}, {}
    try:
        sql = "SELECT * FROM records WHERE deleted=0"
        args = []
        if since:
            sql += " AND hub_datestamp>=?"
            args.append(since)
        for r in conn.execute(sql + " ORDER BY hub_datestamp, identifier", args):
            name = ("inst_%s" % r["institution_code"]) if r["institution_code"] else (
                "site_%s" % (r["site_id"] or "none").split("/")[-1].replace(".", "_"))
            if name not in files:
                files[name] = gzip.open(os.path.join(out_dir, name + ".jsonl.gz"), "wt", encoding="utf-8")
                counts[name] = 0
            work = json.loads(r["work_json"])
            work["hub"] = {"identifier": r["identifier"], "updated": r["hub_datestamp"],
                           "institution_code": r["institution_code"], "journal_key": r["journal_key"]}
            files[name].write(json.dumps(work, ensure_ascii=False) + "\n")
            counts[name] += 1
        deleted = [x[0] for x in conn.execute(
            "SELECT identifier FROM records WHERE deleted=1" + (" AND hub_datestamp>=?" if since else ""),
            args)]
    finally:
        for f in files.values():
            f.close()
        conn.close()
    manifest = {"files": counts, "deleted_identifiers": deleted, "since": since}
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    log("[export] %s" % counts)
    return manifest


def export_by_journal(db_path, out_dir, since=None, site=None, log=print):
    """ジャーナル別（ISSN等）の JSONL.gz を書き出す。1ファイル=1ジャーナル。manifest.json に一覧を付ける。"""
    from .util import slug
    os.makedirs(out_dir, exist_ok=True)
    conn = connect(db_path)
    journals = []
    try:
        w, a = ["deleted=0", "journal_key IS NOT NULL"], []
        if since:
            w.append("hub_datestamp>=?")
            a.append(since)
        if site:
            w.append("site_id=?")
            a.append(site)
        keys = [r[0] for r in conn.execute("SELECT DISTINCT journal_key FROM records WHERE %s ORDER BY 1" % " AND ".join(w), a)]
        for k in keys:
            fn = "jnl_%s.jsonl.gz" % slug(k)
            n, title, prefixes = 0, None, set()
            with gzip.open(os.path.join(out_dir, fn), "wt", encoding="utf-8") as f:
                for r in conn.execute("SELECT * FROM records WHERE %s AND journal_key=? ORDER BY hub_datestamp, identifier"
                                      % " AND ".join(w), a + [k]):
                    work = json.loads(r["work_json"])
                    work["hub"] = {"identifier": r["identifier"], "updated": r["hub_datestamp"],
                                   "institution_code": r["institution_code"], "journal_key": k}
                    f.write(json.dumps(work, ensure_ascii=False) + "\n")
                    n += 1
                    title = title or r["journal_title"]
                    if r["doi_prefix"]:
                        prefixes.add(r["doi_prefix"])
            journals.append({"journal_key": k, "title": title, "doi_prefixes": sorted(prefixes), "records": n, "file": fn})
    finally:
        conn.close()
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"by": "journal", "since": since, "site": site, "journals": journals}, f, ensure_ascii=False, indent=1)
    log("[export] ジャーナル %d 件" % len(journals))
    return journals
