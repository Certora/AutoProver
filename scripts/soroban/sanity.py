import json
import jinja2
from sys import argv

environment = jinja2.Environment()

with open(argv[2]) as t:
  template = environment.from_string(t.read())

  with open(argv[1]) as f:
    d = json.load(f)
    s = set()
    for o in d['functions']:
      for p in o['parameters']:
        s.add( p['type_detail']['type'] )

    for t in s:
      print("use cvlr_soroban::nondet_" + t + ";")
    print("use cvlr_soroban_derive::rule;")
    print("use soroban_sdk::Env;")
    print()
          
    for o in d['functions']:
      print(template.render(arg=o))
