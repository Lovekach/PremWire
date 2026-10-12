from __future__ import annotations

import json
import sqlite3
from typing import Any

from .schema import utc_iso


class StageCRepository:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def save_llm_run(self, values: dict[str, Any]) -> int:
        cursor = self.db.execute(
            """
            INSERT INTO llm_runs(
                story_id, story_version, task_type, model_name, prompt_version,
                schema_version, temperature, seed, input_hash, raw_response,
                parsed_response_json, validation_status, error_code, duration_ms,
                attempt_number, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                values["story_id"], values["story_version"], values["task_type"],
                values["model_name"], values["prompt_version"],
                values["schema_version"], values["temperature"], values.get("seed"),
                values["input_hash"], values["raw_response"],
                values.get("parsed_response_json"), values["validation_status"],
                values.get("error_code"), values["duration_ms"],
                values["attempt_number"], utc_iso(),
            ),
        )
        return int(cursor.lastrowid)

    def save_fact_extraction(
        self,
        *,
        story_id: str,
        story_version: int,
        payload: dict[str, Any],
        status: str,
        llm_run_id: int | None,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO fact_extractions(
                story_id, story_version, facts_schema_version,
                extracted_facts_json, extraction_status, llm_run_id, created_at
            ) VALUES (?, ?, '1.0', ?, ?, ?, ?)
            ON CONFLICT(story_id, story_version, facts_schema_version)
            DO UPDATE SET extracted_facts_json=excluded.extracted_facts_json,
                extraction_status=excluded.extraction_status,
                llm_run_id=excluded.llm_run_id
            """,
            (
                story_id, story_version,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                status, llm_run_id, utc_iso(),
            ),
        )

    def save_validation_report(
        self,
        *,
        story_id: str,
        story_version: int,
        passed: bool,
        blocking_errors: tuple[str, ...],
        warnings: tuple[str, ...],
        field_results: dict[str, str],
        validator_version: str,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO fact_validation_reports(
                story_id, story_version, passed, blocking_errors_json,
                warnings_json, field_results_json, validator_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(story_id, story_version, validator_version)
            DO UPDATE SET passed=excluded.passed,
                blocking_errors_json=excluded.blocking_errors_json,
                warnings_json=excluded.warnings_json,
                field_results_json=excluded.field_results_json
            """,
            (
                story_id, story_version, int(passed),
                json.dumps(blocking_errors, ensure_ascii=False),
                json.dumps(warnings, ensure_ascii=False),
                json.dumps(field_results, ensure_ascii=False, sort_keys=True),
                validator_version, utc_iso(),
            ),
        )

    def save_classification(
        self,
        *,
        story_id: str,
        story_version: int,
        decision: dict[str, Any],
        classifier_version: str,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO classification_decisions(
                story_id, story_version, decision_json, classifier_version, created_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(story_id, story_version, classifier_version)
            DO UPDATE SET decision_json=excluded.decision_json
            """,
            (
                story_id, story_version,
                json.dumps(decision, ensure_ascii=False, sort_keys=True),
                classifier_version, utc_iso(),
            ),
        )

    def save_editorial(
        self,
        *,
        story_id: str,
        story_version: int,
        language: str,
        post_text: str,
        primary_title: str,
        secondary_title: str,
        source_label: str,
        validation_status: str,
        validation_report: dict[str, Any],
        llm_run_ids: list[int],
    ) -> None:
        self.db.execute(
            """
            INSERT INTO editorial_outputs(
                story_id, story_version, language, post_text, primary_title,
                secondary_title, source_label, validation_status,
                validation_report_json, llm_run_ids, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(story_id, story_version, language)
            DO UPDATE SET post_text=excluded.post_text,
                primary_title=excluded.primary_title,
                secondary_title=excluded.secondary_title,
                source_label=excluded.source_label,
                validation_status=excluded.validation_status,
                validation_report_json=excluded.validation_report_json,
                llm_run_ids=excluded.llm_run_ids
            """,
            (
                story_id, story_version, language, post_text, primary_title,
                secondary_title, source_label, validation_status,
                json.dumps(validation_report, ensure_ascii=False, sort_keys=True),
                json.dumps(llm_run_ids), utc_iso(),
            ),
        )

    def save_shadow_stage(
        self,
        *,
        story_id: str,
        story_version: int,
        stage_name: str,
        proposed_from_state: str | None,
        proposed_to_state: str,
        result: str,
        blocking_codes: list[str],
        warning_codes: list[str],
        run_key: str,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO shadow_stage_results(
                story_id, story_version, stage_name, proposed_from_state,
                proposed_to_state, result, blocking_codes, warning_codes,
                run_key, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(story_id, story_version, stage_name, run_key) DO NOTHING
            """,
            (
                story_id, story_version, stage_name, proposed_from_state,
                proposed_to_state, result,
                json.dumps(blocking_codes, ensure_ascii=False),
                json.dumps(warning_codes, ensure_ascii=False),
                run_key, utc_iso(),
            ),
        )

    def save_factual_shadow_result(self, payload: dict[str, Any]) -> None:
        """Upsert a Stage D.1 result without touching story or publication state."""
        self.db.execute(
            """
            INSERT INTO shadow_factual_results(
                story_id, factual_core_version, factual_core_freeze_commit,
                source_event_id, processed_at, decision, event_family,
                transfer_direction, player_entity_id, entities_json, from_club_id,
                to_clubs_json, amount_value, currency, contract_until, negated,
                evidence_json, block_reason, error_code, latency_ms,
                source_metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(story_id, factual_core_version) DO UPDATE SET
                source_event_id=excluded.source_event_id,
                processed_at=excluded.processed_at,
                decision=excluded.decision,
                event_family=excluded.event_family,
                transfer_direction=excluded.transfer_direction,
                player_entity_id=excluded.player_entity_id,
                entities_json=excluded.entities_json,
                from_club_id=excluded.from_club_id,
                to_clubs_json=excluded.to_clubs_json,
                amount_value=excluded.amount_value,
                currency=excluded.currency,
                contract_until=excluded.contract_until,
                negated=excluded.negated,
                evidence_json=excluded.evidence_json,
                block_reason=excluded.block_reason,
                error_code=excluded.error_code,
                latency_ms=excluded.latency_ms,
                source_metadata_json=excluded.source_metadata_json
            """,
            (
                payload["story_id"], payload["factual_core_version"],
                payload["factual_core_freeze_commit"], payload["source_event_id"],
                payload["processed_at"], payload["decision"],
                payload.get("event_family"), payload.get("transfer_direction"),
                payload.get("player_entity_id"),
                json.dumps(payload.get("entities", []), ensure_ascii=False, sort_keys=True),
                payload.get("from_club_id"),
                json.dumps(payload.get("to_club_ids", []), ensure_ascii=False, sort_keys=True),
                payload.get("amount_value"), payload.get("currency"),
                payload.get("contract_until"), int(bool(payload.get("negated"))),
                json.dumps(payload.get("evidence", []), ensure_ascii=False, sort_keys=True),
                payload.get("block_reason"), payload.get("error_code"),
                payload["latency_ms"],
                json.dumps(payload.get("source_metadata", {}), ensure_ascii=False, sort_keys=True),
            ),
        )

    def load_factual_shadow_result(
        self, story_id: str, factual_core_version: str,
    ) -> dict[str, Any] | None:
        row = self.db.execute(
            """
            SELECT * FROM shadow_factual_results
            WHERE story_id = ? AND factual_core_version = ?
            """,
            (story_id, factual_core_version),
        ).fetchone()
        if row is None:
            return None
        return {
            "story_id": str(row["story_id"]),
            "factual_core_version": str(row["factual_core_version"]),
            "factual_core_freeze_commit": str(row["factual_core_freeze_commit"]),
            "source_event_id": int(row["source_event_id"]),
            "processed_at": str(row["processed_at"]),
            "decision": str(row["decision"]),
            "event_family": row["event_family"],
            "transfer_direction": row["transfer_direction"],
            "player_entity_id": row["player_entity_id"],
            "entities": json.loads(str(row["entities_json"])),
            "from_club_id": row["from_club_id"],
            "to_club_ids": json.loads(str(row["to_clubs_json"])),
            "amount_value": row["amount_value"],
            "currency": row["currency"],
            "contract_until": row["contract_until"],
            "negated": bool(row["negated"]),
            "evidence": json.loads(str(row["evidence_json"])),
            "block_reason": row["block_reason"],
            "error_code": row["error_code"],
            "latency_ms": float(row["latency_ms"]),
            "source_metadata": json.loads(str(row["source_metadata_json"])),
        }

    def save_shadow_editorial_result(self, payload: dict[str, Any]) -> None:
        """Upsert a D.2-only editorial artifact; publication tables stay untouched."""
        self.db.execute(
            """
            INSERT INTO shadow_editorial_results(
                story_id, factual_core_version, editorial_prompt_version,
                factual_result_processed_at, editorial_status, headline, body,
                model_backend, model_name, validation_errors_json, generated_at,
                latency_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(story_id, factual_core_version, editorial_prompt_version) DO UPDATE SET
                factual_result_processed_at=excluded.factual_result_processed_at,
                editorial_status=excluded.editorial_status,
                headline=excluded.headline, body=excluded.body,
                model_backend=excluded.model_backend, model_name=excluded.model_name,
                validation_errors_json=excluded.validation_errors_json,
                generated_at=excluded.generated_at, latency_ms=excluded.latency_ms
            """,
            (
                payload["story_id"], payload["factual_core_version"],
                payload["editorial_prompt_version"], payload["factual_result_processed_at"],
                payload["editorial_status"], payload.get("headline"), payload.get("body"),
                payload.get("model_backend"), payload.get("model_name"),
                json.dumps(payload.get("validation_errors", []), ensure_ascii=False, sort_keys=True),
                payload["generated_at"], payload["latency_ms"],
            ),
        )

    def load_shadow_editorial_result(
        self, story_id: str, factual_core_version: str, editorial_prompt_version: str,
    ) -> dict[str, Any] | None:
        row = self.db.execute(
            """
            SELECT * FROM shadow_editorial_results
            WHERE story_id = ? AND factual_core_version = ? AND editorial_prompt_version = ?
            """,
            (story_id, factual_core_version, editorial_prompt_version),
        ).fetchone()
        if row is None:
            return None
        return {
            "story_id": str(row["story_id"]),
            "factual_core_version": str(row["factual_core_version"]),
            "editorial_prompt_version": str(row["editorial_prompt_version"]),
            "factual_result_processed_at": str(row["factual_result_processed_at"]),
            "editorial_status": str(row["editorial_status"]),
            "headline": row["headline"], "body": row["body"],
            "model_backend": row["model_backend"], "model_name": row["model_name"],
            "validation_errors": json.loads(str(row["validation_errors_json"])),
            "generated_at": str(row["generated_at"]), "latency_ms": float(row["latency_ms"]),
        }

    def save_shadow_editorial_audit(self, payload: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO shadow_editorial_audits(
                story_id, factual_core_version, editorial_prompt_version,
                deterministic_fallback_version, trusted_fact_version, llm_attempted,
                llm_status, llm_validation_errors_json, fallback_attempted,
                fallback_reason_json, fallback_validation_errors_json, generation_source,
                editorial_status, headline, body, model_backend, model_name,
                generated_at, latency_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(story_id, factual_core_version, editorial_prompt_version, deterministic_fallback_version)
            DO UPDATE SET trusted_fact_version=excluded.trusted_fact_version,
                llm_attempted=excluded.llm_attempted, llm_status=excluded.llm_status,
                llm_validation_errors_json=excluded.llm_validation_errors_json,
                fallback_attempted=excluded.fallback_attempted,
                fallback_reason_json=excluded.fallback_reason_json,
                fallback_validation_errors_json=excluded.fallback_validation_errors_json,
                generation_source=excluded.generation_source, editorial_status=excluded.editorial_status,
                headline=excluded.headline, body=excluded.body,
                model_backend=excluded.model_backend, model_name=excluded.model_name,
                generated_at=excluded.generated_at, latency_ms=excluded.latency_ms
            """,
            (
                payload["story_id"], payload["factual_core_version"],
                payload["editorial_prompt_version"], payload["deterministic_fallback_version"],
                payload["trusted_fact_version"], int(bool(payload["llm_attempted"])),
                payload["llm_status"], json.dumps(payload["llm_validation_errors"], ensure_ascii=False),
                int(bool(payload["fallback_attempted"])),
                json.dumps(payload["fallback_reason"], ensure_ascii=False),
                json.dumps(payload["fallback_validation_errors"], ensure_ascii=False),
                payload["generation_source"], payload["editorial_status"], payload.get("headline"),
                payload.get("body"), payload.get("model_backend"), payload.get("model_name"),
                payload["generated_at"], payload["latency_ms"],
            ),
        )

    def load_shadow_editorial_audit(
        self, story_id: str, factual_core_version: str, editorial_prompt_version: str,
        deterministic_fallback_version: str,
    ) -> dict[str, Any] | None:
        row = self.db.execute(
            """
            SELECT * FROM shadow_editorial_audits
            WHERE story_id = ? AND factual_core_version = ? AND editorial_prompt_version = ?
              AND deterministic_fallback_version = ?
            """,
            (story_id, factual_core_version, editorial_prompt_version, deterministic_fallback_version),
        ).fetchone()
        if row is None:
            return None
        return {
            "story_id": str(row["story_id"]), "factual_core_version": str(row["factual_core_version"]),
            "editorial_prompt_version": str(row["editorial_prompt_version"]),
            "deterministic_fallback_version": str(row["deterministic_fallback_version"]),
            "trusted_fact_version": str(row["trusted_fact_version"]),
            "llm_attempted": bool(row["llm_attempted"]), "llm_status": str(row["llm_status"]),
            "llm_validation_errors": json.loads(str(row["llm_validation_errors_json"])),
            "fallback_attempted": bool(row["fallback_attempted"]),
            "fallback_reason": json.loads(str(row["fallback_reason_json"])),
            "fallback_validation_errors": json.loads(str(row["fallback_validation_errors_json"])),
            "generation_source": str(row["generation_source"]), "editorial_status": str(row["editorial_status"]),
            "headline": row["headline"], "body": row["body"], "model_backend": row["model_backend"],
            "model_name": row["model_name"], "generated_at": str(row["generated_at"]),
            "latency_ms": float(row["latency_ms"]),
        }
