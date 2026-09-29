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

/// @notice Deploys the guard with a budget that must be stated explicitly.
/// @dev There is deliberately no default budget. Deploying an unbounded guard by accident is
///      the failure mode the contract exists to prevent, so every cap must be provided and
///      the epoch cap must be able to cover at least one full payment.
contract DeployPaymentGuard {
    VmDeploy private constant vm = VmDeploy(address(uint160(uint256(keccak256("hevm cheat code")))));
    uint256 private constant ARC_TESTNET_CHAIN_ID = 5_042_002;
    address private constant ARC_TESTNET_USDC = 0x3600000000000000000000000000000000000000;

    function run() external returns (PaymentGuard guard) {
        require(vm.chainId() == ARC_TESTNET_CHAIN_ID, "Arc Testnet only");

        // Amounts are ERC-20 USDC units (6 decimals); the epoch length is in seconds.
        uint256 perPaymentCap = vm.envUint("PAYMENT_GUARD_PER_PAYMENT_CAP");
        uint256 epochCap = vm.envUint("PAYMENT_GUARD_EPOCH_CAP");
        uint256 recipientEpochCap = vm.envUint("PAYMENT_GUARD_RECIPIENT_EPOCH_CAP");
        uint256 epochLength = vm.envUint("PAYMENT_GUARD_EPOCH_LENGTH_SECONDS");

        require(perPaymentCap > 0, "PAYMENT_GUARD_PER_PAYMENT_CAP must be a stated budget");
        require(epochCap >= perPaymentCap, "epoch cap must cover at least one payment");
        require(recipientEpochCap <= epochCap, "recipient cap cannot exceed the epoch cap");
        require(epochLength >= 60, "epoch must be at least a minute");
        require(perPaymentCap <= type(uint96).max, "per-payment cap exceeds uint96");
        require(epochCap <= type(uint96).max, "epoch cap exceeds uint96");
        require(recipientEpochCap <= type(uint96).max, "recipient cap exceeds uint96");

        uint256 deployerKey = vm.envUint("DEPLOYER_PRIVATE_KEY");
        uint256 policyKey = vm.envUint("PERMIT_SIGNING_PRIVATE_KEY");
        address policySigner = vm.addr(policyKey);
        // Separation of duties: the key that pays for deployment must not also be the key
        // whose signature authorizes payments.
        require(vm.addr(deployerKey) != policySigner, "deployer and policy signer must differ");

        vm.startBroadcast(deployerKey);
        guard = new PaymentGuard(
            ARC_TESTNET_USDC,
            policySigner,
            uint96(perPaymentCap),
            uint96(epochCap),
            uint96(recipientEpochCap),
            uint64(epochLength)
        );
        vm.stopBroadcast();
    }
}
