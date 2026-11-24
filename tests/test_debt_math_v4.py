"""Pure debt maths (``app.services.debt_math``): reference values worked out by hand, then checked against the engine.

Hand-worked reference used below: 10.000.000 COP at 28 % EA over 36 months.
  monthly rate  r = 1.28^(1/12) - 1 = e^(ln(1.28)/12) - 1 = e^0.0205717 - 1 = 0.0207847
  (1 + r)^36 = 1.28^3 = 2.097152, so (1 + r)^-36 = 0.4768372
  installment   = P r / (1 - (1 + r)^-36) = 207.847,3 / 0.5231628 = 397.290 (rounded to the peso)
  month 1       = interest 207.847 (10.000.000 r), principal 397.290 - 207.847 = 189.443, balance 9.810.557
"""
import math
from datetime import date
from decimal import Decimal

import pytest

from app.services import debt_math as m

FIRST = date(2026, 12, 5)
RATE = Decimal("28")
INSTALLMENT = Decimal("397290")


def schedule(**kwargs):
    return m.amortize(10_000_000, RATE, INSTALLMENT, FIRST, **kwargs)


def float_rate(ea):
    return (1 + ea / 100) ** (1 / 12) - 1


# --- rates, rounding, calendar ----------------------------------------------------------------------------------------

def test_monthly_rate_from_ea():
    assert abs(m.monthly_rate(28) - Decimal("0.0207847285")) < Decimal("1e-9")
    assert m.monthly_rate(0) == 0
    # (1 + r)^12 gives the EA back
    assert abs((1 + m.monthly_rate(Decimal("28.5"))) ** 12 - Decimal("1.285")) < Decimal("1e-12")


def test_rounding_is_half_up_to_the_peso():
    assert m.peso(Decimal("2.5")) == 3 and m.peso(Decimal("0.5")) == 1 and m.peso(Decimal("2.49")) == 2 and m.peso(Decimal("1234.4999")) == 1234


def test_add_months_clamps_to_month_length_and_payment_dates():
    assert m.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert m.add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)
    assert m.add_months(date(2026, 11, 5), 14) == date(2028, 1, 5)
    assert m.first_payment_date(date(2026, 11, 24), 5) == date(2026, 12, 5)
    assert m.first_payment_date(date(2026, 11, 24), 24) == date(2026, 11, 24)
    assert m.first_payment_date(date(2026, 11, 24), 31) == date(2026, 11, 30)
    assert m.calendar_remaining(36, date(2026, 1, 15), date(2026, 11, 24)) == 26
    assert m.calendar_remaining(36, date(2026, 1, 30), date(2026, 11, 24)) == 27
    assert m.calendar_remaining(None, date(2026, 1, 15), date(2026, 11, 24)) is None


# --- French installment and schedule ------------------------------------------------------------------------------------

def test_french_installment_reference_value():
    assert m.french_installment(10_000_000, 28, 36) == INSTALLMENT
    assert m.french_installment(1_200_000, 0, 12) == 100_000  # zero rate: P / n


def test_schedule_matches_hand_reference_and_closes_at_zero():
    result = schedule()
    first, last = result.rows[0], result.rows[-1]
    assert (first.interest, first.principal, first.installment, first.balance) == (Decimal("207847"), Decimal("189443"), INSTALLMENT, Decimal("9810557"))
    assert result.months == 36 and last.balance == 0 and last.date == date(2029, 11, 5)
    assert sum(r.principal for r in result.rows) == 10_000_000
    assert result.total_interest == sum(r.interest for r in result.rows)
    # 36 installments minus the principal; the last one absorbs the rounding of the installment (a few pesos)
    assert abs(result.total_interest - (36 * INSTALLMENT - 10_000_000)) < 30
    assert [r.n for r in result.rows] == list(range(1, 37))


def test_insurance_is_a_fixed_charge_apart_from_capital_and_interest():
    plain, insured = schedule(), schedule(insurance=15_000)
    assert all(r.insurance == 15_000 for r in insured.rows) and all(r.insurance == 0 for r in plain.rows)
    assert [(r.interest, r.principal, r.balance) for r in insured.rows] == [(r.interest, r.principal, r.balance) for r in plain.rows]
    assert insured.total_interest == plain.total_interest
    assert sum(r.installment + r.insurance for r in insured.rows) - sum(r.installment for r in plain.rows) == 36 * 15_000


# --- extra payments: reduce term vs reduce installment ------------------------------------------------------------------

def test_extra_once_reducing_the_term_keeps_the_installment():
    """Extra of 2.000.000 after month 1: balance 9.810.557 - 2.000.000 = 7.810.557 still pays 397.290.

    Months left = -ln(1 - B r / P) / ln(1 + r) with B r = 7.810.557 * 0.0207847 = 162.339, so 162.339 / 397.290 = 0.4086 and -ln(0.5914) / 0.0205717 = 25.5 -> 26, plus month 1 = 27.
    """
    r = float_rate(28)
    left = math.ceil(-math.log(1 - 7_810_557 * r / 397_290) / math.log(1 + r))
    result = schedule(extra_once=2_000_000)
    assert result.months == 1 + left == 27
    assert result.rows[0].extra == 2_000_000 and all(row.extra == 0 for row in result.rows[1:])
    assert all(row.installment == INSTALLMENT for row in result.rows[:-1]) and result.new_installment == INSTALLMENT
    assert result.rows[-1].balance == 0


def test_extra_once_reducing_the_installment_keeps_the_term():
    """Same extra, same 36 months: the installment is recomputed for the 35 months left, 7.810.557 r / (1 - (1 + r)^-35)."""
    r = float_rate(28)
    expected = 7_810_557 * r / (1 - (1 + r) ** -35)
    result = schedule(extra_once=2_000_000, mode="installment")
    assert result.months == 36 and result.rows[-1].date == date(2029, 11, 5)
    assert abs(result.new_installment - Decimal(expected)) <= 1
    assert result.rows[1].installment == result.new_installment < INSTALLMENT
    assert result.rows[-1].balance == 0


def test_reducing_the_term_saves_more_interest_than_reducing_the_installment():
    base, by_term, by_installment = schedule(), schedule(extra_once=2_000_000), schedule(extra_once=2_000_000, mode="installment")
    assert by_term.total_interest < by_installment.total_interest < base.total_interest
    assert by_term.months < by_installment.months == base.months
    assert base.total_interest - by_term.total_interest > 0


def test_monthly_extra_shortens_the_term():
    plain, extra = schedule(), schedule(extra_monthly=200_000)
    assert extra.months < plain.months and extra.total_interest < plain.total_interest
    assert all(row.extra == 200_000 for row in extra.rows[:-1])
    assert extra.rows[-1].balance == 0 and sum(r.principal + r.extra for r in extra.rows) == 10_000_000


def test_extra_once_in_a_later_month():
    result = schedule(extra_once=1_000_000, extra_once_month=6)
    assert [row.extra for row in result.rows if row.extra] == [1_000_000] and result.rows[5].extra == 1_000_000


# --- remaining installments from the balance ---------------------------------------------------------------------------

def test_remaining_installments_come_from_the_balance_and_the_installment():
    """5.000.000 left at 28 % EA paying 397.290: n = -ln(1 - 5.000.000 r / 397.290) / ln(1 + r) = 14.1 -> 15."""
    r = float_rate(28)
    expected = math.ceil(-math.log(1 - 5_000_000 * r / 397_290) / math.log(1 + r))
    assert m.term_for(5_000_000, 28, 397_290) == expected
    assert m.amortize(5_000_000, 28, 397_290, FIRST).months == expected
    assert m.term_for(10_000_000, 28, 397_290) == 36  # rounding of the installment does not add a 37th month
    assert m.term_for(10_000_000, 28, 100_000) is None  # does not even cover the interest


def test_a_payment_below_the_interest_never_amortizes():
    with pytest.raises(m.NotAmortizingError):
        m.amortize(10_000_000, 28, 100_000, FIRST)


# --- cards ------------------------------------------------------------------------------------------------------------

def test_card_pays_a_fixed_amount_with_monthly_interest_on_the_balance():
    """2.000.000 at 30 % EA, 100.000 a month: month 1 interest = 2.000.000 * (1.3^(1/12) - 1) = 44.208."""
    payment = m.card_payment(None, Decimal("100000"))
    assert payment == 100_000 and m.card_payment(Decimal("150000"), Decimal("100000")) == 150_000 and m.card_payment(None, None) is None
    result = m.amortize(2_000_000, 30, payment, FIRST)
    assert abs(result.rows[0].interest - Decimal(round(2_000_000 * float_rate(30)))) <= 1
    assert result.rows[0].balance == 2_000_000 + result.rows[0].interest - 100_000
    assert result.rows[-1].balance == 0 and result.months > 20
    with pytest.raises(m.NotAmortizingError):
        m.amortize(2_000_000, 30, 40_000, FIRST)


# --- plans: avalanche, snowball and the snowball effect ----------------------------------------------------------------

DEBTS = [
    m.DebtInput(1, Decimal("800000"), Decimal("10"), Decimal("80000")),     # smallest balance, lowest rate
    m.DebtInput(2, Decimal("6000000"), Decimal("35"), Decimal("200000")),   # highest rate
    m.DebtInput(3, Decimal("3000000"), Decimal("20"), Decimal("120000")),
]


def test_avalanche_aims_at_the_highest_rate_and_snowball_at_the_smallest_balance():
    assert [d.id for d in m.priority(DEBTS, "avalanche")] == [2, 3, 1]
    assert [d.id for d in m.priority(DEBTS, "snowball")] == [1, 3, 2]


def test_avalanche_pays_less_interest_than_snowball_and_both_beat_the_minimums():
    avalanche = m.simulate_plan(DEBTS, 300_000, 0, "avalanche")
    snowball = m.simulate_plan(DEBTS, 300_000, 0, "snowball")
    baseline = m.simulate_plan(DEBTS, 0, 0, "avalanche", redistribute=False)
    assert avalanche.total_interest < snowball.total_interest < baseline.total_interest
    assert m.payoff_order(snowball)[0] == 1  # the smallest debt goes first
    assert m.payoff_order(avalanche)[0] != 2  # the highest-rate debt is also the biggest: it ends last
    assert avalanche.debt_free_month is not None and avalanche.debt_free_month <= snowball.debt_free_month < baseline.debt_free_month
    # nothing is created or lost: each debt's interest is non-negative and the totals add up
    assert avalanche.total_interest == sum(o.total_interest for o in avalanche.outcomes)


def test_snowball_effect_moves_a_paid_off_installment_to_the_next_debt():
    pair = [m.DebtInput(1, Decimal("500000"), Decimal("20"), Decimal("100000")), m.DebtInput(2, Decimal("5000000"), Decimal("20"), Decimal("150000"))]
    with_ball = m.simulate_plan(pair, 0, 0, "snowball")
    without = m.simulate_plan(pair, 0, 0, "snowball", redistribute=False)
    ended = {o.debt_id: o for o in with_ball.outcomes}, {o.debt_id: o for o in without.outcomes}
    assert ended[0][1].months == ended[1][1].months  # the small debt ends at the same month either way
    assert ended[0][2].months < ended[1][2].months  # but the big one ends earlier: it received the freed 100.000
    assert with_ball.total_interest < without.total_interest


def test_extra_once_goes_to_the_first_debt_in_month_one():
    result = m.simulate_plan(DEBTS, 0, 1_000_000, "avalanche")
    plain = m.simulate_plan(DEBTS, 0, 0, "avalanche")
    assert result.total_interest < plain.total_interest


def test_a_debt_that_never_amortizes_is_reported_unpaid_without_an_endless_loop():
    stuck = [m.DebtInput(1, Decimal("10000000"), Decimal("28"), Decimal("100000"))]
    assert m.simulate_plan(stuck, 0, 0, "avalanche", redistribute=False).debt_free_month is None
    assert m.simulate_plan(stuck, 400_000, 0, "avalanche").debt_free_month is not None
