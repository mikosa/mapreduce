from pathlib import Path
from typing import Any, Dict, List, Optional

from db import KnowledgeDB
from llm_client import DualLLMClient
from resolve_and_reduce import ResolverAndReducer


TOPIC_MAP_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "relevant": {
            "type": "boolean",
            "description": "True if this chunk contains any information related to the requested topic.",
        },
        "findings": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Specific technical details, configurations, dependencies, or incident notes from this chunk that address the topic.",
        },
        "missing_info": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Any parts related to the topic that are explicitly marked TBD, incomplete, or unresolved in this chunk.",
        },
    },
    "required": ["relevant", "findings", "missing_info"],
    "additionalProperties": False,
}


class DynamicTopicEngine:
    """
    Implements on-demand Targeted MapReduce when the user wants deeper information
    on a specific topic (for one application or across all applications).
    """

    def __init__(
        self,
        config: Dict[str, Any],
        db: KnowledgeDB,
        llm: DualLLMClient,
    ):
        self.config = config
        self.db = db
        self.llm = llm
        self.output_dir = Path(config.get("output_dir", "output"))

    def drill_down(
        self,
        topic: str,
        app: Optional[str] = None,
        max_chunks: int = 12,
    ) -> Dict[str, Any]:
        # Resolve canonical app name if provided
        canonical_app = None
        if app:
            alias_row = self.db.conn.execute(
                "SELECT canonical_app FROM app_aliases WHERE raw_name_key = ?",
                (app.strip().lower(),),
            ).fetchone()
            canonical_app = (
                alias_row["canonical_app"]
                if alias_row
                else ResolverAndReducer._to_kebab(app)
            )

        scope_label = f"application `{canonical_app}`" if canonical_app else "ALL applications"
        print(
            f"[Dynamic Drill-Down] Searching raw chunks (.md + .csv) for topic: '{topic}' ({scope_label})..."
        )

        matched_chunks = self.db.search_chunks_for_topic(
            topic=topic,
            canonical_app=canonical_app,
            limit=max_chunks,
        )

        if not matched_chunks:
            msg = f"No matching raw chunks found for topic '{topic}' ({scope_label})."
            print(f"  [Dynamic Drill-Down] {msg}")
            return {
                "topic": topic,
                "canonical_app": canonical_app,
                "chunks_inspected": 0,
                "relevant_chunks": 0,
                "markdown": msg,
            }

        print(
            f"[Dynamic Drill-Down] Running Targeted Map with local Mistral on {len(matched_chunks)} candidate chunks..."
        )

        map_sys_prompt = (
            "You are a targeted technical investigator. "
            "Given a raw Confluence Markdown chunk or Jira/ServiceNow ticket (with its [CONTEXT] path/title header), "
            "extract EVERY specific detail, configuration, dependency, error, or note related to the user's requested TOPIC. "
            "Remember that the application name may only appear in the [CONTEXT] File Path or Document Title. "
            "If the chunk has nothing to do with the topic, set `relevant` to false and return empty lists."
        )

        extracted_notes: List[Dict[str, Any]] = []
        supporting_uids: List[str] = []

        for idx, ch in enumerate(matched_chunks, start=1):
            uid = ch["chunk_uid"]
            user_prompt = (
                f"TARGET TOPIC / QUESTION: {topic}\n"
                f"TARGET APPLICATION: {canonical_app or 'Any relevant application'}\n\n"
                f"{ch['full_prompt_text']}"
            )
            res = self.llm.chat_json(
                system_prompt=map_sys_prompt,
                user_prompt=user_prompt,
                json_schema=TOPIC_MAP_SCHEMA,
                schema_name="topic_targeted_map",
                purpose="topic_map",
                prefer_large=False,  # Always use free local Mistral to scan raw chunks
            )
            findings = [
                f.strip() for f in (res.get("findings") or []) if f and f.strip()
            ]
            missing = [
                m.strip() for m in (res.get("missing_info") or []) if m and m.strip()
            ]
            if res.get("relevant") and (findings or missing):
                supporting_uids.append(uid)
                extracted_notes.append(
                    {
                        "chunk_uid": uid,
                        "file_path": ch["file_path"],
                        "section_or_ticket": ch["section_header"] or ch["title"],
                        "inferred_app": ch["inferred_app"],
                        "findings": findings,
                        "missing_info": missing,
                    }
                )
                print(
                    f"  [{idx}/{len(matched_chunks)}] {uid} -> {len(findings)} finding(s)"
                )
            else:
                print(f"  [{idx}/{len(matched_chunks)}] {uid} -> not relevant")

        if not extracted_notes:
            md = (
                f"Inspected {len(matched_chunks)} raw chunks for **{topic}** ({scope_label}), "
                f"but none contained specific details on this topic. "
                f"(The relevant Confluence pages may be incomplete stubs or this topic is undocumented.)"
            )
            return {
                "topic": topic,
                "canonical_app": canonical_app,
                "chunks_inspected": len(matched_chunks),
                "relevant_chunks": 0,
                "markdown": md,
            }

        # Step 3: Targeted Reduce (Uses Large Model if enabled & within budget, otherwise Local Mistral)
        use_large = self.llm.can_use_large_model("use_for_query_synthesis")
        tier_desc = "Large API Model" if use_large else "Local Mistral"
        print(
            f"[Dynamic Drill-Down] Synthesizing deep-dive answer from {len(extracted_notes)} relevant chunks using {tier_desc}..."
        )

        evidence_blocks = []
        for item in extracted_notes:
            lines = [
                f"Source `[{item['chunk_uid']}]` (Path: `{item['file_path']}`, Context App: `{item['inferred_app']}`):"
            ]
            for f in item["findings"]:
                lines.append(f"  - [Finding] {f} `[{item['chunk_uid']}]`")
            for m in item["missing_info"]:
                lines.append(f"  - [Incomplete/Missing] {m} `[{item['chunk_uid']}]`")
            evidence_blocks.append("\n".join(lines))

        reduce_sys_prompt = (
            "You are a senior systems architect answering a targeted deep-dive question "
            "based on freshly extracted evidence from Confluence pages and Jira/ServiceNow tickets. "
            "Organize your response clearly in Markdown, preserve `[chunk_uid]` citations for every claim, "
            "and explicitly note if any part of the topic is marked TBD or incomplete in the source docs."
        )
        reduce_user_prompt = (
            f"Deep-Dive Topic: {topic}\n"
            f"Application Scope: {canonical_app or 'All Applications'}\n\n"
            f"Extracted Evidence:\n\n" + "\n\n".join(evidence_blocks)
        )

        synthesized_md, model_used = self.llm.chat_text(
            system_prompt=reduce_sys_prompt,
            user_prompt=reduce_user_prompt,
            purpose="topic_reduce",
            prefer_large=use_large,
        )

        self.db.save_topic_deep_dive(
            canonical_app=canonical_app or "ALL_APPS",
            topic=topic,
            extracted_notes=extracted_notes,
            synthesized_markdown=synthesized_md,
            supporting_chunk_uids=supporting_uids,
            model_used=model_used,
        )

        # Always save a standalone .md file for this topic query in output/queries/
        queries_dir = self.output_dir / "queries"
        queries_dir.mkdir(parents=True, exist_ok=True)
        topic_slug = ResolverAndReducer._to_kebab(topic)[:60]
        app_slug = canonical_app or "all-apps"
        query_md_path = queries_dir / f"{app_slug}__{topic_slug}.md"
        standalone_query_md = (
            f"# Topic Deep-Dive: {topic}\n\n"
            f"- **Application Scope**: `{canonical_app or 'ALL_APPS'}`\n"
            f"- **Synthesized By**: `{model_used}`\n"
            f"- **Supporting Chunks**: `{', '.join(supporting_uids)}`\n\n"
            f"---\n\n{synthesized_md}\n"
        )
        query_md_path.write_text(standalone_query_md, encoding="utf-8")
        print(f"[Dynamic Drill-Down] Saved standalone topic Markdown file: {query_md_path}")

        # If scoped to an application, automatically update its Markdown dossier!
        if canonical_app:
            reducer = ResolverAndReducer(self.config, self.db, self.llm)
            facts = self.db.get_facts_for_app(canonical_app)
            if facts:
                out_path = reducer._reduce_single_app(canonical_app, facts)
                print(
                    f"[Dynamic Drill-Down] Appended deep-dive section to dossier: {out_path}"
                )

        return {
            "topic": topic,
            "canonical_app": canonical_app,
            "chunks_inspected": len(matched_chunks),
            "relevant_chunks": len(extracted_notes),
            "model_used": model_used,
            "supporting_chunk_uids": supporting_uids,
            "query_md_path": str(query_md_path),
            "markdown": synthesized_md,
        }
