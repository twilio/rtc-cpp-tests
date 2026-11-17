# rtc-cpp test results

This repository contains automated test results for Twilio Programmable Voice and Video SDKs that use rtc-cpp.

The results are generated and published automatically by Twilio's SDK test infrastructure.

## Test Results

Test results may cover multiple SDKs, platforms, versions, and test environments. Each result represents the outcome of a specific automated test run.

These results are intended to provide visibility into rtc-cpp testing and should not be interpreted as a complete statement of SDK compatibility or platform support.

For officially supported platforms and versions, refer to the documentation for the corresponding Twilio SDK.

## How the tests run

`.github/workflows/pull-tests.yml` runs rtc-cpp's voice or video test suites
from prebuilt test binaries:

- A run starts when an rtc-cpp build finishes uploading its test binaries
  (`repository_dispatch`), or by hand from the Actions tab
  (`workflow_dispatch`, which uses the newest complete build unless you give
  it a commit).
- `scripts/plan_matrix.py` fans the run out into one job per suite, platform
  (linux x86_64, macOS arm64), build type (debug, release, ASan/TSan debug) and
  shard. The network (containerized) suites run in a privileged container on
  linux only (`docker/containerized.Dockerfile`).
- `scripts/run_gtest_isolated.py` runs each test as its own process with a
  15-minute timeout, so a crash fails only that test, and lists the results at
  the end of the job. A final `summary` job lists every job and test that
  didn't pass.
- `media/` holds the audio files the tests expect next to their binaries, and
  `voice/websocket-server/` the local server the voice integration tests use
  (both copied from rtc-cpp).

The logs are public, so they show test names, statuses and counts only. Test
output is discarded on the runner.

## Issues

This repository contains generated test results and is not intended for reporting SDK issues.

For SDK support or to report an issue, use the support channels or issue tracker associated with the corresponding Twilio SDK.
