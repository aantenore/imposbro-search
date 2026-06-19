'use client';

import { useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  ArchiveRestore,
  CheckCircle2,
  Download,
  FileCheck2,
  FileJson,
  FileUp,
  ShieldAlert,
  ShieldCheck,
  Upload,
} from 'lucide-react';
import { api } from '../../lib/api';
import { useNotification, Notification } from '../../hooks/useNotification';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../../components/ui/Card';
import Button from '../../components/ui/Button';
import { Checkbox, Textarea } from '../../components/ui/Input';
import PageHeader from '../../components/ui/PageHeader';
import StatusBadge from '../../components/ui/StatusBadge';

const JSON_INDENT = 2;
const SNAPSHOT_VERSION = 'imposbro.state.v1';

function stringifyJson(value) {
  return JSON.stringify(value, null, JSON_INDENT);
}

function parseSnapshot(text) {
  const parsed = JSON.parse(text);
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('Snapshot payload must be a JSON object.');
  }
  return parsed;
}

function countKeys(value) {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? Object.keys(value).length
    : 0;
}

function getSnapshotCounts(snapshot) {
  return {
    clusters: countKeys(snapshot?.federation_clusters_config),
    routingRules: countKeys(snapshot?.collection_routing_rules),
    collectionSchemas: countKeys(snapshot?.collection_schemas),
  };
}

function buildSnapshotFilename(snapshot) {
  const sourceDate = snapshot?.exported_at ? new Date(snapshot.exported_at) : new Date();
  const date = Number.isNaN(sourceDate.getTime()) ? new Date() : sourceDate;
  const stamp = date.toISOString().replace(/[:.]/g, '-');
  const suffix = snapshot?.secrets_included ? 'restore-ready' : 'masked';
  return `imposbro-state-${suffix}-${stamp}.json`;
}

function downloadSnapshot(snapshot) {
  const blob = new Blob([stringifyJson(snapshot)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = buildSnapshotFilename(snapshot);
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function SummaryTile({ label, value, tone = 'default' }) {
  const valueClassName = tone === 'warning'
    ? 'text-amber-400'
    : tone === 'success'
      ? 'text-emerald-400'
      : 'text-foreground';

  return (
    <div className="rounded-md border border-border bg-muted/20 px-3 py-2">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className={`text-sm font-semibold ${valueClassName}`}>{value}</p>
    </div>
  );
}

function SnapshotSummary({ snapshot }) {
  if (!snapshot) {
    return (
      <div className="rounded-lg border border-dashed border-border bg-muted/10 p-4 text-sm text-muted-foreground">
        No snapshot loaded.
      </div>
    );
  }

  const counts = getSnapshotCounts(snapshot);
  const secretsIncluded = Boolean(snapshot.secrets_included);
  const versionMatches = snapshot.version === SNAPSHOT_VERSION;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        <StatusBadge variant={versionMatches ? 'success' : 'warning'}>
          {snapshot.version || 'missing version'}
        </StatusBadge>
        <StatusBadge variant={secretsIncluded ? 'warning' : 'success'}>
          {secretsIncluded ? 'Raw secrets' : 'Masked secrets'}
        </StatusBadge>
        {snapshot.exported_at && (
          <StatusBadge variant="info">
            {new Date(snapshot.exported_at).toLocaleString()}
          </StatusBadge>
        )}
      </div>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <SummaryTile label="Clusters" value={counts.clusters} />
        <SummaryTile label="Routing rules" value={counts.routingRules} />
        <SummaryTile label="Schemas" value={counts.collectionSchemas} />
      </div>
    </div>
  );
}

function ImportResult({ result, validationIsCurrent }) {
  if (!result) return null;

  const counts = result.counts || {};
  const importable = result.importable !== false;
  const dryRun = Boolean(result.dry_run);

  return (
    <div className="space-y-3 rounded-lg border border-border bg-muted/20 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge variant={dryRun ? 'info' : 'success'}>
          {dryRun ? 'Dry-run' : 'Applied'}
        </StatusBadge>
        <StatusBadge variant={importable && validationIsCurrent ? 'success' : 'warning'}>
          {validationIsCurrent ? (importable ? 'Importable' : 'Masked secrets') : 'Stale validation'}
        </StatusBadge>
      </div>
      {result.message && (
        <p className="text-sm text-muted-foreground">{result.message}</p>
      )}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <SummaryTile label="Clusters" value={counts.clusters ?? 0} />
        <SummaryTile label="Routing rules" value={counts.routing_rules ?? 0} />
        <SummaryTile label="Schemas" value={counts.collection_schemas ?? 0} />
      </div>
      {!importable && (
        <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-300">
          <AlertTriangle className="mr-2 inline h-4 w-4" />
          Apply requires a restore-ready snapshot with raw cluster API keys.
        </div>
      )}
    </div>
  );
}

export default function OperationsPage() {
  const [exportMode, setExportMode] = useState(null);
  const [lastExport, setLastExport] = useState(null);
  const [exportText, setExportText] = useState('');
  const [includeSecretsConfirmed, setIncludeSecretsConfirmed] = useState(false);

  const [importText, setImportText] = useState('');
  const [importResult, setImportResult] = useState(null);
  const [validatedSnapshotText, setValidatedSnapshotText] = useState('');
  const [applyConfirmed, setApplyConfirmed] = useState(false);
  const [isValidating, setIsValidating] = useState(false);
  const [isApplying, setIsApplying] = useState(false);
  const fileInputRef = useRef(null);
  const { notification, showSuccess, showError, showInfo } = useNotification();

  const importPreview = useMemo(() => {
    if (!importText.trim()) return null;
    try {
      return parseSnapshot(importText);
    } catch (_) {
      return null;
    }
  }, [importText]);

  const validationIsCurrent = Boolean(
    validatedSnapshotText && validatedSnapshotText === importText.trim()
  );
  const canApply = Boolean(
    validationIsCurrent &&
      importResult?.importable !== false &&
      applyConfirmed &&
      !isValidating &&
      !isApplying
  );

  const handleExport = async (includeSecrets) => {
    if (includeSecrets && !includeSecretsConfirmed) {
      showError('Confirm restore-ready export before including raw secrets.');
      return;
    }

    setExportMode(includeSecrets ? 'secrets' : 'masked');
    try {
      const snapshot = await api.state.exportSnapshot({ includeSecrets });
      setLastExport(snapshot);
      setExportText(stringifyJson(snapshot));
      showSuccess(includeSecrets ? 'Restore-ready snapshot exported.' : 'Masked snapshot exported.');
    } catch (err) {
      showError(err.message);
    } finally {
      setExportMode(null);
    }
  };

  const handleDownload = () => {
    if (!lastExport) {
      showError('Export a snapshot before downloading.');
      return;
    }
    downloadSnapshot(lastExport);
    showInfo('Snapshot download started.');
  };

  const handleImportTextChange = (event) => {
    setImportText(event.target.value);
    setImportResult(null);
    setValidatedSnapshotText('');
    setApplyConfirmed(false);
  };

  const handleChooseFile = () => {
    fileInputRef.current?.click();
  };

  const handleFileSelected = (event) => {
    const file = event.target.files?.[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onload = () => {
      setImportText(String(reader.result || ''));
      setImportResult(null);
      setValidatedSnapshotText('');
      setApplyConfirmed(false);
      showInfo(`Loaded ${file.name}.`);
    };
    reader.onerror = () => showError(`Could not read ${file.name}.`);
    reader.readAsText(file);
    event.target.value = '';
  };

  const handleValidate = async () => {
    const trimmed = importText.trim();
    if (!trimmed) {
      showError('Paste or upload a snapshot before validating.');
      return;
    }

    let snapshot;
    try {
      snapshot = parseSnapshot(trimmed);
    } catch (err) {
      showError(`Invalid JSON: ${err.message}`);
      return;
    }

    setIsValidating(true);
    try {
      const result = await api.state.importSnapshot(snapshot);
      setImportResult(result);
      setValidatedSnapshotText(trimmed);
      if (result.importable === false) {
        showInfo('Snapshot is valid, but masked secrets prevent apply.');
      } else {
        showSuccess('Snapshot validation passed.');
      }
    } catch (err) {
      setImportResult(null);
      setValidatedSnapshotText('');
      showError(err.message);
    } finally {
      setIsValidating(false);
    }
  };

  const handleApply = async () => {
    const trimmed = importText.trim();
    if (!validationIsCurrent) {
      showError('Validate the current snapshot before applying it.');
      return;
    }
    if (importResult?.importable === false) {
      showError('This snapshot cannot be applied while secrets are masked.');
      return;
    }
    if (!applyConfirmed) {
      showError('Confirm apply before importing state.');
      return;
    }

    let snapshot;
    try {
      snapshot = parseSnapshot(trimmed);
    } catch (err) {
      showError(`Invalid JSON: ${err.message}`);
      return;
    }

    setIsApplying(true);
    try {
      const result = await api.state.importSnapshot(snapshot, { apply: true });
      setImportResult(result);
      setValidatedSnapshotText(trimmed);
      setApplyConfirmed(false);
      showSuccess(result.message || 'State snapshot imported.');
    } catch (err) {
      showError(err.message);
    } finally {
      setIsApplying(false);
    }
  };

  return (
    <div>
      <PageHeader
        title="Operations"
        description="Export, validate, and restore the Query API control-plane state."
      />

      {notification && (
        <div className="mb-6">
          <Notification {...notification} />
        </div>
      )}

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-lg">
              <Download className="h-5 w-5 text-primary" />
              Export state
            </CardTitle>
            <CardDescription>
              Create an inspectable snapshot or a restore-ready backup.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            <div className="grid gap-3 sm:grid-cols-2">
              <Button
                type="button"
                variant="outline"
                leftIcon={<ShieldCheck size={16} />}
                loading={exportMode === 'masked'}
                disabled={Boolean(exportMode)}
                onClick={() => handleExport(false)}
              >
                Export masked
              </Button>
              <Button
                type="button"
                variant="secondary"
                leftIcon={<ShieldAlert size={16} />}
                loading={exportMode === 'secrets'}
                disabled={Boolean(exportMode) || !includeSecretsConfirmed}
                onClick={() => handleExport(true)}
              >
                Export with secrets
              </Button>
            </div>

            <Checkbox
              checked={includeSecretsConfirmed}
              onChange={(event) => setIncludeSecretsConfirmed(event.target.checked)}
              label="I need a restore-ready file with raw cluster API keys."
            />

            <SnapshotSummary snapshot={lastExport} />

            <Textarea
              label="Exported snapshot"
              aria-label="Exported snapshot JSON"
              className="min-h-96 font-mono text-xs leading-relaxed"
              value={exportText}
              readOnly
              spellCheck={false}
              placeholder="Exported JSON appears here."
            />

            <Button
              type="button"
              leftIcon={<Download size={16} />}
              disabled={!lastExport}
              onClick={handleDownload}
            >
              Download JSON
            </Button>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-lg">
              <ArchiveRestore className="h-5 w-5 text-primary" />
              Import state
            </CardTitle>
            <CardDescription>
              Validate a snapshot before replacing runtime and persisted state.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            <div className="flex flex-col gap-3 sm:flex-row">
              <Button
                type="button"
                variant="outline"
                leftIcon={<FileUp size={16} />}
                onClick={handleChooseFile}
              >
                Load file
              </Button>
              <input
                ref={fileInputRef}
                type="file"
                accept="application/json,.json"
                className="hidden"
                onChange={handleFileSelected}
              />
              <Button
                type="button"
                className="sm:ml-auto"
                leftIcon={<FileCheck2 size={16} />}
                loading={isValidating}
                disabled={!importText.trim() || isApplying}
                onClick={handleValidate}
              >
                Validate
              </Button>
            </div>

            <Textarea
              label="Snapshot JSON"
              aria-label="Snapshot JSON to import"
              className="min-h-96 font-mono text-xs leading-relaxed"
              value={importText}
              onChange={handleImportTextChange}
              spellCheck={false}
              placeholder="Paste or load an IMPOSBRO state snapshot."
            />

            <SnapshotSummary snapshot={importPreview} />
            <ImportResult result={importResult} validationIsCurrent={validationIsCurrent} />

            <div className="space-y-3 rounded-lg border border-destructive/30 bg-destructive/10 p-4">
              <Checkbox
                checked={applyConfirmed}
                onChange={(event) => setApplyConfirmed(event.target.checked)}
                label="I confirm this snapshot should replace the current control-plane state."
              />
              <Button
                type="button"
                variant="danger"
                leftIcon={<Upload size={16} />}
                loading={isApplying}
                disabled={!canApply}
                onClick={handleApply}
              >
                Apply import
              </Button>
            </div>

            {validationIsCurrent && importResult?.importable !== false && importResult?.dry_run && (
              <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 p-3 text-sm text-emerald-300">
                <CheckCircle2 className="mr-2 inline h-4 w-4" />
                Current snapshot is validated and ready to apply.
              </div>
            )}

            {!importText.trim() && (
              <div className="rounded-lg border border-dashed border-border bg-muted/10 p-4 text-sm text-muted-foreground">
                <FileJson className="mr-2 inline h-4 w-4" />
                Waiting for snapshot JSON.
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
