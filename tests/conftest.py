"""Test fixtures: configure the hooks layer with the source agent's persona.

The suite was adapted from the live agent (Rain), whose name appears in
prompt/scene assertions; configuring it here keeps those assertions exact
while the package itself stays agent-neutral.
"""
import pytest

from telegram_presence import hooks


@pytest.fixture(autouse=True)
def _rain_persona():
    hooks.configure(agent_name="Rain")
    yield
