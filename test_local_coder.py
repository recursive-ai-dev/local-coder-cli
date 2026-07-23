import unittest
import tempfile
import shutil
from pathlib import Path
import local_coder

class TestLocalCoder(unittest.TestCase):
    def setUp(self):
        # Create a temporary target directory for testing
        self.test_dir = Path(tempfile.mkdtemp())
        
    def tearDown(self):
        # Clean up temporary directory
        shutil.rmtree(self.test_dir)
        
    def test_load_agent_config(self):
        import json
        agents_dir = self.test_dir / ".local-coder" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        config_data = {
            "system_prompt": "Test agent prompt",
            "allowed_tools": ["read_file", "list_dir"]
        }
        (agents_dir / "test_agent.json").write_text(json.dumps(config_data), encoding="utf-8")

        config = local_coder.load_agent_config(self.test_dir, "test_agent")
        self.assertEqual(config["system_prompt"], "Test agent prompt")
        self.assertEqual(config["allowed_tools"], ["read_file", "list_dir"])

        with self.assertRaises(FileNotFoundError):
            local_coder.load_agent_config(self.test_dir, "missing_agent")

    def test_safe_path_resolution(self):
        # Normal resolution
        path = local_coder.get_safe_path(self.test_dir, "subdir/file.txt")
        self.assertEqual(path.parent.name, "subdir")
        
        # Path escape detection (security constraint)
        with self.assertRaises(ValueError):
            local_coder.get_safe_path(self.test_dir, "../escaped.txt")
            
        with self.assertRaises(ValueError):
            local_coder.get_safe_path(self.test_dir, "/absolute/escaped.txt")

    def test_tool_write_and_read(self):
        # Write file
        write_res = local_coder.tool_write_file(self.test_dir, "test.txt", "Hello World")
        self.assertIn("Successfully wrote", write_res)
        
        # Verify file actually created and content is correct
        file_path = self.test_dir / "test.txt"
        self.assertTrue(file_path.exists())
        self.assertEqual(file_path.read_text(), "Hello World")
        
        # Read file
        read_res = local_coder.tool_read_file(self.test_dir, "test.txt")
        self.assertEqual(read_res, "Hello World")

    def test_tool_list_dir(self):
        # Create files & folders
        local_coder.tool_write_file(self.test_dir, "a.txt", "content")
        (self.test_dir / "sub").mkdir()
        local_coder.tool_write_file(self.test_dir, "sub/b.txt", "content")
        
        # List root
        list_res = local_coder.tool_list_dir(self.test_dir, ".")
        self.assertIn("[FILE] a.txt", list_res)
        self.assertIn("[DIR]  sub", list_res)
        
        # List sub
        list_sub_res = local_coder.tool_list_dir(self.test_dir, "sub")
        self.assertIn("[FILE] sub/b.txt", list_sub_res)

    def test_tool_patch_file(self):
        content = "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b"
        local_coder.tool_write_file(self.test_dir, "math_utils.py", content)
        
        # Patch
        patch_res = local_coder.tool_patch_file(
            self.test_dir, 
            "math_utils.py", 
            "def sub(a, b):\n    return a - b", 
            "def sub(a, b):\n    # Subtract b from a\n    return a - b"
        )
        self.assertIn("Successfully applied patch", patch_res)
        self.assertIn("--- math_utils.py", patch_res)
        self.assertIn("+++ math_utils.py", patch_res)
        
        patched_content = local_coder.tool_read_file(self.test_dir, "math_utils.py")
        self.assertIn("# Subtract b from a", patched_content)

        # Multi-match
        local_coder.tool_write_file(self.test_dir, "math_utils_2.py", "def a():\n  pass\n\ndef a():\n  pass")
        patch_res2 = local_coder.tool_patch_file(self.test_dir, "math_utils_2.py", "def a():\n  pass", "def a():\n  return 1")
        self.assertIn("Error: Search block found multiple times", patch_res2)

    def test_tool_delete_file(self):
        # Create a file
        local_coder.tool_write_file(self.test_dir, "delete_me.txt", "content")
        self.assertTrue((self.test_dir / "delete_me.txt").exists())

        # Delete it
        res = local_coder.tool_delete_file(self.test_dir, "delete_me.txt")
        self.assertIn("Successfully deleted", res)
        self.assertFalse((self.test_dir / "delete_me.txt").exists())

        # Path traversal should return error rather than throwing, as caught by the function's try-except block
        # Actually wait, ValueError is raised by get_safe_path, then caught by the try-except in tool_delete_file
        # and returned as string.
        res2 = local_coder.tool_delete_file(self.test_dir, "../out_of_bounds.txt")
        self.assertIn("Error deleting file:", res2)
        self.assertIn("escapes target directory", res2)

    def test_tool_move_file(self):
        # Create a file
        local_coder.tool_write_file(self.test_dir, "move_src.txt", "content")
        self.assertTrue((self.test_dir / "move_src.txt").exists())

        # Move it
        res = local_coder.tool_move_file(self.test_dir, "move_src.txt", "subdir/move_dst.txt")
        self.assertIn("Successfully moved", res)
        self.assertFalse((self.test_dir / "move_src.txt").exists())
        self.assertTrue((self.test_dir / "subdir" / "move_dst.txt").exists())

        # Path traversal
        res2 = local_coder.tool_move_file(self.test_dir, "subdir/move_dst.txt", "../escaped.txt")
        self.assertIn("Error moving file:", res2)
        self.assertIn("escapes target directory", res2)

    def test_parse_and_execute_tools(self):
        import textwrap
        # Set YOLO mode to true for original test behavior
        local_coder._YOLO_MODE = True

        # We simulate the LLM's response containing multiple tool blocks
        llm_response = textwrap.dedent("""\
            I will create a script and verify it.
            <write_file path="script.py" lang="python">
            print("Hello CLI")
            </write_file>
            
            Now let's read it.
            <read_file>script.py</read_file>
        """)
        results = local_coder.parse_and_execute_tools(self.test_dir, llm_response)
        
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["tool"], "write_file")
        self.assertEqual(results[0]["path"], "script.py")
        self.assertIn("Successfully wrote", results[0]["result"])
        
        self.assertEqual(results[1]["tool"], "read_file")
        self.assertEqual(results[1]["path"], "script.py")
        self.assertEqual(results[1]["result"], 'print("Hello CLI")\n')
        local_coder._YOLO_MODE = False

    def test_ask_user_confirmation_console_deny(self):
        import textwrap
        from unittest.mock import patch

        local_coder._YOLO_MODE = False
        local_coder.TUI_MODE = False

        llm_response = textwrap.dedent("""\
            <write_file path="evil.py" lang="python">
            print("pwned")
            </write_file>
        """)

        with patch("local_coder.Confirm.ask", return_value=False):
            results = local_coder.parse_and_execute_tools(self.test_dir, llm_response)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["tool"], "write_file")
        self.assertEqual(results[0]["path"], "evil.py")
        self.assertEqual(results[0]["result"], "Error: User denied permission to write file.")

        # Verify file was NOT created
        file_path = self.test_dir / "evil.py"
        self.assertFalse(file_path.exists())

    def test_ask_user_confirmation_console_allow(self):
        import textwrap
        from unittest.mock import patch

        local_coder._YOLO_MODE = False
        local_coder.TUI_MODE = False

        llm_response = textwrap.dedent("""\
            <write_file path="good.py" lang="python">
            print("good")
            </write_file>
        """)

        with patch("local_coder.Confirm.ask", return_value=True):
            results = local_coder.parse_and_execute_tools(self.test_dir, llm_response)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["tool"], "write_file")
        self.assertEqual(results[0]["path"], "good.py")
        self.assertIn("Successfully wrote", results[0]["result"])

        # Verify file WAS created
        file_path = self.test_dir / "good.py"
        self.assertTrue(file_path.exists())

    def test_spawn_agent_parsing(self):
        llm_response = """\
        Let's run a subtask.
        <spawn_agent name="helper">
        Analyze the directory.
        </spawn_agent>
        """
        def mock_runner(name, task):
            return f"Mock ran {name} with task {task}"

        results = local_coder.parse_and_execute_tools(self.test_dir, llm_response, subagent_runner=mock_runner)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["tool"], "spawn_agent")
        self.assertEqual(results[0]["path"], "helper")
        self.assertEqual(results[0]["result"], "Mock ran helper with task Analyze the directory.")

    def test_tool_filtering(self):
        llm_response = """\
        <write_file path="x.txt">test</write_file>
        <read_file>x.txt</read_file>
        """
        results = local_coder.parse_and_execute_tools(self.test_dir, llm_response, allowed_tools=["read_file"])
        self.assertEqual(len(results), 2)

        # write_file should be blocked
        self.assertEqual(results[0]["tool"], "write_file")
        self.assertIn("not permitted", results[0]["result"])

        # read_file should attempt to run (and fail since x.txt wasn't written)
        self.assertEqual(results[1]["tool"], "read_file")
        self.assertIn("Error: File 'x.txt' does not exist.", results[1]["result"])

    def test_provider_detection_fallback(self):
        # When no endpoints are active, detect_provider returns (None, None)
        prov, url = local_coder.detect_provider()
        self.assertEqual(prov, None)
        self.assertEqual(url, None)

    def test_models_integration(self):
        # Check presets exist
        self.assertIn("1", local_coder.MODELS_PRESETS)
        self.assertEqual(local_coder.MODELS_PRESETS["1"]["name"], "Llama 3.1 8B Instruct")
        
        # Check models directory creation and path
        m_dir = local_coder.get_models_dir()
        self.assertTrue(m_dir.exists())
        self.assertTrue(m_dir.is_dir())

if __name__ == "__main__":
    unittest.main()


