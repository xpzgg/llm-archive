#!/usr/bin/env python3
"""Source-tied state models, not execution on hardware or a kernel test."""
from pathlib import Path
from itertools import combinations
root = Path(__file__).resolve().parents[3]
src = (root/'arch/arm64/kernel/vip_smt.c').read_text()
assert 'reg = __this_cpu_read(vip_smt_##_name)' in src
assert 'write_sysreg_s(__this_cpu_read(vip_smt_ifu_actlr1), sys_ifu_actlr1)' in src
assert 'return first_cpu;' in src
# Interrupt callback is indivisible relative to the interrupted idle task.
for irq_at in range(3):
    shadow = hw = 0
    loaded = None
    for point in range(3):
        if irq_at == point:
            hw = shadow = 1  # complete successful synchronous sysfs write IPI
        if point == 0:
            loaded = shadow
        if point == 1:
            hw = loaded
    print(f'idle restore / write IPI: irq_at={irq_at}, shadow={shadow}, hardware={hw}')
    assert (shadow != hw) == (irq_at == 1)
# Shared ROB shadow is initialized on both CPUs, then only owner is updated.
shadow = {2: 0, 3: 0}
siblings = {2, 3}
hw = 0
owner = min(siblings)
hw = shadow[owner] = 1
siblings.remove(2)  # mirrors remove_cpu_topology(2)
owner = min(siblings)
# thread_id is retained by clear_cpu_topology, so vip_smt_core_has_smt is true.
hw = shadow[owner]
print(f'owner offline: new_owner={owner}, shadows={shadow}, hardware={hw}, expected=1')
assert hw == 0
# write chooses former owner, then owner is removed before generic_exec_single.
chosen = 2
online = {3}
ipi_result = 0 if chosen in online else -6  # -ENXIO
store_result = len('TSLOT_EN=1')  # return count irrespective of ipi_result
print(f'owner removal race: IPI={ipi_result}, store={store_result} (false success)')
assert ipi_result < 0 and store_result > 0
print('PASS: modeled counterexamples reproduced; no hardware execution claimed')
