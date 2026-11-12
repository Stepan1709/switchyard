# ADR 0001: modular monolith with separate processes

Status: accepted

## Context

The system has a data plane (proxy) and a control plane (admin API, calibration, analytics). Their load profiles and failure requirements differ, but the domain is small and tightly connected.

## Decision

One code base and one image with several entry points: `api`, `gateway`, `worker`. Each process is scaled and restarted independently. The code is split into modules with a router -> service -> repository layering. Domain microservices are not introduced.

## Consequences

- One build, one dependency set, simple local setup and CI.
- The gateway must not depend on `routing` or `calibration` modules directly: it reads an immutable snapshot, so the hot path has no database access and survives database outages.
- If a module later needs independent scaling, it can be moved into its own process without changing its boundaries.
