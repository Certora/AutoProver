import pytest

from tests.conftest import needs_postgres

pytestmark = [pytest.mark.expensive, needs_postgres, pytest.mark.asyncio]

