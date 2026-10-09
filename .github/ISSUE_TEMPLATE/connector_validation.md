---
name: Connector validation
about: Report exact tested source coverage or a reproducible adapter gap
title: ""
labels: ""
assignees: ""
---

## Source and coverage

- Connector type:
- Exact resources and relevant non-sensitive parameters:
- Oriel, Python, runtime and optional driver versions:
- Evidence type: controlled test / native local fixture / authorized live connection

## What passed or failed?

Describe authentication method by field or environment-variable name only. Include pagination, selected-resource behavior, expected rows, refresh and partial-failure behavior where tested.

## Reproduction

Prefer synthetic inputs and local fixtures. Never include token values, connection strings, account identifiers, customer rows or raw private logs. A successful connection to one resource does not establish coverage of the whole connector.

## Remaining limits

What was not tested? Which additional dependencies or permissions were needed?
