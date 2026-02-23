# Bittensor Burn Rate Monitor

Monitors Bittensor subnet burn rates and sends Telegram notifications when they change.

## Setup

1. **Create and activate virtual environment:**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Create Telegram Bot:**
   - Message [@BotFather](https://t.me/BotFather) on Telegram
   - Send `/newbot` and follow instructions
   - Copy the bot token

4. **Add bot to your channel:**
   - Create a public channel or use existing one
   - Add your bot as an administrator with "Post Messages" permission

5. **Configure credentials:**
   Copy `.env.example` to `.env` and add your Telegram credentials:
   ```bash
   cp .env.example .env
   ```
   
   Edit `.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
   TELEGRAM_CHAT_ID=@your_channel_username
   ```

6. **Configure monitoring options:**
   Edit `config.json`:
   ```json
   {
       "netuids": "all",
       "network": "finney",
       "check_interval_seconds": 3600
   }
   ```
   
   - `netuids`: Use `"all"` to monitor all subnets, or a list like `[1, 18, 19]` for specific ones
   - `check_interval_seconds`: How often to check (3600 = 1 hour)

## Usage

**Test notification:**
```bash
python burn_monitor.py --test
```

**Run once:**
```bash
python burn_monitor.py --once
```

**Run continuously:**
```bash
python burn_monitor.py
```

## Files

- `.env` - Telegram credentials (not committed to git)
- `.env.example` - Example environment file
- `config.json` - Configuration (subnets, network, interval)
- `burn_data.json` - Stored burn rate history (auto-generated)
- `venv/` - Python virtual environment

## Running as a Service

Create a systemd service for continuous monitoring:

```bash
sudo nano /etc/systemd/system/burn-monitor.service
```

```ini
[Unit]
Description=Bittensor Burn Rate Monitor
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/bittensor-burnnoti
ExecStart=/root/bittensor-burnnoti/venv/bin/python /root/bittensor-burnnoti/burn_monitor.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable burn-monitor
sudo systemctl start burn-monitor
```
