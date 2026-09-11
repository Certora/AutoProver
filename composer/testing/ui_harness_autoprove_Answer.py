"""
AUTO-GENERATED fake-LLM tape for the 'autoprove_Answer' AutoProve scenario.

Recorded by composer.testing.record_tape from a real run: each lane is the ordered
list of a phase's AIMessage responses (keyed by run_task task_id), serialized as
JSON and validated back into AIMessages on import. HarnessFakeLLM replays one per
llm.ainvoke. To curate, edit `_TAPE_JSON` (drop diverging turns, fix the tail, etc.).

Replay with the SAME CLI flags used to record:

    COMPOSER_TEST_TAPE=autoprove_Answer console-autoprove <project> <Contract.sol:Contract> \
        <system.md> --max-bug-rounds 1 [--interactive]

Lanes captured: system-analysis=4, harness=4, extract-0=3, formalize-0=7
"""

import json

from langchain_core.messages import AIMessage, BaseMessage

from composer.testing.harness_tape import HarnessFakeLLM, install_fake_llm

# task_id -> ordered list of recorded AIMessage responses (pydantic model_dump JSON).
_TAPE_JSON = r"""
{
  "system-analysis": [
    {
      "content": [
        "\n\nLet me start by checking my memory for any prior progress on this task."
      ],
      "additional_kwargs": {},
      "response_metadata": {},
      "type": "ai",
      "tool_calls": [],
      "invalid_tool_calls": []
    },
    {
      "content": [
        "No prior progress. Let me read the source file directly.",
        {
          "type": "tool_use",
          "id": "toolu_29e35e56ec0c47ddb18d",
          "name": "get_file",
          "input": {
            "path": "src/Answer.sol"
          }
        }
      ],
      "additional_kwargs": {},
      "response_metadata": {},
      "type": "ai",
      "tool_calls": [
        {
          "name": "get_file",
          "args": {
            "path": "src/Answer.sol"
          },
          "id": "toolu_29e35e56ec0c47ddb18d",
          "type": "tool_call"
        }
      ],
      "invalid_tool_calls": []
    },
    {
      "content": [
        "The implementation matches the design document exactly. Let me draft the result.",
        {
          "type": "tool_use",
          "id": "toolu_3f52900a76ca43c4a4ce",
          "name": "write_rough_draft",
          "input": {
            "rough_draft": "\nApplication Type: Smoke Test Contract\nDescription: A deliberately trivial smoke-test system consisting of a single contract with a single pure function. It is designed to exercise an auto-prove pipeline end-to-end on the smallest possible input.\n\nComponents:\n1. Explicit Contract: Answer (singleton)\n   - Path: src/Answer.sol\n   - Description: A stateless singleton contract that exposes a single pure function returning the constant value 42.\n   - Component: \"Answer\"\n     - Description: Provides a single external entry point that always returns the constant value 42.\n     - Entry points: theAnswer()\n     - State variables: none\n     - Interactions: none\n     - Requirements: theAnswer() always returns 42.\n\nNo external actors.\n"
          }
        }
      ],
      "additional_kwargs": {},
      "response_metadata": {},
      "type": "ai",
      "tool_calls": [
        {
          "name": "write_rough_draft",
          "args": {
            "rough_draft": "\nApplication Type: Smoke Test Contract\nDescription: A deliberately trivial smoke-test system consisting of a single contract with a single pure function. It is designed to exercise an auto-prove pipeline end-to-end on the smallest possible input.\n\nComponents:\n1. Explicit Contract: Answer (singleton)\n   - Path: src/Answer.sol\n   - Description: A stateless singleton contract that exposes a single pure function returning the constant value 42.\n   - Component: \"Answer\"\n     - Description: Provides a single external entry point that always returns the constant value 42.\n     - Entry points: theAnswer()\n     - State variables: none\n     - Interactions: none\n     - Requirements: theAnswer() always returns 42.\n\nNo external actors.\n"
          },
          "id": "toolu_3f52900a76ca43c4a4ce",
          "type": "tool_call"
        }
      ],
      "invalid_tool_calls": []
    },
    {
      "content": [
        "The draft is accurate and aligns with both the design document and the source code. Let me submit the final result.",
        {
          "type": "tool_use",
          "id": "toolu_41861aafe3a2438aa075",
          "name": "result",
          "input": {
            "application_type": "Smoke Test Contract",
            "description": "A deliberately trivial smoke-test system consisting of a single contract with a single pure function. It is designed to exercise an auto-prove pipeline end-to-end on the smallest possible input, not to demonstrate creative specification work.",
            "components": [
              {
                "sort": "singleton",
                "name": "Answer",
                "description": "A stateless singleton contract that exposes a single pure function returning the constant value 42. It holds no state and has no interactions with any other contracts or external actors.",
                "path": "src/Answer.sol",
                "components": [
                  {
                    "name": "Answer",
                    "description": "Provides a single external entry point, theAnswer(), which is a pure function that always returns the constant value 42. The component holds no state and has no interactions with other contracts or actors.",
                    "external_entry_points": [
                      "theAnswer()"
                    ],
                    "state_variables": [],
                    "interactions": [],
                    "requirements": [
                      "theAnswer() always returns 42."
                    ]
                  }
                ]
              }
            ]
          }
        }
      ],
      "additional_kwargs": {},
      "response_metadata": {},
      "type": "ai",
      "tool_calls": [
        {
          "name": "result",
          "args": {
            "application_type": "Smoke Test Contract",
            "description": "A deliberately trivial smoke-test system consisting of a single contract with a single pure function. It is designed to exercise an auto-prove pipeline end-to-end on the smallest possible input, not to demonstrate creative specification work.",
            "components": [
              {
                "sort": "singleton",
                "name": "Answer",
                "description": "A stateless singleton contract that exposes a single pure function returning the constant value 42. It holds no state and has no interactions with any other contracts or external actors.",
                "path": "src/Answer.sol",
                "components": [
                  {
                    "name": "Answer",
                    "description": "Provides a single external entry point, theAnswer(), which is a pure function that always returns the constant value 42. The component holds no state and has no interactions with other contracts or actors.",
                    "external_entry_points": [
                      "theAnswer()"
                    ],
                    "state_variables": [],
                    "interactions": [],
                    "requirements": [
                      "theAnswer() always returns 42."
                    ]
                  }
                ]
              }
            ]
          },
          "id": "toolu_41861aafe3a2438aa075",
          "type": "tool_call"
        }
      ],
      "invalid_tool_calls": []
    }
  ],
  "harness": [
    {
      "content": [
        "\n\nI'll start by checking my memory directory for any earlier progress, then work through the task systematically."
      ],
      "additional_kwargs": {},
      "response_metadata": {},
      "type": "ai",
      "tool_calls": [],
      "invalid_tool_calls": []
    },
    {
      "content": [
        "No earlier progress. Let me start fresh. First, let me examine the source code to confirm the structure.",
        {
          "type": "tool_use",
          "id": "toolu_8178cf0022a94240b735",
          "name": "list_files",
          "input": {}
        }
      ],
      "additional_kwargs": {},
      "response_metadata": {},
      "type": "ai",
      "tool_calls": [
        {
          "name": "list_files",
          "args": {},
          "id": "toolu_8178cf0022a94240b735",
          "type": "tool_call"
        }
      ],
      "invalid_tool_calls": []
    },
    {
      "content": [
        "This is indeed a trivially simple contract. Let me now work through all steps and record my findings."
      ],
      "additional_kwargs": {},
      "response_metadata": {},
      "type": "ai",
      "tool_calls": [],
      "invalid_tool_calls": []
    },
    {
      "content": [
        "Now I'll output the final results:",
        {
          "type": "tool_use",
          "id": "toolu_d0fbe92b78ff485ca6f1",
          "name": "result",
          "input": {
            "erc20_contracts": [],
            "external_interfaces": [],
            "non_trivial_state": "The Answer contract is completely stateless \u2014 it has no storage variables, no primary entity, and no managed state of any kind. It consists of a single pure function theAnswer() that always returns the constant 42. Since there is no state, the concept of a \"non-trivial state\" is vacuous/degenerate. A single instance of Answer with no additional setup fully captures all possible behaviors of the contract.",
            "transitive_closure": [
              {
                "name": "Answer",
                "link_fields": [],
                "num_instances": null
              }
            ]
          }
        }
      ],
      "additional_kwargs": {},
      "response_metadata": {},
      "type": "ai",
      "tool_calls": [
        {
          "name": "result",
          "args": {
            "erc20_contracts": [],
            "external_interfaces": [],
            "non_trivial_state": "The Answer contract is completely stateless \u2014 it has no storage variables, no primary entity, and no managed state of any kind. It consists of a single pure function theAnswer() that always returns the constant 42. Since there is no state, the concept of a \"non-trivial state\" is vacuous/degenerate. A single instance of Answer with no additional setup fully captures all possible behaviors of the contract.",
            "transitive_closure": [
              {
                "name": "Answer",
                "link_fields": [],
                "num_instances": null
              }
            ]
          },
          "id": "toolu_d0fbe92b78ff485ca6f1",
          "type": "tool_call"
        }
      ],
      "invalid_tool_calls": []
    }
  ],
  "extract-0": [
    {
      "content": [
        "\n\nI'll start by examining the implementation of the Answer contract.",
        {
          "type": "tool_use",
          "id": "toolu_6453d8ea967c4af7b6d4",
          "name": "list_files",
          "input": {}
        }
      ],
      "additional_kwargs": {},
      "response_metadata": {},
      "type": "ai",
      "tool_calls": [
        {
          "name": "list_files",
          "args": {},
          "id": "toolu_6453d8ea967c4af7b6d4",
          "type": "tool_call"
        }
      ],
      "invalid_tool_calls": []
    },
    {
      "content": [
        "The contract is extremely simple. Let me draft my properties.",
        {
          "type": "tool_use",
          "id": "toolu_9393aa9367ad45a8bd9e",
          "name": "write_rough_draft",
          "input": {
            "rough_draft": "\nProperties for Answer contract:\n\n1. **theAnswer_returns_42** (safety_property)\n   - Methods: theAnswer()\n   - Description: theAnswer() must always return exactly 42.\n   - Rationale: This is the sole requirement for the contract. A correct implementation must satisfy this.\n"
          }
        }
      ],
      "additional_kwargs": {},
      "response_metadata": {},
      "type": "ai",
      "tool_calls": [
        {
          "name": "write_rough_draft",
          "args": {
            "rough_draft": "\nProperties for Answer contract:\n\n1. **theAnswer_returns_42** (safety_property)\n   - Methods: theAnswer()\n   - Description: theAnswer() must always return exactly 42.\n   - Rationale: This is the sole requirement for the contract. A correct implementation must satisfy this.\n"
          },
          "id": "toolu_9393aa9367ad45a8bd9e",
          "type": "tool_call"
        }
      ],
      "invalid_tool_calls": []
    },
    {
      "content": [
        "The contract is trivial: a single pure function with no state, no interactions, no parameters. The only meaningful property is the one stated requirement \u2014 that `theAnswer()` returns 42. There are no state variables to form invariants over, no access control, no external calls, no arithmetic that could overflow, no oracle interactions, no reentrancy surface, no rounding. Proposing anything beyond the core requirement would be padding.",
        {
          "type": "tool_use",
          "id": "toolu_ea2a9fe624e84be0b075",
          "name": "result",
          "input": {
            "items": [
              {
                "title": "theAnswer_returns_42",
                "methods": [
                  "theAnswer()"
                ],
                "sort": "safety_property",
                "description": "theAnswer() must always return exactly 42."
              }
            ],
            "reasoning": "The Answer contract contains a single external pure function `theAnswer()` that takes no arguments and returns a uint256. There is no state, no constructor logic, no modifiers, no external calls, no other functions, and no inheritance. The only requirement is that `theAnswer()` returns 42. This is the sole meaningful, verifiable property. I considered whether there were any invariants to state (there are none \u2014 no state variables exist), any attack vectors (there are none \u2014 no state mutations, no external interactions, no parameters to manipulate), or any other safety properties (there are none \u2014 pure function with a single behavior). Returning a single property is the honest, correct result for this trivially simple contract."
          }
        }
      ],
      "additional_kwargs": {},
      "response_metadata": {},
      "type": "ai",
      "tool_calls": [
        {
          "name": "result",
          "args": {
            "items": [
              {
                "title": "theAnswer_returns_42",
                "methods": [
                  "theAnswer()"
                ],
                "sort": "safety_property",
                "description": "theAnswer() must always return exactly 42."
              }
            ],
            "reasoning": "The Answer contract contains a single external pure function `theAnswer()` that takes no arguments and returns a uint256. There is no state, no constructor logic, no modifiers, no external calls, no other functions, and no inheritance. The only requirement is that `theAnswer()` returns 42. This is the sole meaningful, verifiable property. I considered whether there were any invariants to state (there are none \u2014 no state variables exist), any attack vectors (there are none \u2014 no state mutations, no external interactions, no parameters to manipulate), or any other safety properties (there are none \u2014 pure function with a single behavior). Returning a single property is the honest, correct result for this trivially simple contract."
          },
          "id": "toolu_ea2a9fe624e84be0b075",
          "type": "tool_call"
        }
      ],
      "invalid_tool_calls": []
    }
  ],
  "formalize-0": [
    {
      "content": [
        "Writing the component spec for the sole property.",
        {
          "type": "tool_use",
          "id": "toolu_9ea2e6c4dde944fcabd1",
          "name": "put_cvl_raw",
          "input": {
            "cvl_file": "import \"summaries/Answer_base_summaries.spec\";\n\nmethods {\n    function theAnswer() external returns (uint256) envfree;\n}\n\n/// @title theAnswer_returns_42\n/// Property: theAnswer() must always return exactly 42.\nrule theAnswer_returns_42 {\n    mathint result = theAnswer();\n    assert result == 42, \"theAnswer() must return 42\";\n}\n"
          }
        }
      ],
      "additional_kwargs": {},
      "response_metadata": {},
      "type": "ai",
      "tool_calls": [
        {
          "name": "put_cvl_raw",
          "args": {
            "cvl_file": "import \"summaries/Answer_base_summaries.spec\";\n\nmethods {\n    function theAnswer() external returns (uint256) envfree;\n}\n\n/// @title theAnswer_returns_42\n/// Property: theAnswer() must always return exactly 42.\nrule theAnswer_returns_42 {\n    mathint result = theAnswer();\n    assert result == 42, \"theAnswer() must return 42\";\n}\n"
          },
          "id": "toolu_9ea2e6c4dde944fcabd1",
          "type": "tool_call"
        }
      ],
      "invalid_tool_calls": []
    },
    {
      "content": [
        "Requesting judge feedback on the spec.",
        {
          "type": "tool_use",
          "id": "toolu_a54ca1f6748e4812b7a2",
          "name": "feedback_tool",
          "input": {}
        }
      ],
      "additional_kwargs": {},
      "response_metadata": {},
      "type": "ai",
      "tool_calls": [
        {
          "name": "feedback_tool",
          "args": {},
          "id": "toolu_a54ca1f6748e4812b7a2",
          "type": "tool_call"
        }
      ],
      "invalid_tool_calls": []
    },
    {
      "content": [
        "Judge: inspecting the spec and drafting a verdict.",
        {
          "type": "tool_use",
          "id": "toolu_de079bec10174412a5f0",
          "name": "get_cvl",
          "input": {}
        },
        {
          "type": "tool_use",
          "id": "toolu_cd7fc1c3d080457eb1a6",
          "name": "write_rough_draft",
          "input": {
            "rough_draft": "The spec declares theAnswer() envfree and asserts it returns exactly 42 via rule theAnswer_returns_42 \u2014 it faithfully encodes the sole extracted property. Coverage is complete. Verdict: GOOD."
          }
        }
      ],
      "additional_kwargs": {},
      "response_metadata": {},
      "type": "ai",
      "tool_calls": [
        {
          "name": "get_cvl",
          "args": {},
          "id": "toolu_de079bec10174412a5f0",
          "type": "tool_call"
        },
        {
          "name": "write_rough_draft",
          "args": {
            "rough_draft": "The spec declares theAnswer() envfree and asserts it returns exactly 42 via rule theAnswer_returns_42 \u2014 it faithfully encodes the sole extracted property. Coverage is complete. Verdict: GOOD."
          },
          "id": "toolu_cd7fc1c3d080457eb1a6",
          "type": "tool_call"
        }
      ],
      "invalid_tool_calls": []
    },
    {
      "content": [
        "Judge: reading the draft before the verdict.",
        {
          "type": "tool_use",
          "id": "toolu_cc5034c646da42529fc6",
          "name": "read_rough_draft",
          "input": {}
        }
      ],
      "additional_kwargs": {},
      "response_metadata": {},
      "type": "ai",
      "tool_calls": [
        {
          "name": "read_rough_draft",
          "args": {},
          "id": "toolu_cc5034c646da42529fc6",
          "type": "tool_call"
        }
      ],
      "invalid_tool_calls": []
    },
    {
      "content": [
        "Judge: approving the spec.",
        {
          "type": "tool_use",
          "id": "toolu_0220b7b5f6ba41368d5c",
          "name": "result",
          "input": {
            "good": true,
            "feedback": ""
          }
        }
      ],
      "additional_kwargs": {},
      "response_metadata": {},
      "type": "ai",
      "tool_calls": [
        {
          "name": "result",
          "args": {
            "good": true,
            "feedback": ""
          },
          "id": "toolu_0220b7b5f6ba41368d5c",
          "type": "tool_call"
        }
      ],
      "invalid_tool_calls": []
    },
    {
      "content": [
        "Feedback is positive. Running the prover.",
        {
          "type": "tool_use",
          "id": "toolu_a31905a7c8f74aff9fa6",
          "name": "verify_spec",
          "input": {
            "rules": null
          }
        }
      ],
      "additional_kwargs": {},
      "response_metadata": {},
      "type": "ai",
      "tool_calls": [
        {
          "name": "verify_spec",
          "args": {
            "rules": null
          },
          "id": "toolu_a31905a7c8f74aff9fa6",
          "type": "tool_call"
        }
      ],
      "invalid_tool_calls": []
    },
    {
      "content": [
        "All rules verified. Finalizing the component CVL.",
        {
          "type": "tool_use",
          "id": "toolu_d3df925984c44764a91c",
          "name": "result",
          "input": {
            "commentary": "Formalized the single safety property: theAnswer() always returns 42. The rule verifies against Answer.sol.",
            "property_rules": [
              {
                "property_title": "theAnswer_returns_42",
                "rules": [
                  "theAnswer_returns_42"
                ]
              }
            ]
          }
        }
      ],
      "additional_kwargs": {},
      "response_metadata": {},
      "type": "ai",
      "tool_calls": [
        {
          "name": "result",
          "args": {
            "commentary": "Formalized the single safety property: theAnswer() always returns 42. The rule verifies against Answer.sol.",
            "property_rules": [
              {
                "property_title": "theAnswer_returns_42",
                "rules": [
                  "theAnswer_returns_42"
                ]
              }
            ]
          },
          "id": "toolu_d3df925984c44764a91c",
          "type": "tool_call"
        }
      ],
      "invalid_tool_calls": []
    }
  ]
}
"""

_TAPE: dict[str, list[BaseMessage]] = {
    task_id: [AIMessage.model_validate(m) for m in messages]
    for task_id, messages in json.loads(_TAPE_JSON).items()
}


def get_autoprove_Answer_llm() -> HarnessFakeLLM:
    """Return a fresh fake LLM loaded with the 'autoprove_Answer' tape."""
    return HarnessFakeLLM(lanes=_TAPE)


def install_harness_tape() -> HarnessFakeLLM:
    """Route the pipeline's models to the Answer tape's fake LLM.
    composer/bind.py calls this when COMPOSER_TEST_TAPE=autoprove_Answer is set."""
    fake = get_autoprove_Answer_llm()
    import composer.spec.agent_index as a_ind
    a_ind._UNSAFE_DISABLE_CACHE = True
    install_fake_llm(fake)
    return fake


__all__ = ["get_autoprove_Answer_llm", "install_harness_tape"]
