"""収集の実行。生データを先に保存し、状態を逐次記録するので、中断しても続きから再開できる。"""
import json

from .util import utc_now


def _load_state(store, key):
    if store.exists(key):
        return json.loads(store.get(key).decode("utf-8"))
    return {}


def _save_state(store, key, state):
    store.put(key, json.dumps(state, ensure_ascii=False, indent=1).encode("utf-8"))


def harvest_oai(client, store, source, metadata_prefix, set_spec=None, full=False,
                from_=None, until=None, now=None, max_pages=None, log=print):
    """OAI-PMH(ListRecords)を収集。ページ単位でそのまま保存する。

    - 前回完了時刻を from に使う差分収集（full=True なら全件）。
    - 途中で止まったら、保存済みの resumptionToken から再開する。
    """
    skey = "state/%s.json" % source
    st = _load_state(store, skey)
    pages = 0
    if st.get("pending_token"):
        run_id, started, page_no = st["run_id"], st["started"], st["page"]
        log("[%s] 前回の続きから再開 (page %d)" % (source, page_no))
        gen = client.pages("ListRecords", resumption_token=st["pending_token"])
    else:
        started = utc_now(now)
        run_id = started.replace(":", "").replace("-", "")
        page_no = 0
        f = from_ if from_ else (None if full else st.get("last_complete"))
        log("[%s] 収集開始 from=%s set=%s" % (source, f, set_spec))
        gen = client.pages("ListRecords", metadata_prefix=metadata_prefix, set_spec=set_spec,
                           from_=f, until=until)
    for raw, nxt in gen:
        key = "raw/%s/%s/%06d.xml" % (source, run_id, page_no)
        store.put(key, raw)
        page_no += 1
        pages += 1
        st = {"run_id": run_id, "started": started, "page": page_no,
              "pending_token": nxt, "last_complete": st.get("last_complete")}
        _save_state(store, skey, st)
        if max_pages and pages >= max_pages and nxt:
            log("[%s] max_pages に達したため一時停止（次回再開）" % source)
            return {"pages": pages, "complete": False}
    st = {"last_complete": started, "pending_token": None, "last_run_id": run_id, "pages": page_no}
    _save_state(store, skey, st)
    log("[%s] 完了 pages=%d" % (source, pages))
    return {"pages": pages, "complete": True}


def harvest_jalc(client, store, source="jalc", prefixes=None, site_id=None, from_=None,
                 until=None, full=False, max_dois=None, chunk=500, now=None, log=print):
    """JaLC REST API: prefix → doilist → 各DOIのv2書誌データを JSONL にまとめて保存。

    prefixes を省略すると /prefixes（ra=JaLC）から取得し、site_id（例: SI/JST.JSTAGE）で絞れる。
    DOIごとの1リクエストが必要なため、全件(約1,400万DOI)の取得にはJaLC/JSTの許諾した方法が前提。
    """
    skey = "state/%s.json" % source
    st = _load_state(store, skey)
    pstate = st.get("prefixes", {})
    started = utc_now(now)
    day = started[:10]
    run_id = started.replace(":", "").replace("-", "")
    if prefixes is None:
        items = client.prefixes("JaLC")
        prefixes = [i["prefix"] for i in items if (not site_id or i.get("siteId") == site_id)]
    fetched = 0
    for pfx in prefixes:
        last = None if full else (from_ or pstate.get(pfx))
        buf, n_chunk = [], 0
        for doi, upd, sid in client.doilist(pfx, from_=last, until=until):
            if max_dois and fetched >= max_dois:
                break
            raw = client.doi_record(doi)
            if raw is None:
                continue
            buf.append(raw.decode("utf-8").strip().replace("\n", " "))
            fetched += 1
            if len(buf) >= chunk:
                store.put("raw/%s/%s/%s/%05d.jsonl" % (source, run_id, pfx, n_chunk),
                          ("\n".join(buf) + "\n").encode("utf-8"))
                buf, n_chunk = [], n_chunk + 1
        if buf:
            store.put("raw/%s/%s/%s/%05d.jsonl" % (source, run_id, pfx, n_chunk),
                      ("\n".join(buf) + "\n").encode("utf-8"))
        if not (max_dois and fetched >= max_dois):
            pstate[pfx] = day  # doilist の from は日単位。同日の再取得は冪等
        st = {"prefixes": pstate, "last_run_id": run_id}
        _save_state(store, skey, st)
        log("[%s] %s 完了 (累計DOI %d)" % (source, pfx, fetched))
        if max_dois and fetched >= max_dois:
            break
    return {"dois": fetched}
