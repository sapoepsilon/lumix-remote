"""Imported first by every test module: point the config dir at a temp dir so
tests never read or migrate the real ~/.config/lumix-remote."""
import os
import tempfile

if not os.environ.get("LUMIX_TEST_HOME"):
    os.environ["LUMIX_TEST_HOME"] = tempfile.mkdtemp(prefix="lumix-test-")
    os.environ["LUMIX_REMOTE_HOME"] = os.environ["LUMIX_TEST_HOME"]

# The BTLEServer CPU guard (lumix.ble.check_btle_server) runs `ps`; tests must not
# depend on the state of the machine's Bluetooth daemon. tests/test_btle_guard.py
# clears this to test the guard itself.
os.environ.setdefault("LUMIX_BLE_SKIP_BTLE_CHECK", "1")
