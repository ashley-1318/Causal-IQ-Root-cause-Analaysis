"""
CausalIQ Kafka Topic Provisioner
Creates required Kafka/Redpanda topics on startup.
"""
import os
import time
import logging
from confluent_kafka.admin import AdminClient, NewTopic

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("kafka-setup")

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "redpanda:9092")

TOPICS = [
    NewTopic("otel-logs",    num_partitions=3, replication_factor=1),
    NewTopic("otel-metrics", num_partitions=3, replication_factor=1),
    NewTopic("otel-traces",  num_partitions=3, replication_factor=1),
    NewTopic("anomalies",    num_partitions=1, replication_factor=1),
    NewTopic("rca-results",  num_partitions=1, replication_factor=1),
]

def provision_topics(retries: int = 10, delay: int = 5):
    for attempt in range(retries):
        try:
            admin = AdminClient({"bootstrap.servers": KAFKA_BOOTSTRAP})
            fs = admin.create_topics(TOPICS, validate_only=False)
            for topic, f in fs.items():
                try:
                    f.result()
                    logger.info("Topic created: %s", topic)
                except Exception as e:
                    if "TOPIC_ALREADY_EXISTS" in str(e):
                        logger.info("Topic already exists: %s", topic)
                    else:
                        raise
            return
        except Exception as exc:
            logger.warning("Attempt %d/%d failed: %s", attempt + 1, retries, exc)
            time.sleep(delay)
    raise RuntimeError("Failed to provision Kafka topics after retries")

if __name__ == "__main__":
    provision_topics()
    logger.info("All topics ready.")
