# Home Assistant PyScripts

PyScript modules for Home Assistant automation.

## Structure

- `src/` - PyScript source files
  - `agile_forecast_processor.py` - Process Agile electricity price forecasts
  - `update_ev_charging_schedule.py` - Calculate optimal EV charging schedules

- `tests/` - Test suite with mocked Home Assistant dependencies
  - `test_agile_forecast_processor.py`
  - `test_ev_charging_schedule.py`

## Running Tests

### Local Testing

Install test dependencies:
```bash
pip install -r requirements-test.txt
```

Run tests:
```bash
pytest
```

Run with coverage:
```bash
pytest --cov=src --cov-report=html
```

### Docker Testing

Run tests using the shell script (easiest):
```bash
./run-tests.sh
```

With coverage report:
```bash
./run-tests.sh --coverage
```

Interactive shell:
```bash
./run-tests.sh --shell
```

Or using Make:
```bash
make test-docker          # Run tests
make test-coverage        # Generate coverage report
make shell                # Interactive shell
```

Or using docker-compose:
```bash
docker-compose run --rm test
```

## Installation

Copy the files from `src/` to your Home Assistant PyScript directory (typically `config/pyscript/`).
