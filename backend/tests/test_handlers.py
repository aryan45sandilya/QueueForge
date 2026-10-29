"""Handler validation and behaviour."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from queueforge.db import Database
from queueforge.errors import ValidationFailed
from queueforge.handlers import (
    JobContext,
    PermanentError,
    RetryableError,
    all_handlers,
    get_handler,
    validate_payload,
)
from queueforge.handlers.notification import NotificationPayload, send_notifications
from queueforge.handlers.synthetic import SyntheticPayload, run_synthetic
from queueforge.handlers.transform import TransformPayload, transform
from queueforge.logs import get_logger

CSV = "region,product,units,price\nnorth,widget,12,2.50\nsouth,widget,7,2.50\nnorth,gadget,3,9.99\nsouth,gadget,0,9.99\n"


def ctx(
    attempt: int = 1, db: Database | None = None, project_id: uuid.UUID | None = None
) -> JobContext:
    return JobContext(
        job_id=uuid.UUID(int=1),
        project_id=project_id or uuid.UUID(int=2),
        attempt=attempt,
        max_attempts=5,
        db=db,
        logger=get_logger("test"),
    )


def test_registry_examples_validate_and_schemas_are_json() -> None:
    names = {h.name for h in all_handlers()}
    assert {"notification.send", "data.transform", "report.generate", "bench.synthetic"} <= names
    for spec in all_handlers():
        spec.payload_model.model_validate(spec.example)
        json.dumps(spec.describe())


def test_unknown_type_and_invalid_payload_raise_validation_failed() -> None:
    with pytest.raises(ValidationFailed) as unknown:
        validate_payload("shell.exec", {"cmd": "rm -rf /"})
    assert "Unknown job type" in unknown.value.message
    with pytest.raises(ValidationFailed) as invalid:
        validate_payload("bench.synthetic", {"work_ms": -1, "extra": True})
    locs = {tuple(d["loc"]) for d in invalid.value.details}
    assert ("payload", "work_ms") in locs and ("payload", "extra") in locs


# --- notification.send --------------------------------------------------------------------------


def notification(**over: Any) -> NotificationPayload:
    base: dict[str, Any] = {
        "channel": "email",
        "template": "welcome",
        "subject": "Hi {{name}}",
        "body": "Hello {{name}} from {{team}}",
        "recipients": [
            {"address": "a@example.com", "name": "A", "variables": {"team": "ops"}},
            {"address": "A@example.com", "name": "A dup", "variables": {"team": "ops"}},
            {"address": "x@bounce.test", "variables": {"team": "ops"}},
            {"address": "b@example.com", "name": "B"},
        ],
    }
    base.update(over)
    return NotificationPayload.model_validate(base)


async def test_notification_dedupes_bounces_and_reports_missing_variables() -> None:
    result = await send_notifications(notification(), ctx())
    assert result["requested"] == 4
    assert result["unique_recipients"] == 3
    assert result["duplicates_skipped"] == 1
    assert result["delivered"] == 1
    assert result["bounced_count"] == 1 and result["bounced"][0]["address"] == "x@bounce.test"
    assert result["unrendered"] == [{"address": "b@example.com", "missing_variables": ["team"]}]


async def test_notification_message_ids_are_stable_across_attempts() -> None:
    first = await send_notifications(notification(), ctx(attempt=1))
    second = await send_notifications(notification(), ctx(attempt=2))
    assert first["sample_message_ids"] == second["sample_message_ids"]


async def test_notification_flaky_domain_fails_first_attempt_only() -> None:
    payload = notification(recipients=[{"address": "x@flaky.test"}], body="hi", subject="s")
    with pytest.raises(RetryableError):
        await send_notifications(payload, ctx(attempt=1))
    result = await send_notifications(payload, ctx(attempt=2))
    assert result["delivered"] == 1


@pytest.mark.parametrize(
    "override",
    [
        {"subject": None},  # email requires a subject
        {"recipients": []},
        {"recipients": [{"address": "not-an-email"}]},
        {"channel": "sms", "recipients": [{"address": "4155550101"}]},  # needs E.164
        {"template": "Bad Template!"},
        {"recipients": [{"address": f"u{i}@example.com"} for i in range(501)]},
    ],
)
def test_notification_validation_rejects(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        notification(**override)


# --- data.transform -----------------------------------------------------------------------------


async def test_transform_pipeline_aggregates_deterministically() -> None:
    payload = TransformPayload.model_validate(
        {
            "csv": CSV,
            "steps": [
                {"op": "cast", "column": "units", "to": "int"},
                {"op": "filter", "column": "units", "operator": "gt", "value": 0},
                {
                    "op": "aggregate",
                    "group_by": ["region"],
                    "metrics": [
                        {"fn": "sum", "column": "units", "as": "units"},
                        {"fn": "count", "as": "lines"},
                    ],
                },
                {"op": "sort", "column": "units", "descending": True},
            ],
        }
    )
    result = await transform(payload, ctx())
    assert result["input_rows"] == 4
    assert result["preview"] == [
        {"region": "north", "units": 15.0, "lines": 2},
        {"region": "south", "units": 7.0, "lines": 1},
    ]
    again = await transform(payload, ctx())
    assert again["checksum_sha256"] == result["checksum_sha256"]


async def test_transform_csv_output_and_rename() -> None:
    payload = TransformPayload.model_validate(
        {
            "csv": CSV,
            "output_format": "csv",
            "steps": [
                {"op": "dedupe", "columns": ["product"]},
                {"op": "select", "columns": ["product", "price"]},
                {"op": "rename", "mapping": {"price": "list_price"}},
            ],
        }
    )
    result = await transform(payload, ctx())
    assert result["output"] == "product,list_price\nwidget,2.50\ngadget,9.99\n"
    assert result["columns"] == ["product", "list_price"]


async def test_transform_unknown_column_is_permanent() -> None:
    payload = TransformPayload.model_validate(
        {"csv": CSV, "steps": [{"op": "select", "columns": ["missing"]}]}
    )
    with pytest.raises(PermanentError, match="unknown column"):
        await transform(payload, ctx())


async def test_transform_cast_error_modes() -> None:
    csv = "id,qty\n1,4\n2,four\n3,\n"
    fail = TransformPayload.model_validate(
        {"csv": csv, "steps": [{"op": "cast", "column": "qty", "to": "int"}]}
    )
    with pytest.raises(PermanentError):
        await transform(fail, ctx())
    drop = TransformPayload.model_validate(
        {
            "csv": csv,
            "steps": [{"op": "cast", "column": "qty", "to": "int", "on_error": "drop_row"}],
        }
    )
    result = await transform(drop, ctx())
    assert result["output_rows"] == 2 and result["warnings"]
    null = TransformPayload.model_validate(
        {"csv": csv, "steps": [{"op": "cast", "column": "qty", "to": "int", "on_error": "null"}]}
    )
    assert [r["qty"] for r in (await transform(null, ctx()))["preview"]] == [4, None, None]


async def test_transform_skips_malformed_rows_with_warning() -> None:
    payload = TransformPayload.model_validate({"csv": "a,b\n1,2\n3\n4,5\n"})
    result = await transform(payload, ctx())
    assert result["output_rows"] == 2
    assert "1 row(s) skipped" in result["warnings"][0]


def test_transform_rejects_unknown_step() -> None:
    with pytest.raises(ValidationError):
        TransformPayload.model_validate(
            {"csv": CSV, "steps": [{"op": "eval", "expr": "__import__('os')"}]}
        )


# --- bench.synthetic ----------------------------------------------------------------------------


async def test_synthetic_fails_planned_attempts_then_succeeds() -> None:
    payload = SyntheticPayload(work_ms=0, fail_attempts=2)
    for attempt in (1, 2):
        with pytest.raises(RetryableError):
            await run_synthetic(payload, ctx(attempt=attempt))
    assert (await run_synthetic(payload, ctx(attempt=3)))["attempt"] == 3
    with pytest.raises(PermanentError):
        await run_synthetic(SyntheticPayload(permanent_failure=True), ctx())


def test_report_handler_is_registered_with_db_dependency() -> None:
    spec = get_handler("report.generate")
    assert spec is not None and "Read-only" in spec.side_effects
