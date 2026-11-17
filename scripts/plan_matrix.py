#!/usr/bin/env python3
"""Build the test job matrix for one rtc-cpp project (voice or video).

Prints one JSON object {"include": [...]} for
`strategy.matrix: ${{ fromJSON(...) }}` (any other top-level key would become
a matrix dimension). Every value in it is fixed by the
tables below (nothing from the dispatch payload except the validated project
name and which sanitizer builds it carries), so it's safe in the public log.

The tables mirror the CircleCI jobs rtc-cpp ran against prod
(rtc-cpp .circleci/config_impl.yml on master: the build-test-publish-voice,
build-test-publish-video-prod and test-<project>-<config>-prod workflows), with
two rules from VBLOCKS-7638 applied on top:
  - containerized suites run on linux only;
  - every other suite runs on both darwin and linux (CircleCI ran voice queue
    on darwin only), in both debug and release.
Shard counts are CircleCI's `parallelism` for the same job. The *-all-tests
executables are in the tarballs, but CircleCI never ran them, so neither do we.
"""

import json
import os
import sys

PLATFORMS = [
    # (platform, arch, runner)
    ("linux", "x86_64", "ubuntu-x64"),
    ("darwin", "arm64", "macos-arm"),
]
BUILD_TYPES = ("debug", "release")

# suite -> (executable, shards)
SUITES = {
    "voice": {
        "unit": ("voice-unit-tests", 1),
        "queue": ("voice-queue-tests", 1),
        "integration": ("voice-integration-tests", 4),
    },
    "video": {
        "unit": ("video-unit-tests", 4),
        "main-thread": ("video-main-thread-tests", 2),
        "integration": ("video-integration-tests", 8),
        # CircleCI ran net-tests (project `net`) inside the video workflows,
        # and the binary ships in the video tarballs. Its jobs are named
        # net-tests-<platform>-<arch>-<build> (see JOB_PREFIX), not video-net-*.
        "net": ("net-tests", 1),
    },
}
CONTAINERIZED_SHARDS = {"voice": 4, "video": 4}
# Job name prefix for suites that aren't really the project's own.
JOB_PREFIX = {("video", "net"): "net-tests"}

# CircleCI's voice sanitizer exclude list. CircleCI wrote it as
# "-*CallSetupTime*:..." and then prefixed "*:-", so the first pattern became
# "--*CallSetupTime*" and never matched: CallSetupTime tests weren't actually
# excluded. Fixed here (VBLOCKS-7638: run what CircleCI meant).
VOICE_SANITIZER_EXCLUDE = "*CallSetupTime*:*CallTearDown*:*ValidateSpecialToneDelay*"
# Voice ASan containerized: an exclude list too (CircleCI: FILTER="*:-<filter>").
VOICE_ASAN_CONTAINERIZED_EXCLUDE = (
    "*CallConnection*:*CallReconnection*:*MediaReconnectionDelayParams*:*RTCWarning*:*MosStats*")

# Sanitizer jobs CircleCI ran against prod, all linux x86_64 debug:
# sanitizer -> [(suite, shards, exclude)]. Video TSan only ran against Staging
# (or on demand), so it's left out even though rtc-cpp uploads a video-tsan
# tarball. Voice TSan integration/containerized were excluded in CircleCI
# (CLIENT-8051), which rtc-cpp's build_sanitizer_tests.sh mirrors.
SANITIZERS = {
    "voice": {
        "asan": [("unit", 1, VOICE_SANITIZER_EXCLUDE),
                 ("integration", 4, VOICE_SANITIZER_EXCLUDE),
                 ("containerized", 4, VOICE_ASAN_CONTAINERIZED_EXCLUDE)],
        "tsan": [("unit", 1, VOICE_SANITIZER_EXCLUDE)],
    },
    "video": {
        "asan": [("unit", 4, ""), ("integration", 8, ""), ("containerized", 4, "")],
    },
}
SKIPPED_SANITIZERS = {"video": ["tsan"]}


def leg(project, platform, arch, runner, variant, suite, exe, shards, shard, exclude="",
        containerized=False, sanitizer=""):
    prefix = JOB_PREFIX.get((project, suite), f"{project}-{suite}")
    name = f"{prefix}-{platform}-{arch}-{variant}"
    if shards > 1:
        name += f"-{shard + 1}of{shards}"
    return {
        "name": name, "project": project, "platform": platform, "arch": arch, "runner": runner,
        "variant": variant, "sanitizer": sanitizer, "suite": suite, "exe": exe,
        "shard": shard, "shards": shards, "exclude": exclude,
        "containerized": containerized,
    }


def plan(project, sanitizers_available):
    legs = []
    for platform, arch, runner in PLATFORMS:
        for bt in BUILD_TYPES:
            for suite, (exe, shards) in SUITES[project].items():
                legs += [leg(project, platform, arch, runner, bt, suite, exe, shards, s)
                         for s in range(shards)]
            if platform == "linux":
                shards = CONTAINERIZED_SHARDS[project]
                legs += [leg(project, platform, arch, runner, bt, "containerized",
                             f"{project}-containerized-tests", shards, s, containerized=True)
                         for s in range(shards)]
    platform, arch, runner = PLATFORMS[0]  # sanitizer builds are linux x86_64 only
    for san, suites in SANITIZERS[project].items():
        if san not in sanitizers_available:
            print(f"::warning::debug-{san}: no {project} {san} build for this run, skipping its tests",
                  file=sys.stderr)
            continue
        for suite, shards, exclude in suites:
            legs += [leg(project, platform, arch, runner, f"debug-{san}", suite,
                         f"{project}-{suite}-tests", shards, s, exclude=exclude,
                         containerized=suite == "containerized", sanitizer=san)
                     for s in range(shards)]
    for san in SKIPPED_SANITIZERS.get(project, []):
        print(f"::notice::debug-{san}: CircleCI never ran {project} {san} against prod, not run here",
              file=sys.stderr)
    return legs


def main():
    event_name = os.environ["GITHUB_EVENT_NAME"]
    with open(os.environ["GITHUB_EVENT_PATH"]) as f:
        event = json.load(f)
    all_sanitizers = {s for p in SANITIZERS.values() for s in p}
    if event_name == "repository_dispatch":
        payload = event.get("client_payload") or {}
        project = payload.get("project")
        artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), list) else []
        # Only fixed names leave this set, so nothing from the payload reaches the log.
        available = {a.get("sanitizer") for a in artifacts if isinstance(a, dict)} & all_sanitizers
    elif event_name == "workflow_dispatch":
        project = (event.get("inputs") or {}).get("project") or "voice"
        # Not known without S3 access; each sanitizer leg checks for its own
        # tarball and reports "skipped" if this build didn't upload one.
        available = all_sanitizers
    else:
        print(f"::error::unexpected event {event_name}", file=sys.stderr)
        sys.exit(1)
    if project not in SUITES:
        print("::error::missing or invalid 'project'", file=sys.stderr)
        sys.exit(1)
    legs = plan(project, available)
    print(f"{project}: {len(legs)} test jobs", file=sys.stderr)
    print(json.dumps({"include": legs}, separators=(",", ":")))


if __name__ == "__main__":
    main()
