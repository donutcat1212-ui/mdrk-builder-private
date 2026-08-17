from __future__ import annotations


def normalize_medical_record_number(value: str) -> str:
    normalized = "".join(
        character
        for character in value.casefold().replace("№", "")
        if character.isalnum() or character == "/"
    )
    while normalized.startswith("скп"):
        normalized = normalized.removeprefix("скп")
    return normalized


def normalize_patient_full_name(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())
