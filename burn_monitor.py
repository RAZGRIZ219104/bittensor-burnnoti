#!/usr/bin/env python3
"""
Bittensor Subnet Burn Rate Monitor
Monitors burn rate changes and sends notifications via Telegram.
"""

import json
import os
import time
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

import bittensor as bt
import requests
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv(Path(__file__).parent / ".env")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration
CONFIG_FILE = Path(__file__).parent / "config.json"
DATA_FILE = Path(__file__).parent / "burn_data.json"


def load_config() -> dict:
    """Load configuration from config.json and environment variables"""
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(
            f"Config file not found: {CONFIG_FILE}\n"
            "Please create config.json with your configuration."
        )
    
    with open(CONFIG_FILE, 'r') as f:
        config = json.load(f)
    
    # Load Telegram credentials from environment variables
    config['telegram_bot_token'] = os.getenv('TELEGRAM_BOT_TOKEN')
    config['telegram_chat_id'] = os.getenv('TELEGRAM_CHAT_ID')
    
    if not config['telegram_bot_token']:
        raise ValueError("Missing TELEGRAM_BOT_TOKEN in .env file")
    if not config['telegram_chat_id']:
        raise ValueError("Missing TELEGRAM_CHAT_ID in .env file")
    
    if 'netuids' not in config:
        raise ValueError("Missing required config key: netuids")
    
    return config


def load_burn_data() -> dict:
    """Load previous burn data from local JSON file"""
    if not DATA_FILE.exists():
        return {"subnets": {}}
    
    with open(DATA_FILE, 'r') as f:
        return json.load(f)


def save_burn_data(data: dict) -> None:
    """Save burn data to local JSON file"""
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def rao_to_tao(rao: int) -> float:
    """Convert RAO to TAO (1 TAO = 10^9 RAO)"""
    return rao / 1e9


def send_telegram_message(bot_token: str, chat_id: str, message: str) -> bool:
    """Send a message via Telegram bot"""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        logger.info("Telegram notification sent successfully")
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to send Telegram message: {e}")
        return False


def format_burn_change_message(
    netuid: int,
    old_burn: float,
    new_burn: float,
    emission: float,
    subnet_name: str = None
) -> str:
    """Format the burn rate change notification message"""
    change = new_burn - old_burn
    change_percent = (change / old_burn * 100) if old_burn > 0 else 0
    direction = "🔺" if change > 0 else "🔻"
    
    subnet_label = f"Subnet {netuid}"
    if subnet_name:
        subnet_label = f"{subnet_name} (SN{netuid})"
    
    message = f"""
<b>🔥 Burn Rate Changed - {subnet_label}</b>

{direction} <b>Burn Rate Update</b>
├ Previous: <code>{old_burn:.4f} τ</code>
├ Current: <code>{new_burn:.4f} τ</code>
├ Change: <code>{change:+.4f} τ</code> ({change_percent:+.2f}%)
└ Emission: <code>{emission:.4f} τ/block</code>

⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC
"""
    return message.strip()


def get_all_netuids(subtensor: bt.subtensor) -> list:
    """Get all active subnet netuids from the network"""
    try:
        subnets = subtensor.get_all_subnets_info()
        if subnets:
            return sorted([s.netuid for s in subnets if s is not None])
        return []
    except Exception as e:
        logger.error(f"Error getting all netuids: {e}")
        return []


def get_subnet_burn_info(subtensor: bt.subtensor, netuid: int) -> Optional[dict]:
    """Get burn rate and emission info for a subnet"""
    try:
        subnet_info = subtensor.get_subnet_info(netuid)
        if subnet_info is None:
            logger.warning(f"Subnet {netuid} not found")
            return None
        
        burn_rao = subtensor.burn(netuid=netuid)
        burn_tao = rao_to_tao(burn_rao.rao if hasattr(burn_rao, 'rao') else int(burn_rao))
        
        emission_value = subnet_info.emission_value
        emission_tao = rao_to_tao(emission_value) if emission_value else 0
        
        return {
            "netuid": netuid,
            "burn_tao": burn_tao,
            "emission_tao": emission_tao,
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        logger.error(f"Error getting burn info for subnet {netuid}: {e}")
        return None


def monitor_burn_rates(config: dict, run_once: bool = False) -> None:
    """Main monitoring loop"""
    bot_token = config['telegram_bot_token']
    chat_id = config['telegram_chat_id']
    netuids_config = config['netuids']
    network = config.get('network', 'finney')
    check_interval = config.get('check_interval_seconds', 3600)  # Default 1 hour
    
    logger.info(f"Connecting to Bittensor network: {network}")
    subtensor = bt.subtensor(network=network)
    
    # Get netuids - either from config or all available
    if netuids_config == "all":
        netuids = get_all_netuids(subtensor)
        logger.info(f"Monitoring ALL subnets: {netuids}")
    else:
        netuids = netuids_config
        logger.info(f"Monitoring subnets: {netuids}")
    
    while True:
        burn_data = load_burn_data()
        
        # Refresh netuids list each iteration when monitoring all
        if netuids_config == "all":
            netuids = get_all_netuids(subtensor)
            logger.info(f"Found {len(netuids)} active subnets")
        
        for netuid in netuids:
            netuid_str = str(netuid)
            
            info = get_subnet_burn_info(subtensor, netuid)
            if info is None:
                continue
            
            current_burn = info['burn_tao']
            emission = info['emission_tao']
            
            # Check if burn rate changed
            if netuid_str in burn_data['subnets']:
                previous_burn = burn_data['subnets'][netuid_str].get('burn_tao', 0)
                
                # Only notify if burn rate actually changed
                if abs(current_burn - previous_burn) > 0.0001:  # Small threshold to avoid float noise
                    logger.info(f"Subnet {netuid} burn changed: {previous_burn:.4f} -> {current_burn:.4f}")
                    
                    message = format_burn_change_message(
                        netuid=netuid,
                        old_burn=previous_burn,
                        new_burn=current_burn,
                        emission=emission
                    )
                    send_telegram_message(bot_token, chat_id, message)
            else:
                # First time seeing this subnet, just log it
                logger.info(f"Initial burn rate for subnet {netuid}: {current_burn:.4f} TAO")
            
            # Update stored data
            burn_data['subnets'][netuid_str] = {
                'burn_tao': current_burn,
                'emission_tao': emission,
                'last_updated': info['timestamp']
            }
        
        # Save updated data
        save_burn_data(burn_data)
        
        if run_once:
            break
        
        logger.info(f"Sleeping for {check_interval} seconds...")
        time.sleep(check_interval)


def main():
    """Entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Monitor Bittensor subnet burn rates')
    parser.add_argument('--once', action='store_true', help='Run once and exit')
    parser.add_argument('--test', action='store_true', help='Send a test notification')
    args = parser.parse_args()
    
    config = load_config()
    
    if args.test:
        # Send test notification
        message = "🧪 <b>Test Notification</b>\n\nBurn rate monitor is configured correctly!"
        success = send_telegram_message(
            config['telegram_bot_token'],
            config['telegram_chat_id'],
            message
        )
        if success:
            print("✅ Test notification sent successfully!")
        else:
            print("❌ Failed to send test notification")
        return
    
    try:
        monitor_burn_rates(config, run_once=args.once)
    except KeyboardInterrupt:
        logger.info("Monitoring stopped by user")
    except Exception as e:
        logger.error(f"Error: {e}")
        raise


if __name__ == "__main__":
    main()
