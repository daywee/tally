"""
SQLite cache for rule metadata and match results.

Used to speed up rule CLI commands by avoiding repeated rule parsing
and storing match counts from the last run.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .merchant_engine import MerchantEngine, MerchantRule


@dataclass
class CachedRule:
    """Rule metadata loaded from the cache."""
    name: str
    match_expr: str
    category: str
    subcategory: str
    merchant: str
    tags: List[str]
    priority: int
    position: int
    source_line: int
    is_complex: bool
    let_bindings: List[Tuple[str, str]]
    fields: Dict[str, str]


class RuleCache:
    """SQLite-backed cache of rule metadata and match results."""

    def __init__(self, config_dir: Path):
        self.config_dir = Path(config_dir)
        self.cache_dir = self.config_dir / ".tally"
        self.db_path = self.cache_dir / "cache.db"

    def is_valid(self, rules_path: Path, data_files: Optional[Sequence[Path]] = None, require_data: bool = False) -> bool:
        """Return True if the cache matches the current rules/data hashes."""
        if not self.db_path.exists():
            return False
        if not rules_path.exists():
            return False

        rules_hash = hash_file(rules_path)
        cached_rules_hash = self._get_meta("rules_file_hash")
        if cached_rules_hash != rules_hash:
            return False

        if require_data:
            data_hash = hash_files(data_files or [])
            cached_data_hash = self._get_meta("data_files_hash")
            if cached_data_hash != data_hash:
                return False

        return True

    def rebuild(
        self,
        rules_path: Path,
        data_files: Sequence[Path],
        engine: MerchantEngine,
        transactions: Sequence[Dict],
        data_sources: Optional[Dict] = None,
    ) -> None:
        """Rebuild the cache from rules and transactions."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            self._ensure_schema(conn)
            self._clear_tables(conn)

            rule_id_map = self._insert_rules(conn, engine.rules)
            self._insert_transactions(conn, transactions)
            self._insert_matches(conn, engine, transactions, rule_id_map, data_sources=data_sources)
            self._store_preamble(conn, engine)

            self._set_meta(conn, "rules_file_hash", hash_file(rules_path))
            self._set_meta(conn, "data_files_hash", hash_files(data_files))
            self._set_meta(conn, "last_computed", datetime.now(timezone.utc).isoformat())

    def rebuild_rules_only(self, rules_path: Path, engine: MerchantEngine) -> None:
        """Rebuild cache with rules only (no transactions or matches)."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            self._ensure_schema(conn)
            self._clear_tables(conn)
            self._insert_rules(conn, engine.rules)
            self._store_preamble(conn, engine)
            self._set_meta(conn, "rules_file_hash", hash_file(rules_path) if rules_path.exists() else "")
            self._set_meta(conn, "data_files_hash", "")
            self._set_meta(conn, "last_computed", datetime.now(timezone.utc).isoformat())

    def get_rules(self) -> List[CachedRule]:
        """Return cached rules ordered by position."""
        with self._connect() as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                "SELECT name, match_expr, category, subcategory, merchant, tags, priority, position, source_line, "
                "is_complex, let_bindings, fields "
                "FROM rules ORDER BY position"
            ).fetchall()
        return [self._row_to_rule(row) for row in rows]

    def get_match_counts(self) -> Dict[str, int]:
        """Return match counts keyed by rule name."""
        with self._connect() as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                "SELECT rules.name, COUNT(matches.txn_id) AS match_count "
                "FROM rules LEFT JOIN matches ON matches.rule_id = rules.id "
                "GROUP BY rules.id ORDER BY rules.position"
            ).fetchall()
        return {row["name"]: int(row["match_count"]) for row in rows}

    def get_transactions(self) -> List[Dict]:
        """Return cached transactions for validation."""
        with self._connect() as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                "SELECT id, description, normalized_desc, amount, date, source FROM transactions"
            ).fetchall()

        transactions: List[Dict] = []
        for row in rows:
            transactions.append({
                "id": row["id"],
                "raw_description": row["description"],
                "description": row["normalized_desc"],
                "amount": row["amount"],
                "date": row["date"],
                "source": row["source"],
            })
        return transactions
    def get_unused_rules(self) -> List[CachedRule]:
        """Return rules with no matches."""
        with self._connect() as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                "SELECT rules.name, rules.match_expr, rules.category, rules.subcategory, rules.merchant, rules.tags, "
                "rules.priority, rules.position, rules.source_line, rules.is_complex, rules.let_bindings, rules.fields "
                "FROM rules LEFT JOIN matches ON matches.rule_id = rules.id "
                "WHERE matches.txn_id IS NULL ORDER BY rules.position"
            ).fetchall()
        return [self._row_to_rule(row) for row in rows]

    def search_rules(self, pattern: str) -> List[CachedRule]:
        """Find rules by name or match expression pattern."""
        like = f"%{pattern}%"
        with self._connect() as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                "SELECT name, match_expr, category, subcategory, merchant, tags, priority, position, source_line, "
                "is_complex, let_bindings, fields "
                "FROM rules WHERE name LIKE ? OR match_expr LIKE ? ORDER BY position",
                (like, like),
            ).fetchall()
        return [self._row_to_rule(row) for row in rows]

    def invalidate(self) -> None:
        """Mark the cache stale by clearing metadata."""
        if not self.db_path.exists():
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM cache_meta")

    def _connect(self) -> sqlite3.Connection:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rules (
                id INTEGER PRIMARY KEY,
                name TEXT,
                match_expr TEXT,
                category TEXT,
                subcategory TEXT,
                merchant TEXT,
                tags TEXT,
                priority INTEGER,
                position INTEGER,
                source_line INTEGER,
                is_complex INTEGER,
                let_bindings TEXT,
                fields TEXT
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id TEXT PRIMARY KEY,
                description TEXT,
                normalized_desc TEXT,
                amount REAL,
                date TEXT,
                source TEXT
            );
            CREATE TABLE IF NOT EXISTS matches (
                rule_id INTEGER,
                txn_id TEXT,
                match_type TEXT,
                PRIMARY KEY (rule_id, txn_id)
            );
            CREATE TABLE IF NOT EXISTS cache_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        # Ensure new columns exist for older caches.
        try:
            conn.execute("ALTER TABLE rules ADD COLUMN is_complex INTEGER")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE rules ADD COLUMN merchant TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE rules ADD COLUMN let_bindings TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE rules ADD COLUMN fields TEXT")
        except sqlite3.OperationalError:
            pass

    def _clear_tables(self, conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM rules")
        conn.execute("DELETE FROM transactions")
        conn.execute("DELETE FROM matches")
        conn.execute("DELETE FROM cache_meta")

    def _insert_rules(self, conn: sqlite3.Connection, rules: Sequence[MerchantRule]) -> Dict[int, int]:
        rule_id_map: Dict[int, int] = {}
        for position, rule in enumerate(rules):
            tags_json = json.dumps(sorted(rule.tags))
            is_complex = 1 if (rule.let_bindings or rule.fields) else 0
            let_bindings_json = json.dumps([[var, expr] for var, expr in rule.let_bindings])
            fields_json = json.dumps(rule.fields or {})
            cursor = conn.execute(
                "INSERT INTO rules (name, match_expr, category, subcategory, merchant, tags, priority, position, source_line, "
                "is_complex, let_bindings, fields) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rule.name,
                    rule.match_expr,
                    rule.category,
                    rule.subcategory,
                    rule.merchant,
                    tags_json,
                    rule.priority,
                    position,
                    rule.line_number,
                    is_complex,
                    let_bindings_json,
                    fields_json,
                ),
            )
            rule_id_map[id(rule)] = int(cursor.lastrowid)
        return rule_id_map

    def _insert_transactions(self, conn: sqlite3.Connection, transactions: Sequence[Dict]) -> None:
        rows: List[Tuple[str, str, str, float, str, str]] = []
        for txn in transactions:
            txn_id = transaction_id(txn)
            raw_desc = txn.get("raw_description", txn.get("description", ""))
            normalized_desc = txn.get("description", "")
            amount = float(txn.get("amount", 0.0))
            date_value = txn.get("date")
            if hasattr(date_value, "isoformat"):
                date_str = date_value.isoformat()
            else:
                date_str = str(date_value) if date_value is not None else ""
            source = txn.get("source", "")
            rows.append((txn_id, raw_desc, normalized_desc, amount, date_str, source))

        conn.executemany(
            "INSERT OR REPLACE INTO transactions (id, description, normalized_desc, amount, date, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )

    def _insert_matches(
        self,
        conn: sqlite3.Connection,
        engine: MerchantEngine,
        transactions: Sequence[Dict],
        rule_id_map: Dict[int, int],
        data_sources: Optional[Dict] = None,
    ) -> None:
        rows: List[Tuple[int, str, str]] = []
        for txn in transactions:
            result = engine.match(txn, data_sources=data_sources)
            txn_id = transaction_id(txn)
            for rule in result.all_matching_rules:
                rule_id = rule_id_map.get(id(rule))
                if rule_id is None:
                    continue
                match_type = "category" if rule.is_categorization_rule else "tag_only"
                rows.append((rule_id, txn_id, match_type))

        conn.executemany(
            "INSERT OR IGNORE INTO matches (rule_id, txn_id, match_type) VALUES (?, ?, ?)",
            rows,
        )

    def _get_meta(self, key: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM cache_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_meta(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta (key, value) VALUES (?, ?)",
            (key, value),
        )

    def _store_preamble(self, conn: sqlite3.Connection, engine: MerchantEngine) -> None:
        preamble_lines: List[str] = []
        if engine.variables:
            for name, expr in engine.variables.items():
                preamble_lines.append(f"{name} = {expr}")
        if engine.transforms:
            for field_path, expr in engine.transforms:
                preamble_lines.append(f"{field_path} = {expr}")
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta (key, value) VALUES (?, ?)",
            ("rules_preamble", json.dumps(preamble_lines)),
        )

    def _row_to_rule(self, row: sqlite3.Row) -> CachedRule:
        tags = json.loads(row["tags"]) if row["tags"] else []
        let_bindings = json.loads(row["let_bindings"]) if row["let_bindings"] else []
        fields = json.loads(row["fields"]) if row["fields"] else {}
        return CachedRule(
            name=row["name"],
            match_expr=row["match_expr"],
            category=row["category"] or "",
            subcategory=row["subcategory"] or "",
            merchant=row["merchant"] or row["name"],
            tags=tags,
            priority=int(row["priority"] or 50),
            position=int(row["position"] or 0),
            source_line=int(row["source_line"] or 0),
            is_complex=bool(row["is_complex"]),
            let_bindings=let_bindings,
            fields=fields,
        )

    def regenerate_rules_file(self, rules_path: Path) -> None:
        """Write the .rules file from cache, preserving order and preamble."""
        rules = self.get_rules()
        preamble_json = self._get_meta("rules_preamble")
        preamble_lines = json.loads(preamble_json) if preamble_json else []

        sections: List[str] = []
        if preamble_lines:
            sections.append("\n".join(preamble_lines))

        for rule in rules:
            sections.append(self._format_cached_rule(rule))

        content = "\n\n".join(sections).rstrip() + "\n"
        rules_path.write_text(content, encoding="utf-8")

        with self._connect() as conn:
            self._set_meta(conn, "rules_file_hash", hash_file(rules_path))

    def add_or_update_rule(
        self,
        name: str,
        match_expr: str,
        category: Optional[str],
        subcategory: Optional[str],
        tags: Optional[Iterable[str]],
        priority: Optional[int],
        merchant: Optional[str],
    ) -> Tuple[CachedRule, str]:
        """Add or update a rule by name or match expression."""
        with self._connect() as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT * FROM rules WHERE LOWER(name) = LOWER(?)",
                (name,),
            ).fetchone()

            if row is None:
                row = conn.execute(
                    "SELECT * FROM rules WHERE match_expr = ?",
                    (match_expr,),
                ).fetchone()

            if row is not None:
                existing = self._row_to_rule(row)
                updated_category = category if category is not None else existing.category
                updated_subcategory = subcategory if subcategory is not None else existing.subcategory
                updated_tags = list(tags) if tags is not None else existing.tags
                updated_priority = priority if priority is not None else existing.priority
                updated_merchant = merchant if merchant is not None else existing.merchant

                conn.execute(
                    "UPDATE rules SET match_expr = ?, category = ?, subcategory = ?, merchant = ?, tags = ?, "
                    "priority = ? WHERE id = ?",
                    (
                        match_expr,
                        updated_category,
                        updated_subcategory,
                        updated_merchant,
                        json.dumps(sorted(updated_tags)),
                        updated_priority,
                        row["id"],
                    ),
                )
                updated = CachedRule(
                    name=existing.name,
                    match_expr=match_expr,
                    category=updated_category,
                    subcategory=updated_subcategory,
                    merchant=updated_merchant,
                    tags=updated_tags,
                    priority=updated_priority,
                    position=existing.position,
                    source_line=existing.source_line,
                    is_complex=existing.is_complex,
                    let_bindings=existing.let_bindings,
                    fields=existing.fields,
                )
                return updated, "updated"

            position = conn.execute("SELECT COALESCE(MAX(position), -1) FROM rules").fetchone()[0] + 1
            tags_list = list(tags) if tags is not None else []
            conn.execute(
                "INSERT INTO rules (name, match_expr, category, subcategory, merchant, tags, priority, position, "
                "source_line, is_complex, let_bindings, fields) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    match_expr,
                    category or "",
                    subcategory or "",
                    merchant or name,
                    json.dumps(sorted(tags_list)),
                    priority if priority is not None else 50,
                    position,
                    0,
                    0,
                    json.dumps([]),
                    json.dumps({}),
                ),
            )
        added = CachedRule(
            name=name,
            match_expr=match_expr,
            category=category or "",
            subcategory=subcategory or "",
            merchant=merchant or name,
            tags=tags_list,
            priority=priority if priority is not None else 50,
            position=position,
            source_line=0,
            is_complex=False,
            let_bindings=[],
            fields={},
        )
        return added, "added"

    def update_rule(
        self,
        name: str,
        category: Optional[str],
        subcategory: Optional[str],
        add_tags: Optional[Iterable[str]],
        remove_tags: Optional[Iterable[str]],
        priority: Optional[int],
    ) -> Optional[CachedRule]:
        """Update a cached rule by name."""
        with self._connect() as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT * FROM rules WHERE LOWER(name) = LOWER(?)",
                (name,),
            ).fetchone()
            if row is None:
                return None
            existing = self._row_to_rule(row)
            tags = set(existing.tags)
            if add_tags:
                tags.update(add_tags)
            if remove_tags:
                tags.difference_update(remove_tags)
            updated_category = category if category is not None else existing.category
            updated_subcategory = subcategory if subcategory is not None else existing.subcategory
            updated_priority = priority if priority is not None else existing.priority

            conn.execute(
                "UPDATE rules SET category = ?, subcategory = ?, tags = ?, priority = ? WHERE id = ?",
                (
                    updated_category,
                    updated_subcategory,
                    json.dumps(sorted(tags)),
                    updated_priority,
                    row["id"],
                ),
            )

        return CachedRule(
            name=existing.name,
            match_expr=existing.match_expr,
            category=updated_category,
            subcategory=updated_subcategory,
            merchant=existing.merchant,
            tags=sorted(tags),
            priority=updated_priority,
            position=existing.position,
            source_line=existing.source_line,
            is_complex=existing.is_complex,
            let_bindings=existing.let_bindings,
            fields=existing.fields,
        )

    def delete_by_name(self, name: str) -> bool:
        """Delete a rule by name (case-insensitive)."""
        with self._connect() as conn:
            self._ensure_schema(conn)
            cursor = conn.execute(
                "DELETE FROM rules WHERE LOWER(name) = LOWER(?)",
                (name,),
            )
            return cursor.rowcount > 0

    def delete_by_match(self, match_expr: str) -> bool:
        """Delete a rule by match expression."""
        with self._connect() as conn:
            self._ensure_schema(conn)
            cursor = conn.execute(
                "DELETE FROM rules WHERE match_expr = ?",
                (match_expr,),
            )
            return cursor.rowcount > 0

    def mark_matches_stale(self) -> None:
        """Clear match data and mark cache as needing a rebuild."""
        with self._connect() as conn:
            self._ensure_schema(conn)
            conn.execute("DELETE FROM matches")
            conn.execute("DELETE FROM transactions")
            self._set_meta(conn, "data_files_hash", "")

    def _format_cached_rule(self, rule: CachedRule) -> str:
        lines = [f"[{rule.name}]"]
        if rule.priority != 50:
            lines.append(f"priority: {rule.priority}")
        for var_name, expr in rule.let_bindings:
            lines.append(f"let: {var_name} = {expr}")
        lines.append(f"match: {rule.match_expr}")
        if rule.merchant and rule.merchant != rule.name:
            lines.append(f"merchant: {rule.merchant}")
        if rule.category:
            lines.append(f"category: {rule.category}")
        if rule.subcategory:
            lines.append(f"subcategory: {rule.subcategory}")
        for field_name, expr in (rule.fields or {}).items():
            lines.append(f"field: {field_name} = {expr}")
        if rule.tags:
            lines.append(f"tags: {', '.join(sorted(rule.tags))}")
        return "\n".join(lines)


def transaction_id(txn: Dict) -> str:
    """Stable transaction ID for caching."""
    date_value = txn.get("date")
    if hasattr(date_value, "isoformat"):
        date_str = date_value.isoformat()
    else:
        date_str = str(date_value) if date_value is not None else ""
    raw_desc = txn.get("raw_description", txn.get("description", ""))
    amount = txn.get("amount", 0.0)
    source = txn.get("source", "")
    payload = f"{date_str}|{raw_desc}|{amount}|{source}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def hash_file(path: Path) -> str:
    """Hash a file by content."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def hash_files(paths: Iterable[Path]) -> str:
    """Hash multiple files by content and path."""
    hasher = hashlib.sha256()
    for path in sorted(Path(p) for p in paths):
        hasher.update(str(path).encode("utf-8"))
        hasher.update(hash_file(path).encode("utf-8"))
    return hasher.hexdigest()
