from pathlib import Path
from string import Template

from conversations.prompts_gen import _load_txt

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def gen_ff_plan_prompt(
    queries_filename: str,
    schema: str,
    file_format_plan_filename: str,
) -> str:
    prompt_path = _PROMPTS_DIR / "gen_ff_plan.txt"

    template_str = _load_txt(prompt_path)
    template = Template(template_str)
    return template.substitute(
        queries_path=queries_filename,
        schema=schema,
        file_format_plan_filename=file_format_plan_filename,
    )
