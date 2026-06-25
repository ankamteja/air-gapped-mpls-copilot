# Phase 1 — Network Simulation: Progress Log

## Goal
Build a reproducible, air-gapped Containerlab topology simulating SD-WAN over
MPLS that boots cleanly, routes traffic, and supports fault injection. Phase 1
produces realistic telemetry; it does NOT analyze it (that's Phase 2/3).

## Tooling Decisions (locked)
- **OS:** Bare-metal Fedora Linux (MPLS, namespaces, tc netem are Linux-native)
- **Simulator:** Containerlab — declarative YAML, reproducible, lightweight (no VMs)
- **Router software:** FRRouting (FRR) — free, air-gap-friendly, supports MPLS/OSPF/BGP/VRF
- **Runtime:** Docker CE

## Environment Status: WORKING
- Docker CE installed (resolved Fedora moby-engine conflict)
- Containerlab 0.76.1 installed
- User in clab_admins group
- `docker run hello-world` ✓

## Team Git Workflow (rules)
- No direct commits to `master`
- Work on a branch → commit → push → PR (base: master ← compare: branch) → merge
- `git pull origin master` before starting work each day
- Charan owns master / merges PRs
- `.gitignore` excludes Containerlab runtime dirs (clab-*/)

## Phase 1 Chunk Breakdown
- [x] **Chunk 1 — Minimal topology** (2 FRR routers, 1 link, ping works)
- [ ] **Chunk 2 — MPLS core** (2 PE + 1 P, OSPF reachability, LDP labels, verified LSP)
- [ ] **Chunk 3 — CE sites + VPN** (2 branch + 1 hub + 1 DC, L3VPN/VRF, PE-CE BGP)
- [ ] **Chunk 4 — Traffic generation** (iperf3 → VoIP/bulk/web traffic profiles)
- [ ] **Chunk 5 — Fault injection** (Python + tc netem, gradual degradation, labeled)

**Demoable Phase 1 = Chunks 1–3 working. Chunks 4–5 make it good.**

## Progress

### Chunk 1 — DONE (merged PR #1)
- Created `phase1-simulation/topology/step1.clab.yml`: nodes r1, r2 (FRR), one link
- FRR configs: static IPs 10.0.0.1/24 and 10.0.0.2/24 on eth1
- `daemons` files: zebra + ospfd + staticd enabled (ready for Chunk 2)
- `sudo clab deploy -t step1.clab.yml` → both containers running
- `docker exec clab-step1-r1 ping -c 3 10.0.0.2` → **0% packet loss** ✓
- Committed, pushed, PR #1 merged to master

### Team Checkpoint — IN PROGRESS
All 3 teammates must clone + reproduce Chunk 1 ping (0% loss) on their machines
before Chunk 2. Confirms identical environments.
Reproduce steps:
    git clone https://github.com/ankamteja/air-gapped-mpls-copilot
    cd air-gapped-mpls-copilot/phase1-simulation/topology
    docker pull frrouting/frr:latest
    sudo clab deploy -t step1.clab.yml
    docker exec clab-step1-r1 ping -c 3 10.0.0.2
    sudo clab destroy -t step1.clab.yml

## Next Session
Chunk 2 — MPLS core. Build after team checkpoint passes.
