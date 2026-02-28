/**
 * IMPOSBRO Search Admin - API Client
 * 
 * Centralized API client for all backend communication.
 * Provides consistent error handling and response formatting.
 */

const API_BASE = '/api';

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

    const config = {
        headers: {
            'Content-Type': 'application/json',
            ...options.headers,
        },
        ...options,
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
        delete: (name) => request(`/admin/federation/clusters/${name}`, {
            method: 'DELETE',
        }),
    },

    // ===== Collections =====
    collections: {
        /**
         * Get collection schema
         */
        get: (name) => request(`/admin/collections/${name}`),

        /**
         * Create a new collection
         */
        create: (schema) => request('/admin/collections', {
            method: 'POST',
            body: JSON.stringify(schema),
        }),

        /**
         * Delete a collection
         */
        delete: (name) => request(`/admin/collections/${name}`, {
            method: 'DELETE',
        }),
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
        deleteRules: (collection) => request(`/admin/routing-rules/${collection}`, {
            method: 'DELETE',
        }),
    },

    // ===== Search & Ingestion =====
    search: {
        /**
         * Search a collection
         */
        query: (collection, params) => {
            const queryString = new URLSearchParams(params).toString();
            return request(`/search/${collection}?${queryString}`);
        },

        /**
         * Ingest a document
         */
        ingest: (collection, document) => request(`/ingest/${collection}`, {
            method: 'POST',
            body: JSON.stringify(document),
        }),
    },
};

export default api;
