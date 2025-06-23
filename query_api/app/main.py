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

# Import settings from the settings file
from settings import settings

# --- Typesense-based Persistence Setup ---
STATE_COLLECTION_NAME = "_imposbro_state"
STATE_DOCUMENT_ID = "config_v1"

# --- In-Memory State for Federation ---
federation_clusters_config: Dict[str, Dict] = {}
federation_clients: Dict[str, typesense.Client] = {}
collection_routing_rules: Dict[str, Dict] = {}

# Client for the internal, stateful HA cluster
state_ha_client: Optional[typesense.Client] = None


def save_state_to_typesense():
    if not state_ha_client: return
    try:
        state = {
            "federation_clusters_config": federation_clusters_config,
            "collection_routing_rules": collection_routing_rules,
        }
        state_document = {"id": STATE_DOCUMENT_ID, "state_data": json.dumps(state)}
        state_ha_client.collections[STATE_COLLECTION_NAME].documents.upsert(state_document)
        print(f"✅ State successfully saved to internal Typesense.")
    except Exception as e:
        print(f"🔥 Failed to save state: {e}")


def load_state_from_typesense():
    global federation_clusters_config, collection_routing_rules, federation_clients
    if not state_ha_client: return False
    try:
        try:
            state_ha_client.collections[STATE_COLLECTION_NAME].retrieve()
        except typesense.exceptions.ObjectNotFound:
            print(f"💡 State collection '{STATE_COLLECTION_NAME}' not found. Will create it after loading.")
            return False  # Indicate that no state was loaded

        state_document = state_ha_client.collections[STATE_COLLECTION_NAME].documents[STATE_DOCUMENT_ID].retrieve()
        state = json.loads(state_document.get("state_data", "{}"))

        federation_clusters_config.update(state.get("federation_clusters_config", {}))
        collection_routing_rules.update(state.get("collection_routing_rules", {}))

        for name, config in federation_clusters_config.items():
            nodes = [{'host': h, 'port': config['port'], 'protocol': 'http'} for h in config['host'].split(',')]
            federation_clients[name] = typesense.Client(nodes=nodes, api_key=config['api_key'],
                                                        connection_timeout_seconds=5)

        print(f"✅ Loaded {len(federation_clusters_config)} federated cluster(s) from state.")
        return True
    except typesense.exceptions.ObjectNotFound:
        print(f"💡 No saved state document found.")
        return False
    except Exception as e:
        print(f"🔥 Error loading state: {e}")
        return False


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


app = FastAPI(title="IMPOSBRO Federated Search & Admin API", version="3.4.1-Fix")
Instrumentator().instrument(app).expose(app)


@app.on_event("startup")
async def startup_event():
    global state_ha_client

    # Initialize and wait for the INTERNAL STATE cluster
    while True:
        try:
            print("⏳ Checking Typesense cluster readiness...")
            # The first real operation is to load state, which requires a healthy leader.
            # This serves as our health check.

            conf = create_client_config('internal-state-cluster',settings.INTERNAL_STATE_NODES, settings.INTERNAL_STATE_API_KEY)
            state_ha_client = create_client(conf)

            state_loaded = load_state_from_typesense()
            print("✅ Typesense cluster is ready.")
            break
        except (typesense.exceptions.ServiceUnavailable, typesense.exceptions.ConnectionError,
                typesense.exceptions.ConnectionTimeout) as e:
            # These exceptions are expected if the cluster is not ready or has no leader.
            print(f"⚠️ Typesense cluster not ready yet ({type(e).__name__}). Retrying in 5 seconds...")
            time.sleep(5)
        except Exception as e:
            # Handle other potential exceptions during startup
            print(f"🔥 An unexpected error occurred during startup health check: {e}. Retrying...")
            time.sleep(5)

    if not state_loaded:
        print("💡 No existing state found. Bootstrapping default data cluster configuration...")

        # Create client and register it
        name = 'default-data-cluster'

        federation_clusters_config[name] = create_client_config(name, settings.DEFAULT_DATA_CLUSTER_NODES, settings.DEFAULT_DATA_CLUSTER_API_KEY)
        federation_clients[name] = create_client(federation_clusters_config[name])

        # This becomes the default destination for un-routed documents
        # Note: We need a mechanism to set this in collection_routing_rules
        print("✅ Default data cluster bootstrapped.")
        save_state_to_typesense()

    get_producer()


def create_client_config(name, nodes_str, api_key):
    return {
        'name': name,
        'host': nodes_str,
        'port': 8108,
        'api_key': api_key
    }

def create_client(conf):
    state_nodes = [{'host': h, 'port': '8108', 'protocol': 'http'} for h in
                           conf['host'].split(',')]
    return typesense.Client(
        {'nodes': state_nodes, 'api_key': conf['api_key']})


# --- Helper Functions ---
def get_client_for_document(collection_name: str, document: Dict) -> (typesense.Client, str):
    collection_rules_config = collection_routing_rules.get(collection_name)

    if not collection_rules_config or not collection_rules_config.get("rules"):
        return federation_clients.get("default"), "default"

    for rule in collection_rules_config["rules"]:
        doc_value = document.get(rule["field"])
        if doc_value is not None and str(doc_value) == rule["value"]:
            target_cluster_name = rule["cluster"]
            client = federation_clients.get(target_cluster_name)
            if not client: raise HTTPException(status_code=404,
                                               detail=f"Target cluster '{target_cluster_name}' from rule not found.")
            return client, target_cluster_name

    default_cluster_name = collection_rules_config.get("default_cluster", "default")
    return federation_clients.get(default_cluster_name), default_cluster_name


def get_clients_for_collection_search(collection_name: str) -> List[typesense.Client]:
    if collection_name in collection_routing_rules and collection_routing_rules.get(collection_name, {}).get('rules'):
        rule = collection_routing_rules[collection_name]
        cluster_names = {r["cluster"] for r in rule.get("rules", [])}
        cluster_names.add(rule.get("default_cluster", "default"))
        return [federation_clients[name] for name in set(cluster_names) if name in federation_clients]
    return list(federation_clients.values())


# --- API Endpoints ---
@app.post("/ingest/{collection_name}")
def ingest_document(collection_name: str, document: Dict[str, Any]):
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
    if cluster.name in federation_clients:
        raise HTTPException(status_code=409, detail=f"Cluster '{cluster.name}' is already registered.")
    try:
        # Correctly create client with a config dictionary
        config = {
            'nodes': [{'host': cluster.host, 'port': cluster.port, 'protocol': 'http'}],
            'api_key': cluster.api_key,
            'connection_timeout_seconds': 5
        }
        new_client = typesense.Client(config)
        federation_clients[cluster.name] = new_client
        federation_clusters_config[cluster.name] = cluster.dict()
        save_state_to_typesense()
        return {"status": "ok", "message": f"Cluster '{cluster.name}' registered."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create client for cluster: {str(e)}")


@app.delete("/admin/federation/clusters/{cluster_name}", status_code=200)
def delete_cluster(cluster_name: str):
    if cluster_name == "default":
        raise HTTPException(status_code=400, detail="Cannot delete the default cluster.")
    if cluster_name not in federation_clients:
        raise HTTPException(status_code=404, detail=f"Cluster '{cluster_name}' not found.")

    # Check if cluster is in use before deleting
    for collection, rule_config in collection_routing_rules.items():
        if rule_config.get("default_cluster") == cluster_name:
            raise HTTPException(status_code=400, detail=f"Cluster in use as default for collection '{collection}'.")
        for rule in rule_config.get("rules", []):
            if rule.get("cluster") == cluster_name:
                raise HTTPException(status_code=400, detail=f"Cluster in use by a rule for collection '{collection}'.")

    del federation_clients[cluster_name]
    del federation_clusters_config[cluster_name]
    save_state_to_typesense()
    return {"status": "ok", "message": f"Cluster '{cluster_name}' deleted."}


@app.get("/admin/federation/clusters")
def get_all_clusters():
    # Create a display-friendly version of the config for the UI
    display_config = federation_clusters_config.copy()
    display_config["default"] = {
        "name": "default",
        "host": "Internal HA Cluster",
        "port": 8108,
        "api_key": "N/A"
    }
    return display_config


@app.post("/admin/collections", status_code=201)
async def create_collection(schema: CollectionSchema):
    schema_dict = schema.dict()
    if schema_dict.get("default_sorting_field") is None:
        del schema_dict["default_sorting_field"]

    async def create_on_one_cluster(client, name):
        try:
            await asyncio.to_thread(client.collections.create, schema_dict)
        except typesense.exceptions.ObjectAlreadyExists:
            pass
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed on cluster {name}: {e}")

    tasks = [create_on_one_cluster(client, name) for name, client in federation_clients.items()]
    await asyncio.gather(*tasks)

    if schema.name not in collection_routing_rules:
        collection_routing_rules[schema.name] = {"rules": [], "default_cluster": "default"}
    save_state_to_typesense()
    return {"message": "Collection created successfully on all clusters."}


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


@app.get("/admin/collections/{collection_name}", status_code=200)
def get_collection_schema(collection_name: str):
    if not ha_client:
        raise HTTPException(status_code=500, detail="Default HA client not available.")
    try:
        schema_info = ha_client.collections[collection_name].retrieve()
        return {"fields": schema_info.get("fields", [])}
    except typesense.exceptions.ObjectNotFound:
        raise HTTPException(status_code=404, detail=f"Collection '{collection_name}' not found.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve schema: {str(e)}")


@app.post("/admin/routing-rules", status_code=201)
def set_routing_rules(rules_config: RoutingRules):
    collection_name = rules_config.collection
    try:
        if not ha_client: raise HTTPException(status_code=500, detail="Default HA client not available.")
        ha_client.collections[collection_name].retrieve()
    except typesense.exceptions.ObjectNotFound:
        raise HTTPException(status_code=404,
                            detail=f"Cannot set rules for non-existent collection '{collection_name}'.")

    all_clusters_in_rules = {r.cluster for r in rules_config.rules}
    all_clusters_in_rules.add(rules_config.default_cluster)
    if not all_clusters_in_rules.issubset(federation_clients.keys()):
        raise HTTPException(status_code=404, detail="One or more clusters specified in the rules are not registered.")

    collection_routing_rules[collection_name] = rules_config.dict()
    save_state_to_typesense()
    return {"status": "ok", "message": f"Routing rules for '{collection_name}' have been updated."}


@app.delete("/admin/routing-rules/{collection_name}", status_code=200)
def delete_routing_rule(collection_name: str):
    if collection_name in collection_routing_rules:
        collection_routing_rules[collection_name] = {"rules": [], "default_cluster": "default"}
        save_state_to_typesense()
    return {"status": "ok", "message": f"Routing rules for '{collection_name}' have been deleted."}


@app.get("/admin/routing-map")
def get_routing_map():
    return {"clusters": list(federation_clients.keys()), "collections": collection_routing_rules}
