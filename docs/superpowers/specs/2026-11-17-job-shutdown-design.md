# Stop running jobs cleanly on daemon shutdown (M4)

Status: draft for approval, 2026-11-17. Small change; short design.

## Why

The daemon-architecture final review (M4, ruling R60) found that shutdown never waits for job
threads. `sandbox-link` and `history` jobs run on daemon threads that die when the process
exits, and the daemon lock is released while one may still be writing. A stop that lands
between Plaid's public-token exchange and the local commit leaves an Item at Plaid that
findata never stored: an orphan nobody can revoke, because the access token died with the
thread and Plaid has no API to list a client's Items. Today only Sandbox links reach this;
real linking will go through the same exchange.

## Goals

- `findata stop`, Ctrl+C and SIGTERM never cut a Plaid write step (link exchange + save, one
  sync pass) in half.
- Shutdown stays fast: a job waiting between history polls stops at once.
- Fallbacks cover the cases a clean stop cannot: a step that overruns the wait, a failed save,
  a forced stop.

## Non-goals and anti-goals

- Not: waiting for a job's whole history wait (up to an hour) before stopping.
- Not: resuming a stopped job after restart; the user runs it again, and the next sync
  continues from the stored cursor.
- Anti-goal: `findata stop` that can hang without bound.
- Anti-goal: a stop that loses a stored access token or leaves a half-written Item a later
  command trips over.

## Approaches (user chose A, with fallbacks)

- **A. Protect the write steps, cancel the waiting (chosen).** Shutdown signals jobs to stop;
  sleeping jobs end at once; a job inside a write step finishes that step; shutdown waits a
  bounded time for job threads before releasing the daemon lock.
- B. Wait for every job to finish. Rejected: `findata stop` could hang up to an hour.
- C. Find and remove orphans at the next start. Rejected: impossible, as above.

## Design

1. **Stop signal.** `JobManager` gets `stop(timeout)`: it sets a stop event, then joins the
   running job threads, sharing one deadline of `GRACEFUL_SHUTDOWN_S` (30 s, as uvicorn and
   the scheduler use). `_serve` calls it in its `finally`, beside `scheduler.stop()`, so it
   runs before `DaemonLock` is released.
2. **Waiting stops at once.** The history wait's sleep becomes `stop_event.wait(interval)`
   (keeping the `job_sleep` test seam). On wake by stop, the job ends with a `stopped` event
   and job state `stopped`. Write steps are never interrupted: the check comes only between
   sync passes, outside `PLAID_WRITE_LOCK`.
3. **No new jobs while stopping.** `JobManager.start` refuses once the stop event is set;
   the routes answer 503 `DAEMON_STOPPING`.
4. **Clients say what happened.** CLI `sandbox-link`, the history wait and MCP report a
   `stopped` job as "stopped because the daemon shut down; run it again", not as a failure.

### Fallbacks

- **F1, overrun.** If a job thread is still alive at the deadline, log a warning naming the
  job id and kind, write an audit line (`jobs.stop`, outcome `abandoned`), and exit anyway.
  (My call: a hung Plaid call must not hold `findata stop` forever.)
- **F2, token saved first.** `link_item` commits the Item with its encrypted access token
  right after the exchange, then calls `item_get` and commits the institution in a second
  step. A stop or crash after the first commit leaves a stored Item with no name, not an
  orphan. A later sync or `sandbox-link` reuse fills in a missing name (one `item_get`).
  The only window left is the exchange call itself (Plaid created the Item, the response
  had not arrived). Nothing in-process can close it.
- **F3, undo if the save fails.** If the first commit raises after a successful exchange,
  call `item_remove` with the in-memory token (best effort, logged without the token), then
  re-raise. The user sees the original error and no orphan stays at Plaid.
- **F4, second signal forces.** A second Ctrl+C or SIGTERM during the job wait skips the
  rest of it (log: "Forced stop; a running job was not finished."). F2 still protects the
  token. (My call: the user always has a way out.)

## Left to the builder

- Exact `stopped` event fields and client wording, within the meaning above.
- Whether `JobOut.state` gains a `stopped` value or reuses `failed` with
  `error_code: "DAEMON_STOPPING"`. My preference is a distinct state, since a stop is not a
  failure.
- Where the "missing name" fill-in lives (sync pass or `find_sandbox_item` reuse path).

## Tests

- Stop during a history wait: the job ends `stopped` within a second; no Plaid call after.
- Stop during a link write step (fake gateway blocks inside `exchange_public_token`):
  shutdown waits, the Item is stored, then the daemon exits.
- Overrun: a write step longer than the deadline gives the warning and audit line, and
  shutdown returns.
- F2: a failure in `item_get` after the exchange leaves a stored Item with no name; the next
  sync fills it in.
- F3: a commit failure after the exchange calls `item_remove` with that token.
- Job start after stop begins gets 503 `DAEMON_STOPPING`.
