import json
from datetime import datetime, timezone
import uuid

from taxonomy import DISPLAY_TO_INFO

# =============================================================================
# acp_manager.py — Anomaly Context Packet (ACP) Struct & Schema Manager
#
# The ACP acts as the structured handoff between the stochastic ML models,
# the deterministic Graph-Analytical Engine, and the Edge Policy Engine (EPE).
# =============================================================================

ACP_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "AnomalyContextPacket",
    "type": "object",
    "properties": {
        "acp_id": {"type": "string"},
        "timestamp": {"type": "string", "format": "date-time"},
        "trigger_source": {"type": "string", "enum": ["ML_ENGINE", "GRAPH_ENGINE", "OPERATOR"]},
        "severity": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"]},
        "telemetry_snapshot": {
            "type": "object",
            "properties": {
                "nodes": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "object",
                        "properties": {
                            "cpu_utilization": {"type": "number"},
                            "memory_utilization": {"type": "number"},
                            "interfaces": {
                                "type": "object",
                                "additionalProperties": {
                                    "type": "object",
                                    "properties": {
                                        "rx_bytes_sec": {"type": "number"},
                                        "tx_bytes_sec": {"type": "number"},
                                        "rx_drops": {"type": "number"},
                                        "tx_drops": {"type": "number"},
                                        "rx_errors": {"type": "number"},
                                        "tx_errors": {"type": "number"}
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "required": ["nodes"]
        },
        "ml_analysis": {
            "type": "object",
            "properties": {
                "anomaly_detected": {"type": "boolean"},
                "reconstruction_loss": {"type": "number"},
                "predicted_fault_class": {"type": "string"},
                "confidence_score": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "estimated_time_to_failure_sec": {"type": "number"}
            },
            "required": ["anomaly_detected", "predicted_fault_class", "confidence_score"]
        },
        "graph_analysis": {
            "type": "object",
            "properties": {
                "bottleneck_links": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source": {"type": "string"},
                            "target": {"type": "string"},
                            "capacity_bps": {"type": "number"},
                            "projected_load_bps": {"type": "number"},
                            "saturation_ratio": {"type": "number"}
                        }
                    }
                },
                "paths_impacted": {
                    "type": "array",
                    "items": {"type": "string"}
                }
            },
            "required": ["bottleneck_links", "paths_impacted"]
        },
        "corroboration": {
            "type": "object",
            "properties": {
                "engines_agree": {"type": "boolean"},
                "rationale": {"type": "string"},
                "recommended_action": {"type": "string"},
                "execution_mode": {"type": "string", "enum": ["AUTO_EXECUTE", "RECOMMEND_ONLY", "ROLLBACK"]}
            },
            "required": ["engines_agree", "recommended_action", "execution_mode"]
        }
    },
    "required": ["acp_id", "timestamp", "trigger_source", "severity", "telemetry_snapshot", "ml_analysis", "graph_analysis", "corroboration"]
}

class AnomalyContextPacket:
    def __init__(self, trigger_source="ML_ENGINE", severity="MEDIUM"):
        self.acp_id = str(uuid.uuid4())
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.trigger_source = trigger_source
        self.severity = severity
        self.telemetry_snapshot = {"nodes": {}}
        self.ml_analysis = {
            "anomaly_detected": False,
            "reconstruction_loss": 0.0,
            "predicted_fault_class": "Healthy",
            "confidence_score": 1.0,
            "estimated_time_to_failure_sec": -1.0
        }
        self.graph_analysis = {
            "bottleneck_links": [],
            "paths_impacted": []
        }
        self.corroboration = {
            "engines_agree": False,
            "rationale": "No analysis run yet.",
            "recommended_action": "NO_ACTION",
            "execution_mode": "RECOMMEND_ONLY"
        }

    def set_telemetry(self, nodes_data):
        self.telemetry_snapshot["nodes"] = nodes_data

    def set_ml_results(self, detected, loss, fault_class, confidence, ttf=-1.0):
        self.ml_analysis = {
            "anomaly_detected": detected,
            "reconstruction_loss": float(loss),
            "predicted_fault_class": str(fault_class),
            "confidence_score": float(confidence),
            "estimated_time_to_failure_sec": float(ttf)
        }
        # Escalate severity based on classification. A fault flagged 'critical'
        # in the taxonomy (e.g. control-plane flap) or an imminent breach
        # (TTF < 30s) raises the ACP to CRITICAL.
        if detected:
            is_critical = DISPLAY_TO_INFO.get(str(fault_class), {}).get("critical", False)
            if is_critical or (0 <= ttf < 30):
                self.severity = "CRITICAL"
            else:
                self.severity = "HIGH"

    def set_graph_results(self, bottlenecks, paths):
        self.graph_analysis = {
            "bottleneck_links": bottlenecks,
            "paths_impacted": paths
        }

    def set_corroboration(self, agree, rationale, action, mode):
        self.corroboration = {
            "engines_agree": bool(agree),
            "rationale": str(rationale),
            "recommended_action": str(action),
            "execution_mode": str(mode)
        }

    def to_json(self):
        return json.dumps(self.__dict__, indent=2)

    def write_to_file(self, filepath):
        with open(filepath, 'w') as f:
            f.write(self.to_json())
        print(f"[+] ACP written successfully to {filepath}")

    @classmethod
    def load_from_file(cls, filepath):
        with open(filepath, 'r') as f:
            data = json.load(f)
        acp = cls()
        acp.acp_id = data["acp_id"]
        acp.timestamp = data["timestamp"]
        acp.trigger_source = data["trigger_source"]
        acp.severity = data["severity"]
        acp.telemetry_snapshot = data["telemetry_snapshot"]
        acp.ml_analysis = data["ml_analysis"]
        acp.graph_analysis = data["graph_analysis"]
        acp.corroboration = data["corroboration"]
        return acp
