from typing import Any, Dict, List, Optional

from db import KnowledgeDB
from llm_client import DualLLMClient


MAP_EXTRACTION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "inferred_primary_app": {
            "type": "string",
            "description": "Name of the primary application this chunk describes (use the [CONTEXT] File Path / Folder Hierarchy / Document Title if not explicitly named in the body).",
        },
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": [
                            "function",
                            "architecture",
                            "upstream",
                            "downstream",
                            "failure_mode",
                        ],
                    },
                    "related_app": {
                        "type": ["string", "null"],
                        "description": "For 'upstream' or 'downstream' facts, the name of the other application/system. Null otherwise.",
                    },
                    "mechanism": {
                        "type": ["string", "null"],
                        "description": "Protocol, tech, queue, or DB used (e.g. REST, Kafka, gRPC, Postgres, Webhook). Null if not mentioned.",
                    },
                    "detail": {
                        "type": "string",
                        "description": "Concise, self-contained factual statement extracted strictly from the text.",
                    },
                },
                "required": ["category", "related_app", "mechanism", "detail"],
                "additionalProperties": False,
            },
        },
        "incomplete_or_missing": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List any explicitly incomplete parts, TBDs, TODOs, or cut-off sections noticed in the text.",
        },
    },
    "required": ["inferred_primary_app", "facts", "incomplete_or_missing"],
    "additionalProperties": False,
}


MD_MAP_SYSTEM_PROMPT = """You are a precise technical fact extractor for software architecture documentation.
You are given a chunk from a Confluence Markdown page.

CRITICAL RULES:
1. Many Confluence pages ONLY state the application name in the [CONTEXT] header (File Path, Folder Hierarchy, or Document Title), while the [CONTENT] body only discusses a specific sub-topic. Always attribute facts in the body to the application indicated by the [CONTEXT] unless the text explicitly states it is describing a different application.
2. Only extract facts explicitly stated in [CONTENT]. NEVER invent or guess details. If the page is brief or incomplete, return only the 1 or 2 facts present (or an empty list).
3. Categorize each fact into one of:
   - "function": What the application does, business logic, workflows, features.
   - "architecture": Internal components, databases, caches, queues, hosting, tech stack, ports, configs.
   - "upstream": Another application/system that calls, triggers, or sends data INTO this primary application. Put the caller's name in `related_app`.
   - "downstream": Another application/system that this primary application calls, depends on, or sends data TO. Put the target system's name in `related_app`.
   - "failure_mode": Known operational risks, bottlenecks, timeouts, or error handling behaviors.
4. If the text mentions "TBD", "TODO", missing diagrams, or cut-off notes, record them in `incomplete_or_missing`.
Respond strictly in JSON matching the schema."""


CSV_MAP_SYSTEM_PROMPT = """You are a precise software architecture & reliability analyst extracting lasting system knowledge from Jira and ServiceNow tickets.

CRITICAL RULES:
1. Analyze the ticket's Short Description, Description, Technical/Work Notes, and Comments.
2. Identify the primary application (`inferred_primary_app`) from the Tagged Application/CI or from the ticket text itself.
3. Extract ONLY lasting architectural, functional, dependency, or failure-mode facts:
   - "upstream": If the ticket reveals that App X calls or sends requests/events to this app.
   - "downstream": If the ticket reveals that this app calls, queries, or fails when connecting to App Y (e.g., "503 timeout calling Fraud API").
   - "architecture": Databases, tables, Kafka topics, queues, servers, ports, or deployment details mentioned in the notes/comments.
   - "function": Business process or feature handled by the app.
   - "failure_mode": How the application fails and how issues are mitigated or recovered.
4. Ignore ephemeral administrative chatter ("joined bridge", "reassigned ticket").
Respond strictly in JSON matching the schema."""


class MapWorker:
    """
    Executes the Map phase over pending chunks using the local Mistral (`llama-server`) model.
    Resumable at any point because every chunk's state is committed to SQLite immediately.
    """

    def __init__(self, db: KnowledgeDB, llm: DualLLMClient):
        self.db = db
        self.llm = llm

    def run_map(self, limit: Optional[int] = None) -> Dict[str, int]:
        pending = self.db.get_pending_chunks(limit=limit)
        stats = {
            "total_pending": len(pending),
            "processed": 0,
            "facts_extracted": 0,
            "errors": 0,
        }

        if not pending:
            print("[Map] No pending chunks to process (all chunks already mapped or skipped as stubs).")
            return stats

        print(f"[Map] Processing {len(pending)} pending chunks using local Mistral...")

        for idx, chunk in enumerate(pending, start=1):
            chunk_id = chunk["id"]
            chunk_uid = chunk["chunk_uid"]
            source_type = chunk["source_type"]
            fallback_app = chunk["inferred_app"] or "unknown-app"

            sys_prompt = (
                MD_MAP_SYSTEM_PROMPT
                if source_type == "md"
                else CSV_MAP_SYSTEM_PROMPT
            )

            print(
                f"  [{idx}/{len(pending)}] Mapping {chunk_uid} (context app: {fallback_app})..."
            )

            try:
                result = self.llm.chat_json(
                    system_prompt=sys_prompt,
                    user_prompt=chunk["full_prompt_text"],
                    json_schema=MAP_EXTRACTION_SCHEMA,
                    schema_name="chunk_fact_extraction",
                    purpose="map_extract",
                    prefer_large=False,  # Always use local Mistral for Map phase
                )

                primary_app = (
                    result.get("inferred_primary_app") or fallback_app
                ).strip()
                if primary_app.lower() in (
                    "unknown",
                    "none",
                    "null",
                    "n/a",
                    "unassigned-ticket-app",
                    "",
                ) and fallback_app not in ("unassigned-ticket-app", ""):
                    primary_app = fallback_app

                raw_facts = result.get("facts") or []
                incomplete_list = result.get("incomplete_or_missing") or []

                normalized_facts: List[Dict[str, Any]] = []
                for rf in raw_facts:
                    if not isinstance(rf, dict):
                        continue
                    detail = (rf.get("detail") or "").strip()
                    if not detail:
                        continue
                    cat = (rf.get("category") or "function").strip().lower()
                    if cat not in (
                        "function",
                        "architecture",
                        "upstream",
                        "downstream",
                        "failure_mode",
                    ):
                        cat = "function"

                    normalized_facts.append(
                        {
                            "raw_app_name": primary_app,
                            "category": cat,
                            "related_app": rf.get("related_app"),
                            "mechanism": rf.get("mechanism"),
                            "detail": detail,
                        }
                    )

                for inc in incomplete_list:
                    if isinstance(inc, str) and inc.strip():
                        normalized_facts.append(
                            {
                                "raw_app_name": primary_app,
                                "category": "incomplete_gap",
                                "related_app": None,
                                "mechanism": None,
                                "detail": f"Incomplete documentation noted in {chunk['file_path']} ({chunk['section_header']}): {inc.strip()}",
                            }
                        )

                self.db.save_chunk_facts(
                    chunk_id=chunk_id,
                    chunk_uid=chunk_uid,
                    source_type=source_type,
                    facts=normalized_facts,
                )
                stats["processed"] += 1
                stats["facts_extracted"] += len(normalized_facts)

            except Exception as e:
                print(f"    [Error] Failed on {chunk_uid}: {e}")
                self.db.mark_chunk_error(chunk_id, str(e))
                stats["errors"] += 1

        return stats
