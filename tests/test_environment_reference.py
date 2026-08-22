from scripts.generate_env_reference import OUTPUT, catalogued_variables, discovered_variables, render


def test_environment_reference_is_current() -> None:
    assert OUTPUT.read_text(encoding="utf-8") == render()


def test_every_discovered_environment_variable_is_catalogued() -> None:
    assert discovered_variables() == catalogued_variables()
