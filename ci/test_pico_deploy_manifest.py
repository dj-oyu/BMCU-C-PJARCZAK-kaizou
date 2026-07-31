"""The Pico deploy script must ship every module the bridge imports."""
import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PICO = ROOT / "pico"
DEPLOY = PICO / "deploy.ps1"

# Never uploaded by the module loop: templates, and the live credentials file,
# which is gitignored and provisioned separately through -SecretsPath.
NEVER_DEPLOYED = {"secrets.py", "secrets_example.py", "config_example.py"}
# Import targets that are satisfied some other way than the module loop. Held as
# a constant rather than derived from the directory so the test behaves
# identically on a developer machine (where pico/secrets.py exists) and in CI
# (where it does not).
PROVISIONED_SEPARATELY = {"secrets"}


def local_modules():
    return {path.stem for path in PICO.glob("*.py")} | PROVISIONED_SEPARATELY


def excluded_by_script():
    """The $excluded list the script filters the directory glob by."""
    text = DEPLOY.read_text(encoding="utf-8")
    match = re.search(r"\$excluded\s*=\s*@\(([^)]*)\)", text)
    if match is None:
        raise AssertionError(
            "pico/deploy.ps1 no longer defines an $excluded list; if the upload "
            "set is chosen some other way, update this test to match it"
        )
    return set(re.findall(r"'([^']+)'", match.group(1)))


def imports_of(module, known):
    """Module-level and function-local imports of sibling pico modules."""
    path = PICO / (module + ".py")
    if not path.is_file():
        return set()  # provisioned separately, not part of the repo
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module in known:
                found.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in known:
                    found.add(alias.name)
    return found


def reachable_from(entry, known):
    """Transitive closure of sibling imports starting at `entry`."""
    seen = set()
    pending = [entry]
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        pending.extend(imports_of(module, known) - seen)
    return seen


class DeployManifestTests(unittest.TestCase):
    def test_script_derives_the_upload_set_from_the_directory(self):
        text = DEPLOY.read_text(encoding="utf-8")
        self.assertIn("Get-ChildItem", text,
                      "deploy.ps1 must enumerate pico/*.py, not hardcode a list "
                      "that can go stale when a module is added")

    def test_every_module_the_bridge_imports_is_uploaded(self):
        known = local_modules()
        required = reachable_from("main", known) - PROVISIONED_SEPARATELY
        uploaded = {path.stem for path in PICO.glob("*.py")
                    if path.name not in excluded_by_script()}
        missing = sorted(required - uploaded)
        self.assertEqual(missing, [], "modules imported by the bridge but not "
                                      "uploaded by deploy.ps1: " + repr(missing))

    def test_secrets_is_provisioned_through_the_secrets_path_switch(self):
        # It is excluded from the module loop, so the only remaining route must
        # still exist - otherwise wifi credentials never reach the device.
        text = DEPLOY.read_text(encoding="utf-8")
        self.assertIn("SecretsPath", text)
        self.assertRegex(text, r"':secrets\.py'")

    def test_binary_runtime_modules_are_covered(self):
        required = reachable_from("main", local_modules())
        for module in ("bambuddy_binary_tcp", "bmcu_binary_outbox",
                       "bmcu_journal", "device_key_store",
                       "device_metrics", "runtime_log"):
            self.assertIn(module, required,
                          module + " is expected to be reachable from main.py")

    def test_secrets_are_never_in_the_upload_set(self):
        excluded = excluded_by_script()
        for name in NEVER_DEPLOYED:
            self.assertIn(name, excluded,
                          name + " must not be uploaded by the module loop")

    def test_main_is_uploaded_last(self):
        text = DEPLOY.read_text(encoding="utf-8")
        self.assertRegex(
            text, r"\$files\s*\+=\s*'main\.py'",
            "main.py must be appended last so an interrupted deploy leaves the "
            "old entry point rather than one importing absent modules")


if __name__ == "__main__":
    unittest.main()
