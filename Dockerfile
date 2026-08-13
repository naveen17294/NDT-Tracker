FROM python:3.10-slim

# Stream logs straight to the platform's log drain instead of sitting in a buffer,
# and skip writing .pyc files we never reuse.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Set working directory
WORKDIR /app

# Copy requirements first (for caching)
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create data & sessions directories.
# NOTE: these live on the container's ephemeral layer. Mount a persistent disk and
# set DATA_PATH / SESSION_PATH to it, or the DB and session are wiped on every deploy.
RUN mkdir -p /app/data /app/sessions

# Run NDT bot
CMD ["python", "bot.py"]
