# Pseudonymous leaderboard listing

**Status:** client half shipped (0.9.4). Server + site half is the platform repo's work.
**Audience:** whoever maintains `ollamaherd.com` and the telemetry API.

## Why

The leaderboard currently lists only herds that set `FLEET_NODE_HERD_NICKNAME`.
As of 2026-08-26 that is exactly one — ours — while four other herds report in
anonymously. A one-row leaderboard reads as a dead project.

The first instinct was "those herds chose not to be named". That was wrong. The
first-run notice never mentioned that naming was possible, so nobody declined —
they were never told. Two of the three changes below are just fixing that.

## What ships where

| # | Change | Repo | Status |
|---|--------|------|--------|
| 1 | First-run notice + dashboard disclose the leaderboard | this repo | **done, 0.9.4** |
| 2 | Leaderboard shows the count of unnamed herds | site | todo |
| 3 | Unnamed herds listed under a handle derived from `install_id` | server + site | todo |

## The consent problem, and how `agent_version` solves it

`ollamaherd.com/telemetry` currently promises:

> "Staying anonymous is the default and the leaderboard will never know you exist."
> "You can give your herd a name. That is the one thing that ever shows up in public."

Item 3 contradicts both sentences. The existing installs agreed to the text above,
so listing them — even under a meaningless handle — publishes herds that were told
they never would be. A stable public handle is weaker than a name but stronger than
absence: it lets anyone watch one herd's day-to-day activity, which "you appear only
in global totals" does not.

**The fix needs no new payload field.** The payload already carries `install_id` and
`agent_version` (see `ALLOWED_PAYLOAD_KEYS` in `node/anonymous_rollup.py`), and the
0.9.4 notice tells the operator about the leaderboard before anything is sent.

So:

> **List a herd pseudonymously only when `agent_version >= 0.9.4`.**

That single condition enforces the whole ordering. Installs on ≤0.9.3 never saw the
disclosure and stay invisible until they upgrade — at which point they see the new
notice on the next start. No retroactive publication, no new wire field, no 422 risk
against `extra="forbid"`.

Do **not** implement item 3 by having the client send an auto-generated `nickname`.
That would make "chosen name" and "generated handle" indistinguishable on the wire,
and it would strand anyone who later wants to clear their name.

## Required ordering

1. Ship client 0.9.4 (done — notice + dashboard now say listing happens).
2. Update `ollamaherd.com/telemetry` to describe pseudonymous listing. **The page is
   the contract** (see CLAUDE.md): until it says this, the code is the bug.
3. Only then enable listing on the site, gated on `agent_version >= 0.9.4`.

Doing 3 before 2 means the site is publishing something its own privacy page denies.

## Handle derivation (server-side)

```
handle = "herd-" + sha256(install_id).hexdigest()[:6]
```

- Derive it **server-side**. The client already sends `install_id`; nothing new to add.
- `install_id` is a random UUID4, never machine-derived — `common/install_id.py` has a
  test that fails the build if it is ever the hostname or a hash of one. So the handle
  cannot leak anything about the machine.
- Stable across restarts, which is what makes a leaderboard position meaningful.
- Never show the raw `install_id` publicly. It is the key the operator would use to
  request removal; publishing it would let anyone impersonate that request.
- Collisions: 24 bits is ~16.7M. Fine for hundreds of herds; if the population ever
  approaches thousands, widen to 8 chars rather than adding a counter suffix, so
  existing handles stay stable.

## Item 2 — show unnamed herds in aggregate

Independent of item 3 and worth doing regardless, since it needs no client change and
breaks no promise:

> `41 herds contributing · 6 named`

or a trailing row: `+35 anonymous herds`.

This conveys scale while keeping "anonymous herds appear only in totals" literally
true. If item 3 lands, this line stays useful as the count of herds too old to list.

## Gap this leaves — "contribute but stay unlisted"

Today `nickname == ""` means *not listed*. After item 3 it means *listed under a
handle*, and the only way to be entirely absent is `FLEET_NODE_TELEMETRY=false`,
which also stops contributing data. That trades away a choice some operators will
want, and it is a strictly worse position than they have now.

Recommended follow-up, needing coordination because of `extra="forbid"`:

1. Server accepts an optional boolean `listed` in the payload.
2. Only after the server accepts it, the client starts sending it, driven by a new
   `FLEET_NODE_LEADERBOARD` setting (default `true`).

Order matters: a client that sends `listed` before the server accepts it will 422 the
**entire payload**, not just that field — an unknown key already cost us every send
once (`mlx_servers`, see `docs/observations.md`). Until then, document the coarse
opt-out honestly rather than implying a finer one exists.

## Verifying after rollout

```bash
# our own herd's identity and what it reports
cat ~/.fleet-manager/install_id
python3 -c "import hashlib;print('herd-'+hashlib.sha256(open('$HOME/.fleet-manager/install_id').read().strip().encode()).hexdigest()[:6])"
```

Then confirm on the leaderboard that (a) our handle matches, (b) no herd on ≤0.9.3
appears, and (c) setting a nickname replaces the handle rather than adding a row.
