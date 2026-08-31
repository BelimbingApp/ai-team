import ast
import unittest
from pathlib import Path


TEST_DIRECTORY = Path(__file__).parent


class BashPathContractTest(unittest.TestCase):
    """Keep filesystem-backed shell entry points behind bash_path()."""

    def test_classifier_rejects_the_raw_windows_path_pattern(self):
        raw_conversion = "str" + "(SCRIPT)"
        raw_call = ast.parse(
            f'run_with_bash_path(["bash", {raw_conversion}], '
            'stub_directory=BIN, env=ENV)'
        ).body[0].value
        wrapped_call = ast.parse(
            'run_with_bash_path(["bash", bash_path(SCRIPT)], '
            'stub_directory=BIN, env=ENV)'
        ).body[0].value

        self.assertIsInstance(raw_call, ast.Call)
        self.assertIsInstance(wrapped_call, ast.Call)
        raw_target = self.shell_script_target(raw_call, self.command_list(raw_call))
        wrapped_target = self.shell_script_target(
            wrapped_call,
            self.command_list(wrapped_call),
        )
        self.assertTrue(self.looks_like_filesystem_script(raw_target))
        self.assertFalse(self.is_bash_path_call(raw_target))
        self.assertTrue(self.is_bash_path_call(wrapped_target))

    def test_shell_script_entry_points_use_the_shared_path_boundary(self):
        offenders = []

        for source_path in sorted(TEST_DIRECTORY.glob("test_*.py")):
            source = source_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(source_path))

            for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
                command = self.command_list(call)
                if command is None:
                    continue

                target = self.shell_script_target(call, command)
                if target is None or self.is_bash_path_call(target):
                    continue
                if not self.looks_like_filesystem_script(target):
                    continue

                line = source.splitlines()[target.lineno - 1].strip()
                offenders.append(f"{source_path.name}:{target.lineno}: {line}")

        self.assertEqual(
            offenders,
            [],
            "filesystem-backed Bash entry points must use bash_path(...):\n"
            + "\n".join(offenders),
        )

    @staticmethod
    def command_list(call: ast.Call) -> ast.List | None:
        if not call.args or not isinstance(call.args[0], ast.List):
            return None

        function = call.func
        if isinstance(function, ast.Name) and function.id == "run_with_bash_path":
            return call.args[0]
        if (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and function.value.id == "subprocess"
            and function.attr in {"run", "Popen", "call", "check_call", "check_output"}
        ):
            return call.args[0]
        return None

    @classmethod
    def shell_script_target(cls, call: ast.Call, command: ast.List) -> ast.expr | None:
        elements = command.elts
        if not elements:
            return None

        first = elements[0]
        uses_wrapper = isinstance(call.func, ast.Name) and call.func.id == "run_with_bash_path"
        if cls.is_bash_executable(first):
            for element in elements[1:]:
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    if element.value == "-c":
                        return None
                    if element.value.startswith("-"):
                        continue
                return element
            return None

        return first if uses_wrapper else None

    @staticmethod
    def is_bash_executable(node: ast.expr) -> bool:
        if isinstance(node, ast.Constant):
            return node.value == "bash"
        return isinstance(node, ast.Name) and node.id.lower().endswith("bash")

    @staticmethod
    def is_bash_path_call(node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "bash_path"
        )

    @staticmethod
    def looks_like_filesystem_script(node: ast.expr) -> bool:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return node.func.id == "str"
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value.endswith(".sh") or "/" in node.value or "\\" in node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return True
        if isinstance(node, ast.Attribute):
            return any(
                hint in node.attr.lower()
                for hint in ("script", "hook", "path", "file", "lane")
            )
        if isinstance(node, ast.Name):
            return node.id.isupper() or any(
                hint in node.id.lower()
                for hint in ("script", "hook", "path", "file", "lane")
            )
        return False


if __name__ == "__main__":
    unittest.main()
