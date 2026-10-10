"""OpenAlex互換の work 形式への整形（試作: 主要項目のみ）。

本番では、既存の「JaLC–OpenAlex 項目対応表（79項目）」に置き換える。
方針: 抄録(abstract)は出力しない。有無だけ has_abstract_in_source として記録する。
"""
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

_SKIP = ("lang", "language", "sequence", "type", "seq")


def _first_text(d, keys):
    for k in keys:
        if isinstance(d.get(k), str) and d[k]:
            return d[k]
    for k, v in d.items():  # 想定外のキー名でも、言語・連番以外の最初の文字列を採る
        if k not in _SKIP and isinstance(v, str) and v:
            return v
    return None


def _lang_texts(x, keys=("title", "text", "name", "keyword", "value", "content")):
    """辞書 {lang: text} / [{lang, <key>: text}] / [str] のどの形でも [(lang, text)] にする。"""
    out = []
    if x is None:
        return out
    if isinstance(x, str):
        return [(None, x)]
    if isinstance(x, dict):
        for k, v in x.items():
            if isinstance(v, str):
                out.append((k, v))
            elif isinstance(v, dict):
                t = _first_text(v, keys)
                if t:
                    out.append((k, t))
        return out
    if isinstance(x, list):
        for it in x:
            if isinstance(it, str):
                out.append((None, it))
            elif isinstance(it, dict):
                t = _first_text(it, keys)
                if t:
                    out.append((it.get("lang") or it.get("language"), t))
    return out


def _creator_name(c):
    """creator_list の1要素から氏名を取り出す（キー構造の違いに備えた防御的実装）。"""
    names = []
    for k in ("names", "name", "creator_name"):
        if k in c:
            names += _lang_texts(c[k], keys=("name", "full_name", "text"))
    if not names:
        for lang in ("ja", "en"):
            v = c.get(lang)
            if isinstance(v, dict):
                nm = v.get("name") or " ".join(x for x in (v.get("last_name"), v.get("first_name")) if x)
                if nm:
                    names.append((lang, nm))
    return names


def _find_strings(x, key_hint):
    out = []
    if isinstance(x, dict):
        for k, v in x.items():
            if key_hint in k.lower():
                out += [t for _, t in _lang_texts(v, keys=("name", "text", "affiliation"))]
            else:
                out += _find_strings(v, key_hint)
    elif isinstance(x, list):
        for it in x:
            out += _find_strings(it, key_hint)
    return out


def normalize_jalc(rec):
    """JaLC REST v2 の data 部（dict）→ work dict。"""
    w = {"ids": {}, "authorships": [], "keywords": [], "grants": []}
    doi = clean_doi(rec.get("doi"))
    titles = _lang_texts(rec.get("title_list"))
    w["titles"] = [{"lang": l, "text": t} for l, t in titles]
    ja = [x for x in titles if x[0] == "ja"] or titles
    en = [x for x in titles if x[0] == "en"] or titles
    w["title"] = ja[0][1] if ja else None            # 原題（日本語優先）
    w["display_name"] = en[0][1] if en else None      # JST対応表: 英語優先
    pd = rec.get("publication_date") or rec.get("date") or ""
    w["publication_year"] = _year(pd)
    w["publication_date"] = _full_date(pd)
    w["language"] = _lang2(rec.get("content_language"))
    ct = rec.get("content_type")
    w["type_raw"] = "%s/%s" % (ct, rec.get("article_type"))
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
        names = _creator_name(c) if isinstance(c, dict) else []
        pos = "first" if n == 0 else ("last" if n == len(creators) - 1 else "middle")
        w["authorships"].append({
            "author_position": pos,
            "author": {"display_name": _pick_name(names), "orcid": None},
            "raw_author_name": _pick_name(names),
            "raw_affiliation_strings": _find_strings(c, "affiliation") if isinstance(c, dict) else [],
            "institutions": [],
        })

    printed, other, jid = [], [], None
    for it in rec.get("journal_id_list") or []:
        if isinstance(it, dict):
            val = it.get("journal_id") or it.get("id") or it.get("value") or it.get("content")
            typ = (it.get("type") or it.get("issn_type") or "").upper()
        else:
            val, typ = it, ""
        n = normalize_issn(val) if val else None
        if n:
            (printed if "PRINT" in typ else other).append(n)
        elif val and (typ == "JID" or jid is None):
            jid = val
    issn = list(dict.fromkeys(printed + other))
    jt = _lang_texts(rec.get("journal_title_name_list"), keys=("journal_title_name", "name", "text", "title"))
    jt_ja = [t for l, t in jt if l == "ja"]
    w["issn"] = issn
    w["jid"] = jid
    w["journal_title"] = (jt_ja or [t for _, t in jt] or [None])[0]
    is_ja = ct == "JA"  # 対応表: volume/issue/page は JA のみ
    w["biblio"] = {"volume": (rec.get("volume") or None) if is_ja else None,
                   "issue": (rec.get("issue") or None) if is_ja else None,
                   "first_page": (rec.get("first_page") or None) if is_ja else None,
                   "last_page": (rec.get("last_page") or None) if is_ja else None}
    pubs = _lang_texts(rec.get("publisher_list"), keys=("publisher_name", "name", "text"))
    w["publisher"] = (pubs[0][1] if pubs else None)

    pdf = None
    for r in rec.get("relation_list") or []:
        if isinstance(r, dict) and r.get("relation") == "fullTextPdf":
            pdf = r.get("content")
    w["primary_location"] = {
        "source": {"display_name": w["journal_title"], "issn": w["issn"], "type": "journal"},
        "landing_page_url": rec.get("url"), "pdf_url": pdf, "version": None}
    w["open_access"] = {"is_oa": None, "access_right": None}
    for k in rec.get("keyword_list") or []:
        if isinstance(k, dict) and k.get("keyword"):
            w["keywords"].append({"keyword": k["keyword"], "lang": k.get("lang")})
    w["has_abstract_in_source"] = False  # JaLC REST v2 のJAには抄録項目なし（2026-10時点の確認）
    w["site_id"] = rec.get("siteId")
    w["journal_key"] = journal_key(w["issn"], jid, rec.get("siteId"))
    return w
