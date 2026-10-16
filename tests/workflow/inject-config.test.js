// Tests for the config injection script.
// Ensures configs with edge cases (quotes, backslashes, non-ASCII, U+2028/U+2029) round-trip correctly,
// and marker validation/manifest handling work as expected.

import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { execSync } from 'node:child_process'
import { existsSync, readFileSync, writeFileSync, rmSync, mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { inject } from '../../workflows/inject_config.mjs'

const TEST_DIR = resolve(dirname(fileURLToPath(import.meta.url)), '../../.test-inject')

// Synthetic workflow fixture: minimal valid workflow with the marker.
function fixtureWorkflow(markerCount = 1) {
  const marker = 'const INJECTED_ARGS = null // @@INJECTED_ARGS@@'
  const lines = [
    'export const meta = {',
    "  name: 'x',",
    "  description: 'y',",
    '}',
    '',
    ...(markerCount === 1 ? [marker] : Array(markerCount).fill(marker)),
    '',
    'log(JSON.stringify(INJECTED_ARGS))',
  ]
  return lines.join('\n')
}

// Extract INJECTED_ARGS from injected script by evaluating it.
function extractInjectedArgs(script) {
  // Find the line with INJECTED_ARGS assignment and capture the Object.freeze(...) expression.
  // Use dotall mode (s) to match across newlines if needed.
  const match = script.match(/const INJECTED_ARGS = (Object\.freeze\(.+?\)) \/\/ @@INJECTED_ARGS@@/s)
  if (!match) throw new Error('Could not extract INJECTED_ARGS from injected script')

  // Use Function constructor to evaluate the expression.
  const expr = match[1]
  // eslint-disable-next-line no-new-func
  const fn = new Function(`return ${expr}`)
  return fn()
}

test('round-trip: simple config', () => {
  const config = { key: 'value' }
  const fixture = fixtureWorkflow()
  const { script } = inject(fixture, config)

  const extracted = extractInjectedArgs(script)
  assert.deepEqual(extracted, config)
})

test('round-trip: config with quotes', () => {
  const config = { message: 'say "hello" to the world' }
  const fixture = fixtureWorkflow()
  const { script } = inject(fixture, config)

  const extracted = extractInjectedArgs(script)
  assert.deepEqual(extracted, config)
})

test('round-trip: config with backslashes', () => {
  const config = { path: 'C:\\Users\\test\\file.txt' }
  const fixture = fixtureWorkflow()
  const { script } = inject(fixture, config)

  const extracted = extractInjectedArgs(script)
  assert.deepEqual(extracted, config)
})

test('round-trip: config with non-ASCII characters', () => {
  const config = { text: 'Größe αβγ 中文' }
  const fixture = fixtureWorkflow()
  const { script } = inject(fixture, config)

  const extracted = extractInjectedArgs(script)
  assert.deepEqual(extracted, config)
})

test('round-trip: config with U+2028 (line separator)', () => {
  const config = { text: 'line\u2028separator' }
  const fixture = fixtureWorkflow()
  const { script } = inject(fixture, config)

  const extracted = extractInjectedArgs(script)
  assert.deepEqual(extracted, config)
})

test('round-trip: config with U+2029 (paragraph separator)', () => {
  const config = { text: 'paragraph\u2029separator' }
  const fixture = fixtureWorkflow()
  const { script } = inject(fixture, config)

  const extracted = extractInjectedArgs(script)
  assert.deepEqual(extracted, config)
})

test('round-trip: config with $& (regex special chars)', () => {
  const config = { text: 'match $& replace' }
  const fixture = fixtureWorkflow()
  const { script } = inject(fixture, config)

  const extracted = extractInjectedArgs(script)
  assert.deepEqual(extracted, config)
})

test('round-trip: complex nested config', () => {
  const config = {
    nested: {
      array: [1, 'two', { three: true }],
      special: 'quote"slash\\newline\nnull\0tab\t',
    },
  }
  const fixture = fixtureWorkflow()
  const { script } = inject(fixture, config)

  const extracted = extractInjectedArgs(script)
  assert.deepEqual(extracted, config)
})

test('argsSha256 is deterministic', () => {
  const config = { key: 'value' }
  const fixture = fixtureWorkflow()
  const { argsSha256: sha1 } = inject(fixture, config)
  const { argsSha256: sha2 } = inject(fixture, config)

  assert.equal(sha1, sha2)
  // Sanity check: should be a valid sha256 hex string.
  assert.match(sha1, /^[a-f0-9]{64}$/)
})

test('marker missing throws', () => {
  const config = { key: 'value' }
  const fixture = 'export const meta = { name: "x", description: "y" }\n\nlog("test")'

  assert.throws(
    () => inject(fixture, config),
    /marker line not found/,
  )
})

test('marker duplicated throws', () => {
  const config = { key: 'value' }
  const fixture = fixtureWorkflow(2)

  assert.throws(
    () => inject(fixture, config),
    /marker line appears more than once/,
  )
})

test('args must be a plain object', () => {
  const fixture = fixtureWorkflow()

  assert.throws(
    () => inject(fixture, 'string'),
    /args must be a plain object/,
  )
  assert.throws(
    () => inject(fixture, null),
    /args must be a plain object/,
  )
  assert.throws(
    () => inject(fixture, [1, 2, 3]),
    /args must be a plain object/,
  )
})

test('injected script is frozen and immutable', () => {
  const config = { key: 'value' }
  const fixture = fixtureWorkflow()
  const { script } = inject(fixture, config)

  const extracted = extractInjectedArgs(script)
  assert.throws(() => {
    extracted.key = 'modified'
  }, /Cannot assign to read only property/)
})

test('CLI: write injected workflow to output file', (t) => {
  const testRun = `run-${t.name.replace(/[^a-z0-9]/gi, '-')}`
  const tmpDir = resolve(TEST_DIR, testRun)
  mkdirSync(tmpDir, { recursive: true })

  try {
    const workflowFile = resolve(tmpDir, 'test-workflow.js')
    const argsFile = resolve(tmpDir, 'args.json')
    const outFile = resolve(tmpDir, 'test-workflow.injected.js')
    const manifestFile = resolve(tmpDir, 'manifest.json')

    // Write fixture files.
    writeFileSync(workflowFile, fixtureWorkflow())
    writeFileSync(argsFile, JSON.stringify({ config: 'test' }))

    // Run the injector from the repository root.
    const result = execSync(
      `node workflows/inject_config.mjs --workflow "${workflowFile}" --args "${argsFile}" --out "${outFile}" --manifest "${manifestFile}"`,
      { cwd: resolve(TEST_DIR, '..'), encoding: 'utf8' },
    )

    // Output file should exist.
    assert(existsSync(outFile), 'output file not created')

    // Result should be the output path.
    assert.equal(result.trim(), outFile)

    // Manifest should exist and contain one entry.
    assert(existsSync(manifestFile), 'manifest not created')
    const manifest = JSON.parse(readFileSync(manifestFile, 'utf8'))
    assert.equal(manifest.length, 1)
    assert.equal(manifest[0].workflow, 'test-workflow.injected.js')
  } finally {
    rmSync(tmpDir, { recursive: true, force: true })
  }
})

test('CLI: fail if output file already exists', (t) => {
  const testRun = `run-${t.name.replace(/[^a-z0-9]/gi, '-')}`
  const tmpDir = resolve(TEST_DIR, testRun)
  mkdirSync(tmpDir, { recursive: true })

  try {
    const workflowFile = resolve(tmpDir, 'test-workflow.js')
    const argsFile = resolve(tmpDir, 'args.json')
    const outFile = resolve(tmpDir, 'test-workflow.injected.js')
    const manifestFile = resolve(tmpDir, 'manifest.json')

    // Write fixture files.
    writeFileSync(workflowFile, fixtureWorkflow())
    writeFileSync(argsFile, JSON.stringify({ config: 'test' }))

    // Pre-create the output file.
    writeFileSync(outFile, 'existing content')

    // Run the injector from the repository root.
    try {
      execSync(
        `node workflows/inject_config.mjs --workflow "${workflowFile}" --args "${argsFile}" --out "${outFile}" --manifest "${manifestFile}"`,
        { cwd: resolve(TEST_DIR, '..'), encoding: 'utf8' },
      )
      assert.fail('should have thrown')
    } catch (e) {
      // Should fail with exit code 1.
      assert.equal(e.status, 1)
      assert(e.stderr.includes('already exists'))
    }

    // File should be unchanged.
    assert.equal(readFileSync(outFile, 'utf8'), 'existing content')
  } finally {
    rmSync(tmpDir, { recursive: true, force: true })
  }
})

test('CLI: manifest appends entries across multiple runs', (t) => {
  const testRun = `run-${t.name.replace(/[^a-z0-9]/gi, '-')}`
  const tmpDir = resolve(TEST_DIR, testRun)
  mkdirSync(tmpDir, { recursive: true })

  try {
    const workflowFile = resolve(tmpDir, 'test-workflow.js')
    const argsFile1 = resolve(tmpDir, 'args1.json')
    const argsFile2 = resolve(tmpDir, 'args2.json')
    const outFile1 = resolve(tmpDir, 'test-workflow.run1.js')
    const outFile2 = resolve(tmpDir, 'test-workflow.run2.js')
    const manifestFile = resolve(tmpDir, 'manifest.json')

    // Write fixture files.
    writeFileSync(workflowFile, fixtureWorkflow())
    writeFileSync(argsFile1, JSON.stringify({ config: 'run1' }))
    writeFileSync(argsFile2, JSON.stringify({ config: 'run2' }))

    // Run injector twice with different args from the als-ai-scientist directory.
    const cwd = resolve(TEST_DIR, '..')

    execSync(
      `node workflows/inject_config.mjs --workflow "${workflowFile}" --args "${argsFile1}" --out "${outFile1}" --manifest "${manifestFile}"`,
      { cwd },
    )
    execSync(
      `node workflows/inject_config.mjs --workflow "${workflowFile}" --args "${argsFile2}" --out "${outFile2}" --manifest "${manifestFile}"`,
      { cwd },
    )

    // Manifest should have two entries.
    const manifest = JSON.parse(readFileSync(manifestFile, 'utf8'))
    assert.equal(manifest.length, 2)
    assert.equal(manifest[0].workflow, 'test-workflow.run1.js')
    assert.equal(manifest[1].workflow, 'test-workflow.run2.js')

    // Each entry should have correct sha256 fields.
    for (const entry of manifest) {
      assert(entry.source_sha256, 'missing source_sha256')
      assert(entry.args_sha256, 'missing args_sha256')
      assert(entry.out_sha256, 'missing out_sha256')
      assert.match(entry.source_sha256, /^[a-f0-9]{64}$/)
      assert.match(entry.args_sha256, /^[a-f0-9]{64}$/)
      assert.match(entry.out_sha256, /^[a-f0-9]{64}$/)
    }
  } finally {
    rmSync(tmpDir, { recursive: true, force: true })
  }
})

test('CLI: manifest hashes are correct', (t) => {
  const testRun = `run-${t.name.replace(/[^a-z0-9]/gi, '-')}`
  const tmpDir = resolve(TEST_DIR, testRun)
  mkdirSync(tmpDir, { recursive: true })

  try {
    const workflowFile = resolve(tmpDir, 'test-workflow.js')
    const argsFile = resolve(tmpDir, 'args.json')
    const outFile = resolve(tmpDir, 'test-workflow.injected.js')
    const manifestFile = resolve(tmpDir, 'manifest.json')

    // Write fixture files.
    writeFileSync(workflowFile, fixtureWorkflow())
    writeFileSync(argsFile, JSON.stringify({ config: 'test' }))

    // Run the injector from the repository root.
    execSync(
      `node workflows/inject_config.mjs --workflow "${workflowFile}" --args "${argsFile}" --out "${outFile}" --manifest "${manifestFile}"`,
      { cwd: resolve(TEST_DIR, '..') },
    )

    // Verify manifest hashes.
    const manifest = JSON.parse(readFileSync(manifestFile, 'utf8'))
    const entry = manifest[0]

    const workflowHash = createHash('sha256').update(readFileSync(workflowFile)).digest('hex')
    const outHash = createHash('sha256').update(readFileSync(outFile)).digest('hex')

    assert.equal(entry.source_sha256, workflowHash, 'source_sha256 mismatch')
    assert.equal(entry.out_sha256, outHash, 'out_sha256 mismatch')
  } finally {
    rmSync(tmpDir, { recursive: true, force: true })
  }
})
