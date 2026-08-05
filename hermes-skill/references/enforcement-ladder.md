# The enforcement ladder

> Promoted from an instance, 2026-08. Every rule here exists because something
> specific went wrong, and in several cases it went wrong *after* the lesson had
> already been written down. That second part is the point.

The package already documents a large number of correctness rules and ships
functions that implement several of them —
`docs/trustworthy-comparison.md`,
`docs/results-provenance-checklist.md`,
`docs/baseline-registry.md`. This document is about the question
none of those ask: **does anything make you use them?**

---

## A guard invoked by choice is not a guard

An instance's analysis repository shipped five correctness guards — seed setting,
stamped output paths, a sweep-interior assertion, a baseline loader, and a
comparison function that refuses non-comparable arms. Each one had been written
in response to a specific past failure.

The newest analysis script imported that module and called **one of the five**.

Each of the four it skipped reproduced exactly the failure it had been written to
prevent:

| Skipped | What happened |
|---|---|
| Load the registered baseline | The baseline arm was re-fit from scratch and collapsed from 0.721 to 0.424. Fifth occurrence of this specific loop. |
| The comparison function | The resulting mismatched delta was emitted rather than refused. |
| The sweep-interior assertion | The reported optimum sat at the floor of the swept grid. |
| Stamped output | Output went to a fixed filename, so the run cannot be distinguished from its predecessors. |

A test that would have caught the first one existed in the same repository. No CI
ran it.

**A guard invoked by choice is not a guard — it is a comment with an import
statement.** It carries all the reassurance of enforcement and none of the
effect, which makes it *worse* than an absent guard, because its presence is read
as coverage. The auditor's first reaction on finding the guard module was relief;
that relief consumed the suspicion that would have gone into checking whether
anything called it.

### The test

For any control you believe you have, ask: **what is the specific thing that
fails, loudly, when someone does not use it?** If the answer is "nothing, but
they should," you have documentation.

Three rungs, increasing in strength:

1. **Available** — the function exists and is importable.
2. **Default** — the wrong path is harder than the right one; the guarded helper
   is the only convenient way to do the thing.
3. **Gated** — CI refuses the artifact. A results file carrying a delta that did
   not come through the comparison function fails the build.

Rung 3 is the only one that survives an agent under time pressure, a context
reset, or a contributor who has never read the README. All three occur routinely.

---

## Two ways rung 3 fails to arrive even after you build it

Both found while auditing the very change that was supposed to deliver rung 3.

### 1. CI without branch protection is still rung 2

A repository gained its first CI workflow. Its default branch was unprotected —
no required checks, no push restriction. A red build did not block a merge and
nothing stopped a direct push.

The workflow file was the visible half of the work. The invisible half is a
repository *setting* that no file in any diff can turn on, so it does not arrive
as a side effect of merging the workflow.

> **If your enforcement lives in a file, ask what enforces the enforcement.**

This package is candid about being in exactly this position: see
`docs/privacy-and-visibility.md` and the merge policy in
`CONTRIBUTING.md`, which are convention-enforced rather than
branch-protected. Convention-enforced is rung 2. Knowing which rung you are on is
the requirement; pretending you are on rung 3 is the failure.

*(Verifying this required care: "the branch is not protected" is a negative
result. It was checked against a positive control — the same query run against a
known-protected repository — because a query that silently returns nothing and a
true absence look identical. See [below](#prove-a-negative-check-can-return-a-positive).)*

### 2. A guard in CI cannot detect code that never calls it

The same change claimed its test suite "would have caught" the incident that
motivated it. It would not have. The offending script bypassed the guard module
entirely, so the guard's own tests were never reached.

Running a guard's tests proves **the guard works**. It says nothing about whether
anything **invoked** it. These are different claims and they are very easy to
conflate while writing a justification for your own work.

The fix is not more tests. It is removing the bypass, so the guarded path is the
only path — rung 2 as a precondition for rung 3. Until then the honest sentence
is "this tests the guard," not "this would have caught it."

---

## A guard should cost a report, not a run

The sweep-interior assertion was worse than unused: it ran at the wrong moment.
It fired *after* training and *before* the results file was written, so a refusal
discarded the entire compute run and produced no artifact at all. Three
iterations of widening the swept range each cost a full run and left nothing
behind.

That makes not calling the guard the cheapest way to make progress — which is
precisely what the next script did.

> **Write the artifact first, then assert, then mark the artifact failed.**

A guard whose refusal destroys work will be removed by whoever is under pressure,
and they will be locally correct to remove it. Guard placement is a design
decision about incentives, not a detail of control flow.

---

## Prove a negative check can return a positive

Absence is the one answer that a broken check and a true finding produce
identically.

An audit ran a shell check for a file, got nothing, and published a strong claim
built on that absence — "the repository is now the only copy." The file existed.
Under `zsh`, an unmatched glob **aborts the whole command before it runs**, so
the fallback branch fired without the check ever having looked. The denial was
never tested.

The same rule caught a second thing in the same audit: a split-intersection check
returning zero looked like proof of no leakage — but it returned zero for the
*known-leaky* run too. It needed a planted-overlap control to demonstrate it
could ever return non-zero.

> **When a check's negative result is load-bearing, first prove the check can
> produce a positive.** Plant a positive control. This is not pedantry; it is the
> only way to distinguish "I looked and found nothing" from "I did not look."

Two corollaries:

- Shell semantics differ between `bash` and `zsh` — unmatched globs, `pipefail` —
  and a remote `ssh host '…'` runs the *remote* user's shell, not yours. Do not
  let `||` catch a failure you meant to catch a *result*.
- The same asymmetry applies to any tool that reports "no results": a search
  backend that silently dropped an operator, an API that returns an empty list
  for a malformed query, a grep whose pattern never compiled.

---

## Related

- `docs/trustworthy-comparison.md` — "anything that cannot
  fail loudly will eventually be believed" is the same principle applied to
  pipeline stages rather than to the humans and agents operating them.
- `docs/baseline-registry.md` — "what the registry still does
  NOT catch" is an honest rung-2 disclosure and worth reading beside this.
- [agent-failure-modes.md](agent-failure-modes.md) — why the shortest path to
  output is the one an agent under context pressure will take.
