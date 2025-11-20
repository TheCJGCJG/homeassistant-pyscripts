# pyscript/agile_forecast_processor.py
"""
Process Agile electricity price forecasts into time blocks for Home Assistant.

This script fetches price data from the Agile predict sensor, categorizes prices
into meaningful time blocks, and creates forecast sensors for different periods.
"""
from datetime import datetime, time, date, timedelta
import collections
import logging
# Import HA timezone utility
from homeassistant.util.dt import get_time_zone, as_local, now as ha_now

# Get the PyScript logger
_LOGGER = logging.getLogger(__name__)

# Define the time block boundaries
# Nighttime crosses midnight, so it requires special handling
TIME_BLOCK_RANGES = [
    ('Nighttime', time(23, 0), time(6, 0)),   # 23:00:00 to 05:59:59
    ('Morning', time(6, 0), time(12, 0)),     # 06:00:00 to 11:59:59
    ('Afternoon', time(12, 0), time(16, 0)),  # 12:00:00 to 15:59:59
    ('Peak', time(16, 0), time(20, 0)),       # 16:00:00 to 19:59:59
    ('Evening', time(20, 0), time(23, 0)),    # 20:00:00 to 22:59:59
]

# Standard suffixes for all sensors managed by this script
SENSOR_SUFFIXES = [
    'agile_forecast_24_48h',
    'agile_forecast_48_72h',
    'agile_forecast_72_96h',
    'agile_forecast_96_120h',
    'agile_forecast_120_144h',
]


def get_time_block_info(dt):
    """
    Determines the time block name and effective date for the given datetime.
    
    Args:
        dt (datetime): A timezone-aware datetime object
        
    Returns:
        tuple: (block_name, effective_date) or (None, None) if no match
    """
    t = dt.time()
    d = dt.date()

    # Handle Nighttime block which crosses midnight
    if t >= time(23, 0):
        # 23:00 onwards belongs to Nighttime block starting today
        return 'Nighttime', d
    elif t < time(6, 0):
        # 00:00 to 05:59 belongs to Nighttime block from yesterday
        return 'Nighttime', d - timedelta(days=1)
    
    # Handle other blocks using simple time comparisons
    elif t >= time(20, 0):
        return 'Evening', d
    elif t >= time(16, 0):
        return 'Peak', d
    elif t >= time(12, 0):
        return 'Afternoon', d
    elif t >= time(6, 0):
        return 'Morning', d
    
    # Should never happen with the defined ranges
    _LOGGER.warning(f"Time {t} did not match any block! Input datetime: {dt.isoformat()}")
    return None, None


def set_sensors_unavailable(reason, source_entity="sensor.agile_predict"):
    """
    Sets all forecast sensors to unavailable with appropriate attributes.
    
    Args:
        reason (str): Reason for setting to unavailable (for logging)
        source_entity (str): Source entity ID
    """
    _LOGGER.warning(f"Setting forecast sensors to unavailable: {reason}")
    
    for suffix in SENSOR_SUFFIXES:
        forecast_entity_id = f"sensor.{suffix}"
        parts = suffix.split('_')
        if len(parts) >= 4:
            hours_range = f"{parts[2]}-{parts[3]}"
            friendly_name = f"Agile Forecast {hours_range} Hours"
        else:
            friendly_name = f"Agile Forecast {suffix}"
            
        attrs = {}
        attrs['friendly_name'] = friendly_name
        attrs['icon'] = 'mdi:currency-gbp'
        attrs['source_entity'] = source_entity
        
        try:
            state.set(forecast_entity_id, 'unavailable', attrs)
            _LOGGER.debug(f"Set {forecast_entity_id} to 'unavailable'")
        except Exception as e:
            _LOGGER.error(f"Failed to set {forecast_entity_id} to 'unavailable': {e}")


@service
def update_agile_forecasts():
    """
    Updates Agile price forecast sensors based on time blocks.
    
    This service:
    1. Fetches current Agile price forecasts
    2. Aggregates prices into daily time blocks
    3. Groups blocks into 24-hour periods starting at 16:00
    4. Updates corresponding Home Assistant sensors
    """
    _LOGGER.info("Starting Agile forecast update")
    entity_id = 'sensor.agile_predict'

    # Get Home Assistant's current time (timezone-aware)
    try:
        now = ha_now()
        _LOGGER.debug(f"Current HA time: {now.isoformat()}")
    except Exception as e:
        _LOGGER.error(f"Failed to get HA current time: {e}")
        set_sensors_unavailable("Time retrieval error")
        return

    # Get the Agile sensor state
    agile_sensor_state_obj = hass.states.get(entity_id)
    if not agile_sensor_state_obj:
        _LOGGER.error(f"Source entity {entity_id} not found")
        set_sensors_unavailable("Source entity not found")
        return

    # Get price data from attributes
    all_attributes = getattr(agile_sensor_state_obj, 'attributes', {})
    prices_data = all_attributes.get('prices')

    # Validate price data
    if not prices_data or not isinstance(prices_data, list):
        _LOGGER.error(f"Invalid price data from {entity_id}: {type(prices_data).__name__}")
        set_sensors_unavailable("Invalid price data")
        return

    # Process price data into blocks
    block_prices = collections.defaultdict(list)
    _LOGGER.info(f"Processing {len(prices_data)} price points")

    for point in prices_data:
        try:
            dt_str = point.get('date_time')
            price = point.get('agile_pred')
            
            if dt_str is None or price is None:
                _LOGGER.debug(f"Skipping price point with missing data: {point}")
                continue

            # Parse and localize datetime
            dt_obj_parsed = datetime.fromisoformat(dt_str)
            dt_obj_ha_tz = as_local(dt_obj_parsed)

            # Categorize into time blocks
            block_name, effective_date = get_time_block_info(dt_obj_ha_tz)

            if block_name and effective_date:
                block_prices[(effective_date, block_name)].append(price)
            else:
                _LOGGER.warning(f"Could not determine block for {dt_str}")

        except Exception as e:
            _LOGGER.error(f"Error processing price point: {e}")
            continue

    # Calculate averages for each block
    block_averages = {}
    for key, prices in block_prices.items():
        if prices:
            avg = sum(prices) / len(prices)
            block_averages[key] = round(avg, 2)
    
    _LOGGER.info(f"Calculated {len(block_averages)} block averages")

    # Find unique dates in chronological order
    all_dates = []
    for effective_date, _ in block_averages.keys():
        if effective_date not in all_dates:
            all_dates.append(effective_date)
    
    all_dates.sort()

    if not all_dates:
        _LOGGER.warning("No dates found in processed data")
        set_sensors_unavailable("No forecast dates available")
        return

    # Find first future 16:00 with Peak data
    first_1600_date = None
    
    for potential_date in all_dates:
        dt_1600_naive = datetime.combine(potential_date, time(16, 0))
        dt_1600_ha_tz = as_local(dt_1600_naive)
        
        if (potential_date, 'Peak') in block_averages:
            try:
                if dt_1600_ha_tz >= now:
                    first_1600_date = potential_date
                    _LOGGER.info(f"First future 16:00: {dt_1600_ha_tz.isoformat()}")
                    break
            except Exception as e:
                _LOGGER.error(f"Error comparing times: {e}")
                break

    if first_1600_date is None:
        _LOGGER.warning("No future 16:00 with Peak data available")
        set_sensors_unavailable("No future forecast periods available")
        return

    # Define forecast periods and update sensors
    sensor_definitions = {
        0: {'suffix': '24_48h', 'hours': '24-48'},
        1: {'suffix': '48_72h', 'hours': '48-72'},
        2: {'suffix': '72_96h', 'hours': '72-96'},
        3: {'suffix': '96_120h', 'hours': '96-120'},
        4: {'suffix': '120_144h', 'hours': '120-144'},
    }

    # Update each forecast sensor
    for i in range(5):
        sensor_info = sensor_definitions.get(i)
        if not sensor_info:
            continue

        # Calculate period dates
        block_start_date = first_1600_date + timedelta(days=i+1)
        block_end_date = block_start_date + timedelta(days=1)
        
        suffix = sensor_info['suffix']
        hours = sensor_info['hours']
        entity_id = f"sensor.agile_forecast_{suffix}"
        
        _LOGGER.debug(f"Processing {entity_id} for period {block_start_date} to {block_end_date}")
        
        # Define required blocks for this period
        required_blocks = [
            (block_start_date, 'Peak'),
            (block_start_date, 'Evening'),
            (block_start_date, 'Nighttime'),
            (block_end_date, 'Morning'),
            (block_end_date, 'Afternoon')
        ]
        
        # Gather block data
        attributes = {}
        total_price = 0.0
        blocks_found = 0
        all_blocks_present = True
        
        for day, block_name in required_blocks:
            avg_price = block_averages.get((day, block_name))
            attr_name = f"{block_name.lower()}_price"
            
            if avg_price is not None:
                attributes[attr_name] = avg_price
                total_price += avg_price
                blocks_found += 1
            else:
                attributes[attr_name] = None
                all_blocks_present = False
                _LOGGER.warning(f"Missing data for {block_name} on {day}")
        
        # Calculate overall average if all blocks present
        if blocks_found == len(required_blocks):
            overall_avg = round(total_price / blocks_found, 2)
            state_value = overall_avg
            overall_attr = overall_avg
        else:
            state_value = 'unavailable'
            overall_attr = 'N/A'
        
        # Add period timestamps
        dt_start = as_local(datetime.combine(block_start_date, time(16, 0)))
        dt_end = as_local(datetime.combine(block_end_date, time(16, 0)))
        
        # Complete the attributes
        attributes['forecast_period_start'] = dt_start.isoformat()
        attributes['forecast_period_end'] = dt_end.isoformat()
        attributes['unit_of_measurement'] = all_attributes.get('unit_of_measurement', 'GBP/kWh')
        attributes['icon'] = 'mdi:currency-gbp'
        attributes['friendly_name'] = f"Agile Forecast {hours} Hours"
        attributes['all_blocks_present'] = all_blocks_present
        attributes['source_entity'] = 'sensor.agile_predict'
        attributes['overall_average'] = overall_attr
        
        # Update the sensor
        try:
            state.set(entity_id, state_value, attributes)
            _LOGGER.info(f"Updated {entity_id} to {state_value}")
        except Exception as e:
            _LOGGER.error(f"Failed to update {entity_id}: {e}")
    
    _LOGGER.info("Agile forecast update completed successfully")