// Rates coins a visitor types in. Research only: no wallet, no trade.
// Same gates as gems.py. A first look has no saved liquidity, so growth scores 0.

const DEX = "https://api.dexscreener.com";
const GOPLUS = "https://api.gopluslabs.io/api/v1";
const GOPLUS_CHAIN = {
  ethereum: "1",
  bsc: "56",
  polygon: "137",
  arbitrum: "42161",
  optimism: "10",
  base: "8453",
  avalanche: "43114",
  fantom: "250",
  cronos: "25",
  gnosis: "100",
  opbnb: "204",
  zksync: "324",
  manta: "169",
  metis: "1088",
  polygonzkevm: "1101",
  xlayer: "196",
  linea: "59144",
  scroll: "534352",
  mantle: "5000",
  blast: "81457",
  solana: "solana",
};
const BURN = new Set([
  "0x0000000000000000000000000000000000000000",
  "0x000000000000000000000000000000000000dead",
  "0xdead000000000000000000000000000000000000",
  "1nc1nerator11111111111111111111111111111111",
]);
const MAX_QUERIES = 12;

function num(value) {
  if (value == null || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function flagOn(value) {
  return ["1", "true"].includes(String(value).trim().toLowerCase());
}

function statusOn(value) {
  if (value && typeof value === "object") return flagOn(value.status);
  return flagOn(value);
}

function normAddr(chain, address) {
  const text = String(address || "").trim();
  return chain === "solana" ? text : text.toLowerCase();
}

function isBurn(address) {
  const text = String(address || "").trim();
  return BURN.has(text.toLowerCase()) || BURN.has(text);
}

function taxFraction(value) {
  const parsed = num(value);
  if (parsed == null) return null;
  return parsed > 1 ? parsed / 100 : parsed;
}

function isAddress(query) {
  return /^0x[a-fA-F0-9]{40}$/.test(query) || /^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(query);
}

function money(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  if (n >= 1e9) return "$" + (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return "$" + (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return "$" + (n / 1e3).toFixed(1) + "k";
  return "$" + n.toFixed(0);
}

function el(tag, value, className) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value != null) node.textContent = String(value);
  return node;
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function getJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error("HTTP " + response.status);
  return response.json();
}

function coinFromPair(pair) {
  const base = pair.baseToken || {};
  let marketCap = num(pair.marketCap);
  if (marketCap == null || marketCap <= 0) marketCap = num(pair.fdv);
  const txns = (pair.txns || {}).h24 || {};
  return {
    chain: String(pair.chainId || ""),
    contract: String(base.address || ""),
    symbol: String(base.symbol || "").trim(),
    name: String(base.name || "").trim(),
    pair: String(pair.pairAddress || ""),
    url: String(pair.url || ""),
    price_usd: num(pair.priceUsd),
    market_cap: marketCap,
    liquidity_usd: num((pair.liquidity || {}).usd),
    volume_h24: num((pair.volume || {}).h24) || 0,
    buys: Math.round(num(txns.buys) || 0),
    sells: Math.round(num(txns.sells) || 0),
    pair_created_at: num(pair.pairCreatedAt),
  };
}

function pairLiquidity(pair) {
  return num((pair.liquidity || {}).usd) || 0;
}

function marketCapOf(pair) {
  let marketCap = num(pair.marketCap);
  if (marketCap == null || marketCap <= 0) marketCap = num(pair.fdv);
  return marketCap || 0;
}

function choosePair(pairs, query) {
  const exact = pairs.filter((pair) => {
    const base = pair.baseToken || {};
    if (isAddress(query)) return normAddr(pair.chainId, base.address) === normAddr(pair.chainId, query);
    return String(base.symbol || "").toLowerCase() === query.toLowerCase();
  });
  const pool = exact.length ? exact : pairs;
  const bestByToken = new Map();
  for (const pair of pool) {
    const base = pair.baseToken || {};
    const chain = String(pair.chainId || "");
    const key = chain + ":" + normAddr(chain, base.address);
    const prev = bestByToken.get(key);
    if (!prev || marketCapOf(pair) > marketCapOf(prev)) bestByToken.set(key, pair);
  }
  return [...bestByToken.values()].sort((a, b) => marketCapOf(b) - marketCapOf(a))[0] || null;
}

function peerFromPair(pair) {
  const base = pair.baseToken || {};
  let marketCap = num(pair.marketCap);
  if (marketCap == null || marketCap <= 0) marketCap = num(pair.fdv);
  return {
    chain: String(pair.chainId || ""),
    contract: String(base.address || ""),
    symbol: String(base.symbol || ""),
    market_cap: marketCap,
    pair_created_at: num(pair.pairCreatedAt),
  };
}

function isCopycat(coin, peers) {
  for (const peer of peers) {
    if (peer.chain === coin.chain && normAddr(coin.chain, peer.contract) === normAddr(coin.chain, coin.contract)) continue;
    if (String(peer.symbol || "").toLowerCase() !== coin.symbol.toLowerCase()) continue;
    const bigger = peer.market_cap != null && coin.market_cap != null && peer.market_cap > coin.market_cap;
    if (bigger) return true;
  }
  return false;
}

function marketReasons(coin) {
  const reasons = [];
  if (!coin.symbol || coin.market_cap == null || coin.liquidity_usd == null) reasons.push("DexScreener has no market cap or liquidity");
  if (coin.market_cap != null && (coin.market_cap <= 0 || coin.market_cap >= 100000000)) reasons.push("market cap is not under $100M");
  if (coin.liquidity_usd != null && coin.liquidity_usd <= 50000) reasons.push("liquidity is not over $50k");
  if (coin.pair_created_at == null) reasons.push("pair age is missing");
  else if (Date.now() - coin.pair_created_at < 24 * 60 * 60 * 1000) reasons.push("pair is under 24 hours old");
  return reasons;
}

function lpLocked(holder) {
  if (flagOn(holder.is_locked)) return true;
  const nfts = holder.NFT_list || holder.nft_list;
  if (Array.isArray(nfts) && nfts.length) return true;
  const tag = String(holder.tag || "").toLowerCase();
  if (tag.includes("lock") || tag.includes("burn")) return true;
  return isBurn(holder.address || holder.account);
}

function burnedEnough(value) {
  const parsed = num(value);
  if (parsed == null) return false;
  return parsed > 1 ? parsed >= 95 : parsed >= 0.95;
}

function solanaTax(fee) {
  if (!fee) return 0;
  if (typeof fee !== "object") return null;
  const current = fee.current_fee_rate || fee.fee_rate;
  if (current == null) return 0;
  const rate = num(typeof current === "object" ? current.fee_rate : current);
  if (rate == null) return null;
  return rate > 1 ? rate / 10000 : rate;
}

function lpReason(payload) {
  const holders = Array.isArray(payload.lp_holders) ? payload.lp_holders : [];
  const pools = Array.isArray(payload.dex) ? payload.dex : [];
  if (pools.some((pool) => pool && typeof pool === "object" && burnedEnough(pool.burn_percent))) return null;
  if (holders.length) {
    let lockedShare = 0;
    let known = false;
    for (const holder of holders) {
      if (!holder || typeof holder !== "object") continue;
      let share = num(holder.percent);
      if (share != null) {
        known = true;
        if (share > 1) share = share / 100;
      }
      if (lpLocked(holder) && (share == null || share >= 0.5 || !known)) return null;
      if (lpLocked(holder) && share != null) lockedShare += share;
    }
    if (lockedShare >= 0.5) return null;
    return "lp not locked or burned";
  }
  if (pools.length) return "lp not locked or burned";
  return "lp not verified";
}

function securityReason(chain, payload) {
  if (!payload || !Object.keys(payload).length) return "unverified";
  if (chain === "solana") {
    if (flagOn(payload.non_transferable) || String(payload.default_account_state || "") === "2") return "cannot sell";
    const tax = solanaTax(payload.transfer_fee);
    if (tax == null) return "unverified";
    if (tax > 0.05) return "sell tax " + (tax * 100).toFixed(1) + "%";
    if (statusOn(payload.mintable)) return "mintable";
    if (statusOn(payload.freezable)) return "pausable";
    if (statusOn(payload.transfer_fee_upgradable)) return "owner-changeable fees";
    return lpReason(payload);
  }
  if (!flagOn(payload.is_open_source)) return "unverified";
  if (flagOn(payload.is_honeypot) || flagOn(payload.cannot_sell_all) || flagOn(payload.cannot_buy)) {
    return flagOn(payload.is_honeypot) ? "honeypot" : "cannot sell";
  }
  for (const [label, field] of [["buy tax", "buy_tax"], ["sell tax", "sell_tax"]]) {
    const tax = taxFraction(payload[field]);
    if (tax == null) return "unverified";
    if (tax > 0.05) return label + " " + (tax * 100).toFixed(1) + "%";
  }
  if (flagOn(payload.is_mintable)) return "mintable";
  if (flagOn(payload.transfer_pausable)) return "pausable";
  if (flagOn(payload.is_blacklisted)) return "blacklist";
  if (flagOn(payload.slippage_modifiable) || flagOn(payload.personal_slippage_modifiable)) return "owner-changeable fees";
  return lpReason(payload);
}

function holderPercent(holder, total) {
  let share = num(holder.percent);
  if (share != null && share > 0) return share > 1 ? share / 100 : share;
  const balance = num(holder.balance);
  if (balance != null && total) return balance / total;
  return 0;
}

function devShare(payload, rows) {
  const found = [];
  for (const field of ["creator_percent", "owner_percent"]) {
    if (payload[field] != null && payload[field] !== "") {
      const parsed = num(payload[field]);
      if (parsed != null) found.push(parsed > 1 ? parsed / 100 : parsed);
    }
  }
  const creators = [];
  for (const field of ["creator_address", "owner_address"]) {
    if (payload[field]) creators.push(String(payload[field]));
  }
  for (const creator of payload.creators || []) {
    if (typeof creator === "string") creators.push(creator);
    else if (creator && typeof creator === "object") creators.push(String(creator.address || creator.account || ""));
  }
  const wanted = new Set(creators.filter(Boolean).map((item) => item.toLowerCase()));
  for (const [address, share] of rows) {
    if (wanted.has(address.toLowerCase())) found.push(share);
  }
  if (!found.length) return null;
  return Math.max(...found);
}

function holderFacts(payload) {
  const raw = Array.isArray(payload.holders) ? payload.holders : null;
  if (!raw || !raw.length) return null;
  const lpAddrs = new Set();
  for (const holder of payload.lp_holders || []) {
    if (holder && typeof holder === "object") lpAddrs.add(String(holder.address || holder.account || "").toLowerCase());
  }
  const total = num(payload.total_supply);
  const rows = [];
  let tagged = false;
  let sniperShare = 0;
  for (const holder of raw) {
    if (!holder || typeof holder !== "object") continue;
    const address = String(holder.address || holder.account || "");
    const tag = String(holder.tag || "");
    if (tag.trim()) tagged = true;
    const share = holderPercent(holder, total);
    if (isBurn(address) || lpAddrs.has(address.toLowerCase())) continue;
    if (["sniper", "fresh", "new wallet"].some((word) => tag.toLowerCase().includes(word))) sniperShare += share;
    rows.push([address, share, tag]);
  }
  rows.sort((a, b) => b[1] - a[1]);
  return {
    top10: rows.slice(0, 10).reduce((sum, row) => sum + row[1], 0),
    dev: devShare(payload, rows),
    sniper: tagged ? sniperShare : null,
    sniper_unavailable: !tagged,
  };
}

function holderReason(facts) {
  if (!facts) return "holders not verified";
  if (facts.top10 > 0.3) return "top 10 hold " + Math.round(facts.top10 * 100) + "%";
  if (facts.dev != null && facts.dev > 0.05) return "dev wallet holds " + Math.round(facts.dev * 100) + "%";
  if (facts.sniper != null && facts.sniper > 0.2) return "snipers hold " + Math.round(facts.sniper * 100) + "%";
  return null;
}

function applyFlow(coin) {
  const liquidity = coin.liquidity_usd || 0;
  const ratio = liquidity ? coin.volume_h24 / liquidity : 0;
  coin.suspicious = ratio > 10;
  const trades = coin.buys + coin.sells;
  if (trades <= 0) {
    coin.flow_points = 0;
    return;
  }
  const share = coin.buys / trades;
  let points = Math.floor((25 * Math.min(share, 0.85)) / 0.85);
  if (coin.suspicious) points = Math.min(points, 12);
  coin.flow_points = points;
}

function scoreCoin(coin, facts) {
  applyFlow(coin);
  const holderPoints = facts ? Math.floor(20 * Math.max(0, 1 - facts.top10 / 0.3)) : 0;
  coin.top10 = facts ? facts.top10 : 0;
  coin.dev = facts ? facts.dev : null;
  coin.sniper_unavailable = facts ? facts.sniper_unavailable : true;
  coin.parts = {
    security: 30,
    holders: holderPoints,
    flow: coin.flow_points,
    buzz: 0,
    liquidity_growth: 0,
  };
  coin.score = Object.values(coin.parts).reduce((sum, part) => sum + part, 0);
  const options = {
    holders: coin.parts.holders,
    flow: coin.suspicious ? 0 : coin.parts.flow,
    liquidity_growth: 0,
  };
  const best = Object.keys(options).sort((a, b) => options[b] - options[a])[0];
  coin.strongest_reason = options[best] <= 0 ? "security checks passed" : best === "holders" ? "top 10 hold " + Math.round(coin.top10 * 100) + "%" : "buyers lead sellers";
  if (coin.suspicious) coin.red_flag = "volume over 10x liquidity";
  else if (coin.top10 >= 0.2) coin.red_flag = "top 10 hold " + Math.round(coin.top10 * 100) + "%";
  else if (coin.dev != null && coin.dev >= 0.03) coin.red_flag = "dev wallet holds " + Math.round(coin.dev * 100) + "%";
  else if (coin.sniper_unavailable) coin.red_flag = "sniper data unavailable";
  else coin.red_flag = "no prior liquidity";
}

async function fetchSecurity(coin) {
  const chainId = GOPLUS_CHAIN[coin.chain];
  if (!chainId) return { missing: true, payload: null };
  const contract = encodeURIComponent(coin.contract);
  const url = chainId === "solana"
    ? GOPLUS + "/solana/token_security?contract_addresses=" + contract
    : GOPLUS + "/token_security/" + chainId + "?contract_addresses=" + contract;
  const payload = await getJson(url);
  if (!payload || (payload.code !== 1 && payload.code !== "1")) throw new Error("GoPlus rejected the request");
  const result = payload.result;
  if (!result || typeof result !== "object") return { missing: false, payload: {} };
  const wanted = normAddr(coin.chain, coin.contract);
  for (const [key, value] of Object.entries(result)) {
    if (normAddr(coin.chain, key) === wanted && value && typeof value === "object") return { missing: false, payload: value };
  }
  const first = Object.values(result)[0];
  return { missing: false, payload: first && typeof first === "object" ? first : {} };
}

async function rateQuery(query) {
  const found = await getJson(DEX + "/latest/dex/search?q=" + encodeURIComponent(query));
  const pairs = Array.isArray(found.pairs) ? found.pairs : [];
  const pair = choosePair(pairs, query);
  if (!pair) return { query, missing: true };
  let coin = coinFromPair(pair);
  await wait(350);
  try {
    const listed = await getJson(DEX + "/token-pairs/v1/" + coin.chain + "/" + coin.contract);
    if (Array.isArray(listed) && listed.length) {
      const deepest = listed.slice().sort((a, b) => pairLiquidity(b) - pairLiquidity(a))[0];
      coin = coinFromPair(deepest);
    }
  } catch (error) {
    // The search pair is enough when the pair list does not answer.
  }
  const reasons = marketReasons(coin);
  if (isCopycat(coin, pairs.map(peerFromPair))) reasons.push("an older or bigger copy uses this ticker");
  await wait(1000);
  let security;
  try {
    security = await fetchSecurity(coin);
  } catch (error) {
    return { query, coin, error: error.message || "GoPlus did not answer" };
  }
  if (security.missing) reasons.push("GoPlus does not cover this chain");
  else {
    const blocked = securityReason(coin.chain, security.payload);
    if (blocked) reasons.push(blocked);
    else {
      const facts = holderFacts(security.payload);
      const holders = holderReason(facts);
      if (holders) reasons.push(holders);
      else {
        scoreCoin(coin, facts);
        if (coin.score < 70) reasons.push("score is under 70");
      }
    }
  }
  return { query, coin, reasons };
}

function renderResult(row) {
  const card = el("article", null, "result");
  if (row.missing) {
    card.append(el("h3", row.query), el("p", "Not on DexScreener.", "verdict nope"));
    return card;
  }
  if (row.error) {
    card.append(el("h3", row.query), el("p", row.error, "verdict nope"));
    return card;
  }
  const coin = row.coin;
  card.append(el("h3", (coin.symbol || row.query) + (coin.name ? " · " + coin.name : "")));
  card.append(el("p", coin.chain, "chain"));
  card.append(el("p", coin.contract, "contract"));
  const gem = coin.score >= 70 && !row.reasons.length;
  card.append(el("p", gem ? "Gem " + coin.score : "Not a gem", gem ? "verdict gem" : "verdict nope"));
  if (gem) {
    card.append(el("p", coin.strongest_reason));
    card.append(el("p", "Red flag: " + coin.red_flag, "meta"));
  } else if (row.reasons.length) {
    const list = el("ul", null, "reasons");
    for (const reason of row.reasons) list.append(el("li", reason));
    card.append(list);
  }
  const facts = document.createElement("dl");
  const age = coin.pair_created_at ? Math.floor((Date.now() - coin.pair_created_at) / 86400000) + "d" : "—";
  for (const [label, value] of [
    ["Market cap", money(coin.market_cap)],
    ["Liquidity", money(coin.liquidity_usd)],
    ["24h volume", money(coin.volume_h24)],
    ["Price", coin.price_usd == null ? "—" : "$" + coin.price_usd],
    ["Pair age", age],
    ["Score", coin.score == null ? "—" : coin.score],
    ["Buzz", "buzz=no-x-source"],
  ]) {
    facts.append(el("dt", label), el("dd", value));
  }
  if (coin.parts) {
    facts.append(el("dt", "Points"), el("dd", "security " + coin.parts.security + " · holders " + coin.parts.holders + " · flow " + coin.parts.flow + " · buzz 0 · growth 0"));
  }
  card.append(facts);
  if (coin.url) {
    const link = document.createElement("a");
    link.href = coin.url;
    link.rel = "noreferrer";
    link.textContent = "Pair on DexScreener";
    card.append(link);
  }
  return card;
}

function splitQueries(raw) {
  const seen = new Set();
  const queries = [];
  for (const part of raw.split(/[\n,]+/)) {
    const query = part.trim();
    const key = query.toLowerCase();
    if (!query || seen.has(key)) continue;
    seen.add(key);
    queries.push(query);
  }
  return queries;
}

let job = 0;

async function onSubmit(event) {
  event.preventDefault();
  const mine = ++job;
  const status = document.getElementById("rate-status");
  const results = document.getElementById("rate-results");
  const button = event.currentTarget.querySelector("button");
  const queries = splitQueries(document.getElementById("coins").value);
  results.replaceChildren();
  if (!queries.length) {
    status.textContent = "Type a ticker or a contract address.";
    return;
  }
  const batch = queries.slice(0, MAX_QUERIES);
  button.disabled = true;
  status.textContent = queries.length > MAX_QUERIES
    ? "Rating the first " + MAX_QUERIES + ". The rest can go in a second search."
    : "Looking up " + batch.length + (batch.length === 1 ? " coin" : " coins") + " on DexScreener…";
  for (let index = 0; index < batch.length; index += 1) {
    if (mine !== job) return;
    status.textContent = "Rating " + (index + 1) + " of " + batch.length + ": " + batch[index];
    let row;
    try {
      row = await rateQuery(batch[index]);
    } catch (error) {
      row = { query: batch[index], error: "DexScreener did not answer." };
    }
    if (mine !== job) return;
    results.append(renderResult(row));
    if (index < batch.length - 1) await wait(350);
  }
  if (mine !== job) return;
  button.disabled = false;
  status.textContent = "Done. A score of 70 or more is a gem. First look, so liquidity growth is 0. Buzz is 0 because there is no X source.";
}

document.getElementById("rate-form").addEventListener("submit", onSubmit);
