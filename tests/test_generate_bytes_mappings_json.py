"""Regression test for generate_bytes_mappings_json handling a null storageLayout.

solc <0.5.13 doesn't emit native storageLayout compiler output — the build JSON
still has the "storageLayout" key, but its value is null rather than absent.
"""

import json

from certora_autosetup.cache.cache_fs import init_cache_fs
from certora_autosetup.setup.setup_prover import SetupProver
from certora_autosetup.utils.scope import Scope


def _make_setup_prover(tmp_path):
    init_cache_fs(str(tmp_path), force=True)
    certora_dir = tmp_path / "certora"
    certora_dir.mkdir()
    return SetupProver(
        log=lambda *args, **kwargs: None,
        certora_dir=certora_dir,
        script_dir=tmp_path,
        additional_contracts=[],
        extra_args=[],
        skip_llm=True,
        force_llm_regenerate=False,
        stop_after_summaries=True,
        scope=Scope(project_root=tmp_path),
    )


def test_generate_bytes_mappings_json_handles_null_storage_layout(tmp_path):
    """A contract compiled with an old solc (storageLayout: null) must not crash the writer."""
    setup_prover = _make_setup_prover(tmp_path)

    build_data = {
        "old_compiler_unit": {
            "contracts": [
                {
                    "name": "OldContract",
                    "file": "src/OldContract.sol",
                    "storageLayout": None,
                }
            ]
        }
    }

    # Must not raise AttributeError: 'NoneType' object has no attribute 'get'.
    setup_prover.generate_bytes_mappings_json(build_data)

    with open(setup_prover.bytes_mappings_cache_path()) as f:
        assert json.load(f) == []


def test_generate_bytes_mappings_json_still_finds_bytes_mapping_fields(tmp_path):
    """A contract with a real storageLayout and a bytes-keyed mapping is still detected."""
    setup_prover = _make_setup_prover(tmp_path)

    build_data = {
        "new_compiler_unit": {
            "contracts": [
                {
                    "name": "NewContract",
                    "file": "src/NewContract.sol",
                    "storageLayout": {
                        "storage": [
                            {
                                "label": "byKey",
                                "descriptor": {
                                    "type": "Mapping",
                                    "mappingKeyType": {"type": "PackedBytes"},
                                },
                            }
                        ]
                    },
                }
            ]
        }
    }

    setup_prover.generate_bytes_mappings_json(build_data)

    with open(setup_prover.bytes_mappings_cache_path()) as f:
        result = json.load(f)

    assert result == [
        {
            "contract_name": "NewContract",
            "source_file": "src/NewContract.sol",
            "bytes_mapping_fields": ["byKey"],
        }
    ]
