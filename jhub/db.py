"""SQLite（試作）。本番ではPostgreSQL等に差し替え可能なよう、SQLは単純に保つ。"""
import csv
import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS records(
  identifier TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  datestamp TEXT,
  hub_datestamp TEXT NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0,
  institution_code TEXT,
  institution_how TEXT,
  journal_key TEXT,
  journal_title TEXT,
  doi TEXT,
  doi_prefix TEXT,
  site_id TEXT,
  native_prefix TEXT,
  native_xml TEXT,
  provenance_json TEXT,
  work_json TEXT,
  raw_key TEXT
);
CREATE INDEX IF NOT EXISTS ix_rec_hub ON records(hub_datestamp, identifier);
CREATE INDEX IF NOT EXISTS ix_rec_inst ON records(institution_code);
CREATE INDEX IF NOT EXISTS ix_rec_jnl ON records(journal_key);
CREATE INDEX IF NOT EXISTS ix_rec_pfx ON records(doi_prefix);
CREATE TABLE IF NOT EXISTS record_sets(
  identifier TEXT NOT NULL,
  setspec TEXT NOT NULL,
  PRIMARY KEY(identifier, setspec)
);
CREATE INDEX IF NOT EXISTS ix_set ON record_sets(setspec, identifier);
CREATE TABLE IF NOT EXISTS institutions(
  code TEXT PRIMARY KEY, name_ja TEXT, name_en TEXT, ror TEXT, url TEXT
);
CREATE TABLE IF NOT EXISTS ingested(raw_key TEXT PRIMARY KEY);
"""


def connect(path):
    c = sqlite3.connect(path, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def upsert_record(c, r, hub_now):
    """変化があった場合のみ hub_datestamp を更新して保存。戻り値: 'new'|'changed'|'same'。"""
    old = c.execute("SELECT * FROM records WHERE identifier=?", (r["identifier"],)).fetchone()
    work_json = json.dumps(r["work"], ensure_ascii=False, sort_keys=True) if r.get("work") else None
    if r["deleted"] and old is not None:
        # 削除レコードには本体がない。分類は以前の値を引き継ぐ（セットに墓石を残すため）。
        for k in ("institution_code", "institution_how", "journal_key", "journal_title",
                  "doi", "doi_prefix", "site_id"):
            if not r.get(k):
                r[k] = old[k]
        if old["deleted"] and old["datestamp"] == r.get("datestamp"):
            return "same"
    elif old is not None and not old["deleted"] and old["datestamp"] == r.get("datestamp") \
            and old["work_json"] == work_json and old["native_xml"] == r.get("native_xml") \
            and old["institution_code"] == r.get("institution_code") and old["journal_key"] == r.get("journal_key"):
        return "same"
    c.execute(
        """INSERT INTO records(identifier,source,datestamp,hub_datestamp,deleted,institution_code,
             institution_how,journal_key,journal_title,doi,doi_prefix,site_id,native_prefix,native_xml,
             provenance_json,work_json,raw_key)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(identifier) DO UPDATE SET source=excluded.source, datestamp=excluded.datestamp,
             hub_datestamp=excluded.hub_datestamp, deleted=excluded.deleted,
             institution_code=excluded.institution_code, institution_how=excluded.institution_how,
             journal_key=excluded.journal_key, journal_title=excluded.journal_title, doi=excluded.doi,
             doi_prefix=excluded.doi_prefix, site_id=excluded.site_id, native_prefix=excluded.native_prefix,
             native_xml=excluded.native_xml, provenance_json=excluded.provenance_json,
             work_json=excluded.work_json, raw_key=excluded.raw_key""",
        (r["identifier"], r["source"], r.get("datestamp"), hub_now, 1 if r["deleted"] else 0,
         r.get("institution_code"), r.get("institution_how"), r.get("journal_key"), r.get("journal_title"),
         r.get("doi"), r.get("doi_prefix"), r.get("site_id"), r.get("native_prefix"), r.get("native_xml"),
         json.dumps(r.get("provenance") or {}, ensure_ascii=False), work_json, r.get("raw_key")))
    c.execute("DELETE FROM record_sets WHERE identifier=?", (r["identifier"],))
    for s in r.get("setspecs", []):
        c.execute("INSERT OR IGNORE INTO record_sets(identifier,setspec) VALUES(?,?)", (r["identifier"], s))
    return "new" if old is None else "changed"


def load_institutions_csv(c, path):
    """列: code,name_ja,name_en,ror,url（ListSets拡充・振り分け名称用の台帳）。"""
    n = 0
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            code = (row.get("code") or "").strip()
            if not code:
                continue
            c.execute(
                "INSERT INTO institutions(code,name_ja,name_en,ror,url) VALUES(?,?,?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET name_ja=excluded.name_ja,name_en=excluded.name_en,"
                "ror=excluded.ror,url=excluded.url",
                (code.zfill(5), row.get("name_ja"), row.get("name_en"), row.get("ror"), row.get("url")))
            n += 1
    c.commit()
    return n
