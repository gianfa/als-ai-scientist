#!/usr/bin/env node
// Build a runnable Claude Code workflow script from a modular source.
//
// The workflow runtime requires `export const meta = {...}` as the FIRST statement and gives the
// script no module or filesystem access, so `import` cannot be used. This script inlines every
// module the source imports (following `export {...} from` re-exports) after `meta`.
//
// Usage (from the repository root):
//   node workflows/build_workflow.mjs                  # every workflows/phases/src/<phase>.js -> workflows/phases/<phase>.js
//   node workflows/build_workflow.mjs --check          # exit 1 if a built phase is out of date
//   node workflows/build_workflow.mjs SRC OUT [--check]
//
// A source may also import a text file, `import ROLE from '../../../agents/<phase>.md'`: the role's instructions,
// kept as a document rather than as code. It is inlined as a string constant (its one final newline dropped), and
// its sha256 is listed in the header line `// Role files:`, which the run's manifest records.
//
// Deterministic: the output depends only on the source files; no timestamp or absolute path is written.

import { createHash } from 'node:crypto'
import { readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const PHASES_SRC = resolve(PACKAGE_ROOT, 'workflows/phases/src')
const PHASES_OUT = resolve(PACKAGE_ROOT, 'workflows/phases')

// [source, built] for every phase workflow; `lib/` holds the modules they import.
export function phaseTargets() {
  return readdirSync(PHASES_SRC)
    .filter(f => f.endsWith('.js'))
    .sort()
    .map(f => [resolve(PHASES_SRC, f), resolve(PHASES_OUT, f)])
}

// Globals the workflow runtime provides to the script body.
export const RUNTIME_GLOBALS = ['args', 'agent', 'parallel', 'pipeline', 'phase', 'log', 'budget', 'workflow']

const IMPORT_RE = /^import\s*\{[^}]*\}\s*from\s*'([^']+)'\s*;?[ \t]*\n/gm
const TEXT_IMPORT_RE = /^import\s+([A-Za-z_$][\w$]*)\s+from\s*'([^']+\.md)'\s*;?[ \t]*\n/gm
const REEXPORT_RE = /^export\s*\{[^}]*\}\s*from\s*'([^']+)'\s*;?[ \t]*\n/gm
const META_RE = /^export const meta = \{[\s\S]*?\n\}\n/m
const EXPORT_DECL_RE = /^export (?=(const|let|function|async function|class) )/gm
export const INJECTED_ARGS_MARKER_RE = /^const INJECTED_ARGS = null \/\/ @@INJECTED_ARGS@@$/m

const rel = path => relative(PACKAGE_ROOT, path)
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex')

// A JS string literal for any text: JSON quoting, plus the two line terminators JSON leaves raw.
const jsString = text => JSON.stringify(text).replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029')

// The text files a source imports, each as { name, file, text }. The file must end with one newline, which is
// not part of the text: an editor adds it, and the prompt never had it.
function textImports(srcPath, src) {
  return [...src.matchAll(TEXT_IMPORT_RE)].map(([, name, spec]) => {
    if (!spec.startsWith('.')) throw new Error(`${rel(srcPath)}: only relative imports can be inlined: '${spec}'`)
    const file = resolve(dirname(srcPath), spec)
    const raw = readFileSync(file, 'utf8')
    if (!raw.endsWith('\n')) throw new Error(`${rel(file)}: must end with a newline`)
    return { name, file, text: raw.slice(0, -1) }
  })
}

// Depth-first: a module's own imports come before it. Barrel files (only re-exports) add no code.
function collectModules(file, seen, order) {
  if (seen.has(file)) return
  seen.add(file)
  const src = readFileSync(file, 'utf8')
  for (const re of [IMPORT_RE, REEXPORT_RE]) {
    for (const m of src.matchAll(re)) {
      if (!m[1].startsWith('.')) throw new Error(`${rel(file)}: only relative imports can be inlined: '${m[1]}'`)
      collectModules(resolve(dirname(file), m[1]), seen, order)
    }
  }
  order.push(file)
}

function moduleBody(file) {
  const src = readFileSync(file, 'utf8').replace(IMPORT_RE, '').replace(REEXPORT_RE, '')
  const body = src.replace(EXPORT_DECL_RE, '')
  const left = body.match(/^\s*(import|export)\b.*$/m)
  if (left) throw new Error(`${rel(file)}: unsupported module syntax: ${left[0].trim()}`)
  return body.trim()
}

export function build(srcPath) {
  const src = readFileSync(srcPath, 'utf8')
  const meta = src.match(META_RE)
  if (!meta) throw new Error(`${rel(srcPath)}: no top-level 'export const meta = {...}'`)

  const markerMatches = src.match(INJECTED_ARGS_MARKER_RE)
  if (!markerMatches || markerMatches.length === 0) {
    throw new Error(`${rel(srcPath)}: marker line 'const INJECTED_ARGS = null // @@INJECTED_ARGS@@' not found`)
  }
  // Count occurrences of the marker in the source to ensure it appears exactly once
  const markerCount = (src.match(new RegExp(INJECTED_ARGS_MARKER_RE.source, 'gm')) || []).length
  if (markerCount !== 1) {
    throw new Error(`${rel(srcPath)}: marker line must appear exactly once, found ${markerCount}`)
  }

  const seen = new Set([srcPath])
  const modules = []
  for (const m of src.matchAll(IMPORT_RE)) collectModules(resolve(dirname(srcPath), m[1]), seen, modules)

  const texts = textImports(srcPath, src)
  const inputs = [srcPath, ...modules, ...texts.map(x => x.file)]
  const hash = createHash('sha256')
  for (const f of inputs) hash.update(rel(f)).update('\0').update(readFileSync(f)).update('\0')

  // A module with no code (e.g. a barrel file with only comments and re-exports) is skipped.
  const hasCode = body => body.replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, '').trim() !== ''
  const inlined = modules.map(f => ({ f, body: moduleBody(f) })).filter(m => hasCode(m.body))
  let script = src.replace(IMPORT_RE, '').replace(TEXT_IMPORT_RE, '').replace(META_RE, '').trim()
  const left = script.match(/^\s*(import|export)\b.*$/m)
  if (left) throw new Error(`${rel(srcPath)}: unsupported module syntax: ${left[0].trim()}`)

  const out = [
    meta[0].trimEnd(),
    '',
    `// GENERATED by workflows/build_workflow.mjs from ${rel(srcPath)} — do not edit.`,
    `// Rebuild: node workflows/build_workflow.mjs   Inputs sha256: ${hash.digest('hex')}`,
    `// Inputs: ${inputs.map(rel).join(', ')}`,
    ...(texts.length ? [`// Role files: ${texts.map(x => `${rel(x.file)} sha256=${sha256(readFileSync(x.file))}`).join(', ')}`] : []),
    '',
    ...inlined.flatMap(({ f, body }) => [`// ---------- inlined: ${rel(f)} ----------`, '', body, '']),
    ...texts.flatMap(x => [`// ---------- inlined: ${rel(x.file)} ----------`, '', `const ${x.name} = ${jsString(x.text)}`, '']),
    `// ---------- ${rel(srcPath)} ----------`,
    '',
    script,
    '',
  ].join('\n')
  validate(out)
  return out
}

// Fails on syntax errors and duplicate top-level names, as the runtime would.
export function validate(script) {
  if (!/^export const meta = \{/.test(script)) throw new Error('meta is not the first statement')
  if (/^\s*import\b/m.test(script)) throw new Error('import statement left in the built script')
  const AsyncFunction = (async () => {}).constructor
  new AsyncFunction(...RUNTIME_GLOBALS, script.replace(/^export const meta =/, 'const meta ='))
}

function main(argv) {
  const check = argv.includes('--check')
  const paths = argv.filter(a => a !== '--check').map(p => resolve(p))
  if (paths.length !== 0 && paths.length !== 2) throw new Error('usage: build_workflow.mjs [SRC OUT] [--check]')
  const targets = paths.length ? [paths] : phaseTargets()
  let stale = 0
  for (const [src, out] of targets) {
    const built = build(src)
    if (check) {
      let current = null
      try { current = readFileSync(out, 'utf8') } catch { /* missing counts as stale */ }
      if (current !== built) {
        console.error(`${rel(out)} is out of date: run node workflows/build_workflow.mjs`)
        stale += 1
      } else {
        console.log(`${rel(out)} is up to date`)
      }
    } else {
      writeFileSync(out, built)
      console.log(`wrote ${rel(out)}`)
    }
  }
  return stale ? 1 : 0
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exit(main(process.argv.slice(2)))
}
