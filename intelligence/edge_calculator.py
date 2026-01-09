"""
Edge Detection System
Calculates true odds vs market odds to identify profitable opportunities
"""
from typing import Dict, Optional
import logging

logger = logging.getLogger(__name__)

class EdgeCalculator:
    """Multi-signal edge detection"""
    
    def __init__(self):
        self.min_edge_threshold = 0.15  # 15% minimum edge
    
    def calculate_edge(self, market: Dict, signals: Dict) -> Dict:
        """
        Calculate edge for a market
        
        Args:
            market: Market data from Polymarket
            signals: {
                "news_sentiment": -1.0 to 1.0,
                "price_signal": 0.0 to 1.0 (probability),
                "confidence": 0.0 to 1.0
            }
        
        Returns:
            {
                "edge": float,
                "true_odds": float,
                "market_odds": float,
                "signal": "BUY_YES" | "BUY_NO" | "SKIP",
                "confidence": float
            }
        """
        market_yes_odds = market.get("yes_price", 0.5)
        market_no_odds = market.get("no_price", 0.5)
        
        # Extract signals
        news_sentiment = signals.get("news_sentiment", 0.0)
        price_signal = signals.get("price_signal", 0.5)
        signal_confidence = signals.get("confidence", 0.5)
        
        # Calculate true probability
        # Weight: 40% news, 40% price, 20% baseline
        true_probability = (
            (0.5 + news_sentiment * 0.5) * 0.3 +  # News contribution
            price_signal * 0.5 +                   # Price contribution
            0.5 * 0.2                              # Baseline
        )
        
        # Clamp to valid range
        true_probability = max(0.01, min(0.99, true_probability))
        
        # Calculate edges for both sides
        edge_yes = true_probability - market_yes_odds
        edge_no = (1 - true_probability) - market_no_odds
        
        # Determine best signal
        if edge_yes > self.min_edge_threshold and edge_yes > edge_no:
            return {
                "edge": edge_yes,
                "true_odds": true_probability,
                "market_odds": market_yes_odds,
                "signal": "BUY_YES",
                "confidence": signal_confidence,
                "side": "YES"
            }
        elif edge_no > self.min_edge_threshold:
            return {
                "edge": edge_no,
                "true_odds": 1 - true_probability,
                "market_odds": market_no_odds,
                "signal": "BUY_NO",
                "confidence": signal_confidence,
                "side": "NO"
            }
        else:
            return {
                "edge": max(edge_yes, edge_no),
                "true_odds": true_probability,
                "market_odds": market_yes_odds,
                "signal": "SKIP",
                "confidence": signal_confidence,
                "side": None
            }
    
    def check_price_threshold_arb(self, market: Dict, current_price: float, threshold: float) -> Optional[Dict]:
        """
        Check if outcome already decided by price threshold
        
        Example: Market "BTC above $95K today?"
                 BTC currently at $96K
                 Outcome = YES (guaranteed)
        """
        question = market.get("question", "").lower()
        
        # Extract threshold from question (simple parsing)
        import re
        price_match = re.search(r'\$(\d+(?:,\d+)?(?:k)?)', question)
        
        if not price_match:
            return None
        
        threshold_str = price_match.group(1).replace(',', '').replace('k', '000').replace('K', '000')
        try:
            threshold_price = float(threshold_str)
        except:
            return None
        
        # Determine if outcome decided
        if "above" in question:
            if current_price > threshold_price:
                # YES is guaranteed
                market_yes_odds = market.get("yes_price", 0.5)
                if market_yes_odds < 0.95:  # Underpriced
                    return {
                        "edge": 0.95 - market_yes_odds,
                        "true_odds": 0.98,
                        "market_odds": market_yes_odds,
                        "signal": "BUY_YES",
                        "confidence": 0.98,
                        "side": "YES",
                        "reason": "THRESHOLD_CROSSED"
                    }
        
        elif "below" in question:
            if current_price < threshold_price:
                # YES is guaranteed
                market_yes_odds = market.get("yes_price", 0.5)
                if market_yes_odds < 0.95:
                    return {
                        "edge": 0.95 - market_yes_odds,
                        "true_odds": 0.98,
                        "market_odds": market_yes_odds,
                        "signal": "BUY_YES",
                        "confidence": 0.98,
                        "side": "YES",
                        "reason": "THRESHOLD_CROSSED"
                    }
        
        return None