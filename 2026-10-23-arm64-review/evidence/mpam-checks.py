#!/usr/bin/env python3
"""Fixed-source assertions + deterministic semantic witnesses, not kernel/HW tests."""
from pathlib import Path
import subprocess
root = Path(__file__).resolve().parents[3]
assert subprocess.check_output(['git','rev-parse','HEAD'], cwd=root, text=True).strip() == '00ba9aa3d7f58091cb87f8da96599d6d72023ea0'
dev=(root/'drivers/platform/mpam/mpam_devices.c').read_text()
res=(root/'drivers/platform/mpam/mpam_resctrl.c').read_text()
asm=(root/'arch/arm64/include/asm/mpam.h').read_text()
dbg=(root/'fs/debugfs/file.c').read_text()
def body(s, name):
    start=s.index(name+'(')
    start=s.index('{', start)
    depth=1
    end=start+1
    while depth:
        depth += (s[end]=='{') - (s[end]=='}')
        end+=1
    return s[start:end]
assert '!regval || !mpam_get_regval_partid(regval)' in asm
for req, pmg, parent in [(8,0,0),(0,1,0)]:
    regval=(req<<16)|(req<<48)|pmg|(pmg<<32)
    cpu_default=0
    actual=cpu_default if not regval or parent==0 else regval
    assert actual != regval
    print(f'ROOT_MON_TAG_LOSS req={req}, pmg={pmg}: stored={regval:#x}, effective={actual:#x}')
assert 'debugfs_remove' not in body(dev,'mpam_class_destroy')
assert 'kfree(class)' in body(dev,'mpam_class_destroy')
assert 'debugfs_create_cpumask("affinity", 0400, d, &class->affinity)' in dev
print('CLASS_DEBUGFS_UAF: class freed; class debugfs files not removed')
b=body(dbg,'debugfs_read_file_cpumask')
assert 'goto kfree;' in b and b.index('debugfs_file_put') < b.index('kfree:')
active=1
active+=1 # debugfs_file_get
# kmalloc failure skips put
active-=1 # remove drops baseline reference
assert active==1
print('DEBUGFS_OOM_REF_LEAK: active_users after remove =',active)
b=body(dev,'mpam_disable_thread')
assert b.index('mpam_resctrl_exit') < b.index('static_branch_disable')
assert 'reqpartid_exit();' in body(res,'mpam_resctrl_exit')
assert 'kfree(reqpartid_map)' in body(res,'reqpartid_exit')
print('DISABLE_UAF_WINDOW: reqpartid_map freed before scheduler static key disabled')
# A legal varying capability example for the driver's per-RIS probing design.
req_max=255
limits=[15,63]
actual=0
for limit in limits:
    actual=min(req_max,limit)
assert actual==63 and min(limits)==15
print('RIS_LIMIT_OVERWRITE: limits',limits,'reported',actual,'safe',min(limits))
assert 'msc->intpartid_max = min(msc->partid_max, partid_max)' in dev
b=body(dev,'mpam_reprogram_msc')
assert 'partid < resctrl_arch_get_num_closid(NULL)' in b
assert 'get_num_reqpartid' not in b
print('HOTPLUG_RESTORE_GAP: only intPARTID range traversed (platform constraints require hardware validation)')
print('PASS: source assertions and semantic witnesses; no kernel execution claimed')
