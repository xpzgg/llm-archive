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
        self.hotplug = {}              # cpu -> (first_seen_ts, delay_ms) hotplug 嫌疑

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

    @property
    def max_hotplug_delay_ms(self):
        """这个 GP 中受 hotplug 影响最大的 CPU 的延迟（ms），无则 0"""
        if not self.hotplug:
            return 0
        return max(d for _, d in self.hotplug.values())


def mask_to_cpus(mask_str):
    m = int(mask_str, 16)
    return [i for i in range(64) if m & (1 << i)]


def parse(stream):
    """读 trace，返回 (gps, cpu_first_seen, cpuonl_events)"""
    gps = {}
    cpu_first_seen = {}     # cpu -> ts (该 CPU 第一次出现在 trace 的时刻)
    cpuonl_events = []      # [(ts, gpnum, on_cpu)]

    for line in stream:
        m = TRACE_RE.match(line)
        if not m:
            continue
        trace_cpu, ts_str, event, payload = m.groups()
        ts = float(ts_str)
        trace_cpu = int(trace_cpu)

        # 记录每个 CPU 第一次出现的时刻（任何 trace 行都算）
        if trace_cpu not in cpu_first_seen:
            cpu_first_seen[trace_cpu] = ts

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
            elif action == 'cpuonl':
                cpuonl_events.append((ts, gpnum, trace_cpu))

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

    # 关联 hotplug：对每个 GP，如果 mask 中的某 CPU 第一次出现在 GP 期间
    # （start 之后、end 之前 + 容差），就认为该 GP 受 hotplug 影响
    TOLERANCE_S = 0.001   # 1ms 容差，避免边界 trace 错位
    for gp in gps.values():
        if not gp.complete:
            continue
        for cpu in gp.cpus:
            fs = cpu_first_seen.get(cpu)
            if fs is None:
                continue
            if gp.start_ts <= fs <= gp.end_ts + TOLERANCE_S:
                delay_ms = (fs - gp.start_ts) * 1000
                gp.hotplug[cpu] = (fs, delay_ms)

    return [gps[k] for k in sorted(gps.keys())], cpu_first_seen, cpuonl_events


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
    hp_tag = f"  HOTPLUG(+{gp.max_hotplug_delay_ms:.1f}ms)" if gp.hotplug else ""
    print(f"{gp.seq:>5} {gp.gpnum:>6} {gp.cpu_mask or '?':>6}  "
          f"{cpus_s:<22} {gp.dur_ms:>9.2f}{hp_tag}")
    for cpu in gp.cpus:
        ts, kind = gp.qs_for(cpu)
        if ts is None:
            print(f"      CPU {cpu:>2}: MISSING (no cpuqs, no report)")
        else:
            d = (ts - gp.start_ts) * 1000
            tag = '' if kind == 'natural' else f"  {kind}"
            if cpu in gp.hotplug:
                tag += f"  HOTPLUG(first_seen +{gp.hotplug[cpu][1]:.1f}ms)"
            print(f"      CPU {cpu:>2}: QS at {ts:.6f} (+{d:>7.2f} ms){tag}")
    print()


def print_hotplug_summary(gps, cpu_first_seen, cpuonl_events):
    """汇总 hotplug 影响的统计"""
    complete = [g for g in gps if g.complete]
    affected = [g for g in complete if g.hotplug]

    print("=" * 60)
    print("Hotplug 影响汇总")
    print("=" * 60)
    print(f"完整 GP 总数           : {len(complete)}")
    print(f"受 hotplug 影响的 GP    : {len(affected)} "
          f"({100*len(affected)/max(len(complete),1):.1f}%)")
    print()

    # 每个 CPU 的 bringup 耗时（从第一次 cpuonl 到该 CPU 第一次出现）
    # 这里用粗略关联：按 cpuonl 顺序对应新出现的 CPU
    if cpuonl_events and cpu_first_seen:
        print("CPU bringup 耗时估算（cpuonl → 该 CPU 首次出现）:")
        # 每个 cpuonl 之后到下一个新 CPU 首次出现的时间
        seen_cpus = set()
        # 先看 trace 起始前已存在的 CPU（在 cpuonl 之前就出现）
        for cpu in sorted(cpu_first_seen.keys()):
            if cpu_first_seen[cpu] < (cpuonl_events[0][0] if cpuonl_events else float('inf')):
                seen_cpus.add(cpu)

        for i, (ts, gpnum, on_cpu) in enumerate(cpuonl_events):
            # 找该 cpuonl 之后第一个新出现的 CPU
            for cpu in sorted(cpu_first_seen.keys()):
                if cpu in seen_cpus:
                    continue
                fs = cpu_first_seen[cpu]
                if fs >= ts:
                    delay_ms = (fs - ts) * 1000
                    print(f"  cpuonl at {ts:.6f} (gp {gpnum>>2}, by CPU {on_cpu}) "
                          f"→ likely CPU {cpu} bringup: {delay_ms:.1f} ms")
                    seen_cpus.add(cpu)
                    break
        print()

    # 受影响 GP 的明细
    if affected:
        print("受 hotplug 影响的 GP 明细（按 hotplug 延迟降序）:")
        affected.sort(key=lambda g: -g.max_hotplug_delay_ms)
        for g in affected:
            hp_cpus = ','.join(f"CPU {c}(+{d:.1f}ms)"
                               for c, (_, d) in g.hotplug.items())
            print(f"  seq {g.seq:>5} gpnum {g.gpnum:>5} dur {g.dur_ms:>6.2f}ms  "
                  f"hotplug: {hp_cpus}")
        print()

    # 慢 GP 的成因拆解
    if complete:
        slow_threshold = 30  # ms
        slow = [g for g in complete if g.dur_ms >= slow_threshold]
        slow_hotplug = [g for g in slow if g.hotplug]
        slow_other = [g for g in slow if not g.hotplug]
        print(f"慢 GP (dur >= {slow_threshold}ms) 成因拆解:")
        print(f"  总数               : {len(slow)}")
        print(f"  其中 hotplug 导致  : {len(slow_hotplug)} "
              f"({100*len(slow_hotplug)/max(len(slow),1):.1f}%)")
        print(f"  其中其他原因       : {len(slow_other)} "
              f"({100*len(slow_other)/max(len(slow),1):.1f}%)")
        if slow_other:
            print(f"  非 hotplug 慢 GP 列表:")
            for g in slow_other:
                # 找出最慢的 CPU
                max_cpu = None; max_d = 0; max_kind = ''
                for cpu in g.cpus:
                    ts, kind = g.qs_for(cpu)
                    if ts is None: continue
                    d = (ts - g.start_ts) * 1000
                    if d > max_d:
                        max_d = d; max_cpu = cpu; max_kind = kind
                print(f"    seq {g.seq:>5} dur {g.dur_ms:>6.2f}ms  "
                      f"slowest: CPU {max_cpu} (+{max_d:.1f}ms, {max_kind})")


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
    ap.add_argument('--hotplug', action='store_true',
                    help='只输出 hotplug 影响汇总，不打印每个 GP 明细')
    args = ap.parse_args()

    stream = open(args.file) if args.file else sys.stdin

    gps, cpu_first_seen, cpuonl_events = parse(stream)

    if args.hotplug:
        print_hotplug_summary(gps, cpu_first_seen, cpuonl_events)
        return

    rows = filter_and_sort(gps, args.slow, args.sort, args.top)

    hdr = f"{'seq':>5} {'gpnum':>6} {'mask':>6}  {'cpus':<22} {'dur(ms)':>9}"
    print(hdr); print('-' * len(hdr))
    for gp in rows:
        print_gp(gp)

    total_complete = len([g for g in gps if g.complete])
    suffix = f" (filtered from {total_complete})" if len(rows) != total_complete else ""
    print(f"total: {len(rows)} GPs{suffix}")

    # 即使不 --hotplug，也在末尾打一个简短汇总
    affected = [g for g in gps if g.complete and g.hotplug]
    if affected:
        print(f"\n[hotplug affected: {len(affected)}/{total_complete} GPs, "
              f"run with --hotplug for details]")


if __name__ == '__main__':
    main()
