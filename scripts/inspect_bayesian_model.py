import os
import sys
import json
import pandas as pd
import clickhouse_connect
from dotenv import load_dotenv

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env file if it exists
load_dotenv()

# Override Docker-internal hostnames with localhost for local script execution
# if the environment variables aren't already explicitly set to something else.
if os.getenv("NEO4J_URI") == "bolt://neo4j:7687" or not os.getenv("NEO4J_URI"):
    os.environ["NEO4J_URI"] = "bolt://localhost:7687"
if os.getenv("CLICKHOUSE_HOST") == "clickhouse" or not os.getenv("CLICKHOUSE_HOST"):
    os.environ["CLICKHOUSE_HOST"] = "localhost"

from ai_engine.causal.graph_engine import CausalGraphEngine
from ai_engine.causal.bayesian_engine import BayesianCausalEngine

# Configuration from environment (now with defaults handled above)
CH_HOST = os.environ.get("CLICKHOUSE_HOST", "localhost")
CH_PORT = int(os.environ.get("CLICKHOUSE_PORT", "8123"))
CH_USER = os.environ.get("CLICKHOUSE_USER", "default")
CH_PASS = os.environ.get("CLICKHOUSE_PASSWORD", "causaliq123")
CH_DB   = os.environ.get("CLICKHOUSE_DB", "causaliq")

def inspect_model():
    print("--- CausalIQ Bayesian Model Inspector ---")
    
    # 1. Connect to ClickHouse
    try:
        client = clickhouse_connect.get_client(host=CH_HOST, port=CH_PORT, username=CH_USER, password=CH_PASS)
        print(f"Connected to ClickHouse at {CH_HOST}:{CH_PORT}")
    except Exception as e:
        print(f"Failed to connect to ClickHouse: {e}")
        return

    # 2. Build Engines
    causal_engine = CausalGraphEngine()
    bayesian_engine = BayesianCausalEngine()

    # 3. Get Topology and Build Network
    try:
        edges = causal_engine.get_all_dependencies()
        bayesian_engine.build_network(edges)
        print(f"Network built with {len(edges)} edges.")
    except Exception as e:
        print(f"Failed to build network: {e}")
        return

    # 4. Fetch Training Data from ClickHouse
    try:
        query = f"SELECT service, detected_at FROM {CH_DB}.anomaly_events ORDER BY detected_at DESC LIMIT 5000"
        rows = client.query(query).result_rows
        
        if not rows:
            print("No anomaly history found in ClickHouse. Model has no data to train on.")
            return

        # Transform rows into a binary matrix (Service vs TimeBucket)
        # We bucket by minute to see concurrent anomalies
        df_raw = pd.DataFrame(rows, columns=['service', 'detected_at'])
        df_raw['ts_bucket'] = df_raw['detected_at'].dt.floor('1min')
        
        services = sorted(df_raw['service'].unique())
        buckets = sorted(df_raw['ts_bucket'].unique())
        
        history_df = pd.DataFrame(0, index=buckets, columns=services)
        for _, row in df_raw.iterrows():
            history_df.at[row['ts_bucket'], row['service']] = 1
            
        print(f"Retrieved {len(rows)} events across {len(buckets)} time buckets.")
        print(f"Training matrix shape: {history_df.shape}")
        
        # 5. Train the Engine
        bayesian_engine.train_from_history(history_df)
        
        # 6. Inspect CPTs (Conditional Probability Tables)
        print("\n--- Learned Conditional Probabilities (CPTs) ---")
        for node in bayesian_engine.model.nodes():
            cpd = bayesian_engine.model.get_cpds(node)
            print(f"\n[Service: {node}]")
            print(cpd)
            
    except Exception as e:
        print(f"Inspection failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    inspect_model()
