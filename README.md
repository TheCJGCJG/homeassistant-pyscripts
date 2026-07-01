# Home Assistant PyScripts

PyScript modules for Home Assistant automation.

## Structure

```
src/
  ev_charging_state_machine.py       ← active
  legacy/
    agile_forecast_processor.py      ← superseded, kept for reference
    update_ev_charging_schedule.py   ← superseded, kept for reference

tests/
  test_ev_charging_state_machine.py  ← active (324 tests)
  legacy/
    test_agile_forecast_processor.py
    test_ev_charging_schedule.py

ev_charging_automation.yaml          ← HA automations
```

## Running Tests

Always use the shell script — do not run `pytest` directly:

```bash
./run-tests.sh
```

With coverage report:
```bash
./run-tests.sh --coverage
```

Other options:
```bash
./run-tests.sh --shell     # Interactive shell in container
make test-docker           # Alternative via Make
```

## Installation

1. Copy `src/ev_charging_state_machine.py` to your HA PyScript directory (typically `config/pyscript/`). Do **not** copy the `legacy/` files alongside it.
2. Create the required HA helper entities (see [Required HA Entities](#required-ha-entities) below).
3. Update the entity ID constants at the top of the script to match your setup (see [Customisation](#customisation) below).
4. Copy `samples/ev_charging_automation.yaml` into your HA automations directory and fill in the hardware-specific placeholders.

A Lovelace dashboard example is included at `samples/polestar-2-charging-dashboard.yaml` — it is Polestar 2 specific but the State / Schedule / Settings sections adapt to any EV.

---

## Customisation

The entity ID constants near the top of `ev_charging_state_machine.py` must be updated for your setup. At minimum you will need to change the Octopus Energy entities.

### Script constants

| Constant | Default | What to set it to |
|---|---|---|
| `OCTOPUS_CURRENT_RATES_ENTITY` | `event.octopus_energy_electricity_XXXXX_XXXXXX_current_day_rates` | Your meter's current-day rate event. Find it in HA under **Settings → Integrations → Octopus Energy**. The slug contains your MPAN and meter serial number. |
| `OCTOPUS_NEXT_RATES_ENTITY` | `event.octopus_energy_electricity_XXXXX_XXXXXX_next_day_rates` | Same integration, next-day rates event. |
| `CHARGING_HOURS_ENTITY` | `input_number.polestar_2_charging_hours_required` | An `input_number` holding how many hours to charge. The `polestar_2` prefix is just a naming convention — rename the entity in HA or create a new one and update this constant to match. |
| `CHARGER_WORK_STATE_ENTITY` | `sensor.car_charger_work_state` | The sensor from your EV charger that reports its operating state. |
| `AGILE_FORECAST_ENTITY` | `sensor.agile_forecast` | A sensor with a `prices` attribute containing `date_time` and `agile_pred` (p/kWh) entries. If you have no forecast source, remove `predicted` from `collect_all_prices` — the script works with Octopus actual rates only (today + tomorrow). |

### Charger connected states

`CHARGER_CONNECTED_STATES` is the set of state values from `CHARGER_WORK_STATE_ENTITY` that mean a car is physically plugged in:

```python
CHARGER_CONNECTED_STATES = {'charger_insert', 'charger_pause', 'charger_end', 'charger_charging', 'charger_wait'}
```

These values are specific to one charger brand. Check the states of your charger's sensor and update the set to match.

### Automation placeholders

In `samples/ev_charging_automation.yaml`, replace the following before importing:

| Placeholder | Replace with |
|---|---|
| `DEVICE_ID_HERE` | Your charger switch's device ID — find it in HA Developer Tools → States, or in the device URL. |
| `ENTITY_ID_HERE` | Your charger's switch entity (e.g. `switch.ev_charger`). |
| `SELECT_ENTITY_ID_HERE` | Your charger's mode selector entity (e.g. `select.car_charger_work_mode`), if it has one. Remove the `condition: device` blocks that reference it if your charger has no mode selector. |

The `option: Charger Now` condition in the enable/disable automations is also charger-specific — update it to match your charger's mode options, or remove those conditions entirely.

---

## EV Charging State Machine

`ev_charging_state_machine.py` schedules EV charging across cheap Octopus Agile windows.

- Selects **non-contiguous** charging blocks spread across multiple days
- Applies **gamble tolerance** to risk-adjust predicted prices vs known Octopus rates
- Enforces a **minimum block size** to prevent rapid charger cycling
- Protects the **currently-active block** from eviction on price updates, disconnection, or transient errors
- Shows the schedule even when the car is unplugged — `desired` is only gated on physical connection
- Output sensors only update when values actually change, reducing HA automation noise
- Supports a **boost mode** and a **stop** button

### States

| State | Meaning |
|---|---|
| `idle` | Charging hours set to 0 |
| `scheduled` | Slots computed, waiting for the next one to start |
| `charging` | Currently in a committed slot; `desired` is `on` if the car is physically connected |
| `boosting` | Manual boost override active |
| `complete` | All committed slots have elapsed (may indicate missed window if car was disconnected) |
| `error` | Missing `ready_by`, no price data, or constraints prevent any schedule |

**Disconnection behaviour:** unplugging mid-session (`charger_free`) sets `desired` to `off` but
keeps the state as `charging`/`scheduled` and preserves the stored schedule. The schedule is
always visible on the dashboard regardless of connection state. If price data is temporarily
unavailable, the active session is also preserved through the error state.

### Price Data Sources

The script merges three sources, deduplicating by datetime (actual rates always win over forecast):

| Source entity | Type | Units |
|---|---|---|
| `event.octopus_energy_electricity_..._current_day_rates` | `value_inc_vat` in **£/kWh** (converted × 100 internally to p/kWh) | actual |
| `event.octopus_energy_electricity_..._next_day_rates` | `value_inc_vat` in **£/kWh** (converted × 100 internally to p/kWh) | actual |
| `sensor.agile_forecast` (`agile_pred` attribute) | already in **p/kWh** — no conversion | predicted |

### Scheduling Algorithm

Slots are selected using a sliding-window approach:

1. All candidate windows of `>= min_block_hours` are enumerated from the price data
2. Each window is scored by **weighted average effective price** (raw price adjusted by credibility and gamble tolerance)
3. Windows whose average raw price exceeds `max_price` are excluded
4. The cheapest non-overlapping combination of windows that totals `required_hours` is selected greedily
5. If no exact strict-block combination exists, the last block may be slightly shorter than `min_block_hours`

`max_price` is a **per-window average ceiling**, not per-slot. A window averaging 15p/kWh qualifies
at `max_price=18p` even if one slot inside it hits 20p. This allows cheap afternoon windows to
remain eligible even when the same continuous price stream includes expensive evening peaks.

`required_hours` and `min_block_hours` are rounded **up** to the nearest 30-minute slot boundary
(e.g. 0.75h → 2 slots = 1h; 1.25h → 3 slots = 1.5h).

### Required HA Entities

**Schedule inputs — create these helpers (or adapt entity names to match existing ones):**
- `input_datetime.ev_charger_ready_by` — when charging must be complete by
- `input_number.polestar_2_charging_hours_required` — hours of charging needed; the `polestar_2` prefix is just a naming convention, update `CHARGING_HOURS_ENTITY` in the script if you use a different name

**New helpers — add to `configuration.yaml` or create via HA Helpers UI:**

```yaml
input_number:
  ev_charging_gamble_tolerance:
    name: EV Charging Gamble Tolerance
    min: 0
    max: 100
    step: 5
    initial: 50
    unit_of_measurement: "%"
    icon: mdi:dice-multiple
    mode: slider

  ev_charging_min_block_hours:
    name: EV Charging Min Block Hours
    min: 0.5
    max: 4.0
    step: 0.5
    initial: 1.0
    unit_of_measurement: "h"
    icon: mdi:timer-outline
    mode: slider

  ev_charging_max_price:
    name: EV Charging Max Price
    min: 5
    max: 100
    step: 1
    initial: 20
    unit_of_measurement: "p/kWh"
    icon: mdi:currency-gbp
    mode: box

  ev_charging_boost_duration_hours:
    name: EV Charging Boost Duration
    min: 0.0
    max: 8.0
    step: 0.5
    initial: 0.0
    unit_of_measurement: "h"
    icon: mdi:lightning-bolt
    mode: slider

input_button:
  ev_charging_boost_cancel:
    name: Cancel EV Charging Boost
    icon: mdi:cancel

  ev_charging_stop:
    name: Stop EV Charging
    icon: mdi:stop-circle
```

### Output Entities

| Entity | Description |
|---|---|
| `binary_sensor.ev_charging_desired` | `on` when the charger should be running (car connected + in slot or boosting) |
| `sensor.ev_charging_state` | Current state name |
| `sensor.ev_charging_schedule` | Schedule state; `slots` attribute holds the full session list |
| `sensor.ev_charging_next_slot_start` | ISO datetime of the next scheduled slot start |
| `sensor.ev_charging_next_slot_end` | ISO datetime of the next scheduled slot end |
| `sensor.ev_charging_hours_remaining` | Hours of uncommenced committed charging |

All output sensors are only written when their value changes, so HA automations triggered
by these entities will not fire spuriously on every 5-minute evaluation cycle.

### Schedule Slot Format

Each entry in the `slots` attribute of `sensor.ev_charging_schedule`:

```json
{
  "start": "2026-06-03T11:00:00",
  "end":   "2026-06-03T15:00:00",
  "duration_hours": 4.0,
  "avg_price": 14.52,
  "confidence": 75.0
}
```

| Field | Description |
|---|---|
| `avg_price` | Average raw price of the session in p/kWh |
| `confidence` | Average base credibility of the price data (0–100). 100 = all Octopus actual rates (fixed price); lower values indicate reliance on predictions further ahead in time. |

**Confidence anchor points:**

| Value | Source |
|---|---|
| 100 | All Octopus actual rates (today / tomorrow) — price is fixed |
| 90 | Near-term predictions (< 24 h ahead) — usually accurate |
| 75 | 24–48 h predictions |
| 60 | 48–72 h predictions |
| 40 | > 72 h predictions — rough guide only |

Mixed sessions land between these values.

### Gamble Tolerance

Controls how much predicted prices are inflated relative to actual rates when ranking slots.
At `gamble_tolerance=0`, far-future predictions are treated as ~2.5× their nominal price,
strongly favouring known Octopus slots. At 100, all prices are taken at face value.

| Tolerance | Effect |
|---|---|
| 0 | Strongly favours actual rates; predictions heavily penalised |
| 50 | Moderate discount on predictions (default) |
| 100 | All prices at face value regardless of source |

Credibility tiers by time horizon:

| Source | Base credibility |
|---|---|
| Octopus current / next day actual rates | 1.00 |
| Predicted, ≤ 24 h ahead | 0.90 |
| Predicted, 24–48 h ahead | 0.75 |
| Predicted, 48–72 h ahead | 0.60 |
| Predicted, > 72 h ahead | 0.40 |

### Boost Mode

Set `ev_charging_boost_duration_hours` > 0 to start an immediate boost for that many hours.
The slider resets to 0 automatically once the boost is registered — this prevents the boost
from silently re-triggering after it expires while the slider is still set.

- Press `input_button.ev_charging_boost_cancel` to cancel early; the normal schedule resumes
- Press `input_button.ev_charging_stop` to clear the entire schedule, cancel any boost, and return to idle

**Note:** if `ev_charging_boost_duration_hours` is non-zero when `Stop` is pressed, the slider
reset is issued as an async HA service call. In the rare case that the next automation tick fires
before HA processes the reset, the boost may briefly re-activate. A second press of Stop will
clear it.

### Automations

Include `ev_charging_automation.yaml` in your HA configuration. It wires up:

1. State machine trigger (price updates, input changes, every 5 min, 16:15 daily)
2. Charger enable/disable based on `binary_sensor.ev_charging_desired`
3. Boost cancel button → `pyscript.ev_charging_boost_cancel`
4. Stop button → `pyscript.ev_charging_stop`

### Services

| Service | Description |
|---|---|
| `pyscript.update_ev_charge_state` | Main evaluation — called by automations |
| `pyscript.ev_charging_boost(duration_hours=2)` | Start boost directly |
| `pyscript.ev_charging_boost_cancel` | Cancel active boost; resumes normal schedule |
| `pyscript.ev_charging_stop` | Clear schedule and any active boost; set state to idle |

### HA Restart Behaviour

The schedule is persisted in the `_schedule_data` attribute of `sensor.ev_charging_schedule`.
On HA restart, this attribute is restored from the database and the first post-restart evaluation
will find and preserve any active session that was in progress.

The schedule data survives JSON serialisation/deserialisation intact — all field types
(ISO datetime strings, floats, dicts) round-trip correctly through HA's state storage.

### Debugging

Set `_DEBUG_LOG_ENABLED = True` near the top of `ev_charging_state_machine.py` to write
DEBUG-level output to `/config/pyscript/ev_charging_debug.log`. Set it back to `False`
when done — the flag is `False` by default.

The script is compatible with Python 3.10 and 3.11+ (handles both `+00:00` and `Z`
UTC suffix formats in Octopus rate timestamps).
