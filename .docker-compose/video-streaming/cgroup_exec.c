#define _GNU_SOURCE
#include <errno.h>
#include <linux/sched.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

/*
 * Launch the target directly into an existing cgroup v2 using clone3's
 * CLONE_INTO_CGROUP. There is deliberately no fork-then-write-cgroup.procs
 * fallback: if the kernel cannot provide atomic admission, execution fails.
 */
int main(int argc, char **argv) {
    char *end = NULL;
    long cgroup_fd;
    struct clone_args args;
    pid_t child;
    int status;

    if (argc < 4 || strcmp(argv[2], "--") != 0) {
        (void)fprintf(stderr, "video-cgroup-exec: invalid arguments\n");
        return 125;
    }

    errno = 0;
    cgroup_fd = strtol(argv[1], &end, 10);
    if (errno != 0 || end == argv[1] || *end != '\0' ||
        cgroup_fd < 0 || cgroup_fd > INT32_MAX) {
        (void)fprintf(stderr, "video-cgroup-exec: invalid cgroup descriptor\n");
        return 125;
    }

    memset(&args, 0, sizeof(args));
    args.flags = CLONE_INTO_CGROUP;
    args.exit_signal = SIGCHLD;
    args.cgroup = (uint64_t)cgroup_fd;

    child = (pid_t)syscall(SYS_clone3, &args, sizeof(args));
    if (child < 0) {
        (void)fprintf(stderr, "video-cgroup-exec: atomic cgroup launch failed: %s\n",
                       strerror(errno));
        return 125;
    }

    if (child == 0) {
        (void)close((int)cgroup_fd);
        execvp(argv[3], &argv[3]);
        _exit(errno == ENOENT ? 127 : 126);
    }

    while (waitpid(child, &status, 0) < 0) {
        if (errno == EINTR) {
            continue;
        }
        (void)fprintf(stderr, "video-cgroup-exec: waitpid failed: %s\n",
                       strerror(errno));
        return 125;
    }

    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    if (WIFSIGNALED(status)) {
        return 128 + WTERMSIG(status);
    }
    return 125;
}
