#!/bin/bash
# Check for running workflows in Claude Code background sessions.
#
# A workflow's journal ends on a `started` event while an agent is in flight, so that is the signal
# for "still running". But a workflow whose process died — killed, crashed, SIGABRT — leaves exactly
# the same tail forever, which would block every later run. A journal that has not been touched for
# STALE_MINUTES is therefore reported as stale and does not block.
#
# Environment:
#   WORKFLOWS_BASE  directory holding <session>/subagents/workflows/wf_*/ (default: this project's)
#   STALE_MINUTES   a journal untouched for longer than this is stale, not running (default: 15)
#
# Exit codes:
#   0 = no running workflows
#   1 = running workflows found
#   2 = error checking workflows
#
# Output: running workflow IDs on stdout, one per line; stale ones are listed on stderr for
# awareness only.

set -e

WORKFLOWS_BASE="${WORKFLOWS_BASE:-/home/vscode/.claude/projects/-app-als-ai-scientist}"
STALE_MINUTES="${STALE_MINUTES:-15}"

if [ ! -d "$WORKFLOWS_BASE" ]; then
  # No workflows directory exists = no running workflows
  exit 0
fi

running_ids=()
stale_ids=()
now=$(date +%s)

for session_dir in "$WORKFLOWS_BASE"/*/subagents/workflows/wf_*/; do
  [ -d "$session_dir" ] || continue

  journal_file="$session_dir/journal.jsonl"
  [ -f "$journal_file" ] || continue

  workflow_id=$(basename "$session_dir")
  last_event=$(tail -1 "$journal_file" 2>/dev/null | jq -r '.type' 2>/dev/null || echo "unknown")

  case "$last_event" in
    launched|result|failed)
      # launched-but-never-started is stuck, not running; result/failed are terminal.
      continue
      ;;
    started)
      state="started"
      ;;
    *)
      if tail -1 "$journal_file" 2>/dev/null | jq -e '.stop_reason' >/dev/null 2>&1; then
        continue    # has a stop_reason = terminal
      fi
      state="unknown state: $last_event"
      ;;
  esac

  # A live workflow keeps writing to its journal; a dead one cannot. Note that an agent can be slow,
  # so STALE_MINUTES must stay well above the longest plausible gap between journal events.
  idle_minutes=$(( (now - $(stat -c %Y "$journal_file")) / 60 ))
  if [ "$idle_minutes" -ge "$STALE_MINUTES" ]; then
    stale_ids+=("$workflow_id (${state}, idle ${idle_minutes}m — process is gone)")
  else
    running_ids+=("$workflow_id ($state, idle ${idle_minutes}m)")
    echo "$workflow_id ($state)"
  fi
done

if [ "${#stale_ids[@]}" -gt 0 ]; then
  echo "Ignoring ${#stale_ids[@]} stale workflow(s), untouched for ${STALE_MINUTES}m or more:" >&2
  printf "  %s\n" "${stale_ids[@]}" >&2
fi

if [ "${#running_ids[@]}" -gt 0 ]; then
  echo "" >&2
  echo "Found ${#running_ids[@]} running workflow(s):" >&2
  printf "  %s\n" "${running_ids[@]}" >&2
  exit 1
fi

exit 0
