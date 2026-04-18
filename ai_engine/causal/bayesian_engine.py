import logging
import pandas as pd
import numpy as np
from pgmpy.models import BayesianNetwork
from pgmpy.inference import VariableElimination
from pgmpy.estimators import MaximumLikelihoodEstimator
from typing import List, Tuple, Dict, Optional

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("causaliq-bayesian-engine")

class BayesianCausalEngine:
    """
    Advanced Causal Inference Engine using Bayesian Networks to replace 
    heuristic depth-traversal for Root Cause Analysis.
    """
    
    def __init__(self):
        self.model: Optional[BayesianNetwork] = None
        self.inference: Optional[VariableElimination] = None
        self.is_trained: bool = False

    def build_network(self, edges: List[Tuple[str, str]]):
        """
        Builds the DAG (Directed Acyclic Graph) for the Bayesian Network.
        Edges should be a list of tuples: (SourceService, DependentService)
        """
        try:
            if not edges:
                logger.warning("No edges provided to build causal network.")
                return

            self.model = BayesianNetwork(edges)
            logger.info(f"Bayesian Network built with {len(self.model.nodes())} nodes.")
        except Exception as e:
            logger.error(f"Failed to build Bayesian Network: {str(e)}")
            raise

    def train_from_history(self, history_df: pd.DataFrame):
        """
        Estimates Conditional Probability Tables (CPTs) using Maximum 
        Likelihood Estimation from historical anomaly data.
        
        history_df: DataFrame where each column is a service 
                    and rows are binary anomaly states (0=Healthy, 1=Anomaly).
        """
        if self.model is None:
            raise ValueError("Network must be built before training. Call build_network() first.")
        
        try:
            logger.info("Starting Bayesian CPT training via MLE...")
            
            # Ensure all nodes in the model are present in the dataframe
            for node in self.model.nodes():
                if node not in history_df.columns:
                    logger.warning(f"Node {node} missing from training data. Filling with zeros.")
                    history_df[node] = 0
            
            self.model.fit(history_df, estimator=MaximumLikelihoodEstimator)
            self.inference = VariableElimination(self.model)
            self.is_trained = True
            logger.info("Bayesian Engine training complete.")
            
        except Exception as e:
            logger.error(f"Training failed: {str(e)}")
            self.is_trained = False

    def identify_root_cause(self, active_anomalies: Dict[str, int]) -> List[Dict]:
        """
        Calculates the probability that each service is the root cause
        given the currently observed anomalies.
        
        active_anomalies: {'auth-service': 1, 'order-service': 1}
        Returns: List of dicts sorted by root cause probability.
        """
        if not self.is_trained or self.inference is None:
            logger.warning("Engine not trained. Falling back to simple anomaly score.")
            return [{"service": s, "probability": 0.5} for s in active_anomalies.keys()]

        try:
            results = []
            # Optimization: Only evaluate nodes currently reporting anomalies or their direct parents
            target_nodes = set(active_anomalies.keys())
            for node in active_anomalies.keys():
                target_nodes.update(self.model.predecessors(node))

            for node in target_nodes:
                # Query: What is the probability this node is '1' (Active) given evidence?
                query_res = self.inference.query(variables=[node], evidence=active_anomalies)
                # query_res.values[1] is the prob of state '1'
                results.append({
                    "service": node,
                    "probability": round(query_res.values[1], 4)
                })

            return sorted(results, key=lambda x: x["probability"], reverse=True)
            
        except Exception as e:
            logger.error(f"Inference failed: {str(e)}")
            return []

# --- Unit Test Skeleton ---
def test_bayesian_engine():
    engine = BayesianCausalEngine()
    # Mock Edges: A -> B (A causes B)
    engine.build_network([("payment-service", "order-service")])
    
    # Mock History: payment-service often fails first
    data = pd.DataFrame({
        "payment-service": [0, 1, 1, 0, 1, 0],
        "order-service":   [0, 1, 1, 0, 0, 0]
    })
    engine.train_from_history(data)
    
    # Test Inference: order-service is down. Is payment-service the cause?
    result = engine.identify_root_cause({"order-service": 1})
    assert result[0]["service"] == "payment-service"
    logger.info("Unit test passed.")
