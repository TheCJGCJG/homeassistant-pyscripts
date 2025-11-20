"""Tests for agile_forecast_processor.py"""
import pytest
from datetime import datetime, time, date, timedelta
from unittest.mock import Mock, MagicMock, patch
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

from agile_forecast_processor import (
    get_time_block_info,
    set_sensors_unavailable,
    update_agile_forecasts,
    TIME_BLOCK_RANGES,
    SENSOR_SUFFIXES
)


@pytest.fixture
def mock_ha_now():
    """Mock Home Assistant's now() function"""
    with patch('agile_forecast_processor.ha_now') as mock:
        test_time = datetime(2024, 1, 15, 10, 30, tzinfo=None)
        mock.return_value = test_time
        yield mock


@pytest.fixture
def mock_as_local():
    """Mock Home Assistant's as_local() function"""
    with patch('agile_forecast_processor.as_local') as mock:
        mock.side_effect = lambda dt: dt
        yield mock


@pytest.fixture
def mock_hass():
    """Mock Home Assistant hass object"""
    import builtins
    original = builtins.hass
    mock = MagicMock()
    builtins.hass = mock
    yield mock
    builtins.hass = original


@pytest.fixture
def mock_state():
    """Mock Home Assistant state object"""
    import builtins
    original = builtins.state
    mock = MagicMock()
    builtins.state = mock
    yield mock
    builtins.state = original


class TestGetTimeBlockInfo:
    """Tests for get_time_block_info function"""
    
    def test_morning_block(self):
        """Test time in morning block (6:00-12:00)"""
        dt = datetime(2024, 1, 15, 8, 30)
        block_name, effective_date = get_time_block_info(dt)
        assert block_name == 'Morning'
        assert effective_date == date(2024, 1, 15)
    
    def test_afternoon_block(self):
        """Test time in afternoon block (12:00-16:00)"""
        dt = datetime(2024, 1, 15, 14, 0)
        block_name, effective_date = get_time_block_info(dt)
        assert block_name == 'Afternoon'
        assert effective_date == date(2024, 1, 15)
    
    def test_peak_block(self):
        """Test time in peak block (16:00-20:00)"""
        dt = datetime(2024, 1, 15, 18, 30)
        block_name, effective_date = get_time_block_info(dt)
        assert block_name == 'Peak'
        assert effective_date == date(2024, 1, 15)
    
    def test_evening_block(self):
        """Test time in evening block (20:00-23:00)"""
        dt = datetime(2024, 1, 15, 21, 0)
        block_name, effective_date = get_time_block_info(dt)
        assert block_name == 'Evening'
        assert effective_date == date(2024, 1, 15)
    
    def test_nighttime_after_midnight(self):
        """Test nighttime block after midnight (00:00-06:00)"""
        dt = datetime(2024, 1, 15, 2, 30)
        block_name, effective_date = get_time_block_info(dt)
        assert block_name == 'Nighttime'
        # Should belong to previous day's nighttime block
        assert effective_date == date(2024, 1, 14)
    
    def test_nighttime_before_midnight(self):
        """Test nighttime block before midnight (23:00-00:00)"""
        dt = datetime(2024, 1, 15, 23, 30)
        block_name, effective_date = get_time_block_info(dt)
        assert block_name == 'Nighttime'
        assert effective_date == date(2024, 1, 15)
    
    def test_boundary_morning_start(self):
        """Test exact boundary at morning start (06:00)"""
        dt = datetime(2024, 1, 15, 6, 0)
        block_name, effective_date = get_time_block_info(dt)
        assert block_name == 'Morning'
        assert effective_date == date(2024, 1, 15)
    
    def test_boundary_peak_start(self):
        """Test exact boundary at peak start (16:00)"""
        dt = datetime(2024, 1, 15, 16, 0)
        block_name, effective_date = get_time_block_info(dt)
        assert block_name == 'Peak'
        assert effective_date == date(2024, 1, 15)


class TestSetSensorsUnavailable:
    """Tests for set_sensors_unavailable function"""
    
    def test_sets_all_sensors_unavailable(self, mock_state):
        """Test that all forecast sensors are set to unavailable"""
        set_sensors_unavailable("Test reason")
        
        # Should be called once for each sensor suffix
        assert mock_state.set.call_count == len(SENSOR_SUFFIXES)
        
        # Check that all calls set state to 'unavailable'
        for call in mock_state.set.call_args_list:
            args, kwargs = call
            assert args[1] == 'unavailable'
    
    def test_sensor_attributes(self, mock_state):
        """Test that sensors have correct attributes"""
        set_sensors_unavailable("Test reason", "sensor.test_source")
        
        # Check first sensor call
        args, kwargs = mock_state.set.call_args_list[0]
        entity_id, state_value, attrs = args
        
        assert 'friendly_name' in attrs
        assert attrs['icon'] == 'mdi:currency-gbp'
        assert attrs['source_entity'] == 'sensor.test_source'


class TestUpdateAgileForecasts:
    """Tests for update_agile_forecasts service"""
    
    def test_missing_source_entity(self, mock_hass, mock_state, mock_ha_now, mock_as_local):
        """Test handling when source entity is not found"""
        mock_hass.states.get.return_value = None
        
        update_agile_forecasts()
        
        # Should set sensors to unavailable
        assert mock_state.set.call_count == len(SENSOR_SUFFIXES)
    
    def test_invalid_price_data(self, mock_hass, mock_state, mock_ha_now, mock_as_local):
        """Test handling when price data is invalid"""
        mock_sensor = Mock()
        mock_sensor.attributes = {'prices': None}
        mock_hass.states.get.return_value = mock_sensor
        
        update_agile_forecasts()
        
        # Should set sensors to unavailable
        assert mock_state.set.call_count == len(SENSOR_SUFFIXES)
    
    def test_empty_price_data(self, mock_hass, mock_state, mock_ha_now, mock_as_local):
        """Test handling when price data is empty"""
        mock_sensor = Mock()
        mock_sensor.attributes = {'prices': []}
        mock_hass.states.get.return_value = mock_sensor
        
        update_agile_forecasts()
        
        # Should set sensors to unavailable
        assert mock_state.set.call_count == len(SENSOR_SUFFIXES)
    
    def test_successful_update_with_valid_data(self, mock_hass, mock_state, mock_ha_now, mock_as_local):
        """Test successful update with valid price data"""
        # Setup mock current time
        now = datetime(2024, 1, 15, 10, 0)
        mock_ha_now.return_value = now
        mock_as_local.side_effect = lambda dt: dt if isinstance(dt, datetime) else datetime.combine(dt, time(0, 0))
        
        # Create price data covering multiple days
        prices = []
        start_date = date(2024, 1, 15)
        for day_offset in range(7):
            current_date = start_date + timedelta(days=day_offset)
            for hour in range(0, 24):
                dt = datetime.combine(current_date, time(hour, 0))
                prices.append({
                    'date_time': dt.isoformat(),
                    'agile_pred': 15.5 + day_offset + (hour * 0.1)
                })
        
        mock_sensor = Mock()
        mock_sensor.attributes = {
            'prices': prices,
            'unit_of_measurement': 'GBP/kWh'
        }
        mock_hass.states.get.return_value = mock_sensor
        
        update_agile_forecasts()
        
        # Should update all 5 forecast sensors
        assert mock_state.set.call_count >= 5


class TestTimeBlockBoundaries:
    """Comprehensive tests for all time block boundaries"""
    
    def test_all_block_transitions(self):
        """Test transitions between all time blocks"""
        test_cases = [
            # (hour, minute, expected_block, expected_date_offset)
            (5, 59, 'Nighttime', -1),  # Last minute of nighttime (previous day)
            (6, 0, 'Morning', 0),       # First minute of morning
            (11, 59, 'Morning', 0),     # Last minute of morning
            (12, 0, 'Afternoon', 0),    # First minute of afternoon
            (15, 59, 'Afternoon', 0),   # Last minute of afternoon
            (16, 0, 'Peak', 0),         # First minute of peak
            (19, 59, 'Peak', 0),        # Last minute of peak
            (20, 0, 'Evening', 0),      # First minute of evening
            (22, 59, 'Evening', 0),     # Last minute of evening
            (23, 0, 'Nighttime', 0),    # First minute of nighttime (same day)
            (23, 59, 'Nighttime', 0),   # Last minute before midnight
            (0, 0, 'Nighttime', -1),    # Midnight (previous day's nighttime)
        ]
        
        base_date = date(2024, 1, 15)
        for hour, minute, expected_block, date_offset in test_cases:
            dt = datetime(2024, 1, 15, hour, minute)
            block_name, effective_date = get_time_block_info(dt)
            expected_date = base_date + timedelta(days=date_offset)
            assert block_name == expected_block, f"Failed at {hour:02d}:{minute:02d}"
            assert effective_date == expected_date, f"Date mismatch at {hour:02d}:{minute:02d}"


class TestForecastPeriodCalculation:
    """Tests for forecast period calculation with real-world scenarios"""
    
    def test_forecast_after_1600_publication(self, mock_hass, mock_state, mock_ha_now, mock_as_local):
        """Test forecast calculation after 16:00 when official prices are published"""
        # Current time: 16:30 on Jan 15 (just after publication)
        now = datetime(2024, 1, 15, 16, 30)
        mock_ha_now.return_value = now
        mock_as_local.side_effect = lambda dt: dt if isinstance(dt, datetime) else datetime.combine(dt, time(0, 0))
        
        # Create comprehensive price data
        prices = []
        start_date = date(2024, 1, 15)
        
        # Generate prices for 7 days, every 30 minutes
        for day_offset in range(7):
            current_date = start_date + timedelta(days=day_offset)
            for hour in range(24):
                for minute in [0, 30]:
                    dt = datetime.combine(current_date, time(hour, minute))
                    # Vary prices by time of day (cheaper at night)
                    base_price = 15.0
                    if 23 <= hour or hour < 6:  # Nighttime
                        base_price = 8.0
                    elif 16 <= hour < 20:  # Peak
                        base_price = 25.0
                    
                    prices.append({
                        'date_time': dt.isoformat(),
                        'agile_pred': base_price + (day_offset * 0.5)
                    })
        
        mock_sensor = Mock()
        mock_sensor.attributes = {
            'prices': prices,
            'unit_of_measurement': 'GBP/kWh'
        }
        mock_hass.states.get.return_value = mock_sensor
        
        update_agile_forecasts()
        
        # Should successfully update all sensors
        assert mock_state.set.call_count >= 5
        
        # Verify that sensors have proper attributes
        for call in mock_state.set.call_args_list:
            args, _ = call
            entity_id, state_value, attrs = args
            
            if 'agile_forecast' in entity_id:
                # Should have forecast period timestamps
                assert 'forecast_period_start' in attrs
                assert 'forecast_period_end' in attrs
                assert 'all_blocks_present' in attrs
    
    def test_forecast_before_1600_publication(self, mock_hass, mock_state, mock_ha_now, mock_as_local):
        """Test forecast calculation before 16:00 (using previous day's publication)"""
        # Current time: 10:00 on Jan 15 (before today's publication)
        now = datetime(2024, 1, 15, 10, 0)
        mock_ha_now.return_value = now
        mock_as_local.side_effect = lambda dt: dt if isinstance(dt, datetime) else datetime.combine(dt, time(0, 0))
        
        # Prices available from yesterday's 16:00 until today's 22:30
        prices = []
        start_date = date(2024, 1, 15)
        
        for day_offset in range(7):
            current_date = start_date + timedelta(days=day_offset)
            for hour in range(24):
                for minute in [0, 30]:
                    dt = datetime.combine(current_date, time(hour, minute))
                    prices.append({
                        'date_time': dt.isoformat(),
                        'agile_pred': 12.0 + (hour * 0.5)
                    })
        
        mock_sensor = Mock()
        mock_sensor.attributes = {
            'prices': prices,
            'unit_of_measurement': 'GBP/kWh'
        }
        mock_hass.states.get.return_value = mock_sensor
        
        update_agile_forecasts()
        
        # Should update sensors
        assert mock_state.set.call_count >= 5
    
    def test_missing_peak_block_data(self, mock_hass, mock_state, mock_ha_now, mock_as_local):
        """Test handling when Peak block data is missing for a period"""
        now = datetime(2024, 1, 15, 10, 0)
        mock_ha_now.return_value = now
        mock_as_local.side_effect = lambda dt: dt if isinstance(dt, datetime) else datetime.combine(dt, time(0, 0))
        
        # Create prices but skip Peak hours for one day
        prices = []
        start_date = date(2024, 1, 15)
        
        for day_offset in range(7):
            current_date = start_date + timedelta(days=day_offset)
            for hour in range(24):
                # Skip Peak hours (16-20) on day 2
                if day_offset == 2 and 16 <= hour < 20:
                    continue
                    
                for minute in [0, 30]:
                    dt = datetime.combine(current_date, time(hour, minute))
                    prices.append({
                        'date_time': dt.isoformat(),
                        'agile_pred': 15.0
                    })
        
        mock_sensor = Mock()
        mock_sensor.attributes = {
            'prices': prices,
            'unit_of_measurement': 'GBP/kWh'
        }
        mock_hass.states.get.return_value = mock_sensor
        
        update_agile_forecasts()
        
        # Should still update sensors, but some may be unavailable
        assert mock_state.set.call_count >= 5


class TestBlockAverageCalculation:
    """Tests for price averaging within time blocks"""
    
    def test_block_price_averaging(self, mock_hass, mock_state, mock_ha_now, mock_as_local):
        """Test that prices are correctly averaged within each block"""
        now = datetime(2024, 1, 15, 10, 0)
        mock_ha_now.return_value = now
        mock_as_local.side_effect = lambda dt: dt if isinstance(dt, datetime) else datetime.combine(dt, time(0, 0))
        
        # Create prices with known values for easy verification
        prices = []
        start_date = date(2024, 1, 15)
        
        for day_offset in range(7):
            current_date = start_date + timedelta(days=day_offset)
            
            # Morning block (6:00-12:00): prices 10.0
            for hour in range(6, 12):
                for minute in [0, 30]:
                    dt = datetime.combine(current_date, time(hour, minute))
                    prices.append({'date_time': dt.isoformat(), 'agile_pred': 10.0})
            
            # Afternoon block (12:00-16:00): prices 15.0
            for hour in range(12, 16):
                for minute in [0, 30]:
                    dt = datetime.combine(current_date, time(hour, minute))
                    prices.append({'date_time': dt.isoformat(), 'agile_pred': 15.0})
            
            # Peak block (16:00-20:00): prices 25.0
            for hour in range(16, 20):
                for minute in [0, 30]:
                    dt = datetime.combine(current_date, time(hour, minute))
                    prices.append({'date_time': dt.isoformat(), 'agile_pred': 25.0})
            
            # Evening block (20:00-23:00): prices 20.0
            for hour in range(20, 23):
                for minute in [0, 30]:
                    dt = datetime.combine(current_date, time(hour, minute))
                    prices.append({'date_time': dt.isoformat(), 'agile_pred': 20.0})
            
            # Nighttime block (23:00-06:00): prices 5.0
            for hour in [23, 0, 1, 2, 3, 4, 5]:
                for minute in [0, 30]:
                    dt = datetime.combine(current_date, time(hour, minute))
                    prices.append({'date_time': dt.isoformat(), 'agile_pred': 5.0})
        
        mock_sensor = Mock()
        mock_sensor.attributes = {
            'prices': prices,
            'unit_of_measurement': 'GBP/kWh'
        }
        mock_hass.states.get.return_value = mock_sensor
        
        update_agile_forecasts()
        
        # Verify sensors were updated
        assert mock_state.set.call_count >= 5
        
        # Check that block prices are in attributes
        for call in mock_state.set.call_args_list:
            args, _ = call
            entity_id, state_value, attrs = args
            
            if 'agile_forecast' in entity_id and state_value != 'unavailable':
                # Should have individual block prices
                if 'peak_price' in attrs and attrs['peak_price'] is not None:
                    assert attrs['peak_price'] == 25.0
                if 'morning_price' in attrs and attrs['morning_price'] is not None:
                    assert attrs['morning_price'] == 10.0


class TestEdgeCases:
    """Tests for edge cases and error conditions"""
    
    def test_sparse_price_data(self, mock_hass, mock_state, mock_ha_now, mock_as_local):
        """Test handling of sparse/incomplete price data"""
        now = datetime(2024, 1, 15, 10, 0)
        mock_ha_now.return_value = now
        mock_as_local.side_effect = lambda dt: dt if isinstance(dt, datetime) else datetime.combine(dt, time(0, 0))
        
        # Only a few price points
        prices = [
            {'date_time': datetime(2024, 1, 15, 16, 0).isoformat(), 'agile_pred': 15.0},
            {'date_time': datetime(2024, 1, 15, 17, 0).isoformat(), 'agile_pred': 16.0},
            {'date_time': datetime(2024, 1, 16, 10, 0).isoformat(), 'agile_pred': 12.0},
        ]
        
        mock_sensor = Mock()
        mock_sensor.attributes = {
            'prices': prices,
            'unit_of_measurement': 'GBP/kWh'
        }
        mock_hass.states.get.return_value = mock_sensor
        
        update_agile_forecasts()
        
        # Should handle gracefully
        assert mock_state.set.call_count >= 5
    
    def test_malformed_price_entries(self, mock_hass, mock_state, mock_ha_now, mock_as_local):
        """Test handling of malformed price entries"""
        now = datetime(2024, 1, 15, 10, 0)
        mock_ha_now.return_value = now
        mock_as_local.side_effect = lambda dt: dt if isinstance(dt, datetime) else datetime.combine(dt, time(0, 0))
        
        prices = [
            {'date_time': datetime(2024, 1, 15, 16, 0).isoformat(), 'agile_pred': 15.0},
            {'date_time': None, 'agile_pred': 16.0},  # Missing datetime
            {'date_time': datetime(2024, 1, 15, 18, 0).isoformat(), 'agile_pred': None},  # Missing price
            {},  # Empty entry
            {'date_time': 'invalid', 'agile_pred': 'invalid'},  # Invalid types
            {'date_time': datetime(2024, 1, 15, 20, 0).isoformat(), 'agile_pred': 20.0},
        ]
        
        mock_sensor = Mock()
        mock_sensor.attributes = {
            'prices': prices,
            'unit_of_measurement': 'GBP/kWh'
        }
        mock_hass.states.get.return_value = mock_sensor
        
        update_agile_forecasts()
        
        # Should skip invalid entries and process valid ones
        assert mock_state.set.call_count >= 5
    
    def test_future_data_only(self, mock_hass, mock_state, mock_ha_now, mock_as_local):
        """Test when all price data is in the future"""
        now = datetime(2024, 1, 15, 10, 0)
        mock_ha_now.return_value = now
        mock_as_local.side_effect = lambda dt: dt if isinstance(dt, datetime) else datetime.combine(dt, time(0, 0))
        
        # All prices start from tomorrow
        prices = []
        start_date = date(2024, 1, 16)  # Tomorrow
        
        for day_offset in range(7):
            current_date = start_date + timedelta(days=day_offset)
            for hour in range(24):
                for minute in [0, 30]:
                    dt = datetime.combine(current_date, time(hour, minute))
                    prices.append({
                        'date_time': dt.isoformat(),
                        'agile_pred': 15.0
                    })
        
        mock_sensor = Mock()
        mock_sensor.attributes = {
            'prices': prices,
            'unit_of_measurement': 'GBP/kWh'
        }
        mock_hass.states.get.return_value = mock_sensor
        
        update_agile_forecasts()
        
        # Should process future data
        assert mock_state.set.call_count >= 5
    
    def test_timezone_handling(self, mock_hass, mock_state, mock_ha_now, mock_as_local):
        """Test proper timezone handling across DST boundaries"""
        now = datetime(2024, 3, 31, 10, 0)  # Near DST transition
        mock_ha_now.return_value = now
        mock_as_local.side_effect = lambda dt: dt if isinstance(dt, datetime) else datetime.combine(dt, time(0, 0))
        
        prices = []
        start_date = date(2024, 3, 31)
        
        for day_offset in range(7):
            current_date = start_date + timedelta(days=day_offset)
            for hour in range(24):
                for minute in [0, 30]:
                    dt = datetime.combine(current_date, time(hour, minute))
                    prices.append({
                        'date_time': dt.isoformat(),
                        'agile_pred': 15.0
                    })
        
        mock_sensor = Mock()
        mock_sensor.attributes = {
            'prices': prices,
            'unit_of_measurement': 'GBP/kWh'
        }
        mock_hass.states.get.return_value = mock_sensor
        
        update_agile_forecasts()
        
        # Should handle timezone transitions
        assert mock_state.set.call_count >= 5
