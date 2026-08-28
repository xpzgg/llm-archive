#!/usr/bin/env python3
"""将已确认的 CVE 分析提交到 HULK，供 agent 调用。"""

import argparse
import json
import sys
import urllib.request
from pathlib import Path


BASE_URL = "https://hulk.rnd.huawei.com/api/v1/vuln"
AUTHORIZATION = "2D9072F19CCF790A61ACB57A0C0C53A0"


def request_json(url: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode() if body else None
    headers = {
        "accept": "application/json, text/plain, */*",
        "authorization": AUTHORIZATION,
    }
    if data:
        headers["content-type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)

    if result.get("code") != 20000:
        raise RuntimeError(f"API 返回失败: {result}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="提交 HULK CVE 分析")
    parser.add_argument("cve", help="例如 CVE-2026-74310")
    parser.add_argument("analysis_file", type=Path, help="UTF-8 分析文件")
    args = parser.parse_args()

    description = args.analysis_file.read_text(encoding="utf-8").strip()
    if not description:
        raise ValueError("分析内容不能为空")

    detail = request_json(f"{BASE_URL}/detail/{args.cve.upper()}")
    vulnerability_id = detail["data"]["id"]

    result = request_json(
        f"{BASE_URL}/comment/",
        method="POST",
        body={
            "vulnerability": vulnerability_id,
            "description": description,
            "parent": None,
        },
    )
    print(json.dumps({
        "ok": True,
        "cve": args.cve.upper(),
        "vulnerability_id": vulnerability_id,
        "response": result.get("data"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False),
              file=sys.stderr)
        sys.exit(1)
