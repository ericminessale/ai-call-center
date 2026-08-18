#!/usr/bin/env python3
"""Synthetic-caller scenario runner.

Places REAL calls at this deployment's inbound number, with an AI "caller
bot" persona on the outbound leg (SWML served by the backend's
/api/testing/bot-swml route), then asserts on three verdict sources:

  1. the bot's own structured post-prompt verdict (Redis, via the harness),
  2. backend ground truth (Call row, ai_context, transcript, contact/digest),
  3. keyword checks over the stored transcript.

Prereqs:
  - the stack is up and TESTING_HARNESS_ENABLED=true in .env (backend
    rebuilt/restarted after flipping it),
  - the ngrok tunnel in EXTERNAL_URL is live,
  - TEST_CALLER_NUMBER in .env is a number the SignalWire project owns
    (it is the bot's caller ID and must NOT be the inbound demo number).

Usage:
  python testing/run_scenario.py testing/scenarios/fred_returning_caller.json
  python testing/run_scenario.py <scenario.json> --release   # end demo ws after
"""

import argparse
import base64
import json
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATE_FILE = HERE / '.state.json'
RESULTS_DIR = HERE / 'results'


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_env(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


class Config:
    def __init__(self, args):
        env = load_env(ROOT / '.env')
        self.space = env.get('SIGNALWIRE_SPACE')
        self.project = env.get('SIGNALWIRE_PROJECT_ID')
        self.token = env.get('SIGNALWIRE_API_TOKEN')
        self.demo_number = env.get('SIGNALWIRE_PHONE_NUMBER')
        self.test_number = args.number or env.get('TEST_CALLER_NUMBER')
        self.external_url = (env.get('EXTERNAL_URL') or '').rstrip('/')
        self.backend_local = (args.backend or env.get('BACKEND_LOCAL_URL')
                              or 'http://localhost:5000').rstrip('/')
        self.webhook_user = env.get('WEBHOOK_AUTH_USER')
        self.webhook_password = env.get('WEBHOOK_AUTH_PASSWORD')
        internal_user = env.get('INTERNAL_AUTH_USER') or self.webhook_user
        internal_password = (env.get('INTERNAL_AUTH_PASSWORD')
                             or self.webhook_password)
        self.internal_auth = (internal_user, internal_password)

        missing = [name for name, value in [
            ('SIGNALWIRE_SPACE', self.space),
            ('SIGNALWIRE_PROJECT_ID', self.project),
            ('SIGNALWIRE_API_TOKEN', self.token),
            ('SIGNALWIRE_PHONE_NUMBER', self.demo_number),
            ('TEST_CALLER_NUMBER', self.test_number),
            ('EXTERNAL_URL', self.external_url),
            ('WEBHOOK_AUTH_USER', self.webhook_user),
        ] if not value]
        if missing:
            die(f"missing required .env values: {', '.join(missing)}")
        if self.test_number == self.demo_number:
            die("TEST_CALLER_NUMBER must differ from SIGNALWIRE_PHONE_NUMBER "
                "(the bot cannot call from the number it is calling)")
        # Bot legs that have not yet produced a verdict; ended on exit so a
        # wedged conversation can never outlive the runner.
        self.active_legs = []

    @property
    def sw_auth(self):
        return (self.project, self.token)


def die(msg: str, code: int = 2):
    print(f"FATAL: {msg}")
    sys.exit(code)


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Backend harness client (internal auth, via localhost)
# ---------------------------------------------------------------------------

def internal_get(cfg: Config, path: str, **kwargs):
    return requests.get(cfg.backend_local + path, auth=cfg.internal_auth,
                        timeout=20, **kwargs)


def preflight(cfg: Config):
    log("preflight: backend health ...")
    r = requests.get(cfg.backend_local + '/health', timeout=10)
    r.raise_for_status()

    r = internal_get(cfg, '/api/testing/ping')
    if r.status_code == 404:
        die("harness routes missing - set TESTING_HARNESS_ENABLED=true in "
            ".env and rebuild/restart the backend "
            "(docker compose up -d --build backend)")
    if r.status_code == 401:
        die("internal auth rejected - check WEBHOOK_AUTH_USER/PASSWORD "
            "(or INTERNAL_AUTH_*) in .env match the running backend")
    r.raise_for_status()

    log("preflight: harness through the tunnel ...")
    try:
        r = requests.get(cfg.external_url + '/api/testing/ping',
                         auth=cfg.internal_auth, timeout=15,
                         headers={'ngrok-skip-browser-warning': '1'})
        if r.status_code != 200:
            die(f"tunnel reached but harness ping returned {r.status_code} - "
                "is EXTERNAL_URL pointing at this stack?")
    except requests.RequestException as exc:
        die(f"EXTERNAL_URL unreachable ({exc}) - is ngrok running?")
    log("preflight: OK")


# ---------------------------------------------------------------------------
# Demo session + phone pairing (the real onboarding path)
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding='utf-8'))
        except ValueError:
            return {}
    return {}


def _save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding='utf-8')


def release_previous_session(cfg: Config):
    """End the previous run's demo workspace so its number binding frees up."""
    state = _load_state()
    cookies = state.get('cookies')
    if not cookies:
        return
    log("releasing previous run's demo workspace ...")
    try:
        requests.post(cfg.backend_local + '/api/demo/end',
                      cookies=cookies, timeout=15)
    except requests.RequestException as exc:
        log(f"  (release failed: {exc} - continuing)")
    _save_state({})


def demo_start(cfg: Config):
    """Provision a fresh visitor workspace; returns (session, bearer_token)."""
    session = requests.Session()
    r = session.post(cfg.backend_local + '/api/demo/start', timeout=30)
    if r.status_code == 503:
        die("demo/start says the demo is full - free a workspace and retry")
    r.raise_for_status()
    body = r.json()
    token = (body.get('access_token') or body.get('token')
             or (body.get('tokens') or {}).get('access_token'))
    if not token:
        die(f"demo/start returned no access token (keys: {sorted(body)})")
    ws = body.get('workspace') or {}
    log(f"demo workspace ready: id={ws.get('id')} "
        f"public_id={ws.get('public_id')}")
    _save_state({'cookies': session.cookies.get_dict(),
                 'workspace': {'id': ws.get('id'),
                               'public_id': ws.get('public_id')}})
    return session, token


def get_pairing_code(cfg: Config, session, token) -> str:
    r = session.post(cfg.backend_local + '/api/demo/verify/pairing-code',
                     headers={'Authorization': f'Bearer {token}'}, timeout=15)
    r.raise_for_status()
    code = r.json().get('code')
    if not code:
        die("pairing-code endpoint returned no code")
    return code


def send_pairing_sms(cfg: Config, code: str) -> bool:
    """Real path: MO SMS from the test number to the demo number."""
    url = (f"https://{cfg.space}/api/laml/2010-04-01/Accounts/"
           f"{cfg.project}/Messages.json")
    try:
        r = requests.post(url, auth=cfg.sw_auth, timeout=20, data={
            'From': cfg.test_number, 'To': cfg.demo_number, 'Body': code,
        })
        if r.status_code >= 300:
            log(f"  SMS API returned {r.status_code}: {r.text[:200]}")
            return False
        return True
    except requests.RequestException as exc:
        log(f"  SMS send failed: {exc}")
        return False


def inject_pairing_webhook(cfg: Config, code: str):
    """Fallback: deliver the MO message straight to our own signed webhook.

    Same backend code path as a carrier-delivered SMS (pair_number et al) -
    only the carrier hop is skipped. Used when 10DLC blocks the real SMS.
    """
    log("  falling back to direct sms-inbound webhook injection ...")
    r = requests.post(
        cfg.external_url + '/api/webhooks/sms-inbound',
        auth=(cfg.webhook_user, cfg.webhook_password),
        headers={'ngrok-skip-browser-warning': '1'},
        data={'From': cfg.test_number, 'To': cfg.demo_number, 'Body': code},
        timeout=20,
    )
    if r.status_code != 200:
        die(f"sms-inbound injection failed: HTTP {r.status_code}")


def wait_until_verified(cfg: Config, session, token, timeout_s: int = 60) -> bool:
    tail = re.sub(r'\D', '', cfg.test_number)[-4:]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = session.get(cfg.backend_local + '/api/demo/verify/status',
                        headers={'Authorization': f'Bearer {token}'},
                        timeout=15)
        if r.status_code == 200:
            body = r.json()
            blob = json.dumps(body)
            if body.get('verified') or (tail and tail in blob):
                return True
        time.sleep(3)
    return False


def start_heartbeat(cfg: Config, session, stop_event):
    """Keep the demo workspace's lease + number binding firmly held.

    Without this, another session's pairing can claim the test number away
    from a run in progress (observed 2026-08-10: a concurrent run re-bound
    the number mid-scenario and later calls landed in its workspace).
    """
    def beat():
        while not stop_event.wait(60):
            try:
                session.post(cfg.backend_local + '/api/demo/heartbeat',
                             timeout=10)
            except requests.RequestException:
                pass
    t = threading.Thread(target=beat, daemon=True)
    t.start()
    return t


def pair_test_number(cfg: Config, session, token):
    code = get_pairing_code(cfg, session, token)
    log(f"pairing code {code} - sending SMS "
        f"{cfg.test_number} -> {cfg.demo_number}")
    sent = send_pairing_sms(cfg, code)
    if sent and wait_until_verified(cfg, session, token, timeout_s=45):
        log("number verified via real SMS")
        return
    if sent:
        log("  SMS accepted but pairing did not land (carrier filtering?)")
    inject_pairing_webhook(cfg, code)
    if not wait_until_verified(cfg, session, token, timeout_s=20):
        die("number still unverified after webhook injection - check "
            "backend logs (docker compose logs backend | grep sms-inbound)")
    log("number verified via webhook injection")


# ---------------------------------------------------------------------------
# Mission execution
# ---------------------------------------------------------------------------

def dial_bot(cfg: Config, run_id: str, mission: dict) -> str:
    envelope = {
        'persona': mission['persona'],
        'post_prompt': mission['post_prompt'],
    }
    if 'temperature' in mission:
        envelope['temperature'] = mission['temperature']
    if 'params' in mission:
        envelope['params'] = mission['params']
    m64 = base64.urlsafe_b64encode(
        json.dumps(envelope).encode()).decode()

    parsed = urlparse(cfg.external_url)
    host = parsed.netloc
    swml_url = (f"{parsed.scheme}://{quote(cfg.webhook_user, safe='')}:"
                f"{quote(cfg.webhook_password, safe='')}@{host}"
                f"/api/testing/bot-swml?run_id={run_id}&m={m64}")

    r = requests.post(f"https://{cfg.space}/api/calling/calls",
                      auth=cfg.sw_auth, timeout=30, json={
                          'command': 'dial',
                          'params': {
                              'from': cfg.test_number,
                              'to': cfg.demo_number,
                              'url': swml_url,
                          },
                      })
    if r.status_code >= 300:
        die(f"Calling API dial failed: HTTP {r.status_code}: {r.text[:300]}")
    body = r.json()
    leg_id = body.get('id') or body.get('call_id')
    log(f"dialed: bot leg {leg_id}")
    return leg_id


def end_call_leg(cfg: Config, leg_id: str) -> bool:
    """Hang up a live leg. PUT + top-level id + calling.end is the shape the
    Calling API actually accepts (POST/params variants 404)."""
    try:
        r = requests.put(f"https://{cfg.space}/api/calling/calls",
                         auth=cfg.sw_auth, timeout=20, json={
                             'id': leg_id,
                             'command': 'calling.end',
                             'params': {'reason': 'hangup'},
                         })
        log(f"  kill-switch: ended leg {leg_id} (HTTP {r.status_code})")
        return r.status_code < 300
    except requests.RequestException as exc:
        log(f"  kill-switch failed for {leg_id}: {exc}")
        return False


def poll_report(cfg: Config, since_iso: str, gates: dict, timeout_s: int = 150):
    """Poll /call-report until the awaited artifacts exist.

    Gates: summary (post-prompt landed), digest (caller memory finalized),
    transcript (any rows), tools (ai_tool_call rows persisted — pass ``true``
    for any, or a tool NAME to wait for that specific one).
    Returns the last report seen even on timeout.

    `tools` is a separate gate from `summary` because post_prompt writes the
    summary BEFORE it persists tool calls (and commits in between), so a
    summary-only gate can freeze a report mid-handler with an empty
    tool_calls list — an intermittent failure of the tool assertions that
    has nothing to do with the feature.
    """
    deadline = time.time() + timeout_s
    report = None
    while time.time() < deadline:
        r = internal_get(cfg, '/api/testing/call-report',
                         params={'phone': cfg.test_number, 'since': since_iso})
        if r.status_code == 200:
            report = r.json()
            call = report.get('call') or {}
            contact = report.get('contact') or {}
            ok = True
            if gates.get('summary') and not call.get('summary'):
                ok = False
            if gates.get('digest') and not contact.get('interaction_digest'):
                ok = False
            if gates.get('transcript') and not report.get('transcript'):
                ok = False
            tools_gate = gates.get('tools')
            if tools_gate:
                names = report.get('tool_call_names') or ''
                if not report.get('tool_calls'):
                    ok = False
                elif isinstance(tools_gate, str) and tools_gate not in names:
                    # Naming the tool matters: triage persists its own calls
                    # well before the specialist's post_prompt runs, so "any
                    # tool row exists" can be true while the tool a hard
                    # assertion is about has not landed yet. That is a false
                    # failure on a correct call.
                    ok = False
            if ok:
                return report
        time.sleep(5)
    return report


def derived_fields(report) -> dict:
    if not report:
        return {'ai_text': '', 'caller_text': '', 'full_text': ''}
    rows = report.get('transcript') or []
    ai = ' '.join(r['text'] for r in rows
                  if r.get('text') and r.get('speaker') in ('ai', 'agent'))
    caller = ' '.join(r['text'] for r in rows
                      if r.get('text') and r.get('speaker') == 'caller')
    return {'ai_text': ai, 'caller_text': caller,
            'full_text': f"{ai} {caller}"}


def run_mission(cfg: Config, scenario_name: str, mission: dict) -> dict:
    label = mission.get('label', mission['id'])
    log(f"--- mission: {label}")
    pause = mission.get('pause_before_s', 0)
    if pause:
        log(f"settling {pause}s before dialing ...")
        time.sleep(pause)

    run_id = f"{mission['id']}-{secrets.token_hex(4)}"
    since = (datetime.utcnow() - timedelta(seconds=5)).isoformat()
    leg_id = dial_bot(cfg, run_id, mission)
    cfg.active_legs.append(leg_id)

    log("waiting for the bot's verdict (call in progress) ...")
    dialed_at = time.time()
    max_call_s = mission.get('max_call_s', 240)
    verdict, killed = None, False
    deadline = dialed_at + max_call_s + 90
    while time.time() < deadline:
        r = internal_get(cfg, f'/api/testing/verdict/{run_id}')
        if r.status_code == 200:
            verdict = r.json().get('verdict')
            break
        if not killed and time.time() - dialed_at > max_call_s:
            log(f"  call exceeded max_call_s={max_call_s} - "
                "ending the bot leg (verdict still expected after hangup)")
            end_call_leg(cfg, leg_id)
            killed = True
            deadline = time.time() + 90
        time.sleep(5)
    if leg_id in cfg.active_legs:
        cfg.active_legs.remove(leg_id)
    if verdict is None:
        log("  WARNING: no verdict arrived within timeout")
        if not killed:
            end_call_leg(cfg, leg_id)
    else:
        log(f"  verdict in: parsed={'yes' if verdict.get('parsed') else 'NO'}"
            + (" (call was killed at max_call_s)" if killed else ""))

    gates = mission.get('await') or {}
    log(f"waiting for backend artifacts {sorted(gates) or '(none)'} ...")
    report = poll_report(cfg, since, gates)
    if report is None:
        log("  WARNING: no matching Call row appeared")
    else:
        call = report.get('call') or {}
        log(f"  call row {call.get('id')} status={call.get('status')} "
            f"summary={'yes' if call.get('summary') else 'no'}")
        report['derived'] = derived_fields(report)

    return {'run_id': run_id, 'bot_leg_id': leg_id, 'since': since,
            'verdict': verdict, 'report': report}


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------

def resolve_path(ctx: dict, dotted: str):
    node = ctx
    for part in dotted.split('.'):
        if node is None:
            return None
        if isinstance(node, dict):
            node = node.get(part)
        else:
            node = getattr(node, part, None)
    return node


def check(op: str, actual, expected) -> bool:
    if op == 'eq':
        return actual == expected
    if op == 'truthy':
        return bool(actual)
    if op == 'non_empty':
        return bool(actual) and len(actual) > 0
    if op == 'contains':
        return isinstance(actual, str) and str(expected).lower() in actual.lower()
    if op == 'contains_any':
        return isinstance(actual, str) and any(
            str(e).lower() in actual.lower() for e in expected)
    if op == 'not_contains_any':
        # A hallucination guard, so absence of evidence must NOT read as
        # evidence of absence. An empty or missing transcript means nothing
        # was examined, and a guard that goes green on nothing is worse than
        # no guard: the await gate returns the last report on timeout, so a
        # run could pass this while never having seen what the agent said.
        if not actual or not isinstance(actual, str):
            return False
        return not any(str(e).lower() in actual.lower() for e in expected)
    raise ValueError(f"unknown assertion op {op!r}")


def run_assertions(scenario: dict, ctx: dict) -> list:
    results = []
    for spec in scenario.get('assertions', []):
        actual = resolve_path(ctx, spec['target'])
        try:
            passed = check(spec['op'], actual, spec.get('value'))
        except ValueError as exc:
            passed, actual = False, f"<{exc}>"
        results.append({
            'label': spec['label'],
            'level': spec.get('level', 'hard'),
            'target': spec['target'],
            'passed': passed,
            'actual': actual if isinstance(actual, (str, int, float, bool,
                                                    type(None))) else '<object>',
        })
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('scenario', help='path to scenario JSON')
    parser.add_argument('--release', action='store_true',
                        help='end the demo workspace after the run '
                             '(default: keep it for dashboard inspection; '
                             'the NEXT run releases it automatically)')
    parser.add_argument('--backend', help='backend base URL '
                                          '(default http://localhost:5000)')
    parser.add_argument('--number', help='override TEST_CALLER_NUMBER')
    args = parser.parse_args()

    scenario_path = Path(args.scenario)
    if not scenario_path.exists():
        die(f"scenario file not found: {scenario_path}")
    scenario = json.loads(scenario_path.read_text(encoding='utf-8'))

    cfg = Config(args)
    log(f"scenario: {scenario['name']}")
    log(f"caller bot {cfg.test_number} -> demo line {cfg.demo_number}")

    preflight(cfg)
    release_previous_session(cfg)
    session, token = demo_start(cfg)
    pair_test_number(cfg, session, token)
    heartbeat_stop = threading.Event()
    start_heartbeat(cfg, session, heartbeat_stop)

    ctx = {}
    try:
        for i, mission in enumerate(scenario['missions'], start=1):
            result = run_mission(cfg, scenario['name'], mission)
            ctx[f'verdict{i}'] = result['verdict']
            ctx[f'report{i}'] = result['report']
            ctx.setdefault('runs', []).append(
                {k: result[k] for k in ('run_id', 'bot_leg_id', 'since')})
    finally:
        heartbeat_stop.set()
        for leg in list(cfg.active_legs):
            end_call_leg(cfg, leg)

    results = run_assertions(scenario, ctx)

    print()
    print("=" * 72)
    print(f"RESULTS: {scenario['name']}")
    print("=" * 72)
    hard_failures = 0
    for r in results:
        if r['passed']:
            status = 'PASS'
        elif r['level'] == 'soft':
            status = 'soft-FAIL'
        else:
            status = 'FAIL'
            hard_failures += 1
        print(f"  [{status:>9}] {r['label']}")
        if not r['passed']:
            print(f"             target={r['target']} actual={r['actual']!r}")
    print("=" * 72)

    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    out_dir = RESULTS_DIR / f"{stamp}-{scenario['name']}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'run.json').write_text(json.dumps({
        'scenario': scenario['name'],
        'started': stamp,
        'test_number': cfg.test_number,
        'assertions': results,
        'context': ctx,
    }, indent=2, default=str), encoding='utf-8')
    log(f"artifacts: {out_dir}")

    if args.release:
        release_previous_session(cfg)
        log("demo workspace released")
    else:
        log("demo workspace kept (inspect the dashboard; next run cleans up)")

    verdict = 'PASS' if hard_failures == 0 else f"FAIL ({hard_failures} hard)"
    log(f"scenario outcome: {verdict}")
    sys.exit(0 if hard_failures == 0 else 1)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
