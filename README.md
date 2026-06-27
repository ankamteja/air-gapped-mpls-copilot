# Air-Gapped Predictive Copilot for Secure MPLS Operations

An autonomous, offline AI NOC Copilot that predicts network failures before operational impact and explains reasoning in natural language — with zero cloud dependency.

## Team
- Charan Teja
- Yogeshwar
- Aranya Roy
- Pradyumna

## Problem
Conventional NOC tools are reactive — alerts fire only after service degradation. In air-gapped government/enterprise networks, cloud AI tools are prohibited, leaving operators without intelligent guidance.

## Solution
A fully self-hosted predictive fault analytics platform with:
- Simulated SD-WAN/MPLS topology (Containerlab)
- LSTM-based fault prediction before threshold breach
- Quantized offline LLM (Mistral 7B) with RAG over local runbooks
- Zero outbound network dependency

## Tech Stack
- Network simulation: Containerlab
- Telemetry: Telegraf + Prometheus
- ML models: LSTM, Prophet, ensemble classifiers
- Offline LLM: Mistral 7B (GGUF quantized via Ollama)
- RAG: ChromaDB (local)
- Frontend: FastAPI + HTML dashboard

## Phases
- Phase 1: Network simulation
- Phase 2: Telemetry pipeline
- Phase 3: Predictive models
- Phase 4: Offline LLM deployment
- Phase 5: Copilot integration
- Phase 6: Scenario validation
