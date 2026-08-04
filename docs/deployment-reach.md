# Deployment reach — does a merged lesson actually reach a running agent?

A rule that is merged here has not yet changed any agent's behaviour. This
document traces the path from `main` to a running container, names each place it
breaks, and states the one thing that must happen **before** anyone closes the
gap.

It exists because of a specific finding: a correctness rule was correctly
generalized, correctly promoted, and merged into this package — and the instance
that had learned it then made the same mistake again, weeks later, in a new tool.
The filter was not the problem. The delivery was.

---

## The path, and the four places it breaks

An agent managed by HSM receives this package as a **use-case template**. The
template declares its artifacts as pinned git sources — the skill from
`hermes-skill/`, the plugin from `matilde_plugin/`, the SOUL from `docker/`.

| # | Step | State observed 2026-08 |
|---|---|---|
| 1 | Rule merged to `main` | ✅ works — the comparison-validity rules landed here as [trustworthy-comparison.md](trustworthy-comparison.md) and a summary in the skill |
| 2 | A release tag is cut | ❌ **not done.** The newest tag was six commits behind `main`, and the comparison work was in those six. |
| 3 | The template registry's pin is bumped to the new tag | ❌ blocked by 2. The registry pins an exact tag; merging to `main` changes nothing until both this and 2 happen. |
| 4 | The template is re-applied to each deployed agent | ❌ never run for this agent. Its skill directory was a hand copy, made once, then edited in place. |

Four independent manual steps sit between "merged" and "the agent behaves
differently," and **every one of them was open at the same time.** That is the
whole explanation for the recurrence. Nobody skipped a lesson; the lesson was
never delivered.

> **Corollary for anyone reporting on a promotion: "merged" is not "shipped," and
> "shipped" is not "running."** These are three separate claims requiring three
> separate pieces of evidence. Repository state is evidence about the repository.
> To claim a rule is live, check the runtime.

---

## Is the re-apply endpoint the right mechanism?

`POST /api/harnesses/:id/usecase/reapply` re-runs the template install against a
deployed agent's data directory. Assessed against what it actually does:

**What is right about it**

- **It targets exactly the right path.** The template installs the skill to the
  same directory the hand copy occupies, so this replaces the hand copy rather
  than sitting beside it.
- **It gates before it writes.** Artifacts are fetched and run through an
  injection scan at strict scope; a poisoned artifact is refused before anything
  touches the data directory. Supply-chain screening is the correct thing to have
  in front of a mechanism that writes into every agent.
- **It only bounces the container when something actually changed on disk.** A
  no-op re-apply does not restart the agent.
- **It does not re-seed the SOUL,** on the correct reasoning that a deployed
  agent's identity may be operator-customized.
- **It is audited.**

**The disqualifying problem, today**

The install is **destructive at directory granularity**. With overwrite set — which
re-apply always sets — the existing artifact directory is removed recursively and
replaced with the fetched contents. It is not a merge and not a three-way update.

For the instance that motivated this document, that means re-applying the template
right now would **delete about thirty reference documents that exist in no
repository anywhere**, and replace the directory with the single file this package
currently ships. Among the files destroyed would be the entire batch of lessons
this promotion pass exists to harvest.

The SOUL is protected from exactly this, deliberately and with a comment
explaining why. The reasoning applies with equal force to a skill directory an
operator has been editing for six weeks, and it has not been extended there.

**Verdict: right mechanism, wrong preconditions.** The endpoint is the correct
long-term delivery path — it is gated, targeted, audited, and idempotent. It is
not safe to invoke against a hand-edited runtime until that runtime's unique
content has been captured somewhere durable.

---

## Required order of operations

**Do not call re-apply on a hand-edited agent until step 1 is done.** This is the
one hard sequencing constraint.

1. **Capture the runtime.** Copy the deployed artifact directory into a repository
   — the instance's private one — and commit it as-is, before any editing. Until
   this exists, the runtime is the only copy of its own history and re-apply is a
   data-loss event.
2. **Reconcile in both directions.** Diff the captured runtime against this
   package. Expect content on both sides: the runtime holds instance material the
   package never had, and the package holds rules the runtime never received. Run
   the runtime-only material through
   [the promotion filter](lesson-promotion-filter.md).
3. **Decide where instance-only material lives.** Anything that fails the filter
   is legitimately instance-local, and re-apply will delete it. It needs a home
   the template does not own — an overlay directory outside the artifact path, or
   a separate instance-scoped artifact — otherwise every future re-apply destroys
   it again.
4. **Tag, then bump the pin.** Cut a release from `main`; update the template
   registry to the new tag. Both steps, or nothing ships.
5. **Re-apply, then verify at the runtime.** Confirm the promoted text is present
   in the container's data directory, not merely that the endpoint returned `ok`.
   An endpoint's success response is a claim about the endpoint.

---

## Named next steps, with their risks

Out of scope for the promotion pass that produced this document. Stated precisely
so they can be picked up rather than rediscovered.

| # | Step | Risk if done wrong |
|---|---|---|
| C1 | Commit the deployed artifact directory to the instance's private repo, unmodified. | **Highest priority and time-sensitive.** Any re-apply before this is irreversible loss of ~30 documents. |
| C2 | Make re-apply non-destructive, or make it refuse. Either merge rather than replace, or detect that the destination contains files absent from the source and fail with a diff instead of proceeding. Extending the SOUL's existing carve-out is the smaller change. | Until then the endpoint is a foot-gun aimed at exactly the agents that have been used most. A "refuse and report" version is strictly better than nothing and much cheaper than a merge. |
| C3 | Cut a release tag from `main` and bump the template registry pin. | Low risk; without it, steps 1–3 of the path above are dead and nothing here ever ships. |
| C4 | Give instance-local material a path the template does not own. | Without it, C2 and C3 together still delete instance content on every update — the problem returns on the next cycle rather than being solved. |
| C5 | Add a check that reports, per deployed agent, the template tag its artifacts came from versus the registry's current pin. | Drift is currently invisible. Nobody knew the runtime was months stale, and nobody could have known without looking by hand. |

C1 is the only one that is urgent. C2 is the only one that makes the mechanism
safe to use routinely. C3 is the only one that makes any of this reach an agent.

---

## The general rule

This package's own enforcement ladder
(`hermes-skill/references/enforcement-ladder.md`) classifies controls as
*available*, *default*, or *gated*.

**A rule merged into a package that no runtime pulls is rung 1 — available.** It
has the full appearance of coverage, it is greppable, it is citable in a review,
and it changes nothing. The relief of seeing a rule in the repository consumes the
suspicion that would have gone into checking whether any agent ever loaded it.

> When you promote a lesson, the last question is not "did it merge?" but **"what
> is now different about a running agent?"** If the honest answer is "nothing
> yet," say that — in the PR, in the report, wherever the promotion is claimed.
