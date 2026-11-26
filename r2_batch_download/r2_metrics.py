"""Read bucket-level operation metrics from Cloudflare's GraphQL API."""
from collections import Counter
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
import json
import re
import ssl
from urllib.error import HTTPError
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener

from botocore.httpsession import get_cert_path

_QUERY = """
query BucketOperations($account: string!, $bucket: string!, $start: Time, $end: Time) {
  viewer {
    accounts(filter: {accountTag: $account}) {
      r2OperationsAdaptiveGroups(limit: 10000, filter: {
        bucketName: $bucket, datetime_geq: $start, datetime_leq: $end
      }) {
        sum { requests }
        dimensions { actionType }
      }
    }
  }
}
"""
_CLASS_A = frozenset("""listbuckets putbucket listobjects listobjectsv2 putobject copyobject completeMultipartUpload
    createMultipartUpload lifecycleStorageTierTransition listMultipartUploads uploadPart uploadPartCopy
    listParts putBucketEncryption putBucketCors putBucketLifecycleConfiguration""".lower().split())
_CLASS_B = frozenset("""headBucket headObject getObject usageSummary getBucketEncryption getBucketLocation
    getBucketCors getBucketLifecycleConfiguration""".lower().split())


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def query_bucket_operations(account: str, bucket: str, token: str) -> dict[str, int]:
    if not re.fullmatch(r"[a-f0-9]{32}", account, re.IGNORECASE) or not re.fullmatch(r"[A-Za-z0-9_-]{20,256}", token):
        raise ValueError("请填写 32 位 Cloudflare Account ID 和有效的账户 API Token。")
    now = datetime.now(timezone.utc)
    variables = {"account": account, "bucket": bucket, "start": (now - timedelta(days=7)).isoformat(),
                 "end": now.isoformat()}
    request = Request(
        "https://api.cloudflare.com/client/v4/graphql",
        data=json.dumps({"query": _QUERY, "variables": variables}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Accept": "application/json", "User-Agent": "S3FileManager"},
    )
    try:
        context = ssl.create_default_context()
        context.load_verify_locations(get_cert_path(True))
        opener = build_opener(NoRedirect(), HTTPSHandler(context=context))
        with opener.open(request, timeout=20) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("Cloudflare 指标响应超过大小限制。")
        data = json.loads(raw)
    except HTTPError as exc:
        raise ValueError(f"Cloudflare 返回 HTTP {exc.code}。请检查账户和 Analytics Read 权限。") from None
    except (OSError, HTTPException, ValueError, RecursionError):
        raise ValueError("无法读取 Cloudflare 指标，请检查网络后重试。") from None
    try:
        groups = data["data"]["viewer"]["accounts"][0]["r2OperationsAdaptiveGroups"]
        if data.get("errors") or not isinstance(groups, list):
            raise ValueError
        counts: Counter[str] = Counter({"A": 0, "B": 0, "other": 0})
        for group in groups:
            action = re.sub(r"[^a-z0-9]", "", group["dimensions"]["actionType"].lower())
            count = group["sum"]["requests"]
            if type(count) is not int or count < 0:
                raise ValueError
            category = "A" if action in _CLASS_A else "B" if action in _CLASS_B else "other"
            counts[category] += count
        return dict(counts)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError):
        raise ValueError("Cloudflare 未返回可用指标。请检查账户、桶名称和 Analytics Read 权限。") from None
