"""Run ONCE on your own computer to get a YouTube refresh token.
1. Google Cloud Console: create project, enable "YouTube Data API v3",
   OAuth consent screen (External) -> PUBLISH APP (In production, otherwise
   the token expires in 7 days), create OAuth client "Desktop app",
   download it as client_secret.json next to this file.
2. pip install google-auth-oauthlib
3. python get_refresh_token.py   -> copy the printed values into GitHub secrets.
"""
import json
from google_auth_oauthlib.flow import InstalledAppFlow

flow = InstalledAppFlow.from_client_secrets_file(
    "client_secret.json", scopes=["https://www.googleapis.com/auth/youtube.upload"])
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
print("YT_REFRESH_TOKEN =", creds.refresh_token)
cfg = json.load(open("client_secret.json"))["installed"]
print("YT_CLIENT_ID     =", cfg["client_id"])
print("YT_CLIENT_SECRET =", cfg["client_secret"])
