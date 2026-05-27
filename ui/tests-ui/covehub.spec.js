import { expect, test } from '@playwright/test';

const sha = (digit) => `sha256:${digit.repeat(64)}`;

const summary = {
  ok: true,
  data_root: '/data',
  generated_at: '2026-05-04T00:00:00Z',
  object_count: 5,
  total_bytes: 1284,
  kind_counts: { workflow: 2, static_artifact: 1, runtime_artifact: 1, runtime_certificate: 1 },
  owners: ['alice', 'bob'],
  publishers: ['alice'],
  workflows: ['alice/hello_world'],
};

const objects = [
  {
    hub_path: 'v1/workflows/alice/hello_world/latest',
    kind: 'workflow',
    reference: 'latest',
    is_latest: true,
    owner: null,
    publisher: 'alice',
    workflow_id: 'hello_world',
    artifact_name: null,
    node_id: null,
    digest: null,
    observed_digest: sha('1'),
    observed_exact_hub_path: `v1/workflows/alice/hello_world/${sha('1')}`,
    size: 512,
    modified_at: '2026-05-04T00:00:00Z',
  },
  {
    hub_path: `v1/workflows/alice/hello_world/${sha('9')}`,
    kind: 'workflow',
    reference: sha('9'),
    is_latest: false,
    owner: null,
    publisher: 'alice',
    workflow_id: 'hello_world',
    artifact_name: null,
    node_id: null,
    digest: sha('9'),
    observed_digest: sha('9'),
    observed_exact_hub_path: `v1/workflows/alice/hello_world/${sha('9')}`,
    size: 512,
    modified_at: '2026-05-03T00:00:00Z',
  },
  {
    hub_path: 'v1/artifacts/alice/model/latest',
    kind: 'static_artifact',
    reference: 'latest',
    is_latest: true,
    owner: 'alice',
    publisher: null,
    workflow_id: null,
    artifact_name: 'model',
    node_id: null,
    digest: null,
    observed_digest: sha('5'),
    observed_exact_hub_path: `v1/artifacts/alice/model/${sha('5')}`,
    size: 130,
    modified_at: '2026-05-04T00:00:00Z',
  },
  {
    hub_path: 'v1/runtime/alice/hello_world/certificates/final_server/latest',
    kind: 'runtime_certificate',
    reference: 'latest',
    is_latest: true,
    owner: null,
    publisher: 'alice',
    workflow_id: 'hello_world',
    artifact_name: null,
    node_id: 'final_server',
    digest: null,
    observed_digest: sha('2'),
    observed_exact_hub_path: `v1/runtime/alice/hello_world/certificates/final_server/${sha('2')}`,
    size: 130,
    modified_at: '2026-05-04T00:00:00Z',
  },
  {
    hub_path: 'v1/runtime/alice/hello_world/artifacts/bob_secret_word_transformed/latest',
    kind: 'runtime_artifact',
    reference: 'latest',
    is_latest: true,
    owner: 'bob',
    publisher: 'alice',
    workflow_id: 'hello_world',
    artifact_name: 'bob_secret_word_transformed',
    node_id: null,
    digest: null,
    observed_digest: sha('8'),
    observed_exact_hub_path: `v1/runtime/alice/hello_world/artifacts/bob_secret_word_transformed/${sha('8')}`,
    size: 34,
    modified_at: '2026-05-04T00:00:00Z',
  },
];

const workflowDetail = {
  ...objects[0],
  summary: {
    format: 'cove.workflow.bundle.v1',
    publisher: 'alice',
    workflow_id: 'hello_world',
    manifest_hash: sha('3'),
    node_count: 2,
    file_count: 3,
    nodes: [
      {
        node_id: 'prepare',
        compose_path: 'nodes/prepare.compose.yaml',
        compose_hash: sha('4'),
        dependencies: [],
        artifacts: [{ name: 'model', direction: 'input', hub_path: 'v1/artifacts/alice/model/latest', owner: 'alice' }],
        services: [{ service_name: 'worker', inputs: [{ path: '/in/model', artifact: 'model' }], outputs: [] }],
        definition: { services: { worker: { inputs: { '/in/model': 'model' } } } },
      },
      {
        node_id: 'final_server',
        compose_path: 'nodes/final_server.compose.yaml',
        compose_hash: sha('6'),
        dependencies: ['prepare'],
        artifacts: [],
        services: [{ service_name: 'server', inputs: [], outputs: [{ path: '/out/result', artifact: 'result' }] }],
        definition: { dependencies: ['prepare'], services: { server: { outputs: { '/out/result': 'result' } } } },
      },
    ],
    workflow_definition: {
      path: 'workflow.normalized.cove.yaml',
      raw: 'workflow:\n  id: hello_world\nnodes:\n  prepare: {}\n  final_server:\n    dependencies:\n      - prepare\n',
      parsed: {
        workflow: { id: 'hello_world' },
        artifacts: {
          model: { type: 'static', owner: 'alice', hub_path: 'v1/artifacts/alice/model/latest' },
          result: { type: 'dynamic', owner: 'alice', hub_path: 'runtime/hello_world/artifacts/result/latest' },
        },
        nodes: {
          prepare: { services: { worker: { inputs: { '/in/model': 'model' } } } },
          final_server: {
            dependencies: ['prepare'],
            services: {
              server: {
                outputs: { '/out/result': 'result' },
                preconditions: { '==': [{ var: 'inputs.model.plaintext_hash' }, sha('5')] },
              },
            },
          },
        },
      },
    },
    compose_files: [
      {
        node_id: 'prepare',
        path: 'nodes/prepare.compose.yaml',
        compose_hash: sha('4'),
        raw: 'services:\n  worker:\n    image: worker@sha256:abc\n',
        parsed: { services: { worker: { image: 'worker@sha256:abc' } } },
        services: ['worker'],
      },
      {
        node_id: 'final_server',
        path: 'nodes/final_server.compose.yaml',
        compose_hash: sha('6'),
        raw: 'services:\n  server:\n    image: server@sha256:def\n',
        parsed: { services: { server: { image: 'server@sha256:def' } } },
        services: ['server'],
      },
    ],
    diagram: {
      nodes: [
        {
          node_id: 'prepare',
          compose_hash: sha('4'),
          services: [{ service_name: 'worker', preconditions: { '==': [{ var: 'inputs.model.plaintext_hash' }, sha('5')] } }],
          dependencies: [],
          inputs: [{ artifact: 'model', path: '/in/model' }],
          outputs: [],
          certificate_hub_path: 'v1/runtime/alice/hello_world/certificates/prepare/latest',
        },
        {
          node_id: 'final_server',
          compose_hash: sha('6'),
          services: [{ service_name: 'server', preconditions: { '==': [{ var: 'certificates.prepare.certificate_body.results.worker.pass' }, true] } }],
          dependencies: ['prepare'],
          inputs: [],
          outputs: [{ artifact: 'result', path: '/out/result' }],
          certificate_hub_path: 'v1/runtime/alice/hello_world/certificates/final_server/latest',
        },
      ],
      artifacts: [
        { name: 'model', type: 'static', owner: 'alice', hub_path: 'v1/artifacts/alice/model/latest' },
        { name: 'result', type: 'dynamic', owner: 'alice', hub_path: 'runtime/hello_world/artifacts/result/latest' },
      ],
      edges: [],
    },
  },
  relationships: {},
  preview: { kind: 'json', text: '{ "format": "cove.workflow.bundle.v1" }', truncated: false },
  cli: {
    inspect: 'cove hub inspect --server-url https://api.covehub.io v1/workflows/alice/hello_world/latest',
    get: 'cove hub get --server-url https://api.covehub.io v1/workflows/alice/hello_world/latest --output hello_world-latest.json',
  },
};

const certificateDetail = {
  ...objects[3],
  summary: {
    format: 'cove.runtime.certificate',
    workflow_id: 'hello_world',
    node_id: 'final_server',
    generated_node_compose_hash: sha('6'),
    certificate_body_hash: sha('7'),
    attestation_format: 'phala_dstack_v1',
    meaning: 'This certificate claims that node final_server emitted outputs while running a generated compose hash.',
    certificate: {
      certificate_body: {
        workflow_id: 'hello_world',
        node_id: 'final_server',
        generated_node_compose_hash: sha('6'),
        inputs: {},
        outputs: {},
        results: { server: { pass: true } },
      },
      certificate_body_hash: sha('7'),
    },
  },
  relationships: {
    generated_by: [{ workflow_hub_path: objects[0].hub_path, publisher: 'alice', workflow_id: 'hello_world', node_id: 'final_server', compose_hash: sha('6') }],
    consumed_by: [],
  },
  preview: { kind: 'json', text: '{}', truncated: false },
  cli: {
    inspect: 'cove hub inspect --server-url https://api.covehub.io v1/runtime/alice/hello_world/certificates/final_server/latest',
    get: 'cove hub get --server-url https://api.covehub.io v1/runtime/alice/hello_world/certificates/final_server/latest --output final_server-latest.json',
  },
};

const runtimeArtifactDetail = {
  ...objects[4],
  summary: { format: 'opaque_bytes' },
  relationships: {
    used_by: [
      { workflow_hub_path: objects[0].hub_path, publisher: 'alice', workflow_id: 'hello_world', node_id: 'final_server', artifact_name: 'bob_secret_word_transformed' },
    ],
    produced_by: [
      { workflow_hub_path: objects[0].hub_path, publisher: 'alice', workflow_id: 'hello_world', node_id: 'bob_word_length_checker', artifact_name: 'bob_secret_word_transformed' },
    ],
  },
  preview: { kind: 'bytes', text: 'encrypted bytes', truncated: false },
  cli: {
    inspect: 'cove hub inspect --server-url https://api.covehub.io v1/runtime/alice/hello_world/artifacts/bob_secret_word_transformed/latest',
    get: 'cove hub get --server-url https://api.covehub.io v1/runtime/alice/hello_world/artifacts/bob_secret_word_transformed/latest --output bob_secret_word_transformed-latest.bin',
  },
};

const glossary = {
  terms: [
    {
      id: 'covehub',
      term: 'CoveHub',
      summary: 'Public storage and transport for Cove objects. It is not a trust root.',
      links: ['latest'],
    },
    {
      id: 'latest',
      term: 'latest',
      summary: 'A mutable convenience pointer.',
      links: ['covehub'],
    },
  ],
};

test.beforeEach(async ({ page }) => {
  await page.route('**/ui-api/summary', async (route) => route.fulfill({ json: summary }));
  await page.route('**/ui-api/glossary', async (route) => route.fulfill({ json: glossary }));
  await page.route('**/ui-api/objects?**', async (route) => route.fulfill({ json: { objects, limit: 1000 } }));
  await page.route('**/ui-api/objects/v1/workflows/alice/hello_world/latest', async (route) =>
    route.fulfill({ json: workflowDetail }),
  );
  await page.route(`**/ui-api/objects/v1/workflows/alice/hello_world/${encodeURIComponent(sha('9'))}`, async (route) =>
    route.fulfill({ json: { ...workflowDetail, ...objects[1], cli: workflowDetail.cli } }),
  );
  await page.route('**/ui-api/objects/v1/runtime/alice/hello_world/certificates/final_server/latest', async (route) =>
    route.fulfill({ json: certificateDetail }),
  );
  await page.route('**/ui-api/objects/v1/runtime/alice/hello_world/artifacts/bob_secret_word_transformed/latest', async (route) =>
    route.fulfill({ json: runtimeArtifactDetail }),
  );
});

test('landing page introduces Cove and opens the atlas', async ({ page }) => {
  await page.goto('/');

  await expect(page.getByRole('heading', { name: 'Cove' })).toBeVisible();
  await expect(page.getByText('multi-stage audits over private models, code, and data')).toBeVisible();
  await expect(page.locator('.landing-hero-image')).toHaveAttribute('src', /cove-workflow-hero/);
  await page.getByRole('link', { name: /Explore CoveHub/ }).click();
  await expect(page).toHaveURL(/#workflows$/);
  await expect(page.getByRole('heading', { name: 'Workflows' })).toBeVisible();
});

test('uses left navigation and separate object screens without observed digest column', async ({ page }) => {
  await page.goto('/#workflows');

  await expect(page.getByRole('heading', { name: 'Workflows' })).toBeVisible();
  await expect(page.locator('.row-primary', { hasText: 'hello_world' })).toBeVisible();
  await expect(page.locator('tr', { hasText: 'hello_world' }).getByText('2 versions')).toBeVisible();
  await expect(page.locator('tr', { hasText: 'hello_world' }).getByText('latest')).toHaveCount(0);
  await expect(page.getByText('final_server')).toHaveCount(0);
  await expect(page.getByRole('columnheader', { name: 'Observed digest' })).toHaveCount(0);

  await page.locator('tr', { hasText: 'hello_world' }).locator('td').nth(1).click();
  await expect(page).toHaveURL(/#object\/v1\/workflows\/alice\/hello_world\/latest$/);
  await expect(page.locator('.detail-route')).toContainText('v1/workflows/alice/hello_world/latest');

  await page.getByRole('button', { name: /Data/ }).click();
  await expect(page.getByRole('heading', { name: 'Artifacts' })).toBeVisible();
  await expect(page.locator('.row-primary', { hasText: 'model' })).toBeVisible();

  await page.getByRole('button', { name: /Certs/ }).click();
  await expect(page.getByRole('heading', { name: 'Certificates' })).toBeVisible();
  await expect(page.locator('.row-primary', { hasText: 'final_server' })).toBeVisible();
});

test('searches within the current screen', async ({ page }) => {
  await page.goto('#artifacts');

  await page.getByPlaceholder('Search artifacts by route, owner, publisher, workflow').fill('model');
  await expect(page.locator('.row-primary', { hasText: 'model' })).toBeVisible();

  await page.getByPlaceholder('Search artifacts by route, owner, publisher, workflow').fill('bob');
  await expect(page.locator('.row-primary', { hasText: 'bob_secret_word_transformed' })).toBeVisible();

  await page.getByPlaceholder('Search artifacts by route, owner, publisher, workflow').fill('missing');
  await expect(page.getByRole('heading', { name: 'No visible artifacts' })).toBeVisible();
});

test('runtime artifact detail keeps owner and publisher distinct', async ({ page }) => {
  await page.goto('#artifacts');

  const runtimeRow = page.locator('tr', { hasText: 'bob_secret_word_transformed' });
  await expect(runtimeRow.locator('td').nth(1)).toHaveText('bob');
  await expect(runtimeRow.locator('td').nth(2)).toHaveText('alice');
  await runtimeRow.locator('.row-primary').click();

  await expect(page).toHaveURL(/#object\/v1\/runtime\/alice\/hello_world\/artifacts\/bob_secret_word_transformed\/latest$/);
  await expect(page.locator('.detail-route')).toContainText('v1/runtime/alice/hello_world/artifacts/bob_secret_word_transformed/latest');
  await expect(page.locator('.metadata-table tr', { hasText: 'Owner' })).toContainText('bob');
  await expect(page.locator('.metadata-table tr', { hasText: 'Publisher' })).toContainText('alice');
  await expect(page.locator('.metadata-table tr', { hasText: 'Owner' })).not.toContainText('alice');
});

test('workflow detail exposes definition, compose, diagram, and raw tabs', async ({ page }) => {
  await page.goto('/#object/v1/workflows/alice/hello_world/latest');

  await expect(page.getByText('latest is mutable.')).toBeVisible();
  await expect(page.getByText('cove hub inspect --server-url https://api.covehub.io')).toBeVisible();
  await expect(page.getByText('execution stages')).toBeVisible();
  await expect(page.locator('.tree-count')).toHaveCount(0);
  const definitionTree = page.locator('.tree-view').first();
  const prepareGuideWidth = await definitionTree.locator('.tree-row', { hasText: 'prepare' }).first().evaluate((row) =>
    Number.parseFloat(getComputedStyle(row, '::before').width),
  );
  expect(prepareGuideWidth).toBeGreaterThan(0);
  await definitionTree.getByLabel('Collapse nodes').click();
  await expect(definitionTree.locator('.tree-row', { hasText: 'prepare' })).toHaveCount(0);
  await definitionTree.getByLabel('Expand nodes').press('Enter');
  await expect(definitionTree.locator('.tree-row', { hasText: 'prepare' }).first()).toBeVisible();
  await expect(definitionTree.getByRole('link', { name: 'model', exact: true })).toHaveAttribute(
    'href',
    '#object/v1/artifacts/alice/model/latest',
  );
  await expect(definitionTree.getByRole('link', { name: 'result', exact: true })).toHaveAttribute(
    'href',
    '#object/v1/runtime/alice/hello_world/artifacts/result/latest',
  );
  await expect(definitionTree.getByRole('link', { name: 'runtime/hello_world/artifacts/result/latest' })).toHaveAttribute(
    'href',
    '#object/v1/runtime/alice/hello_world/artifacts/result/latest',
  );
  await expect(definitionTree.locator('.tree-key').filter({ hasText: /^0$/ })).toHaveCount(0);
  await expect(definitionTree.locator('.tree-key').filter({ hasText: /^1$/ })).toHaveCount(0);
  await expect(definitionTree.locator('.tree-row', { hasText: 'inputs.model.plaintext_hash' })).toBeVisible();
  await expect(page.getByLabel('Version')).toHaveValue('v1/workflows/alice/hello_world/latest');
  const versionLabels = await page.getByLabel('Version').evaluate((select) =>
    Array.from(select.options).map((option) => option.textContent),
  );
  expect(versionLabels).toContain(`latest (${sha('1').slice(0, 12)}...${sha('1').slice(-8)})`);
  expect(versionLabels.some((label) => label?.includes(sha('9').slice(0, 12)))).toBe(true);

  await page.getByRole('tab', { name: 'Compose', exact: true }).click();
  await expect(page.getByText('Generated Docker Compose')).toBeVisible();
  await expect(page.getByText('containers/tasks')).toBeVisible();

  await page.getByRole('tab', { name: 'Diagram' }).click();
  await expect(page.locator('svg.workflow-diagram')).toBeVisible();
  await expect(page.getByText('tasks: worker')).toHaveCount(0);
  await expect(page.getByText('static artifact by')).toBeVisible();
  await expect(page.getByText('dynamic output artifact by')).toBeVisible();
  await expect(page.getByTitle('inputs.model.plaintext_hash')).toBeVisible();
  await expect(page.getByTitle(`Copy ${sha('5')}`)).toBeVisible();
  await expect(page.getByTitle('certificates.prepare.certificate_body.results.worker.pass')).toBeVisible();
  await expect(page.getByRole('button', { name: 'inputs.model.pl...' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'certificates.pr...' })).toBeVisible();
  await expect(page.getByRole('button', { name: /^sha256:5{4}\.\.\.$/ })).toBeVisible();
  const nodeBoxHeight = await page.locator('.diagram-box.node').first().evaluate((node) => Number(node.getAttribute('height')));
  expect(nodeBoxHeight).toBeLessThan(100);
  await expect(page.getByText(/^inputs:/)).toHaveCount(0);
  await expect(page.getByText(/^outputs:/)).toHaveCount(0);
  await page.getByTitle('inputs.model.plaintext_hash').hover();
  await expect(page.locator('.diagram-box.input.highlighted')).toBeVisible();
  await expect(page.locator('.diagram-edge.artifact.highlighted')).toHaveCount(1);
  await page.getByTitle('certificates.prepare.certificate_body.results.worker.pass').hover();
  await expect(page.locator('.diagram-box.certificate.highlighted')).toBeVisible();
  await expect(page.locator('.diagram-edge.certificate.highlighted')).toHaveCount(1);
  await expect(page.getByText('output artifact')).toBeVisible();
  await expect(page.getByText('runtime certificate').first()).toBeVisible();

  await page.getByRole('tab', { name: 'Raw workflow' }).click();
  await expect(page.locator('pre', { hasText: 'workflow:' })).toBeVisible();

  await page.getByRole('tab', { name: 'Raw compose' }).click();
  await expect(page.locator('pre', { hasText: 'services:' })).toBeVisible();
  await expect(page.locator('.verification-badge')).toHaveCount(0);

  await page.getByLabel('Version').selectOption(`v1/workflows/alice/hello_world/${sha('9')}`);
  await expect(page.locator('.detail-route')).toContainText(`v1/workflows/alice/hello_world/${sha('9')}`);
});

test('certificate detail explains fields and links workflow node relationships', async ({ page }) => {
  await page.goto('/#object/v1/runtime/alice/hello_world/certificates/final_server/latest');

  await expect(page.locator('.rail-item.selected', { hasText: 'Certs' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Certificate fields' })).toBeVisible();
  await expect(page.getByText('This certificate claims that node final_server')).toBeVisible();
  await expect(page.getByLabel('Copy generated_node_compose_hash').first()).toBeVisible();
  await expect(page.getByRole('link', { name: 'alice/hello_world' })).toBeVisible();
});

test('desktop panels scroll independently', async ({ page }) => {
  await page.goto('/#object/v1/workflows/alice/hello_world/latest');

  const overflowSettings = await page.evaluate(() => ({
    width: window.innerWidth,
    height: window.innerHeight,
    body: getComputedStyle(document.body).overflow,
    rail: getComputedStyle(document.querySelector('.rail')).overflow,
    selection: getComputedStyle(document.querySelector('.selection-panel')).overflowY,
    detail: getComputedStyle(document.querySelector('.detail-workspace')).overflowY,
    shellHeight: getComputedStyle(document.querySelector('.atlas-shell')).height,
  }));

  if (overflowSettings.width > 760) {
    expect(overflowSettings.body).toBe('hidden');
    expect(overflowSettings.rail).toBe('hidden');
    expect(overflowSettings.selection).toBe('auto');
    expect(overflowSettings.detail).toBe('auto');
    expect(overflowSettings.shellHeight).toBe(`${overflowSettings.height}px`);
  } else {
    expect(overflowSettings.body).toBe('auto');
  }
});

test('glossary is a left-nav screen', async ({ page }) => {
  await page.goto('/#workflows');

  await page.getByRole('button', { name: /Terms/ }).click();
  await expect(page.getByRole('heading', { name: 'Glossary' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'CoveHub' })).toBeVisible();
  await expect(page.locator('.glossary-row', { hasText: 'latest' })).toBeVisible();
});

test('desktop and mobile layouts fit without horizontal document overflow', async ({ page }, testInfo) => {
  await page.goto('/');
  await page.screenshot({ path: testInfo.outputPath(`landing-${testInfo.project.name}.png`), fullPage: true });
  const landingMetrics = await page.evaluate(() => {
    const nextSection = document.querySelector('#why');
    return {
      hasOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
      nextSectionTop: nextSection?.getBoundingClientRect().top ?? window.innerHeight,
      viewportHeight: window.innerHeight,
    };
  });
  expect(landingMetrics.hasOverflow).toBe(false);
  expect(landingMetrics.nextSectionTop).toBeLessThan(landingMetrics.viewportHeight);

  await page.goto('/#object/v1/workflows/alice/hello_world/latest');
  await page.screenshot({ path: testInfo.outputPath(`atlas-${testInfo.project.name}.png`), fullPage: true });

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
  expect(overflow).toBe(false);
});
