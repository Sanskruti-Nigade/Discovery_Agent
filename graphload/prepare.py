"""Make 'envelope' style documents loadable without renaming anything by hand.

  {"entities": {"contact": {"record_count": 5, "data": [ {...}, ... ]}, ...}, "reference_data": {...}, ...}
becomes
  {"contact": [ {...}, ... ], ...}

* when the document has an "entities" object, only that is the schema (lookup / reference / exception sections are
  not part of the entity structure and would clash on table names);
* an entity of the form {..., "data": [records]} is replaced by its records, so the table is named after the
  entity ("contact") instead of "data" (which every entity shares).
"""


def _unwrap(value):
    data = value.get("data") if isinstance(value, dict) else None
    if isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
        return data
    return value


def prepare(doc):
    if isinstance(doc, dict) and isinstance(doc.get("entities"), dict):
        return {name: _unwrap(v) for name, v in doc["entities"].items()}
    return doc
