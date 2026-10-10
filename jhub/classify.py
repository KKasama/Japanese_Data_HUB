"""振り分けルール（機関・ジャーナル）。

機関: IRDB レコードの header（setSpec の機関コード、または識別子中のコード）を優先。
      about の irdbProvenance は、構造を実データで確認するまで「そのまま保存」し、
      設定された要素名から機関名を補助的に拾う。
ジャーナル: ISSN（print優先）→ ジャーナルID。DOIプレフィックスは別に保持。
"""
import re

DEFAULT_ID_REGEX = r"^oai:[^:]+:(?P<code>\d+)[:/]"
ISSN_RE = re.compile(r"^\s*(\d{4})-?(\d{3}[\dXx])\s*$")


def pad_code(code):
    return str(int(code)).zfill(5)


def institution_code(identifier, setspecs, id_regex=DEFAULT_ID_REGEX):
    """(コード or None, 判定根拠, 不一致フラグ)。コードは5桁ゼロ埋めに統一。"""
    from_set = [pad_code(s) for s in setspecs if re.fullmatch(r"\d+", s or "")]
    from_id = None
    m = re.match(id_regex, identifier or "")
    if m:
        from_id = pad_code(m.group("code"))
    if len(set(from_set)) == 1:
        code = from_set[0]
        return code, "setSpec", (from_id is not None and from_id != code)
    if from_id:
        return from_id, "identifier", False
    return None, "unknown", False


def normalize_issn(s):
    m = ISSN_RE.match(s or "")
    return "%s-%s" % (m.group(1), m.group(2).upper()) if m else None


def journal_key(issns, jid=None, site=None):
    """ISSN（先頭=print想定）→ ジャーナルID の順でキーを作る。"""
    for s in issns or []:
        n = normalize_issn(s)
        if n:
            return n
    if jid:
        return "jid.%s.%s" % (site or "x", jid)
    return None


def doi_prefix(doi):
    m = re.match(r"^(?:https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9})/", (doi or "").strip(), re.I)
    return m.group(1) if m else None


def clean_doi(doi):
    d = (doi or "").strip()
    d = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", d, flags=re.I)
    return d.lower() if d.startswith("10.") else None


def provenance_institution_name(prov, name_keys=("repositoryName", "institutionName", "name")):
    for k in name_keys:
        if prov.get(k):
            return prov[k]
    return None
