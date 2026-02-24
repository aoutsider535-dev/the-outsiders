"""
🏞 The Outsiders — Auto-Claim Redeemer

Redeems winning positions on Polymarket after market resolution.
Calls redeemPositions() on the CTF contract (Polygon).

Rate-limit safe:
- Only calls Polygon RPC (not Polymarket CLOB API)
- Min 30s between redemption attempts
- Max 5 redemptions per hour
- Retries with exponential backoff on failure
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
# Hardcoded selector for Factory.proxy((address,uint256,bytes)[]) — Solidity 0.5 ABIEncoderV2
FACTORY_PROXY_SELECTOR = bytes.fromhex("34ee9791")
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Minimal ABI for CTF redeemPositions
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

# Rate limiting
MIN_REDEEM_INTERVAL_S = 30  # Min seconds between redemption txs
MAX_REDEEMS_PER_HOUR = 5

# Minimal ABI for Polymarket ProxyWallet
# Uses proxy(ProxyCall[]) where ProxyCall = (address to, uint256 value, bytes data)
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

# Default Polygon RPC (free, generous limits)
DEFAULT_RPC = "https://1rpc.io/matic"

PST = timezone(timedelta(hours=-8))


class Redeemer:
    """Auto-claims winning positions from resolved Polymarket markets."""

    def __init__(self, private_key: str, wallet_address: str, rpc_url: str = None):
        self.rpc_url = rpc_url or os.environ.get("POLYGON_RPC_URL", DEFAULT_RPC)
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": 10}))
        self.account = Account.from_key(private_key)
        self.eoa_address = self.account.address  # EOA signs txs and pays gas
        self.proxy_address = Web3.to_checksum_address(wallet_address)  # Proxy holds tokens
        self.wallet_address = self.proxy_address  # Backward compat for balance checks
        self.ctf = self.w3.eth.contract(
            address=Web3.to_checksum_address(CTF_ADDRESS),
            abi=CTF_ABI,
        )
        self.proxy = self.w3.eth.contract(
            address=self.proxy_address,
            abi=PROXY_ABI,
        )

        # Rate limiting state
        self._last_redeem_ts = 0
        self._redeem_timestamps = []  # Rolling window for hourly cap

    def _ts(self) -> str:
        return datetime.now(timezone.utc).astimezone(PST).strftime("%I:%M:%S %p")

    def _rate_limit_ok(self) -> bool:
        """Check if we're within rate limits."""
        now = time.time()

        # Min interval check
        if now - self._last_redeem_ts < MIN_REDEEM_INTERVAL_S:
            return False

        # Hourly cap check
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
        """Check how many outcome tokens we hold for a given token ID."""
        try:
            balance = self.ctf.functions.balanceOf(
                self.wallet_address,
                int(token_id)
            ).call()
            return balance
        except Exception as e:
            logger.warning(f"[{self._ts()}] ⚠️ Balance check failed for token {token_id[:12]}...: {e}")
            return 0

    def is_condition_resolved(self, condition_id: str) -> bool:
        """Check if a condition has been resolved (payout denominator > 0)."""
        try:
            # condition_id should be bytes32
            if not condition_id.startswith("0x"):
                condition_id = "0x" + condition_id
            cid_bytes = bytes.fromhex(condition_id[2:].zfill(64))
            denom = self.ctf.functions.payoutDenominator(cid_bytes).call()
            return denom > 0
        except Exception as e:
            logger.warning(f"[{self._ts()}] ⚠️ Resolution check failed: {e}")
            return False

    def redeem(self, condition_id: str, index_sets: list[int] = None) -> dict:
        """
        Redeem winning positions for a resolved market.

        Args:
            condition_id: The market's condition ID (hex string)
            index_sets: Which outcome indices to redeem. 
                       For binary markets: [1, 2] (1=Yes, 2=No)
                       Default: [1, 2] (redeem both — only winning side pays out)

        Returns:
            dict with 'success', 'tx_hash', 'error'
        """
        if not self._rate_limit_ok():
            return {"success": False, "error": "rate_limited", "tx_hash": None}

        if index_sets is None:
            index_sets = [1, 2]  # Both outcomes — CTF only pays winning side

        try:
            # Normalize condition_id
            if not condition_id.startswith("0x"):
                condition_id = "0x" + condition_id
            cid_bytes = bytes.fromhex(condition_id[2:].zfill(64))

            # parentCollectionId = bytes32(0) for standard markets
            parent = b'\x00' * 32

            # Encode the CTF redeemPositions call
            redeem_data = self.ctf.functions.redeemPositions(
                Web3.to_checksum_address(USDC_ADDRESS),
                parent,
                cid_bytes,
                index_sets,
            )._encode_transaction_data()

            # Route through Factory.proxy() — Factory forwards to our proxy wallet
            # Factory uses _msgSender() to derive our proxy wallet address
            # Solidity 0.5 ABIEncoderV2 encodes tuple[] with an extra length prefix
            # that eth_abi doesn't produce, so we patch the encoding manually.
            proxy_call_data = bytes.fromhex(redeem_data[2:])
            encoded_args = abi_encode(
                ['(address,uint256,bytes)[]'],
                [[(Web3.to_checksum_address(CTF_ADDRESS), 0, proxy_call_data)]]
            )
            # Patch: insert 32-byte word (value=1) at byte 96 and fix bytes offset 0x60→0x80
            # This matches the on-chain encoding used by Polymarket's frontend
            patched = bytearray(encoded_args[:96]) + bytearray((1).to_bytes(32, 'big')) + bytearray(encoded_args[96:])
            # Fix the tuple's bytes-offset field (now at word[6], byte 192)
            patched[192:224] = (0x80).to_bytes(32, 'big')
            calldata = FACTORY_PROXY_SELECTOR + bytes(patched)

            tx = {
                "from": self.eoa_address,
                "to": Web3.to_checksum_address(FACTORY_ADDRESS),
                "data": calldata,
                "nonce": self.w3.eth.get_transaction_count(self.eoa_address),
                "gas": 300_000,
                "gasPrice": max(self.w3.eth.gas_price, 30_000_000_000),  # min 30 Gwei
                "chainId": 137,
                "value": 0,
            }

            # Sign and send
            signed = self.account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            tx_hash_hex = tx_hash.hex()

            logger.info(f"[{self._ts()}] 📤 Redeem tx sent: {tx_hash_hex}")

            # Wait for receipt (up to 60s)
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

            self._record_redeem()

            if receipt["status"] == 1:
                logger.info(f"[{self._ts()}] ✅ Redeem confirmed in block {receipt['blockNumber']}")
                return {
                    "success": True,
                    "tx_hash": tx_hash_hex,
                    "block": receipt["blockNumber"],
                    "gas_used": receipt["gasUsed"],
                }
            else:
                logger.warning(f"[{self._ts()}] ❌ Redeem tx reverted: {tx_hash_hex}")
                return {"success": False, "tx_hash": tx_hash_hex, "error": "tx_reverted"}

        except Exception as e:
            logger.error(f"[{self._ts()}] ❌ Redeem failed: {e}")
            return {"success": False, "tx_hash": None, "error": str(e)}

    def sweep_positions(self, positions: list[dict]) -> list[dict]:
        """
        Sweep all claimable positions. Each position dict should have:
        - condition_id: str
        - token_id: str (the winning token)
        - market_slug: str (for logging)

        Returns list of results.
        """
        results = []
        for pos in positions:
            cid = pos.get("condition_id")
            token_id = pos.get("token_id")
            slug = pos.get("market_slug", "unknown")

            if not cid:
                continue

            # Check we actually hold tokens
            balance = self.check_token_balance(token_id) if token_id else 0
            if balance == 0:
                logger.info(f"[{self._ts()}] ⏭️ No tokens to redeem for {slug}")
                results.append({"market": slug, "skipped": True, "reason": "no_balance"})
                continue

            # Check resolution
            if not self.is_condition_resolved(cid):
                logger.info(f"[{self._ts()}] ⏳ {slug} not yet resolved on-chain")
                results.append({"market": slug, "skipped": True, "reason": "not_resolved"})
                continue

            logger.info(f"[{self._ts()}] 💰 Redeeming {balance} tokens for {slug}")
            result = self.redeem(cid)
            result["market"] = slug
            result["token_balance"] = balance
            results.append(result)

            # Respect rate limits between positions
            if not self._rate_limit_ok():
                logger.info(f"[{self._ts()}] ⏸️ Rate limit hit, stopping sweep")
                break

        return results
