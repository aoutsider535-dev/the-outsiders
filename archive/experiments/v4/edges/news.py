"""
Edge Source: News & Event Processing
=======================================
Processes breaking news and scheduled events to estimate probabilities 
before the market fully prices them in.

Data sources (free):
- Google News RSS (no API key)
- Reddit API (limited free access)
- Government data calendars (BLS, Fed, etc.)
- Web scraping for specific events

Edge thesis: AI can process and synthesize multiple news sources faster
than manual Polymarket traders. When breaking news hits, there's a 
5-30 minute window before markets fully adjust.
"""

import requests
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional, Dict, List
from dataclasses import dataclass


@dataclass
class NewsSignal:
    """A news-derived probability signal."""
    headline: str
    source: str
    relevance_score: float    # 0-1 how relevant to the market question
    sentiment: float          # -1 (negative) to +1 (positive) for the YES outcome
    confidence: float         # How confident in this signal
    timestamp: str


class NewsEdge:
    """Find edges by processing news faster than the market."""
    
    def __init__(self):
        self.headlines_cache: Dict[str, List[dict]] = {}
        self.cache_ts: Dict[str, float] = {}
        self.cache_ttl = 300  # 5 min cache for news
    
    def analyze_market(self, question: str, outcomes: List[str],
                       prices: List[float], slug: str = "") -> Optional[dict]:
        """
        Search for relevant news and estimate probability impact.
        """
        # Extract key entities from the market question
        entities = self._extract_entities(question)
        if not entities:
            return None
        
        # Fetch recent news for these entities
        signals = []
        for entity in entities[:3]:  # Max 3 entity searches
            headlines = self._search_news(entity)
            for h in headlines:
                signal = self._evaluate_headline(h, question, outcomes)
                if signal and signal.relevance_score > 0.3:
                    signals.append(signal)
        
        if not signals:
            return None
        
        # Aggregate signals into probability estimate
        return self._aggregate_signals(signals, question, outcomes, prices)
    
    def _extract_entities(self, question: str) -> List[str]:
        """Extract key searchable entities from a market question."""
        # Remove common prediction market phrasing
        q = question
        for phrase in ["Will ", "Will the ", "Is ", "Does ", "Has ", "Are ",
                      " before ", " after ", " by ", " in ", " on ", " at ",
                      "?", ":", "-"]:
            q = q.replace(phrase, " ")
        
        # Extract multi-word entities (capitalized sequences)
        entities = []
        
        # Named entities (2+ word capitalized phrases)
        caps = re.findall(r'(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)', question)
        entities.extend(caps)
        
        # Single important words (capitalized, not common)
        common = {"Will", "The", "This", "That", "What", "When", "Where", "How",
                  "Yes", "No", "Before", "After", "More", "Less", "Over", "Under"}
        singles = re.findall(r'\b([A-Z][a-z]{2,})\b', question)
        entities.extend([s for s in singles if s not in common])
        
        # Specific patterns
        # "$X price" → search for the asset
        if re.search(r'\$\d', question):
            entities.append(question.split("$")[0].strip().split()[-1] if "$" in question else "")
        
        # Deduplicate while preserving order
        seen = set()
        unique = []
        for e in entities:
            if e.lower() not in seen and len(e) > 2:
                seen.add(e.lower())
                unique.append(e)
        
        return unique[:5]
    
    def _search_news(self, query: str) -> List[dict]:
        """Search for recent news using Google News RSS (free, no API key)."""
        cache_key = query.lower()
        if cache_key in self.headlines_cache:
            if time.time() - self.cache_ts.get(cache_key, 0) < self.cache_ttl:
                return self.headlines_cache[cache_key]
        
        headlines = []
        try:
            # Google News RSS
            url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=en-US&gl=US&ceid=US:en"
            r = requests.get(url, timeout=5)
            
            if r.status_code == 200:
                root = ET.fromstring(r.content)
                for item in root.findall(".//item")[:10]:
                    title = item.find("title")
                    pub_date = item.find("pubDate")
                    source = item.find("source")
                    
                    if title is not None:
                        headlines.append({
                            "title": title.text or "",
                            "source": source.text if source is not None else "Google News",
                            "date": pub_date.text if pub_date is not None else "",
                        })
        except:
            pass
        
        self.headlines_cache[cache_key] = headlines
        self.cache_ts[cache_key] = time.time()
        
        return headlines
    
    def _evaluate_headline(self, headline: dict, question: str,
                           outcomes: List[str]) -> Optional[NewsSignal]:
        """
        Evaluate a single headline's relevance and sentiment to a market.
        
        Uses keyword matching for now. Future: LLM-based evaluation.
        """
        title = headline.get("title", "").lower()
        q_lower = question.lower()
        
        # Relevance: how many key words from the question appear in the headline?
        q_words = set(re.findall(r'\b\w{4,}\b', q_lower))
        h_words = set(re.findall(r'\b\w{4,}\b', title))
        overlap = q_words & h_words
        
        if not overlap:
            return None
        
        relevance = len(overlap) / max(len(q_words), 1)
        
        # Sentiment: positive/negative keywords
        positive = ["approve", "pass", "win", "sign", "agree", "confirm", 
                    "support", "success", "launch", "increase", "rise",
                    "beat", "exceed", "strong", "surge", "rally"]
        negative = ["reject", "fail", "lose", "block", "deny", "oppose",
                    "cancel", "delay", "decrease", "fall", "miss", "weak",
                    "crash", "drop", "decline"]
        
        pos_count = sum(1 for w in positive if w in title)
        neg_count = sum(1 for w in negative if w in title)
        
        if pos_count + neg_count == 0:
            sentiment = 0.0
        else:
            sentiment = (pos_count - neg_count) / (pos_count + neg_count)
        
        # Check if headline is recent (within last 24 hours)
        confidence = 0.3 + (relevance * 0.4)  # Base 0.3, up to 0.7
        
        return NewsSignal(
            headline=headline.get("title", ""),
            source=headline.get("source", ""),
            relevance_score=relevance,
            sentiment=sentiment,
            confidence=confidence,
            timestamp=headline.get("date", ""),
        )
    
    def _aggregate_signals(self, signals: List[NewsSignal], question: str,
                           outcomes: List[str], prices: List[float]) -> Optional[dict]:
        """Aggregate multiple news signals into a probability estimate."""
        if not signals:
            return None
        
        # Weight by relevance × confidence
        weighted_sentiment = 0
        total_weight = 0
        
        for s in signals:
            weight = s.relevance_score * s.confidence
            weighted_sentiment += s.sentiment * weight
            total_weight += weight
        
        if total_weight == 0:
            return None
        
        avg_sentiment = weighted_sentiment / total_weight
        
        # Convert sentiment to probability adjustment
        # Positive sentiment → higher YES probability
        # Scale: sentiment of 1.0 → +15% to YES probability
        adjustment = avg_sentiment * 0.15
        
        # Current market midpoint
        market_yes = prices[0]
        our_yes = max(0.05, min(0.95, market_yes + adjustment))
        
        edge = our_yes - market_yes
        
        if abs(edge) < 0.03:
            return None
        
        outcome_idx = 0 if edge > 0 else 1
        our_prob = our_yes if edge > 0 else (1 - our_yes)
        
        avg_confidence = sum(s.confidence for s in signals) / len(signals)
        
        # Build thesis from top headlines
        top_headlines = sorted(signals, key=lambda s: s.relevance_score, reverse=True)[:3]
        thesis_parts = [f'"{s.headline[:60]}" ({s.source})' for s in top_headlines]
        
        return {
            "probability": our_prob,
            "confidence": min(avg_confidence, 0.7),  # Cap at 0.7 — news is noisy
            "edge": abs(edge),
            "ev": abs(edge) - 0.02,
            "outcome_idx": outcome_idx,
            "thesis": f"News sentiment {'positive' if avg_sentiment > 0 else 'negative'} "
                     f"({avg_sentiment:+.2f}) based on {len(signals)} headlines. "
                     f"Top: {'; '.join(thesis_parts[:2])}",
            "sources": ["google_news", "sentiment_analysis"],
        }
