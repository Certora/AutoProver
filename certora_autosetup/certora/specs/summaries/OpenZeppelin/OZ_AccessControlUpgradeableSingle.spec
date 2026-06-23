// Specification for enforcing single ownership in AccessControlUpgradeable
// Ensures that for any role, at most one address can have that role

// Reference: Ideal single ownership constraint (cannot be directly used due to hasRole in quantifier)
// function singleOwnership() {
//     require forall bytes32 role. forall address addr1. forall address addr2. 
//         hasRole(role, addr1) && hasRole(role, addr2) => addr1 == addr2;
// }

// Template for storage hooks on AccessControl using automatic ERC-7201 storage extension
// This will be duplicated per contract that has openzeppelin.storage.AccessControl namespace
// Requires "storage_extension_annotation": true in the conf file

// Ghost mapping to mirror the AccessControl roles storage
ghost mapping(bytes32 => mapping(address => bool)) ghostRoles_$CONTRACT_NAME$;

// Hook on writes to the hasRole mapping using automatic storage extension
hook Sstore $CONTRACT_NAME$.ext_openzeppelin_storage_AccessControl._roles[KEY bytes32 role].hasRole[KEY address account] bool hasRole {
    ghostRoles_$CONTRACT_NAME$[role][account] = hasRole;
}

// Hook on reads from the hasRole mapping using automatic storage extension
hook Sload bool hasRole $CONTRACT_NAME$.ext_openzeppelin_storage_AccessControl._roles[KEY bytes32 role].hasRole[KEY address account] {
    require ghostRoles_$CONTRACT_NAME$[role][account] == hasRole;
}

// Function to check single ownership using ghost mapping
// This can be used in rules to detect violations of single ownership
function singleOwnership_$CONTRACT_NAME$() returns bool {
    return forall bytes32 role. forall address addr1. forall address addr2.
        ghostRoles_$CONTRACT_NAME$[role][addr1] && ghostRoles_$CONTRACT_NAME$[role][addr2] => addr1 == addr2;
}