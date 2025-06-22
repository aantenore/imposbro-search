import os
import json
from kafka import KafkaConsumer
import time


def determine_target_cluster(collection_name, document, routing_rules):
    """
    Determines the target cluster for a document based on the latest routing rules.
    This logic is a copy of the one in the query_api service.
    """
    rule = routing_rules.get(collection_name)
    target_cluster_name = "default"  # Fallback cluster

    # If a rule exists for this collection and has a routing field defined
    if rule and 'field' in rule:
        doc_value = document.get(rule["field"])
        # Find the first matching rule, otherwise use the rule's default
        target_cluster_name = next(
            (r["cluster"] for r in rule.get("rules", []) if r.get("value") == doc_value),
            rule.get("default_cluster", "default")
        )

    return target_cluster_name


def run_consumer(typesense_clients, collection_routing_rules):
    """
    Consumes messages from Kafka and indexes documents into Typesense
    after re-evaluating the routing rules.
    """
    KAFKA_BROKER_URL = os.environ.get("KAFKA_BROKER_URL")
    TOPIC_PREFIX = os.environ.get("KAFKA_TOPIC_PREFIX")

    while True:
        try:
            consumer = KafkaConsumer(
                bootstrap_servers=KAFKA_BROKER_URL,
                auto_offset_reset='earliest',
                group_id='imposbro_federated_indexing_group',
                value_deserializer=lambda m: json.loads(m.decode('utf-8'))
            )
            consumer.subscribe(pattern=f"^{TOPIC_PREFIX}_.*")
            print(f"✅ Kafka Consumer connected and subscribed to pattern '{TOPIC_PREFIX}_*'")
            break
        except Exception as e:
            print(f"🔥 Failed to connect to Kafka consumer: {e}. Retrying in 5 seconds...")
            time.sleep(5)

    print("👂 Listening for messages to index...")
    for message in consumer:
        try:
            # The message still contains the original target_cluster, but we will ignore it.
            enriched_message = message.value
            collection_name = enriched_message.get("collection")
            document = enriched_message.get("document")

            if not all([collection_name, document]):
                print(f"⚠️ Invalid message format received (missing collection or document): {enriched_message}")
                continue

            # --- REROUTING LOGIC ---
            # Determine the target cluster using the rules loaded by the indexer.
            target_cluster = determine_target_cluster(collection_name, document, collection_routing_rules)

            client = typesense_clients.get(target_cluster)
            if not client:
                print(
                    f"⚠️ No client found for re-routed target cluster '{target_cluster}'. Document might be lost. Skipping.")
                continue

            client.collections[collection_name].documents.upsert(document)
            print(
                f"📄 Indexed document {document.get('id')} into '{collection_name}' on cluster '{target_cluster}' (re-routed)")
        except Exception as e:
            print(f"🔥 Error indexing document: {e}")
