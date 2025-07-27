- Always restart the bot after making changes to it
- The server restarts on file changes, the discord bot does not

## Starting the Application

To start both the web server and Discord bot:

1. **Start the web server** (auto-restarts on file changes):
   ```bash
   uv run python main.py > logs/app.log 2>&1 &
   ```

2. **Start the Discord bot** (manual restart required):
   ```bash
   uv run python -m smarter_dev.bot.client > logs/bot.log 2>&1 &
   ```

3. **Check status**:
   - Web server logs: `tail -f logs/app.log`
   - Bot logs: `tail -f logs/bot.log`

4. **Stop processes**:
   ```bash
   pkill -f "main.py"
   pkill -f "smarter_dev.bot.client"
   ```