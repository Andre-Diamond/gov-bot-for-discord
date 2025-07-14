FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY discord_bot.py .
COPY script.py .

# Create volume for database persistence
VOLUME ["/app/data"]

# Environment variables (will be overridden by docker-compose or runtime)
ENV DISCORD_BOT_TOKEN=""
ENV DISCORD_CHANNEL_ID=""
ENV GEMINI_API_KEY=""
ENV KOIOS_API_TOKEN=""
ENV KOIOS_BASE_URL="https://api.koios.rest/api/v1"
ENV POLL_INTERVAL_HOURS="6"
ENV POLL_DURATION_MINUTES="20160"
ENV GEMINI_MODEL="gemini-1.5-flash"
ENV INITIAL_BLOCK_TIME=""

# Run the bot
CMD ["python", "-u", "discord_bot.py"] 