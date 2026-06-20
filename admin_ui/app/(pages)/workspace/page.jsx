'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Database,
  FileJson,
  Loader2,
  RefreshCw,
  Search,
  Trash2,
  UploadCloud,
} from 'lucide-react';
import { api } from '../../lib/api';
import { getSearchableFields } from '../../lib/searchFields';
import { useNotification, Notification } from '../../hooks/useNotification';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../../components/ui/Card';
import Button from '../../components/ui/Button';
import EmptyState from '../../components/ui/EmptyState';
import Input, { Checkbox, Select, Textarea } from '../../components/ui/Input';
import PageHeader from '../../components/ui/PageHeader';
import StatusBadge from '../../components/ui/StatusBadge';

const DEFAULT_LIMIT = 10;
const JSON_INDENT = 2;
const NUMERIC_SEARCH_PARAMS = [
  'offset',
  'limit',
  'remote_embedding_timeout_ms',
  'remote_embedding_num_tries',
  'limit_hits',
  'search_cutoff_ms',
  'max_candidates',
];

function buildSampleDocument(collectionName, fields) {
  const doc = { id: `${collectionName || 'doc'}-1` };
  fields.forEach((field) => {
    if (!field.name || field.name === 'id') return;
    if (field.type === 'string') doc[field.name] = `Sample ${field.name}`;
    else if (field.type === 'string[]') doc[field.name] = [`Sample ${field.name}`];
    else if (field.type === 'int32' || field.type === 'int64') doc[field.name] = 1;
    else if (field.type === 'int32[]') doc[field.name] = [1];
    else if (field.type === 'float') doc[field.name] = 1.0;
    else if (field.type === 'float[]') {
      const dimensions = Number(field.num_dim) || 3;
      doc[field.name] = Array.from({ length: dimensions }, () => 0.1);
    }
    else if (field.type === 'bool') doc[field.name] = true;
  });
  return doc;
}

function stringifyJson(value) {
  return JSON.stringify(value, null, JSON_INDENT);
}

function ResultDocument({ hit }) {
  const document = hit?.document || {};
  const entries = Object.entries(document);
  const displayId = document.id || 'unknown';

  return (
    <div className="rounded-lg border border-border bg-muted/20 p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate font-semibold text-foreground">{displayId}</p>
          {hit?.text_match !== undefined && (
            <p className="text-xs text-muted-foreground">text match {hit.text_match}</p>
          )}
          {hit?.vector_distance !== undefined && (
            <p className="text-xs text-muted-foreground">vector distance {hit.vector_distance}</p>
          )}
        </div>
        <StatusBadge variant="info">{entries.length} fields</StatusBadge>
      </div>
      <pre className="max-h-64 overflow-auto rounded-md border border-border bg-background p-3 text-xs leading-relaxed text-muted-foreground">
        {stringifyJson(document)}
      </pre>
    </div>
  );
}

function MetadataItem({ label, value, variant = 'default' }) {
  return (
    <div className="rounded-md border border-border bg-muted/20 px-3 py-2">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className={variant === 'warning' ? 'text-sm font-semibold text-amber-400' : 'text-sm font-semibold text-foreground'}>
        {value}
      </p>
    </div>
  );
}

export default function WorkspacePage() {
  const [collections, setCollections] = useState([]);
  const [selectedCollection, setSelectedCollection] = useState('');
  const [schema, setSchema] = useState(null);
  const [isLoadingCollections, setIsLoadingCollections] = useState(true);
  const [isLoadingSchema, setIsLoadingSchema] = useState(false);
  const [isIngesting, setIsIngesting] = useState(false);
  const [isLoadingDocument, setIsLoadingDocument] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [isSearching, setIsSearching] = useState(false);
  const [documentJson, setDocumentJson] = useState('{}');
  const [ingestResult, setIngestResult] = useState(null);
  const [deleteDocumentId, setDeleteDocumentId] = useState('');
  const [documentReadResult, setDocumentReadResult] = useState(null);
  const [deleteResult, setDeleteResult] = useState(null);
  const [searchParams, setSearchParams] = useState({
    q: '',
    query_by: '',
    filter_by: '',
    sort_by: '',
    vector_query: '',
    query_by_weights: '',
    include_fields: '',
    exclude_fields: '',
    highlight_fields: '',
    highlight_full_fields: '',
    highlight_start_tag: '',
    highlight_end_tag: '',
    remote_embedding_timeout_ms: '',
    remote_embedding_num_tries: '',
    limit_hits: '',
    search_cutoff_ms: '',
    max_candidates: '',
    exhaustive_search: false,
    offset: 0,
    limit: DEFAULT_LIMIT,
  });
  const [searchResult, setSearchResult] = useState(null);
  const { notification, showSuccess, showError, showInfo } = useNotification();

  const fields = useMemo(() => schema?.fields || [], [schema]);
  const searchableFields = useMemo(() => getSearchableFields(fields), [fields]);

  const fetchCollections = useCallback(async () => {
    setIsLoadingCollections(true);
    try {
      const data = await api.routing.getMap();
      const names = Object.keys(data.collections || {});
      setCollections(names);
      setSelectedCollection((previous) => previous || names[0] || '');
    } catch (err) {
      showError(err.message);
    } finally {
      setIsLoadingCollections(false);
    }
  }, [showError]);

  useEffect(() => {
    fetchCollections();
  }, [fetchCollections]);

  useEffect(() => {
    if (!selectedCollection) {
      setSchema(null);
      setDocumentJson('{}');
      setSearchParams((previous) => ({ ...previous, query_by: '' }));
      return;
    }

    let cancelled = false;
    setIsLoadingSchema(true);
    setIngestResult(null);
    setDocumentReadResult(null);
    setDeleteResult(null);
    setDeleteDocumentId('');
    setSearchResult(null);
    api.collections.get(selectedCollection)
      .then((data) => {
        if (cancelled) return;
        const nextFields = data.fields || [];
        const nextSearchableFields = getSearchableFields(nextFields);
        setSchema(data);
        setDocumentJson(stringifyJson(buildSampleDocument(selectedCollection, nextFields)));
        setSearchParams((previous) => ({
          ...previous,
          query_by: nextSearchableFields.join(','),
          q: previous.q || '*',
        }));
      })
      .catch((err) => {
        if (!cancelled) {
          setSchema(null);
          showError(err.message);
        }
      })
      .finally(() => {
        if (!cancelled) setIsLoadingSchema(false);
      });

    return () => {
      cancelled = true;
    };
  }, [selectedCollection, showError]);

  const handleSearchParamChange = (field) => (event) => {
    const value = field === 'exhaustive_search'
      ? event.target.checked
      : event.target.value;
    setSearchParams((previous) => ({
      ...previous,
      [field]: NUMERIC_SEARCH_PARAMS.includes(field) && value !== ''
        ? Number(value)
        : value,
    }));
  };

  const resetSampleDocument = () => {
    setDocumentJson(stringifyJson(buildSampleDocument(selectedCollection, fields)));
    showInfo('Sample document reset from the selected schema.');
  };

  const handleIngest = async (event) => {
    event.preventDefault();
    if (!selectedCollection) {
      showError('Select a collection before ingesting a document.');
      return;
    }

    let document;
    try {
      document = JSON.parse(documentJson);
    } catch (err) {
      showError(`Invalid JSON: ${err.message}`);
      return;
    }

    if (!document || typeof document !== 'object' || Array.isArray(document)) {
      showError('Document payload must be a JSON object.');
      return;
    }
    if (!document.id) {
      showError('Document payload must include an id field.');
      return;
    }

    setIsIngesting(true);
    try {
      const result = await api.search.ingest(selectedCollection, document);
      setIngestResult(result);
      setDeleteDocumentId(result.document_id);
      showSuccess(`Document ${result.document_id} routed to ${result.routed_to}.`);
    } catch (err) {
      showError(err.message);
    } finally {
      setIsIngesting(false);
    }
  };

  const handleDeleteDocument = async (event) => {
    event.preventDefault();
    if (!selectedCollection) {
      showError('Select a collection before deleting a document.');
      return;
    }

    const documentId = deleteDocumentId.trim();
    if (!documentId) {
      showError('Document ID is required.');
      return;
    }

    setIsDeleting(true);
    try {
      const result = await api.search.deleteDocument(selectedCollection, documentId);
      setDeleteResult(result);
      showSuccess(`Document ${result.document_id} queued for deletion.`);
    } catch (err) {
      showError(err.message);
    } finally {
      setIsDeleting(false);
    }
  };

  const handleLoadDocument = async () => {
    if (!selectedCollection) {
      showError('Select a collection before loading a document.');
      return;
    }

    const documentId = deleteDocumentId.trim();
    if (!documentId) {
      showError('Document ID is required.');
      return;
    }

    setIsLoadingDocument(true);
    try {
      const result = await api.search.getDocument(selectedCollection, documentId);
      setDocumentReadResult(result);
      setDeleteResult(null);
      setDocumentJson(stringifyJson(result.document));
      showSuccess(`Document ${result.document_id} loaded from ${result.found_in}.`);
    } catch (err) {
      showError(err.message);
    } finally {
      setIsLoadingDocument(false);
    }
  };

  const executeSearch = async (offsetOverride = 0) => {
    if (!selectedCollection) {
      showError('Select a collection before searching.');
      return;
    }
    const hasVectorQuery = Boolean(searchParams.vector_query.trim());
    if (!searchParams.q.trim() || (!searchParams.query_by.trim() && !hasVectorQuery)) {
      showError('Search query and query_by or vector_query are required.');
      return;
    }

    const params = {
      q: searchParams.q.trim(),
      offset: offsetOverride,
      limit: searchParams.limit || DEFAULT_LIMIT,
    };
    if (searchParams.query_by.trim()) params.query_by = searchParams.query_by.trim();
    if (searchParams.filter_by.trim()) params.filter_by = searchParams.filter_by.trim();
    if (searchParams.sort_by.trim()) params.sort_by = searchParams.sort_by.trim();
    if (searchParams.vector_query.trim()) params.vector_query = searchParams.vector_query.trim();
    if (searchParams.query_by_weights.trim()) params.query_by_weights = searchParams.query_by_weights.trim();
    if (searchParams.include_fields.trim()) params.include_fields = searchParams.include_fields.trim();
    if (searchParams.exclude_fields.trim()) params.exclude_fields = searchParams.exclude_fields.trim();
    if (searchParams.highlight_fields.trim()) params.highlight_fields = searchParams.highlight_fields.trim();
    if (searchParams.highlight_full_fields.trim()) {
      params.highlight_full_fields = searchParams.highlight_full_fields.trim();
    }
    if (searchParams.highlight_start_tag.trim()) {
      params.highlight_start_tag = searchParams.highlight_start_tag.trim();
    }
    if (searchParams.highlight_end_tag.trim()) {
      params.highlight_end_tag = searchParams.highlight_end_tag.trim();
    }
    if (searchParams.remote_embedding_timeout_ms) {
      params.remote_embedding_timeout_ms = Number(searchParams.remote_embedding_timeout_ms);
    }
    if (searchParams.remote_embedding_num_tries) {
      params.remote_embedding_num_tries = Number(searchParams.remote_embedding_num_tries);
    }
    if (searchParams.limit_hits) params.limit_hits = Number(searchParams.limit_hits);
    if (searchParams.search_cutoff_ms) params.search_cutoff_ms = Number(searchParams.search_cutoff_ms);
    if (searchParams.max_candidates) params.max_candidates = Number(searchParams.max_candidates);
    if (searchParams.exhaustive_search) params.exhaustive_search = true;

    setIsSearching(true);
    try {
      const result = await api.search.queryAdvanced(selectedCollection, params);
      setSearchResult(result);
      setSearchParams((previous) => ({ ...previous, offset: offsetOverride }));
      if (result.partial) {
        showInfo(`Search completed with ${result.failed_clusters?.length || 0} failed cluster(s).`);
      } else {
        showSuccess(`Search returned ${result.hits?.length || 0} hit(s).`);
      }
    } catch (err) {
      showError(err.message);
    } finally {
      setIsSearching(false);
    }
  };

  const handleSearch = async (event) => {
    event.preventDefault();
    await executeSearch(Number(searchParams.offset || 0));
  };

  const handlePreviousPage = () => {
    const currentOffset = Number(searchResult?.offset ?? searchParams.offset ?? 0);
    const pageSize = Number(searchParams.limit || DEFAULT_LIMIT);
    executeSearch(Math.max(0, currentOffset - pageSize));
  };

  const handleNextPage = () => {
    const nextOffset = Number(searchResult?.next_offset);
    if (Number.isFinite(nextOffset)) executeSearch(nextOffset);
  };

  const hasCollections = collections.length > 0;
  const hits = searchResult?.hits || [];

  return (
    <div>
      <PageHeader
        title="Search Workspace"
        description="Ingest, delete, and search documents in existing collections."
        action={
          <Button
            type="button"
            variant="outline"
            leftIcon={<RefreshCw size={16} />}
            onClick={fetchCollections}
            disabled={isLoadingCollections}
          >
            Refresh
          </Button>
        }
      />

      {notification && (
        <div className="mb-6">
          <Notification {...notification} />
        </div>
      )}

      {!hasCollections && !isLoadingCollections ? (
        <Card>
          <EmptyState
            icon={<Database size={48} />}
            title="No collections available"
            description="Create a collection before using the workspace."
          />
        </Card>
      ) : (
        <div className="space-y-6">
          <Card>
            <div className="grid gap-4 md:grid-cols-[minmax(220px,320px),1fr]">
              <Select
                label="Collection"
                value={selectedCollection}
                onChange={(event) => setSelectedCollection(event.target.value)}
                disabled={isLoadingCollections || isLoadingSchema}
              >
                {collections.map((collection) => (
                  <option key={collection} value={collection}>
                    {collection}
                  </option>
                ))}
              </Select>
              <div className="flex flex-wrap items-end gap-2">
                {isLoadingSchema ? (
                  <StatusBadge variant="info">
                    <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                    Loading schema
                  </StatusBadge>
                ) : fields.length > 0 ? (
                  fields.map((field) => (
                    <StatusBadge key={`${field.name}-${field.type}`} variant={field.facet ? 'purple' : 'default'}>
                      {field.name}:{field.type}
                    </StatusBadge>
                  ))
                ) : (
                  <StatusBadge variant="warning">No schema fields</StatusBadge>
                )}
              </div>
            </div>
          </Card>

          <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
            <Card>
              <CardHeader className="px-0 pt-0">
                <CardTitle className="flex items-center gap-2 text-lg">
                  <UploadCloud className="h-5 w-5 text-primary" />
                  Document data
                </CardTitle>
                <CardDescription>
                  Submit, load, or delete one document in the selected collection.
                </CardDescription>
              </CardHeader>
              <CardContent className="px-0 pb-0">
                <form onSubmit={handleIngest} className="space-y-4">
                  <Textarea
                    aria-label="Document JSON"
                    className="min-h-72 font-mono text-xs leading-relaxed"
                    value={documentJson}
                    onChange={(event) => setDocumentJson(event.target.value)}
                    spellCheck={false}
                  />
                  <div className="flex flex-col gap-3 sm:flex-row">
                    <Button
                      type="button"
                      variant="outline"
                      leftIcon={<FileJson size={16} />}
                      onClick={resetSampleDocument}
                      disabled={!selectedCollection || isLoadingSchema}
                    >
                      Reset sample
                    </Button>
                    <Button
                      type="submit"
                      className="sm:ml-auto"
                      leftIcon={<UploadCloud size={16} />}
                      loading={isIngesting}
                      disabled={!selectedCollection || isLoadingSchema}
                    >
                      Ingest
                    </Button>
                  </div>
                  {ingestResult && (
                    <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 p-3 text-sm text-emerald-300">
                      <CheckCircle2 className="mr-2 inline h-4 w-4" />
                      {ingestResult.document_id} routed to {ingestResult.routed_to}
                    </div>
                  )}
                </form>
                <form
                  onSubmit={handleDeleteDocument}
                  className="mt-5 space-y-3 border-t border-border pt-4"
                >
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
                    <Input
                      label="Document ID"
                      value={deleteDocumentId}
                      onChange={(event) => {
                        setDeleteDocumentId(event.target.value);
                        setDocumentReadResult(null);
                        setDeleteResult(null);
                      }}
                      placeholder="product-123"
                    />
                    <Button
                      type="button"
                      variant="outline"
                      leftIcon={<FileJson size={16} />}
                      loading={isLoadingDocument}
                      disabled={!selectedCollection || isLoadingSchema}
                      onClick={handleLoadDocument}
                    >
                      Load
                    </Button>
                    <Button
                      type="submit"
                      variant="destructive"
                      leftIcon={<Trash2 size={16} />}
                      loading={isDeleting}
                      disabled={!selectedCollection || isLoadingSchema}
                    >
                      Delete
                    </Button>
                  </div>
                  {documentReadResult && (
                    <div className="rounded-lg border border-sky-500/40 bg-sky-500/10 p-3 text-sm text-sky-200">
                      <FileJson className="mr-2 inline h-4 w-4" />
                      {documentReadResult.document_id} loaded from {documentReadResult.found_in}
                    </div>
                  )}
                  {deleteResult && (
                    <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-200">
                      <Trash2 className="mr-2 inline h-4 w-4" />
                      {deleteResult.document_id} queued on {deleteResult.routed_to}
                    </div>
                  )}
                </form>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="px-0 pt-0">
                <CardTitle className="flex items-center gap-2 text-lg">
                  <Search className="h-5 w-5 text-primary" />
                  Federated search
                </CardTitle>
                <CardDescription>
                  Query the selected collection across available clusters.
                </CardDescription>
              </CardHeader>
              <CardContent className="px-0 pb-0">
                <form onSubmit={handleSearch} className="space-y-4">
                  <div className="grid gap-4 md:grid-cols-2">
                    <Input
                      label="Query"
                      placeholder="* or product name"
                      value={searchParams.q}
                      onChange={handleSearchParamChange('q')}
                    />
                    <Input
                      label="query_by"
                      placeholder={searchableFields.join(',') || 'title'}
                      value={searchParams.query_by}
                      onChange={handleSearchParamChange('query_by')}
                    />
                    <Input
                      label="filter_by"
                      placeholder="brand:=acme"
                      value={searchParams.filter_by}
                      onChange={handleSearchParamChange('filter_by')}
                    />
                    <Input
                      label="sort_by"
                      placeholder="rank:desc,_vector_distance:asc"
                      value={searchParams.sort_by}
                      onChange={handleSearchParamChange('sort_by')}
                    />
                    <Input
                      label="query_by_weights"
                      placeholder="2,1,0"
                      value={searchParams.query_by_weights}
                      onChange={handleSearchParamChange('query_by_weights')}
                    />
                    <Input
                      label="include_fields"
                      placeholder="id,title,brand"
                      value={searchParams.include_fields}
                      onChange={handleSearchParamChange('include_fields')}
                    />
                    <Input
                      label="exclude_fields"
                      placeholder="embedding"
                      value={searchParams.exclude_fields}
                      onChange={handleSearchParamChange('exclude_fields')}
                    />
                    <Input
                      label="highlight_fields"
                      placeholder="title,description"
                      value={searchParams.highlight_fields}
                      onChange={handleSearchParamChange('highlight_fields')}
                    />
                    <Input
                      label="highlight_full_fields"
                      placeholder="description"
                      value={searchParams.highlight_full_fields}
                      onChange={handleSearchParamChange('highlight_full_fields')}
                    />
                  </div>
                  <Textarea
                    label="vector_query"
                    placeholder="embedding:([0.1,0.2,0.3], k:10, alpha: 0.8)"
                    className="min-h-24 font-mono text-xs leading-relaxed"
                    value={searchParams.vector_query}
                    onChange={handleSearchParamChange('vector_query')}
                    spellCheck={false}
                  />
                  <div className="grid gap-4 md:grid-cols-2">
                    <Input
                      label="highlight_start_tag"
                      placeholder="<mark>"
                      value={searchParams.highlight_start_tag}
                      onChange={handleSearchParamChange('highlight_start_tag')}
                    />
                    <Input
                      label="highlight_end_tag"
                      placeholder="</mark>"
                      value={searchParams.highlight_end_tag}
                      onChange={handleSearchParamChange('highlight_end_tag')}
                    />
                  </div>
                  <div className="flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-end">
                    <Input
                      className="sm:max-w-32"
                      label="Offset"
                      type="number"
                      min="0"
                      value={searchParams.offset}
                      onChange={handleSearchParamChange('offset')}
                    />
                    <Input
                      className="sm:max-w-32"
                      label="Limit"
                      type="number"
                      min="1"
                      max="250"
                      value={searchParams.limit}
                      onChange={handleSearchParamChange('limit')}
                    />
                    <Input
                      className="sm:max-w-44"
                      label="Embed timeout ms"
                      type="number"
                      min="1"
                      value={searchParams.remote_embedding_timeout_ms}
                      onChange={handleSearchParamChange('remote_embedding_timeout_ms')}
                    />
                    <Input
                      className="sm:max-w-36"
                      label="Embed tries"
                      type="number"
                      min="1"
                      value={searchParams.remote_embedding_num_tries}
                      onChange={handleSearchParamChange('remote_embedding_num_tries')}
                    />
                    <Input
                      className="sm:max-w-36"
                      label="limit_hits"
                      type="number"
                      min="1"
                      value={searchParams.limit_hits}
                      onChange={handleSearchParamChange('limit_hits')}
                    />
                    <Input
                      className="sm:max-w-40"
                      label="Cutoff ms"
                      type="number"
                      min="1"
                      value={searchParams.search_cutoff_ms}
                      onChange={handleSearchParamChange('search_cutoff_ms')}
                    />
                    <Input
                      className="sm:max-w-44"
                      label="Max candidates"
                      type="number"
                      min="1"
                      value={searchParams.max_candidates}
                      onChange={handleSearchParamChange('max_candidates')}
                    />
                    <div className="flex h-10 items-center">
                      <Checkbox
                        label="Exhaustive search"
                        checked={searchParams.exhaustive_search}
                        onChange={handleSearchParamChange('exhaustive_search')}
                      />
                    </div>
                    <Button
                      type="submit"
                      className="sm:ml-auto"
                      leftIcon={<Search size={16} />}
                      loading={isSearching}
                      disabled={!selectedCollection || isLoadingSchema}
                    >
                      Search
                    </Button>
                  </div>
                </form>

                {searchResult && (
                  <div className="mt-6 space-y-4">
                    <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                      <MetadataItem label="Found" value={searchResult.found ?? 0} />
                      <MetadataItem label="Returned" value={hits.length} />
                      <MetadataItem label="Offset" value={searchResult.offset ?? 0} />
                      <MetadataItem label="Next" value={searchResult.next_offset ?? 'End'} />
                      <MetadataItem label="Responded" value={`${searchResult.clusters_responded ?? 0}/${searchResult.clusters_queried ?? 0}`} />
                      <MetadataItem
                        label="Partial"
                        value={searchResult.partial ? 'Yes' : 'No'}
                        variant={searchResult.partial ? 'warning' : 'default'}
                      />
                    </div>
                    {searchResult.partial && (
                      <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-300">
                        <AlertTriangle className="mr-2 inline h-4 w-4" />
                        Failed clusters: {(searchResult.failed_clusters || []).join(', ') || 'unknown'}
                      </div>
                    )}
                    <div className="flex flex-col gap-3 sm:flex-row">
                      <Button
                        type="button"
                        variant="outline"
                        onClick={handlePreviousPage}
                        disabled={isSearching || Number(searchResult.offset || 0) <= 0}
                      >
                        Previous
                      </Button>
                      <Button
                        type="button"
                        variant="outline"
                        className="sm:ml-auto"
                        onClick={handleNextPage}
                        disabled={isSearching || !searchResult.has_more || searchResult.next_offset === null}
                      >
                        Next
                      </Button>
                    </div>
                    {hits.length > 0 ? (
                      <div className="space-y-3">
                        {hits.map((hit, index) => (
                          <ResultDocument key={`${hit?.document?.id || 'hit'}-${index}`} hit={hit} />
                        ))}
                      </div>
                    ) : (
                      <EmptyState
                        icon={<Search size={44} />}
                        title="No hits returned"
                        description="Adjust the query, query_by fields, or wait for asynchronous indexing to complete."
                        className="py-8"
                      />
                    )}
                  </div>
                )}
              </CardContent>
            </Card>
          </div>
        </div>
      )}
    </div>
  );
}
