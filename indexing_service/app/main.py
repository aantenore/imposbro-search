import os
import typesense
import time
import requests

def get_typesense_client(cluster_info):
    return typesense.Client({
        'nodes': [{'host': cluster_info['host'], 'port': cluster_info['port'], 'protocol': 'http'}],
        'api_key': cluster_info['api_key'],
        'connection_timeout_seconds': 5
    })

if __name__ == "__main__":
    from consumer import run_consumer
    
    QUERY_API_URL = os.environ.get("INTERNAL_QUERY_API_URL", "http://query_api:8000")
    
    federation_clients = {}
    
    # Continuously try to fetch cluster config from Query API
    while True:
        try:
            print(f"⏳ Indexer attempting to fetch cluster config from {QUERY_API_URL}/admin/federation/clusters...")
            cluster_response = requests.get(f"{QUERY_API_URL}/admin/federation/clusters", timeout=5)
            cluster_response.raise_for_status()
            cluster_data = cluster_response.json()
            
            if not cluster_data:
                print("🔥 Query API returned no clusters. Retrying in 10 seconds...")
                time.sleep(10)
                continue

            # Create clients for any new clusters
            for cluster_name, cluster_info in cluster_data.items():
                if cluster_name not in federation_clients:
                    federation_clients[cluster_name] = get_typesense_client(cluster_info)
                    print(f"✅ Indexer initialized Typesense client for cluster: {cluster_name}")
            
            # We have at least one client, break the loop and start the consumer
            break

        except requests.exceptions.RequestException as e:
            print(f"🔥 Indexer failed to connect to Query API: {e}. Retrying in 10 seconds.")
            time.sleep(10)

    print(f"🚀 Starting Indexing Service with {len(federation_clients)} federated client(s)...")
    run_consumer(federation_clients)
