from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str


MIGRATIONS = (
    Migration(
        1,
        "create_stage_b_v2_schema",
        """
        CREATE TABLE IF NOT EXISTS source_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            source_account TEXT NOT NULL,
            external_message_id TEXT NOT NULL,
            source_url TEXT NOT NULL,
            original_text TEXT NOT NULL,
            original_media_url TEXT,
            published_at TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            raw_payload_json TEXT NOT NULL DEFAULT '{}',
            legacy_event_id INTEGER,
            created_at TEXT NOT NULL,
            UNIQUE(platform, source_account, external_message_id)
        );

        CREATE TABLE IF NOT EXISTS stories (
            story_id TEXT PRIMARY KEY,
            current_state TEXT NOT NULL,
            content_version INTEGER NOT NULL DEFAULT 1 CHECK(content_version >= 1),
            dedupe_key TEXT,
            dedupe_algorithm_version INTEGER NOT NULL DEFAULT 1,
            legacy_item_id INTEGER UNIQUE,
            legacy_state TEXT,
            blocked_reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS story_sources (
            story_id TEXT NOT NULL REFERENCES stories(story_id) ON DELETE CASCADE,
            source_event_id INTEGER NOT NULL UNIQUE
                REFERENCES source_events(id) ON DELETE RESTRICT,
            relation_type TEXT NOT NULL DEFAULT 'PRIMARY',
            is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0, 1)),
            linked_at TEXT NOT NULL,
            PRIMARY KEY(story_id, source_event_id)
        );

        CREATE TABLE IF NOT EXISTS story_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            story_id TEXT NOT NULL REFERENCES stories(story_id) ON DELETE CASCADE,
            version INTEGER NOT NULL CHECK(version >= 1),
            source_text_snapshot TEXT NOT NULL,
            editorial_text TEXT,
            primary_title TEXT,
            secondary_title TEXT,
            structured_facts_json TEXT,
            facts_schema_version INTEGER,
            generated_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(story_id, version)
        );

        CREATE TABLE IF NOT EXISTS media_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            story_id TEXT NOT NULL,
            story_version INTEGER NOT NULL,
            asset_type TEXT NOT NULL,
            source_url TEXT,
            local_path TEXT,
            provenance TEXT NOT NULL,
            validation_status TEXT NOT NULL,
            validation_report_json TEXT NOT NULL DEFAULT '{}',
            sha256 TEXT,
            width INTEGER,
            height INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY(story_id, story_version)
                REFERENCES story_versions(story_id, version) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS publications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            story_id TEXT NOT NULL REFERENCES stories(story_id) ON DELETE CASCADE,
            content_version INTEGER NOT NULL CHECK(content_version >= 1),
            channel TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            remote_message_id TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            published_at TEXT
        );

        CREATE TABLE IF NOT EXISTS owner_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            story_id TEXT NOT NULL REFERENCES stories(story_id) ON DELETE CASCADE,
            action_type TEXT NOT NULL,
            reason TEXT,
            remote_message_id TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS state_transitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            story_id TEXT NOT NULL REFERENCES stories(story_id) ON DELETE CASCADE,
            from_state TEXT,
            to_state TEXT NOT NULL,
            reason_code TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS shadow_comparisons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            legacy_item_id INTEGER NOT NULL UNIQUE,
            story_id TEXT NOT NULL REFERENCES stories(story_id) ON DELETE CASCADE,
            legacy_state TEXT NOT NULL,
            mapped_v2_state TEXT NOT NULL,
            mapping_result TEXT NOT NULL,
            warning_codes TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        );
        """,
    ),
    Migration(
        2,
        "create_stage_b_v2_indexes",
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_story_sources_one_primary
            ON story_sources(story_id) WHERE is_primary = 1;
        CREATE INDEX IF NOT EXISTS idx_story_sources_event
            ON story_sources(source_event_id);
        CREATE INDEX IF NOT EXISTS idx_stories_state
            ON stories(current_state, updated_at);
        CREATE INDEX IF NOT EXISTS idx_stories_dedupe
            ON stories(dedupe_key, dedupe_algorithm_version);
        CREATE INDEX IF NOT EXISTS idx_story_versions_story
            ON story_versions(story_id, version);
        CREATE INDEX IF NOT EXISTS idx_media_assets_story_version
            ON media_assets(story_id, story_version);
        CREATE INDEX IF NOT EXISTS idx_publications_status
            ON publications(channel, status, updated_at);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_publications_telegram_version
            ON publications(channel, story_id, content_version)
            WHERE channel = 'telegram';
        CREATE INDEX IF NOT EXISTS idx_owner_actions_story
            ON owner_actions(story_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_state_transitions_story
            ON state_transitions(story_id, id);
        """,
    ),
    Migration(
        3,
        "create_stage_c_facts_copy_schema",
        """
        CREATE TABLE IF NOT EXISTS llm_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            story_id TEXT NOT NULL REFERENCES stories(story_id) ON DELETE CASCADE,
            story_version INTEGER NOT NULL,
            task_type TEXT NOT NULL,
            model_name TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            temperature REAL NOT NULL,
            seed INTEGER,
            input_hash TEXT NOT NULL,
            raw_response TEXT NOT NULL,
            parsed_response_json TEXT,
            validation_status TEXT NOT NULL,
            error_code TEXT,
            duration_ms INTEGER NOT NULL CHECK(duration_ms >= 0),
            attempt_number INTEGER NOT NULL CHECK(attempt_number >= 1),
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS fact_extractions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            story_id TEXT NOT NULL REFERENCES stories(story_id) ON DELETE CASCADE,
            story_version INTEGER NOT NULL,
            facts_schema_version TEXT NOT NULL,
            extracted_facts_json TEXT NOT NULL,
            extraction_status TEXT NOT NULL,
            llm_run_id INTEGER REFERENCES llm_runs(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            UNIQUE(story_id, story_version, facts_schema_version)
        );

        CREATE TABLE IF NOT EXISTS fact_validation_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            story_id TEXT NOT NULL REFERENCES stories(story_id) ON DELETE CASCADE,
            story_version INTEGER NOT NULL,
            passed INTEGER NOT NULL CHECK(passed IN (0, 1)),
            blocking_errors_json TEXT NOT NULL DEFAULT '[]',
            warnings_json TEXT NOT NULL DEFAULT '[]',
            field_results_json TEXT NOT NULL DEFAULT '{}',
            validator_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(story_id, story_version, validator_version)
        );

        CREATE TABLE IF NOT EXISTS editorial_outputs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            story_id TEXT NOT NULL REFERENCES stories(story_id) ON DELETE CASCADE,
            story_version INTEGER NOT NULL,
            language TEXT NOT NULL,
            post_text TEXT NOT NULL,
            primary_title TEXT NOT NULL,
            secondary_title TEXT NOT NULL,
            source_label TEXT NOT NULL,
            validation_status TEXT NOT NULL,
            validation_report_json TEXT NOT NULL DEFAULT '{}',
            llm_run_ids TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            UNIQUE(story_id, story_version, language)
        );

        CREATE TABLE IF NOT EXISTS classification_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            story_id TEXT NOT NULL REFERENCES stories(story_id) ON DELETE CASCADE,
            story_version INTEGER NOT NULL,
            decision_json TEXT NOT NULL,
            classifier_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(story_id, story_version, classifier_version)
        );

        CREATE TABLE IF NOT EXISTS entities (
            entity_id TEXT PRIMARY KEY,
            entity_type TEXT NOT NULL,
            canonical_name TEXT NOT NULL,
            display_name_ru TEXT,
            surname_display TEXT,
            status TEXT NOT NULL,
            provenance TEXT NOT NULL,
            grammatical_forms_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS entity_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
            language TEXT NOT NULL,
            alias TEXT NOT NULL,
            normalized_alias TEXT NOT NULL,
            alias_type TEXT NOT NULL,
            provenance TEXT NOT NULL,
            confidence_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(entity_id, language, normalized_alias)
        );

        CREATE TABLE IF NOT EXISTS shadow_stage_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            story_id TEXT NOT NULL REFERENCES stories(story_id) ON DELETE CASCADE,
            story_version INTEGER NOT NULL DEFAULT 1,
            stage_name TEXT NOT NULL,
            proposed_from_state TEXT,
            proposed_to_state TEXT NOT NULL,
            result TEXT NOT NULL,
            blocking_codes TEXT NOT NULL DEFAULT '[]',
            warning_codes TEXT NOT NULL DEFAULT '[]',
            run_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(story_id, story_version, stage_name, run_key)
        );

        CREATE INDEX IF NOT EXISTS idx_llm_runs_story
            ON llm_runs(story_id, story_version, task_type);
        CREATE INDEX IF NOT EXISTS idx_entity_alias_normalized
            ON entity_aliases(normalized_alias, language);
        CREATE INDEX IF NOT EXISTS idx_shadow_stage_run
            ON shadow_stage_results(run_key, stage_name);
        """,
    ),
    Migration(
        4,
        "create_stage_d1_factual_shadow_results",
        """
        CREATE TABLE IF NOT EXISTS shadow_factual_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            story_id TEXT NOT NULL REFERENCES stories(story_id) ON DELETE CASCADE,
            factual_core_version TEXT NOT NULL,
            factual_core_freeze_commit TEXT NOT NULL,
            source_event_id INTEGER NOT NULL REFERENCES source_events(id) ON DELETE RESTRICT,
            processed_at TEXT NOT NULL,
            decision TEXT NOT NULL CHECK(decision IN ('routed', 'blocked', 'error')),
            event_family TEXT,
            transfer_direction TEXT,
            player_entity_id TEXT,
            entities_json TEXT NOT NULL DEFAULT '[]',
            from_club_id TEXT,
            to_clubs_json TEXT NOT NULL DEFAULT '[]',
            amount_value INTEGER,
            currency TEXT,
            contract_until TEXT,
            negated INTEGER NOT NULL DEFAULT 0 CHECK(negated IN (0, 1)),
            evidence_json TEXT NOT NULL DEFAULT '[]',
            block_reason TEXT,
            error_code TEXT,
            latency_ms REAL NOT NULL CHECK(latency_ms >= 0),
            source_metadata_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(story_id, factual_core_version)
        );

        CREATE INDEX IF NOT EXISTS idx_shadow_factual_results_core
            ON shadow_factual_results(factual_core_version, decision);
        """,
    ),
    Migration(
        5,
        "create_stage_d2_shadow_editorial_results",
        """
        CREATE TABLE IF NOT EXISTS shadow_editorial_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            story_id TEXT NOT NULL REFERENCES stories(story_id) ON DELETE CASCADE,
            factual_core_version TEXT NOT NULL,
            editorial_prompt_version TEXT NOT NULL,
            factual_result_processed_at TEXT NOT NULL,
            editorial_status TEXT NOT NULL
                CHECK(editorial_status IN ('generated', 'blocked_validation', 'model_error')),
            headline TEXT,
            body TEXT,
            model_backend TEXT,
            model_name TEXT,
            validation_errors_json TEXT NOT NULL DEFAULT '[]',
            generated_at TEXT NOT NULL,
            latency_ms REAL NOT NULL CHECK(latency_ms >= 0),
            UNIQUE(story_id, factual_core_version, editorial_prompt_version)
        );

        CREATE INDEX IF NOT EXISTS idx_shadow_editorial_results_lookup
            ON shadow_editorial_results(factual_core_version, editorial_prompt_version, editorial_status);
        """,
    ),
    Migration(
        6,
        "create_stage_d24_shadow_editorial_audits",
        """
        CREATE TABLE IF NOT EXISTS shadow_editorial_audits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            story_id TEXT NOT NULL REFERENCES stories(story_id) ON DELETE CASCADE,
            factual_core_version TEXT NOT NULL,
            editorial_prompt_version TEXT NOT NULL,
            deterministic_fallback_version TEXT NOT NULL,
            trusted_fact_version TEXT NOT NULL,
            llm_attempted INTEGER NOT NULL CHECK(llm_attempted IN (0, 1)),
            llm_status TEXT NOT NULL,
            llm_validation_errors_json TEXT NOT NULL DEFAULT '[]',
            fallback_attempted INTEGER NOT NULL CHECK(fallback_attempted IN (0, 1)),
            fallback_reason_json TEXT NOT NULL DEFAULT '[]',
            fallback_validation_errors_json TEXT NOT NULL DEFAULT '[]',
            generation_source TEXT NOT NULL CHECK(generation_source IN ('llm', 'deterministic_fallback', 'none')),
            editorial_status TEXT NOT NULL CHECK(editorial_status IN ('generated', 'generated_fallback', 'blocked_validation', 'model_error')),
            headline TEXT,
            body TEXT,
            model_backend TEXT,
            model_name TEXT,
            generated_at TEXT NOT NULL,
            latency_ms REAL NOT NULL CHECK(latency_ms >= 0),
            UNIQUE(story_id, factual_core_version, editorial_prompt_version, deterministic_fallback_version)
        );

        CREATE INDEX IF NOT EXISTS idx_shadow_editorial_audits_status
            ON shadow_editorial_audits(factual_core_version, editorial_prompt_version, editorial_status);
        """,
    ),
    Migration(
        7,
        "create_stage_d3_shadow_image_results",
        """
        CREATE TABLE IF NOT EXISTS shadow_image_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            story_id TEXT NOT NULL REFERENCES stories(story_id) ON DELETE CASCADE,
            factual_core_version TEXT NOT NULL,
            editorial_prompt_version TEXT NOT NULL,
            editorial_generation_source TEXT NOT NULL,
            image_status TEXT NOT NULL,
            template_id TEXT,
            template_version TEXT,
            source_image_ref TEXT,
            source_image_hash TEXT,
            processed_image_ref TEXT,
            processed_image_hash TEXT,
            final_image_ref TEXT,
            final_image_hash TEXT,
            width INTEGER,
            height INTEGER,
            format TEXT,
            preprocessing_steps_json TEXT NOT NULL DEFAULT '[]',
            qc_errors_json TEXT NOT NULL DEFAULT '[]',
            rendered_at TEXT NOT NULL,
            latency_ms REAL NOT NULL CHECK(latency_ms >= 0),
            UNIQUE(story_id, factual_core_version, editorial_prompt_version, template_version, source_image_hash)
        );
        """,
    ),
    Migration(8, "create_stage_d4_shadow_publication_packages", """
        CREATE TABLE IF NOT EXISTS shadow_publication_packages (
            package_id TEXT PRIMARY KEY,
            story_id TEXT NOT NULL REFERENCES stories(story_id) ON DELETE CASCADE,
            final_status TEXT NOT NULL,
            package_json TEXT NOT NULL,
            package_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
    """),
)


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_migration_table(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def apply_migrations(db: sqlite3.Connection) -> tuple[int, ...]:
    """Apply every missing additive V2 migration atomically and repeatably."""
    ensure_migration_table(db)
    db.commit()
    applied = {
        int(row[0])
        for row in db.execute("SELECT version FROM schema_migrations").fetchall()
    }
    installed: list[int] = []
    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        name = migration.name.replace("'", "''")
        applied_at = utc_iso().replace("'", "''")
        db.executescript(
            "BEGIN IMMEDIATE;\n"
            f"{migration.sql}\n"
            "INSERT INTO schema_migrations(version, name, applied_at) "
            f"VALUES ({migration.version}, '{name}', '{applied_at}');\n"
            "COMMIT;"
        )
        installed.append(migration.version)
    return tuple(installed)
