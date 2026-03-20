# WuxiaWorld Daily Auto Sign-In Bot

Automates daily check-in and mission reward collection on [wuxiaworld.com](https://www.wuxiaworld.com) using Playwright.

## Features

- 🔐 Email + password login with session persistence
- ✅ Daily attendance check-in
- 🎯 Auto-claim available mission rewards
- 📸 Error screenshots for debugging
- ⏰ Runs automatically via GitHub Actions (daily at 5:00 AM UTC)

## GitHub Actions Setup

1. **Fork / push** this repo to GitHub.
2. Go to **Settings → Secrets and variables → Actions**.
3. Add two **Repository Secrets**:
   | Secret Name   | Value                  |
   |---------------|------------------------|
   | `WW_EMAIL`    | Your WuxiaWorld email   |
   | `WW_PASSWORD` | Your WuxiaWorld password |
4. The bot will run automatically every day. You can also trigger it manually from the **Actions** tab → **Run workflow**.

## Local Usage

```bash
pip install -r requirements.txt
playwright install chromium

export WW_EMAIL="your_email@example.com"
export WW_PASSWORD="your_password"

python wuxiaworld_bot.py
```

## License

MIT
