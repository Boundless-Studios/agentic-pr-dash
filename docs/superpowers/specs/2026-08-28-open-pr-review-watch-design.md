# Open PR Review Watch Design

## Problem

A pull request can receive review comments at any point while it remains open. A
single clean observation after CI turns green is therefore not a terminal proof
that review work is finished. Keeping an interactive agent alive to discover
late feedback wastes model time, while frequent unconditional GitHub polling
wastes API quota.

## Decision

`agentic-pr-dash` owns a durable review-watch schedule for every open PR that has
reached green CI. Gaia and other repositories remain thin lifecycle consumers.

The schedule observes the PR at these elapsed offsets from the latest reset:

`1m, 5m, 15m, 30m, 60m, 120m, 240m, 480m`

After the 480-minute observation, a clean open PR continues to be observed every
480 minutes until it is merged or closed.

## State and transitions

The lifecycle snapshot persists:

- the head SHA that armed the watch;
- the reset timestamp;
- the most recent successful review observation;
- the next due observation timestamp;
- the current schedule index;
- the observed unresolved-thread count;
- a short reason for the most recent reset.

The watch is armed when required CI first becomes green. It resets to the first
one-minute interval when a new head is observed or when new actionable feedback
appears. A failed or indeterminate GitHub observation does not advance the
schedule; it remains due and retries on the normal maintenance tick with existing
quota/backoff controls.

A successful clean observation advances to the next interval. A successful
observation with actionable feedback queues PR maintenance and resets the watch.
Resolved or policy-addressed comments are not actionable, but remain visible in
the normal settlement evidence.

The watch terminates only when the PR is merged or closed. Losing green CI pauses
review-watch advancement while the existing CI-maintenance policy owns the PR;
green CI resumes the same watch unless a head change reset it.

## Completion and stop-hook semantics

An interactive agent may finish after the current head is pushed, CI is green,
all currently observed feedback is settled, and the durable watch is armed. Stop
hooks verify that durable ownership exists; they never sleep through the review
schedule. Late feedback wakes or queues maintenance through the existing
lifecycle workflow.

Thus “session may stop” and “PR lifecycle is complete” are distinct:

- session completion requires a clean current observation plus an armed watch;
- lifecycle completion requires the PR to be merged or closed.

## Observability

The lifecycle/checklist projection exposes `review_watch` with `armed`, `due`, or
`paused` state, `next_check_at`, schedule position, last successful observation,
and reset reason. The dashboard can therefore show at a glance why an apparently
clean PR remains actively monitored.

## Validation

Tests use a fake clock and fake GitHub observations to prove the exact schedule,
the repeating eight-hour tail, reset-on-head and reset-on-feedback behavior,
failure-without-advance, CI pause/resume, close termination, maintenance queueing,
and stop-gate acceptance only when a clean PR has durable watch ownership.
