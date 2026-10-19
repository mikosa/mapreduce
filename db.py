import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class KnowledgeDB:
    """
    SQLite + FTS5 persistence layer for the MapReduce system.
    Stores:
      - documents (file hashes & stub detection)
      - chunks (context-stamped text blocks from .md and .csv)
      - chunks_fts (BM25 full-text search index for dynamic drill-down)
      - facts (atomic observations extracted by local Mistral in the Map phase)
      - app_aliases (entity resolution table mapping raw names -> canonical app)
      - app_dossiers (synthesized Reduce profiles per canonical app)
      - topic_deep_dives (on-demand dynamic topic query results)
      - llm_usage (tracks local vs. large model calls)
    """

    def __init__(self, db_path: str = "knowledge.db"):
        self.db_path = Path(db_path)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self.conn.cursor()

        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT UNIQUE NOT NULL,
                source_type TEXT NOT NULL, -- 'md' or 'csv'
                file_hash TEXT NOT NULL,
                folder_hierarchy TEXT,
                inferred_app TEXT,
                title TEXT,
                is_incomplete_stub INTEGER DEFAULT 0,
                stub_reason TEXT,
                ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chunk_uid TEXT UNIQUE NOT NULL,
                document_id INTEGER NOT NULL,
                source_type TEXT NOT NULL, -- 'md' or 'csv'
                file_path TEXT NOT NULL,
                folder_hierarchy TEXT,
                inferred_app TEXT,
                title TEXT,
                section_header TEXT,
                ticket_id TEXT,
                context_header TEXT NOT NULL,
                raw_content TEXT NOT NULL,
                full_prompt_text TEXT NOT NULL,
                is_incomplete_stub INTEGER DEFAULT 0,
                stub_note TEXT,
                map_status TEXT DEFAULT 'pending', -- 'pending', 'done', 'skipped_stub', 'error'
                map_error TEXT,
                mapped_at TIMESTAMP,
                FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                chunk_uid UNINDEXED,
                inferred_app,
                folder_hierarchy,
                title,
                section_header,
                ticket_id,
                raw_content
            );

            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chunk_id INTEGER NOT NULL,
                chunk_uid TEXT NOT NULL,
                raw_app_name TEXT NOT NULL,
                canonical_app TEXT,
                category TEXT NOT NULL, -- 'function', 'architecture', 'upstream', 'downstream', 'tech_stack', 'failure_mode', 'incomplete_gap'
                related_app TEXT,
                canonical_related_app TEXT,
                mechanism TEXT,
                detail TEXT NOT NULL,
                source_type TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_facts_canonical_app ON facts(canonical_app);
            CREATE INDEX IF NOT EXISTS idx_facts_canonical_related ON facts(canonical_related_app);
            CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category);

            CREATE TABLE IF NOT EXISTS app_aliases (
                raw_name_key TEXT PRIMARY KEY,
                raw_name TEXT NOT NULL,
                canonical_app TEXT NOT NULL,
                resolution_method TEXT DEFAULT 'auto'
            );

            CREATE TABLE IF NOT EXISTS app_dossiers (
                canonical_app TEXT PRIMARY KEY,
                function_md TEXT,
                architecture_md TEXT,
                dependencies_md TEXT,
                gaps_and_issues_md TEXT,
                full_markdown TEXT NOT NULL,
                supporting_chunk_uids TEXT,
                model_used TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS topic_deep_dives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_app TEXT NOT NULL,
                topic TEXT NOT NULL,
                extracted_notes_json TEXT,
                synthesized_markdown TEXT NOT NULL,
                supporting_chunk_uids TEXT,
                model_used TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS llm_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tier TEXT NOT NULL, -- 'local' or 'large'
                model TEXT NOT NULL,
                purpose TEXT NOT NULL,
                prompt_chars INTEGER,
                response_chars INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -------------------------------------------------------------------------
    # Ingestion Helpers
    # -------------------------------------------------------------------------
    def get_document_by_path(self, file_path: str) -> Optional[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM documents WHERE file_path = ?", (file_path,)
        )
        return cur.fetchone()

    def upsert_document(
        self,
        file_path: str,
        source_type: str,
        file_hash: str,
        folder_hierarchy: str,
        inferred_app: str,
        title: str,
        is_incomplete_stub: bool = False,
        stub_reason: Optional[str] = None,
    ) -> int:
        existing = self.get_document_by_path(file_path)
        if existing:
            doc_id = existing["id"]
            # Clear old chunks & FTS entries when re-ingesting a modified file
            old_uids = [
                r["chunk_uid"]
                for r in self.conn.execute(
                    "SELECT chunk_uid FROM chunks WHERE document_id = ?", (doc_id,)
                ).fetchall()
            ]
            for uid in old_uids:
                self.conn.execute(
                    "DELETE FROM chunks_fts WHERE chunk_uid = ?", (uid,)
                )
            self.conn.execute("DELETE FROM chunks WHERE document_id = ?", (doc_id,))
            self.conn.execute(
                """
                UPDATE documents
                SET source_type=?, file_hash=?, folder_hierarchy=?, inferred_app=?,
                    title=?, is_incomplete_stub=?, stub_reason=?, ingested_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    source_type,
                    file_hash,
                    folder_hierarchy,
                    inferred_app,
                    title,
                    1 if is_incomplete_stub else 0,
                    stub_reason,
                    doc_id,
                ),
            )
            self.conn.commit()
            return doc_id

        cur = self.conn.execute(
            """
            INSERT INTO documents (
                file_path, source_type, file_hash, folder_hierarchy,
                inferred_app, title, is_incomplete_stub, stub_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_path,
                source_type,
                file_hash,
                folder_hierarchy,
                inferred_app,
                title,
                1 if is_incomplete_stub else 0,
                stub_reason,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def insert_chunk(
        self,
        chunk_uid: str,
        document_id: int,
        source_type: str,
        file_path: str,
        folder_hierarchy: str,
        inferred_app: str,
        title: str,
        section_header: str,
        ticket_id: str,
        context_header: str,
        raw_content: str,
        full_prompt_text: str,
        is_incomplete_stub: bool = False,
        stub_note: Optional[str] = None,
    ) -> int:
        map_status = "skipped_stub" if is_incomplete_stub else "pending"
        cur = self.conn.execute(
            """
            INSERT INTO chunks (
                chunk_uid, document_id, source_type, file_path, folder_hierarchy,
                inferred_app, title, section_header, ticket_id, context_header,
                raw_content, full_prompt_text, is_incomplete_stub, stub_note, map_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chunk_uid,
                document_id,
                source_type,
                file_path,
                folder_hierarchy,
                inferred_app,
                title,
                section_header,
                ticket_id,
                context_header,
                raw_content,
                full_prompt_text,
                1 if is_incomplete_stub else 0,
                stub_note,
                map_status,
            ),
        )
        chunk_id = int(cur.lastrowid)

        self.conn.execute(
            """
            INSERT INTO chunks_fts (
                chunk_uid, inferred_app, folder_hierarchy, title,
                section_header, ticket_id, raw_content
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chunk_uid,
                inferred_app or "",
                folder_hierarchy or "",
                title or "",
                section_header or "",
                ticket_id or "",
                raw_content or "",
            ),
        )

        # If this chunk is an incomplete stub, automatically record an incomplete_gap fact
        # so the Reduce stage knows this page/section was incomplete without calling the LLM!
        if is_incomplete_stub and stub_note and inferred_app:
            self.conn.execute(
                """
                INSERT INTO facts (
                    chunk_id, chunk_uid, raw_app_name, canonical_app,
                    category, related_app, mechanism, detail, source_type
                ) VALUES (?, ?, ?, ?, 'incomplete_gap', NULL, NULL, ?, ?)
                """,
                (
                    chunk_id,
                    chunk_uid,
                    inferred_app,
                    inferred_app,
                    stub_note,
                    source_type,
                ),
            )

        self.conn.commit()
        return chunk_id

    # -------------------------------------------------------------------------
    # Map Phase Helpers
    # -------------------------------------------------------------------------
    def get_pending_chunks(self, limit: Optional[int] = None) -> List[sqlite3.Row]:
        sql = "SELECT * FROM chunks WHERE map_status = 'pending' ORDER BY id ASC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.conn.execute(sql).fetchall()

    def save_chunk_facts(
        self,
        chunk_id: int,
        chunk_uid: str,
        source_type: str,
        facts: List[Dict[str, Any]],
    ) -> None:
        # Clear any prior facts for this chunk in case of re-run
        self.conn.execute("DELETE FROM facts WHERE chunk_id = ?", (chunk_id,))
        for f in facts:
            raw_app = (f.get("raw_app_name") or "unknown").strip()
            category = (f.get("category") or "function").strip().lower()
            related_app = f.get("related_app")
            if related_app:
                related_app = str(related_app).strip()
                if related_app.lower() in ("none", "null", "n/a", "", raw_app.lower()):
                    related_app = None
            mechanism = f.get("mechanism")
            if mechanism:
                mechanism = str(mechanism).strip()
                if mechanism.lower() in ("none", "null", "n/a", ""):
                    mechanism = None
            detail = (f.get("detail") or "").strip()
            if not detail:
                continue

            self.conn.execute(
                """
                INSERT INTO facts (
                    chunk_id, chunk_uid, raw_app_name, canonical_app,
                    category, related_app, mechanism, detail, source_type
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    chunk_id,
                    chunk_uid,
                    raw_app,
                    category,
                    related_app,
                    mechanism,
                    detail,
                    source_type,
                ),
            )

        self.conn.execute(
            """
            UPDATE chunks
            SET map_status = 'done', map_error = NULL, mapped_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (chunk_id,),
        )
        self.conn.commit()

    def mark_chunk_error(self, chunk_id: int, error_msg: str) -> None:
        self.conn.execute(
            "UPDATE chunks SET map_status = 'error', map_error = ? WHERE id = ?",
            (error_msg, chunk_id),
        )
        self.conn.commit()

    # -------------------------------------------------------------------------
    # Entity Resolution & Reduce Helpers
    # -------------------------------------------------------------------------
    def get_all_distinct_app_mentions(self) -> List[str]:
        rows1 = self.conn.execute(
            "SELECT DISTINCT raw_app_name AS name FROM facts WHERE raw_app_name IS NOT NULL"
        ).fetchall()
        rows2 = self.conn.execute(
            "SELECT DISTINCT related_app AS name FROM facts WHERE related_app IS NOT NULL"
        ).fetchall()
        rows3 = self.conn.execute(
            "SELECT DISTINCT inferred_app AS name FROM chunks WHERE inferred_app IS NOT NULL AND inferred_app != ''"
        ).fetchall()
        names = set()
        for r in (*rows1, *rows2, *rows3):
            n = (r["name"] or "").strip()
            if n and n.lower() not in ("unknown", "none", "null", "n/a"):
                names.add(n)
        return sorted(names)

    def save_alias_mapping(
        self, mapping: Dict[str, str], method: str = "auto"
    ) -> None:
        for raw_name, canonical in mapping.items():
            key = raw_name.strip().lower()
            self.conn.execute(
                """
                INSERT INTO app_aliases (raw_name_key, raw_name, canonical_app, resolution_method)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(raw_name_key) DO UPDATE SET
                    canonical_app = excluded.canonical_app,
                    resolution_method = excluded.resolution_method
                """,
                (key, raw_name.strip(), canonical.strip(), method),
            )

        # Apply aliases to facts table
        aliases = {
            r["raw_name_key"]: r["canonical_app"]
            for r in self.conn.execute("SELECT * FROM app_aliases").fetchall()
        }

        facts = self.conn.execute(
            "SELECT id, raw_app_name, related_app, category FROM facts"
        ).fetchall()
        for f in facts:
            raw_key = (f["raw_app_name"] or "").strip().lower()
            c_app = aliases.get(raw_key, f["raw_app_name"])
            rel = f["related_app"]
            c_rel = None
            cat = f["category"]
            if rel:
                rel_key = rel.strip().lower()
                c_rel = aliases.get(rel_key, rel)
                if c_rel and c_app and c_rel.lower() == c_app.lower():
                    c_rel = None
                    if cat in ("upstream", "downstream"):
                        cat = "architecture"
            self.conn.execute(
                "UPDATE facts SET canonical_app = ?, canonical_related_app = ?, category = ? WHERE id = ?",
                (c_app, c_rel, cat, f["id"]),
            )
        self.conn.commit()

    def get_canonical_apps(self) -> List[str]:
        rows = self.conn.execute(
            """
            SELECT DISTINCT canonical_app AS app FROM facts
            WHERE canonical_app IS NOT NULL AND canonical_app != '' AND LOWER(canonical_app) != 'unknown'
            UNION
            SELECT DISTINCT canonical_related_app AS app FROM facts
            WHERE canonical_related_app IS NOT NULL AND canonical_related_app != '' AND LOWER(canonical_related_app) != 'unknown'
            """
        ).fetchall()
        return sorted([r["app"] for r in rows if r["app"]])

    def get_facts_for_app(self, canonical_app: str) -> List[Dict[str, Any]]:
        """
        Retrieves all direct facts for `canonical_app` PLUS reverse-linked
        upstream/downstream facts where another app mentioned `canonical_app`!
        """
        direct_rows = self.conn.execute(
            """
            SELECT f.*, c.file_path, c.title, c.section_header, c.ticket_id
            FROM facts f
            JOIN chunks c ON f.chunk_id = c.id
            WHERE LOWER(f.canonical_app) = LOWER(?)
            ORDER BY f.id ASC
            """,
            (canonical_app,),
        ).fetchall()

        results = [dict(r) for r in direct_rows]

        # Cross-application reverse dependency propagation:
        # If App B says "downstream -> App A", then for App A, App B is "upstream"!
        # If App B says "upstream -> App A", then for App A, App B is "downstream"!
        reverse_rows = self.conn.execute(
            """
            SELECT f.*, c.file_path, c.title, c.section_header, c.ticket_id
            FROM facts f
            JOIN chunks c ON f.chunk_id = c.id
            WHERE LOWER(f.canonical_related_app) = LOWER(?)
              AND LOWER(f.canonical_app) != LOWER(?)
            ORDER BY f.id ASC
            """,
            (canonical_app, canonical_app),
        ).fetchall()

        for r in reverse_rows:
            d = dict(r)
            other_app = d["canonical_app"]
            orig_cat = d["category"]
            if orig_cat == "downstream":
                # Other app calls canonical_app downstream -> so other_app is UPSTREAM of canonical_app
                d["category"] = "upstream"
                d["canonical_related_app"] = other_app
                d["detail"] = (
                    f"[Inferred from {other_app} docs/tickets] {other_app} sends/calls {canonical_app}: {d['detail']}"
                )
                results.append(d)
            elif orig_cat == "upstream":
                # Other app depends on canonical_app as upstream -> so other_app is DOWNSTREAM of canonical_app
                d["category"] = "downstream"
                d["canonical_related_app"] = other_app
                d["detail"] = (
                    f"[Inferred from {other_app} docs/tickets] {other_app} consumes/depends on {canonical_app}: {d['detail']}"
                )
                results.append(d)
            else:
                # General cross-reference
                d["canonical_related_app"] = other_app
                d["detail"] = f"[Cross-ref from {other_app}] {d['detail']}"
                results.append(d)

        return results

    def save_dossier(
        self,
        canonical_app: str,
        function_md: str,
        architecture_md: str,
        dependencies_md: str,
        gaps_and_issues_md: str,
        full_markdown: str,
        supporting_chunk_uids: List[str],
        model_used: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO app_dossiers (
                canonical_app, function_md, architecture_md, dependencies_md,
                gaps_and_issues_md, full_markdown, supporting_chunk_uids,
                model_used, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(canonical_app) DO UPDATE SET
                function_md = excluded.function_md,
                architecture_md = excluded.architecture_md,
                dependencies_md = excluded.dependencies_md,
                gaps_and_issues_md = excluded.gaps_and_issues_md,
                full_markdown = excluded.full_markdown,
                supporting_chunk_uids = excluded.supporting_chunk_uids,
                model_used = excluded.model_used,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                canonical_app,
                function_md,
                architecture_md,
                dependencies_md,
                gaps_and_issues_md,
                full_markdown,
                json.dumps(sorted(set(supporting_chunk_uids))),
                model_used,
            ),
        )
        self.conn.commit()

    # -------------------------------------------------------------------------
    # Dynamic Drill-Down Search (FTS5 + Folder/Fact Graph)
    # -------------------------------------------------------------------------
    def search_chunks_for_topic(
        self,
        topic: str,
        canonical_app: Optional[str] = None,
        limit: int = 12,
    ) -> List[Dict[str, Any]]:
        """
        Finds the most relevant raw chunks for a dynamic topic query by combining:
        1. Chunks linked to `canonical_app` (via folder path, direct facts, or reverse facts)
        2. SQLite FTS5 BM25 keyword search over `topic` terms
        3. Fallback substring matching so no relevant chunk is missed
        """
        # Extract clean search tokens
        tokens = [
            t.lower()
            for t in re.findall(r"[a-zA-Z0-9_-]{2,}", topic)
            if t.lower()
            not in {
                "the",
                "and",
                "for",
                "how",
                "does",
                "what",
                "are",
                "with",
                "from",
                "about",
                "more",
                "info",
                "information",
            }
        ]

        app_chunk_ids: set[int] = set()
        if canonical_app:
            # Find all aliases for this canonical app
            alias_rows = self.conn.execute(
                "SELECT raw_name FROM app_aliases WHERE LOWER(canonical_app) = LOWER(?)",
                (canonical_app,),
            ).fetchall()
            app_names = {canonical_app.lower()} | {
                r["raw_name"].lower() for r in alias_rows
            }

            # 1) Chunks that produced facts for or related to this app
            fact_chunks = self.conn.execute(
                """
                SELECT DISTINCT chunk_id FROM facts
                WHERE LOWER(canonical_app) = LOWER(?)
                   OR LOWER(canonical_related_app) = LOWER(?)
                """,
                (canonical_app, canonical_app),
            ).fetchall()
            app_chunk_ids.update(r["chunk_id"] for r in fact_chunks)

            # 2) Chunks whose folder path or inferred_app matches any alias
            all_chunks_meta = self.conn.execute(
                "SELECT id, inferred_app, folder_hierarchy, title, raw_content FROM chunks"
            ).fetchall()
            for c in all_chunks_meta:
                inf = (c["inferred_app"] or "").lower()
                fh = (c["folder_hierarchy"] or "").lower()
                title = (c["title"] or "").lower()
                raw = (c["raw_content"] or "").lower()
                if any(
                    name in inf or name in fh or name in title or name in raw
                    for name in app_names
                ):
                    app_chunk_ids.add(c["id"])

        scored_chunks: Dict[int, Tuple[float, Dict[str, Any]]] = {}

        # Run FTS5 OR query if we have tokens
        if tokens:
            fts_query = " OR ".join(f'"{t}"' for t in tokens)
            try:
                fts_rows = self.conn.execute(
                    """
                    SELECT c.*, bm25(chunks_fts) AS rank
                    FROM chunks_fts
                    JOIN chunks c ON chunks_fts.chunk_uid = c.chunk_uid
                    WHERE chunks_fts MATCH ?
                    ORDER BY rank ASC
                    LIMIT 50
                    """,
                    (fts_query,),
                ).fetchall()
                for r in fts_rows:
                    cid = r["id"]
                    if canonical_app and app_chunk_ids and cid not in app_chunk_ids:
                        continue
                    # bm25 in sqlite FTS5 is negative (more negative = better match)
                    score = -float(r["rank"]) + (5.0 if cid in app_chunk_ids else 0.0)
                    scored_chunks[cid] = (score, dict(r))
            except sqlite3.OperationalError:
                pass

        # Also score all app_chunk_ids by token overlap so even if FTS misses a partial word,
        # or if the user asks a broad topic about an app with few chunks, we still inspect its chunks
        candidate_sql = "SELECT * FROM chunks"
        candidate_rows = self.conn.execute(candidate_sql).fetchall()
        for r in candidate_rows:
            cid = r["id"]
            if canonical_app and cid not in app_chunk_ids:
                continue
            text_blob = " ".join(
                [
                    r["inferred_app"] or "",
                    r["folder_hierarchy"] or "",
                    r["title"] or "",
                    r["section_header"] or "",
                    r["raw_content"] or "",
                ]
            ).lower()
            token_hits = sum(1 for t in tokens if t in text_blob)
            if token_hits > 0 or (canonical_app and cid in app_chunk_ids):
                base_score = token_hits * 2.0 + (1.0 if cid in app_chunk_ids else 0.0)
                if cid in scored_chunks:
                    old_score, d = scored_chunks[cid]
                    scored_chunks[cid] = (old_score + base_score, d)
                else:
                    scored_chunks[cid] = (base_score, dict(r))

        ranked = sorted(
            scored_chunks.values(), key=lambda item: item[0], reverse=True
        )
        return [item[1] for item in ranked[:limit]]

    def save_topic_deep_dive(
        self,
        canonical_app: str,
        topic: str,
        extracted_notes: List[Dict[str, Any]],
        synthesized_markdown: str,
        supporting_chunk_uids: List[str],
        model_used: str,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO topic_deep_dives (
                canonical_app, topic, extracted_notes_json,
                synthesized_markdown, supporting_chunk_uids, model_used
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                canonical_app or "ALL_APPS",
                topic,
                json.dumps(extracted_notes, indent=2),
                synthesized_markdown,
                json.dumps(sorted(set(supporting_chunk_uids))),
                model_used,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    # -------------------------------------------------------------------------
    # Usage & Status Stats
    # -------------------------------------------------------------------------
    def log_llm_call(
        self,
        tier: str,
        model: str,
        purpose: str,
        prompt_chars: int,
        response_chars: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO llm_usage (tier, model, purpose, prompt_chars, response_chars)
            VALUES (?, ?, ?, ?, ?)
            """,
            (tier, model, purpose, prompt_chars, response_chars),
        )
        self.conn.commit()

    def get_stats(self) -> Dict[str, Any]:
        docs = self.conn.execute(
            "SELECT source_type, COUNT(*) as cnt, SUM(is_incomplete_stub) as stubs FROM documents GROUP BY source_type"
        ).fetchall()
        chunks = self.conn.execute(
            "SELECT map_status, COUNT(*) as cnt FROM chunks GROUP BY map_status"
        ).fetchall()
        facts_cnt = self.conn.execute("SELECT COUNT(*) as cnt FROM facts").fetchone()[
            "cnt"
        ]
        dossiers_cnt = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM app_dossiers"
        ).fetchone()["cnt"]
        llm_calls = self.conn.execute(
            "SELECT tier, COUNT(*) as cnt FROM llm_usage GROUP BY tier"
        ).fetchall()

        return {
            "documents_by_type": {
                r["source_type"]: {"count": r["cnt"], "stubs": r["stubs"] or 0}
                for r in docs
            },
            "chunks_by_status": {r["map_status"]: r["cnt"] for r in chunks},
            "total_facts": facts_cnt,
            "total_dossiers": dossiers_cnt,
            "llm_calls_by_tier": {r["tier"]: r["cnt"] for r in llm_calls},
        }
