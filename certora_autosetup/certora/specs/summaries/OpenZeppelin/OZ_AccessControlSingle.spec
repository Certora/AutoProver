// Specification for enforcing single ownership in AccessControl
// Ensures that for any role, at most one address can have that role

// Reference: Ideal single ownership constraint (cannot be directly used due to hasRole in quantifier)
// function singleOwnership() {
//     require forall bytes32 role. forall address addr1. forall address addr2. 
//         hasRole(role, addr1) && hasRole(role, addr2) => addr1 == addr2;
// }

// Function to check single ownership using ghost mapping
// This can be used in rules to detect violations of single ownership
function singleOwnership_$CONTRACT_NAME$() returns bool {
    return forall bytes32 role. forall address addr1. forall address addr2.
        $CONTRACT_NAME$._roles[role].hasRole[addr1] && $CONTRACT_NAME$._roles[role].hasRole[addr2] => addr1 == addr2;
}