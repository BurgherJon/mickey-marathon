# Mickey Marathon

Mickey Marathon — a marathon-coach agent for Jonathan. She builds and
maintains training plans in a Google Sheet, tracks workouts / hydration /
body composition through Garmin, schedules each day's workout to the
watch and Todoist, debriefs after workouts, audits equipment usage weekly,
and exchanges data with peer agents (Nora the Nutritionist) through The
Forum's agent-to-agent MCP server.

Built on **[The Forum](https://github.com/Comites-ai/the-forum)** —
[Comites.ai](https://comites.ai)'s open-source middleware that routes
messages from Slack, Google Chat, Telegram, and Discord to AI agents
running on Vertex AI. Mickey serves Discord.

## Architecture

```
┌──────────────────────────────────────────────────────┐
│   Discord                                            │
└──────────────────────────┬───────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────┐
│  The Forum (Cloud Run, project: vertex-ai-middleware-prod)
│  · routes DMs · runs scheduled jobs · hosts the      │
│    scheduler MCP and the agents (A2A) MCP            │
└──────────────────────────┬───────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────┐
│  Mickey Marathon (Vertex AI Reasoning Engine)        │
│  Project: mickey-marathon                            │
│  SA:      mickey-marathon@mickey-marathon.iam.gserviceaccount.com
│  · Garmin Connect (token-only)  · Todoist API v1     │
│  · plan spreadsheet (Sheets)    · memory doc (Docs)  │
└──────────────────────────────────────────────────────┘
```

## Inquiries (what other agents can ping Mickey about)

Published to The Forum's Firestore from [`inquiries.json`](inquiries.json)
at every deploy, discoverable via the Forum's agents MCP
(`{FORUM_URL}/api/v1/mcp/agents/`), and answered when a message arrives
prefixed `[From Agent: <Agent> | On Behalf Of: <User>]`. Mickey only has
data for Jonathan Cavell; for other users she replies
`NO_DATA: I do not coach <User>.`

| Inquiry | Request | Response format |
|---|---|---|
| `planned_workouts_today` | `AGENT_QUERY: planned_workouts_today` | `PLANNED_WORKOUTS <YYYY-MM-DD>: <workout summary> \| purpose=<adaptations targeted> \| energy=<low\|medium\|high> \| est_calories=<n>` followed by a short prose explanation (what the workout is, desired physical changes, energy demand, calorie-estimate reasoning) |
| `calories_burned_today` | `AGENT_QUERY: calories_burned_today` | `CALORIES_BURNED <YYYY-MM-DD>: total=<kcal> active=<kcal> bmr=<kcal>` |
| `alarm_time` | `AGENT_QUERY: alarm_time` | `ALARM <YYYY-MM-DD>: <HH:MM AM/PM ET> (device: <name>)` or `ALARM <YYYY-MM-DD>: none set` |
| `weight_body_fat_week` | `AGENT_QUERY: weight_body_fat_week` | `WEIGHT_BODY_FAT_REPORT (last 7 days, most recent first)` then one line per weigh-in day: `<YYYY-MM-DD> \| weight_lbs=<x.x> \| body_fat_pct=<x.x\|n/a>`; if none: `NO_DATA: no weigh-ins in the last 7 days` |

These formats are a contract — agents parse them. Change them only
together: `inquiries.json` + the prompt section in `agent.py` + this
table, in one commit.

## The training-plan spreadsheet

Workbook `MARATHON_PLAN_SHEET_ID` (in `.env`), shared Editor with the
per-agent SA. Two tabs, layout is a hard contract:

- **Philosophy** — only `A1`: the training philosophy in plain English
  (time goals, course worries like hills/heat, fitness goals, mileage-vs-
  speedwork preference, strength-training approach, 2-a-day/rest-day
  preferences, known scheduling constraints). Talked through with
  Jonathan before every new plan; read before every plan modification.
- **Current Marathon Plan** — row 1 header `Week | Monday | ... | Sunday`;
  column A = the Monday date (YYYY-MM-DD) of each week; day cells B–H.
  Planned cells have no background. After a day passes Mickey rewrites the
  cell as `Plan: <original>` + newline + `Actual: <what happened>` and
  colors it: **green** = did the plan (or ≥90% equivalent), **yellow** =
  worked out but not close, **red** = skipped.

## Scheduled jobs

All created by Mickey through the Forum's scheduler MCP (timezone
America/New_York, delivery to Discord). Ask her to "set up your jobs" to
(re)create them — creates are idempotent by name.

| Job | Schedule | What it does |
|---|---|---|
| `hydration-morning/afternoon/evening` | 10:00 / 15:00 / 19:00 daily | Reads Garmin hydration, nags with the running total |
| `nightly-alarm-sync` | 02:00 daily | Reads the alarm (any Garmin device), retargets `morning-readiness-check` to alarm−45min, replies `[SILENT]` |
| `morning-readiness-check` | retargeted nightly | Sleep/HRV/body-battery/readiness vs today's plan → go message + Todoist task + workout pushed to watch, or a negotiated substitute |
| `hourly-workout-check` | hourly | New Garmin activity? → debrief, sheet update + color, watch cleanup, Todoist completion, `WORKOUT_COMPLETED` ping to Nora; otherwise `[SILENT]` |
| `weekly-equipment-audit` | Sat 12:00 | Last 14 days of runs: GPS start within 50mi of a stored location → expect that location's gear, else the travel default; mismatches → ask Jonathan; clean → `[SILENT]` |

## Operations runbook

**Garmin token refresh** (~yearly, or whenever Mickey says her Garmin
access expired):
```bash
/home/jonathan/projects/.my_venv/bin/python3 scripts/bootstrap_garmin_tokens.py
```
Interactive (credentials + MFA); stores the token bundle in Secret
Manager (`mickey-marathon-garmin-tokens`). The deployed agent never does
credential logins — Garmin blocks headless logins.

**Todoist token**: personal API token (Todoist → Settings → Integrations
→ Developer) in `mickey-marathon-todoist-token`:
```bash
echo -n "TOKEN" | gcloud secrets versions add mickey-marathon-todoist-token \
  --data-file=- --project=mickey-marathon
```
Tasks go to project "Goal 4: Run a 4h Marathon" with label "Have and
Project a Youthful Energy" (hard-coded in `todoist_utilities.py`).

**Forum MCP key** (authenticates both the scheduler and agents MCP
servers): provision/rotate from the Forum repo —
`python scripts/provision_scheduler_api_key.py --agent-id <FIRESTORE_ID>`,
then store the plaintext in `mickey-marathon-scheduler-mcp-key`.

**Memory doc** (`AGENT_MEMORY_DOC_ID`): structured markdown — pupil
profile, equipment defaults by location, processed-activity IDs, Todoist
task map, race metadata. Shared Editor with the per-agent SA.

## Local development

```bash
# In the shared venv with this repo's dependencies installed
adk web
```

For pre-deploy verification, drive `root_agent` with `InMemoryRunner`
(see AGENTS.md "Local development") — the deployed engine hides tool
errors; the in-process runner surfaces them.

## Deploy

```bash
./deploy_and_update.sh
```

Blue/green: deploys a new Reasoning Engine, smoke-tests it, re-registers
in the Forum's Firestore (including `inquiries.json`), clears stale
sessions, deletes the old engine.

## Operating rules

See [AGENTS.md](AGENTS.md) for the invariants (infrastructure via
terraform, secrets in Secret Manager, deploy via the script, inquiry
contract sync, etc.).

## Acknowledgements

Bootstrapped from the [Comites.ai Agent Template](https://github.com/Comites-ai/agent-template)
(MIT) and runs on [The Forum](https://github.com/Comites-ai/the-forum) (AGPL-3.0).
