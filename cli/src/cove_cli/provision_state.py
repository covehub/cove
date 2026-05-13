from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


REGISTERED_ARTIFACTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS registered_artifacts (
    hub_path TEXT PRIMARY KEY,
    artifact_name TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    owner_domain TEXT NOT NULL,
    owner_url TEXT NOT NULL,
    plaintext_hash TEXT NOT NULL,
    ciphertext_hash TEXT NOT NULL,
    content_type TEXT NOT NULL,
    source_path TEXT NOT NULL,
    server_url TEXT NOT NULL,
    transport_mode TEXT NOT NULL,
    mapping_id TEXT NOT NULL,
    key_path TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


ALLOW_RULES_SCHEMA = """
CREATE TABLE artifact_allow_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id TEXT NOT NULL,
    hub_path TEXT NOT NULL,
    publisher TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    compose_hash TEXT NOT NULL,
    artifact_provisioner_digest TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(
        hub_path,
        publisher,
        workflow_id,
        node_id,
        compose_hash,
        artifact_provisioner_digest
    )
);
"""


DYNAMIC_ARTIFACT_CHANNELS_SCHEMA = """
CREATE TABLE IF NOT EXISTS dynamic_artifact_channels (
    hub_path TEXT PRIMARY KEY,
    artifact_name TEXT NOT NULL,
    owner_domain TEXT NOT NULL,
    publisher TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    key_path TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class RegisteredArtifact:
    hub_path: str
    artifact_id: str
    owner_domain: str
    owner_url: str
    plaintext_hash: str
    ciphertext_hash: str
    content_type: str
    source_path: str
    server_url: str
    transport_mode: str
    key_path: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ArtifactAllowRule:
    artifact_id: str
    hub_path: str
    publisher: str
    workflow_id: str
    node_id: str
    compose_hash: str
    artifact_provisioner_digest: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class DynamicArtifactChannel:
    hub_path: str
    artifact_name: str
    owner_domain: str
    publisher: str
    workflow_id: str
    key_path: str
    updated_at: str


class ProvisionState:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            _reject_legacy_schema(connection)
            connection.executescript(REGISTERED_ARTIFACTS_SCHEMA)
            _ensure_allow_rules_schema(connection)
            connection.executescript(DYNAMIC_ARTIFACT_CHANNELS_SCHEMA)
            connection.commit()

    def upsert_dynamic_artifact_channel(
        self,
        *,
        hub_path: str,
        artifact_name: str,
        owner_domain: str,
        publisher: str,
        workflow_id: str,
        key_path: str,
    ) -> DynamicArtifactChannel:
        updated_at = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO dynamic_artifact_channels (
                    hub_path,
                    artifact_name,
                    owner_domain,
                    publisher,
                    workflow_id,
                    key_path,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(hub_path) DO UPDATE SET
                    artifact_name = excluded.artifact_name,
                    owner_domain = excluded.owner_domain,
                    publisher = excluded.publisher,
                    workflow_id = excluded.workflow_id,
                    key_path = excluded.key_path,
                    updated_at = excluded.updated_at
                """,
                (
                    hub_path,
                    artifact_name,
                    owner_domain,
                    publisher,
                    workflow_id,
                    key_path,
                    updated_at,
                ),
            )
            connection.commit()
        channel = self.get_dynamic_artifact_channel(hub_path)
        assert channel is not None
        return channel

    def upsert_registered_artifact(
        self,
        *,
        hub_path: str,
        artifact_id: str,
        owner_domain: str,
        owner_url: str,
        plaintext_hash: str,
        ciphertext_hash: str,
        content_type: str,
        source_path: str,
        server_url: str,
        transport_mode: str,
        key_path: str,
    ) -> RegisteredArtifact:
        updated_at = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO registered_artifacts (
                    hub_path,
                    artifact_name,
                    artifact_id,
                    owner_domain,
                    owner_url,
                    plaintext_hash,
                    ciphertext_hash,
                    content_type,
                    source_path,
                    server_url,
                    transport_mode,
                    mapping_id,
                    key_path,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(hub_path) DO UPDATE SET
                    artifact_name = excluded.artifact_name,
                    artifact_id = excluded.artifact_id,
                    owner_domain = excluded.owner_domain,
                    owner_url = excluded.owner_url,
                    plaintext_hash = excluded.plaintext_hash,
                    ciphertext_hash = excluded.ciphertext_hash,
                    content_type = excluded.content_type,
                    source_path = excluded.source_path,
                    server_url = excluded.server_url,
                    transport_mode = excluded.transport_mode,
                    mapping_id = excluded.mapping_id,
                    key_path = excluded.key_path,
                    updated_at = excluded.updated_at
                """,
                (
                    hub_path,
                    artifact_id,
                    artifact_id,
                    owner_domain,
                    owner_url,
                    plaintext_hash,
                    ciphertext_hash,
                    content_type,
                    source_path,
                    server_url,
                    transport_mode,
                    "",
                    key_path,
                    updated_at,
                ),
            )
            connection.commit()
        artifact = self.get_registered_artifact(hub_path)
        assert artifact is not None
        return artifact

    def get_registered_artifact(self, hub_path: str) -> RegisteredArtifact | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    hub_path,
                    artifact_id,
                    owner_domain,
                    owner_url,
                    plaintext_hash,
                    ciphertext_hash,
                    content_type,
                    source_path,
                    server_url,
                    transport_mode,
                    key_path,
                    updated_at
                FROM registered_artifacts
                WHERE hub_path = ?
                """,
                (hub_path,),
            ).fetchone()
        return _registered_artifact_from_row(row)

    def get_registered_artifact_by_artifact_id(
        self,
        artifact_id: str,
    ) -> RegisteredArtifact | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    hub_path,
                    artifact_id,
                    owner_domain,
                    owner_url,
                    plaintext_hash,
                    ciphertext_hash,
                    content_type,
                    source_path,
                    server_url,
                    transport_mode,
                    key_path,
                    updated_at
                FROM registered_artifacts
                WHERE artifact_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (artifact_id,),
            ).fetchone()
        return _registered_artifact_from_row(row)

    def list_registered_artifacts(self) -> list[RegisteredArtifact]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    hub_path,
                    artifact_id,
                    owner_domain,
                    owner_url,
                    plaintext_hash,
                    ciphertext_hash,
                    content_type,
                    source_path,
                    server_url,
                    transport_mode,
                    key_path,
                    updated_at
                FROM registered_artifacts
                ORDER BY hub_path ASC
                """
            ).fetchall()
        return [_registered_artifact_from_row(row) for row in rows if row is not None]

    def get_dynamic_artifact_channel(self, hub_path: str) -> DynamicArtifactChannel | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    hub_path,
                    artifact_name,
                    owner_domain,
                    publisher,
                    workflow_id,
                    key_path,
                    updated_at
                FROM dynamic_artifact_channels
                WHERE hub_path = ?
                """,
                (hub_path,),
            ).fetchone()
        if row is None:
            return None
        return DynamicArtifactChannel(
            hub_path=str(row["hub_path"]),
            artifact_name=str(row["artifact_name"]),
            owner_domain=str(row["owner_domain"]),
            publisher=str(row["publisher"]),
            workflow_id=str(row["workflow_id"]),
            key_path=str(row["key_path"]),
            updated_at=str(row["updated_at"]),
        )

    def list_dynamic_artifact_channels(self) -> list[DynamicArtifactChannel]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    hub_path,
                    artifact_name,
                    owner_domain,
                    publisher,
                    workflow_id,
                    key_path,
                    updated_at
                FROM dynamic_artifact_channels
                ORDER BY hub_path ASC
                """
            ).fetchall()
        return [
            DynamicArtifactChannel(
                hub_path=str(row["hub_path"]),
                artifact_name=str(row["artifact_name"]),
                owner_domain=str(row["owner_domain"]),
                publisher=str(row["publisher"]),
                workflow_id=str(row["workflow_id"]),
                key_path=str(row["key_path"]),
                updated_at=str(row["updated_at"]),
            )
            for row in rows
        ]

    def upsert_allow_rule(
        self,
        *,
        artifact_id: str,
        hub_path: str,
        publisher: str,
        workflow_id: str,
        node_id: str,
        compose_hash: str,
        artifact_provisioner_digest: str,
    ) -> ArtifactAllowRule:
        updated_at = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO artifact_allow_rules (
                    artifact_id,
                    hub_path,
                    publisher,
                    workflow_id,
                    node_id,
                    compose_hash,
                    artifact_provisioner_digest,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    hub_path,
                    publisher,
                    workflow_id,
                    node_id,
                    compose_hash,
                    artifact_provisioner_digest
                ) DO UPDATE SET
                    artifact_id = excluded.artifact_id,
                    updated_at = excluded.updated_at
                """,
                (
                    artifact_id,
                    hub_path,
                    publisher,
                    workflow_id,
                    node_id,
                    compose_hash,
                    artifact_provisioner_digest,
                    updated_at,
                ),
            )
            connection.commit()
        rule = self.get_allow_rule(
            hub_path=hub_path,
            publisher=publisher,
            workflow_id=workflow_id,
            node_id=node_id,
            compose_hash=compose_hash,
            artifact_provisioner_digest=artifact_provisioner_digest,
        )
        assert rule is not None
        return rule

    def get_allow_rule(
        self,
        *,
        hub_path: str,
        publisher: str,
        workflow_id: str,
        node_id: str,
        compose_hash: str,
        artifact_provisioner_digest: str,
    ) -> ArtifactAllowRule | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    artifact_id,
                    hub_path,
                    publisher,
                    workflow_id,
                    node_id,
                    compose_hash,
                    artifact_provisioner_digest,
                    updated_at
                FROM artifact_allow_rules
                WHERE
                    hub_path = ?
                    AND publisher = ?
                    AND workflow_id = ?
                    AND node_id = ?
                    AND compose_hash = ?
                    AND artifact_provisioner_digest = ?
                """,
                (
                    hub_path,
                    publisher,
                    workflow_id,
                    node_id,
                    compose_hash,
                    artifact_provisioner_digest,
                ),
            ).fetchone()
        if row is None:
            return None
        return ArtifactAllowRule(
            artifact_id=str(row["artifact_id"]),
            hub_path=str(row["hub_path"]),
            publisher=str(row["publisher"]),
            workflow_id=str(row["workflow_id"]),
            node_id=str(row["node_id"]),
            compose_hash=str(row["compose_hash"]),
            artifact_provisioner_digest=str(row["artifact_provisioner_digest"]),
            updated_at=str(row["updated_at"]),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection


def _registered_artifact_from_row(row: sqlite3.Row | None) -> RegisteredArtifact | None:
    if row is None:
        return None
    return RegisteredArtifact(
        hub_path=str(row["hub_path"]),
        artifact_id=str(row["artifact_id"]),
        owner_domain=str(row["owner_domain"]),
        owner_url=str(row["owner_url"]),
        plaintext_hash=str(row["plaintext_hash"]),
        ciphertext_hash=str(row["ciphertext_hash"]),
        content_type=str(row["content_type"]),
        source_path=str(row["source_path"]),
        server_url=str(row["server_url"]),
        transport_mode=str(row["transport_mode"]),
        key_path=str(row["key_path"]),
        updated_at=str(row["updated_at"]),
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )


def _reject_legacy_schema(connection: sqlite3.Connection) -> None:
    registered_columns = _table_columns(connection, "registered_artifacts")
    dynamic_columns = _table_columns(connection, "dynamic_artifact_channels")
    if "username" in registered_columns or "owner_name" in dynamic_columns:
        raise RuntimeError(
            "local provision state uses the removed username schema; remove this Cove home and re-run cove init"
        )


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def _ensure_allow_rules_schema(connection: sqlite3.Connection) -> None:
    table_exists = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'artifact_allow_rules'
        """
    ).fetchone()
    if table_exists is None:
        connection.executescript(ALLOW_RULES_SCHEMA)
        return

    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(artifact_allow_rules)").fetchall()
    }
    if "manifest_hash" not in columns:
        return

    connection.execute("DROP TABLE artifact_allow_rules")
    connection.executescript(ALLOW_RULES_SCHEMA)
