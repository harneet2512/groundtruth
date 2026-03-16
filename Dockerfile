# Dockerfile for GroundTruth MCP and A/B benchmark execution.
# Build: docker build -t groundtruth .
# Run benchmark: docker run --rm groundtruth python -m benchmarks.ab.harness --condition both --fixture python
# Run with env (e.g. API keys): docker run --rm -e OPENAI_API_KEY=... groundtruth ...

FROM python:3.11-slim

WORKDIR /app

# Install project and benchmark dependencies
COPY pyproject.toml README.md ./
COPY src/ src/
COPY benchmarks/ benchmarks/

RUN pip install --no-cache-dir -e ".[dev,benchmark]"

# Default: run A/B benchmark both conditions (python fixture for speed)
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "benchmarks.ab.harness", "--condition", "both", "--fixture", "python"]
