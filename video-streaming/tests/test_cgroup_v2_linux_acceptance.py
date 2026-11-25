"""Real Linux cgroup v2 acceptance tests; run as root on a cgroup v2 host."""

import os
import select
import subprocess
import sys
import time
import unittest
import uuid
from contextlib import contextmanager

from video_streaming.execution import attempt_scope
from video_streaming.execution.cgroup_v2 import AttemptCgroupV2
from video_streaming.execution.process_control import run_streaming_process


CGROUP_ROOT = "/sys/fs/cgroup"
REQUIRED_CONTROLLERS = ("cpu", "memory")


def _read_words(path):
    with open(path, "r") as stream:
        return set(stream.read().split())


def _read_cgroup_values(path):
    values = {}
    with open(path, "r") as stream:
        for line in stream:
            key, value = line.split(None, 1)
            values[key] = int(value.strip())
    return values


def _enable_controllers(path):
    available = _read_words(os.path.join(path, "cgroup.controllers"))
    missing = set(REQUIRED_CONTROLLERS) - available
    if missing:
        raise AssertionError(
            "cgroup controllers unavailable at {}: {}".format(
                path, sorted(missing)
            )
        )
    control = os.path.join(path, "cgroup.subtree_control")
    enabled = _read_words(control)
    disabled = set(REQUIRED_CONTROLLERS) - enabled
    if disabled:
        with open(control, "w") as stream:
            stream.write(" ".join("+" + item for item in sorted(disabled)))
    enabled = _read_words(control)
    if set(REQUIRED_CONTROLLERS) - enabled:
        raise AssertionError(
            "failed to enable CPU/memory controllers at {}".format(path)
        )


@unittest.skipUnless(
    os.geteuid() == 0, "requires root and a delegated Linux cgroup v2 hierarchy"
)
class LinuxCgroupV2AcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.geteuid() != 0:
            raise RuntimeError("cgroup v2 acceptance must run as root")
        if not (
                os.path.isfile(os.path.join(CGROUP_ROOT, "cgroup.controllers"))
                and os.path.isfile(os.path.join(CGROUP_ROOT, "cgroup.subtree_control"))):
            raise RuntimeError("/sys/fs/cgroup is not a writable cgroup v2 root")
        _enable_controllers(CGROUP_ROOT)
        cls.delegated_root = os.path.join(
            CGROUP_ROOT, "video-service-acceptance-" + uuid.uuid4().hex
        )
        os.mkdir(cls.delegated_root)
        try:
            _enable_controllers(cls.delegated_root)
        except BaseException:
            os.rmdir(cls.delegated_root)
            raise

    @classmethod
    def tearDownClass(cls):
        root = getattr(cls, "delegated_root", None)
        if not root or not os.path.isdir(root):
            return
        for name in os.listdir(root):
            child = os.path.join(root, name)
            if not os.path.isdir(child):
                continue
            kill_path = os.path.join(child, "cgroup.kill")
            if os.path.exists(kill_path):
                with open(kill_path, "w") as stream:
                    stream.write("1")
            events_path = os.path.join(child, "cgroup.events")
            deadline = time.monotonic() + 5
            while os.path.exists(events_path) and time.monotonic() < deadline:
                if _read_cgroup_values(events_path).get("populated") == 0:
                    break
                time.sleep(0.05)
            os.rmdir(child)
        os.rmdir(root)

    @contextmanager
    def attempt(self, memory_max=96 * 1024 * 1024, cpu_max="20000 100000"):
        cgroup = AttemptCgroupV2(
            delegated_root=self.delegated_root,
            cpu_max=cpu_max,
            memory_max=memory_max,
            attempt_id=uuid.uuid4().hex,
        )
        try:
            swap_max = os.path.join(cgroup.path, "memory.swap.max")
            if os.path.exists(swap_max):
                with open(swap_max, "w") as stream:
                    stream.write("0")
            yield cgroup
        finally:
            if not cgroup.wait_empty(0.2):
                cgroup.terminate()
                if not cgroup.wait_empty(5):
                    raise AssertionError("attempt cgroup did not become empty")
            cgroup.cleanup(timeout_seconds=5)

    def test_real_process_supervisor_is_cpu_throttled_inside_attempt_cgroup(self):
        with self.attempt(cpu_max="10000 100000") as cgroup:
            lines = []
            token = attempt_scope._CURRENT_ATTEMPT_CGROUP.set(cgroup)
            try:
                run_streaming_process(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import time\n"
                            "end=time.monotonic()+1.2\n"
                            "n=0\n"
                            "while time.monotonic()<end:\n"
                            " n+=1\n"
                            "print(n)\n"
                        ),
                    ],
                    timeout_seconds=5,
                    poll_interval=0.05,
                    on_line=lambda line, _process: lines.append(line),
                )
                usage = cgroup.usage()
            finally:
                attempt_scope._CURRENT_ATTEMPT_CGROUP.reset(token)

            self.assertTrue(lines)
            self.assertGreater(usage["cpu"].get("nr_periods", 0), 0)
            self.assertGreater(usage["cpu"].get("nr_throttled", 0), 0)

    def test_memory_max_causes_a_kernel_oom_kill_inside_the_attempt(self):
        with self.attempt(memory_max=96 * 1024 * 1024) as cgroup:
            process = cgroup.spawn(
                [
                    sys.executable,
                    "-c",
                    (
                        "data=bytearray(256*1024*1024); "
                        "[(data.__setitem__(i, 1)) for i in range(0, len(data), 4096)]; "
                        "print(len(data), flush=True)"
                    ),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            _stdout, _stderr = process.communicate(timeout=15)
            usage = cgroup.usage()
            self.assertNotEqual(process.returncode, 0)
            self.assertGreater(usage["memory_events"].get("oom_kill", 0), 0)

    def test_cgroup_kill_terminates_detached_descendants_and_allows_cleanup(self):
        with self.attempt() as cgroup:
            child_code = "import time; time.sleep(60)"
            parent_code = (
                "import subprocess,sys,time; "
                "child=subprocess.Popen([sys.executable,'-c', {!r}], "
                "start_new_session=True); "
                "print(child.pid, flush=True); time.sleep(60)"
            ).format(child_code)
            process = cgroup.spawn(
                [sys.executable, "-c", parent_code],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            ready, _writeable, _errors = select.select(
                [process.stdout], [], [], 5
            )
            self.assertTrue(ready, "parent did not report its detached child")
            child_pid = int(process.stdout.readline().decode("ascii").strip())
            self.assertGreater(child_pid, 0)

            cgroup.terminate()
            self.assertTrue(cgroup.wait_empty(5))
            process.wait(timeout=5)
            events = _read_cgroup_values(
                os.path.join(cgroup.path, "cgroup.events")
            )
            self.assertEqual(events.get("populated"), 0)
            process.stdout.close()
            process.stderr.close()


if __name__ == "__main__":
    unittest.main()
