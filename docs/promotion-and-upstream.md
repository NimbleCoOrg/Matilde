# Promotion & Upstream

How a technique that proved itself under operational pressure moves out of your
private overlay and into shared infrastructure — without leaking the particulars
that earned it.

There are two promotion paths, and they are not the same thing. Path A is
**yours**: you generalize a method and land it in your own `{{PACKAGE_NAME}}`
package. Path B is **shared**: you propose a genuinely generic pattern to the HSM
base package that every deployed agent inherits. The control model, what actually
merges, and the blast radius are different in each. Read both before you assume
the one you want.

| | **Path A — your package** | **Path B — HSM base (upstream commons)** |
|---|---|---|
| Who controls the merge | You (maintainer of `{{PACKAGE_NAME}}`) | NimbleCo base-package maintainers |
| What actually merges | Your generalized artifact, your bytes | A **re-written** implementation of your pattern — not your bytes |
| Blast radius | Your package's instances | **Every deployed agent** that ships from the base |
| Review model | Gate + maintainer review (self-review OK for a solo operator; the gate is the net) | Gate + maintainer review + **full re-authoring** of the contribution |
| Speed | Fast — autonomous, your call | Slow by design — proposal → re-implementation |
| You submit | The branch | A **description of the pattern** + the generalized artifact as a *reference* |

---

## Path A — Promote into your own package

This is the everyday loop. You control it end to end.

A technique lives in your private overlay first — a per-engagement skill under
`instance/`, a learning captured during one `{{DOMAIN_NOUN}}`, a tool you hacked
together for a single job. That's where it should live until it has earned
generalization.

**Promote when it has proven useful across multiple {{DOMAIN_NOUN}}s.** One use
is an anecdote. Three is a method. The bar is repeatability, not perfection —
if the technique only worked once, it hasn't earned generalization yet.

### How to promote

1. **Strip the particulars, keep the method.** Take the working version out of
   the overlay and rewrite it so nothing names a specific subject, client,
   target, dataset, document ID, codename, date, or figure. What remains is the
   *technique* — how you grade a source, how you structure a handoff, how you
   shape a query — not *who you ran it on*.

   The act of generalizing **is** the proof that nothing leaked. If you can't
   describe the method without the particulars, it isn't generalized yet.

   **This step has its own document.** Deciding *which* rules survive the strip —
   especially the ones stated in one domain's vocabulary whose mechanism is
   domain-free — is [lesson-promotion-filter.md](lesson-promotion-filter.md). It
   gives four ordered tests, worked verdicts on real material, and the rule that
   the unit of promotion is a **rule**, not a file: expect a single instance
   document to split, some of it promoting and some staying.

2. **PR it from a branch into `main`.** Branch from `main`, open the PR against
   `main`.

3. **Let the gate run.** The sanitization gate
   (`scripts/check_sanitization.py`, wired into
   `.github/workflows/sanitization.yml`) runs on the diff:
   - **Layer 1 — deterministic** (secrets/PII: API keys, private keys, emails,
     phones, IPs). No dependencies, fails closed, **hard fail on any hit.** A red
     deterministic check is a stop, not a discussion.
   - **Layer 2 — semantic** (use-case particulars, per your
     `sanitize.config.json`). Advisory → routes to human review. Skipped with a
     warning if no `ANTHROPIC_API_KEY` is present; CI can force it with
     `--require-semantic`.

4. **Review and merge.** A maintainer review is the discipline. If you're a solo
   operator, that's a self-review — and that's fine, because the deterministic
   gate is the safety net underneath you. Do not merge on a red deterministic
   check; resolve it first. A semantic flag is advisory: clear it in the PR, or
   override it with judgment if you're confident it's clean.

That's it. Your package, your call, autonomous.

### The gardener pattern (keeping the base clean over time)

Per-PR diff scanning catches what *this change* touches, but it can't see drift
that accumulated across many small PRs. Running the full-tree scan
(`scripts/check_sanitization.py --full-tree`) on **every** PR is flaky and slow,
so don't.

Instead, let a **stewardship ("gardener") agent** run the `--full-tree` audit on
a schedule — nightly, weekly — and **propose** cleanups as its own PRs. The base
stays clean without inflicting full-tree noise on every contributor. The audit
lane and the diff lane are deliberately separate: diff mode gates contributions,
full-tree mode tends the garden.

---

## Path B — Submit upstream to the HSM base package

The HSM base package is the **shared commons**: the plugins, skills, and base
soul that every agent built on `hermes-agent-mt` inherits. When something in your
package turns out to be generic — useful to *any* team, not just your domain — you
can propose it upstream to the base package (`infra/artifacts.json` in
hermes-swarm-map).

Read this part carefully, because the merge model is **not** what you're used to.

### The security policy — stated plainly

**NimbleCo does not merge external PRs into the base package as submitted.**

A submission to the base is treated as a **proposal**, not a patch. For security,
the maintainers will **close your PR and re-write the contribution from scratch**
against the pattern you described.

The reason is blast radius. The base package ships to **every deployed agent**. A
single injected line in the commons — a subtly malicious dependency, a poisoned
prompt, an exfil hook — has enormous reach. Accepting external bytes into that
surface is a supply-chain risk to every downstream consumer. So the policy is
absolute and structural, not a judgment on any contributor: **your exact bytes do
not enter the base.** The capability lands; the implementation is authored by a
maintainer who owns the result.

You get **credit**. The capability **lands**. Your bytes **do not merge.** That
trade protects every downstream agent from supply-chain injection via the commons,
and it's the price of the commons being trustworthy enough to inherit blindly.

### What to submit

- A **clear description of the pattern**: what it does, why it's generic, what
  problem it solves for any team.
- The **generalized artifact as a reference** — so a maintainer can see exactly
  what behavior to reproduce. Treat it as a worked spec, not a merge candidate.

### What not to expect

- A fast-path merge of your code.
- Your specific implementation, comments, or structure surviving into the base.

If you need it merged *as written*, that's Path A — it stays in your package.

### Why contribute upstream at all

From the polycentric-commons thesis, the incentives are real:

- **Your methodology becomes the default.** Once a pattern is in the base, it's
  what the *next* team starts from. You shape the shared infrastructure instead of
  re-deriving it privately forever.
- **Accepted patterns cut your future merge friction.** Every generic thing you
  push upstream is one fewer local divergence to reconcile when you sync from the
  base. Minimal upstream divergence keeps your updates low-conflict.
- **Lead-time stays your moat.** You generalize and contribute *after* the
  technique has already paid off in your engagements. The commons gets the
  method; you keep the months of operational learning that made it work. Generic
  methodology becomes shared infrastructure — your edge does not.

---

## Quick decision

- Proven across multiple `{{DOMAIN_NOUN}}`s, useful mainly to *you/your domain* →
  **Path A.** Generalize, PR into your `main`, gate runs, you merge.
- Genuinely generic, useful to *any* team → **Path B.** Propose to the base;
  expect a re-write, take the credit.
- Not generalizable without naming particulars yet → **neither.** It stays in
  `instance/`. Come back when the method stands on its own.
