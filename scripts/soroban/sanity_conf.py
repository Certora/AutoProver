from jinja2 import Template
import json
import sys

with open( sys.argv[2] ) as tf:
    template_string = tf.read()
    template = Template(template_string)


with open( sys.argv[1] ) as sf:
    ss = json.load(sf)

    for contract in ss["contracts"]:

        if "/fuzz/" not in contract["file"]:
            result = template.render(crate=contract["crate"], functions=contract["functions"], name=contract["name"])
            
            with open( "conf/" + contract["name"] + "_sanity.conf", "w", encoding="utf-8" ) as cf:
                cf.write(result)

