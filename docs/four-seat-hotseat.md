# Four-seat hotseat extension

This branch prepares opt-in two-to-four-seat Architecture-1 matches. It does not establish a live four-seat startup or replace the two consecutive fresh 30-round acceptance contract. The parent is running its separate two-seat 100-round lane.

Starting custody: `/home/alexk/civ-arena-four-seat-20260906`, branch `feat/four-seat-hotseat-20260906`, clean base `1bc80911373b5a88b3cb7ebe0befb02c5716cb80`. Parent worktree, installed mod, provider, tuner, desktop, and existing runs are untouched.

Implementation plan:

1. Generalize ordered completed-turn accounting and next-seat selection to an explicit roster of two through four distinct seats; reject unsupported counts and identities. Keep engine turns and lease release authoritative.
2. Parameterize the existing shipped-API startup recipe for contiguous fresh seats 0 through N-1, including all human passwords/readiness, closure of every extra major slot, post-host/native-refresh guards, all nonlocal reflags, and an exact complete GameCore census. Fail before desktop input on unsupported configured rosters.
3. Extend the fake engine roster, validator, and dashboard to the same seat count. Exercise four ordered seat completions per round and refusal of skips, duplicates, wrong turns, or missing human seats; preserve one tuner client and no desktop input in fake runs.
4. Add an opt-in four-model 100-round configuration with unchanged per-agent provider/request limits and movement allowance. Capture focused checks and Ruff; request independent review before parent integration or any live run.

Live startup remains unverified until a separate fresh four-seat run proves the existing slot APIs survive hosting, save/load demotion, reflagging, and all four engine handoffs. No new host API is assumed and no live probe runs from this branch. Elimination or an unexpected major seat aborts rather than silently shrinking the required roster. Structural fake/replay checks are separate from live engine-state proof.
