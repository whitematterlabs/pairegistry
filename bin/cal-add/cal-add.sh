#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: cal-add TITLE START END [--notes NOTES] [--calendar NAME] [--help]

Create an event in Apple Calendar via AppleScript.

Arguments:
  TITLE              Event title (quoted if it contains spaces)
  START              Start datetime, e.g. "2026-05-16 19:00"
  END                End datetime,   e.g. "2026-05-16 21:00"

Options:
  --notes NOTES      Notes/description for the event
  --calendar NAME    Target calendar name (default: first available, usually "Calendar")
  --help             Show this help

Example:
  cal-add "Dinner with Alice" "2026-05-16 19:00" "2026-05-16 21:00" \
    --notes "Bring wine" --calendar Work
EOF
}

die() { echo "cal-add: error: $*" >&2; exit 1; }

for a in "$@"; do
  [[ "$a" == "--help" || "$a" == "-h" ]] && { usage; exit 0; }
done
[[ $# -eq 0 ]] && { usage; exit 1; }

TITLE=""
START=""
END=""
NOTES=""
CALENDAR=""
positional=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --notes)
      [[ $# -lt 2 ]] && die "--notes requires a value"
      NOTES="$2"; shift 2 ;;
    --notes=*)
      NOTES="${1#--notes=}"; shift ;;
    --calendar)
      [[ $# -lt 2 ]] && die "--calendar requires a value"
      CALENDAR="$2"; shift 2 ;;
    --calendar=*)
      CALENDAR="${1#--calendar=}"; shift ;;
    --help|-h)
      usage; exit 0 ;;
    --)
      shift; while [[ $# -gt 0 ]]; do positional+=("$1"); shift; done ;;
    -*)
      die "unknown option: $1" ;;
    *)
      positional+=("$1"); shift ;;
  esac
done

[[ ${#positional[@]} -ne 3 ]] && { usage >&2; die "expected TITLE START END (got ${#positional[@]} positional args)"; }

TITLE="${positional[0]}"
START="${positional[1]}"
END="${positional[2]}"

validate_date() {
  local label="$1" value="$2"
  if ! date -j -f "%Y-%m-%d %H:%M" "$value" +%s >/dev/null 2>&1; then
    die "$label is not in 'YYYY-MM-DD HH:MM' format: $value"
  fi
}
validate_date "START" "$START"
validate_date "END"   "$END"

start_epoch=$(date -j -f "%Y-%m-%d %H:%M" "$START" +%s)
end_epoch=$(date -j -f "%Y-%m-%d %H:%M" "$END"   +%s)
[[ "$end_epoch" -le "$start_epoch" ]] && die "END must be after START"

# Pass values to osascript via argv to avoid shell-expansion of $ / backticks
# inside the AppleScript body, and to keep escaping the script's problem.
AS_START="${START}:00"
AS_END="${END}:00"

if [[ -n "$CALENDAR" ]]; then
  CAL_CLAUSE='set targetCal to first calendar whose name is calName'
else
  CAL_CLAUSE='set targetCal to first calendar'
fi

osascript \
  -e 'on run argv' \
  -e '  set theTitle to item 1 of argv' \
  -e '  set theStart to item 2 of argv' \
  -e '  set theEnd to item 3 of argv' \
  -e '  set theNotes to item 4 of argv' \
  -e '  set calName to item 5 of argv' \
  -e '  set startDate to (date theStart)' \
  -e '  set endDate to (date theEnd)' \
  -e '  tell application "Calendar"' \
  -e "    ${CAL_CLAUSE}" \
  -e '    tell targetCal' \
  -e '      make new event with properties {summary:theTitle, start date:startDate, end date:endDate, description:theNotes}' \
  -e '    end tell' \
  -e '  end tell' \
  -e 'end run' \
  -- "$TITLE" "$AS_START" "$AS_END" "$NOTES" "$CALENDAR" >/dev/null

echo "Created event \"$TITLE\" from $START to $END${CALENDAR:+ in calendar \"$CALENDAR\"}"
