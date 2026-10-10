"""生データ保存。ローカルフォルダ、またはS3互換（Wasabi等）。"""
import os


class LocalStore:
    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _p(self, key):
        p = os.path.normpath(os.path.join(self.root, key))
        if not p.startswith(os.path.normpath(self.root)):
            raise ValueError("bad key: %s" % key)
        return p

    def put(self, key, data):
        p = self._p(key)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, p)  # 書きかけファイルを残さない

    def get(self, key):
        with open(self._p(key), "rb") as f:
            return f.read()

    def exists(self, key):
        return os.path.exists(self._p(key))

    def list(self, prefix):
        base = self._p(prefix) if prefix else self.root
        out = []
        for d, _, files in os.walk(base):
            for fn in files:
                if fn.endswith(".tmp"):
                    continue
                out.append(os.path.relpath(os.path.join(d, fn), self.root).replace(os.sep, "/"))
        return sorted(out)


class S3Store:
    """S3 API互換ストレージ（Wasabi等）。boto3 が必要（pip install boto3）。"""

    def __init__(self, bucket, endpoint_url=None, prefix="", region=None,
                 access_key=None, secret_key=None):
        import boto3  # 遅延import
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.c = boto3.client(
            "s3", endpoint_url=endpoint_url, region_name=region,
            aws_access_key_id=access_key, aws_secret_access_key=secret_key)

    def _k(self, key):
        return (self.prefix + "/" + key) if self.prefix else key

    def put(self, key, data):
        self.c.put_object(Bucket=self.bucket, Key=self._k(key), Body=data)

    def get(self, key):
        return self.c.get_object(Bucket=self.bucket, Key=self._k(key))["Body"].read()

    def exists(self, key):
        try:
            self.c.head_object(Bucket=self.bucket, Key=self._k(key))
            return True
        except Exception:
            return False

    def list(self, prefix):
        out = []
        pg = self.c.get_paginator("list_objects_v2")
        full = self._k(prefix)
        for page in pg.paginate(Bucket=self.bucket, Prefix=full):
            for o in page.get("Contents", []):
                k = o["Key"]
                if self.prefix:
                    k = k[len(self.prefix) + 1:]
                out.append(k)
        return sorted(out)


def make_store(cfg):
    t = cfg.get("type", "local")
    if t == "local":
        return LocalStore(cfg.get("path", "data/store"))
    if t == "s3":
        return S3Store(cfg["bucket"], cfg.get("endpoint_url"), cfg.get("prefix", ""),
                       cfg.get("region"), cfg.get("access_key"), cfg.get("secret_key"))
    raise ValueError("unknown store type: %s" % t)
