import os

import pytest


@pytest.fixture(autouse=True)
def isolated_environment():
    original = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(original)
