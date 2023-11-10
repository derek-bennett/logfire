from __future__ import annotations

import re
from contextlib import ExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast

from pydantic_core import CoreConfig, CoreSchema

import logfire
from logfire._config import GLOBAL_CONFIG

if TYPE_CHECKING:
    from pydantic import ValidationError
    from pydantic.plugin import (
        SchemaKind,
        SchemaTypePath,
        ValidateJsonHandlerProtocol,
        ValidatePythonHandlerProtocol,
        ValidateStringsHandlerProtocol,
    )
    from typing_extensions import TypeAlias

    # It might make sense to export the following type alias from `pydantic.plugin`, rather than redefining it here.
    StringInput: TypeAlias = 'dict[str, StringInput]'

METER = GLOBAL_CONFIG._meter_provider.get_meter('pydantic-plugin-meter')  # type: ignore


class BaseValidateHandler:
    validation_method: ClassVar[str]
    span_stack: ExitStack
    __slots__ = 'schema_name', 'span_stack', '_record', '_successful_validation_counter', '_failed_validation_counter'

    def __init__(
        self,
        schema: CoreSchema,
        _config: CoreConfig | None,
        _plugin_settings: dict[str, dict[str, Any]],
        schema_type_path: SchemaTypePath,
        record: Literal['all', 'failure', 'metrics'],
    ) -> None:
        # We accept the schema, config, and plugin_settings in the init since these are the things
        # that are currently exposed by the plugin to potentially configure the validation handlers.
        self.schema_name = get_schema_name(schema)
        self._record = record

        # As the counter name should be less than 63 chars, we only get the last 40 chars of model name
        successful_validation_counter_name = f'{schema_type_path.name.split(".")[-1][-40:]}-successful-validation'
        self._successful_validation_counter = METER.create_counter(name=successful_validation_counter_name)
        failed_validation_counter_name = f'{schema_type_path.name.split(".")[-1][-40:]}-failed-validation'
        self._failed_validation_counter = METER.create_counter(name=failed_validation_counter_name)

    def _on_enter(
        self,
        input_data: Any,
        *,
        strict: bool | None = None,
        from_attributes: bool | None = None,
        context: dict[str, Any] | None = None,
        self_instance: Any | None = None,
    ) -> None:
        self.span_stack = ExitStack()
        if self._record == 'all':
            self.span_stack.enter_context(
                logfire.span(
                    'Pydantic {schema_name} {validation_method}',
                    schema_name=self.schema_name,
                    validation_method=self.validation_method,
                    input_data=input_data,
                    span_name=f'pydantic.{self.validation_method}',
                )
            )

    def on_success(self, result: Any) -> None:
        if self._record == 'all':
            logfire.debug('Validation successful {result=!r}', result=result)

        self._successful_validation_counter.add(1)

        self.span_stack.close()

    def on_error(self, error: ValidationError) -> None:
        error_count = error.error_count()
        plural = '' if error_count == 1 else 's'
        logfire.warning(
            '{error_count} validation error{plural}',
            error_count=error_count,
            plural=plural,
            errors=error.errors(include_url=False),
        )
        self._failed_validation_counter.add(1)
        self.span_stack.close()

    def on_exception(self, exception: Exception) -> None:
        logfire.error(
            '{exception_type=}: {exception_msg=}', exception=type(exception).__name__, exception_msg=exception
        )
        self.span_stack.__exit__(type(exception), exception, exception.__traceback__)


def get_schema_name(schema: CoreSchema) -> str:
    """Find the best name to use for a schema.

    The follow rules are used:
    * If the schema represents a model or dataclass, use the name of the class.
    * If the root schema is a wrap/before/after validator, look at its `schema` property.
    * Otherwise use the schema's `type` property.

    Args:
        schema: The schema to get the name for.

    Returns:
        The name of the schema.
    """
    schema_type = schema['type']
    if schema_type in {'model', 'dataclass'}:
        return schema['cls'].__name__  # type: ignore
    elif schema_type in {'function-after', 'function-before', 'function-wrap'}:
        return get_schema_name(schema['schema'])  # type: ignore
    else:
        return schema_type


class ValidatePythonHandler(BaseValidateHandler):
    """Implements `pydantic.plugin.ValidatePythonHandlerProtocol`."""

    validation_method = 'validate_python'

    def on_enter(
        self,
        input: Any,
        *,
        strict: bool | None = None,
        from_attributes: bool | None = None,
        context: dict[str, Any] | None = None,
        self_instance: Any | None = None,
    ) -> None:
        self._on_enter(
            input, strict=strict, from_attributes=from_attributes, context=context, self_instance=self_instance
        )


class ValidateJsonHandler(BaseValidateHandler):
    """Implements `pydantic.plugin.ValidateJsonHandlerProtocol`."""

    validation_method = 'validate_json'

    def on_enter(
        self,
        input: str | bytes | bytearray,
        *,
        strict: bool | None = None,
        context: dict[str, Any] | None = None,
        self_instance: Any | None = None,
    ) -> None:
        self._on_enter(input, strict=strict, context=context, self_instance=self_instance)


class ValidateStringsHandler(BaseValidateHandler):
    """Implements `pydantic.plugin.ValidateStringsHandlerProtocol`."""

    validation_method = 'validate_strings'

    def on_enter(
        self, input: StringInput, *, strict: bool | None = None, context: dict[str, Any] | None = None
    ) -> None:
        self._on_enter(input, strict=strict, context=context)


@dataclass
class LogfirePydanticPlugin:
    """Implements `pydantic.plugin.PydanticPluginProtocol`.

    Environment Variables:
        LOGFIRE_DISABLE_PYDANTIC_PLUGIN: Set to `1` or `true` to disable the plugin.
            TODO(lig): Use PYDANTIC_DISABLE_PLUGINS instead. See https://github.com/pydantic/pydantic/issues/7709
    """

    def new_schema_validator(
        self,
        schema: CoreSchema,
        schema_type: Any,
        schema_type_path: SchemaTypePath,
        schema_kind: SchemaKind,
        config: CoreConfig | None,
        plugin_settings: dict[str, Any],
    ) -> tuple[
        ValidatePythonHandlerProtocol | None, ValidateJsonHandlerProtocol | None, ValidateStringsHandlerProtocol | None
    ]:
        record = 'off'

        logfire_settings = plugin_settings.get('logfire')
        if logfire_settings and logfire_settings.get('record'):
            record = logfire_settings['record']
        else:
            record = GLOBAL_CONFIG.pydantic_plugin_record

        if record == 'off':
            return None, None, None

        if include_model(schema, schema_type_path):
            record = cast(Literal['all', 'failure', 'metrics'], record)
            return (
                ValidatePythonHandler(schema, config, plugin_settings, schema_type_path, record),
                ValidateJsonHandler(schema, config, plugin_settings, schema_type_path, record),
                ValidateStringsHandler(schema, config, plugin_settings, schema_type_path, record),
            )

        return None, None, None


plugin = LogfirePydanticPlugin()

# set of modules to ignore completed
IGNORED_MODULE_PREFIXES: tuple[str, ...] = 'fastapi.', 'logfire_backend.'


def include_model(schema: CoreSchema, schema_type_path: SchemaTypePath) -> bool:
    """Check whether a model should be instrumented."""
    include = GLOBAL_CONFIG.pydantic_plugin_include
    exclude = GLOBAL_CONFIG.pydantic_plugin_exclude

    schema_type = schema['type']
    if schema_type in {'function-after', 'function-before', 'function-wrap'}:
        return include_model(schema['schema'])  # type: ignore

    # check if the model is in ignored model
    if any(schema_type_path.module.startswith(prefix) for prefix in IGNORED_MODULE_PREFIXES):
        return False

    # check if the model is in exclude models
    if exclude and any(
        re.search(f'{pattern}$', f'{schema_type_path.module}::{schema_type_path.name}') for pattern in exclude
    ):
        return False

    # check if the model is in include models
    if include:
        return any(
            re.search(f'{pattern}$', f'{schema_type_path.module}::{schema_type_path.name}') for pattern in include
        )
    return True


if TYPE_CHECKING:
    # This is just to ensure we get type checking that the plugin actually implements the expected protocol.
    from pydantic.plugin import PydanticPluginProtocol

    def check_plugin_protocol(_plugin: PydanticPluginProtocol) -> None:
        pass

    check_plugin_protocol(plugin)
