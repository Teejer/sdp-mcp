FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml sdp_mcp.py ./
RUN pip install --no-cache-dir .

# Run as non-root
RUN useradd --create-home appuser
USER appuser

# Credentials are NOT baked in — pass them at runtime with -e
ENTRYPOINT ["python", "-c", "import sdp_mcp; sdp_mcp.main()"]
