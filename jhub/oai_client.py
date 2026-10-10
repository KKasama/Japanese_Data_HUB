"""OAI-PMH クライアント（再開・リトライ・差分に対応）。標準ライブラリのみ。"""
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

OAI = "http://www.openarchives.org/OAI/2.0/"


def q(tag):
    return "{%s}%s" % (OAI, tag)


class OAIError(Exception):
    def __init__(self, code, message=""):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message


IP_HINT = (
    "badArgument が返りました。IRDBは申請済みIPからのレコード取得だけを許可している可能性があります"
    "（Identify/ListSetsは通るのにListRecordsだけ失敗する場合）。申請IPのマシンから実行するか、"
    "NII機関リポジトリ担当へIPの追加を申請してください。"
)


class OAIClient:
    def __init__(self, base_url, user_agent="jhub/0.1", timeout=60, retries=4,
                 backoff=2.0, delay=0.0, sleep=time.sleep):
        self.base_url = base_url
        self.user_agent = user_agent
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.delay = delay
        self.sleep = sleep

    def request(self, params):
        url = self.base_url + ("&" if "?" in self.base_url else "?") + urllib.parse.urlencode(params)
        last = None
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    wait = self.backoff ** attempt
                    ra = e.headers.get("Retry-After") if e.headers else None
                    if ra and ra.isdigit():
                        wait = max(wait, int(ra))
                    self.sleep(wait)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = e
                if attempt < self.retries:
                    self.sleep(self.backoff ** attempt)
                    continue
                raise
        raise last  # pragma: no cover

    def pages(self, verb, metadata_prefix=None, set_spec=None, from_=None, until=None,
              resumption_token=None, hint_ip=True):
        """ページごとに (生バイト列, 次のresumptionToken or None) を返す。"""
        if resumption_token:
            params = {"verb": verb, "resumptionToken": resumption_token}
        else:
            params = {"verb": verb}
            if metadata_prefix:
                params["metadataPrefix"] = metadata_prefix
            if set_spec:
                params["set"] = set_spec
            if from_:
                params["from"] = from_
            if until:
                params["until"] = until
        while True:
            raw = self.request(params)
            root = ET.fromstring(raw)
            err = root.find(q("error"))
            if err is not None:
                code = err.get("code", "")
                if code == "noRecordsMatch":
                    return
                msg = (err.text or "").strip()
                if code == "badArgument" and hint_ip:
                    msg = msg + " | " + IP_HINT
                raise OAIError(code, msg)
            cont = root.find(q(verb))
            tok = cont.find(q("resumptionToken")) if cont is not None else None
            nxt = tok.text.strip() if tok is not None and tok.text and tok.text.strip() else None
            yield raw, nxt
            if not nxt:
                return
            params = {"verb": verb, "resumptionToken": nxt}
            if self.delay:
                self.sleep(self.delay)
