import os
import logging
from langchain.agents import initialize_agent, Tool, AgentType
from langchain_community.llms import Ollama
from typing import List

# Setup Logging
logger = logging.getLogger("causaliq-agent")

class RCAAgent:
    """
    Agentic LLM that uses ClickHouse and Neo4j tools to verify 
    RCA hypotheses in real-time.
    """
    def __init__(self, ch_client, neo4j_engine):
        self.llm = Ollama(model="llama3.1", base_url=os.getenv("OLLAMA_URL", "http://ollama:11434"))
        self.ch = ch_client
        self.graph = neo4j_engine
        
        # Tools definitions
        self.tools = [
            Tool(
                name="CheckServiceMetrics",
                func=self._get_metrics,
                description="Checks real-time latency and error rates for a specific service in ClickHouse."
            ),
            Tool(
                name="ExploreDependencies",
                func=self._get_deps,
                description="Finds upstream/downstream dependencies of a service in the Neo4j Causal Graph."
            )
        ]
        
        self.agent = initialize_agent(
            self.tools, 
            self.llm, 
            agent=AgentType.ZERO_SHOT_REACT_DESCRIPTION,
            verbose=True,
            handle_parsing_errors=True
        )

    def _get_metrics(self, service_name: str):
        try:
            query = f"SELECT avg(avg_latency_ms), avg(error_rate) FROM causaliq.service_metrics WHERE service='{service_name}' AND ts > now() - INTERVAL 5 MINUTE"
            res = self.ch.query(query).result_rows
            if not res or res[0][0] is None: return f"No recent metrics for {service_name}"
            return f"Service {service_name} -> Latency: {round(res[0][0], 2)}ms, Error Rate: {round(res[0][1]*100, 2)}%"
        except Exception as e:
            return f"Error querying metrics: {str(e)}"

    def _get_deps(self, service_name: str):
        try:
            upstream = self.graph.trace_upstream(service_name)
            downstream = self.graph.trace_downstream(service_name)
            return f"Topological Context for {service_name}: Upstream callers: {upstream}. Downstream targets: {downstream}."
        except Exception as e:
            return f"Error querying graph: {str(e)}"

    def run_analysis(self, root_candidate: str, active_anomalies: list):
        prompt = (
            f"SYSTEM: You are the CausalIQ Senior RCA Agent.\n"
            f"INCIDENT: Multiple anomalies detected. Top candidate for root cause is '{root_candidate}'.\n"
            f"ANOMALIES OBSERVED: {active_anomalies}\n\n"
            f"TASK: Use your tools to verify if '{root_candidate}' is truly the root cause or just another symptom. "
            f"Check the metrics of '{root_candidate}' and see if its downstream services are also failing. "
            f"Provide a definitive markdown report."
        )
        return self.agent.run(prompt)
