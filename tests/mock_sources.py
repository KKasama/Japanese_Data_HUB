"""テスト用のモックのデータ源（IRDB風OAI-PMH, JaLC風REST）。実サーバーには接続しない。

注意: irdbProvenance の中身は「仮定」の構造。実データで確認でき次第、置き換えること。
"""
import json
import os
import re
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

SAMPLES = os.path.join(os.path.dirname(__file__), "samples")
INSTITUTIONS = {"90001": "テスト大学A", "90002": "テスト大学B", "90003": "テスト大学C"}


def sample_meta(name, n, doi=None):
    """JPCOARサンプルを元に、n番目の合成レコード本体(<jpcoar:jpcoar>…)を文字列で返す。"""
    s = open(os.path.join(SAMPLES, name), encoding="utf-8").read()
    s = re.sub(r"<\?xml[^>]*\?>", "", s).strip()
    s = s.replace("<dc:title xml:lang=\"ja\">", "<dc:title xml:lang=\"ja\">[%03d] " % n, 1)
    if doi:
        s = re.sub(r"(<jpcoar:identifier identifierType=\"DOI\"[^>]*>)[^<]*", r"\g<1>https://doi.org/" + doi, s)
    return s


def make_records(n_each=2):
    """機関3つ×各n件 + 削除1件。"""
    files = ["01_departmental_bulletin_paper_oa.xml", "02_journal_article_embargoed.xml",
             "03_journal_article_oa.xml", "05_doctoral_thesis_oa.xml", "07_dataset.xml"]
    recs, n = [], 0
    for code, name in INSTITUTIONS.items():
        for k in range(n_each):
            f = files[n % len(files)]
            ident = "oai:irdb.nii.ac.jp:%s:%08d" % (code, n + 1)
            about = ('<about><irdbProvenance xmlns="http://irdb.nii.ac.jp/oai">'
                     "<repositoryName>%s リポジトリ</repositoryName></irdbProvenance></about>" % name)
            recs.append({"identifier": ident, "datestamp": "2026-10-%02dT00:00:00Z" % (1 + n),
                         "setspecs": ["irdb", str(int(code))], "deleted": False,
                         "meta": sample_meta(f, n + 1), "about": about})
            n += 1
    ident = "oai:irdb.nii.ac.jp:90001:%08d" % 99
    recs.append({"identifier": ident, "datestamp": "2026-10-09T00:00:00Z",
                 "setspecs": ["irdb", "90001"], "deleted": True, "meta": None, "about": ""})
    return recs


class MockIRDB:
    """OAI-PMH風。set は irdb 以外 badArgument（実IRDBの報告どおり）。"""

    def __init__(self, records, page_size=3):
        self.records = records
        self.page_size = page_size
        self.restrict = False          # True: ListRecords を badArgument で拒否（IP制限の模擬）
        self.fail_after = None         # N ページ配信後に 500 を返し続ける
        self.fail_once_503 = False
        self.served_pages = 0
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests.append(self.path)
                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                if outer.fail_once_503:
                    outer.fail_once_503 = False
                    self.send_response(503)
                    self.send_header("Retry-After", "0")
                    self.end_headers()
                    return
                if outer.fail_after is not None and outer.served_pages >= outer.fail_after:
                    self.send_response(500)
                    self.end_headers()
                    return
                body = outer.respond(q)
                if q.get("verb") == "ListRecords" and "<error" not in body:
                    outer.served_pages += 1
                b = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/xml; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d/oai" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def _wrap(self, body):
        return ('<?xml version="1.0" encoding="UTF-8"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
                "<responseDate>2026-10-10T00:00:00Z</responseDate><request>x</request>%s</OAI-PMH>" % body)

    def respond(self, q):
        verb = q.get("verb")
        if verb == "Identify":
            return self._wrap("<Identify><repositoryName>mock</repositoryName></Identify>")
        if verb != "ListRecords":
            return self._wrap('<error code="badVerb">x</error>')
        if self.restrict:
            return self._wrap('<error code="badArgument">The request includes illegal arguments</error>')
        if "resumptionToken" in q:
            offset, frm, until = (int(x) if x.isdigit() else x or None for x in (q["resumptionToken"].split("|") + ["", ""])[:3])
            offset = int(q["resumptionToken"].split("|")[0])
            frm = q["resumptionToken"].split("|")[1] or None
            until = q["resumptionToken"].split("|")[2] or None
        else:
            if q.get("set") not in (None, "irdb"):
                return self._wrap('<error code="badArgument">set</error>')
            offset, frm, until = 0, q.get("from"), q.get("until")
        sel = [r for r in self.records
               if (not frm or r["datestamp"] >= frm) and (not until or r["datestamp"] <= until)]
        if not sel:
            return self._wrap('<error code="noRecordsMatch">none</error>')
        page = sel[offset: offset + self.page_size]
        out = []
        for r in page:
            sets = "".join("<setSpec>%s</setSpec>" % s for s in r["setspecs"])
            h = '<header%s><identifier>%s</identifier><datestamp>%s</datestamp>%s</header>' % (
                ' status="deleted"' if r["deleted"] else "", r["identifier"], r["datestamp"], sets)
            md = "" if r["deleted"] else "<metadata>%s</metadata>%s" % (r["meta"], r["about"])
            out.append("<record>%s%s</record>" % (h, md))
        nxt = offset + self.page_size
        tok = ("<resumptionToken>%d|%s|%s</resumptionToken>" % (nxt, frm or "", until or "")
               if nxt < len(sel) else "")
        return self._wrap("<ListRecords>%s%s</ListRecords>" % ("".join(out), tok))


def jalc_data(doi, site, issn, jid, title, shape="list"):
    pfx = doi.split("/")[0]
    titles = ([{"lang": "ja", "title": title}, {"lang": "en", "title": "EN " + title}] if shape == "list"
              else {"ja": title, "en": "EN " + title})
    return {"siteId": site, "content_type": "JA", "doi": doi, "url": "https://doi.org/" + doi, "ra": "JaLC",
            "prefix": pfx, "site_name": "J-STAGE" if "JSTAGE" in site else "他",
            "publisher_list": [{"lang": "ja", "publisher_name": "テスト学会"}],
            "title_list": titles,
            "creator_list": [{"sequence": 1, "type": "person",
                              "names": [{"lang": "ja", "name": "山田 太郎"}],
                              "affiliations": [{"lang": "ja", "affiliation_name": "テスト大学A"}]}],
            "publication_date": "2026-03-20", "updated_date": "2026-03-19", "content_language": "ja",
            "relation_list": [{"type": "URL", "relation": "fullTextPdf", "content": "https://example.org/%s.pdf" % jid}],
            "journal_id_list": [{"type": "ISSN", "journal_id": issn[0], "issn_type": "print"},
                                {"type": "ISSN", "journal_id": issn[1], "issn_type": "online"},
                                {"type": "JID", "journal_id": jid}],
            "journal_title_name_list": [{"lang": "ja", "journal_title_name": "テスト誌" + jid}],
            "volume": "16", "issue": "3", "first_page": "87", "last_page": "95",
            "keyword_list": [{"keyword": "実験操作", "sequence": "1", "lang": "ja"}],
            "citation_list": []}


class MockJaLC:
    def __init__(self):
        self.items = {
            "10.11162": [("10.11162/a.1", "SI/JST.JSTAGE", ("1884-4375", "2186-3725"), "daikankyo", "list"),
                         ("10.11162/a.2", "SI/JST.JSTAGE", ("1884-4375", "2186-3725"), "daikankyo", "dict")],
            "10.11163": [("10.11163/b.1", "SI/JST.JSTAGE", ("1111-2222", "3333-444X"), "jb", "list")],
            "10.9999": [("10.9999/c.1", "SI/OTHER.ORG", ("5555-6666", "7777-8888"), "oc", "list")],
        }
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, d):
                b = json.dumps(d, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                u = urlparse(self.path)
                outer.requests.append(self.path)
                if u.path == "/prefixes":
                    return self._json(200, {"status": "OK", "message": {"total": 3, "rows": 3, "totalPages": 1, "page": 1},
                                            "data": {"items": [{"prefix": p, "ra": "JaLC", "siteId": v[0][1], "updated_date": "2014-12-09"}
                                                               for p, v in outer.items.items()]}})
                m = re.match(r"^/doilist/(.+)$", u.path)
                if m:
                    v = outer.items.get(m.group(1))
                    if not v:
                        return self._json(404, {"status": "NG"})
                    return self._json(200, {"status": "OK", "message": {"total": len(v), "rows": len(v), "totalPages": 1, "page": 1},
                                            "data": {"items": [{"dois": {"doi": x[0], "url": "https://doi.org/" + x[0]},
                                                                "ra": "JaLC", "siteId": x[1], "updated_date": "2026-03-19"} for x in v]}})
                m = re.match(r"^/v2/dois/(.+)$", u.path)
                if m:
                    doi = m.group(1)
                    for v in outer.items.values():
                        for x in v:
                            if x[0] == doi:
                                return self._json(200, {"status": "OK", "apiType": "doi", "apiVersion": "v2",
                                                        "data": jalc_data(x[0], x[1], x[2], x[3], "論文" + x[0], x[4])})
                    return self._json(404, {"status": "NG"})
                self._json(404, {})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()
