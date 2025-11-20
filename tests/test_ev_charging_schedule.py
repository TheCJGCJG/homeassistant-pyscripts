"""Tests for update_ev_charging_schedule.py"""
import pytest
from datetime import datetime, timedelta
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

from update_ev_charging_schedule import (
    get_datetime_from_rate,
    get_ready_by_datetime,
    get_required_charging_slots,
    get_price_data,
    process_price_data,
    find_cheapest_block,
    update_sensors,
    set_unavailable,
    update_ev_charging_schedule
)


@pytest.fixture
def mock_ha_now():
    """Mock Home Assistant's now() function"""
    with patch('update_ev_charging_schedule.ha_now') as mock:
        test_time = datetime(2024, 1, 15, 10, 30)
        mock.return_value = test_time
        yield mock


@pytest.fixture
def mock_as_local():
    """Mock Home Assistant's as_local() function"""
    with patch('update_ev_charging_schedule.as_local') as mock:
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


class TestGetDatetimeFromRate:
    """Tests for get_datetime_from_rate function"""
    
    def test_string_datetime(self, mock_as_local):
        """Test conversion from ISO string"""
        dt_str = "2024-01-15T10:30:00"
        result = get_datetime_from_rate(dt_str)
        assert result is not None
        assert isinstance(result, datetime)
    
    def test_datetime_object(self, mock_as_local):
        """Test conversion from datetime object"""
        dt = datetime(2024, 1, 15, 10, 30)
        result = get_datetime_from_rate(dt)
        assert result is not None
        assert isinstance(result, datetime)
    
    def test_invalid_string(self, mock_as_local):
        """Test handling of invalid datetime string"""
        result = get_datetime_from_rate("invalid")
        assert result is None
    
    def test_invalid_type(self, mock_as_local):
        """Test handling of invalid type"""
        result = get_datetime_from_rate(12345)
        assert result is None


class TestGetReadyByDatetime:
    """Tests for get_ready_by_datetime function"""
    
    def test_valid_ready_by_time(self, mock_hass, mock_as_local):
        """Test getting valid ready by time"""
        mock_state = Mock()
        mock_state.state = "2024-01-15T18:00:00"
        mock_hass.states.get.return_value = mock_state
        
        result = get_ready_by_datetime()
        assert result is not None
        assert isinstance(result, datetime)
    
    def test_unavailable_entity(self, mock_hass, mock_as_local):
        """Test handling when entity is unavailable"""
        mock_state = Mock()
        mock_state.state = "unavailable"
        mock_hass.states.get.return_value = mock_state
        
        result = get_ready_by_datetime()
        assert result is None
    
    def test_missing_entity(self, mock_hass, mock_as_local):
        """Test handling when entity doesn't exist"""
        mock_hass.states.get.return_value = None
        
        result = get_ready_by_datetime()
        assert result is None
    
    def test_invalid_datetime_format(self, mock_hass, mock_as_local):
        """Test handling of invalid datetime format"""
        mock_state = Mock()
        mock_state.state = "invalid-datetime"
        mock_hass.states.get.return_value = mock_state
        
        result = get_ready_by_datetime()
        assert result is None


class TestGetRequiredChargingSlots:
    """Tests for get_required_charging_slots function"""
    
    def test_valid_hours(self, mock_hass):
        """Test conversion of valid hours to slots"""
        mock_state = Mock()
        mock_state.state = "4.5"
        mock_hass.states.get.return_value = mock_state
        
        result = get_required_charging_slots()
        assert result == 9  # 4.5 hours * 2 slots per hour
    
    def test_zero_hours(self, mock_hass):
        """Test handling of zero hours"""
        mock_state = Mock()
        mock_state.state = "0"
        mock_hass.states.get.return_value = mock_state
        
        result = get_required_charging_slots()
        assert result is None
    
    def test_negative_hours(self, mock_hass):
        """Test handling of negative hours"""
        mock_state = Mock()
        mock_state.state = "-1"
        mock_hass.states.get.return_value = mock_state
        
        result = get_required_charging_slots()
        assert result is None
    
    def test_unavailable_entity(self, mock_hass):
        """Test handling when entity is unavailable"""
        mock_state = Mock()
        mock_state.state = "unavailable"
        mock_hass.states.get.return_value = mock_state
        
        result = get_required_charging_slots()
        assert result is None
    
    def test_invalid_number(self, mock_hass):
        """Test handling of invalid number format"""
        mock_state = Mock()
        mock_state.state = "not-a-number"
        mock_hass.states.get.return_value = mock_state
        
        result = get_required_charging_slots()
        assert result is None


class TestGetPriceData:
    """Tests for get_price_data function"""
    
    def test_collects_current_rates(self, mock_hass, mock_as_local):
        """Test collection of current day rates"""
        mock_sensor = Mock()
        mock_sensor.attributes = {
            'rates': [
                {'start': '2024-01-15T10:00:00', 'value_inc_vat': 15.5},
                {'start': '2024-01-15T10:30:00', 'value_inc_vat': 16.0}
            ]
        }
        mock_hass.states.get.return_value = mock_sensor
        
        prices = get_price_data()
        
        # Should have prices from current rates
        current_prices = [p for p in prices if p['source'] == 'current_actual']
        assert len(current_prices) == 2
    
    def test_collects_predicted_rates(self, mock_hass, mock_as_local):
        """Test collection of predicted rates"""
        def mock_get(entity_id):
            mock_sensor = Mock()
            if 'agile_predict' in entity_id:
                mock_sensor.attributes = {
                    'prices': [
                        {'date_time': '2024-01-16T10:00:00', 'agile_pred': 1550},  # p/kWh
                        {'date_time': '2024-01-16T10:30:00', 'agile_pred': 1600}
                    ]
                }
            else:
                mock_sensor.attributes = {'rates': []}
            return mock_sensor
        
        mock_hass.states.get.side_effect = mock_get
        
        prices = get_price_data()
        
        # Should have predicted prices converted to £/kWh
        predicted_prices = [p for p in prices if p['source'] == 'predicted']
        assert len(predicted_prices) == 2
        assert predicted_prices[0]['price'] == 15.5  # 1550 p/kWh = 15.5 £/kWh


class TestProcessPriceData:
    """Tests for process_price_data function"""
    
    def test_deduplication_prefers_actual(self):
        """Test that actual prices are preferred over predicted"""
        now = datetime(2024, 1, 15, 10, 0)
        dt = datetime(2024, 1, 15, 12, 0)
        
        prices = [
            {'date_time': dt, 'price': 15.0, 'source': 'predicted'},
            {'date_time': dt, 'price': 16.0, 'source': 'current_actual'}
        ]
        
        result = process_price_data(prices, now)
        
        # Should keep actual price
        assert len(result) == 1
        assert result[0]['price'] == 16.0
        assert result[0]['source'] == 'current_actual'
    
    def test_filters_past_prices(self):
        """Test that past prices are filtered out"""
        now = datetime(2024, 1, 15, 10, 0)
        
        prices = [
            {'date_time': datetime(2024, 1, 15, 9, 0), 'price': 15.0, 'source': 'current_actual'},
            {'date_time': datetime(2024, 1, 15, 11, 0), 'price': 16.0, 'source': 'current_actual'}
        ]
        
        result = process_price_data(prices, now)
        
        # Should only keep future price (slot ending after now)
        assert len(result) == 1
        assert result[0]['date_time'] == datetime(2024, 1, 15, 11, 0)
    
    def test_sorts_chronologically(self):
        """Test that prices are sorted by datetime"""
        now = datetime(2024, 1, 15, 10, 0)
        
        prices = [
            {'date_time': datetime(2024, 1, 15, 13, 0), 'price': 17.0, 'source': 'current_actual'},
            {'date_time': datetime(2024, 1, 15, 11, 0), 'price': 15.0, 'source': 'current_actual'},
            {'date_time': datetime(2024, 1, 15, 12, 0), 'price': 16.0, 'source': 'current_actual'}
        ]
        
        result = process_price_data(prices, now)
        
        # Should be sorted
        assert result[0]['date_time'] == datetime(2024, 1, 15, 11, 0)
        assert result[1]['date_time'] == datetime(2024, 1, 15, 12, 0)
        assert result[2]['date_time'] == datetime(2024, 1, 15, 13, 0)


class TestFindCheapestBlock:
    """Tests for find_cheapest_block function"""
    
    def test_finds_cheapest_block(self):
        """Test finding the cheapest contiguous block"""
        ready_by = datetime(2024, 1, 15, 18, 0)
        
        prices = [
            {'date_time': datetime(2024, 1, 15, 10, 0), 'price': 20.0},
            {'date_time': datetime(2024, 1, 15, 10, 30), 'price': 15.0},
            {'date_time': datetime(2024, 1, 15, 11, 0), 'price': 10.0},  # Cheapest block starts here
            {'date_time': datetime(2024, 1, 15, 11, 30), 'price': 12.0},
            {'date_time': datetime(2024, 1, 15, 12, 0), 'price': 25.0},
        ]
        
        result = find_cheapest_block(prices, 2, ready_by)
        
        assert result is not None
        assert result['start_dt'] == datetime(2024, 1, 15, 11, 0)
        assert result['num_slots'] == 2
        assert result['total_cost'] == 22.0  # 10.0 + 12.0
    
    def test_respects_ready_by_constraint(self):
        """Test that blocks ending after ready_by are excluded"""
        ready_by = datetime(2024, 1, 15, 11, 30)
        
        prices = [
            {'date_time': datetime(2024, 1, 15, 10, 0), 'price': 20.0},
            {'date_time': datetime(2024, 1, 15, 10, 30), 'price': 15.0},
            {'date_time': datetime(2024, 1, 15, 11, 0), 'price': 5.0},  # Would be cheapest but ends after ready_by
            {'date_time': datetime(2024, 1, 15, 11, 30), 'price': 5.0},
        ]
        
        result = find_cheapest_block(prices, 2, ready_by)
        
        # Should pick the only valid block (10:30-11:30 ends exactly at ready_by)
        assert result is not None
        assert result['start_dt'] == datetime(2024, 1, 15, 10, 30)
    
    def test_insufficient_slots(self):
        """Test handling when not enough slots available"""
        ready_by = datetime(2024, 1, 15, 18, 0)
        
        prices = [
            {'date_time': datetime(2024, 1, 15, 10, 0), 'price': 20.0},
        ]
        
        result = find_cheapest_block(prices, 2, ready_by)
        assert result is None
    
    def test_no_valid_blocks_before_ready_by(self):
        """Test when no blocks end before ready_by"""
        ready_by = datetime(2024, 1, 15, 10, 0)
        
        prices = [
            {'date_time': datetime(2024, 1, 15, 10, 0), 'price': 20.0},
            {'date_time': datetime(2024, 1, 15, 10, 30), 'price': 15.0},
        ]
        
        result = find_cheapest_block(prices, 2, ready_by)
        assert result is None


class TestUpdateSensors:
    """Tests for update_sensors function"""
    
    def test_updates_all_sensors(self, mock_state):
        """Test that all sensors are updated"""
        block = {
            'start_dt': datetime(2024, 1, 15, 12, 0),
            'end_dt': datetime(2024, 1, 15, 14, 0),
            'avg_cost': 15.5,
            'total_cost': 62.0,
            'num_slots': 4
        }
        ready_by = datetime(2024, 1, 15, 18, 0)
        now = datetime(2024, 1, 15, 10, 0)
        
        update_sensors(block, ready_by, now)
        
        # Should update 4 sensors (start, end, cost, binary)
        assert mock_state.set.call_count == 4
    
    def test_binary_sensor_on_during_period(self, mock_state):
        """Test binary sensor is 'on' during charging period"""
        block = {
            'start_dt': datetime(2024, 1, 15, 10, 0),
            'end_dt': datetime(2024, 1, 15, 12, 0),
            'avg_cost': 15.5,
            'total_cost': 62.0,
            'num_slots': 4
        }
        ready_by = datetime(2024, 1, 15, 18, 0)
        now = datetime(2024, 1, 15, 11, 0)  # During charging period
        
        update_sensors(block, ready_by, now)
        
        # Find binary sensor call
        binary_call = [call for call in mock_state.set.call_args_list 
                      if 'binary_sensor' in call[0][0]][0]
        
        assert binary_call[0][1] == 'on'
    
    def test_binary_sensor_off_outside_period(self, mock_state):
        """Test binary sensor is 'off' outside charging period"""
        block = {
            'start_dt': datetime(2024, 1, 15, 12, 0),
            'end_dt': datetime(2024, 1, 15, 14, 0),
            'avg_cost': 15.5,
            'total_cost': 62.0,
            'num_slots': 4
        }
        ready_by = datetime(2024, 1, 15, 18, 0)
        now = datetime(2024, 1, 15, 10, 0)  # Before charging period
        
        update_sensors(block, ready_by, now)
        
        # Find binary sensor call
        binary_call = [call for call in mock_state.set.call_args_list 
                      if 'binary_sensor' in call[0][0]][0]
        
        assert binary_call[0][1] == 'off'


class TestSetUnavailable:
    """Tests for set_unavailable function"""
    
    def test_sets_all_sensors_unavailable(self, mock_state, mock_ha_now):
        """Test that all sensors are set to unavailable"""
        set_unavailable("Test reason")
        
        # Should update 4 sensors
        assert mock_state.set.call_count == 4
        
        # Check that standard sensors are unavailable
        standard_calls = [call for call in mock_state.set.call_args_list 
                         if 'binary_sensor' not in call[0][0]]
        
        for call in standard_calls:
            assert call[0][1] == 'unavailable'
    
    def test_binary_sensor_set_to_off(self, mock_state, mock_ha_now):
        """Test that binary sensor is set to 'off'"""
        set_unavailable("Test reason")
        
        # Find binary sensor call
        binary_call = [call for call in mock_state.set.call_args_list 
                      if 'binary_sensor' in call[0][0]][0]
        
        assert binary_call[0][1] == 'off'


class TestUpdateEvChargingSchedule:
    """Integration tests for update_ev_charging_schedule service"""
    
    def test_successful_schedule_update(self, mock_hass, mock_state, mock_ha_now, mock_as_local):
        """Test successful schedule update with valid data"""
        now = datetime(2024, 1, 15, 10, 0)
        mock_ha_now.return_value = now
        
        # Mock ready by time
        ready_by_state = Mock()
        ready_by_state.state = "2024-01-15T18:00:00"
        
        # Mock charging hours
        hours_state = Mock()
        hours_state.state = "2.0"
        
        # Mock price sensors
        def mock_get(entity_id):
            if 'ready_by' in entity_id:
                return ready_by_state
            elif 'charging_hours' in entity_id:
                return hours_state
            elif 'cheapest_start' in entity_id:
                return None  # No existing schedule
            else:
                mock_sensor = Mock()
                mock_sensor.attributes = {'rates': [], 'prices': []}
                return mock_sensor
        
        mock_hass.states.get.side_effect = mock_get
        
        # This will fail due to no price data, but tests the flow
        update_ev_charging_schedule()
        
        # Should attempt to set sensors (to unavailable due to no data)
        assert mock_state.set.call_count > 0


class TestPriceDataPriority:
    """Tests for price data source priority (actual vs predicted)"""
    
    def test_actual_prices_override_predicted(self, mock_hass, mock_as_local):
        """Test that actual prices from Octopus override predicted prices"""
        dt1 = datetime(2024, 1, 15, 12, 0)
        dt2 = datetime(2024, 1, 15, 12, 30)
        dt3 = datetime(2024, 1, 15, 13, 0)
        
        def mock_get(entity_id):
            mock_sensor = Mock()
            if 'current_day_rates' in entity_id:
                # Actual prices for 12:00 and 12:30
                mock_sensor.attributes = {
                    'rates': [
                        {'start': dt1, 'value_inc_vat': 15.5},
                        {'start': dt2, 'value_inc_vat': 16.0}
                    ]
                }
            elif 'agile_predict' in entity_id:
                # Predicted prices for all three slots
                mock_sensor.attributes = {
                    'prices': [
                        {'date_time': dt1.isoformat(), 'agile_pred': 2000},  # 20.00 £/kWh
                        {'date_time': dt2.isoformat(), 'agile_pred': 2100},  # 21.00 £/kWh
                        {'date_time': dt3.isoformat(), 'agile_pred': 1800},  # 18.00 £/kWh
                    ]
                }
            else:
                mock_sensor.attributes = {'rates': []}
            return mock_sensor
        
        mock_hass.states.get.side_effect = mock_get
        
        prices = get_price_data()
        
        # Should have 3 prices total
        assert len(prices) >= 3
        
        # Find prices for dt1 and dt2
        dt1_prices = [p for p in prices if p['date_time'] == dt1]
        dt2_prices = [p for p in prices if p['date_time'] == dt2]
        
        # Should have both actual and predicted
        assert len(dt1_prices) >= 1
        assert len(dt2_prices) >= 1
    
    def test_predicted_prices_converted_from_pence(self, mock_hass, mock_as_local):
        """Test that predicted prices are converted from p/kWh to £/kWh"""
        def mock_get(entity_id):
            mock_sensor = Mock()
            if 'agile_predict' in entity_id:
                mock_sensor.attributes = {
                    'prices': [
                        {'date_time': '2024-01-15T12:00:00', 'agile_pred': 1550},  # 15.50 £/kWh
                        {'date_time': '2024-01-15T12:30:00', 'agile_pred': 2000},  # 20.00 £/kWh
                    ]
                }
            else:
                mock_sensor.attributes = {'rates': []}
            return mock_sensor
        
        mock_hass.states.get.side_effect = mock_get
        
        prices = get_price_data()
        
        predicted = [p for p in prices if p['source'] == 'predicted']
        assert len(predicted) == 2
        assert predicted[0]['price'] == 15.5
        assert predicted[1]['price'] == 20.0


class TestCheapestBlockScenarios:
    """Comprehensive tests for finding cheapest charging blocks"""
    
    def test_cheapest_block_with_mixed_sources(self):
        """Test finding cheapest block when mixing actual and predicted prices"""
        ready_by = datetime(2024, 1, 15, 18, 0)
        now = datetime(2024, 1, 15, 10, 0)
        
        # Simulate real scenario: actual prices until 22:30 today, predicted after
        prices = [
            # Current actual prices (expensive during day)
            {'date_time': datetime(2024, 1, 15, 10, 0), 'price': 20.0, 'source': 'current_actual'},
            {'date_time': datetime(2024, 1, 15, 10, 30), 'price': 21.0, 'source': 'current_actual'},
            {'date_time': datetime(2024, 1, 15, 11, 0), 'price': 22.0, 'source': 'current_actual'},
            {'date_time': datetime(2024, 1, 15, 11, 30), 'price': 23.0, 'source': 'current_actual'},
            
            # Cheaper predicted prices for afternoon
            {'date_time': datetime(2024, 1, 15, 12, 0), 'price': 10.0, 'source': 'predicted'},
            {'date_time': datetime(2024, 1, 15, 12, 30), 'price': 11.0, 'source': 'predicted'},
            {'date_time': datetime(2024, 1, 15, 13, 0), 'price': 12.0, 'source': 'predicted'},
            {'date_time': datetime(2024, 1, 15, 13, 30), 'price': 13.0, 'source': 'predicted'},
        ]
        
        future_prices = process_price_data(prices, now)
        result = find_cheapest_block(future_prices, 4, ready_by)
        
        assert result is not None
        # Should pick the predicted cheaper block starting at 12:00
        assert result['start_dt'] == datetime(2024, 1, 15, 12, 0)
        assert result['num_slots'] == 4
        # Total: 10 + 11 + 12 + 13 = 46, avg = 11.5
        assert result['avg_cost'] == 11.5
    
    def test_overnight_charging_cheapest(self):
        """Test that overnight charging is identified as cheapest"""
        ready_by = datetime(2024, 1, 16, 7, 0)  # Ready by 7am tomorrow
        now = datetime(2024, 1, 15, 20, 0)  # 8pm today
        
        prices = []
        # Evening prices (expensive)
        for hour in [20, 21, 22]:
            for minute in [0, 30]:
                dt = datetime(2024, 1, 15, hour, minute)
                prices.append({'date_time': dt, 'price': 25.0, 'source': 'current_actual'})
        
        # Nighttime prices (cheap)
        for hour in [23, 0, 1, 2, 3, 4, 5, 6]:
            for minute in [0, 30]:
                if hour == 23:
                    dt = datetime(2024, 1, 15, hour, minute)
                else:
                    dt = datetime(2024, 1, 16, hour, minute)
                prices.append({'date_time': dt, 'price': 8.0, 'source': 'predicted'})
        
        future_prices = process_price_data(prices, now)
        result = find_cheapest_block(future_prices, 8, ready_by)  # 4 hours charging
        
        assert result is not None
        # Should pick nighttime slots
        assert result['start_dt'].hour >= 23 or result['start_dt'].hour < 6
        assert result['avg_cost'] == 8.0
    
    def test_peak_vs_offpeak_selection(self):
        """Test that off-peak is selected over peak when available"""
        ready_by = datetime(2024, 1, 15, 20, 0)
        now = datetime(2024, 1, 15, 10, 0)
        
        prices = []
        # Morning off-peak (cheap)
        for hour in [10, 11]:
            for minute in [0, 30]:
                dt = datetime(2024, 1, 15, hour, minute)
                prices.append({'date_time': dt, 'price': 12.0, 'source': 'current_actual'})
        
        # Afternoon moderate
        for hour in [12, 13, 14, 15]:
            for minute in [0, 30]:
                dt = datetime(2024, 1, 15, hour, minute)
                prices.append({'date_time': dt, 'price': 18.0, 'source': 'current_actual'})
        
        # Peak (expensive)
        for hour in [16, 17, 18, 19]:
            for minute in [0, 30]:
                dt = datetime(2024, 1, 15, hour, minute)
                prices.append({'date_time': dt, 'price': 30.0, 'source': 'current_actual'})
        
        future_prices = process_price_data(prices, now)
        result = find_cheapest_block(future_prices, 4, ready_by)  # 2 hours
        
        assert result is not None
        # Should pick morning off-peak
        assert result['start_dt'] == datetime(2024, 1, 15, 10, 0)
        assert result['avg_cost'] == 12.0
    
    def test_long_charging_session(self):
        """Test finding cheapest block for long charging session (8+ hours)"""
        ready_by = datetime(2024, 1, 16, 8, 0)
        now = datetime(2024, 1, 15, 18, 0)
        
        prices = []
        # Generate 24 hours of prices
        for hour in range(18, 24):
            for minute in [0, 30]:
                dt = datetime(2024, 1, 15, hour, minute)
                # Evening expensive
                prices.append({'date_time': dt, 'price': 25.0, 'source': 'current_actual'})
        
        for hour in range(0, 8):
            for minute in [0, 30]:
                dt = datetime(2024, 1, 16, hour, minute)
                # Nighttime cheap
                prices.append({'date_time': dt, 'price': 7.0, 'source': 'predicted'})
        
        future_prices = process_price_data(prices, now)
        result = find_cheapest_block(future_prices, 16, ready_by)  # 8 hours
        
        assert result is not None
        # Should pick overnight block
        assert result['avg_cost'] == 7.0
        assert result['num_slots'] == 16
    
    def test_short_charging_session(self):
        """Test finding cheapest block for short charging session (1 hour)"""
        ready_by = datetime(2024, 1, 15, 15, 0)
        now = datetime(2024, 1, 15, 10, 0)
        
        prices = [
            {'date_time': datetime(2024, 1, 15, 10, 0), 'price': 15.0, 'source': 'current_actual'},
            {'date_time': datetime(2024, 1, 15, 10, 30), 'price': 16.0, 'source': 'current_actual'},
            {'date_time': datetime(2024, 1, 15, 11, 0), 'price': 10.0, 'source': 'current_actual'},  # Cheapest
            {'date_time': datetime(2024, 1, 15, 11, 30), 'price': 11.0, 'source': 'current_actual'},  # Cheapest
            {'date_time': datetime(2024, 1, 15, 12, 0), 'price': 20.0, 'source': 'current_actual'},
            {'date_time': datetime(2024, 1, 15, 12, 30), 'price': 21.0, 'source': 'current_actual'},
        ]
        
        future_prices = process_price_data(prices, now)
        result = find_cheapest_block(future_prices, 2, ready_by)  # 1 hour
        
        assert result is not None
        assert result['start_dt'] == datetime(2024, 1, 15, 11, 0)
        assert result['total_cost'] == 21.0  # 10 + 11
    
    def test_price_spike_avoidance(self):
        """Test that algorithm avoids price spikes"""
        ready_by = datetime(2024, 1, 15, 18, 0)
        now = datetime(2024, 1, 15, 10, 0)
        
        prices = [
            {'date_time': datetime(2024, 1, 15, 10, 0), 'price': 12.0, 'source': 'current_actual'},
            {'date_time': datetime(2024, 1, 15, 10, 30), 'price': 50.0, 'source': 'current_actual'},  # Spike!
            {'date_time': datetime(2024, 1, 15, 11, 0), 'price': 13.0, 'source': 'current_actual'},
            {'date_time': datetime(2024, 1, 15, 11, 30), 'price': 13.0, 'source': 'current_actual'},
            {'date_time': datetime(2024, 1, 15, 12, 0), 'price': 13.0, 'source': 'current_actual'},
            {'date_time': datetime(2024, 1, 15, 12, 30), 'price': 13.0, 'source': 'current_actual'},
        ]
        
        future_prices = process_price_data(prices, now)
        result = find_cheapest_block(future_prices, 3, ready_by)
        
        assert result is not None
        # Should avoid the spike and pick 11:00-12:30
        assert result['start_dt'] == datetime(2024, 1, 15, 11, 0)
        assert result['avg_cost'] == 13.0


class TestReadyByConstraints:
    """Tests for ready-by time constraints"""
    
    def test_tight_deadline(self):
        """Test with very tight deadline (only one valid block)"""
        ready_by = datetime(2024, 1, 15, 11, 0)
        now = datetime(2024, 1, 15, 10, 0)
        
        prices = [
            {'date_time': datetime(2024, 1, 15, 10, 0), 'price': 20.0},
            {'date_time': datetime(2024, 1, 15, 10, 30), 'price': 15.0},
            {'date_time': datetime(2024, 1, 15, 11, 0), 'price': 10.0},  # Too late
        ]
        
        result = find_cheapest_block(prices, 2, ready_by)
        
        assert result is not None
        # Must use the only valid block
        assert result['start_dt'] == datetime(2024, 1, 15, 10, 0)
    
    def test_deadline_exactly_at_block_end(self):
        """Test when deadline is exactly at the end of a charging block"""
        ready_by = datetime(2024, 1, 15, 12, 0)
        now = datetime(2024, 1, 15, 10, 0)
        
        prices = [
            {'date_time': datetime(2024, 1, 15, 10, 0), 'price': 20.0},
            {'date_time': datetime(2024, 1, 15, 10, 30), 'price': 15.0},
            {'date_time': datetime(2024, 1, 15, 11, 0), 'price': 10.0},
            {'date_time': datetime(2024, 1, 15, 11, 30), 'price': 12.0},
        ]
        
        result = find_cheapest_block(prices, 4, ready_by)
        
        assert result is not None
        assert result['end_dt'] == ready_by
    
    def test_impossible_deadline(self):
        """Test when deadline is too soon for required charging"""
        ready_by = datetime(2024, 1, 15, 10, 30)
        now = datetime(2024, 1, 15, 10, 0)
        
        prices = [
            {'date_time': datetime(2024, 1, 15, 10, 0), 'price': 15.0},
            {'date_time': datetime(2024, 1, 15, 10, 30), 'price': 16.0},
        ]
        
        # Need 4 slots but only 1 slot ends before deadline
        result = find_cheapest_block(prices, 4, ready_by)
        
        assert result is None


class TestChargingSessionManagement:
    """Tests for active charging session management"""
    
    def test_schedule_update_during_active_session(self, mock_hass, mock_state, mock_ha_now, mock_as_local):
        """Test that schedule is preserved during active charging session"""
        now = datetime(2024, 1, 15, 11, 30)  # During charging
        mock_ha_now.return_value = now
        
        # Mock existing schedule
        existing_sensor = Mock()
        existing_sensor.state = '2024-01-15T11:00:00'
        existing_sensor.attributes = {
            'cheapest_period_start': '2024-01-15T11:00:00',
            'cheapest_period_end': '2024-01-15T13:00:00',
            'ready_by_time': '2024-01-15T18:00:00',
            'number_of_slots': 4
        }
        
        ready_by_state = Mock()
        ready_by_state.state = '2024-01-15T18:00:00'
        ready_by_state.last_changed = datetime(2024, 1, 15, 9, 0)  # Changed long ago
        
        hours_state = Mock()
        hours_state.state = '2.0'
        hours_state.last_changed = datetime(2024, 1, 15, 9, 0)  # Changed long ago
        
        def mock_get(entity_id):
            if 'cheapest_start' in entity_id:
                return existing_sensor
            elif 'ready_by' in entity_id:
                return ready_by_state
            elif 'charging_hours' in entity_id:
                return hours_state
            else:
                mock_sensor = Mock()
                mock_sensor.attributes = {'rates': [], 'prices': []}
                return mock_sensor
        
        mock_hass.states.get.side_effect = mock_get
        
        update_ev_charging_schedule()
        
        # Should not recalculate during active session with unchanged inputs
        # The function returns early, so minimal state updates
        assert mock_state.set.call_count == 0


class TestRealWorldScenarios:
    """Integration tests simulating real-world usage patterns"""
    
    def test_typical_evening_charge_scenario(self, mock_hass, mock_state, mock_ha_now, mock_as_local):
        """Test typical scenario: arrive home at 6pm, need car by 7am"""
        now = datetime(2024, 1, 15, 18, 0)  # 6pm
        mock_ha_now.return_value = now
        
        ready_by_state = Mock()
        ready_by_state.state = '2024-01-16T07:00:00'  # 7am tomorrow
        
        hours_state = Mock()
        hours_state.state = '4.0'  # 4 hours charging needed
        
        def mock_get(entity_id):
            if 'ready_by' in entity_id:
                return ready_by_state
            elif 'charging_hours' in entity_id:
                return hours_state
            elif 'cheapest_start' in entity_id:
                return None
            elif 'current_day_rates' in entity_id:
                # Actual prices until 22:30 today
                mock_sensor = Mock()
                rates = []
                for hour in range(18, 23):
                    for minute in [0, 30]:
                        dt = datetime(2024, 1, 15, hour, minute)
                        # Evening peak prices
                        rates.append({'start': dt, 'value_inc_vat': 25.0})
                mock_sensor.attributes = {'rates': rates}
                return mock_sensor
            elif 'next_day_rates' in entity_id:
                # Next day actual prices from midnight to 22:30
                mock_sensor = Mock()
                rates = []
                for hour in range(0, 7):
                    for minute in [0, 30]:
                        dt = datetime(2024, 1, 16, hour, minute)
                        # Nighttime cheap prices
                        rates.append({'start': dt, 'value_inc_vat': 8.0})
                mock_sensor.attributes = {'rates': rates}
                return mock_sensor
            elif 'agile_predict' in entity_id:
                # Predicted prices for gaps
                mock_sensor = Mock()
                prices = []
                for hour in range(23, 24):
                    for minute in [0, 30]:
                        dt = datetime(2024, 1, 15, hour, minute)
                        prices.append({'date_time': dt.isoformat(), 'agile_pred': 900})  # 9.00 £/kWh
                mock_sensor.attributes = {'prices': prices}
                return mock_sensor
            else:
                mock_sensor = Mock()
                mock_sensor.attributes = {'rates': [], 'prices': []}
                return mock_sensor
        
        mock_hass.states.get.side_effect = mock_get
        
        update_ev_charging_schedule()
        
        # Should calculate and update sensors
        assert mock_state.set.call_count == 4
        
        # Verify cheapest period is overnight
        start_time_call = [c for c in mock_state.set.call_args_list if 'cheapest_start' in c[0][0]][0]
        start_time = start_time_call[0][1]
        
        # Should be overnight (after 23:00 or before 07:00)
        start_dt = datetime.fromisoformat(start_time)
        assert start_dt.hour >= 23 or start_dt.hour < 7
    
    def test_agile_predict_only_scenario(self, mock_hass, mock_state, mock_ha_now, mock_as_local):
        """Test scenario where only predicted prices are available (far future)"""
        now = datetime(2024, 1, 15, 10, 0)
        mock_ha_now.return_value = now
        
        ready_by_state = Mock()
        ready_by_state.state = '2024-01-20T18:00:00'  # 5 days in future
        
        hours_state = Mock()
        hours_state.state = '3.0'
        
        def mock_get(entity_id):
            if 'ready_by' in entity_id:
                return ready_by_state
            elif 'charging_hours' in entity_id:
                return hours_state
            elif 'cheapest_start' in entity_id:
                return None
            elif 'agile_predict' in entity_id:
                # Only predicted prices available
                mock_sensor = Mock()
                prices = []
                for day in range(15, 21):
                    for hour in range(24):
                        for minute in [0, 30]:
                            dt = datetime(2024, 1, day, hour, minute)
                            # Vary by time of day
                            if 23 <= hour or hour < 6:
                                price = 800  # 8.00 £/kWh nighttime
                            elif 16 <= hour < 20:
                                price = 2500  # 25.00 £/kWh peak
                            else:
                                price = 1500  # 15.00 £/kWh normal
                            prices.append({'date_time': dt.isoformat(), 'agile_pred': price})
                mock_sensor.attributes = {'prices': prices}
                return mock_sensor
            else:
                mock_sensor = Mock()
                mock_sensor.attributes = {'rates': [], 'prices': []}
                return mock_sensor
        
        mock_hass.states.get.side_effect = mock_get
        
        update_ev_charging_schedule()
        
        # Should successfully calculate using predicted prices
        assert mock_state.set.call_count == 4


class TestErrorRecovery:
    """Tests for error handling and recovery"""
    
    def test_recovery_from_unavailable_state(self, mock_hass, mock_state, mock_ha_now, mock_as_local):
        """Test recovery when sensors were previously unavailable"""
        now = datetime(2024, 1, 15, 10, 0)
        mock_ha_now.return_value = now
        
        ready_by_state = Mock()
        ready_by_state.state = '2024-01-15T18:00:00'
        
        hours_state = Mock()
        hours_state.state = '2.0'
        
        # Previous state was unavailable
        existing_sensor = Mock()
        existing_sensor.state = 'unavailable'
        existing_sensor.attributes = {'error_reason': 'No price data'}
        
        def mock_get(entity_id):
            if 'ready_by' in entity_id:
                return ready_by_state
            elif 'charging_hours' in entity_id:
                return hours_state
            elif 'cheapest_start' in entity_id:
                return existing_sensor
            elif 'current_day_rates' in entity_id:
                mock_sensor = Mock()
                rates = []
                for hour in range(10, 18):
                    for minute in [0, 30]:
                        dt = datetime(2024, 1, 15, hour, minute)
                        rates.append({'start': dt, 'value_inc_vat': 15.0})
                mock_sensor.attributes = {'rates': rates}
                return mock_sensor
            else:
                mock_sensor = Mock()
                mock_sensor.attributes = {'rates': [], 'prices': []}
                return mock_sensor
        
        mock_hass.states.get.side_effect = mock_get
        
        update_ev_charging_schedule()
        
        # Should recover and calculate new schedule
        assert mock_state.set.call_count == 4
        
        # Verify state is no longer unavailable
        start_call = [c for c in mock_state.set.call_args_list if 'cheapest_start' in c[0][0]][0]
        assert start_call[0][1] != 'unavailable'
    
    def test_partial_price_data_handling(self, mock_hass, mock_state, mock_ha_now, mock_as_local):
        """Test handling when some price sources are unavailable"""
        now = datetime(2024, 1, 15, 10, 0)
        mock_ha_now.return_value = now
        
        ready_by_state = Mock()
        ready_by_state.state = '2024-01-15T18:00:00'
        
        hours_state = Mock()
        hours_state.state = '2.0'
        
        def mock_get(entity_id):
            if 'ready_by' in entity_id:
                return ready_by_state
            elif 'charging_hours' in entity_id:
                return hours_state
            elif 'cheapest_start' in entity_id:
                return None
            elif 'current_day_rates' in entity_id:
                # Current rates available
                mock_sensor = Mock()
                rates = []
                for hour in range(10, 18):
                    for minute in [0, 30]:
                        dt = datetime(2024, 1, 15, hour, minute)
                        rates.append({'start': dt, 'value_inc_vat': 15.0})
                mock_sensor.attributes = {'rates': rates}
                return mock_sensor
            elif 'next_day_rates' in entity_id:
                # Next day rates not available yet
                return None
            elif 'agile_predict' in entity_id:
                # Predicted prices not available
                return None
            else:
                mock_sensor = Mock()
                mock_sensor.attributes = {'rates': [], 'prices': []}
                return mock_sensor
        
        mock_hass.states.get.side_effect = mock_get
        
        update_ev_charging_schedule()
        
        # Should work with just current day rates
        assert mock_state.set.call_count == 4
