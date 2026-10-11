# Discord Panel Bot

## Setup
1. pip install -r requirements.txt
2. Set your token:
   - Windows (PowerShell): $env:DISCORD_TOKEN="your_token"
   - Linux/Mac: export DISCORD_TOKEN="your_token"
   (or edit TOKEN in bot.py)
3. python bot.py

## Commands (admin only)
- /setuppanel name description script role
- /createkey panel [amount]

Invite the bot with the scopes: bot + applications.commands.
Keep the bot's role above the role it should assign.
