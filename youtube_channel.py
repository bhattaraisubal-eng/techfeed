"""Call the YouTube Data API channels.list endpoint and print the response.

Usage:
    Put YOUTUBE_API_KEY=your-key in a .env file next to this script, then:
    python3 youtube_channel.py UC3kf-QFT6FZzDk9JsPg8Svg
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_URL = "https://www.googleapis.com/youtube/v3/channels"
DEFAULT_CHANNEL_ID = "UC3kf-QFT6FZzDk9JsPg8Svg"  # Hello Interview
ENV_FILE = Path(__file__).parent / ".env"


def load_env_file(path: Path) -> None:
    """Read KEY=VALUE lines from a .env file into os.environ.

    Variables already set in the real environment win, so you can still
    override the file with `export YOUTUBE_API_KEY=...`.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> None:
    load_env_file(ENV_FILE)
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        sys.exit(f"YOUTUBE_API_KEY not found. Add it to {ENV_FILE} or export it.")

    channel_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CHANNEL_ID

    query = urllib.parse.urlencode({
        "part": "snippet,contentDetails,statistics",
        "id": channel_id,
        "key": api_key,
    })

    try:
        with urllib.request.urlopen(f"{API_URL}?{query}", timeout=10) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        # YouTube puts error details in the response body as JSON.
        sys.exit(f"HTTP {error.code}:\n{error.read().decode()}")
    except TimeoutError:
        sys.exit("Request to YouTube timed out after 10 seconds.")
    except urllib.error.URLError as error:
        if isinstance(error.reason, TimeoutError):
            sys.exit("Request to YouTube timed out after 10 seconds.")
        sys.exit(f"Could not reach YouTube: {error.reason}")

    items = data.get("items", [])
    if not items:
        sys.exit(f"No channel found for id {channel_id}")

    
    print(json.dumps({"channelid": data["items"][0]["id"], "description": data["items"][0]["snippet"]["description"]}, indent=2))


if __name__ == "__main__":
    main()
