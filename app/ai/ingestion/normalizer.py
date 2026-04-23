from typing import List, Dict


def normalize_sections(sections: List[Dict]) -> List[Dict]:
    """
    Takes parsed sections and converts tables into explanations.
    Returns normalized sections ready for chunking.
    """

    normalized = []

    for section in sections:
        content = section["content"]

        # Detect tables
        if looks_like_table(content):
            explanation = table_to_explanation(content, section.get("title"))
            normalized.append({"title": section.get("title"), "content": explanation})
        else:
            normalized.append(section)

    return normalized


def looks_like_table(text: str) -> bool:
    """
    Heuristic: detects table-like structures.
    """
    lines = text.splitlines()
    pipe_lines = [line for line in lines if "|" in line]

    # At least 2 rows with column separators
    return len(pipe_lines) >= 2


def table_to_explanation(table_text: str, title: str = None) -> str:
    """
    Converts a markdown / pipe-separated table into a
    human-readable explanation.
    """

    rows = []
    for line in table_text.splitlines():
        if "|" in line:
            # Skip common markdown table separator lines like |---|---|
            if all(c in " |-:" for c in line.strip()):
                continue
            cells = [cell.strip() for cell in line.split("|") if cell.strip()]
            if cells:
                rows.append(cells)

    if len(rows) < 2:
        return table_text  # fallback

    headers = rows[0]
    data_rows = rows[1:]

    explanations = []

    if title:
        explanations.append(f"{title}:")

    for row in data_rows:
        sentence = row_to_sentence(headers, row)
        if sentence:
            explanations.append(sentence)

    return " ".join(explanations)


def row_to_sentence(headers, row):
    """
    Converts one table row into a sentence.
    """
    if len(headers) != len(row):
        return None

    pairs = zip(headers, row)

    subject = row[0]
    descriptors = []

    for header, value in list(pairs)[1:]:
        descriptors.append(f"{header.lower()} is {value}")

    if descriptors:
        return (
            f"{subject} has the following characteristics: "
            + ", ".join(descriptors)
            + "."
        )

    return None
