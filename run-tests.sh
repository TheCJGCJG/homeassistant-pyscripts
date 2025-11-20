#!/bin/bash

set -e

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}=== Home Assistant PyScript Test Runner ===${NC}\n"

# Check if Docker is available
if ! command -v docker &> /dev/null; then
    echo -e "${RED}Error: Docker is not installed or not in PATH${NC}"
    exit 1
fi

# Parse command line arguments
COVERAGE=false
SHELL=false
REBUILD=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --coverage|-c)
            COVERAGE=true
            shift
            ;;
        --shell|-s)
            SHELL=true
            shift
            ;;
        --rebuild|-r)
            REBUILD=true
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  -c, --coverage    Generate HTML coverage report"
            echo "  -s, --shell       Open interactive shell in container"
            echo "  -r, --rebuild     Force rebuild of Docker image"
            echo "  -h, --help        Show this help message"
            echo ""
            echo "Examples:"
            echo "  $0                Run tests"
            echo "  $0 --coverage     Run tests with coverage report"
            echo "  $0 --shell        Open shell in test container"
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# Build Docker image
if [ "$REBUILD" = true ] || ! docker images | grep -q "ha-pyscripts-test"; then
    echo -e "${BLUE}Building Docker image...${NC}"
    docker build -t ha-pyscripts-test .
    echo -e "${GREEN}✓ Image built successfully${NC}\n"
else
    echo -e "${GREEN}✓ Using existing Docker image${NC}\n"
fi

# Run based on mode
if [ "$SHELL" = true ]; then
    echo -e "${BLUE}Opening interactive shell...${NC}"
    docker run --rm -it \
        -v "$(pwd)/src:/app/src" \
        -v "$(pwd)/tests:/app/tests" \
        -v "$(pwd)/pytest.ini:/app/pytest.ini" \
        ha-pyscripts-test /bin/bash
elif [ "$COVERAGE" = true ]; then
    echo -e "${BLUE}Running tests with coverage...${NC}\n"
    docker run --rm \
        -v "$(pwd)/src:/app/src" \
        -v "$(pwd)/tests:/app/tests" \
        -v "$(pwd)/pytest.ini:/app/pytest.ini" \
        -v "$(pwd)/htmlcov:/app/htmlcov" \
        ha-pyscripts-test pytest --cov=src --cov-report=term-missing --cov-report=html
    
    if [ $? -eq 0 ]; then
        echo -e "\n${GREEN}✓ Tests passed!${NC}"
        echo -e "${BLUE}Coverage report: htmlcov/index.html${NC}"
    else
        echo -e "\n${RED}✗ Tests failed${NC}"
        exit 1
    fi
else
    echo -e "${BLUE}Running tests...${NC}\n"
    docker run --rm \
        -v "$(pwd)/src:/app/src" \
        -v "$(pwd)/tests:/app/tests" \
        -v "$(pwd)/pytest.ini:/app/pytest.ini" \
        ha-pyscripts-test
    
    if [ $? -eq 0 ]; then
        echo -e "\n${GREEN}✓ All tests passed!${NC}"
    else
        echo -e "\n${RED}✗ Tests failed${NC}"
        exit 1
    fi
fi
