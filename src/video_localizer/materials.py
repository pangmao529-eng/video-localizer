"""Read user-selected translation tables and character references locally."""

import csv
import io
import json
import unicodedata
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path


class MaterialsError(ValueError):
    pass


def normalized(text):
    return " ".join(unicodedata.normalize("NFKC", text).split()).casefold()


@dataclass
class Materials:
    translations: dict = field(default_factory=dict)
    glossary: dict = field(default_factory=dict)
    references: dict = field(default_factory=dict)


SOURCE_COLUMNS = ("source", "source_text", "source_ja", "source_zh", "original", "原文", "源文本", "中文", "日文", "日语")
TARGET_COLUMNS = ("target", "target_text", "translation", "target_ko", "reference_ko", "译文", "定稿", "韩语", "韩文", "번역")
IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp"}


def _read_text(path):
    try:
        if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            raise MaterialsError(f"Missing or oversized materials file: {path.name}")
        return path.read_text(encoding="utf-8-sig")
    except (UnicodeError, OSError) as exc:
        raise MaterialsError(f"Cannot read {path.name} as UTF-8.") from exc


def _row_pair(row):
    if isinstance(row, (list, tuple)) and len(row) == 2:
        return row
    if isinstance(row, dict):
        cols = {normalized(str(key)): val for key, val in row.items()}
        for source_key in SOURCE_COLUMNS:
            for target_key in TARGET_COLUMNS:
                if source_key in cols and target_key in cols:
                    return cols[source_key], cols[target_key]
    raise MaterialsError("Each table row must have source and target columns.")


def _load_table(value, table_name):
    if value is None:
        return {}
    path = Path(value)
    data = _read_text(path)
    ext = path.suffix.lower()
    try:
        if ext == ".json":
            rows = json.loads(data)
            if isinstance(rows, dict) and isinstance(rows.get(table_name), (list, dict)):
                rows = rows[table_name]
            if isinstance(rows, dict):
                rows = list(rows.items())
            if not isinstance(rows, list):
                raise MaterialsError("Expected an array of rows or a source-to-target object.")
        elif ext in (".csv", ".tsv"):
            rows = csv.reader(io.StringIO(data, newline=""), delimiter="\t" if ext == ".tsv" else ",", strict=True)
            header = next(rows, [])
            if any(normalized(c) in SOURCE_COLUMNS for c in header) and any(normalized(c) in TARGET_COLUMNS for c in header):
                rows = (dict(zip(header, row)) for row in rows if row)
            else:
                rows = chain([header] if header else [], (row for row in rows if row))
        else:
            raise MaterialsError("Translation tables must be UTF-8 CSV, TSV or JSON.")

        result = {}
        for number, row in enumerate(rows, 1):
            if number > 50000:
                raise MaterialsError("The table exceeds 50,000 rows.")
            source, target = _row_pair(row)
            if not all(isinstance(s, str) and s.strip() and len(s) <= 4000 for s in (source, target)):
                raise MaterialsError(f"Missing or oversized translation in row {number}.")
            key = normalized(source)
            values = result.setdefault(key, [])
            if target.strip() not in values:
                values.append(target.strip())
        return result
    except (json.JSONDecodeError, csv.Error, RecursionError) as exc:
        raise MaterialsError(f"Invalid syntax in {path.name}.") from exc


def _load_references(value):
    if value is None:
        return {}
    path = Path(value)
    if path.is_dir():
        rows = ((item.stem, item) for item in path.iterdir()
                if item.is_file() and item.suffix.lower() in IMAGE_TYPES)
        root = path.resolve()
    else:
        if path.suffix.lower() != ".json":
            raise MaterialsError("References must be an image folder or a JSON name-to-file map.")
        try:
            files = json.loads(_read_text(path))
        except (json.JSONDecodeError, RecursionError) as exc:
            raise MaterialsError("Invalid reference map JSON.") from exc
        if not isinstance(files, dict) or not all(isinstance(v, str) and v for v in files.values()):
            raise MaterialsError("Each reference must map a character name to an image file.")
        root = path.resolve().parent
        rows = ((name, root / val) for name, val in files.items())
    output = {}
    try:
        for name, image_file in rows:
            image_file = image_file.resolve()
            if (not name.strip() or len(name) > 140 or not image_file.is_relative_to(root)
                    or image_file.suffix.lower() not in IMAGE_TYPES or not image_file.is_file()):
                raise MaterialsError("Invalid reference. Use a named image inside the selected folder.")
            if name in output or len(output) >= 500:
                raise MaterialsError("Duplicate reference name or more than 500 reference images.")
            output[name] = image_file
    except (OSError, RuntimeError) as exc:
        raise MaterialsError("Cannot read the selected character reference.") from exc
    return output


def load_materials(translations=None, glossary=None, references=None):
    return Materials(_load_table(translations, "translations"), _load_table(glossary, "glossary"),
                     _load_references(references))
