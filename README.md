# Cardano Governance Discord Bot

A Discord bot that monitors Cardano governance proposals, creates polls, and collects community feedback.

## Features

- 🔍 Automatically fetches active governance proposals from Koios API
- 📝 Generates AI-powered summaries using Google Gemini
- 🗳️ Creates Discord polls with Yes/No/Abstain options (configurable duration)
- 💬 Collects community rationales (comments starting with "RATIONAL:")
- 📊 Summarizes voting results and community feedback
- 💾 Stores all data in SQLite database

## Prerequisites

1. **Discord Bot Token**
   - Create a bot at [Discord Developer Portal](https://discord.com/developers/applications)
   - Enable required bot permissions
   - Enable MESSAGE CONTENT INTENT in Bot settings
   - Add the bot to your server

2. **Google Gemini API Key**
   - Get your API key from [Google AI Studio](https://makersuite.google.com/app/apikey)

3. **Koios API Token (Optional)**
   - For higher rate limits, get a token from [Koios](https://koios.rest/)

## Installation

### Using Docker (Recommended)

1. Clone the repository:
```bash
git clone <your-repo>
cd gov-bot-for-discord
```

2. Copy the environment example and configure:
```bash
cp env.example .env
# Edit .env with your actual values
```

3. Build and run with Docker Compose:
```bash
docker compose up -d --build
```

### Manual Installation

1. Install Python 3.11+
2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Set environment variables:
```bash
export DISCORD_BOT_TOKEN="your_token"
export DISCORD_CHANNEL_ID="your_channel_id"
export GEMINI_API_KEY="your_api_key"
# Optional:
export KOIOS_API_TOKEN="your_koios_token"
```
  - Or create .evn file in the directory. See env.example file

4. Run the bot:
```bash
python discord_bot.py
```

## Configuration

### Environment Variables

| Variable | Description | Required | Default |
|----------|-------------|----------|---------|
| `DISCORD_BOT_TOKEN` | Your Discord bot token | Yes | - |
| `DISCORD_CHANNEL_ID` | Channel ID where bot posts proposals | Yes | - |
| `GEMINI_API_KEY` | Google Gemini API key | Yes | - |
| `KOIOS_API_TOKEN` | Koios API token for higher limits | No | - |
| `KOIOS_BASE_URL` | Koios API base URL | No | https://api.koios.rest/api/v1 |
| `POLL_INTERVAL_HOURS` | How often to check for new proposals | No | 6 |
| `POLL_DURATION_MINUTES` | Poll duration in minutes (15-10080) | No | 20160 (14 days) |
| `GEMINI_MODEL` | Gemini model to use | No | gemini-1.5-flash |
| `INITIAL_BLOCK_TIME` | Unix timestamp to start from on first run | No | - |

## Usage

1. The bot will automatically check for new proposals every 6 hours (configurable)
2. For each new proposal, it will:
   - Create a new thread in the specified channel
   - Post a summary with proposal details
   - Create a poll with configurable duration
3. Community members can:
   - Vote on the poll
   - Add rationales by commenting "RATIONAL: [your reasoning]"
4. After the poll ends, the bot will:
   - Collect voting results
   - Summarize all rationales
   - Post the final results in the thread

## Database

The bot uses SQLite to store:
- Proposal tracking (to avoid duplicates)
- Thread and poll IDs
- Voting results
- Community rationales

Database file: `governance.db`

When using Docker, the database is stored in the mounted `data/` directory on the host and inside the container at `/app/data/governance.db`.
