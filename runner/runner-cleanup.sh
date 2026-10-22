#!/bin/bash
# Job-completed hook (ACTIONS_RUNNER_HOOK_JOB_COMPLETED) for the orbit-ci runners on the sandbox box.
# Installed at /home/runner/runner-cleanup.sh; this file is the versioned source (T415 B3).
#
# 1. Strip cache state between jobs on this persistent host:
#   - ~/.pub-cache/git         — Dart private-sibling clones cached across jobs
#   - ~/.npm/_cacache          — npm's tarball/metadata cache
#   Not cleared: ~/.gitconfig, ~/.git-credentials. actions/checkout rewrites these per job with a
#   scoped token; wiping them between jobs of the SAME run breaks npm ci / dart pub get for private
#   siblings. Checkout's own post-cleanup removes the credential at job end.
#
# 2. Reap orphans of this runner's finished or cancelled jobs. A cancelled job can leave `npm test` /
#    jest reparented to init, holding ~4 GB per worker (2026-10-22: two such trees held 17 of 43 GB).
#    Killed by PID, never by process group: job steps share the runner service's group (runsvc.sh,
#    Runner.Listener, Runner.Worker and the job all carry one PGID), so a group kill would stop the runner.
#    A process is killed only when it is owned by this user, has NO Runner.Worker, Runner.Listener or
#    runsvc.sh ancestor (live jobs and the runner service always do; this hook included), and its cwd is
#    under THIS runner's _work dir. Other runners' jobs, and anything outside _work, are never touched.
#
# Change log:
#   2026-09-11  narrow to caches only; see LANE-48 diagnostic on lane #48.
#   2026-10-22  reap orphaned job processes by PID (T415 B3, GIT-02).

rm -rf "$HOME/.pub-cache/git" "$HOME/.npm/_cacache"

# This runner's _work dir, from the live Runner.Worker in our own ancestry. RUNNER_CLEANUP_WORK overrides it (self-check only).
work_dir() {
  [ -n "$RUNNER_CLEANUP_WORK" ] && { echo "$RUNNER_CLEANUP_WORK"; return; }
  local p=$$ exe
  while [ "$p" -gt 1 ]; do
    if [ "$(cat /proc/$p/comm 2>/dev/null)" = "Runner.Worker" ]; then
      exe=$(readlink /proc/$p/exe) || return 1
      echo "$(dirname "$(dirname "$exe")")/_work"; return
    fi
    p=$(awk '/^PPid:/{print $2}' /proc/$p/status 2>/dev/null) || return 1
    [ -n "$p" ] || return 1
  done
  return 1
}

# True when a pid is, or descends from, a live runner process.
under_runner() {
  local p=$1
  while [ -n "$p" ] && [ "$p" -gt 1 ]; do
    case "$(cat /proc/$p/comm 2>/dev/null)" in Runner.Worker|Runner.Listener|runsvc.sh) return 0 ;; esac
    p=$(awk '/^PPid:/{print $2}' /proc/$p/status 2>/dev/null)
  done
  return 1
}

# True when pid is an orphan of this runner's jobs: ours, cwd under $work, no live runner ancestor.
is_orphan() {
  local pid=$1 work=$2 cwd
  [ "$(awk '/^Uid:/{print $2}' /proc/$pid/status 2>/dev/null)" = "$(id -u)" ] || return 1
  cwd=$(readlink /proc/$pid/cwd 2>/dev/null) || return 1
  case "$cwd/" in "$work"/*) ;; *) return 1 ;; esac
  ! under_runner "$pid"
}

reap_orphans() {
  local work pid
  local -a victims=() still=()
  work=$(work_dir) || { echo "runner-cleanup: no Runner.Worker ancestor, orphan reap skipped"; return 0; }
  [ -d "$work" ] || return 0
  for d in /proc/[0-9]*; do
    pid=${d#/proc/}
    is_orphan "$pid" "$work" && victims+=("$pid")
  done
  [ ${#victims[@]} -gt 0 ] || return 0
  echo "runner-cleanup: reaping ${#victims[@]} orphaned process(es) under $work: ${victims[*]}"
  kill -TERM "${victims[@]}" 2>/dev/null
  sleep "${RUNNER_CLEANUP_GRACE:-5}"
  for pid in "${victims[@]}"; do is_orphan "$pid" "$work" && still+=("$pid"); done   # re-check: a PID may be reused
  [ ${#still[@]} -gt 0 ] && kill -KILL "${still[@]}" 2>/dev/null
  return 0
}

reap_orphans
exit 0
