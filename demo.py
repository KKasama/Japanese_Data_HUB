"""デモ: モックのIRDB/JaLCから収集→振り分け→整形→提供までを、外部接続なしで一通り動かす。

    python3 demo.py          # 実行後、http://127.0.0.1:8080 で OAI-PMH / API を確認できる（Ctrl+Cで終了）
"""
import os
import sys
import tempfile

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)
sys.path.insert(0, os.path.join(_here, "tests"))

from jhub.db import connect, load_institutions_csv
from jhub.harvest import harvest_jalc, harvest_oai
from jhub.ingest import ingest
from jhub.jalc_client import JaLCClient
from jhub.oai_client import OAIClient
from jhub.server import Hub, serve
from jhub.store import LocalStore
from mock_sources import MockIRDB, MockJaLC, make_records

tmp = tempfile.mkdtemp(prefix="jhub_demo_")
store, dbp = LocalStore(tmp + "/store"), tmp + "/hub.sqlite"
irdb, jalc = MockIRDB(make_records(3), page_size=4), MockJaLC()
harvest_oai(OAIClient(irdb.url, delay=0), store, "irdb", "jpcoar_2.0")
harvest_jalc(JaLCClient(jalc.url, max_rps=0), store, "jalc")
conn = connect(dbp)
ingest(store, conn, "irdb", "oai", "jpcoar_2.0")
ingest(store, conn, "jalc", "jalc")
load_institutions_csv(conn, os.path.join(os.path.dirname(os.path.abspath(__file__)), "institutions.example.csv"))
hub = Hub(dbp, "http://127.0.0.1:8080/oai", "ops@example.org", page_size=5)
print("""
ハブを起動しました（データは模擬）。ブラウザで次を開いて確認できます。
  機関一覧          http://127.0.0.1:8080/api/institutions
  件数              http://127.0.0.1:8080/api/stats
  機関別の作品      http://127.0.0.1:8080/api/works?institution=90001
  J-STAGEのISSN別   http://127.0.0.1:8080/api/works?journal=1884-4375
  OAI-PMH セット    http://127.0.0.1:8080/oai?verb=ListSets
  機関別の収集      http://127.0.0.1:8080/oai?verb=ListRecords&metadataPrefix=jpcoar_2.0&set=inst:90001
  J-STAGE(整形済)   http://127.0.0.1:8080/oai?verb=ListRecords&metadataPrefix=jhub_work&set=site:JST.JSTAGE
Ctrl+C で終了
""")
try:
    serve(hub, "127.0.0.1", 8080).serve_forever()
except KeyboardInterrupt:
    pass
