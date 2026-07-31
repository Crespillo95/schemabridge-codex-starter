"""Human-readable M32 CLI preview coverage."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from click import unstyle
from typer.testing import CliRunner

import schemabridge.entrypoints.cli.main as cli_module
from schemabridge.adapters.query_studio.advanced_fake_language import (
    M32_REFERENCE_QUESTION_ES,
    M32_SIMPLE_PRODUCTS_QUESTION_ES,
    DeterministicAdvancedLanguageAdapter,
)
from schemabridge.application.natural_sql import (
    ConfirmedNaturalSqlRequest,
    ConfirmNaturalSqlPreview,
    GenerateGovernedCopyableSql,
    GovernedCopyableSqlResult,
    NaturalSqlPreparation,
    PrepareNaturalSqlPreview,
)
from schemabridge.bootstrap import NaturalSqlRuntimeServices, build_natural_sql_runtime
from schemabridge.config import Settings
from schemabridge.domain.advanced_query_studio import (
    AdvancedInterpretationInput,
    AdvancedInterpretationResult,
    AdvancedMentionExtractionInput,
    AdvancedMentionExtractionResult,
    AdvancedNaturalLanguageInput,
    AdvancedQueryConfirmation,
)
from schemabridge.domain.advanced_requests import OutputBooleanPredicate, OutputFilter
from schemabridge.domain.identity import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    IdentityRole,
)
from schemabridge.domain.requests import FilterOperator
from schemabridge.entrypoints.cli.main import app

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[2]
_ENVIRONMENT = {
    "COLUMNS": "240",
}


class _CountingLanguage:
    def __init__(self) -> None:
        self.delegate = DeterministicAdvancedLanguageAdapter()
        self.extraction_calls = 0
        self.interpretation_calls = 0

    def extract(
        self,
        value: AdvancedMentionExtractionInput,
    ) -> AdvancedMentionExtractionResult:
        self.extraction_calls += 1
        return self.delegate.extract(value)

    def interpret(
        self,
        value: AdvancedInterpretationInput,
    ) -> AdvancedInterpretationResult:
        self.interpretation_calls += 1
        return self.delegate.interpret(value)


@dataclass(slots=True)
class _CountingPrepare:
    delegate: PrepareNaturalSqlPreview
    calls: int = 0
    last_result: NaturalSqlPreparation | None = None

    def execute(self, value: AdvancedNaturalLanguageInput) -> NaturalSqlPreparation:
        self.calls += 1
        result = self.delegate.execute(value)
        self.last_result = result
        return result


@dataclass(slots=True)
class _CountingConfirm:
    delegate: ConfirmNaturalSqlPreview
    calls: int = 0
    last_preparation: NaturalSqlPreparation | None = None
    last_confirmation: AdvancedQueryConfirmation | None = None
    last_result: ConfirmedNaturalSqlRequest | None = None

    def execute(
        self,
        preparation: NaturalSqlPreparation,
        confirmation: AdvancedQueryConfirmation,
    ) -> ConfirmedNaturalSqlRequest:
        self.calls += 1
        self.last_preparation = preparation
        self.last_confirmation = confirmation
        result = self.delegate.execute(preparation, confirmation)
        self.last_result = result
        return result


@dataclass(slots=True)
class _CountingGenerate:
    delegate: GenerateGovernedCopyableSql
    calls: int = 0
    last_confirmed: ConfirmedNaturalSqlRequest | None = None

    def execute(
        self,
        confirmed: ConfirmedNaturalSqlRequest,
    ) -> GovernedCopyableSqlResult:
        self.calls += 1
        self.last_confirmed = confirmed
        return self.delegate.execute(confirmed)


@dataclass(frozen=True, slots=True)
class _CliHarness:
    language: _CountingLanguage
    prepare: _CountingPrepare
    confirm: _CountingConfirm
    generate: _CountingGenerate


@pytest.fixture
def deterministic_cli(monkeypatch: pytest.MonkeyPatch) -> _CliHarness:
    now = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    principal = AuthenticatedPrincipal(
        actor_id="m32-cli-analyst",
        workspace_id="m32-cli",
        roles=frozenset({IdentityRole.ANALYST}),
        authentication_method=AuthenticationMethod.LOCAL_DEMO,
        authenticated_at=now,
        expires_at=now + timedelta(hours=1),
    )
    language = _CountingLanguage()
    base_runtime = build_natural_sql_runtime(
        principal=principal,
        repository_root=ROOT,
        settings=Settings(
            _env_file=None,
            OPENAI_API_KEY=None,
            DATABASE_URL=None,
            SCHEMABRIDGE_QUERY_STUDIO_AI_MODE="fake",
            SCHEMABRIDGE_QUERY_STUDIO_SIGNING_KEY=(
                "m32-cli-test-signing-key-with-byte-diversity-2026"
            ),
        ),
        mentions=language,
        interpreter=language,
    )
    prepare = _CountingPrepare(base_runtime.prepare)
    confirm = _CountingConfirm(base_runtime.confirm)
    generate = _CountingGenerate(base_runtime.generate)
    runtime = NaturalSqlRuntimeServices(
        scope=base_runtime.scope,
        ai_mode=base_runtime.ai_mode,
        prepare=cast(PrepareNaturalSqlPreview, prepare),
        confirm=cast(ConfirmNaturalSqlPreview, confirm),
        generate=cast(GenerateGovernedCopyableSql, generate),
    )

    def use_runtime(**_kwargs: object) -> NaturalSqlRuntimeServices:
        return runtime

    monkeypatch.setattr(cli_module, "build_streamlit_principal", lambda: principal)
    monkeypatch.setattr(cli_module, "build_natural_sql_runtime", use_runtime)
    return _CliHarness(
        language=language,
        prepare=prepare,
        confirm=confirm,
        generate=generate,
    )


def _human_preview(question: str) -> str:
    result = runner.invoke(
        app,
        ["sql-from-natural", question],
        env=_ENVIRONMENT,
        terminal_width=240,
    )

    assert result.exit_code == 0, result.stdout
    return unstyle(result.stdout)


def test_advanced_preview_prints_the_complete_typed_request_and_lineage(
    deterministic_cli: _CliHarness,
) -> None:
    output = _human_preview(M32_REFERENCE_QUESTION_ES)

    assert "Interpretación gobernada preparada; todavía no existe SQL." in output
    assert "Interpretación tipada confirmable" in output
    assert "v2 / aggregate" in output
    assert "SaleLine" in output
    assert "month = SalesOrder.ordered_at · grain=month" in output
    assert "category = Product.category" in output
    assert "net_revenue = sum(SaleLine.net_amount)" in output
    assert "units = sum(SaleLine.quantity)" in output
    assert "distinct_orders = count_distinct(SalesOrder.order_key)" in output
    assert 'SalesOrder.order_status equals "COMPLETED"' in output
    assert "month, category" in output
    assert "distinct_orders greater_than_or_equal 4" in output
    assert (
        "revenue_rank = row_number · partition_by=month · order_by=net_revenue desc,category asc"
    ) in output
    assert "revenue_percent = percent_of_total · source=net_revenue · partition_by=month" in output
    assert "cumulative_revenue = running_sum" in output
    assert "Política de empates" in output
    assert "revenue_rank:row_number por net_revenue desc, category asc" in output
    assert "revenue_rank less_than_or_equal 3" in output
    assert "month asc, revenue_rank asc, category asc" in output
    assert "Contexto y resolución gobernados" in output
    assert "SalesOrder, Product, SaleLine" in output
    assert "sales.order_lines, sales.orders, commerce.products" in output
    assert "sales_order_to_sale_line, product_to_sale_line" in output
    assert "Supuesto: shortest_approved_join_path" in output
    assert "Fanout" in output
    assert "ninguno; no se aplicó mitigación automática" in output
    assert re.search(r"Preview: [0-9a-f]{64}", output)
    assert "--confirm-fingerprint <preview>" in output
    assert "\nSELECT " not in output
    assert "\nWITH " not in output
    assert deterministic_cli.prepare.calls == 1
    assert deterministic_cli.language.extraction_calls == 1
    assert deterministic_cli.language.interpretation_calls == 1


def test_simple_row_preview_keeps_empty_advanced_stages_explicit(
    deterministic_cli: _CliHarness,
) -> None:
    output = _human_preview(M32_SIMPLE_PRODUCTS_QUESTION_ES)

    assert "v2 / rows" in output
    assert "Product" in output
    assert "product_key = Product.product_key" in output
    assert "category = Product.category" in output
    assert "unit_price = Product.unit_price" in output
    assert "Métricas" in output
    assert "ninguna" in output
    assert "Product.is_active equals true" in output
    assert "Ventanas" in output
    assert "ninguna" in output
    assert "Política de empates" in output
    assert "no aplica" in output
    assert "category asc, product_key asc" in output
    assert "commerce.products" in output
    assert "Joins aprobados" in output
    assert "ninguno" in output
    assert "Límite" in output
    assert "50" in output
    assert "\nSELECT " not in output
    assert "\nWITH " not in output
    assert deterministic_cli.prepare.calls == 1
    assert deterministic_cli.language.extraction_calls == 1
    assert deterministic_cli.language.interpretation_calls == 1


def test_output_alias_comparison_is_not_misrepresented_as_a_literal() -> None:
    predicate = OutputBooleanPredicate.leaf(
        OutputFilter(
            alias="current_value",
            operator=FilterOperator.GREATER_THAN,
            compare_to_alias="previous_value",
        )
    )

    assert (
        cli_module._natural_sql_predicate(predicate)
        == "current_value greater_than alias:previous_value"
    )


def test_interactive_review_confirms_the_same_single_preparation(
    deterministic_cli: _CliHarness,
) -> None:
    result = runner.invoke(
        app,
        [
            "sql-from-natural",
            M32_SIMPLE_PRODUCTS_QUESTION_ES,
            "--review-and-confirm",
        ],
        input="y\n",
        env=_ENVIRONMENT,
        terminal_width=240,
    )

    assert result.exit_code == 0, result.stdout
    output = unstyle(result.output)
    assert deterministic_cli.prepare.calls == 1
    assert deterministic_cli.language.extraction_calls == 1
    assert deterministic_cli.language.interpretation_calls == 1
    assert deterministic_cli.confirm.calls == 1
    assert deterministic_cli.generate.calls == 1
    assert deterministic_cli.confirm.last_preparation is deterministic_cli.prepare.last_result
    assert deterministic_cli.confirm.last_confirmation is not None
    assert deterministic_cli.prepare.last_result is not None
    assert (
        deterministic_cli.confirm.last_confirmation.token
        is deterministic_cli.prepare.last_result.token
    )
    assert deterministic_cli.generate.last_confirmed is deterministic_cli.confirm.last_result
    assert "Interpretación tipada confirmable" in output
    assert "¿Confirmas exactamente este preview firmado" in output
    assert output.index("Interpretación tipada confirmable") < output.index(
        "¿Confirmas exactamente este preview firmado"
    )
    assert re.search(r"\nSELECT\b", output)
    assert "%s" not in output
    assert "SHA-256:" in output
    assert "executed=false" in output
    assert "repite el comando con --confirm-fingerprint" not in output


def test_interactive_review_rejection_never_confirms_or_generates(
    deterministic_cli: _CliHarness,
) -> None:
    result = runner.invoke(
        app,
        [
            "sql-from-natural",
            M32_SIMPLE_PRODUCTS_QUESTION_ES,
            "--review-and-confirm",
        ],
        input="n\n",
        env=_ENVIRONMENT,
        terminal_width=240,
    )

    assert result.exit_code == 0, result.stdout
    output = unstyle(result.stdout)
    assert deterministic_cli.prepare.calls == 1
    assert deterministic_cli.language.extraction_calls == 1
    assert deterministic_cli.language.interpretation_calls == 1
    assert deterministic_cli.confirm.calls == 0
    assert deterministic_cli.generate.calls == 0
    assert "Confirmación cancelada; no se compiló, generó ni ejecutó SQL." in output
    assert not re.search(r"\n(?:SELECT|WITH)\b", output)
    assert "SHA-256:" not in output
    assert "repite el comando con --confirm-fingerprint" not in output


def test_two_step_fingerprint_mode_reprepares_and_accepts_only_the_same_preview(
    deterministic_cli: _CliHarness,
) -> None:
    preview_result = runner.invoke(
        app,
        ["sql-from-natural", M32_SIMPLE_PRODUCTS_QUESTION_ES],
        env=_ENVIRONMENT,
        terminal_width=240,
    )
    assert preview_result.exit_code == 0, preview_result.stdout
    match = re.search(r"Preview: ([0-9a-f]{64})", unstyle(preview_result.stdout))
    assert match is not None

    generated_result = runner.invoke(
        app,
        [
            "sql-from-natural",
            M32_SIMPLE_PRODUCTS_QUESTION_ES,
            "--confirm-fingerprint",
            match.group(1),
        ],
        env=_ENVIRONMENT,
        terminal_width=240,
    )

    assert generated_result.exit_code == 0, generated_result.stdout
    assert deterministic_cli.prepare.calls == 2
    assert deterministic_cli.language.extraction_calls == 2
    assert deterministic_cli.language.interpretation_calls == 2
    assert deterministic_cli.confirm.calls == 1
    assert deterministic_cli.generate.calls == 1
    assert "SHA-256:" in unstyle(generated_result.stdout)
    assert "executed=false" in unstyle(generated_result.stdout)


@pytest.mark.parametrize(
    "conflicting_option",
    ("--json", "--confirm-fingerprint"),
)
def test_interactive_review_rejects_machine_or_two_step_confirmation_modes(
    deterministic_cli: _CliHarness,
    conflicting_option: str,
) -> None:
    arguments = [
        "sql-from-natural",
        M32_SIMPLE_PRODUCTS_QUESTION_ES,
        "--review-and-confirm",
        conflicting_option,
    ]
    if conflicting_option == "--confirm-fingerprint":
        arguments.append("a" * 64)

    result = runner.invoke(
        app,
        arguments,
        env=_ENVIRONMENT,
        terminal_width=240,
    )

    assert result.exit_code == 2
    output = unstyle(result.output)
    assert "cannot be combined" in output
    assert deterministic_cli.prepare.calls == 0
    assert deterministic_cli.language.extraction_calls == 0
    assert deterministic_cli.language.interpretation_calls == 0
    assert deterministic_cli.confirm.calls == 0
    assert deterministic_cli.generate.calls == 0


def test_fingerprint_help_warns_that_the_request_is_prepared_again() -> None:
    result = runner.invoke(
        app,
        ["sql-from-natural", "--help"],
        env=_ENVIRONMENT,
        terminal_width=240,
    )

    assert result.exit_code == 0
    output = " ".join(unstyle(result.stdout).split())
    assert "--confirm-fingerprint" in output
    assert "This mode prepares again" in output
    assert "--review-and-confirm" in output
    assert "Prepare once" in output
