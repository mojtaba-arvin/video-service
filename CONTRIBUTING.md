# Contributing

## Change policy

- Work on a focused branch; do not push feature work directly to `develop`.
- Keep each pull request to one independently reviewable behavior or vertical increment.
- A behavior change must include regression tests that fail before the fix and pass after it.
- Pull requests targeting `develop` must have all required CI checks green before merge.
- Do not merge while checks are pending, failing, or unavailable. A green check is evidence for the commands it actually ran, not for untested runtime or production behavior.
- Record material architecture or compatibility decisions in a short ADR under `docs/decisions/`.
- Do not claim performance, cost, or quality improvements without a reproducible benchmark and its workload definition.

## Running the regression tests

The initial test suite uses Python's standard-library `unittest` runner and the same `python-decouple` version pinned by the service:

```sh
# Install FFmpeg and FFprobe using your platform package manager first.
python -m pip install "python-decouple==3.4" "ffmpeg-python==0.2.0"
PYTHONPATH=video-streaming python -m unittest discover -s video-streaming/tests -v
```

GitHub Actions runs this command for pull requests targeting `develop` and for pushes to `develop`.

The CI job now validates configuration, environment-file tracking rules, and thumbnail rendering/output validation through real FFmpeg and FFprobe executions. It still does not validate Celery orchestration, Redis, RabbitMQ, S3 publication, container builds, gRPC authentication, webhook security, or a complete VOD job.
