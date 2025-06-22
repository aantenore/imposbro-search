import typesense
import json
import time
import asyncio
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional
from prometheus_fastapi_instrumentator import Instrumentator
from kafka import KafkaProducer
from prometheus_client import Counter

from settings import settings

# --- Kafka Producer Setup ---
documents_ingested = Counter('documents_ingested_total', 'Total number of documents ingested.', ['collection'])
producer = None
def get_producer():
    global producer
    if producer is None:
        while True:
            try:
                producer = KafkaProducer(
                    bootstrap_servers=settings.KAFKA_BROKER_URL,
                    value_serializer=lambda v: json.dumps(v).encode('utf-8')
                )
                print("✅ Kafka Producer connected successfully.")
                break
            except Exception as e:
                print(f"🔥 Failed to connect to Kafka: {e}. Retrying in 5 seconds...")
                time.sleep(5)
    return producer

# --- In-Memory State for Federation ---
# In a real production system, this state should be stored in a shared database like Redis or ZooKeeper
federation_clusters_config: Dict[str, Dict] = {}
federation_clients: Dict[str, typesense.Client] = {}
collection_routing_rules: Dict[str, Dict] = {}

# --- Pydantic Models ---
class Cluster(BaseModel): name: str; host: str; port: int; api_key: str
class CollectionField(BaseModel): name: str; type: str; facet: bool = False
class CollectionSchema(BaseModel):
    name: str
    fields: List[CollectionField]
    default_sorting_field: Optional[str] = None
class RoutingRule(BaseModel):
    collection: str
    field: str
    rules: List[Dict[str, str]] 
    default_cluster: str = "default"

app = FastAPI(title="IMPOSBRO Federated Search & Admin API", version="2.1.0")
Instrumentator().instrument(app).add(documents_ingested).expose(app)

@app.on_event("startup")
async def startup_event():
    get_producer() # Initialize Kafka producer on startup
    
    # Register the default cluster from environment variables
    default_cluster_config = {"host": settings.TYPESENSE_HOST, "port": settings.TYPESENSE_PORT, "api_key": settings.TYPESENSE_API_KEY}
    default_client = typesense.Client({
        'nodes': [{'host': default_cluster_config['host'], 'port': default_cluster_config['port'], 'protocol': 'http'}],
        'api_key': default_cluster_config['api_key'],
        'connection_timeout_seconds': 5
    })
    federation_clients["default"] = default_client
    federation_clusters_config["default"] = default_cluster_config
    print("✅ Default cluster registered.")

def get_client_for_document(collection_name: str, document: Dict) -> (typesense.Client, str):
    rule = collection_routing_rules.get(collection_name)
    target_cluster_name = "default" 
    if rule:
        doc_value = document.get(rule["field"])
        # Find the first matching rule
        target_cluster_name = next((r["cluster"] for r in rule["rules"] if r["value"] == doc_value), rule.get("default_cluster", "default"))
    
    client = federation_clients.get(target_cluster_name)
    if not client:
        raise HTTPException(status_code=404, detail=f"Cluster '{target_cluster_name}' for routing not found.")
    return client, target_cluster_name

def get_clients_for_collection_search(collection_name: str) -> List[typesense.Client]:
    # For scatter-gather, find all clusters that might have data for this collection
    if collection_name in collection_routing_rules:
        rule = collection_routing_rules[collection_name]
        cluster_names = {r["cluster"] for r in rule["rules"]}
        cluster_names.add(rule.get("default_cluster", "default"))
        return [federation_clients[name] for name in cluster_names if name in federation_clients]
    else: # Fallback to default if no specific rule
        return [federation_clients["default"]] if "default" in federation_clients else []

@app.post("/ingest/{collection_name}")
def ingest_document(collection_name: str, document: Dict[str, Any]):
    doc_id = document.get("id")
    if not doc_id:
        raise HTTPException(status_code=400, detail="Document must have an 'id' field.")
    
    _, target_cluster_name = get_client_for_document(collection_name, document)
    
    enriched_message = {
        "target_cluster": target_cluster_name,
        "collection": collection_name,
        "document": document
    }
    
    topic_name = f"{settings.KAFKA_TOPIC_PREFIX}_{collection_name}"
    try:
        get_producer().send(topic_name, key=str(doc_id).encode('utf-8'), value=enriched_message)
        get_producer().flush()
        documents_ingested.labels(collection=collection_name).inc()
        print(f"✅ Sent document {doc_id} to topic {topic_name} for cluster {target_cluster_name}")
        return {"status": "ok", "document_id": doc_id, "routed_to": target_cluster_name}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Kafka producer error: {str(e)}")

@app.get("/search/{collection_name}")
async def search(
    request: Request,
    collection_name: str,
    q: str = Query(..., min_length=1),
    query_by: str = Query(...),
    filter_by: Optional[str] = None,
    sort_by: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=1, le=250)
):
    clients = get_clients_for_collection_search(collection_name)
    if not clients:
        raise HTTPException(status_code=404, detail=f"Collection '{collection_name}' not found on any registered cluster.")

    search_params = {k: v for k, v in {
        'q': q, 'query_by': query_by, 'filter_by': filter_by, 'sort_by': sort_by,
        'page': 1, 'per_page': per_page * page, # Fetch all docs up to the current page for re-ranking
    }.items() if v is not None}

    async def search_in_one_cluster(client: typesense.Client):
        try:
            return await asyncio.to_thread(client.collections[collection_name].documents.search, search_params)
        except Exception as e:
            print(f"⚠️ Warning: Search failed for one cluster: {e}")
            return None # Gracefully handle failure of one shard

    tasks = [search_in_one_cluster(client) for client in clients]
    results_list = await asyncio.gather(*tasks)

    all_hits, total_found, search_time_ms = [], 0, 0
    for res in results_list:
        if res:
            all_hits.extend(res.get('hits', []))
            search_time_ms = max(search_time_ms, res.get('search_time_ms', 0))

    # Remove duplicates by ID, keeping the one with the best score (lowest text_match)
    unique_hits_map = {}
    for hit in all_hits:
        doc_id = hit.get('document', {}).get('id')
        if doc_id:
            if doc_id not in unique_hits_map or hit.get('text_match', 999) < unique_hits_map[doc_id].get('text_match', 999):
                unique_hits_map[doc_id] = hit
    
    unique_hits = list(unique_hits_map.values())
    total_found = len(unique_hits)
            
    # Re-sort results by score (text_match is the score, lower is better)
    unique_hits.sort(key=lambda x: x.get('text_match', float('inf')))
    
    start_index = (page - 1) * per_page
    paginated_hits = unique_hits[start_index : start_index + per_page]

    return {
        "found": total_found, "page": page, "search_time_ms": search_time_ms,
        "hits": paginated_hits, "shards_queried": len(clients), "shards_failed": results_list.count(None)
    }

# --- Admin Endpoints ---
@app.post("/admin/federation/clusters", status_code=201)
def register_cluster(cluster: Cluster):
    if cluster.name in federation_clients:
        raise HTTPException(status_code=409, detail=f"Cluster '{cluster.name}' is already registered.")
    try:
        new_client = typesense.Client({'nodes': [{'host': cluster.host, 'port': cluster.port, 'protocol': 'http'}],'api_key': cluster.api_key})
        federation_clients[cluster.name] = new_client
        federation_clusters_config[cluster.name] = cluster.dict()
        return {"status": "ok", "message": f"Cluster '{cluster.name}' registered."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create client for cluster: {e}")

@app.get("/admin/federation/clusters")
def get_all_clusters(): return federation_clusters_config

@app.post("/admin/collections", status_code=201)
async def create_collection(schema: CollectionSchema):
    # This logic creates the collection on ALL registered clusters to ensure schema consistency
    schema_dict = schema.dict()
    
    async def create_on_one_cluster(client: typesense.Client, cluster_name: str):
        try:
            return await asyncio.to_thread(client.collections.create, schema_dict)
        except typesense.exceptions.ObjectAlreadyExists:
             print(f"Collection '{schema.name}' already exists on cluster '{cluster_name}'. Skipping.")
             return "already_exists"
        except Exception as e:
            print(f"🔥 Failed to create collection '{schema.name}' on cluster '{cluster_name}': {e}")
            return str(e)

    tasks = [create_on_one_cluster(client, name) for name, client in federation_clients.items()]
    results = await asyncio.gather(*tasks)
    
    # Add an empty routing rule placeholder for the new collection
    if schema.name not in collection_routing_rules:
        collection_routing_rules[schema.name] = {}

    success_count = sum(1 for r in results if isinstance(r, dict) or r == "already_exists")
    if success_count == 0:
        raise HTTPException(status_code=500, detail=f"Failed to create collection on any cluster. Errors: {results}")

    return {"message": f"Collection create action dispatched to all {len(federation_clients)} clusters.", "success_count": success_count}

@app.post("/admin/routing-rules", status_code=201)
def set_routing_rule(rule: RoutingRule):
    all_clusters_in_rule = {r["cluster"] for r in rule.rules}
    all_clusters_in_rule.add(rule.default_cluster)
    if not all_clusters_in_rule.issubset(federation_clients.keys()):
        raise HTTPException(status_code=404, detail=f"One or more clusters in the rule are not registered.")
    
    collection_routing_rules[rule.collection] = rule.dict()
    return {"status": "ok", "message": f"Routing rule for collection '{rule.collection}' has been set."}

@app.get("/admin/routing-map")
def get_routing_map(): 
    return {"clusters": list(federation_clients.keys()), "collections": collection_routing_rules}

