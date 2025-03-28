import re
from typing import Optional

from flyteidl.core.execution_pb2 import TaskExecution
from typing_extensions import Annotated

from flytekit import FlyteContextManager, kwtypes
from flytekit.core import context_manager
from flytekit.core.data_persistence import FileAccessProvider
from flytekit.core.type_engine import TypeEngine
from flytekit.extend.backend.base_connector import (
    ConnectorRegistry,
    Resource,
    SyncConnectorBase,
)
from flytekit.models.literals import LiteralMap
from flytekit.models.task import TaskTemplate

from .boto3_mixin import Boto3ConnectorMixin, CustomException


# https://github.com/flyteorg/flyte/issues/4505
def convert_floats_with_no_fraction_to_ints(data):
    if isinstance(data, dict):
        for key, value in data.items():
            data[key] = convert_floats_with_no_fraction_to_ints(value)
    elif isinstance(data, list):
        for i, item in enumerate(data):
            data[i] = convert_floats_with_no_fraction_to_ints(item)
    elif isinstance(data, float) and data.is_integer():
        return int(data)
    return data


class BotoConnector(SyncConnectorBase):
    """A general purpose boto3 connector that can be used to call any boto3 method."""

    name = "Boto Connector"

    def __init__(self):
        super().__init__(task_type_name="boto")

    async def do(
        self,
        task_template: TaskTemplate,
        output_prefix: str,
        inputs: Optional[LiteralMap] = None,
        **kwargs,
    ) -> Resource:
        custom = task_template.custom

        service = custom.get("service")
        raw_config = custom.get("config")
        convert_floats_with_no_fraction_to_ints(raw_config)
        config = raw_config
        region = custom.get("region")
        method = custom.get("method")
        images = custom.get("images")

        boto3_object = Boto3ConnectorMixin(service=service, region=region)

        result = None
        try:
            result, idempotence_token = await boto3_object._call(
                method=method,
                config=config,
                images=images,
                inputs=inputs,
            )
        except CustomException as e:
            original_exception = e.original_exception
            error_code = original_exception.response["Error"]["Code"]
            error_message = original_exception.response["Error"]["Message"]

            if error_code == "ValidationException" and "Cannot create already existing" in error_message:
                arn = re.search(
                    r"arn:aws:[a-zA-Z0-9\-]+:[a-zA-Z0-9\-]+:\d+:[a-zA-Z0-9\-\/]+",
                    error_message,
                ).group(0)
                if arn:
                    arn_result = None
                    if method == "create_model":
                        arn_result = {"ModelArn": arn}
                    elif method == "create_endpoint_config":
                        arn_result = {"EndpointConfigArn": arn}

                    return Resource(
                        phase=TaskExecution.SUCCEEDED,
                        outputs={
                            "result": arn_result if arn_result else {"result": f"Entity already exists {arn}."},
                            "idempotence_token": e.idempotence_token,
                        },
                    )
                else:
                    return Resource(
                        phase=TaskExecution.SUCCEEDED,
                        outputs={
                            "result": {"result": "Entity already exists."},
                            "idempotence_token": e.idempotence_token,
                        },
                    )
            else:
                # Re-raise the exception if it's not the specific error we're handling
                raise e
        except Exception as e:
            raise e

        outputs = {"result": {"result": None}}
        if result:
            truncated_result = None
            if method == "create_model":
                truncated_result = {"ModelArn": result.get("ModelArn")}
            elif method == "create_endpoint_config":
                truncated_result = {"EndpointConfigArn": result.get("EndpointConfigArn")}

            ctx = FlyteContextManager.current_context()
            builder = ctx.with_file_access(
                FileAccessProvider(
                    local_sandbox_dir=ctx.file_access.local_sandbox_dir,
                    raw_output_prefix=output_prefix,
                    data_config=ctx.file_access.data_config,
                )
            )
            with context_manager.FlyteContextManager.with_context(builder) as new_ctx:
                outputs = LiteralMap(
                    {
                        "result": TypeEngine.to_literal(
                            new_ctx,
                            truncated_result if truncated_result else result,
                            Annotated[dict, kwtypes(allow_pickle=True)],
                            TypeEngine.to_literal_type(dict),
                        ),
                        "idempotence_token": TypeEngine.to_literal(
                            new_ctx,
                            idempotence_token,
                            str,
                            TypeEngine.to_literal_type(str),
                        ),
                    }
                )

        return Resource(phase=TaskExecution.SUCCEEDED, outputs=outputs)


ConnectorRegistry.register(BotoConnector())
