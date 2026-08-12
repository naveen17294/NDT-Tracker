FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Copy requirements first (for caching)
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create data & sessions directories
RUN mkdir -p /app/data /app/sessions

# Run NDT bot
CMD ["python", "bot.py"]
