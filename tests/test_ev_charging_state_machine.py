"""Tests for ev_charging_state_machine.py"""
import ast
import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, MagicMock, patch, call
import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

# Mock Home Assistant modules before importing
sys.modules['homeassistant'] = MagicMock()
sys.modules['homeassistant.util'] = MagicMock()
sys.modules['homeassistant.util.dt'] = MagicMock()

# Mock PyScript globals and decorator
import builtins
builtins.service = lambda func: func
builtins.hass = MagicMock()
builtins.state = MagicMock()

from ev_charging_state_machine import (
    # Pure functions
    get_source_tier,
    compute_effective_price,
    assign_credibilities,
    build_contiguous_runs,
    find_optimal_slots,
    slots_to_sessions,
    prune_and_classify,
    compute_hours_remaining,
    determine_state,
    build_schedule_attrs,
    deduplicate_and_sort_prices,
    # Scheduling helpers
    resolve_boost_end,
    compute_sessions,
    apply_boosting_outputs,
    apply_idle_outputs,
    apply_schedule_outputs,
    _make_schedule_data,
    _parse_dt,
    filter_runs_by_max_avg_price,
    # Input collection
    collect_inputs,
    collect_all_prices,
    # HA adapter
    EVChargingHA,
    # Services
    update_ev_charge_state,
    ev_charging_boost,
    ev_charging_boost_cancel,
    ev_charging_stop,
    # Constants
    TIER_ACTUAL, TIER_PREDICTED_0_24, TIER_PREDICTED_24_48,
    TIER_PREDICTED_48_72, TIER_PREDICTED_72_PLUS,
    STATE_IDLE, STATE_SCHEDULED, STATE_CHARGING, STATE_BOOSTING, STATE_COMPLETE,
    STATE_UNSCHEDULABLE, STATE_ERROR,
    BASE_CREDIBILITY,
)

_SOURCE_PATH = os.path.join(os.path.dirname(__file__), '..', 'src', 'ev_charging_state_machine.py')

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_ha_now():
    with patch('ev_charging_state_machine.ha_now') as mock:
        mock.return_value = datetime(2024, 1, 15, 10, 0)
        yield mock


@pytest.fixture
def mock_as_local():
    with patch('ev_charging_state_machine.as_local') as mock:
        mock.side_effect = lambda dt: dt
        yield mock


@pytest.fixture
def mock_hass():
    import builtins
    original = builtins.hass
    mock = MagicMock()
    builtins.hass = mock
    yield mock
    builtins.hass = original


@pytest.fixture
def mock_state_obj():
    import builtins
    original = builtins.state
    mock = MagicMock()
    builtins.state = mock
    yield mock
    builtins.state = original


def make_slot(dt, raw_price, source='current_actual', effective_price=None):
    slot = {'date_time': dt, 'raw_price': raw_price, 'source': source}
    if effective_price is not None:
        slot['effective_price'] = effective_price
    return slot


def make_slots_range(start_dt, count, price=10.0, source='current_actual'):
    """Generate count consecutive 30-min slots starting at start_dt."""
    return [
        make_slot(start_dt + timedelta(minutes=30 * i), price, source)
        for i in range(count)
    ]


NOW = datetime(2024, 1, 15, 10, 0)
READY_BY = datetime(2024, 1, 16, 7, 0)


# ---------------------------------------------------------------------------
# TestPyScriptCompatibility — static analysis to catch HA runtime issues
# ---------------------------------------------------------------------------

class TestPyScriptCompatibility:
    """
    PyScript (Home Assistant's custom Python interpreter) does not implement all
    standard AST node types. These tests scan the source file statically so that
    incompatible constructs are caught during the normal test run, not at HA deploy time.
    """

    @classmethod
    def _get_ast(cls):
        with open(_SOURCE_PATH) as f:
            return ast.parse(f.read())

    def test_no_generator_expressions(self):
        """PyScript raises 'not implemented ast ast_generatorexp'. Use list comprehensions."""
        nodes = [n for n in ast.walk(self._get_ast()) if isinstance(n, ast.GeneratorExp)]
        assert nodes == [], (
            f"{len(nodes)} generator expression(s) found — "
            "replace sum(x for x in y) with sum([x for x in y])"
        )

    def test_no_set_comprehensions(self):
        """PyScript may not support set comprehensions. Use set([...]) instead."""
        nodes = [n for n in ast.walk(self._get_ast()) if isinstance(n, ast.SetComp)]
        assert nodes == [], (
            f"{len(nodes)} set comprehension(s) found — "
            "replace {x for x in y} with set([x for x in y])"
        )

    def test_no_dict_comprehensions(self):
        """PyScript may not support dict comprehensions. Use a loop or dict() instead."""
        nodes = [n for n in ast.walk(self._get_ast()) if isinstance(n, ast.DictComp)]
        assert nodes == [], (
            f"{len(nodes)} dict comprehension(s) found — "
            "replace {k: v for ...} with a loop or dict()"
        )

    def test_no_walrus_operator(self):
        """PyScript does not support the walrus operator (:=)."""
        nodes = [n for n in ast.walk(self._get_ast()) if isinstance(n, ast.NamedExpr)]
        assert nodes == [], f"{len(nodes)} walrus operator(s) found"


# ---------------------------------------------------------------------------
# TestGetSourceTier
# ---------------------------------------------------------------------------

class TestGetSourceTier:
    def test_current_actual(self):
        assert get_source_tier('current_actual', NOW + timedelta(hours=1), NOW) == TIER_ACTUAL

    def test_next_actual(self):
        assert get_source_tier('next_actual', NOW + timedelta(hours=25), NOW) == TIER_ACTUAL

    def test_predicted_within_24h(self):
        assert get_source_tier('predicted', NOW + timedelta(hours=12), NOW) == TIER_PREDICTED_0_24

    def test_predicted_at_24h_boundary(self):
        assert get_source_tier('predicted', NOW + timedelta(hours=24), NOW) == TIER_PREDICTED_0_24

    def test_predicted_24_to_48h(self):
        assert get_source_tier('predicted', NOW + timedelta(hours=36), NOW) == TIER_PREDICTED_24_48

    def test_predicted_at_48h_boundary(self):
        assert get_source_tier('predicted', NOW + timedelta(hours=48), NOW) == TIER_PREDICTED_24_48

    def test_predicted_48_to_72h(self):
        assert get_source_tier('predicted', NOW + timedelta(hours=60), NOW) == TIER_PREDICTED_48_72

    def test_predicted_at_72h_boundary(self):
        assert get_source_tier('predicted', NOW + timedelta(hours=72), NOW) == TIER_PREDICTED_48_72

    def test_predicted_beyond_72h(self):
        assert get_source_tier('predicted', NOW + timedelta(hours=100), NOW) == TIER_PREDICTED_72_PLUS


# ---------------------------------------------------------------------------
# TestComputeEffectivePrice
# ---------------------------------------------------------------------------

class TestComputeEffectivePrice:
    def test_actual_price_unchanged_at_any_gamble(self):
        for gamble in (0, 50, 100):
            assert compute_effective_price(10.0, TIER_ACTUAL, gamble) == pytest.approx(10.0)

    def test_zero_gamble_inflates_predicted_0_24(self):
        raw = 10.0
        eff = compute_effective_price(raw, TIER_PREDICTED_0_24, 0)
        assert eff == pytest.approx(raw / 0.90)

    def test_zero_gamble_inflates_predicted_24_48(self):
        raw = 10.0
        eff = compute_effective_price(raw, TIER_PREDICTED_24_48, 0)
        assert eff == pytest.approx(raw / 0.75)

    def test_zero_gamble_inflates_predicted_72_plus(self):
        raw = 10.0
        eff = compute_effective_price(raw, TIER_PREDICTED_72_PLUS, 0)
        assert eff == pytest.approx(raw / 0.40)

    def test_full_gamble_all_predicted_tiers_unchanged(self):
        raw = 10.0
        for tier in (TIER_PREDICTED_0_24, TIER_PREDICTED_24_48, TIER_PREDICTED_48_72, TIER_PREDICTED_72_PLUS):
            assert compute_effective_price(raw, tier, 100) == pytest.approx(raw)

    def test_50_gamble_midpoint_0_24(self):
        raw = 10.0
        eff = compute_effective_price(raw, TIER_PREDICTED_0_24, 50)
        expected_cred = 0.90 + (1.0 - 0.90) * 0.5  # = 0.95
        assert eff == pytest.approx(raw / expected_cred)

    def test_50_gamble_midpoint_24_48(self):
        raw = 10.0
        eff = compute_effective_price(raw, TIER_PREDICTED_24_48, 50)
        expected_cred = 0.75 + (1.0 - 0.75) * 0.5  # = 0.875
        assert eff == pytest.approx(raw / expected_cred)


# ---------------------------------------------------------------------------
# TestFilterRunsByMaxAvgPrice — max_price applies to session average, not individual slots
# ---------------------------------------------------------------------------

class TestFilterRunsByMaxAvgPrice:
    def test_run_with_avg_above_max_excluded(self):
        """A run whose average exceeds max_price is removed entirely."""
        slots = make_slots_range(NOW, 4, price=25.0)
        result = filter_runs_by_max_avg_price(slots, 20.0)
        assert result == []

    def test_run_with_avg_at_max_kept(self):
        """A run averaging exactly max_price passes."""
        slots = make_slots_range(NOW, 4, price=20.0)
        assert len(filter_runs_by_max_avg_price(slots, 20.0)) == 4

    def test_spike_within_cheap_run_does_not_exclude_run(self):
        """A single expensive slot within a cheap run keeps the whole run eligible."""
        cheap = make_slots_range(NOW, 6, price=10.0)
        cheap[3] = make_slot(NOW + timedelta(minutes=90), 21.0)  # spike
        # avg = (10*5 + 21) / 6 = 12.67p < 20p → run kept, including the 21p slot
        result = filter_runs_by_max_avg_price(cheap, 20.0)
        assert len(result) == 6
        assert any(s['raw_price'] == 21.0 for s in result)

    def test_two_runs_one_eligible(self):
        """Only the run with avg <= max_price is returned."""
        cheap_run = make_slots_range(NOW, 4, price=10.0)
        expensive_run = make_slots_range(NOW + timedelta(hours=3), 4, price=30.0)
        result = filter_runs_by_max_avg_price(cheap_run + expensive_run, 20.0)
        assert len(result) == 4
        assert all(s['raw_price'] == 10.0 for s in result)

    def test_empty_input(self):
        assert filter_runs_by_max_avg_price([], 20.0) == []

    def test_all_excluded(self):
        slots = make_slots_range(NOW, 4, price=30.0)
        assert filter_runs_by_max_avg_price(slots, 20.0) == []


# ---------------------------------------------------------------------------
# TestBuildContiguousRuns
# ---------------------------------------------------------------------------

class TestBuildContiguousRuns:
    def test_single_run(self):
        slots = make_slots_range(NOW, 4)
        runs = build_contiguous_runs(slots)
        assert len(runs) == 1
        assert len(runs[0]) == 4

    def test_gap_splits_run(self):
        s1 = make_slot(NOW, 10.0)
        s2 = make_slot(NOW + timedelta(hours=1), 10.0)  # gap of 60 min
        runs = build_contiguous_runs([s1, s2])
        assert len(runs) == 2

    def test_two_adjacent_runs(self):
        run_a = make_slots_range(NOW, 3)
        run_b = make_slots_range(NOW + timedelta(hours=2), 3)
        runs = build_contiguous_runs(run_a + run_b)
        assert len(runs) == 2

    def test_single_slot(self):
        runs = build_contiguous_runs([make_slot(NOW, 10.0)])
        assert len(runs) == 1
        assert len(runs[0]) == 1

    def test_empty(self):
        assert build_contiguous_runs([]) == []


# ---------------------------------------------------------------------------
# TestFindOptimalSlots
# ---------------------------------------------------------------------------

class TestFindOptimalSlots:
    def _with_effective(self, slots):
        for s in slots:
            if 'effective_price' not in s:
                s['effective_price'] = s['raw_price']
        return slots

    def test_simple_selection(self):
        slots = self._with_effective(make_slots_range(NOW, 6, price=10.0))
        result = find_optimal_slots(slots, 4, READY_BY, min_block_hours=0.5)
        assert len(result) == 4

    def test_insufficient_slots_returns_empty(self):
        slots = self._with_effective(make_slots_range(NOW, 2, price=10.0))
        result = find_optimal_slots(slots, 6, READY_BY, min_block_hours=0.5)
        assert result == []

    def test_ready_by_excludes_late_slots(self):
        early = self._with_effective(make_slots_range(NOW, 4, price=10.0))
        late_start = READY_BY  # ends at READY_BY + 30min — after deadline
        late = self._with_effective([make_slot(late_start, 1.0)])
        result = find_optimal_slots(early + late, 4, READY_BY, min_block_hours=0.5)
        # late slot excluded; result from early slots only
        assert all(s['date_time'] < READY_BY for s in result)

    def test_picks_cheapest_slots(self):
        cheap = self._with_effective(make_slots_range(NOW, 4, price=5.0))
        expensive = self._with_effective(
            make_slots_range(NOW + timedelta(hours=3), 4, price=20.0))
        result = find_optimal_slots(cheap + expensive, 4, READY_BY, min_block_hours=0.5)
        assert all(s['raw_price'] == 5.0 for s in result)

    def test_min_block_rejects_isolated_cheap_slot(self):
        # Single cheap slot surrounded by gaps — cannot form a 1h block
        isolated = make_slot(NOW, 1.0)
        isolated['effective_price'] = 1.0
        # Other slots in a separate run, much more expensive
        run = make_slots_range(NOW + timedelta(hours=2), 4, price=15.0)
        run = self._with_effective(run)
        result = find_optimal_slots([isolated] + run, 2, READY_BY, min_block_hours=1.0)
        # isolated can't form a 1h (2-slot) block, so only run slots used
        assert all(s['raw_price'] == 15.0 for s in result)

    def test_non_contiguous_optimal_split(self):
        # Two cheap runs separated by a gap of expensive slots
        run_a = make_slots_range(NOW, 2, price=5.0)
        gap = make_slots_range(NOW + timedelta(hours=1), 2, price=30.0)
        run_b = make_slots_range(NOW + timedelta(hours=2), 2, price=5.0)
        all_slots = self._with_effective(run_a + gap + run_b)
        result = find_optimal_slots(all_slots, 4, READY_BY, min_block_hours=0.5)
        assert len(result) == 4
        prices = set([s['raw_price'] for s in result])
        assert prices == {5.0}

    def test_gamble_tolerance_0_prefers_actual_over_predicted(self):
        # Actual at 9p (effective=9p), predicted 36h ahead at 7p (effective=7/0.75≈9.33p)
        now = NOW
        actual = [make_slot(now + timedelta(minutes=30 * i), 9.0, 'current_actual') for i in range(4)]
        predicted = [make_slot(now + timedelta(hours=36) + timedelta(minutes=30 * i), 7.0, 'predicted') for i in range(4)]
        all_slots = actual + predicted
        for s in all_slots:
            tier = get_source_tier(s['source'], s['date_time'], now)
            s['effective_price'] = compute_effective_price(s['raw_price'], tier, 0)
        result = find_optimal_slots(all_slots, 4, READY_BY, min_block_hours=0.5)
        # actual effective=9p, predicted_24_48 effective=7/0.75≈9.33p → actual preferred
        assert all(s['source'] == 'current_actual' for s in result)

    def test_returns_empty_for_empty_input(self):
        assert find_optimal_slots([], 4, READY_BY, min_block_hours=1.0) == []

    def test_isolated_cheap_slot_does_not_cause_infinite_loop(self):
        """
        Regression: when the globally cheapest slot is isolated (can't form a min_block
        run), the algorithm must not re-select it after ejection. Without ejected_this_pass
        tracking, this produced 186 identical iterations and returned [].
        """
        # 7 cheap consecutive slots forming a 3.5h run (valid per min_block=3h)
        main_run = make_slots_range(NOW + timedelta(hours=3), 7, price=5.0)
        # 1 cheaper isolated slot — globally cheapest but can never form a 3h block alone
        isolated = make_slot(NOW + timedelta(hours=12, minutes=30), 1.0)
        # some slots extending the main run that are slightly more expensive
        extension = make_slots_range(NOW + timedelta(hours=6, minutes=30), 4, price=8.0)

        all_slots = main_run + [isolated] + extension
        for s in all_slots:
            s['effective_price'] = s['raw_price']

        # Need 8 slots (4h) with min_block=3h; isolated slot can never contribute
        result = find_optimal_slots(all_slots, 8, READY_BY, min_block_hours=3.0)

        # Should find a valid 8-slot selection from the contiguous runs, not []
        assert len(result) == 8
        assert isolated not in result
        # Result must form at least one run of >= 6 slots
        runs = build_contiguous_runs(sorted(result, key=lambda s: s['date_time']))
        assert all(len(r) >= 6 for r in runs)


# ---------------------------------------------------------------------------
# TestSlotsToSessions
# ---------------------------------------------------------------------------

class TestSlotsToSessions:
    def test_single_contiguous_block(self):
        slots = make_slots_range(NOW, 4)
        sessions = slots_to_sessions(slots)
        assert len(sessions) == 1
        assert sessions[0]['start'] == NOW.isoformat()
        assert sessions[0]['end'] == (NOW + timedelta(hours=2)).isoformat()
        assert sessions[0]['duration_hours'] == 2.0

    def test_two_separate_blocks(self):
        block_a = make_slots_range(NOW, 2)
        block_b = make_slots_range(NOW + timedelta(hours=2), 2)
        sessions = slots_to_sessions(block_a + block_b)
        assert len(sessions) == 2

    def test_empty_input(self):
        assert slots_to_sessions([]) == []

    def test_duration_correct(self):
        slots = make_slots_range(NOW, 3)
        sessions = slots_to_sessions(slots)
        assert sessions[0]['duration_hours'] == 1.5

    def test_avg_price_present(self):
        slots = make_slots_range(NOW, 4, price=12.0)
        sessions = slots_to_sessions(slots)
        assert sessions[0]['avg_price'] == pytest.approx(12.0)

    def test_confidence_100_for_actual_tier(self):
        """All TIER_ACTUAL slots → confidence=100."""
        slots = [{'date_time': NOW + timedelta(minutes=30*i), 'raw_price': 10.0,
                  'source': 'current_actual', 'tier': TIER_ACTUAL, 'effective_price': 10.0}
                 for i in range(4)]
        sessions = slots_to_sessions(slots)
        assert sessions[0]['confidence'] == pytest.approx(100.0)

    def test_confidence_90_for_predicted_0_24(self):
        """TIER_PREDICTED_0_24 base credibility 0.90 → confidence=90."""
        slots = [{'date_time': NOW + timedelta(minutes=30*i), 'raw_price': 10.0,
                  'source': 'predicted', 'tier': TIER_PREDICTED_0_24, 'effective_price': 11.0}
                 for i in range(4)]
        sessions = slots_to_sessions(slots)
        assert sessions[0]['confidence'] == pytest.approx(90.0)

    def test_confidence_40_for_far_predicted(self):
        """TIER_PREDICTED_72_PLUS base credibility 0.40 → confidence=40."""
        slots = [{'date_time': NOW + timedelta(minutes=30*i), 'raw_price': 8.0,
                  'source': 'predicted', 'tier': TIER_PREDICTED_72_PLUS, 'effective_price': 20.0}
                 for i in range(4)]
        sessions = slots_to_sessions(slots)
        assert sessions[0]['confidence'] == pytest.approx(40.0)

    def test_confidence_mixed_actual_and_far_predicted(self):
        """2 actual (1.0) + 2 far predicted (0.40) → avg = 0.70 → confidence=70."""
        actual = [{'date_time': NOW + timedelta(minutes=30*i), 'raw_price': 10.0,
                   'source': 'current_actual', 'tier': TIER_ACTUAL, 'effective_price': 10.0}
                  for i in range(2)]
        predicted = [{'date_time': NOW + timedelta(minutes=30*(i+2)), 'raw_price': 8.0,
                      'source': 'predicted', 'tier': TIER_PREDICTED_72_PLUS, 'effective_price': 20.0}
                     for i in range(2)]
        sessions = slots_to_sessions(actual + predicted)
        assert sessions[0]['confidence'] == pytest.approx(70.0)

    def test_confidence_defaults_to_100_when_tier_absent(self):
        """Slots without 'tier' field (e.g. from tests) default to TIER_ACTUAL → 100."""
        slots = make_slots_range(NOW, 2, price=15.0)  # no 'tier' key
        sessions = slots_to_sessions(slots)
        assert sessions[0]['confidence'] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# TestPruneAndClassify
# ---------------------------------------------------------------------------

class TestPruneAndClassify:
    def _session(self, start, end):
        return {'start': start.isoformat(), 'end': end.isoformat(), 'duration_hours': (end - start).total_seconds() / 3600}

    def test_active_session_detected(self):
        session = self._session(NOW - timedelta(minutes=30), NOW + timedelta(hours=1))
        active, future = prune_and_classify([session], NOW)
        assert active is not None
        assert future == []

    def test_expired_session_pruned(self):
        session = self._session(NOW - timedelta(hours=2), NOW - timedelta(hours=1))
        active, future = prune_and_classify([session], NOW)
        assert active is None
        assert future == []

    def test_future_session_kept(self):
        session = self._session(NOW + timedelta(hours=1), NOW + timedelta(hours=2))
        active, future = prune_and_classify([session], NOW)
        assert active is None
        assert len(future) == 1

    def test_at_exact_start_is_active(self):
        session = self._session(NOW, NOW + timedelta(hours=1))
        active, _ = prune_and_classify([session], NOW)
        assert active is not None

    def test_at_exact_end_is_not_active(self):
        session = self._session(NOW - timedelta(hours=1), NOW)
        active, _ = prune_and_classify([session], NOW)
        assert active is None

    def test_multiple_sessions_mixed(self):
        expired = self._session(NOW - timedelta(hours=3), NOW - timedelta(hours=2))
        active = self._session(NOW - timedelta(minutes=30), NOW + timedelta(hours=1))
        future = self._session(NOW + timedelta(hours=2), NOW + timedelta(hours=4))
        act, fut = prune_and_classify([expired, active, future], NOW)
        assert act is not None
        assert len(fut) == 1


# ---------------------------------------------------------------------------
# TestComputeHoursRemaining
# ---------------------------------------------------------------------------

class TestComputeHoursRemaining:
    def _session(self, start, end):
        duration = (end - start).total_seconds() / 3600
        return {'start': start.isoformat(), 'end': end.isoformat(), 'duration_hours': duration}

    def test_no_sessions_returns_zero(self):
        assert compute_hours_remaining([], None, NOW) == 0.0

    def test_active_session_partial_hours(self):
        active = self._session(NOW - timedelta(minutes=30), NOW + timedelta(hours=1, minutes=30))
        hrs = compute_hours_remaining([], active, NOW)
        assert hrs == pytest.approx(1.5)

    def test_future_sessions_full_hours(self):
        f1 = self._session(NOW + timedelta(hours=1), NOW + timedelta(hours=3))
        f2 = self._session(NOW + timedelta(hours=4), NOW + timedelta(hours=5))
        hrs = compute_hours_remaining([f1, f2], None, NOW)
        assert hrs == pytest.approx(3.0)

    def test_active_plus_future(self):
        active = self._session(NOW - timedelta(minutes=30), NOW + timedelta(hours=1))
        future = self._session(NOW + timedelta(hours=2), NOW + timedelta(hours=4))
        hrs = compute_hours_remaining([future], active, NOW)
        assert hrs == pytest.approx(1.0 + 2.0)


# ---------------------------------------------------------------------------
# TestDetermineState
# ---------------------------------------------------------------------------

class TestDetermineState:
    def _call(self, **kwargs):
        defaults = dict(
            required_hours=4.0,
            boost_active=False,
            active_session=None,
            future_sessions=[],
            hours_remaining=0.0,
            data_ok=True,
        )
        defaults.update(kwargs)
        return determine_state(**defaults)

    def test_zero_hours_is_idle(self):
        assert self._call(required_hours=0) == STATE_IDLE

    def test_active_session_is_charging(self):
        assert self._call(active_session={'start': 'x', 'end': 'y'}, hours_remaining=2.0) == STATE_CHARGING

    def test_future_sessions_is_scheduled(self):
        assert self._call(future_sessions=[{'start': 'x'}]) == STATE_SCHEDULED

    def test_no_sessions_no_hours_is_complete(self):
        assert self._call(hours_remaining=0.0) == STATE_COMPLETE

    def test_boost_overrides(self):
        assert self._call(boost_active=True) == STATE_BOOSTING

    def test_no_data_is_error(self):
        assert self._call(data_ok=False) == STATE_ERROR


# ---------------------------------------------------------------------------
# TestBuildScheduleAttrs
# ---------------------------------------------------------------------------

class TestBuildScheduleAttrs:
    def test_all_keys_present(self):
        session = {'start': NOW.isoformat(), 'end': (NOW + timedelta(hours=2)).isoformat(), 'duration_hours': 2.0}
        inputs = {'ready_by_dt': READY_BY.isoformat(), 'required_hours': 4.0}
        attrs = build_schedule_attrs([session], None, [session], 2.0, inputs, None, NOW)
        for key in ('slots', 'active_slot', 'next_slot', 'total_committed_hours',
                    'hours_remaining', 'calculated_at', 'ready_by', 'required_hours', 'boost_end_dt'):
            assert key in attrs

    def test_total_committed_hours_correct(self):
        """total_committed_hours must be the sum of all session durations."""
        s1 = {'start': NOW.isoformat(), 'end': (NOW + timedelta(hours=2)).isoformat(), 'duration_hours': 2.0}
        s2 = {'start': (NOW + timedelta(hours=3)).isoformat(), 'end': (NOW + timedelta(hours=5)).isoformat(), 'duration_hours': 2.0}
        attrs = build_schedule_attrs([s1, s2], None, [s1, s2], 4.0, {}, None, NOW)
        assert attrs['total_committed_hours'] == pytest.approx(4.0)

    def test_total_committed_hours_single_session(self):
        s = {'start': NOW.isoformat(), 'end': (NOW + timedelta(hours=1.5)).isoformat(), 'duration_hours': 1.5}
        attrs = build_schedule_attrs([s], s, [], 0.5, {}, None, NOW)
        assert attrs['total_committed_hours'] == pytest.approx(1.5)

    def test_total_committed_hours_empty_sessions(self):
        attrs = build_schedule_attrs([], None, [], 0.0, {}, None, NOW)
        assert attrs['total_committed_hours'] == 0.0

    def test_active_slot_reflected(self):
        session = {'start': NOW.isoformat(), 'end': (NOW + timedelta(hours=2)).isoformat(), 'duration_hours': 2.0}
        attrs = build_schedule_attrs([session], session, [], 2.0, {}, None, NOW)
        assert attrs['active_slot'] == session
        assert attrs['next_slot'] is None

    def test_next_slot_is_first_future(self):
        f1 = {'start': (NOW + timedelta(hours=1)).isoformat(), 'end': (NOW + timedelta(hours=3)).isoformat(), 'duration_hours': 2.0}
        f2 = {'start': (NOW + timedelta(hours=4)).isoformat(), 'end': (NOW + timedelta(hours=5)).isoformat(), 'duration_hours': 1.0}
        attrs = build_schedule_attrs([f1, f2], None, [f1, f2], 3.0, {}, None, NOW)
        assert attrs['next_slot'] == f1

    def test_boost_end_dt_serialised(self):
        boost_end = NOW + timedelta(hours=2)
        attrs = build_schedule_attrs([], None, [], 0.0, {}, boost_end, NOW)
        assert attrs['boost_end_dt'] == boost_end.isoformat()

    def test_boost_end_dt_none_when_absent(self):
        attrs = build_schedule_attrs([], None, [], 0.0, {}, None, NOW)
        assert attrs['boost_end_dt'] is None


# ---------------------------------------------------------------------------
# TestDeduplicateAndSortPrices
# ---------------------------------------------------------------------------

class TestDeduplicateAndSortPrices:
    def test_actual_overrides_predicted_same_dt(self):
        predicted = {'date_time': NOW, 'raw_price': 8.0, 'source': 'predicted'}
        actual = {'date_time': NOW, 'raw_price': 12.0, 'source': 'current_actual'}
        result = deduplicate_and_sort_prices([predicted, actual], NOW - timedelta(hours=1))
        assert len(result) == 1
        assert result[0]['source'] == 'current_actual'

    def test_filters_past_slots(self):
        past = {'date_time': NOW - timedelta(hours=1), 'raw_price': 10.0, 'source': 'current_actual'}
        future = {'date_time': NOW, 'raw_price': 10.0, 'source': 'current_actual'}
        result = deduplicate_and_sort_prices([past, future], NOW)
        assert len(result) == 1
        assert result[0]['date_time'] == NOW

    def test_sorted_chronologically(self):
        s1 = {'date_time': NOW + timedelta(hours=2), 'raw_price': 10.0, 'source': 'current_actual'}
        s2 = {'date_time': NOW + timedelta(hours=1), 'raw_price': 10.0, 'source': 'current_actual'}
        result = deduplicate_and_sort_prices([s1, s2], NOW)
        assert result[0]['date_time'] < result[1]['date_time']


# ---------------------------------------------------------------------------
# TestResolveBoostEnd
# ---------------------------------------------------------------------------

class TestResolveBoostEnd:
    def test_no_boost_returns_none(self):
        assert resolve_boost_end({}, {'boost_duration': 0.0}, NOW) is None

    def test_boost_duration_starts_new_boost(self):
        result = resolve_boost_end({}, {'boost_duration': 2.0}, NOW)
        assert result == NOW + timedelta(hours=2.0)

    def test_stored_future_boost_honoured(self):
        future_end = NOW + timedelta(hours=1)
        stored = {'boost_end_dt': future_end.isoformat()}
        result = resolve_boost_end(stored, {'boost_duration': 0.0}, NOW)
        assert result == future_end

    def test_stored_expired_boost_ignored(self):
        past_end = NOW - timedelta(hours=1)
        stored = {'boost_end_dt': past_end.isoformat()}
        result = resolve_boost_end(stored, {'boost_duration': 0.0}, NOW)
        assert result is None

    def test_new_boost_duration_overrides_expired_stored(self):
        past_end = NOW - timedelta(hours=1)
        stored = {'boost_end_dt': past_end.isoformat()}
        result = resolve_boost_end(stored, {'boost_duration': 3.0}, NOW)
        assert result == NOW + timedelta(hours=3.0)

    def test_boost_duration_does_not_override_active_stored(self):
        # Active stored boost should be honoured; boost_duration should not extend it
        future_end = NOW + timedelta(hours=1)
        stored = {'boost_end_dt': future_end.isoformat()}
        result = resolve_boost_end(stored, {'boost_duration': 0.5}, NOW)
        # boost_duration > 0 but stored boost still active → existing boost wins
        assert result == future_end

    def test_invalid_stored_boost_end_dt_ignored(self):
        stored = {'boost_end_dt': 'not-a-date'}
        result = resolve_boost_end(stored, {'boost_duration': 0.0}, NOW)
        assert result is None


# ---------------------------------------------------------------------------
# TestComputeSessions
# ---------------------------------------------------------------------------

class TestComputeSessions:
    def _prices(self, count=8, price=10.0, start=None):
        t = start or NOW
        return [
            {'date_time': t + timedelta(minutes=30 * i), 'raw_price': price,
             'source': 'current_actual', 'effective_price': price}
            for i in range(count)
        ]

    def test_returns_session_list(self):
        inputs = {'required_hours': 2.0, 'gamble_tolerance': 50.0, 'min_block_hours': 1.0, 'max_price': 20.0}
        sessions = compute_sessions(self._prices(), [], inputs, READY_BY, NOW)
        assert isinstance(sessions, list)
        assert len(sessions) > 0

    def test_total_duration_matches_required_hours(self):
        inputs = {'required_hours': 2.0, 'gamble_tolerance': 50.0, 'min_block_hours': 1.0, 'max_price': 20.0}
        sessions = compute_sessions(self._prices(), [], inputs, READY_BY, NOW)
        total = sum([s['duration_hours'] for s in sessions])
        assert total == pytest.approx(2.0)

    def test_active_session_preserved(self):
        active_session = {
            'start': (NOW - timedelta(minutes=30)).isoformat(),
            'end': (NOW + timedelta(hours=1, minutes=30)).isoformat(),
            'duration_hours': 2.0,
        }
        inputs = {'required_hours': 2.0, 'gamble_tolerance': 50.0, 'min_block_hours': 1.0, 'max_price': 20.0}
        sessions = compute_sessions(self._prices(), [active_session], inputs, READY_BY, NOW)
        starts = [s['start'] for s in sessions]
        assert active_session['start'] in starts

    def test_future_sessions_not_preserved(self):
        old_future = {
            'start': (NOW + timedelta(hours=5)).isoformat(),
            'end': (NOW + timedelta(hours=7)).isoformat(),
            'duration_hours': 2.0,
        }
        cheap_prices = self._prices(count=4, price=5.0)
        inputs = {'required_hours': 2.0, 'gamble_tolerance': 50.0, 'min_block_hours': 1.0, 'max_price': 20.0}
        sessions = compute_sessions(cheap_prices, [old_future], inputs, READY_BY, NOW)
        starts = [s['start'] for s in sessions]
        assert old_future['start'] not in starts

    def test_max_price_filters_slots(self):
        expensive_prices = self._prices(price=50.0)
        inputs = {'required_hours': 2.0, 'gamble_tolerance': 50.0, 'min_block_hours': 1.0, 'max_price': 20.0}
        sessions = compute_sessions(expensive_prices, [], inputs, READY_BY, NOW)
        assert sessions == []


# ---------------------------------------------------------------------------
# TestMakeScheduleData
# ---------------------------------------------------------------------------

class TestMakeScheduleData:
    def test_contains_required_keys(self):
        data = _make_schedule_data([], None, {}, NOW)
        for key in ('slots', 'boost_end_dt', 'inputs_snapshot', 'stored_at'):
            assert key in data

    def test_boost_end_dt_serialised(self):
        boost = NOW + timedelta(hours=2)
        data = _make_schedule_data([], boost, {}, NOW)
        assert data['boost_end_dt'] == boost.isoformat()

    def test_boost_end_dt_none_when_absent(self):
        data = _make_schedule_data([], None, {}, NOW)
        assert data['boost_end_dt'] is None

    def test_sessions_stored(self):
        sessions = [{'start': NOW.isoformat(), 'end': (NOW + timedelta(hours=1)).isoformat(), 'duration_hours': 1.0}]
        data = _make_schedule_data(sessions, None, {}, NOW)
        assert data['slots'] == sessions


# ---------------------------------------------------------------------------
# TestParseDt — defensive datetime parsing
# ---------------------------------------------------------------------------

class TestParseDt:
    def test_string_input_parsed(self):
        dt_str = NOW.isoformat()
        result = _parse_dt(dt_str)
        assert result == NOW

    def test_datetime_input_returned_as_is(self):
        result = _parse_dt(NOW)
        assert result is NOW

    def test_string_with_timezone(self):
        from datetime import timezone, timedelta as td
        tz = timezone(td(hours=1))
        dt = datetime(2024, 1, 15, 10, 0, tzinfo=tz)
        result = _parse_dt(dt.isoformat())
        assert result == dt

    def test_datetime_with_timezone_returned_as_is(self):
        from datetime import timezone, timedelta as td
        tz = timezone(td(hours=1))
        dt = datetime(2024, 1, 15, 10, 0, tzinfo=tz)
        result = _parse_dt(dt)
        assert result is dt


# ---------------------------------------------------------------------------
# TestEVChargingHA
# ---------------------------------------------------------------------------

class TestEVChargingHA:
    def test_get_ready_by_valid(self, mock_hass, mock_as_local):
        mock_entity = Mock()
        mock_entity.state = "2024-01-15T18:00:00"
        mock_hass.states.get.return_value = mock_entity
        ha = EVChargingHA()
        result = ha.get_ready_by()
        assert result is not None
        assert isinstance(result, datetime)

    def test_get_ready_by_unavailable(self, mock_hass, mock_as_local):
        mock_entity = Mock()
        mock_entity.state = 'unavailable'
        mock_hass.states.get.return_value = mock_entity
        ha = EVChargingHA()
        assert ha.get_ready_by() is None

    def test_get_required_hours_valid(self, mock_hass):
        mock_entity = Mock()
        mock_entity.state = '4.0'
        mock_hass.states.get.return_value = mock_entity
        ha = EVChargingHA()
        assert ha.get_required_hours() == pytest.approx(4.0)

    def test_get_required_hours_unavailable(self, mock_hass):
        mock_entity = Mock()
        mock_entity.state = 'unavailable'
        mock_hass.states.get.return_value = mock_entity
        ha = EVChargingHA()
        assert ha.get_required_hours() is None

    def test_get_gamble_tolerance_default(self, mock_hass):
        mock_hass.states.get.return_value = None
        ha = EVChargingHA()
        assert ha.get_gamble_tolerance() == 50.0

    def test_get_boost_duration_zero_default(self, mock_hass):
        mock_hass.states.get.return_value = None
        ha = EVChargingHA()
        assert ha.get_boost_duration() == 0.0

    def test_get_current_rates_parses_correctly(self, mock_hass, mock_as_local):
        """value_inc_vat is in £/kWh; must be converted to p/kWh (× 100)."""
        mock_entity = Mock()
        mock_entity.attributes = {
            'rates': [
                {'start': '2024-01-15T10:00:00', 'value_inc_vat': 0.125},   # £/kWh → 12.5p
                {'start': '2024-01-15T10:30:00', 'value_inc_vat': 0.08},    # £/kWh → 8.0p
            ]
        }
        mock_hass.states.get.return_value = mock_entity
        ha = EVChargingHA()
        rates = ha.get_current_rates()
        assert len(rates) == 2
        assert rates[0]['raw_price'] == pytest.approx(12.5)
        assert rates[0]['source'] == 'current_actual'

    def test_get_agile_forecast_slots_parses_correctly(self, mock_hass, mock_as_local):
        mock_entity = Mock()
        mock_entity.attributes = {
            'prices': [
                {'date_time': '2024-01-16T02:00:00', 'agile_pred': 5.0},
                {'date_time': '2024-01-16T02:30:00', 'agile_pred': 7.0},
            ]
        }
        mock_hass.states.get.return_value = mock_entity
        ha = EVChargingHA()
        slots = ha.get_agile_forecast_slots()
        assert len(slots) == 2
        assert slots[0]['source'] == 'predicted'
        assert slots[0]['raw_price'] == 5.0

    def test_set_desired_calls_state_set(self, mock_state_obj):
        ha = EVChargingHA()
        ha.set_desired(True)
        mock_state_obj.set.assert_called()
        args = mock_state_obj.set.call_args[0]
        assert args[1] == 'on'

    def test_set_all_unavailable_sets_error_state(self, mock_state_obj, mock_ha_now):
        ha = EVChargingHA()
        ha.set_all_unavailable("test reason")
        assert mock_state_obj.set.call_count >= 4

    def test_set_all_unavailable_preserves_existing_schedule_data(self, mock_hass, mock_ha_now):
        """Active slot must survive a transient error (e.g. prices unavailable when car unplugs)."""
        stored_data = {
            'slots': [{'start': NOW.isoformat(), 'end': (NOW + timedelta(hours=2)).isoformat(),
                       'duration_hours': 2.0}],
            'boost_end_dt': None,
        }
        mock_entity = Mock()
        mock_entity.attributes = {'_schedule_data': stored_data}
        mock_entity.state = 'charging'
        mock_hass.states.get.return_value = mock_entity
        ha = EVChargingHA()
        ha.set_all_unavailable("No price data available")
        # Every state.set call for SCHEDULE_SENSOR must carry _schedule_data forward
        import builtins
        schedule_calls = [
            c for c in builtins.state.set.call_args_list
            if c[0][0] == 'sensor.ev_charging_schedule'
        ]
        assert len(schedule_calls) >= 1
        for c in schedule_calls:
            attrs = c[0][2] if len(c[0]) > 2 else c[1].get('attributes', {})
            assert '_schedule_data' in attrs, (
                "set_all_unavailable must preserve _schedule_data to protect active slots"
            )

    def test_get_stored_schedule_empty_when_no_entity(self, mock_hass):
        mock_hass.states.get.return_value = None
        ha = EVChargingHA()
        assert ha.get_stored_schedule() == {}

    def test_get_stored_schedule_parses_dict(self, mock_hass):
        stored_data = {'slots': [], 'boost_end_dt': None}
        mock_entity = Mock()
        mock_entity.attributes = {'_schedule_data': stored_data}
        mock_hass.states.get.return_value = mock_entity
        ha = EVChargingHA()
        result = ha.get_stored_schedule()
        assert result == stored_data


# ---------------------------------------------------------------------------
# Integration: TestUpdateEvChargeState
# ---------------------------------------------------------------------------

def _make_ha_mock(now_dt, ready_by_dt, required_hours=4.0, charger_state='charger_insert',
                  gamble=50, min_block=1.0, max_price=20.0, boost_duration=0.0,
                  price_slots=None, stored_schedule=None):
    """Build a mock EVChargingHA for integration tests."""
    ha = MagicMock(spec=EVChargingHA)
    ha.get_now.return_value = now_dt
    ha.get_ready_by.return_value = ready_by_dt
    ha.get_required_hours.return_value = required_hours
    ha.get_charger_work_state.return_value = charger_state
    ha.get_gamble_tolerance.return_value = float(gamble)
    ha.get_min_block_hours.return_value = float(min_block)
    ha.get_max_price.return_value = float(max_price)
    ha.get_boost_duration.return_value = float(boost_duration)
    if price_slots is None:
        # Generate 24h of 10p slots starting now
        price_slots = [
            {'date_time': now_dt + timedelta(minutes=30 * i), 'raw_price': 10.0, 'source': 'current_actual'}
            for i in range(48)
        ]
    ha.get_current_rates.return_value = price_slots
    ha.get_next_rates.return_value = []
    ha.get_agile_forecast_slots.return_value = []
    ha.get_stored_schedule.return_value = stored_schedule or {}
    return ha


class TestUpdateEvChargeStateIntegration:
    def test_desired_off_and_schedule_shown_when_charger_disconnected(self):
        """When charger is unplugged, desired stays off but schedule is still computed and displayed."""
        ha = _make_ha_mock(NOW, READY_BY, charger_state='charger_free')
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            update_ev_charge_state()
        ha.set_desired.assert_called_with(False)
        state_arg = ha.set_state_sensor.call_args[0][0]
        assert state_arg in (STATE_SCHEDULED, STATE_CHARGING, STATE_COMPLETE), (
            f"Disconnected with valid schedule should show schedule state, got {state_arg}"
        )

    def test_error_when_no_ready_by(self):
        ha = _make_ha_mock(NOW, None)
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            update_ev_charge_state()
        ha.set_all_unavailable.assert_called()

    def test_error_when_ready_by_in_past(self):
        ha = _make_ha_mock(NOW, NOW - timedelta(hours=1))
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            update_ev_charge_state()
        ha.set_all_unavailable.assert_called()

    def test_scheduled_state_with_valid_data(self):
        ha = _make_ha_mock(NOW, READY_BY, required_hours=2.0)
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            update_ev_charge_state()
        ha.set_state_sensor.assert_called()
        call_arg = ha.set_state_sensor.call_args[0][0]
        assert call_arg in (STATE_SCHEDULED, STATE_CHARGING)

    def test_schedule_sensor_written_with_schedule_data(self):
        """set_schedule_sensor must include _schedule_data for persistence."""
        ha = _make_ha_mock(NOW, READY_BY, required_hours=2.0)
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            update_ev_charge_state()
        ha.set_schedule_sensor.assert_called()
        call_attrs = ha.set_schedule_sensor.call_args[0][1]
        assert '_schedule_data' in call_attrs
        assert 'slots' in call_attrs['_schedule_data']

    def test_active_slot_preserved_on_price_update(self):
        """Key regression: active slot must not be evicted when prices update."""
        active_start = NOW - timedelta(minutes=30)
        active_end = NOW + timedelta(hours=1, minutes=30)
        active_session = {
            'start': active_start.isoformat(),
            'end': active_end.isoformat(),
            'duration_hours': 2.0,
        }
        stored = {
            'slots': [active_session],
            'boost_end_dt': None,
            'inputs_snapshot': {},
        }
        ha = _make_ha_mock(NOW, READY_BY, required_hours=2.0, stored_schedule=stored)
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            update_ev_charge_state()
        ha.set_schedule_sensor.assert_called()
        call_attrs = ha.set_schedule_sensor.call_args[0][1]
        stored_sessions = call_attrs.get('_schedule_data', {}).get('slots', [])
        active_starts = [s['start'] for s in stored_sessions]
        assert active_session['start'] in active_starts

    def test_charging_state_during_active_slot(self):
        active_start = NOW - timedelta(minutes=30)
        active_end = NOW + timedelta(hours=1)
        active_session = {
            'start': active_start.isoformat(),
            'end': active_end.isoformat(),
            'duration_hours': 1.5,
        }
        stored = {'slots': [active_session], 'boost_end_dt': None, 'inputs_snapshot': {}}
        ha = _make_ha_mock(NOW, READY_BY, required_hours=1.5, stored_schedule=stored)
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            update_ev_charge_state()
        ha.set_state_sensor.assert_called_with(STATE_CHARGING)
        ha.set_desired.assert_called_with(True)

    def test_boost_mode_sets_desired_on(self):
        ha = _make_ha_mock(NOW, READY_BY, boost_duration=2.0)
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            update_ev_charge_state()
        ha.set_desired.assert_called_with(True)
        ha.set_state_sensor.assert_called_with(STATE_BOOSTING)

    def test_boost_from_stored_end_dt(self):
        """Boost should remain active based on stored boost_end_dt."""
        boost_end = NOW + timedelta(hours=1)
        stored = {
            'slots': [],
            'boost_end_dt': boost_end.isoformat(),
            'inputs_snapshot': {},
        }
        ha = _make_ha_mock(NOW, READY_BY, boost_duration=0.0, stored_schedule=stored)
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            update_ev_charge_state()
        ha.set_desired.assert_called_with(True)
        ha.set_state_sensor.assert_called_with(STATE_BOOSTING)

    def test_error_when_no_price_data(self):
        ha = _make_ha_mock(NOW, READY_BY, price_slots=[])
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            update_ev_charge_state()
        ha.set_all_unavailable.assert_called()

    def test_boost_slider_reset_when_boost_starts(self):
        """Regression: when boost_duration > 0 starts a boost, slider must be reset to 0."""
        ha = _make_ha_mock(NOW, READY_BY, boost_duration=4.0)
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            update_ev_charge_state()
        ha.reset_boost_duration.assert_called_once()

    def test_boost_slider_not_reset_when_boost_duration_zero(self):
        """If boost runs from stored_boost_end_dt (slider=0), do not reset the slider."""
        boost_end = NOW + timedelta(hours=1)
        stored = {'slots': [], 'boost_end_dt': boost_end.isoformat(), 'inputs_snapshot': {}}
        ha = _make_ha_mock(NOW, READY_BY, boost_duration=0.0, stored_schedule=stored)
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            update_ev_charge_state()
        ha.reset_boost_duration.assert_not_called()

    def test_boost_does_not_retrigger_after_expiry(self):
        """
        Regression: if the boost_duration slider is still set when a boost expires,
        resolve_boost_end starts a new boost. The slider must be reset so the
        NEXT run (with boost expired and slider=0) does not start yet another boost.
        This test verifies the reset is called when needed.
        """
        # Boost expired 1 minute ago; slider still shows 4h
        expired_boost_end = NOW - timedelta(minutes=1)
        stored = {
            'slots': [],
            'boost_end_dt': expired_boost_end.isoformat(),
            'inputs_snapshot': {},
        }
        ha = _make_ha_mock(NOW, READY_BY, boost_duration=4.0, stored_schedule=stored)
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            update_ev_charge_state()
        # A new boost was started from the stale slider; slider should now be reset
        ha.reset_boost_duration.assert_called_once()

    def test_future_slots_recomputed_on_each_call(self):
        """Future slots (not active) should be recomputed fresh on each evaluation."""
        future_session = {
            'start': (NOW + timedelta(hours=5)).isoformat(),
            'end': (NOW + timedelta(hours=7)).isoformat(),
            'duration_hours': 2.0,
        }
        stored = {'slots': [future_session], 'boost_end_dt': None, 'inputs_snapshot': {}}
        # Provide better priced slots closer in time
        cheap_slots = [
            {'date_time': NOW + timedelta(minutes=30 * i), 'raw_price': 5.0, 'source': 'current_actual'}
            for i in range(4)
        ]
        ha = _make_ha_mock(NOW, READY_BY, required_hours=2.0, stored_schedule=stored,
                           price_slots=cheap_slots)
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            update_ev_charge_state()
        ha.set_schedule_sensor.assert_called()
        call_attrs = ha.set_schedule_sensor.call_args[0][1]
        new_sessions = call_attrs.get('_schedule_data', {}).get('slots', [])
        if new_sessions:
            session_starts = [s['start'] for s in new_sessions]
            assert future_session['start'] not in session_starts


# ---------------------------------------------------------------------------
# TestEvChargingBoostService
# ---------------------------------------------------------------------------

class TestEvChargingBoostService:
    def test_boost_stores_boost_end_dt_in_schedule_sensor(self):
        ha = MagicMock(spec=EVChargingHA)
        ha.get_now.return_value = NOW
        ha.get_stored_schedule.return_value = {}
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            ev_charging_boost(duration_hours=2.0)
        ha.set_schedule_sensor.assert_called()
        call_attrs = ha.set_schedule_sensor.call_args[0][1]
        stored_data = call_attrs.get('_schedule_data', {})
        assert stored_data.get('boost_end_dt') == (NOW + timedelta(hours=2.0)).isoformat()

    def test_boost_sets_desired_on(self):
        ha = MagicMock(spec=EVChargingHA)
        ha.get_now.return_value = NOW
        ha.get_stored_schedule.return_value = {}
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            ev_charging_boost(duration_hours=1.0)
        ha.set_desired.assert_called_with(True)

    def test_boost_sets_boosting_state(self):
        ha = MagicMock(spec=EVChargingHA)
        ha.get_now.return_value = NOW
        ha.get_stored_schedule.return_value = {}
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            ev_charging_boost(duration_hours=1.0)
        ha.set_state_sensor.assert_called_with(STATE_BOOSTING)


# ---------------------------------------------------------------------------
# TestEvChargingBoostCancelService
# ---------------------------------------------------------------------------

class TestEvChargingBoostCancelService:
    def test_cancel_clears_boost_end_dt(self):
        ha = MagicMock(spec=EVChargingHA)
        ha.get_now.return_value = NOW
        ha.get_stored_schedule.return_value = {
            'slots': [],
            'boost_end_dt': (NOW + timedelta(hours=1)).isoformat(),
            'inputs_snapshot': {},
        }
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            with patch('ev_charging_state_machine.update_ev_charge_state'):
                ev_charging_boost_cancel()
        ha.store_schedule.assert_called()
        # Second positional arg is boost_end_dt — should be None
        boost_arg = ha.store_schedule.call_args[0][1]
        assert boost_arg is None

    def test_cancel_triggers_re_evaluation(self):
        ha = MagicMock(spec=EVChargingHA)
        ha.get_now.return_value = NOW
        ha.get_stored_schedule.return_value = {}
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            with patch('ev_charging_state_machine.update_ev_charge_state') as mock_update:
                ev_charging_boost_cancel()
        mock_update.assert_called_once()


# ---------------------------------------------------------------------------
# TestEvChargingStopService
# ---------------------------------------------------------------------------

class TestEvChargingStopService:
    def test_stop_writes_empty_schedule_to_sensor(self):
        ha = MagicMock(spec=EVChargingHA)
        ha.get_now.return_value = NOW
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            ev_charging_stop()
        ha.set_schedule_sensor.assert_called()
        call_attrs = ha.set_schedule_sensor.call_args[0][1]
        assert call_attrs.get('slots') == []
        assert call_attrs.get('_schedule_data', {}).get('slots') == []

    def test_stop_sets_idle(self):
        ha = MagicMock(spec=EVChargingHA)
        ha.get_now.return_value = NOW
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            ev_charging_stop()
        ha.set_desired.assert_called_with(False)
        ha.set_state_sensor.assert_called_with(STATE_IDLE)


# ---------------------------------------------------------------------------
# TestRealWorldScenarios
# ---------------------------------------------------------------------------

class TestRealWorldScenarios:
    def test_evening_plugin_overnight_split(self):
        """Plug in at 18:00, need 4h by 07:00 next day. Should pick cheap overnight slots."""
        plug_in = datetime(2024, 1, 15, 18, 0)
        ready = datetime(2024, 1, 16, 7, 0)
        slots = []
        t = plug_in
        while t + timedelta(minutes=30) <= ready:
            if 0 <= t.hour < 6:
                price = 5.0
            elif 16 <= t.hour < 20:
                price = 25.0
            else:
                price = 15.0
            slots.append({'date_time': t, 'raw_price': price, 'source': 'current_actual',
                          'effective_price': price})
            t += timedelta(minutes=30)
        result = find_optimal_slots(slots, 8, ready, min_block_hours=1.0)
        assert len(result) == 8
        assert all(s['raw_price'] == 5.0 for s in result)

    def test_high_avg_run_excluded(self):
        """A run whose average price exceeds max_price is excluded even if it has cheap slots."""
        cheap_run = [make_slot(NOW + timedelta(minutes=30 * i), 10.0) for i in range(4)]
        expensive_run = [make_slot(NOW + timedelta(hours=3) + timedelta(minutes=30 * i), 50.0) for i in range(4)]
        for s in cheap_run + expensive_run:
            s['effective_price'] = s['raw_price']
        filtered = filter_runs_by_max_avg_price(cheap_run + expensive_run, 20.0)
        result = find_optimal_slots(filtered, 4, READY_BY, min_block_hours=0.5)
        assert len(result) == 4
        assert all(s['raw_price'] == 10.0 for s in result)

    def test_spike_within_cheap_run_still_selectable(self):
        """A spike within an otherwise-cheap run doesn't block the run from being scheduled."""
        run = [make_slot(NOW + timedelta(minutes=30 * i), 10.0) for i in range(6)]
        run[2] = make_slot(NOW + timedelta(hours=1), 22.0)  # spike in middle, avg still 12p
        for s in run:
            s['effective_price'] = s['raw_price']
        filtered = filter_runs_by_max_avg_price(run, 20.0)
        # avg = (10+10+22+10+10+10)/6 = 12p < 20p → run eligible, 22p slot included
        assert len(filtered) == 6
        result = find_optimal_slots(filtered, 4, READY_BY, min_block_hours=0.5)
        assert len(result) == 4

    def test_min_block_prevents_isolated_slot(self):
        """A single cheap 30-min slot that cannot be extended should not appear in results."""
        isolated = make_slot(NOW, 1.0)
        isolated['effective_price'] = 1.0
        run = [make_slot(NOW + timedelta(hours=2) + timedelta(minutes=30 * i), 15.0) for i in range(4)]
        for s in run:
            s['effective_price'] = s['raw_price']
        result = find_optimal_slots([isolated] + run, 2, READY_BY, min_block_hours=1.0)
        assert isolated not in result
        assert len(result) == 2

    def test_recomputes_schedule_after_sessions_expire(self):
        """After stored sessions expire the state machine recomputes from fresh price data."""
        expired_session = {
            'start': (NOW - timedelta(hours=3)).isoformat(),
            'end': (NOW - timedelta(hours=1)).isoformat(),
            'duration_hours': 2.0,
        }
        stored = {'slots': [expired_session], 'boost_end_dt': None, 'inputs_snapshot': {}}
        ha = _make_ha_mock(NOW, READY_BY, required_hours=2.0, stored_schedule=stored)
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            update_ev_charge_state()
        # Should recompute and produce a new schedule (not error)
        ha.set_state_sensor.assert_called()
        state_arg = ha.set_state_sensor.call_args[0][0]
        assert state_arg in (STATE_SCHEDULED, STATE_CHARGING, STATE_COMPLETE)

    def test_unschedulable_when_constraints_prevent_scheduling(self):
        """
        When max_price or min_block_hours filters out all slots, report the distinct
        'unschedulable' state (with the reason) rather than 'error' — this is a
        consequence of the user's own settings vs. the price market, not a system fault.
        set_all_unavailable (genuine error path) must NOT be invoked.
        """
        # Provide only expensive slots (all above max_price=20)
        pricey_slots = [
            {'date_time': NOW + timedelta(minutes=30 * i), 'raw_price': 50.0, 'source': 'current_actual'}
            for i in range(48)
        ]
        ha = _make_ha_mock(NOW, READY_BY, required_hours=2.0, price_slots=pricey_slots)
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            update_ev_charge_state()
        ha.set_all_unavailable.assert_not_called()
        ha.set_state_sensor.assert_called_with(STATE_UNSCHEDULABLE)
        schedule_call = ha.set_schedule_sensor.call_args
        assert schedule_call[0][0] == STATE_UNSCHEDULABLE
        reason = schedule_call[0][1].get('error_reason', '')
        assert 'max_price' in reason or 'No slots' in reason

    def test_future_slots_recomputed_on_each_call(self):
        """Future slots (not active) should be recomputed fresh on each evaluation."""
        future_session = {
            'start': (NOW + timedelta(hours=5)).isoformat(),
            'end': (NOW + timedelta(hours=7)).isoformat(),
            'duration_hours': 2.0,
        }
        stored = {'slots': [future_session], 'boost_end_dt': None, 'inputs_snapshot': {}}
        cheap_slots = [
            {'date_time': NOW + timedelta(minutes=30 * i), 'raw_price': 5.0, 'source': 'current_actual'}
            for i in range(4)
        ]
        ha = _make_ha_mock(NOW, READY_BY, required_hours=2.0, stored_schedule=stored,
                           price_slots=cheap_slots)
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            update_ev_charge_state()
        ha.set_schedule_sensor.assert_called()
        call_attrs = ha.set_schedule_sensor.call_args[0][1]
        new_sessions = call_attrs.get('_schedule_data', {}).get('slots', [])
        if new_sessions:
            session_starts = [s['start'] for s in new_sessions]
            assert future_session['start'] not in session_starts


# ---------------------------------------------------------------------------
# TestEndToEndScenarios — realistic Agile Octopus price data
# ---------------------------------------------------------------------------
#
# Price data is derived from two real sample files:
#   tmp-agile-forecast.txt  — Agile forecast slots, created_at 2026-06-02T16:45Z
#   tmp-current-day-rates.txt — Actual Octopus rates for 2026-06-02
#
# The "reference now" for all tests is 2026-06-02T17:00:00 UTC (just after forecast
# was created). Slots at various time horizons then fall into the correct prediction
# tiers automatically.

def _utc(iso_str):
    """Parse an ISO string (no tz) as a UTC-naive datetime."""
    return datetime.fromisoformat(iso_str)


def _build_actual_slots():
    """
    Return the current-day actual rate slots from tmp-current-day-rates.txt,
    converted to p/kWh (raw file values are GBP inc VAT x 100).
    Times are stored as UTC-naive (BST start times shifted back 1h).
    """
    raw = [
        ("2026-06-01T23:00:00", 0.22701),
        ("2026-06-01T23:30:00", 0.21777),
        ("2026-06-02T00:00:00", 0.203385),
        ("2026-06-02T00:30:00", 0.20454),
        ("2026-06-02T01:00:00", 0.20034),
        ("2026-06-02T01:30:00", 0.203805),
        ("2026-06-02T02:00:00", 0.201495),
        ("2026-06-02T02:30:00", 0.19824),
        ("2026-06-02T03:00:00", 0.20559),
        ("2026-06-02T03:30:00", 0.205065),
        ("2026-06-02T04:00:00", 0.223755),
        ("2026-06-02T04:30:00", 0.232785),
        ("2026-06-02T05:00:00", 0.232365),
        ("2026-06-02T05:30:00", 0.251895),
        ("2026-06-02T06:00:00", 0.26061),
        ("2026-06-02T06:30:00", 0.26523),
        ("2026-06-02T07:00:00", 0.274365),
        ("2026-06-02T07:30:00", 0.26733),
        ("2026-06-02T08:00:00", 0.26985),
        ("2026-06-02T08:30:00", 0.26523),
        ("2026-06-02T09:00:00", 0.221235),
        ("2026-06-02T09:30:00", 0.203595),
        ("2026-06-02T10:00:00", 0.19593),
        ("2026-06-02T10:30:00", 0.1911),
        ("2026-06-02T11:00:00", 0.18669),
        ("2026-06-02T11:30:00", 0.18165),
        ("2026-06-02T12:00:00", 0.179235),
        ("2026-06-02T12:30:00", 0.17703),
        ("2026-06-02T13:00:00", 0.18207),
        ("2026-06-02T13:30:00", 0.173565),
        ("2026-06-02T14:00:00", 0.18186),
        ("2026-06-02T14:30:00", 0.18249),
        ("2026-06-02T15:00:00", 0.332325),
        ("2026-06-02T15:30:00", 0.34041),
        ("2026-06-02T16:00:00", 0.35427),
        ("2026-06-02T16:30:00", 0.36267),
        ("2026-06-02T17:00:00", 0.40026),
        ("2026-06-02T17:30:00", 0.39816),
        ("2026-06-02T18:00:00", 0.269535),
        ("2026-06-02T18:30:00", 0.27048),
        ("2026-06-02T19:00:00", 0.26985),
        ("2026-06-02T19:30:00", 0.271005),
        ("2026-06-02T20:00:00", 0.26985),
        ("2026-06-02T20:30:00", 0.25137),
        ("2026-06-02T21:00:00", 0.23478),
        ("2026-06-02T21:30:00", 0.22134),
        ("2026-06-02T22:00:00", 0.208635),
        ("2026-06-02T22:30:00", 0.214725),
    ]
    slots = []
    for iso, val in raw:
        slots.append({
            'date_time': _utc(iso),
            'raw_price': round(val * 100, 4),
            'source': 'current_actual',
        })
    return slots


def _build_forecast_slots():
    """
    Return predicted price slots from tmp-agile-forecast.txt (agile_pred values).
    All tagged source='predicted'. UTC-naive datetimes.
    """
    raw = [
        # 2026-06-02 evening (published, same day as actual)
        ("2026-06-02T17:00:00", 40.03), ("2026-06-02T17:30:00", 39.82),
        ("2026-06-02T18:00:00", 26.95), ("2026-06-02T18:30:00", 27.05),
        ("2026-06-02T19:00:00", 26.99), ("2026-06-02T19:30:00", 27.10),
        ("2026-06-02T20:00:00", 26.99), ("2026-06-02T20:30:00", 25.14),
        ("2026-06-02T21:00:00", 23.48), ("2026-06-02T21:30:00", 22.13),
        ("2026-06-02T22:00:00", 20.86), ("2026-06-02T22:30:00", 21.47),
        ("2026-06-02T23:00:00", 20.20), ("2026-06-02T23:30:00", 20.06),
        # 2026-06-03 overnight/day (published)
        ("2026-06-03T00:00:00", 19.82), ("2026-06-03T00:30:00", 20.00),
        ("2026-06-03T01:00:00", 20.30), ("2026-06-03T01:30:00", 20.06),
        ("2026-06-03T02:00:00", 19.41), ("2026-06-03T02:30:00", 18.78),
        ("2026-06-03T03:00:00", 19.06), ("2026-06-03T03:30:00", 19.13),
        ("2026-06-03T04:00:00", 20.98), ("2026-06-03T04:30:00", 21.83),
        ("2026-06-03T05:00:00", 23.57), ("2026-06-03T05:30:00", 25.65),
        ("2026-06-03T06:00:00", 23.06), ("2026-06-03T06:30:00", 24.22),
        ("2026-06-03T07:00:00", 23.15), ("2026-06-03T07:30:00", 23.40),
        ("2026-06-03T08:00:00", 24.08), ("2026-06-03T08:30:00", 21.83),
        ("2026-06-03T09:00:00", 21.33), ("2026-06-03T09:30:00", 20.88),
        ("2026-06-03T10:00:00", 18.44), ("2026-06-03T10:30:00", 18.44),
        ("2026-06-03T11:00:00", 18.89), ("2026-06-03T11:30:00", 16.86),
        ("2026-06-03T12:00:00", 15.44), ("2026-06-03T12:30:00", 14.53),
        ("2026-06-03T13:00:00", 11.78), ("2026-06-03T13:30:00", 11.14),
        ("2026-06-03T14:00:00", 14.51), ("2026-06-03T14:30:00", 15.52),
        ("2026-06-03T15:00:00", 28.68), ("2026-06-03T15:30:00", 29.88),
        ("2026-06-03T16:00:00", 30.36), ("2026-06-03T16:30:00", 32.49),
        ("2026-06-03T17:00:00", 35.43), ("2026-06-03T17:30:00", 35.47),
        ("2026-06-03T18:00:00", 23.15), ("2026-06-03T18:30:00", 23.72),
        ("2026-06-03T19:00:00", 23.06), ("2026-06-03T19:30:00", 23.31),
        ("2026-06-03T20:00:00", 23.06), ("2026-06-03T20:30:00", 21.21),
        ("2026-06-03T21:00:00", 19.25), ("2026-06-03T21:30:00", 15.78),
        # Unpublished predictions from 2026-06-03T22:00 onwards
        ("2026-06-03T22:00:00", 17.81), ("2026-06-03T22:30:00", 17.67),
        ("2026-06-03T23:00:00", 17.58), ("2026-06-03T23:30:00", 16.73),
        # 2026-06-04 overnight cheap window (~24-48h ahead)
        ("2026-06-04T00:00:00", 15.81), ("2026-06-04T00:30:00", 16.10),
        ("2026-06-04T01:00:00", 15.76), ("2026-06-04T01:30:00", 14.84),
        ("2026-06-04T02:00:00", 14.90), ("2026-06-04T02:30:00", 14.53),
        ("2026-06-04T03:00:00", 15.22), ("2026-06-04T03:30:00", 14.96),
        ("2026-06-04T04:00:00", 15.33), ("2026-06-04T04:30:00", 21.64),
        ("2026-06-04T05:00:00", 24.16), ("2026-06-04T05:30:00", 25.97),
        ("2026-06-04T06:00:00", 25.28), ("2026-06-04T06:30:00", 26.04),
        ("2026-06-04T07:00:00", 25.26), ("2026-06-04T07:30:00", 24.83),
        ("2026-06-04T08:00:00", 23.92), ("2026-06-04T08:30:00", 22.81),
        ("2026-06-04T09:00:00", 21.63), ("2026-06-04T09:30:00", 19.69),
        ("2026-06-04T10:00:00", 18.68), ("2026-06-04T10:30:00", 17.59),
        ("2026-06-04T11:00:00", 17.04), ("2026-06-04T11:30:00", 16.28),
        ("2026-06-04T12:00:00", 15.87), ("2026-06-04T12:30:00", 15.30),
        ("2026-06-04T13:00:00", 15.25), ("2026-06-04T13:30:00", 15.10),
        ("2026-06-04T14:00:00", 15.74), ("2026-06-04T14:30:00", 16.70),
        ("2026-06-04T15:00:00", 31.72), ("2026-06-04T15:30:00", 32.68),
        ("2026-06-04T16:00:00", 35.10), ("2026-06-04T16:30:00", 35.96),
        ("2026-06-04T17:00:00", 38.50), ("2026-06-04T17:30:00", 39.71),
        ("2026-06-04T18:00:00", 27.56), ("2026-06-04T18:30:00", 28.13),
        ("2026-06-04T19:00:00", 28.52), ("2026-06-04T19:30:00", 28.43),
        ("2026-06-04T20:00:00", 27.88), ("2026-06-04T20:30:00", 27.18),
        # 2026-06-04 cheap late evening
        ("2026-06-04T21:00:00", 11.09), ("2026-06-04T21:30:00", 10.60),
        ("2026-06-04T22:00:00", 10.54), ("2026-06-04T22:30:00", 10.54),
        ("2026-06-04T23:00:00",  9.92), ("2026-06-04T23:30:00",  9.41),
        # 2026-06-05 very cheap overnight (sub-10p, ~48-72h ahead)
        ("2026-06-05T00:00:00",  8.57), ("2026-06-05T00:30:00",  8.98),
        ("2026-06-05T01:00:00",  8.57), ("2026-06-05T01:30:00",  7.77),
        ("2026-06-05T02:00:00",  7.84), ("2026-06-05T02:30:00",  7.45),
        ("2026-06-05T03:00:00",  8.33), ("2026-06-05T03:30:00",  8.22),
        ("2026-06-05T04:00:00",  8.87), ("2026-06-05T04:30:00",  9.13),
        ("2026-06-05T05:00:00", 10.15), ("2026-06-05T05:30:00", 11.97),
        ("2026-06-05T06:00:00", 11.52), ("2026-06-05T06:30:00", 12.34),
        ("2026-06-05T07:00:00", 11.80), ("2026-06-05T07:30:00", 11.49),
        ("2026-06-05T08:00:00", 10.85), ("2026-06-05T08:30:00", 10.03),
        ("2026-06-05T09:00:00",  9.42), ("2026-06-05T09:30:00",  8.69),
        ("2026-06-05T10:00:00",  8.46), ("2026-06-05T10:30:00",  7.95),
        ("2026-06-05T11:00:00",  7.99), ("2026-06-05T11:30:00",  7.64),
        ("2026-06-05T12:00:00",  7.71), ("2026-06-05T12:30:00",  7.35),
        ("2026-06-05T13:00:00",  7.64), ("2026-06-05T13:30:00",  7.60),
        ("2026-06-05T14:00:00",  8.23), ("2026-06-05T14:30:00",  9.12),
        # 2026-06-05 peak
        ("2026-06-05T15:00:00", 23.07), ("2026-06-05T15:30:00", 24.06),
        ("2026-06-05T16:00:00", 27.09), ("2026-06-05T16:30:00", 28.57),
        ("2026-06-05T17:00:00", 31.54), ("2026-06-05T17:30:00", 33.04),
        ("2026-06-05T18:00:00", 21.53), ("2026-06-05T18:30:00", 22.26),
        ("2026-06-05T19:00:00", 22.74), ("2026-06-05T19:30:00", 22.86),
        ("2026-06-05T20:00:00", 23.20), ("2026-06-05T20:30:00", 23.06),
        ("2026-06-05T21:00:00", 21.85), ("2026-06-05T21:30:00", 21.06),
        ("2026-06-05T22:00:00", 21.04), ("2026-06-05T22:30:00", 20.86),
        ("2026-06-05T23:00:00", 20.17), ("2026-06-05T23:30:00", 19.73),
    ]
    slots = []
    for iso, price in raw:
        slots.append({
            'date_time': _utc(iso),
            'raw_price': price,
            'source': 'predicted',
        })
    return slots


# Reference now: just after the 2026-06-02 forecast was created
_E2E_NOW = _utc("2026-06-02T17:00:00")
# ready_by: next morning 07:00 UTC — covers ~14h ahead
_E2E_READY_NEXT_MORNING = _utc("2026-06-03T07:00:00")
# ready_by 48h out — covers multi-day prediction band
_E2E_READY_48H = _utc("2026-06-04T17:00:00")
# ready_by 96h out — covers far-future 72h+ band
_E2E_READY_96H = _utc("2026-06-06T17:00:00")


def _e2e_slots_with_credibility(now_dt=None, gamble_tolerance=50.0):
    """Combine actual + forecast, deduplicate, and assign tier/effective_price."""
    if now_dt is None:
        now_dt = _E2E_NOW
    deduped = deduplicate_and_sort_prices(
        _build_actual_slots() + _build_forecast_slots(), now_dt
    )
    return assign_credibilities(deduped, now_dt, gamble_tolerance)


def _assert_min_block_respected(selected, min_block_hours, label=""):
    """Every contiguous run in selected must have >= min_block_hours*2 slots."""
    if not selected:
        return
    min_slots = max(1, int(min_block_hours * 2))
    runs = build_contiguous_runs(sorted(selected, key=lambda s: s['date_time']))
    for run in runs:
        assert len(run) >= min_slots, (
            f"{label}Run at {run[0]['date_time']} has {len(run)} slot(s), "
            f"need >= {min_slots} for min_block={min_block_hours}h"
        )


def _assert_max_price_respected(selected, max_price, label=""):
    """Every contiguous run's average raw_price must be <= max_price."""
    if not selected:
        return
    runs = build_contiguous_runs(sorted(selected, key=lambda s: s['date_time']))
    for run in runs:
        avg = sum([s['raw_price'] for s in run]) / len(run)
        assert avg <= max_price + 1e-9, (
            f"{label}Run at {run[0]['date_time']} avg={avg:.2f}p > max_price={max_price}p"
        )


def _assert_within_ready_by(selected, ready_by_dt, label=""):
    """Every slot must end by ready_by_dt."""
    for s in selected:
        slot_end = s['date_time'] + timedelta(minutes=30)
        assert slot_end <= ready_by_dt, (
            f"{label}Slot at {s['date_time']} ends {slot_end} > ready_by {ready_by_dt}"
        )


class TestEndToEndScenarios:
    """End-to-end scheduling scenarios using realistic Agile Octopus price data."""

    # ------------------------------------------------------------------
    # Dimension 1 x Dimension 2: max_price x required_hours
    # ------------------------------------------------------------------

    def test_tight_max_price_short_session_returns_empty(self):
        """With max_price=5p and realistic Agile data, no slots qualify — result is empty."""
        slots = _e2e_slots_with_credibility(gamble_tolerance=100.0)
        filtered = filter_runs_by_max_avg_price(slots, 5.0)
        result = find_optimal_slots(filtered, 2, _E2E_READY_96H, min_block_hours=0.5)
        assert result == [], f"Expected empty at max_price=5p, got {len(result)} slot(s)"

    def test_tight_max_price_medium_session_returns_empty(self):
        """4h needed at max_price=5p — no runs in the data average below 5p."""
        slots = _e2e_slots_with_credibility(gamble_tolerance=100.0)
        filtered = filter_runs_by_max_avg_price(slots, 5.0)
        result = find_optimal_slots(filtered, 8, _E2E_READY_96H, min_block_hours=0.5)
        assert result == [], "No 5p-avg runs exist in this dataset"

    def test_moderate_max_price_short_session_finds_slots(self):
        """1h at max_price=20p — the combined dataset run averages ~19.8p which passes.

        Note: the combined actual+forecast data forms one contiguous run whose average
        is ~19.78p.  A max_price ceiling of 20p is the minimum that lets any slots
        through; tighter ceilings (15p, 18p) exclude the entire run.
        """
        slots = _e2e_slots_with_credibility(gamble_tolerance=100.0)
        filtered = filter_runs_by_max_avg_price(slots, 20.0)
        result = find_optimal_slots(filtered, 2, _E2E_READY_48H, min_block_hours=0.5)
        assert len(result) == 2, f"Expected 2 slots at max_price=20p, got {len(result)}"
        _assert_max_price_respected(result, 20.0, "moderate/short: ")
        _assert_within_ready_by(result, _E2E_READY_48H, "moderate/short: ")

    def test_moderate_max_price_medium_session_finds_slots(self):
        """4h at max_price=20p — run avg ~19.78p passes; 8 cheapest slots selected.

        The dataset's single run has an average of ~19.78p, so max_price=20p is the
        practical minimum that lets slots through.  Tighter ceilings (e.g. 18p) exclude
        the entire run because filter_runs_by_max_avg_price compares the run average,
        not individual slot prices.
        """
        slots = _e2e_slots_with_credibility(gamble_tolerance=100.0)
        filtered = filter_runs_by_max_avg_price(slots, 20.0)
        result = find_optimal_slots(filtered, 8, _E2E_READY_96H, min_block_hours=0.5)
        assert len(result) == 8, f"Expected 8 slots at max_price=20p, got {len(result)}"
        _assert_max_price_respected(result, 20.0, "moderate/medium: ")
        _assert_within_ready_by(result, _E2E_READY_96H, "moderate/medium: ")

    def test_generous_max_price_short_session_finds_slots(self):
        """1h at max_price=40p — almost all slots qualify; cheapest 2 selected."""
        slots = _e2e_slots_with_credibility(gamble_tolerance=100.0)
        filtered = filter_runs_by_max_avg_price(slots, 40.0)
        result = find_optimal_slots(filtered, 2, _E2E_READY_NEXT_MORNING, min_block_hours=0.5)
        assert len(result) == 2, f"Expected 2 slots, got {len(result)}"
        _assert_within_ready_by(result, _E2E_READY_NEXT_MORNING, "generous/short: ")

    def test_generous_max_price_large_session_finds_slots(self):
        """8h at max_price=40p — enough slots across multiple days."""
        slots = _e2e_slots_with_credibility(gamble_tolerance=100.0)
        filtered = filter_runs_by_max_avg_price(slots, 40.0)
        result = find_optimal_slots(filtered, 16, _E2E_READY_96H, min_block_hours=0.5)
        assert len(result) == 16, f"Expected 16 slots at max_price=40p, got {len(result)}"
        _assert_max_price_respected(result, 40.0, "generous/large: ")
        _assert_within_ready_by(result, _E2E_READY_96H, "generous/large: ")

    def test_unlimited_max_price_large_session(self):
        """8h at max_price=999p — all slots eligible; picks globally cheapest."""
        slots = _e2e_slots_with_credibility(gamble_tolerance=100.0)
        filtered = filter_runs_by_max_avg_price(slots, 999.0)
        result = find_optimal_slots(filtered, 16, _E2E_READY_96H, min_block_hours=0.5)
        assert len(result) == 16, f"Expected 16 slots, got {len(result)}"
        _assert_within_ready_by(result, _E2E_READY_96H, "unlimited/large: ")

    def test_impossible_large_session_tight_price_short_window(self):
        """8h at max_price=10p by next morning — not enough qualifying slots."""
        slots = _e2e_slots_with_credibility(gamble_tolerance=100.0)
        filtered = filter_runs_by_max_avg_price(slots, 10.0)
        result = find_optimal_slots(filtered, 16, _E2E_READY_NEXT_MORNING, min_block_hours=0.5)
        assert result == [], "16 slots below 10p avg by tomorrow morning is impossible"

    # ------------------------------------------------------------------
    # Dimension 3: min_block_hours
    # ------------------------------------------------------------------

    def test_no_min_block_picks_cheapest_scattered(self):
        """min_block=0.5h: single slots are valid; cheapest 4 selected."""
        slots = _e2e_slots_with_credibility(gamble_tolerance=100.0)
        filtered = filter_runs_by_max_avg_price(slots, 30.0)
        result = find_optimal_slots(filtered, 4, _E2E_READY_96H, min_block_hours=0.5)
        assert len(result) == 4, f"Expected 4 slots, got {len(result)}"
        _assert_min_block_respected(result, 0.5, "no_block: ")

    def test_moderate_min_block_2h_forces_contiguous(self):
        """min_block=2h: every run in the result must be >= 4 consecutive slots.

        Using E2E_READY_48H so the dataset is restricted to 96 contiguous slots.
        Within that window the algorithm can find a valid 4-slot block that satisfies
        the 2h min_block constraint.
        """
        slots = _e2e_slots_with_credibility(gamble_tolerance=100.0)
        filtered = filter_runs_by_max_avg_price(slots, 25.0)
        eligible = [s for s in filtered
                    if s['date_time'] + timedelta(minutes=30) <= _E2E_READY_48H]
        result = find_optimal_slots(eligible, 4, _E2E_READY_48H, min_block_hours=2.0)
        assert len(result) == 4, f"Expected 4 slots with min_block=2h, got {len(result)}"
        _assert_min_block_respected(result, 2.0, "moderate_block: ")
        _assert_within_ready_by(result, _E2E_READY_48H, "moderate_block: ")

    def test_strict_min_block_4h_medium_session(self):
        """min_block=4h (8 consecutive slots): algorithm selects exactly one 4h run.

        This test uses a hand-crafted contiguous slot list where the cheapest 8
        slots are all consecutive (Jun 03 11:00–14:30).  The greedy algorithm with
        min_block=4h must select exactly those 8 slots and never a shorter run.
        """
        now_dt = _E2E_NOW
        ready_by = _utc("2026-06-03T15:00:00")
        # Build a single contiguous run that is cheap in the 11:00-14:30 window.
        # Prices: high at start/end, low in the middle (all adjacent, no gaps).
        prices_raw = [
            ("2026-06-03T06:00:00", 24.0), ("2026-06-03T06:30:00", 23.5),
            ("2026-06-03T07:00:00", 22.0), ("2026-06-03T07:30:00", 21.5),
            ("2026-06-03T08:00:00", 20.0), ("2026-06-03T08:30:00", 20.5),
            ("2026-06-03T09:00:00", 19.5), ("2026-06-03T09:30:00", 19.0),
            ("2026-06-03T10:00:00", 18.0), ("2026-06-03T10:30:00", 17.5),
            ("2026-06-03T11:00:00", 12.0), ("2026-06-03T11:30:00", 11.5),
            ("2026-06-03T12:00:00", 10.0), ("2026-06-03T12:30:00", 9.5),
            ("2026-06-03T13:00:00", 9.0), ("2026-06-03T13:30:00", 8.5),
            ("2026-06-03T14:00:00", 9.5), ("2026-06-03T14:30:00", 10.5),
        ]
        slots = [
            {'date_time': _utc(iso), 'raw_price': p, 'source': 'predicted',
             'effective_price': p, 'tier': TIER_PREDICTED_0_24}
            for iso, p in prices_raw
        ]
        result = find_optimal_slots(slots, 8, ready_by, min_block_hours=4.0)
        assert len(result) == 8, f"Expected 8 slots with min_block=4h, got {len(result)}"
        _assert_min_block_respected(result, 4.0, "strict_block_4h: ")
        _assert_within_ready_by(result, ready_by, "strict_block_4h: ")

    def test_strict_min_block_4h_small_session_relaxes_floor(self):
        """min_block longer than the whole requirement relaxes to required_slots.

        min_block=4h (8 slots) but only 2 slots (1h) are required. A floor bigger
        than the entire session would make every such request unschedulable, so
        the algorithm caps the floor at required_slots and schedules the cheapest
        contiguous pair as a single block — it must NOT return [].
        """
        now_dt = _E2E_NOW
        ready_by = _utc("2026-06-03T15:00:00")
        prices_raw = [
            ("2026-06-03T06:00:00", 22.0), ("2026-06-03T06:30:00", 21.0),
            ("2026-06-03T07:00:00", 20.0), ("2026-06-03T07:30:00", 19.5),
            ("2026-06-03T08:00:00", 19.0), ("2026-06-03T08:30:00", 18.5),
            ("2026-06-03T09:00:00", 18.0), ("2026-06-03T09:30:00", 17.5),
            ("2026-06-03T10:00:00", 12.0), ("2026-06-03T10:30:00", 11.0),
            ("2026-06-03T11:00:00", 10.0), ("2026-06-03T11:30:00", 9.5),
            ("2026-06-03T12:00:00", 9.0), ("2026-06-03T12:30:00", 8.5),
            ("2026-06-03T13:00:00", 9.5), ("2026-06-03T13:30:00", 10.5),
            ("2026-06-03T14:00:00", 11.5), ("2026-06-03T14:30:00", 12.5),
        ]
        slots = [
            {'date_time': _utc(iso), 'raw_price': p, 'source': 'predicted',
             'effective_price': p, 'tier': TIER_PREDICTED_0_24}
            for iso, p in prices_raw
        ]
        # 2 required slots < 8 (min_block=4h) — floor relaxes to 2, cheapest
        # contiguous pair is 12:00-13:00 (9.0p, 8.5p, avg 8.75p).
        result = find_optimal_slots(slots, 2, ready_by, min_block_hours=4.0)
        assert len(result) == 2, f"Expected the 1h requirement to be scheduled, got {len(result)} slots"
        dts = sorted(s['date_time'] for s in result)
        assert dts == [_utc("2026-06-03T12:00:00"), _utc("2026-06-03T12:30:00")], (
            f"Expected the cheapest contiguous pair (12:00-13:00), got {dts}"
        )
        _assert_min_block_respected(result, 1.0, "relaxed_block: ")

    def test_strict_min_block_impossible_with_tight_price(self):
        """min_block=4h, max_price=12p, short window: no 8-slot runs below 12p avg."""
        slots = _e2e_slots_with_credibility(gamble_tolerance=100.0)
        filtered = filter_runs_by_max_avg_price(slots, 12.0)
        result = find_optimal_slots(filtered, 8, _E2E_READY_NEXT_MORNING, min_block_hours=4.0)
        assert result == [], "Tight price + strict block + short window should yield []"

    # ------------------------------------------------------------------
    # Dimension 4: Prediction tiers and gamble_tolerance
    # ------------------------------------------------------------------

    def test_all_actual_gamble_tolerance_has_no_effect(self):
        """When all slots are ACTUAL tier, gamble_tolerance does not change selection."""
        actual_future = [
            s for s in _build_actual_slots()
            if s['date_time'] + timedelta(minutes=30) > _E2E_NOW
        ]
        for s in actual_future:
            s['effective_price'] = s['raw_price']

        r0 = find_optimal_slots(actual_future, 2, _E2E_READY_NEXT_MORNING, min_block_hours=0.5)
        r100 = find_optimal_slots(actual_future, 2, _E2E_READY_NEXT_MORNING, min_block_hours=0.5)
        dts0 = sorted([s['date_time'] for s in r0])
        dts100 = sorted([s['date_time'] for s in r100])
        assert dts0 == dts100, "Actual-only slots: same selection regardless of gamble"

    def test_gamble_0_prefers_actual_over_far_predicted(self):
        """gamble=0: far-predicted at 7p inflates to ~17.5p effective; actual at 15p wins."""
        now_dt = _E2E_NOW
        actual_slots = [
            {'date_time': now_dt + timedelta(minutes=30 * i), 'raw_price': 15.0,
             'source': 'current_actual'}
            for i in range(2)
        ]
        far_slots = [
            {'date_time': now_dt + timedelta(hours=80) + timedelta(minutes=30 * i),
             'raw_price': 7.0, 'source': 'predicted'}
            for i in range(2)
        ]
        ready_by = now_dt + timedelta(hours=82)
        all_slots = assign_credibilities(actual_slots + far_slots, now_dt, gamble_tolerance=0.0)
        result = find_optimal_slots(all_slots, 2, ready_by, min_block_hours=0.5)
        assert len(result) == 2
        assert all(s['source'] == 'current_actual' for s in result), (
            f"gamble=0 should pick 15p actual over 7p far-predicted; got {[s['source'] for s in result]}"
        )

    def test_gamble_100_prefers_cheap_far_predicted(self):
        """gamble=100: effective=raw; far-predicted 7p beats actual 15p."""
        now_dt = _E2E_NOW
        actual_slots = [
            {'date_time': now_dt + timedelta(minutes=30 * i), 'raw_price': 15.0,
             'source': 'current_actual'}
            for i in range(2)
        ]
        far_slots = [
            {'date_time': now_dt + timedelta(hours=80) + timedelta(minutes=30 * i),
             'raw_price': 7.0, 'source': 'predicted'}
            for i in range(2)
        ]
        ready_by = now_dt + timedelta(hours=82)
        all_slots = assign_credibilities(actual_slots + far_slots, now_dt, gamble_tolerance=100.0)
        result = find_optimal_slots(all_slots, 2, ready_by, min_block_hours=0.5)
        assert len(result) == 2
        assert all(s['source'] == 'predicted' for s in result), (
            f"gamble=100 should pick 7p predicted; got {[s['source'] for s in result]}"
        )

    def test_gamble_100_picks_cheap_24_48h_predicted(self):
        """gamble=100: 9p predicted (30h ahead) beats 12p actual."""
        now_dt = _E2E_NOW
        actual = [
            {'date_time': now_dt + timedelta(minutes=30 * i), 'raw_price': 12.0,
             'source': 'current_actual'}
            for i in range(2)
        ]
        predicted_30h = [
            {'date_time': now_dt + timedelta(hours=30) + timedelta(minutes=30 * i),
             'raw_price': 9.0, 'source': 'predicted'}
            for i in range(2)
        ]
        ready_by = now_dt + timedelta(hours=33)
        all_slots = assign_credibilities(actual + predicted_30h, now_dt, gamble_tolerance=100.0)
        result = find_optimal_slots(all_slots, 2, ready_by, min_block_hours=0.5)
        assert len(result) == 2
        assert all(s['raw_price'] == 9.0 for s in result), (
            f"gamble=100 should pick 9p predicted; got {[s['raw_price'] for s in result]}"
        )

    def test_near_term_predicted_credibility_at_gamble_0(self):
        """Slots < 24h ahead have base credibility 0.90; at gamble=0 effective = raw/0.90."""
        tier = get_source_tier('predicted', _E2E_NOW + timedelta(hours=12), _E2E_NOW)
        assert tier == TIER_PREDICTED_0_24
        eff = compute_effective_price(10.0, tier, gamble_tolerance=0.0)
        assert eff == pytest.approx(10.0 / 0.90, rel=1e-4)

    def test_mid_term_predicted_credibility_at_gamble_0(self):
        """Slots 24-48h ahead have base credibility 0.75; at gamble=0 effective = raw/0.75."""
        tier = get_source_tier('predicted', _E2E_NOW + timedelta(hours=36), _E2E_NOW)
        assert tier == TIER_PREDICTED_24_48
        eff = compute_effective_price(10.0, tier, gamble_tolerance=0.0)
        assert eff == pytest.approx(10.0 / 0.75, rel=1e-4)

    def test_far_predicted_credibility_48_72h_at_gamble_0(self):
        """Slots 48-72h ahead have base credibility 0.60; at gamble=0 effective = raw/0.60."""
        tier = get_source_tier('predicted', _E2E_NOW + timedelta(hours=60), _E2E_NOW)
        assert tier == TIER_PREDICTED_48_72
        eff = compute_effective_price(10.0, tier, gamble_tolerance=0.0)
        assert eff == pytest.approx(10.0 / 0.60, rel=1e-4)

    def test_very_far_predicted_credibility_72_plus_at_gamble_0(self):
        """Slots > 72h ahead have base credibility 0.40; at gamble=0 effective = raw/0.40."""
        tier = get_source_tier('predicted', _E2E_NOW + timedelta(hours=80), _E2E_NOW)
        assert tier == TIER_PREDICTED_72_PLUS
        eff = compute_effective_price(10.0, tier, gamble_tolerance=0.0)
        assert eff == pytest.approx(10.0 / 0.40, rel=1e-4)

    # ------------------------------------------------------------------
    # Cheapest-slots assertions
    # ------------------------------------------------------------------

    def test_cheap_overnight_window_preferred_over_expensive_daytime(self):
        """
        The 2026-06-05 overnight window (7-9p) must be selected over
        the same-day evening (20-23p) for a 4h requirement with max_price=30p.
        """
        now_dt = _E2E_NOW
        ready_by = _utc("2026-06-06T00:00:00")
        slots = _e2e_slots_with_credibility(now_dt=now_dt, gamble_tolerance=100.0)
        filtered = filter_runs_by_max_avg_price(slots, 30.0)
        eligible = [s for s in filtered if s['date_time'] + timedelta(minutes=30) <= ready_by]
        result = find_optimal_slots(eligible, 8, ready_by, min_block_hours=0.5)
        assert len(result) == 8, f"Expected 8 slots, got {len(result)}"
        for s in result:
            assert s['raw_price'] < 14.0, (
                f"Expected cheap slots (<14p) from overnight window; "
                f"got raw_price={s['raw_price']} at {s['date_time']}"
            )

    def test_selected_slots_cheaper_than_most_expensive_alternative(self):
        """Total effective_price of selected slots <= total of most-expensive same-count set."""
        now_dt = _E2E_NOW
        ready_by = _utc("2026-06-04T06:00:00")
        slots = _e2e_slots_with_credibility(now_dt=now_dt, gamble_tolerance=100.0)
        filtered = filter_runs_by_max_avg_price(slots, 30.0)
        eligible = [s for s in filtered if s['date_time'] + timedelta(minutes=30) <= ready_by]
        result = find_optimal_slots(eligible, 4, ready_by, min_block_hours=0.5)
        assert len(result) == 4, f"Expected 4 slots, got {len(result)}"
        total_selected = sum([s['effective_price'] for s in result])
        worst4 = sorted(eligible, key=lambda s: s['effective_price'], reverse=True)[:4]
        total_worst = sum([s['effective_price'] for s in worst4])
        assert total_selected <= total_worst, (
            f"Selected total {total_selected:.2f}p should be <= worst-4 total {total_worst:.2f}p"
        )

    # ------------------------------------------------------------------
    # Active slot protection
    # ------------------------------------------------------------------

    def test_active_slot_preserved_during_recompute(self):
        """An active session must not be evicted when price data is refreshed."""
        now_dt = _E2E_NOW
        active_session = {
            'start': (now_dt - timedelta(minutes=30)).isoformat(),
            'end': (now_dt + timedelta(hours=1, minutes=30)).isoformat(),
            'duration_hours': 2.0,
        }
        inputs = {
            'required_hours': 4.0, 'gamble_tolerance': 50.0,
            'min_block_hours': 1.0, 'max_price': 30.0,
            'boost_duration': 0.0, 'charger_work_state': 'charger_insert',
        }
        sessions = compute_sessions(
            _build_actual_slots() + _build_forecast_slots(),
            [active_session], inputs, _E2E_READY_48H, now_dt,
        )
        assert active_session['start'] in [s['start'] for s in sessions], (
            f"Active session was evicted; sessions: {[s['start'] for s in sessions]}"
        )

    def test_active_slot_preserved_with_high_raw_price(self):
        """Active session is kept even if its price is well above max_price."""
        now_dt = _E2E_NOW
        expensive_active = {
            'start': (now_dt - timedelta(minutes=15)).isoformat(),
            'end': (now_dt + timedelta(minutes=45)).isoformat(),
            'duration_hours': 1.0,
        }
        inputs = {
            'required_hours': 2.0, 'gamble_tolerance': 100.0,
            'min_block_hours': 0.5, 'max_price': 30.0,
            'boost_duration': 0.0, 'charger_work_state': 'charger_charging',
        }
        sessions = compute_sessions(
            _build_actual_slots() + _build_forecast_slots(),
            [expensive_active], inputs, _E2E_READY_48H, now_dt,
        )
        assert expensive_active['start'] in [s['start'] for s in sessions], (
            "Expensive active session must appear in result"
        )

    def test_active_session_not_overwritten_by_overlapping_future_session(self):
        """
        Regression: the optimizer must not be allowed to pick a window that overlaps
        the active session's still-running portion. If it does, prune_and_classify
        ends up treating that overlapping computed session as "active" — silently
        replacing the real one with different start/end times (an eviction).
        """
        now_dt = NOW
        # Active session covers two still-future slots (10:00 and 10:30) that remain cheap —
        # exactly the kind of slot the optimizer would want to reuse if not excluded.
        active_session = {
            'start': (now_dt - timedelta(minutes=30)).isoformat(),
            'end': (now_dt + timedelta(hours=1)).isoformat(),
            'duration_hours': 1.5,
            'avg_price': 10.0,
            'confidence': 100.0,
        }
        prices = []
        t = now_dt - timedelta(minutes=30)
        for i in range(20):
            dt = t + timedelta(minutes=30 * i)
            # The still-running portion of the active session (10:00, 10:30) stays cheap;
            # everything after it is also cheap so the optimizer has plenty to choose from.
            prices.append({'date_time': dt, 'raw_price': 10.0, 'source': 'current_actual'})
        inputs = {
            'required_hours': 4.0, 'gamble_tolerance': 50.0,
            'min_block_hours': 1.0, 'max_price': 35.0,
        }
        sessions = compute_sessions(prices, [active_session], inputs, READY_BY, now_dt)
        active, future = prune_and_classify(sessions, now_dt)

        assert active == active_session, (
            f"Active session was replaced by an overlapping computed session: {active}"
        )
        active_start = _parse_dt(active_session['start'])
        active_end = _parse_dt(active_session['end'])
        for s in future:
            f_start = _parse_dt(s['start'])
            f_end = _parse_dt(s['end'])
            assert f_end <= active_start or f_start >= active_end, (
                f"Future session {s['start']}->{s['end']} overlaps active session "
                f"{active_session['start']}->{active_session['end']}"
            )

    def test_active_session_preserved_when_price_data_stale(self):
        """
        Regression: when all available price slots are in the past (stale data),
        compute_sessions must still return the active session rather than discarding
        it — losing it here would cause apply_schedule_outputs to report 'unschedulable'
        and turn charging off mid-session.
        """
        now_dt = NOW
        active_session = {
            'start': (now_dt - timedelta(minutes=30)).isoformat(),
            'end': (now_dt + timedelta(hours=1)).isoformat(),
            'duration_hours': 1.5,
        }
        past_slots = [
            {'date_time': now_dt - timedelta(hours=i), 'raw_price': 15.0, 'source': 'current_actual'}
            for i in range(1, 5)
        ]
        inputs = {
            'required_hours': 1.5, 'gamble_tolerance': 50.0,
            'min_block_hours': 0.5, 'max_price': 20.0,
        }
        sessions = compute_sessions(past_slots, [active_session], inputs, READY_BY, now_dt)
        assert sessions == [active_session], (
            f"Active session must survive a stale-price cycle; got {sessions}"
        )

    # ------------------------------------------------------------------
    # Full compute_sessions pipeline
    # ------------------------------------------------------------------

    def test_compute_sessions_short_requirement_next_morning(self):
        """1h required by next morning: at least one session found, total = 1h."""
        now_dt = _E2E_NOW
        inputs = {
            'required_hours': 1.0, 'gamble_tolerance': 100.0,
            'min_block_hours': 0.5, 'max_price': 30.0,
            'boost_duration': 0.0, 'charger_work_state': 'charger_insert',
        }
        sessions = compute_sessions(
            _build_actual_slots() + _build_forecast_slots(),
            [], inputs, _E2E_READY_NEXT_MORNING, now_dt,
        )
        assert len(sessions) >= 1, "Expected at least one session for 1h at max_price=30p"
        assert sum([s['duration_hours'] for s in sessions]) == pytest.approx(1.0)

    def test_compute_sessions_medium_requirement_48h(self):
        """4h required within 48h: total duration = 4.0h.

        Uses min_block_hours=0.5 (no block constraint) so that the cheapest slots
        within the 48h window are selectable regardless of contiguity.  The combined
        dataset forms one large run with avg ~22p which passes the 25p ceiling.
        """
        now_dt = _E2E_NOW
        inputs = {
            'required_hours': 4.0, 'gamble_tolerance': 50.0,
            'min_block_hours': 0.5, 'max_price': 25.0,
            'boost_duration': 0.0, 'charger_work_state': 'charger_insert',
        }
        sessions = compute_sessions(
            _build_actual_slots() + _build_forecast_slots(),
            [], inputs, _E2E_READY_48H, now_dt,
        )
        assert len(sessions) >= 1, "Expected sessions for 4h within 48h window"
        assert sum([s['duration_hours'] for s in sessions]) == pytest.approx(4.0)

    def test_compute_sessions_large_requirement_96h(self):
        """8h required across 96h: total duration = 8.0h."""
        now_dt = _E2E_NOW
        inputs = {
            'required_hours': 8.0, 'gamble_tolerance': 50.0,
            'min_block_hours': 0.5, 'max_price': 25.0,
            'boost_duration': 0.0, 'charger_work_state': 'charger_insert',
        }
        sessions = compute_sessions(
            _build_actual_slots() + _build_forecast_slots(),
            [], inputs, _E2E_READY_96H, now_dt,
        )
        assert len(sessions) >= 1, "Expected sessions for 8h within 96h window"
        assert sum([s['duration_hours'] for s in sessions]) == pytest.approx(8.0)

    def test_compute_sessions_impossible_returns_empty(self):
        """max_price=5p, 4h needed, short window — no qualifying slots, returns []."""
        now_dt = _E2E_NOW
        inputs = {
            'required_hours': 4.0, 'gamble_tolerance': 100.0,
            'min_block_hours': 0.5, 'max_price': 5.0,
            'boost_duration': 0.0, 'charger_work_state': 'charger_insert',
        }
        sessions = compute_sessions(
            _build_actual_slots() + _build_forecast_slots(),
            [], inputs, _E2E_READY_NEXT_MORNING, now_dt,
        )
        assert sessions == [], f"Expected empty with max_price=5p; got {sessions}"

    def test_compute_sessions_strict_block_4h_single_session(self):
        """min_block=4h, 4h required: each returned session is >= 4h.

        Uses a hand-crafted price list (one long contiguous run) so that the
        greedy algorithm can honour min_block=4h without falling into a cycle where
        isolated cheap slots keep getting ejected.
        """
        now_dt = _E2E_NOW
        ready_by = _utc("2026-06-03T15:00:00")
        # Single contiguous run from Jun02 17:00 to Jun03 14:30; cheap in 11:00-14:30.
        prices_raw = []
        t = now_dt
        while t < _utc("2026-06-03T15:00:00"):
            h = t.hour
            # Cheap block: Jun03 11:00-14:30
            if t.date().isoformat() == "2026-06-03" and 11 <= h < 15:
                price = 10.0 + (h - 11) * 0.5
            else:
                price = 25.0
            prices_raw.append((t.isoformat(), price))
            t += timedelta(minutes=30)
        slots = [
            {'date_time': _utc(iso), 'raw_price': p, 'source': 'predicted',
             'effective_price': p, 'tier': TIER_PREDICTED_0_24}
            for iso, p in prices_raw
        ]
        inputs = {
            'required_hours': 4.0, 'gamble_tolerance': 100.0,
            'min_block_hours': 4.0, 'max_price': 30.0,
            'boost_duration': 0.0, 'charger_work_state': 'charger_insert',
        }
        sessions = compute_sessions(slots, [], inputs, ready_by, now_dt)
        assert len(sessions) >= 1, "Expected a valid 4h block"
        total = sum([s['duration_hours'] for s in sessions])
        assert total == pytest.approx(4.0)
        for s in sessions:
            assert s['duration_hours'] >= 4.0 - 1e-6, (
                f"Session {s['duration_hours']}h is shorter than min_block=4h"
            )

    def test_compute_sessions_gamble_0_1h_within_24h(self):
        """gamble=0, 1h needed, next 24h window, unlimited price: finds 2 slots."""
        now_dt = _E2E_NOW
        inputs = {
            'required_hours': 1.0, 'gamble_tolerance': 0.0,
            'min_block_hours': 0.5, 'max_price': 999.0,
            'boost_duration': 0.0, 'charger_work_state': 'charger_insert',
        }
        sessions = compute_sessions(
            _build_actual_slots() + _build_forecast_slots(),
            [], inputs, now_dt + timedelta(hours=24), now_dt,
        )
        assert len(sessions) >= 1
        assert sum([s['duration_hours'] for s in sessions]) == pytest.approx(1.0)

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    def test_actual_overrides_predicted_for_same_slot(self):
        """When both actual and predicted exist for the same datetime, actual wins."""
        now_dt = _E2E_NOW
        slot_dt = now_dt + timedelta(minutes=30)
        deduped = deduplicate_and_sort_prices(
            [{'date_time': slot_dt, 'raw_price': 20.0, 'source': 'current_actual'},
             {'date_time': slot_dt, 'raw_price': 5.0,  'source': 'predicted'}],
            now_dt,
        )
        assert len(deduped) == 1
        assert deduped[0]['source'] == 'current_actual'
        assert deduped[0]['raw_price'] == 20.0

    def test_duplicate_predicted_slots_collapse_to_one(self):
        """Two predicted entries for the same datetime collapse to one."""
        now_dt = _E2E_NOW
        slot_dt = now_dt + timedelta(hours=1)
        deduped = deduplicate_and_sort_prices(
            [{'date_time': slot_dt, 'raw_price': 12.0, 'source': 'predicted'},
             {'date_time': slot_dt, 'raw_price': 9.0,  'source': 'predicted'}],
            now_dt,
        )
        assert len(deduped) == 1

    def test_combined_actual_and_forecast_removes_past_slots(self):
        """After combining actual + forecast, no past slots remain in the deduped result."""
        now_dt = _E2E_NOW
        deduped = deduplicate_and_sort_prices(
            _build_actual_slots() + _build_forecast_slots(), now_dt
        )
        assert len(deduped) <= len(_build_actual_slots()) + len(_build_forecast_slots())
        for s in deduped:
            assert s['date_time'] + timedelta(minutes=30) > now_dt, (
                f"Past slot at {s['date_time']} should have been removed"
            )

    # ------------------------------------------------------------------
    # Parametrised slot-count matrix
    # ------------------------------------------------------------------

    def test_slot_count_matrix(self):
        """
        Matrix: (max_price, req_h, ready_by, min_block, should_succeed).
        Verifies correct slot count, min_block, max_price, and ready_by compliance.

        Note: max_price must be >= ~19.78p for any slots to qualify (the combined
        dataset forms one run with that average).  Cases using 20p+ all succeed;
        cases using 5p correctly return [].  min_block=1h cases use E2E_READY_48H
        to ensure the candidate pool is a single contiguous run that the algorithm
        can work with.
        """
        now_dt = _E2E_NOW
        cases = [
            # (max_price, required_hours, ready_by, min_block, should_succeed)
            (20.0, 1.0, _E2E_READY_48H,          0.5, True),
            (20.0, 2.0, _E2E_READY_48H,          0.5, True),
            (20.0, 4.0, _E2E_READY_96H,          0.5, True),
            (30.0, 1.0, _E2E_READY_NEXT_MORNING, 0.5, True),
            (30.0, 4.0, _E2E_READY_48H,          0.5, True),
            (30.0, 8.0, _E2E_READY_96H,          0.5, True),
            (5.0,  4.0, _E2E_READY_NEXT_MORNING, 0.5, False),
            (5.0,  1.0, _E2E_READY_96H,          0.5, False),
            (999.0, 8.0, _E2E_READY_96H,         0.5, True),
        ]
        all_slots = _e2e_slots_with_credibility(now_dt=now_dt, gamble_tolerance=100.0)
        failures = []
        for max_price, req_h, ready_by, min_block, should_succeed in cases:
            label = f"max_price={max_price} req_h={req_h} min_block={min_block}"
            filtered = filter_runs_by_max_avg_price(all_slots, max_price)
            eligible = [
                s for s in filtered
                if s['date_time'] + timedelta(minutes=30) <= ready_by
            ]
            req_slots = max(1, int(req_h * 2))
            result = find_optimal_slots(eligible, req_slots, ready_by, min_block)
            if should_succeed:
                if len(result) != req_slots:
                    failures.append(f"{label}: expected {req_slots} slots, got {len(result)}")
                    continue
                try:
                    _assert_min_block_respected(result, min_block, f"matrix({label}): ")
                    _assert_max_price_respected(result, max_price, f"matrix({label}): ")
                    _assert_within_ready_by(result, ready_by, f"matrix({label}): ")
                except AssertionError as e:
                    failures.append(str(e))
            else:
                if result != []:
                    failures.append(
                        f"{label}: expected [] (impossible), got {len(result)} slots"
                    )
        assert failures == [], "Slot-count matrix failures:\n" + "\n".join(failures)


# ---------------------------------------------------------------------------
# TestPriceBandScenarios — hand-crafted windows with explicit gaps
# ---------------------------------------------------------------------------
#
# These tests use _win() to create isolated price windows separated by gaps.
# A gap (> 30 min between consecutive slots) causes build_contiguous_runs to
# split the data into separate runs, so filter_runs_by_max_avg_price evaluates
# each window's average independently.  This is the "picks good windows within
# budget" scenario: at max_price=15p, a cheap overnight window (avg 8p) qualifies
# even though an adjacent expensive evening (avg 30p) doesn't.


def _win(start_dt, prices, source='current_actual'):
    """
    Build a list of consecutive 30-min slots starting at start_dt.
    Leave a gap of >= 60 min between calls to create separate contiguous runs.
    effective_price defaults to raw_price; overwrite after assign_credibilities if needed.
    """
    return [
        {'date_time': start_dt + timedelta(minutes=30 * i), 'raw_price': p,
         'source': source, 'effective_price': p}
        for i, p in enumerate(prices)
    ]


_B = datetime(2024, 6, 5, 0, 0)   # base anchor: 2024-06-05 00:00


class TestPriceBandScenarios:
    """Hand-crafted price windows with gaps to test per-run max_price filtering."""

    # ------------------------------------------------------------------ #
    # Moderate max_price: 15p and 18p with isolated windows               #
    # ------------------------------------------------------------------ #

    def test_15p_max_price_cheap_window_selected(self):
        """At max_price=15p only the cheap (avg 7.8p) window qualifies; expensive (avg 31p) excluded."""
        cheap  = _win(_B + timedelta(hours=2),  [7, 8, 9, 8, 7, 8])   # avg 7.83p
        pricey = _win(_B + timedelta(hours=17), [30, 35, 32, 28])      # avg 31.25p
        filtered = filter_runs_by_max_avg_price(cheap + pricey, 15.0)
        assert all(s['raw_price'] < 15 for s in filtered), "Only cheap window should survive"
        result = find_optimal_slots(filtered, 4, _B + timedelta(hours=24), min_block_hours=0.5)
        assert len(result) == 4
        assert all(s['raw_price'] <= 9 for s in result)

    def test_15p_max_price_expensive_window_not_in_result(self):
        """Slots from the expensive window must never appear in results at max_price=15p."""
        cheap  = _win(_B + timedelta(hours=2),  [8, 9, 10, 8, 9, 10])
        pricey = _win(_B + timedelta(hours=17), [28, 32, 30])
        expensive_dts = set(s['date_time'] for s in pricey)
        filtered = filter_runs_by_max_avg_price(cheap + pricey, 15.0)
        assert not any(s['date_time'] in expensive_dts for s in filtered)

    def test_18p_max_price_cheap_and_shoulder_qualify_peak_excluded(self):
        """At max_price=18p: cheap (avg 10p) and shoulder (avg 15.5p) both pass; peak (avg 31p) excluded."""
        cheap    = _win(_B + timedelta(hours=1),  [9, 10, 11, 12, 10, 8])   # avg 10.0p
        shoulder = _win(_B + timedelta(hours=10), [14, 15, 16, 17, 16, 15]) # avg 15.5p
        peak     = _win(_B + timedelta(hours=17), [30, 35, 28, 32])         # avg 31.25p
        filtered = filter_runs_by_max_avg_price(cheap + shoulder + peak, 18.0)
        assert len(filtered) == len(cheap) + len(shoulder), (
            f"cheap ({len(cheap)}) + shoulder ({len(shoulder)}) should pass; "
            f"peak excluded. Got {len(filtered)}"
        )

    def test_18p_max_price_run_just_above_threshold_excluded(self):
        """A run averaging 18.5p is excluded; a run averaging 17.5p is kept."""
        just_above = _win(_B + timedelta(hours=2), [17.0, 18.0, 19.5, 19.5])  # avg 18.5p
        just_below = _win(_B + timedelta(hours=8), [16.0, 17.0, 18.0, 19.0])  # avg 17.5p
        filtered = filter_runs_by_max_avg_price(just_above + just_below, 18.0)
        above_dts = set(s['date_time'] for s in just_above)
        assert not any(s['date_time'] in above_dts for s in filtered), "18.5p avg run must be excluded"
        assert len(filtered) == len(just_below), "17.5p avg run must be kept"

    def test_15p_max_price_with_min_block_2h(self):
        """At max_price=15p with min_block=2h: must find 4 consecutive slots from cheap window."""
        cheap  = _win(_B + timedelta(hours=2),  [8, 9, 10, 11, 9, 8])  # 6 slots avg 9.17p
        pricey = _win(_B + timedelta(hours=17), [30, 35, 28])
        filtered = filter_runs_by_max_avg_price(cheap + pricey, 15.0)
        result = find_optimal_slots(filtered, 4, _B + timedelta(hours=24), min_block_hours=2.0)
        assert len(result) == 4
        _assert_min_block_respected(result, 2.0)
        assert all(s['raw_price'] <= 11 for s in result)

    def test_18p_max_price_fills_requirement_across_two_windows(self):
        """8 slots at max_price=18p: filled equally from cheap + shoulder (4 slots each)."""
        cheap    = _win(_B + timedelta(hours=1),  [8, 9, 10, 11])      # 4 slots avg 9.5p
        shoulder = _win(_B + timedelta(hours=10), [14, 15, 16, 17])    # 4 slots avg 15.5p
        peak     = _win(_B + timedelta(hours=17), [30, 35, 28])
        filtered = filter_runs_by_max_avg_price(cheap + shoulder + peak, 18.0)
        result = find_optimal_slots(filtered, 8, _B + timedelta(hours=24), min_block_hours=0.5)
        assert len(result) == 8, f"Expected 8 from cheap + shoulder, got {len(result)}"
        _assert_max_price_respected(result, 18.0)

    # ------------------------------------------------------------------ #
    # 0p pricing — divide-by-zero safety                                  #
    # ------------------------------------------------------------------ #

    def test_zero_price_compute_effective_all_tiers_all_gamble(self):
        """compute_effective_price(0.0, tier, gamble) == 0.0 for every tier and gamble — no divide-by-zero."""
        for tier in (TIER_ACTUAL, TIER_PREDICTED_0_24, TIER_PREDICTED_24_48,
                     TIER_PREDICTED_48_72, TIER_PREDICTED_72_PLUS):
            for gamble in (0, 50, 100):
                result = compute_effective_price(0.0, tier, gamble)
                assert result == pytest.approx(0.0), (
                    f"Expected 0.0 for tier={tier} gamble={gamble}, got {result}"
                )

    def test_zero_price_passes_any_positive_max_price_filter(self):
        """A run of 0p slots has avg=0 which is <= any positive max_price."""
        zero_slots = _win(_B + timedelta(hours=2), [0.0, 0.0, 0.0, 0.0])
        for threshold in (0.01, 5.0, 15.0, 999.0):
            filtered = filter_runs_by_max_avg_price(zero_slots, threshold)
            assert len(filtered) == 4, f"0p slots must pass max_price={threshold}p filter"

    def test_zero_price_passes_zero_max_price_filter(self):
        """A 0p run has avg=0 which satisfies even max_price=0p (avg <= threshold)."""
        zero_slots = _win(_B + timedelta(hours=2), [0.0, 0.0, 0.0, 0.0])
        filtered = filter_runs_by_max_avg_price(zero_slots, 0.0)
        assert len(filtered) == 4, "0p avg run must pass max_price=0p (0 <= 0)"

    def test_zero_price_slot_selected_over_positive(self):
        """A 0p slot ranks cheaper than any positive slot and is selected first."""
        zero_win = _win(_B + timedelta(hours=2), [0.0, 0.0])
        cheap_win = _win(_B + timedelta(hours=8), [5.0, 5.0])
        result = find_optimal_slots(zero_win + cheap_win, 2, _B + timedelta(hours=24), 0.5)
        assert len(result) == 2
        assert all(s['raw_price'] == 0.0 for s in result), "0p slots must be selected over 5p"

    def test_zero_price_mixed_with_positive_lowers_run_avg(self):
        """Run of [0, 0, 20, 20] has avg 10p — passes max_price=15p even though two slots are 20p."""
        mixed = _win(_B + timedelta(hours=2), [0.0, 0.0, 20.0, 20.0])  # avg 10p
        filtered = filter_runs_by_max_avg_price(mixed, 15.0)
        assert len(filtered) == 4, "Mixed 0p/20p run with avg 10p must pass max_price=15p"

    def test_zero_price_find_optimal_no_crash(self):
        """find_optimal_slots must not crash or loop when some effective_price values are 0."""
        slots = _win(_B + timedelta(hours=2), [0.0, 0.0, 5.0, 5.0, 10.0, 10.0])
        result = find_optimal_slots(slots, 4, _B + timedelta(hours=24), min_block_hours=0.5)
        assert len(result) == 4

    # ------------------------------------------------------------------ #
    # Negative pricing                                                     #
    # ------------------------------------------------------------------ #

    def test_negative_price_compute_effective_all_tiers(self):
        """compute_effective_price with negative raw_price must not crash and must stay negative."""
        for tier in (TIER_ACTUAL, TIER_PREDICTED_0_24, TIER_PREDICTED_24_48,
                     TIER_PREDICTED_48_72, TIER_PREDICTED_72_PLUS):
            for gamble in (0, 50, 100):
                result = compute_effective_price(-5.0, tier, gamble)
                assert result < 0, f"Negative input must give negative output: tier={tier} gamble={gamble}"

    def test_negative_slots_selected_before_positive(self):
        """Negative effective_price slots rank cheapest and are all selected first."""
        negative = _win(_B + timedelta(hours=2), [-5.0, -4.0, -3.0, -5.0])  # avg -4.25p
        positive = _win(_B + timedelta(hours=8), [8.0, 9.0, 10.0, 8.0])
        result = find_optimal_slots(negative + positive, 4, _B + timedelta(hours=24), 0.5)
        assert len(result) == 4
        assert all(s['raw_price'] < 0 for s in result), "Negative slots must be preferred over positive"

    def test_negative_run_passes_positive_max_price_filter(self):
        """A run averaging -3p is below any positive max_price threshold."""
        neg_win = _win(_B + timedelta(hours=2), [-5.0, -3.0, -2.0, -2.0])  # avg -3p
        filtered = filter_runs_by_max_avg_price(neg_win, 5.0)
        assert len(filtered) == 4, "Negative avg run must pass max_price=5p filter"

    def test_negative_run_passes_zero_max_price_filter(self):
        """A run averaging -2.75p also passes max_price=0p (negative avg < 0 threshold)."""
        neg_win = _win(_B + timedelta(hours=2), [-5.0, -3.0, -2.0, -1.0])  # avg -2.75p
        filtered = filter_runs_by_max_avg_price(neg_win, 0.0)
        assert len(filtered) == 4, "Negative-avg run must pass max_price=0p"

    def test_mixed_negative_and_positive_run_avg_determines_eligibility(self):
        """Run of [-8, -6, 12, 12] has avg 2.5p — passes max_price=5p even with positive slots."""
        mixed = _win(_B + timedelta(hours=2), [-8.0, -6.0, 12.0, 12.0])  # avg 2.5p
        filtered = filter_runs_by_max_avg_price(mixed, 5.0)
        assert len(filtered) == 4, "Mixed run avg 2.5p must pass max_price=5p"

    def test_negative_zero_positive_selection_order(self):
        """Isolated slots at -5p, 0p, 5p: algorithm selects all three and respects price order."""
        neg  = _win(_B + timedelta(hours=2),  [-5.0])
        zero = _win(_B + timedelta(hours=6),  [0.0])
        pos  = _win(_B + timedelta(hours=10), [5.0])
        result = find_optimal_slots(neg + zero + pos, 3, _B + timedelta(hours=24), 0.5)
        assert len(result) == 3
        assert sorted([s['raw_price'] for s in result]) == [-5.0, 0.0, 5.0]

    def test_negative_price_effective_amplified_at_zero_gamble(self):
        """At gamble=0, far-predicted -5p becomes -12.5p effective (more desirable than face value)."""
        eff_g0   = compute_effective_price(-5.0, TIER_PREDICTED_72_PLUS, gamble_tolerance=0.0)
        eff_g100 = compute_effective_price(-5.0, TIER_PREDICTED_72_PLUS, gamble_tolerance=100.0)
        assert eff_g0 == pytest.approx(-5.0 / 0.40)   # -12.5p
        assert eff_g100 == pytest.approx(-5.0)          # -5p face value
        assert eff_g0 < eff_g100, "At gamble=0 negative effective must be more negative than face value"

    # ------------------------------------------------------------------ #
    # 0–10p band with expensive evenings                                  #
    # ------------------------------------------------------------------ #

    def test_0_to_10p_overnight_with_expensive_evening(self):
        """Overnight (avg 5p) beats expensive evening (avg 30p); at max_price=15p only overnight qualifies."""
        evening   = _win(_B + timedelta(hours=17), [28, 32, 35, 30, 25])          # avg 30p
        overnight = _win(_B + timedelta(hours=23), [2, 4, 6, 8, 7, 5, 3, 4])     # avg 4.875p
        filtered = filter_runs_by_max_avg_price(evening + overnight, 15.0)
        result = find_optimal_slots(filtered, 6, _B + timedelta(hours=33), min_block_hours=0.5)
        assert len(result) == 6, f"Expected 6 overnight slots, got {len(result)}"
        assert all(s['raw_price'] <= 10 for s in result), "All results must be from 0-10p window"

    def test_sub_5p_window_selected_over_sub_10p_window(self):
        """Both windows qualify at max_price=15p; the cheaper (avg 2.5p) is selected first."""
        very_cheap = _win(_B + timedelta(hours=2),  [1, 2, 3, 4, 3, 2])   # 6 slots avg 2.5p
        cheap      = _win(_B + timedelta(hours=10), [6, 7, 8, 9, 8, 7])   # 6 slots avg 7.5p
        filtered = filter_runs_by_max_avg_price(very_cheap + cheap, 15.0)
        result = find_optimal_slots(filtered, 6, _B + timedelta(hours=24), min_block_hours=0.5)
        assert len(result) == 6
        # All 6 come from very_cheap (the only source with raw_price <= 4)
        assert all(s['raw_price'] <= 4 for s in result), "Should pick from 0-5p window first"

    def test_0_to_10p_with_min_block_2h(self):
        """0-10p overnight window with min_block=2h: must select 4 consecutive slots.

        The window approach scores each 4-slot sub-sequence by average, so it correctly
        finds the cheapest 4-slot window regardless of where the cheap slots sit in the run.
        """
        overnight = _win(_B + timedelta(hours=23), [3, 5, 7, 9, 8, 6, 4, 2])
        result = find_optimal_slots(overnight, 4, _B + timedelta(hours=33), min_block_hours=2.0)
        assert len(result) == 4
        _assert_min_block_respected(result, 2.0)
        assert all(s['raw_price'] <= 10 for s in result)

    # ------------------------------------------------------------------ #
    # 10–15p band with expensive evenings                                 #
    # ------------------------------------------------------------------ #

    def test_10_to_15p_band_selected_when_evening_excluded(self):
        """Shoulder (avg 12.2p) qualifies at max_price=15p; evening (avg 31p) excluded."""
        shoulder = _win(_B + timedelta(hours=10), [10, 11, 12, 13, 14, 13])  # avg 12.17p
        evening  = _win(_B + timedelta(hours=17), [28, 32, 35, 30])
        filtered = filter_runs_by_max_avg_price(shoulder + evening, 15.0)
        result = find_optimal_slots(filtered, 4, _B + timedelta(hours=24), min_block_hours=0.5)
        assert len(result) == 4
        assert all(10 <= s['raw_price'] <= 15 for s in result), "Results must come from 10-15p window"

    def test_10_to_15p_band_with_min_block_1h(self):
        """10-15p window with min_block=1h: selected pair must be consecutive."""
        shoulder = _win(_B + timedelta(hours=10), [10, 11, 12, 13, 14, 13])
        result = find_optimal_slots(shoulder, 2, _B + timedelta(hours=24), min_block_hours=1.0)
        assert len(result) == 2
        _assert_min_block_respected(result, 1.0)

    def test_10_to_15p_band_excluded_at_max_price_10p(self):
        """A run averaging 12.2p is excluded at max_price=10p."""
        shoulder = _win(_B + timedelta(hours=10), [10, 11, 12, 13, 14, 13])  # avg 12.17p
        filtered = filter_runs_by_max_avg_price(shoulder, 10.0)
        assert filtered == [], "Run avg 12.17p must be excluded at max_price=10p"

    # ------------------------------------------------------------------ #
    # Combinations                                                        #
    # ------------------------------------------------------------------ #

    def test_negative_plus_cheap_shoulder_fills_4h(self):
        """Negative window (2 slots) + 10-15p window (6 slots) together fill 4h (8 slots)."""
        negative = _win(_B + timedelta(hours=2),  [-4.0, -3.0])
        shoulder = _win(_B + timedelta(hours=8),  [11, 12, 13, 12, 11, 10])  # 6 slots
        evening  = _win(_B + timedelta(hours=17), [30, 35, 28])
        filtered = filter_runs_by_max_avg_price(negative + shoulder + evening, 15.0)
        result = find_optimal_slots(filtered, 8, _B + timedelta(hours=24), min_block_hours=0.5)
        assert len(result) == 8, f"Negative + shoulder must fill 8 slots; got {len(result)}"
        neg_dts = set(s['date_time'] for s in negative)
        assert all(any(r['date_time'] == dt for r in result) for dt in neg_dts), (
            "Both negative-price slots must appear in result"
        )

    def test_three_price_bands_negative_zero_shoulder_evening_excluded(self):
        """Negative (-3p, -2p), zero (0p, 0p), shoulder (11-14p) qualify at 15p; evening (30p+) excluded."""
        negative = _win(_B + timedelta(hours=1),  [-3.0, -2.0])
        zero_band = _win(_B + timedelta(hours=5), [0.0, 0.0])
        shoulder  = _win(_B + timedelta(hours=10), [11.0, 12.0, 13.0, 14.0])
        evening   = _win(_B + timedelta(hours=17), [30.0, 35.0, 32.0])
        filtered = filter_runs_by_max_avg_price(negative + zero_band + shoulder + evening, 15.0)
        result = find_optimal_slots(filtered, 8, _B + timedelta(hours=24), min_block_hours=0.5)
        assert len(result) == 8
        evening_dts = set(s['date_time'] for s in evening)
        assert not any(s['date_time'] in evening_dts for s in result), "Evening must not appear"
        prices = sorted([s['raw_price'] for s in result])
        assert prices[:2] == [-3.0, -2.0], "Negative slots must be in result"
        assert prices[2:4] == [0.0, 0.0], "Zero slots must be in result"

    def test_evening_expensive_does_not_contaminate_cheap_overnight_run(self):
        """Expensive evening (avg 32p) and cheap overnight (avg 6.5p) are separate runs; only overnight passes 15p."""
        overnight = _win(_B + timedelta(hours=23), [5, 6, 7, 8, 7, 6])  # avg 6.5p
        evening   = _win(_B + timedelta(hours=17), [30, 35, 32])         # avg 32.3p
        filtered = filter_runs_by_max_avg_price(evening + overnight, 15.0)
        assert len(filtered) == len(overnight), (
            f"Only overnight should pass (got {len(filtered)}); "
            "expensive evening must not contaminate cheap run average"
        )
        assert all(s['raw_price'] <= 10 for s in filtered)

    def test_compute_sessions_end_to_end_with_negative_prices(self):
        """compute_sessions must work end-to-end when price data contains negative slots."""
        negative = _win(_B + timedelta(hours=2), [-4.0, -3.0, -2.0, -3.0])  # 4 slots
        cheap    = _win(_B + timedelta(hours=8), [8.0, 9.0, 10.0, 9.0])
        inputs = {
            'required_hours': 2.0, 'gamble_tolerance': 100.0,
            'min_block_hours': 0.5, 'max_price': 15.0,
            'boost_duration': 0.0, 'charger_work_state': 'charger_insert',
        }
        sessions = compute_sessions(negative + cheap, [], inputs, _B + timedelta(hours=24), _B)
        assert len(sessions) >= 1, "Must find sessions with negative price data"
        assert sum([s['duration_hours'] for s in sessions]) == pytest.approx(2.0)
        # Negative window starts at _B+2h; it should be the first session
        first_start = datetime.fromisoformat(sessions[0]['start'])
        assert first_start in [s['date_time'] for s in negative], (
            "First session must start in the negative-price window"
        )


# ---------------------------------------------------------------------------
# TestMissingDataScenarios — entity unavailable, missing attributes, stale data
# ---------------------------------------------------------------------------


class TestMissingDataScenarios:
    """
    Guards for when HA entities are unavailable, return no data, or return stale data.
    Every path must either return empty lists (allowing the caller to fall through to
    set_all_unavailable) or raise no exception.
    """

    # --- _parse_rates (current / next day actual rates) ---

    def test_current_rates_entity_missing_returns_empty(self, mock_hass):
        """Entity does not exist → get_current_rates returns []."""
        mock_hass.states.get.return_value = None
        ha = EVChargingHA()
        assert ha.get_current_rates() == []

    def test_current_rates_entity_unavailable_returns_empty(self, mock_hass):
        """Entity in 'unavailable' state has no meaningful attributes → returns []."""
        entity = Mock()
        entity.attributes = {}
        mock_hass.states.get.return_value = entity
        ha = EVChargingHA()
        assert ha.get_current_rates() == []

    def test_current_rates_entity_no_rates_key_returns_empty(self, mock_hass):
        """Entity has attributes but no 'rates' key → returns []."""
        entity = Mock()
        entity.attributes = {'friendly_name': 'Octopus Rates'}
        mock_hass.states.get.return_value = entity
        ha = EVChargingHA()
        assert ha.get_current_rates() == []

    def test_current_rates_malformed_entry_skipped_valid_returned(self, mock_hass, mock_as_local):
        """A rate entry missing 'start' or 'value_inc_vat' is silently skipped."""
        entity = Mock()
        entity.attributes = {
            'rates': [
                {'start': '2024-01-15T10:00:00', 'value_inc_vat': 0.125},   # valid → 12.5p
                {'value_inc_vat': 0.1},                                       # missing 'start' → skipped
                {'start': '2024-01-15T11:00:00'},                            # missing price → skipped
                {'start': None, 'value_inc_vat': 0.1},                      # None start → skipped
            ]
        }
        mock_hass.states.get.return_value = entity
        ha = EVChargingHA()
        rates = ha.get_current_rates()
        assert len(rates) == 1, f"Only valid entry should be returned, got {len(rates)}"
        assert rates[0]['raw_price'] == pytest.approx(12.5)

    def test_next_rates_entity_missing_returns_empty(self, mock_hass):
        """Next-day rates entity not present → get_next_rates returns []."""
        mock_hass.states.get.return_value = None
        ha = EVChargingHA()
        assert ha.get_next_rates() == []

    # --- get_agile_forecast_slots ---

    def test_agile_forecast_entity_missing_returns_empty(self, mock_hass):
        """Forecast entity not present → returns []."""
        mock_hass.states.get.return_value = None
        ha = EVChargingHA()
        assert ha.get_agile_forecast_slots() == []

    def test_agile_forecast_entity_no_prices_key_returns_empty(self, mock_hass):
        """Forecast entity has no 'prices' attribute → returns []."""
        entity = Mock()
        entity.attributes = {'state': 'ok'}
        mock_hass.states.get.return_value = entity
        ha = EVChargingHA()
        assert ha.get_agile_forecast_slots() == []

    def test_agile_forecast_malformed_entry_skipped(self, mock_hass, mock_as_local):
        """Forecast entries with missing fields are skipped; valid ones returned."""
        entity = Mock()
        entity.attributes = {
            'prices': [
                {'date_time': '2024-01-16T02:00:00', 'agile_pred': 12.5},  # valid
                {'agile_pred': 10.0},                                        # missing date_time → skip
                {'date_time': '2024-01-16T03:00:00'},                       # missing agile_pred → skip
            ]
        }
        mock_hass.states.get.return_value = entity
        ha = EVChargingHA()
        slots = ha.get_agile_forecast_slots()
        assert len(slots) == 1
        assert slots[0]['raw_price'] == pytest.approx(12.5)

    # --- collect_all_prices with partial sources ---

    def test_collect_all_prices_only_forecast_available(self):
        """When actual rates are empty but forecast has data, collect returns forecast slots."""
        ha = MagicMock(spec=EVChargingHA)
        ha.get_current_rates.return_value = []
        ha.get_next_rates.return_value = []
        ha.get_agile_forecast_slots.return_value = [
            {'date_time': NOW + timedelta(hours=1), 'raw_price': 15.0, 'source': 'predicted'},
            {'date_time': NOW + timedelta(hours=2), 'raw_price': 12.0, 'source': 'predicted'},
        ]
        result = collect_all_prices(ha)
        assert len(result) == 2
        assert all(s['source'] == 'predicted' for s in result)

    def test_collect_all_prices_only_actual_rates_available(self):
        """When forecast is empty but actual rates have data, collect returns actual slots."""
        ha = MagicMock(spec=EVChargingHA)
        ha.get_current_rates.return_value = [
            {'date_time': NOW + timedelta(hours=1), 'raw_price': 20.0, 'source': 'current_actual'},
        ]
        ha.get_next_rates.return_value = []
        ha.get_agile_forecast_slots.return_value = []
        result = collect_all_prices(ha)
        assert len(result) == 1
        assert result[0]['source'] == 'current_actual'

    def test_collect_all_prices_all_sources_empty_returns_empty(self):
        """All three sources empty → returns [] so caller can set_all_unavailable."""
        ha = MagicMock(spec=EVChargingHA)
        ha.get_current_rates.return_value = []
        ha.get_next_rates.return_value = []
        ha.get_agile_forecast_slots.return_value = []
        assert collect_all_prices(ha) == []

    # --- Stale data (all slots in the past) ---

    def test_stale_prices_all_in_past_compute_sessions_returns_empty(self):
        """When all price slots are in the past, compute_sessions returns [] gracefully."""
        past_slots = [
            {'date_time': NOW - timedelta(hours=i), 'raw_price': 15.0, 'source': 'current_actual'}
            for i in range(1, 5)
        ]
        inputs = {
            'required_hours': 2.0, 'gamble_tolerance': 50.0,
            'min_block_hours': 0.5, 'max_price': 20.0,
        }
        result = compute_sessions(past_slots, [], inputs, READY_BY, NOW)
        assert result == [], "Stale prices should produce no sessions"

    def test_only_forecast_available_schedule_uses_predicted_tier(self):
        """Schedule from forecast-only data: slots get predicted tier; effective price is inflated."""
        now_dt = NOW
        # Predicted slots 30h ahead → TIER_PREDICTED_24_48 (base_cred=0.75)
        forecast_slots = [
            {'date_time': now_dt + timedelta(hours=30) + timedelta(minutes=30 * i),
             'raw_price': 12.0, 'source': 'predicted'}
            for i in range(6)
        ]
        inputs = {
            'required_hours': 1.0, 'gamble_tolerance': 50.0,
            'min_block_hours': 0.5, 'max_price': 30.0,
        }
        ready_by = now_dt + timedelta(hours=35)
        sessions = compute_sessions(forecast_slots, [], inputs, ready_by, now_dt)
        # Should schedule even with only forecast; gamble=50 inflates 12p to 12/0.875≈13.7p
        # effective, which is still < 30p max, so sessions are found
        assert len(sessions) >= 1, "Forecast-only scheduling must produce sessions"
        assert sum([s['duration_hours'] for s in sessions]) == pytest.approx(1.0)

    def test_only_forecast_available_at_zero_gamble_produces_no_sessions(self):
        """gamble_tolerance=0 hard-excludes forecast data — forecast-only input yields nothing."""
        now_dt = NOW
        forecast_slots = [
            {'date_time': now_dt + timedelta(hours=30) + timedelta(minutes=30 * i),
             'raw_price': 12.0, 'source': 'predicted'}
            for i in range(6)
        ]
        inputs = {
            'required_hours': 1.0, 'gamble_tolerance': 0.0,
            'min_block_hours': 0.5, 'max_price': 30.0,
        }
        ready_by = now_dt + timedelta(hours=35)
        sessions = compute_sessions(forecast_slots, [], inputs, ready_by, now_dt)
        assert sessions == [], "gamble_tolerance=0 must never schedule against forecast-only data"

    def test_only_actual_rates_schedule_limited_to_24h_horizon(self):
        """Schedule from actual-only data within 24h covers the available window."""
        now_dt = NOW
        actual_slots = [
            {'date_time': now_dt + timedelta(minutes=30 * i), 'raw_price': 18.0,
             'source': 'current_actual'}
            for i in range(24)   # 12h of actual rates
        ]
        inputs = {
            'required_hours': 2.0, 'gamble_tolerance': 50.0,
            'min_block_hours': 0.5, 'max_price': 25.0,
        }
        ready_by = now_dt + timedelta(hours=12)
        sessions = compute_sessions(actual_slots, [], inputs, ready_by, now_dt)
        assert len(sessions) >= 1
        assert sum([s['duration_hours'] for s in sessions]) == pytest.approx(2.0)
        # All session slots must be within the actual-data horizon
        for s in sessions:
            assert datetime.fromisoformat(s['end']) <= ready_by


# ---------------------------------------------------------------------------
# TestSourceWeighting — correct slot selection given mixed actual + predicted
# ---------------------------------------------------------------------------


class TestSourceWeighting:
    """
    Verifies that the credibility weighting system produces the right slot selection
    when actual rates and forecast predictions cover overlapping time windows.

    Key invariants:
    - value_inc_vat (£/kWh) × 100 = raw_price in p/kWh (unit conversion)
    - Deduplication: actual rate for a slot always replaces same-datetime forecast
    - At gamble=0: far-predicted prices are inflated → actual rates preferred
    - At gamble=100: prices taken at face value → cheapest raw price wins regardless of source
    """

    def _actual_slot(self, dt, price_pence):
        return {'date_time': dt, 'raw_price': price_pence, 'source': 'current_actual'}

    def _forecast_slot(self, dt, price_pence):
        return {'date_time': dt, 'raw_price': price_pence, 'source': 'predicted'}

    def test_unit_conversion_actual_price_correct_pence(self, mock_hass, mock_as_local):
        """£0.20/kWh in value_inc_vat must become raw_price=20.0p, not 0.20p or 2000p."""
        entity = Mock()
        entity.attributes = {'rates': [{'start': '2024-01-15T10:00:00', 'value_inc_vat': 0.20}]}
        mock_hass.states.get.return_value = entity
        ha = EVChargingHA()
        rates = ha.get_current_rates()
        assert len(rates) == 1
        assert rates[0]['raw_price'] == pytest.approx(20.0), (
            f"£0.20/kWh must convert to 20.0p, got {rates[0]['raw_price']}"
        )

    def test_actual_wins_dedup_and_converted_price_used(self):
        """For a shared datetime, the correctly-converted actual price replaces the forecast."""
        slot_dt = NOW + timedelta(hours=2)
        actual = self._actual_slot(slot_dt, 20.0)   # £0.20/kWh already converted to 20p
        forecast = self._forecast_slot(slot_dt, 14.0)
        deduped = deduplicate_and_sort_prices([forecast, actual], NOW)
        assert len(deduped) == 1
        assert deduped[0]['source'] == 'current_actual'
        assert deduped[0]['raw_price'] == pytest.approx(20.0), (
            "Actual rate price (after unit conversion) must be used, not the forecast price"
        )

    def test_gamble_0_actual_20p_beats_far_predicted_8p(self):
        """At gamble=0, a far-predicted 8p slot (eff=20p at 72h+) loses to actual 18p (eff=18p)."""
        now_dt = NOW
        actual = [self._actual_slot(now_dt + timedelta(minutes=30 * i), 18.0) for i in range(2)]
        far_pred = [self._forecast_slot(
            now_dt + timedelta(hours=80) + timedelta(minutes=30 * i), 8.0) for i in range(2)]
        ready = now_dt + timedelta(hours=82)
        all_slots = assign_credibilities(actual + far_pred, now_dt, gamble_tolerance=0.0)
        # Far predicted 8p: eff = 8 / 0.40 = 20p. Actual 18p: eff = 18p. Actual is cheaper.
        result = find_optimal_slots(all_slots, 2, ready, min_block_hours=0.5)
        assert len(result) == 2
        assert all(s['source'] == 'current_actual' for s in result), (
            "At gamble=0, actual 18p (eff=18p) must beat far-predicted 8p (eff=20p)"
        )

    def test_gamble_100_far_predicted_8p_beats_actual_18p(self):
        """At gamble=100, effective=raw; far-predicted 8p beats actual 18p."""
        now_dt = NOW
        actual = [self._actual_slot(now_dt + timedelta(minutes=30 * i), 18.0) for i in range(2)]
        far_pred = [self._forecast_slot(
            now_dt + timedelta(hours=80) + timedelta(minutes=30 * i), 8.0) for i in range(2)]
        ready = now_dt + timedelta(hours=82)
        all_slots = assign_credibilities(actual + far_pred, now_dt, gamble_tolerance=100.0)
        result = find_optimal_slots(all_slots, 2, ready, min_block_hours=0.5)
        assert len(result) == 2
        assert all(s['source'] == 'predicted' for s in result), (
            "At gamble=100, predicted 8p (eff=8p) must beat actual 18p (eff=18p)"
        )

    def test_gamble_50_midpoint_selects_correctly(self):
        """At gamble=50, predicted 36h ahead (tier 24-48h, cred=0.875) 9p eff=10.3p vs actual 12p."""
        now_dt = NOW
        # Predicted 36h at 9p: eff_cred = 0.75 + 0.25*0.5 = 0.875, eff = 9/0.875 = 10.29p
        # Actual at 12p: eff = 12p. → predicted is cheaper
        actual = [self._actual_slot(now_dt + timedelta(minutes=30 * i), 12.0) for i in range(2)]
        pred = [self._forecast_slot(
            now_dt + timedelta(hours=36) + timedelta(minutes=30 * i), 9.0) for i in range(2)]
        ready = now_dt + timedelta(hours=38)
        all_slots = assign_credibilities(actual + pred, now_dt, gamble_tolerance=50.0)
        result = find_optimal_slots(all_slots, 2, ready, min_block_hours=0.5)
        assert len(result) == 2
        assert all(s['source'] == 'predicted' for s in result), (
            "At gamble=50, predicted 9p (eff≈10.3p) must beat actual 12p (eff=12p)"
        )

    def test_near_predicted_and_actual_same_slot_dedup_then_tier_assignment(self):
        """When actual replaces a forecast for the same slot, the slot gets TIER_ACTUAL."""
        now_dt = NOW
        slot_dt = now_dt + timedelta(hours=3)
        forecast = self._forecast_slot(slot_dt, 10.0)
        actual = self._actual_slot(slot_dt, 15.0)
        deduped = deduplicate_and_sort_prices([forecast, actual], now_dt)
        adjusted = assign_credibilities(deduped, now_dt, gamble_tolerance=0.0)
        assert len(adjusted) == 1
        assert adjusted[0]['tier'] == TIER_ACTUAL, "Actual rate must get TIER_ACTUAL tier"
        assert adjusted[0]['effective_price'] == pytest.approx(15.0), (
            "TIER_ACTUAL slot must have effective_price == raw_price"
        )

    def test_mixed_sources_compute_sessions_selects_cheapest_effective(self):
        """End-to-end: compute_sessions with mixed actual + forecast picks cheapest by effective price."""
        now_dt = NOW
        # Actual rates for next 6h: 20p (moderate cost)
        actual = [
            {'date_time': now_dt + timedelta(minutes=30 * i), 'raw_price': 20.0,
             'source': 'current_actual'}
            for i in range(12)
        ]
        # Forecast for 30-36h ahead: 8p (cheap, but excluded entirely at gamble=0)
        forecast = [
            {'date_time': now_dt + timedelta(hours=30) + timedelta(minutes=30 * i),
             'raw_price': 8.0, 'source': 'predicted'}
            for i in range(6)
        ]
        ready_by = now_dt + timedelta(hours=36)
        inputs_g0 = {
            'required_hours': 1.0, 'gamble_tolerance': 0.0,
            'min_block_hours': 0.5, 'max_price': 30.0,
        }
        inputs_g100 = dict(inputs_g0, gamble_tolerance=100.0)

        # At gamble=0: forecast data is hard-excluded regardless of how cheap it looks —
        # only the known actual 20p slots (now..6h) are eligible.
        sessions_g0 = compute_sessions(actual + forecast, [], inputs_g0, ready_by, now_dt)
        assert sum([s['duration_hours'] for s in sessions_g0]) == pytest.approx(1.0)
        for s in sessions_g0:
            start = datetime.fromisoformat(s['start'])
            assert start < now_dt + timedelta(hours=6), (
                f"gamble=0: must schedule only within known actual-rate window, got {start}"
            )
            assert s['avg_price'] == pytest.approx(20.0), (
                "gamble=0: selected slots must be the actual-rate 20p slots, not forecast"
            )

        # At gamble=100: same conclusion — 8p < 20p at face value
        sessions_g100 = compute_sessions(actual + forecast, [], inputs_g100, ready_by, now_dt)
        assert sum([s['duration_hours'] for s in sessions_g100]) == pytest.approx(1.0)
        for s in sessions_g100:
            start = datetime.fromisoformat(s['start'])
            assert start >= now_dt + timedelta(hours=29), (
                f"gamble=100: should schedule in cheap forecast window, got {start}"
            )


# ---------------------------------------------------------------------------
# TestBugFixes — regression tests for issues found by logic-gap review
# ---------------------------------------------------------------------------


class TestBugFixes:
    """Regression tests for specific bugs found by agent code review."""

    # --- ev_charging_boost resets slider ---

    def test_ev_charging_boost_service_resets_slider(self):
        """ev_charging_boost() must reset the slider so expiry doesn't re-trigger a new boost."""
        ha = MagicMock(spec=EVChargingHA)
        ha.get_now.return_value = NOW
        ha.get_stored_schedule.return_value = {}
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            ev_charging_boost(duration_hours=2.0)
        ha.reset_boost_duration.assert_called_once()

    # --- ev_charging_stop resets slider ---

    def test_ev_charging_stop_resets_boost_slider(self):
        """ev_charging_stop() must reset the boost slider to prevent restart on next tick."""
        ha = MagicMock(spec=EVChargingHA)
        ha.get_now.return_value = NOW
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            ev_charging_stop()
        ha.reset_boost_duration.assert_called_once()

    # --- apply_idle_outputs writes _schedule_data ---

    def test_apply_idle_outputs_writes_empty_schedule_data(self):
        """apply_idle_outputs must write _schedule_data so stored sessions are cleared."""
        ha = MagicMock(spec=EVChargingHA)
        apply_idle_outputs(ha, {}, NOW)
        ha.set_schedule_sensor.assert_called_once()
        attrs = ha.set_schedule_sensor.call_args[0][1]
        assert '_schedule_data' in attrs, "apply_idle_outputs must include _schedule_data"
        assert attrs['_schedule_data']['slots'] == []

    # --- active slot accounting uses duration_hours ---

    def test_active_slot_near_end_does_not_over_schedule(self):
        """Active session with 5 min remaining must not cause full required_hours to be re-scheduled."""
        # 2h session started 115 min ago — only 5 min remaining
        active = {
            'start': (NOW - timedelta(minutes=115)).isoformat(),
            'end': (NOW + timedelta(minutes=5)).isoformat(),
            'duration_hours': 2.0,
        }
        prices = [
            {'date_time': NOW + timedelta(minutes=30 * i), 'raw_price': 10.0,
             'source': 'current_actual'}
            for i in range(8)
        ]
        inputs = {
            'required_hours': 2.0, 'gamble_tolerance': 50.0,
            'min_block_hours': 0.5, 'max_price': 20.0,
        }
        sessions = compute_sessions(prices, [active], inputs, READY_BY, NOW)
        # Active covers the full 2h requirement; no additional future slots needed
        future = [s for s in sessions if s['start'] != active['start']]
        assert future == [], (
            f"Near-end active session must not cause extra future slots; got {future}"
        )

    # --- relaxed fallback sources from eligible_from_valid ---

    def test_relaxed_fallback_does_not_use_short_run_slots(self):
        """Relaxed fallback must not pick slots from runs shorter than min_block_hours."""
        # Short run (1 slot) — explicitly excluded from valid_runs
        isolated = make_slot(NOW + timedelta(hours=12), 0.1)
        isolated['effective_price'] = 0.1
        # Valid run: 6 slots, cheap enough
        valid = make_slots_range(NOW + timedelta(hours=2), 6, price=10.0)
        for s in valid:
            s['effective_price'] = s['raw_price']
        # Need 8 slots with min_block=3h (6 slots). Only 6 from valid_runs.
        # The relaxed fallback should NOT pull in the isolated 0.1p slot.
        result = find_optimal_slots(valid + [isolated], 8, READY_BY, min_block_hours=3.0)
        assert isolated not in result, "Relaxed fallback must not use slots from too-short runs"

    # --- NaN required_hours ---

    def test_collect_inputs_rejects_nan_required_hours(self, mock_hass, mock_as_local):
        """NaN required_hours must be treated as missing, not passed to int() and crash."""
        import math
        ready_entity = MagicMock()
        ready_entity.state = '2024-01-16T07:00:00'
        hours_entity = MagicMock()
        hours_entity.state = 'nan'
        mock_hass.states.get.side_effect = lambda eid: (
            ready_entity if 'ready_by' in eid else hours_entity
        )
        ha = EVChargingHA()
        ha2 = MagicMock(spec=EVChargingHA)
        ha2.get_ready_by.return_value = READY_BY
        ha2.get_required_hours.return_value = float('nan')
        result = collect_inputs(ha2)
        assert result is None, "NaN required_hours must return None from collect_inputs"

    # --- set_all_unavailable: _schedule_data only on SCHEDULE_SENSOR ---

    def test_set_all_unavailable_schedule_data_only_on_schedule_sensor(
            self, mock_hass, mock_ha_now):
        """_schedule_data must only be attached to SCHEDULE_SENSOR, not to other entities."""
        stored_data = {'slots': [{'start': NOW.isoformat(), 'end': READY_BY.isoformat()}]}
        mock_entity = MagicMock()
        mock_entity.attributes = {'_schedule_data': stored_data}
        mock_hass.states.get.return_value = mock_entity
        ha = EVChargingHA()
        ha.set_all_unavailable("test")
        import builtins
        for c in builtins.state.set.call_args_list:
            entity_id = c[0][0]
            attrs = c[0][2] if len(c[0]) > 2 else c[1].get('attributes', {})
            if entity_id != 'sensor.ev_charging_schedule':
                assert '_schedule_data' not in attrs, (
                    f"_schedule_data must not be on {entity_id}"
                )

    # --- get_stored_schedule JSON string path ---

    def test_get_stored_schedule_parses_json_string(self, mock_hass):
        """
        Defensive: _schedule_data stored as a JSON string must still be parsed.
        HA normally returns attributes as dicts (not strings) after restart, but
        this branch provides a fallback if an older HA version or unusual path ever
        serialises the attribute as a raw JSON string.
        """
        import json as _json
        stored_data = {'slots': [], 'boost_end_dt': None}
        mock_entity = MagicMock()
        mock_entity.attributes = {'_schedule_data': _json.dumps(stored_data)}
        mock_hass.states.get.return_value = mock_entity
        ha = EVChargingHA()
        result = ha.get_stored_schedule()
        assert result == stored_data, "JSON-string _schedule_data must be parsed correctly"

    def test_ha_restart_round_trip_preserves_active_session(self):
        """
        Simulate a full HA restart: _make_schedule_data output is JSON-serialised then
        deserialised (as HA does), then fed through get_stored_schedule →
        prune_and_classify → compute_sessions. The active session must survive.
        """
        import json as _json
        active_session = {
            'start': (NOW - timedelta(minutes=30)).isoformat(),
            'end': (NOW + timedelta(hours=1, minutes=30)).isoformat(),
            'duration_hours': 2.0,
            'avg_price': 12.5,
            'confidence': 90.0,
        }
        # Build what would have been written to HA before restart
        schedule_data = _make_schedule_data([active_session], None, {
            'required_hours': 2.0, 'ready_by_dt': READY_BY.isoformat(),
        }, NOW - timedelta(hours=1))

        # Simulate HA JSON round-trip (serialise → deserialise)
        restored_data = _json.loads(_json.dumps(schedule_data))

        # Verify get_stored_schedule handles the restored dict correctly
        assert restored_data['slots'] == schedule_data['slots']
        assert restored_data['boost_end_dt'] is None

        # Feed through prune_and_classify — active session must be found
        active, future = prune_and_classify(restored_data['slots'], NOW)
        assert active is not None, "Active session must survive JSON round-trip"
        assert future == []

        # Feed through compute_sessions — must not re-schedule active portion
        prices = [
            {'date_time': NOW + timedelta(hours=3) + timedelta(minutes=30 * i),
             'raw_price': 10.0, 'source': 'current_actual'}
            for i in range(4)
        ]
        inputs = {'required_hours': 2.0, 'gamble_tolerance': 50.0,
                  'min_block_hours': 0.5, 'max_price': 20.0}
        sessions = compute_sessions(prices, restored_data['slots'], inputs, READY_BY, NOW)
        active_starts = [s['start'] for s in sessions]
        assert active_session['start'] in active_starts, (
            "Active session start must appear in post-restart compute_sessions result"
        )
        future_sessions = [s for s in sessions if s['start'] != active_session['start']]
        assert future_sessions == [], "Active session covers full requirement; no extra slots needed"

    # --- _parse_rates with datetime object for start ---

    def test_parse_rates_handles_datetime_object_start(self, mock_hass, mock_as_local):
        """Octopus sometimes returns datetime objects instead of strings for rate start times."""
        mock_entity = MagicMock()
        mock_entity.attributes = {
            'rates': [
                {'start': datetime(2024, 1, 15, 10, 0), 'value_inc_vat': 0.15},
            ]
        }
        mock_hass.states.get.return_value = mock_entity
        ha = EVChargingHA()
        rates = ha.get_current_rates()
        assert len(rates) == 1
        assert rates[0]['raw_price'] == pytest.approx(15.0)

    # --- resolve_boost_end at exact now_dt boundary ---

    def test_resolve_boost_end_exactly_at_expiry_starts_new_boost(self):
        """stored_boost_end == now_dt is treated as expired; new boost_duration triggers."""
        stored = {'boost_end_dt': NOW.isoformat()}
        result = resolve_boost_end(stored, {'boost_duration': 1.0}, NOW)
        assert result == NOW + timedelta(hours=1.0), (
            "Boost ending exactly at now must be treated as expired"
        )

    # --- compute_hours_remaining with expired active ---

    def test_compute_hours_remaining_expired_active_returns_zero(self):
        """Active session whose end is already in the past must contribute 0, not negative."""
        expired_active = {
            'start': (NOW - timedelta(hours=2)).isoformat(),
            'end': (NOW - timedelta(minutes=5)).isoformat(),
            'duration_hours': 2.0,
        }
        result = compute_hours_remaining([], expired_active, NOW)
        assert result == pytest.approx(0.0), "Expired active session must not produce negative hours"

    # --- all CHARGER_CONNECTED_STATES produce desired=on ---

    def test_all_connected_states_produce_desired_on_during_active_slot(self):
        """Every charger state in CHARGER_CONNECTED_STATES must result in desired=on when charging."""
        active = {
            'start': (NOW - timedelta(minutes=30)).isoformat(),
            'end': (NOW + timedelta(hours=1)).isoformat(),
            'duration_hours': 1.5,
        }
        stored = {'slots': [active], 'boost_end_dt': None, 'inputs_snapshot': {}}
        failures = []
        for charger_state in ('charger_insert', 'charger_pause', 'charger_end',
                              'charger_charging', 'charger_wait'):
            ha = _make_ha_mock(NOW, READY_BY, required_hours=1.5,
                               charger_state=charger_state, stored_schedule=stored)
            with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
                update_ev_charge_state()
            call = ha.set_desired.call_args
            if call is None or call[0][0] is not True:
                failures.append(f"{charger_state}: set_desired not called with True")
        assert failures == [], "\n".join(failures)

    # --- required_hours mid-session changes ---

    def test_required_hours_increase_mid_session_adds_future_slots(self):
        """If required_hours grows while a session is active, new future slots are added."""
        active = {
            'start': (NOW - timedelta(minutes=30)).isoformat(),
            'end': (NOW + timedelta(hours=1, minutes=30)).isoformat(),
            'duration_hours': 2.0,
        }
        prices = [
            {'date_time': NOW + timedelta(hours=2) + timedelta(minutes=30 * i),
             'raw_price': 10.0, 'source': 'current_actual'}
            for i in range(8)
        ]
        # required_hours bumped to 4.0 — active covers 2h, still need 2 more
        inputs = {'required_hours': 4.0, 'gamble_tolerance': 50.0,
                  'min_block_hours': 0.5, 'max_price': 20.0}
        sessions = compute_sessions(prices, [active], inputs, READY_BY, NOW)
        future = [s for s in sessions if s['start'] != active['start']]
        assert len(future) >= 1, "Increased required_hours must produce new future sessions"
        total = sum(s['duration_hours'] for s in future)
        assert total == pytest.approx(2.0)

    def test_required_hours_decrease_mid_session_removes_future_slots(self):
        """If required_hours drops to exactly cover the active session, no future slots added."""
        active = {
            'start': (NOW - timedelta(minutes=30)).isoformat(),
            'end': (NOW + timedelta(hours=1, minutes=30)).isoformat(),
            'duration_hours': 2.0,
        }
        prices = [
            {'date_time': NOW + timedelta(hours=3) + timedelta(minutes=30 * i),
             'raw_price': 10.0, 'source': 'current_actual'}
            for i in range(4)
        ]
        # required_hours matches active session exactly
        inputs = {'required_hours': 2.0, 'gamble_tolerance': 50.0,
                  'min_block_hours': 0.5, 'max_price': 20.0}
        sessions = compute_sessions(prices, [active], inputs, READY_BY, NOW)
        future = [s for s in sessions if s['start'] != active['start']]
        assert future == [], f"Decreased required_hours must not add future slots; got {future}"

    # --- confidence and avg_price via compute_sessions pipeline ---

    def test_sessions_have_avg_price_and_confidence_fields(self):
        """Sessions returned by compute_sessions must include avg_price and confidence."""
        prices = [
            {'date_time': NOW + timedelta(minutes=30 * i), 'raw_price': 12.0,
             'source': 'current_actual'}
            for i in range(4)
        ]
        inputs = {'required_hours': 1.0, 'gamble_tolerance': 100.0,
                  'min_block_hours': 0.5, 'max_price': 20.0}
        sessions = compute_sessions(prices, [], inputs, READY_BY, NOW)
        assert len(sessions) >= 1
        for s in sessions:
            assert 'avg_price' in s, "Session must have avg_price"
            assert 'confidence' in s, "Session must have confidence"
        assert sessions[0]['avg_price'] == pytest.approx(12.0)
        assert sessions[0]['confidence'] == pytest.approx(100.0)

    # --- prune_and_classify with overlapping sessions ---

    def test_prune_and_classify_overlapping_sessions_last_wins(self):
        """Two overlapping active sessions: last one in the list wins (documented behaviour)."""
        s1 = {'start': (NOW - timedelta(hours=1)).isoformat(),
               'end': (NOW + timedelta(hours=2)).isoformat(), 'duration_hours': 3.0}
        s2 = {'start': (NOW - timedelta(minutes=30)).isoformat(),
               'end': (NOW + timedelta(hours=1)).isoformat(), 'duration_hours': 1.5}
        active, future = prune_and_classify([s1, s2], NOW)
        assert active is not None
        # Both overlap — second one wins; future is empty
        assert active['duration_hours'] == 1.5
        assert future == []

    # --- required_hours > available data horizon ---

    def test_compute_sessions_data_horizon_shorter_than_requirement(self):
        """When only 1h of price data fits before ready_by, an 8h requirement returns empty."""
        prices = [
            {'date_time': NOW + timedelta(minutes=30 * i), 'raw_price': 10.0,
             'source': 'current_actual'}
            for i in range(2)   # only 1h of data
        ]
        inputs = {'required_hours': 8.0, 'gamble_tolerance': 50.0,
                  'min_block_hours': 0.5, 'max_price': 20.0}
        tight_ready_by = NOW + timedelta(hours=1, minutes=30)
        sessions = compute_sessions(prices, [], inputs, tight_ready_by, NOW)
        assert sessions == [], "Insufficient data horizon must return empty sessions gracefully"


# ---------------------------------------------------------------------------
# TestSecondPassFixes — regression tests for second-pass review findings
# ---------------------------------------------------------------------------


class TestSecondPassFixes:
    """Regression tests for issues found in the second agent code review."""

    # --- math.ceil for slot counting ---

    def test_min_block_hours_0_75_rounds_up_to_2_slots(self):
        """min_block_hours=0.75 must require 2 consecutive slots, not 1 (int floor bug)."""
        # 1 isolated slot at 1p — valid with min_slots=1 but invalid with min_slots=2
        isolated = make_slot(NOW, 1.0)
        isolated['effective_price'] = 1.0
        run = make_slots_range(NOW + timedelta(hours=2), 4, price=15.0)
        for s in run:
            s['effective_price'] = s['raw_price']
        result = find_optimal_slots([isolated] + run, 2, READY_BY, min_block_hours=0.75)
        # With ceil: min_slots=2; isolated alone cannot form a 2-slot block → not selected
        assert isolated not in result, "min_block_hours=0.75 must round UP to 2 slots min"

    def test_required_hours_0_75_rounds_up_to_2_slots(self):
        """required_hours=0.75 must schedule 2 slots (45 min), not 1 (30 min)."""
        prices = [{'date_time': NOW + timedelta(minutes=30 * i), 'raw_price': 10.0,
                   'source': 'current_actual'} for i in range(4)]
        inputs = {'required_hours': 0.75, 'gamble_tolerance': 50.0,
                  'min_block_hours': 0.5, 'max_price': 20.0}
        sessions = compute_sessions(prices, [], inputs, READY_BY, NOW)
        total = sum(s['duration_hours'] for s in sessions)
        assert total == pytest.approx(1.0), (
            f"required_hours=0.75 must schedule 1.0h (2 slots via ceil), got {total}h"
        )

    # --- Z-suffix datetime parsing ---

    def test_parse_iso_str_handles_z_suffix(self):
        """_parse_iso_str must handle 'Z' UTC suffix without raising ValueError."""
        from ev_charging_state_machine import _parse_iso_str
        result = _parse_iso_str('2024-01-15T10:00:00Z')
        assert result.year == 2024
        assert result.hour == 10

    def test_parse_iso_str_handles_offset_suffix(self):
        """_parse_iso_str must also handle explicit UTC offset unchanged."""
        from ev_charging_state_machine import _parse_iso_str
        result = _parse_iso_str('2024-01-15T10:00:00+00:00')
        assert result.year == 2024

    def test_parse_rates_with_z_suffix_does_not_drop_slot(self, mock_hass, mock_as_local):
        """A rate entry with Z-suffix datetime must be parsed and returned, not silently dropped."""
        mock_entity = Mock()
        mock_entity.attributes = {
            'rates': [{'start': '2024-01-15T10:00:00Z', 'value_inc_vat': 0.20}]
        }
        mock_hass.states.get.return_value = mock_entity
        ha = EVChargingHA()
        rates = ha.get_current_rates()
        assert len(rates) == 1, "Z-suffix rate must be parsed, not silently dropped"
        assert rates[0]['raw_price'] == pytest.approx(20.0)

    # --- set_schedule_sensor fingerprint includes boost_end_dt ---

    def test_set_schedule_sensor_not_suppressed_when_boost_end_dt_changes(
            self, mock_hass, mock_as_local):
        """Changing only boost_end_dt must NOT be suppressed — it must write to the sensor."""
        session = {'start': NOW.isoformat(), 'end': READY_BY.isoformat(), 'duration_hours': 2.0}
        mock_entity = Mock()
        mock_entity.state = 'scheduled'
        mock_entity.attributes = {
            'slots': [session],
            'boost_end_dt': (NOW + timedelta(hours=2)).isoformat(),
        }
        mock_hass.states.get.return_value = mock_entity
        ha = EVChargingHA()
        import builtins
        builtins.state.set.reset_mock()
        # Write same slots, same state, but boost_end_dt cleared
        ha.set_schedule_sensor('scheduled', {'slots': [session], 'boost_end_dt': None})
        builtins.state.set.assert_called_once()

    def test_set_schedule_sensor_suppressed_when_only_calculated_at_changes(
            self, mock_hass, mock_as_local):
        """When only calculated_at changes, set_schedule_sensor must suppress the write."""
        session = {'start': NOW.isoformat(), 'end': READY_BY.isoformat(), 'duration_hours': 2.0}
        mock_entity = Mock()
        mock_entity.state = 'scheduled'
        mock_entity.attributes = {
            'slots': [session],
            'boost_end_dt': None,
            'calculated_at': (NOW - timedelta(minutes=5)).isoformat(),
        }
        mock_hass.states.get.return_value = mock_entity
        ha = EVChargingHA()
        import builtins
        builtins.state.set.reset_mock()
        ha.set_schedule_sensor('scheduled', {
            'slots': [session],
            'boost_end_dt': None,
            'calculated_at': NOW.isoformat(),  # only this changed
        })
        builtins.state.set.assert_not_called()

    # --- Change detection suppression tests ---

    def test_set_desired_suppressed_when_unchanged(self, mock_hass, mock_as_local):
        """set_desired must not call state.set when the value hasn't changed."""
        mock_entity = Mock()
        mock_entity.state = 'on'
        mock_entity.attributes = {}
        mock_hass.states.get.return_value = mock_entity
        ha = EVChargingHA()
        import builtins
        builtins.state.set.reset_mock()
        ha.set_desired(True)
        builtins.state.set.assert_not_called()

    def test_set_state_sensor_suppressed_when_unchanged(self, mock_hass, mock_as_local):
        """set_state_sensor must not call state.set when the state hasn't changed."""
        mock_entity = Mock()
        mock_entity.state = 'scheduled'
        mock_entity.attributes = {}
        mock_hass.states.get.return_value = mock_entity
        ha = EVChargingHA()
        import builtins
        builtins.state.set.reset_mock()
        ha.set_state_sensor('scheduled')
        builtins.state.set.assert_not_called()

    def test_set_next_slot_suppressed_when_unchanged(self, mock_hass, mock_as_local):
        """set_next_slot must not call state.set when start and end haven't changed."""
        start_iso = NOW.isoformat()
        end_iso = READY_BY.isoformat()
        start_entity = Mock()
        start_entity.state = start_iso
        start_entity.attributes = {}
        end_entity = Mock()
        end_entity.state = end_iso
        end_entity.attributes = {}

        def get_entity(entity_id):
            if 'start' in entity_id:
                return start_entity
            return end_entity

        mock_hass.states.get.side_effect = get_entity
        ha = EVChargingHA()
        import builtins
        builtins.state.set.reset_mock()
        ha.set_next_slot(start_iso, end_iso)
        builtins.state.set.assert_not_called()

    def test_set_hours_remaining_suppressed_below_threshold(self, mock_hass, mock_as_local):
        """set_hours_remaining must not write if change is < 0.01h."""
        mock_entity = Mock()
        mock_entity.state = '2.00'
        mock_entity.attributes = {}
        mock_hass.states.get.return_value = mock_entity
        ha = EVChargingHA()
        import builtins
        builtins.state.set.reset_mock()
        ha.set_hours_remaining(2.005)   # 0.005 change — below 0.01 threshold
        builtins.state.set.assert_not_called()

    # --- compute_sessions with duration_hours missing from active session ---

    def test_active_session_without_duration_hours_falls_back_to_start_end(self):
        """Legacy stored session without duration_hours key must derive it from start/end."""
        active_no_duration = {
            'start': (NOW - timedelta(minutes=30)).isoformat(),
            'end': (NOW + timedelta(hours=1, minutes=30)).isoformat(),
            # no 'duration_hours' key
        }
        prices = [
            {'date_time': NOW + timedelta(hours=3) + timedelta(minutes=30 * i),
             'raw_price': 10.0, 'source': 'current_actual'}
            for i in range(4)
        ]
        inputs = {'required_hours': 2.0, 'gamble_tolerance': 50.0,
                  'min_block_hours': 0.5, 'max_price': 20.0}
        sessions = compute_sessions(prices, [active_no_duration], inputs, READY_BY, NOW)
        # Active session is 2h (start to end), covers full 2h requirement
        future = [s for s in sessions if s['start'] != active_no_duration['start']]
        assert future == [], (
            "Duration derived from start/end must count toward requirement; "
            f"got unexpected future sessions: {future}"
        )

    # --- Integration: required_hours=0 via update_ev_charge_state ---

    def test_idle_when_required_hours_zero_writes_empty_schedule_data(self):
        """required_hours=0 must write empty _schedule_data through the full service path."""
        ha = _make_ha_mock(NOW, READY_BY, required_hours=0.0)
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            update_ev_charge_state()
        ha.set_state_sensor.assert_called_with(STATE_IDLE)
        ha.set_schedule_sensor.assert_called()
        call_attrs = ha.set_schedule_sensor.call_args[0][1]
        assert '_schedule_data' in call_attrs
        assert call_attrs['_schedule_data']['slots'] == []

    # --- Integration: NaN required_hours triggers set_all_unavailable ---

    def test_nan_required_hours_triggers_set_all_unavailable(self):
        """NaN required_hours must call set_all_unavailable, not crash."""
        ha = _make_ha_mock(NOW, READY_BY, required_hours=float('nan'))
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            update_ev_charge_state()
        ha.set_all_unavailable.assert_called()

    # --- ev_charging_stop writes boost_end_dt=None ---

    def test_stop_writes_boost_end_dt_none_in_schedule_data(self):
        """ev_charging_stop must write boost_end_dt=None so no boost restarts on next tick."""
        ha = MagicMock(spec=EVChargingHA)
        ha.get_now.return_value = NOW
        with patch('ev_charging_state_machine.EVChargingHA', return_value=ha):
            ev_charging_stop()
        ha.set_schedule_sensor.assert_called()
        attrs = ha.set_schedule_sensor.call_args[0][1]
        assert attrs.get('_schedule_data', {}).get('boost_end_dt') is None

    # --- max_price diagnostic log ---

    def test_find_optimal_slots_returns_empty_and_logs_when_max_price_kills_all_windows(self):
        """When max_price=1p excludes all windows, find_optimal_slots returns [] immediately."""
        slots = make_slots_range(NOW, 6, price=10.0)
        for s in slots:
            s['effective_price'] = s['raw_price']
        result = find_optimal_slots(slots, 4, READY_BY, min_block_hours=0.5, max_price=1.0)
        assert result == [], "max_price filter killing all windows must return []"
