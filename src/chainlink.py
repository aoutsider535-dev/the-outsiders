"""
🔗 Chainlink Oracle Reader

Reads price data from Chainlink aggregator feeds on Polygon.
Used by sniper bot to determine direction from the SAME oracle Polymarket resolves on.
"""
import concurrent.futures
from web3 import Web3

POLYGON_RPCS = [
    "https://polygon.drpc.org",
    "https://polygon-bor-rpc.publicnode.com",
]

# Chainlink price feed addresses on Polygon mainnet
CHAINLINK_FEEDS = {
    "btc": "0xc907E116054Ad103354f2D350FD2514433D57F6f",   # BTC/USD — verified ✅
    "eth": "0xF9680D99D6C9589e2a93a78A04A279e509205945",   # ETH/USD — verified ✅
    "xrp": "0x785ba89291f676b5386652eB12b30cF361020694",   # XRP/USD — verified ✅
    # SOL: no active on-chain aggregator on Polygon — use Binance fallback
}

AGG_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"name": "roundId", "type": "uint80"},
            {"name": "answer", "type": "int256"},
            {"name": "startedAt", "type": "uint256"},
            {"name": "updatedAt", "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "_roundId", "type": "uint80"}],
        "name": "getRoundData",
        "outputs": [
            {"name": "roundId", "type": "uint80"},
            {"name": "answer", "type": "int256"},
            {"name": "startedAt", "type": "uint256"},
            {"name": "updatedAt", "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class ChainlinkReader:
    """Reads Chainlink oracle prices on Polygon for direction determination."""

    def __init__(self, rpcs=None):
        self.rpcs = rpcs or POLYGON_RPCS
        self._w3 = None
        self._feeds = {}      # asset -> contract
        self._decimals = {}   # asset -> int
        self._latest_rounds = {}  # asset -> round_id

    def _get_w3(self):
        """Get a working Web3 connection."""
        if self._w3:
            try:
                self._w3.eth.block_number
                return self._w3
            except Exception:
                pass
        for rpc in self.rpcs:
            try:
                w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 8}))
                w3.eth.block_number
                self._w3 = w3
                return w3
            except Exception:
                continue
        raise ConnectionError("All RPCs failed")

    def _get_feed(self, asset):
        """Get or create a Chainlink feed contract for an asset."""
        if asset not in self._feeds:
            addr = CHAINLINK_FEEDS.get(asset)
            if not addr:
                raise ValueError(f"No Chainlink feed for {asset}")
            w3 = self._get_w3()
            self._feeds[asset] = w3.eth.contract(
                address=Web3.to_checksum_address(addr), abi=AGG_ABI
            )
            self._decimals[asset] = self._feeds[asset].functions.decimals().call()
        return self._feeds[asset], self._decimals[asset]

    def get_latest(self, asset):
        """Get latest price and round ID for an asset."""
        feed, dec = self._get_feed(asset)
        rd = feed.functions.latestRoundData().call()
        self._latest_rounds[asset] = rd[0]
        return {
            "round_id": rd[0],
            "price": rd[1] / (10 ** dec),
            "updated_at": rd[3],
        }

    def _get_round(self, asset, round_id):
        """Get a specific round's data."""
        feed, dec = self._get_feed(asset)
        rd = feed.functions.getRoundData(round_id).call()
        return {
            "round_id": rd[0],
            "price": rd[1] / (10 ** dec),
            "updated_at": rd[3],
        }

    def _batch_get_rounds(self, asset, round_ids):
        """Fetch multiple rounds in parallel using thread pool."""
        feed_addr = CHAINLINK_FEEDS.get(asset)
        if not feed_addr:
            return {}
        _, dec = self._get_feed(asset)

        def fetch_one(rid_rpc):
            rid, rpc_url = rid_rpc
            try:
                w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 6}))
                feed = w3.eth.contract(
                    address=Web3.to_checksum_address(feed_addr), abi=AGG_ABI
                )
                rd = feed.functions.getRoundData(rid).call()
                return rid, {"round_id": rd[0], "price": rd[1] / (10 ** dec), "updated_at": rd[3]}
            except Exception:
                return rid, None

        # Distribute across RPCs
        tasks = [(rid, self.rpcs[i % len(self.rpcs)]) for i, rid in enumerate(round_ids)]
        results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            for rid, data in ex.map(fetch_one, tasks):
                if data:
                    results[rid] = data
        return results

    def find_prices_at(self, asset, target_open_ts, target_close_ts):
        """Find Chainlink prices at two timestamps (open and close) in one batch.

        Returns (open_data, close_data) tuple. Uses parallel RPC calls.
        Optimized for the 5-min window use case.
        """
        if asset not in self._latest_rounds:
            self.get_latest(asset)

        latest_round = self._latest_rounds[asset]
        latest_data = self._get_round(asset, latest_round)
        latest_ts = latest_data["updated_at"]

        # Estimate round positions (~32s per round)
        est_open_back = int((latest_ts - target_open_ts) / 32)
        est_close_back = int((latest_ts - target_close_ts) / 32)

        open_center = latest_round - est_open_back
        close_center = latest_round - est_close_back

        # Build list of round IDs to fetch: ±3 around each estimate
        round_ids = set()
        for offset in range(-4, 5):
            round_ids.add(open_center + offset)
            round_ids.add(close_center + offset)

        # Fetch all rounds in parallel (max ~14 unique rounds, many may overlap)
        batch = self._batch_get_rounds(asset, list(round_ids))

        # Find best match for open (last round AT or BEFORE target)
        open_data = None
        close_data = None

        for rid, rd in batch.items():
            # Open: closest round AT or BEFORE target_open_ts
            if rd["updated_at"] <= target_open_ts:
                if open_data is None or rd["updated_at"] > open_data["updated_at"]:
                    open_data = rd
            # Close: closest round AT or BEFORE target_close_ts
            if rd["updated_at"] <= target_close_ts:
                if close_data is None or rd["updated_at"] > close_data["updated_at"]:
                    close_data = rd

        # Fallback: if no round before target, use closest after
        if not open_data:
            candidates = [(rid, rd) for rid, rd in batch.items()]
            if candidates:
                open_data = min(candidates, key=lambda x: abs(x[1]["updated_at"] - target_open_ts))[1]
        if not close_data:
            candidates = [(rid, rd) for rid, rd in batch.items()]
            if candidates:
                close_data = min(candidates, key=lambda x: abs(x[1]["updated_at"] - target_close_ts))[1]

        return open_data, close_data

    def get_direction(self, asset, window_start_ts, window_end_ts):
        """Determine UP/DOWN direction for a 5-min window using Chainlink.

        Returns: {"direction": "up"|"down"|None, "open": float, "close": float,
                  "move_pct": float, "open_round": int, "close_round": int}

        Uses the same oracle Polymarket resolves on = 100% accuracy.
        """
        try:
            open_data, close_data = self.find_prices_at(asset, window_start_ts, window_end_ts)

            if not open_data or not close_data:
                return None

            open_price = open_data["price"]
            close_price = close_data["price"]
            move_pct = abs(close_price - open_price) / open_price * 100

            # Polymarket rule: "greater than or EQUAL TO" = Up
            if close_price >= open_price:
                direction = "up"
            else:
                direction = "down"

            return {
                "direction": direction,
                "open": open_price,
                "close": close_price,
                "move_pct": move_pct,
                "open_round": open_data["round_id"],
                "close_round": close_data["round_id"],
                "open_ts_delta": open_data["updated_at"] - window_start_ts,
                "close_ts_delta": close_data["updated_at"] - window_end_ts,
            }
        except Exception as e:
            return None
