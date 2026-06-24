set shell := ["bash", "-uc"]

# List available recipes
default:
    @just --list

# Install all dependencies and setup the virtual environment
install:
    uv sync --all-extras

# Run tests
test *args:
    uv run pytest {{args}}

# Run tests with coverage report
test-cov *args:
    uv run pytest --cov=src --cov-report=term-missing --cov-report=xml {{args}}

# Format code with black
format:
    uv run black src tests

# Run all linters (black, flake8, pylint, bandit)
lint: lint-black lint-flake8 lint-pylint lint-bandit

# Check code formatting with black
lint-black:
    uv run black --check src tests

# Lint code with flake8
lint-flake8:
    uv run flake8

# Lint code with pylint
lint-pylint:
    uv run pylint src

# Run security checks with bandit
lint-bandit:
    uv run bandit -c pyproject.toml -r src

# Run pre-commit hooks on all files
pre-commit:
    uv run pre-commit run --all-files

# Build package distributions (wheel and sdist)
build:
    uv build

# Deploy / Publish package to PyPI
deploy: build
    uv publish

# Deploy / Publish package to TestPyPI
deploy-test: build
    uv publish --publish-url https://test.pypi.org/legacy/
