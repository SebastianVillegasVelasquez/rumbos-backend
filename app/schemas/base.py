from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    """Base for every schema that crosses the API boundary.

    Python code keeps snake_case field names; the JSON wire format is
    camelCase. `populate_by_name` lets Python callers (services, tests)
    keep constructing schemas with the snake_case names, and lets requests
    use either spelling. Responses serialize by alias (FastAPI's default
    for `response_model`).
    """

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, from_attributes=True
    )
