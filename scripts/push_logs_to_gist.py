import json
import os
import urllib.request

token = os.environ["GH_GIST_TOKEN"]
gist_id = os.environ["GIST_ID"]

files = {}
for fname in ["trades.csv", "daily_pnl.csv", "bot.log", ".last_briefing_date"]:
    path = f"logs/{fname}"
    if os.path.exists(path):
        with open(path) as f:
            content = f.read()
        files[fname] = {"content": content or " "}

if not files:
    print("No log files to push")
    exit(0)

payload = json.dumps({"files": files}).encode()
req = urllib.request.Request(
    f"https://api.github.com/gists/{gist_id}",
    data=payload,
    method="PATCH",
    headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
    },
)
with urllib.request.urlopen(req) as resp:
    result = json.loads(resp.read())
    print(f"Gist updated: {result['html_url']}")
