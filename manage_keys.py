#!/usr/bin/env python3
"""
Manage API keys for Pi Music Server.
Usage:
  python manage_keys.py add <username> <key>
  python manage_keys.py remove <username>
  python manage_keys.py list
  python manage_keys.py gen <username>   # auto-generate a key
"""
import sys, json, secrets
from pathlib import Path

KEYS_FILE = Path("api_keys.json")

def load():
    if not KEYS_FILE.exists(): return {"keys": {}}
    return json.loads(KEYS_FILE.read_text())

def save(data):
    KEYS_FILE.write_text(json.dumps(data, indent=2))
    print("✅ Saved.")

def main():
    args = sys.argv[1:]
    if not args: sys.exit(__doc__)
    cmd = args[0]
    data = load()
    keys = data["keys"]

    if cmd == "list":
        if not keys: print("No keys.")
        for u, k in keys.items(): print(f"  {u}: {k}")
    elif cmd == "add":
        if len(args) < 3: sys.exit("Usage: add <username> <key>")
        keys[args[1]] = args[2]; save(data)
        print(f"Added user '{args[1]}'.")
    elif cmd == "gen":
        if len(args) < 2: sys.exit("Usage: gen <username>")
        key = secrets.token_urlsafe(24)
        keys[args[1]] = key; save(data)
        print(f"Generated key for '{args[1]}':\n  {key}")
    elif cmd == "remove":
        if len(args) < 2: sys.exit("Usage: remove <username>")
        if args[1] in keys: del keys[args[1]]; save(data); print(f"Removed '{args[1]}'.")
        else: print("User not found.")
    else:
        sys.exit(__doc__)

if __name__ == "__main__":
    main()
