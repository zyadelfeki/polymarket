from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from textblob import TextBlob
from typing import Dict, List
import logging

logger = logging.getLogger(__name__)

class SentimentScorer:
    def __init__(self):
        self.vader = SentimentIntensityAnalyzer()
        self.crypto_lexicon = {
            "moon": 3.0, "bullish": 2.5, "pump": 2.0, "surge": 2.0, "breakout": 2.5,
            "rally": 2.0, "adoption": 1.5, "institutional": 1.5, "accumulation": 1.5,
            "crash": -3.0, "dump": -2.5, "bearish": -2.5, "plunge": -2.5,
            "collapse": -3.0, "liquidation": -2.0, "selloff": -2.0,
            "regulation": -1.5, "ban": -2.5, "volatility": 0.0, "consolidation": 0.0
        }
        self.vader.lexicon.update(self.crypto_lexicon)
    
    def analyze_text(self, text: str) -> Dict[str, float]:
        if not text or len(text.strip()) == 0:
            return {"vader_compound": 0.0, "textblob_polarity": 0.0, "ensemble_score": 0.0, "confidence": 0.0}
        
        vader_scores = self.vader.polarity_scores(text)
        vader_compound = vader_scores["compound"]
        
        try:
            blob = TextBlob(text)
            textblob_polarity = blob.sentiment.polarity
        except:
            textblob_polarity = 0.0
        
        ensemble_score = (vader_compound * 0.6) + (textblob_polarity * 0.4)
        agreement = 1.0 - abs(vader_compound - textblob_polarity) / 2.0
        confidence = max(0.0, min(agreement, 1.0))
        
        return {
            "vader_compound": vader_compound,
            "textblob_polarity": textblob_polarity,
            "ensemble_score": ensemble_score,
            "confidence": confidence
        }
    
    def analyze_news_batch(self, news_items: List) -> Dict[str, float]:
        if not news_items:
            return {"overall_sentiment": 0.0, "confidence": 0.0, "bullish_count": 0, "bearish_count": 0, "neutral_count": 0}
        
        scores = []
        confidences = []
        bullish_count = 0
        bearish_count = 0
        neutral_count = 0
        
        for item in news_items:
            text = f"{item.title} {item.description}"
            result = self.analyze_text(text)
            score = result["ensemble_score"]
            scores.append(score)
            confidences.append(result["confidence"])
            
            if score > 0.1:
                bullish_count += 1
            elif score < -0.1:
                bearish_count += 1
            else:
                neutral_count += 1
        
        if hasattr(news_items[0], 'relevance_score'):
            weights = [item.relevance_score for item in news_items]
            total_weight = sum(weights)
            overall_sentiment = sum(s * w for s, w in zip(scores, weights)) / total_weight if total_weight > 0 else sum(scores) / len(scores)
        else:
            overall_sentiment = sum(scores) / len(scores)
        
        avg_confidence = sum(confidences) / len(confidences)
        
        return {
            "overall_sentiment": overall_sentiment,
            "confidence": avg_confidence,
            "bullish_count": bullish_count,
            "bearish_count": bearish_count,
            "neutral_count": neutral_count,
            "sample_size": len(news_items)
        }
    
    def score_to_label(self, score: float) -> str:
        if score >= 0.5:
            return "Very Bullish 🚀"
        elif score >= 0.1:
            return "Bullish 📈"
        elif score <= -0.5:
            return "Very Bearish 📉"
        elif score <= -0.1:
            return "Bearish 🐻"
        else:
            return "Neutral ➡️"