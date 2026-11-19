"""Regression tests for lightweight gRPC package imports."""
import os
import subprocess
import sys
import unittest


class GrpcPackageImportTests(unittest.TestCase):
    def test_importing_authentication_submodule_does_not_load_server_graph(self):
        script = (
            "import sys; "
            "import video_streaming.grpc.authentication; "
            "assert \"video_streaming.grpc.server\" not in sys.modules; "
            "assert \"video_streaming.grpc.servicers\" not in sys.modules"
        )
        environment = os.environ.copy()
        subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            cwd=os.getcwd(),
            env=environment,
            capture_output=True,
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
