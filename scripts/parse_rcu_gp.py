#!/usr/bin/env python3
"""
解析 RCU trace，按 GP 输出：
  1. gpnum + 对应的 seq (gpnum >> 2)
  2. 该 GP 需要上报 QS 的 CPU 列表（rcu_grace_period_init level=0 的 mask）
  3. 各 CPU 上报 QS 的时间，区分：
     - natural : 通过 cpuqs 事件（CPU 主动进入静默态）
     - FORCED  : 仅通过 rcu_quiescent_state_report（被 fqs 强制推进 —— stall 嫌疑）
     - MISSING : trace 中完全无记录

用法：
  python3 parse_rcu_gp.py trace.txt
  python3 parse_rcu_gp.py --top 20 trace.txt               # 最慢的 20 个 GP
  python3 parse_rcu_gp.py --slow 30 trace.txt              # 只看 dur >= 30ms 的 GP
  python3 parse_rcu_gp.py --top 20 --slow 10 trace.txt     # dur>=10ms 中最慢的 20 个
  cat trace.txt | python3 parse_rcu_gp.py
"""
import re
import sys
import argparse

TRACE_RE = re.compile(
    r'^\s*\S+\s+\[(\d+)\]\s+\S+\s+(\d+\.\d+):\s+(\w+):\s+(.*)$'
)
# rcu_quiescent_state_report payload: rcu_sched <gpnum> <reported_mask>>new_qsmask> ...
QS_REPORT_RE = re.compile(r'([0-9a-f]+)>([0-9a-f]+)')


class GP:
    def __init__(self, gpnum):
        self.gpnum = gpnum
        self.seq = gpnum >> 2          # 真序号 = gpnum 右移 2 位
        self.start_ts = None
        self.end_ts = None
        self.cpu_mask = None           # 根节点 mask（hex 字符串）
        self.cpuqs = {}                # cpu -> ts（自然报 QS）
        self.qs_report = {}            # cpu -> ts（report 事件，可能被 fqs 推进）

    @property
    def complete(self):
        return self.start_ts is not None and self.end_ts is not None

    @property
    def dur_ms(self):
        return (self.end_ts - self.start_ts) * 1000 if self.complete else None

    @property
    def cpus(self):
        return mask_to_cpus(self.cpu_mask) if self.cpu_mask else []

    def qs_for(self, cpu):
        """返回 (ts, kind): kind ∈ {'natural', 'FORCED', 'MISSING'}"""
        if cpu in self.cpuqs:
            return self.cpuqs[cpu], 'natural'
        if cpu in self.qs_report:
            return self.qs_report[cpu], 'FORCED'
        return None, 'MISSING'


def mask_to_cpus(mask_str):
    m = int(mask_str, 16)
    return [i for i in range(64) if m & (1 << i)]


def parse(stream):
    """读 trace，返回按 gpnum 排序的 GP 列表"""
    gps = {}
    for line in stream:
        m = TRACE_RE.match(line)
        if not m:
            continue
        trace_cpu, ts_str, event, payload = m.groups()
        ts = float(ts_str)
        trace_cpu = int(trace_cpu)
        parts = payload.split()
        if not parts or parts[0] != 'rcu_sched':
            continue

        # rcu_grace_period: rcu_sched <gpnum> <action>
        if event == 'rcu_grace_period' and len(parts) >= 3:
            gpnum = int(parts[1]); action = parts[2]
            gp = gps.setdefault(gpnum, GP(gpnum))
            if action == 'start':
                gp.start_ts = ts
            elif action == 'end':
                gp.end_ts = ts
            elif action == 'cpuqs':
                gp.cpuqs.setdefault(trace_cpu, ts)

        # rcu_grace_period_init: rcu_sched <gpnum> <level> <grplo> <grphi> <mask>
        elif event == 'rcu_grace_period_init' and len(parts) >= 6:
            gpnum = int(parts[1]); level = parts[2]; mask = parts[5]
            gp = gps.setdefault(gpnum, GP(gpnum))
            if level == '0' and gp.cpu_mask is None:   # 只取根节点
                gp.cpu_mask = mask

        # rcu_quiescent_state_report: rcu_sched <gpnum> <reported_mask>>new_qsmask> ...
        # reported_mask = 这次 report 涉及到的 CPU 位掩码（叶子节点是单个 CPU，
        # 根节点的 fqs 推进可能一次覆盖多个 CPU）
        elif event == 'rcu_quiescent_state_report' and len(parts) >= 3:
            gpnum = int(parts[1])
            mt = QS_REPORT_RE.match(parts[2])
            if mt:
                reported_mask = int(mt.group(1), 16)
                gp = gps.setdefault(gpnum, GP(gpnum))
                for cpu in range(64):
                    if reported_mask & (1 << cpu):
                        gp.qs_report.setdefault(cpu, ts)

    return [gps[k] for k in sorted(gps.keys())]


def filter_and_sort(gps, slow_ms, sort_by, top_n):
    rows = [g for g in gps if g.complete]
    if slow_ms > 0:
        rows = [g for g in rows if g.dur_ms >= slow_ms]
    if sort_by == 'dur' or top_n > 0:
        rows.sort(key=lambda g: -g.dur_ms)   # top N 蕴含按 dur 降序
    # sort_by == 'seq' 已是默认顺序
    if top_n > 0:
        rows = rows[:top_n]
    return rows


def print_gp(gp):
    cpus_s = ','.join(map(str, gp.cpus)) or '?'
    print(f"{gp.seq:>5} {gp.gpnum:>6} {gp.cpu_mask or '?':>6}  "
          f"{cpus_s:<22} {gp.dur_ms:>9.2f}")
    for cpu in gp.cpus:
        ts, kind = gp.qs_for(cpu)
        if ts is None:
            print(f"      CPU {cpu:>2}: MISSING (no cpuqs, no report)")
        else:
            d = (ts - gp.start_ts) * 1000
            tag = '' if kind == 'natural' else f"  {kind}"
            print(f"      CPU {cpu:>2}: QS at {ts:.6f} (+{d:>7.2f} ms){tag}")
    print()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('file', nargs='?', help='trace 文件，缺省 stdin')
    ap.add_argument('--slow', type=float, default=0,
                    help='只显示 dur >= SLOW ms 的 GP（默认 0 = 不过滤）')
    ap.add_argument('--top', type=int, default=0,
                    help='只显示耗时最长的 TOP 个 GP（默认 0 = 不限制）')
    ap.add_argument('--sort', choices=['seq', 'dur'], default='seq',
                    help='排序：seq=按序号（默认），dur=按耗时降序')
    args = ap.parse_args()

    stream = open(args.file) if args.file else sys.stdin

    gps = parse(stream)
    rows = filter_and_sort(gps, args.slow, args.sort, args.top)

    hdr = f"{'seq':>5} {'gpnum':>6} {'mask':>6}  {'cpus':<22} {'dur(ms)':>9}"
    print(hdr); print('-' * len(hdr))
    for gp in rows:
        print_gp(gp)

    total_complete = len([g for g in gps if g.complete])
    suffix = f" (filtered from {total_complete})" if len(rows) != total_complete else ""
    print(f"total: {len(rows)} GPs{suffix}")


if __name__ == '__main__':
    main()
