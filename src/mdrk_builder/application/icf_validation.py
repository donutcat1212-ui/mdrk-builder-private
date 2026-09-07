"""Current completeness of ICF assessments for both clinical documents."""
from mdrk_builder.domain import ReviewIssue, ReviewSeverity


def icf_assessment_issues(domains, *, include_final):
    issues = []
    for index, domain in enumerate(domains):
        # Personal factors are descriptive rows (for example age/motivation),
        # not numeric ICF qualifier pairs.
        if domain.code.strip().casefold().startswith("pf"):
            continue
        if domain.initial is None:
            issues.append(
                ReviewIssue(
                    code="icf_initial_missing",
                    message=f"У домена {domain.code} отсутствует исходная оценка",
                    severity=ReviewSeverity.WARNING,
                    field=f"icf.{index}.initial",
                    source=domain.initial_source or domain.final_source,
                )
            )
        if include_final and domain.final is None:
            issues.append(
                ReviewIssue(
                    code="icf_final_missing",
                    message=f"У домена {domain.code} отсутствует повторная оценка",
                    severity=ReviewSeverity.WARNING,
                    field=f"icf.{index}.final",
                    source=domain.final_source or domain.initial_source,
                )
            )

    return issues
