"""JaLC REST API クライアント（公開API, 認証不要）。

注意:
- 書誌データは DOI 1件ごとの取得（/v2/dois/{doi}）。DOI一覧は /doilist/{prefix}（from/until は日単位）。
- JaLC一般利用規約 第7条: 機械的な大量取得は、JaLCが許諾する方法に限られる。
  既定は低頻度（max_rps）にし、全件取得はJSTの許諾（OAI-PMH利用など）を得てから行うこと。
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request


class JaLCClient:
    def __init__(self, base_url="https://api.japanlinkcenter.org", user_agent="jhub/0.1",
                 timeout=60, retries=4, backoff=2.0, max_rps=1.0, sleep=time.sleep, clock=time.monotonic):
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.min_interval = 1.0 / max_rps if max_rps else 0.0
        self.sleep = sleep
        self.clock = clock
        self._last = 0.0

    def _throttle(self):
        if self.min_interval:
            wait = self._last + self.min_interval - self.clock()
            if wait > 0:
                self.sleep(wait)
        self._last = self.clock()

    def get_raw(self, path, params=None):
        """生バイト列を返す。404は None（取得結果0件を含む）。"""
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    self.sleep(self.backoff ** attempt)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt < self.retries:
                    self.sleep(self.backoff ** attempt)
                    continue
                raise

    def prefixes(self, ra="JaLC"):
        raw = self.get_raw("/prefixes", {"ra": ra, "sort": "prefix", "order": "asc"})
        if raw is None:
            return []
        return json.loads(raw)["data"]["items"]

    def doilist(self, prefix, from_=None, until=None, rows=1000):
        """(DOI文字列, updated_date, siteId) を全ページ分返す。"""
        page = 1
        while True:
            params = {"rows": rows, "page": page, "sort": "updated_date", "order": "asc"}
            if from_:
                params["from"] = from_
            if until:
                params["until"] = until
            raw = self.get_raw("/doilist/%s" % prefix, params)
            if raw is None:
                return
            d = json.loads(raw)
            for it in d["data"]["items"]:
                yield it["dois"]["doi"], it.get("updated_date"), it.get("siteId")
            if page >= d["message"].get("totalPages", 1):
                return
            page += 1

    def doi_record(self, doi):
        """v2 の書誌データ（生バイト列）。"""
        return self.get_raw("/v2/dois/%s" % urllib.parse.quote(doi, safe="/"))
