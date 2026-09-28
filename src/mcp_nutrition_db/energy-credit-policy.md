# Energy, recovery, and surplus policy

- Policy ID: `energy-credit/v6`
- Accepted: 2026-09-28
- Debit accounting begins: 2026-09-09, in the selected accounting timezone
- Calculation basis: current policy, recalculated from current audited facts

## Exceptional-day excess repays debt

**Unused budget on an exceptional (extraordinary) activity day participates in
debt repayment after the capped protected recovery pool is reserved.** The
exceptional-day pause applies only to the requested additional intake restriction.
It does not pause observed repayment or exempt the day's remaining unused budget.
Repayment occurs when that past local day has logged intake with known calories,
is capped by opening debt, and can exceed 200 kcal. Today's repayment remains
provisional and is recorded as zero until settlement.

Here, debt means the ledger's `debit`; excess budget means unused calorie
allowance, not food eaten above maintenance. Protected recovery covers only the
reserved pool, not all unused exercise credit. For example, 2,677.66 kcal of
unused exercise minus a 1,000 kcal protected pool leaves 1,677.66 kcal for
repayment when opening debt is sufficient, even though additional restriction
is zero.

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
and maintenance creates no additional debit. It can repay debit when incoming
recovery or exercise leaves unused unadjusted budget.
Outstanding debit has no weekly cap, expiry, or automatic forgiveness.

Above-maintenance intake accrues from current logged facts. Today remains provisional.
Once a local calendar day has passed, its logged intake automatically participates in
repayment if at least one active intake entry exists and every component has known
calories. No confirmation, timer, background job, or persisted closing event is needed:
the ledger compares dates with local today whenever queried. Empty days and days with
unknown calories never repay debit.

Past logged intake is an accounting assumption, not a claim that every meal was logged.
Backdated additions, edits, date changes, and deletions deterministically recalculate
that day's repayment, recovery allocations, and all subsequent balances. A late meal
can reduce or reverse previously calculated repayment or add new debit.

Repayment uses unused **unadjusted** daily budget after reserving source-day
recovery. Requested additional restriction does not reduce this accounting budget:
achieving a target lowered by 200 kcal therefore repays 200 kcal.

```text
unadjusted_budget = ordinary_target + incoming_recovery + credited_exercise
unused_budget = max(0, unadjusted_budget - intake - reserved_pool)
actual_repayment = min(opening_debit, unused_budget)
```

Unused incoming recovery participates in repayment when its destination day settles.
It was reserved, not repaid, on its source day, so this credits it only once.
Attribute repayment first to unreserved unused same-day exercise, then unused
incoming recovery, then unused ordinary budget. Incoming recovery left after
intake and repayment expires; it is not carried forward. Empty or unknown-calorie
days cannot convert recovery into repayment.

With a 2,000 ordinary target, 500 incoming recovery, no exercise and sufficient
opening debit: intake of 2,500 / 2,300 / 2,000 repays 0 / 200 / 500 kcal respectively
after local midnight. These are accounting examples, not intake recommendations.
Repayment is an observation, not a score to maximize; recovery and adequate
fuelling remain the priority.

Exercise credit used for repayment is excluded from both new recovery credit
and expired exercise credit. A naturally larger achieved exercise deficit can
repay more than 200 kcal. The 200 kcal limit controls requested restriction,
not the arithmetic of an observed completed day.

## Additional deficit and its pauses

On an eligible day, request at most `min(200, opening_debit)` additional kcal
of deficit, bounded by the available ordinary target. Do not apply an extra
restriction without an active goal and positive planned deficit.

Pause the additional restriction:

- on an exceptional activity day and the following calendar day;
- on any day receiving protected recovery from exceptional activity.

These are pauses of **additional restriction only**, not pauses of repayment.
Exceptional days, the following day, and protected recovery days all use the
same settlement and repayment formula above. Zero `additional_deficit` does not
imply zero `actual_repayment`. No reduction stacks or catches up after
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

On exceptional activity days, start with `min(unused_exercise, pool_cap)` and
split it into next-day / second-day / third-day candidates of 50% / 30% / 20%,
using deterministic integer rounding. For each destination, subtract existing
reservations from earlier source days from its planned-deficit cap. Clip the new
candidate to that remaining capacity. **Reserve only the sum of these fitted
allocations before repaying debit.** Their protected status survives outstanding
debit. Later source days cannot reduce earlier reservations.

Credit excluded by a destination cap remains unreserved unused exercise and
participates in source-day repayment on settlement, capped by opening debit;
any excess expires. Do not redistribute clipped candidates or split the fitted
pool again. The 50% / 30% / 20% weights describe the initial candidates; the
actual reservation can have a different shape when capacity is occupied.
Allocated but unconsumed recovery can repay debit on its destination day only;
any remainder expires. Pending ordinary reservations occupy capacity until their
destination is processed, even if outstanding debit subsequently cancels them.
Cancellation does not retrospectively enlarge another source's reservation.

For example, an earlier source reserves 300 / 200 / 0 kcal on the next three
destinations. With a 500 kcal daily cap, a later source's 500 / 300 / 200
candidates fit as 200 / 300 / 200. Its reserved pool is 700 kcal, leaving an
additional 300 kcal eligible for source-day repayment. Aggregate recovery on
those destinations is 500 / 500 / 200 kcal.

On ordinary activity days with outstanding debit, new carryover is suspended
until the day's observed repayment clears that debit. Any remaining unused
exercise can then generate ordinary capped recovery. Incoming ordinary
carryover is also suspended while a day opens with debit. Cancellation does
not itself count as repayment. With no outstanding debit, ordinary exercise
uses the same candidate taper and remaining-capacity reservation rule.

Conservation for each source day:

```text
unused_exercise = exercise_used_for_debit + recovery_scheduled + exercise_expired
```

Reservations and schedules from a day still in progress can shrink when more
food is logged. Past allocations also recalculate after corrections. Nutrient-field
completeness does not prove all food was logged; settlement uses the current records.

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

## Repeated overshoots need no classification

Debit is independent of whether an overshoot happened during travel, at a social
meal, or at home. There are no trip records, return-date questions, context
thresholds, or manually chosen repayment start dates. Additional deficit starts
on the next day with opening debit, subject only to goal and activity/recovery
eligibility.

Another day above maintenance adds its actual surplus to the balance. Missing
the adjusted target while remaining below maintenance creates no new debit:
it simply achieves less or no repayment. Neither case stacks reductions or
raises the next day's daily cap. The projected repayment period gets longer.
For example, three days with surpluses of 2,800, 700, and 1,000 kcal leave 4,500
kcal outstanding: an estimated 23 eligible days at 200 kcal extra, still with
only a 200 kcal requested adjustment on each eligible day.

Use the usual accounting timezone throughout travel to avoid shifting debit
between ledgers. Meal titles and travel details do not affect the arithmetic.

## MCP presentation and activation

The tool response includes this complete policy text as well as parameters,
formulas, pauses, automatic settlement rules, and the stable document reference.

All dates from the debit effective date are recalculated under this policy
from current logged facts, including today; no separate migration is required. No personal records or daily confirmations are required. Optional
planned exceptional activity is recorded through `nutrition_set_activity_plan`.
The deployed schema's existing review and audit tables retain activity plans; the
legacy intake-completion column is ignored and absent from public tools.

Daily summaries and goals expose the debit ledger, pauses, provisional status,
unsettled dates, projected eligible days, exceptional activity, and protected recovery.
`day_closed` means the local date has passed; `intake_logged` means active entries exist;
`intake_settled` additionally requires known calories. Unsettled dates identify current,
empty, or unknown-calorie days affecting the balance's certainty.
`nutrition_get_activity_plan` provides a saved activity plan and revision before a
correction. Plans use expected revisions and immutable audit snapshots. Clients must
not maintain a hidden balance.

`incoming_recovery_remaining_kcal` is the amount not consumed as food;
`incoming_recovery_repaid_kcal` is the portion used to repay debit. On closed
days, `incoming_recovery_expired_kcal` excludes that repayment. On an open day,
repayment remains zero and expiry is unknown until settlement.

`recovery_pool_kcal` is the sum reserved after destination-capacity clipping.
Each schedule's `candidate_kcal` is that fitted reservation; `scheduled_kcal`
can be smaller if ordinary carryover is cancelled by destination-day debit.
`recovery_pool_expired_at_creation_kcal` measures reserved credit subsequently
cancelled, not credit excluded before reservation. Unreserved exercise appears
in repayment or `exercise_credit_expired_at_creation_kcal`, as applicable.
