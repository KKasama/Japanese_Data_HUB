"""生データ → 分類 → 整形 → DB。ページ単位で冪等（取り込み済みはスキップ）。"""
import json
import xml.etree.ElementTree as ET

from . import normalize as N
from .classify import institution_code, provenance_institution_name
from .db import upsert_record
from .oai_client import q
from .util import kid, kids, ln, slug, text, utc_now

# 直列化時の接頭辞を読みやすく保つ
for _p, _u in [("jpcoar", "https://github.com/JPCOAR/schema/blob/master/2.0/"),
               ("jpcoar1", "https://github.com/JPCOAR/schema/blob/master/1.0/"),
               ("dcterms", "http://purl.org/dc/terms/"),
               ("datacite", "https://schema.datacite.org/meta/kernel-4/"),
               ("oaire", "http://namespace.openaire.eu/schema/oaire/"),
               ("dcndl", "http://ndl.go.jp/dcndl/terms/"),
               ("oai_dc", "http://www.openarchives.org/OAI/2.0/oai_dc/")]:
    try:
        ET.register_namespace(_p, _u)
    except ValueError:
        pass


def _flatten(el):
    """irdbProvenance 等を {要素名: 文字列} に平坦化（構造が確定するまでの暫定）。"""
    out = {}
    for e in el.iter():
        t = (e.text or "").strip()
        if t:
            out.setdefault(ln(e), t)
    return out


def _sets_for(r):
    s = ["irdb"] if r["source"] == "irdb" else []
    if r.get("institution_code"):
        s.append("inst:" + r["institution_code"])
    if r.get("site_id"):
        s.append("site:" + slug(r["site_id"].split("/")[-1]))
    if r.get("journal_key"):
        s.append("jnl:" + slug(r["journal_key"]))
    if r.get("doi_prefix"):
        s.append("pfx:" + slug(r["doi_prefix"]))
    return s


def records_from_oai_page(raw, source, native_prefix, raw_key, id_regex):
    root = ET.fromstring(raw)
    for rec in root.iter(q("record")):
        h = rec.find(q("header"))
        ident = text(h.find(q("identifier")))
        ds = text(h.find(q("datestamp")))
        sets = [text(s) for s in h.findall(q("setSpec"))]
        deleted = h.get("status") == "deleted"
        code, how, mismatch = institution_code(ident, sets, id_regex)
        r = {"source": source, "identifier": ident, "datestamp": ds, "deleted": deleted,
             "institution_code": code, "institution_how": how + ("(不一致あり)" if mismatch else ""),
             "native_prefix": native_prefix, "raw_key": raw_key, "provenance": {}, "work": None,
             "journal_key": None, "journal_title": None, "doi": None, "doi_prefix": None, "site_id": None,
             "native_xml": None}
        if not deleted:
            m = rec.find(q("metadata"))
            meta = next(iter(m), None) if m is not None else None
            if meta is not None:
                w = N.normalize_jpcoar(meta, ident)
                r.update(work=w, journal_key=w["journal_key"], journal_title=w["journal_title"],
                         doi=w["doi"], doi_prefix=w["doi_prefix"],
                         native_xml=ET.tostring(meta, encoding="unicode"))
            for ab in rec.findall(q("about")):
                for child in ab:
                    if "provenance" in ln(child).lower():
                        r["provenance"][ln(child)] = _flatten(child)
        r["setspecs"] = _sets_for(r)
        yield r


def records_from_jalc_chunk(raw, source, raw_key):
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        data = d.get("data") or {}
        if not data.get("doi"):
            continue
        w = N.normalize_jalc(data)
        r = {"source": source, "identifier": "doi:" + w["doi"].replace("https://doi.org/", ""),
             "datestamp": data.get("updated_date"), "deleted": False,
             "institution_code": None, "institution_how": None,
             "journal_key": w["journal_key"], "journal_title": w["journal_title"],
             "doi": w["doi"], "doi_prefix": w["doi_prefix"], "site_id": data.get("siteId"),
             "native_prefix": None, "native_xml": None, "provenance": {}, "work": w, "raw_key": raw_key}
        r["setspecs"] = _sets_for(r)
        yield r


def ingest(store, conn, source, kind, native_prefix=None, id_regex=None, now=None, log=print):
    """kind: 'oai'（IRDB等のOAI-PMH XML） | 'jalc'（REST JSONL）。"""
    from .classify import DEFAULT_ID_REGEX
    id_regex = id_regex or DEFAULT_ID_REGEX
    done = {r[0] for r in conn.execute("SELECT raw_key FROM ingested")}
    stats = {"new": 0, "changed": 0, "same": 0, "pages": 0, "bad_pages": 0}
    for key in store.list("raw/%s/" % source):
        if key in done:
            continue
        raw = store.get(key)
        hub_now = utc_now(now)
        try:
            gen = (records_from_oai_page(raw, source, native_prefix, key, id_regex) if kind == "oai"
                   else records_from_jalc_chunk(raw, source, key))
            for r in gen:
                stats[upsert_record(conn, r, hub_now)] += 1
        except (ET.ParseError, ValueError) as e:
            conn.rollback()
            stats["bad_pages"] += 1
            log("[ingest] 解析できないページをスキップ: %s (%s)" % (key, e))
            continue
        conn.execute("INSERT OR IGNORE INTO ingested(raw_key) VALUES(?)", (key,))
        conn.commit()
        stats["pages"] += 1
    log("[ingest] %s" % stats)
    return stats
