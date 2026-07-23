import unittest
import tempfile
import shutil
import time
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

    def test_save_load_session(self):
        session_file = self.test_dir / ".local-coder" / "sessions" / "test_session.json"
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "World"},
        ]
        local_coder.save_session(session_file, messages)
        self.assertTrue(session_file.exists())

        loaded = local_coder.load_session(session_file)
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["role"], "user")
        self.assertEqual(loaded[1]["role"], "assistant")
        self.assertEqual(loaded[1]["content"], "World")

    def test_resolve_session_file(self):
        sessions_dir = self.test_dir / ".local-coder" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        file1 = sessions_dir / "1.json"
        file1.write_text("[]")
        time.sleep(0.01)
        file2 = sessions_dir / "2.json"
        file2.write_text("[]")

        resolved = local_coder.resolve_session_file(self.test_dir, "LATEST")
        self.assertEqual(resolved, file2)

        resolved = local_coder.resolve_session_file(self.test_dir, "1.json")
        self.assertEqual(resolved, file1)

        resolved = local_coder.resolve_session_file(self.test_dir, ".local-coder/sessions/1.json")
        self.assertEqual(resolved, file1)

        resolved = local_coder.resolve_session_file(self.test_dir, "/absolute/path/file.json")
        self.assertIsNone(resolved)

        resolved = local_coder.resolve_session_file(self.test_dir, "../out_of_bounds.json")
        self.assertIsNone(resolved)

        resolved = local_coder.resolve_session_file(self.test_dir, None)
        self.assertIsNone(resolved)

    def test_trim_messages_context(self):
        messages = [{"role": "system", "content": "You are a helpful bot."}]
        for i in range(10):
            messages.append({"role": "user", "content": f"User {i}"})
            messages.append({"role": "assistant", "content": f"Bot {i}"})
        self.assertEqual(len(messages), 21)

        local_coder.trim_messages_context(messages, max_history=5)

        self.assertEqual(len(messages), 5)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[-4]["content"], "User 8")
        self.assertEqual(messages[-3]["content"], "Bot 8")
        self.assertEqual(messages[-2]["content"], "User 9")
        self.assertEqual(messages[-1]["content"], "Bot 9")

    def test_trim_messages_context_preserves_minimum_length(self):
        messages = [{"role": "system", "content": "You are a helpful bot."},
                    {"role": "user", "content": "A"},
                    {"role": "assistant", "content": "B"},
                    {"role": "user", "content": "C"},
                    {"role": "assistant", "content": "D"}]

        local_coder.trim_messages_context(messages, max_history=2)
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["content"], "C")
        self.assertEqual(messages[2]["content"], "D")

    def test_tool_search_files(self):
        (self.test_dir / "dir").mkdir()
        local_coder.tool_write_file(self.test_dir, "file1.txt", "apple\nbanana\ncherry\n")
        local_coder.tool_write_file(self.test_dir, "dir/file2.txt", "date\napple\nfig\n")

        res1 = local_coder.tool_search_files(self.test_dir, "apple", ".")
        self.assertIn("file1.txt:1:apple", res1)
        self.assertIn("dir/file2.txt:2:apple", res1)

        res2 = local_coder.tool_search_files(self.test_dir, "apple", "dir")
        self.assertNotIn("file1.txt", res2)
        self.assertIn("dir/file2.txt:2:apple", res2)

        large_file = self.test_dir / "large.txt"
        with open(large_file, "wb") as f:
            f.write(b"apple\n" * 100000)
        res3 = local_coder.tool_search_files(self.test_dir, "apple", ".")
        self.assertNotIn("large.txt", res3)
        large_file.unlink()

        res4 = local_coder.tool_search_files(self.test_dir, "[invalid", ".")
        self.assertTrue(res4.startswith("Error: Invalid regex"))

        local_coder.tool_write_file(self.test_dir, "many.txt", "match\n" * 250)
        res5 = local_coder.tool_search_files(self.test_dir, "match", "many.txt")
        self.assertIn("... [50 more items hidden]", res5)

        res6 = local_coder.tool_search_files(self.test_dir, "xyz123", ".")
        self.assertEqual(res6, "No matches found.")

    def test_search_files_tag_parsing(self):
        llm_response = '<search_files path=".">apple</search_files>'
        local_coder.tool_write_file(self.test_dir, "f.txt", "apple\n")
        results = local_coder.parse_and_execute_tools(self.test_dir, llm_response)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["tool"], "search_files")
        self.assertIn("f.txt:1:apple", results[0]["result"])

    def test_tool_list_dir_gitignore_filtering(self):
        local_coder.tool_write_file(self.test_dir, ".gitignore", "ignored_dir/\n*.pyc\n")
        (self.test_dir / ".git").mkdir()
        (self.test_dir / "ignored_dir").mkdir()
        local_coder.tool_write_file(self.test_dir, "ignored_dir/hidden.txt", "content")
        local_coder.tool_write_file(self.test_dir, "test.pyc", "binary")
        local_coder.tool_write_file(self.test_dir, "visible.txt", "content")

        list_res = local_coder.tool_list_dir(self.test_dir, ".")
        self.assertIn("[FILE] visible.txt", list_res)
        self.assertNotIn("[DIR]  .git", list_res)
        self.assertNotIn("[DIR]  ignored_dir", list_res)
        self.assertNotIn("[FILE] test.pyc", list_res)

        list_res_ignored = local_coder.tool_list_dir(self.test_dir, ".", show_ignored=True)
        self.assertIn("[DIR]  .git", list_res_ignored)
        self.assertIn("[DIR]  ignored_dir", list_res_ignored)
        self.assertIn("[FILE] test.pyc", list_res_ignored)

    def test_windows_style_path_traversal(self):
        with self.assertRaises(ValueError):
            local_coder.get_safe_path(self.test_dir, "..\\..\\etc\\passwd")

    def test_multiple_tool_calls_ordering(self):
        import textwrap
        llm_response = textwrap.dedent("""\
            <read_file>1.txt</read_file>
            <list_dir>.</list_dir>
            <delete_file>2.txt</delete_file>
        """)
        results = local_coder.parse_and_execute_tools(self.test_dir, llm_response)
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0]["tool"], "read_file")
        self.assertEqual(results[1]["tool"], "list_dir")
        self.assertEqual(results[2]["tool"], "delete_file")

    def test_malformed_tags_ignored(self):
        llm_response = """
            Here is a malformed tag:
            <write_file path="test.txt">
            Content without closing tag

            And an unclosed read:
            <read_file>missing_close.txt
        """
        results = local_coder.parse_and_execute_tools(self.test_dir, llm_response)
        self.assertEqual(len(results), 0)

    def test_nested_tags_ignored(self):
        # Tags inside the body of a write_file should be treated as literal
        # content, not re-parsed as their own tool calls.
        local_coder._YOLO_MODE = True
        try:
            llm_response = """
                <write_file path="nested.xml">
                <read_file>some_file.txt</read_file>
                <list_dir>.</list_dir>
                </write_file>
            """
            results = local_coder.parse_and_execute_tools(self.test_dir, llm_response)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["tool"], "write_file")
            self.assertEqual(results[0]["path"], "nested.xml")

            content = (self.test_dir / "nested.xml").read_text()
            self.assertIn("<read_file>some_file.txt</read_file>", content)
            self.assertIn("<list_dir>.</list_dir>", content)
        finally:
            local_coder._YOLO_MODE = False

if __name__ == "__main__":
    unittest.main()


