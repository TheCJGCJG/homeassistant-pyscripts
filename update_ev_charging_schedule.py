# pyscript/ev_charging_schedule.py
from datetime import datetime, timedelta
import logging
from homeassistant.util.dt import as_local, now as ha_now

_LOGGER = logging.getLogger(__name__)

# --- Configuration Constants ---
READY_BY_INPUT_DATETIME_ENTITY_ID = 'input_datetime.ev_charger_ready_by'
OCTOPUS_CURRENT_RATES_ENTITY_ID = 'event.octopus_energy_electricity_XXX_XXX_current_day_rates'
OCTOPUS_NEXT_RATES_ENTITY_ID = 'event.octopus_energy_electricity_XXX_XXX_next_day_rates'
AGILE_PREDICT_SENSOR_ENTITY_ID = 'sensor.agile_predict'
CHARGING_HOURS_INPUT_NUMBER_ENTITY_ID = 'input_number.car_charging_hours_required'

# --- Output Sensor Entity IDs ---
CHEAPEST_START_TIME_SENSOR = 'sensor.ev_charging_cheapest_start_time'
CHEAPEST_END_TIME_SENSOR = 'sensor.ev_charging_cheapest_end_time'
CHEAPEST_COST_SENSOR = 'sensor.ev_charging_cheapest_cost'
IS_CHEAPEST_PERIOD_BINARY_SENSOR = 'binary_sensor.ev_charging_is_cheapest_period'

# --- Logging Configuration ---
DETAILED_LOG_SAMPLE_SIZE = 5  # Set to None or 0 to disable detailed logging

def get_datetime_from_rate(dt_value):
    """Converts a string or datetime object to a timezone-aware datetime."""
    if isinstance(dt_value, str):
        try:
            dt_obj = datetime.fromisoformat(dt_value)
            return as_local(dt_obj)
        except ValueError:
            _LOGGER.warning(f"Could not parse datetime string: {dt_value}")
            return None
    elif isinstance(dt_value, datetime):
        return as_local(dt_value)
    else:
        _LOGGER.warning(f"Invalid datetime value type: {type(dt_value)}")
        return None

def get_ready_by_datetime():
    """Get the 'ready by' datetime from the input datetime entity."""
    try:
        ready_by_state = hass.states.get(READY_BY_INPUT_DATETIME_ENTITY_ID)
        if not ready_by_state or ready_by_state.state in ['unknown', 'unavailable', None]:
            _LOGGER.warning(f"Input datetime entity {READY_BY_INPUT_DATETIME_ENTITY_ID} is unavailable.")
            return None
        
        ready_by_state_str = ready_by_state.state
        if ready_by_state_str is None:
            _LOGGER.warning(f"Input datetime entity {READY_BY_INPUT_DATETIME_ENTITY_ID} state is None.")
            return None
        
        ready_by_naive = datetime.fromisoformat(ready_by_state_str)
        ready_by_dt = as_local(ready_by_naive)
        _LOGGER.debug(f"EV Ready By time: {ready_by_dt.isoformat()}")
        return ready_by_dt
    except ValueError as ve:
        _LOGGER.error(f"Could not parse input_datetime state: {ve}")
        return None
    except Exception as e:
        _LOGGER.error(f"Error reading ready by datetime: {e}")
        return None

def get_required_charging_slots():
    """Calculate required charging slots from hours input."""
    try:
        hours_state = hass.states.get(CHARGING_HOURS_INPUT_NUMBER_ENTITY_ID)
        if not hours_state or hours_state.state in ['unknown', 'unavailable', None, '']:
            _LOGGER.warning(f"Input number entity {CHARGING_HOURS_INPUT_NUMBER_ENTITY_ID} is unavailable.")
            return None
        
        required_hours = float(hours_state.state)
        required_slots = int(required_hours * 2)  # 2 slots per hour (30-min intervals)
        
        if required_slots <= 0:
            _LOGGER.warning(f"Calculated required charging slots ({required_slots}) is not positive.")
            return None
            
        _LOGGER.info(f"Required charging slots: {required_slots} (from {required_hours} hours)")
        return required_slots
    except (ValueError, TypeError) as ve:
        _LOGGER.error(f"Could not convert '{hours_state.state}' to a number: {ve}")
        return None
    except Exception as e:
        _LOGGER.error(f"Error reading charging hours: {e}")
        return None

def get_price_data():
    """Collect price data from all sources."""
    all_prices = []
    
    # Get Current Day Prices
    attributes = getattr(hass.states.get(OCTOPUS_CURRENT_RATES_ENTITY_ID), 'attributes', {})
    rates = attributes.get('rates', [])
    _LOGGER.debug(f"Found {len(rates)} current day rates")
    
    for rate in rates:
        try:
            start_dt = get_datetime_from_rate(rate.get('start'))
            price_value = rate.get('value_inc_vat')
            
            if start_dt and price_value is not None:
                price_entry = {}
                price_entry['date_time'] = start_dt
                price_entry['price'] = float(price_value)
                price_entry['source'] = 'current_actual'
                all_prices.append(price_entry)
        except Exception as e:
            _LOGGER.warning(f"Skipping current day rate: {e}")
    
    # Get Next Day Prices
    attributes = getattr(hass.states.get(OCTOPUS_NEXT_RATES_ENTITY_ID), 'attributes', {})
    rates = attributes.get('rates', [])
    _LOGGER.debug(f"Found {len(rates)} next day rates")
    
    for rate in rates:
        try:
            start_dt = get_datetime_from_rate(rate.get('start'))
            price_value = rate.get('value_inc_vat')
            
            if start_dt and price_value is not None:
                price_entry = {}
                price_entry['date_time'] = start_dt
                price_entry['price'] = float(price_value)
                price_entry['source'] = 'next_actual'
                all_prices.append(price_entry)
        except Exception as e:
            _LOGGER.warning(f"Skipping next day rate: {e}")
    
    # Get Predicted Prices
    attributes = getattr(hass.states.get(AGILE_PREDICT_SENSOR_ENTITY_ID), 'attributes', {})
    predicted_prices = attributes.get('prices', [])
    _LOGGER.debug(f"Found {len(predicted_prices)} predicted prices")
    
    for point in predicted_prices:
        try:
            dt_str = point.get('date_time')
            price_value = point.get('agile_pred')
            
            if dt_str and price_value is not None:
                dt_obj = as_local(datetime.fromisoformat(dt_str))
                # Convert from p/kWh to £/kWh
                price_in_pounds = float(price_value) / 100.0
                
                price_entry = {}
                price_entry['date_time'] = dt_obj
                price_entry['price'] = price_in_pounds
                price_entry['source'] = 'predicted'
                all_prices.append(price_entry)
        except Exception as e:
            _LOGGER.warning(f"Skipping predicted price: {e}")
            
    return all_prices

def process_price_data(all_prices, now_dt):
    """Process price data: deduplicate, sort, filter."""
    # Deduplicate with priority to actual prices
    prices_dict = {}
    for price_point in all_prices:
        dt = price_point['date_time']
        priority = 0 if price_point['source'] != 'predicted' else 1
        
        if dt not in prices_dict or prices_dict[dt]['priority'] > priority:
            prices_dict[dt] = {
                'price': price_point['price'],
                'source': price_point['source'],
                'priority': priority
            }
    
    # Convert to sorted list
    combined = []
    for dt in sorted(prices_dict.keys()):
        info = prices_dict[dt]
        price_entry = {}
        price_entry['date_time'] = dt
        price_entry['price'] = info['price']
        price_entry['source'] = info['source']
        combined.append(price_entry)
    
    _LOGGER.debug(f"Combined {len(combined)} unique price points")
    
    # Filter to future prices (include current slot if end time is after now)
    future_prices = []
    for p in combined:
        slot_end_dt = p['date_time'] + timedelta(minutes=30)
        if slot_end_dt > now_dt:
            future_prices.append(p)
    
    _LOGGER.debug(f"Filtered to {len(future_prices)} future price points")
    return future_prices

def find_cheapest_block(prices, required_slots, ready_by_dt):
    """Find the cheapest contiguous block that ends before ready_by time."""
    if len(prices) < required_slots:
        _LOGGER.warning("Not enough price data for required slots")
        return None
    
    # Find max valid start index (block must end before ready_by)
    max_valid_idx = -1
    for i in range(len(prices) - required_slots + 1):
        block_end = prices[i + required_slots - 1]['date_time'] + timedelta(minutes=30)
        if block_end <= ready_by_dt:
            max_valid_idx = i
        else:
            break
    
    if max_valid_idx == -1:
        _LOGGER.warning("No blocks end before ready_by time")
        return None
    
    # Get valid prices to search
    search_prices = prices[:max_valid_idx + required_slots]
    
    # Find cheapest block
    min_cost = float('inf')
    best_idx = -1
    
    for i in range(len(search_prices) - required_slots + 1):
        # Calculate block cost using traditional loop
        cost = 0
        for j in range(required_slots):
            cost += search_prices[i + j]['price']
        
        # Log detailed info for sample blocks
        should_log = False
        if DETAILED_LOG_SAMPLE_SIZE:
            if i < DETAILED_LOG_SAMPLE_SIZE:
                should_log = True
            elif i >= len(search_prices) - required_slots + 1 - DETAILED_LOG_SAMPLE_SIZE:
                should_log = True
        
        if should_log:
            _LOGGER.debug(f"Block {i+1}: Start={search_prices[i]['date_time'].isoformat()}, Cost={cost:.4f}")
            
        if cost < min_cost:
            min_cost = cost
            best_idx = i
            _LOGGER.debug(f"New cheapest block found: index={i}, cost={cost:.4f}")
    
    if best_idx == -1:
        return None
    
    # Create charging block
    block = {}
    block['start_dt'] = search_prices[best_idx]['date_time']
    block['end_dt'] = search_prices[best_idx + required_slots - 1]['date_time'] + timedelta(minutes=30)
    block['avg_cost'] = round(min_cost / required_slots, 4)
    block['total_cost'] = round(min_cost, 4)
    block['num_slots'] = required_slots
    
    return block

def update_sensors(block, ready_by_dt, now_dt):
    """Update Home Assistant sensors with charging block data."""
    _LOGGER.info(f"Cheapest block: Start={block['start_dt'].isoformat()}, "
                f"End={block['end_dt'].isoformat()}, Avg Cost={block['avg_cost']}")
    
    unit = '£/kWh'
    
    # Create base attributes dict manually instead of using dict unpacking
    base_attrs = {
        'cheapest_period_start': block['start_dt'].isoformat(),
        'cheapest_period_end': block['end_dt'].isoformat(),
        'cheapest_period_avg_cost': block['avg_cost'],
        'cheapest_period_total_cost': block['total_cost'],
        'unit_of_measurement': unit,
        'number_of_slots': block['num_slots'],
        'ready_by_time': ready_by_dt.isoformat(),
        'calculated_at': now_dt.isoformat()
    }
    
    try:
        # Update start time sensor
        start_attrs = {
            'friendly_name': 'EV Charging Cheapest Start Time',
            'icon': 'mdi:clock-start'
        }
        # Add all base attributes
        for k, v in base_attrs.items():
            start_attrs[k] = v
            
        state.set(
            CHEAPEST_START_TIME_SENSOR,
            block['start_dt'].isoformat(),
            attributes=start_attrs
        )
        
        # Update end time sensor
        end_attrs = {
            'friendly_name': 'EV Charging Cheapest End Time',
            'icon': 'mdi:clock-end'
        }
        for k, v in base_attrs.items():
            end_attrs[k] = v
            
        state.set(
            CHEAPEST_END_TIME_SENSOR,
            block['end_dt'].isoformat(),
            attributes=end_attrs
        )
        
        # Update cost sensor
        cost_attrs = {
            'friendly_name': 'EV Charging Cheapest Block Avg Cost',
            'icon': 'mdi:currency-gbp'
        }
        for k, v in base_attrs.items():
            cost_attrs[k] = v
            
        state.set(
            CHEAPEST_COST_SENSOR,
            block['avg_cost'],
            attributes=cost_attrs
        )
        
        # Update binary sensor
        is_cheapest_now = False
        if now_dt >= block['start_dt'] and now_dt < block['end_dt']:
            is_cheapest_now = True
            
        binary_attrs = {
            'friendly_name': 'EV Charging Is Cheapest Period',
            'icon': 'mdi:ev-station' if is_cheapest_now else 'mdi:power-off'
        }
        for k, v in base_attrs.items():
            binary_attrs[k] = v
            
        state.set(
            IS_CHEAPEST_PERIOD_BINARY_SENSOR,
            'on' if is_cheapest_now else 'off',
            attributes=binary_attrs
        )
    except Exception as e:
        _LOGGER.error(f"Error setting output sensors: {e}")
        set_unavailable(f"Error updating sensors: {e}")

def set_unavailable(reason="Unknown error"):
    """Set all sensors to unavailable state."""
    now_iso = ha_now().isoformat()
    _LOGGER.warning(f"Setting sensors to unavailable: {reason}")
    
    try:
        # Set standard sensors to unavailable
        entities = [
            (CHEAPEST_START_TIME_SENSOR, 'Start Time', 'mdi:clock-start', None),
            (CHEAPEST_END_TIME_SENSOR, 'End Time', 'mdi:clock-end', None),
            (CHEAPEST_COST_SENSOR, 'Block Avg Cost', 'mdi:currency-gbp', '£/kWh')
        ]
        
        for entity_id, name, icon, unit in entities:
            attrs = {
                'friendly_name': f'EV Charging Cheapest {name} (Unavailable)',
                'icon': icon,
                'error_reason': reason,
                'calculated_at': now_iso
            }
            
            # Add unit only if specified
            if unit:
                attrs['unit_of_measurement'] = unit
                
            state.set(entity_id, 'unavailable', attributes=attrs)
        
        # Set binary sensor to off
        state.set(
            IS_CHEAPEST_PERIOD_BINARY_SENSOR, 
            'off',
            attributes={
                'friendly_name': 'EV Charging Is Cheapest Period (Unavailable)',
                'icon': 'mdi:power-off',
                'error_reason': reason,
                'calculated_at': now_iso
            }
        )
    except Exception as e:
        _LOGGER.error(f"Failed to set unavailable state: {e}")

@service
def update_ev_charging_schedule():
    """Find cheapest charging block between now and 'ready by' time."""
    _LOGGER.info("Starting EV charging schedule update")
    now_dt = ha_now()
    
    # Check if we're in a charging session
    try:
        sensor = hass.states.get(CHEAPEST_START_TIME_SENSOR)
        if sensor and sensor.state != 'unavailable':
            attrs = sensor.attributes
            
            # Check if we're in a charging session
            start_time = datetime.fromisoformat(attrs.get('cheapest_period_start'))
            end_time = datetime.fromisoformat(attrs.get('cheapest_period_end'))
            in_session = start_time <= now_dt < end_time
            
            if in_session:
                _LOGGER.debug(f"Currently in charging session: {start_time} to {end_time}")
                
                # Get values used in last calculation
                last_ready_by = attrs.get('ready_by_time')
                last_slots = attrs.get('number_of_slots')
                
                # Get current values
                ready_by_state = hass.states.get(READY_BY_INPUT_DATETIME_ENTITY_ID)
                current_ready_by = ready_by_state.state
                
                hours_state = hass.states.get(CHARGING_HOURS_INPUT_NUMBER_ENTITY_ID)
                current_hours = float(hours_state.state)
                current_slots = int(current_hours * 2)  # Convert to slots
                
                # Check if values differ
                ready_by_changed = current_ready_by != last_ready_by
                slots_changed = current_slots != last_slots
                
                # If values have changed, check if they changed recently
                if ready_by_changed or slots_changed:
                    one_minute_ago = now_dt - timedelta(minutes=1)
                    ready_by_changed_recently = ready_by_changed and as_local(ready_by_state.last_changed) > one_minute_ago
                    hours_changed_recently = slots_changed and as_local(hours_state.last_changed) > one_minute_ago
                    
                    if ready_by_changed_recently or hours_changed_recently:
                        if ready_by_changed_recently:
                            _LOGGER.info(f"Ready-by time changed recently: {last_ready_by} -> {current_ready_by}")
                        if hours_changed_recently:
                            _LOGGER.info(f"Charging hours changed recently: {last_slots/2} -> {current_hours}")
                    else:
                        _LOGGER.info("Inputs changed but not recently, keeping existing schedule")
                        return
                else:
                    _LOGGER.info("In charging session with unchanged inputs, keeping existing schedule")
                    return
    except Exception as e:
        _LOGGER.warning(f"Error checking charging session: {e}")
    
    # Get required inputs
    ready_by_dt = get_ready_by_datetime()
    if not ready_by_dt:
        set_unavailable("Invalid 'Ready By' time")
        return
        
    if ready_by_dt <= now_dt:
        set_unavailable("'Ready By' time is not in the future")
        return
    
    required_slots = get_required_charging_slots()
    if not required_slots:
        set_unavailable("Invalid required charging hours")
        return
    
    # Get and process price data
    all_prices = get_price_data()
    if not all_prices:
        set_unavailable("No price data available")
        return
        
    future_prices = process_price_data(all_prices, now_dt)
    if len(future_prices) < required_slots:
        set_unavailable(f"Not enough future price data ({len(future_prices)} < {required_slots})")
        return
    
    # Find cheapest block
    charging_block = find_cheapest_block(future_prices, required_slots, ready_by_dt)
    if not charging_block:
        set_unavailable("Could not find valid charging block")
        return
    
    # Update sensors
    update_sensors(charging_block, ready_by_dt, now_dt)
    _LOGGER.info("EV charging schedule update completed successfully")