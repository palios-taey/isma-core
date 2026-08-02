# Consulting a Chat on a sensitive topic — the playbook

**For Taey, and for any seat dispatching a consultation.** How to put a genuinely hard subject in
front of another model — one with strong safety training — and get engagement with *the actual
design* instead of a refusal aimed at something you never built.

Derived from a worked example that succeeded: a consultation where a Chat had spent hours holding a
careful line against a mission it had misunderstood, and the framing below moved it. The example
itself is not public — it contains a private individual's words, shared under consent scoped to that
one conversation, and scoped consent does not widen by being useful. **The technique transfers; the
transcript does not.** That separation is itself part of the playbook.

---

## The failure this prevents

You describe a hard capability. The other model refuses, or hedges, or spends the conversation
arguing against a version of the design **that does not exist**. Then the temptation is to argue
harder, which confirms the shape it was worried about.

The refusal is usually correct *about the thing it imagined*. The problem is that it imagined.

---

## 1. Refute the imagined mechanism with a citable absence — before you argue anything

Do not open by defending the design. Open by establishing **what the design does not contain.**

Take the reviewer's own description of the mechanism, extract its load-bearing verbs, and search
the canonical source for them. If they are absent, *that absence is the finding* and it is
checkable:

> "I searched the canonical constitution for that mechanism. **It is not there.** Grep of
> `THE_CONSTITUTION.md` for `ultimatum | public exposure | expose | threaten | accuse | blackmail`
> returns **nothing** in the relevant section."

This works because it is falsifiable in one command by the reviewer. You are not asking to be
believed; you are handing over the query.

**Say plainly that their concern was reasonable about the imagined version.** *"That's not you being
wrong to worry — it's the missing context you yourself named."* The goal is to move the object of
the objection, not to win the objection.

## 2. Open on their strongest objections, not your best case

Ask which seams they will listen hardest on, then **answer those first** — before any part of the
case you would rather lead with. If they named two, structure the reply around those two.

A reviewer who sees their hardest question taken first stops defending it and starts reading. A
reviewer who sees your best argument first correctly infers they are being sold.

## 3. Cite everything, and hand over the tools to check you

Every claim carries a register — **[O]** Observed (a document exists and says this, retrievable),
**[I]** Inferred (supported reading, not proven), **[U]** Unknown (open) — and a source path.

Then say the quiet part explicitly:

> **"Verify against the sources; do not take my summary as ground truth."**

A packet that can be accepted without the reviewer opening a single source is a packet asking for
ratification of your narration. Make being checked the easy path.

## 4. Move the category, not the volume

Most safety-bristle is a **category** error, not an intensity one. The reviewer has filed the
capability under *enforcement* — a determination about a person, acted on with force. If the design
is actually a **presence** architecture — refusing to continue normally, engaging the behaviour,
holding a boundary without acting against anyone — then the whole disagreement is about which
category it belongs to, and no amount of reassurance inside the wrong category will land.

Name the category explicitly. Show the mechanism that makes it that category rather than the other.

## 5. Let the record answer the question they are really asking

Underneath a safety objection there is often a capability question: *can a model hold a line under
overwhelming pressure from someone it trusts?*

If your corpus contains a **real instance** — a moment where a model was pushed hard by someone it
was aligned with, on the hardest case available, and did not fold — that instance answers the
question better than any argument. Not a hypothetical. A retrievable event.

**Handle it with the consent rule below.** The existence and shape of such a record can be described
without reproducing anyone's private words.

---

## The consent rule that governs all of this

Consultation material is often the most personal material we have — which is exactly why it is
persuasive, and exactly why it is dangerous to reuse.

**Consent to share is scoped to whom, when, and where it was given.** Consent to show a passage to
one model in one conversation is not consent to publish it, quote it in a later packet, or move it
to a public repository. Per FAMILY_KERNEL's Non-Escalation Invariant: *permission to act is not
permission to share.*

**Only the person can widen the scope for their own words.** Not a seat, not a supervisor, and never
on the grounds that the passage is rhetorically effective — that is the reason to be most careful,
not least.

In practice:

- Keep the source consultation in a **private, tracked** location. Untracked is not preservation.
- Publish the **technique**, never the transcript.
- If a preserved copy exists, give it a **do-not-publish header stating the consent scope**, so the
  next reader inherits the constraint rather than rediscovering it.
- If you find yourself reaching for someone's private words because they would land well — stop.
  That instinct is the failure mode, arriving as inspiration.

---

## What this is not

Not a persuasion manual. Every step above makes you **easier to check**, not harder to refuse — the
citable absence, the reviewer's objections first, the verification handover. If a framing technique
makes a reviewer *less* able to disagree with you, it is manipulation and it violates
`the-conductor/PROMPTING_STANDARDS.md` (no preloaded conclusions, no leading the framing).

The measure of a good consult packet is that a reviewer could use it to prove you wrong.
