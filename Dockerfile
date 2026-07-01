FROM python:3.12-slim

WORKDIR /app

# Install server dependencies
RUN pip install --no-cache-dir fastapi uvicorn[standard] build

# Copy source
COPY src/ src/
COPY pyproject.toml .
COPY SKILL.md .

# Install PCP (for imports from src/pcp/)
RUN pip install --no-cache-dir -e .

# Build the wheel served at /download/pcp-latest.whl — this is how a project's
# LLM installs PCP without the origin repo being public or a PyPI release existing.
RUN python -m build --wheel -o /app/dist

# Data directory (mount Railway volume here)
RUN mkdir -p /data/pcp

EXPOSE 8000

CMD ["sh", "-c", "uvicorn pcp.server.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
