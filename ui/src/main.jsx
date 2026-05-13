import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import dagre from '@dagrejs/dagre';
import {
  BookOpen,
  Boxes,
  Cable,
  ChevronDown,
  ChevronRight,
  Copy,
  Database,
  KeyRound,
  Network,
  RefreshCw,
  Search,
  Terminal,
} from 'lucide-react';
import './styles.css';

const KIND_LABELS = {
  static_artifact: 'Static artifact',
  workflow: 'Workflow',
  runtime_certificate: 'Certificate',
  runtime_artifact: 'Runtime artifact',
};

const SCREENS = [
  { id: 'workflows', label: 'Workflows', shortLabel: 'Flows', icon: Network, kinds: ['workflow'] },
  { id: 'artifacts', label: 'Artifacts', shortLabel: 'Data', icon: Boxes, kinds: ['static_artifact', 'runtime_artifact'] },
  { id: 'certificates', label: 'Certificates', shortLabel: 'Certs', icon: KeyRound, kinds: ['runtime_certificate'] },
  { id: 'glossary', label: 'Glossary', shortLabel: 'Terms', icon: BookOpen, kinds: [] },
];

const WORKFLOW_TABS = [
  ['definition', 'Definition'],
  ['compose', 'Compose'],
  ['diagram', 'Diagram'],
  ['raw-workflow', 'Raw workflow'],
  ['raw-compose', 'Raw compose'],
];

function App() {
  const initialRoute = readRoute();
  const [summary, setSummary] = useState(null);
  const [objects, setObjects] = useState([]);
  const [glossary, setGlossary] = useState([]);
  const [selected, setSelected] = useState(null);
  const [selectedPath, setSelectedPath] = useState(initialRoute.objectPath);
  const [screen, setScreen] = useState(initialRoute.screen);
  const [selectedTermId, setSelectedTermId] = useState(initialRoute.termId);
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const refresh = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [summaryResponse, objectResponse, glossaryResponse] = await Promise.all([
        fetch('/ui-api/summary'),
        fetch('/ui-api/objects?limit=1000'),
        fetch('/ui-api/glossary'),
      ]);
      if (!summaryResponse.ok || !objectResponse.ok || !glossaryResponse.ok) {
        throw new Error('CoveHub UI index is unavailable');
      }
      setSummary(await summaryResponse.json());
      const objectPayload = await objectResponse.json();
      const glossaryPayload = await glossaryResponse.json();
      setObjects(objectPayload.objects || []);
      setGlossary(glossaryPayload.terms || []);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Failed to refresh CoveHub index');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    const onHashChange = () => {
      const route = readRoute();
      setScreen(route.screen);
      setSelectedPath(route.objectPath);
      setSelectedTermId(route.termId);
      if (!route.objectPath) setSelected(null);
    };
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  useEffect(() => {
    if (!selectedPath) {
      setSelected(null);
      return;
    }
    let cancelled = false;
    async function loadDetail() {
      setError('');
      try {
        const response = await fetch(`/ui-api/objects/${encodeHubPath(selectedPath)}`);
        if (!response.ok) throw new Error('Object not found in the public CoveHub index');
        const payload = await response.json();
        if (!cancelled) {
          setSelected(payload);
          setScreen(screenForKind(payload.kind));
        }
      } catch (caught) {
        if (!cancelled) {
          setSelected(null);
          setError(caught instanceof Error ? caught.message : 'Failed to load object');
        }
      }
    }
    loadDetail();
    return () => {
      cancelled = true;
    };
  }, [selectedPath]);

  const currentScreen = SCREENS.find((item) => item.id === screen) || SCREENS[0];
  const versionGroups = useMemo(() => groupObjectsByVersion(objects), [objects]);
  const visibleGroups = useMemo(() => {
    if (screen === 'glossary') return [];
    const normalizedQuery = query.trim().toLowerCase();
    return versionGroups
      .filter((group) => currentScreen.kinds.includes(group.primary.kind))
      .filter((group) => !normalizedQuery || groupSearchText(group).includes(normalizedQuery))
      .sort((left, right) => objectSortKey(left.primary).localeCompare(objectSortKey(right.primary)));
  }, [currentScreen, query, screen, versionGroups]);

  const selectedVersionGroup = useMemo(() => {
    if (!selected) return null;
    return versionGroups.find((group) => group.key === groupKeyForObject(selected)) || null;
  }, [selected, versionGroups]);

  const selectedTerm = useMemo(() => {
    if (!glossary.length) return null;
    return glossary.find((term) => term.id === selectedTermId) || glossary[0];
  }, [glossary, selectedTermId]);

  const selectScreen = useCallback((targetScreen) => {
    setScreen(targetScreen);
    setSelectedPath('');
    setSelected(null);
    setSelectedTermId('');
    window.location.hash = targetScreen;
  }, []);

  const selectVersionPath = useCallback((hubPath) => {
    setSelectedPath(hubPath);
    window.location.hash = `object/${hubPath}`;
  }, []);

  return (
    <main className="atlas-shell">
      <aside className="rail" aria-label="Primary navigation">
        <div className="rail-brand">
          <Database size={24} strokeWidth={1.8} aria-hidden="true" />
          <strong>CoveHub</strong>
        </div>
        <nav className="rail-nav">
          {SCREENS.map((item) => (
            <button
              className={screen === item.id ? 'rail-item selected' : 'rail-item'}
              key={item.id}
              onClick={() => selectScreen(item.id)}
              title={item.label}
              type="button"
            >
              <item.icon size={18} aria-hidden="true" />
              <span>{item.shortLabel}</span>
            </button>
          ))}
        </nav>
        <button className="refresh-button" onClick={refresh} title="Refresh index" type="button">
          <RefreshCw size={17} aria-hidden="true" />
        </button>
      </aside>

      <section className="selection-panel">
        <header className="selection-header">
          <div>
            <p className="eyebrow">Public CoveHub atlas</p>
            <h1>{currentScreen.label}</h1>
          </div>
          <SummaryStrip
            summary={summary}
            screen={screen}
            shownCount={screen === 'glossary' ? glossary.length : visibleGroups.length}
          />
        </header>

        {screen === 'glossary' ? (
          <GlossaryList glossary={glossary} selectedTermId={selectedTerm?.id} />
        ) : (
          <>
            <label className="search-box">
              <Search size={16} aria-hidden="true" />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder={`Search ${currentScreen.label.toLowerCase()} by route, owner, publisher, workflow`}
              />
            </label>
            {error ? <div className="error-line">{error}</div> : null}
            {loading ? <LoadingRows /> : null}
            {!loading && visibleGroups.length === 0 ? <EmptySelection screen={screen} /> : null}
            {!loading && visibleGroups.length > 0 ? (
              <ObjectSelectionTable groups={visibleGroups} selectedPath={selectedPath} screen={screen} />
            ) : null}
          </>
        )}
      </section>

      <section className="detail-workspace">
        {screen === 'glossary' ? (
          <GlossaryDetail term={selectedTerm} glossary={glossary} />
        ) : selected ? (
          <ObjectDetail object={selected} versionGroup={selectedVersionGroup} onVersionChange={selectVersionPath} />
        ) : (
          <EmptyDetail screen={screen} />
        )}
      </section>
    </main>
  );
}

function SummaryStrip({ summary, shownCount }) {
  return (
    <div className="summary-strip" aria-label="CoveHub summary">
      <span>{shownCount ?? 0} shown</span>
      <span>{formatBytes(summary?.total_bytes ?? 0)}</span>
      <span>{summary?.publishers?.length ?? 0} publishers</span>
    </div>
  );
}

function ObjectSelectionTable({ groups, selectedPath, screen }) {
  const columns = columnsForScreen(screen);
  return (
    <div className="selection-table-wrap">
      <table className="selection-table">
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column.key}>{column.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {groups.map((group) => {
            const isActive = groupContainsPath(group, selectedPath);
            return (
              <tr
                aria-label={`Open ${selectionRowLabel(group.primary)}`}
                className={isActive ? 'selection-row active' : 'selection-row'}
                key={group.key}
                onClick={(event) => handleSelectionRowClick(event, group.primary.hub_path)}
                onKeyDown={(event) => handleSelectionRowKeyDown(event, group.primary.hub_path)}
                tabIndex={0}
              >
                {columns.map((column) => (
                  <td key={column.key}>{renderObjectCell(group, column.key)}</td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function handleSelectionRowClick(event, hubPath) {
  if (isInteractiveTarget(event.target)) return;
  navigateToHubPath(hubPath);
}

function handleSelectionRowKeyDown(event, hubPath) {
  if (event.key !== 'Enter' && event.key !== ' ') return;
  event.preventDefault();
  navigateToHubPath(hubPath);
}

function isInteractiveTarget(target) {
  return target instanceof Element && Boolean(target.closest('a, button, input, select, textarea, [role="button"], [role="link"]'));
}

function navigateToHubPath(hubPath) {
  window.location.hash = `object/${hubPath}`;
}

function selectionRowLabel(object) {
  return object.workflow_id || object.artifact_name || object.node_id || object.hub_path;
}

function columnsForScreen(screen) {
  if (screen === 'workflows') {
    return [
      { key: 'workflow', label: 'Workflow' },
      { key: 'publisher', label: 'Publisher' },
      { key: 'versions', label: 'Versions' },
      { key: 'size', label: 'Size' },
    ];
  }
  if (screen === 'certificates') {
    return [
      { key: 'node', label: 'Node' },
      { key: 'workflow', label: 'Workflow' },
      { key: 'versions', label: 'Versions' },
      { key: 'size', label: 'Size' },
    ];
  }
  return [
    { key: 'artifact', label: 'Artifact' },
    { key: 'owner', label: 'Owner' },
    { key: 'publisher', label: 'Publisher' },
    { key: 'kind', label: 'Type' },
    { key: 'versions', label: 'Versions' },
  ];
}

function renderObjectCell(group, key) {
  const object = group.primary;
  if (key === 'workflow') {
    return (
      <a className="row-primary" href={`#object/${object.hub_path}`}>
        {object.workflow_id || object.hub_path}
      </a>
    );
  }
  if (key === 'artifact') {
    return (
      <a className="row-primary" href={`#object/${object.hub_path}`}>
        {object.artifact_name || object.hub_path}
      </a>
    );
  }
  if (key === 'node') {
    return (
      <a className="row-primary" href={`#object/${object.hub_path}`}>
        {object.node_id || object.hub_path}
      </a>
    );
  }
  if (key === 'publisher') return object.publisher || '-';
  if (key === 'owner') return object.owner || '-';
  if (key === 'reference') return object.is_latest ? <span className="alias">latest</span> : <span>exact</span>;
  if (key === 'versions') return <VersionSummary group={group} />;
  if (key === 'kind') return <span className={`kind-pill ${object.kind}`}>{KIND_LABELS[object.kind] || object.kind}</span>;
  if (key === 'size') return formatBytes(object.size);
  return object[key] || '-';
}

function VersionSummary({ group }) {
  return (
    <div className="version-summary">
      <span>{group.versions.length} version{group.versions.length === 1 ? '' : 's'}</span>
    </div>
  );
}

function groupObjectsByVersion(objects) {
  const groups = new Map();
  for (const object of objects) {
    const key = groupKeyForObject(object);
    if (!key) continue;
    const existing = groups.get(key) || { key, records: [] };
    existing.records.push(object);
    groups.set(key, existing);
  }
  return Array.from(groups.values()).map((group) => {
    const versions = versionOptionsForRecords(group.records);
    const primary = primaryRecordFor(group.records, versions);
    return {
      ...group,
      primary,
      versions,
    };
  });
}

function groupKeyForObject(object) {
  if (!object) return '';
  if (object.kind === 'workflow') {
    return `workflow:${object.publisher || ''}:${object.workflow_id || ''}`;
  }
  if (object.kind === 'static_artifact') {
    return `static_artifact:${object.owner || ''}:${object.artifact_name || ''}`;
  }
  if (object.kind === 'runtime_artifact') {
    return `runtime_artifact:${object.publisher || ''}:${object.workflow_id || ''}:${object.artifact_name || ''}`;
  }
  if (object.kind === 'runtime_certificate') {
    return `runtime_certificate:${object.publisher || ''}:${object.workflow_id || ''}:${object.node_id || ''}`;
  }
  return `${object.kind}:${object.hub_path}`;
}

function versionOptionsForRecords(records) {
  const latest = records.find((record) => record.is_latest);
  const exactRecords = records
    .filter((record) => !record.is_latest)
    .sort((left, right) => (right.modified_at || '').localeCompare(left.modified_at || ''));
  const byDigest = new Map();
  for (const record of exactRecords) {
    byDigest.set(record.observed_digest || record.digest || record.reference, record);
  }

  const versions = [];
  const latestDigest = latest?.observed_digest;
  if (latest) {
    versions.push({
      value: latest.hub_path,
      digest: latestDigest,
      label: `latest (${shortDigest(latestDigest)})`,
      isLatest: true,
      paths: [latest.hub_path, latest.observed_exact_hub_path].filter(Boolean),
      modified_at: latest.modified_at,
    });
  }
  for (const record of exactRecords) {
    const digest = record.observed_digest || record.digest || record.reference;
    if (latestDigest && digest === latestDigest) continue;
    versions.push({
      value: record.hub_path,
      digest,
      label: `${shortDigest(digest)}${record.modified_at ? ` (${record.modified_at})` : ''}`,
      isLatest: false,
      paths: [record.hub_path, record.observed_exact_hub_path].filter(Boolean),
      modified_at: record.modified_at,
    });
  }
  return versions;
}

function primaryRecordFor(records, versions) {
  const preferredPath = versions[0]?.value;
  return records.find((record) => record.hub_path === preferredPath) || records[0];
}

function groupContainsPath(group, path) {
  if (!group || !path) return false;
  return group.versions.some((version) => version.paths.includes(path) || version.value === path);
}

function selectedVersionOptionValue(group, object) {
  if (!group?.versions?.length || !object) return '';
  const selected = group.versions.find(
    (version) =>
      version.value === object.hub_path ||
      version.paths.includes(object.hub_path) ||
      (version.digest && version.digest === object.observed_digest),
  );
  return selected?.value || object.hub_path;
}

function ObjectDetail({ object, versionGroup, onVersionChange }) {
  if (object.kind === 'workflow') return <WorkflowDetail object={object} versionGroup={versionGroup} onVersionChange={onVersionChange} />;
  if (object.kind === 'runtime_certificate') return <CertificateDetail object={object} versionGroup={versionGroup} onVersionChange={onVersionChange} />;
  return <ArtifactDetail object={object} versionGroup={versionGroup} onVersionChange={onVersionChange} />;
}

function DetailHeader({ object, title, subtitle, icon: Icon, versionGroup, onVersionChange }) {
  const selectedVersionValue = selectedVersionOptionValue(versionGroup, object);
  return (
    <header className="detail-header">
      <div className="detail-topline">
        <div className="detail-title">
          <Icon size={20} aria-hidden="true" />
          <div>
            <p className="eyebrow">{KIND_LABELS[object.kind] || object.kind}</p>
            <h2>{title}</h2>
          </div>
        </div>
        {versionGroup?.versions?.length ? (
          <label className="version-select">
            <span>Version</span>
            <select
              value={selectedVersionValue}
              onChange={(event) => onVersionChange?.(event.target.value)}
            >
              {versionGroup.versions.map((version) => (
                <option key={version.value} value={version.value}>
                  {version.label}
                </option>
              ))}
            </select>
          </label>
        ) : null}
      </div>
      <code className="detail-route">{subtitle || object.hub_path}</code>
      {object.is_latest ? <LatestWarning object={object} /> : null}
    </header>
  );
}

function WorkflowDetail({ object, versionGroup, onVersionChange }) {
  const [tab, setTab] = useState('definition');
  const [composeIndex, setComposeIndex] = useState(0);
  const workflow = object.summary || {};
  const composeFiles = workflow.compose_files || [];
  const activeCompose = composeFiles[composeIndex] || composeFiles[0] || {};
  const treeLinks = useMemo(() => buildWorkflowTreeLinks(object, workflow), [object, workflow]);
  return (
    <div className="detail-content">
      <DetailHeader
        object={object}
        title={`${object.publisher || workflow.publisher}/${object.workflow_id || workflow.workflow_id}`}
        subtitle={object.hub_path}
        icon={Network}
        versionGroup={versionGroup}
        onVersionChange={onVersionChange}
      />
      <MetadataRows
        rows={[
          ['Publisher', object.publisher || workflow.publisher],
          ['Workflow id', object.workflow_id || workflow.workflow_id],
          ['Reference', object.is_latest ? 'latest alias' : 'exact digest path'],
          ['Size', formatBytes(object.size)],
          ['Manifest hash', workflow.manifest_hash],
          ['Nodes', workflow.node_count],
          ['Bundle files', workflow.file_count],
          ['Modified', object.modified_at],
        ]}
      />
      <CommandBlock object={object} />
      <TabBar tabs={WORKFLOW_TABS} selected={tab} onSelect={setTab} />

      {tab === 'definition' ? (
        <StructuredSection
          heading="workflow.normalized.cove.yaml"
          description="Normalized workflow definition with CoveHub routes, node dependencies, services, inputs, outputs, preconditions, and key material annotated inline."
        >
          <TreeView data={workflow.workflow_definition?.parsed} rootLabel="workflow" linkContext={treeLinks} />
        </StructuredSection>
      ) : null}

      {tab === 'compose' ? (
        <StructuredSection
          heading="Generated Docker Compose"
          description="Compiler-generated compose after Cove sidecars and measured runtime material have been injected."
          control={<ComposeSelector files={composeFiles} value={composeIndex} onChange={setComposeIndex} />}
        >
          <TreeView data={activeCompose.parsed} rootLabel={activeCompose.path || 'compose'} linkContext={treeLinks} />
        </StructuredSection>
      ) : null}

      {tab === 'diagram' ? (
        <StructuredSection
          heading="Node, artifact, and certificate graph"
          description="Solid arrows are artifact flow. Dashed amber arrows are certificate dependencies between nodes."
        >
          <WorkflowDiagram diagram={workflow.diagram || { nodes: [], edges: [], artifacts: [] }} />
        </StructuredSection>
      ) : null}

      {tab === 'raw-workflow' ? (
        <StructuredSection heading="Raw workflow definition">
          <CodeBlock value={workflow.workflow_definition?.raw || ''} />
        </StructuredSection>
      ) : null}

      {tab === 'raw-compose' ? (
        <StructuredSection
          heading="Raw generated compose"
          control={<ComposeSelector files={composeFiles} value={composeIndex} onChange={setComposeIndex} />}
        >
          <CodeBlock value={activeCompose.raw || ''} />
        </StructuredSection>
      ) : null}
    </div>
  );
}

function ArtifactDetail({ object, versionGroup, onVersionChange }) {
  const relationships = object.relationships || {};
  const usedBy = relationships.used_by || [];
  const producedBy = relationships.produced_by || [];
  return (
    <div className="detail-content">
      <DetailHeader
        object={object}
        title={object.artifact_name || object.hub_path}
        subtitle={object.hub_path}
        icon={Boxes}
        versionGroup={versionGroup}
        onVersionChange={onVersionChange}
      />
      <MetadataRows
        rows={[
          ['Type', KIND_LABELS[object.kind]],
          ['Owner', object.owner],
          ['Publisher', object.kind === 'runtime_artifact' ? object.publisher : null],
          ['Workflow', object.workflow_id],
          ['Reference', object.is_latest ? 'latest alias' : 'exact digest path'],
          ['Size', formatBytes(object.size)],
          ['Modified', object.modified_at],
        ]}
      />
      <CommandBlock object={object} />
      <StructuredSection heading="Workflow nodes using this artifact as input">
        <RelationshipTable rows={usedBy} empty="No visible workflow node currently lists this artifact as an input." />
      </StructuredSection>
      {producedBy.length ? (
        <StructuredSection heading="Workflow nodes producing this artifact">
          <RelationshipTable rows={producedBy} empty="No producer is visible." />
        </StructuredSection>
      ) : null}
      <StructuredSection heading="Bounded preview">
        <CodeBlock value={object.preview?.text || ''} />
        {object.preview?.truncated ? <p className="subtle">Preview truncated by the read-only UI indexer.</p> : null}
      </StructuredSection>
    </div>
  );
}

function CertificateDetail({ object, versionGroup, onVersionChange }) {
  const relationships = object.relationships || {};
  return (
    <div className="detail-content">
      <DetailHeader
        object={object}
        title={object.node_id || object.hub_path}
        subtitle={object.hub_path}
        icon={KeyRound}
        versionGroup={versionGroup}
        onVersionChange={onVersionChange}
      />
      <MetadataRows
        rows={[
          ['Publisher', object.publisher],
          ['Workflow id', object.workflow_id],
          ['Node id', object.node_id],
          ['Compose hash', object.summary?.generated_node_compose_hash],
          ['Certificate body hash', object.summary?.certificate_body_hash],
          ['Attestation format', object.summary?.attestation_format],
          ['Reference', object.is_latest ? 'latest alias' : 'exact digest path'],
          ['Modified', object.modified_at],
        ]}
      />
      <p className="meaning-line">{object.summary?.meaning}</p>
      <CommandBlock object={object} />
      <StructuredSection heading="Certificate fields">
        <TreeView data={object.summary?.certificate} rootLabel="certificate" />
      </StructuredSection>
      <StructuredSection heading="Generating workflow node">
        <RelationshipTable rows={relationships.generated_by || []} empty="No visible workflow bundle lists the generating node." />
      </StructuredSection>
      <StructuredSection heading="Workflow nodes consuming this certificate">
        <RelationshipTable rows={relationships.consumed_by || []} empty="No visible workflow node declares this certificate dependency." />
      </StructuredSection>
    </div>
  );
}

function StructuredSection({ heading, description, control, children }) {
  return (
    <section className="structured-section">
      <div className="section-line">
        <div>
          <h3>{heading}</h3>
          {description ? <p>{description}</p> : null}
        </div>
        {control}
      </div>
      {children}
    </section>
  );
}

function MetadataRows({ rows }) {
  return (
    <table className="metadata-table">
      <tbody>
        {rows
          .filter(([, value]) => value !== undefined && value !== null && value !== '')
          .map(([label, value]) => (
            <tr key={label}>
              <th>{label}</th>
              <td>{renderDenseValue(value)}</td>
            </tr>
          ))}
      </tbody>
    </table>
  );
}

function renderDenseValue(value) {
  if (typeof value === 'string' && value.startsWith('sha256:')) {
    return <HashChip value={value} />;
  }
  return <span>{String(value)}</span>;
}

function LatestWarning({ object }) {
  return (
    <div className="warning-line">
      <strong>latest is mutable.</strong>
      <span>Observed exact path:</span>
      <code>{object.observed_exact_hub_path}</code>
    </div>
  );
}

function TabBar({ tabs, selected, onSelect }) {
  return (
    <div className="tab-row" role="tablist">
      {tabs.map(([value, label]) => (
        <button
          className={selected === value ? 'tab selected' : 'tab'}
          key={value}
          onClick={() => onSelect(value)}
          role="tab"
          type="button"
        >
          {label}
        </button>
      ))}
    </div>
  );
}

function ComposeSelector({ files, value, onChange }) {
  if (!files?.length) return null;
  return (
    <label className="compact-select">
      <span>Node</span>
      <select value={value} onChange={(event) => onChange(Number(event.target.value))}>
        {files.map((file, index) => (
          <option key={`${file.path}-${index}`} value={index}>
            {file.node_id || file.path || `compose ${index + 1}`}
          </option>
        ))}
      </select>
    </label>
  );
}

function TreeView({ data, rootLabel, linkContext }) {
  if (data === undefined || data === null) {
    return <p className="subtle">No structured data is available for this view.</p>;
  }
  return (
    <div className="tree-view">
      <TreeNode name={rootLabel} value={data} path={rootLabel} depth={0} linkContext={linkContext} />
    </div>
  );
}

function TreeNode({ name, value, path, depth, linkContext, hideKey = false }) {
  const [expanded, setExpanded] = useState(true);
  const children = treeChildren(value, path);
  const annotation = annotationFor(name, value, path);
  const hasChildren = children.length > 0;
  const copyValue = hasChildren ? '' : treeCopyValue(value);
  const showCopy = copyValue.length > 50;
  const rowContent = (
    <>
      {hasChildren ? (
        <span className="tree-toggle" aria-hidden="true">
          {expanded ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
        </span>
      ) : (
        <span className="tree-toggle-spacer" aria-hidden="true" />
      )}
      {hideKey ? null : <span className="tree-key">{name}</span>}
      {annotation ? <span className={`annotation ${annotation.kind}`}>{annotation.label}</span> : null}
      {hasChildren ? null : renderTreeValue(name, value, path, linkContext)}
      {showCopy ? (
        <button
          aria-label={`Copy ${name}`}
          className="tree-copy icon-button"
          onClick={() => navigator.clipboard?.writeText(copyValue)}
          title="Copy value"
          type="button"
        >
          <Copy size={13} aria-hidden="true" />
        </button>
      ) : null}
    </>
  );
  return (
    <div className="tree-node">
      {hasChildren ? (
        <button
          aria-expanded={expanded}
          aria-label={`${expanded ? 'Collapse' : 'Expand'} ${name}`}
          className="tree-row tree-branch-row"
          onClick={() => setExpanded((current) => !current)}
          style={{ '--depth': depth }}
          type="button"
        >
          {rowContent}
        </button>
      ) : (
        <div className="tree-row" style={{ '--depth': depth }}>
          {rowContent}
        </div>
      )}
      {expanded
        ? children.map((child) => (
            <TreeNode
              depth={depth + 1}
              hideKey={child.hideKey}
              key={child.path}
              linkContext={linkContext}
              name={child.name}
              path={child.path}
              value={child.value}
            />
          ))
        : null}
    </div>
  );
}

function treeChildren(value, path) {
  if (Array.isArray(value)) {
    return value.flatMap((child, index) => {
      const childPath = `${path}.${index}`;
      if (child && typeof child === 'object' && !Array.isArray(child)) {
        const entries = Object.entries(child);
        if (entries.length) {
          return entries.map(([childName, childValue]) => ({
            name: childName,
            path: `${childPath}.${childName}`,
            value: childValue,
            hideKey: false,
          }));
        }
      }
      return [{ name: `${index}`, path: childPath, value: child, hideKey: true }];
    });
  }
  if (value && typeof value === 'object') {
    return Object.entries(value).map(([childName, childValue]) => ({
      name: childName,
      path: `${path}.${childName}`,
      value: childValue,
      hideKey: false,
    }));
  }
  return [];
}

function treeCopyValue(value) {
  if (value === undefined) return '';
  if (value === null) return 'null';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return '';
}

function renderTreeValue(name, value, path, linkContext) {
  if (value === null) return <span className="tree-value null">null</span>;
  if (typeof value === 'boolean') return <span className="tree-value bool">{String(value)}</span>;
  if (typeof value === 'number') return <span className="tree-value">{String(value)}</span>;
  const text = String(value);
  const artifactLink = artifactTreeLink(path, text, linkContext);
  if (artifactLink) {
    return (
      <a className="hub-path-link" href={`#object/${artifactLink}`}>
        {text}
      </a>
    );
  }
  const hubPath = normalizeTreeHubPath(text, linkContext);
  if (hubPath && (name === 'hub_path' || text.startsWith('v1/') || text.startsWith('runtime/'))) {
    return (
      <a className="hub-path-link" href={`#object/${hubPath}`}>
        {text}
      </a>
    );
  }
  if (text.startsWith('sha256:')) return <HashChip value={text} />;
  return <span className="tree-value">{text}</span>;
}

function buildWorkflowTreeLinks(object, workflow) {
  const publisher = object.publisher || workflow.publisher;
  const workflowId = object.workflow_id || workflow.workflow_id;
  const artifactHubPaths = {};
  const parsedArtifacts = workflow.workflow_definition?.parsed?.artifacts;
  if (parsedArtifacts && typeof parsedArtifacts === 'object' && !Array.isArray(parsedArtifacts)) {
    for (const [name, artifact] of Object.entries(parsedArtifacts)) {
      if (artifact && typeof artifact === 'object' && typeof artifact.hub_path === 'string') {
        artifactHubPaths[name] = normalizeTreeHubPath(artifact.hub_path, { publisher, workflowId }) || artifact.hub_path;
      }
    }
  }
  for (const artifact of workflow.diagram?.artifacts || []) {
    if (artifact?.name && artifact?.hub_path) {
      artifactHubPaths[artifact.name] = normalizeTreeHubPath(artifact.hub_path, { publisher, workflowId }) || artifact.hub_path;
    }
  }

  const runtimeArtifacts = new Set();
  for (const node of workflow.diagram?.nodes || []) {
    for (const output of node.outputs || []) {
      if (output?.artifact) runtimeArtifacts.add(output.artifact);
    }
  }
  for (const node of workflow.nodes || []) {
    for (const service of node.services || []) {
      for (const output of service.outputs || []) {
        if (output?.artifact) runtimeArtifacts.add(output.artifact);
      }
    }
  }

  return {
    publisher,
    workflowId,
    artifactHubPaths,
    runtimeArtifacts,
  };
}

function artifactTreeLink(path, value, linkContext) {
  if (!linkContext || typeof value !== 'string' || !value || value.startsWith('v1/') || value.startsWith('runtime/')) return null;
  const inInputs = path.includes('.inputs.');
  const inOutputs = path.includes('.outputs.');
  if (!inInputs && !inOutputs) return null;
  if (linkContext.artifactHubPaths?.[value]) return linkContext.artifactHubPaths[value];
  if (
    (inOutputs || linkContext.runtimeArtifacts?.has(value))
    && linkContext.publisher
    && linkContext.workflowId
  ) {
    return `v1/runtime/${linkContext.publisher}/${linkContext.workflowId}/artifacts/${value}/latest`;
  }
  return null;
}

function normalizeTreeHubPath(value, linkContext) {
  if (typeof value !== 'string') return null;
  const path = value.trim().replace(/^\/+/, '');
  if (path.startsWith('v1/')) return path;
  if (path.startsWith('runtime/') && linkContext?.publisher) {
    return `v1/runtime/${linkContext.publisher}/${path.slice('runtime/'.length)}`;
  }
  return null;
}

function annotationFor(name, value, path) {
  if (name === 'nodes') return { label: 'execution stages', kind: 'node' };
  if (name === 'artifacts') return { label: 'CoveHub data routes', kind: 'artifact' };
  if (name === 'dependencies') return { label: 'certificate gates', kind: 'certificate' };
  if (name === 'preconditions') return { label: 'runtime admission rule', kind: 'rule' };
  if (name === 'services') return { label: 'containers/tasks', kind: 'service' };
  if (name === 'inputs') return { label: 'mounted artifacts', kind: 'artifact' };
  if (name === 'outputs') return { label: 'emitted artifacts', kind: 'artifact' };
  if (name === 'ephemeral_keypairs') return { label: 'runtime keys', kind: 'key' };
  if (/\.nodes\.[^.]+$/.test(path) && value && typeof value === 'object') return { label: 'node', kind: 'node' };
  return null;
}

function HashChip({ value }) {
  return (
    <button className="hash-chip" onClick={() => navigator.clipboard?.writeText(value)} title="Copy hash" type="button">
      {shortDigest(value)}
    </button>
  );
}

function WorkflowDiagram({ diagram }) {
  const [hoveredDiagramTarget, setHoveredDiagramTarget] = useState(null);
  const layout = useMemo(() => buildDiagramLayout(diagram), [diagram]);
  if (!layout.boxes.length) return <p className="subtle">No workflow graph data is available.</p>;
  return (
    <div className="diagram-wrap">
      <svg
        className="workflow-diagram vertical"
        width={layout.width}
        height={layout.height}
        viewBox={`0 0 ${layout.width} ${layout.height}`}
        role="img"
        aria-label="Workflow graph"
      >
        <defs>
          <marker id="arrow-artifact" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto">
            <path d="M0,0 L0,6 L9,3 z" fill="#3d6f56" />
          </marker>
          <marker id="arrow-cert" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto">
            <path d="M0,0 L0,6 L9,3 z" fill="#9b6b1a" />
          </marker>
        </defs>
        {layout.edges.map((edge, index) => (
          <g key={`${edge.from}-${edge.to}-${index}`}>
            <path
              className={`diagram-edge ${edge.kind}${isDiagramEdgeHighlighted(edge, hoveredDiagramTarget) ? ' highlighted' : ''}`}
              d={edge.path}
              markerEnd={edge.kind === 'certificate' ? 'url(#arrow-cert)' : 'url(#arrow-artifact)'}
            />
          </g>
        ))}
        {layout.boxes.map((box) => (
          <g key={box.id}>
            <rect
              className={`diagram-box ${box.kind}${isDiagramBoxHighlighted(box, hoveredDiagramTarget) ? ' highlighted' : ''}`}
              x={box.x}
              y={box.y}
              width={box.width}
              height={box.height}
              rx="5"
            />
            {box.kind === 'node' ? (
              <foreignObject x={box.x} y={box.y} width={box.width} height={box.height}>
                <DiagramNodeCard
                  box={box}
                  onHoverTarget={setHoveredDiagramTarget}
                />
              </foreignObject>
            ) : (
              <>
                <text className="diagram-title" x={box.x + 12} y={box.y + 19}>{box.title}</text>
                {box.kind === 'input' || box.kind === 'output' ? (
                  <text className="diagram-muted" x={box.x + 12} y={box.y + 39}>
                    {box.kind === 'output' ? 'dynamic output artifact by ' : 'static artifact by '}
                    <tspan className="diagram-owner">{box.owner || '-'}</tspan>
                  </text>
                ) : (
                  box.lines.map((line, index) => (
                    <text className="diagram-muted" x={box.x + 12} y={box.y + 39 + index * 17} key={`${box.id}-${line}`}>
                      {line}
                    </text>
                  ))
                )}
              </>
            )}
          </g>
        ))}
      </svg>
    </div>
  );
}

function DiagramNodeCard({ box, onHoverTarget }) {
  return (
    <div className="diagram-node-card" xmlns="http://www.w3.org/1999/xhtml">
      <div className="diagram-node-title">{box.title}</div>
      <div className="diagram-preconditions">
        {box.preconditions.length ? (
          box.preconditions.map((entry, index) => (
            <div className="diagram-precondition-row" key={`${box.id}-${entry.service}-${index}`}>
              <span className="diagram-precondition-service">{entry.service}</span>
              <DiagramExpression
                expression={entry.expression}
                nodeBoxId={box.id}
                onHoverTarget={onHoverTarget}
              />
            </div>
          ))
        ) : (
          <span className="diagram-precondition-empty">no preconditions</span>
        )}
      </div>
    </div>
  );
}

function DiagramExpression({ expression, nodeBoxId, onHoverTarget }) {
  const parts = expressionParts(expression);
  return (
    <span className="diagram-expression">
      {parts.map((part, index) => (
        <DiagramExpressionPart
          key={`${part.kind}-${part.value}-${index}`}
          nodeBoxId={nodeBoxId}
          onHoverTarget={onHoverTarget}
          part={part}
        />
      ))}
    </span>
  );
}

function DiagramExpressionPart({ part, nodeBoxId, onHoverTarget }) {
  if (part.kind === 'hash') {
    return (
      <button
        className="diagram-token hash"
        onClick={() => navigator.clipboard?.writeText(part.value)}
        title={`Copy ${part.value}`}
        type="button"
      >
        {ellipsizeEnd(part.value, 14)}
      </button>
    );
  }
  if (part.kind === 'input' || part.kind === 'certificate') {
    const target = part.kind === 'input' ? `artifact:${part.name}` : `certificate:${part.name}`;
    return (
      <button
        className={`diagram-token ${part.kind}`}
        onBlur={() => onHoverTarget(null)}
        onFocus={() => onHoverTarget({ target, nodeBoxId })}
        onMouseEnter={() => onHoverTarget({ target, nodeBoxId })}
        onMouseLeave={() => onHoverTarget(null)}
        title={part.value}
        type="button"
      >
        {ellipsizeEnd(part.label, 18)}
      </button>
    );
  }
  if (part.kind === 'operator') return <span className="diagram-operator">{part.value}</span>;
  if (part.kind === 'paren') return <span className="diagram-paren">{part.value}</span>;
  return <span className="diagram-literal">{part.value}</span>;
}

function isDiagramBoxHighlighted(box, hoveredTarget) {
  return Boolean(hoveredTarget?.target && box.id === hoveredTarget.target);
}

function isDiagramEdgeHighlighted(edge, hoveredTarget) {
  if (!hoveredTarget?.target) return false;
  if (hoveredTarget.nodeBoxId) {
    return edge.from === hoveredTarget.target && edge.to === hoveredTarget.nodeBoxId;
  }
  return edge.from === hoveredTarget.target || edge.to === hoveredTarget.target;
}

function expressionParts(expression) {
  if (expression === null) return [{ kind: 'literal', value: 'null' }];
  if (typeof expression === 'boolean' || typeof expression === 'number') {
    return [{ kind: 'literal', value: String(expression) }];
  }
  if (typeof expression === 'string') {
    if (expression.startsWith('sha256:')) return [{ kind: 'hash', value: expression }];
    return [{ kind: 'literal', value: expression }];
  }
  if (Array.isArray(expression)) {
    return expression.flatMap((entry, index) => [
      ...(index ? [{ kind: 'operator', value: ',' }] : []),
      ...expressionParts(entry),
    ]);
  }
  if (!expression || typeof expression !== 'object') {
    return [{ kind: 'literal', value: String(expression) }];
  }
  if (typeof expression.var === 'string') return [variableExpressionPart(expression.var)];

  const entries = Object.entries(expression);
  if (entries.length !== 1) return [{ kind: 'literal', value: JSON.stringify(expression) }];
  const [operator, operand] = entries[0];
  if (operator === 'and' || operator === 'or') {
    const operands = Array.isArray(operand) ? operand : [operand];
    return [
      { kind: 'paren', value: '(' },
      ...operands.flatMap((entry, index) => [
        ...(index ? [{ kind: 'operator', value: operator }] : []),
        ...expressionParts(entry),
      ]),
      { kind: 'paren', value: ')' },
    ];
  }
  if (operator === '!') {
    return [
      { kind: 'operator', value: 'not' },
      { kind: 'paren', value: '(' },
      ...expressionParts(operand),
      { kind: 'paren', value: ')' },
    ];
  }
  const operands = Array.isArray(operand) ? operand : [operand];
  if (operands.length === 2) {
    return [
      { kind: 'paren', value: '(' },
      ...expressionParts(operands[0]),
      { kind: 'operator', value: infixOperator(operator) },
      ...expressionParts(operands[1]),
      { kind: 'paren', value: ')' },
    ];
  }
  return [
    { kind: 'operator', value: operator },
    { kind: 'paren', value: '(' },
    ...operands.flatMap((entry, index) => [
      ...(index ? [{ kind: 'operator', value: ',' }] : []),
      ...expressionParts(entry),
    ]),
    { kind: 'paren', value: ')' },
  ];
}

function variableExpressionPart(value) {
  const inputMatch = value.match(/^inputs\.([^.]+)(?:\.(.+))?$/);
  if (inputMatch) {
    return {
      kind: 'input',
      name: inputMatch[1],
      value,
      label: value,
    };
  }
  const certificateMatch = value.match(/^certificates\.([^.]+)(?:\.(.+))?$/);
  if (certificateMatch) {
    return {
      kind: 'certificate',
      name: certificateMatch[1],
      value,
      label: value,
    };
  }
  return { kind: 'literal', value };
}

function infixOperator(operator) {
  return {
    '==': '=',
    '===': '=',
    '!=': '!=',
    '!==': '!=',
    '>': '>',
    '>=': '>=',
    '<': '<',
    '<=': '<=',
  }[operator] || operator;
}

function estimateExpressionRows(expression) {
  return Math.max(1, Math.ceil(expressionParts(expression).length / 5));
}

function ellipsizeEnd(value, maxLength) {
  const text = String(value);
  if (text.length <= maxLength) return text;
  return `${text.slice(0, Math.max(0, maxLength - 3)).replace(/[.\s]+$/, '')}...`;
}

function buildDiagramLayout(diagram) {
  const { boxes: inputBoxes, edges: inputEdges } = buildDiagramGraph(diagram);
  if (!inputBoxes.length) return { boxes: [], edges: [], width: 0, height: 0 };

  const graph = new dagre.graphlib.Graph({ multigraph: true });
  graph.setGraph({
    rankdir: 'TB',
    ranker: 'network-simplex',
    acyclicer: 'greedy',
    nodesep: 42,
    edgesep: 18,
    ranksep: 78,
    marginx: 28,
    marginy: 28,
  });
  graph.setDefaultEdgeLabel(() => ({}));

  for (const box of inputBoxes) {
    graph.setNode(box.id, { width: box.width, height: box.height, box });
  }
  inputEdges.forEach((edge, index) => {
    if (graph.hasNode(edge.from) && graph.hasNode(edge.to)) {
      graph.setEdge(edge.from, edge.to, { kind: edge.kind }, `edge-${index}`);
    }
  });

  dagre.layout(graph);

  const boxes = graph.nodes().map((id) => {
    const node = graph.node(id);
    return {
      ...node.box,
      x: node.x - node.width / 2,
      y: node.y - node.height / 2,
    };
  });
  const edges = graph.edges().map((edge) => {
    const label = graph.edge(edge);
    return {
      from: edge.v,
      to: edge.w,
      kind: label.kind || 'artifact',
      path: pointsToPath(label.points || []),
    };
  });
  const graphLabel = graph.graph();
  return {
    boxes,
    edges,
    width: Math.ceil(graphLabel.width || 800),
    height: Math.ceil(graphLabel.height || 420),
  };
}

function buildDiagramGraph(diagram) {
  const rawNodes = diagram?.nodes || [];
  const artifactMetadata = new Map();
  for (const artifact of diagram?.artifacts || []) {
    if (artifact?.name) artifactMetadata.set(artifact.name, artifact);
  }
  const outputProducers = {};
  for (const node of rawNodes) {
    for (const output of node.outputs || []) {
      if (output?.artifact) outputProducers[output.artifact] = node.node_id;
    }
    for (const input of node.inputs || []) {
      if (input?.artifact && !artifactMetadata.has(input.artifact)) {
        artifactMetadata.set(input.artifact, { name: input.artifact, type: 'static' });
      }
    }
    for (const output of node.outputs || []) {
      if (output?.artifact && !artifactMetadata.has(output.artifact)) {
        artifactMetadata.set(output.artifact, { name: output.artifact, type: 'dynamic' });
      }
    }
  }

  const boxesById = new Map();
  const edges = [];
  const edgeKeys = new Set();
  function addBox(box) {
    if (!boxesById.has(box.id)) boxesById.set(box.id, box);
  }
  function addEdge(from, to, kind) {
    const key = `${from}->${to}:${kind}`;
    if (!edgeKeys.has(key)) {
      edgeKeys.add(key);
      edges.push({ from, to, kind });
    }
  }

  for (const [name, artifact] of artifactMetadata.entries()) {
    const produced = Boolean(outputProducers[name]) || artifact.type === 'dynamic';
    const owner = artifact.owner || artifact.publisher || '';
    addBox({
      id: `artifact:${name}`,
      kind: produced ? 'output' : 'input',
      width: 230,
      height: 74,
      title: truncate(name, 28),
      owner,
      lines: [
        produced ? 'dynamic output artifact' : 'static artifact',
      ],
    });
  }

  for (const node of rawNodes) {
    const services = node.services || [];
    const preconditions = services
      .filter((service) => service?.preconditions !== undefined && service?.preconditions !== null)
      .map((service) => ({
        service: service.service_name || 'task',
        expression: service.preconditions,
      }));
    const preconditionHeight = preconditions.length
      ? preconditions.reduce((total, entry) => total + 17 + estimateExpressionRows(entry.expression) * 19, 0)
      : 18;
    addBox({
      id: `node:${node.node_id}`,
      kind: 'node',
      width: 340,
      height: Math.max(86, 38 + preconditionHeight),
      title: truncate(node.node_id, 28),
      tasks: services.map((service) => service.service_name).filter(Boolean),
      preconditions,
      lines: [],
    });
    addBox({
      id: `certificate:${node.node_id}`,
      kind: 'certificate',
      width: 230,
      height: 74,
      title: truncate(node.node_id, 28),
      lines: ['runtime certificate'],
    });

    for (const input of node.inputs || []) {
      if (input?.artifact) addEdge(`artifact:${input.artifact}`, `node:${node.node_id}`, 'artifact');
    }
    for (const output of node.outputs || []) {
      if (output?.artifact) addEdge(`node:${node.node_id}`, `artifact:${output.artifact}`, 'artifact');
    }
    addEdge(`node:${node.node_id}`, `certificate:${node.node_id}`, 'certificate');
    for (const dependency of node.dependencies || []) {
      addEdge(`certificate:${dependency}`, `node:${node.node_id}`, 'certificate');
    }
  }

  return { boxes: Array.from(boxesById.values()), edges };
}

function pointsToPath(points) {
  if (!points.length) return '';
  return points
    .map((point, index) => `${index === 0 ? 'M' : 'L'}${point.x},${point.y}`)
    .join(' ');
}

function RelationshipTable({ rows, empty }) {
  if (!rows?.length) return <p className="subtle">{empty}</p>;
  return (
    <table className="metadata-table relation-table">
      <thead>
        <tr>
          <th>Workflow</th>
          <th>Node</th>
          <th>Context</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row, index) => (
          <tr key={`${row.workflow_hub_path}-${row.node_id}-${index}`}>
            <td>
              <a href={`#object/${row.workflow_hub_path}`}>
                {row.publisher}/{row.workflow_id}
              </a>
            </td>
            <td>{row.node_id || '-'}</td>
            <td>{row.reason || row.artifact_name || shortDigest(row.compose_hash || '') || '-'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function CommandBlock({ object }) {
  return (
    <section className="command-block">
      <div className="command-heading">
        <Terminal size={15} aria-hidden="true" />
        <span>Local inspection commands</span>
      </div>
      <CommandLine command={object.cli?.inspect} />
      <CommandLine command={object.cli?.get} />
    </section>
  );
}

function CommandLine({ command }) {
  if (!command) return null;
  return (
    <div className="command-line">
      <code>{command}</code>
      <button className="icon-button" onClick={() => navigator.clipboard?.writeText(command)} title="Copy" type="button">
        <Copy size={14} aria-hidden="true" />
      </button>
    </div>
  );
}

function CodeBlock({ value }) {
  return (
    <pre className="code-block">
      <code>{value || ''}</code>
    </pre>
  );
}

function GlossaryList({ glossary, selectedTermId }) {
  return (
    <div className="glossary-list">
      {glossary.map((term) => (
        <a className={term.id === selectedTermId ? 'glossary-row selected' : 'glossary-row'} href={`#glossary/${term.id}`} key={term.id}>
          <strong>{term.term}</strong>
          <span>{term.summary}</span>
        </a>
      ))}
    </div>
  );
}

function GlossaryDetail({ term, glossary }) {
  if (!term) return <EmptyDetail screen="glossary" />;
  const linkedTerms = (term.links || []).map((id) => glossary.find((item) => item.id === id)).filter(Boolean);
  return (
    <div className="detail-content">
      <header className="detail-header">
        <div className="detail-title">
          <BookOpen size={20} aria-hidden="true" />
          <div>
            <p className="eyebrow">Glossary</p>
            <h2>{term.term}</h2>
          </div>
        </div>
      </header>
      <p className="meaning-line">{term.summary}</p>
      <StructuredSection heading="Cross references">
        <div className="cross-links">
          {linkedTerms.map((linked) => (
            <a href={`#glossary/${linked.id}`} key={linked.id}>
              <strong>{linked.term}</strong>
              <span>{linked.summary}</span>
            </a>
          ))}
        </div>
      </StructuredSection>
    </div>
  );
}

function EmptySelection({ screen }) {
  return (
    <div className="empty-selection">
      <h2>No visible {screen}</h2>
      <p>Publish Cove objects to CoveHub and refresh this atlas to populate the list.</p>
      <a href="/docs/hello_world.md">Read the hello_world walkthrough</a>
    </div>
  );
}

function EmptyDetail({ screen }) {
  return (
    <div className="empty-detail">
      <Cable size={24} aria-hidden="true" />
      <h2>Select an item</h2>
      <p>
        CoveHub is transport, not truth. This UI organizes public objects and explains their structure, but local
        Cove verification is still where trust decisions happen.
      </p>
      <div className="empty-links">
        <a href="/docs/security.md">Security model</a>
        <a href="/docs/server.md">Server runbook</a>
        <a href={screen === 'glossary' ? '#glossary/covehub' : '#glossary'}>Glossary</a>
      </div>
    </div>
  );
}

function LoadingRows() {
  return (
    <div className="loading-stack">
      <div />
      <div />
      <div />
      <div />
    </div>
  );
}

function readRoute() {
  const rawHash = window.location.hash.replace(/^#/, '');
  if (rawHash.startsWith('object/')) {
    const objectPath = decodeURIComponent(rawHash.slice('object/'.length));
    return {
      screen: screenForHubPath(objectPath),
      objectPath,
      termId: '',
    };
  }
  if (rawHash.startsWith('glossary/')) {
    return { screen: 'glossary', objectPath: '', termId: rawHash.slice('glossary/'.length) };
  }
  if (['workflows', 'artifacts', 'certificates', 'glossary'].includes(rawHash)) {
    return { screen: rawHash, objectPath: '', termId: '' };
  }
  return { screen: 'workflows', objectPath: '', termId: '' };
}

function screenForKind(kind) {
  if (kind === 'workflow') return 'workflows';
  if (kind === 'runtime_certificate') return 'certificates';
  return 'artifacts';
}

function screenForHubPath(path) {
  if (path.startsWith('v1/workflows/')) return 'workflows';
  if (path.includes('/certificates/')) return 'certificates';
  if (path.startsWith('v1/artifacts/') || path.includes('/artifacts/')) return 'artifacts';
  return 'workflows';
}

function encodeHubPath(path) {
  return path.split('/').map(encodeURIComponent).join('/');
}

function objectSearchText(object) {
  return [
    object.hub_path,
    object.kind,
    object.owner,
    object.publisher,
    object.workflow_id,
    object.artifact_name,
    object.node_id,
    object.reference,
  ]
    .filter(Boolean)
    .join(' ')
    .toLowerCase();
}

function groupSearchText(group) {
  return [
    objectSearchText(group.primary),
    ...group.records.map(objectSearchText),
    ...group.versions.map((version) => [version.label, version.digest, version.modified_at].filter(Boolean).join(' ')),
  ]
    .join(' ')
    .toLowerCase();
}

function objectSortKey(object) {
  return [object.publisher || object.owner || '', object.workflow_id || '', object.artifact_name || '', object.node_id || '', object.hub_path].join('/');
}

function formatBytes(value) {
  if (!Number.isFinite(value)) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let amount = value;
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024;
    index += 1;
  }
  return `${amount.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function shortDigest(value) {
  if (!value) return '';
  if (value.length <= 24) return value;
  return `${value.slice(0, 12)}...${value.slice(-8)}`;
}

function truncate(value, length) {
  if (!value) return '';
  return value.length > length ? `${value.slice(0, length - 1)}...` : value;
}

createRoot(document.getElementById('root')).render(<App />);
