#!/usr/bin/env python3
"""Combine every test job's results into one summary.

Reads the planned matrix (MATRIX env, the plan job's output) and the
results-<job>.json files the test jobs uploaded into RESULTS_DIR. A job with
no results file never got as far as writing one (download failure, runner
loss, cancelled), so it's reported as an infrastructure failure rather than
silently dropped. A job that skipped because its sanitizer build wasn't
uploaded writes {"skipped": true}.

Prints the summary, writes it to $GITHUB_STEP_SUMMARY, and exits 1 if any job
didn't pass. Only job names, test names and statuses are printed; they're
already in the public test-job logs.
"""

import json
import os
import sys

STATUSES = ("passed", "failed", "crashed", "timeout", "disabled")


def main():
    legs = json.loads(os.environ["MATRIX"])["include"]
    results_dir = os.environ["RESULTS_DIR"]
    rows, bad_jobs, bad_tests = [], [], []
    totals = dict.fromkeys(STATUSES, 0)
    for leg in legs:
        path = os.path.join(results_dir, f"results-{leg['name']}.json")
        try:
            with open(path) as f:
                r = json.load(f)
        except (OSError, ValueError):
            rows.append((leg["name"], "NO RESULTS", None))
            bad_jobs.append((leg["name"], "no results (the job failed before or while running tests)"))
            continue
        if r.get("skipped"):
            rows.append((leg["name"], "skipped", None))
            continue
        # A results file with an "error" may have no counts (the job failed
        # before running tests) or partial ones (interrupted shard).
        c = r.get("counts") or {}
        for s in STATUSES:
            totals[s] += c.get(s, 0)
        failed = [t for t in r.get("tests", []) if t["status"] != "passed"]
        if r.get("error"):
            outcome = "ERROR"
            bad_jobs.append((leg["name"], r["error"]))
        elif failed:
            outcome = "FAILED"
            bad_jobs.append((leg["name"], f"{len(failed)} test(s) didn't pass"))
        else:
            outcome = "passed"
        bad_tests += [(leg["name"], t["name"], t["status"]) for t in failed]
        rows.append((leg["name"], outcome, c or None))

    lines = [f"{len(legs)} test jobs: {len(legs) - len(bad_jobs)} ok, {len(bad_jobs)} not ok",
             "tests: " + ", ".join(f"{s} {totals[s]}" for s in STATUSES)]
    print("\n========== RUN SUMMARY ==========")
    print("\n".join(lines))
    if bad_jobs:
        print("\nJobs that didn't pass:")
        for name, why in bad_jobs:
            print(f"  - {name}: {why}")
    if bad_tests:
        print("\nTests that didn't pass:")
        for job, test, status in bad_tests:
            print(f"  - [{job}] {test}: {status}")

    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write("## rtc-cpp test run\n\n" + "\n\n".join(lines) + "\n\n")
            f.write("| job | outcome | " + " | ".join(STATUSES) + " |\n")
            f.write("|---|---|" + "---|" * len(STATUSES) + "\n")
            for name, outcome, c in rows:
                cells = [str(c.get(s, 0)) for s in STATUSES] if c else [""] * len(STATUSES)
                f.write(f"| {name} | {outcome} | " + " | ".join(cells) + " |\n")
            if bad_tests:
                f.write("\n<details><summary>Tests that didn't pass</summary>\n\n")
                for job, test, status in bad_tests:
                    f.write(f"- `{job}`: `{test}`: {status}\n")
                f.write("\n</details>\n")
    sys.exit(1 if bad_jobs else 0)


if __name__ == "__main__":
    main()
