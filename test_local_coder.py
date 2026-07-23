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

        import time
        time.sleep(0.01)

        file2 = sessions_dir / "2.json"
        file2.write_text("[]")

        # Test LATEST
        resolved = local_coder.resolve_session_file(self.test_dir, "LATEST")
        self.assertEqual(resolved, file2)

        # Test specific filename in directory
        resolved = local_coder.resolve_session_file(self.test_dir, "1.json")
        self.assertEqual(resolved, file1)

        # Test full path (using relative path from target_dir to be safe)
        resolved = local_coder.resolve_session_file(self.test_dir, ".local-coder/sessions/1.json")
        self.assertEqual(resolved, file1)

        # Test out of bounds
        resolved = local_coder.resolve_session_file(self.test_dir, "/absolute/path/file.json")
        self.assertIsNone(resolved)

        resolved = local_coder.resolve_session_file(self.test_dir, "../out_of_bounds.json")
        self.assertIsNone(resolved)

        # Test None
        resolved = local_coder.resolve_session_file(self.test_dir, None)
        self.assertIsNone(resolved)

if __name__ == "__main__":
    unittest.main()
