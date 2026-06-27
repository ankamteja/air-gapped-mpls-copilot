#!/usr/bin/env python3
# =============================================================================
# exporter.py — Multi-Node Prometheus Exporter for FRR & Container Interfaces
#
# This script runs on the HOST. It collects telemetry from all 7 simulation
# containers using 'docker exec' and parses interface statistics + FRR state.
# Exposes a Prometheus metrics endpoint on port 8000.
#
# Zero External Dependencies: Uses python's built-in http.server module.
# =============================================================================
import subprocess
import json
import re
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle each request in a separate thread so Prometheus scrapes don't block the collector."""
    daemon_threads = True

PORT = 8000
LAB_NAME = "chunk3"
NODES = ["pe1", "p1", "pe2", "ce-branch1", "ce-branch2", "ce-hub", "ce-dc"]

def get_container_name(node):
    return f"clab-{LAB_NAME}-{node}"

def exec_cmd(container, cmd):
    try:
        res = subprocess.run(
            f"docker exec {container} {cmd}",
            shell=True,
            capture_output=True,
            text=True,
            timeout=2
        )
        if res.returncode == 0:
            return res.stdout
        return ""
    except Exception:
        return ""

def collect_interface_metrics(node, container):
    """
    Parses /proc/net/dev inside the container to get interface statistics.
    """
    metrics = []
    output = exec_cmd(container, "cat /proc/net/dev")
    if not output:
        return metrics

    # Format of /proc/net/dev lines:
    # face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed
    lines = output.split('\n')
    for line in lines:
        if ':' not in line:
            continue
        parts = line.split(':')
        iface = parts[0].strip()
        # Skip loopback
        if iface == "lo":
            continue
            
        stats = parts[1].split()
        if len(stats) >= 16:
            rx_bytes = stats[0]
            rx_packets = stats[1]
            rx_errors = stats[2]
            rx_drops = stats[3]
            tx_bytes = stats[8]
            tx_packets = stats[9]
            tx_errors = stats[10]
            tx_drops = stats[11]
            
            metrics.extend([
                f'net_rx_bytes{{node="{node}",interface="{iface}"}} {rx_bytes}',
                f'net_rx_packets{{node="{node}",interface="{iface}"}} {rx_packets}',
                f'net_rx_errors{{node="{node}",interface="{iface}"}} {rx_errors}',
                f'net_rx_drops{{node="{node}",interface="{iface}"}} {rx_drops}',
                f'net_tx_bytes{{node="{node}",interface="{iface}"}} {tx_bytes}',
                f'net_tx_packets{{node="{node}",interface="{iface}"}} {tx_packets}',
                f'net_tx_errors{{node="{node}",interface="{iface}"}} {tx_errors}',
                f'net_tx_drops{{node="{node}",interface="{iface}"}} {tx_drops}'
            ])
    return metrics

def collect_frr_metrics(node, container):
    """
    Queries vtysh inside FRR nodes to get routing and neighbor details in JSON.
    """
    metrics = []
    
    # 1. OSPF Neighbors
    ospf_json_str = exec_cmd(container, "vtysh -c 'show ip ospf neighbor json'")
    if ospf_json_str:
        try:
            ospf_data = json.loads(ospf_json_str)
            # FRR JSON structure can vary slightly; handle dictionary or list of neighbors
            neighbors = ospf_data.get("neighbors", {})
            total_neighbors = len(neighbors)
            metrics.append(f'frr_ospf_neighbors_total{{node="{node}"}} {total_neighbors}')

            # FRR OSPF JSON: each key maps to a list of adjacency objects
            for nbr_ip, nbr_entries in neighbors.items():
                entries = nbr_entries if isinstance(nbr_entries, list) else [nbr_entries]
                for nbr_info in entries:
                    state = nbr_info.get("state", nbr_info.get("nbrState", ""))
                    is_full = 1 if "Full" in state else 0
                    metrics.append(f'frr_ospf_neighbor_full{{node="{node}",neighbor="{nbr_ip}"}} {is_full}')
        except json.JSONDecodeError:
            pass

    # 2. BGP VPNv4 Summary (Only for PEs: pe1, pe2)
    if node in ["pe1", "pe2"]:
        bgp_json_str = exec_cmd(container, "vtysh -c 'show bgp ipv4 vpn summary json'")
        if bgp_json_str:
            try:
                bgp_data = json.loads(bgp_json_str)
                peers = bgp_data.get("peers", {})
                for peer_ip, peer_info in peers.items():
                    state = peer_info.get("state", "")
                    is_est = 1 if state.lower() == "established" else 0
                    pfx_rcd = peer_info.get("pfxRcd", 0)
                    metrics.extend([
                        f'frr_bgp_vpn_established{{node="{node}",peer="{peer_ip}"}} {is_est}',
                        f'frr_bgp_vpn_prefixes_received{{node="{node}",peer="{peer_ip}"}} {pfx_rcd}'
                    ])
            except json.JSONDecodeError:
                pass

        # 3. VRF BGP Summary (PE-CE Peerings)
        bgp_vrf_json = exec_cmd(container, "vtysh -c 'show bgp vrf CUST ipv4 unicast summary json'")
        if bgp_vrf_json:
            try:
                bgp_vrf_data = json.loads(bgp_vrf_json)
                peers = bgp_vrf_data.get("peers", {})
                for peer_ip, peer_info in peers.items():
                    state = peer_info.get("state", "")
                    is_est = 1 if state.lower() == "established" else 0
                    pfx_rcd = peer_info.get("pfxRcd", 0)
                    metrics.extend([
                        f'frr_bgp_vrf_established{{node="{node}",vrf="CUST",peer="{peer_ip}"}} {is_est}',
                        f'frr_bgp_vrf_prefixes_received{{node="{node}",vrf="CUST",peer="{peer_ip}"}} {pfx_rcd}'
                    ])
            except json.JSONDecodeError:
                pass

    # 4. LDP Status
    ldp_json_str = exec_cmd(container, "vtysh -c 'show mpls ldp neighbor json'")
    if ldp_json_str:
        try:
            ldp_data = json.loads(ldp_json_str)
            # FRR LDP JSON: {"neighbors": [{neighborId, state, ...}]}
            neighbors = ldp_data if isinstance(ldp_data, list) else ldp_data.get("neighbors", [])
            metrics.append(f'frr_ldp_neighbors_total{{node="{node}"}} {len(neighbors)}')
            for nbr in neighbors:
                # FRR uses "neighborId" (not "peerId") and "state" (not "connectionState")
                peer_id = nbr.get("neighborId", nbr.get("peerId", "unknown"))
                state = nbr.get("state", nbr.get("connectionState", ""))
                is_oper = 1 if "OPERATIONAL" in state.upper() else 0
                metrics.append(f'frr_ldp_session_operational{{node="{node}",peer="{peer_id}"}} {is_oper}')
        except json.JSONDecodeError:
            pass

    return metrics

class MetricsHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress per-request stdout noise at 1 s scrape rate

    def do_GET(self):
        if self.path == '/metrics':
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; version=0.0.4; charset=utf-8')
            self.end_headers()
            
            output_metrics = []
            
            # System CPU/Memory emulation or metrics could be added here
            for node in NODES:
                container = get_container_name(node)
                # Verify container is running
                status = exec_cmd(container, "echo running")
                if not status:
                    output_metrics.append(f'container_running{{node="{node}"}} 0')
                    continue
                
                output_metrics.append(f'container_running{{node="{node}"}} 1')
                
                # Collect Interface telemetry
                iface_m = collect_interface_metrics(node, container)
                output_metrics.extend(iface_m)
                
                # Collect Routing telemetry (only core routers run OSPF/LDP/BGP)
                if node in ["pe1", "p1", "pe2"]:
                    frr_m = collect_frr_metrics(node, container)
                    output_metrics.extend(frr_m)
            
            # Join with newlines
            self.wfile.write("\n".join(output_metrics).encode('utf-8') + b"\n")
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

def run():
    print(f"[*] Starting Air-Gapped Telemetry Exporter on port {PORT}...")
    server = ThreadedHTTPServer(('0.0.0.0', PORT), MetricsHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Shutting down telemetry exporter.")
        server.server_close()

if __name__ == "__main__":
    run()
