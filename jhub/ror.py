"""ROR（Research Organization Registry）の補完。

方針（生データ優先）:
  * 生データ(raw)は一切変更しない。ROR補完は「整形後のJSON(work_json)」にだけ付ける。
  * 生データに ROR が既にある所属（JPCOAR の affiliation/nameIdentifier[ROR] 等）は、そのまま尊重し、触らない。
  * 無い所属だけ、所属文字列を ROR 公式API（affiliation マッチング）で照合し、
    「chosen（APIが確信ありと判定）」かつ score >= min_score のときだけ付与する。
    付与したものは {"source": "ror_affiliation_api", "score": ...} を付け、生データ由来と区別できる。
  * 照合結果は DB の ror_cache に残す（同じ所属文字列は二度と問い合わせない。ingest --reprocess でも消えない）。
  * ingest 中はネットワークを使わず、キャッシュだけを引く。API照会は enrich-ror コマンドで行う。
"""
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

SOURCE_TAG = "ror_affiliation_api"
ROR_RE = re.compile(r"(?:https?://ror\.org/)?(0[a-z0-9]{6}[0-9]{2})", re.I)
DEFAULT = {"enabled": False, "min_score": 0.9, "base_url": "https://api.ror.org/v2/organizations",
           "delay": 0.2, "client_id": None, "max_len": 300, "overrides": None}


def resolve_config(cfg):
    out = dict(DEFAULT)
    out.update(cfg or {})
    return out


def norm_key(s):
    """キャッシュのキー。全角半角・大文字小文字・空白を揃える。"""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s or "")).strip().lower()


def ror_url(v):
    m = ROR_RE.search(v or "")
    return "https://ror.org/" + m.group(1).lower() if m else None


def _display_name(org):
    names = org.get("names") or []
    for t in ("ror_display", "label"):
        for n in names:
            if t in (n.get("types") or []):
                return n.get("value")
    return names[0].get("value") if names else None


class RorClient:
    """ROR v2 の affiliation マッチング。client_id（任意）を付けると上限が緩くなる。"""

    def __init__(self, base_url=DEFAULT["base_url"], delay=0.2, client_id=None,
                 user_agent="jhub", retries=3, opener=None):
        self.base_url, self.delay, self.client_id = base_url, delay, client_id
        self.ua, self.retries = user_agent, retries
        self._open = opener or urllib.request.urlopen
        self._last = 0.0

    def match(self, affiliation):
        return self._fetch(self.base_url + "?" + urllib.parse.urlencode({"affiliation": affiliation}))

    def _fetch(self, url):
        headers = {"User-Agent": self.ua, "Accept": "application/json"}
        if self.client_id:
            headers["Client-Id"] = self.client_id
        for attempt in range(self.retries):
            wait = self.delay - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                with self._open(urllib.request.Request(url, headers=headers), timeout=30) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries - 1:
                    time.sleep(float(e.headers.get("Retry-After") or 2 * (attempt + 1)))
                    continue
                raise
            except urllib.error.URLError:
                if attempt < self.retries - 1:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise


    def search(self, name):
        """名称検索(query=)。日本語の機関名の完全一致確認に使う。"""
        url = self.base_url + "?" + urllib.parse.urlencode({"query": name})
        return self._fetch(url)


def _name_consistent(org, query):
    """採用した組織のいずれかの名称（別名・日本語名を含む）が、所属文字列に含まれる（またはその逆）こと。
    『Purdue University → Hue University of Education』のような、名称が無関係な誤一致を落とす安全弁。"""
    q = norm_key(query)
    for n in org.get("names") or []:
        v = norm_key(n.get("value"))
        v2 = v[4:] if v.startswith("the ") else v
        if v2 and (v2 in q or (len(q) >= 4 and q in v)):
            return True
    return False


def best_match(resp, min_score, query=None):
    """APIの応答から採用する1件を選ぶ。chosen かつ score>=min_score、かつ名称が整合するときだけ。"""
    items = (resp or {}).get("items") or []
    if query is not None and not any(i.get("chosen") for i in items):
        # APIが候補同士の同点で chosen を付けなかった場合（例: JSPS本体とJSPSロンドン事務所）。
        # 所属文字列が、ちょうど1つの組織の正式名称・別名と完全一致するときだけ採用する。
        q = norm_key(query)
        hit = [i for i in items if float(i.get("score") or 0) >= min_score
               and any(norm_key(n.get("value")) == q for n in (i.get("organization") or {}).get("names") or [])]
        if len(hit) == 1:
            org = hit[0]["organization"]
            rid = ror_url(org.get("id"))
            if rid:
                return {"ror": rid, "display_name": _display_name(org), "score": float(hit[0]["score"]),
                        "matching_type": "EXACT_NAME_TIE"}
    for it in items:
        if it.get("chosen") and float(it.get("score") or 0) >= min_score:
            org = it.get("organization") or {}
            rid = ror_url(org.get("id"))
            if rid and (query is None or _name_consistent(org, query)):
                return {"ror": rid, "display_name": _display_name(org), "score": float(it["score"]),
                        "matching_type": it.get("matching_type")}
    return None


_COMPOUND = re.compile(r"[/／、;；]|&middot;|・.*・")
_JP = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_JP_HEAD = re.compile(r"^(.{1,25}?(?:大学院大学|大学|研究所|研究機構|研究開発機構|機構|病院))")


def is_compound(s):
    """複数機関が1文字列に並んでいる可能性が高いもの（誤付与を避けるため自動照合しない）。"""
    return bool(_COMPOUND.search(s or ""))


def jp_head(s):
    """日本語の所属から先頭の機関名（「京都大学」など）を取り出す。"""
    m = _JP_HEAD.match(unicodedata.normalize("NFKC", s or "").replace(" ", ""))
    return m.group(1) if m else None


def exact_name_match(resp, name):
    """名称検索の結果から、名称が完全一致する組織だけを採用（score=1.0, matching_type=EXACT_NAME）。"""
    want = norm_key(name)
    for it in (resp or {}).get("items") or []:
        org = it.get("organization") or it
        if any(norm_key(n.get("value")) == want for n in org.get("names") or []):
            rid = ror_url(org.get("id"))
            if rid:
                return {"ror": rid, "display_name": _display_name(org), "score": 1.0, "matching_type": "EXACT_NAME"}
    return None


def resolve_string(client, s, min_score):
    """1つの所属文字列 → 照合結果。①複数機関並記は対象外 ②affiliation照合 ③日本語は先頭の機関名を完全一致で再照会。"""
    if is_compound(s):
        return None
    m = best_match(client.match(s), min_score, s)
    if m or not _JP.search(s):
        return m
    head = jp_head(s)
    return exact_name_match(client.search(head), head) if head else None


def load_overrides(path):
    """手動の訂正表 CSV（列: query,ror）。ror が空/none なら「付与しない」。自動照合より常に優先。"""
    import csv
    out = {}
    if path:
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                raw_q = (row.get("query") or "").strip()
                q = raw_q if raw_q.startswith("re:") else norm_key(raw_q)
                if q:
                    r = (row.get("ror") or "").strip()
                    out[q] = None if r.lower() in ("", "none") else ror_url(r)
    return out


# ---- キャッシュ(DB) ----
_OVR = {}
_OVR_RE = []


def set_overrides(d):
    """訂正表。キーが "re:" で始まるものは正規表現（正規化後の文字列に re.search）。"""
    _OVR.clear()
    _OVR_RE.clear()
    for k, v in (d or {}).items():
        if k.startswith("re:"):
            _OVR_RE.append((re.compile(k[3:]), v))
        else:
            _OVR[k] = v


def _override(key):
    if key in _OVR:
        return True, _OVR[key]
    for rx, v in _OVR_RE:
        if rx.search(key):
            return True, v
    return False, None


def cache_get(conn, key):
    hit, v = _override(key)
    if hit:   # 手動の訂正表が最優先
        return {"ror": v, "display_name": None, "score": 1.0, "matching_type": "MANUAL"} if v else None
    r = conn.execute("SELECT ror, display_name, score, matching_type FROM ror_cache WHERE key=?", (key,)).fetchone()
    return None if r is None else {"ror": r[0], "display_name": r[1], "score": r[2], "matching_type": r[3]}


def cache_has(conn, key):
    return conn.execute("SELECT 1 FROM ror_cache WHERE key=?", (key,)).fetchone() is not None


def cache_put(conn, key, raw, m, checked_at):
    conn.execute(
        "INSERT OR REPLACE INTO ror_cache(key, query, ror, display_name, score, matching_type, checked_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (key, raw, m and m["ror"], m and m["display_name"], m and m["score"], m and m["matching_type"], checked_at))


# ---- 付与（ネットワーク不要） ----
def _funder_strings(g):
    out = [t.get("text") for t in g.get("funder_names") or [] if t.get("text")]
    if g.get("funder_display_name"):
        out.append(g["funder_display_name"])
    return list(dict.fromkeys(x for x in out if x and x.strip()))


def _funder_has_ror(g):
    return bool(ror_url(g.get("funder_ror")) or ror_url((g.get("funder") or {}).get("ror")))


def affiliations_without_ror(work):
    """RORが無い著者の所属文字列・助成機関名を返す。"""
    out = []
    for g in (work or {}).get("grants") or []:
        if not _funder_has_ror(g) or (g.get("funder") or {}).get("source") == SOURCE_TAG:
            out.extend(_funder_strings(g))
    for a in (work or {}).get("authorships") or []:
        # 以前の自動付与分(source=ror_affiliation_api)は「RORあり」と数えない（キャッシュを消した後に再照会するため）
        if any(i.get("ror") for i in a.get("institutions") or [] if i.get("source") != SOURCE_TAG):
            continue
        out.extend(s for s in a.get("raw_affiliation_strings") or [] if s and s.strip())
    return out


def apply_ror(work, conn, cfg=None):
    """キャッシュにある照合結果を work に付ける。生データのRORは尊重（重複追加も、上書きもしない）。
    戻り値: 付与した件数。"""
    if not work:
        return 0
    cfg = resolve_config(cfg)
    n = 0
    for a in work.get("authorships") or []:
        insts = a.setdefault("institutions", [])
        # 以前の自動付与分は作り直す（キャッシュ・閾値の変更を反映するため）。生データ由来は残す。
        insts[:] = [i for i in insts if i.get("source") != SOURCE_TAG]
        if any(i.get("ror") for i in insts):
            continue
        seen = set()
        for s in a.get("raw_affiliation_strings") or []:
            if not s or len(s) > cfg["max_len"]:
                continue
            m = cache_get(conn, norm_key(s))
            if m and m["ror"] and m["score"] is not None and m["score"] >= cfg["min_score"] and m["ror"] not in seen:
                seen.add(m["ror"])
                insts.append({"ror": m["ror"], "display_name": m["display_name"], "source": SOURCE_TAG,
                              "score": m["score"], "matched_string": s})
                n += 1
    for g in work.get("grants") or []:
        if (g.get("funder") or {}).get("source") == SOURCE_TAG:
            del g["funder"]                       # 作り直す
        if _funder_has_ror(g):
            if g.get("funder_ror") and not g.get("funder"):   # 生データのRORを標準の位置にも出す
                g["funder"] = {"ror": ror_url(g["funder_ror"]), "source": "raw"}
            continue
        for s in _funder_strings(g):
            if len(s) > cfg["max_len"]:
                continue
            m = cache_get(conn, norm_key(s))
            if m and m["ror"] and m["score"] is not None and m["score"] >= cfg["min_score"]:
                g["funder"] = {"ror": m["ror"], "display_name": m["display_name"], "source": SOURCE_TAG,
                               "score": m["score"], "matched_string": s}
                n += 1
                break
    return n


# ---- 照会（ネットワークあり）----
def enrich(conn, client, cfg=None, log=print, limit=None, now=None, retry=False):
    """RORが無い所属文字列を重複なく集め、未照会のものだけAPIで照合 → キャッシュ → 既存レコードに反映。"""
    from .util import utc_now
    cfg = resolve_config(cfg)
    set_overrides(load_overrides(cfg.get("overrides")))
    now_s = utc_now(now)
    if retry:   # 「一致なし」と、現在は対象外(複数機関並記)の過去の一致を捨てて、やり直す
        conn.execute("DELETE FROM ror_cache WHERE ror IS NULL")
        for k, q in conn.execute("SELECT key, query FROM ror_cache").fetchall():
            if is_compound(q):
                conn.execute("DELETE FROM ror_cache WHERE key=?", (k,))
        conn.commit()
    todo, total = {}, 0
    for row in conn.execute("SELECT identifier, work_json FROM records WHERE deleted=0 AND work_json IS NOT NULL"):
        for s in affiliations_without_ror(json.loads(row[1])):
            total += 1
            if len(s) <= cfg["max_len"]:
                k = norm_key(s)
                if k and not cache_has(conn, k) and k not in todo:
                    todo[k] = s
    log("[ror] RORなしの所属 %d 件のうち、未照会の種類 %d" % (total, len(todo)))
    done = hit = err = 0
    for k, s in list(todo.items())[:limit]:
        try:
            m = resolve_string(client, s, cfg["min_score"])
        except Exception as e:  # 一時的な失敗はキャッシュしない（次回やり直す）
            err += 1
            log("[ror] 照会に失敗: %r (%s)" % (s[:40], e))
            if err >= 5:
                log("[ror] 失敗が続くため中断します（ネットワーク/上限を確認）")
                break
            continue
        cache_put(conn, k, s, m, now_s)
        done += 1
        hit += 1 if m else 0
        if done % 50 == 0:
            conn.commit()
    conn.commit()
    changed = reapply(conn, cfg, now_s)
    inst = enrich_institutions(conn, client, cfg, now_s, log)
    log("[ror] 照会 %d 件（一致 %d）/ 失敗 %d / レコード更新 %d / 機関台帳へ付与 %d" % (done, hit, err, changed, inst))
    return {"queried": done, "matched": hit, "errors": err, "records_updated": changed, "institutions": inst}


def reapply(conn, cfg=None, now=None):
    """DB上の全レコードにキャッシュを再適用し、変わったものだけ hub_datestamp を更新（OAIで差分取得できる）。"""
    from .util import utc_now
    now_s = utc_now(now)
    n = 0
    for row in conn.execute("SELECT identifier, work_json FROM records WHERE deleted=0 AND work_json IS NOT NULL").fetchall():
        w = json.loads(row[1])
        apply_ror(w, conn, cfg)
        new = json.dumps(w, ensure_ascii=False, sort_keys=True)
        if new != row[1]:
            conn.execute("UPDATE records SET work_json=?, hub_datestamp=? WHERE identifier=?", (new, now_s, row[0]))
            n += 1
    conn.commit()
    return n


def enrich_institutions(conn, client, cfg, now_s, log=print):
    """機関台帳(institutions)のRORが空の行を、英語名→日本語名の順で照合して補う。"""
    n = 0
    for r in conn.execute("SELECT code, name_ja, name_en FROM institutions WHERE ror IS NULL OR ror=''").fetchall():
        for name in (r[2], r[1]):
            if not name:
                continue
            k = norm_key(name)
            if not cache_has(conn, k):
                try:
                    cache_put(conn, k, name, resolve_string(client, name, cfg["min_score"]), now_s)
                except Exception as e:
                    log("[ror] 機関 %s の照会に失敗: %s" % (r[0], e))
                    continue
            m = cache_get(conn, k)
            if m and m["ror"] and m["score"] >= cfg["min_score"]:
                conn.execute("UPDATE institutions SET ror=? WHERE code=?", (m["ror"], r[0]))
                n += 1
                break
    conn.commit()
    return n
