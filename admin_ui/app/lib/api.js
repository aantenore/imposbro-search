/**
 * IMPOSBRO Search Admin - API Client
 * 
 * Centralized API client for all backend communication.
 * Provides consistent error handling and response formatting.
 */

const API_BASE = '/api';

function encodeSegment(value) {
    return encodeURIComponent(value);
}

/**
 * Custom error class for API errors
 */
export class ApiError extends Error {
    constructor(message, status, data) {
        super(message);
        this.name = 'ApiError';
        this.status = status;
        this.data = data;
    }
}

/**
 * Make an API request with consistent error handling
 * 
 * @param {string} endpoint - API endpoint (without base URL)
 * @param {Object} options - Fetch options
 * @returns {Promise<any>} Response data
 * @throws {ApiError} On API error
 */
async function request(endpoint, options = {}) {
    const url = `${API_BASE}${endpoint}`;
    const { redirectOnAuth = true, ...fetchOptions } = options;

    const config = {
        ...fetchOptions,
        headers: {
            'Content-Type': 'application/json',
            ...fetchOptions.headers,
        },
    };

    try {
        const response = await fetch(url, config);
        const contentType = response.headers.get('Content-Type') || '';
        let data;
        try {
            data = contentType.includes('application/json')
                ? await response.json()
                : { detail: await response.text() || `HTTP ${response.status}` };
        } catch (_) {
            data = { detail: `HTTP ${response.status}` };
        }

        if (!response.ok) {
            if (
                response.status === 401 &&
                redirectOnAuth &&
                data.login_url &&
                typeof window !== 'undefined'
            ) {
                window.location.assign(data.login_url);
            }
            throw new ApiError(
                data.detail || data.message || 'An error occurred',
                response.status,
                data
            );
        }

        return data;
    } catch (error) {
        if (error instanceof ApiError) {
            throw error;
        }
        throw new ApiError(error.message || 'Network error', 0, null);
    }
}

/**
 * API client with methods for all endpoints
 */
export const api = {
    // ===== Admin UI auth session =====
    auth: {
        /**
         * Check whether Admin UI OIDC is enabled and if the browser has a session.
         */
        session: () => request('/auth/session', { redirectOnAuth: false }),
    },

    // ===== Clusters =====
    clusters: {
        /**
         * Get all registered clusters
         */
        list: () => request('/admin/federation/clusters'),

        /**
         * Register a new cluster
         */
        create: (cluster) => request('/admin/federation/clusters', {
            method: 'POST',
            body: JSON.stringify(cluster),
        }),

        /**
         * Delete a cluster
         */
        delete: (name) => request(`/admin/federation/clusters/${encodeSegment(name)}`, {
            method: 'DELETE',
        }),
    },

    // ===== Collections =====
    collections: {
        /**
         * Get collection schema
         */
        get: (name) => request(`/admin/collections/${encodeSegment(name)}`),

        /**
         * Create a new collection
         */
        create: (schema) => request('/admin/collections', {
            method: 'POST',
            body: JSON.stringify(schema),
        }),

        /**
         * Recreate any missing desired collection schemas on registered clusters
         */
        reconcile: () => request('/admin/collections/reconcile', {
            method: 'POST',
        }),

        /**
         * Delete a collection
         */
        delete: (name) => request(`/admin/collections/${encodeSegment(name)}`, {
            method: 'DELETE',
        }),
    },

    // ===== Collection Aliases =====
    aliases: {
        /**
         * List aliases on a cluster
         */
        list: ({ clusterName = 'default' } = {}) => {
            const params = new URLSearchParams({ cluster_name: clusterName });
            return request(`/admin/aliases?${params.toString()}`);
        },

        /**
         * Create or update an alias on a cluster
         */
        upsert: ({ aliasName, collectionName, clusterName = 'default' }) => {
            const params = new URLSearchParams({
                collection_name: collectionName,
                cluster_name: clusterName,
            });
            return request(`/admin/aliases/${encodeSegment(aliasName)}?${params.toString()}`, {
                method: 'PUT',
            });
        },

        /**
         * Delete an alias from a cluster
         */
        delete: ({ aliasName, clusterName = 'default' }) => {
            const params = new URLSearchParams({ cluster_name: clusterName });
            return request(`/admin/aliases/${encodeSegment(aliasName)}?${params.toString()}`, {
                method: 'DELETE',
            });
        },
    },

    // ===== Stats & Health =====
    stats: {
        /** Dashboard metrics summary */
        get: () => request('/admin/stats'),
    },
    health: {
        /** Service health (status, redis, kafka, clusters) */
        get: () => request('/health'),
    },

    // ===== Routing =====
    routing: {
        /**
         * Get complete routing map
         */
        getMap: () => request('/admin/routing-map'),

        /**
         * Set routing rules for a collection
         */
        setRules: (rules) => request('/admin/routing-rules', {
            method: 'POST',
            body: JSON.stringify(rules),
        }),

        /**
         * Delete routing rules for a collection
         */
        deleteRules: (collection) => request(`/admin/routing-rules/${encodeSegment(collection)}`, {
            method: 'DELETE',
        }),
    },

    // ===== Operations =====
    state: {
        /**
         * Export control-plane state. Secrets are masked unless explicitly requested.
         */
        exportSnapshot: ({ includeSecrets = false } = {}) => {
            const query = includeSecrets ? '?include_secrets=true' : '';
            return request(`/admin/state/export${query}`);
        },

        /**
         * Validate or apply a control-plane state snapshot.
         */
        importSnapshot: (snapshot, { apply = false } = {}) => {
            const query = apply ? '?apply=true' : '';
            return request(`/admin/state/import${query}`, {
                method: 'POST',
                body: JSON.stringify(snapshot),
            });
        },
    },

    audit: {
        /**
         * List recent sanitized admin audit events.
         */
        list: ({ limit = 25, action = '', resourceType = '' } = {}) => {
            const params = new URLSearchParams();
            if (limit) params.set('limit', String(limit));
            if (action) params.set('action', action);
            if (resourceType) params.set('resource_type', resourceType);
            const query = params.toString();
            return request(`/admin/audit-log${query ? `?${query}` : ''}`);
        },
    },

    // ===== Search & Ingestion =====
    search: {
        /**
         * Search a collection
         */
        query: (collection, params) => {
            const queryString = new URLSearchParams(params).toString();
            return request(`/search/${encodeSegment(collection)}?${queryString}`);
        },

        /**
         * Search a collection with a JSON body (preferred for vector/hybrid params)
         */
        queryAdvanced: (collection, params) => request(`/search/${encodeSegment(collection)}`, {
            method: 'POST',
            body: JSON.stringify(params),
        }),

        /**
         * Ingest a document
         */
        ingest: (collection, document) => request(`/ingest/${encodeSegment(collection)}`, {
            method: 'POST',
            body: JSON.stringify(document),
        }),
    },
};

export default api;
