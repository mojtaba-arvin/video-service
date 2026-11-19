# Synthetic VOD regression corpus v1

The corpus is generated on demand using FFmpeg lavfi sources. It contains no
downloaded, copyrighted, or customer media. Generated binaries live in the
caller-selected output directory and are never committed to Git.

## Generate

~~~sh
python video-streaming/tests/fixtures/synthetic-vod-v1/generate.py \
  --output-dir /tmp/synthetic-vod-v1
~~~

Requires FFmpeg and FFprobe on PATH.

| Fixture | Video | Audio | Purpose |
|---|---|---|---|
| landscape-silent-24fps | 640×360, 24 fps, 6 s | None | Landscape and silent stream handling |
| portrait-audio-30fps | 360×640, 30 fps, 5 s | AAC, 997 Hz tone | Portrait and audio/video mux handling |
| low-resolution-color-15fps | 320×180, 15 fps, 2 s | None | Small frame and low frame-rate edge cases |

The versioned manifest contains exact source expressions and expected probe
metadata. The generated dataset-manifest.json records FFmpeg build identity,
FFprobe metadata, output sizes, and SHA-256 hashes. Hashes identify generated
artifacts; byte-for-byte identity across FFmpeg builds is not promised.

This corpus is appropriate for correctness, parser, manifest, and smoke tests.
It is intentionally too small and synthetic to establish perceptual quality,
real-world performance, production capacity, or cloud cost savings. Such
claims require a representative corpus with documented redistribution rights.


## Package validation harness

The package harness exercises the existing fail-closed HLS/DASH validator against all three generated fixtures. It creates two video renditions per fixture, generates HLS master/variant playlists and a static DASH MPD, validates every referenced playlist/representation/segment, and writes a machine-readable report with manifest hashes and per-file package inventory. Generated media remains outside the repository.

Run from the repository root:

~~~sh
python video-streaming/tests/fixtures/synthetic-vod-v1/generate.py --output-dir /tmp/synthetic-vod-v1
PYTHONPATH=video-streaming python video-streaming/tests/fixtures/synthetic-vod-v1/package_harness.py --input-dir /tmp/synthetic-vod-v1 --output-dir /tmp/synthetic-vod-packages
~~~

The regression test named test_synthetic_vod_package_harness.py runs the generator and harness in temporary directories. It asserts that all three fixtures produce valid two-rendition HLS and DASH packages with non-empty referenced segments and hashed manifests. This is structural/package-completeness validation; it does not claim perceptual quality or player interoperability.
