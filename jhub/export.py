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
