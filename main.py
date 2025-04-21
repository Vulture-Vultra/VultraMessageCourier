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
import logging # Added for cleaner logging control

# --- Basic Logging Setup ---
# Configure logging to provide clearer output than just print statements
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
# Suppress noisy logs from libraries if needed (optional)
logging.getLogger('discord.gateway').setLevel(logging.WARNING)
logging.getLogger('discord.client').setLevel(logging.WARNING)
logging.getLogger('discord.http').setLevel(logging.WARNING)
logging.getLogger('werkzeug').setLevel(logging.WARNING) # Quieten Flask dev server logs slightly

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
    logging.info("Loaded configuration from Replit Secrets.")
    # Use Replit DB for mapping and state persistence
    from replit import db as state_db # Using 'state_db' to avoid confusion
    USING_REPLIT_DB = True
except KeyError:
    logging.warning("Replit Secrets not found, attempting to load from .env file...")
    try:
        from dotenv import load_dotenv
        if load_dotenv():
            logging.info("Loaded variables from .env file.")
        else:
            logging.warning(".env file not found or empty.")

        DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
        X_API_KEY = os.getenv('X_API_KEY')
        X_API_SECRET = os.getenv('X_API_SECRET')
        X_ACCESS_TOKEN = os.getenv('X_ACCESS_TOKEN')
        X_ACCESS_TOKEN_SECRET = os.getenv('X_ACCESS_TOKEN_SECRET')
        X_BEARER_TOKEN = os.getenv('X_BEARER_TOKEN')
        # Provide default 0 if TARGET_DISCORD_CHANNEL_ID is missing in .env
        TARGET_DISCORD_CHANNEL_ID = int(os.getenv('TARGET_DISCORD_CHANNEL_ID', 0))
        logging.info("Configured to use local JSON file for state.")
        # Use a JSON file for mapping and state locally
        MAPPING_FILE = 'discord_bot_state.json'
        USING_REPLIT_DB = False
        state_db = {} # Placeholder dictionary

    except ImportError:
        logging.error("dotenv library not found. Install with 'pip install python-dotenv'")
        DISCORD_BOT_TOKEN = None # Ensure vars are None if loading fails
    except Exception as e:
        logging.error(f"Error loading config from .env: {e}")
        DISCORD_BOT_TOKEN = None


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
    logging.info("Loading application state...")
    if USING_REPLIT_DB:
        try:
            # Load mapping
            loaded_map = state_db.get("discord_message_to_x_post_map", {})
            discord_message_to_x_post_map = {str(k): v for k, v in loaded_map.items()}

            # Load counters
            posts_attempted = int(state_db.get("posts_attempted", 0))
            posts_succeeded = int(state_db.get("posts_succeeded", 0))
            posts_failed = int(state_db.get("posts_failed", 0))
            logging.info(f"Loaded state from Replit DB: {len(discord_message_to_x_post_map)} mappings, "
                         f"{posts_attempted} attempts, {posts_succeeded} successes, {posts_failed} failures.")
        except Exception as e:
            logging.error(f"Error loading state from Replit DB: {e}. Starting fresh.", exc_info=True)
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
                logging.info(f"Loaded state from {MAPPING_FILE}")
        except FileNotFoundError:
            logging.warning(f"{MAPPING_FILE} not found, starting fresh.")
            discord_message_to_x_post_map = {}
            posts_attempted = posts_succeeded = posts_failed = 0
        except json.JSONDecodeError:
             logging.error(f"Error decoding {MAPPING_FILE}, starting fresh.")
             discord_message_to_x_post_map = {}
             posts_attempted = posts_succeeded = posts_failed = 0
        except Exception as e:
             logging.error(f"Unexpected error loading state from {MAPPING_FILE}: {e}", exc_info=True)
             discord_message_to_x_post_map = {}
             posts_attempted = posts_succeeded = posts_failed = 0

def save_state():
    """Saves mappings and counters to storage."""
    global state_db, discord_message_to_x_post_map, posts_attempted, posts_succeeded, posts_failed
    with state_lock:
        if USING_REPLIT_DB:
            try:
                state_db["discord_message_to_x_post_map"] = dict(discord_message_to_x_post_map)
                state_db["posts_attempted"] = posts_attempted
                state_db["posts_succeeded"] = posts_succeeded
                state_db["posts_failed"] = posts_failed
                logging.debug(f"Saved state to Replit DB.")
            except Exception as e:
                logging.error(f"Error saving state to Replit DB: {e}", exc_info=True)
        else: # Using JSON file
            try:
                data_to_save = {
                    "discord_message_to_x_post_map": discord_message_to_x_post_map,
                    "posts_attempted": posts_attempted,
                    "posts_succeeded": posts_succeeded,
                    "posts_failed": posts_failed
                }
                with open(MAPPING_FILE, 'w') as f:
                    json.dump(data_to_save, f, indent=4)
                logging.debug(f"Saved state to {MAPPING_FILE}")
            except IOError as e:
                logging.error(f"Error saving state to {MAPPING_FILE}: {e}", exc_info=True)
            except Exception as e:
                logging.error(f"Unexpected error saving state to {MAPPING_FILE}: {e}", exc_info=True)

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
        try:
            log_entry = {
                "timestamp": time.time(),
                "type": str(activity_type),
                "status": str(status),
                "details": str(details)
            }
            recent_activity.appendleft(log_entry)
            logging.debug(f"Logged Activity: {log_entry['type']} - {log_entry['status']}")
        except Exception as e:
            logging.error(f"Error adding activity log entry: {e}", exc_info=True)

# --- X (Twitter) API Setup ---
api_v1 = None
client_v2 = None
try:
    if X_API_KEY and X_API_SECRET and X_ACCESS_TOKEN and X_ACCESS_TOKEN_SECRET and X_BEARER_TOKEN:
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
        logging.info("X API clients initialized successfully.")
    else:
        logging.error("One or more X API credentials missing, skipping initialization.")

except tweepy.errors.TweepyException as e:
     logging.error(f"FATAL ERROR: TweepyException during X API client initialization: {e}", exc_info=True)
     add_activity("System", "❌ Error", f"X Init Failed: {e}")
except Exception as e:
    logging.error(f"FATAL ERROR: Unexpected error during X API client initialization: {e}", exc_info=True)
    add_activity("System", "❌ Error", f"X Init Failed: {e}")

# --- Helper Function to Post to X ---
async def post_to_x(text_content, discord_media_attachments=None, reply_to_x_id=None, discord_message_id=None):
    """Posts content to X, updates state, and logs activity."""
    global posts_attempted, posts_succeeded, posts_failed
    global last_x_api_status, last_x_api_timestamp

    if client_v2 is None or api_v1 is None:
        logging.error("X API client(s) not initialized. Cannot post.")
        add_activity("X Post", "❌ Failed", f"Discord ID: {discord_message_id} | Error: X Client Not Initialized")
        return None

    media_ids_v1 = []
    loop = asyncio.get_event_loop()
    current_timestamp = time.time()
    status_detail_info = f"Discord ID: {discord_message_id}" if discord_message_id else "Unknown Discord ID"

    with state_lock:
        posts_attempted += 1

    try:
        # --- Media Handling ---
        if discord_media_attachments:
            logging.info(f"Processing {len(discord_media_attachments)} media attachments for Msg {discord_message_id}...")
            processed_media_count = 0
            for attachment in discord_media_attachments:
                if processed_media_count >= 4:
                    logging.warning(f"Max 4 media items reached. Skipping further attachments for Msg {discord_message_id}.")
                    break
                logging.info(f"Downloading: {attachment.filename} ({attachment.size} bytes)")
                try:
                    response = await loop.run_in_executor(None, lambda: requests.get(attachment.url, stream=True, timeout=45))
                    response.raise_for_status()
                    media_data = BytesIO()
                    for chunk in response.iter_content(chunk_size=8192):
                        media_data.write(chunk)
                    media_data.seek(0)
                    logging.info(f"Finished downloading {attachment.filename}.")

                    logging.info(f"Uploading {attachment.filename} to X...")
                    uploaded_media = await loop.run_in_executor(
                         None,
                         lambda: api_v1.media_upload(filename=attachment.filename, file=media_data)
                    )
                    media_ids_v1.append(uploaded_media.media_id_string)
                    logging.info(f"Uploaded {attachment.filename}, Media ID: {uploaded_media.media_id_string}")
                    processed_media_count += 1
                except requests.exceptions.RequestException as e:
                    logging.error(f"Error downloading media {attachment.filename}: {e}", exc_info=True)
                    raise Exception(f"Media Download Failed: {attachment.filename}") from e
                except tweepy.errors.TweepyException as e:
                    logging.error(f"Error uploading media {attachment.filename}: {e}", exc_info=True)
                    raise Exception(f"Media Upload Failed: {attachment.filename}") from e
                except Exception as e:
                    logging.error(f"Unexpected error processing media {attachment.filename}: {e}", exc_info=True)
                    raise Exception(f"Media Processing Failed: {attachment.filename}") from e

        # --- Post Tweet using API v2 ---
        logging.info(f"Preparing to post tweet for Msg {discord_message_id}...")
        post_params = {"text": text_content}
        if media_ids_v1:
            post_params["media_ids"] = media_ids_v1
            logging.info(f"Attaching media IDs: {media_ids_v1}")
        if reply_to_x_id:
            post_params["in_reply_to_tweet_id"] = reply_to_x_id
            logging.info(f"Posting as reply to X ID: {reply_to_x_id}")

        logging.info(f"Sending create_tweet request...")
        response = client_v2.create_tweet(**post_params)
        new_tweet_id = response.data['id']
        status_detail_info += f" -> X ID: {new_tweet_id}"

        # --- Update State on Success ---
        with state_lock:
            posts_succeeded += 1
            last_x_api_status = "✅ Success"
            last_x_api_timestamp = current_timestamp
        add_activity("X Post", "✅ Success", status_detail_info)
        save_state()
        # --- End Update State ---

        logging.info(f"Successfully posted to X. ID: {new_tweet_id} (Attempts: {posts_attempted}, Success: {posts_succeeded})")
        return new_tweet_id

    except Exception as e:
        error_type = type(e).__name__
        error_msg = str(e).replace('\n', ' ')
        logging.error(f"Error during post_to_x for Msg {discord_message_id}: {error_type}: {error_msg}", exc_info=True)

        short_error = f"{error_type}"
        if isinstance(e, tweepy.errors.TweepyException) and hasattr(e, 'api_messages') and e.api_messages:
            short_error = f"{error_type}: {e.api_messages[0]}"
        elif len(error_msg) > 0:
            short_error = f"{error_type}: {error_msg[:70]}..."

        if "Error:" not in status_detail_info:
            status_detail_info += f" | Error: {short_error}"

        # --- Update State on Failure ---
        with state_lock:
            posts_failed += 1
            last_x_api_status = f"❌ Failed ({short_error})"
            last_x_api_timestamp = current_timestamp
        add_activity("X Post", "❌ Failed", status_detail_info)
        save_state()
        # --- End Update State ---
        return None

# --- Flask Setup ---
app = Flask('')

@app.route('/')
def route_home():
    """Basic route for UptimeRobot or basic check."""
    return "Bot is alive!"

@app.route('/dashboard')
def route_dashboard():
    """Serves the dashboard HTML page."""
    try:
        return render_template('dashboard.html')
    except Exception as e:
        logging.error(f"Error rendering dashboard template: {e}", exc_info=True)
        return "Error loading dashboard.", 500

@app.route('/api/status')
def route_api_status():
    """Returns current bot status as JSON."""
    current_bot_status = "Running ✅"
    with state_lock:
        activity_log = list(recent_activity)
        current_discord_status = discord_status
        current_last_x_api_status = last_x_api_status
        current_last_x_api_timestamp = last_x_api_timestamp
        current_posts_attempted = posts_attempted
        current_posts_succeeded = posts_succeeded
        current_posts_failed = posts_failed

    if client_v2 is None or api_v1 is None:
        current_bot_status = "X Client Init Failed ❌"
    elif "❌ Failed" in current_last_x_api_status:
         current_bot_status = "Running with API Errors ⚠️"
    if "Disconnected" in current_discord_status:
         current_bot_status = "Discord Disconnected ❌"
    elif "Connecting" in current_discord_status:
        current_bot_status = "Discord Connecting ⚠️"

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
    try:
        return jsonify(status_data)
    except Exception as e:
        logging.error(f"Error creating JSON for API status: {e}", exc_info=True)
        return jsonify({"error": "Failed to generate status JSON"}), 500

def run_flask():
    """Runs the Flask app in a separate thread."""
    try:
        logging.info("Starting Flask server thread on 0.0.0.0:8080")
        app.run(host='0.0.0.0', port=8080, debug=False)
    except Exception as e:
        logging.critical(f"FATAL ERROR: Flask server thread failed to start: {e}", exc_info=True)

# --- Discord Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True # REQUIRED
intents.messages = True
intents.guilds = True
# Explicitly add presence/members if needed and enabled in portal
# intents.presences = True
# intents.members = True

client = discord.Client(intents=intents)

# --- Discord Event Handlers ---
@client.event
async def on_ready():
    """Called when the bot is ready and connected to Discord."""
    global discord_status
    load_state()
    with state_lock:
        discord_status = "Connected"
    add_activity("Discord", "✅ Connected", f"Logged in as {client.user.name}")
    logging.info(f'Logged in successfully as {client.user.name} ({client.user.id})')
    logging.info(f'Discord Status: {discord_status}')
    logging.info(f'Monitored Channel ID: {TARGET_DISCORD_CHANNEL_ID}')
    logging.info('------ Bot Ready ------')

@client.event
async def on_disconnect():
    """Called when the bot loses connection to Discord."""
    global discord_status
    with state_lock:
        discord_status = "Disconnected"
    add_activity("Discord", "❌ Disconnected", "Lost connection to gateway")
    logging.warning('Bot disconnected from Discord.')

@client.event
async def on_connect():
    """Called when the bot initially connects (before ready)."""
    global discord_status
    with state_lock:
        discord_status = "Connecting"
    logging.info('Bot connecting to Discord gateway...')

@client.event
async def on_resumed():
    """Called when the bot resumes a session after connection loss."""
    global discord_status
    with state_lock:
        discord_status = "Connected (Resumed)"
    add_activity("Discord", "✅ Resumed", "Session resumed")
    logging.info('Bot session resumed successfully.')

@client.event
async def on_message(message: discord.Message):
    """Handles incoming messages."""
    # 1. Ignore self
    if message.author == client.user:
        return

    # 2. Check target context
    is_in_target_channel = message.channel.id == TARGET_DISCORD_CHANNEL_ID
    is_in_target_thread = isinstance(message.channel, discord.Thread) and message.channel.parent_id == TARGET_DISCORD_CHANNEL_ID

    if not (is_in_target_channel or is_in_target_thread):
        return # Ignore messages elsewhere

    logging.info(f"Relevant message detected: ID {message.id} | Author: {message.author.name} | In Thread: {is_in_target_thread}")

    # 3. Determine reply target
    original_x_post_id = None
    post_as_reply_to = None # Default: posts as new tweet if no mapping found
    is_initial_message = False

    if is_in_target_thread:
        original_discord_message_id = message.channel.id
        logging.info(f"Message is reply in thread. Original Discord Msg ID: {original_discord_message_id}")
        original_x_post_id = get_x_post_id(original_discord_message_id)

        if original_x_post_id:
            logging.info(f"Found corresponding X Post ID for reply: {original_x_post_id}")
            post_as_reply_to = original_x_post_id # Set the reply target for the current message
        else:
            # --- IMPLEMENTATION OF OPTION B ---
            # Original X Post ID not found for this thread's starter message
            logging.warning(f"Original X Post ID not found for thread starter {original_discord_message_id}. **Ignoring this reply.**")
            add_activity("Warning", "⚠️ Ignored Reply", f"Discord ID: {message.id} - Original X post mapping missing for thread {original_discord_message_id}")
            return # Stop processing this message entirely
            # --- END OF IMPLEMENTATION ---

    elif is_in_target_channel:
        is_initial_message = True
        logging.info(f"Message is initial post in channel. Discord Msg ID: {message.id}")
        # post_as_reply_to remains None

    # 4. Prepare content (check if empty)
    x_content = message.content
    if not x_content and not message.attachments:
        logging.warning(f"Msg ID: {message.id} has no text or attachments, skipping X post.")
        add_activity("Info", "⚪ Skipped", f"Discord ID: {message.id} - Empty message")
        return

    # 5. Call Post to X
    logging.debug(f"Calling post_to_x for Msg ID: {message.id} | Reply Target: {post_as_reply_to}")
    new_x_post_id = await post_to_x(
        text_content=x_content,
        discord_media_attachments=message.attachments,
        reply_to_x_id=post_as_reply_to, # Use the determined reply target ID (or None)
        discord_message_id=message.id   # Pass Discord message ID for logging
    )

    # 6. Save Mapping for Initial Posts ONLY if successful
    if is_initial_message and new_x_post_id:
        logging.info(f"Saving mapping for initial message: Discord {message.id} -> X {new_x_post_id}")
        save_mapping_entry(message.id, new_x_post_id)

    logging.info(f"--- Message Handling Complete for Msg ID: {message.id} ---")


# --- Main Execution Block ---
if __name__ == "__main__":
    # Check essential configuration before starting
    if not DISCORD_BOT_TOKEN or not all([X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET, X_BEARER_TOKEN]) or not TARGET_DISCORD_CHANNEL_ID:
        logging.critical("❌ FATAL ERROR: Missing one or more required configuration variables.")
        logging.critical("Please check DISCORD_BOT_TOKEN, X API Keys/Tokens, and TARGET_DISCORD_CHANNEL_ID in Replit Secrets or .env file.")
        add_activity("System", "❌ Error", "Missing critical configuration")
        import sys
        sys.exit(1) # Exit if essential config is missing

    # Start Flask server in a background thread
    try:
        logging.info("Starting Flask server in a background thread...")
        flask_thread = Thread(target=run_flask, name="FlaskThread")
        flask_thread.daemon = True
        flask_thread.start()
    except Exception as e:
        logging.critical(f"Failed to start Flask thread: {e}", exc_info=True)
        # Decide if bot should run without Flask
        # import sys
        # sys.exit(1) # Or exit if Flask is critical

    time.sleep(1) # Brief pause

    # Start Discord client in the main thread
    try:
        logging.info("Starting Discord client...")
        add_activity("System", "▶️ Starting", "Initiating Discord client connection")
        # Use root logger configured earlier by passing None
        client.run(DISCORD_BOT_TOKEN, log_handler=None)

    except discord.errors.LoginFailure:
        logging.critical("❌ FATAL ERROR: Invalid Discord Bot Token. Login failed.")
        add_activity("System", "❌ Error", "Discord Login Failure - Invalid Token")
    except discord.errors.PrivilegedIntentsRequired:
        logging.critical("❌ FATAL ERROR: Privileged Intents (like Message Content) are required but not enabled.")
        logging.critical("Go to your Discord Developer Portal -> Bot -> Privileged Gateway Intents.")
        add_activity("System", "❌ Error", "Discord Privileged Intents Required")
    except Exception as e:
        logging.critical(f"❌ FATAL ERROR during Discord client execution: {e}", exc_info=True)
        add_activity("System", "❌ Error", f"Discord Client Failed: {e}")

    # --- Bot Shutdown ---
    logging.info("Discord client has stopped. Shutting down.")
    add_activity("System", "⏹️ Stopped", "Discord client has stopped.")
    # Perform any cleanup here if necessary
