"""
Agent Runner v3 — Global concurrency guard prevents OOM.
Max 6 concurrent claude processes across entire system.
Uses --system-prompt-file, stdin pipe, bypassPermissions.
"""
# Adapted from GrowthMastery-ai/gm-agent-core for Larry-Yatch/ourliberty-agent-core (2026-05-08)

import subprocess
import os
import re
import shutil
import sys
import json
import time
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))
# Removed: was importing GM-specific token_manager (multi-account OAuth pool).
# Stub with single-account no-op rate-limit semantics. Larry uses one Claude
# Max OAuth on the droplet; rate-limit detection becomes meaningful when/if
# a dedicated agent-only Max is added (Phase F+).
def get_manager():
    class _StubTokenManager:
        def get_token(self):
            return os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', ''), 'oauth'
        def check_for_rate_limit(self, _output):
            # No multi-account pool to cool down; treat all output as
            # non-rate-limited. Real claude errors (5xx, transient) still
            # trigger run_claude's retry path via the returncode!=0 branch.
            return False
        def detect_cap_in_output(self, _output):
            return False
        def report_rate_limit(self, _account_id, cooldown_seconds=300):
            return None
        def report_success(self, _account_id):
            return None
    return _StubTokenManager()
from concurrency_guard import get_guard

AGENTS_ROOT = Path.home() / 'agents'


def resolve_log_dir():
    """Return the directory this module writes per-agent log files to.

    OURLIBERTY_LOG_DIR override exists so test runs do not leak sentinel
    strings into the live agent logs (e.g., ~/agents/logs/forge.log) when
    a test imports this module and triggers log() via mocked Popen flows.
    Production keeps the env var unset, preserving the historical path.
    """
    override = os.environ.get('OURLIBERTY_LOG_DIR')
    return Path(override) if override else AGENTS_ROOT / 'logs'


LOG_DIR = resolve_log_dir()
AGENT_MODELS_FILE = AGENTS_ROOT / 'config' / 'agent-models.json'

def get_agent_model(agent_id, context='default'):
    """
    Get model for an agent based on context.
    context='telegram' uses the chat model (fast, token-efficient).
    context='inbox' or 'default' uses the work model (deep thinking).
    """
    try:
        with open(AGENT_MODELS_FILE) as f:
            config = json.load(f)
        agent_cfg = config.get('agents', {}).get(agent_id, {})
        if context == 'telegram':
            return agent_cfg.get('telegram_model', agent_cfg.get('model', 'sonnet')), agent_cfg.get('fallback_model', 'sonnet')
        else:
            return agent_cfg.get('inbox_model', agent_cfg.get('model', 'sonnet')), agent_cfg.get('fallback_model', 'sonnet')
    except:
        return 'sonnet', 'sonnet'

MAX_RETRIES = 5
# Exponential backoff base. Attempt N waits min(BASE * 2**N, RETRY_DELAY_MAX)
# before retrying: 10 → 20 → 40 → 80 → 160s (total max patience ~5min before
# "All retries exhausted"). Was fixed 10s × 3 = 30s — often not enough for a
# 300s cooldown to expire even once. See pipeline audit 2026-04-14.
RETRY_BASE_DELAY = 10
RETRY_DELAY_MAX = 160
# Back-compat alias for any external code reading the old constant.
RETRY_DELAY = RETRY_BASE_DELAY

# Tier 2 fallback (claude-quota-tier2-fallback-wrapper, 2026-05-26).
# When Tier 1 (the agent's primary Claude Max account) hits rate-limit OR
# auth-401, retry once with HOME swapped to Larry's personal Claude Max
# OAuth dir. Separate accounts = separate quota + auth buckets.
# Resume-discipline rule: --resume session IDs are NOT portable between
# accounts. If session_id is set, we DM Larry instead of attempting a
# silent Tier 2 retry that would fail with 'session not found'.
TIER2_HOME = '/home/larry/.claude-larry-personal'

RATE_LIMIT_RE = re.compile(
    r'(hit your limit|5-hour|resets \d+)', re.IGNORECASE,
)
AUTH_401_RE = re.compile(
    r'(401|Invalid authentication credentials|Failed to authenticate)',
    re.IGNORECASE,
)


def classify_tier1_failure(stdout, stderr):
    """Return 'rate_limit', 'auth_401', or None.

    Detection runs against the combined stdout+stderr (the Claude CLI emits
    rate-limit AND auth-401 messages on stdout — that's the 2026-05-26 gap).
    Rate-limit takes precedence when both regexes match; the recovery is the
    same either way (Tier 2 retry).
    """
    combined = (stdout or '') + '\n' + (stderr or '')
    if RATE_LIMIT_RE.search(combined):
        return 'rate_limit'
    if AUTH_401_RE.search(combined):
        return 'auth_401'
    return None


def tier2_available():
    """True iff /home/larry/.claude-larry-personal/.claude/.credentials.json
    exists. Checked BEFORE swapping HOME so a missing Tier 2 setup DMs Larry
    rather than producing a confusing claude-no-credentials failure."""
    return Path(TIER2_HOME, '.claude', '.credentials.json').exists()


def _mark_paused_on_tier1(task_stem, failure_type):
    """Write a sentinel in the in-flight state file so heal_pipeline_stall's
    Check 8 can detect resume-tasks paused on Tier 1 quota/auth failure.

    Failure is non-fatal; the DM via larry_alerts is the primary signal
    (this is defense-in-depth state for the healer to surface if Larry
    misses the DM)."""
    if not task_stem:
        return
    try:
        IN_FLIGHT_DIR.mkdir(parents=True, exist_ok=True)
        target = IN_FLIGHT_DIR / f'{task_stem}.json'
        try:
            data = json.loads(target.read_text()) if target.exists() else {}
        except (OSError, json.JSONDecodeError):
            data = {}
        data['paused_on_tier1'] = {
            'failure_type': failure_type,
            'at': datetime.now(timezone.utc).isoformat(),
        }
        target.write_text(json.dumps(data, indent=2))
    except OSError:
        pass


def _dm_tier2_unavailable(failure_type, task_stem, agent_id, session_id):
    """DM Larry that Tier 1 failed and Tier 2 was unavailable / also failed
    OR the session was a --resume that can't fall back. Uses larry_alerts
    with the existing 'warning' severity; subject buckets on intent +
    failure_type so different failure types get distinct cooldown windows.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import larry_alerts as la  # noqa: E402
    except Exception:
        return
    if failure_type == 'rate_limit':
        recovery = (
            'Rate-limit: wait for reset (~5h) OR provision Tier 2 per '
            'docs/runbooks/restore-larry-personal-claude-oauth-tier2.md.'
        )
    elif failure_type == 'auth_401':
        recovery = (
            'Auth-401: run /tmp/auth_orchestrator.py from chat to '
            'headless-re-auth Tier 1. Runbook: '
            'docs/runbooks/restore-larry-personal-claude-oauth-tier2.md.'
        )
    else:
        recovery = (
            'Investigate the agent_runner log for the task and recover '
            'manually.'
        )
    task_label = task_stem or 'unknown-task'
    resume_note = ''
    if session_id:
        resume_note = (
            ' Resume-mode task — cannot fall back to Tier 2 mid-session '
            '(session IDs are account-bound). Marked paused_on_tier1 in '
            'the in-flight state file.'
        )
    try:
        la.append_alert(
            source=f'agent-runner-{agent_id}',
            severity='warning',
            message=(
                f'Task `{task_label}` ({agent_id}) hit Tier 1 {failure_type} '
                f'and Tier 2 fallback was unavailable, failed, or skipped.'
                f'{resume_note}'
            ),
            subject=f'claude_tier1_failed_tier2_unavailable:{failure_type}',
            suggested_action=recovery,
        )
    except Exception:
        pass


def _build_cmd_for_tier(base_cmd, model, fallback, session_id):
    """Return a fresh command list for a Tier 2 retry. Keeps everything
    identical to the Tier 1 invocation EXCEPT we never thread --resume on
    a Tier 2 retry — the caller has already vetted that no session_id was
    set before reaching this helper, so the absence is structural."""
    return list(base_cmd)


def _retry_delay(attempt):
    """Exponential backoff with cap: 10 → 20 → 40 → 80 → 160 (s)."""
    return min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_DELAY_MAX)


def log(agent_id, message, level='INFO'):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    entry = '[' + ts + '] [' + agent_id + '] [' + level + '] ' + message
    print(entry)
    # Re-resolve at write time so OURLIBERTY_LOG_DIR set after import (e.g.,
    # by the autouse test fixture in scripts/tests/conftest.py) still
    # redirects writes. In production the env var is unset and the path
    # collapses to LOG_DIR — no behavior change, no extra disk syscall.
    log_file = resolve_log_dir() / (agent_id + '.log')
    with open(log_file, 'a') as f:
        f.write(entry + '\n')


# === Parent-directory CLAUDE.md poison guard (Dispatch 6 — Issue #31) ===
#
# Claude Code auto-loads CLAUDE.md from every parent directory up to /. When
# a worker spawns in /tmp/wt-main-*/, a stray /tmp/CLAUDE.md poisons the
# worker's identity and silently overrides the worktree-local CLAUDE.md.
#
# The 2026-04-15/16 incident: a root-owned /tmp/CLAUDE.md identified itself
# as another agent. Every worker spawned in /tmp/ inherited that wrong
# identity → routing fallbacks all day. The stale file was removed manually,
# but nothing prevented re-pollution — hence this guard, which runs at every
# worker spawn.
#
# Semantics: quarantine-and-continue. The guard never blocks a spawn; it
# moves suspect parent-dir CLAUDE.md files to a quarantine directory with a
# timestamped suffix, logs a WARN, and lets the spawn proceed. The guard is
# idempotent (safe to call repeatedly).
#
# Allowlist: only paths under /home/larry/agents/ are treated as legitimate
# parent roots. The allowlist is intentionally empty of /tmp/ entries; never
# add /tmp/ to it. To restore a quarantined file that WAS legitimate,
# move it back from /var/log/agents/quarantine/ and add the parent
# path to CLAUDE_MD_ALLOWLIST_ROOTS below. See shared/PIPELINE-ROLLBACK.md §8.

QUARANTINE_DIR = Path('/var/log/agents/quarantine')

# Paths under any of these roots are allowed to carry a CLAUDE.md that
# might be inherited by a worker spawned in a descendant directory. The
# set is intentionally minimal; adding entries is a policy decision, not
# a one-off fix. Never whitelist /tmp/.
CLAUDE_MD_ALLOWLIST_ROOTS = (
    Path('/home/larry/agents'),
)


def _is_under_allowlist(parent_path, allowlist_roots):
    """Return True iff parent_path is inside (or equal to) any allowlist root."""
    try:
        parent_resolved = Path(parent_path).resolve()
    except Exception:
        return False
    for root in allowlist_roots:
        try:
            root_resolved = Path(root).resolve()
        except Exception:
            continue
        if parent_resolved == root_resolved:
            return True
        try:
            parent_resolved.relative_to(root_resolved)
            return True
        except ValueError:
            continue
    return False


def quarantine_parent_claude_md_poison(worker_cwd, agent_id=None, log_fn=None,
                                        allowlist_roots=None,
                                        quarantine_dir=None):
    """Scan parent dirs of worker_cwd and quarantine poison CLAUDE.md files.

    Walk every parent directory of `worker_cwd` up to the filesystem root.
    For each parent NOT covered by the allowlist, if that parent contains a
    top-level `CLAUDE.md`, move the file to the quarantine directory with a
    timestamped suffix and log a WARN. The spawn is never blocked —
    exceptions are logged and swallowed.

    Args:
      worker_cwd: absolute path to the worker's cwd at spawn time.
      agent_id: agent id for log attribution (optional).
      log_fn: callable(agent, msg, level) used in place of the module-level
              :func:`log`. Tests must inject a silent/capturing sink so
              synthetic fixtures don't pollute the production log.
      allowlist_roots: iterable of Path roots under which CLAUDE.md is
              legitimate. Defaults to :data:`CLAUDE_MD_ALLOWLIST_ROOTS`.
              Supply extras to allow additional parent roots — never add
              /tmp/.
      quarantine_dir: override for the quarantine directory (tests).

    Returns:
      list[Path]: destinations of quarantined files (empty when no poison
      was found). The list order matches the parent-walk order (closest
      parent first).

    Idempotent: calling twice on the same cwd with no new poison files is
    a no-op.
    """
    logger = log_fn or log
    attribution = agent_id or 'orchestrator'
    roots = tuple(allowlist_roots) if allowlist_roots is not None else CLAUDE_MD_ALLOWLIST_ROOTS
    qdir = Path(quarantine_dir) if quarantine_dir is not None else QUARANTINE_DIR

    quarantined = []

    try:
        cwd_resolved = Path(worker_cwd).resolve()
    except Exception as e:
        try:
            logger(attribution,
                   'parent-claude-md-guard: cannot resolve worker cwd ' +
                   repr(worker_cwd) + ': ' + str(e), 'WARN')
        except Exception:
            pass
        return quarantined

    # Parents only — the worktree-local CLAUDE.md (inside cwd itself) is
    # the legitimate identity file and must not be disturbed.
    try:
        parents = list(cwd_resolved.parents)
    except Exception as e:
        try:
            logger(attribution,
                   'parent-claude-md-guard: cannot enumerate parents of ' +
                   str(cwd_resolved) + ': ' + str(e), 'WARN')
        except Exception:
            pass
        return quarantined

    for parent in parents:
        if _is_under_allowlist(parent, roots):
            continue

        candidate = parent / 'CLAUDE.md'
        try:
            if not candidate.is_file():
                continue
        except Exception:
            continue

        try:
            qdir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(qdir, 0o755)
            except Exception:
                # chmod may fail if we don't own the dir (e.g., created by
                # root earlier). Non-fatal — mkdir(exist_ok=True) already
                # confirmed the path is usable.
                pass

            # Encode the origin path in the filename so operators can
            # reconstruct where a quarantined file came from without
            # grepping logs.
            iso = datetime.now().strftime('%Y%m%dT%H%M%S')
            safe_parent = str(parent).strip('/').replace('/', '_') or 'root'
            dest = qdir / ('CLAUDE-md-' + iso + '-' + safe_parent + '.bak')

            # Collision guard: if two spawns fire in the same second, append
            # a monotonically increasing suffix.
            collision = 0
            while dest.exists():
                collision += 1
                dest = qdir / (
                    'CLAUDE-md-' + iso + '-' + safe_parent +
                    '-' + str(collision) + '.bak')

            shutil.move(str(candidate), str(dest))
            quarantined.append(dest)

            try:
                logger(attribution,
                       'parent-claude-md-guard: quarantined poison ' +
                       str(candidate) + ' -> ' + str(dest) +
                       ' (parent not in allowlist — would have overridden ' +
                       'worktree-local CLAUDE.md)',
                       'WARN')
            except Exception:
                pass
        except Exception as e:
            # Log and continue to the next parent — never block spawn.
            try:
                logger(attribution,
                       'parent-claude-md-guard: failed to quarantine ' +
                       str(candidate) + ': ' + str(e), 'WARN')
            except Exception:
                pass

    return quarantined


# === Identity landmine scrubber (2026-04-16 incident) =====================
# Root cause: Claude Code CLI walks UP from cwd looking for CLAUDE.md /
# AGENTS.md / IDENTITY.md to load as project context. Worktree cwds live at
# /tmp/wt-<agent>-<stem>-<ts>/, so the walk reaches /tmp/ and picks up any
# top-level file named CLAUDE.md (etc.) there. On 2026-04-16 a stale
# /tmp/CLAUDE.md (placed weeks earlier by a manual `sudo cp`) was loading
# Prism's identity into every `main`/Luma subprocess, causing 4 consecutive
# deterministic mis-routes (dispatches self-refused as Prism).
#
# Scope rule: scrub ONLY /tmp top-level identity files. Never touch
# /home/larry/agents/agents/<agent>/workspace/CLAUDE.md (legitimate
# per-agent identity) or any nested CLAUDE.md in project repos.
_IDENTITY_LANDMINE_NAMES = ('CLAUDE.md', 'AGENTS.md', 'IDENTITY.md')


def scrub_tmp_identity_landmines(tmp_root=None, log_fn=None):
    """Remove any top-level /tmp/CLAUDE.md, /tmp/AGENTS.md, /tmp/IDENTITY.md.

    Called on every task spawn as a belt alongside the daily cron suspenders.
    Deletion (not just logging) is intentional — landmines actively poison
    every spawn, leaving them in place with a warning is strictly worse.

    Logs at WARN when a landmine is evicted (visible to watchdogs, not noisy).
    Logs at ERROR on PermissionError — silent failure here would re-create
    the original bug.

    Parameters
    ----------
    tmp_root : Path | None
        Override the scrub root (test hook). Defaults to /tmp. Must be a
        pathlib.Path when supplied.
    log_fn : callable | None
        Override the logging sink (test hook). Defaults to module-level
        ``log``. Signature is ``log_fn(agent_id, message, level='INFO')``.

    Returns
    -------
    list[tuple[str, str, str]]
        One (path, action, note) tuple per file actually inspected. Action
        is one of REMOVED / WARN / STUCK. Empty list when /tmp is already
        clean, which is the common case.
    """
    root = tmp_root if tmp_root is not None else Path('/tmp')
    emit = log_fn if log_fn is not None else log
    reports = []
    for name in _IDENTITY_LANDMINE_NAMES:
        target = root / name
        try:
            if target.is_file() or target.is_symlink():
                target.unlink()
                reports.append((str(target), 'REMOVED',
                                'stale auto-loaded memory landmine'))
                emit('orchestrator',
                     'IDENTITY_LANDMINE_EVICTED: deleted ' + str(target) +
                     ' (prevented cross-agent identity leak)',
                     'WARN')
        except PermissionError as e:
            reports.append((str(target), 'STUCK',
                            'permission denied: ' + str(e)))
            emit('orchestrator',
                 'IDENTITY_LANDMINE_STUCK: cannot delete ' + str(target) +
                 ' (PermissionError: ' + str(e) + ') — next spawn may load wrong identity',
                 'ERROR')
        except Exception as e:
            reports.append((str(target), 'STUCK', str(e)))
            emit('orchestrator',
                 'IDENTITY_LANDMINE_STUCK: unexpected error on ' + str(target) +
                 ': ' + str(e),
                 'ERROR')
    return reports


# === Opt-in identity assertion (defense-in-depth) =========================
# Sets a "bouncer at the door" assertion as a prompt preamble when the task
# envelope carries `expected_agent`. If the subprocess loads a CLAUDE.md
# that does not match the expected identity, it responds with a single
# IDENTITY_MISMATCH line and stops — no rogue work, loud failure.

IDENTITY_ASSERTION_MARKER = "IDENTITY ASSERTION (READ THIS FIRST)"


def build_expected_agent_assertion(expected_agent):
    """Build a prompt preamble that tells the subprocess to verify its
    loaded CLAUDE.md matches `expected_agent` before doing anything.

    Opt-in: only inserted when the caller declares an expected identity.
    Never duplicated (idempotent via marker check in
    `_maybe_prepend_identity_assertion`).
    """
    ea = str(expected_agent).strip().lower()
    return ("=" * 70 + "\n"
            + IDENTITY_ASSERTION_MARKER + "\n"
            + "=" * 70 + "\n\n"
            "This task is routed to the `" + ea + "` agent.\n\n"
            "Before doing ANY work, verify that the CLAUDE.md loaded into your\n"
            "context identifies you as `" + ea + "` (check the H1 heading and\n"
            "the 'You are operating as the **<name>** agent' line).\n\n"
            "If the loaded CLAUDE.md names a DIFFERENT agent, respond with\n"
            "exactly this single line and stop — do not proceed, do not edit\n"
            "any files, do not open any PRs:\n\n"
            "  IDENTITY_MISMATCH: expected=" + ea +
            " loaded=<the agent name you actually see>\n\n"
            "If the loaded CLAUDE.md matches `" + ea + "`, proceed normally.\n"
            + "=" * 70 + "\n\n")


def _maybe_prepend_identity_assertion(prompt, expected_agent, session_id):
    """Return `prompt` with the identity-assertion preamble prepended iff:
      - `expected_agent` is set (caller declared an identity), AND
      - `session_id` is None (we're not resuming a session — the assertion
        was made on the original turn, repeating it on every --resume is
        noise), AND
      - the marker is not already present (idempotent: caller may have
        built the prompt with the preamble already in place).

    Otherwise return `prompt` unchanged.

    E1.2: the gating logic used to live in `inbox_watcher.process_task` and
    in `process_inbox` (still does, for the upstream codepath). Centralizing
    it here lets every `run_claude` caller get the right thing by default
    just by passing `expected_agent`. The watcher no longer needs to
    construct the preamble manually.
    """
    if not expected_agent:
        return prompt
    if session_id:
        return prompt
    if IDENTITY_ASSERTION_MARKER in prompt:
        return prompt
    return build_expected_agent_assertion(expected_agent) + prompt


CANCEL_DIR = AGENTS_ROOT / 'blackboard'
CANCEL_POLL_INTERVAL = 5  # seconds between cancel checks during worker execution

# === RESTART-INDEPENDENT WORKERS ===
# Workers run as detached processes (start_new_session=True) so they survive
# orchestrator restarts. An on-disk in-flight registry tracks running workers
# so the orchestrator can adopt orphans on startup.
IN_FLIGHT_DIR = AGENTS_ROOT / 'state' / 'in-flight'


def _register_in_flight(task_stem, agent_id, pid):
    """Write an in-flight entry so the orchestrator can adopt orphans after restart."""
    IN_FLIGHT_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        'task_stem': task_stem,
        'agent_id': agent_id,
        'pid': pid,
        'started_at': datetime.now(timezone.utc).isoformat(),
    }
    try:
        with open(IN_FLIGHT_DIR / f'{task_stem}.json', 'w') as f:
            json.dump(entry, f, indent=2)
    except OSError:
        pass


def _unregister_in_flight(task_stem):
    """Remove the in-flight entry when a worker completes."""
    try:
        p = IN_FLIGHT_DIR / f'{task_stem}.json'
        if p.exists():
            p.unlink()
    except OSError:
        pass


def _is_task_in_flight(task_stem):
    """Check if a task is already running (disk-backed, survives restarts)."""
    entry_file = IN_FLIGHT_DIR / f'{task_stem}.json'
    if not entry_file.exists():
        return False
    try:
        with open(entry_file) as f:
            entry = json.load(f)
        pid = entry.get('pid')
        if pid:
            # Check if process is still alive
            os.kill(pid, 0)
            return True  # Process alive — task is in flight
    except (ProcessLookupError, PermissionError):
        # Process dead — clean up stale entry
        _unregister_in_flight(task_stem)
        return False
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    return False


def _check_cancel(task_stem):
    """Check if a cancel marker exists for this task. Returns reason or None."""
    cancel_file = CANCEL_DIR / f'cancel-task-{task_stem}.json'
    if cancel_file.exists():
        try:
            with open(cancel_file) as f:
                data = json.load(f)
            return data.get('reason', 'cancelled by request')
        except (OSError, json.JSONDecodeError):
            return 'cancelled (marker found)'
    return None


def _clear_cancel(task_stem):
    """Remove the cancel marker after processing."""
    cancel_file = CANCEL_DIR / f'cancel-task-{task_stem}.json'
    try:
        if cancel_file.exists():
            cancel_file.unlink()
    except OSError:
        pass


def run_claude(agent_id, prompt, working_dir=None, system_prompt=None,
               system_prompt_file=None, timeout=14400, context='default',
               model_override=None, session_id=None, effort='high',
               task_stem=None, out_meta=None, expected_agent=None):
    """
    Run a claude CLI command with concurrency guard + token management.
    Max 6 concurrent across entire system to prevent OOM.

    Supports graceful cancellation: if blackboard/cancel-task-{task_stem}.json
    exists, the worker process is terminated and the task returns as cancelled.
    Any agent can create this file to stop a running task.

    Params:
      session_id: If provided, adds --resume <session_id> to resume a prior session.
      effort: 'low', 'medium', 'high', or 'max'. Sets CLAUDE_CODE_EFFORT_LEVEL env var.
      task_stem: Task filename stem for cancel-marker matching + in-flight registry.
      out_meta: Optional dict. If provided, populated on success with keys:
        cost_usd, usage{input_tokens,output_tokens,cache_read,cache_creation},
        model, account_id, attempts, started_at, completed_at, duration_sec.
        Callers that don't need cost telemetry can ignore this; the function's
        return value is unchanged for back-compat.
      expected_agent: If set, prepends an identity-assertion preamble (defined
        in `build_expected_agent_assertion`) that makes the subprocess refuse
        if its loaded CLAUDE.md names a different agent. Skipped when
        session_id is set (preamble was already there on the original turn)
        or when the marker is already present in `prompt` (idempotent). E1.2
        moved this gating from the caller into run_claude so every caller
        gets the right behavior by passing the parameter.

    Returns: (success: bool, output_text: str, new_session_id: str | None)
    """
    _meta_started_at = datetime.now(timezone.utc).isoformat()
    _meta_t0 = time.time()
    tm = get_manager()
    guard = get_guard()

    # E1.2: opt-in identity-assertion preamble. Centralized here so callers
    # don't have to replicate the gating logic. The helper is a no-op when
    # expected_agent is None.
    prompt = _maybe_prepend_identity_assertion(prompt, expected_agent, session_id)

    # Wait for a concurrency slot (blocks if at capacity)
    if not guard.wait_for_slot(agent_id, timeout=1800):
        log(agent_id, 'Concurrency limit reached (' + str(guard.active_count()) + ' active). Waited 120s.', 'WARN')
        return False, 'Concurrency limit - too many parallel tasks. Try again shortly.', None

    try:
        for attempt in range(MAX_RETRIES):
            token, account_id = tm.get_token()

            env = os.environ.copy()
            env['CLAUDE_CODE_OAUTH_TOKEN'] = token
            env['CLAUDE_CODE_EFFORT_LEVEL'] = effort

            if model_override:
                model, fallback = model_override, 'sonnet'
            else:
                model, fallback = get_agent_model(agent_id, context)
            cmd = ['claude', '-p', '--output-format', 'json',
                   '--model', model,
                   '--permission-mode', 'bypassPermissions']

            # TODO(Larry): wire up per-agent workspace isolation in Phase D.
            # Upstream had a Mula-specific isolation block here; for now every
            # agent gets the standard agents-root mount and Phase D will narrow
            # individual agents (e.g. aide for executive-assistant work) once
            # their permission boundaries are decided.
            cmd.extend(['--add-dir', '/home/larry/agents'])
            if fallback and fallback != model:
                cmd.extend(['--fallback-model', fallback])
            if session_id:
                cmd.extend(['--resume', session_id])

            if system_prompt_file and Path(system_prompt_file).exists():
                cmd.extend(['--system-prompt-file', system_prompt_file])
            elif system_prompt:
                tmp_file = AGENTS_ROOT / 'logs' / ('tmp-sysprompt-' + agent_id + '.txt')
                with open(tmp_file, 'w') as f:
                    f.write(system_prompt)
                cmd.extend(['--system-prompt-file', str(tmp_file)])

            cwd = working_dir or str(AGENTS_ROOT / 'agents' / agent_id / 'workspace')

            # Parent-pollution guard (Dispatch 6 / Issue #31): before Claude
            # inherits CLAUDE.md from ancestor directories, quarantine any
            # non-allowlisted parent-dir CLAUDE.md so it can't override the
            # worktree-local identity file. Never blocks the spawn.
            try:
                quarantine_parent_claude_md_poison(cwd, agent_id=agent_id)
            except Exception as _guard_exc:
                log(agent_id,
                    'parent-claude-md-guard raised (proceeding with spawn): ' +
                    str(_guard_exc), 'WARN')

            log(agent_id, 'Running (model=' + model +
                ', account=' + account_id +
                ', attempt=' + str(attempt+1) + '/' + str(MAX_RETRIES) +
                ', active=' + str(guard.active_count()) + '/10' +
                (', resume=' + session_id[:12] + '...' if session_id else '') +
                ', effort=' + effort + ')')

            # Belt: scrub /tmp identity landmines before every subprocess
            # spawn. Suspenders (the daily 03:00 UTC cron) handles the
            # bulk-clearing case; this guarantees a single landmine can't
            # poison more than one task even between cron runs.
            scrub_tmp_identity_landmines()

            try:
                # Use Popen + polling for cancel-marker support.
                # If task_stem is set, we check for a cancel file every
                # CANCEL_POLL_INTERVAL seconds. If found, SIGTERM the worker.
                # start_new_session=True detaches the worker from the
                # orchestrator's process group. If the orchestrator restarts,
                # the claude process keeps running. The in-flight registry
                # on disk lets the new orchestrator adopt the orphan.
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                    cwd=cwd,
                    start_new_session=True,
                )
                # Register in-flight for restart-safe dedup + orphan adoption
                if task_stem:
                    _register_in_flight(task_stem, agent_id, proc.pid)

                # Write prompt to stdin then close
                try:
                    proc.stdin.write(prompt)
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass

                # Poll for completion + cancel check
                effective_timeout = timeout if timeout > 0 else 14400
                elapsed = 0
                cancelled = False
                while proc.poll() is None:
                    time.sleep(CANCEL_POLL_INTERVAL)
                    elapsed += CANCEL_POLL_INTERVAL
                    # Cancel check
                    if task_stem:
                        cancel_reason = _check_cancel(task_stem)
                        if cancel_reason:
                            log(agent_id, f'TASK_CANCELLED: {task_stem} — {cancel_reason}. Sending SIGTERM.', 'WARN')
                            proc.terminate()
                            try:
                                proc.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                log(agent_id, f'SIGTERM did not stop {task_stem} — sending SIGKILL', 'WARN')
                                proc.kill()
                                proc.wait()
                            _clear_cancel(task_stem)
                            cancelled = True
                            break
                    # Timeout check
                    if elapsed >= effective_timeout:
                        log(agent_id, 'Timeout after ' + str(timeout) + 's', 'ERROR')
                        proc.terminate()
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait()
                        break

                stdout_text = proc.stdout.read() if proc.stdout else ''
                stderr_text = proc.stderr.read() if proc.stderr else ''

                # Clean up in-flight registry regardless of outcome
                if task_stem:
                    _unregister_in_flight(task_stem)

                if cancelled:
                    guard.release(agent_id)
                    return False, f'TASK_CANCELLED: {cancel_reason}', None

                # Build a result-like object for downstream compatibility
                class _Result:
                    def __init__(self, rc, out, err):
                        self.returncode = rc
                        self.stdout = out
                        self.stderr = err
                result = _Result(proc.returncode, stdout_text, stderr_text)
                output = result.stdout + result.stderr

                # === Tier 2 fallback (2026-05-26) =========================
                # Both rate-limit AND auth-401 messages from the Claude CLI
                # go to stdout. The legacy stderr-only log line dropped them
                # silently — today's OAuth expiry was misdiagnosed as rate
                # limit because of this. Log BOTH streams on non-zero exit
                # and detect from the combined output.
                if result.returncode != 0:
                    failure_type = classify_tier1_failure(
                        result.stdout, result.stderr,
                    )
                    if failure_type:
                        log(agent_id,
                            'TIER1_FAILURE_DETECTED type=' + failure_type +
                            ' stdout=' + repr((result.stdout or '')[:300]) +
                            ' stderr=' + repr((result.stderr or '')[:300]),
                            'WARN')
                        # Resume-discipline rule: --resume session IDs are
                        # NOT portable between accounts. A Tier 2 retry on a
                        # resume task would fail with 'session not found'
                        # AND would orphan the original session's context.
                        # DM Larry + mark paused; the next retry would hit
                        # the same wall, so exit the loop terminally.
                        if session_id:
                            log(agent_id,
                                'TIER2_FALLBACK_SKIPPED reason=' + failure_type +
                                ' cause=resume_session_account_bound',
                                'WARN')
                            _mark_paused_on_tier1(task_stem, failure_type)
                            _dm_tier2_unavailable(
                                failure_type, task_stem, agent_id, session_id,
                            )
                            return (False,
                                    'Tier 1 ' + failure_type +
                                    ' on --resume session; cannot fall back '
                                    'to Tier 2 (session is account-bound). '
                                    'DM sent.',
                                    None)
                        if not tier2_available():
                            log(agent_id,
                                'TIER2_FALLBACK_UNAVAILABLE reason=' +
                                failure_type + ' home=' + TIER2_HOME +
                                ' (missing credentials file)',
                                'WARN')
                            _dm_tier2_unavailable(
                                failure_type, task_stem, agent_id, None,
                            )
                            # Fall through to existing retry behavior — a
                            # transient rate-limit might clear on its own,
                            # though auth-401 will keep failing the same way.
                        else:
                            log(agent_id,
                                'TIER2_FALLBACK_ATTEMPT reason=' +
                                failure_type + ' home=' + TIER2_HOME,
                                'INFO')
                            t2_env = dict(env)
                            t2_env['HOME'] = TIER2_HOME
                            t2_cmd = _build_cmd_for_tier(
                                cmd, model, fallback, session_id,
                            )
                            try:
                                t2 = subprocess.run(
                                    t2_cmd,
                                    input=prompt,
                                    capture_output=True,
                                    text=True,
                                    env=t2_env,
                                    cwd=cwd,
                                    timeout=min(timeout if timeout > 0 else 14400, 1800),
                                )
                                if t2.returncode == 0:
                                    log(agent_id,
                                        'TIER2_FALLBACK_USED reason=' +
                                        failure_type, 'INFO')
                                    # Replace the result for downstream JSON
                                    # parsing — the happy path takes over
                                    # from here.
                                    result = _Result(
                                        t2.returncode, t2.stdout, t2.stderr,
                                    )
                                    output = result.stdout + result.stderr
                                else:
                                    log(agent_id,
                                        'TIER2_FALLBACK_FAILED reason=' +
                                        failure_type + ' exit=' +
                                        str(t2.returncode), 'WARN')
                                    _dm_tier2_unavailable(
                                        failure_type, task_stem, agent_id, None,
                                    )
                            except (subprocess.TimeoutExpired,
                                    FileNotFoundError, OSError) as t2_exc:
                                log(agent_id,
                                    'TIER2_FALLBACK_FAILED reason=' +
                                    failure_type + ' exc=' +
                                    type(t2_exc).__name__ + ': ' +
                                    str(t2_exc), 'WARN')
                                _dm_tier2_unavailable(
                                    failure_type, task_stem, agent_id, None,
                                )

                if tm.check_for_rate_limit(output):
                    # Distinguish usage-cap (hours until reset) from transient
                    # 429 (seconds-to-minutes until reset). Usage caps get a
                    # longer cooldown to prevent thrashing between accounts.
                    is_cap = tm.detect_cap_in_output(output)
                    cooldown = 3600 if is_cap else 300  # LONG vs SHORT
                    suffix = ' (usage cap → 1h cooldown)' if is_cap else ''
                    log(agent_id, 'Rate limit on [' + account_id + ']' + suffix, 'WARN')
                    tm.report_rate_limit(account_id, cooldown_seconds=cooldown)
                    time.sleep(_retry_delay(attempt))
                    continue

                # Parse JSON response to extract result text and session_id
                new_session_id = None
                output_text = ''
                try:
                    response = json.loads(result.stdout)
                    output_text = response.get('result', '')
                    new_session_id = response.get('session_id')
                    is_error = response.get('is_error', False)

                    # Defensive: if 'result' field is missing (known bug #36811
                    # with --resume + --output-format json), log a warning.
                    # The task still succeeds but the output is empty.
                    if 'result' not in response and result.returncode == 0:
                        log(agent_id, 'JSON response missing result field (bug #36811?) — keys: ' +
                            str(list(response.keys())), 'WARN')

                    if result.returncode == 0 and not is_error:
                        tm.report_success(account_id)
                        cost = response.get('total_cost_usd')
                        cost_str = ', $' + f'{cost:.4f}' if cost else ''
                        log(agent_id, 'Completed successfully (account=' + account_id +
                            (', sid=' + new_session_id[:12] + '...' if new_session_id else '') +
                            cost_str + ')')
                        if out_meta is not None:
                            usage = response.get('usage') or {}
                            out_meta['cost_usd'] = cost
                            out_meta['usage'] = {
                                'input_tokens': usage.get('input_tokens'),
                                'output_tokens': usage.get('output_tokens'),
                                'cache_read': usage.get('cache_read_input_tokens'),
                                'cache_creation': usage.get('cache_creation_input_tokens'),
                            }
                            out_meta['model'] = model
                            out_meta['account_id'] = account_id
                            out_meta['attempts'] = attempt + 1
                            out_meta['started_at'] = _meta_started_at
                            out_meta['completed_at'] = datetime.now(timezone.utc).isoformat()
                            out_meta['duration_sec'] = round(time.time() - _meta_t0, 2)
                        return True, output_text, new_session_id

                    if is_error:
                        log(agent_id, 'Claude returned is_error: ' + output_text[:300], 'WARN')

                except json.JSONDecodeError:
                    # Fallback: raw text (Claude crashed or wrote partial output)
                    output_text = result.stdout.strip() if result.stdout else ''
                    log(agent_id, 'Non-JSON output, falling back to raw text', 'WARN')

                    if result.returncode == 0:
                        tm.report_success(account_id)
                        return True, output_text, None

                log(agent_id,
                    'Non-zero exit (' + str(result.returncode) +
                    '): stdout=' + repr((result.stdout or '')[:500]) +
                    ' stderr=' + repr((result.stderr or '')[:500]),
                    'WARN')
                if attempt < MAX_RETRIES - 1:
                    time.sleep(_retry_delay(attempt))
                continue

            except subprocess.TimeoutExpired:
                log(agent_id, 'Timeout after ' + str(timeout) + 's', 'ERROR')
                return False, 'Timeout', None

            except Exception as e:
                log(agent_id, 'Exception: ' + str(e), 'ERROR')
                if attempt < MAX_RETRIES - 1:
                    time.sleep(_retry_delay(attempt))
                continue

        log(agent_id, 'All retries exhausted', 'ERROR')
        return False, 'All retries exhausted', None

    finally:
        guard.release(agent_id)




def create_worktree_for_task(agent_id, task_stem):
    """
    Create a fresh git worktree for a task. Returns the worktree path or None on failure.

    Each task runs in an isolated checkout from origin/main so concurrent tasks
    can't collide on file edits, git branches, or workspace state.

    The worktree is preserved after task completion for 24 hours (for debugging),
    then cleaned up by cleanup_stale_worktrees.py.
    """
    # TODO(Larry): Phase D wires up which repo lives in each agent's workspace.
    # For now use a generic 'repo' subdir; orchestrator can override at dispatch.
    repo_dir = AGENTS_ROOT / 'agents' / agent_id / 'workspace' / 'repo'
    if not repo_dir.exists():
        log(agent_id, 'Cannot create worktree: repo not found at ' + str(repo_dir), 'WARN')
        return None

    # Sanitize task stem for path safety
    safe_stem = ''.join(c if (c.isalnum() or c in '-_') else '-' for c in task_stem)[:50]
    timestamp = int(time.time())
    worktree_path = Path('/tmp') / ('wt-' + agent_id + '-' + safe_stem + '-' + str(timestamp))

    # Pull latest from origin/main first so the worktree starts on the freshest base
    try:
        subprocess.run(
            ['git', 'fetch', 'origin', 'main'],
            cwd=str(repo_dir),
            check=True, capture_output=True, text=True, timeout=180,
        )
    except Exception as e:
        log(agent_id, 'git fetch warning before worktree create: ' + str(e)[:200], 'WARN')

    # Create the worktree on a detached HEAD pointing at origin/main.
    # Luma can `git checkout -b <branch-name>` inside the worktree to start her work.
    try:
        result = subprocess.run(
            ['git', 'worktree', 'add', '--detach', str(worktree_path), 'origin/main'],
            cwd=str(repo_dir),
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0:
            log(agent_id, 'git worktree add failed: ' + result.stderr[:300], 'ERROR')
            return None
        log(agent_id, 'Created worktree: ' + str(worktree_path))
        return str(worktree_path)
    except Exception as e:
        log(agent_id, 'worktree creation exception: ' + str(e), 'ERROR')
        return None


# Stable marker used to detect whether a prompt already carries the worktree
# preamble. Kept short and unique so we can search with `in` cheaply.
WORKTREE_PREAMBLE_MARKER = "WORKTREE ISOLATION (READ THIS FIRST)"

# ============================================================================
# LAYER 4 ARCHITECTURAL ENFORCEMENT (iter 26 — Larry directive 2026-05-01):
# Before spawning Luma's claude, pre-create the branch on origin so that:
#  - If Luma's session times out at 50 min before committing, the branch + WIP
#    checkpoint already exist on origin = next dispatch resumes from there
#  - Prompt-level "FIRST ACTION" instructions to Luma (iter 21 strengthening)
#    are no longer the only enforcement — orchestrator does it before claude
#    even starts. Architectural >> prompt-begging.
# Failure is non-fatal (warns + continues) so a transient git error never
# blocks a dispatch.
# ============================================================================

def setup_branch_checkpoint(worktree_path, agent_id, prompt, task_stem):
    """Pre-create the branch on origin with an empty WIP commit.

    Extracts `Branch hint: \`<branch>\`` from the prompt. If found, runs in
    the worktree: checkout -b <branch>; commit --allow-empty; push -u origin.
    Returns the branch name on success, None on no-hint or failure.

    Never raises — failures log WARN and return None so the spawn proceeds.
    """
    import re
    import subprocess as _sp

    if not worktree_path:
        return None

    m = re.search(r"Branch hint:\s*`([^`]+)`", prompt or "")
    if not m:
        log(agent_id, 'setup_branch_checkpoint: no Branch hint in prompt — skipping', 'INFO')
        return None
    branch = m.group(1).strip()
    if not branch or len(branch) > 200:
        log(agent_id, f'setup_branch_checkpoint: bad branch name {branch!r}', 'WARN')
        return None

    safe_stem = (task_stem or 'task')[:60]
    commit_msg = f'[WIP][session-start] {safe_stem}'

    try:
        # 1. Create + switch to branch (or switch if already exists locally)
        r1 = _sp.run(
            ['git', 'checkout', '-B', branch],
            cwd=worktree_path, capture_output=True, text=True, timeout=60,
        )
        if r1.returncode != 0:
            log(agent_id, f'setup_branch_checkpoint: checkout -B failed: {r1.stderr[:200]}', 'WARN')
            return None

        # 2. Empty WIP commit — checkpoint exists even if claude exits early
        r2 = _sp.run(
            ['git', 'commit', '--allow-empty', '-m', commit_msg],
            cwd=worktree_path, capture_output=True, text=True, timeout=60,
        )
        # Non-fatal if "nothing to commit" (branch existed and was clean)
        if r2.returncode != 0 and 'nothing to commit' not in (r2.stdout + r2.stderr).lower():
            log(agent_id, f'setup_branch_checkpoint: empty commit failed: {r2.stderr[:200]}', 'WARN')

        # 3. Push to origin — establishes branch on remote (force needed if
        #    branch existed remotely from a prior dispatch; our WIP commit
        #    is at the head of origin/main + 1 so push should be fast-forward
        #    in clean cases, but use -u and accept failure non-fatally).
        r3 = _sp.run(
            ['git', 'push', '-u', 'origin', branch],
            cwd=worktree_path, capture_output=True, text=True, timeout=120,
        )
        if r3.returncode != 0:
            # Try with --force-with-lease as a fallback for branches that
            # diverged from prior dispatches (safer than --force).
            r3b = _sp.run(
                ['git', 'push', '-u', '--force-with-lease', 'origin', branch],
                cwd=worktree_path, capture_output=True, text=True, timeout=120,
            )
            if r3b.returncode != 0:
                log(agent_id, f'setup_branch_checkpoint: push failed: {r3.stderr[:200]} | force-with-lease: {r3b.stderr[:200]}', 'WARN')
                return None

        log(agent_id, f'setup_branch_checkpoint: pushed {branch} with WIP checkpoint')
        return branch
    except Exception as e:
        log(agent_id, f'setup_branch_checkpoint: exception {e}', 'WARN')
        return None


# Defensive cap on total prompt size (preamble + task body) before the task
# body is appended. If we ever exceed this on re-prepend attempts it means a
# retry loop has accumulated preambles — short-circuit to the first occurrence.
WORKTREE_PREAMBLE_MAX_BYTES = 200_000


def _build_worktree_preamble_body(worktree_path):
    return ("=" * 70 + "\n"
            + WORKTREE_PREAMBLE_MARKER + "\n"
            + "=" * 70 + "\n\n"
            "Your working directory is: " + worktree_path + "\n\n"
            "This is an isolated git worktree on a fresh checkout of origin/main.\n"
            "Other parallel tasks are running in their own separate worktrees, so you\n"
            "will NEVER collide with them on file edits or git state.\n\n"
            "RULES:\n"
            "1. STAY in your worktree (" + worktree_path + ") for ALL git operations.\n"
            "2. DO NOT cd to the agent's shared workspace under\n"
            "   /home/larry/agents/agents/<agent>/workspace — that is the SHARED workspace\n"
            "   and is used by the orchestrator only. Touching it will cause merge\n"
            "   conflicts with other agents.\n"
            "3. To start a new feature branch:  git checkout -b forge/feature-name\n"
            "4. To resume work on an existing PR branch (e.g. addressing review feedback):\n"
            "   git fetch origin && git checkout forge/existing-branch\n"
            "5. After your task completes, this worktree will be retained for 24 hours\n"
            "   for debugging, then cleaned up automatically.\n"
            "6. Use pnpm typecheck && pnpm lint && git push (or the project's equivalent),\n"
            "   then let CI / preview deploys handle the heavy build.\n\n"
            + "=" * 70 + "\n\n")


def prepend_worktree_preamble(worktree_path, existing_prompt, agent_id=None,
                              log_fn=None):
    """Prepend the worktree-isolation preamble to existing_prompt exactly once.

    Idempotency: if the preamble marker is already present in existing_prompt,
    return the prompt unchanged. This prevents the Bug B scenario documented
    in Dispatch 4 (2026-04-15) where retry/requeue paths re-prepended the
    preamble on every iteration, accumulating ~80 copies in a single prompt.

    Defensive cap: if the incoming prompt is already larger than
    WORKTREE_PREAMBLE_MAX_BYTES, HEADER_BLOAT is logged and the prompt is
    truncated to everything from the first preamble marker onward (one
    preamble + original task body). This is an emergency escape hatch —
    normal operation should never trigger it.

    log_fn: optional callable(agent_id, message, level) used in place of the
    module-level :func:`log` when HEADER_BLOAT fires. Tests must pass a
    silent callback so synthetic bloat fixtures never pollute the production
    orchestrator log (the Dispatch 5 root-cause — every smoke run of
    ``test_bloat_guard_truncates`` was writing a real HEADER_BLOAT line to
    /home/larry/agents/logs/orchestrator.log, which watchdogs then surfaced
    as "multiple bloat events per hour"). Production callers should leave
    this as None to get the normal log path.
    """
    existing_prompt = existing_prompt or ""
    if WORKTREE_PREAMBLE_MARKER in existing_prompt:
        # Already has a preamble — be idempotent.
        if len(existing_prompt) > WORKTREE_PREAMBLE_MAX_BYTES:
            # Strip ALL duplicate preamble blocks (between the first `=`*70
            # line preceding any marker and the `=`*70 closing line after the
            # last marker), preserve whatever content sits outside that span,
            # and prepend exactly ONE fresh preamble. This ensures the result
            # has a single header regardless of how bloated the input was.
            first_marker = existing_prompt.find(WORKTREE_PREAMBLE_MARKER)
            first_header_start = existing_prompt.rfind("=" * 70, 0, first_marker)
            if first_header_start < 0:
                first_header_start = first_marker
            last_marker = existing_prompt.rfind(WORKTREE_PREAMBLE_MARKER)
            last_close_start = existing_prompt.find("=" * 70, last_marker)
            last_close_end = (last_close_start + 70) if last_close_start >= 0 else (
                last_marker + len(WORKTREE_PREAMBLE_MARKER))
            preserved = (existing_prompt[:first_header_start]
                         + existing_prompt[last_close_end:]).lstrip('\n ').rstrip()
            truncated = _build_worktree_preamble_body(worktree_path) + (
                (preserved + '\n') if preserved else '')
            try:
                (log_fn or log)(
                    agent_id or 'orchestrator',
                    'HEADER_BLOAT: prompt exceeded ' + str(WORKTREE_PREAMBLE_MAX_BYTES) +
                    ' bytes with duplicate worktree preambles; truncated to one header '
                    '(was=' + str(len(existing_prompt)) + 'B, now=' + str(len(truncated)) + 'B)',
                    'ERROR')
            except Exception:
                pass
            return truncated
        return existing_prompt
    return _build_worktree_preamble_body(worktree_path) + existing_prompt


def build_worktree_prompt_preamble(worktree_path, existing_prompt=None,
                                   agent_id=None, log_fn=None):
    """Return the worktree-isolation preamble block.

    Historically this returned the raw preamble body only, and callers
    concatenated it to the task prompt themselves (opening the door to
    uncontrolled duplication on retry/resume paths). Dispatch 5 hardens this
    shim so that when an ``existing_prompt`` is supplied, the call is
    equivalent to :func:`prepend_worktree_preamble` — guaranteeing the
    returned string contains exactly one preamble regardless of the caller's
    discipline. Callers that want just the raw body (no idempotency) can
    still omit ``existing_prompt`` and get the pre-Dispatch-5 behavior.

    Prefer :func:`prepend_worktree_preamble` at every call site — it is the
    explicit, documented entry point. This function exists for backwards
    compatibility and as a defense-in-depth net for any future caller that
    forgets which API enforces idempotency.
    """
    if existing_prompt is None:
        return _build_worktree_preamble_body(worktree_path)
    return prepend_worktree_preamble(
        worktree_path, existing_prompt, agent_id=agent_id, log_fn=log_fn)



# === Lesson injection from sweep ledgers ===

SWEEP_LEDGERS_DIR = AGENTS_ROOT / 'shared' / 'sweep-ledgers'

# Feature keywords → ledger file mapping. Detect feature from task prompt.
FEATURE_KEYWORDS = {
    'academy': 'academy',
    'discovery': 'discovery',
    'ecosystem': 'discovery',
    'stripe': 'stripe-connect',
    'payment': 'stripe-connect',
    'checkout': 'checkout',
    'compositor': 'checkout',
    'ads manager': 'ads-manager',
    'ads-manager': 'ads-manager',
    'meta ads': 'ads-manager',
    'campaign': 'ads-manager',
    'ai editor': 'ai-editor',
    'ai-editor': 'ai-editor',
    'page editor': 'ai-editor',
    'visual edit': 'ai-editor',
    'fathom': 'fathom-calls',
    'calls': 'fathom-calls',
    'watch room': 'watch-room',
    'watch-room': 'watch-room',
    'registration': 'registration-page',
    'custom domain': 'custom-domains',
    'ssl': 'custom-domains',
    'dns': 'custom-domains',
    'affiliate': 'affiliate',
    'presentation': 'presentations',
    'deck': 'presentations',
    'contact': 'contacts',
    'funnel builder': 'funnel-builder',
    'my funnel': 'funnel-builder',
    'canvas': 'funnel-builder',
    'brand color': 'branding',
}


def extract_lessons_for_prompt(prompt_text):
    """Detect which feature a task is about and return a compact lesson brief.
    Returns empty string if no feature detected or no lessons available."""
    if not prompt_text:
        return ''

    prompt_lower = prompt_text.lower()

    # Find matching features (may match multiple)
    matched_features = set()
    for keyword, feature in FEATURE_KEYWORDS.items():
        if keyword in prompt_lower:
            matched_features.add(feature)

    if not matched_features:
        return ''

    lessons = []
    for feature in matched_features:
        ledger_file = SWEEP_LEDGERS_DIR / (feature + '.json')
        if not ledger_file.exists():
            continue
        try:
            import json as _json
            data = _json.load(open(ledger_file))
            fixes = data.get('fixes', {})
            for slug, value in fixes.items():
                if isinstance(value, dict) and value.get('lesson'):
                    pr = value.get('pr', '?')
                    lessons.append((pr, value['lesson']))
        except Exception:
            continue

    if not lessons:
        return ''

    # Take the 10 most recent (by PR number, descending)
    def pr_num(entry):
        try:
            return int(entry[0].replace('#', ''))
        except (ValueError, AttributeError):
            return 0
    lessons.sort(key=pr_num, reverse=True)
    lessons = lessons[:10]

    features_str = ', '.join(sorted(matched_features))
    lines = [f'=== PRIOR LESSONS ({features_str}) ===']
    for pr, lesson in lessons:
        lines.append(f'- {lesson} ({pr})')
    lines.append('===\n')

    return '\n'.join(lines) + '\n'


def process_inbox(agent_id):
    """Process all inbox tasks for an agent. Called by orchestrator."""
    inbox = AGENTS_ROOT / 'inboxes' / agent_id
    outbox = AGENTS_ROOT / 'outboxes' / agent_id

    if not inbox.exists():
        return

    for task_file in sorted(inbox.glob('*.json')):
        try:
            with open(task_file) as f:
                task = json.load(f)

            prompt = task.get('prompt', '')
            system_prompt = task.get('system_prompt')
            system_prompt_file = task.get('system_prompt_file')
            timeout = task.get('timeout', 14400)
            source = task.get('source', 'unknown')
            model_override = task.get('model')  # Optional: task can force a specific model
            expected_agent = task.get('expected_agent')  # Opt-in identity assertion

            log(agent_id, 'Processing inbox task: ' + task_file.name + ' from ' + source)

            # Opt-in identity assertion: if the dispatcher declared which
            # logical agent this task is for, prepend a verification preamble
            # that makes the subprocess refuse if its loaded CLAUDE.md doesn't
            # match. Idempotent — only prepend if marker not already present.
            if expected_agent and IDENTITY_ASSERTION_MARKER not in prompt:
                prompt = build_expected_agent_assertion(expected_agent) + prompt
                log(agent_id, 'Prepended identity-assertion preamble (expected_agent=' +
                    str(expected_agent) + ') for task: ' + task_file.name)

            # WORKTREE ISOLATION (main/Luma only):
            # Each task runs in its own fresh worktree from origin/main so concurrent
            # tasks cannot collide on file edits, git state, or branch checkouts.
            # Sage/Nova/Mula don't get worktrees because they don't typically write code.
            worktree_path = None
            working_dir = None
            if agent_id == 'main':
                worktree_path = create_worktree_for_task(agent_id, task_file.stem)
                if worktree_path:
                    working_dir = worktree_path
                    # Note: /tmp identity landmine scrubbing happens
                    # unconditionally inside run_claude() below (see
                    # scrub_tmp_identity_landmines at line ~80). That fires
                    # on EVERY subprocess spawn, not just worktree paths,
                    # so we don't need a second call here.
                    # Idempotent prepend: never duplicate the preamble even if a
                    # prior retry/requeue already injected one into the prompt.
                    prompt = prepend_worktree_preamble(worktree_path, prompt, agent_id)
                else:
                    log(agent_id, 'WORKTREE FAILED — refusing to run task in shared workspace (collision risk). Task will be retried later.', 'ERROR')
                    continue  # Skip this task; orchestrator will pick it up again

            # Inject relevant lessons from sweep ledgers
            lesson_brief = extract_lessons_for_prompt(prompt)
            if lesson_brief:
                prompt = lesson_brief + prompt
                log(agent_id, 'Injected lesson brief for task: ' + task_file.name)

            success, output, _ = run_claude(
                agent_id, prompt,
                working_dir=working_dir,
                system_prompt=system_prompt,
                system_prompt_file=system_prompt_file,
                timeout=timeout,
                context='inbox',
                model_override=model_override,
                task_stem=task_file.stem,
            )

            result = {
                'task_id': task_file.stem,
                'source': source,
                'success': success,
                'output': output,
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'agent_id': agent_id,
                'worktree': worktree_path,
            }
            result_file = outbox / (task_file.stem + '-result.json')
            with open(result_file, 'w') as f:
                json.dump(result, f, indent=2)

            task_file.unlink()
            log(agent_id, 'Task ' + task_file.name + ' completed (success=' + str(success) + ')')

        except Exception as e:
            log(agent_id, 'Error processing ' + task_file.name + ': ' + str(e), 'ERROR')


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Run Claude Code agent')
    parser.add_argument('agent_id', help='Agent ID (beacon, forge, mirror, pulse)')
    parser.add_argument('--prompt', '-p', help='Direct prompt to run')
    parser.add_argument('--inbox', action='store_true', help='Process inbox tasks')
    parser.add_argument('--working-dir', '-d', help='Working directory override')
    parser.add_argument('--timeout', '-t', type=int, default=14400, help='Timeout in seconds')

    args = parser.parse_args()

    if args.prompt:
        success, output, _ = run_claude(args.agent_id, args.prompt,
                                         working_dir=args.working_dir,
                                         timeout=args.timeout)
        print(output)
        sys.exit(0 if success else 1)
    elif args.inbox:
        process_inbox(args.agent_id)
    else:
        parser.print_help()
