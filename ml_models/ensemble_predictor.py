import numpy as np
from typing import Dict, Tuple, List
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.calibration import CalibratedClassifierCV
import logging

logger = logging.getLogger(__name__)

class EnsemblePredictor:
    """
    Ensemble ML model for Polymarket event prediction.
    
    Trained on:
    - Historical Polymarket prices + resolutions
    - Technical indicators (RSI, momentum, volatility)
    - News sentiment (when available)
    - On-chain data
    
    Output: Probability that event resolves YES
    
    Profitability: Find markets where model prob ≠ market prob > 3%
    """
    
    def __init__(self):
        self.models = []
        self.scaler = StandardScaler()
        self.feature_importance = {}
        self.calibration_data = []  # For probability calibration
        
    def train(self, X: np.ndarray, y: np.ndarray) -> Dict:
        """
        Train ensemble of 5 gradient boosting models.
        
        X: Features (technical indicators, sentiment, on-chain data)
        y: Labels (1 = resolved YES, 0 = resolved NO)
        """
        
        # Normalize features
        X_scaled = self.scaler.fit_transform(X)
        
        # Train 5 models with different random seeds for diversity
        for seed in [42, 123, 456, 789, 999]:
            model = GradientBoostingClassifier(
                n_estimators=100,
                learning_rate=0.05,
                max_depth=5,
                min_samples_split=20,
                subsample=0.8,
                random_state=seed
            )
            
            model.fit(X_scaled, y)
            
            # Calibrate probabilities (important for betting)
            cal_model = CalibratedClassifierCV(
                model,
                method='sigmoid',
                cv=5
            )
            cal_model.fit(X_scaled, y)
            
            self.models.append(cal_model)
        
        # Track feature importance
        self.feature_importance = {
            f'feature_{i}': np.mean([
                m.base_estimator_.feature_importances_[i]
                for m in self.models
            ])
            for i in range(X.shape[1])
        }
        
        logger.info(f"Ensemble trained: {len(self.models)} models")
        
        return {
            'models_trained': len(self.models),
            'feature_importance': self.feature_importance
        }
    
    def predict(self, X: np.ndarray) -> Tuple[float, float]:
        """
        Predict probability and confidence for event.
        
        Returns:
        - prob_yes: Probability market resolves YES (0.0 - 1.0)
        - confidence: Confidence in prediction (0.0 - 1.0)
        """
        
        if not self.models:
            return 0.5, 0.0  # No model
        
        X_scaled = self.scaler.transform(X.reshape(1, -1))
        
        # Get predictions from all 5 models
        predictions = []
        for model in self.models:
            pred = model.predict_proba(X_scaled)[0][1]  # Probability of YES
            predictions.append(pred)
        
        # Ensemble: average probability
        prob_yes = np.mean(predictions)
        
        # Confidence: low std = high confidence
        std = np.std(predictions)
        confidence = 1.0 - (std / 0.5)  # Normalize std to 0-1
        confidence = np.clip(confidence, 0.0, 1.0)
        
        return float(prob_yes), float(confidence)
    
    def find_mispriced_markets(self,
                              markets: List[Dict],
                              min_edge: float = 0.03) -> List[Dict]:
        """
        Find markets where model probability differs from market by >min_edge.
        
        Example: Model says 70% YES, market shows 45% YES → 25% edge
        """
        
        opportunities = []
        
        for market in markets:
            condition_id = market.get('condition_id')
            question = market.get('question')
            yes_price = float(market.get('yes_price', 0.5))
            
            # Get features for this market
            features = self._extract_features(market)
            if features is None:
                continue
            
            # Predict
            model_prob, confidence = self.predict(features)
            
            # Calculate edge
            market_prob = yes_price
            edge = abs(model_prob - market_prob)
            
            if edge > min_edge and confidence > 0.3:
                action = 'BUY_YES' if model_prob > market_prob else 'BUY_NO'
                expected_return = edge  # Simplified
                
                opp = {
                    'type': 'ml_mispricing',
                    'market_id': condition_id,
                    'question': question,
                    'model_prob': model_prob,
                    'market_prob': market_prob,
                    'edge': edge,
                    'confidence': confidence,
                    'action': action,
                    'expected_return': expected_return,
                    'expected_return_pct': expected_return * 100
                }
                opportunities.append(opp)
        
        return sorted(opportunities, key=lambda x: x['edge'], reverse=True)
    
    def _extract_features(self, market: Dict) -> np.ndarray:
        """
        Extract feature vector for a market.
        
        Features (example):
        - Probability (0-1)
        - Volatility (recent price moves)
        - Volume (trading activity)
        - Time to resolution (days)
        - Liquidity (depth)
        - Sentiment (if available)
        """
        
        try:
            features = np.array([
                float(market.get('yes_price', 0.5)),  # Current YES price
                float(market.get('total_liquidity', 100)) / 1000,  # Liquidity (normalized)
                1.0 if 'crypto' in market.get('question', '').lower() else 0.0,  # Is crypto market
                float(market.get('yes_liquidity', 50)) / float(market.get('total_liquidity', 100) + 0.001),  # Liquidity imbalance
                0.5,  # Placeholder: sentiment (would be real in production)
            ])
            
            return features
        except:
            return None