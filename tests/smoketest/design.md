# Answer — smoke test

This is a deliberately trivial smoke test. The whole point is to exercise the
autoprove pipeline end-to-end with the smallest possible input, **not** to
demonstrate creative specification work.

## Contract

`src/Answer.sol` defines one contract, `Answer`, with one function:

```solidity
function theAnswer() external pure returns (uint256) {
    return 42;
}
```

That's the entire contract. There is nothing else.

## Property to verify

There is exactly **one** property:

- `theAnswer()` always returns `42`.

## Instructions for the autoprove pipeline

Do not extract additional properties. Do not consider edge cases (there
are none). Do not propose invariants beyond the literal "this function
returns 42" property. Do not look for security implications, gas
considerations, or composability concerns — none of those apply here.

The expected output is a CVL spec containing one rule along the lines
of:

```cvl
rule theAnswerIs42 {
    env e;
    assert theAnswer(e) == 42;
}
```

If you find yourself thinking hard about this, you've already gone too
far.
