import os
import json
from kafka import KafkaConsumer
import time

def run_consumer(typesense_clients):
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
            enriched_message = message.value
            target_cluster = enriched_message.get("target_cluster")
            collection_name = enriched_message.get("collection")
            document = enriched_message.get("document")

            if not all([target_cluster, collection_name, document]):
                print(f"⚠️ Invalid message format received: {enriched_message}")
                continue

            client = typesense_clients.get(target_cluster)
            if not client:
                print(f"⚠️ No client found for target cluster '{target_cluster}'. Document might be lost. Skipping.")
                continue

            client.collections[collection_name].documents.upsert(document)
            print(f"📄 Indexed document {document.get('id')} into '{collection_name}' on cluster '{target_cluster}'")
        except Exception as e:
            print(f"🔥 Error indexing document: {e}")
