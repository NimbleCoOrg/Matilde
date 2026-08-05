# The lesson promotion filter

How to decide whether something an instance learned the hard way belongs **here**
— in the package that ships to every instance — or stays **there**, with the
instance that learned it.

[promotion-and-upstream.md](promotion-and-upstream.md) says *how* to promote:
branch, strip the particulars, PR, let the gate run. This document is the missing
half — *what* to promote. It exists because "strip the particulars, keep the
method" is easy to agree with and hard to apply, and because the interesting
cases are the ones where a rule is **stated in one domain's vocabulary but its
mechanism is domain-free**. A filter that only catches the easy cases is not
worth having.

---

## The unit of promotion is a rule, not a file

Start here, because it dissolves most of the difficulty.

Instance documents are mixed. A single reference file will contain a genuinely
universal rule, a worked example that names a private dataset, and a parsing
recipe that is meaningless outside its domain — often in the same paragraph.
Asking "should this file be promoted?" has no good answer. Asking "should this
*rule* be promoted, and what evidence can come with it?" always does.

So: read the instance document, extract the rules as separate claims, and run
each one through the tests below on its own. Expect a single source document to
split — some rules up, some staying, and the evidence for a promoted rule often
needing to be rewritten even when the rule itself passes untouched.

---

## The four tests

A rule is promoted only if it passes **all four**. They are ordered cheapest
first, so a rule that fails T1 costs you one sentence.

### T1 — Substitution: does the mechanism survive a domain swap?

Rewrite the rule with every domain noun replaced by a variable. Then ask whether
the rewrite still explains **why the failure happens**.

- If the explanation survives, the domain vocabulary was decoration. **General.**
- If the rewrite collapses into a truism ("be careful", "document things"), the
  content was in the particulars. **Specific.**

The common error is to judge by vocabulary. Vocabulary is the least reliable
signal available, and it misfires in *both* directions:

| Rule as written | Substituted | Verdict |
|---|---|---|
| "Score every arm of the comparison with the same matcher — one arm counted a single-sample overlap as a hit." | "Score every arm with the same evaluation function." | **General.** The mechanism — a more permissive scorer inflates the arm it is applied to — never mentioned the domain. The domain words were noise. |
| "Look at your spectrograms before reporting a segmentation number." | "Look at your figures before reporting a number derived from them." | **General.** Sounds maximally domain-bound; the mechanism ("a result you have never viewed is a number, not an observation") is universal. |
| "The guard fires after training and before the result writes, so a refusal discards the whole run and produces nothing." | "A guard whose refusal discards the work costs a run, not a report." | **General.** A hyper-specific artifact carrying a fully general rule about where in a pipeline a check belongs. |
| "The identifier is at the fifth underscore-delimited position of the filename." | "The identifier is parseable from the filename." | **Specific.** The substituted form is content-free; all the value was in the parsing recipe. |
| "Filter the label taxonomy from 41 families to 14 by a minimum-count threshold." | "Any data-selection threshold must be recorded, and the result shown at a second cut." | **General — but only the second sentence.** The taxonomy's identity stays; the rule about undocumented selection thresholds travels, and the counts travel with it as derived evidence (T3). This is the split described above, inside one sentence. |

**T1 is the test the brief's hard case needs.** "Score every arm with the same
matcher" reads as bioacoustics and is a general experimental-design rule; T1
resolves it correctly and cheaply because it asks about the *mechanism*, not the
*words*.

### T2 — Counterfactual audience: would a stranger behave differently?

Would an agent working in a completely unrelated field, who has never heard of
this instance's domain, **do something different** because of this rule?

Not "would they nod." Would their behaviour change.

- "A capability limitation you wrote down is a claim with a date on it; re-check
  before declining on the strength of it." → A legal-research agent carrying a
  stale "I cannot read PDFs" note refuses work it could do. **Behaviour changes.
  General.**
- "Your metric's ceiling is the reliability of your ground truth." → Anyone
  scoring against human labels — content moderation, clinical coding, relevance
  judgments — is currently unable to say whether their last improvement was
  signal. **Behaviour changes. General.**
- "The evaluation script lives at this path and takes these flags." → Nobody
  outside the instance can act on it at all. **Specific.**

T2 catches rules that pass T1 by being *stated* generally while only ever
mattering to one setting. It is the test for false generality, where T1 is the
test for false specificity.

### T3 — Independence: can the rule and its evidence be separated?

Two conditions, both required:

1. **The rule must be statable without the private particular.** If you cannot
   write the rule down without naming the collaborator, the dataset, the
   filename, or the path, it is not generalized yet. This is the existing
   promotion doctrine — *the act of generalizing is the proof that nothing
   leaked* — restated as a test.
2. **The evidence must survive sanitization and still be believable.** Evidence
   is what makes a rule stick rather than read as a platitude, so it should come
   along. It comes along **reduced to derived quantities and structural shapes**.

What that reduction looks like in practice:

| Instance evidence | What travels | What stays |
|---|---|---|
| "4 of 12 test subject IDs also appear in training: *(four identifiers)*." | "4 of 12 test subjects also appeared in training." | The identifiers. |
| "*(a recording filename)* is in train and *(a near-identical recording filename)* is in test — the annotator's own filename says they are the same individual." | "Two files the annotator's own naming marked as the same individual landed on opposite sides of the split." | The filenames. They encode site, subject and date; they are the raw material, not a derived metric. |
| "A hop-length/normalisation mismatch dropped F1 from 0.826 to 0.595 on identical data." | The whole thing. Derived metrics are publishable; nothing here identifies anyone. | Nothing. |
| "`print(f\"Baseline: F1=0.731\")` reached a completion notification and was read back as a measurement." | The whole thing. A code shape is not a particular. | Nothing. |
| "The threshold was tuned on the test set for the *(named)* corpus at *(named site)*." | "The threshold was tuned on the reported set." | The corpus name and the site. |

**The useful discovery is that T3 makes privacy and generality the same test.** A
rule that still needs the private particular in order to be convincing has not
been generalized — it is a case report wearing a rule's clothes. Sanitizing it
does not damage a genuinely general rule; it damages a rule that was never
general, and that damage is the signal.

Hard constraint, non-negotiable, no override path: this package is public.
Nothing promoted may carry a collaborator or participant name, an unpublished
dataset's specifics, a raw data filename, a site or location label, a private
filesystem path, or an infrastructure identifier (channel, guild, bot, account).
Derived metrics — scores, deltas, counts, confusion structure — are publishable
and should be kept, because they are what makes a rule believable.

### T4 — Recurrence, or a mechanism that predicts it

The package's existing bar for promoting a *technique* is repetition: one use is
an anecdote, three is a method. A *lesson* is different — a lesson earns
promotion by demonstrating that writing it down locally was not enough. Promote
if **either**:

**(a) It recurred after it had already been written down.** This is the strongest
possible evidence, and it is common. One instance recorded the same
comparison-validity error three times in five weeks; the third occurrence
happened five days after the second was written up, in a new tool with its own
scoring loop, precisely because the lesson lived in prose that the new tool's
author never read. Prose in one instance demonstrably did not hold. That is an
argument for shipping it where every instance gets it.

**(b) The write-up names a structural asymmetry that guarantees it will not
self-correct.** Some failures are silent by construction and so will never
generate the error that would expose them. Example: a false "I cannot do X" is
silent forever, while a false "I can do X" fails loudly on first attempt — so
self-imposed limitations are exactly the beliefs least likely to ever be tested.
That asymmetry predicts recurrence without waiting for it. A rule that names one
qualifies on first occurrence.

If a lesson has occurred once and offers no mechanism, it stays local. Come back
when it happens again — and note that it happening again is itself the finding.

---

## Worked verdicts

Run against one instance's accumulated material, kept here because a filter's
value is in the cases where it says *no* and the cases where it surprises you.

### Promoted

| Rule | Passed on |
|---|---|
| An optional guard is documentation; enforcement has three rungs (available / default / gated). | T4(a) — the guards existed, were imported, and four of five were not called. |
| A guard should cost a report, not a run. | T1 — a scheduling rule about checks, stated via one script. |
| Running a guard's own tests proves the guard works, not that anything invoked it. | T4(b) — the conflation is invisible until something bypasses the guard. |
| Your metric's ceiling is the reliability of your ground truth. | T2 — changes what any human-labelled evaluation can conclude. |
| Report the metric-versus-tolerance curve, not a single operating point. | T1 — "tolerance" substitutes cleanly for any matching-slack parameter. |
| Leakage has a **unit**; disjointness at the file level is not disjointness at the subject level. | T1, and it extends a limitation the package already documents. |
| If a configuration knob moves the metric more than the effect you are claiming, you are measuring configuration. | T1 — the magnitudes are derived metrics and travel intact. |
| Per-artifact validation is structurally blind to reversals across runs; diff against the previous run first. | T4(a) — a validation pass returned a clean verdict on a run whose headline conclusion had reversed. |
| Conversational pressure shifts attention from verification to resolution. | T4(b) — resolution and verification compete for the same capacity. |
| Default affirmation makes agreement uninformative. | T4(b) — the agreement sounds identical whether the idea is good or bad, so it cannot be checked. |
| A mentioned date is not a deadline until it can name who set it, what type it is, and what is owed. | T4(a) — three occurrences of a derived value acquiring unearned authority. |
| A stated limitation is a claim with a date on it. | T4(b) — the silence asymmetry above. |
| Repetition launders inference into observation; restate the derivation with the number. | T4(a) — three occurrences in five weeks. |
| When a check's negative result is load-bearing, first prove the check can return a positive. | T4(a) — an unmatched glob aborted a command, and the empty result was published as verified absence. |

### Kept local

| Material | Failed on |
|---|---|
| The taxonomy's identity and the specific class filter — *which* taxonomy, and which classes it kept. The raw counts are derived quantities and travel with the rule under T3, as its evidence. | T1 — the filter itself substitutes to a truism; only the "record your selection threshold" rule travels (promoted as E5 in `hermes-skill/references/evaluation-validity.md`). |
| Identifier parsing from filenames. | T1 — the recipe *is* the content, and the filenames are raw material anyway. |
| Script paths, flags and per-tool evaluation entry points. | T2 — unusable outside the instance. |
| Corpus names, sites, per-recording labels, collaborator names. | T3 — hard block. |
| Chat platform guild / channel / bot identifiers used by a scheduled job. | T3 — infrastructure identifiers, and they sit next to a credential location. Looks like inert config; is not. |
| The specific date that was mistaken for a deadline, and who said it. | T3 — the *rule* about deadline provenance promoted; the incident stays. |
| An archival corpus chosen as one study's field-condition arm. | T3 — a public archive's name becomes a particular when it identifies one study's unpublished design. Public provenance does not make it non-identifying. |

### The case that changed the conclusion

"Score every arm of a comparison with the same matcher" passes every test — and
**was already promoted**, before this pass ran. It is in the package as
[trustworthy-comparison.md](trustworthy-comparison.md), and it is summarised in
the skill.

The instance then made the error again, in a new tool, weeks later.

The filter was not the bottleneck. The rule had already cleared it, been
generalized well, and shipped. What failed is that the instance's runtime never
received the package version, because deployment was a hand copy that had drifted
in both directions from the repository.

**So: a filter is necessary and it is not sufficient.** Promotion that does not
reach a runtime is a rung-1 control — available, not enforced — which is exactly
the failure this filter's top-ranked promoted rule describes. Anyone running this
process should treat "did the promoted rule reach a running agent?" as part of
the process, not as someone else's problem. See
[deployment-reach.md](deployment-reach.md).

---

## The process

1. **Collect.** Take the instance's changed reference material since the last
   promotion pass. Diff the deployed tree against the repository copy **in both
   directions** — a hand-deployed runtime accumulates content the repository
   never saw, and misses content the repository has.
2. **Split into rules.** One claim per line. Do not promote files.
3. **Run T1 → T4** on each rule, cheapest first.
4. **Sanitize the evidence**, per T3. Reduce to derived quantities and structural
   shapes.
5. **Write it where it will be loaded**, not merely where it will be stored — see
   below.
6. **Run the gate.** `python3 scripts/check_sanitization.py --full-tree`, plus
   `python -m pytest tests/`. A deterministic hit is a hard stop with no override
   path. A gate flag on promoted text is the gate working; fix the text.
7. **Record the rejections** in the PR, with the test each failed. The rejected
   list is how the next person calibrates, and it is the only evidence that the
   filter was applied rather than assumed.
8. **Check the promoted rule can reach a runtime.** If it cannot, say so
   explicitly rather than closing the loop on the repository.

## Where a promoted lesson goes

Follow the shape the package already uses: **the rule in the skill, the case in
`docs/`.**

- A short, imperative statement of the rule goes in `hermes-skill/SKILL.md`, at
  the point in the workflow where an agent would violate it, with a pointer to
  the full document. This matters more than it sounds. A reference the agent
  never loads is rung 1 on the enforcement ladder — available, not enforced — and
  the ladder is itself one of the promoted rules. Do not promote a lesson about
  optional guards into a file nothing opens.
- The full case study, with its sanitized evidence, goes in `docs/` as its own
  file or as a section of an existing one. Prefer extending an existing document
  when the new rule is a refinement of one already there; prefer a new file when
  it is a distinct concern.
- Anything mechanically checkable gets a test in `tests/`. This is rung 3, and it
  is the only rung that survives an agent under time pressure, a context reset,
  or a contributor who never read this file.

## When not to promote

- The rule is true but nothing acts on it. Promoted prose that changes no
  behaviour is package weight.
- The rule is a restatement of one already in the package. Extend the existing
  document; a second copy that drifts is worse than none.
- The rule needs its particulars to be convincing. Under T3 that means it is not
  general — it is a case report. It stays.
