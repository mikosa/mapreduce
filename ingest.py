import csv
import hashlib
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from db import KnowledgeDB


GENERIC_SUBFOLDERS = {
    "docs",
    "doc",
    "confluence",
    "markdown",
    "md",
    "pages",
    "spaces",
    "space",
    "archive",
    "attachments",
    "templates",
    "architecture",
    "design",
    "runbooks",
    "runbook",
    "operations",
    "ops",
    "database",
    "db",
    "api",
    "apis",
    "specs",
    "guides",
    "how-to",
    "troubleshooting",
    "incidents",
    "overview",
    "general",
    "security",
    "deployment",
    "infra",
    "infrastructure",
    "monitoring",
    "alerts",
    "config",
    "configuration",
}

STUB_PATTERNS = re.compile(
    r"^\s*(tbd|todo|wip|under construction|coming soon|n/a|none|placeholder|fill in later|draft)\s*[\.\!\-]*\s*$",
    re.IGNORECASE,
)


class Ingester:
    """
    Ingests nested Confluence .md directories and Jira/ServiceNow .csv files into SQLite + FTS5.
    Injects folder path & document title metadata into every chunk and detects incomplete/stub pages.
    """

    def __init__(self, config: Dict[str, Any], db: KnowledgeDB):
        self.config = config
        self.db = db
        md_cfg = config.get("ingestion", {}).get("markdown", {})
        self.max_chunk_chars = int(md_cfg.get("max_chunk_chars", 2200))
        self.min_words_for_map = int(md_cfg.get("min_words_for_map", 12))
        extra_ignored = {
            s.lower() for s in md_cfg.get("ignored_folder_names", [])
        }
        self.ignored_folders = GENERIC_SUBFOLDERS | extra_ignored

        csv_cfg = config.get("ingestion", {}).get("csv", {})
        self.csv_cfg = csv_cfg
        self.max_ticket_chars = int(csv_cfg.get("max_ticket_chars", 2500))

    def ingest_paths(
        self, paths: List[str], force_reingest: bool = False
    ) -> Dict[str, int]:
        stats = {
            "md_files_processed": 0,
            "md_stubs_detected": 0,
            "md_chunks_created": 0,
            "csv_files_processed": 0,
            "csv_tickets_ingested": 0,
            "skipped_unchanged": 0,
        }

        for raw_p in paths:
            p = Path(raw_p).resolve()
            if not p.exists():
                print(f"[Warn] Path does not exist: {raw_p}")
                continue

            if p.is_file():
                self._ingest_single_file(p, p.parent, stats, force_reingest)
            elif p.is_dir():
                for root, _, files in os.walk(p):
                    for fname in sorted(files):
                        fpath = Path(root) / fname
                        if fpath.suffix.lower() in (".md", ".markdown", ".csv"):
                            self._ingest_single_file(
                                fpath, p, stats, force_reingest
                            )

        return stats

    def _ingest_single_file(
        self,
        file_path: Path,
        base_root: Path,
        stats: Dict[str, int],
        force_reingest: bool,
    ) -> None:
        ext = file_path.suffix.lower()
        rel_path = str(file_path.relative_to(base_root))
        file_hash = self._hash_file(file_path)

        existing = self.db.get_document_by_path(rel_path)
        if existing and existing["file_hash"] == file_hash and not force_reingest:
            stats["skipped_unchanged"] += 1
            return

        if ext in (".md", ".markdown"):
            self._ingest_markdown_file(file_path, rel_path, file_hash, stats)
        elif ext == ".csv":
            self._ingest_csv_file(file_path, rel_path, file_hash, stats)

    # -------------------------------------------------------------------------
    # Confluence Nested Markdown Ingestion
    # -------------------------------------------------------------------------
    def _ingest_markdown_file(
        self,
        file_path: Path,
        rel_path: str,
        file_hash: str,
        stats: Dict[str, int],
    ) -> None:
        raw_text = file_path.read_text(encoding="utf-8", errors="replace")
        folder_parts = list(Path(rel_path).parent.parts)
        folder_Parts_clean = [p for p in folder_parts if p not in (".", "")]
        folder_hierarchy = (
            " > ".join(folder_Parts_clean) if folder_Parts_clean else "(root)"
        )

        title = self._extract_md_title(raw_text, file_path.stem)
        inferred_app = self._infer_app_from_path_and_title(
            folder_Parts_clean, file_path.stem, title
        )

        # Check if the entire file is an incomplete stub
        body_without_headers = re.sub(r"(?m)^#+\s+.*$", "", raw_text).strip()
        words = re.findall(r"[a-zA-Z0-9]{2,}", body_without_headers)
        is_file_stub = (
            len(words) < self.min_words_for_map
            or bool(STUB_PATTERNS.match(body_without_headers))
        )
        stub_reason = None
        if is_file_stub:
            snippet = body_without_headers.replace("\n", " ").strip()[:120]
            stub_reason = (
                f"Incomplete/stub Confluence page '{title}' at '{rel_path}' "
                f"(only {len(words)} content words: '{snippet or 'empty'}')"
            )
            stats["md_stubs_detected"] += 1

        doc_id = self.db.upsert_document(
            file_path=rel_path,
            source_type="md",
            file_hash=file_hash,
            folder_hierarchy=folder_hierarchy,
            inferred_app=inferred_app,
            title=title,
            is_incomplete_stub=is_file_stub,
            stub_reason=stub_reason,
        )
        stats["md_files_processed"] += 1

        if is_file_stub:
            chunk_uid = f"md:{rel_path}#stub"
            context_header = self._build_md_context_header(
                rel_path=rel_path,
                folder_hierarchy=folder_hierarchy,
                inferred_app=inferred_app,
                title=title,
                section_header="(Entire Page Stub)",
            )
            full_prompt = f"{context_header}\n[CONTENT]\n{raw_text.strip()}\n[/CONTENT]"
            self.db.insert_chunk(
                chunk_uid=chunk_uid,
                document_id=doc_id,
                source_type="md",
                file_path=rel_path,
                folder_hierarchy=folder_hierarchy,
                inferred_app=inferred_app,
                title=title,
                section_header="(Entire Page Stub)",
                ticket_id="",
                context_header=context_header,
                raw_content=raw_text.strip() or "(Empty Page)",
                full_prompt_text=full_prompt,
                is_incomplete_stub=True,
                stub_note=stub_reason,
            )
            stats["md_chunks_created"] += 1
            return

        # Split non-stub Markdown page into section-aware chunks
        sections = self._split_markdown_sections(raw_text, title)
        for idx, (sec_header, sec_body, is_sec_stub) in enumerate(sections, start=1):
            chunk_uid = f"md:{rel_path}#s{idx}"
            context_header = self._build_md_context_header(
                rel_path=rel_path,
                folder_hierarchy=folder_hierarchy,
                inferred_app=inferred_app,
                title=title,
                section_header=sec_header,
            )
            full_prompt = f"{context_header}\n[CONTENT]\n{sec_body}\n[/CONTENT]"
            stub_note = None
            if is_sec_stub:
                stub_note = (
                    f"Incomplete section '{sec_header}' in page '{title}' ({rel_path}): "
                    f"'{sec_body.replace(chr(10), ' ').strip()[:100] or 'empty section'}'"
                )
            self.db.insert_chunk(
                chunk_uid=chunk_uid,
                document_id=doc_id,
                source_type="md",
                file_path=rel_path,
                folder_hierarchy=folder_hierarchy,
                inferred_app=inferred_app,
                title=title,
                section_header=sec_header,
                ticket_id="",
                context_header=context_header,
                raw_content=sec_body,
                full_prompt_text=full_prompt,
                is_incomplete_stub=is_sec_stub,
                stub_note=stub_note,
            )
            stats["md_chunks_created"] += 1

    def _infer_app_from_path_and_title(
        self, folder_parts: List[str], file_stem: str, title: str
    ) -> str:
        """
        Crucial logic for nested Confluence exports where the application name is only
        in the directory path or document title:
        Walks the folder hierarchy from deepest specific folder up, skipping generic
        organizational folders ('docs', 'architecture', 'database', 'runbooks', etc.).
        If no folder gives a specific app name, checks the file stem / title prefix.
        """
        non_generic_folders = [
            f
            for f in folder_parts
            if f.lower().strip("_- ") not in self.ignored_folders
        ]
        # In a path like: `payments/checkout-service/database/postgres.md`,
        # `non_generic_folders` is `['payments', 'checkout-service']`.
        # The most specific application folder is the deepest non-generic folder (`checkout-service`),
        # unless there is only 1 folder.
        if non_generic_folders:
            return non_generic_folders[-1]

        # Next check if the title or filename has "App Name - Topic" or "App Name: Topic"
        for candidate in (title, file_stem):
            if " - " in candidate:
                prefix = candidate.split(" - ", 1)[0].strip()
                if prefix.lower() not in self.ignored_folders:
                    return prefix
            if ":" in candidate:
                prefix = candidate.split(":", 1)[0].strip()
                if prefix.lower() not in self.ignored_folders:
                    return prefix

        if file_stem.lower() not in self.ignored_folders and file_stem.lower() not in (
            "index",
            "readme",
            "overview",
            "page",
        ):
            return file_stem

        return folder_parts[-1] if folder_parts else title

    @staticmethod
    def _extract_md_title(raw_text: str, fallback_stem: str) -> str:
        # Check YAML frontmatter title if present
        fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw_text, re.DOTALL)
        if fm_match:
            title_line = re.search(
                r"(?m)^title:\s*['\"]?(.+?)['\"]?\s*$", fm_match.group(1)
            )
            if title_line:
                return title_line.group(1).strip()

        # Check first H1 heading
        h1_match = re.search(r"(?m)^#\s+(.+)$", raw_text)
        if h1_match:
            return h1_match.group(1).strip()

        return fallback_stem.replace("_", " ").replace("-", " ").strip()

    @staticmethod
    def _build_md_context_header(
        rel_path: str,
        folder_hierarchy: str,
        inferred_app: str,
        title: str,
        section_header: str,
    ) -> str:
        return (
            "[CONTEXT]\n"
            "Source Type: Confluence Markdown Document\n"
            f"File Path: {rel_path}\n"
            f"Folder Hierarchy: {folder_hierarchy}\n"
            f"Inferred Application: {inferred_app}\n"
            f"Document Title: {title}\n"
            f"Section: {section_header}\n"
            "Note: The page body below may omit the application name because it is implied by the File Path / Document Title above.\n"
            "[/CONTEXT]"
        )

    def _split_markdown_sections(
        self, raw_text: str, doc_title: str
    ) -> List[Tuple[str, str, bool]]:
        """
        Splits a Markdown document by headers (#, ##, ###) and preserves the header breadcrumb.
        Also flags individual sections that are empty/TBD stubs (`is_sec_stub = True`).
        """
        lines = raw_text.splitlines()
        raw_sections: List[Tuple[str, List[str]]] = []
        h_stack: List[Tuple[int, str]] = []
        current_header = doc_title
        current_lines: List[str] = []

        header_re = re.compile(r"^(#{1,4})\s+(.+)$")

        for line in lines:
            m = header_re.match(line)
            if m:
                # Save previous section if it had any lines or if it was an explicit sub-header
                if current_lines or len(h_stack) >= 2:
                    raw_sections.append((current_header, current_lines))
                level = len(m.group(1))
                h_text = m.group(2).strip()
                while h_stack and h_stack[-1][0] >= level:
                    h_stack.pop()
                h_stack.append((level, h_text))
                current_header = " > ".join(h[1] for h in h_stack)
                current_lines = []
            else:
                current_lines.append(line)

        if current_lines or not raw_sections:
            raw_sections.append((current_header, current_lines))

        final_chunks: List[Tuple[str, str, bool]] = []
        for sec_header, sec_lines in raw_sections:
            body = "\n".join(sec_lines).strip()
            words = re.findall(r"[a-zA-Z0-9]{2,}", body)

            # Ignore empty top-level intro right before the first H2
            if not body and sec_header == doc_title and len(raw_sections) > 1:
                continue

            is_stub = (
                len(words) < 5
                or bool(STUB_PATTERNS.match(body))
                or any(
                    marker in body.lower()
                    for marker in ("todo:", "tbd", "under construction", "coming soon")
                )
                and len(words) < self.min_words_for_map
            )

            if len(body) <= self.max_chunk_chars:
                final_chunks.append((sec_header, body or "(Empty Section)", is_stub))
            else:
                # Split oversized section by paragraphs
                paras = body.split("\n\n")
                buf = ""
                part = 1
                for p in paras:
                    if len(buf) + len(p) + 2 > self.max_chunk_chars and buf:
                        final_chunks.append(
                            (f"{sec_header} (Part {part})", buf.strip(), False)
                        )
                        buf = p
                        part += 1
                    else:
                        buf = f"{buf}\n\n{p}" if buf else p
                if buf.strip():
                    final_chunks.append(
                        (f"{sec_header} (Part {part})", buf.strip(), False)
                    )

        return final_chunks

    # -------------------------------------------------------------------------
    # Jira / ServiceNow CSV Ticket Ingestion
    # -------------------------------------------------------------------------
    def _ingest_csv_file(
        self,
        file_path: Path,
        rel_path: str,
        file_hash: str,
        stats: Dict[str, int],
    ) -> None:
        folder_parts = [
            p for p in Path(rel_path).parent.parts if p not in (".", "")
        ]
        folder_hierarchy = " > ".join(folder_parts) if folder_parts else "(root)"
        fallback_app = self._infer_app_from_path_and_title(
            folder_parts, file_path.stem, file_path.stem
        )

        doc_id = self.db.upsert_document(
            file_path=rel_path,
            source_type="csv",
            file_hash=file_hash,
            folder_hierarchy=folder_hierarchy,
            inferred_app=fallback_app,
            title=file_path.name,
            is_incomplete_stub=False,
            stub_reason=None,
        )
        stats["csv_files_processed"] += 1

        with file_path.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return

            col_map = self._resolve_csv_columns(reader.fieldnames)

            for row_idx, row in enumerate(reader, start=1):
                ticket_id = (
                    self._get_first_matching_col(row, col_map["id"])
                    or f"row_{row_idx}"
                )
                row_app = (
                    self._get_first_matching_col(row, col_map["app"])
                    or ""
                )
                short_desc = self._get_first_matching_col(
                    row, col_map["short_description"]
                )
                desc = self._get_first_matching_col(row, col_map["description"])
                notes = self._get_first_matching_col(row, col_map["notes"])
                comments = self._get_first_matching_col(
                    row, col_map["comments"]
                )

                # If none of the standard columns matched, fall back to concatenating all non-empty columns
                if not any([short_desc, desc, notes, comments]):
                    combined_other = "\n".join(
                        f"{k}: {v}" for k, v in row.items() if v and str(v).strip()
                    )
                    desc = combined_other

                cleaned_notes = self._clean_ticket_noise(notes)
                cleaned_comments = self._clean_ticket_noise(comments)

                inferred_app = row_app.strip() if row_app.strip() else fallback_app
                # If fallback_app is just a generic CSV filename like 'tickets' or 'servicenow',
                # try to spot a bracketed/prefixed app tag in short_description
                if inferred_app.lower() in (
                    "tickets",
                    "incidents",
                    "servicenow",
                    "jira",
                    "export",
                    "csv",
                ):
                    tag_match = re.match(r"^\[([A-Za-z0-9_-]+)\]", short_desc)
                    if tag_match:
                        inferred_app = tag_match.group(1)
                    else:
                        inferred_app = "unassigned-ticket-app"

                ticket_lines = [f"Ticket ID: {ticket_id}"]
                if row_app:
                    ticket_lines.append(f"Tagged Application/CI: {row_app}")
                if short_desc:
                    ticket_lines.append(f"Short Description: {short_desc}")
                if desc:
                    ticket_lines.append(f"Description: {desc}")
                if cleaned_notes:
                    ticket_lines.append(f"Technical / Work Notes: {cleaned_notes}")
                if cleaned_comments:
                    ticket_lines.append(f"Comments: {cleaned_comments}")

                ticket_body = "\n".join(ticket_lines).strip()
                if len(ticket_body) > self.max_ticket_chars:
                    ticket_body = ticket_body[: self.max_ticket_chars] + "\n...[truncated]"

                words = re.findall(r"[a-zA-Z0-9]{2,}", ticket_body)
                is_stub = len(words) < 6

                chunk_uid = f"csv:{rel_path}#{ticket_id}"
                context_header = (
                    "[CONTEXT]\n"
                    "Source Type: Jira / ServiceNow Ticket (CSV)\n"
                    f"CSV File: {rel_path} (Row {row_idx})\n"
                    f"Ticket ID: {ticket_id}\n"
                    f"Inferred Application: {inferred_app}\n"
                    f"Short Description: {short_desc or '(none)'}\n"
                    "[/CONTEXT]"
                )
                full_prompt = f"{context_header}\n[CONTENT]\n{ticket_body}\n[/CONTENT]"

                self.db.insert_chunk(
                    chunk_uid=chunk_uid,
                    document_id=doc_id,
                    source_type="csv",
                    file_path=rel_path,
                    folder_hierarchy=folder_hierarchy,
                    inferred_app=inferred_app,
                    title=short_desc or f"Ticket {ticket_id}",
                    section_header=f"Ticket {ticket_id}",
                    ticket_id=ticket_id,
                    context_header=context_header,
                    raw_content=ticket_body,
                    full_prompt_text=full_prompt,
                    is_incomplete_stub=is_stub,
                    stub_note="Empty or minimal ticket row" if is_stub else None,
                )
                stats["csv_tickets_ingested"] += 1

    def _resolve_csv_columns(
        self, fieldnames: List[str]
    ) -> Dict[str, List[str]]:
        """
        Matches CSV header names case-insensitively and flexibly (ignoring underscores/spaces).
        """
        def norm(s: str) -> str:
            return re.sub(r"[^a-z0-9]", "", s.lower())

        mapping: Dict[str, List[str]] = {
            "id": [],
            "app": [],
            "short_description": [],
            "description": [],
            "notes": [],
            "comments": [],
        }

        candidates = {
            "id": [norm(c) for c in self.csv_cfg.get("id_columns", ["number", "ticket_id", "id", "issue_key", "key"])],
            "app": [norm(c) for c in self.csv_cfg.get("app_columns", ["application", "app", "cmdb_ci", "component", "service"])],
            "short_description": [norm(c) for c in self.csv_cfg.get("short_description_columns", ["short_description", "summary", "title"])],
            "description": [norm(c) for c in self.csv_cfg.get("description_columns", ["description", "details", "body"])],
            "notes": [norm(c) for c in self.csv_cfg.get("notes_columns", ["notes", "work_notes", "resolution_notes", "close_notes"])],
            "comments": [norm(c) for c in self.csv_cfg.get("comments_columns", ["comments", "additional_comments", "comment"])],
        }

        for col in fieldnames:
            ncol = norm(col)
            for key, cand_list in candidates.items():
                if ncol in cand_list:
                    mapping[key].append(col)

        return mapping

    @staticmethod
    def _get_first_matching_col(row: Dict[str, Any], cols: List[str]) -> str:
        parts = []
        for c in cols:
            val = row.get(c)
            if val and str(val).strip():
                parts.append(str(val).strip())
        return "\n".join(parts)

    @staticmethod
    def _clean_ticket_noise(text: str) -> str:
        if not text:
            return ""
        # Strip ServiceNow / Jira timestamp + author headers like "2026-03-11 14:22:10 - Jane Smith (Work notes):"
        cleaned = re.sub(
            r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s*-\s*[^(\n]+(\([^)]+\))?:?",
            " | ",
            text,
        )
        # Strip repetitive separator bars
        cleaned = re.sub(r"[-_=]{4,}", " ", cleaned)
        # Collapse excessive whitespace
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" |")
        return cleaned

    @staticmethod
    def _hash_file(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()
