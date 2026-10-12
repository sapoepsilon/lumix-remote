"""Imported first by every test module: point the config dir at a temp dir so
tests never read or migrate the real ~/.config/lumix-remote."""
import os
import tempfile

if not os.environ.get("LUMIX_TEST_HOME"):
    os.environ["LUMIX_TEST_HOME"] = tempfile.mkdtemp(prefix="lumix-test-")
    os.environ["LUMIX_REMOTE_HOME"] = os.environ["LUMIX_TEST_HOME"]
