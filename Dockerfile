# ---- Build stage ----
FROM python:3.11-slim AS builder

WORKDIR /app

# System deps for building Python packages with C extensions
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Copy dependency-related files first for layer caching
COPY pyproject.toml ./
COPY src/helixis/__init__.py src/helixis/__init__.py

# Install all optional dependency groups into a virtual env
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
# ``security`` pulls Presidio (+ spaCy lib, NO model, NO torch) so the prod
# image uses the real PII redaction engine rather than the regex fallback.
# ``document`` bakes in docling-parse (D#5): programmatic PDF text extraction
# for the document specialist so text-layer PDFs are parsed locally (cheap,
# deterministic, no doc bytes sent to the LLM) instead of the multimodal LLM
# path. It's the lean ``docling-parse`` primitive only (manylinux wheel, no
# torch, no OCR/layout models → no runtime model download, gotcha 7 N/A);
# scanned/image-only PDFs still fall back to the multimodal path.
# Retry the install across PyPI CDN 502 storms: files.pythonhosted.org
# killed BOTH the 2026-08-16 and 2026-08-17 deploys mid-build ("too many
# 502 error responses" — hatchling, then asyncpg). pip's own --retries
# only re-tries a single request; a storm outlasts it, so wrap the whole
# install: 3 attempts, 30s/60s between. A real dependency error still
# fails all three attempts and exits 1.
RUN for i in 1 2 3; do \
      pip install --no-cache-dir --retries 10 --timeout 30 \
        -e ".[browser,connectors,memory,security,document]" && break; \
      if [ "$i" = "3" ]; then exit 1; fi; \
      echo "pip install attempt $i failed (PyPI blip?) — retrying in $((i*30))s"; \
      sleep $((i*30)); \
    done

# Bake the spaCy model mem0's BM25 lemmatization needs at SEARCH time. Without
# it, mem0 tries to pip-download en_core_web_sm at runtime — which fails in the
# egress-restricted container and sys.exit(1)s. manager.py now contains that
# SystemExit (memory degrades instead of crashing the agent), but baking the
# model is what makes memory search actually WORK in prod rather than no-op.
RUN python -m spacy download en_core_web_sm

# ---- Runtime stage ----
FROM python:3.11-slim

WORKDIR /app

# Runtime system deps (libpq for psycopg, curl for healthcheck)
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq5 curl && \
    rm -rf /var/lib/apt/lists/*

# Copy virtual env from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application source and config
COPY src/ src/
COPY config/ config/
COPY scripts/ scripts/
COPY pyproject.toml ./

# Re-install in editable mode (CONFIG_DIR in settings.py resolves
# relative to __file__: Path(__file__).parent.parent.parent / "config"
# Editable install keeps __file__ at /app/src/helixis/settings.py
# so CONFIG_DIR resolves to /app/config)
RUN pip install --no-cache-dir --no-deps -e .

# Non-root user
RUN useradd --create-home appuser
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/api/v1/health || exit 1

# --proxy-headers / --forwarded-allow-ips=* make uvicorn honour
# X-Forwarded-Proto from Render's edge so FastAPI's slash-redirects
# build https:// targets instead of http:// (otherwise the browser
# blocks the cross-scheme redirect as mixed content).
# --timeout-graceful-shutdown 25: on SIGTERM (every deploy) uvicorn stops
# accepting connections and waits for in-flight requests — but our SSE
# streams stay open indefinitely, so without a bound uvicorn waits until
# Render SIGKILLs (~30s), skipping the lifespan shutdown entirely. Capping at
# 25s force-closes lingering streams IN TIME for the shutdown drain to run
# (release parked awaiting-approval claims + orphan pending confirms), so a
# deploy mid-confirm leaves retryable state instead of a needs-review row.
CMD ["uvicorn", "helixis.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips=*", "--timeout-graceful-shutdown", "25"]
