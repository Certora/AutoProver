import sys
import tomlkit
from pathlib import Path
import shutil
import re

from classify_cargo import process as classify

def add_stuff(inlineTable, dict):
  for k in dict: inlineTable[k] = dict[k]
  return inlineTable


def put_stuff(dict):
  return add_stuff(tomlkit.inline_table(), dict)


def fix_profile(t):
  if "profile" not in t:
    t["profile"] = {}
            
  if "release" not in t["profile"]:
    t["profile"]["release"] = {}

  t["profile"]["release"]["opt-level"] = 2
  t["profile"]["release"]["debug"] = 2
  t["profile"]["release"]["strip"] = "none"


def fix_soroban_sdk(t):
  features = ["alloc"]
  if (sdk_version >= (23, 0, 0)):
    features.append("hazmat-address")
  if (sdk_version >= (25, 2, 0)):
    features.append("experimental_spec_shaking_v2")
  dstl = { "version": sdk_version_string,
           "default-features": False,
           "features": features}
  if "dependencies" in t:
    x = t["dependencies"]
    x["soroban-sdk"] = put_stuff(dstl)
  else:
    t["dependencies"] = tomlkit.inline_table()
    t["dependencies"]["soroban-sdk"] = put_stuff(dstl)

      
def get_version_tuple(y):
  if y is not None:
    return tuple(int(x) for x in y.split('.'))
  else:
    return None


def get_version_string(obj):
  if "soroban-sdk" in obj:
    sdk = obj["soroban-sdk"]
    if isinstance(sdk, str):
      return sdk
    elif "version" in sdk:
      return sdk["version"]

  return None


def get_version(obj):
  y = get_version_tuple(get_version_string(obj))


def check_muxed_address():
  vs = sdk_version
  if vs is not None:
    if vs[0] >= 23:
      print(vs.__str__() + " has MuxedAddress")
      return True
    else:
      print(vs.__str__() + " doesn't have MuxedAddress")
      return False


def inherit_cvlr_stuff(t):
  t["dependencies"]["soroban-sdk"] = put_stuff( { "workspace": True, "default-features": False })
    
  t["dependencies"]["cvlr"] = put_stuff({ "workspace": True, "default-features": False })
  t["dependencies"]["cvlr-soroban"] = put_stuff({ "workspace": True, "default-features": False })
  t["dependencies"]["cvlr-soroban-derive"] = put_stuff({ "workspace": True, "default-features": False })

  
def put_dependencies(t):
  t["dependencies"]["cvlr"] = put_stuff({"git": "https://github.com/Certora/cvlr", "branch": "0.6.1-soroban-changes", "default-features": False})
  
  if check_muxed_address():
    t["dependencies"]["cvlr-soroban"] = put_stuff({ "path": "../cvlr-soroban/cvlr-soroban", "default-features": False })
  else:
    t["dependencies"]["cvlr-soroban"] = put_stuff({ "path": "../cvlr-soroban/cvlr-soroban", "default-features": False, "features": ["nomuxedaddress"] })

  t["dependencies"]["cvlr-soroban-derive"] = put_stuff({ "path": "../cvlr-soroban/cvlr-soroban-derive", "default-features": False })


def ensure_cvlr_soroban(obj, from_path, to_path):
  to_path = Path(to_path).parent / "cvlr-soroban"
  if not to_path.exists():
    shutil.copytree(Path(from_path).parent, to_path, dirs_exist_ok=False)
    cstomln = to_path / "Cargo.toml"
    with open( cstomln ) as cstomlf:
      cstoml = tomlkit.parse(cstomlf.read())
      print(cstoml["workspace"]["dependencies"])
      if not sdk_version == get_version(cstoml["workspace"]["dependencies"]):
        if get_version_string(obj) is not None:
          cstoml["workspace"]["dependencies"]["soroban-sdk"] = put_stuff({ "version": sdk_version_string, "default-features": False})
        with open(cstomln, "w") as f:
          tomlkit.dump(cstoml, f)


def remove_test_projects(t, regex):
  members = t["workspace"]["members"]
  members = [m for m in members if not re.match(regex, m)]
  t["workspace"]["members"] = members

  
sdk_version_string=sys.argv[3]
sdk_version=get_version_tuple(sdk_version_string)
    
tomls = classify(Path(sys.argv[1]))

with open( sys.argv[2] ) as cvlrsf:
  cvlrs = tomlkit.parse(cvlrsf.read())
  for cargo in tomls:
    print(cargo)
    with open( cargo ) as f:
      t = tomlkit.parse(f.read())
      match tomls[cargo]["category"]:
        case "WORKSPACE_ROOT":
          fix_profile(t)
          fix_soroban_sdk(t["workspace"])
          put_dependencies(t["workspace"])
          ensure_cvlr_soroban(t["workspace"]["dependencies"], sys.argv[2], sys.argv[1])
          remove_test_projects(t,  r"tests?/")
          remove_test_projects(t,  r'fuzz(?: targets)?/')

        case "UNRELATED_STANDALONE":
          fix_profile(t)
          fix_soroban_sdk(t)
          put_dependencies(t)
          ensure_cvlr_soroban(t["dependencies"], sys.argv[2], sys.argv[1]) 
          
        case "MEMBER":
          inherit_cvlr_stuff(t)
          
    with open(cargo, "w") as f:
        tomlkit.dump(t, f)

