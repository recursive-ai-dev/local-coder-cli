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
        
        patched_content = local_coder.tool_read_file(self.test_dir, "math_utils.py")
        self.assertIn("# Subtract b from a", patched_content)

        # Multi-match
        local_coder.tool_write_file(self.test_dir, "math_utils_2.py", "def a():\n  pass\n\ndef a():\n  pass")
        patch_res2 = local_coder.tool_patch_file(self.test_dir, "math_utils_2.py", "def a():\n  pass", "def a():\n  return 1")
        self.assertIn("Error: Search block found multiple times", patch_res2)

    def test_parse_and_execute_tools(self):
        import textwrap
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

    def test_run_agent_loop_max_history(self):
        messages = [{"role": "system", "content": "Sys prompt"}]
        for _ in range(30):
            messages.append({"role": "user", "content": "Msg"})

        class FakeClient:
            pass

        # Fake stream_completion that just returns "Agent thought"
        import sys
        original_stream = local_coder.stream_completion
        original_parse = local_coder.parse_and_execute_tools
        try:
            local_coder.stream_completion = lambda c, m, msgs, cb: "Agent thought"
            local_coder.parse_and_execute_tools = lambda d, r: [{"tool": "read_file", "path": "x", "result": "content"}]
            local_coder.run_agent_loop(FakeClient(), "m", self.test_dir, messages, max_iterations=5, max_history=10)
            self.assertLessEqual(len(messages), 10)
            self.assertEqual(messages[0]["content"], "Sys prompt")
        finally:
            local_coder.stream_completion = original_stream
            local_coder.parse_and_execute_tools = original_parse

    def test_trim_messages_context(self):
        # Initial context with system prompt and some turns
        messages = [{"role": "system", "content": "You are a helpful bot."}]
        for i in range(10):
            messages.append({"role": "user", "content": f"User {i}"})
            messages.append({"role": "assistant", "content": f"Bot {i}"})

        # We have 1 system + 20 message pairs = 21 items.
        self.assertEqual(len(messages), 21)

        # Test trimming to a max of 5
        local_coder.trim_messages_context(messages, max_history=5)

        # Expect length to drop to 5
        self.assertEqual(len(messages), 5)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], "You are a helpful bot.")

        # The remaining 4 items should be the latest ones.
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

        # Trimming with an impossibly small max history should clamp to min 3
        local_coder.trim_messages_context(messages, max_history=2)
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["content"], "C")
        self.assertEqual(messages[2]["content"], "D")

if __name__ == "__main__":
    unittest.main()
