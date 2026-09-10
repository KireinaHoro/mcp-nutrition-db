# Energy, recovery, and surplus policy

- Policy ID: `energy-credit/v3`
- Accepted: 2026-09-10
- Debit accounting begins: 2026-09-09, in the selected accounting timezone
- Calculation basis: current policy, recalculated from current audited facts

## Purpose and limits

The service distinguishes ordinary intake targets, optional exercise allowance,
protected recovery allowance, and outstanding surplus. A surplus can extend the
number of easier days carrying a modest additional deficit; it never increases
the requested daily adjustment above 200 kcal. Fuel demanding activity and its
recovery first. An estimated large exercise deficit is an accounting observation,
not a suggested intake or an instruction to exercise more.

The confidence factors, activity thresholds, recovery pool and taper are planning
conventions, not measured physiological requirements. Adequate carbohydrate,
protein, hydration, and the next activity still matter. The service does not
infer that a calorie allowance guarantees adequate fuelling.

## Ordinary and exercise accounting

For each local calendar day:

```text
ordinary_target = base_burn - planned_deficit
credited_exercise = sum(reported_active_burn * confidence_multiplier)
estimated_maintenance = base_burn + credited_exercise
planned_baseline = ordinary_target + incoming_recovery - additional_deficit
available_ceiling = planned_baseline + credited_exercise
```

Confidence multipliers are high 1.00, medium 0.80, and low 0.60. Reported burn and
its evidence remain intact. Exercise and recovery allowances are optional
ceilings. They are never prescriptions to consume every estimated calorie.

## Debit creation and repayment

From the debit effective date onwards:

```text
new_debit = max(0, logged_intake - estimated_maintenance)
closing_debit = opening_debit + new_debit - actual_repayment
```

The missed ordinary deficit is forgiven. Intake between the ordinary target
and maintenance creates no additional debit and ordinarily repays none.
Outstanding debit has no weekly cap, expiry, or automatic forgiveness.

An above-maintenance total is visible even before a day is confirmed complete;
it is a provisional observation from current logged facts. Unknown calorie
values or incomplete logging cannot produce repayment. Only a past local day
whose full intake the user explicitly confirmed through `nutrition_review_day`
and whose logged components all have calories can repay debit. Confirming today
does not settle it until the next local day. Future and empty unconfirmed days
never repay. Corrections deterministically recalculate subsequent balances.

Repayment uses achieved extra deficit beyond the ordinary plan and after the
protected source recovery pool. It is not credited merely because the service
lowered a target or cancelled an allowance. Unused incoming recovery expires
and cannot repay debit:

```text
incoming_used = min(max(intake - ordinary_target, 0), incoming_recovery)
extra_deficit = max(0,
    ordinary_target + credited_exercise + incoming_used - intake - reserved_pool)
actual_repayment = min(opening_debit, extra_deficit)
```

Exercise credit used for repayment is excluded from both new recovery credit
and expired exercise credit. A naturally larger achieved exercise deficit can
repay more than 200 kcal. The 200 kcal limit controls requested restriction,
not the arithmetic of an observed completed day.

## Additional deficit and its pauses

On an eligible day, request at most `min(200, opening_debit)` additional kcal
of deficit, bounded by the available ordinary target. Do not apply an extra
restriction without an active goal and positive planned deficit.

Pause the additional restriction:

- during a confirmed trip, including the return date;
- while a calorie-surplus context question or return-date question is unresolved;
- on an exceptional activity day and the following calendar day;
- on any day receiving protected recovery from exceptional activity.

Pausing a requested restriction does not erase debit or prevent repayment from
an actual, fully logged extra deficit. No reduction stacks or catches up after
a pause. `ceil(remaining_debit / 200)` is a projection in eligible days, not a
calendar deadline; it assumes each such day actually achieves 200 kcal extra.
A projection above 28 eligible days prompts review, without automatically
forgiving debit or increasing restriction. A final partial adjustment uses the
remaining amount exactly.

An exceptional day means at least 1,000 confidence-adjusted exercise kcal,
at least 180 total logged activity minutes, or an explicit exceptional-activity
flag in its day review. The flag also supports planned activities before burn
is logged. These thresholds are conservative software defaults and are exposed
by the policy tool; they are not clinical cutoffs.

## Protected recovery comes first

Incoming recovery is attributed before same-day exercise:

```text
exercise_used = clamp(intake - ordinary_target - incoming_recovery,
                      0, credited_exercise)
unused_exercise = credited_exercise - exercise_used
pool_cap = next_day_planned_deficit / 0.50
```

On exceptional activity days, reserve `min(unused_exercise, pool_cap)` **before**
repaying debit. Split the reserve into next-day / second-day / third-day
candidates of 50% / 30% / 20%. Their protected status survives outstanding debit.
Incoming candidates share the destination day's planned-deficit cap. Collisions
are reduced proportionally with deterministic integer rounding. Clipped or
unconsumed allocations expire; they are never redistributed or used later to
repay debit. Reserving the source pool before destination clipping avoids
future-day collisions changing an already allocated source repayment.

On ordinary activity days with outstanding debit, new carryover is suspended
until the day's observed repayment clears that debit. Any remaining unused
exercise can then generate ordinary capped recovery. Incoming ordinary
carryover is also suspended while a day opens with debit. Cancellation does
not itself count as repayment. With no outstanding debit, ordinary exercise
uses the same capped recovery pool and taper.

Conservation for each source day:

```text
unused_exercise = exercise_used_for_debit + recovery_scheduled + exercise_expired
```

Reservations and schedules from a day still in progress can shrink when more
food is logged. Day-level calorie completeness does not mean all food was
logged; day-review confirmation is a separate fact.

### Hike example with an opening debit of 2,800 kcal

Use 2,500 base burn, 500 ordinary deficit, 3,917 reported exercise kcal at
medium confidence, 2,455.94 intake, and no incoming recovery:

```text
credited_exercise = 3,133.6
unused_exercise = 2,677.66
protected_pool = 1,000
actual_repayment = 1,677.66
closing_debit = 1,122.34
protected recovery candidates = 500 / 300 / 200
```

No additional restriction is requested on the hike day or its protected
recovery days. This reuses historical numbers to illustrate allocation; it
is not an intake recommendation for another hike.

## Calorie-based context prompts and trips

Meal titles, flight numbers, restaurants, locations, and other narrative details
are **not** used to detect trips. Calories do not establish whether someone is
travelling. They trigger a context question when either:

- one day has at least 500 kcal above estimated maintenance; or
- at least two days within three local calendar days each have at least 250 kcal
  above estimated maintenance.

Use exercise-adjusted maintenance so fuelling a large activity does not look
like a surplus. Existing logged surplus can trigger the question before day
completion, with its uncertainty exposed. The response includes dates, intake,
estimated maintenance, and observed surplus as evidence.

`travel_context.action_required = ask_overshoot_context` tells ChatGPT to ask
whether this is an ongoing trip or a one-off. Only if travelling should it ask
when the user is scheduled to get home. It must not guess or store a trip from
calorie evidence alone. A user's explicit travel statement can also be recorded
without waiting for any numerical threshold.

`nutrition_set_trip` records the user's answer:

- `active`: a confirmed trip, with a known or unknown return date;
- `not_travelling`: a bounded range of surplus dates the user explained as
  non-travel; this dismisses those prompts without pausing adjustments;
- `cancelled`: a bounded cancellation of a saved trip/context.

Records are keyed by accounting timezone and start date, use expected revisions,
and have immutable audit snapshots. Active trips cannot overlap. A missing
return date keeps extra restriction paused and asks for the return date.
A known return starts eligibility on the following local day, subject to activity
and recovery pauses. Adjusting the return date recalculates those pauses.
Use the usual accounting timezone throughout travel to avoid shifting debit
between ledgers. Later unexplained surplus dates can prompt a new question.

## MCP presentation and current-trip activation

The tool response explains only this active policy. It includes parameters,
formulas, pauses, confirmation rules, and the stable document reference.
Historical comparisons belong in repository history, not the tool response.

The schema migration creates empty trip, day-review, and audit tables. It does
not seed personal records, confirm days, or infer a return date. The effective
date makes the existing 9 September surplus participate immediately from logged
facts, with a context prompt and paused additional restriction. ChatGPT owns
asking the user and recording their answers.

Daily summaries and goals expose the server-calculated debit ledger, pauses,
provisional status, unconfirmed dates, projected eligible days, exceptional-day
status, protected recovery, and the calorie-based context action. Logging a meal
also returns current energy context so the prompt can be surfaced promptly.
`nutrition_get_energy_context` provides saved records and revisions, including
when a trip has no return date yet. Clients must not maintain a hidden balance.
