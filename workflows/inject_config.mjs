#!/usr/bin/env node
// Inject resolved configuration into a per-run workflow script.
//
// The workflow runtime has no filesystem, no Buffer, and no clock, so any scheme where an LLM
// re-types configuration is fragile by construction. Instead, run.sh writes a per-run copy of the
// built workflow with the resolved config embedded as a JS literal. The launcher passes only
// scriptPath (a short path) and no args. This keeps config out of the LLM's context and makes
// every run reproducible and auditable.
//
// Usage (from ):
//   node workflows/inject_config.mjs --workflow <built.js> --args <args.json> --out <out.js> --manifest <manifest.json>
//
// The marker contract: each workflow source declares one line immediately after meta:
//   const INJECTED_ARGS = null // @@INJECTED_ARGS@@
// This script replaces it with:
//   const INJECTED_ARGS = Object.freeze(<JSON>) // @@INJECTED_ARGS@@ sha256=<hash>

import { createHash } from 'node:crypto'
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { dirname, basename, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { validate } from './build_workflow.mjs'

const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')

// Marker must appear exactly once as a complete line.
const MARKER_RE = /^const INJECTED_ARGS = null \/\/ @@INJECTED_ARGS@@$/m
const MARKER_GLOBAL_RE = /^const INJECTED_ARGS = null \/\/ @@INJECTED_ARGS@@$/gm

// The role files a built workflow carries, from its `// Role files:` header line (workflows/build_workflow.mjs):
// [{ path, sha256 }], empty for a workflow with none.
export function roleFiles(workflowSrc) {
  const line = workflowSrc.match(/^\/\/ Role files: (.+)$/m)
  if (!line) return []
  return line[1].split(', ').map(entry => {
    const m = entry.match(/^(\S+) sha256=([0-9a-f]{64})$/)
    if (!m) throw new Error(`malformed role file entry: ${entry}`)
    return { path: m[1], sha256: m[2] }
  })
}

export function inject(workflowSrc, argsObj) {
  // Validate args is a plain object.
  if (typeof argsObj !== 'object' || argsObj === null || Array.isArray(argsObj)) {
    throw new Error('args must be a plain object')
  }

  // Stringify with U+2028 and U+2029 escaped so it's a valid JS literal.
  let literal = JSON.stringify(argsObj)
  // Escape line separator (U+2028) and paragraph separator (U+2029)
  const lineSep = String.fromCharCode(0x2028)
  const paraSep = String.fromCharCode(0x2029)
  literal = literal.replace(new RegExp(lineSep, 'g'), '\\u2028')
  literal = literal.replace(new RegExp(paraSep, 'g'), '\\u2029')

  // Compute sha256 of the literal.
  const argsSha256 = createHash('sha256').update(literal).digest('hex')

  // Find the marker: must appear exactly once.
  const matches = workflowSrc.match(MARKER_RE)
  if (!matches) throw new Error('marker line not found: const INJECTED_ARGS = null // @@INJECTED_ARGS@@')
  const globalMatches = workflowSrc.match(MARKER_GLOBAL_RE) || []
  if (globalMatches.length !== 1) {
    throw new Error('marker line appears more than once')
  }

  // Replace using a replacer function so $ in the JSON is not interpreted.
  const script = workflowSrc.replace(MARKER_RE, () => {
    return `const INJECTED_ARGS = Object.freeze(${literal}) // @@INJECTED_ARGS@@ sha256=${argsSha256}`
  })

  // Validate the result.
  validate(script)

  return { script, argsSha256 }
}

function sha256File(path) {
  const data = readFileSync(path)
  return createHash('sha256').update(data).digest('hex')
}

function usage() {
  console.error('Usage: node inject_config.mjs --workflow <built.js> --args <args.json> --out <out.js> --manifest <manifest.json>')
  process.exit(2)
}

function main(argv) {
  const params = {}
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--workflow') params.workflow = argv[++i]
    else if (argv[i] === '--args') params.args = argv[++i]
    else if (argv[i] === '--out') params.out = argv[++i]
    else if (argv[i] === '--manifest') params.manifest = argv[++i]
    else { usage() }
  }

  if (!params.workflow || !params.args || !params.out || !params.manifest) {
    usage()
  }

  try {
    // Read arguments JSON file.
    let argsObj
    try {
      const argsJson = readFileSync(params.args, 'utf8')
      argsObj = JSON.parse(argsJson)
    } catch (e) {
      throw new Error(`failed to parse args JSON: ${e.message}`)
    }

    // Read workflow file.
    const workflowSrc = readFileSync(params.workflow, 'utf8')

    // Inject config.
    const { script, argsSha256 } = inject(workflowSrc, argsObj)

    // Ensure output directory exists.
    const outDir = dirname(params.out)
    mkdirSync(outDir, { recursive: true })

    // Write output file (never overwrite).
    try {
      writeFileSync(params.out, script, { flag: 'wx' })
    } catch (e) {
      if (e.code === 'EEXIST') {
        console.error(`${params.out} already exists`)
        process.exit(1)
      }
      throw e
    }

    // Compute hashes for manifest.
    const workflowSha256 = sha256File(params.workflow)
    const outSha256 = sha256File(params.out)

    // Update manifest.
    let manifest = []
    try {
      const manifestJson = readFileSync(params.manifest, 'utf8')
      manifest = JSON.parse(manifestJson)
      if (!Array.isArray(manifest)) throw new Error('manifest must be an array')
    } catch (e) {
      // Manifest may not exist yet; start fresh.
      if (!e.code || e.code !== 'ENOENT') throw e
      manifest = []
    }

    manifest.push({
      workflow: basename(params.out),
      source_path: params.workflow,
      source_sha256: workflowSha256,
      args_sha256: argsSha256,
      out_path: params.out,
      out_sha256: outSha256,
      role_files: roleFiles(workflowSrc),
    })

    mkdirSync(dirname(params.manifest), { recursive: true })
    writeFileSync(params.manifest, JSON.stringify(manifest, null, 2) + '\n')

    console.log(params.out)
    return 0
  } catch (e) {
    console.error(e.message)
    return 1
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exit(main(process.argv.slice(2)))
}
