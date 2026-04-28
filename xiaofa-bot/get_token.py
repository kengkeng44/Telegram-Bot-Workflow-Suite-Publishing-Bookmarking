import os
import requests
import webbrowser
from dotenv import load_dotenv

load_dotenv()

APP_ID = os.getenv("THREADS_APP_ID")
APP_SECRET = os.getenv("THREADS_APP_SECRET")
REDIRECT_URI = os.getenv("THREADS_REDIRECT_URI", "https://localhost")

if not APP_ID or not APP_SECRET:
    raise SystemExit("請在 .env 設定 THREADS_APP_ID 與 THREADS_APP_SECRET（從 developers.facebook.com 你的 app 拿）")

auth_url = (
    f"https://threads.net/oauth/authorize"
    f"?client_id={APP_ID}"
    f"&redirect_uri={REDIRECT_URI}"
    f"&scope=threads_basic,threads_content_publish"
    f"&response_type=code"
)

print("開啟瀏覽器授權...")
webbrowser.open(auth_url)

print("\n授權後瀏覽器會跳到錯誤頁面，把網址列完整網址貼過來：")
redirect_url = input("> ").strip()

code = redirect_url.split("code=")[1].split("#")[0]

res = requests.post("https://graph.threads.net/oauth/access_token", data={
    "client_id": APP_ID,
    "client_secret": APP_SECRET,
    "grant_type": "authorization_code",
    "redirect_uri": REDIRECT_URI,
    "code": code,
})
short_token = res.json()["access_token"]
user_id = res.json()["user_id"]

res2 = requests.get("https://graph.threads.net/access_token", params={
    "grant_type": "th_exchange_token",
    "client_secret": APP_SECRET,
    "access_token": short_token,
})
long_token = res2.json()["access_token"]
print(f"\n長效 Token：{long_token}")
print(f"User ID：{user_id}")
