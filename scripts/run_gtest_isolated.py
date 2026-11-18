#!/usr/bin/env python3
"""Run one shard of a gtest executable, one test per process.

Replaces rtc-cpp's scripts/gtest-single (what CircleCI ran) for this repo:
  - every test runs as its own process, so a crash or abort (e.g. RTC_FATAL on
    a missing credential) fails that test and the shard carries on;
  - a per-test timeout (CircleCI: INDIVIDUAL_DURATION=15m);
  - DISABLED_ tests are counted and skipped, and tests that GTEST_SKIP
    themselves are reported as skipped, not passed;
  - a summary at the end: counts plus the names of the tests that didn't pass;
  - --results is rewritten after every test, so a shard that's cancelled or
    hits the job timeout still reports what it ran (with an "error").

Sharding replaces CircleCI's `circleci tests split --timings-type=classname`:
we have no timing data, so test classes are dealt round-robin over the shards
in the order the binary lists them. Every shard lists the same binary with the
same filter, so the shards partition the classes exactly.

Logging policy: this repo's Actions logs are public. Test stdout/stderr and
gtest XML can carry SDK/WebRTC logs and assertion text, so they're discarded
unless --log-dir is given (for local reruns only; CI never passes it). Only
test names, statuses and durations are printed or written to --results.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

# A skipped test isn't a failure; it's counted separately so the counts show
# what actually ran.
OK_STATUSES = ("passed", "skipped")

# Grace period between SIGTERM and SIGKILL on timeout (gtest-single: `timeout -k 30s`).
KILL_GRACE_SECONDS = 30


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--binary", required=True, help="gtest executable")
    p.add_argument("--label", required=True, help="name for this shard in the summary")
    p.add_argument("--shard", type=int, default=0, help="0-based shard index")
    p.add_argument("--shards", type=int, default=1, help="total shards")
    p.add_argument("--exclude", default="", help="gtest negative patterns, ':'-separated (no leading '-')")
    p.add_argument("--timeout", type=int, default=15 * 60, help="per-test timeout in seconds")
    p.add_argument("--results", help="write a JSON summary here")
    p.add_argument("--log-dir", help="keep each test's output here (local debugging only)")
    p.add_argument("--restore-routes", action="store_true",
                   help="Linux: restore links, qdiscs, iptables filter rules and routes after every "
                        "test (network tests change them)")
    p.add_argument("test_args", nargs="*", help="extra args passed to every test (after --)")
    a = p.parse_args()
    if not 0 <= a.shard < a.shards:
        p.error("--shard must be in [0, --shards)")
    return a


def gtest_filter(positive, exclude):
    return f"{positive}:-{exclude}" if exclude else positive


def list_tests(binary, exclude):
    """Return ([(class, [tests])], disabled_count) in the binary's listing order."""
    cmd = [binary, "--gtest_list_tests", f"--gtest_filter={gtest_filter('*', exclude)}"]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                       cwd=os.path.dirname(binary))
    if r.returncode != 0:
        raise RuntimeError(f"--gtest_list_tests exited {r.returncode}")
    classes, disabled = [], 0
    current = None
    for raw in r.stdout.splitlines():
        line = raw.split("#", 1)[0].rstrip()  # drop "  # GetParam() = ..." comments
        if not line:
            continue
        if not raw.startswith(" "):
            # Class line, e.g. "Foo." or "Prefix/FooTest/0." (typed/param suites).
            current = (line, [])
            classes.append(current)
            continue
        if current is None:
            continue  # stray output before the first class
        test = line.strip()
        # gtest-single only checked the class line for DISABLED_ but then ran
        # the tests under it under the previous class's prefix. Here a test is
        # disabled if either its class or its own name is.
        if current[0].startswith("DISABLED_") or test.startswith("DISABLED_") \
                or "/DISABLED_" in current[0]:
            disabled += 1
            continue
        current[1].append(test)
    return [(c, t) for c, t in classes if t], disabled


def run_one(binary, name, timeout, test_args, log_path, current):
    """Run a single test; return (status, seconds).

    status: passed|skipped|failed|crashed|timeout.
    """
    with tempfile.TemporaryDirectory() as tmp:
        xml_path = os.path.join(tmp, "result.xml")
        cmd = [binary, "--gtest_also_run_disabled_tests", f"--gtest_filter={name}",
               f"--gtest_output=xml:{xml_path}", *test_args]
        out = open(log_path, "wb") if log_path else subprocess.DEVNULL
        start = time.monotonic()
        try:
            # New session so a timeout can kill the test and anything it spawned.
            proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT,
                                    cwd=os.path.dirname(binary), start_new_session=True)
            current["proc"] = proc
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                kill_group(proc)
                return "timeout", time.monotonic() - start
        finally:
            if log_path:
                out.close()
        elapsed = time.monotonic() - start
        if rc < 0:
            return "crashed", elapsed  # killed by a signal (SIGSEGV, SIGABRT, ...)
        if not os.path.exists(xml_path):
            # gtest writes the XML at exit, so no XML means the process died
            # before finishing (abort handler, exit() from a fatal check, ...).
            return "crashed", elapsed
        # Nonzero with an XML: a gtest failure, or a sanitizer report at exit
        # (TSan exits 66 after the run when it found races).
        if rc != 0:
            return "failed", elapsed
        # GTEST_SKIP exits 0 too; only the XML tells it apart from a pass.
        return ("skipped" if was_skipped(xml_path) else "passed"), elapsed


def was_skipped(xml_path):
    try:
        cases = ET.parse(xml_path).getroot().iter("testcase")
        return any(c.get("result") == "skipped" for c in cases)
    except ET.ParseError:
        return False


def kill_group(proc):
    for sig, wait in ((signal.SIGTERM, KILL_GRACE_SECONDS), (signal.SIGKILL, 10)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def quiet(cmd, stdin=None):
    try:
        r = subprocess.run(cmd, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           text=True, check=False)
        return r.stdout if r.returncode == 0 else None
    except OSError:
        return None


class NetworkState:
    """The container's network as it was before the first test.

    Network tests clean up in their fixture teardown, which a crash or a
    timeout kill skips, and the next test would run with eth0 down, a 100%
    loss qdisc or a DROP policy. gtest-single only restored routes; this also
    brings back links that were up, removes root qdiscs and restores the
    iptables filter table (Docker's DNS rules live in the nat table, which is
    left alone). Best effort, like gtest-single's `ip route restore`: e.g.
    restoring routes that still exist fails with "File exists", which is fine.
    """

    def __init__(self):
        self.links = (quiet(["ip", "-o", "link", "show", "up"]) or "").splitlines()
        self.links = [l.split(":")[1].strip().split("@")[0] for l in self.links if l.count(":") >= 2]
        self.routes = tempfile.NamedTemporaryFile(prefix="routes-", delete=False).name
        with open(self.routes, "wb") as f:
            subprocess.run(["ip", "route", "save"], stdout=f, stderr=subprocess.DEVNULL, check=False)
        self.filter = quiet(["iptables-save", "-t", "filter"])

    def restore(self):
        for link in self.links:
            quiet(["tc", "qdisc", "del", "dev", link, "root"])
            quiet(["ip", "link", "set", link, "up"])
        if self.filter:
            with tempfile.TemporaryFile("w+") as f:
                f.write(self.filter)
                f.seek(0)
                quiet(["iptables-restore", "-w", "5", "-T", "filter"], stdin=f)
        with open(self.routes, "rb") as f:
            quiet(["ip", "route", "restore"], stdin=f)


def safe_filename(name):
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in name)


def write_step_summary(label, counts, results):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    bad = [r for r in results if r["status"] not in OK_STATUSES]
    with open(path, "a") as f:
        f.write(f"### {label}\n\n")
        f.write("| " + " | ".join(counts) + " |\n|" + "---|" * len(counts) + "\n")
        f.write("| " + " | ".join(str(v) for v in counts.values()) + " |\n\n")
        if bad:
            f.write("<details><summary>Tests that didn't pass</summary>\n\n")
            for r in bad:
                f.write(f"- `{r['name']}`: {r['status']}\n")
            f.write("\n</details>\n\n")


class Interrupted(Exception):
    pass


def on_signal(signum, _frame):
    raise Interrupted(signal.Signals(signum).name)


def write_json(path, data):
    # Atomic, so a reader (or a kill mid-write) never sees half a file.
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def main():
    a = parse_args()
    binary = os.path.abspath(a.binary)
    counts = {"passed": 0, "skipped": 0, "failed": 0, "crashed": 0, "timeout": 0, "disabled": 0}
    summary = {"label": a.label, "shard": a.shard, "shards": a.shards, "counts": counts,
               "tests": [], "error": None}

    def save():
        if a.results:
            write_json(a.results, summary)

    def finish(code):
        save()
        sys.exit(code)

    try:
        classes, disabled = list_tests(binary, a.exclude)
    except (OSError, RuntimeError) as e:
        summary["error"] = f"listing tests failed: {e}"
        print(f"::error::{a.label}: {summary['error']}")
        finish(1)
    mine = [(c, tests) for i, (c, tests) in enumerate(classes) if i % a.shards == a.shard]
    names = [c + t for c, tests in mine for t in tests]
    # Disabled tests are reported once, by shard 0, rather than per shard.
    counts["disabled"] = disabled if a.shard == 0 else 0
    print(f"{a.label}: shard {a.shard + 1}/{a.shards} runs {len(names)} tests "
          f"from {len(mine)} of {len(classes)} classes")
    if a.log_dir:
        os.makedirs(a.log_dir, exist_ok=True)

    network = NetworkState() if a.restore_routes and sys.platform.startswith("linux") else None

    # Until the loop finishes, the file on disk says the shard didn't.
    summary["error"] = f"interrupted after 0 of {len(names)} tests"
    save()

    # Step cancel / job timeout: GitHub sends SIGINT then SIGTERM (docker run
    # forwards both). Record the shard as interrupted instead of dying silently.
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    current = {}
    try:
        for n, name in enumerate(names, 1):
            current["name"] = name
            log_path = os.path.join(a.log_dir, safe_filename(name) + ".log") if a.log_dir else None
            status, secs = run_one(binary, name, a.timeout, a.test_args, log_path, current)
            if network:
                network.restore()
            counts[status] += 1
            summary["tests"].append({"name": name, "status": status, "seconds": round(secs, 1)})
            summary["error"] = f"interrupted after {n} of {len(names)} tests"
            save()
            current.clear()
            print(f"[{n}/{len(names)}] {status.upper():8} {name} ({secs:.1f}s)", flush=True)
    except Interrupted as e:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        proc = current.get("proc")
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if current.get("name"):
            counts["timeout"] += 1
            summary["tests"].append({"name": current["name"], "status": "timeout", "seconds": None})
        summary["error"] = (f"interrupted ({e}: cancelled or job timeout) at test "
                            f"{len(summary['tests'])} of {len(names)}")
        print(f"::error::{a.label}: {summary['error']}")
    else:
        summary["error"] = None

    bad = [t for t in summary["tests"] if t["status"] not in OK_STATUSES]
    print("\n========== SUMMARY: " + a.label + " ==========")
    print(f"passed {counts['passed']}, skipped {counts['skipped']}, failed {counts['failed']}, "
          f"crashed {counts['crashed']}, timeout {counts['timeout']}, disabled {counts['disabled']}")
    if bad:
        print(f"{len(bad)} test(s) did not pass:")
        for t in bad:
            print(f"  - {t['name']} [{t['status']}]")
    elif names and not summary["error"]:
        print(f"All {len(names)} tests passed or skipped")
    write_step_summary(a.label, counts, summary["tests"])
    finish(1 if bad or summary["error"] else 0)


if __name__ == "__main__":
    main()
