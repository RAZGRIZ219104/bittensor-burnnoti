"""Subnet detail lookup backed by a shared, TTL-cached taostats snapshot.

The taostats API rate-limits aggressively, so every lookup is served from a cache
refreshed at most once per CACHE_TTL. A refresh costs two requests regardless of how
many subnets or users are asking.
"""

import html
import logging
import os
import re
import threading
import time

import requests

log = logging.getLogger("subnet_info")

TAOSTATS_API = "https://api.taostats.io/api/subnet/latest/v1"
TAOSTATS_IDENTITY_API = "https://api.taostats.io/api/subnet/identity/v1"
TAOSTATS_POOL_API = "https://api.taostats.io/api/dtao/pool/latest/v1"
TAOSTATS_METAGRAPH_API = "https://api.taostats.io/api/metagraph/latest/v1"
SUBNET_URL = "https://taostats.io/subnets/{netuid}"
METAGRAPH_URL = "https://taostats.io/subnets/{netuid}/metagraph"
ACCOUNT_URL = "https://taostats.io/account/{ss58}"

CACHE_TTL = 300  # seconds
RAO_PER_TAO = 1_000_000_000
# Bittensor targets a 12s block.
BLOCKS_PER_DAY = 7200
# On-chain SubnetAlphaOutEmission is 1 alpha/block for every subnet, so each subnet
# emits the same alpha per day regardless of whether it receives any TAO. This is what
# miners are actually paid in. (Verified against chain storage on 2026-07-10.)
ALPHA_PER_DAY = 7200
METAGRAPH_PAGE_SIZE = 256
METAGRAPH_MAX_PAGES = 8


def _headers() -> dict:
    key = os.getenv("TAOSTATS_API_KEY", "")
    return {"Authorization": key} if key else {}


def _f(value, default: float = 0.0) -> float:
    """Parse taostats' stringified numbers without blowing up on None."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ss58(value) -> str:
    """Owner fields arrive as {"ss58": ..., "hex": ...}."""
    if isinstance(value, dict):
        return value.get("ss58", "") or ""
    return value or ""


def abbrev(addr: str, keep: int = 6) -> str:
    if len(addr) > keep * 2 + 2:
        return f"{addr[:keep]}…{addr[-keep:]}"
    return addr


def format_pct(fraction: float) -> str:
    pct = fraction * 100
    if pct == 0:
        return "0%"
    if pct >= 99.995:
        return "100%"
    if pct >= 10:
        return f"{pct:.1f}%"
    return f"{pct:.2f}%"


def _format_amount(rao: float, symbol: str) -> str:
    amount = rao / RAO_PER_TAO
    if amount == 0:
        return f"0 {symbol}"
    if amount >= 1000:
        return f"{amount:,.0f} {symbol}"
    if amount >= 1:
        return f"{amount:,.2f} {symbol}"
    return f"{amount:.6f}".rstrip("0").rstrip(".") + f" {symbol}"


def format_tao(rao: float) -> str:
    return _format_amount(rao, "τ")


class SubnetCache:
    """Thread-safe TTL cache of merged subnet stats + identity."""

    def __init__(self, ttl: int = CACHE_TTL):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._subnets: dict[int, dict] = {}
        self._fetched_at: float = 0.0
        # netuid -> (fetched_at, payout summary)
        self._mg_lock = threading.Lock()
        self._metagraphs: dict[int, tuple[float, dict]] = {}

    def _fetch_metagraph(self, netuid: int) -> dict:
        """Per-day payout split, as taostats shows it on the subnet page.

        `daily_mining_alpha_as_tao` is the miners' alpha slice valued in TAO -- this is
        the "miners" TAO/day figure. Do NOT use `daily_total_rewards_as_tao`: it also
        includes root TAO dividends, so it exceeds the subnet's real payout.
        """
        headers = _headers()
        rows: list[dict] = []
        page = 1
        while page <= METAGRAPH_MAX_PAGES:
            resp = requests.get(
                TAOSTATS_METAGRAPH_API,
                headers=headers,
                params={"netuid": netuid, "per_page": METAGRAPH_PAGE_SIZE, "page": page},
                timeout=90,
            )
            resp.raise_for_status()
            payload = resp.json()
            rows.extend(payload.get("data", []))
            total_pages = (payload.get("pagination") or {}).get("total_pages", 1) or 1
            if page >= total_pages:
                break
            page += 1

        def total(key: str) -> float:
            return sum(_f(r.get(key)) for r in rows)

        # The burn comes entirely out of the miner slice, and `daily_mining_alpha*` is
        # reported PRE-burn: a 100%-burn subnet still shows a full miner allocation
        # alongside an equal burn. Miners actually receive gross - burned.
        miners_gross = total("daily_mining_alpha_as_tao")
        burned_tao = total("daily_burned_alpha_as_tao")

        return {
            "miners_gross_tao_rao": miners_gross,
            "burned_tao_rao": burned_tao,
            "miners_net_tao_rao": max(0.0, miners_gross - burned_tao),
            "validators_tao_rao": total("daily_validating_alpha_as_tao"),
            "owner_tao_rao": total("daily_owner_alpha_as_tao"),
            "burned_alpha_rao": total("daily_burned_alpha"),
            "miners_incentivized": sum(1 for r in rows if _f(r.get("incentive")) > 0),
            "neurons": len(rows),
        }

    def get_metagraph(self, netuid: int) -> dict | None:
        """Cached per-subnet payout summary, or None if taostats won't serve it."""
        with self._mg_lock:
            entry = self._metagraphs.get(netuid)
            if entry and time.time() - entry[0] < self.ttl:
                return entry[1]
            try:
                summary = self._fetch_metagraph(netuid)
            except Exception as e:
                log.warning("Metagraph fetch failed for netuid %s: %s", netuid, e)
                return entry[1] if entry else None
            self._metagraphs[netuid] = (time.time(), summary)
            return summary

    def _fetch_pools(self) -> dict[int, dict]:
        """Pool data carries `subnet_emission_enabled`, which explains a zero TAO emission."""
        try:
            resp = requests.get(
                TAOSTATS_POOL_API, headers=_headers(), params={"per_page": 1024}, timeout=60
            )
            resp.raise_for_status()
            return {int(p["netuid"]): p for p in resp.json().get("data", [])}
        except Exception as e:
            log.warning("Failed to fetch dtao pools: %s", e)
            return {}

    def _fetch(self) -> dict[int, dict]:
        headers = _headers()

        resp = requests.get(TAOSTATS_API, headers=headers, params={"per_page": 1024}, timeout=60)
        resp.raise_for_status()
        stats = resp.json().get("data", [])

        identities: dict[int, dict] = {}
        try:
            resp = requests.get(
                TAOSTATS_IDENTITY_API, headers=headers, params={"per_page": 1024}, timeout=60
            )
            resp.raise_for_status()
            for entry in resp.json().get("data", []):
                identities[int(entry["netuid"])] = entry
        except Exception as e:
            # Identity is decoration; stats alone still answer most of the question.
            log.warning("Failed to fetch subnet identities: %s", e)

        merged: dict[int, dict] = {}
        for s in stats:
            netuid = int(s["netuid"])
            ident = identities.get(netuid, {})
            merged[netuid] = {
                "netuid": netuid,
                "name": ident.get("subnet_name") or f"SN{netuid}",
                "description": ident.get("description") or "",
                "website": ident.get("subnet_url") or "",
                "github": ident.get("github_repo") or "",
                "discord": ident.get("discord") or "",
                "twitter": ident.get("twitter") or "",
                "owner_coldkey": _ss58(s.get("owner")),
                "owner_hotkey": _ss58(s.get("owner_hotkey")),
                "incentive_burn": _f(s.get("incentive_burn")),
                # Equals the chain's SubnetTaoInEmission: TAO the chain emits into this
                # subnet, in rao per block. Zero for subnets with emission disabled.
                "tao_in_rao": _f(s.get("emission")),
                # A forward-looking share (== ema_tao_flow / sum(ema_tao_flow)); it is 0
                # for most subnets, so it is NOT the current emission. Not rendered.
                "projected_share": _f(s.get("projected_emission")),
                # taostats counts a miner as "active" exactly when its incentive > 0
                "active_miners": int(s.get("active_miners") or 0),
                "active_validators": int(s.get("active_validators") or 0),
                "validators": int(s.get("validators") or 0),
                "active_keys": int(s.get("active_keys") or 0),
                "max_neurons": int(s.get("max_neurons") or 0),
                "registration_allowed": bool(s.get("registration_allowed")),
                "registration_cost_rao": _f(s.get("neuron_registration_cost")),
                "recycled_24h_rao": _f(s.get("recycled_24_hours")),
                "block_number": s.get("block_number"),
            }

        pools = self._fetch_pools()
        total_tao_in = sum(i["tao_in_rao"] for i in merged.values())
        for netuid, info in merged.items():
            pool = pools.get(netuid, {})
            enabled = pool.get("subnet_emission_enabled")
            info["emission_enabled"] = True if enabled is None else bool(enabled)
            info["daily_tao_rao"] = info["tao_in_rao"] * BLOCKS_PER_DAY
            info["tao_share"] = info["tao_in_rao"] / total_tao_in if total_tao_in else 0.0
        return merged

    def get_all(self, force: bool = False) -> dict[int, dict]:
        with self._lock:
            fresh = time.time() - self._fetched_at < self.ttl
            if self._subnets and fresh and not force:
                return self._subnets
            try:
                self._subnets = self._fetch()
                self._fetched_at = time.time()
                log.info("Subnet cache refreshed (%d subnets)", len(self._subnets))
            except Exception as e:
                log.error("Failed to refresh subnet cache: %s", e)
                if not self._subnets:
                    raise
                # Serve stale data rather than failing the user's query.
                log.warning("Serving stale subnet cache")
            return self._subnets


def lookup(subnets: dict[int, dict], query: str) -> tuple[list[dict], str]:
    """Resolve a query (netuid or name) to matching subnets.

    Returns (matches, kind) where kind is "exact", "partial" or "none".
    """
    query = query.strip()
    if not query:
        return [], "none"

    # Numeric, optionally written as "sn64" or "SN 64"
    numeric = query.lower().removeprefix("sn").strip()
    if numeric.isdigit():
        info = subnets.get(int(numeric))
        return ([info], "exact") if info else ([], "none")

    lowered = query.lower()

    exact = [s for s in subnets.values() if s["name"].lower() == lowered]
    if exact:
        return exact, "exact"

    prefix = [s for s in subnets.values() if s["name"].lower().startswith(lowered)]
    if len(prefix) == 1:
        return prefix, "exact"
    if prefix:
        return sorted(prefix, key=lambda s: s["netuid"]), "partial"

    substr = [s for s in subnets.values() if lowered in s["name"].lower()]
    if len(substr) == 1:
        return substr, "exact"
    if substr:
        return sorted(substr, key=lambda s: s["netuid"]), "partial"

    return [], "none"


def _link(url: str, label: str) -> str:
    """Build an anchor. Both parts come from the API, so both get escaped."""
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'


_DOMAIN_RE = re.compile(r"^[\w.-]+\.[a-z]{2,}(/.*)?$", re.IGNORECASE)


def _maybe_link(value: str, label: str) -> str | None:
    """Link real URLs and bare domains. Some identity fields hold a handle or invite code."""
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return _link(value, label)
    if _DOMAIN_RE.match(value):
        return _link(f"https://{value}", label)
    return f"{label}: {html.escape(value)}"


def format_subnet(info: dict, mg: dict | None = None) -> str:
    """Render a subnet's details as a Telegram HTML message.

    `mg` carries the daily payout split; without it those lines are omitted rather
    than substituted with a different quantity.
    """
    netuid = info["netuid"]
    lines: list[str] = []

    lines.append(f'🌐 <b>SN{netuid} · {html.escape(info["name"])}</b>')
    if info["description"]:
        lines.append(f'<i>{html.escape(info["description"][:200])}</i>')
    lines.append("")

    lines.append(f'🔥 Incentive burn: <b>{format_pct(info["incentive_burn"])}</b>')

    if mg:
        payout = mg["miners_net_tao_rao"] + mg["validators_tao_rao"] + mg["owner_tao_rao"]
        lines.append(f'📈 Emission: <b>{format_tao(payout)}/day</b> <i>after burn</i>')

        miner_line = f'   ⛏️ Miners: <b>{format_tao(mg["miners_net_tao_rao"])}/day</b>'
        if mg["burned_tao_rao"] > 0:
            miner_line += (
                f' <i>({format_tao(mg["miners_gross_tao_rao"])} gross, '
                f'{format_pct(info["incentive_burn"])} burned)</i>'
            )
        lines.append(miner_line)
        lines.append(f'   🛡️ Validators: {format_tao(mg["validators_tao_rao"])}/day')
        lines.append(f'   👑 Owner: {format_tao(mg["owner_tao_rao"])}/day')

        alpha = f"{ALPHA_PER_DAY:,} α/day"
        burned = mg["burned_alpha_rao"] / RAO_PER_TAO
        if burned > 0:
            alpha += f" · {burned:,.0f} α burned"
        lines.append(f"🪙 Alpha: {alpha}")

    # TAO the chain injects into the pool. Separate from the payout above, and zero for
    # subnets with emission disabled -- whose miners still earn alpha.
    if info["daily_tao_rao"] > 0:
        lines.append(
            f'💧 TAO into pool: {format_tao(info["daily_tao_rao"])}/day '
            f'({format_pct(info["tao_share"])} of all subnets)'
        )
    elif not info["emission_enabled"]:
        lines.append("💧 TAO into pool: 0 τ/day <i>(emission disabled)</i>")
    else:
        lines.append("💧 TAO into pool: 0 τ/day")

    miners = mg["miners_incentivized"] if mg else info["active_miners"]
    max_neurons = (mg["neurons"] if mg else 0) or info["max_neurons"]
    denom = f" / {max_neurons} neurons" if max_neurons else ""
    lines.append(f'⛏️ Miners earning incentive: <b>{miners}</b>{denom}')
    lines.append(
        f'🛡️ Validators: <b>{info["active_validators"]}</b> active of {info["validators"]}'
    )
    lines.append(f'🔑 Registered keys: {info["active_keys"]}')

    reg = "open" if info["registration_allowed"] else "closed"
    lines.append(
        f'💰 Registration: {reg} · cost {format_tao(info["registration_cost_rao"])}'
    )
    lines.append(f'♻️ Recycled (24h): {format_tao(info["recycled_24h_rao"])}')
    lines.append("")

    coldkey = info["owner_coldkey"]
    if coldkey:
        lines.append(f'👤 Owner: {_link(ACCOUNT_URL.format(ss58=coldkey), abbrev(coldkey))}')
    hotkey = info["owner_hotkey"]
    if hotkey:
        lines.append(f'🔐 Owner hotkey: {_link(ACCOUNT_URL.format(ss58=hotkey), abbrev(hotkey))}')

    twitter = info["twitter"]
    if twitter and not twitter.startswith("http"):
        twitter = f"https://x.com/{twitter.lstrip('@')}"

    externals = [
        _maybe_link(info["website"], "Website"),
        _maybe_link(info["github"], "GitHub"),
        _maybe_link(info["discord"], "Discord"),
        _maybe_link(twitter, "X"),
    ]
    externals = [e for e in externals if e]
    if externals:
        lines.append("🔗 " + " · ".join(externals))

    lines.append(
        "📊 "
        + _link(SUBNET_URL.format(netuid=netuid), "Taostats")
        + " · "
        + _link(METAGRAPH_URL.format(netuid=netuid), "Leaderboard")
    )

    if info.get("block_number"):
        lines.append(f'\n<i>as of block {info["block_number"]}</i>')

    return "\n".join(lines)


def format_candidates(matches: list[dict], query: str) -> str:
    lines = [f'🔎 Multiple subnets match "<b>{html.escape(query)}</b>":', ""]
    for s in matches[:15]:
        lines.append(f'  SN{s["netuid"]} · {html.escape(s["name"])}')
    if len(matches) > 15:
        lines.append(f"  … and {len(matches) - 15} more")
    lines.append("\nReply with the subnet number for details.")
    return "\n".join(lines)
