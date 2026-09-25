"""Render the architecture as a PNG — the poster version of README §2.

Mermaid in the README is the *maintained* diagram: it lives in the text, renders on
GitHub, and gets reviewed in a diff. This is the complement — an icon-based image for a
slide, a doc, or the top of the README, with real product marks and grouped tiers.

Why `diagrams` (mingrammer) rather than a hosted tool: it is a Python package over
Graphviz, so the picture is code in this repo, regenerates with `make diagram`, and
costs nothing — the same rule the rest of the stack follows. Hosted editors (Eraser,
Cloudcraft, Excalidraw) look similar and are fine, but the diagram then lives somewhere
that does not get updated when the code does.

Needs Graphviz on PATH (`brew install graphviz`).

Run: `make diagram`   →  docs/img/architecture.png
"""
from __future__ import annotations

import os

from diagrams import Cluster, Diagram, Edge
from diagrams.generic.storage import Storage
from diagrams.onprem.client import User
from diagrams.onprem.compute import Server
from diagrams.onprem.database import PostgreSQL
from diagrams.onprem.mlops import Mlflow
from diagrams.programming.framework import FastAPI, React
from diagrams.programming.language import Python

OUT = "docs/img/architecture"

# Dark canvas, light type — matches the style these diagrams are usually shown in, and
# keeps the tier colours (which carry meaning) readable against it.
GRAPH_ATTR = {
    "bgcolor": "#0d1117", "fontcolor": "#e6edf3", "fontname": "Helvetica",
    "pad": "0.5", "splines": "ortho", "nodesep": "0.4", "ranksep": "1.0",
    "concentrate": "false",
}
NODE_ATTR = {"fontcolor": "#e6edf3", "fontname": "Helvetica", "fontsize": "11"}
# Edge labels need to be BRIGHT on a dark canvas — the first render used the same grey
# as the lines and every label disappeared into the background.
EDGE_ATTR = {"color": "#6e7681", "fontcolor": "#c9d1d9", "fontname": "Helvetica",
             "fontsize": "10"}

# One colour per tier, the same semantics the app uses: green = deterministic and
# trusted, amber = the gate, blue = the model, grey = storage.
TIER = {
    "ui": "#1b4664", "api": "#0f3d3e", "agent": "#243b6b",
    "core": "#14432a", "data": "#3d3222", "ops": "#3b2a4a",
}


def cluster(label: str, tone: str) -> Cluster:
    return Cluster(label, graph_attr={
        "bgcolor": tone, "fontcolor": "#e6edf3", "fontname": "Helvetica",
        "fontsize": "12", "style": "rounded", "color": "#30363d", "penwidth": "1.4",
    })


def build() -> None:
    os.makedirs("docs/img", exist_ok=True)
    with Diagram("Meridian Petroleum — VRR Reasoning & Lineage", filename=OUT,
                 outformat="png", show=False, direction="LR",
                 graph_attr=GRAPH_ATTR, node_attr=NODE_ATTR, edge_attr=EDGE_ATTR):

        analyst = User("Reservoir analyst")

        with cluster("WORKBENCH (web/)", TIER["ui"]):
            ui = React("Portfolio · Report\nLineage · Approvals")
            bot = React("Chatbot")

        with cluster("API (api/) — JWT bearer", TIER["api"]):
            reads = FastAPI("reads\n(public)")
            writes = FastAPI("writes + chat\n(token required)")

        with cluster("AGENT (agent/)", TIER["agent"]):
            router = Python("chat.py\nintent router")
            graph = Python("graph.py\nLangGraph StateGraph")
            tools = Python("tools.py\n15 deterministic tools")

        with cluster("CORE (core/) — pure, no I/O", TIER["core"]):
            physics = Python("physics · decompose")
            gate = Python("faithfulness\nGATE")
            rules = Python("anomaly · audit\nrecommend · approval")

        with cluster("POSTGRESQL + pgvector", TIER["data"]):
            raw = PostgreSQL("vrr_raw")
            curated = PostgreSQL("vrr_curated")
            agentdb = PostgreSQL("vrr_agent")
            knowledge = Storage("reservoir_knowledge\nvector(768)")

        with cluster("OPS", TIER["ops"]):
            mlflow = Mlflow("MLflow\ntraces · eval · prompts")
            ollama = Server("Ollama\nqwen2.5:7b")

        # ---- the paths that matter -------------------------------------------
        analyst >> Edge(color="#58a6ff") >> ui
        analyst >> Edge(color="#58a6ff") >> bot
        ui >> Edge(label="GET") >> reads
        bot >> Edge(label="POST /chat\nBearer token", color="#d29922") >> writes

        reads >> Edge(color="#3fb950") >> tools
        writes >> Edge(color="#3fb950") >> router
        router >> Edge(label="agentic") >> graph
        router >> Edge(label="default") >> tools
        graph >> tools
        graph >> Edge(label="phrasing only", color="#a371f7", style="dashed") >> ollama

        tools >> Edge(color="#3fb950") >> physics
        tools >> Edge(color="#3fb950") >> rules
        tools >> Edge(label="every number") >> curated
        tools >> raw
        tools >> agentdb
        tools >> Edge(label="cosine search") >> knowledge

        graph >> Edge(label="verify narration", color="#d29922") >> gate

        raw >> Edge(label="core.physics") >> curated
        agentdb >> Edge(label="learned rho", style="dashed", color="#8b949e") >> rules

        router >> Edge(label="every span", color="#a371f7", style="dotted") >> mlflow

    print(f"wrote {OUT}.png")


if __name__ == "__main__":
    build()
