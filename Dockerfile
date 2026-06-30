FROM python:3.12-slim

WORKDIR /app

# Install server dependencies
RUN pip install --no-cache-dir fastapi uvicorn[standard]

# Copy source
COPY src/ src/
COPY pyproject.toml .

# Install PCP (for imports from src/pcp/)
RUN pip install --no-cache-dir -e .

# Data directory (mount Railway volume here)
RUN mkdir -p /data/pcp

EXPOSE 8000

CMD ["sh", "-c", "uvicorn pcp.server.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
