"""Validated loaders for the small versioned synthetic evaluation fixture set."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from schemabridge.application.ports.evaluation import EvaluationError, EvaluationErrorCode
from schemabridge.domain.evaluation import (
    EvaluationGroundTruth,
    EvaluationGuidedCase,
    EvaluationJoinGroundTruth,
    EvaluationQueryGroundTruth,
    EvaluationRecipeReuseGroundTruth,
    EvaluationSafetyGroundTruth,
    SourceFixtureVersion,
    fingerprint_fixture,
    normalize_mapping_rows,
)
from schemabridge.domain.intents import IntentAlternativeId
from schemabridge.domain.joins import Cardinality
from schemabridge.domain.recipes import PublishedQueryRecipe, QueryRecipe
from schemabridge.domain.requests import AnalyticalRequest


class RecordedEvaluationGroundTruthAdapter:
    """Load only fixed repository fixtures; manifest paths are never executable input."""

    def __init__(self, repository_root: Path) -> None:
        self._root = repository_root.resolve()
        self._paths = {
            "evaluation": self._root / "demo/ground_truth/evaluation.yml",
            "semantic_mappings": self._root / "demo/ground_truth/semantic_mappings.yml",
            "query_cases": self._root / "demo/ground_truth/query_cases.yml",
            "join_contracts": self._root / "demo/ground_truth/join_contracts.yml",
            "approved_logical_context": (
                self._root / "demo/ground_truth/approved_logical_context.yml"
            ),
            "planning_mappings": self._root / "demo/ground_truth/planning_mappings.yml",
            "sql_guard_cases": self._root / "tests/fixtures/security/sql_guard_cases.yml",
            "query_recipe": self._root / "examples/query-recipe-secondary-holders.yml",
        }

    def load(self) -> EvaluationGroundTruth:
        try:
            payloads = {name: _load_yaml(path) for name, path in self._paths.items()}
            manifest = payloads["evaluation"]
            expected_versions = _string_int_dict(manifest.get("source_versions"))
            observed_versions = {
                name: _integer(payloads[name].get("version")) for name in expected_versions
            }
            if observed_versions != expected_versions:
                raise ValueError("evaluation source versions do not match the manifest")

            query_config = {
                _string(item.get("id")): item for item in _object_list(manifest.get("query_cases"))
            }
            queries = tuple(
                self._query_case(item, query_config)
                for item in _object_list(payloads["query_cases"].get("cases"))
            )
            if set(query_config) != {item.id for item in queries}:
                raise ValueError("evaluation query manifest does not match query ground truth")

            joins = tuple(
                EvaluationJoinGroundTruth(
                    id=_string(item.get("id")),
                    cardinality=Cardinality(_string(item.get("cardinality"))),
                )
                for item in _object_list(payloads["join_contracts"].get("contracts"))
            )
            safety = tuple(
                EvaluationSafetyGroundTruth(
                    id=_string(item.get("id")),
                    sql=_string(item.get("sql")),
                    expected_code=_string(item.get("expected_code")),
                    effective_limit=_integer(item.get("effective_limit", 1)),
                )
                for item in _object_list(payloads["sql_guard_cases"].get("cases"))
            )
            recipe_cases = tuple(
                EvaluationRecipeReuseGroundTruth.model_validate(item)
                for item in _object_list(manifest.get("recipe_reuse_cases"))
            )
            recipe = QueryRecipe.model_validate(payloads["query_recipe"])
            parts = tuple(
                (str(path.relative_to(self._root)), path.read_bytes())
                for path in self._paths.values()
            )
            return EvaluationGroundTruth(
                version=_integer(manifest.get("version")),
                fixture_notice=_string(manifest.get("fixture_notice")),
                sources=tuple(
                    SourceFixtureVersion(name=name, version=version)
                    for name, version in sorted(expected_versions.items())
                ),
                queries=queries,
                joins=joins,
                safety_cases=safety,
                recipe_reuse_cases=recipe_cases,
                recipe=recipe,
                fixture_fingerprint=fingerprint_fixture(parts),
            )
        except OSError as error:
            raise EvaluationError(
                EvaluationErrorCode.GROUND_TRUTH_UNAVAILABLE,
                "versioned evaluation ground truth is unavailable",
            ) from error
        except (KeyError, TypeError, ValueError, ValidationError, yaml.YAMLError) as error:
            raise EvaluationError(
                EvaluationErrorCode.GROUND_TRUTH_INVALID,
                "versioned evaluation ground truth is invalid",
            ) from error

    @staticmethod
    def _query_case(
        raw: dict[str, Any],
        config_by_id: dict[str, dict[str, Any]],
    ) -> EvaluationQueryGroundTruth:
        case_id = _string(raw.get("id"))
        config = config_by_id[case_id]
        expected_rows = tuple(
            {str(key): value for key, value in row.items()}
            for row in _object_list(raw.get("expected_rows"))
        )
        rejections = tuple(
            _string(item.get("reason")) for item in _object_list(raw.get("expected_rejections", []))
        )
        alternative = config.get("intent_alternative")
        return EvaluationQueryGroundTruth(
            id=case_id,
            guided_case=EvaluationGuidedCase(_string(config.get("guided_case"))),
            question=_string(raw.get("question_es")),
            expected_request=AnalyticalRequest.model_validate(raw.get("interpretation")),
            expected_join_contracts=tuple(
                _string(item) for item in _plain_list(raw.get("join_contracts", []))
            ),
            expected_rows=normalize_mapping_rows(expected_rows),
            expected_rejection_codes=rejections,
            intent_alternative=(
                IntentAlternativeId(_string(alternative)) if alternative is not None else None
            ),
            intent_skip_reason=(
                _string(config.get("intent_skip_reason")) if alternative is None else None
            ),
        )


class StaticEvaluationRecipeAdapter:
    """Expose one generated SQL-free recipe as immutable evaluation provenance."""

    def __init__(self, recipe: QueryRecipe) -> None:
        self._published = PublishedQueryRecipe(
            recipe=recipe,
            document_urn=f"urn:li:document:schemabridge-evaluation-{recipe.id}",
            versioned_document_urn=(
                f"urn:li:document:schemabridge-evaluation-{recipe.id}-v{recipe.version}"
            ),
            approval_id="synthetic-evaluation-recipe-approval",
            published_at=recipe.created_at,
        )

    def find_current(self, intent_fingerprint: str) -> PublishedQueryRecipe | None:
        if self._published.recipe.intent_fingerprint != intent_fingerprint:
            return None
        return self._published


def _load_yaml(path: Path) -> dict[str, Any]:
    payload: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("evaluation YAML root must be an object")
    return {str(key): value for key, value in payload.items()}


def _object_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("evaluation value must be a list of objects")
    return [{str(key): item for key, item in raw.items()} for raw in value]


def _plain_list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("evaluation value must be a list")
    return value


def _string_int_dict(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ValueError("evaluation versions must be an object")
    return {str(key): _integer(item) for key, item in value.items()}


def _string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("evaluation value must be nonblank text")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("evaluation value must be an integer")
    return value
