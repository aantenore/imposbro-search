export const DEFAULT_CLUSTER_PROTOCOL = 'http';

export function createEmptyClusterForm() {
    return {
        name: '',
        protocol: DEFAULT_CLUSTER_PROTOCOL,
        host: '',
        port: 8108,
        api_key: '',
    };
}

export function formatClusterEndpoint(cluster) {
    const protocol = cluster.protocol || DEFAULT_CLUSTER_PROTOCOL;
    return `${protocol}://${cluster.host}:${cluster.port}`;
}
