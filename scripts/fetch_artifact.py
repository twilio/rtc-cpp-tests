#!/usr/bin/env python3
"""Resolve and download one rtc-cpp test tarball from S3, then extract it.

Each test job needs one build (PLATFORM/ARCH/VARIANT, VARIANT being debug,
release, debug-asan or debug-tsan) and downloads just that tarball into
tests/<VARIANT>/. Tarballs are rooted at tests/, so the executables land in
tests/<VARIANT>/tests/.

repository_dispatch: the artifact comes from the payload and is downloaded by
exact {key, version_id}, so an S3 overwrite between dispatch and download
can't feed us different bytes than the dispatcher signed off on.
workflow_dispatch: keys come from list-object-versions (the bucket is
versioned). With no `sha` input, the newest build with every platform and both
build types first uploaded at least CUTOFF_MARGIN before CUTOFF (set once by
the plan job) is used. Each key is judged by its oldest version, so a re-upload
of the same commit during a run (a Buildkite rebuild, say) can't make the
build look incomplete to the jobs that list after it. Each job then downloads
the newest version uploaded by that time, so all jobs of a run, including
re-runs, get the same bytes. The margin covers uploads still in flight at
CUTOFF: S3 stamps LastModified when an upload starts but lists the object only
once it completes, so without it a job listing early could miss a version that
a later job sees.

A sanitizer build that's missing (its rtc-cpp build failed or didn't upload)
isn't an error: "available=false" goes to $GITHUB_OUTPUT and the job skips its
tests. Any other missing build fails the job.

Logging policy: this repo is public, so its Actions logs are too, while rtc-cpp
is private. Only project/platform/variant and counts are printed. rtc-cpp SHAs,
S3 keys, version ids, extracted file names, and aws/tar error output are never
printed in Actions, not even with debug logging on: turning that on needs
write access, but the log it produces is as public as any other. To see them,
run this script locally (outside Actions) with FETCH_DEBUG=1 and the same
PLATFORM/ARCH/VARIANT/CUTOFF and event file. Nothing sensitive goes to
$GITHUB_OUTPUT either (GitHub prints `env:` values that use step outputs).
"""

import datetime
import json
import os
import re
import subprocess
import sys
import traceback

# Local runs only: GITHUB_ACTIONS is always "true" on a runner.
debug = os.environ.get("FETCH_DEBUG") == "1" and os.environ.get("GITHUB_ACTIONS") != "true"
DETAILS_HINT = "details withheld from the public log; reproduce locally with FETCH_DEBUG=1"


def fail(msg, detail=None):
    # `msg` goes to the public log and must not contain SHAs, keys, version ids
    # or payload values; `detail` only shows in a local debug run.
    if debug and detail:
        print(f"debug: {detail}", file=sys.stderr)
    print(f"::error::{msg}")
    sys.exit(1)


# Hardcoded, not taken from the payload: anyone who can fire a
# repository_dispatch shouldn't be able to point the runner at a bucket of
# their choosing.
BUCKET = "cpp-test-artifacts"
# Every platform/arch the tests run on. A manual "newest" run picks a build
# that has all of them, so every job resolves the same SHA.
REQUIRED = [("linux", "x86_64"), ("darwin", "arm64")]
BUILD_TYPES = ("debug", "release")
# Sanitizer builds exist only for this platform/arch/build_type, and only these
# sanitizers per project (rtc-cpp's build_sanitizer_tests.sh).
SANITIZER_CONFIG = ("linux", "x86_64", "debug")
SANITIZERS = {"voice": ("asan", "tsan"), "video": ("asan", "tsan")}
SHA_RE = re.compile(r"[0-9a-f]{7,40}")
# rtc-cpp names tarballs with ${BUILDKITE_COMMIT:0:12}, so a manual `sha` input
# must be exactly that; anything else would just miss every key.
KEY_SHA_RE = re.compile(r"[0-9a-f]{12}")
CUTOFF_MARGIN = datetime.timedelta(minutes=10)
VERSION_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,1024}")
CUTOFF_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
VARIANT_RE = re.compile(r"(debug|release)(?:-(asan|tsan))?")


def expected_key(project, platform, arch, bt, sha, sanitizer=None):
    variant = f"{bt}-{sanitizer}" if sanitizer else bt
    return f"packages/{project}-{platform}-{arch}-{variant}-{sha}.tar.gz"


def artifact(bt, sanitizer, key, vid):
    return {"name": f"{bt}-{sanitizer}" if sanitizer else bt, "build_type": bt,
            "sanitizer": sanitizer, "key": key, "version_id": vid}


def resolve_dispatch(payload, platform, arch):
    project = payload.get("project")
    if project not in ("voice", "video"):
        fail("repository_dispatch payload has a missing or invalid 'project'", f"project={project!r}")
    if payload.get("bucket") not in (None, BUCKET):
        fail(f"repository_dispatch payload bucket is not {BUCKET}", f"bucket={payload.get('bucket')!r}")
    sha = payload.get("sha")
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        fail("repository_dispatch payload 'sha' is not a hex commit SHA", f"sha={sha!r}")
    all_artifacts = payload.get("artifacts")
    if not isinstance(all_artifacts, list):
        fail("repository_dispatch payload 'artifacts' is missing or not a list")
    # Filter to our own platform+arch (payload also carries the other platform).
    mine = [a for a in all_artifacts
            if isinstance(a, dict) and a.get("platform") == platform and a.get("arch") == arch]
    regular = [a for a in mine if a.get("sanitizer") is None]
    sanitized = [a for a in mine if a.get("sanitizer") is not None]
    if sorted(str(a.get("build_type")) for a in regular) != sorted(BUILD_TYPES):
        fail(f"repository_dispatch payload must carry exactly one debug and one release "
             f"artifact for {platform}/{arch}",
             f"build_types={[a.get('build_type') for a in regular]!r}")
    # The allowlist and build_type checks below must run before any fail()
    # that names `san`/`label`: those come from the payload, and are only safe
    # in the public log once they're known to be allowed values.
    allowed = SANITIZERS[project] if (platform, arch) == SANITIZER_CONFIG[:2] else ()
    names = [a.get("sanitizer") for a in sanitized]
    if len(set(names)) != len(names) or not set(names) <= set(allowed):
        fail(f"repository_dispatch payload has unexpected sanitizer artifacts for {platform}/{arch}",
             f"sanitizers={names!r} allowed={allowed!r}")
    artifacts = []
    for a in regular + sanitized:
        bt, san, key, vid = a.get("build_type"), a.get("sanitizer"), a.get("key"), a.get("version_id")
        label = f"{bt}-{san}" if san else bt
        if san and bt != SANITIZER_CONFIG[2]:
            fail(f"repository_dispatch payload sanitizer artifact {san} must be a "
                 f"{SANITIZER_CONFIG[2]} build", f"build_type={bt!r}")
        # The key must be exactly what the producer's naming scheme yields for
        # this project/platform/arch/build_type/sanitizer/sha — nothing outside
        # packages/, no other project, no other commit.
        want = expected_key(project, platform, arch, bt, sha, san)
        if key != want:
            fail(f"repository_dispatch payload key for {label} doesn't match the naming scheme",
                 f"key={key!r} expected={want!r}")
        if not isinstance(vid, str) or not VERSION_ID_RE.fullmatch(vid):
            fail(f"repository_dispatch payload version_id for {label} is missing or malformed",
                 f"version_id={vid!r}")
        artifacts.append(artifact(bt, san, key, vid))
    return project, sha, artifacts


def list_versions(prefix):
    """{key: [(last_modified, version_id), ...]} for keys whose latest version
    isn't a delete marker."""
    # `|| ``[]``` guards the empty case: the response omits Versions /
    # DeleteMarkers entirely when there are none.
    try:
        q = subprocess.run(
            ["aws", "s3api", "list-object-versions", "--bucket", BUCKET, "--prefix", prefix,
             "--query", "{v: Versions[].[Key, LastModified, VersionId] || `[]`, "
                        "d: DeleteMarkers[?IsLatest].Key || `[]`}",
             "--output", "json"],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        fail(f"listing s3://{BUCKET}/packages/ failed ({DETAILS_HINT})", e.stderr.strip())
    listing = json.loads(q.stdout or "{}")
    deleted = set(listing.get("d") or [])
    versions = {}
    for key, modified, vid in listing.get("v") or []:
        if key not in deleted:
            # list-object-versions returns e.g. "2026-11-10T18:01:39+00:00"
            # (always UTC); second-resolution UTC strings sort chronologically.
            versions.setdefault(key, []).append((modified[:19], vid))
    return versions


def newest_complete_sha(project, cutoff):
    # One listing for the project. A build counts only once all REQUIRED
    # platforms have both build types uploaded; otherwise "newest" could be a
    # build whose release (or other platform) isn't in S3 yet. Sanitizer
    # tarballs are optional and don't count.
    wanted = {(p, a, bt) for p, a in REQUIRED for bt in BUILD_TYPES}
    # Basename format: <project>-<platform>-<arch>-<build_type>-<sha>.tar.gz.
    # Known components contain no `-`, so the fields split cleanly. Sanitizer
    # keys (...-debug-asan-<sha>) don't match: `asan` isn't hex.
    key_re = re.compile(rf"packages/{project}-([a-z0-9]+)-([a-z0-9_]+)-(debug|release)-([0-9a-f]{{7,40}})\.tar\.gz")
    seen, first_upload = {}, {}
    for key, versions in list_versions(f"packages/{project}-").items():
        m = key_re.fullmatch(key)
        oldest = min(modified for modified, _ in versions)
        if not m or (m[1], m[2], m[3]) not in wanted or oldest > cutoff:
            continue
        sha = m[4]
        seen.setdefault(sha, set()).add((m[1], m[2], m[3]))
        first_upload[sha] = max(first_upload.get(sha, ""), oldest)
    complete = [sha for sha, have in seen.items() if have == wanted]
    if not complete:
        fail(f"no {project} build under s3://{BUCKET}/packages/ has all of "
             f"{sorted(p + '/' + a for p, a in REQUIRED)} in debug and release")
    return max(complete, key=lambda s: first_upload[s])


def version_at(key, cutoff):
    """Version id of the newest upload of `key` at or before `cutoff`, or None."""
    versions = [v for v in list_versions(key).get(key, []) if v[0] <= cutoff]
    return max(versions)[1] if versions else None


def resolve_manual(inputs, platform, arch, variant, cutoff):
    project = inputs.get("project") or "voice"
    if project not in ("voice", "video"):
        fail("invalid 'project' input", f"project={project!r}")
    sha = inputs.get("sha") or ""
    if sha and not KEY_SHA_RE.fullmatch(sha):
        fail("'sha' input must be the first 12 hex characters of an rtc-cpp commit", f"sha={sha!r}")
    if not CUTOFF_RE.fullmatch(cutoff):
        fail("CUTOFF is missing or malformed")
    t = datetime.datetime.strptime(cutoff, "%Y-%m-%dT%H:%M:%SZ") - CUTOFF_MARGIN
    cutoff = t.strftime("%Y-%m-%dT%H:%M:%S")
    if not sha:
        sha = newest_complete_sha(project, cutoff)
    m = VARIANT_RE.fullmatch(variant)
    bt, san = m[1], m[2]
    key = expected_key(project, platform, arch, bt, sha, san)
    # Listed with the exact key as prefix, so only that object's versions match.
    vid = version_at(key, cutoff)
    if vid is None:
        if san:
            return project, sha, []  # sanitizer tarballs are optional
        fail(f"{variant}: no {project} {platform}/{arch} tarball for this build uploaded at least "
             f"{CUTOFF_MARGIN.seconds // 60} minutes before the run started")
    return project, sha, [artifact(bt, san, key, vid)]


def run(what, cmd):
    # check=True would raise CalledProcessError, whose message is the full
    # command line (key, version id), so run unchecked and report generically.
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        fail(f"{what} failed ({DETAILS_HINT})",
             (r.stderr or "").strip().replace("\n", " | "))


def set_output(name, value):
    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        f.write(f"{name}={value}\n")


def main():
    platform, arch, variant = os.environ["PLATFORM"], os.environ["ARCH"], os.environ["VARIANT"]
    if (platform, arch) not in REQUIRED or not VARIANT_RE.fullmatch(variant):
        fail(f"unexpected PLATFORM/ARCH/VARIANT {platform}/{arch}/{variant}")
    event_name = os.environ["GITHUB_EVENT_NAME"]
    with open(os.environ["GITHUB_EVENT_PATH"]) as f:
        event = json.load(f)
    if event_name == "repository_dispatch":
        payload = event.get("client_payload")
        if not isinstance(payload, dict):
            fail("repository_dispatch has no client_payload object")
        project, sha, artifacts = resolve_dispatch(payload, platform, arch)
        artifacts = [a for a in artifacts if a["name"] == variant]
    elif event_name == "workflow_dispatch":
        project, sha, artifacts = resolve_manual(event.get("inputs") or {}, platform, arch, variant,
                                                 os.environ.get("CUTOFF", ""))
    else:
        fail(f"unexpected event {event_name}")

    if not artifacts:
        if VARIANT_RE.fullmatch(variant)[2]:
            # `variant` was validated above, so it's log-safe.
            print(f"::warning::{variant}: no {project} tarball for this build "
                  f"(the rtc-cpp build failed, or didn't upload), skipping")
            set_output("available", "false")
            return
        fail(f"{variant}: no {project} {platform}/{arch} tarball for this build")
    a = artifacts[0]
    if debug:
        print(f"debug: sha={sha} artifact={json.dumps(a)}", file=sys.stderr)

    out_dir = os.path.join("tests", variant)
    os.makedirs(out_dir, exist_ok=True)
    tarball = os.path.join(out_dir + ".tar.gz")
    print(f"{event_name}: {project} {platform}/{arch} {variant} (pinned version)")
    cmd = ["aws", "s3api", "get-object", "--bucket", BUCKET, "--key", a["key"]]
    if a["version_id"]:
        cmd += ["--version-id", a["version_id"]]
    # get-object prints the response (VersionId, ETag, metadata) as JSON on
    # stdout; it's captured, not printed.
    run(f"{variant}: download", cmd + [tarball])
    run(f"{variant}: extract", ["tar", "-xzf", tarball, "-C", out_dir])
    os.remove(tarball)
    files = [os.path.join(d, n) for d, _, names in os.walk(out_dir) for n in names]
    if not files:
        fail(f"{variant} tarball extracted no files")
    print(f"extracted {len(files)} files to {out_dir}")
    if debug:
        for p in files:
            print(f"debug: {p}", file=sys.stderr)
    set_output("available", "true")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # A traceback can carry payload values in exception messages.
        fail(f"fetching the test tarball failed ({DETAILS_HINT})",
             traceback.format_exc().replace("\n", " | "))
