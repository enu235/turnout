#!/usr/bin/env bash
# Live end-to-end check: call every enabled target once through Turnout.
#
# This costs real money and credits -- one short call per target. Run it after
# changing an adapter or adding a model, not routinely.
set -uo pipefail
cd "$(dirname "$0")/.."
URL="${TURNOUT_URL:-http://127.0.0.1:8700}"
PROMPT="${1:-Reply with exactly: OK}"

mapfile -t TARGETS < <(curl -sS "$URL/api/state" | .venv/bin/python -c "
import json,sys
for t in json.load(sys.stdin)['targets']:
    if t['enabled'] and t['available'] is not False: print(t['id'])
")

printf '%-18s %-10s %-9s %s\n' TARGET RESULT LATENCY DETAIL
fail=0
for t in "${TARGETS[@]}"; do
  start=$(date +%s)
  body=$(curl -sS -m 600 -X POST "$URL/v1/chat/completions" -H 'Content-Type: application/json' \
    -d "$(.venv/bin/python -c "
import json,sys; print(json.dumps({'model':sys.argv[1],'messages':[{'role':'user','content':sys.argv[2]}]}))
" "$t" "$PROMPT")")
  el=$(( $(date +%s) - start ))
  line=$(printf '%s' "$body" | .venv/bin/python -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: print('BAD_JSON|'); raise SystemExit
if 'error' in d: print('ERROR|'+str(d['error'].get('message',''))[:70])
else:
    sh=d.get('turnout',{})
    print('OK|'+repr(d['choices'][0]['message']['content'][:24])+' as '+str(sh.get('provider_model')))
")
  status=${line%%|*}; detail=${line#*|}
  [[ "$status" == OK ]] || fail=1
  printf '%-18s %-10s %-9s %s\n' "$t" "$status" "${el}s" "$detail"
done
exit $fail
