"""Low-cap gem research desk.

Research only. This program never buys, sells, connects a wallet, or moves funds.
Market data comes from DexScreener. Contract and holder checks come from GoPlus.
"""

import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "data" / "gems.json"

DEX = "https://api.dexscreener.com"
GOPLUS = "https://api.gopluslabs.io/api/v1"
CHAINS = {"ethereum": "1", "bsc": "56", "base": "8453", "solana": "solana"}
LISTS = (
    f"{DEX}/token-profiles/latest/v1",
    f"{DEX}/token-boosts/latest/v1",
    f"{DEX}/token-boosts/top/v1",
)

MCAP_MAX = 100_000_000
LIQ_MIN = 50_000
PAIR_MIN_AGE = timedelta(hours=24)
TAX_MAX = 0.05
TOP10_MAX = 0.30
DEV_MAX = 0.05
SNIPER_MAX = 0.20
VOL_LIQ_FLAG = 10
FLOW_CAP_WHEN_FLAGGED = 12
SCORE_MIN = 70
KILL_SKIP = timedelta(days=7)
REVIEW_SKIP = timedelta(hours=24)
MAX_SECURITY = 24
DEX_PAUSE = 0.35
GOPLUS_PAUSE = 1.0

BURN = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
    "0xdead000000000000000000000000000000000000",
    "1nc1nerator11111111111111111111111111111111",
}


def now_utc():
    return datetime.now(timezone.utc)


def stamp(moment=None):
    moment = moment or now_utc()
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_stamp(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def empty_log():
    return {
        "last_scan": None,
        "last_errors": [],
        "morning": None,
        "scorecard": None,
        "gems": [],
        "kills": [],
        "reviewed": [],
    }


def load_log():
    if not LOG_PATH.is_file():
        return empty_log()
    try:
        data = json.loads(LOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_log()
    base = empty_log()
    if isinstance(data, dict):
        for key in base:
            if key in data:
                base[key] = data[key]
    return base


def save_log(log):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LOG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(log, indent=2) + "\n", encoding="utf-8")
    tmp.replace(LOG_PATH)


def get_json(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "gem-desk/1.0", "Accept": "application/json"},
    )
    last = "fetch failed"
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                return json.load(response), None
        except urllib.error.HTTPError as error:
            last = f"HTTP {error.code} {url.split('?')[0][-90:]}"
            if error.code == 429 and attempt == 0:
                time.sleep(3)
                continue
            return None, last
        except Exception as error:
            last = f"{type(error).__name__} {url.split('?')[0][-90:]}"
            if attempt == 0:
                time.sleep(1)
                continue
            return None, last
    return None, last


def num(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def flag_on(value):
    return str(value).strip().lower() in {"1", "true"}


def status_on(value):
    if isinstance(value, dict):
        return flag_on(value.get("status"))
    return flag_on(value)


def norm_addr(chain, address):
    text = str(address or "").strip()
    if chain != "solana":
        return text.lower()
    return text


def is_burn(address):
    text = str(address or "").strip()
    return text.lower() in BURN or text in BURN


def tax_fraction(value):
    parsed = num(value)
    if parsed is None:
        return None
    if parsed > 1:
        return parsed / 100.0
    return parsed


def as_rows(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("pairs", "data", "profiles"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return rows
    return []


def pause(seconds):
    time.sleep(seconds)


def scout_lists(errors):
    found = []
    for url in LISTS:
        payload, error = get_json(url)
        pause(DEX_PAUSE)
        if error:
            errors.append(error)
            continue
        for row in as_rows(payload):
            if not isinstance(row, dict):
                continue
            chain = str(row.get("chainId") or "")
            contract = str(row.get("tokenAddress") or "").strip()
            if chain in CHAINS and contract:
                found.append((chain, contract))
    unique = []
    seen = set()
    for chain, contract in found:
        key = (chain, norm_addr(chain, contract))
        if key in seen:
            continue
        seen.add(key)
        unique.append((chain, contract))
    return unique


def best_pairs(chain, addresses, errors):
    chosen = {}
    confirmed = set()
    for start in range(0, len(addresses), 30):
        batch = addresses[start : start + 30]
        joined = ",".join(urllib.parse.quote(item, safe="") for item in batch)
        payload, error = get_json(f"{DEX}/tokens/v1/{chain}/{joined}")
        pause(DEX_PAUSE)
        if error:
            errors.append(error)
            continue
        for item in batch:
            confirmed.add(norm_addr(chain, item))
        for pair in as_rows(payload):
            if not isinstance(pair, dict):
                continue
            base = pair.get("baseToken") or {}
            contract = str(base.get("address") or "")
            key = norm_addr(chain, contract)
            if not key:
                continue
            liquidity = num((pair.get("liquidity") or {}).get("usd")) or 0
            current = chosen.get(key)
            current_liq = 0 if current is None else (num((current.get("liquidity") or {}).get("usd")) or 0)
            if current is None or liquidity > current_liq:
                chosen[key] = pair
    return chosen, confirmed


def coin_from_pair(chain, contract, pair):
    market_cap = num(pair.get("marketCap"))
    if market_cap is None or market_cap <= 0:
        market_cap = num(pair.get("fdv"))
    txns = ((pair.get("txns") or {}).get("h24") or {})
    base = pair.get("baseToken") or {}
    return {
        "chain": chain,
        "contract": contract,
        "symbol": str(base.get("symbol") or "").strip(),
        "name": str(base.get("name") or "").strip(),
        "pair": str(pair.get("pairAddress") or ""),
        "dex": str(pair.get("dexId") or ""),
        "url": str(pair.get("url") or ""),
        "price_usd": num(pair.get("priceUsd")),
        "market_cap": market_cap,
        "liquidity_usd": num((pair.get("liquidity") or {}).get("usd")),
        "volume_h24": num((pair.get("volume") or {}).get("h24")) or 0,
        "buys": int(num(txns.get("buys")) or 0),
        "sells": int(num(txns.get("sells")) or 0),
        "pair_created_at": num(pair.get("pairCreatedAt")),
    }


def passes_market_filters(coin, moment):
    if not coin["symbol"] or coin["market_cap"] is None or coin["liquidity_usd"] is None:
        return False
    if coin["market_cap"] <= 0 or coin["market_cap"] >= MCAP_MAX:
        return False
    if coin["liquidity_usd"] <= LIQ_MIN:
        return False
    created = coin["pair_created_at"]
    if created is None:
        return False
    age = moment - datetime.fromtimestamp(created / 1000, timezone.utc)
    return age >= PAIR_MIN_AGE


def is_copycat(coin, peers):
    for peer in peers:
        if peer["chain"] == coin["chain"] and norm_addr(coin["chain"], peer["contract"]) == norm_addr(coin["chain"], coin["contract"]):
            continue
        if str(peer.get("symbol") or "").casefold() != coin["symbol"].casefold():
            continue
        older = (
            peer.get("pair_created_at") is not None
            and coin.get("pair_created_at") is not None
            and peer["pair_created_at"] < coin["pair_created_at"]
        )
        bigger = (
            peer.get("market_cap") is not None
            and coin.get("market_cap") is not None
            and peer["market_cap"] > coin["market_cap"]
        )
        if older or bigger:
            return True
    return False


def search_peers(symbol, errors):
    payload, error = get_json(f"{DEX}/latest/dex/search?q={urllib.parse.quote(symbol)}")
    pause(DEX_PAUSE)
    if error:
        errors.append(error)
        return None
    peers = []
    for pair in as_rows(payload):
        if not isinstance(pair, dict):
            continue
        base = pair.get("baseToken") or {}
        chain = str(pair.get("chainId") or "")
        contract = str(base.get("address") or "")
        if chain not in CHAINS or not contract:
            continue
        market_cap = num(pair.get("marketCap"))
        if market_cap is None or market_cap <= 0:
            market_cap = num(pair.get("fdv"))
        peers.append(
            {
                "chain": chain,
                "contract": contract,
                "symbol": str(base.get("symbol") or ""),
                "market_cap": market_cap,
                "pair_created_at": num(pair.get("pairCreatedAt")),
            }
        )
    return peers


def drop_copycats(coins, errors):
    groups = {}
    for coin in coins:
        groups.setdefault(coin["symbol"].casefold(), []).append(coin)
    kept = []
    for symbol, group in groups.items():
        peers = search_peers(group[0]["symbol"], errors)
        if peers is None:
            continue
        peers = list(group) + peers
        for coin in group:
            if not is_copycat(coin, peers):
                kept.append(coin)
    return kept


def scout(errors, moment):
    listed = scout_lists(errors)
    by_chain = {}
    for chain, contract in listed:
        by_chain.setdefault(chain, []).append(contract)
    candidates = []
    for chain, addresses in by_chain.items():
        pairs, _confirmed = best_pairs(chain, addresses, errors)
        for contract in addresses:
            pair = pairs.get(norm_addr(chain, contract))
            if not pair:
                continue
            coin = coin_from_pair(chain, contract, pair)
            if passes_market_filters(coin, moment):
                candidates.append(coin)
    candidates.sort(key=lambda coin: coin["liquidity_usd"] or 0, reverse=True)
    return drop_copycats(candidates, errors)


def fetch_security(coin, errors):
    chain = coin["chain"]
    contract = urllib.parse.quote(coin["contract"], safe="")
    if chain == "solana":
        url = f"{GOPLUS}/solana/token_security?contract_addresses={contract}"
    else:
        url = f"{GOPLUS}/token_security/{CHAINS[chain]}?contract_addresses={contract}"
    payload, error = get_json(url)
    pause(GOPLUS_PAUSE)
    if error:
        errors.append(f"{coin['symbol']} {error}")
        return None
    if not isinstance(payload, dict) or payload.get("code") not in (1, "1"):
        errors.append(f"{coin['symbol']} goplus rejected the request")
        return None
    result = payload.get("result")
    if not isinstance(result, dict) or not result:
        return {}
    wanted = norm_addr(chain, coin["contract"])
    for key, value in result.items():
        if norm_addr(chain, key) == wanted and isinstance(value, dict):
            return value
    value = next(iter(result.values()))
    return value if isinstance(value, dict) else {}


def lp_locked(holder):
    if flag_on(holder.get("is_locked")):
        return True
    nfts = holder.get("NFT_list") or holder.get("nft_list")
    if isinstance(nfts, list) and nfts:
        return True
    tag = str(holder.get("tag") or "").lower()
    if "lock" in tag or "burn" in tag:
        return True
    return is_burn(holder.get("address") or holder.get("account"))


def burned_enough(value):
    parsed = num(value)
    if parsed is None:
        return False
    if parsed > 1:
        return parsed >= 95
    return parsed >= 0.95


def security_reason(chain, payload):
    if not payload:
        return "unverified"
    if chain == "solana":
        return solana_security_reason(payload)
    if not flag_on(payload.get("is_open_source")):
        return "unverified"
    if flag_on(payload.get("is_honeypot")) or flag_on(payload.get("cannot_sell_all")) or flag_on(payload.get("cannot_buy")):
        return "honeypot" if flag_on(payload.get("is_honeypot")) else "cannot sell"
    for label, field in (("buy tax", "buy_tax"), ("sell tax", "sell_tax")):
        tax = tax_fraction(payload.get(field))
        if tax is None:
            return "unverified"
        if tax > TAX_MAX:
            return f"{label} {tax * 100:.1f}%"
    if flag_on(payload.get("is_mintable")):
        return "mintable"
    if flag_on(payload.get("transfer_pausable")):
        return "pausable"
    if flag_on(payload.get("is_blacklisted")):
        return "blacklist"
    if flag_on(payload.get("slippage_modifiable")) or flag_on(payload.get("personal_slippage_modifiable")):
        return "owner-changeable fees"
    return lp_reason(payload)


def solana_security_reason(payload):
    if flag_on(payload.get("non_transferable")) or str(payload.get("default_account_state") or "") == "2":
        return "cannot sell"
    tax = solana_tax(payload.get("transfer_fee"))
    if tax is None:
        return "unverified"
    if tax > TAX_MAX:
        return f"sell tax {tax * 100:.1f}%"
    if status_on(payload.get("mintable")):
        return "mintable"
    if status_on(payload.get("freezable")):
        return "pausable"
    if status_on(payload.get("transfer_fee_upgradable")):
        return "owner-changeable fees"
    return lp_reason(payload)


def solana_tax(fee):
    if not fee:
        return 0.0
    if not isinstance(fee, dict):
        return None
    current = fee.get("current_fee_rate") or fee.get("fee_rate")
    if current is None:
        return 0.0
    rate = num(current.get("fee_rate") if isinstance(current, dict) else current)
    if rate is None:
        return None
    if rate > 1:
        return rate / 10000.0
    return rate


def lp_reason(payload):
    holders = payload.get("lp_holders") if isinstance(payload.get("lp_holders"), list) else []
    pools = payload.get("dex") if isinstance(payload.get("dex"), list) else []
    if any(burned_enough(pool.get("burn_percent")) for pool in pools if isinstance(pool, dict)):
        return None
    if holders:
        locked_share = 0.0
        known = False
        for holder in holders:
            if not isinstance(holder, dict):
                continue
            share = num(holder.get("percent"))
            if share is not None:
                known = True
                if share > 1:
                    share = share / 100.0
            if lp_locked(holder) and (share is None or share >= 0.5 or not known):
                return None
            if lp_locked(holder) and share is not None:
                locked_share += share
        if locked_share >= 0.5:
            return None
        return "lp not locked or burned"
    if pools:
        return "lp not locked or burned"
    return "lp not verified"


def holder_percent(holder, total):
    share = num(holder.get("percent"))
    if share is not None and share > 0:
        return share / 100.0 if share > 1 else share
    balance = num(holder.get("balance"))
    if balance is not None and total:
        return balance / total
    return 0.0


def holder_facts(payload):
    raw = payload.get("holders") if isinstance(payload.get("holders"), list) else None
    if not raw:
        return None
    lp_addrs = set()
    for holder in payload.get("lp_holders") or []:
        if isinstance(holder, dict):
            lp_addrs.add(str(holder.get("address") or holder.get("account") or "").lower())
    total = num(payload.get("total_supply"))
    rows = []
    tagged = False
    sniper_share = 0.0
    for holder in raw:
        if not isinstance(holder, dict):
            continue
        address = str(holder.get("address") or holder.get("account") or "")
        tag = str(holder.get("tag") or "")
        if tag.strip():
            tagged = True
        share = holder_percent(holder, total)
        if is_burn(address) or address.lower() in lp_addrs:
            continue
        if any(word in tag.lower() for word in ("sniper", "fresh", "new wallet")):
            sniper_share += share
        rows.append((address, share, tag))
    rows.sort(key=lambda item: item[1], reverse=True)
    top10 = sum(share for _, share, _ in rows[:10])
    dev = dev_share(payload, rows)
    return {
        "top10": top10,
        "dev": dev,
        "sniper": sniper_share if tagged else None,
        "sniper_unavailable": not tagged,
    }


def dev_share(payload, rows):
    found = []
    for field in ("creator_percent", "owner_percent"):
        if payload.get(field) not in (None, ""):
            parsed = num(payload.get(field))
            if parsed is not None:
                found.append(parsed / 100.0 if parsed > 1 else parsed)
    creators = []
    for field in ("creator_address", "owner_address"):
        if payload.get(field):
            creators.append(str(payload.get(field)))
    for creator in payload.get("creators") or []:
        if isinstance(creator, str):
            creators.append(creator)
        elif isinstance(creator, dict):
            creators.append(str(creator.get("address") or creator.get("account") or ""))
    wanted = {item.lower() for item in creators if item}
    for address, share, _ in rows:
        if address.lower() in wanted:
            found.append(share)
    if not found:
        return None
    return max(found)


def holder_reason(facts):
    if facts is None:
        return "holders not verified"
    if facts["top10"] > TOP10_MAX:
        return f"top 10 hold {facts['top10'] * 100:.0f}%"
    if facts["dev"] is not None and facts["dev"] > DEV_MAX:
        return f"dev wallet holds {facts['dev'] * 100:.0f}%"
    if facts["sniper"] is not None and facts["sniper"] > SNIPER_MAX:
        return f"snipers hold {facts['sniper'] * 100:.0f}%"
    return None


def apply_hype(coin):
    coin["buzz"] = "no-x-source"
    coin["buzz_points"] = 0


def apply_flow(coin):
    liquidity = coin["liquidity_usd"] or 0
    ratio = (coin["volume_h24"] / liquidity) if liquidity else 0
    coin["vol_liq"] = ratio
    coin["suspicious"] = ratio > VOL_LIQ_FLAG
    trades = coin["buys"] + coin["sells"]
    if trades <= 0:
        coin["flow_points"] = 0
        return
    share = coin["buys"] / trades
    points = math.floor(25 * min(share, 0.85) / 0.85)
    if coin["suspicious"]:
        points = min(points, FLOW_CAP_WHEN_FLAGGED)
    coin["flow_points"] = points


def growth_points(current, previous, seen):
    if not seen or previous is None or previous <= 0 or current is None:
        return 0
    change = (current - previous) / previous
    if change >= 0.50:
        return 10
    if change >= 0.20:
        return 7
    if change >= 0:
        return 4
    return 0


def holder_points(facts):
    if facts is None:
        return 0
    room = max(0.0, 1 - (facts["top10"] / TOP10_MAX))
    return math.floor(20 * room)


def strongest_reason(coin):
    parts = coin["parts"]
    options = {
        "holders": parts["holders"],
        "flow": 0 if coin.get("suspicious") else parts["flow"],
        "liquidity_growth": parts["liquidity_growth"],
    }
    best = max(options, key=options.get)
    if options[best] <= 0:
        return "security checks passed"
    if best == "holders":
        return f"top 10 hold {coin['top10'] * 100:.0f}%"
    if best == "flow":
        return "buyers lead sellers"
    change = coin.get("liquidity_change") or 0
    return f"liquidity is up {change * 100:.0f}%"


def red_flag(coin):
    if coin.get("suspicious"):
        return "volume over 10x liquidity"
    if coin.get("top10", 0) >= 0.20:
        return f"top 10 hold {coin['top10'] * 100:.0f}%"
    if coin.get("dev") is not None and coin["dev"] >= 0.03:
        return f"dev wallet holds {coin['dev'] * 100:.0f}%"
    if coin.get("sniper_unavailable"):
        return "sniper data unavailable"
    if not coin.get("seen_before"):
        return "no prior liquidity"
    return "buzz=no-x-source"


def apply_score(coin, seen_liquidity):
    seen = seen_liquidity is not None
    current = coin["liquidity_usd"]
    previous = seen_liquidity
    change = 0 if not seen or not previous else (current - previous) / previous
    coin["seen_before"] = seen
    coin["liquidity_change"] = change
    coin["parts"] = {
        "security": 30,
        "holders": holder_points({"top10": coin["top10"], "dev": coin.get("dev")}),
        "flow": coin["flow_points"],
        "buzz": 0,
        "liquidity_growth": growth_points(current, previous, seen),
    }
    coin["score"] = sum(coin["parts"].values())
    coin["strongest_reason"] = strongest_reason(coin)
    coin["red_flag"] = red_flag(coin)


def killed_recently(log, coin, moment):
    for row in log["kills"]:
        if row.get("chain") != coin["chain"]:
            continue
        if norm_addr(coin["chain"], row.get("contract")) != norm_addr(coin["chain"], coin["contract"]):
            continue
        try:
            if moment - parse_stamp(row["at"]) < KILL_SKIP:
                return True
        except (KeyError, ValueError):
            continue
    return False


def prior_liquidity(log, coin, moment):
    latest = None
    liquidity = None
    for row in log["reviewed"]:
        if row.get("chain") != coin["chain"]:
            continue
        if norm_addr(coin["chain"], row.get("contract")) != norm_addr(coin["chain"], coin["contract"]):
            continue
        try:
            seen = parse_stamp(row["at"])
        except (KeyError, ValueError):
            continue
        if moment - seen < REVIEW_SKIP:
            return "skip", None
        if latest is None or seen > latest:
            latest = seen
            liquidity = num(row.get("liquidity_usd"))
    return "check", liquidity


def remember(log, coin, moment):
    chain = coin["chain"]
    contract = norm_addr(chain, coin["contract"])
    kept = []
    for row in log["reviewed"]:
        if row.get("chain") == chain and norm_addr(chain, row.get("contract")) == contract:
            continue
        try:
            if moment - parse_stamp(row["at"]) < REVIEW_SKIP:
                kept.append(row)
        except (KeyError, ValueError):
            continue
    kept.append(
        {
            "chain": chain,
            "contract": coin["contract"],
            "at": stamp(moment),
            "score": coin.get("score"),
            "liquidity_usd": coin.get("liquidity_usd"),
        }
    )
    log["reviewed"] = kept


def record_kill(log, coin, reason, moment):
    log["kills"].append(
        {
            "at": stamp(moment),
            "chain": coin["chain"],
            "contract": coin["contract"],
            "symbol": coin.get("symbol") or "",
            "reason": reason,
        }
    )


def record_gem(log, coin, moment):
    chain = coin["chain"]
    contract = norm_addr(chain, coin["contract"])
    for gem in log["gems"]:
        if gem["chain"] == chain and norm_addr(chain, gem["contract"]) == contract:
            return
    log["gems"].append(
        {
            "symbol": coin["symbol"],
            "name": coin["name"],
            "chain": chain,
            "contract": coin["contract"],
            "pair": coin["pair"],
            "dex": coin["dex"],
            "url": coin["url"],
            "found_at": stamp(moment),
            "price_usd": coin["price_usd"],
            "market_cap": coin["market_cap"],
            "liquidity_usd": coin["liquidity_usd"],
            "score": coin["score"],
            "parts": coin["parts"],
            "strongest_reason": coin["strongest_reason"],
            "red_flag": coin["red_flag"],
            "buzz": "no-x-source",
            "top10": coin["top10"],
            "dev": coin.get("dev"),
            "sniper_unavailable": bool(coin.get("sniper_unavailable")),
            "checks": {"d1": None, "d7": None, "d30": None},
            "best_multiple": None,
            "worst_multiple": None,
            "rugged": False,
            "rug_reason": "",
            "last_price_usd": coin["price_usd"],
            "last_market_cap": coin["market_cap"],
            "last_liquidity_usd": coin["liquidity_usd"],
            "buys": coin["buys"],
            "sells": coin["sells"],
            "suspicious": coin["suspicious"],
        }
    )


def research(coins, log, errors, moment):
    queued = []
    for coin in coins:
        if any(
            gem["chain"] == coin["chain"] and norm_addr(coin["chain"], gem["contract"]) == norm_addr(coin["chain"], coin["contract"])
            for gem in log["gems"]
        ):
            continue
        if killed_recently(log, coin, moment):
            continue
        state, liquidity = prior_liquidity(log, coin, moment)
        if state == "skip":
            continue
        coin["seen_liquidity"] = liquidity
        queued.append(coin)
    queued = queued[:MAX_SECURITY]

    survivors = []
    for coin in queued:
        payload = fetch_security(coin, errors)
        if payload is None:
            continue
        reason = security_reason(coin["chain"], payload)
        if reason:
            record_kill(log, coin, reason, moment)
            continue
        survivors.append((coin, payload))

    held = []
    for coin, payload in survivors:
        facts = holder_facts(payload)
        reason = holder_reason(facts)
        if reason:
            record_kill(log, coin, reason, moment)
            continue
        coin.update(facts)
        held.append(coin)

    scored = []
    for coin in held:
        apply_hype(coin)
        apply_flow(coin)
        apply_score(coin, coin.get("seen_liquidity"))
        scored.append(coin)

    gems = 0
    for coin in scored:
        if coin["score"] >= SCORE_MIN:
            record_gem(log, coin, moment)
            gems += 1
        else:
            remember(log, coin, moment)
    return gems, len(queued)


def refresh_tracked(log, errors, moment):
    due = [gem for gem in log["gems"] if gem["checks"].get("d30") is None]
    by_chain = {}
    for gem in due:
        by_chain.setdefault(gem["chain"], []).append(gem)
    for chain, gems in by_chain.items():
        addresses = [gem["contract"] for gem in gems]
        pairs, confirmed = best_pairs(chain, addresses, errors)
        for gem in gems:
            key = norm_addr(chain, gem["contract"])
            if key not in confirmed:
                continue
            pair = pairs.get(key)
            if pair is None:
                mark_rug(gem, "pair gone")
                stamp_checks(gem, moment, None, None, None)
                continue
            update_gem(gem, pair, moment)


def mark_rug(gem, reason):
    if gem.get("rugged"):
        return
    gem["rugged"] = True
    gem["rug_reason"] = reason


def update_gem(gem, pair, moment):
    fresh = coin_from_pair(gem["chain"], gem["contract"], pair)
    price = fresh["price_usd"]
    liquidity = fresh["liquidity_usd"]
    entry_liq = num(gem.get("liquidity_usd")) or 0
    previous = num(gem.get("last_liquidity_usd"))
    if liquidity is not None and entry_liq and liquidity < entry_liq * 0.1:
        mark_rug(gem, "liquidity under 10% of entry")
    entry_price = num(gem.get("price_usd"))
    if price and entry_price:
        multiple = price / entry_price
        gem["best_multiple"] = multiple if gem.get("best_multiple") is None else max(gem["best_multiple"], multiple)
        gem["worst_multiple"] = multiple if gem.get("worst_multiple") is None else min(gem["worst_multiple"], multiple)
    snapshot = {
        "liquidity_usd": liquidity or 0,
        "volume_h24": fresh["volume_h24"],
        "buys": fresh["buys"],
        "sells": fresh["sells"],
    }
    apply_flow(snapshot)
    parts = dict(gem.get("parts") or {})
    parts["flow"] = snapshot["flow_points"]
    parts["buzz"] = 0
    parts["liquidity_growth"] = growth_points(liquidity, previous, previous is not None)
    parts.setdefault("security", 30)
    parts.setdefault("holders", 0)
    gem["parts"] = parts
    gem["score"] = sum(parts.values())
    gem["buys"] = fresh["buys"]
    gem["sells"] = fresh["sells"]
    gem["suspicious"] = snapshot["suspicious"]
    gem["flow_points"] = snapshot["flow_points"]
    gem["seen_before"] = previous is not None
    gem["top10"] = num(gem.get("top10")) or 0
    gem["liquidity_change"] = 0 if not previous else ((liquidity or 0) - previous) / previous
    gem["strongest_reason"] = strongest_reason(gem)
    gem["red_flag"] = red_flag(gem)
    gem["last_price_usd"] = price
    gem["last_market_cap"] = fresh["market_cap"]
    gem["last_liquidity_usd"] = liquidity
    stamp_checks(gem, moment, price, fresh["market_cap"], liquidity)


def stamp_checks(gem, moment, price, market_cap, liquidity):
    try:
        found = parse_stamp(gem["found_at"])
    except (KeyError, ValueError):
        return
    entry = num(gem.get("price_usd"))
    multiple = (price / entry) if price and entry else None
    age = moment - found
    for key, days in (("d1", 1), ("d7", 7), ("d30", 30)):
        if gem["checks"].get(key) is None and age >= timedelta(days=days):
            gem["checks"][key] = {
                "at": stamp(moment),
                "price_usd": price,
                "market_cap": market_cap,
                "liquidity_usd": liquidity,
                "multiple": multiple,
                "rugged": bool(gem.get("rugged")),
            }


def morning_view(gem):
    return {
        "symbol": gem.get("symbol"),
        "chain": gem.get("chain"),
        "contract": gem.get("contract"),
        "market_cap": gem.get("last_market_cap") if gem.get("last_market_cap") is not None else gem.get("market_cap"),
        "score": gem.get("score"),
        "strongest_reason": gem.get("strongest_reason"),
        "red_flag": gem.get("red_flag"),
        "buzz": "no-x-source",
        "url": gem.get("url") or "",
    }


def publish_morning(log, moment):
    if moment.hour != 8:
        return
    open_gems = [gem for gem in log["gems"] if gem.get("score", 0) >= SCORE_MIN and not gem.get("rugged")]
    open_gems.sort(key=lambda gem: (-(gem.get("score") or 0), gem.get("found_at") or ""))
    top = [morning_view(gem) for gem in open_gems[:3]]
    log["morning"] = {
        "at": stamp(moment),
        "note": None if top else "nothing today",
        "gems": top,
    }


def week_multiple(gem, field):
    if gem.get(field) is not None:
        return gem[field]
    if field == "worst_multiple" and gem.get("rugged"):
        return 0
    return None


def publish_scorecard(log, moment):
    if moment.weekday() != 6 or moment.hour != 8:
        return
    start = moment - timedelta(days=7)
    week = []
    for gem in log["gems"]:
        try:
            found = parse_stamp(gem["found_at"])
        except (KeyError, ValueError):
            continue
        if found >= start:
            week.append(gem)

    def has_gain_data(gem):
        return gem.get("best_multiple") is not None or gem.get("rugged")

    no_gain = [
        gem
        for gem in week
        if gem.get("best_multiple") is not None and gem["best_multiple"] <= 1 and not gem.get("rugged")
    ]
    losers = [
        gem
        for gem in week
        if gem.get("rugged") or (gem.get("best_multiple") is not None and gem["best_multiple"] <= 1)
    ]
    flags = {}
    for gem in losers:
        flag = gem.get("red_flag") or "unspecified"
        flags[flag] = flags.get(flag, 0) + 1
    ranked_best = [gem for gem in week if week_multiple(gem, "best_multiple") is not None]
    ranked_worst = [gem for gem in week if week_multiple(gem, "worst_multiple") is not None]
    best = max(ranked_best, key=lambda gem: week_multiple(gem, "best_multiple"), default=None)
    worst = min(ranked_worst, key=lambda gem: week_multiple(gem, "worst_multiple"), default=None)

    def brief(gem, field):
        if gem is None:
            return None
        return {
            "symbol": gem.get("symbol"),
            "chain": gem.get("chain"),
            "multiple": week_multiple(gem, field),
            "rugged": bool(gem.get("rugged")),
        }

    log["scorecard"] = {
        "at": stamp(moment),
        "found": len(week),
        "doubled": sum(1 for gem in week if (gem.get("best_multiple") or 0) >= 2),
        "x5": sum(1 for gem in week if (gem.get("best_multiple") or 0) >= 5),
        "x10": sum(1 for gem in week if (gem.get("best_multiple") or 0) >= 10),
        "no_gain": len(no_gain),
        "rugged": sum(1 for gem in week if gem.get("rugged")),
        "best": brief(best, "best_multiple"),
        "worst": brief(worst, "worst_multiple"),
        "loser_flags": [{"flag": flag, "count": count} for flag, count in sorted(flags.items(), key=lambda item: (-item[1], item[0]))],
        "tracked": len([gem for gem in week if has_gain_data(gem)]),
    }


def self_check():
    clean = {
        "is_open_source": "1",
        "is_honeypot": "0",
        "cannot_sell_all": "0",
        "cannot_buy": "0",
        "buy_tax": "0",
        "sell_tax": "0.05",
        "is_mintable": "0",
        "transfer_pausable": "0",
        "is_blacklisted": "0",
        "slippage_modifiable": "0",
        "personal_slippage_modifiable": "0",
        "lp_holders": [{"address": "0x000000000000000000000000000000000000dead", "percent": "1", "is_locked": 0}],
    }
    assert security_reason("ethereum", clean) is None
    assert security_reason("ethereum", {**clean, "buy_tax": "0.051"}) == "buy tax 5.1%"
    assert security_reason("ethereum", {**clean, "is_honeypot": "1"}) == "honeypot"
    assert security_reason("ethereum", {**clean, "is_open_source": "0"}) == "unverified"
    assert security_reason("ethereum", {}) == "unverified"
    assert math.floor(20 * (1 / 3)) == 6
    coin = {
        "liquidity_usd": 60_000,
        "volume_h24": 60_000,
        "buys": 90,
        "sells": 10,
        "top10": 0.0,
        "dev": 0.0,
        "sniper_unavailable": True,
    }
    apply_hype(coin)
    apply_flow(coin)
    apply_score(coin, None)
    assert coin["buzz"] == "no-x-source"
    assert coin["parts"]["buzz"] == 0
    assert coin["score"] == 75
    assert coin["score"] >= SCORE_MIN
    coin["volume_h24"] = 700_000
    apply_flow(coin)
    apply_score(coin, None)
    assert coin["suspicious"] is True
    assert coin["flow_points"] <= FLOW_CAP_WHEN_FLAGGED
    assert coin["score"] < SCORE_MIN
    older = {"chain": "ethereum", "contract": "0x1", "symbol": "AAA", "market_cap": 2_000_000, "pair_created_at": 1}
    newer = {"chain": "base", "contract": "0x2", "symbol": "AAA", "market_cap": 80_000, "pair_created_at": 2}
    assert is_copycat(newer, [older, newer]) is True
    assert is_copycat(older, [older, newer]) is False
    facts = {"top10": 0.31, "dev": 0.0, "sniper": None, "sniper_unavailable": True}
    assert holder_reason(facts) == "top 10 hold 31%"
    facts["top10"] = 0.30
    assert holder_reason(facts) is None


def run_once(moment=None):
    self_check()
    moment = moment or now_utc()
    log = load_log()
    errors = []
    try:
        refresh_tracked(log, errors, moment)
        found = scout(errors, moment)
        gems, checked = research(found, log, errors, moment)
    except Exception as error:
        gems, checked, found = 0, 0, []
        errors.append(f"scan failed: {type(error).__name__}: {error}")
    log["last_scan"] = stamp(moment)
    log["last_errors"] = errors[-12:]
    publish_morning(log, moment)
    publish_scorecard(log, moment)
    save_log(log)
    print(f"Scan {log['last_scan']} | candidates after filters will vary | security checks {checked} | new gems {gems}")
    print(f"Log: {len(log['gems'])} gems, {len(log['kills'])} kills")
    if log["morning"] and log["morning"].get("at", "").startswith(stamp(moment)[:13]):
        if log["morning"].get("note"):
            print(f"gems 08:00Z | {log['morning']['note']}")
        else:
            print("gems 08:00Z | " + " | ".join(
                f"{row['symbol']} {row['chain']} {row['contract']} mcap={row['market_cap']} score={row['score']} {row['strongest_reason']} flag={row['red_flag']}"
                for row in log["morning"]["gems"]
            ))
    if errors:
        print("Source errors:")
        for item in log["last_errors"]:
            print(f"  {item}")
    return 0


def main():
    if "--once" in sys.argv:
        return run_once()
    print("usage: python gems.py --once")
    return 2


if __name__ == "__main__":
    sys.exit(main())
