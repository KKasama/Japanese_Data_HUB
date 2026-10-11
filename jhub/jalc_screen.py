"""JaLC検索画面の「JSONで保存」形式 → REST API v2 の data 形式への変換。

検索画面の形式は、項目名・構造がREST v2と異なる（titles / creators(姓・名分割) / creators_affiliation /
日本語の種別名 など）。変換してから、既存の整形処理（normalize_jalc）に渡す。
REST v2にあってこの形式にない項目（siteId・参考文献・キーワードなど）は補えない。
"""
import re

# 検索画面の種別名 → JaLCの種別コード（未知の名称は None のままにし、type_raw に表示名を残す）
CONTENT_TYPES = {"ジャーナルアーティクル": "JA", "図書": "BK", "書籍": "BK", "研究データ": "RD", "データ": "RD",
                 "電子リソース": "EL", "汎用データ": "GD"}
# 検索画面のサイト名 → siteId（REST v2の siteId と同じ値。未知のサイトは None）
SITE_IDS = {"J-STAGE": "SI/JST.JSTAGE", "J-STAGE Data": "SI/JST.JSTAGE_Data", "Jxiv": "SI/JST.preprint",
            "NDLサーチ": "SI/NDL.NDL.Search", "NDL Search": "SI/NDL.NDL.Search"}
KNOWN = {"doi", "content_type", "titles", "journal_names", "creators", "creators_affiliation", "published",
         "publishers", "language", "volume", "issue", "first_page", "last_page", "paper_number", "site_name",
         "ra_name", "issns", "third_party_use", "summaries", "update_date"}


def _date(s):
    """'2014' / '2014-04' / '2014/4/1' / '20140401' → (年, 月, 日)。読めなければ空。"""
    s = str(s or "").strip()
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s) or re.fullmatch(r"(\d{4})[-/.](\d{1,2})(?:[-/.](\d{1,2}))?", s) \
        or re.fullmatch(r"(\d{4})()()", s)
    if not m:
        return {}
    out = {"publication_year": m.group(1)}
    if m.group(2):
        out["publication_month"] = m.group(2).zfill(2)
    if m.group(3):
        out["publication_day"] = m.group(3).zfill(2)
    return out


def _upd(s):
    s = str(s or "")
    return "%s-%s-%s" % (s[:4], s[4:6], s[6:8]) if re.fullmatch(r"\d{8}", s) else (s or None)


def screen_to_v2(r):
    """検索画面形式の1件 → REST v2 の data（dict）。"""
    d = {"doi": r.get("doi"), "ra": r.get("ra_name"), "site_name": r.get("site_name"),
         "siteId": SITE_IDS.get(r.get("site_name") or ""), "updated_date": _upd(r.get("update_date")),
         "third_party_use": r.get("third_party_use")}
    ct = r.get("content_type")
    d["content_type"] = CONTENT_TYPES.get(ct, ct)
    d["title_list"] = [{"lang": t.get("language"), "title": t.get("title")} for t in r.get("titles") or []]
    d["journal_title_name_list"] = [{"lang": j.get("language"), "journal_title_name": j.get("journal_name"), "type": "full"}
                                    for j in r.get("journal_names") or []]
    d["journal_id_list"] = [{"journal_id": i.get("issn_cd"), "type": "ISSN", "issn_type": i.get("issn_type")}
                            for i in r.get("issns") or []]
    d["publisher_list"] = [{"lang": p.get("language"), "publisher_name": p.get("publisher")}
                           for p in r.get("publishers") or []]
    d["publication_date"] = _date(r.get("published"))
    d["content_language"] = r.get("language")
    for k in ("volume", "issue", "first_page", "last_page"):
        d[k] = r.get(k)
    # 著者: sequence ごとに、言語別の姓・名をまとめる。所属は sequence で対応付ける
    seqs, order = {}, []
    for c in r.get("creators") or []:
        s = str(c.get("sequence"))
        if s not in seqs:
            seqs[s] = {"sequence": s, "type": "person", "names": [], "affiliation_list": []}
            order.append(s)
        seqs[s]["names"].append({"lang": c.get("language"), "last_name": c.get("family"), "first_name": c.get("given")})
    for a in r.get("creators_affiliation") or []:
        s = str(a.get("sequence"))
        if s in seqs:
            seqs[s]["affiliation_list"].append({
                "sequence": a.get("affiliation_sequence"),
                "affiliation_name_list": [{"lang": x.get("language"), "affiliation_name": x.get("affiliation")}
                                          for x in a.get("affiliations") or []]})
    d["creator_list"] = [seqs[s] for s in sorted(order, key=lambda x: (int(x) if x.isdigit() else 0))]
    # 抄録: 「-」は無しとして扱う。内容は保持しない方針のため、有無だけが後段に伝わる
    ab = [s for s in r.get("summaries") or [] if (s.get("summary") or "").strip() not in ("", "-")]
    if ab:
        d["description_list"] = [{"type": "abstract", "lang": s.get("language"), "description": s.get("summary")} for s in ab]
    return d


def records(raw_bytes):
    """ファイル全体（配列）から (data, 未対応の項目名) を順に返す。単体のオブジェクトも受け付ける。"""
    import json
    obj = json.loads(raw_bytes.decode("utf-8-sig"))
    for r in (obj if isinstance(obj, list) else [obj]):
        if isinstance(r, dict) and r.get("doi"):
            yield screen_to_v2(r), sorted(set(r) - KNOWN)
