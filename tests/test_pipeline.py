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
        self.assertTrue(w["display_name"].startswith("EN "))
        self.assertEqual(w["journal_title"], "テスト誌daikankyo")  # 英語優先
        self.assertEqual(w["authorships"][0]["author"]["display_name"], "Taro Yamada")  # 英語優先: 名 姓
        self.assertIn("山田 太郎", [n["text"] for n in w["authorships"][0]["names"]])  # 日本語: 姓 名
        self.assertEqual(w["publication_date"], "2026-03-20")
        self.assertEqual(w["references_count"], 1)
        self.assertEqual(w["authorships"][0]["raw_affiliation_strings"], ["テスト大学A", "Test University A"])
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



class CleanTextTest(unittest.TestCase):
    def test_clean_text_strips_tags_and_entities(self):
        from jhub.normalize import clean_text
        self.assertEqual(clean_text("UV/O<sub>3</sub>処理装置"), "UV/O3処理装置")
        self.assertEqual(clean_text('on &quot;Structure-to-Hazards&quot; Image'), 'on "Structure-to-Hazards" Image')
        self.assertEqual(clean_text("<i>Americamysis bahia</i>  test "), "Americamysis bahia test")

    def test_jalc_title_cleaned_and_lang_guessed(self):
        from jhub.normalize import normalize_jalc
        w = normalize_jalc({"doi": "10.1/x", "title_list": [{"lang": "en", "title": "A <i>b</i> &amp; c"}, {"title": "日本語の題"}]})
        self.assertEqual(w["display_name"], "A b & c")
        self.assertEqual(w["titles"][1]["lang"], "ja")


class ReprocessTest(unittest.TestCase):
    def test_reprocess_renormalizes_ingested_pages(self):
        import tempfile
        from jhub.db import connect
        from jhub.harvest import harvest_jalc
        from jhub.ingest import ingest
        from jhub.jalc_client import JaLCClient
        from jhub.store import LocalStore
        from mock_sources import MockJaLC
        tmp = tempfile.mkdtemp()
        jalc = MockJaLC()
        try:
            store, conn = LocalStore(tmp + "/s"), connect(tmp + "/h.sqlite")
            harvest_jalc(JaLCClient(jalc.url, max_rps=0), store, "jalc")
            self.assertGreater(ingest(store, conn, "jalc", "jalc", log=lambda *a: None)["new"], 0)
            self.assertEqual(ingest(store, conn, "jalc", "jalc", log=lambda *a: None)["pages"], 0)
            st = ingest(store, conn, "jalc", "jalc", reprocess=True, log=lambda *a: None)
            self.assertGreater(st["pages"], 0)
            self.assertEqual(st["new"], 0)
        finally:
            jalc.stop()


class JaLCOtherSitesTest(unittest.TestCase):
    """J-STAGE Data / Jxiv / NDL の実データ（2026-10-10取得）の構造に基づく。"""

    def test_jxiv_preprint(self):
        from jhub.normalize import normalize_jalc
        w = normalize_jalc({
            "siteId": "SI/JST.preprint", "content_type": "RD", "doi": "10.51094/jxiv.1", "prefix": "10.51094",
            "resource_type": "Preprint", "site_name": "JST preprint",
            "title_list": [{"lang": "ja", "title": "因果モデル", "subtitle": "試行的分析"},
                           {"lang": "en", "title": "Causal Model", "subtitle": "Trial Analysis"}],
            "creator_list": [{"names": [{"lang": "ja", "last_name": "高山", "first_name": "正行"},
                                        {"lang": "en", "last_name": "TAKAYAMA", "first_name": "Masayuki"}],
                              "researcher_id_list": [{"id_code": "https://orcid.org/0000-0003-3580-8897", "type": "orcid"}]}],
            "publication_date": {"publication_year": "2022", "publication_month": "03", "publication_day": "25"},
            "rights_list": [{"rights": "CC BY-NC-ND 4.0", "uri": "https://creativecommons.org/licenses/by-nc-nd/4.0"}],
            "description_list": [{"description": "<p>要旨</p>", "type": "Abstract", "lang": "ja"}],
            "repository_list": [{"repository_name": "Jxiv", "lang": "ja"}, {"repository_name": "Jxiv", "lang": "en"}]})
        self.assertEqual(w["type"], "preprint")
        self.assertEqual(w["display_name"], "Causal Model: Trial Analysis")
        self.assertEqual(w["title"], "因果モデル: 試行的分析")
        self.assertEqual(w["authorships"][0]["author"]["orcid"], "https://orcid.org/0000-0003-3580-8897")
        self.assertTrue(w["has_abstract_in_source"])
        self.assertNotIn("abstract", w)
        self.assertEqual(w["publication_date"], "2022-03-25")
        self.assertTrue(w["open_access"]["is_oa"])
        self.assertEqual(w["primary_location"]["source"]["type"], "repository")
        self.assertEqual(w["primary_location"]["source"]["display_name"], "Jxiv")

    def test_jstage_data_funders_and_relations(self):
        from jhub.normalize import normalize_jalc
        w = normalize_jalc({
            "siteId": "SI/JST.JSTAGE_Data", "content_type": "RD", "doi": "10.34474/data.jmsj.1", "prefix": "10.34474",
            "resource_type": "Dataset", "title_list": [{"lang": "en", "title": "TC data"}],
            "creator_list": [{"names": [{"lang": "en", "last_name": "Satoh", "first_name": "Masaki"}]}],
            "fund_list": [{"funder_name": [{"funder_name": "SPIRE", "lang": "en"}]}],
            "relation_list": [{"content": "10.2151/jmsj.2015-025", "type": "DOI", "relation": "IsReferencedBy"}],
            "date_list": [{"date": "2020-11-19T01:51:57", "type": "Issued"}]})
        self.assertEqual(w["type"], "dataset")
        self.assertEqual(w["grants"][0]["funder_display_name"], "SPIRE")
        self.assertEqual(w["relations"][0]["relation"], "IsReferencedBy")
        self.assertEqual(w["dates"]["Issued"], "2020-11-19T01:51:57")
        self.assertFalse(w["has_abstract_in_source"])

    def test_ndl_placeholders(self):
        from jhub.normalize import normalize_jalc
        w = normalize_jalc({
            "siteId": "SI/NDL.NDL.Search", "content_type": "BK", "doi": "10.11501/1287699", "prefix": "10.11501",
            "title_list": [{"title": "十二月遊ひ 下"}],
            "creator_list": [{"sequence": "1", "names": [{"first_name": "[ -- ]"}]}],
            "publication_date": {"publication_year": "9999"},
            "alternate_identifier_list": [{"alternate_identifier": "oai:iss.ndl.go.jp:R1-I2-00", "type": "OAIPMH"}]})
        self.assertEqual(w["type"], "book")
        self.assertEqual(w["authorships"], [])
        self.assertIsNone(w["publication_year"])
        self.assertEqual(w["titles"][0]["lang"], "ja")
        self.assertEqual(w["ids"]["alternate"][0]["type"], "OAIPMH")

    def test_references_dois_for_article(self):
        from jhub.normalize import normalize_jalc
        w = normalize_jalc({"siteId": "SI/JST.JSTAGE", "content_type": "JA", "doi": "10.11162/x.1",
                            "title_list": [{"lang": "en", "title": "T"}],
                            "citation_list": [{"doi": "10.1007/BF01925580"}, {"original_text": "x"}]})
        self.assertEqual(w["references_count"], 2)
        self.assertEqual(w["referenced_dois"], ["10.1007/bf01925580"])

if __name__ == "__main__":
    unittest.main()


class UITest(unittest.TestCase):
    def test_ui_and_new_endpoints(self):
        import json as _j, os as _o, tempfile as _t, threading as _th, urllib.request as _u
        from jhub.server import Hub, serve
        from jhub.store import LocalStore
        tmp = _t.mkdtemp()
        store, dbp = LocalStore(tmp + "/s"), tmp + "/h.sqlite"
        jalc = MockJaLC()
        try:
            harvest_jalc(JaLCClient(jalc.url, max_rps=0), store, "jalc", log=lambda *a: None)
            conn = connect(dbp)
            ingest(store, conn, "jalc", "jalc", log=lambda *a: None)
            srv = serve(Hub(dbp, store=store), "127.0.0.1", 0)
            _th.Thread(target=srv.serve_forever, daemon=True).start()
            base = "http://127.0.0.1:%d" % srv.server_address[1]
            g = lambda p: _u.urlopen(base + p).read().decode()
            self.assertIn("<html", g("/"))
            self.assertIn("by_site", _j.loads(g("/api/stats")))
            w = _j.loads(g("/api/works?limit=1"))
            self.assertEqual(w["total"], 4)
            raw = _j.loads(g("/api/raw/" + w["results"][0]["identifier"].replace(":", "%3A").replace("/", "%2F")))
            self.assertEqual(raw["format"], "json")
            self.assertEqual(_j.loads(g("/api/works?q=nomatch"))["total"], 0)
            srv.shutdown()
        finally:
            jalc.stop()


class JaLCRepositoryTest(unittest.TestCase):
    """機関リポジトリ由来のJaLCレコード（SI/NII.JAIRO）。実データ(10.15083/00004131, 2026-10-11)の構造。"""

    def test_institution_from_irdb_identifier(self):
        import json as _j
        from jhub.ingest import records_from_jalc_chunk
        data = {"siteId": "SI/NII.JAIRO", "content_type": "BK", "doi": "10.15083/00004131", "prefix": "10.15083",
                "site_name": "機関リポジトリ／Institutional Repository",
                "publisher_list": [{"publisher_name": "University of Tokyo (東京大学)", "location": "JPN"}],
                "title_list": [{"title": "Development of a New RF Accelerating Cavity"}],
                "creator_list": [{"sequence": "1", "type": "person", "names": [{"last_name": "Morita", "first_name": "Yuichi"}]}],
                "publication_date": {"publication_year": "2011", "publication_month": "03", "publication_day": "24"},
                "alternate_identifier_list": [{"alternate_identifier": "oai:irdb.nii.ac.jp:00926:0001750744", "type": "OAIPMH"}],
                "content_language": "en", "updated_date": "2026-07-24", "book_classification": "03"}
        raw = (_j.dumps({"data": data}) + "\n").encode()
        r = next(records_from_jalc_chunk(raw, "jalc", "k"))
        self.assertEqual(r["institution_code"], "00926")
        self.assertIn("inst:00926", r["setspecs"])
        self.assertEqual(r["institution_how"], "jalc_alternate_id")


class JournalExportTest(unittest.TestCase):
    def test_export_by_journal_and_api(self):
        import json as _j, os as _o, tempfile as _t
        from jhub.export import export_by_journal
        tmp = _t.mkdtemp()
        store, dbp = LocalStore(tmp + "/s"), tmp + "/h.sqlite"
        jalc = MockJaLC()
        try:
            harvest_jalc(JaLCClient(jalc.url, max_rps=0), store, "jalc", log=lambda *a: None)
            conn = connect(dbp)
            ingest(store, conn, "jalc", "jalc", log=lambda *a: None)
            js = export_by_journal(dbp, tmp + "/out", log=lambda *a: None)
            self.assertEqual({j["journal_key"] for j in js}, {"1884-4375", "1111-2222", "5555-6666"})
            top = [j for j in js if j["journal_key"] == "1884-4375"][0]
            self.assertEqual(top["records"], 2)
            self.assertEqual(top["doi_prefixes"], ["10.11162"])
            with gzip.open(_o.path.join(tmp, "out", top["file"]), "rt", encoding="utf-8") as f:
                self.assertEqual(len(f.read().strip().splitlines()), 2)
            code, d = Hub(dbp).api("/api/journals", {"q": "1884"})
            self.assertEqual(d["total"], 1)
            self.assertEqual(d["journals"][0]["doi_prefix"], "10.11162")
            self.assertEqual(d["journals"][0]["records"], 2)
        finally:
            jalc.stop()


class JaLCScreenFormatTest(unittest.TestCase):
    """JaLC検索画面の「JSONで保存」形式（実ファイル）の取り込み。"""
    SAMPLE = os.path.join(os.path.dirname(__file__), "data", "jalc_screen_sample.json")

    def test_convert_and_ingest(self):
        import shutil
        raw = open(self.SAMPLE, "rb").read()
        d = tempfile.mkdtemp()
        try:
            store = LocalStore(os.path.join(d, "s"))
            store.put("raw/jalc/20261011T000000Z/screen/jalc_x.json", raw)
            conn = connect(os.path.join(d, "h.sqlite"))
            self.assertEqual(ingest(store, conn, "jalc", "jalc", log=lambda *a: None)["new"], 1)
            hub = Hub(os.path.join(d, "h.sqlite"), store=store)
            code, r = hub.api("/api/works", {"q": "13g1001"})
            w = r["results"][0]["work"]
            self.assertEqual(w["type"], "article")
            self.assertEqual(w["site_id"], "SI/JST.JSTAGE")
            self.assertEqual(w["issn"], ["1884-4375", "2186-3725"])
            self.assertEqual(w["journal_title_en"], "Journal of Environment and Safety")
            self.assertEqual(w["publication_year"], 2014)
            self.assertEqual(w["biblio"]["first_page"], "3")
            self.assertEqual(len(w["authorships"]), 8)
            self.assertEqual(w["authorships"][0]["author"]["display_name"], "Kazushige Suzuki")
            self.assertIn("鈴木 一成", [n["text"] for n in w["authorships"][0]["names"]])
            # 所属は sequence で著者に対応（5番目の著者は2つ）
            self.assertEqual(len(w["authorships"][4]["raw_affiliation_strings"]), 4)
            self.assertEqual(w["third_party_use"], "不可")
            self.assertFalse(w["has_abstract_in_source"])
            code, raw_r = hub.api("/api/raw/" + "doi%3A10.11162%2Fdaikankyo.13g1001".replace("%3A", ":").replace("%2F", "/"), {})
            self.assertEqual(code, 200)
            self.assertEqual(raw_r["raw"]["doi"], "10.11162/daikankyo.13G1001")
        finally:
            shutil.rmtree(d)

    def test_rest_record_not_overwritten(self):
        import shutil, json as _j
        d = tempfile.mkdtemp()
        try:
            store = LocalStore(os.path.join(d, "s"))
            rest = {"data": {"doi": "10.11162/daikankyo.13G1001", "siteId": "SI/JST.JSTAGE", "content_type": "JA",
                             "title_list": [{"lang": "ja", "title": "REST版"}]}}
            store.put("raw/jalc/20261010T000000Z/10.11162/00000.jsonl", (_j.dumps(rest) + "\n").encode())
            store.put("raw/jalc/20261011T000000Z/screen/x.json", open(self.SAMPLE, "rb").read())
            conn = connect(os.path.join(d, "h.sqlite"))
            ingest(store, conn, "jalc", "jalc", log=lambda *a: None)
            t = conn.execute("SELECT work_json FROM records").fetchone()[0]
            self.assertIn("REST版", t)
        finally:
            shutil.rmtree(d)


class PerPrefixTest(TestJaLCFlow):
    def test_per_prefix_limits_each_prefix_and_keeps_incremental_state(self):
        r = harvest_jalc(self.client, self.store, "jalc", site_id="SI/JST.JSTAGE", per_prefix=1,
                         now="2026-10-10T00:00:00Z", log=noop)
        self.assertEqual(r["dois"], 2)   # J-STAGEの2プレフィックスから1件ずつ
        st = json.loads(self.store.get("state/jalc.json").decode("utf-8"))
        # 途中で打ち切ったプレフィックスは「完了」にしない（次回の差分取得で残りを取れる）
        self.assertIn("10.11163", st["prefixes"])
        self.assertNotIn("10.11162", st["prefixes"])


class TopicsHandoffTest(unittest.TestCase):
    """分野の自動推定: 入力の書き出し → ツールの出力を取り込み → APIに別枠で表示。"""

    def test_roundtrip(self):
        import shutil
        from jhub.topics import export_topics_input, import_topics
        d = tempfile.mkdtemp()
        try:
            store = LocalStore(os.path.join(d, "s"))
            store.put("raw/jalc/20261011T000000Z/screen/x.json", open(JaLCScreenFormatTest.SAMPLE, "rb").read())
            db = os.path.join(d, "h.sqlite")
            conn = connect(db)
            ingest(store, conn, "jalc", "jalc", log=lambda *a: None)
            inp = os.path.join(d, "in.jsonl")
            self.assertEqual(export_topics_input(db, inp), 1)
            rec = json.loads(open(inp, encoding="utf-8").readline())
            self.assertTrue(rec["id"].startswith("doi:10.11162/"))
            self.assertIn("オスミウム", rec["title"])
            out = os.path.join(d, "out.jsonl")
            top = {"id": "https://openalex.org/T1", "display_name": "Environmental Chemistry", "score": 0.81}
            with open(out, "w", encoding="utf-8") as f:
                f.write(json.dumps({"work_id": rec["id"], "primary_topic": top, "topics": [top], "method": "ensemble",
                                    "emb_score": 0.8, "bm25_score": 0.3}) + "\n")
                f.write(json.dumps({"work_id": "doi:10.1/none", "topics": [top]}) + "\n")
                f.write(json.dumps({"work_id": rec["id"].replace("1001", "9999")}) + "\n")
            self.assertEqual(import_topics(db, out, version="t1"), {"saved": 1, "unknown": 2, "empty": 0})
            self.assertEqual(export_topics_input(db, inp), 0)   # 推定済みは既定で除く
            self.assertEqual(export_topics_input(db, inp, only_missing=False), 1)
            hub = Hub(db)
            code, r = hub.api("/api/works/" + rec["id"], {})
            self.assertEqual(r["auto_topics"]["primary_topic"]["display_name"], "Environmental Chemistry")
            self.assertEqual(r["auto_topics"]["version"], "t1")
            self.assertNotIn("topics", r["work"])        # 整形結果とは別枠
            # 再取り込み(--reprocess)をしても推定は消えない
            ingest(store, conn, "jalc", "jalc", reprocess=True, log=lambda *a: None)
            code, r = hub.api("/api/works", {"q": "13g1001"})
            self.assertIn("auto_topics", r["results"][0])
        finally:
            shutil.rmtree(d)


class RepositoryListTest(unittest.TestCase):
    def test_names_from_prefix_match(self):
        import shutil
        from jhub.institutions import load_repositorylist
        d = tempfile.mkdtemp()
        try:
            db = os.path.join(d, "h.sqlite")
            c = connect(db)
            for i, (code, pfx) in enumerate([("00937", "10.18880"), ("00937", "10.18880"), ("00111", "10.99999"), ("00222", "10.15015")]):
                c.execute("INSERT INTO records(identifier,source,hub_datestamp,institution_code,doi_prefix) VALUES(?,?,?,?,?)",
                          ("doi:x%d" % i, "jalc", "2026-10-11T00:00:00Z", code, pfx))
            c.commit()
            csvp = os.path.join(d, "list.csv")
            with open(csvp, "w", encoding="utf-8-sig", newline="") as f:
                f.write("№,機関名,リポジトリ名,リポジトリURL,JaLC DOI Prefix,Crossref DOI Prefix,DataCite Prefix\n"
                        "1,横浜国立大学,横浜国立大学学術情報リポジトリ,https://ynu.repo.nii.ac.jp,10.18880,,\n"
                        "2,横浜市立大学,横浜市立大学学術機関リポジトリ,https://ycu.repo.nii.ac.jp,10.15015,,\n")
            r = load_repositorylist(db, csvp)
            self.assertEqual(r["matched"], 2)
            self.assertEqual(r["unmatched"], ["00111"])
            names = {x["code"]: x["name_ja"] for x in connect(db).execute("SELECT * FROM institutions")}
            self.assertEqual(names, {"00937": "横浜国立大学", "00222": "横浜市立大学"})
            code, d2 = Hub(db).api("/api/institutions", {})
            self.assertEqual(d2["institutions"][-1]["name_ja"] if d2["institutions"][-1]["code"] == "00937" else
                             [i for i in d2["institutions"] if i["code"] == "00937"][0]["name_ja"], "横浜国立大学")
        finally:
            shutil.rmtree(d)


class AbstractPolicyTest(unittest.TestCase):
    """抄録の出力ポリシー（IRDB=出力 / JaLC=既定は出さない・設定で切替）"""

    JALC = {"doi": "10.99999/abs.1", "title_list": [{"lang": "ja", "title": "題"}],
            "description_list": [{"description": "<p>これは&quot;抄録&quot;です</p>", "type": "Abstract", "lang": "ja"}],
            "third_party_use": "可"}

    def _store_jalc(self, d, data):
        store = LocalStore(os.path.join(d, "store"))
        store.put("raw/jalc/20261011T000000Z/10.99999/00001.jsonl",
                  (json.dumps({"data": data}, ensure_ascii=False) + "\n").encode("utf-8"))
        return store

    def _work(self, conn):
        return json.loads(conn.execute("SELECT work_json FROM records").fetchone()[0])

    def test_jalc_default_off_then_switch_by_reprocess(self):
        from jhub.policy import resolve_policy
        d = tempfile.mkdtemp()
        try:
            store = self._store_jalc(d, self.JALC)
            conn = connect(os.path.join(d, "h.sqlite"))
            ingest(store, conn, "jalc", "jalc", log=noop)
            w = self._work(conn)
            self.assertNotIn("abstracts", w)
            self.assertNotIn("_abstracts", w)
            self.assertTrue(w["has_abstract_in_source"])
            self.assertEqual(w["abstract_status"], "withheld_by_policy")
            # include に切替 → 再処理で出力される（HTML・文字参照は除去）
            ingest(store, conn, "jalc", "jalc", reprocess=True, log=noop, abstracts={"jalc": "include"})
            w = self._work(conn)
            self.assertEqual(w["abstract_status"], "included")
            self.assertEqual(w["abstracts"], [{"lang": "ja", "text": 'これは"抄録"です'}])
            # 元に戻す
            ingest(store, conn, "jalc", "jalc", reprocess=True, log=noop, abstracts={"jalc": "off"})
            self.assertNotIn("abstracts", self._work(conn))
            with self.assertRaises(ValueError):
                resolve_policy({"jalc": "yes"})
        finally:
            shutil.rmtree(d)

    def test_jalc_permitted_only(self):
        d = tempfile.mkdtemp()
        try:
            for tpu, want in (("可", "included"), ("不可", "not_permitted"), (None, "not_permitted")):
                data = dict(self.JALC)
                data["third_party_use"] = tpu
                store = self._store_jalc(d, data)
                conn = connect(os.path.join(d, "h_%s.sqlite" % want))
                ingest(store, conn, "jalc", "jalc", log=noop, abstracts={"jalc": "permitted_only"})
                self.assertEqual(self._work(conn)["abstract_status"], want)
        finally:
            shutil.rmtree(d)

    def test_no_abstract_status_none(self):
        d = tempfile.mkdtemp()
        try:
            store = self._store_jalc(d, {"doi": "10.99999/abs.2", "title_list": [{"lang": "ja", "title": "題"}]})
            conn = connect(os.path.join(d, "h.sqlite"))
            ingest(store, conn, "jalc", "jalc", log=noop, abstracts={"jalc": "include"})
            w = self._work(conn)
            self.assertEqual(w["abstract_status"], "none")
            self.assertNotIn("abstracts", w)
        finally:
            shutil.rmtree(d)

    def test_irdb_abstract_included_by_default(self):
        xml = ('<jpcoar:jpcoar xmlns:jpcoar="https://github.com/JPCOAR/schema/blob/master/2.0/" '
               'xmlns:datacite="http://datacite.org/schema/kernel-4" xmlns:dc="http://purl.org/dc/elements/1.1/" '
               'xmlns:xml="http://www.w3.org/XML/1998/namespace">'
               '<dc:title xml:lang="ja">題</dc:title>'
               '<datacite:description descriptionType="Abstract" xml:lang="ja">要旨です</datacite:description>'
               '<datacite:description descriptionType="Abstract" xml:lang="en">An abstract.</datacite:description>'
               '<datacite:description descriptionType="Other">注記</datacite:description></jpcoar:jpcoar>')
        from jhub.policy import apply_abstract_policy, DEFAULT_POLICY
        w = N.normalize_jpcoar(ET.fromstring(xml), "oai:x:1")
        self.assertTrue(w["has_abstract_in_source"])
        apply_abstract_policy(w, "irdb", DEFAULT_POLICY)
        self.assertEqual([a["lang"] for a in w["abstracts"]], ["ja", "en"])
        self.assertEqual(w["abstract_status"], "included")
        w2 = N.normalize_jpcoar(ET.fromstring(xml), "oai:x:1")
        apply_abstract_policy(w2, "irdb", {"irdb": "off"})
        self.assertNotIn("abstracts", w2)
        self.assertEqual(w2["abstract_status"], "withheld_by_policy")


class RorEnrichTest(unittest.TestCase):
    """ROR補完: 生データのRORは尊重し、無い所属・助成機関だけをAPI照合で付与。生データは不変。"""

    class FakeClient:
        def __init__(self):
            self.calls = []

        def search(self, name):
            self.calls.append("search:" + name)
            if name == "京都大学":
                return {"items": [{"id": "https://ror.org/02kpeqv85", "names": [
                    {"value": "Kyoto University", "types": ["ror_display"]},
                    {"value": "京都大学", "types": ["label"]}]}]}
            return {"items": []}

        def match(self, s):
            self.calls.append(s)
            known = {"東京大学": ("https://ror.org/057zh3y96", "University of Tokyo", 1.0, True),
                     "purdue university": ("https://ror.org/0bbbbbb22", "Hue University of Education", 1.0, True),
                     "japan society for the promotion of science": ("https://ror.org/00hhkn466", "JSPS", 1.0, True),
                     "曖昧大学": ("https://ror.org/0aaaaaa11", "Maybe U", 0.6, False)}
            k = s.lower()
            if k not in known:
                return {"items": []}
            i, n, sc, ch = known[k]
            return {"items": [{"score": sc, "chosen": ch, "matching_type": "EXACT",
                               "organization": {"id": i, "names": [{"value": n, "types": ["ror_display"]}]
                                            + ([{"value": "東京大学", "types": ["label"]}] if "Tokyo" in n else [])
                                            + ([{"value": "Japan Society for the Promotion of Science", "types": ["label"]}] if n == "JSPS" else [])}}]}

    def _work(self):
        return {"authorships": [
            {"raw_affiliation_strings": ["東京大学"], "institutions": []},
            {"raw_affiliation_strings": ["東京大学"], "institutions": [{"ror": "https://ror.org/05dxps055"}]},
            {"raw_affiliation_strings": ["曖昧大学"], "institutions": []},
            {"raw_affiliation_strings": ["不明機関"], "institutions": []}],
            "grants": [{"funder_display_name": "Japan Society for the Promotion of Science"},
                       {"funder_display_name": "X", "funder_ror": "https://ror.org/0abc12345"}]}

    def _db(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        return connect(os.path.join(d, "h.sqlite"))

    def test_enrich_and_apply(self):
        from jhub import ror as R
        conn = self._db()
        w = self._work()
        conn.execute("INSERT INTO records(identifier,source,hub_datestamp,deleted,work_json) VALUES('x','jalc','2026-01-01T00:00:00Z',0,?)",
                     (json.dumps(w, ensure_ascii=False),))
        fc = self.FakeClient()
        r = R.enrich(conn, fc, {"min_score": 0.9}, log=noop, now="2026-10-17T00:00:00Z")
        # 同じ文字列は1回だけ照会。RORが既にある所属・助成は照会しない
        self.assertEqual(sorted(fc.calls), sorted(["東京大学", "曖昧大学", "search:曖昧大学", "不明機関", "Japan Society for the Promotion of Science"]))
        self.assertEqual(r["records_updated"], 1)
        out = json.loads(conn.execute("SELECT work_json FROM records").fetchone()[0])
        a = out["authorships"]
        self.assertEqual(a[0]["institutions"][0]["ror"], "https://ror.org/057zh3y96")
        self.assertEqual(a[0]["institutions"][0]["source"], "ror_affiliation_api")
        self.assertEqual(a[1]["institutions"], [{"ror": "https://ror.org/05dxps055"}])   # 生データ尊重
        self.assertEqual(a[2]["institutions"], [])   # chosen=False / 低スコアは付けない
        self.assertEqual(a[3]["institutions"], [])
        self.assertEqual(out["grants"][0]["funder"]["ror"], "https://ror.org/00hhkn466")
        self.assertEqual(out["grants"][1]["funder"]["ror"], "https://ror.org/0abc12345")
        # 2回目はAPIを呼ばない（「一致なし」もキャッシュ済み）
        fc2 = self.FakeClient()
        R.enrich(conn, fc2, {"min_score": 0.9}, log=noop)
        self.assertEqual(fc2.calls, [])

    def test_ingest_applies_cache_and_raw_untouched(self):
        from jhub import ror as R
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        store = LocalStore(os.path.join(tmp, "store"))
        conn = connect(os.path.join(tmp, "h.sqlite"))
        irdb = MockIRDB(make_records(1), page_size=10)
        harvest_oai(OAIClient(irdb.url, delay=0), store, "irdb", "jpcoar_2.0")
        irdb.close() if hasattr(irdb, "close") else None
        before = {k: store.get(k) for k in store.list("raw/irdb/")}
        ingest(store, conn, "irdb", "oai", "jpcoar_2.0", log=noop, ror={"enabled": True})
        affs = [s for row in conn.execute("SELECT work_json FROM records WHERE work_json IS NOT NULL")
                for a in json.loads(row[0])["authorships"] for s in a["raw_affiliation_strings"]]
        if not affs:
            self.skipTest("サンプルに所属がない")
        R.cache_put(conn, R.norm_key(affs[0]), affs[0],
                    {"ror": "https://ror.org/057zh3y96", "display_name": "U", "score": 1.0, "matching_type": "EXACT"}, "t")
        ingest(store, conn, "irdb", "oai", "jpcoar_2.0", log=noop, reprocess=True, ror={"enabled": True})
        found = [i for row in conn.execute("SELECT work_json FROM records WHERE work_json IS NOT NULL")
                 for a in json.loads(row[0])["authorships"] for i in a["institutions"]
                 if i.get("source") == "ror_affiliation_api"]
        self.assertTrue(found)
        self.assertEqual(before, {k: store.get(k) for k in store.list("raw/irdb/")})

    def test_compound_skipped_and_japanese_head_exact(self):
        from jhub import ror as R
        fc = self.FakeClient()
        self.assertIsNone(R.resolve_string(fc, "NISTEP / RIKEN / The University of Tokyo", 0.9))
        self.assertEqual(fc.calls, [])          # 複数機関並記は照会もしない
        m = R.resolve_string(fc, "京都大学環境科学センター", 0.9)
        self.assertEqual(m["ror"], "https://ror.org/02kpeqv85")
        self.assertEqual(m["matching_type"], "EXACT_NAME")
        self.assertIsNone(R.resolve_string(fc, "未知大学理学部", 0.9))   # 先頭名が完全一致しなければ付けない
        self.assertEqual(R.jp_head("東北大学大学院工学研究科"), "東北大学")

    def test_tie_exact_name_and_regex_override(self):
        from jhub import ror as R
        def org(i, names): return {"score": 1.0, "chosen": False, "matching_type": "SINGLE SEARCH",
                                  "organization": {"id": i, "names": [{"value": n, "types": []} for n in names]}}
        resp = {"items": [org("https://ror.org/02m7axw05", ["JSPS", "JSPS London"]),
                          org("https://ror.org/00hhkn466", ["JSPS", "Japan Society for the Promotion of Science"])]}
        m = R.best_match(resp, 0.9, "Japan Society for the Promotion of Science")
        self.assertEqual(m["ror"], "https://ror.org/00hhkn466")
        self.assertIsNone(R.best_match(resp, 0.9, "JSPS"))          # 「JSPS」は2組織に一致 → 付けない
        R.set_overrides({"re:jsps kakenhi": "https://ror.org/00hhkn466"})
        self.addCleanup(R.set_overrides, {})
        c = self._db()
        self.assertEqual(R.cache_get(c, R.norm_key("the JSPS KAKENHI (19J15582)"))["ror"], "https://ror.org/00hhkn466")

    def test_requery_after_cache_cleared(self):
        from jhub import ror as R
        w = {"authorships": [{"raw_affiliation_strings": ["東京大学"], "institutions": [
            {"ror": "https://ror.org/057zh3y96", "source": "ror_affiliation_api"}]}], "grants": []}
        self.assertEqual(R.affiliations_without_ror(w), ["東京大学"])   # 自動付与分は数えない

    def test_unrelated_name_rejected(self):
        from jhub import ror as R
        self.assertIsNone(R.resolve_string(self.FakeClient(), "Purdue University", 0.9))

    def test_manual_overrides_win(self):
        from jhub import ror as R
        conn = self._db()
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        f = os.path.join(d, "o.csv")
        open(f, "w", encoding="utf-8").write("query,ror\nUniversity of Occupational and Environmental Health,https://ror.org/020p3h829\n誤り,none\n")
        R.set_overrides(R.load_overrides(f))
        self.addCleanup(R.set_overrides, {})
        w = {"authorships": [{"raw_affiliation_strings": ["University of Occupational and Environmental Health"],
                              "institutions": []}], "grants": []}
        R.cache_put(conn, R.norm_key("University of Occupational and Environmental Health"), "x",
                    {"ror": "https://ror.org/0590fg074", "display_name": "Environmental Health", "score": 1.0,
                     "matching_type": "SINGLE SEARCH"}, "t")
        R.apply_ror(w, conn, {"min_score": 0.9})
        self.assertEqual(w["authorships"][0]["institutions"][0]["ror"], "https://ror.org/020p3h829")

