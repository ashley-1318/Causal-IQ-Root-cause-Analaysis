# Stage 1: Build Stage
FROM python:3.11-slim AS builder

WORKDIR /build
# Install compilers only for the build phase
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# Install to a local directory to be copied later
# Upgrade pip and install the smaller CPU version of torch specifically
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --user torch --index-url https://download.pytorch.org/whl/cpu

# Install the remaining requirements
RUN pip install --no-cache-dir --user --default-timeout=1000 --retries 10 -r requirements.txt

# Stage 2: Runtime Stage
FROM python:3.11-slim
WORKDIR /app

# Copy ONLY the installed packages from the builder (from the .local dir)
COPY --from=builder /root/.local /root/.local
COPY . .

# Ensure the installed packages are in the PATH
ENV PATH=/root/.local/bin:$PATH
ENV PYTHONPATH=/root/.local/lib/python3.11/site-packages:$PYTHONPATH

CMD ["python", "orchestrator.py"]
