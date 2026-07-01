# pyscript/ev_charging_state_machine.py
from datetime import datetime, timedelta
import json
import logging
import math
from homeassistant.util.dt import as_local, now as ha_now

_LOGGER = logging.getLogger(__name__)

# Set True to write DEBUG logs to a local file (useful when HA log only shows INFO+).
_DEBUG_LOG_ENABLED = False
_DEBUG_LOG_FILE = '/config/pyscript/ev_charging_debug.log'

for _h in list(_LOGGER.handlers):
    if isinstance(_h, logging.FileHandler):
        _LOGGER.removeHandler(_h)
        _h.close()
if _DEBUG_LOG_ENABLED:
    try:
        _fh = logging.FileHandler(_DEBUG_LOG_FILE, mode='a')
        _fh.setLevel(logging.DEBUG)
        _fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)-8s %(message)s'))
        _LOGGER.addHandler(_fh)
        _LOGGER.setLevel(logging.DEBUG)
    except Exception:
        pass

# --- Input Entity IDs (existing) ---
READY_BY_ENTITY = 'input_datetime.ev_charger_ready_by'
CHARGING_HOURS_ENTITY = 'input_number.polestar_2_charging_hours_required'
OCTOPUS_CURRENT_RATES_ENTITY = 'event.octopus_energy_electricity_XXXXX_XXXXXX_current_day_rates'
OCTOPUS_NEXT_RATES_ENTITY = 'event.octopus_energy_electricity_XXXXX_XXXXXX_next_day_rates'
AGILE_FORECAST_ENTITY = 'sensor.agile_forecast'
CHARGER_WORK_STATE_ENTITY = 'sensor.car_charger_work_state'

# --- Input Entity IDs (new) ---
GAMBLE_TOLERANCE_ENTITY = 'input_number.ev_charging_gamble_tolerance'
MIN_BLOCK_HOURS_ENTITY = 'input_number.ev_charging_min_block_hours'
MAX_PRICE_ENTITY = 'input_number.ev_charging_max_price'
BOOST_DURATION_ENTITY = 'input_number.ev_charging_boost_duration_hours'

# --- Output Entity IDs ---
DESIRED_BINARY = 'binary_sensor.ev_charging_desired'
STATE_SENSOR = 'sensor.ev_charging_state'
SCHEDULE_SENSOR = 'sensor.ev_charging_schedule'
NEXT_SLOT_START_SENSOR = 'sensor.ev_charging_next_slot_start'
NEXT_SLOT_END_SENSOR = 'sensor.ev_charging_next_slot_end'
HOURS_REMAINING_SENSOR = 'sensor.ev_charging_hours_remaining'

# --- Charger states that indicate a car is connected ---
CHARGER_CONNECTED_STATES = {'charger_insert', 'charger_pause', 'charger_end', 'charger_charging', 'charger_wait'}

# --- State machine state names ---
STATE_IDLE = 'idle'
STATE_SCHEDULED = 'scheduled'
STATE_CHARGING = 'charging'
STATE_BOOSTING = 'boosting'
STATE_COMPLETE = 'complete'
STATE_UNSCHEDULABLE = 'unschedulable'
STATE_ERROR = 'error'

# --- Credibility tiers ---
TIER_ACTUAL = 'actual'
TIER_PREDICTED_0_24 = 'predicted_0_24'
TIER_PREDICTED_24_48 = 'predicted_24_48'
TIER_PREDICTED_48_72 = 'predicted_48_72'
TIER_PREDICTED_72_PLUS = 'predicted_72_plus'

BASE_CREDIBILITY = {
    TIER_ACTUAL: 1.0,
    TIER_PREDICTED_0_24: 0.90,
    TIER_PREDICTED_24_48: 0.75,
    TIER_PREDICTED_48_72: 0.60,
    TIER_PREDICTED_72_PLUS: 0.40,
}

# --- Default values ---
DEFAULT_GAMBLE_TOLERANCE = 50.0
DEFAULT_MIN_BLOCK_HOURS = 1.0
DEFAULT_MAX_PRICE = 20.0


# =============================================================================
# Pure functions — no HA dependency, fully unit-testable
# =============================================================================

def get_source_tier(source, slot_dt, now_dt):
    """Return credibility tier string for a price slot based on source and time horizon."""
    if source in ('current_actual', 'next_actual'):
        return TIER_ACTUAL
    hours_ahead = (slot_dt - now_dt).total_seconds() / 3600
    if hours_ahead <= 24:
        return TIER_PREDICTED_0_24
    elif hours_ahead <= 48:
        return TIER_PREDICTED_24_48
    elif hours_ahead <= 72:
        return TIER_PREDICTED_48_72
    return TIER_PREDICTED_72_PLUS


def compute_effective_price(raw_price, tier, gamble_tolerance):
    """
    Return the risk-adjusted price used for slot ranking (NOT for max_price checks).
    Low gamble_tolerance inflates predicted prices, strongly favouring known Octopus rates.
    At gamble_tolerance=100 all prices are taken at face value regardless of source.
    raw_price in p/kWh.
    """
    base_cred = BASE_CREDIBILITY[tier]
    eff_cred = base_cred + (1.0 - base_cred) * (gamble_tolerance / 100.0)
    return raw_price / eff_cred


def assign_credibilities(slots, now_dt, gamble_tolerance):
    """Add 'tier' and 'effective_price' fields to each slot dict. Returns new list."""
    result = []
    for slot in slots:
        tier = get_source_tier(slot['source'], slot['date_time'], now_dt)
        eff_price = compute_effective_price(slot['raw_price'], tier, gamble_tolerance)
        entry = dict(slot)
        entry['tier'] = tier
        entry['effective_price'] = eff_price
        result.append(entry)
    return result


def filter_runs_by_max_avg_price(slots, max_price):
    """
    Remove entire contiguous runs whose AVERAGE raw price exceeds max_price.
    max_price is a session-level ceiling, not a per-slot ceiling. This allows
    price spikes within an otherwise-cheap window to remain eligible — a run
    with average 15p is kept even if one slot inside it is 21p.
    """
    if not slots:
        return []
    sorted_slots = sorted(slots, key=lambda s: s['date_time'])
    runs = build_contiguous_runs(sorted_slots)
    result = []
    for run in runs:
        avg_raw = sum([s['raw_price'] for s in run]) / len(run)
        start_s = run[0]['date_time'].strftime('%m-%d %H:%M')
        end_s = (run[-1]['date_time'] + timedelta(minutes=30)).strftime('%H:%M')
        if avg_raw <= max_price:
            result.extend(run)
            _LOGGER.debug(
                f"  max_price: kept run {start_s}-{end_s} "
                f"avg={avg_raw:.1f}p <= {max_price}p ({len(run)} slots)"
            )
        else:
            _LOGGER.debug(
                f"  max_price: excluded run {start_s}-{end_s} "
                f"avg={avg_raw:.1f}p > {max_price}p ({len(run)} slots)"
            )
    return result


def build_contiguous_runs(slots):
    """
    Group a sorted list of 30-min price slots into runs of consecutive slots.
    Two slots are consecutive when the second starts exactly 30 min after the first.
    """
    if not slots:
        return []
    runs = []
    current_run = [slots[0]]
    for slot in slots[1:]:
        expected = current_run[-1]['date_time'] + timedelta(minutes=30)
        if slot['date_time'] == expected:
            current_run.append(slot)
        else:
            runs.append(current_run)
            current_run = [slot]
    runs.append(current_run)
    return runs


def find_optimal_slots(candidate_slots, required_slots, ready_by_dt, min_block_hours, max_price=None):
    """
    Select the cheapest combination of slots totalling required_slots, where every
    contiguous block is >= min_block_hours and (if max_price given) each block's
    average raw price <= max_price.

    min_block_hours is capped at the total requirement: a floor longer than the
    session itself would make every sub-min_block request unschedulable, so it is
    relaxed to required_slots and the whole requirement is scheduled as one block.

    Enumerates all valid contiguous windows (sub-sequences of >= min_block_hours) from
    the price data, scores each by weighted average effective_price, then greedily picks
    the cheapest non-overlapping combination that sums to exactly required_slots.

    max_price is applied at window level (per-session average), not per-slot or per-run.
    This means a cheap afternoon window qualifies even if the overall dataset average is
    above max_price due to expensive peaks elsewhere in the same continuous price stream.

    If no exact strict-block solution exists, a relaxed fallback fills any remainder
    with the cheapest available slots (the last block may be shorter than min_block_hours).

    Returns a flat list of selected slot dicts, or [] if no valid selection exists.
    Slots must already have 'effective_price' set (see assign_credibilities).
    """
    _LOGGER.debug(
        f"find_optimal_slots: {len(candidate_slots)} candidates, need {required_slots} slots "
        f"({required_slots * 0.5:.1f}h), min_block={min_block_hours}h, "
        f"max_price={max_price}p, ready_by={ready_by_dt.strftime('%m-%d %H:%M')}"
    )

    if not candidate_slots or required_slots <= 0:
        _LOGGER.debug("  Early exit: no candidates or zero required slots")
        return []

    # min_block_hours can never exceed the total requirement — a "minimum session
    # length" longer than the whole session would make every request for less than
    # that long unschedulable. When that happens, relax the floor to required_slots
    # so the entire requirement is scheduled as a single contiguous block instead.
    min_slots_per_block = min(max(1, math.ceil(min_block_hours * 2)), required_slots)
    if min_slots_per_block < math.ceil(min_block_hours * 2):
        _LOGGER.debug(
            f"  min_block={min_block_hours}h exceeds the {required_slots * 0.5:.1f}h requirement; "
            f"relaxing minimum block to {min_slots_per_block * 0.5:.1f}h (single contiguous block)"
        )

    # Only consider slots whose 30-min window ends by ready_by
    eligible = [
        s for s in candidate_slots
        if s['date_time'] + timedelta(minutes=30) <= ready_by_dt
    ]
    eligible.sort(key=lambda s: s['date_time'])
    removed_late = len(candidate_slots) - len(eligible)
    if removed_late:
        _LOGGER.debug(f"  Removed {removed_late} slots past ready_by, {len(eligible)} remain")

    # Identify runs long enough to ever satisfy min_block
    all_runs = build_contiguous_runs(eligible)
    valid_runs = [r for r in all_runs if len(r) >= min_slots_per_block]
    eligible_from_valid = [s for run in valid_runs for s in run]

    _LOGGER.debug(f"  Contiguous runs found: {len(all_runs)}")
    for run in all_runs:
        avg_p = sum([s['raw_price'] for s in run]) / len(run)
        if len(run) >= min_slots_per_block:
            _LOGGER.debug(
                f"  Run {run[0]['date_time'].strftime('%m-%d %H:%M')}-"
                f"{(run[-1]['date_time'] + timedelta(minutes=30)).strftime('%H:%M')}: "
                f"{len(run)} slots avg={avg_p:.1f}p — ELIGIBLE"
            )
        else:
            _LOGGER.debug(
                f"  Run {run[0]['date_time'].strftime('%m-%d %H:%M')}-"
                f"{(run[-1]['date_time'] + timedelta(minutes=30)).strftime('%H:%M')}: "
                f"{len(run)} slots avg={avg_p:.1f}p — SKIPPED (need {min_slots_per_block} slots)"
            )

    if len(eligible_from_valid) < required_slots:
        _LOGGER.debug(
            f"  FAIL: only {len(eligible_from_valid)} eligible slots across valid runs, "
            f"need {required_slots}"
        )
        return []

    # Enumerate all candidate windows: contiguous sub-sequences of valid_runs with
    # size in [min_slots_per_block, required_slots], scored by average effective_price.
    windows = []
    for run in valid_runs:
        max_window_size = min(required_slots, len(run))
        for size in range(min_slots_per_block, max_window_size + 1):
            for start_idx in range(len(run) - size + 1):
                w_slots = run[start_idx:start_idx + size]
                avg_eff = sum([s['effective_price'] for s in w_slots]) / size
                windows.append({
                    'slots': w_slots,
                    'dts': set([s['date_time'] for s in w_slots]),
                    'size': size,
                    'avg_eff': avg_eff,
                })
    # Apply max_price at window level: only keep windows whose average raw price
    # is within budget. This is intentionally per-window (session-level average),
    # not per-dataset-run, so a cheap afternoon window still qualifies even when
    # the overall dataset average exceeds max_price due to expensive peak periods.
    if max_price is not None:
        before = len(windows)
        windows = [
            w for w in windows
            if sum([s['raw_price'] for s in w['slots']]) / w['size'] <= max_price
        ]
        _LOGGER.debug(
            f"  max_price={max_price}p filter: {len(windows)} of {before} windows pass"
        )
        if not windows:
            _LOGGER.debug(
                f"  FAIL: max_price={max_price}p filter excluded all {before} candidate windows. "
                f"Raise max_price or reduce min_block_hours."
            )
            return []

    windows.sort(key=lambda w: w['avg_eff'])

    top5 = [
        f"{w['slots'][0]['date_time'].strftime('%m-%d %H:%M')} "
        f"sz={w['size']} avg={w['avg_eff']:.1f}p"
        for w in windows[:5]
    ]
    _LOGGER.debug(f"  {len(windows)} candidate windows after filters. Cheapest 5: {top5}")

    # Greedy selection: pick cheapest non-overlapping windows summing to required_slots.
    # At each step the remaining slots after this pick must be 0 or >= min_slots_per_block
    # so the remainder can always be filled with valid full-sized blocks.
    selected_dts = set()
    remaining = required_slots

    for w in windows:
        if w['size'] > remaining:
            continue
        after = remaining - w['size']
        if after > 0 and after < min_slots_per_block:
            continue  # would leave an unfillable gap
        if w['dts'] & selected_dts:
            continue  # overlaps already-selected slots
        selected_dts |= w['dts']
        remaining -= w['size']
        _LOGGER.debug(
            f"  Picked {w['slots'][0]['date_time'].strftime('%m-%d %H:%M')} "
            f"sz={w['size']} avg_eff={w['avg_eff']:.1f}p — {remaining} remaining"
        )
        if remaining == 0:
            break

    # Relaxed fallback: if some slots were selected via proper windows but a small
    # remainder can't be filled with another full-sized block, top up with the
    # cheapest available slots from valid runs (the last block may be shorter than
    # min_block_hours). Sourcing from eligible_from_valid (not the wider eligible
    # list) ensures we never re-introduce slots from runs that were explicitly
    # excluded because their run was too short to satisfy min_block.
    # Not used when NO proper windows were selected — that means the requirement
    # itself cannot be satisfied even partially (e.g. required < min_block_hours).
    if remaining > 0 and remaining < required_slots:
        _LOGGER.debug(
            f"  Strict fill left {remaining} slot(s) unfilled; "
            f"filling remainder (last block may be < {min_block_hours}h)"
        )
        leftover = sorted(
            [s for s in eligible_from_valid if s['date_time'] not in selected_dts],
            key=lambda s: s['effective_price'],
        )
        for s in leftover:
            selected_dts.add(s['date_time'])
            remaining -= 1
            if remaining == 0:
                break

    if remaining > 0:
        _LOGGER.debug(f"  FAIL: could not fill {required_slots} slots from available data")
        return []

    result = [s for s in eligible if s['date_time'] in selected_dts]
    result.sort(key=lambda s: s['date_time'])
    result_runs = build_contiguous_runs(result)
    _LOGGER.debug(f"  Selection complete: {len(result)} slots in {len(result_runs)} run(s)")
    for run in result_runs:
        avg_p = sum([s['raw_price'] for s in run]) / len(run)
        _LOGGER.debug(
            f"    Run {run[0]['date_time'].strftime('%m-%d %H:%M')}-"
            f"{(run[-1]['date_time'] + timedelta(minutes=30)).strftime('%H:%M')}: "
            f"{len(run)} slots avg={avg_p:.1f}p"
        )
    return result


def slots_to_sessions(selected_slots):
    """
    Convert a flat list of slot dicts into session dicts grouped by contiguous run.

    confidence (0–100): average base credibility of the slots in the session.
      100 = all Octopus actual rates (won't change).
      90  = near-term predictions (<24h ahead) — usually accurate.
      75  = 24–48h predictions.
      60  = 48–72h predictions.
      40  = >72h predictions — treat as a rough guide only.
    Mixed sessions produce values between these anchors.

    Returns [{'start', 'end', 'duration_hours', 'avg_price', 'confidence'}, ...]
    """
    if not selected_slots:
        return []
    sorted_slots = sorted(selected_slots, key=lambda s: s['date_time'])
    runs = build_contiguous_runs(sorted_slots)
    sessions = []
    for run in runs:
        start = run[0]['date_time']
        end = run[-1]['date_time'] + timedelta(minutes=30)
        avg_price = sum([s['raw_price'] for s in run]) / len(run)
        avg_cred = sum([BASE_CREDIBILITY.get(s.get('tier', TIER_ACTUAL), 1.0) for s in run]) / len(run)
        sessions.append({
            'start': start.isoformat(),
            'end': end.isoformat(),
            'duration_hours': len(run) * 0.5,
            'avg_price': round(avg_price, 2),
            'confidence': round(avg_cred * 100, 1),
        })
    return sessions


def _parse_iso_str(s):
    """
    Parse an ISO 8601 datetime string, normalising the 'Z' UTC suffix to '+00:00'
    before calling fromisoformat. Python 3.10 does not accept 'Z'; 3.11+ does.
    HA installs span both versions so we must handle it explicitly.
    """
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    return datetime.fromisoformat(s)


def _parse_dt(value):
    """
    Parse a datetime value from a session dict or stored attribute.
    HA sometimes returns datetime objects (not strings) when reading back
    entity attributes, so we must handle both types defensively.
    """
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def prune_and_classify(sessions, now_dt):
    """
    Split sessions into active and future relative to now_dt.
    Expired sessions (end <= now_dt) are silently dropped.
    Returns (active_session | None, [future_sessions]).
    """
    active = None
    future = []
    for s in sessions:
        start = _parse_dt(s['start'])
        end = _parse_dt(s['end'])
        if start <= now_dt < end:
            active = s
        elif end > now_dt:
            future.append(s)
    return active, future


def compute_hours_remaining(future_sessions, active_session, now_dt):
    """
    Total uncommenced committed charging time.
    Counts the remaining portion of the active session plus all future sessions in full.
    """
    total = 0.0
    if active_session:
        end = _parse_dt(active_session['end'])
        total += max(0.0, (end - now_dt).total_seconds() / 3600)
    for s in future_sessions:
        total += s.get('duration_hours', 0.0)
    return total


def determine_state(required_hours, boost_active,
                    active_session, future_sessions, hours_remaining, data_ok):
    """
    Return the appropriate STATE_* constant for the current conditions.
    charger_connected is intentionally NOT a parameter — the state reflects what
    is scheduled regardless of connection. desired is gated on connection separately
    in apply_schedule_outputs so the dashboard shows a live schedule even when the
    car is unplugged.
    """
    if not data_ok:
        return STATE_ERROR
    if required_hours is None or required_hours <= 0:
        return STATE_IDLE
    if boost_active:
        return STATE_BOOSTING
    if active_session:
        return STATE_CHARGING
    if future_sessions:
        return STATE_SCHEDULED
    if hours_remaining <= 0:
        return STATE_COMPLETE
    return STATE_ERROR


def build_schedule_attrs(sessions, active_session, future_sessions,
                         hours_remaining, inputs_snapshot, boost_end_dt, now_dt):
    """Build the attribute dict for sensor.ev_charging_schedule for dashboard use."""
    next_slot = future_sessions[0] if future_sessions else None
    return {
        'slots': sessions,
        'active_slot': active_session,
        'next_slot': next_slot,
        'total_committed_hours': sum([s.get('duration_hours', 0.0) for s in sessions]),
        'hours_remaining': round(hours_remaining, 2),
        'calculated_at': now_dt.isoformat(),
        'ready_by': inputs_snapshot.get('ready_by_dt'),
        'required_hours': inputs_snapshot.get('required_hours'),
        'boost_end_dt': boost_end_dt.isoformat() if boost_end_dt else None,
    }


def deduplicate_and_sort_prices(all_prices, now_dt):
    """
    Merge price data from all sources: actual rates win over predicted for the same slot.
    Discards slots that have already ended, sorts the rest chronologically.
    Returns list of dicts: {date_time, raw_price, source}.
    """
    prices_by_dt = {}
    for p in all_prices:
        dt = p['date_time']
        is_actual = p['source'] in ('current_actual', 'next_actual')
        existing = prices_by_dt.get(dt)
        if existing is None:
            prices_by_dt[dt] = p
        elif is_actual and existing['source'] not in ('current_actual', 'next_actual'):
            prices_by_dt[dt] = p

    result = []
    for dt in sorted(prices_by_dt.keys()):
        p = prices_by_dt[dt]
        if dt + timedelta(minutes=30) > now_dt:
            result.append(p)
    return result


# =============================================================================
# HA adapter class — all Home Assistant I/O in one place
# =============================================================================

class EVChargingHA:
    """
    Thin adapter that isolates all Home Assistant state I/O.
    All methods either read from or write to HA entities.
    No business logic lives here.
    """

    def get_now(self):
        return ha_now()

    def _read_state(self, entity_id):
        entity = hass.states.get(entity_id)
        if not entity or entity.state in ('unknown', 'unavailable', None, ''):
            return None
        return entity.state

    def _read_float(self, entity_id, default=None):
        val = self._read_state(entity_id)
        if val is None:
            return default
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    def _read_attrs(self, entity_id):
        entity = hass.states.get(entity_id)
        return getattr(entity, 'attributes', {}) if entity else {}

    def get_ready_by(self):
        """Return timezone-aware ready_by datetime, or None."""
        val = self._read_state(READY_BY_ENTITY)
        if val is None:
            return None
        try:
            return as_local(datetime.fromisoformat(val))
        except ValueError:
            _LOGGER.warning(f"Cannot parse ready_by: {val}")
            return None

    def get_required_hours(self):
        return self._read_float(CHARGING_HOURS_ENTITY)

    def get_gamble_tolerance(self):
        return self._read_float(GAMBLE_TOLERANCE_ENTITY, default=DEFAULT_GAMBLE_TOLERANCE)

    def get_min_block_hours(self):
        return self._read_float(MIN_BLOCK_HOURS_ENTITY, default=DEFAULT_MIN_BLOCK_HOURS)

    def get_max_price(self):
        return self._read_float(MAX_PRICE_ENTITY, default=DEFAULT_MAX_PRICE)

    def get_boost_duration(self):
        return self._read_float(BOOST_DURATION_ENTITY, default=0.0)

    def get_charger_work_state(self):
        return self._read_state(CHARGER_WORK_STATE_ENTITY)

    def _parse_rates(self, entity_id, source_label):
        """Parse Octopus rate event attributes into price slot dicts."""
        attrs = self._read_attrs(entity_id)
        rates = attrs.get('rates', [])
        slots = []
        for rate in rates:
            try:
                dt_val = rate.get('start')
                price_val = rate.get('value_inc_vat')
                if dt_val is None or price_val is None:
                    continue
                if isinstance(dt_val, str):
                    dt = as_local(_parse_iso_str(dt_val))
                elif isinstance(dt_val, datetime):
                    dt = as_local(dt_val)
                else:
                    continue
                # value_inc_vat is in £/kWh; convert to p/kWh for all internal calculations
                slots.append({'date_time': dt, 'raw_price': round(float(price_val) * 100, 4), 'source': source_label})
            except Exception as e:
                _LOGGER.warning(f"Skipping {source_label} rate: {e}")
        return slots

    def get_current_rates(self):
        return self._parse_rates(OCTOPUS_CURRENT_RATES_ENTITY, 'current_actual')

    def get_next_rates(self):
        return self._parse_rates(OCTOPUS_NEXT_RATES_ENTITY, 'next_actual')

    def get_agile_forecast_slots(self):
        """Return agile predicted price slots (raw_price in p/kWh)."""
        attrs = self._read_attrs(AGILE_FORECAST_ENTITY)
        predicted = attrs.get('prices', [])
        slots = []
        for point in predicted:
            try:
                dt_str = point.get('date_time')
                price_val = point.get('agile_pred')
                if dt_str is None or price_val is None:
                    continue
                dt = as_local(_parse_iso_str(dt_str))
                slots.append({'date_time': dt, 'raw_price': float(price_val), 'source': 'predicted'})
            except Exception as e:
                _LOGGER.warning(f"Skipping predicted price: {e}")
        return slots

    def get_stored_schedule(self):
        """Read persisted schedule from sensor attribute. Returns {} if not yet set."""
        attrs = self._read_attrs(SCHEDULE_SENSOR)
        stored = attrs.get('_schedule_data')
        if not stored:
            return {}
        try:
            if isinstance(stored, str):
                return json.loads(stored)
            if isinstance(stored, dict):
                return stored
        except (json.JSONDecodeError, TypeError):
            pass
        return {}

    def store_schedule(self, sessions, boost_end_dt, inputs, now_dt):
        """
        Persist schedule data by updating _schedule_data in the schedule sensor attribute.
        Preserves the sensor's current display state — only the stored data is changed.
        Used by boost_cancel to clear boost_end_dt before re-evaluation.
        """
        schedule_data = {
            'slots': sessions,
            'boost_end_dt': boost_end_dt.isoformat() if boost_end_dt else None,
            'inputs_snapshot': inputs,
            'stored_at': now_dt.isoformat(),
        }
        current_state = self._read_state(SCHEDULE_SENSOR) or 'idle'
        existing_attrs = dict(self._read_attrs(SCHEDULE_SENSOR))
        existing_attrs['_schedule_data'] = schedule_data
        state.set(SCHEDULE_SENSOR, current_state, existing_attrs)

    def reset_boost_duration(self):
        """
        Reset the boost duration slider to 0 after the boost has been registered.
        This prevents re-triggering a new boost each time the stored boost expires
        while the slider is still set to a non-zero value.
        """
        hass.services.call('input_number', 'set_value', {
            'entity_id': BOOST_DURATION_ENTITY,
            'value': 0,
        })

    def _schedule_fingerprint(self, slots):
        """Stable identity for a slot list — tuple of (start, end) pairs sorted by start."""
        return tuple(sorted([(s.get('start', ''), s.get('end', '')) for s in slots]))

    def set_desired(self, on):
        new_val = 'on' if on else 'off'
        if self._read_state(DESIRED_BINARY) == new_val:
            return
        state.set(DESIRED_BINARY, new_val, {
            'friendly_name': 'EV Charging Desired',
            'icon': 'mdi:ev-station' if on else 'mdi:power-off',
        })

    def set_state_sensor(self, state_name):
        if self._read_state(STATE_SENSOR) == state_name:
            return
        state.set(STATE_SENSOR, state_name, {
            'friendly_name': 'EV Charging State',
            'icon': 'mdi:state-machine',
        })

    def set_schedule_sensor(self, state_name, attrs):
        # Only write when state name, slot list, boost_end_dt, or error_reason has
        # meaningfully changed. Suppresses noise from calculated_at ticking every
        # 5 min when nothing else changed.
        # boost_end_dt must be in the comparison so that a boost cancel or extension
        # (same slots, different boost time) is not incorrectly suppressed.
        # error_reason must be compared too — e.g. an unschedulable reason can change
        # (required_hours shifts) while slots/boost_end_dt stay at their empty defaults,
        # and the dashboard banner would otherwise show stale text.
        current_attrs = self._read_attrs(SCHEDULE_SENSOR)
        current_state = self._read_state(SCHEDULE_SENSOR)
        current_slots = current_attrs.get('slots', [])
        new_slots = attrs.get('slots', [])
        if (current_state == state_name
                and self._schedule_fingerprint(current_slots) == self._schedule_fingerprint(new_slots)
                and current_attrs.get('boost_end_dt') == attrs.get('boost_end_dt')
                and current_attrs.get('error_reason') == attrs.get('error_reason')):
            return
        state.set(SCHEDULE_SENSOR, state_name, attrs)

    def set_next_slot(self, start_iso, end_iso):
        new_start = start_iso or 'none'
        new_end = end_iso or 'none'
        if (self._read_state(NEXT_SLOT_START_SENSOR) == new_start and
                self._read_state(NEXT_SLOT_END_SENSOR) == new_end):
            return
        state.set(NEXT_SLOT_START_SENSOR, new_start, {
            'friendly_name': 'EV Charging Next Slot Start',
            'icon': 'mdi:clock-start',
        })
        state.set(NEXT_SLOT_END_SENSOR, new_end, {
            'friendly_name': 'EV Charging Next Slot End',
            'icon': 'mdi:clock-end',
        })

    def set_hours_remaining(self, hours):
        rounded = round(hours, 2)
        try:
            current = float(self._read_state(HOURS_REMAINING_SENSOR) or -1)
            if abs(current - rounded) < 0.01:
                return
        except (ValueError, TypeError):
            pass
        state.set(HOURS_REMAINING_SENSOR, rounded, {
            'friendly_name': 'EV Charging Hours Remaining',
            'unit_of_measurement': 'h',
            'icon': 'mdi:timer-outline',
        })

    def set_all_unavailable(self, reason):
        # Only write when the error state or reason has actually changed. Without this
        # guard, a persistent error would rewrite every entity (with a fresh
        # calculated_at) on every evaluation — firing state_changed events that can
        # retrigger automations watching these entities and create a tight error loop.
        if (self._read_state(STATE_SENSOR) == STATE_ERROR
                and self._read_attrs(STATE_SENSOR).get('error_reason') == reason):
            return
        _LOGGER.warning(f"Setting EV charging sensors unavailable: {reason}")
        now_iso = ha_now().isoformat()
        err_attrs = {'error_reason': reason, 'calculated_at': now_iso}
        # Preserve any existing _schedule_data on the schedule sensor only so that
        # transient errors do not evict an active charging slot from persistence.
        # _schedule_data is only meaningful on SCHEDULE_SENSOR — don't broadcast it
        # to every other entity and waste HA attribute storage.
        existing_data = self._read_attrs(SCHEDULE_SENSOR).get('_schedule_data')
        schedule_err_attrs = dict(err_attrs)
        if existing_data:
            schedule_err_attrs['_schedule_data'] = existing_data
        state.set(DESIRED_BINARY, 'off', {**err_attrs, 'friendly_name': 'EV Charging Desired'})
        state.set(STATE_SENSOR, STATE_ERROR, {**err_attrs, 'friendly_name': 'EV Charging State'})
        state.set(SCHEDULE_SENSOR, STATE_ERROR, {**schedule_err_attrs, 'friendly_name': 'EV Charging Schedule'})
        state.set(NEXT_SLOT_START_SENSOR, 'unavailable', {**err_attrs})
        state.set(NEXT_SLOT_END_SENSOR, 'unavailable', {**err_attrs})
        state.set(HOURS_REMAINING_SENSOR, 0, {**err_attrs, 'unit_of_measurement': 'h'})


# =============================================================================
# Input / price collection — reads from HA, returns plain Python dicts
# =============================================================================

def collect_inputs(ha):
    """
    Read all required inputs from HA.
    Returns a plain dict on success, None if a critical input is missing.
    """
    ready_by = ha.get_ready_by()
    required_hours = ha.get_required_hours()
    if ready_by is None or required_hours is None:
        return None
    # Guard against NaN (some HA entities can report 'nan' as a float string).
    # NaN passes None checks and would crash int() conversion downstream.
    try:
        if required_hours != required_hours:  # NaN is not equal to itself
            _LOGGER.warning(f"required_hours is NaN — treating as missing")
            return None
    except TypeError:
        return None
    return {
        'ready_by_dt': ready_by.isoformat(),
        'required_hours': required_hours,
        'gamble_tolerance': ha.get_gamble_tolerance(),
        'min_block_hours': ha.get_min_block_hours(),
        'max_price': ha.get_max_price(),
        'boost_duration': ha.get_boost_duration(),
        'charger_work_state': ha.get_charger_work_state(),
    }


def collect_all_prices(ha):
    """Collect raw price slots from all sources (actual + predicted)."""
    current = ha.get_current_rates()
    next_day = ha.get_next_rates()
    predicted = ha.get_agile_forecast_slots()
    _LOGGER.debug(
        f"Prices collected: {len(current)} current-day actual, "
        f"{len(next_day)} next-day actual, {len(predicted)} predicted "
        f"= {len(current) + len(next_day) + len(predicted)} total"
    )
    if not current and not next_day:
        _LOGGER.warning("No actual Octopus rates — check entity IDs for current/next day rates")
    if not predicted:
        _LOGGER.warning(f"No predicted prices from {AGILE_FORECAST_ENTITY} — schedule limited to actual rates only")
    return current + next_day + predicted


# =============================================================================
# Scheduling logic — pure functions that compute what should happen
# =============================================================================

def resolve_boost_end(stored, inputs, now_dt):
    """
    Determine the effective boost end time, or None if no boost is active.
    Starts a new boost if boost_duration > 0 and no current boost is running.
    An existing stored boost is honoured until it expires.
    """
    stored_boost_end = None
    stored_raw = stored.get('boost_end_dt')
    if stored_raw:
        try:
            stored_boost_end = _parse_dt(stored_raw)
        except ValueError:
            pass

    boost_duration = inputs.get('boost_duration', 0.0)
    if boost_duration > 0 and (stored_boost_end is None or stored_boost_end <= now_dt):
        new_end = now_dt + timedelta(hours=boost_duration)
        _LOGGER.info(f"Boost started, ends at {new_end.isoformat()}")
        return new_end

    if stored_boost_end is not None and stored_boost_end > now_dt:
        return stored_boost_end

    return None


def compute_sessions(all_prices, stored_sessions, inputs, ready_by_dt, now_dt):
    """
    Compute the optimal charging session list from current price data.
    The currently-active session (if any) is always preserved unchanged.
    Future sessions are recomputed fresh from the latest prices on every call.
    Returns a list of session dicts sorted by start time.
    """
    _LOGGER.debug(
        f"compute_sessions: {inputs['required_hours']}h needed, "
        f"ready_by={ready_by_dt.strftime('%Y-%m-%d %H:%M')}, "
        f"gamble={inputs['gamble_tolerance']}, "
        f"max_price={inputs['max_price']}p, "
        f"min_block={inputs['min_block_hours']}h"
    )

    # Resolve the active session first — it must survive even if fresh price data
    # is unavailable this cycle (the early-return below must not discard it).
    active_session, _ = prune_and_classify(stored_sessions, now_dt)
    if active_session:
        _LOGGER.debug(
            f"  Active session: {active_session['start']} -> {active_session['end']} "
            f"({active_session.get('duration_hours', '?')}h) — PRESERVED"
        )

    now_prices = deduplicate_and_sort_prices(all_prices, now_dt)
    _LOGGER.debug(
        f"  After dedup + past-slot filter: {len(now_prices)} slots "
        f"(from {now_prices[0]['date_time'].strftime('%m-%d %H:%M') if now_prices else 'none'} "
        f"to {now_prices[-1]['date_time'].strftime('%m-%d %H:%M') if now_prices else 'none'})"
    )
    if not now_prices:
        _LOGGER.warning(
            "All available price slots are in the past — price data from all sources is stale. "
            "Check that current_day_rates, next_day_rates, and agile_forecast are updating."
        )
        return [active_session] if active_session else []

    adjusted = assign_credibilities(now_prices, now_dt, inputs['gamble_tolerance'])

    # gamble_tolerance <= 0 means the user wants no exposure to forecast risk at all —
    # price inflation alone doesn't guarantee that (a sufficiently cheap forecast can
    # still outrank an actual rate), so hard-exclude every non-actual tier here.
    if inputs['gamble_tolerance'] <= 0:
        before = len(adjusted)
        adjusted = [s for s in adjusted if s['tier'] == TIER_ACTUAL]
        _LOGGER.debug(
            f"  gamble_tolerance<=0: restricted to known/actual rates only "
            f"({len(adjusted)} of {before} slots remain)"
        )

    # Slots already claimed by the active session must be removed from the candidate
    # pool before optimising future sessions. Otherwise the optimizer can pick a window
    # that overlaps the active session's still-running portion — prune_and_classify then
    # treats that overlapping computed session as "active" instead of the real one,
    # silently replacing (evicting) it with a different start/end.
    candidate_slots = adjusted
    if active_session:
        active_start = _parse_dt(active_session['start'])
        active_end = _parse_dt(active_session['end'])
        candidate_slots = [
            s for s in adjusted
            if not (active_start <= s['date_time'] < active_end)
        ]

    required_slots = max(1, math.ceil(inputs['required_hours'] * 2))

    # Active session slots are already committed; subtract the entire session duration
    # from what we still need. Using the stored duration_hours avoids an integer-
    # truncation edge case where a slot that is nearly finished (e.g. 25 min elapsed,
    # 5 min remaining) would count as zero slots in both elapsed and remaining.
    slots_still_needed = required_slots
    if active_session:
        duration_h = active_session.get('duration_hours')
        if duration_h is None:
            # Legacy stored sessions may lack duration_hours; derive from start/end.
            try:
                duration_h = (_parse_dt(active_session['end']) - _parse_dt(active_session['start'])).total_seconds() / 3600
            except Exception:
                duration_h = 0.0
        active_total = math.ceil(duration_h * 2)
        slots_still_needed = max(0, required_slots - active_total)
        _LOGGER.debug(
            f"  Slots: need {required_slots} total, active covers {active_total}, "
            f"still need {slots_still_needed}"
        )
    else:
        _LOGGER.debug(f"  Slots needed: {required_slots} ({required_slots * 0.5:.1f}h), no active session")

    future_sessions = []
    if slots_still_needed > 0:
        future_slots = find_optimal_slots(
            candidate_slots, slots_still_needed, ready_by_dt, inputs['min_block_hours'],
            max_price=inputs['max_price'])
        future_sessions = slots_to_sessions(future_slots)
        if future_sessions:
            _LOGGER.debug(f"  Scheduled {len(future_sessions)} session(s):")
            for s in future_sessions:
                _LOGGER.debug(f"    {s['start']} -> {s['end']} ({s['duration_hours']}h)")
        else:
            _LOGGER.debug("  No future sessions found — see find_optimal_slots log for why")
    else:
        _LOGGER.debug("  No additional slots needed (active session covers requirement)")

    return ([active_session] if active_session else []) + future_sessions


# =============================================================================
# Output application — writes computed state to HA sensors
# =============================================================================

def _make_schedule_data(sessions, boost_end_dt, inputs, now_dt):
    """Build the _schedule_data dict that is persisted in the sensor attribute."""
    return {
        'slots': sessions,
        'boost_end_dt': boost_end_dt.isoformat() if boost_end_dt else None,
        'inputs_snapshot': inputs,
        'stored_at': now_dt.isoformat(),
    }


def apply_boosting_outputs(ha, stored_sessions, boost_end_dt, inputs, now_dt):
    """Write all HA sensors for the boosting state."""
    active, future = prune_and_classify(stored_sessions, now_dt)
    hours_remaining = compute_hours_remaining(future, active, now_dt)
    next_slot = future[0] if future else None

    _LOGGER.info(
        f"EV charging: boosting until {boost_end_dt.isoformat()}, "
        f"hours_remaining={hours_remaining:.1f}h"
    )

    schedule_attrs = build_schedule_attrs(
        stored_sessions, active, future, hours_remaining, inputs, boost_end_dt, now_dt)
    schedule_attrs['_schedule_data'] = _make_schedule_data(stored_sessions, boost_end_dt, inputs, now_dt)

    ha.set_desired(True)
    ha.set_state_sensor(STATE_BOOSTING)
    ha.set_hours_remaining(hours_remaining)
    ha.set_next_slot(
        next_slot['start'] if next_slot else None,
        next_slot['end'] if next_slot else None,
    )
    ha.set_schedule_sensor(STATE_BOOSTING, schedule_attrs)


def apply_idle_outputs(ha, inputs, now_dt):
    """Write all HA sensors for the idle state (required_hours = 0)."""
    _LOGGER.info("EV charging: idle (no charging hours required)")
    schedule_attrs = build_schedule_attrs([], None, [], 0.0, inputs, None, now_dt)
    # Write an explicit empty _schedule_data so any previously stored sessions
    # (including active ones from a mid-session required_hours=0 event) are cleared
    # rather than left as stale orphans that would be picked up on the next tick.
    schedule_attrs['_schedule_data'] = _make_schedule_data([], None, inputs, now_dt)

    ha.set_desired(False)
    ha.set_state_sensor(STATE_IDLE)
    ha.set_hours_remaining(0.0)
    ha.set_next_slot(None, None)
    ha.set_schedule_sensor(STATE_IDLE, schedule_attrs)


def apply_unschedulable_outputs(ha, required_hours, inputs, reason, now_dt):
    """
    Write all HA sensors for the unschedulable state: charging is still required, but
    no combination of available price slots satisfies max_price/min_block_hours within
    the ready_by window. This is an expected consequence of the user's own constraints
    versus the current price market — not a system fault — so it gets a distinct state
    rather than STATE_ERROR (which is reserved for genuine failures: missing inputs, no
    price data, stale data, etc).
    """
    _LOGGER.warning(f"EV charging: unschedulable — {reason}")
    schedule_attrs = build_schedule_attrs([], None, [], required_hours, inputs, None, now_dt)
    schedule_attrs['_schedule_data'] = _make_schedule_data([], None, inputs, now_dt)
    schedule_attrs['error_reason'] = reason

    ha.set_desired(False)
    ha.set_state_sensor(STATE_UNSCHEDULABLE)
    ha.set_hours_remaining(required_hours)
    ha.set_next_slot(None, None)
    ha.set_schedule_sensor(STATE_UNSCHEDULABLE, schedule_attrs)


def apply_schedule_outputs(ha, sessions, charger_connected, required_hours, inputs, now_dt):
    """Classify sessions, determine the correct state, and write all HA sensors."""
    active, future = prune_and_classify(sessions, now_dt)
    hours_remaining = compute_hours_remaining(future, active, now_dt)

    # No sessions and still have required hours means the user's own constraints
    # (max_price / min_block_hours / ready_by) rule out every slot combination —
    # report it as 'unschedulable' with the reason, not as a system 'error'.
    if not sessions and active is None and required_hours > 0:
        reason = (
            f"No slots satisfy constraints: {required_hours}h needed, "
            f"max_price={inputs.get('max_price')}p/kWh, "
            f"min_block_hours={inputs.get('min_block_hours')}h"
        )
        apply_unschedulable_outputs(ha, required_hours, inputs, reason, now_dt)
        return

    sm_state = determine_state(
        required_hours=required_hours,
        boost_active=False,
        active_session=active,
        future_sessions=future,
        hours_remaining=hours_remaining,
        data_ok=True,
    )

    next_slot = future[0] if future else None
    schedule_attrs = build_schedule_attrs(sessions, active, future, hours_remaining, inputs, None, now_dt)
    schedule_attrs['_schedule_data'] = _make_schedule_data(sessions, None, inputs, now_dt)

    # desired is on only when the car is physically connected AND it's a charging slot.
    # The state sensor shows the schedule regardless of connection so the dashboard
    # always reflects when charging would happen.
    ha.set_desired(charger_connected and sm_state == STATE_CHARGING)
    ha.set_state_sensor(sm_state)
    ha.set_hours_remaining(hours_remaining)
    ha.set_next_slot(
        next_slot['start'] if next_slot else None,
        next_slot['end'] if next_slot else None,
    )
    ha.set_schedule_sensor(sm_state, schedule_attrs)

    _LOGGER.info(
        f"EV charging: state={sm_state}, hours_remaining={hours_remaining:.1f}h, "
        f"{len(sessions)} session(s)"
    )
    for s in sessions:
        _LOGGER.debug(f"  Session: {s['start']} -> {s['end']} ({s['duration_hours']}h)")


# =============================================================================
# Services — thin orchestrators; all logic lives in the functions above
# =============================================================================

@service
def update_ev_charge_state():
    """Main EV charging state machine. Called by automations every 5 min and on input changes."""
    _LOGGER.debug("EV charging state machine: evaluating")
    ha = EVChargingHA()
    now_dt = ha.get_now()

    inputs = collect_inputs(ha)
    if inputs is None:
        ha.set_all_unavailable("Missing required inputs (ready_by or charging_hours)")
        return

    ready_by_dt = datetime.fromisoformat(inputs['ready_by_dt'])
    if ready_by_dt <= now_dt:
        ha.set_all_unavailable("ready_by time is not in the future")
        return

    stored = ha.get_stored_schedule()
    stored_sessions = stored.get('slots', [])
    boost_end_dt = resolve_boost_end(stored, inputs, now_dt)

    # If the slider triggered or extended a boost, reset it to 0 so that the boost
    # doesn't silently re-trigger after it expires.
    if boost_end_dt is not None and inputs['boost_duration'] > 0:
        ha.reset_boost_duration()

    if boost_end_dt is not None and boost_end_dt > now_dt:
        apply_boosting_outputs(ha, stored_sessions, boost_end_dt, inputs, now_dt)
        return

    charger_connected = inputs['charger_work_state'] in CHARGER_CONNECTED_STATES
    if inputs['required_hours'] <= 0:
        apply_idle_outputs(ha, inputs, now_dt)
        return

    all_prices = collect_all_prices(ha)
    if not all_prices:
        ha.set_all_unavailable("No price data available")
        return

    new_sessions = compute_sessions(all_prices, stored_sessions, inputs, ready_by_dt, now_dt)
    apply_schedule_outputs(ha, new_sessions, charger_connected, inputs['required_hours'], inputs, now_dt)


@service
def ev_charging_boost(duration_hours=2.0):
    """Start a charging boost for the given number of hours."""
    _LOGGER.info(f"Boost requested for {duration_hours} hours")
    ha = EVChargingHA()
    now_dt = ha.get_now()
    stored = ha.get_stored_schedule()
    stored_sessions = stored.get('slots', [])
    inputs_snapshot = stored.get('inputs_snapshot', {})
    boost_end_dt = now_dt + timedelta(hours=float(duration_hours))
    # Reset the slider so that when this boost expires the next evaluation
    # doesn't see a non-zero boost_duration and re-trigger a new boost.
    ha.reset_boost_duration()
    apply_boosting_outputs(ha, stored_sessions, boost_end_dt, inputs_snapshot, now_dt)
    _LOGGER.info(f"Boost active until {boost_end_dt.isoformat()}")


@service
def ev_charging_boost_cancel():
    """Cancel an active boost and re-evaluate the normal schedule."""
    _LOGGER.info("Boost cancel requested")
    ha = EVChargingHA()
    now_dt = ha.get_now()
    stored = ha.get_stored_schedule()
    stored_sessions = stored.get('slots', [])
    inputs_snapshot = stored.get('inputs_snapshot', {})
    # Persist with boost_end_dt cleared so the next evaluation sees no boost
    ha.store_schedule(stored_sessions, None, inputs_snapshot, now_dt)
    update_ev_charge_state()


@service
def ev_charging_stop():
    """Clear the entire charging schedule and set state to idle."""
    _LOGGER.info("EV charging stop requested — clearing schedule")
    ha = EVChargingHA()
    now_dt = ha.get_now()
    # Reset boost slider so a pending non-zero value doesn't restart a boost on next tick.
    ha.reset_boost_duration()
    empty_attrs = {
        'slots': [], 'active_slot': None, 'next_slot': None,
        'total_committed_hours': 0.0, 'hours_remaining': 0.0,
        'calculated_at': now_dt.isoformat(),
        '_schedule_data': _make_schedule_data([], None, {}, now_dt),
    }
    ha.set_desired(False)
    ha.set_state_sensor(STATE_IDLE)
    ha.set_hours_remaining(0.0)
    ha.set_next_slot(None, None)
    ha.set_schedule_sensor(STATE_IDLE, empty_attrs)
