import gzip
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from jhub import normalize as N
from jhub.classify import institution_code, journal_key, normalize_issn
from jhub.db import connect, load_institutions_csv
from jhub.export import export_bulk
from jhub.harvest import harvest_jalc, harvest_oai
from jhub.ingest import ingest
from jhub.jalc_client import JaLCClient
from jhub.oai_client import OAIClient, OAIError
from jhub.server import Hub, serve
from jhub.store import LocalStore
from mock_sources import MockIRDB, MockJaLC, SAMPLES, make_records, sample_meta

NS = {"o": "http://www.openarchives.org/OAI/2.0/"}


def noop(*a, **k):
    pass


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = LocalStore(os.path.join(self.tmp, "store"))
        self.dbp = os.path.join(self.tmp, "hub.sqlite")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestNormalize(unittest.TestCase):
    def meta(self, name):
        with open(os.path.join(SAMPLES, name), encoding="utf-8") as fh:
            root = ET.fromstring(fh.read())
        return root

    def test_journal_article_sample(self):
        w = N.normalize_jpcoar(self.meta("03_journal_article_oa.xml"), "oai:x:1")
        self.assertEqual(w["type"], "article")
        self.assertEqual(w["publication_year"], 2015)
        self.assertEqual(w["issn"], ["1880-697X"])
        self.assertEqual(w["journal_key"], "1880-697X")
        self.assertEqual(w["doi"], "https://doi.org/10.1371/journal.pone.0170224")
        self.assertEqual(w["doi_prefix"], "10.1371")
        self.assertEqual(w["authorships"][0]["author"]["orcid"], "https://orcid.org/0000-0001-0002-0003")
        self.assertEqual(w["authorships"][0]["raw_affiliation_strings"][0], "東京大学")
        self.assertTrue(w["open_access"]["is_oa"])
        self.assertEqual(w["biblio"]["first_page"], "34")

    def test_thesis_and_dataset_types(self):
        self.assertEqual(N.normalize_jpcoar(self.meta("05_doctoral_thesis_oa.xml"))["type"], "dissertation")
        self.assertEqual(N.normalize_jpcoar(self.meta("07_dataset.xml"))["type"], "dataset")

    def test_no_abstract_text_in_output(self):
        for f in os.listdir(SAMPLES):
            w = N.normalize_jpcoar(self.meta(f))
            self.assertNotIn("abstract", w)
            self.assertIn("has_abstract_in_source", w)

    def test_embargo_pdf_not_exposed(self):
        w = N.normalize_jpcoar(self.meta("02_journal_article_embargoed.xml"))
        self.assertFalse(w["open_access"]["is_oa"])
        self.assertIsNone(w["primary_location"]["pdf_url"])


class TestClassify(unittest.TestCase):
    def test_institution_from_setspec_and_id(self):
        self.assertEqual(institution_code("oai:irdb.nii.ac.jp:01490:00000001", ["irdb", "1490"])[:2], ("01490", "setSpec"))
        self.assertEqual(institution_code("oai:irdb.nii.ac.jp:01490:00000001", ["irdb"])[:2], ("01490", "identifier"))
        self.assertEqual(institution_code("weird", ["irdb"])[0], None)

    def test_mismatch_flagged(self):
        code, how, mm = institution_code("oai:irdb.nii.ac.jp:01490:1", ["irdb", "1491"])
        self.assertEqual(code, "01491")
        self.assertTrue(mm)

    def test_issn(self):
        self.assertEqual(normalize_issn("1880697x"), "1880-697X")
        self.assertIsNone(normalize_issn("abc"))
        self.assertEqual(journal_key([], "daikankyo", "SI/JST.JSTAGE"), "jid.SI/JST.JSTAGE.daikankyo")


class TestIRDBFlow(Base):
    def setUp(self):
        super().setUp()
        self.src = MockIRDB(make_records(2), page_size=3)
        self.client = OAIClient(self.src.url, retries=2, sleep=noop)

    def tearDown(self):
        self.src.stop()
        super().tearDown()

    def run_pipeline(self, now_h, now_i):
        r = harvest_oai(self.client, self.store, "irdb", "jpcoar_2.0", now=now_h, log=noop)
        conn = connect(self.dbp)
        s = ingest(self.store, conn, "irdb", "oai", "jpcoar_2.0", now=now_i, log=noop)
        return r, s, conn

    def test_harvest_ingest_split(self):
        r, s, conn = self.run_pipeline("2026-10-10T00:00:00Z", "2026-10-10T01:00:00Z")
        self.assertEqual(r["pages"], 3)  # 7件 / 3件ページ
        self.assertEqual(s["new"], 7)
        n = {r[0]: r[1] for r in conn.execute("SELECT institution_code, COUNT(*) FROM records GROUP BY 1")}
        self.assertEqual(n, {"90001": 3, "90002": 2, "90003": 2})
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM records WHERE deleted=1").fetchone()[0], 1)
        # provenance は構造を仮定せず、そのまま保存される
        p = json.loads(conn.execute("SELECT provenance_json FROM records WHERE deleted=0 LIMIT 1").fetchone()[0])
        self.assertIn("irdbProvenance", p)
        self.assertIn("repositoryName", p["irdbProvenance"])

    def test_reingest_is_idempotent(self):
        _, _, conn = self.run_pipeline("2026-10-10T00:00:00Z", "2026-10-10T01:00:00Z")
        s2 = ingest(self.store, conn, "irdb", "oai", "jpcoar_2.0", now="2026-10-10T02:00:00Z", log=noop)
        self.assertEqual(s2["pages"], 0)

    def test_incremental_and_deletion(self):
        self.run_pipeline("2026-10-10T00:00:00Z", "2026-10-10T01:00:00Z")
        # 元データ側: 1件更新、1件削除、1件追加
        recs = self.src.records
        recs[0]["datestamp"] = "2026-10-11T00:00:00Z"
        recs[0]["meta"] = sample_meta("03_journal_article_oa.xml", 777)
        recs[1]["datestamp"] = "2026-10-11T00:00:00Z"
        recs[1]["deleted"], recs[1]["meta"] = True, None
        recs.append({"identifier": "oai:irdb.nii.ac.jp:90003:00000100", "datestamp": "2026-10-11T00:00:00Z",
                     "setspecs": ["irdb", "90003"], "deleted": False, "meta": sample_meta("07_dataset.xml", 100), "about": ""})
        before = len(self.src.requests)
        r, s, conn = self.run_pipeline("2026-10-11T12:00:00Z", "2026-10-11T13:00:00Z")
        self.assertTrue(any("from=2026-10-10T00%3A00%3A00Z" in u for u in self.src.requests[before:]))
        self.assertEqual((s["new"], s["changed"]), (1, 2))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM records WHERE deleted=1").fetchone()[0], 2)
        # 削除されたレコードも以前の機関セットに残る（OpenAlex側が削除を検知できる）
        row = conn.execute("SELECT identifier FROM records WHERE identifier=?", (recs[1]["identifier"],)).fetchone()
        sets = [x[0] for x in conn.execute("SELECT setspec FROM record_sets WHERE identifier=?", (row[0],))]
        self.assertIn("inst:90001", sets)

    def test_resume_after_failure(self):
        self.src.fail_after = 1
        with self.assertRaises(Exception):
            harvest_oai(self.client, self.store, "irdb", "jpcoar_2.0", now="2026-10-10T00:00:00Z", log=noop)
        st = json.loads(self.store.get("state/irdb.json"))
        self.assertTrue(st["pending_token"])
        self.assertEqual(len(self.store.list("raw/irdb/")), 1)
        self.src.fail_after = None
        r = harvest_oai(self.client, self.store, "irdb", "jpcoar_2.0", now="2026-10-12T00:00:00Z", log=noop)
        self.assertTrue(r["complete"])
        self.assertEqual(len(self.store.list("raw/irdb/")), 3)  # 重複なし、取りこぼしなし
        st = json.loads(self.store.get("state/irdb.json"))
        self.assertEqual(st["last_complete"], "2026-10-10T00:00:00Z")  # 最初の開始時刻を維持

    def test_retry_on_503(self):
        self.src.fail_once_503 = True
        r = harvest_oai(self.client, self.store, "irdb", "jpcoar_2.0", now="2026-10-10T00:00:00Z", log=noop)
        self.assertTrue(r["complete"])

    def test_ip_restriction_message(self):
        self.src.restrict = True
        with self.assertRaises(OAIError) as cm:
            harvest_oai(self.client, self.store, "irdb", "jpcoar_2.0", log=noop)
        self.assertEqual(cm.exception.code, "badArgument")
        self.assertIn("申請済みIP", cm.exception.message)

    def test_hub_oai_provider_split_by_institution(self):
        _, _, conn = self.run_pipeline("2026-10-10T00:00:00Z", "2026-10-10T01:00:00Z")
        csvp = os.path.join(self.tmp, "inst.csv")
        open(csvp, "w", encoding="utf-8").write(
            "code,name_ja,name_en,ror,url\n90001,テスト大学A,Test Univ A,https://ror.org/000000001,https://repo.a.example/\n")
        load_institutions_csv(conn, csvp)
        hub = Hub(self.dbp, "http://hub.test/oai", "ops@example.org", page_size=2, now="2026-10-12T00:00:00Z")
        srv = serve(hub, "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d/oai" % srv.server_address[1]
        try:
            # OpenAlexと同じ要領（resumptionTokenでページ送り）で、機関別セットを収集
            c = OAIClient(base, sleep=noop)
            ids, deleted = [], []
            for raw, _ in c.pages("ListRecords", "jpcoar_2.0", set_spec="inst:90001"):
                for rec in ET.fromstring(raw).iter("{%s}record" % NS["o"]):
                    h = rec.find("o:header", NS)
                    ids.append(h.find("o:identifier", NS).text)
                    if h.get("status") == "deleted":
                        deleted.append(ids[-1])
                    else:
                        # 原XMLがそのまま入っている（JPCOAR）
                        self.assertTrue(any("jpcoar" in e.tag for e in rec.find("o:metadata", NS).iter()))
            self.assertEqual(len(ids), 3)
            self.assertTrue(all(":90001:" in i for i in ids))
            self.assertEqual(len(deleted), 1)  # 削除も伝わる
            # ListSets: 台帳で拡充（ROR・名称・URL）
            raw = c.request({"verb": "ListSets"})
            sets = {s.find("o:setSpec", NS).text: s for s in ET.fromstring(raw).iter("{%s}set" % NS["o"])}
            self.assertIn("inst:90001", sets)
            self.assertIn("https://ror.org/000000001", ET.tostring(sets["inst:90001"], encoding="unicode"))
            self.assertIn("inst:90002", sets)
            # 差分: from 以降のみ
            raw = c.request({"verb": "ListIdentifiers", "metadataPrefix": "jpcoar_2.0", "from": "2026-10-10T02:00:00Z"})
            self.assertIn("noRecordsMatch", raw.decode())
            # エラー系
            for params, code in [({"verb": "Foo"}, "badVerb"),
                                 ({"verb": "ListRecords"}, "badArgument"),
                                 ({"verb": "ListRecords", "metadataPrefix": "zzz"}, "cannotDisseminateFormat"),
                                 ({"verb": "GetRecord", "metadataPrefix": "jpcoar_2.0", "identifier": "nope"}, "idDoesNotExist"),
                                 ({"verb": "ListRecords", "metadataPrefix": "jpcoar_2.0", "from": "2026-10-10"}, "badArgument"),
                                 ({"verb": "ListRecords", "metadataPrefix": "jpcoar_2.0", "set": "inst:00000"}, "noRecordsMatch"),
                                 ({"verb": "ListRecords", "resumptionToken": "garbage"}, "badResumptionToken")]:
                self.assertIn('code="%s"' % code, c.request(params).decode(), params)
            # 取得API
            code, d = hub.api("/api/works", {"institution": "90002"})
            self.assertEqual(len(d["results"]), 2)
            code, d = hub.api("/api/stats", {})
            self.assertEqual(d["records"], 6)
            self.assertEqual(d["deleted"], 1)
            code, d = hub.api("/api/institutions", {})
            self.assertEqual({i["code"] for i in d["institutions"]}, {"90001", "90002", "90003"})
            # ページング（cursor）
            code, p1 = hub.api("/api/works", {"limit": "4"})
            code, p2 = hub.api("/api/works", {"limit": "4", "cursor": p1["next_cursor"]})
            got = [r["identifier"] for r in p1["results"] + p2["results"]]
            self.assertEqual(len(got), len(set(got)))
            self.assertEqual(len(got), 6)
            self.assertIsNone(p2["next_cursor"])
            self.assertEqual(hub.api("/api/works", {"from": "bad"})[0], 400)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_bulk_export(self):
        self.run_pipeline("2026-10-10T00:00:00Z", "2026-10-10T01:00:00Z")
        out = os.path.join(self.tmp, "exp")
        m = export_bulk(self.dbp, out, log=noop)
        self.assertEqual(m["files"], {"inst_90001": 2, "inst_90002": 2, "inst_90003": 2})
        self.assertEqual(len(m["deleted_identifiers"]), 1)
        with gzip.open(os.path.join(out, "inst_90001.jsonl.gz"), "rt", encoding="utf-8") as f:
            rows = [json.loads(l) for l in f]
        self.assertEqual(len(rows), 2)
        self.assertNotIn("abstract", rows[0])


class TestJaLCFlow(Base):
    def setUp(self):
        super().setUp()
        self.src = MockJaLC()
        self.client = JaLCClient(self.src.url, max_rps=0, sleep=noop)

    def tearDown(self):
        self.src.stop()
        super().tearDown()

    def test_jstage_only_harvest_and_split(self):
        r = harvest_jalc(self.client, self.store, "jalc", site_id="SI/JST.JSTAGE", now="2026-10-10T00:00:00Z", log=noop)
        self.assertEqual(r["dois"], 3)  # J-STAGEの2プレフィックスのみ（その他サイトは取らない）
        conn = connect(self.dbp)
        s = ingest(self.store, conn, "jalc", "jalc", now="2026-10-10T01:00:00Z", log=noop)
        self.assertEqual(s["new"], 3)
        hub = Hub(self.dbp, now="2026-10-12T00:00:00Z")
        code, d = hub.api("/api/works", {"site": "SI/JST.JSTAGE"})
        self.assertEqual(len(d["results"]), 3)
        code, d = hub.api("/api/works", {"journal": "1884-4375"})  # ISSN(print) でジャーナル別
        self.assertEqual(len(d["results"]), 2)
        w = d["results"][0]["work"]
        self.assertEqual(w["issn"], ["1884-4375", "2186-3725"])
        self.assertEqual(w["jid"], "daikankyo")
        self.assertEqual(w["title"][:2], "論文")
        self.assertTrue(w["display_name"].startswith("EN "))  # 英語優先
        self.assertEqual(w["authorships"][0]["author"]["display_name"], "山田 太郎")
        self.assertEqual(w["authorships"][0]["raw_affiliation_strings"], ["テスト大学A"])
        self.assertEqual(w["primary_location"]["pdf_url"], "https://example.org/daikankyo.pdf")
        self.assertFalse(w["has_abstract_in_source"])
        code, d = hub.api("/api/works", {"doi_prefix": "10.11163"})
        self.assertEqual(len(d["results"]), 1)

    def test_jalc_records_served_as_hub_work_over_oai(self):
        harvest_jalc(self.client, self.store, "jalc", now="2026-10-10T00:00:00Z", log=noop)
        conn = connect(self.dbp)
        ingest(self.store, conn, "jalc", "jalc", now="2026-10-10T01:00:00Z", log=noop)
        hub = Hub(self.dbp, now="2026-10-12T00:00:00Z")
        xml = hub.oai({"verb": ["ListRecords"], "metadataPrefix": ["jhub_work"], "set": ["site:JST.JSTAGE"]})
        root = ET.fromstring(xml)
        recs = list(root.iter("{%s}record" % NS["o"]))
        self.assertEqual(len(recs), 3)
        work = next(iter(recs[0].find("o:metadata", NS))).text
        self.assertEqual(json.loads(work)["site_id"], "SI/JST.JSTAGE")
        # JaLC由来は jpcoar 形では出さない
        self.assertIn("noRecordsMatch", hub.oai({"verb": ["ListRecords"], "metadataPrefix": ["jpcoar_2.0"], "set": ["site:JST.JSTAGE"]}))

    def test_incremental_uses_last_date(self):
        harvest_jalc(self.client, self.store, "jalc", site_id="SI/JST.JSTAGE", now="2026-10-10T00:00:00Z", log=noop)
        n = len(self.src.requests)
        harvest_jalc(self.client, self.store, "jalc", site_id="SI/JST.JSTAGE", now="2026-10-11T00:00:00Z", log=noop)
        self.assertTrue(any("from=2026-10-10" in u for u in self.src.requests[n:]))


if __name__ == "__main__":
    unittest.main()
