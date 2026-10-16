// A role's instructions live in agents/<phase>.md: inlined at build time, filled at run time, hashed into the run's
// manifest. These tests cover the three steps on throwaway files; prompts-golden.test.js covers the real roles.

import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'

import { build } from '../../workflows/build_workflow.mjs'
import { inject, roleFiles } from '../../workflows/inject_config.mjs'
import { fill } from '../../workflows/phases/src/lib/phase.js'
import { runWorkflow } from './harness.js'

test('fill replaces every slot once and does not rescan a value', () => {
  assert.equal(fill('a {{x}} b {{y}} {{x}}', { x: '1', y: '{{x}}' }), 'a 1 b {{x}} 1')
  assert.equal(fill('n={{n}}', { n: 0 }), 'n=0')
})

test('fill refuses a slot with no value and a value with no slot', () => {
  assert.throws(() => fill('{{x}} {{y}}', { x: '1' }), /no value for \{\{y\}\}/)
  assert.throws(() => fill('{{x}}', { x: '1', z: '2' }), /no slot for z/)
})

test('fill renders a value as a template literal does, undefined included', () => {
  const missing = {}
  assert.equal(fill('[{{x}}]', { x: missing.absent }), `[${missing.absent}]`)
  assert.equal(fill('[{{x}}]', { x: JSON.stringify(undefined) }), `[${JSON.stringify(undefined)}]`)
})

function workspace(t, role) {
  const dir = mkdtempSync(join(tmpdir(), 'role-files-'))
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  writeFileSync(join(dir, 'role.md'), role)
  writeFileSync(join(dir, 'phase.js'), [
    'export const meta = {',
    "  name: 'p',",
    "  description: 'd',",
    '}',
    "import ROLE from './role.md'",
    'const INJECTED_ARGS = null // @@INJECTED_ARGS@@',
    "const out = await agent(ROLE, { label: 'a' })",
    'return out',
    '',
  ].join('\n'))
  return dir
}

test('a text import is inlined verbatim, without its final newline, and hashed in the header', async (t) => {
  const text = 'Use `tool` and ${not_a_template}   "quoted" \\ backslash\n'
  const dir = workspace(t, text)
  const built = build(join(dir, 'phase.js'))
  assert.ok(!/^import /m.test(built))
  const sha = createHash('sha256').update(text).digest('hex')
  assert.ok(built.includes(`role.md sha256=${sha}`))
  writeFileSync(join(dir, 'built.js'), built)
  const { calls } = await runWorkflow(() => ({}), { args: {}, path: join(dir, 'built.js') })
  assert.equal(calls[0].prompt, text.slice(0, -1))
})

test('a text file without a final newline is refused', (t) => {
  const dir = workspace(t, 'no newline')
  assert.throws(() => build(join(dir, 'phase.js')), /must end with a newline/)
})

test('the role files of a built workflow are read back for the manifest', (t) => {
  const dir = workspace(t, 'Role text.\n')
  const built = build(join(dir, 'phase.js'))
  const files = roleFiles(built)
  assert.equal(files.length, 1)
  assert.ok(files[0].path.endsWith('role.md'))
  assert.equal(files[0].sha256, createHash('sha256').update('Role text.\n').digest('hex'))
  assert.deepEqual(roleFiles(inject(built, { a: 1 }).script), files)
  assert.deepEqual(roleFiles('// no header\n'), [])
})
