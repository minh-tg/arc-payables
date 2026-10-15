// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {AuditAnchor} from "../src/AuditAnchor.sol";

interface VmDeployAuditAnchor {
    function envUint(string calldata name) external returns (uint256);
    function envAddress(string calldata name) external returns (address);
    function startBroadcast(uint256 privateKey) external;
    function stopBroadcast() external;
}

/// @notice Deploys the audit anchor with an owner that must be stated explicitly.
/// @dev The owner is the only address that can publish a head. It may be a different address
///      from the deployer, so publishing authority does not have to sit with the deploy key.
contract DeployAuditAnchor {
    VmDeployAuditAnchor private constant vm =
        VmDeployAuditAnchor(address(uint160(uint256(keccak256("hevm cheat code")))));
    uint256 private constant ARC_TESTNET_CHAIN_ID = 5_042_002;

    function run() external returns (AuditAnchor anchor) {
        require(block.chainid == ARC_TESTNET_CHAIN_ID, "Arc Testnet only");
        address owner = vm.envAddress("AUDIT_ANCHOR_OWNER");
        require(owner != address(0), "AUDIT_ANCHOR_OWNER must be an address");
        uint256 deployerKey = vm.envUint("DEPLOYER_PRIVATE_KEY");

        vm.startBroadcast(deployerKey);
        anchor = new AuditAnchor(owner);
        vm.stopBroadcast();
    }
}
