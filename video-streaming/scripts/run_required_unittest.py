"""Run a required unittest discovery lane with strict discovery/result checks.

This file intentionally has a guarded main entry point: tests that use the
multiprocessing "spawn" context need an importable __main__ file. Do not replace
this runner invocation with a stdin/heredoc runner.
"""
import argparse
import unittest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("start_directory")
    parser.add_argument("pattern")
    parser.add_argument("expected_count", type=int)
    args = parser.parse_args()

    loader = unittest.TestLoader()
    suite = loader.discover(
        args.start_directory,
        pattern=args.pattern,
    )
    if loader.errors:
        raise SystemExit(
            "Required unittest discovery failed:\n" + "\n".join(loader.errors)
        )

    discovered = suite.countTestCases()
    if discovered != args.expected_count:
        raise SystemExit(
            "Expected {} tests for {}; discovered {}".format(
                args.expected_count, args.pattern, discovered
            )
        )

    result = unittest.TextTestRunner(verbosity=2).run(suite)
    skipped = len(result.skipped)
    print(
        "Required tests: discovered={}, executed={}, skipped={}, failures={}, "
        "errors={}".format(
            discovered,
            result.testsRun,
            skipped,
            len(result.failures),
            len(result.errors),
        )
    )
    if (
        result.testsRun != discovered
        or skipped
        or result.failures
        or result.errors
    ):
        raise SystemExit("Required unittest acceptance failed")


if __name__ == "__main__":
    main()
