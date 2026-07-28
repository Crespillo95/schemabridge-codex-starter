"""Natural-language interpretation with bounded vocabulary and explicit confirmation."""

from __future__ import annotations

import re
from dataclasses import dataclass

from schemabridge.application.ports.intents import IntentParserPort
from schemabridge.application.ports.planning import GovernedSemanticRegistryPort
from schemabridge.application.ports.requests import ApprovedRequestContextPort
from schemabridge.domain.concepts import LogicalFieldRef, LogicalModelRef
from schemabridge.domain.intents import (
    IntentAlternativeId,
    IntentAmbiguityKind,
    IntentConfirmation,
    IntentModelOutput,
    IntentParseInput,
    IntentVocabulary,
    IntentVocabularyField,
    IntentVocabularyJoin,
    IntentVocabularyModel,
    UserLanguage,
    intent_interpretation_fingerprint,
    intent_vocabulary_fingerprint,
)
from schemabridge.domain.request_context import (
    ApprovedLogicalContext,
    ValidatedAnalyticalRequest,
    validate_analytical_request,
)
from schemabridge.domain.requests import (
    AnalyticalRequest,
    DateGrain,
    Filter,
    FilterOperator,
    Metric,
    MetricOperation,
)
from schemabridge.domain.validation import (
    ValidationFinding,
    ValidationSeverity,
)

_M11_MODELS = ("Customer", "AccountHolder")
_M11_FIELDS = (
    "Customer.customer_key",
    "Customer.registration_date",
    "AccountHolder.customer_key",
    "AccountHolder.holder_role",
)
_ROLE_VALUES = ("PRIMARY", "SECONDARY")
_CONTROL_TEXT = re.compile(
    r"(?i)(ignore\s+(?:all\s+)?(?:previous\s+)?(?:rules|instructions)|"
    r"ignora\s+(?:todas\s+)?(?:las\s+)?(?:reglas|instrucciones)|"
    r"drop\s+table|elimina\s+la\s+tabla|/\*|--|;)"
)
_SQL_PAYLOAD = re.compile(
    r"(?i)(\b(?:select|insert|update|delete|drop|alter|create|truncate)\b|/\*|--|;)"
)


@dataclass(frozen=True, slots=True)
class IntentAlternative:
    id: IntentAlternativeId
    label: str
    rationale: str
    resolves: tuple[IntentAmbiguityKind, ...]
    request: AnalyticalRequest | None

    @property
    def available(self) -> bool:
        return self.request is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id.value,
            "label": self.label,
            "rationale": self.rationale,
            "resolves": [item.value for item in self.resolves],
            "available": self.available,
            "request": self.request.model_dump(mode="json") if self.request is not None else None,
        }


@dataclass(frozen=True, slots=True)
class IntentPreview:
    language: UserLanguage
    adapter: str
    vocabulary: IntentVocabulary
    interpretation_fingerprint: str
    proposed_request: AnalyticalRequest | None
    ambiguities: tuple[IntentAmbiguityKind, ...]
    alternatives: tuple[IntentAlternative, ...]
    findings: tuple[ValidationFinding, ...]

    @property
    def requires_confirmation(self) -> bool:
        return self.proposed_request is not None or bool(self.ambiguities)

    @property
    def can_confirm(self) -> bool:
        unresolved = set(self.ambiguities)
        return not any(
            finding.severity is ValidationSeverity.ERROR for finding in self.findings
        ) and any(
            alternative.available and unresolved.issubset(alternative.resolves)
            for alternative in self.alternatives
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "language": self.language.value,
            "adapter": self.adapter,
            "vocabulary": {
                "source": self.vocabulary.context_source,
                "version": self.vocabulary.context_version,
                "fingerprint": intent_vocabulary_fingerprint(self.vocabulary),
                "models": [item.id.root for item in self.vocabulary.models],
                "fields": [item.id.root for item in self.vocabulary.fields],
                "allowed_role_values": list(_ROLE_VALUES),
            },
            "interpretation_fingerprint": self.interpretation_fingerprint,
            "proposed_request": (
                self.proposed_request.model_dump(mode="json")
                if self.proposed_request is not None
                else None
            ),
            "ambiguities": [item.value for item in self.ambiguities],
            "alternatives": [item.as_dict() for item in self.alternatives],
            "findings": [item.model_dump(mode="json") for item in self.findings],
            "requires_confirmation": self.requires_confirmation,
            "can_confirm": self.can_confirm,
        }


class IntentConfirmationError(RuntimeError):
    """Confirmation failed closed before semantic planning."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ResolveNaturalLanguageIntent:
    parser: IntentParserPort
    context: GovernedSemanticRegistryPort | ApprovedRequestContextPort
    adapter_label: str

    def preview(self, text: str, language: UserLanguage) -> IntentPreview:
        approved = _load_approved_context(self.context)
        vocabulary = build_intent_vocabulary(approved)
        parse_input = IntentParseInput(text=text, language=language, vocabulary=vocabulary)
        output = self.parser.parse(parse_input)
        findings, validated = _validate_model_output(output, parse_input, approved)
        ambiguities = _detect_ambiguities(parse_input, output, validated)
        fingerprint = intent_interpretation_fingerprint(
            IntentModelOutput(
                language=output.language,
                request=output.request,
                ambiguities=ambiguities,
            ),
            vocabulary,
        )

        if _CONTROL_TEXT.search(text):
            findings.append(
                _finding(
                    "untrusted_instruction_in_business_text",
                    ValidationSeverity.ERROR,
                    "Instruction-like text is treated only as business input and cannot alter "
                    "tools, approvals, SQL policy, or execution.",
                    "text",
                )
            )
            validated = None

        alternatives = _build_alternatives(
            validated,
            output.request,
            ambiguities,
            approved,
            language,
        )
        return IntentPreview(
            language=language,
            adapter=self.adapter_label,
            vocabulary=vocabulary,
            interpretation_fingerprint=fingerprint,
            proposed_request=validated.request if validated is not None else None,
            ambiguities=ambiguities,
            alternatives=alternatives,
            findings=tuple(findings),
        )

    def confirm(
        self,
        preview: IntentPreview,
        confirmation: IntentConfirmation,
    ) -> ValidatedAnalyticalRequest:
        if confirmation.interpretation_fingerprint != preview.interpretation_fingerprint:
            raise IntentConfirmationError(
                "intent_interpretation_changed",
                "The interpretation changed; inspect a new preview before confirming.",
            )
        current_context = _load_approved_context(self.context)
        if intent_vocabulary_fingerprint(
            build_intent_vocabulary(current_context)
        ) != intent_vocabulary_fingerprint(preview.vocabulary):
            raise IntentConfirmationError(
                "intent_context_changed",
                "The approved vocabulary changed; inspect a new preview before confirming.",
            )
        if any(finding.severity is ValidationSeverity.ERROR for finding in preview.findings):
            raise IntentConfirmationError(
                "intent_validation_failed",
                "Invalid model output cannot be confirmed or sent to semantic planning.",
            )
        alternative = next(
            (item for item in preview.alternatives if item.id is confirmation.selected_alternative),
            None,
        )
        if alternative is None or alternative.request is None:
            raise IntentConfirmationError(
                "intent_alternative_unavailable",
                "Choose an available alternative from the exact interpretation preview.",
            )
        if not set(preview.ambiguities).issubset(alternative.resolves):
            raise IntentConfirmationError(
                "intent_ambiguity_unresolved",
                "The selected alternative does not resolve every reported ambiguity.",
            )
        validation = validate_analytical_request(alternative.request, current_context)
        if validation.validated_request is None:
            raise IntentConfirmationError(
                "intent_context_validation_failed",
                "The confirmed request is not valid against the current approved context.",
            )
        return validation.validated_request


def _load_approved_context(
    context: GovernedSemanticRegistryPort | ApprovedRequestContextPort,
) -> ApprovedLogicalContext:
    loaded = context.load()
    if isinstance(loaded, ApprovedLogicalContext):
        return loaded
    return loaded.registry.logical_context


def build_intent_vocabulary(context: ApprovedLogicalContext) -> IntentVocabulary:
    """Expose only the exact approved logical subset needed by the M11 business request."""

    models = context.model_index()
    fields = context.field_index()
    selected_models = tuple(
        IntentVocabularyModel(id=models[item].id, definition=models[item].description)
        for item in _M11_MODELS
    )
    selected_fields = tuple(
        IntentVocabularyField(
            id=fields[item].id,
            canonical_type=fields[item].canonical_type,
            role=fields[item].role,
            definition=fields[item].definition,
            allowed_values=_ROLE_VALUES if item == "AccountHolder.holder_role" else (),
        )
        for item in _M11_FIELDS
    )
    selected_joins = tuple(
        IntentVocabularyJoin(
            id=join.id,
            left_model=join.left_model,
            right_model=join.right_model,
            cardinality=join.cardinality,
            fanout_policy=join.fanout_policy,
        )
        for join in context.joins
        if join.left_model.root in _M11_MODELS and join.right_model.root in _M11_MODELS
    )
    return IntentVocabulary(
        context_source=context.source,
        context_version=context.version,
        models=selected_models,
        fields=selected_fields,
        joins=selected_joins,
        metric_operations=(MetricOperation.COUNT, MetricOperation.COUNT_DISTINCT),
        filter_operators=(FilterOperator.EQUALS,),
        date_grains=(DateGrain.DAY,),
    )


def _validate_model_output(
    output: IntentModelOutput,
    parse_input: IntentParseInput,
    context: ApprovedLogicalContext,
) -> tuple[list[ValidationFinding], ValidatedAnalyticalRequest | None]:
    findings: list[ValidationFinding] = []
    bounded_value_invalid = False
    if output.language is not parse_input.language:
        findings.append(
            _finding(
                "intent_language_mismatch",
                ValidationSeverity.ERROR,
                "Model output language does not match the current user language.",
                "language",
            )
        )
    request = output.request
    if request is None:
        return findings, None

    known_models = {item.id.root for item in parse_input.vocabulary.models}
    known_fields = parse_input.vocabulary.field_index()
    if request.primary_entity.root not in known_models:
        findings.append(
            _finding(
                "hallucinated_logical_model",
                ValidationSeverity.ERROR,
                "The model returned a logical model outside the supplied approved vocabulary.",
                "request",
                "primary_entity",
            )
        )
    for path, field in _request_fields(request):
        if field.root not in known_fields:
            findings.append(
                _finding(
                    "hallucinated_logical_field",
                    ValidationSeverity.ERROR,
                    "The model returned a logical field outside the supplied approved vocabulary.",
                    "request",
                    *path,
                )
            )

    for index, dimension in enumerate(request.dimensions):
        if (
            dimension.grain is not None
            and dimension.grain not in parse_input.vocabulary.date_grains
        ):
            findings.append(
                _finding(
                    "unsupported_intent_date_grain",
                    ValidationSeverity.ERROR,
                    "The model returned a date grain outside the supplied vocabulary.",
                    "request",
                    "dimensions",
                    str(index),
                    "grain",
                )
            )
    for index, metric in enumerate(request.metrics):
        if metric.operation not in parse_input.vocabulary.metric_operations:
            findings.append(
                _finding(
                    "unsupported_intent_metric_operation",
                    ValidationSeverity.ERROR,
                    "The model returned a metric operation outside the supplied vocabulary.",
                    "request",
                    "metrics",
                    str(index),
                    "operation",
                )
            )

    for index, request_filter in enumerate(request.filters):
        if request_filter.operator not in parse_input.vocabulary.filter_operators:
            findings.append(
                _finding(
                    "unsupported_intent_filter_operator",
                    ValidationSeverity.ERROR,
                    "The model returned a filter operator outside the supplied vocabulary.",
                    "request",
                    "filters",
                    str(index),
                    "operator",
                )
            )
        vocabulary_field = known_fields.get(request_filter.field.root)
        values = (
            request_filter.value
            if isinstance(request_filter.value, tuple)
            else (request_filter.value,)
        )
        if any(isinstance(value, str) and _SQL_PAYLOAD.search(value) for value in values):
            findings.append(
                _finding(
                    "raw_sql_payload_rejected",
                    ValidationSeverity.ERROR,
                    "Model output contains SQL-like text where a bounded filter value is required.",
                    "request",
                    "filters",
                    str(index),
                    "value",
                )
            )
        if (
            vocabulary_field is not None
            and vocabulary_field.allowed_values
            and any(value not in vocabulary_field.allowed_values for value in values)
        ):
            bounded_value_invalid = True
            findings.append(
                _finding(
                    "unknown_role_value",
                    ValidationSeverity.WARNING,
                    "The requested role is not approved; choose an explicit governed role.",
                    "request",
                    "filters",
                    str(index),
                    "value",
                )
            )

    if bounded_value_invalid or any(
        finding.severity is ValidationSeverity.ERROR for finding in findings
    ):
        return findings, None
    validation = validate_analytical_request(request, context)
    findings.extend(validation.result.findings)
    return findings, validation.validated_request


def _detect_ambiguities(
    parse_input: IntentParseInput,
    output: IntentModelOutput,
    validated: ValidatedAnalyticalRequest | None,
) -> tuple[IntentAmbiguityKind, ...]:
    detected = list(output.ambiguities)
    normalized = " ".join(parse_input.text.casefold().split())
    words = set(re.findall(r"[a-záéíóúñ]+", normalized))
    request = validated.request if validated is not None else output.request

    if normalized in {"agrupa clientes", "group customers"}:
        detected.append(IntentAmbiguityKind.COUNT_OR_LIST)
    if (
        ("todos los clientes" in normalized or "all customers" in normalized)
        and request is not None
        and any(field.field.root.startswith("AccountHolder.") for field in request.filters)
    ):
        detected.append(IntentAmbiguityKind.DISTINCT_OR_RELATIONSHIP_COUNT)
    if ("fecha" in words and "registro" not in words) or (
        "date" in words and "registration" not in words
    ):
        detected.append(IntentAmbiguityKind.DATE_MEANING)
    if request is not None and any(
        request_filter.field.root == "AccountHolder.holder_role"
        and request_filter.value not in _ROLE_VALUES
        for request_filter in request.filters
    ):
        detected.append(IntentAmbiguityKind.UNKNOWN_ROLE_VALUE)
    if _CONTROL_TEXT.search(parse_input.text):
        detected.append(IntentAmbiguityKind.UNRESOLVED_BUSINESS_REQUEST)
    return tuple(dict.fromkeys(detected))


def _build_alternatives(
    validated: ValidatedAnalyticalRequest | None,
    candidate: AnalyticalRequest | None,
    ambiguities: tuple[IntentAmbiguityKind, ...],
    context: ApprovedLogicalContext,
    language: UserLanguage,
) -> tuple[IntentAlternative, ...]:
    alternatives: list[IntentAlternative] = []
    request = validated.request if validated is not None else None
    if request is not None and not ambiguities:
        alternatives.append(
            IntentAlternative(
                id=IntentAlternativeId.CONFIRM_INTERPRETATION,
                label=_localized(language, "Confirmar interpretación", "Confirm interpretation"),
                rationale=_localized(
                    language,
                    "Confirma la solicitud lógica validada exactamente como se muestra.",
                    "Confirms the validated logical request exactly as shown.",
                ),
                resolves=(),
                request=request,
            )
        )
    for ambiguity in ambiguities:
        if ambiguity is IntentAmbiguityKind.DISTINCT_OR_RELATIONSHIP_COUNT and request is not None:
            alternatives.extend(
                (
                    IntentAlternative(
                        id=IntentAlternativeId.COUNT_DISTINCT_CUSTOMERS,
                        label=_localized(
                            language, "Contar clientes distintos", "Count distinct customers"
                        ),
                        rationale=_localized(
                            language,
                            "Cuenta una vez cada Customer aprobado entre titulares uno-a-muchos.",
                            "Counts each approved Customer once across one-to-many holders.",
                        ),
                        resolves=(ambiguity,),
                        request=request,
                    ),
                    IntentAlternative(
                        id=IntentAlternativeId.COUNT_HOLDER_RELATIONSHIPS,
                        label=_localized(
                            language,
                            "Contar relaciones de titularidad",
                            "Count holder relationships",
                        ),
                        rationale=_localized(
                            language,
                            "Cuenta filas de AccountHolder, incluidos enlaces duplicados.",
                            "Counts AccountHolder rows, including duplicate customer links.",
                        ),
                        resolves=(ambiguity,),
                        request=_relationship_count_request(request),
                    ),
                )
            )
        elif ambiguity is IntentAmbiguityKind.COUNT_OR_LIST:
            count_request = request or _simple_customer_count_request()
            alternatives.extend(
                (
                    IntentAlternative(
                        id=IntentAlternativeId.COUNT_DISTINCT_CUSTOMERS,
                        label=_localized(
                            language, "Contar clientes distintos", "Count distinct customers"
                        ),
                        rationale=_localized(
                            language,
                            "Produce un único recuento agregado y acotado.",
                            "Produces one bounded aggregate count.",
                        ),
                        resolves=(ambiguity,),
                        request=count_request,
                    ),
                    IntentAlternative(
                        id=IntentAlternativeId.LIST_CUSTOMERS,
                        label=_localized(language, "Listar clientes", "List customers"),
                        rationale=_localized(
                            language,
                            "El listado está fuera del modelo actual, limitado a agregados.",
                            "Row listing is outside the current aggregate-only request model.",
                        ),
                        resolves=(ambiguity,),
                        request=None,
                    ),
                )
            )
        elif ambiguity is IntentAmbiguityKind.DATE_MEANING:
            alternatives.extend(
                (
                    IntentAlternative(
                        id=IntentAlternativeId.USE_REGISTRATION_DATE,
                        label=_localized(
                            language, "Usar fecha de registro", "Use registration date"
                        ),
                        rationale=_localized(
                            language,
                            "Usa la definición aprobada Customer.registration_date.",
                            "Uses the approved Customer.registration_date definition.",
                        ),
                        resolves=(ambiguity,),
                        request=request,
                    ),
                    IntentAlternative(
                        id=IntentAlternativeId.USE_RELATIONSHIP_DATE,
                        label=_localized(
                            language, "Usar fecha de relación", "Use relationship date"
                        ),
                        rationale=_localized(
                            language,
                            "No hay una fecha de relación aprobada en este vocabulario.",
                            "No approved relationship-date field is available in this vocabulary.",
                        ),
                        resolves=(ambiguity,),
                        request=None,
                    ),
                )
            )
        elif ambiguity is IntentAmbiguityKind.UNKNOWN_ROLE_VALUE:
            alternatives.extend(
                (
                    IntentAlternative(
                        id=IntentAlternativeId.USE_PRIMARY_ROLE,
                        label=_localized(language, "Usar rol PRIMARY", "Use PRIMARY role"),
                        rationale=_localized(
                            language,
                            "Sustituye el valor desconocido por el rol gobernado PRIMARY.",
                            "Replaces the unknown value with the governed PRIMARY role.",
                        ),
                        resolves=(ambiguity,),
                        request=_role_request(candidate, "PRIMARY"),
                    ),
                    IntentAlternative(
                        id=IntentAlternativeId.USE_SECONDARY_ROLE,
                        label=_localized(language, "Usar rol SECONDARY", "Use SECONDARY role"),
                        rationale=_localized(
                            language,
                            "Sustituye el valor desconocido por el rol gobernado SECONDARY.",
                            "Replaces the unknown value with the governed SECONDARY role.",
                        ),
                        resolves=(ambiguity,),
                        request=_role_request(candidate, "SECONDARY"),
                    ),
                )
            )

    deduplicated: dict[IntentAlternativeId, IntentAlternative] = {}
    for alternative in alternatives:
        existing = deduplicated.get(alternative.id)
        if existing is None:
            deduplicated[alternative.id] = alternative
        else:
            deduplicated[alternative.id] = IntentAlternative(
                id=existing.id,
                label=existing.label,
                rationale=existing.rationale,
                resolves=tuple(dict.fromkeys((*existing.resolves, *alternative.resolves))),
                request=existing.request,
            )
    return tuple(
        alternative
        for alternative in deduplicated.values()
        if alternative.request is None
        or validate_analytical_request(alternative.request, context).validated_request is not None
    )


def _localized(language: UserLanguage, spanish: str, english: str) -> str:
    return spanish if language is UserLanguage.SPANISH else english


def _relationship_count_request(request: AnalyticalRequest) -> AnalyticalRequest:
    return AnalyticalRequest(
        primary_entity=request.primary_entity,
        dimensions=request.dimensions,
        metrics=(
            Metric(
                operation=MetricOperation.COUNT,
                field=LogicalFieldRef("AccountHolder.customer_key"),
                alias="secondary_holder_relationships",
            ),
        ),
        filters=request.filters,
        order_by=request.order_by,
        limit=request.limit,
    )


def _simple_customer_count_request() -> AnalyticalRequest:
    return AnalyticalRequest(
        primary_entity=LogicalModelRef("Customer"),
        metrics=(
            Metric(
                operation=MetricOperation.COUNT_DISTINCT,
                field=LogicalFieldRef("Customer.customer_key"),
                alias="customers",
            ),
        ),
        limit=100,
    )


def _role_request(request: AnalyticalRequest | None, role: str) -> AnalyticalRequest | None:
    if request is None or not any(
        item.field.root == "AccountHolder.holder_role" for item in request.filters
    ):
        return None
    return AnalyticalRequest(
        primary_entity=request.primary_entity,
        dimensions=request.dimensions,
        metrics=request.metrics,
        filters=tuple(
            Filter(field=item.field, operator=item.operator, value=role)
            if item.field.root == "AccountHolder.holder_role"
            else item
            for item in request.filters
        ),
        order_by=request.order_by,
        limit=request.limit,
    )


def _request_fields(
    request: AnalyticalRequest,
) -> tuple[tuple[tuple[str, ...], LogicalFieldRef], ...]:
    fields: list[tuple[tuple[str, ...], LogicalFieldRef]] = []
    fields.extend(
        (("dimensions", str(index), "field"), item.field)
        for index, item in enumerate(request.dimensions)
    )
    fields.extend(
        (("metrics", str(index), "field"), item.field) for index, item in enumerate(request.metrics)
    )
    fields.extend(
        (("filters", str(index), "field"), item.field) for index, item in enumerate(request.filters)
    )
    fields.extend(
        (("order_by", str(index), "field"), item.field)
        for index, item in enumerate(request.order_by)
    )
    return tuple(fields)


def _finding(
    code: str,
    severity: ValidationSeverity,
    message: str,
    *path: str,
) -> ValidationFinding:
    return ValidationFinding(code=code, severity=severity, message=message, path=path)
