# query_api/app/main.py
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

# Import settings from the new settings file
from settings import settings

# --- Typesense-based Persistence Setup ---
STATE_COLLECTION_NAME = "_imposbro_state"
STATE_DOCUMENT_ID = "config_v1"

# --- In-Memory State for Federation (will be populated from Typesense) ---
federation_clusters_config: Dict[str, Dict] = {}
federation_clients: Dict[str, typesense.Client] = {}
collection_routing_rules: Dict[str, Dict] = {}

# This client connects to the entire HA cluster for resilience
ha_client: Optional[typesense.Client] = None


def save_state_to_typesense():
    """Saves the current configuration state to a document in the Typesense HA cluster."""
    if not ha_client:
        print("🔥 Cannot save state: HA Typesense client not available.")
        return

    try:
        state = {
            "federation_clusters_config": federation_clusters_config,
            "collection_routing_rules": collection_routing_rules,
        }
        state_document = {"id": STATE_DOCUMENT_ID, "state_data": json.dumps(state)}
        ha_client.collections[STATE_COLLECTION_NAME].documents.upsert(state_document)
        print(f"✅ State successfully saved to Typesense collection '{STATE_COLLECTION_NAME}'.")
    except Exception as e:
        print(f"🔥 Failed to save state to Typesense: {e}")


def load_state_from_typesense():
    """Loads configuration state from the Typesense HA cluster if it exists."""
    global federation_clusters_config, collection_routing_rules, federation_clients
    if not ha_client: return

    try:
        try:
            ha_client.collections[STATE_COLLECTION_NAME].retrieve()
        except typesense.exceptions.ObjectNotFound:
            print(f"💡 State collection '{STATE_COLLECTION_NAME}' not found. Creating it.")
            ha_client.collections.create({
                'name': STATE_COLLECTION_NAME,
                'fields': [{'name': 'state_data', 'type': 'string'}]
            })

        state_document = ha_client.collections[STATE_COLLECTION_NAME].documents[STATE_DOCUMENT_ID].retrieve()
        state = json.loads(state_document.get("state_data", "{}"))

        federation_clusters_config.update(state.get("federation_clusters_config", {}))
        collection_routing_rules.update(state.get("collection_routing_rules", {}))

        for name, config in federation_clusters_config.items():
            if name != "default":
                client = typesense.Client(
                    nodes=[{'host': config['host'], 'port': config['port'], 'protocol': 'http'}],
                    api_key=config['api_key'],
                    connection_timeout_seconds=5
                )
                federation_clients[name] = client
        print(f"✅ State successfully loaded from Typesense.")
    except typesense.exceptions.ObjectNotFound:
        print(f"💡 No saved state found in Typesense. Starting with a fresh configuration.")
    except Exception as e:
        print(f"🔥 Failed to load state from Typesense, starting fresh: {e}")


# --- Kafka Producer & Pydantic Models ---
producer = None
documents_ingested = Counter('documents_ingested_total', 'Total number of documents ingested.', ['collection'])


def get_producer():
    global producer
    if producer is None:
        while True:
            try:
                producer = KafkaProducer(bootstrap_servers=settings.KAFKA_BROKER_URL,
                                         value_serializer=lambda v: json.dumps(v).encode('utf-8'))
                print("✅ Kafka Producer connected successfully.")
                break
            except Exception as e:
                print(f"🔥 Failed to connect to Kafka: {e}. Retrying...")
                time.sleep(5)
    return producer


class Cluster(BaseModel): name: str; host: str; port: int; api_key: str


class CollectionField(BaseModel): name: str; type: str; facet: bool = False


class CollectionSchema(BaseModel): name: str; fields: List[CollectionField]; default_sorting_field: Optional[str] = None


class FieldRule(BaseModel): field: str; value: str; cluster: str


class RoutingRules(BaseModel): collection: str; rules: List[FieldRule]; default_cluster: str = "default"


app = FastAPI(title="IMPOSBRO Federated Search & Admin API", version="3.0.0-HA")
Instrumentator().instrument(app).expose(app)


@app.on_event("startup")
async def startup_event():
    global ha_client
    typesense_nodes = [{"host": host, "port": "8108", "protocol": "http"} for host in
                       settings.TYPESENSE_NODES.split(',')]
    ha_client = typesense.Client(
        {'nodes': typesense_nodes, 'api_key': settings.TYPESENSE_API_KEY, 'connection_timeout_seconds': 10})

    federation_clients["default"] = ha_client
    if "default" not in federation_clusters_config:
        federation_clusters_config["default"] = {"name": "default", "host": settings.TYPESENSE_NODES, "port": 8108,
                                                 "api_key": settings.TYPESENSE_API_KEY}
    print("✅ HA Typesense client initialized.")

    load_state_from_typesense()
    get_producer()


# --- Helper Functions ---
def get_client_for_document(collection_name: str, document: Dict) -> (typesense.Client, str):
    collection_rules = collection_routing_rules.get(collection_name)
    if not collection_rules or not collection_rules.get("rules"):
        return federation_clients.get("default"), "default"

    for rule in collection_rules["rules"]:
        if document.get(rule["field"]) == rule["value"]:
            target_cluster_name = rule["cluster"]
            client = federation_clients.get(target_cluster_name)
            if not client: raise HTTPException(status_code=404,
                                               detail=f"Target cluster '{target_cluster_name}' from rule not found.")
            return client, target_cluster_name

    default_cluster_name = collection_rules.get("default_cluster", "default")
    return federation_clients.get(default_cluster_name), default_cluster_name


def get_clients_for_collection_search(collection_name: str) -> List[typesense.Client]:
    if collection_name in collection_routing_rules and collection_routing_rules.get(collection_name, {}).get('rules'):
        rule = collection_routing_rules[collection_name]
        cluster_names = {r["cluster"] for r in rule.get("rules", [])}
        cluster_names.add(rule.get("default_cluster", "default"))
        return [federation_clients[name] for name in cluster_names if name in federation_clients]
    return list(federation_clients.values())


# --- API Endpoints ---
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
        return {"status": "ok", "document_id": doc_id, "routed_to": target_cluster_name}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Kafka producer error: {str(e)}")


@app.get("/search/{collection_name}")
async def search(request: Request, collection_name: str, q: str = Query(..., min_length=1), query_by: str = Query(...),
                 filter_by: Optional[str] = None, sort_by: Optional[str] = None, page: int = Query(1, ge=1),
                 per_page: int = Query(10, ge=1, le=250)):
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

    all_hits, total_found = [], 0
    for res in results_list:
        if res: all_hits.extend(res.get('hits', [])); total_found += res.get("found", 0)

    unique_hits_map = {hit.get('document', {}).get('id'): hit for hit in all_hits if hit.get('document', {}).get('id')}
    unique_hits = sorted(list(unique_hits_map.values()), key=lambda x: x.get('text_match', float('inf')))

    return {"found": total_found, "page": page, "hits": unique_hits}


@app.post("/admin/federation/clusters", status_code=201)
def register_cluster(cluster: Cluster):
    if cluster.name in federation_clients: raise HTTPException(status_code=409,
                                                               detail=f"Cluster '{cluster.name}' is already registered.")
    try:
        new_client = typesense.Client(nodes=[{'host': cluster.host, 'port': cluster.port, 'protocol': 'http'}],
                                      api_key=cluster.api_key)
        federation_clients[cluster.name] = new_client
        federation_clusters_config[cluster.name] = cluster.dict()
        save_state_to_typesense()
        return {"status": "ok", "message": f"Cluster '{cluster.name}' registered."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create client: {e}")


@app.delete("/admin/federation/clusters/{cluster_name}", status_code=200)
def delete_cluster(cluster_name: str):
    if cluster_name == "default": raise HTTPException(status_code=400, detail="Cannot delete the default cluster.")
    if cluster_name not in federation_clients: raise HTTPException(status_code=404,
                                                                   detail=f"Cluster '{cluster_name}' not found.")
    del federation_clients[cluster_name]
    del federation_clusters_config[cluster_name]
    save_state_to_typesense()
    return {"status": "ok", "message": f"Cluster '{cluster_name}' deleted."}


@app.get("/admin/federation/clusters")
def get_all_clusters(): return federation_clusters_config


@app.post("/admin/collections", status_code=201)
async def create_collection(schema: CollectionSchema):
    schema_dict = schema.dict()
    if schema_dict.get("default_sorting_field") is None: del schema_dict["default_sorting_field"]

    async def create_on_one_cluster(client, name):
        try:
            await asyncio.to_thread(client.collections.create, schema_dict)
        except typesense.exceptions.ObjectAlreadyExists:
            pass
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed on cluster {name}: {e}")

    tasks = [create_on_one_cluster(client, name) for name, client in federation_clients.items()]
    await asyncio.gather(*tasks)

    if schema.name not in collection_routing_rules: collection_routing_rules[schema.name] = {"rules": [],
                                                                                             "default_cluster": "default"}
    save_state_to_typesense()
    return {"message": "Collection created successfully on all clusters."}


@app.get("/admin/collections/{collection_name}", status_code=200)
def get_collection_schema(collection_name: str):
    if not ha_client: raise HTTPException(status_code=500, detail="Default HA client not available.")
    try:
        return ha_client.collections[collection_name].retrieve()
    except typesense.exceptions.ObjectNotFound:
        raise HTTPException(status_code=404, detail=f"Collection '{collection_name}' not found.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve schema: {e}")


@app.delete("/admin/collections/{collection_name}", status_code=200)
async def delete_collection(collection_name: str):
    async def delete_on_one_cluster(client: typesense.Client):
        try:
            await asyncio.to_thread(client.collections[collection_name].delete)
        except typesense.exceptions.ObjectNotFound:
            pass

    tasks = [delete_on_one_cluster(client) for client in federation_clients.values()]
    await asyncio.gather(*tasks)

    if collection_name in collection_routing_rules:
        del collection_routing_rules[collection_name]
    save_state_to_typesense()
    return {"status": "ok", "message": f"Collection '{collection_name}' deleted."}


@app.post("/admin/routing-rules", status_code=201)
def set_routing_rules(rules_config: RoutingRules):
    collection_routing_rules[rules_config.collection] = rules_config.dict()
    save_state_to_typesense()
    return {"status": "ok", "message": f"Routing rules for '{rules_config.collection}' set."}


@app.delete("/admin/routing-rules/{collection_name}", status_code=200)
def delete_routing_rule(collection_name: str):
    if collection_name in collection_routing_rules:
        collection_routing_rules[collection_name] = {"rules": [], "default_cluster": "default"}
        save_state_to_typesense()
    return {"status": "ok", "message": f"Routing rule for '{collection_name}' deleted."}


@app.get("/admin/routing-map")
def get_routing_map(): return {"clusters": list(federation_clients.keys()), "collections": collection_routing_rules}
