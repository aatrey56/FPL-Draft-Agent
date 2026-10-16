# Shared launchd helpers for the autopilot scripts (source, don't execute).
# bootout of a running service is asynchronous: bootstrapping before it has
# finished unloading fails with "Bootstrap failed: 5: Input/output error".
# Tunables (env): LAUNCHD_WAIT_POLLS (default 50), LAUNCHD_POLL_SLEEP (0.2s),
# LAUNCHD_BOOTSTRAP_TRIES (3). Compatible with macOS bash 3.2.

LAUNCHD_WAIT_POLLS="${LAUNCHD_WAIT_POLLS:-50}"
LAUNCHD_POLL_SLEEP="${LAUNCHD_POLL_SLEEP:-0.2}"
LAUNCHD_BOOTSTRAP_TRIES="${LAUNCHD_BOOTSTRAP_TRIES:-3}"

# unload_agent UID LABEL — boot the agent out (if loaded) and wait until
# launchd no longer knows it. Returns 1 if it is still loaded after the wait.
unload_agent() {
  local uid_n="$1" label="$2" i=0
  launchctl bootout "gui/$uid_n/$label" 2>/dev/null || true
  while launchctl print "gui/$uid_n/$label" >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -ge "$LAUNCHD_WAIT_POLLS" ]; then
      echo "warning: $label still loaded after bootout" >&2
      return 1
    fi
    sleep "$LAUNCHD_POLL_SLEEP"
  done
  return 0
}

# load_agent UID LABEL PLIST — bootstrap with retries. Returns 1 (after naming
# the label) if every attempt fails, so callers can continue with other agents.
load_agent() {
  local uid_n="$1" label="$2" plist="$3" n=1
  while [ "$n" -le "$LAUNCHD_BOOTSTRAP_TRIES" ]; do
    if launchctl bootstrap "gui/$uid_n" "$plist"; then
      return 0
    fi
    n=$((n + 1))
    [ "$n" -le "$LAUNCHD_BOOTSTRAP_TRIES" ] && sleep 1
  done
  echo "error: failed to bootstrap $label ($plist) after $LAUNCHD_BOOTSTRAP_TRIES attempts" >&2
  return 1
}
