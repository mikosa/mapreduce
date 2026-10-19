import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from db import KnowledgeDB
from llm_client import DualLLMClient


ENTITY_RESOLUTION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "aliases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "raw_name": {"type": "string"},
                    "canonical_app": {
                        "type": "string",
                        "description": "Standardized kebab-case canonical application identifier (e.g. 'checkout-service').",
                    },
                },
                "required": ["raw_name", "canonical_app"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["aliases"],
    "additionalProperties": False,
}


class ResolverAndReducer:
    """
    Handles the Shuffle (Entity Resolution + Cross-App Dependency Linking)
    and Reduce (Per-Application Dossier Synthesis + Global Dependency Graph) phases.
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
        self.apps_dir = self.output_dir / "apps"
        self.apps_dir.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------------------
    # Step 1: Entity Resolution (Shuffle Prep)
    # -------------------------------------------------------------------------
    def resolve_entities(self) -> Dict[str, str]:
        raw_names = self.db.get_all_distinct_app_mentions()
        if not raw_names:
            return {}

        # 1. Deterministic baseline normalization (kebab-case + stripping common suffixes for matching)
        baseline_map: Dict[str, str] = {}
        canonical_by_stem: Dict[str, str] = {}

        # Prefer folder-derived app names as canonical anchors
        folder_apps = [
            r["inferred_app"]
            for r in self.db.conn.execute(
                "SELECT DISTINCT inferred_app FROM documents WHERE source_type = 'md' AND inferred_app IS NOT NULL"
            ).fetchall()
        ]
        for fa in sorted(set(folder_apps)):
            kebab = self._to_kebab(fa)
            stem = self._core_stem(kebab)
            canonical_by_stem[stem] = kebab
            baseline_map[fa] = kebab

        for name in raw_names:
            kebab = self._to_kebab(name)
            stem = self._core_stem(kebab)
            if stem in canonical_by_stem:
                baseline_map[name] = canonical_by_stem[stem]
            else:
                canonical_by_stem[stem] = kebab
                baseline_map[name] = kebab

        # 2. Optional LLM Entity Resolution pass (1 single call across all unique names)
        if len(raw_names) > 1:
            use_large = self.llm.can_use_large_model("use_for_entity_resolution")
            sys_prompt = (
                "You are an enterprise architecture data normalizer. "
                "Given a list of raw application/service names extracted from Confluence folders, "
                "pages, and Jira/ServiceNow tickets, group synonyms, abbreviations, and aliases "
                "under a single canonical kebab-case application name (e.g., 'Checkout Service', "
                "'CHKOUT', and 'checkout-service' -> 'checkout-service'). "
                "Prefer existing kebab-case folder names when available."
            )
            names_bullets = "\n".join(f'- "{n}"' for n in raw_names)
            user_prompt = (
                f"Folder-anchored application names:\n{', '.join(sorted(set(folder_apps)))}\n\n"
                f"All discovered raw application mentions to resolve:\n{names_bullets}"
            )
            try:
                res = self.llm.chat_json(
                    system_prompt=sys_prompt,
                    user_prompt=user_prompt,
                    json_schema=ENTITY_RESOLUTION_SCHEMA,
                    schema_name="entity_resolution",
                    purpose="entity_resolution",
                    prefer_large=use_large,
                )
                for item in res.get("aliases", []):
                    rn = item.get("raw_name")
                    ca = item.get("canonical_app")
                    if rn and ca:
                        baseline_map[rn] = self._to_kebab(ca)
            except Exception as e:
                print(
                    f"  [Entity Resolution] LLM pass skipped ({e}); using deterministic folder/stem matching."
                )

        self.db.save_alias_mapping(baseline_map, method="hybrid")
        return baseline_map

    # -------------------------------------------------------------------------
    # Step 2: Reduce Phase (Dossier Generation)
    # -------------------------------------------------------------------------
    def run_reduce(
        self, target_app: Optional[str] = None
    ) -> List[Tuple[str, Path]]:
        self.resolve_entities()
        all_apps = self.db.get_canonical_apps()
        if target_app:
            target_kebab = self._to_kebab(target_app)
            all_apps = [
                a
                for a in all_apps
                if a.lower() == target_app.lower()
                or a.lower() == target_kebab.lower()
                or target_app.lower() in a.lower()
            ]

        generated: List[Tuple[str, Path]] = []
        for app in all_apps:
            if app.lower() in ("unassigned-ticket-app", "unknown-app", "unknown"):
                continue
            facts = self.db.get_facts_for_app(app)
            if not facts:
                continue

            print(
                f"[Reduce] Synthesizing dossier for '{app}' ({len(facts)} total facts including cross-app links)..."
            )
            dossier_path = self._reduce_single_app(app, facts)
            generated.append((app, dossier_path))

        catalog_path = self._write_global_catalog()
        print(f"[Reduce] Global system catalog written to {catalog_path}")
        return generated

    def _reduce_single_app(
        self, canonical_app: str, facts: List[Dict[str, Any]]
    ) -> Path:
        # Group facts by category and deduplicate identical details
        by_cat: Dict[str, List[Dict[str, Any]]] = {
            "function": [],
            "architecture": [],
            "upstream": [],
            "downstream": [],
            "failure_mode": [],
            "incomplete_gap": [],
        }
        seen_keys = set()
        supporting_uids: List[str] = []

        for f in facts:
            cat = f["category"] if f["category"] in by_cat else "function"
            dedup_key = (
                cat,
                (f.get("canonical_related_app") or "").lower(),
                f["detail"].strip().lower(),
            )
            supporting_uids.append(f["chunk_uid"])
            if dedup_key not in seen_keys:
                seen_keys.add(dedup_key)
                by_cat[cat].append(f)

        # Build deterministic Mermaid diagram + dependency table from structured facts
        mermaid_and_table_md = self._build_dependency_diagram_and_table(
            canonical_app, by_cat["upstream"], by_cat["downstream"]
        )

        # Decide whether to use Large Model (1 single synthesis call for this app)
        # or Local Mistral (focused category-by-category synthesis calls)
        use_large = self.llm.can_use_large_model("use_for_reduce")

        if use_large:
            (
                function_md,
                architecture_md,
                dep_narrative_md,
                gaps_and_issues_md,
                model_used,
            ) = self._synthesize_with_large_model(canonical_app, by_cat)
        else:
            (
                function_md,
                architecture_md,
                dep_narrative_md,
                gaps_and_issues_md,
                model_used,
            ) = self._synthesize_with_local_mistral(canonical_app, by_cat)

        dependencies_md = f"{mermaid_and_table_md}\n\n### Dependency Notes\n{dep_narrative_md}"

        # Collect existing deep dives for this app so re-running reduce preserves them
        existing_dives = self.db.conn.execute(
            "SELECT topic, synthesized_markdown, created_at FROM topic_deep_dives WHERE LOWER(canonical_app) = LOWER(?) ORDER BY id ASC",
            (canonical_app,),
        ).fetchall()
        deep_dives_section = ""
        if existing_dives:
            parts = ["\n---\n\n## 5. Dynamic Topic Deep-Dives"]
            for d in existing_dives:
                parts.append(
                    f"\n### Topic: {d['topic']} *(Generated {d['created_at']})*\n\n{d['synthesized_markdown']}"
                )
            deep_dives_section = "\n".join(parts)

        # Source provenance table
        unique_sources = {}
        for f in facts:
            uid = f["chunk_uid"]
            if uid not in unique_sources:
                unique_sources[uid] = (
                    f"`{uid}` — `{f['file_path']}` ({f.get('section_header') or f.get('title') or ''})"
                )
        sources_list_md = "\n".join(
            f"- {desc}" for _, desc in sorted(unique_sources.items())
        )

        full_markdown = (
            f"# Application Dossier: `{canonical_app}`\n\n"
            f"> **Synthesized by**: `{model_used}` | **Supporting Chunks**: `{len(unique_sources)}` | **Extracted Facts**: `{len(facts)}`\n\n"
            f"## 1. How the Application Functions\n\n{function_md}\n\n"
            f"## 2. Architecture & Technical Stack\n\n{architecture_md}\n\n"
            f"## 3. Upstream & Downstream Dependencies\n\n{dependencies_md}\n\n"
            f"## 4. Operational Failure Modes & Incomplete Documentation Gaps\n\n{gaps_and_issues_md}"
            f"{deep_dives_section}\n\n"
            f"---\n\n## Provenance & Source Index\n\n{sources_list_md}\n"
        )

        self.db.save_dossier(
            canonical_app=canonical_app,
            function_md=function_md,
            architecture_md=architecture_md,
            dependencies_md=dependencies_md,
            gaps_and_issues_md=gaps_and_issues_md,
            full_markdown=full_markdown,
            supporting_chunk_uids=supporting_uids,
            model_used=model_used,
        )

        out_file = self.apps_dir / f"{canonical_app}.md"
        out_file.write_text(full_markdown, encoding="utf-8")
        return out_file

    def _synthesize_with_local_mistral(
        self,
        canonical_app: str,
        by_cat: Dict[str, List[Dict[str, Any]]],
    ) -> Tuple[str, str, str, str, str]:
        """
        Category-by-Category Reduce tailored for a small local 7B model (Mistral).
        Each call only sees a small, single-category list of facts so it stays accurate.
        """
        sys_prompt = (
            "You are a technical writer synthesizing extracted facts into a clear, "
            "concise Markdown section for an application dossier. "
            "Keep every `[chunk_uid]` citation intact at the end of the bullet or sentence it supports. "
            "Do not invent facts beyond what is provided."
        )

        func_bullets = self._format_fact_bullets(by_cat["function"])
        if func_bullets:
            function_md, model_used = self.llm.chat_text(
                system_prompt=sys_prompt,
                user_prompt=(
                    f"Application: {canonical_app}\n"
                    f"Synthesize how `{canonical_app}` functions based strictly on these facts (preserve `[chunk_uid]` citations):\n\n{func_bullets}"
                ),
                purpose="reduce_function",
                prefer_large=False,
            )
        else:
            function_md = "*No functional overview documented yet in Confluence or tickets (pages may be incomplete stubs).*"
            model_used = "local:mistral"

        arch_bullets = self._format_fact_bullets(by_cat["architecture"])
        if arch_bullets:
            architecture_md, model_used = self.llm.chat_text(
                system_prompt=sys_prompt,
                user_prompt=(
                    f"Application: {canonical_app}\n"
                    f"Synthesize the internal architecture, databases, queues, protocols, and tech stack of `{canonical_app}` (preserve `[chunk_uid]` citations):\n\n{arch_bullets}"
                ),
                purpose="reduce_architecture",
                prefer_large=False,
            )
        else:
            architecture_md = "*No internal architecture details extracted yet.*"

        dep_facts = by_cat["upstream"] + by_cat["downstream"]
        dep_bullets = self._format_fact_bullets(dep_facts)
        if dep_bullets:
            dep_narrative_md, model_used = self.llm.chat_text(
                system_prompt=sys_prompt,
                user_prompt=(
                    f"Application: {canonical_app}\n"
                    f"Summarize the upstream callers and downstream dependencies of `{canonical_app}` and how they interact (preserve `[chunk_uid]` citations):\n\n{dep_bullets}"
                ),
                purpose="reduce_dependencies",
                prefer_large=False,
            )
        else:
            dep_narrative_md = "*No upstream or downstream dependencies found.*"

        issues_and_gaps = by_cat["failure_mode"] + by_cat["incomplete_gap"]
        issues_bullets = self._format_fact_bullets(issues_and_gaps)
        if issues_bullets:
            gaps_and_issues_md, model_used = self.llm.chat_text(
                system_prompt=sys_prompt,
                user_prompt=(
                    f"Application: {canonical_app}\n"
                    f"Summarize (a) known operational failure modes from tickets/docs and (b) incomplete/TBD Confluence pages that need more documentation (preserve `[chunk_uid]` citations):\n\n{issues_bullets}"
                ),
                purpose="reduce_gaps_issues",
                prefer_large=False,
            )
        else:
            gaps_and_issues_md = "*No operational failure modes or incomplete documentation stubs recorded.*"

        return (
            function_md,
            architecture_md,
            dep_narrative_md,
            gaps_and_issues_md,
            model_used,
        )

    def _synthesize_with_large_model(
        self,
        canonical_app: str,
        by_cat: Dict[str, List[Dict[str, Any]]],
    ) -> Tuple[str, str, str, str, str]:
        """
        Single-shot Reduce for `canonical_app` using only 1 API call to the Larger Model.
        """
        schema = {
            "type": "object",
            "properties": {
                "function_md": {"type": "string"},
                "architecture_md": {"type": "string"},
                "dependencies_md": {"type": "string"},
                "gaps_and_issues_md": {"type": "string"},
            },
            "required": [
                "function_md",
                "architecture_md",
                "dependencies_md",
                "gaps_and_issues_md",
            ],
            "additionalProperties": False,
        }
        sys_prompt = (
            "You are a Principal Enterprise Architect synthesizing an authoritative Application Dossier "
            "from fragmented Confluence pages and Jira/ServiceNow tickets. "
            "Preserve `[chunk_uid]` citations on every claim. Highlight any contradictions between "
            "Confluence docs and ServiceNow/Jira incident reality, and explicitly call out incomplete documentation."
        )
        sections_text = []
        for cat, items in by_cat.items():
            bullets = self._format_fact_bullets(items)
            sections_text.append(f"### Category: {cat.upper()}\n{bullets or '(none)'}")

        user_prompt = (
            f"Target Application: `{canonical_app}`\n\n"
            + "\n\n".join(sections_text)
        )
        res = self.llm.chat_json(
            system_prompt=sys_prompt,
            user_prompt=user_prompt,
            json_schema=schema,
            schema_name="dossier_synthesis",
            purpose="reduce_large_single_shot",
            prefer_large=True,
        )
        large_model_name = self.config.get("large_llm", {}).get("model", "large-llm")
        return (
            res.get("function_md", ""),
            res.get("architecture_md", ""),
            res.get("dependencies_md", ""),
            res.get("gaps_and_issues_md", ""),
            f"large:{large_model_name}",
        )

    @staticmethod
    def _format_fact_bullets(facts: List[Dict[str, Any]]) -> str:
        lines = []
        for f in facts:
            rel = f.get("canonical_related_app") or f.get("related_app")
            mech = f.get("mechanism")
            prefix = f"[{f['category'].upper()}]"
            if rel:
                prefix += f" (Related App: `{rel}`)"
            if mech:
                prefix += f" [via `{mech}`]"
            lines.append(f"- {prefix} {f['detail']} `[{f['chunk_uid']}]`")
        return "\n".join(lines)

    def _build_dependency_diagram_and_table(
        self,
        canonical_app: str,
        upstream_facts: List[Dict[str, Any]],
        downstream_facts: List[Dict[str, Any]],
    ) -> str:
        mermaid_lines = ["```mermaid", "flowchart LR"]
        center_id = self._safe_node_id(canonical_app)
        mermaid_lines.append(f'    {center_id}[["{canonical_app}"]]')

        table_rows = [
            "| Direction | Related Application | Protocol / Mechanism | Evidence & Details | Source Citation |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ]

        added_edges = set()

        for uf in upstream_facts:
            rel = uf.get("canonical_related_app") or uf.get("related_app") or "external-caller"
            mech = uf.get("mechanism") or "API/Event"
            uid = uf["chunk_uid"]
            rel_id = self._safe_node_id(rel)
            edge_key = (rel_id, center_id, mech)
            if edge_key not in added_edges and rel_id != center_id:
                added_edges.add(edge_key)
                mermaid_lines.append(
                    f'    {rel_id}["{rel}"] -->|"{mech}"| {center_id}'
                )
            detail_clean = uf["detail"].replace("|", "/")
            table_rows.append(
                f"| **Upstream (Caller)** | `{rel}` | `{mech}` | {detail_clean} | `{uid}` |"
            )

        for df in downstream_facts:
            rel = df.get("canonical_related_app") or df.get("related_app") or "external-dependency"
            mech = df.get("mechanism") or "API/Event"
            uid = df["chunk_uid"]
            rel_id = self._safe_node_id(rel)
            edge_key = (center_id, rel_id, mech)
            if edge_key not in added_edges and rel_id != center_id:
                added_edges.add(edge_key)
                mermaid_lines.append(
                    f'    {center_id} -->|"{mech}"| {rel_id}["{rel}"]'
                )
            detail_clean = df["detail"].replace("|", "/")
            table_rows.append(
                f"| **Downstream (Target)** | `{rel}` | `{mech}` | {detail_clean} | `{uid}` |"
            )

        mermaid_lines.append("```")

        if len(table_rows) == 2:
            return "*No upstream or downstream application links discovered yet.*"

        return "\n".join(mermaid_lines) + "\n\n" + "\n".join(table_rows)

    def _write_global_catalog(self) -> Path:
        dossiers = self.db.conn.execute(
            "SELECT canonical_app, full_markdown, model_used, updated_at FROM app_dossiers ORDER BY canonical_app ASC"
        ).fetchall()
        all_dep_facts = self.db.conn.execute(
            """
            SELECT canonical_app, category, canonical_related_app, mechanism
            FROM facts
            WHERE category IN ('upstream', 'downstream')
              AND canonical_app IS NOT NULL
              AND canonical_related_app IS NOT NULL
            """
        ).fetchall()

        mermaid = ["```mermaid", "flowchart LR"]
        edges = set()
        for r in all_dep_facts:
            src = r["canonical_app"]
            dst = r["canonical_related_app"]
            mech = r["mechanism"] or "calls"
            if r["category"] == "upstream":
                # dst is upstream of src -> dst calls src
                u, v = dst, src
            else:
                u, v = src, dst
            if u.lower() == v.lower():
                continue
            uid_node = self._safe_node_id(u)
            vid_node = self._safe_node_id(v)
            edge_key = (uid_node, vid_node, mech)
            if edge_key not in edges:
                edges.add(edge_key)
                mermaid.append(f'    {uid_node}["{u}"] -->|"{mech}"| {vid_node}["{v}"]')
        mermaid.append("```")

        lines = [
            "# Global Application Architecture & Dependency Catalog",
            "",
            "## System-Wide Dependency Graph",
            "",
            "\n".join(mermaid),
            "",
            "## Discovered Applications",
            "",
            "| Application | Dossier File | Last Synthesized By | Updated At |",
            "| :--- | :--- | :--- | :--- |",
        ]
        for d in dossiers:
            app = d["canonical_app"]
            lines.append(
                f"| **`{app}`** | [apps/{app}.md](apps/{app}.md) | `{d['model_used']}` | {d['updated_at']} |"
            )

        catalog_path = self.output_dir / "system_catalog.md"
        catalog_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # Also write a single master consolidated .md report containing all dossiers
        master_lines = list(lines) + ["", "---", ""]
        for d in dossiers:
            master_lines.append(d["full_markdown"])
            master_lines.append("\n---\n")

        master_path = self.output_dir / "all_applications_report.md"
        master_path.write_text("\n".join(master_lines) + "\n", encoding="utf-8")

        return catalog_path

    @staticmethod
    def _to_kebab(name: str) -> str:
        s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
        return s or "unknown-app"

    @staticmethod
    def _core_stem(kebab_name: str) -> str:
        # Strips generic suffixes like -service, -svc, -app, -api, -portal, -engine, -system
        # so 'checkout-service' and 'checkout-svc' match deterministically!
        cleaned = re.sub(
            r"-(service|svc|app|application|api|portal|engine|system|platform|gateway|gw)$",
            "",
            kebab_name,
        )
        return cleaned or kebab_name

    @staticmethod
    def _safe_node_id(name: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", name.strip())
        if not cleaned or cleaned[0].isdigit():
            cleaned = f"app_{cleaned}"
        return cleaned
