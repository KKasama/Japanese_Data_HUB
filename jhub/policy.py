"""抄録（abstract）の出力ポリシー。

抄録の本文は、正規化器が内部項目 _abstracts として取り出す。それを出力するかどうかは、
ここで供給元ごとに決める。設定（config の "abstracts"）を変えて `ingest --reprocess`
を実行すれば、生データから作り直して反映される（再収集は不要）。

モード:
  off             出力しない（有無だけ has_abstract_in_source に残る）
  permitted_only  抄録の第三者利用が「可」と分かるものだけ出力する（JaLC検索画面形式の third_party_use）
  include         出力する

既定: irdb=include / jalc=off（JSTから「抄録なしの書誌情報のみ」と連絡を受けているため。2026-10時点）

abstract_status:
  included             出力した
  withheld_by_policy   原データにはあるが、ポリシーにより出力していない
  not_permitted        permitted_only で、第三者利用が「可」と確認できないため出力していない
  none                 原データに抄録がない
"""

MODES = ("off", "permitted_only", "include")
DEFAULT_POLICY = {"irdb": "include", "jalc": "off"}


def resolve_policy(cfg_abstracts=None):
    p = dict(DEFAULT_POLICY)
    for k, v in (cfg_abstracts or {}).items():
        if k.startswith("_"):
            continue
        if v not in MODES:
            raise ValueError("abstracts.%s は %s のいずれかにしてください: %r" % (k, "/".join(MODES), v))
        p[k] = v
    return p


def _permitted(w):
    v = w.get("third_party_use")
    return isinstance(v, str) and v.strip() in ("可", "OK", "true", "True", "permitted")


def apply_abstract_policy(w, source, policy=None):
    """w から内部項目 _abstracts を取り除き、ポリシーに従って w["abstracts"] を付ける。"""
    absts = w.pop("_abstracts", None) or []
    mode = (policy or DEFAULT_POLICY).get(source, "off")
    if not absts:
        w["abstract_status"] = "none"
        return w
    if mode == "include" or (mode == "permitted_only" and _permitted(w)):
        w["abstracts"] = absts
        w["abstract_status"] = "included"
    elif mode == "permitted_only":
        w["abstract_status"] = "not_permitted"
    else:
        w["abstract_status"] = "withheld_by_policy"
    return w
