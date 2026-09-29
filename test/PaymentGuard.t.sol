// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {PaymentGuard} from "../src/PaymentGuard.sol";

interface VmGuardTest {
    function addr(uint256 privateKey) external returns (address);
    function sign(uint256 privateKey, bytes32 digest) external returns (uint8 v, bytes32 r, bytes32 s);
    function warp(uint256 timestamp) external;
    function chainId(uint256 newChainId) external;
}

contract MockArcUSDC {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 permitted = allowance[from][msg.sender];
        require(permitted >= amount, "allowance");
        require(balanceOf[from] >= amount, "balance");
        allowance[from][msg.sender] = permitted - amount;
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        return true;
    }
}

contract PaymentGuardTest {
    VmGuardTest private constant vm = VmGuardTest(address(uint160(uint256(keccak256("hevm cheat code")))));
    uint256 private constant POLICY_KEY = 0xA11CE;
    uint256 private constant CHAIN_ID = 5_042_002;
    MockArcUSDC private token;
    PaymentGuard private guard;
    address private policySigner;
    address private recipient = address(0xBEEF);

    function setUp() public {
        vm.chainId(CHAIN_ID);
        token = new MockArcUSDC();
        policySigner = vm.addr(POLICY_KEY);
        guard = new PaymentGuard(address(token), policySigner);
        token.mint(address(this), 10_000_000);
        token.approve(address(guard), 10_000_000);
    }

    function test_executesExactAuthorizedPayment() public {
        PaymentGuard.Permit memory permit = _permit(1, recipient, 2_500_000);
        guard.pay(permit, _sign(guard, permit));
        require(token.balanceOf(recipient) == permit.amount, "wrong recipient amount");
        require(token.balanceOf(address(this)) == 7_500_000, "wrong payer balance");
        require(guard.used(permit.paymentId), "payment id not consumed");
    }

    function test_replayIsRejected() public {
        PaymentGuard.Permit memory permit = _permit(2, recipient, 100);
        bytes memory signature = _sign(guard, permit);
        guard.pay(permit, signature);
        (bool success,) = address(guard).call(abi.encodeCall(PaymentGuard.pay, (permit, signature)));
        require(!success, "replay unexpectedly succeeded");
        require(token.balanceOf(recipient) == 100, "duplicate transfer occurred");
    }

    function test_alteredRecipientAndAmountInvalidateSignature() public {
        PaymentGuard.Permit memory permit = _permit(3, recipient, 100);
        bytes memory signature = _sign(guard, permit);
        permit.recipient = address(0xCAFE);
        (bool changedRecipient,) = address(guard).call(abi.encodeCall(PaymentGuard.pay, (permit, signature)));
        require(!changedRecipient, "altered recipient accepted");
        permit.recipient = recipient;
        permit.amount = 101;
        (bool changedAmount,) = address(guard).call(abi.encodeCall(PaymentGuard.pay, (permit, signature)));
        require(!changedAmount, "altered amount accepted");
        require(token.balanceOf(recipient) == 0 && token.balanceOf(address(0xCAFE)) == 0, "altered permit transferred funds");
    }

    function test_wrongTokenPayerAndZeroFieldsRejected() public {
        PaymentGuard.Permit memory permit = _permit(4, recipient, 10);
        permit.token = address(0x1234);
        (bool wrongToken,) = address(guard).call(abi.encodeCall(PaymentGuard.pay, (permit, _sign(guard, permit))));
        require(!wrongToken, "wrong token accepted");

        permit = _permit(5, recipient, 10);
        permit.payer = address(0x1234);
        (bool wrongPayer,) = address(guard).call(abi.encodeCall(PaymentGuard.pay, (permit, _sign(guard, permit))));
        require(!wrongPayer, "wrong payer accepted");

        permit = _permit(6, address(0), 10);
        (bool zeroRecipient,) = address(guard).call(abi.encodeCall(PaymentGuard.pay, (permit, _sign(guard, permit))));
        require(!zeroRecipient, "zero recipient accepted");

        permit = _permit(7, recipient, 0);
        (bool zeroAmount,) = address(guard).call(abi.encodeCall(PaymentGuard.pay, (permit, _sign(guard, permit))));
        require(!zeroAmount, "zero amount accepted");

        permit = _permit(0, recipient, 10);
        (bool zeroPaymentId,) = address(guard).call(abi.encodeCall(PaymentGuard.pay, (permit, _sign(guard, permit))));
        require(!zeroPaymentId, "zero payment id accepted");
    }

    function test_expiredPermitRejected() public {
        vm.warp(1_800_000_000);
        PaymentGuard.Permit memory permit = _permit(8, recipient, 100);
        permit.expiry = uint64(block.timestamp - 1);
        (bool success,) = address(guard).call(abi.encodeCall(PaymentGuard.pay, (permit, _sign(guard, permit))));
        require(!success, "expired permit accepted");
    }

    function test_wrongChainAndContractDomainRejected() public {
        PaymentGuard.Permit memory permit = _permit(9, recipient, 100);
        bytes memory signature = _sign(guard, permit);
        vm.chainId(1);
        (bool wrongChain,) = address(guard).call(abi.encodeCall(PaymentGuard.pay, (permit, signature)));
        require(!wrongChain, "wrong chain accepted");
        vm.chainId(CHAIN_ID);

        PaymentGuard secondGuard = new PaymentGuard(address(token), policySigner);
        token.approve(address(secondGuard), 100);
        (bool wrongDomain,) = address(secondGuard).call(abi.encodeCall(PaymentGuard.pay, (permit, signature)));
        require(!wrongDomain, "wrong contract domain accepted");
    }

    function test_transferFailureDoesNotConsumePaymentId() public {
        PaymentGuard.Permit memory permit = _permit(10, recipient, 100);
        token.approve(address(guard), 0);
        (bool success,) = address(guard).call(abi.encodeCall(PaymentGuard.pay, (permit, _sign(guard, permit))));
        require(!success, "payment without allowance succeeded");
        require(!guard.used(permit.paymentId), "failed transfer consumed payment id");
    }

    function test_deploymentIsRestrictedToArcTestnet() public {
        vm.chainId(1);
        (bool success,) = address(this).call(abi.encodeCall(this.deployOnCurrentChain, ()));
        require(!success, "guard deployed off Arc Testnet");
    }

    function deployOnCurrentChain() external returns (address) {
        return address(new PaymentGuard(address(token), policySigner));
    }

    function test_wrongSignerRejected() public {
        PaymentGuard.Permit memory permit = _permit(11, recipient, 100);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(uint256(0xB0B), guard.hashPermit(permit));
        bytes memory signature = abi.encodePacked(r, s, v);
        (bool success,) = address(guard).call(abi.encodeCall(PaymentGuard.pay, (permit, signature)));
        require(!success, "wrong policy signer accepted");
    }

    function _permit(uint256 nonce, address to, uint256 amount) private view returns (PaymentGuard.Permit memory) {
        return PaymentGuard.Permit({
            payer: address(this),
            token: address(token),
            recipient: to,
            amount: amount,
            evidenceHash: keccak256(abi.encode("evidence", nonce)),
            paymentId: bytes32(nonce),
            expiry: uint64(block.timestamp + 300)
        });
    }

    function _sign(PaymentGuard target, PaymentGuard.Permit memory permit) private returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(POLICY_KEY, target.hashPermit(permit));
        return abi.encodePacked(r, s, v);
    }
}
