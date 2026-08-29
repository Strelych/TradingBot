import time
import json
import adapter


def test_bump_records_only_on_change():
    a = adapter.adapter
    cur = a.kn_conn.cursor()
    # Ensure a known starting state by reading current count
    cur.execute("SELECT COUNT(*) FROM config_versions")
    before = cur.fetchone()[0]
    # Ensure CONFIG exists for the adapter instance
    if a.CONFIG is None:
        a.CONFIG = {"pair_overrides":{}}
    a.CONFIG.setdefault("pair_overrides",{})
    # Set an override unique and bump
    key = f"__test_{int(time.time())}"
    a.CONFIG["pair_overrides"][key] = {"adapter_strategy":"TREND"}
    a.bump("test bump 1")
    cur.execute("SELECT COUNT(*) FROM config_versions")
    after1 = cur.fetchone()[0]
    assert after1 == before + 1
    # Call bump again without changing overrides -> count should not increase
    a.bump("test bump 2")
    cur.execute("SELECT COUNT(*) FROM config_versions")
    after2 = cur.fetchone()[0]
    assert after2 == after1
    # Now change overrides and bump -> count should increase
    a.CONFIG["pair_overrides"][key]["adapter_strategy"] = "SWING"
    a.bump("test bump 3")
    cur.execute("SELECT COUNT(*) FROM config_versions")
    after3 = cur.fetchone()[0]
    assert after3 == after2 + 1
    # cleanup: remove test key
    a.CONFIG["pair_overrides"].pop(key, None)
    a.bump("cleanup")
