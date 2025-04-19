# --- Imports ---
import discord
import tweepy
import requests
import os
import json
import asyncio
import time
import traceback
from io import BytesIO
from threading import Thread, Lock
from collections import deque
from flask import Flask, render_template, jsonify

# --- Configuration Loading ---
# Attempts to load from Replit Secrets first, then falls back to .env
try:
    DISCORD_BOT_TOKEN = os.environ['DISCORD_BOT_TOKEN']
    X_API_KEY = os.environ['X_API_KEY']
    X_API_SECRET = os.environ['X_API_SECRET']
    X_ACCESS_TOKEN = os.environ['X_ACCESS_TOKEN']
    X_ACCESS_TOKEN_SECRET = os.environ['X_ACCESS_TOKEN_SECRET']
    X_BEARER_TOKEN = os.environ['X_BEARER_TOKEN']
    TARGET_DISCORD_CHANNEL_ID = int(os.environ['TARGET_DISCORD_CHANNEL_ID'])
    print("Loaded configuration from Replit Secrets.")
    # Use Replit DB for mapping and state persistence
    from replit import db as state_db # Using 'state_db' to avoid confusion
    USING_REPLIT_DB = True
except KeyError:
    print("Replit Secrets not found, attempting to load from .env file...")
    from dotenv import load_dotenv
    load_dotenv()
    DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
    X_API_KEY = os.getenv('X_API_KEY')
    X_API_SECRET = os.getenv('X_API_SECRET')
    X_ACCESS_TOKEN = os.getenv('X_ACCESS_TOKEN')
    X_ACCESS_TOKEN_SECRET = os.getenv('X_ACCESS_TOKEN_SECRET')
    X_BEARER_TOKEN = os.getenv('X_BEARER_TOKEN')
    TARGET_DISCORD_CHANNEL_ID = int(os.getenv('TARGET_DISCORD_CHANNEL_ID'))
    print("Loaded configuration from .env file.")
    # Use a JSON file for mapping and state locally
    MAPPING_FILE = 'discord_bot_state.json'
    USING_REPLIT_DB = False
    state_db = {} # Placeholder dictionary

# --- Global State Variables & Lock ---
state_lock = Lock()
discord_message_to_x_post_map = {} # Stores Discord Msg ID -> X Post ID mapping
posts_attempted = 0
posts_succeeded = 0
posts_failed = 0
discord_status = "Initializing"
last_x_api_status = "Unknown"
last_x_api_timestamp = None
# Stores the last N activities (type, status, details)
recent_activity = deque(maxlen=10) # Show last 10 activities

# --- Mapping & State Persistence Functions ---
def load_state():
    """Loads mappings and counters from storage."""
    global state_db, discord_message_to_x_post_map, posts_attempted, posts_succeeded, posts_failed
    print("Loading state...")
    if USING_REPLIT_DB:
        try:
            # Load mapping (convert keys back to int if needed, though strings are safer)
            loaded_map = state_db.get("discord_message_to_x_post_map", {})
            discord_message_to_x_post_map = dict(loaded_map) # Ensure it's a dict

            # Load counters
            posts_attempted = int(state_db.get("posts_attempted", 0))
            posts_succeeded = int(state_db.get("posts_succeeded", 0))
            posts_failed = int(state_db.get("posts_failed", 0))
            print(f"Loaded state from Replit DB: {len(discord_message_to_x_post_map)} mappings, "
                  f"{posts_attempted} attempts, {posts_succeeded} successes, {posts_failed} failures.")
        except Exception as e:
            print(f"Error loading state from Replit DB: {e}. Starting fresh.")
            discord_message_to_x_post_map = {}
            posts_attempted = posts_succeeded = posts_failed = 0
    else: # Using JSON file
        try:
            with open(MAPPING_FILE, 'r') as f:
                loaded_data = json.load(f)
                discord_message_to_x_post_map = loaded_data.get("discord_message_to_x_post_map", {})
                posts_attempted = loaded_data.get("posts_attempted", 0)
                posts_succeeded = loaded_data.get("posts_succeeded", 0)
                posts_failed = loaded_data.get("posts_failed", 0)
                state_db = loaded_data # Store loaded data if needed elsewhere
                print(f"Loaded state from {MAPPING_FILE}")
        except FileNotFoundError:
            print(f"{MAPPING_FILE} not found, starting fresh.")
            discord_message_to_x_post_map = {}
            posts_attempted = posts_succeeded = posts_failed = 0
        except json.JSONDecodeError:
             print(f"Error decoding {MAPPING_FILE}, starting fresh.")
             discord_message_to_x_post_map = {}
             posts_attempted = posts_succeeded = posts_failed = 0

def save_state():
    """Saves mappings and counters to storage."""
    global state_db, discord_message_to_x_post_map, posts_attempted, posts_succeeded, posts_failed
    # print("Attempting to save state...") # Can be noisy, enable if debugging save issues
    if USING_REPLIT_DB:
        try:
            # Replit DB requires keys to be strings if using dict-like access directly
            # Using set_bulk or individual assignments might be safer
            state_db["discord_message_to_x_post_map"] = dict(discord_message_to_x_post_map) # Ensure it's a dict
            state_db["posts_attempted"] = posts_attempted
            state_db["posts_succeeded"] = posts_succeeded
            state_db["posts_failed"] = posts_failed
            # print(f"Saved state to Replit DB.") # Confirm save
        except Exception as e:
            print(f"Error saving state to Replit DB: {e}")
    else: # Using JSON file
        try:
            # Create a structure to save
            data_to_save = {
                "discord_message_to_x_post_map": discord_message_to_x_post_map,
                "posts_attempted": posts_attempted,
                "posts_succeeded": posts_succeeded,
                "posts_failed": posts_failed
            }
            with open(MAPPING_FILE, 'w') as f:
                json.dump(data_to_save, f, indent=4)
            # print(f"Saved state to {MAPPING_FILE}")
        except IOError as e:
            print(f"Error saving state to {MAPPING_FILE}: {e}")

def save_mapping_entry(discord_msg_id, x_post_id):
    """Adds or updates a single mapping entry and saves the whole state."""
    with state_lock:
        discord_message_to_x_post_map[str(discord_msg_id)] = x_post_id
        save_state() # Save immediately after modifying map

def get_x_post_id(discord_msg_id):
    """Retrieves the X post ID for a given Discord message ID."""
    with state_lock:
        return discord_message_to_x_post_map.get(str(discord_msg_id))

# --- Helper to Add Activity ---
def add_activity(activity_type, status, details=""):
    """Safely adds an entry to the recent activity log."""
    with state_lock:
        log_entry = {
            "timestamp": time.time(),
            "type": activity_type,
            "status": status,
            "details": str(details) # Ensure details are string
        }
        recent_activity.appendleft(log_entry) # Add to the beginning (newest first)
        # print(f"Logged Activity: {log_entry}") # Debug logging

# --- X (Twitter) API Setup ---
try:
    auth_v1 = tweepy.OAuth1UserHandler(X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET)
    api_v1 = tweepy.API(auth_v1)
    client_v2 = tweepy.Client(
        bearer_token=X_BEARER_TOKEN,
        consumer_key=X_API_KEY,
        consumer_secret=X_API_SECRET,
        access_token=X_ACCESS_TOKEN,
        access_token_secret=X_ACCESS_TOKEN_SECRET,
        wait_on_rate_limit=True
    )
    print("X API clients initialized.")
except Exception as e:
    print(f"FATAL ERROR: Could not initialize X API clients: {e}")
    # Consider exiting or setting a critical error state if X is essential
    client_v2 = None # Ensure client is None if setup fails

# --- Helper Function to Post to X ---
async def post_to_x(text_content, discord_media_attachments=None, reply_to_x_id=None, discord_message_id=None):
    """Posts content to X, updates state, and logs activity."""
    global posts_attempted, posts_succeeded, posts_failed
    global last_x_api_status, last_x_api_timestamp

    # Check if X client is initialized
    if client_v2 is None:
         print("X API client not initialized. Cannot post.")
         add_activity("X Post", "❌ Failed", f"Discord ID: {discord_message_id} | Error: X Client Not Initialized")
         # No need to increment posts_failed here as it wasn't an API attempt failure
         return None

    media_ids_v1 = []
    loop = asyncio.get_event_loop()
    current_timestamp = time.time()
    status_detail_info = f"Discord ID: {discord_message_id}"

    with state_lock:
        posts_attempted += 1 # Increment attempt counter

    try:
        # --- Media Handling ---
        media_upload_failed = False # Flag
        if discord_media_attachments:
            print(f"Processing {len(discord_media_attachments)} media attachments...")
            for attachment in discord_media_attachments[:4]: # Limit to 4 media
                print(f"Downloading: {attachment.filename} ({attachment.size} bytes)")
                try:
                    async with asyncio.Semaphore(5):
                        response = await loop.run_in_executor(None, requests.get, attachment.url, {'stream': True, 'timeout': 30}) # Added timeout
                        response.raise_for_status()
                        media_data = BytesIO()
                        for chunk in response.iter_content(chunk_size=8192):
                            media_data.write(chunk)
                        media_data.seek(0)

                        print(f"Uploading {attachment.filename} to X (v1.1 endpoint)...")
                        # Use run_in_executor for potentially blocking I/O with API v1.1
                        uploaded_media = await loop.run_in_executor(
                             None,
                             lambda: api_v1.media_upload(filename=attachment.filename, file=media_data)
                        )
                        media_ids_v1.append(uploaded_media.media_id_string)
                        print(f"Uploaded {attachment.filename}, Media ID: {uploaded_media.media_id_string}")

                except requests.exceptions.RequestException as e:
                    print(f"Error downloading media {attachment.filename}: {e}")
                    raise Exception(f"Media Download Failed: {attachment.filename}") from e # Re-raise specific error
                except tweepy.errors.TweepyException as e:
                    print(f"Error uploading media {attachment.filename}: {e}")
                    raise Exception(f"Media Upload Failed: {attachment.filename}") from e # Re-raise specific error
                except Exception as e: # Catch other potential errors
                    print(f"Unexpected error processing media {attachment.filename}: {e}")
                    raise Exception(f"Media Processing Failed: {attachment.filename}") from e # Re-raise specific error

        # --- Post Tweet using API v2 ---
        print("Posting tweet via API v2...")
        post_params = {"text": text_content}
        if media_ids_v1:
            post_params["media_ids"] = media_ids_v1
        if reply_to_x_id:
            post_params["in_reply_to_tweet_id"] = reply_to_x_id
            print(f"Posting as reply to X ID: {reply_to_x_id}")

        # --- THE ACTUAL API CALL ---
        response = client_v2.create_tweet(**post_params)
        new_tweet_id = response.data['id']
        status_detail_info += f" -> X ID: {new_tweet_id}"

        # --- Update State on Success ---
        with state_lock:
            posts_succeeded += 1
            last_x_api_status = "✅ Success"
            last_x_api_timestamp = current_timestamp
            save_state() # Save state after successful post increments
        add_activity("X Post", "✅ Success", status_detail_info)
        # --- End Update State ---

        print(f"Successfully posted to X. ID: {new_tweet_id} (Attempts: {posts_attempted}, Success: {posts_succeeded})")
        return new_tweet_id

    except Exception as e:
        error_type = type(e).__name__
        error_msg = str(e)
        print(f"Error during post_to_x: {error_type}: {error_msg}")
        print(traceback.format_exc(limit=2)) # Log brief traceback for context

        # Format shorter error for dashboard
        short_error = f"{error_type}"
        if isinstance(e, tweepy.errors.TweepyException) and hasattr(e, 'api_messages') and e.api_messages:
             short_error = f"{error_type}: {e.api_messages[0]}"
        elif len(error_msg) > 0:
             short_error = f"{error_type}: {error_msg[:60].replace(newline, ' ')}..." # Truncate, remove newlines

        status_detail_info += f" | Error: {short_error}"

        # --- Update State on Failure ---
        with state_lock:
            posts_failed += 1
            last_x_api_status = f"❌ Failed ({short_error})"
            last_x_api_timestamp = current_timestamp
            save_state() # Save state after failure increments
        add_activity("X Post", "❌ Failed", status_detail_info)
        # --- End Update State ---
        return None

# --- Flask Setup ---
app = Flask('')

@app.route('/')
def route_home(): # Renamed to avoid conflict with built-in 'home'
    return "Bot is alive!"

@app.route('/dashboard')
def route_dashboard(): # Renamed
    # Serves the dashboard HTML page
    return render_template('dashboard.html')

@app.route('/api/status')
def route_api_status(): # Renamed
    """Returns current bot status as JSON."""
    current_bot_status = "Running ✅" # Default assumption
    with state_lock:
        # Convert deque to list for JSON serialization (newest first)
        activity_log = list(recent_activity)
        # Copy state variables to avoid holding lock during JSON creation
        current_discord_status = discord_status
        current_last_x_api_status = last_x_api_status
        current_last_x_api_timestamp = last_x_api_timestamp
        current_posts_attempted = posts_attempted
        current_posts_succeeded = posts_succeeded
        current_posts_failed = posts_failed

    # Determine overall bot status based on component states
    if "❌ Failed" in current_last_x_api_status:
         current_bot_status = "Running with API Errors ⚠️"
    if "Disconnected" in current_discord_status:
         current_bot_status = "Discord Disconnected ❌"
    elif "Connecting" in current_discord_status:
        current_bot_status = "Discord Connecting ⚠️"
    elif client_v2 is None: # Check if X client failed to initialize
        current_bot_status = "X Client Init Failed ❌"


    status_data = {
        "discord_status": current_discord_status,
        "last_x_api_status": current_last_x_api_status,
        "last_x_api_timestamp": current_last_x_api_timestamp,
        "posts_attempted": current_posts_attempted,
        "posts_succeeded": current_posts_succeeded,
        "posts_failed": current_posts_failed,
        "recent_activity": activity_log,
        "bot_status": current_bot_status,
        "monitoring_channel": TARGET_DISCORD_CHANNEL_ID,
        "current_time_unix": time.time()
    }
    return jsonify(status_data)

def run_flask():
  """Runs the Flask app."""
  # Use try-except block for Flask run issues
  try:
      print("Starting Flask server on 0.0.0.0:8080")
      app.run(host='0.0.0.0', port=8080)
  except Exception as e:
      print(f"FATAL ERROR: Flask server failed to start: {e}")
      # Consider how to handle this - maybe sys.exit()?

# --- Discord Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True # REQUIRED
intents.messages = True
intents.guilds = True

client = discord.Client(intents=intents)

# --- Discord Event Handlers ---
@client.event
async def on_ready():
    global discord_status
    with state_lock:
        discord_status = "Connected"
    # Load state *after* lock is acquired if needed, or before is fine too
    load_state() # Load previous state (mappings, counts)
    add_activity("Discord", "✅ Connected", f"Logged in as {client.user.name}")
    print(f'Logged in as {client.user.name} ({client.user.id})')
    print(f'Discord Status: {discord_status}')
    print(f'Monitored Channel: {TARGET_DISCORD_CHANNEL_ID}')
    print('------ Bot Ready ------')

@client.event
async def on_disconnect():
    global discord_status
    with state_lock:
        discord_status = "Disconnected"
    add_activity("Discord", "❌ Disconnected")
    print(f'Bot disconnected from Discord.')

@client.event
async def on_connect():
    global discord_status
    with state_lock:
        discord_status = "Connecting"
    # Optional: add_activity("Discord", "⚠️ Connecting")
    print(f'Bot connecting to Discord...')

@client.event
async def on_resumed():
    global discord_status
    with state_lock:
        discord_status = "Connected (Resumed)"
    add_activity("Discord", "✅ Resumed")
    print(f'Bot session resumed.')

@client.event
async def on_message(message):
    """Handles incoming messages."""
    # Ignore messages from the bot itself
    if message.author == client.user:
        return

    # Check if the message is in the target channel OR a thread within it
    is_in_target_channel = message.channel.id == TARGET_DISCORD_CHANNEL_ID
    is_in_target_thread = isinstance(message.channel, discord.Thread) and message.channel.parent_id == TARGET_DISCORD_CHANNEL_ID

    if not (is_in_target_channel or is_in_target_thread):
        return # Ignore messages elsewhere

    print(f"\n--- Relevant Message Detected ---")
    print(f"Msg ID: {message.id} | Author: {message.author.name} | Channel: {message.channel.name}")

    # Determine if it's a reply and get the original X post ID
    original_x_post_id = None
    if is_in_target_thread:
        original_discord_message_id = message.channel.id
        print(f"Message is in thread, original Discord Msg ID: {original_discord_message_id}")
        original_x_post_id = get_x_post_id(original_discord_message_id)
        if original_x_post_id:
             print(f"Found corresponding X Post ID: {original_x_post_id}")
        else:
             print(f"Warning: Could not find original X Post ID for thread starter {original_discord_message_id}. Posting as new tweet.")
             add_activity("Warning", "⚠️ Mapping", f"Missing X ID for Discord thread starter {original_discord_message_id}")


    # Prepare content
    x_content = message.content
    if not x_content and not message.attachments:
        print("Message has no text content or attachments, skipping X post.")
        add_activity("Info", "⚪ Skipped", f"Discord ID: {message.id} - Empty message")
        return

    # --- Call Post to X ---
    new_x_post_id = await post_to_x(
        text_content=x_content,
        discord_media_attachments=message.attachments,
        reply_to_x_id=original_x_post_id,
        discord_message_id=message.id     # Pass Discord message ID for logging
    )

    # --- Save Mapping for Initial Posts ONLY ---
    # Only save if it's a NEW message in the main channel (not a thread reply) AND posting succeeded
    if is_in_target_channel and not is_in_target_thread and new_x_post_id:
        save_mapping_entry(message.id, new_x_post_id)
        print(f"Saved mapping for initial message: Discord {message.id} -> X {new_x_post_id}")

    print("--- Message Handling Complete ---")


# --- Run the Bot ---
if not DISCORD_BOT_TOKEN or not all([X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET, X_BEARER_TOKEN]) or not TARGET_DISCORD_CHANNEL_ID:
    print("❌ FATAL ERROR: Missing one or more required configuration variables (Tokens, Keys, Channel ID). Check Secrets/.env.")
    add_activity("System", "❌ Error", "Missing critical configuration")
    # Optionally exit if config is missing
    # import sys
    # sys.exit(1)
else:
    try:
        print("Starting Flask server in a background thread...")
        flask_thread = Thread(target=run_flask)
        flask_thread.daemon = True # Allow program to exit even if thread is running
        flask_thread.start()

        # Brief pause to potentially allow Flask to start logging before Discord client
        # time.sleep(1)

        print("Starting Discord client...")
        add_activity("System", "▶️ Starting", "Initiating Discord client connection")
        # This call blocks the main thread until the bot stops
        client.run(DISCORD_BOT_TOKEN)

    except discord.errors.LoginFailure:
        print("❌ FATAL ERROR: Invalid Discord Bot Token. Check DISCORD_BOT_TOKEN.")
        add_activity("System", "❌ Error", "Discord Login Failure - Invalid Token")
    except Exception as e:
        # Catch other potential errors during startup
        print(f"❌ FATAL ERROR during bot startup: {e}")
        print(traceback.format_exc())
        add_activity("System", "❌ Error", f"Bot Startup Failed: {e}")

    # Code here will run after the bot stops (e.g., Ctrl+C or client.close())
    print("Discord client stopped.")
    add_activity("System", "⏹️ Stopped", "Discord client has stopped.")