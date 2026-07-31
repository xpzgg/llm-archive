#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HULK CVE 待处理列表拉取与汇总脚本。

用法:
    export HULK_AUTH_TOKEN='你的 authorization 值'
    python3 hulk_cve_report.py [--group 15] [--size 50]

架构分三层:
    1. 拉取层 fetch_*  : 负责 HTTP 请求与分页
    2. 解析层 parse_*  : 把原始 JSON 转成扁平的 VulnRecord 列表
    3. 展示层 render_* : 只消费解析结果做打印, 展示格式变更只改这一层
"""

import argparse
import json
import os
import ssl
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

API_URL = "https://hulk.rnd.huawei.com/api/v1/vuln/wait"
ENV_AUTH = "HULK_AUTH_TOKEN"


# ---------------------------------------------------------------------------
# 数据结构(解析层输出)
# ---------------------------------------------------------------------------

@dataclass
class VulnRecord:
    vuln_id: str            # CVE 编号
    project: str            # 待合入版本名, 如 OLK-5.10
    owner: str              # 责任人
    state: str              # 当前阶段, 如 漏洞修补
    conclusion: str         # 当前阶段结论, 如 待修补
    receive_at: str = ""    # 接收时间(备用字段, 展示层可自行取舍)
    extra: dict = field(default_factory=dict)  # 预留: 后续要展示更多字段时塞这里


# ---------------------------------------------------------------------------
# 1. 拉取层
# ---------------------------------------------------------------------------

def build_request(page: int, size: int, group: int, token: str) -> urllib.request.Request:
    params = {
        "page": page,
        "size": size,
        "total": 0,
        "groups[]": group,
    }
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "authorization": token,
        "user-agent": "hulk-cve-report/1.0",
        "referer": "https://hulk.rnd.huawei.com/dashboard",
    }
    return urllib.request.Request(url, headers=headers)


def fetch_page(page: int, size: int, group: int, token: str,
               insecure: bool = False) -> dict:
    """拉取单页, 返回响应 JSON。"""
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = build_request(page, size, group, token)
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("code") != 20000:
        raise RuntimeError(f"API 返回异常: code={payload.get('code')} "
                           f"msg={payload.get('message', '')}")
    return payload["data"]


def fetch_all(group: int, size: int, token: str,
              insecure: bool = False, interval: float = 1.0) -> List[dict]:
    """按分页拉取全部原始条目。

    每页拉 size 条, 按服务端声明的 count 判断拉完为止;
    每页之间间隔 interval 秒, 避免请求过快。
    """
    items: List[dict] = []
    page = 1
    while True:
        data = fetch_page(page, size, group, token, insecure)
        batch = data.get("data") or []
        items.extend(batch)
        total = data.get("count", 0)
        if len(items) >= total or not batch:
            break
        page += 1
        time.sleep(interval)
    return items


# ---------------------------------------------------------------------------
# 2. 解析层
# ---------------------------------------------------------------------------

def parse_record(raw: dict) -> VulnRecord:
    """把单条原始 JSON 解析成 VulnRecord。字段来源变更只改这里。"""
    current = raw.get("current") or {}
    state = current.get("state") or {}
    return VulnRecord(
        vuln_id=raw.get("vuln_id", ""),
        project=(raw.get("project") or {}).get("name", "未知版本"),
        owner=(raw.get("owner") or {}).get("name", "未分配"),
        state=state.get("name", ""),
        conclusion=raw.get("conclusion_cn", ""),
        receive_at=raw.get("receive_at", ""),
    )


def parse_all(raw_items: List[dict]) -> List[VulnRecord]:
    return [parse_record(it) for it in raw_items]


def group_by_project(records: List[VulnRecord]) -> Dict[str, List[VulnRecord]]:
    """按版本分组, 组内按责任人、CVE 编号排序。"""
    grouped: Dict[str, List[VulnRecord]] = defaultdict(list)
    for rec in records:
        grouped[rec.project].append(rec)
    for recs in grouped.values():
        recs.sort(key=lambda r: (r.owner, r.vuln_id))
    return dict(sorted(grouped.items()))


def build_summary(records: List[VulnRecord]) -> Dict[str, Dict[str, int]]:
    """汇总: {版本: {责任人: 剩余数}}。"""
    summary: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for rec in records:
        summary[rec.project][rec.owner] += 1
    return {p: dict(owners) for p, owners in sorted(summary.items())}


# ---------------------------------------------------------------------------
# 3. 展示层
# ---------------------------------------------------------------------------

def display_width(text: str) -> int:
    """计算字符串的显示宽度: 中文等宽字符占 2 列, ASCII 占 1 列。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in text)


def pad(text: str, width: int) -> str:
    """按显示宽度右填充空格, 保证中文列对齐。"""
    return text + " " * max(0, width - display_width(text))


def render_projects(grouped: Dict[str, List[VulnRecord]]) -> None:
    for project, recs in grouped.items():
        print(f"\n{project} (共 {len(recs)} 个)")
        for r in recs:
            print(f"  {pad(r.vuln_id, 16)} {pad(r.owner, 8)} "
                  f"{pad(r.state, 8)} {r.conclusion}")


def render_summary(summary: Dict[str, Dict[str, int]]) -> None:
    per_person_total: Dict[str, int] = defaultdict(int)
    grand_total = 0

    print("\n[各版本剩余数]")
    for project, owners in summary.items():
        detail = ", ".join(f"{owner} {cnt}" for owner, cnt in
                           sorted(owners.items(), key=lambda kv: -kv[1]))
        subtotal = sum(owners.values())
        grand_total += subtotal
        for owner, cnt in owners.items():
            per_person_total[owner] += cnt
        print(f"  {project}: {detail} (小计 {subtotal})")

    print("\n[各责任人剩余总数]")
    for owner, cnt in sorted(per_person_total.items(), key=lambda kv: -kv[1]):
        print(f"  {owner}: {cnt}")
    print(f"  总计: {grand_total}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="HULK CVE 待处理列表汇总")
    parser.add_argument("--group", type=int, default=15, help="组 ID (默认 15)")
    parser.add_argument("--size", type=int, default=20, help="每页条数 (默认 20)")
    parser.add_argument("--insecure", action="store_true",
                        help="跳过 HTTPS 证书校验(内网自签证书时使用)")
    args = parser.parse_args()

    token = os.environ.get(ENV_AUTH, "").strip()
    if not token:
        print(f"错误: 请先设置环境变量 {ENV_AUTH}", file=sys.stderr)
        return 1

    raw_items = fetch_all(args.group, args.size, token, args.insecure)
    records = parse_all(raw_items)

    if not records:
        print("没有待处理的 CVE。")
        return 0

    render_projects(group_by_project(records))
    render_summary(build_summary(records))
    return 0


if __name__ == "__main__":
    sys.exit(main())
