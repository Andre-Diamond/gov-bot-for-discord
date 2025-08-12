#!/usr/bin/env python3
"""Discord bot for Cardano governance proposals"""

import asyncio
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any
import json

import discord
from discord.ext import commands, tasks
import google.generativeai as genai

# Import our existing Koios functionality
from utils import (
    list_proposals, to_gaid, to_gaid_components, pick_title,
    lovelace_to_ada, link_templates, init_gemini, fetch_meta
)

# Bot configuration
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
KOIOS_BASE_URL = os.getenv("KOIOS_BASE_URL", "https://api.koios.rest/api/v1")
POLL_INTERVAL_HOURS = int(os.getenv("POLL_INTERVAL_HOURS", "6"))

# Discord poll duration in minutes
# Default: 20160 minutes (14 days)
# Minimum: 15 minutes
POLL_DURATION_MINUTES = int(os.getenv("POLL_DURATION_MINUTES", "20160"))
# Ensure poll duration is at least 15 minutes
POLL_DURATION_MINUTES = max(15, POLL_DURATION_MINUTES)

# Initial block_time to start from if database is empty
# Default: None (fetch all proposals)
# Example: 1704757130 (Unix timestamp for Jan 8, 2024)
INITIAL_BLOCK_TIME = os.getenv("INITIAL_BLOCK_TIME")
if INITIAL_BLOCK_TIME:
    try:
        INITIAL_BLOCK_TIME = int(INITIAL_BLOCK_TIME)
    except ValueError:
        print(f"Warning: Invalid INITIAL_BLOCK_TIME '{INITIAL_BLOCK_TIME}', ignoring...")
        INITIAL_BLOCK_TIME = None

class GovernanceBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.polls = True
        super().__init__(command_prefix='!', intents=intents)
        
        self.db_path = Path("governance.db")
        self.init_database()
        self.model = init_gemini(os.getenv("GEMINI_MODEL", "gemini-1.5-flash"))
        
    def init_database(self):
        """Initialize SQLite database for tracking proposals"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS proposals (
                gaid TEXT PRIMARY KEY,
                thread_id INTEGER,
                poll_message_id INTEGER,
                block_time INTEGER,  -- Unix timestamp from the proposal
                posted_at TIMESTAMP,
                poll_ends_at TIMESTAMP,
                final_vote TEXT,
                final_rational TEXT,
                processed BOOLEAN DEFAULT 0
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rationals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gaid TEXT,
                user_id INTEGER,
                username TEXT,
                rational TEXT,
                posted_at TIMESTAMP,
                FOREIGN KEY(gaid) REFERENCES proposals(gaid)
            )
        """)
        
        conn.commit()
        conn.close()

    async def on_ready(self):
        print(f'{self.user} has connected to Discord!')
        print(f'Bot is in {len(self.guilds)} guilds')
        # Only start tasks if they haven't been started yet
        if not self.check_proposals.is_running():
            self.check_proposals.start()
            print("Started check_proposals task")
        if not self.process_ended_polls.is_running():
            self.process_ended_polls.start()
            print("Started process_ended_polls task")

    def get_latest_block_time(self) -> Optional[int]:
        """Get the latest block_time from stored proposals"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(block_time) FROM proposals WHERE block_time IS NOT NULL")
        result = cursor.fetchone()
        conn.close()
        return result[0] if result and result[0] else None

    def is_proposal_posted(self, gaid: str) -> bool:
        """Check if a proposal has already been posted"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM proposals WHERE gaid = ?", (gaid,))
        exists = cursor.fetchone() is not None
        conn.close()
        return exists

    def save_proposal(self, gaid: str, thread_id: int, poll_message_id: int, poll_ends_at: datetime, block_time: int):
        """Save a new proposal to the database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO proposals (gaid, thread_id, poll_message_id, block_time, posted_at, poll_ends_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (gaid, thread_id, poll_message_id, block_time, datetime.now(timezone.utc), poll_ends_at))
        conn.commit()
        conn.close()

    def summarize_proposal(self, prop: Dict[str, Any]) -> str:
        """Generate a Discord-formatted summary of the proposal"""
        try:
            # Fetch metadata if needed
            if prop.get("meta_json") is None and prop.get("meta_url"):
                fetched = fetch_meta(
                    prop["meta_url"],
                    expected_hash=prop.get("meta_hash")
                )
                if fetched is not None:
                    prop["meta_json"] = fetched

            # Generate AI summary - limit the size of data sent to AI
            prop_summary = {
                "proposal_type": prop.get("proposal_type"),
                "title": pick_title(prop),
                "deposit": prop.get("deposit"),
                "proposed_epoch": prop.get("proposed_epoch"),
                "expiration": prop.get("expiration"),
                "meta_json": prop.get("meta_json", {})
            }
            
            prompt = (
                "You are an expert Cardano governance analyst. Given JSON metadata of an on-chain governance proposal, produce:\n"
                "1. A concise 2-3 sentence summary suitable for Discord.\n"
                "2. 3-5 bullet points with key insights (impact, pros/cons, important details).\n"
                "3. Format using Discord markdown (** for bold, * for italics, - for bullets).\n"
                "4. Keep the total response under 1000 characters.\n\n"
                "5. Do not include technical information like expiration date, proposed epoch, or deposit.\n"
                f"Proposal metadata:\n```json\n{json.dumps(prop_summary, indent=2)}\n```"
            )
            
            summary = self.model.generate_content(prompt).text.strip()
            
        except Exception as e:
            print(f"Error generating AI summary: {e}")
            summary = "AI summary generation failed. Please check the proposal details below."
        
        # Build Discord message
        title = pick_title(prop)
        gaid = to_gaid(prop)
        action_type = prop.get("proposal_type", "Unknown")
        deposit = lovelace_to_ada(prop.get("deposit"))
        expiration = prop.get("expiration")
        
        links = link_templates(KOIOS_BASE_URL)
        comps = to_gaid_components(prop)
        adastat_link = ""
        govtool_link = ""
        
        if comps:
            tx_hash, index = comps
            ada_id = f"{tx_hash}{index}"
            adastat_link = links["adastat"].format(ada_id=ada_id)
            govtool_link = links["govtool"].format(gaid=gaid)
        
        message = f"""# {title}

**GAID:** `{gaid}`
**Action Type:** {action_type}
**Deposit:** {deposit}
**Expiration:** {expiration}

{summary}

**Links:** [AdaStat]({adastat_link}) | [GovTool]({govtool_link})

*Please vote below and add your rationale as a comment starting with "RATIONAL:"*"""
        
        # Ensure message doesn't exceed Discord's limit
        if len(message) > 2000:
            message = message[:1997] + "..."
        
        return message

    @tasks.loop(hours=POLL_INTERVAL_HOURS)
    async def check_proposals(self):
        """Periodically check for new proposals"""
        try:
            await self.wait_until_ready()
            
            channel = self.get_channel(CHANNEL_ID)
            if not channel:
                print(f"Channel {CHANNEL_ID} not found!")
                return
            
            # Get the latest block_time we've seen
            latest_block_time = self.get_latest_block_time()
            
            # If we have a latest block_time, use it as a filter
            # Otherwise, use INITIAL_BLOCK_TIME if set, or get all proposals
            if latest_block_time:
                # For Koios API, we need to pass the Unix timestamp directly
                print(f"Fetching proposals after block_time: {latest_block_time}")
                
                # Get proposals after the latest block_time
                proposals = list_proposals(
                    KOIOS_BASE_URL,
                    page_size=50,
                    after_date=str(latest_block_time),  # Pass Unix timestamp as string
                    verbose=False
                )
            elif INITIAL_BLOCK_TIME:
                print(f"No previous proposals found, fetching proposals after initial block_time: {INITIAL_BLOCK_TIME}")
                proposals = list_proposals(
                    KOIOS_BASE_URL,
                    page_size=50,
                    after_date=str(INITIAL_BLOCK_TIME),
                    verbose=False
                )
            else:
                print("No previous proposals found, fetching all active proposals")
                proposals = list_proposals(
                    KOIOS_BASE_URL,
                    page_size=50,
                    verbose=False
                )
            
            print(f"Found {len(proposals)} proposals to check")
            
            for prop in proposals:
                gaid = to_gaid(prop)
                if not gaid:
                    continue
                
                # Get block_time from proposal
                block_time = prop.get("block_time")
                if block_time is None:
                    print(f"Warning: No block_time for proposal {gaid}")
                    continue
                
                # Skip if we've already posted this (double-check)
                if self.is_proposal_posted(gaid):
                    continue
                
                # Skip if this proposal is older than our latest (shouldn't happen with filter, but just in case)
                if latest_block_time and block_time <= latest_block_time:
                    continue
                
                try:
                    # Generate summary
                    summary = self.summarize_proposal(prop)
                    
                    # Create thread
                    thread_title = f"{pick_title(prop)[:90]} ({gaid[:10]}...)"
                    thread = await channel.create_thread(
                        name=thread_title,
                        type=discord.ChannelType.public_thread,
                        auto_archive_duration=10080  # 7 days
                    )
                    
                    # Post summary
                    await thread.send(summary)
                    
                    # Create poll
                    poll = discord.Poll(
                        question="How should we vote on this proposal?",
                        duration=timedelta(minutes=POLL_DURATION_MINUTES),
                        multiple=False
                    )
                    poll.add_answer(text="Yes", emoji="✅")
                    poll.add_answer(text="No", emoji="❌")
                    poll.add_answer(text="Abstain", emoji="🤷")
                    
                    poll_message = await thread.send(poll=poll)
                    
                    # Save to database with block_time
                    poll_ends_at = datetime.now(timezone.utc) + timedelta(minutes=POLL_DURATION_MINUTES)
                    self.save_proposal(gaid, thread.id, poll_message.id, poll_ends_at, block_time)
                    
                    print(f"Posted proposal: {gaid} (block_time: {block_time})")
                    
                    # Add a small delay to avoid rate limits
                    await asyncio.sleep(2)
                    
                except Exception as e:
                    print(f"Error posting proposal {gaid}: {e}")
                    
        except Exception as e:
            print(f"Error in check_proposals task: {e}")

    @tasks.loop(hours=1)
    async def process_ended_polls(self):
        """Check for ended polls and process results"""
        try:
            await self.wait_until_ready()
            
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Find unprocessed polls that have ended
            cursor.execute("""
                SELECT gaid, thread_id, poll_message_id 
                FROM proposals 
                WHERE processed = 0 AND poll_ends_at <= ?
            """, (datetime.now(timezone.utc),))
            
            ended_polls = cursor.fetchall()
            conn.close()
            
            for gaid, thread_id, poll_message_id in ended_polls:
                await self.process_poll_results(gaid, thread_id, poll_message_id)
                await asyncio.sleep(1)  # Rate limit protection
                
        except Exception as e:
            print(f"Error in process_ended_polls task: {e}")

    async def process_poll_results(self, gaid: str, thread_id: int, poll_message_id: int):
        """Process results of an ended poll"""
        try:
            thread = self.get_channel(thread_id)
            if not thread:
                print(f"Thread {thread_id} not found")
                return
                
            # Get poll message
            poll_message = await thread.fetch_message(poll_message_id)
            if not poll_message.poll:
                print(f"No poll found in message {poll_message_id}")
                return
            
            # Get poll results
            poll = poll_message.poll
            results = {
                "Yes": 0,
                "No": 0,
                "Abstain": 0
            }
            
            for answer in poll.answers:
                results[answer.text] = answer.vote_count or 0
            
            # Determine winning vote
            total_votes = sum(results.values())
            # If there are no votes, treat the outcome as Abstain
            if total_votes == 0:
                final_vote = "Abstain"
            else:
                final_vote = max(results, key=results.get)
            
            # Collect rationals from thread
            rationals = []
            async for message in thread.history(limit=200):
                if message.author.bot:
                    continue
                    
                if message.content.startswith("RATIONAL:"):
                    rational_text = message.content[9:].strip()
                    rationals.append({
                        "user": message.author.name,
                        "text": rational_text
                    })
                    
                    # Save to database
                    conn = sqlite3.connect(self.db_path)
                    cursor = conn.cursor()
                    cursor.execute("""
                        INSERT INTO rationals (gaid, user_id, username, rational, posted_at)
                        VALUES (?, ?, ?, ?, ?)
                    """, (gaid, message.author.id, message.author.name, rational_text, message.created_at))
                    conn.commit()
                    conn.close()
            
            # Generate final rational summary
            final_rational = self.generate_final_rational(final_vote, results, rationals)
            
            # Post results
            result_message = f"""## 📊 **Poll Results**

**Final Vote:** {final_vote}
- ✅ Yes: {results['Yes']} votes
- ❌ No: {results['No']} votes  
- 🤷 Abstain: {results['Abstain']} votes

**Total Votes:** {total_votes}

## 📝 **Community Rational**

{final_rational}"""
            
            await thread.send(result_message)
            
            # Update database
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE proposals 
                SET final_vote = ?, final_rational = ?, processed = 1
                WHERE gaid = ?
            """, (final_vote, final_rational, gaid))
            conn.commit()
            conn.close()
            
            print(f"Processed poll results for {gaid}")
            
        except Exception as e:
            print(f"Error processing poll results for {gaid}: {e}")

    def generate_final_rational(self, vote: str, results: Dict[str, int], rationals: List[Dict[str, str]]) -> str:
        """Generate a summary of community rationals using AI.

        Handles the edge case where no votes were cast by treating the outcome as "Abstain".
        """
        if not rationals:
            return "No rationals provided by the community."

        total_votes = sum(results.values())
        if total_votes == 0:
            effective_vote = "Abstain"
        else:
            effective_vote = vote if vote in results else max(results, key=results.get)

        votes_for_effective = results.get(effective_vote, 0)

        rational_texts = "\n".join([f"- {r['user']}: {r['text']}" for r in rationals[:20]])  # Limit to 20 rationals

        if total_votes == 0:
            prompt = (
                "No votes were cast in the poll. Treat this as an \"Abstain\" outcome. "
                "Using the following rationals from community members, generate a concise summary (2-3 sentences) "
                "that neutrally captures the main themes raised:\n\n"
                f"Community Rationals:\n{rational_texts}\n\n"
                "Keep it balanced and under 500 characters."
            )
        else:
            prompt = (
                f"Based on the community vote ({effective_vote} won with {votes_for_effective} votes) and the following rationals from community members, "
                "generate a concise summary (2-3 sentences) that captures the main reasons for this decision:\n\n"
                f"Community Rationals:\n{rational_texts}\n\n"
                "Provide a balanced summary that reflects the community's reasoning. Keep it under 500 characters."
            )

        try:
            summary = self.model.generate_content(prompt).text.strip()
            if len(summary) > 500:
                summary = summary[:497] + "..."
            return summary
        except Exception as e:
            print(f"Error generating rational summary: {e}")
            return f"The community voted {effective_vote} based on {len(rationals)} submitted rationals."

    @check_proposals.error
    async def check_proposals_error(self, error):
        print(f"Error in check_proposals task: {error}")

    @process_ended_polls.error
    async def process_ended_polls_error(self, error):
        print(f"Error in process_ended_polls task: {error}")

    async def close(self):
        """Gracefully shutdown the bot"""
        # Cancel all running tasks
        if self.check_proposals.is_running():
            self.check_proposals.cancel()
        if self.process_ended_polls.is_running():
            self.process_ended_polls.cancel()
        await super().close()

    async def on_disconnect(self):
        print("Bot disconnected from Discord")

# Create and run bot
bot = GovernanceBot()

async def main():
    async with bot:
        await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("Error: DISCORD_BOT_TOKEN not set")
        exit(1)
    if CHANNEL_ID == 0:
        print("Error: DISCORD_CHANNEL_ID not set")
        exit(1)
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user")
    except Exception as e:
        print(f"Fatal error: {e}") 