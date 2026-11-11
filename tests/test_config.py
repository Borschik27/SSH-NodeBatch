from nodebatch.i18n import tr
import copy
import shlex
import pytest
import yaml
from nodebatch.config import (ConfigError, ConfigStore, atomic_write, bootstrap, load_bundle,
                            parse_yaml, validate_inventory, validate_scenario, validate_settings)
from nodebatch.models import Host, Job, Step


def test_bootstrap_preserves_edits_and_deleted_example(tmp_path):
    bootstrap(tmp_path)
    inventory = tmp_path / "inventory.yaml"
    inventory.write_text("version: 1\nhosts: [{alias: node, os: freebsd}]\n", encoding="utf-8")
    (tmp_path / "scenarios/system-info.yaml").unlink()
    bootstrap(tmp_path)
    bundle = load_bundle(tmp_path)
    assert bundle["hosts"][0].os == "freebsd"
    assert not bundle["scenarios"]


@pytest.mark.parametrize("text", ["a: 1\na: 2", "a: [", "- bad", "a: !!python/object:foo {}"])
def test_reject_bad_yaml(text):
    with pytest.raises(ValueError):
        parse_yaml(text)


@pytest.mark.parametrize("row", [{"alias": "-oProxyCommand=x"}, {"alias": "a", "port": True},
                                  {"alias": "a", "shell": "sh;bad"}, {"alias": "a", "os": "windows"},
                                  {"alias": "a", "address": "host;touch /tmp/x"}])
def test_inventory_validation(row):
    with pytest.raises(ValueError):
        validate_inventory({"version": 1, "hosts": [row]})


def test_settings_limits(bundle):
    for key, value in (("parallelism", 0), ("parallelism", True), ("step_timeout", -1)):
        settings = copy.deepcopy(bundle["settings"])
        settings["execution"][key] = value
        with pytest.raises(ValueError):
            validate_settings(settings)


def scenario(command="printf '%s' {{value}}"):
    return {"id": "demo", "name": "Demo", "os": ["unix"], "shells": ["sh"],
            "parameters": {"value": {"secret": False}}, "steps": [{"name": "One", "command": command}]}


def test_parameter_injection_is_one_shell_word():
    parsed = validate_scenario(scenario())
    value = "name'; touch /tmp/SHOULD_NOT_EXIST; #"
    steps, secrets = parsed.render({"value": value})
    assert shlex.split(steps[0].command) == ["printf", "%s", value]
    assert not secrets


@pytest.mark.parametrize("command", ["echo '{{value}}'", 'echo "{{value}}"', "echo prefix{{value}}"])
def test_reject_parameters_embedded_in_quotes(command):
    with pytest.raises(ValueError):
        validate_scenario(scenario(command))


def test_job_is_snapshot(bundle):
    job = Job.create([Host("a")], [Step("x", "true")], bundle, ())
    bundle["settings"]["execution"]["parallelism"] = 99
    bundle["safety"]["rules"].clear()
    assert job.config["settings"]["execution"]["parallelism"] == 4
    assert job.config["safety"]["rules"]


def test_hot_reload_keeps_last_good(qapp, tmp_path, wait):
    store = ConfigStore(tmp_path)
    errors, changes = [], []
    store.invalid.connect(errors.append)
    store.changed.connect(changes.append)
    old = copy.deepcopy(store.bundle)
    atomic_write(tmp_path / "settings.yaml", "broken: [")
    assert not store.reload()
    assert errors and store.bundle == old
    settings = old["settings"]
    settings["execution"]["parallelism"] = 7
    atomic_write(tmp_path / "settings.yaml", yaml.safe_dump(settings))
    assert store.reload()
    assert store.bundle["settings"]["execution"]["parallelism"] == 7
    assert changes
    store.poll.stop()
    store.debounce.stop()


def test_parameterized_heredoc_is_rejected():
    with pytest.raises(ValueError, match="heredoc"):
        validate_scenario(scenario("cat <<EOF\n{{value}}\nEOF"))


def test_secret_defaults_are_not_persistable():
    definition = scenario()
    definition["parameters"]["value"]["secret"] = True
    definition["parameters"]["value"]["default"] = "not allowed"
    with pytest.raises(ValueError, match="default"):
        validate_scenario(definition)


@pytest.mark.parametrize("sudo", [False, True])
@pytest.mark.parametrize("value", ["synthetic-private-value", ""])
def test_secret_substitution_blocked_before_render(sudo, value):
    definition = scenario()
    definition["parameters"]["value"] = {"secret": True, "required": False}
    definition["steps"][0]["sudo"] = sudo
    parsed = validate_scenario(definition)
    with pytest.raises(ValueError, match=tr('secure step.secrets channel')) as caught:
        parsed.render({"value": value})
    assert not value or value not in str(caught.value)


def test_direct_scenario_cannot_bypass_secret_guard():
    from nodebatch.models import Scenario
    parsed = Scenario("demo", "Demo", "", ("unix",), ("sh",),
                      {"value": {"secret": True}}, (Step("One", "echo {{value}}"),))
    with pytest.raises(ValueError, match=tr('secure step.secrets channel')):
        parsed.render({"value": "synthetic-private-value"})
