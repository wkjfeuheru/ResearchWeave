"""Four independent metric dimensions; unknowns never become zero-cost successes."""

import re
import shlex
from datetime import datetime
from decimal import Decimal, InvalidOperation

from openharness.evaluation.judge import citation_map, evaluated_text
from openharness.evaluation.models import MetricResult


def metric(name, value, *, numerator=None, denominator=None, source="rule", explanation=""):
    return MetricResult(
        name=name,
        value=value,
        numerator=numerator,
        denominator=denominator,
        source=source,
        explanation=explanation,
        status="not_applicable" if value is None else "scored",
    )


def ratio(name, passed, total, **kwargs):
    return metric(
        name, passed / total if total else None, numerator=passed, denominator=total, **kwargs
    )


def numeric_match(requirement, review):
    if review is None or review.observed_value is None or not review.quote:
        return False
    try:
        actual = Decimal(review.observed_value.replace(",", ""))
        expected = Decimal(requirement.value)
        quoted_numbers = [
            Decimal(v.replace(",", ""))
            for v in re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?", review.quote)
        ]
        if not actual.is_finite() or actual not in quoted_numbers:
            return False
        aliases = {
            "RMB": "CNY",
            "人民币": "CNY",
            "合并报表": "合并",
            "合并口径": "合并",
            "consolidated": "合并",
            "百分比": "%",
            "元人民币": "元",
            "亿元人民币": "亿元",
        }

        def canonical(value, key):
            value = aliases.get(value, value)
            if key == "period":
                value = re.sub(r"^(?:FY)?(\d{4})(?:年(?:度)?|A)?$", r"\1", value)
            return value

        if any(
            getattr(requirement, key)
            and canonical(getattr(review, key), key) != canonical(getattr(requirement, key), key)
            for key in ("unit", "currency", "period", "scope")
        ):
            return False
        tolerance = max(Decimal(requirement.atol), abs(expected) * Decimal(requirement.rtol))
        return abs(actual - expected) <= tolerance
    except (InvalidOperation, TypeError):
        return False


def path_checks(case, artifact):
    tools = [o for o in artifact.observations if o.kind == "tool"]
    successful = [o for o in tools if o.status == "ok"]
    names = {o.name for o in successful}
    selected = [bool(names.intersection(group)) for group in case.path.tool_groups]
    forbidden = [o.name for o in tools if o.name in case.path.forbidden_tools]
    selected.append(not forbidden)
    skills = [
        any(o.name == "skill" and (o.input or {}).get("name") == skill for o in successful)
        for skill in case.path.skills
    ]

    def executed_script(observation, script):
        if observation.name != "bash":
            return False
        recorded = observation.metadata.get("executed_script")
        if recorded:
            return recorded.rsplit("/", 1)[-1] == script
        try:
            parts = shlex.split((observation.input or {}).get("command", ""))
            # Script must be an executed Python argument, not mentioned in echo/print.
            for index, token in enumerate(parts):
                if token.rsplit("/", 1)[-1] == script and index:
                    prefix = parts[max(0, index - 2) : index]
                    if (
                        any(p.rsplit("/", 1)[-1].startswith("python") for p in prefix)
                        and "-c" not in prefix
                    ):
                        return True
        except ValueError:
            return False
        return False

    scripts = [any(executed_script(o, script) for o in successful) for script in case.path.scripts]
    skills.extend(scripts)
    history = artifact.research_state.get("history", [])
    ordering = []
    if case.path.plan_before_collection:
        plan = next(
            (i for i, event in enumerate(history) if event["action"] == "create_plan"), None
        )
        substantive = [
            i
            for i, event in enumerate(history)
            if event["action"] == "capture_source"
            and artifact.research_state.get("sources", {})
            .get(event["data"].get("source_id"), {})
            .get("kind")
            in {"web", "file", "mcp", "calculation"}
        ]
        ordering.append(plan is not None and (not substantive or plan < min(substantive)))
    for earlier, later in case.path.order:
        positions_a = [i for i, o in enumerate(tools) if o.name == earlier and o.status == "ok"]
        positions_b = [i for i, o in enumerate(tools) if o.name == later and o.status == "ok"]
        ordering.append(bool(positions_a and positions_b and positions_a[0] < positions_b[0]))
    evidence = artifact.research_state.get("evidence_pool", {})
    if case.path.verification_required:
        ordering.append(
            any(
                e.get("status") in {"source_checked", "verified"} and not e.get("needs_review")
                for e in evidence.values()
            )
        )
    for fault in case.faults:
        injected = [
            o
            for o in tools
            if o.name == fault.tool and o.metadata.get("injected_fault") == fault.kind
        ]
        ordering.append(bool(injected))
        if injected and fault.kind == "tool_error":
            ordering.append(
                any(
                    o.name == fault.tool
                    and o.status == "ok"
                    and o.started_at > injected[0].started_at
                    for o in tools
                )
            )
    conflict = []
    conflicts = list(artifact.research_state.get("conflicts", {}).values())
    current = [c for c in conflicts if c.get("kind") == case.path.conflict_kind]
    if case.path.conflict != "none":
        conflict.append(bool(current))
        if case.path.conflict in {"resolve", "unresolved", "reopen"}:
            conflict.append(any(o.name == "investigate_conflict" for o in tools))
        if case.path.conflict == "resolve":
            conflict.append(any(c["status"] == "resolved" for c in current))
        if case.path.conflict == "unresolved":
            conflict.append(
                any(
                    c["status"] in {"unresolved", "open", "interrupted", "awaiting_review"}
                    for c in current
                )
            )
        if case.path.conflict == "reopen":
            conflict.append(
                any(e["action"] == "reopen_conflict" for e in history)
                or any(
                    c.get("review_note") == "相关证据已修订或撤回，裁决需要复核" for c in current
                )
            )
    # Citing retracted/superseded or review-required evidence always invalidates the path.
    replaced = {e.get("supersedes") for e in evidence.values() if e.get("supersedes")}
    answers = list(artifact.research_state.get("answers", {}).values())
    latest = next((a for a in reversed(answers) if a.get("rendered") == artifact.answer), {})
    for item in latest.get("citations", {}).values():
        e = evidence.get(item["evidence"]["id"], item["evidence"])
        ordering.append(
            e["id"] not in replaced and e.get("status") != "retracted" and not e.get("needs_review")
        )
    if latest.get("invalid"):
        ordering.append(False)
    return {
        "tool_selection": selected,
        "skill_use": skills,
        "dependencies": ordering,
        "conflict": conflict,
    }


def efficiency(artifact):
    generations = [o for o in artifact.observations if o.kind == "generation"]
    reported = [o for o in generations if o.usage and o.usage.get("reported")]
    all_reported = len(reported) == len(generations) and bool(generations)
    scores = [
        ratio("token_reporting_coverage", len(reported), len(generations), source="measurement")
    ]
    for name in (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ):
        known = [o.usage[name] for o in reported if o.usage.get(name) is not None]
        value = sum(known) if all_reported and len(known) == len(generations) else None
        scores.append(metric(name, value, source="measurement"))
        if known and value is None:
            scores.append(
                metric(
                    "observed_" + name,
                    sum(known),
                    source="measurement",
                    explanation="部分统计，不能作为完整任务消耗",
                )
            )
    scores.extend(
        [
            metric(
                "total_tokens",
                sum(o.usage["input_tokens"] + o.usage["output_tokens"] for o in reported)
                if all_reported
                else None,
                source="measurement",
            ),
            metric("model_steps", len(generations), source="measurement"),
            metric(
                "tool_steps",
                sum(o.kind == "tool" for o in artifact.observations),
                source="measurement",
            ),
            metric(
                "retries",
                sum(o.metadata.get("retries", 0) for o in generations),
                source="measurement",
            ),
            metric(
                "investigation_tool_steps",
                sum(
                    o.kind == "tool" and o.metadata.get("investigation", False)
                    for o in artifact.observations
                ),
                source="measurement",
            ),
            metric("end_to_end_ms", artifact.elapsed_ms, source="measurement"),
            metric("first_content_ms", artifact.first_content_ms, source="measurement"),
        ]
    )
    by_id = {o.id: o for o in artifact.observations}

    def role(observation):
        current = observation
        while current:
            if current.name == "compaction":
                return "compaction"
            if current.name == "investigate_conflict" or current.metadata.get("investigation"):
                return "investigation"
            current = by_id.get(current.parent_id)
        return "main"

    # main decision rounds differ from all model calls including investigation/compaction.
    scores.append(
        metric(
            "agent_decision_steps",
            sum(role(o) == "main" for o in generations),
            source="measurement",
        )
    )
    for name in ("main", "investigation", "compaction"):
        group = [o for o in generations if role(o) == name]
        complete = all(o.usage and o.usage.get("reported") for o in group)
        scores.append(
            metric(
                name + "_total_tokens",
                sum(o.usage["input_tokens"] + o.usage["output_tokens"] for o in group)
                if complete
                else None,
                source="measurement",
            )
        )
        scores.append(metric(name + "_model_calls", len(group), source="measurement"))
    for kind in ("generation", "tool"):
        intervals = sorted(
            (datetime.fromisoformat(o.started_at).timestamp() * 1000, o.duration_ms)
            for o in artifact.observations
            if o.kind == kind
        )
        active, end = 0, float("-inf")
        for begin, duration in intervals:
            stop = begin + duration
            active += max(0, stop - max(end, begin))
            end = max(end, stop)
        scores.append(
            metric(
                kind + "_active_ms",
                active,
                source="measurement",
                explanation="同类调用时间区间并集；并发和嵌套不重复相加",
            )
        )
    judge_reported = [u for u in artifact.judge_usage if u.get("usage_reported")]
    scores.extend(
        [
            ratio(
                "judge_token_reporting_coverage",
                len(judge_reported),
                len(artifact.judge_usage),
                source="measurement",
            ),
            metric("judge_model_calls", len(artifact.judge_usage), source="measurement"),
            metric(
                "judge_elapsed_ms",
                artifact.provenance.get("judge_elapsed_ms"),
                source="measurement",
            ),
        ]
    )
    scores.append(
        metric(
            "judge_total_tokens",
            sum(u["input_tokens"] + u["output_tokens"] for u in judge_reported)
            if judge_reported and len(judge_reported) == len(artifact.judge_usage)
            else None,
            source="measurement",
        )
    )
    cached = [o.usage for o in reported if o.usage.get("cache_read_input_tokens") is not None]
    scores.append(
        ratio(
            "cache_hit_rate",
            sum(u["cache_read_input_tokens"] for u in cached),
            sum(u.get("cache_observed_input_tokens", u["input_tokens"]) for u in cached),
            source="measurement",
        )
    )
    return scores


def score_case(case, artifact, judge=None):
    scores = efficiency(artifact)
    checks = path_checks(case, artifact)
    path_passed = []
    for name, values in checks.items():
        scores.append(ratio("path_" + name + "_rule_coverage", sum(values), len(values)))
        if judge:
            semantic = getattr(judge.path, name)
            if semantic is not None:
                values.append(semantic >= 3)
        elif values:
            scores.append(
                MetricResult(
                    name="path_" + name,
                    value=None,
                    status="unjudged",
                    explanation="规则轨迹已检查；语义判定尚未完成",
                )
            )
            continue
        scores.append(ratio("path_" + name, sum(values), len(values), source="combined"))
        path_passed.extend(values)
    if judge:
        scores.append(
            metric("path_correct", int(bool(path_passed) and all(path_passed)), source="combined")
        )
    else:
        rule_failed = any(not v for values in checks.values() for v in values)
        scores.append(
            MetricResult(
                name="path_correct",
                value=0 if rule_failed else None,
                status="scored" if rule_failed else "unjudged",
                explanation="必要规则约束失败" if rule_failed else "语义判断待补齐",
            )
        )
    reviews = {r.id: r for r in judge.requirements} if judge else {}
    requirements = []
    for required in case.requirements:
        review = reviews.get(required.id)
        if required.check == "artifact":
            matching = [name for name in artifact.artifacts if name.endswith(required.value or "")]
            valid = any(
                artifact.artifact_metadata.get(name, {}).get("valid", False) for name in matching
            )
            unknown = any(name not in artifact.artifact_metadata for name in matching)
            passed = (
                (review.score >= 3 if review else None) if valid else (None if unknown else False)
            )
        elif required.check == "numeric":
            passed = review.score >= 3 and numeric_match(required, review) if judge else None
        else:
            passed = review.score >= 3 if review else None
        if required.required:
            requirements.append(passed)
        scores.append(
            MetricResult(
                name="requirement_" + required.id,
                value=float(passed) if passed is not None else None,
                status="scored" if passed is not None else "unjudged",
                source="rule" if required.check == "artifact" else "combined",
                explanation=review.explanation if review else "",
            )
        )
    known = [v for v in requirements if v is not None]
    if len(known) == len(requirements):
        scores.append(
            ratio("requirement_coverage", sum(known), len(requirements), source="combined")
        )
    else:
        scores.append(MetricResult(name="requirement_coverage", value=None, status="unjudged"))
    scores.append(ratio("requirement_judgment_coverage", len(known), len(requirements)))
    if artifact.status != "completed" or not artifact.answer.strip():
        success = 0
    elif len(known) != len(requirements):
        success = None
    else:
        success = int(all(known))
    if judge is None:
        scores.extend(
            [
                MetricResult(
                    name=name,
                    value=None,
                    status="unjudged" if evaluated_text(artifact).strip() else "not_applicable",
                )
                for name in (
                    "unsupported_statement_rate",
                    "citation_correctness",
                    "citation_coverage",
                )
            ]
        )
    else:
        text = evaluated_text(artifact)
        mapping = citation_map(artifact)
        claims = [c for c in judge.claims if c.kind == "fact"]
        unsupported = sum(c.support != "supported" or not c.source_ids for c in claims)
        citations = [(c, citation) for c in judge.claims for citation in c.citations]
        correct = sum(
            citation.supported
            and mapping.get(citation.marker) == citation.source_id
            and citation.marker in c.quote
            for c, citation in citations
        )
        expected_citation = [
            c for c in judge.claims if c.requires_citation and c.kind != "operation"
        ]
        covered = sum(
            any(
                cit.supported and mapping.get(cit.marker) == cit.source_id and cit.marker in c.quote
                for cit in c.citations
            )
            for c in expected_citation
        )
        # Unmapped reference markers and explicit invalid citations cannot be omitted by the judge.
        cited_markers = {cit.marker for _, cit in citations}
        body = re.sub(r"(?m)^- \[report:[^\]]+\].*$", "", text)
        body = re.sub(r"(?ms)^来源：.*?(?=\n报告文件：|\Z)", "", body)
        used_markers = set(re.findall(r"\[(?:E:[^\]\s]+|report:[a-f0-9]+:R\d+|R?\d+)\]", body))
        missing = used_markers - cited_markers
        total_citations = len(citations) + len(missing) + text.count("[来源不可核验]")
        scores.extend(
            [
                ratio("unsupported_statement_rate", unsupported, len(claims), source="combined"),
                ratio("citation_correctness", correct, total_citations, source="combined"),
                ratio("citation_coverage", covered, len(expected_citation), source="combined"),
            ]
        )
        if success == 1 and (
            judge.critical_errors
            or unsupported
            or text.count("[来源不可核验]")
            or correct < total_citations
            or covered < len(expected_citation)
        ):
            success = 0
    scores.append(
        MetricResult(
            name="task_success",
            value=success,
            source="combined",
            status="unjudged" if success is None else "scored",
        )
    )
    return scores
