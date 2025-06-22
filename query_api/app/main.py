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


app = FastAPI(title="IMPOSBRO Federated Search & Admin API", version="2.5.0")
Instrumentator().instrument(app).expose(app)


@app.on_event("startup")
async def startup_event():
    get_producer()
    default_cluster_config = {"host": settings.TYPESENSE_HOST, "port": settings.TYPESENSE_PORT,
                              "api_key": settings.TYPESENSE_API_KEY}
    default_client = typesense.Client({
        'nodes': [{'host': default_cluster_config['host'], 'port': default_cluster_config['port'], 'protocol': 'http'}],
        'api_key': default_cluster_config['api_key'], 'connection_timeout_seconds': 5
    })
    federation_clients["default"] = default_client
    federation_clusters_config["default"] = default_cluster_config
    print("✅ Default cluster registered.")


# Helper functions...
def get_client_for_document(collection_name: str, document: Dict) -> (typesense.Client, str):
    rule = collection_routing_rules.get(collection_name)
    target_cluster_name = "default"
    if rule and 'field' in rule:
        doc_value = document.get(rule["field"])
        target_cluster_name = next((r["cluster"] for r in rule["rules"] if r["value"] == doc_value),
                                   rule.get("default_cluster", "default"))
    client = federation_clients.get(target_cluster_name)
    if not client: raise HTTPException(status_code=404,
                                       detail=f"Cluster '{target_cluster_name}' for routing not found.")
    return client, target_cluster_name


def get_clients_for_collection_search(collection_name: str) -> List[typesense.Client]:
    if collection_name in collection_routing_rules and 'field' in collection_routing_rules[collection_name]:
        rule = collection_routing_rules[collection_name]
        cluster_names = {r["cluster"] for r in rule.get("rules", [])}
        cluster_names.add(rule.get("default_cluster", "default"))
        return [federation_clients[name] for name in cluster_names if name in federation_clients]
    return [client for client in federation_clients.values()]


# API endpoints...
@app.post("/ingest/{collection_name}")
def ingest_document(collection_name: str, document: Dict[str, Any]):
    # (Implementation as before)
    doc_id = document.get("id")
    if not doc_id: raise HTTPException(status_code=400, detail="Document must have an 'id' field.")
    _, target_cluster_name = get_client_for_document(collection_name, document)
    enriched_message = {"target_cluster": target_cluster_name, "collection": collection_name, "document": document}
    topic_name = f"{settings.KAFKA_TOPIC_PREFIX}_{collection_name}"
    try:
        get_producer().send(topic_name, key=str(doc_id).encode('utf-8'), value=enriched_message)
        get_producer().flush()
        documents_ingested.labels(collection=collection_name).inc()
        return {"status": "ok", "document_id": doc_id, "routed_to": target_cluster_name}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Kafka producer error: {str(e)}")


@app.get("/search/{collection_name}")
async def search(request: Request, collection_name: str, q: str = Query(..., min_length=1), query_by: str = Query(...),
                 filter_by: Optional[str] = None, sort_by: Optional[str] = None, page: int = Query(1, ge=1),
                 per_page: int = Query(10, ge=1, le=250)):
    # (Implementation as before)
    clients = get_clients_for_collection_search(collection_name)
    if not clients: raise HTTPException(status_code=404,
                                        detail=f"Collection '{collection_name}' not found on any registered cluster.")
    search_params = {k: v for k, v in
                     {'q': q, 'query_by': query_by, 'filter_by': filter_by, 'sort_by': sort_by, 'page': page,
                      'per_page': per_page}.items() if v is not None}

    async def search_in_one_cluster(client: typesense.Client):
        try:
            return await asyncio.to_thread(client.collections[collection_name].documents.search, search_params)
        except Exception as e:
            print(f"⚠️ Warning: Search failed for one cluster: {e}"); return None

    tasks = [search_in_one_cluster(client) for client in clients]
    results_list = await asyncio.gather(*tasks)
    all_hits = [];
    search_time_ms = 0
    for res in results_list:
        if res: all_hits.extend(res.get('hits', [])); search_time_ms = max(search_time_ms, res.get('search_time_ms', 0))
    unique_hits = sorted(list({hit.get('document', {}).get('id'): hit for hit in all_hits}.values()),
                         key=lambda x: x.get('text_match', float('inf')))
    return {"found": len(unique_hits), "page": page, "search_time_ms": search_time_ms, "hits": unique_hits}


# --- Admin Endpoints (with new DELETE methods) ---
@app.post("/admin/federation/clusters", status_code=201)
def register_cluster(cluster: Cluster):
    # (Implementation as before)
    if cluster.name in federation_clients: raise HTTPException(status_code=409,
                                                               detail=f"Cluster '{cluster.name}' is already registered.")
    try:
        new_client = typesense.Client(
            {'nodes': [{'host': cluster.host, 'port': cluster.port, 'protocol': 'http'}], 'api_key': cluster.api_key})
        federation_clients[cluster.name] = new_client;
        federation_clusters_config[cluster.name] = cluster.dict()
        return {"status": "ok", "message": f"Cluster '{cluster.name}' registered."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create client for cluster: {e}")


@app.get("/admin/federation/clusters")
def get_all_clusters(): return federation_clusters_config


@app.delete("/admin/federation/clusters/{cluster_name}", status_code=200)
def delete_cluster(cluster_name: str):
    # (Implementation as before)
    if cluster_name == "default": raise HTTPException(status_code=400, detail="Cannot delete the default cluster.")
    if cluster_name not in federation_clients: raise HTTPException(status_code=404,
                                                                   detail=f"Cluster '{cluster_name}' not found.")
    for collection, rule in collection_routing_rules.items():
        if rule and (rule.get("default_cluster") == cluster_name or any(
                r.get("cluster") == cluster_name for r in rule.get("rules", []))):
            raise HTTPException(status_code=400,
                                detail=f"Cannot delete cluster '{cluster_name}', in use by routing rule for '{collection}'.")
    try:
        del federation_clients[cluster_name];
        del federation_clusters_config[cluster_name]
        return {"status": "ok", "message": f"Cluster '{cluster_name}' deleted successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/admin/collections", status_code=201)
async def create_collection(schema: CollectionSchema):
    schema_dict = schema.dict()
    if schema_dict.get("default_sorting_field") is None:
        del schema_dict["default_sorting_field"]

    async def create_on_one_cluster(client: typesense.Client, cluster_name: str):
        try:
            return await asyncio.to_thread(client.collections.create, schema_dict)
        except typesense.exceptions.ObjectAlreadyExists:
            return "already_exists"
        except Exception as e:
            print(f"🔥 Failed to create collection '{schema.name}' on '{cluster_name}': {e}"); return str(e)

    tasks = [create_on_one_cluster(client, name) for name, client in federation_clients.items()]
    results = await asyncio.gather(*tasks)
    if schema.name not in collection_routing_rules: collection_routing_rules[schema.name] = {}
    success_count = sum(1 for r in results if isinstance(r, dict) or r == "already_exists")
    if success_count == 0: raise HTTPException(status_code=500,
                                               detail=f"Failed to create collection on any cluster. Errors: {results}")
    return {"message": f"Collection create action dispatched.", "success_count": success_count}


@app.delete("/admin/collections/{collection_name}", status_code=200)
async def delete_collection(collection_name: str):
    """Deletes a collection from all clusters and removes its routing rule."""

    async def delete_on_one_cluster(client: typesense.Client, cluster_name: str):
        try:
            return await asyncio.to_thread(client.collections[collection_name].delete)
        except typesense.exceptions.ObjectNotFound:
            print(f"Collection '{collection_name}' not found on cluster '{cluster_name}'. Skipping.")
            return "not_found"
        except Exception as e:
            print(f"🔥 Failed to delete collection '{collection_name}' on '{cluster_name}': {e}")
            return str(e)

    tasks = [delete_on_one_cluster(client, name) for name, client in federation_clients.items()]
    await asyncio.gather(*tasks)

    # Also remove from in-memory routing rules
    if collection_name in collection_routing_rules:
        del collection_routing_rules[collection_name]

    return {"status": "ok", "message": f"Collection '{collection_name}' deleted from all clusters."}


@app.post("/admin/routing-rules", status_code=201)
def set_routing_rule(rule: RoutingRule):
    # (Implementation as before)
    all_clusters = {r["cluster"] for r in rule.rules}
    all_clusters.add(rule.default_cluster)
    if not all_clusters.issubset(federation_clients.keys()): raise HTTPException(status_code=404,
                                                                                 detail="One or more clusters in the rule are not registered.")
    collection_routing_rules[rule.collection] = rule.dict()
    return {"status": "ok", "message": f"Routing rule for collection '{rule.collection}' has been set."}


@app.delete("/admin/routing-rules/{collection_name}", status_code=200)
def delete_routing_rule(collection_name: str):
    """Deletes a routing rule for a collection, reverting it to 'Not Sharded' state."""
    if collection_name not in collection_routing_rules:
        raise HTTPException(status_code=404, detail=f"No routing rule found for collection '{collection_name}'.")

    # Resetting the rule to an empty dict signifies it's no longer sharded
    collection_routing_rules[collection_name] = {}
    return {"status": "ok", "message": f"Routing rule for '{collection_name}' has been deleted."}


@app.get("/admin/routing-map")
def get_routing_map():
    return {"clusters": list(federation_clients.keys()), "collections": collection_routing_rules}
