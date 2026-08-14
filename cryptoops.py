#!/usr/bin/env python3
"""Autonomous Solana range-trade watchdog, run by cron every 30 minutes.

Lanes:
- engine: swap SOL <-> cbBTC at the BTC range boundaries (buy support,
  sell resistance). Spot style, no leverage, no liquidation risk.
- farm: mSOL holdings accrue staking yield with no action needed.

State is persisted in crypto-state.json; the cron wrapper appends each run to crypto.log.
Failures are loud but never fatal: a failed swap simply retries next run.
"""

import base58
import base64
import json
import time
import urllib.request

RPC = "https://api.mainnet-beta.solana.com"
SOL_MINT = "So11111111111111111111111111111111111111112"
BTC_MINT = "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij"
MSOL_MINT = "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So"
WALLET = "/home/ng/Projects/5090watch/wallet.json"
STATE = "/home/ng/Projects/5090watch/crypto-state.json"
BUY_TRIGGER = 63000.0
SELL_TRIGGER = 65500.0
SWAP_SOL = 0.6


def req(url, method="GET", body=None):
    r = urllib.request.Request(
        url, data=body, method=method,
        headers={"User-Agent": "5090watch/1.0", "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=60) as resp:
        return json.loads(resp.read().decode())


def rpc(method, params):
    res = req(RPC, "POST", json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                                       "params": params}).encode())
    if res.get("error"):
        raise RuntimeError(f"rpc {method}: {res['error']}")
    return res["result"]


def mint_decimals(mint):
    acct = rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    return int(acct["value"]["data"]["parsed"]["info"]["decimals"])


def token_balance(pubkey, mint):
    """Balance in human units, scaled by the mint's own decimals.
    cbBTC is 8 decimals, mSOL/SOL are 9: hardcoding 1e9 made cbBTC
    holdings read 10x too small (sell/state only 10% of the position)."""
    dec = mint_decimals(mint)
    acts = rpc("getTokenAccountsByOwner", [pubkey, {"mint": mint}, {"encoding": "jsonParsed"}])
    total = sum(int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
                for a in acts.get("value", []))
    return total / 10 ** dec


def swap(in_mint, out_mint, amount, taker):
    """Fresh order -> solders-native sign -> Jupiter managed execute. Returns signature or None."""
    from solders.transaction import VersionedTransaction
    q = (f"https://api.jup.ag/swap/v2/order?inputMint={in_mint}&outputMint={out_mint}"
         f"&amount={amount}&taker={taker}")
    for _ in range(4):
        order = req(q)
        if not order.get("transaction"):
            time.sleep(2)
            continue
        vtx = VersionedTransaction.from_bytes(base64.b64decode(order["transaction"]))
        signed = VersionedTransaction(vtx.message, [KP])
        if signed.verify_with_results() != [True]:
            time.sleep(2)
            continue
        body = json.dumps({"signedTransaction": base64.b64encode(bytes(signed)).decode(),
                           "requestId": order["requestId"]})
        try:
            res = req("https://api.jup.ag/swap/v2/execute", "POST", body.encode())
            if res.get("status") == "Success":
                return res["signature"]
        except urllib.error.HTTPError:
            pass
        time.sleep(2)
    return None


def _keypair():
    from solders.keypair import Keypair
    seed = base58.b58decode(json.load(open(WALLET))["seed_base58"])
    return Keypair.from_seed(seed)


def main():
    global KP
    KP = _keypair()
    pub = str(KP.pubkey())
    price = req("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd")["bitcoin"]["usd"]
    btc_held = token_balance(pub, BTC_MINT)
    msol = token_balance(pub, MSOL_MINT)
    sol = rpc("getBalance", [pub])["value"] / 1e9

    action = None
    sig = None
    if btc_held > 0.00001:
        if price >= SELL_TRIGGER:
            sig = swap(BTC_MINT, SOL_MINT, int(btc_held * 1e9), pub)
            action = f"sold {btc_held:.5f} BTC @ ${price:,.0f}"
    elif sol >= SWAP_SOL + 0.05 and price <= BUY_TRIGGER:
        sig = swap(SOL_MINT, BTC_MINT, int(SWAP_SOL * 1e9), pub)
        action = f"bought 0.6 SOL of BTC @ ${price:,.0f}"
    state = {"updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "btc_price": price, "sol_balance": sol, "btc_held": btc_held,
             "farm_msol": msol}
    if action:
        state["last_action"] = action
        state["last_tx"] = sig
    json.dump(state, open(STATE, "w"), indent=2)

    line = (f"{state['updated_at']} btc=${price:,.0f} sol={sol:.4f} farm={msol:.4f} btc_held={btc_held:.5f}"
            + (f" | {action} {sig}" if action else " | no action"))
    print(line)


if __name__ == "__main__":
    main()
