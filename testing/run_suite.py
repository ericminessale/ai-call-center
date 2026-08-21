#!/usr/bin/env python3
"""Run several scenarios in sequence and aggregate what they found.

    python testing/run_suite.py --suite naive
    python testing/run_suite.py testing/scenarios/naive_hesitant.json ...

Why this exists rather than just looping in a shell: for the naive suite the
interesting output is not four separate pass/fail lines, it is the pattern
ACROSS callers. "3 of 4 callers did not know what the options meant" is a
finding someone can act on; the same fact spread over four scrollbacks as
individual soft-FAILs is noise that gets skimmed.

Each scenario still runs as its own ``run_scenario.py`` process, so every run
gets a fresh demo workspace and each caller is genuinely a first-time caller -
which matters here, because contact memory would otherwise have callers 2..N
greeted by name and no longer naive about anything.

Exit code is 1 if any scenario had a HARD failure. Soft failures never fail the
suite: in the naive suite they are findings to triage, not tests to fix.
"""
import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCENARIOS = HERE / 'scenarios'
ARTIFACT_RE = re.compile(r'artifacts:\s*(.+?)\s*$')


def discover(suite: str) -> list:
    found = []
    for path in sorted(SCENARIOS.glob('*.json')):
        try:
            doc = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        if doc.get('_suite') == suite:
            found.append(path)
    return found


def run_one(path: Path) -> dict:
    """Run one scenario, echoing its output, and return its parsed artifacts."""
    print(f"\n{'=' * 72}\nSUITE: running {path.name}\n{'=' * 72}", flush=True)
    artifact_dir = None
    proc = subprocess.Popen(
        [sys.executable, str(HERE / 'run_scenario.py'), str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding='utf-8', errors='replace', bufsize=1,
    )
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        match = ARTIFACT_RE.search(line)
        if match:
            artifact_dir = match.group(1)
    proc.wait()

    result = {'scenario': path.stem, 'exit_code': proc.returncode,
              'assertions': [], 'verdict': None}
    if artifact_dir:
        run_json = Path(artifact_dir) / 'run.json'
        if run_json.exists():
            try:
                doc = json.loads(run_json.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError):
                return result
            result['assertions'] = doc.get('assertions') or []
            context = doc.get('context') or {}
            verdict = (context.get('verdict1') or {}).get('parsed')
            result['verdict'] = verdict if isinstance(verdict, dict) else None
    return result


def report(results: list) -> int:
    print(f"\n\n{'=' * 72}\nSUITE SUMMARY\n{'=' * 72}")

    hard_failures = 0
    for res in results:
        hard = [a for a in res['assertions']
                if a.get('level', 'hard') == 'hard' and not a.get('passed')]
        soft = [a for a in res['assertions']
                if a.get('level') == 'soft' and not a.get('passed')]
        hard_failures += len(hard)
        if not res['assertions']:
            state = 'NO ARTIFACTS'
        elif hard:
            state = f'DEAD END ({len(hard)} hard)'
        elif soft:
            state = f'got through, {len(soft)} friction'
        else:
            state = 'clean'
        print(f"  {res['scenario']:<30} {state}")
        for a in hard:
            print(f"      HARD  {a['label']}  (actual={a['actual']!r})")

    # The point of the whole exercise: which friction signals tripped, and for
    # how many of the callers. Grouped by signal rather than by caller, because
    # a thing that happens to every caller is a product problem and a thing
    # that happens to one is a personality.
    by_signal = defaultdict(list)
    for res in results:
        for a in res['assertions']:
            if a.get('level') == 'soft' and not a.get('passed'):
                label = a['label'].replace('FRICTION: ', '')
                by_signal[label].append(res['scenario'])

    total = len(results)
    print(f"\n{'-' * 72}\nFRICTION ACROSS {total} CALLER(S) - findings to triage, not tests to fix\n{'-' * 72}")
    if not by_signal:
        print('  (none)')
    for label, scenarios in sorted(by_signal.items(),
                                   key=lambda kv: (-len(kv[1]), kv[0])):
        # "FAILED:" is not redundant. The assertion labels are phrased
        # positively ("the caller knew what the options meant"), so a bare
        # count in front of one reads as the opposite of what it means.
        print(f"  {len(scenarios)}/{total} callers FAILED: {label}")
        print(f"          {', '.join(scenarios)}")

    # Free-text is where the unanticipated things show up - the friction nobody
    # thought to write an assertion for.
    print(f"\n{'-' * 72}\nWHAT THE CALLERS SAID WAS CONFUSING\n{'-' * 72}")
    said_something = False
    for res in results:
        verdict = res['verdict'] or {}
        for key in ('most_confusing_moment', 'notes'):
            text = (verdict.get(key) or '').strip()
            if text:
                said_something = True
                print(f"  [{res['scenario']}] {key}: {text}")
    if not said_something:
        print('  (nothing reported)')

    print(f"\n{'=' * 72}")
    verdict_line = ('SUITE PASS - every caller got somewhere'
                    if hard_failures == 0
                    else f'SUITE FAIL - {hard_failures} dead end(s)')
    print(verdict_line)
    return 0 if hard_failures == 0 else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('scenarios', nargs='*', help='scenario JSON paths')
    parser.add_argument('--suite', help="run every scenario whose _suite field matches "
                                        "(e.g. 'naive')")
    args = parser.parse_args()

    paths = [Path(p) for p in args.scenarios]
    if args.suite:
        paths = discover(args.suite) + paths
    if not paths:
        parser.error('nothing to run: pass scenario paths or --suite NAME')

    missing = [p for p in paths if not p.exists()]
    if missing:
        parser.error(f"scenario file(s) not found: {', '.join(map(str, missing))}")

    print(f"suite: {len(paths)} scenario(s) -> "
          f"{', '.join(p.stem for p in paths)}")
    results = [run_one(p) for p in paths]
    sys.exit(report(results))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\ninterrupted')
        sys.exit(130)
