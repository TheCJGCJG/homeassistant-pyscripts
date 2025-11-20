FROM public.ecr.aws/docker/library/python:slim-trixie

WORKDIR /app

# Copy requirements first for better caching
COPY requirements-test.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements-test.txt

# Copy source and tests
COPY src/ ./src/
COPY tests/ ./tests/
COPY pytest.ini .

# Run tests by default
CMD ["pytest", "--cov=src", "--cov-report=term-missing"]
