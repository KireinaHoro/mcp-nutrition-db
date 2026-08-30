# Exercise and recovery energy-credit policy

- Policy ID: `energy-credit/v1`
- Status: Implemented in schema v3; production deployment pending
- Accepted: 2026-08-30
- Last revised: 2026-08-30
- Applies when: a release that declares this policy ID is deployed

## 1. Purpose

This policy turns logged exercise expenditure into a conservative calorie
allowance without implying that the user should eat every exercise calorie.
It also permits part of a large unused allowance to increase flexibility over
the following recovery days.

The policy has three goals:

- preserve the full reported exercise estimate and its provenance;
- discount less reliable estimates before they affect calorie accounting;
- prevent a very large physical day from producing an implausibly large
  single-day target or an indefinitely rolling calorie balance.

This is planning logic, not a physiological measurement or medical
recommendation. A budget or allowance is a ceiling available to the user, not
a prescription to consume it.

## 2. Terms

For a local calendar day `d`:

- `base_burn[d]` is the effective goal's ordinary daily energy expenditure.
- `deficit[d]` is the effective planned calorie deficit.
- `ordinary_target[d] = base_burn[d] - deficit[d]`.
- `reported_training_burn[d]` is the sum of the unmodified burn estimates on
  active training records assigned to the day.
- `credited_training_burn[d]` is the sum after applying each training record's
  confidence multiplier.
- `incoming_recovery[d]` is the non-recurring allowance scheduled by earlier
  exercise days.
- `intake[d]` is the day's logged calorie intake.

The service must expose both reported and credited burn. It must not overwrite
the source estimate with the policy-adjusted value.

## 3. Training evidence and confidence

Every training record that participates in energy accounting has:

- `reported_burn_kcal`;
- a structured `measurement_method` or source;
- `confidence`: `high`, `medium`, or `low`;
- enough supporting details to explain the classification when available, such
  as device, average power, mechanical work, heart-rate/GPS model, or manual
  calculation.

The confidence multipliers are:

| Confidence | Multiplier | Typical evidence |
| --- | ---: | --- |
| `high` | 1.00 | Strong direct basis, such as a cycling power meter or indirect calorimetry |
| `medium` | 0.80 | Individualized but model-derived estimate, such as a wearable using heart rate and GPS |
| `low` | 0.60 | Generic activity tables, rough manual estimates, or weakly supported device estimates |

These values are policy coefficients, not statistical confidence intervals.
Source type may guide the initial classification, but it does not create a
second hidden multiplier. The confidence remains explicit and correctable.

For each training record:

```text
credited_burn = reported_burn * confidence_multiplier
```

Corrections to burn, method, confidence, timing, or deletion must be auditable
and must cause affected derived balances to be recalculated.

## 4. Same-day accounting

The day's planned baseline budget is:

```text
planned_baseline = ordinary_target + incoming_recovery
```

The confidence-adjusted same-day exercise allowance is added as an optional
ceiling:

```text
available_ceiling = planned_baseline + credited_training_burn
```

Incoming recovery is attributed before same-day exercise allowance. Therefore:

```text
exercise_credit_used = clamp(
  intake - ordinary_target - incoming_recovery,
  lower = 0,
  upper = credited_training_burn
)

unused_exercise_credit = credited_training_burn - exercise_credit_used
```

This ordering is important. Unused incoming recovery expires; it does not
become new exercise credit and cannot generate another recovery schedule.

The service should present `ordinary_target`, `incoming_recovery`,
`credited_training_burn`, `available_ceiling`, and
`unused_exercise_credit` separately. A single number labelled merely
"calorie target" would obscure the distinction between the ordinary plan and
optional exercise allowance.

## 5. Scheduling recovery allowance

Recovery scheduling uses the confidence-adjusted, unused same-day exercise
credit calculated above.

1. Calculate three independent candidate allocations from all positive
   `unused_exercise_credit`:
   - next local day: `50%`;
   - two local days later: `30%`;
   - three local days later: `20%`.
2. Cap each candidate independently at that destination day's planned deficit.
   Under the current 500 kcal deficit, this is a 500 kcal daily cap.
3. Any amount clipped by a daily cap expires. It is not redistributed to a
   later day.
4. Any scheduled amount not consumed on its destination day expires. It does
   not roll forward and does not create another recovery schedule.

The destination-day cap applies to the aggregate incoming recovery from all
source days. If several source days produce candidates for the same destination
and their sum exceeds its cap, reduce those candidates proportionally to fit;
the clipped portions expire. This keeps the result independent of processing
order and prevents consecutive large exercise days from multiplying the cap.

For each source day with positive unused exercise credit, offsets `n = 1, 2, 3`
have weights `0.50`, `0.30`, and `0.20`. For each destination day `t`:

```text
candidate[d, n] = unused_exercise_credit[d] * weight[n]
raw_incoming[t] = sum(candidate[d, n] where d + n = t)
incoming_recovery[t] = min(raw_incoming[t], planned_deficit[t])

if raw_incoming[t] > 0:
  scheduled[d, n] = candidate[d, n] * incoming_recovery[t] / raw_incoming[t]

expired_at_creation[d] = unused_exercise_credit[d] - sum(scheduled[d, n])
```

Every positive amount of unused exercise credit is distributed according to
the weights above. Zero unused credit produces no recovery allowance.

## 6. Worked examples

### 1,200 kcal unused credit at high confidence

The 1,200 kcal is already confidence-adjusted. Candidate allocations are
600/360/240 kcal. The next-day candidate is capped at 500 kcal, so the applied
schedule is 500/360/240 kcal and 100 kcal expires. The overflow is not moved to
days two or three.

### 1,200 kcal reported unused burn at medium confidence

The credited amount is `1,200 * 0.80 = 960 kcal`. Candidate and applied
allocations are 480/288/192 kcal; none reaches the 500 kcal daily cap.

### Large hike

Given 3,917 kcal reported burn at medium confidence, 2,000 kcal ordinary
target, no incoming recovery, and 2,455.94 kcal intake:

```text
credited_training_burn = 3917 * 0.80 = 3133.60
exercise_credit_used = 2455.94 - 2000 = 455.94
unused_exercise_credit = 3133.60 - 455.94 = 2677.66
```

The candidates are 1,338.83/803.30/535.53 kcal. With a 500 kcal planned
deficit on each destination day, the schedule is 500/500/500 kcal and
1,177.66 kcal expires.

## 7. Versioning and recalculation

All derived energy-balance responses must include `policy_id`. Stored training,
meal, goal, and audit facts remain separate from derived policy results.

A later semantic change after deployment requires a new policy ID. Tunable
configuration such as confidence multipliers, weights, and caps must be
effective-dated if historical answers are intended to remain reproducible. The
API must make clear whether a historical result was calculated using the policy
effective on that historical day or recalculated under the current policy.

Corrections can affect the source day and its following three days. The
implementation should derive the schedule deterministically from current facts
and effective-dated policy rather than mutating opaque running balances.
An allocation derived from a source day still in progress is provisional and
may shrink as more intake is logged. Responses must identify provisional
source days; closing a calendar day does not prevent a later audited correction
from recalculating its results.

## 8. MCP presentation

The calculation belongs in the service. ChatGPT must not be required to
reconstruct the formulas or preserve a hidden rolling balance in conversation.

The MCP surface should provide:

- concise server initialization instructions explaining the distinction
  between ordinary target, incoming recovery, and optional exercise allowance;
- action-oriented descriptions on every affected summary, goal, training, and
  energy-balance tool;
- a read-only `nutrition_get_energy_policy` tool returning the active policy ID,
  effective parameters, definitions, and a stable document reference;
- `policy_id` in every response whose values depend on this policy;
- server-calculated ledger fields for reported burn, credited burn, allowance
  used, allowance unused, scheduled recovery, and expired amounts.

The policy tool should return structured data, not merely this Markdown file.
For example:

```json
{
  "policy_id": "energy-credit/v1",
  "status": "active",
  "confidence_multipliers": {"high": 1.0, "medium": 0.8, "low": 0.6},
  "recovery_weights": [0.5, 0.3, 0.2],
  "daily_cap": "destination_planned_deficit",
  "overflow": "expire",
  "missed_allocation": "expire",
  "document_ref": "docs/energy-credit-policy.md"
}
```

An externally reachable canonical URL may accompany `document_ref` if the
policy is deliberately published later. The repository path alone is an audit
reference; ChatGPT cannot be expected to retrieve a local file from it.

There is no special MCP "description tool" that is guaranteed to be called.
Clients receive server instructions during initialization and tool metadata
during discovery, then the model selects tools. The policy lookup tool is for
explanation and auditability, not a prerequisite for correct calculation.

A document URL is useful provenance but is insufficient as the only guidance:
the client might not fetch it. The essential cross-tool semantics belong in the
server instructions, per-tool call guidance belongs in tool descriptions, and
the service must enforce the actual arithmetic. The most important server
instructions should remain concise and appear first.

This follows [OpenAI's MCP server guidance](https://developers.openai.com/plugins/build/mcp-server),
which assigns cross-tool guidance to server instructions and call-selection
guidance to tool names, descriptions, schemas, and annotations.

Suggested leading server instruction:

> Calorie accounting distinguishes the ordinary target, incoming recovery
> allowance, and confidence-adjusted exercise allowance. An allowance is an
> optional ceiling, not a recommendation to eat it. Use server-returned energy
> calculations; call `nutrition_get_energy_policy` when explaining the policy
> or proposing a change.
