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
- MPLS kernel modules (`mpls_router`, `mpls_iptunnel`) load on boot via `/etc/modules-load.d/mpls.conf`
- `docker run hello-world` ✓

## Team Git Workflow (rules)
- No direct commits to `master`
- Work on a branch → commit → push → PR (base: master ← compare: branch) → merge
- `git pull origin master` before starting work each day
- `.gitignore` excludes Containerlab runtime dirs (clab-*/)

## Phase 1 Chunk Breakdown
- [x] **Chunk 1 — Minimal topology** (2 FRR routers, 1 link, ping works)
- [x] **Chunk 2 — MPLS core** (2 PE + 1 P, OSPF reachability, LDP labels, verified LSP)
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

### Chunk 2 — DONE (branch charan/chunk2-mpls-core)
3-router MPLS core: pe1 ←→ p1 ←→ pe2.

- Topology: `phase1-simulation/topology/chunk2.clab.yml` — 3 FRR nodes, 2 links
  (pe1:eth1↔p1:eth1, p1:eth2↔pe2:eth1)
- Configs per router (`configs/pe1`, `configs/p1`, `configs/pe2`):
  - `daemons` — zebra + ospfd + ldpd enabled
  - `frr.conf` — interface IPs, loopbacks (1.1.1.1 / 2.2.2.2 / 3.3.3.3), OSPF, LDP
  - `vtysh.conf` — `service integrated-vtysh-config` (all daemons read one frr.conf)
- Automation: `phase1-simulation/topology/chunk2-setup.sh` — loads MPLS modules,
  sets MPLS sysctls per container, applies OSPF-on-loopback + LDP live, self-verifies
- **Verified:** OSPF neighbors Full, LDP sessions OPERATIONAL, label-switched path
  pe1→pe2 active (label 18→17), `ping pe1→pe2` **0% loss** with ttl=63 (proves
  traffic transits p1 via MPLS, not plain IP)
- Reproducible end-to-end: clean destroy → deploy → setup script passes hands-off

**IP / addressing plan:**

| Router | Role | eth1 | eth2 | Loopback |
|---|---|---|---|---|
| pe1 | Provider Edge | 10.0.1.1/24 | — | 1.1.1.1/32 |
| p1  | Provider Core | 10.0.1.2/24 | 10.0.2.1/24 | 2.2.2.2/32 |
| pe2 | Provider Edge | 10.0.2.2/24 | — | 3.3.3.3/32 |

**Key gotchas hit + fixed (documented in docs/phase1-simulation-doc/chunk2.md):**
1. MPLS kernel modules must be loaded on host (`modprobe mpls_router mpls_iptunnel`)
2. MPLS sysctls (`platform_labels`, `conf.<iface>.input=1`) needed per container, every deploy
3. `mpls ldp` block + `ip ospf area 0` on loopback don't reliably load from frr.conf
   at boot → applied live via vtysh in the setup script
4. Loopbacks MUST be in OSPF or LDP can't form its TCP session (discovery works
   link-local, but the session needs the loopback to be routable)
5. LDP needs OSPF to converge first → setup script waits between steps

### Team Checkpoint — Chunk 1: IN PROGRESS
All 3 teammates must clone + reproduce Chunk 1 ping (0% loss) on their machines.

    git clone https://github.com/ankamteja/air-gapped-mpls-copilot
    cd air-gapped-mpls-copilot/phase1-simulation/topology
    docker pull frrouting/frr:latest
    sudo clab deploy -t step1.clab.yml
    docker exec clab-step1-r1 ping -c 3 10.0.0.2
    sudo clab destroy -t step1.clab.yml

### Team Checkpoint — Chunk 2: PENDING
After Chunk 2 merges, all 3 teammates reproduce the MPLS core before Chunk 3.

    git pull origin master
    cd phase1-simulation/topology
    docker pull frrouting/frr:latest
    sudo clab deploy -t chunk2.clab.yml
    ./chunk2-setup.sh

Script self-verifies — ends with LDP OPERATIONAL + 0% packet loss if the machine is good.

## Next Session
Chunk 3 — CE sites + L3VPN (VRF segmentation, PE-CE BGP). Build after Chunk 2 team
checkpoint passes.
