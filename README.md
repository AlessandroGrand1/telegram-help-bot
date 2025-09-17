# Telegram Help Bot
Paste links/files → tag with #hashtags → search or use inline picker (`@bitgpt_help_bot `). 

## Quick start
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env  # fill BOT_TOKEN, ADMIN_IDS, TARGET_CHAT_ID (optional)
python app.py
