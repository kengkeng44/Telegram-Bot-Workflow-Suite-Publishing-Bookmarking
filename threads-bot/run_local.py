"""本地執行用：自動載入 .env 然後啟動 bot"""
from dotenv import load_dotenv
load_dotenv()

from bot import main
main()
