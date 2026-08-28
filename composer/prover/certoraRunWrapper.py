#      The Certora Prover
#      Copyright (C) 2025  Certora Ltd.
#
#      This program is free software: you can redistribute it and/or modify
#      it under the terms of the GNU General Public License as published by
#      the Free Software Foundation, version 3 of the License.
#
#      This program is distributed in the hope that it will be useful,
#      but WITHOUT ANY WARRANTY; without even the implied warranty of
#      MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
#      GNU General Public License for more details.
#
#      You should have received a copy of the GNU General Public License
#      along with this program.  If not, see <https://www.gnu.org/licenses/>.

import sys
import json
import os

from typing import Literal, TypedDict, Annotated

class ProverSuccess(TypedDict):
    sort: Literal["success"]
    is_local_link: bool
    link: str | None

class ProverFailure(TypedDict):
    sort: Literal["failure"]
    exc_str: str

"""
This is a wrapper script which sandboxes an invocation of a Certora Prover CLI
while still allowing access to the structured return type.

The structured data is returned via the first argument, which is a temp file
into which the serialized result is written (either None or a CertoraRunResult).

The second argument names which Prover this run submits to (`evm`, `solana`,
`soroban`); each is a separate certora_cli entry point with its own build step,
and they share the `list[str] -> CertoraRunResult | None` signature this wrapper
depends on.

If the run throws an exception, it is caught, and the serialized representation of the
exception is written to the same file.

All other arguments past the second are passed through to that entry point.
"""

os.putenv("DONT_USE_VERIFICATION_RESULTS_FOR_EXITCODE", "1")

output = sys.argv[1]

from composer.certora_env import import_prover_entry, prover_app
run_prover_cli = import_prover_entry(prover_app(sys.argv[2]))

with open(output, 'w') as out:
    try:
        r = run_prover_cli(sys.argv[3:])
        if r is None:
            json.dump(None, out)
        else:
            succ : ProverSuccess = {
                "sort": "success",
                "is_local_link": r.is_local_link,
                "link": r.link
            }
            json.dump(succ, out)
    except Exception as e:
        fail : ProverFailure = {
            "sort": "failure",
            "exc_str": str(e)
        }
        json.dump(fail, out)

sys.exit(0)
