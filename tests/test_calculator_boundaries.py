import tracemalloc

import pytest

from agent.tools.builtin import calculate


@pytest.mark.parametrize('expression', ['0e-10000000', '0e-10000000%2', '2%0e-10000000'])
def test_zero_exponent_cannot_allocate_unbounded_decimal_output(expression):
    from agent.tools import ToolError

    tracemalloc.start()
    try:
        if expression.startswith('2%'):
            with pytest.raises(ToolError) as error:
                calculate(expression)
            assert error.value.code == 'arithmetic_error'
        else:
            assert calculate(expression)['value'] == '0'
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1_000_000
