import json
import os
import urllib.request

token = os.environ["GH_GIST_TOKEN"]
gist_id = os.environ["GIST_ID"]

req = urllib.request.Request(
    f"https://api.github.com/gists/{gist_id}",
    headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    },
)

os.makedirs("logs", exist_ok=True)

with urllib.request.urlopen(req) as resp:
    gist = json.loads(resp.read())

for fname, file_data in gist.get("files", {}).items():
    content = file_data.get("content", "")
    if content and content.strip():
        path = f"logs/{fname}"
        with open(path, "w") as f:
            f.write(content)
        print(f"Restored {path} ({len(content)} bytes)")
