import datetime as _dt
import re

XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


def utc_now(now=None):
    """OAI-PMHの粒度(秒)に揃えたUTC時刻文字列。now は文字列またはdatetimeで上書き可（テスト用）。"""
    if isinstance(now, str):
        return now
    d = now or _dt.datetime.now(_dt.timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


_SETSPEC_BAD = re.compile(r"[^A-Za-z0-9\-_.!~*'()]")


def slug(s):
    """OAI-PMH setSpecPart に使える文字だけにする。"""
    return _SETSPEC_BAD.sub("_", str(s))


def ln(el):
    """名前空間を除いたタグ名。"""
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def kids(el, name):
    return [c for c in el if ln(c) == name]


def kid(el, name):
    for c in el:
        if ln(c) == name:
            return c
    return None


def text(el):
    return (el.text or "").strip() if el is not None else ""
