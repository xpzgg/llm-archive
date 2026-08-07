// SPDX-License-Identifier: GPL-2.0
/*
 * Stress PoC for a suspected shmem accounting invariant break:
 *
 *   MADV_SOFT_OFFLINE can invalidate a clean shared-anonymous shmem folio
 *   through mapping_evict_folio(), then VMA replacement drops the last shmem
 *   file reference and shmem_evict_inode() observes stale inode->i_blocks.
 *
 * Run as root/CAP_SYS_ADMIN inside the target VM:
 *
 *   ./shmem_soft_offline_evict_poc -p 4 -r 4 -n 0
 */

#define _GNU_SOURCE

#include <errno.h>
#include <getopt.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/types.h>
#include <unistd.h>

#ifndef MADV_SOFT_OFFLINE
#define MADV_SOFT_OFFLINE 101
#endif

#define DEFAULT_ADDR ((void *)0x200000000000UL)
#define DEFAULT_LEN  (64UL * 1024)
#define DEFAULT_ADVISERS 4
#define DEFAULT_REPLACERS 4

struct options {
	void *addr;
	size_t len;
	unsigned long iterations;
	int advisers;
	int replacers;
	unsigned int delay_us;
};

struct counters {
	unsigned long madvise_ok;
	unsigned long madvise_fail;
	unsigned long shared_maps;
	unsigned long private_maps;
	unsigned long map_fail;
};

struct thread_arg {
	const struct options *opts;
	struct counters *counters;
	int id;
};

static volatile sig_atomic_t stop;

static void on_signal(int sig)
{
	(void)sig;
	stop = 1;
}

static void count_inc(unsigned long *counter)
{
	__atomic_add_fetch(counter, 1, __ATOMIC_RELAXED);
}

static unsigned long count_read(unsigned long *counter)
{
	return __atomic_load_n(counter, __ATOMIC_RELAXED);
}

static unsigned long parse_ulong(const char *s, const char *name)
{
	char *end;
	unsigned long val;

	errno = 0;
	val = strtoul(s, &end, 0);
	if (errno || *end) {
		fprintf(stderr, "invalid %s: %s\n", name, s);
		exit(2);
	}
	return val;
}

static void usage(const char *prog)
{
	fprintf(stderr,
		"usage: %s [-p advisers] [-r replacers] [-n iterations] [-a addr] [-l len] [-d usec]\n"
		"\n"
		"  -p advisers    MADV_SOFT_OFFLINE threads, default %d\n"
		"  -r replacers   MAP_FIXED replacement threads, default %d\n"
		"  -n iterations  madvise iterations per adviser, 0 means forever\n"
		"  -a addr        fixed mmap address, default %p\n"
		"  -l len         mapping length, default %lu\n"
		"  -d usec        delay after each madvise, default 0\n",
		prog, DEFAULT_ADVISERS, DEFAULT_REPLACERS, DEFAULT_ADDR,
		DEFAULT_LEN);
}

static void pin_thread(int id)
{
	long ncpu = sysconf(_SC_NPROCESSORS_ONLN);
	cpu_set_t set;

	if (ncpu <= 0)
		return;

	CPU_ZERO(&set);
	CPU_SET(id % ncpu, &set);
	(void)sched_setaffinity(0, sizeof(set), &set);
}

static void *map_shared_shmem(void *addr, size_t len)
{
	void *p;

	/*
	 * MAP_SHARED | MAP_ANONYMOUS uses shmem_zero_setup().  We do not write
	 * into the mapping: MADV_SOFT_OFFLINE will fault pages as needed, and
	 * clean folios are the interesting invalidation target.
	 */
	p = mmap(addr, len, PROT_READ | PROT_WRITE,
		 MAP_SHARED | MAP_ANONYMOUS | MAP_FIXED, -1, 0);
	if (p == MAP_FAILED || p != addr)
		return MAP_FAILED;
	return p;
}

static void *map_private_none(void *addr, size_t len)
{
	void *p;

	/*
	 * Replacing the shared mapping with a private PROT_NONE VMA drops the
	 * old shmem file reference without creating new user pages for
	 * MADV_SOFT_OFFLINE to poison.
	 */
	p = mmap(addr, len, PROT_NONE,
		 MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED, -1, 0);
	if (p == MAP_FAILED || p != addr)
		return MAP_FAILED;
	return p;
}

static void *adviser_thread(void *data)
{
	struct thread_arg *arg = data;
	const struct options *opts = arg->opts;
	struct counters *counters = arg->counters;
	unsigned long i;

	pin_thread(arg->id);

	for (i = 0; !stop && (opts->iterations == 0 || i < opts->iterations);
	     i++) {
		if (madvise(opts->addr, opts->len, MADV_SOFT_OFFLINE) == 0) {
			count_inc(&counters->madvise_ok);
		} else {
			count_inc(&counters->madvise_fail);
			if (errno == EPERM) {
				fprintf(stderr,
					"adviser %d: MADV_SOFT_OFFLINE needs CAP_SYS_ADMIN\n",
					arg->id);
				stop = 1;
				break;
			}
		}

		if (opts->delay_us)
			usleep(opts->delay_us);

		if ((i & 0x3ff) == 0) {
			fprintf(stderr,
				"adviser %d iter=%lu madvise_ok=%lu fail=%lu shared_maps=%lu private_maps=%lu map_fail=%lu\n",
				arg->id, i,
				count_read(&counters->madvise_ok),
				count_read(&counters->madvise_fail),
				count_read(&counters->shared_maps),
				count_read(&counters->private_maps),
				count_read(&counters->map_fail));
		}
	}

	return NULL;
}

static void *replacer_thread(void *data)
{
	struct thread_arg *arg = data;
	const struct options *opts = arg->opts;
	struct counters *counters = arg->counters;
	unsigned long i = 0;

	pin_thread(arg->id + opts->advisers);

	while (!stop) {
		if (map_shared_shmem(opts->addr, opts->len) == MAP_FAILED)
			count_inc(&counters->map_fail);
		else
			count_inc(&counters->shared_maps);

		sched_yield();

		if (map_private_none(opts->addr, opts->len) == MAP_FAILED)
			count_inc(&counters->map_fail);
		else
			count_inc(&counters->private_maps);

		if ((++i & 0xfff) == 0)
			sched_yield();
	}

	return NULL;
}

static int create_threads(pthread_t *threads, struct thread_arg *args,
			  const struct options *opts, struct counters *counters)
{
	int idx = 0;
	int i;

	for (i = 0; i < opts->replacers; i++, idx++) {
		args[idx].opts = opts;
		args[idx].counters = counters;
		args[idx].id = i;
		if (pthread_create(&threads[idx], NULL, replacer_thread,
				   &args[idx])) {
			perror("pthread_create replacer");
			stop = 1;
			return idx;
		}
	}

	for (i = 0; i < opts->advisers; i++, idx++) {
		args[idx].opts = opts;
		args[idx].counters = counters;
		args[idx].id = i;
		if (pthread_create(&threads[idx], NULL, adviser_thread,
				   &args[idx])) {
			perror("pthread_create adviser");
			stop = 1;
			return idx;
		}
	}

	return idx;
}

int main(int argc, char **argv)
{
	struct options opts = {
		.addr = DEFAULT_ADDR,
		.len = DEFAULT_LEN,
		.iterations = 0,
		.advisers = DEFAULT_ADVISERS,
		.replacers = DEFAULT_REPLACERS,
		.delay_us = 0,
	};
	struct counters counters = {};
	pthread_t *threads;
	struct thread_arg *args;
	int nr_threads;
	int created;
	int opt;
	int i;

	while ((opt = getopt(argc, argv, "a:d:l:n:p:r:h")) != -1) {
		switch (opt) {
		case 'a':
			opts.addr = (void *)parse_ulong(optarg, "addr");
			break;
		case 'd':
			opts.delay_us = parse_ulong(optarg, "delay");
			break;
		case 'l':
			opts.len = parse_ulong(optarg, "len");
			break;
		case 'n':
			opts.iterations = parse_ulong(optarg, "iterations");
			break;
		case 'p':
			opts.advisers = (int)parse_ulong(optarg, "advisers");
			if (opts.advisers <= 0) {
				fprintf(stderr, "advisers must be positive\n");
				return 2;
			}
			break;
		case 'r':
			opts.replacers = (int)parse_ulong(optarg, "replacers");
			if (opts.replacers <= 0) {
				fprintf(stderr, "replacers must be positive\n");
				return 2;
			}
			break;
		case 'h':
			usage(argv[0]);
			return 0;
		default:
			usage(argv[0]);
			return 2;
		}
	}

	if (opts.len == 0 || (opts.len & (unsigned long)(getpagesize() - 1))) {
		fprintf(stderr, "len must be page-aligned and non-zero\n");
		return 2;
	}

	if (geteuid() != 0)
		fprintf(stderr,
			"warning: MADV_SOFT_OFFLINE usually requires CAP_SYS_ADMIN\n");

	signal(SIGINT, on_signal);
	signal(SIGTERM, on_signal);

	if (map_shared_shmem(opts.addr, opts.len) == MAP_FAILED) {
		perror("initial shared mmap");
		return 1;
	}

	fprintf(stderr,
		"addr=%p len=%zu advisers=%d replacers=%d iterations=%lu delay_us=%u advice=MADV_SOFT_OFFLINE\n",
		opts.addr, opts.len, opts.advisers, opts.replacers,
		opts.iterations, opts.delay_us);

	nr_threads = opts.advisers + opts.replacers;
	threads = calloc(nr_threads, sizeof(*threads));
	args = calloc(nr_threads, sizeof(*args));
	if (!threads || !args) {
		perror("calloc");
		return 1;
	}

	created = create_threads(threads, args, &opts, &counters);

	if (opts.iterations == 0) {
		while (!stop)
			sleep(1);
	} else {
		for (i = opts.replacers; i < created; i++)
			(void)pthread_join(threads[i], NULL);
		stop = 1;
	}

	for (i = 0; i < created; i++)
		(void)pthread_join(threads[i], NULL);

	(void)munmap(opts.addr, opts.len);
	fprintf(stderr,
		"done: madvise_ok=%lu fail=%lu shared_maps=%lu private_maps=%lu map_fail=%lu\n",
		count_read(&counters.madvise_ok),
		count_read(&counters.madvise_fail),
		count_read(&counters.shared_maps),
		count_read(&counters.private_maps),
		count_read(&counters.map_fail));

	free(args);
	free(threads);
	return 0;
}
