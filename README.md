# IMPOSBRO SEARCH (Enterprise Federated Architecture Typesense based search engine)

Welcome to **IMPOSBRO SEARCH**. The name is an acronym for the Italian sentence **"il mio primo progetto open source scritto con un braccio rotto"**, reflecting the project's challenging origins.

This is a complete, enterprise-grade open-source search framework designed for true document-level sharding, high availability, and comprehensive management.
## ✨ Final Features

* **Document-Level Sharding:** Define routing rules based on document fields (e.g., `country`, `tenant_id`) to distribute a single logical collection across multiple physical clusters.
* **Resilient Scatter-Gather Search:** Queries are automatically sent to all relevant clusters, results are merged and re-ranked, and the system gracefully handles partial failures if a shard is unavailable.
* **Federated Indexing:** The ingestion pipeline intelligently routes documents to the correct physical shard based on your rules.
* **Fully Functional Next.js Admin UI:** A complete web interface to manage clusters, collections, and routing rules from your browser.
* **Enterprise-Ready:** Includes robust caching, guaranteed message ordering via Kafka, and a full Prometheus + Grafana monitoring stack.

## 🚀 Quick Start Guide

### 1. Start the Services

```bash
# Navigate into the project directory
cd ./imposbro-search

# Copy the example environment file
Copy-Item .env.example .env

# Build and start all services
docker-compose up --build
```

### 2. Access the UIs

* **Admin UI:** `http://localhost:3001` - **Your primary control panel.**
* **Grafana Monitoring:** `http://localhost:3000` (Login: `admin` / `admin`)

### 3. Test Document-Level Sharding via the Admin UI

1.  **Open the Admin UI** at `http://localhost:3001`.
2.  **Register Clusters:** Go to the **Clusters** page and register two clusters:
    * **Cluster 1:** Name: `cluster-it`, Host: `typesense`, Port: `8108`, API Key: `xyz`
    * **Cluster 2:** Name: `cluster-us`, Host: `typesense-replica`, Port: `8108`, API Key: `xyz-replica`
3.  **Create a Collection:** Go to the **Collections** page and create a collection named `users` on the `default` cluster.
4.  **Define Routing Rules:** Go to the **Routing** page.
    * **Collection Name:** `users`
    * **Routing Field:** `country`
    * **Rule 1:** Value `IT` -> Cluster `cluster-it`
    * **Rule 2:** Value `US` -> Cluster `cluster-us`
    * **Default Cluster:** `cluster-it`
    * Click "Set Routing Rule".
5.  **Ingest Sharded Data:** Use `curl` to push documents. The `query_api` will now route them based on the `country` field.
    ```bash
    # This document will go to the 'cluster-it' (typesense)
    curl -X POST "http://localhost:8000/ingest/users" -H "Content-Type: application/json" -d '{"id": "user-1", "name": "Giovanni", "country": "IT"}'

    # This document will go to the 'cluster-us' (typesense-replica)
    curl -X POST "http://localhost:8000/ingest/users" -H "Content-Type: application/json" -d '{"id": "user-2", "name": "John", "country": "US"}'
    ```
6.  **Run a Federated Search:** This single query will hit **both** clusters and merge the results.
    ```bash
    curl "http://localhost:8000/search/users?q=*&query_by=name"
    ```
    *Expected Output:* You will see both Giovanni and John in the search results.

### 4. Next steps

1.  Tests
2.  Support for alias and collection copy
3.  Helm and K8 support
4.  Better UI :D
