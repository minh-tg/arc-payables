// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {PaymentGuard} from "../src/PaymentGuard.sol";

interface VmDeploy {
    function envUint(string calldata name) external returns (uint256);
    function addr(uint256 privateKey) external returns (address);
    function startBroadcast(uint256 privateKey) external;
    function stopBroadcast() external;
    function chainId() external view returns (uint256);
}

contract DeployPaymentGuard {
    VmDeploy private constant vm = VmDeploy(address(uint160(uint256(keccak256("hevm cheat code")))));
    uint256 private constant ARC_TESTNET_CHAIN_ID = 5_042_002;
    address private constant ARC_TESTNET_USDC = 0x3600000000000000000000000000000000000000;

    function run() external returns (PaymentGuard guard) {
        require(vm.chainId() == ARC_TESTNET_CHAIN_ID, "Arc Testnet only");
        uint256 deployerKey = vm.envUint("DEPLOYER_PRIVATE_KEY");
        uint256 policyKey = vm.envUint("PERMIT_SIGNING_PRIVATE_KEY");
        address policySigner = vm.addr(policyKey);
        vm.startBroadcast(deployerKey);
        guard = new PaymentGuard(ARC_TESTNET_USDC, policySigner);
        vm.stopBroadcast();
    }
}
