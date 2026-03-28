"""
Run this once locally to get your Spotify refresh token.
You only need to do this one time — the refresh token doesn't expire.

Usage:
    python get_refresh_token.py
"""
from spotipy.oauth2 import SpotifyOAuth

CLIENT_ID = input("Spotify Client ID: ").strip()
CLIENT_SECRET = input("Spotify Client Secret: ").strip()

sp_oauth = SpotifyOAuth(
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    redirect_uri="http://127.0.0.1:8888/callback",
    scope="playlist-modify-public playlist-modify-private",
)

auth_url = sp_oauth.get_authorize_url()
print(f"\n1. Open this URL in your browser:\n\n   {auth_url}\n")
print("2. After approving, you'll be redirected to a URL starting with http://127.0.0.1:8888/callback")
print("   (the page will fail to load — that's fine)\n")

response = input("3. Paste the full redirect URL here: ").strip()
code = sp_oauth.parse_response_code(response)
token_info = sp_oauth.get_access_token(code, as_dict=True)

print(f"\nYour refresh token:\n\n  {token_info['refresh_token']}\n")
print("Add this as SPOTIFY_REFRESH_TOKEN in your GitHub repository secrets.")
