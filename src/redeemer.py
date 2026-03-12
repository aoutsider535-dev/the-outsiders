"""
🏞 The Outsiders — Auto-Claim Redeemer

Redeems winning positions on Polymarket after market resolution.
Calls redeemPositions() on the CTF contract (Polygon).

v2: Fixed nonce management, stuck tx replacement, RPC error handling.
"""
import time
import os
import json
import logging
from datetime import datetime, timezone, timedelta
from web3 import Web3
from eth_account import Account
from eth_abi import encode as abi_encode

logger = logging.getLogger(__name__)

# Contract addresses (Polygon mainnet)
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
FACTORY_ADDRESS = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
FACTORY_PROXY_SELECTOR = bytes.fromhex("34ee9791")
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

CTF_ABI = json.loads("""[
    {
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"}
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id", "type": "uint256"}
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "conditionId", "type": "bytes32"}
        ],
        "name": "payoutDenominator",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]""")

PROXY_ABI = json.loads("""[
    {
        "inputs": [
            {
                "components": [
                    {"name": "to", "type": "address"},
                    {"name": "value", "type": "uint256"},
                    {"name": "data", "type": "bytes"}
                ],
                "name": "calls",
                "type": "tuple[]"
            }
        ],
        "name": "proxy",
        "outputs": [{"name": "returnValues", "type": "bytes[]"}],
        "stateMutability": "payable",
        "type": "function"
    }
]""")

POLYGON_RPCS = [
    "https://polygon.drpc.org",
    "https://polygon-bor-rpc.publicnode.com",
    "https://1rpc.io/matic",
]
DEFAULT_RPC = POLYGON_RPCS[0]

# Rate limiting
MIN_REDEEM_INTERVAL_S = 30
MAX_REDEEMS_PER_HOUR = 15

PST = timezone(timedelta(hours=-8))


class Redeemer:
    """Auto-claims winning positions from resolved Polymarket markets."""

    def __init__(self, private_key: str, wallet_address: str, rpc_url: str = None):
        self.rpc_url = rpc_url or os.environ.get("POLYGON_RPC_URL", DEFAULT_RPC)
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": 10}))
        self.account = Account.from_key(private_key)
        self.eoa_address = self.account.address
        self.proxy_address = Web3.to_checksum_address(wallet_address)
        self.wallet_address = self.proxy_address
        self.ctf = self.w3.eth.contract(
            address=Web3.to_checksum_address(CTF_ADDRESS),
            abi=CTF_ABI,
        )
        self.proxy = self.w3.eth.contract(
            address=self.proxy_address,
            abi=PROXY_ABI,
        )

        self._last_redeem_ts = 0
        self._redeem_timestamps = []
        # Pending tx tracking for stuck tx replacement
        self._pending_tx = None  # {"hash": hex, "nonce": int, "sent_at": float}

    def _ts(self) -> str:
        return datetime.now(timezone.utc).astimezone(PST).strftime("%I:%M:%S %p")

    def _get_working_w3(self):
        """Get a working Web3 connection, trying all RPCs."""
        for rpc_url in POLYGON_RPCS:
            try:
                w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 8}))
                w3.eth.block_number  # Quick connectivity check
                return w3, rpc_url
            except Exception:
                continue
        return self.w3, self.rpc_url

    def _get_chain_nonce(self) -> int:
        """Get nonce directly from chain. Always use chain truth, never track locally."""
        w3, rpc = self._get_working_w3()
        self.w3 = w3
        try:
            # Use 'pending' to account for txs in mempool
            return w3.eth.get_transaction_count(self.eoa_address, "pending")
        except Exception:
            try:
                return w3.eth.get_transaction_count(self.eoa_address, "latest")
            except Exception:
                return 0

    def _check_pending_tx(self) -> bool:
        """Check if we have a pending tx and handle it.
        Returns True if there's a stuck tx that needs replacement."""
        if not self._pending_tx:
            return False

        tx_hash = self._pending_tx["hash"]
        nonce = self._pending_tx["nonce"]
        sent_at = self._pending_tx["sent_at"]
        age_s = time.time() - sent_at

        # Check if tx was mined
        w3, _ = self._get_working_w3()
        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash)
            if receipt:
                if receipt["status"] == 1:
                    logger.info(f"[{self._ts()}] ✅ Pending tx confirmed! Block {receipt['blockNumber']}")
                else:
                    logger.warning(f"[{self._ts()}] ❌ Pending tx reverted")
                self._pending_tx = None
                return False
        except Exception:
            pass  # Not mined yet

        # If tx is older than 120s, it's stuck
        if age_s > 120:
            logger.warning(f"[{self._ts()}] ⚠️ Tx {tx_hash[:16]}... stuck for {age_s:.0f}s (nonce {nonce})")
            return True

        # Still waiting, not stuck yet
        logger.info(f"[{self._ts()}] ⏳ Tx pending ({age_s:.0f}s), waiting...")
        return False

    def _get_gas_params(self, w3, boost_pct=0):
        """Get EIP-1559 gas parameters. boost_pct for replacement txs."""
        try:
            base_fee = w3.eth.gas_price
        except Exception:
            base_fee = 50_000_000_000  # 50 Gwei fallback

        # Base formula: 2x base + tip
        tip = max(int(base_fee * 0.3), 30_000_000_000)  # 30% or 30 Gwei min
        max_fee = int(base_fee * 2) + tip

        # Apply boost for replacement (must be > original gas by at least 10%)
        if boost_pct > 0:
            multiplier = 1 + (boost_pct / 100)
            tip = int(tip * multiplier)
            max_fee = int(max_fee * multiplier)

        logger.info(f"[{self._ts()}] Gas: base={base_fee/1e9:.0f} maxFee={max_fee/1e9:.0f} tip={tip/1e9:.0f} Gwei" +
                    (f" (+{boost_pct}% boost)" if boost_pct else ""))
        return max_fee, tip

    def _rate_limit_ok(self) -> bool:
        now = time.time()
        if now - self._last_redeem_ts < MIN_REDEEM_INTERVAL_S:
            return False
        one_hour_ago = now - 3600
        self._redeem_timestamps = [t for t in self._redeem_timestamps if t > one_hour_ago]
        if len(self._redeem_timestamps) >= MAX_REDEEMS_PER_HOUR:
            return False
        return True

    def _record_redeem(self):
        now = time.time()
        self._last_redeem_ts = now
        self._redeem_timestamps.append(now)

    def check_token_balance(self, token_id: str) -> int:
        try:
            balance = self.ctf.functions.balanceOf(
                self.wallet_address, int(token_id)
            ).call()
            return balance
        except Exception as e:
            logger.warning(f"[{self._ts()}] ⚠️ Balance check failed for token {token_id[:12]}...: {e}")
            return 0

    def is_condition_resolved(self, condition_id: str) -> bool:
        try:
            if not condition_id.startswith("0x"):
                condition_id = "0x" + condition_id
            cid_bytes = bytes.fromhex(condition_id[2:].zfill(64))
            denom = self.ctf.functions.payoutDenominator(cid_bytes).call()
            return denom > 0
        except Exception as e:
            logger.warning(f"[{self._ts()}] ⚠️ Resolution check failed: {e}")
            return False

    def _build_redeem_calldata(self, condition_id: str, index_sets: list[int]) -> bytes:
        """Build the full Factory.proxy() calldata for a redeem."""
        if not condition_id.startswith("0x"):
            condition_id = "0x" + condition_id
        cid_bytes = bytes.fromhex(condition_id[2:].zfill(64))
        parent = b'\x00' * 32

        redeem_data = self.ctf.functions.redeemPositions(
            Web3.to_checksum_address(USDC_ADDRESS),
            parent,
            cid_bytes,
            index_sets,
        )._encode_transaction_data()

        proxy_call_data = bytes.fromhex(redeem_data[2:])
        encoded_args = abi_encode(
            ['(address,uint256,bytes)[]'],
            [[(Web3.to_checksum_address(CTF_ADDRESS), 0, proxy_call_data)]]
        )
        # Patch for Solidity 0.5 ABIEncoderV2 encoding
        patched = bytearray(encoded_args[:96]) + bytearray((1).to_bytes(32, 'big')) + bytearray(encoded_args[96:])
        patched[192:224] = (0x80).to_bytes(32, 'big')
        return FACTORY_PROXY_SELECTOR + bytes(patched)

    def _send_tx(self, calldata: bytes, nonce: int, gas_boost_pct: int = 0) -> dict:
        """Sign, send tx, and track it. Returns {"success": bool, "tx_hash": str, ...}"""
        w3, rpc_name = self._get_working_w3()
        max_fee, tip = self._get_gas_params(w3, boost_pct=gas_boost_pct)

        tx = {
            "from": self.eoa_address,
            "to": Web3.to_checksum_address(FACTORY_ADDRESS),
            "data": calldata,
            "nonce": nonce,
            "gas": 300_000,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": tip,
            "chainId": 137,
            "value": 0,
            "type": 2,
        }

        signed = self.account.sign_transaction(tx)
        tx_hash = None

        # Broadcast to all RPCs for better propagation
        # But only try each once, with short timeout
        for rpc_url in POLYGON_RPCS:
            try:
                w3_send = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 8}))
                h = w3_send.eth.send_raw_transaction(signed.raw_transaction)
                if tx_hash is None:
                    tx_hash = h.hex()
                logger.info(f"[{self._ts()}] 📡 Broadcast to {rpc_url.split('//')[1].split('/')[0]}")
            except Exception as e:
                err_str = str(e).lower()
                # "already known" means it's in the mempool — that's actually OK
                if "already known" in err_str or "known transaction" in err_str:
                    logger.info(f"[{self._ts()}] 📡 {rpc_url.split('//')[1].split('/')[0]}: already in mempool (OK)")
                    if tx_hash is None:
                        # We don't have the hash from this RPC but the tx is there
                        tx_hash = signed.hash.hex()
                elif "nonce too low" in err_str:
                    logger.warning(f"[{self._ts()}] ⚠️ Nonce {nonce} already used — tx was mined!")
                    self._pending_tx = None
                    return {"success": False, "error": "nonce_used", "tx_hash": None}
                elif "replacement transaction underpriced" in err_str:
                    logger.warning(f"[{self._ts()}] ⚠️ Need higher gas to replace stuck tx")
                    return {"success": False, "error": "underpriced", "tx_hash": None}
                else:
                    logger.warning(f"[{self._ts()}] RPC {rpc_url.split('//')[1].split('/')[0]} error: {e}")

        if tx_hash is None:
            return {"success": False, "error": "all RPCs failed", "tx_hash": None}

        # Track pending tx
        self._pending_tx = {"hash": tx_hash, "nonce": nonce, "sent_at": time.time()}
        logger.info(f"[{self._ts()}] 📤 Tx sent: {tx_hash[:20]}... (nonce {nonce})")

        # Wait for receipt with escalating timeout
        for attempt, (wait_rpc, wait_timeout) in enumerate([
            (POLYGON_RPCS[0], 60),
            (POLYGON_RPCS[1], 60),
            (POLYGON_RPCS[0], 60),
        ]):
            try:
                w3_wait = Web3(Web3.HTTPProvider(wait_rpc, request_kwargs={"timeout": 10}))
                receipt = w3_wait.eth.wait_for_transaction_receipt(tx_hash, timeout=wait_timeout)
                self._pending_tx = None
                self._record_redeem()

                if receipt["status"] == 1:
                    logger.info(f"[{self._ts()}] ✅ Confirmed! Block {receipt['blockNumber']}, gas {receipt['gasUsed']}")
                    return {
                        "success": True,
                        "tx_hash": tx_hash,
                        "block": receipt["blockNumber"],
                        "gas_used": receipt["gasUsed"],
                    }
                else:
                    logger.warning(f"[{self._ts()}] ❌ Tx reverted: {tx_hash}")
                    return {"success": False, "tx_hash": tx_hash, "error": "tx_reverted"}
            except Exception as e:
                if attempt < 2:
                    logger.info(f"[{self._ts()}] ⏳ Still waiting... (attempt {attempt+1})")
                continue

        # Tx sent but not confirmed within timeout — leave it pending
        logger.warning(f"[{self._ts()}] ⏳ Tx {tx_hash[:16]}... not confirmed yet, tracking as pending")
        return {"success": False, "tx_hash": tx_hash, "error": "timeout_pending"}

    def redeem(self, condition_id: str, index_sets: list[int] = None) -> dict:
        """Redeem winning positions for a resolved market."""
        if not self._rate_limit_ok():
            return {"success": False, "error": "rate_limited", "tx_hash": None}

        if index_sets is None:
            index_sets = [1, 2]

        try:
            # Check for stuck pending tx first
            is_stuck = self._check_pending_tx()
            if is_stuck:
                # Replace stuck tx with higher gas
                logger.info(f"[{self._ts()}] 🔄 Replacing stuck tx with +50% gas boost")
                calldata = self._build_redeem_calldata(condition_id, index_sets)
                nonce = self._pending_tx["nonce"]
                result = self._send_tx(calldata, nonce, gas_boost_pct=50)
                if result.get("error") == "underpriced":
                    # Try even higher boost
                    logger.info(f"[{self._ts()}] 🔄 Retrying with +100% gas boost")
                    result = self._send_tx(calldata, nonce, gas_boost_pct=100)
                return result
            elif self._pending_tx:
                # Still pending but not stuck yet — don't send new tx
                return {"success": False, "error": "tx_pending", "tx_hash": self._pending_tx["hash"]}

            # Fresh redeem — get nonce from chain
            calldata = self._build_redeem_calldata(condition_id, index_sets)
            nonce = self._get_chain_nonce()
            return self._send_tx(calldata, nonce)

        except Exception as e:
            logger.error(f"[{self._ts()}] ❌ Redeem failed: {e}")
            return {"success": False, "tx_hash": None, "error": str(e)}

    def sweep_positions(self, positions: list[dict]) -> list[dict]:
        """Sweep all claimable positions."""
        results = []
        for pos in positions:
            cid = pos.get("condition_id")
            token_id = pos.get("token_id")
            slug = pos.get("market_slug", "unknown")

            if not cid:
                continue

            balance = self.check_token_balance(token_id) if token_id else 0
            if balance == 0:
                logger.info(f"[{self._ts()}] ⏭️ No tokens to redeem for {slug}")
                results.append({"market": slug, "skipped": True, "reason": "no_balance"})
                continue

            if not self.is_condition_resolved(cid):
                logger.info(f"[{self._ts()}] ⏳ {slug} not yet resolved on-chain")
                results.append({"market": slug, "skipped": True, "reason": "not_resolved"})
                continue

            logger.info(f"[{self._ts()}] 💰 Redeeming {balance} tokens for {slug}")
            result = self.redeem(cid)
            result["market"] = slug
            result["token_balance"] = balance
            results.append(result)

            if not self._rate_limit_ok():
                logger.info(f"[{self._ts()}] ⏸️ Rate limit hit, stopping sweep")
                break

        return results
