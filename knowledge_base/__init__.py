"""Persistent, claim-based knowledge base for the discovery agents.

Layers, each importing only from the ones before it: `schemas` (record types and per-record rules),
`validate` (rules across records), `store` (append-only JSONL files and the single writer),
`status` (computed epistemic status, mechanism graph), `retrieve` (deterministic slices for agents).
"""
