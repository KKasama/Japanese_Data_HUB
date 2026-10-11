"""ハブの提供側: OAI-PMH 2.0 プロバイダ（機関別・ジャーナル別セット）と JSON 取得API。

OAI-PMH:
  /oai?verb=Identify|ListMetadataFormats|ListSets|ListIdentifiers|ListRecords|GetRecord
  metadataPrefix: jpcoar_2.0 （IRDB由来の原XMLをそのまま。OpenAlexが既存の取り込みで処理できる形）
                  jhub_work  （全データ源の整形済みJSON。JaLC等はこちら。引渡し方式はOpenAlexと要合意）
  set: irdb | inst:<5桁機関コード> | site:<例 JSTAGE> | jnl:<ISSN等> | pfx:<DOIプレフィックス>
  日時粒度は秒。datestamp はハブが内容の変化を取り込んだ時刻（hub_datestamp）。
API:
  /api/stats  /api/institutions  /api/journals  /api/sites  /api/works  /api/works/<identifier>
  /api/raw/<identifier>（原データ）  /api/status（収集状況）  /health
画面:
  /  （ダッシュボード・検索・ダウンロード・収集状況。jhub/ui/index.html）
"""
import base64
import json
import os
import re
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse
from xml.sax.saxutils import escape

from .db import connect
from .util import utc_now

OAI_NS = "http://www.openarchives.org/OAI/2.0/"
PREFIXES = {
    "jpcoar_2.0": ("http://irdb.nii.ac.jp/oai/jpcoar.xsd", "http://irdb.nii.ac.jp/oai"),
    "jhub_work": ("urn:jhub:work:1.0", "urn:jhub:work:1.0"),
}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
VERBS = {
    "Identify": (set(), set()),
    "ListMetadataFormats": (set(), {"identifier"}),
    "ListSets": (set(), {"resumptionToken"}),
    "ListIdentifiers": ({"metadataPrefix"}, {"from", "until", "set", "resumptionToken"}),
    "ListRecords": ({"metadataPrefix"}, {"from", "until", "set", "resumptionToken"}),
    "GetRecord": ({"identifier", "metadataPrefix"}, set()),
}


class OAIFault(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


def _b64e(d):
    return base64.urlsafe_b64encode(json.dumps(d, separators=(",", ":")).encode()).decode()


def _b64d(s):
    try:
        return json.loads(base64.urlsafe_b64decode(s.encode()))
    except Exception:
        raise OAIFault("badResumptionToken", "invalid resumptionToken")


class Hub:
    def __init__(self, db_path, base_url="http://localhost:8080/oai", admin_email="admin@example.org",
                 repo_name="Japan Scholarly Metadata Hub (prototype)", page_size=100,
                 max_listed_journal_sets=2000, now=None, store=None):
        self.db_path, self.base_url, self.admin = db_path, base_url, admin_email
        self.repo_name, self.page_size = repo_name, page_size
        self.max_jsets = max_listed_journal_sets
        self.now = now
        self.store = store

    # ---------------- OAI-PMH ----------------
    def oai(self, params):
        """params: {name: [values]} → XML文字列。"""
        flat, attrs = {}, {}
        try:
            for k, v in params.items():
                if len(v) != 1:
                    raise OAIFault("badArgument", "repeated argument: %s" % k)
                flat[k] = v[0]
            verb = flat.get("verb")
            if verb not in VERBS:
                raise OAIFault("badVerb", "Illegal OAI verb")
            req, opt = VERBS[verb]
            if "resumptionToken" in flat:
                if set(flat) - {"verb", "resumptionToken"}:
                    raise OAIFault("badArgument", "resumptionToken is exclusive")
            else:
                missing = req - set(flat)
                extra = set(flat) - {"verb"} - req - opt
                if missing or extra:
                    raise OAIFault("badArgument", "missing=%s illegal=%s" % (sorted(missing), sorted(extra)))
            attrs = dict(flat)
            conn = connect(self.db_path)
            try:
                body = getattr(self, "_v_" + verb)(conn, flat)
            finally:
                conn.close()
            return self._wrap(attrs, body)
        except OAIFault as f:
            a = {} if f.code in ("badVerb", "badArgument") else attrs
            return self._wrap(a, '<error code="%s">%s</error>' % (f.code, escape(f.msg)))

    def _wrap(self, attrs, body):
        a = "".join(' %s="%s"' % (k, escape(v, {'"': "&quot;"})) for k, v in attrs.items()
                    if k != "resumptionToken")
        return ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<OAI-PMH xmlns="%s" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
                'xsi:schemaLocation="%s http://www.openarchives.org/OAI/2.0/OAI-PMH.xsd">\n'
                '<responseDate>%s</responseDate>\n<request%s>%s</request>\n%s\n</OAI-PMH>\n'
                % (OAI_NS, OAI_NS, utc_now(self.now), a, escape(self.base_url), body))

    def _v_Identify(self, conn, p):
        e = conn.execute("SELECT MIN(hub_datestamp) FROM records").fetchone()[0] or utc_now(self.now)
        return ("<Identify><repositoryName>%s</repositoryName><baseURL>%s</baseURL>"
                "<protocolVersion>2.0</protocolVersion><adminEmail>%s</adminEmail>"
                "<earliestDatestamp>%s</earliestDatestamp><deletedRecord>persistent</deletedRecord>"
                "<granularity>YYYY-MM-DDThh:mm:ssZ</granularity></Identify>"
                % (escape(self.repo_name), escape(self.base_url), escape(self.admin), e))

    def _v_ListMetadataFormats(self, conn, p):
        out = "".join("<metadataFormat><metadataPrefix>%s</metadataPrefix><schema>%s</schema>"
                      "<metadataNamespace>%s</metadataNamespace></metadataFormat>" % (k, v[0], v[1])
                      for k, v in PREFIXES.items())
        return "<ListMetadataFormats>%s</ListMetadataFormats>" % out

    def _setxml(self, spec, name, desc=None):
        d = ""
        if desc:
            d = ('<setDescription><oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/" '
                 'xmlns:dc="http://purl.org/dc/elements/1.1/">%s</oai_dc:dc></setDescription>' % desc)
        return "<set><setSpec>%s</setSpec><setName>%s</setName>%s</set>" % (escape(spec), escape(name), d)

    def _v_ListSets(self, conn, p):
        if "resumptionToken" in p:
            raise OAIFault("badResumptionToken", "ListSets does not use resumption tokens")
        sets = [self._setxml("irdb", "IRDB由来の全レコード")]
        inst = {r["code"]: r for r in conn.execute("SELECT * FROM institutions")}
        for r in conn.execute("SELECT institution_code c, COUNT(*) n FROM records "
                              "WHERE institution_code IS NOT NULL GROUP BY c ORDER BY c"):
            i = inst.get(r["c"])
            ja = (i["name_ja"] if i and i["name_ja"] else "機関コード " + r["c"])
            d = ""
            if i:
                if i["name_ja"]:
                    d += '<dc:title xml:lang="ja">%s</dc:title><dc:publisher xml:lang="ja">%s</dc:publisher>' % (
                        escape(i["name_ja"]), escape(i["name_ja"]))
                if i["name_en"]:
                    d += '<dc:title xml:lang="en">%s</dc:title><dc:publisher xml:lang="en">%s</dc:publisher>' % (
                        escape(i["name_en"]), escape(i["name_en"]))
                for k in ("ror", "url"):
                    if i[k]:
                        d += "<dc:identifier>%s</dc:identifier>" % escape(i[k])
            sets.append(self._setxml("inst:" + r["c"], ja, d))
        for r in conn.execute("SELECT DISTINCT site_id FROM records WHERE site_id IS NOT NULL ORDER BY 1"):
            from .util import slug
            sets.append(self._setxml("site:" + slug(r[0].split("/")[-1]), "JaLCサイト " + r[0]))
        from .util import slug
        for r in conn.execute("SELECT journal_key k, MAX(journal_title) t FROM records "
                              "WHERE journal_key IS NOT NULL GROUP BY k ORDER BY k LIMIT ?", (self.max_jsets,)):
            sets.append(self._setxml("jnl:" + slug(r["k"]), r["t"] or r["k"]))
        return "<ListSets>%s</ListSets>" % "".join(sets)

    def _header(self, r):
        sets = [s[0] for s in self._sets_of(r["_conn"], r["identifier"])]
        return ('<header%s><identifier>%s</identifier><datestamp>%s</datestamp>%s</header>'
                % (' status="deleted"' if r["deleted"] else "", escape(r["identifier"]), r["hub_datestamp"],
                   "".join("<setSpec>%s</setSpec>" % escape(s) for s in sets)))

    @staticmethod
    def _sets_of(conn, ident):
        return conn.execute("SELECT setspec FROM record_sets WHERE identifier=? ORDER BY setspec", (ident,)).fetchall()

    def _meta(self, r, prefix):
        if r["deleted"]:
            return ""
        if prefix == "jpcoar_2.0":
            return "<metadata>%s</metadata>" % r["native_xml"]
        return '<metadata><work xmlns="urn:jhub:work:1.0">%s</work></metadata>' % escape(r["work_json"] or "{}")

    def _select(self, conn, prefix, set_, from_, until, after):
        w, a = [], []
        if prefix == "jpcoar_2.0":
            w.append("r.native_prefix IS NOT NULL")
        if set_:
            w.append("EXISTS(SELECT 1 FROM record_sets s WHERE s.identifier=r.identifier AND s.setspec=?)")
            a.append(set_)
        if from_:
            w.append("r.hub_datestamp>=?")
            a.append(from_)
        if until:
            w.append("r.hub_datestamp<=?")
            a.append(until)
        if after:
            w.append("(r.hub_datestamp,r.identifier)>(?,?)")
            a += after
        sql = ("SELECT r.* FROM records r %s ORDER BY r.hub_datestamp, r.identifier LIMIT ?"
               % ("WHERE " + " AND ".join(w) if w else ""))
        return conn.execute(sql, a + [self.page_size + 1]).fetchall()

    def _list(self, conn, p, verb):
        if "resumptionToken" in p:
            t = _b64d(p["resumptionToken"])
            prefix, set_, from_, until, after = t["p"], t["s"], t["f"], t["u"], t["k"]
            resumed = True
        else:
            prefix, set_, from_, until, after = p["metadataPrefix"], p.get("set"), p.get("from"), p.get("until"), None
            resumed = False
            if prefix not in PREFIXES:
                raise OAIFault("cannotDisseminateFormat", "unsupported metadataPrefix")
            for d in (from_, until):
                if d and not DATE_RE.match(d):
                    raise OAIFault("badArgument", "date must be YYYY-MM-DDThh:mm:ssZ")
            if from_ and until and from_ > until:
                raise OAIFault("badArgument", "from is later than until")
            if set_:
                ok = conn.execute("SELECT 1 FROM record_sets WHERE setspec=? LIMIT 1", (set_,)).fetchone()
                if not ok:
                    raise OAIFault("noRecordsMatch", "no records in set")
        rows = self._select(conn, prefix, set_, from_, until, after)
        if not rows and not resumed:
            raise OAIFault("noRecordsMatch", "no matching records")
        more = len(rows) > self.page_size
        rows = rows[: self.page_size]
        out = []
        for r in rows:
            d = dict(r)
            d["_conn"] = conn
            h = self._header(d)
            out.append("<record>%s%s</record>" % (h, self._meta(d, prefix)) if verb == "ListRecords" else h)
        tok = ""
        if more:
            last = rows[-1]
            tok = "<resumptionToken>%s</resumptionToken>" % _b64e(
                {"p": prefix, "s": set_, "f": from_, "u": until, "k": [last["hub_datestamp"], last["identifier"]]})
        elif resumed:
            tok = "<resumptionToken/>"
        return "<%s>%s%s</%s>" % (verb, "".join(out), tok, verb)

    def _v_ListRecords(self, conn, p):
        return self._list(conn, p, "ListRecords")

    def _v_ListIdentifiers(self, conn, p):
        return self._list(conn, p, "ListIdentifiers")

    def _v_GetRecord(self, conn, p):
        if p["metadataPrefix"] not in PREFIXES:
            raise OAIFault("cannotDisseminateFormat", "unsupported metadataPrefix")
        r = conn.execute("SELECT * FROM records WHERE identifier=?", (p["identifier"],)).fetchone()
        if not r:
            raise OAIFault("idDoesNotExist", "unknown identifier")
        d = dict(r)
        if p["metadataPrefix"] == "jpcoar_2.0" and d["native_prefix"] is None:
            raise OAIFault("cannotDisseminateFormat", "record has no jpcoar form")
        d["_conn"] = conn
        return "<GetRecord><record>%s%s</record></GetRecord>" % (self._header(d), self._meta(d, p["metadataPrefix"]))

    # ---------------- JSON API ----------------
    def api(self, path, q):
        """(status, dict)。q は {name: 値} のフラット辞書。"""
        conn = connect(self.db_path)
        try:
            if path == "/api/stats":
                tot = conn.execute("SELECT COUNT(*) FROM records WHERE deleted=0").fetchone()[0]
                dele = conn.execute("SELECT COUNT(*) FROM records WHERE deleted=1").fetchone()[0]
                by_src = {r[0]: r[1] for r in conn.execute(
                    "SELECT source, COUNT(*) FROM records WHERE deleted=0 GROUP BY source")}
                unk = conn.execute("SELECT COUNT(*) FROM records WHERE source='irdb' AND institution_code IS NULL").fetchone()[0]
                by_site = {(r[0] or "(IRDB等)"): r[1] for r in conn.execute(
                    "SELECT site_id, COUNT(*) FROM records WHERE deleted=0 AND source='jalc' "
                    "GROUP BY site_id ORDER BY 2 DESC")}
                by_day = {r[0]: r[1] for r in conn.execute(
                    "SELECT substr(hub_datestamp,1,10) d, COUNT(*) FROM records GROUP BY d ORDER BY d DESC LIMIT 30")}
                by_type = {(r[0] or "(不明)"): r[1] for r in conn.execute(
                    "SELECT json_extract(work_json,'$.type'), COUNT(*) FROM records WHERE deleted=0 "
                    "GROUP BY 1 ORDER BY 2 DESC")}
                by_year = {str(r[0]): r[1] for r in conn.execute(
                    "SELECT json_extract(work_json,'$.publication_year') y, COUNT(*) FROM records "
                    "WHERE deleted=0 AND y BETWEEN 1000 AND 2100 GROUP BY y ORDER BY y")}
                abs_n = conn.execute("SELECT COUNT(*) FROM records WHERE deleted=0 AND "
                                     "json_extract(work_json,'$.has_abstract_in_source')=1").fetchone()[0]
                abs_out = conn.execute("SELECT COUNT(*) FROM records WHERE deleted=0 AND "
                                       "json_extract(work_json,'$.abstract_status')='included'").fetchone()[0]
                oa_n = conn.execute("SELECT COUNT(*) FROM records WHERE deleted=0 AND "
                                    "json_extract(work_json,'$.open_access.is_oa')=1").fetchone()[0]
                orcid_n = conn.execute("SELECT COUNT(*) FROM records WHERE deleted=0 AND "
                                       "work_json LIKE '%\"orcid\": \"%'").fetchone()[0]
                last = conn.execute("SELECT MAX(hub_datestamp) FROM records").fetchone()[0]
                return 200, {"records": tot, "deleted": dele, "by_source": by_src, "irdb_without_institution": unk,
                             "by_site": by_site, "by_day": by_day, "by_type": by_type, "by_year": by_year,
                             "with_abstract_flag": abs_n, "with_abstract_output": abs_out, "open_access": oa_n, "with_orcid": orcid_n,
                             "last_update": last}
            if path == "/api/sites":
                return 200, {"sites": [{"site_id": r[0], "records": r[1]} for r in conn.execute(
                    "SELECT site_id, COUNT(*) FROM records WHERE deleted=0 AND site_id IS NOT NULL "
                    "GROUP BY site_id ORDER BY 2 DESC")]}
            if path == "/api/status":
                return 200, self._status(conn)
            m = re.match(r"^/api/raw/(.+)$", path)
            if m:
                return self._raw(conn, unquote(m.group(1)))
            if path == "/api/institutions":
                inst = {r["code"]: r for r in conn.execute("SELECT * FROM institutions")}
                out = []
                for r in conn.execute("SELECT institution_code c, COUNT(*) n FROM records WHERE deleted=0 "
                                      "AND institution_code IS NOT NULL GROUP BY c ORDER BY c"):
                    i = inst.get(r["c"])
                    out.append({"code": r["c"], "records": r["n"], "name_ja": i["name_ja"] if i else None,
                                "name_en": i["name_en"] if i else None, "ror": i["ror"] if i else None,
                                "url": i["url"] if i else None})
                return 200, {"institutions": out}
            if path == "/api/journals":
                lim = min(int(q.get("limit", 100)), 1000)
                like = "%" + q["q"] + "%" if q.get("q") else "%"
                w, a = ["deleted=0", "journal_key IS NOT NULL", "(journal_key LIKE ? OR journal_title LIKE ?)"], [like, like]
                if q.get("site"):
                    w.append("site_id=?")
                    a.append(q["site"])
                tot = conn.execute("SELECT COUNT(DISTINCT journal_key) FROM records WHERE %s" % " AND ".join(w), a).fetchone()[0]
                rows = conn.execute(
                    "SELECT journal_key k, MAX(journal_title) t, MAX(doi_prefix) p, COUNT(DISTINCT doi_prefix) np, "
                    "MAX(site_id) s, COUNT(*) n FROM records WHERE %s GROUP BY k ORDER BY n DESC, k LIMIT ?"
                    % " AND ".join(w), a + [lim]).fetchall()
                return 200, {"total": tot, "journals": [
                    {"key": r["k"], "title": r["t"], "doi_prefix": r["p"], "doi_prefixes": r["np"],
                     "site_id": r["s"], "records": r["n"]} for r in rows]}
            if path == "/api/works":
                return 200, self._works(conn, q)
            m = re.match(r"^/api/works/(.+)$", path)
            if m:
                r = conn.execute("SELECT * FROM records WHERE identifier=?", (unquote(m.group(1)),)).fetchone()
                if not r:
                    return 404, {"error": "not found"}
                row = self._work_row(r)
                self._attach_topics(conn, [row])
                return 200, row
            return 404, {"error": "unknown endpoint"}
        except ValueError as e:
            return 400, {"error": str(e)}
        finally:
            conn.close()

    @staticmethod
    def _work_row(r):
        w = json.loads(r["work_json"]) if r["work_json"] else None
        return {"identifier": r["identifier"], "source": r["source"], "deleted": bool(r["deleted"]),
                "updated": r["hub_datestamp"], "source_datestamp": r["datestamp"],
                "institution_code": r["institution_code"], "journal_key": r["journal_key"],
                "doi_prefix": r["doi_prefix"], "site_id": r["site_id"], "work": w}

    @staticmethod
    def _attach_topics(conn, rows):
        """分野の自動推定（別表）を、work の外側の "auto_topics" として付ける。原データ・整形結果は変えない。"""
        ids = [r["identifier"] for r in rows]
        if not ids:
            return
        got = {t["identifier"]: t for t in conn.execute(
            "SELECT * FROM topics WHERE identifier IN (%s)" % ",".join("?" * len(ids)), ids)}
        for r in rows:
            t = got.get(r["identifier"])
            if t:
                r["auto_topics"] = dict(json.loads(t["topics_json"]), version=t["version"], assigned_at=t["assigned_at"],
                                        note="自動推定（題名とキーワードから）。参考情報です")

    def _works(self, conn, q):
        lim = min(int(q.get("limit", 100)), 1000)
        w, a = ["r.deleted=0"], []
        for key, col in (("institution", "r.institution_code"), ("journal", "r.journal_key"),
                         ("doi_prefix", "r.doi_prefix"), ("site", "r.site_id"), ("source", "r.source")):
            if q.get(key):
                v = q[key]
                if key == "institution":
                    v = v.zfill(5)
                w.append("%s=?" % col)
                a.append(v)
        if q.get("q"):
            w.append("(r.doi LIKE ? OR r.work_json LIKE ?)")
            like = "%" + q["q"].replace("%", "").replace("_", "") + "%"
            a += [like, like]
        for key, op in (("from", ">="), ("until", "<=")):
            if q.get(key):
                if not DATE_RE.match(q[key]):
                    raise ValueError("%s は YYYY-MM-DDThh:mm:ssZ 形式で指定してください" % key)
                w.append("r.hub_datestamp%s?" % op)
                a.append(q[key])
        total = conn.execute("SELECT COUNT(*) FROM records r WHERE %s" % " AND ".join(w), a).fetchone()[0]
        if q.get("cursor"):
            c = _b64d(q["cursor"])
            w.append("(r.hub_datestamp,r.identifier)>(?,?)")
            a += c
        rows = conn.execute("SELECT r.* FROM records r WHERE %s ORDER BY r.hub_datestamp, r.identifier LIMIT ?"
                            % " AND ".join(w), a + [lim + 1]).fetchall()
        more = len(rows) > lim
        rows = rows[:lim]
        nxt = _b64e([rows[-1]["hub_datestamp"], rows[-1]["identifier"]]) if more else None
        res = [self._work_row(r) for r in rows]
        self._attach_topics(conn, res)
        return {"total": total, "results": res, "next_cursor": nxt}

    # ---------------- 収集状況・原データ（画面用） ----------------
    def _status(self, conn):
        out = {"ingested_pages": conn.execute("SELECT COUNT(*) FROM ingested").fetchone()[0],
               "store": None, "raw": {}, "state": {}}
        if self.store is None:
            return out
        out["store"] = type(self.store).__name__
        for src in ("irdb", "jalc"):
            keys = self.store.list("raw/%s/" % src)
            runs = sorted({k.split("/")[2] for k in keys if len(k.split("/")) > 3})
            out["raw"][src] = {"files": len(keys), "runs": runs[-10:]}
            try:
                out["state"][src] = json.loads(self.store.get("state/%s.json" % src).decode("utf-8"))
            except Exception:
                out["state"][src] = None
        return out

    def _raw(self, conn, ident):
        r = conn.execute("SELECT source, raw_key, doi FROM records WHERE identifier=?", (ident,)).fetchone()
        if not r:
            return 404, {"error": "not found"}
        if self.store is None or not r["raw_key"]:
            return 404, {"error": "原データの保存先が設定されていません"}
        try:
            raw = self.store.get(r["raw_key"])
        except Exception as e:
            return 404, {"error": "原データを読めません: %s" % e}
        if r["source"] == "jalc":
            want = (r["doi"] or "").replace("https://doi.org/", "").lower()
            if r["raw_key"].endswith(".json"):   # 検索画面の保存形式（配列）
                try:
                    arr = json.loads(raw.decode("utf-8-sig"))
                except ValueError:
                    arr = []
                for d in (arr if isinstance(arr, list) else [arr]):
                    if isinstance(d, dict) and (d.get("doi") or "").lower() == want:
                        return 200, {"format": "json", "raw_key": r["raw_key"], "raw": d}
                return 404, {"error": "原データ内に見つかりません"}
            for line in raw.decode("utf-8").splitlines():
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if ((d.get("data") or {}).get("doi") or "").lower() == want:
                    return 200, {"format": "json", "raw_key": r["raw_key"], "raw": d}
            return 404, {"error": "原データ内に見つかりません"}
        try:
            root = ET.fromstring(raw)
            for rec in root.iter("{%s}record" % OAI_NS):
                h = rec.find("{%s}header/{%s}identifier" % (OAI_NS, OAI_NS))
                if h is not None and (h.text or "").strip() == ident:
                    return 200, {"format": "xml", "raw_key": r["raw_key"],
                                 "raw": ET.tostring(rec, encoding="unicode")}
        except ET.ParseError:
            pass
        return 404, {"error": "原データ内に見つかりません"}


def make_handler(hub):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, ctype, body):
            b = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/oai", "/oai/"):
                self._send(200, "text/xml; charset=utf-8", hub.oai(parse_qs(u.query, keep_blank_values=True)))
            elif u.path in ("/", "/ui", "/index.html"):
                p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui", "index.html")
                with open(p, encoding="utf-8") as f:
                    self._send(200, "text/html; charset=utf-8", f.read())
            elif u.path == "/health":
                self._send(200, "application/json", '{"status":"ok"}')
            elif u.path.startswith("/api/"):
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                code, d = hub.api(u.path, q)
                self._send(code, "application/json; charset=utf-8", json.dumps(d, ensure_ascii=False))
            else:
                self._send(404, "text/plain", "not found")
    return H


def serve(hub, host="127.0.0.1", port=8080):
    srv = ThreadingHTTPServer((host, port), make_handler(hub))
    return srv
