from pathlib import Path
from string import Template


def build_query_files(
    query_list: list[str], sql_dict: dict[str, str]
) -> dict[str, str]:
    """Build per-query file contents without writing to disk."""
    result: dict[str, str] = {}

    # generate queryX.hpp files from template:
    template_path = Path(__file__).parent / "templates" / "queryX.hpp"
    template_str = template_path.read_text()
    template = Template(template_str)

    for qid in query_list:
        result[f"query{qid}.hpp"] = template.substitute(qid=qid)

    # generate queryX.cpp files from template:
    template_path = Path(__file__).parent / "templates" / "queryX.cpp"
    template_str = template_path.read_text()
    template = Template(template_str)

    for qid in query_list:
        assert not qid.startswith("Q"), f"Query id should not start with 'Q': {qid}"
        result[f"query{qid}.cpp"] = template.substitute(
            qid=qid,
            query_sql=sql_dict[f"Q{qid}"],
        )

    return result
