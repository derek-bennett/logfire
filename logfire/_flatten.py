from dataclasses import dataclass
from typing import Any, Generic, Mapping, Sequence, overload

from typing_extensions import TypeVar

ValueType = TypeVar('ValueType', default=Any)
KT = TypeVar('KT')
VT = TypeVar('VT')


@dataclass(slots=True)
class Flatten(Generic[ValueType]):
    value: ValueType


FlattenMapping = Flatten[Mapping[KT, VT]]
FlattenSequence = Flatten[Sequence[VT]]


@overload
def flatten(value: Mapping[KT, VT]) -> FlattenMapping[KT, VT]:
    ...


@overload
def flatten(value: Sequence[VT]) -> FlattenSequence[VT]:
    ...


def flatten(value: Any) -> Any:
    """
    A wrapper class that tells logfire to flatten the first level of a mapping or sequence into OTel
    parameters so they can be easily queried.

    Importantly, wrapping something in `flatten` doesn't affect how it's formatted in the log message.

    The function can be used on any `Mapping` or `Sequence` type, including `dict`, `list`, `tuple`, etc.

    Sample usage:
    ```py
    logfire.info('{my_dict=} {my_list=}', my_dict=flatten({'a': 1, 'b': 2}), my_list=flatten([3, 4]))
    #> my_dict={'a': 1, 'b': 2} my_list=[3, 4]
    ```
    Will have OTel attributes:
    ```json
    {
        "my_dict.a": 1,
        "my_dict.b": 2,
        "my_list.0": 3,
        "my_list.1": 4
    }
    ```
    """
    return Flatten(value)
