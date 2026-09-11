"""Shared examination headings for MDRK and discharge documents."""
from datetime import datetime
import re

from mdrk_builder.domain import SpecialistRole


_GENITIVE_TITLES = {
    SpecialistRole.FRM: "врача физической и реабилитационной медицины",
    SpecialistRole.NEUROLOGIST: "врача физической и реабилитационной медицины",
    SpecialistRole.PHYSICAL_THERAPIST: "специалиста по физической реабилитации",
    SpecialistRole.OCCUPATIONAL_THERAPIST: "специалиста по эргореабилитации",
    SpecialistRole.LOGOPEDIST: "медицинского логопеда",
    SpecialistRole.NEUROPSYCHOLOGIST: "медицинского психолога/нейропсихолога",
    SpecialistRole.PATHOPSYCHOLOGIST: "медицинского психолога/патопсихолога",
    SpecialistRole.OTHER: "консультанта",
}


def specialist_result_heading(
    role: SpecialistRole,
    occurred_at: datetime | None,
    *,
    specialist_name: str = "",
    specialist_title: str = "",
) -> str:
    title = " ".join(specialist_title.split())
    if role in {SpecialistRole.FRM, SpecialistRole.NEUROLOGIST}:
        title = _GENITIVE_TITLES[SpecialistRole.FRM]
    elif not title or title.casefold() == role.display_name.casefold():
        title = _GENITIVE_TITLES[role]
    else:
        # Non-physician source titles may retain a clinician's edit.
        # Inflect known professional nouns without replacing the written post.
        title = title[0].lower() + title[1:]
        words = {"врач": "врача", "невролог": "невролога",
                 "специалист": "специалиста", "медицинский": "медицинского",
                 "логопед": "логопеда", "психолог": "психолога",
                 "нейропсихолог": "нейропсихолога", "патопсихолог": "патопсихолога"}
        title = re.sub(r"\b(?:" + "|".join(words) + r")\b",
                       lambda match: words[match.group().casefold()], title, flags=re.I)
    # Keep the name exactly as supplied by the selected source or manual edit.
    specialist = " ".join(part for part in (title, specialist_name.strip()) if part)
    timestamp = occurred_at.strftime("%d.%m.%Y, %H:%M") if occurred_at else "ДД.ММ.ГГГГ, ЧЧ:ММ"
    return f"Результат осмотра {specialist} ({timestamp})"
