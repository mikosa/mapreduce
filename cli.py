#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

import yaml

from db import KnowledgeDB
from ingest import Ingester
from llm_client import DualLLMClient
from map_worker import MapWorker
from query import DynamicTopicEngine
from resolve_and_reduce import ResolverAndReducer


def load_config(config_path: str) -> Dict[str, Any]:
    p = Path(config_path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hybrid MapReduce + Dynamic Topic Drill-Down System for Application Knowledge (.md & .csv)"
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to configuration YAML file (default: config.yaml)",
    )
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="Force 100%% local Mistral execution (never call the Large API Model).",
    )
    parser.add_argument(
        "--use-large-model",
        action="store_true",
        help="Allow sparse high-value stages (Entity Resolution, Reduce, Query Synthesis) to use the configured Large API Model within budget.",
    )
    parser.add_argument(
        "--mock-llm",
        action="store_true",
        help="Run with deterministic heuristic mock LLM (for testing pipeline offline without llama-server).",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # 1. ingest
    p_ingest = sub.add_parser(
        "ingest",
        help="Ingest nested Confluence .md directories and Jira/ServiceNow .csv files into SQLite + FTS5.",
    )
    p_ingest.add_argument(
        "paths",
        nargs="+",
        help="One or more directories or .md / .csv files to ingest.",
    )
    p_ingest.add_argument(
        "--force",
        action="store_true",
        help="Re-ingest files even if their SHA256 hash has not changed.",
    )

    # 2. map
    p_map = sub.add_parser(
        "map",
        help="Run resumable Map phase on pending chunks using local llama-server (Mistral).",
    )
    p_map.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional max number of pending chunks to process in this run.",
    )

    # 3. reduce
    p_reduce = sub.add_parser(
        "reduce",
        help="Resolve application aliases, link cross-app upstream/downstream dependencies, and synthesize Application Dossiers.",
    )
    p_reduce.add_argument(
        "--app",
        default=None,
        help="Optional application name to reduce (default: reduce all discovered applications).",
    )

    # 4. run-all
    p_all = sub.add_parser(
        "run-all",
        help="Run Ingest -> Map -> Reduce end-to-end.",
    )
    p_all.add_argument(
        "paths",
        nargs="+",
        help="One or more directories or .md / .csv files to ingest and process.",
    )
    p_all.add_argument(
        "--force-ingest",
        action="store_true",
        help="Force re-ingestion of unchanged files.",
    )

    # 5. query (Dynamic Drill-Down)
    p_query = sub.add_parser(
        "query",
        help="Dynamically fetch deeper information on a specific topic from raw chunks and append to the application dossier.",
    )
    p_query.add_argument(
        "--topic",
        required=True,
        help="The specific topic or question to investigate (e.g., 'Kafka dead letter queue and retry policy').",
    )
    p_query.add_argument(
        "--app",
        default=None,
        help="Optional application to scope the drill-down to (e.g., 'checkout-service').",
    )
    p_query.add_argument(
        "--max-chunks",
        type=int,
        default=12,
        help="Max candidate raw chunks to inspect during the targeted Map step (default: 12).",
    )

    # 6. status
    sub.add_parser(
        "status",
        help="Display knowledge base statistics, discovered applications, incomplete stubs, and LLM call usage.",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    config = load_config(args.config)
    db_path = config.get("database_path", "knowledge.db")
    db = KnowledgeDB(db_path)

    use_large_override = True if args.use_large_model else None
    llm = DualLLMClient(
        config=config,
        db=db,
        force_local_only=args.local_only,
        use_large_override=use_large_override,
        mock_mode=args.mock_llm,
    )

    try:
        if args.command == "ingest":
            ingester = Ingester(config, db)
            stats = ingester.ingest_paths(args.paths, force_reingest=args.force)
            print("\n=== Ingestion Summary ===")
            print(json.dumps(stats, indent=2))

        elif args.command == "map":
            worker = MapWorker(db, llm)
            stats = worker.run_map(limit=args.limit)
            print("\n=== Map Phase Summary ===")
            print(json.dumps(stats, indent=2))

        elif args.command == "reduce":
            reducer = ResolverAndReducer(config, db, llm)
            generated = reducer.run_reduce(target_app=args.app)
            print("\n=== Reduce Phase Summary ===")
            for app_name, path in generated:
                print(f"  - {app_name}: {path}")

        elif args.command == "run-all":
            ingester = Ingester(config, db)
            in_stats = ingester.ingest_paths(
                args.paths, force_reingest=args.force_ingest
            )
            print("\n=== 1. Ingestion Summary ===")
            print(json.dumps(in_stats, indent=2))

            worker = MapWorker(db, llm)
            map_stats = worker.run_map()
            print("\n=== 2. Map Phase Summary ===")
            print(json.dumps(map_stats, indent=2))

            reducer = ResolverAndReducer(config, db, llm)
            generated = reducer.run_reduce()
            print("\n=== 3. Reduce Phase Summary ===")
            for app_name, path in generated:
                print(f"  - {app_name}: {path}")

        elif args.command == "query":
            engine = DynamicTopicEngine(config, db, llm)
            res = engine.drill_down(
                topic=args.topic,
                app=args.app,
                max_chunks=args.max_chunks,
            )
            print("\n" + "=" * 72)
            print(
                f"DYNAMIC TOPIC DEEP-DIVE: {res['topic']} (App: {res.get('canonical_app') or 'ALL'})"
            )
            print("=" * 72 + "\n")
            print(res["markdown"])
            print("\n" + "=" * 72)

        elif args.command == "status":
            stats = db.get_stats()
            apps = db.get_canonical_apps()
            print("\n=== Knowledge Store Status ===")
            print(json.dumps(stats, indent=2))
            print(f"\nCanonical Applications ({len(apps)}):")
            for a in apps:
                print(f"  - {a}")

    finally:
        db.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
