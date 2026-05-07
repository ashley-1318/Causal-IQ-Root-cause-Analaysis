import os
import logging
import hashlib
import json
from langchain.agents import initialize_agent, Tool, AgentType
from langchain_community.chat_models import ChatOllama
from langchain_groq import ChatGroq
from typing import List, Dict

# Setup Logging
logger = logging.getLogger("causaliq-agent")

class RCAAgent:
    """
    Agentic LLM that uses ClickHouse and Neo4j tools to verify 
    RCA hypotheses in real-time.
    """
    def __init__(self, ch_client, neo4j_engine):
        # Configuration for LLM Provider
        provider = os.getenv("LLM_PROVIDER", "ollama").lower()
        
        if provider == "groq":
            model_name = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
            groq_api_key = os.getenv("GROQ_API_KEY", "")
            if not groq_api_key:
                logger.warning("GROQ_API_KEY is missing! Falling back to Ollama.")
                provider = "ollama"
            else:
                self.llm = ChatGroq(model_name=model_name, api_key=groq_api_key)
        
        if provider == "ollama":
            model_name = os.getenv("OLLAMA_MODEL", "llama3")
            ollama_url = os.getenv("OLLAMA_URL", "http://ollama:11434")
            self.llm = ChatOllama(model=model_name, base_url=ollama_url)
            logger.info(f"Using Ollama with model {model_name} at {ollama_url}")

        self.ch = ch_client
        self.graph = neo4j_engine
        
        # Simple in-memory cache for analysis results
        self._cache: Dict[str, str] = {}
        
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
            handle_parsing_errors=True,
            max_iterations=3,
            early_stopping_method="generate"
        )

    def _get_metrics(self, service_name: str):
        try:
            import re
            safe_name = re.sub(r"[^A-Za-z0-9_-]", "", service_name.strip())[:64]
            query = (
                "SELECT avg(avg_latency_ms), avg(error_rate) "
                "FROM causaliq.service_metrics "
                "WHERE service = {p_service:String} AND ts > now() - INTERVAL 5 MINUTE"
            )
            res = self.ch.query(query, parameters={"p_service": safe_name}).result_rows
            if not res or res[0][0] is None:
                return f"No recent metrics for {safe_name}"
            return f"Service {safe_name} -> Latency: {round(res[0][0], 2)}ms, Error Rate: {round(res[0][1]*100, 2)}%"
        except Exception as e:
            return f"Error querying metrics: {str(e)}"

    def _get_deps(self, service_name: str):
        try:
            upstream = self.graph.trace_upstream(service_name)
            downstream = self.graph.trace_downstream(service_name)
            return f"Topological Context for {service_name}: Upstream callers: {upstream}. Downstream targets: {downstream}."
        except Exception as e:
            return f"Error querying graph: {str(e)}"

    def _get_cache_key(self, root_candidate: str, active_anomalies: list):
        # Create a stable key based on root cause and anomaly set
        anomaly_ids = sorted([a.get('service', '') for a in active_anomalies])
        data = f"{root_candidate}:{','.join(anomaly_ids)}"
        return hashlib.md5(data.encode()).hexdigest()

    def run_analysis(self, root_candidate: str, active_anomalies: list):
        cache_key = self._get_cache_key(root_candidate, active_anomalies)
        if cache_key in self._cache:
            logger.info(f"Cache hit for RCA analysis of {root_candidate}")
            return self._cache[cache_key]

        prompt = (
            f"SYSTEM: You are the CausalIQ Senior RCA Agent.\n"
            f"INCIDENT: Multiple anomalies detected. Top candidate for root cause is '{root_candidate}'.\n"
            f"ANOMALIES OBSERVED: {active_anomalies}\n\n"
            f"TASK: Use the information provided to explain why '{root_candidate}' is the root cause. "
            f"Provide a definitive markdown report."
        )
        try:
            response = self.llm.invoke(prompt)
            # Handle both string and Message responses from LangChain
            result = response.content if hasattr(response, 'content') else str(response)
            self._cache[cache_key] = result
            return result
        except Exception as e:
            return f"Agent Generation Error: {str(e)}"

