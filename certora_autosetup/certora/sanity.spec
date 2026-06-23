//use builtin rule sanity;

// turns out some codes do have an 'f'! (seen in a real project)
rule sanity {
    env e;
    calldataarg args;
    method certoraF;
    certoraF(e, args);
    satisfy true, "sanity check failed";
}