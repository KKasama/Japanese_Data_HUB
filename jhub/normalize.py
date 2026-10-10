"""OpenAlex互換の work 形式への整形（試作: 主要項目のみ）。

本番では、既存の「JaLC–OpenAlex 項目対応表（79項目）」に置き換える。
方針: 抄録(abstract)は出力しない。有無だけ has_abstract_in_source として記録する。
"""
import html as _html
import re

from .classify import clean_doi, doi_prefix, journal_key, normalize_issn
from .util import XML_LANG, kid, kids, ln, text

LANG3 = {"jpn": "ja", "eng": "en", "chi": "zh", "zho": "zh", "kor": "ko", "fre": "fr", "fra": "fr",
         "ger": "de", "deu": "de", "rus": "ru", "spa": "es"}

TYPE_MAP = {
    "journal article": "article", "departmental bulletin paper": "article",
    "conference paper": "article", "conference object": "other", "article": "article",
    "doctoral thesis": "dissertation", "master thesis": "dissertation", "thesis": "dissertation",
    "dataset": "dataset", "book": "book", "book part": "book-chapter", "preprint": "preprint",
    "technical report": "report", "report": "report",
}


def _year(s):
    m = re.match(r"^(\d{4})", s or "")
    return int(m.group(1)) if m else None


def _full_date(s):
    return s if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s or "") else None


def _lang2(s):
    s = (s or "").strip()
    return LANG3.get(s, s[:2].lower() if s else None)


def _pick_name(names):
    """names: [(lang, text)]。カナ・ローマ字表記以外を優先。"""
    for lang, t in names:
        if t and (lang or "") not in ("ja-Kana", "ja-Latn"):
            return t
    return names[0][1] if names else None


def normalize_jpcoar(meta, header_identifier=None):
    """JPCOAR(1.0/2.0) の <jpcoar:jpcoar> 要素 → work dict。名前空間バージョンに依存しない。"""
    w = {"ids": {}, "authorships": [], "keywords": [], "grants": []}
    titles = [(t.get(XML_LANG), text(t)) for t in kids(meta, "title")]
    w["titles"] = [{"lang": l, "text": t} for l, t in titles]
    w["title"] = _pick_name([x for x in titles if x[0] != "ja-Kana"] or titles)
    w["display_name"] = w["title"]

    # 日付
    dates = {d.get("dateType"): text(d) for d in kids(meta, "date")}
    issued = dates.get("Issued") or dates.get("Created") or dates.get("Available") or ""
    w["publication_year"] = _year(issued)
    w["publication_date"] = _full_date(issued)

    # 種別・言語
    t = kid(meta, "type")
    raw_type = text(t)
    w["type_raw"] = raw_type
    w["type"] = TYPE_MAP.get(raw_type.lower(), "other") if raw_type else None
    lang = kid(meta, "language")
    w["language"] = _lang2(text(lang)) if lang is not None else None

    # 識別子・DOI
    doi = None
    ids = {}
    landing = None
    for i in kids(meta, "identifier"):
        typ, val = (i.get("identifierType") or "").upper(), text(i)
        if typ == "DOI":
            doi = doi or clean_doi(val)
        elif typ in ("HDL", "URI", "NAID", "NCID"):
            ids.setdefault(typ.lower(), val)
        if not landing and val.startswith("http"):
            landing = val
    for rel in kids(meta, "relation"):
        for ri in kids(rel, "relatedIdentifier"):
            if (ri.get("identifierType") or "").upper() == "DOI" and rel.get("relationType") == "isIdenticalTo":
                doi = doi or clean_doi(text(ri))
    if doi:
        ids["doi"] = "https://doi.org/" + doi
    if header_identifier:
        ids["oai"] = header_identifier
    w["ids"] = ids
    w["doi"] = ids.get("doi")
    w["doi_prefix"] = doi_prefix(doi) if doi else None

    # 著者
    creators = kids(meta, "creator")
    for n, c in enumerate(creators):
        names = [(x.get(XML_LANG), text(x)) for x in kids(c, "creatorName")]
        orcid = None
        for ni in kids(c, "nameIdentifier"):
            if (ni.get("nameIdentifierScheme") or "").upper() == "ORCID":
                orcid = ni.get("nameIdentifierURI") or ("https://orcid.org/" + text(ni))
        affs, rors = [], []
        for a in kids(c, "affiliation"):
            for an in kids(a, "affiliationName"):
                if text(an):
                    affs.append(text(an))
            for ni in kids(a, "nameIdentifier"):
                if (ni.get("nameIdentifierScheme") or "").upper() == "ROR":
                    rors.append(ni.get("nameIdentifierURI") or text(ni))
        pos = "first" if n == 0 else ("last" if n == len(creators) - 1 else "middle")
        w["authorships"].append({
            "author_position": pos,
            "author": {"display_name": _pick_name(names), "orcid": orcid},
            "raw_author_name": _pick_name(names),
            "raw_affiliation_strings": affs,
            "institutions": [{"ror": r} for r in rors],
        })

    # 掲載誌・書誌
    issn = []
    for si in kids(meta, "sourceIdentifier"):
        n = normalize_issn(text(si)) if (si.get("identifierType") or "").upper() in ("PISSN", "EISSN", "ISSN") else None
        if n and n not in issn:
            issn.append(n)
    st = [(x.get(XML_LANG), text(x)) for x in kids(meta, "sourceTitle")]
    source_title = _pick_name(st)
    w["issn"] = issn
    w["journal_title"] = source_title
    w["biblio"] = {
        "volume": text(kid(meta, "volume")) or None, "issue": text(kid(meta, "issue")) or None,
        "first_page": text(kid(meta, "pageStart")) or None, "last_page": text(kid(meta, "pageEnd")) or None,
    }
    pub = [(x.get(XML_LANG), text(x)) for x in kids(meta, "publisher")]
    w["publisher"] = _pick_name(pub)

    # アクセス・ファイル
    ar = kid(meta, "accessRights")
    ar_txt = text(ar).lower()
    is_oa = ar_txt == "open access" if ar_txt else None
    pdf = None
    for f in kids(meta, "file"):
        u = kid(f, "URI")
        if u is not None and (u.get("objectType") or "") == "fulltext" and "pdf" in text(kid(f, "mimeType")).lower():
            pdf = text(u)
    ver = text(kid(meta, "version"))
    w["primary_location"] = {
        "source": {"display_name": source_title, "issn": issn, "type": "repository"},
        "landing_page_url": landing, "pdf_url": pdf if is_oa else None, "version": ver or None,
    }
    w["open_access"] = {"is_oa": is_oa, "access_right": ar_txt or None}

    # キーワード・助成
    for s in kids(meta, "subject"):
        if text(s):
            w["keywords"].append({"keyword": text(s), "lang": s.get(XML_LANG)})
    for fr in kids(meta, "fundingReference"):
        w["grants"].append({"funder_display_name": text(kid(fr, "funderName")) or None,
                            "award_id": text(kid(fr, "awardNumber")) or None})

    # 抄録は出力しない（有無のみ）
    w["has_abstract_in_source"] = any(
        (d.get("descriptionType") or "").lower() == "abstract" for d in kids(meta, "description")
    ) or kid(meta, "abstract") is not None
    w["journal_key"] = journal_key(issn)
    return w


# ---- JaLC (REST v2 JSON) -------------------------------------------------
# 構造は、2026-10-10 に実データ（J-STAGE 10.11162 の20件）で確認したもの。

def _lang_texts(x, keys=("title", "text", "name", "value", "content")):
    """[{lang, <key>: text}] / {lang: text} / [str] → [(lang, text)]（キー名の揺れに備える）。"""
    out = []
    if x is None:
        return out
    if isinstance(x, str):
        return [(None, x)]
    items = x.items() if isinstance(x, dict) else enumerate(x)
    for k, it in items:
        if isinstance(it, str):
            out.append((k if isinstance(x, dict) else None, it))
        elif isinstance(it, dict):
            t = next((it[kk] for kk in keys if isinstance(it.get(kk), str) and it[kk]), None)
            if t:
                out.append((it.get("lang") or (k if isinstance(x, dict) else None), t))
    return out


def clean_text(t):
    """表示用テキスト: HTMLタグを除去し、文字参照（&quot; 等）を戻し、空白を整える。
    生データ（raw）は別に保存されているので、ここでは元に影響しない。"""
    if not isinstance(t, str):
        return t
    t = re.sub(r"<[^>]+>", "", t)
    t = _html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def _guess_lang(t):
    """lang が無い題名の言語を文字種で推定（かな・漢字を含めば ja、なければ en）。"""
    import re
    return "ja" if re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", t or "") else "en"


_PLACEHOLDER = re.compile(r"^[\[\(]?\s*[-‐ー—–・?？]*\s*[\]\)]?$")


def _is_placeholder(t):
    """「[ -- ]」のような、実質空の値（NDLの著者名など）。"""
    return (not t) or bool(_PLACEHOLDER.match(t.strip()))


def _jalc_titles(rec):
    """title_list → [(lang, 題名, 副題)]。lang が無ければ文字種から推定。"""
    out = []
    for it in rec.get("title_list") or []:
        if not isinstance(it, dict):
            continue
        t = clean_text(it.get("title"))
        if not t:
            continue
        st = clean_text(it.get("subtitle")) or None
        out.append((it.get("lang") or _guess_lang(t), t, st))
    return out


def _jalc_date(rec):
    """publication_date は {publication_year, publication_month, publication_day}（月・日は無いことがある）。"""
    pd = rec.get("publication_date")
    if isinstance(pd, dict):
        y, m, d = pd.get("publication_year"), pd.get("publication_month"), pd.get("publication_day")
        if str(y) in ("9999", "0", "0000"):   # NDL等の「年不明」
            return rec.get("date") or ""
        if y and m and d:
            return "%04d-%02d-%02d" % (int(y), int(m), int(d))
        if y and m:
            return "%04d-%02d" % (int(y), int(m))
        if y:
            return "%04d" % int(y)
    elif isinstance(pd, str):
        return pd
    return rec.get("date") or ""


def _jalc_person_name(n):
    """names[] の1件 {lang, last_name, first_name}。日本語は「姓 名」、それ以外は「名 姓」。"""
    last, first = (n.get("last_name") or "").strip(), (n.get("first_name") or "").strip()
    if not (last or first):
        return n.get("name") or None
    if n.get("lang") == "ja":
        return (last + " " + first).strip()
    return (first + " " + last).strip()


def normalize_jalc(rec):
    """JaLC REST v2 の data 部（dict）→ work dict。"""
    w = {"ids": {}, "authorships": [], "keywords": [], "grants": []}
    doi = clean_doi(rec.get("doi"))
    tl = _jalc_titles(rec)
    w["titles"] = [{"lang": l, "text": t, **({"subtitle": st} if st else {})} for l, t, st in tl]
    full = lambda t, st: (t + ": " + st) if st else t
    ja = [x for x in tl if x[0] == "ja"] or tl
    en = [x for x in tl if x[0] == "en"] or tl
    w["title"] = full(ja[0][1], ja[0][2]) if ja else None            # 原題（日本語優先）
    w["display_name"] = full(en[0][1], en[0][2]) if en else None      # JST対応表: 英語優先
    pd = _jalc_date(rec)
    w["publication_year"] = _year(pd)
    w["publication_date"] = _full_date(pd)
    w["language"] = _lang2(rec.get("content_language"))
    ct = rec.get("content_type")
    w["type_raw"] = "/".join(x for x in (ct, rec.get("article_type")) if x)
    w["resource_type"], w["resource_kind"] = rec.get("resource_type"), rec.get("resource_kind")
    w["type"] = {"JA": "article", "BK": "book", "RD": "dataset", "EL": "other", "GD": "other"}.get(ct)
    if rec.get("siteId") == "SI/JST.preprint":        # Jxiv（プレプリント）
        w["type"] = "preprint"
    w["content_type"] = ct
    w["ra"] = rec.get("ra")
    if doi:
        w["ids"]["doi"] = "https://doi.org/" + doi
    w["doi"] = w["ids"].get("doi")
    w["doi_prefix"] = rec.get("prefix") or (doi_prefix(doi) if doi else None)

    creators = rec.get("creator_list") or []
    for n, c in enumerate(creators):
        if not isinstance(c, dict):
            continue
        names = [(x.get("lang"), _jalc_person_name(x)) for x in (c.get("names") or []) if isinstance(x, dict)]
        names = [(l or _guess_lang(t), t) for l, t in names if t and not _is_placeholder(t)]
        if not names and c.get("names"):   # 「[ -- ]」だけの著者は出力しない
            continue
        if not names:  # 団体名など
            names = _lang_texts(c.get("names") or c.get("name"), keys=("name", "organization_name", "text"))
        en_ = [x for x in names if x[0] == "en"] or names
        affs = []
        for a in c.get("affiliation_list") or []:
            for an in (a.get("affiliation_name_list") or []) if isinstance(a, dict) else []:
                t = an.get("affiliation_name") if isinstance(an, dict) else None
                if t and t not in affs:
                    affs.append(t)
        pos = "first" if n == 0 else ("last" if n == len(creators) - 1 else "middle")
        orcid = next((r.get("id_code") for r in (c.get("researcher_id_list") or [])
                      if isinstance(r, dict) and (r.get("type") or "").lower() == "orcid"), None)
        w["authorships"].append({
            "author_position": pos,
            "author": {"display_name": en_[0][1] if en_ else None, "orcid": orcid},
            "names": [{"lang": l, "text": t} for l, t in names],
            "raw_author_name": en_[0][1] if en_ else None,
            "raw_affiliation_strings": affs,
            "institutions": [],
        })

    printed, other, jid = [], [], None
    for it in rec.get("journal_id_list") or []:
        if not isinstance(it, dict):
            continue
        val, typ, ity = it.get("journal_id"), (it.get("type") or "").upper(), (it.get("issn_type") or "").lower()
        n = normalize_issn(val) if (val and typ != "JID") else None
        if n:
            (printed if ity == "print" else other).append(n)
        elif val and (typ == "JID" or jid is None):
            jid = val
    issn = list(dict.fromkeys(printed + other))
    names_j = [it for it in (rec.get("journal_title_name_list") or []) if isinstance(it, dict)]
    full = [it for it in names_j if (it.get("type") or "full") == "full"] or names_j
    jt_ja = [it["journal_title_name"] for it in full if it.get("lang") == "ja" and it.get("journal_title_name")]
    jt_any = [it["journal_title_name"] for it in full if it.get("journal_title_name")]
    w["issn"] = issn
    w["jid"] = jid
    w["journal_title"] = (jt_ja or jt_any or [None])[0]
    w["journal_title_en"] = next((it["journal_title_name"] for it in full if it.get("lang") == "en"), None)
    is_ja = ct == "JA"  # 対応表: volume/issue/page は JA のみ
    w["biblio"] = {"volume": (rec.get("volume") or None) if is_ja else None,
                   "issue": (rec.get("issue") or None) if is_ja else None,
                   "first_page": (rec.get("first_page") or None) if is_ja else None,
                   "last_page": (rec.get("last_page") or None) if is_ja else None}
    pubs = _lang_texts(rec.get("publisher_list"), keys=("publisher_name", "name", "text"))
    pe = [t for l, t in pubs if l == "en"] or [t for _, t in pubs]
    w["publisher"] = pe[0] if pe else None

    pdf = None
    for r in rec.get("relation_list") or []:
        if isinstance(r, dict) and r.get("relation") == "fullTextPdf":
            pdf = r.get("content")
    if is_ja:
        src = {"display_name": w["journal_title_en"] or w["journal_title"], "issn": w["issn"], "type": "journal"}
    else:   # データ・プレプリント・図書: 公開先リポジトリを source とする
        repos = _lang_texts(rec.get("repository_list"), keys=("repository_name",))
        rn = [t for l, t in repos if l == "en"] or [t for _, t in repos]
        src = {"display_name": rn[0] if rn else rec.get("site_name"), "issn": [], "type": "repository"}
    w["primary_location"] = {"source": src, "landing_page_url": rec.get("url"), "pdf_url": pdf, "version": None}
    rights = [r for r in (rec.get("rights_list") or []) if isinstance(r, dict)]
    w["license"] = ({"rights": rights[0].get("rights"), "uri": rights[0].get("uri")} if rights else None)
    cc = bool(w["license"] and "creativecommons.org" in (w["license"].get("uri") or w["license"].get("rights") or ""))
    w["open_access"] = {"is_oa": True if cc else None, "access_right": None,
                        "basis": "license" if cc else None}
    for k in rec.get("keyword_list") or []:
        if isinstance(k, dict) and k.get("keyword"):
            w["keywords"].append({"keyword": clean_text(k["keyword"]), "lang": k.get("lang")})
    w["subjects"] = [{"subject": clean_text(k["subject"]), "lang": k.get("lang")}
                     for k in (rec.get("subject_list") or []) if isinstance(k, dict) and k.get("subject")]
    for f in rec.get("fund_list") or []:
        if not isinstance(f, dict):
            continue
        fn = _lang_texts(f.get("funder_name"), keys=("funder_name",))
        en_f = [t for l, t in fn if l == "en"] or [t for _, t in fn]
        extra = {k: v for k, v in f.items() if k != "funder_name"}
        w["grants"].append({"funder_display_name": clean_text(en_f[0]) if en_f else None,
                            "funder_names": [{"lang": l, "text": clean_text(t)} for l, t in fn],
                            **({"other": extra} if extra else {})})
    w["dates"] = {d["type"]: d["date"] for d in (rec.get("date_list") or [])
                  if isinstance(d, dict) and d.get("type") and d.get("date")}
    w["relations"] = [{"relation": r.get("relation"), "type": r.get("type"), "id": r.get("content")}
                      for r in (rec.get("relation_list") or [])
                      if isinstance(r, dict) and r.get("relation") != "fullTextPdf"]
    w["ids"]["alternate"] = [{"type": a.get("type"), "id": a.get("alternate_identifier")}
                             for a in (rec.get("alternate_identifier_list") or []) if isinstance(a, dict)]
    w["contributors"] = [{"contributor_type": c.get("contributor_type"),
                          "names": [{"lang": x.get("lang"), "text": _jalc_person_name(x)} for x in (c.get("names") or []) if isinstance(x, dict)]}
                         for c in (rec.get("contributor_list") or []) if isinstance(c, dict)]
    ed = rec.get("edition") if isinstance(rec.get("edition"), dict) else {}
    w["version"] = ed.get("version")
    w["book_classification"] = rec.get("book_classification")
    cites = [c for c in (rec.get("citation_list") or []) if isinstance(c, dict)]
    w["references_count"] = len(cites)
    w["referenced_dois"] = [d for d in (clean_doi(c.get("doi")) for c in cites) if d]
    # 抄録は出力しない（範囲外）。有無だけ記録する
    w["has_abstract_in_source"] = any(isinstance(d, dict) and (d.get("type") or "").lower() == "abstract" and d.get("description")
                                      for d in (rec.get("description_list") or []))
    w["site_id"] = rec.get("siteId")
    w["journal_key"] = journal_key(w["issn"], jid, rec.get("siteId"))
    return w
