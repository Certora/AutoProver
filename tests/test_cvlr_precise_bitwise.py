"""``prove --precise-bitwise``: the bit-vector encoding for one job, never by default."""

from composer.spec.cvlr.conf import BASE_CONF, PreciseBitwise, TunableConf, conf_history, tunable_conf


def test_the_default_conf_keeps_the_integer_encoding():
    assert tunable_conf(TunableConf())["prover_args"] == BASE_CONF["prover_args"]


def test_precise_bitwise_swaps_the_integer_encoding_for_bit_vectors():
    args = tunable_conf(TunableConf(precise_bitwise=PreciseBitwise("BWXOr mismatch")))["prover_args"]
    assert "-smt_preciseBitwiseOps" in args
    assert "-solanaTACMathInt false" in args
    assert "-solanaTACMathInt true" not in args
    # Every other fixed flag stays.
    rest = [a for a in BASE_CONF["prover_args"] if not a.startswith("-solanaTACMathInt ")]
    assert all(a in args for a in rest)


def test_precise_bitwise_changes_the_conf_stamp_but_its_reason_does_not():
    off = conf_history(TunableConf())
    on = conf_history(TunableConf(precise_bitwise=PreciseBitwise("one reason")))
    reworded = conf_history(TunableConf(precise_bitwise=PreciseBitwise("another reason")))
    assert off != on
    assert on == reworded
