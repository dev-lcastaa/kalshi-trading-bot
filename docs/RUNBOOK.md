# Runbook: waiting for data

State as of **2026-10-06**. Everything that can be done without new data has been done. What is left is waiting for
the collector, then running one pre-registered test.

## Where things stand

| Item | State |
|---|---|
| Collector | Running on node004 since 2026-10-06 21:02 UTC, writing to the `aqlabs-eventstore` volume. Healthy, no alerts. |
| Legacy research data | Imported (Sep 12 to Oct 6). The Oct 6 gap between the legacy export and the collector is filled by the pull tool. |
| Phase 2a (maker execution) | Failed its kill check. Do not revisit without a new hypothesis written in the pre-registration. |
| Phase 3 stage 1 (H1, H2, H3) | All three failed (see `ARCHITECTURE.md`). They do not advance. H1b is blocked with H1. |
| **H4 (depth and trade flow)** | The only live hypothesis. Pipeline built and tested. Needs 14 days of collector data. |
| Holdout | Oct 20 onward, untouched. |
| Live trading | Keep it paused. Nothing has passed any gate. Keep momentum scalping off (it loses 3 to 4c per trade in replay). |

## Calendar

| Date (UTC) | What to do |
|---|---|
| any time | Health check (below). Optional dry run: `phase3_h4 --stage validate --allow-partial` once 8+ days exist. A dry run is never logged. |
| about Oct 13 | Pull and read the report. Check feed uptime and that H4's depth coverage is high. Nothing else to decide. |
| **Oct 20** (after Oct 19 has settled) | Pull, then run **H4 stage 2**. This is the one real test. |
| **Oct 27** | Stop rule: if no hypothesis has passed stage 2, trading development stops. The collector can keep running. |
| after a stage 2 pass | Stage 3 on the holdout. Pick `--holdout-end` **before** looking (suggest at least 7 days after Oct 20, e.g. Nov 3), then run it once. |

## Commands

All commands run from the repository root with the project's virtualenv. `$STORE` is the combined store.

```powershell
$LEGACY = "C:\Users\luis0\kalshi_research\eventstore"
$WORK   = "C:\Users\luis0\kalshi_research\collector_pull"
$STORE  = "$WORK\combined"

# 1. Pull everything and rebuild the combined research store (read-only on the server, safe to repeat, ~30 s)
.\.venv\Scripts\python.exe -m aqlabs.store.pull --legacy-root $LEGACY --work $WORK

# 2. Data quality (the pull prints this too): per-feed ticks, gaps and uptime
.\.venv\Scripts\python.exe -m aqlabs.store.cli report --root $STORE

# 3. H4 stage 2 (refuses if the period is incomplete; --allow-partial makes an unlogged dry run)
.\.venv\Scripts\python.exe -m aqlabs.research.phase3_h4 --root $STORE --stage validate --out "$WORK\h4_validate.txt"

# 4. Only after a passed stage 2: stage 3, once, on a window chosen in advance
.\.venv\Scripts\python.exe -m aqlabs.research.phase3_h4 --root $STORE --stage confirm --confirm-holdout --holdout-end 2026-11-03
```

Everything that has been run is in `$STORE\registry.jsonl` (hypothesis, stage, data fingerprint, code commit,
result). `python -c "import json;[print(r['hypothesis'],r['stage'],r['passed']) for r in map(json.loads,open(r'$STORE\registry.jsonl'))]"`
prints the history. The pull tool carries the registry across rebuilds.

## Health check (30 seconds)

```bash
ssh lcastaa@192.168.1.208
docker ps --format '{{.Names}}  {{.Status}}' | grep kalshi          # collector Up, "(healthy)", uptime not reset by deploys
docker logs --tail 30 aqlabs-kalshi-trading-bot-collector-1         # "Collector started", no ERROR / ALERT lines
docker exec aqlabs-kalshi-trading-bot-collector-1 python -m aqlabs.store.cli report --root /data/eventstore
df -h /                                                             # about 100 GB free at the start; ~170 MB per day
```

| Symptom | Meaning and action |
|---|---|
| An `ALERT: feed ... silent` log line | One feed is down past its limit. If it recovers on its own (`recovered` line) it is a short gap; if not, restart the collector (`docker restart`). Gaps cost data, so check it. |
| Container restarted recently | A deploy ran with `DEPLOY_COLLECTOR` ticked, or the host rebooted. Expect one ~80 s gap in every feed. |
| `dropped_rows` above 0 in `collector_status.json` | The write buffer filled. Check disk space and the host's load. |
| Disk below 10 GB free | Free space or extend the volume before the collector fills it. |
| `bitstamp/SOLUSD_RTI` looks sparse | Normal: Bitstamp only ticks when someone trades (limit 600 s). |

Deploys: Jenkins rebuilds only `bot` and `frontend`. Tick **DEPLOY_COLLECTOR** only when the collector code changed.

## Reading the H4 result

`==> H4 validate: PASS` needs every criterion: positive mean, 90% CI lower bound above zero, at least 300 trades,
positive in both halves, the shuffled-feature placebo negative, still positive with a 5 s delay, and depth coverage
at least 90%. Anything else is a fail, and the pre-registration does not allow re-running with other settings.

- **Pass:** run stage 3 once on the holdout. If that passes too, the next step is paper trading, not live money.
- **Fail with low trade counts:** that is "not enough evidence", not evidence of an edge. The rules still count it as a
  fail at stage 2; a longer window would be a new, separately registered test.
- **Fail:** the stop rule applies on Oct 27. Honest options are to keep collecting for a new hypothesis written
  down first (see below), or to stop. Neither involves tuning H1 to H4.

## Adding a new idea

Write it into a new section of `docs/PHASE3_PREREGISTRATION.md` (spec, metric, pass rule, placebo) and commit it
**before** running anything. Ideas that follow from what is already known: the Coinbase basis carries a real but tiny
signal (about 0.1 bp of a 10 s move per 1-sigma basis); the final-minute settlement model has mixed signs and few
trades. Re-trying a near-miss is how overfitting starts, so a new idea needs its own rationale.

## Do not

- Look at `holdout` outcomes outside a confirm run, or change any frozen setting after seeing a result.
- Restart the collector casually, or turn live trading on.
- Treat a green dry run (`--allow-partial`) as a result. It is a smoke test.
