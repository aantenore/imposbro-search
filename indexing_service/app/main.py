import os
import typesense
import time
import requests


def get_typesense_client(cluster_info):
    """Initializes a Typesense client from a configuration dictionary."""
    return typesense.Client({
        'nodes': [{'host': cluster_info['host'], 'port': cluster_info['port'], 'protocol': 'http'}],
        'api_key': cluster_info['api_key'],
        'connection_timeout_seconds': 5
    })


if __name__ == "__main__":
    from consumer import run_consumer

    QUERY_API_URL = os.environ.get("INTERNAL_QUERY_API_URL", "http://query_api:8000")

    federation_clients = {}
    collection_routing_rules = {}

    # Continuously try to fetch the full configuration from the Query API
    while True:
        try:
            print(f"⏳ Indexer attempting to fetch cluster config from {QUERY_API_URL}/admin/federation/clusters...")
            cluster_response = requests.get(f"{QUERY_API_URL}/admin/federation/clusters", timeout=5)
            cluster_response.raise_for_status()
            cluster_data = cluster_response.json()

            print(f"⏳ Indexer attempting to fetch routing rules from {QUERY_API_URL}/admin/routing-map...")
            rules_response = requests.get(f"{QUERY_API_URL}/admin/routing-map", timeout=5)
            rules_response.raise_for_status()
            rules_data = rules_response.json()

            if not cluster_data:
                print("🔥 Query API returned no clusters. Retrying in 10 seconds...")
                time.sleep(10)
                continue

            # Create clients for any new clusters
            for cluster_name, cluster_info in cluster_data.items():
                if cluster_name not in federation_clients:
                    federation_clients[cluster_name] = get_typesense_client(cluster_info)
                    print(f"✅ Indexer initialized Typesense client for cluster: {cluster_name}")

            # Store the collection routing rules
            collection_routing_rules = rules_data.get("collections", {})
            print(f"✅ Indexer loaded {len(collection_routing_rules)} routing rule(s).")

            # We have at least one client, break the loop and start the consumer
            break

        except requests.exceptions.RequestException as e:
            print(f"🔥 Indexer failed to connect to Query API to get config: {e}. Retrying in 10 seconds.")
            time.sleep(10)

    print(f"🚀 Starting Indexing Service with {len(federation_clients)} federated client(s)...")
    # Pass both clients and routing rules to the consumer
    run_consumer(federation_clients, collection_routing_rules)
