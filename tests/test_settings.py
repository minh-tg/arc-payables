"""Settings must not accept a limit that does not exist.

The trade limits are written two ways: dollars (`max_invoice_usdc`, `min_reserve_usdc`) and derived
unit properties (`max_invoice_units`, `min_reserve_units`). Both names look like fields, so a caller
or an operator can easily set the derived one, which is not a field at all. A live run did exactly
that and then reported a reserve of 1 USDC while the system was enforcing the default 2000.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from arc_payables.settings import Settings


def test_an_unknown_setting_name_is_rejected_not_ignored():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, min_reserve_units=1_000_000)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_invoice_unit=2_000_000)


def test_unit_limits_are_derived_from_the_stated_amounts():
    settings = Settings(_env_file=None, max_invoice_usdc=Decimal("2"), min_reserve_usdc=Decimal("1"))
    assert settings.max_invoice_units == 2_000_000
    assert settings.min_reserve_units == 1_000_000


def test_demo_defaults_are_the_documented_examples():
    """These are examples for the demo, not recommendations, and they should not drift silently."""
    settings = Settings(_env_file=None)
    assert settings.max_invoice_usdc == Decimal("1000")
    assert settings.min_reserve_usdc == Decimal("2000")
