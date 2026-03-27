"""
Edge Source: Sports Statistical Modeling
==========================================
Uses free public APIs for NBA/NFL/MLB stats to find mispriced player props
and game outcomes on Polymarket.

Data sources (free, no API key):
- balldontlie.io (NBA stats)
- nba.com/stats (official)
- odds-api.com (free tier, line comparison)

Edge thesis: Polymarket sports markets are less efficient than Vegas 
because they attract crypto/prediction market traders, not sharp sports 
bettors. Statistical models based on recent player performance can 
identify mispriced props.
"""

import requests
import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass


@dataclass
class PlayerStats:
    """Recent player performance stats."""
    name: str
    team: str
    games_played: int
    # Per-game averages (last N games)
    pts_avg: float = 0.0
    reb_avg: float = 0.0
    ast_avg: float = 0.0
    pts_std: float = 0.0  # Standard deviation (for confidence)
    reb_std: float = 0.0
    ast_std: float = 0.0
    # Hit rates for over/under analysis
    pts_over_rates: Dict[float, float] = None  # threshold -> % of games over
    reb_over_rates: Dict[float, float] = None
    ast_over_rates: Dict[float, float] = None


class SportsEdge:
    """Find mispriced sports props using statistical analysis."""
    
    def __init__(self):
        self.player_cache: Dict[str, PlayerStats] = {}
        self.cache_ts: Dict[str, float] = {}
        self.cache_ttl = 3600  # 1 hour cache
    
    def analyze_market(self, question: str, outcomes: List[str], 
                       prices: List[float]) -> Optional[dict]:
        """
        Analyze a sports market question and estimate fair probability.
        
        Returns dict with probability estimate or None if can't analyze.
        """
        q = question.lower()
        
        # ── Player Props (Points/Rebounds/Assists Over/Under) ──
        prop_match = self._parse_player_prop(question)
        if prop_match:
            return self._analyze_player_prop(prop_match, outcomes, prices)
        
        # ── Game Spread ──
        spread_match = self._parse_spread(question)
        if spread_match:
            return self._analyze_spread(spread_match, outcomes, prices)
        
        # ── Game Total (Over/Under) ──
        total_match = self._parse_total(question)
        if total_match:
            return self._analyze_total(total_match, outcomes, prices)
        
        return None
    
    def _parse_player_prop(self, question: str) -> Optional[dict]:
        """Parse 'Player Name: Points O/U 25.5' format."""
        # Pattern: "Player Name: Stat O/U Number"
        patterns = [
            r"(.+?):\s*(Points|Rebounds|Assists|3-Pointers?)\s*O/U\s*([\d.]+)",
            r"(.+?)\s*-\s*(Points|Rebounds|Assists)\s*Over/Under\s*([\d.]+)",
        ]
        for pattern in patterns:
            m = re.search(pattern, question, re.IGNORECASE)
            if m:
                return {
                    "type": "player_prop",
                    "player": m.group(1).strip(),
                    "stat": m.group(2).lower(),
                    "line": float(m.group(3)),
                }
        return None
    
    def _parse_spread(self, question: str) -> Optional[dict]:
        """Parse 'Spread: Team (-4.5)' format."""
        m = re.search(r"Spread:\s*(.+?)\s*\(([-+]?[\d.]+)\)", question)
        if m:
            return {
                "type": "spread",
                "team": m.group(1).strip(),
                "line": float(m.group(2)),
            }
        return None
    
    def _parse_total(self, question: str) -> Optional[dict]:
        """Parse 'Team A vs Team B: O/U 219.5' format."""
        m = re.search(r"(.+?)\s*vs\.?\s*(.+?):\s*O/U\s*([\d.]+)", question)
        if m:
            return {
                "type": "total",
                "team1": m.group(1).strip(),
                "team2": m.group(2).strip(),
                "line": float(m.group(3)),
            }
        return None
    
    def _analyze_player_prop(self, prop: dict, outcomes: List[str], 
                              prices: List[float]) -> Optional[dict]:
        """Estimate over/under probability for a player prop."""
        player = prop["player"]
        stat = prop["stat"]
        line = prop["line"]
        
        # Fetch player stats
        stats = self._get_player_stats(player)
        if not stats:
            return None
        
        # Get relevant stat
        if "point" in stat:
            avg, std = stats.pts_avg, stats.pts_std
        elif "rebound" in stat:
            avg, std = stats.reb_avg, stats.reb_std
        elif "assist" in stat:
            avg, std = stats.ast_avg, stats.ast_std
        else:
            return None
        
        if std == 0:
            return None
        
        # Estimate probability using normal distribution approximation
        # P(X > line) where X ~ N(avg, std)
        import math
        z = (line - avg) / std
        # Standard normal CDF approximation
        over_prob = 1 - self._norm_cdf(z)
        
        # Find which outcome is "Over"
        over_idx = None
        for i, outcome in enumerate(outcomes):
            if "over" in outcome.lower() or "more" in outcome.lower():
                over_idx = i
                break
            if "yes" in outcome.lower() and i == 0:
                over_idx = 0
                break
        
        if over_idx is None:
            over_idx = 0  # Default: first outcome is "Over/Yes"
        
        under_idx = 1 - over_idx
        market_over = prices[over_idx]
        
        edge = over_prob - market_over
        
        if abs(edge) < 0.03:  # Need at least 3% edge
            return None
        
        # Determine direction
        if edge > 0:
            # Over is underpriced
            outcome_idx = over_idx
            our_prob = over_prob
        else:
            # Under is underpriced
            outcome_idx = under_idx
            our_prob = 1 - over_prob
            edge = abs(edge)
        
        confidence = self._calc_confidence(stats.games_played, std, abs(avg - line))
        
        return {
            "probability": our_prob,
            "confidence": confidence,
            "edge": edge,
            "ev": edge - 0.02,
            "outcome_idx": outcome_idx,
            "thesis": (f"{player} averages {avg:.1f} {stat} (std {std:.1f}) over "
                      f"{stats.games_played} games. Line is {line}. "
                      f"Model says {over_prob*100:.0f}% over, market says {market_over*100:.0f}%."),
            "sources": ["player_stats", "normal_distribution_model"],
        }
    
    def _analyze_spread(self, spread: dict, outcomes: List[str],
                        prices: List[float]) -> Optional[dict]:
        """Analyze game spread market. Placeholder for future enhancement."""
        # Need team strength ratings for this — future work
        return None
    
    def _analyze_total(self, total: dict, outcomes: List[str],
                       prices: List[float]) -> Optional[dict]:
        """Analyze game total market. Placeholder for future enhancement."""
        # Need pace/efficiency data — future work
        return None
    
    def _get_player_stats(self, player_name: str, last_n_games: int = 10) -> Optional[PlayerStats]:
        """
        Fetch recent player stats from free NBA API.
        Uses balldontlie.io (free, no auth needed).
        """
        # Check cache
        cache_key = player_name.lower()
        if cache_key in self.player_cache:
            if time.time() - self.cache_ts.get(cache_key, 0) < self.cache_ttl:
                return self.player_cache[cache_key]
        
        try:
            # Search for player
            r = requests.get(
                "https://api.balldontlie.io/v1/players",
                params={"search": player_name},
                headers={"Authorization": ""},  # Free tier
                timeout=5
            )
            
            if r.status_code == 401:
                # balldontlie requires API key now — try nba_api approach
                return self._get_stats_fallback(player_name, last_n_games)
            
            players = r.json().get("data", [])
            if not players:
                return self._get_stats_fallback(player_name, last_n_games)
            
            player = players[0]
            player_id = player["id"]
            
            # Get recent game stats
            season = datetime.now().year if datetime.now().month > 9 else datetime.now().year - 1
            r2 = requests.get(
                "https://api.balldontlie.io/v1/stats",
                params={
                    "player_ids[]": player_id,
                    "seasons[]": season,
                    "per_page": last_n_games,
                },
                timeout=5
            )
            
            games = r2.json().get("data", [])
            if len(games) < 3:
                return None
            
            import numpy as np
            pts = [g["pts"] for g in games if g["pts"] is not None]
            reb = [g["reb"] for g in games if g["reb"] is not None]
            ast = [g["ast"] for g in games if g["ast"] is not None]
            
            stats = PlayerStats(
                name=player_name,
                team=player.get("team", {}).get("abbreviation", ""),
                games_played=len(pts),
                pts_avg=float(np.mean(pts)) if pts else 0,
                reb_avg=float(np.mean(reb)) if reb else 0,
                ast_avg=float(np.mean(ast)) if ast else 0,
                pts_std=float(np.std(pts)) if pts else 0,
                reb_std=float(np.std(reb)) if reb else 0,
                ast_std=float(np.std(ast)) if ast else 0,
            )
            
            self.player_cache[cache_key] = stats
            self.cache_ts[cache_key] = time.time()
            return stats
            
        except Exception as e:
            return self._get_stats_fallback(player_name, last_n_games)
    
    def _get_stats_fallback(self, player_name: str, last_n: int = 10) -> Optional[PlayerStats]:
        """
        Fallback: use nba.com stats API (no auth needed but rate limited).
        """
        try:
            # NBA.com stats API — search all players
            headers = {
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.nba.com/",
                "Accept": "application/json",
            }
            
            # Use the free stats.nba.com commonallplayers endpoint
            r = requests.get(
                "https://stats.nba.com/stats/commonallplayers",
                params={
                    "LeagueID": "00",
                    "Season": "2025-26",
                    "IsOnlyCurrentSeason": 1,
                },
                headers=headers,
                timeout=10
            )
            
            if r.status_code != 200:
                return None
            
            # This is complex — for now return None and log
            # Full NBA stats integration is a separate task
            return None
            
        except:
            return None
    
    def _norm_cdf(self, z: float) -> float:
        """Approximate standard normal CDF."""
        import math
        return 0.5 * (1 + math.erf(z / math.sqrt(2)))
    
    def _calc_confidence(self, n_games: int, std: float, distance_from_line: float) -> float:
        """
        Calculate confidence in our estimate.
        Higher with more games, lower std, larger distance from line.
        """
        # Sample size factor
        if n_games >= 20:
            size_conf = 0.8
        elif n_games >= 10:
            size_conf = 0.6
        elif n_games >= 5:
            size_conf = 0.4
        else:
            size_conf = 0.2
        
        # Distance from line factor (further = more confident)
        if std > 0:
            z = abs(distance_from_line) / std
            if z > 1.5:
                dist_conf = 0.8
            elif z > 0.8:
                dist_conf = 0.6
            elif z > 0.3:
                dist_conf = 0.4
            else:
                dist_conf = 0.2
        else:
            dist_conf = 0.2
        
        return (size_conf + dist_conf) / 2
