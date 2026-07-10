"""Telegram long-polling bot: answer subnet queries on demand.

Runs alongside the hourly burn report. Users send a subnet number ("64", "sn64")
or a subnet name ("chutes"), and get back that subnet's details.
"""

import html
import json
import logging
import os
import time
from pathlib import Path

import requests

import subnet_info

log = logging.getLogger("telegram_bot")

API_BASE = "https://api.telegram.org/bot{token}/{method}"
OFFSET_FILE = Path(__file__).parent / "bot_offset.json"

POLL_TIMEOUT = 30   # long-poll seconds held open by Telegram
HTTP_TIMEOUT = POLL_TIMEOUT + 15
ERROR_BACKOFF = 5

HELP_TEXT = (
    "🤖 <b>Subnet Info Bot</b>\n\n"
    "Send me a subnet number or name and I'll show its details:\n\n"
    "  <code>64</code>\n"
    "  <code>sn64</code>\n"
    "  <code>chutes</code>\n"
    "  <code>/subnet 64</code>\n\n"
    "You'll get emission, incentive burn, miners earning incentive, "
    "registration cost, owner address and links."
)


class SubnetBot:
    """Polls Telegram for messages and replies with subnet details."""

    def __init__(self, token: str, cache: subnet_info.SubnetCache):
        self.token = token
        self.cache = cache
        self.session = requests.Session()
        self._offset = self._load_offset()

    # -- offset persistence -------------------------------------------------
    def _load_offset(self) -> int | None:
        try:
            if OFFSET_FILE.exists():
                return int(json.loads(OFFSET_FILE.read_text())["offset"])
        except Exception as e:
            log.warning("Could not read offset file: %s", e)
        return None

    def _save_offset(self, offset: int):
        try:
            OFFSET_FILE.write_text(json.dumps({"offset": offset}))
        except Exception as e:
            log.warning("Could not save offset file: %s", e)

    # -- telegram api -------------------------------------------------------
    def _call(self, method: str, http_timeout: int = 30, **params):
        """Call a Bot API method. `params` go in the JSON body verbatim."""
        url = API_BASE.format(token=self.token, method=method)
        resp = self.session.post(url, json=params, timeout=http_timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"Telegram {method} failed ({resp.status_code}): {resp.text[:200]}")
        return resp.json().get("result")

    def send(self, chat_id, text: str, reply_to: int | None = None):
        params = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_to is not None:
            params["reply_to_message_id"] = reply_to
        try:
            self._call("sendMessage", **params)
        except Exception as e:
            log.error("Failed to reply to chat %s: %s", chat_id, e)

    def _drop_backlog(self):
        """Skip messages that queued up while the bot was down."""
        if self._offset is not None:
            return
        try:
            updates = self._call("getUpdates", offset=-1) or []
            if updates:
                self._offset = updates[-1]["update_id"] + 1
                self._save_offset(self._offset)
                log.info("Skipping backlog, starting at offset %d", self._offset)
        except Exception as e:
            log.warning("Could not determine starting offset: %s", e)

    # -- query handling -----------------------------------------------------
    def _extract_query(self, text: str, is_private: bool) -> str | None:
        """Turn a raw message into a subnet query, or None if it isn't one."""
        text = text.strip()
        if not text:
            return None

        if text.startswith("/"):
            command, _, rest = text.partition(" ")
            # Group messages address the bot as /subnet@MyBot
            command = command.split("@", 1)[0].lower()

            if command in ("/start", "/help"):
                return "__help__"
            if command in ("/subnet", "/sn", "/info"):
                return rest.strip() or "__help__"
            return None

        # Outside a private chat, bare text is ordinary conversation, not a query.
        # In a channel it would also match our own hourly burn report. Commands only.
        return text if is_private else None

    def handle_message(self, message: dict):
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        text = message.get("text")
        if not chat_id or not text:
            return

        # Never react to bots. Channel posts carry no `from`, so the command-only rule
        # above is what actually stops us replying to our own burn report.
        if message.get("from", {}).get("is_bot"):
            return

        query = self._extract_query(text, is_private=chat.get("type") == "private")
        if query is None:
            return

        message_id = message.get("message_id")

        if query == "__help__":
            self.send(chat_id, HELP_TEXT, reply_to=message_id)
            return

        try:
            subnets = self.cache.get_all()
        except Exception as e:
            log.error("Subnet cache unavailable: %s", e)
            self.send(chat_id, "⚠️ Subnet data is unavailable right now. Try again shortly.",
                      reply_to=message_id)
            return

        matches, kind = subnet_info.lookup(subnets, query)

        if kind == "exact":
            info = matches[0]
            # Best-effort: the card still renders if taostats rate-limits the metagraph.
            mg = self.cache.get_metagraph(info["netuid"])
            self.send(chat_id, subnet_info.format_subnet(info, mg), reply_to=message_id)
        elif kind == "partial":
            self.send(chat_id, subnet_info.format_candidates(matches, query), reply_to=message_id)
        else:
            self.send(
                chat_id,
                f'❓ No subnet matches "<b>{html.escape(query)}</b>".\n'
                f"Send a subnet number (e.g. <code>64</code>) or name, or /help.",
                reply_to=message_id,
            )

    # -- main loop ----------------------------------------------------------
    def run(self):
        log.info("Subnet info bot started (long-polling)")
        self._drop_backlog()

        while True:
            try:
                # `timeout` here is Telegram's long-poll hold; the socket waits longer.
                updates = self._call(
                    "getUpdates",
                    http_timeout=HTTP_TIMEOUT,
                    offset=self._offset,
                    timeout=POLL_TIMEOUT,
                    allowed_updates=["message", "channel_post"],
                ) or []
            except Exception as e:
                log.error("getUpdates failed: %s", e)
                time.sleep(ERROR_BACKOFF)
                continue

            for update in updates:
                self._offset = update["update_id"] + 1
                try:
                    # A bot that is a channel admin receives the channel's posts too.
                    message = update.get("message") or update.get("channel_post")
                    if message:
                        self.handle_message(message)
                except Exception as e:
                    log.error("Error handling update %s: %s", update.get("update_id"), e)

            if updates:
                self._save_offset(self._offset)


def start_in_background(cache: subnet_info.SubnetCache):
    """Start the bot on a daemon thread. Returns the thread, or None if disabled."""
    import threading

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN not set — subnet info bot disabled")
        return None

    bot = SubnetBot(token, cache)
    thread = threading.Thread(target=bot.run, name="subnet-bot", daemon=True)
    thread.start()
    return thread
