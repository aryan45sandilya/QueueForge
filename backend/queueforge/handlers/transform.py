"""data.transform: parses CSV and applies a declarative pipeline of steps.

Steps are data, not code: filter, select, rename, cast, sort, dedupe, aggregate. Unknown
columns and impossible casts are permanent errors because retrying would fail the same way.
The pipeline is deterministic; its output checksum is part of the result so downstream
consumers can detect re-delivery of an identical output.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import OrderedDict
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from queueforge.handlers.base import HandlerSpec, JobContext, PermanentError, register

Scalar = str | int | float | bool | None
_CHUNK = 500


class FilterStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["filter"]
    column: str = Field(min_length=1, max_length=64)
    operator: Literal["eq", "ne", "gt", "gte", "lt", "lte", "contains", "empty", "not_empty"]
    value: Scalar = None


class SelectStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["select"]
    columns: list[str] = Field(min_length=1, max_length=100)


class RenameStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["rename"]
    mapping: dict[str, str] = Field(min_length=1, max_length=100)


class CastStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["cast"]
    column: str = Field(min_length=1, max_length=64)
    to: Literal["int", "float", "str", "bool"]
    on_error: Literal["fail", "null", "drop_row"] = "fail"


class SortStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["sort"]
    column: str = Field(min_length=1, max_length=64)
    descending: bool = False


class DedupeStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["dedupe"]
    columns: list[str] = Field(min_length=1, max_length=20)


class Metric(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fn: Literal["count", "sum", "avg", "min", "max"]
    column: str | None = Field(default=None, max_length=64)
    as_: str | None = Field(default=None, alias="as", max_length=64)

    def output_name(self) -> str:
        return self.as_ or (f"{self.fn}_{self.column}" if self.column else self.fn)


class AggregateStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["aggregate"]
    group_by: list[str] = Field(default_factory=list, max_length=10)
    metrics: list[Metric] = Field(min_length=1, max_length=20)


Step = Annotated[
    FilterStep | SelectStep | RenameStep | CastStep | SortStep | DedupeStep | AggregateStep,
    Field(discriminator="op"),
]


class TransformPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    csv: str = Field(min_length=1, description="CSV text including the header row")
    delimiter: Literal[",", ";", "\t", "|"] = ","
    steps: list[Step] = Field(default_factory=list, max_length=25)
    output_format: Literal["json", "csv"] = "json"
    preview_rows: int = Field(default=25, ge=0, le=200)


Row = dict[str, Any]


def _require(columns: list[str], wanted: list[str], step_index: int) -> None:
    missing = [c for c in wanted if c not in columns]
    if missing:
        raise PermanentError(
            f"Step {step_index + 1}: unknown column(s) {missing}; available: {columns}"
        )


def _cast(value: Any, to: str) -> Any:
    if value is None or value == "":
        return None
    if to == "int":
        return int(float(value)) if isinstance(value, str) and "." in value else int(value)
    if to == "float":
        return float(value)
    if to == "bool":
        lowered = str(value).strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n"}:
            return False
        raise ValueError(f"not a boolean: {value!r}")
    return str(value)


def _compare(left: Any, operator: str, right: Any) -> bool:
    if operator == "empty":
        return left is None or left == ""
    if operator == "not_empty":
        return not (left is None or left == "")
    if operator == "contains":
        return right is not None and str(right).lower() in str(left or "").lower()
    if left is None or left == "":
        return False
    # Compare numerically when both sides look numeric, otherwise as strings.
    try:
        lnum, rnum = float(left), float(right)
        left_v: Any = lnum
        right_v: Any = rnum
    except (TypeError, ValueError):
        left_v, right_v = str(left), str(right)
    match operator:
        case "eq":
            return bool(left_v == right_v)
        case "ne":
            return bool(left_v != right_v)
        case "gt":
            return bool(left_v > right_v)
        case "gte":
            return bool(left_v >= right_v)
        case "lt":
            return bool(left_v < right_v)
        case "lte":
            return bool(left_v <= right_v)
    raise PermanentError(f"unsupported operator {operator}")


def _sort_key(value: Any) -> tuple[int, Any]:
    if value is None or value == "":
        return (2, "")
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, (int, float)):
        return (0, value)
    try:
        return (0, float(value))
    except ValueError:
        return (1, str(value))


def _aggregate(rows: list[Row], step: AggregateStep) -> tuple[list[str], list[Row]]:
    groups: OrderedDict[tuple[Any, ...], list[Row]] = OrderedDict()
    for row in rows:
        groups.setdefault(tuple(row.get(c) for c in step.group_by), []).append(row)
    if not step.group_by and not groups:
        groups[()] = []
    out: list[Row] = []
    for key, members in groups.items():
        record: Row = dict(zip(step.group_by, key, strict=True))
        for metric in step.metrics:
            name = metric.output_name()
            if metric.fn == "count":
                record[name] = (
                    len(members)
                    if metric.column is None
                    else sum(1 for m in members if m.get(metric.column) not in (None, ""))
                )
                continue
            values: list[float] = []
            for m in members:
                raw = m.get(metric.column or "")
                if raw in (None, ""):
                    continue
                try:
                    values.append(float(raw))
                except (TypeError, ValueError) as exc:
                    raise PermanentError(
                        f"aggregate {metric.fn}({metric.column}): non-numeric value {raw!r}"
                    ) from exc
            if not values:
                record[name] = None
            elif metric.fn == "sum":
                record[name] = round(sum(values), 10)
            elif metric.fn == "avg":
                record[name] = round(sum(values) / len(values), 10)
            elif metric.fn == "min":
                record[name] = min(values)
            else:
                record[name] = max(values)
        out.append(record)
    columns = [*step.group_by, *[m.output_name() for m in step.metrics]]
    return columns, out


async def transform(payload: TransformPayload, ctx: JobContext) -> dict[str, Any]:
    reader = csv.reader(io.StringIO(payload.csv), delimiter=payload.delimiter)
    try:
        header = next(reader)
    except StopIteration as exc:
        raise PermanentError("CSV is empty") from exc
    columns = [h.strip() for h in header]
    if len(set(columns)) != len(columns) or any(not c for c in columns):
        raise PermanentError("CSV header must contain unique, non-empty column names")

    rows: list[Row] = []
    malformed = 0
    for line_number, record in enumerate(reader, start=2):
        if not record:
            continue
        if len(record) != len(columns):
            malformed += 1
            if malformed > 100:
                raise PermanentError(
                    f"More than 100 malformed rows (first bad row near line {line_number})"
                )
            continue
        rows.append(dict(zip(columns, record, strict=True)))
        if len(rows) % _CHUNK == 0:
            await ctx.checkpoint()
    input_rows = len(rows)

    warnings: list[str] = []
    if malformed:
        warnings.append(f"{malformed} row(s) skipped: column count did not match the header")

    for index, step in enumerate(payload.steps):
        if isinstance(step, FilterStep):
            _require(columns, [step.column], index)
            rows = [r for r in rows if _compare(r.get(step.column), step.operator, step.value)]
        elif isinstance(step, SelectStep):
            _require(columns, step.columns, index)
            rows = [{c: r.get(c) for c in step.columns} for r in rows]
            columns = list(step.columns)
        elif isinstance(step, RenameStep):
            _require(columns, list(step.mapping), index)
            columns = [step.mapping.get(c, c) for c in columns]
            if len(set(columns)) != len(columns):
                raise PermanentError(f"Step {index + 1}: rename produces duplicate column names")
            rows = [{step.mapping.get(k, k): v for k, v in r.items()} for r in rows]
        elif isinstance(step, CastStep):
            _require(columns, [step.column], index)
            kept: list[Row] = []
            failures = 0
            for r in rows:
                try:
                    r[step.column] = _cast(r.get(step.column), step.to)
                except (TypeError, ValueError) as exc:
                    failures += 1
                    if step.on_error == "fail":
                        raise PermanentError(
                            f"Step {index + 1}: cannot cast {step.column}={r.get(step.column)!r} to {step.to}"
                        ) from exc
                    if step.on_error == "drop_row":
                        continue
                    r[step.column] = None
                kept.append(r)
            rows = kept
            if failures:
                warnings.append(
                    f"Step {index + 1}: {failures} value(s) failed to cast ({step.on_error})"
                )
        elif isinstance(step, SortStep):
            _require(columns, [step.column], index)
            rows.sort(key=lambda r: _sort_key(r.get(step.column)), reverse=step.descending)
        elif isinstance(step, DedupeStep):
            _require(columns, step.columns, index)
            seen: set[tuple[Any, ...]] = set()
            unique: list[Row] = []
            for r in rows:
                key = tuple(r.get(c) for c in step.columns)
                if key not in seen:
                    seen.add(key)
                    unique.append(r)
            rows = unique
        elif isinstance(step, AggregateStep):
            wanted = list(step.group_by) + [m.column for m in step.metrics if m.column]
            _require(columns, wanted, index)
            columns, rows = _aggregate(rows, step)
        await ctx.checkpoint()

    if payload.output_format == "csv":
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        serialized = buffer.getvalue()
    else:
        serialized = json.dumps(rows, separators=(",", ":"), default=str)

    ctx.logger.info(
        "transform_completed",
        input_rows=input_rows,
        output_rows=len(rows),
        steps=len(payload.steps),
    )
    output_inline = len(serialized) <= 32_000
    return {
        "input_rows": input_rows,
        "output_rows": len(rows),
        "columns": columns,
        "steps_applied": len(payload.steps),
        "warnings": warnings,
        "preview": rows[: payload.preview_rows],
        "output_format": payload.output_format,
        "output": serialized if output_inline else None,
        "output_truncated": not output_inline,
        "output_bytes": len(serialized.encode()),
        "checksum_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
    }


SPEC = register(
    HandlerSpec(
        name="data.transform",
        title="Transform CSV data",
        description=(
            "Parses CSV text and applies filter, select, rename, cast, sort, dedupe and "
            "aggregate steps. Returns a preview, the output and its checksum."
        ),
        side_effects="None. Pure computation; safe to retry.",
        payload_model=TransformPayload,
        run=transform,
        default_timeout_seconds=120,
        example={
            "csv": "region,product,units,price\nnorth,widget,12,2.50\nsouth,widget,7,2.50\nnorth,gadget,3,9.99\nsouth,gadget,0,9.99\n",
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
            "output_format": "json",
        },
    )
)
