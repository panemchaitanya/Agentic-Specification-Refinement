# FR/NFR extraction guidelines (Stage 2 ground truth)

Status: evaluation-lane draft, 6 Nov 2026, fixed before extraction starts.
These guidelines are what the two independent extractors apply when
pulling FRs/NFRs from reference specs; Cohen's kappa is computed on their
independent passes before adjudication (proposal section 4). They also
define the FR/NFR typing the pipeline is measured against - they are the
answer to the repo's known gap "PRD statements typed as FRs even when
they are really quality statements".

## Definitions

- **Functional requirement (FR):** a behavior the system must perform -
  an actor, an action, and an object. Test: you can write a functional
  test case for it ("user resets password", "admin exports CSV").
- **Non-functional requirement (NFR):** a quality, constraint, or
  property the system must hold, attached to one of the six categories
  below. Test: it says how well, how much, how safely, or under what
  constraint - not what the system does.

## Category taxonomy (proposal's six)

| Category | Includes | Excludes (common confusion) |
| --- | --- | --- |
| security | confidentiality, integrity, access control, authentication strength, encryption, audit logging, data protection | reliability/backups (reliability), legal compliance wording (other, unless about data protection) |
| performance | latency, throughput, response time, resource budgets | capacity growth (scalability) |
| scalability | concurrent users, data-volume growth, horizontal/vertical scaling | point-in-time speed (performance) |
| reliability | availability, durability, failure handling, recovery, backups, fault tolerance | maintainability wording about code structure (other) |
| usability | UX quality, accessibility, learnability, error-message quality | look-and-feel branding constraints (other) |
| other | legal/licensing, portability, maintainability, operability, anything real that fits no category above | - |

## Boundary rules (apply in order)

1. **Behavior or property first, category second.** Decide FR vs NFR
   before picking a category.
2. **"The app must be fast" is an NFR (performance), not an FR.** Vague
   wording does not make a quality statement functional. The vagueness is
   recorded (the pipeline's `vague` finding kind), but the type is NFR.
3. **"We store user profile data" is an FR** (the system performs
   storage). It also IMPLIES security/reliability concerns; implications
   go into `implied_categories`, not into the FR list.
4. **One sentence can yield at most one FR.** Split compound sentences
   ("users can sign up and reset passwords") into one FR per behavior.
5. **Design decisions are not requirements.** "We use Postgres" is
   extracted only if the spec states it as a binding constraint (other);
   rationale text is skipped.
6. **Quantities stay attached.** An NFR without its number ("under 2s")
   is a different requirement from the same sentence with it; keep the
   full quantified text.
7. **When unsure between two categories,** pick the one matching the
   stated consequence (e.g. "so the site stays up" -> reliability), not
   the mechanism. Record the disagreement for adjudication - do not
   silently resolve it.

## Process (proposal)

1. Both extractors apply these guidelines independently to the same
   reference spec, producing candidate FR/NFR lists with categories.
2. Cohen's kappa on the independent labels is reported per item.
3. Disagreements are adjudicated together; the adjudicated list becomes
   the gold file (`eval/gold.py` format), with the kappa recorded in
   `provenance.extractor_agreement_kappa`.
4. Known limitation (proposal section 5): reference specs omit things a
   good spec would include, understating measured precision; a manual
   sample of "false extra" generated requirements is checked and reported.
