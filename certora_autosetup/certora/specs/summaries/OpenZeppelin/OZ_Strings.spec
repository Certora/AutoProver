import "../Strings.spec";

methods {
    // TODO: consider if we want to map the resulting string from a ghost or not.
    function Strings.toString(uint256) internal returns (string memory) => nondet_string();
    // TODO: The Strings library is full with 'fun' functions for PTA. Consider summarizing those.
}
