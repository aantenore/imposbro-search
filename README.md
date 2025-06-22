# IMPOSBRO SEARCH (Enterprise Federated Architecture Typesense-based search engine)

Welcome to **IMPOSBRO SEARCH**. The name is an acronym for the Italian sentence **"il mio primo progetto open source scritto con un braccio rotto"**, reflecting the project's challenging origins.

This is a complete, enterprise-grade open-source search framework designed for true document-level sharding, high availability, and comprehensive management using Typesense.

## ✨ Final Features

* **Document-Level Sharding:** Define routing rules based on document fields (e.g., `country`, `tenant_id`) to distribute a single logical collection across multiple physical clusters.

* **Resilient Scatter-Gather Search:** Queries are automatically sent to all relevant clusters, results are merged and re-ranked, and the system gracefully handles partial failures if a shard is unavailable.

* **Federated Indexing:** The ingestion pipeline intelligently routes documents to the correct physical shard based on your rules.

* **Fully Functional Next.js Admin UI:** A complete web interface to manage clusters, collections, and routing rules from your browser.

* **Enterprise-Ready:** Includes robust caching, guaranteed message ordering via Kafka, and a full Prometheus + Grafana monitoring stack.

## 🚀 Local Deployment with Docker Compose

### 1. Start the Services


Navigate into the project directory
cd ./imposbro-search

Copy the example environment file
Copy-Item .env.example .env

Build and start all services
docker-compose up --build


### 2. Access the UIs

* **Admin UI:** `http://localhost:3001` - **Your primary control panel.**

* **Grafana Monitoring:** `http://localhost:3000` (Login: `admin` / `admin`)

### 3. Test Document-Level Sharding via the Admin UI

1. **Open the Admin UI** at `http://localhost:3001`.

2. **Register Clusters:** Go to the **Clusters** page and register two clusters:

   * **Cluster 1:** Name: `cluster-it`, Host: `typesense`, Port: `8108`, API Key: `xyz`

   * **Cluster 2:** Name: `cluster-us`, Host: `typesense-replica`, Port: `8108`, API Key: `xyz-replica`

3. **Create a Collection:** Go to the **Collections** page and create a collection named `users`.

4. **Define Routing Rules:** Go to the **Routing** page.

   * **Collection Name:** `users`

   * **Routing Field:** `country`

   * **Rule 1:** Value `IT` -> Cluster `cluster-it`

   * **Rule 2:** Value `US` -> Cluster `cluster-us`

   * **Default Cluster:** `cluster-it`

   * Click "Set Routing Rule".

5. **Ingest Sharded Data:** Use `curl` to push documents. The `query_api` will now route them based on the `country` field.


This document will go to the 'cluster-it' (typesense)
curl -X POST "http://localhost:8000/ingest/users" -H "Content-Type: application/json" -d '{"id": "user-1", "name": "Giovanni", "country": "IT"}'

This document will go to the 'cluster-us' (typesense-replica)
curl -X POST "http://localhost:8000/ingest/users" -H "Content-Type: application/json" -d '{"id": "user-2", "name": "John", "country": "US"}'


6. **Run a Federated Search:** This single query will hit **both** clusters and merge the results.


curl "http://localhost:8000/search/users?q=*&query_by=name"


*Expected Output:* You will see both Giovanni and John in the search results.

## 🚀 Deployment to Kubernetes with Helm

This section describes how to deploy the `admin-ui` and `query-api` services to a Kubernetes cluster using the provided Helm chart.

**Prerequisites:**

* A running Kubernetes cluster.

* `kubectl` configured to connect to your cluster.

* Helm v3 installed.

* A container registry (e.g., Docker Hub, GCR, ECR) to host your Docker images.

### Step 1: Build and Push Docker Images

The Helm chart deploys pre-built images. You must first build the images from the source code and push them to your registry.


1. Build the images using docker-compose
docker-compose build admin-ui query-api

2. Tag the images for your registry
Replace 'your-registry-user' with your container registry's username/organization
docker tag imposbro-search-admin-ui your-registry-user/imposbro-admin-ui:latest
docker tag imposbro-search-query-api your-registry-user/imposbro-query-api:latest

3. Push the images to the registry
docker push your-registry-user/imposbro-admin-ui:latest
docker push your-registry-user/imposbro-query-api:latest


### Step 2: Configure and Deploy the Helm Chart

1. **Update `values.yaml`:** Open the `helm/imposbro-search/values.yaml` file. Find the `image` keys for `queryApi` and `adminUi` and update them to match the image names you just pushed to your registry.

2. **Install the Chart:** From the project's root directory, run the following command to install the chart into your Kubernetes cluster. This will create a new release named `imposbro-release`.


helm install imposbro-release ./helm/imposbro-search


To check the status of your deployment, you can run:
`kubectl get all -l app.kubernetes.io/instance=imposbro-release`

*(Note: This Helm chart only deploys the custom applications. For a complete production deployment, you should also deploy Kafka, Typesense, and Redis, preferably using their own official Helm charts.)*

## 4. Next Steps

* \[ \] Add functional and integration tests.

* \[ \] Implement support for collection aliases and copying.

* \[x\] Finalize Helm chart for Kubernetes deployment.

* \[ \] Enhance UI/UX with more feedback and loading states.
