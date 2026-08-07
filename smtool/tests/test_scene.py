"""Scene sourcing: Signature.from_scene (all_methods.json shape) and scene.methods_from_build
(deriving facts from certoraRun's .certora_build.json via the reused parse_type_descriptor)."""
import json

from smtool import scene
from smtool.ir import Signature


def test_signature_from_scene():
    m = {"name": "f", "fullSignature": ["uint256", "address"], "paramNames": ["a", "b"],
         "returns": ["uint256"], "stateMutability": "view", "visibility": "external"}
    sig = Signature.from_scene(m, lambda s: s)          # identity resolver (types already CVL strings)
    assert sig.name == "f"
    assert [p.type for p in sig.params] == ["uint256", "address"]
    assert [p.name for p in sig.params] == ["a", "b"]
    assert sig.returns == ["uint256"]
    assert sig.mutability == "view"


def test_signature_from_scene_synthesizes_missing_param_names():
    m = {"name": "g", "fullSignature": ["uint256", "address"], "paramNames": ["a"],  # short
         "returns": [], "stateMutability": "nonpayable", "visibility": "external"}
    sig = Signature.from_scene(m, lambda s: s)
    assert len(sig.params) == 2                          # not dropped by zip
    assert sig.params[0].name == "a"


def test_methods_from_build(tmp_path):
    build = {"unit": {"contracts": [{"name": "C", "allMethods": [
        {"name": "f", "contractName": "C", "paramNames": ["a"],
         "fullArgs": [{"typeDesc": {"type": "Primitive", "primitiveName": "uint256"}, "location": ""}],
         "returns": [{"typeDesc": {"type": "Primitive", "primitiveName": "uint256"}, "location": ""}],
         "stateMutability": "nonpayable", "visibility": "external"},
        {"name": "getA", "contractName": "C", "paramNames": ["a"],
         "fullArgs": [{"typeDesc": {"type": "Primitive", "primitiveName": "uint256"}, "location": ""}],
         "returns": [{"typeDesc": {"type": "Primitive", "primitiveName": "address"}, "location": ""}],
         "stateMutability": "view", "visibility": "external"},
    ]}]}}
    p = tmp_path / "build.json"
    p.write_text(json.dumps(build))
    ms = {m["name"]: m for m in scene.methods_from_build(str(p))}
    assert set(ms) == {"f", "getA"}
    assert ms["f"]["fullSignature"] == ["uint256"] and ms["f"]["returns"] == ["uint256"]
    assert ms["f"]["stateMutability"] == "nonpayable"
    assert ms["getA"]["returns"] == ["address"] and ms["getA"]["stateMutability"] == "view"


def test_methods_from_build_dedups(tmp_path):
    ent = {"name": "f", "contractName": "C", "paramNames": [], "fullArgs": [], "returns": [],
           "stateMutability": "view", "visibility": "external"}
    build = {"a": {"contracts": [{"name": "C", "allMethods": [ent, dict(ent)]}]}}  # same method twice
    p = tmp_path / "b.json"
    p.write_text(json.dumps(build))
    assert len(scene.methods_from_build(str(p))) == 1
