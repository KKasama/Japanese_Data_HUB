"""IRDBの「参加リポジトリ一覧」CSV（https://irdb.nii.ac.jp/repositorylist の CSV出力）から、機関名を台帳へ入れる。

一覧には機関コード（00937など）の列がない。そこで、ハブの記録にある「機関コード × DOIプレフィックス」の組と、
一覧の「JaLC DOI Prefix」を突き合わせて、コードに機関名を対応づける（機関コードの列がある版なら、それを使う）。
DOIプレフィックスを持たないリポジトリや、複数の機関で共有されるプレフィックスは、対応づけられない。
"""
import csv
import io
from collections import Counter, defaultdict

from .db import connect

_NAME = ("機関名", "Institution", "institution")
_REPO = ("リポジトリ名", "Repository", "repository")
_URL = ("リポジトリURL", "Repository URL", "URL")
_JALC = ("JaLC DOI Prefix", "JaLC DOI prefix", "JaLC")
_CODE = ("機関コード", "コード", "Institution Code", "code")


def _col(row, names):
    for n in names:
        for k in row:
            if k and k.strip().lstrip("﻿") == n:
                return (row[k] or "").strip()
    return ""


def _read(path):
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    raise ValueError("文字コードを判別できません（UTF-8またはShift_JISのCSVを指定してください）")


def load_repositorylist(db_path, csv_path):
    """csv_path: 1ファイル、または複数ファイル（リスト、またはカンマ区切り。一覧が500件ずつに分かれて出力される場合）。
    戻り値: {"matched": n, "unmatched": [コード…], "conflicts": [コード…], "rows": 一覧の行数}"""
    import os
    if isinstance(csv_path, str):
        csv_path = [csv_path] if os.path.exists(csv_path) else [x.strip() for x in csv_path.split(",") if x.strip()]
    rows = []
    for pth in csv_path:
        rows += list(csv.DictReader(io.StringIO(_read(os.path.expanduser(pth)))))
    by_prefix, by_code = defaultdict(list), {}
    for r in rows:
        name = _col(r, _NAME)
        if not name:
            continue
        ent = {"name": name, "repo": _col(r, _REPO), "url": _col(r, _URL)}
        for p in _col(r, _JALC).replace("、", ",").replace(";", ",").split(","):
            if p.strip():
                by_prefix[p.strip()].append(ent)
        code = _col(r, _CODE)
        if code:
            by_code[code.zfill(5)] = ent
    c = connect(db_path)
    pairs = defaultdict(Counter)
    for r in c.execute("SELECT institution_code, doi_prefix, COUNT(*) n FROM records WHERE institution_code IS NOT NULL "
                       "AND doi_prefix IS NOT NULL GROUP BY 1,2"):
        pairs[r[0]][r[1]] += r[2]
    codes = {r[0] for r in c.execute("SELECT DISTINCT institution_code FROM records WHERE institution_code IS NOT NULL")}
    st = {"matched": 0, "unmatched": [], "conflicts": [], "rows": len(rows)}
    for code in sorted(codes):
        ent = by_code.get(code)
        if ent is None:
            names = Counter()
            for pfx, n in pairs.get(code, {}).items():
                ents = by_prefix.get(pfx, [])
                if len({e["name"] for e in ents}) == 1:   # 同じ機関の複数リポジトリが同じプレフィックスを持つ場合は、1機関とみなす
                    names[(ents[0]["name"], ents[0]["url"])] += n
            if not names:
                st["unmatched"].append(code)
                continue
            if len(names) > 1:
                st["conflicts"].append(code)
            (name, url), _n = names.most_common(1)[0]
            ent = {"name": name, "url": url}
        c.execute("INSERT INTO institutions(code,name_ja,url) VALUES(?,?,?) ON CONFLICT(code) DO UPDATE SET "
                  "name_ja=excluded.name_ja, url=COALESCE(NULLIF(excluded.url,''), institutions.url)",
                  (code, ent["name"], ent.get("url")))
        st["matched"] += 1
    c.commit()
    c.close()
    return st
