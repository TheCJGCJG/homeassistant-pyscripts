.PHONY: test test-docker build clean

# Run tests locally
test:
	pytest --cov=src --cov-report=term-missing

# Build Docker image
build:
	docker build -t ha-pyscripts-test .

# Run tests in Docker
test-docker: build
	docker run --rm ha-pyscripts-test

# Run tests in Docker with docker-compose
test-compose:
	docker-compose run --rm test

# Run tests with coverage report
test-coverage:
	docker-compose run --rm test pytest --cov=src --cov-report=html
	@echo "Coverage report generated in htmlcov/index.html"

# Interactive shell in container
shell:
	docker-compose run --rm test /bin/bash

# Clean up
clean:
	rm -rf __pycache__ .pytest_cache .coverage htmlcov
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
