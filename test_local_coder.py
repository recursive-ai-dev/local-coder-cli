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
            local_coder.get_safe_path(self.test_dir, "../../etc/passwd")
            
        with self.assertRaises(ValueError):
            local_coder.get_safe_path(self.test_dir, "/absolute/escaped.txt")

        # Windows-style sequence escape attempt
        with self.assertRaises(ValueError):
            local_coder.get_safe_path(self.test_dir, "..\\..\\etc\\passwd")

        # Symlink escape attempt
        symlink_dir = self.test_dir / "symlink_dir"
        symlink_dir.mkdir()
        symlink_file = symlink_dir / "link"
        try:
            symlink_file.symlink_to("/etc/passwd")
        except Exception:
            pass # ignore symlink failure if no permission
        else:
            with self.assertRaises(ValueError):
                # Wait, get_safe_path just resolves. It resolves symlinks, so it points out of dir
                local_coder.get_safe_path(self.test_dir, "symlink_dir/link")

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

    def test_multiple_tool_calls_ordering(self):
        import textwrap
        llm_response = textwrap.dedent("""\
            <read_file>1.txt</read_file>
            <list_dir>.</list_dir>
            <write_file path="2.txt">content</write_file>
            <patch_file path="3.txt">
            <search>foo</search>
            <replace>bar</replace>
            </patch_file>
        """)
        results = local_coder.parse_and_execute_tools(self.test_dir, llm_response)
        self.assertEqual(len(results), 4)
        self.assertEqual(results[0]["tool"], "read_file")
        self.assertEqual(results[1]["tool"], "list_dir")
        self.assertEqual(results[2]["tool"], "write_file")
        self.assertEqual(results[3]["tool"], "patch_file")

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
        # Tags inside the body of a write_file should be treated as literal content
        llm_response = """
            <write_file path="nested.xml">
            <read_file>some_file.txt</read_file>
            <list_dir>.</list_dir>
            </write_file>
        """
        results = local_coder.parse_and_execute_tools(self.test_dir, llm_response)
        # Should only execute write_file once
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["tool"], "write_file")
        self.assertEqual(results[0]["path"], "nested.xml")

        content = (self.test_dir / "nested.xml").read_text()
        self.assertIn("<read_file>some_file.txt</read_file>", content)
        self.assertIn("<list_dir>.</list_dir>", content)

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


